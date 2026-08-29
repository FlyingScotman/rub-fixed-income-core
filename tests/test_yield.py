from __future__ import annotations

from datetime import date

import pytest

from rub_fixed_income import BondCashflow, YieldRequest, solve_effective_annual_yield


def test_effective_annual_yield_recovers_constant_rate_from_multi_period_cashflows() -> None:
    horizon = date(2025, 1, 1)
    cashflows = (
        BondCashflow(date(2026, 1, 1), 50.0),
        BondCashflow(date(2027, 1, 1), 1050.0),
    )
    dirty_price = 50.0 / 1.10 + 1050.0 / 1.10**2

    result = solve_effective_annual_yield(YieldRequest(horizon, dirty_price, cashflows))

    assert result.effective_annual_yield == pytest.approx(0.10, abs=1e-12)
    assert result.repricing_error_rub_per_bond == pytest.approx(0.0, abs=1e-10)


def test_effective_annual_yield_rejects_cashflow_on_horizon() -> None:
    horizon = date(2025, 1, 1)
    request = YieldRequest(horizon, 1000.0, (BondCashflow(horizon, 1000.0),))

    with pytest.raises(ValueError, match="yield cashflows must be strictly after horizon"):
        solve_effective_annual_yield(request)


@pytest.mark.parametrize(
    ("dirty_price", "expected_yield"),
    [(913.2231404958677, 0.10), (770.8333333333334, 0.20)],
)
def test_stdlib_solver_shadows_frozen_scipy_brentq_results(
    dirty_price: float, expected_yield: float
) -> None:
    request = YieldRequest(
        date(2025, 1, 1),
        dirty_price,
        (BondCashflow(date(2026, 1, 1), 50.0), BondCashflow(date(2027, 1, 1), 1050.0)),
    )

    result = solve_effective_annual_yield(request)

    assert result.effective_annual_yield == pytest.approx(expected_yield, abs=5e-13)
