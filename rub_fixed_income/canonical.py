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
from datetime import date
from decimal import Decimal
from enum import Enum
from types import MappingProxyType
from typing import Any

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


def _dump(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


class _Canonical:
    """An already-canonical value (internal): emitted as is, so large stable parts such
    as curve nodes are converted once and reused across hashes. Bytes are unchanged."""

    __slots__ = ("value",)

    def __init__(self, value: object) -> None:
        self.value = value

    def __deepcopy__(self, memo: dict[int, object]) -> _Canonical:
        return self


def _precanonical(value: object) -> _Canonical:
    return _Canonical(_canonical_value(value))


def _canonical_value(value: object) -> object:
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
    return _dump(_canonical_value(value)).encode("utf-8")


def canonical_sha256(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()
