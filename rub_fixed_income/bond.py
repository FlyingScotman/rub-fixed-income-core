from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from itertools import pairwise
from math import isfinite

from .accumulation import AccumulationPeriod
from .cashflows import BondCashflow, CashEvent, EventKey
from .dates import year_fraction


@dataclass(frozen=True)
class FinancedBondRequest:
    start: EventKey
    horizon: EventKey
    initial_dirty_rub_per_bond: float
    cash_events: tuple[CashEvent, ...]
    accumulation_periods: tuple[AccumulationPeriod, ...]


@dataclass(frozen=True)
class FinancingStep:
    period_id: str
    start: EventKey
    end: EventKey
    opening_balance_rub_per_bond: float
    accumulation_factor: float
    cash_event_id: str | None
    cash_amount_rub_per_bond: float
    closing_balance_rub_per_bond: float


@dataclass(frozen=True)
class FinancedBondProjection:
    horizon_dirty_rub_per_bond: float
    steps: tuple[FinancingStep, ...]


@dataclass(frozen=True)
class YieldRequest:
    horizon_date: date
    dirty_price_rub_per_bond: float
    cashflows: tuple[BondCashflow, ...]


@dataclass(frozen=True)
class YieldResult:
    effective_annual_yield: float
    repricing_error_rub_per_bond: float


def _bisect_root(
    function: Callable[[float], float], lower: float, upper: float, tolerance: float
) -> float:
    """Solve a continuous bracketed scalar function without runtime dependencies."""
    low_value = function(lower)
    high_value = function(upper)
    if low_value * high_value > 0.0:
        raise ValueError("root is not bracketed")
    for _ in range(240):
        middle = (lower + upper) / 2.0
        middle_value = function(middle)
        if abs(middle_value) <= tolerance or upper - lower <= tolerance:
            return middle
        if low_value * middle_value <= 0.0:
            upper = middle
        else:
            lower = middle
            low_value = middle_value
    raise RuntimeError("root solver did not converge")


def solve_effective_annual_yield(request: YieldRequest) -> YieldResult:
    if not request.cashflows:
        raise ValueError("remaining cashflows are required after horizon")
    if request.dirty_price_rub_per_bond <= 0.0:
        raise ValueError("forward dirty price must be positive")
    if any(cashflow.payment_date <= request.horizon_date for cashflow in request.cashflows):
        raise ValueError("yield cashflows must be strictly after horizon")

    def error(rate: float) -> float:
        return float(
            sum(
                cashflow.amount
                / (1.0 + rate) ** year_fraction(request.horizon_date, cashflow.payment_date)
                for cashflow in request.cashflows
            )
            - request.dirty_price_rub_per_bond
        )

    try:
        solved = _bisect_root(error, -0.99, 5.0, 1e-13)
    except ValueError as exc:
        raise ValueError("forward yield root is not bracketed") from exc
    return YieldResult(solved, error(solved))


def project_financed_bond(request: FinancedBondRequest) -> FinancedBondProjection:
    if request.initial_dirty_rub_per_bond <= 0.0 or not isfinite(
        request.initial_dirty_rub_per_bond
    ):
        raise ValueError("initial dirty cash must be positive and finite")
    if any(not request.start < event.key <= request.horizon for event in request.cash_events):
        raise ValueError("cash events must be after start and on or before horizon")
    event_keys = tuple(event.key for event in request.cash_events)
    if any(left >= right for left, right in pairwise(event_keys)):
        raise ValueError("cash-event keys must be strictly ordered and unique")
    event_ids = tuple(event.event_id for event in request.cash_events)
    if len(set(event_ids)) != len(event_ids):
        raise ValueError("cash-event IDs must be unique")
    period_ids = tuple(period.period_id for period in request.accumulation_periods)
    if len(set(period_ids)) != len(period_ids):
        raise ValueError("accumulation-period IDs must be unique")
    period_ends = {period.end for period in request.accumulation_periods}
    if any(event.key not in period_ends for event in request.cash_events):
        raise ValueError("every cash event must terminate an accumulation period")
    expected_start = request.start
    for period in request.accumulation_periods:
        if period.factor <= 0.0 or not isfinite(period.factor):
            raise ValueError("accumulation factors must be positive and finite")
        if period.start != expected_start:
            raise ValueError("accumulation periods must be ordered without gaps or overlaps")
        if period.end <= period.start:
            raise ValueError("accumulation-period ends must follow starts")
        if (
            period.start.event_date == period.end.event_date
            and period.end.ordinal != period.start.ordinal + 1
        ):
            raise ValueError("same-day event ordinals must be contiguous")
        expected_start = period.end
    if expected_start != request.horizon:
        raise ValueError("accumulation periods must be ordered without gaps or overlaps")
    events = {event.key: event for event in request.cash_events}
    balance = request.initial_dirty_rub_per_bond
    steps: list[FinancingStep] = []
    for period in request.accumulation_periods:
        opening = balance
        event = events.get(period.end)
        cash_amount = event.amount_rub_per_bond if event is not None else 0.0
        balance = opening * period.factor - cash_amount
        steps.append(
            FinancingStep(
                period.period_id,
                period.start,
                period.end,
                opening,
                period.factor,
                event.event_id if event is not None else None,
                cash_amount,
                balance,
            )
        )
    return FinancedBondProjection(balance, tuple(steps))
