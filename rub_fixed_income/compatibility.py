from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from math import exp, isfinite

from .cashflows import BondCashflow, clean_price_pct_to_dirty_rub
from .dates import year_fraction
from .discount import DiscountCurve


@dataclass(frozen=True)
class DurationResult:
    macaulay_years: float
    modified_years: float
    present_value: float


def _materialize(cashflows: Iterable[tuple[date, float]]) -> tuple[BondCashflow, ...]:
    return tuple(BondCashflow(payment_date, float(amount)) for payment_date, amount in cashflows)


def _time(start: date, end: date, day_count_basis: float) -> float:
    return (end - start).days / day_count_basis


def _price_at_nominal_yield(
    cashflows: tuple[BondCashflow, ...],
    settlement_date: date,
    annual_yield: float,
    compounding_frequency: int,
    day_count_basis: float,
) -> float:
    base = 1.0 + annual_yield / compounding_frequency
    if base <= 0.0:
        raise ValueError("annual yield is outside the compounding domain")
    return float(
        sum(
            item.amount
            / base
            ** (compounding_frequency * _time(settlement_date, item.payment_date, day_count_basis))
            for item in cashflows
            if item.payment_date > settlement_date
        )
    )


def solve_nominal_annual_yield(
    *,
    dirty_price: float,
    settlement_date: date,
    cashflows: Iterable[tuple[date, float]],
    compounding_frequency: int = 1,
    day_count_basis: float = 365.0,
) -> float:
    if dirty_price <= 0.0 or compounding_frequency < 1 or day_count_basis <= 0.0:
        raise ValueError("dirty price, compounding frequency, and day-count basis must be positive")
    flows = _materialize(cashflows)
    if not any(item.payment_date > settlement_date and item.amount > 0.0 for item in flows):
        raise ValueError("future positive cashflows are required")
    lower = -float(compounding_frequency) + 1e-12
    upper = 1.0
    while (
        _price_at_nominal_yield(
            flows, settlement_date, upper, compounding_frequency, day_count_basis
        )
        > dirty_price
    ):
        upper *= 2.0
        if upper > 1_000_000.0:
            raise ValueError("unable to bracket yield")
    for _ in range(256):
        middle = (lower + upper) / 2.0
        value = _price_at_nominal_yield(
            flows, settlement_date, middle, compounding_frequency, day_count_basis
        )
        if abs(value - dirty_price) <= 1e-12:
            return middle
        if value > dirty_price:
            lower = middle
        else:
            upper = middle
    return (lower + upper) / 2.0


def bond_duration(
    *,
    annual_yield: float,
    settlement_date: date,
    cashflows: Iterable[tuple[date, float]],
    compounding_frequency: int = 1,
    day_count_basis: float = 365.0,
) -> DurationResult:
    if compounding_frequency < 1 or day_count_basis <= 0.0:
        raise ValueError("compounding frequency and day-count basis must be positive")
    flows = _materialize(cashflows)
    base = 1.0 + annual_yield / compounding_frequency
    if base <= 0.0:
        raise ValueError("annual yield is outside the compounding domain")
    discounted = tuple(
        (
            _time(settlement_date, item.payment_date, day_count_basis),
            item.amount
            / base
            ** (compounding_frequency * _time(settlement_date, item.payment_date, day_count_basis)),
        )
        for item in flows
        if item.payment_date > settlement_date
    )
    present_value = sum(value for _, value in discounted)
    if present_value <= 0.0:
        raise ValueError("future positive cashflows are required")
    macaulay = sum(years * value for years, value in discounted) / present_value
    return DurationResult(macaulay, macaulay / base, present_value)


def clean_price_pct_to_dirty_amount(
    clean_price_pct: float, current_nominal: float, accrued_interest_amount: float
) -> float:
    return clean_price_pct_to_dirty_rub(clean_price_pct, current_nominal, accrued_interest_amount)


def price_cashflows(
    cashflows: Iterable[tuple[date, float]],
    curve: DiscountCurve,
    *,
    spread: float = 0.0,
    compounding_frequency: int = 1,
) -> float:
    if compounding_frequency < 1:
        raise ValueError("compounding frequency must be positive")
    price = 0.0
    for payment_date, amount in cashflows:
        if payment_date < curve.curve_date:
            continue
        if payment_date == curve.curve_date:
            price += amount
            continue
        time = year_fraction(curve.curve_date, payment_date)
        discount_factor = curve.discount_factor(payment_date)
        zero_rate = compounding_frequency * (
            discount_factor ** (-1.0 / (compounding_frequency * time)) - 1.0
        )
        base = 1.0 + (zero_rate + spread) / compounding_frequency
        if base <= 0.0:
            raise ValueError("rate is outside the compounding domain")
        price += amount * base ** (-compounding_frequency * time)
    return price


def solve_continuous_z_spread(
    cashflows: Iterable[tuple[date, float]],
    dirty_price: float,
    curve: DiscountCurve,
    *,
    settlement_date: date | None = None,
) -> float:
    settlement = settlement_date or curve.curve_date
    if settlement < curve.curve_date:
        raise ValueError("settlement cannot predate curve valuation")
    if not isfinite(dirty_price) or dirty_price <= 0.0:
        raise ValueError("dirty price must be positive and finite")
    flows = tuple(
        (payment_date, float(amount))
        for payment_date, amount in cashflows
        if payment_date > settlement
    )
    if not flows or any(not isfinite(amount) or amount <= 0.0 for _, amount in flows):
        raise ValueError("future positive finite cashflows are required")
    settlement_df = curve.discount_factor(settlement)

    def error(spread: float) -> float:
        return (
            sum(
                amount
                * curve.discount_factor(payment_date)
                / settlement_df
                * exp(-spread * year_fraction(settlement, payment_date))
                for payment_date, amount in flows
            )
            - dirty_price
        )

    lower, upper = -5.0, 5.0
    low_value, high_value = error(lower), error(upper)
    if low_value * high_value > 0.0:
        raise ValueError("continuous Z-spread root is not bracketed")
    for _ in range(240):
        middle = (lower + upper) / 2.0
        middle_value = error(middle)
        if abs(middle_value) <= 1e-13 or upper - lower <= 1e-13:
            return middle
        if low_value * middle_value <= 0.0:
            upper = middle
        else:
            lower, low_value = middle, middle_value
    raise RuntimeError("continuous Z-spread solver did not converge")


def solve_z_spread(
    cashflows: Iterable[tuple[date, float]],
    dirty_price: float,
    curve: DiscountCurve,
    *,
    compounding_frequency: int = 1,
) -> float:
    flows = tuple(cashflows)
    if not flows:
        raise ValueError("at least one cashflow is required")

    def error(spread: float) -> float:
        return (
            price_cashflows(
                flows,
                curve,
                spread=spread,
                compounding_frequency=compounding_frequency,
            )
            - dirty_price
        )

    lower, upper = -0.95, 5.0
    low_value, high_value = error(lower), error(upper)
    if low_value * high_value > 0.0:
        raise ValueError("root is not bracketed")
    for _ in range(240):
        middle = (lower + upper) / 2.0
        middle_value = error(middle)
        if abs(middle_value) <= 1e-13 or upper - lower <= 1e-13:
            return middle
        if low_value * middle_value <= 0.0:
            upper = middle
        else:
            lower, low_value = middle, middle_value
    raise RuntimeError("root solver did not converge")
