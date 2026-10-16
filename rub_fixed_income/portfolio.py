"""Portfolio valuation over the domain model (M1 entry point for the desk backend).

``value_portfolio`` prices every position of a book against one shared
:class:`MarketSnapshot` and returns a versioned, hash-verified result in the desk
factor vocabulary. Supported in 0.11.0: fixed-coupon RUB bonds (bullet, amortizing,
confirmed put) on a KBD curve, a prebuilt discount curve or RUONIA OIS quotes.
Everything else is a typed rejection recorded in coverage, never a zero.

Each curve is built once per call and its shocked versions are shared across
positions. Settlement is the instrument's lag in business days of its own calendar
from the snapshot's business date; dates outside a bounded calendar are rejected.
"""

from __future__ import annotations

import json
import math
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime
from typing import Literal, cast

from ._money import finite_money, positive_money
from ._version import __version__
from .bond import YieldRequest, solve_effective_annual_yield
from .canonical import canonical_json_bytes, canonical_sha256
from .compatibility import bond_duration
from .conventions import ZSpreadConvention, calendar_fingerprint
from .curve_specs import CurveKind, get_curve_spec
from .discount import CurveBootstrapRequest, DiscountCurve
from .domain import (
    AccountingCategory,
    CurveEntry,
    Instrument,
    MarketSnapshot,
    Position,
    ProductType,
)
from .errors import CoreRejection, InvalidTerms, UnsupportedProduct
from .factors import FactorRisk, bond_factor_risk
from .kbd import KBD_MODEL_VERSION, ZcycParameters, g_spread_bp, kbd_discount_curve
from .local_risk import (
    FixedRubBondInput,
    PreparedLocalBondRisk,
    RiskRunContext,
    prepare_local_bond_risk,
)
from .risk import BondScenario, ShockedCurveCache
from .serialization import decode

__all__ = [
    "PORTFOLIO_RESULT_SCHEMA",
    "SUPPORTED_PRODUCTS",
    "CalendarUse",
    "CurveUse",
    "FactorTotal",
    "PortfolioCoverage",
    "PortfolioRiskResult",
    "PortfolioScenario",
    "PortfolioScenarioResult",
    "PositionScenarioPnl",
    "PositionValuation",
    "Rejection",
    "ValuationPolicy",
    "portfolio_result_from_json",
    "portfolio_result_to_json",
    "value_portfolio",
    "verify_portfolio_result",
]

PORTFOLIO_RESULT_SCHEMA = "rub-portfolio-risk-result.v2"
SUPPORTED_PRODUCTS = frozenset({ProductType.FIXED_BOND})

CoverageStatus = Literal["complete", "partial", "failed"]
Interruption = Literal["cancelled", "time_budget_exceeded"]
CALCULATION_FAILED = "CALCULATION_FAILED"
_INTERRUPTION_CODES: dict[str, str] = {
    "cancelled": "CANCELLED",
    "time_budget_exceeded": "TIME_BUDGET_EXCEEDED",
}
_monotonic = time.monotonic


@dataclass(frozen=True)
class ValuationPolicy:
    """How the book is valued; part of the job identity."""

    discount_curve_id: str
    spread_kind: Literal["credit", "ofz"] = "credit"
    spread_convention: ZSpreadConvention = field(default_factory=ZSpreadConvention)
    horizon: Literal["maturity", "confirmed_put"] = "maturity"
    curve_overrides: tuple[tuple[str, str], ...] = ()
    include_second_order: bool = True
    bump_bp: float = 1.0

    def __post_init__(self) -> None:
        if not self.discount_curve_id:
            raise InvalidTerms("discount_curve_id is required")
        if self.spread_kind not in ("credit", "ofz"):
            raise InvalidTerms("spread_kind must be credit or ofz")
        if self.horizon not in ("maturity", "confirmed_put"):
            raise InvalidTerms("horizon must be maturity or confirmed_put")
        object.__setattr__(self, "curve_overrides", tuple(sorted(self.curve_overrides)))
        if len({i for i, _ in self.curve_overrides}) != len(self.curve_overrides):
            raise InvalidTerms("duplicate curve override")
        if not isinstance(self.include_second_order, bool):
            raise TypeError("include_second_order must be boolean")
        positive_money(self.bump_bp, "bump_bp")

    def curve_for(self, instrument_id: str) -> str:
        return dict(self.curve_overrides).get(instrument_id, self.discount_curve_id)


@dataclass(frozen=True)
class PortfolioScenario:
    """Bucket shifts of each position's discount curve plus a spread shift (bp)."""

    name: str
    shock: BondScenario

    def __post_init__(self) -> None:
        if not self.name:
            raise InvalidTerms("scenario name is required")


@dataclass(frozen=True)
class Rejection:
    position_id: str
    instrument_id: str
    code: str
    message: str


@dataclass(frozen=True)
class PortfolioCoverage:
    status: CoverageStatus
    expected: int
    completed: int
    rejections: tuple[Rejection, ...]


@dataclass(frozen=True)
class PositionValuation:
    """One computed position. Prices are per bond; PV, risk and JTD are signed RUB."""

    position_id: str
    instrument_id: str
    book: str
    accounting_category: AccountingCategory
    calculation_id: str
    curve_id: str
    spec_id: str
    curve_basis: str
    settlement_date: date
    horizon: Literal["maturity", "confirmed_put"]
    horizon_date: date
    quantity: float
    current_nominal_rub: float
    clean_price_pct: float
    accrued_interest_rub: float
    dirty_price_rub: float
    pv_rub: float
    spread_kind: Literal["credit", "ofz"]
    spread_convention: str
    spread_bp: float
    effective_yield: float
    macaulay_duration_years: float
    g_spread_bp: float | None
    factor_risk: FactorRisk
    jtd_pnl_rub: float | None


@dataclass(frozen=True)
class PositionScenarioPnl:
    position_id: str
    pnl_rub: float | None
    code: str | None


@dataclass(frozen=True)
class PortfolioScenarioResult:
    name: str
    scenario_id: str
    coverage: PortfolioCoverage
    positions: tuple[PositionScenarioPnl, ...]
    covered_pnl_rub: float
    total_pnl_rub: float | None


@dataclass(frozen=True)
class CurveUse:
    curve_id: str
    spec_id: str
    model_version: str


@dataclass(frozen=True)
class CalendarUse:
    calendar_id: str
    version: str
    fingerprint: str
    valid_from: date | None
    valid_to: date | None


@dataclass(frozen=True)
class FactorTotal:
    factor: str
    first_order_rub: float


@dataclass(frozen=True)
class PortfolioRiskResult:
    run_id: str
    job_id: str
    snapshot_id: str
    business_date: date
    as_of: datetime
    library_version: str
    status: CoverageStatus
    coverage: PortfolioCoverage
    positions: tuple[PositionValuation, ...]
    scenarios: tuple[PortfolioScenarioResult, ...]
    curves: tuple[CurveUse, ...]
    calendars: tuple[CalendarUse, ...]
    covered_pv_rub: float
    total_pv_rub: float | None
    factor_totals: tuple[FactorTotal, ...]
    interruption: Interruption | None = None
    schema_version: str = PORTFOLIO_RESULT_SCHEMA
    result_sha256: str = ""


@dataclass(frozen=True)
class _BuiltCurve:
    entry: CurveEntry
    curve: DiscountCurve | CurveBootstrapRequest
    kbd: ZcycParameters | None
    model_version: str


def _build_curve(entry: CurveEntry, business_date: date) -> _BuiltCurve:
    spec = get_curve_spec(entry.spec_id)
    source = entry.curve
    if spec.kind is CurveKind.KBD_PARAMETRIC:
        if isinstance(source, ZcycParameters):
            if source.trade_date != business_date:
                raise InvalidTerms("KBD parameters are not for the snapshot business date")
            return _BuiltCurve(entry, kbd_discount_curve(source), source, KBD_MODEL_VERSION)
        if isinstance(source, DiscountCurve) and source.model_version == KBD_MODEL_VERSION:
            return _BuiltCurve(entry, source, None, KBD_MODEL_VERSION)
        raise InvalidTerms(f"curve {entry.curve_id} does not match spec {spec.spec_id}")
    if spec.kind is CurveKind.OIS_BOOTSTRAP:
        if isinstance(source, CurveBootstrapRequest):
            if source.convention.fixed_leg_schedule is not None:
                raise InvalidTerms(
                    f"curve {entry.curve_id}: {spec.spec_id} uses the legacy fixed-leg schedule"
                )
            return _BuiltCurve(entry, source, None, spec.model_version)
        if isinstance(source, DiscountCurve) and source.model_version == spec.model_version:
            return _BuiltCurve(entry, source, None, spec.model_version)
        raise InvalidTerms(f"curve {entry.curve_id} does not match spec {spec.spec_id}")
    raise InvalidTerms(f"spec {spec.spec_id} is not a bond discount curve")


def _coverage(expected: Sequence[Position], rejections: list[Rejection]) -> PortfolioCoverage:
    failed = {r.position_id for r in rejections}
    completed = sum(1 for p in expected if p.position_id not in failed)
    status: CoverageStatus = "complete" if not failed else "partial" if completed else "failed"
    return PortfolioCoverage(status, len(expected), completed, tuple(rejections))


def value_portfolio(
    *,
    run_id: str,
    as_of: datetime,
    snapshot: MarketSnapshot,
    instruments: Sequence[Instrument],
    positions: Sequence[Position],
    policy: ValuationPolicy,
    scenarios: Sequence[PortfolioScenario] = (),
    time_budget_seconds: float | None = None,
    should_cancel: Callable[[], bool] | None = None,
    curve_cache: ShockedCurveCache | None = None,
) -> PortfolioRiskResult:
    """Value a book and return its risk in desk factors, with explicit coverage.

    A malformed job (duplicate IDs, naive timestamps) raises. Per-position problems
    become rejections with a stable code (``CoreRejection.code``, or
    ``CALCULATION_FAILED`` for a numerical failure). Totals are ``None`` unless
    coverage is complete; covered subtotals are always reported.

    ``curve_cache`` lets the caller keep bootstrapped base and shocked OIS curves
    across calls (seed it from persisted :class:`PrebuiltCurve` entries, read them back
    with :meth:`ShockedCurveCache.export`). Results are identical with or without it.
    """
    if not run_id:
        raise InvalidTerms("run_id is required")
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise InvalidTerms("as_of must carry a UTC offset")
    if time_budget_seconds is not None and finite_money(time_budget_seconds, "time budget") < 0:
        raise ValueError("time_budget_seconds must be nonnegative")
    if curve_cache is not None and not isinstance(curve_cache, ShockedCurveCache):
        raise TypeError("curve_cache must be a ShockedCurveCache")
    for names, label in (
        ([i.instrument_id for i in instruments], "instrument"),
        ([p.position_id for p in positions], "position"),
        ([s.name for s in scenarios], "scenario"),
    ):
        if len(set(names)) != len(names):
            raise ValueError(f"duplicate {label} identifiers")
    started = _monotonic()
    interruption: Interruption | None = None

    def stopped() -> Interruption | None:
        nonlocal interruption
        if interruption is None:
            if should_cancel is not None and should_cancel():
                interruption = "cancelled"
            elif time_budget_seconds is not None and _monotonic() - started >= time_budget_seconds:
                interruption = "time_budget_exceeded"
        return interruption

    by_id = {i.instrument_id: i for i in instruments}
    job_id = canonical_sha256(
        {
            "model": "portfolio-risk-v2",
            "library_version": __version__,
            "run_id": run_id,
            "as_of": as_of,
            "snapshot": canonical_sha256(snapshot),
            "instruments": sorted(canonical_sha256(i) for i in instruments),
            "positions": [asdict(p) for p in positions],
            "policy": policy,
            "scenarios": list(scenarios),
        }
    )
    built: dict[str, _BuiltCurve | CoreRejection] = {}
    cache = ShockedCurveCache() if curve_cache is None else curve_cache
    valuations: list[PositionValuation] = []
    rejections: list[Rejection] = []
    prepared_by_position: dict[str, PreparedLocalBondRisk] = {}
    used_calendars: dict[str, CalendarUse] = {}

    def curve(curve_id: str) -> _BuiltCurve:
        if curve_id not in built:
            try:
                built[curve_id] = _build_curve(snapshot.curve(curve_id), snapshot.business_date)
            except CoreRejection as exc:
                built[curve_id] = exc
        outcome = built[curve_id]
        if isinstance(outcome, CoreRejection):
            raise outcome
        return outcome

    for position in positions:
        reason = stopped()
        if reason is not None:
            rejections.append(
                Rejection(
                    position.position_id,
                    position.instrument_id,
                    _INTERRUPTION_CODES[reason],
                    "job stopped before this position",
                )
            )
            continue
        try:
            valuation, prepared = _value_position(
                position, by_id, snapshot, policy, curve, cache, job_id, run_id, used_calendars
            )
        except CoreRejection as exc:
            rejections.append(
                Rejection(position.position_id, position.instrument_id, exc.code, exc.message)
            )
            continue
        except (ValueError, LookupError, ArithmeticError) as exc:
            rejections.append(
                Rejection(
                    position.position_id, position.instrument_id, CALCULATION_FAILED, str(exc)
                )
            )
            continue
        valuations.append(valuation)
        prepared_by_position[position.position_id] = prepared

    coverage = _coverage(positions, rejections)
    scenario_results = [
        _run_scenario(scenario, positions, prepared_by_position, rejections, job_id, stopped)
        for scenario in scenarios
    ]
    covered_pv = finite_money(math.fsum(v.pv_rub for v in valuations), "covered PV")
    totals: dict[str, list[float]] = {}
    for valuation in valuations:
        for factor, value in valuation.factor_risk.first_order_by_factor().items():
            totals.setdefault(factor, []).append(value)
    factor_totals = tuple(FactorTotal(k, math.fsum(v)) for k, v in sorted(totals.items()))
    status: CoverageStatus = (
        "complete"
        if coverage.status == "complete"
        and all(s.coverage.status == "complete" for s in scenario_results)
        else "partial"
        if coverage.completed
        else "failed"
    )
    curves_used = tuple(
        CurveUse(c.entry.curve_id, c.entry.spec_id, c.model_version)
        for _, c in sorted(built.items())
        if isinstance(c, _BuiltCurve)
    )
    result = PortfolioRiskResult(
        run_id,
        job_id,
        snapshot.snapshot_id,
        snapshot.business_date,
        as_of,
        __version__,
        status,
        coverage,
        tuple(valuations),
        tuple(scenario_results),
        curves_used,
        tuple(used_calendars[k] for k in sorted(used_calendars)),
        covered_pv,
        covered_pv if coverage.status == "complete" else None,
        factor_totals,
        interruption,
    )
    return replace(result, result_sha256=canonical_sha256(portfolio_result_hash_projection(result)))


def _value_position(
    position: Position,
    instruments: Mapping[str, Instrument],
    snapshot: MarketSnapshot,
    policy: ValuationPolicy,
    curve: Callable[[str], _BuiltCurve],
    cache: ShockedCurveCache,
    job_id: str,
    run_id: str,
    used_calendars: dict[str, CalendarUse],
) -> tuple[PositionValuation, PreparedLocalBondRisk]:
    instrument = instruments.get(position.instrument_id)
    if instrument is None:
        raise InvalidTerms("instrument terms were not supplied for this position")
    if instrument.product not in SUPPORTED_PRODUCTS:
        raise UnsupportedProduct(f"{instrument.product.value} is not supported by this release")
    if instrument.currency != "RUB":
        raise UnsupportedProduct("only RUB bonds are supported by this release")
    if position.currency != instrument.currency:
        raise InvalidTerms("position currency differs from the instrument currency")
    if instrument.is_perpetual:
        raise UnsupportedProduct("perpetual bonds need a call assumption (package 9)")
    if any(o.kind.value == "call" for o in instrument.offers):
        raise UnsupportedProduct("callable bonds are not supported (package 9)")
    calendar = snapshot.calendar(instrument.calendar_id)
    used_calendars[instrument.calendar_id] = CalendarUse(
        instrument.calendar_id,
        calendar.version,
        calendar_fingerprint(calendar),
        calendar.valid_from,
        calendar.valid_to,
    )
    settlement = calendar.add_business_days(snapshot.business_date, instrument.settlement_lag_days)
    curve_id = policy.curve_for(instrument.instrument_id)
    built = curve(curve_id)
    horizon_date, offer = instrument.horizon(policy.horizon, settlement)
    flows = instrument.cashflows(after=settlement, horizon=horizon_date, offer=offer)
    if not flows:
        raise InvalidTerms("no cashflows remain after settlement")
    quote = snapshot.quote(instrument.instrument_id)
    accrued = (
        quote.accrued_per_bond
        if quote.accrued_per_bond is not None
        else instrument.accrued(settlement)
    )
    nominal = instrument.outstanding_face(settlement)
    basis: Literal["continuous_zero", "ois_quote"] = (
        "ois_quote" if isinstance(built.curve, CurveBootstrapRequest) else "continuous_zero"
    )
    prepared = prepare_local_bond_risk(
        FixedRubBondInput(
            context=RiskRunContext(
                run_id,
                instrument.instrument_id,
                snapshot.snapshot_id,
                snapshot.snapshot_id,
                f"{snapshot.snapshot_id}:{curve_id}",
                basis,
            ),
            cashflows=flows,
            settlement_date=settlement,
            horizon_date=horizon_date,
            horizon="maturity" if offer is None else "confirmed_investor_put",
            cashflow_currency="RUB",
            coupon_model="fixed",
            current_nominal_rub=nominal,
            clean_price_pct=quote.clean_price_pct,
            accrued_interest_rub=accrued,
            quantity=position.quantity,
            curve=built.curve,
            spread_convention=policy.spread_convention,
            spread_kind=policy.spread_kind,
        ),
        curve_cache=cache,
    )
    engine = prepared.engine
    risk = engine.risk(bump_bp=policy.bump_bp, difference="central")
    second = engine.second_order(bump_bp=policy.bump_bp) if policy.include_second_order else None
    factor_risk = bond_factor_risk(risk, curve_id, second)
    dirty = cast(float, engine.request.dirty_price_rub)
    flow_pairs = tuple((f.payment_date, f.amount) for f in flows)
    effective_yield = solve_effective_annual_yield(
        YieldRequest(settlement, dirty, flows)
    ).effective_annual_yield
    duration = bond_duration(
        annual_yield=effective_yield, settlement_date=settlement, cashflows=flow_pairs
    ).macaulay_years
    g_spread = None if built.kbd is None else g_spread_bp(built.kbd, effective_yield, duration)
    jtd = (
        None
        if position.recovery_rate is None
        else engine.jump_to_default(
            outstanding_nominal_rub_per_bond=nominal, recovery_rate=position.recovery_rate
        ).jtd_pnl_rub
    )
    calculation_id = canonical_sha256(
        {
            "job_id": job_id,
            "position_id": position.position_id,
            "adapter_id": prepared.calculation_id,
        }
    )
    valuation = PositionValuation(
        position.position_id,
        instrument.instrument_id,
        position.book,
        position.accounting_category,
        calculation_id,
        curve_id,
        built.entry.spec_id,
        basis,
        settlement,
        "maturity" if offer is None else "confirmed_put",
        horizon_date,
        position.quantity,
        nominal,
        quote.clean_price_pct,
        accrued,
        dirty,
        risk.pv_rub,
        policy.spread_kind,
        policy.spread_convention.identifier,
        engine.spread_bp,
        effective_yield,
        duration,
        g_spread,
        factor_risk,
        jtd,
    )
    return valuation, prepared


def _run_scenario(
    scenario: PortfolioScenario,
    positions: Sequence[Position],
    prepared: Mapping[str, PreparedLocalBondRisk],
    rejections: list[Rejection],
    job_id: str,
    stopped: Callable[[], Interruption | None],
) -> PortfolioScenarioResult:
    rows: list[PositionScenarioPnl] = []
    failed: list[Rejection] = []
    base_failures = {r.position_id: r for r in rejections}
    for position in positions:
        if position.position_id in base_failures:
            failure = base_failures[position.position_id]
            rows.append(PositionScenarioPnl(position.position_id, None, failure.code))
            failed.append(failure)
            continue
        reason = stopped()
        if reason is not None:
            code = _INTERRUPTION_CODES[reason]
            rows.append(PositionScenarioPnl(position.position_id, None, code))
            failed.append(
                Rejection(position.position_id, position.instrument_id, code, "job stopped")
            )
            continue
        engine = prepared[position.position_id].engine
        try:
            pnl = engine.reprice(scenario.shock).total_pnl_rub
        except (ValueError, ArithmeticError) as exc:
            rows.append(PositionScenarioPnl(position.position_id, None, CALCULATION_FAILED))
            failed.append(
                Rejection(
                    position.position_id, position.instrument_id, CALCULATION_FAILED, str(exc)
                )
            )
            continue
        rows.append(PositionScenarioPnl(position.position_id, pnl, None))
    coverage = _coverage(positions, failed)
    covered = finite_money(
        math.fsum(r.pnl_rub for r in rows if r.pnl_rub is not None), "covered scenario P&L"
    )
    return PortfolioScenarioResult(
        scenario.name,
        canonical_sha256({"job_id": job_id, "scenario": scenario}),
        coverage,
        tuple(rows),
        covered,
        covered if coverage.status == "complete" else None,
    )


def portfolio_result_hash_projection(
    result: PortfolioRiskResult | Mapping[str, object],
) -> dict[str, object]:
    raw = (
        cast(dict[str, object], json.loads(canonical_json_bytes(result)))
        if isinstance(result, PortfolioRiskResult)
        else dict(result)
    )
    if "result_sha256" not in raw:
        raise ValueError("result_sha256 field is required")
    raw["result_sha256"] = ""
    return raw


def verify_portfolio_result(result: PortfolioRiskResult | Mapping[str, object]) -> bool:
    raw = (
        cast(dict[str, object], json.loads(canonical_json_bytes(result)))
        if isinstance(result, PortfolioRiskResult)
        else dict(result)
    )
    if raw.get("schema_version") != PORTFOLIO_RESULT_SCHEMA:
        return False
    declared = raw.get("result_sha256")
    try:
        actual = canonical_sha256(portfolio_result_hash_projection(raw))
    except (TypeError, ValueError):
        return False
    return isinstance(declared, str) and declared == actual


def portfolio_result_to_json(result: PortfolioRiskResult) -> bytes:
    if not verify_portfolio_result(result):
        raise ValueError("portfolio result hash or schema does not verify")
    return canonical_json_bytes(result)


def portfolio_result_from_json(data: bytes | str) -> PortfolioRiskResult:
    try:
        raw = json.loads(data)
    except (TypeError, ValueError) as exc:
        raise ValueError("portfolio result is not valid JSON") from exc
    if not isinstance(raw, dict) or raw.get("schema_version") != PORTFOLIO_RESULT_SCHEMA:
        raise ValueError("not a portfolio result with a supported schema_version")
    if not verify_portfolio_result(raw):
        raise ValueError("portfolio result_sha256 does not verify")
    return cast(PortfolioRiskResult, decode(PortfolioRiskResult, raw, "result"))
