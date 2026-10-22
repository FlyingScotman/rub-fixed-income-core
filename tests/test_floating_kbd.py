"""The KBD constant-maturity index (0.34.0): projection, convexity, risk and validation."""

from __future__ import annotations

import math
from dataclasses import replace
from datetime import date, timedelta

import pytest
from test_kbd import load

from rub_fixed_income import (
    ANNUAL,
    RISK_BUCKETS,
    BondCashflow,
    BondRiskEngine,
    BondRiskRequest,
    BusinessCalendar,
    ConstantMaturityConvexity,
    DayCount,
    DiscountCurve,
    FactorRisk,
    FixingDateRule,
    FixingHistory,
    FloatingCouponPeriod,
    FloatingCouponRule,
    FloatingLeg,
    InvalidTerms,
    TwoCurveBondRiskResult,
    ZSpreadConvention,
    basis_view,
    bond_factor_risk,
    kbd_discount_curve,
    kbd_zero_rate_bp,
)

PARAMS, _ = load("zcyc_2026-10-15.json")
VALUATION = PARAMS.trade_date
CALENDAR = BusinessCalendar(version="weekdays")
ACT365 = DayCount.ACT_365_FIXED
KBD = kbd_discount_curve(PARAMS, zero_shock="annual")
RUONIA = DiscountCurve.flat(VALUATION, 0.145)
TENOR = 7.0


def rule(**kw: object) -> FloatingCouponRule:
    args: dict[str, object] = {
        "index": "kbd",
        "averaging": "single_fixing",
        "margin": 0.005,
        "coupon_day_count": ACT365,
        "rate_day_count": ACT365,
        "rate_rounding_decimals": 2,
        "amount_rounding_decimals": 2,
        "fixing_date": FixingDateRule("accrual_start", 5, "business_days"),
        "index_tenor_years": TENOR,
    }
    args.update(kw)
    return FloatingCouponRule(**args)  # type: ignore[arg-type]


def history(fixings: tuple[tuple[date, float], ...] = ()) -> FixingHistory:
    return FixingHistory("kbd", VALUATION, fixings, CALENDAR, "test", tenor_years=TENOR)


def periods(count: int = 14, start: date = date(2026, 10, 23)) -> tuple[FloatingCouponPeriod, ...]:
    out = []
    for _ in range(count):
        end = start + timedelta(days=182)
        out.append(FloatingCouponPeriod(start, end, end, 1000.0))
        start = end
    return tuple(out)


def back(day: date, count: int) -> date:
    """``count`` business days strictly before ``day``."""
    for _ in range(count):
        day -= timedelta(days=1)
        while not CALENDAR.is_business_day(day):
            day -= timedelta(days=1)
    return day


def _forward(fixing: date) -> float:
    """``(G(t + T)(t + T) - G(t) t) / T`` straight from the ZCYC formula."""
    t = (fixing - VALUATION).days / 365.0
    end = t + TENOR
    total = kbd_zero_rate_bp(PARAMS, end) * end
    if t > 0:
        total -= kbd_zero_rate_bp(PARAMS, t) * t
    return math.expm1(total / TENOR / 1e4)


def test_projected_fixing_is_the_forward_kbd_yield_over_the_tenor() -> None:
    leg = FloatingLeg(rule(), periods(), history())
    for i in (1, 5, 13):
        coupon = leg.coupon(i, KBD)
        fixing = back(leg.periods[i].accrual_start, 5)
        assert coupon.source == "projected" and coupon.index_rate is not None
        # The materialised KBD is within 0.005 bp of the formula at every day, so the
        # forward over seven years is within about 0.02 bp.
        assert coupon.index_rate == pytest.approx(_forward(fixing), abs=2e-6)
        days = (leg.periods[i].accrual_end - leg.periods[i].accrual_start).days
        # A projection is not rounded.
        assert coupon.amount_rub == pytest.approx(1000.0 * (coupon.index_rate + 0.005) * days / 365)


def test_known_fixing_comes_from_history_before_the_coupon_is_published() -> None:
    """A coupon fixed on an earlier KBD is known even while MOEX lists it as unknown."""
    start = date(2026, 10, 20)
    fixing = back(start, 5)
    assert fixing < VALUATION
    leg = FloatingLeg(rule(), periods(start=start), history(((fixing, 0.16487),)))
    first = leg.coupon(0, None)
    assert first.source == "fixed_by_history" and first.coupon_rate == pytest.approx(0.1699)
    assert first.amount_rub == round(1000.0 * 0.1699 * 182 / 365, 2)


def test_period_margin_overrides_the_rule() -> None:
    reset = tuple(
        replace(p, margin=0.0225) if p.accrual_start >= date(2029, 1, 1) else p for p in periods()
    )
    base = FloatingLeg(rule(), periods(), history())
    leg = FloatingLeg(rule(), reset, history())
    for i, period in enumerate(reset):
        expected = 0.0225 if period.margin is not None else 0.005
        assert leg.coupon(i, KBD).coupon_rate == pytest.approx(
            base.coupon(i, KBD).index_rate + expected  # type: ignore[operator]
        )
    # The margin of the period enters the leg identity; no margin keeps the 0.33.0 bytes.
    assert leg.identity != base.identity
    assert "margin" not in str(base.identity["periods"])


def test_convexity_adjustment_is_explicit_and_zero_by_default() -> None:
    plain = FloatingLeg(rule(), periods(), history())
    vol = 150.0
    adjusted = FloatingLeg(
        rule(convexity=ConstantMaturityConvexity(normal_vol_bp=vol)), periods(), history()
    )
    assert plain.rule.identifier != adjusted.rule.identifier
    for i in (1, 7, 13):
        f = plain.coupon(i, KBD).index_rate
        g = adjusted.coupon(i, KBD).index_rate
        assert f is not None and g is not None
        fixing = back(plain.periods[i].accrual_start, 5)
        t = (fixing - VALUATION).days / 365.0
        sigma = vol * 1e-4
        assert g - f == pytest.approx(0.5 * sigma**2 * t * (TENOR + 1.0) / (1.0 + f), rel=1e-12)
    # About 23 bp of coupon at three years for 150 bp a year of normal vol.
    assert 0.0015 < 0.5 * (vol * 1e-4) ** 2 * 3.0 * 8.0 / 1.16 < 0.0030
    zero = FloatingLeg(
        rule(convexity=ConstantMaturityConvexity(normal_vol_bp=0.0)), periods(), history()
    )
    assert zero.coupon(5, KBD).index_rate == plain.coupon(5, KBD).index_rate
    with pytest.raises(ValueError, match="normal volatility"):
        ConstantMaturityConvexity(normal_vol_bp=-1.0)
    with pytest.raises(ValueError, match="KBD index only"):
        FloatingCouponRule(
            "ruonia", "compounded", 0.0, ACT365, ACT365, convexity=ConstantMaturityConvexity(10.0)
        )


def _request(projection: DiscountCurve = KBD) -> BondRiskRequest:
    leg = FloatingLeg(rule(), periods(), history())
    return BondRiskRequest(
        cashflows=(BondCashflow(leg.periods[-1].payment_date, 1000.0),),
        settlement_date=VALUATION,
        curve=RUONIA,
        spread_convention=ZSpreadConvention(compounding=ANNUAL),
        spread_kind="credit",
        spread_bp=250.0,
        floating_leg=leg,
        projection_curve=projection,
        quantity=1000.0,
    )


def test_projection_risk_is_a_steepener_on_the_kbd_buckets() -> None:
    """The coupon rises with the long end (t + 7Y) and falls with the short end (t)."""
    result = BondRiskEngine(_request()).risk(difference="central")
    assert isinstance(result, TwoCurveBondRiskResult)
    assert result.projection_curve_basis == "annual_zero"
    proj = dict(zip(RISK_BUCKETS, (b.dv01_rub for b in result.projection_buckets), strict=True))
    assert all(proj[b] <= 1e-9 for b in ("6M", "1Y", "2Y", "3Y", "5Y"))
    assert proj["10Y"] > 0 and proj["15Y"] > 0
    assert sum(proj.values()) > 0
    # Discount risk on RUONIA: a long position loses when RUONIA rises.
    assert sum(b.dv01_rub for b in result.buckets) < 0
    # A parallel move of both curves: a fixed-tenor floater keeps little rate risk.
    assert abs(result.parallel_dv01_rub) < 0.2 * sum(proj.values())


def test_validation() -> None:
    with pytest.raises(ValueError, match="needs index_tenor_years"):
        rule(index_tenor_years=None)
    with pytest.raises(ValueError, match="KBD index only"):
        FloatingCouponRule("ruonia", "compounded", 0.0, ACT365, ACT365, index_tenor_years=7.0)
    with pytest.raises(ValueError, match="single fixing"):
        rule(averaging="arithmetic", fixing_date=None)
    with pytest.raises(ValueError, match="ACT/365F"):
        rule(rate_day_count=DayCount.ACT_ACT_ISDA)
    with pytest.raises(ValueError, match="30 years"):
        rule(index_tenor_years=31.0)
    with pytest.raises(ValueError, match="needs tenor_years"):
        FixingHistory("kbd", VALUATION, (), CALENDAR, "test")
    with pytest.raises(ValueError, match="tenors differ"):
        FloatingLeg(rule(index_tenor_years=5.0), periods(), history())
    # Earlier indexes keep their identifiers: the new terms are omitted at their defaults.
    ruonia = FloatingCouponRule("ruonia", "compounded", 0.0, ACT365, ACT365)
    assert "index_tenor_years" not in str(ruonia.__dataclass_fields__["index_tenor_years"].default)
    assert FixingHistory("ruonia", VALUATION, (), CALENDAR, "x").identity.get("tenor_years") is None


def _taylor(risk: FactorRisk, moves: dict[str, float]) -> float:
    x = [moves.get(f, 0.0) for f in risk.factors]
    total = math.fsum(g * m for g, m in zip(risk.first_order, x, strict=True))
    for e in risk.hessian:
        total += (0.5 if e.i == e.j else 1.0) * e.value * x[e.i] * x[e.j]
    return total


def test_basis_view_is_the_same_risk_on_base_and_basis() -> None:
    engine = BondRiskEngine(_request())
    risk = engine.risk(difference="central")
    second = engine.second_order()
    kc = bond_factor_risk(risk, "RUONIA", second, projection_curve_id="KBD")
    view = basis_view(kc, "RUONIA", "KBD", "KBD-RUONIA")
    assert view.factors[11:22] == tuple(f"IR|KBD-RUONIA|{b}" for b in RISK_BUCKETS)
    first, old = view.first_order_by_factor(), kc.first_order_by_factor()
    for b in RISK_BUCKETS:
        assert first[f"IR|RUONIA|{b}"] == old[f"IR|RUONIA|{b}"] + old[f"IR|KBD|{b}"]
        assert first[f"IR|KBD-RUONIA|{b}"] == old[f"IR|KBD|{b}"]
    # Any move gives the same second-order P&L in both views: x_K = y_R + y_B.
    for y_r, y_b, cs in ((10.0, 0.0, 0.0), (0.0, 25.0, 0.0), (-15.0, 40.0, 5.0)):
        base = {f"IR|RUONIA|{b}": y_r * (i + 1) / 11 for i, b in enumerate(RISK_BUCKETS)}
        basis = {f"IR|KBD-RUONIA|{b}": y_b * (11 - i) / 11 for i, b in enumerate(RISK_BUCKETS)}
        moved: dict[str, float] = {}
        for b in RISK_BUCKETS:
            moved[f"IR|RUONIA|{b}"] = base[f"IR|RUONIA|{b}"]
            moved[f"IR|KBD|{b}"] = base[f"IR|RUONIA|{b}"] + basis[f"IR|KBD-RUONIA|{b}"]
        moved["CS"] = base["CS"] = cs
        assert _taylor(view, {**base, **basis}) == pytest.approx(_taylor(kc, moved), rel=1e-12)
    # A risk on one curve only gains the base factors (zero before) and stays equivalent.
    kbd_only = FactorRisk(
        tuple(f"IR|KBD|{b}" for b in RISK_BUCKETS), tuple(range(11)), (), "central", 1.0, False
    )
    only = basis_view(kbd_only, "RUONIA", "KBD", "B")
    assert only.factors[:11] == tuple(f"IR|RUONIA|{b}" for b in RISK_BUCKETS)
    assert only.first_order[:11] == only.first_order[11:] == tuple(float(i) for i in range(11))
    assert basis_view(kc, "X", "Y", "Z") is kc
    with pytest.raises(InvalidTerms, match="three different"):
        basis_view(kc, "RUONIA", "KBD", "KBD")


def test_period_margin_enters_the_accrued_of_a_coupon_set_in_arrears() -> None:
    days = (VALUATION - timedelta(days=d) for d in range(40, 0, -1))
    rows = tuple((day, 0.16) for day in days if CALENDAR.is_business_day(day))
    ruonia = FixingHistory("ruonia", VALUATION, rows, CALENDAR, "r")
    start = VALUATION - timedelta(days=30)
    plain = FloatingCouponPeriod(
        start, start + timedelta(days=91), start + timedelta(days=91), 1000.0
    )
    arrears = FloatingCouponRule("ruonia", "arithmetic", 0.01, ACT365, ACT365)
    base = FloatingLeg(arrears, (plain,), ruonia).accrued_interest(VALUATION)
    reset = FloatingLeg(arrears, (replace(plain, margin=0.02),), ruonia).accrued_interest(VALUATION)
    assert reset - base == pytest.approx(1000.0 * 0.01 * 30 / 365, abs=0.011)
