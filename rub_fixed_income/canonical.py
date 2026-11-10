"""Canonical JSON bytes and SHA-256 identities for core values.

Every identifier the library emits (result hashes, ``base_id``, ``content_id``,
``job_id``) is ``sha256`` over these bytes: sorted keys, compact separators,
UTF-8, dates as ISO strings, exact float ``repr``, non-finite numbers rejected.

The bytes of every value that serialized before 0.9.0 are unchanged. 0.9.0 adds
canonical forms for values that used to raise ``TypeError``:

- an ``Enum`` member serializes as its ``value``;
- a ``set``/``frozenset`` serializes as a list sorted by each element's canonical bytes;
- a dataclass nested inside a mapping or a sequence serializes as its field mapping;
- ``date`` and ``Enum`` mapping keys serialize as their ISO string or value.

Fields added to an existing type after its first release are declared with
``field(default=..., metadata=OMIT_IF_DEFAULT)``: they are left out of the canonical
mapping while they hold their (immutable) default, so every value that does not use them
keeps its earlier bytes and hash; ``from_dict`` then accepts them as absent.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import Field, fields, is_dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from types import MappingProxyType
from typing import Any, cast

__all__ = ["OMIT_IF_DEFAULT", "canonical_json_bytes", "canonical_sha256"]

_OMIT_KEY = "rub_fixed_income.omit_if_default"
OMIT_IF_DEFAULT = MappingProxyType({_OMIT_KEY: True})
"""Field metadata: leave the field out of canonical bytes while it holds its default."""


def is_omittable(spec: Field[Any]) -> bool:
    return bool(spec.metadata.get(_OMIT_KEY))


def _kept(spec: Field[Any], value: object) -> bool:
    return not (is_omittable(spec) and getattr(value, spec.name) == spec.default)


def _key(value: object) -> object:
    if isinstance(value, Enum):
        return _key(value.value)
    if isinstance(value, date):
        return value.isoformat()
    return value


_ENCODER = json.JSONEncoder(sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _dump(value: object) -> str:
    return _ENCODER.encode(value)


class _Canonical:
    """An already-canonical value (internal): emitted as is, so large stable parts such
    as curve nodes are converted once and reused across hashes. Bytes are unchanged."""

    __slots__ = ("_encoded", "value")

    def __init__(self, value: object) -> None:
        self.value = value
        self._encoded: str | None = None

    def __deepcopy__(self, memo: dict[int, object]) -> _Canonical:
        return self

    @property
    def encoded(self) -> str:
        """The JSON text of ``value``, encoded once: a curve's text is the bulk of every
        position's identity, and the encoder (float ``repr``) is most of the cost of a hash."""
        text = self._encoded
        if text is None:
            text = self._encoded = _dump(self.value)
        return text


def _precanonical(value: object) -> _Canonical:
    return _Canonical(_canonical_value(value))


_ATOMIC = frozenset({str, int, bool, type(None)})
_ISO_TYPES = frozenset({date, datetime})
_SEQUENCE_TYPES = frozenset({list, tuple})
# Per exact dataclass type: its fields, each with the spec when it is omittable. Filled only
# for a type that no earlier branch of the generic chain below would have claimed.
# Unbounded by design: it has one entry per dataclass type, and those classes are created
# statically at import, never per call or per input, so the table cannot grow with the data.
_DATACLASS_FIELDS: dict[type, tuple[tuple[str, Field[Any] | None], ...]] = {}
_NOT_DATACLASS = (_Canonical, Enum, date, Decimal, float, dict, list, tuple, set, frozenset)


def _canonical_value(value: object) -> object:
    # The common exact types are handled first; anything else (a subclass, an Enum, a Decimal,
    # a set, ...) takes the generic chain, whose results are the bytes' definition.
    kind = type(value)
    if kind in _ATOMIC:
        return value
    if kind is float:
        if not math.isfinite(cast(float, value)):
            raise ValueError("canonical JSON forbids non-finite numbers")
        return value
    if kind in _SEQUENCE_TYPES:
        return [_canonical_value(item) for item in cast("list[object]", value)]
    if kind is dict:
        return {
            _key(key): _canonical_value(item)
            for key, item in cast("dict[object, object]", value).items()
        }
    if kind in _ISO_TYPES:
        return cast(date, value).isoformat()
    specs = _DATACLASS_FIELDS.get(kind)
    if specs is not None:
        return _dataclass_value(value, specs)
    if (
        not isinstance(value, _NOT_DATACLASS)
        and is_dataclass(value)
        and not isinstance(value, type)
    ):
        specs = tuple((f.name, f if is_omittable(f) else None) for f in fields(value))
        _DATACLASS_FIELDS[kind] = specs
        return _dataclass_value(value, specs)
    return _canonical_generic(value)


def _dataclass_value(
    value: object, specs: tuple[tuple[str, Field[Any] | None], ...]
) -> dict[str, object]:
    result: dict[str, object] = {}
    for name, omittable in specs:
        item = getattr(value, name)
        if omittable is not None and item == omittable.default:
            continue
        kind = type(item)
        if kind in _ATOMIC or (kind is float and math.isfinite(cast(float, item))):
            result[name] = item
        else:
            result[name] = _canonical_value(item)
    return result


def _canonical_generic(value: object) -> object:
    if isinstance(value, _Canonical):
        return value.value
    if isinstance(value, Enum):
        return _canonical_value(value.value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("canonical JSON forbids non-finite numbers")
        return value
    if isinstance(value, dict):
        return {_key(key): _canonical_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item) for item in value]
    if isinstance(value, (set, frozenset)):
        items = [_canonical_value(item) for item in value]
        return sorted(items, key=_dump)
    if is_dataclass(value) and not isinstance(value, type):
        return {
            f.name: _canonical_value(getattr(value, f.name))
            for f in fields(value)
            if _kept(f, value)
        }
    return value


def canonical_json_bytes(value: object) -> bytes:
    # A dataclass is converted field by field (the bytes equal those of ``dataclasses.asdict``,
    # used before) so that omittable fields can be left out at every level.
    if type(value) is dict:
        spliced = _spliced_bytes(cast("dict[object, object]", value))
        if spliced is not None:
            return spliced
    return _dump(_canonical_value(value)).encode("utf-8")


def _spliced_bytes(value: dict[object, object]) -> bytes | None:
    """The canonical bytes of a mapping that holds already-canonical values (``_Canonical``),
    which are copied in as their cached text instead of being encoded again.

    The encoder writes a mapping as its sorted ``"key":value`` pairs between braces, each value
    exactly as it would be written alone, so the text is the encoder's. None (the caller
    encodes it as usual) when no value is ``_Canonical`` or a key is not a string.
    """
    if not any(type(item) is _Canonical for item in value.values()):
        return None
    if any(type(key) is not str for key in value):
        return None
    parts = []
    for key in sorted(cast("list[str]", value)):
        item = value[key]
        text = item.encoded if type(item) is _Canonical else _dump(_canonical_value(item))
        parts.append(f"{_dump(key)}:{text}")
    return ("{" + ",".join(parts) + "}").encode("utf-8")


def canonical_sha256(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _sha256_of_decoded(decoded: object) -> str:
    """``canonical_sha256`` of a value ``json.loads`` decoded from canonical bytes.

    Such a value (dicts with string keys, lists, strings, numbers, booleans, None) is already
    its own canonical form, so its bytes come straight from the encoder without a second walk.
    Not for a value from anywhere else: it would skip the checks and conversions of the walk.
    """
    return hashlib.sha256(_dump(decoded).encode("utf-8")).hexdigest()
