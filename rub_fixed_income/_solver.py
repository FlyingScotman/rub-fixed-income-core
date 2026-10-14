"""Single bracketed bisection root finder shared by every solver in the package."""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class RootResult:
    root: float
    iterations: int
    value: float


def bisect(
    function: Callable[[float], float],
    lower: float,
    upper: float,
    *,
    tolerance: float,
    x_tolerance: float = 0.0,
    max_iterations: int = 240,
    lower_value: float | None = None,
    upper_value: float | None = None,
    label: str = "solver",
) -> RootResult:
    """Find a sign change of ``function`` on ``[lower, upper]``.

    Contract:
    - both bracket values must be finite unless explicitly supplied by the caller (a caller
      may pass an infinite endpoint value whose sign is known analytically);
    - the bracket must contain a sign change, otherwise ``ValueError('<label> root is not
      bracketed')``;
    - every interior evaluation must be finite, otherwise ``ValueError`` (never continue);
    - returns when ``|f(mid)| <= tolerance``, when the pre-step interval width is
      ``<= x_tolerance``, or when the bracket has collapsed to adjacent floats (machine
      precision; the objective cannot be resolved further);
    - otherwise ``RuntimeError('<label> solver did not converge')`` after ``max_iterations``.
    """
    if not (math.isfinite(lower) and math.isfinite(upper)) or not lower < upper:
        raise ValueError(f"{label} root bracket must be finite and ordered")
    low_value = function(lower) if lower_value is None else lower_value
    high_value = function(upper) if upper_value is None else upper_value
    if lower_value is None and not math.isfinite(low_value):
        raise ValueError(f"{label} root bracket has a non-finite objective value")
    if upper_value is None and not math.isfinite(high_value):
        raise ValueError(f"{label} root bracket has a non-finite objective value")
    if math.isnan(low_value) or math.isnan(high_value) or low_value * high_value > 0.0:
        raise ValueError(f"{label} root is not bracketed")
    for iteration in range(1, max_iterations + 1):
        middle = (lower + upper) / 2.0
        value = function(middle)
        if not math.isfinite(value):
            raise ValueError(f"{label} solver hit a non-finite objective value")
        if abs(value) <= tolerance or upper - lower <= x_tolerance:
            return RootResult(middle, iteration, value)
        if middle <= lower or middle >= upper:
            # Bracket collapsed to adjacent floats: converged to machine precision.
            return RootResult(middle, iteration, value)
        if low_value * value <= 0.0:
            upper = middle
        else:
            lower, low_value = middle, value
    raise RuntimeError(f"{label} solver did not converge")
