"""Carry, rolldown and theta of rate futures and OFZ basket futures (BACKLOG #101)."""

from __future__ import annotations

import math
from datetime import date, timedelta

import pytest
from test_ofz_futures import CONTRACT, DELIVERY, LONG, SHORT, SPOT
from test_ofz_futures import VALUATION as OFZ_VALUATION
from test_ofz_futures import engine as ofz_engine
from test_rate_futures import VALUATION, fixings, flat, index_engine, ruon_october

from rub_fixed_income import (
    OFZ_FUTURES_HORIZON_MODEL_VERSION,
    RATE_FUTURES_HORIZON_MODEL_VERSION,
    CurveBootstrapRequest,
    DeliverableBond,
    DiscountCurve,
    InvalidTerms,
    KeyRateForwardCurve,
    KeyRateForwardSegment,
    KeyRateFuturesContract,
    OfzFuturesHorizonValuation,
    OfzFuturesRiskEngine,
    OfzFuturesRiskRequest,
    OisQuote,
    RateFuturesHorizonValuation,
    RateFuturesRiskEngine,
    RateFuturesRiskRequest,
    ofz_futures_horizon,
    rate_futures_horizon,
)

H = date(2026, 10, 21)


def upward(curve_date: date = VALUATION, start: float = 0.12, step: float = 0.002) -> DiscountCurve:
    """Weekly nodes with zero rates rising ``step`` per week, then 1Y and 2Y."""
    days = [7 * k for k in range(1, 13)] + [365, 730]
    nodes = []
    for i, d in enumerate(days):
        zero = start + step * min(i + 1, 14)
        nodes.append((curve_date + timedelta(days=d), math.exp(-zero * d / 365.0)))
    return DiscountCurve(curve_date, tuple(nodes))


def ruon(
    curve: DiscountCurve | CurveBootstrapRequest, quantity: float = 3.0
) -> RateFuturesRiskEngine:
    return RateFuturesRiskEngine(
        RateFuturesRiskRequest(
            ruon_october(), curve, fixings=fixings(), basis_bp=4.0, quantity=quantity
        )
    )


def key_rate_curve() -> KeyRateForwardCurve:
    """Easing path: 14 % until 26 Oct, then 13.75 %, then 13 % from 21 Dec."""
    return KeyRateForwardCurve(
        VALUATION,
        0.14,
        date(2026, 10, 26),
        (
            KeyRateForwardSegment(date(2026, 10, 26), date(2026, 12, 21), 0.1375, "2M"),
            KeyRateForwardSegment(date(2026, 12, 21), date(2027, 3, 22), 0.13, "5M"),
        ),
    )


def key_rate(contract: KeyRateFuturesContract, quantity: float = 1.0) -> RateFuturesRiskEngine:
    return RateFuturesRiskEngine(
        RateFuturesRiskRequest(contract, key_rate_curve(), basis_bp=1.0, quantity=quantity)
    )


DECEMBER = KeyRateFuturesContract("KEYRATE-12.26", date(2026, 12, 18), date(2026, 12, 21))


def assert_identities(
    result: RateFuturesHorizonValuation | OfzFuturesHorizonValuation, scale: float
) -> None:
    assert result.pv_start_rub == 0.0 and result.received_rub == 0.0
    assert result.carry_rub == pytest.approx(scale * (result.price_forward - result.price_start))
    assert result.rolldown_rub == pytest.approx(
        scale * (result.price_rolled - result.price_forward)
    )
    assert result.horizon_pnl_rub == pytest.approx(result.carry_rub + result.rolldown_rub, abs=1e-9)


# --- rate futures ----------------------------------------------------------------------


def test_ruon_carry_is_zero_and_rolldown_follows_the_slope() -> None:
    engine = ruon(upward())
    result = engine.horizon(H)
    assert result.model_version == RATE_FUTURES_HORIZON_MODEL_VERSION
    assert (result.base_id, result.contract_id) == (engine.base_id, "RUON-10.26")
    assert (result.valuation_date, result.horizon_date, result.quantity) == (VALUATION, H, 3.0)
    assert result.price_start == engine.base_price
    assert result.price_forward == pytest.approx(engine.base_price, rel=1e-14)
    assert result.carry_rub == pytest.approx(0.0, abs=1e-8)
    # An upward curve rolls down: later overnight forwards are lower, the price higher.
    assert result.price_rolled > result.price_forward
    assert result.rolldown_rub > 0.0
    assert_identities(result, 3.0 * ruon_october().margin_per_point_rub)
    assert rate_futures_horizon(engine.request, horizon_date=H) == result


def test_ruon_rolled_price_reads_the_rolled_forwards_after_the_horizon() -> None:
    curve = upward()
    engine = ruon(curve)
    contract = ruon_october()
    fixed = 0.14 * (VALUATION - contract.period_start).days
    shift = H - VALUATION
    total = fixed
    for day in (contract.period_start + timedelta(days=i) for i in range(contract.period_days)):
        if day < VALUATION:
            continue
        observation = day
        while observation.weekday() >= 5:
            observation -= timedelta(days=1)
        following = observation + timedelta(days=1)
        while following.weekday() >= 5:
            following += timedelta(days=1)
        tau = (following - observation).days / 365.0
        if observation < H:
            ratio = curve.discount_factor(observation) / curve.discount_factor(following)
        else:
            ratio = curve.discount_factor(observation - shift) / curve.discount_factor(
                following - shift
            )
        total += (ratio - 1.0) / tau + 4e-4
    expected = 100.0 - 100.0 * total / contract.period_days
    assert engine.horizon(H).price_rolled == pytest.approx(expected, rel=1e-13)


def test_index_and_key_rate_carry_is_zero_and_rolldown_has_the_curve_sign() -> None:
    index = index_engine(upward(), futures_price=4.58)
    result = index.horizon(date(2026, 11, 2))
    assert result.price_forward == pytest.approx(index.base_price, rel=1e-14)
    assert result.price_rolled < result.price_forward  # lower rolled forwards, less growth
    assert_identities(result, 10_000.0)
    key = key_rate(DECEMBER, quantity=-2)
    decided = key.horizon(date(2026, 11, 13))
    assert decided.price_forward == key.base_price == pytest.approx(13.01)
    # Rolled 30 days: the 21 Dec rate is today's 21 Nov rate, 13.75 % (an easing path).
    assert decided.price_rolled == pytest.approx(13.76)
    assert decided.rolldown_rub == pytest.approx(-2 * 0.75 * 1000.0)
    assert_identities(decided, -2 * 1000.0)


def test_flat_curve_has_no_rolldown() -> None:
    for engine in (ruon(flat(0.12)), index_engine(flat(0.13), basis_bp=-20.0)):
        result = engine.horizon(date(2026, 10, 28))
        assert result.price_forward == pytest.approx(engine.base_price, rel=1e-14)
        assert result.rolldown_rub == pytest.approx(0.0, abs=1e-8)
    flat_key = KeyRateForwardCurve(
        VALUATION,
        0.14,
        date(2026, 10, 26),
        (KeyRateForwardSegment(date(2026, 10, 26), date(2027, 3, 22), 0.14, "5M"),),
    )
    engine = RateFuturesRiskEngine(RateFuturesRiskRequest(DECEMBER, flat_key, basis_bp=0.0))
    assert engine.horizon(date(2026, 11, 13)).rolldown_rub == 0.0


def test_contract_expiring_inside_the_horizon_settles_on_the_realised_path() -> None:
    contract = ruon_october()
    engine = ruon(upward())
    after = engine.horizon(contract.last_trading_day + timedelta(days=3))
    assert after.price_forward == after.price_rolled == engine.base_price
    assert after.carry_rub == after.rolldown_rub == 0.0
    index = index_engine(upward(), basis_bp=0.0)
    on_expiry = index.horizon(date(2026, 12, 1))
    assert on_expiry.price_forward == on_expiry.price_rolled
    assert on_expiry.price_forward == pytest.approx(index.base_price, rel=1e-14)
    key = key_rate(DECEMBER)
    for horizon in (DECEMBER.meeting_date, DECEMBER.effective_date + timedelta(days=5)):
        settled = key.horizon(horizon)
        assert settled.price_forward == settled.price_rolled == key.base_price
    # A rate decided before the curve's current interval ends does not move either.
    early = KeyRateFuturesContract("KEYRATE-X", date(2026, 10, 16), date(2026, 10, 19))
    decided = key_rate(early).horizon(date(2026, 10, 15))
    assert decided.price_rolled == decided.price_start == pytest.approx(14.01)


def test_ois_request_uses_the_built_base_curve_and_quantity_scales() -> None:
    quotes = (
        OisQuote("1M", date(2026, 11, 14), 0.139),
        OisQuote("3M", date(2027, 1, 14), 0.137),
        OisQuote("6M", date(2027, 4, 14), 0.134),
    )
    engine = ruon(CurveBootstrapRequest(VALUATION, quotes), quantity=1.0)
    one = engine.horizon(H)
    assert one.price_forward == pytest.approx(engine.base_price, rel=1e-14)
    twin = engine.for_quantity(-10.0)
    ten = twin.horizon(H)
    assert (ten.price_start, ten.price_forward, ten.price_rolled) == (
        one.price_start,
        one.price_forward,
        one.price_rolled,
    )
    assert ten.quantity == -10.0 and ten.base_id == twin.base_id != one.base_id
    assert ten.rolldown_rub == pytest.approx(-10.0 * one.rolldown_rub)
    assert ten.horizon_pnl_rub == pytest.approx(-10.0 * one.horizon_pnl_rub)
    key = key_rate(DECEMBER)
    assert key.for_quantity(5).horizon(date(2026, 11, 13)).rolldown_rub == pytest.approx(
        5 * key.horizon(date(2026, 11, 13)).rolldown_rub
    )


def test_rate_futures_horizon_must_follow_the_valuation_date() -> None:
    engine = ruon(flat(0.12))
    for horizon in (VALUATION, VALUATION - timedelta(days=1)):
        with pytest.raises(InvalidTerms):
            engine.horizon(horizon)
    with pytest.raises(InvalidTerms):
        key_rate(DECEMBER).horizon(VALUATION)


# --- OFZ futures -----------------------------------------------------------------------

OFZ_H = date(2026, 11, 13)
OFZ_SH = date(2026, 11, 16)


def test_ofz_carry_on_flat_curves_matches_a_hand_computation() -> None:
    engine = ofz_engine(((LONG, 1.1, 104.0),), quantity=4)
    result = engine.horizon(OFZ_H, horizon_spot_settlement_date=OFZ_SH)
    assert result.model_version == OFZ_FUTURES_HORIZON_MODEL_VERSION
    assert (result.valuation_date, result.horizon_date) == (OFZ_VALUATION, OFZ_H)
    assert result.horizon_spot_settlement_date == OFZ_SH
    assert result.price_start == engine.base_price
    bond, funding = DiscountCurve.flat(OFZ_VALUATION, 0.14), DiscountCurve.flat(OFZ_VALUATION, 0.15)
    spread = engine.spreads[0]
    dirty = math.fsum(
        cf.amount
        * bond.discount_factor(cf.payment_date)
        / bond.discount_factor(OFZ_SH)
        * math.exp(-spread * (cf.payment_date - OFZ_SH).days / 365.0)
        for cf in LONG.cashflows(after=OFZ_SH)
    )
    forward = dirty * funding.discount_factor(OFZ_SH) / funding.discount_factor(DELIVERY)
    expected = 10 * (forward - LONG.accrued(DELIVERY)) / 1.1
    assert result.price_forward == pytest.approx(expected, rel=1e-12)
    # Flat curves roll onto themselves.
    assert result.price_rolled == pytest.approx(result.price_forward, rel=1e-14)
    # Funding (15 %) above the bond yield: the forward drifts down to delivery.
    assert result.carry_rub < 0.0
    assert_identities(result, 4.0)
    assert (
        ofz_futures_horizon(engine.request, horizon_date=OFZ_H, horizon_spot_settlement_date=OFZ_SH)
        == result
    )


def test_ofz_has_no_carry_when_funding_is_the_bond_curve() -> None:
    curve = DiscountCurve.flat(OFZ_VALUATION, 0.14)
    dirty = math.fsum(
        cf.amount * curve.discount_factor(cf.payment_date) / curve.discount_factor(SPOT)
        for cf in SHORT.cashflows(after=SPOT)
    )
    clean_pct = (dirty - SHORT.accrued(SPOT)) / 10.0
    engine = OfzFuturesRiskEngine(
        OfzFuturesRiskRequest(
            CONTRACT, (DeliverableBond(SHORT, 1.0, clean_pct),), SPOT, curve, curve, basis_bp=0.0
        )
    )
    # Coupon before delivery included: the forward price does not depend on the spot date.
    result = engine.horizon(date(2026, 11, 25), horizon_spot_settlement_date=date(2026, 11, 26))
    assert result.carry_rub == pytest.approx(0.0, abs=1e-5)
    assert result.rolldown_rub == pytest.approx(0.0, abs=1e-8)


def test_ofz_sloped_curves_roll_and_the_cheapest_bond_is_reported() -> None:
    bond_curve, funding_curve = upward(OFZ_VALUATION, 0.13), upward(OFZ_VALUATION, 0.14, 0.001)
    request = OfzFuturesRiskRequest(
        CONTRACT,
        (DeliverableBond(SHORT, 1.0, 98.0), DeliverableBond(LONG, 1.1, 104.0)),
        SPOT,
        bond_curve,
        funding_curve,
        basis_bp=5.0,
    )
    engine = OfzFuturesRiskEngine(request)
    result = engine.horizon(OFZ_H, horizon_spot_settlement_date=OFZ_SH)
    assert result.price_rolled != pytest.approx(result.price_forward, rel=1e-9)
    assert result.cheapest_start == engine.risk().cheapest_to_deliver
    assert_identities(result, 1.0)
    # SU_SHORT is cheapest today but carries better than SU_LONG: by the horizon it is not.
    switch = ofz_engine(((SHORT, 0.97, 96.8), (LONG, 1.1, 110.0)))
    later = switch.horizon(date(2026, 11, 27), horizon_spot_settlement_date=date(2026, 11, 30))
    assert later.cheapest_start == "SU_SHORT"
    assert later.cheapest_forward == later.cheapest_rolled == "SU_LONG"


def test_ofz_horizon_scales_with_quantity_and_validates_dates() -> None:
    engine = ofz_engine(futures_price=9500.0, basis_bp=None)
    one = engine.horizon(OFZ_H, horizon_spot_settlement_date=OFZ_SH)
    seven = engine.for_quantity(-7).horizon(OFZ_H, horizon_spot_settlement_date=OFZ_SH)
    assert seven.price_forward == one.price_forward and seven.quantity == -7.0
    assert seven.carry_rub == pytest.approx(-7 * one.carry_rub)
    assert seven.base_id != one.base_id
    with pytest.raises(InvalidTerms):
        engine.horizon(OFZ_VALUATION, horizon_spot_settlement_date=SPOT)
    with pytest.raises(InvalidTerms):
        engine.horizon(OFZ_H, horizon_spot_settlement_date=OFZ_H - timedelta(days=1))
    with pytest.raises(InvalidTerms, match="delivery"):
        engine.horizon(date(2026, 12, 4), horizon_spot_settlement_date=DELIVERY)
    with pytest.raises(InvalidTerms, match="delivery"):
        engine.horizon(DELIVERY, horizon_spot_settlement_date=DELIVERY + timedelta(days=1))
    late_spot = OfzFuturesRiskRequest(
        CONTRACT,
        (DeliverableBond(LONG, 1.1, 104.0),),
        date(2026, 10, 20),
        DiscountCurve.flat(OFZ_VALUATION, 0.14),
        DiscountCurve.flat(OFZ_VALUATION, 0.15),
        basis_bp=0.0,
    )
    with pytest.raises(InvalidTerms, match="today's spot"):
        OfzFuturesRiskEngine(late_spot).horizon(SPOT, horizon_spot_settlement_date=SPOT)
