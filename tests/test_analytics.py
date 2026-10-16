from __future__ import annotations

import json
from dataclasses import asdict
from datetime import date

import pytest

from rub_fixed_income.analytics import (
    BondAnalyticsResultV1,
    BondTermsV1,
    CalculationFailure,
    CalculationPolicy,
    CurveSnapshotV1,
    MarketObservationV1,
    ValuationContext,
    accrued_interest_rub,
    analyze_bond,
    canonical_sha256,
    result_hash_projection,
    verify_result_sha256,
)
from rub_fixed_income.cashflows import BondCashflow
from rub_fixed_income.dates import year_fraction
from rub_fixed_income.discount import DiscountCurve

# These tests pin the behaviour of APIs deprecated in 0.12.0 (kept until 1.0);
# tests/test_deprecations.py checks that each one warns.
pytestmark = pytest.mark.filterwarnings("ignore:rub_fixed_income:DeprecationWarning")


def test_analyze_fixed_bullet_returns_ai_yield_durations_zspread_and_stable_hash() -> None:
    settlement = date(2026, 1, 1)
    terms = BondTermsV1(
        issue_id="RU000TEST001",
        current_nominal_rub=1000.0,
        maturity_date=date(2028, 1, 1),
        horizon="maturity",
        horizon_date=date(2028, 1, 1),
        accrual_period_start=date(2025, 10, 1),
        accrual_period_end=date(2026, 4, 1),
        coupon_amount_rub=50.0,
        terms_hash="terms-1",
    )
    schedule = (
        BondCashflow(date(2026, 4, 1), 50.0),
        BondCashflow(date(2026, 10, 1), 50.0),
        BondCashflow(date(2027, 4, 1), 50.0),
        BondCashflow(date(2027, 10, 1), 50.0),
        BondCashflow(date(2028, 1, 1), 1000.0),
    )
    market = MarketObservationV1(100.0, settlement, "midpoint", "market-1")
    curve = CurveSnapshotV1(
        "curve-world-1",
        DiscountCurve(settlement, ((date(2027, 1, 1), 1 / 1.08), (date(2028, 1, 1), 1 / 1.08**2))),
        "curve-1",
    )
    context = ValuationContext(settlement, settlement)

    first = analyze_bond(terms, schedule, market, curve, context, CalculationPolicy())
    second = analyze_bond(terms, schedule, market, curve, context, CalculationPolicy())

    assert isinstance(first, BondAnalyticsResultV1)
    assert isinstance(second, BondAnalyticsResultV1)
    assert first.accrued_interest_rub == 25.27
    assert first.dirty_price_rub_per_bond == pytest.approx(1025.27)
    assert first.yield_pct > 0
    assert first.macaulay_duration_years > first.modified_duration_years > 0
    assert abs(first.repricing_error_rub_per_bond) < 1e-8
    assert first.result_sha256 == second.result_sha256
    assert len(first.result_sha256) == 64

    serialized = json.loads(json.dumps(asdict(first), default=str))
    assert result_hash_projection(serialized)["result_sha256"] == ""
    assert verify_result_sha256(serialized)
    serialized["yield_pct"] += 0.01
    assert not verify_result_sha256(serialized)

    incomplete = {"result_sha256": ""}
    incomplete["result_sha256"] = canonical_sha256(result_hash_projection(incomplete))
    assert not verify_result_sha256(incomplete)


def test_confirmed_put_uses_explicit_put_horizon_and_ignores_later_cashflows() -> None:
    settlement = date(2026, 1, 1)
    put_date = date(2027, 1, 1)
    terms = BondTermsV1(
        issue_id="RU000TESTPUT",
        current_nominal_rub=1000.0,
        maturity_date=date(2030, 1, 1),
        horizon="confirmed_investor_put",
        horizon_date=put_date,
        accrual_period_start=date(2026, 1, 1),
        accrual_period_end=date(2026, 7, 1),
        coupon_amount_rub=40.0,
        terms_hash="terms-put",
    )
    schedule = (
        BondCashflow(date(2026, 7, 1), 40.0),
        BondCashflow(put_date, 1040.0),
        BondCashflow(date(2028, 1, 1), 1040.0),
    )
    curve = CurveSnapshotV1(
        "curve-put",
        DiscountCurve(settlement, ((put_date, 1 / 1.08),)),
        "curve-put-hash",
    )

    result = analyze_bond(
        terms,
        schedule,
        MarketObservationV1(100.0, settlement, "midpoint", "market-put"),
        curve,
        ValuationContext(settlement, settlement),
        CalculationPolicy(),
    )

    assert isinstance(result, BondAnalyticsResultV1)
    assert result.horizon == "confirmed_investor_put"
    assert result.yield_pct == pytest.approx(8.16, abs=0.1)


def test_z_spread_uses_settlement_forward_curve_discount_factors() -> None:
    valuation = date(2026, 1, 1)
    settlement = date(2026, 7, 1)
    payment = date(2027, 7, 1)
    curve = DiscountCurve(
        valuation,
        (
            (settlement, 1 / 1.10 ** year_fraction(valuation, settlement)),
            (payment, 1 / 1.10 ** year_fraction(valuation, payment)),
        ),
    )
    forward_dirty = 1000.0 * curve.discount_factor(payment) / curve.discount_factor(settlement)
    terms = BondTermsV1(
        "RU000FORWARD",
        1000.0,
        payment,
        "maturity",
        payment,
        settlement,
        payment,
        0.0,
        "terms-forward",
    )

    result = analyze_bond(
        terms,
        (BondCashflow(payment, 1000.0),),
        MarketObservationV1(forward_dirty / 10.0, settlement, "midpoint", "market-forward"),
        CurveSnapshotV1("curve-forward", curve, "curve-hash"),
        ValuationContext(valuation, settlement),
        CalculationPolicy(),
    )

    assert isinstance(result, BondAnalyticsResultV1)
    assert result.z_spread_bp == pytest.approx(0.0, abs=1e-8)


@pytest.mark.parametrize(
    "terms",
    [
        BondTermsV1(
            "x",
            0.0,
            date(2028, 1, 1),
            "maturity",
            date(2028, 1, 1),
            date(2026, 1, 1),
            date(2026, 7, 1),
            40.0,
            "t",
        ),
        BondTermsV1(
            "x",
            1000.0,
            date(2028, 1, 1),
            "maturity",
            date(2027, 1, 1),
            date(2026, 1, 1),
            date(2026, 7, 1),
            40.0,
            "t",
        ),
        BondTermsV1(
            "x",
            1000.0,
            date(2028, 1, 1),
            "confirmed_investor_put",
            date(2028, 1, 1),
            date(2026, 1, 1),
            date(2026, 7, 1),
            40.0,
            "t",
        ),
    ],
)
def test_terms_reject_invalid_units_and_horizon_semantics(terms: BondTermsV1) -> None:
    with pytest.raises(ValueError):
        terms.validate()


def test_analyze_rejects_unordered_duplicate_or_nonpositive_cashflows() -> None:
    settlement = date(2026, 1, 1)
    terms = BondTermsV1(
        "x",
        1000.0,
        date(2027, 1, 1),
        "maturity",
        date(2027, 1, 1),
        settlement,
        date(2026, 7, 1),
        40.0,
        "t",
    )
    curve = CurveSnapshotV1("cw", DiscountCurve(settlement, ((date(2027, 1, 1), 0.9),)), "c")
    market = MarketObservationV1(100.0, settlement, "midpoint", "m")
    schedules = (
        (BondCashflow(date(2027, 1, 1), 1000.0), BondCashflow(date(2026, 7, 1), 40.0)),
        (BondCashflow(date(2026, 7, 1), 40.0), BondCashflow(date(2026, 7, 1), 100.0)),
        (BondCashflow(date(2026, 7, 1), -40.0), BondCashflow(date(2027, 1, 1), 1000.0)),
    )

    for schedule in schedules:
        result = analyze_bond(
            terms,
            schedule,
            market,
            curve,
            ValuationContext(settlement, settlement),
            CalculationPolicy(),
        )
        assert isinstance(result, CalculationFailure)
        assert result.code in {"CASHFLOW_INCOMPLETE", "INVALID_UNITS"}


def test_leap_year_accrual_rounds_half_up_at_contractual_boundary() -> None:
    terms = BondTermsV1(
        "leap",
        1000.0,
        date(2029, 1, 1),
        "maturity",
        date(2029, 1, 1),
        date(2028, 1, 31),
        date(2028, 7, 31),
        50.0,
        "t",
    )

    assert accrued_interest_rub(terms, date(2028, 2, 29)) == pytest.approx(50.0 * 29 / 182)


def test_analyze_requires_terminal_cashflow_on_explicit_horizon() -> None:
    settlement = date(2026, 1, 1)
    horizon = date(2027, 1, 1)
    terms = BondTermsV1(
        "x", 1000.0, horizon, "maturity", horizon, settlement, date(2026, 7, 1), 40.0, "t"
    )
    result = analyze_bond(
        terms,
        (BondCashflow(date(2026, 7, 1), 40.0),),
        MarketObservationV1(100.0, settlement, "midpoint", "m"),
        CurveSnapshotV1("cw", DiscountCurve(settlement, ((horizon, 0.9),)), "c"),
        ValuationContext(settlement, settlement),
        CalculationPolicy(),
    )

    assert result == CalculationFailure(
        "CASHFLOW_INCOMPLETE", "terminal horizon cash flow is required"
    )
