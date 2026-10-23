"""Closed-form second order of fixed-cashflow bonds (0.37.0, ``second_order(method="analytic")``)."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import numpy as np
import pytest
from test_floating_kbd import _request as kbd_floater_request
from test_kbd import load
from test_portfolio import POLICY, run

from rub_fixed_income import (
    BondCashflow,
    BondRiskEngine,
    BondRiskRequest,
    BondScenario,
    Compounding,
    InvalidTerms,
    ZSpreadConvention,
    kbd_discount_curve,
)
from rub_fixed_income._analytic_risk import _par_map
from rub_fixed_income._par_overlay import par_jacobians
from rub_fixed_income.dates import add_months
from rub_fixed_income.risk import ANALYTIC_SECOND_ORDER_MODEL_VERSION
from rub_fixed_income.serialization import to_dict

PARAMS, _ = load("zcyc_2026-10-15.json")
DATE = PARAMS.trade_date
CURVES = {
    "continuous": kbd_discount_curve(PARAMS),
    "annual": kbd_discount_curve(PARAMS, zero_shock="annual"),
    "annual_par": kbd_discount_curve(PARAMS, zero_shock="annual_par"),
}
CONVENTIONS = (
    ZSpreadConvention(),
    ZSpreadConvention(Compounding(2)),
    ZSpreadConvention(Compounding(0)),
)


def bond(
    shock: str,
    *,
    settle_days: int = 0,
    convention: ZSpreadConvention = CONVENTIONS[0],
    years: int = 9,
    coupon: float = 0.11,
    quantity: float = -2.5,
) -> BondRiskEngine:
    dates = [add_months(DATE, 6 * k) for k in range(1, 2 * years + 1)]
    flows = [BondCashflow(d, 1000.0 * coupon / 2) for d in dates]
    flows[-1] = BondCashflow(dates[-1], 1000.0 + 1000.0 * coupon / 2)
    return BondRiskEngine(
        BondRiskRequest(
            tuple(flows),
            DATE + timedelta(days=settle_days),
            CURVES[shock],
            convention,
            "ofz",
            dirty_price_rub=930.0,
            quantity=quantity,
        )
    )


@pytest.mark.parametrize("shock", sorted(CURVES))
@pytest.mark.parametrize("convention", CONVENTIONS, ids=lambda c: c.identifier)
@pytest.mark.parametrize("settle_days", [0, 3])
def test_closed_form_equals_the_bumped_second_order(
    shock: str, convention: ZSpreadConvention, settle_days: int
) -> None:
    engine = bond(shock, settle_days=settle_days, convention=convention)
    exact = engine.second_order(method="analytic")
    bumped = engine.second_order()
    assert exact.factors == bumped.factors and exact.base_id == bumped.base_id
    assert exact.curve_basis == bumped.curve_basis
    assert exact.model_version == ANALYTIC_SECOND_ORDER_MODEL_VERSION
    g_a, g_b = np.array(exact.gradient_rub_per_bp), np.array(bumped.gradient_rub_per_bp)
    h_a, h_b = np.array(exact.hessian_rub_per_bp2), np.array(bumped.hessian_rub_per_bp2)
    # Central differences of 1 bp carry an O(bump^2) error: 1e-7 relative here.
    assert np.max(np.abs(g_a - g_b)) < 1e-6 * np.max(np.abs(g_b))
    assert np.max(np.abs(h_a - h_b)) < 2e-6 * np.max(np.abs(h_b))
    assert np.array_equal(h_a, h_a.T)
    if shock == "annual_par":
        assert g_a[:3].tolist() == [0.0, 0.0, 0.0]


def test_par_gamma_needs_the_curvature_of_the_instruments() -> None:
    """Without the second-order term of the par instruments the bucket gamma is wrong."""
    engine = bond("annual")
    zero = engine.second_order(method="analytic")
    par = bond("annual_par").second_order(method="analytic")
    jacobian, _ = par_jacobians(CURVES["annual_par"])
    inverse = np.linalg.inv(jacobian)
    e = _par_map()
    h_x = np.array(zero.hessian_rub_per_bp2)[:11, :11]
    naive = inverse.T @ (e.T @ h_x @ e) @ inverse
    h_q = np.array(par.hessian_rub_per_bp2)[3:11, 3:11]
    assert np.max(np.abs(naive - h_q)) > 0.05 * np.max(np.abs(h_q))
    # The first order needs no correction: J^-T maps it exactly.
    g_x = np.array(zero.gradient_rub_per_bp)[:11]
    assert inverse.T @ (e.T @ g_x) == pytest.approx(par.gradient_rub_per_bp[3:11], rel=1e-10)


def test_taylor_with_the_closed_form_tracks_a_large_par_move() -> None:
    engine = bond("annual_par", quantity=1000.0)
    scenario = BondScenario((0, 0, 0, 20.0, 40.0, 60.0, 80.0, 100.0, 80.0, 60.0, 40.0), 15.0)
    exact = engine.reprice(scenario).total_pnl_rub
    second = engine.second_order(method="analytic")
    x = np.array((*scenario.curve_shifts_bp, scenario.spread_shift_bp))
    taylor = float(
        np.dot(second.gradient_rub_per_bp, x) + 0.5 * x @ np.array(second.hessian_rub_per_bp2) @ x
    )
    linear = float(np.dot(second.gradient_rub_per_bp, x))
    assert abs(taylor - exact) < 0.05 * abs(linear - exact)


def test_unsupported_engines_are_refused_with_a_reason() -> None:
    with pytest.raises(InvalidTerms, match="floating coupons"):
        BondRiskEngine(kbd_floater_request()).second_order(method="analytic")
    with pytest.raises(ValueError, match="bump or analytic"):
        bond("annual").second_order(method="taylor")  # type: ignore[arg-type]


def test_portfolio_policy_takes_the_closed_form_where_it_exists() -> None:
    analytic = replace(POLICY, second_order_method="analytic")
    # Omitted at its default: earlier policies and job IDs keep their bytes.
    assert "second_order_method" not in to_dict(POLICY)
    assert to_dict(analytic)["second_order_method"] == "analytic"
    left = {p.position_id: p for p in run().positions}
    right = {p.position_id: p for p in run(policy=analytic).positions}
    assert left.keys() == right.keys()
    for key, a in left.items():
        b = right[key]
        assert a.pv_rub == b.pv_rub
        fa, fb = np.array(a.factor_risk.first_order), np.array(b.factor_risk.first_order)
        assert np.max(np.abs(fa - fb)) <= 1e-6 * max(1.0, np.max(np.abs(fa)))
        ha = {(e.i, e.j): e.value for e in a.factor_risk.hessian}
        hb = {(e.i, e.j): e.value for e in b.factor_risk.hessian}
        scale = max([1.0, *map(abs, ha.values())])
        for ij in ha.keys() | hb.keys():
            assert abs(ha.get(ij, 0.0) - hb.get(ij, 0.0)) <= 2e-6 * scale
    with pytest.raises(InvalidTerms, match="second_order_method"):
        replace(POLICY, second_order_method="exact")  # type: ignore[arg-type]
