from __future__ import annotations

import math
from datetime import date, timedelta

import pytest

from rub_fixed_income import EventKey, constant_daily_accumulation_periods


def _key(day: date) -> EventKey:
    return EventKey(day, 0)


@pytest.mark.parametrize(
    ("annual_rate", "day_count"),
    [
        (-365.0, 90),
        (math.nextafter(-365.0, -math.inf), 90),
        (-730.0, 90),
        (-730.0, 91),
    ],
)
def test_constant_daily_rejects_nonpositive_compounding_base(
    annual_rate: float, day_count: int
) -> None:
    start_date = date(2026, 1, 1)

    with pytest.raises(ValueError, match="daily compounding base must be positive"):
        constant_daily_accumulation_periods(
            _key(start_date),
            _key(start_date + timedelta(days=day_count)),
            (),
            annual_rate,
        )


@pytest.mark.parametrize("annual_rate", [0.14, -0.05, math.nextafter(-365.0, math.inf)])
def test_constant_daily_preserves_finite_positive_base_factors(annual_rate: float) -> None:
    start_date = date(2026, 1, 1)
    horizon_date = start_date + timedelta(days=1)

    (period,) = constant_daily_accumulation_periods(
        _key(start_date), _key(horizon_date), (), annual_rate
    )

    assert period.factor == pytest.approx(1.0 + annual_rate / 365.0)


@pytest.mark.parametrize("annual_rate", [math.inf, -math.inf, math.nan])
def test_constant_daily_rejects_nonfinite_annual_rate(annual_rate: float) -> None:
    start_date = date(2026, 1, 1)

    with pytest.raises(ValueError, match="annual rate must be finite"):
        constant_daily_accumulation_periods(
            _key(start_date),
            _key(start_date + timedelta(days=90)),
            (),
            annual_rate,
        )


@pytest.mark.parametrize(
    ("annual_rate", "day_count"),
    [
        (1e308, 2),
        (math.nextafter(-365.0, math.inf), 90),
    ],
)
def test_constant_daily_rejects_nonpositive_or_nonfinite_factor(
    annual_rate: float, day_count: int
) -> None:
    start_date = date(2026, 1, 1)

    with pytest.raises(ValueError, match="accumulation factors must be positive and finite"):
        constant_daily_accumulation_periods(
            _key(start_date),
            _key(start_date + timedelta(days=day_count)),
            (),
            annual_rate,
        )
