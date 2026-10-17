"""Repo leg risk benchmark (roadmap package 6, §2.7): milliseconds per position.

Builds a synthetic book of outstanding RUB repo legs and values each one with
:class:`RepoRiskEngine`, sharing one :class:`ShockedCurveCache` per job:

- ``fixed``: fixed-rate term repos and reverse repos (ACT/365-366), 1 day to 1 year,
  started up to 90 days ago, discounted on a 17-tenor OIS quote set (quote-shift risk,
  rebootstrapped curves shared across positions); 11 factors;
- ``floating``: RUSFAR repos (simple and capitalised, every third one an open repo
  with an assumed end), two curves: the same OIS quotes for discounting and a
  RUSFAR projection curve (zero-rate overlay), with known RUSFAR fixings; 22 factors.

Modes: ``first`` (valuation and central bucket DV01 per curve), ``hessian`` (plus the
full central Hessian; cold curve cache) and ``hessian-warm`` (the same job again on
the filled cache). Run from the repository root::

    uv run python benchmarks/repo_bench.py --positions 2000

The numbers are machine-dependent.
"""

from __future__ import annotations

import argparse
import json
import platform
import time
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from rub_fixed_income import (
    BusinessCalendar,
    CurveBootstrapRequest,
    DayCount,
    DiscountCurve,
    FixedRepoRate,
    FixingHistory,
    FloatingRepoRate,
    OisQuote,
    RepoCollateral,
    RepoDayBasis,
    RepoRiskEngine,
    RepoRiskRequest,
    RepoTrade,
    ShockedCurveCache,
    __version__,
    repo_factor_risk,
)

BUSINESS_DATE = date(2026, 10, 15)
CALENDAR = BusinessCalendar(
    holidays=frozenset({date(2026, 11, 4), date(2027, 1, 1), date(2027, 1, 7)}),
    version="synthetic-benchmark-2026",
)
OIS_TENORS = (
    ("1W", 7),
    ("2W", 14),
    ("1M", 30),
    ("2M", 61),
    ("3M", 91),
    ("6M", 182),
    ("9M", 273),
    ("1Y", 365),
    *((f"{y}Y", 365 * y) for y in range(2, 11)),
)
TERMS = (1, 7, 14, 30, 61, 91, 182, 365)


def ois_request(curve_date: date) -> CurveBootstrapRequest:
    quotes = tuple(
        OisQuote(tenor, curve_date + timedelta(days=days), 0.137 + 0.002 * days / 365.0)
        for tenor, days in OIS_TENORS
    )
    return CurveBootstrapRequest(curve_date, quotes)


def rusfar_history(valuation: date) -> FixingHistory:
    rows = []
    day = valuation - timedelta(days=120)
    while day < valuation:
        if CALENDAR.is_business_day(day):
            rows.append((day, 0.138 + 0.0001 * (day.toordinal() % 9)))
        day += timedelta(days=1)
    return FixingHistory("ruonia", valuation, tuple(rows), CALENDAR, "bench-rusfar")


def _settlement(day: date) -> date:
    while not CALENDAR.is_business_day(day):
        day += timedelta(days=1)
    return day


def _end(start: date, days: int) -> date:
    end = _settlement(start + timedelta(days=days))
    floor = CALENDAR.add_business_days(BUSINESS_DATE, 1)
    return end if end > BUSINESS_DATE else floor


def fixed_book(size: int) -> list[RepoRiskRequest]:
    curve = ois_request(BUSINESS_DATE)
    requests = []
    for i in range(size):
        start = _settlement(BUSINESS_DATE - timedelta(days=(i * 13) % 90))
        start = min(start, BUSINESS_DATE)
        trade = RepoTrade(
            f"FIX-{i:05d}",
            "reverse_repo" if i % 2 else "repo",
            start,
            _end(start, TERMS[i % len(TERMS)]),
            1e8 * (1 + i % 7),
            FixedRepoRate(0.14 + 0.0005 * (i % 20), RepoDayBasis.ACT_365_366),
            2,
            RepoCollateral("SU26238RMFS4", 1e5 * (1 + i % 7)),
        )
        requests.append(RepoRiskRequest(trade, CALENDAR, curve))
    return requests


def floating_book(size: int) -> list[RepoRiskRequest]:
    discount = ois_request(BUSINESS_DATE)
    projection = DiscountCurve(
        BUSINESS_DATE,
        tuple(
            (BUSINESS_DATE + timedelta(days=days), (1 + 0.14 * days / 365.0) ** -1.0)
            for days in (7, 30, 91, 182, 365, 730)
        ),
    )
    history = rusfar_history(BUSINESS_DATE)
    requests = []
    for i in range(size):
        start = _settlement(BUSINESS_DATE - timedelta(days=(i * 11) % 90))
        start = min(start, BUSINESS_DATE)
        open_repo = i % 3 == 0
        end = _end(start, TERMS[1 + i % (len(TERMS) - 1)])
        rate = FloatingRepoRate(
            "RUSFAR",
            0.001 * (i % 5),
            RepoDayBasis.ACT_365_366,
            "previous_settlement_day",
            DayCount.ACT_365_FIXED,
            "capitalised" if open_repo else "simple",
            nonpositive_rate_replacement=0.0001,
        )
        trade = RepoTrade(
            f"FLT-{i:05d}",
            "reverse_repo" if i % 2 else "repo",
            start,
            None if open_repo else end,
            1e8 * (1 + i % 5),
            rate,
            2,
            RepoCollateral("RU000A0JW4Z1", 1e8 * (1 + i % 5), pool="GC Bonds"),
        )
        requests.append(
            RepoRiskRequest(
                trade,
                CALENDAR,
                discount,
                projection,
                history,
                assumed_end_date=end if open_repo else None,
            )
        )
    return requests


@dataclass(frozen=True)
class Timing:
    product: str
    mode: str
    positions: int
    seconds: float

    @property
    def ms_per_position(self) -> float:
        return 1000.0 * self.seconds / self.positions


def run_case(
    product: str, mode: str, requests: list[RepoRiskRequest], cache: ShockedCurveCache
) -> Timing:
    projection_id = None if product == "fixed" else "RUB_RUSFAR"
    started = time.perf_counter()
    for request in requests:
        engine = RepoRiskEngine(request, cache)
        engine.valuation()
        risk = engine.risk()
        second = engine.second_order() if mode != "first" else None
        repo_factor_risk(
            risk,
            discount_curve_id="RUB_OIS",
            projection_curve_id=projection_id,
            second_order=second,
        )
    return Timing(product, mode, len(requests), time.perf_counter() - started)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--positions", type=int, default=2000, help="total, split fixed/floating")
    parser.add_argument("--json", type=Path, help="also write the timings as JSON")
    args = parser.parse_args(argv)
    half = max(1, args.positions // 2)
    books = {"fixed": fixed_book(half), "floating": floating_book(half)}
    timings = []
    for product in ("fixed", "floating"):
        timings.append(run_case(product, "first", books[product], ShockedCurveCache()))
        cache = ShockedCurveCache()
        timings.append(run_case(product, "hessian", books[product], cache))
        timings.append(run_case(product, "hessian-warm", books[product], cache))
    print(
        f"rub-fixed-income-core {__version__}, Python {platform.python_version()}, "
        f"{platform.system()} {platform.machine()}, {2 * half} positions"
    )
    print("| product | mode | positions | total s | ms/position |")
    print("|---|---|---|---|---|")
    for t in timings:
        print(
            f"| {t.product} | {t.mode} | {t.positions} | {t.seconds:.2f} | "
            f"{t.ms_per_position:.2f} |"
        )
    if args.json is not None:
        args.json.write_text(
            json.dumps(
                {
                    "library_version": __version__,
                    "python": platform.python_version(),
                    "platform": f"{platform.system()} {platform.machine()}",
                    "positions": 2 * half,
                    "timings": [
                        {
                            "product": t.product,
                            "mode": t.mode,
                            "positions": t.positions,
                            "seconds": t.seconds,
                            "ms_per_position": t.ms_per_position,
                        }
                        for t in timings
                    ],
                },
                indent=2,
            ),
            encoding="utf-8",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
