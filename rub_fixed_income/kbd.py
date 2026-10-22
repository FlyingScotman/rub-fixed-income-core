"""MOEX zero-coupon yield curve (KBD, G-curve) evaluator (roadmap 0.6).

MOEX publishes the OFZ zero curve as parameters ``B1, B2, B3, T1, G1..G9``. The
continuously compounded zero rate in basis points at ``t`` years is::

    G(t) = B1 + B2 * L + B3 * (L - exp(-t/T1)) + sum_i G_i * exp(-((t - a_i) / b_i)^2)
    L    = T1 / t * (1 - exp(-t/T1))

with ``a_1 = 0``, ``a_2 = 0.6``, ``a_(i+1) = a_i + 0.6 * 1.6^(i-1)``, ``b_1 = 0.6`` and
``b_(i+1) = 1.6 * b_i``. MOEX's published yields are effective annual:
``Y(t) = exp(G(t) / 10^4) - 1``. Golden tests reproduce MOEX's standard-tenor yields
within 0.01 bp (``tests/test_kbd.py``).

Time is ACT/365F years from the curve date. :func:`kbd_discount_curve` materialises the
curve as a :class:`DiscountCurve` on a dense node grid, so every pricing and risk path
(Z-spread, bucket risk, Hessian, scenarios) works on it unchanged. With
``zero_shock="annual"`` (0.34.0; the ``rub_ofz_kbd_v2`` spec) bucket risk moves the
effective annual yields MOEX publishes (``curve_basis = "annual_zero"``), so KBD DV01
sums with par RUONIA OIS DV01; the default keeps continuous zero rates.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Literal

import numpy as np

from ._money import finite_money
from .conventions import CurveConvention, DayCount
from .discount import DiscountCurve
from .errors import InvalidTerms

__all__ = [
    "KBD_MODEL_VERSION",
    "ZcycParameters",
    "g_spread_bp",
    "kbd_discount_curve",
    "kbd_effective_yield",
    "kbd_effective_yields",
    "kbd_zero_rate_bp",
]

KBD_MODEL_VERSION = "moex-kbd-gcurve-v1"


def _grid() -> tuple[tuple[float, ...], tuple[float, ...]]:
    centres = [0.0, 0.6]
    for power in range(1, 8):
        centres.append(centres[-1] + 0.6 * 1.6**power)
    widths = [0.6]
    for _ in range(8):
        widths.append(widths[-1] * 1.6)
    return tuple(centres), tuple(widths)


_CENTRES, _WIDTHS = _grid()


@dataclass(frozen=True)
class ZcycParameters:
    """One MOEX KBD publication: ``b1..b3`` and ``g`` in basis points, ``t1`` in years."""

    trade_date: date
    b1: float
    b2: float
    b3: float
    t1: float
    g: tuple[float, float, float, float, float, float, float, float, float]

    def __post_init__(self) -> None:
        for name in ("b1", "b2", "b3", "t1"):
            finite_money(getattr(self, name), f"KBD {name}")
        if self.t1 <= 0.0:
            raise InvalidTerms("KBD T1 must be positive")
        object.__setattr__(self, "g", tuple(self.g))
        if len(self.g) != 9:
            raise InvalidTerms("KBD needs exactly nine G parameters")
        for value in self.g:
            finite_money(value, "KBD G parameter")


def kbd_zero_rate_bp(params: ZcycParameters, tenor_years: float) -> float:
    """Continuously compounded zero rate in basis points at ``tenor_years`` (> 0)."""
    if not math.isfinite(tenor_years) or tenor_years <= 0.0:
        raise ValueError("tenor must be positive and finite")
    decay = math.exp(-tenor_years / params.t1)
    loading = params.t1 / tenor_years * (1.0 - decay)
    terms = [params.b1, params.b2 * loading, params.b3 * (loading - decay)]
    terms.extend(
        g * math.exp(-(((tenor_years - centre) / width) ** 2))
        for g, centre, width in zip(params.g, _CENTRES, _WIDTHS, strict=True)
    )
    return math.fsum(terms)


def kbd_effective_yield(params: ZcycParameters, tenor_years: float) -> float:
    """Effective annual yield as a decimal, as MOEX publishes it (in %)."""
    return math.expm1(kbd_zero_rate_bp(params, tenor_years) / 10_000.0)


def kbd_effective_yields(params: ZcycParameters, tenors_years: object) -> tuple[float, ...]:
    """:func:`kbd_effective_yield` at many tenors at once (numpy), in input order.

    The same formula summed in floating point rather than with ``math.fsum``: results
    agree with the scalar function to about 1e-15. Tenors must be positive and finite.
    """
    t = np.asarray(tenors_years, dtype=float).ravel()
    if t.size == 0:
        return ()
    return tuple(float(v) for v in _effective_yield_array(params, t))


def _effective_yield_array(params: ZcycParameters, t: np.ndarray) -> np.ndarray:
    if not np.all(np.isfinite(t)) or np.any(t <= 0.0):
        raise ValueError("tenors must be positive and finite")
    decay = np.exp(-t / params.t1)
    loading = params.t1 / t * (1.0 - decay)
    zero = params.b1 + params.b2 * loading + params.b3 * (loading - decay)
    for g, centre, width in zip(params.g, _CENTRES, _WIDTHS, strict=True):
        zero = zero + g * np.exp(-(((t - centre) / width) ** 2))
    return np.expm1(zero / 10_000.0)


def g_spread_bp(params: ZcycParameters, bond_effective_yield: float, tenor_years: float) -> float:
    """G-spread: the bond's effective annual yield minus the KBD yield at ``tenor_years``.

    Pass the tenor the desk measures G-spread at (MOEX uses the bond's duration in
    years); it is an explicit input, not inferred.
    """
    finite_money(bond_effective_yield, "bond effective yield")
    return (bond_effective_yield - kbd_effective_yield(params, tenor_years)) * 10_000.0


def _node_offsets(horizon_days: int) -> tuple[int, ...]:
    # Daily nodes to 2Y (the short end bends sharply when T1 is small), weekly to 5Y,
    # then monthly. Log-linear DF interpolation then stays within 0.005 bp of the
    # formula on every day to 30Y for the archived publications (tests/test_kbd.py).
    offsets = set(range(1, 731))
    offsets.update(range(730, 1826, 7))
    offsets.update(range(1825, horizon_days, 30))
    offsets.add(horizon_days)
    return tuple(sorted(offsets))


def kbd_discount_curve(
    params: ZcycParameters,
    curve_date: date | None = None,
    *,
    horizon_years: int = 30,
    zero_shock: Literal["continuous", "annual"] = "continuous",
) -> DiscountCurve:
    """Materialise the KBD curve as a log-linear-DF :class:`DiscountCurve`.

    ``DF(t) = exp(-G(t)/10^4 * t)`` at ACT/365F times on the node grid; beyond the last
    node the curve extrapolates flat-forward. ``curve_date`` defaults to the trade date.
    ``zero_shock`` is the unit of its bucket risk (``CurveConvention.zero_shock``):
    continuous zero rates by default, ``"annual"`` the effective annual KBD yield.
    """
    start = params.trade_date if curve_date is None else curve_date
    if horizon_years < 1:
        raise ValueError("horizon_years must be at least 1")
    nodes = []
    for offset in _node_offsets(365 * horizon_years + horizon_years // 4):
        tenor = offset / 365.0
        rate = kbd_zero_rate_bp(params, tenor) / 10_000.0
        nodes.append((start + timedelta(days=offset), math.exp(-rate * tenor)))
    return DiscountCurve(
        start,
        tuple(nodes),
        KBD_MODEL_VERSION,
        convention=CurveConvention(time_day_count=DayCount.ACT_365_FIXED, zero_shock=zero_shock),
    )
