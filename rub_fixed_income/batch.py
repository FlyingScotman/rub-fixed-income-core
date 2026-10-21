"""Vectorised bond metrics for history at scale (the Trading Desk Monitor's past dates).

A desk chart of a bond's yield, duration and spreads over a year, or the history of a whole
book, needs the same few numbers for hundreds of bonds on hundreds of dates. The scalar path
(:func:`solve_effective_annual_yield`, :func:`bond_duration`, :func:`solve_z_spread`,
:func:`g_spread_bp`) costs about 1 ms per bond and date; :func:`bond_metrics_batch` solves a
whole date's bonds at once with numpy, for a few tens of microseconds per bond.

Definitions are those of the scalar functions, on the same inputs:

- ``effective_yield``: ``P = sum CF / (1 + y)^t``, ``t`` on the yield convention's day count
  from settlement (MOEX effective annual ACT/365F by default);
- ``macaulay_years`` and ``modified_duration`` (``macaulay / (1 + y)``) at that yield;
- ``g_spread_bp``: the yield minus the KBD effective yield at the Macaulay duration (MOEX's
  ``GSPREADBP``), when KBD parameters are given;
- ``z_spread_bp``: the spread over the curve's settlement-forward zero rates in the spread
  convention (annual ACT/365F by default) that reprices ``P``, when a curve is given. On KBD
  parameters the discount factors are the formula's (``kbd_discount_curve`` interpolates the
  same formula within 0.005 bp).

Roots are found by a safeguarded Newton iteration to machine precision; the results agree
with the scalar bisection solvers to about 1e-12 (``tests/test_batch.py``). A bond that has
no root in the scalar solvers' brackets (yield in (-99 %, 500 %), annual Z-spread in
(-95 %, 500 %)) gets None and a reason, never a number: the batch never fails as a whole.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date

import numpy as np

from ._money import finite_money, positive_money
from .canonical import OMIT_IF_DEFAULT
from .cashflows import BondCashflow, clean_price_pct_to_dirty_rub
from .conventions import DEFAULT_YIELD_CONVENTION, YieldConvention, ZSpreadConvention
from .discount import DiscountFactorCurve
from .domain import HorizonPolicy, Instrument
from .errors import InvalidTerms
from .kbd import ZcycParameters, _effective_yield_array

__all__ = [
    "BATCH_MODEL_VERSION",
    "BondMetrics",
    "BondMetricsInput",
    "bond_metrics_batch",
]

BATCH_MODEL_VERSION = "rub-bond-metrics-batch-v1"
_YIELD_BRACKET = (-0.99, 5.0)
_Z_BRACKET = {"periodic": (-0.95, 5.0), "continuous": (-5.0, 5.0)}
_ITERATIONS = 80


@dataclass(frozen=True)
class BondMetricsInput:
    """One bond on one date: its remaining flows and dirty price per bond.

    ``cashflows`` are the flows after ``settlement_date`` (any order), as
    :meth:`Instrument.cashflows` gives them; :meth:`from_instrument` builds the input.
    """

    settlement_date: date
    dirty_price: float
    cashflows: tuple[BondCashflow, ...]
    key: str = field(default="", metadata=OMIT_IF_DEFAULT)

    def __post_init__(self) -> None:
        object.__setattr__(self, "cashflows", tuple(self.cashflows))
        positive_money(self.dirty_price, "dirty price")
        for flow in self.cashflows:
            finite_money(flow.amount, "cashflow amount")
            if flow.payment_date <= self.settlement_date:
                raise InvalidTerms("batch cashflows must be paid after settlement")
        if not self.cashflows:
            raise InvalidTerms("a bond needs a cashflow after settlement")

    @classmethod
    def from_instrument(
        cls,
        instrument: Instrument,
        settlement_date: date,
        clean_price_pct: float,
        *,
        accrued_rub: float | None = None,
        horizon: HorizonPolicy = "maturity",
        key: str | None = None,
    ) -> BondMetricsInput:
        """The input of a fixed-coupon bond at ``settlement_date`` from its terms.

        The flows are those of the horizon policy (``maturity``, ``confirmed_put`` or
        ``open_put``); the dirty price is ``clean_price_pct`` of the outstanding face plus
        ``accrued_rub`` (default: the instrument's own accrued interest).
        """
        horizon_date, offer = instrument.horizon(horizon, settlement_date)
        flows = instrument.cashflows(after=settlement_date, horizon=horizon_date, offer=offer)
        accrued = instrument.accrued(settlement_date) if accrued_rub is None else accrued_rub
        dirty = clean_price_pct_to_dirty_rub(
            clean_price_pct, instrument.outstanding_face(settlement_date), accrued
        )
        return cls(
            settlement_date,
            dirty,
            tuple(BondCashflow(f.payment_date, f.amount) for f in flows),
            instrument.instrument_id if key is None else key,
        )


@dataclass(frozen=True)
class BondMetrics:
    """Metrics of one :class:`BondMetricsInput`; None with ``error`` when not solvable."""

    key: str
    effective_yield: float | None
    macaulay_years: float | None
    modified_duration: float | None
    g_spread_bp: float | None
    z_spread_bp: float | None
    error: str | None = None


def _grid(
    inputs: Sequence[BondMetricsInput], year: float
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Padded (bonds x flows) amounts, times in ``days / year`` and payment day numbers."""
    width = max(len(i.cashflows) for i in inputs)
    amounts = np.zeros((len(inputs), width))
    days = np.zeros((len(inputs), width))
    ordinals = np.zeros((len(inputs), width), dtype=np.int64)
    for row, item in enumerate(inputs):
        start = item.settlement_date.toordinal()
        for col, flow in enumerate(item.cashflows):
            amounts[row, col] = flow.amount
            ordinals[row, col] = flow.payment_date.toordinal()
            days[row, col] = ordinals[row, col] - start
    return amounts, days / year, ordinals


def _time_basis(day_count_name: str) -> float:
    if day_count_name == "ACT/365F":
        return 365.0
    if day_count_name == "ACT/360":
        return 360.0
    raise InvalidTerms(f"batch metrics support ACT/365F and ACT/360 time, not {day_count_name}")


def _newton(
    price: np.ndarray,
    value: object,
    lower: float,
    upper: float,
    start: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    """Safeguarded Newton on each row of ``value(x) -> (f, f')`` with ``f`` decreasing.

    Every row keeps a bracket ``[lo, hi]`` with ``f(lo) > 0 > f(hi)``; a Newton step that
    leaves it is replaced by bisection. Returns the roots and a mask of rows solved.
    """
    func = value  # (x) -> (f(x), f'(x)), vectorised over rows
    lo = np.full(price.shape, lower)
    hi = np.full(price.shape, upper)
    f_lo, _ = func(lo)  # type: ignore[operator]
    f_hi, _ = func(hi)  # type: ignore[operator]
    ok = np.isfinite(f_lo) & np.isfinite(f_hi) & (f_lo > 0.0) & (f_hi < 0.0)
    x = np.clip(start, lower + 1e-9, upper - 1e-9)
    done = ~ok
    for _ in range(_ITERATIONS):
        if done.all():
            break
        f, slope = func(x)  # type: ignore[operator]
        positive = f > 0.0
        lo = np.where(positive & ~done, x, lo)
        hi = np.where(~positive & ~done, x, hi)
        exact = np.abs(f) <= 1e-13 * price
        with np.errstate(divide="ignore", invalid="ignore"):
            step = x - f / slope
        bad = ~np.isfinite(step) | (step < lo) | (step > hi)
        candidate = np.where(bad, 0.5 * (lo + hi), step)
        tolerance = 4.0 * np.finfo(float).eps * np.maximum(1.0, np.abs(x))
        settled = ~bad & (np.abs(candidate - x) <= tolerance)
        # A root already reached stays put; otherwise take the (safeguarded) step.
        x = np.where(done | exact, x, candidate)
        done = done | exact | settled
    return x, ok & done


def bond_metrics_batch(
    inputs: Sequence[BondMetricsInput],
    *,
    kbd: ZcycParameters | None = None,
    curve: DiscountFactorCurve | ZcycParameters | None = None,
    yield_convention: YieldConvention = DEFAULT_YIELD_CONVENTION,
    spread_convention: ZSpreadConvention | None = None,
) -> tuple[BondMetrics, ...]:
    """Yield, duration, G-spread and Z-spread of many bonds at once (see the module doc).

    ``kbd`` gives the G-spread; ``curve`` (a discount curve, or KBD parameters for the
    formula's discount factors) gives the Z-spread in ``spread_convention`` (annual
    ACT/365F by default). Settlements may differ between inputs but not precede the
    curve date. Results are in input order.
    """
    if not inputs:
        return ()
    convention = ZSpreadConvention() if spread_convention is None else spread_convention
    if not isinstance(yield_convention, YieldConvention):
        raise TypeError("yield_convention must be a YieldConvention")
    if yield_convention.compounding.periods_per_year != 1:
        raise InvalidTerms("batch yields are effective annual")
    keys = [i.key for i in inputs]
    price = np.array([i.dirty_price for i in inputs])
    amounts, times, ordinals = _grid(inputs, _time_basis(yield_convention.day_count.value))

    def yield_value(y: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        base = 1.0 + y[:, None]
        with np.errstate(over="ignore", invalid="ignore", divide="ignore"):
            discounted = amounts * base ** (-times)
            f = discounted.sum(axis=1) - price
            slope = -(discounted * times).sum(axis=1) / base[:, 0]
        return f, slope

    total = amounts.sum(axis=1)
    mean_time = (amounts * times).sum(axis=1) / np.where(total > 0, total, 1.0)
    with np.errstate(divide="ignore", invalid="ignore"):
        guess = np.where(
            (total > 0) & (mean_time > 0), (total / price) ** (1.0 / mean_time) - 1.0, 0.1
        )
    guess = np.where(np.isfinite(guess), guess, 0.1)
    yields, solved = _newton(price, yield_value, *_YIELD_BRACKET, guess)
    base = 1.0 + yields[:, None]
    with np.errstate(over="ignore", invalid="ignore"):
        discounted = amounts * base ** (-times)
        pv = discounted.sum(axis=1)
        macaulay = (discounted * times).sum(axis=1) / pv
    g_spreads: np.ndarray | None = None
    if kbd is not None:
        tenors = np.where(solved & (macaulay > 0), macaulay, 1.0)
        g_spreads = (yields - _effective_yield_array(kbd, tenors)) * 10_000.0
    z_spreads: np.ndarray | None = None
    z_solved = np.ones(len(inputs), dtype=bool)
    if curve is not None:
        z_spreads, z_solved = _z_spreads(inputs, curve, convention, amounts, ordinals, price)
    out = []
    for row, key in enumerate(keys):
        if not solved[row] or not np.isfinite(macaulay[row]):
            out.append(BondMetrics(key, None, None, None, None, None, "yield is not bracketed"))
            continue
        y, d = float(yields[row]), float(macaulay[row])
        z = None
        error = None
        if z_spreads is not None:
            if z_solved[row]:
                z = float(z_spreads[row]) * 10_000.0
            else:
                error = "Z-spread is not bracketed"
        g = None if g_spreads is None else float(g_spreads[row])
        out.append(BondMetrics(key, y, d, d / (1.0 + y), g, z, error))
    return tuple(out)


def _z_spreads(
    inputs: Sequence[BondMetricsInput],
    curve: DiscountFactorCurve | ZcycParameters,
    convention: ZSpreadConvention,
    amounts: np.ndarray,
    ordinals: np.ndarray,
    price: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    year = _time_basis(convention.day_count.value)
    settle = np.array([i.settlement_date.toordinal() for i in inputs])
    mask = amounts != 0.0
    if isinstance(curve, ZcycParameters):
        origin = curve.trade_date.toordinal()
        if (settle < origin).any():
            raise InvalidTerms("settlement cannot predate the curve date")

        def kbd_df(years: np.ndarray) -> np.ndarray:
            # DF(t) = exp(-G(t) t / 10^4) on ACT/365F years from the curve date; DF(0) = 1.
            safe = np.where(years > 0.0, years, 1.0)
            log_df = -np.log1p(_effective_yield_array(curve, safe.ravel())).reshape(safe.shape)
            return np.where(years > 0.0, np.exp(log_df * safe), 1.0)

        flow_df = kbd_df(np.where(mask, (ordinals - origin) / 365.0, 1.0))
        settle_df = kbd_df((settle - origin) / 365.0)
    else:
        if (settle < curve.curve_date.toordinal()).any():
            raise InvalidTerms("settlement cannot predate the curve date")
        cache: dict[int, float] = {}

        def df(ordinal: int) -> float:
            value = cache.get(ordinal)
            if value is None:
                value = cache[ordinal] = curve.discount_factor(date.fromordinal(ordinal))
            return value

        flow_df = np.ones(amounts.shape)
        for row, col in zip(*np.nonzero(mask), strict=True):
            flow_df[row, col] = df(int(ordinals[row, col]))
        settle_df = np.array([df(int(o)) for o in settle])
    forward = np.where(mask, flow_df / settle_df[:, None], 1.0)
    tau = np.where(mask, (ordinals - settle[:, None]) / year, 1.0)
    if convention.compounding.is_continuous:

        def value(z: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
            with np.errstate(over="ignore", invalid="ignore"):
                discounted = amounts * forward * np.exp(-z[:, None] * tau)
                return discounted.sum(axis=1) - price, -(discounted * tau).sum(axis=1)

        lower, upper = _Z_BRACKET["continuous"]
    else:
        m = convention.compounding.periods_per_year
        zero = m * (forward ** (-1.0 / (m * tau)) - 1.0)

        def value(z: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
            base = 1.0 + (zero + z[:, None]) / m
            with np.errstate(over="ignore", invalid="ignore", divide="ignore"):
                safe = np.where(base > 0.0, base, np.nan)
                discounted = amounts * safe ** (-m * tau)
                f = np.where(mask, discounted, 0.0).sum(axis=1) - price
                slope = -np.where(mask, discounted * tau / safe, 0.0).sum(axis=1)
            return f, slope

        lower, upper = _Z_BRACKET["periodic"]
    return _newton(price, value, lower, upper, np.zeros(len(inputs)))
