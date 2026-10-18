"""Horizon valuation: carry, rolldown and P&L explain (roadmap 0.8)."""

from __future__ import annotations

import math
from dataclasses import replace
from datetime import date, timedelta

import pytest

from rub_fixed_income import (
    CONTINUOUS,
    BusinessCalendar,
    CashflowKind,
    CouponPeriod,
    CurveBootstrapRequest,
    DayCount,
    DiscountCurve,
    FixedCouponLeg,
    FloatingCouponLeg,
    FloatingCouponPeriod,
    FloatingCouponRule,
    Instrument,
    InvalidTerms,
    Offer,
    OfferKind,
    OisQuote,
    PrincipalLeg,
    ProductType,
    Redemption,
    UnsupportedProduct,
    ZSpreadConvention,
    explain_horizon_pnl,
    horizon_valuation,
    roll_curve,
)
from rub_fixed_income.dates import add_months

D0 = date(2026, 10, 15)  # Thursday; settlement Friday 16 October
D1 = date(2026, 11, 16)  # Monday; settlement Tuesday 17 November
CAL = BusinessCalendar(version="synthetic-weekends-only")
CONT = ZSpreadConvention(compounding=CONTINUOUS)


def zero_coupon(maturity: date = date(2029, 10, 15)) -> Instrument:
    return Instrument(
        "ZERO",
        ProductType.FIXED_BOND,
        "RUB",
        1000.0,
        None,
        PrincipalLeg((Redemption(maturity, 1000.0),)),
        "CAL",
    )


def coupon_bond(
    instrument_id: str = "BULLET",
    count: int = 10,
    coupon: float = 40.0,
    first: date = date(2026, 5, 1),
    redemptions: tuple[Redemption, ...] | None = None,
    offers: tuple[Offer, ...] = (),
) -> Instrument:
    periods = tuple(
        CouponPeriod(
            add_months(first, 6 * i),
            add_months(first, 6 * (i + 1)),
            add_months(first, 6 * (i + 1)),
            coupon,
        )
        for i in range(count)
    )
    return Instrument(
        instrument_id,
        ProductType.FIXED_BOND,
        "RUB",
        1000.0,
        FixedCouponLeg(periods),
        PrincipalLeg(redemptions or (Redemption(periods[-1].payment_date, 1000.0),)),
        "CAL",
        offers=offers,
    )


def upward_curve(curve_date: date = D0, shift: float = 0.0) -> DiscountCurve:
    nodes = []
    for years in (0.25, 0.5, 1, 2, 3, 5, 7, 10, 15):
        rate = 0.10 + 0.004 * years + shift
        nodes.append((curve_date + timedelta(days=round(365 * years)), math.exp(-rate * years)))
    return DiscountCurve(curve_date, tuple(nodes))


def test_zero_coupon_carry_is_analytical_and_a_flat_curve_has_no_rolldown() -> None:
    r, s = 0.12, 0.015
    result = horizon_valuation(
        zero_coupon(),
        DiscountCurve.flat(D0, r),
        CAL,
        horizon_date=D1,
        spread_bp=s * 1e4,
        spread_convention=CONT,
    )
    assert result.settlement_date == date(2026, 10, 16)
    assert result.horizon_settlement_date == date(2026, 11, 17)
    h = (result.horizon_settlement_date - result.settlement_date).days / 365.0
    assert result.carry_rub == pytest.approx(
        result.pv_start_rub * math.expm1((r + s) * h), rel=1e-10
    )
    assert abs(result.rolldown_rub) < 1e-9
    assert result.received_rub == 0.0 and result.accrued_start_rub == 0.0
    assert result.horizon_pnl_rub == pytest.approx(
        result.carry_rub + result.rolldown_rub, abs=1e-12
    )


def test_coupon_paid_inside_the_horizon_is_received_and_accrual_restarts() -> None:
    bond = coupon_bond()  # coupons on 1 November and 1 May
    result = horizon_valuation(bond, upward_curve(), CAL, horizon_date=D1, spread_bp=120.0)
    assert result.coupons_received_rub == 40.0 and result.principal_received_rub == 0.0
    assert result.accrued_start_rub == pytest.approx(40.0 * 168 / 184, abs=0.01)
    assert result.accrued_end_rub == pytest.approx(40.0 * 16 / 181, abs=0.01)
    assert result.accrued_start_rub is not None and result.accrued_end_rub is not None
    clean_start = result.pv_start_rub - result.accrued_start_rub
    clean_end = result.pv_rolled_rub - result.accrued_end_rub
    assert result.horizon_pnl_rub == pytest.approx(
        clean_end - clean_start + result.accrued_end_rub - result.accrued_start_rub + 40.0
    )


def test_amortization_inside_the_horizon_reduces_the_nominal() -> None:
    first = date(2026, 5, 1)
    bond = coupon_bond(
        "AMORT",
        count=6,
        first=first,
        redemptions=(
            Redemption(date(2026, 11, 1), 300.0, CashflowKind.AMORTIZATION),
            Redemption(date(2029, 5, 1), 700.0),
        ),
    )
    result = horizon_valuation(bond, upward_curve(), CAL, horizon_date=D1, spread_bp=150.0)
    assert result.principal_received_rub == 300.0
    assert result.nominal_start_rub == 1000.0 and result.nominal_end_rub == 700.0
    assert result.carry_rub == pytest.approx(
        result.pv_forward_rub + result.received_rub - result.pv_start_rub
    )


def test_rolling_down_an_upward_sloping_curve_gains() -> None:
    bond = coupon_bond(count=16)
    result = horizon_valuation(bond, upward_curve(), CAL, horizon_date=D1, spread_bp=100.0)
    assert result.rolldown_rub > 0.0
    rolled = roll_curve(upward_curve(), D1)
    assert rolled.discount_factor(D1 + timedelta(days=400)) == upward_curve().discount_factor(
        D0 + timedelta(days=400)
    )
    with pytest.raises(InvalidTerms):
        roll_curve(upward_curve(), D0 - timedelta(days=1))


def test_confirmed_put_is_the_redemption_horizon_and_dirty_price_calibrates_the_spread() -> None:
    put = Offer(date(2028, 5, 1), OfferKind.PUT, confirmed=True)
    bond = coupon_bond("PUT", count=10, offers=(put,))
    to_put = horizon_valuation(
        bond,
        upward_curve(),
        CAL,
        horizon_date=D1,
        spread_bp=200.0,
        redemption_horizon="confirmed_put",
    )
    to_maturity = horizon_valuation(bond, upward_curve(), CAL, horizon_date=D1, spread_bp=200.0)
    assert to_put.redemption_horizon == "confirmed_put"
    assert to_put.redemption_date == put.offer_date
    assert to_maturity.redemption_date == date(2031, 5, 1)
    assert to_put.pv_start_rub != to_maturity.pv_start_rub
    again = horizon_valuation(
        bond,
        upward_curve(),
        CAL,
        horizon_date=D1,
        dirty_price_rub=to_put.pv_start_rub,
        redemption_horizon="confirmed_put",
    )
    assert again.spread_bp == pytest.approx(200.0, abs=1e-7)
    assert again.carry_rub == pytest.approx(to_put.carry_rub, abs=1e-6)


def test_explain_reconciles_to_full_revaluation() -> None:
    bond = coupon_bond(count=16)
    end_curve = upward_curve(D1, shift=0.0025)
    explain = explain_horizon_pnl(
        bond, upward_curve(), end_curve, CAL, start_spread_bp=150.0, end_spread_bp=160.0
    )
    parts = (
        explain.carry_rub
        + explain.rolldown_rub
        + explain.rates_rub
        + explain.spread_rub
        + explain.residual_rub
    )
    assert parts == pytest.approx(explain.total_rub, abs=1e-9)
    assert explain.total_rub == pytest.approx(
        explain.pv_end_rub + explain.horizon.received_rub - explain.horizon.pv_start_rub
    )
    assert explain.rates_rub < 0.0 and explain.spread_rub < 0.0
    # The cross term of a 25 bp rates move and a 10 bp spread move is second order.
    assert abs(explain.residual_rub) < 0.01 * abs(explain.rates_rub)

    rolled = roll_curve(upward_curve(), D1)
    calm = explain_horizon_pnl(
        bond, upward_curve(), rolled, CAL, start_spread_bp=150.0, end_spread_bp=150.0
    )
    assert abs(calm.rates_rub) < 1e-9 and abs(calm.spread_rub) < 1e-9
    assert abs(calm.residual_rub) < 1e-9
    assert calm.total_rub == pytest.approx(calm.carry_rub + calm.rolldown_rub, abs=1e-9)

    priced = explain_horizon_pnl(
        bond,
        upward_curve(),
        end_curve,
        CAL,
        start_spread_bp=150.0,
        end_dirty_price_rub=explain.pv_end_rub,
    )
    assert priced.end_spread_bp == pytest.approx(160.0, abs=1e-7)


def test_ois_quote_curves_are_bootstrapped_once() -> None:
    quotes = tuple(
        OisQuote(f"{y}Y", D0 + timedelta(days=365 * y), 0.13 + 0.002 * y) for y in (1, 2, 3, 5, 7)
    )
    request = CurveBootstrapRequest(D0, quotes)
    result = horizon_valuation(coupon_bond(), request, CAL, horizon_date=D1, spread_bp=100.0)
    assert result.start_date == D0 and math.isfinite(result.carry_rub)


def test_horizon_rejections() -> None:
    curve = upward_curve()
    with pytest.raises(InvalidTerms):
        horizon_valuation(coupon_bond(), curve, CAL, horizon_date=D0, spread_bp=1.0)
    with pytest.raises(InvalidTerms):
        horizon_valuation(coupon_bond(), curve, CAL, horizon_date=D1)
    with pytest.raises(InvalidTerms):
        horizon_valuation(
            coupon_bond(), curve, CAL, horizon_date=D1, spread_bp=1.0, dirty_price_rub=990.0
        )
    with pytest.raises(InvalidTerms):
        horizon_valuation(
            zero_coupon(date(2026, 11, 2)), curve, CAL, horizon_date=D1, spread_bp=1.0
        )
    rule = FloatingCouponRule(
        "ruonia", "compounded", 0.01, DayCount.ACT_365_FIXED, DayCount.ACT_365_FIXED
    )
    floater = replace(
        coupon_bond(),
        product=ProductType.FLOATING_BOND,
        coupon_leg=FloatingCouponLeg(
            rule,
            (FloatingCouponPeriod(date(2026, 8, 1), date(2026, 11, 1), date(2026, 11, 1), 1000.0),),
            "RUONIA",
        ),
    )
    with pytest.raises(UnsupportedProduct):
        horizon_valuation(floater, curve, CAL, horizon_date=D1, spread_bp=1.0)


def test_horizon_portfolio_resolves_positions_like_value_portfolio() -> None:
    from test_portfolio import BUSINESS_DATE, POLICY, instruments, positions, run, snapshot

    from rub_fixed_income import horizon_portfolio

    book = positions()[:6]
    risk = run(scenarios=())
    carry = horizon_portfolio(
        snapshot=snapshot(),
        instruments=instruments(),
        positions=book,
        policy=POLICY,
        horizon_date=BUSINESS_DATE + timedelta(days=30),
    )
    assert carry.coverage == risk.coverage
    assert [r.position_id for r in carry.positions] == [p.position_id for p in risk.positions]
    for row, valued in zip(carry.positions, risk.positions, strict=True):
        assert row.per_bond.spread_bp == pytest.approx(valued.spread_bp, abs=1e-6)
        assert row.per_bond.pv_start_rub == pytest.approx(valued.dirty_price_rub, rel=1e-10)
        assert row.per_bond.redemption_date == valued.horizon_date
        assert row.horizon_pnl_rub == pytest.approx(row.per_bond.horizon_pnl_rub * row.quantity)
    assert carry.total_horizon_pnl_rub is None
    assert carry.covered_horizon_pnl_rub == pytest.approx(
        math.fsum(r.horizon_pnl_rub for r in carry.positions)
    )
    assert carry.calendars == risk.calendars
    with pytest.raises(InvalidTerms):
        horizon_portfolio(
            snapshot=snapshot(),
            instruments=instruments(),
            positions=book,
            policy=POLICY,
            horizon_date=BUSINESS_DATE,
        )


def test_forward_curve_reads_today_forward_from_the_horizon() -> None:
    from rub_fixed_income import forward_curve, fx_horizon

    curve = upward_curve()
    anchor = curve.discount_factor(D1)
    view = forward_curve(curve, D1)
    assert view.curve_date == D1
    last = curve.nodes[-1][0]
    for days in (0, 1, 17, 400, 2000, (last - D1).days, (last - D1).days + 900):
        target = D1 + timedelta(days=days)
        assert view.discount_factor(target) == pytest.approx(
            curve.discount_factor(target) / anchor, rel=1e-14
        )
    # A horizon past the last node keeps the flat forward of the extrapolation.
    beyond = last + timedelta(days=30)
    tail = forward_curve(curve, beyond)
    later = beyond + timedelta(days=365)
    assert tail.discount_factor(later) == pytest.approx(
        curve.discount_factor(later) / curve.discount_factor(beyond), rel=1e-13
    )
    flat = DiscountCurve.flat(D0, 0.12)
    assert forward_curve(flat, D1).discount_factor(D1 + timedelta(days=365)) == pytest.approx(
        math.exp(-0.12)
    )
    assert forward_curve(curve, D0) is curve
    with pytest.raises(InvalidTerms):
        forward_curve(curve, D0 - timedelta(days=1))
    assert fx_horizon("spot_held") == "spot_held"
    with pytest.raises(InvalidTerms):
        fx_horizon("sticky")


def test_roll_key_rate_curve_shifts_every_date() -> None:
    from rub_fixed_income import roll_key_rate_curve
    from rub_fixed_income.key_rate import KeyRateForwardCurve, KeyRateForwardSegment

    curve = KeyRateForwardCurve(
        D0,
        0.165,
        date(2026, 10, 26),
        (KeyRateForwardSegment(date(2026, 10, 26), date(2027, 6, 1), 0.15, "1Y"),),
    )
    rolled = roll_key_rate_curve(curve, D1)
    shift = D1 - D0
    assert rolled.curve_date == D1
    assert rolled.current_rate_end == curve.current_rate_end + shift
    assert rolled.rate_at(date(2027, 1, 4) + shift) == curve.rate_at(date(2027, 1, 4))
    with pytest.raises(InvalidTerms):
        roll_key_rate_curve(curve, D0 - timedelta(days=1))
