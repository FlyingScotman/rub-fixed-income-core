"""Core domain model: instruments, a shared market snapshot, positions (roadmap 0.3).

The backend adapter builds these from market-data hub snapshots and private desk
data (ADR-003); core never parses hub payloads. Units follow ``docs/CONVENTIONS.md``:
amounts are per bond in the instrument currency, prices are % of current nominal,
rates are decimals. Every type is frozen, validated on construction and round-trips
through :func:`rub_fixed_income.serialization.to_dict` / ``from_dict``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
from itertools import pairwise
from typing import Literal

from ._money import finite_money, positive_money
from .cashflows import Cashflow, CashflowKind, accrued_interest
from .dates import BusinessCalendar
from .discount import CurveBootstrapRequest, DiscountCurve
from .errors import InvalidTerms, MissingMarketData
from .floating import FixingHistory, FloatingCouponPeriod, FloatingCouponRule

__all__ = [
    "AccountingCategory",
    "BondPriceObservation",
    "CalendarEntry",
    "CouponPeriod",
    "CurveEntry",
    "FixedCouponLeg",
    "FixingEntry",
    "FloatingCouponLeg",
    "FxRate",
    "Instrument",
    "MarketSnapshot",
    "Offer",
    "OfferKind",
    "Position",
    "PrincipalLeg",
    "ProductType",
    "Redemption",
    "SnapshotEvidence",
]


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InvalidTerms(f"{label} must be a non-empty string")
    return value


def _aware(value: datetime, label: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise InvalidTerms(f"{label} must be a timestamp with an explicit UTC offset")


def _currency(value: object) -> str:
    if not isinstance(value, str) or len(value) != 3 or not value.isalpha() or not value.isupper():
        raise InvalidTerms("currency must be an ISO 4217 code such as RUB")
    return value


def _unique(keys: list[str], label: str) -> None:
    if len(set(keys)) != len(keys):
        raise InvalidTerms(f"duplicate {label}")


class ProductType(Enum):
    """Every product the desk book holds (roadmap §3). Support is decided per release."""

    FIXED_BOND = "fixed_bond"
    FLOATING_BOND = "floating_bond"
    INFLATION_LINKED_BOND = "inflation_linked_bond"
    IRS = "irs"
    FX_SPOT = "fx_spot"
    FX_FORWARD = "fx_forward"
    FX_SWAP = "fx_swap"
    FX_FUTURE = "fx_future"
    BOND_FUTURE = "bond_future"
    RATE_FUTURE = "rate_future"
    REPO = "repo"
    XCCY_SWAP = "xccy_swap"
    CDS = "cds"
    MBS_ABS = "mbs_abs"


class OfferKind(Enum):
    PUT = "put"
    CALL = "call"


class AccountingCategory(Enum):
    TRADING = "trading"
    HTM = "htm"
    REPO = "repo"


@dataclass(frozen=True)
class SnapshotEvidence:
    """Where an input came from and when it was known (UTC-aware timestamps)."""

    snapshot_id: str
    source_id: str
    observed_at: datetime
    known_at: datetime
    valid_until: datetime

    def validate(self, as_of: datetime) -> None:
        _text(self.snapshot_id, "snapshot_id")
        _text(self.source_id, "source_id")
        for value in (self.observed_at, self.known_at, self.valid_until):
            if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("timestamps must have explicit UTC offsets")
        if not self.observed_at <= self.known_at <= as_of <= self.valid_until:
            raise ValueError("snapshot is stale, future-known or has inconsistent timestamps")


@dataclass(frozen=True)
class CouponPeriod:
    """A coupon known as a fact: amount per bond for ``[accrual_start, accrual_end)``."""

    accrual_start: date
    accrual_end: date
    payment_date: date
    amount: float

    def __post_init__(self) -> None:
        finite_money(self.amount, "coupon amount")
        if self.amount < 0.0:
            raise InvalidTerms("coupon amount cannot be negative")
        if self.accrual_end <= self.accrual_start:
            raise InvalidTerms("coupon accrual period must have positive length")
        if self.payment_date < self.accrual_end:
            raise InvalidTerms("a coupon cannot be paid before its accrual period ends")


@dataclass(frozen=True)
class FixedCouponLeg:
    periods: tuple[CouponPeriod, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "periods", tuple(self.periods))
        for left, right in pairwise(self.periods):
            if right.accrual_start < left.accrual_end:
                raise InvalidTerms("coupon periods must be ordered and must not overlap")
            if right.payment_date <= left.payment_date:
                raise InvalidTerms("coupon payment dates must increase")


@dataclass(frozen=True)
class FloatingCouponLeg:
    """A floater formula over its periods; ``fixing_id`` keys the snapshot's history."""

    rule: FloatingCouponRule
    periods: tuple[FloatingCouponPeriod, ...]
    fixing_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "periods", tuple(self.periods))
        _text(self.fixing_id, "fixing_id")
        if not self.periods:
            raise InvalidTerms("a floating leg needs at least one period")


@dataclass(frozen=True)
class Redemption:
    """Principal paid per bond on ``payment_date``."""

    payment_date: date
    amount: float
    kind: CashflowKind = CashflowKind.MATURITY

    def __post_init__(self) -> None:
        positive_money(self.amount, "redemption amount")
        if self.kind not in (CashflowKind.AMORTIZATION, CashflowKind.MATURITY):
            raise InvalidTerms("a redemption is an amortization or the maturity payment")


@dataclass(frozen=True)
class PrincipalLeg:
    redemptions: tuple[Redemption, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "redemptions", tuple(self.redemptions))
        for left, right in pairwise(self.redemptions):
            if right.payment_date <= left.payment_date:
                raise InvalidTerms("redemption dates must increase")
        if self.redemptions and self.redemptions[-1].kind is not CashflowKind.MATURITY:
            raise InvalidTerms("the last redemption must be the maturity payment")
        if any(r.kind is CashflowKind.MATURITY for r in self.redemptions[:-1]):
            raise InvalidTerms("only the last redemption can be the maturity payment")


@dataclass(frozen=True)
class Offer:
    """A put or call date; the redemption is ``price_pct`` of the outstanding face."""

    offer_date: date
    kind: OfferKind
    confirmed: bool
    price_pct: float = 100.0

    def __post_init__(self) -> None:
        positive_money(self.price_pct, "offer price")
        if not isinstance(self.kind, OfferKind):
            raise TypeError("offer kind must be an OfferKind")


HorizonPolicy = Literal["maturity", "confirmed_put"]


@dataclass(frozen=True)
class Instrument:
    """Market-independent terms of an instrument.

    For bonds: a coupon leg (known fixed periods or a floater formula), a principal
    leg whose redemptions sum to ``initial_face`` (empty for a perpetual), offers,
    and the calendar ID under which settlement is computed.
    """

    instrument_id: str
    product: ProductType
    currency: str
    initial_face: float
    coupon_leg: FixedCouponLeg | FloatingCouponLeg | None
    principal_leg: PrincipalLeg
    calendar_id: str
    offers: tuple[Offer, ...] = ()
    settlement_lag_days: int = 1
    isin: str | None = None
    issuer_id: str | None = None

    def __post_init__(self) -> None:
        _text(self.instrument_id, "instrument_id")
        _text(self.calendar_id, "calendar_id")
        _currency(self.currency)
        if not isinstance(self.product, ProductType):
            raise TypeError("product must be a ProductType")
        positive_money(self.initial_face, "initial face")
        object.__setattr__(self, "offers", tuple(sorted(self.offers, key=lambda o: o.offer_date)))
        if isinstance(self.settlement_lag_days, bool) or not isinstance(
            self.settlement_lag_days, int
        ):
            raise TypeError("settlement_lag_days must be an int")
        if self.settlement_lag_days < 0:
            raise InvalidTerms("settlement lag cannot be negative")
        redemptions = self.principal_leg.redemptions
        if redemptions:
            total = math.fsum(r.amount for r in redemptions)
            if not math.isclose(total, self.initial_face, rel_tol=1e-12, abs_tol=1e-9):
                raise InvalidTerms("redemptions must sum to the initial face")
        if self.product is ProductType.FIXED_BOND and isinstance(
            self.coupon_leg, FloatingCouponLeg
        ):
            raise InvalidTerms("a fixed bond cannot carry a floating coupon leg")
        if self.product is ProductType.FLOATING_BOND and not isinstance(
            self.coupon_leg, FloatingCouponLeg
        ):
            raise InvalidTerms("a floating bond needs a floating coupon leg")

    @property
    def maturity_date(self) -> date | None:
        redemptions = self.principal_leg.redemptions
        return redemptions[-1].payment_date if redemptions else None

    @property
    def is_perpetual(self) -> bool:
        return not self.principal_leg.redemptions

    def outstanding_face(self, on: date) -> float:
        """Face per bond after every redemption paid on or before ``on``."""
        paid = math.fsum(r.amount for r in self.principal_leg.redemptions if r.payment_date <= on)
        return max(self.initial_face - paid, 0.0)

    def horizon(self, policy: HorizonPolicy, settlement: date) -> tuple[date, Offer | None]:
        """Horizon date and the offer that sets it, for ``policy`` after ``settlement``.

        ``confirmed_put`` uses the first confirmed put after settlement and falls back
        to maturity when there is none. Calls are never assumed exercised.
        """
        if policy == "confirmed_put":
            for offer in self.offers:
                if (
                    offer.kind is OfferKind.PUT
                    and offer.confirmed
                    and offer.offer_date > settlement
                ):
                    return offer.offer_date, offer
        elif policy != "maturity":
            raise ValueError("horizon policy must be maturity or confirmed_put")
        maturity = self.maturity_date
        if maturity is None:
            raise InvalidTerms("a perpetual bond has no maturity horizon")
        return maturity, None

    def cashflows(
        self,
        *,
        after: date | None = None,
        horizon: date | None = None,
        offer: Offer | None = None,
    ) -> tuple[Cashflow, ...]:
        """Known coupon and principal flows, merged by payment date.

        Only flows paid strictly after ``after`` and on or before ``horizon`` are
        returned. With ``offer`` the outstanding face is redeemed at the offer date at
        ``offer.price_pct`` (kind ``OFFER_REDEMPTION``). Floating coupons are not
        projected here: a floating leg contributes principal only.
        """
        if offer is not None:
            if horizon is not None and horizon != offer.offer_date:
                raise ValueError("an offer horizon must be the offer date")
            horizon = offer.offer_date
        coupons: dict[date, CouponPeriod] = {}
        if isinstance(self.coupon_leg, FixedCouponLeg):
            coupons = {p.payment_date: p for p in self.coupon_leg.periods}
        principal: dict[date, Redemption] = {
            r.payment_date: r for r in self.principal_leg.redemptions
        }
        if offer is not None:
            principal = {d: r for d, r in principal.items() if d < offer.offer_date}
            # Face outstanding just before the offer date, including any redemption due on it.
            remaining = self.outstanding_face(offer.offer_date) + math.fsum(
                r.amount
                for r in self.principal_leg.redemptions
                if r.payment_date == offer.offer_date
            )
            principal[offer.offer_date] = Redemption(
                offer.offer_date, remaining * offer.price_pct / 100.0, CashflowKind.MATURITY
            )
        flows: list[Cashflow] = []
        for paid in sorted(set(coupons) | set(principal)):
            if after is not None and paid <= after:
                continue
            if horizon is not None and paid > horizon:
                continue
            period = coupons.get(paid)
            redemption = principal.get(paid)
            if redemption is None:
                kind = CashflowKind.COUPON
            elif offer is not None and paid == offer.offer_date:
                kind = CashflowKind.OFFER_REDEMPTION
            else:
                kind = redemption.kind
            flows.append(
                Cashflow.split(
                    paid,
                    coupon=0.0 if period is None else period.amount,
                    principal=0.0 if redemption is None else redemption.amount,
                    accrual_start=None if period is None else period.accrual_start,
                    accrual_end=None if period is None else period.accrual_end,
                    kind=kind,
                )
            )
        return tuple(flows)

    def accrued(self, settlement: date) -> float:
        """Accrued coupon per bond at ``settlement``, rounded half-up to 0.01."""
        if isinstance(self.coupon_leg, FloatingCouponLeg):
            raise InvalidTerms("floating accrued interest needs fixings; use the floating leg")
        return accrued_interest(self.cashflows(), settlement)


@dataclass(frozen=True)
class BondPriceObservation:
    """A chosen market price for an instrument (the backend picks the price basis)."""

    instrument_id: str
    clean_price_pct: float
    price_basis: str
    accrued_per_bond: float | None = None
    evidence_id: str | None = None

    def __post_init__(self) -> None:
        _text(self.instrument_id, "instrument_id")
        _text(self.price_basis, "price_basis")
        positive_money(self.clean_price_pct, "clean price")
        if self.accrued_per_bond is not None:
            finite_money(self.accrued_per_bond, "accrued interest")
            if self.accrued_per_bond < 0.0:
                raise InvalidTerms("accrued interest cannot be negative")


@dataclass(frozen=True)
class CurveEntry:
    """A curve under a desk curve ID, built to a registered curve spec."""

    curve_id: str
    spec_id: str
    curve: DiscountCurve | CurveBootstrapRequest

    def __post_init__(self) -> None:
        _text(self.curve_id, "curve_id")
        _text(self.spec_id, "spec_id")
        if "|" in self.curve_id:
            raise InvalidTerms("curve_id cannot contain '|' (it is a factor-key separator)")


@dataclass(frozen=True)
class CalendarEntry:
    calendar_id: str
    calendar: BusinessCalendar

    def __post_init__(self) -> None:
        _text(self.calendar_id, "calendar_id")


@dataclass(frozen=True)
class FixingEntry:
    fixing_id: str
    history: FixingHistory

    def __post_init__(self) -> None:
        _text(self.fixing_id, "fixing_id")


@dataclass(frozen=True)
class FxRate:
    """RUB per one unit of ``currency``."""

    currency: str
    rub_per_unit: float

    def __post_init__(self) -> None:
        _currency(self.currency)
        positive_money(self.rub_per_unit, "FX rate")


@dataclass(frozen=True)
class MarketSnapshot:
    """Everything market-side that positions share for one valuation.

    Collections are stored as tuples sorted by key; look entries up with the
    accessor methods, which raise :class:`MissingMarketData` for an absent key.
    """

    snapshot_id: str
    business_date: date
    known_at: datetime
    curves: tuple[CurveEntry, ...] = ()
    calendars: tuple[CalendarEntry, ...] = ()
    fixings: tuple[FixingEntry, ...] = ()
    fx: tuple[FxRate, ...] = ()
    quotes: tuple[BondPriceObservation, ...] = ()
    evidence: tuple[SnapshotEvidence, ...] = ()

    def __post_init__(self) -> None:
        _text(self.snapshot_id, "snapshot_id")
        _aware(self.known_at, "known_at")
        object.__setattr__(self, "curves", tuple(sorted(self.curves, key=lambda e: e.curve_id)))
        object.__setattr__(
            self, "calendars", tuple(sorted(self.calendars, key=lambda e: e.calendar_id))
        )
        object.__setattr__(self, "fixings", tuple(sorted(self.fixings, key=lambda e: e.fixing_id)))
        object.__setattr__(self, "fx", tuple(sorted(self.fx, key=lambda e: e.currency)))
        object.__setattr__(
            self, "quotes", tuple(sorted(self.quotes, key=lambda e: e.instrument_id))
        )
        object.__setattr__(
            self, "evidence", tuple(sorted(self.evidence, key=lambda e: e.snapshot_id))
        )
        _unique([e.curve_id for e in self.curves], "curve_id")
        _unique([e.calendar_id for e in self.calendars], "calendar_id")
        _unique([e.fixing_id for e in self.fixings], "fixing_id")
        _unique([e.currency for e in self.fx], "FX currency")
        _unique([e.instrument_id for e in self.quotes], "quote instrument_id")
        _unique([e.snapshot_id for e in self.evidence], "evidence snapshot_id")
        if any(e.currency == "RUB" for e in self.fx):
            raise InvalidTerms("RUB has no FX rate against itself")

    def curve(self, curve_id: str) -> CurveEntry:
        for entry in self.curves:
            if entry.curve_id == curve_id:
                return entry
        raise MissingMarketData(f"curve {curve_id!r} is not in snapshot {self.snapshot_id}")

    def calendar(self, calendar_id: str) -> BusinessCalendar:
        for entry in self.calendars:
            if entry.calendar_id == calendar_id:
                return entry.calendar
        raise MissingMarketData(f"calendar {calendar_id!r} is not in snapshot {self.snapshot_id}")

    def fixing(self, fixing_id: str) -> FixingHistory:
        for entry in self.fixings:
            if entry.fixing_id == fixing_id:
                return entry.history
        raise MissingMarketData(f"fixings {fixing_id!r} are not in snapshot {self.snapshot_id}")

    def fx_rate(self, currency: str) -> float:
        if currency == "RUB":
            return 1.0
        for entry in self.fx:
            if entry.currency == currency:
                return entry.rub_per_unit
        raise MissingMarketData(f"FX rate for {currency} is not in snapshot {self.snapshot_id}")

    def quote(self, instrument_id: str) -> BondPriceObservation:
        for entry in self.quotes:
            if entry.instrument_id == instrument_id:
                return entry
        raise MissingMarketData(f"no price for {instrument_id!r} in snapshot {self.snapshot_id}")


@dataclass(frozen=True)
class Position:
    """A signed holding of an instrument in a book; quantity is in bonds (units)."""

    position_id: str
    instrument_id: str
    quantity: float
    book: str
    accounting_category: AccountingCategory
    currency: str
    recovery_rate: float | None = None

    def __post_init__(self) -> None:
        _text(self.position_id, "position_id")
        _text(self.instrument_id, "instrument_id")
        _text(self.book, "book")
        _currency(self.currency)
        finite_money(self.quantity, "quantity")
        if not isinstance(self.accounting_category, AccountingCategory):
            raise TypeError("accounting_category must be an AccountingCategory")
        if self.recovery_rate is not None:
            recovery = finite_money(self.recovery_rate, "recovery_rate")
            if not 0.0 <= recovery <= 1.0:
                raise InvalidTerms("recovery_rate must be between 0 and 1")
