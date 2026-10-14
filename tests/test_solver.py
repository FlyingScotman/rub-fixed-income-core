from __future__ import annotations

import math

import pytest

from rub_fixed_income._solver import RootResult, bisect


def test_bisect_finds_root_and_reports_iterations() -> None:
    result = bisect(lambda x: x * x - 2.0, 0.0, 2.0, tolerance=1e-14, label="sqrt")
    assert isinstance(result, RootResult)
    assert result.root == pytest.approx(math.sqrt(2.0), abs=1e-14)
    assert result.iterations > 10


def test_bisect_rejects_unbracketed_interval() -> None:
    with pytest.raises(ValueError, match="sqrt root is not bracketed"):
        bisect(lambda x: x * x + 1.0, 0.0, 2.0, tolerance=1e-14, label="sqrt")


def test_bisect_rejects_nan_bracket_value() -> None:
    with pytest.raises(ValueError, match="non-finite"):
        bisect(lambda x: math.nan, 0.0, 1.0, tolerance=1e-14, label="f")


def test_bisect_rejects_non_finite_interior_value() -> None:
    def function(x: float) -> float:
        if 0.4 < x < 0.6:
            return math.nan
        return x - 0.7

    with pytest.raises(ValueError, match="non-finite objective"):
        bisect(function, 0.0, 1.0, tolerance=1e-14, label="f")


def test_bisect_accepts_known_infinite_endpoint_sign() -> None:
    result = bisect(
        lambda x: 1.0 / x - 2.0, 0.0, 1.0, tolerance=1e-15, lower_value=math.inf, label="f"
    )
    assert result.root == pytest.approx(0.5)


def test_bisect_raises_on_iteration_exhaustion() -> None:
    with pytest.raises(RuntimeError, match="did not converge"):
        bisect(lambda x: x - 0.3, 0.0, 1.0, tolerance=0.0, max_iterations=5, label="f")
