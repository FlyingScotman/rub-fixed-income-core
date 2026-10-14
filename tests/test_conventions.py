from __future__ import annotations

from datetime import date

import pytest

from rub_fixed_income import (
    ANNUAL,
    CONTINUOUS,
    MONTHLY,
    BusinessCalendar,
    BusinessDayConvention,
    Compounding,
    CurveBootstrapRequest,
    CurveConvention,
    DayCount,
    DiscountCurve,
    OisQuote,
    ScheduleConvention,
    SettlementConvention,
    YieldConvention,
    ZSpreadConvention,
    adjust_date,
    bootstrap_discount_curve,
    generate_schedule,
)
from rub_fixed_income.dates import annual_schedule, year_fraction


def test_day_counts_match_closed_form_values() -> None:
    start, end = date(2027, 7, 1), date(2028, 7, 1)
    assert DayCount.ACT_365_FIXED.year_fraction(start, end) == 366 / 365.0
    assert DayCount.ACT_360.year_fraction(start, end) == 366 / 360.0
    # 184 days in 2027 (non-leap) + 182 days in 2028 (leap)
    assert DayCount.ACT_ACT_ISDA.year_fraction(start, end) == pytest.approx(
        184 / 365 + 182 / 366, abs=1e-15
    )
    assert DayCount.ACT_365_FIXED.year_fraction(start, end) == year_fraction(start, end)
    with pytest.raises(ValueError, match="end must not precede start"):
        DayCount.ACT_360.year_fraction(end, start)


def test_compounding_constants_and_validation() -> None:
    assert ANNUAL == Compounding(1)
    assert MONTHLY == Compounding(12)
    assert CONTINUOUS.is_continuous and not ANNUAL.is_continuous
    assert ANNUAL.identifier == "annual" and MONTHLY.identifier == "m12"
    assert CONTINUOUS.identifier == "continuous"
    with pytest.raises(ValueError, match="periods_per_year"):
        Compounding(-1)
    assert CONTINUOUS.discount_factor(0.1, 2.0) == pytest.approx(0.8187307530779818)
    assert ANNUAL.discount_factor(0.1, 2.0) == pytest.approx(1 / 1.21)
    assert ANNUAL.rate_from_discount_factor(1 / 1.21, 2.0) == pytest.approx(0.1)


def test_business_day_conventions_adjust_against_explicit_calendar() -> None:
    calendar = BusinessCalendar(holidays=frozenset({date(2026, 11, 2)}), version="t-v1")
    saturday = date(2026, 10, 31)
    assert adjust_date(saturday, BusinessDayConvention.UNADJUSTED, calendar) == saturday
    assert adjust_date(saturday, BusinessDayConvention.FOLLOWING, calendar) == date(2026, 11, 3)
    assert adjust_date(saturday, BusinessDayConvention.PRECEDING, calendar) == date(2026, 10, 30)
    # following crosses month end, so modified following goes back
    assert adjust_date(saturday, BusinessDayConvention.MODIFIED_FOLLOWING, calendar) == date(
        2026, 10, 30
    )


def test_settlement_convention_uses_business_day_lag() -> None:
    calendar = BusinessCalendar(holidays=frozenset({date(2026, 10, 19)}), version="t-v1")
    convention = SettlementConvention(lag_business_days=1, calendar=calendar)
    assert convention.settlement_date(date(2026, 10, 16)) == date(2026, 10, 20)
    assert SettlementConvention(0).settlement_date(date(2026, 10, 16)) == date(2026, 10, 16)
    with pytest.raises(ValueError, match="lag"):
        SettlementConvention(-1)


def test_legacy_annual_schedule_keeps_stub_but_generated_schedule_absorbs_it() -> None:
    start, adjusted_maturity = date(2026, 10, 15), date(2028, 10, 16)
    assert annual_schedule(start, adjusted_maturity) == (
        date(2027, 10, 15),
        date(2028, 10, 15),
        date(2028, 10, 16),
    )
    assert generate_schedule(start, adjusted_maturity, ScheduleConvention()) == (
        date(2027, 10, 15),
        date(2028, 10, 16),
    )
    unadjusted = ScheduleConvention(business_day_convention=BusinessDayConvention.UNADJUSTED)
    assert generate_schedule(start, adjusted_maturity, unadjusted) == annual_schedule(
        start, adjusted_maturity
    )


def test_generated_schedule_adjusts_roll_dates_and_supports_other_frequencies() -> None:
    convention = ScheduleConvention(frequency_months=6)
    # 2027-04-17 is a Saturday -> following Monday
    assert generate_schedule(date(2026, 10, 17), date(2027, 10, 18), convention) == (
        date(2027, 4, 19),
        date(2027, 10, 18),
    )
    with pytest.raises(ValueError, match="frequency"):
        ScheduleConvention(frequency_months=0)


def test_backward_roll_puts_short_stub_at_the_front() -> None:
    convention = ScheduleConvention(
        frequency_months=6,
        roll="backward",
        business_day_convention=BusinessDayConvention.UNADJUSTED,
    )
    assert generate_schedule(date(2026, 10, 15), date(2027, 12, 2), convention) == (
        date(2026, 12, 2),
        date(2027, 6, 2),
        date(2027, 12, 2),
    )


def test_conventions_are_hashable_and_have_stable_identifiers() -> None:
    conventions = {
        YieldConvention(),
        ZSpreadConvention(),
        ZSpreadConvention(compounding=MONTHLY),
        CurveConvention(),
        ScheduleConvention(),
    }
    assert len(conventions) == 5
    assert ZSpreadConvention().identifier == "zspread:annual:ACT/365F"
    assert ZSpreadConvention(compounding=CONTINUOUS).identifier == "zspread:continuous:ACT/365F"
    assert YieldConvention().identifier == "yield:annual:ACT/365F"
    assert CurveConvention().identifier == (
        "curve:time=ACT/365F:interp=log-linear-df:extrap=flat-forward:"
        "fixed=legacy-annual-forward:ACT/365F"
    )
    schedule_id = ScheduleConvention().identifier
    assert schedule_id.startswith("schedule:12M:forward:MODIFIED_FOLLOWING:weekends-only@")
    assert ScheduleConvention().to_dict()["calendar_version"] == "weekends-only"


def test_calendar_identifier_changes_with_holidays_even_with_same_version() -> None:
    first = ScheduleConvention(calendar=BusinessCalendar(version="v"))
    second = ScheduleConvention(
        calendar=BusinessCalendar(holidays=frozenset({date(2026, 1, 1)}), version="v")
    )
    assert first.identifier != second.identifier


def test_yield_convention_rejects_continuous_compounding() -> None:
    with pytest.raises(ValueError, match="continuous"):
        YieldConvention(compounding=CONTINUOUS)


def test_default_curve_convention_reproduces_legacy_bootstrap_bitwise() -> None:
    curve_date = date(2026, 10, 15)
    quotes = (
        OisQuote("1Y", date(2027, 10, 15), 0.15),
        OisQuote("2Y", date(2028, 10, 16), 0.14),
        OisQuote("3Y", date(2029, 10, 15), 0.135),
    )
    legacy = bootstrap_discount_curve(CurveBootstrapRequest(curve_date, quotes))
    explicit = bootstrap_discount_curve(
        CurveBootstrapRequest(curve_date, quotes, convention=CurveConvention())
    )
    assert legacy.nodes == explicit.nodes
    assert legacy.convention == CurveConvention()


def test_business_day_fixed_leg_bootstrap_reprices_without_stub() -> None:
    curve_date = date(2026, 10, 15)
    quotes = (
        OisQuote("1Y", date(2027, 10, 15), 0.15),
        OisQuote("2Y", date(2028, 10, 16), 0.14),
    )
    convention = CurveConvention(fixed_leg_schedule=ScheduleConvention())
    curve = bootstrap_discount_curve(CurveBootstrapRequest(curve_date, quotes, convention))
    legacy = bootstrap_discount_curve(CurveBootstrapRequest(curve_date, quotes))
    assert curve.max_repricing_error_bp(quotes) < 1e-7
    assert curve.convention is convention
    # different fixed-leg schedule => different, but close, 2Y discount factor
    assert curve.nodes[1][1] != legacy.nodes[1][1]
    assert curve.nodes[1][1] == pytest.approx(legacy.nodes[1][1], abs=5e-4)
    # explicit two-period par-rate check on the stub-free schedule
    one, two = date(2027, 10, 15), date(2028, 10, 16)
    annuity = year_fraction(curve_date, one) * curve.discount_factor(one) + year_fraction(
        one, two
    ) * curve.discount_factor(two)
    assert (1 - curve.discount_factor(two)) / annuity == pytest.approx(0.14, abs=1e-12)


def test_curve_time_day_count_changes_zero_rate_only_when_requested() -> None:
    curve_date = date(2026, 1, 1)
    nodes = ((date(2027, 1, 1), 0.9), (date(2028, 1, 1), 0.8))
    legacy = DiscountCurve(curve_date, nodes)
    act360 = DiscountCurve(
        curve_date, nodes, convention=CurveConvention(time_day_count=DayCount.ACT_360)
    )
    target = date(2027, 7, 1)
    assert legacy.discount_factor(target) == act360.discount_factor(target)
    assert legacy.zero_rate(target) != act360.zero_rate(target)
    t360 = (target - curve_date).days / 360.0
    assert act360.zero_rate(target) == pytest.approx(
        act360.discount_factor(target) ** (-1 / t360) - 1, abs=1e-15
    )
