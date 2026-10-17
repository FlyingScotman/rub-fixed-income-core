"""Caller-held base and shocked OIS curves across jobs (roadmap 0.7)."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest
from test_portfolio import POLICY, run, snapshot

from rub_fixed_income import (
    RISK_BUCKETS,
    InvalidTerms,
    PrebuiltCurve,
    ShockedCurveCache,
    from_dict,
    portfolio_result_to_json,
    to_dict,
)

OIS_POLICY = replace(POLICY, curve_overrides=(("OFZ-AMORT", "RUB_OIS"), ("OFZ-BULLET", "RUB_OIS")))


def cold_run() -> tuple[bytes, ShockedCurveCache]:
    cache = ShockedCurveCache()
    result = run(policy=OIS_POLICY, curve_cache=cache)
    assert {p.curve_id for p in result.positions} >= {"RUB_OIS"}
    return portfolio_result_to_json(result), cache


def persisted(cache: ShockedCurveCache) -> tuple[PrebuiltCurve, ...]:
    text = json.dumps([to_dict(e) for e in cache.export()])
    return tuple(from_dict(PrebuiltCurve, item) for item in json.loads(text))


def test_seeded_cache_skips_every_bootstrap_and_gives_identical_bytes() -> None:
    cold, cache = cold_run()
    assert cache.misses == len(cache) > 1
    entries = persisted(cache)
    assert entries == cache.export()
    warm_cache = ShockedCurveCache()
    assert warm_cache.seed(entries) == len(entries)
    warm = portfolio_result_to_json(run(policy=OIS_POLICY, curve_cache=warm_cache))
    assert warm == cold
    assert warm_cache.misses == 0 and warm_cache.hits > 0 and warm_cache.seeded == len(entries)
    assert portfolio_result_to_json(run(policy=OIS_POLICY)) == cold


def test_curve_key_is_stable_and_depends_on_shifts() -> None:
    _, cache = cold_run()
    entries = cache.export()
    keys = [e.curve_key for e in entries]
    assert keys == sorted(keys) and len(set(keys)) == len(keys)
    assert len({e.request_id for e in entries}) == 1
    base = next(e for e in entries if not any(e.shifts))
    assert base.curve_key == replace(base, shifts=(0.0,) * len(RISK_BUCKETS)).curve_key


def test_seed_rejects_curves_that_do_not_belong_to_their_request() -> None:
    _, cache = cold_run()
    entries = cache.export()
    shocked = next(e for e in entries if any(e.shifts))
    nodes = list(shocked.curve.nodes)
    nodes[-1] = (nodes[-1][0], nodes[-1][1] * (1 + 1e-9))
    tampered = replace(shocked, curve=replace(shocked.curve, nodes=tuple(nodes)))
    swapped = replace(shocked, shifts=next(e for e in entries if e is not shocked).shifts)
    other_date = replace(
        shocked, curve=replace(shocked.curve, curve_date=shocked.curve.curve_date.replace(day=1))
    )
    for bad in (tampered, swapped, other_date):
        fresh = ShockedCurveCache()
        with pytest.raises(InvalidTerms):
            fresh.seed((*entries[:1], bad))
        assert len(fresh) == 0 and fresh.seeded == 0
    with pytest.raises(TypeError):
        ShockedCurveCache().seed(["not a curve"])  # type: ignore[list-item]


def test_seed_respects_capacity_and_skips_known_curves() -> None:
    _, cache = cold_run()
    entries = cache.export()
    small = ShockedCurveCache(max_entries=2)
    assert small.seed(entries) == 2 and len(small) == 2
    assert small.seed(entries) == 0
    assert cache.seed(entries) == 0


def test_value_portfolio_checks_the_cache_type() -> None:
    with pytest.raises(TypeError):
        run(curve_cache={})
    assert snapshot().curve("RUB_OIS").spec_id == "rub_ruonia_ois_legacy_v1"


def test_curve_caches_stay_out_of_pickles_and_mutable_inputs_are_not_memoised() -> None:
    import pickle
    from datetime import date

    from rub_fixed_income import DiscountCurve
    from rub_fixed_income._risk_common import _snapshot_curve

    _, cache = cold_run()
    curve = cache.export()[0].curve
    curve.discount_factor(date(2027, 1, 15))
    assert "_df_memo" in curve.__dict__
    clone = pickle.loads(pickle.dumps(curve))
    assert clone == curve and set(clone.__dict__) == set(curve.__dataclass_fields__)
    assert clone.discount_factor(date(2027, 1, 15)) == curve.discount_factor(date(2027, 1, 15))

    nodes = [(date(2027, 10, 15), 0.88), (date(2028, 10, 15), 0.78)]
    mutable = DiscountCurve(date(2026, 10, 15), nodes)  # type: ignore[arg-type]
    first = _snapshot_curve(mutable)
    nodes[0] = (date(2027, 10, 15), 0.87)
    assert _snapshot_curve(mutable) != first
    frozen = DiscountCurve(date(2026, 10, 15), tuple(nodes))
    assert _snapshot_curve(frozen) is _snapshot_curve(frozen)
