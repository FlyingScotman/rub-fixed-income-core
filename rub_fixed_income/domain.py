"""Core domain model: instruments, a shared market snapshot, positions (roadmap 0.3).

The backend adapter builds these from market-data hub snapshots and private desk
data (ADR-003); core never parses hub payloads. Units follow ``docs/CONVENTIONS.md``:
amounts are per bond in the instrument currency, prices are % of current nominal,
rates are decimals. Every type is frozen, validated on construction and round-trips
through :func:`rub_fixed_income.serialization.to_dict` / ``from_dict``.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from enum import Enum
from itertools import pairwise
from typing import Literal

from ._money import finite_money, positive_money
from .canonical import OMIT_IF_DEFAULT
from .cashflows import Cashflow, CashflowKind, accrued_interest
from .conventions import DEFAULT_YIELD_CONVENTION, YieldConvention
from .dates import BusinessCalendar
from .discount import CurveBootstrapRequest, DiscountCurve
from .errors import InvalidTerms, MissingMarketData
from .floating import FixingHistory, FloatingCouponPeriod, FloatingCouponRule
from .inflation import CpiSeries, IndexedCouponLeg, IndexedPrincipalLeg
from .kbd import ZcycParameters

__all__ = [
    "AccountingCategory",
    "AccrualBasis",
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
    "RateAccrualCouponLeg",
    "RateCouponPeriod",
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


def _check_coupon_period(
    accrual_start: date, accrual_end: date, payment_date: date, amount: float
) -> None:
    finite_money(amount, "coupon amount")
    if amount < 0.0:
        raise InvalidTerms("coupon amount cannot be negative")
    if accrual_end <= accrual_start:
        raise InvalidTerms("coupon accrual period must have positive length")
    if payment_date < accrual_end:
        raise InvalidTerms("a coupon cannot be paid before its accrual period ends")


def _check_coupon_order(periods: Sequence[CouponPeriod | RateCouponPeriod]) -> None:
    for left, right in pairwise(periods):
        if right.accrual_start < left.accrual_end:
            raise InvalidTerms("coupon periods must be ordered and must not overlap")
        if right.payment_date <= left.payment_date:
            raise InvalidTerms("coupon payment dates must increase")


@dataclass(frozen=True)
class CouponPeriod:
    """A coupon known as a fact: amount per bond for ``[accrual_start, accrual_end)``."""

    accrual_start: date
    accrual_end: date
    payment_date: date
    amount: float

    def __post_init__(self) -> None:
        _check_coupon_period(self.accrual_start, self.accrual_end, self.payment_date, self.amount)


@dataclass(frozen=True)
class FixedCouponLeg:
    """Known coupons accruing pro rata on the amount: ``amount * elapsed / period days``.

    This is the Minfin rule for OFZ. MOEX accrues corporate bonds from the coupon
    rate instead; use :class:`RateAccrualCouponLeg` for those.
    """

    periods: tuple[CouponPeriod, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "periods", tuple(self.periods))
        _check_coupon_order(self.periods)


class AccrualBasis(Enum):
    """How a :class:`RateAccrualCouponLeg` accrues inside a period.

    Each accrues ``face * rate * days / year`` on the face outstanding at the period
    start, rounded half-up to 0.01:

    - ``RATE_ACT_365``: actual days over 365 (MOEX corporate bonds);
    - ``RATE_ACT_360``: actual days over 360 (a few bonds whose terms say so);
    - ``RATE_30_360``: 30/360 bond basis days over 360 (replacement bonds of USD
      eurobonds).
    """

    RATE_ACT_365 = "rate:ACT/365F"
    RATE_ACT_360 = "rate:ACT/360"
    RATE_30_360 = "rate:30/360"

    def days(self, start: date, end: date) -> tuple[int, int]:
        """Accrued days from ``start`` to ``end`` and the days in a year."""
        if self is AccrualBasis.RATE_ACT_365:
            return (end - start).days, 365
        if self is AccrualBasis.RATE_ACT_360:
            return (end - start).days, 360
        # 30/360 bond basis (ISDA 2006 4.16(f)): D1 31 -> 30; D2 31 -> 30 when D1 is 30.
        d1 = min(start.day, 30)
        d2 = 30 if end.day == 31 and d1 == 30 else end.day
        return 360 * (end.year - start.year) + 30 * (end.month - start.month) + d2 - d1, 360


@dataclass(frozen=True)
class RateCouponPeriod:
    """A known coupon with its annual ``rate`` (decimal), which drives the accrual."""

    accrual_start: date
    accrual_end: date
    payment_date: date
    amount: float
    rate: float

    def __post_init__(self) -> None:
        _check_coupon_period(self.accrual_start, self.accrual_end, self.payment_date, self.amount)
        finite_money(self.rate, "coupon rate")
        if self.rate < 0.0:
            raise InvalidTerms("coupon rate cannot be negative")


@dataclass(frozen=True)
class RateAccrualCouponLeg:
    """Known coupons whose accrued interest comes from the rate, not the amount.

    Coupons are paid as their ``amount``; accrued interest is computed by
    ``accrual_basis``. MOEX accrues corporate bonds this way, so a period whose coupon
    amount was rounded can accrue a kopeck away from ``amount * elapsed / period``.
    """

    periods: tuple[RateCouponPeriod, ...]
    accrual_basis: AccrualBasis

    def __post_init__(self) -> None:
        object.__setattr__(self, "periods", tuple(self.periods))
        if not isinstance(self.accrual_basis, AccrualBasis):
            raise TypeError("accrual_basis must be an AccrualBasis")
        _check_coupon_order(self.periods)


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

    An inflation-linked bond (package 9a) carries an :class:`IndexedPrincipalLeg` and an
    :class:`IndexedCouponLeg` instead: its face grows with a CPI series, so its flows,
    face and accrued need market data and come from :mod:`rub_fixed_income.linkers`.

    ``yield_convention`` is the issue's yield quote basis, independent of the accrual
    basis: the default is MOEX's effective annual ACT/365F. Some USD replacement bonds
    are quoted on ACT/360 time while they accrue 30/360 (BACKLOG #38). It comes from
    the issue terms (backend static data); core never infers it. Left at the default
    it is not part of the instrument's serialized form or hash.
    """

    instrument_id: str
    product: ProductType
    currency: str
    initial_face: float
    coupon_leg: (
        FixedCouponLeg | RateAccrualCouponLeg | FloatingCouponLeg | IndexedCouponLeg | None
    )
    principal_leg: PrincipalLeg | IndexedPrincipalLeg
    calendar_id: str
    offers: tuple[Offer, ...] = ()
    settlement_lag_days: int = 1
    isin: str | None = None
    issuer_id: str | None = None
    yield_convention: YieldConvention = field(
        default=DEFAULT_YIELD_CONVENTION, metadata=OMIT_IF_DEFAULT
    )

    def __post_init__(self) -> None:
        _text(self.instrument_id, "instrument_id")
        if not isinstance(self.yield_convention, YieldConvention):
            raise TypeError("yield_convention must be a YieldConvention")
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
        if isinstance(self.principal_leg, IndexedPrincipalLeg) or isinstance(
            self.coupon_leg, IndexedCouponLeg
        ):
            self._check_indexed()
            return
        if self.product is ProductType.INFLATION_LINKED_BOND:
            raise InvalidTerms("an inflation-linked bond needs an indexed principal leg")
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

    def _check_indexed(self) -> None:
        principal = self.principal_leg
        if not isinstance(principal, IndexedPrincipalLeg):
            raise InvalidTerms("coupons on an indexed nominal need an indexed principal leg")
        if self.product is not ProductType.INFLATION_LINKED_BOND:
            raise InvalidTerms("an indexed principal leg belongs to an inflation-linked bond")
        if not isinstance(self.coupon_leg, (IndexedCouponLeg, type(None))):
            raise InvalidTerms("an indexed principal pays coupons from an indexed coupon leg")
        if self.offers:
            raise InvalidTerms("offers on an indexed nominal are not modelled")
        if self.coupon_leg is not None:
            periods = self.coupon_leg.periods
            if periods[0].accrual_start < principal.indexation.base_date:
                raise InvalidTerms("coupons cannot accrue before the indexation base date")
            if periods[-1].payment_date > principal.maturity_date:
                raise InvalidTerms("coupons cannot be paid after maturity")

    @property
    def is_indexed(self) -> bool:
        """True when the face grows with an index (an :class:`IndexedPrincipalLeg`)."""
        return isinstance(self.principal_leg, IndexedPrincipalLeg)

    def _redemptions(self) -> tuple[Redemption, ...]:
        if isinstance(self.principal_leg, IndexedPrincipalLeg):
            raise InvalidTerms(
                "an indexed nominal needs a CPI series; use rub_fixed_income.linkers"
            )
        return self.principal_leg.redemptions

    @property
    def maturity_date(self) -> date | None:
        if isinstance(self.principal_leg, IndexedPrincipalLeg):
            return self.principal_leg.maturity_date
        redemptions = self.principal_leg.redemptions
        return redemptions[-1].payment_date if redemptions else None

    @property
    def is_perpetual(self) -> bool:
        return self.maturity_date is None

    def outstanding_face(self, on: date) -> float:
        """Face per bond after every redemption paid on or before ``on``."""
        paid = math.fsum(r.amount for r in self._redemptions() if r.payment_date <= on)
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
        coupons: dict[date, CouponPeriod | RateCouponPeriod] = {}
        if isinstance(self.coupon_leg, (FixedCouponLeg, RateAccrualCouponLeg)):
            coupons = {p.payment_date: p for p in self.coupon_leg.periods}
        principal: dict[date, Redemption] = {r.payment_date: r for r in self._redemptions()}
        if offer is not None:
            principal = {d: r for d, r in principal.items() if d < offer.offer_date}
            # Face outstanding just before the offer date, including any redemption due on it.
            remaining = self.outstanding_face(offer.offer_date) + math.fsum(
                r.amount
                for r in self._redemptions()
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
        """Accrued coupon per bond at ``settlement``, rounded half-up to 0.01.

        A :class:`FixedCouponLeg` accrues pro rata on the coupon amount; a
        :class:`RateAccrualCouponLeg` by its ``accrual_basis``. Settlement on a period
        end gives 0, as does settlement after the last period.
        """
        if isinstance(self.coupon_leg, FloatingCouponLeg):
            raise InvalidTerms("floating accrued interest needs fixings; use the floating leg")
        if isinstance(self.coupon_leg, IndexedCouponLeg):
            raise InvalidTerms("indexed accrued interest needs a CPI series; use the linkers module")
        if isinstance(self.coupon_leg, RateAccrualCouponLeg):
            return self._rate_accrued(self.coupon_leg, settlement)
        return accrued_interest(self.cashflows(), settlement)

    def _rate_accrued(self, leg: RateAccrualCouponLeg, settlement: date) -> float:
        periods = [p for p in leg.periods if p.amount > 0.0]
        for period in periods:
            if period.accrual_start <= settlement < period.accrual_end:
                days, year = leg.accrual_basis.days(period.accrual_start, settlement)
                face = Decimal(repr(self.outstanding_face(period.accrual_start)))
                raw = face * Decimal(repr(period.rate)) * days / year
                return float(raw.quantize(Decimal("0.01"), ROUND_HALF_UP))
        if not periods or settlement >= periods[-1].accrual_end:
            return 0.0
        raise InvalidTerms("no coupon accrual period contains the settlement date")


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
    """A curve under a desk curve ID, built to a registered curve spec.

    ``curve`` is a built curve, OIS quotes to bootstrap, or KBD parameters; the
    valuation builds and shares it once per call.
    """

    curve_id: str
    spec_id: str
    curve: DiscountCurve | CurveBootstrapRequest | ZcycParameters

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
    cpi: tuple[CpiSeries, ...] = field(default=(), metadata=OMIT_IF_DEFAULT)

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
        object.__setattr__(self, "cpi", tuple(sorted(self.cpi, key=lambda e: e.series_id)))
        _unique([e.series_id for e in self.cpi], "CPI series_id")
        for series in self.cpi:
            if not isinstance(series, CpiSeries):
                raise TypeError("cpi entries must be CpiSeries")
            if series.as_of > self.business_date:
                raise InvalidTerms(
                    f"CPI series {series.series_id} is cut after the snapshot business date"
                )
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

    def cpi_series(self, series_id: str) -> CpiSeries:
        for series in self.cpi:
            if series.series_id == series_id:
                return series
        raise MissingMarketData(f"CPI series {series_id!r} is not in snapshot {self.snapshot_id}")

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
