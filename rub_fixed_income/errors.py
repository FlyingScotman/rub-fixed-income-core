"""Typed rejections.

Core never returns a zero for something it cannot price: it raises one of these.
Each carries a stable machine-readable ``code`` that portfolio coverage records.
All derive from ``ValueError``, so callers that catch ``ValueError`` keep working.
"""

from __future__ import annotations

__all__ = [
    "CalendarRangeError",
    "CoreRejection",
    "ExpiredContract",
    "InvalidTerms",
    "MissingAssumption",
    "MissingBasketPrice",
    "MissingFixing",
    "MissingMarketData",
    "SettledLeg",
    "UndeclaredFixingGap",
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


class MissingFixing(MissingMarketData, LookupError):
    """A required fixing (for example a RUONIA business day) is absent; it is never filled.

    Also a ``LookupError``, which is what the floating-coupon module raised before.
    """


class CalendarRangeError(CoreRejection):
    """A date lies outside the calendar's covered range; it is never assumed Mon-Fri."""

    code = "CALENDAR_OUT_OF_RANGE"


class UnknownCurveSpec(CoreRejection):
    """The curve-spec ID is not in the registry."""

    code = "UNKNOWN_CURVE_SPEC"


class InvalidTerms(CoreRejection):
    """Instrument or position terms are inconsistent."""

    code = "INVALID_TERMS"


class ExpiredContract(InvalidTerms):
    """A futures contract past its last trading or delivery day (portfolio v3)."""

    code = "EXPIRED_CONTRACT"


class SettledLeg(InvalidTerms):
    """An FX leg settled before the business date; only outstanding legs are valued."""

    code = "SETTLED_LEG"


class UndeclaredFixingGap(MissingFixing):
    """A RUONIA business day without a fixing that is not declared unpublished."""

    code = "UNDECLARED_FIXING_GAP"


class MissingBasketPrice(MissingMarketData):
    """A deliverable bond of an OFZ futures basket has no price in the snapshot."""

    code = "MISSING_BASKET_PRICE"


class MissingAssumption(InvalidTerms):
    """A required valuation assumption is absent; core never picks one.

    A pass-through without a prepayment assumption (core never picks a CPR), an open
    repo without an assumed end date.
    """

    code = "MISSING_ASSUMPTION"
