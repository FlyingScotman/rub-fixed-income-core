"""Contractual RUB floating coupons: realized fixings joined with curve projection.

An accrual day takes the fixing of the latest business day on or before it,
optionally looked back a number of business days ("lookback without observation
shift": rates move, weights stay on accrual days). Observations dated before the
history's valuation date come from published fixings and never move under curve
shocks; later observations are overnight forwards of the projection curve. No I/O:
the caller supplies a point-in-time fixing snapshot and the publication calendar.
"""

from __future__ import annotations

import math
from bisect import bisect_right
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from itertools import pairwise
from typing import Literal

from ._money import finite_money, non_negative_money, positive_money
from .analytics import canonical_sha256
from .conventions import DayCount, calendar_fingerprint
from .dates import BusinessCalendar
from .discount import DiscountFactorCurve

FixingIndex = Literal["ruonia", "key_rate"]
IndexAveraging = Literal["compounded", "arithmetic"]
CouponSource = Literal["published", "fixed_by_history", "partially_projected", "projected"]
FLOATING_MODEL_VERSION = "rub-floating-coupon-v1"

# RUONIA is published for every business day; the key rate stays in force until changed.
_DAILY_PUBLISHED: Mapping[str, bool] = {"ruonia": True, "key_rate": False}
_LABELS: Mapping[str, str] = {"ruonia": "RUONIA", "key_rate": "key rate"}


def _previous_business_day(calendar: BusinessCalendar, value: date) -> date:
    """Latest business day on or before ``value``."""
    current = value
    while not calendar.is_business_day(current):
        current -= timedelta(days=1)
    return current


def _shift_back(calendar: BusinessCalendar, value: date, count: int) -> date:
    current = value
    for _ in range(count):
        current = _previous_business_day(calendar, current - timedelta(days=1))
    return current


def _round_half_up(value: float, decimals: int) -> float:
    return float(Decimal(str(value)).quantize(Decimal(1).scaleb(-decimals), ROUND_HALF_UP))


def _optional_decimals(value: int | None, name: str) -> None:
    if value is not None and (
        isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 12
    ):
        raise ValueError(f"{name} must be None or an integer between 0 and 12")


@dataclass(frozen=True)
class FixingHistory:
    """Fixings known for observations strictly before ``valuation_date``.

    Rates are decimal annual (0.15 = 15 %). ``snapshot_id`` names the caller's
    point-in-time selection (for example a market-data-store fixing snapshot).
    RUONIA must have a fixing on every ``calendar`` business day before the
    latest fixing; only the trailing ``max_unpublished_business_days`` business
    days before ``valuation_date`` may carry the latest fixing. The key rate stays
    in force until changed, so gaps between its fixings are not errors.
    """

    index: FixingIndex
    valuation_date: date
    fixings: tuple[tuple[date, float], ...]
    calendar: BusinessCalendar
    snapshot_id: str
    max_unpublished_business_days: int = 0
    known_at_utc: str | None = None
    _dates: tuple[date, ...] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.index not in _DAILY_PUBLISHED:
            raise ValueError("fixing index must be ruonia or key_rate")
        if not isinstance(self.calendar, BusinessCalendar):
            raise TypeError("calendar must be a BusinessCalendar")
        if not isinstance(self.snapshot_id, str) or not self.snapshot_id.strip():
            raise ValueError("snapshot_id must be a nonempty string")
        if self.known_at_utc is not None and (
            not isinstance(self.known_at_utc, str) or not self.known_at_utc.strip()
        ):
            raise ValueError("known_at_utc must be None or a nonempty string")
        limit = self.max_unpublished_business_days
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
            raise ValueError("max_unpublished_business_days must be a nonnegative integer")
        rows = tuple((day, finite_money(rate, "fixing rate")) for day, rate in tuple(self.fixings))
        if not rows:
            raise ValueError("fixing history is empty")
        for (left, _), (right, _) in pairwise(rows):
            if right <= left:
                raise ValueError("fixing dates must be strictly increasing")
        if rows[-1][0] >= self.valuation_date:
            raise ValueError("fixing history must end before the valuation date")
        if any(rate <= -1.0 for _, rate in rows):
            raise ValueError("fixing rates must exceed -100 %")
        if _DAILY_PUBLISHED[self.index]:
            for day, _ in rows:
                if not self.calendar.is_business_day(day):
                    raise ValueError(
                        f"{_LABELS[self.index]} fixing on non-business day {day.isoformat()}"
                    )
        object.__setattr__(self, "fixings", rows)
        object.__setattr__(self, "_dates", tuple(day for day, _ in rows))

    @property
    def latest_fixing_date(self) -> date:
        return self._dates[-1]

    @property
    def unpublished_business_days(self) -> tuple[date, ...]:
        """Business days after the latest fixing and before the valuation date."""
        if not _DAILY_PUBLISHED[self.index]:
            return ()
        days = []
        day = self.latest_fixing_date + timedelta(days=1)
        while day < self.valuation_date:
            if self.calendar.is_business_day(day):
                days.append(day)
            day += timedelta(days=1)
        return tuple(days)

    @property
    def identity(self) -> dict[str, object]:
        return {
            "index": self.index,
            "valuation_date": self.valuation_date,
            "snapshot_id": self.snapshot_id,
            "known_at_utc": self.known_at_utc,
            "calendar_version": self.calendar.version,
            "calendar_fingerprint": calendar_fingerprint(self.calendar),
            "max_unpublished_business_days": self.max_unpublished_business_days,
            "fixings_sha256": canonical_sha256(self.fixings),
        }

    def rate(self, observation: date) -> tuple[float, bool]:
        """Return the rate for a business-day observation and whether it was carried."""
        if observation >= self.valuation_date:
            raise ValueError("observation is not before the valuation date")
        label = _LABELS[self.index]
        position = bisect_right(self._dates, observation) - 1
        if position < 0:
            raise LookupError(f"no {label} fixing on or before {observation.isoformat()}")
        if not _DAILY_PUBLISHED[self.index] or self._dates[position] == observation:
            return self.fixings[position][1], False
        if observation < self.latest_fixing_date:
            raise LookupError(f"missing {label} fixing on business day {observation.isoformat()}")
        unpublished = self.unpublished_business_days
        if len(unpublished) > self.max_unpublished_business_days:
            raise LookupError(
                f"{label} is unpublished for {len(unpublished)} business days from "
                f"{unpublished[0].isoformat()}; at most "
                f"{self.max_unpublished_business_days} may carry the latest fixing"
            )
        return self.fixings[-1][1], True


@dataclass(frozen=True)
class FloatingCouponRule:
    """Contractual coupon formula: index return over the period plus a margin.

    ``margin`` is decimal annual (0.015 = +150 bp). The index return is
    ``prod(1 + r_i * tau_i) - 1`` (compounded) or ``sum(r_i * tau_i)`` (arithmetic)
    with ``tau_i`` from ``rate_day_count`` over the days each rate applies. The
    coupon rate is that return divided by the ``coupon_day_count`` accrual plus the
    margin. Rounding (rate in percent points, amount in RUB, half-up) applies only to
    coupons fully fixed by history: a rounded projection is not an expectation.
    """

    index: FixingIndex
    averaging: IndexAveraging
    margin: float
    coupon_day_count: DayCount
    rate_day_count: DayCount
    lookback_business_days: int = 0
    rate_rounding_decimals: int | None = None
    amount_rounding_decimals: int | None = None

    def __post_init__(self) -> None:
        if self.index not in _DAILY_PUBLISHED:
            raise ValueError("coupon index must be ruonia or key_rate")
        if self.averaging not in ("compounded", "arithmetic"):
            raise ValueError("averaging must be compounded or arithmetic")
        object.__setattr__(self, "margin", finite_money(self.margin, "coupon margin"))
        if not isinstance(self.coupon_day_count, DayCount) or not isinstance(
            self.rate_day_count, DayCount
        ):
            raise TypeError("day counts must be DayCount values")
        lookback = self.lookback_business_days
        if isinstance(lookback, bool) or not isinstance(lookback, int) or not 0 <= lookback <= 30:
            raise ValueError("lookback_business_days must be an integer between 0 and 30")
        _optional_decimals(self.rate_rounding_decimals, "rate_rounding_decimals")
        _optional_decimals(self.amount_rounding_decimals, "amount_rounding_decimals")

    @property
    def identifier(self) -> str:
        return canonical_sha256(
            {
                "model": FLOATING_MODEL_VERSION,
                "index": self.index,
                "averaging": self.averaging,
                "margin": self.margin,
                "coupon_day_count": self.coupon_day_count.value,
                "rate_day_count": self.rate_day_count.value,
                "lookback_business_days": self.lookback_business_days,
                "rate_rounding_decimals": self.rate_rounding_decimals,
                "amount_rounding_decimals": self.amount_rounding_decimals,
            }
        )


@dataclass(frozen=True)
class FloatingCouponPeriod:
    """One coupon period; amounts are RUB per bond on the period's notional.

    ``published_coupon_rub`` is known cash and is used as is. MOEX shows 0 for
    coupons not yet fixed, so a zero is rejected: pass None for those.
    """

    accrual_start: date
    accrual_end: date
    payment_date: date
    notional_rub: float
    published_coupon_rub: float | None = None

    def __post_init__(self) -> None:
        if self.accrual_end <= self.accrual_start:
            raise ValueError("coupon accrual end must follow its start")
        if self.payment_date < self.accrual_end:
            raise ValueError("coupon payment cannot precede the accrual end")
        object.__setattr__(
            self, "notional_rub", positive_money(self.notional_rub, "coupon notional")
        )
        if self.published_coupon_rub is not None:
            amount = non_negative_money(self.published_coupon_rub, "published coupon")
            if amount == 0.0:
                raise ValueError(
                    "a zero published coupon is ambiguous; pass None for an unfixed coupon"
                )
            object.__setattr__(self, "published_coupon_rub", amount)


@dataclass(frozen=True)
class FloatingCouponProjection:
    accrual_start: date
    accrual_end: date
    payment_date: date
    amount_rub: float
    source: CouponSource
    index_rate: float | None
    coupon_rate: float | None
    realized_days: int
    projected_days: int
    carried_business_days: tuple[date, ...]


@dataclass(frozen=True)
class _Segment:
    start: date
    end: date
    year_fraction: float
    observation: date
    next_observation: date
    realized_rate: float | None
    carried: bool


@dataclass(frozen=True)
class FloatingLeg:
    """Coupon periods of one floater with their formula and fixing history.

    Principal is not part of the leg: pass it as fixed cashflows. Periods paid on
    or before the settlement date are skipped and need no history.
    """

    rule: FloatingCouponRule
    periods: tuple[FloatingCouponPeriod, ...]
    history: FixingHistory
    _plans: dict[int, tuple[_Segment, ...]] = field(
        init=False, repr=False, compare=False, default_factory=dict
    )

    def __post_init__(self) -> None:
        if not isinstance(self.rule, FloatingCouponRule):
            raise TypeError("rule must be a FloatingCouponRule")
        if not isinstance(self.history, FixingHistory):
            raise TypeError("history must be a FixingHistory")
        if self.rule.index != self.history.index:
            raise ValueError("coupon rule and fixing history index differ")
        periods = tuple(self.periods)
        if not periods or not all(isinstance(p, FloatingCouponPeriod) for p in periods):
            raise ValueError("at least one FloatingCouponPeriod is required")
        for left, right in pairwise(periods):
            if right.accrual_start < left.accrual_end:
                raise ValueError("coupon periods must be ordered and non-overlapping")
        object.__setattr__(self, "periods", periods)

    @property
    def identity(self) -> dict[str, object]:
        return {
            "model": FLOATING_MODEL_VERSION,
            "rule": self.rule.identifier,
            "periods": [asdict(period) for period in self.periods],
            "history": self.history.identity,
        }

    def _plan(self, position: int) -> tuple[_Segment, ...]:
        cached = self._plans.get(position)
        if cached is not None:
            return cached
        period = self.periods[position]
        calendar = self.history.calendar
        segments = []
        cursor = period.accrual_start
        while cursor < period.accrual_end:
            business_day = _previous_business_day(calendar, cursor)
            end = min(calendar.add_business_days(business_day, 1), period.accrual_end)
            observation = _shift_back(calendar, business_day, self.rule.lookback_business_days)
            realized: float | None = None
            carried = False
            if observation < self.history.valuation_date:
                realized, carried = self.history.rate(observation)
            segments.append(
                _Segment(
                    cursor,
                    end,
                    self.rule.rate_day_count.year_fraction(cursor, end),
                    observation,
                    calendar.add_business_days(observation, 1),
                    realized,
                    carried,
                )
            )
            cursor = end
        plan = tuple(segments)
        self._plans[position] = plan
        return plan

    def _coupon(self, position: int, curve: DiscountFactorCurve | None) -> FloatingCouponProjection:
        period = self.periods[position]
        if period.published_coupon_rub is not None:
            return FloatingCouponProjection(
                period.accrual_start,
                period.accrual_end,
                period.payment_date,
                period.published_coupon_rub,
                "published",
                None,
                None,
                0,
                0,
                (),
            )
        rule = self.rule
        plan = self._plan(position)
        compounded = 1.0
        arithmetic = 0.0
        forwards: dict[date, float] = {}
        realized_days = projected_days = 0
        for segment in plan:
            days = (segment.end - segment.start).days
            rate = segment.realized_rate
            if rate is None:
                if curve is None:
                    raise ValueError("a projection curve is required for unfixed observations")
                rate = forwards.get(segment.observation)
                if rate is None:
                    rate = _overnight_forward(curve, segment, rule.rate_day_count)
                    forwards[segment.observation] = rate
                projected_days += days
            else:
                realized_days += days
            compounded *= 1.0 + rate * segment.year_fraction
            arithmetic += rate * segment.year_fraction
        index_return = compounded - 1.0 if rule.averaging == "compounded" else arithmetic
        accrual = rule.coupon_day_count.year_fraction(period.accrual_start, period.accrual_end)
        index_rate = finite_money(index_return / accrual, "index rate")
        coupon_rate = index_rate + rule.margin
        fixed = projected_days == 0
        if fixed and rule.rate_rounding_decimals is not None:
            coupon_rate = _round_half_up(coupon_rate * 100.0, rule.rate_rounding_decimals) / 100.0
        amount = period.notional_rub * coupon_rate * accrual
        if fixed and rule.amount_rounding_decimals is not None:
            amount = _round_half_up(amount, rule.amount_rounding_decimals)
        source: CouponSource = (
            "fixed_by_history"
            if fixed
            else "projected"
            if realized_days == 0
            else "partially_projected"
        )
        return FloatingCouponProjection(
            period.accrual_start,
            period.accrual_end,
            period.payment_date,
            non_negative_money(amount, "floating coupon"),
            source,
            index_rate,
            finite_money(coupon_rate, "coupon rate"),
            realized_days,
            projected_days,
            tuple(s.observation for s in plan if s.carried),
        )

    def project(
        self, curve: DiscountFactorCurve | None, *, settlement_date: date
    ) -> tuple[FloatingCouponProjection, ...]:
        """Project every coupon paid after ``settlement_date``.

        ``curve`` projects unfixed observations; it must be dated at the history's
        valuation date. It may be None only when every remaining coupon is known.
        A negative projected coupon raises: floors are not modelled.
        """
        if curve is not None and curve.curve_date != self.history.valuation_date:
            raise ValueError("projection curve date must equal the fixing history valuation date")
        return tuple(
            self._coupon(i, curve)
            for i, period in enumerate(self.periods)
            if period.payment_date > settlement_date
        )


def _overnight_forward(curve: DiscountFactorCurve, segment: _Segment, day_count: DayCount) -> float:
    tau = day_count.year_fraction(segment.observation, segment.next_observation)
    ratio = curve.discount_factor(segment.observation) / curve.discount_factor(
        segment.next_observation
    )
    rate = (ratio - 1.0) / tau
    if not math.isfinite(rate):
        raise ValueError("projection curve produced a non-finite overnight forward")
    return rate
