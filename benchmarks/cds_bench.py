"""CDS risk benchmark (roadmap package 8): milliseconds per position.

Builds a synthetic book of standard single-name CDS on ``--entities`` reference
entities (five par-spread quotes each, 1Y-10Y), both sides, 100 bp and 500 bp
coupons, maturities from 1Y to 10Y, and values every position with
:class:`~rub_fixed_income.cds.CdsRiskEngine` on a shared discount curve:

- ``first``: valuation, IR buckets, parallel DV01, CS01 per quote and recovery01;
- ``hessian``: plus the 12x12 central Hessian (11 IR buckets and ``CS``);
- ``warm``: ``first`` again with the caches of a previous run (credit curves and
  OIS curves already built), what a backend that keeps them would pay.

Discount curves: a flat continuous zero curve (``zero``) and a 10-quote OIS
request (``ois``, quote bump and rebootstrap). Shocked OIS and credit curves are
shared across positions through one :class:`ShockedCurveCache` and one
:class:`CdsCurveCache` per job.

Run from the repository root::

    uv run python benchmarks/cds_bench.py --positions 2000

The numbers are machine-dependent.
"""

from __future__ import annotations

import argparse
import json
import platform
import sys
import time
from datetime import date

from rub_fixed_income import (
    BusinessCalendar,
    CdsContract,
    CdsConvention,
    CdsCurveCache,
    CdsCurveRequest,
    CdsRiskEngine,
    CdsRiskRequest,
    CdsSide,
    CdsSpreadQuote,
    CurveBootstrapRequest,
    DiscountCurve,
    OisQuote,
    ShockedCurveCache,
    __version__,
    cds_trade_dates,
    standard_cds_maturity,
)
from rub_fixed_income.dates import add_months

VALUATION_DATE = date(2026, 10, 15)
CALENDAR = BusinessCalendar(
    holidays=frozenset({date(2026, 11, 4)}), version="synthetic-benchmark-2026"
)
CONVENTION = CdsConvention.isda_standard(CALENDAR)
QUOTE_YEARS = (1, 3, 5, 7, 10)
TENOR_YEARS = (1, 2, 3, 4, 5, 7, 10)


def discount_inputs() -> dict[str, DiscountCurve | CurveBootstrapRequest]:
    return {
        "zero": DiscountCurve.flat(VALUATION_DATE, 0.14),
        "ois": CurveBootstrapRequest(
            VALUATION_DATE,
            tuple(
                OisQuote(f"{y}Y", add_months(VALUATION_DATE, 12 * y), 0.15 - 0.003 * y)
                for y in range(1, 11)
            ),
        ),
    }


def credit_curves(entities: int) -> list[CdsCurveRequest]:
    curves = []
    for k in range(entities):
        level = 0.004 + 0.0004 * (k % 50)
        quotes = tuple(
            CdsSpreadQuote(
                standard_cds_maturity(VALUATION_DATE, 12 * y, roll="semiannual"),
                level * (1.0 + 0.08 * y),
            )
            for y in QUOTE_YEARS
        )
        curves.append(CdsCurveRequest(VALUATION_DATE, quotes, 0.4 if k % 4 else 0.25, CONVENTION))
    return curves


def book(positions: int, entities: int) -> list[tuple[CdsContract, int]]:
    rows = []
    for i in range(positions):
        maturity = standard_cds_maturity(
            VALUATION_DATE, 12 * TENOR_YEARS[i % len(TENOR_YEARS)], roll="semiannual"
        )
        start = cds_trade_dates(VALUATION_DATE, CONVENTION, maturity_date=maturity)
        contract = CdsContract(
            f"CDS-{i:05d}",
            f"ENTITY-{i % entities:03d}",
            "USD",
            CdsSide.BUYER if i % 2 else CdsSide.SELLER,
            1e6 * (1 + i % 10),
            0.01 if i % 3 else 0.05,
            start.accrual_start_date,
            maturity,
            CONVENTION,
        )
        rows.append((contract, i % entities))
    return rows


def run(
    rows: list[tuple[CdsContract, int]],
    curves: list[CdsCurveRequest],
    discount: DiscountCurve | CurveBootstrapRequest,
    *,
    hessian: bool,
    caches: tuple[ShockedCurveCache, CdsCurveCache],
) -> float:
    started = time.perf_counter()
    for contract, entity in rows:
        engine = CdsRiskEngine(
            CdsRiskRequest(contract, VALUATION_DATE, discount, curves[entity]), *caches
        )
        engine.risk()
        if hessian:
            engine.second_order()
    return (time.perf_counter() - started) * 1000.0 / len(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--positions", type=int, default=2000)
    parser.add_argument("--entities", type=int, default=100)
    parser.add_argument("--json", action="store_true", help="print JSON instead of a table")
    args = parser.parse_args()
    curves = credit_curves(args.entities)
    rows = book(args.positions, args.entities)
    results: dict[str, float] = {}
    for name, discount in discount_inputs().items():
        caches = (ShockedCurveCache(), CdsCurveCache())
        results[f"{name}_first"] = run(rows, curves, discount, hessian=False, caches=caches)
        results[f"{name}_warm"] = run(rows, curves, discount, hessian=False, caches=caches)
        fresh = (ShockedCurveCache(), CdsCurveCache())
        results[f"{name}_hessian"] = run(rows, curves, discount, hessian=True, caches=fresh)
    meta = {
        "library_version": __version__,
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "positions": args.positions,
        "entities": args.entities,
    }
    if args.json:
        print(json.dumps({"meta": meta, "ms_per_position": results}, indent=2))
        return
    print(json.dumps(meta))
    print("| run | ms / position |")
    print("|---|---|")
    for name, value in results.items():
        print(f"| {name} | {value:.2f} |")


if __name__ == "__main__":
    main()
