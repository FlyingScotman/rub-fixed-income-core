"""Covered-range calendars (roadmap 0.1).

The calendar below is synthetic test data in the shape the hub publishes (holidays on
weekdays, transferred working Saturdays). It is not an official holiday list.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date

import pytest

from rub_fixed_income import (
    BusinessCalendar,
    BusinessDayConvention,
    CalendarRangeError,
    ScheduleConvention,
    SettlementConvention,
    adjust_date,
    calendar_fingerprint,
    calendar_identity,
    generate_schedule,
)

SYNTHETIC = BusinessCalendar(
    holidays=frozenset({date(2026, 6, 12), date(2026, 11, 4), date(2026, 12, 31)}),
    working_days=frozenset({date(2026, 11, 7)}),  # a Saturday made a working day
    version="synthetic-moex-2026",
    valid_from=date(2026, 1, 1),
    valid_to=date(2026, 12, 31),
)


def test_settlement_rolls_over_a_holiday_and_onto_a_working_saturday() -> None:
    t_plus_1 = SettlementConvention(1, SYNTHETIC)
    assert t_plus_1.settlement_date(date(2026, 6, 11)) == date(2026, 6, 15)  # Thu -> Mon
    assert t_plus_1.settlement_date(date(2026, 11, 6)) == date(2026, 11, 7)  # Fri -> Sat
    assert t_plus_1.settlement_date(date(2026, 11, 3)) == date(2026, 11, 5)  # over Nov 4


def test_adjustment_and_schedule_use_the_calendar() -> None:
    assert adjust_date(date(2026, 6, 12), BusinessDayConvention.FOLLOWING, SYNTHETIC) == date(
        2026, 6, 15
    )
    assert adjust_date(date(2026, 6, 12), BusinessDayConvention.PRECEDING, SYNTHETIC) == date(
        2026, 6, 11
    )
    schedule = generate_schedule(
        date(2026, 3, 12),
        date(2026, 12, 30),
        ScheduleConvention(frequency_months=6, calendar=SYNTHETIC),
    )
    # The roll lands on Saturday 12 September and moves to Monday; the end date is as given.
    assert schedule == (date(2026, 9, 14), date(2026, 12, 30))


def test_dates_outside_the_covered_range_are_rejected_not_assumed_weekdays() -> None:
    with pytest.raises(CalendarRangeError, match="outside calendar synthetic-moex-2026"):
        SYNTHETIC.is_business_day(date(2027, 1, 4))
    with pytest.raises(CalendarRangeError):
        SYNTHETIC.add_business_days(date(2026, 12, 30), 1)  # Dec 31 holiday, then 2027
    with pytest.raises(CalendarRangeError):
        SettlementConvention(1, SYNTHETIC).settlement_date(date(2025, 12, 30))
    assert issubclass(CalendarRangeError, ValueError)
    assert CalendarRangeError.code == "CALENDAR_OUT_OF_RANGE"


def test_unbounded_calendar_keeps_legacy_behaviour_and_fingerprint() -> None:
    legacy = BusinessCalendar()
    assert not legacy.is_bounded
    assert legacy.is_business_day(date(2099, 1, 5))
    # Pre-0.9.0 fingerprint of an empty calendar: sha256("|")[:12].
    assert calendar_fingerprint(legacy) == "cbe5cfdf7c21"
    unbounded = replace(SYNTHETIC, valid_from=None, valid_to=None)
    assert calendar_fingerprint(unbounded) != calendar_fingerprint(SYNTHETIC)
    assert calendar_identity(SYNTHETIC) == ("synthetic-moex-2026", calendar_fingerprint(SYNTHETIC))


def test_calendar_validation() -> None:
    with pytest.raises(ValueError, match="together"):
        BusinessCalendar(valid_from=date(2026, 1, 1))
    with pytest.raises(ValueError, match="precede"):
        BusinessCalendar(valid_from=date(2026, 1, 2), valid_to=date(2026, 1, 1))
    with pytest.raises(ValueError, match="inside the covered range"):
        BusinessCalendar(
            holidays=frozenset({date(2027, 1, 1)}),
            valid_from=date(2026, 1, 1),
            valid_to=date(2026, 12, 31),
        )
    with pytest.raises(ValueError, match="both a holiday"):
        BusinessCalendar(
            holidays=frozenset({date(2026, 1, 3)}), working_days=frozenset({date(2026, 1, 3)})
        )
    with pytest.raises(ValueError, match="non-empty"):
        BusinessCalendar(version="")
