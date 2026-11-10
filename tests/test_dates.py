from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta

import pytest

from rub_fixed_income import BusinessCalendar, CalendarRangeError


def test_business_calendar_applies_working_weekend_and_holiday_overrides() -> None:
    calendar = BusinessCalendar(
        holidays=frozenset({date(2026, 8, 10)}),
        working_days=frozenset({date(2026, 8, 8)}),
        version="test-calendar-v1",
    )

    assert calendar.add_business_days(date(2026, 8, 7), 1) == date(2026, 8, 8)
    assert calendar.add_business_days(date(2026, 8, 8), 1) == date(2026, 8, 11)


def _reference_add_business_days(calendar: BusinessCalendar, value: date, count: int) -> date:
    """The day-by-day walk the memoised ``add_business_days`` must agree with."""
    calendar.require_covered(value)
    current = value
    added = 0
    while added < count:
        current += timedelta(days=1)
        if calendar.is_business_day(current):
            added += 1
    return current


def _reference_is_business_day(calendar: BusinessCalendar, value: date) -> bool:
    calendar.require_covered(value)
    if value in calendar.working_days:
        return True
    return value.weekday() < 5 and value not in calendar.holidays


def test_add_business_days_matches_the_day_by_day_walk_on_every_start() -> None:
    holidays = frozenset(date(2026, 1, 1) + timedelta(days=d) for d in (0, 1, 6, 7, 45, 46, 120))
    working = frozenset({date(2026, 2, 14), date(2026, 5, 2)})  # Saturdays
    unbounded = BusinessCalendar(holidays=holidays, working_days=working, version="u")
    bounded = BusinessCalendar(
        holidays=holidays,
        working_days=working,
        version="b",
        valid_from=date(2026, 1, 1),
        valid_to=date(2026, 9, 30),
    )
    for calendar in (unbounded, bounded):
        for _ in range(2):  # the second round answers from the memo
            for offset in range(-3, 280):
                start = date(2026, 1, 1) + timedelta(days=offset)
                for count in (0, 1, 2, 3, 7, 21):
                    try:
                        expected: date | type[Exception] = _reference_add_business_days(
                            calendar, start, count
                        )
                    except CalendarRangeError:
                        expected = CalendarRangeError
                    if expected is CalendarRangeError:
                        with pytest.raises(CalendarRangeError) as raised:
                            calendar.add_business_days(start, count)
                        with pytest.raises(CalendarRangeError) as reference:
                            _reference_add_business_days(calendar, start, count)
                        assert str(raised.value) == str(reference.value)
                    else:
                        assert calendar.add_business_days(start, count) == expected
                if calendar.is_bounded and not calendar.covers(start):
                    continue
                assert calendar.is_business_day(start) == _reference_is_business_day(
                    calendar, start
                )


def test_add_business_days_keeps_its_errors_and_never_stores_a_failed_walk() -> None:
    calendar = BusinessCalendar(
        version="b", valid_from=date(2026, 1, 1), valid_to=date(2026, 1, 31)
    )
    with pytest.raises(ValueError, match="negative"):
        calendar.add_business_days(date(2026, 1, 5), -1)
    for _ in range(2):
        with pytest.raises(CalendarRangeError, match="2026-02-01"):
            calendar.add_business_days(date(2026, 1, 30), 2)
        with pytest.raises(CalendarRangeError, match="2025-12-31"):
            calendar.add_business_days(date(2025, 12, 31), 1)
    assert calendar.add_business_days(date(2026, 1, 28), 2) == date(2026, 1, 30)
    # Equal calendars are separate objects; a change of calendar is a new object.
    other = replace(calendar, holidays=frozenset({date(2026, 1, 29)}))
    assert other.add_business_days(date(2026, 1, 28), 1) == date(2026, 1, 30)
    assert calendar.add_business_days(date(2026, 1, 28), 1) == date(2026, 1, 29)
