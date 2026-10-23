"""Risk in the desk factor vocabulary (roadmap §2, item 4).

Factor keys:

- ``IR|<curve_id>|<bucket>``: a 1 bp shift of one of the 11 bucket pillars of a curve;
- ``CS``: a 1 bp move of the position's own spread (credit or OFZ spread);
- ``FX|<ccy>``: a 1 % move of the currency against RUB;
- ``FXBASIS|<ccy>``: a 1 bp move of a calibrated FX-futures basis;
- ``REAL|<curve_id>|<bucket>``: a 1 bp shift of one of the 11 bucket pillars of a *real*
  (inflation-adjusted) discount curve. Only inflation-linked bonds carry it (package 9a),
  so books without linkers report exactly the factors they did before.

A non-RUB bond's risk is in RUB at the snapshot FX rate; ``FX|<ccy>`` and its cross terms
with the rates and spread factors come from :func:`fx_translated_factor_risk`.

``first_order`` is RUB per factor unit. ``hessian`` holds the sparse upper triangle
``(i, j, d2PV)`` with ``i <= j`` over ``factors`` (RUB per unit squared); exact zeros
are omitted. First order and Hessian both come from central differences with the same
bump (DEC08), so a Taylor expansion of the two is consistent.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

from .errors import InvalidTerms
from .fx_futures import FxFuturesRiskResult, FxFuturesSecondOrderRisk
from .risk import RISK_BUCKETS, BondRiskResult, BondSecondOrderRisk, TwoCurveBondRiskResult

__all__ = [
    "CS_FACTOR",
    "FactorRisk",
    "HessianEntry",
    "basis_view",
    "bond_factor_risk",
    "fx_basis_factor",
    "fx_factor",
    "fx_futures_factor_risk",
    "fx_translated_factor_risk",
    "ir_factor",
    "linker_factor_risk",
    "real_factor",
]

CS_FACTOR = "CS"


def _component(value: str, label: str) -> str:
    if not isinstance(value, str) or not value or "|" in value:
        raise InvalidTerms(f"{label} must be a non-empty string without '|'")
    return value


def ir_factor(curve_id: str, bucket: str) -> str:
    if bucket not in RISK_BUCKETS:
        raise InvalidTerms(f"unknown risk bucket {bucket!r}")
    return f"IR|{_component(curve_id, 'curve_id')}|{bucket}"


def real_factor(curve_id: str, bucket: str) -> str:
    """``REAL|<curve_id>|<bucket>``: 1 bp at a bucket pillar of a real discount curve."""
    if bucket not in RISK_BUCKETS:
        raise InvalidTerms(f"unknown risk bucket {bucket!r}")
    return f"REAL|{_component(curve_id, 'curve_id')}|{bucket}"


def fx_factor(currency: str) -> str:
    return f"FX|{_component(currency, 'currency')}"


def fx_basis_factor(currency: str) -> str:
    return f"FXBASIS|{_component(currency, 'currency')}"


@dataclass(frozen=True)
class HessianEntry:
    i: int
    j: int
    value: float


@dataclass(frozen=True)
class FactorRisk:
    factors: tuple[str, ...]
    first_order: tuple[float, ...]
    hessian: tuple[HessianEntry, ...]
    difference: Literal["central"]
    bump_bp: float
    has_second_order: bool

    def __post_init__(self) -> None:
        if len(set(self.factors)) != len(self.factors):
            raise InvalidTerms("duplicate risk factors")
        if len(self.first_order) != len(self.factors):
            raise InvalidTerms("first_order must align with factors")
        for entry in self.hessian:
            if not 0 <= entry.i <= entry.j < len(self.factors):
                raise InvalidTerms("Hessian entries must be upper-triangle factor indices")

    def first_order_by_factor(self) -> dict[str, float]:
        return dict(zip(self.factors, self.first_order, strict=True))

    def hessian_by_factor(self) -> dict[tuple[str, str], float]:
        return {(self.factors[e.i], self.factors[e.j]): e.value for e in self.hessian}


def _upper(matrix: tuple[tuple[float, ...], ...]) -> tuple[HessianEntry, ...]:
    return tuple(
        HessianEntry(i, j, row[j])
        for i, row in enumerate(matrix)
        for j in range(i, len(row))
        if row[j] != 0.0
    )


def bond_factor_risk(
    risk: BondRiskResult,
    curve_id: str,
    second_order: BondSecondOrderRisk | None = None,
    *,
    projection_curve_id: str | None = None,
) -> FactorRisk:
    """Map a bond's central risk (and optionally its Hessian) to desk factors.

    With ``second_order`` the first order is its central gradient, which uses the same
    bump as the Hessian. Otherwise ``risk`` must itself be central.

    A two-curve floater (:class:`TwoCurveBondRiskResult`) needs ``projection_curve_id``:
    its factors are ``IR|<curve_id>|*`` (discount), ``IR|<projection_curve_id>|*`` and
    ``CS`` (the spread over the discount curve), in that order, and its Hessian covers
    all 23 with the discount/projection cross terms.
    """
    two_curve = isinstance(risk, TwoCurveBondRiskResult)
    if two_curve != (projection_curve_id is not None):
        raise InvalidTerms("projection_curve_id is required for, and only for, two-curve risk")
    rates = tuple(ir_factor(curve_id, b) for b in RISK_BUCKETS)
    if projection_curve_id is not None:
        if projection_curve_id == curve_id:
            raise InvalidTerms("discount and projection curves need distinct curve IDs")
        rates = (*rates, *(ir_factor(projection_curve_id, b) for b in RISK_BUCKETS))
    factors = (*rates, CS_FACTOR)
    if second_order is not None:
        if second_order.base_id != risk.base_id:
            raise InvalidTerms("risk and second-order results describe different calibrations")
        if len(second_order.factors) != len(factors):
            raise InvalidTerms("unexpected second-order factor set")
        return FactorRisk(
            factors,
            second_order.gradient_rub_per_bp,
            _upper(second_order.hessian_rub_per_bp2),
            "central",
            second_order.bump_bp,
            True,
        )
    if risk.difference != "central":
        raise InvalidTerms("desk factors need central-difference risk (DEC08)")
    spread = risk.cs01_rub if risk.cs01_rub != 0.0 else risk.ofz_spr01_rub
    projection = risk.projection_buckets if isinstance(risk, TwoCurveBondRiskResult) else ()
    first = (*(b.dv01_rub for b in (*risk.buckets, *projection)), spread)
    return FactorRisk(factors, first, (), "central", risk.bump_bp, False)


def fx_translated_factor_risk(
    risk: FactorRisk, currency: str, fx_delta_rub_per_pct: float
) -> FactorRisk:
    """Add ``FX|<ccy>`` to the RUB risk of a position whose RUB value is ``FX * PV_ccy``.

    ``risk`` is already in RUB at the spot rate ``FX`` (each factor's sensitivity in
    currency times ``FX``) and ``fx_delta_rub_per_pct`` is ``PV_ccy * FX / 100``. A 1 %
    move of the rate scales every RUB amount by 1.01, so the cross term of FX with each
    factor is ``first_order_i / 100`` (only when ``risk`` has a Hessian) and the FX gamma
    is zero. Central differences of this bilinear form with a 1 % FX bump and the rates
    bump give exactly these numbers, so the Hessian stays consistent (DEC08).
    """
    factor = fx_factor(currency)
    if currency == "RUB":
        raise InvalidTerms("RUB has no FX factor against itself")
    if factor in risk.factors:
        raise InvalidTerms(f"{factor} is already a factor of this risk")
    fx_index = len(risk.factors)
    cross = (
        tuple(
            HessianEntry(i, fx_index, value / 100.0)
            for i, value in enumerate(risk.first_order)
            if value != 0.0
        )
        if risk.has_second_order
        else ()
    )
    return FactorRisk(
        (*risk.factors, factor),
        (*risk.first_order, fx_delta_rub_per_pct),
        tuple(sorted((*risk.hessian, *cross), key=lambda e: (e.i, e.j))),
        risk.difference,
        risk.bump_bp,
        risk.has_second_order,
    )


def fx_futures_factor_risk(
    risk: FxFuturesRiskResult,
    rub_curve_id: str,
    foreign_curve_id: str,
    second_order: FxFuturesSecondOrderRisk | None = None,
) -> FactorRisk:
    """Map FX-futures risk to desk factors.

    Rates and basis come from the central gradient and Hessian when given. The FX
    delta is RUB per 1 % spot move; spot is not a Hessian factor here, so FX cross
    terms are not reported.
    """
    if rub_curve_id == foreign_curve_id:
        raise InvalidTerms("RUB and foreign curves need distinct curve IDs")
    rates = (
        *(ir_factor(rub_curve_id, b) for b in RISK_BUCKETS),
        *(ir_factor(foreign_curve_id, b) for b in RISK_BUCKETS),
        fx_basis_factor(risk.currency),
    )
    factors = (*rates, fx_factor(risk.currency))
    if second_order is not None:
        if second_order.base_id != risk.base_id:
            raise InvalidTerms("risk and second-order results describe different calibrations")
        if len(second_order.factors) != len(rates):
            raise InvalidTerms("unexpected second-order factor set")
        return FactorRisk(
            factors,
            (*second_order.gradient_rub_per_bp, risk.fx_delta_rub_per_pct),
            _upper(second_order.hessian_rub_per_bp2),
            "central",
            second_order.bump_bp,
            True,
        )
    if risk.difference != "central":
        raise InvalidTerms("desk factors need central-difference risk (DEC08)")
    first = (
        *(b.dv01_rub for b in risk.rub_buckets),
        *(b.dv01_rub for b in risk.foreign_buckets),
        risk.basis01_rub,
        risk.fx_delta_rub_per_pct,
    )
    return FactorRisk(factors, first, (), "central", risk.bump_bp, False)


def linker_factor_risk(
    risk: BondRiskResult, real_curve_id: str, second_order: BondSecondOrderRisk | None = None
) -> FactorRisk:
    """Desk factors of an inflation-linked bond valued on a real curve (package 9a).

    The same numbers as :func:`bond_factor_risk` (central first order, the Hessian from
    the same bump), keyed ``REAL|<real_curve_id>|<bucket>`` and ``CS``.
    """
    nominal = bond_factor_risk(risk, real_curve_id, second_order)
    renamed = (*(real_factor(real_curve_id, b) for b in RISK_BUCKETS), CS_FACTOR)
    return FactorRisk(
        renamed,
        nominal.first_order,
        nominal.hessian,
        nominal.difference,
        nominal.bump_bp,
        nominal.has_second_order,
    )


def basis_view(risk: FactorRisk, base_curve_id: str, curve_id: str, basis_id: str) -> FactorRisk:
    """The same risk on ``(base, basis = curve - base)`` instead of ``(base, curve)``.

    With both curves bucketed alike and shocked in the same unit (for the KBD against
    RUONIA OIS: ``rub_ofz_kbd_v3`` par quotes and par OIS quotes, the par-par basis;
    ``rub_ofz_kbd_v2`` annual yields agree with par to 0.5-2 %), moving the base
    with the basis held moves both curves, and moving the basis moves ``curve`` alone:

        ``IR|<base>|b  <- IR|<base>|b + IR|<curve>|b``,  ``IR|<basis>|b <- IR|<curve>|b``

    and the Hessian is ``J^T H J`` for that linear change of coordinates, so a Taylor
    expansion gives the same P&L in either view. ``IR|<curve>|*`` factors are renamed
    ``IR|<basis>|*`` in place; base factors a position lacks are added (zero before the
    change) ahead of them. A risk without ``curve`` factors is returned unchanged.
    """
    for label, name in (("base", base_curve_id), ("curve", curve_id), ("basis", basis_id)):
        _component(name, f"{label} curve id")
    if len({base_curve_id, curve_id, basis_id}) != 3:
        raise InvalidTerms("base, curve and basis must be three different curve ids")
    curve = {ir_factor(curve_id, b): b for b in RISK_BUCKETS}
    if not any(f in curve for f in risk.factors):
        return risk
    if any(f.startswith(f"IR|{basis_id}|") for f in risk.factors):
        raise InvalidTerms(f"the risk already carries {basis_id} factors")
    factors = list(risk.factors)
    missing = [
        ir_factor(base_curve_id, b)
        for b in RISK_BUCKETS
        if ir_factor(base_curve_id, b) not in factors
    ]
    if missing:
        at = min(i for i, f in enumerate(factors) if f in curve)
        factors[at:at] = missing
    renamed = tuple(ir_factor(basis_id, curve[f]) if f in curve else f for f in factors)
    index = {f: i for i, f in enumerate(factors)}
    size = len(factors)
    # x = J y: a base factor y_R moves x_R and x_K; a basis factor y_B moves x_K.
    columns: list[list[tuple[int, float]]] = []
    for f in factors:
        column = [(index[f], 1.0)]
        if f.startswith(f"IR|{base_curve_id}|"):
            twin = ir_factor(curve_id, f.rsplit("|", 1)[1])
            if twin in index:
                column.append((index[twin], 1.0))
        columns.append(column)
    grad = [0.0] * size
    for f, value in zip(risk.factors, risk.first_order, strict=True):
        grad[index[f]] = value
    first = tuple(math.fsum(grad[k] * w for k, w in col) for col in columns)
    hessian: tuple[HessianEntry, ...] = ()
    if risk.hessian:
        dense = [[0.0] * size for _ in range(size)]
        for e in risk.hessian:
            i, j = index[risk.factors[e.i]], index[risk.factors[e.j]]
            dense[i][j] = dense[j][i] = e.value
        hessian = _upper(
            tuple(
                tuple(
                    math.fsum(dense[k][m] * wk * wm for k, wk in columns[a] for m, wm in columns[b])
                    for b in range(size)
                )
                for a in range(size)
            )
        )
    return FactorRisk(renamed, first, hessian, risk.difference, risk.bump_bp, risk.has_second_order)
