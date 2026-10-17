"""OTC FX forwards and FX swaps against RUB: covered-parity value, rate, basis and spot risk.

An outstanding forward leg exchanges ``N`` foreign units for ``N * K`` RUB on its value
date ``T``. Its P&L is paid on ``T`` (or on an explicit payment date for a cash-settled
leg) and is discounted on the RUB curve:

``PV = sum(N / q * (F(T) - K) * DF_rub(P))``, with the covered-parity outright
``F(T) = S * [DF_for(T) / DF_for(s)] / [DF_rub(T) / DF_rub(s)] * exp(b * tau(s, T))``

for spot ``S`` (value date ``s``), quote units ``q`` and basis ``b``. This is a different
settlement style from FX futures, whose variation margin is paid daily and is not
discounted (``fx_futures``). The outright formula, the 11-bucket grid, the shock
operations, the shocked-curve cache and the identities are shared with FX futures.

Value dates come from an explicit calendar (``fx_value_date``); implied foreign curves
come from caller-supplied swap points and outrights (``implied_foreign_curve``). No
market data, calendar or exchange specification is looked up here.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Literal

from ._money import finite_money, positive_money
from ._risk_common import (
    _ZERO_SHOCK,
    RISK_BUCKETS,
    ShockedCurveCache,
    _base_curve,
    _curve_basis,
    _curve_identity,
    _shock_curve,
    _snapshot_curve,
)
from .canonical import canonical_sha256
from .conventions import BusinessDayConvention, CurveConvention, DayCount
from .dates import BusinessCalendar
from .discount import (
    CurveBootstrapRequest,
    DiscountCurve,
    DiscountFactorCurve,
    bootstrap_discount_curve,
)
from .errors import InvalidTerms
from .factors import FactorRisk, _upper, fx_basis_factor, fx_factor, ir_factor
from .fx_futures import FxFuturesScenario
from .tenors import parse_tenor, resolve_tenor

__all__ = [
    "FX_FORWARD_RISK_MODEL_VERSION",
    "FX_FORWARD_SECOND_ORDER_MODEL_VERSION",
    "FX_IMPLIED_CURVE_MODEL_VERSION",
    "FX_VALUE_DATE_CODES",
    "FxForward",
    "FxForwardBucketRisk",
    "FxForwardLeg",
    "FxForwardLegValuation",
    "FxForwardRiskEngine",
    "FxForwardRiskRequest",
    "FxForwardRiskResult",
    "FxForwardScenarioResult",
    "FxForwardSecondOrderRisk",
    "FxOutrightQuote",
    "FxSwap",
    "FxSwapPointsQuote",
    "cip_implied_foreign_rate",
    "cip_implied_rub_rate",
    "cip_outright",
    "fx_forward_basis_bp",
    "fx_forward_factor_risk",
    "fx_forward_outright",
    "fx_swap_value_dates",
    "fx_value_date",
    "implied_foreign_curve",
    "implied_outrights",
    "joint_calendar",
]

FX_FORWARD_RISK_MODEL_VERSION = "rub-fx-forward-parity-risk-v1"
FX_FORWARD_SECOND_ORDER_MODEL_VERSION = "rub-fx-forward-parity-second-order-v1"
FX_IMPLIED_CURVE_MODEL_VERSION = "fx-implied-logdf-v1"

FX_VALUE_DATE_CODES = {"TOD": 0, "TOM": 1, "SPT": 2}
"""Business-day lags of the named FX value dates: today, tomorrow, spot (T+2)."""


# --- Calendars and value dates -------------------------------------------------------


def joint_calendar(calendars: Iterable[BusinessCalendar], *, version: str) -> BusinessCalendar:
    """A day is a business day only if it is one in every calendar (FX settlement).

    Holidays are the union; weekend working days are those every calendar works. The
    covered range is the intersection of the bounded calendars' ranges (overrides
    outside it are dropped, since those dates are rejected anyway); with no bounded
    calendar the result is unbounded. ``version`` names the combination explicitly.
    """
    items = tuple(calendars)
    if not items:
        raise InvalidTerms("joint_calendar needs at least one calendar")
    for item in items:
        if not isinstance(item, BusinessCalendar):
            raise TypeError("joint_calendar expects BusinessCalendar values")
    holidays = frozenset(day for c in items for day in c.holidays)
    working = items[0].working_days
    for item in items[1:]:
        working &= item.working_days
    bounded = [c for c in items if c.is_bounded]
    if not bounded:
        return BusinessCalendar(holidays, working, version)
    start = max(c.valid_from for c in bounded if c.valid_from is not None)
    end = min(c.valid_to for c in bounded if c.valid_to is not None)
    if end < start:
        raise InvalidTerms("the calendars share no covered date range")
    return BusinessCalendar(
        frozenset(d for d in holidays if start <= d <= end),
        frozenset(d for d in working if start <= d <= end),
        version,
        start,
        end,
    )


def fx_value_date(
    trade_date: date,
    code: str,
    calendar: BusinessCalendar,
    *,
    spot_lag_days: int | None = None,
    business_day_convention: BusinessDayConvention = BusinessDayConvention.MODIFIED_FOLLOWING,
) -> date:
    """Value date of ``TOD``, ``TOM``, ``SPT`` or a ``<n>D/W/M/Y`` tenor from spot.

    ``calendar`` is the settlement calendar of the pair (usually a ``joint_calendar``).
    ``TOD`` exists only on a settlement business day; ``TOM`` and ``SPT`` are one and
    two business days later. A tenor runs from the spot date, ``spot_lag_days``
    business days after the trade date (1 for MOEX ``TOM`` swaps, 2 for ``SPT``),
    with :func:`resolve_tenor`'s rule: calendar months with month-end clamping, then
    ``business_day_convention``. Tenor codes need an explicit ``spot_lag_days``.
    """
    if not isinstance(code, str):
        raise InvalidTerms("value-date code must be a string")
    name = code.strip().upper()
    if name in FX_VALUE_DATE_CODES:
        if name == "TOD" and not calendar.is_business_day(trade_date):
            raise InvalidTerms(
                f"no TOD value date: {trade_date.isoformat()} is not a settlement day "
                f"in calendar {calendar.version}"
            )
        return calendar.add_business_days(trade_date, FX_VALUE_DATE_CODES[name])
    tenor = parse_tenor(name)
    if tenor.unit in ("ON", "TN", "SN"):
        raise InvalidTerms(f"{tenor.code} is a deposit period, not an FX value date")
    if spot_lag_days is None:
        raise InvalidTerms("a tenor value date needs an explicit spot_lag_days")
    return resolve_tenor(
        trade_date,
        tenor.code,
        calendar,
        spot_lag_days=spot_lag_days,
        business_day_convention=business_day_convention,
    ).end_date


def fx_swap_value_dates(
    trade_date: date,
    near: str,
    far: str,
    calendar: BusinessCalendar,
    *,
    spot_lag_days: int | None = None,
    business_day_convention: BusinessDayConvention = BusinessDayConvention.MODIFIED_FOLLOWING,
) -> tuple[date, date]:
    """Near and far value dates of an FX swap, e.g. ``("TOD", "TOM")`` or ``("TOM", "1M")``."""
    near_date, far_date = (
        fx_value_date(
            trade_date,
            code,
            calendar,
            spot_lag_days=spot_lag_days,
            business_day_convention=business_day_convention,
        )
        for code in (near, far)
    )
    if far_date <= near_date:
        raise InvalidTerms(f"swap {near}/{far}: far value date must follow the near one")
    return near_date, far_date


# --- Money-market covered interest parity ---------------------------------------------


def _accrual(day_count: DayCount, start: date, end: date, label: str) -> float:
    if not isinstance(day_count, DayCount):
        raise TypeError(f"{label} day count must be a DayCount")
    if end <= start:
        raise InvalidTerms("the forward period must end after it starts")
    return positive_money(day_count.year_fraction(start, end), f"{label} accrual")


def cip_outright(
    spot_rate: float,
    start: date,
    end: date,
    *,
    rub_rate: float,
    foreign_rate: float,
    rub_day_count: DayCount,
    foreign_day_count: DayCount,
) -> float:
    """``F = S * (1 + r_rub * tau_rub) / (1 + r_for * tau_for)`` with simple rates.

    ``S`` is the outright for ``start`` and ``F`` the outright for ``end``; both day
    counts are explicit (for example ACT/365F for RUB, ACT/360 for USD).
    """
    spot = positive_money(spot_rate, "spot rate")
    rub = 1.0 + finite_money(rub_rate, "RUB rate") * _accrual(rub_day_count, start, end, "RUB")
    foreign = 1.0 + finite_money(foreign_rate, "foreign rate") * _accrual(
        foreign_day_count, start, end, "foreign"
    )
    if rub <= 0.0 or foreign <= 0.0:
        raise InvalidTerms("a simple rate leaves the growth factor non-positive")
    return positive_money(spot * rub / foreign, "outright")


def cip_implied_rub_rate(
    spot_rate: float,
    outright: float,
    start: date,
    end: date,
    *,
    foreign_rate: float,
    rub_day_count: DayCount,
    foreign_day_count: DayCount,
) -> float:
    """The simple RUB rate for ``[start, end]`` that :func:`cip_outright` maps to ``outright``."""
    ratio = positive_money(outright, "outright") / positive_money(spot_rate, "spot rate")
    foreign = 1.0 + finite_money(foreign_rate, "foreign rate") * _accrual(
        foreign_day_count, start, end, "foreign"
    )
    if foreign <= 0.0:
        raise InvalidTerms("the foreign rate leaves the growth factor non-positive")
    return finite_money(
        (ratio * foreign - 1.0) / _accrual(rub_day_count, start, end, "RUB"), "RUB rate"
    )


def cip_implied_foreign_rate(
    spot_rate: float,
    outright: float,
    start: date,
    end: date,
    *,
    rub_rate: float,
    rub_day_count: DayCount,
    foreign_day_count: DayCount,
) -> float:
    """The simple foreign rate for ``[start, end]`` that :func:`cip_outright` maps to ``outright``."""
    ratio = positive_money(spot_rate, "spot rate") / positive_money(outright, "outright")
    rub = 1.0 + finite_money(rub_rate, "RUB rate") * _accrual(rub_day_count, start, end, "RUB")
    if rub <= 0.0:
        raise InvalidTerms("the RUB rate leaves the growth factor non-positive")
    return finite_money(
        (ratio * rub - 1.0) / _accrual(foreign_day_count, start, end, "foreign"), "foreign rate"
    )


# --- Curve parity ---------------------------------------------------------------------


def _signed_years(day_count: DayCount, start: date, end: date) -> float:
    if end >= start:
        return day_count.year_fraction(start, end)
    return -day_count.year_fraction(end, start)


def _parity(
    spot: float,
    rub: DiscountFactorCurve,
    foreign: DiscountFactorCurve,
    spot_date: date,
    value_date: date,
) -> float:
    """``S * [DF_for(T)/DF_for(s)] / [DF_rub(T)/DF_rub(s)]`` (no basis)."""
    return (
        spot
        * (foreign.discount_factor(value_date) / foreign.discount_factor(spot_date))
        * (rub.discount_factor(spot_date) / rub.discount_factor(value_date))
    )


def _resolved_curve(curve: DiscountCurve | CurveBootstrapRequest, label: str) -> DiscountCurve:
    if isinstance(curve, CurveBootstrapRequest):
        return bootstrap_discount_curve(curve)
    if isinstance(curve, DiscountCurve):
        return curve
    raise TypeError(f"{label} must be DiscountCurve or CurveBootstrapRequest")


def _check_parity_dates(
    rub: DiscountCurve, foreign: DiscountCurve, spot_value_date: date, value_date: date
) -> None:
    if rub.curve_date != foreign.curve_date:
        raise InvalidTerms("RUB and foreign curves must share one valuation date")
    if spot_value_date < rub.curve_date or value_date < rub.curve_date:
        raise InvalidTerms("spot and value dates cannot predate curve valuation")


def fx_forward_outright(
    *,
    spot_rate: float,
    spot_value_date: date,
    value_date: date,
    rub_curve: DiscountCurve | CurveBootstrapRequest,
    foreign_curve: DiscountCurve | CurveBootstrapRequest,
    basis_bp: float,
    basis_day_count: DayCount = DayCount.ACT_365_FIXED,
) -> float:
    """Covered-parity outright for ``value_date`` (before or after the spot date)."""
    rub = _resolved_curve(rub_curve, "rub_curve")
    foreign = _resolved_curve(foreign_curve, "foreign_curve")
    _check_parity_dates(rub, foreign, spot_value_date, value_date)
    tau = _signed_years(basis_day_count, spot_value_date, value_date)
    basis = finite_money(basis_bp, "basis bp") * 1e-4
    spot = positive_money(spot_rate, "spot rate")
    return positive_money(
        _parity(spot, rub, foreign, spot_value_date, value_date) * math.exp(basis * tau),
        "outright",
    )


def fx_forward_basis_bp(
    *,
    spot_rate: float,
    spot_value_date: date,
    value_date: date,
    market_outright: float,
    rub_curve: DiscountCurve | CurveBootstrapRequest,
    foreign_curve: DiscountCurve | CurveBootstrapRequest,
    basis_day_count: DayCount = DayCount.ACT_365_FIXED,
) -> float:
    """The basis (bp, continuous in ``basis_day_count``) that reprices a market outright."""
    if value_date == spot_value_date:
        raise InvalidTerms("the basis needs a value date other than the spot date")
    theoretical = fx_forward_outright(
        spot_rate=spot_rate,
        spot_value_date=spot_value_date,
        value_date=value_date,
        rub_curve=rub_curve,
        foreign_curve=foreign_curve,
        basis_bp=0.0,
        basis_day_count=basis_day_count,
    )
    tau = _signed_years(basis_day_count, spot_value_date, value_date)
    if tau == 0.0:
        raise InvalidTerms("the basis accrual time is zero")
    market = positive_money(market_outright, "market outright")
    return finite_money(math.log(market / theoretical) / tau * 10_000.0, "basis bp")


# --- Implied foreign curve ------------------------------------------------------------


@dataclass(frozen=True)
class FxSwapPointsQuote:
    """Swap points: far-leg outright minus near-leg outright, in the spot's price units."""

    label: str
    near_date: date
    far_date: date
    points: float

    def __post_init__(self) -> None:
        _text(self.label, "quote label")
        if self.far_date <= self.near_date:
            raise InvalidTerms(f"{self.label}: far date must follow the near date")
        object.__setattr__(self, "points", finite_money(self.points, "swap points"))


@dataclass(frozen=True)
class FxOutrightQuote:
    """An outright for one value date (an OTC forward, or an FX futures price)."""

    label: str
    value_date: date
    outright: float

    def __post_init__(self) -> None:
        _text(self.label, "quote label")
        object.__setattr__(self, "outright", positive_money(self.outright, "outright"))


FxCurveQuote = FxSwapPointsQuote | FxOutrightQuote


def implied_outrights(
    spot_rate: float,
    spot_value_date: date,
    quotes: Sequence[FxCurveQuote],
) -> tuple[tuple[date, float], ...]:
    """Outrights by value date, chaining swap points from the spot and from each other.

    A swap quote is resolved once one of its legs is known (``far = near + points``).
    Two quotes that fix the same date, a quote no chain reaches, or a non-positive
    outright raise :class:`InvalidTerms`. The result includes the spot and is sorted.
    """
    spot = positive_money(spot_rate, "spot rate")
    known: dict[date, tuple[float, str]] = {spot_value_date: (spot, "spot")}

    def fix(day: date, value: float, label: str) -> None:
        if day in known:
            raise InvalidTerms(f"{label} and {known[day][1]} both fix {day.isoformat()}")
        known[day] = (positive_money(value, f"{label} outright"), label)

    pending: list[FxSwapPointsQuote] = []
    labels: set[str] = set()
    for quote in quotes:
        if not isinstance(quote, (FxSwapPointsQuote, FxOutrightQuote)):
            raise TypeError("quotes must be FxSwapPointsQuote or FxOutrightQuote")
        if quote.label in labels:
            raise InvalidTerms(f"duplicate quote label {quote.label!r}")
        labels.add(quote.label)
        if isinstance(quote, FxOutrightQuote):
            fix(quote.value_date, quote.outright, quote.label)
        else:
            pending.append(quote)
    while pending:
        remaining = []
        for quote in pending:
            near, far = known.get(quote.near_date), known.get(quote.far_date)
            if near is not None and far is not None:
                raise InvalidTerms(f"{quote.label} links two dates that are already fixed")
            if near is not None:
                fix(quote.far_date, near[0] + quote.points, quote.label)
            elif far is not None:
                fix(quote.near_date, far[0] - quote.points, quote.label)
            else:
                remaining.append(quote)
        if len(remaining) == len(pending):
            names = ", ".join(q.label for q in remaining)
            raise InvalidTerms(f"swap quotes not connected to the spot or an outright: {names}")
        pending = remaining
    return tuple(sorted((d, v) for d, (v, _) in known.items()))


def implied_foreign_curve(
    *,
    rub_curve: DiscountCurve | CurveBootstrapRequest,
    spot_rate: float,
    spot_value_date: date,
    quotes: Sequence[FxCurveQuote],
    convention: CurveConvention | None = None,
) -> DiscountCurve:
    """The foreign discount curve that makes covered parity reprice every quote exactly.

    ``DF_for(T) / DF_for(s) = (F(T) / S) * DF_rub(T) / DF_rub(s)`` at each outright date
    from :func:`implied_outrights`; the basis is zero by construction. The outrights must
    reach the curve date (for example through a ``TOD/TOM`` swap) so that the curve is
    anchored at ``DF = 1`` there; otherwise :class:`InvalidTerms`. Between nodes the
    curve is log-linear in DF, beyond the last node flat-forward (``DiscountCurve``).
    """
    rub = _resolved_curve(rub_curve, "rub_curve")
    curve_date = rub.curve_date
    if spot_value_date < curve_date:
        raise InvalidTerms("spot value date cannot predate curve valuation")
    outrights = implied_outrights(spot_rate, spot_value_date, quotes)
    if outrights[0][0] < curve_date:
        raise InvalidTerms("an implied outright predates the curve date")
    if outrights[0][0] != curve_date:
        raise InvalidTerms(
            "the quotes do not reach the curve date; add a swap from the curve date "
            "(for example TOD/TOM) to anchor the foreign curve"
        )
    spot = positive_money(spot_rate, "spot rate")
    rub_spot = rub.discount_factor(spot_value_date)
    relative = {
        day: (value / spot) * rub.discount_factor(day) / rub_spot for day, value in outrights
    }
    anchor = relative[curve_date]
    nodes = tuple(
        (day, positive_money(relative[day] / anchor, "implied discount factor"))
        for day, _ in outrights
        if day > curve_date
    )
    if not nodes:
        raise InvalidTerms("the quotes give no outright after the curve date")
    return DiscountCurve(
        curve_date,
        nodes,
        FX_IMPLIED_CURVE_MODEL_VERSION,
        convention=CurveConvention() if convention is None else convention,
    )


# --- Instruments ----------------------------------------------------------------------


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InvalidTerms(f"{label} must be nonempty text")
    return value


def _foreign_currency(value: object) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 3
        or not value.isascii()
        or not value.isalpha()
        or not value.isupper()
    ):
        raise InvalidTerms("currency must be a three-letter upper-case ISO code")
    if value == "RUB":
        raise InvalidTerms("an FX forward needs a foreign currency, not RUB")
    return value


@dataclass(frozen=True)
class FxForwardLeg:
    """One exchange: receive ``notional_foreign`` (signed; negative = deliver) on ``value_date``
    against ``notional_foreign * contract_rate / quote_units`` RUB.

    ``payment_date`` is when the leg's RUB value is paid. ``None`` means the value date
    (a deliverable leg). A cash-settled leg pays ``N / q * (F(T) - K)`` on its
    explicit payment date instead.
    """

    value_date: date
    notional_foreign: float
    contract_rate: float
    payment_date: date | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.value_date, date):
            raise TypeError("value_date must be a date")
        if self.payment_date is not None and not isinstance(self.payment_date, date):
            raise TypeError("payment_date must be a date or None")
        notional = finite_money(self.notional_foreign, "foreign notional")
        if notional == 0.0:
            raise InvalidTerms("a forward leg needs a nonzero foreign notional")
        object.__setattr__(self, "notional_foreign", notional)
        object.__setattr__(self, "contract_rate", positive_money(self.contract_rate, "rate"))

    @property
    def settlement_date(self) -> date:
        return self.value_date if self.payment_date is None else self.payment_date


@dataclass(frozen=True)
class FxForward:
    """An outstanding outright forward (also an unsettled spot trade: value date = its spot).

    Rates are RUB per ``quote_units`` foreign units; ``quote_units`` comes from the
    trade's confirmation (1 for USD or CNY, 100 for some quotes) and nothing is inferred.
    """

    trade_id: str
    currency: str
    quote_units: float
    leg: FxForwardLeg

    def __post_init__(self) -> None:
        _text(self.trade_id, "trade_id")
        _foreign_currency(self.currency)
        if not isinstance(self.leg, FxForwardLeg):
            raise TypeError("leg must be an FxForwardLeg")
        object.__setattr__(self, "quote_units", positive_money(self.quote_units, "quote units"))

    @property
    def legs(self) -> tuple[FxForwardLeg, ...]:
        return (self.leg,)


@dataclass(frozen=True)
class FxSwap:
    """Near and far legs of opposite sign. Only outstanding legs may be valued.

    Once the near leg has settled, value the far leg as an :class:`FxForward`
    (:meth:`far_leg_forward`).
    """

    trade_id: str
    currency: str
    quote_units: float
    near: FxForwardLeg
    far: FxForwardLeg

    def __post_init__(self) -> None:
        _text(self.trade_id, "trade_id")
        _foreign_currency(self.currency)
        if not isinstance(self.near, FxForwardLeg) or not isinstance(self.far, FxForwardLeg):
            raise TypeError("near and far must be FxForwardLeg values")
        if self.far.value_date <= self.near.value_date:
            raise InvalidTerms("the far leg must settle after the near leg")
        if (self.near.notional_foreign > 0.0) == (self.far.notional_foreign > 0.0):
            raise InvalidTerms("near and far legs of an FX swap must have opposite signs")
        object.__setattr__(self, "quote_units", positive_money(self.quote_units, "quote units"))

    @property
    def legs(self) -> tuple[FxForwardLeg, ...]:
        return (self.near, self.far)

    @property
    def swap_points(self) -> float:
        """Far contract rate minus near contract rate."""
        return self.far.contract_rate - self.near.contract_rate

    def far_leg_forward(self) -> FxForward:
        return FxForward(self.trade_id, self.currency, self.quote_units, self.far)


# --- Risk -----------------------------------------------------------------------------


@dataclass(frozen=True)
class FxForwardRiskRequest:
    """Inputs of one outstanding forward or swap; spot is in the trade's quote units.

    ``basis_bp`` is explicit: zero when ``foreign_curve`` is implied from FX swaps (it
    then carries the whole cross-currency basis), or calibrated with
    :func:`fx_forward_basis_bp` when it is, for example, a foreign OIS curve. Each
    curve's type selects its risk basis as in ``BondRiskRequest``. A leg whose value
    date or payment date precedes the curve date has settled and is rejected.
    """

    instrument: FxForward | FxSwap
    spot_rate: float
    spot_value_date: date
    rub_curve: DiscountCurve | CurveBootstrapRequest
    foreign_curve: DiscountCurve | CurveBootstrapRequest
    basis_bp: float
    basis_day_count: DayCount = DayCount.ACT_365_FIXED

    def __post_init__(self) -> None:
        if not isinstance(self.instrument, (FxForward, FxSwap)):
            raise TypeError("instrument must be an FxForward or an FxSwap")
        for name in ("rub_curve", "foreign_curve"):
            if not isinstance(getattr(self, name), (DiscountCurve, CurveBootstrapRequest)):
                raise TypeError(f"{name} must be DiscountCurve or CurveBootstrapRequest")
        if not isinstance(self.basis_day_count, DayCount):
            raise TypeError("basis_day_count must be a DayCount")
        curve_date = self.rub_curve.curve_date
        if curve_date != self.foreign_curve.curve_date:
            raise InvalidTerms("RUB and foreign curves must share one valuation date")
        if self.spot_value_date < curve_date:
            raise InvalidTerms("spot value date cannot predate curve valuation")
        for leg in self.instrument.legs:
            if leg.value_date < curve_date or leg.settlement_date < curve_date:
                raise InvalidTerms(
                    f"{self.instrument.trade_id}: a leg settled before the curve date "
                    f"({leg.value_date.isoformat()}); value only outstanding legs"
                )
        object.__setattr__(self, "spot_rate", positive_money(self.spot_rate, "spot rate"))
        object.__setattr__(self, "basis_bp", finite_money(self.basis_bp, "basis bp"))
        object.__setattr__(self, "rub_curve", _snapshot_curve(self.rub_curve))
        object.__setattr__(self, "foreign_curve", _snapshot_curve(self.foreign_curve))


@dataclass(frozen=True)
class FxForwardBucketRisk:
    """RUB of PV per bp of a shift at one bucket pillar (difference over the bump)."""

    bucket: str
    dv01_rub: float


@dataclass(frozen=True)
class FxForwardLegValuation:
    value_date: date
    payment_date: date
    notional_foreign: float
    contract_rate: float
    forward_rate: float
    forward_points: float
    rub_discount_factor: float
    pv_rub: float


@dataclass(frozen=True)
class FxForwardRiskResult:
    """Signed RUB PV sensitivities of an outstanding forward or swap.

    ``fx_delta_foreign`` is the spot-equivalent foreign amount for the spot value date
    (``dPV/dS * q / DF_rub(s)``); for a deliverable leg it is its notional discounted
    from ``T`` to ``s`` on the foreign curve (times the basis growth).
    ``fx_delta_rub_per_pct`` is the exact PV change for a +1 % relative spot move.
    """

    base_id: str
    trade_id: str
    currency: str
    rub_curve_basis: str
    foreign_curve_basis: str
    difference: str
    bump_bp: float
    spot_rate: float
    spot_value_date: date
    basis_bp: float
    pv_rub: float
    legs: tuple[FxForwardLegValuation, ...]
    fx_delta_foreign: float
    fx_delta_rub_per_pct: float
    rub_buckets: tuple[FxForwardBucketRisk, ...]
    rub_dv01_rub: float
    rub_parallel_dv01_rub: float
    foreign_buckets: tuple[FxForwardBucketRisk, ...]
    foreign_dv01_rub: float
    foreign_parallel_dv01_rub: float
    basis01_rub: float
    model_version: str = FX_FORWARD_RISK_MODEL_VERSION


@dataclass(frozen=True)
class FxForwardScenarioResult:
    base_id: str
    scenario_id: str
    rub_curve_basis: str
    foreign_curve_basis: str
    base_pv_rub: float
    shocked_pv_rub: float
    rub_rates_pnl_rub: float
    foreign_rates_pnl_rub: float
    basis_pnl_rub: float
    fx_spot_pnl_rub: float
    interaction_pnl_rub: float
    total_pnl_rub: float
    model_version: str = FX_FORWARD_RISK_MODEL_VERSION


@dataclass(frozen=True)
class FxForwardSecondOrderRisk:
    """Gradient and full symmetric Hessian over 11 RUB, 11 foreign, basis and spot.

    Rates and basis are in bp (central differences with ``bump_bp``); spot is a 1 %
    relative move and is exact, since PV is linear in spot: its diagonal is zero and
    its cross terms are 1 % of the central rate derivative of the foreign legs' PV.
    Units: RUB per factor unit and RUB per product of units.
    """

    base_id: str
    rub_curve_basis: str
    foreign_curve_basis: str
    factors: tuple[str, ...]
    bump_bp: float
    gradient_rub: tuple[float, ...]
    hessian_rub: tuple[tuple[float, ...], ...]
    model_version: str = FX_FORWARD_SECOND_ORDER_MODEL_VERSION


_Moves = tuple[tuple[float, ...], tuple[float, ...], float, float]
_NO_MOVE: _Moves = (_ZERO_SHOCK, _ZERO_SHOCK, 0.0, 0.0)


@dataclass(frozen=True)
class _Leg:
    value_date: date
    payment_date: date
    units: float  # notional / quote_units
    contract_rate: float
    tau: float  # basis accrual from the spot date, signed


@dataclass(frozen=True)
class FxForwardRiskEngine:
    """Value an outstanding forward or swap and shock it with the basis held fixed.

    ``PV = A - B`` with the foreign legs' RUB value ``A = sum(N/q * F(T) * DF_rub(P))``
    and the RUB legs' value ``B = sum(N/q * K * DF_rub(P))``. Share one
    ``curve_cache`` across engines on the same curves.
    """

    request: FxForwardRiskRequest
    curve_cache: ShockedCurveCache = field(
        default_factory=ShockedCurveCache, compare=False, repr=False
    )
    base_pv_rub: float = field(init=False)
    base_id: str = field(init=False)
    _rub_curve: DiscountCurve = field(init=False, repr=False)
    _foreign_curve: DiscountCurve = field(init=False, repr=False)
    _legs: tuple[_Leg, ...] = field(init=False, repr=False)
    _base_values: tuple[float, float] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        req = self.request
        if not isinstance(self.curve_cache, ShockedCurveCache):
            raise TypeError("curve_cache must be a ShockedCurveCache")
        object.__setattr__(self, "_rub_curve", _base_curve(req.rub_curve, self.curve_cache))
        object.__setattr__(self, "_foreign_curve", _base_curve(req.foreign_curve, self.curve_cache))
        instrument = req.instrument
        legs = tuple(
            _Leg(
                leg.value_date,
                leg.settlement_date,
                leg.notional_foreign / instrument.quote_units,
                leg.contract_rate,
                _signed_years(req.basis_day_count, req.spot_value_date, leg.value_date),
            )
            for leg in instrument.legs
        )
        object.__setattr__(self, "_legs", legs)
        values = self._values(_NO_MOVE)
        object.__setattr__(self, "_base_values", values)
        object.__setattr__(self, "base_pv_rub", finite_money(values[0] - values[1], "PV"))
        object.__setattr__(
            self,
            "base_id",
            canonical_sha256(
                {
                    "model": FX_FORWARD_RISK_MODEL_VERSION,
                    "instrument_type": type(instrument).__name__,
                    "instrument": instrument,
                    "spot_rate": req.spot_rate,
                    "spot_value_date": req.spot_value_date,
                    "rub_curve_basis": self.rub_curve_basis,
                    "rub_curve": _curve_identity(req.rub_curve),
                    "foreign_curve_basis": self.foreign_curve_basis,
                    "foreign_curve": _curve_identity(req.foreign_curve),
                    "basis_bp": req.basis_bp,
                    "basis_day_count": req.basis_day_count.value,
                }
            ),
        )

    @property
    def rub_curve_basis(self) -> str:
        return _curve_basis(self.request.rub_curve)

    @property
    def foreign_curve_basis(self) -> str:
        return _curve_basis(self.request.foreign_curve)

    def _curves(
        self, rub_shifts: tuple[float, ...], foreign_shifts: tuple[float, ...]
    ) -> tuple[DiscountFactorCurve, DiscountFactorCurve]:
        req = self.request
        rub = _shock_curve(req.rub_curve, self._rub_curve, rub_shifts, self.curve_cache)
        foreign = _shock_curve(
            req.foreign_curve, self._foreign_curve, foreign_shifts, self.curve_cache
        )
        return rub, foreign

    def _forward(
        self,
        leg: _Leg,
        spot: float,
        rub: DiscountFactorCurve,
        foreign: DiscountFactorCurve,
        basis: float,
    ) -> float:
        parity = _parity(spot, rub, foreign, self.request.spot_value_date, leg.value_date)
        return positive_money(parity * math.exp(basis * leg.tau), "forward rate")

    def _values(self, moves: _Moves) -> tuple[float, float]:
        """(foreign legs' RUB PV, RUB legs' PV) under the moves."""
        rub_shifts, foreign_shifts, basis_shift, spot_shift = moves
        req = self.request
        try:
            rub, foreign = self._curves(rub_shifts, foreign_shifts)
            spot = req.spot_rate * (1.0 + spot_shift / 100.0)
            basis = finite_money(req.basis_bp + basis_shift, "shocked basis") * 1e-4
            spot_date = req.spot_value_date
            # N/q * F(T) * DF_rub(P) written as N/q * S * DF_rub(s) * DF_for(T)/DF_for(s) *
            # exp(b tau) * DF_rub(P)/DF_rub(T): for a deliverable leg (P = T) the last ratio
            # is exactly one, so the foreign legs carry no rounding noise from RUB rates
            # beyond DF_rub(s).
            spot_pv = spot * rub.discount_factor(spot_date) / foreign.discount_factor(spot_date)
            foreign_pv = []
            rub_pv = []
            for leg in self._legs:
                df = rub.discount_factor(leg.payment_date)
                carry = df / rub.discount_factor(leg.value_date)
                growth = foreign.discount_factor(leg.value_date) * math.exp(basis * leg.tau)
                foreign_pv.append(leg.units * spot_pv * growth * carry)
                rub_pv.append(leg.units * leg.contract_rate * df)
            return (
                finite_money(math.fsum(foreign_pv), "foreign legs PV"),
                finite_money(math.fsum(rub_pv), "RUB legs PV"),
            )
        except (OverflowError, ZeroDivisionError) as exc:
            raise ValueError("shock is outside the supported numerical domain") from exc

    def _pv(self, moves: _Moves) -> float:
        foreign, rub = self._values(moves)
        return foreign - rub

    def leg_valuations(self) -> tuple[FxForwardLegValuation, ...]:
        req = self.request
        basis = req.basis_bp * 1e-4
        rows = []
        for leg, terms in zip(self._legs, req.instrument.legs, strict=True):
            forward = self._forward(leg, req.spot_rate, self._rub_curve, self._foreign_curve, basis)
            df = self._rub_curve.discount_factor(leg.payment_date)
            rows.append(
                FxForwardLegValuation(
                    value_date=leg.value_date,
                    payment_date=leg.payment_date,
                    notional_foreign=terms.notional_foreign,
                    contract_rate=leg.contract_rate,
                    forward_rate=forward,
                    forward_points=forward - req.spot_rate,
                    rub_discount_factor=df,
                    pv_rub=finite_money(leg.units * (forward - leg.contract_rate) * df, "leg PV"),
                )
            )
        return tuple(rows)

    def risk(
        self, *, bump_bp: float = 1.0, difference: Literal["forward", "central"] = "forward"
    ) -> FxForwardRiskResult:
        """Signed RUB per bucket of each curve, parallel per curve, basis and spot.

        Forward is the exact +1bp PV change; central estimates a derivative (DEC08:
        desk factors need central). Bucket sums and independently repriced parallel
        shifts are both reported.
        """
        bump = positive_money(bump_bp, "risk bump bp")
        if difference not in ("forward", "central"):
            raise ValueError("difference must be forward or central")
        size = len(RISK_BUCKETS)

        def sensitivity(moves: _Moves) -> float:
            up = self._pv(moves)
            if difference == "central":
                rub, foreign, basis, spot = moves
                down = self._pv((tuple(-s for s in rub), tuple(-s for s in foreign), -basis, spot))
                return finite_money((up - down) / (2.0 * bump), "sensitivity")
            return finite_money((up - self.base_pv_rub) / bump, "sensitivity")

        def buckets(curve_index: int) -> tuple[FxForwardBucketRisk, ...]:
            rows = []
            for i, bucket in enumerate(RISK_BUCKETS):
                shifts = tuple(bump if i == j else 0.0 for j in range(size))
                moves: _Moves = (
                    (shifts, _ZERO_SHOCK, 0.0, 0.0)
                    if curve_index == 0
                    else (_ZERO_SHOCK, shifts, 0.0, 0.0)
                )
                rows.append(FxForwardBucketRisk(bucket, sensitivity(moves)))
            return tuple(rows)

        parallel = (bump,) * size
        rub_buckets = buckets(0)
        foreign_buckets = buckets(1)
        req = self.request
        foreign_pv = self._base_values[0]
        delta_per_pct = finite_money(foreign_pv / 100.0, "FX delta RUB")
        return FxForwardRiskResult(
            base_id=self.base_id,
            trade_id=req.instrument.trade_id,
            currency=req.instrument.currency,
            rub_curve_basis=self.rub_curve_basis,
            foreign_curve_basis=self.foreign_curve_basis,
            difference=difference,
            bump_bp=bump,
            spot_rate=req.spot_rate,
            spot_value_date=req.spot_value_date,
            basis_bp=req.basis_bp,
            pv_rub=self.base_pv_rub,
            legs=self.leg_valuations(),
            fx_delta_foreign=finite_money(
                foreign_pv
                * req.instrument.quote_units
                / (req.spot_rate * self._rub_curve.discount_factor(req.spot_value_date)),
                "FX delta",
            ),
            fx_delta_rub_per_pct=delta_per_pct,
            rub_buckets=rub_buckets,
            rub_dv01_rub=finite_money(math.fsum(b.dv01_rub for b in rub_buckets), "RUB DV01"),
            rub_parallel_dv01_rub=sensitivity((parallel, _ZERO_SHOCK, 0.0, 0.0)),
            foreign_buckets=foreign_buckets,
            foreign_dv01_rub=finite_money(
                math.fsum(b.dv01_rub for b in foreign_buckets), "foreign DV01"
            ),
            foreign_parallel_dv01_rub=sensitivity((_ZERO_SHOCK, parallel, 0.0, 0.0)),
            basis01_rub=sensitivity((_ZERO_SHOCK, _ZERO_SHOCK, bump, 0.0)),
        )

    def reprice(self, scenario: FxFuturesScenario) -> FxForwardScenarioResult:
        """Exact PV change, each factor group isolated, and their interaction.

        The scenario type is shared with FX futures, so one scenario applies to both.
        """
        if not isinstance(scenario, FxFuturesScenario):
            raise TypeError("scenario must be an FxFuturesScenario")
        rub, foreign = scenario.rub_curve_shifts_bp, scenario.foreign_curve_shifts_bp
        basis, spot = scenario.basis_shift_bp, scenario.fx_spot_shift_pct
        shocked = finite_money(self._pv((rub, foreign, basis, spot)), "shocked PV")

        def pnl(moves: _Moves, label: str) -> float:
            return finite_money(self._pv(moves) - self.base_pv_rub, label)

        rub_pnl = pnl((rub, _ZERO_SHOCK, 0.0, 0.0), "RUB rates PnL")
        foreign_pnl = pnl((_ZERO_SHOCK, foreign, 0.0, 0.0), "foreign rates PnL")
        basis_pnl = pnl((_ZERO_SHOCK, _ZERO_SHOCK, basis, 0.0), "basis PnL")
        spot_pnl = pnl((_ZERO_SHOCK, _ZERO_SHOCK, 0.0, spot), "FX spot PnL")
        total = finite_money(shocked - self.base_pv_rub, "total PnL")
        return FxForwardScenarioResult(
            base_id=self.base_id,
            scenario_id=canonical_sha256(scenario),
            rub_curve_basis=self.rub_curve_basis,
            foreign_curve_basis=self.foreign_curve_basis,
            base_pv_rub=self.base_pv_rub,
            shocked_pv_rub=shocked,
            rub_rates_pnl_rub=rub_pnl,
            foreign_rates_pnl_rub=foreign_pnl,
            basis_pnl_rub=basis_pnl,
            fx_spot_pnl_rub=spot_pnl,
            interaction_pnl_rub=finite_money(
                total - math.fsum((rub_pnl, foreign_pnl, basis_pnl, spot_pnl)),
                "interaction PnL",
            ),
            total_pnl_rub=total,
        )

    def second_order(self, *, bump_bp: float = 1.0) -> FxForwardSecondOrderRisk:
        """Central gradient and full Hessian over 23 rate/basis factors plus spot (1 %)."""
        bump = positive_money(bump_bp, "second-order bump bp")
        size = len(RISK_BUCKETS)
        rates = 2 * size + 1

        def moves_of(vector: list[float]) -> _Moves:
            return (tuple(vector[:size]), tuple(vector[size : 2 * size]), vector[2 * size], 0.0)

        def values(vector: list[float]) -> tuple[float, float]:
            return self._values(moves_of(vector))

        # The formulas of the shared central-difference helper (``_central_derivatives``).
        # A factor whose single bumps leave both leg values bit-identical has zero weight
        # at every date the trade reads, so every difference involving it is exactly zero;
        # its pairs are skipped (the result equals the full evaluation, see the tests).
        square = positive_money(bump * bump, "squared second-order bump")
        positive_money(4 * square, "mixed-difference denominator")
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
            up_pv, down_pv = up[0] - up[1], down[0] - down[1]
            gradient[i] = finite_money((up_pv - down_pv) / (2 * bump), "gradient")
            hessian[i][i] = finite_money(
                ((up_pv - base_pv) + (down_pv - base_pv)) / square, "diagonal gamma"
            )
            # PV(S(1+x)) = A(1+x) - B: spot terms are exact in x, central in rates.
            fx_cross = finite_money((up[0] - down[0]) / (2 * bump) / 100.0, "FX cross gamma")
            hessian[i][rates] = hessian[rates][i] = fx_cross
        for position, i in enumerate(active):
            for j in active[:position]:
                corners = []
                for sign_i, sign_j in ((1, 1), (1, -1), (-1, 1), (-1, -1)):
                    pair = [0.0] * rates
                    pair[i], pair[j] = sign_i * bump, sign_j * bump
                    foreign, rub = values(pair)
                    corners.append(foreign - rub)
                mixed = finite_money(
                    ((corners[0] - corners[1]) - (corners[2] - corners[3])) / (4 * square),
                    "cross gamma",
                )
                hessian[i][j] = hessian[j][i] = mixed
        foreign_pv = base[0]
        currency = self.request.instrument.currency
        return FxForwardSecondOrderRisk(
            base_id=self.base_id,
            rub_curve_basis=self.rub_curve_basis,
            foreign_curve_basis=self.foreign_curve_basis,
            factors=(
                *(f"RUB {b}" for b in RISK_BUCKETS),
                *(f"{currency} {b}" for b in RISK_BUCKETS),
                "basis",
                "spot",
            ),
            bump_bp=bump,
            gradient_rub=(*gradient, finite_money(foreign_pv / 100.0, "FX delta RUB")),
            hessian_rub=tuple(tuple(row) for row in hessian),
        )


def fx_forward_factor_risk(
    risk: FxForwardRiskResult,
    rub_curve_id: str,
    foreign_curve_id: str,
    second_order: FxForwardSecondOrderRisk | None = None,
) -> FactorRisk:
    """Map forward/swap risk to ``IR|rub|*``, ``IR|foreign|*``, ``FXBASIS|ccy``, ``FX|ccy``.

    With ``second_order`` the first order is its gradient and the sparse Hessian
    includes the FX x rates cross terms. Otherwise ``risk`` must be central.
    """
    if rub_curve_id == foreign_curve_id:
        raise InvalidTerms("RUB and foreign curves need distinct curve IDs")
    factors = (
        *(ir_factor(rub_curve_id, b) for b in RISK_BUCKETS),
        *(ir_factor(foreign_curve_id, b) for b in RISK_BUCKETS),
        fx_basis_factor(risk.currency),
        fx_factor(risk.currency),
    )
    if second_order is not None:
        if second_order.base_id != risk.base_id:
            raise InvalidTerms("risk and second-order results describe different positions")
        if len(second_order.factors) != len(factors):
            raise InvalidTerms("unexpected second-order factor set")
        return FactorRisk(
            factors,
            second_order.gradient_rub,
            _upper(second_order.hessian_rub),
            "central",
            second_order.bump_bp,
            True,
        )
    if risk.difference != "central":
        raise InvalidTerms("desk factors need central-difference risk (DEC08)")
    first = (
        *(b.dv01_rub for b in risk.rub_buckets),
        *(b.dv01_rub for b in risk.foreign_buckets),
        risk.basis01_rub,
        risk.fx_delta_rub_per_pct,
    )
    return FactorRisk(factors, first, (), "central", risk.bump_bp, False)
