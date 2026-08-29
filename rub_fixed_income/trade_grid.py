from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from itertools import pairwise

SEED_SHIFTS_BP = (-200.0, -100.0, -50.0, -25.0, -10.0, 0.0, 10.0, 25.0, 50.0, 100.0, 200.0)


@dataclass(frozen=True)
class TradeGridNode:
    clean_price_pct_of_nominal: float
    metric_bp: float


@dataclass(frozen=True)
class GridIdentity:
    terms_hash: str
    cashflow_hash: str
    settlement_date: date
    horizon: str
    horizon_date: date
    metric: str
    units: str
    curve_world_instance_id: str | None
    pricer_version: str
    grid_policy_version: str

    def __post_init__(self) -> None:
        if self.metric == "z_spread" and self.curve_world_instance_id is None:
            raise ValueError("z_spread grid identity requires a Curve World instance")


@dataclass(frozen=True)
class TradeGrid:
    grid_id: str
    structure: str
    nodes: tuple[TradeGridNode, ...]
    accepted: bool
    metric_error_bp_max: float
    valid_through: date | None = None
    held_out_count: int = 0
    held_out_metric_error_bp_max: float = math.inf
    identity: GridIdentity | None = None
    certification_sha256: str = ""


@dataclass(frozen=True)
class GridBuildPolicy:
    metric_error_bp_max: float
    max_nodes: int = 129

    def __post_init__(self) -> None:
        if self.metric_error_bp_max <= 0.0 or not math.isfinite(self.metric_error_bp_max):
            raise ValueError("metric error tolerance must be positive and finite")
        if self.max_nodes < len(SEED_SHIFTS_BP):
            raise ValueError("max nodes must fit every mandatory seed node")


@dataclass(frozen=True)
class TradeMetricLookup:
    metric_bp: float
    method: str
    grid_id: str | None
    reason_code: str | None = None


def _valid_nodes(nodes: tuple[TradeGridNode, ...]) -> bool:
    if len(nodes) < 2:
        return False
    values = tuple((node.clean_price_pct_of_nominal, node.metric_bp) for node in nodes)
    if any(not math.isfinite(value) for pair in values for value in pair):
        return False
    prices = tuple(pair[0] for pair in values)
    metrics = tuple(pair[1] for pair in values)
    return all(left < right for left, right in pairwise(prices)) and (
        all(left < right for left, right in pairwise(metrics))
        or all(left > right for left, right in pairwise(metrics))
    )


def _certification_sha256(
    grid_id: str,
    structure: str,
    nodes: tuple[TradeGridNode, ...],
    accepted: bool,
    metric_error_bp_max: float,
    valid_through: date | None,
    held_out_count: int,
    held_out_metric_error_bp_max: float,
    identity: GridIdentity,
) -> str:
    payload = {
        "grid_id": grid_id,
        "structure": structure,
        "nodes": [[node.clean_price_pct_of_nominal, node.metric_bp] for node in nodes],
        "accepted": accepted,
        "metric_error_bp_max": metric_error_bp_max,
        "valid_through": valid_through.isoformat() if valid_through is not None else None,
        "held_out_count": held_out_count,
        "held_out_metric_error_bp_max": held_out_metric_error_bp_max,
        "identity": {
            "terms_hash": identity.terms_hash,
            "cashflow_hash": identity.cashflow_hash,
            "settlement_date": identity.settlement_date.isoformat(),
            "horizon": identity.horizon,
            "horizon_date": identity.horizon_date.isoformat(),
            "metric": identity.metric,
            "units": identity.units,
            "curve_world_instance_id": identity.curve_world_instance_id,
            "pricer_version": identity.pricer_version,
            "grid_policy_version": identity.grid_policy_version,
        },
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(canonical).hexdigest()


def _slopes(x: tuple[float, ...], y: tuple[float, ...]) -> tuple[float, ...]:
    count = len(x)
    intervals = tuple(x[index + 1] - x[index] for index in range(count - 1))
    secants = tuple((y[index + 1] - y[index]) / intervals[index] for index in range(count - 1))
    if count == 2:
        return (secants[0], secants[0])
    result = [0.0] * count
    for index in range(1, count - 1):
        left, right = secants[index - 1], secants[index]
        if left * right <= 0.0:
            result[index] = 0.0
        else:
            left_weight = 2.0 * intervals[index] + intervals[index - 1]
            right_weight = intervals[index] + 2.0 * intervals[index - 1]
            result[index] = (left_weight + right_weight) / (
                left_weight / left + right_weight / right
            )
    first = ((2.0 * intervals[0] + intervals[1]) * secants[0] - intervals[0] * secants[1]) / (
        intervals[0] + intervals[1]
    )
    if first * secants[0] <= 0.0:
        first = 0.0
    elif secants[0] * secants[1] < 0.0 and abs(first) > abs(3.0 * secants[0]):
        first = 3.0 * secants[0]
    last = ((2.0 * intervals[-1] + intervals[-2]) * secants[-1] - intervals[-1] * secants[-2]) / (
        intervals[-1] + intervals[-2]
    )
    if last * secants[-1] <= 0.0:
        last = 0.0
    elif secants[-1] * secants[-2] < 0.0 and abs(last) > abs(3.0 * secants[-1]):
        last = 3.0 * secants[-1]
    result[0], result[-1] = first, last
    return tuple(result)


def _interpolate(nodes: tuple[TradeGridNode, ...], price: float, *, linear: bool = False) -> float:
    x = tuple(node.clean_price_pct_of_nominal for node in nodes)
    y = tuple(node.metric_bp for node in nodes)
    index = next(
        position for position in range(len(x) - 1) if x[position] <= price <= x[position + 1]
    )
    width = x[index + 1] - x[index]
    t = (price - x[index]) / width
    if linear:
        return y[index] + t * (y[index + 1] - y[index])
    slopes = _slopes(x, y)
    return (
        (2.0 * t**3 - 3.0 * t**2 + 1.0) * y[index]
        + (t**3 - 2.0 * t**2 + t) * width * slopes[index]
        + (-2.0 * t**3 + 3.0 * t**2) * y[index + 1]
        + (t**3 - t**2) * width * slopes[index + 1]
    )


def build_trade_grid(
    *,
    grid_id: str,
    structure: str,
    center_metric_bp: float,
    exact_price: Callable[[float], float],
    policy: GridBuildPolicy,
    identity: GridIdentity | None = None,
    valid_through: date | None = None,
) -> TradeGrid:
    if structure not in {"bullet", "amortizing", "confirmed_put"}:
        raise ValueError(f"unsupported structure: {structure}")
    if identity is None:
        raise ValueError("full grid identity is required")
    metric_nodes = sorted({center_metric_bp + shift for shift in SEED_SHIFTS_BP})
    while True:
        nodes = tuple(
            sorted(
                (TradeGridNode(exact_price(metric), metric) for metric in metric_nodes),
                key=lambda node: node.clean_price_pct_of_nominal,
            )
        )
        if not _valid_nodes(nodes):
            return TradeGrid(
                grid_id,
                structure,
                nodes,
                False,
                policy.metric_error_bp_max,
                identity=identity,
            )
        failures: list[float] = []
        held_out_errors: list[float] = []
        ordered_metrics = sorted(metric_nodes)
        for left, right in pairwise(ordered_metrics):
            for weight in (0.125, 0.25, 0.375, 0.5, 0.625, 0.75, 0.875):
                exact_metric = left + (right - left) * weight
                price = exact_price(exact_metric)
                pchip = _interpolate(nodes, price)
                linear = _interpolate(nodes, price, linear=True)
                error = max(abs(pchip - exact_metric), abs(pchip - linear))
                held_out_errors.append(error)
                if error > policy.metric_error_bp_max:
                    failures.append(exact_metric)
        if not failures:
            held_out_count = len(held_out_errors)
            held_out_error = max(held_out_errors)
            return TradeGrid(
                grid_id,
                structure,
                nodes,
                True,
                policy.metric_error_bp_max,
                valid_through=valid_through,
                held_out_count=held_out_count,
                held_out_metric_error_bp_max=held_out_error,
                identity=identity,
                certification_sha256=_certification_sha256(
                    grid_id,
                    structure,
                    nodes,
                    True,
                    policy.metric_error_bp_max,
                    valid_through,
                    held_out_count,
                    held_out_error,
                    identity,
                ),
            )
        room = policy.max_nodes - len(metric_nodes)
        if room <= 0:
            return TradeGrid(
                grid_id,
                structure,
                nodes,
                False,
                policy.metric_error_bp_max,
                held_out_count=len(held_out_errors),
                held_out_metric_error_bp_max=max(held_out_errors),
                identity=identity,
            )
        metric_nodes.extend(failures[:room])
        metric_nodes = sorted(set(metric_nodes))


def lookup_trade_metric(
    grid: TradeGrid,
    clean_price_pct_of_nominal: float,
    exact_metric: Callable[[float], float],
    *,
    as_of: date | None = None,
    expected_identity: GridIdentity | None = None,
) -> TradeMetricLookup:
    if grid.structure not in {"bullet", "amortizing", "confirmed_put"}:
        return TradeMetricLookup(
            exact_metric(clean_price_pct_of_nominal),
            "exact_fallback",
            None,
            "UNSUPPORTED_STRUCTURE",
        )
    if not grid.accepted or not _valid_nodes(grid.nodes):
        return TradeMetricLookup(
            exact_metric(clean_price_pct_of_nominal), "exact_fallback", None, "GRID_INVALID"
        )
    if grid.identity is None:
        return TradeMetricLookup(
            exact_metric(clean_price_pct_of_nominal),
            "exact_fallback",
            None,
            "GRID_IDENTITY_MISSING",
        )
    identity_mismatch = _identity_mismatch_reason(grid.identity, expected_identity)
    if identity_mismatch is not None:
        return TradeMetricLookup(
            exact_metric(clean_price_pct_of_nominal), "exact_fallback", None, identity_mismatch
        )
    if (
        grid.held_out_count <= 0
        or not math.isfinite(grid.metric_error_bp_max)
        or grid.metric_error_bp_max <= 0.0
        or not math.isfinite(grid.held_out_metric_error_bp_max)
        or grid.held_out_metric_error_bp_max > grid.metric_error_bp_max
        or grid.certification_sha256
        != _certification_sha256(
            grid.grid_id,
            grid.structure,
            grid.nodes,
            grid.accepted,
            grid.metric_error_bp_max,
            grid.valid_through,
            grid.held_out_count,
            grid.held_out_metric_error_bp_max,
            grid.identity,
        )
    ):
        return TradeMetricLookup(
            exact_metric(clean_price_pct_of_nominal), "exact_fallback", None, "GRID_UNCERTIFIED"
        )
    if grid.valid_through is not None and as_of is None:
        return TradeMetricLookup(
            exact_metric(clean_price_pct_of_nominal), "exact_fallback", None, "GRID_STALE"
        )
    if as_of is not None and grid.valid_through is not None and as_of > grid.valid_through:
        return TradeMetricLookup(
            exact_metric(clean_price_pct_of_nominal), "exact_fallback", None, "GRID_STALE"
        )
    lower = grid.nodes[0].clean_price_pct_of_nominal
    upper = grid.nodes[-1].clean_price_pct_of_nominal
    if not lower <= clean_price_pct_of_nominal <= upper:
        return TradeMetricLookup(
            exact_metric(clean_price_pct_of_nominal),
            "exact_fallback",
            None,
            "GRID_OUT_OF_RANGE",
        )
    return TradeMetricLookup(
        _interpolate(grid.nodes, clean_price_pct_of_nominal), "grid_pchip", grid.grid_id
    )


def _identity_mismatch_reason(
    actual: GridIdentity | None, expected: GridIdentity | None
) -> str | None:
    if expected is None:
        return "GRID_LOOKUP_CONTEXT_MISSING" if actual is not None else None
    if actual is None:
        return "GRID_IDENTITY_MISSING"
    checks = (
        (actual.terms_hash != expected.terms_hash, "GRID_TERMS_MISMATCH"),
        (actual.cashflow_hash != expected.cashflow_hash, "GRID_CASHFLOW_MISMATCH"),
        (actual.settlement_date != expected.settlement_date, "GRID_SETTLEMENT_MISMATCH"),
        (
            actual.horizon != expected.horizon or actual.horizon_date != expected.horizon_date,
            "GRID_HORIZON_MISMATCH",
        ),
        (actual.metric != expected.metric, "GRID_METRIC_MISMATCH"),
        (actual.units != expected.units, "GRID_UNITS_MISMATCH"),
        (
            actual.curve_world_instance_id != expected.curve_world_instance_id,
            "GRID_CURVE_WORLD_MISMATCH",
        ),
        (actual.pricer_version != expected.pricer_version, "GRID_PRICER_VERSION_MISMATCH"),
        (
            actual.grid_policy_version != expected.grid_policy_version,
            "GRID_POLICY_VERSION_MISMATCH",
        ),
    )
    return next((reason for mismatch, reason in checks if mismatch), None)
