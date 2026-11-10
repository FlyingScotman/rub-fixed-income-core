from __future__ import annotations

from typing import Any


def _fields_only_state(self: Any) -> dict[str, object]:
    """Pickle state without the lazily built caches kept beside the fields.

    Assign it as ``__getstate__`` on a frozen dataclass that memoises into its own
    ``__dict__``: pickles, ``copy`` and ``deepcopy`` then carry the fields only, and the
    cache is rebuilt on first use in the copy.
    """
    names = self.__dataclass_fields__
    return {k: v for k, v in self.__dict__.items() if k in names}
