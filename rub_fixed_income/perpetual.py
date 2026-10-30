"""The coupon stream of a perpetual bond that is never called (BACKLOG #125).

Russian perpetuals (bank AT1 and others) are rarely called in practice, so the desk
values them by default as an endless stream of coupons. A
:class:`PerpetualTail` describes the coupons after the last listed one: the same
amount, forever, every ``period_days`` days or ``period_months`` months from the
anchor (the last listed payment date). It is a term of the valuation, built by
:func:`perpetual_tail` from the listed coupons; the listed coupons themselves stay
ordinary cash flows.

The tail is valued exactly, without materialising its flows on the instrument:

- on a curve plus a Z-spread, each tail coupon is discounted like any other flow
  (``compatibility.price_cashflows``: the settlement-forward zero rate in the spread
  convention plus the spread). Discount factors are read from the curve up to
  :data:`TAIL_CURVE_YEARS` (30) years from the curve date; beyond, where every curve
  core builds is flat-forward (the KBD materialises to 30 years, RUONIA OIS to 10) and
  the bucket shocks are flat (20Y+), they follow the curve's own flat-forward rate,
  read from the curve and checked. A curve that is not flat-forward there (an
  annual-yield shock of the 20Y+ bucket bends it) is read date by date instead;
- at an effective yield, ``sum a (1 + y)^-t``.

A perpetual whose coupon follows a formula (0.41.0, BACKLOG #127: a RUONIA or key-rate
floater, a coupon reset every N years to the N-year KBD) continues its floating leg
instead: :func:`floating_perpetual_tail` lists the periods after the last listed one at
the same step until one (a reset leg: a whole cycle) starts :data:`FLOATING_EXPLICIT_YEARS`
(35) years past settlement, each projected as any floating coupon; past them every
projection curve core builds is flat-forward (and every bucket shock flat), so the rates
are constant and a tail with ``amount=None`` continues the last coupon scaled to the mean
step (:meth:`PerpetualTail.projected`), re-projected on each curve by the risk engine.

The sum runs to :data:`TAIL_SUM_YEARS` (600) years past the anchor and adds the
geometric remainder of its last two terms. A stream that does not converge (a total
discount rate at or below zero) is worth infinity: the pricing functions return
``inf`` and the solvers move their brackets past it.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from functools import lru_cache
from itertools import pairwise
from typing import TYPE_CHECKING, cast

import numpy as np

from ._money import finite_money, positive_money
from ._risk_common import RISK_BUCKETS, _zero_shock_terms, _ZeroShockCurve
from .canonical import OMIT_IF_DEFAULT
from .conventions import DayCount, YieldConvention, ZSpreadConvention
from .dates import add_months
from .discount import DiscountCurve, DiscountFactorCurve
from .errors import InvalidTerms

if TYPE_CHECKING:
    from .domain import CouponPeriod, FloatingCouponLeg, RateCouponPeriod
    from .floating import FloatingCouponPeriod, FloatingCouponProjection

__all__ = [
    "FLOATING_EXPLICIT_YEARS",
    "PERPETUAL_TAIL_MODEL_VERSION",
    "TAIL_CURVE_YEARS",
    "TAIL_SUM_YEARS",
    "PerpetualTail",
    "PerpetualYield",
    "floating_perpetual_tail",
    "perpetual_tail",
    "perpetual_yield",
    "tail_pv_at_spread",
    "tail_pv_at_yield",
]

PERPETUAL_TAIL_MODEL_VERSION = "rub-perpetual-tail-v1"
TAIL_CURVE_YEARS = 30
TAIL_SUM_YEARS = 600
# A floating perpetual's leg is listed explicitly until its last period (a reset leg: its
# last cycle) starts this many years past settlement: past 30 years from the curve date
# every curve core builds is flat-forward, so with five years of margin for a horizon
# every later coupon is projected at the last one's rate.
FLOATING_EXPLICIT_YEARS = 35
_MAX_CONTINUED_PERIODS = 5000
_MEAN_MONTH_DAYS = 365.2425 / 12.0  # the Gregorian mean
# Discount factors are read from the curve up to this many days after the curve date:
# 30 years and their leap days. The KBD materialises nodes to 365 * 30 + 7 days.
_CURVE_DAYS = 365 * TAIL_CURVE_YEARS + 8
# The flat-forward rate beyond is read over ten years and checked at the midpoint.
_FORWARD_SPAN_DAYS = 3650
_FLAT_TOLERANCE = 1e-9
_SHOCK_TERMS_MEMO_SIZE = 64
_NORMAL_MIN = float(np.finfo(float).tiny)


@dataclass(frozen=True)
class PerpetualTail:
    """Coupons of ``amount`` per bond paid forever after ``anchor_date``.

    The k-th tail coupon (k >= 1) is paid ``k * period_days`` days or
    ``add_months(anchor_date, k * period_months)`` after the anchor; exactly one of
    the two steps is given. Amounts are in the instrument currency per bond.

    ``amount=None`` (0.41.0) continues a floating leg whose last period is paid on the
    anchor: each tail coupon is that period's projected coupon, re-projected on every
    curve (``BondRiskRequest(floating_leg=..., perpetual_tail=...)``); such a tail is
    priced only with its leg (:meth:`with_amount` fixes it).
    """

    anchor_date: date
    amount: float | None
    period_days: int | None = field(default=None, metadata=OMIT_IF_DEFAULT)
    period_months: int | None = field(default=None, metadata=OMIT_IF_DEFAULT)
    model_version: str = PERPETUAL_TAIL_MODEL_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.anchor_date, date):
            raise TypeError("anchor_date must be a date")
        if self.amount is not None:
            object.__setattr__(self, "amount", positive_money(self.amount, "tail coupon amount"))
        if (self.period_days is None) == (self.period_months is None):
            raise InvalidTerms("a perpetual tail needs exactly one of period_days, period_months")
        for name in ("period_days", "period_months"):
            step = getattr(self, name)
            if step is None:
                continue
            if isinstance(step, bool) or not isinstance(step, int):
                raise TypeError(f"{name} must be an int")
            if step <= 0:
                raise InvalidTerms(f"{name} must be positive")

    def payment_date(self, k: int) -> date:
        """The k-th tail payment date (k >= 1)."""
        return _shifted(self.anchor_date, k, self.period_days, self.period_months)

    def scaled(self, factor: float) -> PerpetualTail:
        """The same tail with its amount multiplied by ``factor`` (an FX rate)."""
        return self.with_amount(self.coupon * positive_money(factor, "tail scale"))

    def projected(self, last: FloatingCouponProjection) -> PerpetualTail:
        """A floating tail at one projection of the leg's last coupon ``last``.

        Past the explicit periods the coupon's rate is constant; its amount follows the
        accrual days of each period, which vary around the mean step (89-92 days a
        quarter). The tail pays the last coupon scaled to the mean step, ``amount * mean
        days / last period's days`` (``period_days``, or 365.2425 / 12 days a month), so
        that the periodic day-count variation averages out instead of repeating the last
        period's length forever. A tail with an amount is returned unchanged.
        """
        if self.amount is not None:
            return self
        if last.payment_date != self.anchor_date:
            raise InvalidTerms("a floating perpetual tail continues its leg's last coupon")
        days = (last.accrual_end - last.accrual_start).days
        mean = (
            float(self.period_days)
            if self.period_days is not None
            else _MEAN_MONTH_DAYS * cast(int, self.period_months)
        )
        return self.with_amount(last.amount_rub * mean / days)

    def with_amount(self, amount: float) -> PerpetualTail:
        """The same schedule paying ``amount``: a floating tail at one projection."""
        return PerpetualTail(
            self.anchor_date, amount, self.period_days, self.period_months, self.model_version
        )

    @property
    def floating(self) -> bool:
        """Whether the coupon continues a floating leg (``amount`` is None)."""
        return self.amount is None

    @property
    def coupon(self) -> float:
        """The tail coupon; a floating tail has none until it is projected."""
        if self.amount is None:
            raise InvalidTerms(
                "a floating perpetual tail is projected with its floating leg "
                "(BondRiskRequest(floating_leg=..., perpetual_tail=...))"
            )
        return self.amount

    @property
    def identifier(self) -> str:
        step = f"{self.period_days}d" if self.period_days is not None else f"{self.period_months}m"
        return f"tail:{self.anchor_date.isoformat()}:{step}:{self.amount!r}"


def _step(ends: Sequence[date]) -> tuple[int | None, int | None]:
    """The regular step after the last of ``ends`` (ordered): days or whole months.

    Days when the last two intervals are equally long in days, else whole months when
    the last two dates fall on the same day of the month; two dates alone step by their
    distance in days when they are not whole months apart.
    """
    last, previous = ends[-1], ends[-2]
    days = (last - previous).days
    if len(ends) >= 3 and (previous - ends[-3]).days == days:
        return days, None
    months = (last.year - previous.year) * 12 + (last.month - previous.month)
    if months > 0 and add_months(previous, months) == last:
        return None, months
    if len(ends) < 3:
        return days, None
    raise InvalidTerms(f"the coupon schedule ending {last} has no regular step to continue")


def perpetual_tail(periods: Sequence[CouponPeriod | RateCouponPeriod]) -> PerpetualTail:
    """The tail continuing the listed coupons: the last amount, at the last step.

    The step is the last period's length in days when the last two periods are
    equally long in days (the usual 91 / 182-day schedules), or in whole months when
    the last two payments fall on the same day of the month that many months apart.
    The last listed coupon must have an amount: an unknown coupon (amount 0, as MOEX
    lists coupons not set yet) is not continued.
    """
    if len(periods) < 2:
        raise InvalidTerms("a perpetual tail needs at least two listed coupons for its step")
    for left, right in pairwise(periods):
        if right.payment_date <= left.payment_date:
            raise InvalidTerms("listed coupons must be ordered by payment date")
    last = periods[-1]
    if not last.amount > 0.0:
        raise InvalidTerms(
            f"the coupon paid {last.payment_date} is not known; a perpetual is continued "
            "only from a known coupon"
        )
    days, months = _step([p.payment_date for p in periods[-3:]])
    return PerpetualTail(last.payment_date, last.amount, period_days=days, period_months=months)


def _shifted(start: date, k: int, days: int | None, months: int | None) -> date:
    """``start`` moved by ``k`` steps of ``days`` days or ``months`` months."""
    if days is not None:
        return start + timedelta(days=k * days)
    assert months is not None
    return add_months(start, k * months)


def floating_perpetual_tail(
    leg: FloatingCouponLeg, settlement: date
) -> tuple[FloatingCouponLeg, PerpetualTail]:
    """A never-called floating perpetual's leg continued, and the tail after it (0.41.0).

    The listed periods must run without a gap from ``settlement``. New periods follow the
    last at its step (:func:`perpetual_tail`'s rule, on the accrual ends), with its
    notional, margin and payment lag, until the last one starts at least
    :data:`FLOATING_EXPLICIT_YEARS` after settlement. A period with a ``fixing_anchor``
    (a coupon fixed once per reset cycle) is continued cycle by cycle: the cycle is the
    index tenor (``index_tenor_years``, whole months: a coupon reset every N years to the
    N-year KBD). Each reset falls on a coupon start: the continued period start nearest
    ``add_months(previous reset, 12 N)`` (a 182-day schedule resets on the start six days
    before five calendar years, so every cycle has its ten coupons), every period is
    anchored on its cycle's reset, and the leg ends with the first whole cycle that starts
    that far out. The tail (``amount=None``) continues the last period's projected coupon
    at the same step from its payment date.

    The last coupon is thus fixed 35 years out or later, where every curve core builds is
    flat-forward (from 30 years after the curve date; bucket shocks are flat past 20
    years, a rolled curve for a horizon of up to five years past 30 years from it), so
    every later coupon would be projected at the same rate and differs from the last
    only by its accrual days: the tail pays the last coupon scaled to the mean step
    (:meth:`PerpetualTail.projected`).
    """
    periods = leg.periods
    future = [p for p in periods if p.payment_date > settlement]
    if not future:
        raise InvalidTerms(
            f"no coupon of the perpetual is listed after {settlement}; "
            "list its current period or value it to its put"
        )
    for left, right in pairwise(future):
        if right.accrual_start != left.accrual_end:
            raise InvalidTerms(
                f"coupon periods have a gap from {left.accrual_end} to {right.accrual_start}"
            )
    if len(periods) < 2:
        raise InvalidTerms("a perpetual tail needs at least two listed coupons for its step")
    days, months = _step([p.accrual_end for p in periods[-3:]])
    last = periods[-1]
    lag = last.payment_date - last.accrual_end
    reset = last.fixing_anchor
    cycle = 0
    if reset is not None:
        tenor = leg.rule.index_tenor_years
        cycle = 0 if tenor is None else round(12.0 * tenor)
        if tenor is None or abs(12.0 * tenor - cycle) > 1e-9 or cycle <= 0:
            raise InvalidTerms(
                "a coupon fixed once per reset cycle is continued over cycles of its index "
                "tenor: index_tenor_years must be a whole number of months"
            )
    target = add_months(settlement, 12 * FLOATING_EXPLICIT_YEARS)
    added: list[FloatingCouponPeriod] = []
    current = last
    anchor = reset
    # The next reset, nominally: snapped to the nearest coupon start when one comes.
    nominal = None if reset is None else add_months(reset, cycle)
    k = 1
    while True:
        if len(added) >= _MAX_CONTINUED_PERIODS:
            raise InvalidTerms("the floating leg cannot be continued to the explicit horizon")
        start = current.accrual_end
        end = _shifted(last.accrual_end, k, days, months)
        if anchor is None or nominal is None:
            if current.accrual_start >= target:
                break  # the last period starts 35 years out
        elif abs((start - nominal).days) <= abs((end - nominal).days):
            # This period starts the next cycle, unless the current one started 35 years
            # out: the leg then ends with that whole cycle.
            if anchor >= target:
                break
            anchor, nominal = start, add_months(start, cycle)
        current = replace(
            last,
            accrual_start=start,
            accrual_end=end,
            payment_date=end + lag,
            published_coupon_rub=None,
            published_rate=None,
            fixing_anchor=anchor,
        )
        added.append(current)
        k += 1
    continued = replace(leg, periods=(*periods, *added))
    tail = PerpetualTail(current.payment_date, None, period_days=days, period_months=months)
    return continued, tail


@lru_cache(maxsize=1024)
def _schedule(anchor: date, period_days: int | None, period_months: int | None) -> np.ndarray:
    """Payment dates of a tail as proleptic ordinals, to ``TAIL_SUM_YEARS`` past the anchor."""
    end = add_months(anchor, 12 * TAIL_SUM_YEARS)
    out = []
    k = 1
    while (payment := _shifted(anchor, k, period_days, period_months)) <= end:
        out.append(payment.toordinal())
        k += 1
    array = np.asarray(out, dtype=np.int64)
    array.setflags(write=False)
    return array


def _ordinals(tail: PerpetualTail) -> np.ndarray:
    """Payment dates of the tail (cached on its schedule: a re-projected amount shares it)."""
    return _schedule(tail.anchor_date, tail.period_days, tail.period_months)


def _times(tail: PerpetualTail, settlement: date, day_count: DayCount) -> np.ndarray:
    return _schedule_times(
        tail.anchor_date, tail.period_days, tail.period_months, settlement, day_count
    )


@lru_cache(maxsize=4096)
def _schedule_times(
    anchor: date,
    period_days: int | None,
    period_months: int | None,
    settlement: date,
    day_count: DayCount,
) -> np.ndarray:
    """Year fractions from ``settlement`` of the tail payments after it."""
    ordinals = _schedule(anchor, period_days, period_months)
    future = ordinals[ordinals > settlement.toordinal()]
    if day_count is DayCount.ACT_365_FIXED:
        times = (future - settlement.toordinal()) / 365.0
    else:
        times = np.asarray(
            [day_count.year_fraction(settlement, date.fromordinal(int(o))) for o in future]
        )
    times.setflags(write=False)
    return times


def _sum_with_remainder(terms: np.ndarray) -> float:
    """Sum of the terms plus the geometric remainder of the last two; ``inf`` if divergent."""
    if terms.size == 0:
        return 0.0
    if not np.all(np.isfinite(terms)):
        return math.inf
    total = float(np.sum(terms))  # pairwise summation: about 1e-16 relative
    # Terms that underflowed to subnormals (a high spread over 600 years) end equal, a ratio
    # of 1 that is not divergence: they and their remainder are below 1e-307 (issue #84).
    if terms.size >= 2 and terms[-1] >= _NORMAL_MIN:
        ratio = float(terms[-1] / terms[-2])
        if not ratio < 1.0:
            return math.inf
        total += float(terms[-1]) * ratio / (1.0 - ratio)
    return total if math.isfinite(total) else math.inf


def _discount_factors(curve: DiscountFactorCurve, ordinals: np.ndarray) -> np.ndarray:
    """Curve discount factors at ``ordinals``: read to 30 years, flat-forward beyond."""
    start = curve.curve_date.toordinal() + _CURVE_DAYS
    near = ordinals[ordinals <= start]
    far = ordinals[ordinals > start]
    read = _read(curve, near)
    if far.size:
        anchor = curve.discount_factor(date.fromordinal(start))
        later = curve.discount_factor(date.fromordinal(start + _FORWARD_SPAN_DAYS))
        middle = curve.discount_factor(date.fromordinal(start + _FORWARD_SPAN_DAYS // 2))
        rate = math.log(anchor / later) / _FORWARD_SPAN_DAYS
        expected = math.log(anchor) - rate * (_FORWARD_SPAN_DAYS // 2)
        if abs(math.log(middle) - expected) <= _FLAT_TOLERANCE:
            with np.errstate(over="ignore", under="ignore"):
                extrapolated = anchor * np.exp(-rate * (far - start))
        else:  # not flat-forward past 30 years: read every date
            extrapolated = _read(curve, far)
        return np.concatenate((read, extrapolated))
    return read


def _read(curve: DiscountFactorCurve, ordinals: np.ndarray) -> np.ndarray:
    """Discount factors read from the curve at each date.

    A bucket-shocked view of a base curve (the risk engine's) is evaluated at once, as
    the view evaluates it date by date: ``DF_base(t) exp(-shift(t) t)`` for a
    continuous-zero shock, ``(DF_base(t)^(-1/t) + shift(t))^(-t)`` for an annual-yield
    shock (``CurveConvention.zero_shock``), with the bucket weights of each date cached
    on the base curve.
    """
    if isinstance(curve, _ZeroShockCurve) and ordinals.size:
        times, weights, base = _shock_terms(curve.base, ordinals)
        shift = weights @ np.asarray(curve.shifts, dtype=float) * 1e-4
        if curve.base.convention.zero_shock != "continuous":  # annual or annual_par overlay
            growth = base ** (-1.0 / times) + shift
            if np.any(growth <= 0.0):
                raise ValueError("shocked annual yield must exceed -100 %")
            return np.asarray(growth ** (-times))
        return np.asarray(base * np.exp(-shift * times))
    return np.asarray([curve.discount_factor(date.fromordinal(int(o))) for o in ordinals], float)


def _shock_terms(base: DiscountCurve, ordinals: np.ndarray) -> tuple[np.ndarray, ...]:
    """Times, bucket weights and base discount factors at ``ordinals``, cached on ``base``."""
    cache = base.__dict__.get("_tail_shock_terms")
    if cache is None:
        cache = {}
        object.__setattr__(base, "_tail_shock_terms", cache)
    key = ordinals.tobytes()
    terms = cache.get(key)
    if terms is None:
        rows = [_zero_shock_terms(base, date.fromordinal(int(o))) for o in ordinals]
        weights = np.zeros((len(rows), len(RISK_BUCKETS)))
        for row, (_, nonzero, _) in enumerate(rows):
            for column, weight in nonzero:
                weights[row, column] = weight
        terms = (
            np.asarray([r[0] for r in rows]),
            weights,
            np.asarray([r[2] for r in rows]),
        )
        if len(cache) < _SHOCK_TERMS_MEMO_SIZE:
            cache[key] = terms
    return cast(tuple[np.ndarray, ...], terms)


def tail_pv_at_spread(
    tail: PerpetualTail,
    curve: DiscountFactorCurve,
    settlement: date,
    spread: float,
    convention: ZSpreadConvention,
) -> float:
    """PV at ``settlement`` of the tail on ``curve`` plus the decimal ``spread``.

    Each coupon is discounted as ``compatibility.price_cashflows`` discounts a flow:
    the settlement-forward discount factor, its zero rate in ``convention`` plus the
    spread. Returns ``inf`` when the stream does not converge.
    """
    amount = tail.coupon
    ordinals = _ordinals(tail)
    future = ordinals[ordinals > settlement.toordinal()]
    if future.size == 0:
        return 0.0
    times = _times(tail, settlement, convention.day_count)
    forward = _discount_factors(curve, future) / curve.discount_factor(settlement)
    with np.errstate(over="ignore", under="ignore", divide="ignore", invalid="ignore"):
        if convention.compounding.is_continuous:
            terms = amount * forward * np.exp(-spread * times)
        else:
            m = convention.compounding.periods_per_year
            zero = m * (forward ** (-1.0 / (m * times)) - 1.0)
            base = 1.0 + (zero + spread) / m
            if np.any(base <= 0.0):
                raise ValueError("rate is outside the compounding domain")
            terms = amount * base ** (-m * times)
    return _sum_with_remainder(terms)


def tail_pv_at_yield(
    tail: PerpetualTail,
    settlement: date,
    effective_yield: float,
    convention: YieldConvention,
    *,
    time_weighted: bool = False,
) -> float:
    """``sum a (1 + y)^-t`` over the tail (``t`` on the yield day count), ``inf`` if divergent.

    ``time_weighted`` gives ``sum t a (1 + y)^-t``, the Macaulay numerator.
    """
    amount = tail.coupon
    y = finite_money(effective_yield, "effective yield")
    if y <= -1.0:
        raise InvalidTerms("effective yield must be above -100%")
    times = _times(tail, settlement, convention.day_count)
    if times.size == 0:
        return 0.0
    with np.errstate(over="ignore", under="ignore"):
        terms = amount * (1.0 + y) ** (-times)
        if time_weighted:
            terms = terms * times
    return _sum_with_remainder(terms)


@dataclass(frozen=True)
class PerpetualYield:
    """Effective annual yield and durations of listed flows plus a tail, at one price."""

    effective_yield: float
    macaulay_duration_years: float
    modified_duration_years: float


def perpetual_yield(
    settlement: date,
    dirty_price: float,
    cashflows: Sequence[tuple[date, float]],
    tail: PerpetualTail,
    convention: YieldConvention,
) -> PerpetualYield:
    """Solve ``P = sum CF (1 + y)^-t + tail(y)`` and the Macaulay / modified durations.

    ``t`` runs from ``settlement`` on the convention's day count; listed flows on or
    before settlement are left out. This is the yield of the endless stream: the
    worst case of a perpetual that trades below par.
    """
    from ._solver import bisect
    from .compatibility import _finite_lower

    if convention.compounding.periods_per_year != 1:
        raise InvalidTerms("a perpetual's effective yield compounds annually")
    price = positive_money(dirty_price, "dirty price")
    listed = tuple(
        (convention.day_count.year_fraction(settlement, d), finite_money(a, "cashflow amount"))
        for d, a in cashflows
        if d > settlement
    )

    def pv(y: float) -> float:
        try:
            value = math.fsum(a * (1.0 + y) ** -t for t, a in listed)
        except OverflowError:
            return math.inf
        return value + tail_pv_at_yield(tail, settlement, y, convention)

    def error(y: float) -> float:
        return pv(y) - price

    lower, lower_value = _finite_lower(error, -0.99, 5.0)
    solved = bisect(
        error,
        lower,
        5.0,
        tolerance=1e-13,
        x_tolerance=1e-13,
        lower_value=lower_value,
        label="perpetual yield",
    ).root
    value = pv(solved)
    weighted = math.fsum(t * a * (1.0 + solved) ** -t for t, a in listed) + tail_pv_at_yield(
        tail, settlement, solved, convention, time_weighted=True
    )
    macaulay = finite_money(weighted / value, "Macaulay duration")
    return PerpetualYield(solved, macaulay, finite_money(macaulay / (1.0 + solved), "modified"))
