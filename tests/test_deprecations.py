"""APIs deprecated in 0.12.0 warn at the caller and keep their behaviour (roadmap 0.9)."""

from __future__ import annotations

import warnings
from collections.abc import Callable
from dataclasses import replace
from datetime import date

import pytest
from test_hash_stability import _bond_inputs
from test_local_worker import snapshot
from test_public_schedule import payload

from rub_fixed_income import (
    CONTINUOUS,
    DiscountCurve,
    ZSpreadConvention,
    analyze_bond,
    analyze_bond_v2,
    clean_price_pct_to_dirty_amount,
    clean_price_pct_to_dirty_rub,
    price_cashflows,
    solve_continuous_z_spread,
    solve_z_spread,
)
from rub_fixed_income.analytics import (
    BondAnalyticsResultV1,
    BondAnalyticsResultV2,
    CalculationPolicy,
)
from rub_fixed_income.public_schedule import bind_market_data_store_schedule

START = date(2026, 1, 1)
CURVE = DiscountCurve.flat(START, 0.1)
FLOWS = ((date(2027, 1, 1), 100.0), (date(2028, 1, 1), 1100.0))


def test_each_deprecated_api_warns_from_the_callers_line() -> None:
    terms, schedule, market, curve, context = _bond_inputs()
    calls: dict[str, Callable[[], object]] = {
        "analyze_bond": lambda: analyze_bond(
            terms, schedule, market, curve, context, CalculationPolicy()
        ),
        "clean_price_pct_to_dirty_amount": lambda: clean_price_pct_to_dirty_amount(99, 1000, 5),
        "solve_continuous_z_spread": lambda: solve_continuous_z_spread(FLOWS, 1000.0, CURVE),
        "price_cashflows without settlement_date": lambda: price_cashflows(FLOWS, CURVE),
        "solve_z_spread without settlement_date": lambda: solve_z_spread(FLOWS, 1000.0, CURVE),
    }
    for name, call in calls.items():
        with pytest.warns(DeprecationWarning, match=f"rub_fixed_income: {name}") as record:
            call()
        assert record[0].filename == __file__


def test_bridge_warns() -> None:
    with pytest.warns(DeprecationWarning, match="bind_market_data_store_schedule"):
        record = bind_market_data_store_schedule(
            replace(snapshot(), cashflows=()), payload(), financing_curve_build_id=7
        )
    assert record.source_payload_id


def test_replacements_do_not_warn_and_match_the_deprecated_numbers() -> None:
    terms, schedule, market, curve, context = _bond_inputs()
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        v2 = analyze_bond_v2(terms, schedule, market, curve, context, CalculationPolicy())
        dirty = clean_price_pct_to_dirty_rub(99, 1000, 5)
        z = solve_z_spread(
            FLOWS,
            1000.0,
            CURVE,
            settlement_date=START,
            convention=ZSpreadConvention(compounding=CONTINUOUS),
        )
        price_cashflows(FLOWS, CURVE, settlement_date=START)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        assert clean_price_pct_to_dirty_amount(99, 1000, 5) == dirty
        assert solve_continuous_z_spread(FLOWS, 1000.0, CURVE) == pytest.approx(z, abs=1e-12)
        v1 = analyze_bond(terms, schedule, market, curve, context, CalculationPolicy())
    assert isinstance(v1, BondAnalyticsResultV1) and isinstance(v2, BondAnalyticsResultV2)
    assert v1.yield_pct == pytest.approx(v2.yield_pct, abs=1e-9)
