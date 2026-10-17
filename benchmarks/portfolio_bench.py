"""Portfolio valuation benchmark (roadmap 0.7): milliseconds per position.

Builds a synthetic book of distinct fixed-coupon RUB bonds (bullets, amortizers and
confirmed puts, 1-15 years) and values it with ``value_portfolio`` on the archived
KBD publication of 15 October 2026 and on a 17-tenor RUONIA OIS quote set:

- ``first``: PV, spread and first-order desk-factor risk;
- ``hessian``: plus the 12x12 Hessian per position;
- ``scenarios``: Hessian plus five curve scenarios;
- ``warm``: OIS only, the same job with a cache seeded from a previous job's
  :meth:`ShockedCurveCache.export` (the backend's persisted curves).

Run from the repository root::

    uv run python benchmarks/portfolio_bench.py --positions 2000

The numbers are machine-dependent; the target is <= 10 ms per position amortised
for OIS with the Hessian at 2k positions.
"""

from __future__ import annotations

import argparse
import json
import platform
import sys
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from rub_fixed_income import (
    AccountingCategory,
    BondPriceObservation,
    BondScenario,
    BusinessCalendar,
    CalendarEntry,
    CashflowKind,
    CouponPeriod,
    CurveBootstrapRequest,
    CurveEntry,
    FixedCouponLeg,
    Instrument,
    MarketSnapshot,
    Offer,
    OfferKind,
    OisQuote,
    PortfolioScenario,
    Position,
    PrincipalLeg,
    ProductType,
    Redemption,
    ShockedCurveCache,
    ValuationPolicy,
    ZcycParameters,
    __version__,
    value_portfolio,
)
from rub_fixed_income.dates import add_months

ROOT = Path(__file__).resolve().parents[1]
KBD_FIXTURE = ROOT / "tests" / "fixtures" / "kbd" / "zcyc_2026-10-15.json"
BUSINESS_DATE = date(2026, 10, 15)
AS_OF = datetime(2026, 10, 15, 16, 30, tzinfo=UTC)
CALENDAR = BusinessCalendar(
    holidays=frozenset({date(2026, 11, 4)}),
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
SCENARIOS = (
    PortfolioScenario("parallel+100", BondScenario.parallel(100.0)),
    PortfolioScenario("parallel-100", BondScenario.parallel(-100.0)),
    PortfolioScenario("steepener", BondScenario(tuple(-50.0 + 10.0 * i for i in range(11)))),
    PortfolioScenario("flattener", BondScenario(tuple(50.0 - 10.0 * i for i in range(11)))),
    PortfolioScenario("spread+50", BondScenario.parallel(0.0, spread_shift_bp=50.0)),
)


def kbd_parameters() -> ZcycParameters:
    block = json.loads(KBD_FIXTURE.read_text(encoding="utf-8"))["params"]
    row = dict(zip(block["columns"], block["data"][0], strict=True))
    return ZcycParameters(
        date.fromisoformat(row["tradedate"]),
        row["B1"],
        row["B2"],
        row["B3"],
        row["T1"],
        tuple(row[f"G{i}"] for i in range(1, 10)),
    )


def ois_request() -> CurveBootstrapRequest:
    quotes = tuple(
        OisQuote(tenor, BUSINESS_DATE + timedelta(days=days), 0.137 + 0.002 * days / 365.0)
        for tenor, days in OIS_TENORS
    )
    return CurveBootstrapRequest(BUSINESS_DATE, quotes)


def book(size: int) -> tuple[list[Instrument], list[Position], list[BondPriceObservation]]:
    instruments, positions, quotes = [], [], []
    for i in range(size):
        years = 1 + i % 15
        first = add_months(date(2026, 4, 15), -(i % 6))
        coupon = 30.0 + (i % 9) * 5.0
        periods = tuple(
            CouponPeriod(
                add_months(first, 6 * k),
                add_months(first, 6 * (k + 1)),
                add_months(first, 6 * (k + 1)),
                coupon,
            )
            for k in range(2 * years + 1)
        )
        end = periods[-1].payment_date
        kind = i % 3
        redemptions: tuple[Redemption, ...] = (Redemption(end, 1000.0),)
        offers: tuple[Offer, ...] = ()
        if kind == 1 and len(periods) > 4:
            mid = periods[len(periods) // 2].payment_date
            redemptions = (
                Redemption(mid, 400.0, CashflowKind.AMORTIZATION),
                Redemption(end, 600.0),
            )
        elif kind == 2 and len(periods) > 4:
            offers = (Offer(periods[3].payment_date, OfferKind.PUT, confirmed=True),)
        instrument_id = f"BOND-{i:05d}"
        instruments.append(
            Instrument(
                instrument_id,
                ProductType.FIXED_BOND,
                "RUB",
                1000.0,
                FixedCouponLeg(periods),
                PrincipalLeg(redemptions),
                "MOEX_STOCK",
                offers=offers,
            )
        )
        positions.append(
            Position(
                f"pos-{i:05d}",
                instrument_id,
                float(100 + i % 50),
                "RATES",
                AccountingCategory.TRADING,
                "RUB",
                0.3,
            )
        )
        quotes.append(BondPriceObservation(instrument_id, 95.0 + (i % 11) * 0.5, "waprice"))
    return instruments, positions, quotes


@dataclass(frozen=True)
class Timing:
    curve: str
    mode: str
    positions: int
    computed: int
    seconds: float

    @property
    def ms_per_position(self) -> float:
        return 1000.0 * self.seconds / self.positions


def run_case(
    curve: str, mode: str, size: int, cache: ShockedCurveCache | None = None
) -> tuple[Timing, ShockedCurveCache]:
    instruments, positions, quotes = book(size)
    snapshot = MarketSnapshot(
        f"bench-{BUSINESS_DATE}",
        BUSINESS_DATE,
        AS_OF,
        curves=(
            CurveEntry("RUB_OFZ_KBD", "rub_ofz_kbd_v1", kbd_parameters()),
            CurveEntry("RUB_OIS", "rub_ruonia_ois_legacy_v1", ois_request()),
        ),
        calendars=(CalendarEntry("MOEX_STOCK", CALENDAR),),
        quotes=tuple(quotes),
    )
    policy = ValuationPolicy(
        "RUB_OFZ_KBD" if curve == "kbd" else "RUB_OIS",
        horizon="confirmed_put",
        include_second_order=mode != "first",
    )
    cache = ShockedCurveCache() if cache is None else cache
    started = time.perf_counter()
    result = value_portfolio(
        run_id=f"bench-{curve}-{mode}",
        as_of=AS_OF,
        snapshot=snapshot,
        instruments=instruments,
        positions=positions,
        policy=policy,
        scenarios=SCENARIOS if mode == "scenarios" else (),
        curve_cache=cache,
    )
    elapsed = time.perf_counter() - started
    return Timing(curve, mode, size, result.coverage.completed, elapsed), cache


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--positions", type=int, default=2000)
    parser.add_argument("--json", type=Path, help="also write the timings as JSON")
    args = parser.parse_args(argv)
    size = args.positions
    timings = []
    for curve in ("kbd", "ois"):
        for mode in ("first", "hessian", "scenarios"):
            timing, cache = run_case(curve, mode, size)
            timings.append(timing)
            if curve == "ois" and mode == "hessian":
                warm_cache = ShockedCurveCache()
                warm_cache.seed(cache.export())
                warm, _ = run_case(curve, "warm", size, warm_cache)
                timings.append(warm)
    print(
        f"rub-fixed-income-core {__version__}, Python {platform.python_version()}, "
        f"{platform.system()} {platform.machine()}, {size} positions"
    )
    print("| curve | mode | computed | total s | ms/position |")
    print("|---|---|---|---|---|")
    for t in timings:
        print(
            f"| {t.curve} | {t.mode} | {t.computed} | {t.seconds:.2f} | {t.ms_per_position:.2f} |"
        )
    if args.json is not None:
        args.json.write_text(
            json.dumps(
                {
                    "library_version": __version__,
                    "python": platform.python_version(),
                    "platform": f"{platform.system()} {platform.machine()}",
                    "positions": size,
                    "timings": [
                        {
                            "curve": t.curve,
                            "mode": t.mode,
                            "computed": t.computed,
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
    incomplete = [t for t in timings if t.computed != t.positions]
    return 1 if incomplete else 0


if __name__ == "__main__":
    sys.exit(main())
