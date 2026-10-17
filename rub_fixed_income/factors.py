"""Risk in the desk factor vocabulary (roadmap §2, item 4).

Factor keys:

- ``IR|<curve_id>|<bucket>``: a 1 bp shift of one of the 11 bucket pillars of a curve;
- ``CS``: a 1 bp move of the position's own spread (credit or OFZ spread);
- ``FX|<ccy>``: a 1 % move of the currency against RUB;
- ``FXBASIS|<ccy>``: a 1 bp move of a calibrated FX-futures basis.

A non-RUB bond's risk is in RUB at the snapshot FX rate; ``FX|<ccy>`` and its cross terms
with the rates and spread factors come from :func:`fx_translated_factor_risk`.

``first_order`` is RUB per factor unit. ``hessian`` holds the sparse upper triangle
``(i, j, d2PV)`` with ``i <= j`` over ``factors`` (RUB per unit squared); exact zeros
are omitted. First order and Hessian both come from central differences with the same
bump (DEC08), so a Taylor expansion of the two is consistent.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .errors import InvalidTerms
from .fx_futures import FxFuturesRiskResult, FxFuturesSecondOrderRisk
from .risk import RISK_BUCKETS, BondRiskResult, BondSecondOrderRisk

__all__ = [
    "CS_FACTOR",
    "FactorRisk",
    "HessianEntry",
    "bond_factor_risk",
    "fx_basis_factor",
    "fx_factor",
    "fx_futures_factor_risk",
    "fx_translated_factor_risk",
    "ir_factor",
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
    risk: BondRiskResult, curve_id: str, second_order: BondSecondOrderRisk | None = None
) -> FactorRisk:
    """Map a bond's central risk (and optionally its Hessian) to desk factors.

    With ``second_order`` the first order is its central gradient, which uses the same
    bump as the Hessian. Otherwise ``risk`` must itself be central.
    """
    factors = (*(ir_factor(curve_id, b) for b in RISK_BUCKETS), CS_FACTOR)
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
    first = (*(b.dv01_rub for b in risk.buckets), spread)
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
