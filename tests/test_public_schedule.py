from __future__ import annotations

from dataclasses import replace

import pytest
from test_local_worker import END, START, run, snapshot

from rub_fixed_income.public_schedule import bind_market_data_store_schedule


def payload() -> dict[str, object]:
    return {
        "secid": "SYNTHETIC",
        "settlement_date": START.isoformat(),
        "horizon_date": END.isoformat(),
        "horizon_type": "maturity",
        "formula_type": "fixed",
        "calculation_quality": "exact_published",
        "warnings": [],
        "projection_curve": None,
        "financing_curve_build_id": 7,
        "cashflows": [
            {
                "payment_date": END.isoformat(),
                "coupon": 0,
                "principal": 1000,
                "total": 1000,
                "coupon_source": None,
            }
        ],
    }


def test_service_payload_to_worker_is_identical_to_direct_valuation() -> None:
    record = bind_market_data_store_schedule(
        replace(snapshot(), cashflows=()),
        payload(),
        financing_curve_build_id=7,
    )
    result = run((record,))
    assert result.total_pv_rub == run().total_pv_rub
    assert result.scenarios[0].total_pnl_rub == run().scenarios[0].total_pnl_rub
    assert record.source_payload_id
    assert result.public_bundle_id != run().public_bundle_id


@pytest.mark.parametrize(
    "changes",
    [
        {"secid": "other"},
        {"settlement_date": "2025-12-31"},
        {"horizon_date": "2028-01-02"},
        {"horizon_type": "call"},
        {"formula_type": "arithmetic_ruonia"},
        {"calculation_quality": "approximate"},
        {"warnings": ["incomplete"]},
        {"projection_curve": {}},
        {"financing_curve_build_id": 8},
        {"financing_curve_build_id": True},
        {"cashflows": []},
    ],
)
def test_unverified_or_mismatched_service_payload_rejected(changes: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        bind_market_data_store_schedule(
            replace(snapshot(), cashflows=()),
            {**payload(), **changes},
            financing_curve_build_id=7,
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"total": 999},
        {"principal": -1},
        {"total": float("nan")},
        {"coupon_source": "projected"},
        {"principal": 0, "coupon": 1000},
        {"payment_date": "2029-01-01"},
        {"payment_date": "2025-01-01"},
    ],
)
def test_invalid_cashflow_never_becomes_fixed_risk(changes: dict[str, object]) -> None:
    data = payload()
    data["cashflows"] = [
        {
            "payment_date": END.isoformat(),
            "coupon": 0,
            "principal": 1000,
            "total": 1000,
            "coupon_source": None,
            **changes,
        }
    ]
    with pytest.raises(ValueError):
        bind_market_data_store_schedule(
            replace(snapshot(), cashflows=()),
            data,
            financing_curve_build_id=7,
        )


def test_confirmed_put_and_amortization_are_preserved() -> None:
    data = payload()
    data["horizon_type"] = "offer"
    data["cashflows"] = [
        {
            "payment_date": "2027-01-01",
            "coupon": 50,
            "principal": 300,
            "total": 350,
            "coupon_source": "published_fixed",
        },
        {
            "payment_date": END.isoformat(),
            "coupon": 35,
            "principal": 686,
            "total": 721,
            "coupon_source": "published_fixed",
        },
    ]
    record = bind_market_data_store_schedule(
        replace(snapshot(), cashflows=(), horizon="confirmed_investor_put"),
        data,
        financing_curve_build_id=7,
    )
    assert [f.amount for f in record.cashflows] == [350, 721]
    assert run((record,)).coverage.status == "complete"
