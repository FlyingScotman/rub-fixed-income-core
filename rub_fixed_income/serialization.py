"""``to_dict`` / ``from_dict`` for core's own frozen dataclasses.

``to_dict`` returns the canonical JSON-ready mapping, the same structure whose bytes
``canonical_sha256`` hashes. ``from_dict(cls, data)`` rebuilds a value from that
mapping, driven by the class's type hints, and re-runs its validation.

The mapping is strict: an object must carry exactly the dataclass's fields, enum
values must exist, timestamps must carry a UTC offset. This is **not** a parser of
market-data hub payloads; the backend adapter maps those to core types (ADR-003).
"""

from __future__ import annotations

import types
from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, Literal, TypeVar, Union, cast, get_args, get_origin, get_type_hints

from .canonical import _canonical_value

__all__ = ["from_dict", "to_dict"]

T = TypeVar("T")


def to_dict(value: object) -> dict[str, Any]:
    """Canonical JSON-ready mapping of a dataclass instance."""
    if not is_dataclass(value) or isinstance(value, type):
        raise TypeError("to_dict expects a dataclass instance")
    return cast(dict[str, Any], _canonical_value(value))


def from_dict(cls: type[T], data: Mapping[str, object]) -> T:
    """Rebuild ``cls`` from a mapping produced by :func:`to_dict` (or parsed JSON)."""
    if not (isinstance(cls, type) and is_dataclass(cls)):
        raise TypeError("from_dict expects a dataclass type")
    return cast(T, decode(cls, data, cls.__name__))


def _aware(value: datetime, path: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{path}: timestamps must have explicit UTC offsets")


def _decode_key(kind: Any, key: object, path: str) -> object:
    if kind in (str, Any, object):
        if not isinstance(key, str):
            raise ValueError(f"{path}: expected a string key")
        return key
    return decode(kind, key, path)


def decode(kind: Any, value: object, path: str) -> object:
    """Decode ``value`` as type ``kind``; ``path`` names the location in errors."""
    if kind is Any or kind is object:
        return value
    if kind is None or kind is type(None):
        if value is not None:
            raise ValueError(f"{path}: expected null")
        return None
    origin = get_origin(kind)
    if origin is Union or origin is types.UnionType:
        options = get_args(kind)
        if value is None and type(None) in options:
            return None
        candidates = [option for option in options if option is not type(None)]
        if len(candidates) == 1:
            return decode(candidates[0], value, path)
        decoded: list[object] = []
        errors: list[str] = []
        for option in candidates:
            try:
                decoded.append(decode(option, value, path))
            except (TypeError, ValueError) as exc:
                errors.append(str(exc))
        if len(decoded) != 1:
            detail = "ambiguous" if decoded else "; ".join(errors)
            raise ValueError(f"{path}: value does not match exactly one of {kind}: {detail}")
        return decoded[0]
    if origin is Literal:
        if value not in get_args(kind):
            raise ValueError(f"{path}: unexpected value {value!r}")
        return value
    if origin is tuple:
        if not isinstance(value, list):
            raise ValueError(f"{path}: expected a list")
        args = get_args(kind)
        if len(args) == 2 and args[1] is Ellipsis:
            return tuple(decode(args[0], item, f"{path}[{i}]") for i, item in enumerate(value))
        if len(args) != len(value):
            raise ValueError(f"{path}: expected {len(args)} items")
        return tuple(
            decode(arg, item, f"{path}[{i}]") for i, (arg, item) in enumerate(zip(args, value))
        )
    if origin in (frozenset, set):
        if not isinstance(value, list):
            raise ValueError(f"{path}: expected a list")
        (item_kind,) = get_args(kind)
        items = [decode(item_kind, item, f"{path}[{i}]") for i, item in enumerate(value)]
        if len(set(items)) != len(items):
            raise ValueError(f"{path}: duplicate set members")
        return frozenset(items) if origin is frozenset else set(items)
    if origin in (dict, Mapping) or (
        isinstance(origin, type) and issubclass(origin, Mapping)  # collections.abc.Mapping
    ):
        if not isinstance(value, dict):
            raise ValueError(f"{path}: expected an object")
        key_kind, item_kind = get_args(kind)
        return {
            _decode_key(key_kind, key, f"{path}.key"): decode(item_kind, item, f"{path}.{key}")
            for key, item in value.items()
        }
    if isinstance(kind, type) and is_dataclass(kind):
        return _decode_dataclass(kind, value, path)
    if isinstance(kind, type) and issubclass(kind, Enum):
        try:
            return kind(value)
        except ValueError as exc:
            raise ValueError(f"{path}: {value!r} is not a {kind.__name__}") from exc
    if kind is datetime:
        if not isinstance(value, str):
            raise ValueError(f"{path}: expected an ISO timestamp")
        parsed = datetime.fromisoformat(value)
        _aware(parsed, path)
        return parsed
    if kind is date:
        if not isinstance(value, str) or len(value) != 10:
            raise ValueError(f"{path}: expected an ISO date")
        return date.fromisoformat(value)
    if kind is Decimal:
        if not isinstance(value, str):
            raise ValueError(f"{path}: expected a decimal string")
        try:
            return Decimal(value)
        except InvalidOperation as exc:
            raise ValueError(f"{path}: invalid decimal {value!r}") from exc
    if kind is float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{path}: expected a number")
        return value
    if kind is bool:
        if not isinstance(value, bool):
            raise ValueError(f"{path}: expected a boolean")
        return value
    if kind in (int, str):
        if isinstance(value, bool) or not isinstance(value, kind):
            raise ValueError(f"{path}: expected {kind.__name__}")
        return value
    raise TypeError(f"{path}: unsupported field type {kind!r}")


def _decode_dataclass(kind: type, value: object, path: str) -> object:
    if not isinstance(value, dict):
        # Malformed data is a ValueError throughout the decoder, as for every other shape.
        raise ValueError(f"{path}: expected an object")  # noqa: TRY004
    hints = get_type_hints(kind)
    declared = fields(kind)
    names = {f.name for f in declared}
    if value.keys() != names:
        raise ValueError(f"{path}: fields do not match {kind.__name__}")
    init = {
        f.name: decode(hints[f.name], value[f.name], f"{path}.{f.name}") for f in declared if f.init
    }
    instance = kind(**init)
    for f in declared:
        if not f.init and getattr(instance, f.name) != decode(
            hints[f.name], value[f.name], f"{path}.{f.name}"
        ):
            raise ValueError(f"{path}.{f.name}: derived field does not match")
    return instance
