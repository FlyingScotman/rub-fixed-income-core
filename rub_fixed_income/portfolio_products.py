"""Derivative products in ``value_portfolio``: row types and valuation adapters (v3).

Each adapter values one position with its product module's engine, on the inputs the
position type names (:mod:`rub_fixed_income.portfolio_instruments`) and the shared
snapshot, and returns an :class:`ProductOutcome`: the result row (signed RUB, scaled by
the position quantity), its desk factor risk, an exact scenario repricer, its
jump-to-default and its model record. Engines, conventions and identities are the
product modules' own; nothing is re-derived here.

**Extension point.** A new product adds:

1. a frozen position type with ``instrument_id``, ``product`` and ``currency``;
2. a frozen row type whose first fields are those of :class:`DerivativeRowHeader`
   (``position_id`` ... ``factor_risk``);
3. an adapter ``(position, instrument, context) -> ProductOutcome`` registered in
   :data:`PRODUCT_ADAPTERS` under the position type;
4. a tuple field for its rows on ``PortfolioRiskResultV3`` with
   ``metadata=OMIT_IF_DEFAULT`` (so existing v3 bytes do not move) and its name in
   :data:`ROW_FIELDS`.

Factor totals, the combined Hessian, JTD totals, coverage and scenarios then include it.

**Carry.** ``horizon_portfolio`` carries a derivative through a second registry,
:data:`HORIZON_ADAPTERS` ``(position, instrument, context, horizon_date, fx_horizon) ->
row``, on the same engines, with its row type in :data:`HORIZON_ROW_FIELDS`; a position
type without one is ``UNSUPPORTED_PRODUCT`` there.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date
from typing import TYPE_CHECKING, Literal, Protocol

from ._curve_views import FxHorizon
from ._money import finite_money
from ._risk_common import ShockedCurveCache
from .canonical import canonical_sha256
from .cds import (
    CDS_RISK_MODEL_VERSION,
    CdsCurveCache,
    CdsCurveRequest,
    CdsHorizonValuation,
    CdsRiskEngine,
    CdsRiskRequest,
    CdsScenario,
    CdsSide,
    cds_factor_risk,
)
from .curve_specs import CurveKind, get_curve_spec
from .dates import BusinessCalendar
from .discount import CurveBootstrapRequest, DiscountCurve
from .domain import (
    AccountingCategory,
    FuturesQuote,
    FxBasis,
    FxRate,
    FxSpot,
    Instrument,
    MarketSnapshot,
    Position,
)
from .errors import (
    CoreRejection,
    ExpiredContract,
    InvalidTerms,
    MissingAssumption,
    MissingBasketPrice,
    MissingFixing,
    MissingMarketData,
    SettledLeg,
    UndeclaredFixingGap,
    UnsupportedProduct,
)
from .factors import FactorRisk, HessianEntry, fx_futures_factor_risk, fx_translated_factor_risk
from .floating import FixingHistory, FixingIndex
from .fx_forwards import (
    FX_FORWARD_RISK_MODEL_VERSION,
    FX_IMPLIED_CURVE_MODEL_VERSION,
    FxForwardHorizonValuation,
    FxForwardLegValuation,
    FxForwardRiskEngine,
    FxForwardRiskRequest,
    FxSwap,
    fx_forward_factor_risk,
)
from .fx_futures import (
    FX_FUTURES_RISK_MODEL_VERSION,
    FxFuturesHorizonValuation,
    FxFuturesRiskEngine,
    FxFuturesRiskRequest,
    FxFuturesScenario,
)
from .key_rate import MODEL_VERSION as KEY_RATE_CURVE_MODEL_VERSION
from .key_rate import KeyRateForwardCurve
from .ofz_futures import (
    OFZ_FUTURES_RISK_MODEL_VERSION,
    DeliverableAnalysis,
    DeliverableBond,
    OfzFuturesHorizonValuation,
    OfzFuturesRiskEngine,
    OfzFuturesRiskRequest,
    OfzFuturesScenario,
    ofz_futures_factor_risk,
)
from .portfolio_instruments import (
    CdsInstrument,
    FxForwardInstrument,
    FxFuturesInstrument,
    OfzFuturesInstrument,
    RateFuturesInstrument,
    RepoInstrument,
    SwapInstrument,
    XccyInstrument,
)
from .rate_futures import (
    RATE_FUTURES_RISK_MODEL_VERSION,
    KeyRateFuturesContract,
    RateFuturesHorizonValuation,
    RateFuturesRiskEngine,
    RateFuturesRiskRequest,
    RateFuturesScenario,
    RuoniaAverageFuturesContract,
    RuoniaIndexFuturesContract,
    rate_futures_factor_risk,
    rate_futures_variation_margin,
)
from .repo import (
    REPO_MODEL_VERSION,
    AmountSource,
    FloatingRepoRate,
    RepoCarry,
    RepoCollateralReference,
    RepoRiskEngine,
    RepoRiskRequest,
    RepoScenario,
    RepoSide,
    repo_factor_risk,
)
from .swaps import (
    SWAP_MODEL_VERSION,
    SwapHorizonValuation,
    SwapRiskEngine,
    SwapRiskRequest,
    SwapScenario,
    SwapSide,
    swap_factor_risk,
)
from .xccy import (
    XCCY_CURVE_MODEL_VERSION,
    XCCY_MODEL_VERSION,
    XccyCurveBootstrapRequest,
    XccyCurveCache,
    XccyCurveRole,
    XccyHorizonValuation,
    XccyRiskEngine,
    XccyRiskRequest,
    XccyScenario,
    xccy_factor_risk,
    xccy_par_rate,
)

if TYPE_CHECKING:
    from .portfolio import _BuiltCurve

__all__ = [
    "HORIZON_ADAPTERS",
    "HORIZON_ROW_FIELDS",
    "PRODUCT_ADAPTERS",
    "ROW_FIELDS",
    "CdsPositionHorizon",
    "CdsPositionValuation",
    "CreditCurveUse",
    "DerivativeHorizonHeader",
    "DerivativeRowHeader",
    "FxForwardPositionHorizon",
    "FxForwardPositionValuation",
    "FxFuturesPositionHorizon",
    "FxFuturesPositionValuation",
    "HessianTotal",
    "JtdTotal",
    "OfzFuturesPositionHorizon",
    "OfzFuturesPositionValuation",
    "PositionModel",
    "ProductContext",
    "ProductOutcome",
    "RateFuturesPositionHorizon",
    "RateFuturesPositionValuation",
    "RepoPositionHorizon",
    "RepoPositionValuation",
    "ScenarioView",
    "SwapPositionHorizon",
    "SwapPositionValuation",
    "XccyPositionHorizon",
    "XccyPositionValuation",
    "adapter_for",
    "hessian_totals",
    "horizon_adapter_for",
    "horizon_spot_date",
    "jtd_totals",
    "scale_factor_risk",
]


# --- rows ----------------------------------------------------------------------------


@dataclass(frozen=True)
class DerivativeRowHeader:
    """The leading fields of every derivative row (documentation of the shared shape).

    Money is signed RUB for the position: the product's own amounts times the position
    quantity, converted at the snapshot rate where the product is not in RUB.
    """

    position_id: str
    instrument_id: str
    book: str
    accounting_category: AccountingCategory
    calculation_id: str
    quantity: float
    pv_rub: float
    factor_risk: FactorRisk


@dataclass(frozen=True)
class SwapPositionValuation:
    """A RUONIA OIS or key-rate swap (``swaps``).

    ``clean_pv_rub = pv_rub - accrued_rub``; ``par_rate`` is the fixed rate that zeroes
    the remaining flows (the quantity does not change it). ``fixing_snapshot_id`` is
    the ``FixingHistory.snapshot_id`` of the fixings read, when any were.
    """

    position_id: str
    instrument_id: str
    book: str
    accounting_category: AccountingCategory
    calculation_id: str
    quantity: float
    pv_rub: float
    factor_risk: FactorRisk
    swap_id: str
    side: SwapSide
    notional_rub: float
    fixed_rate: float
    convention: str
    valuation_date: date
    discount_curve_id: str
    projection_curve_id: str | None
    discount_curve_basis: str
    projection_curve_basis: str
    fixing_id: str | None
    fixing_snapshot_id: str | None
    clean_pv_rub: float
    accrued_rub: float
    fixed_leg_pv_rub: float
    float_leg_pv_rub: float
    par_rate: float
    annuity: float


@dataclass(frozen=True)
class FxForwardPositionValuation:
    """An FX forward or FX swap (``fx_forwards``); legs are scaled by the quantity."""

    position_id: str
    instrument_id: str
    book: str
    accounting_category: AccountingCategory
    calculation_id: str
    quantity: float
    pv_rub: float
    factor_risk: FactorRisk
    trade_id: str
    kind: Literal["forward", "swap"]
    currency: str
    quote_units: float
    spot_rate: float
    spot_value_date: date
    settlement_calendar_id: str
    basis_bp: float
    rub_curve_id: str
    foreign_curve_id: str
    fx_delta_foreign: float
    legs: tuple[FxForwardLegValuation, ...]


@dataclass(frozen=True)
class XccyPositionValuation:
    """A cross-currency swap (``xccy``).

    ``curve_ids`` maps each curve role to its curve ID. ``foreign_leg_par_rate`` is the
    foreign leg's fixed rate (or spread, if floating) that zeroes the PV; ``None`` when
    no unknown foreign coupon remains.
    """

    position_id: str
    instrument_id: str
    book: str
    accounting_category: AccountingCategory
    calculation_id: str
    quantity: float
    pv_rub: float
    factor_risk: FactorRisk
    trade_id: str
    currency: str
    spot_rate: float
    spot_value_date: date
    basis_bp: float
    fx_today: float
    curve_ids: tuple[tuple[str, str], ...]
    rub_fixing_id: str | None
    foreign_fixing_id: str | None
    rub_leg_pv_rub: float
    foreign_leg_pv: float
    foreign_leg_pv_rub: float
    foreign_leg_par_rate: float | None


@dataclass(frozen=True)
class CdsPositionValuation:
    """A single-name CDS (``cds``); ``_rub`` amounts at ``rub_per_unit`` (1 for RUB).

    ``pv`` and ``clean_pv`` are in the contract currency; ``points_upfront`` and
    ``par_spread`` do not depend on the side or the quantity. ``jtd_pnl_rub`` is the
    immediate default at the realised recovery ``jtd_recovery_rate`` (the position's
    recovery when it states one, otherwise the credit curve's), signed like bond JTD.
    """

    position_id: str
    instrument_id: str
    book: str
    accounting_category: AccountingCategory
    calculation_id: str
    quantity: float
    pv_rub: float
    factor_risk: FactorRisk
    contract_id: str
    reference_entity: str
    currency: str
    side: CdsSide
    notional: float
    coupon: float
    recovery_rate: float
    rub_per_unit: float
    discount_curve_id: str
    step_in_date: date
    cash_settlement_date: date
    pv: float
    clean_pv: float
    clean_pv_rub: float
    accrued_premium_rub: float
    points_upfront: float
    par_spread: float
    risky_annuity: float
    recovery01_rub: float
    jtd_recovery_rate: float
    jtd_pnl_rub: float


@dataclass(frozen=True)
class FxFuturesPositionValuation:
    """An FX future (``fx_futures``); ``quantity`` is signed contracts.

    Prices are in the contract's price units. ``variation_margin_rub`` is the margin
    due at the next clearing, ``contracts * multiplier * (futures_price -
    margined_price)``, not discounted; it is the position's ``pv_rub``.
    """

    position_id: str
    instrument_id: str
    book: str
    accounting_category: AccountingCategory
    calculation_id: str
    quantity: float
    pv_rub: float
    factor_risk: FactorRisk
    contract_id: str
    currency: str
    delivery_date: date
    multiplier: float
    futures_price: float
    margined_price: float
    theoretical_price: float
    basis_bp: float
    spot_rate: float
    spot_value_date: date
    rub_curve_id: str
    foreign_curve_id: str
    notional_rub: float
    variation_margin_rub: float
    fx_delta_foreign: float


@dataclass(frozen=True)
class RateFuturesPositionValuation:
    """A RUON, RUONIA index or KEYRATE future (``rate_futures``); ``quantity`` is contracts.

    ``futures_price`` is the model price at the calibrated basis (the snapshot price,
    or for a fully fixed RUON contract the price its fixings determine);
    ``variation_margin_rub`` is the margin from ``margined_price`` to the snapshot price
    (``rate_futures_variation_margin``, RUON rounding included), the position's
    ``pv_rub``. ``index_anchor_date`` is the RUONIA index value the index contract grows.
    """

    position_id: str
    instrument_id: str
    book: str
    accounting_category: AccountingCategory
    calculation_id: str
    quantity: float
    pv_rub: float
    factor_risk: FactorRisk
    contract_id: str
    kind: Literal["ruonia_average", "ruonia_index", "key_rate"]
    last_trading_day: date
    margin_per_point_rub: float
    futures_price: float
    margined_price: float
    theoretical_price: float
    basis_bp: float
    expected_rate: float
    fixed_days: int
    projected_days: int
    projection_curve_id: str
    fixing_id: str | None
    fixing_snapshot_id: str | None
    index_anchor_date: date | None
    variation_margin_rub: float


@dataclass(frozen=True)
class OfzFuturesPositionValuation:
    """A deliverable OFZ basket future (``ofz_futures``); ``quantity`` is contracts.

    ``basket`` is the per-bond analysis at the base market (forward, implied repo, gross
    and net basis, the cheapest bond), per bond and not scaled. The delivery options are
    not valued (``has_optionality`` in the position model).
    """

    position_id: str
    instrument_id: str
    book: str
    accounting_category: AccountingCategory
    calculation_id: str
    quantity: float
    pv_rub: float
    factor_risk: FactorRisk
    contract_id: str
    last_trading_day: date
    delivery_date: date
    spot_settlement_date: date
    futures_price: float
    margined_price: float
    theoretical_price: float
    basis_bp: float
    cheapest_to_deliver: str
    cheapest_by_implied_repo: str
    bond_curve_id: str
    funding_curve_id: str
    variation_margin_rub: float
    basket: tuple[DeliverableAnalysis, ...]


@dataclass(frozen=True)
class RepoPositionValuation:
    """A repo or reverse-repo leg (``repo``); cash only.

    ``pv_rub`` and ``factor_risk`` are signed for the position (times its quantity).
    The contract amounts (``start_amount_rub`` S1, ``end_amount_rub`` S2 expected,
    ``accrued_interest_rub``, ``current_repurchase_amount_rub``) are per trade, as the
    engine reports them. ``collateral`` is the reference (market value, exposure,
    coverage); it is not in PV or risk.
    """

    position_id: str
    instrument_id: str
    book: str
    accounting_category: AccountingCategory
    calculation_id: str
    quantity: float
    pv_rub: float
    factor_risk: FactorRisk
    trade_id: str
    side: RepoSide
    start_date: date
    end_date: date
    end_date_assumed: bool
    discount_curve_id: str
    projection_curve_id: str | None
    fixing_id: str | None
    fixing_snapshot_id: str | None
    start_amount_rub: float
    end_amount_rub: float
    end_amount_source: AmountSource
    known_interest_rub: float
    accrued_interest_rub: float
    current_repurchase_amount_rub: float
    collateral: RepoCollateralReference | None


ProductRow = (
    SwapPositionValuation
    | FxForwardPositionValuation
    | XccyPositionValuation
    | CdsPositionValuation
    | FxFuturesPositionValuation
    | RateFuturesPositionValuation
    | OfzFuturesPositionValuation
    | RepoPositionValuation
)

ROW_FIELDS: dict[type, str] = {
    SwapPositionValuation: "swaps",
    FxForwardPositionValuation: "fx_forwards",
    XccyPositionValuation: "xccy_swaps",
    CdsPositionValuation: "cds",
    FxFuturesPositionValuation: "fx_futures",
    RateFuturesPositionValuation: "rate_futures",
    OfzFuturesPositionValuation: "ofz_futures",
    RepoPositionValuation: "repos",
}
"""The ``PortfolioRiskResultV3`` field that holds each row type."""


@dataclass(frozen=True)
class PositionModel:
    """How one computed position was valued (BACKLOG #83).

    ``model_version`` is the engine's; ``flags`` are sorted tokens from a fixed
    vocabulary (``docs/CONVENTIONS.md``, "Portfolio result v3"), e.g. ``two_curve``,
    ``has_optionality``, ``fx_translated``, ``quote_curve``.
    """

    position_id: str
    instrument_id: str
    product: str
    model_version: str
    flags: tuple[str, ...]


@dataclass(frozen=True)
class HessianTotal:
    """One entry of the book's sparse Hessian, RUB per unit squared; ``factor_i <= factor_j``."""

    factor_i: str
    factor_j: str
    value_rub: float


@dataclass(frozen=True)
class JtdTotal:
    """Jump-to-default of one entity (bond ``issuer_id`` or CDS reference entity).

    ``entity`` is ``None`` for bonds without an ``issuer_id``. Only positions with a
    JTD count (bonds need a recovery on the position).
    """

    entity: str | None
    jtd_pnl_rub: float
    positions: int


@dataclass(frozen=True)
class CreditCurveUse:
    """A credit curve that valued CDS positions."""

    reference_entity: str
    currency: str
    recovery_rate: float
    quotes: int
    convention: str
    evidence_id: str | None


# --- adapter plumbing ---------------------------------------------------------------


class ScenarioView(Protocol):
    """What an adapter reads from a portfolio scenario."""

    def curve_shift(self, curve_id: str, *, projection: bool = False) -> tuple[float, ...]: ...

    def fx_move_pct(self, currency: str) -> float: ...

    @property
    def spread_shift_bp(self) -> float: ...


@dataclass
class ProductContext:
    """Job-wide inputs and memo tables shared by every adapter call (internal state)."""

    snapshot: MarketSnapshot
    job_id: str
    bump_bp: float
    include_second_order: bool
    curve_cache: ShockedCurveCache
    bond_curve: Callable[[str], _BuiltCurve]
    projection: Callable[[str, FixingIndex], _BuiltCurve]
    calendar: Callable[[str], BusinessCalendar]
    used_fx: dict[str, FxRate]
    instruments: Mapping[str, Instrument] = field(default_factory=dict)
    xccy_cache: XccyCurveCache = field(default_factory=XccyCurveCache)
    cds_cache: CdsCurveCache = field(default_factory=CdsCurveCache)
    used_fx_spots: dict[str, FxSpot] = field(default_factory=dict)
    used_curves: dict[str, tuple[str, str]] = field(default_factory=dict)
    used_credit: dict[tuple[str, str], CreditCurveUse] = field(default_factory=dict)
    credit_requests: dict[tuple[str, str], CdsCurveRequest] = field(default_factory=dict)
    engines: dict[str, object] = field(default_factory=dict)

    @property
    def business_date(self) -> date:
        return self.snapshot.business_date

    def discount_curve(self, curve_id: str, currency: str) -> DiscountCurve | CurveBootstrapRequest:
        """A nominal discount curve of ``currency`` dated the business date."""
        built = self.bond_curve(curve_id)
        if built.real:
            raise InvalidTerms(f"curve {curve_id} is a real curve; it discounts only linkers")
        if built.currency != currency:
            raise InvalidTerms(f"curve {curve_id} is a {built.currency} curve, not {currency}")
        if built.curve.curve_date != self.business_date:
            raise InvalidTerms(f"curve {curve_id} is not dated the snapshot business date")
        return built.curve

    def foreign_fx_curve(self, curve_id: str, currency: str) -> DiscountCurve:
        """The foreign curve of an FX product: discount factors of ``currency``.

        Caller-built factors (``<ccy>_discount_factors_v1``), a curve implied from FX
        swaps (``fx_forwards.implied_foreign_curve``, same spec) or a built XCCY curve
        (``cny_ruonia_xccy_v1``); dated the business date.
        """
        entry = self.snapshot.curve(curve_id)
        spec = get_curve_spec(entry.spec_id)
        source = entry.curve
        if spec.currency != currency:
            raise InvalidTerms(f"curve {curve_id} is a {spec.currency} curve, not {currency}")
        models = {
            CurveKind.DISCOUNT_FACTORS: (spec.model_version, FX_IMPLIED_CURVE_MODEL_VERSION),
            CurveKind.XCCY_BOOTSTRAP: (XCCY_CURVE_MODEL_VERSION,),
        }.get(spec.kind)
        if models is None:
            raise InvalidTerms(f"curve {curve_id} ({spec.spec_id}) is not a foreign discount curve")
        if isinstance(source, XccyCurveBootstrapRequest):
            raise InvalidTerms(
                f"curve {curve_id} holds XCCY quotes; only cross-currency swaps rebuild it"
            )
        if not (
            isinstance(source, DiscountCurve)
            and source.model_version in models
            and source.convention.time_day_count is spec.time_day_count
        ):
            raise InvalidTerms(f"curve {curve_id} does not match spec {spec.spec_id}")
        if source.curve_date != self.business_date:
            raise InvalidTerms(f"curve {curve_id} is not dated the snapshot business date")
        self.used_curves[curve_id] = (spec.spec_id, source.model_version)
        return source

    def key_rate_curve(self, curve_id: str) -> KeyRateForwardCurve:
        """A key-rate swap projection curve (``rub_key_rate_swap_v1``) dated the business date.

        A curve calibrated on TOM-dated quotes is re-dated by the backend with
        ``key_rate.rebase_key_rate_curve``; core does not move a curve in time here.
        """
        entry = self.snapshot.curve(curve_id)
        spec = get_curve_spec(entry.spec_id)
        source = entry.curve
        if spec.kind is not CurveKind.KEY_RATE_SWAP:
            raise InvalidTerms(f"curve {curve_id} ({spec.spec_id}) cannot project a key-rate swap")
        if not (
            isinstance(source, KeyRateForwardCurve)
            and source.model_version == KEY_RATE_CURVE_MODEL_VERSION
        ):
            raise InvalidTerms(f"curve {curve_id} does not match spec {spec.spec_id}")
        if source.curve_date != self.business_date:
            raise InvalidTerms(
                f"key-rate curve {curve_id} is dated {source.curve_date.isoformat()}, not the "
                "snapshot business date; re-date a TOM calibration with rebase_key_rate_curve"
            )
        self.used_curves[curve_id] = (spec.spec_id, source.model_version)
        return source

    def fx_spot(self, currency: str) -> tuple[FxSpot, BusinessCalendar]:
        spot = self.snapshot.fx_spot(currency)
        calendar = self.calendar(spot.calendar_id)
        if not calendar.is_business_day(spot.value_date):
            raise InvalidTerms(
                f"FX spot value date {spot.value_date.isoformat()} is not a business day of "
                f"settlement calendar {spot.calendar_id}"
            )
        self.used_fx_spots[currency] = spot
        return spot, calendar

    def fx_rate(self, currency: str) -> float:
        rate = self.snapshot.fx_rate(currency)
        if currency != "RUB":
            self.used_fx[currency] = FxRate(currency, rate)
        return rate

    def credit_request(self, entity: str, currency: str) -> CdsCurveRequest:
        key = (entity, currency)
        request = self.credit_requests.get(key)
        if request is None:
            entry = self.snapshot.credit_curve(entity, currency)
            request = CdsCurveRequest(
                self.business_date, entry.quotes, entry.recovery_rate, entry.convention
            )
            self.credit_requests[key] = request
            self.used_credit[key] = CreditCurveUse(
                entity,
                currency,
                entry.recovery_rate,
                len(entry.quotes),
                entry.convention.identifier,
                entry.evidence_id,
            )
        return request

    def shared_engine(self, instrument_id: str, build: Callable[[], object]) -> object:
        """One engine per instrument and job (futures positions share it); a rejection
        is kept and raised again for every position on the instrument."""
        if instrument_id not in self.engines:
            try:
                self.engines[instrument_id] = build()
            except CoreRejection as exc:
                self.engines[instrument_id] = exc
        engine = self.engines[instrument_id]
        if isinstance(engine, CoreRejection):
            raise engine
        return engine

    def calculation_id(self, position: Position, model: str, base_id: str) -> str:
        return canonical_sha256(
            {
                "job_id": self.job_id,
                "position_id": position.position_id,
                "model": model,
                "base_id": base_id,
            }
        )


@dataclass(frozen=True)
class ProductOutcome:
    """One valued derivative position."""

    row: ProductRow
    reprice: Callable[[ScenarioView], float]
    model: PositionModel
    jtd: tuple[str | None, float] | None = None


Adapter = Callable[[Position, object, ProductContext], ProductOutcome]


def scale_factor_risk(risk: FactorRisk, factor: float) -> FactorRisk:
    """``risk`` times ``factor`` (first order and Hessian); exact zeros stay omitted."""
    if factor == 1.0:
        return risk
    hessian = tuple(
        HessianEntry(e.i, e.j, e.value * factor) for e in risk.hessian if e.value * factor != 0.0
    )
    return FactorRisk(
        risk.factors,
        tuple(v * factor for v in risk.first_order),
        hessian,
        risk.difference,
        risk.bump_bp,
        risk.has_second_order,
    )


def _check_position(position: Position, currency: str) -> None:
    if position.currency != currency:
        raise InvalidTerms(f"position currency {position.currency} differs from {currency}")


def _model(
    position: Position, instrument_id: str, product: str, version: str, *flags: str
) -> PositionModel:
    return PositionModel(
        position.position_id, instrument_id, product, version, tuple(sorted(flags))
    )


def _money(value: float, label: str) -> float:
    return finite_money(value, label)


# --- swaps --------------------------------------------------------------------------


def _swap_engine(
    position: Position, instrument: SwapInstrument, ctx: ProductContext
) -> tuple[SwapRiskEngine, FixingHistory | None]:
    """The swap's engine on the job's curves (shared by valuation and carry)."""
    _check_position(position, instrument.currency)
    swap = instrument.swap
    calendar = ctx.calendar(instrument.calendar_id)
    discount_id = instrument.discount_curve_id
    discount = ctx.discount_curve(discount_id, "RUB")
    projection_id = instrument.projection_curve_id
    projection: DiscountCurve | CurveBootstrapRequest | KeyRateForwardCurve | None = None
    if projection_id is not None:
        if swap.convention.index == "key_rate":
            projection = ctx.key_rate_curve(projection_id)
        else:
            projection = ctx.projection(projection_id, "ruonia").curve
    fixings = (
        None
        if instrument.fixing_id is None
        else ctx.snapshot.point_in_time_fixing(instrument.fixing_id)
    )
    engine = SwapRiskEngine(
        SwapRiskRequest(swap, calendar, discount, projection, fixings), ctx.curve_cache
    )
    return engine, fixings


def value_swap(position: Position, instrument: object, ctx: ProductContext) -> ProductOutcome:
    assert isinstance(instrument, SwapInstrument)
    engine, fixings = _swap_engine(position, instrument, ctx)
    swap = instrument.swap
    discount_id = instrument.discount_curve_id
    projection_id = instrument.projection_curve_id
    valuation = engine.valuation()
    risk = engine.risk(bump_bp=ctx.bump_bp, difference="central")
    second = engine.second_order(bump_bp=ctx.bump_bp) if ctx.include_second_order else None
    q = position.quantity
    factor_risk = scale_factor_risk(
        swap_factor_risk(
            risk,
            discount_curve_id=discount_id,
            projection_curve_id=projection_id,
            second_order=second,
        ),
        q,
    )
    row = SwapPositionValuation(
        position.position_id,
        instrument.instrument_id,
        position.book,
        position.accounting_category,
        ctx.calculation_id(position, SWAP_MODEL_VERSION, engine.base_id),
        q,
        _money(valuation.pv_rub * q, "swap PV"),
        factor_risk,
        swap.swap_id,
        swap.side,
        _money(swap.notional * q, "swap notional"),
        swap.fixed_rate,
        swap.convention.identifier,
        valuation.valuation_date,
        discount_id,
        projection_id,
        valuation.discount_curve_basis,
        valuation.projection_curve_basis,
        instrument.fixing_id,
        None if fixings is None else fixings.snapshot_id,
        _money(valuation.clean_pv_rub * q, "swap clean PV"),
        _money(valuation.accrued_rub * q, "swap accrued"),
        _money(valuation.fixed_leg_pv_rub * q, "fixed leg PV"),
        _money(valuation.float_leg_pv_rub * q, "float leg PV"),
        valuation.par_rate,
        valuation.annuity,
    )

    def reprice(scenario: ScenarioView) -> float:
        shock = SwapScenario(
            scenario.curve_shift(discount_id),
            (
                scenario.curve_shift(projection_id, projection=True)
                if projection_id is not None
                else SwapScenario().projection_shifts_bp
            ),
        )
        return _money(engine.reprice(shock).total_pnl_rub * q, "swap scenario P&L")

    flags = ["two_curve" if projection_id is not None else "single_curve"]
    if swap.convention.index == "key_rate":
        flags.append("key_rate_projection")
    if fixings is not None:
        flags.append("known_fixings")
    model = _model(
        position, instrument.instrument_id, instrument.product.value, SWAP_MODEL_VERSION, *flags
    )
    return ProductOutcome(row, reprice, model)


# --- FX forwards and swaps ------------------------------------------------------------


def _fx_forward_engine(
    position: Position, instrument: FxForwardInstrument, ctx: ProductContext
) -> tuple[FxForwardRiskEngine, FxSpot, BusinessCalendar, FxBasis]:
    """The forward's engine on the job's curves and spot (shared by valuation and carry)."""
    trade = instrument.trade
    currency = trade.currency
    _check_position(position, currency)
    spot, settlement = ctx.fx_spot(currency)
    for leg in trade.legs:
        if leg.value_date < ctx.business_date or leg.settlement_date < ctx.business_date:
            raise SettledLeg(
                f"{trade.trade_id}: a leg settled on {leg.value_date.isoformat()}; value only "
                "outstanding legs (FxSwap.far_leg_forward)"
            )
        if not settlement.is_business_day(leg.value_date):
            raise InvalidTerms(
                f"{trade.trade_id}: value date {leg.value_date.isoformat()} is not a business "
                f"day of settlement calendar {spot.calendar_id}"
            )
    rub = ctx.discount_curve(instrument.rub_curve_id, "RUB")
    foreign = ctx.foreign_fx_curve(instrument.foreign_curve_id, currency)
    basis = ctx.snapshot.fx_basis(currency, instrument.foreign_curve_id)
    engine = FxForwardRiskEngine(
        FxForwardRiskRequest(
            trade,
            spot.rub_per_unit * trade.quote_units,
            spot.value_date,
            rub,
            foreign,
            basis.basis_bp,
            basis.day_count,
        ),
        ctx.curve_cache,
    )
    return engine, spot, settlement, basis


def value_fx_forward(position: Position, instrument: object, ctx: ProductContext) -> ProductOutcome:
    assert isinstance(instrument, FxForwardInstrument)
    engine, spot, _, basis = _fx_forward_engine(position, instrument, ctx)
    trade = instrument.trade
    currency = trade.currency
    risk = engine.risk(bump_bp=ctx.bump_bp, difference="central")
    second = engine.second_order(bump_bp=ctx.bump_bp) if ctx.include_second_order else None
    q = position.quantity
    factor_risk = scale_factor_risk(
        fx_forward_factor_risk(risk, instrument.rub_curve_id, instrument.foreign_curve_id, second),
        q,
    )
    legs = tuple(
        FxForwardLegValuation(
            leg.value_date,
            leg.payment_date,
            leg.notional_foreign * q,
            leg.contract_rate,
            leg.forward_rate,
            leg.forward_points,
            leg.rub_discount_factor,
            _money(leg.pv_rub * q, "leg PV"),
        )
        for leg in risk.legs
    )
    row = FxForwardPositionValuation(
        position.position_id,
        instrument.instrument_id,
        position.book,
        position.accounting_category,
        ctx.calculation_id(position, FX_FORWARD_RISK_MODEL_VERSION, engine.base_id),
        q,
        _money(engine.base_pv_rub * q, "FX forward PV"),
        factor_risk,
        trade.trade_id,
        "swap" if isinstance(trade, FxSwap) else "forward",
        currency,
        trade.quote_units,
        risk.spot_rate,
        spot.value_date,
        spot.calendar_id,
        basis.basis_bp,
        instrument.rub_curve_id,
        instrument.foreign_curve_id,
        _money(risk.fx_delta_foreign * q, "FX delta"),
        legs,
    )

    def reprice(scenario: ScenarioView) -> float:
        shock = FxFuturesScenario(
            scenario.curve_shift(instrument.rub_curve_id),
            scenario.curve_shift(instrument.foreign_curve_id),
            0.0,
            scenario.fx_move_pct(currency),
        )
        return _money(engine.reprice(shock).total_pnl_rub * q, "FX forward scenario P&L")

    model = _model(
        position,
        instrument.instrument_id,
        instrument.product.value,
        FX_FORWARD_RISK_MODEL_VERSION,
        "covered_parity",
    )
    return ProductOutcome(row, reprice, model)


# --- cross-currency swaps -----------------------------------------------------------


def _xccy_foreign_discount(
    ctx: ProductContext, curve_id: str, currency: str, spot: FxSpot
) -> DiscountCurve | XccyCurveBootstrapRequest:
    entry = ctx.snapshot.curve(curve_id)
    source = entry.curve
    if not isinstance(source, XccyCurveBootstrapRequest):
        return ctx.foreign_fx_curve(curve_id, currency)
    spec = get_curve_spec(entry.spec_id)
    if spec.kind is not CurveKind.XCCY_BOOTSTRAP or spec.currency != currency:
        raise InvalidTerms(f"curve {curve_id} does not match spec {spec.spec_id}")
    if source.currency != currency:
        raise InvalidTerms(f"curve {curve_id} quotes a {source.currency} curve, not {currency}")
    if source.trade_date != ctx.business_date:
        raise InvalidTerms(f"XCCY quotes {curve_id} are not dated the snapshot business date")
    if source.spot_rate != spot.rub_per_unit or source.spot_value_date != spot.value_date:
        raise InvalidTerms(f"XCCY quotes {curve_id} carry a spot other than the snapshot FX spot")
    if source.convention.spec_id not in (None, spec.spec_id):
        raise InvalidTerms(f"curve {curve_id} quotes are of spec {source.convention.spec_id}")
    ctx.used_curves[curve_id] = (spec.spec_id, spec.model_version)
    return source


def _xccy_engine(
    position: Position, instrument: XccyInstrument, ctx: ProductContext
) -> tuple[XccyRiskEngine, FxSpot, BusinessCalendar]:
    """The swap's engine on the job's curves and spot (shared by valuation and carry)."""
    swap = instrument.swap
    currency = swap.currency
    _check_position(position, currency)
    spot, settlement = ctx.fx_spot(currency)
    rub_d = ctx.discount_curve(instrument.rub_discount_curve_id, "RUB")
    rub_p = (
        None
        if instrument.rub_projection_curve_id is None
        else ctx.projection(instrument.rub_projection_curve_id, "ruonia").curve
    )
    foreign_d = _xccy_foreign_discount(ctx, instrument.foreign_discount_curve_id, currency, spot)
    foreign_p = (
        None
        if instrument.foreign_projection_curve_id is None
        else ctx.discount_curve(instrument.foreign_projection_curve_id, currency)
    )
    rub_fixings = (
        None
        if instrument.rub_fixing_id is None
        else ctx.snapshot.point_in_time_fixing(instrument.rub_fixing_id)
    )
    foreign_fixings = (
        None
        if instrument.foreign_fixing_id is None
        else ctx.snapshot.index_fixing(instrument.foreign_fixing_id)
    )
    basis = ctx.snapshot.fx_basis(currency, instrument.foreign_discount_curve_id)
    request = XccyRiskRequest(
        swap,
        spot.rub_per_unit,
        spot.value_date,
        rub_d,
        foreign_d,
        basis.basis_bp,
        rub_projection_curve=rub_p,
        foreign_projection_curve=foreign_p,
        rub_fixings=rub_fixings,
        foreign_fixings=foreign_fixings,
        basis_day_count=basis.day_count,
    )
    return XccyRiskEngine(request, ctx.curve_cache, ctx.xccy_cache), spot, settlement


def value_xccy(position: Position, instrument: object, ctx: ProductContext) -> ProductOutcome:
    assert isinstance(instrument, XccyInstrument)
    engine, spot, _ = _xccy_engine(position, instrument, ctx)
    request = engine.request
    swap = instrument.swap
    currency = swap.currency
    rub_p = request.rub_projection_curve
    foreign_d = request.foreign_discount_curve
    foreign_p = request.foreign_projection_curve
    rub_fixings = request.rub_fixings
    foreign_fixings = request.foreign_fixings
    risk = engine.risk(bump_bp=ctx.bump_bp, difference="central")
    second = engine.second_order(bump_bp=ctx.bump_bp) if ctx.include_second_order else None
    ids: dict[XccyCurveRole, str | None] = {
        "rub_discount": instrument.rub_discount_curve_id,
        "rub_projection": instrument.rub_projection_curve_id,
        "foreign_discount": instrument.foreign_discount_curve_id,
        "foreign_projection": instrument.foreign_projection_curve_id,
    }
    curve_ids: dict[str, str] = {role: cid for role, cid in ids.items() if cid is not None}
    q = position.quantity
    factor_risk = scale_factor_risk(xccy_factor_risk(risk, curve_ids, second), q)
    try:
        par: float | None = xccy_par_rate(
            request, leg="foreign", curve_cache=ctx.curve_cache, xccy_cache=ctx.xccy_cache
        )
    except InvalidTerms:
        par = None
    row = XccyPositionValuation(
        position.position_id,
        instrument.instrument_id,
        position.book,
        position.accounting_category,
        ctx.calculation_id(position, XCCY_MODEL_VERSION, engine.base_id),
        q,
        _money(engine.base_pv_rub * q, "XCCY PV"),
        factor_risk,
        swap.trade_id,
        currency,
        spot.rub_per_unit,
        spot.value_date,
        request.basis_bp,
        risk.fx_today,
        tuple((role, curve_ids[role]) for role in engine.roles),
        instrument.rub_fixing_id,
        instrument.foreign_fixing_id,
        _money(risk.rub_leg_pv_rub * q, "RUB leg PV"),
        _money(risk.foreign_leg_pv * q, "foreign leg PV"),
        _money(risk.foreign_leg_pv_rub * q, "foreign leg RUB PV"),
        par,
    )

    def reprice(scenario: ScenarioView) -> float:
        rub_proj = instrument.rub_projection_curve_id
        for_proj = instrument.foreign_projection_curve_id
        shock = XccyScenario(
            scenario.curve_shift(instrument.rub_discount_curve_id),
            scenario.curve_shift(instrument.foreign_discount_curve_id),
            0.0,
            scenario.fx_move_pct(currency),
            None if rub_proj is None else scenario.curve_shift(rub_proj, projection=True),
            None if for_proj is None else scenario.curve_shift(for_proj, projection=True),
        )
        return _money(engine.reprice(shock).total_pnl_rub * q, "XCCY scenario P&L")

    flags = ["covered_parity"]
    if isinstance(foreign_d, XccyCurveBootstrapRequest):
        flags.append("quote_curve")
    if rub_p is not None or foreign_p is not None:
        flags.append("two_curve")
    if rub_fixings is not None or foreign_fixings is not None:
        flags.append("known_fixings")
    model = _model(
        position, instrument.instrument_id, instrument.product.value, XCCY_MODEL_VERSION, *flags
    )
    return ProductOutcome(row, reprice, model)


# --- CDS ----------------------------------------------------------------------------


def _cds_engine(
    position: Position, instrument: CdsInstrument, ctx: ProductContext
) -> tuple[CdsRiskEngine, CdsCurveRequest, float]:
    """The contract's engine, its credit curve and the FX rate (valuation and carry)."""
    contract = instrument.contract
    currency = contract.currency
    _check_position(position, currency)
    discount = ctx.discount_curve(instrument.discount_curve_id, currency)
    fx = ctx.fx_rate(currency)
    credit = ctx.credit_request(contract.reference_entity, currency)
    if credit.convention.hazard_day_count != contract.convention.hazard_day_count:
        raise InvalidTerms("contract and credit curve hazard day counts differ")
    engine = CdsRiskEngine(
        CdsRiskRequest(contract, ctx.business_date, discount, credit),
        ctx.curve_cache,
        ctx.cds_cache,
    )
    return engine, credit, fx


def value_cds_position(
    position: Position, instrument: object, ctx: ProductContext
) -> ProductOutcome:
    assert isinstance(instrument, CdsInstrument)
    engine, credit, fx = _cds_engine(position, instrument, ctx)
    contract = instrument.contract
    currency = contract.currency
    risk = engine.risk(bump_bp=ctx.bump_bp, difference="central")
    second = engine.second_order(bump_bp=ctx.bump_bp) if ctx.include_second_order else None
    q = position.quantity
    scale = q * fx
    factor_risk = scale_factor_risk(
        cds_factor_risk(risk, instrument.discount_curve_id, second), scale
    )
    valuation = risk.valuation
    pv_ccy = _money(valuation.pv * q, "CDS PV")
    if currency != "RUB":
        factor_risk = fx_translated_factor_risk(factor_risk, currency, pv_ccy * fx / 100.0)
    recovery = (
        position.recovery_rate if position.recovery_rate is not None else credit.recovery_rate
    )
    default = engine.jump_to_default(recovery_rate=recovery)
    jtd = _money(default.jtd_pnl * scale, "CDS JTD")
    row = CdsPositionValuation(
        position.position_id,
        instrument.instrument_id,
        position.book,
        position.accounting_category,
        ctx.calculation_id(position, CDS_RISK_MODEL_VERSION, engine.base_id),
        q,
        _money(valuation.pv * scale, "CDS PV RUB"),
        factor_risk,
        contract.contract_id,
        contract.reference_entity,
        currency,
        contract.side,
        _money(contract.notional * q, "CDS notional"),
        contract.coupon,
        credit.recovery_rate,
        fx,
        instrument.discount_curve_id,
        valuation.step_in_date,
        valuation.cash_settlement_date,
        pv_ccy,
        _money(valuation.clean_pv * q, "CDS clean PV"),
        _money(valuation.clean_pv * scale, "CDS clean PV RUB"),
        _money(valuation.accrued_premium * scale, "CDS accrued premium"),
        valuation.points_upfront,
        valuation.par_spread,
        valuation.risky_annuity,
        _money(risk.recovery01 * scale, "recovery01"),
        recovery,
        jtd,
    )

    def reprice(scenario: ScenarioView) -> float:
        shock = CdsScenario(
            scenario.curve_shift(instrument.discount_curve_id), scenario.spread_shift_bp
        )
        result = engine.reprice(shock)
        move = scenario.fx_move_pct(currency) if currency != "RUB" else 0.0
        if not move:
            return _money(result.total_pnl * scale, "CDS scenario P&L")
        return _money(
            (result.shocked_pv * (1.0 + move / 100.0) - result.base_pv) * scale,
            "CDS scenario P&L",
        )

    flags = ["hazard_bootstrap"]
    if currency != "RUB":
        flags.append("fx_translated")
    model = _model(
        position, instrument.instrument_id, instrument.product.value, CDS_RISK_MODEL_VERSION, *flags
    )
    return ProductOutcome(row, reprice, model, (contract.reference_entity, jtd))


# --- FX futures ---------------------------------------------------------------------


def _fx_futures_engine(
    position: Position, instrument: FxFuturesInstrument, ctx: ProductContext
) -> tuple[FxFuturesRiskEngine, FxSpot, BusinessCalendar, FuturesQuote]:
    """The position's engine on the job's curves, spot and price (valuation and carry)."""
    contract = instrument.contract
    currency = contract.currency
    _check_position(position, currency)
    if contract.delivery_date <= ctx.business_date:
        raise ExpiredContract(
            f"futures {contract.contract_id} delivered on {contract.delivery_date.isoformat()}"
        )
    spot, settlement = ctx.fx_spot(currency)
    if contract.delivery_date <= spot.value_date:
        raise InvalidTerms("the futures delivery date must follow the spot value date")
    quote = ctx.snapshot.futures_quote(contract.contract_id)
    rub = ctx.discount_curve(instrument.rub_curve_id, "RUB")
    foreign = ctx.foreign_fx_curve(instrument.foreign_curve_id, currency)
    q = position.quantity
    engine = FxFuturesRiskEngine(
        FxFuturesRiskRequest(
            contract,
            spot.rub_per_unit * contract.price_quote_units,
            spot.value_date,
            rub,
            foreign,
            futures_price=quote.price,
            basis_day_count=instrument.basis_day_count,
            quantity=q,
        ),
        ctx.curve_cache,
    )
    return engine, spot, settlement, quote


def value_fx_future(position: Position, instrument: object, ctx: ProductContext) -> ProductOutcome:
    assert isinstance(instrument, FxFuturesInstrument)
    engine, spot, _, quote = _fx_futures_engine(position, instrument, ctx)
    contract = instrument.contract
    currency = contract.currency
    q = position.quantity
    risk = engine.risk(bump_bp=ctx.bump_bp, difference="central")
    second = engine.second_order(bump_bp=ctx.bump_bp) if ctx.include_second_order else None
    factor_risk = fx_futures_factor_risk(
        risk, instrument.rub_curve_id, instrument.foreign_curve_id, second
    )
    margin = _money(q * contract.multiplier * (quote.price - quote.margined_price), "margin")
    row = FxFuturesPositionValuation(
        position.position_id,
        instrument.instrument_id,
        position.book,
        position.accounting_category,
        ctx.calculation_id(position, FX_FUTURES_RISK_MODEL_VERSION, engine.base_id),
        q,
        margin,
        factor_risk,
        contract.contract_id,
        currency,
        contract.delivery_date,
        contract.multiplier,
        engine.base_price,
        quote.margined_price,
        engine.theoretical_price,
        engine.basis_bp,
        risk.spot_rate,
        spot.value_date,
        instrument.rub_curve_id,
        instrument.foreign_curve_id,
        risk.notional_rub,
        margin,
        risk.fx_delta_foreign,
    )

    def reprice(scenario: ScenarioView) -> float:
        shock = FxFuturesScenario(
            scenario.curve_shift(instrument.rub_curve_id),
            scenario.curve_shift(instrument.foreign_curve_id),
            0.0,
            scenario.fx_move_pct(currency),
        )
        return engine.reprice(shock).total_pnl_rub

    model = _model(
        position,
        instrument.instrument_id,
        instrument.product.value,
        FX_FUTURES_RISK_MODEL_VERSION,
        "covered_parity",
        "daily_margined",
        "implied_basis",
    )
    return ProductOutcome(row, reprice, model)


# --- rate futures -------------------------------------------------------------------


def _rate_futures_engine(
    instrument: RateFuturesInstrument, ctx: ProductContext
) -> RateFuturesRiskEngine:
    contract = instrument.contract
    if contract.last_trading_day < ctx.business_date:
        raise ExpiredContract(
            f"futures {contract.contract_id} expired on {contract.last_trading_day.isoformat()}"
        )
    quote = ctx.snapshot.futures_quote(contract.contract_id)
    if isinstance(contract, KeyRateFuturesContract):
        request = RateFuturesRiskRequest(
            contract,
            ctx.key_rate_curve(instrument.projection_curve_id),
            futures_price=quote.price,
        )
    else:
        assert instrument.fixing_id is not None
        anchor = (
            ctx.snapshot.latest_ruonia_index()
            if isinstance(contract, RuoniaIndexFuturesContract)
            else None
        )
        request = RateFuturesRiskRequest(
            contract,
            ctx.projection(instrument.projection_curve_id, "ruonia").curve,
            ctx.snapshot.ruonia_fixing(instrument.fixing_id),
            anchor,
            futures_price=quote.price,
        )
    try:
        try:
            return RateFuturesRiskEngine(request, ctx.curve_cache)
        except InvalidTerms:
            # A RUON or index contract whose whole rate period is fixed has no basis to
            # calibrate: its fixings determine the price.
            probe = RateFuturesRiskEngine(
                RateFuturesRiskRequest(
                    contract,
                    request.projection_curve,
                    request.fixings,
                    request.index_anchor,
                    basis_bp=0.0,
                ),
                ctx.curve_cache,
            )
            if isinstance(contract, KeyRateFuturesContract) or probe.risk().projected_days:
                raise
            return probe
    except MissingFixing as exc:
        raise UndeclaredFixingGap(exc.message) from None


def value_rate_future(
    position: Position, instrument: object, ctx: ProductContext
) -> ProductOutcome:
    assert isinstance(instrument, RateFuturesInstrument)
    _check_position(position, instrument.currency)
    base = ctx.shared_engine(
        instrument.instrument_id, lambda: _rate_futures_engine(instrument, ctx)
    )
    assert isinstance(base, RateFuturesRiskEngine)
    contract = instrument.contract
    q = position.quantity
    engine = base.for_quantity(q)
    risk = engine.risk(bump_bp=ctx.bump_bp, difference="central")
    second = engine.second_order(bump_bp=ctx.bump_bp) if ctx.include_second_order else None
    factor_risk = rate_futures_factor_risk(risk, instrument.projection_curve_id, second)
    quote = ctx.snapshot.futures_quote(contract.contract_id)
    margin = rate_futures_variation_margin(contract, quote.margined_price, quote.price, q)
    request = engine.request
    kind: Literal["ruonia_average", "ruonia_index", "key_rate"] = (
        "ruonia_average"
        if isinstance(contract, RuoniaAverageFuturesContract)
        else "ruonia_index"
        if isinstance(contract, RuoniaIndexFuturesContract)
        else "key_rate"
    )
    row = RateFuturesPositionValuation(
        position.position_id,
        instrument.instrument_id,
        position.book,
        position.accounting_category,
        ctx.calculation_id(position, RATE_FUTURES_RISK_MODEL_VERSION, engine.base_id),
        q,
        margin,
        factor_risk,
        contract.contract_id,
        kind,
        contract.last_trading_day,
        contract.margin_per_point_rub,
        engine.base_price,
        quote.margined_price,
        engine.theoretical_price,
        engine.basis_bp,
        risk.expected_rate,
        risk.fixed_days,
        risk.projected_days,
        instrument.projection_curve_id,
        instrument.fixing_id,
        None if request.fixings is None else request.fixings.snapshot_id,
        None if request.index_anchor is None else request.index_anchor.index_date,
        margin,
    )

    def reprice(scenario: ScenarioView) -> float:
        shock = RateFuturesScenario(
            scenario.curve_shift(instrument.projection_curve_id, projection=True)
        )
        return engine.reprice(shock).total_pnl_rub

    flags = ["daily_margined"]
    flags.append("implied_basis" if request.futures_price is not None else "fully_fixed")
    if kind == "key_rate":
        flags.append("key_rate_projection")
    else:
        flags.append("known_fixings")
    model = _model(
        position,
        instrument.instrument_id,
        instrument.product.value,
        RATE_FUTURES_RISK_MODEL_VERSION,
        *flags,
    )
    return ProductOutcome(row, reprice, model)


# --- OFZ basket futures --------------------------------------------------------------


def _ofz_futures_engine(
    instrument: OfzFuturesInstrument, ctx: ProductContext
) -> OfzFuturesRiskEngine:
    contract = instrument.contract
    if contract.last_trading_day < ctx.business_date:
        raise ExpiredContract(
            f"futures {contract.contract_id} expired on {contract.last_trading_day.isoformat()}"
        )
    basket = []
    settlements = set()
    for item in instrument.basket:
        bond = ctx.instruments.get(item.instrument_id)
        if bond is None:
            raise InvalidTerms(f"basket bond {item.instrument_id} terms were not supplied")
        try:
            price = ctx.snapshot.quote(item.instrument_id)
        except MissingMarketData as exc:
            raise MissingBasketPrice(exc.message) from None
        calendar = ctx.calendar(bond.calendar_id)
        settlements.add(calendar.add_business_days(ctx.business_date, bond.settlement_lag_days))
        basket.append(DeliverableBond(bond, item.conversion_factor, price.clean_price_pct))
    if len(settlements) != 1:
        raise InvalidTerms("basket bonds settle on different dates")
    (settlement,) = settlements
    if settlement > contract.delivery_date:
        raise ExpiredContract(
            f"futures {contract.contract_id}: the basket settles after delivery "
            f"({contract.delivery_date.isoformat()})"
        )
    quote = ctx.snapshot.futures_quote(contract.contract_id)
    request = OfzFuturesRiskRequest(
        contract,
        tuple(basket),
        settlement,
        ctx.discount_curve(instrument.bond_curve_id, "RUB"),
        ctx.discount_curve(instrument.funding_curve_id, "RUB"),
        futures_price=quote.price,
    )
    return OfzFuturesRiskEngine(request, ctx.curve_cache)


def value_ofz_future(position: Position, instrument: object, ctx: ProductContext) -> ProductOutcome:
    assert isinstance(instrument, OfzFuturesInstrument)
    _check_position(position, instrument.currency)
    base = ctx.shared_engine(instrument.instrument_id, lambda: _ofz_futures_engine(instrument, ctx))
    assert isinstance(base, OfzFuturesRiskEngine)
    contract = instrument.contract
    q = position.quantity
    engine = base.for_quantity(q)
    risk = engine.risk(bump_bp=ctx.bump_bp, difference="central")
    second = engine.second_order(bump_bp=ctx.bump_bp) if ctx.include_second_order else None
    factor_risk = ofz_futures_factor_risk(
        risk, instrument.bond_curve_id, instrument.funding_curve_id, second
    )
    quote = ctx.snapshot.futures_quote(contract.contract_id)
    margin = _money(q * (quote.price - quote.margined_price), "variation margin")
    row = OfzFuturesPositionValuation(
        position.position_id,
        instrument.instrument_id,
        position.book,
        position.accounting_category,
        ctx.calculation_id(position, OFZ_FUTURES_RISK_MODEL_VERSION, engine.base_id),
        q,
        margin,
        factor_risk,
        contract.contract_id,
        contract.last_trading_day,
        contract.delivery_date,
        engine.request.spot_settlement_date,
        engine.base_price,
        quote.margined_price,
        engine.theoretical_price,
        engine.basis_bp,
        risk.cheapest_to_deliver,
        risk.cheapest_by_implied_repo,
        instrument.bond_curve_id,
        instrument.funding_curve_id,
        margin,
        risk.basket,
    )

    def reprice(scenario: ScenarioView) -> float:
        shock = OfzFuturesScenario(
            scenario.curve_shift(instrument.bond_curve_id),
            scenario.curve_shift(instrument.funding_curve_id),
            scenario.spread_shift_bp,
        )
        return engine.reprice(shock).total_pnl_rub

    model = _model(
        position,
        instrument.instrument_id,
        instrument.product.value,
        OFZ_FUTURES_RISK_MODEL_VERSION,
        "cheapest_to_deliver",
        "daily_margined",
        "has_optionality",
        "implied_basis",
    )
    return ProductOutcome(row, reprice, model)


# --- repo ---------------------------------------------------------------------------


def _collateral_price(ctx: ProductContext, instrument_id: str) -> float | None:
    """Dirty price per bond of a collateral bond whose terms and quote the job has."""
    bond = ctx.instruments.get(instrument_id)
    if bond is None:
        return None
    try:
        quote = ctx.snapshot.quote(instrument_id)
        calendar = ctx.snapshot.calendar(bond.calendar_id)
    except MissingMarketData:
        return None
    settlement = calendar.add_business_days(ctx.business_date, bond.settlement_lag_days)
    accrued = (
        quote.accrued_per_bond if quote.accrued_per_bond is not None else bond.accrued(settlement)
    )
    return quote.clean_price_pct / 100.0 * bond.outstanding_face(settlement) + accrued


def _repo_engine(
    position: Position, instrument: RepoInstrument, ctx: ProductContext
) -> tuple[RepoRiskEngine, FixingHistory | None]:
    """The repo leg's engine on the job's curves (shared by valuation and carry)."""
    trade = instrument.trade
    _check_position(position, instrument.currency)
    if trade.is_open and instrument.assumed_end_date is None:
        raise MissingAssumption(
            f"open repo {trade.trade_id} needs an explicit assumed_end_date; core assumes none"
        )
    end = trade.end_date if trade.end_date is not None else instrument.assumed_end_date
    assert end is not None
    if end <= ctx.business_date:
        raise SettledLeg(f"repo {trade.trade_id} ended on {end.isoformat()}")
    calendar = ctx.calendar(instrument.calendar_id)
    discount = ctx.discount_curve(instrument.discount_curve_id, "RUB")
    projection_id = instrument.projection_curve_id
    projection = None
    fixings = None
    rate = trade.rate
    if isinstance(rate, FloatingRepoRate):
        index: FixingIndex = "key_rate" if rate.benchmark == "KEY_RATE" else "ruonia"
        if projection_id is not None:
            projection = ctx.projection(projection_id, index).curve
        if instrument.fixing_id is None:
            raise MissingMarketData(f"floating repo {trade.trade_id} names no fixing history")
        fixings = ctx.snapshot.point_in_time_fixing(instrument.fixing_id)
    elif projection_id is not None or instrument.fixing_id is not None:
        raise InvalidTerms("a fixed-rate repo takes no projection curve or fixings")
    price = (
        None if trade.collateral is None else _collateral_price(ctx, trade.collateral.instrument_id)
    )
    engine = RepoRiskEngine(
        RepoRiskRequest(
            trade,
            calendar,
            discount,
            projection,
            fixings,
            instrument.assumed_end_date,
            price,
        ),
        ctx.curve_cache,
    )
    return engine, fixings


def value_repo(position: Position, instrument: object, ctx: ProductContext) -> ProductOutcome:
    assert isinstance(instrument, RepoInstrument)
    engine, fixings = _repo_engine(position, instrument, ctx)
    trade = instrument.trade
    projection_id = instrument.projection_curve_id
    rate = trade.rate
    valuation = engine.valuation()
    risk = engine.risk(bump_bp=ctx.bump_bp, difference="central")
    second = engine.second_order(bump_bp=ctx.bump_bp) if ctx.include_second_order else None
    q = position.quantity
    factor_risk = scale_factor_risk(
        repo_factor_risk(
            risk,
            discount_curve_id=instrument.discount_curve_id,
            projection_curve_id=projection_id,
            second_order=second,
        ),
        q,
    )
    row = RepoPositionValuation(
        position.position_id,
        instrument.instrument_id,
        position.book,
        position.accounting_category,
        ctx.calculation_id(position, REPO_MODEL_VERSION, engine.base_id),
        q,
        _money(valuation.pv_rub * q, "repo PV"),
        factor_risk,
        trade.trade_id,
        trade.side,
        trade.start_date,
        valuation.end_date,
        valuation.end_date_assumed,
        instrument.discount_curve_id,
        projection_id,
        instrument.fixing_id,
        None if fixings is None else fixings.snapshot_id,
        valuation.start_amount_rub,
        valuation.end_amount_rub,
        valuation.end_amount_source,
        valuation.known_interest_rub,
        valuation.accrued_interest_rub,
        valuation.current_repurchase_amount_rub,
        valuation.collateral,
    )

    def reprice(scenario: ScenarioView) -> float:
        shock = RepoScenario(
            scenario.curve_shift(instrument.discount_curve_id),
            (
                scenario.curve_shift(projection_id, projection=True)
                if projection_id is not None
                else RepoScenario().projection_shifts_bp
            ),
        )
        return _money(engine.reprice(shock).total_pnl_rub * q, "repo scenario P&L")

    flags = []
    if isinstance(rate, FloatingRepoRate):
        flags.append("two_curve" if projection_id is not None else "single_curve")
        flags.append("known_fixings")
    if valuation.end_date_assumed:
        flags.append("assumed_end")
    if trade.collateral is not None:
        flags.append("collateral_reference")
    model = _model(
        position, instrument.instrument_id, instrument.product.value, REPO_MODEL_VERSION, *flags
    )
    return ProductOutcome(row, reprice, model)


# --- carry (horizon_portfolio) ---------------------------------------------------------


@dataclass(frozen=True)
class DerivativeHorizonHeader:
    """The leading fields of every derivative carry row (documentation of the shape).

    Money is signed RUB for the position: the trade's amounts (``per_trade``) times the
    quantity, converted to RUB where the product is not. ``carry`` realises today's
    forwards, ``rolldown`` moves the curves to the horizon with their shape held, and
    ``horizon_pnl = carry + rolldown`` is the theta; ``received`` (flows paid to the
    horizon, as cash) is part of carry.
    """

    position_id: str
    instrument_id: str
    book: str
    accounting_category: AccountingCategory
    quantity: float
    carry_rub: float
    rolldown_rub: float
    received_rub: float
    horizon_pnl_rub: float


@dataclass(frozen=True)
class SwapPositionHorizon:
    """Carry, rolldown and theta of a swap position (``swaps.swap_horizon``)."""

    position_id: str
    instrument_id: str
    book: str
    accounting_category: AccountingCategory
    quantity: float
    carry_rub: float
    rolldown_rub: float
    received_rub: float
    horizon_pnl_rub: float
    discount_curve_id: str
    projection_curve_id: str | None
    per_trade: SwapHorizonValuation


@dataclass(frozen=True)
class RepoPositionHorizon:
    """Carry, rolldown and theta of a repo leg position (``repo.repo_carry``).

    ``carry_rub`` is the PV carry (today's forwards realised); ``interest_carry_rub``
    is the leg's accrual P&L over the same days.
    """

    position_id: str
    instrument_id: str
    book: str
    accounting_category: AccountingCategory
    quantity: float
    carry_rub: float
    rolldown_rub: float
    received_rub: float
    horizon_pnl_rub: float
    discount_curve_id: str
    projection_curve_id: str | None
    interest_carry_rub: float
    per_trade: RepoCarry


@dataclass(frozen=True)
class CdsPositionHorizon:
    """Carry, rolldown and theta of a CDS position (``cds.cds_horizon``), conditional on
    no default before the horizon.

    ``per_trade`` is in the contract currency; a non-RUB contract is converted at the
    snapshot rate ``fx_rate`` (``fx_horizon="spot_held"`` only).
    """

    position_id: str
    instrument_id: str
    book: str
    accounting_category: AccountingCategory
    quantity: float
    carry_rub: float
    rolldown_rub: float
    received_rub: float
    horizon_pnl_rub: float
    currency: str
    fx_rate: float
    discount_curve_id: str
    per_trade: CdsHorizonValuation


@dataclass(frozen=True)
class RateFuturesPositionHorizon:
    """Carry, rolldown and theta of a short-rate futures position
    (``rate_futures.rate_futures_horizon``): the variation margin to the horizon on the
    model price with the calibrated basis held; ``received_rub`` is zero."""

    position_id: str
    instrument_id: str
    book: str
    accounting_category: AccountingCategory
    quantity: float
    carry_rub: float
    rolldown_rub: float
    received_rub: float
    horizon_pnl_rub: float
    contract_id: str
    projection_curve_id: str
    price_start: float
    price_forward: float
    price_rolled: float
    per_trade: RateFuturesHorizonValuation


@dataclass(frozen=True)
class OfzFuturesPositionHorizon:
    """Carry, rolldown and theta of an OFZ futures position
    (``ofz_futures.ofz_futures_horizon``): the variation margin to the horizon, spreads
    and basis held; ``cheapest_*`` name the cheapest bond in each view."""

    position_id: str
    instrument_id: str
    book: str
    accounting_category: AccountingCategory
    quantity: float
    carry_rub: float
    rolldown_rub: float
    received_rub: float
    horizon_pnl_rub: float
    contract_id: str
    bond_curve_id: str
    funding_curve_id: str
    price_start: float
    price_forward: float
    price_rolled: float
    cheapest_start: str
    cheapest_rolled: str
    per_trade: OfzFuturesHorizonValuation


@dataclass(frozen=True)
class FxForwardPositionHorizon:
    """Carry, rolldown and theta of an FX forward or swap position
    (``fx_forwards.fx_forward_horizon``); the spot at the horizon follows ``fx_horizon``."""

    position_id: str
    instrument_id: str
    book: str
    accounting_category: AccountingCategory
    quantity: float
    carry_rub: float
    rolldown_rub: float
    received_rub: float
    horizon_pnl_rub: float
    currency: str
    rub_curve_id: str
    foreign_curve_id: str
    horizon_spot_rate: float
    horizon_spot_value_date: date
    per_trade: FxForwardHorizonValuation


@dataclass(frozen=True)
class XccyPositionHorizon:
    """Carry, rolldown and theta of a cross-currency swap position
    (``xccy.xccy_horizon``); the spot at the horizon follows ``fx_horizon``."""

    position_id: str
    instrument_id: str
    book: str
    accounting_category: AccountingCategory
    quantity: float
    carry_rub: float
    rolldown_rub: float
    received_rub: float
    horizon_pnl_rub: float
    currency: str
    horizon_spot_rate: float
    horizon_spot_value_date: date
    per_trade: XccyHorizonValuation


@dataclass(frozen=True)
class FxFuturesPositionHorizon:
    """Carry, rolldown and theta of an FX futures position
    (``fx_futures.fx_futures_horizon``): the variation margin to the horizon with the
    calibrated basis held; ``received_rub`` is zero."""

    position_id: str
    instrument_id: str
    book: str
    accounting_category: AccountingCategory
    quantity: float
    carry_rub: float
    rolldown_rub: float
    received_rub: float
    horizon_pnl_rub: float
    contract_id: str
    currency: str
    price_start: float
    price_forward: float
    price_rolled: float
    horizon_spot_rate: float
    horizon_spot_value_date: date
    per_trade: FxFuturesHorizonValuation


HorizonRow = (
    SwapPositionHorizon
    | FxForwardPositionHorizon
    | XccyPositionHorizon
    | CdsPositionHorizon
    | FxFuturesPositionHorizon
    | RateFuturesPositionHorizon
    | OfzFuturesPositionHorizon
    | RepoPositionHorizon
)

HORIZON_ROW_FIELDS: dict[type, str] = {
    SwapPositionHorizon: "swaps",
    FxForwardPositionHorizon: "fx_forwards",
    XccyPositionHorizon: "xccy_swaps",
    CdsPositionHorizon: "cds",
    FxFuturesPositionHorizon: "fx_futures",
    RateFuturesPositionHorizon: "rate_futures",
    OfzFuturesPositionHorizon: "ofz_futures",
    RepoPositionHorizon: "repos",
}
"""The ``PortfolioHorizonResult`` field that holds each carry row type."""
HorizonAdapter = Callable[[Position, object, ProductContext, date, FxHorizon], HorizonRow]


def horizon_spot_date(
    business_date: date, spot: FxSpot, calendar: BusinessCalendar, horizon_date: date
) -> date:
    """The spot value date that applies on ``horizon_date``: today's spot lag in business
    days of the settlement calendar (TOM stays TOM), counted from the horizon."""
    lag = 0
    while calendar.add_business_days(business_date, lag) < spot.value_date:
        lag += 1
    if calendar.add_business_days(business_date, lag) != spot.value_date:
        raise InvalidTerms(
            f"FX spot value date {spot.value_date.isoformat()} is not a business-day lag "
            f"from {business_date.isoformat()}"
        )
    if lag == 0 and not calendar.is_business_day(horizon_date):
        return calendar.add_business_days(horizon_date, 1)
    return calendar.add_business_days(horizon_date, lag)


class _Carried(Protocol):
    @property
    def carry_rub(self) -> float: ...

    @property
    def rolldown_rub(self) -> float: ...

    @property
    def received_rub(self) -> float: ...

    @property
    def horizon_pnl_rub(self) -> float: ...


def _horizon_header(
    position: Position, instrument_id: str, carry: _Carried, factor: float, label: str
) -> tuple[str, str, str, AccountingCategory, float, float, float, float, float]:
    """The header fields of a carry row: the trade's RUB amounts times ``factor``."""
    return (
        position.position_id,
        instrument_id,
        position.book,
        position.accounting_category,
        position.quantity,
        _money(carry.carry_rub * factor, f"{label} carry"),
        _money(carry.rolldown_rub * factor, f"{label} rolldown"),
        _money(carry.received_rub * factor, f"{label} received"),
        _money(carry.horizon_pnl_rub * factor, f"{label} horizon P&L"),
    )


def horizon_swap(
    position: Position,
    instrument: object,
    ctx: ProductContext,
    horizon_date: date,
    fx_horizon: FxHorizon,
) -> SwapPositionHorizon:
    assert isinstance(instrument, SwapInstrument)
    engine, _ = _swap_engine(position, instrument, ctx)
    carry = engine.horizon(horizon_date)
    q = position.quantity
    return SwapPositionHorizon(
        *_horizon_header(position, instrument.instrument_id, carry, q, "swap"),
        instrument.discount_curve_id,
        instrument.projection_curve_id,
        carry,
    )


def horizon_repo(
    position: Position,
    instrument: object,
    ctx: ProductContext,
    horizon_date: date,
    fx_horizon: FxHorizon,
) -> RepoPositionHorizon:
    assert isinstance(instrument, RepoInstrument)
    engine, _ = _repo_engine(position, instrument, ctx)
    carry = engine.carry(horizon_date)
    q = position.quantity
    return RepoPositionHorizon(
        position.position_id,
        instrument.instrument_id,
        position.book,
        position.accounting_category,
        q,
        _money(carry.pv_carry_rub * q, "repo carry"),
        _money(carry.rolldown_rub * q, "repo rolldown"),
        _money(carry.received_rub * q, "repo received"),
        _money(carry.horizon_pnl_rub * q, "repo horizon P&L"),
        instrument.discount_curve_id,
        instrument.projection_curve_id,
        _money(carry.interest_carry_rub * q, "repo interest carry"),
        carry,
    )


def horizon_fx_forward(
    position: Position,
    instrument: object,
    ctx: ProductContext,
    horizon_date: date,
    fx_horizon: FxHorizon,
) -> FxForwardPositionHorizon:
    assert isinstance(instrument, FxForwardInstrument)
    engine, spot, settlement, _ = _fx_forward_engine(position, instrument, ctx)
    spot_date = horizon_spot_date(ctx.business_date, spot, settlement, horizon_date)
    carry = engine.horizon(horizon_date, horizon_spot_value_date=spot_date, fx_horizon=fx_horizon)
    return FxForwardPositionHorizon(
        *_horizon_header(position, instrument.instrument_id, carry, position.quantity, "FX"),
        instrument.currency,
        instrument.rub_curve_id,
        instrument.foreign_curve_id,
        carry.horizon_spot_rate,
        spot_date,
        carry,
    )


def horizon_xccy(
    position: Position,
    instrument: object,
    ctx: ProductContext,
    horizon_date: date,
    fx_horizon: FxHorizon,
) -> XccyPositionHorizon:
    assert isinstance(instrument, XccyInstrument)
    engine, spot, settlement = _xccy_engine(position, instrument, ctx)
    spot_date = horizon_spot_date(ctx.business_date, spot, settlement, horizon_date)
    carry = engine.horizon(horizon_date, horizon_spot_value_date=spot_date, fx_horizon=fx_horizon)
    return XccyPositionHorizon(
        *_horizon_header(position, instrument.instrument_id, carry, position.quantity, "XCCY"),
        instrument.swap.currency,
        carry.horizon_spot_rate,
        spot_date,
        carry,
    )


def horizon_fx_future(
    position: Position,
    instrument: object,
    ctx: ProductContext,
    horizon_date: date,
    fx_horizon: FxHorizon,
) -> FxFuturesPositionHorizon:
    assert isinstance(instrument, FxFuturesInstrument)
    engine, spot, settlement, _ = _fx_futures_engine(position, instrument, ctx)
    spot_date = horizon_spot_date(ctx.business_date, spot, settlement, horizon_date)
    carry = engine.horizon(horizon_date, horizon_spot_value_date=spot_date, fx_horizon=fx_horizon)
    return FxFuturesPositionHorizon(
        *_horizon_header(position, instrument.instrument_id, carry, 1.0, "FX futures"),
        instrument.contract.contract_id,
        instrument.contract.currency,
        carry.price_start,
        carry.price_forward,
        carry.price_rolled,
        carry.horizon_spot_rate,
        spot_date,
        carry,
    )


def horizon_cds(
    position: Position,
    instrument: object,
    ctx: ProductContext,
    horizon_date: date,
    fx_horizon: FxHorizon,
) -> CdsPositionHorizon:
    assert isinstance(instrument, CdsInstrument)
    currency = instrument.contract.currency
    if currency != "RUB" and fx_horizon != "spot_held":
        raise UnsupportedProduct(
            f"a {currency} CDS is carried with fx_horizon='spot_held' only (its position "
            "names no RUB curve for a forward FX rate)"
        )
    engine, _, fx = _cds_engine(position, instrument, ctx)
    carry = engine.horizon(horizon_date)
    scale = position.quantity * fx
    return CdsPositionHorizon(
        position.position_id,
        instrument.instrument_id,
        position.book,
        position.accounting_category,
        position.quantity,
        _money(carry.carry * scale, "CDS carry"),
        _money(carry.rolldown * scale, "CDS rolldown"),
        _money(carry.received * scale, "CDS received"),
        _money(carry.horizon_pnl * scale, "CDS horizon P&L"),
        currency,
        fx,
        instrument.discount_curve_id,
        carry,
    )


def horizon_rate_future(
    position: Position,
    instrument: object,
    ctx: ProductContext,
    horizon_date: date,
    fx_horizon: FxHorizon,
) -> RateFuturesPositionHorizon:
    assert isinstance(instrument, RateFuturesInstrument)
    _check_position(position, instrument.currency)
    base = ctx.shared_engine(
        instrument.instrument_id, lambda: _rate_futures_engine(instrument, ctx)
    )
    assert isinstance(base, RateFuturesRiskEngine)
    carry = base.for_quantity(position.quantity).horizon(horizon_date)
    return RateFuturesPositionHorizon(
        *_horizon_header(position, instrument.instrument_id, carry, 1.0, "rate futures"),
        instrument.contract.contract_id,
        instrument.projection_curve_id,
        carry.price_start,
        carry.price_forward,
        carry.price_rolled,
        carry,
    )


def horizon_ofz_future(
    position: Position,
    instrument: object,
    ctx: ProductContext,
    horizon_date: date,
    fx_horizon: FxHorizon,
) -> OfzFuturesPositionHorizon:
    assert isinstance(instrument, OfzFuturesInstrument)
    _check_position(position, instrument.currency)
    base = ctx.shared_engine(instrument.instrument_id, lambda: _ofz_futures_engine(instrument, ctx))
    assert isinstance(base, OfzFuturesRiskEngine)
    # The basket settles on one lag (checked when the engine was built).
    bond = ctx.instruments[instrument.basket[0].instrument_id]
    settlement = ctx.calendar(bond.calendar_id).add_business_days(
        horizon_date, bond.settlement_lag_days
    )
    carry = base.for_quantity(position.quantity).horizon(
        horizon_date, horizon_spot_settlement_date=settlement
    )
    return OfzFuturesPositionHorizon(
        *_horizon_header(position, instrument.instrument_id, carry, 1.0, "OFZ futures"),
        instrument.contract.contract_id,
        instrument.bond_curve_id,
        instrument.funding_curve_id,
        carry.price_start,
        carry.price_forward,
        carry.price_rolled,
        carry.cheapest_start,
        carry.cheapest_rolled,
        carry,
    )


HORIZON_ADAPTERS: Mapping[type, HorizonAdapter] = {
    SwapInstrument: horizon_swap,
    FxForwardInstrument: horizon_fx_forward,
    XccyInstrument: horizon_xccy,
    CdsInstrument: horizon_cds,
    FxFuturesInstrument: horizon_fx_future,
    RateFuturesInstrument: horizon_rate_future,
    OfzFuturesInstrument: horizon_ofz_future,
    RepoInstrument: horizon_repo,
}
"""Carry adapter per derivative position type (BACKLOG #101)."""


def horizon_adapter_for(instrument: object) -> HorizonAdapter:
    adapter = HORIZON_ADAPTERS.get(type(instrument))
    if adapter is None:
        product = getattr(instrument, "product", None)
        name = type(instrument).__name__ if product is None else product.value
        raise UnsupportedProduct(
            f"carry of {name} positions is not supported "
            "(horizon_portfolio carries bonds, swaps and repos)"
        )
    return adapter


PRODUCT_ADAPTERS: Mapping[type, Adapter] = {
    SwapInstrument: value_swap,
    FxForwardInstrument: value_fx_forward,
    XccyInstrument: value_xccy,
    CdsInstrument: value_cds_position,
    FxFuturesInstrument: value_fx_future,
    RateFuturesInstrument: value_rate_future,
    OfzFuturesInstrument: value_ofz_future,
    RepoInstrument: value_repo,
}
"""Valuation adapter per derivative position type (see the module docstring)."""


def adapter_for(instrument: object) -> Adapter:
    adapter = PRODUCT_ADAPTERS.get(type(instrument))
    if adapter is None:
        raise UnsupportedProduct(f"{type(instrument).__name__} positions are not supported")
    return adapter


def hessian_totals(risks: list[FactorRisk]) -> tuple[HessianTotal, ...]:
    """Sum of every position's Hessian, keyed by factor names (``factor_i <= factor_j``)."""
    sums: dict[tuple[str, str], list[float]] = {}
    for risk in risks:
        if not risk.has_second_order:
            continue
        for entry in risk.hessian:
            a, b = risk.factors[entry.i], risk.factors[entry.j]
            key = (a, b) if a <= b else (b, a)
            sums.setdefault(key, []).append(entry.value)
    return tuple(
        HessianTotal(a, b, finite_money(math.fsum(values), "Hessian total"))
        for (a, b), values in sorted(sums.items())
    )


def jtd_totals(contributions: list[tuple[str | None, float]]) -> tuple[JtdTotal, ...]:
    groups: dict[str | None, list[float]] = {}
    for entity, value in contributions:
        groups.setdefault(entity, []).append(value)
    ordered = sorted(groups, key=lambda e: (e is not None, e or ""))
    return tuple(
        JtdTotal(e, finite_money(math.fsum(groups[e]), "JTD total"), len(groups[e]))
        for e in ordered
    )
