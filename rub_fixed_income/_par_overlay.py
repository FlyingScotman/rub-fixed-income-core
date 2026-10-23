"""Par-yield bucket shocks of a discount curve (``CurveConvention.zero_shock="annual_par"``).

A bucket shock of ``s`` bp moves the par yield of one synthetic instrument per bucket from
1Y to 20Y+ by ``s``: an annual-coupon bond of 1, 2, 3, 5, 7, 10, 15 or 20 years from the
curve date, coupon dates rolled annually from it unadjusted, accrual on the curve's time
day count (ACT/365F for the KBD), redeemed at par. The curve itself is not refitted: it
moves by an overlay of its effective annual zero yield,

    Y'(t) = Y(t) + sum_j w_j(t) x_j,        DF'(t) = (1 + Y'(t))^(-t),

with the hat weights of the risk buckets (``key_rate_weights``) and the 1M-6M nodes tied to
1Y (the short end is flat), the eight node shifts ``x`` solved by Newton so that the eight
instruments reprice at their shocked par yields exactly. This is the bootstrap of a quote
curve, done as an overlay so the base curve (the published KBD) is untouched at zero shock.

The 1M, 3M and 6M buckets have no instrument: shifting them moves nothing and their risk is
zero (the KBD has no OFZ that short; its short end is MOEX's extrapolation). The 1Y
instrument carries the short end.

Every risk engine reads the shocked curve through ``_risk_common._shock_curve``; the solved
overlay is cached per (base curve, shift vector), so positions on one curve share it.
``par_jacobians`` gives the Jacobian and the instruments' Hessians in overlay space for
mapping zero-space risk to par risk (``factor_transform``).
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING, cast

import numpy as np

from .dates import add_months

if TYPE_CHECKING:
    from .discount import DiscountCurve

PAR_YEARS = (1, 2, 3, 5, 7, 10, 15, 20)
"""Tenor in years of the par instrument of each bucket from 1Y (bucket index 3) on."""
PAR_FIRST_BUCKET = 3
_BP = 1e-4
_NEWTON_TOLERANCE = 1e-13  # decimal par yield (1e-9 bp)
_NEWTON_ITERATIONS = 30
_MEMO_SIZE = 4096


class _Instrument:
    """Coupon dates' curve time, hat weights and base discount factor, plus accruals."""

    __slots__ = ("accruals", "base_df", "times", "weights")

    def __init__(self, base: DiscountCurve, years: int) -> None:
        from ._risk_common import _zero_shock_terms

        start = base.curve_date
        dates = [add_months(start, 12 * k) for k in range(1, years + 1)]
        previous = [start, *dates[:-1]]
        day_count = base.convention.time_day_count
        self.accruals = np.array([day_count.year_fraction(a, b) for a, b in zip(previous, dates)])
        terms = [_zero_shock_terms(base, d) for d in dates]
        self.times = np.array([t[0] for t in terms])
        self.base_df = np.array([t[2] for t in terms])
        weights = np.zeros((len(dates), len(PAR_YEARS)))
        for row, (_, nonzero, _) in enumerate(terms):
            for bucket, weight in nonzero:
                weights[row, max(bucket - PAR_FIRST_BUCKET, 0)] += weight
        self.weights = weights

    def growth(self, x: np.ndarray) -> np.ndarray:
        """1 + Y'(t) at the coupon dates for node shifts ``x`` (bp)."""
        g = self.base_df ** (-1.0 / self.times) + self.weights @ x * _BP
        if np.any(g <= 0.0):
            raise ValueError("shocked annual yield must exceed -100 %")
        return cast(np.ndarray, g)

    def par(self, x: np.ndarray) -> float:
        df = self.growth(x) ** (-self.times)
        return float((1.0 - df[-1]) / np.dot(self.accruals, df))

    def derivatives(self, x: np.ndarray) -> tuple[float, np.ndarray, np.ndarray]:
        """Par yield, its gradient (per bp of x) and Hessian (per bp^2), decimal units."""
        g = self.growth(x)
        t = self.times
        df = g ** (-t)
        d1 = (-t * g ** (-t - 1.0) * _BP)[:, None] * self.weights  # dDF/dx
        d2c = t * (t + 1.0) * g ** (-t - 2.0) * _BP * _BP  # d2DF/dx2 along the weights
        annuity = float(np.dot(self.accruals, df))
        par = (1.0 - df[-1]) / annuity
        da = self.accruals @ d1
        grad = (-d1[-1] - par * da) / annuity
        w = self.weights
        d2n = -d2c[-1] * np.outer(w[-1], w[-1])
        d2a = (w * (self.accruals * d2c)[:, None]).T @ w
        hess = (d2n - np.outer(grad, da) - np.outer(da, grad) - par * d2a) / annuity
        return par, grad, hess


def _instruments(base: DiscountCurve) -> tuple[_Instrument, ...]:
    cached = base.__dict__.get("_par_instruments")
    if cached is None:
        cached = tuple(_Instrument(base, years) for years in PAR_YEARS)
        object.__setattr__(base, "_par_instruments", cached)
    return cast(tuple[_Instrument, ...], cached)


def par_overlay_shifts(base: DiscountCurve, shifts: tuple[float, ...]) -> tuple[float, ...]:
    """The 11 annual-zero bucket shifts (bp) that move the par instruments by ``shifts``."""
    memo = base.__dict__.get("_par_overlay_memo")
    if memo is None:
        memo = {}
        object.__setattr__(base, "_par_overlay_memo", memo)
    cached = cast(dict[tuple[float, ...], tuple[float, ...]], memo).get(shifts)
    if cached is not None:
        return cached
    instruments = _instruments(base)
    zero = np.zeros(len(PAR_YEARS))
    target = (
        np.array([i.par(zero) for i in instruments])
        + np.asarray(shifts[PAR_FIRST_BUCKET:], dtype=float) * _BP
    )
    x = zero.copy()
    for _ in range(_NEWTON_ITERATIONS):
        values, rows = [], []
        for instrument in instruments:
            par, grad, _ = instrument.derivatives(x)
            values.append(par)
            rows.append(grad)
        residual = np.array(values) - target
        if np.max(np.abs(residual)) < _NEWTON_TOLERANCE:
            break
        x = x - np.linalg.solve(np.array(rows), residual)
    else:
        raise ValueError("par-yield shock did not converge")
    if not np.all(np.isfinite(x)):
        raise ValueError("par-yield shock is outside the supported numerical domain")
    result = (float(x[0]),) * PAR_FIRST_BUCKET + tuple(float(v) for v in x)
    if len(memo) < _MEMO_SIZE:
        memo[shifts] = result
    return result


def par_jacobians(base: DiscountCurve) -> tuple[np.ndarray, tuple[np.ndarray, ...]]:
    """``J[i, j]`` = d par_i / d x_j (bp/bp) at zero shock and each instrument's Hessian
    ``d2 par_i / d x d x`` (bp per bp^2), over the eight par nodes 1Y..20Y+."""
    cached = base.__dict__.get("_par_jacobians")
    if cached is None:
        zero = np.zeros(len(PAR_YEARS))
        rows, hessians = [], []
        for instrument in _instruments(base):
            _, grad, hess = instrument.derivatives(zero)
            rows.append(grad / _BP)
            hessians.append(hess / _BP)
        cached = (np.array(rows), tuple(hessians))
        object.__setattr__(base, "_par_jacobians", cached)
    return cast(tuple[np.ndarray, tuple[np.ndarray, ...]], cached)


def par_rates(base: DiscountCurve) -> tuple[tuple[date, float], ...]:
    """Maturity and par yield (decimal) of each par instrument on the unshocked curve."""
    zero = np.zeros(len(PAR_YEARS))
    return tuple(
        (add_months(base.curve_date, 12 * years), i.par(zero))
        for years, i in zip(PAR_YEARS, _instruments(base))
    )
