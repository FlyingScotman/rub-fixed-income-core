"""Seasoned RUB interest-rate swaps: RUONIA OIS and key-rate swaps (roadmap package 2).

A swap exchanges a fixed leg for a floating leg on one constant RUB notional. Its
schedules come from an explicit :class:`SwapConvention` (or one derived from a
registered curve spec with :func:`swap_convention`), its known resets from a
point-in-time :class:`~rub_fixed_income.floating.FixingHistory`, and its value
from a discount curve and a projection curve:

- **RUONIA OIS** (``index="ruonia"``, compounded): a float coupon is
  ``prod(1 + r_i * tau_i) - 1`` over the business-day RUONIA fixings of the
  accrual period. Observations before the valuation date come from the history;
  the rest are the compounded overnight forwards of the projection curve,
  ``DF(start) / DF(end)``. Without a separate projection curve the discount curve
  projects (single curve).
- **Key-rate swap** (``index="key_rate"``, arithmetic): a float coupon is the
  calendar-day average of the key rate over the period times the float accrual
  factor, as in :mod:`rub_fixed_income.key_rate`. Days before the valuation date
  take the key rate in force from the history; later days come from a
  :class:`~rub_fixed_income.key_rate.KeyRateForwardCurve`.

The valuation date is the curves' date. Flows paid on or before it are settled
and excluded. Amounts are signed RUB for the position: a payer pays fixed and
receives float. Known fixings never move under curve shocks.
"""

from __future__ import annotations

import math
from calendar import isleap
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from itertools import pairwise
from typing import Literal, cast

import numpy as np

from ._curve_views import roll_curve, roll_key_rate_curve
from ._money import finite_money, positive_money
from ._risk_common import (
    _ZERO_SHOCK,
    RISK_BUCKETS,
    RISK_PILLARS,
    RiskCurve,
    ShockedCurveCache,
    _base_curve,
    _central_derivatives,
    _check_scenario_shifts,
    _curve_basis,
    _curve_identity,
    _shifts,
    _shock_curve,
    _snapshot_curve,
)
from .canonical import OMIT_IF_DEFAULT, _Canonical, _precanonical, canonical_sha256
from .conventions import (
    BusinessDayConvention,
    DayCount,
    _ruonia_ois_compound_act,
    adjust_date,
    calendar_fingerprint,
)
from .curve_specs import CurveKind, get_curve_spec
from .dates import BusinessCalendar, add_months, annual_schedule
from .discount import CurveBootstrapRequest, DiscountCurve, DiscountFactorCurve
from .errors import InvalidTerms, MissingMarketData, UnsupportedProduct
from .factors import FactorRisk, HessianEntry, ir_factor
from .floating import FixingHistory
from .key_rate import KeyRateForwardCurve
from .risk import BucketRisk
from .tenors import parse_tenor, resolve_tenor

__all__ = [
    "SWAP_HORIZON_MODEL_VERSION",
    "SWAP_MODEL_VERSION",
    "SWAP_SECOND_ORDER_MODEL_VERSION",
    "InterestRateSwap",
    "SwapCashflow",
    "SwapConvention",
    "SwapCurveRisk",
    "SwapHorizonValuation",
    "SwapLegSchedule",
    "SwapPeriod",
    "SwapRiskEngine",
    "SwapRiskRequest",
    "SwapRiskResult",
    "SwapScenario",
    "SwapScenarioResult",
    "SwapSchedule",
    "SwapSecondOrderRisk",
    "SwapValuation",
    "swap_convention",
    "swap_factor_risk",
    "swap_horizon",
    "swap_schedule",
]

SWAP_MODEL_VERSION = "rub-irs-ois-krs-v1"
SWAP_SECOND_ORDER_MODEL_VERSION = "rub-irs-ois-krs-second-order-v1"
SWAP_HORIZON_MODEL_VERSION = "rub-irs-horizon-carry-v2"

SwapSide = Literal["payer", "receiver"]
SwapIndex = Literal["ruonia", "key_rate"]
SwapAveraging = Literal["compounded", "arithmetic"]
ScheduleRoll = Literal["forward", "backward", "legacy-annual-forward"]
SwapLeg = Literal["fixed", "float"]
FlowSource = Literal["fixed", "known", "partially_projected", "projected"]
CurveRole = Literal["discount", "projection", "discount_and_projection"]
ProjectionCurve = DiscountCurve | CurveBootstrapRequest | KeyRateForwardCurve
FloatCompounding = Literal["rate_day_count", "nfa_act"]

_KEY_RATE_SPEC = "rub_key_rate_swap_v1"
_LEGACY_OIS_SPEC = "rub_ruonia_ois_legacy_v1"
_MOEX_OIS_SPEC = "rub_ruonia_ois_moex_v2"
_MEMO_SIZE = 4096


def _count(value: object, label: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise InvalidTerms(f"{label} must be an integer >= {minimum}")
    return value


@dataclass(frozen=True)
class SwapLegSchedule:
    """How one leg's periods are generated.

    Unadjusted roll dates run every ``frequency_months`` ``forward`` from the
    effective date or ``backward`` from the unadjusted termination date (month ends
    clamp); every date, the termination included, is then adjusted with the
    swap's business-day convention. ``legacy-annual-forward`` is
    :func:`rub_fixed_income.dates.annual_schedule` exactly (the legacy OIS
    bootstrap's fixed leg: iterated 12-month roll, short end stub). With
    ``single_period_up_to_months`` a swap whose unadjusted termination is no later
    than that many months after the effective date has one period (KEYRATE fixed
    leg up to 1Y). ``day_count`` gives each period's accrual factor.
    """

    frequency_months: int
    roll: ScheduleRoll
    day_count: DayCount
    single_period_up_to_months: int | None = None

    def __post_init__(self) -> None:
        _count(self.frequency_months, "frequency_months", minimum=1)
        if self.roll not in ("forward", "backward", "legacy-annual-forward"):
            raise InvalidTerms("roll must be forward, backward or legacy-annual-forward")
        if self.roll == "legacy-annual-forward" and self.frequency_months != 12:
            raise InvalidTerms("the legacy annual schedule has a 12-month frequency")
        if not isinstance(self.day_count, DayCount):
            raise TypeError("day_count must be a DayCount")
        if self.single_period_up_to_months is not None:
            _count(self.single_period_up_to_months, "single_period_up_to_months", minimum=1)

    @property
    def identifier(self) -> str:
        single = (
            ""
            if self.single_period_up_to_months is None
            else f":single<={self.single_period_up_to_months}M"
        )
        return f"{self.frequency_months}M:{self.roll}:{self.day_count.value}{single}"


@dataclass(frozen=True)
class SwapConvention:
    """Every convention of a swap, explicit. Nothing is defaulted or inferred.

    ``rate_day_count`` weights each daily RUONIA fixing in the compounded return
    (RUONIA is an ACT/365 rate); arithmetic key-rate averages are calendar-day
    averages and take ``None``. ``spot_lag_days`` only resolves trade date and
    tenor in :meth:`InterestRateSwap.from_tenor`. Payments are
    ``payment_lag_days`` business days after each accrual end. ``spec_id`` records
    the registered curve spec the convention was derived from.

    ``float_compounding="nfa_act"`` is NFA ``RUB-RUONIA-OIS-COMPOUND`` (amendments
    No. 1, 2021, 8.4(б)): each fixing weighs ``n_i / Act`` with ``Act`` = 366 if any day of
    the period is in a leap year, else 365, and the float amount is
    ``(growth - 1) * Act / d * float day count`` (``OisSwapConvention.float_amount_scale``);
    ``rate_day_count`` is then None. The default, ``"rate_day_count"``, weighs each fixing
    by ``rate_day_count`` and pays ``growth - 1``; it is left out of the identity.
    """

    index: SwapIndex
    averaging: SwapAveraging
    fixed_leg: SwapLegSchedule
    float_leg: SwapLegSchedule
    rate_day_count: DayCount | None
    business_day_convention: BusinessDayConvention
    spot_lag_days: int
    payment_lag_days: int
    spec_id: str | None = None
    float_compounding: FloatCompounding = field(default="rate_day_count", metadata=OMIT_IF_DEFAULT)

    def __post_init__(self) -> None:
        if self.float_compounding not in ("rate_day_count", "nfa_act"):
            raise InvalidTerms("float_compounding must be rate_day_count or nfa_act")
        if self.float_compounding == "nfa_act":
            if self.averaging != "compounded":
                raise InvalidTerms("NFA RUONIA compounding applies to compounded RUONIA swaps")
            if self.rate_day_count is not None:
                raise InvalidTerms("NFA RUONIA compounding takes no rate_day_count (Act rule)")
        if self.index not in ("ruonia", "key_rate"):
            raise InvalidTerms("swap index must be ruonia or key_rate")
        supported = {"ruonia": "compounded", "key_rate": "arithmetic"}
        if self.averaging not in ("compounded", "arithmetic"):
            raise InvalidTerms("averaging must be compounded or arithmetic")
        if supported[self.index] != self.averaging:
            raise UnsupportedProduct(
                f"{self.averaging} {self.index} swaps are not supported "
                "(RUONIA compounded and key-rate arithmetic are)"
            )
        for leg in (self.fixed_leg, self.float_leg):
            if not isinstance(leg, SwapLegSchedule):
                raise TypeError("swap legs need a SwapLegSchedule")
        if (
            self.averaging == "compounded"
            and self.float_compounding == "rate_day_count"
            and not isinstance(self.rate_day_count, DayCount)
        ):
            raise InvalidTerms("compounded averaging needs a rate_day_count")
        if self.averaging == "arithmetic" and self.rate_day_count is not None:
            raise InvalidTerms("an arithmetic calendar-day average takes no rate_day_count")
        if not isinstance(self.business_day_convention, BusinessDayConvention):
            raise TypeError("business_day_convention must be a BusinessDayConvention")
        _count(self.spot_lag_days, "spot_lag_days")
        _count(self.payment_lag_days, "payment_lag_days")
        if self.spec_id is not None and (not isinstance(self.spec_id, str) or not self.spec_id):
            raise InvalidTerms("spec_id must be None or a non-empty string")

    @property
    def identifier(self) -> str:
        rate = "-" if self.rate_day_count is None else self.rate_day_count.value
        return (
            f"swap:{self.index}:{self.averaging}:fixed={self.fixed_leg.identifier}:"
            f"float={self.float_leg.identifier}:rate={rate}:"
            f"bdc={self.business_day_convention.value}:spot={self.spot_lag_days}:"
            f"pay={self.payment_lag_days}:spec={self.spec_id or '-'}"
            + (":compound=nfa_act" if self.float_compounding == "nfa_act" else "")
        )


def swap_convention(spec_id: str) -> SwapConvention:
    """The swap convention of a registered swap-curve spec.

    ``rub_key_rate_swap_v1``: MOEX IRS KEYRATE (TOM start, modified following,
    ACT/ACT ISDA, annual fixed with a single payment up to 1Y, quarterly
    arithmetic key-rate average, backward roll from the unadjusted maturity, no
    payment lag), the schedule :func:`rub_fixed_income.key_rate.price_key_rate_swap`
    prices. ``rub_ruonia_ois_legacy_v1``: the legacy OIS bootstrap's swap (start on
    the curve date, legacy annual schedule on both legs, ACT/365F, unadjusted, no
    lags), whose par swaps the bootstrap reprices. ``rub_ruonia_ois_moex_v2``: the MOEX
    OIS RUONIA standard swap (TOM start, MODFOL, a single period up to 1Y and annual
    beyond with a short initial stub, Act/Act ISDA on both legs, +1 business-day payment
    lag, NFA ``RUB-RUONIA-OIS-COMPOUND`` float leg), whose par swaps the v2 bootstrap
    reprices. Other specs raise.
    """
    spec = get_curve_spec(spec_id)
    if spec.kind is CurveKind.KEY_RATE_SWAP and spec.spec_id == _KEY_RATE_SPEC:
        day_count = cast(DayCount, spec.fixed_leg_day_count)
        return SwapConvention(
            index="key_rate",
            averaging="arithmetic",
            fixed_leg=SwapLegSchedule(
                cast(int, spec.fixed_leg_frequency_months),
                "backward",
                day_count,
                single_period_up_to_months=12,
            ),
            float_leg=SwapLegSchedule(3, "backward", day_count),
            rate_day_count=None,
            business_day_convention=BusinessDayConvention(spec.business_day_convention),
            spot_lag_days=cast(int, spec.spot_lag_business_days),
            payment_lag_days=cast(int, spec.payment_lag_business_days),
            spec_id=spec.spec_id,
        )
    if spec.kind is CurveKind.OIS_BOOTSTRAP and spec.spec_id == _LEGACY_OIS_SPEC:
        day_count = cast(DayCount, spec.fixed_leg_day_count)
        leg = SwapLegSchedule(
            cast(int, spec.fixed_leg_frequency_months), "legacy-annual-forward", day_count
        )
        return SwapConvention(
            index="ruonia",
            averaging="compounded",
            fixed_leg=leg,
            float_leg=leg,
            rate_day_count=spec.time_day_count,
            business_day_convention=BusinessDayConvention(spec.business_day_convention),
            spot_lag_days=cast(int, spec.spot_lag_business_days),
            payment_lag_days=cast(int, spec.payment_lag_business_days),
            spec_id=spec.spec_id,
        )
    if spec.kind is CurveKind.OIS_BOOTSTRAP and spec.spec_id == _MOEX_OIS_SPEC:
        months = cast(int, spec.fixed_leg_frequency_months)
        fixed = SwapLegSchedule(
            months,
            "backward",
            cast(DayCount, spec.fixed_leg_day_count),
            single_period_up_to_months=12,
        )
        floating = SwapLegSchedule(
            months,
            "backward",
            cast(DayCount, spec.float_leg_day_count),
            single_period_up_to_months=12,
        )
        return SwapConvention(
            index="ruonia",
            averaging="compounded",
            fixed_leg=fixed,
            float_leg=floating,
            rate_day_count=None,
            business_day_convention=BusinessDayConvention(spec.business_day_convention),
            spot_lag_days=cast(int, spec.spot_lag_business_days),
            payment_lag_days=cast(int, spec.payment_lag_business_days),
            spec_id=spec.spec_id,
            float_compounding="nfa_act",
        )
    raise UnsupportedProduct(f"curve spec {spec_id!r} does not define a swap convention")


@dataclass(frozen=True)
class InterestRateSwap:
    """Terms of one swap; market-independent.

    ``notional`` is RUB (constant), ``fixed_rate`` and ``float_spread`` decimal
    annual. A ``payer`` pays fixed and receives float, a ``receiver`` the reverse.
    ``effective_date`` is the (adjusted) accrual start, ``termination_date`` the
    **unadjusted** end the schedule rolls from. ``float_spread`` is added simply
    over the float accrual factor, not compounded.
    """

    swap_id: str
    side: SwapSide
    notional: float
    fixed_rate: float
    effective_date: date
    termination_date: date
    convention: SwapConvention
    float_spread: float = 0.0
    currency: str = "RUB"

    def __post_init__(self) -> None:
        if not isinstance(self.swap_id, str) or not self.swap_id.strip():
            raise InvalidTerms("swap_id must be a non-empty string")
        if self.side not in ("payer", "receiver"):
            raise InvalidTerms("side must be payer or receiver")
        if self.currency != "RUB":
            raise UnsupportedProduct("only RUB swaps are supported (XCCY is package 7)")
        object.__setattr__(self, "notional", positive_money(self.notional, "swap notional"))
        object.__setattr__(self, "fixed_rate", finite_money(self.fixed_rate, "fixed rate"))
        object.__setattr__(self, "float_spread", finite_money(self.float_spread, "float spread"))
        for name in ("effective_date", "termination_date"):
            if not isinstance(getattr(self, name), date):
                raise TypeError(f"{name} must be a date")
        if self.termination_date <= self.effective_date:
            raise InvalidTerms("termination date must follow the effective date")
        if not isinstance(self.convention, SwapConvention):
            raise TypeError("convention must be a SwapConvention")

    @classmethod
    def from_tenor(
        cls,
        *,
        swap_id: str,
        side: SwapSide,
        notional: float,
        fixed_rate: float,
        trade_date: date,
        tenor: str,
        calendar: BusinessCalendar,
        convention: SwapConvention,
        float_spread: float = 0.0,
    ) -> InterestRateSwap:
        """A swap traded on ``trade_date`` for ``tenor``: spot start, unadjusted end."""
        if parse_tenor(tenor).unit in ("ON", "TN", "SN"):
            raise InvalidTerms(f"{tenor} is not a swap tenor")
        dates = resolve_tenor(
            trade_date,
            tenor,
            calendar,
            spot_lag_days=convention.spot_lag_days,
            business_day_convention=convention.business_day_convention,
        )
        return cls(
            swap_id,
            side,
            notional,
            fixed_rate,
            dates.effective_date,
            dates.unadjusted_end,
            convention,
            float_spread,
        )

    @property
    def fixed_sign(self) -> float:
        return -1.0 if self.side == "payer" else 1.0


@dataclass(frozen=True)
class SwapPeriod:
    accrual_start: date
    accrual_end: date
    payment_date: date
    accrual_factor: float


@dataclass(frozen=True)
class SwapSchedule:
    fixed: tuple[SwapPeriod, ...]
    floating: tuple[SwapPeriod, ...]


def _leg_periods(
    swap: InterestRateSwap, leg: SwapLegSchedule, calendar: BusinessCalendar
) -> tuple[SwapPeriod, ...]:
    start, end = swap.effective_date, swap.termination_date
    months = leg.frequency_months
    unadjusted: list[date]
    if leg.single_period_up_to_months is not None and end <= add_months(
        start, leg.single_period_up_to_months
    ):
        unadjusted = [end]
    elif leg.roll == "legacy-annual-forward":
        unadjusted = list(annual_schedule(start, end))
    elif leg.roll == "forward":
        unadjusted = []
        count = 1
        while (cursor := add_months(start, months * count)) < end:
            unadjusted.append(cursor)
            count += 1
        unadjusted.append(end)
    else:
        unadjusted = [end]
        count = 1
        while (cursor := add_months(end, -months * count)) > start:
            unadjusted.append(cursor)
            count += 1
        unadjusted.reverse()
    convention = swap.convention
    adjusted = [adjust_date(d, convention.business_day_convention, calendar) for d in unadjusted]
    if adjusted[0] <= start or any(left >= right for left, right in pairwise(adjusted)):
        raise InvalidTerms("adjusted swap schedule dates must be strictly increasing")
    periods = []
    previous = start
    for accrual_end in adjusted:
        payment = calendar.add_business_days(accrual_end, convention.payment_lag_days)
        periods.append(
            SwapPeriod(
                previous, accrual_end, payment, leg.day_count.year_fraction(previous, accrual_end)
            )
        )
        previous = accrual_end
    return tuple(periods)


def swap_schedule(swap: InterestRateSwap, calendar: BusinessCalendar) -> SwapSchedule:
    """Both legs' periods from the swap's convention on ``calendar``."""
    if not isinstance(calendar, BusinessCalendar):
        raise TypeError("calendar must be a BusinessCalendar")
    return SwapSchedule(
        _leg_periods(swap, swap.convention.fixed_leg, calendar),
        _leg_periods(swap, swap.convention.float_leg, calendar),
    )


@dataclass(frozen=True)
class SwapRiskRequest:
    """A swap, the calendar of its schedule and its market.

    ``discount_curve`` and a separate ``projection_curve`` each select their risk
    basis by type, as in ``BondRiskRequest``: a ``DiscountCurve`` takes zero-rate
    overlays, a ``CurveBootstrapRequest`` quote shifts and a rebootstrap. A
    ``KeyRateForwardCurve`` (key-rate swaps) takes bucket shifts of the forward key
    rate after its current-rate interval. ``projection_curve=None`` projects RUONIA
    on the discount curve (single curve, one set of factors). ``fixings`` is
    required once a remaining period has started; its valuation date is the curves'.
    """

    swap: InterestRateSwap
    calendar: BusinessCalendar
    discount_curve: DiscountCurve | CurveBootstrapRequest
    projection_curve: ProjectionCurve | None = None
    fixings: FixingHistory | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.swap, InterestRateSwap):
            raise TypeError("swap must be an InterestRateSwap")
        if not isinstance(self.calendar, BusinessCalendar):
            raise TypeError("calendar must be a BusinessCalendar")
        if not isinstance(self.discount_curve, (DiscountCurve, CurveBootstrapRequest)):
            raise TypeError("discount_curve must be DiscountCurve or CurveBootstrapRequest")
        projection = self.projection_curve
        index = self.swap.convention.index
        if index == "key_rate" and not isinstance(projection, KeyRateForwardCurve):
            raise InvalidTerms("a key-rate swap is projected on a KeyRateForwardCurve")
        if index == "ruonia" and not (
            projection is None or isinstance(projection, (DiscountCurve, CurveBootstrapRequest))
        ):
            raise InvalidTerms("a RUONIA swap is projected on a DiscountCurve or OIS quotes")
        valuation = self.discount_curve.curve_date
        if projection is not None and projection.curve_date != valuation:
            raise InvalidTerms("discount and projection curves must share one valuation date")
        if self.fixings is not None:
            if not isinstance(self.fixings, FixingHistory):
                raise TypeError("fixings must be a FixingHistory")
            if self.fixings.index != index:
                raise InvalidTerms("fixing history index differs from the swap index")
            if self.fixings.valuation_date != valuation:
                raise InvalidTerms("fixing history valuation date must equal the curve date")
        object.__setattr__(self, "discount_curve", _snapshot_curve(self.discount_curve))
        if isinstance(projection, (DiscountCurve, CurveBootstrapRequest)):
            object.__setattr__(self, "projection_curve", _snapshot_curve(projection))
        elif isinstance(projection, KeyRateForwardCurve) and type(projection.segments) is not tuple:
            object.__setattr__(
                self, "projection_curve", replace(projection, segments=tuple(projection.segments))
            )

    @property
    def valuation_date(self) -> date:
        return self.discount_curve.curve_date

    @property
    def single_curve(self) -> bool:
        return self.projection_curve is None


@dataclass(frozen=True)
class SwapScenario:
    """Absolute bp bucket shifts of the discount and projection curves.

    A single-curve swap moves its one curve with ``discount_shifts_bp``; its
    ``projection_shifts_bp`` must stay zero.
    """

    discount_shifts_bp: tuple[float, ...] = _ZERO_SHOCK
    projection_shifts_bp: tuple[float, ...] = _ZERO_SHOCK

    def __post_init__(self) -> None:
        object.__setattr__(self, "discount_shifts_bp", _shifts(tuple(self.discount_shifts_bp)))
        object.__setattr__(self, "projection_shifts_bp", _shifts(tuple(self.projection_shifts_bp)))

    @classmethod
    def parallel(cls, discount_bp: float = 0.0, projection_bp: float = 0.0) -> SwapScenario:
        size = len(RISK_BUCKETS)
        return cls((discount_bp,) * size, (projection_bp,) * size)


@dataclass(frozen=True)
class SwapCashflow:
    """One remaining flow. ``amount_rub`` and ``pv_rub`` are signed for the position.

    ``rate`` is the leg's annual rate over ``accrual_factor``: the fixed rate, or
    the float coupon rate (spread included) implied by the amount. ``known_days``
    and ``projected_days`` are calendar days of the accrual period taken from
    fixings and from the projection curve.
    """

    leg: SwapLeg
    accrual_start: date
    accrual_end: date
    payment_date: date
    accrual_factor: float
    rate: float
    amount_rub: float
    source: FlowSource
    known_days: int
    projected_days: int
    discount_factor: float
    pv_rub: float


@dataclass(frozen=True)
class SwapValuation:
    """PV and its parts at the valuation date (signed RUB for the position).

    Accrued interest is what each leg has earned and not yet been paid at the
    valuation date: the part of the current coupon accrued so far (float: from the
    known fixings) and any finished period still awaiting its payment date;
    ``clean_pv_rub`` is PV minus
    the net accrued. ``par_rate`` is the fixed rate that sets the PV of the
    remaining flows to zero; ``annuity`` is ``sum(alpha_i * DF_i)`` of the
    remaining fixed periods per unit notional.
    """

    base_id: str
    swap_id: str
    side: SwapSide
    valuation_date: date
    discount_curve_basis: str
    projection_curve_basis: str
    pv_rub: float
    fixed_leg_pv_rub: float
    float_leg_pv_rub: float
    accrued_fixed_rub: float
    accrued_float_rub: float
    accrued_rub: float
    clean_pv_rub: float
    par_rate: float
    annuity: float
    cashflows: tuple[SwapCashflow, ...]
    model_version: str = SWAP_MODEL_VERSION


@dataclass(frozen=True)
class SwapCurveRisk:
    """Bucket risk of one curve role. ``krd_years = -dv01 / notional * 1e4`` (signed)."""

    curve_role: CurveRole
    curve_basis: str
    buckets: tuple[BucketRisk, ...]
    dv01_rub: float
    parallel_dv01_rub: float


@dataclass(frozen=True)
class SwapRiskResult:
    """Signed RUB per bp: positive for a payer when rates rise. ``dv01_rub`` sums every curve's buckets."""

    base_id: str
    swap_id: str
    side: SwapSide
    difference: str
    bump_bp: float
    notional_rub: float
    pv_rub: float
    curves: tuple[SwapCurveRisk, ...]
    dv01_rub: float
    model_version: str = SWAP_MODEL_VERSION


@dataclass(frozen=True)
class SwapScenarioResult:
    base_id: str
    scenario_id: str
    base_pv_rub: float
    shocked_pv_rub: float
    discount_pnl_rub: float
    projection_pnl_rub: float
    interaction_pnl_rub: float
    total_pnl_rub: float
    model_version: str = SWAP_MODEL_VERSION


@dataclass(frozen=True)
class SwapSecondOrderRisk:
    """Central gradient (RUB/bp) and full symmetric Hessian (RUB/bp squared).

    Factors are the 11 buckets of the single curve, or 11 discount then 11
    projection buckets.
    """

    base_id: str
    curve_roles: tuple[CurveRole, ...]
    factors: tuple[str, ...]
    bump_bp: float
    gradient_rub_per_bp: tuple[float, ...]
    hessian_rub_per_bp2: tuple[tuple[float, ...], ...]
    model_version: str = SWAP_SECOND_ORDER_MODEL_VERSION


@dataclass(frozen=True)
class SwapHorizonValuation:
    """Carry and rolldown from the valuation date to ``horizon_date``, as in ``horizon``.

    - ``received_rub``: flows paid in ``(valuation date, horizon]``, as cash;
    - ``pv_forward_rub``: the remaining flows at the horizon on today's curves read
      forward (today's forwards realised, so float amounts are today's projections);
    - ``pv_rolled_rub``: the remaining flows at the horizon on rolled curves (shape
      held in calendar days); fixings up to the horizon are today's forwards.

    ``carry = pv_forward + received - pv_start``, ``rolldown = pv_rolled -
    pv_forward`` and ``horizon_pnl = pv_rolled + received - pv_start``.
    """

    base_id: str
    swap_id: str
    valuation_date: date
    horizon_date: date
    pv_start_rub: float
    pv_forward_rub: float
    pv_rolled_rub: float
    received_rub: float
    carry_rub: float
    rolldown_rub: float
    horizon_pnl_rub: float
    model_version: str = SWAP_HORIZON_MODEL_VERSION


@dataclass(frozen=True)
class _FloatFlow:
    period: SwapPeriod
    known_growth: float
    known_sum: float
    known_days: int
    projection_start: date
    spread_amount: float
    accrued: float
    base_average: float | None
    shift_weights: tuple[float, ...] | None
    growth_scale: float = 1.0  # float amount per unit of compounded growth minus one

    @property
    def total_days(self) -> int:
        return (self.period.accrual_end - self.period.accrual_start).days

    @property
    def projected_days(self) -> int:
        return (self.period.accrual_end - self.projection_start).days


def _fixings_identity(history: FixingHistory) -> _Canonical:
    """``history.identity`` converted once per history object and reused by every swap."""
    cached = history.__dict__.get("_swap_identity_canonical")
    if cached is None:
        cached = _precanonical(history.identity)
        object.__setattr__(history, "_swap_identity_canonical", cached)
    return cast(_Canonical, cached)


def _previous_business_day(calendar: BusinessCalendar, value: date) -> date:
    current = value
    while not calendar.is_business_day(current):
        current -= timedelta(days=1)
    return current


def _compounded_known(
    period: SwapPeriod, history: FixingHistory, day_count: DayCount | None
) -> tuple[float, date]:
    """Realized RUONIA growth from the period start and the date projection starts.

    Each accrual day takes the fixing of the latest business day on or before it
    (no lookback). Segments whose observation is before the valuation date are known.
    """
    calendar = history.calendar
    cursor = period.accrual_start
    growth = 1.0
    # NFA RUB-RUONIA-OIS-COMPOUND (day_count None): n_i / Act of the whole period.
    act = _ruonia_ois_compound_act(period.accrual_start, period.accrual_end)
    while cursor < period.accrual_end:
        observation = _previous_business_day(calendar, cursor)
        if observation >= history.valuation_date:
            break
        end = min(calendar.add_business_days(observation, 1), period.accrual_end)
        try:
            rate, _ = history.rate(observation)
        except LookupError as exc:
            raise MissingMarketData(str(exc)) from None
        weight = (
            (end - cursor).days / act if day_count is None else day_count.year_fraction(cursor, end)
        )
        growth *= 1.0 + rate * weight
        cursor = end
    return growth, cursor


def _arithmetic_known(period: SwapPeriod, history: FixingHistory) -> tuple[float, int]:
    """Sum of the key rate in force on each period day before the valuation date."""
    total = []
    day = period.accrual_start
    last = min(period.accrual_end, history.valuation_date)
    while day < last:
        try:
            rate, _ = history.rate(day)
        except LookupError as exc:
            raise MissingMarketData(str(exc)) from None
        total.append(rate)
        day += timedelta(days=1)
    return math.fsum(total), len(total)


def _key_rate_time(curve_date: date, day_count: DayCount, horizon: date) -> np.ndarray:
    """Curve time of every day from ``curve_date`` up to ``horizon`` (exclusive)."""
    times = np.empty((horizon - curve_date).days)
    year_base: dict[int, tuple[float, date, float]] = {}
    for offset in range(times.size):
        day = curve_date + timedelta(days=offset)
        base = year_base.get(day.year)
        if base is None:
            start = max(curve_date, date(day.year, 1, 1))
            base = (
                day_count.year_fraction(curve_date, start),
                start,
                366.0 if isleap(day.year) else 365.0,
            )
            year_base[day.year] = base
        if day_count is DayCount.ACT_ACT_ISDA:
            times[offset] = base[0] + (day - base[1]).days / base[2]
        else:
            times[offset] = day_count.year_fraction(curve_date, day)
    return times


def _key_rate_weight_prefix(curve: KeyRateForwardCurve) -> np.ndarray:
    """Running sums of the bucket weights of each day, cached on the curve.

    Row ``n`` holds, per bucket, the sum of the weights of days ``0..n-1`` after the
    curve date; days take their weight at their own curve time (spec time basis).
    """
    cached = curve.__dict__.get("_swap_weight_prefix")
    if cached is not None:
        return cast(np.ndarray, cached)
    day_count = get_curve_spec(_KEY_RATE_SPEC).time_day_count
    times = _key_rate_time(curve.curve_date, day_count, curve.horizon_date)
    pillars = np.asarray(RISK_PILLARS)
    identity = np.eye(len(RISK_PILLARS))
    weights = np.column_stack([np.interp(times, pillars, identity[k]) for k in range(len(pillars))])
    prefix = np.vstack([np.zeros((1, len(pillars))), np.cumsum(weights, axis=0)])
    object.__setattr__(curve, "_swap_weight_prefix", prefix)
    return prefix


def _key_rate_shift_weights(
    curve: KeyRateForwardCurve, start: date, end: date
) -> tuple[float, ...]:
    """Bucket weights summed over the days of ``[start, end)`` after the current-rate interval.

    The key rate in force until ``current_rate_end`` was set by the regulator and
    does not move with the curve.
    """
    first = max(start, curve.current_rate_end)
    if first >= end:
        return (0.0,) * len(RISK_BUCKETS)
    prefix = _key_rate_weight_prefix(curve)
    row = prefix[(end - curve.curve_date).days] - prefix[(first - curve.curve_date).days]
    return tuple(float(value) for value in row)


_Vector = tuple[float, ...]


@dataclass(frozen=True)
class SwapRiskEngine:
    """Price a swap once, then reprice it under curve shocks with its fixings frozen.

    PV is ``notional * sum(a_i * DF_i)``: signed unit amounts ``a_i`` (projection
    curve) times discount factors (discount curve) at the payment dates. Share one
    ``curve_cache`` across engines on the same OIS quotes.
    """

    request: SwapRiskRequest
    curve_cache: ShockedCurveCache = field(
        default_factory=ShockedCurveCache, compare=False, repr=False
    )
    base_id: str = field(init=False)
    base_pv: float = field(init=False)
    _discount: DiscountCurve = field(init=False, repr=False)
    _projection: DiscountCurve | KeyRateForwardCurve | None = field(init=False, repr=False)
    _fixed: tuple[tuple[SwapPeriod, float, float], ...] = field(init=False, repr=False)
    _float: tuple[_FloatFlow, ...] = field(init=False, repr=False)
    _dates: tuple[date, ...] = field(init=False, repr=False)
    _key_rate_amounts: _Vector = field(init=False, repr=False)
    _key_rate_slope: np.ndarray | None = field(init=False, repr=False)
    _memo: dict[tuple[str, _Vector], _Vector] = field(
        init=False, repr=False, compare=False, default_factory=dict
    )

    def __post_init__(self) -> None:
        req = self.request
        if not isinstance(req, SwapRiskRequest):
            raise TypeError("request must be a SwapRiskRequest")
        if not isinstance(self.curve_cache, ShockedCurveCache):
            raise TypeError("curve_cache must be a ShockedCurveCache")
        swap = req.swap
        valuation = req.valuation_date
        object.__setattr__(self, "_discount", _base_curve(req.discount_curve, self.curve_cache))
        projection = req.projection_curve
        if isinstance(projection, (DiscountCurve, CurveBootstrapRequest)):
            object.__setattr__(self, "_projection", _base_curve(projection, self.curve_cache))
        else:
            object.__setattr__(self, "_projection", projection)
        schedule = swap_schedule(swap, req.calendar)
        fixed = []
        for period in schedule.fixed:
            if period.payment_date <= valuation:
                continue
            accrued = 0.0
            if period.accrual_start < valuation:
                accrued = swap.fixed_rate * swap.convention.fixed_leg.day_count.year_fraction(
                    period.accrual_start, min(valuation, period.accrual_end)
                )
            fixed.append((period, swap.fixed_rate * period.accrual_factor, accrued))
        floating = tuple(
            self._float_flow(period)
            for period in schedule.floating
            if period.payment_date > valuation
        )
        if not fixed and not floating:
            raise InvalidTerms("no swap flows remain after the valuation date")
        object.__setattr__(self, "_fixed", tuple(fixed))
        object.__setattr__(self, "_float", floating)
        object.__setattr__(
            self,
            "_dates",
            (*(p.payment_date for p, _, _ in fixed), *(f.period.payment_date for f in floating)),
        )
        self._linear_key_rate_projection()
        object.__setattr__(self, "base_pv", self._pv(_ZERO_SHOCK, _ZERO_SHOCK))
        object.__setattr__(
            self,
            "base_id",
            canonical_sha256(
                {
                    "model": SWAP_MODEL_VERSION,
                    # Canonical form of the terms (the bytes of ``asdict`` before
                    # ``SwapConvention.float_compounding``, left out at its default).
                    "swap": swap,
                    "convention": swap.convention.identifier,
                    "calendar": (req.calendar.version, calendar_fingerprint(req.calendar)),
                    "discount_curve_basis": self.discount_curve_basis,
                    "discount_curve": _curve_identity(req.discount_curve),
                    "projection_curve_basis": self.projection_curve_basis,
                    "projection_curve": self._projection_identity(),
                    "fixings": None if req.fixings is None else _fixings_identity(req.fixings),
                }
            ),
        )

    def _projection_identity(self) -> object:
        projection = self.request.projection_curve
        if projection is None:
            return None
        if isinstance(projection, KeyRateForwardCurve):
            return canonical_sha256(projection)
        return _curve_identity(projection)

    @property
    def discount_curve_basis(self) -> str:
        return _curve_basis(self.request.discount_curve)

    @property
    def projection_curve_basis(self) -> str:
        projection = self.request.projection_curve
        if projection is None:
            return "single_curve"
        if isinstance(projection, KeyRateForwardCurve):
            return "key_rate_forward"
        return _curve_basis(projection)

    @property
    def valuation_date(self) -> date:
        return self.request.valuation_date

    def _history(self, period: SwapPeriod) -> FixingHistory:
        history = self.request.fixings
        if history is None:
            raise MissingMarketData(
                f"the float period from {period.accrual_start.isoformat()} has started; "
                "its fixing history is required"
            )
        return history

    def _float_flow(self, period: SwapPeriod) -> _FloatFlow:
        req = self.request
        swap = req.swap
        convention = swap.convention
        valuation = req.valuation_date
        float_day_count = convention.float_leg.day_count
        spread_amount = swap.float_spread * period.accrual_factor
        started = period.accrual_start < valuation
        spread_accrued = (
            swap.float_spread
            * float_day_count.year_fraction(
                period.accrual_start, min(valuation, period.accrual_end)
            )
            if started
            else 0.0
        )
        if convention.index == "ruonia":
            nfa = convention.float_compounding == "nfa_act"
            growth, start = (
                _compounded_known(
                    period,
                    self._history(period),
                    None if nfa else cast(DayCount, convention.rate_day_count),
                )
                if started
                else (1.0, period.accrual_start)
            )
            scale = 1.0
            accrued = growth - 1.0 + spread_accrued
            if nfa:
                act = _ruonia_ois_compound_act(period.accrual_start, period.accrual_end)
                total = (period.accrual_end - period.accrual_start).days
                scale = act / total * period.accrual_factor
                known = (start - period.accrual_start).days
                accrued = spread_accrued + (
                    (growth - 1.0)
                    * act
                    / known
                    * float_day_count.year_fraction(period.accrual_start, start)
                    if known
                    else 0.0
                )
            return _FloatFlow(
                period,
                growth,
                0.0,
                (start - period.accrual_start).days,
                start,
                spread_amount,
                accrued,
                None,
                None,
                scale,
            )
        known_sum, known_days = (
            _arithmetic_known(period, self._history(period)) if started else (0.0, 0)
        )
        start = period.accrual_start + timedelta(days=known_days)
        total_days = (period.accrual_end - period.accrual_start).days
        accrued = known_sum / total_days * period.accrual_factor + spread_accrued
        average: float | None = None
        weights: tuple[float, ...] | None = None
        if start < period.accrual_end:
            curve = cast(KeyRateForwardCurve, self._projection)
            if period.accrual_end > curve.horizon_date:
                raise MissingMarketData(
                    "key-rate projection curve does not cover the float period to "
                    f"{period.accrual_end.isoformat()}"
                )
            average = curve.arithmetic_average(start, period.accrual_end)
            weights = _key_rate_shift_weights(curve, start, period.accrual_end)
        return _FloatFlow(
            period, 1.0, known_sum, known_days, start, spread_amount, accrued, average, weights
        )

    # --- pricing -----------------------------------------------------------------

    def _discount_view(self, shifts: _Vector) -> DiscountFactorCurve:
        return _shock_curve(self.request.discount_curve, self._discount, shifts, self.curve_cache)

    def _projection_view(self, shifts: _Vector) -> DiscountFactorCurve | None:
        """The shocked RUONIA projection curve of a two-curve swap; None for key-rate swaps."""
        source = self.request.projection_curve
        if isinstance(source, KeyRateForwardCurve):
            return None
        base = cast(DiscountCurve, self._projection)
        return _shock_curve(cast(RiskCurve, source), base, shifts, self.curve_cache)

    def _float_unit(self, flow: _FloatFlow, view: DiscountFactorCurve | None) -> float:
        """Unsigned float amount per unit notional (key rate: on the base curve)."""
        end = flow.period.accrual_end
        if self.request.swap.convention.index == "ruonia":
            growth = flow.known_growth
            if flow.projection_start < end:
                assert view is not None
                growth *= view.discount_factor(flow.projection_start) / view.discount_factor(end)
            return (growth - 1.0) * flow.growth_scale + flow.spread_amount
        if flow.base_average is None:
            average = flow.known_sum / flow.total_days
        elif flow.known_days == 0:
            average = flow.base_average
        else:
            average = (flow.known_sum + flow.base_average * flow.projected_days) / flow.total_days
        return average * flow.period.accrual_factor + flow.spread_amount

    def _linear_key_rate_projection(self) -> None:
        """Key-rate amounts are linear in the bucket shifts: base plus slope @ shifts.

        A shift ``s_k`` (bp) of bucket k moves the average by ``1e-4 * sum_k s_k * W_k``
        over the averaging days, ``W_k`` the bucket weights summed over the projected
        days after the current-rate interval.
        """
        sign = self.request.swap.fixed_sign
        if self.request.swap.convention.index != "key_rate":
            object.__setattr__(self, "_key_rate_amounts", ())
            object.__setattr__(self, "_key_rate_slope", None)
            return
        object.__setattr__(
            self,
            "_key_rate_amounts",
            (
                *(sign * unit for _, unit, _ in self._fixed),
                *(-sign * self._float_unit(f, None) for f in self._float),
            ),
        )
        slope = np.zeros((len(self._dates), len(RISK_BUCKETS)))
        offset = len(self._fixed)
        for row, flow in enumerate(self._float, start=offset):
            if flow.shift_weights is None:
                continue
            days = flow.projected_days if flow.known_days == 0 else flow.total_days
            scale = -sign * 1e-4 * flow.period.accrual_factor / days
            slope[row] = scale * np.asarray(flow.shift_weights)
        object.__setattr__(self, "_key_rate_slope", slope)

    def _amounts(self, view: DiscountFactorCurve | None, shifts: _Vector) -> _Vector:
        """Signed unit amounts: fixed periods, then float periods."""
        if self._key_rate_slope is not None:
            if not any(shifts):
                return self._key_rate_amounts
            moved = np.asarray(self._key_rate_amounts) + self._key_rate_slope @ np.asarray(shifts)
            return tuple(float(value) for value in moved)
        sign = self.request.swap.fixed_sign
        return (
            *(sign * unit for _, unit, _ in self._fixed),
            *(-sign * self._float_unit(f, view) for f in self._float),
        )

    def _payment_dates(self) -> tuple[date, ...]:
        return self._dates

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
        """Signed unit amounts and discount factors; memoized per shift vector."""

        def discounts() -> _Vector:
            view = self._discount_view(discount_shifts)
            return tuple(view.discount_factor(d) for d in self._payment_dates())

        dfs = self._cached(("discount", discount_shifts), discounts)
        if self.request.single_curve:
            if any(projection_shifts):
                raise InvalidTerms("a single-curve swap has no separate projection shifts")
            amounts = self._cached(
                ("single", discount_shifts),
                lambda: self._amounts(self._discount_view(discount_shifts), _ZERO_SHOCK),
            )
        else:
            amounts = self._cached(
                ("projection", projection_shifts),
                lambda: self._amounts(self._projection_view(projection_shifts), projection_shifts),
            )
        return amounts, dfs

    def _pv(self, discount_shifts: _Vector, projection_shifts: _Vector) -> float:
        try:
            amounts, dfs = self._vectors(discount_shifts, projection_shifts)
            value = self.request.swap.notional * math.fsum(
                a * d for a, d in zip(amounts, dfs, strict=True)
            )
        except (OverflowError, ZeroDivisionError) as exc:
            raise ValueError("shock is outside the supported numerical domain") from exc
        return finite_money(value, "swap PV")

    # --- results -----------------------------------------------------------------

    def valuation(self) -> SwapValuation:
        """PV, legs, accrued, par rate and the remaining cashflows on the base curves."""
        swap = self.request.swap
        amounts, dfs = self._vectors(_ZERO_SHOCK, _ZERO_SHOCK)
        notional = swap.notional
        rows: list[SwapCashflow] = []
        for (period, unit, _), amount, df in zip(self._fixed, amounts, dfs, strict=False):
            rows.append(
                SwapCashflow(
                    "fixed",
                    period.accrual_start,
                    period.accrual_end,
                    period.payment_date,
                    period.accrual_factor,
                    swap.fixed_rate,
                    notional * amount,
                    "fixed",
                    0,
                    0,
                    df,
                    notional * amount * df,
                )
            )
        offset = len(self._fixed)
        for flow, amount, df in zip(self._float, amounts[offset:], dfs[offset:], strict=True):
            period = flow.period
            projected = flow.projected_days
            source: FlowSource = (
                "known"
                if projected == 0
                else "projected"
                if flow.known_days == 0
                else "partially_projected"
            )
            rows.append(
                SwapCashflow(
                    "float",
                    period.accrual_start,
                    period.accrual_end,
                    period.payment_date,
                    period.accrual_factor,
                    -swap.fixed_sign * amount / period.accrual_factor,
                    notional * amount,
                    source,
                    flow.known_days,
                    projected,
                    df,
                    notional * amount * df,
                )
            )
        fixed_pv = math.fsum(r.pv_rub for r in rows if r.leg == "fixed")
        float_pv = math.fsum(r.pv_rub for r in rows if r.leg == "float")
        annuity = math.fsum(
            p.accrual_factor * df for (p, _, _), df in zip(self._fixed, dfs, strict=False)
        )
        accrued_fixed = swap.fixed_sign * notional * math.fsum(a for _, _, a in self._fixed)
        accrued_float = -swap.fixed_sign * notional * math.fsum(f.accrued for f in self._float)
        accrued = accrued_fixed + accrued_float
        par = math.nan
        if annuity > 0.0:
            par = -swap.fixed_sign * float_pv / (notional * annuity)
        return SwapValuation(
            self.base_id,
            swap.swap_id,
            swap.side,
            self.valuation_date,
            self.discount_curve_basis,
            self.projection_curve_basis,
            self.base_pv,
            finite_money(fixed_pv, "fixed leg PV"),
            finite_money(float_pv, "float leg PV"),
            finite_money(accrued_fixed, "fixed accrued"),
            finite_money(accrued_float, "float accrued"),
            finite_money(accrued, "accrued"),
            finite_money(self.base_pv - accrued, "clean PV"),
            par if annuity > 0.0 else 0.0,
            annuity,
            tuple(rows),
        )

    def _roles(self) -> tuple[CurveRole, ...]:
        if self.request.single_curve:
            return ("discount_and_projection",)
        return ("discount", "projection")

    def _moves(self, role: CurveRole, shifts: _Vector) -> tuple[_Vector, _Vector]:
        return (_ZERO_SHOCK, shifts) if role == "projection" else (shifts, _ZERO_SHOCK)

    def risk(
        self, *, bump_bp: float = 1.0, difference: Literal["forward", "central"] = "central"
    ) -> SwapRiskResult:
        """Bucket and parallel DV01 per curve role; central by default (DEC08)."""
        bump = positive_money(bump_bp, "risk bump bp")
        if difference not in ("forward", "central"):
            raise ValueError("difference must be forward or central")
        notional = self.request.swap.notional
        size = len(RISK_BUCKETS)

        def sensitivity(role: CurveRole, shifts: _Vector) -> float:
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
                SwapCurveRisk(
                    role,
                    basis,
                    tuple(buckets),
                    finite_money(math.fsum(b.dv01_rub for b in buckets), "curve DV01"),
                    sensitivity(role, (bump,) * size),
                )
            )
        return SwapRiskResult(
            self.base_id,
            self.request.swap.swap_id,
            self.request.swap.side,
            difference,
            bump,
            notional,
            self.base_pv,
            tuple(curves),
            finite_money(math.fsum(c.dv01_rub for c in curves), "DV01"),
        )

    def reprice(self, scenario: SwapScenario) -> SwapScenarioResult:
        """Exact repricing, each curve isolated, and their interaction."""
        if not isinstance(scenario, SwapScenario):
            raise TypeError("scenario must be a SwapScenario")
        discount, projection = scenario.discount_shifts_bp, scenario.projection_shifts_bp
        _check_scenario_shifts(self.request.discount_curve, discount)
        _check_scenario_shifts(self.request.projection_curve, projection)
        shocked = self._pv(discount, projection)
        discount_pnl = self._pv(discount, _ZERO_SHOCK) - self.base_pv
        projection_pnl = self._pv(_ZERO_SHOCK, projection) - self.base_pv
        total = shocked - self.base_pv
        return SwapScenarioResult(
            self.base_id,
            canonical_sha256(scenario),
            self.base_pv,
            shocked,
            finite_money(discount_pnl, "discount P&L"),
            finite_money(projection_pnl, "projection P&L"),
            finite_money(total - discount_pnl - projection_pnl, "interaction P&L"),
            finite_money(total, "total P&L"),
        )

    def second_order(self, *, bump_bp: float = 1.0) -> SwapSecondOrderRisk:
        """Full central gradient and Hessian over the curve buckets (11 or 22 factors)."""
        bump = positive_money(bump_bp, "second-order bump bp")
        size = len(RISK_BUCKETS)
        roles = self._roles()
        single = self.request.single_curve

        def price(moves: list[float]) -> float:
            if single:
                return self._pv(tuple(moves), _ZERO_SHOCK)
            return self._pv(tuple(moves[:size]), tuple(moves[size:]))

        if single:
            gradient, hessian = _central_derivatives(price, size, bump, self.base_pv, 1.0)
        else:
            gradient, hessian = self._separable_derivatives(bump)
        labels = {
            "discount_and_projection": "curve",
            "discount": "discount",
            "projection": "projection",
        }
        return SwapSecondOrderRisk(
            self.base_id,
            roles,
            tuple(f"{labels[role]} {b}" for role in roles for b in RISK_BUCKETS),
            bump,
            gradient,
            hessian,
        )

    def _separable_derivatives(
        self, bump: float
    ) -> tuple[tuple[float, ...], tuple[tuple[float, ...], ...]]:
        """Central gradient and Hessian of a two-curve swap from leg vectors.

        ``PV = N * a(p) . d(q)`` is bilinear in the amounts (projection shifts p) and
        discount factors (discount shifts q), so the four-point mixed difference of a
        discount and a projection bucket is exactly
        ``N * (d(+q_j) - d(-q_j)) . (a(+p_k) - a(-p_k)) / (4 h^2)``. Same-curve terms use
        the same differences as the generic central scheme, except that key-rate
        amounts are linear in their shifts: that block is exactly zero.
        """
        size = len(RISK_BUCKETS)
        notional = self.request.swap.notional
        square = bump * bump

        def moved(i: int, si: float, j: int | None = None, sj: float = 0.0) -> _Vector:
            values = [0.0] * size
            values[i] = si * bump
            if j is not None:
                values[j] = sj * bump
            return tuple(values)

        def block(
            projection: bool, linear: bool = False
        ) -> tuple[list[float], np.ndarray, np.ndarray, np.ndarray]:
            """Gradient and same-curve Hessian, as the generic central scheme computes them."""

            def pv(shifts: _Vector) -> float:
                return (
                    self._pv(_ZERO_SHOCK, shifts) if projection else self._pv(shifts, _ZERO_SHOCK)
                )

            def vector(shifts: _Vector) -> np.ndarray:
                pair = (
                    self._vectors(_ZERO_SHOCK, shifts)
                    if projection
                    else self._vectors(shifts, _ZERO_SHOCK)
                )
                return np.asarray(pair[0] if projection else pair[1])

            gradient = []
            hessian = np.zeros((size, size))
            for i in range(size):
                up, down = pv(moved(i, 1.0)), pv(moved(i, -1.0))
                gradient.append((up - down) / (2 * bump))
                if linear:
                    continue
                hessian[i, i] = ((up - self.base_pv) + (down - self.base_pv)) / square
                for j in range(i):
                    corners = [
                        pv(moved(i, a, j, b)) for a, b in ((1, 1), (1, -1), (-1, 1), (-1, -1))
                    ]
                    mixed = ((corners[0] - corners[1]) - (corners[2] - corners[3])) / (4 * square)
                    hessian[i, j] = hessian[j, i] = mixed
            ups = np.stack([vector(moved(i, 1.0)) for i in range(size)])
            downs = np.stack([vector(moved(i, -1.0)) for i in range(size)])
            return gradient, hessian, ups, downs

        g_d, h_d, up_d, down_d = block(False)
        g_p, h_p, up_p, down_p = block(True, linear=self._key_rate_slope is not None)
        cross = notional * ((up_d - down_d) @ (up_p - down_p).T) / (4 * square)
        hessian = np.block([[h_d, cross], [cross.T, h_p]])
        if not (np.all(np.isfinite(hessian)) and all(map(math.isfinite, g_d + g_p))):
            raise ValueError("second-order risk must be finite")
        return tuple(g_d + g_p), tuple(tuple(float(v) for v in row) for row in hessian)

    # --- horizon -----------------------------------------------------------------

    def _rolled_float_unit(
        self,
        flow: _FloatFlow,
        horizon: date,
        today: DiscountFactorCurve | KeyRateForwardCurve,
        rolled: DiscountFactorCurve | KeyRateForwardCurve,
    ) -> float:
        """Float amount with today's forwards up to ``horizon`` and the rolled curve after."""
        start, end = flow.projection_start, flow.period.accrual_end
        middle = min(max(start, horizon), end)
        if self.request.swap.convention.index == "ruonia":
            growth = flow.known_growth
            if start < middle:
                now = cast(DiscountFactorCurve, today)
                growth *= now.discount_factor(start) / now.discount_factor(middle)
            if middle < end:
                later = cast(DiscountFactorCurve, rolled)
                growth *= later.discount_factor(middle) / later.discount_factor(end)
            return (growth - 1.0) * flow.growth_scale + flow.spread_amount
        total = flow.known_sum
        if start < middle:
            now_k = cast(KeyRateForwardCurve, today)
            total += now_k.arithmetic_average(start, middle) * (middle - start).days
        if middle < end:
            later_k = cast(KeyRateForwardCurve, rolled)
            total += later_k.arithmetic_average(middle, end) * (end - middle).days
        return total / flow.total_days * flow.period.accrual_factor + flow.spread_amount

    def horizon(self, horizon_date: date) -> SwapHorizonValuation:
        """Carry and rolldown to ``horizon_date`` (see :class:`SwapHorizonValuation`)."""
        valuation = self.valuation_date
        if not isinstance(horizon_date, date) or horizon_date <= valuation:
            raise InvalidTerms("horizon date must be after the valuation date")
        swap = self.request.swap
        notional = swap.notional
        amounts, dfs = self._vectors(_ZERO_SHOCK, _ZERO_SHOCK)
        payments = self._payment_dates()
        discount = self._discount
        forward_df = discount.discount_factor(horizon_date)
        received = notional * math.fsum(
            a for a, d in zip(amounts, payments, strict=True) if d <= horizon_date
        )
        pv_forward = notional * math.fsum(
            a * df / forward_df
            for a, df, d in zip(amounts, dfs, payments, strict=True)
            if d > horizon_date
        )
        rolled_discount = roll_curve(discount, horizon_date)
        today: DiscountFactorCurve | KeyRateForwardCurve
        rolled: DiscountFactorCurve | KeyRateForwardCurve
        projection = self._projection
        if projection is None:
            today, rolled = discount, rolled_discount
        elif isinstance(projection, KeyRateForwardCurve):
            if projection.horizon_date <= horizon_date:
                raise MissingMarketData("key-rate projection curve ends before the horizon")
            today, rolled = projection, roll_key_rate_curve(projection, horizon_date)
        else:
            today, rolled = projection, roll_curve(projection, horizon_date)
        sign = swap.fixed_sign
        rolled_amounts = (
            *(sign * unit for _, unit, _ in self._fixed),
            *(-sign * self._rolled_float_unit(f, horizon_date, today, rolled) for f in self._float),
        )
        pv_rolled = notional * math.fsum(
            a * rolled_discount.discount_factor(d)
            for a, d in zip(rolled_amounts, payments, strict=True)
            if d > horizon_date
        )
        return SwapHorizonValuation(
            self.base_id,
            swap.swap_id,
            valuation,
            horizon_date,
            self.base_pv,
            finite_money(pv_forward, "forward PV"),
            finite_money(pv_rolled, "rolled PV"),
            finite_money(received, "received"),
            finite_money(pv_forward + received - self.base_pv, "carry"),
            finite_money(pv_rolled - pv_forward, "rolldown"),
            finite_money(pv_rolled + received - self.base_pv, "horizon P&L"),
        )


def swap_horizon(
    request: SwapRiskRequest,
    *,
    horizon_date: date,
    curve_cache: ShockedCurveCache | None = None,
) -> SwapHorizonValuation:
    """Carry and rolldown of a swap to ``horizon_date``."""
    engine = SwapRiskEngine(request, ShockedCurveCache() if curve_cache is None else curve_cache)
    return engine.horizon(horizon_date)


def _upper(matrix: tuple[tuple[float, ...], ...]) -> tuple[HessianEntry, ...]:
    return tuple(
        HessianEntry(i, j, row[j])
        for i, row in enumerate(matrix)
        for j in range(i, len(row))
        if row[j] != 0.0
    )


def swap_factor_risk(
    risk: SwapRiskResult,
    *,
    discount_curve_id: str,
    projection_curve_id: str | None = None,
    second_order: SwapSecondOrderRisk | None = None,
) -> FactorRisk:
    """Map swap risk to desk factors ``IR|<curve_id>|<bucket>``.

    A single-curve swap has one curve ID (``projection_curve_id`` None or equal);
    a two-curve swap needs two distinct IDs. With ``second_order`` the first order
    is its central gradient (same bump as the Hessian); otherwise ``risk`` must be
    central (DEC08).
    """
    roles = tuple(c.curve_role for c in risk.curves)
    if roles == ("discount_and_projection",):
        if projection_curve_id not in (None, discount_curve_id):
            raise InvalidTerms("a single-curve swap has one curve ID")
        ids: tuple[str, ...] = (discount_curve_id,)
    else:
        if projection_curve_id is None or projection_curve_id == discount_curve_id:
            raise InvalidTerms("a two-curve swap needs distinct discount and projection curve IDs")
        ids = (discount_curve_id, projection_curve_id)
    factors = tuple(ir_factor(curve_id, b) for curve_id in ids for b in RISK_BUCKETS)
    if second_order is not None:
        if second_order.base_id != risk.base_id:
            raise InvalidTerms("risk and second-order results describe different swaps or markets")
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
