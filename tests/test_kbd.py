"""KBD evaluator against MOEX-published standard yields (roadmap 0.6)."""

from __future__ import annotations

import json
import math
from datetime import date, timedelta
from pathlib import Path

import pytest

from rub_fixed_income import (
    BondCashflow,
    BondRiskEngine,
    BondRiskRequest,
    InvalidTerms,
    ZcycParameters,
    ZSpreadConvention,
    g_spread_bp,
    kbd_discount_curve,
    kbd_effective_yield,
    kbd_zero_rate_bp,
)

FIXTURES = Path(__file__).parent / "fixtures" / "kbd"


def load(name: str) -> tuple[ZcycParameters, list[tuple[float, float]]]:
    raw = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    row = raw["params"]["data"][0]
    params = ZcycParameters(
        date.fromisoformat(row[0]), row[2], row[3], row[4], row[5], tuple(row[6:15])
    )
    yields = [(float(r[2]), float(r[3])) for r in raw["yearyields"]["data"]]
    return params, yields


FILES = sorted(p.name for p in FIXTURES.glob("zcyc_*.json"))


@pytest.mark.parametrize("name", FILES)
def test_reproduces_moex_standard_yields_within_a_hundredth_of_a_bp(name: str) -> None:
    params, published = load(name)
    assert len(published) == 11
    for tenor, yield_pct in published:
        model_pct = kbd_effective_yield(params, tenor) * 100.0
        assert abs(model_pct - yield_pct) * 100.0 < 0.01, (name, tenor)


def test_formula_limits_and_validation() -> None:
    params = ZcycParameters(date(2026, 1, 1), 1000.0, 0.0, 0.0, 1.0, (0.0,) * 9)
    assert kbd_zero_rate_bp(params, 3.0) == pytest.approx(1000.0)
    assert kbd_effective_yield(params, 3.0) == pytest.approx(math.expm1(0.1))
    # Short end: G(t) -> B1 + B2 as t -> 0.
    short = ZcycParameters(date(2026, 1, 1), 800.0, -200.0, 50.0, 0.5, (0.0,) * 9)
    assert kbd_zero_rate_bp(short, 1e-9) == pytest.approx(600.0, abs=1e-5)
    with pytest.raises(ValueError, match="positive"):
        kbd_zero_rate_bp(params, 0.0)
    with pytest.raises(InvalidTerms, match="nine"):
        ZcycParameters(date(2026, 1, 1), 1.0, 0.0, 0.0, 1.0, (0.0,) * 8)  # type: ignore[arg-type]
    with pytest.raises(InvalidTerms, match="T1"):
        ZcycParameters(date(2026, 1, 1), 1.0, 0.0, 0.0, 0.0, (0.0,) * 9)


@pytest.mark.parametrize("name", FILES)
def test_materialised_curve_matches_the_formula_daily(name: str) -> None:
    params, _ = load(name)
    curve = kbd_discount_curve(params)
    worst = 0.0
    for offset in range(1, 365 * 30 + 7):
        target = params.trade_date + timedelta(days=offset)
        tenor = offset / 365.0
        model = -math.log(curve.discount_factor(target)) / tenor * 10_000.0
        worst = max(worst, abs(model - kbd_zero_rate_bp(params, tenor)))
    assert worst < 0.01


def test_g_spread_and_pricing_on_the_kbd_curve() -> None:
    params, published = load("zcyc_2026-10-15.json")
    five_year = dict(published)[5.0] / 100.0
    assert g_spread_bp(params, five_year + 0.0123, 5.0) == pytest.approx(123.0, abs=0.01)
    curve = kbd_discount_curve(params)
    # A 5Y zero priced on the curve itself has zero Z-spread and rate risk on 5Y only.
    maturity = params.trade_date + timedelta(days=(5 * 365))
    dirty = 1000.0 * curve.discount_factor(maturity)
    engine = BondRiskEngine(
        BondRiskRequest(
            (BondCashflow(maturity, 1000.0),),
            params.trade_date,
            curve,
            ZSpreadConvention(),
            "ofz",
            dirty_price_rub=dirty,
        )
    )
    risk = engine.risk(difference="central")
    assert engine.spread_bp == pytest.approx(0.0, abs=1e-8)
    assert {b.bucket for b in risk.buckets if abs(b.dv01_rub) > 1e-9} == {"5Y"}
