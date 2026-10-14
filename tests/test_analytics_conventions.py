"""F4 accrued interest on coupon date and F2 explicit analytics spread convention (V2)."""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import date

import pytest

from rub_fixed_income import (
    CONTINUOUS,
    MONTHLY,
    BondAnalyticsResultV1,
    BondAnalyticsResultV2,
    BondCashflow,
    BondTermsV1,
    CalculationFailure,
    CalculationPolicy,
    CurveSnapshotV1,
    DiscountCurve,
    MarketObservationV1,
    ValuationContext,
    ZSpreadConvention,
    analyze_bond,
    analyze_bond_v2,
    result_hash_projection,
    solve_z_spread,
    verify_result_sha256,
)
from rub_fixed_income.analytics import accrued_interest_rub

VALUATION = date(2026, 1, 1)
SETTLE = date(2026, 1, 2)
TERMS = BondTermsV1(
    "RU000V2",
    1000.0,
    date(2028, 1, 1),
    "maturity",
    date(2028, 1, 1),
    date(2025, 10, 1),
    date(2026, 4, 1),
    50.0,
    "terms",
)
SCHEDULE = (
    BondCashflow(date(2026, 4, 1), 50.0),
    BondCashflow(date(2026, 10, 1), 50.0),
    BondCashflow(date(2027, 4, 1), 50.0),
    BondCashflow(date(2027, 10, 1), 50.0),
    BondCashflow(date(2028, 1, 1), 1000.0),
)
CURVE = CurveSnapshotV1(
    "cw",
    DiscountCurve(VALUATION, ((date(2027, 1, 1), 1 / 1.08), (date(2028, 1, 1), 1 / 1.08**2))),
    "c",
)
MARKET = MarketObservationV1(97.0, SETTLE, "midpoint", "m")
CONTEXT = ValuationContext(VALUATION, SETTLE)


def test_accrued_interest_is_zero_on_coupon_payment_date() -> None:
    assert accrued_interest_rub(TERMS, TERMS.accrual_period_end) == 0.0
    assert accrued_interest_rub(TERMS, TERMS.accrual_period_start) == 0.0
    assert accrued_interest_rub(TERMS, date(2026, 3, 31)) == pytest.approx(50.0 * 181 / 182)


def test_analyze_on_coupon_date_excludes_coupon_and_accrual() -> None:
    coupon_date = date(2026, 4, 1)
    result = analyze_bond(
        TERMS,
        SCHEDULE,
        MarketObservationV1(97.0, coupon_date, "midpoint", "m"),
        CurveSnapshotV1(
            "cw",
            DiscountCurve(coupon_date, ((date(2027, 1, 1), 0.93), (date(2028, 1, 1), 0.86))),
            "c",
        ),
        ValuationContext(coupon_date, coupon_date),
        CalculationPolicy(),
    )
    assert isinstance(result, BondAnalyticsResultV1)
    assert result.accrued_interest_rub == 0.0
    assert result.dirty_price_rub_per_bond == pytest.approx(970.0)


def test_v1_output_is_still_continuous_and_v2_reports_convention() -> None:
    v1 = analyze_bond(TERMS, SCHEDULE, MARKET, CURVE, CONTEXT, CalculationPolicy())
    v2 = analyze_bond_v2(
        TERMS,
        SCHEDULE,
        MARKET,
        CURVE,
        CONTEXT,
        CalculationPolicy(),
        z_spread_convention=ZSpreadConvention(compounding=CONTINUOUS),
    )
    assert isinstance(v1, BondAnalyticsResultV1)
    assert isinstance(v2, BondAnalyticsResultV2)
    assert v2.z_spread_bp == pytest.approx(v1.z_spread_bp, abs=1e-9)
    assert v2.z_spread_convention == "zspread:continuous:ACT/365F"
    assert v2.yield_convention == "yield:annual:ACT/365F"
    assert v2.yield_pct == pytest.approx(v1.yield_pct, abs=1e-9)


@pytest.mark.parametrize(
    "convention", [ZSpreadConvention(), ZSpreadConvention(compounding=MONTHLY)]
)
def test_v2_default_annual_and_monthly_match_settlement_aligned_solver(
    convention: ZSpreadConvention,
) -> None:
    v2 = analyze_bond_v2(
        TERMS,
        SCHEDULE,
        MARKET,
        CURVE,
        CONTEXT,
        CalculationPolicy(),
        z_spread_convention=convention,
    )
    assert isinstance(v2, BondAnalyticsResultV2)
    future = tuple((c.payment_date, c.amount) for c in SCHEDULE if c.payment_date > SETTLE)
    expected = solve_z_spread(
        future,
        v2.dirty_price_rub_per_bond,
        CURVE.discount_curve,
        settlement_date=SETTLE,
        convention=convention,
    )
    assert v2.z_spread_bp == pytest.approx(expected * 10_000.0, abs=1e-8)
    assert v2.z_spread_convention == convention.identifier


def test_v2_default_convention_is_annual() -> None:
    v2 = analyze_bond_v2(TERMS, SCHEDULE, MARKET, CURVE, CONTEXT, CalculationPolicy())
    assert isinstance(v2, BondAnalyticsResultV2)
    assert v2.z_spread_convention == "zspread:annual:ACT/365F"


def test_v2_hash_is_self_verifiable_and_distinct_from_v1_field_set() -> None:
    v2 = analyze_bond_v2(TERMS, SCHEDULE, MARKET, CURVE, CONTEXT, CalculationPolicy())
    assert isinstance(v2, BondAnalyticsResultV2)
    assert verify_result_sha256(v2)
    serialized = json.loads(json.dumps(asdict(v2), default=str))
    assert verify_result_sha256(serialized)
    assert result_hash_projection(serialized)["result_sha256"] == ""
    serialized["z_spread_convention"] = "zspread:continuous:ACT/365F"
    assert not verify_result_sha256(serialized)
    del serialized["z_spread_convention"]
    assert not verify_result_sha256(serialized)


@pytest.mark.parametrize(
    "policy",
    [
        CalculationPolicy(spread_lower_bp=float("nan")),
        CalculationPolicy(spread_lower_bp=100.0, spread_upper_bp=-100.0),
        CalculationPolicy(root_tolerance=0.0),
    ],
)
def test_analyze_rejects_invalid_policy(policy: CalculationPolicy) -> None:
    result = analyze_bond(TERMS, SCHEDULE, MARKET, CURVE, CONTEXT, policy)
    assert isinstance(result, CalculationFailure)
    assert result.code == "INVALID_UNITS"
