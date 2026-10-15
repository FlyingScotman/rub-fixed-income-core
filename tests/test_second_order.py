from __future__ import annotations

import math
from dataclasses import replace
from datetime import date, timedelta
from unittest.mock import patch

import pytest

from rub_fixed_income import (
    ANNUAL,
    CONTINUOUS,
    MONTHLY,
    BondCashflow,
    BondRiskEngine,
    BondRiskRequest,
    BondScenario,
    Compounding,
    CurveBootstrapRequest,
    DiscountCurve,
    OisQuote,
    ZSpreadConvention,
)

START = date(2025, 1, 1)


def engine(quantity: float = 1.0) -> BondRiskEngine:
    return BondRiskEngine(
        BondRiskRequest(
            cashflows=(BondCashflow(START + timedelta(days=1460), 1000),),
            settlement_date=START,
            curve=DiscountCurve.flat(START, 0.10),
            spread_convention=ZSpreadConvention(compounding=CONTINUOUS),
            spread_kind="credit",
            spread_bp=200,
            quantity=quantity,
        )
    )


def test_full_hessian_and_gradient_against_exponential_derivatives() -> None:
    result = engine().second_order()
    pv = 1000 * math.exp(-0.12 * 4)
    # 4Y is halfway between 3Y and 5Y: P = PV exp(-2x_3Y-2x_5Y-4s).
    coefficients = [0.0] * 12
    coefficients[5] = coefficients[6] = -2e-4
    coefficients[11] = -4e-4
    for i in range(12):
        assert result.gradient_rub_per_bp[i] == pytest.approx(pv * coefficients[i], abs=1e-8)
        for j in range(12):
            assert result.hessian_rub_per_bp2[i][j] == pytest.approx(
                pv * coefficients[i] * coefficients[j], abs=2e-11
            )
            assert result.hessian_rub_per_bp2[i][j] == result.hessian_rub_per_bp2[j][i]
    assert result.factors[-1] == "credit_spread"


def test_nonlocal_cross_gamma_from_settlement_rebasing_is_retained() -> None:
    e = BondRiskEngine(
        replace(
            engine().request,
            settlement_date=START + timedelta(days=365),
            cashflows=(BondCashflow(START + timedelta(days=3650), 1000),),
        )
    )
    result = e.second_order()
    pv = 1000 * math.exp(-0.12 * 9)
    # Settlement 1Y enters with +1; payment 10Y with -10: far off-diagonal.
    assert result.hessian_rub_per_bp2[3][8] == pytest.approx(-10e-8 * pv, rel=3e-7)
    assert result.hessian_rub_per_bp2[3][11] == pytest.approx(-9e-8 * pv, rel=3e-7)


@pytest.mark.parametrize("compounding", [ANNUAL, MONTHLY])
def test_nominal_spread_gamma_against_independent_derivatives(compounding: Compounding) -> None:
    e = BondRiskEngine(
        replace(
            engine().request,
            cashflows=(BondCashflow(START + timedelta(days=730), 1000),),
            spread_convention=ZSpreadConvention(compounding=compounding),
        )
    )
    m, t = compounding.periods_per_year, 2.0
    a = math.exp(0.1 / m)
    b = a + 0.02 / m
    pv = 1000 * b ** (-m * t)
    risk = e.second_order(bump_bp=2)
    assert risk.gradient_rub_per_bp[4] == pytest.approx(-t * pv * a / b * 1e-4, abs=1e-8)
    assert risk.gradient_rub_per_bp[11] == pytest.approx(-t * pv / b * 1e-4, abs=1e-8)
    expected = {
        (4, 4): t * pv * ((t + 1 / m) * a * a / (b * b) - a / (m * b)),
        (4, 11): t * (t + 1 / m) * pv * a / (b * b),
        (11, 11): t * (t + 1 / m) * pv / (b * b),
    }
    for (i, j), value in expected.items():
        assert risk.hessian_rub_per_bp2[i][j] == pytest.approx(value * 1e-8, abs=1e-10)


def test_quote_hessian_against_analytical_two_node_bootstrap() -> None:
    a, b = 0.1, 0.12
    e = BondRiskEngine(
        replace(
            engine().request,
            cashflows=(BondCashflow(START + timedelta(days=730), 1000),),
            curve=CurveBootstrapRequest(
                START,
                (
                    OisQuote("1Y", START + timedelta(days=365), a),
                    OisQuote("2Y", START + timedelta(days=730), b),
                ),
            ),
            spread_bp=0,
        )
    )
    r = e.second_order(bump_bp=2)
    pv = 1000 * (1 - b / (1 + a)) / (1 + b)
    da = 1000 * b / ((1 + b) * (1 + a) ** 2)
    db = -1000 * (1 + 1 / (1 + a)) / (1 + b) ** 2
    assert r.curve_basis == "ois_quote"
    assert r.gradient_rub_per_bp[3] == pytest.approx(da * 1e-4, rel=1e-7)
    assert r.gradient_rub_per_bp[4] == pytest.approx(db * 1e-4, rel=1e-7)
    expected = {
        (3, 3): -2000 * b / ((1 + b) * (1 + a) ** 3),
        (4, 4): 2000 * (1 + 1 / (1 + a)) / (1 + b) ** 3,
        (3, 4): 1000 / ((1 + a) ** 2 * (1 + b) ** 2),
        (3, 11): -2 * da,
        (4, 11): -2 * db,
        (11, 11): 4 * pv,
    }
    for (i, j), derivative in expected.items():
        assert r.hessian_rub_per_bp2[i][j] == pytest.approx(derivative * 1e-8, abs=1e-10)


@pytest.mark.parametrize("quantity", [-7.0, 0.0, 2.5])
def test_signed_quantity_scales_every_derivative(quantity: float) -> None:
    unit, scaled = engine().second_order(), engine(quantity).second_order()
    assert scaled.gradient_rub_per_bp == pytest.approx(
        tuple(x * quantity for x in unit.gradient_rub_per_bp)
    )
    for a, b in zip(unit.hessian_rub_per_bp2, scaled.hessian_rub_per_bp2):
        assert b == pytest.approx(tuple(x * quantity for x in a))


def test_gamma_bump_convergence_and_no_recalibration() -> None:
    e = engine()
    calibrated = BondRiskEngine(
        replace(e.request, spread_bp=None, dirty_price_rub=e.base_price_rub)
    )
    expected = 16e-8 * 1000 * math.exp(-0.48)
    with patch("rub_fixed_income.risk.solve_z_spread", side_effect=AssertionError("recalibrated")):
        coarse = calibrated.second_order(bump_bp=20).hessian_rub_per_bp2[11][11]
        fine = calibrated.second_order(bump_bp=2).hessian_rub_per_bp2[11][11]
    assert abs(fine - expected) < abs(coarse - expected) / 50


@pytest.mark.parametrize("rate,spread", [(0, 0), (20, 30), (500, 200), (-500, -200)])
def test_taylor_attribution_against_independent_formula(rate: float, spread: float) -> None:
    result = engine().approximate(BondScenario.parallel(rate, spread_shift_bp=spread))
    pv = 1000 * math.exp(-0.48)
    x, y = rate * 1e-4, spread * 1e-4
    assert result.linear_pnl_rub == pytest.approx(-4 * pv * (x + y), abs=2e-5)
    assert result.rates_gamma_pnl_rub == pytest.approx(8 * pv * x * x, abs=1e-5)
    assert result.spread_gamma_pnl_rub == pytest.approx(8 * pv * y * y, abs=1e-5)
    assert result.rates_spread_cross_pnl_rub == pytest.approx(16 * pv * x * y, abs=1e-5)
    exact = pv * math.expm1(-4 * (x + y))
    assert result.exact_pnl_rub == pytest.approx(exact, abs=1e-10)
    assert result.approximate_pnl_rub + result.residual_pnl_rub == pytest.approx(exact)
    if rate:
        assert abs(result.residual_pnl_rub) < abs(exact - result.linear_pnl_rub)


def test_ofz_gamma_label_and_identities() -> None:
    e = BondRiskEngine(replace(engine().request, spread_kind="ofz"))
    assert e.second_order().factors[-1] == "ofz_spread"
    a = e.approximate(BondScenario.parallel(1))
    assert a == e.approximate(BondScenario.parallel(1))
    assert a.second_order_id != e.approximate(BondScenario.parallel(1), bump_bp=2).second_order_id
    assert a.scenario_id != e.approximate(BondScenario.parallel(2)).scenario_id


@pytest.mark.parametrize("bad", [True, float("nan"), float("inf"), -1.0, 0.0, 1e-300, 1e300])
def test_invalid_second_order_bumps(bad: float) -> None:
    with pytest.raises(ValueError):
        engine().second_order(bump_bp=bad)


@pytest.mark.parametrize("quantity", [1.0, -2.0, 0.0])
@pytest.mark.parametrize("recovery", [0.0, 0.4, 1.0])
def test_default_signed_dirty_pnl_on_outstanding_principal(
    quantity: float, recovery: float
) -> None:
    e = engine(quantity)
    r = e.jump_to_default(outstanding_nominal_rub_per_bond=600, recovery_rate=recovery)
    pv = 1000 * math.exp(-0.48) * quantity
    assert r.base_pv_rub == pytest.approx(pv)
    assert r.recovery_pv_rub == 600 * recovery * quantity
    assert r.jtd_pnl_rub == pytest.approx(600 * recovery * quantity - pv)
    assert r.base_id == e.base_id


def test_default_requires_explicit_recovery_and_tracks_model_inputs() -> None:
    e = engine()
    with pytest.raises(TypeError):
        e.jump_to_default(outstanding_nominal_rub_per_bond=1000)  # type: ignore[call-arg]
    a = e.jump_to_default(outstanding_nominal_rub_per_bond=600, recovery_rate=0.4)
    b = e.jump_to_default(outstanding_nominal_rub_per_bond=1000, recovery_rate=0.4)
    c = e.jump_to_default(outstanding_nominal_rub_per_bond=600, recovery_rate=0.5)
    assert len({a.default_id, b.default_id, c.default_id}) == 3


@pytest.mark.parametrize("bad", [True, float("nan"), float("inf"), -0.1, 1.1, 40])
def test_invalid_recovery(bad: float) -> None:
    with pytest.raises(ValueError):
        engine().jump_to_default(outstanding_nominal_rub_per_bond=1000, recovery_rate=bad)


@pytest.mark.parametrize("bad", [True, float("nan"), float("inf"), 0.0, -100])
def test_invalid_default_nominal(bad: float) -> None:
    with pytest.raises(ValueError):
        engine().jump_to_default(outstanding_nominal_rub_per_bond=bad, recovery_rate=0.4)


def test_overflow_is_an_error_not_nonfinite_risk() -> None:
    e = engine(1e308)
    with pytest.raises(ValueError):
        e.jump_to_default(outstanding_nominal_rub_per_bond=1000, recovery_rate=1)
    with pytest.raises(ValueError):
        engine().approximate(BondScenario.parallel(1e300))
