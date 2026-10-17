"""Swap risk benchmark (roadmap package 2, §2.7): milliseconds per position.

Builds a synthetic book of seasoned RUB swaps and values each one with
:class:`SwapRiskEngine`, sharing one :class:`ShockedCurveCache` per curve set:

- ``ois``: RUONIA OIS on the legacy spec, single curve on a 17-tenor OIS quote set
  (quote-shift risk, rebootstrapped curves shared across positions), 1-10 years,
  started up to a year ago with known RUONIA fixings;
- ``krs``: key-rate swaps on the MOEX KEYRATE spec, two curves: OIS quotes for
  discounting and a calibrated key-rate forward curve for projection, 1-5 years,
  started up to a year ago with known key-rate fixings.

Modes: ``first`` (valuation and central bucket DV01 per curve), ``hessian`` (plus
the full central Hessian: 11 factors for OIS, 22 for KRS; cold curve cache) and
``hessian-warm`` (the same job again on the filled cache, as when the backend seeds
persisted shocked curves). Run from the repository root::

    uv run python benchmarks/swap_bench.py --positions 2000

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
    FixingHistory,
    InterestRateSwap,
    KeyRateCurveBuildRequest,
    KeyRateForwardCurve,
    KeyRateSwapQuote,
    OisQuote,
    ShockedCurveCache,
    SwapRiskEngine,
    SwapRiskRequest,
    __version__,
    bootstrap_discount_curve,
    build_key_rate_forward_curve,
    swap_convention,
    swap_factor_risk,
)

BUSINESS_DATE = date(2026, 10, 15)
CALENDAR = BusinessCalendar(
    holidays=frozenset({date(2026, 11, 4), date(2027, 1, 1)}),
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
KRS_QUOTES = (
    ("3M", 0.165),
    ("6M", 0.16),
    ("1Y", 0.152),
    ("2Y", 0.143),
    ("3Y", 0.138),
    ("5Y", 0.134),
    ("7Y", 0.132),
)


def ois_request(curve_date: date) -> CurveBootstrapRequest:
    quotes = tuple(
        OisQuote(tenor, curve_date + timedelta(days=days), 0.137 + 0.002 * days / 365.0)
        for tenor, days in OIS_TENORS
    )
    return CurveBootstrapRequest(curve_date, quotes)


def ruonia_history(valuation: date) -> FixingHistory:
    rows = []
    day = valuation - timedelta(days=800)
    while day < valuation:
        if CALENDAR.is_business_day(day):
            rows.append((day, 0.16 + 0.0001 * (day.toordinal() % 9)))
        day += timedelta(days=1)
    return FixingHistory("ruonia", valuation, tuple(rows), CALENDAR, "bench-ruonia")


def key_rate_history(valuation: date) -> FixingHistory:
    rows = ((date(2024, 7, 29), 0.18), (date(2025, 9, 15), 0.17), (date(2026, 7, 28), 0.165))
    return FixingHistory("key_rate", valuation, rows, CALENDAR, "bench-key-rate")


def key_rate_curve(tom: date, discount: CurveBootstrapRequest) -> KeyRateForwardCurve:
    quotes = tuple(KeyRateSwapQuote(t, m - 0.0005, m, m + 0.0005) for t, m in KRS_QUOTES)
    built = build_key_rate_forward_curve(
        KeyRateCurveBuildRequest(
            BUSINESS_DATE,
            0.165,
            date(2026, 10, 27),
            quotes,
            bootstrap_discount_curve(discount),
            CALENDAR,
        )
    )
    return built.mid.curve


def ois_book(size: int) -> list[SwapRiskRequest]:
    convention = swap_convention("rub_ruonia_ois_legacy_v1")
    curve = ois_request(BUSINESS_DATE)
    history = ruonia_history(BUSINESS_DATE)
    requests = []
    for i in range(size):
        start = BUSINESS_DATE - timedelta(days=(i * 37) % 365)
        swap = InterestRateSwap(
            f"OIS-{i:05d}",
            "payer" if i % 2 else "receiver",
            1e8 * (1 + i % 7),
            0.13 + 0.0005 * (i % 20),
            start,
            start + timedelta(days=365 * (1 + i % 10)),
            convention,
        )
        requests.append(SwapRiskRequest(swap, CALENDAR, curve, None, history))
    return requests


def krs_book(size: int) -> list[SwapRiskRequest]:
    convention = swap_convention("rub_key_rate_swap_v1")
    tom = CALENDAR.add_business_days(BUSINESS_DATE, 1)
    discount = ois_request(tom)
    projection = key_rate_curve(tom, discount)
    history = key_rate_history(tom)
    requests = []
    for i in range(size):
        trade = BUSINESS_DATE - timedelta(days=(i * 41) % 365)
        swap = InterestRateSwap.from_tenor(
            swap_id=f"KRS-{i:05d}",
            side="payer" if i % 2 else "receiver",
            notional=1e8 * (1 + i % 5),
            fixed_rate=0.14 + 0.0005 * (i % 20),
            trade_date=trade,
            tenor=f"{2 + i % 4}Y",
            calendar=CALENDAR,
            convention=convention,
        )
        requests.append(SwapRiskRequest(swap, CALENDAR, discount, projection, history))
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
    product: str, mode: str, requests: list[SwapRiskRequest], cache: ShockedCurveCache
) -> Timing:
    ids = ("RUB_OIS", None) if product == "ois" else ("RUB_OIS", "RUB_KEYRATE")
    started = time.perf_counter()
    for request in requests:
        engine = SwapRiskEngine(request, cache)
        engine.valuation()
        risk = engine.risk()
        second = engine.second_order() if mode != "first" else None
        swap_factor_risk(
            risk, discount_curve_id=ids[0], projection_curve_id=ids[1], second_order=second
        )
    return Timing(product, mode, len(requests), time.perf_counter() - started)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--positions", type=int, default=2000, help="total, split OIS/KRS")
    parser.add_argument("--json", type=Path, help="also write the timings as JSON")
    args = parser.parse_args(argv)
    half = max(1, args.positions // 2)
    books = {"ois": ois_book(half), "krs": krs_book(half)}
    timings = []
    for product in ("ois", "krs"):
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
