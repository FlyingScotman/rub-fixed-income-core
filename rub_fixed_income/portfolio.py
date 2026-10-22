"""Portfolio valuation over the domain model (M1 entry point for the desk backend).

``value_portfolio`` prices every position of a book against one shared
:class:`MarketSnapshot` and returns a versioned, hash-verified result in the desk
factor vocabulary. Supported in 0.11.0: fixed-coupon RUB bonds (bullet, amortizing,
confirmed put) on a KBD curve, a prebuilt discount curve or RUONIA OIS quotes.
Package 3 adds CNY, USD and EUR bonds: each is discounted on the curve that the policy
names for its currency (``ValuationPolicy.foreign_curves``), valued in RUB at the
snapshot FX rate, and carries ``FX|<ccy>`` with its cross terms. Since 0.34.0 that
curve can also be the built CNY XCCY curve (``cny_ruonia_xccy_v1``) or a USD / EUR
government par-yield proxy (``proxy_curves``), with explicit same-currency fallbacks
(``ValuationPolicy.foreign_curve_fallbacks``) flagged on the row. Everything else is a
typed rejection recorded in coverage, never a zero.

Each curve is built once per call and its shocked versions are shared across
positions. Settlement is the instrument's lag in business days of its own calendar
from the snapshot's business date; dates outside a bounded calendar are rejected.
"""

from __future__ import annotations

import json
import math
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime
from itertools import pairwise
from typing import Literal, cast

from ._money import finite_money, positive_money
from ._risk_common import _shifts
from ._version import __version__
from .bond import YieldRequest, solve_effective_annual_yield
from .callables import ExerciseAssumption, ExercisedBond, exercised_instrument, resolve_exercise
from .canonical import OMIT_IF_DEFAULT, canonical_json_bytes, canonical_sha256
from .cashflows import BondCashflow, clean_price_pct_to_dirty_rub
from .compatibility import bond_duration
from .conventions import (
    ANNUAL,
    DEFAULT_YIELD_CONVENTION,
    YieldConvention,
    ZSpreadConvention,
    calendar_fingerprint,
)
from .curve_specs import CurveKind, _check_ois_spec_convention, get_curve_spec
from .dates import BusinessCalendar
from .discount import CurveBootstrapRequest, DiscountCurve
from .domain import (
    AccountingCategory,
    BondPriceObservation,
    CurveEntry,
    FixedCouponLeg,
    FloatingCouponLeg,
    FxRate,
    FxSpot,
    Instrument,
    MarketSnapshot,
    Offer,
    Position,
    ProductType,
    RateAccrualCouponLeg,
)
from .errors import CoreRejection, InvalidTerms, MissingMarketData, UnsupportedProduct
from .factors import (
    FactorRisk,
    bond_factor_risk,
    fx_translated_factor_risk,
    linker_factor_risk,
)
from .floating import FixingIndex
from .horizon import HORIZON_MODEL_VERSION, FxHorizon, HorizonValuation, horizon_valuation
from .horizon import fx_horizon as fx_horizon_rule
from .inflation import IndexedPrincipalLeg
from .kbd import KBD_MODEL_VERSION, ZcycParameters, g_spread_bp, kbd_discount_curve
from .linkers import linker_accrued, linker_nominal, real_cashflows
from .local_risk import (
    FixedRubBondInput,
    PreparedLocalBondRisk,
    RiskRunContext,
    prepare_local_bond_risk,
)
from .portfolio_instruments import PortfolioInstrument
from .portfolio_products import (
    HORIZON_ROW_FIELDS,
    ROW_FIELDS,
    CdsPositionHorizon,
    CdsPositionValuation,
    CreditCurveUse,
    FxForwardPositionHorizon,
    FxForwardPositionValuation,
    FxFuturesPositionHorizon,
    FxFuturesPositionValuation,
    HessianTotal,
    HorizonRow,
    JtdTotal,
    OfzFuturesPositionHorizon,
    OfzFuturesPositionValuation,
    PositionModel,
    ProductContext,
    ProductOutcome,
    ProductRow,
    RateFuturesPositionHorizon,
    RateFuturesPositionValuation,
    RepoPositionHorizon,
    RepoPositionValuation,
    SwapPositionHorizon,
    SwapPositionValuation,
    XccyPositionHorizon,
    XccyPositionValuation,
    adapter_for,
    hessian_totals,
    horizon_adapter_for,
    jtd_totals,
)
from .risk import BondRiskEngine, BondRiskRequest, BondScenario, ShockedCurveCache
from .serialization import decode
from .xccy import XccyCurveBootstrapRequest

__all__ = [
    "PORTFOLIO_RESULT_SCHEMA",
    "PORTFOLIO_RESULT_SCHEMA_V3",
    "SUPPORTED_CURRENCIES",
    "SUPPORTED_PRODUCTS",
    "CalendarUse",
    "CpiUse",
    "CurrencyAmounts",
    "CurveUse",
    "FactorTotal",
    "PortfolioCoverage",
    "PortfolioHorizonResult",
    "PortfolioRiskResult",
    "PortfolioRiskResultV3",
    "PortfolioScenario",
    "PortfolioScenarioResult",
    "PositionHorizon",
    "PositionScenarioPnl",
    "PositionValuation",
    "Rejection",
    "ValuationPolicy",
    "horizon_portfolio",
    "portfolio_result_from_json",
    "portfolio_result_to_json",
    "value_portfolio",
    "verify_portfolio_result",
    "verify_portfolio_result_v3",
]

PORTFOLIO_RESULT_SCHEMA = "rub-portfolio-risk-result.v2"
PORTFOLIO_RESULT_SCHEMA_V3 = "rub-portfolio-risk-result.v3"
"""Emitted only for a book with a derivative position; bond-only books stay v2."""
SUPPORTED_PRODUCTS = frozenset({ProductType.FIXED_BOND, ProductType.FLOATING_BOND})
# Bond currencies of value_portfolio (package 3): each non-RUB one needs a discount curve
# named in ValuationPolicy.foreign_curves and an FX rate in the snapshot.
SUPPORTED_CURRENCIES = frozenset({"RUB", "CNY", "USD", "EUR"})

CoverageStatus = Literal["complete", "partial", "failed"]
Interruption = Literal["cancelled", "time_budget_exceeded"]
CALCULATION_FAILED = "CALCULATION_FAILED"
_INTERRUPTION_CODES: dict[str, str] = {
    "cancelled": "CANCELLED",
    "time_budget_exceeded": "TIME_BUDGET_EXCEEDED",
}
_monotonic = time.monotonic


def _foreign_currency(value: object, label: str) -> str:
    if not isinstance(value, str) or len(value) != 3 or not value.isalpha() or not value.isupper():
        raise InvalidTerms(f"{label} must be an ISO 4217 code such as USD")
    if value == "RUB":
        raise InvalidTerms(f"{label} must be a currency other than RUB")
    return value


@dataclass(frozen=True)
class ValuationPolicy:
    """How the book is valued; part of the job identity.

    RUB bonds are discounted on ``discount_curve_id``. A non-RUB bond is discounted on
    the curve that ``foreign_curves`` names for its currency, as ``(currency, curve_id)``
    pairs. ``curve_overrides`` win for the instruments they name.

    A missing foreign curve is never replaced silently, and never by a curve of another
    currency (a bond's curve must be of its currency, whatever names it).
    ``foreign_curve_fallbacks`` (0.34.0, off by default) lists ``(currency, curve_id)``
    alternatives tried in the given order when the ``foreign_curves`` curve of that
    currency is **absent** from the snapshot (a curve present but unusable is still a
    rejection); the row then names the missing curve in
    ``PositionValuation.curve_fallback_from``. A typical use is a USD SOFR curve with the
    UST par-yield proxy (``usd_ust_par_proxy_v1``) behind it; ``CurveUse.proxy`` marks a
    proxy curve in the result. Without a usable curve the position is a
    ``MISSING_MARKET_DATA`` rejection naming every curve tried.

    ``cap_floor_guard_bp`` is the model policy for floaters with a floor or cap: core
    prices them at intrinsic value on the projected rate only when that rate is at least
    this many bp from every strike, and rejects them otherwise (``UNSUPPORTED_PRODUCT``).
    None, the default, rejects every projected coupon that has a floor or cap.

    ``exercise_assumptions`` (BACKLOG #50) values callable and perpetual bonds natively:
    ``(instrument_id, ExerciseAssumption)`` pairs, each turning the bond into the
    deterministic bond its resolved exercise implies (``callables.exercised_instrument``),
    held under every bump and scenario. The row records the resolved assumption in
    ``PositionValuation.exercise_assumption`` and a call as ``horizon="call"``. There is
    no default: a callable without an assumption is still rejected.

    Desk-monitor additions (0.33.0), each omitted at its default so earlier policies,
    job IDs and results keep their bytes:

    - ``spread_kinds`` gives instruments their own spread kind, as ``(instrument_id,
      "credit" | "ofz")`` pairs; the others take ``spread_kind``. One call then values
      a book of OFZ and corporates (core brief C8). Factor ``CS`` is still each
      position's own spread, so ``factor_totals["CS"]`` of a mixed book sums OFZ spread
      risk and credit spread risk: split it by the rows' ``spread_kind``.
    - ``g_spread_curve_id`` names a KBD curve (spec kind ``KBD_PARAMETRIC``) in the
      snapshot: every RUB fixed-coupon bond then reports its G-spread to it, whatever its
      discount curve (a bond discounted on RUONIA OIS has a G-spread too). None keeps the
      rule of earlier releases: a G-spread only for a bond discounted on the KBD.
    - ``discount_margin=True`` reports a floater's discount margin
      (``PositionValuation.discount_margin_bp``): the spread over its projection curve,
      in ``spread_convention``, that reprices its projected flows to the dirty price.
    - ``require_known_coupons=True`` rejects a fixed-coupon bond whose coupon periods do
      not run without a gap to its horizon (``MISSING_MARKET_DATA``), instead of valuing
      it without the missing coupons (core brief C7).
    - ``horizon="open_put"`` values to the first put whose exercise window is still open
      on the settlement date (``Offer.window_end``), confirmed or not (core brief C1).
    - ``spread_buckets=True`` reports each bond's own-spread risk by bucket
      (``PositionValuation.spread_buckets_rub``, :meth:`BondRiskEngine.spread_bucket_risk`):
      CS01 by tenor for credit, the OFZ basis by tenor for an OFZ discounted on RUONIA
      OIS (OWNER_REQUESTS R18: the OFZ curve is RUONIA plus a basis).
    """

    discount_curve_id: str
    spread_kind: Literal["credit", "ofz"] = "credit"
    spread_convention: ZSpreadConvention = field(default_factory=ZSpreadConvention)
    horizon: Literal["maturity", "confirmed_put", "open_put"] = "maturity"
    curve_overrides: tuple[tuple[str, str], ...] = ()
    include_second_order: bool = True
    bump_bp: float = 1.0
    foreign_curves: tuple[tuple[str, str], ...] = field(default=(), metadata=OMIT_IF_DEFAULT)
    cap_floor_guard_bp: float | None = field(default=None, metadata=OMIT_IF_DEFAULT)
    exercise_assumptions: tuple[tuple[str, ExerciseAssumption], ...] = field(
        default=(), metadata=OMIT_IF_DEFAULT
    )
    spread_kinds: tuple[tuple[str, Literal["credit", "ofz"]], ...] = field(
        default=(), metadata=OMIT_IF_DEFAULT
    )
    g_spread_curve_id: str | None = field(default=None, metadata=OMIT_IF_DEFAULT)
    discount_margin: bool = field(default=False, metadata=OMIT_IF_DEFAULT)
    require_known_coupons: bool = field(default=False, metadata=OMIT_IF_DEFAULT)
    spread_buckets: bool = field(default=False, metadata=OMIT_IF_DEFAULT)
    foreign_curve_fallbacks: tuple[tuple[str, str], ...] = field(
        default=(), metadata=OMIT_IF_DEFAULT
    )

    def __post_init__(self) -> None:
        pairs = tuple(sorted(self.exercise_assumptions, key=lambda p: p[0]))
        for instrument_id, assumption in pairs:
            if not isinstance(instrument_id, str) or not instrument_id:
                raise InvalidTerms("exercise assumption instrument IDs must be non-empty")
            if not isinstance(assumption, ExerciseAssumption):
                raise TypeError("exercise assumptions must be ExerciseAssumption values")
        if len({i for i, _ in pairs}) != len(pairs):
            raise InvalidTerms("duplicate exercise assumption")
        object.__setattr__(self, "exercise_assumptions", pairs)
        if self.cap_floor_guard_bp is not None:
            guard = finite_money(self.cap_floor_guard_bp, "cap/floor guard bp")
            if guard < 0.0:
                raise InvalidTerms("cap_floor_guard_bp cannot be negative")
            object.__setattr__(self, "cap_floor_guard_bp", guard)
        object.__setattr__(
            self, "foreign_curves", tuple(sorted(tuple(p) for p in self.foreign_curves))
        )
        for currency, curve_id in self.foreign_curves:
            _foreign_currency(currency, "foreign curve currency")
            if not isinstance(curve_id, str) or not curve_id:
                raise InvalidTerms("foreign curve IDs must be non-empty strings")
        if len({c for c, _ in self.foreign_curves}) != len(self.foreign_curves):
            raise InvalidTerms("duplicate foreign curve currency")
        # Sorted by currency only: the order within a currency is the order of trial.
        fallbacks = tuple(
            sorted((tuple(p) for p in self.foreign_curve_fallbacks), key=lambda p: p[0])
        )
        primary = dict(self.foreign_curves)
        for currency, curve_id in fallbacks:
            _foreign_currency(currency, "fallback curve currency")
            if not isinstance(curve_id, str) or not curve_id:
                raise InvalidTerms("fallback curve IDs must be non-empty strings")
            if currency not in primary:
                raise InvalidTerms(
                    f"a fallback curve for {currency} needs a foreign_curves entry first"
                )
            if curve_id == primary[currency]:
                raise InvalidTerms(f"the fallback curve of {currency} is its primary curve")
        if len(set(fallbacks)) != len(fallbacks):
            raise InvalidTerms("duplicate fallback curve")
        object.__setattr__(self, "foreign_curve_fallbacks", fallbacks)
        if not self.discount_curve_id:
            raise InvalidTerms("discount_curve_id is required")
        if self.spread_kind not in ("credit", "ofz"):
            raise InvalidTerms("spread_kind must be credit or ofz")
        kinds = tuple(sorted(tuple(p) for p in self.spread_kinds))
        for instrument_id, kind in kinds:
            if not isinstance(instrument_id, str) or not instrument_id:
                raise InvalidTerms("spread kind instrument IDs must be non-empty strings")
            if kind not in ("credit", "ofz"):
                raise InvalidTerms("spread kinds must be credit or ofz")
        if len({i for i, _ in kinds}) != len(kinds):
            raise InvalidTerms("duplicate spread kind instrument")
        object.__setattr__(self, "spread_kinds", kinds)
        if self.g_spread_curve_id is not None and (
            not isinstance(self.g_spread_curve_id, str) or not self.g_spread_curve_id
        ):
            raise InvalidTerms("g_spread_curve_id must be a non-empty string or None")
        for flag in ("discount_margin", "require_known_coupons", "spread_buckets"):
            if not isinstance(getattr(self, flag), bool):
                raise TypeError(f"{flag} must be boolean")
        if self.horizon not in ("maturity", "confirmed_put", "open_put"):
            raise InvalidTerms("horizon must be maturity, confirmed_put or open_put")
        object.__setattr__(self, "curve_overrides", tuple(sorted(self.curve_overrides)))
        if len({i for i, _ in self.curve_overrides}) != len(self.curve_overrides):
            raise InvalidTerms("duplicate curve override")
        if not isinstance(self.include_second_order, bool):
            raise TypeError("include_second_order must be boolean")
        positive_money(self.bump_bp, "bump_bp")

    def curve_for(self, instrument_id: str) -> str:
        return dict(self.curve_overrides).get(instrument_id, self.discount_curve_id)

    def spread_kind_for(self, instrument_id: str) -> Literal["credit", "ofz"]:
        """The spread kind of ``instrument_id``: its ``spread_kinds`` entry, else ``spread_kind``."""
        for key, kind in self.spread_kinds:
            if key == instrument_id:
                return kind
        return self.spread_kind

    def exercise_for(self, instrument_id: str) -> ExerciseAssumption | None:
        """The exercise assumption for ``instrument_id``, or None."""
        for key, assumption in self.exercise_assumptions:
            if key == instrument_id:
                return assumption
        return None

    def discount_curve_for(self, instrument: Instrument) -> str:
        """The discount curve ID of ``instrument``; raises when its currency has none."""
        override = dict(self.curve_overrides).get(instrument.instrument_id)
        if override is not None or instrument.currency == "RUB":
            return self.curve_for(instrument.instrument_id)
        curve_id = dict(self.foreign_curves).get(instrument.currency)
        if curve_id is None:
            raise MissingMarketData(
                f"the valuation policy names no discount curve for {instrument.currency}"
            )
        return curve_id

    def fallback_curves_for(self, currency: str) -> tuple[str, ...]:
        """The ``foreign_curve_fallbacks`` of ``currency``, in the order of trial."""
        return tuple(c for k, c in self.foreign_curve_fallbacks if k == currency)


@dataclass(frozen=True)
class PortfolioScenario:
    """Bucket shifts of each position's discount curve plus a spread shift (bp).

    ``fx_shocks_pct`` moves FX rates against RUB, as ``(currency, percent)`` pairs: a
    position in that currency is revalued at ``FX * (1 + percent / 100)``. RUB positions
    and currencies not listed are not moved.

    A floater that projects off a curve other than its discount curve moves that curve
    by ``projection_curve_shifts_bp`` (11 bucket shifts, bp) or, when None, by the
    shock's curve shifts too, so a rates scenario moves every curve it depends on.

    ``curve_shifts_bp`` (BACKLOG #43) gives named curves their own 11 bucket shifts, as
    ``(curve_id, shifts)`` pairs: every position that reads a listed curve, in any role
    (discount, projection, RUB or foreign leg, a real curve), moves it by that vector;
    unlisted curves move as above. A real curve moves only when it is listed. Derivative
    positions apply the same rule to each curve they read, ``shock.spread_shift_bp`` to
    CDS quoted spreads (``CS``) and ``fx_shocks_pct`` to their spot.

    ``spread_kind_shifts_bp`` (0.33.0) moves bond spreads by kind, as ``("credit" |
    "ofz", bp)`` pairs: a bond whose spread kind is listed takes that shift instead of
    ``shock.spread_shift_bp``. A "+20 bp OFZ basis" scenario is ``(("ofz", 20.0),)`` on a
    book whose OFZ are spread to RUONIA OIS; credit spreads then stay put.
    """

    name: str
    shock: BondScenario
    fx_shocks_pct: tuple[tuple[str, float], ...] = field(default=(), metadata=OMIT_IF_DEFAULT)
    projection_curve_shifts_bp: tuple[float, ...] | None = field(
        default=None, metadata=OMIT_IF_DEFAULT
    )
    curve_shifts_bp: tuple[tuple[str, tuple[float, ...]], ...] = field(
        default=(), metadata=OMIT_IF_DEFAULT
    )
    spread_kind_shifts_bp: tuple[tuple[str, float], ...] = field(
        default=(), metadata=OMIT_IF_DEFAULT
    )

    def __post_init__(self) -> None:
        if not self.name:
            raise InvalidTerms("scenario name is required")
        kinds = tuple(sorted(tuple(p) for p in self.spread_kind_shifts_bp))
        for kind, move in kinds:
            if kind not in ("credit", "ofz"):
                raise InvalidTerms("spread kind shifts are for credit or ofz")
            finite_money(cast(float, move), "spread kind shift bp")
        if len({k for k, _ in kinds}) != len(kinds):
            raise InvalidTerms("duplicate spread kind shift")
        object.__setattr__(self, "spread_kind_shifts_bp", kinds)
        if self.projection_curve_shifts_bp is not None:
            object.__setattr__(
                self, "projection_curve_shifts_bp", _shifts(tuple(self.projection_curve_shifts_bp))
            )
        named = []
        for pair in self.curve_shifts_bp:
            curve_id, shifts = pair
            if not isinstance(curve_id, str) or not curve_id or "|" in curve_id:
                raise InvalidTerms("scenario curve IDs must be non-empty strings without '|'")
            named.append((curve_id, _shifts(tuple(shifts))))
        object.__setattr__(self, "curve_shifts_bp", tuple(sorted(named)))
        if len({c for c, _ in self.curve_shifts_bp}) != len(self.curve_shifts_bp):
            raise InvalidTerms("duplicate scenario curve ID")
        object.__setattr__(
            self, "fx_shocks_pct", tuple(sorted(tuple(p) for p in self.fx_shocks_pct))
        )
        for currency, move in self.fx_shocks_pct:
            _foreign_currency(currency, "FX shock currency")
            if finite_money(move, "FX shock percent") <= -100.0:
                raise InvalidTerms("an FX shock must leave the rate positive")
        if len({c for c, _ in self.fx_shocks_pct}) != len(self.fx_shocks_pct):
            raise InvalidTerms("duplicate FX shock currency")

    def fx_move_pct(self, currency: str) -> float:
        return dict(self.fx_shocks_pct).get(currency, 0.0)

    def named_curve_shift(self, curve_id: str) -> tuple[float, ...] | None:
        """The shifts ``curve_shifts_bp`` gives ``curve_id``, or None when it is not listed."""
        return dict(self.curve_shifts_bp).get(curve_id)

    def curve_shift(self, curve_id: str, *, projection: bool = False) -> tuple[float, ...]:
        """The 11 bucket shifts (bp) that move ``curve_id`` in this scenario.

        A listed curve takes its own vector; otherwise a projection curve takes
        ``projection_curve_shifts_bp`` when given, and every other curve the shock's.
        """
        named = self.named_curve_shift(curve_id)
        if named is not None:
            return named
        if projection and self.projection_curve_shifts_bp is not None:
            return self.projection_curve_shifts_bp
        return self.shock.curve_shifts_bp

    @property
    def spread_shift_bp(self) -> float:
        return self.shock.spread_shift_bp

    def bond_shock(self, spread_kind: str) -> BondScenario:
        """The shock of a bond whose spread is of ``spread_kind`` (``spread_kind_shifts_bp``)."""
        for kind, move in self.spread_kind_shifts_bp:
            if kind == spread_kind:
                return replace(self.shock, spread_shift_bp=move)
        return self.shock


@dataclass(frozen=True)
class Rejection:
    position_id: str
    instrument_id: str
    code: str
    message: str


@dataclass(frozen=True)
class PortfolioCoverage:
    status: CoverageStatus
    expected: int
    completed: int
    rejections: tuple[Rejection, ...]


@dataclass(frozen=True)
class CurrencyAmounts:
    """A non-RUB position in its own currency (per bond, except the signed ``pv``).

    The position's ``_rub`` fields are these amounts at ``rub_per_unit``, the snapshot
    FX rate (RUB per one unit of ``currency``).
    """

    currency: str
    rub_per_unit: float
    current_nominal: float
    accrued_interest: float
    dirty_price: float
    pv: float


@dataclass(frozen=True)
class PositionValuation:
    """One computed position. Prices are per bond; PV, risk and JTD are signed RUB.

    For a non-RUB bond every ``_rub`` amount is converted at the snapshot FX rate,
    ``currency_amounts`` holds them in the bond currency, and the factor risk adds
    ``FX|<ccy>``. ``effective_yield`` and ``macaulay_duration_years`` are on the issue's
    yield time basis, named by ``yield_convention``.

    A bond valued under a ``ValuationPolicy.exercise_assumptions`` entry records the
    resolved assumption (``ResolvedExercise.identifier``, e.g.
    ``exercise:to_call:2027-03-25@call:2027-03-25``) in ``exercise_assumption``; an
    exercised call reads ``horizon="call"`` with the call date as ``horizon_date``.

    ``discount_margin_bp`` (``ValuationPolicy.discount_margin``, floaters only) is the
    spread over the projection curve that reprices the projected flows; it equals
    ``spread_bp`` when the floater projects off its discount curve. ``horizon="open_put"``
    marks a put chosen by the open-window rule (``ValuationPolicy.horizon``).
    ``spread_buckets_rub`` (``ValuationPolicy.spread_buckets``) is the central ``CS``
    sensitivity split by :data:`RISK_BUCKETS` (see ``BondRiskEngine.spread_bucket_risk``).
    ``curve_fallback_from`` (0.34.0) names the curve the policy wanted for a non-RUB bond
    when it was absent from the snapshot and ``curve_id`` is a fallback of the same
    currency (``ValuationPolicy.foreign_curve_fallbacks``); None otherwise.
    """

    position_id: str
    instrument_id: str
    book: str
    accounting_category: AccountingCategory
    calculation_id: str
    curve_id: str
    spec_id: str
    curve_basis: str
    settlement_date: date
    horizon: Literal["maturity", "confirmed_put", "open_put", "call"]
    horizon_date: date
    quantity: float
    current_nominal_rub: float
    clean_price_pct: float
    accrued_interest_rub: float
    dirty_price_rub: float
    pv_rub: float
    spread_kind: Literal["credit", "ofz"]
    spread_convention: str
    spread_bp: float
    effective_yield: float
    macaulay_duration_years: float
    g_spread_bp: float | None
    factor_risk: FactorRisk
    jtd_pnl_rub: float | None
    yield_convention: str = field(
        default=DEFAULT_YIELD_CONVENTION.identifier, metadata=OMIT_IF_DEFAULT
    )
    currency_amounts: CurrencyAmounts | None = field(default=None, metadata=OMIT_IF_DEFAULT)
    exercise_assumption: str | None = field(default=None, metadata=OMIT_IF_DEFAULT)
    discount_margin_bp: float | None = field(default=None, metadata=OMIT_IF_DEFAULT)
    spread_buckets_rub: tuple[float, ...] = field(default=(), metadata=OMIT_IF_DEFAULT)
    curve_fallback_from: str | None = field(default=None, metadata=OMIT_IF_DEFAULT)


@dataclass(frozen=True)
class PositionScenarioPnl:
    position_id: str
    pnl_rub: float | None
    code: str | None


@dataclass(frozen=True)
class PortfolioScenarioResult:
    name: str
    scenario_id: str
    coverage: PortfolioCoverage
    positions: tuple[PositionScenarioPnl, ...]
    covered_pnl_rub: float
    total_pnl_rub: float | None


@dataclass(frozen=True)
class CurveUse:
    """A curve the job read. ``proxy`` (0.34.0) marks a stand-in curve of its currency
    (``CurveSpec.proxy``: a government par-yield curve, not OIS); omitted when False."""

    curve_id: str
    spec_id: str
    model_version: str
    proxy: bool = field(default=False, metadata=OMIT_IF_DEFAULT)


@dataclass(frozen=True)
class CalendarUse:
    calendar_id: str
    version: str
    fingerprint: str
    valid_from: date | None
    valid_to: date | None


@dataclass(frozen=True)
class CpiUse:
    """A CPI series that indexed an inflation-linked position (package 9a)."""

    series_id: str
    source_id: str
    as_of: date
    first_month: date
    last_month: date


@dataclass(frozen=True)
class FactorTotal:
    factor: str
    first_order_rub: float


@dataclass(frozen=True)
class PortfolioRiskResult:
    run_id: str
    job_id: str
    snapshot_id: str
    business_date: date
    as_of: datetime
    library_version: str
    status: CoverageStatus
    coverage: PortfolioCoverage
    positions: tuple[PositionValuation, ...]
    scenarios: tuple[PortfolioScenarioResult, ...]
    curves: tuple[CurveUse, ...]
    calendars: tuple[CalendarUse, ...]
    covered_pv_rub: float
    total_pv_rub: float | None
    factor_totals: tuple[FactorTotal, ...]
    interruption: Interruption | None = None
    schema_version: str = PORTFOLIO_RESULT_SCHEMA
    result_sha256: str = ""
    fx_rates: tuple[FxRate, ...] = field(default=(), metadata=OMIT_IF_DEFAULT)
    cpi: tuple[CpiUse, ...] = field(default=(), metadata=OMIT_IF_DEFAULT)


@dataclass(frozen=True)
class PortfolioRiskResultV3(PortfolioRiskResult):
    """Contract ``rub-portfolio-risk-result.v3``: v2 plus derivative positions.

    ``value_portfolio`` emits it only when a position is on a derivative (a type of
    :mod:`rub_fixed_income.portfolio_instruments`); a bond-only book stays v2, byte for
    byte. ``positions`` keeps the bond rows; each derivative product has its own row
    tuple (``swaps``, ``fx_forwards``, ``xccy_swaps``, ``cds``, ``fx_futures``,
    ``rate_futures``, ``ofz_futures``, ``repos``), omitted when empty. ``coverage``, scenarios, ``covered_pv_rub`` / ``total_pv_rub`` and
    ``factor_totals`` cover every position. The v3-only blocks:

    - ``position_models``: engine model version and flags of every computed position
      (BACKLOG #83), in position order;
    - ``hessian_totals``: the book's sparse Hessian keyed by factor names, the sum of
      every position's Hessian (empty without ``include_second_order``);
    - ``jtd_totals`` and ``jtd_total_rub``: jump-to-default by entity (bond
      ``issuer_id``, CDS reference entity) and over the book, from the positions that
      have one; bonds and CDS share the sign rule (DEC07);
    - ``fx_spots`` and ``credit_curves``: the FX spots and credit curves used.
    """

    schema_version: str = PORTFOLIO_RESULT_SCHEMA_V3
    swaps: tuple[SwapPositionValuation, ...] = field(default=(), metadata=OMIT_IF_DEFAULT)
    fx_forwards: tuple[FxForwardPositionValuation, ...] = field(
        default=(), metadata=OMIT_IF_DEFAULT
    )
    xccy_swaps: tuple[XccyPositionValuation, ...] = field(default=(), metadata=OMIT_IF_DEFAULT)
    cds: tuple[CdsPositionValuation, ...] = field(default=(), metadata=OMIT_IF_DEFAULT)
    fx_futures: tuple[FxFuturesPositionValuation, ...] = field(default=(), metadata=OMIT_IF_DEFAULT)
    rate_futures: tuple[RateFuturesPositionValuation, ...] = field(
        default=(), metadata=OMIT_IF_DEFAULT
    )
    ofz_futures: tuple[OfzFuturesPositionValuation, ...] = field(
        default=(), metadata=OMIT_IF_DEFAULT
    )
    repos: tuple[RepoPositionValuation, ...] = field(default=(), metadata=OMIT_IF_DEFAULT)
    position_models: tuple[PositionModel, ...] = ()
    hessian_totals: tuple[HessianTotal, ...] = ()
    jtd_totals: tuple[JtdTotal, ...] = ()
    jtd_total_rub: float = 0.0
    fx_spots: tuple[FxSpot, ...] = field(default=(), metadata=OMIT_IF_DEFAULT)
    credit_curves: tuple[CreditCurveUse, ...] = field(default=(), metadata=OMIT_IF_DEFAULT)

    def derivative_rows(self) -> tuple[ProductRow, ...]:
        """Every derivative row, product by product (the order of the fields)."""
        return (
            *self.swaps,
            *self.fx_forwards,
            *self.xccy_swaps,
            *self.cds,
            *self.fx_futures,
            *self.rate_futures,
            *self.ofz_futures,
            *self.repos,
        )


@dataclass(frozen=True)
class _BuiltCurve:
    entry: CurveEntry
    curve: DiscountCurve | CurveBootstrapRequest
    kbd: ZcycParameters | None
    model_version: str
    currency: str
    # A real (CPI-adjusted) curve: discounts only inflation-linked bonds (package 9a).
    real: bool = False


def _build_curve(entry: CurveEntry, business_date: date) -> _BuiltCurve:
    spec = get_curve_spec(entry.spec_id)
    source = entry.curve
    if spec.kind is CurveKind.KBD_PARAMETRIC:
        if isinstance(source, ZcycParameters):
            if source.trade_date != business_date:
                raise InvalidTerms("KBD parameters are not for the snapshot business date")
            return _BuiltCurve(
                entry, kbd_discount_curve(source), source, KBD_MODEL_VERSION, spec.currency
            )
        if isinstance(source, DiscountCurve) and source.model_version == KBD_MODEL_VERSION:
            return _BuiltCurve(entry, source, None, KBD_MODEL_VERSION, spec.currency)
        raise InvalidTerms(f"curve {entry.curve_id} does not match spec {spec.spec_id}")
    if spec.kind is CurveKind.OIS_BOOTSTRAP:
        if isinstance(source, (CurveBootstrapRequest, DiscountCurve)):
            _check_ois_spec_convention(entry.curve_id, spec.spec_id, source.convention)
        if isinstance(source, CurveBootstrapRequest):
            if source.convention.fixed_leg_schedule is not None:
                raise InvalidTerms(
                    f"curve {entry.curve_id}: {spec.spec_id} uses the legacy fixed-leg schedule"
                )
            return _BuiltCurve(entry, source, None, spec.model_version, spec.currency)
        if isinstance(source, DiscountCurve) and source.model_version == spec.model_version:
            return _BuiltCurve(entry, source, None, spec.model_version, spec.currency)
        raise InvalidTerms(f"curve {entry.curve_id} does not match spec {spec.spec_id}")
    if spec.kind in (CurveKind.DISCOUNT_FACTORS, CurveKind.REAL_DISCOUNT_FACTORS):
        if (
            isinstance(source, DiscountCurve)
            and source.model_version == spec.model_version
            and source.convention.time_day_count is spec.time_day_count
        ):
            if source.curve_date != business_date:
                raise InvalidTerms(
                    f"curve {entry.curve_id} is not dated the snapshot business date"
                )
            return _BuiltCurve(
                entry,
                source,
                None,
                spec.model_version,
                spec.currency,
                spec.kind is CurveKind.REAL_DISCOUNT_FACTORS,
            )
        raise InvalidTerms(f"curve {entry.curve_id} does not match spec {spec.spec_id}")
    if spec.kind in (CurveKind.XCCY_BOOTSTRAP, CurveKind.PAR_YIELD_PROXY):
        # A built XCCY curve (CNY under RUB collateral) or a par-yield proxy (0.34.0):
        # read, interpolated and shifted on its continuous zero rates as caller factors.
        if isinstance(source, XccyCurveBootstrapRequest):
            raise InvalidTerms(
                f"curve {entry.curve_id} holds XCCY quotes; a bond needs the built curve "
                "(bootstrap_xccy_curve) under the spec"
            )
        if (
            isinstance(source, DiscountCurve)
            and source.model_version == spec.model_version
            and source.convention.time_day_count is spec.time_day_count
        ):
            if source.curve_date != business_date:
                raise InvalidTerms(
                    f"curve {entry.curve_id} is not dated the snapshot business date"
                )
            return _BuiltCurve(entry, source, None, spec.model_version, spec.currency)
        raise InvalidTerms(f"curve {entry.curve_id} does not match spec {spec.spec_id}")
    raise InvalidTerms(f"spec {spec.spec_id} is not a bond discount curve")


def _coverage(expected: Sequence[Position], rejections: list[Rejection]) -> PortfolioCoverage:
    failed = {r.position_id for r in rejections}
    completed = sum(1 for p in expected if p.position_id not in failed)
    status: CoverageStatus = "complete" if not failed else "partial" if completed else "failed"
    return PortfolioCoverage(status, len(expected), completed, tuple(rejections))


def value_portfolio(
    *,
    run_id: str,
    as_of: datetime,
    snapshot: MarketSnapshot,
    instruments: Sequence[PortfolioInstrument],
    positions: Sequence[Position],
    policy: ValuationPolicy,
    scenarios: Sequence[PortfolioScenario] = (),
    time_budget_seconds: float | None = None,
    should_cancel: Callable[[], bool] | None = None,
    curve_cache: ShockedCurveCache | None = None,
) -> PortfolioRiskResult:
    """Value a book and return its risk in desk factors, with explicit coverage.

    A malformed job (duplicate IDs, naive timestamps) raises. Per-position problems
    become rejections with a stable code (``CoreRejection.code``, or
    ``CALCULATION_FAILED`` for a numerical failure). Totals are ``None`` unless
    coverage is complete; covered subtotals are always reported.

    ``curve_cache`` lets the caller keep bootstrapped base and shocked OIS curves
    across calls (seed it from persisted :class:`PrebuiltCurve` entries, read them back
    with :meth:`ShockedCurveCache.export`). Results are identical with or without it.
    """
    if not run_id:
        raise InvalidTerms("run_id is required")
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise InvalidTerms("as_of must carry a UTC offset")
    if time_budget_seconds is not None and finite_money(time_budget_seconds, "time budget") < 0:
        raise ValueError("time_budget_seconds must be nonnegative")
    if curve_cache is not None and not isinstance(curve_cache, ShockedCurveCache):
        raise TypeError("curve_cache must be a ShockedCurveCache")
    for names, label in (
        ([i.instrument_id for i in instruments], "instrument"),
        ([p.position_id for p in positions], "position"),
        ([s.name for s in scenarios], "scenario"),
    ):
        if len(set(names)) != len(names):
            raise ValueError(f"duplicate {label} identifiers")
    started = _monotonic()
    interruption: Interruption | None = None

    def stopped() -> Interruption | None:
        nonlocal interruption
        if interruption is None:
            if should_cancel is not None and should_cancel():
                interruption = "cancelled"
            elif time_budget_seconds is not None and _monotonic() - started >= time_budget_seconds:
                interruption = "time_budget_exceeded"
        return interruption

    by_id = {i.instrument_id: i for i in instruments}
    bonds_by_id = {k: i for k, i in by_id.items() if isinstance(i, Instrument)}
    derivative_book = any(
        not isinstance(by_id.get(p.instrument_id), (Instrument, type(None))) for p in positions
    )
    job_id = canonical_sha256(
        {
            "model": "portfolio-risk-v3" if derivative_book else "portfolio-risk-v2",
            "library_version": __version__,
            "run_id": run_id,
            "as_of": as_of,
            "snapshot": canonical_sha256(snapshot),
            "instruments": sorted(canonical_sha256(i) for i in instruments),
            "positions": [asdict(p) for p in positions],
            "policy": policy,
            "scenarios": list(scenarios),
        }
    )
    built: dict[str, _BuiltCurve | CoreRejection] = {}
    cache = ShockedCurveCache() if curve_cache is None else curve_cache
    valuations: list[PositionValuation] = []
    rejections: list[Rejection] = []
    prepared_by_position: dict[str, BondRiskEngine] = {}
    projections: dict[str, _BuiltCurve | CoreRejection] = {}
    used_calendars: dict[str, CalendarUse] = {}
    used_fx: dict[str, FxRate] = {}
    fx_currency: dict[str, str] = {}
    used_cpi: dict[str, CpiUse] = {}
    real_curve_positions: set[str] = set()
    bond_curves: dict[str, tuple[str, str | None]] = {}
    outcomes: dict[str, ProductOutcome] = {}
    models: list[PositionModel] = []
    exercised_memo: dict[str, ExercisedBond | CoreRejection] = {}

    context = _product_context(
        snapshot, policy, job_id, cache, built, projections, used_calendars, used_fx, bonds_by_id
    )
    curve, projection = context.bond_curve, context.projection

    for position in positions:
        reason = stopped()
        if reason is not None:
            rejections.append(
                Rejection(
                    position.position_id,
                    position.instrument_id,
                    _INTERRUPTION_CODES[reason],
                    "job stopped before this position",
                )
            )
            continue
        instrument = by_id.get(position.instrument_id)
        try:
            if instrument is not None and not isinstance(instrument, Instrument):
                outcomes[position.position_id] = adapter_for(instrument)(
                    position, instrument, context
                )
                models.append(outcomes[position.position_id].model)
                continue
            exercised = (
                None
                if not policy.exercise_assumptions or not isinstance(instrument, Instrument)
                else _exercised(instrument, policy, snapshot, exercised_memo)
            )
            lookup: Mapping[str, Instrument] = bonds_by_id
            if exercised is not None:
                bond = exercised.instrument
                lookup = _Replaced(bonds_by_id, bond)
            valuation, prepared = _value_position(
                position,
                lookup,
                snapshot,
                policy,
                curve,
                cache,
                job_id,
                run_id,
                used_calendars,
                used_fx,
                projection,
                used_cpi,
            )
        except CoreRejection as exc:
            rejections.append(
                Rejection(position.position_id, position.instrument_id, exc.code, exc.message)
            )
            continue
        except (ValueError, LookupError, ArithmeticError) as exc:
            rejections.append(
                Rejection(
                    position.position_id, position.instrument_id, CALCULATION_FAILED, str(exc)
                )
            )
            continue
        if exercised is not None:
            valuation = replace(
                valuation,
                horizon="call"
                if valuation.horizon == "maturity" and exercised.resolved.exercise == "call"
                else valuation.horizon,
                exercise_assumption=exercised.resolved.identifier,
            )
        valuations.append(valuation)
        prepared_by_position[position.position_id] = prepared
        if valuation.currency_amounts is not None:
            fx_currency[position.position_id] = valuation.currency_amounts.currency
        if get_curve_spec(valuation.spec_id).kind is CurveKind.REAL_DISCOUNT_FACTORS:
            real_curve_positions.add(position.position_id)
        assert isinstance(instrument, Instrument)
        projection_id = (
            cast(FloatingCouponLeg, instrument.coupon_leg).projection_curve_id
            if prepared.two_curve
            else None
        )
        bond_curves[position.position_id] = (valuation.curve_id, projection_id)
        if derivative_book:
            models.append(_bond_model(valuation, instrument, prepared))

    coverage = _coverage(positions, rejections)
    scenario_results = [
        _run_scenario(
            scenario,
            positions,
            prepared_by_position,
            fx_currency,
            rejections,
            job_id,
            stopped,
            frozenset(real_curve_positions),
            bond_curves,
            outcomes,
        )
        for scenario in scenarios
    ]
    rows = [o.row for o in outcomes.values()]
    covered_pv = finite_money(
        math.fsum((*(v.pv_rub for v in valuations), *(r.pv_rub for r in rows))), "covered PV"
    )
    totals: dict[str, list[float]] = {}
    for risk in (*(v.factor_risk for v in valuations), *(r.factor_risk for r in rows)):
        for factor, value in risk.first_order_by_factor().items():
            totals.setdefault(factor, []).append(value)
    factor_totals = tuple(FactorTotal(k, math.fsum(v)) for k, v in sorted(totals.items()))
    status: CoverageStatus = (
        "complete"
        if coverage.status == "complete"
        and all(s.coverage.status == "complete" for s in scenario_results)
        else "partial"
        if coverage.completed
        else "failed"
    )
    used_curves = {
        k: (c.entry.spec_id, c.model_version)
        for k, c in projections.items()
        if "|" not in k and isinstance(c, _BuiltCurve)
    }
    used_curves.update(
        (k, (c.entry.spec_id, c.model_version))
        for k, c in built.items()
        if isinstance(c, _BuiltCurve)
    )
    for key, use in context.used_curves.items():
        used_curves.setdefault(key, use)
    curves_used = tuple(
        CurveUse(curve_id, spec_id, model_version, get_curve_spec(spec_id).proxy)
        for curve_id, (spec_id, model_version) in sorted(used_curves.items())
    )
    fields_v2 = (
        run_id,
        job_id,
        snapshot.snapshot_id,
        snapshot.business_date,
        as_of,
        __version__,
        status,
        coverage,
        tuple(valuations),
        tuple(scenario_results),
        curves_used,
        tuple(used_calendars[k] for k in sorted(used_calendars)),
        covered_pv,
        covered_pv if coverage.status == "complete" else None,
        factor_totals,
        interruption,
    )
    fx_rates = tuple(used_fx[k] for k in sorted(used_fx))
    cpi = tuple(used_cpi[k] for k in sorted(used_cpi))
    if not derivative_book:
        result = PortfolioRiskResult(*fields_v2, fx_rates=fx_rates, cpi=cpi)
        return replace(
            result, result_sha256=canonical_sha256(portfolio_result_hash_projection(result))
        )
    by_field: dict[str, list[ProductRow]] = {name: [] for name in ROW_FIELDS.values()}
    for row in rows:
        by_field[ROW_FIELDS[type(row)]].append(row)
    contributions: list[tuple[str | None, float]] = []
    bond_rows = {v.position_id: v for v in valuations}
    for position in positions:
        bond_row = bond_rows.get(position.position_id)
        if bond_row is not None and bond_row.jtd_pnl_rub is not None:
            issuer = bonds_by_id[bond_row.instrument_id].issuer_id
            contributions.append((issuer, bond_row.jtd_pnl_rub))
        derivative = outcomes.get(position.position_id)
        if derivative is not None and derivative.jtd is not None:
            contributions.append(derivative.jtd)
    jtd = jtd_totals(contributions)
    result_v3 = PortfolioRiskResultV3(
        *fields_v2,
        fx_rates=fx_rates,
        cpi=cpi,
        **{name: tuple(items) for name, items in by_field.items()},  # type: ignore[arg-type]
        position_models=tuple(models),
        hessian_totals=hessian_totals(
            [*(v.factor_risk for v in valuations), *(r.factor_risk for r in rows)]
        ),
        jtd_totals=jtd,
        jtd_total_rub=finite_money(math.fsum(t.jtd_pnl_rub for t in jtd), "JTD total"),
        fx_spots=tuple(context.used_fx_spots[k] for k in sorted(context.used_fx_spots)),
        credit_curves=tuple(context.used_credit[k] for k in sorted(context.used_credit)),
    )
    return replace(
        result_v3, result_sha256=canonical_sha256(portfolio_result_hash_projection(result_v3))
    )


def _product_context(
    snapshot: MarketSnapshot,
    policy: ValuationPolicy,
    job_id: str,
    cache: ShockedCurveCache,
    built: dict[str, _BuiltCurve | CoreRejection],
    projections: dict[str, _BuiltCurve | CoreRejection],
    used_calendars: dict[str, CalendarUse],
    used_fx: dict[str, FxRate],
    bonds: Mapping[str, Instrument],
) -> ProductContext:
    """The derivative adapters' context; curves and calendars resolve into the job's memos."""

    def curve(curve_id: str) -> _BuiltCurve:
        if curve_id not in built:
            try:
                built[curve_id] = _build_curve(snapshot.curve(curve_id), snapshot.business_date)
            except CoreRejection as exc:
                built[curve_id] = exc
        outcome = built[curve_id]
        if isinstance(outcome, CoreRejection):
            raise outcome
        return outcome

    def projection(curve_id: str, index: FixingIndex) -> _BuiltCurve:
        return _projection_curve(snapshot, projections, curve_id, index)

    def calendar(calendar_id: str) -> BusinessCalendar:
        return _record_calendar(snapshot, calendar_id, used_calendars)

    return ProductContext(
        snapshot,
        job_id,
        policy.bump_bp,
        policy.include_second_order,
        cache,
        curve,
        projection,
        calendar,
        used_fx,
        bonds,
    )


class _Replaced(Mapping[str, Instrument]):
    """``base`` with one instrument replaced by another of the same ID (no copy)."""

    def __init__(self, base: Mapping[str, Instrument], instrument: Instrument) -> None:
        self._base = base
        self._instrument = instrument

    def __getitem__(self, key: str) -> Instrument:
        return self._instrument if key == self._instrument.instrument_id else self._base[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._base)

    def __len__(self) -> int:
        return len(self._base)


def _exercised(
    instrument: Instrument,
    policy: ValuationPolicy,
    snapshot: MarketSnapshot,
    memo: dict[str, ExercisedBond | CoreRejection],
) -> ExercisedBond | None:
    """The bond the policy's exercise assumption implies for ``instrument``, or None.

    Settlement is the instrument's lag in its calendar, as for every bond; ``to_worst``
    is resolved at the snapshot quote. Resolved once per instrument and job.
    """
    assumption = policy.exercise_for(instrument.instrument_id)
    if assumption is None:
        return None
    key = instrument.instrument_id
    if key not in memo:
        try:
            calendar = snapshot.calendar(instrument.calendar_id)
            settlement = calendar.add_business_days(
                snapshot.business_date, instrument.settlement_lag_days
            )
            dirty = None
            if assumption.kind == "to_worst":
                quote = snapshot.quote(key)
                dirty = clean_price_pct_to_dirty_rub(
                    quote.clean_price_pct,
                    instrument.outstanding_face(settlement),
                    _accrued(instrument, quote, settlement, snapshot),
                )
            resolved = resolve_exercise(instrument, settlement, assumption, dirty_price_rub=dirty)
            memo[key] = exercised_instrument(instrument, resolved)
        except CoreRejection as exc:
            memo[key] = exc
    outcome = memo[key]
    if isinstance(outcome, CoreRejection):
        raise outcome
    return outcome


def _supported_instrument(
    position: Position,
    instruments: Mapping[str, Instrument],
    currencies: frozenset[str] = frozenset({"RUB"}),
) -> Instrument:
    instrument = instruments.get(position.instrument_id)
    if instrument is None:
        raise InvalidTerms("instrument terms were not supplied for this position")
    if instrument.product not in SUPPORTED_PRODUCTS:
        raise UnsupportedProduct(f"{instrument.product.value} is not supported by this release")
    if instrument.currency not in currencies:
        supported = ", ".join(sorted(currencies))
        raise UnsupportedProduct(
            f"{instrument.currency} bonds are not supported here (supported: {supported})"
        )
    if position.currency != instrument.currency:
        raise InvalidTerms("position currency differs from the instrument currency")
    if instrument.is_perpetual:
        raise UnsupportedProduct("perpetual bonds need a call assumption (package 9)")
    if any(o.kind.value == "call" for o in instrument.offers):
        raise UnsupportedProduct("callable bonds are not supported (package 9)")
    leg = instrument.coupon_leg
    if isinstance(leg, FloatingCouponLeg):
        if instrument.currency != "RUB":
            raise UnsupportedProduct("floating coupons are supported on RUB bonds only")
        if leg.projection_curve_id is None:
            raise UnsupportedProduct(
                "the floating leg names no projection curve (FloatingCouponLeg.projection_curve_id)"
            )
    return instrument


def _projection_curve(
    snapshot: MarketSnapshot,
    memo: dict[str, _BuiltCurve | CoreRejection],
    curve_id: str,
    index: FixingIndex,
) -> _BuiltCurve:
    """The snapshot curve ``curve_id`` checked as a projection curve, once per call."""
    key = f"{curve_id}|{index}"
    if key not in memo:
        try:
            source = snapshot.projection_curve(curve_id, index)
            entry = snapshot.curve(curve_id)
            spec = get_curve_spec(entry.spec_id)
            memo[key] = _BuiltCurve(entry, source, None, spec.model_version, spec.currency)
        except CoreRejection as exc:
            memo[key] = exc
    outcome = memo[key]
    if isinstance(outcome, CoreRejection):
        raise outcome
    memo.setdefault(curve_id, outcome)
    return outcome


def _accrued(
    instrument: Instrument,
    quote: BondPriceObservation,
    settlement: date,
    snapshot: MarketSnapshot | None = None,
) -> float:
    """The quote's accrued interest, else the instrument's own at ``settlement``.

    A floater's own accrued (0.33.0, BACKLOG #81) comes from the snapshot's fixings by
    :meth:`FloatingLeg.accrued_interest`; before 0.33.0 it was a rejection.
    """
    if quote.accrued_per_bond is not None:
        return quote.accrued_per_bond
    if isinstance(instrument.coupon_leg, FloatingCouponLeg):
        if snapshot is None:
            raise MissingMarketData("a floater's accrued interest must come with its quote")
        return instrument.accrued(settlement, snapshot)
    return instrument.accrued(settlement)


def _use_calendar(
    snapshot: MarketSnapshot, instrument: Instrument, used: dict[str, CalendarUse]
) -> BusinessCalendar:
    return _record_calendar(snapshot, instrument.calendar_id, used)


def _record_calendar(
    snapshot: MarketSnapshot, calendar_id: str, used: dict[str, CalendarUse]
) -> BusinessCalendar:
    calendar = snapshot.calendar(calendar_id)
    used[calendar_id] = CalendarUse(
        calendar_id,
        calendar.version,
        calendar_fingerprint(calendar),
        calendar.valid_from,
        calendar.valid_to,
    )
    return calendar


def _bond_model(
    valuation: PositionValuation, instrument: Instrument, engine: BondRiskEngine
) -> PositionModel:
    """The v3 model record of a bond position (BACKLOG #83)."""
    flags = []
    leg = instrument.coupon_leg
    if engine.two_curve:
        flags.append("two_curve")
    if isinstance(leg, FloatingCouponLeg):
        flags.append("floating")
        if leg.rule.bounds is not None:
            flags.append("has_optionality")
    if valuation.currency_amounts is not None:
        flags.append("fx_translated")
    if instrument.product is ProductType.INFLATION_LINKED_BOND:
        flags.append("real_curve")
    if valuation.horizon == "confirmed_put":
        flags.append("to_confirmed_put")
    return PositionModel(
        valuation.position_id,
        valuation.instrument_id,
        instrument.product.value,
        engine.model_version,
        tuple(sorted(flags)),
    )


def _value_position(
    position: Position,
    instruments: Mapping[str, Instrument],
    snapshot: MarketSnapshot,
    policy: ValuationPolicy,
    curve: Callable[[str], _BuiltCurve],
    cache: ShockedCurveCache,
    job_id: str,
    run_id: str,
    used_calendars: dict[str, CalendarUse],
    used_fx: dict[str, FxRate],
    projection: Callable[[str, FixingIndex], _BuiltCurve],
    used_cpi: dict[str, CpiUse] | None = None,
) -> tuple[PositionValuation, BondRiskEngine]:
    """Value one bond position.

    A non-RUB bond (package 3) is priced in its currency on its own currency's curve.
    Its flows, nominal and accrued enter the RUB risk adapter converted at the snapshot
    FX rate, so PV, DV01, CS01, the Hessian and JTD come out in RUB; the FX delta and
    the FX cross terms are added from them (``fx_translated_factor_risk``).

    A floater (package 1) projects its coupons off its leg's projection curve with the
    snapshot's point-in-time fixings; a projection curve other than the discount curve
    adds its own ``IR|<projection curve>|<bucket>`` factors.

    An inflation-linked bond (package 9a) goes to :func:`_value_linker_position`.
    """
    linker = instruments.get(position.instrument_id)
    if linker is not None and linker.product is ProductType.INFLATION_LINKED_BOND:
        valuation, prepared = _value_linker_position(
            position,
            linker,
            snapshot,
            policy,
            curve,
            cache,
            job_id,
            run_id,
            used_calendars,
            {} if used_cpi is None else used_cpi,
        )
        return valuation, prepared.engine
    instrument = _supported_instrument(position, instruments, SUPPORTED_CURRENCIES)
    convention = instrument.yield_convention
    if convention.compounding != ANNUAL:
        raise UnsupportedProduct(
            f"portfolio yields are effective annual; the issue quotes {convention.identifier}"
        )
    calendar = _use_calendar(snapshot, instrument, used_calendars)
    settlement = calendar.add_business_days(snapshot.business_date, instrument.settlement_lag_days)
    curve_id, fallback_from = _bond_discount_curve(policy, instrument, snapshot)
    built = curve(curve_id)
    if built.real:
        raise InvalidTerms(
            f"curve {curve_id} is a real curve; only inflation-linked bonds are discounted on it"
        )
    currency = instrument.currency
    if built.currency != currency:
        raise InvalidTerms(
            f"curve {curve_id} is a {built.currency} curve; the bond pays {currency}"
        )
    foreign = currency != "RUB"
    fx = snapshot.fx_rate(currency) if foreign else 1.0
    if foreign:
        used_fx[currency] = FxRate(currency, fx)
    horizon_date, offer = instrument.horizon(policy.horizon, settlement)
    if policy.require_known_coupons:
        _check_known_coupons(instrument, settlement, horizon_date)
    flows = instrument.cashflows(after=settlement, horizon=horizon_date, offer=offer)
    if not flows:
        raise InvalidTerms("no cashflows remain after settlement")
    spread_kind = policy.spread_kind_for(instrument.instrument_id)
    quote = snapshot.quote(instrument.instrument_id)
    accrued = _accrued(instrument, quote, settlement, snapshot)
    nominal = instrument.outstanding_face(settlement)
    # RUB amounts: unchanged for a RUB bond, converted at the snapshot rate otherwise.
    rub_flows: tuple[BondCashflow, ...] = (
        tuple(BondCashflow(f.payment_date, f.amount * fx) for f in flows) if foreign else flows
    )
    nominal_rub = nominal * fx if foreign else nominal
    accrued_rub = accrued * fx if foreign else accrued
    basis: Literal["continuous_zero", "ois_quote"] = (
        "ois_quote" if isinstance(built.curve, CurveBootstrapRequest) else "continuous_zero"
    )
    projection_id: str | None = None
    yield_flows: Sequence[BondCashflow] = flows
    if isinstance(instrument.coupon_leg, FloatingCouponLeg):
        engine, adapter_id, projection_id = _floating_engine(
            instrument,
            snapshot,
            policy,
            built,
            curve_id,
            projection,
            cache,
            (settlement, horizon_date, offer),
            flows,
            quote,
            (nominal, accrued, position.quantity),
            run_id,
        )
        # Yield and duration of a floater are on its base projected flows.
        yield_flows = tuple(
            BondCashflow(d, a)
            for d, a in sorted(
                (
                    *((f.payment_date, f.amount) for f in engine.request.cashflows),
                    *((c.payment_date, c.amount_rub) for c in engine.projected_coupons),
                )
            )
        )
    else:
        prepared = prepare_local_bond_risk(
            FixedRubBondInput(
                context=RiskRunContext(
                    run_id,
                    instrument.instrument_id,
                    snapshot.snapshot_id,
                    snapshot.snapshot_id,
                    f"{snapshot.snapshot_id}:{curve_id}",
                    basis,
                ),
                cashflows=rub_flows,
                settlement_date=settlement,
                horizon_date=horizon_date,
                horizon="maturity" if offer is None else "confirmed_investor_put",
                cashflow_currency="RUB",
                coupon_model="fixed",
                current_nominal_rub=nominal_rub,
                clean_price_pct=quote.clean_price_pct,
                accrued_interest_rub=accrued_rub,
                quantity=position.quantity,
                curve=built.curve,
                spread_convention=policy.spread_convention,
                spread_kind=spread_kind,
            ),
            curve_cache=cache,
        )
        engine, adapter_id = prepared.engine, prepared.calculation_id
    risk = engine.risk(bump_bp=policy.bump_bp, difference="central")
    second = engine.second_order(bump_bp=policy.bump_bp) if policy.include_second_order else None
    factor_risk = bond_factor_risk(risk, curve_id, second, projection_curve_id=projection_id)
    dirty = cast(float, engine.request.dirty_price_rub)
    dirty_ccy = (
        clean_price_pct_to_dirty_rub(quote.clean_price_pct, nominal, accrued) if foreign else dirty
    )
    effective_yield, duration = _yield_and_duration(settlement, dirty_ccy, yield_flows, convention)
    g_spread = None
    floating = isinstance(instrument.coupon_leg, FloatingCouponLeg)
    # A G-spread compares a fixed-rate yield with the KBD; a floater has none.
    kbd = _g_spread_kbd(policy, built, curve, currency) if not floating else None
    if kbd is not None:
        # The KBD is effective annual on ACT/365F: compare on that basis.
        kbd_yield, kbd_tenor = (
            (effective_yield, duration)
            if convention == DEFAULT_YIELD_CONVENTION
            else _yield_and_duration(settlement, dirty_ccy, yield_flows, DEFAULT_YIELD_CONVENTION)
        )
        g_spread = g_spread_bp(kbd, kbd_yield, kbd_tenor)
    margin = engine.discount_margin_bp() if floating and policy.discount_margin else None
    spread_buckets = (
        engine.spread_bucket_risk(bump_bp=policy.bump_bp, risk=risk) if policy.spread_buckets else ()
    )
    jtd = (
        None
        if position.recovery_rate is None
        else engine.jump_to_default(
            outstanding_nominal_rub_per_bond=nominal_rub, recovery_rate=position.recovery_rate
        ).jtd_pnl_rub
    )
    amounts = None
    if foreign:
        pv_ccy = finite_money(dirty_ccy * position.quantity, "position PV in currency")
        factor_risk = fx_translated_factor_risk(factor_risk, currency, pv_ccy * fx / 100.0)
        amounts = CurrencyAmounts(currency, fx, nominal, accrued, dirty_ccy, pv_ccy)
    calculation_id = canonical_sha256(
        {
            "job_id": job_id,
            "position_id": position.position_id,
            "adapter_id": adapter_id,
        }
    )
    valuation = PositionValuation(
        position.position_id,
        instrument.instrument_id,
        position.book,
        position.accounting_category,
        calculation_id,
        curve_id,
        built.entry.spec_id,
        basis,
        settlement,
        _horizon_label(policy, offer),
        horizon_date,
        position.quantity,
        nominal_rub,
        quote.clean_price_pct,
        accrued_rub,
        dirty,
        risk.pv_rub,
        spread_kind,
        policy.spread_convention.identifier,
        engine.spread_bp,
        effective_yield,
        duration,
        g_spread,
        factor_risk,
        jtd,
        convention.identifier,
        amounts,
        discount_margin_bp=margin,
        spread_buckets_rub=spread_buckets,
        curve_fallback_from=fallback_from,
    )
    return valuation, engine


def _bond_discount_curve(
    policy: ValuationPolicy, instrument: Instrument, snapshot: MarketSnapshot
) -> tuple[str, str | None]:
    """The discount curve ID of a bond and, when it is a fallback, the curve it replaces.

    Only a foreign curve that is absent from the snapshot falls back, and only to the
    policy's ``foreign_curve_fallbacks`` of the bond's currency; the caller still checks
    that the curve chosen is of that currency.
    """
    curve_id = policy.discount_curve_for(instrument)
    currency = instrument.currency
    if currency == "RUB" or instrument.instrument_id in dict(policy.curve_overrides):
        return curve_id, None
    present = {entry.curve_id for entry in snapshot.curves}
    if curve_id in present:
        return curve_id, None
    fallbacks = policy.fallback_curves_for(currency)
    for candidate in fallbacks:
        if candidate in present:
            return candidate, curve_id
    if not fallbacks:
        return curve_id, None  # the curve lookup raises MISSING_MARKET_DATA
    tried = ", ".join(repr(c) for c in (curve_id, *fallbacks))
    raise MissingMarketData(
        f"no {currency} discount curve in snapshot {snapshot.snapshot_id} (tried {tried})"
    )


def _horizon_label(
    policy: ValuationPolicy, offer: Offer | None
) -> Literal["maturity", "confirmed_put", "open_put"]:
    if offer is None:
        return "maturity"
    return "open_put" if policy.horizon == "open_put" else "confirmed_put"


def _g_spread_kbd(
    policy: ValuationPolicy,
    built: _BuiltCurve,
    curve: Callable[[str], _BuiltCurve],
    currency: str,
) -> ZcycParameters | None:
    """The KBD parameters a bond's G-spread is measured to, or None for no G-spread.

    Without ``g_spread_curve_id`` only a bond discounted on KBD parameters has one (the
    rule before 0.33.0). With it, every RUB bond is measured to that curve, which must be
    a KBD built from its parameters.
    """
    if policy.g_spread_curve_id is None:
        return built.kbd
    if currency != "RUB":
        return None
    kbd = curve(policy.g_spread_curve_id).kbd
    if kbd is None:
        raise InvalidTerms(
            f"G-spread curve {policy.g_spread_curve_id} must be a KBD given by its parameters"
        )
    return kbd


def _check_known_coupons(instrument: Instrument, settlement: date, horizon: date) -> None:
    """Raise unless the fixed coupons run without a gap from settlement to ``horizon``.

    ``ValuationPolicy.require_known_coupons`` (core brief C7). A zero amount counts as
    unknown (MOEX lists coupons that are not set yet as 0). A bond without a coupon
    leg is a zero-coupon bond and passes; floaters project their coupons and pass.
    """
    leg = instrument.coupon_leg
    if not isinstance(leg, (FixedCouponLeg, RateAccrualCouponLeg)):
        return
    periods = [p for p in leg.periods if p.payment_date > settlement]
    label = f"{instrument.instrument_id}: coupons are not known to the horizon {horizon}"
    if not periods:
        raise MissingMarketData(f"{label} (none after {settlement})")
    for period in periods:
        if period.payment_date > horizon:
            break
        if period.amount <= 0.0:
            raise MissingMarketData(f"{label} (coupon paid {period.payment_date} has no amount)")
    for left, right in pairwise(periods):
        if left.payment_date >= horizon:
            break
        if right.accrual_start > left.accrual_end:
            raise MissingMarketData(f"{label} (no coupon for {left.accrual_end}..{right.accrual_start})")
    last = periods[-1]
    if max(last.payment_date, last.accrual_end) < horizon:
        raise MissingMarketData(f"{label} (the last known coupon ends {last.accrual_end})")


def _floating_engine(
    instrument: Instrument,
    snapshot: MarketSnapshot,
    policy: ValuationPolicy,
    built: _BuiltCurve,
    curve_id: str,
    projection: Callable[[str, FixingIndex], _BuiltCurve],
    cache: ShockedCurveCache,
    dates: tuple[date, date, Offer | None],
    flows: Sequence[BondCashflow],
    quote: BondPriceObservation,
    amounts: tuple[float, float, float],
    run_id: str,
) -> tuple[BondRiskEngine, str, str | None]:
    """The risk engine of a RUB floater, its adapter ID and its separate projection curve.

    ``flows`` are the principal flows; the coupons come from the floating leg.
    """
    settlement, horizon_date, offer = dates
    nominal, accrued, quantity = amounts
    leg = cast(FloatingCouponLeg, instrument.coupon_leg)
    projection_id = cast(str, leg.projection_curve_id)
    if built.curve.curve_date != snapshot.business_date:
        raise InvalidTerms(f"curve {curve_id} is not dated the snapshot business date")
    model = instrument.floating_leg(
        snapshot, horizon=horizon_date, strike_guard_bp=policy.cap_floor_guard_bp
    )
    projected = projection(projection_id, leg.rule.index)
    two_curve = projection_id != curve_id
    engine = BondRiskEngine(
        BondRiskRequest(
            cashflows=tuple(BondCashflow(f.payment_date, f.amount) for f in flows),
            settlement_date=settlement,
            curve=built.curve,
            spread_convention=policy.spread_convention,
            spread_kind=policy.spread_kind_for(instrument.instrument_id),
            dirty_price_rub=clean_price_pct_to_dirty_rub(quote.clean_price_pct, nominal, accrued),
            quantity=quantity,
            floating_leg=model,
            projection_curve=projected.curve if two_curve else None,
        ),
        cache,
    )
    adapter_id = canonical_sha256(
        {
            "adapter_model": "portfolio-floating-rub-risk-v1",
            "library_version": __version__,
            "run_id": run_id,
            "instrument_id": instrument.instrument_id,
            "market_snapshot_id": snapshot.snapshot_id,
            "discount_curve": f"{snapshot.snapshot_id}:{curve_id}",
            "projection_curve": f"{snapshot.snapshot_id}:{projection_id}",
            "fixing_id": leg.fixing_id,
            "core_base_id": engine.base_id,
            "horizon": "maturity" if offer is None else "confirmed_investor_put",
            "horizon_date": horizon_date,
            "current_nominal_rub": nominal,
            "clean_price_pct": quote.clean_price_pct,
            "accrued_interest_rub": accrued,
        }
    )
    return engine, adapter_id, projection_id if two_curve else None


def _value_linker_position(
    position: Position,
    instrument: Instrument,
    snapshot: MarketSnapshot,
    policy: ValuationPolicy,
    curve: Callable[[str], _BuiltCurve],
    cache: ShockedCurveCache,
    job_id: str,
    run_id: str,
    used_calendars: dict[str, CalendarUse],
    used_cpi: dict[str, CpiUse],
) -> tuple[PositionValuation, PreparedLocalBondRisk]:
    """Value an inflation-linked bond on a real curve (package 9a, ``docs/linkers.md``).

    The indexed nominal and accrued of the settlement date come from the snapshot's CPI
    series; the real cash flows hold that nominal (:func:`linkers.real_cashflows`) and
    are discounted on the real curve that ``curve_overrides`` names for the bond. The
    quote is % of the indexed nominal. Risk is ``REAL|<curve>|<bucket>`` and ``CS``;
    ``effective_yield`` is the real yield. There is no G-spread (the KBD is nominal).
    """
    if instrument.currency != "RUB":
        raise UnsupportedProduct("inflation-linked bonds are supported in RUB only")
    if position.currency != instrument.currency:
        raise InvalidTerms("position currency differs from the instrument currency")
    principal = instrument.principal_leg
    if not isinstance(principal, IndexedPrincipalLeg):
        raise UnsupportedProduct("an inflation-linked bond needs an indexed principal leg")
    convention = instrument.yield_convention
    if convention != DEFAULT_YIELD_CONVENTION:
        raise UnsupportedProduct("linker real yields are effective annual ACT/365F")
    calendar = _use_calendar(snapshot, instrument, used_calendars)
    settlement = calendar.add_business_days(snapshot.business_date, instrument.settlement_lag_days)
    series = snapshot.cpi_series(principal.indexation.series_id)
    used_cpi[series.series_id] = CpiUse(
        series.series_id, series.source_id, series.as_of, series.first_month, series.last_month
    )
    curve_id = policy.curve_for(instrument.instrument_id)
    built = curve(curve_id)
    if not built.real or built.currency != "RUB":
        raise InvalidTerms(
            f"curve {curve_id} is not a RUB real curve; name one for the linker in "
            "ValuationPolicy.curve_overrides"
        )
    nominal = linker_nominal(instrument, series, settlement)
    flows = real_cashflows(instrument, series, settlement)
    if not flows:
        raise InvalidTerms("no cashflows remain after settlement")
    quote = snapshot.quote(instrument.instrument_id)
    accrued = (
        quote.accrued_per_bond
        if quote.accrued_per_bond is not None
        else linker_accrued(instrument, series, settlement)
    )
    maturity = principal.maturity_date
    prepared = prepare_local_bond_risk(
        FixedRubBondInput(
            context=RiskRunContext(
                run_id,
                instrument.instrument_id,
                snapshot.snapshot_id,
                snapshot.snapshot_id,
                f"{snapshot.snapshot_id}:{curve_id}",
                "continuous_zero",
            ),
            # Real flows: fixed in settlement-date money, discounted on the real curve.
            cashflows=flows,
            settlement_date=settlement,
            horizon_date=maturity,
            horizon="maturity",
            cashflow_currency="RUB",
            coupon_model="fixed",
            current_nominal_rub=nominal,
            clean_price_pct=quote.clean_price_pct,
            accrued_interest_rub=accrued,
            quantity=position.quantity,
            curve=built.curve,
            spread_convention=policy.spread_convention,
            spread_kind=policy.spread_kind_for(instrument.instrument_id),
        ),
        curve_cache=cache,
    )
    engine = prepared.engine
    risk = engine.risk(bump_bp=policy.bump_bp, difference="central")
    second = engine.second_order(bump_bp=policy.bump_bp) if policy.include_second_order else None
    dirty = cast(float, engine.request.dirty_price_rub)
    real_yield, duration = _yield_and_duration(settlement, dirty, flows, convention)
    jtd = (
        None
        if position.recovery_rate is None
        else engine.jump_to_default(
            outstanding_nominal_rub_per_bond=nominal, recovery_rate=position.recovery_rate
        ).jtd_pnl_rub
    )
    calculation_id = canonical_sha256(
        {
            "job_id": job_id,
            "position_id": position.position_id,
            "adapter_id": prepared.calculation_id,
            "indexation": principal.indexation.identifier,
            "cpi": series.content_sha256,
        }
    )
    valuation = PositionValuation(
        position.position_id,
        instrument.instrument_id,
        position.book,
        position.accounting_category,
        calculation_id,
        curve_id,
        built.entry.spec_id,
        "continuous_zero",
        settlement,
        "maturity",
        maturity,
        position.quantity,
        nominal,
        quote.clean_price_pct,
        accrued,
        dirty,
        risk.pv_rub,
        policy.spread_kind_for(instrument.instrument_id),
        policy.spread_convention.identifier,
        engine.spread_bp,
        real_yield,
        duration,
        None,
        linker_factor_risk(risk, curve_id, second),
        jtd,
        spread_buckets_rub=(
            engine.spread_bucket_risk(bump_bp=policy.bump_bp, risk=risk)
            if policy.spread_buckets
            else ()
        ),
    )
    return valuation, prepared


def _yield_and_duration(
    settlement: date,
    dirty: float,
    flows: Sequence[BondCashflow],
    convention: YieldConvention,
) -> tuple[float, float]:
    """Effective annual yield and Macaulay duration, both on ``convention``'s time basis."""
    effective = solve_effective_annual_yield(
        YieldRequest(settlement, dirty, tuple(flows), convention)
    ).effective_annual_yield
    duration = bond_duration(
        annual_yield=effective,
        settlement_date=settlement,
        cashflows=tuple((f.payment_date, f.amount) for f in flows),
        convention=convention,
    ).macaulay_years
    return effective, duration


def _run_scenario(
    scenario: PortfolioScenario,
    positions: Sequence[Position],
    prepared: Mapping[str, BondRiskEngine],
    fx_currency: Mapping[str, str],
    rejections: list[Rejection],
    job_id: str,
    stopped: Callable[[], Interruption | None],
    real_curve_positions: frozenset[str] = frozenset(),
    bond_curves: Mapping[str, tuple[str, str | None]] | None = None,
    derivatives: Mapping[str, ProductOutcome] | None = None,
) -> PortfolioScenarioResult:
    """Reprice every computed position under ``scenario``.

    Bucket shifts are nominal-curve moves, so positions discounted on a real curve
    (inflation-linked bonds) are reported as ``UNSUPPORTED_PRODUCT`` rather than moved
    by a nominal shock, unless ``scenario.curve_shifts_bp`` names the real curve.
    Derivative positions reprice with their own engines (``ProductOutcome.reprice``).
    """
    named = bool(scenario.curve_shifts_bp)
    rows: list[PositionScenarioPnl] = []
    failed: list[Rejection] = []
    base_failures = {r.position_id: r for r in rejections}
    for position in positions:
        if position.position_id in base_failures:
            failure = base_failures[position.position_id]
            rows.append(PositionScenarioPnl(position.position_id, None, failure.code))
            failed.append(failure)
            continue
        reason = stopped()
        if reason is not None:
            code = _INTERRUPTION_CODES[reason]
            rows.append(PositionScenarioPnl(position.position_id, None, code))
            failed.append(
                Rejection(position.position_id, position.instrument_id, code, "job stopped")
            )
            continue
        outcome = None if derivatives is None else derivatives.get(position.position_id)
        if outcome is not None:
            try:
                value = outcome.reprice(scenario)
            except CoreRejection as exc:
                rows.append(PositionScenarioPnl(position.position_id, None, exc.code))
                failed.append(
                    Rejection(position.position_id, position.instrument_id, exc.code, exc.message)
                )
                continue
            except (ValueError, LookupError, ArithmeticError) as exc:
                rows.append(PositionScenarioPnl(position.position_id, None, CALCULATION_FAILED))
                failed.append(
                    Rejection(
                        position.position_id, position.instrument_id, CALCULATION_FAILED, str(exc)
                    )
                )
                continue
            rows.append(PositionScenarioPnl(position.position_id, value, None))
            continue
        curves = None if bond_curves is None else bond_curves.get(position.position_id)
        real_named = (
            named and curves is not None and scenario.named_curve_shift(curves[0]) is not None
        )
        if position.position_id in real_curve_positions and not real_named:
            code = UnsupportedProduct.code
            rows.append(PositionScenarioPnl(position.position_id, None, code))
            failed.append(
                Rejection(
                    position.position_id,
                    position.instrument_id,
                    code,
                    "scenario shifts are nominal; real-curve scenarios are not defined",
                )
            )
            continue
        engine = prepared[position.position_id]
        currency = fx_currency.get(position.position_id)
        move = 0.0 if currency is None else scenario.fx_move_pct(currency)
        shock = scenario.bond_shock(engine.request.spread_kind)
        try:
            if named and curves is not None:
                discount_id, projection_id = curves
                repriced = engine.reprice(
                    replace(shock, curve_shifts_bp=scenario.curve_shift(discount_id)),
                    projection_curve_shifts_bp=(
                        scenario.curve_shift(projection_id, projection=True)
                        if engine.two_curve and projection_id is not None
                        else None
                    ),
                )
            else:
                repriced = engine.reprice(
                    shock,
                    projection_curve_shifts_bp=(
                        scenario.projection_curve_shifts_bp if engine.two_curve else None
                    ),
                )
            pnl = repriced.total_pnl_rub
            if move:
                # RUB value is linear in the rate: the shocked PV at the moved rate.
                pnl = finite_money(
                    repriced.shocked_pv_rub * (1.0 + move / 100.0) - repriced.base_pv_rub,
                    "scenario P&L",
                )
        except (ValueError, ArithmeticError) as exc:
            rows.append(PositionScenarioPnl(position.position_id, None, CALCULATION_FAILED))
            failed.append(
                Rejection(
                    position.position_id, position.instrument_id, CALCULATION_FAILED, str(exc)
                )
            )
            continue
        rows.append(PositionScenarioPnl(position.position_id, pnl, None))
    coverage = _coverage(positions, failed)
    covered = finite_money(
        math.fsum(r.pnl_rub for r in rows if r.pnl_rub is not None), "covered scenario P&L"
    )
    return PortfolioScenarioResult(
        scenario.name,
        canonical_sha256({"job_id": job_id, "scenario": scenario}),
        coverage,
        tuple(rows),
        covered,
        covered if coverage.status == "complete" else None,
    )


def portfolio_result_hash_projection(
    result: PortfolioRiskResult | Mapping[str, object],
) -> dict[str, object]:
    raw = (
        cast(dict[str, object], json.loads(canonical_json_bytes(result)))
        if isinstance(result, PortfolioRiskResult)
        else dict(result)
    )
    if "result_sha256" not in raw:
        raise ValueError("result_sha256 field is required")
    raw["result_sha256"] = ""
    return raw


def _verify(result: PortfolioRiskResult | Mapping[str, object], schema: str) -> bool:
    raw = (
        cast(dict[str, object], json.loads(canonical_json_bytes(result)))
        if isinstance(result, PortfolioRiskResult)
        else dict(result)
    )
    if raw.get("schema_version") != schema:
        return False
    declared = raw.get("result_sha256")
    try:
        actual = canonical_sha256(portfolio_result_hash_projection(raw))
    except (TypeError, ValueError):
        return False
    return isinstance(declared, str) and declared == actual


def verify_portfolio_result(result: PortfolioRiskResult | Mapping[str, object]) -> bool:
    """True for a ``rub-portfolio-risk-result.v2`` result whose hash verifies."""
    return _verify(result, PORTFOLIO_RESULT_SCHEMA)


def verify_portfolio_result_v3(result: PortfolioRiskResult | Mapping[str, object]) -> bool:
    """True for a ``rub-portfolio-risk-result.v3`` result whose hash verifies.

    The hash is the same construction as v2: SHA-256 of the canonical JSON with
    ``result_sha256`` blanked.
    """
    return _verify(result, PORTFOLIO_RESULT_SCHEMA_V3)


def portfolio_result_to_json(result: PortfolioRiskResult) -> bytes:
    """Canonical JSON of a v2 or v3 result; raises unless its schema and hash verify."""
    v3 = isinstance(result, PortfolioRiskResultV3)
    if not (verify_portfolio_result_v3(result) if v3 else verify_portfolio_result(result)):
        raise ValueError("portfolio result hash or schema does not verify")
    return canonical_json_bytes(result)


def portfolio_result_from_json(data: bytes | str) -> PortfolioRiskResult:
    """Decode a v2 result (``PortfolioRiskResult``) or a v3 one (``PortfolioRiskResultV3``).

    The schema version selects the type; the hash must verify.
    """
    try:
        raw = json.loads(data)
    except (TypeError, ValueError) as exc:
        raise ValueError("portfolio result is not valid JSON") from exc
    schemas: dict[object, type[PortfolioRiskResult]] = {
        PORTFOLIO_RESULT_SCHEMA: PortfolioRiskResult,
        PORTFOLIO_RESULT_SCHEMA_V3: PortfolioRiskResultV3,
    }
    kind = schemas.get(raw.get("schema_version")) if isinstance(raw, dict) else None
    if kind is None:
        raise ValueError("not a portfolio result with a supported schema_version")
    if not _verify(raw, cast(str, raw["schema_version"])):
        raise ValueError("portfolio result_sha256 does not verify")
    return cast(PortfolioRiskResult, decode(kind, raw, "result"))


@dataclass(frozen=True)
class PositionHorizon:
    """Carry and rolldown of one position (RUB, signed by quantity) and its per-bond detail."""

    position_id: str
    instrument_id: str
    book: str
    accounting_category: AccountingCategory
    curve_id: str
    quantity: float
    carry_rub: float
    rolldown_rub: float
    received_rub: float
    horizon_pnl_rub: float
    per_bond: HorizonValuation


@dataclass(frozen=True)
class PortfolioHorizonResult:
    """Book carry to ``horizon_date``; totals are ``None`` unless coverage is complete.

    ``positions`` holds the bonds; the derivative rows are in one field per product
    (BACKLOG #101). Covered carry, rolldown and horizon P&L (the theta) sum every row.
    ``fx_horizon`` is the FX spot rule the FX products were carried with.
    """

    snapshot_id: str
    business_date: date
    horizon_date: date
    library_version: str
    coverage: PortfolioCoverage
    positions: tuple[PositionHorizon, ...]
    calendars: tuple[CalendarUse, ...]
    covered_carry_rub: float
    covered_rolldown_rub: float
    covered_horizon_pnl_rub: float
    total_horizon_pnl_rub: float | None
    model_version: str = HORIZON_MODEL_VERSION
    swaps: tuple[SwapPositionHorizon, ...] = field(default=(), metadata=OMIT_IF_DEFAULT)
    repos: tuple[RepoPositionHorizon, ...] = field(default=(), metadata=OMIT_IF_DEFAULT)
    cds: tuple[CdsPositionHorizon, ...] = field(default=(), metadata=OMIT_IF_DEFAULT)
    fx_forwards: tuple[FxForwardPositionHorizon, ...] = field(default=(), metadata=OMIT_IF_DEFAULT)
    xccy_swaps: tuple[XccyPositionHorizon, ...] = field(default=(), metadata=OMIT_IF_DEFAULT)
    fx_futures: tuple[FxFuturesPositionHorizon, ...] = field(default=(), metadata=OMIT_IF_DEFAULT)
    rate_futures: tuple[RateFuturesPositionHorizon, ...] = field(
        default=(), metadata=OMIT_IF_DEFAULT
    )
    ofz_futures: tuple[OfzFuturesPositionHorizon, ...] = field(default=(), metadata=OMIT_IF_DEFAULT)
    fx_horizon: FxHorizon = field(default="spot_held", metadata=OMIT_IF_DEFAULT)

    def derivative_rows(self) -> tuple[HorizonRow, ...]:
        """Every derivative carry row, product by product."""
        return tuple(row for name in HORIZON_ROW_FIELDS.values() for row in getattr(self, name))


def horizon_portfolio(
    *,
    snapshot: MarketSnapshot,
    instruments: Sequence[PortfolioInstrument],
    positions: Sequence[Position],
    policy: ValuationPolicy,
    horizon_date: date,
    curve_cache: ShockedCurveCache | None = None,
    fx_horizon: FxHorizon = "spot_held",
) -> PortfolioHorizonResult:
    """Carry and rolldown of a book from the snapshot's business date to ``horizon_date``.

    Positions resolve exactly as in :func:`value_portfolio` (instrument checks, curve,
    calendar, quote and accrued, horizon policy); each spread is calibrated to the
    quoted dirty price and held. Swaps and repo legs are carried on the engines
    ``value_portfolio`` builds for them (``swap_horizon``, ``repo_carry``); other
    derivatives are ``UNSUPPORTED_PRODUCT``. Rejections carry the same codes.

    ``fx_horizon`` states the FX spot at the horizon for FX products: ``"spot_held"``
    (today's spot, sticky spot) or ``"forwards_realised"`` (today's forward for the
    horizon spot value date).
    """
    for names, label in (
        ([i.instrument_id for i in instruments], "instrument"),
        ([p.position_id for p in positions], "position"),
    ):
        if len(set(names)) != len(names):
            raise ValueError(f"duplicate {label} identifiers")
    if horizon_date <= snapshot.business_date:
        raise InvalidTerms("horizon date must be after the snapshot business date")
    fx_rule = fx_horizon_rule(fx_horizon)
    exercised_memo: dict[str, ExercisedBond | CoreRejection] = {}
    every = {i.instrument_id: i for i in instruments}
    by_id = {k: i for k, i in every.items() if isinstance(i, Instrument)}
    cache = ShockedCurveCache() if curve_cache is None else curve_cache
    built: dict[str, _BuiltCurve | CoreRejection] = {}
    used_calendars: dict[str, CalendarUse] = {}
    # Carry rows need no calculation IDs, so the context has no job identity.
    context = _product_context(snapshot, policy, "", cache, built, {}, used_calendars, {}, by_id)
    rows: list[PositionHorizon] = []
    derivatives: list[HorizonRow] = []
    rejections: list[Rejection] = []
    for position in positions:
        try:
            other = every.get(position.instrument_id)
            if other is not None and not isinstance(other, Instrument):
                derivatives.append(
                    horizon_adapter_for(other)(position, other, context, horizon_date, fx_rule)
                )
                continue
            rows.append(
                _horizon_position(
                    position,
                    by_id,
                    snapshot,
                    policy,
                    horizon_date,
                    built,
                    cache,
                    used_calendars,
                    exercised_memo,
                )
            )
        except CoreRejection as exc:
            rejections.append(
                Rejection(position.position_id, position.instrument_id, exc.code, exc.message)
            )
        except (ValueError, LookupError, ArithmeticError) as exc:
            rejections.append(
                Rejection(
                    position.position_id, position.instrument_id, CALCULATION_FAILED, str(exc)
                )
            )
    coverage = _coverage(positions, rejections)
    every_row = (*rows, *derivatives)
    by_field: dict[str, list[HorizonRow]] = {name: [] for name in HORIZON_ROW_FIELDS.values()}
    for row in derivatives:
        by_field[HORIZON_ROW_FIELDS[type(row)]].append(row)
    covered = finite_money(math.fsum(r.horizon_pnl_rub for r in every_row), "covered horizon P&L")
    return PortfolioHorizonResult(
        snapshot.snapshot_id,
        snapshot.business_date,
        horizon_date,
        __version__,
        coverage,
        tuple(rows),
        tuple(used_calendars[k] for k in sorted(used_calendars)),
        finite_money(math.fsum(r.carry_rub for r in every_row), "covered carry"),
        finite_money(math.fsum(r.rolldown_rub for r in every_row), "covered rolldown"),
        covered,
        covered if coverage.status == "complete" else None,
        **{name: tuple(items) for name, items in by_field.items()},  # type: ignore[arg-type]
        fx_horizon=fx_rule,
    )


def _horizon_position(
    position: Position,
    instruments: Mapping[str, Instrument],
    snapshot: MarketSnapshot,
    policy: ValuationPolicy,
    horizon_date: date,
    built: dict[str, _BuiltCurve | CoreRejection],
    cache: ShockedCurveCache,
    used_calendars: dict[str, CalendarUse],
    exercised_memo: dict[str, ExercisedBond | CoreRejection] | None = None,
) -> PositionHorizon:
    terms = instruments.get(position.instrument_id)
    if terms is not None and policy.exercise_assumptions:
        exercised = _exercised(
            terms, policy, snapshot, {} if exercised_memo is None else exercised_memo
        )
        if exercised is not None:
            instruments = _Replaced(instruments, exercised.instrument)
    instrument = _supported_instrument(position, instruments)
    calendar = _use_calendar(snapshot, instrument, used_calendars)
    curve_id = policy.curve_for(instrument.instrument_id)
    if curve_id not in built:
        try:
            built[curve_id] = _build_curve(snapshot.curve(curve_id), snapshot.business_date)
        except CoreRejection as exc:
            built[curve_id] = exc
    outcome = built[curve_id]
    if isinstance(outcome, CoreRejection):
        raise outcome
    if outcome.curve.curve_date != snapshot.business_date:
        raise InvalidTerms(f"curve {curve_id} is not dated the snapshot business date")
    settlement = calendar.add_business_days(snapshot.business_date, instrument.settlement_lag_days)
    quote = snapshot.quote(instrument.instrument_id)
    accrued = _accrued(instrument, quote, settlement, snapshot)
    nominal = instrument.outstanding_face(settlement)
    fixings = projection_curve = None
    leg = instrument.coupon_leg
    if isinstance(leg, FloatingCouponLeg):
        fixings = snapshot.point_in_time_fixing(leg.fixing_id)
        projection_id = cast(str, leg.projection_curve_id)
        source = snapshot.projection_curve(projection_id, leg.rule.index)
        projection_curve = None if projection_id == curve_id else source
    per_bond = horizon_valuation(
        instrument,
        outcome.curve,
        calendar,
        horizon_date=horizon_date,
        dirty_price_rub=clean_price_pct_to_dirty_rub(quote.clean_price_pct, nominal, accrued),
        spread_convention=policy.spread_convention,
        redemption_horizon=policy.horizon,
        curve_cache=cache,
        fixings=fixings,
        projection_curve=projection_curve,
        strike_guard_bp=policy.cap_floor_guard_bp,
    )
    q = position.quantity
    return PositionHorizon(
        position.position_id,
        instrument.instrument_id,
        position.book,
        position.accounting_category,
        curve_id,
        q,
        per_bond.carry_rub * q,
        per_bond.rolldown_rub * q,
        per_bond.received_rub * q,
        per_bond.horizon_pnl_rub * q,
        per_bond,
    )
