"""Native exercise assumptions in ``value_portfolio`` (BACKLOG #50).

``ValuationPolicy.exercise_assumptions`` values a callable or perpetual bond as the
deterministic bond its resolved exercise implies, exactly as ``value_assumed_portfolio``
does by wrapping the portfolio; the row records the assumption and a call horizon.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import date

from test_assumed_portfolio import (
    book_positions,
    book_snapshot,
    callable_bond,
    perpetual,
)
from test_portfolio import AS_OF, POLICY, instruments

from rub_fixed_income import (
    AssumptionPolicy,
    ExerciseAssumption,
    InstrumentAssumption,
    canonical_sha256,
    horizon_portfolio,
    portfolio_result_from_json,
    portfolio_result_to_json,
    to_dict,
    value_assumed_portfolio,
    value_portfolio,
)

ASSUMPTIONS = (
    ("CORP-CALL", ExerciseAssumption.to_worst()),
    ("PERP", ExerciseAssumption.to_call(date(2028, 12, 1))),
)
NATIVE = replace(POLICY, exercise_assumptions=ASSUMPTIONS)
BOOK = (*instruments()[:3], callable_bond(), perpetual())
POSITIONS = book_positions()[:5]  # no pass-through


def native(**kw: object):  # type: ignore[no-untyped-def]
    args: dict[str, object] = {
        "run_id": "eod-2026-10-15",
        "as_of": AS_OF,
        "snapshot": book_snapshot(),
        "instruments": BOOK,
        "positions": POSITIONS,
        "policy": NATIVE,
    }
    args.update(kw)
    return value_portfolio(**args)  # type: ignore[arg-type]


def test_native_assumptions_value_like_the_assumed_portfolio_wrapper() -> None:
    result = native()
    wrapped = value_assumed_portfolio(
        run_id="eod-2026-10-15",
        as_of=AS_OF,
        snapshot=book_snapshot(),
        instruments=BOOK,
        positions=POSITIONS,
        policy=POLICY,
        assumptions=AssumptionPolicy(
            overrides=tuple(InstrumentAssumption(i, exercise=a) for i, a in ASSUMPTIONS)
        ),
    )
    assert result.status == wrapped.status == "complete"
    rows = {v.position_id: v for v in result.positions}
    uses = {u.instrument_id: u for u in wrapped.assumptions}
    for theirs in wrapped.portfolio.positions:
        ours = rows[theirs.position_id]
        assert ours.pv_rub == theirs.pv_rub
        assert ours.factor_risk == theirs.factor_risk
        assert ours.effective_yield == theirs.effective_yield
        assert ours.horizon_date == theirs.horizon_date
        use = uses.get(theirs.instrument_id)
        if use is None:
            assert ours.exercise_assumption is None and ours.horizon == theirs.horizon
            continue
        assert ours.exercise_assumption == use.assumption
        assert ours.horizon == ("call" if use.exercise == "call" else "maturity")
    assert rows["p-perp"].horizon == "call"
    assert rows["p-perp"].horizon_date == date(2028, 12, 1)
    assert rows["p-perp"].exercise_assumption == ("exercise:to_call:2028-12-01@call:2028-12-01")


def test_result_stays_v2_and_records_the_assumption() -> None:
    result = native()
    assert result.schema_version == "rub-portfolio-risk-result.v2"
    data = portfolio_result_to_json(result)
    rows = {r["position_id"]: r for r in json.loads(data)["positions"]}
    assert rows["p-perp"]["exercise_assumption"].startswith("exercise:to_call")
    assert "exercise_assumption" not in rows["p-bullet"]
    assert portfolio_result_to_json(portfolio_result_from_json(data)) == data


def test_default_policy_keeps_its_bytes_and_callables_without_assumption_reject() -> None:
    assert canonical_sha256(POLICY) == canonical_sha256(replace(POLICY, exercise_assumptions=()))
    assert "exercise_assumptions" not in to_dict(POLICY)
    plain = native(policy=POLICY)
    codes = {r.position_id: r.code for r in plain.coverage.rejections}
    assert codes == {"p-call": "UNSUPPORTED_PRODUCT", "p-perp": "UNSUPPORTED_PRODUCT"}
    # Only the named instrument is exercised.
    one = native(policy=replace(POLICY, exercise_assumptions=ASSUMPTIONS[1:]))
    assert {r.position_id for r in one.coverage.rejections} == {"p-call"}


def test_an_assumption_that_cannot_apply_is_a_coded_rejection() -> None:
    bad = replace(
        POLICY, exercise_assumptions=(("PERP", ExerciseAssumption.to_call(date(2027, 12, 1))),)
    )
    codes = {r.position_id: r.code for r in native(policy=bad).coverage.rejections}
    assert codes["p-perp"] == "INVALID_TERMS"
    maturity = replace(POLICY, exercise_assumptions=(("PERP", ExerciseAssumption.to_maturity()),))
    codes = {r.position_id: r.code for r in native(policy=maturity).coverage.rejections}
    assert codes["p-perp"] == "UNSUPPORTED_PRODUCT"


def test_horizon_portfolio_applies_the_same_assumptions() -> None:
    result = horizon_portfolio(
        snapshot=book_snapshot(),
        instruments=BOOK,
        positions=POSITIONS,
        policy=NATIVE,
        horizon_date=date(2026, 12, 15),
    )
    assert {r.position_id for r in result.coverage.rejections} == set()
    plain = horizon_portfolio(
        snapshot=book_snapshot(),
        instruments=BOOK,
        positions=POSITIONS,
        policy=POLICY,
        horizon_date=date(2026, 12, 15),
    )
    assert {r.position_id for r in plain.coverage.rejections} == {"p-call", "p-perp"}
