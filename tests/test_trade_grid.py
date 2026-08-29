from __future__ import annotations

import math
from dataclasses import replace
from datetime import date
from typing import Any

import pytest

from rub_fixed_income.trade_grid import (
    GridBuildPolicy,
    GridIdentity,
    TradeGrid,
    TradeGridNode,
    TradeMetricLookup,
    build_trade_grid,
    lookup_trade_metric,
)


def _identity() -> GridIdentity:
    return GridIdentity(
        terms_hash="terms-v1",
        cashflow_hash="cashflows-v1",
        settlement_date=date(2026, 8, 12),
        horizon="maturity",
        horizon_date=date(2030, 1, 1),
        metric="yield",
        units="bp",
        curve_world_instance_id=None,
        pricer_version="core-0.1.0",
        grid_policy_version="bond_trade_grid_v1",
    )


def _nonlinear_price(metric_bp: float, curvature: float) -> float:
    x = metric_bp / 10_000.0
    return 100.0 * math.exp(-4.0 * x + curvature * x * x)


def _constant_metric(_: float, *, expected: float) -> float:
    return expected


def _unexpected_exact_lookup(_: float) -> float:
    raise AssertionError("accepted in-range grid lookup called the exact solver")


@pytest.mark.parametrize(
    ("structure", "curvature"),
    [("bullet", 4.0), ("amortizing", 2.0), ("confirmed_put", 0.75)],
)
def test_accepted_grid_matches_held_out_exact_metric_within_tolerance(
    structure: str, curvature: float
) -> None:
    grid = build_trade_grid(
        grid_id=f"grid-{structure}",
        structure=structure,
        center_metric_bp=1600.0,
        exact_price=lambda metric: _nonlinear_price(metric, curvature),
        policy=GridBuildPolicy(metric_error_bp_max=1.5, max_nodes=129),
        identity=_identity(),
    )

    assert grid.accepted
    assert len(grid.nodes) >= 11
    for metric in (1412.5, 1537.5, 1675.0, 1787.5):
        lookup = lookup_trade_metric(
            grid,
            _nonlinear_price(metric, curvature),
            _unexpected_exact_lookup,
            expected_identity=_identity(),
        )
        assert lookup.method == "grid_pchip"
        assert lookup.metric_bp == pytest.approx(metric, abs=1.5)
        assert lookup.grid_id == grid.grid_id

    assert grid.held_out_count >= 7 * (len(grid.nodes) - 1)
    assert grid.held_out_metric_error_bp_max <= grid.metric_error_bp_max


def test_invalid_and_out_of_range_grids_use_exact_fallback() -> None:
    valid = build_trade_grid(
        grid_id="grid-1",
        structure="bullet",
        center_metric_bp=1600.0,
        exact_price=lambda metric: 100.0 - metric / 1000.0,
        policy=GridBuildPolicy(1.5),
        identity=_identity(),
    )
    invalid = TradeGrid(
        grid_id="bad",
        structure="bullet",
        nodes=(TradeGridNode(100.0, 1600.0), TradeGridNode(100.0, 1500.0)),
        accepted=True,
        metric_error_bp_max=0.1,
    )

    out_of_range = lookup_trade_metric(
        valid, 102.0, lambda _: 1432.1, expected_identity=_identity()
    )
    bad = lookup_trade_metric(invalid, 100.0, lambda _: 1599.9)

    assert (out_of_range.method, out_of_range.reason_code, out_of_range.metric_bp) == (
        "exact_fallback",
        "GRID_OUT_OF_RANGE",
        1432.1,
    )
    assert (bad.method, bad.reason_code, bad.metric_bp) == (
        "exact_fallback",
        "GRID_INVALID",
        1599.9,
    )


def test_stale_grid_uses_exact_fallback() -> None:
    grid = build_trade_grid(
        grid_id="stale-grid",
        structure="bullet",
        center_metric_bp=1600.0,
        exact_price=lambda metric: 100.0 - metric / 1000.0,
        policy=GridBuildPolicy(1.5),
        identity=_identity(),
        valid_through=date(2026, 8, 12),
    )

    lookup = lookup_trade_metric(
        grid,
        100.0,
        lambda _: 1600.0,
        as_of=date(2026, 8, 13),
        expected_identity=_identity(),
    )

    assert (lookup.method, lookup.reason_code, lookup.grid_id) == (
        "exact_fallback",
        "GRID_STALE",
        None,
    )


def test_dated_grid_without_as_of_uses_exact_fallback() -> None:
    grid = build_trade_grid(
        grid_id="dated",
        structure="bullet",
        center_metric_bp=1600.0,
        exact_price=lambda metric: 100.0 - metric / 1000.0,
        policy=GridBuildPolicy(1.5),
        identity=_identity(),
        valid_through=date(2026, 8, 12),
    )

    lookup = lookup_trade_metric(grid, 100.0, lambda _: 1600.0, expected_identity=_identity())

    assert lookup.reason_code == "GRID_STALE"


@pytest.mark.parametrize("tolerance,max_nodes", [(0.0, 129), (-0.1, 129), (0.1, 10)])
def test_grid_builder_rejects_invalid_policy(tolerance: float, max_nodes: int) -> None:
    with pytest.raises(ValueError, match="positive|seed"):
        GridBuildPolicy(tolerance, max_nodes)


def test_identity_less_legacy_grid_uses_exact_fallback() -> None:
    grid = TradeGrid(
        "adversarial",
        "bullet",
        (TradeGridNode(99.0, 1700.0), TradeGridNode(101.0, 1500.0)),
        True,
        0.1,
    )

    lookup = lookup_trade_metric(grid, 100.0, lambda _: 1599.5)

    assert (lookup.method, lookup.reason_code, lookup.metric_bp) == (
        "exact_fallback",
        "GRID_IDENTITY_MISSING",
        1599.5,
    )


def test_grid_builder_rejects_unsupported_structure() -> None:
    with pytest.raises(ValueError, match="unsupported structure"):
        build_trade_grid(
            grid_id="floater",
            structure="floater",
            center_metric_bp=0.0,
            exact_price=lambda metric: 100.0 - metric / 1000.0,
            policy=GridBuildPolicy(0.1),
            identity=_identity(),
        )


def test_grid_builder_requires_full_identity() -> None:
    with pytest.raises(ValueError, match="identity"):
        build_trade_grid(
            grid_id="anonymous",
            structure="bullet",
            center_metric_bp=1600.0,
            exact_price=lambda metric: 100.0 - metric / 1000.0,
            policy=GridBuildPolicy(1.5),
        )


def test_lookup_rejects_unsupported_grid_structure() -> None:
    grid = TradeGrid(
        "floater",
        "floater",
        (TradeGridNode(99.0, 1700.0), TradeGridNode(101.0, 1500.0)),
        True,
        0.1,
    )

    lookup = lookup_trade_metric(grid, 100.0, lambda _: 1600.0)

    assert lookup == TradeMetricLookup(1600.0, "exact_fallback", None, "UNSUPPORTED_STRUCTURE")


def test_identified_grid_requires_lookup_context() -> None:
    grid = build_trade_grid(
        grid_id="identified-grid",
        structure="bullet",
        center_metric_bp=1600.0,
        exact_price=lambda metric: 100.0 - metric / 1000.0,
        policy=GridBuildPolicy(1.5),
        identity=_identity(),
    )

    lookup = lookup_trade_metric(grid, 98.4, lambda _: 1600.25)

    assert lookup == TradeMetricLookup(
        1600.25, "exact_fallback", None, "GRID_LOOKUP_CONTEXT_MISSING"
    )


def test_rejected_grid_retains_full_identity() -> None:
    identity = _identity()
    grid = build_trade_grid(
        grid_id="rejected-grid",
        structure="bullet",
        center_metric_bp=1600.0,
        exact_price=lambda _: 100.0,
        policy=GridBuildPolicy(1.5),
        identity=identity,
    )

    assert not grid.accepted
    assert grid.identity == identity


def test_z_spread_grid_identity_requires_curve_world() -> None:
    with pytest.raises(ValueError, match="Curve World"):
        replace(_identity(), metric="z_spread")


@pytest.mark.parametrize(
    "changed",
    [
        {"held_out_count": 0},
        {"held_out_metric_error_bp_max": math.inf},
        {"held_out_metric_error_bp_max": 1.6},
        {"metric_error_bp_max": math.nan},
        {"nodes": (TradeGridNode(99.0, 1700.0), TradeGridNode(101.0, 1500.0))},
        {"certification_sha256": "0" * 64},
        {"grid_id": "forged-grid"},
        {"structure": "amortizing"},
        {"valid_through": date(2026, 8, 12)},
    ],
)
def test_uncertified_grid_uses_exact_fallback(changed: dict[str, Any]) -> None:
    identity = _identity()
    grid = build_trade_grid(
        grid_id="certified-grid",
        structure="bullet",
        center_metric_bp=1600.0,
        exact_price=lambda metric: 100.0 - metric / 1000.0,
        policy=GridBuildPolicy(1.5),
        identity=identity,
    )

    lookup = lookup_trade_metric(
        replace(grid, **changed),
        98.4,
        lambda _: 1600.25,
        expected_identity=identity,
    )

    assert lookup == TradeMetricLookup(1600.25, "exact_fallback", None, "GRID_UNCERTIFIED")


def test_runtime_linear_guard_never_calls_exact_for_certified_in_range_lookup() -> None:
    identity = _identity()
    exact_calls = 0

    def nonlinear_price(metric: float) -> float:
        x = metric / 10_000.0
        return 100.0 * math.exp(-12.0 * x + 12.0 * x * x)

    grid = build_trade_grid(
        grid_id="nonlinear-grid",
        structure="bullet",
        center_metric_bp=0.0,
        exact_price=nonlinear_price,
        policy=GridBuildPolicy(1.5),
        identity=identity,
    )

    def forbidden_exact(_: float) -> float:
        nonlocal exact_calls
        exact_calls += 1
        raise AssertionError("accepted in-range lookup called exact solver")

    lookup = lookup_trade_metric(
        grid,
        nonlinear_price(-10.0),
        forbidden_exact,
        expected_identity=identity,
    )

    assert lookup.method == "grid_pchip"
    assert exact_calls == 0


@pytest.mark.parametrize(
    ("changed", "reason"),
    [
        ({"terms_hash": "terms-v2"}, "GRID_TERMS_MISMATCH"),
        ({"cashflow_hash": "cashflows-v2"}, "GRID_CASHFLOW_MISMATCH"),
        ({"settlement_date": date(2026, 8, 13)}, "GRID_SETTLEMENT_MISMATCH"),
        ({"horizon": "confirmed_investor_put"}, "GRID_HORIZON_MISMATCH"),
        ({"horizon_date": date(2029, 1, 1)}, "GRID_HORIZON_MISMATCH"),
        ({"metric": "z_spread", "curve_world_instance_id": "curve-1"}, "GRID_METRIC_MISMATCH"),
        ({"units": "pct"}, "GRID_UNITS_MISMATCH"),
        ({"curve_world_instance_id": "curve-1"}, "GRID_CURVE_WORLD_MISMATCH"),
        ({"pricer_version": "core-0.2.0"}, "GRID_PRICER_VERSION_MISMATCH"),
        ({"grid_policy_version": "bond_trade_grid_v2"}, "GRID_POLICY_VERSION_MISMATCH"),
    ],
)
def test_grid_identity_mismatch_uses_reason_coded_exact_fallback(
    changed: dict[str, Any], reason: str
) -> None:
    identity = _identity()
    grid = build_trade_grid(
        grid_id="identified-grid",
        structure="bullet",
        center_metric_bp=1600.0,
        exact_price=lambda metric: 100.0 - metric / 1000.0,
        policy=GridBuildPolicy(1.5),
        identity=identity,
    )

    lookup = lookup_trade_metric(
        grid,
        98.4,
        lambda _: 1600.25,
        expected_identity=replace(identity, **changed),
    )

    assert lookup == TradeMetricLookup(1600.25, "exact_fallback", None, reason)
