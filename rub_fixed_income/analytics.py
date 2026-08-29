from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, replace
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from itertools import pairwise
from typing import Any, Literal, cast

from .bond import YieldRequest, solve_effective_annual_yield
from .cashflows import BondCashflow, clean_price_pct_to_dirty_rub
from .dates import year_fraction
from .discount import DiscountCurve


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


_RESULT_FIELDS = frozenset(BondAnalyticsResultV1.__dataclass_fields__)


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
    result: BondAnalyticsResultV1 | Mapping[str, object],
) -> dict[str, object]:
    """Return the canonical public projection whose SHA-256 identifies a result.

    The projection includes every result field and fixes ``result_sha256`` to the empty
    string, allowing the hash to be verified after JSON serialization and deserialization.
    """
    raw = asdict(result) if isinstance(result, BondAnalyticsResultV1) else dict(result)
    if "result_sha256" not in raw:
        raise ValueError("result_sha256 field is required")
    raw["result_sha256"] = ""
    return raw


def verify_result_sha256(result: BondAnalyticsResultV1 | Mapping[str, object]) -> bool:
    """Verify a result's declared SHA-256 against its canonical public projection."""
    raw = asdict(result) if isinstance(result, BondAnalyticsResultV1) else dict(result)
    if raw.keys() != _RESULT_FIELDS:
        return False
    declared = raw.get("result_sha256")
    return isinstance(declared, str) and declared == canonical_sha256(result_hash_projection(raw))


def _bisect(
    function: Callable[[float], float], lower: float, upper: float, tolerance: float
) -> tuple[float, int]:
    low_value = function(lower)
    high_value = function(upper)
    if low_value * high_value > 0.0:
        raise ValueError("root is not bracketed")
    for iteration in range(1, 241):
        middle = (lower + upper) / 2.0
        value = function(middle)
        if abs(value) <= tolerance or upper - lower <= tolerance:
            return middle, iteration
        if low_value * value <= 0.0:
            upper = middle
        else:
            lower, low_value = middle, value
    raise RuntimeError("root solver did not converge")


def accrued_interest_rub(terms: BondTermsV1, settlement_date: date) -> float:
    elapsed = (settlement_date - terms.accrual_period_start).days
    period = (terms.accrual_period_end - terms.accrual_period_start).days
    if period <= 0 or not 0 <= elapsed <= period:
        raise ValueError("settlement must lie inside the accrual period")
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


def _z_spread(
    cashflows: tuple[BondCashflow, ...],
    curve: DiscountCurve,
    settlement_date: date,
    dirty_price: float,
    policy: CalculationPolicy,
) -> float:
    def error(spread_bp: float) -> float:
        spread = spread_bp / 10_000.0
        settlement_df = curve.discount_factor(settlement_date)
        return (
            sum(
                cashflow.amount
                * curve.discount_factor(cashflow.payment_date)
                / settlement_df
                * math.exp(-spread * year_fraction(settlement_date, cashflow.payment_date))
                for cashflow in cashflows
            )
            - dirty_price
        )

    spread, _ = _bisect(
        error, policy.spread_lower_bp, policy.spread_upper_bp, policy.root_tolerance
    )
    return spread


def analyze_bond(
    terms_snapshot: BondTermsV1,
    cashflow_schedule: tuple[BondCashflow, ...],
    market_observation: MarketObservationV1,
    curve_snapshot: CurveSnapshotV1,
    valuation_context: ValuationContext,
    calculation_policy: CalculationPolicy,
) -> BondAnalyticsResultV1 | CalculationFailure:
    try:
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
            return CalculationFailure(
                "CASHFLOW_INCOMPLETE", "cash flows must be ordered and unique"
            )
        if terms_snapshot.horizon == "maturity" and any(
            cashflow.payment_date > terms_snapshot.horizon_date for cashflow in cashflow_schedule
        ):
            return CalculationFailure("CASHFLOW_INCOMPLETE", "cash flows cannot exceed horizon")
        future = tuple(
            cashflow
            for cashflow in cashflow_schedule
            if valuation_context.settlement_date
            < cashflow.payment_date
            <= terms_snapshot.horizon_date
        )
        if not future:
            return CalculationFailure("CASHFLOW_INCOMPLETE", "future cash flows are required")
        if future[-1].payment_date != terms_snapshot.horizon_date:
            return CalculationFailure(
                "CASHFLOW_INCOMPLETE", "terminal horizon cash flow is required"
            )
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
        solved = solve_effective_annual_yield(
            YieldRequest(valuation_context.settlement_date, dirty, future)
        )
        macaulay, modified = _durations(
            future, valuation_context.settlement_date, solved.effective_annual_yield, dirty
        )
        spread = _z_spread(
            future,
            curve_snapshot.discount_curve,
            valuation_context.settlement_date,
            dirty,
            calculation_policy,
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
    except ValueError as exc:
        message = str(exc)
        code = "SOLVER_NOT_BRACKETED" if "bracket" in message else "INVALID_UNITS"
        return CalculationFailure(code, message)
    except (OverflowError, RuntimeError) as exc:
        code = "NON_FINITE_RESULT" if isinstance(exc, OverflowError) else "SOLVER_NON_CONVERGENCE"
        return CalculationFailure(code, str(exc))
