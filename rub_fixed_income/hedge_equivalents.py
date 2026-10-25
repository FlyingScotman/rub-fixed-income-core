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

Benchmarks are fixed-cashflow bonds on one ``DiscountCurve``. Positions (0.38.0) are

- fixed-cashflow bonds on that curve: ``g`` in closed form (``_analytic_risk``);
- floaters that project or discount on it, for example a KBD floater (``ZR_YLD_CRV``)
  projected off the KBD and discounted on RUONIA OIS: the coupons are re-projected off the
  overlaid curve, ``g_j`` the central difference of the price over ``FLOATER_NODE_BUMP_BP``
  on node ``j`` (its O(h^2) error is below 1e-9 relative);
- OFZ basket futures whose bond or funding curve it is: ``g`` in closed form through the
  cheapest-to-deliver bond (its forward price over the conversion factor, the spread and the
  basis held), as the engine prices the contract. A switch of the cheapest bond is not a
  first-order effect.

A curve of a position that is not the benchmarks' (a floater's RUONIA discount curve, a
future's funding curve) is held, and a warning names those positions.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

import numpy as np

from ._analytic_risk import flow_partials, supports_analytic
from ._money import finite_money
from ._risk_common import _curve_identity, _zero_shock_terms
from .compatibility import price_cashflows
from .conventions import DayCount
from .discount import CurveBootstrapRequest, DiscountCurve, DiscountFactorCurve
from .errors import InvalidTerms
from .ofz_futures import _NO_MOVE, OfzFuturesRiskEngine
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
FLOATER_NODE_BUMP_BP = 0.1
"""Node bump (bp) of the central difference that gives a floater's node risk."""
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


def _log_df(curve: DiscountCurve, nodes: np.ndarray, target: date) -> np.ndarray:
    """d ln DF'(target) / d x_j (per bp) for the annual-zero overlay at ``nodes``."""
    time, _, base_df = _zero_shock_terms(curve, target)
    if time <= 0.0:
        return np.zeros(nodes.size)
    return np.asarray(-time * _hats(time, nodes) * _BP / base_df ** (-1.0 / time))


@dataclass(frozen=True)
class _Overlaid:
    """``base`` under the annual-zero overlay ``shifts`` (bp) at hat ``nodes`` (curve years)."""

    base: DiscountCurve
    nodes: np.ndarray
    shifts: np.ndarray

    @property
    def curve_date(self) -> date:
        return self.base.curve_date

    def discount_factor(self, target: date) -> float:
        time, _, base_df = _zero_shock_terms(self.base, target)
        if time <= 0.0:
            return base_df
        growth = base_df ** (-1.0 / time) + float(_hats(time, self.nodes) @ self.shifts) * _BP
        if growth <= 0.0:
            raise ValueError("shocked annual yield must exceed -100 %")
        return float(growth ** (-time))


def _node_gradient(engine: BondRiskEngine, nodes: np.ndarray) -> np.ndarray:
    """d P / d x_j (RUB per bond per bp) for the annual-zero overlay at ``nodes``."""
    req = engine.request
    curve = engine._curve
    settlement = req.settlement_date

    def log_df(target: date) -> np.ndarray:
        return _log_df(curve, nodes, target)

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


def _floater_price(
    engine: BondRiskEngine, discount: DiscountFactorCurve, projection: DiscountFactorCurve
) -> float:
    """The floater's dirty price per bond, coupons re-projected off ``projection``, the
    calibrated spread held over ``discount``."""
    req = engine.request
    leg = req.floating_leg
    assert leg is not None
    coupons = (
        r.base if r.last is None else leg.coupon(r.index, projection, check_guard=False)
        for r in engine._reuse
    )
    flows = sorted((*engine._fixed_flows(), *((c.payment_date, c.amount_rub) for c in coupons)))
    return price_cashflows(
        flows,
        discount,
        spread=engine.spread_bp * _BP,
        settlement_date=req.settlement_date,
        convention=req.spread_convention,
    )


def _floater_gradient(
    engine: BondRiskEngine, curve: DiscountCurve, nodes: np.ndarray, discounts: bool, projects: bool
) -> np.ndarray:
    """d P / d x_j (RUB per bond per bp), central differences of ``FLOATER_NODE_BUMP_BP``;
    the overlay moves the engine's discount and/or projection curve (``curve`` itself)."""
    h = FLOATER_NODE_BUMP_BP
    gradient = np.zeros(nodes.size)
    for j in range(nodes.size):
        values = []
        for sign in (1.0, -1.0):
            shifts = np.zeros(nodes.size)
            shifts[j] = sign * h
            view = _Overlaid(curve, nodes, shifts)
            values.append(
                _floater_price(
                    engine,
                    view if discounts else engine._curve,
                    view if projects else engine._projection,
                )
            )
        gradient[j] = (values[0] - values[1]) / (2.0 * h)
    return gradient


def _futures_gradient(
    engine: OfzFuturesRiskEngine,
    curve: DiscountCurve,
    nodes: np.ndarray,
    bond_curve: bool,
    funding_curve: bool,
) -> np.ndarray:
    """d F / d x_j (RUB per contract per bp) through the cheapest-to-deliver bond: its forward
    clean price over the conversion factor, its spread and the basis held."""
    req = engine.request
    settlement, delivery = req.spot_settlement_date, req.contract.delivery_date
    index = engine._price(_NO_MOVE)[1]
    bond, spread = engine._bonds[index], engine.spreads[index]
    bonds_on, funding_on = engine._bond_curve, engine._funding_curve
    basis = engine.basis_bp * _BP
    dirty = engine._dirty(bond, bonds_on, spread)
    d_dirty = np.zeros(nodes.size)
    if bond_curve:
        a_s = _log_df(curve, nodes, settlement)
        df_s = bonds_on.discount_factor(settlement)
        for paid, amount, time in bond.flows:
            value = amount * bonds_on.discount_factor(paid) / df_s * math.exp(-spread * time)
            d_dirty += value * (_log_df(curve, nodes, paid) - a_s)

    def growth(start: date) -> float:
        ratio = funding_on.discount_factor(start) / funding_on.discount_factor(delivery)
        return ratio * math.exp(basis * DayCount.ACT_365_FIXED.year_fraction(start, delivery))

    d_forward = growth(settlement) * d_dirty
    if funding_curve:
        a_d = _log_df(curve, nodes, delivery)
        d_forward += dirty * growth(settlement) * (_log_df(curve, nodes, settlement) - a_d)
        for paid, amount in bond.carry_coupons:
            d_forward -= amount * growth(paid) * (_log_df(curve, nodes, paid) - a_d)
    forward = engine._forward_dirty(bond, dirty, funding_on, basis)
    lot = req.contract.bonds_per_lot
    if not np.isclose(
        lot * (forward - bond.accrued_delivery) / bond.conversion_factor,
        engine.base_price,
        rtol=1e-9,
        atol=1e-9,
    ):
        raise ValueError("the cheapest bond does not reproduce the engine's futures price")
    return np.asarray(lot / bond.conversion_factor * d_forward)


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
    horizons: Sequence[date | None],
    first: tuple[float, date, str, BondRiskEngine],
    last: tuple[float, date, str, BondRiskEngine],
) -> list[str]:
    """Positions whose last curve date (``horizons``) is before the first benchmark or after
    the last."""
    early, late = [], []
    for index, horizon in enumerate(horizons):
        if horizon is None:
            continue
        if horizon < first[1]:
            early.append(str(index))
        if horizon > last[1]:
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


def _identity(curve: DiscountCurve | CurveBootstrapRequest) -> object:
    """The curve's canonical identity, compared by value: equal curves built apart match."""
    return _curve_identity(curve).value


@dataclass(frozen=True)
class _Exposure:
    gradient: np.ndarray
    """RUB per bp of each node for the position (signed by quantity)."""
    horizon: date | None
    """The last date at which the position reads the benchmarks' curve."""
    holds_other_curve: bool


def _exposure(
    engine: BondRiskEngine | OfzFuturesRiskEngine,
    curve: DiscountCurve,
    identity: object,
    nodes: np.ndarray,
) -> _Exposure:
    if isinstance(engine, OfzFuturesRiskEngine):
        bonds = _identity(engine.request.bond_curve) == identity
        funding = _identity(engine.request.funding_curve) == identity
        if not (bonds or funding):
            raise InvalidTerms("benchmarks and positions must be on the same curve")
        index = engine._price(_NO_MOVE)[1]
        dates = [engine.request.contract.delivery_date] if funding else []
        if bonds:
            dates += [paid for paid, _, _ in engine._bonds[index].flows]
        gradient = _futures_gradient(engine, curve, nodes, bonds, funding)
        return _Exposure(gradient * engine.request.quantity, max(dates), not (bonds and funding))
    if not isinstance(engine, BondRiskEngine):
        raise TypeError("positions must be BondRiskEngine or OfzFuturesRiskEngine")
    req = engine.request
    settlement = req.settlement_date
    leg = req.floating_leg
    if leg is None:
        reason = supports_analytic(engine)
        if reason is not None:
            raise InvalidTerms(f"benchmark equivalents: {reason}")
        if _identity(engine._curve) != identity:
            raise InvalidTerms("benchmarks and positions must be on the same curve")
        payments = [p for p, a in engine._fixed_flows() if p > settlement and a != 0.0]
        return _Exposure(
            _node_gradient(engine, nodes) * req.quantity,
            max(payments) if payments else None,
            False,
        )
    discounts = _identity(engine._curve) == identity
    projects = _identity(engine._projection) == identity
    if not (discounts or projects):
        raise InvalidTerms("benchmarks and positions must be on the same curve")
    dates = []
    if discounts:
        dates += [p for p, a in engine._fixed_flows() if p > settlement and a != 0.0]
        dates += [c.payment_date for c in engine.projected_coupons]
    if projects:
        dates += [r.last for r in engine._reuse if r.last is not None]
    gradient = _floater_gradient(engine, curve, nodes, discounts, projects)
    return _Exposure(
        gradient * req.quantity, max(dates) if dates else None, not (discounts and projects)
    )


def benchmark_equivalents(
    benchmarks: Sequence[tuple[str, BondRiskEngine]],
    positions: Sequence[BondRiskEngine | OfzFuturesRiskEngine],
) -> BenchmarkHedge:
    """The positions' rate risk against ``benchmarks`` (``(instrument_id, engine)``, each
    engine calibrated to the benchmark's price; its quantity is ignored).

    Every benchmark must be a fixed-cashflow bond on the same ``DiscountCurve``
    (:class:`InvalidTerms` otherwise, naming the first that is not); benchmark maturities must
    differ. Positions are fixed-cashflow bonds on that curve, floaters that project or
    discount on it and OFZ futures (:class:`OfzFuturesRiskEngine`) whose bond or funding
    curve it is; anything else is :class:`InvalidTerms` with the reason. The positions'
    spreads (and a future's basis), like the benchmarks', are held.

    ``warnings`` name the positions (by index in ``positions``) that mature before the first
    benchmark or pay after the last (that risk is carried by the end benchmark, the overlay
    being flat beyond it; for a floater the last date its projection reads counts, for a
    future the cheapest bond's maturity), those with a curve other than the benchmarks'
    (held: its risk is not in this view), and hedges that amplify the positions' risk more
    than ``AMPLIFICATION_WARNING`` times.
    """
    if not benchmarks:
        raise InvalidTerms("at least one benchmark is required")
    first = benchmarks[0][1]
    curve = first._curve
    if not isinstance(first.request.curve, DiscountCurve):
        raise InvalidTerms("benchmark equivalents need a DiscountCurve")
    identity = _identity(curve)
    for _, engine in benchmarks:
        reason = supports_analytic(engine)
        if reason is not None:
            raise InvalidTerms(f"benchmark equivalents: {reason}")
        if _identity(engine._curve) != identity:
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
    exposures = [_exposure(engine, curve, identity, times) for engine in positions]
    dv01 = np.array([_own_yield_dv01(r[3]) for r in rows])
    per_bond = np.array([_node_gradient(r[3], times) for r in rows])
    jacobian = per_bond / dv01[:, None]
    condition = float(np.linalg.cond(jacobian))
    exposure = np.zeros(times.size)
    for item in exposures:
        exposure += item.gradient
    risk = np.linalg.solve(jacobian.T, exposure)
    warnings = _range_warnings([e.horizon for e in exposures], rows[0], rows[-1])
    held = [str(i) for i, e in enumerate(exposures) if e.holds_other_curve]
    if held:
        warnings.append(
            f"positions {', '.join(held)} also depend on a curve other than the benchmarks': "
            "it is held, its risk is not in this view"
        )
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
