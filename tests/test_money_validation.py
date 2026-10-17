from collections.abc import Callable
from dataclasses import replace
from datetime import date

import pytest

from rub_fixed_income import (
    AccumulationPeriod,
    BondTermsV1,
    CashEvent,
    DiscountCurve,
    EventKey,
    FinancedBondRequest,
    bond_duration,
    clean_price_pct_to_dirty_amount,
    clean_price_pct_to_dirty_rub,
    dirty_rub_to_clean_price_pct,
    price_cashflows,
    project_financed_bond,
)
from rub_fixed_income.analytics import accrued_interest_rub

# These tests pin the behaviour of APIs deprecated in 0.12.0 (kept until 1.0);
# tests/test_deprecations.py checks that each one warns.
pytestmark = pytest.mark.filterwarnings("ignore:rub_fixed_income:DeprecationWarning")


Conversion = Callable[[float, float, float], float]

BAD = [float("nan"), float("inf"), float("-inf"), True]
START = EventKey(date(2026, 1, 1), 0)
END = EventKey(date(2027, 1, 1), 0)


@pytest.mark.parametrize(
    "convert",
    [clean_price_pct_to_dirty_rub, clean_price_pct_to_dirty_amount, dirty_rub_to_clean_price_pct],
)
@pytest.mark.parametrize("index", [0, 1, 2])
@pytest.mark.parametrize("invalid", BAD)
def test_clean_dirty_helpers_reject_each_nonfinite_or_boolean_input(
    convert: Conversion, index: int, invalid: float
) -> None:
    args: list[float] = [100, 1000, 10]
    args[index] = invalid
    with pytest.raises(ValueError, match="finite"):
        convert(*args)


@pytest.mark.parametrize("convert", [clean_price_pct_to_dirty_rub, dirty_rub_to_clean_price_pct])
@pytest.mark.parametrize("index", [0, 1])
@pytest.mark.parametrize("invalid", [0, -1])
def test_unit_price_and_nominal_are_strictly_positive(
    convert: Conversion, index: int, invalid: float
) -> None:

    args: list[float] = [100, 1000, 10]
    args[index] = invalid
    with pytest.raises(ValueError, match="positive"):
        convert(*args)


@pytest.mark.parametrize("convert", [clean_price_pct_to_dirty_rub, dirty_rub_to_clean_price_pct])
def test_negative_accrued_interest_is_rejected(convert: Conversion) -> None:
    with pytest.raises(ValueError, match="negative"):
        convert(100, 1000, -1)


@pytest.mark.parametrize("dirty", [9, 10])
def test_dirty_less_than_or_equal_to_accrued_cannot_imply_valid_clean_price(dirty: float) -> None:
    with pytest.raises(ValueError, match="positive"):
        dirty_rub_to_clean_price_pct(dirty, 1000, 10)


def test_conversion_rejects_finite_inputs_that_overflow_result() -> None:
    with pytest.raises(ValueError, match="finite"):
        clean_price_pct_to_dirty_rub(1e308, 1e308, 0)
    with pytest.raises(ValueError, match="finite"):
        dirty_rub_to_clean_price_pct(1e308, 1e-308, 0)


@pytest.mark.parametrize("amount", BAD)
def test_financing_rejects_invalid_cash_events(amount: float) -> None:
    request = FinancedBondRequest(
        START,
        END,
        1000,
        (CashEvent(END, amount, "cash"),),
        (AccumulationPeriod(START, END, 1.1, "funding"),),
    )
    with pytest.raises(ValueError, match="cash-event amount must be finite"):
        project_financed_bond(request)


def test_financing_rejects_overflow_before_emitting_steps() -> None:
    request = FinancedBondRequest(
        START, END, 1e308, (), (AccumulationPeriod(START, END, 2, "funding"),)
    )
    with pytest.raises(ValueError, match="financing balance result must be finite"):
        project_financed_bond(request)


def test_signed_cash_expense_is_valid_and_increases_funding_balance() -> None:
    result = project_financed_bond(
        FinancedBondRequest(
            START,
            END,
            1000,
            (CashEvent(END, -50, "expense"),),
            (AccumulationPeriod(START, END, 1.1, "funding"),),
        )
    )
    assert result.horizon_dirty_rub_per_bond == pytest.approx(1150)
    assert result.steps[0].cash_amount_rub_per_bond == -50


@pytest.mark.parametrize("invalid", BAD)
def test_public_accrued_helper_validates_coupon_even_on_payment_date(invalid: float) -> None:
    terms = BondTermsV1(
        "BOND",
        1000,
        END.event_date,
        "maturity",
        END.event_date,
        START.event_date,
        END.event_date,
        50,
        "terms",
    )
    with pytest.raises(ValueError, match="finite"):
        accrued_interest_rub(replace(terms, coupon_amount_rub=invalid), END.event_date)


def test_public_pricing_rejects_overflow_but_preserves_signed_net_cashflows() -> None:
    curve = DiscountCurve.flat(START.event_date, 0)
    with pytest.raises(ValueError, match="finite"):
        price_cashflows([(END.event_date, 1e308), (END.event_date, 1e308)], curve)
    assert price_cashflows([(END.event_date, 100), (END.event_date, -100)], curve) == 0


def test_duration_does_not_return_nan_from_overflowing_present_value() -> None:
    with pytest.raises(ValueError, match="finite"):
        bond_duration(
            annual_yield=0,
            settlement_date=START.event_date,
            cashflows=[(END.event_date, 1e308), (END.event_date, 1e308)],
        )


@pytest.mark.parametrize("amount", BAD)
def test_public_pricer_rejects_invalid_cashflow_money(amount: float) -> None:
    with pytest.raises(ValueError, match="finite"):
        price_cashflows([(END.event_date, amount)], DiscountCurve.flat(START.event_date, 0))
