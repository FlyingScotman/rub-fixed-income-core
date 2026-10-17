"""Floater terms beyond 0.8.0: floors and caps, observation shifts and windows, single
fixings, key-rate effective dates, point-in-time publication and the key-rate projection
curve. Every expected value is computed independently of ``FloatingLeg``."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest

from rub_fixed_income import (
    BusinessCalendar,
    CouponRateBounds,
    DayCount,
    DiscountCurve,
    FixingDateRule,
    FixingHistory,
    FloatingCouponPeriod,
    FloatingCouponProjection,
    FloatingCouponRule,
    FloatingLeg,
    KeyRateForwardCurve,
    KeyRateForwardSegment,
    MissingFixing,
    MissingMarketData,
    ObservationShift,
    ObservationWindow,
    UnsupportedProduct,
    canonical_sha256,
    from_dict,
    key_rate_projection_curve,
    to_dict,
)
from rub_fixed_income.floating import FLOATING_MODEL_VERSION

VALUATION = date(2026, 8, 5)  # Wednesday
CALENDAR = BusinessCalendar(holidays=frozenset({date(2026, 7, 15)}), version="test-cbr")
CURVE = DiscountCurve.flat(VALUATION, 0.14)
ACT365 = DayCount.ACT_365_FIXED
STAMP = datetime(2026, 8, 5, 9, 0, tzinfo=UTC)


def _business_days(start: date, end: date) -> list[date]:
    days, day = [], start
    while day < end:
        if CALENDAR.is_business_day(day):
            days.append(day)
        day += timedelta(days=1)
    return days


def _history(rate: float = 0.15, end: date = VALUATION, **kw: object) -> FixingHistory:
    rows = tuple((d, rate) for d in _business_days(date(2026, 4, 1), end))
    return FixingHistory("ruonia", VALUATION, rows, CALENDAR, "fix", **kw)  # type: ignore[arg-type]


def _rule(**changes: object) -> FloatingCouponRule:
    rule = FloatingCouponRule("ruonia", "arithmetic", 0.0, ACT365, ACT365)
    return replace(rule, **changes)  # type: ignore[arg-type]


def _one(
    rule: FloatingCouponRule,
    start: date,
    end: date,
    *,
    history: FixingHistory | None = None,
    curve: DiscountCurve | None = CURVE,
    guard: float | None = None,
) -> FloatingCouponProjection:
    period = FloatingCouponPeriod(start, end, end, 1000.0)
    leg = FloatingLeg(rule, (period,), history or _history(), strike_guard_bp=guard)
    (coupon,) = leg.project(curve, settlement_date=start - timedelta(days=1))
    return coupon


def _forward(day: date, following: date, curve: DiscountCurve = CURVE) -> float:
    tau = (following - day).days / 365.0
    return (curve.discount_factor(day) / curve.discount_factor(following) - 1.0) / tau


# --- floors and caps ---------------------------------------------------------------


def test_a_floor_on_the_coupon_rate_binds_a_fixed_coupon_exactly() -> None:
    start, end = date(2026, 6, 1), date(2026, 7, 1)  # 30 days at 15 % + 1 % margin
    bounds = CouponRateBounds("coupon_rate", floor=0.18)
    coupon = _one(_rule(margin=0.01, bounds=bounds), start, end, curve=None)
    assert coupon.source == "fixed_by_history" and coupon.bound == "floor"
    assert coupon.index_rate == pytest.approx(0.15, abs=1e-14)  # reported before the bound
    assert coupon.coupon_rate == 0.18
    assert coupon.amount_rub == pytest.approx(1000.0 * 0.18 * 30 / 365, rel=1e-14)
    unbound = _one(
        _rule(margin=0.01, bounds=CouponRateBounds("coupon_rate", floor=0.10)), start, end
    )
    assert unbound.bound is None
    assert unbound.amount_rub == pytest.approx(1000.0 * 0.16 * 30 / 365, rel=1e-13)


def test_an_index_cap_applies_before_the_multiplier_and_margin() -> None:
    start, end = date(2026, 6, 1), date(2026, 7, 1)
    rule = _rule(margin=0.02, multiplier=1.5, bounds=CouponRateBounds("index_rate", cap=0.12))
    coupon = _one(rule, start, end, curve=None)
    assert coupon.bound == "cap"
    assert coupon.coupon_rate == pytest.approx(1.5 * 0.12 + 0.02, abs=1e-15)
    # The same cap on the all-in rate binds at the cap itself.
    all_in = _one(replace(rule, bounds=CouponRateBounds("coupon_rate", cap=0.12)), start, end)
    assert all_in.coupon_rate == 0.12 and all_in.bound == "cap"


def test_a_bound_is_applied_before_rounding() -> None:
    start, end = date(2026, 6, 1), date(2026, 7, 1)
    rule = _rule(
        bounds=CouponRateBounds("coupon_rate", floor=0.181234),
        rate_rounding_decimals=2,
        amount_rounding_decimals=2,
    )
    coupon = _one(rule, start, end, curve=None)
    assert coupon.coupon_rate == pytest.approx(0.1812, abs=1e-15)
    assert coupon.amount_rub == round(1000.0 * 0.1812 * 30 / 365, 2)


def test_projected_bounds_need_a_guard_and_must_be_outside_it() -> None:
    start, end = date(2026, 8, 10), date(2026, 11, 10)  # projected near 14 %
    floor = CouponRateBounds("coupon_rate", floor=0.139)
    with pytest.raises(UnsupportedProduct, match="strike_guard_bp"):
        _one(_rule(bounds=floor), start, end)
    with pytest.raises(UnsupportedProduct, match="inside the 25 bp guard"):
        _one(_rule(bounds=floor), start, end, guard=25.0)
    deep = _one(_rule(bounds=CouponRateBounds("coupon_rate", floor=0.05)), start, end, guard=25.0)
    plain = _one(_rule(), start, end)
    assert deep.amount_rub == plain.amount_rub and deep.bound is None
    capped = _one(_rule(bounds=CouponRateBounds("coupon_rate", cap=0.08)), start, end, guard=25.0)
    assert capped.bound == "cap"
    assert capped.amount_rub == pytest.approx(1000.0 * 0.08 * 92 / 365, rel=1e-14)
    # A capped coupon no longer moves with the projection curve (intrinsic value).
    shocked = DiscountCurve.flat(VALUATION, 0.15)
    assert _one(
        _rule(bounds=capped_bounds()), start, end, curve=shocked, guard=25.0
    ).amount_rub == (capped.amount_rub)


def capped_bounds() -> CouponRateBounds:
    return CouponRateBounds("coupon_rate", cap=0.08)


def test_bound_validation() -> None:
    with pytest.raises(ValueError, match="floor, a cap or both"):
        CouponRateBounds("coupon_rate")
    with pytest.raises(ValueError, match="below the cap"):
        CouponRateBounds("index_rate", floor=0.2, cap=0.1)
    with pytest.raises(ValueError, match="coupon_rate or index_rate"):
        CouponRateBounds("all_in", floor=0.1)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="multiplier must be positive"):
        _rule(multiplier=0.0)
    with pytest.raises(ValueError, match="cannot be negative"):
        FloatingLeg(
            _rule(),
            (FloatingCouponPeriod(VALUATION, date(2026, 9, 5), date(2026, 9, 5), 1.0),),
            _history(),
            strike_guard_bp=-1.0,
        )


# --- observation shift and in-advance windows --------------------------------------


def test_calendar_day_shift_observes_the_shifted_window_and_accrues_over_it() -> None:
    # Coupon [2026-07-20, 2026-08-03) observes [2026-07-14, 2026-07-28): 14 days, of which
    # 2026-07-14 (Tue) and 15 (holiday, Tuesday's fixing again) at one rate.
    rows = tuple(
        (d, 0.10 + 0.001 * i) for i, d in enumerate(_business_days(date(2026, 6, 1), VALUATION))
    )
    history = FixingHistory("ruonia", VALUATION, rows, CALENDAR, "fix")
    rates = dict(rows)
    shift = ObservationShift(6, "calendar_days", "observation")
    coupon = _one(
        _rule(margin=0.01, observation_shift=shift),
        date(2026, 7, 20),
        date(2026, 8, 3),
        history=history,
    )
    expected = 0.0
    day = date(2026, 7, 14)
    while day < date(2026, 7, 28):
        fixing = max(d for d in rates if d <= day)
        expected += rates[fixing] / 365.0
        day += timedelta(days=1)
    assert coupon.amount_rub == pytest.approx(1000.0 * (expected + 0.01 * 14 / 365), rel=1e-13)
    assert coupon.realized_days == 14
    # On the accrual period instead (same length, so the same amount here).
    accrual = _one(
        _rule(margin=0.01, observation_shift=replace(shift, accrues_over="accrual")),
        date(2026, 7, 20),
        date(2026, 8, 3),
        history=history,
    )
    assert accrual.amount_rub == pytest.approx(coupon.amount_rub, rel=1e-13)


def test_a_business_day_shift_moves_both_boundaries_by_business_days() -> None:
    shift = ObservationShift(2, "business_days", "observation")
    start, end = date(2026, 7, 20), date(2026, 7, 27)  # Mon..Mon
    coupon = _one(_rule(observation_shift=shift), start, end)
    # Observes [Thu 2026-07-16, Thu 2026-07-23): 7 days at 15 %.
    assert coupon.realized_days == 7
    assert coupon.amount_rub == pytest.approx(1000.0 * 0.15 * 7 / 365, rel=1e-13)


def test_a_projected_shifted_compounded_coupon_telescopes_on_the_window() -> None:
    start, end = date(2026, 9, 14), date(2026, 12, 14)
    shift = ObservationShift(7, "calendar_days", "accrual")
    coupon = _one(_rule(averaging="compounded", observation_shift=shift), start, end)
    w0, w1 = start - timedelta(days=7), end - timedelta(days=7)
    assert CALENDAR.is_business_day(w0) and CALENDAR.is_business_day(w1)
    index = (CURVE.discount_factor(w0) / CURVE.discount_factor(w1) - 1.0) / ((w1 - w0).days / 365)
    assert coupon.index_rate == pytest.approx(index, rel=1e-12)
    assert coupon.amount_rub == pytest.approx(1000.0 * index * (end - start).days / 365, rel=1e-12)


def test_an_in_advance_window_sets_the_rate_and_the_period_sets_the_amount() -> None:
    window = ObservationWindow(2, "business_days", length_months=1)
    start, end = date(2026, 7, 22), date(2026, 10, 21)
    # 2 business days before Wed 2026-07-22 is Mon 2026-07-20; window [06-20, 07-20).
    assert window.bounds(CALENDAR, start, end) == (date(2026, 6, 20), date(2026, 7, 20))
    rule = _rule(margin=0.012, observation_window=window, rate_rounding_decimals=2)
    coupon = _one(rule, start, end, curve=None)
    assert coupon.source == "fixed_by_history" and coupon.realized_days == 30
    assert coupon.coupon_rate == pytest.approx(0.1620, abs=1e-15)
    assert coupon.amount_rub == pytest.approx(1000.0 * 0.162 * 91 / 365, rel=1e-14)
    same_length = ObservationWindow(6, "calendar_days")
    assert same_length.bounds(CALENDAR, start, end) == (date(2026, 4, 16), date(2026, 7, 16))


def test_observation_rule_validation() -> None:
    shift = ObservationShift(6, "calendar_days", "observation")
    with pytest.raises(ValueError, match="either a lookback or an observation shift"):
        _rule(lookback_business_days=2, observation_shift=shift)
    with pytest.raises(ValueError, match="observation window takes no"):
        _rule(observation_shift=shift, observation_window=ObservationWindow(1, "calendar_days"))
    with pytest.raises(ValueError, match="accrues_over"):
        ObservationShift(6, "calendar_days", "payment")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="RUONIA is observed on business days only"):
        _rule(observation_days="calendar")
    with pytest.raises(ValueError, match="needs a fixing_date"):
        _rule(averaging="single_fixing")


# --- single fixings and the key rate ----------------------------------------------


def _key_history(rows: tuple[tuple[date, float], ...]) -> FixingHistory:
    return FixingHistory("key_rate", VALUATION, rows, CALENDAR, "kr")


def test_key_rate_applies_from_its_effective_date_by_calendar_or_business_day() -> None:
    # A decision effective on Saturday 2026-07-25 (synthetic) is seen on that day by
    # calendar-day observation and only from Monday by business-day observation.
    history = _key_history(((date(2026, 6, 1), 0.18), (date(2026, 7, 25), 0.16)))
    start, end = date(2026, 7, 20), date(2026, 8, 3)
    rule = _rule(index="key_rate")
    business = _one(rule, start, end, history=history, curve=None)
    calendar = _one(
        replace(rule, observation_days="calendar"), start, end, history=history, curve=None
    )
    assert business.amount_rub == pytest.approx(1000.0 * (0.18 * 7 + 0.16 * 7) / 365, rel=1e-13)
    assert calendar.amount_rub == pytest.approx(1000.0 * (0.18 * 5 + 0.16 * 9) / 365, rel=1e-13)


def test_single_fixings_in_advance_and_in_arrears() -> None:
    history = _key_history(((date(2026, 6, 1), 0.18), (date(2026, 7, 27), 0.16)))
    start, end = date(2026, 7, 29), date(2026, 10, 29)
    advance = FixingDateRule("accrual_start", 1, "business_days")  # Tue 2026-07-28
    rule = _rule(index="key_rate", averaging="single_fixing", margin=0.02, fixing_date=advance)
    coupon = _one(rule, start, end, history=history, curve=None)
    assert coupon.source == "fixed_by_history" and coupon.index_rate == 0.16
    assert coupon.amount_rub == pytest.approx(1000.0 * 0.18 * 92 / 365, rel=1e-14)
    # In arrears 5 calendar days before the end (Sat 2026-10-24 -> Friday's business day):
    # projected as the curve's overnight forward over that business day.
    arrears = replace(rule, fixing_date=FixingDateRule("accrual_end", 5, "calendar_days"))
    projected = _one(arrears, start, end, history=history)
    friday = date(2026, 10, 23)
    assert projected.source == "projected"
    assert projected.index_rate == pytest.approx(_forward(friday, date(2026, 10, 26)), rel=1e-13)
    on_day = _one(replace(arrears, observation_days="calendar"), start, end, history=history)
    assert on_day.index_rate == pytest.approx(
        _forward(date(2026, 10, 24), date(2026, 10, 25)), rel=1e-13
    )


def test_key_rate_projection_curve_reproduces_the_key_rate_path() -> None:
    forward = KeyRateForwardCurve(
        VALUATION,
        0.16,
        date(2026, 9, 14),
        (
            KeyRateForwardSegment(date(2026, 9, 14), date(2026, 10, 26), 0.15, "3M"),
            KeyRateForwardSegment(date(2026, 10, 26), date(2027, 8, 5), 0.13, "1Y"),
        ),
    )
    for days in ("business", "calendar"):
        curve = key_rate_projection_curve(
            forward,
            CALENDAR,
            rate_day_count=ACT365,
            observation_days=days,
        )
        rule = _rule(index="key_rate", observation_days=days)
        empty = _key_history(())
        start, end = date(2026, 9, 1), date(2026, 12, 1)
        coupon = _one(rule, start, end, history=empty, curve=curve)
        expected = 0.0
        day = start
        while day < end:
            if days == "calendar":
                expected += forward.rate_at(day) / 365
            else:
                bday = day
                while not CALENDAR.is_business_day(bday):
                    bday -= timedelta(days=1)
                expected += forward.rate_at(bday) / 365
            day += timedelta(days=1)
        assert coupon.amount_rub == pytest.approx(1000.0 * expected, rel=1e-12), days
    with pytest.raises(ValueError, match="business or calendar"):
        key_rate_projection_curve(forward, CALENDAR, rate_day_count=ACT365, observation_days="x")  # type: ignore[arg-type]


# --- point in time ----------------------------------------------------------------


def test_rows_published_after_the_cut_are_dropped_and_never_filled() -> None:
    days = _business_days(date(2026, 7, 1), VALUATION)
    rows = tuple((d, 0.15) for d in days)
    stamps = tuple(
        datetime(d.year, d.month, d.day, 11, tzinfo=UTC) + timedelta(days=1) for d in days
    )
    history = FixingHistory("ruonia", VALUATION, rows, CALENDAR, "fix", published_at=stamps)
    cut = datetime(2026, 8, 4, 10, tzinfo=UTC)  # 3 August is published at 11:00 on 4 August
    known = history.published_by(cut)
    assert known.latest_fixing_date == date(2026, 7, 31)
    assert known.unpublished_business_days == (date(2026, 8, 3), date(2026, 8, 4))
    leg = FloatingLeg(
        _rule(),
        (FloatingCouponPeriod(date(2026, 7, 27), date(2026, 8, 10), date(2026, 8, 10), 1.0),),
        known,
    )
    with pytest.raises(MissingFixing, match="unpublished for 2 business days") as caught:
        leg.project(CURVE, settlement_date=date(2026, 7, 1))
    assert isinstance(caught.value, MissingMarketData) and isinstance(caught.value, LookupError)
    assert caught.value.code == "MISSING_MARKET_DATA"
    assert history.published_by(datetime(2026, 9, 1, tzinfo=UTC)) is history
    with pytest.raises(ValueError, match="one timestamp per fixing"):
        replace(history, published_at=stamps[:-1])
    with pytest.raises(ValueError, match="no publication timestamps"):
        _history().published_by(cut)


def test_known_at_parses_iso_timestamps() -> None:
    assert _history(known_at_utc="2026-08-05T09:00:00Z").known_at == STAMP
    with pytest.raises(ValueError, match="not an ISO 8601"):
        _ = _history(known_at_utc="yesterday").known_at
    with pytest.raises(ValueError, match="no UTC offset"):
        _ = _history(known_at_utc="2026-08-05T09:00:00").known_at


# --- encodings --------------------------------------------------------------------


def test_rules_without_new_terms_keep_their_identity_and_encoding() -> None:
    rule = FloatingCouponRule("ruonia", "compounded", 0.013, ACT365, ACT365, 2, 2, 2)
    legacy = {
        "model": FLOATING_MODEL_VERSION,
        "index": "ruonia",
        "averaging": "compounded",
        "margin": 0.013,
        "coupon_day_count": ACT365.value,
        "rate_day_count": ACT365.value,
        "lookback_business_days": 2,
        "rate_rounding_decimals": 2,
        "amount_rounding_decimals": 2,
    }
    assert rule.identifier == canonical_sha256(legacy)
    encoded = to_dict(rule)
    assert set(encoded) == {
        "index",
        "averaging",
        "margin",
        "coupon_day_count",
        "rate_day_count",
        "lookback_business_days",
        "rate_rounding_decimals",
        "amount_rounding_decimals",
    }
    assert from_dict(FloatingCouponRule, encoded) == rule
    full = replace(
        rule,
        lookback_business_days=0,
        bounds=CouponRateBounds("coupon_rate", floor=0.0),
        observation_shift=ObservationShift(6, "calendar_days", "observation"),
        multiplier=1.1,
    )
    assert full.identifier != replace(rule, lookback_business_days=0).identifier
    assert from_dict(FloatingCouponRule, to_dict(full)) == full
    history = _history(published_at=())
    assert "published_at" not in to_dict(history)
