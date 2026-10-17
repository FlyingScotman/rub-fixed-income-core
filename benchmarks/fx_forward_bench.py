"""FX forward and FX swap risk benchmark (package 4): milliseconds per position.

Builds a synthetic CNY/RUB book of outstanding forwards and FX swaps (value dates from
one week to two years) on shared curves: a RUB curve, either a 17-tenor RUONIA OIS
quote set (``ois``, rebootstrapped under shocks) or its bootstrapped discount factors
(``zero``), and a CNY curve implied from swap points. One ``ShockedCurveCache`` is
shared by every position of a run.

- ``first``: PV and central first-order risk (both curves, basis, FX);
- ``hessian``: plus the 24-factor Hessian and desk-factor mapping;
- ``scenarios``: Hessian plus five exact scenarios.

Run from the repository root::

    uv run python benchmarks/fx_forward_bench.py --positions 2000
"""

from __future__ import annotations

import argparse
import platform
import time
from dataclasses import dataclass
from datetime import date, timedelta

from rub_fixed_income import (
    CurveBootstrapRequest,
    DiscountCurve,
    FxForward,
    FxForwardLeg,
    FxForwardRiskEngine,
    FxForwardRiskRequest,
    FxFuturesScenario,
    FxSwap,
    FxSwapPointsQuote,
    OisQuote,
    ShockedCurveCache,
    __version__,
    bootstrap_discount_curve,
    fx_forward_factor_risk,
    implied_foreign_curve,
)

BUSINESS_DATE = date(2026, 10, 15)
SPOT_DATE = date(2026, 10, 16)
SPOT = 12.5
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
    FxFuturesScenario.parallel(rub_shift_bp=100.0),
    FxFuturesScenario.parallel(rub_shift_bp=-100.0),
    FxFuturesScenario.parallel(foreign_shift_bp=50.0),
    FxFuturesScenario.parallel(fx_spot_shift_pct=-10.0),
    FxFuturesScenario.parallel(rub_shift_bp=200.0, fx_spot_shift_pct=15.0),
)


def rub_ois() -> CurveBootstrapRequest:
    quotes = tuple(
        OisQuote(tenor, BUSINESS_DATE + timedelta(days=days), 0.137 + 0.002 * days / 365.0)
        for tenor, days in OIS_TENORS
    )
    return CurveBootstrapRequest(BUSINESS_DATE, quotes)


def cny_curve(rub: DiscountCurve) -> DiscountCurve:
    quotes = [FxSwapPointsQuote("TODTOM", BUSINESS_DATE, SPOT_DATE, 0.0047)]
    for days in (7, 30, 91, 182, 365, 730):
        far = SPOT_DATE + timedelta(days=days)
        points = SPOT * ((1 + 0.135 * days / 365) / (1 + 0.015 * days / 365) - 1)
        quotes.append(FxSwapPointsQuote(f"TOM{days}D", SPOT_DATE, far, points))
    return implied_foreign_curve(
        rub_curve=rub, spot_rate=SPOT, spot_value_date=SPOT_DATE, quotes=quotes
    )


def book(size: int) -> list[FxForward | FxSwap]:
    trades: list[FxForward | FxSwap] = []
    for i in range(size):
        far = SPOT_DATE + timedelta(days=7 + (i * 37) % 723)
        notional = 1_000_000.0 * (1 + i % 7) * (1 if i % 3 else -1)
        rate = SPOT * (1.0 + 0.12 * (far - SPOT_DATE).days / 365.0) + 0.01 * (i % 11 - 5)
        if i % 5 < 3:
            trades.append(FxForward(f"F{i}", "CNY", 1.0, FxForwardLeg(far, notional, rate)))
        else:
            near = SPOT_DATE + timedelta(days=(i * 13) % ((far - SPOT_DATE).days))
            trades.append(
                FxSwap(
                    f"S{i}",
                    "CNY",
                    1.0,
                    FxForwardLeg(near, -notional, SPOT + 0.001 * (i % 9)),
                    FxForwardLeg(far, notional, rate),
                )
            )
    return trades


@dataclass(frozen=True)
class Timing:
    curve: str
    mode: str
    positions: int
    seconds: float

    @property
    def ms_per_position(self) -> float:
        return 1000.0 * self.seconds / self.positions


def run_case(curve: str, mode: str, size: int) -> Timing:
    ois = rub_ois()
    rub_zero = bootstrap_discount_curve(ois)
    cny = cny_curve(rub_zero)
    rub: DiscountCurve | CurveBootstrapRequest = ois if curve == "ois" else rub_zero
    trades = book(size)
    cache = ShockedCurveCache()
    started = time.perf_counter()
    for trade in trades:
        engine = FxForwardRiskEngine(
            FxForwardRiskRequest(trade, SPOT, SPOT_DATE, rub, cny, basis_bp=0.0),
            curve_cache=cache,
        )
        risk = engine.risk(difference="central")
        if mode == "first":
            fx_forward_factor_risk(risk, "RUB_OIS", "CNY_FX_IMPLIED")
            continue
        fx_forward_factor_risk(risk, "RUB_OIS", "CNY_FX_IMPLIED", engine.second_order())
        if mode == "scenarios":
            for scenario in SCENARIOS:
                engine.reprice(scenario)
    return Timing(curve, mode, size, time.perf_counter() - started)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--positions", type=int, default=2000)
    args = parser.parse_args(argv)
    timings = [
        run_case(curve, mode, args.positions)
        for curve in ("zero", "ois")
        for mode in ("first", "hessian", "scenarios")
    ]
    print(
        f"rub-fixed-income-core {__version__}, Python {platform.python_version()}, "
        f"{platform.system()} {platform.machine()}, {args.positions} positions"
    )
    print("| RUB curve | mode | total s | ms/position |")
    print("|---|---|---|---|")
    for t in timings:
        print(f"| {t.curve} | {t.mode} | {t.seconds:.2f} | {t.ms_per_position:.2f} |")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
