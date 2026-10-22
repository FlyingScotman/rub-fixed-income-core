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
from typing import Literal, cast

from ._money import finite_money, positive_money
from .canonical import OMIT_IF_DEFAULT
from .cashflows import Cashflow, CashflowKind, accrued_interest
from .cds import CdsConvention, CdsSpreadQuote, CdsUpfrontQuote
from .conventions import DEFAULT_YIELD_CONVENTION, DayCount, YieldConvention
from .curve_specs import CurveKind, _check_ois_spec_convention, get_curve_spec
from .dates import BusinessCalendar
from .discount import CurveBootstrapRequest, DiscountCurve, bootstrap_discount_curve
from .errors import InvalidTerms, MissingMarketData, UnsupportedProduct
from .floating import (
    FixingHistory,
    FixingIndex,
    FloatingCouponPeriod,
    FloatingCouponProjection,
    FloatingCouponRule,
    FloatingLeg,
)
from .inflation import CpiSeries, IndexedCouponLeg, IndexedPrincipalLeg
from .kbd import ZcycParameters, kbd_discount_curve
from .key_rate import KeyRateForwardCurve
from .rate_futures import RuoniaFixings, RuoniaIndexValue
from .xccy import IndexFixings, XccyCurveBootstrapRequest

__all__ = [
    "AccountingCategory",
    "AccrualBasis",
    "BondPriceObservation",
    "CalendarEntry",
    "CouponPeriod",
    "CreditCurveEntry",
    "CurveEntry",
    "FixedCouponLeg",
    "FixingEntry",
    "FloatingCouponLeg",
    "FuturesQuote",
    "FxBasis",
    "FxRate",
    "FxSpot",
    "IndexFixingEntry",
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
    "RuoniaFixingsEntry",
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
    """A floater formula over its periods; ``fixing_id`` keys the snapshot's history.

    ``projection_curve_id`` names the snapshot curve that projects unfixed observations
    (a RUONIA OIS curve for RUONIA, a ``rub_key_rate_projection_v1`` curve for the key
    rate). It may equal the discount curve (single-curve). A leg without one is not
    projected: :meth:`Instrument.cashflows` then returns its principal only and
    portfolio valuation rejects it as unsupported.

    ``accrual_basis`` (0.33.0) is how a coupon known in advance accrues: None, pro rata
    on its amount (OFZ-PK, the Minfin rule); ``AccrualBasis.RATE_ACT_365``, on its rate
    (MOEX corporate floaters, as fixed corporates). See :meth:`Instrument.accrued`.
    """

    rule: FloatingCouponRule
    periods: tuple[FloatingCouponPeriod, ...]
    fixing_id: str
    projection_curve_id: str | None = field(default=None, metadata=OMIT_IF_DEFAULT)
    accrual_basis: AccrualBasis | None = field(default=None, metadata=OMIT_IF_DEFAULT)

    def __post_init__(self) -> None:
        object.__setattr__(self, "periods", tuple(self.periods))
        _text(self.fixing_id, "fixing_id")
        if self.accrual_basis is not None and not isinstance(self.accrual_basis, AccrualBasis):
            raise TypeError("accrual_basis must be an AccrualBasis or None")
        if not self.periods:
            raise InvalidTerms("a floating leg needs at least one period")
        if self.projection_curve_id is not None:
            _text(self.projection_curve_id, "projection_curve_id")
            if "|" in self.projection_curve_id:
                raise InvalidTerms("projection_curve_id cannot contain '|'")


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
    """A put or call date; the redemption is ``price_pct`` of the outstanding face.

    ``window_start`` / ``window_end`` (0.33.0, optional) bound the period in which holders
    submit a put (MOEX bondization ``offerdatestart`` / ``offerdateend``). A put whose
    window closed before the settlement date can no longer be exercised: the horizon
    policy ``open_put`` skips it. Left at None they are not part of the offer's bytes.
    """

    offer_date: date
    kind: OfferKind
    confirmed: bool
    price_pct: float = 100.0
    window_start: date | None = field(default=None, metadata=OMIT_IF_DEFAULT)
    window_end: date | None = field(default=None, metadata=OMIT_IF_DEFAULT)

    def __post_init__(self) -> None:
        positive_money(self.price_pct, "offer price")
        if not isinstance(self.kind, OfferKind):
            raise TypeError("offer kind must be an OfferKind")
        for name in ("window_start", "window_end"):
            value = getattr(self, name)
            if value is not None and (isinstance(value, datetime) or not isinstance(value, date)):
                raise TypeError(f"offer {name} must be a date or None")
        if self.window_end is not None and self.window_end > self.offer_date:
            raise InvalidTerms("an offer's exercise window cannot end after the offer date")
        if (
            self.window_start is not None
            and self.window_end is not None
            and self.window_end < self.window_start
        ):
            raise InvalidTerms("an offer's exercise window cannot end before it starts")

    def is_open(self, on: date) -> bool:
        """Whether the exercise window has not closed by ``on`` (no window end: open)."""
        return self.window_end is None or self.window_end >= on


HorizonPolicy = Literal["maturity", "confirmed_put", "open_put"]


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
    coupon_leg: FixedCouponLeg | RateAccrualCouponLeg | FloatingCouponLeg | IndexedCouponLeg | None
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
        to maturity when there is none. ``open_put`` uses the first put after settlement
        whose exercise window is still open on the settlement date
        (:meth:`Offer.is_open`), confirmed or not: MOEX's yield-to-offer horizon. Both
        skip an offer on or after maturity. Calls are never assumed exercised.
        """
        maturity = self.maturity_date
        if policy in ("confirmed_put", "open_put"):
            for offer in self.offers:
                if offer.kind is not OfferKind.PUT or offer.offer_date <= settlement:
                    continue
                if maturity is not None and offer.offer_date >= maturity and policy == "open_put":
                    continue
                if policy == "confirmed_put" and offer.confirmed:
                    return offer.offer_date, offer
                if policy == "open_put" and offer.is_open(settlement):
                    return offer.offer_date, offer
        elif policy != "maturity":
            raise ValueError("horizon policy must be maturity, confirmed_put or open_put")
        if maturity is None:
            raise InvalidTerms("a perpetual bond has no maturity horizon")
        return maturity, None

    def floating_leg(
        self,
        market: MarketSnapshot,
        *,
        horizon: date | None = None,
        strike_guard_bp: float | None = None,
    ) -> FloatingLeg:
        """The floating coupon model: this leg's periods paid on or before ``horizon``
        with the snapshot's point-in-time fixings (:meth:`MarketSnapshot.point_in_time_fixing`).

        ``strike_guard_bp`` is the cap/floor model policy of :class:`FloatingLeg`.
        """
        leg = self.coupon_leg
        if not isinstance(leg, FloatingCouponLeg):
            raise InvalidTerms("the instrument has no floating coupon leg")
        history = market.point_in_time_fixing(leg.fixing_id)
        if history.index != leg.rule.index:
            raise InvalidTerms(
                f"fixings {leg.fixing_id!r} are {history.index}, the coupon is on {leg.rule.index}"
            )
        periods = tuple(p for p in leg.periods if horizon is None or p.payment_date <= horizon)
        if not periods:
            raise InvalidTerms("no floating coupon period is paid by the horizon")
        return FloatingLeg(leg.rule, periods, history, strike_guard_bp=strike_guard_bp)

    def cashflows(
        self,
        *,
        after: date | None = None,
        horizon: date | None = None,
        offer: Offer | None = None,
        market: MarketSnapshot | None = None,
        strike_guard_bp: float | None = None,
    ) -> tuple[Cashflow, ...]:
        """Known coupon and principal flows, merged by payment date.

        Only flows paid strictly after ``after`` and on or before ``horizon`` are
        returned. With ``offer`` the outstanding face is redeemed at the offer date at
        ``offer.price_pct`` (kind ``OFFER_REDEMPTION``).

        A floating leg contributes principal only, unless ``market`` is given: its
        coupons are then projected off the leg's ``projection_curve_id`` with the
        snapshot's point-in-time fixings (see :meth:`floating_leg`); known fixings and
        published coupons are used as they are.
        """
        if offer is not None:
            if horizon is not None and horizon != offer.offer_date:
                raise ValueError("an offer horizon must be the offer date")
            horizon = offer.offer_date
        coupons: dict[date, CouponPeriod | RateCouponPeriod | FloatingCouponProjection] = {}
        if isinstance(self.coupon_leg, (FixedCouponLeg, RateAccrualCouponLeg)):
            coupons = {p.payment_date: p for p in self.coupon_leg.periods}
        elif isinstance(self.coupon_leg, FloatingCouponLeg) and market is not None:
            coupons = {
                c.payment_date: c
                for c in self._projected_coupons(market, after, horizon, strike_guard_bp)
            }
        principal: dict[date, Redemption] = {r.payment_date: r for r in self._redemptions()}
        if offer is not None:
            principal = {d: r for d, r in principal.items() if d < offer.offer_date}
            # Face outstanding just before the offer date, including any redemption due on it.
            remaining = self.outstanding_face(offer.offer_date) + math.fsum(
                r.amount for r in self._redemptions() if r.payment_date == offer.offer_date
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
            coupon = 0.0
            if isinstance(period, FloatingCouponProjection):
                coupon = period.amount_rub
            elif period is not None:
                coupon = period.amount
            flows.append(
                Cashflow.split(
                    paid,
                    coupon=coupon,
                    principal=0.0 if redemption is None else redemption.amount,
                    accrual_start=None if period is None else period.accrual_start,
                    accrual_end=None if period is None else period.accrual_end,
                    kind=kind,
                )
            )
        return tuple(flows)

    def _projected_coupons(
        self,
        market: MarketSnapshot,
        after: date | None,
        horizon: date | None,
        strike_guard_bp: float | None,
    ) -> tuple[FloatingCouponProjection, ...]:
        leg = self.coupon_leg
        assert isinstance(leg, FloatingCouponLeg)
        if leg.projection_curve_id is None:
            raise UnsupportedProduct(
                f"floating leg of {self.instrument_id} names no projection curve"
            )
        model = self.floating_leg(market, horizon=horizon, strike_guard_bp=strike_guard_bp)
        source = market.projection_curve(leg.projection_curve_id, leg.rule.index)
        curve = (
            bootstrap_discount_curve(source)
            if isinstance(source, CurveBootstrapRequest)
            else source
        )
        return model.project(curve, settlement_date=date.min if after is None else after)

    def accrued(self, settlement: date, market: MarketSnapshot | None = None) -> float:
        """Accrued coupon per bond at ``settlement``, rounded half-up to 0.01.

        A :class:`FixedCouponLeg` accrues pro rata on the coupon amount; a
        :class:`RateAccrualCouponLeg` by its ``accrual_basis``. Settlement on a period
        end gives 0, as does settlement after the last period.

        A floater needs ``market`` (0.33.0): its fixings and, when the elapsed part of
        the running coupon still needs a projected rate, its projection curve
        (:meth:`FloatingLeg.accrued_interest`).
        """
        if isinstance(self.coupon_leg, FloatingCouponLeg):
            if market is None:
                raise InvalidTerms("floating accrued interest needs fixings; pass the market")
            return self._floating_accrued(market, settlement)
        if isinstance(self.coupon_leg, IndexedCouponLeg):
            raise InvalidTerms(
                "indexed accrued interest needs a CPI series; use the linkers module"
            )
        if isinstance(self.coupon_leg, RateAccrualCouponLeg):
            return self._rate_accrued(self.coupon_leg, settlement)
        return accrued_interest(self.cashflows(), settlement)

    def _floating_accrued(self, market: MarketSnapshot, settlement: date) -> float:
        leg = self.coupon_leg
        assert isinstance(leg, FloatingCouponLeg)
        model = self.floating_leg(market)
        basis = leg.accrual_basis
        try:
            return model.accrued_interest(settlement, rate_basis=basis)
        except MissingMarketData:
            if leg.projection_curve_id is None:
                raise
        source = market.projection_curve(leg.projection_curve_id, leg.rule.index)
        curve = (
            bootstrap_discount_curve(source)
            if isinstance(source, CurveBootstrapRequest)
            else source
        )
        return model.accrued_interest(settlement, curve, rate_basis=basis)

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
    valuation builds and shares it once per call. Derivative positions (result contract
    v3) also read a :class:`~rub_fixed_income.key_rate.KeyRateForwardCurve` under
    ``rub_key_rate_swap_v1`` (key-rate swap projection, dated the business date) and an
    :class:`~rub_fixed_income.xccy.XccyCurveBootstrapRequest` under
    ``cny_ruonia_xccy_v1`` (the CNY curve from XCCY par quotes). Bonds reject both.
    """

    curve_id: str
    spec_id: str
    curve: (
        DiscountCurve
        | CurveBootstrapRequest
        | ZcycParameters
        | KeyRateForwardCurve
        | XccyCurveBootstrapRequest
    )

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


def _foreign(value: object, label: str) -> str:
    currency = _currency(value)
    if currency == "RUB":
        raise InvalidTerms(f"{label} must be a currency other than RUB")
    return currency


@dataclass(frozen=True)
class FxSpot:
    """A spot FX rate with its value date (derivative positions, result contract v3).

    ``rub_per_unit`` RUB per one unit of ``currency`` for settlement on ``value_date``;
    ``calendar_id`` names the pair's settlement calendar among the snapshot calendars
    (usually a joint calendar, ``fx_forwards.joint_calendar``). The backend chooses the
    rate (for example the MOEX ``TOM`` close) and records it in the evidence. Bonds keep
    converting at :class:`FxRate`, which has no value date.
    """

    currency: str
    rub_per_unit: float
    value_date: date
    calendar_id: str
    evidence_id: str | None = None

    def __post_init__(self) -> None:
        _foreign(self.currency, "FX spot currency")
        object.__setattr__(self, "rub_per_unit", positive_money(self.rub_per_unit, "FX spot"))
        if not isinstance(self.value_date, date):
            raise TypeError("value_date must be a date")
        _text(self.calendar_id, "calendar_id")


@dataclass(frozen=True)
class FxBasis:
    """The continuous FX-forward basis of a pair on one foreign curve, in bp.

    Covered parity with ``foreign_curve_id`` is corrected by ``exp(b * tau(s, T))`` from
    the spot date (``fx_forwards``, ``xccy``): zero on a curve implied from FX swaps or
    bootstrapped from XCCY quotes, calibrated (``fx_forward_basis_bp``) on, for example,
    a foreign OIS curve. Always explicit: a pair without an entry is rejected.
    """

    currency: str
    foreign_curve_id: str
    basis_bp: float
    day_count: DayCount = DayCount.ACT_365_FIXED

    def __post_init__(self) -> None:
        _foreign(self.currency, "FX basis currency")
        _text(self.foreign_curve_id, "foreign_curve_id")
        object.__setattr__(self, "basis_bp", finite_money(self.basis_bp, "FX basis bp"))
        if not isinstance(self.day_count, DayCount):
            raise TypeError("day_count must be a DayCount")


@dataclass(frozen=True)
class CreditCurveEntry:
    """Quoted CDS curve of one reference entity in one currency, with its recovery.

    ``reference_entity`` is the backend's entity code (seniority and documentation
    clause belong in it); ``recovery_rate`` is the stated recovery the hazard curve is
    built with. There is no default recovery. Quotes are par spreads or upfronts of
    standard contracts traded on the business date under ``convention``.
    """

    reference_entity: str
    currency: str
    quotes: tuple[CdsSpreadQuote | CdsUpfrontQuote, ...]
    recovery_rate: float
    convention: CdsConvention
    evidence_id: str | None = None

    def __post_init__(self) -> None:
        _text(self.reference_entity, "reference_entity")
        _currency(self.currency)
        quotes = tuple(sorted(self.quotes, key=lambda q: q.maturity))
        if not quotes:
            raise InvalidTerms("a credit curve needs at least one quote")
        for quote in quotes:
            if not isinstance(quote, (CdsSpreadQuote, CdsUpfrontQuote)):
                raise TypeError("credit quotes must be CdsSpreadQuote or CdsUpfrontQuote")
        _unique([q.maturity.isoformat() for q in quotes], "credit quote maturity")
        object.__setattr__(self, "quotes", quotes)
        recovery = finite_money(self.recovery_rate, "recovery_rate")
        if not 0.0 <= recovery < 1.0:
            raise InvalidTerms("a credit curve recovery must be in [0, 1)")
        if not isinstance(self.convention, CdsConvention):
            raise TypeError("convention must be a CdsConvention")


@dataclass(frozen=True)
class FuturesQuote:
    """Market price of a futures contract and the price positions were last margined at.

    Both in the contract's price units (FX futures: RUB per ``price_quote_units`` foreign
    units; RUON: 100 - rate; index futures: index points; OFZ futures: RUB per lot).
    ``price`` is the settlement price or mark the basis is calibrated to;
    ``margined_price`` is the settlement price of the last clearing, so pending variation
    margin is the contract's margin per point times ``price - margined_price``.
    """

    contract_id: str
    price: float
    margined_price: float
    evidence_id: str | None = None

    def __post_init__(self) -> None:
        _text(self.contract_id, "contract_id")
        object.__setattr__(self, "price", positive_money(self.price, "futures price"))
        object.__setattr__(
            self, "margined_price", positive_money(self.margined_price, "margined price")
        )


@dataclass(frozen=True)
class RuoniaFixingsEntry:
    """RUONIA fixings with the declared unpublished days (rate futures, BACKLOG #87).

    ``fixings`` must be dated the snapshot business date. A business day without a
    fixing that is not in ``unpublished_dates`` is an undeclared gap and rejects the
    positions that read it (``UNDECLARED_FIXING_GAP``); nothing is filled.
    """

    fixing_id: str
    fixings: RuoniaFixings

    def __post_init__(self) -> None:
        _text(self.fixing_id, "fixing_id")
        if not isinstance(self.fixings, RuoniaFixings):
            raise TypeError("fixings must be RuoniaFixings")


@dataclass(frozen=True)
class IndexFixingEntry:
    """Published fixings of a foreign overnight index (XCCY floating foreign legs)."""

    fixing_id: str
    fixings: IndexFixings

    def __post_init__(self) -> None:
        _text(self.fixing_id, "fixing_id")
        if not isinstance(self.fixings, IndexFixings):
            raise TypeError("fixings must be IndexFixings")


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
    fx_spots: tuple[FxSpot, ...] = field(default=(), metadata=OMIT_IF_DEFAULT)
    fx_bases: tuple[FxBasis, ...] = field(default=(), metadata=OMIT_IF_DEFAULT)
    credit_curves: tuple[CreditCurveEntry, ...] = field(default=(), metadata=OMIT_IF_DEFAULT)
    futures_quotes: tuple[FuturesQuote, ...] = field(default=(), metadata=OMIT_IF_DEFAULT)
    index_fixings: tuple[IndexFixingEntry, ...] = field(default=(), metadata=OMIT_IF_DEFAULT)
    ruonia_fixings: tuple[RuoniaFixingsEntry, ...] = field(default=(), metadata=OMIT_IF_DEFAULT)
    ruonia_index: tuple[RuoniaIndexValue, ...] = field(default=(), metadata=OMIT_IF_DEFAULT)

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
        self._check_derivative_inputs()

    def _check_derivative_inputs(self) -> None:
        """Sort and validate the inputs of derivative positions (all empty by default)."""
        object.__setattr__(self, "fx_spots", tuple(sorted(self.fx_spots, key=lambda e: e.currency)))
        _unique([e.currency for e in self.fx_spots], "FX spot currency")
        for spot in self.fx_spots:
            if not isinstance(spot, FxSpot):
                raise TypeError("fx_spots entries must be FxSpot")
            if spot.value_date < self.business_date:
                raise InvalidTerms(f"FX spot {spot.currency} has a value date before the snapshot")
        object.__setattr__(
            self,
            "fx_bases",
            tuple(sorted(self.fx_bases, key=lambda e: (e.currency, e.foreign_curve_id))),
        )
        _unique([f"{e.currency}|{e.foreign_curve_id}" for e in self.fx_bases], "FX basis")
        object.__setattr__(
            self,
            "credit_curves",
            tuple(sorted(self.credit_curves, key=lambda e: (e.reference_entity, e.currency))),
        )
        _unique([f"{e.reference_entity}|{e.currency}" for e in self.credit_curves], "credit curve")
        object.__setattr__(
            self,
            "futures_quotes",
            tuple(sorted(self.futures_quotes, key=lambda e: e.contract_id)),
        )
        _unique([e.contract_id for e in self.futures_quotes], "futures quote contract_id")
        object.__setattr__(
            self, "index_fixings", tuple(sorted(self.index_fixings, key=lambda e: e.fixing_id))
        )
        _unique([e.fixing_id for e in self.index_fixings], "index fixing_id")
        object.__setattr__(
            self, "ruonia_fixings", tuple(sorted(self.ruonia_fixings, key=lambda e: e.fixing_id))
        )
        _unique([e.fixing_id for e in self.ruonia_fixings], "RUONIA fixing_id")
        fixing_ids = [
            *(e.fixing_id for e in self.fixings),
            *(e.fixing_id for e in self.index_fixings),
            *(e.fixing_id for e in self.ruonia_fixings),
        ]
        _unique(fixing_ids, "fixing_id across fixing collections")
        object.__setattr__(
            self, "ruonia_index", tuple(sorted(self.ruonia_index, key=lambda e: e.index_date))
        )
        _unique([e.index_date.isoformat() for e in self.ruonia_index], "RUONIA index date")
        for value in self.ruonia_index:
            if not isinstance(value, RuoniaIndexValue):
                raise TypeError("ruonia_index entries must be RuoniaIndexValue")
            if value.index_date > self.business_date:
                raise InvalidTerms("a RUONIA index value cannot postdate the snapshot")

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

    def point_in_time_fixing(self, fixing_id: str) -> FixingHistory:
        """The fixings under ``fixing_id`` as known at this snapshot's ``known_at``.

        The history must be dated the business date and say when it was known: a
        ``known_at_utc`` after ``known_at`` is rejected, and with per-row
        ``published_at`` every row published after ``known_at`` is dropped (a dropped
        RUONIA day is then missing or unpublished, never filled). A history with
        neither is not point-in-time and is rejected.
        """
        history = self.fixing(fixing_id)
        label = f"fixings {fixing_id!r}"
        if history.valuation_date != self.business_date:
            raise InvalidTerms(f"{label} are not dated the snapshot business date")
        try:
            known = history.known_at
        except ValueError as exc:
            raise InvalidTerms(f"{label}: {exc}") from exc
        if known is None and not history.published_at:
            raise InvalidTerms(f"{label} carry neither known_at_utc nor publication times")
        if known is not None and known > self.known_at:
            raise InvalidTerms(f"{label} were known after the snapshot cut {self.known_at}")
        return history.published_by(self.known_at) if history.published_at else history

    def projection_curve(
        self, curve_id: str, index: FixingIndex
    ) -> DiscountCurve | CurveBootstrapRequest:
        """The curve under ``curve_id`` checked as a projection curve for ``index``.

        RUONIA projects off a RUONIA OIS spec (quotes or the built curve), the key rate
        off a ``rub_key_rate_projection_v1`` curve, the KBD index off a KBD spec (its ZCYC
        parameters, built with the spec's shock unit, or the built curve); all must be
        dated the business date.
        """
        entry = self.curve(curve_id)
        spec = get_curve_spec(entry.spec_id)
        source = entry.curve
        expected = {
            "ruonia": CurveKind.OIS_BOOTSTRAP,
            "key_rate": CurveKind.KEY_RATE_PROJECTION,
            "kbd": CurveKind.KBD_PARAMETRIC,
        }
        if spec.kind is not expected[index]:
            raise InvalidTerms(f"curve {curve_id} ({spec.spec_id}) cannot project the {index}")
        if spec.kind is CurveKind.KBD_PARAMETRIC:
            shock = cast(Literal["continuous", "annual"], spec.zero_shock)
            if isinstance(source, ZcycParameters):
                if source.trade_date != self.business_date:
                    raise InvalidTerms(f"curve {curve_id} is not dated the snapshot business date")
                return kbd_discount_curve(source, zero_shock=shock)
            if not (
                isinstance(source, DiscountCurve)
                and source.model_version == spec.model_version
                and source.convention.zero_shock == shock
            ):
                raise InvalidTerms(f"curve {curve_id} does not match spec {spec.spec_id}")
        if spec.kind is CurveKind.OIS_BOOTSTRAP and isinstance(
            source, (CurveBootstrapRequest, DiscountCurve)
        ):
            _check_ois_spec_convention(curve_id, spec.spec_id, source.convention)
        if isinstance(source, CurveBootstrapRequest) and spec.kind is CurveKind.OIS_BOOTSTRAP:
            if source.convention.fixed_leg_schedule is not None:
                raise InvalidTerms(
                    f"curve {curve_id}: {spec.spec_id} uses the legacy fixed-leg schedule"
                )
        elif not (isinstance(source, DiscountCurve) and source.model_version == spec.model_version):
            raise InvalidTerms(f"curve {curve_id} does not match spec {spec.spec_id}")
        if source.curve_date != self.business_date:
            raise InvalidTerms(f"curve {curve_id} is not dated the snapshot business date")
        return source

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

    def fx_spot(self, currency: str) -> FxSpot:
        for entry in self.fx_spots:
            if entry.currency == currency:
                return entry
        raise MissingMarketData(
            f"FX spot with a value date for {currency} is not in snapshot {self.snapshot_id}"
        )

    def fx_basis(self, currency: str, foreign_curve_id: str) -> FxBasis:
        for entry in self.fx_bases:
            if entry.currency == currency and entry.foreign_curve_id == foreign_curve_id:
                return entry
        raise MissingMarketData(
            f"no FX basis for {currency} on curve {foreign_curve_id!r} in snapshot "
            f"{self.snapshot_id}"
        )

    def credit_curve(self, reference_entity: str, currency: str) -> CreditCurveEntry:
        for entry in self.credit_curves:
            if entry.reference_entity == reference_entity and entry.currency == currency:
                return entry
        raise MissingMarketData(
            f"no {currency} credit curve for {reference_entity!r} in snapshot {self.snapshot_id}"
        )

    def futures_quote(self, contract_id: str) -> FuturesQuote:
        for entry in self.futures_quotes:
            if entry.contract_id == contract_id:
                return entry
        raise MissingMarketData(
            f"no futures price for {contract_id!r} in snapshot {self.snapshot_id}"
        )

    def index_fixing(self, fixing_id: str) -> IndexFixings:
        """Foreign index fixings under ``fixing_id``, dated the business date."""
        for entry in self.index_fixings:
            if entry.fixing_id == fixing_id:
                if entry.fixings.valuation_date != self.business_date:
                    raise InvalidTerms(
                        f"index fixings {fixing_id!r} are not dated the snapshot business date"
                    )
                return entry.fixings
        raise MissingMarketData(
            f"index fixings {fixing_id!r} are not in snapshot {self.snapshot_id}"
        )

    def ruonia_fixing(self, fixing_id: str) -> RuoniaFixings:
        """RUONIA fixings (with declared unpublished days) under ``fixing_id``."""
        for entry in self.ruonia_fixings:
            if entry.fixing_id == fixing_id:
                if entry.fixings.valuation_date != self.business_date:
                    raise InvalidTerms(
                        f"RUONIA fixings {fixing_id!r} are not dated the snapshot business date"
                    )
                return entry.fixings
        raise MissingMarketData(
            f"RUONIA fixings {fixing_id!r} are not in snapshot {self.snapshot_id}"
        )

    def latest_ruonia_index(self) -> RuoniaIndexValue:
        """The latest published RUONIA index value (on or before the business date)."""
        if not self.ruonia_index:
            raise MissingMarketData(f"no RUONIA index value in snapshot {self.snapshot_id}")
        return self.ruonia_index[-1]

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
