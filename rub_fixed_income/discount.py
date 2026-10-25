from __future__ import annotations

import math
from bisect import bisect_left
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date
from functools import lru_cache
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


def _fixed_leg_schedule(
    curve_date: date, convention: CurveConvention, maturity: date
) -> tuple[date, ...]:
    schedule = convention.fixed_leg_schedule
    if schedule is None:
        return annual_schedule(curve_date, maturity)
    return generate_schedule(curve_date, maturity, schedule)


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
        return _fixed_leg_schedule(self.curve_date, self.convention, maturity)

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


# One bootstrap root search evaluates its objective ~50 times. ``_NodeCurve`` gives the
# discount factors those evaluations read without building a ``DiscountCurve`` per
# trial: the same float operations as ``DiscountCurve._discount_factor``, with
# everything that does not move with the solved node computed once, and the objectives
# add their terms in the same order. The bisection then sees bit-identical objective
# values and returns bit-identical nodes (``tests/test_bootstrap_exact.py``).
_Interpolated = tuple[int, int, int, float]  # position, left slot, right slot, weight
_Extrapolated = tuple[int, int, int, float, float]  # position, prior, last, tau, tau to target


class _NodeCurve:
    """``DiscountCurve(curve_date, nodes, convention=...).discount_factor`` at ``targets``
    while the DF of node ``moving`` varies (its value in ``nodes`` is ignored).

    ``values`` holds the DFs at the targets; :meth:`move` updates in place those that
    depend on the moving node, none of them before position ``first_moving``.
    """

    def __init__(
        self,
        curve_date: date,
        nodes: list[tuple[date, float]],
        moving: int,
        convention: CurveConvention,
        targets: Sequence[date],
    ) -> None:
        self._slot = slot = moving + 1  # slot 0 is the curve date (log DF 0.0)
        self._logs = [
            0.0,
            *(0.0 if i == moving else math.log(df) for i, (_, df) in enumerate(nodes)),
        ]
        self._dfs = [1.0, *(df for _, df in nodes)]
        dates = [node_date for node_date, _ in nodes]
        time = convention.time_day_count.year_fraction
        self.values = [1.0] * len(targets)
        self._interpolated: list[_Interpolated] = []
        self._extrapolated: list[_Extrapolated] = []
        for position, target in enumerate(targets):
            if target < curve_date:
                raise ValueError("cannot discount before curve date")
            if target == curve_date:
                continue
            index = bisect_left(dates, target)
            if index < len(dates):
                left_date = dates[index - 1] if index else curve_date
                weight = (target - left_date).days / (dates[index] - left_date).days
                interpolated = (position, index, index + 1, weight)
                if slot in (index, index + 1) or not self._fill(self._interpolate, interpolated):
                    self._interpolated.append(interpolated)
            else:
                prior_date = dates[-2] if len(dates) > 1 else curve_date
                last = len(dates)
                tau = time(prior_date, dates[-1])
                extrapolated = (position, last - 1, last, tau, time(dates[-1], target))
                if slot in (last - 1, last) or not self._fill(self._extrapolate, extrapolated):
                    self._extrapolated.append(extrapolated)
        moving_positions = [p[0] for p in self._interpolated] + [p[0] for p in self._extrapolated]
        self.first_moving = min(moving_positions, default=len(targets))

    def _fill(self, value: Callable[[Any], float], plan: _Interpolated | _Extrapolated) -> bool:
        # A DF that overflows is left to every trial, where the curve would raise it too.
        try:
            self.values[plan[0]] = value(plan)
        except OverflowError:
            return False
        return True

    def _interpolate(self, plan: _Interpolated) -> float:
        _, left, right, weight = plan
        logs = self._logs
        return math.exp(logs[left] + weight * (logs[right] - logs[left]))

    def _extrapolate(self, plan: _Extrapolated) -> float:
        _, prior, last, tau, tau_target = plan
        flat_forward = -(self._logs[last] - self._logs[prior]) / tau
        return self._dfs[last] * math.exp(-flat_forward * tau_target)

    def move(self, discount_factor: float) -> list[float]:
        """Set the moving node's DF (validated as ``DiscountCurve`` does); returns ``values``."""
        if not math.isfinite(discount_factor) or discount_factor <= 0.0:
            raise ValueError("discount factors must be positive and finite")
        logs, values, exp = self._logs, self.values, math.exp
        logs[self._slot] = math.log(discount_factor)
        self._dfs[self._slot] = discount_factor
        # ``_interpolate`` and ``_extrapolate`` inlined: this runs on every trial.
        for position, left, right, weight in self._interpolated:
            values[position] = exp(logs[left] + weight * (logs[right] - logs[left]))
        for position, prior, last, tau, tau_target in self._extrapolated:
            flat_forward = -(logs[last] - logs[prior]) / tau
            values[position] = self._dfs[last] * exp(-flat_forward * tau_target)
        return values


# Coupled spot-OIS bootstrap: a quote's last payment (end + payment lag) lies past its
# node, so solving a later node moves an earlier quote slightly; repeat the sequential
# solve with the other nodes held until every quote reprices within this tolerance.
_SPOT_OIS_TOLERANCE_BP = 1e-9
_SPOT_OIS_MAX_PASSES = 50


_SpotOisLayout = tuple[
    tuple[tuple[OisSwapPeriod, ...], ...],  # periods of each quote's swap
    tuple[tuple[date, ...], ...],  # their (start, end, payment) dates, flattened
    tuple[tuple[tuple[float, float], ...], ...],  # fixed DCF and floating scale per period
]


@lru_cache(maxsize=256)
def _spot_ois_layout(
    curve_date: date, convention: CurveConvention, quotes: tuple[tuple[str, date], ...]
) -> _SpotOisLayout:
    """Swap periods and their day-count terms, as ``ois_swap_par_rate`` reads them.

    They depend on the tenors and maturities only, so every shocked rebuild of one
    request shares them.
    """
    ois = convention.ois_swap
    assert ois is not None
    legs = tuple(
        ois_quote_periods(curve_date, OisQuote(tenor, maturity, 0.0), convention)
        for tenor, maturity in quotes
    )
    targets = tuple(
        tuple(d for p in periods for d in (p.start, p.end, p.payment)) for periods in legs
    )
    terms = tuple(
        tuple(
            (
                ois.fixed_day_count.year_fraction(p.start, p.end),
                ois.float_amount_scale(p.start, p.end),
            )
            for p in periods
        )
        for periods in legs
    )
    return legs, targets, terms


def _bootstrap_spot_ois(request: CurveBootstrapRequest, ordered: list[OisQuote]) -> DiscountCurve:
    convention = request.convention
    legs, targets, terms = _spot_ois_layout(
        request.curve_date, convention, tuple((q.tenor, q.maturity) for q in ordered)
    )
    nodes: list[tuple[date, float]] = []

    def solve(index: int, later: list[tuple[date, float]]) -> tuple[date, float]:
        quote = ordered[index]
        earlier = nodes[:index]
        previous_date, previous_df = earlier[-1] if earlier else (request.curve_date, 1.0)
        interval = convention.time_day_count.year_fraction(previous_date, quote.maturity)
        trial = _NodeCurve(
            request.curve_date,
            [*earlier, (quote.maturity, 1.0), *later],
            index,
            convention,
            targets[index],
        )
        # Periods before the first moving DF add the same terms on every trial.
        rows = terms[index]
        first = trial.first_moving // 3
        dfs = trial.values
        fixed_annuity = fixed_floating = 0.0
        for period, (fixed_fraction, scale) in enumerate(rows[:first]):
            start, end, pay = dfs[3 * period : 3 * period + 3]
            fixed_annuity += fixed_fraction * pay
            fixed_floating += (start / end - 1.0) * scale * pay

        def error(forward: float) -> float:
            exponent = max(-700.0, min(700.0, -forward * interval))
            dfs = trial.move(previous_df * math.exp(exponent))
            annuity, floating = fixed_annuity, fixed_floating
            for period in range(first, len(rows)):
                fixed_fraction, scale = rows[period]
                start, end, pay = dfs[3 * period : 3 * period + 3]
                annuity += fixed_fraction * pay
                floating += (start / end - 1.0) * scale * pay
            if annuity <= 0.0:
                raise ValueError("OIS swap has no fixed-leg annuity")
            return floating / annuity - quote.par_rate

        lower, upper = _dynamic_bracket(error)
        root = bisect(error, lower, upper, tolerance=1e-14, label="OIS bootstrap").root
        return quote.maturity, previous_df * math.exp(-root * interval)

    for index in range(len(ordered)):
        nodes.append(solve(index, []))
    for _ in range(_SPOT_OIS_MAX_PASSES):
        curve = DiscountCurve(request.curve_date, tuple(nodes), convention=convention)
        # ``max_repricing_error_bp`` on the periods already resolved.
        errors = (
            abs(curve.ois_swap_par_rate(p) - q.par_rate) * 10_000.0 for q, p in zip(ordered, legs)
        )
        if max(errors) <= _SPOT_OIS_TOLERANCE_BP:
            return curve
        for index in range(len(ordered)):
            nodes[index] = solve(index, nodes[index + 1 :])
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
        # Returned only once every quote reprices within 1e-9 bp (the 1e-7 bp check).
        return _bootstrap_spot_ois(request, ordered)
    nodes: list[tuple[date, float]] = []
    day_count = request.convention.fixed_leg_day_count
    for quote in ordered:
        if not math.isfinite(quote.par_rate):
            raise ValueError(f"OIS par rate must be finite for {quote.tenor}")
        if quote.maturity <= request.curve_date or (nodes and quote.maturity <= nodes[-1][0]):
            raise ValueError("OIS maturities must be unique and increasing")
        previous_date, previous_df = nodes[-1] if nodes else (request.curve_date, 1.0)
        interval = request.convention.time_day_count.year_fraction(previous_date, quote.maturity)
        # ``DiscountCurve.par_rate`` on the trial curve: its fixed-leg dates, then the
        # maturity; accruals before the first moving DF add the same terms every trial.
        schedule = _fixed_leg_schedule(request.curve_date, request.convention, quote.maturity)
        trial = _NodeCurve(
            request.curve_date,
            [*nodes, (quote.maturity, 1.0)],
            len(nodes),
            request.convention,
            [*schedule, quote.maturity],
        )
        accruals = [
            day_count.year_fraction(start, end)
            for start, end in zip((request.curve_date, *schedule), schedule)
        ]
        first = min(trial.first_moving, len(accruals))
        fixed_annuity = 0.0
        for accrual, df in zip(accruals[:first], trial.values):
            fixed_annuity += accrual * df

        def error(
            forward: float,
            previous_df: float = previous_df,
            interval: float = interval,
            trial: _NodeCurve = trial,
            accruals: list[float] = accruals,
            first: int = first,
            fixed_annuity: float = fixed_annuity,
            par_rate: float = quote.par_rate,
        ) -> float:
            exponent = max(-700.0, min(700.0, -forward * interval))
            dfs = trial.move(previous_df * math.exp(exponent))
            annuity = fixed_annuity
            for position in range(first, len(accruals)):
                annuity += accruals[position] * dfs[position]
            return (1.0 - dfs[-1]) / annuity - par_rate

        lower, upper = _dynamic_bracket(error)
        solved_forward = bisect(error, lower, upper, tolerance=1e-14, label="OIS bootstrap").root
        nodes.append((quote.maturity, previous_df * math.exp(-solved_forward * interval)))
    curve = DiscountCurve(request.curve_date, tuple(nodes), convention=request.convention)
    if curve.max_repricing_error_bp(ordered) > 1e-7:
        raise RuntimeError("bootstrapped OIS curve failed exact repricing")
    return curve
