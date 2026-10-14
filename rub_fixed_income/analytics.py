from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from itertools import pairwise
from typing import Any, Literal, cast

from ._solver import bisect
from .bond import YieldRequest, solve_effective_annual_yield
from .cashflows import BondCashflow, clean_price_pct_to_dirty_rub
from .compatibility import _spread_price, bond_duration, solve_nominal_annual_yield
from .conventions import CONTINUOUS, YieldConvention, ZSpreadConvention
from .dates import year_fraction
from .discount import DiscountCurve

# analyze_bond (V1) always reports a continuous ACT/365F Z-spread; this is frozen for
# hash/reproducibility compatibility. analyze_bond_v2 makes the convention an input.
V1_Z_SPREAD_CONVENTION = ZSpreadConvention(compounding=CONTINUOUS)
V1_YIELD_CONVENTION = YieldConvention()


@dataclass(frozen=True)
class BondTermsV1:
    issue_id: str
    current_nominal_rub: float
    maturity_date: date
    horizon: Literal["maturity", "confirmed_investor_put"]
    horizon_date: date
    accrual_period_start: date
    accrual_period_end: date
    coupon_amount_rub: float
    terms_hash: str

    def validate(self) -> None:
        if (
            not math.isfinite(self.current_nominal_rub)
            or not math.isfinite(self.coupon_amount_rub)
            or self.current_nominal_rub <= 0.0
            or self.coupon_amount_rub < 0.0
        ):
            raise ValueError("nominal and coupon must have valid finite units")
        if self.accrual_period_end <= self.accrual_period_start:
            raise ValueError("accrual period must be positive")
        if self.horizon == "maturity" and self.horizon_date != self.maturity_date:
            raise ValueError("maturity horizon date must equal maturity date")
        if self.horizon == "confirmed_investor_put" and not self.horizon_date < self.maturity_date:
            raise ValueError("confirmed put horizon must precede maturity")


@dataclass(frozen=True)
class MarketObservationV1:
    clean_price_pct_of_current_nominal: float
    settlement_date: date
    selected_price_basis: str
    market_hash: str

    def validate(self) -> None:
        price = self.clean_price_pct_of_current_nominal
        if not math.isfinite(price) or price <= 0.0:
            raise ValueError("clean price must be positive and finite")


@dataclass(frozen=True)
class CurveSnapshotV1:
    curve_world_instance_id: str
    discount_curve: DiscountCurve
    curve_hash: str


@dataclass(frozen=True)
class ValuationContext:
    valuation_date: date
    settlement_date: date


@dataclass(frozen=True)
class CalculationPolicy:
    money_quantum: Decimal = Decimal("0.01")
    spread_lower_bp: float = -5000.0
    spread_upper_bp: float = 5000.0
    root_tolerance: float = 1e-13

    def validate(self) -> None:
        bounds = (self.spread_lower_bp, self.spread_upper_bp, self.root_tolerance)
        if not all(math.isfinite(value) for value in bounds):
            raise ValueError("calculation policy values must be finite")
        if not self.spread_lower_bp < self.spread_upper_bp:
            raise ValueError("calculation policy spread bounds must be ordered")
        if self.root_tolerance <= 0.0:
            raise ValueError("calculation policy root tolerance must be positive")
        if not self.money_quantum.is_finite() or self.money_quantum <= 0:
            raise ValueError("calculation policy money quantum must be positive")


@dataclass(frozen=True)
class CalculationFailure:
    code: str
    message: str


@dataclass(frozen=True)
class BondAnalyticsResultV1:
    issue_id: str
    terms_hash: str
    market_hash: str
    curve_hash: str
    curve_world_instance_id: str
    settlement_date: date
    horizon: str
    selected_price_basis: str
    clean_price_pct_of_current_nominal: float
    accrued_interest_rub: float
    dirty_price_rub_per_bond: float
    yield_pct: float
    macaulay_duration_years: float
    modified_duration_years: float
    z_spread_bp: float
    repricing_error_rub_per_bond: float
    result_sha256: str = ""


@dataclass(frozen=True)
class BondAnalyticsResultV2:
    """V1 fields plus explicit convention identifiers; produced by ``analyze_bond_v2``."""

    issue_id: str
    terms_hash: str
    market_hash: str
    curve_hash: str
    curve_world_instance_id: str
    settlement_date: date
    horizon: str
    selected_price_basis: str
    clean_price_pct_of_current_nominal: float
    accrued_interest_rub: float
    dirty_price_rub_per_bond: float
    yield_pct: float
    macaulay_duration_years: float
    modified_duration_years: float
    z_spread_bp: float
    repricing_error_rub_per_bond: float
    yield_convention: str
    z_spread_convention: str
    result_sha256: str = ""


_RESULT_FIELDS = frozenset(BondAnalyticsResultV1.__dataclass_fields__)
_RESULT_FIELDS_V2 = frozenset(BondAnalyticsResultV2.__dataclass_fields__)
_AnyResult = BondAnalyticsResultV1 | BondAnalyticsResultV2


def _canonical_value(value: object) -> object:
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("canonical JSON forbids non-finite numbers")
        return value
    if isinstance(value, dict):
        return {key: _canonical_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item) for item in value]
    return value


def canonical_json_bytes(value: object) -> bytes:
    raw = asdict(cast(Any, value)) if hasattr(value, "__dataclass_fields__") else value
    return json.dumps(
        _canonical_value(raw), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def canonical_sha256(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def result_hash_projection(
    result: BondAnalyticsResultV1 | BondAnalyticsResultV2 | Mapping[str, object],
) -> dict[str, object]:
    """Return the canonical public projection whose SHA-256 identifies a result.

    The projection includes every result field and fixes ``result_sha256`` to the empty
    string, allowing the hash to be verified after JSON serialization and deserialization.
    """
    raw = asdict(result) if isinstance(result, _AnyResult) else dict(result)
    if "result_sha256" not in raw:
        raise ValueError("result_sha256 field is required")
    raw["result_sha256"] = ""
    return raw


def verify_result_sha256(
    result: BondAnalyticsResultV1 | BondAnalyticsResultV2 | Mapping[str, object],
) -> bool:
    """Verify a result's declared SHA-256 against its canonical public projection.

    The field set must be exactly the V1 or exactly the V2 schema.
    """
    raw = asdict(result) if isinstance(result, _AnyResult) else dict(result)
    if raw.keys() != _RESULT_FIELDS and raw.keys() != _RESULT_FIELDS_V2:
        return False
    declared = raw.get("result_sha256")
    return isinstance(declared, str) and declared == canonical_sha256(result_hash_projection(raw))


def accrued_interest_rub(terms: BondTermsV1, settlement_date: date) -> float:
    """Linear accrued interest in RUB per bond: coupon * elapsed_days / period_days.

    Settlement on the coupon payment date (``accrual_period_end``) returns 0.0: the buyer
    does not receive that coupon (MOEX convention), consistent with future flows being
    strictly after settlement.
    """
    elapsed = (settlement_date - terms.accrual_period_start).days
    period = (terms.accrual_period_end - terms.accrual_period_start).days
    if period <= 0 or not 0 <= elapsed <= period:
        raise ValueError("settlement must lie inside the accrual period")
    if elapsed == period:
        return 0.0
    return float(terms.coupon_amount_rub * elapsed / period)


def _price_at_yield(cashflows: tuple[BondCashflow, ...], settlement: date, rate: float) -> float:
    return float(
        sum(
            cashflow.amount / (1.0 + rate) ** year_fraction(settlement, cashflow.payment_date)
            for cashflow in cashflows
        )
    )


def _durations(
    cashflows: tuple[BondCashflow, ...], settlement: date, rate: float, dirty_price: float
) -> tuple[float, float]:
    weighted = sum(
        year_fraction(settlement, cashflow.payment_date)
        * cashflow.amount
        / (1.0 + rate) ** year_fraction(settlement, cashflow.payment_date)
        for cashflow in cashflows
    )
    macaulay = weighted / dirty_price
    return macaulay, macaulay / (1.0 + rate)


def _z_spread_bp(
    cashflows: tuple[BondCashflow, ...],
    curve: DiscountCurve,
    settlement_date: date,
    dirty_price: float,
    policy: CalculationPolicy,
    convention: ZSpreadConvention,
) -> float:
    flows = tuple((cashflow.payment_date, cashflow.amount) for cashflow in cashflows)

    def error(spread_bp: float) -> float:
        return (
            _spread_price(
                flows,
                curve,
                settlement_date,
                spread_bp / 10_000.0,
                convention,
                include_settlement_flows=False,
            )
            - dirty_price
        )

    return bisect(
        error,
        policy.spread_lower_bp,
        policy.spread_upper_bp,
        tolerance=policy.root_tolerance,
        x_tolerance=policy.root_tolerance,
        label="Z-spread",
    ).root


@dataclass(frozen=True)
class _Prepared:
    future: tuple[BondCashflow, ...]
    accrued_interest: float
    dirty_price: float


def _prepare(
    terms_snapshot: BondTermsV1,
    cashflow_schedule: tuple[BondCashflow, ...],
    market_observation: MarketObservationV1,
    curve_snapshot: CurveSnapshotV1,
    valuation_context: ValuationContext,
    calculation_policy: CalculationPolicy,
) -> _Prepared | CalculationFailure:
    calculation_policy.validate()
    terms_snapshot.validate()
    market_observation.validate()
    if market_observation.settlement_date != valuation_context.settlement_date:
        return CalculationFailure("QUOTE_CURVE_DATE_MISMATCH", "settlement dates differ")
    if curve_snapshot.discount_curve.curve_date != valuation_context.valuation_date:
        return CalculationFailure("QUOTE_CURVE_DATE_MISMATCH", "curve valuation date differs")
    if any(
        not math.isfinite(cashflow.amount) or cashflow.amount <= 0.0
        for cashflow in cashflow_schedule
    ):
        return CalculationFailure("INVALID_UNITS", "cash flows must be positive and finite")
    dates = tuple(cashflow.payment_date for cashflow in cashflow_schedule)
    if any(left >= right for left, right in pairwise(dates)):
        return CalculationFailure("CASHFLOW_INCOMPLETE", "cash flows must be ordered and unique")
    if terms_snapshot.horizon == "maturity" and any(
        cashflow.payment_date > terms_snapshot.horizon_date for cashflow in cashflow_schedule
    ):
        return CalculationFailure("CASHFLOW_INCOMPLETE", "cash flows cannot exceed horizon")
    future = tuple(
        cashflow
        for cashflow in cashflow_schedule
        if valuation_context.settlement_date < cashflow.payment_date <= terms_snapshot.horizon_date
    )
    if not future:
        return CalculationFailure("CASHFLOW_INCOMPLETE", "future cash flows are required")
    if future[-1].payment_date != terms_snapshot.horizon_date:
        return CalculationFailure("CASHFLOW_INCOMPLETE", "terminal horizon cash flow is required")
    ai = float(
        Decimal(
            str(accrued_interest_rub(terms_snapshot, valuation_context.settlement_date))
        ).quantize(calculation_policy.money_quantum, ROUND_HALF_UP)
    )
    dirty = clean_price_pct_to_dirty_rub(
        market_observation.clean_price_pct_of_current_nominal,
        terms_snapshot.current_nominal_rub,
        ai,
    )
    return _Prepared(future, ai, dirty)


def _failure_from(exc: Exception) -> CalculationFailure:
    if isinstance(exc, ValueError):
        message = str(exc)
        code = "SOLVER_NOT_BRACKETED" if "bracket" in message else "INVALID_UNITS"
        return CalculationFailure(code, message)
    code = "NON_FINITE_RESULT" if isinstance(exc, OverflowError) else "SOLVER_NON_CONVERGENCE"
    return CalculationFailure(code, str(exc))


def analyze_bond(
    terms_snapshot: BondTermsV1,
    cashflow_schedule: tuple[BondCashflow, ...],
    market_observation: MarketObservationV1,
    curve_snapshot: CurveSnapshotV1,
    valuation_context: ValuationContext,
    calculation_policy: CalculationPolicy,
) -> BondAnalyticsResultV1 | CalculationFailure:
    """V1 analytics: effective annual ACT/365F yield and a *continuous* ACT/365F Z-spread.

    The V1 conventions are frozen (``V1_YIELD_CONVENTION``, ``V1_Z_SPREAD_CONVENTION``) so
    stored V1 result hashes stay reproducible. Use :func:`analyze_bond_v2` to choose the
    Z-spread convention (desk default annual) and to record it in the result.
    """
    try:
        prepared = _prepare(
            terms_snapshot,
            cashflow_schedule,
            market_observation,
            curve_snapshot,
            valuation_context,
            calculation_policy,
        )
        if isinstance(prepared, CalculationFailure):
            return prepared
        future, ai, dirty = prepared.future, prepared.accrued_interest, prepared.dirty_price
        solved = solve_effective_annual_yield(
            YieldRequest(valuation_context.settlement_date, dirty, future)
        )
        macaulay, modified = _durations(
            future, valuation_context.settlement_date, solved.effective_annual_yield, dirty
        )
        spread = _z_spread_bp(
            future,
            curve_snapshot.discount_curve,
            valuation_context.settlement_date,
            dirty,
            calculation_policy,
            V1_Z_SPREAD_CONVENTION,
        )
        result = BondAnalyticsResultV1(
            terms_snapshot.issue_id,
            terms_snapshot.terms_hash,
            market_observation.market_hash,
            curve_snapshot.curve_hash,
            curve_snapshot.curve_world_instance_id,
            valuation_context.settlement_date,
            terms_snapshot.horizon,
            market_observation.selected_price_basis,
            market_observation.clean_price_pct_of_current_nominal,
            ai,
            dirty,
            solved.effective_annual_yield * 100.0,
            macaulay,
            modified,
            spread,
            solved.repricing_error_rub_per_bond,
        )
        return replace(result, result_sha256=canonical_sha256(result_hash_projection(result)))
    except (ValueError, OverflowError, RuntimeError) as exc:
        return _failure_from(exc)


def analyze_bond_v2(
    terms_snapshot: BondTermsV1,
    cashflow_schedule: tuple[BondCashflow, ...],
    market_observation: MarketObservationV1,
    curve_snapshot: CurveSnapshotV1,
    valuation_context: ValuationContext,
    calculation_policy: CalculationPolicy,
    *,
    yield_convention: YieldConvention | None = None,
    z_spread_convention: ZSpreadConvention | None = None,
) -> BondAnalyticsResultV2 | CalculationFailure:
    """Analytics with explicit conventions recorded in the result.

    Defaults: yield annual ACT/365F (same economics as V1), Z-spread **annual** ACT/365F
    over the settlement-forward curve (desk standard). Pass
    ``ZSpreadConvention(compounding=CONTINUOUS)`` to reproduce the V1 spread.
    """
    yield_conv = YieldConvention() if yield_convention is None else yield_convention
    spread_conv = ZSpreadConvention() if z_spread_convention is None else z_spread_convention
    try:
        prepared = _prepare(
            terms_snapshot,
            cashflow_schedule,
            market_observation,
            curve_snapshot,
            valuation_context,
            calculation_policy,
        )
        if isinstance(prepared, CalculationFailure):
            return prepared
        future, ai, dirty = prepared.future, prepared.accrued_interest, prepared.dirty_price
        settlement = valuation_context.settlement_date
        flows = tuple((cashflow.payment_date, cashflow.amount) for cashflow in future)
        annual_yield = solve_nominal_annual_yield(
            dirty_price=dirty, settlement_date=settlement, cashflows=flows, convention=yield_conv
        )
        duration = bond_duration(
            annual_yield=annual_yield,
            settlement_date=settlement,
            cashflows=flows,
            convention=yield_conv,
        )
        spread = _z_spread_bp(
            future,
            curve_snapshot.discount_curve,
            settlement,
            dirty,
            calculation_policy,
            spread_conv,
        )
        result = BondAnalyticsResultV2(
            terms_snapshot.issue_id,
            terms_snapshot.terms_hash,
            market_observation.market_hash,
            curve_snapshot.curve_hash,
            curve_snapshot.curve_world_instance_id,
            settlement,
            terms_snapshot.horizon,
            market_observation.selected_price_basis,
            market_observation.clean_price_pct_of_current_nominal,
            ai,
            dirty,
            annual_yield * 100.0,
            duration.macaulay_years,
            duration.modified_years,
            spread,
            duration.present_value - dirty,
            yield_conv.identifier,
            spread_conv.identifier,
        )
        return replace(result, result_sha256=canonical_sha256(result_hash_projection(result)))
    except (ValueError, OverflowError, RuntimeError) as exc:
        return _failure_from(exc)
