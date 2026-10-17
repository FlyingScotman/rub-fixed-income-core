"""Inflation-linked bond benchmark (package 9a, roadmap §2.7): milliseconds per position.

Builds a synthetic book of distinct OFZ-IN-like linkers (2.5 % semi-annual coupons on
the CPI-indexed face, placed 2018-2025, 1-15 years to maturity, deflation floor) and
values it with ``value_portfolio`` on one caller-built real curve
(``rub_real_discount_factors_v1``) and one monthly CPI series in the snapshot:

- ``first``: indexed face and accrued from CPI, PV, spread and first-order
  ``REAL|<curve>|<bucket>`` and ``CS`` risk;
- ``hessian``: plus the 12x12 Hessian per position.

The real curve is a zero-shift (``continuous_zero``) curve, so there is no bootstrap
to share; the cost is the per-position indexation, flows and risk repricing. Run from
the repository root::

    uv run python benchmarks/linker_bench.py --positions 2000

The numbers are machine-dependent.
"""

from __future__ import annotations

import argparse
import json
import math
import platform
import time
from datetime import UTC, date, datetime, timedelta
from itertools import pairwise

from rub_fixed_income import (
    AccountingCategory,
    BondPriceObservation,
    BusinessCalendar,
    CalendarEntry,
    CpiIndexation,
    CpiObservation,
    CpiSeries,
    CurveEntry,
    DayCount,
    DiscountCurve,
    IndexedCouponLeg,
    IndexedCouponPeriod,
    IndexedPrincipalLeg,
    Instrument,
    MarketSnapshot,
    Position,
    ProductType,
    ValuationPolicy,
    __version__,
    value_portfolio,
)
from rub_fixed_income.dates import add_months
from rub_fixed_income.inflation import add_months as add_cpi_months

BUSINESS_DATE = date(2026, 10, 15)
CALENDAR = BusinessCalendar(version="synthetic-weekends-only")
CPI_ID = "RU_CPI_SYNTHETIC"


def cpi_series() -> CpiSeries:
    start = date(2017, 1, 1)
    rows = []
    level = 100.0
    month = start
    while month <= date(2026, 9, 1):
        rows.append(CpiObservation(month, level, add_cpi_months(month, 1).replace(day=12)))
        level *= 1.0 + 0.004 + 0.002 * math.sin(month.month)
        month = add_cpi_months(month, 1)
    return CpiSeries(CPI_ID, "synthetic", BUSINESS_DATE, tuple(rows))


def real_curve() -> DiscountCurve:
    nodes = tuple(
        (BUSINESS_DATE + timedelta(days=round(365.25 * k / 4)), math.exp(-0.06 * k / 4))
        for k in range(1, 4 * 31)
    )
    return DiscountCurve(BUSINESS_DATE, nodes)


def linker(index: int) -> tuple[Instrument, float]:
    placed = date(2018 + index % 8, 1 + index % 12, 1 + index % 27)
    target = BUSINESS_DATE + timedelta(days=365 * (1 + index % 15))
    dates = [placed]
    while dates[-1] < target:
        dates.append(add_months(placed, 6 * len(dates)))
    periods = tuple(IndexedCouponPeriod(s, e, e, 0.025) for s, e in pairwise(dates))
    bond = Instrument(
        f"LINKER{index:05d}",
        ProductType.INFLATION_LINKED_BOND,
        "RUB",
        1000.0,
        IndexedCouponLeg(periods, DayCount.ACT_365_FIXED),
        IndexedPrincipalLeg(dates[-1], CpiIndexation(CPI_ID, placed, 3), True),
        "MOEX_STOCK",
    )
    return bond, 80.0 + index % 20


def run(count: int, second_order: bool) -> float:
    pairs = [linker(i) for i in range(count)]
    instruments = [b for b, _ in pairs]
    snapshot = MarketSnapshot(
        "bench",
        BUSINESS_DATE,
        datetime(2026, 10, 15, 19, 0, tzinfo=UTC),
        curves=(CurveEntry("RUB_REAL", "rub_real_discount_factors_v1", real_curve()),),
        calendars=(CalendarEntry("MOEX_STOCK", CALENDAR),),
        quotes=tuple(BondPriceObservation(b.instrument_id, p, "CLOSE") for b, p in pairs),
        cpi=(cpi_series(),),
    )
    positions = [
        Position(f"P{i:05d}", b.instrument_id, 100.0, "bench", AccountingCategory.TRADING, "RUB")
        for i, b in enumerate(instruments)
    ]
    policy = ValuationPolicy(
        "RUB_REAL",
        spread_kind="ofz",
        include_second_order=second_order,
    )
    started = time.perf_counter()
    result = value_portfolio(
        run_id="linker-bench",
        as_of=datetime(2026, 10, 15, 20, 0, tzinfo=UTC),
        snapshot=snapshot,
        instruments=instruments,
        positions=positions,
        policy=policy,
    )
    elapsed = time.perf_counter() - started
    if result.coverage.status != "complete":
        raise SystemExit(f"benchmark book not fully covered: {result.coverage.rejections[:3]}")
    return elapsed * 1000.0 / count


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--positions", type=int, default=2000)
    parser.add_argument("--json", action="store_true", help="print one JSON object")
    args = parser.parse_args()
    rows = {
        "first": run(args.positions, second_order=False),
        "hessian": run(args.positions, second_order=True),
    }
    meta = {
        "library_version": __version__,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "positions": args.positions,
    }
    if args.json:
        print(json.dumps({**meta, "ms_per_position": rows}))
        return
    print(f"linkers, {args.positions} positions, {meta['python']} on {meta['platform']}")
    for mode, ms in rows.items():
        print(f"  {mode:8s} {ms:7.2f} ms/position")


if __name__ == "__main__":
    main()
