"""MOEX-published repo amounts reproduced by ``rub_fixed_income.repo`` (package 6).

Sources (URLs, dates and SHA-256 in ``docs/sources/repo_moex/README.md``):

- MOEX, «РЕПО с КСУ с автоматической капитализацией процентов», 10 September 2026:
  the worked RUSFAR examples of a compounding open repo (slides 4, 10 and 17);
- MOEX, «РЕПО с плавающими ставками в EQM06», 21 November 2023: the key-rate repo
  example (Пример 1, «РЕПО с ЦК. Срок 7 дней. Индикатор Ключевая ставка»);
- MOEX ISS RUSFAR history (``tests/fixtures/moex_repo``), which carries the real
  RUSFAR fixings behind the September 2026 examples.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from rub_fixed_income import (
    BusinessCalendar,
    DayCount,
    DiscountCurve,
    FixingHistory,
    FloatingRepoRate,
    KeyRateForwardCurve,
    KeyRateForwardSegment,
    RepoDayBasis,
    RepoRiskEngine,
    RepoRiskRequest,
    RepoTrade,
    key_rate_projection_curve,
)

FIXTURE = Path(__file__).parent / "fixtures" / "moex_repo" / "rusfar_2026-08-25_2026-10-16.json"
# August-October 2026 has no Russian public holiday: weekends are the only
# non-settlement days and the only days without RUSFAR (methodology, section 3).
CALENDAR_2026 = BusinessCalendar(
    version="weekends-2026-08-10", valid_from=date(2026, 8, 1), valid_to=date(2026, 10, 31)
)
CALENDAR_2023 = BusinessCalendar(
    version="weekends-2023-09", valid_from=date(2023, 9, 1), valid_to=date(2023, 12, 31)
)


def _rusfar_rows() -> tuple[tuple[date, float], ...]:
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))["history"]
    columns = payload["columns"]
    day, close = columns.index("TRADEDATE"), columns.index("CLOSE")
    rows = tuple(
        (date.fromisoformat(row[day]), row[close] / 100.0)
        for row in payload["data"]
        if row[columns.index("SECID")] == "RUSFAR"
    )
    assert len(rows) == 39
    return rows


def _rusfar(valuation: date) -> FixingHistory:
    return FixingHistory(
        "ruonia",
        valuation,
        tuple(row for row in _rusfar_rows() if row[0] < valuation),
        CALENDAR_2026,
        "iss-rusfar-2026-10-17",
    )


def _compounding(spread: float) -> FloatingRepoRate:
    return FloatingRepoRate(
        "RUSFAR",
        spread,
        RepoDayBasis.ACT_365_366,
        "previous_settlement_day",
        DayCount.ACT_365_FIXED,
        "capitalised",
        nonpositive_rate_replacement=0.0001,
    )


def _engine(
    trade: RepoTrade, valuation: date, history: FixingHistory, **kwargs: object
) -> RepoRiskEngine:
    request = RepoRiskRequest(
        trade,
        CALENDAR_2026 if valuation.year == 2026 else CALENDAR_2023,
        DiscountCurve.flat(valuation, 0.14),
        fixings=history,
        **kwargs,  # type: ignore[arg-type]
    )
    return RepoRiskEngine(request)


def test_iss_rusfar_matches_the_presentation_benchmarks() -> None:
    rows = dict(_rusfar_rows())
    # Slide 17, column «RUSFAR определен и опубликован в отчете за указанную дату».
    assert rows[date(2026, 9, 2)] == pytest.approx(0.1388)
    assert rows[date(2026, 9, 3)] == pytest.approx(0.1395)
    assert rows[date(2026, 9, 4)] == pytest.approx(0.1415)
    assert rows[date(2026, 9, 7)] == pytest.approx(0.1399)
    # Slide 10: the known value for 02/09 is RUSFAR of 01/09.
    assert rows[date(2026, 9, 1)] == pytest.approx(0.1399)


def test_compounding_rusfar_open_repo_slide_17() -> None:
    """10 000 000 from 02.09.2026, spread 0.30 %, closed for 07.09.2026: 10 019 676,52."""
    trade = RepoTrade(
        "slide-17",
        "reverse_repo",
        date(2026, 9, 2),
        None,
        10_000_000.0,
        _compounding(0.003),
        2,
    )
    end = date(2026, 9, 7)
    # Balances after each credit (EQM64 InterestAmount 3 884,93 / 3 905,63 / 11 885,96).
    friday = _engine(trade, date(2026, 9, 4), _rusfar(date(2026, 9, 4)), assumed_end_date=end)
    assert friday.valuation().current_repurchase_amount_rub == pytest.approx(
        10_007_790.56, abs=1e-9
    )
    on_saturday = _engine(trade, date(2026, 9, 5), _rusfar(date(2026, 9, 5)), assumed_end_date=end)
    valuation = on_saturday.valuation()
    assert valuation.end_amount_source == "known"
    assert valuation.end_amount_rub == pytest.approx(10_019_676.52, abs=1e-9)
    assert valuation.known_interest_rub == pytest.approx(19_676.52, abs=1e-6)
    thursday = _engine(trade, date(2026, 9, 3), _rusfar(date(2026, 9, 3)), assumed_end_date=end)
    assert thursday.valuation().current_repurchase_amount_rub == pytest.approx(
        10_003_884.93, abs=1e-9
    )


def test_compounding_example_slide_4_on_its_stated_rates() -> None:
    """Slide 4 states its applied rates (13,89 / 13,93 / 13,77 / 13,97 %), not ISS ones.

    They are fed as a synthetic RUSFAR history with a zero spread, each fixing dated
    the settlement day before its credit; the weekend accrues simply (3 days at
    13,77 % credited on Monday).
    """
    rows = (
        (date(2026, 9, 16), 0.1389),
        (date(2026, 9, 17), 0.1393),
        (date(2026, 9, 18), 0.1377),
        (date(2026, 9, 21), 0.1397),
    )
    trade = RepoTrade(
        "slide-4", "reverse_repo", date(2026, 9, 16), None, 10_000_000.0, _compounding(0.0), 2
    )
    expected = {
        date(2026, 9, 17): 10_003_805.48,
        date(2026, 9, 18): 10_007_623.37,
        date(2026, 9, 21): 10_018_949.81,
        date(2026, 9, 22): 10_022_784.46,
    }
    for valuation, balance in expected.items():
        history = FixingHistory(
            "ruonia",
            valuation,
            tuple(row for row in rows if row[0] < valuation),
            CALENDAR_2026,
            "slide-4-rates",
        )
        engine = _engine(trade, valuation, history, assumed_end_date=date(2026, 9, 24))
        assert engine.valuation().current_repurchase_amount_rub == pytest.approx(balance, abs=1e-9)


def test_open_repo_amount_to_execute_slide_10() -> None:
    """30 000 000 from 01.09.2026, spread 0.10 %: on 02.09 «сумма к исполнению» is
    ОКРУГЛ(30 000 000 * (1 + (13,99 + 0,1) / 100 / 365); 2) = 30 011 580,82."""
    trade = RepoTrade(
        "slide-10", "reverse_repo", date(2026, 9, 1), None, 30_000_000.0, _compounding(0.001), 2
    )
    engine = _engine(
        trade, date(2026, 9, 2), _rusfar(date(2026, 9, 2)), assumed_end_date=date(2026, 9, 3)
    )
    assert engine.valuation().current_repurchase_amount_rub == pytest.approx(
        30_011_580.82, abs=1e-9
    )


KEY_RATE_HISTORY_ROWS = ((date(2023, 9, 18), 0.13), (date(2023, 9, 25), 0.17))


def _key_rate_engine(valuation: date, forward: KeyRateForwardCurve) -> RepoRiskEngine:
    """EQM06 Пример 1: 6 449 940,00 from 20.09.2023 to 27.09.2023, key rate + 0,20 %.

    The example's key rate (13 % to 24.09, 17 % from 25.09) is the presentation's
    illustration, fed as a synthetic key-rate history. Its projected part uses MOEX's
    risk-parameter rate of the day (13,06 %, 13,05 % ...), fed as a flat forward.
    """
    history = FixingHistory(
        "key_rate",
        valuation,
        tuple(row for row in KEY_RATE_HISTORY_ROWS if row[0] < valuation),
        CALENDAR_2023,
        "eqm06-example-1",
    )
    projection = key_rate_projection_curve(
        forward, CALENDAR_2023, rate_day_count=DayCount.ACT_365_FIXED, observation_days="calendar"
    )
    rate = FloatingRepoRate(
        "KEY_RATE", 0.002, RepoDayBasis.ACT_365_366, "calendar_day", DayCount.ACT_365_FIXED
    )
    trade = RepoTrade(
        "eqm06-1", "reverse_repo", date(2023, 9, 20), date(2023, 9, 27), 6_449_940.0, rate, 2
    )
    return RepoRiskEngine(
        RepoRiskRequest(
            trade,
            CALENDAR_2023,
            DiscountCurve.flat(valuation, 0.13),
            projection_curve=projection,
            fixings=history,
        )
    )


def _flat_key_rate(valuation: date, rate: float) -> KeyRateForwardCurve:
    return KeyRateForwardCurve(valuation, rate, date(2023, 12, 31), ())


@pytest.mark.parametrize(
    ("valuation", "risk_rate", "known_amount", "end_amount"),
    [
        # Report of day 1 (20.09): nothing known, 7 days at 13,06 % + 0,20 %.
        (date(2023, 9, 20), 0.1306, 6_449_940.00, 6_466_342.29),
        # Report of day 2 (21.09): day 21 known, 6 days at 13,05 % + 0,20 %.
        (date(2023, 9, 22), 0.1305, 6_452_272.58, 6_466_321.08),
        # Report of day 3 (22.09): days 21-22 known, 5 days at 13,02 % + 0,20 %.
        (date(2023, 9, 23), 0.1302, 6_454_605.16, 6_466_285.74),
        # Report of day 6 (25.09): 4 days at 13,20 % and 1 at 17,20 % known, 2 at 13,21 %.
        (date(2023, 9, 26), 0.1301, 6_462_309.75, 6_466_978.44),
    ],
)
def test_key_rate_repo_eqm06_example(
    valuation: date, risk_rate: float, known_amount: float, end_amount: float
) -> None:
    engine = _key_rate_engine(valuation, _flat_key_rate(valuation, risk_rate))
    result = engine.valuation()
    # The report's day d is our valuation date d + 1: fixings strictly before it.
    assert 6_449_940.0 + result.known_interest_rub == pytest.approx(known_amount, abs=0.005)
    # Projected amounts are not rounded by core; MOEX shows them to the kopeck.
    assert result.end_amount_rub == pytest.approx(end_amount, abs=0.005)


def test_key_rate_repo_eqm06_final_amounts() -> None:
    """Days 7 and 8: 6 467 681,75 and the settled 6 468 388,60 (4 days at 13,20 %, 3 at
    17,20 %), valued on 26.09 with the key rate held at 17 % (day 8) or 13 % on 27.09
    (day 7's risk parameter 13,00 %)."""
    valuation = date(2023, 9, 26)
    held = _key_rate_engine(valuation, _flat_key_rate(valuation, 0.17))
    assert held.valuation().end_amount_rub == pytest.approx(6_468_388.60, abs=0.005)
    stepped = KeyRateForwardCurve(
        valuation,
        0.17,
        date(2023, 9, 27),
        (KeyRateForwardSegment(date(2023, 9, 27), date(2023, 12, 31), 0.13, "3M"),),
    )
    day_seven = _key_rate_engine(valuation, stepped)
    assert day_seven.valuation().end_amount_rub == pytest.approx(6_467_681.75, abs=0.005)
