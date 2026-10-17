"""MOEX rate futures risk benchmark (package 5): milliseconds per position.

A synthetic book of short-rate futures (RUON one-month, RUONIA index, KEYRATE) and OFZ
basket futures (five-bond baskets) on shared curves: a RUONIA projection curve, either
a 17-tenor OIS quote set (``ois``, rebootstrapped under shocks) or its bootstrapped
discount factors (``zero``), a KBD-like bond curve and a key-rate forward curve. One
``ShockedCurveCache`` is shared by every position.

- ``first``: price and central first-order risk mapped to desk factors;
- ``hessian``: plus the Hessian;
- ``scenarios``: Hessian plus five exact scenarios.

``cold`` builds one engine per position; ``shared`` builds one engine per contract and
derives each position with ``for_quantity`` (prices do not depend on quantity, so every
repricing is shared). Run from the repository root::

    uv run python benchmarks/rate_futures_bench.py --positions 2000
"""

from __future__ import annotations

import argparse
import platform
import time
from dataclasses import replace
from datetime import date, timedelta
from itertools import pairwise

from rub_fixed_income import (
    BusinessCalendar,
    CashflowKind,
    CouponPeriod,
    CurveBootstrapRequest,
    DeliverableBond,
    DiscountCurve,
    FixedCouponLeg,
    Instrument,
    KeyRateForwardCurve,
    KeyRateForwardSegment,
    KeyRateFuturesContract,
    OfzFuturesContract,
    OfzFuturesRiskEngine,
    OfzFuturesRiskRequest,
    OfzFuturesScenario,
    OisQuote,
    PrincipalLeg,
    ProductType,
    RateFuturesRiskEngine,
    RateFuturesRiskRequest,
    RateFuturesScenario,
    Redemption,
    RuoniaAverageFuturesContract,
    RuoniaFixings,
    RuoniaIndexFuturesContract,
    RuoniaIndexValue,
    ShockedCurveCache,
    __version__,
    bootstrap_discount_curve,
    ofz_futures_factor_risk,
    rate_futures_factor_risk,
    ruonia_average_settlement_period,
)

TODAY = date(2026, 10, 15)
CAL = BusinessCalendar(version="weekends-only")
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
STIR_SCENARIOS = (
    RateFuturesScenario.parallel(100.0),
    RateFuturesScenario.parallel(-100.0),
    RateFuturesScenario.parallel(25.0, basis_shift_bp=10.0),
    RateFuturesScenario.parallel(200.0),
    RateFuturesScenario.parallel(-50.0, basis_shift_bp=-5.0),
)
OFZ_SCENARIOS = (
    OfzFuturesScenario.parallel(bond_shift_bp=100.0),
    OfzFuturesScenario.parallel(bond_shift_bp=-100.0),
    OfzFuturesScenario.parallel(funding_shift_bp=50.0),
    OfzFuturesScenario.parallel(spread_shift_bp=25.0),
    OfzFuturesScenario.parallel(bond_shift_bp=200.0, funding_shift_bp=100.0),
)


def ruonia_ois() -> CurveBootstrapRequest:
    quotes = tuple(
        OisQuote(tenor, TODAY + timedelta(days=days), 0.139 - 0.003 * min(days, 730) / 365.0)
        for tenor, days in OIS_TENORS
    )
    return CurveBootstrapRequest(TODAY, quotes)


def fixings() -> RuoniaFixings:
    rows, day = [], date(2026, 8, 1)
    while day < TODAY:
        if CAL.is_business_day(day):
            rows.append((day, 0.1391))
        day += timedelta(days=1)
    return RuoniaFixings(TODAY, tuple(rows), CAL, "bench")


def key_curve() -> KeyRateForwardCurve:
    meetings = (date(2026, 10, 26), date(2026, 12, 21), date(2027, 2, 15), date(2027, 3, 22))
    rates = (0.1375, 0.135, 0.1325, 0.13)
    segments = tuple(
        KeyRateForwardSegment(start, end, rate, f"M{i}")
        for i, (start, end, rate) in enumerate(
            zip(meetings, (*meetings[1:], date(2027, 12, 31)), rates, strict=True)
        )
    )
    return KeyRateForwardCurve(TODAY, 0.14, meetings[0], segments)


def bond(name: str, coupon: float, first: date, years: int) -> Instrument:
    """Annual coupons on 1000 face, the first paid on ``first``."""
    starts = [date(first.year - 1 + k, first.month, first.day) for k in range(years + 1)]
    periods = tuple(CouponPeriod(s, e, e, coupon) for s, e in pairwise(starts))
    return Instrument(
        name,
        ProductType.FIXED_BOND,
        "RUB",
        1000.0,
        FixedCouponLeg(periods),
        PrincipalLeg((Redemption(starts[-1], 1000.0, CashflowKind.MATURITY),)),
        "MOEX_STOCK",
    )


def stir_engines(
    curve: DiscountCurve | CurveBootstrapRequest, cache: ShockedCurveCache
) -> list[RateFuturesRiskEngine]:
    fx = fixings()
    engines = []
    for month in (10, 11, 12):
        start, end = ruonia_average_settlement_period(2026, month, CAL)
        ruon = RuoniaAverageFuturesContract(f"RUON-{month}.26", start, end)
        engines.append(
            RateFuturesRiskEngine(
                RateFuturesRiskRequest(ruon, curve, fixings=fx, basis_bp=0.0), cache
            )
        )
    for ltd in (date(2026, 12, 1), date(2027, 3, 1), date(2027, 6, 1), date(2027, 9, 1)):
        index = RuoniaIndexFuturesContract(f"RUONIA-{ltd.month}.{ltd.year % 100}", ltd)
        engines.append(
            RateFuturesRiskEngine(
                RateFuturesRiskRequest(
                    index,
                    curve,
                    fixings=fx,
                    index_anchor=RuoniaIndexValue(date(2026, 10, 14), 4.5439),
                    basis_bp=0.0,
                ),
                cache,
            )
        )
    keys = key_curve()
    for meeting, effective in (
        (date(2026, 10, 23), date(2026, 10, 26)),
        (date(2026, 12, 18), date(2026, 12, 21)),
        (date(2027, 2, 12), date(2027, 2, 15)),
    ):
        keyrate = KeyRateFuturesContract(
            f"KEYRATE-{meeting.month}.{meeting.year % 100}", meeting, effective
        )
        engines.append(
            RateFuturesRiskEngine(RateFuturesRiskRequest(keyrate, keys, basis_bp=0.0), cache)
        )
    return engines


def ofz_engines(
    funding: DiscountCurve | CurveBootstrapRequest, cache: ShockedCurveCache
) -> list[OfzFuturesRiskEngine]:
    kbd = DiscountCurve.flat(TODAY, 0.145)
    basket = tuple(
        DeliverableBond(
            bond(f"SU{i}", 70.0 + 5 * i, date(2027, 3 + i, 1), 6 + i % 3),
            1.0 - 0.03 * i,
            95.0 - 2 * i,
        )
        for i in range(5)
    )
    return [
        OfzFuturesRiskEngine(
            OfzFuturesRiskRequest(
                OfzFuturesContract(code, ltd, ltd + timedelta(days=3)),
                basket,
                date(2026, 10, 16),
                kbd,
                funding,
                basis_bp=0.0,
            ),
            cache,
        )
        for code, ltd in (("OFZ6-12.26", date(2026, 12, 4)), ("OFZ6-3.27", date(2027, 3, 4)))
    ]


Engine = RateFuturesRiskEngine | OfzFuturesRiskEngine


def templates(
    curve: DiscountCurve | CurveBootstrapRequest, cache: ShockedCurveCache
) -> list[Engine]:
    return [*stir_engines(curve, cache), *ofz_engines(curve, cache)]


def run_case(curve_kind: str, mode: str, style: str, size: int) -> float:
    ois = ruonia_ois()
    curve: DiscountCurve | CurveBootstrapRequest = (
        ois if curve_kind == "ois" else bootstrap_discount_curve(ois)
    )
    cache = ShockedCurveCache()
    started = time.perf_counter()
    shared = templates(curve, cache)
    for i in range(size):
        quantity = float((i % 17) - 8 or 1)
        template = shared[i % len(shared)]
        engine: Engine
        if style == "shared":
            engine = template.for_quantity(quantity)
        elif isinstance(template, OfzFuturesRiskEngine):  # fresh engine; curves shared
            engine = OfzFuturesRiskEngine(replace(template.request, quantity=quantity), cache)
        else:
            engine = RateFuturesRiskEngine(replace(template.request, quantity=quantity), cache)
        if isinstance(engine, OfzFuturesRiskEngine):
            risk_o = engine.risk(difference="central")
            gamma_o = engine.second_order() if mode != "first" else None
            ofz_futures_factor_risk(risk_o, "RUB_KBD", "RUB_RUONIA", gamma_o)
            if mode == "scenarios":
                for scenario_o in OFZ_SCENARIOS:
                    engine.reprice(scenario_o)
        else:
            risk_s = engine.risk(difference="central")
            gamma_s = engine.second_order() if mode != "first" else None
            rate_futures_factor_risk(risk_s, "RUB_RUONIA", gamma_s)
            if mode == "scenarios":
                for scenario_s in STIR_SCENARIOS:
                    engine.reprice(scenario_s)
    return time.perf_counter() - started


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--positions", type=int, default=2000)
    args = parser.parse_args(argv)
    print(
        f"rub-fixed-income-core {__version__}, Python {platform.python_version()}, "
        f"{platform.system()} {platform.machine()}, {args.positions} positions"
    )
    print("| RUONIA curve | engines | mode | total s | ms/position |")
    print("|---|---|---|---|---|")
    for curve in ("zero", "ois"):
        for style in ("shared", "cold"):
            for mode in ("first", "hessian", "scenarios"):
                seconds = run_case(curve, mode, style, args.positions)
                per = 1000 * seconds / args.positions
                print(f"| {curve} | {style} | {mode} | {seconds:.2f} | {per:.2f} |")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
