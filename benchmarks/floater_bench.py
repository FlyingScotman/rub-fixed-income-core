"""Floater portfolio benchmark (roadmap §2.7, package 1): milliseconds per position.

Builds a synthetic book of distinct RUB floaters (quarterly, 1-7 years, the current
coupon partly fixed): RUONIA compounded and arithmetic coupons observed over the period
shifted 6 calendar days (the OFZ-PK rule), some with a far floor priced at intrinsic
value, and key-rate coupons. It values the book with ``value_portfolio`` on the curves
of ``portfolio_bench.py`` (the archived KBD of 15 October 2026 and a 17-tenor RUONIA
OIS quote set) and a key-rate projection curve:

- ``ois``: single-curve, RUONIA OIS projects and discounts (12 factors);
- ``kbd+ois``: KBD discounts, RUONIA OIS projects (23 factors);
- ``kbd+key``: KBD discounts, the key-rate projection curve projects (23 factors);

each in the modes ``first`` (PV, spread, first-order factors) and ``hessian``.

Run from the repository root::

    uv run python benchmarks/floater_bench.py --positions 2000
"""

from __future__ import annotations

import argparse
import json
import platform
import sys
import time
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from portfolio_bench import AS_OF, BUSINESS_DATE, kbd_parameters, ois_request

from rub_fixed_income import (
    AccountingCategory,
    BondPriceObservation,
    BusinessCalendar,
    CalendarEntry,
    CouponRateBounds,
    CurveEntry,
    DayCount,
    FixingEntry,
    FixingHistory,
    FloatingCouponLeg,
    FloatingCouponPeriod,
    FloatingCouponRule,
    Instrument,
    KeyRateForwardCurve,
    KeyRateForwardSegment,
    MarketSnapshot,
    ObservationShift,
    Position,
    PrincipalLeg,
    ProductType,
    Redemption,
    ShockedCurveCache,
    ValuationPolicy,
    __version__,
    key_rate_projection_curve,
    value_portfolio,
)
from rub_fixed_income.dates import add_months

CALENDAR = BusinessCalendar(holidays=frozenset({date(2026, 11, 4)}), version="synthetic-bench")
ACT365 = DayCount.ACT_365_FIXED
SHIFT6 = ObservationShift(6, "calendar_days", "observation")
KNOWN = "2026-10-15T12:00:00Z"


def histories() -> tuple[FixingHistory, FixingHistory]:
    days: list[tuple[date, float]] = []
    day = date(2026, 4, 1)
    while day < BUSINESS_DATE:
        if CALENDAR.is_business_day(day):
            days.append((day, 0.16 - 0.00001 * len(days)))
        day += timedelta(days=1)
    ruonia = FixingHistory(
        "ruonia", BUSINESS_DATE, tuple(days), CALENDAR, "bench-ruonia", known_at_utc=KNOWN
    )
    key = FixingHistory(
        "key_rate",
        BUSINESS_DATE,
        ((date(2026, 4, 1), 0.18), (date(2026, 9, 14), 0.165)),
        CALENDAR,
        "bench-key",
        known_at_utc=KNOWN,
    )
    return ruonia, key


def key_projection() -> CurveEntry:
    forward = KeyRateForwardCurve(
        BUSINESS_DATE,
        0.165,
        date(2026, 10, 26),
        (
            KeyRateForwardSegment(date(2026, 10, 26), date(2027, 6, 1), 0.15, "1Y"),
            KeyRateForwardSegment(date(2027, 6, 1), date(2034, 1, 1), 0.13, "7Y"),
        ),
    )
    curve = key_rate_projection_curve(
        forward, CALENDAR, rate_day_count=ACT365, observation_days="business"
    )
    return CurveEntry("RUB_KEY", "rub_key_rate_projection_v1", curve)


def book(
    size: int, case: str
) -> tuple[list[Instrument], list[Position], list[BondPriceObservation]]:
    instruments, positions, quotes = [], [], []
    projection = {"ois": "RUB_OIS", "kbd+ois": "RUB_OIS", "kbd+key": "RUB_KEY"}[case]
    index = "key_rate" if case == "kbd+key" else "ruonia"
    for i in range(size):
        years = 1 + i % 7
        first = add_months(date(2026, 8, 20), -(i % 3)) + timedelta(days=i % 17)
        averaging = "arithmetic" if index == "key_rate" or i % 2 else "compounded"
        rule = FloatingCouponRule(
            index,  # type: ignore[arg-type]
            averaging,  # type: ignore[arg-type]
            0.005 + 0.0005 * (i % 9),
            ACT365,
            ACT365,
            amount_rounding_decimals=2,
            observation_shift=SHIFT6,
            bounds=CouponRateBounds("coupon_rate", floor=0.02) if i % 5 == 0 else None,
        )
        periods = tuple(
            FloatingCouponPeriod(
                add_months(first, 3 * k),
                add_months(first, 3 * (k + 1)),
                add_months(first, 3 * (k + 1)),
                1000.0,
            )
            for k in range(4 * years)
        )
        instrument_id = f"FRN-{i:05d}"
        instruments.append(
            Instrument(
                instrument_id,
                ProductType.FLOATING_BOND,
                "RUB",
                1000.0,
                FloatingCouponLeg(
                    rule, periods, "KEY_RATE" if index == "key_rate" else "RUONIA", projection
                ),
                PrincipalLeg((Redemption(periods[-1].payment_date, 1000.0),)),
                "MOEX_STOCK",
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
        quotes.append(
            BondPriceObservation(
                instrument_id, 99.0 + (i % 11) * 0.2, "waprice", accrued_per_bond=4.0 + i % 7
            )
        )
    return instruments, positions, quotes


@dataclass(frozen=True)
class Timing:
    case: str
    mode: str
    positions: int
    computed: int
    seconds: float

    @property
    def ms_per_position(self) -> float:
        return 1000.0 * self.seconds / self.positions


def run_case(case: str, mode: str, size: int) -> Timing:
    instruments, positions, quotes = book(size, case)
    ruonia, key = histories()
    snapshot = MarketSnapshot(
        f"floater-bench-{BUSINESS_DATE}",
        BUSINESS_DATE,
        AS_OF,
        curves=(
            CurveEntry("RUB_OFZ_KBD", "rub_ofz_kbd_v1", kbd_parameters()),
            CurveEntry("RUB_OIS", "rub_ruonia_ois_legacy_v1", ois_request()),
            key_projection(),
        ),
        calendars=(CalendarEntry("MOEX_STOCK", CALENDAR),),
        fixings=(FixingEntry("RUONIA", ruonia), FixingEntry("KEY_RATE", key)),
        quotes=tuple(quotes),
    )
    policy = ValuationPolicy(
        "RUB_OIS" if case == "ois" else "RUB_OFZ_KBD",
        include_second_order=mode == "hessian",
        cap_floor_guard_bp=50.0,
    )
    started = time.perf_counter()
    result = value_portfolio(
        run_id=f"floater-bench-{case}-{mode}",
        as_of=AS_OF,
        snapshot=snapshot,
        instruments=instruments,
        positions=positions,
        policy=policy,
        curve_cache=ShockedCurveCache(),
    )
    elapsed = time.perf_counter() - started
    if result.coverage.rejections:
        print(result.coverage.rejections[0], file=sys.stderr)
    return Timing(case, mode, size, result.coverage.completed, elapsed)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--positions", type=int, default=2000)
    parser.add_argument("--json", type=Path, help="also write the timings as JSON")
    args = parser.parse_args(argv)
    size = args.positions
    timings = [
        run_case(case, mode, size)
        for case in ("ois", "kbd+ois", "kbd+key")
        for mode in ("first", "hessian")
    ]
    print(
        f"rub-fixed-income-core {__version__}, Python {platform.python_version()}, "
        f"{platform.system()} {platform.machine()}, {size} floater positions"
    )
    print("| curves | mode | computed | total s | ms/position |")
    print("|---|---|---|---|---|")
    for t in timings:
        print(f"| {t.case} | {t.mode} | {t.computed} | {t.seconds:.2f} | {t.ms_per_position:.2f} |")
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
                            "curves": t.case,
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
    return 1 if any(t.computed != t.positions for t in timings) else 0


if __name__ == "__main__":
    sys.exit(main())
