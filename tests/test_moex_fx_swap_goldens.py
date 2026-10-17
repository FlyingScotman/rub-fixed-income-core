"""Covered interest parity against MOEX CNY/RUB swap data (fixtures in tests/fixtures/moex_fx_swaps).

``SRATE_CNY_ON`` is MOEX's daily indicative CNY/RUB overnight swap rate: ``rate`` is the
TOD/TOM swap points (RUB per CNY) and ``rateprc`` the implied RUB rate in percent. The
methodology text is not reachable from here (fs.moex.com is refused), so the tests pin
what the data establish, with the spot it is quoted on unknown (the day's TOD WAP is used):

- the TOD/TOM period runs on the joint Russian and Chinese settlement calendar;
- ``rateprc`` is the money-market parity rate with ACT/365F and no CNY rate term;
- ACT/360, or adding a CNY rate, is clearly rejected by the same data.
"""

from __future__ import annotations

import json
import math
import statistics
from datetime import date
from itertools import pairwise
from pathlib import Path
from typing import Any

import pytest

from rub_fixed_income import (
    BusinessCalendar,
    DayCount,
    DiscountCurve,
    FxForward,
    FxForwardLeg,
    FxForwardRiskEngine,
    FxForwardRiskRequest,
    FxSwapPointsQuote,
    InvalidTerms,
    cip_implied_rub_rate,
    cip_outright,
    fx_swap_value_dates,
    fx_value_date,
    implied_foreign_curve,
    joint_calendar,
)

FIXTURES = Path(__file__).parent / "fixtures" / "moex_fx_swaps"
RANGE = (date(2026, 6, 1), date(2026, 10, 31))
# Settlement holidays of the period, stated as inputs (the library ships no holiday list):
# Russia Day, and the Dragon Boat, Mid-Autumn and National Day holidays in China.
RU = BusinessCalendar(frozenset({date(2026, 6, 12)}), frozenset(), "test-ru-2026", *RANGE)
CN = BusinessCalendar(
    frozenset({date(2026, 6, 19), date(2026, 9, 25), *(date(2026, 10, d) for d in range(1, 8))}),
    frozenset(),
    "test-cn-2026",
    *RANGE,
)
CNYRUB = joint_calendar((RU, CN), version="test-rub-cny-2026")


def _rows(name: str, block: str = "history") -> list[dict[str, Any]]:
    payload = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    columns = payload[block]["columns"]
    return [dict(zip(columns, row, strict=True)) for row in payload[block]["data"]]


def _by_date(name: str, key: str) -> dict[date, dict[str, Any]]:
    return {date.fromisoformat(row[key]): row for row in _rows(name)}


SRATE = _by_date("srate_cny_on.json", "tradedate")
TOD = _by_date("cets_cnyrub_tod_history.json", "TRADEDATE")
TOM = _by_date("cets_cnyrub_tom_history.json", "TRADEDATE")
DAYS = sorted(SRATE)


def test_fixture_coverage() -> None:
    assert len(DAYS) == 92 and DAYS[0] == date(2026, 6, 1) and DAYS[-1] == date(2026, 10, 16)
    assert all(row["secid"] == "SRATE_CNY_ON" for row in SRATE.values())


def test_tod_tom_periods_follow_the_joint_settlement_calendar() -> None:
    # Every published TOD/TOM swap runs to the next publication, which is the TOM date.
    for today, following in pairwise(DAYS):
        assert fx_swap_value_dates(today, "TOD", "TOM", CNYRUB) == (today, following)
    assert (DAYS[DAYS.index(date(2026, 9, 30)) + 1] - date(2026, 9, 30)).days == 8
    # A day without TOD settlement is exactly a joint holiday or weekend.
    for day in (date(2026, 6, 19), date(2026, 9, 25), date(2026, 10, 1), date(2026, 10, 7)):
        assert TOD[day]["NUMTRADES"] == 0 and TOM[day]["NUMTRADES"] > 0
        with pytest.raises(InvalidTerms, match="no TOD"):
            fx_value_date(day, "TOD", CNYRUB)
        assert fx_value_date(day, "TOM", CNYRUB) > day
    assert fx_value_date(date(2026, 10, 1), "TOM", CNYRUB) == date(2026, 10, 8)
    assert date(2026, 6, 12) not in TOD and date(2026, 6, 12) not in TOM  # no session


def test_value_dates_match_the_cets_board_settlement_dates() -> None:
    rows = {row["SECID"]: row for row in _rows("cets_cny_securities.json", "securities")}
    session = date(2026, 10, 16)
    for secid, code in (("CNY000000TOD", "TOD"), ("CNYRUB_TOM", "TOM"), ("CNYRUB_SPT", "SPT")):
        assert fx_value_date(session, code, CNYRUB) == date.fromisoformat(rows[secid]["SETTLEDATE"])
    # Swap instruments carry the near-leg date: TOD for TODTOM, TOM for the TOM swaps.
    assert date.fromisoformat(rows["CNYRUBTODTOM"]["SETTLEDATE"]) == session
    for secid, far in (("CNYRUB_TOM1D", "SPT"), ("CNYRUB_TOM1W", "1W")):
        near, _ = fx_swap_value_dates(session, "TOM", far, CNYRUB, spot_lag_days=1)
        assert near == date.fromisoformat(rows[secid]["SETTLEDATE"])


def _errors_bp(day_count: DayCount, foreign_rate: float = 0.0) -> list[float]:
    """Core implied RUB rate minus MOEX ``rateprc`` for every TOD/TOM swap, in bp."""
    errors = []
    for today in DAYS[:-1]:
        near, far = fx_swap_value_dates(today, "TOD", "TOM", CNYRUB)
        spot = TOD[today]["WAPRICE"]
        implied = cip_implied_rub_rate(
            spot,
            spot + SRATE[today]["rate"],
            near,
            far,
            foreign_rate=foreign_rate,
            rub_day_count=day_count,
            foreign_day_count=day_count,
        )
        errors.append((implied * 100.0 - SRATE[today]["rateprc"]) * 100.0)
    return errors


def test_published_overnight_swap_rate_is_act365_parity_without_a_cny_rate() -> None:
    errors = _errors_bp(DayCount.ACT_365_FIXED)
    absolute = sorted(abs(e) for e in errors)
    assert len(errors) == 91
    # The points are rounded to 1e-5 RUB (about 1.5 bp on one day) and the spot behind the
    # published figure is not the daily WAP, so single days scatter; the bias does not.
    assert abs(statistics.mean(errors)) < 2.0
    assert statistics.median(absolute) < 5.0
    assert absolute[-1] < 25.0
    assert sum(e <= 10.0 for e in absolute) >= 75
    # The alternatives the same data reject: ACT/360, or adding even a 1 % CNY rate.
    assert statistics.mean(_errors_bp(DayCount.ACT_360)) < -15.0
    assert statistics.mean(_errors_bp(DayCount.ACT_365_FIXED, foreign_rate=0.01)) > 90.0


def test_published_rate_round_trips_to_published_points() -> None:
    worst = 0.0
    for today in DAYS[:-1]:
        near, far = fx_swap_value_dates(today, "TOD", "TOM", CNYRUB)
        spot = TOD[today]["WAPRICE"]
        rate = SRATE[today]["rateprc"] / 100.0
        outright = cip_outright(
            spot,
            near,
            far,
            rub_rate=rate,
            foreign_rate=0.0,
            rub_day_count=DayCount.ACT_365_FIXED,
            foreign_day_count=DayCount.ACT_365_FIXED,
        )
        assert cip_implied_rub_rate(
            spot,
            outright,
            near,
            far,
            foreign_rate=0.0,
            rub_day_count=DayCount.ACT_365_FIXED,
            foreign_day_count=DayCount.ACT_365_FIXED,
        ) == pytest.approx(rate, abs=1e-12)
        points = outright - spot
        worst = max(worst, abs(points - SRATE[today]["rate"]) / SRATE[today]["rate"])
    assert worst < 0.025  # points within 2.5 % on every day (median a fraction of that)


def test_implied_cny_curve_from_the_published_overnight_swap() -> None:
    """A TOD/TOM swap struck at MOEX's points is at market on the implied curve.

    With a flat RUB curve at MOEX's implied rate the overnight CNY rate implied by the
    points is near zero, as the published rate carries no CNY rate term.
    """
    today = date(2026, 10, 15)
    near, far = fx_swap_value_dates(today, "TOD", "TOM", CNYRUB)
    spot = TOD[today]["WAPRICE"]
    points = SRATE[today]["rate"]
    simple = SRATE[today]["rateprc"] / 100.0
    tau = (far - near).days / 365.0
    rub = DiscountCurve(today, ((far, 1.0 / (1.0 + simple * tau)),))
    cny = implied_foreign_curve(
        rub_curve=rub,
        spot_rate=spot,
        spot_value_date=today,
        quotes=(FxSwapPointsQuote("CNY_TODTOM", near, far, points),),
    )
    implied_cny = -math.log(cny.discount_factor(far)) / tau
    assert abs(implied_cny) < 0.01
    swap = FxForward("TOM-LEG", "CNY", 1.0, FxForwardLeg(far, -1_000_000.0, spot + points))
    engine = FxForwardRiskEngine(FxForwardRiskRequest(swap, spot, today, rub, cny, basis_bp=0.0))
    assert engine.base_pv_rub == pytest.approx(0.0, abs=1e-6)
