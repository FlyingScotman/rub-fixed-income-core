"""Shared guards for money consumed and returned by deterministic calculations."""

from __future__ import annotations

from math import isfinite


def finite_money(value: float, label: str) -> float:
    try:
        valid = not isinstance(value, bool) and isfinite(value)
    except (TypeError, ValueError, OverflowError):
        valid = False
    if not valid:
        raise ValueError(f"{label} must be finite")
    return float(value)


def positive_money(value: float, label: str) -> float:
    result = finite_money(value, label)
    if result <= 0:
        raise ValueError(f"{label} must be positive")
    return result


def non_negative_money(value: float, label: str) -> float:
    result = finite_money(value, label)
    if result < 0:
        raise ValueError(f"{label} cannot be negative")
    return result
