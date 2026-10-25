"""Single-name credit default swaps: ISDA standard model pricing, bootstrap, quotes and risk.

The model is the ISDA CDS Standard Model's: a piecewise-constant hazard rate, a
discount curve read as constant forward rates between the dates of a merged grid
(the discount curve's nodes, the hazard curve's nodes and the contract's own dates),
protection from the start of the step-in day to the end of the maturity day,
premium accrued ACT/360 with the last period including the maturity date, and
accrued premium paid on default. Every contract convention is an explicit input
(:class:`CdsConvention`); :meth:`CdsConvention.isda_standard` collects the
published standard choices. Recovery is always explicit; there is no default.

Money is in the contract currency, signed by side: a protection buyer gains when
spreads widen and on default; a seller is long credit, like a bondholder (DEC07).
No market data is looked up here and nothing is inferred from a reference entity.
See ``docs/cds.md``.
"""

from __future__ import annotations

import math
from bisect import bisect_left
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from enum import Enum
from functools import lru_cache
from itertools import pairwise
from typing import Literal, cast

from ._curve_views import forward_curve, roll_curve
from ._money import finite_money, non_negative_money, positive_money
from ._quote_curves import QuoteCurve
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
from ._solver import illinois
from .canonical import canonical_sha256
from .conventions import BusinessDayConvention, DayCount, adjust_date, calendar_fingerprint
from .dates import BusinessCalendar, add_months
from .discount import CurveBootstrapRequest, DiscountCurve, DiscountFactorCurve
from .errors import InvalidTerms, UnsupportedProduct
from .factors import CS_FACTOR, FactorRisk, HessianEntry, ir_factor

__all__ = [
    "CDS_HORIZON_MODEL_VERSION",
    "CDS_MODEL_VERSION",
    "CDS_RISK_MODEL_VERSION",
    "CDS_SECOND_ORDER_MODEL_VERSION",
    "AccrualOnDefault",
    "CdsBucketRisk",
    "CdsContract",
    "CdsConvention",
    "CdsCurveCache",
    "CdsCurveRequest",
    "CdsDefaultResult",
    "CdsHorizonValuation",
    "CdsPremiumPeriod",
    "CdsQuoteRisk",
    "CdsRiskEngine",
    "CdsRiskRequest",
    "CdsRiskResult",
    "CdsScenario",
    "CdsScenarioResult",
    "CdsSecondOrderRisk",
    "CdsSide",
    "CdsSpreadQuote",
    "CdsTradeDates",
    "CdsUpfrontQuote",
    "CdsValuation",
    "HazardCurve",
    "bootstrap_hazard_curve",
    "cds_factor_risk",
    "cds_horizon",
    "cds_par_spread",
    "cds_premium_schedule",
    "cds_trade_dates",
    "flat_hazard_from_spread",
    "flat_hazard_from_upfront",
    "forward_hazard_curve",
    "previous_imm_date",
    "roll_hazard_curve",
    "spread_from_upfront",
    "standard_cds_maturity",
    "upfront_from_spread",
    "value_cds",
]

CDS_MODEL_VERSION = "cds-isda-standard-v1"
CDS_RISK_MODEL_VERSION = "cds-isda-standard-risk-v1"
CDS_SECOND_ORDER_MODEL_VERSION = "cds-isda-standard-second-order-v1"
CDS_HORIZON_MODEL_VERSION = "rub-cds-horizon-v1"

_IMM_MONTHS = (3, 6, 9, 12)
_LINEAR_DAY_COUNTS = (DayCount.ACT_360, DayCount.ACT_365_FIXED)
_BASIS_DAYS = {DayCount.ACT_360: 360.0, DayCount.ACT_365_FIXED: 365.0}
_BOOTSTRAP_TOLERANCE = 1e-14
_REPRICING_TOLERANCE = 1e-12
_MAX_HAZARD = 50.0
_PLAN_MEMO_SIZE = 512


class CdsSide(Enum):
    """The protection buyer pays the premium; the seller is long the reference credit."""

    BUYER = "protection_buyer"
    SELLER = "protection_seller"

    @property
    def sign(self) -> float:
        return 1.0 if self is CdsSide.BUYER else -1.0


class AccrualOnDefault(Enum):
    """How premium accrued at default enters the premium leg.

    - ``NONE``: not paid (legacy contracts without accrual on default);
    - ``EXACT``: the accrued premium to the end of the default day;
    - ``ISDA_HALF_DAY_BIAS``: as the ISDA Standard Model C code integrates it, half a
      day more than ``EXACT`` (the formula QuantLib labels ``HalfDayBias`` and uses to
      reproduce Markit's published values).
    """

    NONE = "none"
    EXACT = "exact"
    ISDA_HALF_DAY_BIAS = "isda-half-day-bias"


@dataclass(frozen=True)
class CdsConvention:
    """Contract and model conventions; all explicit, none inferred from the entity.

    - ``calendar``: business days for payment dates and the cash settlement date;
    - ``payment_frequency_months``: premium period length; the schedule rolls back from
      the maturity date (unadjusted) in these steps;
    - ``business_day_convention``: adjustment of every premium date except the maturity
      date, which stays unadjusted as the last accrual end; its payment is adjusted;
    - ``accrual_day_count``: premium accrual (ACT/360 or ACT/365F; linear in days);
    - ``step_in_days``: calendar days from trade date to the step-in date, from which
      protection runs and to which accrued premium is counted;
    - ``cash_settlement_days``: business days from trade date to the upfront payment;
    - ``accrual_on_default``: see :class:`AccrualOnDefault`;
    - ``hazard_day_count``: time basis of hazard rates (ACT/365F or ACT/360).
    """

    calendar: BusinessCalendar
    payment_frequency_months: int
    business_day_convention: BusinessDayConvention
    accrual_day_count: DayCount
    step_in_days: int
    cash_settlement_days: int
    accrual_on_default: AccrualOnDefault
    hazard_day_count: DayCount

    def __post_init__(self) -> None:
        if not isinstance(self.calendar, BusinessCalendar):
            raise TypeError("calendar must be a BusinessCalendar")
        for name in ("payment_frequency_months", "step_in_days", "cash_settlement_days"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{name} must be an integer")
        if self.payment_frequency_months not in (1, 3, 6, 12):
            raise InvalidTerms("payment_frequency_months must be 1, 3, 6 or 12")
        if self.step_in_days < 0 or self.cash_settlement_days < 0:
            raise InvalidTerms("step-in and cash settlement lags cannot be negative")
        if not isinstance(self.business_day_convention, BusinessDayConvention):
            raise TypeError("business_day_convention must be a BusinessDayConvention")
        if not isinstance(self.accrual_on_default, AccrualOnDefault):
            raise TypeError("accrual_on_default must be an AccrualOnDefault")
        for name in ("accrual_day_count", "hazard_day_count"):
            value = getattr(self, name)
            if not isinstance(value, DayCount):
                raise TypeError(f"{name} must be a DayCount")
            if value not in _LINEAR_DAY_COUNTS:
                raise UnsupportedProduct(f"{name} must be ACT/360 or ACT/365F")

    @classmethod
    def isda_standard(cls, calendar: BusinessCalendar) -> CdsConvention:
        """The ISDA standard contract (2009 "Big Bang"/SNAC) and Standard Model choices.

        Quarterly premiums on the 20th of March, June, September and December, adjusted
        following; ACT/360; step-in T+1 calendar day; cash settlement T+3 business days;
        accrual on default as in the Standard Model code; hazard time ACT/365F. The
        calendar must still come from the caller; nothing here encodes holidays.
        """
        return cls(
            calendar=calendar,
            payment_frequency_months=3,
            business_day_convention=BusinessDayConvention.FOLLOWING,
            accrual_day_count=DayCount.ACT_360,
            step_in_days=1,
            cash_settlement_days=3,
            accrual_on_default=AccrualOnDefault.ISDA_HALF_DAY_BIAS,
            hazard_day_count=DayCount.ACT_365_FIXED,
        )

    @property
    def identifier(self) -> str:
        return (
            f"cds:{self.payment_frequency_months}M:{self.business_day_convention.value}:"
            f"{self.accrual_day_count.value}:stepin+{self.step_in_days}d:"
            f"cash+{self.cash_settlement_days}bd:aod={self.accrual_on_default.value}:"
            f"hazard={self.hazard_day_count.value}:"
            f"{self.calendar.version}@{calendar_fingerprint(self.calendar)}"
        )


# ---------------------------------------------------------------- dates


def previous_imm_date(value: date) -> date:
    """The latest unadjusted 20 March/June/September/December on or before ``value``."""
    candidate = date(value.year, value.month, 20)
    if candidate > value:
        candidate = add_months(candidate, -1)
    return add_months(candidate, -(candidate.month % 3))


def _is_imm_date(value: date) -> bool:
    return value.day == 20 and value.month in _IMM_MONTHS


def standard_cds_maturity(
    trade_date: date, tenor_months: int, *, roll: Literal["quarterly", "semiannual"]
) -> date:
    """Scheduled termination date of a standard contract of ``tenor_months``.

    ``quarterly``: the first IMM date after the trade date plus the tenor (rolls on
    every IMM date; the pre-2015 rule). ``semiannual``: the ISDA rule from
    20 December 2015, with on-the-run maturities rolling only on 20 March and
    20 September (a trade from 20 March to 19 September matures on 20 June + tenor).
    """
    if isinstance(tenor_months, bool) or not isinstance(tenor_months, int):
        raise TypeError("tenor_months must be an integer")
    if tenor_months <= 0 or tenor_months % 3:
        raise InvalidTerms("CDS tenor must be a positive multiple of 3 months")
    if roll not in ("quarterly", "semiannual"):
        raise InvalidTerms("roll must be 'quarterly' or 'semiannual'")
    anchor = previous_imm_date(trade_date)
    if roll == "semiannual" and anchor.month in (6, 12):
        anchor = add_months(anchor, -3)
    return add_months(anchor, tenor_months + 3)


@dataclass(frozen=True)
class CdsTradeDates:
    """Dates of a trade struck on ``trade_date`` under a convention.

    ``accrual_start_date`` is the standard full-first-coupon accrual start: the latest
    adjusted IMM date on or before the step-in date and before the maturity date
    (quarterly IMM conventions only). The maturity matters only in the last days of a
    contract, when the step-in date is the maturity date itself.
    """

    trade_date: date
    step_in_date: date
    cash_settlement_date: date
    accrual_start_date: date


def cds_trade_dates(
    trade_date: date, convention: CdsConvention, *, maturity_date: date
) -> CdsTradeDates:
    """Step-in, cash settlement and standard accrual start of a trade on ``trade_date``."""
    step_in = trade_date + timedelta(days=convention.step_in_days)
    cash = convention.calendar.add_business_days(trade_date, convention.cash_settlement_days)
    if convention.payment_frequency_months != 3:
        raise UnsupportedProduct("the standard accrual start needs quarterly IMM premiums")
    if maturity_date < step_in:
        raise InvalidTerms("the CDS matures before the step-in date")
    candidate = previous_imm_date(step_in)
    while True:
        adjusted = adjust_date(candidate, convention.business_day_convention, convention.calendar)
        if adjusted <= step_in and adjusted < maturity_date:
            break
        candidate = add_months(candidate, -3)
    return CdsTradeDates(trade_date, step_in, cash, adjusted)


@dataclass(frozen=True)
class CdsPremiumPeriod:
    """One premium period; ``accrual_end`` is exclusive (the last is maturity + 1 day)."""

    accrual_start: date
    accrual_end: date
    payment_date: date
    year_fraction: float


def cds_premium_schedule(
    accrual_start: date, maturity: date, convention: CdsConvention
) -> tuple[CdsPremiumPeriod, ...]:
    """Premium periods from ``accrual_start`` to ``maturity`` inclusive.

    Roll dates run back from the unadjusted maturity in steps of the payment frequency
    and are adjusted with the convention; a first period shorter than a full one is a
    front stub. The last accrual period includes the maturity date (ends the day
    after it) and pays on the adjusted maturity.
    """
    if maturity <= accrual_start:
        raise InvalidTerms("CDS maturity must follow the accrual start")
    step = convention.payment_frequency_months
    rolls: list[date] = []
    count = 1
    while True:
        unadjusted = add_months(maturity, -step * count)
        if unadjusted <= accrual_start:
            break
        rolls.append(
            adjust_date(unadjusted, convention.business_day_convention, convention.calendar)
        )
        count += 1
    rolls.reverse()
    boundaries = [accrual_start, *(d for d in rolls if accrual_start < d < maturity)]
    if any(left >= right for left, right in pairwise(boundaries)):
        raise InvalidTerms("adjusted CDS premium dates must be strictly increasing")
    final_payment = adjust_date(maturity, convention.business_day_convention, convention.calendar)
    ends = [*boundaries[1:], maturity + timedelta(days=1)]
    payments = [*boundaries[1:], final_payment]
    return tuple(
        CdsPremiumPeriod(start, end, pay, convention.accrual_day_count.year_fraction(start, end))
        for start, end, pay in zip(boundaries, ends, payments)
    )


# ---------------------------------------------------------------- hazard curve


@dataclass(frozen=True)
class HazardCurve:
    """Piecewise-constant hazard rates; survival ``Q(d) = exp(-integral of h)``.

    ``nodes`` are ``(end_date, hazard_rate)``: the rate applies on
    ``(previous end, end]`` from ``curve_date``, and the last rate extends flat beyond
    the last node. Time is ``day_count`` (ACT/365F in the ISDA model) from
    ``curve_date``. Rates are decimal per year and nonnegative.
    """

    curve_date: date
    nodes: tuple[tuple[date, float], ...]
    day_count: DayCount = DayCount.ACT_365_FIXED

    def __post_init__(self) -> None:
        nodes = tuple((d, r) for d, r in self.nodes)
        if not nodes:
            raise InvalidTerms("a hazard curve needs at least one node")
        if self.day_count not in _LINEAR_DAY_COUNTS:
            raise UnsupportedProduct("hazard day count must be ACT/360 or ACT/365F")
        previous = self.curve_date
        for node_date, rate in nodes:
            if not isinstance(node_date, date):
                raise TypeError("hazard node dates must be dates")
            if node_date <= previous:
                raise InvalidTerms("hazard node dates must increase after the curve date")
            if finite_money(rate, "hazard rate") < 0.0:
                raise InvalidTerms("hazard rates cannot be negative")
            previous = node_date
        object.__setattr__(self, "nodes", tuple((d, float(r)) for d, r in nodes))

    @classmethod
    def flat(
        cls, curve_date: date, hazard_rate: float, day_count: DayCount = DayCount.ACT_365_FIXED
    ) -> HazardCurve:
        """One rate everywhere (a single node the day after the curve date)."""
        return cls(curve_date, ((curve_date + timedelta(days=1), hazard_rate),), day_count)

    @property
    def node_dates(self) -> tuple[date, ...]:
        return tuple(d for d, _ in self.nodes)

    @property
    def hazard_rates(self) -> tuple[float, ...]:
        return tuple(r for _, r in self.nodes)

    def _segments(self) -> _Segments:
        cached = self.__dict__.get("_segments_cache")
        if cached is None:
            cached = _Segments.build(self.curve_date, self.node_dates, self.day_count)
            object.__setattr__(self, "_segments_cache", cached)
        assert isinstance(cached, _Segments)
        return cached

    def log_survival(self, target: date) -> float:
        if target < self.curve_date:
            raise InvalidTerms("cannot read survival before the hazard curve date")
        segments = self._segments()
        bases = segments.log_survival_bases(self.hazard_rates)
        index, tau = segments.locate(target)
        return bases[index] - self.hazard_rates[index] * tau

    def survival_probability(self, target: date) -> float:
        return math.exp(self.log_survival(target))

    def hazard_rate(self, target: date) -> float:
        """The hazard rate in force just before the end of ``target``."""
        if target <= self.curve_date:
            return self.nodes[0][1]
        return self.hazard_rates[self._segments().locate(target)[0]]


@dataclass(frozen=True)
class _Segments:
    curve_date: date
    ends: tuple[date, ...]
    lengths: tuple[float, ...]
    day_count: DayCount

    @classmethod
    def build(cls, curve_date: date, ends: tuple[date, ...], day_count: DayCount) -> _Segments:
        starts = (curve_date, *ends[:-1])
        return cls(
            curve_date,
            ends,
            tuple(day_count.year_fraction(s, e) for s, e in zip(starts, ends)),
            day_count,
        )

    def locate(self, target: date) -> tuple[int, float]:
        index = min(bisect_left(self.ends, target), len(self.ends) - 1)
        start = self.curve_date if index == 0 else self.ends[index - 1]
        return index, self.day_count.year_fraction(start, target)

    def log_survival_bases(self, rates: Sequence[float]) -> list[float]:
        bases = [0.0]
        for length, rate in zip(self.lengths[:-1], rates):
            bases.append(bases[-1] - rate * length)
        return bases


def forward_hazard_curve(curve: HazardCurve, horizon_date: date) -> HazardCurve:
    """Today's hazard curve read forward from ``horizon_date``: ``Q_h(t) = Q(t) / Q(h)``.

    The same rates on the same node dates after the horizon; the rate in force at the
    horizon applies from it, and with no node after it the last rate is flat.
    """
    if horizon_date < curve.curve_date:
        raise InvalidTerms("a hazard curve can only be read forward")
    if horizon_date == curve.curve_date:
        return curve
    later = tuple((d, r) for d, r in curve.nodes if d > horizon_date)
    if not later:
        return HazardCurve.flat(horizon_date, curve.nodes[-1][1], curve.day_count)
    return HazardCurve(horizon_date, later, curve.day_count)


def roll_hazard_curve(curve: HazardCurve, horizon_date: date) -> HazardCurve:
    """The hazard curve moved to ``horizon_date``, its shape held in calendar days."""
    if horizon_date < curve.curve_date:
        raise InvalidTerms("a hazard curve can only be rolled forward")
    shift = horizon_date - curve.curve_date
    return HazardCurve(horizon_date, tuple((d + shift, r) for d, r in curve.nodes), curve.day_count)


# ---------------------------------------------------------------- integration plan


def _e1(k: float) -> float:
    """(1 - exp(-k)) / k, its series near zero."""
    if abs(k) < 1e-2:
        return 1.0 + k * (
            -1 / 2 + k * (1 / 6 + k * (-1 / 24 + k * (1 / 120 + k * (-1 / 720 + k / 5040))))
        )
    return -math.expm1(-k) / k


def _e2(k: float) -> float:
    """Integral of s * exp(-k s) over [0, 1], its series near zero."""
    if abs(k) < 1e-2:
        return 1 / 2 + k * (
            -1 / 3 + k * (1 / 8 + k * (-1 / 30 + k * (1 / 144 + k * (-1 / 840 + k / 5760))))
        )
    return (-math.expm1(-k) - k * math.exp(-k)) / (k * k)


def _node_dates(curve: DiscountFactorCurve) -> tuple[date, ...]:
    """Dates between which the curve is read as constant forward rates."""
    base = getattr(curve, "base", curve)
    if not isinstance(base, DiscountCurve):
        return ()
    cached = base.__dict__.get("_cds_node_dates")
    if cached is None:
        cached = tuple(d for d, _ in base.nodes)
        object.__setattr__(base, "_cds_node_dates", cached)
    return cast(tuple[date, ...], cached)


@dataclass(frozen=True)
class _Layout:
    """Every date one contract reads, and how the legs combine them; no curve values.

    The integration grid between two contract dates is the union of the discount
    curve's and the hazard curve's node dates. A layout is shared by every shock of
    one contract: shocked curves keep their node dates. Indices point into ``dates``.
    """

    dates: tuple[date, ...]
    segments: tuple[int, ...]
    taus: tuple[float, ...]
    protection: tuple[tuple[int, int], ...]
    premium: tuple[tuple[float, int, int], ...]
    accrual: tuple[tuple[int, int, float, int], ...]
    cash_index: int
    accrual_scale: float
    accrued_fraction: float


@lru_cache(maxsize=4096)
def _layout(
    dates: _ContractDates,
    accrual_on_default: AccrualOnDefault,
    accrual_day_count: DayCount,
    segments: _Segments,
    discount_nodes: tuple[date, ...],
) -> _Layout:
    one_day = timedelta(days=1)
    grid = sorted(set(discount_nodes) | set(segments.ends))

    def path(start: date, end: date) -> list[date]:
        lo = bisect_left(grid, start + one_day)
        hi = bisect_left(grid, end)
        return [start, *grid[lo:hi], end]

    protection_path = path(dates.protection_start - one_day, dates.maturity)
    accrual_paths: list[tuple[float, list[date]]] = []
    if accrual_on_default is not AccrualOnDefault.NONE:
        bias = 0.5 if accrual_on_default is AccrualOnDefault.ISDA_HALF_DAY_BIAS else 0.0
        for p in dates.periods:
            anchor = float((p.accrual_start - one_day).toordinal()) - bias
            start = max(p.accrual_start, dates.protection_start) - one_day
            accrual_paths.append((anchor, path(start, p.accrual_end - one_day)))
    needed = {*protection_path, dates.cash_settlement_date}
    for p in dates.periods:
        needed.update((p.payment_date, p.accrual_end - one_day))
    for _, points in accrual_paths:
        needed.update(points)
    ordered = tuple(sorted(needed))
    index = {d: i for i, d in enumerate(ordered)}
    located = [segments.locate(d) for d in ordered]
    return _Layout(
        dates=ordered,
        segments=tuple(i for i, _ in located),
        taus=tuple(t for _, t in located),
        protection=tuple((index[a], index[b]) for a, b in pairwise(protection_path)),
        premium=tuple(
            (p.year_fraction, index[p.payment_date], index[p.accrual_end - one_day])
            for p in dates.periods
        ),
        accrual=tuple(
            (index[a], index[b], a.toordinal() - anchor, (b - a).days)
            for anchor, points in accrual_paths
            for a, b in pairwise(points)
        ),
        cash_index=index[dates.cash_settlement_date],
        accrual_scale=1.0 / _BASIS_DAYS[accrual_day_count],
        accrued_fraction=dates.accrued_fraction,
    )


@dataclass(frozen=True)
class _LegPlan:
    """A layout with the discount factors of one curve; evaluating it for a set of
    hazard rates reads no curve.

    Between grid dates the log discount factor and log survival are linear in days
    (constant forward rate and hazard), so each piece integrates in closed form.
    """

    layout: _Layout
    discount_factors: tuple[float, ...]
    log_discount_factors: tuple[float, ...]

    @property
    def cash_settlement_df(self) -> float:
        return self.discount_factors[self.layout.cash_index]

    @property
    def accrued_fraction(self) -> float:
        return self.layout.accrued_fraction

    def evaluate(self, rates: Sequence[float], bases: Sequence[float]) -> tuple[float, float]:
        """(protection per unit loss, risky annuity per unit coupon), both PV at today."""
        layout = self.layout
        log_q = [bases[s] - rates[s] * t for s, t in zip(layout.segments, layout.taus)]
        log_pq = [f + q for f, q in zip(self.log_discount_factors, log_q)]
        exp = math.exp
        protection = 0.0
        for i, j in layout.protection:
            hazard = log_q[i] - log_q[j]
            if hazard != 0.0:
                protection += hazard * exp(log_pq[i]) * _e1(log_pq[i] - log_pq[j])
        dfs = self.discount_factors
        annuity = math.fsum(
            alpha * dfs[pay] * exp(log_q[obs]) for alpha, pay, obs in layout.premium
        )
        accrual = 0.0
        for i, j, elapsed, length in layout.accrual:
            hazard = log_q[i] - log_q[j]
            if hazard != 0.0:
                k = log_pq[i] - log_pq[j]
                accrual += hazard * exp(log_pq[i]) * (elapsed * _e1(k) + length * _e2(k))
        return protection, annuity + accrual * layout.accrual_scale


@dataclass(frozen=True)
class _ContractDates:
    valuation_date: date
    step_in_date: date
    cash_settlement_date: date
    protection_start: date
    maturity: date
    periods: tuple[CdsPremiumPeriod, ...]
    accrued_fraction: float


@lru_cache(maxsize=4096)
def _contract_dates(
    valuation_date: date, accrual_start: date, maturity: date, convention: CdsConvention
) -> _ContractDates:
    """Pure date arithmetic, memoized: every shocked bootstrap repeats it."""
    step_in = valuation_date + timedelta(days=convention.step_in_days)
    cash = convention.calendar.add_business_days(valuation_date, convention.cash_settlement_days)
    if step_in > maturity:
        raise InvalidTerms("the CDS has matured: step-in is after the maturity date")
    periods = cds_premium_schedule(accrual_start, maturity, convention)
    accrued = 0.0
    for period in periods:
        if period.accrual_start <= step_in < period.accrual_end:
            accrued = convention.accrual_day_count.year_fraction(period.accrual_start, step_in)
            break
    live = tuple(p for p in periods if p.accrual_end > step_in)
    return _ContractDates(
        valuation_date,
        step_in,
        cash,
        max(step_in, accrual_start),
        maturity,
        live,
        accrued,
    )


def _build_plan(
    dates: _ContractDates,
    convention: CdsConvention,
    discount: DiscountFactorCurve,
    segments: _Segments,
) -> _LegPlan:
    if discount.curve_date != dates.valuation_date:
        raise InvalidTerms("the discount curve must be dated on the valuation date")
    if segments.curve_date != dates.valuation_date:
        raise InvalidTerms("the hazard curve must be dated on the valuation date")
    layout = _layout(
        dates,
        convention.accrual_on_default,
        convention.accrual_day_count,
        segments,
        _node_dates(discount),
    )
    dfs = tuple(
        positive_money(discount.discount_factor(d), "discount factor") for d in layout.dates
    )
    return _LegPlan(layout, dfs, tuple(math.log(df) for df in dfs))


def _evaluate_curve(plan: _LegPlan, hazard: HazardCurve) -> tuple[float, float]:
    segments = hazard._segments()
    rates = hazard.hazard_rates
    return plan.evaluate(rates, segments.log_survival_bases(rates))


def _clean_upfront(
    protection: float, annuity: float, plan: _LegPlan, coupon: float, recovery: float
) -> float:
    """Buyer's clean upfront per unit notional, paid at cash settlement."""
    dirty = (1.0 - recovery) * protection - coupon * annuity
    return dirty / plan.cash_settlement_df + coupon * plan.accrued_fraction


# ---------------------------------------------------------------- quotes and bootstrap


def _recovery(value: float) -> float:
    recovery = non_negative_money(value, "recovery rate")
    if recovery >= 1.0:
        raise InvalidTerms("recovery rate must be in [0, 1)")
    return recovery


@dataclass(frozen=True)
class CdsSpreadQuote:
    """A par spread (decimal): the running coupon at which the clean upfront is zero."""

    maturity: date
    par_spread: float

    def __post_init__(self) -> None:
        if not isinstance(self.maturity, date):
            raise TypeError("quote maturity must be a date")
        object.__setattr__(self, "par_spread", non_negative_money(self.par_spread, "par spread"))


@dataclass(frozen=True)
class CdsUpfrontQuote:
    """A standard-coupon quote: clean upfront per unit notional the buyer pays at
    cash settlement (points / 100), for the running ``coupon`` (decimal)."""

    maturity: date
    coupon: float
    upfront: float

    def __post_init__(self) -> None:
        if not isinstance(self.maturity, date):
            raise TypeError("quote maturity must be a date")
        object.__setattr__(self, "coupon", non_negative_money(self.coupon, "coupon"))
        upfront = finite_money(self.upfront, "upfront")
        if not -1.0 < upfront < 1.0:
            raise InvalidTerms("upfront must lie strictly between -1 and 1 of notional")
        object.__setattr__(self, "upfront", upfront)


CdsQuote = CdsSpreadQuote | CdsUpfrontQuote


def _quote_terms(quote: CdsQuote) -> tuple[float, float]:
    """(running coupon, target clean upfront)."""
    if isinstance(quote, CdsSpreadQuote):
        return quote.par_spread, 0.0
    return quote.coupon, quote.upfront


@dataclass(frozen=True)
class CdsCurveRequest:
    """Standard contracts traded on ``valuation_date`` with the stated recovery.

    Each quote is a standard contract: accrual from the standard accrual start of the
    trade, maturity as quoted. The recovery is used both to build the curve and to
    price on it.
    """

    valuation_date: date
    quotes: tuple[CdsQuote, ...]
    recovery_rate: float
    convention: CdsConvention

    def __post_init__(self) -> None:
        quotes = tuple(sorted(self.quotes, key=lambda q: q.maturity))
        if not quotes:
            raise InvalidTerms("at least one CDS quote is required")
        for quote in quotes:
            if not isinstance(quote, (CdsSpreadQuote, CdsUpfrontQuote)):
                raise TypeError("quotes must be CdsSpreadQuote or CdsUpfrontQuote")
        maturities = [q.maturity for q in quotes]
        if len(set(maturities)) != len(maturities):
            raise InvalidTerms("CDS quote maturities must be unique")
        if not isinstance(self.convention, CdsConvention):
            raise TypeError("convention must be a CdsConvention")
        step_in = self.valuation_date + timedelta(days=self.convention.step_in_days)
        if maturities[0] < step_in:
            raise InvalidTerms("every CDS quote must mature on or after the step-in date")
        object.__setattr__(self, "quotes", quotes)
        object.__setattr__(self, "recovery_rate", _recovery(self.recovery_rate))


def _quote_plan(
    request: CdsCurveRequest, maturity: date, discount: DiscountFactorCurve, segments: _Segments
) -> _LegPlan:
    trade = cds_trade_dates(request.valuation_date, request.convention, maturity_date=maturity)
    dates = _contract_dates(
        request.valuation_date, trade.accrual_start_date, maturity, request.convention
    )
    return _build_plan(dates, request.convention, discount, segments)


def bootstrap_hazard_curve(
    request: CdsCurveRequest, discount_curve: DiscountFactorCurve
) -> HazardCurve:
    """Piecewise-constant hazard rates, one per quote, nodes at the quote maturities.

    Each hazard is solved in maturity order so that its contract's clean upfront
    matches the quote; the curve then reprices every quote within 1e-12 of notional.
    A quote below the zero-hazard value (negative hazard) is rejected.
    """
    return _bootstrap(request, discount_curve, None)


def _bootstrap(
    request: CdsCurveRequest,
    discount_curve: DiscountFactorCurve,
    guesses: Sequence[float] | None,
) -> HazardCurve:
    """The bootstrap; ``guesses`` (e.g. the unshocked hazards) only narrow the first
    bracket and change no result beyond the solver tolerance."""
    ends = tuple(q.maturity for q in request.quotes)
    segments = _Segments.build(request.valuation_date, ends, request.convention.hazard_day_count)
    recovery = request.recovery_rate
    rates: list[float] = [0.0] * len(ends)
    for i, quote in enumerate(request.quotes):
        plan = _quote_plan(request, quote.maturity, discount_curve, segments)
        coupon, target = _quote_terms(quote)

        def error(
            hazard: float,
            i: int = i,
            plan: _LegPlan = plan,
            coupon: float = coupon,
            target: float = target,
        ) -> float:
            rates[i] = hazard
            bases = segments.log_survival_bases(rates)
            protection, annuity = plan.evaluate(rates, bases)
            return _clean_upfront(protection, annuity, plan, coupon, recovery) - target

        bracket = None
        if guesses is not None and guesses[i] > 0.0:
            lower, upper = 0.9 * guesses[i], 1.1 * guesses[i]
            lower_value, upper_value = error(lower), error(upper)
            if lower_value < 0.0 < upper_value:
                bracket = (lower, upper, lower_value, upper_value)
        if bracket is None:
            at_zero = error(0.0)
            if abs(at_zero) <= _BOOTSTRAP_TOLERANCE:
                rates[i] = 0.0
                continue
            if at_zero > 0.0:
                raise InvalidTerms(
                    f"CDS quote maturing {quote.maturity.isoformat()} implies a negative "
                    "hazard rate"
                )
            upper = max(0.05, 2.0 * (coupon + max(target, 0.0)) / (1.0 - recovery))
            upper_value = error(upper)
            while upper_value < 0.0:
                if upper >= _MAX_HAZARD:
                    raise InvalidTerms(
                        f"CDS quote maturing {quote.maturity.isoformat()} needs a hazard rate "
                        f"above {_MAX_HAZARD}"
                    )
                upper = min(2.0 * upper, _MAX_HAZARD)
                upper_value = error(upper)
            bracket = (0.0, upper, at_zero, upper_value)
        result = illinois(
            error,
            bracket[0],
            bracket[1],
            tolerance=_BOOTSTRAP_TOLERANCE,
            lower_value=bracket[2],
            upper_value=bracket[3],
            label="CDS hazard bootstrap",
        )
        # Later hazards never enter this quote's value, so the solver residual is the
        # repricing error of the finished curve.
        if abs(result.value) > _REPRICING_TOLERANCE:
            raise RuntimeError("bootstrapped hazard curve failed exact repricing")
        rates[i] = result.root
    return HazardCurve(
        request.valuation_date,
        tuple(zip(ends, rates)),
        request.convention.hazard_day_count,
    )


def flat_hazard_from_spread(
    spread: float,
    *,
    valuation_date: date,
    maturity_date: date,
    recovery_rate: float,
    discount_curve: DiscountFactorCurve,
    convention: CdsConvention,
) -> float:
    """The flat hazard at which the standard contract's par spread is ``spread``."""
    request = CdsCurveRequest(
        valuation_date, (CdsSpreadQuote(maturity_date, spread),), recovery_rate, convention
    )
    return bootstrap_hazard_curve(request, discount_curve).nodes[0][1]


def flat_hazard_from_upfront(
    upfront: float,
    coupon: float,
    *,
    valuation_date: date,
    maturity_date: date,
    recovery_rate: float,
    discount_curve: DiscountFactorCurve,
    convention: CdsConvention,
) -> float:
    """The flat hazard at which the standard contract's clean upfront is ``upfront``."""
    request = CdsCurveRequest(
        valuation_date,
        (CdsUpfrontQuote(maturity_date, coupon, upfront),),
        recovery_rate,
        convention,
    )
    return bootstrap_hazard_curve(request, discount_curve).nodes[0][1]


def _standard_legs(
    hazard: HazardCurve,
    valuation_date: date,
    maturity_date: date,
    discount_curve: DiscountFactorCurve,
    convention: CdsConvention,
) -> tuple[float, float, _LegPlan]:
    trade = cds_trade_dates(valuation_date, convention, maturity_date=maturity_date)
    dates = _contract_dates(valuation_date, trade.accrual_start_date, maturity_date, convention)
    plan = _build_plan(dates, convention, discount_curve, hazard._segments())
    protection, annuity = _evaluate_curve(plan, hazard)
    return protection, annuity, plan


def cds_par_spread(
    hazard_curve: HazardCurve,
    *,
    valuation_date: date,
    maturity_date: date,
    recovery_rate: float,
    discount_curve: DiscountFactorCurve,
    convention: CdsConvention,
) -> float:
    """Par spread of the standard contract on a hazard curve (clean upfront zero)."""
    protection, annuity, plan = _standard_legs(
        hazard_curve, valuation_date, maturity_date, discount_curve, convention
    )
    clean_annuity = annuity - plan.accrued_fraction * plan.cash_settlement_df
    return (
        (1.0 - _recovery(recovery_rate))
        * protection
        / positive_money(clean_annuity, "clean risky annuity")
    )


def upfront_from_spread(
    spread: float,
    coupon: float,
    *,
    valuation_date: date,
    maturity_date: date,
    recovery_rate: float,
    discount_curve: DiscountFactorCurve,
    convention: CdsConvention,
) -> float:
    """Clean upfront per unit notional (buyer pays, at cash settlement) of a standard
    contract with running ``coupon`` quoted at a flat ``spread`` (the ISDA convention)."""
    hazard = flat_hazard_from_spread(
        spread,
        valuation_date=valuation_date,
        maturity_date=maturity_date,
        recovery_rate=recovery_rate,
        discount_curve=discount_curve,
        convention=convention,
    )
    curve = HazardCurve.flat(valuation_date, hazard, convention.hazard_day_count)
    protection, annuity, plan = _standard_legs(
        curve, valuation_date, maturity_date, discount_curve, convention
    )
    return _clean_upfront(
        protection, annuity, plan, non_negative_money(coupon, "coupon"), _recovery(recovery_rate)
    )


def spread_from_upfront(
    upfront: float,
    coupon: float,
    *,
    valuation_date: date,
    maturity_date: date,
    recovery_rate: float,
    discount_curve: DiscountFactorCurve,
    convention: CdsConvention,
) -> float:
    """The flat quoted spread equivalent to a clean upfront for running ``coupon``."""
    hazard = flat_hazard_from_upfront(
        upfront,
        coupon,
        valuation_date=valuation_date,
        maturity_date=maturity_date,
        recovery_rate=recovery_rate,
        discount_curve=discount_curve,
        convention=convention,
    )
    return cds_par_spread(
        HazardCurve.flat(valuation_date, hazard, convention.hazard_day_count),
        valuation_date=valuation_date,
        maturity_date=maturity_date,
        recovery_rate=recovery_rate,
        discount_curve=discount_curve,
        convention=convention,
    )


# ---------------------------------------------------------------- contracts and valuation


def _code(value: str, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InvalidTerms(f"{label} must be nonempty text")
    return value


@dataclass(frozen=True)
class CdsContract:
    """Terms of one CDS position; money is in ``currency``.

    ``accrual_start_date`` is the contract's first accrual date (for a standard trade,
    :func:`cds_trade_dates` of its trade date); ``maturity_date`` is the unadjusted
    scheduled termination date. ``coupon`` is the decimal running premium.
    """

    contract_id: str
    reference_entity: str
    currency: str
    side: CdsSide
    notional: float
    coupon: float
    accrual_start_date: date
    maturity_date: date
    convention: CdsConvention

    def __post_init__(self) -> None:
        _code(self.contract_id, "contract_id")
        _code(self.reference_entity, "reference_entity")
        if (
            not isinstance(self.currency, str)
            or len(self.currency) != 3
            or not self.currency.isascii()
            or not self.currency.isalpha()
            or not self.currency.isupper()
        ):
            raise InvalidTerms("currency must be a three-letter upper-case ISO code")
        if not isinstance(self.side, CdsSide):
            raise TypeError("side must be a CdsSide")
        if not isinstance(self.convention, CdsConvention):
            raise TypeError("convention must be a CdsConvention")
        for name in ("accrual_start_date", "maturity_date"):
            if not isinstance(getattr(self, name), date):
                raise TypeError(f"{name} must be a date")
        if self.maturity_date <= self.accrual_start_date:
            raise InvalidTerms("CDS maturity must follow the accrual start")
        object.__setattr__(self, "notional", positive_money(self.notional, "notional"))
        object.__setattr__(self, "coupon", non_negative_money(self.coupon, "coupon"))


@dataclass(frozen=True)
class CdsValuation:
    """Value of a contract on a hazard curve, signed for its side, contract currency.

    - ``pv``: dirty value today, i.e. protection minus the risky premium including the
      full current coupon (the buyer's sign; negated for a seller);
    - ``accrued_premium``: premium accrued to the step-in date (owed by the buyer),
      signed so that it is negative for a buyer;
    - ``clean_pv``: ``pv`` without the accrued premium, discounted from cash settlement;
    - ``cash_settlement_amount``: ``pv`` forward to cash settlement (unwind cash);
    - ``par_spread``, ``risky_annuity`` (clean, per unit coupon and notional, PV
      today) and ``points_upfront`` (buyer's clean upfront per unit notional at cash
      settlement, the market quote) do not depend on the side.
    """

    contract_id: str
    side: CdsSide
    notional: float
    coupon: float
    recovery_rate: float
    step_in_date: date
    cash_settlement_date: date
    protection_leg_pv: float
    premium_leg_pv: float
    accrued_premium: float
    pv: float
    clean_pv: float
    cash_settlement_amount: float
    par_spread: float
    risky_annuity: float
    points_upfront: float
    model_version: str = CDS_MODEL_VERSION


def _valuation(
    contract: CdsContract,
    plan: _LegPlan,
    dates: _ContractDates,
    protection: float,
    annuity: float,
    recovery: float,
) -> CdsValuation:
    n, c, sign = contract.notional, contract.coupon, contract.side.sign
    df = plan.cash_settlement_df
    protection_pv = n * (1.0 - recovery) * protection
    premium_pv = n * c * annuity
    accrued = n * c * plan.accrued_fraction
    pv = sign * (protection_pv - premium_pv)
    clean = pv + sign * accrued * df
    clean_annuity = positive_money(annuity - plan.accrued_fraction * df, "clean risky annuity")
    return CdsValuation(
        contract_id=contract.contract_id,
        side=contract.side,
        notional=n,
        coupon=c,
        recovery_rate=recovery,
        step_in_date=dates.step_in_date,
        cash_settlement_date=dates.cash_settlement_date,
        protection_leg_pv=finite_money(protection_pv, "protection leg PV"),
        premium_leg_pv=finite_money(premium_pv, "premium leg PV"),
        accrued_premium=finite_money(-sign * accrued, "accrued premium"),
        pv=finite_money(pv, "CDS PV"),
        clean_pv=finite_money(clean, "clean CDS PV"),
        cash_settlement_amount=finite_money(pv / df, "cash settlement amount"),
        par_spread=finite_money((1.0 - recovery) * protection / clean_annuity, "par spread"),
        risky_annuity=finite_money(clean_annuity, "risky annuity"),
        points_upfront=finite_money(
            _clean_upfront(protection, annuity, plan, c, recovery), "points upfront"
        ),
    )


def value_cds(
    contract: CdsContract,
    *,
    valuation_date: date,
    discount_curve: DiscountFactorCurve,
    hazard_curve: HazardCurve,
    recovery_rate: float,
) -> CdsValuation:
    """Value a contract on given curves (both dated on ``valuation_date``)."""
    recovery = _recovery(recovery_rate)
    dates = _contract_dates(
        valuation_date, contract.accrual_start_date, contract.maturity_date, contract.convention
    )
    plan = _build_plan(dates, contract.convention, discount_curve, hazard_curve._segments())
    protection, annuity = _evaluate_curve(plan, hazard_curve)
    return _valuation(contract, plan, dates, protection, annuity, recovery)


# ---------------------------------------------------------------- risk


@dataclass(frozen=True)
class CdsScenario:
    """Absolute bp shifts of the 11 discount buckets and of every quoted spread, and an
    absolute recovery shift (decimal: 0.05 = five points)."""

    curve_shifts_bp: tuple[float, ...] = _ZERO_SHOCK
    spread_shift_bp: float = 0.0
    recovery_shift: float = 0.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "curve_shifts_bp", _shifts(tuple(self.curve_shifts_bp)))
        object.__setattr__(
            self, "spread_shift_bp", finite_money(self.spread_shift_bp, "spread shift bp")
        )
        object.__setattr__(
            self, "recovery_shift", finite_money(self.recovery_shift, "recovery shift")
        )

    @classmethod
    def parallel(
        cls,
        curve_shift_bp: float = 0.0,
        *,
        spread_shift_bp: float = 0.0,
        recovery_shift: float = 0.0,
    ) -> CdsScenario:
        return cls((curve_shift_bp,) * len(RISK_BUCKETS), spread_shift_bp, recovery_shift)


_Key = tuple[tuple[float, ...], float, float, int]


class CdsCurveCache:
    """Memoize hazard bootstraps by credit request, discount input and shocks.

    Positions on one reference entity share every shocked credit curve within a job:
    about 40 per entity for first-order risk and 330 with the Hessian, so the default
    holds about 200 entities with Hessians. Failed bootstraps are not cached; past
    ``max_entries`` curves are built but not stored, which changes no number.
    """

    def __init__(self, max_entries: int = 65536) -> None:
        if isinstance(max_entries, bool) or not isinstance(max_entries, int) or max_entries < 0:
            raise ValueError("max_entries must be a nonnegative integer")
        self.max_entries = max_entries
        self.hits = 0
        self.misses = 0
        self._curves: dict[object, HazardCurve] = {}

    def __len__(self) -> int:
        return len(self._curves)

    def get(self, key: object, build: Callable[[], HazardCurve]) -> HazardCurve:
        cached = self._curves.get(key)
        if cached is not None:
            self.hits += 1
            return cached
        self.misses += 1
        curve = build()
        if len(self._curves) < self.max_entries:
            self._curves[key] = curve
        return curve


def _shift_quote(
    quote: CdsQuote, request: CdsCurveRequest, discount: DiscountFactorCurve, shift_bp: float
) -> CdsQuote:
    """Move a quote by ``shift_bp`` of spread. Upfront quotes move in their flat quoted
    spread: convert, shift and convert back on the same discount curve."""
    shift = shift_bp * 1e-4
    if isinstance(quote, CdsSpreadQuote):
        moved = quote.par_spread + shift
        if moved < 0.0:
            raise InvalidTerms("a spread shock cannot make a par spread negative")
        return replace(quote, par_spread=moved)

    def convert(function: Callable[..., float], value: float) -> float:
        return function(
            value,
            quote.coupon,
            valuation_date=request.valuation_date,
            maturity_date=quote.maturity,
            recovery_rate=request.recovery_rate,
            discount_curve=discount,
            convention=request.convention,
        )

    spread = convert(spread_from_upfront, quote.upfront)
    if spread + shift < 0.0:
        raise InvalidTerms("a spread shock cannot make a quoted spread negative")
    upfront = convert(upfront_from_spread, spread + shift)
    return replace(quote, upfront=upfront)


@dataclass(frozen=True)
class CdsRiskRequest:
    """A contract, its valuation date, discount curve input and credit quotes.

    The discount input selects the rate risk basis as for bonds (``DiscountCurve`` =
    zero-rate overlay, ``CurveBootstrapRequest`` = OIS quote bump and rebootstrap, a
    par-yield proxy request = par-yield quote bump and rebuild);
    both must be dated on ``valuation_date``. The credit curve is always the quotes,
    so every shock holds quoted spreads (or upfronts) fixed and rebootstraps hazard.
    """

    contract: CdsContract
    valuation_date: date
    discount_curve: DiscountCurve | CurveBootstrapRequest | QuoteCurve
    credit_curve: CdsCurveRequest

    def __post_init__(self) -> None:
        if not isinstance(self.contract, CdsContract):
            raise TypeError("contract must be a CdsContract")
        if not isinstance(self.credit_curve, CdsCurveRequest):
            raise TypeError("credit_curve must be a CdsCurveRequest")
        curve = self.discount_curve
        if not isinstance(curve, (DiscountCurve, CurveBootstrapRequest, QuoteCurve)) or (
            isinstance(curve, QuoteCurve) and curve.needs_rub
        ):
            raise TypeError(
                "discount_curve must be DiscountCurve, CurveBootstrapRequest or par yields"
            )
        if self.discount_curve.curve_date != self.valuation_date:
            raise InvalidTerms("the discount curve must be dated on the valuation date")
        if self.credit_curve.valuation_date != self.valuation_date:
            raise InvalidTerms("the credit quotes must be dated on the valuation date")
        if self.credit_curve.convention.hazard_day_count != (
            self.contract.convention.hazard_day_count
        ):
            raise InvalidTerms("contract and credit curve hazard day counts differ")
        object.__setattr__(self, "discount_curve", _snapshot_curve(self.discount_curve))


@dataclass(frozen=True)
class CdsBucketRisk:
    bucket: str
    dv01: float


@dataclass(frozen=True)
class CdsQuoteRisk:
    """Sensitivity to one quote's spread at the quote's maturity."""

    maturity: date
    cs01: float


@dataclass(frozen=True)
class CdsRiskResult:
    """Signed sensitivities in the contract currency per bp (recovery: per 0.01).

    - ``buckets``/``dv01``: discount-curve risk per bucket and their sum;
    - ``parallel_dv01``: all buckets bumped together and repriced;
    - ``cs01``: all quoted spreads moved together (desk factor ``CS``);
    - ``quote_buckets``: one quote at a time, summing to about ``cs01``;
    - ``recovery01``: recovery up by one point, quotes held, curve rebootstrapped.

    Every shock holds the credit quotes and rebootstraps the hazard curve.
    """

    base_id: str
    contract_id: str
    currency: str
    side: CdsSide
    curve_basis: str
    difference: str
    bump_bp: float
    recovery_bump: float
    valuation: CdsValuation
    buckets: tuple[CdsBucketRisk, ...]
    dv01: float
    parallel_dv01: float
    cs01: float
    quote_buckets: tuple[CdsQuoteRisk, ...]
    recovery01: float
    model_version: str = CDS_RISK_MODEL_VERSION


@dataclass(frozen=True)
class CdsScenarioResult:
    base_id: str
    scenario_id: str
    curve_basis: str
    base_pv: float
    shocked_pv: float
    rates_pnl: float
    spread_pnl: float
    recovery_pnl: float
    interaction_pnl: float
    total_pnl: float
    model_version: str = CDS_RISK_MODEL_VERSION


@dataclass(frozen=True)
class CdsSecondOrderRisk:
    """Central gradient (per bp) and full symmetric Hessian (per bp squared)."""

    base_id: str
    curve_basis: str
    factors: tuple[str, ...]
    bump_bp: float
    gradient_per_bp: tuple[float, ...]
    hessian_per_bp2: tuple[tuple[float, ...], ...]
    model_version: str = CDS_SECOND_ORDER_MODEL_VERSION


@dataclass(frozen=True)
class CdsDefaultResult:
    """Immediate default at the step-in date, signed like bond JTD (DEC07).

    ``jtd = side * notional * ((1 - R) - coupon * accrued fraction) - pv``: the buyer
    receives the loss given default and pays the accrued premium, and the contract's
    value is gone. Positive for a buyer of protection at a fair price, negative for a
    seller, as for a bondholder.
    """

    base_id: str
    default_id: str
    recovery_rate: float
    default_settlement: float
    base_pv: float
    jtd_pnl: float


@dataclass(frozen=True)
class CdsHorizonValuation:
    """Carry and rolldown from the valuation date to ``horizon_date``, conditional on
    no default before the horizon; signed for the side, contract currency.

    - ``received``: premium coupons that leave the contract's value between the two
      step-in dates (accrual ending after today's step-in and by the horizon's), as
      undiscounted cash: negative for a buyer, who pays them;
    - ``pv_forward``: the contract at the horizon on today's curves read forward
      (:func:`~rub_fixed_income.horizon.forward_curve`, :func:`forward_hazard_curve`);
    - ``pv_rolled``: the contract at the horizon on rolled curves, shape held in
      calendar days (:func:`~rub_fixed_income.horizon.roll_curve`,
      :func:`roll_hazard_curve`): the theta at constant spreads per tenor;
    - ``accrued_start``/``accrued_horizon``: :attr:`CdsValuation.accrued_premium` today
      and on the rolled valuation.

    ``carry = pv_forward + received - pv_start``, ``rolldown = pv_rolled - pv_forward``
    and ``horizon_pnl = pv_rolled + received - pv_start``. A contract whose step-in at
    the horizon passes its maturity has every remaining coupon received and zero
    horizon value.
    """

    base_id: str
    contract_id: str
    currency: str
    side: CdsSide
    valuation_date: date
    horizon_date: date
    pv_start: float
    pv_forward: float
    pv_rolled: float
    received: float
    accrued_start: float
    accrued_horizon: float
    carry: float
    rolldown: float
    horizon_pnl: float
    model_version: str = CDS_HORIZON_MODEL_VERSION


_Moves = tuple[tuple[float, ...], float, float]
_NO_MOVES: _Moves = (_ZERO_SHOCK, 0.0, 0.0)


@dataclass(frozen=True)
class CdsRiskEngine:
    """Bootstrap the credit curve once per shock; value the contract on it.

    Shocks: discount buckets (OIS rebootstrap or zero overlay), a parallel shift of
    every quoted spread, and a recovery shift. Each rebuilds the hazard curve from
    the unchanged quotes, the ISDA practice for quoted CDS risk.
    """

    request: CdsRiskRequest
    curve_cache: ShockedCurveCache = field(
        default_factory=ShockedCurveCache, compare=False, repr=False
    )
    credit_cache: CdsCurveCache = field(default_factory=CdsCurveCache, compare=False, repr=False)
    valuation: CdsValuation = field(init=False)
    hazard_curve: HazardCurve = field(init=False)
    base_pv: float = field(init=False)
    base_id: str = field(init=False)
    _discount: DiscountCurve = field(init=False, repr=False)
    _dates: _ContractDates = field(init=False, repr=False)
    _plans: dict[tuple[tuple[float, ...], tuple[date, ...]], _LegPlan] = field(
        init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        object.__setattr__(self, "_plans", {})
        if not isinstance(self.curve_cache, ShockedCurveCache):
            raise TypeError("curve_cache must be a ShockedCurveCache")
        if not isinstance(self.credit_cache, CdsCurveCache):
            raise TypeError("credit_cache must be a CdsCurveCache")
        req = self.request
        contract = req.contract
        object.__setattr__(self, "_discount", _base_curve(req.discount_curve, self.curve_cache))
        object.__setattr__(
            self,
            "_dates",
            _contract_dates(
                req.valuation_date,
                contract.accrual_start_date,
                contract.maturity_date,
                contract.convention,
            ),
        )
        hazard = self._hazard(_NO_MOVES)
        object.__setattr__(self, "hazard_curve", hazard)
        valuation = self._value(_NO_MOVES)
        object.__setattr__(self, "valuation", valuation)
        object.__setattr__(self, "base_pv", valuation.pv)
        object.__setattr__(
            self,
            "base_id",
            canonical_sha256(
                {
                    "model": CDS_RISK_MODEL_VERSION,
                    "contract": contract,
                    "convention": contract.convention.identifier,
                    "valuation_date": req.valuation_date,
                    "curve_basis": self.curve_basis,
                    "discount_curve": _curve_identity(req.discount_curve),
                    "credit_quotes": req.credit_curve.quotes,
                    "credit_convention": req.credit_curve.convention.identifier,
                    "recovery_rate": req.credit_curve.recovery_rate,
                }
            ),
        )

    @property
    def curve_basis(self) -> str:
        return _curve_basis(self.request.discount_curve)

    def _discount_view(self, shifts: tuple[float, ...]) -> DiscountFactorCurve:
        return _shock_curve(self.request.discount_curve, self._discount, shifts, self.curve_cache)

    def _hazard(self, moves: _Moves, quote_index: int = -1) -> HazardCurve:
        shifts, spread_shift, recovery_shift = moves
        credit = self.request.credit_curve
        source = self.request.discount_curve
        key = (credit, source, (shifts, spread_shift, recovery_shift, quote_index))

        def build() -> HazardCurve:
            discount = self._discount_view(shifts)
            recovery = credit.recovery_rate + recovery_shift
            if not 0.0 <= recovery < 1.0:
                raise InvalidTerms("a recovery shock must keep recovery in [0, 1)")
            quotes = credit.quotes
            if spread_shift != 0.0:
                quotes = tuple(
                    _shift_quote(q, credit, discount, spread_shift) if quote_index in (-1, i) else q
                    for i, q in enumerate(quotes)
                )
            shocked = replace(credit, quotes=quotes, recovery_rate=recovery)
            base = self.__dict__.get("hazard_curve")
            guesses = None if base is None else base.hazard_rates
            return _bootstrap(shocked, discount, guesses)

        return self.credit_cache.get(key, build)

    def _value(self, moves: _Moves, quote_index: int = -1) -> CdsValuation:
        try:
            hazard = self._hazard(moves, quote_index)
            contract = self.request.contract
            # The plan depends only on the discount shock and the hazard node dates
            # (the quote maturities), so spread and recovery shocks reuse it.
            key = (moves[0], hazard.node_dates)
            plan = self._plans.get(key)
            if plan is None:
                discount = self._discount_view(moves[0])
                plan = _build_plan(self._dates, contract.convention, discount, hazard._segments())
                if len(self._plans) < _PLAN_MEMO_SIZE:
                    self._plans[key] = plan
            protection, annuity = _evaluate_curve(plan, hazard)
            recovery = self.request.credit_curve.recovery_rate + moves[2]
            return _valuation(contract, plan, self._dates, protection, annuity, recovery)
        except (OverflowError, ZeroDivisionError) as exc:
            raise ValueError("shock is outside the supported numerical domain") from exc

    def _pv(self, moves: _Moves, quote_index: int = -1) -> float:
        return self._value(moves, quote_index).pv

    def risk(
        self,
        *,
        bump_bp: float = 1.0,
        recovery_bump: float = 0.01,
        difference: Literal["forward", "central"] = "central",
    ) -> CdsRiskResult:
        """Bucketed and parallel IR DV01, CS01 (parallel and per quote), recovery01.

        Central (the default, DEC08) estimates derivatives per bp; forward is the
        exact +1 bp P&L. Recovery is always central: per 0.01 of recovery.
        """
        bump = positive_money(bump_bp, "risk bump bp")
        rec = positive_money(recovery_bump, "recovery bump")
        if difference not in ("forward", "central"):
            raise ValueError("difference must be forward or central")
        base = self.base_pv

        def sensitivity(moves: _Moves, quote_index: int = -1) -> float:
            up = self._pv(moves, quote_index)
            if difference == "central":
                shifts, spread, recovery = moves
                down = self._pv((tuple(-s for s in shifts), -spread, -recovery), quote_index)
                return finite_money((up - down) / (2.0 * bump), "sensitivity")
            return finite_money((up - base) / bump, "sensitivity")

        size = len(RISK_BUCKETS)
        buckets = tuple(
            CdsBucketRisk(
                bucket,
                sensitivity((tuple(bump if i == j else 0.0 for j in range(size)), 0.0, 0.0)),
            )
            for i, bucket in enumerate(RISK_BUCKETS)
        )
        quote_buckets = tuple(
            CdsQuoteRisk(q.maturity, sensitivity((_ZERO_SHOCK, bump, 0.0), i))
            for i, q in enumerate(self.request.credit_curve.quotes)
        )
        recovery01 = finite_money(
            (self._pv((_ZERO_SHOCK, 0.0, rec)) - self._pv((_ZERO_SHOCK, 0.0, -rec)))
            / (2.0 * rec)
            * 0.01,
            "recovery01",
        )
        contract = self.request.contract
        return CdsRiskResult(
            base_id=self.base_id,
            contract_id=contract.contract_id,
            currency=contract.currency,
            side=contract.side,
            curve_basis=self.curve_basis,
            difference=difference,
            bump_bp=bump,
            recovery_bump=rec,
            valuation=self.valuation,
            buckets=buckets,
            dv01=finite_money(math.fsum(b.dv01 for b in buckets), "DV01"),
            parallel_dv01=sensitivity(((bump,) * size, 0.0, 0.0)),
            cs01=sensitivity((_ZERO_SHOCK, bump, 0.0)),
            quote_buckets=quote_buckets,
            recovery01=recovery01,
        )

    def reprice(self, scenario: CdsScenario) -> CdsScenarioResult:
        """Exact P&L of a scenario, each factor group isolated, and their interaction."""
        if not isinstance(scenario, CdsScenario):
            raise TypeError("scenario must be a CdsScenario")
        shifts, spread, recovery = (
            scenario.curve_shifts_bp,
            scenario.spread_shift_bp,
            scenario.recovery_shift,
        )
        base = self.base_pv

        def pnl(moves: _Moves, label: str) -> float:
            return finite_money(self._pv(moves) - base, label)

        rates = pnl((shifts, 0.0, 0.0), "rates PnL")
        spread_pnl = pnl((_ZERO_SHOCK, spread, 0.0), "spread PnL")
        recovery_pnl = pnl((_ZERO_SHOCK, 0.0, recovery), "recovery PnL")
        shocked = self._pv((shifts, spread, recovery))
        total = finite_money(shocked - base, "total PnL")
        return CdsScenarioResult(
            base_id=self.base_id,
            scenario_id=canonical_sha256(scenario),
            curve_basis=self.curve_basis,
            base_pv=base,
            shocked_pv=shocked,
            rates_pnl=rates,
            spread_pnl=spread_pnl,
            recovery_pnl=recovery_pnl,
            interaction_pnl=finite_money(
                total - math.fsum((rates, spread_pnl, recovery_pnl)), "interaction PnL"
            ),
            total_pnl=total,
        )

    def second_order(self, *, bump_bp: float = 1.0) -> CdsSecondOrderRisk:
        """Central gradient and full Hessian over the 11 discount buckets and ``CS``."""
        bump = positive_money(bump_bp, "second-order bump bp")
        size = len(RISK_BUCKETS)

        def price(moves: list[float]) -> float:
            return self._pv((tuple(moves[:size]), moves[size], 0.0))

        gradient, hessian = _central_derivatives(price, size + 1, bump, self.base_pv, 1.0)
        return CdsSecondOrderRisk(
            base_id=self.base_id,
            curve_basis=self.curve_basis,
            factors=(*RISK_BUCKETS, CS_FACTOR),
            bump_bp=bump,
            gradient_per_bp=gradient,
            hessian_per_bp2=hessian,
        )

    def jump_to_default(self, *, recovery_rate: float) -> CdsDefaultResult:
        """Immediate default P&L with an explicit realised recovery (DEC07 sign)."""
        recovery = non_negative_money(recovery_rate, "recovery rate")
        if recovery > 1.0:
            raise InvalidTerms("recovery rate must be between 0 and 1")
        contract = self.request.contract
        settlement = (
            contract.side.sign
            * contract.notional
            * ((1.0 - recovery) - contract.coupon * self._dates.accrued_fraction)
        )
        return CdsDefaultResult(
            base_id=self.base_id,
            default_id=canonical_sha256(
                {
                    "base_id": self.base_id,
                    "recovery_rate": recovery,
                    "model": "cds-immediate-default-at-step-in-v1",
                }
            ),
            recovery_rate=recovery,
            default_settlement=finite_money(settlement, "default settlement"),
            base_pv=self.base_pv,
            jtd_pnl=finite_money(settlement - self.base_pv, "JTD PnL"),
        )

    def horizon(self, horizon_date: date) -> CdsHorizonValuation:
        """Carry and rolldown to ``horizon_date`` (see :class:`CdsHorizonValuation`)."""
        valuation = self.request.valuation_date
        if not isinstance(horizon_date, date) or horizon_date <= valuation:
            raise InvalidTerms("horizon date must be after the valuation date")
        contract = self.request.contract
        recovery = self.request.credit_curve.recovery_rate
        horizon_step_in = horizon_date + timedelta(days=contract.convention.step_in_days)
        # Coupons (a buyer pays: negative) of the periods ending by the horizon's
        # step-in: they are in the start value and not in the horizon value.
        premium = -contract.side.sign * contract.notional * contract.coupon
        received = math.fsum(
            premium * p.year_fraction
            for p in self._dates.periods
            if p.accrual_end <= horizon_step_in
        )
        pv_forward = pv_rolled = accrued_horizon = 0.0
        if horizon_step_in <= contract.maturity_date:
            discount, hazard = self._discount, self.hazard_curve

            def value(curve: DiscountFactorCurve, credit: HazardCurve) -> CdsValuation:
                return value_cds(
                    contract,
                    valuation_date=horizon_date,
                    discount_curve=curve,
                    hazard_curve=credit,
                    recovery_rate=recovery,
                )

            pv_forward = value(
                forward_curve(discount, horizon_date), forward_hazard_curve(hazard, horizon_date)
            ).pv
            rolled = value(
                roll_curve(discount, horizon_date), roll_hazard_curve(hazard, horizon_date)
            )
            pv_rolled, accrued_horizon = rolled.pv, rolled.accrued_premium
        base = self.base_pv
        return CdsHorizonValuation(
            base_id=self.base_id,
            contract_id=contract.contract_id,
            currency=contract.currency,
            side=contract.side,
            valuation_date=valuation,
            horizon_date=horizon_date,
            pv_start=base,
            pv_forward=finite_money(pv_forward, "forward PV"),
            pv_rolled=finite_money(pv_rolled, "rolled PV"),
            received=finite_money(received, "received"),
            accrued_start=self.valuation.accrued_premium,
            accrued_horizon=finite_money(accrued_horizon, "horizon accrued premium"),
            carry=finite_money(pv_forward + received - base, "carry"),
            rolldown=finite_money(pv_rolled - pv_forward, "rolldown"),
            horizon_pnl=finite_money(pv_rolled + received - base, "horizon P&L"),
        )


def cds_factor_risk(
    risk: CdsRiskResult, discount_curve_id: str, second_order: CdsSecondOrderRisk | None = None
) -> FactorRisk:
    """Map CDS risk to desk factors: ``IR|<curve>|<bucket>`` and ``CS`` (all quoted
    spreads, 1 bp). Recovery is not a desk factor; it stays in ``risk.recovery01``."""
    factors = (*(ir_factor(discount_curve_id, b) for b in RISK_BUCKETS), CS_FACTOR)
    if second_order is not None:
        if second_order.base_id != risk.base_id:
            raise InvalidTerms("risk and second-order results describe different calibrations")
        upper = tuple(
            HessianEntry(i, j, row[j])
            for i, row in enumerate(second_order.hessian_per_bp2)
            for j in range(i, len(row))
            if row[j] != 0.0
        )
        return FactorRisk(
            factors, second_order.gradient_per_bp, upper, "central", second_order.bump_bp, True
        )
    if risk.difference != "central":
        raise InvalidTerms("desk factors need central-difference risk (DEC08)")
    first = (*(b.dv01 for b in risk.buckets), risk.cs01)
    return FactorRisk(factors, first, (), "central", risk.bump_bp, False)


def cds_horizon(
    request: CdsRiskRequest,
    *,
    horizon_date: date,
    curve_cache: ShockedCurveCache | None = None,
    cds_cache: CdsCurveCache | None = None,
) -> CdsHorizonValuation:
    """Carry and rolldown of a CDS to ``horizon_date`` on the request's base curves."""
    engine = CdsRiskEngine(
        request,
        ShockedCurveCache() if curve_cache is None else curve_cache,
        CdsCurveCache() if cds_cache is None else cds_cache,
    )
    return engine.horizon(horizon_date)
