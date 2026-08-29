from __future__ import annotations

from datetime import date
from math import exp

import pytest

from rub_fixed_income import DiscountCurve, solve_continuous_z_spread
from rub_fixed_income.dates import year_fraction


def test_continuous_z_spread_reprices_cashflows_against_discount_factors() -> None:
    settlement = date(2026, 8, 28)
    one_year = date(2027, 8, 28)
    two_year = date(2028, 8, 28)
    curve = DiscountCurve(settlement, ((one_year, 0.98), (two_year, 0.90)))
    cashflows = ((one_year, 10.0), (two_year, 110.0))
    expected_spread = 0.0125
    dirty_price = sum(
        amount
        * curve.discount_factor(payment_date)
        * exp(-expected_spread * year_fraction(settlement, payment_date))
        for payment_date, amount in cashflows
    )

    solved = solve_continuous_z_spread(
        cashflows,
        dirty_price,
        curve,
        settlement_date=settlement,
    )

    assert solved == pytest.approx(expected_spread, abs=1e-12)
