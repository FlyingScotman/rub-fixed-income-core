"""Outstanding RUB repo and reverse-repo legs: instrument valuation (roadmap package 6).

A repo exchanges cash against securities on the start date (first leg) and back on
the end date (second leg). The cash lender (``reverse_repo``) pays the start amount
``S1`` and receives the repurchase amount ``S2`` with interest; the cash borrower
(``repo``) is the mirror. Interest accrues by **calendar day** ``i`` in
``(start, end]`` (the NFA "Срок РЕПО": from the day after the first leg to the second
leg inclusive), each day weighted by an explicit day basis:

- **Fixed** (MOEX Trading Rules, Part III, 1.5.8.5):
  ``S2 = S1 * (1 + R * (T365 / 365 + T366 / 366))`` with the ``ACT/365-366`` basis,
  or ``ACT/365F`` / ``ACT/360`` when the terms say so.
- **Floating, simple** (MOEX "Инструменты с плавающими ставками"):
  ``S2 = S1 * (1 + sum_i (r_i + spread) * w_i)``; ``r_i`` is the benchmark applying to
  day ``i``: RUSFAR or RUONIA of the settlement day before it (the last known value on
  days without a fixing), or the key rate in force on the day.
- **Floating, capitalised** (MOEX open repo with compounding, FRPO / FKPO): interest is
  credited on every settlement day ``d_k`` on the balance after the previous one,
  ``I_k = B_{k-1} * sum_{i in (d_{k-1}, d_k]} (r_i + spread) * w_i``, rounded to the
  kopeck; non-settlement days accrue simply until the next settlement day.

Observations dated before the valuation date take the point-in-time
:class:`~rub_fixed_income.floating.FixingHistory` (read-only, its gap and carry rules);
later ones the overnight forwards ``(DF(o) / DF(next) - 1) / tau`` of the projection
curve. Known amounts never move under curve shocks.

The value is the PV of the remaining cash legs on the RUB discount curve. The
collateral is **not** part of it: :class:`RepoCollateralReference` reports the bond,
the quantity and the coverage as a separate position reference, so a backend that
books the bond in the cash borrower's book (where its risk stays) does not count it
twice. Desk funding economics (previous-day clean MV base, calendar-day rates, OCP
transfers) are the backend's ``desk_rules``, not this module's.
"""

from __future__ import annotations

import math
from calendar import isleap
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from enum import Enum
from typing import Literal, cast

from ._money import finite_money, positive_money
from ._risk_common import (
    _ZERO_SHOCK,
    RISK_BUCKETS,
    ShockedCurveCache,
    _base_curve,
    _central_derivatives,
    _curve_basis,
    _curve_identity,
    _shifts,
    _shock_curve,
    _snapshot_curve,
)
from .canonical import _Canonical, _precanonical, canonical_sha256
from .conventions import DayCount, calendar_fingerprint
from .dates import BusinessCalendar
from .discount import CurveBootstrapRequest, DiscountCurve, DiscountFactorCurve
from .errors import InvalidTerms, MissingMarketData, UnsupportedProduct
from .factors import FactorRisk, _upper, ir_factor
from .floating import _FORWARD_MEMO_SIZE, FixingHistory, _forward_key, _forward_memo
from .risk import BucketRisk

__all__ = [
    "REPO_MODEL_VERSION",
    "REPO_SECOND_ORDER_MODEL_VERSION",
    "FixedRepoRate",
    "FloatingRepoRate",
    "RepoCarry",
    "RepoCashAdjustment",
    "RepoCashflow",
    "RepoCollateral",
    "RepoCollateralReference",
    "RepoCurveRisk",
    "RepoDayBasis",
    "RepoRiskEngine",
    "RepoRiskRequest",
    "RepoRiskResult",
    "RepoScenario",
    "RepoScenarioResult",
    "RepoSecondOrderRisk",
    "RepoTrade",
    "RepoValuation",
    "repo_carry",
    "repo_end_amount",
    "repo_factor_risk",
    "repo_initial_discount_pct",
    "repo_start_amount",
]

REPO_MODEL_VERSION = "rub-repo-leg-v1"
REPO_SECOND_ORDER_MODEL_VERSION = "rub-repo-leg-second-order-v1"

RepoSide = Literal["repo", "reverse_repo"]
RepoBenchmark = Literal["RUSFAR", "RUONIA", "KEY_RATE"]
RepoObservation = Literal["previous_settlement_day", "calendar_day"]
RepoInterest = Literal["simple", "capitalised"]
RepoAdjustmentKind = Literal["margin_call", "income_compensation", "repurchase_adjustment"]
RepoFlowKind = Literal["start_leg", "end_leg", "adjustment"]
AmountSource = Literal["fixed", "known", "partially_projected", "projected"]
RepoCurveRole = Literal["discount", "projection", "discount_and_projection"]

# The benchmark/observation pairs confirmed by the MOEX sources (docs/repo.md).
_OBSERVATION: dict[str, str] = {
    "RUSFAR": "previous_settlement_day",
    "RUONIA": "previous_settlement_day",
    "KEY_RATE": "calendar_day",
}
_HISTORY_INDEX: dict[str, str] = {"RUSFAR": "ruonia", "RUONIA": "ruonia", "KEY_RATE": "key_rate"}
_MEMO_SIZE = 4096
_Vector = tuple[float, ...]


class RepoDayBasis(str, Enum):
    """Weight of one calendar day of interest.

    ``ACT_365_366`` is the MOEX and NFA rouble basis ("фактическое количество дней в
    году (365 или 366)"): each day ``i`` of ``(start, end]`` counts ``1 / 366`` in a
    leap year and ``1 / 365`` otherwise, which is ``T365 / 365 + T366 / 366`` of the
    MOEX formula. It differs from ACT/ACT ISDA (``[start, end)``) on a year boundary.
    """

    ACT_365_FIXED = "ACT/365F"
    ACT_360 = "ACT/360"
    ACT_365_366 = "ACT/365-366"

    def day_weight(self, day: date) -> float:
        if self is RepoDayBasis.ACT_365_FIXED:
            return 1.0 / 365.0
        if self is RepoDayBasis.ACT_360:
            return 1.0 / 360.0
        return 1.0 / (366.0 if isleap(day.year) else 365.0)

    def year_fraction(self, start: date, end: date) -> float:
        """Sum of the day weights over ``(start, end]``."""
        if end < start:
            raise ValueError("end must not precede start")
        if self is RepoDayBasis.ACT_365_FIXED:
            return (end - start).days / 365.0
        if self is RepoDayBasis.ACT_360:
            return (end - start).days / 360.0
        total = 0.0
        for year in range(start.year, end.year + 1):
            first = max(start, date(year - 1, 12, 31))
            last = min(end, date(year, 12, 31))
            if last > first:
                total += (last - first).days / (366.0 if isleap(year) else 365.0)
        return total


def _round_half_up(value: float, decimals: int) -> float:
    return float(Decimal(str(value)).quantize(Decimal(1).scaleb(-decimals), ROUND_HALF_UP))


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or "|" in value:
        raise InvalidTerms(f"{label} must be a non-empty string without '|'")
    return value


def _decimals(value: object, label: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 8:
        raise InvalidTerms(f"{label} must be None or an integer between 0 and 8")
    return value


@dataclass(frozen=True)
class FixedRepoRate:
    """A fixed repo rate (decimal annual) on an explicit day basis.

    ``interest="simple"`` is the MOEX term repo formula; ``"capitalised"`` credits the
    interest on every settlement day (compounding open repo).
    """

    rate: float
    day_basis: RepoDayBasis
    interest: RepoInterest = "simple"

    def __post_init__(self) -> None:
        object.__setattr__(self, "rate", finite_money(self.rate, "repo rate"))
        if not isinstance(self.day_basis, RepoDayBasis):
            raise TypeError("day_basis must be a RepoDayBasis")
        if self.interest not in ("simple", "capitalised"):
            raise InvalidTerms("interest must be simple or capitalised")


@dataclass(frozen=True)
class FloatingRepoRate:
    """A floating repo rate: benchmark plus a simple spread (decimal annual).

    ``observation`` must state which benchmark value applies to a day:

    - ``"previous_settlement_day"`` (RUSFAR, RUONIA): the fixing of the settlement day
      before the day, or the last fixing published on or before it (MOEX: "значение
      индикатора применяется за предыдущий Расчетный день ... принимается равным
      последнему известному значению");
    - ``"calendar_day"`` (key rate): the rate in force on the day.

    ``projection_day_count`` turns projection-curve discount factors into overnight
    rates (it should be the curve's own rate basis, for example the ``rate_day_count``
    of ``key_rate_projection_curve``). ``nonpositive_rate_replacement`` is the MOEX GC
    (KSU) rule: when ``benchmark + spread <= 0`` the repo rate is this value (0.0001).
    """

    benchmark: RepoBenchmark
    spread: float
    day_basis: RepoDayBasis
    observation: RepoObservation
    projection_day_count: DayCount
    interest: RepoInterest = "simple"
    nonpositive_rate_replacement: float | None = None

    def __post_init__(self) -> None:
        if self.benchmark not in _OBSERVATION:
            raise InvalidTerms("benchmark must be RUSFAR, RUONIA or KEY_RATE")
        object.__setattr__(self, "spread", finite_money(self.spread, "repo spread"))
        if not isinstance(self.day_basis, RepoDayBasis):
            raise TypeError("day_basis must be a RepoDayBasis")
        if self.observation not in ("previous_settlement_day", "calendar_day"):
            raise InvalidTerms("observation must be previous_settlement_day or calendar_day")
        if self.observation != _OBSERVATION[self.benchmark]:
            raise UnsupportedProduct(
                f"{self.benchmark} observed on {self.observation} is not a confirmed "
                f"convention (supported: {_OBSERVATION[self.benchmark]})"
            )
        if not isinstance(self.projection_day_count, DayCount):
            raise TypeError("projection_day_count must be a DayCount")
        if self.interest not in ("simple", "capitalised"):
            raise InvalidTerms("interest must be simple or capitalised")
        if self.interest == "capitalised" and self.benchmark == "KEY_RATE":
            raise UnsupportedProduct(
                "capitalised key-rate repo (MOEX FKPO) applies the closing settlement "
                "day's key rate to the whole period; not implemented"
            )
        if self.nonpositive_rate_replacement is not None:
            replacement = finite_money(self.nonpositive_rate_replacement, "rate replacement")
            if replacement <= 0.0:
                raise InvalidTerms("the nonpositive-rate replacement must be positive")
            object.__setattr__(self, "nonpositive_rate_replacement", replacement)


@dataclass(frozen=True)
class RepoCollateral:
    """The securities delivered on the first leg, as a reference (never valued here).

    ``quantity`` is the number of bonds (or GC clearing certificates) currently held as
    collateral, after any securities margin transfers. ``initial_discount_pct`` is the
    haircut agreed at the trade (MOEX "начальное значение дисконта"); ``pool`` names a
    GC pool (for example ``"GC Bonds"``) when the collateral is a KSU certificate.
    """

    instrument_id: str
    quantity: float
    initial_discount_pct: float | None = None
    pool: str | None = None

    def __post_init__(self) -> None:
        _text(self.instrument_id, "collateral instrument_id")
        object.__setattr__(self, "quantity", positive_money(self.quantity, "collateral quantity"))
        if self.initial_discount_pct is not None:
            discount = finite_money(self.initial_discount_pct, "initial discount pct")
            if not 0.0 <= discount < 100.0:
                raise InvalidTerms("the initial discount must be in [0, 100) percent")
            object.__setattr__(self, "initial_discount_pct", discount)
        if self.pool is not None:
            _text(self.pool, "collateral pool")


@dataclass(frozen=True)
class RepoCashAdjustment:
    """An explicit cash flow of the trade besides its two legs.

    ``amount_rub`` is signed for the **cash lender**: positive when the lender
    receives it (a cash margin call paid by the borrower), negative when it pays (a
    coupon passed back to the borrower, NFA 10.1; a margin return). An agreed change of
    the repurchase amount (NFA 10.2) is an adjustment paid on the end date. Core never
    derives margin calls; they are inputs.
    """

    payment_date: date
    amount_rub: float
    kind: RepoAdjustmentKind

    def __post_init__(self) -> None:
        if not isinstance(self.payment_date, date):
            raise TypeError("payment_date must be a date")
        object.__setattr__(self, "amount_rub", finite_money(self.amount_rub, "adjustment"))
        if self.kind not in ("margin_call", "income_compensation", "repurchase_adjustment"):
            raise InvalidTerms(
                "adjustment kind must be margin_call, income_compensation or repurchase_adjustment"
            )


@dataclass(frozen=True)
class RepoTrade:
    """Terms of one repo; market-independent.

    ``side="reverse_repo"`` lends cash (pays ``start_amount_rub`` on ``start_date``,
    receives the repurchase amount on the end date); ``"repo"`` borrows it.
    ``end_date=None`` is an open repo, whose end must be assumed explicitly at
    valuation. ``amount_rounding_decimals`` rounds amounts fully fixed by the terms
    and fixings (MOEX: 2, to the kopeck); projected amounts are never rounded.
    """

    trade_id: str
    side: RepoSide
    start_date: date
    end_date: date | None
    start_amount_rub: float
    rate: FixedRepoRate | FloatingRepoRate
    amount_rounding_decimals: int | None
    collateral: RepoCollateral | None = None
    cash_adjustments: tuple[RepoCashAdjustment, ...] = ()
    currency: str = "RUB"

    def __post_init__(self) -> None:
        _text(self.trade_id, "trade_id")
        if self.side not in ("repo", "reverse_repo"):
            raise InvalidTerms("side must be repo or reverse_repo")
        if self.currency != "RUB":
            raise UnsupportedProduct("only RUB repos are supported")
        if not isinstance(self.start_date, date):
            raise TypeError("start_date must be a date")
        if self.end_date is not None:
            if not isinstance(self.end_date, date):
                raise TypeError("end_date must be a date or None")
            if self.end_date <= self.start_date:
                raise InvalidTerms("the end date must follow the start date")
        object.__setattr__(
            self, "start_amount_rub", positive_money(self.start_amount_rub, "start amount")
        )
        if not isinstance(self.rate, (FixedRepoRate, FloatingRepoRate)):
            raise TypeError("rate must be a FixedRepoRate or FloatingRepoRate")
        _decimals(self.amount_rounding_decimals, "amount_rounding_decimals")
        if self.collateral is not None and not isinstance(self.collateral, RepoCollateral):
            raise TypeError("collateral must be a RepoCollateral")
        adjustments = tuple(self.cash_adjustments)
        for adjustment in adjustments:
            if not isinstance(adjustment, RepoCashAdjustment):
                raise TypeError("cash_adjustments must be RepoCashAdjustment values")
            if adjustment.payment_date < self.start_date:
                raise InvalidTerms("a cash adjustment cannot precede the start date")
            if self.end_date is not None and adjustment.payment_date > self.end_date:
                raise InvalidTerms("a cash adjustment cannot follow the end date")
        object.__setattr__(self, "cash_adjustments", adjustments)

    @property
    def sign(self) -> float:
        """+1 for the cash lender, -1 for the cash borrower."""
        return 1.0 if self.side == "reverse_repo" else -1.0

    @property
    def is_open(self) -> bool:
        return self.end_date is None


@dataclass(frozen=True)
class RepoRiskRequest:
    """A repo, its settlement calendar and its market.

    - ``calendar``: settlement days ("Расчетные дни"); start and end must be among them
      and capitalised interest is credited on them.
    - ``discount_curve``: the RUB curve the remaining cash legs are discounted on; its
      date is the valuation date. A ``DiscountCurve`` takes zero-rate overlays, a
      ``CurveBootstrapRequest`` quote shifts and a rebootstrap.
    - ``projection_curve``: floating repos only; ``None`` projects on the discount
      curve (single curve).
    - ``fixings``: required for floating repos (may be empty); dated the valuation
      date. RUSFAR and RUONIA use a ``FixingHistory`` with ``index="ruonia"`` (the
      daily-publication rules) on the benchmark's own publication calendar; the key
      rate one with ``index="key_rate"``.
    - ``assumed_end_date``: the end assumed for an open repo (required for one,
      rejected for a term repo); recorded in the identity.
    - ``collateral_dirty_price_rub_per_bond``: the collateral's current dirty price,
      only for the collateral reference.
    """

    trade: RepoTrade
    calendar: BusinessCalendar
    discount_curve: DiscountCurve | CurveBootstrapRequest
    projection_curve: DiscountCurve | CurveBootstrapRequest | None = None
    fixings: FixingHistory | None = None
    assumed_end_date: date | None = None
    collateral_dirty_price_rub_per_bond: float | None = None

    def __post_init__(self) -> None:
        trade = self.trade
        if not isinstance(trade, RepoTrade):
            raise TypeError("trade must be a RepoTrade")
        if not isinstance(self.calendar, BusinessCalendar):
            raise TypeError("calendar must be a BusinessCalendar")
        if not isinstance(self.discount_curve, (DiscountCurve, CurveBootstrapRequest)):
            raise TypeError("discount_curve must be DiscountCurve or CurveBootstrapRequest")
        valuation = self.discount_curve.curve_date
        projection = self.projection_curve
        floating = isinstance(trade.rate, FloatingRepoRate)
        if projection is not None:
            if not floating:
                raise InvalidTerms("a fixed-rate repo takes no projection curve")
            if not isinstance(projection, (DiscountCurve, CurveBootstrapRequest)):
                raise TypeError("projection_curve must be DiscountCurve or CurveBootstrapRequest")
            if projection.curve_date != valuation:
                raise InvalidTerms("discount and projection curves must share one date")
        if floating:
            history = self.fixings
            if history is None:
                raise MissingMarketData("a floating repo needs its benchmark fixing history")
            if not isinstance(history, FixingHistory):
                raise TypeError("fixings must be a FixingHistory")
            rate = cast(FloatingRepoRate, trade.rate)
            if history.index != _HISTORY_INDEX[rate.benchmark]:
                raise InvalidTerms(
                    f"{rate.benchmark} fixings need a FixingHistory with index "
                    f"{_HISTORY_INDEX[rate.benchmark]!r}"
                )
            if history.valuation_date != valuation:
                raise InvalidTerms("fixing history valuation date must equal the curve date")
        elif self.fixings is not None:
            raise InvalidTerms("a fixed-rate repo takes no fixings")
        if trade.is_open:
            if self.assumed_end_date is None:
                raise InvalidTerms("an open repo needs an explicit assumed_end_date")
            if not isinstance(self.assumed_end_date, date):
                raise TypeError("assumed_end_date must be a date")
            if self.assumed_end_date <= trade.start_date:
                raise InvalidTerms("the assumed end must follow the start date")
        elif self.assumed_end_date is not None:
            raise InvalidTerms("a term repo has its own end date; assumed_end_date is None")
        end = self.end_date
        for label, day in (("start", trade.start_date), ("end", end)):
            if not self.calendar.is_business_day(day):
                raise InvalidTerms(f"the {label} date {day.isoformat()} is not a settlement day")
        for adjustment in trade.cash_adjustments:
            if adjustment.payment_date > end:
                raise InvalidTerms("a cash adjustment cannot follow the (assumed) end date")
        if self.collateral_dirty_price_rub_per_bond is not None:
            if trade.collateral is None:
                raise InvalidTerms("a collateral price needs the trade's collateral")
            object.__setattr__(
                self,
                "collateral_dirty_price_rub_per_bond",
                positive_money(self.collateral_dirty_price_rub_per_bond, "collateral price"),
            )
        object.__setattr__(self, "discount_curve", _snapshot_curve(self.discount_curve))
        if projection is not None:
            object.__setattr__(self, "projection_curve", _snapshot_curve(projection))

    @property
    def valuation_date(self) -> date:
        return self.discount_curve.curve_date

    @property
    def end_date(self) -> date:
        """The contractual end, or the assumed end of an open repo."""
        end = self.trade.end_date if self.trade.end_date is not None else self.assumed_end_date
        assert end is not None
        return end

    @property
    def single_curve(self) -> bool:
        return self.projection_curve is None


@dataclass(frozen=True)
class RepoScenario:
    """Absolute bp bucket shifts of the discount and projection curves.

    A fixed or single-curve repo moves its one curve with ``discount_shifts_bp``;
    ``projection_shifts_bp`` must then stay zero.
    """

    discount_shifts_bp: tuple[float, ...] = _ZERO_SHOCK
    projection_shifts_bp: tuple[float, ...] = _ZERO_SHOCK

    def __post_init__(self) -> None:
        object.__setattr__(self, "discount_shifts_bp", _shifts(tuple(self.discount_shifts_bp)))
        object.__setattr__(self, "projection_shifts_bp", _shifts(tuple(self.projection_shifts_bp)))

    @classmethod
    def parallel(cls, discount_bp: float = 0.0, projection_bp: float = 0.0) -> RepoScenario:
        size = len(RISK_BUCKETS)
        return cls((discount_bp,) * size, (projection_bp,) * size)


@dataclass(frozen=True)
class RepoCashflow:
    """One remaining flow; ``amount_rub`` and ``pv_rub`` are signed for the position."""

    kind: RepoFlowKind
    payment_date: date
    amount_rub: float
    source: AmountSource
    discount_factor: float
    pv_rub: float


@dataclass(frozen=True)
class RepoCollateralReference:
    """The collateral as a separate position reference; **not** included in any PV.

    ``market_value_rub = quantity * dirty price`` (when the price is given),
    ``exposure_rub = current repurchase amount - market value`` (the cash lender's
    unsecured amount, positive when under-collateralised) and ``coverage_ratio =
    market value / current repurchase amount``. ``booking`` documents the rule: the
    bond stays a position of the cash borrower (``repo`` side) and is valued there by
    the bond engines; the cash lender holds only the repo leg.
    """

    instrument_id: str
    quantity: float
    pool: str | None
    initial_discount_pct: float | None
    dirty_price_rub_per_bond: float | None
    market_value_rub: float | None
    current_repurchase_amount_rub: float
    exposure_rub: float | None
    coverage_ratio: float | None
    booking: str = "bond booked by the cash borrower; the repo leg values cash only"


@dataclass(frozen=True)
class RepoValuation:
    """PV and its parts at the valuation date.

    Unsigned contract amounts: ``start_amount_rub`` (S1), ``end_amount_rub`` (S2,
    expected), ``known_interest_rub`` (interest from fixed rates and known fixings),
    ``accrued_interest_rub`` (interest of the days up to the valuation date inclusive,
    projected where a fixing is not yet known) and ``current_repurchase_amount_rub``
    (S1 plus that accrued, MOEX "сумма к исполнению"). ``repurchase_price_rub_per_bond``
    is S2 per collateral bond (dirty). Signed for the position: ``pv_rub`` and its legs.
    """

    base_id: str
    trade_id: str
    side: RepoSide
    valuation_date: date
    end_date: date
    end_date_assumed: bool
    discount_curve_basis: str
    projection_curve_basis: str
    start_amount_rub: float
    end_amount_rub: float
    end_amount_source: AmountSource
    interest_rub: float
    known_interest_rub: float
    accrued_interest_rub: float
    current_repurchase_amount_rub: float
    repurchase_price_rub_per_bond: float | None
    pv_rub: float
    start_leg_pv_rub: float
    end_leg_pv_rub: float
    adjustments_pv_rub: float
    cashflows: tuple[RepoCashflow, ...]
    collateral: RepoCollateralReference | None
    model_version: str = REPO_MODEL_VERSION


@dataclass(frozen=True)
class RepoCurveRisk:
    """Bucket risk of one curve role; ``krd_years = -dv01 / start amount * 1e4``."""

    curve_role: RepoCurveRole
    curve_basis: str
    buckets: tuple[BucketRisk, ...]
    dv01_rub: float
    parallel_dv01_rub: float


@dataclass(frozen=True)
class RepoRiskResult:
    """Signed RUB per bp; a cash lender's discount DV01 is negative."""

    base_id: str
    trade_id: str
    side: RepoSide
    difference: str
    bump_bp: float
    start_amount_rub: float
    pv_rub: float
    curves: tuple[RepoCurveRisk, ...]
    dv01_rub: float
    model_version: str = REPO_MODEL_VERSION


@dataclass(frozen=True)
class RepoScenarioResult:
    base_id: str
    scenario_id: str
    base_pv_rub: float
    shocked_pv_rub: float
    discount_pnl_rub: float
    projection_pnl_rub: float
    interaction_pnl_rub: float
    total_pnl_rub: float
    model_version: str = REPO_MODEL_VERSION


@dataclass(frozen=True)
class RepoSecondOrderRisk:
    """Central gradient (RUB/bp) and full symmetric Hessian (RUB/bp squared).

    11 factors for a fixed or single-curve repo, else 11 discount then 11 projection.
    """

    base_id: str
    curve_roles: tuple[RepoCurveRole, ...]
    factors: tuple[str, ...]
    bump_bp: float
    gradient_rub_per_bp: tuple[float, ...]
    hessian_rub_per_bp2: tuple[tuple[float, ...], ...]
    model_version: str = REPO_SECOND_ORDER_MODEL_VERSION


@dataclass(frozen=True)
class RepoCarry:
    """Carry from the valuation date to ``horizon_date`` (signed for the position).

    - ``interest_carry_rub``: repo interest accrued over ``(valuation, horizon]`` (to
      the end date at most) on known fixings and today's projections: the accrual
      P&L of the leg;
    - ``received_rub``: flows paid in ``(valuation, horizon]``;
    - ``pv_forward_rub``: the remaining flows at the horizon on today's discount curve
      read forward, ``DF(t) / DF(horizon)``;
    - ``pv_carry_rub = pv_forward + received - pv_start`` (today's forwards realised).
    """

    base_id: str
    trade_id: str
    valuation_date: date
    horizon_date: date
    accrued_start_rub: float
    accrued_horizon_rub: float
    interest_carry_rub: float
    received_rub: float
    pv_start_rub: float
    pv_forward_rub: float
    pv_carry_rub: float
    model_version: str = REPO_MODEL_VERSION


# --- contract helpers ------------------------------------------------------------


def repo_start_amount(
    quantity: float, dirty_price_rub_per_bond: float, discount_pct: float, *, decimals: int = 2
) -> float:
    """MOEX Trading Rules, Part III, 1.5.8.2: ``S = (1 - D / 100) * Q * (P0 + a0)``.

    ``dirty_price_rub_per_bond`` is ``P0 + a0`` (price plus accrued, RUB per bond);
    the amount is rounded half-up to ``decimals`` ("с точностью до сотых").
    """
    qty = positive_money(quantity, "quantity")
    price = positive_money(dirty_price_rub_per_bond, "dirty price")
    discount = finite_money(discount_pct, "discount pct")
    if not 0.0 <= discount < 100.0:
        raise InvalidTerms("the discount must be in [0, 100) percent")
    places = _decimals(decimals, "decimals")
    assert places is not None
    return _round_half_up((1.0 - discount / 100.0) * qty * price, places)


def repo_initial_discount_pct(
    start_amount_rub: float, quantity: float, dirty_price_rub_per_bond: float
) -> float:
    """MOEX Trading Rules, Part III, 1.5.8.2: ``D = (1 - S / (Q * (P0 + a0))) * 100``."""
    amount = positive_money(start_amount_rub, "start amount")
    value = positive_money(quantity, "quantity") * positive_money(
        dirty_price_rub_per_bond, "dirty price"
    )
    return (1.0 - amount / value) * 100.0


def repo_end_amount(
    start_amount_rub: float,
    rate: float,
    start_date: date,
    end_date: date,
    day_basis: RepoDayBasis,
    *,
    decimals: int | None,
) -> float:
    """Fixed-rate repurchase amount, MOEX Trading Rules, Part III, 1.5.8.5:
    ``S2 = S1 * (1 + R * yf)``, ``yf = T365 / 365 + T366 / 366`` on ``ACT/365-366``."""
    amount = positive_money(start_amount_rub, "start amount")
    if end_date <= start_date:
        raise InvalidTerms("the end date must follow the start date")
    value = amount * (
        1.0 + finite_money(rate, "repo rate") * day_basis.year_fraction(start_date, end_date)
    )
    places = _decimals(decimals, "decimals")
    return value if places is None else _round_half_up(value, places)


# --- engine -----------------------------------------------------------------------


@dataclass(frozen=True)
class _Observation:
    day: date
    following: date
    tau: float
    known: float | None


@dataclass(frozen=True)
class _FastPlan:
    """Precomputed amounts: see :meth:`RepoRiskEngine._fast_plan`."""

    constant: float
    weights: tuple[tuple[int, float], ...]
    periods: tuple[tuple[tuple[int, float], ...], ...]
    known_end: float | None
    known_interest: float
    known_rates: tuple[float | None, ...]
    projected: tuple[int, ...]
    # (observation index, memo key or -1, observation, next observation, tau)
    steps: tuple[tuple[int, int, date, date, float], ...] = ()


@dataclass(frozen=True)
class _Period:
    """Days ``(previous settlement day, end]``; groups are (observation, weight sum)."""

    end: date
    groups: tuple[tuple[int, float], ...]
    known: bool


def _previous_business_day(calendar: BusinessCalendar, value: date) -> date:
    current = value
    while not calendar.is_business_day(current):
        current -= timedelta(days=1)
    return current


def _history_identity(history: FixingHistory) -> _Canonical:
    cached = history.__dict__.get("_repo_identity_canonical")
    if cached is None:
        cached = _precanonical(history.identity)
        object.__setattr__(history, "_repo_identity_canonical", cached)
    return cast(_Canonical, cached)


@dataclass(frozen=True)
class RepoRiskEngine:
    """Value a repo once, then reprice it under curve shocks with known fixings frozen.

    PV is ``sign * sum(a_j * DF(t_j))`` over the remaining flows: the start leg (if
    after the valuation date), the end leg and the explicit cash adjustments. Only the
    end amount of a floating repo depends on the projection curve. Share one
    ``curve_cache`` across engines on the same OIS quotes.
    """

    request: RepoRiskRequest
    curve_cache: ShockedCurveCache = field(
        default_factory=ShockedCurveCache, compare=False, repr=False
    )
    base_id: str = field(init=False)
    base_pv: float = field(init=False)
    _discount: DiscountCurve = field(init=False, repr=False)
    _projection: DiscountCurve | None = field(init=False, repr=False)
    _observations: tuple[_Observation, ...] = field(init=False, repr=False)
    _periods: tuple[_Period, ...] = field(init=False, repr=False)
    _flows: tuple[tuple[RepoFlowKind, date, float | None], ...] = field(init=False, repr=False)
    _fast: _FastPlan = field(init=False, repr=False)
    _memo: dict[tuple[str, _Vector], _Vector] = field(
        init=False, repr=False, compare=False, default_factory=dict
    )

    def __post_init__(self) -> None:
        req = self.request
        if not isinstance(req, RepoRiskRequest):
            raise TypeError("request must be a RepoRiskRequest")
        if not isinstance(self.curve_cache, ShockedCurveCache):
            raise TypeError("curve_cache must be a ShockedCurveCache")
        trade = req.trade
        valuation = req.valuation_date
        end = req.end_date
        if end <= valuation:
            raise InvalidTerms("the repo has settled: its end is on or before the valuation date")
        object.__setattr__(self, "_discount", _base_curve(req.discount_curve, self.curve_cache))
        projection = req.projection_curve
        object.__setattr__(
            self,
            "_projection",
            None if projection is None else _base_curve(projection, self.curve_cache),
        )
        observations, periods = self._plan(end)
        object.__setattr__(self, "_observations", observations)
        object.__setattr__(self, "_periods", periods)
        flows: list[tuple[RepoFlowKind, date, float | None]] = []
        if trade.start_date > valuation:
            flows.append(("start_leg", trade.start_date, -trade.start_amount_rub))
        flows.append(("end_leg", end, None))
        flows.extend(
            ("adjustment", a.payment_date, a.amount_rub)
            for a in trade.cash_adjustments
            if a.payment_date > valuation
        )
        object.__setattr__(self, "_flows", tuple(flows))
        object.__setattr__(self, "_fast", self._fast_plan())
        self._check_projected_rates()
        object.__setattr__(self, "base_pv", self._pv(_ZERO_SHOCK, _ZERO_SHOCK))
        object.__setattr__(
            self,
            "base_id",
            canonical_sha256(
                {
                    "model": REPO_MODEL_VERSION,
                    "trade": trade,
                    "calendar": (req.calendar.version, calendar_fingerprint(req.calendar)),
                    "assumed_end_date": req.assumed_end_date,
                    "discount_curve_basis": self.discount_curve_basis,
                    "discount_curve": _curve_identity(req.discount_curve),
                    "projection_curve_basis": self.projection_curve_basis,
                    "projection_curve": None
                    if req.projection_curve is None
                    else _curve_identity(req.projection_curve),
                    "fixings": None if req.fixings is None else _history_identity(req.fixings),
                    "collateral_price": req.collateral_dirty_price_rub_per_bond,
                }
            ),
        )

    # --- plan ----------------------------------------------------------------------

    def _observation_day(self, day: date, rate: FloatingRepoRate) -> tuple[date, date]:
        """Observation date of interest day ``day`` and the next observation date."""
        history = cast(FixingHistory, self.request.fixings)
        if rate.observation == "calendar_day":
            return day, day + timedelta(days=1)
        settlement = _previous_business_day(self.request.calendar, day - timedelta(days=1))
        observed = _previous_business_day(history.calendar, settlement)
        return observed, history.calendar.add_business_days(observed, 1)

    def _plan(self, end: date) -> tuple[tuple[_Observation, ...], tuple[_Period, ...]]:
        req = self.request
        trade = req.trade
        rate = trade.rate
        valuation = req.valuation_date
        basis = rate.day_basis
        observations: list[_Observation] = []
        index_of: dict[date, int] = {}
        periods: list[_Period] = []
        groups: dict[int, float] = {}
        day = trade.start_date + timedelta(days=1)
        while day <= end:
            if isinstance(rate, FixedRepoRate):
                position = -1
            else:
                observed, following = self._observation_day(day, rate)
                position = index_of.get(observed, -2)
                if position == -2:
                    known: float | None = None
                    if observed < valuation:
                        history = cast(FixingHistory, req.fixings)
                        try:
                            known, _ = history.rate(observed)
                        except LookupError as exc:
                            raise MissingMarketData(str(exc)) from None
                    tau = rate.projection_day_count.year_fraction(observed, following)
                    observations.append(_Observation(observed, following, tau, known))
                    position = index_of[observed] = len(observations) - 1
            groups[position] = groups.get(position, 0.0) + basis.day_weight(day)
            if req.calendar.is_business_day(day):
                periods.append(
                    _Period(
                        day,
                        tuple(groups.items()),
                        all(i < 0 or observations[i].known is not None for i in groups),
                    )
                )
                groups = {}
            day += timedelta(days=1)
        return tuple(observations), tuple(periods)

    def _all_in(self, benchmark: float, known: bool) -> float:
        rate = cast(FloatingRepoRate, self.request.trade.rate)
        value = benchmark + rate.spread
        replacement = rate.nonpositive_rate_replacement
        if replacement is not None and value <= 0.0:
            if not known:
                raise UnsupportedProduct(
                    "a projected repo rate is not positive under the nonpositive-rate "
                    "replacement rule; its option value is not modelled"
                )
            return replacement
        return value

    def _fast_plan(self) -> _FastPlan:
        """Everything the fixed rate and the known fixings determine, computed once.

        Simple interest is linear in the rates: the fixed and known contributions are
        summed here, and the projected observations keep their summed day weights.
        Capitalised interest credits the known prefix of settlement periods here
        (rounded); the projected periods are compounded on every pricing.
        """
        trade = self.request.trade
        rate = trade.rate
        fixed = rate.rate if isinstance(rate, FixedRepoRate) else 0.0
        known_rates = tuple(
            None if o.known is None else self._all_in(o.known, True) for o in self._observations
        )
        start = trade.start_amount_rub
        decimals = trade.amount_rounding_decimals

        def value(index: int) -> float | None:
            return fixed if index < 0 else known_rates[index]

        if rate.interest == "simple":
            known_terms: list[float] = []
            projected: dict[int, float] = {}
            for period in self._periods:
                for index, weight in period.groups:
                    known = value(index)
                    if known is None:
                        projected[index] = projected.get(index, 0.0) + weight
                    else:
                        known_terms.append(known * weight)
            constant = math.fsum(known_terms)
            known_end = None
            if not projected:
                known_end = start * (1.0 + constant)
                if decimals is not None:
                    known_end = _round_half_up(known_end, decimals)
            return _FastPlan(
                constant,
                tuple(projected.items()),
                (),
                known_end,
                start * constant,
                known_rates,
                tuple(projected),
                self._steps(tuple(projected)),
            )
        balance = start
        position = 0
        for period in self._periods:
            if not period.known:
                break
            credited = balance * math.fsum(cast(float, value(i)) * w for i, w in period.groups)
            if decimals is not None:
                credited = _round_half_up(credited, decimals)
            balance += credited
            position += 1
        remaining = self._periods[position:]
        known_end = None
        if not remaining:
            known_end = balance if decimals is None else _round_half_up(balance, decimals)
        needed = sorted({i for p in remaining for i, _ in p.groups if i >= 0})
        return _FastPlan(
            balance,
            (),
            tuple(p.groups for p in remaining),
            known_end,
            balance - start,
            known_rates,
            tuple(needed),
            self._steps(tuple(needed)),
        )

    def _steps(self, indexes: tuple[int, ...]) -> tuple[tuple[int, int, date, date, float], ...]:
        steps = []
        for index in indexes:
            obs = self._observations[index]
            memoable = (obs.following - obs.day).days < 64
            key = _forward_key(obs.day, obs.following) if memoable else -1
            steps.append((index, key, obs.day, obs.following, obs.tau))
        return tuple(steps)

    def _check_projected_rates(self) -> None:
        if isinstance(self.request.trade.rate, FloatingRepoRate):
            self._rates(self._projection_view(_ZERO_SHOCK, _ZERO_SHOCK))

    # --- pricing -------------------------------------------------------------------

    @property
    def discount_curve_basis(self) -> str:
        return _curve_basis(self.request.discount_curve)

    @property
    def projection_curve_basis(self) -> str:
        if isinstance(self.request.trade.rate, FixedRepoRate):
            return "fixed_rate"
        projection = self.request.projection_curve
        return "single_curve" if projection is None else _curve_basis(projection)

    @property
    def valuation_date(self) -> date:
        return self.request.valuation_date

    def _discount_view(self, shifts: _Vector) -> DiscountFactorCurve:
        return _shock_curve(self.request.discount_curve, self._discount, shifts, self.curve_cache)

    def _projection_view(
        self, discount_shifts: _Vector, projection_shifts: _Vector
    ) -> DiscountFactorCurve:
        source = self.request.projection_curve
        if source is None:
            return self._discount_view(discount_shifts)
        base = cast(DiscountCurve, self._projection)
        return _shock_curve(source, base, projection_shifts, self.curve_cache)

    def _forwards(self, view: DiscountFactorCurve) -> dict[int, float]:
        """All-in rates of the projected observations on ``view``.

        Overnight forwards ``(DF(o) / DF(next) - 1) / tau`` are memoised on the view
        (shared by every position on the same shocked curve), with the keys and the
        formula of :mod:`rub_fixed_income.floating`, so the floats are identical.
        """
        rate = cast(FloatingRepoRate, self.request.trade.rate)
        spread = rate.spread
        replacement = rate.nonpositive_rate_replacement
        memo = _forward_memo(view, rate.projection_day_count)
        get = memo.get
        out: dict[int, float] = {}
        for index, key, day, following, tau in self._fast.steps:
            forward = get(key) if key >= 0 else None
            if forward is None:
                ratio = view.discount_factor(day) / view.discount_factor(following)
                forward = (ratio - 1.0) / tau
                if not math.isfinite(forward):
                    raise ValueError("projection curve produced a non-finite overnight forward")
                if key >= 0 and len(memo) < _FORWARD_MEMO_SIZE:
                    memo[key] = forward
            value = forward + spread
            if replacement is not None and value <= 0.0:
                self._all_in(forward, False)  # raises the typed rejection
            out[index] = value
        return out

    def _rates(self, view: DiscountFactorCurve | None) -> _Vector:
        """All-in rate (benchmark + spread) of every observation."""
        forwards = {} if view is None or not self._fast.projected else self._forwards(view)
        return tuple(
            forwards[i] if known is None else known
            for i, known in enumerate(self._fast.known_rates)
        )

    def _accumulate(
        self, rates: Sequence[float], periods: Sequence[_Period]
    ) -> tuple[float, float, bool]:
        """Interest over ``periods``, its known part and whether all of it is known."""
        trade = self.request.trade
        fixed = trade.rate.rate if isinstance(trade.rate, FixedRepoRate) else 0.0
        decimals = trade.amount_rounding_decimals
        start = trade.start_amount_rub

        def period_rate(period: _Period) -> float:
            return math.fsum((fixed if i < 0 else rates[i]) * w for i, w in period.groups)

        known_all = all(p.known for p in periods)
        if trade.rate.interest == "simple":
            interest = start * math.fsum(period_rate(p) for p in periods)
            known = start * math.fsum(period_rate(p) for p in periods if p.known)
            return interest, known, known_all
        balance = start
        known_balance = start
        for period in periods:
            credited = balance * period_rate(period)
            if period.known and decimals is not None:
                credited = _round_half_up(credited, decimals)
            balance += credited
            if period.known:
                known_balance = balance
        return balance - start, known_balance - start, known_all

    def _end_amount(self, view: DiscountFactorCurve | None) -> float:
        """The repurchase amount on a projection view (fixed or known: a constant)."""
        fast = self._fast
        if fast.known_end is not None:
            return fast.known_end
        assert view is not None
        forwards = self._forwards(view)
        start = self.request.trade.start_amount_rub
        if fast.weights:
            terms = [fast.constant, *(forwards[i] * w for i, w in fast.weights)]
            return finite_money(start * (1.0 + math.fsum(terms)), "repurchase amount")
        balance = fast.constant
        for groups in fast.periods:
            balance += balance * math.fsum(forwards[i] * w for i, w in groups)
        return finite_money(balance, "repurchase amount")

    def _cached(self, key: tuple[str, _Vector], build: Callable[[], _Vector]) -> _Vector:
        cached = self._memo.get(key)
        if cached is not None:
            return cached
        value = build()
        if len(self._memo) < _MEMO_SIZE:
            self._memo[key] = value
        return value

    def _vectors(
        self, discount_shifts: _Vector, projection_shifts: _Vector
    ) -> tuple[_Vector, _Vector]:
        """Unsigned flow amounts (lender view) and discount factors; memoized."""

        def discounts() -> _Vector:
            view = self._discount_view(discount_shifts)
            return tuple(view.discount_factor(d) for _, d, _ in self._flows)

        dfs = self._cached(("discount", discount_shifts), discounts)
        fixed_rate = isinstance(self.request.trade.rate, FixedRepoRate)
        if fixed_rate or self.request.single_curve:
            if any(projection_shifts):
                raise InvalidTerms("this repo has no separate projection curve to shift")
            key: tuple[str, _Vector] = ("single", () if fixed_rate else discount_shifts)
        else:
            key = ("projection", projection_shifts)

        def amounts() -> _Vector:
            view = (
                None
                if self._fast.known_end is not None
                else self._projection_view(discount_shifts, projection_shifts)
            )
            end = self._end_amount(view)
            return tuple(end if a is None else a for _, _, a in self._flows)

        return self._cached(key, amounts), dfs

    def _pv(self, discount_shifts: _Vector, projection_shifts: _Vector) -> float:
        try:
            amounts, dfs = self._vectors(discount_shifts, projection_shifts)
            value = self.request.trade.sign * math.fsum(
                a * d for a, d in zip(amounts, dfs, strict=True)
            )
        except (OverflowError, ZeroDivisionError) as exc:
            raise ValueError("shock is outside the supported numerical domain") from exc
        return finite_money(value, "repo PV")

    # --- results -------------------------------------------------------------------

    def _accrued(self, cutoff: date, rates: Sequence[float]) -> tuple[float, bool]:
        """Interest of the days ``(start, cutoff]`` (capped at the end date) and
        whether every rate behind it is fixed or known."""
        trade = self.request.trade
        if cutoff <= trade.start_date:
            return 0.0, True
        last = min(cutoff, self.request.end_date)
        complete = [p for p in self._periods if p.end <= last]
        interest, _, known = self._accumulate(rates, complete)
        if complete and complete[-1].end == last:
            return interest, known
        # The open period: simple interest of its days up to the cutoff on the balance.
        rate = trade.rate
        begin = complete[-1].end if complete else trade.start_date
        partial = 0.0
        index_of = {o.day: i for i, o in enumerate(self._observations)}
        day = begin + timedelta(days=1)
        while day <= last:
            if isinstance(rate, FixedRepoRate):
                value = rate.rate
            else:
                observed, _ = self._observation_day(day, rate)
                position = index_of[observed]
                value = rates[position]
                known = known and self._observations[position].known is not None
            partial += value * rate.day_basis.day_weight(day)
            day += timedelta(days=1)
        base = trade.start_amount_rub
        if rate.interest == "capitalised":
            base += interest
        return interest + base * partial, known

    def _base_rates(self) -> _Vector:
        if isinstance(self.request.trade.rate, FixedRepoRate):
            return ()
        return self._rates(self._projection_view(_ZERO_SHOCK, _ZERO_SHOCK))

    def valuation(self) -> RepoValuation:
        """PV, contract amounts, accrued interest, flows and the collateral reference."""
        req = self.request
        trade = req.trade
        sign = trade.sign
        amounts, dfs = self._vectors(_ZERO_SHOCK, _ZERO_SHOCK)
        rates = self._base_rates()
        end_amount = next(
            a for (k, _, _), a in zip(self._flows, amounts, strict=True) if k == "end_leg"
        )
        interest = end_amount - trade.start_amount_rub
        known = self._fast.known_interest
        if isinstance(trade.rate, FixedRepoRate):
            source: AmountSource = "fixed"
        elif self._fast.known_end is not None:
            source = "known"
        elif any(o.known is not None for o in self._observations):
            source = "partially_projected"
        else:
            source = "projected"
        rows = []
        for (kind, day, _), amount, df in zip(self._flows, amounts, dfs, strict=True):
            flow_source: AmountSource = source if kind == "end_leg" else "fixed"
            rows.append(RepoCashflow(kind, day, sign * amount, flow_source, df, sign * amount * df))
        accrued, accrued_known = self._accrued(req.valuation_date, rates)
        current = trade.start_amount_rub + accrued
        decimals = trade.amount_rounding_decimals
        if accrued_known and decimals is not None:
            current = _round_half_up(current, decimals)
            accrued = current - trade.start_amount_rub
        collateral = trade.collateral
        reference: RepoCollateralReference | None = None
        price_per_bond: float | None = None
        if collateral is not None:
            price_per_bond = end_amount / collateral.quantity
            dirty = req.collateral_dirty_price_rub_per_bond
            value = None if dirty is None else collateral.quantity * dirty
            reference = RepoCollateralReference(
                collateral.instrument_id,
                collateral.quantity,
                collateral.pool,
                collateral.initial_discount_pct,
                dirty,
                value,
                current,
                None if value is None else current - value,
                None if value is None else value / current,
            )

        def leg(kind: str) -> float:
            return finite_money(math.fsum(r.pv_rub for r in rows if r.kind == kind), f"{kind} PV")

        return RepoValuation(
            self.base_id,
            trade.trade_id,
            trade.side,
            req.valuation_date,
            req.end_date,
            trade.is_open,
            self.discount_curve_basis,
            self.projection_curve_basis,
            trade.start_amount_rub,
            end_amount,
            source,
            finite_money(interest, "interest"),
            finite_money(known, "known interest"),
            finite_money(accrued, "accrued interest"),
            finite_money(current, "current repurchase amount"),
            price_per_bond,
            self.base_pv,
            leg("start_leg"),
            leg("end_leg"),
            leg("adjustment"),
            tuple(rows),
            reference,
        )

    def _roles(self) -> tuple[RepoCurveRole, ...]:
        if isinstance(self.request.trade.rate, FixedRepoRate):
            return ("discount",)
        if self.request.single_curve:
            return ("discount_and_projection",)
        return ("discount", "projection")

    def _moves(self, role: RepoCurveRole, shifts: _Vector) -> tuple[_Vector, _Vector]:
        return (_ZERO_SHOCK, shifts) if role == "projection" else (shifts, _ZERO_SHOCK)

    def risk(
        self, *, bump_bp: float = 1.0, difference: Literal["forward", "central"] = "central"
    ) -> RepoRiskResult:
        """Bucket and parallel DV01 per curve role; central by default (DEC08)."""
        bump = positive_money(bump_bp, "risk bump bp")
        if difference not in ("forward", "central"):
            raise ValueError("difference must be forward or central")
        notional = self.request.trade.start_amount_rub
        size = len(RISK_BUCKETS)

        def sensitivity(role: RepoCurveRole, shifts: _Vector) -> float:
            up = self._pv(*self._moves(role, shifts))
            if difference == "central":
                down = self._pv(*self._moves(role, tuple(-s for s in shifts)))
                return finite_money((up - down) / (2.0 * bump), "DV01")
            return finite_money((up - self.base_pv) / bump, "DV01")

        curves = []
        for role in self._roles():
            buckets = []
            for i, bucket in enumerate(RISK_BUCKETS):
                dv01 = sensitivity(role, tuple(bump if i == j else 0.0 for j in range(size)))
                buckets.append(BucketRisk(bucket, dv01, -dv01 / notional * 10_000.0))
            basis = (
                self.projection_curve_basis if role == "projection" else self.discount_curve_basis
            )
            curves.append(
                RepoCurveRisk(
                    role,
                    basis,
                    tuple(buckets),
                    finite_money(math.fsum(b.dv01_rub for b in buckets), "curve DV01"),
                    sensitivity(role, (bump,) * size),
                )
            )
        trade = self.request.trade
        return RepoRiskResult(
            self.base_id,
            trade.trade_id,
            trade.side,
            difference,
            bump,
            notional,
            self.base_pv,
            tuple(curves),
            finite_money(math.fsum(c.dv01_rub for c in curves), "DV01"),
        )

    def reprice(self, scenario: RepoScenario) -> RepoScenarioResult:
        """Exact repricing, each curve isolated, and their interaction."""
        if not isinstance(scenario, RepoScenario):
            raise TypeError("scenario must be a RepoScenario")
        discount, projection = scenario.discount_shifts_bp, scenario.projection_shifts_bp
        shocked = self._pv(discount, projection)
        discount_pnl = self._pv(discount, _ZERO_SHOCK) - self.base_pv
        projection_pnl = self._pv(_ZERO_SHOCK, projection) - self.base_pv
        total = shocked - self.base_pv
        return RepoScenarioResult(
            self.base_id,
            canonical_sha256(scenario),
            self.base_pv,
            shocked,
            finite_money(discount_pnl, "discount P&L"),
            finite_money(projection_pnl, "projection P&L"),
            finite_money(total - discount_pnl - projection_pnl, "interaction P&L"),
            finite_money(total, "total P&L"),
        )

    def second_order(self, *, bump_bp: float = 1.0) -> RepoSecondOrderRisk:
        """Full central gradient and Hessian over the curve buckets (11 or 22 factors).

        Mixed discount/projection points combine memoized single-curve vectors, so
        the 22-factor scheme costs no more curve reads than two 11-factor ones.
        """
        bump = positive_money(bump_bp, "second-order bump bp")
        size = len(RISK_BUCKETS)
        roles = self._roles()
        two = len(roles) == 2

        def price(moves: list[float]) -> float:
            if two:
                return self._pv(tuple(moves[:size]), tuple(moves[size:]))
            return self._pv(tuple(moves), _ZERO_SHOCK)

        gradient, hessian = _central_derivatives(price, size * len(roles), bump, self.base_pv, 1.0)
        labels = {
            "discount_and_projection": "curve",
            "discount": "discount",
            "projection": "projection",
        }
        return RepoSecondOrderRisk(
            self.base_id,
            roles,
            tuple(f"{labels[role]} {b}" for role in roles for b in RISK_BUCKETS),
            bump,
            gradient,
            hessian,
        )

    def carry(self, horizon_date: date) -> RepoCarry:
        """Accrual carry and forward-PV carry to ``horizon_date`` (see :class:`RepoCarry`)."""
        valuation = self.valuation_date
        if not isinstance(horizon_date, date) or horizon_date <= valuation:
            raise InvalidTerms("horizon date must be after the valuation date")
        trade = self.request.trade
        sign = trade.sign
        rates = self._base_rates()
        accrued_start, _ = self._accrued(valuation, rates)
        accrued_end, _ = self._accrued(horizon_date, rates)
        amounts, dfs = self._vectors(_ZERO_SHOCK, _ZERO_SHOCK)
        forward_df = self._discount.discount_factor(horizon_date)
        received = sign * math.fsum(
            a for (_, d, _), a in zip(self._flows, amounts, strict=True) if d <= horizon_date
        )
        pv_forward = sign * math.fsum(
            a * df / forward_df
            for (_, d, _), a, df in zip(self._flows, amounts, dfs, strict=True)
            if d > horizon_date
        )
        return RepoCarry(
            self.base_id,
            trade.trade_id,
            valuation,
            horizon_date,
            finite_money(sign * accrued_start, "accrued at start"),
            finite_money(sign * accrued_end, "accrued at horizon"),
            finite_money(sign * (accrued_end - accrued_start), "interest carry"),
            finite_money(received, "received"),
            self.base_pv,
            finite_money(pv_forward, "forward PV"),
            finite_money(pv_forward + received - self.base_pv, "PV carry"),
        )


def repo_carry(
    request: RepoRiskRequest,
    *,
    horizon_date: date,
    curve_cache: ShockedCurveCache | None = None,
) -> RepoCarry:
    """Carry of a repo leg to ``horizon_date``."""
    engine = RepoRiskEngine(request, ShockedCurveCache() if curve_cache is None else curve_cache)
    return engine.carry(horizon_date)


def repo_factor_risk(
    risk: RepoRiskResult,
    *,
    discount_curve_id: str,
    projection_curve_id: str | None = None,
    second_order: RepoSecondOrderRisk | None = None,
) -> FactorRisk:
    """Map repo risk to desk factors ``IR|<curve_id>|<bucket>``.

    A fixed or single-curve repo has one curve ID (``projection_curve_id`` None or
    equal); a two-curve floating repo needs two distinct IDs, discount first. With
    ``second_order`` the first order is its central gradient (same bump as the
    Hessian); otherwise ``risk`` must be central (DEC08).
    """
    roles = tuple(c.curve_role for c in risk.curves)
    if len(roles) == 1:
        if projection_curve_id not in (None, discount_curve_id):
            raise InvalidTerms("a fixed or single-curve repo has one curve ID")
        ids: tuple[str, ...] = (discount_curve_id,)
    else:
        if projection_curve_id is None or projection_curve_id == discount_curve_id:
            raise InvalidTerms("a two-curve repo needs distinct discount and projection curve IDs")
        ids = (discount_curve_id, projection_curve_id)
    factors = tuple(ir_factor(curve_id, b) for curve_id in ids for b in RISK_BUCKETS)
    if second_order is not None:
        if second_order.base_id != risk.base_id:
            raise InvalidTerms("risk and second-order results describe different repos or markets")
        if second_order.curve_roles != roles:
            raise InvalidTerms("unexpected second-order factor set")
        return FactorRisk(
            factors,
            second_order.gradient_rub_per_bp,
            _upper(second_order.hessian_rub_per_bp2),
            "central",
            second_order.bump_bp,
            True,
        )
    if risk.difference != "central":
        raise InvalidTerms("desk factors need central-difference risk (DEC08)")
    first = tuple(b.dv01_rub for c in risk.curves for b in c.buckets)
    return FactorRisk(factors, first, (), "central", risk.bump_bp, False)
