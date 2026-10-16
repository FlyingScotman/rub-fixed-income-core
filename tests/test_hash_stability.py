"""Pinned hashes: refactors must not change the bytes of identifiers consumers store."""

from __future__ import annotations

from dataclasses import asdict, replace
from datetime import date

import pytest
from test_local_worker import run, snapshot

from rub_fixed_income import (
    BondCashflow,
    BondRiskEngine,
    BondRiskRequest,
    BusinessCalendar,
    CurveBootstrapRequest,
    CurveConvention,
    OisQuote,
    ScheduleConvention,
    ZSpreadConvention,
    local_risk,
    local_worker,
)
from rub_fixed_income.analytics import (
    BondAnalyticsResultV1,
    BondAnalyticsResultV2,
    BondTermsV1,
    CalculationPolicy,
    CurveSnapshotV1,
    MarketObservationV1,
    ValuationContext,
    analyze_bond,
    analyze_bond_v2,
    canonical_sha256,
)
from rub_fixed_income.discount import DiscountCurve

SETTLEMENT = date(2026, 1, 1)
CALENDAR = BusinessCalendar(
    holidays=frozenset({date(2026, 6, 12), date(2027, 1, 1)}),
    working_days=frozenset({date(2026, 11, 1)}),
    version="synthetic-test-calendar",
)


def _bond_inputs() -> tuple[
    BondTermsV1,
    tuple[BondCashflow, ...],
    MarketObservationV1,
    CurveSnapshotV1,
    ValuationContext,
]:
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
    market = MarketObservationV1(100.0, SETTLEMENT, "midpoint", "market-1")
    curve = CurveSnapshotV1(
        "curve-world-1",
        DiscountCurve(SETTLEMENT, ((date(2027, 1, 1), 1 / 1.08), (date(2028, 1, 1), 1 / 1.08**2))),
        "curve-1",
    )
    return terms, schedule, market, curve, ValuationContext(SETTLEMENT, SETTLEMENT)


def _ois_request() -> CurveBootstrapRequest:
    convention = CurveConvention(
        fixed_leg_schedule=ScheduleConvention(calendar=CALENDAR),
    )
    return CurveBootstrapRequest(
        SETTLEMENT,
        (
            OisQuote("1Y", date(2027, 1, 1), 0.15),
            OisQuote("2Y", date(2028, 1, 3), 0.14),
            OisQuote("3Y", date(2029, 1, 3), 0.135),
        ),
        convention,
    )


def test_analyze_bond_result_hashes_are_pinned() -> None:
    terms, schedule, market, curve, context = _bond_inputs()
    v1 = analyze_bond(terms, schedule, market, curve, context, CalculationPolicy())
    v2 = analyze_bond_v2(terms, schedule, market, curve, context, CalculationPolicy())
    assert isinstance(v1, BondAnalyticsResultV1)
    assert isinstance(v2, BondAnalyticsResultV2)
    assert v1.result_sha256 == PINNED["analyze_bond"]
    assert v2.result_sha256 == PINNED["analyze_bond_v2"]


def test_risk_engine_base_id_is_pinned_for_calendar_bearing_curve() -> None:
    _, schedule, _, _, _ = _bond_inputs()
    request = BondRiskRequest(
        cashflows=schedule,
        settlement_date=SETTLEMENT,
        dirty_price_rub=1020.0,
        curve=_ois_request(),
        spread_convention=ZSpreadConvention(),
        spread_kind="credit",
    )
    assert BondRiskEngine(request).base_id == PINNED["risk_base_id"]


def test_local_worker_identifiers_are_pinned(monkeypatch: pytest.MonkeyPatch) -> None:
    # The job identity includes the library version; pin it to the version the hashes were taken at.
    monkeypatch.setattr(local_worker, "__version__", "0.8.1")
    monkeypatch.setattr(local_risk, "__version__", "0.8.1")
    assert snapshot().content_id == PINNED["snapshot_content_id"]
    calendar_snapshot = replace(snapshot(), curve=_ois_request())
    assert calendar_snapshot.content_id == PINNED["ois_snapshot_content_id"]
    result = run()
    assert result.job_id == PINNED["job_id"]
    assert canonical_sha256(_at_version(asdict(result), "0.8.1")) == PINNED["local_result_body"]


def _at_version(value: object, version: str) -> object:
    """Set every ``library_version`` and drop ``result_sha256`` (it hashes the version)."""
    if isinstance(value, dict):
        return {
            key: version if key == "library_version" else _at_version(item, version)
            for key, item in value.items()
            if key != "result_sha256"
        }
    if isinstance(value, (list, tuple)):
        return [_at_version(item, version) for item in value]
    return value


def test_canonical_sha256_of_plain_values_is_pinned() -> None:
    payload = {"d": date(2026, 1, 1), "x": [1, 2.5, "a"], "n": None}
    assert canonical_sha256(payload) == PINNED["plain"]


PINNED: dict[str, str] = {
    "analyze_bond": "d80ec4f02155f0c27673f21fc5f87f038cd0c8096f374e2abf77fd132b4a647c",
    "analyze_bond_v2": "2e0b13a343a300fd864af4a5d32ce32e19e956e4e9ce1ea4e303a56f797fe80c",
    "risk_base_id": "f7e742b3506ced02a767f1d3b981101970c31f92ebd33434917d8ec8ab76776b",
    "snapshot_content_id": "915af55b5daace8b7189435f0793270315043276270f04dd1ecc1c045d14c95b",
    "ois_snapshot_content_id": "5d9cd3199ddee2f0ec9d759cf14859caeb299027d21450977534dfcc4cff0f0e",
    "job_id": "0d9d9d88c815393c478fe3b6cbeb7d3468aa12f61b248062bfcfb28bcfa0935c",
    "local_result_body": "2d37374572ae89eb4dd69ffe13d7b21963c2f7b1395ae3dbbef381604b2b82ca",
    "plain": "b96b0e8e367c6ed6ee81cbccd316d0f95e0a02375e8fe7117c7a220e190c6afb",
}
