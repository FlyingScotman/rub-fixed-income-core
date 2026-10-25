"""Cross-currency swaps against RUB: CNY/RUB vs RUONIA (roadmap package 7).

A cross-currency swap has one RUB leg and one foreign-currency leg, each a "bond" in
its currency: its receiver pays the notional at the start (initial exchange), receives
fixed or floating coupons and the notional back at the end (final exchange), with
optional intermediate exchanges as the notional amortizes. The RUB leg is fixed or
floats on compounded RUONIA (``RUB-RUONIA-OIS-COMPOUND``); the foreign leg is fixed or
floats on an explicit compounded overnight index of its currency. Known fixings and
published amounts never move under shocks.

Valuation, all in RUB, for a curve date ``c`` and a spot rate ``S`` for value date ``s``::

    PV = sum_RUB  CF * DF_rub(P) + S * DF_rub(s) / DF_for(s) * sum_for  CF * DF_for(P) * exp(b tau(s, P))

the second sum being covered parity (``fx_forwards``) applied to every foreign flow: a
foreign flow paid at ``P`` is worth its outright ``F(P)`` in RUB discounted on the RUB
curve. ``DF_for`` is the foreign discount curve under the RUB collateral (caller-built,
implied from FX swaps with ``fx_forwards.implied_foreign_curve``, or bootstrapped here
from XCCY par quotes with :func:`bootstrap_xccy_curve`), and ``b`` an optional basis in
bp with the sign and day count of the FX-forward basis. A zero-coupon swap with only a
final exchange is therefore exactly an FX forward.

The standard MOEX swap behind the hub's ``cny_ruonia_xccy`` quotes (``XCS CNYRUB``) is
registered as :func:`xcs_cnyrub_convention`; its sources are in ``docs/xccy.md``. No
market data, calendar or confirmation term is looked up here.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from itertools import pairwise
from typing import ClassVar, Literal, cast

from ._curve_views import FxHorizon, roll_curve
from ._curve_views import fx_horizon as _check_fx_horizon
from ._money import finite_money, positive_money
from ._quote_curves import QuoteCurve
from ._risk_common import (
    _ZERO_SHOCK,
    RISK_BUCKETS,
    ShockedCurveCache,
    _base_curve,
    _curve_basis,
    _curve_identity,
    _shift_at,
    _shifts,
    _shock_curve,
    _snapshot_curve,
)
from ._solver import bisect
from .canonical import _Canonical, _precanonical, canonical_sha256
from .conventions import (
    CurveConvention,
    DayCount,
    OisSwapConvention,
    _ruonia_ois_compound_act,
    calendar_fingerprint,
)
from .dates import BusinessCalendar
from .discount import CurveBootstrapRequest, DiscountCurve, DiscountFactorCurve
from .errors import InvalidTerms, MissingMarketData, UnsupportedProduct
from .factors import FactorRisk, HessianEntry, fx_factor, ir_factor
from .floating import FixingHistory
from .fx_futures import FxFuturesScenario
from .tenors import parse_tenor, resolve_tenor

__all__ = [
    "RUONIA_OIS_COMPOUND",
    "XCCY_CURVE_MODEL_VERSION",
    "XCCY_HORIZON_MODEL_VERSION",
    "XCCY_MODEL_VERSION",
    "XCCY_QUOTE_SPEC_ID",
    "XCCY_SECOND_ORDER_MODEL_VERSION",
    "CompoundedIndex",
    "IndexFixings",
    "NotionalExchange",
    "XccyBucketRisk",
    "XccyCashflow",
    "XccyCurveBootstrapRequest",
    "XccyCurveCache",
    "XccyCurveRisk",
    "XccyHorizonValuation",
    "XccyLeg",
    "XccyParQuote",
    "XccyPeriod",
    "XccyQuoteConvention",
    "XccyRiskEngine",
    "XccyRiskRequest",
    "XccyRiskResult",
    "XccyScenario",
    "XccyScenarioResult",
    "XccySecondOrderRisk",
    "XccySwap",
    "bootstrap_xccy_curve",
    "notional_exchanges",
    "standard_xccy_swap",
    "xccy_basis_factor",
    "xccy_factor_risk",
    "xccy_horizon",
    "xccy_leg_periods",
    "xccy_par_rate",
    "xcs_cnyrub_convention",
]

XCCY_MODEL_VERSION = "rub-xccy-v1"
XCCY_SECOND_ORDER_MODEL_VERSION = "rub-xccy-second-order-v1"
XCCY_HORIZON_MODEL_VERSION = "rub-xccy-horizon-v1"
XCCY_CURVE_MODEL_VERSION = "xccy-cny-logdf-v1"
XCCY_QUOTE_SPEC_ID = "cny_ruonia_xccy_v1"
"""Core spec ID of the MOEX ``XCS CNYRUB`` standard swap (:func:`xcs_cnyrub_convention`)."""

LegSide = Literal["receive", "pay"]
CompoundingBasis = Literal["nfa_act", "act_365f", "act_360"]
XccyCurveRole = Literal["rub_discount", "rub_projection", "foreign_discount", "foreign_projection"]
FlowSource = Literal["fixed", "known", "fixed_by_history", "partially_projected", "projected"]
FlowKind = Literal["coupon", "exchange"]
LegName = Literal["rub", "foreign"]

_RUONIA_INDEX = "RUB-RUONIA-OIS-COMPOUND"
_BASES = ("nfa_act", "act_365f", "act_360")
_CACHE_SIZE = 4096
_REPRICING_TOLERANCE_BP = 1e-7


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InvalidTerms(f"{label} must be nonempty text")
    return value


def _currency(value: object) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 3
        or not value.isascii()
        or not value.isalpha()
        or not value.isupper()
    ):
        raise InvalidTerms("currency must be a three-letter upper-case ISO code")
    return value


def _lag(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise InvalidTerms(f"{label} must be a nonnegative integer")
    return value


def _date(value: object, label: str) -> date:
    if not isinstance(value, date):
        raise TypeError(f"{label} must be a date")
    return value


def _signed_years(day_count: DayCount, start: date, end: date) -> float:
    if end >= start:
        return day_count.year_fraction(start, end)
    return -day_count.year_fraction(end, start)


# --- Index terms and fixings --------------------------------------------------------


@dataclass(frozen=True)
class CompoundedIndex:
    """A daily-compounded overnight index: ``rate = (prod(1 + r_i n_i / W) - 1) * A / d``.

    ``n_i`` are the calendar days each business-day fixing applies, ``d`` the days of
    the period. ``weight_basis`` gives ``W`` and ``annualization_basis`` gives ``A``:
    ``nfa_act`` is 365, or 366 if any day of the period falls in a leap year (NFA,
    clause 8.4(б), ``RUB-RUONIA-OIS-COMPOUND``); ``act_365f`` is 365, ``act_360`` 360.
    The coupon is ``notional * (rate + spread) * leg day count``. Projected
    observations compound to the curve's ``DF(c) / DF(end)`` whatever the basis.
    """

    name: str
    weight_basis: CompoundingBasis
    annualization_basis: CompoundingBasis

    def __post_init__(self) -> None:
        _text(self.name, "index name")
        if self.weight_basis not in _BASES or self.annualization_basis not in _BASES:
            raise InvalidTerms(f"compounding bases must be one of {', '.join(_BASES)}")

    def _days(self, basis: CompoundingBasis, start: date, end: date) -> float:
        if basis == "nfa_act":
            return float(_ruonia_ois_compound_act(start, end))
        return 365.0 if basis == "act_365f" else 360.0

    def weight_days(self, start: date, end: date) -> float:
        return self._days(self.weight_basis, start, end)

    def annualization(self, start: date, end: date) -> float:
        """``A / d`` of the period ``[start, end)``."""
        return self._days(self.annualization_basis, start, end) / (end - start).days


RUONIA_OIS_COMPOUND = CompoundedIndex(_RUONIA_INDEX, "nfa_act", "nfa_act")
"""``RUB-RUONIA-OIS-COMPOUND`` of NFA amendments No. 1 (2021), clause 8.4(б)."""


@dataclass(frozen=True)
class IndexFixings:
    """Published fixings of a foreign overnight index, dated before ``valuation_date``.

    Every business day of ``calendar`` that a remaining period observes before the
    valuation date must have its fixing; a missing one raises ``MissingMarketData``
    and is never filled. Rates are decimal annual.
    """

    index: str
    valuation_date: date
    fixings: tuple[tuple[date, float], ...]
    calendar: BusinessCalendar
    snapshot_id: str

    def __post_init__(self) -> None:
        _text(self.index, "index name")
        _text(self.snapshot_id, "snapshot_id")
        if not isinstance(self.calendar, BusinessCalendar):
            raise TypeError("calendar must be a BusinessCalendar")
        rows = tuple((_date(d, "fixing date"), finite_money(r, "fixing")) for d, r in self.fixings)
        for (left, _), (right, _) in pairwise(rows):
            if right <= left:
                raise InvalidTerms("fixing dates must be strictly increasing")
        if rows and rows[-1][0] >= self.valuation_date:
            raise InvalidTerms("fixings must be dated before the valuation date")
        object.__setattr__(self, "fixings", rows)
        object.__setattr__(self, "_by_date", dict(rows))

    @property
    def identity(self) -> dict[str, object]:
        return {
            "index": self.index,
            "valuation_date": self.valuation_date,
            "snapshot_id": self.snapshot_id,
            "calendar_version": self.calendar.version,
            "calendar_fingerprint": calendar_fingerprint(self.calendar),
            "fixings_sha256": canonical_sha256(self.fixings),
        }

    def rate(self, observation: date) -> tuple[float, bool]:
        if observation >= self.valuation_date:
            raise ValueError("observation is not before the valuation date")
        value = cast(dict[date, float], self.__dict__["_by_date"]).get(observation)
        if value is None:
            raise LookupError(f"missing {self.index} fixing on {observation.isoformat()}")
        return value, False


Fixings = FixingHistory | IndexFixings


# --- Terms --------------------------------------------------------------------------


@dataclass(frozen=True)
class XccyPeriod:
    """One interest period ``[accrual_start, accrual_end)`` of a leg, paid on ``payment_date``.

    ``notional`` is the leg-currency notional outstanding in the period (positive).
    ``known_amount`` is a published or confirmed coupon (unsigned, leg currency): it is
    used as is and never moves.
    """

    accrual_start: date
    accrual_end: date
    payment_date: date
    notional: float
    known_amount: float | None = None

    def __post_init__(self) -> None:
        for name in ("accrual_start", "accrual_end", "payment_date"):
            _date(getattr(self, name), name)
        if self.accrual_end <= self.accrual_start:
            raise InvalidTerms("an interest period must end after it starts")
        if self.payment_date < self.accrual_end:
            raise InvalidTerms("an interest payment cannot precede its period end")
        object.__setattr__(self, "notional", positive_money(self.notional, "period notional"))
        if self.known_amount is not None:
            object.__setattr__(
                self, "known_amount", finite_money(self.known_amount, "known coupon amount")
            )


@dataclass(frozen=True)
class NotionalExchange:
    """A notional payment of a leg: ``amount`` (leg currency) is received by the leg's
    receiver when positive (final and amortization exchanges) and paid when negative
    (the initial exchange)."""

    payment_date: date
    amount: float

    def __post_init__(self) -> None:
        _date(self.payment_date, "exchange payment_date")
        amount = finite_money(self.amount, "exchange amount")
        if amount == 0.0:
            raise InvalidTerms("a notional exchange needs a nonzero amount")
        object.__setattr__(self, "amount", amount)


@dataclass(frozen=True)
class XccyLeg:
    """One currency leg. ``side="receive"``: the position receives its coupons and final
    notional (and pays the initial exchange). Exactly one of ``fixed_rate`` and ``index``.

    ``spread`` (decimal) is added to a floating rate simply, ``N * (rate + spread) *
    alpha`` (NFA standard terms 2011, 7.3(а), no compounding condition). Coupons with a
    ``known_amount`` and every exchange are fixed amounts.
    """

    currency: str
    side: LegSide
    day_count: DayCount
    periods: tuple[XccyPeriod, ...]
    exchanges: tuple[NotionalExchange, ...] = ()
    fixed_rate: float | None = None
    index: CompoundedIndex | None = None
    spread: float = 0.0

    def __post_init__(self) -> None:
        _currency(self.currency)
        if self.side not in ("receive", "pay"):
            raise InvalidTerms("leg side must be receive or pay")
        if not isinstance(self.day_count, DayCount):
            raise TypeError("day_count must be a DayCount")
        periods = tuple(self.periods)
        exchanges = tuple(self.exchanges)
        if not all(isinstance(p, XccyPeriod) for p in periods):
            raise TypeError("periods must be XccyPeriod values")
        if not all(isinstance(e, NotionalExchange) for e in exchanges):
            raise TypeError("exchanges must be NotionalExchange values")
        if not periods and not exchanges:
            raise InvalidTerms("a leg needs interest periods or notional exchanges")
        for left, right in pairwise(periods):
            if right.accrual_start < left.accrual_end:
                raise InvalidTerms("interest periods must be ordered and must not overlap")
        if (self.fixed_rate is None) == (self.index is None):
            raise InvalidTerms("a leg is either fixed (fixed_rate) or floating (index)")
        if self.fixed_rate is not None:
            object.__setattr__(self, "fixed_rate", finite_money(self.fixed_rate, "fixed rate"))
        if self.index is not None and not isinstance(self.index, CompoundedIndex):
            raise TypeError("index must be a CompoundedIndex")
        spread = finite_money(self.spread, "spread")
        if self.fixed_rate is not None and spread != 0.0:
            raise InvalidTerms("a fixed leg has no spread; fold it into the fixed rate")
        object.__setattr__(self, "spread", spread)
        object.__setattr__(self, "periods", periods)
        object.__setattr__(self, "exchanges", exchanges)

    @property
    def sign(self) -> float:
        return 1.0 if self.side == "receive" else -1.0

    @property
    def is_floating(self) -> bool:
        return self.index is not None


@dataclass(frozen=True)
class XccySwap:
    """A RUB leg and a foreign leg of opposite sides.

    The RUB leg is fixed or floats on ``RUB-RUONIA-OIS-COMPOUND``. Notionals are fixed by
    the terms (per period, with explicit exchanges); a mark-to-market notional reset is
    not modelled and raises ``UnsupportedProduct``.
    """

    trade_id: str
    rub_leg: XccyLeg
    foreign_leg: XccyLeg
    mark_to_market_resets: bool = False

    def __post_init__(self) -> None:
        _text(self.trade_id, "trade_id")
        if not isinstance(self.rub_leg, XccyLeg) or not isinstance(self.foreign_leg, XccyLeg):
            raise TypeError("legs must be XccyLeg values")
        if self.mark_to_market_resets is not False:
            raise UnsupportedProduct(
                "mark-to-market (resettable) cross-currency swaps are not modelled: the NCC "
                "product parameters list no notional reset, and its FX fixing is unspecified"
            )
        if self.rub_leg.currency != "RUB":
            raise InvalidTerms("rub_leg must be in RUB")
        if self.foreign_leg.currency == "RUB":
            raise InvalidTerms("foreign_leg must be in a currency other than RUB")
        if self.rub_leg.side == self.foreign_leg.side:
            raise InvalidTerms("the two legs of a cross-currency swap have opposite sides")
        rub_index = self.rub_leg.index
        if rub_index is not None and rub_index.name != _RUONIA_INDEX:
            raise UnsupportedProduct(
                f"RUB floating index {rub_index.name!r} is not modelled; only {_RUONIA_INDEX}"
            )
        foreign_index = self.foreign_leg.index
        if foreign_index is not None and foreign_index.name == _RUONIA_INDEX:
            raise InvalidTerms("RUONIA is a RUB index; the foreign leg needs its own index")

    @property
    def currency(self) -> str:
        return self.foreign_leg.currency


# --- Schedules and the standard swap ------------------------------------------------


def xccy_leg_periods(
    convention: OisSwapConvention,
    effective_date: date,
    unadjusted_end: date,
    end_date: date,
    notional: float,
) -> tuple[XccyPeriod, ...]:
    """Constant-notional periods of a leg on ``convention``'s schedule rule.

    The rule is that of :class:`~rub_fixed_income.conventions.OisSwapConvention`: a single
    period up to ``single_period_max_months``, otherwise ``frequency_months`` periods
    rolled back from ``unadjusted_end`` (short initial stub), each date adjusted, payments
    ``payment_lag_business_days`` after each period end. Amortizing legs pass their own
    periods.
    """
    if not isinstance(convention, OisSwapConvention):
        raise TypeError("convention must be an OisSwapConvention")
    try:
        periods = convention.periods(effective_date, unadjusted_end, end_date)
    except ValueError as exc:
        if isinstance(exc, InvalidTerms):
            raise
        raise InvalidTerms(str(exc)) from None
    return tuple(XccyPeriod(p.start, p.end, p.payment, notional) for p in periods)


def notional_exchanges(
    periods: Sequence[XccyPeriod],
    *,
    initial_date: date | None,
    final_date: date,
) -> tuple[NotionalExchange, ...]:
    """Exchanges of a leg from its period notionals (NFA standard terms 2011, 6.4).

    The initial exchange pays the first period's notional on ``initial_date`` (``None``:
    no initial exchange); each fall of the notional between periods is an intermediate
    exchange received on the earlier period's payment date (6.4(д)); the final exchange
    returns the last notional on ``final_date``. A rising notional raises
    ``InvalidTerms`` (it is not an installment of the final amount).
    """
    items = tuple(periods)
    if not items:
        raise InvalidTerms("notional exchanges need at least one period")
    rows = []
    if initial_date is not None:
        rows.append(NotionalExchange(initial_date, -items[0].notional))
    for left, right in pairwise(items):
        change = left.notional - right.notional
        if change < 0.0:
            raise InvalidTerms("a leg notional may only amortize between periods")
        if change > 0.0:
            rows.append(NotionalExchange(left.payment_date, change))
    rows.append(NotionalExchange(final_date, items[-1].notional))
    return tuple(rows)


@dataclass(frozen=True)
class XccyQuoteConvention:
    """Every term of a standard (quoted) cross-currency swap, all explicit.

    ``swap`` holds the dates and day counts: TOM start, end = start + tenor, the period
    rule, the payment lag, ``fixed_day_count`` for the foreign fixed leg and
    ``float_day_count`` for the RUB floating leg (``calendar`` is the joint calendar of
    the swap). ``rub_index`` is the RUB leg's index. The initial and final exchanges
    are paid ``initial_exchange_lag_days`` business days after the start and
    ``final_exchange_lag_days`` after the adjusted end. ``spec_id`` names the set.
    """

    swap: OisSwapConvention
    rub_index: CompoundedIndex
    initial_exchange_lag_days: int
    final_exchange_lag_days: int
    spec_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.swap, OisSwapConvention):
            raise TypeError("swap must be an OisSwapConvention")
        if not isinstance(self.rub_index, CompoundedIndex):
            raise TypeError("rub_index must be a CompoundedIndex")
        if self.rub_index.name != _RUONIA_INDEX:
            raise UnsupportedProduct(f"the quoted RUB leg must float on {_RUONIA_INDEX}")
        _lag(self.initial_exchange_lag_days, "initial_exchange_lag_days")
        _lag(self.final_exchange_lag_days, "final_exchange_lag_days")
        _text(self.spec_id, "spec_id")

    @property
    def identifier(self) -> str:
        return (
            f"{self.spec_id}:{self.swap.identifier}:rub={self.rub_index.name}:"
            f"{self.rub_index.weight_basis}/{self.rub_index.annualization_basis}:"
            f"exchange+{self.initial_exchange_lag_days}B/+{self.final_exchange_lag_days}B"
        )


def xcs_cnyrub_convention(calendar: BusinessCalendar) -> XccyQuoteConvention:
    """The MOEX ``XCS CNYRUB`` standard swap (spec ``cny_ruonia_xccy_v1``).

    From MOEX, «Методика формирования ценовых кривых СПФИ» (25 December 2024), Appendix
    1, section 3: trade date T+0, start TOM, end start + tenor, MODFOL start, end and
    periods; CNY fixed leg and RUB floating leg ``RUONIA-OIS-COMPOUND``, each a single
    period up to 1Y and annual beyond, Short Initial stub, Act/Act (ISDA), +1B payment
    offset, fixing offset 0B, no spread, no lookback; calendars SPFI, MOSCOW, BEIJING
    and RUONIA (``calendar`` is the caller's joint calendar). Index formula: NFA
    amendments No. 1 (2021), 8.4(б). Exchanges: MOEX product parameters (initial
    exchange, interest, reverse exchange) with the NFA 2011 standard-terms defaults,
    6.2(а)-(б): initial payment on the start date, final payment on the end date.
    """
    return XccyQuoteConvention(
        swap=OisSwapConvention(
            calendar,
            spot_lag_business_days=1,
            payment_lag_business_days=1,
            single_period_max_months=12,
            frequency_months=12,
            stub="short_initial",
            fixed_day_count=DayCount.ACT_ACT_ISDA,
            float_day_count=DayCount.ACT_ACT_ISDA,
        ),
        rub_index=RUONIA_OIS_COMPOUND,
        initial_exchange_lag_days=0,
        final_exchange_lag_days=0,
        spec_id=XCCY_QUOTE_SPEC_ID,
    )


def standard_xccy_swap(
    trade_id: str,
    trade_date: date,
    tenor: str,
    convention: XccyQuoteConvention,
    *,
    currency: str,
    foreign_notional: float,
    notional_fx_rate: float,
    fixed_rate: float,
    foreign_side: LegSide,
    rub_spread: float = 0.0,
) -> XccySwap:
    """A standard swap traded on ``trade_date``: foreign fixed against RUONIA (+ spread).

    The RUB notional is ``foreign_notional * notional_fx_rate`` (for ``XCS CNYRUB`` the
    NCC central CNY/RUB rate of the trade date). ``foreign_side`` is the position's side
    of the foreign fixed leg.
    """
    tenor_code = parse_tenor(tenor)
    if tenor_code.unit in ("ON", "TN", "SN"):
        raise InvalidTerms(f"{tenor_code.code} is not a swap tenor")
    terms = convention.swap
    dates = resolve_tenor(
        trade_date,
        tenor_code.code,
        terms.calendar,
        spot_lag_days=terms.spot_lag_business_days,
        business_day_convention=terms.business_day_convention,
    )
    foreign = positive_money(foreign_notional, "foreign notional")
    rub = positive_money(foreign * positive_money(notional_fx_rate, "notional FX rate"), "RUB")
    calendar = terms.calendar
    initial = calendar.add_business_days(dates.effective_date, convention.initial_exchange_lag_days)
    final = calendar.add_business_days(dates.end_date, convention.final_exchange_lag_days)

    def leg(currency_code: str, notional: float, side: LegSide, floating: bool) -> XccyLeg:
        periods = xccy_leg_periods(
            terms, dates.effective_date, dates.unadjusted_end, dates.end_date, notional
        )
        return XccyLeg(
            currency=currency_code,
            side=side,
            day_count=terms.float_day_count if floating else terms.fixed_day_count,
            periods=periods,
            exchanges=notional_exchanges(periods, initial_date=initial, final_date=final),
            fixed_rate=None if floating else fixed_rate,
            index=convention.rub_index if floating else None,
            spread=rub_spread if floating else 0.0,
        )

    rub_side: LegSide = "pay" if foreign_side == "receive" else "receive"
    return XccySwap(
        trade_id,
        rub_leg=leg("RUB", rub, rub_side, True),
        foreign_leg=leg(_currency(currency), foreign, foreign_side, False),
    )


# --- Leg flows ----------------------------------------------------------------------


@dataclass(frozen=True)
class _Flow:
    """``amount = const + coef * (known * DF_p(start) / DF_p(end) - 1)`` (signed, leg ccy).

    A fixed flow has ``coef = 0``; a floating coupon's projection runs over
    ``[proj_start, proj_end)`` (absent once every observation is known).
    """

    kind: FlowKind
    payment_date: date
    period: XccyPeriod | None
    source: FlowSource
    const: float
    coef: float
    known: float
    proj_start: date | None
    proj_end: date | None


def _known_growth(
    period: XccyPeriod, index: CompoundedIndex, fixings: Fixings
) -> tuple[float, date]:
    """Realized growth of the observations before the valuation date, and where projection
    starts. Each accrual day takes the fixing of the latest business day on or before it
    (fixing offset 0B, no lookback)."""
    calendar = fixings.calendar
    weight = index.weight_days(period.accrual_start, period.accrual_end)
    cursor = period.accrual_start
    growth = 1.0
    while cursor < period.accrual_end:
        observation = cursor
        while not calendar.is_business_day(observation):
            observation -= timedelta(days=1)
        if observation >= fixings.valuation_date:
            break
        end = min(calendar.add_business_days(observation, 1), period.accrual_end)
        try:
            rate, _ = fixings.rate(observation)
        except LookupError as exc:
            raise MissingMarketData(str(exc)) from None
        growth *= 1.0 + rate * (end - cursor).days / weight
        cursor = end
    return growth, cursor


def _leg_flows(leg: XccyLeg, valuation: date, fixings: Fixings | None) -> tuple[_Flow, ...]:
    """Flows paid after ``valuation``; flows on or before it have settled."""
    sign = leg.sign
    rows: list[_Flow] = []
    for period in leg.periods:
        if period.payment_date <= valuation:
            continue
        alpha = leg.day_count.year_fraction(period.accrual_start, period.accrual_end)
        if period.known_amount is not None:
            rows.append(
                _Flow(
                    "coupon",
                    period.payment_date,
                    period,
                    "known",
                    sign * period.known_amount,
                    0.0,
                    1.0,
                    None,
                    None,
                )
            )
            continue
        if leg.fixed_rate is not None:
            rows.append(
                _Flow(
                    "coupon",
                    period.payment_date,
                    period,
                    "fixed",
                    sign * period.notional * leg.fixed_rate * alpha,
                    0.0,
                    1.0,
                    None,
                    None,
                )
            )
            continue
        index = cast(CompoundedIndex, leg.index)
        coef = (
            sign
            * period.notional
            * alpha
            * index.annualization(period.accrual_start, period.accrual_end)
        )
        const = sign * period.notional * leg.spread * alpha
        known, start = 1.0, period.accrual_start
        if period.accrual_start < valuation:
            if fixings is None:
                raise MissingMarketData(
                    f"the {leg.currency} period from {period.accrual_start.isoformat()} has "
                    "started; its fixing history is required"
                )
            known, start = _known_growth(period, index, fixings)
        if start >= period.accrual_end:
            rows.append(
                _Flow(
                    "coupon",
                    period.payment_date,
                    period,
                    "fixed_by_history",
                    const + coef * (known - 1.0),
                    0.0,
                    1.0,
                    None,
                    None,
                )
            )
        else:
            source: FlowSource = (
                "projected" if start == period.accrual_start else "partially_projected"
            )
            rows.append(
                _Flow(
                    "coupon",
                    period.payment_date,
                    period,
                    source,
                    const,
                    coef,
                    known,
                    start,
                    period.accrual_end,
                )
            )
    for exchange in leg.exchanges:
        if exchange.payment_date > valuation:
            rows.append(
                _Flow(
                    "exchange",
                    exchange.payment_date,
                    None,
                    "fixed",
                    sign * exchange.amount,
                    0.0,
                    1.0,
                    None,
                    None,
                )
            )
    return tuple(sorted(rows, key=lambda f: f.payment_date))


def _amount(flow: _Flow, projection: DiscountFactorCurve | None) -> float:
    if flow.proj_start is None or flow.proj_end is None:
        return flow.const
    assert projection is not None
    ratio = projection.discount_factor(flow.proj_start) / projection.discount_factor(flow.proj_end)
    return flow.const + flow.coef * (flow.known * ratio - 1.0)


def _rolled_amount(
    flow: _Flow,
    horizon: date,
    today: DiscountFactorCurve | None,
    rolled: DiscountFactorCurve | None,
) -> float:
    """Amount with today's forwards up to ``horizon`` and the rolled projection after."""
    if flow.proj_start is None or flow.proj_end is None:
        return flow.const
    assert today is not None and rolled is not None
    start, end = flow.proj_start, flow.proj_end
    middle = min(max(start, horizon), end)
    growth = flow.known
    if start < middle:
        growth *= today.discount_factor(start) / today.discount_factor(middle)
    if middle < end:
        growth *= rolled.discount_factor(middle) / rolled.discount_factor(end)
    return flow.const + flow.coef * (growth - 1.0)


def _leg_pv(
    flows: tuple[_Flow, ...], discount: DiscountFactorCurve, projection: DiscountFactorCurve | None
) -> float:
    return math.fsum(
        _amount(f, projection) * discount.discount_factor(f.payment_date) for f in flows
    )


# --- Bootstrap of the foreign curve from XCCY par quotes ------------------------------


@dataclass(frozen=True)
class XccyParQuote:
    """Par fixed rate (decimal) of the foreign leg of a standard swap against RUONIA flat,
    for a tenor code (``1W``..``5Y``), e.g. MOEX ``CNY-XCS-CNYRUO``."""

    tenor: str
    rate: float

    def __post_init__(self) -> None:
        tenor = parse_tenor(self.tenor)
        if tenor.unit in ("ON", "TN", "SN"):
            raise InvalidTerms(f"{tenor.code} is not a swap tenor")
        object.__setattr__(self, "tenor", tenor.code)
        object.__setattr__(self, "rate", finite_money(self.rate, "par rate"))


@dataclass(frozen=True)
class XccyCurveBootstrapRequest(QuoteCurve):
    """Inputs of :func:`bootstrap_xccy_curve`; the curve date is ``trade_date``.

    ``spot_rate`` (RUB per foreign unit, value date ``spot_value_date``) converts foreign
    flows; ``notional_fx_rate`` fixes the RUB notional of the par swaps (NCC central rate
    of the trade date for ``XCS CNYRUB``). ``short_end`` (for example a curve implied from
    FX swaps) supplies nodes before the first quote, as MOEX's hybrid curve takes FX swaps
    up to 9M; the quotes then start after its last node.

    FX engines (``fx_forwards``, ``fx_crosses``, ``fx_futures``) take it in place of a
    built foreign curve for risk in the quotes: rebuilt under every shock on their shocked
    RUB curve (discounting and projecting RUONIA), quotes held under RUB shocks and moved
    by the bucket hats at their end dates under foreign shocks, as ``XccyRiskEngine``.
    """

    quote_basis: ClassVar[str] = "xccy_quote"
    needs_rub: ClassVar[bool] = True

    trade_date: date
    currency: str
    spot_rate: float
    spot_value_date: date
    notional_fx_rate: float
    quotes: tuple[XccyParQuote, ...]
    convention: XccyQuoteConvention
    short_end: DiscountCurve | None = None
    curve_convention: CurveConvention = field(default_factory=CurveConvention)

    def __post_init__(self) -> None:
        _date(self.trade_date, "trade_date")
        _date(self.spot_value_date, "spot_value_date")
        if _currency(self.currency) == "RUB":
            raise InvalidTerms("the bootstrapped curve is of a foreign currency")
        if self.spot_value_date < self.trade_date:
            raise InvalidTerms("spot value date cannot predate the trade date")
        object.__setattr__(self, "spot_rate", positive_money(self.spot_rate, "spot rate"))
        object.__setattr__(
            self, "notional_fx_rate", positive_money(self.notional_fx_rate, "notional FX rate")
        )
        quotes = tuple(self.quotes)
        if not quotes or not all(isinstance(q, XccyParQuote) for q in quotes):
            raise InvalidTerms("at least one XccyParQuote is required")
        if len({q.tenor for q in quotes}) != len(quotes):
            raise InvalidTerms("duplicate quote tenors")
        object.__setattr__(self, "quotes", quotes)
        if not isinstance(self.convention, XccyQuoteConvention):
            raise TypeError("convention must be an XccyQuoteConvention")
        if not isinstance(self.curve_convention, CurveConvention):
            raise TypeError("curve_convention must be a CurveConvention")
        if self.short_end is not None:
            if not isinstance(self.short_end, DiscountCurve) or not self.short_end.nodes:
                raise InvalidTerms("short_end must be a DiscountCurve with nodes")
            if self.short_end.curve_date != self.trade_date:
                raise InvalidTerms("short_end must be dated the trade date")
        _templates(self)  # resolves every quote's swap now: bad tenors fail here

    @property
    def curve_date(self) -> date:
        return self.trade_date

    def _quote_build(
        self, shifts: tuple[float, ...], rub: DiscountFactorCurve | None
    ) -> DiscountCurve:
        assert rub is not None
        if rub.curve_date != self.trade_date:
            raise InvalidTerms("RUB curves must be dated the trade date")
        return _bootstrap(self, rub, rub, _shocked_rates(self, shifts))

    def _quote_identity(self) -> object:
        return _foreign_identity(self)


@dataclass(frozen=True)
class _ParTemplate:
    quote: XccyParQuote
    node: date
    curve_time: float  # curve time of the quote's end date (bucket of its quote shift)
    rub_flows: tuple[_Flow, ...]  # per unit foreign notional
    foreign_dates: tuple[date, ...]
    foreign_fixed: tuple[float, ...]  # exchanges, signed
    foreign_unit: tuple[float, ...]  # coupon per unit rate, signed


def _templates(request: XccyCurveBootstrapRequest) -> tuple[_ParTemplate, ...]:
    cached = request.__dict__.get("_xccy_templates")
    if cached is not None:
        return cast(tuple[_ParTemplate, ...], cached)
    convention = request.convention
    rows = []
    for quote in request.quotes:
        swap = standard_xccy_swap(
            f"par-{quote.tenor}",
            request.trade_date,
            quote.tenor,
            convention,
            currency=request.currency,
            foreign_notional=1.0,
            notional_fx_rate=request.notional_fx_rate,
            fixed_rate=1.0,
            foreign_side="receive",
        )
        flows = _leg_flows(swap.foreign_leg, request.trade_date, None)
        end = swap.foreign_leg.periods[-1].accrual_end
        rows.append(
            _ParTemplate(
                quote,
                max(f.payment_date for f in flows),
                request.curve_convention.time_day_count.year_fraction(request.trade_date, end),
                _leg_flows(swap.rub_leg, request.trade_date, None),
                tuple(f.payment_date for f in flows),
                tuple(f.const if f.kind == "exchange" else 0.0 for f in flows),
                tuple(f.const if f.kind == "coupon" else 0.0 for f in flows),
            )
        )
    ordered = tuple(sorted(rows, key=lambda t: t.node))
    for left, right in pairwise(ordered):
        if right.node <= left.node:
            raise InvalidTerms(
                f"quotes {left.quote.tenor} and {right.quote.tenor} end on the same date"
            )
    object.__setattr__(request, "_xccy_templates", ordered)
    return ordered


def _par_value(
    template: _ParTemplate,
    rate: float,
    curve: DiscountFactorCurve,
    conversion: float,
    rub_pv: float,
) -> float:
    """PV in RUB, per unit foreign notional, of receiving the foreign leg at ``rate``."""
    foreign = math.fsum(
        (fixed + rate * unit) * curve.discount_factor(day)
        for day, fixed, unit in zip(
            template.foreign_dates, template.foreign_fixed, template.foreign_unit, strict=True
        )
    )
    return conversion * foreign + rub_pv


def _bootstrap(
    request: XccyCurveBootstrapRequest,
    rub_discount: DiscountFactorCurve,
    rub_projection: DiscountFactorCurve,
    rates: tuple[float, ...],
) -> DiscountCurve:
    templates = _templates(request)
    trade = request.trade_date
    spot_date = request.spot_value_date
    convention = request.curve_convention
    nodes: list[tuple[date, float]] = (
        [] if request.short_end is None else list(request.short_end.nodes)
    )
    if nodes and templates[0].node <= nodes[-1][0]:
        raise InvalidTerms("the first XCCY quote must end after the short end's last node")
    spot_rub = request.spot_rate * rub_discount.discount_factor(spot_date)
    for template, rate in zip(templates, rates, strict=True):
        rub_pv = _leg_pv(template.rub_flows, rub_discount, rub_projection)
        previous_date, previous_df = nodes[-1] if nodes else (trade, 1.0)
        interval = convention.time_day_count.year_fraction(previous_date, template.node)
        existing = tuple(nodes)

        def error(
            forward: float,
            template: _ParTemplate = template,
            rate: float = rate,
            rub_pv: float = rub_pv,
            previous_df: float = previous_df,
            interval: float = interval,
            existing: tuple[tuple[date, float], ...] = existing,
        ) -> float:
            exponent = max(-700.0, min(700.0, -forward * interval))
            candidate = DiscountCurve(
                trade,
                existing + ((template.node, previous_df * math.exp(exponent)),),
                XCCY_CURVE_MODEL_VERSION,
                convention=convention,
            )
            conversion = spot_rub / candidate.discount_factor(spot_date)
            return _par_value(template, rate, candidate, conversion, rub_pv)

        lower, upper = -0.5, 2.0
        for _ in range(12):
            low, high = error(lower), error(upper)
            if math.isfinite(low) and math.isfinite(high) and low * high <= 0.0:
                break
            lower, upper = lower * 2.0, upper * 2.0
        else:
            raise ValueError(f"XCCY bootstrap root for {template.quote.tenor} is not bracketed")
        root = bisect(error, lower, upper, tolerance=1e-15, label="XCCY bootstrap").root
        nodes.append((template.node, previous_df * math.exp(-root * interval)))
    curve = DiscountCurve(trade, tuple(nodes), XCCY_CURVE_MODEL_VERSION, convention=convention)
    conversion = spot_rub / curve.discount_factor(spot_date)
    for template, rate in zip(templates, rates, strict=True):
        rub_pv = _leg_pv(template.rub_flows, rub_discount, rub_projection)
        annuity = conversion * math.fsum(
            unit * curve.discount_factor(day)
            for day, unit in zip(template.foreign_dates, template.foreign_unit, strict=True)
        )
        value = _par_value(template, rate, curve, conversion, rub_pv)
        if annuity == 0.0 or abs(value / annuity) * 1e4 > _REPRICING_TOLERANCE_BP:
            raise RuntimeError("bootstrapped XCCY curve failed exact repricing")
    return curve


def bootstrap_xccy_curve(
    request: XccyCurveBootstrapRequest,
    *,
    rub_discount_curve: DiscountFactorCurve,
    rub_projection_curve: DiscountFactorCurve | None = None,
) -> DiscountCurve:
    """The foreign discount curve (RUB collateral) on which every par quote has zero PV.

    Each quote is a standard swap of ``request.convention`` (receive the foreign fixed
    rate, pay RUONIA flat, both notionals exchanged). Quotes are solved in order of their
    last foreign payment date, where each adds a node: a constant continuous forward from
    the previous node (the trade date, or the short end's last node), log-linear DF in
    between, as the OIS bootstrap. RUONIA is projected on ``rub_projection_curve``
    (default: the discount curve). Before the first node the curve is that forward; only
    ratios to the spot date enter the swaps. Every quote reprices within 1e-7 bp.
    """
    if not isinstance(request, XccyCurveBootstrapRequest):
        raise TypeError("request must be an XccyCurveBootstrapRequest")
    projection = rub_discount_curve if rub_projection_curve is None else rub_projection_curve
    for curve in (rub_discount_curve, projection):
        if curve.curve_date != request.trade_date:
            raise InvalidTerms("RUB curves must be dated the trade date")
    rates = tuple(t.quote.rate for t in _templates(request))
    return _bootstrap(request, rub_discount_curve, projection, rates)


def _shocked_rates(
    request: XccyCurveBootstrapRequest, shifts: tuple[float, ...]
) -> tuple[float, ...]:
    """Quotes moved by the bucket shift (bp) at each quote's end date, in node order."""
    return tuple(
        finite_money(t.quote.rate + _shift_at(t.curve_time, shifts) * 1e-4, "shocked quote")
        if any(shifts)
        else t.quote.rate
        for t in _templates(request)
    )


_ShiftKey = tuple[tuple[float, ...], tuple[float, ...], tuple[float, ...]]


class XccyCurveCache:
    """Memoize XCCY rebootstraps by request, RUB curve sources and shift vectors.

    Under risk shocks a bootstrapped foreign curve is rebuilt from shocked RUB curves
    and shifted quotes; every position on the same inputs shares the rebuilt curves.
    Deterministic, so cached and fresh builds are identical. Beyond ``max_entries``
    curves are built but not stored.
    """

    def __init__(self, max_entries: int = _CACHE_SIZE) -> None:
        if isinstance(max_entries, bool) or not isinstance(max_entries, int) or max_entries < 0:
            raise ValueError("max_entries must be a nonnegative integer")
        self.max_entries = max_entries
        self.hits = 0
        self.misses = 0
        self._size = 0
        self._curves: dict[tuple[object, ...], dict[_ShiftKey, DiscountCurve]] = {}
        self._last: tuple[tuple[object, ...], dict[_ShiftKey, DiscountCurve]] | None = None

    def __len__(self) -> int:
        return self._size

    def _group(self, sources: tuple[object, ...]) -> dict[_ShiftKey, DiscountCurve]:
        last = self._last
        if last is not None and all(a is b for a, b in zip(last[0], sources, strict=True)):
            return last[1]
        group = self._curves.setdefault(sources, {})
        self._last = (sources, group)
        return group

    def get(
        self, sources: tuple[object, ...], key: _ShiftKey, build: Callable[[], DiscountCurve]
    ) -> DiscountCurve:
        group = self._group(sources)
        cached = group.get(key)
        if cached is not None:
            self.hits += 1
            return cached
        self.misses += 1
        curve = build()
        if self._size < self.max_entries:
            group[key] = curve
            self._size += 1
        return curve


# --- Risk request, scenarios and results ---------------------------------------------


RubCurve = DiscountCurve | CurveBootstrapRequest
ForeignCurve = DiscountCurve | XccyCurveBootstrapRequest | QuoteCurve


@dataclass(frozen=True)
class XccyRiskRequest:
    """One swap and its market. All curves share one date, the valuation date.

    - ``spot_rate`` is RUB per one foreign unit for value date ``spot_value_date``.
    - ``rub_discount_curve`` discounts RUB flows; ``rub_projection_curve`` projects
      RUONIA (``None``: the discount curve, one set of factors). Each is a
      ``DiscountCurve`` (``continuous_zero`` risk) or OIS quotes (``ois_quote``).
    - ``foreign_discount_curve`` is the foreign curve under RUB collateral: a
      ``DiscountCurve`` (zero-rate overlay) or an :class:`XccyCurveBootstrapRequest`
      (``xccy_quote``: rebuilt from the shocked RUB curves and shifted quotes; quotes,
      spot and spot date held). Its spot must be the request's.
    - ``foreign_projection_curve`` projects a floating foreign leg (required then).
    - ``basis_bp``: continuous basis on the foreign flows from the spot date, as the FX
      forward basis (``fx_forwards``); 0 on an implied or bootstrapped foreign curve.
    - ``rub_fixings`` / ``foreign_fixings``: point-in-time fixings, required once a
      floating period has started; dated the valuation date.
    """

    swap: XccySwap
    spot_rate: float
    spot_value_date: date
    rub_discount_curve: RubCurve
    foreign_discount_curve: ForeignCurve
    basis_bp: float
    rub_projection_curve: RubCurve | None = None
    foreign_projection_curve: RubCurve | None = None
    rub_fixings: FixingHistory | None = None
    foreign_fixings: IndexFixings | None = None
    basis_day_count: DayCount = DayCount.ACT_365_FIXED

    def __post_init__(self) -> None:
        swap = self.swap
        if not isinstance(swap, XccySwap):
            raise TypeError("swap must be an XccySwap")
        _date(self.spot_value_date, "spot_value_date")
        for name in ("rub_discount_curve", "rub_projection_curve", "foreign_projection_curve"):
            value = getattr(self, name)
            if value is not None and not isinstance(value, (DiscountCurve, CurveBootstrapRequest)):
                raise TypeError(f"{name} must be DiscountCurve or CurveBootstrapRequest")
        if self.rub_discount_curve is None:
            raise TypeError("rub_discount_curve is required")
        foreign = self.foreign_discount_curve
        if not isinstance(foreign, (DiscountCurve, QuoteCurve)):
            raise TypeError(
                "foreign_discount_curve must be DiscountCurve or XccyCurveBootstrapRequest"
            )
        if not isinstance(self.basis_day_count, DayCount):
            raise TypeError("basis_day_count must be a DayCount")
        valuation = self.rub_discount_curve.curve_date
        curves = (self.rub_projection_curve, foreign, self.foreign_projection_curve)
        if any(c is not None and c.curve_date != valuation for c in curves):
            raise InvalidTerms("all curves must share one valuation date")
        if self.spot_value_date < valuation:
            raise InvalidTerms("spot value date cannot predate curve valuation")
        if swap.rub_leg.index is None and self.rub_projection_curve is not None:
            raise InvalidTerms("a fixed RUB leg takes no projection curve")
        if swap.foreign_leg.index is None:
            if self.foreign_projection_curve is not None:
                raise InvalidTerms("a fixed foreign leg takes no projection curve")
        elif self.foreign_projection_curve is None:
            raise MissingMarketData("a floating foreign leg needs foreign_projection_curve")
        if isinstance(foreign, XccyCurveBootstrapRequest) and foreign.currency != swap.currency:
            raise InvalidTerms("the bootstrapped curve is of another currency")
        object.__setattr__(self, "spot_rate", positive_money(self.spot_rate, "spot rate"))
        object.__setattr__(self, "basis_bp", finite_money(self.basis_bp, "basis bp"))
        if isinstance(foreign, XccyCurveBootstrapRequest) and (
            foreign.spot_rate != self.spot_rate or foreign.spot_value_date != self.spot_value_date
        ):
            raise InvalidTerms("the bootstrapped curve's spot must be the request's spot")
        if self.rub_fixings is not None:
            if not isinstance(self.rub_fixings, FixingHistory):
                raise TypeError("rub_fixings must be a FixingHistory")
            if self.rub_fixings.index != "ruonia":
                raise InvalidTerms("rub_fixings must be a RUONIA history")
            if self.rub_fixings.valuation_date != valuation:
                raise InvalidTerms("rub_fixings must be dated the valuation date")
        if self.foreign_fixings is not None:
            if not isinstance(self.foreign_fixings, IndexFixings):
                raise TypeError("foreign_fixings must be IndexFixings")
            index = swap.foreign_leg.index
            if index is None or self.foreign_fixings.index != index.name:
                raise InvalidTerms("foreign_fixings are not of the foreign leg's index")
            if self.foreign_fixings.valuation_date != valuation:
                raise InvalidTerms("foreign_fixings must be dated the valuation date")
        for name in ("rub_discount_curve", "rub_projection_curve", "foreign_projection_curve"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _snapshot_curve(value))
        if isinstance(foreign, DiscountCurve):
            object.__setattr__(self, "foreign_discount_curve", _snapshot_curve(foreign))

    @property
    def valuation_date(self) -> date:
        return self.rub_discount_curve.curve_date


def _vector(values: Sequence[float], label: str) -> tuple[float, ...]:
    try:
        return _shifts(tuple(values))
    except ValueError as exc:
        raise InvalidTerms(f"{label}: {exc}") from None


@dataclass(frozen=True)
class XccyScenario:
    """Absolute bp bucket shifts, a basis shift (bp) and a relative spot move (percent).

    ``rub_shifts_bp`` moves the RUB discount curve and, unless
    ``rub_projection_shifts_bp`` is given, a separate RUB projection curve too; likewise
    for the foreign curves. A shift for a curve the swap does not have is rejected.
    """

    rub_shifts_bp: tuple[float, ...] = _ZERO_SHOCK
    foreign_shifts_bp: tuple[float, ...] = _ZERO_SHOCK
    basis_shift_bp: float = 0.0
    fx_spot_shift_pct: float = 0.0
    rub_projection_shifts_bp: tuple[float, ...] | None = None
    foreign_projection_shifts_bp: tuple[float, ...] | None = None

    def __post_init__(self) -> None:
        for name in ("rub_shifts_bp", "foreign_shifts_bp"):
            object.__setattr__(self, name, _vector(getattr(self, name), name))
        for name in ("rub_projection_shifts_bp", "foreign_projection_shifts_bp"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _vector(value, name))
        object.__setattr__(self, "basis_shift_bp", finite_money(self.basis_shift_bp, "basis"))
        spot = finite_money(self.fx_spot_shift_pct, "FX spot shift")
        if spot <= -100.0:
            raise InvalidTerms("an FX spot shift must keep the rate positive")
        object.__setattr__(self, "fx_spot_shift_pct", spot)

    @classmethod
    def parallel(
        cls,
        *,
        rub_shift_bp: float = 0.0,
        foreign_shift_bp: float = 0.0,
        basis_shift_bp: float = 0.0,
        fx_spot_shift_pct: float = 0.0,
    ) -> XccyScenario:
        size = len(RISK_BUCKETS)
        return cls(
            (rub_shift_bp,) * size, (foreign_shift_bp,) * size, basis_shift_bp, fx_spot_shift_pct
        )

    @classmethod
    def from_fx_scenario(cls, scenario: FxFuturesScenario) -> XccyScenario:
        """The same moves as an FX futures / forward scenario: RUB, foreign, basis, spot."""
        if not isinstance(scenario, FxFuturesScenario):
            raise TypeError("scenario must be an FxFuturesScenario")
        return cls(
            scenario.rub_curve_shifts_bp,
            scenario.foreign_curve_shifts_bp,
            scenario.basis_shift_bp,
            scenario.fx_spot_shift_pct,
        )


@dataclass(frozen=True)
class XccyBucketRisk:
    bucket: str
    dv01_rub: float


@dataclass(frozen=True)
class XccyCurveRisk:
    """RUB per bp per bucket of one curve role, their sum and a repriced parallel shift."""

    role: XccyCurveRole
    curve_basis: str
    buckets: tuple[XccyBucketRisk, ...]
    dv01_rub: float
    parallel_dv01_rub: float


@dataclass(frozen=True)
class XccyCashflow:
    """One remaining flow; ``amount`` signed for the position in the leg currency.

    ``rate`` is the period's fixed rate, or the floating rate implied by the amount
    (index rate plus spread); ``None`` for exchanges and known amounts.
    ``discount_factor`` is the leg's discount factor at the payment date (foreign: times
    the basis growth); ``pv_rub`` is the flow's RUB value.
    """

    leg: LegName
    kind: FlowKind
    currency: str
    accrual_start: date | None
    accrual_end: date | None
    payment_date: date
    notional: float | None
    rate: float | None
    amount: float
    source: FlowSource
    discount_factor: float
    pv_rub: float


@dataclass(frozen=True)
class XccyRiskResult:
    """Signed RUB value and sensitivities.

    ``fx_today`` is ``S * DF_rub(s) / DF_for(s)``, the rate that converts the foreign
    leg's value today (``foreign_leg_pv``, foreign currency) into RUB.
    ``fx_delta_rub_per_pct`` is the exact PV change for a +1 % spot move: the foreign
    leg's RUB value / 100 (curves held).
    """

    base_id: str
    trade_id: str
    currency: str
    difference: str
    bump_bp: float
    spot_rate: float
    spot_value_date: date
    basis_bp: float
    fx_today: float
    pv_rub: float
    rub_leg_pv_rub: float
    foreign_leg_pv: float
    foreign_leg_pv_rub: float
    fx_delta_rub_per_pct: float
    curves: tuple[XccyCurveRisk, ...]
    dv01_rub: float
    basis01_rub: float
    model_version: str = XCCY_MODEL_VERSION

    def curve(self, role: XccyCurveRole) -> XccyCurveRisk:
        for item in self.curves:
            if item.role == role:
                return item
        raise InvalidTerms(f"this swap has no {role} curve")


@dataclass(frozen=True)
class XccyScenarioResult:
    base_id: str
    scenario_id: str
    base_pv_rub: float
    shocked_pv_rub: float
    rub_rates_pnl_rub: float
    foreign_rates_pnl_rub: float
    basis_pnl_rub: float
    fx_spot_pnl_rub: float
    interaction_pnl_rub: float
    total_pnl_rub: float
    model_version: str = XCCY_MODEL_VERSION


@dataclass(frozen=True)
class XccySecondOrderRisk:
    """Central gradient and full symmetric Hessian: 11 buckets per curve role, the basis
    (bp) and the spot (1 % relative move, exact: its diagonal is zero and its cross terms
    are 1 % of the central rate derivative of the foreign leg's RUB value)."""

    base_id: str
    roles: tuple[XccyCurveRole, ...]
    factors: tuple[str, ...]
    bump_bp: float
    gradient_rub: tuple[float, ...]
    hessian_rub: tuple[tuple[float, ...], ...]
    model_version: str = XCCY_SECOND_ORDER_MODEL_VERSION


@dataclass(frozen=True)
class XccyHorizonValuation:
    """Carry and rolldown from the valuation date to ``horizon_date``, in RUB.

    - ``received_rub``: flows paid in ``(valuation date, horizon]``, as cash
      (undiscounted, not reinvested); floating amounts are today's projections and
      foreign flows convert at ``spot_rate`` (``spot_held``) or at today's outright for
      their payment date (``forwards_realised``).
    - ``pv_forward_rub``: the remaining flows at the horizon on today's curves read
      forward (``DF(t) / DF(horizon)``), floating amounts on today's projections.
    - ``pv_rolled_rub``: the remaining flows at the horizon on rolled curves (shape held
      in calendar days); observations before the horizon keep today's forwards.
    - Remaining foreign flows convert at ``horizon_spot_rate`` for value date
      ``horizon_spot_value_date``, with the basis held in bp.

    ``carry = pv_forward + received - pv_start``, ``rolldown = pv_rolled - pv_forward``
    and ``horizon_pnl = pv_rolled + received - pv_start``.
    """

    base_id: str
    trade_id: str
    currency: str
    valuation_date: date
    horizon_date: date
    fx_horizon: FxHorizon
    spot_rate: float
    spot_value_date: date
    horizon_spot_rate: float
    horizon_spot_value_date: date
    pv_start_rub: float
    pv_forward_rub: float
    pv_rolled_rub: float
    received_rub: float
    carry_rub: float
    rolldown_rub: float
    horizon_pnl_rub: float
    model_version: str = XCCY_HORIZON_MODEL_VERSION


# --- Engine -------------------------------------------------------------------------


_Shifts = tuple[tuple[float, ...], ...]  # one 11-bucket vector per curve role


@dataclass(frozen=True)
class _Moves:
    shifts: _Shifts
    basis: float = 0.0
    spot: float = 0.0


def _fixings_identity(fixings: Fixings | None) -> _Canonical | None:
    if fixings is None:
        return None
    cached = fixings.__dict__.get("_xccy_identity")
    if cached is None:
        cached = _precanonical(fixings.identity)
        object.__setattr__(fixings, "_xccy_identity", cached)
    return cast(_Canonical, cached)


def _foreign_identity(curve: ForeignCurve) -> _Canonical:
    if not isinstance(curve, XccyCurveBootstrapRequest):
        return _curve_identity(curve)
    cached = curve.__dict__.get("_xccy_identity")
    if cached is None:
        cached = _precanonical(
            {
                "trade_date": curve.trade_date,
                "currency": curve.currency,
                "spot_rate": curve.spot_rate,
                "spot_value_date": curve.spot_value_date,
                "notional_fx_rate": curve.notional_fx_rate,
                "quotes": [(q.tenor, q.rate) for q in curve.quotes],
                "convention": curve.convention.identifier,
                "short_end": None if curve.short_end is None else _curve_identity(curve.short_end),
                "curve_convention": curve.curve_convention.identifier,
            }
        )
        object.__setattr__(curve, "_xccy_identity", cached)
    return cast(_Canonical, cached)


def _role_basis(curve: RubCurve | ForeignCurve) -> str:
    if isinstance(curve, XccyCurveBootstrapRequest):
        return "xccy_quote"
    return _curve_basis(curve)


@dataclass(frozen=True)
class XccyRiskEngine:
    """Value a cross-currency swap and shock its curves, basis and spot.

    ``PV = A + B`` with the foreign leg's RUB value ``A`` (linear in spot) and the RUB
    leg's value ``B``. Share ``curve_cache`` (OIS rebootstraps) and ``xccy_cache``
    (foreign curve rebootstraps) across engines on the same curves.
    """

    request: XccyRiskRequest
    curve_cache: ShockedCurveCache = field(
        default_factory=ShockedCurveCache, compare=False, repr=False
    )
    xccy_cache: XccyCurveCache = field(default_factory=XccyCurveCache, compare=False, repr=False)
    base_pv_rub: float = field(init=False)
    base_id: str = field(init=False)
    roles: tuple[XccyCurveRole, ...] = field(init=False)
    _bases: dict[str, DiscountCurve] = field(init=False, repr=False)
    _rub_flows: tuple[_Flow, ...] = field(init=False, repr=False)
    _foreign_flows: tuple[_Flow, ...] = field(init=False, repr=False)
    _foreign_tau: tuple[float, ...] = field(init=False, repr=False)
    _base_values: tuple[float, float] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        req = self.request
        if not isinstance(req, XccyRiskRequest):
            raise TypeError("request must be an XccyRiskRequest")
        if not isinstance(self.curve_cache, ShockedCurveCache):
            raise TypeError("curve_cache must be a ShockedCurveCache")
        if not isinstance(self.xccy_cache, XccyCurveCache):
            raise TypeError("xccy_cache must be an XccyCurveCache")
        roles: list[XccyCurveRole] = ["rub_discount"]
        if req.rub_projection_curve is not None:
            roles.append("rub_projection")
        roles.append("foreign_discount")
        if req.foreign_projection_curve is not None:
            roles.append("foreign_projection")
        object.__setattr__(self, "roles", tuple(roles))
        bases: dict[str, DiscountCurve] = {
            "rub_discount": _base_curve(req.rub_discount_curve, self.curve_cache)
        }
        if req.rub_projection_curve is not None:
            bases["rub_projection"] = _base_curve(req.rub_projection_curve, self.curve_cache)
        if req.foreign_projection_curve is not None:
            bases["foreign_projection"] = _base_curve(
                req.foreign_projection_curve, self.curve_cache
            )
        object.__setattr__(self, "_bases", bases)
        valuation = req.valuation_date
        swap = req.swap
        object.__setattr__(self, "_rub_flows", _leg_flows(swap.rub_leg, valuation, req.rub_fixings))
        foreign_flows = _leg_flows(swap.foreign_leg, valuation, req.foreign_fixings)
        if not foreign_flows and not self._rub_flows:
            raise InvalidTerms(f"{swap.trade_id}: every flow has settled; nothing to value")
        object.__setattr__(self, "_foreign_flows", foreign_flows)
        object.__setattr__(
            self,
            "_foreign_tau",
            tuple(
                _signed_years(req.basis_day_count, req.spot_value_date, f.payment_date)
                for f in foreign_flows
            ),
        )
        base_curve = self._foreign_discount(tuple(_ZERO_SHOCK for _ in roles))
        bases["foreign_discount"] = cast(DiscountCurve, base_curve)
        values = self._values(_Moves(tuple(_ZERO_SHOCK for _ in roles)))
        object.__setattr__(self, "_base_values", values)
        object.__setattr__(self, "base_pv_rub", finite_money(values[0] + values[1], "PV"))
        object.__setattr__(
            self,
            "base_id",
            canonical_sha256(
                {
                    "model": XCCY_MODEL_VERSION,
                    "swap": swap,
                    "spot_rate": req.spot_rate,
                    "spot_value_date": req.spot_value_date,
                    "rub_discount": (
                        _role_basis(req.rub_discount_curve),
                        _curve_identity(req.rub_discount_curve),
                    ),
                    "rub_projection": None
                    if req.rub_projection_curve is None
                    else (
                        _role_basis(req.rub_projection_curve),
                        _curve_identity(req.rub_projection_curve),
                    ),
                    "foreign_discount": (
                        _role_basis(req.foreign_discount_curve),
                        _foreign_identity(req.foreign_discount_curve),
                    ),
                    "foreign_projection": None
                    if req.foreign_projection_curve is None
                    else (
                        _role_basis(req.foreign_projection_curve),
                        _curve_identity(req.foreign_projection_curve),
                    ),
                    "basis_bp": req.basis_bp,
                    "basis_day_count": req.basis_day_count.value,
                    "rub_fixings": _fixings_identity(req.rub_fixings),
                    "foreign_fixings": _fixings_identity(req.foreign_fixings),
                }
            ),
        )

    # --- curves ------------------------------------------------------------------

    def curve_basis(self, role: XccyCurveRole) -> str:
        req = self.request
        source = {
            "rub_discount": req.rub_discount_curve,
            "rub_projection": req.rub_projection_curve,
            "foreign_discount": req.foreign_discount_curve,
            "foreign_projection": req.foreign_projection_curve,
        }[role]
        if source is None:
            raise InvalidTerms(f"this swap has no {role} curve")
        return _role_basis(source)

    def _shift(self, shifts: _Shifts, role: XccyCurveRole) -> tuple[float, ...]:
        return shifts[self.roles.index(role)] if role in self.roles else _ZERO_SHOCK

    def _rub_curves(self, shifts: _Shifts) -> tuple[DiscountFactorCurve, DiscountFactorCurve]:
        req = self.request
        discount = _shock_curve(
            req.rub_discount_curve,
            self._bases["rub_discount"],
            self._shift(shifts, "rub_discount"),
            self.curve_cache,
        )
        if req.rub_projection_curve is None:
            return discount, discount
        projection = _shock_curve(
            req.rub_projection_curve,
            self._bases["rub_projection"],
            self._shift(shifts, "rub_projection"),
            self.curve_cache,
        )
        return discount, projection

    def _foreign_discount(self, shifts: _Shifts) -> DiscountFactorCurve:
        req = self.request
        source = req.foreign_discount_curve
        own = self._shift(shifts, "foreign_discount")
        if not isinstance(source, XccyCurveBootstrapRequest):  # built, or par-yield quotes
            base = _base_curve(source, self.curve_cache)
            return _shock_curve(source, base, own, self.curve_cache)
        rub_d = self._shift(shifts, "rub_discount")
        rub_p = self._shift(shifts, "rub_projection")
        sources = (source, req.rub_discount_curve, req.rub_projection_curve)

        def build() -> DiscountCurve:
            discount, projection = self._rub_curves(shifts)
            return _bootstrap(source, discount, projection, _shocked_rates(source, own))

        return self.xccy_cache.get(sources, (rub_d, rub_p, own), build)

    def _foreign_projection(self, shifts: _Shifts) -> DiscountFactorCurve | None:
        source = self.request.foreign_projection_curve
        if source is None:
            return None
        return _shock_curve(
            source,
            self._bases["foreign_projection"],
            self._shift(shifts, "foreign_projection"),
            self.curve_cache,
        )

    # --- pricing -----------------------------------------------------------------

    def _values(self, moves: _Moves) -> tuple[float, float]:
        """(foreign leg's RUB value with the spot move, RUB leg's value)."""
        req = self.request
        try:
            rub_d, rub_p = self._rub_curves(moves.shifts)
            foreign_d = self._foreign_discount(moves.shifts)
            foreign_p = self._foreign_projection(moves.shifts)
            rub = _leg_pv(self._rub_flows, rub_d, rub_p)
            basis = finite_money(req.basis_bp + moves.basis, "shocked basis") * 1e-4
            spot_date = req.spot_value_date
            spot = req.spot_rate * (1.0 + moves.spot / 100.0)
            conversion = (
                spot * rub_d.discount_factor(spot_date) / foreign_d.discount_factor(spot_date)
            )
            foreign = math.fsum(
                _amount(f, foreign_p)
                * foreign_d.discount_factor(f.payment_date)
                * math.exp(basis * tau)
                for f, tau in zip(self._foreign_flows, self._foreign_tau, strict=True)
            )
            return (
                finite_money(conversion * foreign, "foreign leg PV"),
                finite_money(rub, "RUB leg PV"),
            )
        except (OverflowError, ZeroDivisionError) as exc:
            raise ValueError("shock is outside the supported numerical domain") from exc

    def _pv(self, moves: _Moves) -> float:
        foreign, rub = self._values(moves)
        return foreign + rub

    def _zero(self) -> _Shifts:
        return tuple(_ZERO_SHOCK for _ in self.roles)

    # --- results -----------------------------------------------------------------

    @property
    def fx_today(self) -> float:
        req = self.request
        return (
            req.spot_rate
            * self._bases["rub_discount"].discount_factor(req.spot_value_date)
            / self._bases["foreign_discount"].discount_factor(req.spot_value_date)
        )

    def cashflows(self) -> tuple[XccyCashflow, ...]:
        """The remaining flows of both legs on the base curves (independently checkable)."""
        req = self.request
        rub_d, rub_p = self._rub_curves(self._zero())
        foreign_d = self._bases["foreign_discount"]
        foreign_p = self._foreign_projection(self._zero())
        basis = req.basis_bp * 1e-4
        fx_today = self.fx_today
        rows = []
        for name, leg, flows, discount, projection in (
            ("rub", req.swap.rub_leg, self._rub_flows, rub_d, rub_p),
            ("foreign", req.swap.foreign_leg, self._foreign_flows, foreign_d, foreign_p),
        ):
            for i, flow in enumerate(flows):
                amount = _amount(flow, projection)
                df = discount.discount_factor(flow.payment_date)
                if name == "foreign":
                    df *= math.exp(basis * self._foreign_tau[i])
                period = flow.period
                rate: float | None = None
                if period is not None and flow.source != "known":
                    alpha = leg.day_count.year_fraction(period.accrual_start, period.accrual_end)
                    rate = leg.sign * amount / (period.notional * alpha)
                rows.append(
                    XccyCashflow(
                        cast(LegName, name),
                        flow.kind,
                        leg.currency,
                        None if period is None else period.accrual_start,
                        None if period is None else period.accrual_end,
                        flow.payment_date,
                        None if period is None else period.notional,
                        rate,
                        amount,
                        flow.source,
                        df,
                        amount * df * (fx_today if name == "foreign" else 1.0),
                    )
                )
        return tuple(rows)

    def risk(
        self, *, bump_bp: float = 1.0, difference: Literal["forward", "central"] = "central"
    ) -> XccyRiskResult:
        """Bucket and parallel DV01 per curve role, basis01 and the exact FX delta.

        Central by default (DEC08: desk factors need it); forward gives the exact
        +bump PV change.
        """
        bump = positive_money(bump_bp, "risk bump bp")
        if difference not in ("forward", "central"):
            raise ValueError("difference must be forward or central")
        size = len(RISK_BUCKETS)
        zero = self._zero()

        def with_role(position: int, vector: tuple[float, ...]) -> _Shifts:
            return tuple(vector if k == position else _ZERO_SHOCK for k in range(len(self.roles)))

        def sensitivity(moves: _Moves) -> float:
            up = self._pv(moves)
            if difference == "central":
                down = self._pv(
                    _Moves(tuple(tuple(-s for s in v) for v in moves.shifts), -moves.basis)
                )
                return finite_money((up - down) / (2.0 * bump), "sensitivity")
            return finite_money((up - self.base_pv_rub) / bump, "sensitivity")

        curves = []
        for position, role in enumerate(self.roles):
            buckets = tuple(
                XccyBucketRisk(
                    bucket,
                    sensitivity(
                        _Moves(
                            with_role(position, tuple(bump if i == j else 0.0 for j in range(size)))
                        )
                    ),
                )
                for i, bucket in enumerate(RISK_BUCKETS)
            )
            curves.append(
                XccyCurveRisk(
                    role,
                    self.curve_basis(role),
                    buckets,
                    finite_money(math.fsum(b.dv01_rub for b in buckets), "curve DV01"),
                    sensitivity(_Moves(with_role(position, (bump,) * size))),
                )
            )
        req = self.request
        foreign_rub, rub_pv = self._base_values
        fx_today = self.fx_today
        return XccyRiskResult(
            base_id=self.base_id,
            trade_id=req.swap.trade_id,
            currency=req.swap.currency,
            difference=difference,
            bump_bp=bump,
            spot_rate=req.spot_rate,
            spot_value_date=req.spot_value_date,
            basis_bp=req.basis_bp,
            fx_today=fx_today,
            pv_rub=self.base_pv_rub,
            rub_leg_pv_rub=rub_pv,
            foreign_leg_pv=finite_money(foreign_rub / fx_today, "foreign leg PV"),
            foreign_leg_pv_rub=foreign_rub,
            fx_delta_rub_per_pct=finite_money(foreign_rub / 100.0, "FX delta"),
            curves=tuple(curves),
            dv01_rub=finite_money(math.fsum(c.dv01_rub for c in curves), "DV01"),
            basis01_rub=sensitivity(_Moves(zero, bump)),
        )

    def reprice(self, scenario: XccyScenario) -> XccyScenarioResult:
        """Exact PV change; RUB rates, foreign rates, basis and spot isolated; interaction."""
        if not isinstance(scenario, XccyScenario):
            raise TypeError("scenario must be an XccyScenario")
        roles = self.roles
        zero = _ZERO_SHOCK

        def vector(role: XccyCurveRole) -> tuple[float, ...]:
            if role == "rub_discount":
                return scenario.rub_shifts_bp
            if role == "foreign_discount":
                return scenario.foreign_shifts_bp
            if role == "rub_projection":
                own = scenario.rub_projection_shifts_bp
                return scenario.rub_shifts_bp if own is None else own
            own = scenario.foreign_projection_shifts_bp
            return scenario.foreign_shifts_bp if own is None else own

        if "rub_projection" not in roles and scenario.rub_projection_shifts_bp is not None:
            raise InvalidTerms("this swap has no separate RUB projection curve")
        if "foreign_projection" not in roles and scenario.foreign_projection_shifts_bp is not None:
            raise InvalidTerms("this swap has no foreign projection curve")
        full = tuple(vector(r) for r in roles)
        rub_only = tuple(vector(r) if r.startswith("rub") else zero for r in roles)
        foreign_only = tuple(vector(r) if r.startswith("foreign") else zero for r in roles)
        none = self._zero()
        base = self.base_pv_rub
        shocked = finite_money(
            self._pv(_Moves(full, scenario.basis_shift_bp, scenario.fx_spot_shift_pct)), "PV"
        )

        def pnl(moves: _Moves, label: str) -> float:
            return finite_money(self._pv(moves) - base, label)

        rub = pnl(_Moves(rub_only), "RUB rates P&L")
        foreign = pnl(_Moves(foreign_only), "foreign rates P&L")
        basis = pnl(_Moves(none, scenario.basis_shift_bp), "basis P&L")
        spot = pnl(_Moves(none, 0.0, scenario.fx_spot_shift_pct), "FX spot P&L")
        total = finite_money(shocked - base, "total P&L")
        return XccyScenarioResult(
            self.base_id,
            canonical_sha256(scenario),
            base,
            shocked,
            rub,
            foreign,
            basis,
            spot,
            finite_money(total - math.fsum((rub, foreign, basis, spot)), "interaction P&L"),
            total,
        )

    def second_order(self, *, bump_bp: float = 1.0) -> XccySecondOrderRisk:
        """Central gradient and Hessian over every role's buckets, the basis and the spot."""
        bump = positive_money(bump_bp, "second-order bump bp")
        size = len(RISK_BUCKETS)
        count = len(self.roles)
        rates = size * count + 1

        def moves_of(vector: list[float]) -> _Moves:
            shifts = tuple(tuple(vector[k * size : (k + 1) * size]) for k in range(count))
            return _Moves(shifts, vector[-1])

        def values(vector: list[float]) -> tuple[float, float]:
            return self._values(moves_of(vector))

        # The formulas of the shared central-difference helper; a factor whose single
        # bumps leave both leg values bit-identical is skipped (its entries are zeros).
        square = positive_money(bump * bump, "squared second-order bump")
        base = self._base_values
        base_pv = self.base_pv_rub
        gradient = [0.0] * rates
        hessian = [[0.0] * (rates + 1) for _ in range(rates + 1)]
        active = []
        for i in range(rates):
            vector = [0.0] * rates
            vector[i] = bump
            up = values(vector)
            vector[i] = -bump
            down = values(vector)
            if up == base and down == base:
                continue
            active.append(i)
            up_pv, down_pv = up[0] + up[1], down[0] + down[1]
            gradient[i] = finite_money((up_pv - down_pv) / (2 * bump), "gradient")
            hessian[i][i] = finite_money(
                ((up_pv - base_pv) + (down_pv - base_pv)) / square, "diagonal gamma"
            )
            cross = finite_money((up[0] - down[0]) / (2 * bump) / 100.0, "FX cross gamma")
            hessian[i][rates] = hessian[rates][i] = cross
        for position, i in enumerate(active):
            for j in active[:position]:
                corners = []
                for sign_i, sign_j in ((1, 1), (1, -1), (-1, 1), (-1, -1)):
                    pair = [0.0] * rates
                    pair[i], pair[j] = sign_i * bump, sign_j * bump
                    foreign, rub = values(pair)
                    corners.append(foreign + rub)
                mixed = finite_money(
                    ((corners[0] - corners[1]) - (corners[2] - corners[3])) / (4 * square),
                    "cross gamma",
                )
                hessian[i][j] = hessian[j][i] = mixed
        currency = self.request.swap.currency
        return XccySecondOrderRisk(
            base_id=self.base_id,
            roles=self.roles,
            factors=(
                *(f"{role} {b}" for role in self.roles for b in RISK_BUCKETS),
                f"{currency} basis",
                f"{currency} spot",
            ),
            bump_bp=bump,
            gradient_rub=(*gradient, finite_money(base[0] / 100.0, "FX delta")),
            hessian_rub=tuple(tuple(row) for row in hessian),
        )

    # --- horizon -----------------------------------------------------------------

    def horizon(
        self,
        horizon_date: date,
        *,
        horizon_spot_value_date: date,
        fx_horizon: FxHorizon = "spot_held",
    ) -> XccyHorizonValuation:
        """Carry and rolldown to ``horizon_date`` (see :class:`XccyHorizonValuation`).

        The spot at the horizon follows ``fx_horizon``: today's spot (``spot_held``) or
        today's outright for ``horizon_spot_value_date`` (``forwards_realised``). Base
        curves only; a bootstrapped foreign curve is read forward and rolled as built
        (its quotes are not rebootstrapped at the horizon).
        """
        req = self.request
        valuation = req.valuation_date
        if not isinstance(horizon_date, date) or horizon_date <= valuation:
            raise InvalidTerms("horizon date must be after the valuation date")
        spot_h = horizon_spot_value_date
        if not isinstance(spot_h, date) or spot_h < horizon_date:
            raise InvalidTerms("horizon spot value date cannot predate the horizon date")
        if spot_h < req.spot_value_date:
            raise InvalidTerms("horizon spot value date cannot predate the spot value date")
        rule = _check_fx_horizon(fx_horizon)
        rub_d = self._bases["rub_discount"]
        rub_p = self._bases.get("rub_projection", rub_d)
        foreign_d = self._bases["foreign_discount"]
        foreign_p = self._bases.get("foreign_projection")
        basis, day_count = req.basis_bp * 1e-4, req.basis_day_count
        spot, spot_date = req.spot_rate, req.spot_value_date

        def outright(day: date) -> float:
            """Today's forward outright for ``day`` (covered parity with the basis)."""
            return (
                spot
                * (foreign_d.discount_factor(day) / foreign_d.discount_factor(spot_date))
                / (rub_d.discount_factor(day) / rub_d.discount_factor(spot_date))
                * math.exp(basis * _signed_years(day_count, spot_date, day))
            )

        spot_at_h = spot if rule == "spot_held" else outright(spot_h)
        rub_left = tuple(f for f in self._rub_flows if f.payment_date > horizon_date)
        foreign_left = tuple(f for f in self._foreign_flows if f.payment_date > horizon_date)
        received = math.fsum(
            (
                *(_amount(f, rub_p) for f in self._rub_flows if f.payment_date <= horizon_date),
                *(
                    _amount(f, foreign_p)
                    * (spot if rule == "spot_held" else outright(f.payment_date))
                    for f in self._foreign_flows
                    if f.payment_date <= horizon_date
                ),
            )
        )

        def at_horizon(
            rub_amounts: Sequence[float],
            foreign_amounts: Sequence[float],
            rub_df: Callable[[date], float],
            foreign_df: Callable[[date], float],
        ) -> float:
            """Remaining flows' RUB value at the horizon on one view of the curves."""
            rub = math.fsum(
                a * rub_df(f.payment_date) for f, a in zip(rub_left, rub_amounts, strict=True)
            )
            foreign = math.fsum(
                a
                * foreign_df(f.payment_date)
                * math.exp(basis * _signed_years(day_count, spot_h, f.payment_date))
                for f, a in zip(foreign_left, foreign_amounts, strict=True)
            )
            return rub + spot_at_h * rub_df(spot_h) / foreign_df(spot_h) * foreign

        rub_h = rub_d.discount_factor(horizon_date)
        foreign_h = foreign_d.discount_factor(horizon_date)
        pv_forward = at_horizon(
            tuple(_amount(f, rub_p) for f in rub_left),
            tuple(_amount(f, foreign_p) for f in foreign_left),
            lambda t: rub_d.discount_factor(t) / rub_h,
            lambda t: foreign_d.discount_factor(t) / foreign_h,
        )
        rolled_rub_d = roll_curve(rub_d, horizon_date)
        rolled_rub_p = rolled_rub_d if rub_p is rub_d else roll_curve(rub_p, horizon_date)
        rolled_foreign_d = roll_curve(foreign_d, horizon_date)
        rolled_foreign_p = None if foreign_p is None else roll_curve(foreign_p, horizon_date)
        pv_rolled = at_horizon(
            tuple(_rolled_amount(f, horizon_date, rub_p, rolled_rub_p) for f in rub_left),
            tuple(
                _rolled_amount(f, horizon_date, foreign_p, rolled_foreign_p) for f in foreign_left
            ),
            rolled_rub_d.discount_factor,
            rolled_foreign_d.discount_factor,
        )
        pv_start = self.base_pv_rub
        return XccyHorizonValuation(
            self.base_id,
            req.swap.trade_id,
            req.swap.currency,
            valuation,
            horizon_date,
            rule,
            spot,
            spot_date,
            finite_money(spot_at_h, "horizon spot"),
            spot_h,
            pv_start,
            finite_money(pv_forward, "forward PV"),
            finite_money(pv_rolled, "rolled PV"),
            finite_money(received, "received"),
            finite_money(pv_forward + received - pv_start, "carry"),
            finite_money(pv_rolled - pv_forward, "rolldown"),
            finite_money(pv_rolled + received - pv_start, "horizon P&L"),
        )


def xccy_horizon(
    request: XccyRiskRequest,
    *,
    horizon_date: date,
    horizon_spot_value_date: date,
    fx_horizon: FxHorizon = "spot_held",
    curve_cache: ShockedCurveCache | None = None,
    xccy_cache: XccyCurveCache | None = None,
) -> XccyHorizonValuation:
    """Carry and rolldown of a cross-currency swap to ``horizon_date``."""
    engine = XccyRiskEngine(
        request,
        ShockedCurveCache() if curve_cache is None else curve_cache,
        XccyCurveCache() if xccy_cache is None else xccy_cache,
    )
    return engine.horizon(
        horizon_date, horizon_spot_value_date=horizon_spot_value_date, fx_horizon=fx_horizon
    )


# --- Par rates and desk factors ------------------------------------------------------


def _with_rate(leg: XccyLeg, value: float) -> XccyLeg:
    if leg.fixed_rate is not None:
        return replace(leg, fixed_rate=value)
    return replace(leg, spread=value)


def xccy_par_rate(
    request: XccyRiskRequest,
    *,
    leg: LegName,
    curve_cache: ShockedCurveCache | None = None,
    xccy_cache: XccyCurveCache | None = None,
) -> float:
    """The rate of ``leg`` that sets the swap's PV to zero (par basis solver).

    For a fixed leg it is the fixed rate, for a floating leg its spread (decimal). PV is
    affine in either, so the root is found from two valuations and checked on a third:
    |PV| must be at most 1e-8 of the leg's RUB notional value per unit rate.
    """
    if leg not in ("rub", "foreign"):
        raise InvalidTerms("leg must be rub or foreign")
    cache = ShockedCurveCache() if curve_cache is None else curve_cache
    xcache = XccyCurveCache() if xccy_cache is None else xccy_cache
    swap = request.swap
    current = swap.rub_leg if leg == "rub" else swap.foreign_leg
    rate0 = current.fixed_rate if current.fixed_rate is not None else current.spread

    def pv(value: float) -> float:
        moved = _with_rate(current, value)
        new_swap = (
            replace(swap, rub_leg=moved) if leg == "rub" else replace(swap, foreign_leg=moved)
        )
        return XccyRiskEngine(replace(request, swap=new_swap), cache, xcache).base_pv_rub

    pv0 = pv(rate0)
    step = 1e-4
    slope = (pv(rate0 + step) - pv0) / step
    if slope == 0.0 or not math.isfinite(slope):
        raise InvalidTerms(f"the {leg} leg's rate does not move the PV (no unknown coupons)")
    par = finite_money(rate0 - pv0 / slope, "par rate")
    residual = pv(par)
    if abs(residual) > 1e-8 * abs(slope):
        raise RuntimeError("par rate did not zero the PV (non-affine terms)")
    return par


def xccy_basis_factor(currency: str) -> str:
    """``XCCYBASIS|<ccy>``: 1 bp of the cross-currency basis on the foreign flows."""
    if not isinstance(currency, str) or not currency or "|" in currency:
        raise InvalidTerms("currency must be a non-empty string without '|'")
    return f"XCCYBASIS|{currency}"


def xccy_factor_risk(
    risk: XccyRiskResult,
    curve_ids: Mapping[str, str],
    second_order: XccySecondOrderRisk | None = None,
) -> FactorRisk:
    """Map XCCY risk to ``IR|<curve>|<bucket>`` per role, ``XCCYBASIS|ccy`` and ``FX|ccy``.

    ``curve_ids`` names each role of the swap (``rub_discount``, ``foreign_discount`` and
    any projection roles); distinct curves need distinct IDs. With ``second_order`` the
    first order is its gradient and the sparse Hessian includes the FX x rates terms;
    otherwise ``risk`` must be central (DEC08).
    """
    roles = tuple(c.role for c in risk.curves)
    if set(curve_ids) != set(roles):
        raise InvalidTerms(f"curve_ids must name exactly the roles {', '.join(roles)}")
    ids = [curve_ids[r] for r in roles]
    if len(set(ids)) != len(ids):
        raise InvalidTerms("distinct curve roles need distinct curve IDs")
    factors = (
        *(ir_factor(curve_ids[r], b) for r in roles for b in RISK_BUCKETS),
        xccy_basis_factor(risk.currency),
        fx_factor(risk.currency),
    )
    if second_order is not None:
        if second_order.base_id != risk.base_id:
            raise InvalidTerms("risk and second-order results describe different positions")
        if second_order.roles != roles or len(second_order.factors) != len(factors):
            raise InvalidTerms("unexpected second-order factor set")
        entries = tuple(
            HessianEntry(i, j, row[j])
            for i, row in enumerate(second_order.hessian_rub)
            for j in range(i, len(row))
            if row[j] != 0.0
        )
        return FactorRisk(
            factors, second_order.gradient_rub, entries, "central", second_order.bump_bp, True
        )
    if risk.difference != "central":
        raise InvalidTerms("desk factors need central-difference risk (DEC08)")
    first = (
        *(b.dv01_rub for c in risk.curves for b in c.buckets),
        risk.basis01_rub,
        risk.fx_delta_rub_per_pct,
    )
    return FactorRisk(factors, first, (), "central", risk.bump_bp, False)
