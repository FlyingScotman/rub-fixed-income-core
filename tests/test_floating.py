from __future__ import annotations

import math
from dataclasses import replace
from datetime import date, timedelta

import pytest

from rub_fixed_income import (
    ANNUAL,
    BondCashflow,
    BondRiskEngine,
    BondRiskRequest,
    BondScenario,
    BusinessCalendar,
    CurveBootstrapRequest,
    DayCount,
    DiscountCurve,
    FixingHistory,
    FloatingCouponPeriod,
    FloatingCouponProjection,
    FloatingCouponRule,
    FloatingLeg,
    OisQuote,
    ZSpreadConvention,
)
from rub_fixed_income.risk import FLOATING_RISK_MODEL_VERSION, RISK_MODEL_VERSION

VALUATION = date(2026, 8, 5)  # Wednesday
HOLIDAY = date(2026, 7, 15)  # synthetic CBR holiday
CALENDAR = BusinessCalendar(holidays=frozenset({HOLIDAY}), version="test-cbr")
CURVE = DiscountCurve.flat(VALUATION, 0.14)
ACT365 = DayCount.ACT_365_FIXED


def _business_days(start: date, end: date) -> list[date]:
    days = []
    day = start
    while day < end:
        if CALENDAR.is_business_day(day):
            days.append(day)
        day += timedelta(days=1)
    return days


def _history(
    *,
    index: str = "ruonia",
    end: date = VALUATION,
    rate: float = 0.15,
    skip: frozenset[date] = frozenset(),
    max_unpublished: int = 0,
) -> FixingHistory:
    rows = tuple((d, rate) for d in _business_days(date(2026, 5, 1), end) if d not in skip)
    return FixingHistory(
        index,  # type: ignore[arg-type]
        VALUATION,
        rows,
        CALENDAR,
        "snapshot-1",
        max_unpublished_business_days=max_unpublished,
    )


def _rule(**changes: object) -> FloatingCouponRule:
    rule = FloatingCouponRule("ruonia", "compounded", 0.0, ACT365, ACT365)
    return replace(rule, **changes)  # type: ignore[arg-type]


def _period(start: date, end: date, **changes: object) -> FloatingCouponPeriod:
    return replace(FloatingCouponPeriod(start, end, end, 1000.0), **changes)  # type: ignore[arg-type]


def _one(leg: FloatingLeg, curve: DiscountCurve | None = CURVE) -> FloatingCouponProjection:
    (coupon,) = leg.project(curve, settlement_date=VALUATION - timedelta(days=60))
    return coupon


def _realized_factor(start: date, end: date, rate: float) -> float:
    """Business-day compounding: each fixing applies until the next business day."""
    factor = 1.0
    for day in _business_days(start, end):
        following = CALENDAR.add_business_days(day, 1)
        factor *= 1.0 + rate * (min(following, end) - day).days / 365.0
    return factor


def test_projected_compounded_coupon_equals_the_discount_factor_ratio() -> None:
    start, end = date(2026, 8, 10), date(2026, 11, 10)
    coupon = _one(FloatingLeg(_rule(), (_period(start, end),), _history()))
    expected = 1000.0 * (CURVE.discount_factor(start) / CURVE.discount_factor(end) - 1.0)
    assert coupon.source == "projected"
    assert coupon.amount_rub == pytest.approx(expected, rel=1e-12)
    assert (coupon.realized_days, coupon.projected_days) == (0, 92)


def test_fully_fixed_coupon_compounds_over_weekends_and_holidays_and_rounds() -> None:
    start, end = date(2026, 7, 6), date(2026, 8, 3)
    rule = _rule(margin=0.0125, rate_rounding_decimals=2, amount_rounding_decimals=2)
    coupon = _one(FloatingLeg(rule, (_period(start, end),), _history()), curve=None)
    accrual = (end - start).days / 365.0
    raw_rate = (_realized_factor(start, end, 0.15) - 1.0) / accrual + 0.0125
    rate = round(raw_rate * 100.0, 2) / 100.0
    assert coupon.source == "fixed_by_history"
    assert coupon.index_rate == pytest.approx(raw_rate - 0.0125, rel=1e-12)
    assert coupon.coupon_rate == pytest.approx(rate, abs=1e-15)
    assert coupon.amount_rub == round(1000.0 * rate * accrual, 2)
    assert (coupon.realized_days, coupon.projected_days) == (28, 0)
    # The holiday carries the previous fixing for two days instead of one.
    assert _realized_factor(start, end, 0.15) != pytest.approx((1 + 0.15 / 365) ** 28, rel=1e-12)


def test_partial_period_freezes_realized_days_and_projects_the_rest() -> None:
    start, end = date(2026, 7, 20), date(2026, 8, 20)
    leg = FloatingLeg(_rule(margin=0.01), (_period(start, end),), _history())
    base = _one(leg)
    realized = _realized_factor(start, VALUATION, 0.15)
    projected = CURVE.discount_factor(VALUATION) / CURVE.discount_factor(end)
    accrual = (end - start).days / 365.0
    assert base.source == "partially_projected"
    assert (base.realized_days, base.projected_days) == (16, 15)
    assert base.amount_rub == pytest.approx(
        1000.0 * (realized * projected - 1.0 + 0.01 * accrual), rel=1e-12
    )
    # Rounding never applies to a coupon that still depends on the curve.
    rounded = _one(replace(leg, rule=_rule(margin=0.01, amount_rounding_decimals=0)))
    assert rounded.amount_rub == base.amount_rub
    shocked = _one(leg, DiscountCurve.flat(VALUATION, 0.20))
    shocked_projected = math.exp(-0.20 * 15 / 365)
    assert shocked.amount_rub == pytest.approx(
        1000.0 * (realized / shocked_projected - 1.0 + 0.01 * accrual), rel=1e-12
    )


def test_arithmetic_average_sums_rates_and_coupon_day_count_scales_the_margin() -> None:
    start, end = date(2026, 7, 6), date(2026, 8, 3)
    rule = _rule(averaging="arithmetic", margin=0.02, coupon_day_count=DayCount.ACT_360)
    coupon = _one(FloatingLeg(rule, (_period(start, end),), _history()), curve=None)
    index_return = 0.15 * 28 / 365.0
    assert coupon.amount_rub == pytest.approx(1000.0 * (index_return + 0.02 * 28 / 360.0))
    assert coupon.index_rate == pytest.approx(index_return / (28 / 360.0))


def test_lookback_uses_published_fixings_for_the_first_projected_days() -> None:
    start, end = VALUATION, date(2026, 8, 12)
    history = _history(rate=0.15)
    plain = _one(FloatingLeg(_rule(), (_period(start, end),), history))
    lagged = _one(FloatingLeg(_rule(lookback_business_days=2), (_period(start, end),), history))
    assert (plain.realized_days, plain.projected_days) == (0, 7)
    # 5 and 6 August observe 3 and 4 August, both published.
    assert (lagged.realized_days, lagged.projected_days) == (2, 5)
    assert lagged.source == "partially_projected"
    assert lagged.amount_rub > plain.amount_rub  # 15 % fixings above the 14 % curve


def test_published_coupons_are_cash_and_zero_placeholders_are_rejected() -> None:
    start, end = date(2026, 8, 10), date(2026, 11, 10)
    leg = FloatingLeg(_rule(), (_period(start, end, published_coupon_rub=37.4),), _history())
    coupon = _one(leg, curve=None)
    assert (coupon.source, coupon.amount_rub, coupon.coupon_rate) == ("published", 37.4, None)
    with pytest.raises(ValueError, match="zero published coupon is ambiguous"):
        _period(start, end, published_coupon_rub=0.0)


def test_missing_ruonia_history_fails_and_trailing_days_carry_within_the_limit() -> None:
    start, end = date(2026, 7, 27), date(2026, 8, 10)
    gap = FloatingLeg(
        _rule(), (_period(start, end),), _history(skip=frozenset({date(2026, 7, 29)}))
    )
    with pytest.raises(LookupError, match="missing RUONIA fixing on business day 2026-07-29"):
        _one(gap)
    trailing = _history(end=date(2026, 8, 4), max_unpublished=0)
    assert trailing.unpublished_business_days == (date(2026, 8, 4),)
    with pytest.raises(LookupError, match="unpublished for 1 business days from 2026-08-04"):
        _one(FloatingLeg(_rule(), (_period(start, end),), trailing))
    carried = _one(
        FloatingLeg(
            _rule(), (_period(start, end),), _history(end=date(2026, 8, 4), max_unpublished=2)
        )
    )
    assert carried.carried_business_days == (date(2026, 8, 4),)
    with pytest.raises(LookupError, match="no RUONIA fixing on or before 2026-04-30"):
        _one(FloatingLeg(_rule(), (_period(date(2026, 4, 30), end),), _history()))


def test_key_rate_stays_in_force_until_changed() -> None:
    history = FixingHistory(
        "key_rate", VALUATION, ((date(2026, 6, 1), 0.18), (date(2026, 7, 27), 0.16)), CALENDAR, "kr"
    )
    rule = _rule(index="key_rate", averaging="arithmetic")
    coupon = _one(FloatingLeg(rule, (_period(date(2026, 7, 20), VALUATION),), history), None)
    expected = 0.18 * 7 / 365 + 0.16 * 9 / 365  # 20-26 July at 18 %, then 16 %
    assert coupon.amount_rub == pytest.approx(1000.0 * expected, rel=1e-12)
    assert coupon.carried_business_days == ()


@pytest.mark.parametrize(
    ("rows", "message"),
    [
        (((VALUATION, 0.15),), "must end before the valuation date"),
        (((date(2026, 8, 3), 0.1), (date(2026, 8, 3), 0.1)), "strictly increasing"),
        (((date(2026, 8, 1), 0.15),), "fixing on non-business day 2026-08-01"),
        (((date(2026, 8, 3), -1.0),), "exceed -100 %"),
    ],
)
def test_fixing_history_validation(rows: tuple[tuple[date, float], ...], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        FixingHistory("ruonia", VALUATION, rows, CALENDAR, "s")


def test_leg_validation_and_curve_requirements() -> None:
    first = _period(date(2026, 8, 10), date(2026, 11, 10))
    second = _period(date(2026, 11, 1), date(2027, 2, 10))
    with pytest.raises(ValueError, match="non-overlapping"):
        FloatingLeg(_rule(), (first, second), _history())
    with pytest.raises(ValueError, match="index differ"):
        FloatingLeg(_rule(index="key_rate"), (first,), _history())
    with pytest.raises(ValueError, match="payment cannot precede"):
        FloatingCouponPeriod(date(2026, 8, 10), date(2026, 11, 10), date(2026, 11, 9), 1000.0)
    leg = FloatingLeg(_rule(), (first,), _history())
    with pytest.raises(ValueError, match="projection curve is required"):
        _one(leg, curve=None)
    with pytest.raises(ValueError, match="projection curve date must equal"):
        _one(leg, DiscountCurve.flat(date(2026, 8, 4), 0.14))
    with pytest.raises(ValueError, match="lookback_business_days"):
        _rule(lookback_business_days=-1)
    with pytest.raises(ValueError, match="rate_rounding_decimals"):
        _rule(rate_rounding_decimals=True)


def test_periods_paid_before_settlement_need_no_history() -> None:
    old = _period(date(2026, 1, 10), date(2026, 4, 10))
    current = _period(date(2026, 7, 10), date(2026, 10, 10))
    leg = FloatingLeg(_rule(), (old, current), _history())
    (coupon,) = leg.project(CURVE, settlement_date=VALUATION)
    assert coupon.accrual_start == date(2026, 7, 10)


def _quarterly(start: date, years: int) -> tuple[FloatingCouponPeriod, ...]:
    """Business-day-adjusted quarterly periods, so projected coupons telescope exactly."""
    periods = []
    cursor = start
    for quarter in range(1, 4 * years + 1):
        month = start.month - 1 + 3 * quarter
        end = CALENDAR.modified_following(date(start.year + month // 12, month % 12 + 1, start.day))
        periods.append(FloatingCouponPeriod(cursor, end, end, 1000.0))
        cursor = end
    return tuple(periods)


def _floater_request(
    periods: tuple[FloatingCouponPeriod, ...],
    curve: DiscountCurve | CurveBootstrapRequest,
    *,
    dirty: float | None = None,
    spread: float | None = None,
) -> BondRiskRequest:
    return BondRiskRequest(
        cashflows=(BondCashflow(periods[-1].payment_date, 1000.0),),
        settlement_date=VALUATION,
        curve=curve,
        spread_convention=ZSpreadConvention(compounding=ANNUAL),
        spread_kind="credit",
        spread_bp=spread,
        dirty_price_rub=dirty,
        floating_leg=FloatingLeg(_rule(), periods, _history()),
    )


def test_a_reset_date_par_floater_has_no_rate_risk() -> None:
    periods = _quarterly(VALUATION, 3)
    engine = BondRiskEngine(_floater_request(periods, CURVE, dirty=1000.0))
    assert abs(engine.spread_bp) < 1e-6
    risk = engine.risk()
    assert risk.model_version == FLOATING_RISK_MODEL_VERSION
    assert max(abs(b.dv01_rub) for b in risk.buckets) < 1e-6
    assert abs(risk.parallel_dv01_rub) < 1e-6
    assert risk.cs01_rub < -0.2  # discount margin risk stays (about the 3y annuity)
    assert all(c.source == "projected" for c in engine.projected_coupons)
    assert engine.second_order().model_version == "rub-floating-coupon-second-order-v1"


def test_an_ois_quote_floater_reprices_its_coupons_off_each_shocked_curve() -> None:
    quotes = tuple(
        OisQuote(f"{y}Y", date(VALUATION.year + y, VALUATION.month, VALUATION.day), 0.14)
        for y in (1, 2, 3, 5)
    )
    request = CurveBootstrapRequest(VALUATION, quotes)
    periods = _quarterly(VALUATION, 3)
    engine = BondRiskEngine(_floater_request(periods, request, dirty=1000.0))
    risk = engine.risk()
    assert engine.curve_basis == "ois_quote"
    assert abs(risk.parallel_dv01_rub) < 1e-6
    scenario = engine.reprice(BondScenario.parallel(100.0))
    assert abs(scenario.rates_pnl_rub) < 1e-6


def test_known_fixings_and_published_coupons_are_frozen_under_bumps() -> None:
    periods = _quarterly(date(2026, 7, 6), 3)
    floating = BondRiskEngine(_floater_request(periods, CURVE, spread=50.0))
    first = floating.projected_coupons[0]
    assert first.source == "partially_projected"
    published = tuple(
        replace(p, published_coupon_rub=c.amount_rub)
        for p, c in zip(periods, floating.projected_coupons, strict=True)
    )
    frozen = BondRiskEngine(_floater_request(published, CURVE, spread=50.0))
    fixed = BondRiskEngine(
        BondRiskRequest(
            cashflows=tuple(
                BondCashflow(c.payment_date, c.amount_rub + (1000.0 if i == 11 else 0.0))
                for i, c in enumerate(floating.projected_coupons)
            ),
            settlement_date=VALUATION,
            curve=CURVE,
            spread_convention=ZSpreadConvention(compounding=ANNUAL),
            spread_kind="credit",
            spread_bp=50.0,
        )
    )
    assert frozen.base_price_rub == pytest.approx(floating.base_price_rub, rel=1e-13)
    assert frozen.risk().parallel_dv01_rub == pytest.approx(
        fixed.risk().parallel_dv01_rub, rel=1e-9
    )
    assert fixed.risk().model_version == RISK_MODEL_VERSION
    assert frozen.base_id != fixed.base_id
    assert abs(floating.risk().parallel_dv01_rub) < abs(fixed.risk().parallel_dv01_rub) / 10


def test_floating_history_must_be_dated_at_the_curve_date() -> None:
    periods = _quarterly(VALUATION, 1)
    with pytest.raises(ValueError, match="dated at the curve date"):
        _floater_request(periods, DiscountCurve.flat(date(2026, 8, 4), 0.14), spread=0.0)


def test_an_empty_history_projects_future_coupons_only() -> None:
    empty = FixingHistory("ruonia", VALUATION, (), CALENDAR, "empty")
    assert empty.latest_fixing_date is None
    assert empty.unpublished_business_days == ()
    future = _one(FloatingLeg(_rule(), (_period(date(2026, 8, 10), date(2026, 11, 10)),), empty))
    assert future.source == "projected"
    with pytest.raises(LookupError, match="no RUONIA fixing on or before 2026-08-04"):
        _one(FloatingLeg(_rule(), (_period(date(2026, 8, 4), date(2026, 11, 10)),), empty))
