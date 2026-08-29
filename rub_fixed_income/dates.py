from __future__ import annotations

from calendar import monthrange
from dataclasses import dataclass, field
from datetime import date, timedelta


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
    """Calendar arithmetic over explicit holiday and working-weekend overrides."""

    holidays: frozenset[date] = field(default_factory=frozenset)
    working_days: frozenset[date] = field(default_factory=frozenset)
    version: str = "weekends-only"

    def is_business_day(self, value: date) -> bool:
        if value in self.working_days:
            return True
        return value.weekday() < 5 and value not in self.holidays

    def add_business_days(self, value: date, count: int) -> date:
        if count < 0:
            raise ValueError("negative business-day lags are not supported")
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
