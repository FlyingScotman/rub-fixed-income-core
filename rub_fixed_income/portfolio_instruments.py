"""Derivative positions in the portfolio path (result contract v3).

``Instrument`` is bond-shaped and its encodings are fixed, so each derivative gets its
own frozen position type. Each wraps the terms its product module already defines
(``swaps``, ``fx_forwards``, ``xccy``, ``cds``, ``fx_futures``, ``rate_futures``,
``ofz_futures``, ``repo``) and names the market data
it reads from the shared :class:`~rub_fixed_income.domain.MarketSnapshot` by ID: curves,
fixings and calendars. Valuation choices that belong to the trade (which projection curve,
which fixing history) live here, not on ``ValuationPolicy``, which is part of the job
identity.

Every type has ``instrument_id``, ``product`` and ``currency``. ``currency`` is what a
:class:`~rub_fixed_income.domain.Position` on it must state: RUB for a swap, the foreign
currency of an FX forward, FX swap, XCCY swap or FX future, the contract currency of a
CDS, RUB for rate and OFZ futures and repos. The position's ``quantity`` multiplies the contract as
described (1 = the trade as booked, -1 its mirror); for a future it is the signed number
of contracts.

To add a product: define its position type
here or in its own module, add it to :data:`PortfolioInstrument`, and register a
valuation adapter and a row type in :mod:`rub_fixed_income.portfolio_products`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from ._money import positive_money
from .cds import CdsContract
from .conventions import DayCount
from .domain import Instrument, ProductType
from .errors import InvalidTerms
from .fx_forwards import FxForward, FxSwap
from .fx_futures import FxFuturesContract
from .ofz_futures import OfzFuturesContract
from .rate_futures import (
    KeyRateFuturesContract,
    RateFuturesContract,
    RuoniaAverageFuturesContract,
    RuoniaIndexFuturesContract,
)
from .repo import RepoTrade
from .swaps import InterestRateSwap
from .xccy import XccySwap

__all__ = [
    "CdsInstrument",
    "DerivativeInstrument",
    "FxForwardInstrument",
    "FxFuturesInstrument",
    "OfzDeliverable",
    "OfzFuturesInstrument",
    "PortfolioInstrument",
    "RateFuturesInstrument",
    "RepoInstrument",
    "SwapInstrument",
    "XccyInstrument",
]


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InvalidTerms(f"{label} must be a non-empty string")
    return value


def _optional(value: object, label: str) -> None:
    if value is not None:
        _text(value, label)


@dataclass(frozen=True)
class SwapInstrument:
    """A seasoned RUONIA OIS or key-rate swap (BACKLOG #55).

    - ``calendar_id``: the snapshot calendar the schedule is generated on;
    - ``discount_curve_id``: a RUB nominal curve (OIS quotes, a built OIS curve or caller
      discount factors) dated the business date;
    - ``projection_curve_id``: ``None`` projects RUONIA on the discount curve (single
      curve); otherwise a RUONIA OIS curve, or for a key-rate swap (required) a
      ``rub_key_rate_swap_v1`` entry holding a ``KeyRateForwardCurve`` dated the business
      date (``key_rate.rebase_key_rate_curve`` re-dates a TOM calibration);
    - ``fixing_id``: the snapshot fixing history of the float index, required once a
      remaining period has started; read point-in-time at the snapshot cut.
    """

    instrument_id: str
    swap: InterestRateSwap
    calendar_id: str
    discount_curve_id: str
    projection_curve_id: str | None = None
    fixing_id: str | None = None

    def __post_init__(self) -> None:
        _text(self.instrument_id, "instrument_id")
        if not isinstance(self.swap, InterestRateSwap):
            raise TypeError("swap must be an InterestRateSwap")
        _text(self.calendar_id, "calendar_id")
        _text(self.discount_curve_id, "discount_curve_id")
        _optional(self.projection_curve_id, "projection_curve_id")
        _optional(self.fixing_id, "fixing_id")
        if self.projection_curve_id == self.discount_curve_id:
            raise InvalidTerms("a single-curve swap names no separate projection curve")
        if self.swap.convention.index == "key_rate" and self.projection_curve_id is None:
            raise InvalidTerms("a key-rate swap needs a key-rate projection curve")

    @property
    def product(self) -> ProductType:
        return ProductType.IRS

    @property
    def currency(self) -> str:
        return "RUB"


@dataclass(frozen=True)
class FxForwardInstrument:
    """An outstanding FX forward or FX swap against RUB (BACKLOG #45).

    Valued by covered parity on ``rub_curve_id`` and ``foreign_curve_id`` from the
    snapshot :class:`~rub_fixed_income.domain.FxSpot` of the currency (its value date and
    settlement calendar) and the :class:`~rub_fixed_income.domain.FxBasis` of the pair on
    the foreign curve.
    """

    instrument_id: str
    trade: FxForward | FxSwap
    rub_curve_id: str
    foreign_curve_id: str

    def __post_init__(self) -> None:
        _text(self.instrument_id, "instrument_id")
        if not isinstance(self.trade, (FxForward, FxSwap)):
            raise TypeError("trade must be an FxForward or an FxSwap")
        _text(self.rub_curve_id, "rub_curve_id")
        _text(self.foreign_curve_id, "foreign_curve_id")
        if self.rub_curve_id == self.foreign_curve_id:
            raise InvalidTerms("RUB and foreign curves need distinct curve IDs")

    @property
    def product(self) -> ProductType:
        return ProductType.FX_SWAP if isinstance(self.trade, FxSwap) else ProductType.FX_FORWARD

    @property
    def currency(self) -> str:
        return self.trade.currency


@dataclass(frozen=True)
class XccyInstrument:
    """A cross-currency swap against RUB (BACKLOG #71), with a curve ID per role.

    ``foreign_discount_curve_id`` names the foreign curve under RUB collateral: caller
    discount factors of the currency, a curve implied from FX swaps, or a
    ``cny_ruonia_xccy_v1`` entry holding XCCY par quotes (risk on the quote basis). A
    floating RUB leg projects on ``rub_projection_curve_id`` (``None``: the discount
    curve) with RUONIA fixings ``rub_fixing_id``; a floating foreign leg needs
    ``foreign_projection_curve_id`` and, once started, ``foreign_fixing_id`` (snapshot
    index fixings).
    """

    instrument_id: str
    swap: XccySwap
    rub_discount_curve_id: str
    foreign_discount_curve_id: str
    rub_projection_curve_id: str | None = None
    foreign_projection_curve_id: str | None = None
    rub_fixing_id: str | None = None
    foreign_fixing_id: str | None = None

    def __post_init__(self) -> None:
        _text(self.instrument_id, "instrument_id")
        if not isinstance(self.swap, XccySwap):
            raise TypeError("swap must be an XccySwap")
        _text(self.rub_discount_curve_id, "rub_discount_curve_id")
        _text(self.foreign_discount_curve_id, "foreign_discount_curve_id")
        for name in (
            "rub_projection_curve_id",
            "foreign_projection_curve_id",
            "rub_fixing_id",
            "foreign_fixing_id",
        ):
            _optional(getattr(self, name), name)
        if self.rub_projection_curve_id == self.rub_discount_curve_id:
            raise InvalidTerms("a single-curve RUB leg names no separate projection curve")
        if self.swap.rub_leg.index is None and (
            self.rub_projection_curve_id is not None or self.rub_fixing_id is not None
        ):
            raise InvalidTerms("a fixed RUB leg takes no projection curve or fixings")
        foreign_floating = self.swap.foreign_leg.index is not None
        if foreign_floating and self.foreign_projection_curve_id is None:
            raise InvalidTerms("a floating foreign leg needs foreign_projection_curve_id")
        if not foreign_floating and (
            self.foreign_projection_curve_id is not None or self.foreign_fixing_id is not None
        ):
            raise InvalidTerms("a fixed foreign leg takes no projection curve or fixings")
        ids = [
            i
            for i in (
                self.rub_discount_curve_id,
                self.rub_projection_curve_id,
                self.foreign_discount_curve_id,
                self.foreign_projection_curve_id,
            )
            if i is not None
        ]
        if len(set(ids)) != len(ids):
            raise InvalidTerms("distinct curve roles need distinct curve IDs")

    @property
    def product(self) -> ProductType:
        return ProductType.XCCY_SWAP

    @property
    def currency(self) -> str:
        return self.swap.currency


@dataclass(frozen=True)
class CdsInstrument:
    """A single-name CDS (BACKLOG #66) discounted on ``discount_curve_id``.

    The hazard curve comes from the snapshot
    :class:`~rub_fixed_income.domain.CreditCurveEntry` of the contract's reference entity
    and currency, built with its stated recovery. The discount curve is of the contract
    currency; a non-RUB contract is valued in RUB at the snapshot ``FxRate``.
    """

    instrument_id: str
    contract: CdsContract
    discount_curve_id: str

    def __post_init__(self) -> None:
        _text(self.instrument_id, "instrument_id")
        if not isinstance(self.contract, CdsContract):
            raise TypeError("contract must be a CdsContract")
        _text(self.discount_curve_id, "discount_curve_id")

    @property
    def product(self) -> ProductType:
        return ProductType.CDS

    @property
    def currency(self) -> str:
        return self.contract.currency


@dataclass(frozen=True)
class FxFuturesInstrument:
    """An exchange FX future against RUB (BACKLOG #33, core part).

    Covered parity on ``rub_curve_id`` and ``foreign_curve_id`` from the snapshot
    ``FxSpot``; the basis is calibrated to the snapshot futures price
    (:class:`~rub_fixed_income.domain.FxFuturesQuote`) in ``basis_day_count`` and held
    under every shock. A position's quantity is signed contracts.
    """

    instrument_id: str
    contract: FxFuturesContract
    rub_curve_id: str
    foreign_curve_id: str
    basis_day_count: DayCount = DayCount.ACT_365_FIXED

    def __post_init__(self) -> None:
        _text(self.instrument_id, "instrument_id")
        if not isinstance(self.contract, FxFuturesContract):
            raise TypeError("contract must be an FxFuturesContract")
        _text(self.rub_curve_id, "rub_curve_id")
        _text(self.foreign_curve_id, "foreign_curve_id")
        if self.rub_curve_id == self.foreign_curve_id:
            raise InvalidTerms("RUB and foreign curves need distinct curve IDs")
        if not isinstance(self.basis_day_count, DayCount):
            raise TypeError("basis_day_count must be a DayCount")

    @property
    def product(self) -> ProductType:
        return ProductType.FX_FUTURE

    @property
    def currency(self) -> str:
        return self.contract.currency


@dataclass(frozen=True)
class RateFuturesInstrument:
    """A MOEX RUON, RUONIA index or KEYRATE future (BACKLOG #87).

    RUONIA contracts project on ``projection_curve_id`` (a RUONIA OIS curve) with the
    snapshot RUONIA fixings ``fixing_id`` (declared unpublished days included); the index
    contract also reads the latest published RUONIA index value of the snapshot. A KEYRATE
    contract projects on a ``rub_key_rate_swap_v1`` key-rate curve dated the business date
    and takes no fixings. The basis is calibrated to the snapshot futures price and frozen;
    positions on one instrument share one engine (``for_quantity``).
    """

    instrument_id: str
    contract: RateFuturesContract
    projection_curve_id: str
    fixing_id: str | None = None

    def __post_init__(self) -> None:
        _text(self.instrument_id, "instrument_id")
        if not isinstance(
            self.contract,
            (RuoniaAverageFuturesContract, RuoniaIndexFuturesContract, KeyRateFuturesContract),
        ):
            raise TypeError("contract must be a MOEX rate futures contract")
        _text(self.projection_curve_id, "projection_curve_id")
        _optional(self.fixing_id, "fixing_id")
        key_rate = isinstance(self.contract, KeyRateFuturesContract)
        if key_rate and self.fixing_id is not None:
            raise InvalidTerms("key-rate futures take no RUONIA fixings")
        if not key_rate and self.fixing_id is None:
            raise InvalidTerms("RUONIA futures need a fixing_id")

    @property
    def product(self) -> ProductType:
        return ProductType.RATE_FUTURE

    @property
    def currency(self) -> str:
        return "RUB"


@dataclass(frozen=True)
class OfzDeliverable:
    """A basket bond of an OFZ future: its instrument ID and the exchange conversion factor.

    The bond's terms are an :class:`~rub_fixed_income.domain.Instrument` in the same
    ``instruments`` list (it need not be a position); its price is the snapshot quote.
    """

    instrument_id: str
    conversion_factor: float

    def __post_init__(self) -> None:
        _text(self.instrument_id, "instrument_id")
        object.__setattr__(
            self, "conversion_factor", positive_money(self.conversion_factor, "conversion factor")
        )


@dataclass(frozen=True)
class OfzFuturesInstrument:
    """A deliverable OFZ basket future (BACKLOG #87).

    ``bond_curve_id`` prices the basket (a spread per bond calibrated to its snapshot
    clean price and frozen), ``funding_curve_id`` carries it to delivery; distinct RUB
    curves. The basket prices settle on the bonds' common settlement date (their lag on
    their calendar from the business date). Positions on one instrument share one engine.
    """

    instrument_id: str
    contract: OfzFuturesContract
    basket: tuple[OfzDeliverable, ...]
    bond_curve_id: str
    funding_curve_id: str

    def __post_init__(self) -> None:
        _text(self.instrument_id, "instrument_id")
        if not isinstance(self.contract, OfzFuturesContract):
            raise TypeError("contract must be an OfzFuturesContract")
        basket = tuple(self.basket)
        if not basket or not all(isinstance(b, OfzDeliverable) for b in basket):
            raise InvalidTerms("an OFZ future needs a basket of OfzDeliverable bonds")
        ids = [b.instrument_id for b in basket]
        if len(set(ids)) != len(ids):
            raise InvalidTerms("basket bonds must be distinct")
        object.__setattr__(self, "basket", basket)
        _text(self.bond_curve_id, "bond_curve_id")
        _text(self.funding_curve_id, "funding_curve_id")
        if self.bond_curve_id == self.funding_curve_id:
            raise InvalidTerms("bond and funding curves need distinct curve IDs")

    @property
    def product(self) -> ProductType:
        return ProductType.BOND_FUTURE

    @property
    def currency(self) -> str:
        return "RUB"


@dataclass(frozen=True)
class RepoInstrument:
    """A RUB repo or reverse-repo leg (BACKLOG #94).

    ``calendar_id`` names the settlement calendar; ``discount_curve_id`` a RUB nominal
    curve; a floating repo projects on ``projection_curve_id`` (``None``: the discount
    curve; a RUONIA OIS curve for RUSFAR and RUONIA, a ``rub_key_rate_projection_v1``
    curve for the key rate) with the snapshot fixing history ``fixing_id``.
    ``assumed_end_date`` is the end assumed for an open repo: required for one (else the
    position is rejected ``MISSING_ASSUMPTION``), rejected for a term repo. The
    collateral is a reference only and never enters PV or risk: the cash borrower books
    the bond as its own position.
    """

    instrument_id: str
    trade: RepoTrade
    calendar_id: str
    discount_curve_id: str
    projection_curve_id: str | None = None
    fixing_id: str | None = None
    assumed_end_date: date | None = None

    def __post_init__(self) -> None:
        _text(self.instrument_id, "instrument_id")
        if not isinstance(self.trade, RepoTrade):
            raise TypeError("trade must be a RepoTrade")
        _text(self.calendar_id, "calendar_id")
        _text(self.discount_curve_id, "discount_curve_id")
        _optional(self.projection_curve_id, "projection_curve_id")
        _optional(self.fixing_id, "fixing_id")
        if self.projection_curve_id == self.discount_curve_id:
            raise InvalidTerms("a single-curve repo names no separate projection curve")
        if not self.trade.is_open and self.assumed_end_date is not None:
            raise InvalidTerms("a term repo has its own end date; assumed_end_date is None")

    @property
    def product(self) -> ProductType:
        return ProductType.REPO

    @property
    def currency(self) -> str:
        return self.trade.currency


DerivativeInstrument = (
    SwapInstrument
    | FxForwardInstrument
    | XccyInstrument
    | CdsInstrument
    | FxFuturesInstrument
    | RateFuturesInstrument
    | OfzFuturesInstrument
    | RepoInstrument
)
"""Non-bond position types :func:`~rub_fixed_income.portfolio.value_portfolio` accepts."""

PortfolioInstrument = Instrument | DerivativeInstrument
"""Everything ``value_portfolio(instruments=...)`` accepts; IDs are unique across all."""
