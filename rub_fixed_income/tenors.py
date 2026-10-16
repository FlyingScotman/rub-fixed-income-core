"""Tenor codes to dates with a business calendar (roadmap 0.5).

Market data quotes swap and deposit rates by tenor code (``1W``, ``3M``, ``2Y``,
``ON``...). Core resolves a code to dates itself, so the backend passes codes and a
calendar, never hand-made maturities:

- ``ON`` (overnight): from the trade date to the next business day;
- ``TN`` (tom-next): from the next business day to the one after;
- ``SN`` (spot-next): from the spot date to the next business day;
- ``<n>D``, ``<n>W``, ``<n>M``, ``<n>Y``: from the spot date (``spot_lag_days`` business
  days after the trade date) by calendar days, weeks or months (month ends clamp, as
  :func:`rub_fixed_income.dates.add_months`); the end date is then adjusted with the
  business-day convention.

``TOM`` start (``spot_lag_days=1``) and modified following are the MOEX swap defaults
(KEYRATE IRS); each curve spec states its own.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Literal, cast

from .conventions import BusinessDayConvention, adjust_date
from .dates import BusinessCalendar, add_months
from .discount import OisQuote
from .errors import InvalidTerms

__all__ = ["Tenor", "TenorDates", "add_tenor", "ois_quotes", "parse_tenor", "resolve_tenor"]

TenorUnit = Literal["D", "W", "M", "Y", "ON", "TN", "SN"]
_PATTERN = re.compile(r"^(?:(ON|TN|SN)|([1-9][0-9]*)([DWMY]))$")


@dataclass(frozen=True)
class Tenor:
    """A parsed tenor code: ``count`` units, or one of the overnight codes (count 1)."""

    count: int
    unit: TenorUnit

    @property
    def code(self) -> str:
        return self.unit if self.unit in ("ON", "TN", "SN") else f"{self.count}{self.unit}"


@dataclass(frozen=True)
class TenorDates:
    trade_date: date
    tenor: str
    effective_date: date
    unadjusted_end: date
    end_date: date


def parse_tenor(code: str) -> Tenor:
    """Parse ``ON|TN|SN|<n>D|<n>W|<n>M|<n>Y`` (case and surrounding spaces ignored)."""
    if not isinstance(code, str):
        raise InvalidTerms("tenor must be a string")
    match = _PATTERN.match(code.strip().upper())
    if match is None:
        raise InvalidTerms(f"unsupported tenor: {code!r}")
    overnight, count, unit = match.groups()
    if overnight is not None:
        return Tenor(1, cast(TenorUnit, overnight))
    return Tenor(int(count), cast(TenorUnit, unit))


def add_tenor(start: date, tenor: str | Tenor) -> date:
    """Unadjusted end of a ``D/W/M/Y`` tenor from ``start``."""
    parsed = parse_tenor(tenor) if isinstance(tenor, str) else tenor
    if parsed.unit == "D":
        return start + timedelta(days=parsed.count)
    if parsed.unit == "W":
        return start + timedelta(weeks=parsed.count)
    if parsed.unit == "M":
        return add_months(start, parsed.count)
    if parsed.unit == "Y":
        return add_months(start, 12 * parsed.count)
    raise InvalidTerms(f"{parsed.code} is resolved with a calendar, not added to a date")


def resolve_tenor(
    trade_date: date,
    tenor: str,
    calendar: BusinessCalendar,
    *,
    spot_lag_days: int = 1,
    business_day_convention: BusinessDayConvention = BusinessDayConvention.MODIFIED_FOLLOWING,
) -> TenorDates:
    """Effective and end dates of ``tenor`` traded on ``trade_date``.

    Dates outside a bounded calendar raise :class:`CalendarRangeError`.
    """
    if isinstance(spot_lag_days, bool) or not isinstance(spot_lag_days, int) or spot_lag_days < 0:
        raise InvalidTerms("spot_lag_days must be a nonnegative integer")
    parsed = parse_tenor(tenor)
    spot = calendar.add_business_days(trade_date, spot_lag_days)
    if parsed.unit in ("ON", "TN", "SN"):
        start = {
            "ON": trade_date,
            "TN": calendar.add_business_days(trade_date, 1),
            "SN": spot,
        }[parsed.unit]
        end = calendar.add_business_days(start, 1)
        return TenorDates(trade_date, parsed.code, start, end, end)
    unadjusted = add_tenor(spot, parsed)
    end = adjust_date(unadjusted, business_day_convention, calendar)
    if end <= spot:
        raise InvalidTerms(f"{parsed.code} ends on or before its effective date")
    return TenorDates(trade_date, parsed.code, spot, unadjusted, end)


def ois_quotes(
    trade_date: date,
    rates: Iterable[tuple[str, float]],
    calendar: BusinessCalendar,
    *,
    spot_lag_days: int = 1,
    business_day_convention: BusinessDayConvention = BusinessDayConvention.MODIFIED_FOLLOWING,
) -> tuple[OisQuote, ...]:
    """``OisQuote``s with maturities resolved from tenor codes, sorted by maturity.

    Duplicate tenors, or two tenors resolving to the same maturity, are rejected.
    """
    quotes = []
    for tenor, rate in rates:
        dates = resolve_tenor(
            trade_date,
            tenor,
            calendar,
            spot_lag_days=spot_lag_days,
            business_day_convention=business_day_convention,
        )
        quotes.append(OisQuote(dates.tenor, dates.end_date, rate))
    if len({q.tenor for q in quotes}) != len(quotes):
        raise InvalidTerms("duplicate tenor codes")
    if len({q.maturity for q in quotes}) != len(quotes):
        raise InvalidTerms("two tenors resolve to the same maturity")
    return tuple(sorted(quotes, key=lambda q: q.maturity))
