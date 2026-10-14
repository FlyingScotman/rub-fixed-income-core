from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from ._money import non_negative_money, positive_money


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
    clean = positive_money(clean_price_pct, "clean price")
    nominal = positive_money(current_nominal_rub, "current nominal")
    accrued = non_negative_money(accrued_interest_rub, "accrued interest")
    return positive_money(clean / 100.0 * nominal + accrued, "dirty price result")


def dirty_rub_to_clean_price_pct(
    dirty_rub_per_bond: float, current_nominal_rub: float, accrued_interest_rub: float
) -> float:
    dirty = positive_money(dirty_rub_per_bond, "dirty price")
    nominal = positive_money(current_nominal_rub, "current nominal")
    accrued = non_negative_money(accrued_interest_rub, "accrued interest")
    return positive_money((dirty - accrued) / nominal * 100.0, "clean price result")
