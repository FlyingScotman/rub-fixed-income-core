"""Package 9b benchmark: callables, perpetuals and pass-throughs, ms per position.

A synthetic book of distinct instruments on the archived KBD of 15 October 2026:

- half callable fixed bonds (2-15 years, two or three par calls) under ``to_worst``;
- a quarter perpetuals priced to their first call (``to_call``);
- a quarter monthly pass-throughs (5 years, level-pay schedule) at a 15 % CPR.

It times :func:`prepare_assumed_book` alone (exercise resolution and CPR projection)
and :func:`value_assumed_portfolio` with first-order risk and with the Hessian.

Run from the repository root::

    uv run python benchmarks/assumptions_bench.py --positions 2000
"""

from __future__ import annotations

import argparse
import json
import platform
import sys
import time
from datetime import UTC, date, datetime, timedelta
from itertools import pairwise
from pathlib import Path

from rub_fixed_income import (
    AccountingCategory,
    AccrualBasis,
    AssumptionPolicy,
    BondPriceObservation,
    BusinessCalendar,
    CalendarEntry,
    CouponPeriod,
    CurveEntry,
    ExerciseAssumption,
    FixedCouponLeg,
    Instrument,
    InstrumentAssumption,
    MarketSnapshot,
    Offer,
    OfferKind,
    PassThroughPeriod,
    PassThroughTerms,
    Position,
    PrepaymentAssumption,
    PrincipalLeg,
    ProductType,
    Redemption,
    ValuationPolicy,
    ZcycParameters,
    __version__,
    prepare_assumed_book,
    value_assumed_portfolio,
)
from rub_fixed_income.dates import add_months

ROOT = Path(__file__).resolve().parents[1]
KBD_FIXTURE = ROOT / "tests" / "fixtures" / "kbd" / "zcyc_2026-10-15.json"
BUSINESS_DATE = date(2026, 10, 15)
AS_OF = datetime(2026, 10, 15, 16, 30, tzinfo=UTC)
CALENDAR = BusinessCalendar(version="synthetic-benchmark-2026")


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


def semiannual(first: date, count: int, coupon: float) -> tuple[CouponPeriod, ...]:
    return tuple(
        CouponPeriod(
            add_months(first, 6 * k),
            add_months(first, 6 * (k + 1)),
            add_months(first, 6 * (k + 1)),
            coupon,
        )
        for k in range(count)
    )


def pool(i: int) -> PassThroughTerms:
    dates = [date(2026, 10, 1) + timedelta(days=30 * k) for k in range(61)]
    r = 0.12 * 30 / 365
    face = 400.0 + i % 5 * 100.0
    payment = face * r / (1.0 - (1.0 + r) ** -60)
    balance, scheduled = face, []
    for k in range(60):
        principal = payment - balance * r if k < 59 else balance
        scheduled.append(principal)
        balance -= principal
    return PassThroughTerms(
        f"MBS-{i:05d}",
        "RUB",
        1000.0,
        face,
        0.10 + (i % 7) * 0.01,
        AccrualBasis.RATE_ACT_365,
        tuple(
            PassThroughPeriod(s, e, e, p)
            for (s, e), p in zip(pairwise(dates), scheduled, strict=True)
        ),
        12,
        "MOEX_STOCK",
    )


def book(
    size: int,
) -> tuple[list[Instrument], list[PassThroughTerms], list[Position], AssumptionPolicy, list[str]]:
    instruments: list[Instrument] = []
    pools: list[PassThroughTerms] = []
    overrides: list[InstrumentAssumption] = []
    ids: list[str] = []
    for i in range(size):
        kind = i % 4
        first = add_months(date(2026, 4, 15), -(i % 6))
        coupon = 60.0 + (i % 9) * 5.0
        if kind in (0, 1):
            years = 2 + i % 14
            periods = semiannual(first, 2 * years + 1, coupon)
            calls = tuple(
                Offer(periods[k].payment_date, OfferKind.CALL, confirmed=False)
                for k in (2, 4, 6)[: 2 + i % 2]
                if k < len(periods) - 1
            )
            iid = f"CALL-{i:05d}"
            instruments.append(
                Instrument(
                    iid,
                    ProductType.FIXED_BOND,
                    "RUB",
                    1000.0,
                    FixedCouponLeg(periods),
                    PrincipalLeg((Redemption(periods[-1].payment_date, 1000.0),)),
                    "MOEX_STOCK",
                    offers=calls,
                )
            )
        elif kind == 2:
            periods = semiannual(first, 8, coupon)
            call = periods[4].payment_date
            iid = f"PERP-{i:05d}"
            instruments.append(
                Instrument(
                    iid,
                    ProductType.FIXED_BOND,
                    "RUB",
                    1000.0,
                    FixedCouponLeg(periods),
                    PrincipalLeg(()),
                    "MOEX_STOCK",
                    offers=(Offer(call, OfferKind.CALL, confirmed=False),),
                )
            )
            overrides.append(InstrumentAssumption(iid, exercise=ExerciseAssumption.to_call(call)))
        else:
            terms = pool(i)
            iid = terms.instrument_id
            pools.append(terms)
            overrides.append(InstrumentAssumption(iid, prepayment=PrepaymentAssumption(0.15)))
        ids.append(iid)
    positions = [
        Position(
            f"pos-{i:05d}", iid, float(100 + i % 50), "CREDIT", AccountingCategory.TRADING, "RUB"
        )
        for i, iid in enumerate(ids)
    ]
    policy = AssumptionPolicy(ExerciseAssumption.to_worst(), tuple(overrides))
    return instruments, pools, positions, policy, ids


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--positions", type=int, default=2000)
    args = parser.parse_args()
    instruments, pools, positions, assumptions, ids = book(args.positions)
    snapshot = MarketSnapshot(
        "bench",
        BUSINESS_DATE,
        AS_OF,
        curves=(CurveEntry("RUB_KBD", "rub_ofz_kbd_v1", kbd_parameters()),),
        calendars=(CalendarEntry("MOEX_STOCK", CALENDAR),),
        quotes=tuple(
            BondPriceObservation(iid, 97.0 + (i % 13) * 0.5, "waprice") for i, iid in enumerate(ids)
        ),
    )
    started = time.perf_counter()
    prepared = prepare_assumed_book(
        snapshot=snapshot, instruments=instruments, policy=assumptions, pass_throughs=pools
    )
    prepare_s = time.perf_counter() - started
    rejected = [u for u in prepared.uses if u.status == "rejected"]
    report: dict[str, object] = {
        "library_version": __version__,
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "positions": args.positions,
        "prepare_ms_per_position": 1000.0 * prepare_s / args.positions,
        "rejected_assumptions": len(rejected),
    }
    for mode, second in (("first", False), ("hessian", True)):
        started = time.perf_counter()
        result = value_assumed_portfolio(
            run_id=f"bench-{mode}",
            as_of=AS_OF,
            snapshot=snapshot,
            instruments=instruments,
            positions=positions,
            policy=ValuationPolicy("RUB_KBD", include_second_order=second),
            assumptions=assumptions,
            pass_throughs=pools,
        )
        seconds = time.perf_counter() - started
        report[f"{mode}_ms_per_position"] = 1000.0 * seconds / args.positions
        report[f"{mode}_computed"] = result.coverage.completed
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
