"""Desk factor vocabulary (roadmap §2.4)."""

from __future__ import annotations

from datetime import date

import pytest
from test_fx_futures import engine as fx_engine

from rub_fixed_income import (
    RISK_BUCKETS,
    BondCashflow,
    BondRiskEngine,
    BondRiskRequest,
    DiscountCurve,
    InvalidTerms,
    ZSpreadConvention,
    bond_factor_risk,
    fx_factor,
    fx_futures_factor_risk,
    ir_factor,
)

START = date(2026, 1, 1)


def bond_engine() -> BondRiskEngine:
    flows = tuple(BondCashflow(date(2026 + k, 1, 1), 100.0) for k in range(1, 6)) + (
        BondCashflow(date(2031, 1, 1), 1000.0),
    )
    return BondRiskEngine(
        BondRiskRequest(
            flows,
            START,
            DiscountCurve.flat(START, 0.12),
            ZSpreadConvention(),
            "credit",
            dirty_price_rub=1010.0,
            quantity=3,
        )
    )


def test_factor_keys() -> None:
    assert ir_factor("RUB_OFZ_KBD", "5Y") == "IR|RUB_OFZ_KBD|5Y"
    assert fx_factor("CNY") == "FX|CNY"
    with pytest.raises(InvalidTerms, match="bucket"):
        ir_factor("C", "4Y")
    with pytest.raises(InvalidTerms, match="without"):
        ir_factor("A|B", "5Y")


def test_bond_factors_match_engine_risk_and_hessian_is_upper_triangle() -> None:
    eng = bond_engine()
    risk = eng.risk(difference="central")
    second = eng.second_order()
    factors = bond_factor_risk(risk, "RUB_OFZ_KBD", second)
    assert factors.factors == (*(f"IR|RUB_OFZ_KBD|{b}" for b in RISK_BUCKETS), "CS")
    first = factors.first_order_by_factor()
    for bucket in risk.buckets:
        assert first[f"IR|RUB_OFZ_KBD|{bucket.bucket}"] == pytest.approx(bucket.dv01_rub, rel=1e-9)
    assert first["CS"] == pytest.approx(risk.cs01_rub, rel=1e-9)
    assert all(e.i <= e.j for e in factors.hessian)
    dense = second.hessian_rub_per_bp2
    for entry in factors.hessian:
        assert entry.value == dense[entry.i][entry.j] == dense[entry.j][entry.i]
    assert factors.hessian_by_factor()[("CS", "CS")] == pytest.approx(dense[-1][-1])
    first_only = bond_factor_risk(risk, "RUB_OFZ_KBD")
    assert not first_only.has_second_order and first_only.hessian == ()
    with pytest.raises(InvalidTerms, match="central"):
        bond_factor_risk(eng.risk(), "RUB_OFZ_KBD")


def test_fx_futures_factors_cover_both_curves_basis_and_fx() -> None:
    eng = fx_engine()
    risk = eng.risk(difference="central")
    second = eng.second_order()
    factors = fx_futures_factor_risk(risk, "RUB_OIS", "USD_OIS", second)
    assert len(factors.factors) == 24
    assert factors.factors[-2:] == ("FXBASIS|USD", "FX|USD")
    first = factors.first_order_by_factor()
    assert first["FX|USD"] == risk.fx_delta_rub_per_pct
    assert first["FXBASIS|USD"] == pytest.approx(risk.basis01_rub, rel=1e-9)
    assert sum(first[f"IR|RUB_OIS|{b}"] for b in RISK_BUCKETS) == pytest.approx(
        risk.rub_dv01_rub, rel=1e-9
    )
    assert max(e.j for e in factors.hessian) < 23  # spot is not a Hessian factor
    with pytest.raises(InvalidTerms, match="distinct"):
        fx_futures_factor_risk(risk, "X", "X")
