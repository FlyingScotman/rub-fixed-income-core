from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True, order=True)
class EventKey:
    event_date: date
    ordinal: int


@dataclass(frozen=True)
class CashEvent:
    key: EventKey
    amount_rub_per_bond: float
    event_id: str


@dataclass(frozen=True)
class BondCashflow:
    payment_date: date
    amount: float


def clean_price_pct_to_dirty_rub(
    clean_price_pct: float, current_nominal_rub: float, accrued_interest_rub: float
) -> float:
    if current_nominal_rub <= 0.0:
        raise ValueError("current nominal must be positive")
    if accrued_interest_rub < 0.0:
        raise ValueError("accrued interest cannot be negative")
    return clean_price_pct / 100.0 * current_nominal_rub + accrued_interest_rub


def dirty_rub_to_clean_price_pct(
    dirty_rub_per_bond: float, current_nominal_rub: float, accrued_interest_rub: float
) -> float:
    if current_nominal_rub <= 0.0:
        raise ValueError("current nominal must be positive")
    if accrued_interest_rub < 0.0:
        raise ValueError("accrued interest cannot be negative")
    return (dirty_rub_per_bond - accrued_interest_rub) / current_nominal_rub * 100.0
