"""Contractual RUB floating coupons: realized fixings joined with curve projection.

An accrual day takes the fixing of the latest business day on or before it,
optionally looked back a number of business days ("lookback without observation
shift": rates move, weights stay on accrual days). An observation shift moves the
whole observation period instead (rates and weights), by business or calendar days.
A key-rate floater may observe calendar days (the rate in force on each day from its
effective date) or a single fixing date in advance or in arrears. Observations dated
before the history's valuation date come from published fixings and never move under
curve shocks; later observations are overnight forwards of the projection curve.

The KBD index (0.34.0, MOEX ``ZR_YLD_CRV``) is a constant-maturity rate: one fixing of
the effective annual KBD zero yield at a fixed tenor, published with the ZCYC curve. A
projected fixing is the forward of that yield on the projection curve (the KBD),
optionally with a convexity adjustment from an explicit normal volatility.

Floors and caps bound one period's rate, on the benchmark average or on the all-in
rate. They are priced at intrinsic value on the projected rate, which is accepted only
when the projected rate is at least an explicit guard away from every strike: time
value is not modelled, so a near-the-money option is a typed rejection.

No I/O: the caller supplies a point-in-time fixing snapshot and the publication calendar.
"""

from __future__ import annotations

import math
from bisect import bisect_right
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from itertools import pairwise
from typing import Literal, Protocol

from ._money import finite_money, non_negative_money, positive_money
from .canonical import OMIT_IF_DEFAULT, _canonical_value, canonical_sha256
from .conventions import DayCount, calendar_fingerprint
from .dates import BusinessCalendar, add_months
from .discount import DiscountCurve, DiscountFactorCurve
from .errors import InvalidTerms, MissingFixing, MissingMarketData, UnsupportedProduct
from .key_rate import KeyRateForwardCurve

FixingIndex = Literal["ruonia", "key_rate", "kbd"]
IndexAveraging = Literal["compounded", "arithmetic", "single_fixing"]
CouponSource = Literal["published", "fixed_by_history", "partially_projected", "projected"]
DayUnit = Literal["business_days", "calendar_days"]
ObservationDays = Literal["business", "calendar"]
BoundTarget = Literal["coupon_rate", "index_rate"]
BoundKind = Literal["floor", "cap"]
FixingAnchor = Literal["accrual_start", "accrual_end", "payment_date"]
FLOATING_MODEL_VERSION = "rub-floating-coupon-v1"
KEY_RATE_PROJECTION_MODEL_VERSION = "key-rate-projection-df-v1"

# RUONIA and the KBD are published for every business day of their publication calendar;
# the key rate stays in force until changed.
_DAILY_PUBLISHED: Mapping[str, bool] = {"ruonia": True, "key_rate": False, "kbd": True}
_LABELS: Mapping[str, str] = {"ruonia": "RUONIA", "key_rate": "key rate", "kbd": "KBD"}
_MAX_INDEX_TENOR_YEARS = 30.0


def _index_tenor(value: float | None, index: str, name: str) -> float | None:
    """A KBD index needs its constant maturity in years; the other indexes take none."""
    if index != "kbd":
        if value is not None:
            raise ValueError(f"{name} is a term of the KBD index only")
        return None
    if value is None:
        raise ValueError(f"the KBD index needs {name}")
    tenor = positive_money(value, name)
    if tenor > _MAX_INDEX_TENOR_YEARS:
        raise ValueError(f"{name} must not exceed {_MAX_INDEX_TENOR_YEARS:g} years")
    return tenor


def _tenor_days(tenor_years: float) -> int:
    """The forward's length: the tenor in ACT/365F days (the KBD's time basis)."""
    return max(1, round(tenor_years * 365.0))


_DAY_UNITS = ("business_days", "calendar_days")


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


def _move_back(calendar: BusinessCalendar, value: date, count: int, unit: DayUnit) -> date:
    """``count`` business days strictly before ``value``, or ``count`` calendar days."""
    if unit == "calendar_days":
        return value - timedelta(days=count)
    return _shift_back(calendar, value, count)


def _round_half_up(value: float, decimals: int) -> float:
    return float(Decimal(str(value)).quantize(Decimal(1).scaleb(-decimals), ROUND_HALF_UP))


def _optional_decimals(value: int | None, name: str) -> None:
    if value is not None and (
        isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 12
    ):
        raise ValueError(f"{name} must be None or an integer between 0 and 12")


def _day_count(value: object, name: str, low: int, high: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ValueError(f"{name} must be an integer between {low} and {high}")


def _parse_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"known_at_utc {value!r} is not an ISO 8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"known_at_utc {value!r} has no UTC offset")
    return parsed


@dataclass(frozen=True)
class FixingHistory:
    """Fixings known for observations strictly before ``valuation_date``.

    Rates are decimal annual (0.15 = 15 %). ``snapshot_id`` names the caller's
    point-in-time selection (for example a market-data-store fixing snapshot).
    RUONIA must have a fixing on every ``calendar`` business day before the
    latest fixing; only the trailing ``max_unpublished_business_days`` business
    days before ``valuation_date`` may carry the latest fixing. Key-rate rows are
    dated by the **effective date** of each CBR decision: a rate applies from that
    date until the next row, so gaps between them are not errors. An empty history
    projects coupons observed wholly on or after ``valuation_date``.

    ``published_at`` optionally gives each row's publication time (UTC-aware, aligned
    with ``fixings``); :meth:`published_by` then drops rows published after a cut.

    ``calendar`` is the **publication calendar** of the index, not the exchange's: its
    business days are exactly the days with a fixing. For RUONIA that differs from the
    official calendar on the last business day of the year, which CBR does not compute
    (2023-12-29, the working Saturday 2024-12-28, 2025-12-30): mark it a holiday and the
    previous fixing covers it, as in MOEX coupons. With the official calendar the day is a
    missing fixing and raises; core never fills it.

    The KBD (0.34.0) has one history per tenor: ``tenor_years`` is the constant maturity
    and the rows are the effective annual KBD zero yields at it (``kbd_effective_yield``
    of each day's ZCYC publication), on the ZCYC publication calendar.
    """

    index: FixingIndex
    valuation_date: date
    fixings: tuple[tuple[date, float], ...]
    calendar: BusinessCalendar
    snapshot_id: str
    max_unpublished_business_days: int = 0
    known_at_utc: str | None = None
    _dates: tuple[date, ...] = field(init=False, repr=False, compare=False)
    published_at: tuple[datetime, ...] = field(default=(), metadata=OMIT_IF_DEFAULT)
    tenor_years: float | None = field(default=None, metadata=OMIT_IF_DEFAULT)

    def __post_init__(self) -> None:
        if self.index not in _DAILY_PUBLISHED:
            raise ValueError("fixing index must be ruonia, key_rate or kbd")
        object.__setattr__(
            self, "tenor_years", _index_tenor(self.tenor_years, self.index, "tenor_years")
        )
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
        for (left, _), (right, _) in pairwise(rows):
            if right <= left:
                raise ValueError("fixing dates must be strictly increasing")
        if rows and rows[-1][0] >= self.valuation_date:
            raise ValueError("fixing history must end before the valuation date")
        if any(rate <= -1.0 for _, rate in rows):
            raise ValueError("fixing rates must exceed -100 %")
        if _DAILY_PUBLISHED[self.index]:
            for day, _ in rows:
                if not self.calendar.is_business_day(day):
                    raise ValueError(
                        f"{_LABELS[self.index]} fixing on non-business day {day.isoformat()}"
                    )
        published = tuple(self.published_at)
        if published:
            if len(published) != len(rows):
                raise ValueError("published_at must give one timestamp per fixing")
            for stamp in published:
                if (
                    not isinstance(stamp, datetime)
                    or stamp.tzinfo is None
                    or stamp.utcoffset() is None
                ):
                    raise ValueError("publication timestamps must have explicit UTC offsets")
        object.__setattr__(self, "fixings", rows)
        object.__setattr__(self, "published_at", published)
        object.__setattr__(self, "_dates", tuple(day for day, _ in rows))

    @property
    def latest_fixing_date(self) -> date | None:
        return self._dates[-1] if self._dates else None

    @property
    def known_at(self) -> datetime | None:
        """``known_at_utc`` parsed as an aware timestamp; raises if it is not ISO 8601."""
        return None if self.known_at_utc is None else _parse_timestamp(self.known_at_utc)

    def published_by(self, cut: datetime) -> FixingHistory:
        """The history without the rows published after ``cut`` (needs ``published_at``).

        A dropped RUONIA row inside the history becomes a missing business day, which
        fails on use; a dropped trailing row becomes an unpublished day.
        """
        if cut.tzinfo is None or cut.utcoffset() is None:
            raise ValueError("the publication cut must have an explicit UTC offset")
        if not self.published_at:
            raise ValueError("this history carries no publication timestamps")
        kept = [
            (row, stamp)
            for row, stamp in zip(self.fixings, self.published_at, strict=True)
            if stamp <= cut
        ]
        if len(kept) == len(self.fixings):
            return self
        return replace(
            self,
            fixings=tuple(row for row, _ in kept),
            published_at=tuple(stamp for _, stamp in kept),
        )

    @property
    def unpublished_business_days(self) -> tuple[date, ...]:
        """Business days after the latest fixing and before the valuation date."""
        latest = self.latest_fixing_date
        if not _DAILY_PUBLISHED[self.index] or latest is None:
            return ()
        days = []
        day = latest + timedelta(days=1)
        while day < self.valuation_date:
            if self.calendar.is_business_day(day):
                days.append(day)
            day += timedelta(days=1)
        return tuple(days)

    @property
    def identity(self) -> dict[str, object]:
        identity: dict[str, object] = {
            "index": self.index,
            "valuation_date": self.valuation_date,
            "snapshot_id": self.snapshot_id,
            "known_at_utc": self.known_at_utc,
            "calendar_version": self.calendar.version,
            "calendar_fingerprint": calendar_fingerprint(self.calendar),
            "max_unpublished_business_days": self.max_unpublished_business_days,
            "fixings_sha256": canonical_sha256(self.fixings),
        }
        if self.published_at:
            identity["published_at_sha256"] = canonical_sha256(self.published_at)
        if self.tenor_years is not None:
            identity["tenor_years"] = self.tenor_years
        return identity

    def rate(self, observation: date) -> tuple[float, bool]:
        """Return the rate for an observation and whether it was carried.

        RUONIA observations are business days. A key-rate observation may be any day:
        it takes the row with the latest effective date on or before it.
        """
        if observation >= self.valuation_date:
            raise ValueError("observation is not before the valuation date")
        label = _LABELS[self.index]
        position = bisect_right(self._dates, observation) - 1
        if position < 0:
            raise MissingFixing(f"no {label} fixing on or before {observation.isoformat()}")
        if not _DAILY_PUBLISHED[self.index] or self._dates[position] == observation:
            return self.fixings[position][1], False
        if observation < self._dates[-1]:
            raise MissingFixing(f"missing {label} fixing on business day {observation.isoformat()}")
        unpublished = self.unpublished_business_days
        if len(unpublished) > self.max_unpublished_business_days:
            raise MissingFixing(
                f"{label} is unpublished for {len(unpublished)} business days from "
                f"{unpublished[0].isoformat()}; at most "
                f"{self.max_unpublished_business_days} may carry the latest fixing"
            )
        return self.fixings[-1][1], True


@dataclass(frozen=True)
class CouponRateBounds:
    """A floor and/or cap (decimal annual) on one period's rate, applied per period.

    ``applies_to="index_rate"`` bounds the benchmark average before the multiplier and
    margin; ``"coupon_rate"`` bounds the all-in coupon rate after them. Both bounds are
    applied before rounding.
    """

    applies_to: BoundTarget
    floor: float | None = None
    cap: float | None = None

    def __post_init__(self) -> None:
        if self.applies_to not in ("coupon_rate", "index_rate"):
            raise ValueError("bounds apply to coupon_rate or index_rate")
        if self.floor is None and self.cap is None:
            raise ValueError("give a floor, a cap or both")
        if self.floor is not None:
            object.__setattr__(self, "floor", finite_money(self.floor, "floor rate"))
        if self.cap is not None:
            object.__setattr__(self, "cap", finite_money(self.cap, "cap rate"))
        if self.floor is not None and self.cap is not None and not self.floor < self.cap:
            raise ValueError("the floor must be below the cap")

    def clamp(self, rate: float) -> tuple[float, BoundKind | None]:
        if self.floor is not None and rate < self.floor:
            return self.floor, "floor"
        if self.cap is not None and rate > self.cap:
            return self.cap, "cap"
        return rate, None

    def strikes(self) -> tuple[float, ...]:
        return tuple(s for s in (self.floor, self.cap) if s is not None)


@dataclass(frozen=True)
class ObservationShift:
    """Observation period ``[start - L, end - L)`` with rates **and** weights shifted.

    ``days`` business days strictly before each boundary, or calendar days. Each day of
    the shifted window takes the fixing of the latest business day on or before it, and
    the index rate is annualised over the window (``coupon_day_count``).
    ``accrues_over`` says which period's ``coupon_day_count`` the amount uses:

    - ``"observation"``: the shifted window, so
      ``amount = notional * (index_return + margin * tau_window)``. MOEX OFZ-PK and
      RUONIA or key-rate corporates (6, 7 or 4 calendar days) reproduce this way;
    - ``"accrual"``: the accrual period (the ISDA observation-period shift).
    """

    days: int
    unit: DayUnit
    accrues_over: Literal["observation", "accrual"]

    def __post_init__(self) -> None:
        _day_count(self.days, "observation shift", 1, 30)
        if self.unit not in _DAY_UNITS:
            raise ValueError("observation shift unit must be business_days or calendar_days")
        if self.accrues_over not in ("observation", "accrual"):
            raise ValueError("accrues_over must be observation or accrual")


@dataclass(frozen=True)
class ObservationWindow:
    """An in-advance observation window, decoupled from the accrual period.

    The window ends ``end_offset_days`` (business or calendar days) before the accrual
    start and lasts ``length_months`` calendar months or, when None, as many calendar
    days as the coupon period. The index rate is averaged over the window and
    annualised on its ``coupon_day_count``; the amount accrues over the accrual period:
    ``amount = notional * (index_rate + margin) * coupon_day_count(accrual)``.
    Old OFZ-PK 29007-29010 use 6 months to the 2nd business day before the start;
    some monthly corporates a coupon-length window ending 6 calendar days before it.
    """

    end_offset_days: int
    end_unit: DayUnit
    length_months: int | None = None

    def __post_init__(self) -> None:
        _day_count(self.end_offset_days, "window end offset", 0, 30)
        if self.end_unit not in _DAY_UNITS:
            raise ValueError("window end unit must be business_days or calendar_days")
        if self.length_months is not None:
            _day_count(self.length_months, "window length in months", 1, 24)

    def bounds(self, calendar: BusinessCalendar, start: date, end: date) -> tuple[date, date]:
        """The window ``[from, to)`` of the coupon period ``[start, end)``."""
        to = _move_back(calendar, start, self.end_offset_days, self.end_unit)
        if self.length_months is not None:
            return add_months(to, -self.length_months), to
        return to - (end - start), to


@dataclass(frozen=True)
class ConstantMaturityConvexity:
    """Convexity adjustment of a projected constant-maturity fixing (KBD index, 0.34.0).

    The forward ``F`` of a T-year zero yield is not its expectation: the payoff is linear
    in the yield, the zero bond it prices is convex in it. With a normal (absolute)
    volatility ``sigma`` of that yield, ``t`` years to the fixing (ACT/365F from the curve
    date), the adjustment for a zero yield is

        ``F + 0.5 * sigma^2 * t * (T + 1) / (1 + F)``

    (``-G''(F) / G'(F)`` of ``G(y) = (1 + y)^-T``). The timing of the payment after the
    fixing is not adjusted. The volatility is an explicit input in basis points a year;
    without this term (the default) a projected fixing is the deterministic forward.
    """

    normal_vol_bp: float

    def __post_init__(self) -> None:
        vol = non_negative_money(self.normal_vol_bp, "normal volatility bp")
        object.__setattr__(self, "normal_vol_bp", vol)

    def adjust(self, forward: float, years_to_fixing: float, tenor_years: float) -> float:
        if years_to_fixing <= 0.0:
            return forward
        sigma = self.normal_vol_bp * 1e-4
        return forward + 0.5 * sigma * sigma * years_to_fixing * (tenor_years + 1.0) / (
            1.0 + forward
        )


@dataclass(frozen=True)
class FixingDateRule:
    """Single fixing date: ``offset_days`` before ``anchor``, in business or calendar days.

    ``anchor="accrual_start"`` fixes in advance; ``"accrual_end"`` or ``"payment_date"``
    fixes in arrears. A zero offset observes the anchor itself.
    """

    anchor: FixingAnchor
    offset_days: int
    unit: DayUnit

    def __post_init__(self) -> None:
        if self.anchor not in ("accrual_start", "accrual_end", "payment_date"):
            raise ValueError("fixing anchor must be accrual_start, accrual_end or payment_date")
        _day_count(self.offset_days, "fixing offset", 0, 30)
        if self.unit not in _DAY_UNITS:
            raise ValueError("fixing offset unit must be business_days or calendar_days")


class RateDayBasis(Protocol):
    """Accrued days and days a year of a rate accrual (``domain.AccrualBasis``)."""

    def days(self, start: date, end: date) -> tuple[int, int]: ...


@dataclass(frozen=True)
class FloatingCouponRule:
    """Contractual coupon formula: index return over the period plus a margin.

    ``margin`` is decimal annual (0.015 = +150 bp). The index return is
    ``prod(1 + r_i * tau_i) - 1`` (compounded) or ``sum(r_i * tau_i)`` (arithmetic)
    with ``tau_i`` from ``rate_day_count`` over the days each rate applies;
    ``single_fixing`` takes one rate on the ``fixing_date``. The coupon rate is
    ``multiplier * index_rate + margin``, with ``index_rate`` the return divided by
    the ``coupon_day_count`` accrual. Rounding (rate in percent points, amount in RUB,
    half-up) applies only to coupons fully fixed by history: a rounded projection is
    not an expectation.

    ``observation_days="calendar"`` (key rate only) observes each calendar day's rate in
    force; ``"business"`` takes the latest business day on or before each day.

    ``index="kbd"`` (0.34.0, MOEX ``ZR_YLD_CRV``) is the effective annual KBD zero yield at
    the constant maturity ``index_tenor_years``, one fixing (``single_fixing`` with a
    ``fixing_date``, in advance for every bond seen), on ACT/365F. A projected fixing is
    the forward yield of the projection curve over ``[fixing, fixing + tenor)``,
    ``exp(ln(DF(fixing) / DF(fixing + tenor)) / tenor) - 1``, plus the ``convexity``
    adjustment when one is given.
    """

    index: FixingIndex
    averaging: IndexAveraging
    margin: float
    coupon_day_count: DayCount
    rate_day_count: DayCount
    lookback_business_days: int = 0
    rate_rounding_decimals: int | None = None
    amount_rounding_decimals: int | None = None
    multiplier: float = field(default=1.0, metadata=OMIT_IF_DEFAULT)
    bounds: CouponRateBounds | None = field(default=None, metadata=OMIT_IF_DEFAULT)
    observation_shift: ObservationShift | None = field(default=None, metadata=OMIT_IF_DEFAULT)
    observation_days: ObservationDays = field(default="business", metadata=OMIT_IF_DEFAULT)
    fixing_date: FixingDateRule | None = field(default=None, metadata=OMIT_IF_DEFAULT)
    observation_window: ObservationWindow | None = field(default=None, metadata=OMIT_IF_DEFAULT)
    index_tenor_years: float | None = field(default=None, metadata=OMIT_IF_DEFAULT)
    convexity: ConstantMaturityConvexity | None = field(default=None, metadata=OMIT_IF_DEFAULT)

    def __post_init__(self) -> None:
        if self.index not in _DAILY_PUBLISHED:
            raise ValueError("coupon index must be ruonia, key_rate or kbd")
        object.__setattr__(
            self,
            "index_tenor_years",
            _index_tenor(self.index_tenor_years, self.index, "index_tenor_years"),
        )
        if self.convexity is not None:
            if not isinstance(self.convexity, ConstantMaturityConvexity):
                raise TypeError("convexity must be a ConstantMaturityConvexity")
            if self.index != "kbd":
                raise ValueError("a convexity adjustment applies to the KBD index only")
        if self.index == "kbd":
            if self.averaging != "single_fixing":
                raise ValueError("the KBD index is a single fixing (single_fixing averaging)")
            if self.rate_day_count is not DayCount.ACT_365_FIXED:
                raise ValueError("the KBD index is on ACT/365F (rate_day_count)")
        if self.averaging not in ("compounded", "arithmetic", "single_fixing"):
            raise ValueError("averaging must be compounded, arithmetic or single_fixing")
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
        multiplier = finite_money(self.multiplier, "index multiplier")
        if multiplier <= 0.0:
            raise ValueError("the index multiplier must be positive")
        object.__setattr__(self, "multiplier", multiplier)
        if self.bounds is not None and not isinstance(self.bounds, CouponRateBounds):
            raise TypeError("bounds must be CouponRateBounds")
        if self.observation_shift is not None:
            if not isinstance(self.observation_shift, ObservationShift):
                raise TypeError("observation_shift must be an ObservationShift")
            if lookback:
                raise ValueError("use either a lookback or an observation shift, not both")
        if self.observation_days not in ("business", "calendar"):
            raise ValueError("observation_days must be business or calendar")
        if self.observation_days == "calendar":
            if self.index != "key_rate":
                raise ValueError("RUONIA is observed on business days only")
            if lookback:
                raise ValueError("a business-day lookback needs business-day observation")
        if (self.averaging == "single_fixing") != (self.fixing_date is not None):
            raise ValueError("single_fixing averaging needs a fixing_date, and only it")
        if self.fixing_date is not None:
            if not isinstance(self.fixing_date, FixingDateRule):
                raise TypeError("fixing_date must be a FixingDateRule")
            if lookback or self.observation_shift is not None:
                raise ValueError("a single fixing takes no lookback or observation shift")
        if self.observation_window is not None:
            if not isinstance(self.observation_window, ObservationWindow):
                raise TypeError("observation_window must be an ObservationWindow")
            if lookback or self.observation_shift is not None or self.fixing_date is not None:
                raise ValueError(
                    "an observation window takes no lookback, observation shift or fixing date"
                )

    @property
    def identifier(self) -> str:
        payload: dict[str, object] = {
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
        # Terms added after 0.8.0 enter only when used, so older rules keep their IDs.
        if self.multiplier != 1.0:
            payload["multiplier"] = self.multiplier
        for name in ("bounds", "observation_shift", "fixing_date", "observation_window"):
            value = getattr(self, name)
            if value is not None:
                payload[name] = asdict(value)
        if self.observation_days != "business":
            payload["observation_days"] = self.observation_days
        if self.index_tenor_years is not None:
            payload["index_tenor_years"] = self.index_tenor_years
        if self.convexity is not None:
            payload["convexity"] = asdict(self.convexity)
        return canonical_sha256(payload)


@dataclass(frozen=True)
class FloatingCouponPeriod:
    """One coupon period; amounts are RUB per bond on the period's notional.

    ``published_coupon_rub`` is known cash and is used as is. MOEX shows 0 for
    coupons not yet fixed, so a zero is rejected: pass None for those.
    ``published_rate`` (0.33.0, decimal a year, optional) is the rate the issuer
    published with that coupon (bondization ``valueprc``); a leg that accrues on the
    rate (:meth:`FloatingLeg.accrued_interest` with a ``rate_basis``) uses it.
    ``margin`` (0.34.0, decimal a year, optional) replaces the rule's margin for this
    period: a margin reset at an offer.
    """

    accrual_start: date
    accrual_end: date
    payment_date: date
    notional_rub: float
    published_coupon_rub: float | None = None
    published_rate: float | None = field(default=None, metadata=OMIT_IF_DEFAULT)
    margin: float | None = field(default=None, metadata=OMIT_IF_DEFAULT)

    def __post_init__(self) -> None:
        if self.margin is not None:
            object.__setattr__(self, "margin", finite_money(self.margin, "period margin"))
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
        if self.published_rate is not None:
            if self.published_coupon_rub is None:
                raise ValueError("a published rate needs its published coupon")
            if non_negative_money(self.published_rate, "published rate") > 1.0:
                raise ValueError("the published rate is a decimal (0.15 = 15 %)")


@dataclass(frozen=True)
class FloatingCouponProjection:
    """One coupon: amount, source and the rates behind it.

    ``index_rate`` is the benchmark average before any bound; ``coupon_rate`` the
    final (bounded, rounded) rate. ``bound`` names the floor or cap that set it.
    """

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
    bound: BoundKind | None = field(default=None, metadata=OMIT_IF_DEFAULT)


@dataclass(frozen=True)
class _Segment:
    start: date
    end: date
    year_fraction: float
    observation: date
    next_observation: date
    realized_rate: float | None
    carried: bool
    # rate_day_count over [observation, next_observation): an overnight forward's tau.
    forward_tau: float


@dataclass(frozen=True)
class _Plan:
    segments: tuple[_Segment, ...]
    # coupon_day_count over the observation period: the accrual unless shifted.
    index_accrual: float
    projection_dates: tuple[date, ...]
    # coupon_day_count of the amount: the accrual period, or the window it accrues over.
    amount_accrual: float
    realized_days: int
    projected_days: int
    carried: tuple[date, ...]
    # Fast path: the realized segments form a prefix, accumulated here in segment order;
    # the projected ones follow as (memo key, observation, next, tau, year fraction).
    prefix: tuple[float, float, float | None] | None
    steps: tuple[tuple[int, date, date, float, float], ...]


@dataclass(frozen=True)
class FloatingLeg:
    """Coupon periods of one floater with their formula and fixing history.

    Principal is not part of the leg: pass it as fixed cashflows. Periods paid on
    or before the settlement date are skipped and need no history.

    ``strike_guard_bp`` is the model policy for floors and caps: a projected coupon
    is priced at intrinsic value only when its unbounded projected rate is at least
    this far from every strike; otherwise, or when it is None, projection raises
    :class:`UnsupportedProduct`. Coupons fully fixed by history are bounded exactly.
    """

    rule: FloatingCouponRule
    periods: tuple[FloatingCouponPeriod, ...]
    history: FixingHistory
    _plans: dict[int, _Plan] = field(init=False, repr=False, compare=False, default_factory=dict)
    strike_guard_bp: float | None = field(default=None, metadata=OMIT_IF_DEFAULT)

    def __post_init__(self) -> None:
        if not isinstance(self.rule, FloatingCouponRule):
            raise TypeError("rule must be a FloatingCouponRule")
        if not isinstance(self.history, FixingHistory):
            raise TypeError("history must be a FixingHistory")
        if self.rule.index != self.history.index:
            raise ValueError("coupon rule and fixing history index differ")
        if self.rule.index_tenor_years != self.history.tenor_years:
            raise ValueError("coupon rule and fixing history tenors differ")
        periods = tuple(self.periods)
        if not periods or not all(isinstance(p, FloatingCouponPeriod) for p in periods):
            raise ValueError("at least one FloatingCouponPeriod is required")
        for left, right in pairwise(periods):
            if right.accrual_start < left.accrual_end:
                raise ValueError("coupon periods must be ordered and non-overlapping")
        object.__setattr__(self, "periods", periods)
        if self.strike_guard_bp is not None:
            guard = finite_money(self.strike_guard_bp, "strike guard bp")
            if guard < 0.0:
                raise ValueError("strike_guard_bp cannot be negative")
            object.__setattr__(self, "strike_guard_bp", guard)

    @property
    def identity(self) -> dict[str, object]:
        identity: dict[str, object] = {
            "model": FLOATING_MODEL_VERSION,
            "rule": self.rule.identifier,
            # Canonical mappings: equal to ``asdict`` for periods without the 0.33.0
            # ``published_rate``, so earlier identities keep their bytes.
            "periods": [_canonical_value(period) for period in self.periods],
            "history": self.history.identity,
        }
        if self.strike_guard_bp is not None:
            identity["strike_guard_bp"] = self.strike_guard_bp
        return identity

    @property
    def has_optionality(self) -> bool:
        """Whether the rule carries a floor or cap (priced at intrinsic value only)."""
        return self.rule.bounds is not None

    def remaining(self, settlement_date: date) -> tuple[int, ...]:
        """Indexes of the periods paid after ``settlement_date``."""
        return tuple(
            i for i, period in enumerate(self.periods) if period.payment_date > settlement_date
        )

    def projection_dates(self, position: int) -> tuple[date, ...]:
        """Dates whose discount factors the coupon reads from a projection curve."""
        if self.periods[position].published_coupon_rub is not None:
            return ()
        return self._plan(position).projection_dates

    def _segment(
        self, start: date, end: date, observation: date, next_observation: date
    ) -> _Segment:
        realized: float | None = None
        carried = False
        if observation < self.history.valuation_date:
            realized, carried = self.history.rate(observation)
        return _Segment(
            start,
            end,
            self.rule.rate_day_count.year_fraction(start, end),
            observation,
            next_observation,
            realized,
            carried,
            self.rule.rate_day_count.year_fraction(observation, next_observation),
        )

    def _plan(self, position: int) -> _Plan:
        cached = self._plans.get(position)
        if cached is not None:
            return cached
        period = self.periods[position]
        rule = self.rule
        calendar = self.history.calendar
        calendar_days = rule.observation_days == "calendar"
        one_day = timedelta(days=1)
        segments = []
        start, end = period.accrual_start, period.accrual_end
        if rule.fixing_date is not None:
            fixing = rule.fixing_date
            anchor = getattr(period, fixing.anchor)
            fixing_day = _move_back(calendar, anchor, fixing.offset_days, fixing.unit)
            if calendar_days:
                observation, following = fixing_day, fixing_day + one_day
            else:
                observation = _previous_business_day(calendar, fixing_day)
                following = calendar.add_business_days(observation, 1)
            if rule.index_tenor_years is not None:
                # A constant-maturity fixing reads the curve over the index tenor.
                following = observation + timedelta(days=_tenor_days(rule.index_tenor_years))
            segments.append(self._segment(start, end, observation, following))
        else:
            if rule.observation_shift is not None:
                shift = rule.observation_shift
                start = _move_back(calendar, start, shift.days, shift.unit)
                end = _move_back(calendar, end, shift.days, shift.unit)
                if end <= start:
                    raise ValueError("the shifted observation period is empty")
            elif rule.observation_window is not None:
                start, end = rule.observation_window.bounds(calendar, start, end)
            cursor = start
            while cursor < end:
                if calendar_days:
                    following = cursor + one_day
                    segments.append(self._segment(cursor, following, cursor, following))
                    cursor = following
                    continue
                business_day = _previous_business_day(calendar, cursor)
                segment_end = min(calendar.add_business_days(business_day, 1), end)
                observation = _shift_back(calendar, business_day, rule.lookback_business_days)
                segments.append(
                    self._segment(
                        cursor,
                        segment_end,
                        observation,
                        calendar.add_business_days(observation, 1),
                    )
                )
                cursor = segment_end
        dates = sorted(
            {s.observation for s in segments if s.realized_rate is None}
            | {s.next_observation for s in segments if s.realized_rate is None}
        )
        shifted = rule.observation_shift
        observed = shifted is not None and shifted.accrues_over == "observation"
        plan = _Plan(
            tuple(segments),
            rule.coupon_day_count.year_fraction(start, end),
            tuple(dates),
            rule.coupon_day_count.year_fraction(
                start if observed else period.accrual_start,
                end if observed else period.accrual_end,
            ),
            sum((s.end - s.start).days for s in segments if s.realized_rate is not None),
            sum((s.end - s.start).days for s in segments if s.realized_rate is None),
            tuple(s.observation for s in segments if s.carried),
            *_fast_path(segments, rule.averaging == "compounded"),
        )
        self._plans[position] = plan
        return plan

    def coupon(
        self,
        position: int,
        curve: DiscountFactorCurve | None,
        *,
        check_guard: bool = True,
        bridge: tuple[DiscountFactorCurve, date] | None = None,
    ) -> FloatingCouponProjection:
        """Project the coupon of period ``position`` (see :meth:`project`).

        ``check_guard=False`` skips the strike-guard check: risk engines use it on
        shocked curves after the base projection has passed it.
        """
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
        compounding = rule.averaging == "compounded"
        if bridge is None and plan.prefix is not None:
            compounded, arithmetic, single = _project_steps(
                plan, curve, rule.rate_day_count, compounding
            )
        else:
            compounded, arithmetic, single = self._project_segments(
                plan, curve, bridge, compounding
            )
        realized_days, projected_days = plan.realized_days, plan.projected_days
        accrual = plan.amount_accrual
        if rule.averaging == "single_fixing":
            assert single is not None
            index_rate = finite_money(single, "index rate")
        else:
            index_return = compounded - 1.0 if rule.averaging == "compounded" else arithmetic
            index_rate = finite_money(index_return / plan.index_accrual, "index rate")
        fixed = projected_days == 0
        bounds = rule.bounds
        bound: BoundKind | None = None
        bounded_index = index_rate
        unbounded = index_rate
        if bounds is not None and bounds.applies_to == "index_rate":
            bounded_index, bound = bounds.clamp(index_rate)
        margin = rule.margin if period.margin is None else period.margin
        coupon_rate = rule.multiplier * bounded_index + margin
        if bounds is not None and bounds.applies_to == "coupon_rate":
            unbounded = coupon_rate
            coupon_rate, bound = bounds.clamp(coupon_rate)
        if bounds is not None and not fixed and check_guard:
            self._check_guard(period, bounds, unbounded)
        if fixed and rule.rate_rounding_decimals is not None:
            coupon_rate = _round_half_up(coupon_rate * 100.0, rule.rate_rounding_decimals) / 100.0
        amount = period.notional_rub * coupon_rate * accrual
        if fixed and rule.amount_rounding_decimals is not None:
            amount = _round_half_up(amount, rule.amount_rounding_decimals)
        source_label: CouponSource = (
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
            source_label,
            index_rate,
            finite_money(coupon_rate, "coupon rate"),
            realized_days,
            projected_days,
            plan.carried,
            bound,
        )

    def _project_segments(
        self,
        plan: _Plan,
        curve: DiscountFactorCurve | None,
        bridge: tuple[DiscountFactorCurve, date] | None,
        compounding: bool,
    ) -> tuple[float, float, float | None]:
        """Accumulate every segment in order (any plan, with or without a bridge).

        Each forward is ``(DF(obs) / DF(next) - 1) / tau``, tau over ``[obs, next)`` in
        ``rate_day_count``, memoised on the curve object: shocked curves are shared by
        every position of a job, so a forward is computed once per curve. ``_project_steps``
        is the same computation without the bridge, for plans whose realized days lead.
        """
        day_count = self.rule.rate_day_count
        compounded = 1.0
        arithmetic = 0.0
        single: float | None = None
        split = None if bridge is None else bridge[1]
        memos: dict[int, dict[int, float]] = {}
        for segment in plan.segments:
            rate = segment.realized_rate
            if rate is None:
                observation = segment.observation
                source = curve
                if split is not None and observation < split:
                    source = bridge[0]  # type: ignore[index]
                if source is None:
                    raise ValueError("a projection curve is required for unfixed observations")
                if self.rule.index_tenor_years is not None:
                    rate = _constant_maturity_forward(self.rule, segment, source)
                    single = rate
                    continue
                memo = memos.get(id(source))
                if memo is None:
                    memo = memos[id(source)] = _forward_memo(source, day_count)
                key = _forward_key(observation, segment.next_observation)
                rate = memo.get(key)
                if rate is None:
                    ratio = source.discount_factor(observation) / source.discount_factor(
                        segment.next_observation
                    )
                    rate = (ratio - 1.0) / segment.forward_tau
                    if not math.isfinite(rate):
                        raise ValueError("projection curve produced a non-finite overnight forward")
                    if len(memo) < _FORWARD_MEMO_SIZE:
                        memo[key] = rate
            if compounding:
                compounded *= 1.0 + rate * segment.year_fraction
            else:
                arithmetic += rate * segment.year_fraction
            single = rate
        return compounded, arithmetic, single

    def _check_guard(
        self, period: FloatingCouponPeriod, bounds: CouponRateBounds, rate: float
    ) -> None:
        label = f"coupon {period.accrual_start.isoformat()}..{period.accrual_end.isoformat()}"
        guard = self.strike_guard_bp
        if guard is None:
            raise UnsupportedProduct(
                f"{label} has a projected floor or cap: core prices intrinsic value only and "
                "needs an explicit strike_guard_bp to accept it"
            )
        for strike in bounds.strikes():
            distance = abs(rate - strike) * 10_000.0
            if distance < guard:
                raise UnsupportedProduct(
                    f"{label}: projected {bounds.applies_to} is {distance:.2f} bp from a strike "
                    f"of {strike:.6f}, inside the {guard:g} bp guard; option time value is "
                    "not modelled"
                )

    def accrued_interest(
        self,
        settlement_date: date,
        curve: DiscountFactorCurve | None = None,
        *,
        rate_basis: RateDayBasis | None = None,
    ) -> float:
        """Accrued coupon per bond at ``settlement_date``, rounded half-up to 0.01 (#81).

        The rule MOEX ``ACCINT`` follows for RUB floaters (``docs/floating.md``):

        - a published coupon, and a coupon fixed in advance (``fixing_date`` or
          ``observation_window``: fully known at the period start), accrues pro rata on
          its amount, ``amount * elapsed days / period days`` (OFZ-PK, the Minfin rule);
          with ``rate_basis`` (corporates: ``AccrualBasis.RATE_ACT_365``) it accrues on
          its rate instead, ``notional * rate * days / year`` from ``rate_basis.days``,
          the rate being ``published_rate`` or the in-advance formula's rate (a published
          coupon without a rate stays pro rata);
        - a coupon set in arrears accrues the formula over the elapsed part
          ``[accrual_start, settlement_date)``: the realised fixings of that sub-period
          (its observation shift or lookback applied as for the whole coupon), the
          margin, bounds, multiplier and day counts of the rule.

        Settlement on a period start, after the last period or before the first gives 0.
        A sub-period that still needs a projected rate takes it from ``curve``; without
        one it raises :class:`MissingMarketData`.
        """
        for position, period in enumerate(self.periods):
            if not period.accrual_start <= settlement_date < period.accrual_end:
                continue
            if settlement_date == period.accrual_start:
                return 0.0
            elapsed = (settlement_date - period.accrual_start).days
            length = (period.accrual_end - period.accrual_start).days
            rule = self.rule
            try:
                if period.published_coupon_rub is not None or (
                    rule.fixing_date is not None or rule.observation_window is not None
                ):
                    known = self.coupon(position, curve, check_guard=False)
                    rate = (
                        period.published_rate
                        if period.published_coupon_rub is not None
                        else known.coupon_rate
                    )
                    if rate_basis is not None and rate is not None:
                        days, year = rate_basis.days(period.accrual_start, settlement_date)
                        raw = Decimal(repr(period.notional_rub)) * Decimal(repr(rate)) * days / year
                        return float(raw.quantize(Decimal("0.01"), ROUND_HALF_UP))
                    amount = known.amount_rub
                    # Exact decimal pro rata: a float product can fall a hair below a half
                    # kopeck (77.49 * 13 / 182 = 5.535 is 5.534999... in binary).
                    raw = Decimal(repr(amount)) * elapsed / length
                    return float(raw.quantize(Decimal("0.01"), ROUND_HALF_UP))
                part = FloatingCouponPeriod(
                    period.accrual_start,
                    settlement_date,
                    settlement_date,
                    period.notional_rub,
                    margin=period.margin,
                )
                partial = FloatingLeg(rule, (part,), self.history)
                amount = partial.coupon(0, curve, check_guard=False).amount_rub
            except ValueError as exc:
                if curve is None and "projection curve is required" in str(exc):
                    raise MissingMarketData(
                        f"accrued interest on {settlement_date.isoformat()} needs a projected "
                        "rate: pass a projection curve"
                    ) from exc
                raise
            return _round_half_up(amount, 2)
        first, last = self.periods[0], self.periods[-1]
        if settlement_date < first.accrual_start or settlement_date >= last.accrual_end:
            return 0.0
        raise InvalidTerms("no floating coupon period contains the settlement date")

    def project(
        self,
        curve: DiscountFactorCurve | None,
        *,
        settlement_date: date,
        check_guard: bool = True,
        bridge: tuple[DiscountFactorCurve, date] | None = None,
    ) -> tuple[FloatingCouponProjection, ...]:
        """Project every coupon paid after ``settlement_date``.

        ``curve`` projects unfixed observations; it must be dated at the history's
        valuation date. It may be None only when every remaining coupon is known.
        A negative projected coupon raises: give a floor if the terms have one.

        ``bridge=(start_curve, until)`` projects observations in
        ``[valuation_date, until)`` off ``start_curve`` (dated at the valuation date)
        and later ones off ``curve`` (dated ``until``): the horizon path on which the
        start curve's forwards are realised.
        """
        expected = self.history.valuation_date
        if bridge is not None:
            if bridge[0].curve_date != expected:
                raise ValueError("bridge curve date must equal the fixing history valuation date")
            if bridge[1] < expected:
                raise ValueError("the bridge cannot end before the valuation date")
            expected = bridge[1]
        if curve is not None and curve.curve_date != expected:
            raise ValueError("projection curve date must equal the fixing history valuation date")
        return tuple(
            self.coupon(i, curve, check_guard=check_guard, bridge=bridge)
            for i in self.remaining(settlement_date)
        )


_FORWARD_MEMO_SIZE = 65_536


def _constant_maturity_forward(
    rule: FloatingCouponRule, segment: _Segment, curve: DiscountFactorCurve
) -> float:
    """The projected KBD fixing: the forward effective annual yield over the tenor."""
    tau = segment.forward_tau
    growth = curve.discount_factor(segment.observation) / curve.discount_factor(
        segment.next_observation
    )
    forward = math.expm1(math.log(growth) / tau)
    if not math.isfinite(forward):
        raise ValueError("projection curve produced a non-finite constant-maturity forward")
    if rule.convexity is not None:
        assert rule.index_tenor_years is not None
        years = max((segment.observation - curve.curve_date).days, 0) / 365.0
        forward = rule.convexity.adjust(forward, years, rule.index_tenor_years)
    return forward


def _forward_memo(curve: DiscountFactorCurve, day_count: DayCount) -> dict[int, float]:
    """Overnight forwards already read from ``curve`` on ``day_count``, beside its fields.

    Keys are ``observation ordinal * 64 + days to the next observation``. Curves without
    an instance dictionary get a fresh memo per coupon. The values are the floats the
    forward formula gives, so numbers never depend on the memo.
    """
    state = getattr(curve, "__dict__", None)
    if state is None:
        return {}
    memos = state.get("_forward_memo")
    if memos is None:
        memos = {}
        object.__setattr__(curve, "_forward_memo", memos)
    memo = memos.get(day_count)
    if memo is None:
        memo = memos[day_count] = {}
    return memo  # type: ignore[no-any-return]


def _forward_key(observation: date, following: date) -> int:
    return observation.toordinal() * 64 + (following - observation).days


def _fast_path(
    segments: list[_Segment], compounding: bool
) -> tuple[
    tuple[float, float, float | None] | None, tuple[tuple[int, date, date, float, float], ...]
]:
    """The realized prefix and the projected steps, or (None, ()) when they interleave."""
    compounded, arithmetic, single = 1.0, 0.0, None
    steps = []
    for segment in segments:
        rate = segment.realized_rate
        if rate is None:
            if (segment.next_observation - segment.observation).days >= 64:
                return None, ()
            steps.append(
                (
                    _forward_key(segment.observation, segment.next_observation),
                    segment.observation,
                    segment.next_observation,
                    segment.forward_tau,
                    segment.year_fraction,
                )
            )
            continue
        if steps:
            return None, ()
        if compounding:
            compounded *= 1.0 + rate * segment.year_fraction
        else:
            arithmetic += rate * segment.year_fraction
        single = rate
    return (compounded, arithmetic, single), tuple(steps)


def _project_steps(
    plan: _Plan, curve: DiscountFactorCurve | None, day_count: DayCount, compounding: bool
) -> tuple[float, float, float | None]:
    """Continue the realized prefix over the projected steps (same order of operations)."""
    assert plan.prefix is not None
    compounded, arithmetic, single = plan.prefix
    if not plan.steps:
        return compounded, arithmetic, single
    if curve is None:
        raise ValueError("a projection curve is required for unfixed observations")
    memo = _forward_memo(curve, day_count)
    get = memo.get
    discount = curve.discount_factor
    for key, observation, following, tau, year_fraction in plan.steps:
        rate = get(key)
        if rate is None:
            rate = (discount(observation) / discount(following) - 1.0) / tau
            if not math.isfinite(rate):
                raise ValueError("projection curve produced a non-finite overnight forward")
            if len(memo) < _FORWARD_MEMO_SIZE:
                memo[key] = rate
        if compounding:
            compounded *= 1.0 + rate * year_fraction
        else:
            arithmetic += rate * year_fraction
        single = rate
    return compounded, arithmetic, single


def key_rate_projection_curve(
    forward: KeyRateForwardCurve,
    calendar: BusinessCalendar,
    *,
    rate_day_count: DayCount,
    observation_days: ObservationDays,
) -> DiscountCurve:
    """A discount-factor curve whose overnight forwards are the key-rate path.

    Nodes sit on every business day (``observation_days="business"``) or calendar day
    up to the forward curve's horizon, with
    ``DF(next) = DF(day) / (1 + rate_at(day) * tau(day, next))`` and ``tau`` from
    ``rate_day_count``. Overnight forwards of a floating leg observed the same way and
    weighted by the same day count then reproduce the key rate in force exactly; the
    curve extrapolates flat-forward beyond the horizon. Use it as the projection curve
    of a key-rate floater (spec ``rub_key_rate_projection_v1``).
    """
    if observation_days not in ("business", "calendar"):
        raise ValueError("observation_days must be business or calendar")
    if not isinstance(rate_day_count, DayCount):
        raise TypeError("rate_day_count must be a DayCount")
    nodes: list[tuple[date, float]] = []
    discount = 1.0
    day = forward.curve_date
    horizon = forward.horizon_date
    while day < horizon:
        following = (
            day + timedelta(days=1)
            if observation_days == "calendar"
            else calendar.add_business_days(day, 1)
        )
        rate = forward.rate_at(day)
        growth = 1.0 + rate * rate_day_count.year_fraction(day, following)
        discount = positive_money(discount / growth, "key-rate projection discount factor")
        nodes.append((following, discount))
        day = following
    if not nodes:
        raise ValueError("the key-rate forward curve has no horizon")
    return DiscountCurve(forward.curve_date, tuple(nodes), KEY_RATE_PROJECTION_MODEL_VERSION)
