"""F1: invalid numeric input must raise instead of returning solver-domain garbage."""

from __future__ import annotations

import math
from datetime import date

import pytest

from rub_fixed_income import (
    BondCashflow,
    CurveBootstrapRequest,
    DiscountCurve,
    OisQuote,
    YieldRequest,
    bond_duration,
    bootstrap_discount_curve,
    price_cashflows,
    solve_continuous_z_spread,
    solve_effective_annual_yield,
    solve_nominal_annual_yield,
    solve_z_spread,
)

# These tests pin the behaviour of APIs deprecated in 0.12.0 (kept until 1.0);
# tests/test_deprecations.py checks that each one warns.
pytestmark = pytest.mark.filterwarnings("ignore:rub_fixed_income:DeprecationWarning")


SETTLE = date(2026, 1, 1)
FLOWS = ((date(2027, 1, 1), 50.0), (date(2028, 1, 1), 1050.0))
CURVE = DiscountCurve(SETTLE, ((date(2027, 1, 1), 0.9), (date(2028, 1, 1), 0.8)))
BAD = [math.nan, math.inf, -math.inf]


@pytest.mark.parametrize("price", [*BAD, 0.0, -1.0])
def test_z_spread_rejects_non_finite_or_non_positive_price(price: float) -> None:
    with pytest.raises(ValueError, match="dirty price"):
        solve_z_spread(FLOWS, price, CURVE)
    with pytest.raises(ValueError, match="dirty price"):
        solve_continuous_z_spread(FLOWS, price, CURVE)


@pytest.mark.parametrize("amount", [*BAD])
def test_z_spread_rejects_non_finite_cashflows(amount: float) -> None:
    with pytest.raises(ValueError, match="cashflow"):
        solve_z_spread(((date(2027, 1, 1), amount),), 900.0, CURVE)


@pytest.mark.parametrize("spread", BAD)
def test_price_cashflows_rejects_non_finite_spread(spread: float) -> None:
    with pytest.raises(ValueError, match="spread"):
        price_cashflows(FLOWS, CURVE, spread=spread)


def test_z_spread_unbracketed_price_raises_instead_of_returning_bound() -> None:
    with pytest.raises(ValueError, match="not bracketed"):
        solve_z_spread(FLOWS, 1e9, CURVE)


@pytest.mark.parametrize("price", [*BAD, 0.0, -5.0])
def test_nominal_yield_rejects_invalid_price(price: float) -> None:
    with pytest.raises(ValueError, match="dirty price"):
        solve_nominal_annual_yield(dirty_price=price, settlement_date=SETTLE, cashflows=FLOWS)


def test_nominal_yield_root_at_domain_edge_is_not_bracketed() -> None:
    with pytest.raises(ValueError, match="not bracketed"):
        solve_nominal_annual_yield(dirty_price=1e300, settlement_date=SETTLE, cashflows=FLOWS)


@pytest.mark.parametrize("amount", BAD)
def test_nominal_yield_rejects_non_finite_cashflows(amount: float) -> None:
    with pytest.raises(ValueError, match="cashflow"):
        solve_nominal_annual_yield(
            dirty_price=900.0,
            settlement_date=SETTLE,
            cashflows=((date(2027, 1, 1), amount), (date(2028, 1, 1), 1000.0)),
        )


@pytest.mark.parametrize("basis", [*BAD, 0.0])
def test_nominal_yield_rejects_invalid_day_count_basis(basis: float) -> None:
    with pytest.raises(ValueError):
        solve_nominal_annual_yield(
            dirty_price=900.0, settlement_date=SETTLE, cashflows=FLOWS, day_count_basis=basis
        )


@pytest.mark.parametrize("annual_yield", BAD)
def test_bond_duration_rejects_non_finite_yield(annual_yield: float) -> None:
    with pytest.raises(ValueError, match="yield"):
        bond_duration(annual_yield=annual_yield, settlement_date=SETTLE, cashflows=FLOWS)


@pytest.mark.parametrize("price", [*BAD, 0.0])
def test_effective_yield_rejects_invalid_price(price: float) -> None:
    request = YieldRequest(SETTLE, price, tuple(BondCashflow(d, a) for d, a in FLOWS))
    with pytest.raises(ValueError):
        solve_effective_annual_yield(request)


def test_effective_yield_rejects_non_finite_cashflow() -> None:
    request = YieldRequest(SETTLE, 900.0, (BondCashflow(date(2027, 1, 1), math.nan),))
    with pytest.raises(ValueError, match="cashflow"):
        solve_effective_annual_yield(request)


@pytest.mark.parametrize("rate", BAD)
def test_bootstrap_rejects_non_finite_quotes(rate: float) -> None:
    with pytest.raises(ValueError, match="par rate"):
        bootstrap_discount_curve(
            CurveBootstrapRequest(SETTLE, (OisQuote("1Y", date(2027, 1, 1), rate),))
        )


@pytest.mark.parametrize("discount_factor", [math.nan, math.inf])
def test_discount_curve_rejects_non_finite_nodes(discount_factor: float) -> None:
    with pytest.raises(ValueError, match="discount factors"):
        DiscountCurve(SETTLE, ((date(2027, 1, 1), discount_factor),))


def test_flat_curve_rejects_non_finite_rate() -> None:
    with pytest.raises(ValueError, match="finite"):
        DiscountCurve.flat(SETTLE, math.nan)
