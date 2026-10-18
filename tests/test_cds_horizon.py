"""CDS horizon: carry, rolldown and theta of the ISDA standard model contract."""

from __future__ import annotations

import math
from dataclasses import fields
from datetime import date, timedelta

import pytest

from rub_fixed_income import (
    BusinessCalendar,
    CurveBootstrapRequest,
    DiscountCurve,
    InvalidTerms,
    OisQuote,
    ShockedCurveCache,
    forward_curve,
    roll_curve,
)
from rub_fixed_income.cds import (
    CDS_HORIZON_MODEL_VERSION,
    CdsContract,
    CdsConvention,
    CdsCurveCache,
    CdsCurveRequest,
    CdsHorizonValuation,
    CdsRiskEngine,
    CdsRiskRequest,
    CdsSide,
    CdsSpreadQuote,
    CdsUpfrontQuote,
    HazardCurve,
    cds_horizon,
    cds_trade_dates,
    forward_hazard_curve,
    roll_hazard_curve,
    standard_cds_maturity,
    value_cds,
)
from rub_fixed_income.dates import add_months

WEEKENDS = BusinessCalendar(version="weekends-only")
ISDA = CdsConvention.isda_standard(WEEKENDS)
VAL = date(2026, 10, 15)
MATURITY = standard_cds_maturity(VAL, 60, roll="semiannual")  # 20 Dec 2031
MATURITIES = tuple(standard_cds_maturity(VAL, 12 * y, roll="semiannual") for y in (1, 3, 5, 7, 10))
# The first premium is paid on 21 Dec 2026 (20 Dec is a Sunday): 21 Sep to 21 Dec.
FIRST_PAYMENT = date(2026, 12, 21)
FIRST_COUPON_FRACTION = 91 / 360


def _contract(side: CdsSide = CdsSide.BUYER, coupon: float = 0.01) -> CdsContract:
    accrual_start = cds_trade_dates(VAL, ISDA, maturity_date=MATURITY).accrual_start_date
    return CdsContract("CDS-1", "ENTITY", "USD", side, 1e7, coupon, accrual_start, MATURITY, ISDA)


def _quotes() -> tuple[CdsSpreadQuote | CdsUpfrontQuote, ...]:
    return (
        CdsSpreadQuote(MATURITIES[0], 0.008),
        CdsSpreadQuote(MATURITIES[1], 0.012),
        CdsUpfrontQuote(MATURITIES[2], 0.01, 0.035),
        CdsSpreadQuote(MATURITIES[3], 0.022),
        CdsUpfrontQuote(MATURITIES[4], 0.05, -0.12),
    )


def _ois() -> CurveBootstrapRequest:
    return CurveBootstrapRequest(
        VAL,
        tuple(OisQuote(f"{y}Y", add_months(VAL, 12 * y), 0.14 + 0.002 * y) for y in range(1, 11)),
    )


def _request(
    side: CdsSide = CdsSide.BUYER,
    *,
    coupon: float = 0.01,
    discount: DiscountCurve | CurveBootstrapRequest | None = None,
    quotes: tuple[CdsSpreadQuote | CdsUpfrontQuote, ...] | None = None,
) -> CdsRiskRequest:
    return CdsRiskRequest(
        _contract(side, coupon),
        VAL,
        DiscountCurve.flat(VAL, 0.12) if discount is None else discount,
        CdsCurveRequest(VAL, _quotes() if quotes is None else quotes, 0.4, ISDA),
    )


def _assert_identities(result: CdsHorizonValuation) -> None:
    assert result.carry == pytest.approx(result.pv_forward + result.received - result.pv_start)
    assert result.rolldown == pytest.approx(result.pv_rolled - result.pv_forward)
    assert result.horizon_pnl == pytest.approx(result.pv_rolled + result.received - result.pv_start)
    assert result.horizon_pnl == pytest.approx(result.carry + result.rolldown)


def test_horizon_identities_and_forward_and_rolled_values() -> None:
    request = _request(discount=_ois())
    engine = CdsRiskEngine(request)
    horizon = date(2027, 4, 15)
    result = engine.horizon(horizon)
    assert result.model_version == CDS_HORIZON_MODEL_VERSION
    assert (result.base_id, result.contract_id, result.currency, result.side) == (
        engine.base_id,
        "CDS-1",
        "USD",
        CdsSide.BUYER,
    )
    assert (result.valuation_date, result.horizon_date) == (VAL, horizon)
    assert result.pv_start == engine.base_pv
    assert result.accrued_start == engine.valuation.accrued_premium
    _assert_identities(result)
    discount = engine._discount
    forward = value_cds(
        request.contract,
        valuation_date=horizon,
        discount_curve=forward_curve(discount, horizon),
        hazard_curve=forward_hazard_curve(engine.hazard_curve, horizon),
        recovery_rate=0.4,
    )
    rolled = value_cds(
        request.contract,
        valuation_date=horizon,
        discount_curve=roll_curve(discount, horizon),
        hazard_curve=roll_hazard_curve(engine.hazard_curve, horizon),
        recovery_rate=0.4,
    )
    assert result.pv_forward == forward.pv
    assert result.pv_rolled == rolled.pv
    assert result.accrued_horizon == rolled.accrued_premium == forward.accrued_premium
    # Two coupons paid in the window: 21 Dec 2026 and 22 Mar 2027 (20 Mar is a Saturday).
    expected = FIRST_COUPON_FRACTION + 91 / 360
    assert result.received == pytest.approx(-1e7 * 0.01 * expected, rel=1e-15)
    # The free function on shared caches gives the same numbers.
    caches = (ShockedCurveCache(), CdsCurveCache())
    assert cds_horizon(request, horizon_date=horizon) == result
    assert (
        cds_horizon(request, horizon_date=horizon, curve_cache=caches[0], cds_cache=caches[1])
        == result
    )
    with pytest.raises(AttributeError):
        result.carry = 0.0  # type: ignore[misc]


def test_forward_hazard_survival_is_the_conditional_survival() -> None:
    curve = HazardCurve(VAL, tuple(zip(MATURITIES, (0.01, 0.025, 0.03, 0.045, 0.02))))
    last = MATURITIES[-1]
    for horizon in (
        VAL + timedelta(days=1),
        date(2027, 4, 15),
        MATURITIES[1],
        MATURITIES[1] + timedelta(days=1),
        last - timedelta(days=1),
        last,
        last + timedelta(days=400),
    ):
        view = forward_hazard_curve(curve, horizon)
        assert view.curve_date == horizon
        assert view.day_count == curve.day_count
        anchor = curve.survival_probability(horizon)
        for days in (0, 1, 30, 365, 1000, 2500, 5000):
            target = horizon + timedelta(days=days)
            expected = curve.survival_probability(target) / anchor
            assert view.survival_probability(target) == pytest.approx(expected, rel=1e-13)
        assert view.hazard_rate(horizon + timedelta(days=1)) == curve.hazard_rate(
            horizon + timedelta(days=1)
        )
    assert forward_hazard_curve(curve, VAL) is curve
    # The rolled curve keeps the shape in calendar days.
    shift = timedelta(days=200)
    rolled = roll_hazard_curve(curve, VAL + shift)
    assert rolled.curve_date == VAL + shift
    assert rolled.hazard_rates == curve.hazard_rates
    assert rolled.node_dates == tuple(d + shift for d in MATURITIES)
    for days in (1, 365, 3000):
        target = VAL + timedelta(days=days)
        assert rolled.survival_probability(target + shift) == pytest.approx(
            curve.survival_probability(target), rel=1e-14
        )
    assert roll_hazard_curve(curve, VAL) == curve
    for helper in (forward_hazard_curve, roll_hazard_curve):
        with pytest.raises(InvalidTerms):
            helper(curve, VAL - timedelta(days=1))


def test_flat_hazard_and_flat_discount_have_no_rolldown() -> None:
    for side in CdsSide:
        engine = CdsRiskEngine(_request(side, quotes=(CdsSpreadQuote(MATURITIES[-1], 0.015),)))
        for horizon in (date(2026, 11, 2), date(2027, 4, 15), date(2029, 1, 31)):
            result = engine.horizon(horizon)
            _assert_identities(result)
            assert result.rolldown == pytest.approx(0.0, abs=1e-6)
            assert result.pv_rolled == pytest.approx(result.pv_forward, abs=1e-6)


def test_received_premium_has_the_side_sign_and_sides_mirror() -> None:
    buyer = CdsRiskEngine(_request(CdsSide.BUYER))
    seller = CdsRiskEngine(_request(CdsSide.SELLER))
    coupon = 1e7 * 0.01 * FIRST_COUPON_FRACTION
    before = buyer.horizon(FIRST_PAYMENT - timedelta(days=5))
    assert before.received == 0.0
    assert math.copysign(1.0, before.received) == 1.0  # no negative zero
    for horizon in (FIRST_PAYMENT, date(2027, 1, 15)):
        b, s = buyer.horizon(horizon), seller.horizon(horizon)
        assert b.received == pytest.approx(-coupon, rel=1e-15)
        assert s.received == pytest.approx(coupon, rel=1e-15)
        for f in fields(CdsHorizonValuation):
            left, right = getattr(b, f.name), getattr(s, f.name)
            if isinstance(left, float):
                assert left == pytest.approx(-right, rel=1e-12, abs=1e-9), f.name
    # A coupon paid on the horizon's step-in day has left the horizon value: received.
    edge = buyer.horizon(FIRST_PAYMENT - timedelta(days=1))
    assert edge.received == pytest.approx(-coupon, rel=1e-15)
    _assert_identities(edge)
    # Seller: premium income and decaying protection, positive carry on an upward curve.
    assert seller.horizon(date(2027, 1, 15)).carry > 0.0


def test_horizon_at_or_after_maturity_receives_every_remaining_coupon() -> None:
    engine = CdsRiskEngine(_request(CdsSide.SELLER))
    total = 1e7 * 0.01 * math.fsum(p.year_fraction for p in engine._dates.periods)
    for horizon in (MATURITY, date(2032, 3, 1)):
        result = engine.horizon(horizon)
        assert result.received == pytest.approx(total, rel=1e-15)
        assert result.pv_forward == result.pv_rolled == result.accrued_horizon == 0.0
        assert result.rolldown == 0.0
        assert result.carry == pytest.approx(total - result.pv_start)
        _assert_identities(result)
    # The day before maturity the step-in is the maturity: still valued, last coupon live.
    live = engine.horizon(MATURITY - timedelta(days=1))
    assert live.received < total
    assert live.pv_rolled != 0.0
    _assert_identities(live)


def test_seller_rolls_down_an_upward_credit_curve() -> None:
    upward = tuple(
        CdsSpreadQuote(m, s) for m, s in zip(MATURITIES, (0.004, 0.01, 0.016, 0.02, 0.024))
    )
    for horizon in (date(2027, 4, 15), date(2027, 10, 15)):
        seller = CdsRiskEngine(_request(CdsSide.SELLER, quotes=upward)).horizon(horizon)
        buyer = CdsRiskEngine(_request(CdsSide.BUYER, quotes=upward)).horizon(horizon)
        assert seller.rolldown > 0.0 > buyer.rolldown
        assert seller.rolldown == pytest.approx(-buyer.rolldown, rel=1e-12)


def test_horizon_validation() -> None:
    engine = CdsRiskEngine(_request())
    for bad in (VAL, VAL - timedelta(days=1)):
        with pytest.raises(InvalidTerms):
            engine.horizon(bad)
        with pytest.raises(InvalidTerms):
            cds_horizon(_request(), horizon_date=bad)
    with pytest.raises(InvalidTerms):
        engine.horizon("2027-01-15")  # type: ignore[arg-type]
