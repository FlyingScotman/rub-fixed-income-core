"""Benchmark-bond equivalents of curve risk (0.37.0, the hedge view; floaters and OFZ futures 0.38.0)."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import numpy as np
import pytest
import test_ofz_futures as ofz
from test_floating_kbd import _request as kbd_floater_request
from test_kbd import load

from rub_fixed_income import (
    RISK_BUCKETS,
    BondCashflow,
    BondRiskEngine,
    BondRiskRequest,
    BondScenario,
    DeliverableBond,
    DiscountCurve,
    InvalidTerms,
    OfzFuturesRiskEngine,
    OfzFuturesRiskRequest,
    TwoCurveBondRiskResult,
    ZSpreadConvention,
    benchmark_equivalents,
    kbd_discount_curve,
)
from rub_fixed_income import hedge_equivalents as hedge
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


def shifted(engine: BondRiskEngine, days: int) -> BondRiskEngine:
    """The same bond with every payment ``days`` later."""
    flows = tuple(
        BondCashflow(f.payment_date + timedelta(days=days), f.amount)
        for f in engine.request.cashflows
    )
    return BondRiskEngine(replace(engine.request, cashflows=flows))


def test_close_benchmarks_stay_well_conditioned_and_bad_inputs_are_refused() -> None:
    # Nodes sit at the benchmarks' own maturities, so J is nearly triangular: even a day
    # apart the system is well conditioned and the hedges do not blow up.
    for days in (1, 7, 30):
        close = (*BENCHMARKS, ("B5b", shifted(bond(5.5, 0.15, 985.0), days)))
        result = benchmark_equivalents(close, [bond(6, 0.10, 860.0)])
        assert result.condition_number < 10 and result.warnings == ()
        gross = sum(abs(e.risk_rub_per_bp) for e in result.equivalents)
        assert gross < 1.3 * abs(result.total_risk_rub_per_bp)
    with pytest.raises(InvalidTerms, match="must differ"):
        benchmark_equivalents((*BENCHMARKS, ("B7b", bond(7, 0.10, 900.0))), [])
    with pytest.raises(InvalidTerms, match="same curve"):
        other = kbd_discount_curve(PARAMS, zero_shock="annual")
        benchmark_equivalents(BENCHMARKS, [bond(6, 0.1, 860.0, curve=other)])
    # A floater neither discounted nor projected on the benchmarks' curve (0.38.0).
    with pytest.raises(InvalidTerms, match="same curve"):
        benchmark_equivalents(BENCHMARKS, [BondRiskEngine(kbd_floater_request())])
    with pytest.raises(InvalidTerms, match="at least one"):
        benchmark_equivalents((), [])


def test_benchmark_quantity_is_ignored_even_at_zero() -> None:
    held = [bond(6, 0.10, 860.0, quantity=1e6)]
    reference = benchmark_equivalents(BENCHMARKS, held)
    for quantity in (0.0, -5.0, 1e9):
        scaled = tuple((name, bond(*TERMS[name], quantity=quantity)) for name, _ in BENCHMARKS)
        result = benchmark_equivalents(scaled, held)
        for a, b in zip(result.equivalents, reference.equivalents, strict=True):
            assert a.risk_rub_per_bp == pytest.approx(b.risk_rub_per_bp, rel=1e-12, abs=1e-12)
            assert a.yield_dv01_rub == b.yield_dv01_rub


def zero_bond(years: float, quantity: float = 1.0) -> BondRiskEngine:
    maturity = add_months(DATE, round(years * 12))
    return BondRiskEngine(
        BondRiskRequest(
            (BondCashflow(maturity, 1000.0),),
            DATE + timedelta(days=1),
            KBD,
            ZSpreadConvention(),
            "ofz",
            dirty_price_rub=1000.0 * 1.16**-years,
            quantity=quantity,
        )
    )


def test_positions_outside_the_benchmarks_are_flagged() -> None:
    # Before the first benchmark the overlay is flat: a 1Y bill is all on the 2Y.
    short = benchmark_equivalents(BENCHMARKS, [bond(6, 0.10, 860.0), zero_bond(1)])
    first = BENCHMARKS[0][1].request.cashflows[-1].payment_date
    assert short.warnings == (
        f"positions 1 mature before the first benchmark B2 ({first}): their risk is carried by it",
    )
    # Past the last benchmark too: a 20Y bond's tail is carried by the 14Y benchmark.
    long = benchmark_equivalents(BENCHMARKS, [bond(20, 0.12, 750.0), bond(6, 0.10, 860.0)])
    assert len(long.warnings) == 1 and long.warnings[0].startswith("positions 0 pay after")
    assert "B15" in long.warnings[0]
    # A coupon bond between benchmarks maps at about its own risk; nothing to flag.
    inside = benchmark_equivalents(BENCHMARKS, [bond(9, 0.11, 830.0), bond(3, 0.1, 960.0)])
    assert inside.warnings == ()


def test_hedges_that_amplify_the_risk_are_flagged() -> None:
    """Coupon-heavy benchmarks hedge a zero with large opposite-sign neighbours."""
    rich = (
        ("L10", bond(10, 0.25, 1300.0)),
        ("L11", bond(11, 0.25, 1310.0)),
        ("L12", bond(12, 0.25, 1320.0)),
    )
    result = benchmark_equivalents(rich, [zero_bond(11, quantity=1e4)])
    by_id = {e.instrument_id: e.risk_rub_per_bp for e in result.equivalents}
    assert by_id["L10"] > 0 > by_id["L11"]
    assert abs(by_id["L11"]) > 3 * abs(result.total_risk_rub_per_bp)
    assert (
        len(result.warnings) == 1 and "amplify the positions' risk 5.9 times" in result.warnings[0]
    )
    # The condition number stays small: it does not see this.
    assert result.condition_number < 10


# -- floaters and OFZ futures (0.38.0)

ANNUAL_KBD = kbd_discount_curve(PARAMS, zero_shock="annual")
PILLAR_YEARS = (2, 3, 5, 7, 10, 15)
INTERIOR = ("3Y", "5Y", "7Y", "10Y")


def pillar_bond(years: int, coupon: float = 0.12) -> BondRiskEngine:
    """An annual-coupon bond maturing exactly at the ``years`` risk pillar (ACT/365F)."""
    flows = [
        BondCashflow(DATE + timedelta(days=365 * k), 1000.0 * coupon) for k in range(1, 1 + years)
    ]
    flows[-1] = BondCashflow(flows[-1].payment_date, 1000.0 * (1.0 + coupon))
    return BondRiskEngine(
        BondRiskRequest(tuple(flows), DATE, ANNUAL_KBD, ZSpreadConvention(), "ofz", spread_bp=20.0)
    )


PILLAR_BENCHMARKS = tuple((f"P{y}", pillar_bond(y)) for y in PILLAR_YEARS)
"""Benchmarks at the 2Y-15Y pillars: between them the overlay's hats are the buckets' own."""


def floater(
    two_curve: bool, curve: DiscountCurve = KBD, quantity: float = 1000.0
) -> BondRiskEngine:
    """The KBD floater of ``test_floating_kbd``: single-curve on ``curve``, or projected off it
    and discounted on RUONIA."""
    request = kbd_floater_request(curve)
    if not two_curve:
        request = replace(request, curve=curve, projection_curve=None)
    return BondRiskEngine(replace(request, quantity=quantity))


def futures(
    funding_on_curve: bool, curve: DiscountCurve, quantity: float = -7.0
) -> OfzFuturesRiskEngine:
    basket = ((ofz.SHORT, 1.0, 98.0), (ofz.LONG, 1.1, 104.0))
    return OfzFuturesRiskEngine(
        OfzFuturesRiskRequest(
            ofz.CONTRACT,
            tuple(DeliverableBond(i, cf, px) for i, cf, px in basket),
            ofz.SPOT,
            curve,
            curve if funding_on_curve else DiscountCurve.flat(DATE, 0.15),
            basis_bp=0.0,
            quantity=quantity,
        )
    )


def interior_gap(engine: BondRiskEngine | OfzFuturesRiskEngine, buckets: list[float]) -> float:
    """Relative gap of the node risk at the interior pillars to the engine's bumped buckets."""
    nodes = np.array([float(y) for y in PILLAR_YEARS])
    exposure = hedge._exposure(engine, ANNUAL_KBD, hedge._identity(ANNUAL_KBD), nodes)
    node = exposure.gradient[1:5]
    bumped = np.array([buckets[RISK_BUCKETS.index(b)] for b in INTERIOR])
    return float(np.max(np.abs(node - bumped)) / np.max(np.abs(bumped)))


def test_floater_and_futures_node_risk_is_the_bumped_bucket_risk() -> None:
    """At an interior benchmark the overlay's hat is the bucket's: the node risk must be the
    engine's own central bucket DV01, coupons re-projected and the CTD repriced."""
    single = floater(False, ANNUAL_KBD)
    risk = single.risk(difference="central")
    assert interior_gap(single, [b.dv01_rub for b in risk.buckets]) < 1e-6
    two = floater(True, ANNUAL_KBD)
    risk = two.risk(difference="central")
    assert isinstance(risk, TwoCurveBondRiskResult)
    assert interior_gap(two, [b.dv01_rub for b in risk.projection_buckets]) < 1e-6
    for funding in (False, True):
        future = futures(funding, ANNUAL_KBD)
        bumped = future.risk(difference="central")
        buckets = [
            b.dv01_rub + (f.dv01_rub if funding else 0.0)
            for b, f in zip(bumped.bond_buckets, bumped.funding_buckets, strict=True)
        ]
        assert interior_gap(future, buckets) < 1e-6
        result = benchmark_equivalents(PILLAR_BENCHMARKS, [future])
        parallel = bumped.bond_parallel_dv01_rub + (
            bumped.funding_parallel_dv01_rub if funding else 0.0
        )
        assert result.total_risk_rub_per_bp == pytest.approx(parallel, rel=0.02)
        held = [w for w in result.warnings if "other than the benchmarks'" in w]
        assert len(held) == (0 if funding else 1)


def test_a_kbd_floater_is_hedged_in_benchmarks() -> None:
    for two_curve in (False, True):
        position = floater(two_curve)
        result = benchmark_equivalents(BENCHMARKS, [position])
        risk = position.risk(difference="central")
        kbd = (
            sum(b.dv01_rub for b in risk.projection_buckets)
            if isinstance(risk, TwoCurveBondRiskResult)
            else risk.parallel_dv01_rub
        )
        # The coupon rises with the long end, falls with the short end: a steepener.
        by_id = {e.instrument_id: e.risk_rub_per_bp for e in result.equivalents}
        assert by_id["B2"] < 0 < by_id["B10"]
        assert result.total_risk_rub_per_bp == pytest.approx(kbd, rel=0.05)
        expected = (
            (
                "positions 0 also depend on a curve other than the benchmarks': it is held, its risk is not in this view",
            )
            if two_curve
            else ()
        )
        assert result.warnings == expected
        hedges = [
            bond(*TERMS[e.instrument_id], quantity=e.hedge_quantity) for e in result.equivalents
        ]
        hedged = benchmark_equivalents(BENCHMARKS, [position, *hedges])
        gross = sum(abs(e.risk_rub_per_bp) for e in result.equivalents)
        assert all(abs(e.risk_rub_per_bp) < 1e-9 * gross for e in hedged.equivalents)


def test_products_off_the_benchmarks_curve_are_refused() -> None:
    other = kbd_discount_curve(PARAMS, zero_shock="annual")
    with pytest.raises(InvalidTerms, match="same curve"):
        benchmark_equivalents(BENCHMARKS, [floater(False, other)])
    with pytest.raises(InvalidTerms, match="same curve"):
        benchmark_equivalents(BENCHMARKS, [futures(False, other)])
    with pytest.raises(TypeError, match="OfzFuturesRiskEngine"):
        benchmark_equivalents(BENCHMARKS, ["SU26238"])  # type: ignore[list-item]
    # An equal curve built apart is the same curve.
    twin = kbd_discount_curve(PARAMS, zero_shock="annual_par")
    assert twin is not KBD
    result = benchmark_equivalents(BENCHMARKS, [bond(6, 0.10, 860.0, curve=twin)])
    reference = benchmark_equivalents(BENCHMARKS, [bond(6, 0.10, 860.0)])
    assert result == reference
