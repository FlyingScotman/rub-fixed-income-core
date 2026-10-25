"""FX crosses without RUB (USD/CNY ...): closed forms and the two-RUB-leg parity."""

from __future__ import annotations

import math
from dataclasses import replace
from datetime import date, timedelta

import pytest

from rub_fixed_income import (
    RISK_BUCKETS,
    CurveConvention,
    DiscountCurve,
    FxCrossForward,
    FxCrossRiskEngine,
    FxCrossRiskRequest,
    FxCrossScenario,
    FxCrossSwap,
    FxForwardLeg,
    FxForwardRiskEngine,
    FxForwardRiskRequest,
    InvalidTerms,
    fx_cross_factor_risk,
    fx_cross_rub_legs,
)

C = date(2026, 1, 1)
T = C + timedelta(days=365)
R, QU, QC = 0.15, 0.04, 0.015
SU, SC = 80.0, 11.0  # RUB per USD, RUB per CNY
N = 1_000_000.0  # USD bought
K = 7.1  # CNY per USD
RUB = DiscountCurve.flat(C, R)
USD = DiscountCurve.flat(C, QU)
CNY = DiscountCurve.flat(C, QC)


def cross(leg: FxForwardLeg | None = None, q: float = 1.0) -> FxCrossForward:
    return FxCrossForward("X-1", "USD", "CNY", q, FxForwardLeg(T, N, K) if leg is None else leg)


def request(
    trade: FxCrossForward | FxCrossSwap | None = None, **changes: object
) -> FxCrossRiskRequest:
    base = FxCrossRiskRequest(
        cross() if trade is None else trade, SU, C, SC, C, RUB, USD, CNY, 0.0, 0.0
    )
    return replace(base, **changes)  # type: ignore[arg-type]


def engine(
    trade: FxCrossForward | FxCrossSwap | None = None, **changes: object
) -> FxCrossRiskEngine:
    return FxCrossRiskEngine(request(trade, **changes))


def rub_leg_pv(trade: FxCrossForward | FxCrossSwap, pivot: float, req: FxCrossRiskRequest) -> float:
    base, quote = fx_cross_rub_legs(trade, pivot_rate=pivot)
    a = FxForwardRiskEngine(
        FxForwardRiskRequest(
            base,
            req.base_spot_rate,
            req.base_spot_value_date,
            req.rub_curve,
            req.base_curve,
            req.base_basis_bp,
        )
    )
    b = FxForwardRiskEngine(
        FxForwardRiskRequest(
            quote,
            req.quote_spot_rate,
            req.quote_spot_value_date,
            req.rub_curve,
            req.quote_curve,
            req.quote_basis_bp,
        )
    )
    return a.base_pv_rub + b.base_pv_rub


def test_deliverable_cross_closed_form_has_no_rub_rate_exposure() -> None:
    e = engine()
    closed = N * (SU * math.exp(-QU) - K * SC * math.exp(-QC))
    assert e.base_pv_rub == pytest.approx(closed, rel=1e-13)
    risk = e.risk(difference="central")
    # Spot on the curve date, delivered on T: DF_rub cancels exactly.
    assert all(b.dv01_rub == 0.0 for b in risk.rub_buckets)
    assert risk.fx_delta_base == pytest.approx(N * math.exp(-QU), rel=1e-13)
    assert risk.fx_delta_quote == pytest.approx(-N * K * math.exp(-QC), rel=1e-13)
    assert risk.base_parallel_dv01_rub == pytest.approx(-N * SU * math.exp(-QU) * 1e-4, rel=1e-6)
    assert risk.quote_parallel_dv01_rub == pytest.approx(
        N * K * SC * math.exp(-QC) * 1e-4, rel=1e-6
    )
    assert risk.cross_spot_rate == pytest.approx(SU / SC)
    leg = risk.legs[0]
    forward = SU * math.exp(R - QU) / (SC * math.exp(R - QC))
    assert leg.forward_rate == pytest.approx(forward, rel=1e-13)
    assert leg.quote_notional == pytest.approx(-N * K)
    assert leg.pv_rub == pytest.approx(e.base_pv_rub, rel=1e-12)


@pytest.mark.parametrize("pivot", [1.0, 11.0, 80.0, 1234.5])
@pytest.mark.parametrize("spot_days", [0, 1])
def test_cross_is_the_sum_of_two_rub_forwards_for_any_pivot(pivot: float, spot_days: int) -> None:
    spot = C + timedelta(days=spot_days)
    trade = cross(FxForwardLeg(T, N, K, T + timedelta(days=2)))
    req = request(trade, base_spot_value_date=spot, quote_spot_value_date=spot, base_basis_bp=7.0)
    e = FxCrossRiskEngine(req)
    assert e.base_pv_rub == pytest.approx(rub_leg_pv(trade, pivot, req), rel=1e-12, abs=1e-6)


def test_cash_settled_cross_pays_the_cross_difference_in_rub() -> None:
    pay = T + timedelta(days=3)
    e = engine(cross(FxForwardLeg(T, N, K, pay)))
    fu, fc = SU * math.exp(R - QU), SC * math.exp(R - QC)
    closed = N * (fu / fc - K) * fc * math.exp(-R * (368 / 365))
    assert e.base_pv_rub == pytest.approx(closed, rel=1e-12)
    rub = e.risk(difference="central").rub_parallel_dv01_rub
    assert rub == pytest.approx(
        -N * (fu - K * fc) * math.exp(-R * 368 / 365) * 3 / 365 * 1e-4, rel=1e-4
    )


def test_quote_units_scale_the_quote_amount() -> None:
    per_100 = engine(cross(FxForwardLeg(T, N, K * 100), q=100.0))
    assert per_100.base_pv_rub == pytest.approx(engine().base_pv_rub, rel=1e-13)


def test_cross_swap_is_the_sum_of_its_legs_and_far_leg_survives() -> None:
    near = FxForwardLeg(C + timedelta(days=1), -N, 7.27)
    far = FxForwardLeg(T, N, 7.0)
    swap = FxCrossSwap("XS-1", "USD", "CNY", 1.0, near, far)
    legs = engine(cross(near)).base_pv_rub + engine(cross(far)).base_pv_rub
    assert engine(swap).base_pv_rub == pytest.approx(legs, rel=1e-12)
    assert swap.far_leg_forward().leg == far
    with pytest.raises(InvalidTerms, match="opposite signs"):
        FxCrossSwap("XS-1", "USD", "CNY", 1.0, near, replace(far, notional_foreign=-N))


def test_factor_risk_names_gradient_and_fx_rate_cross_gamma() -> None:
    tom = C + timedelta(days=1)
    e = engine(base_spot_value_date=tom, quote_spot_value_date=tom)
    risk = e.risk(difference="central")
    second = e.second_order()
    factors = fx_cross_factor_risk(risk, "RUB_OIS", "USD_FX", "CNY_FX", second)
    assert factors.factors[:11] == tuple(f"IR|RUB_OIS|{b}" for b in RISK_BUCKETS)
    assert factors.factors[-4:] == ("FXBASIS|USD", "FXBASIS|CNY", "FX|USD", "FX|CNY")
    first = fx_cross_factor_risk(risk, "RUB_OIS", "USD_FX", "CNY_FX").first_order_by_factor()
    full = factors.first_order_by_factor()
    for name, value in first.items():
        assert full[name] == pytest.approx(value, rel=1e-6, abs=1e-6)
    assert full["FX|USD"] == pytest.approx(risk.fx_delta_base_rub_per_pct, rel=1e-13)
    hess = factors.hessian_by_factor()
    # FX|USD x IR|USD_FX|1Y = 1 % of the base rate derivative; no FX|USD x IR|CNY term.
    assert hess[("IR|USD_FX|1Y", "FX|USD")] == pytest.approx(full["IR|USD_FX|1Y"] / 100, rel=1e-6)
    assert ("IR|CNY_FX|1Y", "FX|USD") not in hess
    assert ("FX|USD", "FX|CNY") not in hess
    with pytest.raises(InvalidTerms, match="distinct"):
        fx_cross_factor_risk(risk, "A", "A", "B")
    with pytest.raises(InvalidTerms, match="central"):
        fx_cross_factor_risk(e.risk(), "A", "B", "C")


def test_second_order_matches_the_two_rub_legs_combined() -> None:
    spot = C + timedelta(days=1)
    trade = cross()
    req = request(trade, base_spot_value_date=spot, quote_spot_value_date=spot)
    e = FxCrossRiskEngine(req)
    second = e.second_order()
    base, quote = fx_cross_rub_legs(trade)
    b = FxForwardRiskEngine(FxForwardRiskRequest(base, SU, spot, RUB, USD, 0.0)).second_order()
    q = FxForwardRiskEngine(FxForwardRiskRequest(quote, SC, spot, RUB, CNY, 0.0)).second_order()
    # RUB 1M (the spot date) against each currency's 1Y bucket (its delivery).
    i_rub, i_usd, i_cny = 0, 11 + 3, 22 + 3
    rub_usd, rub_cny = b.hessian_rub[0][11 + 3], q.hessian_rub[0][11 + 3]
    assert rub_usd != 0.0 and rub_cny != 0.0
    assert second.hessian_rub[i_rub][i_usd] == pytest.approx(rub_usd, rel=1e-6)
    assert second.hessian_rub[i_rub][i_cny] == pytest.approx(rub_cny, rel=1e-6)
    assert second.hessian_rub[i_usd][i_cny] == 0.0
    assert second.factors[-2:] == ("USD spot", "CNY spot")
    assert second.gradient_rub[-2] == pytest.approx(b.gradient_rub[-1], rel=1e-12)


def test_scenario_reprices_exactly_and_splits_by_factor_group() -> None:
    e = engine()
    move = FxCrossScenario(
        base_curve_shifts_bp=(25.0,) * 11, base_spot_shift_pct=5.0, quote_spot_shift_pct=-3.0
    )
    result = e.reprice(move)
    closed = N * (SU * 1.05 * math.exp(-(QU + 0.0025)) - K * SC * 0.97 * math.exp(-QC))
    assert result.shocked_pv_rub == pytest.approx(closed, rel=1e-12)
    assert result.rub_rates_pnl_rub == 0.0 and result.basis_pnl_rub == 0.0
    total = (
        result.base_rates_pnl_rub
        + result.quote_rates_pnl_rub
        + result.fx_spot_pnl_rub
        + result.interaction_pnl_rub
    )
    assert total == pytest.approx(result.total_pnl_rub, rel=1e-12)
    with pytest.raises(TypeError):
        e.reprice(object())  # type: ignore[arg-type]


def test_horizon_and_points_held_are_the_sums_over_the_rub_legs() -> None:
    spot = C + timedelta(days=1)
    trade = cross()
    req = request(trade, base_spot_value_date=spot, quote_spot_value_date=spot)
    e = FxCrossRiskEngine(req)
    h = date(2026, 4, 1)
    carry = e.horizon(
        h,
        base_horizon_spot_value_date=h + timedelta(days=1),
        quote_horizon_spot_value_date=h + timedelta(days=1),
        fx_horizon="forwards_realised",
    )
    base, quote = fx_cross_rub_legs(trade)
    parts = [
        FxForwardRiskEngine(FxForwardRiskRequest(t, s, spot, RUB, c, 0.0)).horizon(
            h, horizon_spot_value_date=h + timedelta(days=1), fx_horizon="forwards_realised"
        )
        for t, s, c in ((base, SU, USD), (quote, SC, CNY))
    ]
    assert carry.pv_rolled_rub == pytest.approx(sum(p.pv_rolled_rub for p in parts), rel=1e-12)
    assert carry.carry_rub == pytest.approx(
        carry.pv_forward_rub + carry.received_rub - e.base_pv_rub, abs=1e-6
    )
    assert carry.fx_horizon == "forwards_realised"
    held = e.points_held_rub_risk()
    assert len(held.rub_buckets) == 11
    # With both outrights held the RUB curve only discounts the cross PV from T.
    assert held.rub_parallel_dv01_rub == pytest.approx(-e.base_pv_rub * 1e-4, rel=1e-4)


def test_identity_and_validation() -> None:
    a, b = engine(), engine(base_basis_bp=1.0)
    assert a.base_id != b.base_id and a.base_id == engine().base_id
    with pytest.raises(InvalidTerms, match="two different currencies"):
        FxCrossForward("X", "USD", "USD", 1.0, FxForwardLeg(T, N, K))
    with pytest.raises(InvalidTerms, match="not RUB"):
        FxCrossForward("X", "USD", "RUB", 1.0, FxForwardLeg(T, N, K))
    ndf_leg = FxForwardLeg(T, N, K, T, fixing_date=T)
    with pytest.raises(InvalidTerms, match="not supported"):
        FxCrossForward("X", "USD", "CNY", 1.0, ndf_leg)
    with pytest.raises(TypeError):
        FxCrossRiskRequest(object(), SU, C, SC, C, RUB, USD, CNY, 0.0, 0.0)  # type: ignore[arg-type]
    with pytest.raises(InvalidTerms, match="settled"):
        engine(cross(FxForwardLeg(C - timedelta(days=1), N, K)))
    with pytest.raises(ValueError, match="pivot rate"):
        fx_cross_rub_legs(cross(), pivot_rate=0.0)


def test_scenarios_on_a_par_yield_rub_curve_refuse_dropped_short_shifts() -> None:
    """As every engine: a par-yield curve's 1M-6M scenario shifts are zero or the 1Y's."""
    par = DiscountCurve.flat(C, R, convention=CurveConvention(zero_shock="annual_par"))
    risk = engine(rub_curve=par)
    with pytest.raises(InvalidTerms, match="3M \\+25.0"):
        risk.reprice(FxCrossScenario(rub_curve_shifts_bp=(0.0, 25.0) + (0.0,) * 9))
    # A parallel shift is valid (the RUB legs of a cross offset: no PnL to check).
    risk.reprice(FxCrossScenario(rub_curve_shifts_bp=(25.0,) * 11))
    # Curves that are not par curves take any shifts.
    engine().reprice(FxCrossScenario(rub_curve_shifts_bp=(0.0, 25.0) + (0.0,) * 9))
