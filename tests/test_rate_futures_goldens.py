"""Golden tests: MOEX short-rate futures settlement prices from Bank of Russia inputs.

Fixtures and provenance: ``tests/fixtures/moex_rate_futures/README.md``. Final settlement
prices of expired contracts are fully determined by published fixings, so they must be
reproduced exactly; prices of live contracts are market marks, compared with stated
tolerances.
"""

from __future__ import annotations

import json
import re
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from rub_fixed_income import (
    BusinessCalendar,
    DiscountCurve,
    KeyRateForwardCurve,
    KeyRateForwardSegment,
    KeyRateFuturesContract,
    RateFuturesRiskEngine,
    RateFuturesRiskRequest,
    RuoniaAverageFuturesContract,
    RuoniaFixings,
    RuoniaIndexFuturesContract,
    RuoniaIndexValue,
    key_rate_index_value,
    ruonia_average_final_price,
    ruonia_average_settlement_period,
    ruonia_index_final_price,
    ruonia_index_value,
)

FIXTURES = Path(__file__).parent / "fixtures" / "moex_rate_futures"

# Official Russian production calendar, 2025-10..2026-12 (Government decrees on the
# transfer of days off). It matches the RUONIA publication days in the fixture: every
# weekday listed here has no fixing, 2025-11-01 (a working Saturday) has one.
HOLIDAYS = frozenset(
    {
        date(2025, 11, 3),
        date(2025, 11, 4),
        date(2025, 12, 31),
        *(date(2026, 1, d) for d in range(1, 10)),
        date(2026, 2, 23),
        date(2026, 3, 9),
        date(2026, 5, 1),
        date(2026, 5, 11),
        date(2026, 6, 12),
        date(2026, 11, 4),
    }
)
CALENDAR = BusinessCalendar(
    holidays=HOLIDAYS,
    working_days=frozenset({date(2025, 11, 1)}),
    version="ru-production-2025-2026-test",
    valid_from=date(2025, 10, 1),
    valid_to=date(2026, 12, 31),
)
# The Bank of Russia publishes no RUONIA for the last business day of the year.
UNPUBLISHED = frozenset({date(2025, 12, 30)})


def iss_rows(name: str, block: str = "history") -> list[dict[str, Any]]:
    raw = json.loads((FIXTURES / "iss" / name).read_text("utf-8"))[block]
    return [dict(zip(raw["columns"], row, strict=True)) for row in raw["data"]]


def description(secid: str) -> dict[str, str]:
    raw = json.loads((FIXTURES / "iss" / f"description_{secid}.json").read_text("utf-8"))
    return {row[0]: row[2] for row in raw["description"]["data"]}


def settle(secid: str, day: str) -> float:
    (row,) = iss_rows(f"history_{secid}_{day}.json")
    assert row["TRADEDATE"] == day
    return float(row["SETTLEPRICE"])


def ruonia_fixings() -> tuple[tuple[date, float], ...]:
    text = (FIXTURES / "cbr" / "RuoniaXML_2025-11-01_2026-10-16.xml").read_text("utf-8")
    rows = re.findall(r"<D0>(\d{4}-\d\d-\d\d)T[^<]*</D0><ruo>([\d.]+)</ruo>", text)
    return tuple(sorted((date.fromisoformat(d), float(Decimal(r) / 100)) for d, r in rows))


def ruonia_index(name: str) -> dict[date, float]:
    text = (FIXTURES / "cbr" / name).read_text("utf-8")
    rows = re.findall(r"<DT>(\d{4}-\d\d-\d\d)T[^<]*</DT><RUONIA_Index>([\d.]+)<", text)
    return {date.fromisoformat(d): float(v) for d, v in rows}


def key_rates() -> tuple[tuple[date, float], ...]:
    text = (FIXTURES / "cbr" / "KeyRateXML_2026-01-01_2026-10-16.xml").read_text("utf-8")
    rows = re.findall(r"<DT>(\d{4}-\d\d-\d\d)T[^<]*</DT><Rate>([\d.]+)</Rate>", text)
    return tuple(sorted((date.fromisoformat(d), float(Decimal(r) / 100)) for d, r in rows))


FIXINGS = ruonia_fixings()


def fixings_at(valuation: date) -> RuoniaFixings:
    return RuoniaFixings(
        valuation,
        tuple(row for row in FIXINGS if row[0] < valuation),
        CALENDAR,
        f"cbr-ruonia-{valuation.isoformat()}",
        frozenset(d for d in UNPUBLISHED if d < valuation),
    )


def test_calendar_matches_the_ruonia_publication_days() -> None:
    published = {d for d, _ in FIXINGS}
    day = FIXINGS[0][0]
    while day <= FIXINGS[-1][0]:
        expected = CALENDAR.is_business_day(day) and day not in UNPUBLISHED
        assert (day in published) == expected, day
        day += timedelta(days=1)


RUON_FINALS = {
    "RRZ5": (2025, 12),
    "RRF6": (2026, 1),
    "RRG6": (2026, 2),
    "RRH6": (2026, 3),
    "RRJ6": (2026, 4),
    "RRM6": (2026, 6),
    "RRN6": (2026, 7),
    "RRQ6": (2026, 8),
    "RRU6": (2026, 9),
}


@pytest.mark.parametrize("secid", sorted(RUON_FINALS))
def test_one_month_ruonia_final_settlement_is_reproduced(secid: str) -> None:
    year, month = RUON_FINALS[secid]
    start, end = ruonia_average_settlement_period(year, month, CALENDAR)
    assert end.isoformat() == description(secid)["LSTTRADE"]
    contract = RuoniaAverageFuturesContract(secid, start, end)
    final = ruonia_average_final_price(contract, fixings_at(end))
    assert final == settle(secid, end.isoformat())


def test_one_month_ruonia_may_2026_is_a_documented_outlier() -> None:
    """RRK6 (no open interest) settled at 85.79; the specification gives 85.82.

    No window or carry rule reproduces 85.79 while keeping the other nine months exact;
    the gap is recorded in the release notes, not hidden.
    """
    start, end = ruonia_average_settlement_period(2026, 5, CALENDAR)
    contract = RuoniaAverageFuturesContract("RRK6", start, end)
    assert ruonia_average_final_price(contract, fixings_at(end)) == 85.82
    assert settle("RRK6", "2026-05-29") == 85.79


def test_year_end_unpublished_ruonia_is_carried_and_declared() -> None:
    # RRF6's month starts on 2025-12-30, the business day without a RUONIA.
    start, end = ruonia_average_settlement_period(2026, 1, CALENDAR)
    assert start == date(2025, 12, 30)
    undeclared = RuoniaFixings(
        end, tuple(r for r in FIXINGS if r[0] < end), CALENDAR, "no-declaration"
    )
    contract = RuoniaAverageFuturesContract("RRF6", start, end)
    with pytest.raises(ValueError, match="2025-12-30"):
        ruonia_average_final_price(contract, undeclared)


def test_ruonia_index_is_reproduced_from_fixings() -> None:
    published = ruonia_index("RuoniaSVXML_2026-05-01_2026-10-16.xml")
    anchor_day = date(2026, 5, 4)
    anchor = RuoniaIndexValue(anchor_day, published[anchor_day])
    fixings = fixings_at(date(2026, 10, 16))
    for day, value in published.items():
        if day < anchor_day:
            continue
        assert ruonia_index_value(anchor, fixings, day) == pytest.approx(value, rel=2e-15), day


def test_ruonia_index_bridges_the_year_end_gap_by_simple_interest() -> None:
    published = ruonia_index("RuoniaSVXML_2025-12-01_2026-01-31.xml")
    anchor = RuoniaIndexValue(date(2025, 12, 26), published[date(2025, 12, 26)])
    fixings = fixings_at(date(2026, 1, 31))
    for day in (date(2025, 12, 31), date(2026, 1, 10), date(2026, 1, 12), date(2026, 1, 13)):
        assert ruonia_index_value(anchor, fixings, day) == pytest.approx(published[day], rel=2e-15)


def test_ruonia_index_futures_final_settlement_is_reproduced() -> None:
    published = ruonia_index("RuoniaSVXML_2026-05-01_2026-10-16.xml")
    ltd = date.fromisoformat(description("RFU6")["LSTTRADE"])
    contract = RuoniaIndexFuturesContract("RFU6", ltd)
    anchor = RuoniaIndexValue(date(2026, 5, 4), published[date(2026, 5, 4)])
    final = ruonia_index_final_price(contract, anchor, fixings_at(ltd))
    assert final == settle("RFU6", ltd.isoformat()) == 4.4697


def test_keyrate_index_is_reproduced_from_bank_of_russia_decisions() -> None:
    meetings = frozenset(
        date.fromisoformat(description(s)["LSTTRADE"]) for s in ("KKV6", "KKZ6", "KKG7")
    )
    history = key_rates()
    rows = iss_rows("history_KEYRATE_2026-08-24_2026-10-16.json")
    assert len(rows) == 40
    for row in rows:
        day = date.fromisoformat(row["TRADEDATE"])
        assert key_rate_index_value(day, history, meetings, CALENDAR) == row["CLOSE"]


def test_live_contracts_against_a_key_rate_path_from_keyrate_futures() -> None:
    """2026-10-16: RUONIA index futures vs the path implied by KEYRATE futures.

    Live settlement prices are exchange marks: RFZ6 had no trades that day. The key-rate
    path comes from the KEYRATE settlement prices (13.92 from 2026-10-26, 13.80 from
    2026-12-21), RUONIA from that path plus the mean RUONIA - key rate spread of the last
    20 fixings. Core's RFZ6 (4.62777) is then 0.0032 points (32 RUB per contract,
    0.07 %) above the mark 4.6246; the mark implies RUONIA 54 bp below that path, which
    the calibrated basis absorbs. The tolerance is that observed gap, not a model error:
    the mark is the exchange's own valuation of an untraded contract.
    """
    valuation = date(2026, 10, 16)
    kkv6, kkz6 = settle("KKV6", "2026-10-16"), settle("KKZ6", "2026-10-16")
    key_curve = KeyRateForwardCurve(
        valuation,
        0.14,
        date(2026, 10, 26),
        (
            KeyRateForwardSegment(date(2026, 10, 26), date(2026, 12, 21), kkv6 / 100, "KKV6"),
            KeyRateForwardSegment(date(2026, 12, 21), date(2027, 2, 15), kkz6 / 100, "KKZ6"),
        ),
    )
    for secid, contract in (
        ("KKV6", KeyRateFuturesContract("KKV6", date(2026, 10, 23), date(2026, 10, 26))),
        ("KKZ6", KeyRateFuturesContract("KKZ6", date(2026, 12, 18), date(2026, 12, 21))),
    ):
        engine = RateFuturesRiskEngine(
            RateFuturesRiskRequest(contract, key_curve, futures_price=settle(secid, "2026-10-16"))
        )
        assert abs(engine.basis_bp) < 1e-8  # the path reprices its own futures

    history = dict(key_rates())
    recent = [d for d, _ in FIXINGS][-20:]
    spread = sum(dict(FIXINGS)[d] - history[d] for d in recent) / len(recent)
    # A discount curve whose overnight forwards are the projected RUONIA path.
    nodes = []
    df = 1.0
    day = valuation
    while day < date(2026, 12, 2):
        nxt = day + timedelta(days=1)
        rate = key_curve.rate_at(day) + spread
        df /= 1.0 + rate / 365.0
        nodes.append((nxt, df))
        day = nxt
    curve = DiscountCurve(valuation, tuple(nodes))
    published = ruonia_index("RuoniaSVXML_2026-05-01_2026-10-16.xml")
    rfz6 = RuoniaIndexFuturesContract("RFZ6", date(2026, 12, 1))
    engine = RateFuturesRiskEngine(
        RateFuturesRiskRequest(
            rfz6,
            curve,
            fixings=fixings_at(valuation),
            index_anchor=RuoniaIndexValue(valuation, published[valuation]),
            basis_bp=0.0,
        )
    )
    mark = settle("RFZ6", "2026-10-16")
    assert engine.theoretical_price == pytest.approx(mark, abs=0.0035)
    calibrated = RateFuturesRiskEngine(
        RateFuturesRiskRequest(
            rfz6,
            curve,
            fixings=fixings_at(valuation),
            index_anchor=RuoniaIndexValue(valuation, published[valuation]),
            futures_price=mark,
        )
    )
    assert calibrated.base_price == pytest.approx(mark, abs=1e-12)
    assert -80.0 < calibrated.basis_bp < -20.0
