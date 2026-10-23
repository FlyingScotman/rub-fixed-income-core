"""Closed-form gradient and Hessian of a fixed-cashflow bond on a zero-shocked curve (0.37.0).

``BondRiskEngine.second_order(method="analytic")``: the same factors and numbers as the
central-difference path (11 buckets of the curve's own basis and the spread), from one pass
over the cash flows instead of 289 repricings.

Internally every flow is a function of the annual (or continuous) zero overlay ``x`` at the
11 bucket nodes, ``DF'(t) = (DF(t)^(-1/t) + w(t).x)^(-t)`` (or ``DF(t) exp(-w(t).x t)``),
through the settlement-forward discount factor and the request's Z-spread convention. A
par-yield curve (``zero_shock="annual_par"``) maps the zero-space risk to its par factors
``q`` through the Jacobian ``J = dq/dx`` of its par instruments (``_par_overlay``):

    dV/dq   = J^-T dV/dx
    d2V/dq2 = J^-T (d2V/dx2 - sum_l (dV/dq)_l d2q_l/dx2) J^-1

The second term is the curvature of the instruments themselves (implicit-function theorem):
without it a book of par instruments would show a gamma it does not have.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np

from ._par_overlay import PAR_FIRST_BUCKET, PAR_YEARS, par_jacobians
from ._risk_common import RISK_BUCKETS, _zero_shock_terms
from .conventions import ZSpreadConvention
from .discount import DiscountCurve
from .errors import InvalidTerms

if TYPE_CHECKING:
    from .risk import BondRiskEngine

_BP = 1e-4
_N = len(RISK_BUCKETS)


def _log_df_derivatives(curve: DiscountCurve, target: object) -> tuple[np.ndarray, np.ndarray]:
    """Gradient (per bp) and Hessian (per bp^2) of ln DF'(target) in the overlay at x = 0."""
    time, weights, base_df = _zero_shock_terms(curve, target)  # type: ignore[arg-type]
    w = np.zeros(_N)
    for i, value in weights:
        w[i] = value
    if time <= 0.0:
        return np.zeros(_N), np.zeros((_N, _N))
    if curve.convention.zero_shock == "continuous":
        return -time * w * _BP, np.zeros((_N, _N))
    growth = base_df ** (-1.0 / time)
    return -time * w * _BP / growth, time * np.outer(w, w) * (_BP / growth) ** 2


def _par_map() -> np.ndarray:
    """11 bucket shifts from the 8 par nodes: 1M-6M follow 1Y."""
    e = np.zeros((_N, len(PAR_YEARS)))
    for i in range(_N):
        e[i, max(i - PAR_FIRST_BUCKET, 0)] = 1.0
    return e


def flow_partials(
    amount: float, forward: float, tau: float, spread: float, convention: ZSpreadConvention
) -> tuple[float, float, float, float, float, float]:
    """PV of one flow and its partials in ``L = ln(forward DF)`` and the decimal spread ``Z``:
    ``(P, dP/dL, d2P/dL2, dP/dZ, d2P/dZ2, d2P/dLdZ)``, the request's spread convention."""
    if convention.compounding.is_continuous:
        value = amount * forward * float(np.exp(-spread * tau))
        return value, value, value, -tau * value, tau * tau * value, -tau * value
    m = convention.compounding.periods_per_year
    mt = m * tau
    e = forward ** (-1.0 / mt)
    u = e + spread / m
    if u <= 0.0:
        raise ValueError("spread is outside the compounding domain")
    value = amount * u**-mt
    p_l = amount * e * u ** (-mt - 1.0)
    p_ll = amount * (-e / mt * u ** (-mt - 1.0) + e * e * (mt + 1.0) / mt * u ** (-mt - 2.0))
    p_z = -tau * amount * u ** (-mt - 1.0)
    p_zz = tau * (mt + 1.0) / m * amount * u ** (-mt - 2.0)
    p_lz = -amount * e * (mt + 1.0) / m * u ** (-mt - 2.0)
    return value, p_l, p_ll, p_z, p_zz, p_lz


def supports_analytic(engine: BondRiskEngine) -> str | None:
    """None when ``engine`` has a closed form, else the reason it has not."""
    req = engine.request
    if not isinstance(req.curve, DiscountCurve):
        return "the closed form needs a DiscountCurve; OIS quote risk is bumped"
    if req.floating_leg is not None:
        return "floating coupons re-project under shocks; they are bumped"
    if req.perpetual_tail is not None:
        return "a perpetual tail is bumped"
    return None


def analytic_second_order(
    engine: BondRiskEngine,
) -> tuple[tuple[float, ...], tuple[tuple[float, ...], ...]]:
    """Gradient (RUB/bp) and Hessian (RUB/bp^2) over 11 buckets and the spread, signed by
    quantity, in the engine curve's basis."""
    reason = supports_analytic(engine)
    if reason is not None:
        raise InvalidTerms(reason)
    req = engine.request
    curve = engine._curve
    settlement = req.settlement_date
    convention = req.spread_convention
    a_s, b_s = _log_df_derivatives(curve, settlement)
    df_s = curve.discount_factor(settlement)
    spread = engine.spread_bp * _BP
    g_x = np.zeros(_N)
    h_xx = np.zeros((_N, _N))
    h_xz = np.zeros(_N)
    g_z = h_zz = price = 0.0
    for payment, amount in engine._fixed_flows():
        if payment <= settlement or amount == 0.0:
            continue
        a_k, b_k = _log_df_derivatives(curve, payment)
        grad_l, hess_l = a_k - a_s, b_k - b_s
        forward = curve.discount_factor(payment) / df_s
        tau = convention.day_count.year_fraction(settlement, payment)
        value, p_l, p_ll, p_z, p_zz, p_lz = flow_partials(amount, forward, tau, spread, convention)
        price += value
        g_x += p_l * grad_l
        h_xx += p_ll * np.outer(grad_l, grad_l) + p_l * hess_l
        h_xz += p_lz * _BP * grad_l
        g_z += p_z * _BP
        h_zz += p_zz * _BP * _BP
    if not np.isclose(price, engine.base_price_rub, rtol=1e-9, atol=1e-9):
        raise ValueError("closed-form price does not reproduce the engine's base price")
    if curve.convention.zero_shock == "annual_par":
        jacobian, hessians = par_jacobians(curve)
        inverse = np.linalg.inv(jacobian)
        e_map = _par_map()
        g_q = inverse.T @ (e_map.T @ g_x)
        curvature = sum(g * h for g, h in zip(g_q, hessians, strict=True))
        h_q = inverse.T @ (e_map.T @ h_xx @ e_map - curvature) @ inverse
        h_qz = inverse.T @ (e_map.T @ h_xz)
        g_x = np.concatenate((np.zeros(PAR_FIRST_BUCKET), g_q))
        h_xx = np.zeros((_N, _N))
        h_xx[PAR_FIRST_BUCKET:, PAR_FIRST_BUCKET:] = h_q
        h_xz = np.concatenate((np.zeros(PAR_FIRST_BUCKET), h_qz))
    q = req.quantity
    gradient = (*(float(v) * q for v in g_x), g_z * q)
    full = np.zeros((_N + 1, _N + 1))
    full[:_N, :_N] = (h_xx + h_xx.T) / 2.0
    full[:_N, _N] = full[_N, :_N] = h_xz
    full[_N, _N] = h_zz
    full *= q
    return gradient, tuple(tuple(float(v) for v in row) for row in full)
