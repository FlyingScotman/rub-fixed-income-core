"""Typed rejections.

Core never returns a zero for something it cannot price: it raises one of these.
Each carries a stable machine-readable ``code`` that portfolio coverage records.
All derive from ``ValueError``, so callers that catch ``ValueError`` keep working.
"""

from __future__ import annotations

__all__ = [
    "CalendarRangeError",
    "CoreRejection",
    "InvalidTerms",
    "MissingMarketData",
    "UnknownCurveSpec",
    "UnsupportedProduct",
]


class CoreRejection(ValueError):
    """Base class of typed rejections; ``code`` is stable across releases."""

    code: str = "REJECTED"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class UnsupportedProduct(CoreRejection):
    """The product or one of its features is not modelled by this release."""

    code = "UNSUPPORTED_PRODUCT"


class MissingMarketData(CoreRejection):
    """A required curve, fixing, FX rate, calendar or quote is absent from the snapshot."""

    code = "MISSING_MARKET_DATA"


class CalendarRangeError(CoreRejection):
    """A date lies outside the calendar's covered range; it is never assumed Mon-Fri."""

    code = "CALENDAR_OUT_OF_RANGE"


class UnknownCurveSpec(CoreRejection):
    """The curve-spec ID is not in the registry."""

    code = "UNKNOWN_CURVE_SPEC"


class InvalidTerms(CoreRejection):
    """Instrument or position terms are inconsistent."""

    code = "INVALID_TERMS"
