from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from enum import Enum

from ._money import finite_money, non_negative_money, positive_money
from .errors import InvalidTerms


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


class CashflowKind(Enum):
    """What the principal part of a payment is; ``COUPON`` carries no principal."""

    COUPON = "coupon"
    AMORTIZATION = "amortization"
    MATURITY = "maturity"
    OFFER_REDEMPTION = "offer_redemption"


@dataclass(frozen=True)
class Cashflow(BondCashflow):
    """A payment split into coupon and principal, per bond.

    It is a :class:`BondCashflow` whose ``amount`` is ``coupon + principal``, so every
    pricing and risk entry point accepts it unchanged. Build it with
    :meth:`Cashflow.split`. ``accrual_start``/``accrual_end`` give the coupon period
    that accrued interest is measured on; both or neither.
    """

    coupon: float = 0.0
    principal: float = 0.0
    accrual_start: date | None = None
    accrual_end: date | None = None
    kind: CashflowKind = CashflowKind.COUPON

    @classmethod
    def split(
        cls,
        payment_date: date,
        *,
        coupon: float = 0.0,
        principal: float = 0.0,
        accrual_start: date | None = None,
        accrual_end: date | None = None,
        kind: CashflowKind | None = None,
    ) -> Cashflow:
        if kind is None:
            kind = CashflowKind.COUPON if principal == 0.0 else CashflowKind.MATURITY
        return cls(
            payment_date,
            coupon + principal,
            coupon,
            principal,
            accrual_start,
            accrual_end,
            kind,
        )

    def __post_init__(self) -> None:
        coupon = non_negative_money(self.coupon, "coupon")
        principal = non_negative_money(self.principal, "principal")
        amount = finite_money(self.amount, "cashflow amount")
        if not math.isclose(amount, coupon + principal, rel_tol=1e-12, abs_tol=1e-9):
            raise InvalidTerms("cashflow amount must equal coupon + principal")
        if not isinstance(self.kind, CashflowKind):
            raise TypeError("kind must be a CashflowKind")
        if (self.kind is CashflowKind.COUPON) != (principal == 0.0):
            raise InvalidTerms("a COUPON flow has no principal; any other kind must have some")
        if (self.accrual_start is None) != (self.accrual_end is None):
            raise InvalidTerms("accrual_start and accrual_end must be given together")
        if self.accrual_start is not None and self.accrual_end is not None:
            if self.accrual_end <= self.accrual_start:
                raise InvalidTerms("accrual period must have positive length")
            if self.payment_date < self.accrual_end:
                raise InvalidTerms("a coupon cannot be paid before its accrual period ends")
        elif coupon > 0.0:
            raise InvalidTerms("a coupon needs its accrual period")


def accrued_interest(
    cashflows: Iterable[BondCashflow],
    settlement_date: date,
    *,
    quantum: Decimal | None = Decimal("0.01"),
) -> float:
    """Linear accrued interest per bond from split cashflows.

    ``coupon * elapsed_days / period_days`` for the coupon period containing the
    settlement date, computed in exact decimal arithmetic from the coupon's shortest
    decimal form and rounded half-up to ``quantum`` (``None`` keeps the raw float value).
    Settlement on the period end gives 0: the buyer does not receive that coupon
    (MOEX convention, as :func:`rub_fixed_income.analytics.accrued_interest_rub`).
    Flows without an accrual period (plain ``BondCashflow`` or principal-only) are
    ignored. A bond with coupons but no period containing the settlement is rejected.
    """
    periods = [
        flow
        for flow in cashflows
        if isinstance(flow, Cashflow)
        and flow.coupon > 0.0
        and flow.accrual_start is not None
        and flow.accrual_end is not None
    ]
    if not periods:
        return 0.0
    for flow in sorted(periods, key=lambda f: f.payment_date):
        assert flow.accrual_start is not None and flow.accrual_end is not None
        if flow.accrual_start <= settlement_date < flow.accrual_end:
            elapsed = (settlement_date - flow.accrual_start).days
            period = (flow.accrual_end - flow.accrual_start).days
            raw = finite_money(flow.coupon * elapsed / period, "accrued interest result")
            if quantum is None:
                return raw
            # Exact decimal pro rata (#16), as the floater path: a float product can fall a
            # hair below a half kopeck (77.49 * 13 / 182 = 5.535 is 5.534999... in binary).
            exact = Decimal(repr(flow.coupon)) * elapsed / period
            return float(exact.quantize(quantum, ROUND_HALF_UP))
    if settlement_date >= max(f.accrual_end for f in periods if f.accrual_end is not None):
        return 0.0
    raise InvalidTerms("no coupon accrual period contains the settlement date")


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
