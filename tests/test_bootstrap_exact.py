"""The fast OIS bootstrap returns the nodes of the trial-curve bisection bit for bit.

``bootstrap_discount_curve`` evaluates its root-search objective without building a
``DiscountCurve`` per trial (after 0.37.0, FlyingScotman/rub-fixed-income-core#77). Pinned
result hashes depend on every bit of the curve, so the reference below is the 0.37.0
bootstrap itself, and every comparison is exact.
"""

from __future__ import annotations

import math
from dataclasses import replace
from datetime import date, timedelta
from itertools import combinations

import pytest

import rub_fixed_income._risk_common as risk_common
from rub_fixed_income import (
    BusinessCalendar,
    CurveBootstrapRequest,
    CurveConvention,
    DiscountCurve,
    OisQuote,
    ScheduleConvention,
    ShockedCurveCache,
    bootstrap_discount_curve,
    ois_curve_convention,
    ois_quote_periods,
    ois_quotes,
)
from rub_fixed_income._risk_common import RISK_BUCKETS, _shock_curve, _shocked_request
from rub_fixed_income._solver import bisect
from rub_fixed_income.discount import (
    _SPOT_OIS_MAX_PASSES,
    _SPOT_OIS_TOLERANCE_BP,
    _dynamic_bracket,
    _NodeCurve,
)

# Synthetic: weekends plus two holidays, one on a payment date of the 1Y swap.
CAL = BusinessCalendar(
    holidays=frozenset({date(2026, 11, 4), date(2027, 10, 20)}), version="synthetic-77"
)
V2 = ois_curve_convention("rub_ruonia_ois_moex_v2", CAL)
TRADE = date(2026, 10, 16)
MOEX = ["1W", "2W", "1M", "2M", "3M", "6M", "9M", "1Y"] + [f"{y}Y" for y in range(2, 11)]
LONG = [*MOEX, "12Y", "15Y", "20Y", "25Y", "30Y"]


def reference_bootstrap(request: CurveBootstrapRequest) -> DiscountCurve:
    """The 0.37.0 ``bootstrap_discount_curve``: a full ``DiscountCurve`` per trial."""
    ordered = sorted(request.quotes, key=lambda item: item.maturity)
    convention = request.convention
    if convention.ois_swap is not None:
        legs = [ois_quote_periods(request.curve_date, q, convention) for q in ordered]
        nodes: list[tuple[date, float]] = []

        def solve(index: int, later: tuple[tuple[date, float], ...]) -> tuple[date, float]:
            quote, periods = ordered[index], legs[index]
            earlier = tuple(nodes[:index])
            previous_date, previous_df = earlier[-1] if earlier else (request.curve_date, 1.0)
            interval = convention.time_day_count.year_fraction(previous_date, quote.maturity)

            def error(forward: float) -> float:
                exponent = max(-700.0, min(700.0, -forward * interval))
                node = (quote.maturity, previous_df * math.exp(exponent))
                candidate = DiscountCurve(
                    request.curve_date, earlier + (node,) + later, convention=convention
                )
                return candidate.ois_swap_par_rate(periods) - quote.par_rate

            lower, upper = _dynamic_bracket(error)
            root = bisect(error, lower, upper, tolerance=1e-14, label="OIS bootstrap").root
            return quote.maturity, previous_df * math.exp(-root * interval)

        for index in range(len(ordered)):
            nodes.append(solve(index, ()))
        for _ in range(_SPOT_OIS_MAX_PASSES):
            curve = DiscountCurve(request.curve_date, tuple(nodes), convention=convention)
            if curve.max_repricing_error_bp(ordered) <= _SPOT_OIS_TOLERANCE_BP:
                return curve
            for index in range(len(ordered)):
                nodes[index] = solve(index, tuple(nodes[index + 1 :]))
        raise RuntimeError("spot-starting OIS bootstrap did not converge")
    nodes = []
    for quote in ordered:
        previous_date, previous_df = nodes[-1] if nodes else (request.curve_date, 1.0)
        interval = convention.time_day_count.year_fraction(previous_date, quote.maturity)
        existing = tuple(nodes)

        def legacy_error(
            forward: float,
            previous_df: float = previous_df,
            interval: float = interval,
            existing: tuple[tuple[date, float], ...] = existing,
            maturity: date = quote.maturity,
            par_rate: float = quote.par_rate,
        ) -> float:
            exponent = max(-700.0, min(700.0, -forward * interval))
            candidate = DiscountCurve(
                request.curve_date,
                existing + ((maturity, previous_df * math.exp(exponent)),),
                convention=convention,
            )
            return candidate.par_rate(maturity) - par_rate

        lower, upper = _dynamic_bracket(legacy_error)
        forward = bisect(legacy_error, lower, upper, tolerance=1e-14, label="OIS bootstrap").root
        nodes.append((quote.maturity, previous_df * math.exp(-forward * interval)))
    return DiscountCurve(request.curve_date, tuple(nodes), convention=convention)


def assert_bitwise(request: CurveBootstrapRequest) -> DiscountCurve:
    curve = bootstrap_discount_curve(request)
    expected = reference_bootstrap(request)
    assert [(d, df.hex()) for d, df in curve.nodes] == [(d, df.hex()) for d, df in expected.nodes]
    assert curve == expected
    return curve


def v2_request(rates: list[float], tenors: list[str] = MOEX) -> CurveBootstrapRequest:
    return CurveBootstrapRequest(TRADE, ois_quotes(TRADE, list(zip(tenors, rates)), CAL), V2)


def shapes(size: int) -> dict[str, list[float]]:
    ramp = [i / max(size - 1, 1) for i in range(size)]
    return {
        "upward": [0.1625 + 0.02 * x for x in ramp],
        "inverted": [0.21 - 0.12 * x for x in ramp],
        "steep": [0.03 + 0.12 * x for x in ramp],
        "humped": [0.15 + 0.04 * math.sin(6 * x) for x in ramp],
        "negative": [-0.004 + 0.006 * x for x in ramp],
        "zero": [0.0] * size,
    }


@pytest.mark.parametrize("shape", sorted(shapes(1)))
def test_spot_ois_matches_the_reference_bitwise(shape: str) -> None:
    request = v2_request(shapes(len(MOEX))[shape])
    assert assert_bitwise(request).max_repricing_error_bp(request.quotes) < 1e-7


@pytest.mark.parametrize("shape", ["upward", "inverted", "negative"])
def test_spot_ois_long_and_single_pillars_match_bitwise(shape: str) -> None:
    assert_bitwise(v2_request(shapes(len(LONG))[shape], LONG))
    assert_bitwise(v2_request(shapes(1)[shape], ["1W"]))
    assert_bitwise(v2_request(shapes(1)[shape], ["30Y"]))
    assert_bitwise(v2_request(shapes(3)[shape], ["1W", "10Y", "30Y"]))


def test_bucket_shocks_and_their_pairs_match_bitwise() -> None:
    # What the Hessian rebuilds: each bucket bumped alone and every pair, both signs.
    base = v2_request(shapes(len(MOEX))["upward"])
    n = len(RISK_BUCKETS)
    vectors: list[tuple[float, ...]] = []
    for i in range(n):
        for sign in (1.0, -1.0):
            vectors.append(tuple(sign * 25.0 if k == i else 0.0 for k in range(n)))
    for i, j in combinations(range(0, n, 3), 2):
        vectors.append(tuple(25.0 if k == i else (-25.0 if k == j else 0.0) for k in range(n)))
    vectors.append((-1500.0,) * n)  # a large parallel move through zero
    for shifts in vectors:
        assert_bitwise(_shocked_request(base, shifts))


LEGACY_DATE = date(2025, 1, 15)


def legacy_request(
    rates: list[float], days: list[int], convention: CurveConvention | None = None
) -> CurveBootstrapRequest:
    quotes = tuple(
        OisQuote(f"{d}D", LEGACY_DATE + timedelta(days=d), r) for d, r in zip(days, rates)
    )
    return CurveBootstrapRequest(
        LEGACY_DATE, quotes, CurveConvention() if convention is None else convention
    )


LEGACY_DAYS = [1, 7, 30, 91, 182, 365, 548, 730, 1095, 1826, 3652, 5479, 7305]


@pytest.mark.parametrize("shape", sorted(shapes(1)))
def test_legacy_matches_the_reference_bitwise(shape: str) -> None:
    rates = shapes(len(LEGACY_DAYS))[shape]
    assert_bitwise(legacy_request(rates, LEGACY_DAYS))
    scheduled = CurveConvention(fixed_leg_schedule=ScheduleConvention(calendar=CAL))
    assert_bitwise(legacy_request(rates, LEGACY_DAYS, scheduled))


def test_bracket_expansion_and_extreme_quotes_match_bitwise() -> None:
    # Forwards beyond the (-0.5, 2.0) start bracket: the bracket doubles until it holds.
    assert_bitwise(legacy_request([3.5, 6.0, 0.01], [7, 30, 365]))
    assert_bitwise(v2_request([2.5, 4.0, 0.3, 0.05], ["1W", "2W", "1M", "1Y"]))
    # A par curve no positive forward reaches: both fail with the same error.
    infeasible = legacy_request([0.10, 0.12, 0.60], [365, 3650, 18262])
    with pytest.raises(ValueError, match="not bracketed") as fast:
        bootstrap_discount_curve(infeasible)
    with pytest.raises(ValueError, match="not bracketed") as reference:
        reference_bootstrap(infeasible)
    assert str(fast.value) == str(reference.value)


def test_node_curve_reads_the_curve_bitwise() -> None:
    curve = bootstrap_discount_curve(v2_request(shapes(len(MOEX))["humped"]))
    nodes = list(curve.nodes)
    targets = [TRADE + timedelta(days=d) for d in range(0, 4000, 13)]
    targets += [d for d, _ in nodes] + [nodes[-1][0] + timedelta(days=d) for d in (1, 3, 400)]
    for moving in range(len(nodes)):
        for count in (moving + 1, len(nodes)):  # the moving node last, and inside
            prefix = nodes[:count]
            view = _NodeCurve(TRADE, prefix, moving, V2, targets)
            for scale in (1.0, 0.97, 1.03):
                df = nodes[moving][1] * scale
                trial = [*prefix]
                trial[moving] = (trial[moving][0], df)
                expected = DiscountCurve(TRADE, tuple(trial), convention=V2)
                values = view.move(df)
                assert [v.hex() for v in values] == [
                    expected.discount_factor(t).hex() for t in targets
                ]
                assert all(values[p] == view.values[p] for p in range(view.first_moving))
    with pytest.raises(ValueError, match="positive and finite"):
        _NodeCurve(TRADE, nodes, 0, V2, targets).move(0.0)


def test_shifts_that_move_no_quote_share_one_bootstrap(monkeypatch: pytest.MonkeyPatch) -> None:
    base_request = v2_request(shapes(len(MOEX))["upward"])
    built: list[CurveBootstrapRequest] = []

    def counted(request: CurveBootstrapRequest) -> DiscountCurve:
        built.append(request)
        return bootstrap_discount_curve(request)

    monkeypatch.setattr(risk_common, "bootstrap_discount_curve", counted)
    cache = ShockedCurveCache()
    base = risk_common._base_curve(base_request, cache)
    one = tuple(10.0 if k == 0 else 0.0 for k in range(len(RISK_BUCKETS)))
    beyond = (0.0,) * (len(RISK_BUCKETS) - 1) + (10.0,)  # 20Y+: no quote past 15Y
    both = tuple(a + b for a, b in zip(one, beyond))
    assert _shock_curve(base_request, base, beyond, cache) is base
    shocked = _shock_curve(base_request, base, one, cache)
    assert _shock_curve(base_request, base, both, cache) is shocked
    assert len(built) == 2 and cache.misses == 4 and len(cache) == 4
    assert shocked == bootstrap_discount_curve(_shocked_request(base_request, one))
    # The cache keys and its export are unchanged: one entry per shift vector.
    assert sorted(e.shifts for e in cache.export()) == sorted(
        [(0.0,) * len(RISK_BUCKETS), one, beyond, both]
    )
    uncached = ShockedCurveCache(max_entries=0)
    _shock_curve(base_request, base, beyond, uncached)
    _shock_curve(base_request, base, beyond, uncached)
    assert len(uncached) == 0 and len(built) == 4


def test_layout_follows_the_calendar() -> None:
    # The cached swap layout is keyed by the convention: same calendar label, another
    # holiday, other periods.
    moved = replace(CAL, holidays=frozenset({date(2027, 10, 21)}))
    rates = shapes(len(MOEX))["upward"]
    first = bootstrap_discount_curve(v2_request(rates))
    other = CurveBootstrapRequest(
        TRADE,
        ois_quotes(TRADE, list(zip(MOEX, rates)), moved),
        ois_curve_convention("rub_ruonia_ois_moex_v2", moved),
    )
    assert bootstrap_discount_curve(other).nodes != first.nodes
    assert_bitwise(other)
