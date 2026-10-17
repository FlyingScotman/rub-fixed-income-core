"""Pinned hashes: refactors must not change the bytes of identifiers consumers store."""

from __future__ import annotations

import hashlib
import sys
from dataclasses import asdict, replace
from datetime import date
from pathlib import Path

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
    portfolio,
    portfolio_result_to_json,
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

# These tests pin the behaviour of APIs deprecated in 0.12.0 (kept until 1.0);
# tests/test_deprecations.py checks that each one warns.
pytestmark = pytest.mark.filterwarnings("ignore:rub_fixed_income:DeprecationWarning")


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


def _pin_version(monkeypatch: pytest.MonkeyPatch, version: str) -> None:
    monkeypatch.setattr(portfolio, "__version__", version)
    monkeypatch.setattr(local_risk, "__version__", version)


def test_bond_only_portfolio_results_keep_their_0_26_v2_hashes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bond-only books (fixed, amortizing, put, floaters, non-RUB, linkers) give the
    bytes, job IDs and result hashes of release 0.26.0 after the v3 work."""
    import test_inflation
    import test_non_rub_bonds
    import test_portfolio
    import test_portfolio_floaters

    _pin_version(monkeypatch, "0.26.0")
    books = {
        "portfolio": test_portfolio.run(),
        "portfolio_all": test_portfolio.run(positions=test_portfolio.positions()),
        "non_rub": test_non_rub_bonds.run(),
        "floaters": test_portfolio_floaters.run(),
        "linker": test_inflation._value([test_inflation.bond(), test_inflation.fixed_bond()]),
    }
    # Python 3.12 made float sum() compensated, so some 0.26.0 numbers differ in the last
    # bit between 3.11 and later; each interpreter is pinned to the 0.26.0 output on it.
    pinned = {**V2_PINNED, **V2_PINNED_PY311} if sys.version_info < (3, 12) else V2_PINNED
    for name, result in books.items():
        data = portfolio_result_to_json(result)
        assert result.schema_version == "rub-portfolio-risk-result.v2", name
        assert (result.job_id, result.result_sha256, hashlib.sha256(data).hexdigest()) == (
            pinned[name]
        ), name


def test_portfolio_v3_result_is_pinned(monkeypatch: pytest.MonkeyPatch) -> None:
    from portfolio_v3_book import pinned_v3_result

    _pin_version(monkeypatch, "0.27.0")
    result = pinned_v3_result()
    assert result.schema_version == "rub-portfolio-risk-result.v3"
    assert (result.job_id, result.result_sha256) == (
        PINNED["portfolio_v3_job_id"],
        PINNED["portfolio_v3_result"],
    )
    assert portfolio_result_to_json(result) == V3_FIXTURE.read_bytes()


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
    "portfolio_v3_job_id": "f2ed552f4ff6c90de05e7be0a377759db76804394d42b5b028274689390bcb5a",
    "portfolio_v3_result": "ff72b04d27e0d9166e9034247f3198d1e4ce31e3b1ad1c6245b56fd293ed6635",
}

V3_FIXTURE = Path(__file__).parent / "fixtures" / "portfolio_result_v3.json"

# (job_id, result_sha256, SHA-256 of the JSON bytes) of bond-only books under 0.26.0.
V2_PINNED: dict[str, tuple[str, str, str]] = {
    "portfolio": (
        "a1b88d5e0854bb3feae7f6934390f43c3e4cd75fb015cb73b8d851a0c974a0ad",
        "d2b939e2b1440227a3112b8b164d340b03a80eaade64fb53a66beca0bea29c1c",
        "0d154ef550229533e254cb63d3fab908f778505a3c28666b9a24eb887e6cb2b2",
    ),
    "portfolio_all": (
        "70828ba8a7fba69227394bd9d1a22f469b7aa7f5ff2f782ca62497e2ca72eef9",
        "169d809f1036e5069e835003f5b9ee7d54ac06c0e1cee33ee9af880f94793f49",
        "e9b76a34756e16035eefd78c3f0b348a6ca5c845616e47b4e2729e1a950fd9af",
    ),
    "non_rub": (
        "53d764fdd2ee6e60c44e05ee85bce512ba31f8d2ffcd272c39e8c3f4de2adf00",
        "835b09634bd6a836976c89007f9a31aeee5a9866aac288aa2f5a64a92c4e72c8",
        "59388261960d8a79ccfedbd32b1c4298f8b008957a903fd12a78d7f3b85f3574",
    ),
    "floaters": (
        "7bcb2cbabebf2ce400560a3b26e88932b2e380a0fbe19c35cc71ff3f054284bf",
        "3b9fad26221a40d8788b966fc6855727c4e3bf7cfac9fdcd279cc1d5d6f6d809",
        "ca7fbf288301b99a526d2b66768423340de92d975b434f100376c76b67e2f882",
    ),
    "linker": (
        "7617c6f9ea550d84a24d2d436fdf23f4dbb3b64eef5bf01007dadf135a876b8d",
        "45920705d72d558c6832924f432ab43595129b200bf98a6eb2f26885704c74a7",
        "f9c101e2073fea9c0ffde0d81a933e300e5a6687d274189b973573007f7276ae",
    ),
}

# The books whose 0.26.0 output differs on Python 3.11 (taken from 0.26.0 code on 3.11).
V2_PINNED_PY311: dict[str, tuple[str, str, str]] = {
    "portfolio": (
        "a1b88d5e0854bb3feae7f6934390f43c3e4cd75fb015cb73b8d851a0c974a0ad",
        "e2c15dee10184746bac89cfa3819dd3219ae8bf659464b784d34a4edde3dccf8",
        "0fb4f6807c24d06316af6edf7d8ba2ce2b99db93860e939c1f986ecb54499622",
    ),
    "portfolio_all": (
        "70828ba8a7fba69227394bd9d1a22f469b7aa7f5ff2f782ca62497e2ca72eef9",
        "c07a75d0fe52bd369b106442aa1c8ad1a1e5b43b835307a4e6a651a88784a798",
        "50a43c746f0ea3cd2f8269a3c3114d0741f7201c1552b790d3ee43eb781675d1",
    ),
    "floaters": (
        "7bcb2cbabebf2ce400560a3b26e88932b2e380a0fbe19c35cc71ff3f054284bf",
        "8b454b01725edb318deb2fcd360a03918084695034d6c2a20e6c36be6f99d8f5",
        "98bcd1e70c81668c7a4c6715bd2b9c55d1b21e558690090c163d5fd40f110303",
    ),
}
