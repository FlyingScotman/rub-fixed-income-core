from __future__ import annotations

from datetime import date

from rub_fixed_income import BusinessCalendar


def test_business_calendar_applies_working_weekend_and_holiday_overrides() -> None:
    calendar = BusinessCalendar(
        holidays=frozenset({date(2026, 8, 10)}),
        working_days=frozenset({date(2026, 8, 8)}),
        version="test-calendar-v1",
    )

    assert calendar.add_business_days(date(2026, 8, 7), 1) == date(2026, 8, 8)
    assert calendar.add_business_days(date(2026, 8, 8), 1) == date(2026, 8, 11)
