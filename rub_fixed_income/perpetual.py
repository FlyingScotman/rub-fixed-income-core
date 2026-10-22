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

The sum runs to :data:`TAIL_SUM_YEARS` (600) years past the anchor and adds the
geometric remainder of its last two terms. A stream that does not converge (a total
discount rate at or below zero) is worth infinity: the pricing functions return
``inf`` and the solvers move their brackets past it.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
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
    from .domain import CouponPeriod, RateCouponPeriod

__all__ = [
    "PERPETUAL_TAIL_MODEL_VERSION",
    "TAIL_CURVE_YEARS",
    "TAIL_SUM_YEARS",
    "PerpetualTail",
    "PerpetualYield",
    "perpetual_tail",
    "perpetual_yield",
    "tail_pv_at_spread",
    "tail_pv_at_yield",
]

PERPETUAL_TAIL_MODEL_VERSION = "rub-perpetual-tail-v1"
TAIL_CURVE_YEARS = 30
TAIL_SUM_YEARS = 600
# Discount factors are read from the curve up to this many days after the curve date:
# 30 years and their leap days. The KBD materialises nodes to 365 * 30 + 7 days.
_CURVE_DAYS = 365 * TAIL_CURVE_YEARS + 8
# The flat-forward rate beyond is read over ten years and checked at the midpoint.
_FORWARD_SPAN_DAYS = 3650
_FLAT_TOLERANCE = 1e-9
_SHOCK_TERMS_MEMO_SIZE = 64


@dataclass(frozen=True)
class PerpetualTail:
    """Coupons of ``amount`` per bond paid forever after ``anchor_date``.

    The k-th tail coupon (k >= 1) is paid ``k * period_days`` days or
    ``add_months(anchor_date, k * period_months)`` after the anchor; exactly one of
    the two steps is given. Amounts are in the instrument currency per bond.
    """

    anchor_date: date
    amount: float
    period_days: int | None = field(default=None, metadata=OMIT_IF_DEFAULT)
    period_months: int | None = field(default=None, metadata=OMIT_IF_DEFAULT)
    model_version: str = PERPETUAL_TAIL_MODEL_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.anchor_date, date):
            raise TypeError("anchor_date must be a date")
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
        if self.period_days is not None:
            return self.anchor_date + timedelta(days=k * self.period_days)
        assert self.period_months is not None
        return add_months(self.anchor_date, k * self.period_months)

    def scaled(self, factor: float) -> PerpetualTail:
        """The same tail with its amount multiplied by ``factor`` (an FX rate)."""
        return PerpetualTail(
            self.anchor_date,
            self.amount * positive_money(factor, "tail scale"),
            self.period_days,
            self.period_months,
            self.model_version,
        )

    @property
    def identifier(self) -> str:
        step = f"{self.period_days}d" if self.period_days is not None else f"{self.period_months}m"
        return f"tail:{self.anchor_date.isoformat()}:{step}:{self.amount!r}"


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
    previous = periods[-2]
    days = (last.payment_date - previous.payment_date).days
    if len(periods) >= 3:
        earlier = (previous.payment_date - periods[-3].payment_date).days
        if earlier == days:
            return PerpetualTail(last.payment_date, last.amount, period_days=days)
    months = (last.payment_date.year - previous.payment_date.year) * 12 + (
        last.payment_date.month - previous.payment_date.month
    )
    if months > 0 and add_months(previous.payment_date, months) == last.payment_date:
        return PerpetualTail(last.payment_date, last.amount, period_months=months)
    if len(periods) < 3:
        return PerpetualTail(last.payment_date, last.amount, period_days=days)
    raise InvalidTerms(
        f"the coupon schedule ending {last.payment_date} has no regular step to continue"
    )


@lru_cache(maxsize=1024)
def _ordinals(tail: PerpetualTail) -> np.ndarray:
    """Payment dates of the tail as proleptic ordinals, to ``TAIL_SUM_YEARS`` past the anchor."""
    end = add_months(tail.anchor_date, 12 * TAIL_SUM_YEARS)
    out = []
    k = 1
    while (payment := tail.payment_date(k)) <= end:
        out.append(payment.toordinal())
        k += 1
    array = np.asarray(out, dtype=np.int64)
    array.setflags(write=False)
    return array


@lru_cache(maxsize=4096)
def _times(tail: PerpetualTail, settlement: date, day_count: DayCount) -> np.ndarray:
    """Year fractions from ``settlement`` of the tail payments after it."""
    ordinals = _ordinals(tail)
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
    if terms.size >= 2 and terms[-1] > 0.0:
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
        if curve.base.convention.zero_shock == "annual":
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
    ordinals = _ordinals(tail)
    future = ordinals[ordinals > settlement.toordinal()]
    if future.size == 0:
        return 0.0
    times = _times(tail, settlement, convention.day_count)
    forward = _discount_factors(curve, future) / curve.discount_factor(settlement)
    with np.errstate(over="ignore", under="ignore", divide="ignore", invalid="ignore"):
        if convention.compounding.is_continuous:
            terms = tail.amount * forward * np.exp(-spread * times)
        else:
            m = convention.compounding.periods_per_year
            zero = m * (forward ** (-1.0 / (m * times)) - 1.0)
            base = 1.0 + (zero + spread) / m
            if np.any(base <= 0.0):
                raise ValueError("rate is outside the compounding domain")
            terms = tail.amount * base ** (-m * times)
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
    y = finite_money(effective_yield, "effective yield")
    if y <= -1.0:
        raise InvalidTerms("effective yield must be above -100%")
    times = _times(tail, settlement, convention.day_count)
    if times.size == 0:
        return 0.0
    with np.errstate(over="ignore", under="ignore"):
        terms = tail.amount * (1.0 + y) ** (-times)
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
