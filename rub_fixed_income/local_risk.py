"""Pure local adapter for validated fixed RUB schedules and explicit curve factors.

No I/O or market selection: caller supplies source-validated terms and snapshots.
This boundary checks units, horizons and identities, not source authenticity.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Literal

from ._money import non_negative_money, positive_money
from ._risk_common import _curve_basis
from ._version import __version__
from .canonical import canonical_sha256
from .cashflows import BondCashflow, clean_price_pct_to_dirty_rub
from .conventions import ZSpreadConvention
from .discount import CurveBootstrapRequest, DiscountCurve
from .risk import (
    BondRiskEngine,
    BondRiskRequest,
    BondScenario,
    BondScenarioResult,
    ShockedCurveCache,
)

CurveRiskBasis = Literal["continuous_zero", "annual_zero", "ois_quote"]


@dataclass(frozen=True)
class RiskRunContext:
    run_id: str
    instrument_id: str
    market_snapshot_id: str
    schedule_snapshot_id: str
    curve_snapshot_id: str
    curve_basis: CurveRiskBasis

    def __post_init__(self) -> None:
        for name in (
            "run_id",
            "instrument_id",
            "market_snapshot_id",
            "schedule_snapshot_id",
            "curve_snapshot_id",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a nonempty string")
        if self.curve_basis not in ("continuous_zero", "annual_zero", "ois_quote"):
            raise ValueError("curve_basis must be continuous_zero, annual_zero or ois_quote")


@dataclass(frozen=True)
class FixedRubBondInput:
    """Explicit per-bond money and signed quantity; all flows already resolved.

    The caller must establish fixed coupons, RUB payments, calendar-adjusted
    dates and contractual redemption. Unknown/floating/FX-linked flows cannot
    be represented as fixed merely by supplying a projected amount.
    """

    context: RiskRunContext
    cashflows: tuple[BondCashflow, ...]
    settlement_date: date
    horizon_date: date
    horizon: Literal["maturity", "confirmed_investor_put"]
    cashflow_currency: Literal["RUB"]
    coupon_model: Literal["fixed"]
    current_nominal_rub: float
    clean_price_pct: float
    accrued_interest_rub: float
    quantity: float
    curve: DiscountCurve | CurveBootstrapRequest
    spread_convention: ZSpreadConvention
    spread_kind: Literal["credit", "ofz"]


@dataclass(frozen=True)
class LocalBondScenarioResult:
    calculation_id: str
    scenario_id: str
    context: RiskRunContext
    library_version: str
    result: BondScenarioResult


@dataclass(frozen=True)
class PreparedLocalBondRisk:
    context: RiskRunContext
    engine: BondRiskEngine
    horizon: str
    horizon_date: date
    current_nominal_rub: float
    clean_price_pct: float
    accrued_interest_rub: float
    calculation_id: str
    library_version: str = __version__

    def reprice(
        self, scenario: BondScenario, *, curve_basis: CurveRiskBasis
    ) -> LocalBondScenarioResult:
        """Reject a mismatched market-move basis before any repricing."""
        if curve_basis != self.context.curve_basis:
            raise ValueError("scenario curve_basis does not match the prepared calculation")
        result = self.engine.reprice(scenario)
        identity = canonical_sha256(
            {
                "calculation_id": self.calculation_id,
                "curve_basis": curve_basis,
                "shock_id": result.scenario_id,
            }
        )
        return LocalBondScenarioResult(
            self.calculation_id, identity, self.context, self.library_version, result
        )


def prepare_local_bond_risk(
    source: FixedRubBondInput, *, curve_cache: ShockedCurveCache | None = None
) -> PreparedLocalBondRisk:
    """Validate the boundary, convert clean % to dirty RUB, calibrate once.

    Pass one ``curve_cache`` to every position of a job to reuse shocked OIS curves.
    """
    if source.cashflow_currency != "RUB" or source.coupon_model != "fixed":
        raise ValueError("only validated fixed RUB cashflows are supported")
    if source.horizon not in ("maturity", "confirmed_investor_put"):
        raise ValueError("unsupported redemption horizon")
    if source.horizon_date <= source.settlement_date:
        raise ValueError("redemption horizon must follow settlement")
    flows = tuple(source.cashflows)
    if any(f.payment_date > source.horizon_date for f in flows):
        raise ValueError("cashflow falls after the redemption horizon")
    if not any(f.payment_date == source.horizon_date and f.amount > 0 for f in flows):
        raise ValueError("a positive payment at the redemption horizon is required")
    nominal = positive_money(source.current_nominal_rub, "current nominal")
    clean = positive_money(source.clean_price_pct, "clean price percent")
    accrued = non_negative_money(source.accrued_interest_rub, "accrued interest")
    expected_basis = _curve_basis(source.curve)
    if source.context.curve_basis != expected_basis:
        raise ValueError("curve input does not match the explicit curve_basis")
    engine = BondRiskEngine(
        BondRiskRequest(
            cashflows=flows,
            settlement_date=source.settlement_date,
            curve=source.curve,
            spread_convention=source.spread_convention,
            spread_kind=source.spread_kind,
            dirty_price_rub=clean_price_pct_to_dirty_rub(clean, nominal, accrued),
            quantity=source.quantity,
        ),
        ShockedCurveCache() if curve_cache is None else curve_cache,
    )
    identity = canonical_sha256(
        {
            "adapter_model": "local-fixed-rub-risk-v1",
            "library_version": __version__,
            "context": source.context.__dict__,
            "core_base_id": engine.base_id,
            "horizon": source.horizon,
            "horizon_date": source.horizon_date,
            "current_nominal_rub": nominal,
            "clean_price_pct": clean,
            "accrued_interest_rub": accrued,
        }
    )
    return PreparedLocalBondRisk(
        source.context,
        engine,
        source.horizon,
        source.horizon_date,
        nominal,
        clean,
        accrued,
        identity,
    )
