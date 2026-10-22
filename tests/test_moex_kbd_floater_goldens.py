"""Golden tests: MOEX coupons of KBD-linked floaters (``ZR_YLD_CRV``, 0.34.0).

Fixture: ``tests/fixtures/kbd_floaters/kbd_floaters.json`` (provenance inside and in
``tests/fixtures/kbd_floaters/README.md``): the ZCYC parameters MOEX published at end of
day around each fixing, the list of ZCYC publication days, and the coupons MOEX lists as
known (rate and amount per bond) for ten corporate bonds.

The rule is identified from the data, not from issue documents: the coupon rate is the
effective annual KBD zero yield at a constant tenor, taken on the N-th ZCYC publication
day before the coupon starts, rounded to 0.01 %, plus the margin; the amount is
``face * rate * days / 365`` rounded to the kopeck. Two families reproduce every checked
coupon: 7Y with N = 5 and 5Y with N = 4. The margin of the 5Y bonds resets at their
offers; ``FloatingCouponPeriod.margin`` carries it. A rule is kept only because its
neighbours (another tenor, N +- 1) do not give one margin per run.

KAMAZ BO-09/10/11 list five coupons (2022-05 to 2024-05) to 0.0001 % instead of 0.01 %;
they are checked with that rounding. Not covered (pinned gaps, see ``docs/floating.md``):
the first coupon of a bond (set at placement); Avtodor BO-002R-01 (two June coupons off), FPK 001R-05 (several coupons off
by up to 0.14 pp), VTB T2-3, Gazprom Capital 001B-02/03, RZD 001B-01/02 and GTLK 001P-01
(early coupons from another formula).
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from functools import cache
from itertools import pairwise
from pathlib import Path
from typing import Any

import pytest

from rub_fixed_income import (
    BusinessCalendar,
    DayCount,
    FixingDateRule,
    FixingHistory,
    FloatingCouponPeriod,
    FloatingCouponRule,
    FloatingLeg,
    MissingFixing,
    ZcycParameters,
    kbd_effective_yield,
)

FIXTURE = Path(__file__).parent / "fixtures" / "kbd_floaters" / "kbd_floaters.json"
VALUATION = date(2026, 10, 22)
ACT365 = DayCount.ACT_365_FIXED


@cache
def _data() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))  # type: ignore[no-any-return]


@cache
def publication_calendar() -> BusinessCalendar:
    """Business days are exactly the days with an end-of-day ZCYC publication."""
    published = {date.fromisoformat(d) for d in _data()["zcyc_eod_dates"]}
    first, last = min(published), max(published)
    holidays, working = set(), set()
    day = first
    while day <= last:
        weekday = day.weekday() < 5
        if weekday and day not in published:
            holidays.add(day)
        elif not weekday and day in published:
            working.add(day)
        day += timedelta(days=1)
    return BusinessCalendar(
        frozenset(holidays), frozenset(working), "zcyc-eod-publication", first, last
    )


def _params(day: str, row: list[float]) -> ZcycParameters:
    b1, b2, b3, t1, *g = row
    return ZcycParameters(date.fromisoformat(day), b1, b2, b3, t1, tuple(g))  # type: ignore[arg-type]


def history(tenor: float) -> FixingHistory:
    fixings = tuple(
        (date.fromisoformat(day), kbd_effective_yield(_params(day, row), tenor))
        for day, row in sorted(_data()["zcyc_params"].items())
    )
    return FixingHistory(
        "kbd", VALUATION, fixings, publication_calendar(), "hub-zcyc-2026-10-22", tenor_years=tenor
    )


def rule(tenor: float, offset: int, margin: float, decimals: int = 2) -> FloatingCouponRule:
    return FloatingCouponRule(
        "kbd",
        "single_fixing",
        margin,
        ACT365,
        ACT365,
        rate_rounding_decimals=decimals,
        amount_rounding_decimals=2,
        fixing_date=FixingDateRule("accrual_start", offset, "business_days"),
        index_tenor_years=tenor,
    )


def leg(bond: dict[str, Any], tenor: float, offset: int, *, decimals: int = 2) -> FloatingLeg:
    periods = tuple(
        FloatingCouponPeriod(
            date.fromisoformat(c["start"]),
            date.fromisoformat(c["payment"]),
            date.fromisoformat(c["payment"]),
            c["face"],
            margin=c["margin_pct"] / 100.0,
        )
        for c in bond["coupons"]
    )
    margin = bond["hub_spread_bp"] / 1e4
    return FloatingLeg(rule(tenor, offset, margin, decimals), periods, history(tenor))


BONDS = {b["secid"]: b for b in _data()["bonds"]}


KAMAZ = ("RU000A0ZZ877", "RU000A0ZZ885", "RU000A0ZZ893")
FOUR_DECIMALS = ("2022-05-24", "2022-11-22", "2023-05-23", "2023-11-21", "2024-05-21")
"""KAMAZ BO-09/10/11 coupons whose rate MOEX lists to 0.0001 %: the KBD yield unrounded
plus the margin (11.1198 = 10.3698 + 0.75). Before and after, the rate is to 0.01 %."""


def _decimals(secid: str, start: str) -> int:
    return 4 if secid in KAMAZ and start in FOUR_DECIMALS else 2


@pytest.mark.parametrize("secid", sorted(BONDS))
def test_every_known_coupon_to_the_kopeck(secid: str) -> None:
    bond = BONDS[secid]
    by_decimals = {
        n: leg(bond, bond["tenor_years"], bond["fixing_offset"], decimals=n) for n in (2, 4)
    }
    for i, coupon in enumerate(bond["coupons"]):
        projected = by_decimals[_decimals(secid, coupon["start"])].coupon(i, None)
        assert projected.source == "fixed_by_history"
        assert projected.coupon_rate is not None
        assert round(projected.coupon_rate * 100.0, 6) == coupon["rate_pct"], (secid, coupon)
        assert projected.amount_rub == coupon["amount"], (secid, coupon)


def _margin_runs(bond: dict[str, Any]) -> int:
    margins = [c["margin_pct"] for c in bond["coupons"]]
    return 1 + sum(1 for a, b in pairwise(margins) if a != b)


def _implied_margin_runs(bond: dict[str, Any], tenor: float, offset: int) -> int:
    """Runs of the margin a candidate rule implies from the published rates."""
    fixings = history(tenor)
    calendar = publication_calendar()
    margins = []
    for coupon in bond["coupons"]:
        start = date.fromisoformat(coupon["start"])
        day = start
        for _ in range(offset):
            day -= timedelta(days=1)
            while not calendar.is_business_day(day):
                day -= timedelta(days=1)
        try:
            index, _ = fixings.rate(day)
        except MissingFixing:
            return 99
        decimals = _decimals(bond["secid"], coupon["start"])
        margins.append(round(coupon["rate_pct"] - round(index * 100.0, decimals), 2))
    return 1 + sum(1 for a, b in pairwise(margins) if a != b)


@pytest.mark.parametrize("secid", sorted(BONDS))
def test_the_rule_is_identified_against_its_neighbours(secid: str) -> None:
    """One margin per offer period with the rule; the neighbouring rules break the runs."""
    bond = BONDS[secid]
    tenor, offset = bond["tenor_years"], bond["fixing_offset"]
    assert _margin_runs(bond) <= 2
    assert _implied_margin_runs(bond, tenor, offset) == _margin_runs(bond)
    for other_offset in (offset - 1, offset + 1):
        assert _implied_margin_runs(bond, tenor, other_offset) > 2, other_offset
    other_tenor = 5 if tenor == 7 else 7
    assert _implied_margin_runs(bond, other_tenor, offset) > 2


def test_margin_reset_at_the_offer_is_the_hub_spread() -> None:
    """RZD 001B-03/04/06: the margin after the offer is the one the hub publishes now."""
    for secid in ("RU000A102564", "RU000A1028U9", "RU000A102FE1"):
        bond = BONDS[secid]
        assert bond["coupons"][-1]["margin_pct"] == pytest.approx(bond["hub_spread_bp"] / 100.0)
