from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from itertools import pairwise
from typing import Literal

import numpy as np

from .conventions import DayCount
from .dates import BusinessCalendar, add_months
from .discount import DiscountCurve
from .tenors import parse_tenor, resolve_tenor

MODEL_VERSION = "krs-pillar-forward-average-v1"
CONVENTION_ID = "moex-irs-keyrate-tom-modfol-actactisda-v1"


@dataclass(frozen=True)
class KeyRateForwardSegment:
    start_date: date
    end_date: date
    rate: float
    pillar_tenor: str

    def __post_init__(self) -> None:
        if self.end_date <= self.start_date:
            raise ValueError("key-rate segment must have positive length")
        if not math.isfinite(self.rate):
            raise ValueError("key-rate segment rate must be finite")
        if not self.pillar_tenor:
            raise ValueError("key-rate segment pillar tenor is required")


@dataclass(frozen=True)
class KeyRateForwardCurve:
    curve_date: date
    current_rate: float
    current_rate_end: date
    segments: tuple[KeyRateForwardSegment, ...]
    model_version: str = MODEL_VERSION

    def __post_init__(self) -> None:
        if not math.isfinite(self.current_rate):
            raise ValueError("current key rate must be finite")
        if self.current_rate_end <= self.curve_date:
            raise ValueError("current key-rate interval must have positive length")
        expected_start = self.current_rate_end
        for segment in self.segments:
            if segment.start_date != expected_start:
                raise ValueError("key-rate segments must be contiguous and ordered")
            expected_start = segment.end_date

    @property
    def horizon_date(self) -> date:
        return self.segments[-1].end_date if self.segments else self.current_rate_end

    def rate_at(self, target: date) -> float:
        if target < self.curve_date or target >= self.horizon_date:
            raise ValueError("rate date is outside the key-rate curve horizon")
        if target < self.current_rate_end:
            return self.current_rate
        for segment in self.segments:
            if segment.start_date <= target < segment.end_date:
                return segment.rate
        raise AssertionError("validated key-rate curve must cover its horizon")

    def arithmetic_average(self, start: date, end: date) -> float:
        if start < self.curve_date or end > self.horizon_date or end <= start:
            raise ValueError("average period is outside the key-rate curve horizon")
        total = 0.0
        cursor = start
        while cursor < end:
            if cursor < self.current_rate_end:
                boundary = min(end, self.current_rate_end)
                rate = self.current_rate
            else:
                segment = next(
                    (
                        item
                        for item in self.segments
                        if item.start_date <= cursor < item.end_date
                    ),
                    None,
                )
                if segment is None:
                    raise ValueError("key-rate curve does not cover the average period")
                boundary = min(end, segment.end_date)
                rate = segment.rate
            total += rate * (boundary - cursor).days
            cursor = boundary
        return total / (end - start).days


@dataclass(frozen=True)
class KeyRateSwapQuote:
    tenor: str
    bid: float
    mid: float
    ask: float

    def __post_init__(self) -> None:
        if not self.tenor:
            raise ValueError("KRS tenor is required")
        if not all(math.isfinite(value) for value in (self.bid, self.mid, self.ask)):
            raise ValueError("KRS quote rates must be finite")
        if not self.bid <= self.mid <= self.ask:
            raise ValueError(f"invalid KRS bid/mid/ask for {self.tenor}")


@dataclass(frozen=True)
class KeyRateSwapPricingRequest:
    trade_date: date
    tenor: str
    projection_curve: KeyRateForwardCurve
    discount_curve: DiscountCurve
    calendar: BusinessCalendar


@dataclass(frozen=True)
class KeyRateSwapPeriod:
    accrual_start: date
    accrual_end: date
    payment_date: date
    accrual_factor: float


@dataclass(frozen=True)
class KeyRateSwapPricingResult:
    trade_date: date
    effective_date: date
    maturity_date: date
    tenor: str
    par_rate: float
    floating_pv: float
    fixed_annuity: float
    fixed_periods: tuple[KeyRateSwapPeriod, ...]
    floating_periods: tuple[KeyRateSwapPeriod, ...]
    convention_id: str = CONVENTION_ID


@dataclass(frozen=True)
class KeyRateCurveBuildRequest:
    trade_date: date
    current_rate: float
    next_policy_effective_date: date
    quotes: tuple[KeyRateSwapQuote, ...]
    discount_curve: DiscountCurve
    calendar: BusinessCalendar
    rate_floor: float = -0.05
    rate_cap: float = 0.50
    fit_tolerance_bp: float = 1e-7


@dataclass(frozen=True)
class KeyRateCurveDiagnostics:
    equation_count: int
    variable_count: int
    rank: int
    nullity: int
    singular_values: tuple[float, ...]
    condition_number: float
    inverse_quote_sensitivity: tuple[tuple[float, ...], ...]


@dataclass(frozen=True)
class KeyRateCurveScenario:
    side: str
    curve: KeyRateForwardCurve
    market_rates: tuple[float, ...]
    model_rates: tuple[float, ...]
    repricing_errors_bp: tuple[float, ...]
    max_repricing_error_bp: float
    rmse_bp: float


@dataclass(frozen=True)
class KeyRateCurveBuildResult:
    representation: str
    effective_date: date
    maturities: tuple[date, ...]
    quotes: tuple[KeyRateSwapQuote, ...]
    bid: KeyRateCurveScenario
    mid: KeyRateCurveScenario
    ask: KeyRateCurveScenario
    diagnostics: KeyRateCurveDiagnostics
    calendar_version: str
    convention_id: str = CONVENTION_ID
    model_version: str = MODEL_VERSION
    usable: bool = True


def _year_fraction_act_act_isda(start: date, end: date) -> float:
    if end <= start:
        raise ValueError("accrual period must have positive length")
    return DayCount.ACT_ACT_ISDA.year_fraction(start, end)


def _unadjusted_schedule(start: date, maturity: date, months: int) -> tuple[date, ...]:
    if maturity <= start:
        raise ValueError("maturity must be after effective date")
    backwards = [maturity]
    period_count = 1
    while True:
        cursor = add_months(maturity, -months * period_count)
        if cursor <= start:
            break
        backwards.append(cursor)
        period_count += 1
    return tuple(reversed(backwards))


def _periods(
    effective_date: date,
    unadjusted_maturity: date,
    calendar: BusinessCalendar,
    months: int,
    *,
    single_payment_up_to_one_year: bool,
) -> tuple[KeyRateSwapPeriod, ...]:
    unadjusted_dates: tuple[date, ...]
    if single_payment_up_to_one_year and unadjusted_maturity <= add_months(effective_date, 12):
        unadjusted_dates = (unadjusted_maturity,)
    else:
        unadjusted_dates = _unadjusted_schedule(effective_date, unadjusted_maturity, months)
    adjusted_dates = tuple(calendar.modified_following(value) for value in unadjusted_dates)
    if any(left >= right for left, right in pairwise(adjusted_dates)):
        raise ValueError("adjusted KRS schedule dates must be strictly increasing")
    result: list[KeyRateSwapPeriod] = []
    previous = effective_date
    for payment_date in adjusted_dates:
        if payment_date <= previous:
            raise ValueError("adjusted KRS accrual period must have positive length")
        result.append(
            KeyRateSwapPeriod(
                accrual_start=previous,
                accrual_end=payment_date,
                payment_date=payment_date,
                accrual_factor=_year_fraction_act_act_isda(previous, payment_date),
            )
        )
        previous = payment_date
    return tuple(result)


def _swap_dates(
    trade_date: date, tenor: str, calendar: BusinessCalendar
) -> tuple[date, date, date]:
    if parse_tenor(tenor).unit in ("ON", "TN", "SN"):
        raise ValueError(f"unsupported tenor: {tenor}")
    dates = resolve_tenor(trade_date, tenor, calendar, spot_lag_days=1)
    return dates.effective_date, dates.unadjusted_end, dates.end_date


def price_key_rate_swap(request: KeyRateSwapPricingRequest) -> KeyRateSwapPricingResult:
    effective_date, unadjusted_maturity, maturity_date = _swap_dates(
        request.trade_date, request.tenor, request.calendar
    )
    if request.projection_curve.curve_date != effective_date:
        raise ValueError("KRS projection curve date must equal the TOM effective date")
    if request.discount_curve.curve_date != effective_date:
        raise ValueError("KRS discount curve date must equal the TOM effective date")
    if request.projection_curve.horizon_date < maturity_date:
        raise ValueError("KRS projection curve does not cover maturity")

    fixed_periods = _periods(
        effective_date,
        unadjusted_maturity,
        request.calendar,
        12,
        single_payment_up_to_one_year=True,
    )
    floating_periods = _periods(
        effective_date,
        unadjusted_maturity,
        request.calendar,
        3,
        single_payment_up_to_one_year=False,
    )
    floating_pv = sum(
        request.projection_curve.arithmetic_average(period.accrual_start, period.accrual_end)
        * period.accrual_factor
        * request.discount_curve.discount_factor(period.payment_date)
        for period in floating_periods
    )
    fixed_annuity = sum(
        period.accrual_factor * request.discount_curve.discount_factor(period.payment_date)
        for period in fixed_periods
    )
    if fixed_annuity <= 0.0:
        raise ValueError("KRS fixed annuity must be positive")
    return KeyRateSwapPricingResult(
        trade_date=request.trade_date,
        effective_date=effective_date,
        maturity_date=maturity_date,
        tenor=request.tenor,
        par_rate=floating_pv / fixed_annuity,
        floating_pv=floating_pv,
        fixed_annuity=fixed_annuity,
        fixed_periods=fixed_periods,
        floating_periods=floating_periods,
    )


def _scenario(
    *,
    side: Literal["all_bid_quotes", "mid", "all_ask_quotes"],
    target: np.ndarray,
    exposure: np.ndarray,
    base: np.ndarray,
    request: KeyRateCurveBuildRequest,
    effective_date: date,
    maturities: tuple[date, ...],
    state_starts: tuple[date, ...],
) -> KeyRateCurveScenario:
    rates = np.linalg.solve(exposure, target - base)
    if not np.all(np.isfinite(rates)):
        raise ValueError(f"{side} KRS calibration produced non-finite rates")
    if float(np.min(rates)) <= request.rate_floor or float(np.max(rates)) >= request.rate_cap:
        raise ValueError(f"{side} KRS calibration produced rates outside the supported domain")
    curve = KeyRateForwardCurve(
        curve_date=effective_date,
        current_rate=request.current_rate,
        current_rate_end=request.next_policy_effective_date,
        segments=tuple(
            KeyRateForwardSegment(start, end, float(rate), quote.tenor)
            for start, end, rate, quote in zip(
                state_starts, maturities, rates, request.quotes, strict=True
            )
        ),
    )
    model = np.asarray(
        [
            price_key_rate_swap(
                KeyRateSwapPricingRequest(
                    trade_date=request.trade_date,
                    tenor=quote.tenor,
                    projection_curve=curve,
                    discount_curve=request.discount_curve,
                    calendar=request.calendar,
                )
            ).par_rate
            for quote in request.quotes
        ]
    )
    errors_bp = (model - target) * 10_000.0
    maximum = float(np.max(np.abs(errors_bp)))
    rmse = float(np.sqrt(np.mean(np.square(errors_bp))))
    if maximum > request.fit_tolerance_bp:
        raise ValueError(
            f"{side} KRS calibration failed repricing: max error {maximum:.12g} bp"
        )
    return KeyRateCurveScenario(
        side=side,
        curve=curve,
        market_rates=tuple(float(value) for value in target),
        model_rates=tuple(float(value) for value in model),
        repricing_errors_bp=tuple(float(value) for value in errors_bp),
        max_repricing_error_bp=maximum,
        rmse_bp=rmse,
    )


def build_key_rate_forward_curve(
    request: KeyRateCurveBuildRequest,
) -> KeyRateCurveBuildResult:
    if not request.quotes:
        raise ValueError("at least one KRS quote is required")
    if len({quote.tenor.strip().upper() for quote in request.quotes}) != len(request.quotes):
        raise ValueError("KRS tenors must be unique")
    effective_date, _, _ = _swap_dates(
        request.trade_date, request.quotes[0].tenor, request.calendar
    )
    if request.discount_curve.curve_date != effective_date:
        raise ValueError("KRS discount curve date must equal the TOM effective date")
    maturities = tuple(
        _swap_dates(request.trade_date, quote.tenor, request.calendar)[2]
        for quote in request.quotes
    )
    if len(set(maturities)) != len(maturities):
        raise ValueError("KRS maturities must be unique")
    if any(left >= right for left, right in pairwise(maturities)):
        raise ValueError("KRS quotes must be ordered by increasing maturity")
    if not effective_date < request.next_policy_effective_date < maturities[0]:
        raise ValueError("next policy effective date must be before the first KRS maturity")
    if not request.rate_floor < request.current_rate < request.rate_cap:
        raise ValueError("current key rate is outside the supported domain")

    state_starts = (request.next_policy_effective_date, *maturities[:-1])

    def curve_from(values: np.ndarray, anchor: float) -> KeyRateForwardCurve:
        return KeyRateForwardCurve(
            curve_date=effective_date,
            current_rate=anchor,
            current_rate_end=request.next_policy_effective_date,
            segments=tuple(
                KeyRateForwardSegment(start, end, float(value), quote.tenor)
                for start, end, value, quote in zip(
                    state_starts, maturities, values, request.quotes, strict=True
                )
            ),
        )

    def model(values: np.ndarray, anchor: float) -> np.ndarray:
        curve = curve_from(values, anchor)
        return np.asarray(
            [
                price_key_rate_swap(
                    KeyRateSwapPricingRequest(
                        trade_date=request.trade_date,
                        tenor=quote.tenor,
                        projection_curve=curve,
                        discount_curve=request.discount_curve,
                        calendar=request.calendar,
                    )
                ).par_rate
                for quote in request.quotes
            ]
        )

    zeros = np.zeros(len(request.quotes))
    base = model(zeros, request.current_rate)
    columns: list[np.ndarray] = []
    for index in range(len(request.quotes)):
        basis = np.zeros(len(request.quotes))
        basis[index] = 1.0
        columns.append(model(basis, 0.0))
    exposure = np.column_stack(columns)
    singular_values = np.linalg.svd(exposure, compute_uv=False)
    tolerance = max(exposure.shape) * singular_values[0] * np.finfo(float).eps
    rank = int(np.linalg.matrix_rank(exposure, tol=tolerance))
    if rank != len(request.quotes):
        raise ValueError(
            f"KRS pillar-forward system is rank deficient: rank {rank}/{len(request.quotes)}"
        )
    inverse = np.linalg.inv(exposure)
    diagnostics = KeyRateCurveDiagnostics(
        equation_count=len(request.quotes),
        variable_count=len(request.quotes),
        rank=rank,
        nullity=len(request.quotes) - rank,
        singular_values=tuple(float(value) for value in singular_values),
        condition_number=float(singular_values[0] / singular_values[-1]),
        inverse_quote_sensitivity=tuple(
            tuple(float(value) for value in row) for row in inverse
        ),
    )
    bid_target = np.asarray([quote.bid for quote in request.quotes])
    mid_target = np.asarray([quote.mid for quote in request.quotes])
    ask_target = np.asarray([quote.ask for quote in request.quotes])
    bid = _scenario(
        side="all_bid_quotes",
        target=bid_target,
        exposure=exposure,
        base=base,
        request=request,
        effective_date=effective_date,
        maturities=maturities,
        state_starts=state_starts,
    )
    mid = _scenario(
        side="mid",
        target=mid_target,
        exposure=exposure,
        base=base,
        request=request,
        effective_date=effective_date,
        maturities=maturities,
        state_starts=state_starts,
    )
    ask = _scenario(
        side="all_ask_quotes",
        target=ask_target,
        exposure=exposure,
        base=base,
        request=request,
        effective_date=effective_date,
        maturities=maturities,
        state_starts=state_starts,
    )
    return KeyRateCurveBuildResult(
        representation="pillar_forward_average",
        effective_date=effective_date,
        maturities=maturities,
        quotes=request.quotes,
        bid=bid,
        mid=mid,
        ask=ask,
        diagnostics=diagnostics,
        calendar_version=request.calendar.version,
    )
