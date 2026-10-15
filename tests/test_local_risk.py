from __future__ import annotations

import math
from dataclasses import replace
from datetime import date, timedelta

import pytest

from rub_fixed_income import (
    CONTINUOUS,
    BondCashflow,
    BondScenario,
    CurveBootstrapRequest,
    DiscountCurve,
    FixedRubBondInput,
    OisQuote,
    RiskRunContext,
    ZSpreadConvention,
    __version__,
    prepare_local_bond_risk,
)

START = date(2025, 1, 1)
END = START + timedelta(days=730)


def source() -> FixedRubBondInput:
    return FixedRubBondInput(
        context=RiskRunContext("run", "synthetic", "market", "terms", "curve", "continuous_zero"),
        cashflows=(BondCashflow(END, 1000),),
        settlement_date=START,
        horizon_date=END,
        horizon="maturity",
        cashflow_currency="RUB",
        coupon_model="fixed",
        current_nominal_rub=1000,
        clean_price_pct=78,
        accrued_interest_rub=10,
        quantity=-2,
        curve=DiscountCurve.flat(START, 0.1),
        spread_convention=ZSpreadConvention(compounding=CONTINUOUS),
        spread_kind="credit",
    )


def test_adapter_converts_clean_price_calibrates_and_binds_exact_scenario() -> None:
    prepared = prepare_local_bond_risk(source())
    assert prepared.engine.base_price_rub == pytest.approx(790)
    expected_spread = (-math.log(790 / 1000) / 2 - 0.1) * 1e4
    assert prepared.engine.spread_bp == pytest.approx(expected_spread, abs=1e-7)
    result = prepared.reprice(BondScenario.parallel(100), curve_basis="continuous_zero")
    assert result.result.total_pnl_rub == pytest.approx(-2 * 790 * math.expm1(-0.02), abs=1e-8)
    assert result.context == source().context
    assert result.library_version == __version__
    assert result.calculation_id == prepared.calculation_id
    assert result == prepared.reprice(BondScenario.parallel(100), curve_basis="continuous_zero")
    assert (
        result.scenario_id
        != prepared.reprice(BondScenario.parallel(200), curve_basis="continuous_zero").scenario_id
    )


def test_original_ois_quotes_require_matching_context_and_shocks() -> None:
    s = source()
    quotes = CurveBootstrapRequest(START, (OisQuote("2Y", END, 0.1),))
    with pytest.raises(ValueError, match="basis"):
        prepare_local_bond_risk(replace(s, curve=quotes))
    p = prepare_local_bond_risk(
        replace(s, curve=quotes, context=replace(s.context, curve_basis="ois_quote"))
    )
    assert p.engine.risk().curve_basis == "ois_quote"
    assert p.reprice(BondScenario.parallel(10), curve_basis="ois_quote").result.total_pnl_rub > 0
    with pytest.raises(ValueError, match="basis"):
        p.reprice(BondScenario.parallel(10), curve_basis="continuous_zero")
    with pytest.raises(ValueError, match="basis"):
        prepare_local_bond_risk(replace(s, context=replace(s.context, curve_basis="ois_quote")))


@pytest.mark.parametrize(
    "field",
    ["run_id", "instrument_id", "market_snapshot_id", "schedule_snapshot_id", "curve_snapshot_id"],
)
def test_provenance_is_required_and_affects_identity(field: str) -> None:
    s = source()
    with pytest.raises(ValueError):
        replace(s.context, **{field: " "})  # type: ignore[arg-type]
    a = prepare_local_bond_risk(s)
    b = prepare_local_bond_risk(replace(s, context=replace(s.context, **{field: "revision-2"})))  # type: ignore[arg-type]
    assert a.engine.base_id == b.engine.base_id
    assert a.calculation_id != b.calculation_id
    shock = BondScenario.parallel(1)
    assert (
        a.reprice(shock, curve_basis="continuous_zero").scenario_id
        != b.reprice(shock, curve_basis="continuous_zero").scenario_id
    )


def test_clean_and_accrued_decomposition_is_bound_even_at_same_dirty_price() -> None:
    a = prepare_local_bond_risk(source())
    b = prepare_local_bond_risk(replace(source(), clean_price_pct=77, accrued_interest_rub=20))
    assert a.engine.base_id == b.engine.base_id
    assert a.calculation_id != b.calculation_id


@pytest.mark.parametrize(
    "changes",
    [
        {"cashflow_currency": "USD"},
        {"coupon_model": "floating"},
        {"horizon": "call"},
        {"horizon_date": START},
        {"horizon_date": END - timedelta(days=1)},
        {"horizon_date": END + timedelta(days=1)},
        {"cashflows": ()},
        {"current_nominal_rub": 0},
        {"clean_price_pct": float("nan")},
        {"accrued_interest_rub": -1},
        {"quantity": float("inf")},
    ],
)
def test_invalid_boundary_inputs_are_errors(changes: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        prepare_local_bond_risk(replace(source(), **changes))  # type: ignore[arg-type]


def test_amortizing_confirmed_put_uses_supplied_horizon_and_no_post_put_flows() -> None:
    s = source()
    flows = [
        BondCashflow(START, 1000),
        BondCashflow(START + timedelta(days=365), 300),
        BondCashflow(END, 720),
    ]
    p = prepare_local_bond_risk(replace(s, cashflows=flows, horizon="confirmed_investor_put"))  # type: ignore[arg-type]
    before = p.reprice(BondScenario.parallel(100), curve_basis="continuous_zero")
    flows.clear()
    assert before == p.reprice(BondScenario.parallel(100), curve_basis="continuous_zero")
    assert p.engine.base_price_rub == pytest.approx(790)


def test_no_implicit_factor_basis() -> None:
    p = prepare_local_bond_risk(source())
    with pytest.raises(TypeError):
        p.reprice(BondScenario())  # type: ignore[call-arg]
    with pytest.raises(ValueError):
        replace(source().context, curve_basis="unknown")  # type: ignore[arg-type]
