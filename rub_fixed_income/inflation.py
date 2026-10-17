"""CPI-indexed nominal: OFZ-IN and other bonds whose face grows with a price index.

The OFZ-IN rule is fixed by the Minfin terms (order No. 80n of 22 May 2015, "Условия
эмиссии и обращения облигаций федерального займа с индексируемым номиналом"),
formulas (1)-(5) as restated by the Bank of Russia (methodological comment "Оценка
вмененной инфляции из цен на индексируемые на инфляцию облигации", January 2021,
section 3). For a calendar date ``i`` in month ``M``, the ``n``-th day of a month of
``d`` days:

- ``INDEX_i = CPI(M-4) + (CPI(M-3) - CPI(M-4)) * (n - 1) / d`` (the 3-month lag: the
  face grows during month ``M`` by the CPI change of month ``M - 3``);
- ``I_i = INDEX_i / INDEX_base`` with ``INDEX_base`` the same index on the placement
  date;
- nominal ``N_i = initial_face * I_i``, published in kopecks;
- coupon ``CF_m = N(payment date) * rate * D_m / 365``;
- accrued ``AI_i = N_i * rate * (i - t) / 365`` from the period start ``t``;
- redemption at the indexed nominal, never below the initial face (MOEX OFZ-IN page:
  "Минимальное значение номинала на дату погашения - 1000").

``CPI`` is the monthly consumer price index level of Rosstat ("индекс потребительских
цен ... к среднегодовым ценам 2000 г."); only ratios of levels matter. Core does no
I/O: the caller supplies a point-in-time :class:`CpiSeries` and every observation says
when it was published. Evidence and gaps: ``docs/linkers.md``.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from itertools import pairwise
from typing import Literal

from ._money import finite_money, positive_money
from .canonical import canonical_sha256
from .conventions import DayCount
from .errors import InvalidTerms, MissingMarketData

__all__ = [
    "INFLATION_MODEL_VERSION",
    "CpiIndexation",
    "CpiObservation",
    "CpiSeries",
    "IndexedCouponLeg",
    "IndexedCouponPeriod",
    "IndexedPrincipalLeg",
    "add_months",
    "indexed_accrued",
    "indexed_coupon_amount",
]

INFLATION_MODEL_VERSION = "rub-cpi-indexation-v1"
_KOPECK = Decimal("0.01")


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InvalidTerms(f"{label} must be a non-empty string")
    return value


def _month(value: date, label: str) -> date:
    if not isinstance(value, date) or value.day != 1:
        raise InvalidTerms(f"{label} must be the first day of a month")
    return value


def add_months(month: date, count: int) -> date:
    """The first day of the month ``count`` months after ``month`` (negative goes back)."""
    total = month.year * 12 + month.month - 1 + count
    return date(total // 12, total % 12 + 1, 1)


def _round(value: Decimal, quantum: Decimal | None) -> Decimal:
    return value if quantum is None else value.quantize(quantum, ROUND_HALF_UP)


@dataclass(frozen=True)
class CpiObservation:
    """The index level for ``month`` (its first day), published on ``published_on``."""

    month: date
    value: float
    published_on: date

    def __post_init__(self) -> None:
        _month(self.month, "CPI month")
        positive_money(self.value, "CPI level")
        if not isinstance(self.published_on, date):
            raise TypeError("published_on must be a date")
        if self.published_on < add_months(self.month, 1):
            raise InvalidTerms("a monthly CPI cannot be published before its month ends")


@dataclass(frozen=True)
class CpiSeries:
    """Monthly index levels known on ``as_of``: consecutive months, no gaps.

    Every observation must be published on or before ``as_of`` (no look-ahead); use
    :meth:`known_on` for an earlier cut. ``series_id`` keys the series in a snapshot and
    in :class:`CpiIndexation`; ``source_id`` names the caller's point-in-time selection.
    Levels are on any fixed base: indexation uses their ratios only.
    """

    series_id: str
    source_id: str
    as_of: date
    observations: tuple[CpiObservation, ...]

    def __post_init__(self) -> None:
        _text(self.series_id, "series_id")
        _text(self.source_id, "source_id")
        if "|" in self.series_id:
            raise InvalidTerms("series_id cannot contain '|'")
        if not isinstance(self.as_of, date):
            raise TypeError("as_of must be a date")
        rows = tuple(self.observations)
        if not rows:
            raise InvalidTerms("a CPI series needs at least one observation")
        for row in rows:
            if not isinstance(row, CpiObservation):
                raise TypeError("observations must be CpiObservation")
            if row.published_on > self.as_of:
                raise InvalidTerms(
                    f"CPI for {row.month:%Y-%m} was published after the series as-of date"
                )
        for left, right in pairwise(rows):
            if right.month != add_months(left.month, 1):
                raise InvalidTerms("CPI months must be consecutive and increasing")
        object.__setattr__(self, "observations", rows)

    @property
    def first_month(self) -> date:
        return self.observations[0].month

    @property
    def last_month(self) -> date:
        return self.observations[-1].month

    def level(self, month: date) -> float:
        """The level of ``month``; :class:`MissingMarketData` if it is not known."""
        # Lookup cached outside the dataclass fields (not part of equality or the hash).
        levels: dict[date, float] | None = self.__dict__.get("_level_cache")
        if levels is None:
            levels = {r.month: r.value for r in self.observations}
            object.__setattr__(self, "_level_cache", levels)
        value = levels.get(_month(month, "CPI month"))
        if value is None:
            raise MissingMarketData(
                f"CPI {self.series_id} for {month:%Y-%m} is not in the series "
                f"({self.first_month:%Y-%m}..{self.last_month:%Y-%m}, as of {self.as_of})"
            )
        return value

    @property
    def content_sha256(self) -> str:
        """``canonical_sha256`` of the series, computed once (it is immutable)."""
        cached: str | None = self.__dict__.get("_sha_cache")
        if cached is None:
            cached = canonical_sha256(self)
            object.__setattr__(self, "_sha_cache", cached)
        return cached

    def known_on(self, as_of: date) -> CpiSeries:
        """The series as it was known on ``as_of`` (observations published by then)."""
        if as_of > self.as_of:
            raise InvalidTerms("cannot extend a CPI series beyond its as-of date")
        rows = tuple(r for r in self.observations if r.published_on <= as_of)
        if not rows:
            raise MissingMarketData(f"no CPI {self.series_id} was published by {as_of}")
        return CpiSeries(self.series_id, self.source_id, as_of, rows)


@dataclass(frozen=True)
class CpiIndexation:
    """How a face is indexed: the reference index on a date over the base index.

    ``lag_months`` = 3 is the OFZ-IN rule: in month ``M`` the reference index moves
    linearly by calendar day from ``CPI(M - 4)`` on the 1st towards ``CPI(M - 3)``
    (``interpolation="daily_linear"``, ``(n - 1) / days in month``). ``base_index`` is
    the issue's ``INDEX_base`` when the terms publish it; ``None`` computes it from the
    series on ``base_date`` (the placement date), unrounded. The indexed nominal is
    rounded half-up to ``nominal_quantum`` (kopecks, as Minfin and MOEX publish it;
    ``None`` keeps it raw).
    """

    series_id: str
    base_date: date
    lag_months: int
    base_index: float | None = None
    interpolation: Literal["daily_linear"] = "daily_linear"
    nominal_quantum: Decimal | None = _KOPECK

    def __post_init__(self) -> None:
        _text(self.series_id, "series_id")
        if not isinstance(self.base_date, date):
            raise TypeError("base_date must be a date")
        if isinstance(self.lag_months, bool) or not isinstance(self.lag_months, int):
            raise TypeError("lag_months must be an int")
        if not 1 <= self.lag_months <= 12:
            raise InvalidTerms("lag_months must be between 1 and 12")
        if self.base_index is not None:
            positive_money(self.base_index, "base index")
        if self.interpolation != "daily_linear":
            raise InvalidTerms("interpolation must be daily_linear")
        if self.nominal_quantum is not None and (
            not isinstance(self.nominal_quantum, Decimal)
            or not self.nominal_quantum.is_finite()
            or self.nominal_quantum <= 0
        ):
            raise InvalidTerms("nominal_quantum must be a positive Decimal or None")

    @property
    def identifier(self) -> str:
        quantum = "raw" if self.nominal_quantum is None else str(self.nominal_quantum)
        return f"cpi:lag{self.lag_months}:{self.interpolation}:nominal-{quantum}"

    def reference_months(self, on: date) -> tuple[date, date]:
        """The two CPI months the reference index of ``on`` interpolates between."""
        month = date(on.year, on.month, 1)
        return add_months(month, -self.lag_months - 1), add_months(month, -self.lag_months)

    def reference_index(self, series: CpiSeries, on: date) -> Decimal:
        """``INDEX`` on ``on``: formula (3) of the terms, in exact decimal arithmetic."""
        self._check_series(series)
        start, end = self.reference_months(on)
        low = Decimal(repr(series.level(start)))
        high = Decimal(repr(series.level(end)))
        days = calendar.monthrange(on.year, on.month)[1]
        return low + (high - low) * (on.day - 1) / days

    def base(self, series: CpiSeries) -> Decimal:
        if self.base_index is not None:
            return Decimal(repr(self.base_index))
        return self.reference_index(series, self.base_date)

    def index_ratio(self, series: CpiSeries, on: date) -> Decimal:
        """``I = INDEX(on) / INDEX_base``; raises before the base date."""
        if on < self.base_date:
            raise InvalidTerms("the indexed nominal starts on the base (placement) date")
        return self.reference_index(series, on) / self.base(series)

    def nominal(self, initial_face: float, series: CpiSeries, on: date) -> float:
        """Indexed face per bond on ``on``, rounded to ``nominal_quantum``."""
        face = Decimal(repr(positive_money(initial_face, "initial face")))
        raw = face * self.index_ratio(series, on)
        return float(_round(raw, self.nominal_quantum))

    def _check_series(self, series: CpiSeries) -> None:
        if not isinstance(series, CpiSeries):
            raise TypeError("series must be a CpiSeries")
        if series.series_id != self.series_id:
            raise InvalidTerms(f"the bond indexes to {self.series_id}, not to {series.series_id}")


def _year_days(day_count: DayCount) -> int:
    if day_count is DayCount.ACT_365_FIXED:
        return 365
    if day_count is DayCount.ACT_360:
        return 360
    raise InvalidTerms("an indexed coupon leg accrues ACT/365F or ACT/360")


@dataclass(frozen=True)
class IndexedCouponPeriod:
    """A coupon at a fixed (real) ``rate`` on the nominal indexed to ``payment_date``."""

    accrual_start: date
    accrual_end: date
    payment_date: date
    rate: float

    def __post_init__(self) -> None:
        finite_money(self.rate, "coupon rate")
        if self.rate < 0.0:
            raise InvalidTerms("coupon rate cannot be negative")
        if self.accrual_end <= self.accrual_start:
            raise InvalidTerms("coupon accrual period must have positive length")
        if self.payment_date < self.accrual_end:
            raise InvalidTerms("a coupon cannot be paid before its accrual period ends")


@dataclass(frozen=True)
class IndexedCouponLeg:
    """Coupons on the indexed nominal (OFZ-IN formulas 4 and 5).

    The coupon is ``N(payment) * rate * days / year`` and the accrued interest
    ``N(date) * rate * elapsed / year``, each rounded half-up to 0.01; ``year`` is 365
    for ``ACT_365_FIXED`` (OFZ-IN) or 360 for ``ACT_360``.
    """

    periods: tuple[IndexedCouponPeriod, ...]
    day_count: DayCount

    def __post_init__(self) -> None:
        object.__setattr__(self, "periods", tuple(self.periods))
        if not isinstance(self.day_count, DayCount):
            raise TypeError("day_count must be a DayCount")
        _year_days(self.day_count)
        if not self.periods:
            raise InvalidTerms("an indexed coupon leg needs at least one period")
        for left, right in pairwise(self.periods):
            if right.accrual_start < left.accrual_end:
                raise InvalidTerms("coupon periods must be ordered and must not overlap")
            if right.payment_date <= left.payment_date:
                raise InvalidTerms("coupon payment dates must increase")

    def period_on(self, on: date) -> IndexedCouponPeriod | None:
        """The period whose ``[accrual_start, accrual_end)`` contains ``on``."""
        for period in self.periods:
            if period.accrual_start <= on < period.accrual_end:
                return period
        return None


@dataclass(frozen=True)
class IndexedPrincipalLeg:
    """A bullet redemption of the indexed nominal on ``maturity_date``.

    ``floor_at_initial_face`` redeems at no less than the initial face (OFZ-IN).
    """

    maturity_date: date
    indexation: CpiIndexation
    floor_at_initial_face: bool

    def __post_init__(self) -> None:
        if not isinstance(self.maturity_date, date):
            raise TypeError("maturity_date must be a date")
        if not isinstance(self.indexation, CpiIndexation):
            raise TypeError("indexation must be a CpiIndexation")
        if not isinstance(self.floor_at_initial_face, bool):
            raise TypeError("floor_at_initial_face must be boolean")
        if self.maturity_date <= self.indexation.base_date:
            raise InvalidTerms("maturity must follow the indexation base date")

    def redemption(self, initial_face: float, nominal_at_maturity: float) -> float:
        """Principal paid at maturity for a given indexed nominal on that date."""
        if self.floor_at_initial_face:
            return max(nominal_at_maturity, initial_face)
        return nominal_at_maturity


def indexed_coupon_amount(
    nominal: float, period: IndexedCouponPeriod, day_count: DayCount
) -> float:
    """Formula (4): ``nominal * rate * period days / year``, rounded half-up to 0.01."""
    days = (period.accrual_end - period.accrual_start).days
    raw = (
        Decimal(repr(positive_money(nominal, "nominal")))
        * Decimal(repr(period.rate))
        * days
        / _year_days(day_count)
    )
    return float(_round(raw, _KOPECK))


def indexed_accrued(
    nominal: float, period: IndexedCouponPeriod, on: date, day_count: DayCount
) -> float:
    """Formula (5): ``nominal * rate * (on - start) / year``, rounded half-up to 0.01."""
    if not period.accrual_start <= on < period.accrual_end:
        raise InvalidTerms("the date is outside the coupon accrual period")
    raw = (
        Decimal(repr(positive_money(nominal, "nominal")))
        * Decimal(repr(period.rate))
        * (on - period.accrual_start).days
        / _year_days(day_count)
    )
    return float(_round(raw, _KOPECK))
