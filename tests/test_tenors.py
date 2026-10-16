"""Tenor codes resolved with a business calendar (roadmap 0.5)."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from rub_fixed_income import (
    BusinessCalendar,
    BusinessDayConvention,
    CalendarRangeError,
    CurveBootstrapRequest,
    InvalidTerms,
    Tenor,
    add_tenor,
    bootstrap_discount_curve,
    ois_quotes,
    parse_tenor,
    resolve_tenor,
)
from rub_fixed_income.dates import add_months

# Synthetic test data, not an official calendar: a Wednesday holiday and a working Saturday.
CAL = BusinessCalendar(
    holidays=frozenset({date(2026, 11, 4), date(2026, 12, 31)}),
    working_days=frozenset({date(2026, 11, 7)}),
    version="synthetic-tenor-calendar",
    valid_from=date(2026, 1, 1),
    valid_to=date(2037, 12, 31),
)


def test_parse_tenor_codes() -> None:
    assert parse_tenor(" 3m ") == Tenor(3, "M")
    assert parse_tenor("10Y").code == "10Y"
    assert parse_tenor("on") == Tenor(1, "ON")
    for bad in ("", "0M", "M", "3Q", "1.5Y", "-1W", "ON1"):
        with pytest.raises(InvalidTerms):
            parse_tenor(bad)
    with pytest.raises(InvalidTerms):
        parse_tenor(3)  # type: ignore[arg-type]
    assert add_tenor(date(2026, 1, 31), "1M") == date(2026, 2, 28)
    assert add_tenor(date(2026, 1, 1), "2W") == date(2026, 1, 15)
    with pytest.raises(InvalidTerms):
        add_tenor(date(2026, 1, 1), "ON")


def test_tom_start_and_modified_following_over_a_holiday_and_a_working_saturday() -> None:
    # Tuesday 3 Nov: TOM would be the Wednesday holiday, so spot is Thursday 5 Nov.
    one_week = resolve_tenor(date(2026, 11, 3), "1W", CAL)
    assert one_week.effective_date == date(2026, 11, 5)
    assert one_week.end_date == date(2026, 11, 12)
    # Friday 6 Nov: TOM is the working Saturday 7 Nov.
    saturday = resolve_tenor(date(2026, 11, 6), "1M", CAL)
    assert saturday.effective_date == date(2026, 11, 7)
    assert saturday.unadjusted_end == date(2026, 12, 7)
    month_end = resolve_tenor(date(2026, 11, 27), "1M", CAL)
    assert month_end.effective_date == date(2026, 11, 30)
    assert month_end.unadjusted_end == date(2026, 12, 30)
    # 31 Dec is a holiday and 1 Jan is in the next month: modified following steps back.
    year_end = resolve_tenor(date(2026, 12, 23), "1W", CAL)
    assert year_end.unadjusted_end == date(2026, 12, 31)
    assert year_end.end_date == date(2026, 12, 30)
    unadjusted = resolve_tenor(
        date(2026, 11, 3),
        "1W",
        CAL,
        spot_lag_days=0,
        business_day_convention=BusinessDayConvention.UNADJUSTED,
    )
    assert unadjusted.end_date == date(2026, 11, 10)


def test_overnight_codes() -> None:
    trade = date(2026, 11, 3)  # Tuesday before the Wednesday holiday
    on = resolve_tenor(trade, "ON", CAL)
    tn = resolve_tenor(trade, "TN", CAL)
    sn = resolve_tenor(trade, "SN", CAL, spot_lag_days=2)
    assert (on.effective_date, on.end_date) == (trade, date(2026, 11, 5))
    assert (tn.effective_date, tn.end_date) == (date(2026, 11, 5), date(2026, 11, 6))
    assert (sn.effective_date, sn.end_date) == (date(2026, 11, 6), date(2026, 11, 7))


def test_bounded_calendar_and_lag_validation() -> None:
    with pytest.raises(CalendarRangeError):
        resolve_tenor(date(2037, 6, 1), "1Y", CAL)
    with pytest.raises(InvalidTerms):
        resolve_tenor(date(2026, 11, 3), "1W", CAL, spot_lag_days=-1)
    with pytest.raises(InvalidTerms):
        resolve_tenor(date(2026, 11, 3), "1W", CAL, spot_lag_days=True)


def test_ois_quotes_from_the_ratescalcs_tenor_set_bootstrap_and_reprice() -> None:
    tenors = ["1W", "2W", "1M", "2M", "3M", "6M", "9M", "1Y"] + [f"{y}Y" for y in range(2, 11)]
    trade = date(2026, 10, 16)
    rates = [(t, 0.137 + 0.001 * i) for i, t in enumerate(tenors)]
    quotes = ois_quotes(trade, reversed(rates), CAL)
    assert [q.tenor for q in quotes] == tenors
    assert quotes[0].maturity == date(2026, 10, 26)  # spot Mon 19 Oct + 1W
    curve = bootstrap_discount_curve(CurveBootstrapRequest(trade, quotes))
    assert curve.max_repricing_error_bp(quotes) < 1e-7
    with pytest.raises(InvalidTerms):
        ois_quotes(trade, [("1Y", 0.1), ("1y", 0.1)], CAL)
    with pytest.raises(InvalidTerms):
        ois_quotes(trade, [("7D", 0.1), ("1W", 0.1)], CAL)


def _legacy_krs_dates(trade: date, tenor: str, cal: BusinessCalendar) -> tuple[date, date, date]:
    effective = cal.add_business_days(trade, 1)
    amount, unit = int(tenor[:-1]), tenor[-1]
    if unit == "D":
        raw = effective + timedelta(days=amount)
    elif unit == "W":
        raw = effective + timedelta(weeks=amount)
    else:
        raw = add_months(effective, amount * (12 if unit == "Y" else 1))
    return effective, raw, cal.modified_following(raw)


def test_key_rate_swap_dates_are_unchanged() -> None:
    from rub_fixed_income.key_rate import _swap_dates

    start = date(2026, 10, 1)
    for offset in range(0, 120, 3):
        trade = start + timedelta(days=offset)
        for tenor in ("1W", "2W", "1M", "3M", "6M", "9M", "1Y", "2Y", "5Y", "10Y", "45D"):
            assert _swap_dates(trade, tenor, CAL) == _legacy_krs_dates(trade, tenor, CAL)
    with pytest.raises(ValueError, match="unsupported tenor"):
        _swap_dates(start, "ON", CAL)
