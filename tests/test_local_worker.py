from __future__ import annotations

import json
import math
from collections.abc import Iterator
from dataclasses import asdict, replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
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
    local_worker,
)
from rub_fixed_income.analytics import canonical_json_bytes, canonical_sha256
from rub_fixed_income.local_risk import prepare_local_bond_risk
from rub_fixed_income.local_worker import (
    LOCAL_RISK_RESULT_SCHEMA,
    LocalPosition,
    LocalRiskRunResult,
    LocalScenario,
    PublicBondSnapshot,
    SnapshotEvidence,
    local_risk_result_from_json,
    local_risk_result_hash_projection,
    local_risk_result_to_json,
    run_local_risk,
    verify_local_risk_result,
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


def test_ois_job_shares_shocked_curves_across_positions(monkeypatch: pytest.MonkeyPatch) -> None:
    record = replace(snapshot(), curve=CurveBootstrapRequest(START, (OisQuote("2Y", END, 0.1),)))
    caches: list[Any] = []

    def spy(source: Any, **kwargs: Any) -> Any:
        caches.append(kwargs["curve_cache"])
        return prepare_local_bond_risk(source, **kwargs)

    monkeypatch.setattr(local_worker, "prepare_local_bond_risk", spy)
    positions = tuple(LocalPosition(f"p{i}", "SYNTHETIC", i + 1, 0.4) for i in range(3))
    single = run((record,), positions[:1], (), curve_basis="ois_quote")
    built_for_one = caches[0].misses
    caches.clear()
    triple = run((record,), positions, (), curve_basis="ois_quote")
    assert triple.status == "complete"
    assert len(caches) == 3 and all(c is caches[0] for c in caches)
    # Later positions reuse every shocked curve built for the first one.
    assert caches[0].misses == built_for_one
    assert caches[0].hits > 2 * built_for_one
    assert triple.positions[0].risk == single.positions[0].risk
    assert triple.positions[0].second_order == single.positions[0].second_order


def test_cancellation_between_positions_keeps_skipped_rows_in_coverage() -> None:
    checks: list[None] = []

    def cancel_after_first_position() -> bool:
        checks.append(None)
        return len(checks) > 2  # position 1, its scenario, then stop

    result = run(
        positions=(
            LocalPosition("first", "SYNTHETIC", 1, 0.4),
            LocalPosition("second", "SYNTHETIC", 1, 0.4),
        ),
        should_cancel=cancel_after_first_position,
    )
    assert result.interruption == "cancelled"
    assert result.status == "partial"
    assert result.coverage.failed_position_ids == ("second",)
    assert result.positions[1].error == "job cancelled before calculation"
    assert result.positions[1].calculation_id is None
    assert result.total_pv_rub is None
    assert result.covered_pv_rub == pytest.approx(790)
    scenario = result.scenarios[0]
    assert [p.position_id for p in scenario.positions] == ["first", "second"]
    assert scenario.positions[1].error == "job cancelled before calculation"
    assert scenario.total_pnl_rub is None
    assert len(checks) == 3
    # Job identity describes inputs, not how far an interrupted run got.
    assert (
        result.job_id
        == run(
            positions=(
                LocalPosition("first", "SYNTHETIC", 1, 0.4),
                LocalPosition("second", "SYNTHETIC", 1, 0.4),
            )
        ).job_id
    )


def test_cancellation_before_scenario_preserves_base_risk() -> None:
    answers = iter([False, False, True])
    result = run(
        scenarios=(
            LocalScenario("a", "continuous_zero", BondScenario.parallel(10)),
            LocalScenario("b", "continuous_zero", BondScenario.parallel(20)),
        ),
        should_cancel=lambda: next(answers),
    )
    assert result.coverage.status == "complete"
    assert result.total_pv_rub == pytest.approx(1580)
    assert result.status == "partial"
    assert [s.coverage.status for s in result.scenarios] == ["complete", "failed"]
    assert result.scenarios[1].positions[0].calculation_id == result.positions[0].calculation_id


def test_time_budget_uses_monotonic_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    ticks: Iterator[float] = iter(range(0, 1000, 10))
    monkeypatch.setattr(local_worker, "_monotonic", lambda: next(ticks))
    positions = tuple(LocalPosition(f"p{i}", "SYNTHETIC", 1, 0.4) for i in range(3))
    # Start at 0; checks at 10 (p0), 20 (p0 scenario) pass; 30 >= 25 stops p1 and p2.
    result = run(positions=positions, time_budget_seconds=25)
    assert result.interruption == "time_budget_exceeded"
    assert result.coverage.failed_position_ids == ("p1", "p2")
    assert result.positions[2].error == "job time budget exceeded before calculation"
    assert run(time_budget_seconds=0).status == "failed"


def test_uninterrupted_job_reports_no_interruption() -> None:
    result = run(time_budget_seconds=3600, should_cancel=lambda: False)
    assert result.interruption is None
    assert result.status == "complete"
    assert result == run()


@pytest.mark.parametrize(
    "changes",
    [{"time_budget_seconds": -1}, {"time_budget_seconds": float("nan")}, {"should_cancel": 1}],
)
def test_interruption_guards(changes: dict[str, object]) -> None:
    with pytest.raises((ValueError, TypeError)):
        run(**changes)  # type: ignore[arg-type]


def _contract_example() -> LocalRiskRunResult:
    return run(
        positions=(
            LocalPosition("long", "SYNTHETIC", 2, 0.4),
            LocalPosition("absent", "MISSING", 1, 0.4),
        ),
        scenarios=(
            LocalScenario("up", "continuous_zero", BondScenario.parallel(100, spread_shift_bp=25)),
        ),
    )


def test_result_carries_schema_and_verifiable_hash() -> None:
    result = _contract_example()
    assert result.schema_version == LOCAL_RISK_RESULT_SCHEMA
    assert verify_local_risk_result(result)
    assert result.result_sha256 == canonical_sha256(local_risk_result_hash_projection(result))
    assert not verify_local_risk_result(replace(result, covered_pv_rub=1.0))
    assert not verify_local_risk_result(replace(result, schema_version="v0"))


def test_result_json_round_trip_is_exact() -> None:
    for result in (_contract_example(), run(should_cancel=lambda: True), run(positions=())):
        data = local_risk_result_to_json(result)
        rebuilt = local_risk_result_from_json(data)
        assert rebuilt == result
        assert local_risk_result_to_json(rebuilt) == data
        assert local_risk_result_from_json(data.decode()) == result


def test_result_json_rejects_tampering_unknown_schema_and_fields() -> None:
    raw = json.loads(local_risk_result_to_json(_contract_example()))
    tampered = dict(raw, covered_pv_rub=raw["covered_pv_rub"] + 1)
    with pytest.raises(ValueError, match="does not verify"):
        local_risk_result_from_json(json.dumps(tampered))
    with pytest.raises(ValueError, match="schema_version"):
        local_risk_result_from_json(json.dumps(dict(raw, schema_version="other.v9")))
    with pytest.raises(ValueError, match="schema_version"):
        local_risk_result_from_json("[]")
    with pytest.raises(ValueError, match="not valid JSON"):
        local_risk_result_from_json(b"{")
    # A consistent hash does not make an unknown field acceptable.
    extra = dict(raw, unexpected=1)
    extra["result_sha256"] = canonical_sha256(local_risk_result_hash_projection(extra))
    with pytest.raises(ValueError, match="fields do not match"):
        local_risk_result_from_json(json.dumps(extra))
    nested = json.loads(json.dumps(raw))
    del nested["positions"][0]["risk"]["buckets"][0]["krd_years"]
    nested["result_sha256"] = canonical_sha256(local_risk_result_hash_projection(nested))
    with pytest.raises(ValueError, match="fields do not match BucketRisk"):
        local_risk_result_from_json(json.dumps(nested))
    with pytest.raises(ValueError, match="does not verify"):
        local_risk_result_to_json(replace(_contract_example(), result_sha256=""))


FIXTURE = Path(__file__).parent / "fixtures" / "local_risk_result_v1.json"


def test_frozen_v1_fixture_still_parses_and_reserializes_byte_for_byte() -> None:
    """Changing any persisted result field requires a new schema version."""
    data = FIXTURE.read_bytes()
    result = local_risk_result_from_json(data)
    assert result.schema_version == LOCAL_RISK_RESULT_SCHEMA
    assert result.status == "partial"
    assert local_risk_result_to_json(result) == data
    assert set(asdict(result)) == set(asdict(_contract_example()))
