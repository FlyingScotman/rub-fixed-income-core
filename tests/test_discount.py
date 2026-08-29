from __future__ import annotations

from datetime import date

import pytest

from rub_fixed_income import (
    CurveBootstrapRequest,
    DiscountCurve,
    OisQuote,
    bootstrap_discount_curve,
)


def test_flat_continuous_discount_curve_supports_compatibility_callers() -> None:
    curve = DiscountCurve.flat(date(2026, 1, 1), 0.12)

    assert curve.discount_factor(date(2027, 1, 1)) == pytest.approx(0.8869204367171575, abs=1e-12)


def test_bootstrap_discount_curve_recovers_constant_rate_discount_factors() -> None:
    curve_date = date(2025, 1, 1)
    quotes = (
        OisQuote("1Y", date(2026, 1, 1), 0.10),
        OisQuote("2Y", date(2027, 1, 1), 0.10),
    )

    curve = bootstrap_discount_curve(CurveBootstrapRequest(curve_date, quotes))

    assert curve.discount_factor(date(2026, 1, 1)) == pytest.approx(1.0 / 1.10)
    assert curve.discount_factor(date(2027, 1, 1)) == pytest.approx(1.0 / 1.10**2)
    assert curve.max_repricing_error_bp(quotes) < 1e-7
