from __future__ import annotations

import math
from dataclasses import FrozenInstanceError, replace
from datetime import date, timedelta

import pytest

from rub_fixed_income import (
    FX_FORWARD_HORIZON_MODEL_VERSION,
    FX_FUTURES_HORIZON_MODEL_VERSION,
    FX_HORIZONS,
    CurveBootstrapRequest,
    DiscountCurve,
    FxForward,
    FxForwardHorizonValuation,
    FxForwardLeg,
    FxForwardRiskEngine,
    FxForwardRiskRequest,
    FxFuturesContract,
    FxFuturesHorizonValuation,
    FxFuturesRiskEngine,
    FxFuturesRiskRequest,
    FxSwap,
    InvalidTerms,
    OisQuote,
    ShockedCurveCache,
    bootstrap_discount_curve,
    fx_forward_horizon,
    fx_futures_horizon,
)

C = date(2026, 1, 1)
R = 0.15  # RUB continuous zero rate
Q = 0.03  # CNY continuous zero rate
S = 12.5  # RUB per CNY
T = C + timedelta(days=365)
N = 1_000_000.0  # CNY bought forward
K = 13.9
RUB = DiscountCurve.flat(C, R)
CNY = DiscountCurve.flat(C, Q)
RULES = FX_HORIZONS


def days(n: int) -> date:
    return C + timedelta(days=n)


def node_curve(rates: tuple[tuple[int, float], ...]) -> DiscountCurve:
    return DiscountCurve(C, tuple((days(n), math.exp(-r * n / 365.0)) for n, r in rates))


# Upward RUB, downward CNY: the rolled curves differ from the forward-read ones.
RUB_NODES = node_curve(((30, 0.13), (180, 0.145), (365, 0.155), (730, 0.165)))
CNY_NODES = node_curve(((30, 0.035), (180, 0.03), (365, 0.025), (730, 0.02)))


def dfr(day: date) -> float:
    return math.exp(-R * (day - C).days / 365.0)


def dfq(day: date) -> float:
    return math.exp(-Q * (day - C).days / 365.0)


def forward(leg: FxForwardLeg | None = None, quote_units: float = 1.0) -> FxForward:
    return FxForward("FWD-1", "CNY", quote_units, FxForwardLeg(T, N, K) if leg is None else leg)


def swap() -> FxSwap:
    return FxSwap(
        "SWP-1", "CNY", 1.0, FxForwardLeg(days(7), N, 12.55), FxForwardLeg(days(200), -N, 13.2)
    )


def request(**changes: object) -> FxForwardRiskRequest:
    base = FxForwardRiskRequest(
        instrument=forward(),
        spot_rate=S,
        spot_value_date=C,
        rub_curve=RUB,
        foreign_curve=CNY,
        basis_bp=0.0,
    )
    return replace(base, **changes)  # type: ignore[arg-type]


def _check_identities(h: FxForwardHorizonValuation | FxFuturesHorizonValuation) -> None:
    assert h.carry_rub + h.rolldown_rub == pytest.approx(h.horizon_pnl_rub, rel=1e-12, abs=1e-6)
    if isinstance(h, FxForwardHorizonValuation):
        start, fwd, rolled = h.pv_start_rub, h.pv_forward_rub, h.pv_rolled_rub
    else:
        scale = h.quantity * 1.0  # the synthetic contract's multiplier is one
        start, fwd, rolled = (scale * p for p in (h.price_start, h.price_forward, h.price_rolled))
    assert h.carry_rub == pytest.approx(fwd + h.received_rub - start, rel=1e-12, abs=1e-6)
    assert h.rolldown_rub == pytest.approx(rolled - fwd, rel=1e-12, abs=1e-6)
    assert h.horizon_pnl_rub == pytest.approx(rolled + h.received_rub - start, rel=1e-12, abs=1e-6)


# --- FX forwards and swaps ------------------------------------------------------------


def test_single_leg_spot_held_and_forwards_realised_by_hand() -> None:
    engine = FxForwardRiskEngine(request())
    h, sh = days(91), days(92)
    held = engine.horizon(h, horizon_spot_value_date=sh)
    realised = engine.horizon(h, horizon_spot_value_date=sh, fx_horizon="forwards_realised")
    pv_start = N * (S * dfq(T) / dfr(T) - K) * dfr(T)
    assert held.pv_start_rub == pytest.approx(pv_start, rel=1e-13)
    # Spot held: the outright runs from today's spot at the horizon spot date.
    f_held = S * (dfq(T) / dfq(sh)) * (dfr(sh) / dfr(T))
    assert held.horizon_spot_rate == S
    assert held.pv_forward_rub == pytest.approx(N * (f_held - K) * dfr(T) / dfr(h), rel=1e-12)
    # Forwards realised: the horizon spot is today's outright for its value date.
    s_h = S * dfq(sh) / dfr(sh)
    f_realised = S * dfq(T) / dfr(T)
    assert realised.horizon_spot_rate == pytest.approx(s_h, rel=1e-14)
    assert realised.pv_forward_rub == pytest.approx(
        N * (f_realised - K) * dfr(T) / dfr(h), rel=1e-12
    )
    spot_effect = N * (s_h - S) * (dfq(T) / dfq(sh)) * (dfr(sh) / dfr(T)) * dfr(T) / dfr(h)
    assert realised.pv_forward_rub - held.pv_forward_rub == pytest.approx(spot_effect, rel=1e-9)
    assert realised.carry_rub - held.carry_rub == pytest.approx(spot_effect, rel=1e-9)
    for result in (held, realised):
        assert result.received_rub == 0.0
        assert result.rolldown_rub == pytest.approx(0.0, abs=1e-7)  # flat curves
        _check_identities(result)
    # Forwards realised on flat curves: the carry is the RUB accretion of today's PV.
    assert realised.carry_rub == pytest.approx(pv_start * (1.0 / dfr(h) - 1.0), rel=1e-10)


def test_forwards_realised_pv_forward_is_todays_pv_over_the_horizon_df() -> None:
    # Basis, a later spot date, quote units and a cash-settled leg fixed before the
    # horizon but paid after it: tau additivity keeps the identity exact.
    cash = FxForwardLeg(days(20), -2.5 * N, 1260.0, payment_date=days(45))
    trades = (
        swap(),
        forward(FxForwardLeg(days(500), 3 * N, 1310.0), quote_units=100.0),
        forward(cash, quote_units=100.0),
    )
    h, sh = days(30), days(32)
    for trade in trades:
        quote = S * trade.quote_units
        req = request(
            instrument=trade,
            spot_rate=quote,
            spot_value_date=days(1),
            rub_curve=RUB_NODES,
            foreign_curve=CNY_NODES,
            basis_bp=-35.0,
        )
        engine = FxForwardRiskEngine(req)
        legs = engine.leg_valuations()
        later = math.fsum(leg.pv_rub for leg in legs if leg.payment_date > h)
        result = engine.horizon(h, horizon_spot_value_date=sh, fx_horizon="forwards_realised")
        assert result.pv_forward_rub == pytest.approx(
            later / RUB_NODES.discount_factor(h), rel=1e-12
        )
        assert result.pv_start_rub == engine.base_pv_rub
        assert result.base_id == engine.base_id
        assert result.rolldown_rub != pytest.approx(0.0, abs=1.0)
        _check_identities(result)
        _check_identities(engine.horizon(h, horizon_spot_value_date=sh))


def test_flat_curves_have_no_rolldown() -> None:
    for trade in (forward(), swap()):
        engine = FxForwardRiskEngine(request(instrument=trade, basis_bp=40.0))
        for rule in RULES:
            for h in (days(3), days(30), days(190)):
                result = engine.horizon(
                    h, horizon_spot_value_date=h + timedelta(days=1), fx_horizon=rule
                )
                assert result.rolldown_rub == pytest.approx(0.0, abs=1e-7)
                assert result.pv_rolled_rub == pytest.approx(result.pv_forward_rub, rel=1e-13)


def test_horizon_past_every_leg_receives_everything() -> None:
    for trade in (forward(), swap()):
        engine = FxForwardRiskEngine(
            request(instrument=trade, rub_curve=RUB_NODES, foreign_curve=CNY_NODES)
        )
        h = days(400)
        legs = engine.leg_valuations()
        held = engine.horizon(h, horizon_spot_value_date=h)
        realised = engine.horizon(h, horizon_spot_value_date=h, fx_horizon="forwards_realised")
        assert held.received_rub == pytest.approx(
            math.fsum(leg.notional_foreign * (S - leg.contract_rate) for leg in legs), rel=1e-13
        )
        assert realised.received_rub == pytest.approx(
            math.fsum(
                leg.notional_foreign * (leg.forward_rate - leg.contract_rate) for leg in legs
            ),
            rel=1e-13,
        )
        for result in (held, realised):
            assert result.pv_forward_rub == 0.0
            assert result.pv_rolled_rub == 0.0
            assert result.rolldown_rub == 0.0
            assert result.carry_rub == pytest.approx(result.received_rub - result.pv_start_rub)
            _check_identities(result)


def test_fx_swap_near_leg_received_inside_the_horizon() -> None:
    trade = swap()
    req = request(instrument=trade, rub_curve=RUB_NODES, foreign_curve=CNY_NODES, basis_bp=15.0)
    far = replace(req, instrument=trade.far_leg_forward())
    h, sh = days(30), days(31)
    near_forward = FxForwardRiskEngine(req).leg_valuations()[0].forward_rate
    for rule in RULES:
        result = fx_forward_horizon(
            req, horizon_date=h, horizon_spot_value_date=sh, fx_horizon=rule
        )
        alone = fx_forward_horizon(far, horizon_date=h, horizon_spot_value_date=sh, fx_horizon=rule)
        fixing = S if rule == "spot_held" else near_forward
        assert result.received_rub == pytest.approx(N * (fixing - 12.55), rel=1e-13)
        assert alone.received_rub == 0.0
        # The far leg is all that remains at the horizon.
        assert result.pv_forward_rub == pytest.approx(alone.pv_forward_rub, rel=1e-13)
        assert result.pv_rolled_rub == pytest.approx(alone.pv_rolled_rub, rel=1e-13)
        assert result.fx_horizon == rule
        _check_identities(result)


def test_forward_horizon_on_a_bootstrap_request_uses_the_built_curve() -> None:
    ois = CurveBootstrapRequest(
        C,
        (
            OisQuote("3M", date(2026, 4, 1), 0.16),
            OisQuote("6M", date(2026, 7, 1), 0.155),
            OisQuote("1Y", date(2027, 1, 1), 0.15),
            OisQuote("2Y", date(2028, 1, 1), 0.14),
        ),
    )
    cache = ShockedCurveCache()
    req = request(rub_curve=ois, foreign_curve=CNY_NODES)
    engine = FxForwardRiskEngine(req, cache)
    h = days(60)
    result = fx_forward_horizon(
        req,
        horizon_date=h,
        horizon_spot_value_date=h,
        fx_horizon="forwards_realised",
        curve_cache=cache,
    )
    assert result == engine.horizon(h, horizon_spot_value_date=h, fx_horizon="forwards_realised")
    df_h = bootstrap_discount_curve(ois).discount_factor(h)
    assert result.pv_forward_rub == pytest.approx(engine.base_pv_rub / df_h, rel=1e-12)
    _check_identities(result)


def test_forward_horizon_result_contract() -> None:
    result = fx_forward_horizon(request(), horizon_date=days(10), horizon_spot_value_date=days(11))
    assert result.model_version == FX_FORWARD_HORIZON_MODEL_VERSION == "rub-fx-forward-horizon-v1"
    assert (result.trade_id, result.currency, result.valuation_date) == ("FWD-1", "CNY", C)
    assert (result.spot_rate, result.spot_value_date) == (S, C)
    assert result.horizon_spot_value_date == days(11)
    with pytest.raises(FrozenInstanceError):
        result.carry_rub = 0.0  # type: ignore[misc]


def test_forward_horizon_validation() -> None:
    engine = FxForwardRiskEngine(request(spot_value_date=days(2)))
    with pytest.raises(InvalidTerms, match="fx_horizon"):
        engine.horizon(days(10), horizon_spot_value_date=days(12), fx_horizon="sticky")  # type: ignore[arg-type]
    with pytest.raises(InvalidTerms, match="horizon date"):
        engine.horizon(C, horizon_spot_value_date=days(2))
    with pytest.raises(InvalidTerms, match="horizon date"):
        engine.horizon(C - timedelta(days=1), horizon_spot_value_date=days(2))
    with pytest.raises(InvalidTerms, match="precede the horizon date"):
        engine.horizon(days(10), horizon_spot_value_date=days(9))
    with pytest.raises(InvalidTerms, match="today's spot value date"):
        engine.horizon(days(1), horizon_spot_value_date=days(1))


# --- FX futures ------------------------------------------------------------------------

RUB_RATE = 0.15
USD_RATE = 0.04
SPOT = 95_000.0  # synthetic RUB per 1000 USD
RUB_F = DiscountCurve.flat(C, RUB_RATE)
USD_F = DiscountCurve.flat(C, USD_RATE)


def futures_request(**changes: object) -> FxFuturesRiskRequest:
    base = FxFuturesRiskRequest(
        contract=FxFuturesContract("SYNTH-USD", "USD", 1000.0, 1000.0, T),
        spot_rate=SPOT,
        spot_value_date=C,
        rub_curve=RUB_F,
        foreign_curve=USD_F,
        basis_bp=30.0,
        quantity=3.0,
    )
    return replace(base, **changes)  # type: ignore[arg-type]


def test_futures_spot_held_and_forwards_realised_by_hand() -> None:
    engine = FxFuturesRiskEngine(futures_request())
    scale = 3.0
    b = 30.0e-4
    h, sh = days(100), days(101)
    left = (T - sh).days / 365.0
    held = engine.horizon(h, horizon_spot_value_date=sh)
    realised = engine.horizon(h, horizon_spot_value_date=sh, fx_horizon="forwards_realised")
    assert held.price_start == engine.base_price
    assert held.price_forward == pytest.approx(SPOT * math.exp((RUB_RATE - USD_RATE + b) * left))
    # Forwards realised: spot moves to its parity forward without the contract basis,
    # so the carry is the basis decaying over the horizon.
    s_h = SPOT * math.exp((RUB_RATE - USD_RATE) * 101 / 365.0)
    assert realised.horizon_spot_rate == pytest.approx(s_h, rel=1e-14)
    assert realised.price_forward == pytest.approx(
        SPOT * math.exp(RUB_RATE - USD_RATE) * math.exp(b * left), rel=1e-13
    )
    assert realised.carry_rub == pytest.approx(
        scale * engine.base_price * (math.exp(-b * 101 / 365.0) - 1.0), rel=1e-9
    )
    for result in (held, realised):
        assert result.pv_start_rub == 0.0
        assert result.received_rub == 0.0
        assert result.quantity == 3.0
        assert result.price_rolled == pytest.approx(result.price_forward, rel=1e-13)
        assert result.rolldown_rub == pytest.approx(0.0, abs=1e-6)
        assert result.carry_rub == pytest.approx(
            scale * (result.price_forward - result.price_start)
        )
        _check_identities(result)


def test_futures_on_sloped_curves_hold_the_calibrated_basis() -> None:
    market = 104_000.0
    req = futures_request(
        rub_curve=RUB_NODES,
        foreign_curve=CNY_NODES,
        basis_bp=None,
        futures_price=market,
        quantity=-2.0,
    )
    engine = FxFuturesRiskEngine(req)
    h, sh = days(60), days(61)
    for rule in RULES:
        result = fx_futures_horizon(
            req, horizon_date=h, horizon_spot_value_date=sh, fx_horizon=rule
        )
        assert result.price_start == pytest.approx(market, rel=1e-12)
        assert result.base_id == engine.base_id
        assert result.rolldown_rub != pytest.approx(0.0, abs=1.0)
        assert result.horizon_pnl_rub == pytest.approx(
            -2.0 * (result.price_rolled - result.price_start), rel=1e-12
        )
        _check_identities(result)
    # The forward-read price from today's forward spot is today's parity forward
    # times the basis over what is left to delivery.
    realised = engine.horizon(h, horizon_spot_value_date=sh, fx_horizon="forwards_realised")
    tau = (T - sh).days / 365.0
    assert realised.price_forward == pytest.approx(
        engine.theoretical_price * math.exp(engine.basis_bp * 1e-4 * tau), rel=1e-12
    )


def test_futures_delivered_before_the_horizon_settle_at_the_fixing() -> None:
    engine = FxFuturesRiskEngine(futures_request())
    h = days(400)
    held = engine.horizon(h, horizon_spot_value_date=h)
    realised = engine.horizon(h, horizon_spot_value_date=h, fx_horizon="forwards_realised")
    assert held.price_forward == held.price_rolled == SPOT
    convergence = SPOT * math.exp(RUB_RATE - USD_RATE)  # parity forward, no basis
    assert realised.price_forward == realised.price_rolled
    assert realised.price_forward == pytest.approx(convergence, rel=1e-14)
    assert realised.price_forward == pytest.approx(engine.theoretical_price, rel=1e-14)
    for result in (held, realised):
        assert result.rolldown_rub == 0.0
        assert result.carry_rub == pytest.approx(3.0 * (result.price_forward - engine.base_price))
        _check_identities(result)


def test_futures_delivery_inside_the_horizon_spot_lag() -> None:
    # Delivery after the horizon but before its spot date: parity runs backward.
    req = futures_request(contract=FxFuturesContract("SYNTH-USD", "USD", 1000.0, 1000.0, days(101)))
    held = FxFuturesRiskEngine(req).horizon(days(100), horizon_spot_value_date=days(102))
    back = -1.0 / 365.0
    assert held.price_forward == pytest.approx(
        SPOT * math.exp((RUB_RATE - USD_RATE + 30e-4) * back)
    )


def test_futures_horizon_result_contract_and_validation() -> None:
    engine = FxFuturesRiskEngine(futures_request(spot_value_date=days(2)))
    result = engine.horizon(days(10), horizon_spot_value_date=days(12))
    assert result.model_version == FX_FUTURES_HORIZON_MODEL_VERSION == "rub-fx-futures-horizon-v1"
    assert (result.contract_id, result.valuation_date, result.fx_horizon) == (
        "SYNTH-USD",
        C,
        "spot_held",
    )
    with pytest.raises(FrozenInstanceError):
        result.carry_rub = 0.0  # type: ignore[misc]
    with pytest.raises(InvalidTerms, match="fx_horizon"):
        engine.horizon(days(10), horizon_spot_value_date=days(12), fx_horizon="forward")  # type: ignore[arg-type]
    with pytest.raises(InvalidTerms, match="horizon date"):
        engine.horizon(C, horizon_spot_value_date=days(2))
    with pytest.raises(InvalidTerms, match="precede the horizon date"):
        engine.horizon(days(10), horizon_spot_value_date=days(9))
    with pytest.raises(InvalidTerms, match="today's spot value date"):
        engine.horizon(days(1), horizon_spot_value_date=days(1))
