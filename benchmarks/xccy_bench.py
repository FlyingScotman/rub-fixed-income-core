"""Cross-currency swap risk benchmark (package 7): milliseconds per position.

Builds a synthetic book of CNY/RUB XCS CNYRUB swaps (CNY fixed against RUONIA, both
notionals exchanged): half seasoned (started up to two years ago, with a RUONIA fixing
history), half traded on the business date, tenors 3M to 5Y, both sides. All positions
share one RUB curve and one CNY curve:

- ``zero``: RUB discount factors and the bootstrapped CNY discount factors
  (``continuous_zero`` risk on both);
- ``quotes``: 17 RUONIA OIS quotes (rebootstrapped under shocks) and the 10 XCCY par
  quotes (``xccy_quote``: the CNY curve is rebuilt under every RUB and quote shock).

One ``ShockedCurveCache`` and one ``XccyCurveCache`` are shared by every position of a
run. Modes: ``first`` (PV and central first order), ``hessian`` (plus the full Hessian
and desk factors), ``scenarios`` (plus five exact scenarios).

Run from the repository root::

    uv run python benchmarks/xccy_bench.py --positions 2000
"""

from __future__ import annotations

import argparse
import math
import platform
import time
from dataclasses import dataclass
from datetime import date, timedelta

from rub_fixed_income import (
    BusinessCalendar,
    CurveBootstrapRequest,
    DiscountCurve,
    FixingHistory,
    OisQuote,
    ShockedCurveCache,
    XccyCurveBootstrapRequest,
    XccyCurveCache,
    XccyParQuote,
    XccyRiskEngine,
    XccyRiskRequest,
    XccyScenario,
    XccySwap,
    __version__,
    bootstrap_discount_curve,
    bootstrap_xccy_curve,
    standard_xccy_swap,
    xccy_factor_risk,
    xcs_cnyrub_convention,
)

BUSINESS_DATE = date(2026, 10, 15)
CALENDAR = BusinessCalendar(frozenset(), frozenset(), "bench", date(2023, 1, 1), date(2033, 12, 31))
CONVENTION = xcs_cnyrub_convention(CALENDAR)
SPOT_DATE = CALENDAR.add_business_days(BUSINESS_DATE, 1)
SPOT = 11.2
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
XCCY_QUOTES = (
    ("1W", 0.020),
    ("1M", 0.0205),
    ("3M", 0.021),
    ("6M", 0.022),
    ("9M", 0.0228),
    ("1Y", 0.0235),
    ("2Y", 0.0255),
    ("3Y", 0.027),
    ("4Y", 0.0282),
    ("5Y", 0.029),
)
TENORS = ("3M", "6M", "1Y", "2Y", "3Y", "4Y", "5Y")
SCENARIOS = (
    XccyScenario.parallel(rub_shift_bp=100.0),
    XccyScenario.parallel(rub_shift_bp=-100.0),
    XccyScenario.parallel(foreign_shift_bp=50.0),
    XccyScenario.parallel(fx_spot_shift_pct=-10.0),
    XccyScenario.parallel(rub_shift_bp=200.0, fx_spot_shift_pct=15.0),
)


def rub_ois() -> CurveBootstrapRequest:
    quotes = tuple(
        OisQuote(tenor, BUSINESS_DATE + timedelta(days=days), 0.137 + 0.002 * days / 365.0)
        for tenor, days in OIS_TENORS
    )
    return CurveBootstrapRequest(BUSINESS_DATE, quotes)


def xccy_request() -> XccyCurveBootstrapRequest:
    return XccyCurveBootstrapRequest(
        BUSINESS_DATE,
        "CNY",
        SPOT,
        SPOT_DATE,
        11.25,
        tuple(XccyParQuote(t, r) for t, r in XCCY_QUOTES),
        CONVENTION,
    )


def history() -> FixingHistory:
    rows = []
    day = date(2024, 9, 1)
    while day < BUSINESS_DATE:
        if CALENDAR.is_business_day(day):
            rows.append((day, 0.16 + 0.01 * math.sin(day.toordinal() / 30.0)))
        day += timedelta(days=1)
    return FixingHistory("ruonia", BUSINESS_DATE, tuple(rows), CALENDAR, "bench-ruonia")


def book(size: int) -> list[XccySwap]:
    trades = []
    for i in range(size):
        tenor = TENORS[i % len(TENORS)]
        seasoned = i % 2 == 1
        trade_date = (
            BUSINESS_DATE - timedelta(days=30 + (i * 53) % 700) if seasoned else BUSINESS_DATE
        )
        while not CALENDAR.is_business_day(trade_date):
            trade_date -= timedelta(days=1)
        swap = standard_xccy_swap(
            f"X{i}",
            trade_date,
            tenor,
            CONVENTION,
            currency="CNY",
            foreign_notional=1_000_000.0 * (1 + i % 9),
            notional_fx_rate=11.0 + 0.01 * (i % 50),
            fixed_rate=0.02 + 0.0005 * (i % 21),
            foreign_side="receive" if i % 3 else "pay",
        )
        if swap.rub_leg.periods[-1].payment_date <= BUSINESS_DATE:
            swap = standard_xccy_swap(
                f"X{i}",
                BUSINESS_DATE,
                tenor,
                CONVENTION,
                currency="CNY",
                foreign_notional=1_000_000.0,
                notional_fx_rate=11.25,
                fixed_rate=0.025,
                foreign_side="receive",
            )
        trades.append(swap)
    return trades


@dataclass(frozen=True)
class Timing:
    curves: str
    mode: str
    positions: int
    seconds: float

    @property
    def ms_per_position(self) -> float:
        return 1000.0 * self.seconds / self.positions


def run_case(curves: str, mode: str, trades: list[XccySwap], fixings: FixingHistory) -> Timing:
    ois = rub_ois()
    rub_zero = bootstrap_discount_curve(ois)
    request = xccy_request()
    rub: DiscountCurve | CurveBootstrapRequest = ois if curves == "quotes" else rub_zero
    cny: DiscountCurve | XccyCurveBootstrapRequest = (
        request
        if curves == "quotes"
        else bootstrap_xccy_curve(request, rub_discount_curve=rub_zero)
    )
    ids = {"rub_discount": "RUB_RUONIA_OIS", "foreign_discount": "CNY_XCCY"}
    cache, xcache = ShockedCurveCache(), XccyCurveCache()
    started = time.perf_counter()
    for swap in trades:
        engine = XccyRiskEngine(
            XccyRiskRequest(swap, SPOT, SPOT_DATE, rub, cny, 0.0, rub_fixings=fixings),
            cache,
            xcache,
        )
        risk = engine.risk()
        if mode == "first":
            xccy_factor_risk(risk, ids)
            continue
        xccy_factor_risk(risk, ids, engine.second_order())
        if mode == "scenarios":
            for scenario in SCENARIOS:
                engine.reprice(scenario)
    return Timing(curves, mode, len(trades), time.perf_counter() - started)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--positions", type=int, default=2000)
    args = parser.parse_args(argv)
    trades = book(args.positions)
    fixings = history()
    timings = [
        run_case(curves, mode, trades, fixings)
        for curves in ("zero", "quotes")
        for mode in ("first", "hessian", "scenarios")
    ]
    print(
        f"rub-fixed-income-core {__version__}, Python {platform.python_version()}, "
        f"{platform.system()} {platform.machine()}, {args.positions} positions"
    )
    print("| curves | mode | total s | ms/position |")
    print("|---|---|---|---|")
    for t in timings:
        print(f"| {t.curves} | {t.mode} | {t.seconds:.2f} | {t.ms_per_position:.2f} |")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
