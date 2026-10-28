"""A floating coupon fixed once per multi-period reset cycle (0.41.0, issue #29).

Synthetic perpetual-style terms valued to a finite horizon: the coupon is reset every five
years to the KBD 5Y plus a margin and paid semi-annually, so the ten periods of a cycle
share one fixing, four publications before the cycle's start
(``FloatingCouponPeriod.fixing_anchor``).
"""

from __future__ import annotations

import math
from dataclasses import replace
from datetime import date, timedelta

import pytest
import test_portfolio
from test_floating_kbd import RUONIA, back
from test_kbd import load
from test_portfolio import AS_OF
from test_portfolio_floaters import CALENDAR as PORTFOLIO_CALENDAR
from test_portfolio_floaters import KNOWN, OIS, snapshot

from rub_fixed_income import (
    ANNUAL,
    RISK_BUCKETS,
    AccountingCategory,
    BondCashflow,
    BondPriceObservation,
    BondRiskEngine,
    BondRiskRequest,
    BusinessCalendar,
    CurveEntry,
    DayCount,
    FixingDateRule,
    FixingEntry,
    FixingHistory,
    FloatingCouponLeg,
    FloatingCouponPeriod,
    FloatingCouponRule,
    FloatingLeg,
    Instrument,
    InvalidTerms,
    Position,
    PrincipalLeg,
    ProductType,
    Redemption,
    TwoCurveBondRiskResult,
    ValuationPolicy,
    ZSpreadConvention,
    from_dict,
    kbd_discount_curve,
    kbd_zero_rate_bp,
    to_dict,
    value_portfolio,
)
from rub_fixed_income.dates import add_months

PARAMS, _ = load("zcyc_2026-10-15.json")
VALUATION = PARAMS.trade_date
CALENDAR = BusinessCalendar(version="weekdays")
ACT365 = DayCount.ACT_365_FIXED
KBD = kbd_discount_curve(PARAMS, zero_shock="annual")
TENOR = 5.0
MARGIN = 0.0325
LAG = 4
FIRST_RESET = date(2024, 12, 16)
CYCLES = 3
PER_CYCLE = 10
PAST_FIXING = back(FIRST_RESET, LAG)
PAST_RATE = 0.15
# Each period start's own fixing, published before the valuation date, at another level:
# the rows a per-period anchor would read.
OWN_RATE = 0.14
OWN_FIXINGS = tuple(
    back(add_months(FIRST_RESET, 6 * k), LAG)
    for k in range(1, 10)
    if back(add_months(FIRST_RESET, 6 * k), LAG) < VALUATION
)


def rule(**kw: object) -> FloatingCouponRule:
    args: dict[str, object] = {
        "index": "kbd",
        "averaging": "single_fixing",
        "margin": MARGIN,
        "coupon_day_count": ACT365,
        "rate_day_count": ACT365,
        "rate_rounding_decimals": 2,
        "amount_rounding_decimals": 2,
        "fixing_date": FixingDateRule("accrual_start", LAG, "business_days"),
        "index_tenor_years": TENOR,
    }
    args.update(kw)
    return FloatingCouponRule(**args)  # type: ignore[arg-type]


def history(calendar: BusinessCalendar = CALENDAR, **kw: object) -> FixingHistory:
    return FixingHistory(
        "kbd",
        VALUATION,
        ((PAST_FIXING, PAST_RATE), *((day, OWN_RATE) for day in OWN_FIXINGS)),
        calendar,
        "test",
        tenor_years=TENOR,
        **kw,  # type: ignore[arg-type]
    )


def reset(cycle: int) -> date:
    return add_months(FIRST_RESET, 12 * 5 * cycle)


def periods(*, anchored: bool = True) -> tuple[FloatingCouponPeriod, ...]:
    out = []
    for k in range(CYCLES * PER_CYCLE):
        start, end = add_months(FIRST_RESET, 6 * k), add_months(FIRST_RESET, 6 * (k + 1))
        anchor = reset(k // PER_CYCLE) if anchored else None
        out.append(FloatingCouponPeriod(start, end, end, 1000.0, fixing_anchor=anchor))
    return tuple(out)


def leg(*, anchored: bool = True) -> FloatingLeg:
    return FloatingLeg(rule(), periods(anchored=anchored), history())


def cycle_positions(cycle: int) -> range:
    return range(PER_CYCLE * cycle, PER_CYCLE * (cycle + 1))


def df_forward(fixing: date) -> float:
    """The documented projection: ``exp(ln(DF(fix) / DF(fix + T)) / T) - 1``."""
    end = fixing + timedelta(days=round(365 * TENOR))
    return math.exp(math.log(KBD.discount_factor(fixing) / KBD.discount_factor(end)) / TENOR) - 1


def zcyc_forward(fixing: date) -> float:
    """``(G(t + T)(t + T) - G(t) t) / T`` straight from the ZCYC formula."""
    t = (fixing - VALUATION).days / 365.0
    total = kbd_zero_rate_bp(PARAMS, t + TENOR) * (t + TENOR) - kbd_zero_rate_bp(PARAMS, t) * t
    return math.expm1(total / TENOR / 1e4)


def test_every_period_of_a_cycle_takes_the_cycle_fixing() -> None:
    reset_leg, plain = leg(), leg(anchored=False)
    for cycle in (1, 2):
        fixing = back(reset(cycle), LAG)
        assert fixing > VALUATION
        rates = {reset_leg.coupon(i, KBD).index_rate for i in cycle_positions(cycle)}
        assert len(rates) == 1
        (rate,) = rates
        assert rate is not None
        # (b) the forward 5Y yield at the reset fixing from the curve's discount factors,
        # and the ZCYC formula itself within the materialised KBD's 0.02 bp.
        assert rate == pytest.approx(df_forward(fixing), rel=1e-12, abs=1e-14)
        assert rate == pytest.approx(zcyc_forward(fixing), abs=2e-6)
        dates = {reset_leg.projection_dates(i) for i in cycle_positions(cycle)}
        assert dates == {(fixing, fixing + timedelta(days=round(365 * TENOR)))}
        for i in cycle_positions(cycle):
            coupon = reset_leg.coupon(i, KBD)
            period = reset_leg.periods[i]
            days = (period.accrual_end - period.accrual_start).days
            assert coupon.source == "projected"
            assert coupon.coupon_rate == pytest.approx(rate + MARGIN, rel=1e-15)
            assert coupon.amount_rub == pytest.approx(1000.0 * (rate + MARGIN) * days / 365)
        # The first period of a cycle fixes as before; the later ones no longer on their own start.
        first = cycle_positions(cycle)[0]
        assert plain.coupon(first, KBD).index_rate == rate
        assert plain.coupon(first + 1, KBD).index_rate != pytest.approx(rate, abs=1e-6)


def test_a_cycle_fixed_in_the_past_comes_from_history() -> None:
    reset_leg = leg()
    remaining = reset_leg.remaining(VALUATION)
    in_first = [i for i in remaining if i < PER_CYCLE]
    assert len(in_first) == 7  # 2026-06-16 .. 2029-12-16
    for i in in_first:
        coupon = reset_leg.coupon(i, None)
        period = reset_leg.periods[i]
        days = (period.accrual_end - period.accrual_start).days
        assert coupon.source == "fixed_by_history"
        assert coupon.index_rate == PAST_RATE
        assert coupon.coupon_rate == pytest.approx(0.1825, abs=1e-15)
        assert coupon.amount_rub == round(1000.0 * 0.1825 * days / 365, 2)
    # Known coupons need no curve; a later cycle does.
    with pytest.raises(ValueError, match="projection curve is required"):
        reset_leg.coupon(PER_CYCLE, None)
    # Without the anchor a period of the first cycle reads the fixing before its own start.
    plain = leg(anchored=False)
    assert plain.coupon(in_first[0], None).index_rate == OWN_RATE
    # The coupon in progress is known in advance: its accrued is pro rata on the amount.
    current = in_first[0]
    period = reset_leg.periods[current]
    assert period.accrual_start < VALUATION < period.accrual_end
    amount = reset_leg.coupon(current, None).amount_rub
    elapsed = (VALUATION - period.accrual_start).days
    length = (period.accrual_end - period.accrual_start).days
    assert reset_leg.accrued_interest(VALUATION) == round(amount * elapsed / length, 2)


def test_published_coupons_still_override() -> None:
    published = list(periods())
    for i in (12, 13):
        published[i] = replace(published[i], published_coupon_rub=101.23)
    published_leg = FloatingLeg(rule(), tuple(published), history())
    base = leg()
    for i in cycle_positions(1):
        coupon = published_leg.coupon(i, KBD)
        if i in (12, 13):
            assert coupon.source == "published" and coupon.amount_rub == 101.23
            assert published_leg.projection_dates(i) == ()
        else:
            assert coupon == base.coupon(i, KBD)


def _request(floating: FloatingLeg) -> BondRiskRequest:
    return BondRiskRequest(
        cashflows=(BondCashflow(floating.periods[-1].payment_date, 1000.0),),
        settlement_date=VALUATION,
        curve=RUONIA,
        spread_convention=ZSpreadConvention(compounding=ANNUAL),
        spread_kind="credit",
        spread_bp=250.0,
        floating_leg=floating,
        projection_curve=KBD,
        quantity=1000.0,
    )


def _projection(result: TwoCurveBondRiskResult) -> dict[str, float]:
    return dict(zip(RISK_BUCKETS, (b.dv01_rub for b in result.projection_buckets), strict=True))


def test_projection_risk_sits_at_the_reset_fixings() -> None:
    anchored = BondRiskEngine(_request(leg())).risk(difference="central")
    plain = BondRiskEngine(_request(leg(anchored=False))).risk(difference="central")
    assert isinstance(anchored, TwoCurveBondRiskResult)
    assert isinstance(plain, TwoCurveBondRiskResult)
    reset_risk, plain_risk = _projection(anchored), _projection(plain)
    # Cycle fixings in 2029-12 and 2034-12, read with fixing + 5Y: nothing before 3Y,
    # short at the 2029 fixing, long at the forward tenors. The per-period anchors spread it.
    assert all(reset_risk[b] == 0.0 for b in ("1M", "3M", "6M", "1Y", "2Y", "20Y+"))
    assert reset_risk["3Y"] < 0.0 < min(reset_risk[b] for b in ("7Y", "10Y", "15Y"))
    assert plain_risk["1Y"] < 0.0 and plain_risk["20Y+"] > 0.0
    differences = [abs(reset_risk[b] - plain_risk[b]) for b in RISK_BUCKETS]
    assert max(differences) > 0.1 * max(abs(v) for v in plain_risk.values())
    assert anchored.pv_rub != plain.pv_rub


def _instrument(*, anchored: bool = True) -> Instrument:
    terms = periods(anchored=anchored)
    return Instrument(
        "FRN-KBD-RESET",
        ProductType.FLOATING_BOND,
        "RUB",
        1000.0,
        FloatingCouponLeg(rule(), terms, "KBD_5Y", "RUB_KBD"),
        PrincipalLeg((Redemption(terms[-1].payment_date, 1000.0),)),
        "MOEX_STOCK",
    )


def _portfolio_row(instrument: Instrument):  # type: ignore[no-untyped-def]
    fixings = history(PORTFOLIO_CALENDAR, known_at_utc=KNOWN)
    market = snapshot(
        curves=(
            CurveEntry("RUB_KBD", "rub_ofz_kbd_v2", PARAMS),
            CurveEntry("RUB_OIS", "rub_ruonia_ois_legacy_v1", OIS),
        ),
        fixings=(FixingEntry("KBD_5Y", fixings),),
        quotes=(BondPriceObservation(instrument.instrument_id, 104.0, "waprice"),),
    )
    result = value_portfolio(
        run_id="kbd-reset",
        as_of=AS_OF,
        snapshot=market,
        instruments=(instrument,),
        positions=(
            Position(
                "p-reset",
                instrument.instrument_id,
                1000.0,
                "CREDIT",
                AccountingCategory.TRADING,
                "RUB",
            ),
        ),
        policy=ValuationPolicy("RUB_OIS", include_second_order=False),
    )
    assert result.status == "complete", result.coverage.rejections
    (row,) = result.positions
    first = row.factor_risk.first_order_by_factor()
    return row, {b: first[f"IR|RUB_KBD|{b}"] for b in RISK_BUCKETS}


def test_portfolio_valuation_uses_the_reset_fixings() -> None:
    assert test_portfolio.BUSINESS_DATE == VALUATION
    row, projection = _portfolio_row(_instrument())
    plain_row, plain_projection = _portfolio_row(_instrument(anchored=False))
    assert sum(abs(v) for v in projection.values()) > 0.0
    assert any(
        abs(projection[b] - plain_projection[b]) > 1e-3 * max(map(abs, plain_projection.values()))
        for b in RISK_BUCKETS
    )
    assert row.factor_risk.first_order != plain_row.factor_risk.first_order


def test_fixing_anchor_round_trips_and_enters_the_identity() -> None:
    period = periods()[12]
    assert to_dict(period)["fixing_anchor"] == reset(1).isoformat()
    assert from_dict(FloatingCouponPeriod, to_dict(period)) == period
    plain = periods(anchored=False)[12]
    assert "fixing_anchor" not in to_dict(plain)
    assert from_dict(FloatingCouponPeriod, to_dict(plain)) == plain
    instrument = _instrument()
    assert from_dict(Instrument, to_dict(instrument)) == instrument
    assert leg().identity != leg(anchored=False).identity
    assert "fixing_anchor" not in str(leg(anchored=False).identity["periods"])
    # A period anchored on its own start is the plain in-advance rule, but not the same bytes.
    own = replace(plain, fixing_anchor=plain.accrual_start)
    own_leg = FloatingLeg(rule(), (own,), history())
    plain_leg = FloatingLeg(rule(), (plain,), history())
    assert own_leg.coupon(0, KBD) == plain_leg.coupon(0, KBD)
    assert own_leg.identity != plain_leg.identity


def test_validation() -> None:
    start, end = date(2027, 6, 16), date(2027, 12, 16)
    with pytest.raises(ValueError, match="in advance"):
        FloatingCouponPeriod(start, end, end, 1000.0, fixing_anchor=start + timedelta(days=1))
    with pytest.raises(ValueError, match="in advance"):
        FloatingCouponPeriod(start, end, end, 1000.0, fixing_anchor=end)
    with pytest.raises(TypeError, match="fixing_anchor"):
        FloatingCouponPeriod(start, end, end, 1000.0, fixing_anchor="2027-06-16")  # type: ignore[arg-type]
    anchored = FloatingCouponPeriod(start, end, end, 1000.0, fixing_anchor=date(2024, 12, 16))
    ruonia = FixingHistory("ruonia", VALUATION, (), CALENDAR, "r")
    compounded = FloatingCouponRule("ruonia", "compounded", 0.01, ACT365, ACT365)
    with pytest.raises(InvalidTerms, match="single_fixing rule"):
        FloatingLeg(compounded, (anchored,), ruonia)
    with pytest.raises(InvalidTerms, match="single_fixing rule"):
        FloatingCouponLeg(compounded, (anchored,), "RUONIA", "RUB_OIS")
    # A single fixing in arrears takes the anchor too: it is then fixed in advance.
    arrears = rule(fixing_date=FixingDateRule("accrual_end", LAG, "business_days"))
    in_arrears = FloatingLeg(arrears, (replace(anchored, fixing_anchor=None),), history())
    moved = FloatingLeg(arrears, (anchored,), history())
    assert in_arrears.coupon(0, KBD).index_rate == pytest.approx(df_forward(back(end, LAG)))
    assert moved.coupon(0, None).index_rate == PAST_RATE
