from __future__ import annotations

import math
from bisect import bisect_left
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Protocol, SupportsInt, cast

from ._solver import bisect
from .conventions import (
    Compounding,
    CurveConvention,
    OisSwapConvention,
    OisSwapPeriod,
    generate_schedule,
)
from .dates import annual_schedule
from .errors import InvalidTerms


class _CompoundingLike(Protocol):
    periods_per_year: int


class DiscountFactorCurve(Protocol):
    """Minimal curve interface for settlement pricing, including shocked curves."""

    @property
    def curve_date(self) -> date: ...

    def discount_factor(self, target: date) -> float: ...


@dataclass(frozen=True)
class OisQuote:
    tenor: str
    maturity: date
    par_rate: float


_DF_MEMO_SIZE = 4096


def _fields_only_state(self: Any) -> dict[str, object]:
    """Pickle state without the lazily built caches kept beside the fields."""
    names = self.__dataclass_fields__
    return {k: v for k, v in self.__dict__.items() if k in names}


@dataclass(frozen=True)
class DiscountCurve:
    curve_date: date
    nodes: tuple[tuple[date, float], ...]
    model_version: str = "ois-logdf-v1"
    _flat_continuous_rate: float | None = None
    convention: CurveConvention = field(default_factory=CurveConvention)

    __getstate__ = _fields_only_state

    def __post_init__(self) -> None:
        previous = self.curve_date
        for node_date, discount_factor in self.nodes:
            if node_date <= previous:
                raise ValueError("discount-curve dates must increase")
            if not math.isfinite(discount_factor) or discount_factor <= 0.0:
                raise ValueError("discount factors must be positive and finite")
            previous = node_date
        if self._flat_continuous_rate is not None and not math.isfinite(self._flat_continuous_rate):
            raise ValueError("flat continuous rate must be finite")

    @classmethod
    def flat(
        cls,
        curve_date: date,
        continuous_rate: float,
        *,
        convention: CurveConvention | None = None,
    ) -> DiscountCurve:
        return cls(
            curve_date,
            (),
            "flat-continuous-v1",
            continuous_rate,
            CurveConvention() if convention is None else convention,
        )

    def _log_nodes(self) -> tuple[tuple[date, ...], tuple[float, ...]]:
        # Lazily cached outside the dataclass fields (equality, hashing and asdict
        # ignore it). The same math.log values as a linear scan: results are identical.
        cached = self.__dict__.get("_log_node_cache")
        if cached is None:
            cached = (
                tuple(node_date for node_date, _ in self.nodes),
                tuple(math.log(df) for _, df in self.nodes),
            )
            object.__setattr__(self, "_log_node_cache", cached)
        return cast(tuple[tuple[date, ...], tuple[float, ...]], cached)

    def _time(self, start: date, end: date) -> float:
        return self.convention.time_day_count.year_fraction(start, end)

    @property
    def valuation_date(self) -> date:
        return self.curve_date

    @property
    def flat_continuous_rate(self) -> float | None:
        """The continuous rate of a ``DiscountCurve.flat`` curve, ``None`` for a node curve."""
        return self._flat_continuous_rate

    def discount_factor(self, target: date) -> float:
        # Shared shocked curves are read at the same payment dates by every position;
        # a bounded per-curve memo (outside the fields) returns the identical float.
        memo = self.__dict__.get("_df_memo")
        if memo is not None:
            cached = memo.get(target)
            if cached is not None:
                return cast(float, cached)
        value = self._discount_factor(target)
        if memo is None:
            memo = {}
            object.__setattr__(self, "_df_memo", memo)
        if len(memo) < _DF_MEMO_SIZE:
            memo[target] = value
        return value

    def _discount_factor(self, target: date) -> float:
        if target < self.curve_date:
            raise ValueError("cannot discount before curve date")
        if target == self.curve_date:
            return 1.0
        if self._flat_continuous_rate is not None:
            return math.exp(-self._flat_continuous_rate * self._time(self.curve_date, target))
        if not self.nodes:
            raise ValueError("discount curve has no nodes")
        dates, log_dfs = self._log_nodes()
        index = bisect_left(dates, target)
        if index < len(dates):
            right_date, right_log_df = dates[index], log_dfs[index]
            if index == 0:
                left_date, left_log_df = self.curve_date, 0.0
            else:
                left_date, left_log_df = dates[index - 1], log_dfs[index - 1]
            weight = (target - left_date).days / (right_date - left_date).days
            return math.exp(left_log_df + weight * (right_log_df - left_log_df))
        if len(self.nodes) == 1:
            prior_date, prior_log_df = self.curve_date, 0.0
        else:
            prior_date, prior_df = self.nodes[-2]
            prior_log_df = math.log(prior_df)
        last_date, last_df = self.nodes[-1]
        flat_forward = -(math.log(last_df) - prior_log_df) / self._time(prior_date, last_date)
        return last_df * math.exp(-flat_forward * self._time(last_date, target))

    def fixed_leg_schedule(self, maturity: date) -> tuple[date, ...]:
        schedule = self.convention.fixed_leg_schedule
        if schedule is None:
            return annual_schedule(self.curve_date, maturity)
        return generate_schedule(self.curve_date, maturity, schedule)

    def par_rate(self, maturity: date) -> float:
        previous = self.curve_date
        annuity = 0.0
        day_count = self.convention.fixed_leg_day_count
        for payment_date in self.fixed_leg_schedule(maturity):
            accrual = day_count.year_fraction(previous, payment_date)
            annuity += accrual * self.discount_factor(payment_date)
            previous = payment_date
        return (1.0 - self.discount_factor(maturity)) / annuity

    def par_ois_rate(self, maturity: date) -> float:
        return self.par_rate(maturity)

    def ois_swap_par_rate(self, periods: Iterable[OisSwapPeriod]) -> float:
        """Par fixed rate of the swap with these periods under ``convention.ois_swap``.

        ``sum(fixed DCF x DF(pay)) x rate = sum((DF(s) / DF(e) - 1) x scale x DF(pay))``,
        with the floating ``scale`` of :meth:`OisSwapConvention.float_amount_scale`.
        """
        ois = self._require_ois_swap()
        annuity = 0.0
        floating = 0.0
        for period in periods:
            pay = self.discount_factor(period.payment)
            annuity += ois.fixed_day_count.year_fraction(period.start, period.end) * pay
            growth = self.discount_factor(period.start) / self.discount_factor(period.end)
            floating += (growth - 1.0) * ois.float_amount_scale(period.start, period.end) * pay
        if annuity <= 0.0:
            raise ValueError("OIS swap has no fixed-leg annuity")
        return floating / annuity

    def quote_par_rate(self, quote: OisQuote) -> float:
        """Model par rate of ``quote`` under this curve's quote convention.

        Legacy convention: :meth:`par_rate` at the quote maturity. With
        ``convention.ois_swap``: the spot-starting swap of the quote's tenor traded on
        the curve date, whose adjusted end must be the quote maturity.
        """
        if self.convention.ois_swap is None:
            return self.par_rate(quote.maturity)
        return self.ois_swap_par_rate(ois_quote_periods(self.curve_date, quote, self.convention))

    def _require_ois_swap(self) -> OisSwapConvention:
        ois = self.convention.ois_swap
        if ois is None:
            raise ValueError("curve convention has no ois_swap quote convention")
        return ois

    def zero_rate(self, target: date, periods_per_year: object = 1) -> float:
        """Zero rate from curve date; ``periods_per_year`` is an int or a Compounding.

        ``Compounding`` instances (including ``CONTINUOUS``) are honoured; plain integers must
        be >= 1 as before.
        """
        if target <= self.curve_date:
            raise ValueError("target and compounding frequency must be positive")
        time = self._time(self.curve_date, target)
        if isinstance(periods_per_year, Compounding) and periods_per_year.is_continuous:
            return periods_per_year.rate_from_discount_factor(self.discount_factor(target), time)
        frequency = (
            int(cast(_CompoundingLike, periods_per_year).periods_per_year)
            if hasattr(periods_per_year, "periods_per_year")
            else int(cast(SupportsInt, periods_per_year))
        )
        if time <= 0.0 or frequency < 1:
            raise ValueError("target and compounding frequency must be positive")
        discount_factor = self.discount_factor(target)
        return float(frequency * (discount_factor ** (-1.0 / (frequency * time)) - 1.0))

    def forward_rate(self, start: date, end: date) -> float:
        if end <= start:
            raise ValueError("forward period must be positive")
        tau = self._time(start, end)
        if tau <= 0.0:
            raise ValueError("forward period must be positive")
        return float((self.discount_factor(start) / self.discount_factor(end)) ** (1.0 / tau) - 1.0)

    def max_repricing_error_bp(self, quotes: Iterable[OisQuote]) -> float:
        errors = [abs(self.quote_par_rate(q) - q.par_rate) * 10_000.0 for q in quotes]
        return max(errors, default=0.0)


@dataclass(frozen=True)
class CurveBootstrapRequest:
    curve_date: date
    quotes: tuple[OisQuote, ...]
    convention: CurveConvention = field(default_factory=CurveConvention)

    __getstate__ = _fields_only_state


def _check_quotes(curve_date: date, ordered: list[OisQuote]) -> None:
    previous = curve_date
    for quote in ordered:
        if not math.isfinite(quote.par_rate):
            raise ValueError(f"OIS par rate must be finite for {quote.tenor}")
        if quote.maturity <= previous:
            raise ValueError("OIS maturities must be unique and increasing")
        previous = quote.maturity


def _dynamic_bracket(
    function: Callable[[float], float], lower: float = -0.50, upper: float = 2.00
) -> tuple[float, float]:
    for _ in range(12):
        try:
            low_value = function(lower)
            high_value = function(upper)
        except OverflowError:
            low_value, high_value = math.inf, -math.inf
        if math.isfinite(low_value) and math.isfinite(high_value) and low_value * high_value <= 0.0:
            return lower, upper
        lower *= 2.0
        upper *= 2.0
    raise ValueError("OIS bootstrap root is not bracketed within the supported numerical domain")


def ois_quote_periods(
    trade_date: date, quote: OisQuote, convention: CurveConvention
) -> tuple[OisSwapPeriod, ...]:
    """Periods of the spot-starting swap behind ``quote`` under ``convention.ois_swap``.

    The tenor resolves with :func:`rub_fixed_income.tenors.resolve_tenor` (spot lag and
    business-day convention of the swap convention); its adjusted end must equal
    ``quote.maturity``. Overnight codes (``ON``, ``TN``, ``SN``) are not swap tenors.
    """
    from .tenors import parse_tenor, resolve_tenor  # tenors imports this module

    ois = convention.ois_swap
    if ois is None:
        raise ValueError("curve convention has no ois_swap quote convention")
    if parse_tenor(quote.tenor).unit in ("ON", "TN", "SN"):
        raise InvalidTerms(f"{quote.tenor} is not a spot-starting OIS tenor")
    dates = resolve_tenor(
        trade_date,
        quote.tenor,
        ois.calendar,
        spot_lag_days=ois.spot_lag_business_days,
        business_day_convention=ois.business_day_convention,
    )
    if dates.end_date != quote.maturity:
        raise InvalidTerms(
            f"OIS quote {quote.tenor} matures on {quote.maturity.isoformat()}, but the tenor "
            f"resolves to {dates.end_date.isoformat()}"
        )
    return ois.periods(dates.effective_date, dates.unadjusted_end, dates.end_date)


# Coupled spot-OIS bootstrap: a quote's last payment (end + payment lag) lies past its
# node, so solving a later node moves an earlier quote slightly; repeat the sequential
# solve with the other nodes held until every quote reprices within this tolerance.
_SPOT_OIS_TOLERANCE_BP = 1e-9
_SPOT_OIS_MAX_PASSES = 50


def _bootstrap_spot_ois(request: CurveBootstrapRequest, ordered: list[OisQuote]) -> DiscountCurve:
    convention = request.convention
    legs = [ois_quote_periods(request.curve_date, quote, convention) for quote in ordered]
    nodes: list[tuple[date, float]] = []

    def solve(index: int, later: tuple[tuple[date, float], ...]) -> tuple[date, float]:
        quote, periods = ordered[index], legs[index]
        earlier = tuple(nodes[:index])
        previous_date, previous_df = earlier[-1] if earlier else (request.curve_date, 1.0)
        interval = convention.time_day_count.year_fraction(previous_date, quote.maturity)

        def error(forward: float) -> float:
            exponent = max(-700.0, min(700.0, -forward * interval))
            node = (quote.maturity, previous_df * math.exp(exponent))
            candidate = DiscountCurve(
                request.curve_date, earlier + (node,) + later, convention=convention
            )
            return candidate.ois_swap_par_rate(periods) - quote.par_rate

        lower, upper = _dynamic_bracket(error)
        root = bisect(error, lower, upper, tolerance=1e-14, label="OIS bootstrap").root
        return quote.maturity, previous_df * math.exp(-root * interval)

    for index in range(len(ordered)):
        nodes.append(solve(index, ()))
    for _ in range(_SPOT_OIS_MAX_PASSES):
        curve = DiscountCurve(request.curve_date, tuple(nodes), convention=convention)
        if curve.max_repricing_error_bp(ordered) <= _SPOT_OIS_TOLERANCE_BP:
            return curve
        for index in range(len(ordered)):
            nodes[index] = solve(index, tuple(nodes[index + 1 :]))
    raise RuntimeError("spot-starting OIS bootstrap did not converge")


def bootstrap_discount_curve(request: CurveBootstrapRequest) -> DiscountCurve:
    """Exact par bootstrap of log-linear discount factors on the quote maturities.

    With the legacy convention (``convention.ois_swap is None``) the numbers are those
    of every release since 0.2.1. With ``ois_swap`` each quote is the spot-starting swap
    of its tenor on the curve date (spot and payment lags, adjusted periods, stubs).
    """
    ordered = sorted(request.quotes, key=lambda item: item.maturity)
    if not ordered:
        raise ValueError("at least one OIS quote is required")
    if request.convention.ois_swap is not None:
        _check_quotes(request.curve_date, ordered)
        curve = _bootstrap_spot_ois(request, ordered)
        if curve.max_repricing_error_bp(ordered) > 1e-7:
            raise RuntimeError("bootstrapped OIS curve failed exact repricing")
        return curve
    nodes: list[tuple[date, float]] = []
    for quote in ordered:
        if not math.isfinite(quote.par_rate):
            raise ValueError(f"OIS par rate must be finite for {quote.tenor}")
        if quote.maturity <= request.curve_date or (nodes and quote.maturity <= nodes[-1][0]):
            raise ValueError("OIS maturities must be unique and increasing")
        previous_date, previous_df = nodes[-1] if nodes else (request.curve_date, 1.0)
        interval = request.convention.time_day_count.year_fraction(previous_date, quote.maturity)
        existing = tuple(nodes)

        def error(
            forward: float,
            previous_df: float = previous_df,
            interval: float = interval,
            existing: tuple[tuple[date, float], ...] = existing,
            maturity: date = quote.maturity,
            par_rate: float = quote.par_rate,
        ) -> float:
            exponent = max(-700.0, min(700.0, -forward * interval))
            terminal_df = previous_df * math.exp(exponent)
            candidate = DiscountCurve(
                request.curve_date,
                existing + ((maturity, terminal_df),),
                convention=request.convention,
            )
            return candidate.par_rate(maturity) - par_rate

        lower, upper = _dynamic_bracket(error)
        solved_forward = bisect(error, lower, upper, tolerance=1e-14, label="OIS bootstrap").root
        nodes.append((quote.maturity, previous_df * math.exp(-solved_forward * interval)))
    curve = DiscountCurve(request.curve_date, tuple(nodes), convention=request.convention)
    if curve.max_repricing_error_bp(ordered) > 1e-7:
        raise RuntimeError("bootstrapped OIS curve failed exact repricing")
    return curve
