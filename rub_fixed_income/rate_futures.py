"""MOEX short-rate futures: one-month RUONIA, RUONIA index and key-rate index futures.

Contract terms come from the MOEX specifications (see ``docs/rate-futures.md``):

- **RUON** (one-month RUONIA, "Фьючерсные контракты на процентные ставки", in force from
  23 March 2026): price ``100 - R``, ``R`` the arithmetic mean of RUONIA over the calendar
  days of the settlement month, which runs from the last trading day of the previous month
  (included) to the last trading day of the expiry month (excluded). A day without a
  RUONIA takes the latest earlier one. Notional 1,000,000 RUB, step 0.01, step value
  ``W = Round(T/365 * N * R/100%; 5)``.
- **RUONIA** (RUONIA index futures, "Фьючерсные контракты на индексы долгового и
  денежного рынка", in force from 14 September 2026): final price = the Bank of Russia
  RUONIA index published on the last trading day, rounded to 4 decimals; step 0.0001,
  step value 1 RUB. The index follows the Bank of Russia methodology of 12 April 2022
  (``I_t = I_T * (1 + RUONIA_T * (d/366 + (t - T - d)/365))``).
- **KEYRATE** (key-rate index futures, same specification): final price = the MOEX
  KEYRATE index on the last trading day, a scheduled Bank of Russia meeting; on a
  scheduled meeting date the index is the key rate set for the first business day after
  it, times 100 (MOEX methodology of 13 August 2026). Step 0.01, step value 10 RUB.

Variation margin is daily and undiscounted (as in ``fx_futures``). Prices before expiry
are expectations under a deterministic projection curve: RUONIA overnight forwards from a
discount curve, or a :class:`KeyRateForwardCurve`. A calibrated basis (bp added to every
projected rate) reprices a market settlement price and is frozen under curve shocks.

:meth:`RateFuturesRiskEngine.horizon` carries a position to a horizon on the two curve
views of ``horizon`` (today's curve read forward, and rolled); see
:class:`RateFuturesHorizonValuation`.
"""

from __future__ import annotations

import math
from bisect import bisect_right
from dataclasses import asdict, dataclass, field, replace
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from itertools import pairwise
from typing import Literal

from ._curve_views import forward_curve, roll_curve, roll_key_rate_curve
from ._money import finite_money, positive_money
from ._risk_common import (
    _ZERO_SHOCK,
    RISK_BUCKETS,
    ShockedCurveCache,
    _base_curve,
    _central_derivatives,
    _curve_basis,
    _curve_identity,
    _shift_at,
    _shifts,
    _shock_curve,
    _snapshot_curve,
)
from ._solver import bisect
from .canonical import canonical_sha256
from .conventions import DayCount, calendar_fingerprint
from .dates import BusinessCalendar
from .discount import CurveBootstrapRequest, DiscountCurve, DiscountFactorCurve
from .errors import InvalidTerms, MissingFixing, MissingMarketData
from .factors import FactorRisk, HessianEntry, ir_factor
from .key_rate import KeyRateForwardCurve
from .risk import BucketRisk

__all__ = [
    "RATE_FUTURES_HORIZON_MODEL_VERSION",
    "RATE_FUTURES_RISK_MODEL_VERSION",
    "RATE_FUTURES_SECOND_ORDER_MODEL_VERSION",
    "KeyRateFuturesContract",
    "RateFuturesHorizonValuation",
    "RateFuturesRiskEngine",
    "RateFuturesRiskRequest",
    "RateFuturesRiskResult",
    "RateFuturesScenario",
    "RateFuturesScenarioResult",
    "RateFuturesSecondOrderRisk",
    "RuoniaAverageFuturesContract",
    "RuoniaFixings",
    "RuoniaIndexFuturesContract",
    "RuoniaIndexValue",
    "cbr_ruonia_year_fraction",
    "futures_basis_factor",
    "key_rate_index_value",
    "rate_futures_factor_risk",
    "rate_futures_horizon",
    "rate_futures_variation_margin",
    "ruonia_average_final_price",
    "ruonia_average_settlement_period",
    "ruonia_index_final_price",
    "ruonia_index_value",
]

RATE_FUTURES_RISK_MODEL_VERSION = "rub-rate-futures-v1"
RATE_FUTURES_SECOND_ORDER_MODEL_VERSION = "rub-rate-futures-second-order-v1"
RATE_FUTURES_HORIZON_MODEL_VERSION = "rub-rate-futures-horizon-v2"


def _round_half_up(value: float | Decimal, decimals: int) -> float:
    number = value if isinstance(value, Decimal) else Decimal(repr(value))
    return float(number.quantize(Decimal(1).scaleb(-decimals), ROUND_HALF_UP))


def _previous_business_day(calendar: BusinessCalendar, value: date) -> date:
    current = value
    while not calendar.is_business_day(current):
        current -= timedelta(days=1)
    return current


def _next_business_day(calendar: BusinessCalendar, value: date) -> date:
    return calendar.add_business_days(value, 1)


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or "|" in value:
        raise InvalidTerms(f"{label} must be nonempty text without '|'")
    return value


def cbr_ruonia_year_fraction(start: date, end: date) -> float:
    """``d/366 + (days - d)/365`` with ``d`` the days of ``[start, end)`` in leap years.

    The Bank of Russia RUONIA index accrues on this basis (methodology of 12 April 2022,
    formula 1); it is ACT/365F unless the interval touches a leap year.
    """
    if end < start:
        raise ValueError("end must not precede start")
    leap = 0
    day = start
    while day < end:
        year_end = date(day.year + 1, 1, 1)
        segment_end = min(end, year_end)
        if day.year % 4 == 0 and (day.year % 100 != 0 or day.year % 400 == 0):
            leap += (segment_end - day).days
        day = segment_end
    return leap / 366.0 + ((end - start).days - leap) / 365.0


# --- fixings -----------------------------------------------------------------------------


@dataclass(frozen=True)
class RuoniaFixings:
    """RUONIA fixings published before ``valuation_date`` (a point-in-time snapshot).

    Rates are decimal (0.1391 = 13.91 %) and keyed by the business day they were
    calculated for. Every business day before ``valuation_date`` needs a fixing, except
    the days in ``unpublished_dates``: days the Bank of Russia declared without a RUONIA
    (it publishes none for the last business day of a year, for example 2025-12-30).
    Those take the latest earlier fixing, as the RUON specification and the RUONIA index
    methodology both prescribe. They must be declared: a gap is never filled silently
    (:class:`MissingFixing`). Passing the publication calendar of ``FixingHistory`` (the
    day marked a holiday) is equivalent. Days on or after ``valuation_date`` are projected.
    """

    valuation_date: date
    fixings: tuple[tuple[date, float], ...]
    calendar: BusinessCalendar
    snapshot_id: str
    unpublished_dates: frozenset[date] = frozenset()
    _rates: dict[date, float] = field(init=False, repr=False, compare=False)
    _dates: tuple[date, ...] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not isinstance(self.calendar, BusinessCalendar):
            raise TypeError("calendar must be a BusinessCalendar")
        _text(self.snapshot_id, "snapshot_id")
        rows = tuple((day, finite_money(rate, "RUONIA fixing")) for day, rate in self.fixings)
        for (left, _), (right, _) in pairwise(rows):
            if right <= left:
                raise InvalidTerms("RUONIA fixing dates must be strictly increasing")
        for day, rate in rows:
            if day >= self.valuation_date:
                raise InvalidTerms("RUONIA fixings must predate the valuation date")
            if not self.calendar.is_business_day(day):
                raise InvalidTerms(f"RUONIA fixing on non-business day {day.isoformat()}")
            if rate <= -1.0:
                raise InvalidTerms("RUONIA fixings must exceed -100 %")
        unpublished = frozenset(self.unpublished_dates)
        for day in unpublished:
            if not self.calendar.is_business_day(day):
                raise InvalidTerms("unpublished RUONIA dates must be business days")
        if unpublished & {day for day, _ in rows}:
            raise InvalidTerms("a date cannot both have a RUONIA fixing and be unpublished")
        object.__setattr__(self, "fixings", rows)
        object.__setattr__(self, "unpublished_dates", unpublished)
        object.__setattr__(self, "_rates", dict(rows))
        object.__setattr__(self, "_dates", tuple(day for day, _ in rows))

    def is_calculation_day(self, day: date) -> bool:
        """A business day for which RUONIA is (or will be) calculated."""
        return self.calendar.is_business_day(day) and day not in self.unpublished_dates

    def known_rate(self, business_day: date) -> float | None:
        """The fixing that applies to ``business_day``; None on or after the valuation date.

        Raises :class:`MissingMarketData` for an undeclared gap before the valuation date.
        """
        if business_day >= self.valuation_date:
            return None
        rate = self._rates.get(business_day)
        if rate is not None:
            return rate
        if business_day in self.unpublished_dates:
            position = bisect_right(self._dates, business_day) - 1
            if position < 0:
                raise MissingFixing(
                    f"no RUONIA fixing before unpublished day {business_day.isoformat()}"
                )
            return self.fixings[position][1]
        raise MissingFixing(f"RUONIA fixing for {business_day.isoformat()} is missing")

    @property
    def identity(self) -> dict[str, object]:
        return {
            "valuation_date": self.valuation_date,
            "snapshot_id": self.snapshot_id,
            "calendar_version": self.calendar.version,
            "calendar_fingerprint": calendar_fingerprint(self.calendar),
            "fixings_sha256": canonical_sha256(self.fixings),
            "unpublished_dates": sorted(self.unpublished_dates),
        }


@dataclass(frozen=True)
class RuoniaIndexValue:
    """A Bank of Russia RUONIA index value published for a business day ``index_date``."""

    index_date: date
    value: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "value", positive_money(self.value, "RUONIA index"))


# --- contracts -----------------------------------------------------------------------------


@dataclass(frozen=True)
class RuoniaAverageFuturesContract:
    """MOEX one-month RUONIA futures (RUON): price = 100 - mean RUONIA in percent.

    ``period_start`` is the last trading day of the previous month (included) and
    ``period_end`` the last trading day of the expiry month (excluded), which is also the
    last trading day; :func:`ruonia_average_settlement_period` derives both from the
    trading calendar. ``final_price_decimals`` = 2 rounds the final settlement price to
    the 0.01 price step half up: ISS final settlement prices of RRZ5..RRU6 show it, the
    specification does not state it.
    """

    contract_id: str
    period_start: date
    period_end: date
    notional_rub: float = 1_000_000.0
    price_step_pct: float = 0.01
    final_price_decimals: int = 2

    def __post_init__(self) -> None:
        _text(self.contract_id, "contract_id")
        if self.period_end <= self.period_start:
            raise InvalidTerms("settlement period must have positive length")
        object.__setattr__(self, "notional_rub", positive_money(self.notional_rub, "notional"))
        object.__setattr__(
            self, "price_step_pct", positive_money(self.price_step_pct, "price step")
        )

    @property
    def last_trading_day(self) -> date:
        return self.period_end

    @property
    def period_days(self) -> int:
        return (self.period_end - self.period_start).days

    @property
    def step_value_rub(self) -> float:
        """``W = Round(T/365 * N * R/100%; 5)`` from the specification."""
        return _round_half_up(
            Decimal(self.period_days)
            / Decimal(365)
            * Decimal(repr(self.notional_rub))
            * Decimal(repr(self.price_step_pct))
            / Decimal(100),
            5,
        )

    @property
    def margin_per_point_rub(self) -> float:
        """``Round(W / R; 5)``: RUB of variation margin per contract per 1.00 of price."""
        return _round_half_up(
            Decimal(repr(self.step_value_rub)) / Decimal(repr(self.price_step_pct)), 5
        )


def ruonia_average_settlement_period(
    year: int, month: int, calendar: BusinessCalendar
) -> tuple[date, date]:
    """``(period_start, period_end)`` of the RUON contract expiring in ``year``-``month``.

    The last trading day of the previous month and of the expiry month, on the exchange
    trading calendar. The exchange may move a last trading day by decision; pass the
    contract dates directly then.
    """

    def last_trading_day(y: int, m: int) -> date:
        first_next = date(y + 1, 1, 1) if m == 12 else date(y, m + 1, 1)
        return _previous_business_day(calendar, first_next - timedelta(days=1))

    previous = (year - 1, 12) if month == 1 else (year, month - 1)
    return last_trading_day(*previous), last_trading_day(year, month)


@dataclass(frozen=True)
class RuoniaIndexFuturesContract:
    """MOEX RUONIA index futures: final price = the RUONIA index on the last trading day.

    The last trading day is the first trading day of March, June, September and December.
    Price in index points (4 decimals), step 0.0001, step value 1 RUB.
    """

    contract_id: str
    last_trading_day: date
    price_step: float = 0.0001
    step_value_rub: float = 1.0
    final_price_decimals: int = 4

    def __post_init__(self) -> None:
        _text(self.contract_id, "contract_id")
        object.__setattr__(self, "price_step", positive_money(self.price_step, "price step"))
        object.__setattr__(
            self, "step_value_rub", positive_money(self.step_value_rub, "step value")
        )

    @property
    def margin_per_point_rub(self) -> float:
        return self.step_value_rub / self.price_step


@dataclass(frozen=True)
class KeyRateFuturesContract:
    """MOEX KEYRATE futures: final price = the KEYRATE index on a scheduled meeting date.

    ``meeting_date`` is the last trading day (a scheduled Bank of Russia key-rate meeting);
    ``effective_date`` is the first business day after it, from which the decided rate
    applies and whose rate the index takes on the meeting date. Step 0.01, step value 10 RUB.
    """

    contract_id: str
    meeting_date: date
    effective_date: date
    price_step: float = 0.01
    step_value_rub: float = 10.0

    def __post_init__(self) -> None:
        _text(self.contract_id, "contract_id")
        if self.effective_date <= self.meeting_date:
            raise InvalidTerms("the decided key rate applies after the meeting date")
        object.__setattr__(self, "price_step", positive_money(self.price_step, "price step"))
        object.__setattr__(
            self, "step_value_rub", positive_money(self.step_value_rub, "step value")
        )

    @property
    def last_trading_day(self) -> date:
        return self.meeting_date

    @property
    def margin_per_point_rub(self) -> float:
        return self.step_value_rub / self.price_step


RateFuturesContract = (
    RuoniaAverageFuturesContract | RuoniaIndexFuturesContract | KeyRateFuturesContract
)


def rate_futures_variation_margin(
    contract: RateFuturesContract, previous_price: float, price: float, quantity: float = 1.0
) -> float:
    """Signed RUB variation margin of ``quantity`` long contracts from one price to the next.

    RUON follows its specification: ``Round(P * Round(W/R; 5); 2)`` for each price, then
    the difference. The index specifications state ``(P - P_prev) * W / R`` without
    rounding; the clearing rules may round to kopecks.
    """
    previous_price = finite_money(previous_price, "previous price")
    price = finite_money(price, "price")
    quantity = finite_money(quantity, "quantity")
    if isinstance(contract, RuoniaAverageFuturesContract):
        unit = Decimal(repr(contract.margin_per_point_rub))
        new = Decimal(repr(price)) * unit
        old = Decimal(repr(previous_price)) * unit
        per_contract = _round_half_up(new, 2) - _round_half_up(old, 2)
        return finite_money(per_contract * quantity, "variation margin")
    return finite_money(
        (price - previous_price) * contract.margin_per_point_rub * quantity, "variation margin"
    )


# --- final settlement and index reproduction ---------------------------------------------


def _daily_rates(
    fixings: RuoniaFixings, start: date, end: date
) -> tuple[tuple[date, float | None], ...]:
    """Each calendar day of ``[start, end)`` with its business-day observation date."""
    rows = []
    day = start
    while day < end:
        observation = _previous_business_day(fixings.calendar, day)
        rows.append((observation, fixings.known_rate(observation)))
        day += timedelta(days=1)
    return tuple(rows)


def ruonia_average_final_price(
    contract: RuoniaAverageFuturesContract, fixings: RuoniaFixings
) -> float:
    """Final settlement price ``100 - sum(RUONIA_i) / T`` from published fixings.

    Every day of the settlement period must be fixed (the valuation date after the
    period's last observation). Computed in decimal arithmetic and rounded half up to
    ``final_price_decimals``.
    """
    rows = _daily_rates(fixings, contract.period_start, contract.period_end)
    total = Decimal(0)
    for observation, rate in rows:
        if rate is None:
            raise MissingFixing(f"RUONIA for {observation.isoformat()} is not yet published")
        total += Decimal(repr(rate))
    average_pct = total * 100 / len(rows)
    return _round_half_up(Decimal(100) - average_pct, contract.final_price_decimals)


def _index_segments(
    fixings: RuoniaFixings, start: date, end: date
) -> tuple[tuple[date, date], ...]:
    """Calculation days ``T`` in ``[start, end)`` and the end of the span each one accrues.

    A business day without RUONIA (declared unpublished) is not a calculation day: the
    previous one accrues across it by simple interest (methodology, item 2.2).
    """
    starts = []
    day = start
    while day < end:
        if fixings.is_calculation_day(day):
            starts.append(day)
        day += timedelta(days=1)
    if not starts or starts[0] != start:
        raise InvalidTerms("the index anchor must be a RUONIA calculation day")
    spans = []
    for i, first in enumerate(starts):
        last = starts[i + 1] if i + 1 < len(starts) else end
        spans.append((first, last))
    return tuple(spans)


def ruonia_index_value(anchor: RuoniaIndexValue, fixings: RuoniaFixings, target: date) -> float:
    """The RUONIA index on ``target`` from a published value and the fixings after it.

    ``I_t = I_T * (1 + RUONIA_T * (d/366 + (t - T - d)/365))`` chained over calculation
    days; every fixing in ``[anchor, target)`` must be published.
    """
    if target < anchor.index_date:
        raise InvalidTerms("target precedes the index anchor")
    value = anchor.value
    if target == anchor.index_date:
        return value
    for first, last in _index_segments(fixings, anchor.index_date, target):
        rate = fixings.known_rate(first)
        if rate is None:
            raise MissingFixing(f"RUONIA for {first.isoformat()} is not yet published")
        value *= 1.0 + rate * cbr_ruonia_year_fraction(first, last)
    return value


def ruonia_index_final_price(
    contract: RuoniaIndexFuturesContract, anchor: RuoniaIndexValue, fixings: RuoniaFixings
) -> float:
    """Final settlement price: the index on the last trading day, rounded half up to 4 places."""
    value = ruonia_index_value(anchor, fixings, contract.last_trading_day)
    return _round_half_up(value, contract.final_price_decimals)


def key_rate_index_value(
    day: date,
    key_rate_history: tuple[tuple[date, float], ...],
    scheduled_meetings: frozenset[date],
    calendar: BusinessCalendar,
) -> float:
    """The MOEX KEYRATE index on business day ``day`` (methodology of 13 August 2026).

    ``key_rate_history`` holds ``(effective_date, decimal rate)`` steps as the Bank of
    Russia publishes them. On a scheduled meeting date the index takes the rate set for
    the first business day after it; otherwise the rate in force on ``day``. Result in
    points (rate * 100), rounded to 0.01.
    """
    rows = tuple(sorted(key_rate_history))
    if not rows:
        raise MissingMarketData("key-rate history is empty")
    target = _next_business_day(calendar, day) if day in scheduled_meetings else day
    position = bisect_right([d for d, _ in rows], target) - 1
    if position < 0:
        raise MissingMarketData(f"no key rate in force on {target.isoformat()}")
    return _round_half_up(Decimal(repr(rows[position][1])) * 100, 2)


# --- risk engine ---------------------------------------------------------------------------

ProjectionCurve = DiscountCurve | CurveBootstrapRequest | KeyRateForwardCurve
_Moves = tuple[tuple[float, ...], float]
_NO_MOVE: _Moves = (_ZERO_SHOCK, 0.0)


@dataclass(frozen=True)
class RateFuturesRiskRequest:
    """One short-rate futures position; quantity is signed contracts.

    RUONIA contracts need a discount-curve projection (``DiscountCurve`` or an OIS
    ``CurveBootstrapRequest``) and ``fixings`` dated the curve date; the index contract
    also needs ``index_anchor``, a published index value on or before the curve date.
    The key-rate contract needs a :class:`KeyRateForwardCurve`. Supply exactly one of
    ``futures_price`` (a market settlement price, calibrates the basis) and ``basis_bp``.
    """

    contract: RateFuturesContract
    projection_curve: ProjectionCurve
    fixings: RuoniaFixings | None = None
    index_anchor: RuoniaIndexValue | None = None
    futures_price: float | None = None
    basis_bp: float | None = None
    quantity: float = 1.0

    def __post_init__(self) -> None:
        contract, curve = self.contract, self.projection_curve
        if not isinstance(
            contract,
            (RuoniaAverageFuturesContract, RuoniaIndexFuturesContract, KeyRateFuturesContract),
        ):
            raise TypeError("contract must be a MOEX rate futures contract")
        if isinstance(contract, KeyRateFuturesContract):
            if not isinstance(curve, KeyRateForwardCurve):
                raise InvalidTerms("key-rate futures project on a KeyRateForwardCurve")
            if self.fixings is not None or self.index_anchor is not None:
                raise InvalidTerms("key-rate futures take no RUONIA fixings")
            if contract.effective_date < curve.curve_date:
                raise InvalidTerms("the key-rate futures have expired")
            if contract.effective_date >= curve.horizon_date:
                raise MissingMarketData("key-rate curve does not reach the effective date")
        else:
            if not isinstance(curve, (DiscountCurve, CurveBootstrapRequest)):
                raise InvalidTerms("RUONIA futures project on a discount curve or OIS request")
            if not isinstance(self.fixings, RuoniaFixings):
                raise MissingMarketData("RUONIA futures need RUONIA fixings")
            if self.fixings.valuation_date != curve.curve_date:
                raise InvalidTerms("fixings must be dated the projection curve date")
            if isinstance(contract, RuoniaIndexFuturesContract):
                if not isinstance(self.index_anchor, RuoniaIndexValue):
                    raise MissingMarketData("RUONIA index futures need a published index value")
                if self.index_anchor.index_date > curve.curve_date:
                    raise InvalidTerms("the index anchor cannot postdate the valuation date")
                if contract.last_trading_day < self.index_anchor.index_date:
                    raise InvalidTerms("the RUONIA index futures have expired")
            elif self.index_anchor is not None:
                raise InvalidTerms("one-month RUONIA futures take no index anchor")
            object.__setattr__(self, "projection_curve", _snapshot_curve(curve))
        if (self.futures_price is None) == (self.basis_bp is None):
            raise InvalidTerms("supply exactly one of futures_price and basis_bp")
        if self.futures_price is not None:
            object.__setattr__(
                self, "futures_price", finite_money(self.futures_price, "futures price")
            )
        if self.basis_bp is not None:
            object.__setattr__(self, "basis_bp", finite_money(self.basis_bp, "basis bp"))
        object.__setattr__(self, "quantity", finite_money(self.quantity, "quantity"))

    @property
    def valuation_date(self) -> date:
        return self.projection_curve.curve_date


@dataclass(frozen=True)
class RateFuturesScenario:
    """Absolute bp shifts of the 11 projection-curve buckets and of the basis."""

    curve_shifts_bp: tuple[float, ...] = _ZERO_SHOCK
    basis_shift_bp: float = 0.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "curve_shifts_bp", _shifts(tuple(self.curve_shifts_bp)))
        object.__setattr__(
            self, "basis_shift_bp", finite_money(self.basis_shift_bp, "basis shift bp")
        )

    @classmethod
    def parallel(cls, shift_bp: float = 0.0, basis_shift_bp: float = 0.0) -> RateFuturesScenario:
        return cls((shift_bp,) * len(RISK_BUCKETS), basis_shift_bp)


@dataclass(frozen=True)
class RateFuturesRiskResult:
    """Signed RUB variation-margin sensitivities of the position per 1 bp.

    ``expected_rate`` is the projected mean RUONIA (RUON), the expected key rate
    (KEYRATE) or, for the index, the simple annual rate implied between the index anchor
    and the last trading day. ``fixed_days`` / ``projected_days`` count calendar days of
    the rate period already fixed and still projected.
    """

    base_id: str
    contract_id: str
    curve_basis: str
    difference: str
    bump_bp: float
    futures_price: float
    theoretical_price: float
    basis_bp: float
    expected_rate: float
    fixed_days: int
    projected_days: int
    margin_per_point_rub: float
    buckets: tuple[BucketRisk, ...]
    dv01_rub: float
    parallel_dv01_rub: float
    basis01_rub: float
    model_version: str = RATE_FUTURES_RISK_MODEL_VERSION


@dataclass(frozen=True)
class RateFuturesScenarioResult:
    base_id: str
    scenario_id: str
    base_price: float
    shocked_price: float
    rates_pnl_rub: float
    basis_pnl_rub: float
    interaction_pnl_rub: float
    total_pnl_rub: float
    model_version: str = RATE_FUTURES_RISK_MODEL_VERSION


@dataclass(frozen=True)
class RateFuturesSecondOrderRisk:
    """Central gradient (RUB/bp) and full symmetric Hessian (RUB/bp^2): 11 buckets + basis."""

    base_id: str
    factors: tuple[str, ...]
    bump_bp: float
    gradient_rub_per_bp: tuple[float, ...]
    hessian_rub_per_bp2: tuple[tuple[float, ...], ...]
    model_version: str = RATE_FUTURES_SECOND_ORDER_MODEL_VERSION


@dataclass(frozen=True)
class RateFuturesHorizonValuation:
    """Carry and rolldown of the position from the valuation date to ``horizon_date``.

    Prices are model prices (basis held), unrounded. A rate observed before the horizon
    (a RUONIA business day ``T < horizon``, an index span starting before it, a key rate
    effective before it) is realised at today's projection in both views; later rates are
    projected on the view: ``price_forward`` on today's curve read forward (today's
    forwards realised, so it equals ``price_start`` up to rounding) and ``price_rolled``
    on the rolled curve (shape held in calendar days). A contract whose last trading day
    is on or before the horizon settles on the realised path: both prices agree.

    The position is margined daily: ``pv_start_rub`` and ``received_rub`` are zero and the
    variation margin to the horizon is the price change, ``carry = scale * (price_forward
    - price_start)``, ``rolldown = scale * (price_rolled - price_forward)`` and
    ``horizon_pnl = scale * (price_rolled - price_start)``, ``scale = quantity *
    margin_per_point``.
    """

    base_id: str
    contract_id: str
    valuation_date: date
    horizon_date: date
    quantity: float
    price_start: float
    price_forward: float
    price_rolled: float
    pv_start_rub: float
    received_rub: float
    carry_rub: float
    rolldown_rub: float
    horizon_pnl_rub: float
    model_version: str = RATE_FUTURES_HORIZON_MODEL_VERSION


@dataclass(frozen=True)
class _Plan:
    """The rate period split into fixed and projected pieces (no market data)."""

    fixed_sum: Decimal  # RUON: sum of fixed daily rates
    fixed_growth: float  # index: product of the fixed growth factors
    projected: tuple[tuple[date, date, int, float], ...]  # (T, T_next, weight days, tau)
    fixed_days: int
    projected_days: int
    dates: tuple[date, ...] = ()  # every date whose discount factor a repricing reads

    def __post_init__(self) -> None:
        dates = sorted({d for first, last, _, _ in self.projected for d in (first, last)})
        object.__setattr__(self, "dates", tuple(dates))


@dataclass(frozen=True)
class RateFuturesRiskEngine:
    """Theoretical price, calibrated basis, bucket risk, scenarios and Hessian.

    The price is a deterministic function of the projection curve and the basis; the
    position's variation margin is ``quantity * margin_per_point * dF``. Curves are
    shocked exactly as for bonds (zero-rate overlay, or quote shift and rebootstrap for
    an OIS request). A :class:`KeyRateForwardCurve` takes the bucket shift interpolated
    at the effective date's ACT/ACT ISDA time from the curve date, as in ``swaps``; a
    rate already decided (before ``current_rate_end``) does not move.
    """

    request: RateFuturesRiskRequest
    curve_cache: ShockedCurveCache = field(
        default_factory=ShockedCurveCache, compare=False, repr=False
    )
    basis_bp: float = field(init=False)
    theoretical_price: float = field(init=False)
    base_price: float = field(init=False)
    base_id: str = field(init=False)
    _plan: _Plan | None = field(init=False, repr=False)
    _curve: DiscountCurve | None = field(init=False, repr=False)
    _memo: dict[_Moves, float] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        req = self.request
        if not isinstance(self.curve_cache, ShockedCurveCache):
            raise TypeError("curve_cache must be a ShockedCurveCache")
        object.__setattr__(self, "_memo", {})
        curve = req.projection_curve
        object.__setattr__(
            self,
            "_curve",
            None
            if isinstance(curve, KeyRateForwardCurve)
            else _base_curve(curve, self.curve_cache),
        )
        object.__setattr__(self, "_plan", self._build_plan())
        object.__setattr__(self, "basis_bp", 0.0)
        theoretical = self._price(_NO_MOVE)
        object.__setattr__(self, "theoretical_price", theoretical)
        basis = req.basis_bp
        if basis is None:
            assert req.futures_price is not None
            basis = self._calibrate(req.futures_price)
        object.__setattr__(self, "basis_bp", finite_money(basis, "calibrated basis"))
        self._memo.clear()
        object.__setattr__(self, "base_price", self._price(_NO_MOVE))
        object.__setattr__(self, "base_id", self._identity())

    # -- construction helpers

    def _build_plan(self) -> _Plan | None:
        req = self.request
        contract = req.contract
        if isinstance(contract, KeyRateFuturesContract):
            return None
        fixings = req.fixings
        assert fixings is not None
        if isinstance(contract, RuoniaAverageFuturesContract):
            total = Decimal(0)
            fixed = 0
            weights: dict[date, int] = {}
            for observation, rate in _daily_rates(
                fixings, contract.period_start, contract.period_end
            ):
                if rate is None:
                    weights[observation] = weights.get(observation, 0) + 1
                else:
                    total += Decimal(repr(rate))
                    fixed += 1
            projected = tuple(
                (day, end, weight, cbr_ruonia_year_fraction(day, end))
                for day, weight in weights.items()
                for end in (self._projection_end(day),)
            )
            return _Plan(total, 1.0, projected, fixed, sum(weights.values()))
        anchor = req.index_anchor
        assert anchor is not None
        growth = 1.0
        spans = []
        fixed_days = projected_days = 0
        if contract.last_trading_day > anchor.index_date:
            for first, last in _index_segments(
                fixings, anchor.index_date, contract.last_trading_day
            ):
                rate = fixings.known_rate(first)
                days = (last - first).days
                if rate is None:
                    spans.append((first, last, days, cbr_ruonia_year_fraction(first, last)))
                    projected_days += days
                else:
                    growth *= 1.0 + rate * cbr_ruonia_year_fraction(first, last)
                    fixed_days += days
        return _Plan(Decimal(0), growth, tuple(spans), fixed_days, projected_days)

    def _projection_end(self, business_day: date) -> date:
        fixings = self.request.fixings
        assert fixings is not None
        day = business_day + timedelta(days=1)
        while not fixings.is_calculation_day(day):
            day += timedelta(days=1)
        return day

    def _calibrate(self, market: float) -> float:
        if self._plan is not None and not self._plan.projected:
            raise InvalidTerms(
                "the rate period is fully fixed: the price does not depend on a basis; "
                "pass basis_bp=0"
            )

        def error(basis: float) -> float:
            object.__setattr__(self, "basis_bp", basis)
            self._memo.clear()
            return self._price(_NO_MOVE) - market

        root = bisect(
            error, -5_000.0, 5_000.0, tolerance=1e-12, x_tolerance=1e-12, label="rate futures basis"
        ).root
        return root

    def _identity(self) -> str:
        req = self.request
        contract = asdict(req.contract)
        contract["kind"] = type(req.contract).__name__
        curve = req.projection_curve
        curve_id: object = (
            canonical_sha256(curve)
            if isinstance(curve, KeyRateForwardCurve)
            else _curve_identity(curve)
        )
        return canonical_sha256(
            {
                "model": RATE_FUTURES_RISK_MODEL_VERSION,
                "contract": contract,
                "curve_basis": self.curve_basis,
                "curve": curve_id,
                "fixings": None if req.fixings is None else req.fixings.identity,
                "index_anchor": None if req.index_anchor is None else asdict(req.index_anchor),
                "input_futures_price": req.futures_price,
                "input_basis_bp": req.basis_bp,
                "calibrated_basis_bp": self.basis_bp,
                "quantity": req.quantity,
            }
        )

    # -- pricing

    @property
    def curve_basis(self) -> str:
        curve = self.request.projection_curve
        if isinstance(curve, KeyRateForwardCurve):
            return "key_rate_forward"
        return _curve_basis(curve)

    @property
    def _scale(self) -> float:
        return self.request.quantity * self.request.contract.margin_per_point_rub

    def for_quantity(self, quantity: float) -> RateFuturesRiskEngine:
        """The same contract and market for another position size, sharing every repricing."""
        twin = RateFuturesRiskEngine.__new__(RateFuturesRiskEngine)
        for name in (
            "curve_cache",
            "basis_bp",
            "theoretical_price",
            "base_price",
            "_plan",
            "_curve",
            "_memo",
        ):
            object.__setattr__(twin, name, getattr(self, name))
        object.__setattr__(twin, "request", replace(self.request, quantity=quantity))
        object.__setattr__(twin, "base_id", twin._identity())
        return twin

    def _price(self, moves: _Moves) -> float:
        cached = self._memo.get(moves)
        if cached is not None:
            return cached
        try:
            value = self._raw_price(moves)
        except (OverflowError, ZeroDivisionError) as exc:
            raise ValueError("shock is outside the supported numerical domain") from exc
        if len(self._memo) < 8192:
            self._memo[moves] = value
        return value

    def _raw_price(self, moves: _Moves) -> float:
        shifts, basis_shift = moves
        basis = finite_money(self.basis_bp + basis_shift, "shocked basis") * 1e-4
        req = self.request
        contract = req.contract
        if isinstance(contract, KeyRateFuturesContract):
            curve = req.projection_curve
            assert isinstance(curve, KeyRateForwardCurve)
            effective = contract.effective_date
            rate = curve.rate_at(effective) + basis
            if effective >= curve.current_rate_end:  # a decided rate does not move
                time = DayCount.ACT_ACT_ISDA.year_fraction(curve.curve_date, effective)
                rate += _shift_at(time, shifts) * 1e-4
            return finite_money(rate * 100.0, "key-rate futures price")
        plan = self._plan
        source = req.projection_curve
        assert plan is not None and self._curve is not None
        assert not isinstance(source, KeyRateForwardCurve)
        view: DiscountFactorCurve = _shock_curve(source, self._curve, shifts, self.curve_cache)
        dfs = {day: view.discount_factor(day) for day in plan.dates}
        # Projected RUONIA_T = (DF(T) / DF(T_next) - 1) / tau(T, T_next) + basis.
        if isinstance(contract, RuoniaAverageFuturesContract):
            projected = math.fsum(
                ((dfs[first] / dfs[last] - 1.0) / tau + basis) * weight
                for first, last, weight, tau in plan.projected
            )
            mean = (float(plan.fixed_sum) + projected) / contract.period_days
            return finite_money(100.0 - mean * 100.0, "RUONIA futures price")
        anchor = req.index_anchor
        assert anchor is not None
        growth = plan.fixed_growth
        for first, last, _, tau in plan.projected:
            growth *= dfs[first] / dfs[last] + basis * tau
        return positive_money(anchor.value * growth, "RUONIA index futures price")

    # -- outputs

    def _expected_rate(self) -> float:
        contract = self.request.contract
        price = self.base_price
        if isinstance(contract, RuoniaAverageFuturesContract):
            return (100.0 - price) / 100.0
        if isinstance(contract, KeyRateFuturesContract):
            return price / 100.0
        anchor = self.request.index_anchor
        assert anchor is not None
        tau = cbr_ruonia_year_fraction(anchor.index_date, contract.last_trading_day)
        return (price / anchor.value - 1.0) / tau if tau > 0.0 else 0.0

    def _days(self) -> tuple[int, int]:
        if self._plan is None:
            return 0, 0
        return self._plan.fixed_days, self._plan.projected_days

    def risk(
        self, *, bump_bp: float = 1.0, difference: Literal["forward", "central"] = "forward"
    ) -> RateFuturesRiskResult:
        """Signed RUB/bp per bucket, an independently repriced parallel shift and basis01."""
        bump = positive_money(bump_bp, "risk bump bp")
        if difference not in ("forward", "central"):
            raise ValueError("difference must be forward or central")

        def unit(moves: _Moves) -> float:
            up = self._price(moves)
            if difference == "central":
                shifts, basis = moves
                down = self._price((tuple(-s for s in shifts), -basis))
                return finite_money((up - down) / (2.0 * bump), "unit sensitivity")
            return finite_money((up - self.base_price) / bump, "unit sensitivity")

        rows = []
        size = len(RISK_BUCKETS)
        for i, bucket in enumerate(RISK_BUCKETS):
            shifts = tuple(bump if i == j else 0.0 for j in range(size))
            sensitivity = unit((shifts, 0.0))
            rows.append(
                BucketRisk(
                    bucket,
                    finite_money(sensitivity * self._scale, "bucket DV01"),
                    finite_money(-sensitivity / self.base_price * 10_000.0, "price duration"),
                )
            )
        fixed, projected = self._days()
        return RateFuturesRiskResult(
            base_id=self.base_id,
            contract_id=self.request.contract.contract_id,
            curve_basis=self.curve_basis,
            difference=difference,
            bump_bp=bump,
            futures_price=self.base_price,
            theoretical_price=self.theoretical_price,
            basis_bp=self.basis_bp,
            expected_rate=self._expected_rate(),
            fixed_days=fixed,
            projected_days=projected,
            margin_per_point_rub=self.request.contract.margin_per_point_rub,
            buckets=tuple(rows),
            dv01_rub=finite_money(math.fsum(b.dv01_rub for b in rows), "DV01"),
            parallel_dv01_rub=finite_money(
                unit(((bump,) * size, 0.0)) * self._scale, "parallel DV01"
            ),
            basis01_rub=finite_money(unit((_ZERO_SHOCK, bump)) * self._scale, "basis01"),
        )

    def reprice(self, scenario: RateFuturesScenario) -> RateFuturesScenarioResult:
        """Exact variation margin of the scenario, rates and basis isolated."""
        shocked = self._price((scenario.curve_shifts_bp, scenario.basis_shift_bp))

        def pnl(moves: _Moves, label: str) -> float:
            return finite_money((self._price(moves) - self.base_price) * self._scale, label)

        rates = pnl((scenario.curve_shifts_bp, 0.0), "rates PnL")
        basis = pnl((_ZERO_SHOCK, scenario.basis_shift_bp), "basis PnL")
        total = finite_money((shocked - self.base_price) * self._scale, "total PnL")
        return RateFuturesScenarioResult(
            base_id=self.base_id,
            scenario_id=canonical_sha256(scenario),
            base_price=self.base_price,
            shocked_price=shocked,
            rates_pnl_rub=rates,
            basis_pnl_rub=basis,
            interaction_pnl_rub=finite_money(total - rates - basis, "interaction PnL"),
            total_pnl_rub=total,
        )

    def second_order(self, *, bump_bp: float = 1.0) -> RateFuturesSecondOrderRisk:
        """Central gradient and full Hessian over the 11 curve buckets and the basis."""
        bump = positive_money(bump_bp, "second-order bump bp")
        size = len(RISK_BUCKETS)

        def price(moves: list[float]) -> float:
            return self._price((tuple(moves[:size]), moves[size]))

        gradient, hessian = _central_derivatives(
            price, size + 1, bump, self.base_price, self._scale
        )
        return RateFuturesSecondOrderRisk(
            base_id=self.base_id,
            factors=(*RISK_BUCKETS, "basis"),
            bump_bp=bump,
            gradient_rub_per_bp=gradient,
            hessian_rub_per_bp2=hessian,
        )

    # -- horizon

    def _horizon_price(
        self, horizon: date, view: DiscountFactorCurve | KeyRateForwardCurve
    ) -> float:
        """The price with rates observed from ``horizon`` on read off ``view``.

        Earlier rates (and every rate of a contract settled by the horizon) keep today's
        projection; the basis is held. Each term is computed as in ``_raw_price``.
        """
        basis = self.basis_bp * 1e-4
        req = self.request
        contract = req.contract
        settled = contract.last_trading_day <= horizon
        if isinstance(contract, KeyRateFuturesContract):
            today = req.projection_curve
            assert isinstance(today, KeyRateForwardCurve)
            assert isinstance(view, KeyRateForwardCurve)
            effective = contract.effective_date
            source = today
            if not (settled or effective < horizon or effective < today.current_rate_end):
                if effective >= view.horizon_date:
                    raise MissingMarketData(
                        "rolled key-rate curve does not reach the effective date"
                    )
                source = view
            rate = source.rate_at(effective) + basis
            return finite_money(rate * 100.0, "key-rate futures price")
        plan, now = self._plan, self._curve
        assert plan is not None and now is not None
        assert not isinstance(view, KeyRateForwardCurve)

        def ratio(first: date, last: date) -> float:
            curve = now if settled or first < horizon else view
            return curve.discount_factor(first) / curve.discount_factor(last)

        if isinstance(contract, RuoniaAverageFuturesContract):
            projected = math.fsum(
                ((ratio(first, last) - 1.0) / tau + basis) * weight
                for first, last, weight, tau in plan.projected
            )
            mean = (float(plan.fixed_sum) + projected) / contract.period_days
            return finite_money(100.0 - mean * 100.0, "RUONIA futures price")
        anchor = req.index_anchor
        assert anchor is not None
        growth = plan.fixed_growth
        for first, last, _, tau in plan.projected:
            growth *= ratio(first, last) + basis * tau
        return positive_money(anchor.value * growth, "RUONIA index futures price")

    def horizon(self, horizon_date: date) -> RateFuturesHorizonValuation:
        """Carry and rolldown to ``horizon_date`` (see :class:`RateFuturesHorizonValuation`)."""
        req = self.request
        valuation = req.valuation_date
        if not isinstance(horizon_date, date) or horizon_date <= valuation:
            raise InvalidTerms("horizon date must be after the valuation date")
        curve = req.projection_curve
        forward: DiscountFactorCurve | KeyRateForwardCurve
        rolled: DiscountFactorCurve | KeyRateForwardCurve
        if isinstance(curve, KeyRateForwardCurve):
            forward, rolled = curve, roll_key_rate_curve(curve, horizon_date)
        else:
            assert self._curve is not None
            forward = forward_curve(self._curve, horizon_date)
            rolled = roll_curve(self._curve, horizon_date)
        try:
            price_forward = self._horizon_price(horizon_date, forward)
            price_rolled = self._horizon_price(horizon_date, rolled)
        except (OverflowError, ZeroDivisionError) as exc:
            raise ValueError("horizon is outside the supported numerical domain") from exc
        start, scale = self.base_price, self._scale
        return RateFuturesHorizonValuation(
            base_id=self.base_id,
            contract_id=req.contract.contract_id,
            valuation_date=valuation,
            horizon_date=horizon_date,
            quantity=req.quantity,
            price_start=start,
            price_forward=price_forward,
            price_rolled=price_rolled,
            pv_start_rub=0.0,
            received_rub=0.0,
            carry_rub=finite_money(scale * (price_forward - start), "carry"),
            rolldown_rub=finite_money(scale * (price_rolled - price_forward), "rolldown"),
            horizon_pnl_rub=finite_money(scale * (price_rolled - start), "horizon P&L"),
        )


def rate_futures_horizon(
    request: RateFuturesRiskRequest,
    *,
    horizon_date: date,
    curve_cache: ShockedCurveCache | None = None,
) -> RateFuturesHorizonValuation:
    """Carry and rolldown of a short-rate futures position to ``horizon_date``."""
    engine = RateFuturesRiskEngine(
        request, ShockedCurveCache() if curve_cache is None else curve_cache
    )
    return engine.horizon(horizon_date)


# --- desk factors ---------------------------------------------------------------------------


def futures_basis_factor(contract_id: str) -> str:
    """``FUTBASIS|<contract_id>``: 1 bp of a calibrated futures basis (frozen under shocks)."""
    return f"FUTBASIS|{_text(contract_id, 'contract_id')}"


def _upper(matrix: tuple[tuple[float, ...], ...]) -> tuple[HessianEntry, ...]:
    return tuple(
        HessianEntry(i, j, row[j])
        for i, row in enumerate(matrix)
        for j in range(i, len(row))
        if row[j] != 0.0
    )


def rate_futures_factor_risk(
    risk: RateFuturesRiskResult,
    curve_id: str,
    second_order: RateFuturesSecondOrderRisk | None = None,
) -> FactorRisk:
    """``IR|<curve_id>|<bucket>`` and ``FUTBASIS|<contract>``, central (DEC08)."""
    factors = (
        *(ir_factor(curve_id, b) for b in RISK_BUCKETS),
        futures_basis_factor(risk.contract_id),
    )
    if second_order is not None:
        if second_order.base_id != risk.base_id:
            raise InvalidTerms("risk and second-order results describe different calibrations")
        return FactorRisk(
            factors,
            second_order.gradient_rub_per_bp,
            _upper(second_order.hessian_rub_per_bp2),
            "central",
            second_order.bump_bp,
            True,
        )
    if risk.difference != "central":
        raise InvalidTerms("desk factors need central-difference risk (DEC08)")
    first = (*(b.dv01_rub for b in risk.buckets), risk.basis01_rub)
    return FactorRisk(factors, first, (), "central", risk.bump_bp, False)
