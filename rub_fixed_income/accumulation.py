from __future__ import annotations

from dataclasses import dataclass
from itertools import pairwise
from math import isfinite

from .cashflows import EventKey


@dataclass(frozen=True)
class AccumulationPeriod:
    start: EventKey
    end: EventKey
    factor: float
    period_id: str


def constant_daily_accumulation_periods(
    start: EventKey,
    horizon: EventKey,
    boundaries: tuple[EventKey, ...],
    annual_rate: float,
) -> tuple[AccumulationPeriod, ...]:
    if not isfinite(annual_rate):
        raise ValueError("annual rate must be finite")
    daily_base = 1.0 + annual_rate / 365.0
    if daily_base <= 0.0:
        raise ValueError("daily compounding base must be positive")

    def accumulation_factor(left: EventKey, right: EventKey) -> float:
        try:
            factor = daily_base ** (right.event_date - left.event_date).days
        except OverflowError as exc:
            raise ValueError("accumulation factors must be positive and finite") from exc
        if factor <= 0.0 or not isfinite(factor):
            raise ValueError("accumulation factors must be positive and finite")
        return factor

    keys = (start, *boundaries, horizon)
    return tuple(
        AccumulationPeriod(
            left,
            right,
            accumulation_factor(left, right),
            f"constant-daily-{index}",
        )
        for index, (left, right) in enumerate(pairwise(keys), start=1)
    )
