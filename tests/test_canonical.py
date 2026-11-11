"""The canonical walk and its fast paths give the bytes the plain recursive walk defines.

``_reference_value`` is that plain walk as written when the format was defined (every value
through one chain of ``isinstance`` checks); the library's walk answers common exact types
first and caches per-class field lists, and ``canonical_json_bytes`` copies the cached text of
a precomputed part into a mapping. None of it may change a byte, since the bytes are hashed
into identifiers consumers store.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable
from dataclasses import dataclass, field, fields, is_dataclass, replace
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import Enum, IntEnum
from typing import NamedTuple

import pytest

from rub_fixed_income import (
    PortfolioRiskResult,
    canonical_json_bytes,
    canonical_sha256,
    verify_portfolio_result,
    verify_portfolio_result_v3,
)
from rub_fixed_income.canonical import (
    OMIT_IF_DEFAULT,
    _Canonical,
    _canonical_value,
    _key,
    _precanonical,
    _sha256_of_decoded,
    is_omittable,
)
from rub_fixed_income.portfolio import (
    _result_sha256,
    portfolio_result_hash_projection,
    portfolio_result_to_json,
)


def _reference_dump(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _reference_value(value: object) -> object:
    if isinstance(value, _Canonical):
        return value.value
    if isinstance(value, Enum):
        return _reference_value(value.value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("canonical JSON forbids non-finite numbers")
        return value
    if isinstance(value, dict):
        return {_key(key): _reference_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_reference_value(item) for item in value]
    if isinstance(value, (set, frozenset)):
        items = [_reference_value(item) for item in value]
        return sorted(items, key=_reference_dump)
    if is_dataclass(value) and not isinstance(value, type):
        return {
            f.name: _reference_value(getattr(value, f.name))
            for f in fields(value)
            if not (is_omittable(f) and getattr(value, f.name) == f.default)
        }
    return value


def _reference_bytes(value: object) -> bytes:
    return _reference_dump(_reference_value(value)).encode("utf-8")


class Colour(Enum):
    RED = "red"
    BLUE = "blue"


class Level(IntEnum):
    LOW = 1
    HIGH = 2


class Pair(NamedTuple):
    left: float
    right: str


class FloatSub(float):
    pass


class StrSub(str):
    pass


class DictSub(dict):  # type: ignore[type-arg]
    pass


@dataclass(frozen=True)
class Leaf:
    name: str
    weight: float
    colour: Colour = Colour.RED
    tags: tuple[str, ...] = ()
    extra: int = field(default=0, metadata=OMIT_IF_DEFAULT)
    note: str | None = field(default=None, metadata=OMIT_IF_DEFAULT)


@dataclass(frozen=True)
class Tree:
    leaves: tuple[Leaf, ...]
    by_day: dict[date, Leaf]
    by_colour: dict[Colour, float]
    stamp: datetime
    level: Level = Level.HIGH
    ids: frozenset[int] = frozenset({3, 1, 2})
    amount: Decimal = Decimal("1.50")


@dataclass(frozen=True)
class DataclassDict(dict):  # type: ignore[type-arg]
    """A dataclass that is also a dict: the earlier branch of the chain takes it."""

    label: str = "x"


@dataclass
class Plain:
    a: int
    b: list[float]


def _values() -> list[object]:
    leaf = Leaf("a", 0.1, tags=("x", "y"))
    tree = Tree(
        (leaf, replace(leaf, name="b", extra=3, note="n", colour=Colour.BLUE)),
        {date(2026, 1, 2): leaf, date(2026, 1, 1): leaf},
        {Colour.BLUE: 1.5, Colour.RED: 2.5},
        datetime(2026, 1, 1, 12, 30, tzinfo=UTC),
    )
    return [
        None,
        True,
        False,
        0,
        -7,
        10**30,
        0.1,
        -0.0,
        1e-320,
        1e300,
        "",
        'text with ünïcode and \u0000 and "quotes"',
        date(2026, 2, 3),
        datetime(2026, 2, 3, 4, 5, 6),  # noqa: DTZ001
        datetime(2026, 2, 3, 4, 5, 6, 789, tzinfo=UTC),
        Decimal("12.3400"),
        Colour.RED,
        Level.LOW,
        (1, 2.5, "a"),
        [None, (1, (2, [3.5]))],
        {"b": 1, "a": [2, {"z": 1.5}]},
        {date(2026, 1, 1): 1},
        {Colour.BLUE: 2, Colour.RED: 1},
        {1: "one", 2: "two"},
        {"x": date(2026, 1, 1), "y": {Level.HIGH: Colour.BLUE}},
        {3, 1, 2},
        frozenset({"b", "a", "c"}),
        frozenset({(1, 2), (0, 5)}),
        Pair(1.5, "r"),
        FloatSub(2.5),
        StrSub("sub"),
        DictSub(a=1, b=2),
        DataclassDict(label="q"),
        leaf,
        tree,
        Plain(1, [0.5, 1.5]),
        [leaf, tree, {"k": leaf}],
        {"leaf": leaf, "pre": _precanonical({"nodes": [(date(2026, 1, 1), 0.5)]})},
        _precanonical([1, 2, {"a": 3.25}]),
        [_precanonical({"p": 1}), {"q": _precanonical([2])}],
        Leaf,  # a class object is returned as it is (and then fails to encode)
    ]


@pytest.mark.parametrize("index", range(len(_values())))
def test_the_walk_gives_the_reference_bytes(index: int) -> None:
    value = _values()[index]
    try:
        expected = _reference_bytes(value)
    except TypeError:
        with pytest.raises(TypeError):
            canonical_json_bytes(value)
        return
    for _ in range(2):  # the second round reads the per-class field cache
        assert canonical_json_bytes(value) == expected
    assert canonical_sha256(value) == hashlib.sha256(expected).hexdigest()
    assert json.dumps(_canonical_value(value), default=str) == json.dumps(
        _reference_value(value), default=str
    )


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
def test_non_finite_numbers_are_still_rejected_at_every_depth(bad: float) -> None:
    leaf = Leaf("a", 1.0)
    for value in (
        bad,
        [bad],
        {"x": bad},
        (1, [bad]),
        replace(leaf, weight=bad),
        {"l": [leaf, bad]},
    ):
        with pytest.raises(ValueError, match="non-finite"):
            canonical_json_bytes(value)
        with pytest.raises(ValueError, match="non-finite"):
            _reference_bytes(value)
    with pytest.raises(ValueError, match="non-finite"):
        canonical_json_bytes({"pre": _precanonical([1.0]), "x": bad})


def test_a_mapping_with_precomputed_parts_is_spliced_to_the_same_bytes() -> None:
    nodes = _precanonical({"nodes": [(date(2026, 1, 1), 0.987654321), (date(2027, 1, 1), 0.9)]})
    leaf = Leaf("a", 0.25, extra=1)
    cases: list[dict[str, object]] = [
        {"curve": nodes},
        {"z": 1, "curve": nodes, "a": leaf, "m": [1.5, nodes], "é": date(2026, 5, 5)},
        {"curve": nodes, "same": nodes, "n": None, "empty": {}, "list": []},
    ]
    for case in cases:
        for _ in range(2):  # the text is encoded on the first use and reused after
            assert canonical_json_bytes(case) == _reference_bytes(case)
    assert nodes.encoded == _reference_dump(nodes.value)
    # Non-string keys are not spliced: they take the plain walk, whose order and key text
    # (an enum's value, a date's ISO text) the reference gives.
    mixed: dict[object, object] = {Colour.RED: nodes, date(2026, 1, 1): 1}
    assert canonical_json_bytes(mixed) == _reference_bytes(mixed)
    # A shared part is not changed by being hashed in two different mappings.
    first = canonical_json_bytes({"curve": nodes, "a": 1})
    second = canonical_json_bytes({"curve": nodes, "a": 2})
    assert first != second
    assert canonical_json_bytes({"curve": nodes, "a": 1}) == first


def test_a_decoded_projection_is_hashed_as_the_canonical_walk_hashes_it() -> None:
    result = _v2_result()
    raw = json.loads(canonical_json_bytes(result))
    raw["result_sha256"] = ""
    assert _sha256_of_decoded(raw) == canonical_sha256(raw)
    assert _result_sha256(result) == canonical_sha256(portfolio_result_hash_projection(result))
    assert _result_sha256(result) == result.result_sha256


def _v2_result() -> PortfolioRiskResult:
    import test_portfolio

    return test_portfolio.run()


def _v3_result() -> PortfolioRiskResult:
    from portfolio_v3_book import pinned_v3_result

    return pinned_v3_result()


@pytest.mark.parametrize("make", [_v2_result, _v3_result])
def test_result_serialization_verifies_once_and_agrees_with_the_walk(
    make: Callable[[], PortfolioRiskResult],
) -> None:
    result = make()
    verify = (
        verify_portfolio_result_v3 if "v3" in result.schema_version else verify_portfolio_result
    )
    other = (
        verify_portfolio_result
        if verify is verify_portfolio_result_v3
        else verify_portfolio_result_v3
    )
    data = portfolio_result_to_json(result)
    assert data == _reference_bytes(result) == canonical_json_bytes(result)
    assert verify(result) and not other(result)
    assert result.result_sha256 == canonical_sha256(portfolio_result_hash_projection(result))
    for tampered in (
        replace(result, result_sha256="0" * 64),
        replace(result, covered_pv_rub=result.covered_pv_rub + 1.0),
    ):
        assert not verify(tampered)
        with pytest.raises(ValueError, match="does not verify"):
            portfolio_result_to_json(tampered)
    # The mapping form (what a consumer holds after json.loads) is unchanged.
    assert verify(json.loads(data))
    assert not verify({**json.loads(data), "result_sha256": "0" * 64})
    assert not verify({k: v for k, v in json.loads(data).items() if k != "result_sha256"})
