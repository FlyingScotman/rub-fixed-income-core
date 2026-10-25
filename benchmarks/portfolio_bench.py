"""Portfolio valuation benchmark (roadmap 0.7): milliseconds per position.

Builds a synthetic book of distinct fixed-coupon RUB bonds (bullets, amortizers and
confirmed puts, 1-15 years) and values it with ``value_portfolio`` on the archived
KBD publication of 15 October 2026 and on a 17-tenor RUONIA OIS quote set, under the
legacy convention (``ois``) and as the MOEX spot-starting swaps of
``rub_ruonia_ois_moex_v2`` (``ruonia``, the desk monitor's curve risk in quotes; each
shocked curve is a full coupled bootstrap):

- ``first``: PV, spread and first-order desk-factor risk;
- ``hessian``: plus the 12x12 Hessian per position;
- ``scenarios``: Hessian plus five curve scenarios;
- ``warm``: OIS curves only, the same job with a cache seeded from a previous job's
  :meth:`ShockedCurveCache.export` (the backend's persisted curves).

The ``fx`` curve case (package 3) is the same book as CNY, USD and EUR bonds in turn,
each on a caller-built discount curve of its currency, valued in RUB at snapshot FX
rates with ``FX|<ccy>`` and its cross terms; its scenarios add a 10 % FX move.

The ``mixed`` case (result contract v3) cycles through the positions of the mixed book
of ``tests/portfolio_v3_book.py``: bonds, seasoned, two-curve and key-rate swaps, an FX
forward and swap, XCCY on quotes and on discount factors, RUB and USD CDS, FX, RUON,
RUONIA index, KEYRATE and OFZ futures, term and open repos. Every OTC trade is its own
instrument; futures positions share their contract (one engine per contract). Its
scenarios add per-curve shifts and FX moves.

Run from the repository root::

    uv run python benchmarks/portfolio_bench.py --positions 2000
    uv run python benchmarks/portfolio_bench.py --positions 2000 --cases mixed
    uv run python benchmarks/portfolio_bench.py --positions 50 --cases ruonia

The numbers are machine-dependent; the target is <= 10 ms per position amortised
for OIS with the Hessian at 2k positions.
"""

from __future__ import annotations

import argparse
import json
import platform
import sys
import time
from dataclasses import dataclass, replace
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
    DiscountCurve,
    FixedCouponLeg,
    FxFuturesInstrument,
    FxRate,
    Instrument,
    MarketSnapshot,
    Offer,
    OfferKind,
    OfzFuturesInstrument,
    OisQuote,
    PortfolioInstrument,
    PortfolioScenario,
    Position,
    PrincipalLeg,
    ProductType,
    RateFuturesInstrument,
    Redemption,
    ShockedCurveCache,
    ValuationPolicy,
    ZcycParameters,
    __version__,
    ois_curve_convention,
    ois_quotes,
    value_portfolio,
)
from rub_fixed_income.dates import add_months

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
import portfolio_v3_book as mixed_book

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


def ruonia_request() -> CurveBootstrapRequest:
    """The same tenors as MOEX standard OIS RUONIA swaps (``rub_ruonia_ois_moex_v2``)."""
    rates = [(tenor, 0.137 + 0.002 * days / 365.0) for tenor, days in OIS_TENORS]
    convention = ois_curve_convention("rub_ruonia_ois_moex_v2", CALENDAR)
    return CurveBootstrapRequest(
        BUSINESS_DATE, ois_quotes(BUSINESS_DATE, rates, CALENDAR), convention
    )


FOREIGN = {"CNY": (11.4, 0.017), "USD": (81.5, 0.042), "EUR": (94.2, 0.023)}
FX_SCENARIOS = (
    *SCENARIOS,
    PortfolioScenario(
        "fx+10", BondScenario.parallel(0.0), tuple((c, 10.0) for c in sorted(FOREIGN))
    ),
)


def foreign_curve(rate: float) -> DiscountCurve:
    nodes = tuple(
        (add_months(BUSINESS_DATE, m), (1.0 + rate + 0.0005 * m / 12.0) ** (-m / 12.0))
        for m in (1, 3, 6, 12, 24, 36, 60, 84, 120, 180, 240, 360)
    )
    return DiscountCurve(BUSINESS_DATE, nodes)


def book(
    size: int, currencies: tuple[str, ...] = ("RUB",)
) -> tuple[list[Instrument], list[Position], list[BondPriceObservation]]:
    instruments, positions, quotes = [], [], []
    for i in range(size):
        currency = currencies[i % len(currencies)]
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
                currency,
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
                currency,
                0.3,
            )
        )
        quotes.append(BondPriceObservation(instrument_id, 95.0 + (i % 11) * 0.5, "waprice"))
    return instruments, positions, quotes


def mixed_case(mode: str, size: int) -> Timing:
    """The v3 mixed book, ``size`` positions cycling through its position templates."""
    templates = mixed_book.instruments()
    by_id = {i.instrument_id: i for i in templates}
    shared = (FxFuturesInstrument, RateFuturesInstrument, OfzFuturesInstrument)
    book_positions = mixed_book.positions()
    instruments: dict[str, PortfolioInstrument] = {
        i.instrument_id: i for i in templates if isinstance(i, Instrument)
    }
    instruments.update((i.instrument_id, i) for i in templates if isinstance(i, shared))
    positions = []
    for k in range(size):
        template = book_positions[k % len(book_positions)]
        source = by_id[template.instrument_id]
        if isinstance(source, (Instrument, *shared)):
            positions.append(replace(template, position_id=f"pos-{k:05d}"))
            continue
        copy = replace(source, instrument_id=f"{source.instrument_id}-{k:05d}")
        instruments[copy.instrument_id] = copy
        positions.append(
            replace(template, position_id=f"pos-{k:05d}", instrument_id=copy.instrument_id)
        )
    policy = replace(mixed_book.POLICY, include_second_order=mode != "first")
    scenarios = (
        PortfolioScenario("rates+100", BondScenario.parallel(100.0)),
        PortfolioScenario(
            "usd+10 fx",
            BondScenario.parallel(0.0),
            fx_shocks_pct=(("USD", 10.0), ("CNY", 10.0)),
            curve_shifts_bp=(("USD_DF", (50.0,) * 11),),
        ),
        PortfolioScenario("spread+50", BondScenario.parallel(0.0, spread_shift_bp=50.0)),
    )
    started = time.perf_counter()
    result = value_portfolio(
        run_id=f"bench-mixed-{mode}",
        as_of=mixed_book.AS_OF,
        snapshot=mixed_book.snapshot(),
        instruments=tuple(instruments.values()),
        positions=positions,
        policy=policy,
        scenarios=scenarios if mode == "scenarios" else (),
    )
    elapsed = time.perf_counter() - started
    return Timing("mixed", mode, size, result.coverage.completed, elapsed)


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
    foreign = curve == "fx"
    instruments, positions, quotes = book(size, tuple(sorted(FOREIGN)) if foreign else ("RUB",))
    snapshot = MarketSnapshot(
        f"bench-{BUSINESS_DATE}",
        BUSINESS_DATE,
        AS_OF,
        curves=(
            CurveEntry("RUB_OFZ_KBD", "rub_ofz_kbd_v1", kbd_parameters()),
            (
                CurveEntry("RUB_OIS", "rub_ruonia_ois_moex_v2", ruonia_request())
                if curve == "ruonia"
                else CurveEntry("RUB_OIS", "rub_ruonia_ois_legacy_v1", ois_request())
            ),
            *(
                CurveEntry(f"{c}_DF", f"{c.lower()}_discount_factors_v1", foreign_curve(r))
                for c, (_, r) in FOREIGN.items()
            ),
        ),
        calendars=(CalendarEntry("MOEX_STOCK", CALENDAR),),
        fx=tuple(FxRate(c, fx) for c, (fx, _) in FOREIGN.items()),
        quotes=tuple(quotes),
    )
    policy = ValuationPolicy(
        "RUB_OFZ_KBD" if curve == "kbd" else "RUB_OIS",
        horizon="confirmed_put",
        include_second_order=mode != "first",
        foreign_curves=tuple((c, f"{c}_DF") for c in FOREIGN) if foreign else (),
    )
    scenarios = FX_SCENARIOS if foreign else SCENARIOS
    cache = ShockedCurveCache() if cache is None else cache
    started = time.perf_counter()
    result = value_portfolio(
        run_id=f"bench-{curve}-{mode}",
        as_of=AS_OF,
        snapshot=snapshot,
        instruments=instruments,
        positions=positions,
        policy=policy,
        scenarios=scenarios if mode == "scenarios" else (),
        curve_cache=cache,
    )
    elapsed = time.perf_counter() - started
    return Timing(curve, mode, size, result.coverage.completed, elapsed), cache


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--positions", type=int, default=2000)
    parser.add_argument("--json", type=Path, help="also write the timings as JSON")
    parser.add_argument(
        "--cases",
        default="kbd,ois,ruonia,fx,mixed",
        help="comma-separated: kbd, ois, ruonia, fx, mixed",
    )
    args = parser.parse_args(argv)
    size = args.positions
    cases = tuple(c.strip() for c in args.cases.split(",") if c.strip())
    timings = []
    for curve in cases:
        for mode in ("first", "hessian", "scenarios"):
            if curve == "mixed":
                timings.append(mixed_case(mode, size))
                continue
            timing, cache = run_case(curve, mode, size)
            timings.append(timing)
            if curve in ("ois", "ruonia") and mode == "hessian":
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
