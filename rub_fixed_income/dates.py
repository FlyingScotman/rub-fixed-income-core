from __future__ import annotations

from calendar import monthrange
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from .errors import CalendarRangeError


def year_fraction(start: date, end: date) -> float:
    return (end - start).days / 365.0


def add_months(value: date, months: int) -> date:
    month_index = value.month - 1 + months
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    return date(year, month, min(value.day, monthrange(year, month)[1]))


def annual_schedule(start: date, maturity: date) -> tuple[date, ...]:
    if maturity <= start:
        raise ValueError("maturity must be after curve date")
    result: list[date] = []
    cursor = add_months(start, 12)
    while cursor < maturity:
        result.append(cursor)
        cursor = add_months(cursor, 12)
    result.append(maturity)
    return tuple(result)


@dataclass(frozen=True)
class BusinessCalendar:
    """Calendar arithmetic over explicit holiday and working-weekend overrides.

    ``valid_from``/``valid_to`` give the covered date range (both or neither). A
    bounded calendar raises ``CalendarRangeError`` for any date outside it instead of
    assuming Mon-Fri. An unbounded calendar keeps the pre-0.9.0 behaviour: weekdays
    are business days unless listed as holidays. Holidays and working days are frozen
    on construction, so a caller's mutable set cannot change the calendar later.
    """

    holidays: frozenset[date] = field(default_factory=frozenset)
    working_days: frozenset[date] = field(default_factory=frozenset)
    version: str = "weekends-only"
    valid_from: date | None = None
    valid_to: date | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "holidays", frozenset(self.holidays))
        object.__setattr__(self, "working_days", frozenset(self.working_days))
        if not isinstance(self.version, str) or not self.version:
            raise ValueError("calendar version must be a non-empty string")
        for day in self.holidays | self.working_days:
            if not isinstance(day, date) or isinstance(day, datetime):
                raise TypeError("calendar overrides must be dates")
        if self.holidays & self.working_days:
            raise ValueError("a date cannot be both a holiday and a working day")
        if (self.valid_from is None) != (self.valid_to is None):
            raise ValueError("valid_from and valid_to must be given together")
        if self.valid_from is not None and self.valid_to is not None:
            if self.valid_to < self.valid_from:
                raise ValueError("valid_to must not precede valid_from")
            outside = [
                day
                for day in self.holidays | self.working_days
                if not self.valid_from <= day <= self.valid_to
            ]
            if outside:
                raise ValueError("calendar overrides must lie inside the covered range")

    @property
    def is_bounded(self) -> bool:
        return self.valid_from is not None

    def covers(self, value: date) -> bool:
        if self.valid_from is None or self.valid_to is None:
            return True
        return self.valid_from <= value <= self.valid_to

    def require_covered(self, value: date) -> None:
        if not self.covers(value):
            raise CalendarRangeError(
                f"{value.isoformat()} is outside calendar {self.version} "
                f"({self.valid_from}..{self.valid_to})"
            )

    def is_business_day(self, value: date) -> bool:
        self.require_covered(value)
        if value in self.working_days:
            return True
        return value.weekday() < 5 and value not in self.holidays

    def add_business_days(self, value: date, count: int) -> date:
        if count < 0:
            raise ValueError("negative business-day lags are not supported")
        self.require_covered(value)
        current = value
        added = 0
        while added < count:
            current += timedelta(days=1)
            if self.is_business_day(current):
                added += 1
        return current

    def modified_following(self, value: date) -> date:
        if self.is_business_day(value):
            return value
        following = value
        while not self.is_business_day(following):
            following += timedelta(days=1)
        if following.month == value.month:
            return following
        preceding = value
        while not self.is_business_day(preceding):
            preceding -= timedelta(days=1)
        return preceding
