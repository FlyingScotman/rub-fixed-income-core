"""History benchmark (desk-monitor review): bonds x past dates, milliseconds per bond-date.

The Trading Desk Monitor recomputes past dates for its history pages (C11): hundreds of
bonds on hundreds of dates. This benchmark values the synthetic fixed-coupon book of
``portfolio_bench`` (bullets, amortizers, confirmed puts) on ``--dates`` consecutive
business days, each with its own KBD publication (the archived 15 October 2026 parameters
re-dated, with a parallel move per day):

- ``batch``: ``bond_metrics_batch`` per date (yield, Macaulay and modified duration,
  G-spread and Z-spread to the KBD formula), inputs built with
  ``BondMetricsInput.from_instrument``; the input build is timed separately;
- ``scalar``: the same numbers from the scalar functions (``solve_effective_annual_yield``,
  ``bond_duration``, ``g_spread_bp``, ``solve_z_spread`` on ``kbd_discount_curve``);
- ``portfolio``: ``value_portfolio`` first order (PV, Z, bucket DV01, CS, yield, G) per date
  on the first ``--portfolio-dates`` dates only, extrapolated per bond-date.

Run from the repository root::

    uv run python benchmarks/history_bench.py --bonds 300 --dates 250
"""

from __future__ import annotations

import argparse
import json
import platform
import sys
import time
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from portfolio_bench import CALENDAR, book, kbd_parameters

from rub_fixed_income import (
    BondMetricsInput,
    CalendarEntry,
    CurveEntry,
    MarketSnapshot,
    ValuationPolicy,
    YieldRequest,
    ZcycParameters,
    __version__,
    bond_duration,
    bond_metrics_batch,
    g_spread_bp,
    kbd_discount_curve,
    solve_effective_annual_yield,
    solve_z_spread,
    value_portfolio,
)


def dates(count: int, start: date) -> list[date]:
    out: list[date] = []
    day = start
    while len(out) < count:
        if CALENDAR.is_business_day(day):
            out.append(day)
        day += timedelta(days=1)
    return out


def kbd_on(base: ZcycParameters, day: date, k: int) -> ZcycParameters:
    return replace(base, trade_date=day, b1=base.b1 + 2.0 * ((k % 11) - 5))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--bonds", type=int, default=300)
    parser.add_argument("--dates", type=int, default=250)
    parser.add_argument("--portfolio-dates", type=int, default=5)
    parser.add_argument("--scalar-dates", type=int, default=20)
    parser.add_argument("--json", type=Path)
    args = parser.parse_args(argv)
    instruments, positions, quotes = book(args.bonds)
    base = kbd_parameters()
    days = dates(args.dates, base.trade_date)
    prices = {q.instrument_id: q.clean_price_pct for q in quotes}
    timings: dict[str, float] = {}

    started = time.perf_counter()
    per_date = []
    for day in days:
        settle = CALENDAR.add_business_days(day, 1)
        inputs = []
        for inst in instruments:
            if inst.maturity_date is not None and inst.maturity_date <= settle:
                continue
            inputs.append(
                BondMetricsInput.from_instrument(
                    inst, settle, prices[inst.instrument_id], horizon="confirmed_put"
                )
            )
        per_date.append(inputs)
    timings["inputs"] = time.perf_counter() - started
    count = sum(len(i) for i in per_date)

    started = time.perf_counter()
    solved = 0
    for k, (day, inputs) in enumerate(zip(days, per_date, strict=True)):
        params = kbd_on(base, day, k)
        metrics = bond_metrics_batch(inputs, kbd=params, curve=params)
        solved += sum(1 for r in metrics if r.error is None)
    timings["batch"] = time.perf_counter() - started

    started = time.perf_counter()
    scalar_count = 0
    for k, (day, inputs) in enumerate(zip(days[: args.scalar_dates], per_date, strict=False)):
        params = kbd_on(base, day, k)
        curve = kbd_discount_curve(params)
        for inp in inputs:
            y = solve_effective_annual_yield(
                YieldRequest(inp.settlement_date, inp.dirty_price, inp.cashflows)
            ).effective_annual_yield
            pairs = tuple((f.payment_date, f.amount) for f in inp.cashflows)
            d = bond_duration(annual_yield=y, settlement_date=inp.settlement_date, cashflows=pairs)
            g_spread_bp(params, y, d.macaulay_years)
            solve_z_spread(pairs, inp.dirty_price, curve, settlement_date=inp.settlement_date)
            scalar_count += 1
    timings["scalar"] = time.perf_counter() - started

    started = time.perf_counter()
    portfolio_count = 0
    policy = ValuationPolicy("KBD", horizon="confirmed_put", include_second_order=False)
    for k, day in enumerate(days[: args.portfolio_dates]):
        as_of = datetime(day.year, day.month, day.day, 16, 30, tzinfo=UTC)
        snapshot = MarketSnapshot(
            f"history-{day}",
            day,
            as_of,
            curves=(CurveEntry("KBD", "rub_ofz_kbd_v1", kbd_on(base, day, k)),),
            calendars=(CalendarEntry("MOEX_STOCK", CALENDAR),),
            quotes=tuple(quotes),
        )
        result = value_portfolio(
            run_id=f"history-{day}",
            as_of=as_of,
            snapshot=snapshot,
            instruments=instruments,
            positions=positions,
            policy=policy,
        )
        portfolio_count += result.coverage.completed
    timings["portfolio"] = time.perf_counter() - started

    rows = [
        ("inputs (from_instrument)", count, timings["inputs"]),
        ("batch metrics", count, timings["batch"]),
        ("scalar metrics", scalar_count, timings["scalar"]),
        ("value_portfolio first order", portfolio_count, timings["portfolio"]),
    ]
    print(
        f"rub-fixed-income-core {__version__}, Python {platform.python_version()}, "
        f"{platform.system()} {platform.machine()}, {args.bonds} bonds x {args.dates} dates"
    )
    print("| path | bond-dates | total s | ms/bond-date | projected s for all bond-dates |")
    print("|---|---|---|---|---|")
    for name, n, seconds in rows:
        per = 1000.0 * seconds / n if n else float("nan")
        print(f"| {name} | {n} | {seconds:.2f} | {per:.3f} | {per * count / 1000.0:.1f} |")
    if args.json is not None:
        args.json.write_text(
            json.dumps(
                {
                    "library_version": __version__,
                    "python": platform.python_version(),
                    "bonds": args.bonds,
                    "dates": args.dates,
                    "bond_dates": count,
                    "solved": solved,
                    "rows": [{"path": n, "count": c, "seconds": s} for n, c, s in rows],
                },
                indent=2,
            ),
            encoding="utf-8",
        )
    # The synthetic book keeps each bond's clean price on every date, so a bond days from
    # maturity can imply a yield outside the scalar solver's bracket (-99 %, 500 %): such a
    # bond-date gets None and a reason, as the scalar solver raises. Not a failure.
    print(f"solved {solved} of {count} bond-dates; the rest have no yield in (-99 %, 500 %)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
