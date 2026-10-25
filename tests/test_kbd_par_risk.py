"""Par-yield bucket shocks of the KBD (0.37.0, ``zero_shock="annual_par"``, ``rub_ofz_kbd_v3``)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import date

import numpy as np
import pytest
from test_floating_kbd import _request as kbd_floater_request
from test_fx_forwards import request as fx_forward_request
from test_kbd import load
from test_ofz_futures import engine as ofz_futures_engine
from test_portfolio import PARAMS as PORTFOLIO_PARAMS
from test_portfolio import run, snapshot

from rub_fixed_income import (
    RISK_BUCKETS,
    BondCashflow,
    BondRiskEngine,
    BondRiskRequest,
    BondScenario,
    CurveConvention,
    CurveEntry,
    FxForwardRiskEngine,
    FxFuturesScenario,
    InvalidTerms,
    OfzFuturesRiskEngine,
    OfzFuturesScenario,
    PortfolioScenario,
    TwoCurveBondRiskResult,
    ZSpreadConvention,
    kbd_discount_curve,
)
from rub_fixed_income._analytic_risk import _par_map
from rub_fixed_income._par_overlay import (
    PAR_YEARS,
    _instruments,
    par_jacobians,
    par_overlay_shifts,
    par_rates,
)
from rub_fixed_income._risk_common import _shock_curve, key_rate_weights
from rub_fixed_income.dates import add_months
from rub_fixed_income.discount import DiscountCurve, DiscountFactorCurve
from rub_fixed_income.risk import ShockedCurveCache

PARAMS, _ = load("zcyc_2026-10-15.json")
DATE = PARAMS.trade_date
PAR = kbd_discount_curve(PARAMS, zero_shock="annual_par")
ANNUAL = kbd_discount_curve(PARAMS, zero_shock="annual")


def par_bond(years: int, coupon: float, curve: DiscountCurve = PAR) -> BondRiskEngine:
    """The par instrument of a bucket as a bond: annual coupon, ACT/365F, from the curve date."""
    dates = [add_months(DATE, 12 * k) for k in range(1, years + 1)]
    previous = [DATE, *dates[:-1]]
    flows = [
        BondCashflow(d, 1000.0 * coupon * (d - p).days / 365.0) for p, d in zip(previous, dates)
    ]
    flows[-1] = BondCashflow(dates[-1], flows[-1].amount + 1000.0)
    request = BondRiskRequest(
        tuple(flows),
        DATE,
        curve,
        ZSpreadConvention(),
        "ofz",
        spread_bp=0.0,
    )
    return BondRiskEngine(request)


def par_yield(curve: DiscountFactorCurve, years: int) -> float:
    dates = [add_months(DATE, 12 * k) for k in range(1, years + 1)]
    previous = [DATE, *dates[:-1]]
    annuity = sum((d - p).days / 365.0 * curve.discount_factor(d) for p, d in zip(previous, dates))
    return (1.0 - curve.discount_factor(dates[-1])) / annuity


def test_convention_and_basis() -> None:
    assert PAR.nodes == ANNUAL.nodes
    assert PAR.convention.identifier.endswith(":shock=annual_par")
    assert CurveConvention(zero_shock="annual_par").zero_shock == "annual_par"
    engine = par_bond(5, 0.15)
    assert engine.curve_basis == "annual_par"
    assert engine.base_id != par_bond(5, 0.15, ANNUAL).base_id


def test_shocked_curve_reprices_every_par_instrument_at_its_shifted_yield() -> None:
    shifts = (7.0, -3.0, 11.0, 25.0, -40.0, 10.0, 60.0, -15.0, 5.0, 33.0, -8.0)
    view = _shock_curve(PAR, PAR, shifts, ShockedCurveCache())
    for k, (years, (_, before)) in enumerate(zip(PAR_YEARS, par_rates(PAR), strict=True)):
        assert par_yield(PAR, years) == pytest.approx(before, abs=1e-15)
        assert (par_yield(view, years) - before) * 1e4 == pytest.approx(shifts[3 + k], abs=1e-8)
    # The short buckets have no instrument: they move nothing, and the 1Y node holds the
    # short end (the zero overlay is flat below 1Y).
    zero = par_overlay_shifts(PAR, shifts)
    assert zero[:3] == (zero[3],) * 3
    assert par_overlay_shifts(PAR, (5.0, 5.0, 5.0) + (0.0,) * 8) == (0.0,) * 11
    short_only = _shock_curve(PAR, PAR, (5.0, 5.0, 5.0) + (0.0,) * 8, ShockedCurveCache())
    for day in (date(2026, 11, 15), date(2027, 3, 1), date(2031, 1, 1)):
        assert short_only.discount_factor(day) == PAR.discount_factor(day)
    # A bump of one bucket leaves the zero overlay before its tenor all but untouched (the
    # 5Y instrument matures a leap day past 5.0 years, so it reads the 7Y node a little).
    seven = par_overlay_shifts(PAR, tuple(1.0 if b == "7Y" else 0.0 for b in RISK_BUCKETS))
    assert all(abs(v) < 0.005 for v in seven[: RISK_BUCKETS.index("7Y")])
    assert seven[RISK_BUCKETS.index("7Y")] > 1.0 and seven[RISK_BUCKETS.index("10Y")] < 0.0


@pytest.mark.parametrize("years", PAR_YEARS)
def test_a_par_instrument_has_risk_in_its_own_bucket_only(years: int) -> None:
    (_, rate) = par_rates(PAR)[PAR_YEARS.index(years)]
    risk = par_bond(years, rate).risk(difference="central")
    bucket = 3 + PAR_YEARS.index(years)
    own = risk.buckets[bucket].dv01_rub
    assert own < 0
    for i, b in enumerate(risk.buckets):
        if i != bucket:
            assert abs(b.dv01_rub) < 1e-6 * abs(own)
    # Its par DV01 is the annuity: d price / d par yield at par.
    annuity = par_bond(years, 1.0).base_price_rub - par_bond(years, 0.0).base_price_rub
    assert own == pytest.approx(-annuity * 1e-4, rel=1e-6)


def test_bucket_sum_is_the_parallel_risk_and_close_to_annual_zero() -> None:
    for years, coupon in ((2, 0.08), (6, 0.12), (13, 0.07)):
        par = par_bond(years, coupon).risk(difference="central")
        zero = par_bond(years, coupon, ANNUAL).risk(difference="central")
        assert par.dv01_rub == pytest.approx(par.parallel_dv01_rub, rel=1e-4)
        assert par.dv01_rub == pytest.approx(zero.dv01_rub, rel=0.03)
        assert sum(b.dv01_rub for b in par.buckets[:3]) == 0.0


def test_a_kbd_floater_reprojects_exactly_under_par_shocks() -> None:
    """A par shock moves zero nodes past its own: the coupon reuse is keyed by them."""
    engine = BondRiskEngine(kbd_floater_request(PAR))
    risk = engine.risk(difference="central")
    assert isinstance(risk, TwoCurveBondRiskResult)
    assert risk.projection_curve_basis == "annual_par"
    every = BondRiskEngine(kbd_floater_request(PAR))
    object.__setattr__(
        every, "_reuse", tuple(replace(r, buckets=tuple(range(11))) for r in every._reuse)
    )
    full = every.risk(difference="central")
    assert isinstance(full, TwoCurveBondRiskResult)
    for a, b in zip(risk.projection_buckets, full.projection_buckets, strict=True):
        assert a.dv01_rub == pytest.approx(b.dv01_rub, rel=1e-12, abs=1e-9)
    scenario = BondScenario((0, 0, 0, 0, 0, 30.0, 0, 0, 0, 0, 0))
    assert engine.reprice(scenario).total_pnl_rub == pytest.approx(
        every.reprice(scenario).total_pnl_rub, rel=1e-12
    )


def rebuilt_price(flows: tuple[BondCashflow, ...], par_shifts: tuple[float, ...]) -> float:
    """PV on the curve rebuilt for ``par_shifts`` (bp at 1Y..20Y+), apart from the engines:
    the overlay re-solved by ``par_overlay_shifts`` and applied by hand."""
    zero = par_overlay_shifts(PAR, (0.0,) * 3 + par_shifts) if any(par_shifts) else (0.0,) * 11
    total = 0.0
    for flow in flows:
        time = (flow.payment_date - DATE).days / 365.0
        move = sum(w * x for w, x in zip(key_rate_weights(time), zero)) * 1e-4
        total += (
            flow.amount * (PAR.discount_factor(flow.payment_date) ** (-1 / time) + move) ** -time
        )
    return total


def rebuilt_derivatives(flows: tuple[BondCashflow, ...]) -> tuple[np.ndarray, np.ndarray]:
    """Gradient and Hessian over the eight par buckets by bump and rebuild: central
    differences of 5 and 10 bp, Richardson-extrapolated (error O(bump^4)). Bumps this size
    keep the solver's 1e-13 tolerance on the par yields below 1e-8 of the Hessian."""
    n = len(PAR_YEARS)

    def price(moves: dict[int, float]) -> float:
        return rebuilt_price(flows, tuple(moves.get(i, 0.0) for i in range(n)))

    base = price({})

    def central(h: float) -> tuple[np.ndarray, np.ndarray]:
        grad, hess = np.zeros(n), np.zeros((n, n))
        for i in range(n):
            up, down = price({i: h}), price({i: -h})
            grad[i] = (up - down) / (2 * h)
            hess[i, i] = (up - 2 * base + down) / h**2
            for j in range(i):
                corners = [
                    price({i: a * h, j: b * h}) for a, b in ((1, 1), (1, -1), (-1, 1), (-1, -1))
                ]
                hess[i, j] = hess[j, i] = (corners[0] - corners[1] - corners[2] + corners[3]) / (
                    4 * h**2
                )
        return grad, hess

    (g1, h1), (g2, h2) = central(5.0), central(10.0)
    return (4 * g1 - g2) / 3, (4 * h1 - h2) / 3


def test_second_order_is_the_par_bump_and_rebuild() -> None:
    """The closed form against an independent bump and rebuild of the par curve."""
    engine = par_bond(9, 0.11)
    grad, hess = rebuilt_derivatives(engine.request.cashflows)
    exact = engine.second_order(method="analytic")
    g_a = np.array(exact.gradient_rub_per_bp[3:11])
    h_a = np.array(exact.hessian_rub_per_bp2)[3:11, 3:11]
    assert np.max(np.abs(g_a - grad)) < 1e-7 * np.max(np.abs(grad))
    assert np.max(np.abs(h_a - hess)) < 2e-7 * np.max(np.abs(hess))
    # The bumped engine path shares the solver but not the pricing: O(bump^2) apart.
    bumped = np.array(engine.second_order().hessian_rub_per_bp2)[3:11, 3:11]
    assert np.max(np.abs(bumped - hess)) < 2e-6 * np.max(np.abs(hess))


@pytest.mark.parametrize("years", [2, 7, 15])
def test_a_par_instrument_has_the_gamma_of_its_annuity(years: int) -> None:
    """Not zero: ``V = 1 + (c - q_l) A(q)`` gives ``d2V/dq_l dq_k = -(1 + [k = l]) dA/dq_k``."""
    (_, rate) = par_rates(PAR)[PAR_YEARS.index(years)]
    engine = par_bond(years, rate)
    h_q = np.array(engine.second_order(method="analytic").hessian_rub_per_bp2)[3:11, 3:11]
    unit = par_bond(years, 1.0).request.cashflows
    coupons = tuple(
        BondCashflow(f.payment_date, f.amount - (1000.0 if f is unit[-1] else 0.0)) for f in unit
    )
    own = PAR_YEARS.index(years)
    expected = np.zeros((len(PAR_YEARS), len(PAR_YEARS)))
    for k in range(len(PAR_YEARS)):
        step = tuple(1.0 if i == k else 0.0 for i in range(len(PAR_YEARS)))
        down = tuple(-v for v in step)
        slope = (rebuilt_price(coupons, step) - rebuilt_price(coupons, down)) / 2 * 1e-4  # dA/dq
        expected[own, k] -= slope
        expected[k, own] -= slope
    assert np.max(np.abs(h_q - expected)) < 1e-6 * np.max(np.abs(expected))
    assert h_q[own, own] > 0.0  # a long par bond is long convexity in its own quote


def test_portfolio_kbd_v3_spec() -> None:
    v3 = snapshot(curves=(CurveEntry("RUB_OFZ_KBD", "rub_ofz_kbd_v3", PORTFOLIO_PARAMS),))
    v2 = snapshot(curves=(CurveEntry("RUB_OFZ_KBD", "rub_ofz_kbd_v2", PORTFOLIO_PARAMS),))
    new = next(p for p in run(snapshot=v3).positions if p.position_id == "p-bullet")
    old = next(p for p in run(snapshot=v2).positions if p.position_id == "p-bullet")
    assert (new.curve_basis, new.spec_id) == ("annual_par", "rub_ofz_kbd_v3")
    assert new.pv_rub == old.pv_rub and new.spread_bp == old.spread_bp
    # Par risk is the annual-zero risk mapped through the par instruments, g_q = J^-T E^T g_x;
    # both are central 1 bp differences, so they agree to O(bump^2), 2e-8 here.
    g_q = np.array(new.factor_risk.first_order[:11])
    g_x = np.array(old.factor_risk.first_order[:11])
    jacobian, _ = par_jacobians(kbd_discount_curve(PORTFOLIO_PARAMS, zero_shock="annual_par"))
    mapped = np.linalg.inv(jacobian).T @ (_par_map().T @ g_x)
    assert g_q[:3].tolist() == [0.0, 0.0, 0.0]
    assert np.max(np.abs(g_q[3:] - mapped)) < 1e-6 * np.max(np.abs(g_q))
    # The parallel par move is 1.4 % larger than the parallel zero move for this bond.
    assert g_q.sum() == pytest.approx(g_x.sum(), rel=0.02)
    # A prebuilt annual-zero KBD does not satisfy the v3 spec.
    prebuilt = snapshot(
        curves=(
            CurveEntry(
                "RUB_OFZ_KBD",
                "rub_ofz_kbd_v3",
                kbd_discount_curve(PORTFOLIO_PARAMS, zero_shock="annual"),
            ),
        )
    )
    codes = {r.position_id: r.code for r in run(snapshot=prebuilt).coverage.rejections}
    assert codes["p-bullet"] == "INVALID_TERMS"


def plain_newton(curve: DiscountCurve, par_shifts: tuple[float, ...]) -> tuple[float, ...]:
    """The undamped Newton of 0.37.0, for the bit-for-bit check of realistic shocks."""
    instruments = _instruments(curve)
    zero = np.zeros(len(PAR_YEARS))
    target = np.array([i.par(zero) for i in instruments]) + np.array(par_shifts) * 1e-4
    x = zero.copy()
    for _ in range(30):
        rows = [i.derivatives(x) for i in instruments]
        residual = np.array([r[0] for r in rows]) - target
        if np.max(np.abs(residual)) < 1e-13:
            return (float(x[0]),) * 3 + tuple(float(v) for v in x)
        x = x - np.linalg.solve(np.array([r[1] for r in rows]), residual)
    raise AssertionError("plain Newton did not converge")


REALISTIC = {
    "2022 bear flattener": (1000.0, 900.0, 800.0, 700.0, 600.0, 500.0, 450.0, 400.0),
    "twice 2022": (2000.0, 1800.0, 1600.0, 1400.0, 1200.0, 1000.0, 900.0, 800.0),
    "2022 rally": (-1000.0, -900.0, -800.0, -700.0, -600.0, -500.0, -450.0, -400.0),
    "steepener": (100.0, 150.0, 200.0, 250.0, 300.0, 350.0, 375.0, 400.0),
    "hump": (150.0, 250.0, 350.0, 300.0, 250.0, 200.0, 150.0, 150.0),
    "parallel -1500": (-1500.0,) * 8,
}


@pytest.mark.parametrize("name", sorted(REALISTIC))
@pytest.mark.parametrize("fixture", ["zcyc_2022-01-03.json", "zcyc_2026-10-15.json"])
def test_realistic_shocks_take_full_newton_steps(name: str, fixture: str) -> None:
    curve = kbd_discount_curve(load(fixture)[0], zero_shock="annual_par")
    shifts = REALISTIC[name]
    assert par_overlay_shifts(curve, (shifts[0],) * 3 + shifts) == plain_newton(curve, shifts)


def test_extreme_par_shocks_converge_or_say_they_cannot() -> None:
    curve = kbd_discount_curve(PARAMS, zero_shock="annual_par")
    # Undamped, the first step overshoots below -100 %; damped, it converges.
    down = (0.0,) * 10 + (-1500.0,)
    view = _shock_curve(curve, curve, down, ShockedCurveCache())
    for k, (years, (_, before)) in enumerate(zip(PAR_YEARS, par_rates(curve), strict=True)):
        assert (par_yield(view, years) - before) * 1e4 == pytest.approx(down[3 + k], abs=1e-8)
    with pytest.raises(ValueError, match="shocked annual yield must exceed -100 %"):
        plain_newton(curve, down[3:])
    # The overlay cannot put the 20Y+ par yield 1500 bp above the 15Y one (at most ~180 bp
    # on this 16 % curve), nor a saw-tooth of +-500 bp: a clear miss, not a domain error.
    for shifts, instrument in (
        ((0.0,) * 10 + (1500.0,), "20Y"),
        ((0.0,) * 3 + (500.0, -500.0) * 4, "15Y"),
        ((0.0, 0.0, 0.0, -20000.0) + (0.0,) * 7, "1Y"),
    ):
        with pytest.raises(
            ValueError, match=f"did not converge.*the {instrument} instrument is [-+][0-9.]+ bp off"
        ):
            par_overlay_shifts(curve, shifts)
    # Within the reach a saw-tooth converges and reprices exactly.
    saw = (0.0,) * 3 + (200.0, -200.0) * 4
    view = _shock_curve(curve, curve, saw, ShockedCurveCache())
    for k, (years, (_, before)) in enumerate(zip(PAR_YEARS, par_rates(curve), strict=True)):
        assert (par_yield(view, years) - before) * 1e4 == pytest.approx(saw[3 + k], abs=1e-8)


def test_scenarios_may_move_the_short_buckets_only_with_1y() -> None:
    engine = par_bond(5, 0.12)
    # A parallel shift moves 1M-6M with 1Y: valid, and the same as leaving them at zero.
    parallel = engine.reprice(BondScenario.parallel(50.0)).total_pnl_rub
    long_end = BondScenario((0.0, 0.0, 0.0) + (50.0,) * 8)
    assert engine.reprice(long_end).total_pnl_rub == parallel
    front = BondScenario((30.0, 30.0, 30.0, 30.0) + (0.0,) * 7)
    assert (
        engine.reprice(front).total_pnl_rub
        == engine.reprice(BondScenario((0.0, 0.0, 0.0, 30.0) + (0.0,) * 7)).total_pnl_rub
    )
    with pytest.raises(InvalidTerms, match=r"1M \+5 bp"):
        engine.reprice(BondScenario((5.0,) + (0.0,) * 10))
    with pytest.raises(
        InvalidTerms, match=r"equal to the 1Y shift \(\+15 bp\), got 3M \+10, 6M \+20"
    ):
        engine.approximate(BondScenario((15.0, 10.0, 20.0, 15.0) + (0.0,) * 7))
    # Risk bumps of the short buckets are not scenarios: they read zero.
    assert [b.dv01_rub for b in engine.risk(difference="central").buckets[:3]] == [0.0] * 3
    # Other curves take any shifts.
    annual = BondRiskEngine(replace(par_bond_short().request, curve=ANNUAL))
    assert annual.reprice(BondScenario((0.0, 0.0, 5.0) + (0.0,) * 8)).total_pnl_rub < 0.0


def test_a_portfolio_scenario_with_short_shifts_fails_the_v3_positions_only() -> None:
    legacy = PortfolioScenario("legacy front end", BondScenario((50.0, 40.0, 30.0) + (20.0,) * 8))
    parallel = PortfolioScenario("parallel", BondScenario.parallel(20.0))
    v3 = snapshot(curves=(CurveEntry("RUB_OFZ_KBD", "rub_ofz_kbd_v3", PORTFOLIO_PARAMS),))
    result = run(snapshot=v3, scenarios=(legacy, parallel))
    failed, fine = result.scenarios
    bullet = next(p for p in failed.positions if p.position_id == "p-bullet")
    assert bullet.pnl_rub is None
    message = next(r.message for r in failed.coverage.rejections if r.position_id == "p-bullet")
    assert "no instrument below 1Y" in message and "1M +50, 3M +40, 6M +30" in message
    assert next(p for p in fine.positions if p.position_id == "p-bullet").pnl_rub is not None


def amortising(curve: DiscountCurve = PAR) -> BondRiskEngine:
    """Eight years, semiannual coupons of 12 % on the outstanding, 1/8 of par redeemed a year."""
    flows, outstanding = [], 1000.0
    for k in range(1, 17):
        amount = outstanding * 0.06
        if k % 2 == 0:
            amount += 125.0
            outstanding -= 125.0
        flows.append(BondCashflow(add_months(DATE, 6 * k), amount))
    return BondRiskEngine(
        BondRiskRequest(tuple(flows), DATE, curve, ZSpreadConvention(), "ofz", spread_bp=0.0)
    )


@pytest.mark.parametrize(
    "make",
    [
        pytest.param(lambda: par_bond_short(), id="9M"),
        pytest.param(lambda: par_bond(25, 0.10), id="25Y"),
        pytest.param(amortising, id="amortising"),
    ],
)
def test_v3_risk_off_the_par_grid_is_the_par_bump_and_rebuild(
    make: Callable[[], BondRiskEngine],
) -> None:
    """Shorter than 1Y, longer than 20Y, amortising: exact against the rebuilt curve."""
    engine = make()
    flows = engine.request.cashflows
    assert engine.base_price_rub == pytest.approx(rebuilt_price(flows, (0.0,) * 8), rel=1e-12)
    grad, hess = rebuilt_derivatives(flows)
    exact = engine.second_order(method="analytic")
    g_a = np.array(exact.gradient_rub_per_bp[3:11])
    h_a = np.array(exact.hessian_rub_per_bp2)[3:11, 3:11]
    assert exact.gradient_rub_per_bp[:3] == (0.0, 0.0, 0.0)
    assert np.max(np.abs(g_a - grad)) < 1e-7 * np.max(np.abs(grad))
    assert np.max(np.abs(h_a - hess)) < 2e-7 * np.max(np.abs(hess))
    risk = engine.risk(difference="central")
    assert risk.dv01_rub == pytest.approx(risk.parallel_dv01_rub, rel=1e-4)
    shifts = (60.0, -20.0, 35.0, 10.0, -15.0, 25.0, 40.0, 30.0)
    pnl = engine.reprice(BondScenario((shifts[0],) * 3 + shifts)).total_pnl_rub
    assert pnl == pytest.approx(
        rebuilt_price(flows, shifts) - rebuilt_price(flows, (0.0,) * 8), rel=1e-10
    )


def par_bond_short() -> BondRiskEngine:
    """Nine months, one coupon: all of its risk is on the 1Y instrument."""
    flows = (BondCashflow(add_months(DATE, 9), 1000.0 + 1000.0 * 0.15 * 0.75),)
    return BondRiskEngine(
        BondRiskRequest(flows, DATE, PAR, ZSpreadConvention(), "ofz", spread_bp=0.0)
    )


def test_a_bond_shorter_than_1y_has_risk_on_the_1y_instrument_only() -> None:
    engine = par_bond_short()
    buckets = [b.dv01_rub for b in engine.risk(difference="central").buckets]
    assert buckets[:3] == [0.0, 0.0, 0.0] and buckets[3] < 0.0
    assert all(abs(v) < 1e-9 * abs(buckets[3]) for v in buckets[4:])
    # The overlay is flat below 1Y at the 1Y node: dq_1Y = J_11 dx, so the annual-zero risk
    # of the 1M-1Y buckets over J_11 is the par risk.
    zero = BondRiskEngine(replace(engine.request, curve=ANNUAL)).risk(difference="central")
    jacobian, _ = par_jacobians(PAR)
    short = sum(b.dv01_rub for b in zero.buckets[:4])
    assert buckets[3] == pytest.approx(short / jacobian[0, 0], rel=1e-7)


def test_a_bond_longer_than_20y_loads_the_20y_instrument() -> None:
    """Flows past 20 years ride the 20Y+ node: long the 20Y+ par bucket, short the rest."""
    risk = par_bond(25, 0.10).risk(difference="central")
    buckets = [b.dv01_rub for b in risk.buckets]
    assert buckets[-1] / risk.dv01_rub > 1.0
    assert all(v >= 0.0 for v in buckets[3:-1]) and buckets[:3] == [0.0, 0.0, 0.0]


def test_derivative_scenarios_on_a_par_curve_follow_the_same_rule() -> None:
    """Every engine checks each par-yield curve it shifts in a scenario, in any role."""
    par_flat = DiscountCurve.flat(
        ofz_futures_engine().request.bond_curve.curve_date,
        0.14,
        convention=CurveConvention(zero_shock="annual_par"),
    )
    futures = OfzFuturesRiskEngine(replace(ofz_futures_engine().request, bond_curve=par_flat))
    stale = (25.0,) + (0.0,) * 10
    with pytest.raises(InvalidTerms, match="1M \\+25"):
        futures.reprice(OfzFuturesScenario(bond_curve_shifts_bp=stale))
    # The funding curve is not a par curve: any shifts.
    assert futures.reprice(OfzFuturesScenario(funding_curve_shifts_bp=stale)).total_pnl_rub != 0.0
    assert futures.reprice(OfzFuturesScenario.parallel(bond_shift_bp=25.0)).total_pnl_rub != 0.0
    forward = FxForwardRiskEngine(
        replace(
            fx_forward_request(),
            rub_curve=replace(
                fx_forward_request().rub_curve, convention=CurveConvention(zero_shock="annual_par")
            ),
        )
    )
    with pytest.raises(InvalidTerms, match="6M \\+25"):
        forward.reprice(FxFuturesScenario(rub_curve_shifts_bp=(0.0, 0.0, 25.0) + (0.0,) * 8))
    assert forward.reprice(FxFuturesScenario.parallel(rub_shift_bp=25.0)).total_pnl_rub != 0.0
