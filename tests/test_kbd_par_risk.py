"""Par-yield bucket shocks of the KBD (0.37.0, ``zero_shock="annual_par"``, ``rub_ofz_kbd_v3``)."""

from __future__ import annotations

from dataclasses import replace
from datetime import date

import pytest
from test_floating_kbd import _request as kbd_floater_request
from test_kbd import load
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
    TwoCurveBondRiskResult,
    ZSpreadConvention,
    kbd_discount_curve,
)
from rub_fixed_income._par_overlay import PAR_YEARS, par_overlay_shifts, par_rates
from rub_fixed_income._risk_common import _shock_curve
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


def test_second_order_is_the_par_bump_and_rebuild() -> None:
    engine = par_bond(9, 0.11)
    second = engine.second_order()
    hessian = second.hessian_rub_per_bp2
    for i, row in enumerate(hessian):
        for j, value in enumerate(row):
            assert value == pytest.approx(hessian[j][i], rel=1e-9, abs=1e-12)
    scenario = BondScenario((0, 0, 0, 0, 0, 0, 40.0, -25.0, 60.0, 0, 0), 10.0)
    approx = engine.approximate(scenario)
    assert abs(approx.residual_pnl_rub) < 0.02 * abs(approx.exact_pnl_rub)


def test_portfolio_kbd_v3_spec() -> None:
    v3 = snapshot(curves=(CurveEntry("RUB_OFZ_KBD", "rub_ofz_kbd_v3", PORTFOLIO_PARAMS),))
    v2 = snapshot(curves=(CurveEntry("RUB_OFZ_KBD", "rub_ofz_kbd_v2", PORTFOLIO_PARAMS),))
    new = next(p for p in run(snapshot=v3).positions if p.position_id == "p-bullet")
    old = next(p for p in run(snapshot=v2).positions if p.position_id == "p-bullet")
    assert (new.curve_basis, new.spec_id) == ("annual_par", "rub_ofz_kbd_v3")
    assert new.pv_rub == old.pv_rub and new.spread_bp == old.spread_bp
    rates_new = sum(new.factor_risk.first_order[:-1])
    rates_old = sum(old.factor_risk.first_order[:-1])
    assert rates_new == pytest.approx(rates_old, rel=0.03)
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
