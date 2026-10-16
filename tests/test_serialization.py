"""Canonical forms, to_dict/from_dict round trips and typed rejections (roadmap 0.4)."""

from __future__ import annotations

import json
import random
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from enum import Enum

import pytest

from rub_fixed_income import (
    BusinessCalendar,
    BusinessDayConvention,
    CalendarRangeError,
    CoreRejection,
    CurveBootstrapRequest,
    CurveConvention,
    DayCount,
    DiscountCurve,
    InvalidTerms,
    MissingMarketData,
    OisQuote,
    ScheduleConvention,
    UnknownCurveSpec,
    UnsupportedProduct,
    ZSpreadConvention,
    canonical_json_bytes,
    canonical_sha256,
    from_dict,
    to_dict,
)
from rub_fixed_income.local_worker import SnapshotEvidence

BASE = date(2026, 1, 1)


def random_calendar(rng: random.Random) -> BusinessCalendar:
    days = [BASE + timedelta(days=rng.randrange(365)) for _ in range(rng.randrange(8))]
    holidays = frozenset(day for day in days if day.weekday() < 5)
    working = frozenset(day for day in days if day.weekday() >= 5)
    bounded = rng.random() < 0.5
    return BusinessCalendar(
        holidays,
        working,
        f"cal-{rng.randrange(1000)}",
        BASE if bounded else None,
        BASE + timedelta(days=364) if bounded else None,
    )


def random_convention(rng: random.Random) -> CurveConvention:
    schedule = (
        None
        if rng.random() < 0.3
        else ScheduleConvention(
            rng.choice((3, 6, 12)),
            rng.choice(("forward", "backward")),
            rng.choice(list(BusinessDayConvention)),
            random_calendar(rng),
        )
    )
    return CurveConvention(
        time_day_count=rng.choice(list(DayCount)),
        fixed_leg_schedule=schedule,
        fixed_leg_day_count=rng.choice(list(DayCount)),
    )


def random_curve(rng: random.Random) -> DiscountCurve | CurveBootstrapRequest:
    convention = random_convention(rng)
    if rng.random() < 0.3:
        return DiscountCurve.flat(BASE, rng.uniform(0.0, 0.2), convention=convention)
    pillars = sorted({rng.randrange(30, 7300) for _ in range(rng.randrange(1, 6))})
    if rng.random() < 0.5:
        nodes = tuple((BASE + timedelta(days=d), 1.0 / (1.0 + 0.1 * d / 365)) for d in pillars)
        return DiscountCurve(BASE, nodes, convention=convention)
    quotes = tuple(
        OisQuote(f"{d}D", BASE + timedelta(days=d), rng.uniform(0.05, 0.2)) for d in pillars
    )
    return CurveBootstrapRequest(BASE, quotes, convention)


@pytest.mark.parametrize("seed", range(40))
def test_seeded_round_trips_are_exact_and_hash_stable(seed: int) -> None:
    rng = random.Random(seed)
    curve = random_curve(rng)
    data = json.loads(json.dumps(to_dict(curve)))
    rebuilt = from_dict(type(curve), data)
    assert rebuilt == curve
    assert canonical_sha256(rebuilt) == canonical_sha256(curve)
    assert canonical_json_bytes(rebuilt) == canonical_json_bytes(curve)


def test_round_trip_of_evidence_and_conventions() -> None:
    now = datetime(2026, 1, 1, 12, tzinfo=UTC)
    evidence = SnapshotEvidence("snap", "source", now, now, now + timedelta(hours=1))
    assert from_dict(SnapshotEvidence, to_dict(evidence)) == evidence
    spread = ZSpreadConvention()
    assert from_dict(ZSpreadConvention, to_dict(spread)) == spread


def test_from_dict_is_strict() -> None:
    calendar = BusinessCalendar(version="x")
    data = to_dict(calendar)
    with pytest.raises(ValueError, match="fields do not match BusinessCalendar"):
        from_dict(BusinessCalendar, {**data, "extra": 1})
    with pytest.raises(ValueError, match="expected an ISO date"):
        from_dict(BusinessCalendar, {**data, "holidays": ["2026-01-01T00:00"]})
    with pytest.raises(ValueError, match="duplicate set members"):
        from_dict(BusinessCalendar, {**data, "holidays": ["2026-01-01", "2026-01-01"]})
    convention = to_dict(CurveConvention())
    with pytest.raises(ValueError, match="is not a DayCount"):
        from_dict(CurveConvention, {**convention, "time_day_count": "30/360"})
    now = to_dict(SnapshotEvidence("s", "s", *(datetime(2026, 1, 1, tzinfo=UTC),) * 3))
    with pytest.raises(ValueError, match="UTC offsets"):
        from_dict(SnapshotEvidence, {**now, "known_at": "2026-01-01T00:00:00"})


class Colour(Enum):
    RED = "red"


@dataclass(frozen=True)
class Leaf:
    value: Decimal


def test_canonical_forms_for_previously_unhashable_values() -> None:
    assert canonical_json_bytes({"c": Colour.RED}) == b'{"c":"red"}'
    assert canonical_json_bytes(frozenset({3, 1, 2})) == b"[1,2,3]"
    assert canonical_json_bytes({date(2026, 1, 2): 1}) == b'{"2026-01-02":1}'
    assert canonical_json_bytes({"leaf": Leaf(Decimal("1.50"))}) == b'{"leaf":{"value":"1.50"}}'
    # Set order never depends on construction order.
    left = BusinessCalendar(holidays=frozenset({date(2026, 1, 2), date(2026, 1, 5)}))
    right = BusinessCalendar(holidays=frozenset({date(2026, 1, 5), date(2026, 1, 2)}))
    assert canonical_sha256(left) == canonical_sha256(right)
    assert canonical_sha256(left) == (
        "39c5fdfc21c13530e51d3bb456e6fe57d35b8cef1376c89b956729304c341b88"
    )


def test_typed_rejections_are_value_errors_with_stable_codes() -> None:
    codes = {
        UnsupportedProduct: "UNSUPPORTED_PRODUCT",
        MissingMarketData: "MISSING_MARKET_DATA",
        CalendarRangeError: "CALENDAR_OUT_OF_RANGE",
        UnknownCurveSpec: "UNKNOWN_CURVE_SPEC",
        InvalidTerms: "INVALID_TERMS",
    }
    for kind, code in codes.items():
        error = kind("why")
        assert isinstance(error, CoreRejection)
        assert isinstance(error, ValueError)
        assert error.code == code
        assert error.message == "why"
