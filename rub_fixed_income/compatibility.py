from __future__ import annotations

import math
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date
from math import exp, isfinite
from typing import TYPE_CHECKING

from ._deprecation import deprecated
from ._money import finite_money
from ._solver import bisect
from .cashflows import BondCashflow, clean_price_pct_to_dirty_rub
from .conventions import CONTINUOUS, Compounding, YieldConvention, ZSpreadConvention
from .discount import DiscountFactorCurve

if TYPE_CHECKING:
    from .perpetual import PerpetualTail

# A nominal-yield root below -0.99 * m (per-period discount base below 0.01) is treated as
# outside the supported domain and reported as "not bracketed" instead of returned.
_NOMINAL_YIELD_FLOOR = 0.99


@dataclass(frozen=True)
class DurationResult:
    macaulay_years: float
    modified_years: float
    present_value: float


def _materialize(cashflows: Iterable[tuple[date, float]]) -> tuple[BondCashflow, ...]:
    flows = tuple(
        BondCashflow(payment_date, finite_money(amount, "cashflow amounts"))
        for payment_date, amount in cashflows
    )
    if any(not isfinite(item.amount) for item in flows):
        raise ValueError("cashflow amounts must be finite")
    return flows


def _resolve_yield_convention(
    compounding_frequency: int, day_count_basis: float, convention: YieldConvention | None
) -> tuple[int, Callable[[date, date], float]]:
    if convention is not None:
        if compounding_frequency != 1 or day_count_basis != 365.0:
            raise ValueError(
                "pass either compounding_frequency/day_count_basis or convention, not both"
            )
        return convention.compounding.periods_per_year, convention.day_count.year_fraction
    if (
        isinstance(compounding_frequency, bool)
        or compounding_frequency < 1
        or not isfinite(day_count_basis)
        or day_count_basis <= 0.0
    ):
        raise ValueError("compounding frequency and day-count basis must be positive")

    def time(start: date, end: date) -> float:
        return (end - start).days / day_count_basis

    return compounding_frequency, time


def _price_at_nominal_yield(
    cashflows: tuple[BondCashflow, ...],
    settlement_date: date,
    annual_yield: float,
    compounding_frequency: int,
    time: Callable[[date, date], float],
) -> float:
    base = 1.0 + annual_yield / compounding_frequency
    if base <= 0.0:
        raise ValueError("annual yield is outside the compounding domain")
    return float(
        sum(
            item.amount / base ** (compounding_frequency * time(settlement_date, item.payment_date))
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
    convention: YieldConvention | None = None,
) -> float:
    """Solve ``P = sum CF / (1 + y/m)^(m t)`` for the nominal annual yield ``y`` (decimal).

    Defaults: m=1, t=days/365. ``convention`` replaces ``compounding_frequency`` and
    ``day_count_basis`` (passing both raises). Raises ``ValueError`` for a non-finite or
    non-positive price, non-finite cash flows, no future positive cash flow, or a root
    outside ``y > -0.99 m`` (message contains "not bracketed").
    """
    if not isfinite(dirty_price) or dirty_price <= 0.0:
        raise ValueError("dirty price must be positive and finite")
    finite_money(dirty_price, "dirty price")
    frequency, time = _resolve_yield_convention(compounding_frequency, day_count_basis, convention)
    flows = _materialize(cashflows)
    if not any(item.payment_date > settlement_date and item.amount > 0.0 for item in flows):
        raise ValueError("future positive cashflows are required")

    def price(rate: float) -> float:
        return _price_at_nominal_yield(flows, settlement_date, rate, frequency, time)

    floor = -_NOMINAL_YIELD_FLOOR * frequency
    try:
        floor_price = price(floor)
    except OverflowError:
        floor_price = math.inf
    if floor_price < dirty_price:
        raise ValueError(
            "nominal yield root is not bracketed: dirty price exceeds the price at the "
            f"yield-domain floor {floor:g}"
        )
    upper = 1.0
    upper_price = price(upper)
    while upper_price > dirty_price:
        upper *= 2.0
        if upper > 1_000_000.0:
            raise ValueError("nominal yield root is not bracketed: unable to bracket yield")
        upper_price = price(upper)
    return bisect(
        lambda rate: price(rate) - dirty_price,
        -float(frequency) + 1e-12,
        upper,
        tolerance=1e-12,
        max_iterations=256,
        lower_value=math.inf,
        upper_value=upper_price - dirty_price,
        label="nominal yield",
    ).root


def bond_duration(
    *,
    annual_yield: float,
    settlement_date: date,
    cashflows: Iterable[tuple[date, float]],
    compounding_frequency: int = 1,
    day_count_basis: float = 365.0,
    convention: YieldConvention | None = None,
) -> DurationResult:
    if not isfinite(annual_yield):
        raise ValueError("annual yield must be finite")
    frequency, time = _resolve_yield_convention(compounding_frequency, day_count_basis, convention)
    flows = _materialize(cashflows)
    base = 1.0 + annual_yield / frequency
    if base <= 0.0:
        raise ValueError("annual yield is outside the compounding domain")
    discounted = tuple(
        (
            time(settlement_date, item.payment_date),
            item.amount / base ** (frequency * time(settlement_date, item.payment_date)),
        )
        for item in flows
        if item.payment_date > settlement_date
    )
    present_value = sum(value for _, value in discounted)
    finite_money(present_value, "duration present value result")
    if present_value <= 0.0:
        raise ValueError("future positive cashflows are required")
    macaulay = sum(years * value for years, value in discounted) / present_value
    return DurationResult(
        finite_money(macaulay, "Macaulay duration result"),
        finite_money(macaulay / base, "modified duration result"),
        present_value,
    )


def clean_price_pct_to_dirty_amount(
    clean_price_pct: float, current_nominal: float, accrued_interest_amount: float
) -> float:
    deprecated("clean_price_pct_to_dirty_amount", "use clean_price_pct_to_dirty_rub")
    return clean_price_pct_to_dirty_rub(clean_price_pct, current_nominal, accrued_interest_amount)


def _resolve_spread_convention(
    compounding_frequency: int, convention: ZSpreadConvention | None
) -> ZSpreadConvention:
    if convention is not None:
        if compounding_frequency != 1:
            raise ValueError("pass either compounding_frequency or convention, not both")
        return convention
    if isinstance(compounding_frequency, bool) or compounding_frequency < 1:
        raise ValueError("compounding frequency must be positive")
    return ZSpreadConvention(compounding=Compounding(compounding_frequency))


def _settlement(curve: DiscountFactorCurve, settlement_date: date | None) -> date:
    settlement = curve.curve_date if settlement_date is None else settlement_date
    if settlement < curve.curve_date:
        raise ValueError("settlement cannot predate curve valuation")
    return settlement


def _spread_price(
    flows: tuple[tuple[date, float], ...],
    curve: DiscountFactorCurve,
    settlement: date,
    spread: float,
    convention: ZSpreadConvention,
    *,
    include_settlement_flows: bool,
    tail: PerpetualTail | None = None,
) -> float:
    """PV at ``settlement`` with the settlement-forward zero rate plus ``spread``.

    Forward DF is ``DF(t) / DF(settlement)``; time runs from settlement with
    ``convention.day_count``. When ``settlement == curve_date`` the forward DF is the curve
    DF exactly, so the legacy curve-date path is reproduced bit for bit. A perpetual
    ``tail`` adds its coupons the same way (``inf`` when the stream does not converge).
    """
    listed = _listed_price(
        flows, curve, settlement, spread, convention, include_settlement_flows
    )
    if tail is None:
        return listed
    from .perpetual import tail_pv_at_spread

    return listed + tail_pv_at_spread(tail, curve, settlement, spread, convention)


def _listed_price(
    flows: tuple[tuple[date, float], ...],
    curve: DiscountFactorCurve,
    settlement: date,
    spread: float,
    convention: ZSpreadConvention,
    include_settlement_flows: bool,
) -> float:
    settlement_df = curve.discount_factor(settlement)
    if convention.compounding.is_continuous:
        return float(
            sum(
                amount
                * curve.discount_factor(payment_date)
                / settlement_df
                * exp(-spread * convention.day_count.year_fraction(settlement, payment_date))
                for payment_date, amount in flows
                if payment_date > settlement
            )
        )
    frequency = convention.compounding.periods_per_year
    price = 0.0
    for payment_date, amount in flows:
        if payment_date < settlement:
            continue
        if payment_date == settlement:
            if include_settlement_flows:
                price += amount
            continue
        time = convention.day_count.year_fraction(settlement, payment_date)
        discount_factor = curve.discount_factor(payment_date) / settlement_df
        zero_rate = frequency * (discount_factor ** (-1.0 / (frequency * time)) - 1.0)
        base = 1.0 + (zero_rate + spread) / frequency
        if base <= 0.0:
            raise ValueError("rate is outside the compounding domain")
        price += amount * base ** (-frequency * time)
    return price


def price_cashflows(
    cashflows: Iterable[tuple[date, float]],
    curve: DiscountFactorCurve,
    *,
    spread: float = 0.0,
    compounding_frequency: int = 1,
    settlement_date: date | None = None,
    convention: ZSpreadConvention | None = None,
    perpetual_tail: PerpetualTail | None = None,
) -> float:
    """Price cash flows on ``curve`` plus a decimal Z-spread.

    Default (no ``settlement_date``): legacy curve-date valuation; a flow on the curve date
    counts at face and earlier flows are ignored. With ``settlement_date``: flows on or
    before settlement are dropped and the value is expressed at settlement.
    ``perpetual_tail`` (with ``settlement_date``) adds a never-called perpetual's coupons
    after its last listed one (``perpetual.tail_pv_at_spread``).
    """
    if settlement_date is None:
        deprecated(
            "price_cashflows without settlement_date (curve-date valuation)", "pass settlement_date"
        )
    if not isfinite(spread):
        raise ValueError("spread must be finite")
    resolved = _resolve_spread_convention(compounding_frequency, convention)
    flows = tuple(
        (payment_date, finite_money(amount, "cashflow amounts"))
        for payment_date, amount in cashflows
    )
    if any(not isfinite(amount) for _, amount in flows):
        raise ValueError("cashflow amounts must be finite")
    try:
        price = _spread_price(
            flows,
            curve,
            _settlement(curve, settlement_date),
            spread,
            resolved,
            include_settlement_flows=settlement_date is None,
            tail=_tail_for(perpetual_tail, settlement_date),
        )
    except OverflowError as exc:
        raise ValueError("cashflow price result must be finite") from exc
    return finite_money(price, "cashflow price result")


def _tail_for(tail: PerpetualTail | None, settlement_date: date | None) -> PerpetualTail | None:
    if tail is not None and settlement_date is None:
        raise ValueError("a perpetual tail is valued at an explicit settlement_date")
    return tail


def _solve_spread(
    flows: tuple[tuple[date, float], ...],
    dirty_price: float,
    curve: DiscountFactorCurve,
    settlement_date: date | None,
    convention: ZSpreadConvention,
    tail: PerpetualTail | None = None,
) -> float:
    if not isfinite(dirty_price) or dirty_price <= 0.0:
        raise ValueError("dirty price must be positive and finite")
    finite_money(dirty_price, "dirty price")
    if any(not isfinite(amount) for _, amount in flows):
        raise ValueError("cashflow amounts must be finite")
    settlement = _settlement(curve, settlement_date)
    include = settlement_date is None
    if tail is None and not any(
        payment_date > settlement or (include and payment_date == settlement)
        for payment_date, _ in flows
    ):
        raise ValueError("at least one cashflow after settlement is required")

    def error(spread: float) -> float:
        return (
            _spread_price(
                flows,
                curve,
                settlement,
                spread,
                convention,
                include_settlement_flows=include,
                tail=tail,
            )
            - dirty_price
        )

    if convention.compounding.is_continuous:
        lower, upper, label = -5.0, 5.0, "continuous Z-spread"
    else:
        lower, upper, label = -0.95, 5.0, "Z-spread"
    lower_value = None
    if tail is not None:
        # A perpetual is worth infinity below the spread at which its stream converges:
        # raise the lower end until the price is finite, the sign there is known.
        lower, lower_value = _finite_lower(error, lower, upper)
    return bisect(
        error,
        lower,
        upper,
        tolerance=1e-13,
        x_tolerance=1e-13,
        lower_value=lower_value,
        label=label,
    ).root


def _finite_lower(
    error: Callable[[float], float], lower: float, upper: float
) -> tuple[float, float]:
    """A lower bracket end where ``error`` is finite, and positive when it can be.

    A perpetual's price is infinite below the rate at which its stream converges and
    falls from infinity above it: bisect that boundary from the finite side until the
    price exceeds the target (or the boundary is resolved).
    """
    value = error(lower)
    if isfinite(value):
        return lower, value
    infinite, finite = lower, upper
    finite_value = error(upper)
    for _ in range(200):
        if finite_value > 0.0 or finite - infinite <= 1e-15:
            break
        middle = (infinite + finite) / 2.0
        value = error(middle)
        if isfinite(value):
            finite, finite_value = middle, value
        else:
            infinite = middle
    return finite, finite_value


def solve_continuous_z_spread(
    cashflows: Iterable[tuple[date, float]],
    dirty_price: float,
    curve: DiscountFactorCurve,
    *,
    settlement_date: date | None = None,
) -> float:
    """Continuous Z-spread over settlement-forward DFs: PV = sum CF DF(t)/DF(s) e^(-z tau)."""
    deprecated(
        "solve_continuous_z_spread",
        "use solve_z_spread(..., convention=ZSpreadConvention(compounding=CONTINUOUS))",
    )
    settlement = _settlement(curve, settlement_date)
    if not isfinite(dirty_price) or dirty_price <= 0.0:
        raise ValueError("dirty price must be positive and finite")
    finite_money(dirty_price, "dirty price")
    flows = tuple(
        (payment_date, finite_money(amount, "cashflow amounts"))
        for payment_date, amount in cashflows
        if payment_date > settlement
    )
    if not flows or any(not isfinite(amount) or amount <= 0.0 for _, amount in flows):
        raise ValueError("future positive finite cashflows are required")
    return _solve_spread(
        flows, dirty_price, curve, settlement, ZSpreadConvention(compounding=CONTINUOUS)
    )


def solve_z_spread(
    cashflows: Iterable[tuple[date, float]],
    dirty_price: float,
    curve: DiscountFactorCurve,
    *,
    compounding_frequency: int = 1,
    settlement_date: date | None = None,
    convention: ZSpreadConvention | None = None,
    perpetual_tail: PerpetualTail | None = None,
) -> float:
    """Solve the decimal Z-spread over the curve's m-compounded zero rates.

    Default: annual (m=1), ACT/365F, valued at the curve date (legacy behaviour). Monthly:
    ``compounding_frequency=12`` or ``convention=ZSpreadConvention(compounding=MONTHLY)``.
    ``convention`` also allows ``CONTINUOUS`` and other spread day counts; it cannot be
    combined with a non-default ``compounding_frequency``. ``settlement_date`` values the
    bond at settlement (DF(t)/DF(settlement), time from settlement, flows on or before
    settlement dropped); it is equivalent to rebasing the curve to settlement manually.
    ``perpetual_tail`` adds a never-called perpetual's coupons (see ``price_cashflows``).
    """
    if settlement_date is None:
        deprecated(
            "solve_z_spread without settlement_date (curve-date valuation)", "pass settlement_date"
        )
    resolved = _resolve_spread_convention(compounding_frequency, convention)
    flows = tuple(
        (payment_date, finite_money(amount, "cashflow amounts"))
        for payment_date, amount in cashflows
    )
    if not flows and perpetual_tail is None:
        raise ValueError("at least one cashflow is required")
    return _solve_spread(
        flows,
        dirty_price,
        curve,
        settlement_date,
        resolved,
        _tail_for(perpetual_tail, settlement_date),
    )
