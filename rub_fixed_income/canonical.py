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
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, fields, is_dataclass
from datetime import date
from decimal import Decimal
from enum import Enum
from typing import Any, cast

__all__ = ["canonical_json_bytes", "canonical_sha256"]


def _key(value: object) -> object:
    if isinstance(value, Enum):
        return _key(value.value)
    if isinstance(value, date):
        return value.isoformat()
    return value


def _dump(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _canonical_value(value: object) -> object:
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
        return {f.name: _canonical_value(getattr(value, f.name)) for f in fields(value)}
    return value


def canonical_json_bytes(value: object) -> bytes:
    raw = asdict(cast(Any, value)) if hasattr(value, "__dataclass_fields__") else value
    return _dump(_canonical_value(raw)).encode("utf-8")


def canonical_sha256(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()
