"""Pure local portfolio worker; public snapshots and private positions stay separate.

This consumes already normalized public records, not raw provider responses. Evidence
is checked, not certified: the data adapter owns calendars and contractual terms.
"""

from __future__ import annotations

import math
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import date, datetime
from typing import Literal, cast

from ._money import finite_money
from ._version import __version__
from .analytics import canonical_sha256
from .cashflows import BondCashflow
from .conventions import ZSpreadConvention
from .discount import CurveBootstrapRequest, DiscountCurve
from .local_risk import (
    CurveRiskBasis,
    FixedRubBondInput,
    LocalBondScenarioResult,
    RiskRunContext,
    prepare_local_bond_risk,
)
from .risk import (
    BondDefaultResult,
    BondRiskResult,
    BondScenario,
    BondSecondOrderRisk,
    _curve_identity,
)

CoverageStatus = Literal["complete", "partial", "failed"]


def _text(value: str, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be nonempty text")


def _aware(value: datetime) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must have explicit UTC offsets")


@dataclass(frozen=True)
class SnapshotEvidence:
    snapshot_id: str
    source_id: str
    observed_at: datetime
    known_at: datetime
    valid_until: datetime

    def validate(self, as_of: datetime) -> None:
        _text(self.snapshot_id, "snapshot_id")
        _text(self.source_id, "source_id")
        for value in (self.observed_at, self.known_at, self.valid_until):
            _aware(value)
        if not self.observed_at <= self.known_at <= as_of <= self.valid_until:
            raise ValueError("snapshot is stale, future-known or has inconsistent timestamps")


@dataclass(frozen=True)
class PublicBondSnapshot:
    instrument_id: str
    market: SnapshotEvidence
    schedule: SnapshotEvidence
    curve_evidence: SnapshotEvidence
    calendar_version: str
    schedule_complete: bool
    redemption_confirmed: bool
    cashflows: tuple[BondCashflow, ...]
    settlement_date: date
    horizon_date: date
    horizon: str
    cashflow_currency: str
    coupon_model: str
    current_nominal_rub: float
    clean_price_pct: float
    accrued_interest_rub: float
    curve: DiscountCurve | CurveBootstrapRequest
    spread_convention: ZSpreadConvention
    spread_kind: Literal["credit", "ofz"]
    source_payload_id: str | None = None

    @property
    def content_id(self) -> str:
        # Convention identifiers include calendar fingerprints; serialize them rather
        # than Enum/frozenset implementation details from dataclasses.asdict.
        payload = asdict(self)
        payload["curve"] = _curve_identity(self.curve)
        payload["spread_convention"] = self.spread_convention.identifier
        return canonical_sha256(payload)

    def local_input(
        self, *, run_id: str, as_of: datetime, basis: CurveRiskBasis, quantity: float
    ) -> FixedRubBondInput:
        _text(self.instrument_id, "instrument_id")
        _text(self.calendar_version, "calendar_version")
        for evidence in (self.market, self.schedule, self.curve_evidence):
            evidence.validate(as_of)
        if self.schedule_complete is not True or self.redemption_confirmed is not True:
            raise ValueError("complete schedule and confirmed redemption are required")
        if self.cashflow_currency != "RUB" or self.coupon_model != "fixed":
            raise ValueError("unsupported product: only fixed RUB cashflows are eligible")
        if self.horizon not in ("maturity", "confirmed_investor_put"):
            raise ValueError("unsupported redemption horizon")
        # Dates are explicit economic inputs. No machine-timezone or T+1 inference.
        return FixedRubBondInput(
            context=RiskRunContext(
                run_id,
                self.instrument_id,
                self.market.snapshot_id,
                self.schedule.snapshot_id,
                self.curve_evidence.snapshot_id,
                basis,
            ),
            cashflows=self.cashflows,
            settlement_date=self.settlement_date,
            horizon_date=self.horizon_date,
            horizon=cast(Literal["maturity", "confirmed_investor_put"], self.horizon),
            cashflow_currency="RUB",
            coupon_model="fixed",
            current_nominal_rub=self.current_nominal_rub,
            clean_price_pct=self.clean_price_pct,
            accrued_interest_rub=self.accrued_interest_rub,
            quantity=quantity,
            curve=self.curve,
            spread_convention=self.spread_convention,
            spread_kind=self.spread_kind,
        )


@dataclass(frozen=True)
class LocalPosition:
    position_id: str
    instrument_id: str
    quantity: float
    recovery_rate: float

    def __post_init__(self) -> None:
        _text(self.position_id, "position_id")
        _text(self.instrument_id, "instrument_id")
        finite_money(self.quantity, "quantity")
        recovery = finite_money(self.recovery_rate, "recovery_rate")
        if not 0 <= recovery <= 1:
            raise ValueError("recovery_rate must be between 0 and 1")


@dataclass(frozen=True)
class LocalScenario:
    name: str
    curve_basis: CurveRiskBasis
    shock: BondScenario

    def __post_init__(self) -> None:
        _text(self.name, "scenario name")
        if self.curve_basis not in ("continuous_zero", "ois_quote"):
            raise ValueError("unknown scenario curve_basis")


@dataclass(frozen=True)
class PositionRiskOutcome:
    position_id: str
    instrument_id: str
    calculation_id: str | None
    snapshot_content_id: str | None
    error: str | None
    risk: BondRiskResult | None = None
    second_order: BondSecondOrderRisk | None = None
    default: BondDefaultResult | None = None


@dataclass(frozen=True)
class PositionScenarioOutcome:
    position_id: str
    calculation_id: str | None
    error: str | None
    result: LocalBondScenarioResult | None = None


@dataclass(frozen=True)
class Coverage:
    status: CoverageStatus
    expected: int
    completed: int
    failed_position_ids: tuple[str, ...]


def _coverage(expected: tuple[str, ...], completed: set[str]) -> Coverage:
    failed = tuple(value for value in expected if value not in completed)
    status: CoverageStatus = "complete" if not failed else "partial" if completed else "failed"
    return Coverage(status, len(expected), len(completed), failed)


@dataclass(frozen=True)
class ScenarioCoverageResult:
    name: str
    scenario_id: str
    coverage: Coverage
    positions: tuple[PositionScenarioOutcome, ...]
    covered_pnl_rub: float
    total_pnl_rub: float | None


@dataclass(frozen=True)
class LocalRiskRunResult:
    run_id: str
    job_id: str
    public_bundle_id: str
    as_of: datetime
    curve_basis: CurveRiskBasis
    library_version: str
    status: CoverageStatus
    coverage: Coverage
    positions: tuple[PositionRiskOutcome, ...]
    scenarios: tuple[ScenarioCoverageResult, ...]
    covered_pv_rub: float
    total_pv_rub: float | None


def run_local_risk(
    *,
    run_id: str,
    as_of: datetime,
    curve_basis: CurveRiskBasis,
    public_snapshots: tuple[PublicBondSnapshot, ...],
    positions: tuple[LocalPosition, ...],
    scenarios: tuple[LocalScenario, ...],
    include_second_order: bool = True,
    bump_bp: float = 1.0,
) -> LocalRiskRunResult:
    """Compute one immutable batch, retaining every expected position and failure.

    A malformed job (duplicate IDs, invalid units or timestamps) raises. Expected
    per-instrument data/calibration errors become explicit failed rows. Scenario
    failures do not destroy base risk. No partial subtotal is labelled a total.
    """
    _text(run_id, "run_id")
    _aware(as_of)
    if curve_basis not in ("continuous_zero", "ois_quote"):
        raise ValueError("unknown curve_basis")
    if not isinstance(include_second_order, bool):
        raise TypeError("include_second_order must be boolean")
    if finite_money(bump_bp, "bump_bp") <= 0:
        raise ValueError("bump_bp must be positive")
    # Copy even nested calendar sets/node lists supplied by callers.
    public_snapshots, positions, scenarios = deepcopy((public_snapshots, positions, scenarios))
    for names, label in (
        ([s.instrument_id for s in public_snapshots], "instrument"),
        ([p.position_id for p in positions], "position"),
        ([s.name for s in scenarios], "scenario"),
    ):
        if len(set(names)) != len(names):
            raise ValueError(f"duplicate {label} identifiers")
    records = {s.instrument_id: s for s in public_snapshots}
    content_ids = {s.instrument_id: s.content_id for s in public_snapshots}
    bundle_id = canonical_sha256({"schema": "public-fixed-rub-risk.v1", "records": content_ids})
    job_id = canonical_sha256(
        {
            "model": "local-portfolio-risk-v1",
            "library_version": __version__,
            "run_id": run_id,
            "as_of": as_of,
            "curve_basis": curve_basis,
            "public_bundle_id": bundle_id,
            "positions": [asdict(p) for p in positions],
            "scenarios": [asdict(s) for s in scenarios],
            "include_second_order": include_second_order,
            "bump_bp": bump_bp,
        }
    )
    outcomes: list[PositionRiskOutcome] = []
    scenario_rows: list[list[PositionScenarioOutcome]] = [[] for _ in scenarios]
    expected = tuple(p.position_id for p in positions)
    for position in positions:
        content_id = content_ids.get(position.instrument_id)
        calculation_id: str | None = None
        try:
            if position.instrument_id not in records:
                raise LookupError("public instrument snapshot is missing")
            record = records[position.instrument_id]
            prepared = prepare_local_bond_risk(
                record.local_input(
                    run_id=run_id,
                    as_of=as_of,
                    basis=curve_basis,
                    quantity=position.quantity,
                )
            )
            calculation_id = canonical_sha256(
                {
                    "job_id": job_id,
                    "position_id": position.position_id,
                    "adapter_id": prepared.calculation_id,
                    "snapshot_content_id": content_id,
                }
            )
            risk = prepared.engine.risk(bump_bp=bump_bp, difference="central")
            second = prepared.engine.second_order(bump_bp=bump_bp) if include_second_order else None
            default = prepared.engine.jump_to_default(
                outstanding_nominal_rub_per_bond=record.current_nominal_rub,
                recovery_rate=position.recovery_rate,
            )
        except (ValueError, LookupError, ArithmeticError) as exc:
            outcomes.append(
                PositionRiskOutcome(
                    position.position_id,
                    position.instrument_id,
                    calculation_id,
                    content_id,
                    str(exc),
                )
            )
            for rows in scenario_rows:
                rows.append(
                    PositionScenarioOutcome(
                        position.position_id,
                        calculation_id,
                        "base calculation unavailable",
                    )
                )
            continue
        outcomes.append(
            PositionRiskOutcome(
                position.position_id,
                position.instrument_id,
                calculation_id,
                content_id,
                None,
                risk,
                second,
                default,
            )
        )
        for scenario, rows in zip(scenarios, scenario_rows, strict=True):
            try:
                result = prepared.reprice(scenario.shock, curve_basis=scenario.curve_basis)
                rows.append(
                    PositionScenarioOutcome(position.position_id, calculation_id, None, result)
                )
            except (ValueError, LookupError, ArithmeticError) as exc:
                rows.append(PositionScenarioOutcome(position.position_id, calculation_id, str(exc)))
    coverage = _coverage(expected, {p.position_id for p in outcomes if p.error is None})
    covered_pv = finite_money(math.fsum(p.risk.pv_rub for p in outcomes if p.risk), "covered PV")
    scenario_results: list[ScenarioCoverageResult] = []
    for scenario, rows in zip(scenarios, scenario_rows, strict=True):
        scenario_coverage = _coverage(expected, {p.position_id for p in rows if p.error is None})
        subtotal = finite_money(
            math.fsum(p.result.result.total_pnl_rub for p in rows if p.result is not None),
            "covered scenario P&L",
        )
        scenario_results.append(
            ScenarioCoverageResult(
                scenario.name,
                canonical_sha256({"job_id": job_id, "scenario": asdict(scenario)}),
                scenario_coverage,
                tuple(rows),
                subtotal,
                subtotal if scenario_coverage.status == "complete" else None,
            )
        )
    status: CoverageStatus = (
        "complete"
        if coverage.status == "complete"
        and all(s.coverage.status == "complete" for s in scenario_results)
        else "partial"
        if coverage.completed
        else "failed"
    )
    return LocalRiskRunResult(
        run_id,
        job_id,
        bundle_id,
        as_of,
        curve_basis,
        __version__,
        status,
        coverage,
        tuple(outcomes),
        tuple(scenario_results),
        covered_pv,
        covered_pv if coverage.status == "complete" else None,
    )
