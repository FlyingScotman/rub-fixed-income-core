from __future__ import annotations

from datetime import date

import pytest

from rub_fixed_income import BusinessCalendar, DiscountCurve
from rub_fixed_income.dates import add_months
from rub_fixed_income.key_rate import (
    KeyRateCurveBuildRequest,
    KeyRateForwardCurve,
    KeyRateForwardSegment,
    KeyRateSwapPricingRequest,
    KeyRateSwapQuote,
    build_key_rate_forward_curve,
    price_key_rate_swap,
)


def _maturity(effective: date, tenor: str, calendar: BusinessCalendar) -> date:
    amount = int(tenor[:-1])
    unit = tenor[-1]
    if unit == "M":
        unadjusted = add_months(effective, amount)
    elif unit == "Y":
        unadjusted = add_months(effective, amount * 12)
    else:
        raise AssertionError(f"unsupported test tenor: {tenor}")
    return calendar.modified_following(unadjusted)


def test_three_month_constant_key_rate_prices_at_the_same_rate() -> None:
    trade_date = date(2026, 1, 5)
    calendar = BusinessCalendar(version="weekends-only-test")
    effective = calendar.add_business_days(trade_date, 1)
    maturity = _maturity(effective, "3M", calendar)
    curve = KeyRateForwardCurve(
        curve_date=effective,
        current_rate=0.14,
        current_rate_end=maturity,
        segments=(),
    )

    result = price_key_rate_swap(
        KeyRateSwapPricingRequest(
            trade_date=trade_date,
            tenor="3M",
            projection_curve=curve,
            discount_curve=DiscountCurve.flat(effective, 0.12),
            calendar=calendar,
        )
    )

    assert result.effective_date == effective
    assert result.maturity_date == maturity
    assert result.par_rate == pytest.approx(0.14, abs=1e-14)
    assert len(result.fixed_periods) == 1
    assert len(result.floating_periods) == 1


def test_krs_schedule_uses_tom_modified_following_and_act_act_isda() -> None:
    trade_date = date(2027, 8, 27)
    calendar = BusinessCalendar(
        holidays=frozenset({date(2028, 8, 30)}),
        version="explicit-holiday-test-v1",
    )
    effective = date(2027, 8, 30)
    expected_maturity = date(2028, 8, 31)
    curve = KeyRateForwardCurve(
        curve_date=effective,
        current_rate=0.10,
        current_rate_end=expected_maturity,
        segments=(),
    )

    result = price_key_rate_swap(
        KeyRateSwapPricingRequest(
            trade_date=trade_date,
            tenor="1Y",
            projection_curve=curve,
            discount_curve=DiscountCurve.flat(effective, 0.08),
            calendar=calendar,
        )
    )

    assert result.effective_date == effective
    assert result.maturity_date == expected_maturity
    assert result.fixed_periods[0].accrual_factor == pytest.approx(
        (date(2028, 1, 1) - effective).days / 365
        + (expected_maturity - date(2028, 1, 1)).days / 366,
        abs=1e-14,
    )
    assert [period.payment_date for period in result.floating_periods] == [
        date(2027, 11, 30),
        date(2028, 2, 29),
        date(2028, 5, 30),
        expected_maturity,
    ]


def test_pillar_forward_builder_recovers_identified_curve_and_all_quote_sides() -> None:
    trade_date = date(2026, 1, 5)
    calendar = BusinessCalendar(version="weekends-only-test")
    effective = calendar.add_business_days(trade_date, 1)
    next_policy = date(2026, 2, 16)
    tenors = ("3M", "6M", "9M", "1Y", "2Y")
    maturities = tuple(_maturity(effective, tenor, calendar) for tenor in tenors)
    true_rates = (0.13, 0.125, 0.12, 0.115, 0.11)
    true_curve = KeyRateForwardCurve(
        curve_date=effective,
        current_rate=0.14,
        current_rate_end=next_policy,
        segments=tuple(
            KeyRateForwardSegment(
                start_date=start,
                end_date=end,
                rate=rate,
                pillar_tenor=tenor,
            )
            for start, end, rate, tenor in zip(
                (next_policy, *maturities[:-1]),
                maturities,
                true_rates,
                tenors,
                strict=True,
            )
        ),
    )
    discount_curve = DiscountCurve.flat(effective, 0.10)
    mids = tuple(
        price_key_rate_swap(
            KeyRateSwapPricingRequest(
                trade_date=trade_date,
                tenor=tenor,
                projection_curve=true_curve,
                discount_curve=discount_curve,
                calendar=calendar,
            )
        ).par_rate
        for tenor in tenors
    )
    quotes = tuple(
        KeyRateSwapQuote(tenor, mid - 0.0005, mid, mid + 0.0005)
        for tenor, mid in zip(tenors, mids, strict=True)
    )

    result = build_key_rate_forward_curve(
        KeyRateCurveBuildRequest(
            trade_date=trade_date,
            current_rate=0.14,
            next_policy_effective_date=next_policy,
            quotes=quotes,
            discount_curve=discount_curve,
            calendar=calendar,
        )
    )

    assert result.usable is True
    assert result.representation == "pillar_forward_average"
    assert result.diagnostics.equation_count == 5
    assert result.diagnostics.variable_count == 5
    assert result.diagnostics.rank == 5
    assert result.diagnostics.nullity == 0
    assert result.mid.max_repricing_error_bp < 1e-7
    assert [segment.rate for segment in result.mid.curve.segments] == pytest.approx(
        true_rates,
        abs=1e-11,
    )
    assert result.bid.side == "all_bid_quotes"
    assert result.ask.side == "all_ask_quotes"
    assert result.bid.max_repricing_error_bp < 1e-7
    assert result.ask.max_repricing_error_bp < 1e-7


def test_builder_rejects_duplicate_economic_maturity() -> None:
    trade_date = date(2026, 1, 5)
    calendar = BusinessCalendar(version="weekends-only-test")
    effective = calendar.add_business_days(trade_date, 1)

    with pytest.raises(ValueError, match="maturities must be unique"):
        build_key_rate_forward_curve(
            KeyRateCurveBuildRequest(
                trade_date=trade_date,
                current_rate=0.14,
                next_policy_effective_date=date(2026, 2, 16),
                quotes=(
                    KeyRateSwapQuote("12M", 0.10, 0.11, 0.12),
                    KeyRateSwapQuote("1Y", 0.20, 0.21, 0.22),
                ),
                discount_curve=DiscountCurve.flat(effective, 0.10),
                calendar=calendar,
            )
        )


def test_builder_fails_when_current_rate_covers_first_quote_maturity() -> None:
    trade_date = date(2026, 1, 5)
    calendar = BusinessCalendar(version="weekends-only-test")
    effective = calendar.add_business_days(trade_date, 1)

    with pytest.raises(ValueError, match="before the first KRS maturity"):
        build_key_rate_forward_curve(
            KeyRateCurveBuildRequest(
                trade_date=trade_date,
                current_rate=0.14,
                next_policy_effective_date=date(2026, 5, 1),
                quotes=(KeyRateSwapQuote("3M", 0.10, 0.11, 0.12),),
                discount_curve=DiscountCurve.flat(effective, 0.10),
                calendar=calendar,
            )
        )
