from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from itertools import pairwise
from math import isfinite

from ._money import finite_money
from ._solver import bisect
from .accumulation import AccumulationPeriod
from .canonical import OMIT_IF_DEFAULT
from .cashflows import BondCashflow, CashEvent, EventKey
from .conventions import ANNUAL, DEFAULT_YIELD_CONVENTION, YieldConvention


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
    """Effective annual yield ``y``: ``P = sum CF / (1 + y)^t``, ``t`` from the horizon.

    ``convention`` sets the time basis of ``t`` (``convention.day_count``; default
    ACT/365F, the MOEX quote basis). It is a term of the issue, never inferred: some
    USD replacement bonds are quoted on ACT/360 (BACKLOG #38). Its compounding must be
    annual; for nominal yields use ``solve_nominal_annual_yield``.
    """

    horizon_date: date
    dirty_price_rub_per_bond: float
    cashflows: tuple[BondCashflow, ...]
    convention: YieldConvention = field(default=DEFAULT_YIELD_CONVENTION, metadata=OMIT_IF_DEFAULT)

    def __post_init__(self) -> None:
        if not isinstance(self.convention, YieldConvention):
            raise TypeError("convention must be a YieldConvention")
        if self.convention.compounding != ANNUAL:
            raise ValueError(
                "an effective annual yield compounds annually; use solve_nominal_annual_yield"
            )


@dataclass(frozen=True)
class YieldResult:
    effective_annual_yield: float
    repricing_error_rub_per_bond: float


def solve_effective_annual_yield(request: YieldRequest) -> YieldResult:
    if not request.cashflows:
        raise ValueError("remaining cashflows are required after horizon")
    if not isfinite(request.dirty_price_rub_per_bond) or request.dirty_price_rub_per_bond <= 0.0:
        raise ValueError("forward dirty price must be positive and finite")
    finite_money(request.dirty_price_rub_per_bond, "forward dirty price")
    if any(not isfinite(cashflow.amount) for cashflow in request.cashflows):
        raise ValueError("cashflow amounts must be finite")
    for cashflow in request.cashflows:
        finite_money(cashflow.amount, "cashflow amount")
    if any(cashflow.payment_date <= request.horizon_date for cashflow in request.cashflows):
        raise ValueError("yield cashflows must be strictly after horizon")
    # ACT/365F is (end - start).days / 365.0, the expression used before the time basis
    # became an input, so default results are unchanged bit for bit.
    times = tuple(
        request.convention.day_count.year_fraction(request.horizon_date, cashflow.payment_date)
        for cashflow in request.cashflows
    )

    def error(rate: float) -> float:
        return float(
            sum(
                cashflow.amount / (1.0 + rate) ** time
                for cashflow, time in zip(request.cashflows, times, strict=True)
            )
            - request.dirty_price_rub_per_bond
        )

    solved = bisect(
        error, -0.99, 5.0, tolerance=1e-13, x_tolerance=1e-13, label="forward yield"
    ).root
    return YieldResult(solved, error(solved))


def project_financed_bond(request: FinancedBondRequest) -> FinancedBondProjection:
    if request.initial_dirty_rub_per_bond <= 0.0 or not isfinite(
        request.initial_dirty_rub_per_bond
    ):
        raise ValueError("initial dirty cash must be positive and finite")
    finite_money(request.initial_dirty_rub_per_bond, "initial dirty cash")
    for cash_event in request.cash_events:
        finite_money(cash_event.amount_rub_per_bond, "cash-event amount")
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
        finite_money(period.factor, "accumulation factor")
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
        finite_money(balance, "financing balance result")
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
