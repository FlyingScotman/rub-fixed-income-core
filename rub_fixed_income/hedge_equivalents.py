"""Benchmark-bond equivalents of curve risk: the hedge view of OFZ risk (0.37.0).

The risk ladder of a curve is per bucket (``IR|<curve>|b``); for hedging, the same rate risk
is shown against a set of benchmark bonds chosen by the caller (the desk monitor picks liquid
OFZ and lets the desk change them). The curve moves by an overlay of its effective annual zero
yield with hat nodes at the benchmarks' maturities (flat before the first and after the last),
each benchmark's spread to the curve held; the overlay that moves benchmark ``b``'s own
effective annual yield by 1 bp and the others' by nothing is its factor. A position's risk to
these factors is

    r = J^-T g,      J[b, j] = d y_b / d x_j,      g_j = d V / d x_j,

and ``-r_b / DV01_b`` bonds of benchmark ``b`` offset it. A benchmark held alone has risk in
its own factor only; a bond between two benchmarks maps to those two. Nodes at the hedge
bonds' maturities (rather than at fixed tenors) is what keeps a hedge to the neighbours: on
the history of 2025-2026 it beat the 11 fixed-tenor buckets by 25-37 % of residual P&L on
2.5-7.5 years (``docs/proposals/ofz-curve-risk-factors.md`` §4).

Positions and benchmarks are fixed-cashflow bonds on one ``DiscountCurve`` (the closed form of
``_analytic_risk``); floaters and other products are not covered yet.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

import numpy as np

from ._analytic_risk import flow_partials, supports_analytic
from ._money import finite_money
from ._risk_common import _curve_identity, _zero_shock_terms
from .conventions import DayCount
from .discount import DiscountCurve
from .errors import InvalidTerms
from .risk import BondRiskEngine

__all__ = [
    "BENCHMARK_EQUIVALENTS_MODEL_VERSION",
    "BenchmarkEquivalent",
    "BenchmarkHedge",
    "benchmark_equivalents",
]

BENCHMARK_EQUIVALENTS_MODEL_VERSION = "benchmark-equivalents-annual-overlay-v1"
AMPLIFICATION_WARNING = 3.0
"""Gross benchmark risk over the positions' gross node risk (``sum |r_b| / sum |g_j|``, both
RUB/bp) above which the result warns that the hedges are large and of opposite sign. A
coupon bond between benchmarks maps at 1.0-1.3, a long zero next to a 13 % coupon benchmark
at 3.8, an 11Y zero against 25 % coupon benchmarks at 5.9; benchmarks a day apart stay at
1.2-1.7 (cond(J) about 3, which is why ``condition_number`` is reported but not used)."""
_BP = 1e-4


@dataclass(frozen=True)
class BenchmarkEquivalent:
    instrument_id: str
    maturity: date
    maturity_years: float
    yield_dv01_rub: float
    """RUB per bond per +1 bp of the benchmark's own effective annual yield (ACT/365F)."""
    risk_rub_per_bp: float
    """The positions' P&L for +1 bp of this benchmark's yield, the others held."""
    hedge_quantity: float
    """Bonds of the benchmark that offset ``risk_rub_per_bp``: ``-risk / yield_dv01``."""


@dataclass(frozen=True)
class BenchmarkHedge:
    curve_date: date
    equivalents: tuple[BenchmarkEquivalent, ...]
    total_risk_rub_per_bp: float
    """Sum over the benchmarks: the positions' risk to a parallel move of every benchmark
    yield."""
    condition_number: float
    warnings: tuple[str, ...]
    model_version: str = BENCHMARK_EQUIVALENTS_MODEL_VERSION


def _hats(time: float, nodes: np.ndarray) -> np.ndarray:
    eye = np.eye(nodes.size)
    return np.array([np.interp(time, nodes, eye[j]) for j in range(nodes.size)])


def _node_gradient(engine: BondRiskEngine, nodes: np.ndarray) -> np.ndarray:
    """d P / d x_j (RUB per bond per bp) for the annual-zero overlay at ``nodes``."""
    req = engine.request
    curve = engine._curve
    settlement = req.settlement_date

    def log_df(target: date) -> np.ndarray:
        time, _, base_df = _zero_shock_terms(curve, target)
        if time <= 0.0:
            return np.zeros(nodes.size)
        return np.asarray(-time * _hats(time, nodes) * _BP / base_df ** (-1.0 / time))

    a_s = log_df(settlement)
    df_s = curve.discount_factor(settlement)
    spread = engine.spread_bp * _BP
    convention = req.spread_convention
    gradient = np.zeros(nodes.size)
    for payment, amount in engine._fixed_flows():
        if payment <= settlement or amount == 0.0:
            continue
        forward = curve.discount_factor(payment) / df_s
        tau = convention.day_count.year_fraction(settlement, payment)
        _, p_l, *_ = flow_partials(amount, forward, tau, spread, convention)
        gradient += p_l * (log_df(payment) - a_s)
    return gradient


def _own_yield_dv01(engine: BondRiskEngine) -> float:
    """dP/dy per bond per bp of the effective annual yield (ACT/365F from settlement)."""
    settlement = engine.request.settlement_date
    flows = [(p, a) for p, a in engine._fixed_flows() if p > settlement and a != 0.0]
    times = np.array([DayCount.ACT_365_FIXED.year_fraction(settlement, p) for p, _ in flows])
    amounts = np.array([a for _, a in flows])
    price = engine.base_price_rub
    y = 0.1
    for _ in range(100):
        value = float(np.sum(amounts * (1.0 + y) ** -times)) - price
        slope = float(np.sum(-times * amounts * (1.0 + y) ** (-times - 1.0)))
        step = value / slope
        y -= step
        if abs(step) < 1e-14:
            break
    else:
        raise ValueError("benchmark yield did not converge")
    return float(np.sum(-times * amounts * (1.0 + y) ** (-times - 1.0))) * _BP


def _range_warnings(
    positions: Sequence[BondRiskEngine],
    first: tuple[float, date, str, BondRiskEngine],
    last: tuple[float, date, str, BondRiskEngine],
) -> list[str]:
    """Positions maturing before the first benchmark or paying after the last."""
    early, late = [], []
    for index, engine in enumerate(positions):
        settlement = engine.request.settlement_date
        payments = [p for p, a in engine._fixed_flows() if p > settlement and a != 0.0]
        if not payments:
            continue
        if max(payments) < first[1]:
            early.append(str(index))
        if max(payments) > last[1]:
            late.append(str(index))
    warnings = []
    if early:
        warnings.append(
            f"positions {', '.join(early)} mature before the first benchmark {first[2]} "
            f"({first[1].isoformat()}): their risk is carried by it"
        )
    if late:
        warnings.append(
            f"positions {', '.join(late)} pay after the last benchmark {last[2]} "
            f"({last[1].isoformat()}): their risk past it is carried by it"
        )
    return warnings


def benchmark_equivalents(
    benchmarks: Sequence[tuple[str, BondRiskEngine]],
    positions: Sequence[BondRiskEngine],
) -> BenchmarkHedge:
    """The positions' rate risk against ``benchmarks`` (``(instrument_id, engine)``, each
    engine calibrated to the benchmark's price; its quantity is ignored).

    Every engine must be a fixed-cashflow bond on the same ``DiscountCurve``
    (:class:`InvalidTerms` otherwise, naming the first that is not); benchmark maturities must
    differ. The positions' spreads, like the benchmarks', are held.

    ``warnings`` name the positions (by index in ``positions``) that mature before the first
    benchmark or pay after the last (that risk is carried by the end benchmark, the overlay
    being flat beyond it), and hedges that amplify the positions' risk more than
    ``AMPLIFICATION_WARNING`` times.
    """
    if not benchmarks:
        raise InvalidTerms("at least one benchmark is required")
    engines = [e for _, e in benchmarks] + list(positions)
    curve = engines[0]._curve
    if not isinstance(engines[0].request.curve, DiscountCurve):
        raise InvalidTerms("benchmark equivalents need a DiscountCurve")
    identity = _curve_identity(curve)
    for engine in engines:
        reason = supports_analytic(engine)
        if reason is not None:
            raise InvalidTerms(f"benchmark equivalents: {reason}")
        if _curve_identity(engine._curve) != identity:
            raise InvalidTerms("benchmarks and positions must be on the same curve")
    rows = []
    for instrument_id, engine in benchmarks:
        last = max(p for p, a in engine._fixed_flows() if a > 0.0)
        time = _zero_shock_terms(curve, last)[0]
        rows.append((time, last, instrument_id, engine))
    rows.sort(key=lambda r: r[0])
    times = np.array([r[0] for r in rows])
    if np.any(np.diff(times) <= 0.0):
        raise InvalidTerms("benchmark maturities must differ")
    if len({r[2] for r in rows}) != len(rows):
        raise InvalidTerms("duplicate benchmark instrument")
    dv01 = np.array([_own_yield_dv01(r[3]) for r in rows])
    per_bond = np.array([_node_gradient(r[3], times) for r in rows])
    jacobian = per_bond / dv01[:, None]
    condition = float(np.linalg.cond(jacobian))
    exposure = np.zeros(times.size)
    for engine in positions:
        exposure += _node_gradient(engine, times) * engine.request.quantity
    risk = np.linalg.solve(jacobian.T, exposure)
    warnings = _range_warnings(positions, rows[0], rows[-1])
    gross = float(np.sum(np.abs(exposure)))
    amplification = float(np.sum(np.abs(risk))) / gross if gross > 0.0 else 0.0
    if amplification > AMPLIFICATION_WARNING:
        warnings.append(
            f"hedges amplify the positions' risk {amplification:.1f} times: large neighbouring "
            "hedges of opposite sign"
        )
    equivalents = tuple(
        BenchmarkEquivalent(
            instrument_id=r[2],
            maturity=r[1],
            maturity_years=float(r[0]),
            yield_dv01_rub=finite_money(float(d), "benchmark DV01"),
            risk_rub_per_bp=finite_money(float(x), "benchmark risk"),
            hedge_quantity=finite_money(float(-x / d), "hedge quantity"),
        )
        for r, d, x in zip(rows, dv01, risk, strict=True)
    )
    return BenchmarkHedge(
        curve_date=curve.curve_date,
        equivalents=equivalents,
        total_risk_rub_per_bp=finite_money(float(np.sum(risk)), "total benchmark risk"),
        condition_number=condition,
        warnings=tuple(warnings),
    )
