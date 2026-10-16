from __future__ import annotations

import math
from bisect import bisect_left
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date
from typing import Protocol, SupportsInt, cast

from ._solver import bisect
from .conventions import Compounding, CurveConvention, generate_schedule
from .dates import annual_schedule


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


@dataclass(frozen=True)
class DiscountCurve:
    curve_date: date
    nodes: tuple[tuple[date, float], ...]
    model_version: str = "ois-logdf-v1"
    _flat_continuous_rate: float | None = None
    convention: CurveConvention = field(default_factory=CurveConvention)

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
        errors = [abs(self.par_rate(q.maturity) - q.par_rate) * 10_000.0 for q in quotes]
        return max(errors, default=0.0)


@dataclass(frozen=True)
class CurveBootstrapRequest:
    curve_date: date
    quotes: tuple[OisQuote, ...]
    convention: CurveConvention = field(default_factory=CurveConvention)


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


def bootstrap_discount_curve(request: CurveBootstrapRequest) -> DiscountCurve:
    ordered = sorted(request.quotes, key=lambda item: item.maturity)
    if not ordered:
        raise ValueError("at least one OIS quote is required")
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
