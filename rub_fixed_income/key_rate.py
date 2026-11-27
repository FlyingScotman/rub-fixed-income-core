from __future__ import annotations

import math
from dataclasses import dataclass, field, fields, replace
from datetime import date
from functools import lru_cache
from itertools import pairwise
from typing import Literal, cast

import numpy as np

from ._risk_common import (
    _ZERO_SHOCK,
    _curve_identity,
    _shift_at,
    _shifts,
    _snapshot_curve,
    key_rate_weights,
)
from ._state import _fields_only_state
from .canonical import OMIT_IF_DEFAULT, canonical_sha256
from .conventions import DayCount
from .dates import BusinessCalendar, add_months
from .discount import CurveBootstrapRequest, DiscountCurve, DiscountFactorCurve
from .errors import InvalidTerms
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


def rebase_key_rate_curve(
    curve: KeyRateForwardCurve, business_date: date, *, rate_in_force: float
) -> KeyRateForwardCurve:
    """``curve`` re-dated to an earlier ``business_date`` (the portfolio's curve date).

    :func:`build_key_rate_forward_curve` dates its curve at the swaps' TOM start. A
    portfolio values on the snapshot business date, so its key-rate swaps and futures read
    a curve dated that day. Between ``business_date`` and the curve date the key rate in
    force is the one the Bank of Russia has already set: ``rate_in_force`` states it, and
    it must equal the curve's ``current_rate`` (a decision effective on the TOM date itself
    cannot be expressed by extending the current interval and is rejected). The current
    interval is extended back; the forward segments are unchanged. Bucket shifts are then
    read at times from ``business_date``.
    """
    if not isinstance(curve, KeyRateForwardCurve):
        raise TypeError("curve must be a KeyRateForwardCurve")
    if business_date > curve.curve_date:
        raise InvalidTerms("a key-rate curve can only be re-dated to an earlier date")
    if not math.isfinite(rate_in_force) or rate_in_force != curve.current_rate:
        raise InvalidTerms(
            "the key rate in force on the business date differs from the curve's current rate"
        )
    return KeyRateForwardCurve(
        business_date,
        curve.current_rate,
        curve.current_rate_end,
        curve.segments,
        curve.model_version,
    )


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


@lru_cache(maxsize=2048)
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


@lru_cache(maxsize=2048)
def _swap_dates(
    trade_date: date, tenor: str, calendar: BusinessCalendar
) -> tuple[date, date, date]:
    if parse_tenor(tenor).unit in ("ON", "TN", "SN"):
        raise ValueError(f"unsupported tenor: {tenor}")
    dates = resolve_tenor(trade_date, tenor, calendar, spot_lag_days=1)
    return dates.effective_date, dates.unadjusted_end, dates.end_date


def price_key_rate_swap(request: KeyRateSwapPricingRequest) -> KeyRateSwapPricingResult:
    return _price_key_rate_swap(
        request.trade_date,
        request.tenor,
        request.projection_curve,
        request.discount_curve,
        request.calendar,
    )


def _price_key_rate_swap(
    trade_date: date,
    tenor: str,
    projection_curve: KeyRateForwardCurve,
    discount_curve: DiscountFactorCurve,
    calendar: BusinessCalendar,
) -> KeyRateSwapPricingResult:
    effective_date, unadjusted_maturity, maturity_date = _swap_dates(trade_date, tenor, calendar)
    if projection_curve.curve_date != effective_date:
        raise ValueError("KRS projection curve date must equal the TOM effective date")
    if discount_curve.curve_date != effective_date:
        raise ValueError("KRS discount curve date must equal the TOM effective date")
    if projection_curve.horizon_date < maturity_date:
        raise ValueError("KRS projection curve does not cover maturity")

    fixed_periods = _periods(
        effective_date,
        unadjusted_maturity,
        calendar,
        12,
        single_payment_up_to_one_year=True,
    )
    floating_periods = _periods(
        effective_date,
        unadjusted_maturity,
        calendar,
        3,
        single_payment_up_to_one_year=False,
    )
    floating_pv = sum(
        projection_curve.arithmetic_average(period.accrual_start, period.accrual_end)
        * period.accrual_factor
        * discount_curve.discount_factor(period.payment_date)
        for period in floating_periods
    )
    fixed_annuity = sum(
        period.accrual_factor * discount_curve.discount_factor(period.payment_date)
        for period in fixed_periods
    )
    if fixed_annuity <= 0.0:
        raise ValueError("KRS fixed annuity must be positive")
    return KeyRateSwapPricingResult(
        trade_date=trade_date,
        effective_date=effective_date,
        maturity_date=maturity_date,
        tenor=tenor,
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
    request: KeyRateCurveBuildRequest,
    calibration: _Calibration,
) -> KeyRateCurveScenario:
    rates = np.linalg.solve(calibration.exposure, target - calibration.base)
    if not np.all(np.isfinite(rates)):
        raise ValueError(f"{side} KRS calibration produced non-finite rates")
    if float(np.min(rates)) <= request.rate_floor or float(np.max(rates)) >= request.rate_cap:
        raise ValueError(f"{side} KRS calibration produced rates outside the supported domain")
    curve = KeyRateForwardCurve(
        curve_date=calibration.effective_date,
        current_rate=request.current_rate,
        current_rate_end=request.next_policy_effective_date,
        segments=tuple(
            KeyRateForwardSegment(start, end, float(rate), quote.tenor)
            for start, end, rate, quote in zip(
                calibration.state_starts, calibration.maturities, rates, request.quotes, strict=True
            )
        ),
    )
    model = np.asarray(
        [
            _price_key_rate_swap(
                request.trade_date, quote.tenor, curve, calibration.discount, request.calendar
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


@dataclass(frozen=True)
class _Calibration:
    """The quote-independent part of a KRS calibration: rates = solve(exposure, quotes - base)."""

    effective_date: date
    maturities: tuple[date, ...]
    state_starts: tuple[date, ...]
    exposure: np.ndarray
    base: np.ndarray
    rank: int
    singular_values: np.ndarray
    discount: DiscountFactorCurve


def _calibration(
    request: KeyRateCurveBuildRequest, discount: DiscountFactorCurve | None = None
) -> _Calibration:
    if discount is None:
        discount = request.discount_curve
    if not request.quotes:
        raise ValueError("at least one KRS quote is required")
    if len({quote.tenor.strip().upper() for quote in request.quotes}) != len(request.quotes):
        raise ValueError("KRS tenors must be unique")
    effective_date, _, _ = _swap_dates(
        request.trade_date, request.quotes[0].tenor, request.calendar
    )
    if discount.curve_date != effective_date:
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
                _price_key_rate_swap(
                    request.trade_date, quote.tenor, curve, discount, request.calendar
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
    return _Calibration(
        effective_date, maturities, state_starts, exposure, base, rank, singular_values, discount
    )


def build_key_rate_forward_curve(
    request: KeyRateCurveBuildRequest,
) -> KeyRateCurveBuildResult:
    calibration = _calibration(request)
    maturities = calibration.maturities
    exposure = calibration.exposure
    rank = calibration.rank
    singular_values = calibration.singular_values
    inverse = np.linalg.inv(exposure)
    diagnostics = KeyRateCurveDiagnostics(
        equation_count=len(request.quotes),
        variable_count=len(request.quotes),
        rank=rank,
        nullity=len(request.quotes) - rank,
        singular_values=tuple(float(value) for value in singular_values),
        condition_number=float(singular_values[0] / singular_values[-1]),
        inverse_quote_sensitivity=tuple(tuple(float(value) for value in row) for row in inverse),
    )
    bid_target = np.asarray([quote.bid for quote in request.quotes])
    mid_target = np.asarray([quote.mid for quote in request.quotes])
    ask_target = np.asarray([quote.ask for quote in request.quotes])

    bid = _scenario(
        side="all_bid_quotes", target=bid_target, request=request, calibration=calibration
    )
    mid = _scenario(side="mid", target=mid_target, request=request, calibration=calibration)
    ask = _scenario(
        side="all_ask_quotes", target=ask_target, request=request, calibration=calibration
    )
    return KeyRateCurveBuildResult(
        representation="pillar_forward_average",
        effective_date=calibration.effective_date,
        maturities=maturities,
        quotes=request.quotes,
        bid=bid,
        mid=mid,
        ask=ask,
        diagnostics=diagnostics,
        calendar_version=request.calendar.version,
    )


_SHOCKED_CURVES = 4096


@dataclass(frozen=True)
class _TomDiscount:
    curve_date: date
    ruonia: DiscountCurve
    anchor: float

    def discount_factor(self, target: date) -> float:
        return self.ruonia.discount_factor(target) / self.anchor


@dataclass(frozen=True)
class _QuoteRisk:
    calibration: _Calibration
    mid: np.ndarray
    times: tuple[float, ...]
    curve: KeyRateForwardCurve
    jacobian: np.ndarray  # segment forward move per bp of each bucket, in bp
    shocked: dict[tuple[float, ...], KeyRateForwardCurve]


@dataclass(frozen=True)
class KeyRateCurveRequest:
    """KRS quotes of a key-rate projection curve: the source of quote-basis risk.

    ``build`` is the TOM calibration of :func:`build_key_rate_forward_curve`;
    :meth:`curve` is its mid curve re-dated to ``business_date`` at ``rate_in_force``
    (:func:`rebase_key_rate_curve`), the curve every engine prices on. An engine given
    this request instead of a :class:`KeyRateForwardCurve` bumps quotes, not forwards: a
    bucket shift of ``s`` bp moves each KRS mid quote by the shift at its maturity (the
    hats of the risk buckets at ACT/ACT ISDA time from ``business_date``) and recalibrates
    the mid curve (:meth:`shocked`). Bid, ask and the decided rate are held. The
    calibration is linear in the quotes, so each bucket
    moves the forwards by a fixed vector for each RUONIA state (:meth:`shift_weights`).
    A portfolio supplies `ruonia_curve_id` and resolves it to RUONIA OIS quotes.
    Each shocked RUONIA curve is then read forward from TOM for calibration, replacing
    ``build.discount_curve``. Standalone calibration without a link uses that field.
    """

    build: KeyRateCurveBuildRequest
    business_date: date
    rate_in_force: float
    ruonia_curve_id: str | None = field(default=None, metadata=OMIT_IF_DEFAULT)

    def __getstate__(self) -> dict[str, object]:
        state = _fields_only_state(self)
        if self.ruonia_source is not None:
            state["_ruonia_source"] = self.ruonia_source
        return state

    def __post_init__(self) -> None:
        if not isinstance(self.build, KeyRateCurveBuildRequest):
            raise TypeError("build must be a KeyRateCurveBuildRequest")
        if not isinstance(self.business_date, date):
            raise TypeError("business_date must be a date")
        if not math.isfinite(self.rate_in_force):
            raise InvalidTerms("rate in force must be finite")
        if self.ruonia_curve_id is not None and (
            not isinstance(self.ruonia_curve_id, str)
            or not self.ruonia_curve_id.strip()
            or "|" in self.ruonia_curve_id
        ):
            raise InvalidTerms("ruonia_curve_id must be a nonempty curve ID without '|'")
        if type(self.build.quotes) is not tuple:
            object.__setattr__(self, "build", replace(self.build, quotes=tuple(self.build.quotes)))

    @property
    def curve_date(self) -> date:
        return self.business_date

    @property
    def identity(self) -> str:
        """Canonical SHA-256 of the request, computed once."""
        cached = self.__dict__.get("_identity")
        if cached is None:
            ruonia = self.ruonia_source
            cached = canonical_sha256(
                self
                if ruonia is None
                else {
                    "build": {
                        item.name: getattr(self.build, item.name)
                        for item in fields(self.build)
                        if item.name != "discount_curve"
                    },
                    "business_date": self.business_date,
                    "rate_in_force": self.rate_in_force,
                    "ruonia_curve_id": self.ruonia_curve_id,
                    "ruonia": _curve_identity(ruonia),
                }
            )
            object.__setattr__(self, "_identity", cached)
        return cast(str, cached)

    @property
    def ruonia_source(self) -> CurveBootstrapRequest | None:
        return cast("CurveBootstrapRequest | None", self.__dict__.get("_ruonia_source"))

    def bind_ruonia(self, source: object) -> KeyRateCurveRequest:
        """Bind the OIS quote request used by the product and by TOM calibration."""
        if not isinstance(source, CurveBootstrapRequest) or source.curve_date != self.business_date:
            raise InvalidTerms(
                "KRS quote risk needs the linked RUONIA OIS quotes on the business date"
            )
        source = _snapshot_curve(source)
        assert isinstance(source, CurveBootstrapRequest)
        if self.ruonia_source is not None:
            if self.ruonia_source != source:
                raise InvalidTerms("product discount must match the linked RUONIA OIS curve")
            return self
        if self.build.calendar.add_business_days(self.build.trade_date, 1) < self.business_date:
            raise InvalidTerms("KRS TOM calibration date cannot predate the linked RUONIA curve")
        bindings = cast(
            dict[CurveBootstrapRequest, KeyRateCurveRequest],
            self.__dict__.setdefault("_ruonia_bindings", {}),
        )
        bound = bindings.get(source)
        if bound is None:
            bound = replace(self)
            object.__setattr__(bound, "_ruonia_source", source)
            if len(bindings) >= 4:
                del bindings[next(iter(bindings))]
            bindings[source] = bound
        return bound

    def _risk(self, ruonia: DiscountFactorCurve | None = None) -> _QuoteRisk:
        if (
            ruonia is not None or self.ruonia_source is not None or self.ruonia_curve_id is not None
        ) and not isinstance(ruonia, DiscountCurve):
            raise InvalidTerms("linked KRS quotes need a bootstrapped RUONIA curve")
        states = cast(
            dict[DiscountCurve | None, _QuoteRisk],
            self.__dict__.setdefault("_quote_risk", {}),
        )
        assert ruonia is None or isinstance(ruonia, DiscountCurve)
        state = states.get(ruonia)
        if state is not None:
            return state
        build = self.build
        discount = None
        if ruonia is not None:
            if ruonia.curve_date != self.business_date:
                raise InvalidTerms("RUONIA calibration discount must be dated the business date")
            tom = build.calendar.add_business_days(build.trade_date, 1)
            discount = _TomDiscount(tom, ruonia, ruonia.discount_factor(tom))
        calibration = _calibration(build, discount)
        mid = np.asarray([quote.mid for quote in build.quotes])
        curve = self._rebase(self._solve(calibration, mid))
        times = tuple(
            DayCount.ACT_ACT_ISDA.year_fraction(self.business_date, maturity)
            for maturity in calibration.maturities
        )
        weights = np.asarray([key_rate_weights(time) for time in times])
        jacobian = np.linalg.solve(calibration.exposure, weights)
        state = _QuoteRisk(calibration, mid, times, curve, jacobian, {})
        if len(states) < _SHOCKED_CURVES:
            states[ruonia] = state
        return state

    def _solve(self, calibration: _Calibration, target: np.ndarray) -> KeyRateForwardCurve:
        return _scenario(
            side="mid", target=target, request=self.build, calibration=calibration
        ).curve

    def _rebase(self, curve: KeyRateForwardCurve) -> KeyRateForwardCurve:
        try:
            return rebase_key_rate_curve(
                curve, self.business_date, rate_in_force=self.rate_in_force
            )
        except InvalidTerms as exc:
            raise InvalidTerms(
                f"KRS quotes dated {self.business_date.isoformat()}: {exc}"
            ) from None

    def curve(self, *, ruonia: DiscountFactorCurve | None = None) -> KeyRateForwardCurve:
        """The mid curve dated ``business_date``."""
        return self._risk(ruonia).curve

    def shocked(
        self, shifts: tuple[float, ...] = _ZERO_SHOCK, *, ruonia: DiscountFactorCurve | None = None
    ) -> KeyRateForwardCurve:
        """The mid curve recalibrated to the quotes moved by bucket ``shifts`` (bp).

        The same solve as :func:`build_key_rate_forward_curve` on the moved mid quotes,
        with its domain and repricing checks.
        """
        moves = _shifts(tuple(shifts))
        state = self._risk(ruonia)
        if not any(moves):
            return state.curve
        target = tuple(
            float(mid + _shift_at(time, moves) * 1e-4)
            for time, mid in zip(state.times, state.mid, strict=True)
        )
        if tuple(state.mid) == target:
            return state.curve
        cached = state.shocked.get(target)
        if cached is None:
            cached = self._rebase(self._solve(state.calibration, np.asarray(target)))
            if len(state.shocked) < _SHOCKED_CURVES:
                state.shocked[target] = cached
        return cached

    def shift_weights(
        self, start: date, end: date, *, ruonia: DiscountFactorCurve | None = None
    ) -> tuple[float, ...]:
        """Per bucket, the forward key rate summed over the days of ``[start, end)`` after
        the current-rate interval, per unit of quote shift (the unit of
        ``swaps``' forward-bucket weights: the average moves by ``1e-4 * w / days`` per bp).
        """
        state = self._risk(ruonia)
        curve = state.curve
        first = max(start, curve.current_rate_end)
        days = np.zeros(len(curve.segments))
        for index, segment in enumerate(curve.segments):
            overlap = (min(end, segment.end_date) - max(first, segment.start_date)).days
            days[index] = max(overlap, 0)
        return tuple(float(value) for value in days @ state.jacobian)
