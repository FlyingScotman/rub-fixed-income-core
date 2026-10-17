"""Explicit, immutable, hashable market conventions.

Every default reproduces the numbers produced by rub_fixed_income <= 0.2.1:
ACT/365F time, annual compounding, unadjusted legacy schedules, curve-date anchoring.
Conventions are inputs; nothing here encodes an official holiday list. Callers must supply
a versioned :class:`~rub_fixed_income.dates.BusinessCalendar` built from an official source.
"""

from __future__ import annotations

import hashlib
import math
from calendar import isleap
from dataclasses import dataclass, field
from datetime import date, timedelta
from enum import Enum
from itertools import pairwise
from typing import Literal

from .dates import BusinessCalendar, add_months


class DayCount(Enum):
    """Year-fraction conventions. ``ACT/ACT-ICMA`` is intentionally not provided yet."""

    ACT_365_FIXED = "ACT/365F"
    ACT_360 = "ACT/360"
    ACT_ACT_ISDA = "ACT/ACT-ISDA"

    def year_fraction(self, start: date, end: date) -> float:
        if end < start:
            raise ValueError("day count: end must not precede start")
        if self is DayCount.ACT_365_FIXED:
            return (end - start).days / 365.0
        if self is DayCount.ACT_360:
            return (end - start).days / 360.0
        result = 0.0
        cursor = start
        while cursor < end:
            boundary = min(end, date(cursor.year + 1, 1, 1))
            result += (boundary - cursor).days / (366.0 if isleap(cursor.year) else 365.0)
            cursor = boundary
        return result


@dataclass(frozen=True)
class Compounding:
    """Rate compounding. ``periods_per_year == 0`` is the continuous sentinel."""

    periods_per_year: int

    def __post_init__(self) -> None:
        if isinstance(self.periods_per_year, bool) or not isinstance(self.periods_per_year, int):
            raise TypeError("compounding periods_per_year must be an integer")
        if self.periods_per_year < 0:
            raise ValueError("compounding periods_per_year must be >= 0 (0 = continuous)")

    @property
    def is_continuous(self) -> bool:
        return self.periods_per_year == 0

    @property
    def identifier(self) -> str:
        if self.is_continuous:
            return "continuous"
        if self.periods_per_year == 1:
            return "annual"
        return f"m{self.periods_per_year}"

    def discount_factor(self, rate: float, time: float) -> float:
        if not (math.isfinite(rate) and math.isfinite(time)) or time < 0.0:
            raise ValueError("rate and time must be finite and time non-negative")
        if self.is_continuous:
            return math.exp(-rate * time)
        base = 1.0 + rate / self.periods_per_year
        if base <= 0.0:
            raise ValueError("rate is outside the compounding domain")
        return float(base ** (-self.periods_per_year * time))

    def rate_from_discount_factor(self, discount_factor: float, time: float) -> float:
        if not (math.isfinite(discount_factor) and discount_factor > 0.0):
            raise ValueError("discount factor must be positive and finite")
        if not (math.isfinite(time) and time > 0.0):
            raise ValueError("time must be positive and finite")
        if self.is_continuous:
            return -math.log(discount_factor) / time
        m = self.periods_per_year
        return float(m * (discount_factor ** (-1.0 / (m * time)) - 1.0))


ANNUAL = Compounding(1)
SEMI_ANNUAL = Compounding(2)
QUARTERLY = Compounding(4)
MONTHLY = Compounding(12)
CONTINUOUS = Compounding(0)


class BusinessDayConvention(Enum):
    UNADJUSTED = "UNADJUSTED"
    FOLLOWING = "FOLLOWING"
    MODIFIED_FOLLOWING = "MODIFIED_FOLLOWING"
    PRECEDING = "PRECEDING"


def adjust_date(value: date, convention: BusinessDayConvention, calendar: BusinessCalendar) -> date:
    """Roll ``value`` to a business day of ``calendar`` according to ``convention``."""
    if convention is BusinessDayConvention.UNADJUSTED or calendar.is_business_day(value):
        return value
    if convention is BusinessDayConvention.MODIFIED_FOLLOWING:
        return calendar.modified_following(value)
    step = timedelta(days=1 if convention is BusinessDayConvention.FOLLOWING else -1)
    current = value
    while not calendar.is_business_day(current):
        current += step
    return current


def calendar_fingerprint(calendar: BusinessCalendar) -> str:
    """Stable content hash of a calendar's overrides and covered range (not its label).

    The range enters the hash only when the calendar is bounded, so unbounded
    calendars keep their pre-0.9.0 fingerprints.
    """
    parts = [
        ",".join(sorted(day.isoformat() for day in calendar.holidays)),
        ",".join(sorted(day.isoformat() for day in calendar.working_days)),
    ]
    if calendar.valid_from is not None and calendar.valid_to is not None:
        parts.append(f"{calendar.valid_from.isoformat()}..{calendar.valid_to.isoformat()}")
    return hashlib.sha256("|".join(parts).encode("ascii")).hexdigest()[:12]


def calendar_identity(calendar: BusinessCalendar) -> tuple[str, str]:
    """``(version, fingerprint)`` to record in results computed with ``calendar``."""
    return calendar.version, calendar_fingerprint(calendar)


@dataclass(frozen=True)
class SettlementConvention:
    """Settlement = ``lag_business_days`` business days after trade date on ``calendar``."""

    lag_business_days: int = 1
    calendar: BusinessCalendar = field(default_factory=BusinessCalendar)

    def __post_init__(self) -> None:
        if self.lag_business_days < 0:
            raise ValueError("settlement lag must be a non-negative number of business days")

    def settlement_date(self, trade_date: date) -> date:
        return self.calendar.add_business_days(trade_date, self.lag_business_days)

    @property
    def identifier(self) -> str:
        return (
            f"settlement:T+{self.lag_business_days}:"
            f"{self.calendar.version}@{calendar_fingerprint(self.calendar)}"
        )


@dataclass(frozen=True)
class ScheduleConvention:
    """Regular coupon/fixed-leg schedule rule.

    Roll dates are generated from *unadjusted* dates (``forward`` from the start date or
    ``backward`` from the end date), then adjusted with ``business_day_convention`` on
    ``calendar``. A roll whose adjusted date falls on or after the end date is merged into
    the final period, so a business-day-adjusted end date never creates a 1-day stub. The
    end date is used as given (pass the already adjusted contractual maturity).
    """

    frequency_months: int = 12
    roll: Literal["forward", "backward"] = "forward"
    business_day_convention: BusinessDayConvention = BusinessDayConvention.MODIFIED_FOLLOWING
    calendar: BusinessCalendar = field(default_factory=BusinessCalendar)

    def __post_init__(self) -> None:
        if self.frequency_months < 1:
            raise ValueError("schedule frequency must be a positive number of months")
        if self.roll not in ("forward", "backward"):
            raise ValueError("schedule roll must be 'forward' or 'backward'")

    @property
    def identifier(self) -> str:
        return (
            f"schedule:{self.frequency_months}M:{self.roll}:"
            f"{self.business_day_convention.value}:"
            f"{self.calendar.version}@{calendar_fingerprint(self.calendar)}"
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "frequency_months": self.frequency_months,
            "roll": self.roll,
            "business_day_convention": self.business_day_convention.value,
            "calendar_version": self.calendar.version,
            "calendar_fingerprint": calendar_fingerprint(self.calendar),
        }


def generate_schedule(start: date, end: date, convention: ScheduleConvention) -> tuple[date, ...]:
    """Return adjusted payment dates after ``start`` up to and including ``end``."""
    if end <= start:
        raise ValueError("schedule end must be after start")
    months = convention.frequency_months
    unadjusted: list[date] = []
    count = 1
    while True:
        if convention.roll == "forward":
            cursor = add_months(start, months * count)
            if cursor >= end:
                break
        else:
            cursor = add_months(end, -months * count)
            if cursor <= start:
                break
        unadjusted.append(cursor)
        count += 1
    if convention.roll == "backward":
        unadjusted.reverse()
    adjusted = [
        adjust_date(value, convention.business_day_convention, convention.calendar)
        for value in unadjusted
    ]
    result = [value for value in adjusted if start < value < end]
    result.append(end)
    if any(left >= right for left, right in pairwise(result)):
        raise ValueError("adjusted schedule dates must be strictly increasing")
    return tuple(result)


@dataclass(frozen=True)
class YieldConvention:
    """Yield-to-horizon convention: P = sum CF / (1 + y/m)^(m * t), t by ``day_count``."""

    compounding: Compounding = ANNUAL
    day_count: DayCount = DayCount.ACT_365_FIXED

    def __post_init__(self) -> None:
        if self.compounding.is_continuous:
            raise ValueError("yield convention does not support continuous compounding")

    @property
    def identifier(self) -> str:
        return f"yield:{self.compounding.identifier}:{self.day_count.value}"

    def to_dict(self) -> dict[str, object]:
        return {
            "periods_per_year": self.compounding.periods_per_year,
            "day_count": self.day_count.value,
        }


DEFAULT_YIELD_CONVENTION = YieldConvention()
"""Effective annual on ACT/365F: the MOEX quote basis and the library default."""


@dataclass(frozen=True)
class ZSpreadConvention:
    """Z-spread compounding and spread-time day count. Desk default: annual, ACT/365F."""

    compounding: Compounding = ANNUAL
    day_count: DayCount = DayCount.ACT_365_FIXED

    @property
    def identifier(self) -> str:
        return f"zspread:{self.compounding.identifier}:{self.day_count.value}"

    def to_dict(self) -> dict[str, object]:
        return {
            "periods_per_year": self.compounding.periods_per_year,
            "day_count": self.day_count.value,
        }


@dataclass(frozen=True)
class CurveConvention:
    """Discount-curve conventions.

    ``time_day_count`` measures curve time for zero/forward rates, flat-forward
    extrapolation and flat curves. Interpolation is log-linear in discount factor by
    calendar days; extrapolation is flat-forward from the last two nodes. Only these are
    supported and are recorded for provenance. ``fixed_leg_schedule=None`` selects the legacy
    OIS fixed-leg schedule (annual forward roll from curve date, unadjusted, short end stub);
    pass a :class:`ScheduleConvention` for an adjusted stub-free schedule.
    """

    time_day_count: DayCount = DayCount.ACT_365_FIXED
    interpolation: Literal["log-linear-df"] = "log-linear-df"
    extrapolation: Literal["flat-forward"] = "flat-forward"
    fixed_leg_schedule: ScheduleConvention | None = None
    fixed_leg_day_count: DayCount = DayCount.ACT_365_FIXED

    def __post_init__(self) -> None:
        if self.interpolation != "log-linear-df":
            raise ValueError("only log-linear-df interpolation is supported")
        if self.extrapolation != "flat-forward":
            raise ValueError("only flat-forward extrapolation is supported")

    @property
    def identifier(self) -> str:
        fixed = (
            "legacy-annual-forward"
            if self.fixed_leg_schedule is None
            else self.fixed_leg_schedule.identifier
        )
        return (
            f"curve:time={self.time_day_count.value}:interp={self.interpolation}:"
            f"extrap={self.extrapolation}:fixed={fixed}:{self.fixed_leg_day_count.value}"
        )
