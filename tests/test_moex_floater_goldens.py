"""Golden tests: MOEX-published floater coupons from CBR fixings (roadmap package 1).

Fixtures and provenance: ``tests/fixtures/floaters/README.md``. Fixings are raw CBR
DailyInfo responses (RUONIA, key rate, term RUONIA); coupons are MOEX ISS bondization
and board data fetched on 2026-10-17. Each coupon is rebuilt with ``FixingHistory`` and
``FloatingLeg`` and must equal the exchange's figure to the kopeck.

The observation rules are identified from the data, not from issue-terms text (the
Minfin and issuer documents are not reachable from the build environment). A rule is
kept only when it reproduces every checked coupon of the bond exactly and its
neighbours (other lags, averaging, a business-day lookback, other day counts) do not;
see ``docs/release-notes/floater-goldens.md``.

Most bonds observe a **calendar-day shifted window**: the coupon for ``[start, end)``
averages the fixing in force on each day of ``[start - k, end - k)``, with day weights
and year fraction taken over that window (``ObservationShift(k, "calendar_days",
"observation")``). In-advance coupons observe a window that ends before the coupon
starts and accrue over the coupon period (``ObservationWindow``). Every coupon is
projected on its real accrual dates and payment date.
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from calendar import monthrange
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from functools import cache
from pathlib import Path
from typing import Any

import pytest

from rub_fixed_income import (
    BusinessCalendar,
    DayCount,
    FixingHistory,
    FloatingCouponPeriod,
    FloatingCouponProjection,
    FloatingCouponRule,
    FloatingLeg,
    ObservationShift,
    ObservationWindow,
)
from rub_fixed_income.floating import DayUnit, FixingIndex, IndexAveraging

FIXTURES = Path(__file__).parent / "fixtures" / "floaters"
VALUATION = date(2026, 10, 17)  # fixings dated before it; RUONIA is published to 2026-10-15
FIRST_FIXING = date(2023, 11, 1)
LAST_CALENDAR_DAY = date(2026, 10, 16)
ACT365 = DayCount.ACT_365_FIXED
ACT_ACT = DayCount.ACT_ACT_ISDA


# --- fixture parsing (test side only: the library never parses payloads) ------------


def _xml_rows(name: str, row_tag: str) -> list[dict[str, str]]:
    root = ET.parse(FIXTURES / "cbr" / name).getroot()
    return [{child.tag: child.text or "" for child in row} for row in root.iter(row_tag)]


def _day(text: str) -> date:
    return date.fromisoformat(text[:10])


@cache
def ruonia() -> dict[date, tuple[float, datetime]]:
    """RUONIA by rate date: (decimal rate, publication time)."""
    return {
        _day(row["D0"]): (float(row["ruo"]) / 100.0, datetime.fromisoformat(row["DateUpdate"]))
        for row in _xml_rows("ruonia_2023-11-01_2026-10-16.xml", "ro")
    }


@cache
def key_rate() -> dict[date, float]:
    return {
        _day(row["DT"]): float(row["Rate"]) / 100.0
        for row in _xml_rows("keyrate_2023-11-01_2026-10-16.xml", "KR")
    }


@cache
def term_ruonia_3m() -> dict[date, float]:
    """CBR term RUONIA 3M (``RUONIA_AVG_3M``) by date, decimal."""
    return {
        _day(row["DT"]): float(row["RUONIA_AVG_3M"]) / 100.0
        for row in _xml_rows("ruonia_sv_2025-06-01_2026-10-16.xml", "ra")
    }


@cache
def calendar() -> BusinessCalendar:
    """Business days are the RUONIA rate dates: CBR publishes RUONIA for each of them.

    Weekdays without RUONIA are holidays and weekend days with RUONIA are transferred
    working Saturdays (``test_fixings_and_publication_calendar`` checks some).
    """
    published = set(ruonia())
    holidays, working = set(), set()
    day = FIRST_FIXING
    while day <= max(published):
        weekend = day.weekday() >= 5
        if weekend and day in published:
            working.add(day)
        if not weekend and day not in published:
            holidays.add(day)
        day += timedelta(days=1)
    return BusinessCalendar(
        frozenset(holidays),
        frozenset(working),
        "cbr-ruonia-dates-2023-11-01-2026-10-15",
        valid_from=FIRST_FIXING,
        valid_to=LAST_CALENDAR_DAY,
    )


@cache
def history(index: FixingIndex) -> FixingHistory:
    if index == "ruonia":
        rows = ruonia()
        fixings = tuple((day, rows[day][0]) for day in sorted(rows))
        known = max(published for _, published in rows.values())
        name = "RuoniaXML"
    else:
        fixings = tuple(sorted(key_rate().items()))
        known = datetime(2026, 10, 17, 13, 56, tzinfo=UTC)  # fetch time; no publication stamp
        name = "KeyRateXML"
    return FixingHistory(
        index,
        VALUATION,
        fixings,
        calendar(),
        f"cbr-dailyinfo-{name}-2023-11-01-2026-10-16",
        known_at_utc=known.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )


def _rows(block: dict[str, Any]) -> list[dict[str, Any]]:
    return [dict(zip(block["columns"], row, strict=True)) for row in block["data"]]


def _iss(path: str) -> dict[str, Any]:
    return dict(json.loads((FIXTURES / "iss" / path).read_text("utf-8")))


def description(secid: str) -> dict[str, str]:
    return {
        row["name"]: row["value"] for row in _rows(_iss(f"description/{secid}.json")["description"])
    }


@dataclass(frozen=True)
class Coupon:
    start: date
    end: date
    face: float
    value: float | None  # None: not yet fixed in bondization
    rate_pct: float | None


def coupons(secid: str) -> list[Coupon]:
    return [
        Coupon(
            date.fromisoformat(row["startdate"]),
            date.fromisoformat(row["coupondate"]),
            float(row["facevalue"]),
            float(row["value"]) if row["value"] else None,
            row["valueprc"],
        )
        for row in _rows(_iss(f"bondization/{secid}.json")["coupons"])
    ]


def board_rows() -> list[dict[str, Any]]:
    return [
        row for name in ("TQOB", "TQCB") for row in _rows(_iss(f"board_{name}.json")["securities"])
    ]


def round_half_up(value: float, decimals: int = 2) -> float:
    return float(Decimal(str(value)).quantize(Decimal(1).scaleb(-decimals), ROUND_HALF_UP))


# --- coupon conventions -------------------------------------------------------------


@dataclass(frozen=True)
class Convention:
    """How a bond's coupon observes its index (see the module docstring).

    ``shift_days`` moves the observation window back (calendar days unless
    ``shift_unit`` says otherwise). In advance, the window has the coupon's length and
    ends ``shift_days`` before the coupon starts.
    """

    index: FixingIndex
    averaging: IndexAveraging
    shift_days: int
    day_count: DayCount = ACT365
    in_advance: bool = False
    lookback_business_days: int = 0
    rate_rounding_decimals: int | None = None
    shift_unit: DayUnit = "calendar_days"

    def rule(self, margin: float) -> FloatingCouponRule:
        shift = window = None
        if self.shift_days and self.in_advance:
            window = ObservationWindow(self.shift_days, self.shift_unit)
        elif self.shift_days:
            shift = ObservationShift(self.shift_days, self.shift_unit, "observation")
        return FloatingCouponRule(
            self.index,
            self.averaging,
            margin,
            self.day_count,
            self.day_count,
            lookback_business_days=self.lookback_business_days,
            rate_rounding_decimals=self.rate_rounding_decimals,
            amount_rounding_decimals=2,
            observation_shift=shift,
            observation_window=window,
        )

    def window(self, start: date, end: date) -> tuple[date, date]:
        """The calendar-day observation window (used to keep fixtures in range)."""
        shift = timedelta(days=self.shift_days)
        if self.in_advance:
            return start - shift - (end - start), start - shift
        return start - shift, end - shift


# OFZ-PK 29013-29025: arithmetic RUONIA over the period shifted 6 days, no margin; the
# unrounded rate times the window's ACT/ACT (coupons in 2020 and 2024 need 366 days).
MINFIN_RUONIA = Convention("ruonia", "arithmetic", 6, ACT_ACT)
# Corporate RUONIA floaters on the same formula plus a margin (2025-2026 only, so the
# leap-year basis is not confirmed: ACT/365F and ACT/ACT give the same amounts).
RUONIA_SHIFT6 = Convention("ruonia", "arithmetic", 6)
# Monthly coupons set in advance on the previous window; rate rounded to 0.01 %.
RUONIA_ADVANCE6 = Convention("ruonia", "arithmetic", 6, in_advance=True, rate_rounding_decimals=2)
# Compounded RUONIA over the period shifted 7 days: ISS "Индекс RUONIA" (VEB.RF) and
# OFZ-PK 29026-29028 "Срочная ставка RUONIA 3m" (see test_term_ruonia_is_the_shifted_period).
RUONIA_COMPOUNDED7 = Convention("ruonia", "compounded", 7)
KEY_SHIFT6 = Convention("key_rate", "arithmetic", 6)
KEY_SHIFT4 = Convention("key_rate", "arithmetic", 4)
# Native FloatingLeg lookback: each accrual day observes the key rate 4 business days earlier.
KEY_LOOKBACK4 = Convention("key_rate", "arithmetic", 0, lookback_business_days=4)

BONDS: dict[str, Convention] = {
    "SU29013RMFS8": MINFIN_RUONIA,
    "SU29015RMFS3": MINFIN_RUONIA,
    "SU29017RMFS9": MINFIN_RUONIA,
    "SU29022RMFS9": MINFIN_RUONIA,
    "SU29025RMFS2": MINFIN_RUONIA,
    "SU29026RMFS0": RUONIA_COMPOUNDED7,
    "SU29027RMFS8": RUONIA_COMPOUNDED7,
    "SU29028RMFS6": RUONIA_COMPOUNDED7,
    "RU000A1093L2": RUONIA_SHIFT6,  # VEB.RF, 14-day coupons, no margin
    "RU000A10ACF3": RUONIA_SHIFT6,  # VEB.RF, monthly, +2.00
    "RU000A10ABH1": RUONIA_SHIFT6,  # VTB, monthly, +2.80
    "RU000A1042W6": RUONIA_SHIFT6,  # VEB.RF, quarterly, +1.55
    "RU000A106K43": RUONIA_ADVANCE6,  # Russian Railways, monthly, +1.30
    "RU000A108447": RUONIA_ADVANCE6,  # Rosagroleasing, monthly, +1.80
    "RU000A10B8D9": RUONIA_COMPOUNDED7,  # VEB.RF, quarterly, +2.30
    "RU000A10C6S0": RUONIA_COMPOUNDED7,  # VEB.RF, quarterly, +1.75
    "RU000A10A4S7": KEY_SHIFT6,  # monthly, +2.00
    "RU000A109JY6": KEY_SHIFT6,  # monthly, +2.50
    "RU000A10C0V7": KEY_SHIFT4,  # monthly, +3.00
    "RU000A10EDW8": KEY_LOOKBACK4,  # monthly, +1.35
    "RU000A1079S6": KEY_LOOKBACK4,  # quarterly, +1.20
}
# The first coupons of the December 2024 issues are not reproduced (known gap).
FIRST_COUPON_GAPS = {
    ("SU29026RMFS0", date(2024, 12, 4)): (52.66, 52.72),
    ("SU29027RMFS8", date(2024, 12, 11)): (52.71, 52.75),
}
# ISS COUPON_BENCHMARK per index; the margin is COUPON_BENCHMARK_SPREAD in percent.
BENCHMARKS: dict[FixingIndex, set[str]] = {
    "ruonia": {"RUONIA", "RUONIA_IND", "RUONIA3M"},
    "key_rate": {"RREFKEYR"},
}


def margin(secid: str) -> float:
    fields = description(secid)
    benchmark = fields["COUPON_BENCHMARK"]
    index = "ruonia" if secid in MINFIN_ADVANCE else BONDS[secid].index
    assert benchmark in BENCHMARKS[index]
    return float(fields["COUPON_BENCHMARK_SPREAD"]) / 100.0


def core_coupon(
    secid: str, start: date, end: date, face: float, convention: Convention | None = None
) -> FloatingCouponProjection:
    convention = convention or BONDS[secid]
    period = FloatingCouponPeriod(start, end, end, face)
    leg = FloatingLeg(convention.rule(margin(secid)), (period,), history(convention.index))
    (projection,) = leg.project(None, settlement_date=start)
    assert (projection.accrual_start, projection.accrual_end) == (start, end)
    return projection


def in_fixture_window(start: date, end: date) -> bool:
    return start - timedelta(days=10) >= FIRST_FIXING and end <= max(ruonia()) + timedelta(days=1)


def fixed_coupons(secid: str) -> list[Coupon]:
    """Published coupons whose observation window lies inside the fixture's fixings."""
    out = []
    for coupon in coupons(secid):
        if coupon.value is None or coupon.start > VALUATION:
            continue  # MOEX repeats the current rate in future in-advance rows
        if (secid, coupon.start) in FIRST_COUPON_GAPS:
            continue
        if in_fixture_window(*BONDS[secid].window(coupon.start, coupon.end)):
            out.append(coupon)
    return out


# Old OFZ-PK 29007-29010: semiannual, set in advance. The rate is the arithmetic mean of
# RUONIA over [d - 6 months, d), d the second business day before the coupon starts, plus
# the margin, rounded to 0.01 %; the amount is face * rate * days / 365.
MINFIN_ADVANCE = ("SU29007RMFS0", "SU29008RMFS8", "SU29009RMFS6", "SU29010RMFS4")
MINFIN_ADVANCE_WINDOW = ObservationWindow(2, "business_days", length_months=6)


def _months_before(day: date, months: int) -> date:
    year, month = divmod(day.year * 12 + day.month - 1 - months, 12)
    return date(year, month + 1, min(day.day, monthrange(year, month + 1)[1]))


def minfin_advance_window(start: date) -> tuple[date, date]:
    end, found = start, 0
    while found < 2:
        end -= timedelta(days=1)
        found += calendar().is_business_day(end)
    return _months_before(end, 6), end


def minfin_advance_coupon(
    secid: str, start: date, end: date, face: float
) -> FloatingCouponProjection:
    rule = FloatingCouponRule(
        "ruonia",
        "arithmetic",
        margin(secid),
        ACT365,
        ACT365,
        rate_rounding_decimals=2,
        amount_rounding_decimals=2,
        observation_window=MINFIN_ADVANCE_WINDOW,
    )
    period = FloatingCouponPeriod(start, end, end, face)
    (projection,) = FloatingLeg(rule, (period,), history("ruonia")).project(
        None, settlement_date=start
    )
    return projection


def advance_amount(rate: float, face: float, start: date, end: date) -> float:
    """Independent check of the in-advance amount: face * rate * days / 365, half-up."""
    return round_half_up(face * rate * (end - start).days / 365.0)


# --- tests --------------------------------------------------------------------------


def test_fixings_and_publication_calendar() -> None:
    cal = calendar()
    assert len(ruonia()) == 728 and min(ruonia()) == FIRST_FIXING
    assert max(ruonia()) == date(2026, 10, 15)
    assert ruonia()[date(2026, 10, 15)][0] == pytest.approx(0.1391)
    assert key_rate()[date(2026, 10, 16)] == pytest.approx(0.14)
    # Official non-working days and transferred working Saturdays of 2024-2026.
    for holiday in (date(2024, 4, 29), date(2024, 12, 30), date(2025, 5, 2), date(2025, 11, 3)):
        assert not cal.is_business_day(holiday)
    for working_saturday in (date(2024, 4, 27), date(2024, 11, 2), date(2025, 11, 1)):
        assert cal.is_business_day(working_saturday)
    assert all(not cal.is_business_day(date(2026, 1, d)) for d in range(1, 9))
    # RUONIA is not computed on the last business day of the year (the working Saturday
    # 2024-12-28 included); the coupons carry the previous fixing over it, so the RUONIA
    # publication calendar, not the official one, is the history's calendar.
    for year_end in (date(2023, 12, 29), date(2024, 12, 28), date(2025, 12, 30)):
        assert not cal.is_business_day(year_end) and year_end in key_rate()
    # RUONIA for day T is published on a later business day (no look-ahead).
    assert all(published.date() > day for day, (_, published) in ruonia().items())


@pytest.mark.parametrize("secid", sorted(BONDS))
def test_published_coupons_reproduced_to_the_kopeck(secid: str) -> None:
    checked = fixed_coupons(secid)
    assert len(checked) >= 4, "too few fixed coupons in the fixture window"
    for coupon in checked:
        projection = core_coupon(secid, coupon.start, coupon.end, coupon.face)
        assert projection.source == "fixed_by_history"
        assert projection.amount_rub == pytest.approx(coupon.value, abs=1e-9), (
            secid,
            coupon.start,
        )
        if BONDS[secid].rate_rounding_decimals is not None:
            assert projection.coupon_rate is not None
            assert projection.coupon_rate * 100.0 == pytest.approx(coupon.rate_pct, abs=1e-9)


def test_golden_coverage() -> None:
    counts = {secid: len(fixed_coupons(secid)) for secid in BONDS}
    assert sum(counts.values()) == 328
    assert sum(n for s, n in counts.items() if s.startswith("SU")) == 70


@pytest.mark.parametrize("secid", MINFIN_ADVANCE)
def test_old_ofz_pk_coupons_set_in_advance(secid: str) -> None:
    """Rate over the 6-month window, amount over the coupon period, both from the leg."""
    checked = [
        c
        for c in coupons(secid)
        if c.value is not None
        and c.start <= VALUATION
        and _months_before(c.start, 6) - timedelta(days=20) >= FIRST_FIXING
    ]
    assert len(checked) >= 4
    for coupon in checked:
        projection = minfin_advance_coupon(secid, coupon.start, coupon.end, coupon.face)
        assert projection.source == "fixed_by_history" and projection.coupon_rate is not None
        assert projection.coupon_rate * 100.0 == pytest.approx(coupon.rate_pct, abs=1e-9)
        assert projection.amount_rub == pytest.approx(coupon.value, abs=1e-9), (secid, coupon.start)
        amount = advance_amount(projection.coupon_rate, coupon.face, coupon.start, coupon.end)
        assert projection.amount_rub == pytest.approx(amount, abs=1e-9)


def test_in_advance_coupon_accrues_over_the_coupon_period() -> None:
    # SU29007 coupon 2026-03-04..2026-09-02 (182 days) observes [2025-09-02, 2026-03-02),
    # 181 days. The rate comes from the window, the amount from the coupon period (MOEX
    # 87.16); accruing the window instead would give 86.68.
    start, end = date(2026, 3, 4), date(2026, 9, 2)
    assert minfin_advance_window(start) == (date(2025, 9, 2), date(2026, 3, 2))
    assert MINFIN_ADVANCE_WINDOW.bounds(calendar(), start, end) == minfin_advance_window(start)
    projection = minfin_advance_coupon("SU29007RMFS0", start, end, 1000.0)
    assert projection.coupon_rate == pytest.approx(0.1748, abs=1e-12)
    assert projection.amount_rub == pytest.approx(87.16, abs=1e-9)
    assert projection.realized_days == 181  # the window's days, not the coupon's
    assert round_half_up(1000.0 * 0.1748 * 181 / 365.0) == pytest.approx(86.68, abs=1e-9)


def test_current_coupons_on_the_boards() -> None:
    """Every coupon the boards show as fixed for the running period is reproduced."""
    checked = 0
    for row in board_rows():
        if not row["COUPONVALUE"]:
            continue  # 0: the current coupon is not fixed yet
        secid, end = row["SECID"], date.fromisoformat(row["NEXTCOUPON"])
        (coupon,) = [c for c in coupons(secid) if c.end == end]
        if secid in MINFIN_ADVANCE:
            projection = minfin_advance_coupon(secid, coupon.start, end, coupon.face)
        else:
            projection = core_coupon(secid, coupon.start, end, coupon.face)
        amount = projection.amount_rub
        assert amount == pytest.approx(row["COUPONVALUE"], abs=1e-9), secid
        if row["COUPONPERCENT"] is not None and projection.coupon_rate is not None:
            assert round_half_up(projection.coupon_rate * 100.0) == pytest.approx(
                row["COUPONPERCENT"], abs=1e-9
            )
        checked += 1
    # 4 old OFZ-PK, SU29015, SU29028, RU000A1042W6 (only on the board), RU000A106K43,
    # RU000A108447.
    assert checked == 9


def test_in_arrears_amount_uses_the_unrounded_rate() -> None:
    # SU29013 2026-06-24..2026-09-23: rate 14.0356 % gives 34.99; MOEX's valueprc 14.03 is
    # the amount's rate on 365 days, and 14.03 % or 14.04 % would give 34.98 or 35.00.
    (coupon,) = [c for c in coupons("SU29013RMFS8") if c.start == date(2026, 6, 24)]
    projection = core_coupon("SU29013RMFS8", coupon.start, coupon.end, coupon.face)
    assert projection.coupon_rate == pytest.approx(0.140356, abs=5e-7)
    assert projection.amount_rub == coupon.value == 34.99 and coupon.rate_pct == 14.03
    rounded = replace(MINFIN_RUONIA, rate_rounding_decimals=2)
    assert core_coupon("SU29013RMFS8", coupon.start, coupon.end, 1000.0, rounded).amount_rub == 35.0


def test_leap_year_coupons_need_act_act() -> None:
    # The 2024 OFZ-PK coupons accrue 1/366 per day of the observation window.
    act365 = replace(MINFIN_RUONIA, day_count=ACT365)
    leap = [c for c in fixed_coupons("SU29015RMFS3") if c.end.year == 2024]
    assert len(leap) == 3
    for coupon in leap:
        assert coupon.value is not None
        assert core_coupon("SU29015RMFS3", coupon.start, coupon.end, coupon.face).amount_rub == (
            pytest.approx(coupon.value, abs=1e-9)
        )
        wrong = core_coupon("SU29015RMFS3", coupon.start, coupon.end, coupon.face, act365)
        assert wrong.amount_rub > coupon.value + 0.05


@pytest.mark.parametrize("lookback", [3, 4, 5])
def test_business_day_lookback_does_not_reproduce_the_calendar_shift(lookback: int) -> None:
    """The native lookback misses OFZ-PK coupons that the 6-day calendar shift hits.

    With ``L = 4`` it observes the same day as the shift from Monday to Thursday, but
    not on Fridays, weekends and around holidays: the shift must be in calendar days.
    """
    native = replace(MINFIN_RUONIA, shift_days=0, lookback_business_days=lookback)
    misses = 0
    for secid in ("SU29013RMFS8", "SU29015RMFS3", "SU29017RMFS9", "SU29022RMFS9", "SU29025RMFS2"):
        for coupon in [c for c in fixed_coupons(secid) if c.end <= LAST_CALENDAR_DAY]:
            amount = core_coupon(secid, coupon.start, coupon.end, coupon.face, native).amount_rub
            misses += amount != pytest.approx(coupon.value, abs=1e-9)
    assert misses >= 10


@pytest.mark.parametrize("days", [3, 4, 5])
def test_business_day_observation_shift_does_not_reproduce_the_calendar_shift(days: int) -> None:
    native = replace(MINFIN_RUONIA, shift_days=days, shift_unit="business_days")
    misses = 0
    for secid in ("SU29013RMFS8", "SU29015RMFS3", "SU29017RMFS9", "SU29022RMFS9", "SU29025RMFS2"):
        for coupon in [c for c in fixed_coupons(secid) if c.end <= LAST_CALENDAR_DAY]:
            amount = core_coupon(secid, coupon.start, coupon.end, coupon.face, native).amount_rub
            misses += amount != pytest.approx(coupon.value, abs=1e-9)
    assert misses >= 3


def test_amount_accrues_over_the_shifted_window() -> None:
    # Coupons whose 6-day shifted window splits its days between a leap and a common
    # year differently from the accrual period: ACT/ACT over the window (MOEX) and over
    # the accrual dates (the ISDA shift) differ, and only the window reproduces all.
    straddling = [
        (secid, c)
        for secid in (
            "SU29013RMFS8",
            "SU29015RMFS3",
            "SU29017RMFS9",
            "SU29022RMFS9",
            "SU29025RMFS2",
        )
        for c in fixed_coupons(secid)
        if abs(
            ACT_ACT.year_fraction(c.start, c.end)
            - ACT_ACT.year_fraction(c.start - timedelta(days=6), c.end - timedelta(days=6))
        )
        > 1e-6
    ]
    assert straddling
    misses = 0
    for secid, coupon in straddling:
        window = core_coupon(secid, coupon.start, coupon.end, coupon.face)
        assert window.amount_rub == pytest.approx(coupon.value, abs=1e-9)
        rule = replace(
            MINFIN_RUONIA.rule(0.0),
            observation_shift=ObservationShift(6, "calendar_days", "accrual"),
        )
        period = FloatingCouponPeriod(coupon.start, coupon.end, coupon.end, coupon.face)
        (accrual,) = FloatingLeg(rule, (period,), history("ruonia")).project(
            None, settlement_date=coupon.start
        )
        assert accrual.index_rate == window.index_rate
        misses += accrual.amount_rub != pytest.approx(coupon.value, abs=1e-9)
    # 7 of the 8 such coupons in the fixture miss on the accrual dates (one rounds alike).
    assert (len(straddling), misses) == (8, 7)


def test_term_ruonia_is_the_shifted_period() -> None:
    """``FloatingLeg`` compounding reproduces CBR's published term RUONIA 3M.

    On dates where ``D`` and ``D - 3 months`` are business days, compounded RUONIA over
    ``[D - 3M, D)`` equals ``RUONIA_AVG_3M`` of ``D`` (CBR publishes 14 decimals). The
    OFZ-PK 29026-29028 coupons are compounded over the period shifted 7 days, which is
    the published term rate only when that window is exactly three months: SU29026 pays
    on the 4th, its windows start on the 28th, and three of its coupons differ from
    ``RUONIA_AVG_3M`` of the fixing date while the shifted period reproduces all.
    """
    cal = calendar()
    rule = replace(RUONIA_COMPOUNDED7, shift_days=0).rule(0.0)  # the window is the period
    last = max(ruonia())
    checked = 0
    for day, published in sorted(term_ruonia_3m().items()):
        start = _months_before(day, 3)
        if day > last or not (cal.is_business_day(day) and cal.is_business_day(start)):
            continue
        period = FloatingCouponPeriod(start, day, day, 1.0)
        (projection,) = FloatingLeg(rule, (period,), history("ruonia")).project(
            None, settlement_date=start
        )
        assert projection.index_rate == pytest.approx(published, abs=1e-12), day
        checked += 1
    assert checked > 200
    # SU29026 2025-09-04..2025-12-04 fixes on 2025-11-27 over [2025-08-28, 2025-11-27).
    projection = core_coupon("SU29026RMFS0", date(2025, 9, 4), date(2025, 12, 4), 1000.0)
    assert projection.amount_rub == 42.6 and projection.index_rate is not None
    assert term_ruonia_3m()[date(2025, 11, 27)] > projection.index_rate + 1e-4


def test_first_coupons_of_december_2024_ofz_pk_are_a_known_gap() -> None:
    for (secid, start), (published, core) in FIRST_COUPON_GAPS.items():
        (coupon,) = [c for c in coupons(secid) if c.start == start]
        assert coupon.value == published
        assert core_coupon(secid, start, coupon.end, coupon.face).amount_rub == core
