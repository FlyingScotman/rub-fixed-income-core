"""Benchmark-bond equivalents of curve risk (0.37.0, the hedge view)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from test_floating_kbd import _request as kbd_floater_request
from test_kbd import load

from rub_fixed_income import (
    BondCashflow,
    BondRiskEngine,
    BondRiskRequest,
    BondScenario,
    DiscountCurve,
    InvalidTerms,
    ZSpreadConvention,
    benchmark_equivalents,
    kbd_discount_curve,
)
from rub_fixed_income.dates import add_months

PARAMS, _ = load("zcyc_2026-10-15.json")
DATE = PARAMS.trade_date
KBD = kbd_discount_curve(PARAMS, zero_shock="annual_par")


def bond(
    years: float,
    coupon: float,
    price: float,
    *,
    quantity: float = 1.0,
    curve: DiscountCurve = KBD,
) -> BondRiskEngine:
    months = round(years * 12)
    dates = [add_months(DATE, months - 6 * k) for k in range(months // 6)][::-1]
    flows = [BondCashflow(d, 1000.0 * coupon / 2) for d in dates]
    flows[-1] = BondCashflow(dates[-1], 1000.0 + 1000.0 * coupon / 2)
    return BondRiskEngine(
        BondRiskRequest(
            tuple(flows),
            DATE + timedelta(days=1),
            curve,
            ZSpreadConvention(),
            "ofz",
            dirty_price_rub=price,
            quantity=quantity,
        )
    )


TERMS = {
    "B2": (2.0, 0.12, 980.0),
    "B3": (3.5, 0.14, 990.0),
    "B5": (5.5, 0.15, 985.0),
    "B7": (7.0, 0.16, 1000.0),
    "B10": (9.5, 0.12, 820.0),
    "B15": (14.0, 0.13, 830.0),
}
"""Years, coupon and dirty price of each benchmark."""
BENCHMARKS = tuple((name, bond(*terms)) for name, terms in TERMS.items())


def test_a_benchmark_held_alone_is_its_own_hedge() -> None:
    for name, _ in BENCHMARKS:
        held = bond(*TERMS[name], quantity=1000.0)
        result = benchmark_equivalents(BENCHMARKS, [held])
        own = next(e for e in result.equivalents if e.instrument_id == name)
        assert own.hedge_quantity == pytest.approx(-1000.0, rel=1e-9)
        assert own.risk_rub_per_bp == pytest.approx(1000.0 * own.yield_dv01_rub, rel=1e-9)
        for other in result.equivalents:
            if other.instrument_id != name:
                assert abs(other.risk_rub_per_bp) < 1e-9 * abs(own.risk_rub_per_bp)


def test_a_bond_between_two_benchmarks_maps_to_them_and_the_hedge_works() -> None:
    target = bond(6, 0.10, 860.0, quantity=1e6)
    result = benchmark_equivalents(BENCHMARKS, [target])
    assert result.condition_number < 10 and result.warnings == ()
    by_id = {e.instrument_id: e for e in result.equivalents}
    # The redemption and the late coupons go to the two neighbours, the early coupons to
    # the shorter benchmarks; nothing beyond the 7Y.
    neighbours = by_id["B5"].risk_rub_per_bp + by_id["B7"].risk_rub_per_bp
    assert abs(neighbours) > 0.9 * sum(abs(e.risk_rub_per_bp) for e in result.equivalents)
    assert abs(by_id["B10"].risk_rub_per_bp) + abs(by_id["B15"].risk_rub_per_bp) < 1e-6
    assert by_id["B5"].risk_rub_per_bp < 0 and by_id["B7"].risk_rub_per_bp < 0
    # The total is the parallel risk: within the spread-held unit gap of the bond's DV01.
    parallel = target.risk(difference="central").parallel_dv01_rub
    assert result.total_risk_rub_per_bp == pytest.approx(parallel, rel=0.03)
    # Holding the hedge, a parallel par move of the KBD costs little.
    hedges = [bond(*TERMS[e.instrument_id], quantity=e.hedge_quantity) for e in result.equivalents]
    move = BondScenario.parallel(25.0)
    pnl = target.reprice(move).total_pnl_rub + sum(h.reprice(move).total_pnl_rub for h in hedges)
    assert abs(pnl) < 0.02 * abs(target.reprice(move).total_pnl_rub)
    hedged = benchmark_equivalents(BENCHMARKS, [target, *hedges])
    assert all(abs(e.risk_rub_per_bp) < 1e-6 * abs(parallel) for e in hedged.equivalents)


def test_close_benchmarks_stay_well_conditioned_and_bad_inputs_are_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Nodes sit at the benchmarks' own maturities, so J is nearly triangular: even 5.5 and
    # 5.6 years apart the system is well conditioned.
    close = (*BENCHMARKS, ("B5b", bond(5.6, 0.15, 985.0)))
    result = benchmark_equivalents(close, [bond(6, 0.10, 860.0)])
    assert result.condition_number < 10 and result.warnings == ()
    monkeypatch.setattr("rub_fixed_income.hedge_equivalents.CONDITION_WARNING", 1.0)
    assert "too close" in benchmark_equivalents(close, []).warnings[0]
    with pytest.raises(InvalidTerms, match="must differ"):
        benchmark_equivalents((*BENCHMARKS, ("B7b", bond(7, 0.10, 900.0))), [])
    with pytest.raises(InvalidTerms, match="same curve"):
        other = kbd_discount_curve(PARAMS, zero_shock="annual")
        benchmark_equivalents(BENCHMARKS, [bond(6, 0.1, 860.0, curve=other)])
    with pytest.raises(InvalidTerms, match="floating coupons"):
        benchmark_equivalents(BENCHMARKS, [BondRiskEngine(kbd_floater_request())])
    with pytest.raises(InvalidTerms, match="at least one"):
        benchmark_equivalents((), [])
