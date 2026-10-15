from __future__ import annotations

import math
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest

from rub_fixed_income import (
    CONTINUOUS,
    BondCashflow,
    BondScenario,
    CurveBootstrapRequest,
    DiscountCurve,
    OisQuote,
    ZSpreadConvention,
)
from rub_fixed_income.analytics import canonical_json_bytes
from rub_fixed_income.local_worker import (
    LocalPosition,
    LocalRiskRunResult,
    LocalScenario,
    PublicBondSnapshot,
    SnapshotEvidence,
    run_local_risk,
)

START = date(2026, 1, 1)
END = START + timedelta(days=730)
NOW = datetime(2026, 1, 1, 12, tzinfo=UTC)


def snapshot() -> PublicBondSnapshot:
    evidence = SnapshotEvidence("synthetic-v1", "synthetic", NOW, NOW, NOW + timedelta(hours=1))
    return PublicBondSnapshot(
        "SYNTHETIC",
        evidence,
        evidence,
        evidence,
        "synthetic-calendar-v1",
        True,
        True,
        (BondCashflow(END, 1000),),
        START,
        END,
        "maturity",
        "RUB",
        "fixed",
        1000,
        79,
        0,
        DiscountCurve.flat(START, 0.1),
        ZSpreadConvention(compounding=CONTINUOUS),
        "credit",
    )


def run(
    records: tuple[PublicBondSnapshot, ...] | None = None,
    positions: tuple[LocalPosition, ...] | None = None,
    scenarios: tuple[LocalScenario, ...] | None = None,
    **kwargs: Any,
) -> LocalRiskRunResult:
    args: dict[str, Any] = {
        "run_id": "run-1",
        "as_of": NOW,
        "curve_basis": "continuous_zero",
        "public_snapshots": (snapshot(),) if records is None else records,
        "positions": (LocalPosition("long", "SYNTHETIC", 2, 0.4),)
        if positions is None
        else positions,
        "scenarios": (LocalScenario("up", "continuous_zero", BondScenario.parallel(100)),)
        if scenarios is None
        else scenarios,
    }
    args.update(kwargs)
    return run_local_risk(**args)


def test_independent_zero_coupon_risk_hessian_jtd_and_exact_scenario() -> None:
    result = run()
    assert result.coverage.status == "complete"
    assert result.coverage.completed == 1
    assert result.total_pv_rub == pytest.approx(1580)
    row = result.positions[0]
    assert row.risk is not None and row.second_order is not None and row.default is not None
    assert row.risk.difference == "central"
    assert row.risk.dv01_rub == pytest.approx(-1580 * 2e-4, rel=1e-7)
    assert row.second_order.gradient_rub_per_bp[-1] == pytest.approx(-1580 * 2e-4, rel=1e-7)
    assert row.second_order.hessian_rub_per_bp2[-1][-1] == pytest.approx(1580 * 4e-8, rel=1e-6)
    assert row.default.jtd_pnl_rub == pytest.approx(800 - 1580)
    assert result.scenarios[0].total_pnl_rub == pytest.approx(1580 * math.expm1(-0.02))
    assert result.scenarios[0].positions[0].calculation_id == row.calculation_id
    assert b'"total_pv_rub"' in canonical_json_bytes(result)


def test_missing_and_unsupported_positions_remain_in_coverage() -> None:
    unsupported = replace(snapshot(), instrument_id="FLOAT", coupon_model="floating")
    result = run(
        (snapshot(), unsupported),
        (
            LocalPosition("ok", "SYNTHETIC", 1, 0.4),
            LocalPosition("absent", "MISSING", 1, 0.4),
            LocalPosition("floating", "FLOAT", 1, 0.4),
        ),
    )
    assert result.coverage.status == "partial"
    assert result.coverage.failed_position_ids == ("absent", "floating")
    assert result.total_pv_rub is None
    assert result.covered_pv_rub == pytest.approx(790)
    scenario = result.scenarios[0]
    assert scenario.coverage.status == "partial"
    assert scenario.total_pnl_rub is None
    assert scenario.covered_pnl_rub == pytest.approx(790 * math.expm1(-0.02))
    assert len(scenario.positions) == 3


def test_all_failed_and_complete_empty_book_are_distinct() -> None:
    failed = run(records=())
    assert failed.coverage.status == "failed"
    assert failed.total_pv_rub is None
    assert failed.scenarios[0].total_pnl_rub is None
    empty = run(positions=())
    assert empty.coverage.status == "complete"
    assert empty.coverage.expected == 0
    assert empty.total_pv_rub == 0
    assert empty.scenarios[0].total_pnl_rub == 0


def test_opposite_and_zero_positions_are_not_dropped_or_netted_in_coverage() -> None:
    result = run(
        positions=(
            LocalPosition("long", "SYNTHETIC", 2, 0.4),
            LocalPosition("short", "SYNTHETIC", -2, 0.4),
            LocalPosition("zero", "SYNTHETIC", 0, 0.4),
        )
    )
    assert result.coverage.completed == 3
    assert result.total_pv_rub == pytest.approx(0)
    assert result.scenarios[0].total_pnl_rub == pytest.approx(0)
    assert len({r.calculation_id for r in result.positions}) == 3


@pytest.mark.parametrize("field", ["market", "schedule", "curve_evidence"])
@pytest.mark.parametrize("change", ["stale", "future", "naive", "inverted"])
def test_each_snapshot_requires_fresh_known_evidence(field: str, change: str) -> None:
    record = snapshot()
    evidence = record.market
    if change == "stale":
        evidence = replace(evidence, valid_until=NOW - timedelta(seconds=1))
    elif change == "future":
        evidence = replace(evidence, known_at=NOW + timedelta(seconds=1))
    elif change == "naive":
        evidence = replace(evidence, observed_at=NOW.replace(tzinfo=None))
    else:
        evidence = replace(evidence, observed_at=NOW + timedelta(seconds=1))
    result = run((replace(record, **{field: evidence}),))  # type: ignore[arg-type]
    assert result.coverage.status == "failed"
    assert result.positions[0].error


@pytest.mark.parametrize(
    "change",
    [
        {"calendar_version": ""},
        {"schedule_complete": False},
        {"redemption_confirmed": False},
        {"schedule_complete": 1},
        {"cashflow_currency": "USD"},
        {"horizon": "issuer_call"},
        {"cashflows": ()},
        {"clean_price_pct": -1},
        {"current_nominal_rub": 0},
    ],
)
def test_invalid_instrument_cannot_be_priced_as_fixed(change: dict[str, object]) -> None:
    assert run((replace(snapshot(), **change),)).coverage.status == "failed"  # type: ignore[arg-type]


def test_scenario_failure_does_not_destroy_successful_base_and_other_scenarios() -> None:
    result = run(
        scenarios=(
            LocalScenario("valid", "continuous_zero", BondScenario.parallel(10)),
            LocalScenario("wrong-basis", "ois_quote", BondScenario.parallel(10)),
            LocalScenario("overflow", "continuous_zero", BondScenario.parallel(-1e9)),
        )
    )
    assert result.coverage.status == "complete"
    assert result.status == "partial"
    assert result.scenarios[0].coverage.status == "complete"
    assert [s.coverage.status for s in result.scenarios[1:]] == ["failed", "failed"]
    assert all(s.total_pnl_rub is None for s in result.scenarios[1:])


def test_quote_basis_uses_original_ois_quotes_and_exact_rebootstrap() -> None:
    record = replace(snapshot(), curve=CurveBootstrapRequest(START, (OisQuote("2Y", END, 0.1),)))
    result = run(
        (record,),
        curve_basis="ois_quote",
        scenarios=(LocalScenario("up", "ois_quote", BondScenario.parallel(100)),),
    )
    assert result.coverage.status == "complete"
    assert result.positions[0].risk is not None
    assert result.positions[0].risk.curve_basis == "ois_quote"
    # A single 2Y annual-pay par quote bootstraps a constant annual rate.
    assert result.scenarios[0].total_pnl_rub == pytest.approx(1580 * ((1.1 / 1.11) ** 2 - 1))


def test_identities_bind_private_and_public_inputs_and_replay_is_deterministic() -> None:
    baseline = run()
    assert baseline == run()
    changed_runs = [
        run(run_id="run-2"),
        run(as_of=NOW + timedelta(seconds=1)),
        run(include_second_order=False),
        run(bump_bp=0.5),
        run(positions=(LocalPosition("long", "SYNTHETIC", 2, 0.5),)),
        run(positions=(LocalPosition("other-position", "SYNTHETIC", 2, 0.4),)),
        run((replace(snapshot(), clean_price_pct=80),)),
        run((replace(snapshot(), calendar_version="synthetic-v2"),)),
        run((replace(snapshot(), market=replace(snapshot().market, source_id="corrected")),)),
        run(scenarios=(LocalScenario("up", "continuous_zero", BondScenario.parallel(200)),)),
        run(
            positions=(
                LocalPosition("long", "SYNTHETIC", 2, 0.4),
                LocalPosition("missing", "ABSENT", 1, 0.4),
            )
        ),
    ]
    assert all(r.job_id != baseline.job_id for r in changed_runs)
    assert all(
        r.scenarios[0].scenario_id != baseline.scenarios[0].scenario_id for r in changed_runs
    )
    assert changed_runs[4].public_bundle_id == baseline.public_bundle_id


def test_duplicate_ids_are_job_errors() -> None:
    with pytest.raises(ValueError, match="duplicate instrument"):
        run((snapshot(), snapshot()))
    p = LocalPosition("same", "SYNTHETIC", 1, 0.4)
    with pytest.raises(ValueError, match="duplicate position"):
        run(positions=(p, p))
    s = LocalScenario("same", "continuous_zero", BondScenario())
    with pytest.raises(ValueError, match="duplicate scenario"):
        run(scenarios=(s, s))


@pytest.mark.parametrize(
    "changes",
    [
        {"quantity": float("nan")},
        {"quantity": True},
        {"recovery_rate": -0.1},
        {"recovery_rate": 1.1},
        {"recovery_rate": True},
        {"position_id": ""},
    ],
)
def test_position_units_rejected(changes: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        replace(LocalPosition("p", "SYNTHETIC", 1, 0.4), **changes)  # type: ignore[arg-type]


def test_no_mutation_or_hidden_second_order_work() -> None:
    flows = [BondCashflow(END, 1000)]
    record = replace(snapshot(), cashflows=flows)  # type: ignore[arg-type]
    result = run((record,), include_second_order=False)
    flows.clear()
    assert result.total_pv_rub == pytest.approx(1580)
    assert result.positions[0].second_order is None


@pytest.mark.parametrize(
    "changes",
    [
        {"run_id": ""},
        {"as_of": NOW.replace(tzinfo=None)},
        {"curve_basis": "unknown"},
        {"bump_bp": 0},
        {"bump_bp": True},
        {"include_second_order": 1},
    ],
)
def test_job_guards(changes: dict[str, object]) -> None:
    with pytest.raises((ValueError, TypeError)):
        run(**changes)  # type: ignore[arg-type]
