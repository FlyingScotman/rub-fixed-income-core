"""Deprecation notices for APIs kept until 1.0 (roadmap item 0.9)."""

from __future__ import annotations

import warnings


def deprecated(name: str, instead: str) -> None:
    """Warn at the caller of the deprecated API; nothing changes in its behaviour."""
    warnings.warn(
        f"rub_fixed_income: {name} is deprecated and will be removed in 1.0; {instead}",
        DeprecationWarning,
        stacklevel=3,
    )
