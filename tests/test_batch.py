"""Vectorised history metrics (``rub_fixed_income.batch``) against the scalar functions."""

from __future__ import annotations

import math
from datetime import date, timedelta

import pytest
from test_portfolio import CALENDAR, PARAMS, bond, periods
from test_portfolio_floaters import OIS

from rub_fixed_income import (
    CONTINUOUS,
    BondCashflow,
    BondMetricsInput,
    DayCount,
    InvalidTerms,
    Offer,
    OfferKind,
    YieldConvention,
    YieldRequest,
    ZSpreadConvention,
    bond_duration,
    bond_metrics_batch,
    bootstrap_discount_curve,
    g_spread_bp,
    kbd_discount_curve,
    kbd_effective_yield,
    kbd_effective_yields,
    solve_effective_annual_yield,
    solve_z_spread,
)
from rub_fixed_income.dates import add_months

SETTLE = CALENDAR.add_business_days(PARAMS.trade_date, 1)
ACT_360 = YieldConvention(day_count=DayCount.ACT_360)


def book(size: int = 60) -> list[BondMetricsInput]:
    out = []
    for i in range(size):
        legs = periods(add_months(date(2026, 4, 15), -(i % 6)), 2 + (i % 29), 20.0 + 3.0 * (i % 11))
        offers = (Offer(legs[1].payment_date, OfferKind.PUT, True),) if i % 7 == 3 else ()
        instrument = bond(f"B{i:03d}", legs, offers=offers)
        out.append(
            BondMetricsInput.from_instrument(
                instrument,
                SETTLE,
                88.0 + (i % 17) * 1.5,
                horizon="confirmed_put" if offers else "maturity",
            )
        )
    return out


def scalar(inp: BondMetricsInput) -> tuple[float, float, float, float]:
    y = solve_effective_annual_yield(
        YieldRequest(inp.settlement_date, inp.dirty_price, inp.cashflows)
    ).effective_annual_yield
    pairs = tuple((f.payment_date, f.amount) for f in inp.cashflows)
    d = bond_duration(annual_yield=y, settlement_date=inp.settlement_date, cashflows=pairs)
    z = solve_z_spread(
        pairs, inp.dirty_price, kbd_discount_curve(PARAMS), settlement_date=inp.settlement_date
    )
    return y, d.macaulay_years, g_spread_bp(PARAMS, y, d.macaulay_years), z * 10_000.0


def test_batch_metrics_equal_the_scalar_functions() -> None:
    inputs = book()
    curve = kbd_discount_curve(PARAMS)
    on_curve = bond_metrics_batch(inputs, kbd=PARAMS, curve=curve)
    on_formula = bond_metrics_batch(inputs, kbd=PARAMS, curve=PARAMS)
    for inp, row, formula in zip(inputs, on_curve, on_formula, strict=True):
        y, d, g, z = scalar(inp)
        assert row.key == inp.key and row.error is None
        assert row.effective_yield == pytest.approx(y, abs=1e-12)
        assert row.macaulay_years == pytest.approx(d, abs=1e-10)
        assert row.modified_duration == pytest.approx(d / (1.0 + y), abs=1e-10)
        assert row.g_spread_bp == pytest.approx(g, abs=1e-7)
        assert row.z_spread_bp == pytest.approx(z, abs=1e-7)
        # The materialised curve interpolates the formula within 0.005 bp.
        assert formula.z_spread_bp == pytest.approx(z, abs=0.005)


def test_batch_on_a_bootstrapped_curve_and_other_conventions() -> None:
    inputs = book(20)
    curve = bootstrap_discount_curve(OIS)
    continuous = ZSpreadConvention(compounding=CONTINUOUS)
    rows = bond_metrics_batch(inputs, curve=curve, spread_convention=continuous)
    for inp, row in zip(inputs, rows, strict=True):
        pairs = tuple((f.payment_date, f.amount) for f in inp.cashflows)
        z = solve_z_spread(
            pairs, inp.dirty_price, curve, settlement_date=inp.settlement_date, convention=continuous
        )
        assert row.z_spread_bp == pytest.approx(z * 10_000.0, abs=1e-7)
        assert row.g_spread_bp is None
    act360 = bond_metrics_batch(inputs, yield_convention=ACT_360)
    for inp, row in zip(inputs, act360, strict=True):
        y = solve_effective_annual_yield(
            YieldRequest(inp.settlement_date, inp.dirty_price, inp.cashflows, ACT_360)
        ).effective_annual_yield
        assert row.effective_yield == pytest.approx(y, abs=1e-12)
        assert row.z_spread_bp is None


def test_unsolvable_bonds_get_none_and_a_reason_without_failing_the_batch() -> None:
    good = book(2)
    far = date(2030, 1, 1)
    absurd = BondMetricsInput(SETTLE, 1e12, (BondCashflow(far, 1000.0),), "ABSURD")
    rows = bond_metrics_batch([*good, absurd], kbd=PARAMS)
    assert rows[-1].effective_yield is None and rows[-1].error == "yield is not bracketed"
    with pytest.raises(ValueError, match="not bracketed"):  # as the scalar solver
        solve_effective_annual_yield(YieldRequest(SETTLE, 1e12, absurd.cashflows))
    assert all(r.error is None for r in rows[:-1])
    assert bond_metrics_batch([]) == ()


def test_inputs_are_validated() -> None:
    with pytest.raises(InvalidTerms):
        BondMetricsInput(SETTLE, 100.0, (BondCashflow(SETTLE, 1.0),))
    with pytest.raises(InvalidTerms):
        BondMetricsInput(SETTLE, 100.0, ())
    with pytest.raises(ValueError):
        BondMetricsInput(SETTLE, -1.0, (BondCashflow(SETTLE + timedelta(days=9), 1.0),))
    early = BondMetricsInput(
        PARAMS.trade_date - timedelta(days=3),
        990.0,
        (BondCashflow(date(2027, 1, 1), 1000.0),),
    )
    with pytest.raises(InvalidTerms, match="predate"):
        bond_metrics_batch([early], curve=PARAMS)


def test_kbd_effective_yields_match_the_scalar_formula() -> None:
    tenors = [1 / 365, 0.25, 0.5, 1.0, 2.0, 3.3, 7.0, 10.0, 15.5, 20.0, 30.0]
    vector = kbd_effective_yields(PARAMS, tenors)
    for tenor, value in zip(tenors, vector, strict=True):
        assert value == pytest.approx(kbd_effective_yield(PARAMS, tenor), abs=1e-15)
    assert kbd_effective_yields(PARAMS, []) == ()
    with pytest.raises(ValueError):
        kbd_effective_yields(PARAMS, [0.0])
    assert math.isfinite(vector[0])
