"""A synthetic mixed book for result contract v3: bonds and every derivative product.

Shared by ``tests/test_portfolio_v3.py``, the pinned hashes in
``tests/test_hash_stability.py`` and ``benchmarks/portfolio_bench.py`` (``mixed`` case).
Business date Thursday 15 October 2026; every curve is dated it.
"""

from __future__ import annotations

import math
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from itertools import pairwise

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
    FixingEntry,
    FixingHistory,
    FxRate,
    Instrument,
    MarketSnapshot,
    OisQuote,
    PortfolioRiskResult,
    PortfolioScenario,
    Position,
    PrincipalLeg,
    ProductType,
    Redemption,
    ValuationPolicy,
    value_portfolio,
)
from rub_fixed_income.cds import (
    CdsContract,
    CdsConvention,
    CdsSide,
    CdsSpreadQuote,
    cds_trade_dates,
    standard_cds_maturity,
)
from rub_fixed_income.conventions import BusinessDayConvention, DayCount
from rub_fixed_income.dates import add_months
from rub_fixed_income.domain import (
    CreditCurveEntry,
    FuturesQuote,
    FxBasis,
    FxSpot,
    RuoniaFixingsEntry,
)
from rub_fixed_income.fx_forwards import FxForward, FxForwardLeg, FxSwap
from rub_fixed_income.fx_futures import FxFuturesContract
from rub_fixed_income.key_rate import KeyRateForwardCurve, KeyRateForwardSegment
from rub_fixed_income.ofz_futures import OfzFuturesContract
from rub_fixed_income.portfolio_instruments import (
    CdsInstrument,
    FxForwardInstrument,
    FxFuturesInstrument,
    OfzDeliverable,
    OfzFuturesInstrument,
    PortfolioInstrument,
    RateFuturesInstrument,
    RepoInstrument,
    SwapInstrument,
    XccyInstrument,
)
from rub_fixed_income.rate_futures import (
    KeyRateFuturesContract,
    RuoniaAverageFuturesContract,
    RuoniaFixings,
    RuoniaIndexFuturesContract,
    RuoniaIndexValue,
    ruonia_average_settlement_period,
)
from rub_fixed_income.repo import (
    FixedRepoRate,
    FloatingRepoRate,
    RepoCollateral,
    RepoDayBasis,
    RepoTrade,
)
from rub_fixed_income.swaps import (
    InterestRateSwap,
    SwapConvention,
    SwapLegSchedule,
    swap_convention,
)
from rub_fixed_income.xccy import (
    XccyCurveBootstrapRequest,
    XccyParQuote,
    standard_xccy_swap,
    xcs_cnyrub_convention,
)

BUSINESS_DATE = date(2026, 10, 15)
TOM = date(2026, 10, 16)
AS_OF = datetime(2026, 10, 15, 16, 30, tzinfo=UTC)
KNOWN = "2026-10-15T12:00:00Z"
CAL = BusinessCalendar(holidays=frozenset({date(2026, 11, 4)}), version="synthetic-v3-book")
SPOT = {"USD": 81.5, "CNY": 11.2}
FX_RATE_USD = 81.4  # FxRate (bonds, CDS): the backend's conversion rate, no value date
XCCY_CONVENTION = xcs_cnyrub_convention(CAL)
ISDA = CdsConvention.isda_standard(CAL)

POLICY = ValuationPolicy("RUB_OIS", spread_kind="credit")


def node_curve(level: float, slope: float, curve_date: date = BUSINESS_DATE) -> DiscountCurve:
    nodes = []
    for months in (1, 3, 6, 12, 24, 36, 60, 84, 120, 180, 240, 360):
        node = add_months(curve_date, months)
        t = (node - curve_date).days / 365.0
        nodes.append((node, math.exp(-(level + slope * min(t, 10.0)) * t)))
    return DiscountCurve(curve_date, tuple(nodes))


def ois_request() -> CurveBootstrapRequest:
    return CurveBootstrapRequest(
        BUSINESS_DATE,
        tuple(
            OisQuote(f"{y}Y", BUSINESS_DATE + timedelta(days=365 * y), 0.152 - 0.002 * y)
            for y in (1, 2, 3, 5, 7, 10)
        ),
    )


KEY_RATE_CURVE = KeyRateForwardCurve(
    BUSINESS_DATE,
    0.165,
    date(2026, 10, 26),
    (
        KeyRateForwardSegment(date(2026, 10, 26), date(2027, 6, 1), 0.15, "1Y"),
        KeyRateForwardSegment(date(2027, 6, 1), date(2031, 1, 1), 0.13, "5Y"),
    ),
)


def xccy_quotes() -> XccyCurveBootstrapRequest:
    return XccyCurveBootstrapRequest(
        trade_date=BUSINESS_DATE,
        currency="CNY",
        spot_rate=SPOT["CNY"],
        spot_value_date=TOM,
        notional_fx_rate=11.25,
        quotes=(XccyParQuote("1Y", 0.031), XccyParQuote("2Y", 0.029), XccyParQuote("3Y", 0.028)),
        convention=XCCY_CONVENTION,
    )


def business_days(start: date, end: date) -> list[date]:
    out, day = [], start
    while day < end:
        if CAL.is_business_day(day):
            out.append(day)
        day += timedelta(days=1)
    return out


def ruonia_history() -> FixingHistory:
    rows = tuple(
        (d, 0.158 + 0.0001 * (k % 5))
        for k, d in enumerate(business_days(date(2025, 12, 1), BUSINESS_DATE))
    )
    return FixingHistory("ruonia", BUSINESS_DATE, rows, CAL, "cbr-ruonia", known_at_utc=KNOWN)


def ruonia_futures_fixings(drop: date | None = None) -> RuoniaFixings:
    rows = tuple((d, 0.157) for d in business_days(date(2026, 8, 1), BUSINESS_DATE) if d != drop)
    return RuoniaFixings(BUSINESS_DATE, rows, CAL, "cbr-ruonia-futures")


def annual_bond(name: str, coupon: float, first: date, years: int, **kw: object) -> Instrument:
    starts = [date(first.year - 1 + k, first.month, first.day) for k in range(years + 1)]
    periods = tuple(CouponPeriod(s, e, e, coupon) for s, e in pairwise(starts))
    return Instrument(
        name,
        ProductType.FIXED_BOND,
        "RUB",
        1000.0,
        FixedCouponLeg(periods),
        PrincipalLeg((Redemption(starts[-1], 1000.0, CashflowKind.MATURITY),)),
        "MOEX",
        **kw,  # type: ignore[arg-type]
    )


def ois_swap_convention() -> SwapConvention:
    return SwapConvention(
        index="ruonia",
        averaging="compounded",
        fixed_leg=SwapLegSchedule(12, "backward", DayCount.ACT_365_FIXED),
        float_leg=SwapLegSchedule(3, "backward", DayCount.ACT_365_FIXED),
        rate_day_count=DayCount.ACT_365_FIXED,
        business_day_convention=BusinessDayConvention.MODIFIED_FOLLOWING,
        spot_lag_days=1,
        payment_lag_days=1,
    )


OFZ_CONTRACT = OfzFuturesContract("OFZ6-12.26", date(2026, 12, 4), date(2026, 12, 7))
RUON = RuoniaAverageFuturesContract("RUON-10.26", *ruonia_average_settlement_period(2026, 10, CAL))
RUONIA_INDEX = RuoniaIndexFuturesContract("RF-12.26", date(2026, 12, 1))
KEYRATE = KeyRateFuturesContract("KK-10.26", date(2026, 10, 23), date(2026, 10, 26))
SI = FxFuturesContract("Si-12.26", "USD", 1000.0, 1000.0, date(2026, 12, 18))


def instruments(*, pinned: bool = False) -> tuple[PortfolioInstrument, ...]:
    """The book's terms. ``pinned`` leaves out the products whose engines use numpy
    matrix products (two-curve and key-rate swaps), so the pinned hash stays portable."""
    gaz = annual_bond("GAZ-29", 140.0, date(2027, 3, 1), 3, issuer_id="GAZPROM")
    ofz_short = annual_bond("SU_SHORT", 70.0, date(2026, 11, 20), 5)
    ofz_long = annual_bond("SU_LONG", 90.0, date(2027, 6, 1), 7)
    seasoned = InterestRateSwap(
        "OIS-SEASONED",
        "payer",
        1_000_000_000.0,
        0.152,
        date(2026, 1, 15),
        date(2028, 1, 15),
        ois_swap_convention(),
        float_spread=0.0025,
    )
    forward_ois = InterestRateSwap.from_tenor(
        swap_id="OIS-2C",
        side="receiver",
        notional=500_000_000.0,
        fixed_rate=0.148,
        trade_date=BUSINESS_DATE,
        tenor="3Y",
        calendar=CAL,
        convention=ois_swap_convention(),
    )
    krs = InterestRateSwap.from_tenor(
        swap_id="KRS-2Y",
        side="payer",
        notional=300_000_000.0,
        fixed_rate=0.145,
        trade_date=BUSINESS_DATE,
        tenor="2Y",
        calendar=CAL,
        convention=swap_convention("rub_key_rate_swap_v1"),
    )
    cds_maturity = standard_cds_maturity(BUSINESS_DATE, 60, roll="semiannual")
    accrual = cds_trade_dates(BUSINESS_DATE, ISDA, maturity_date=cds_maturity).accrual_start_date
    cds_rub = CdsContract(
        "CDS-GAZ", "GAZPROM", "RUB", CdsSide.BUYER, 100_000_000.0, 0.01, accrual, cds_maturity, ISDA
    )
    cds_usd = CdsContract(
        "CDS-SBER", "SBER", "USD", CdsSide.SELLER, 5_000_000.0, 0.01, accrual, cds_maturity, ISDA
    )
    rows: list[PortfolioInstrument] = [
        gaz,
        ofz_short,
        ofz_long,
        SwapInstrument("OIS-SEASONED", seasoned, "MOEX", "RUB_OIS", fixing_id="RUONIA"),
        FxForwardInstrument(
            "FWD-USD",
            FxForward("FWD-USD", "USD", 1.0, FxForwardLeg(date(2027, 1, 15), 1_000_000.0, 84.0)),
            "RUB_OIS",
            "USD_DF",
        ),
        FxForwardInstrument(
            "SWAP-CNY",
            FxSwap(
                "SWAP-CNY",
                "CNY",
                1.0,
                FxForwardLeg(TOM, -10_000_000.0, 11.2),
                FxForwardLeg(date(2027, 4, 16), 10_000_000.0, 11.9),
            ),
            "RUB_OIS",
            "CNY_DF",
        ),
        XccyInstrument(
            "XCS-2Y",
            standard_xccy_swap(
                "XCS-2Y",
                BUSINESS_DATE,
                "2Y",
                XCCY_CONVENTION,
                currency="CNY",
                foreign_notional=10_000_000.0,
                notional_fx_rate=11.25,
                fixed_rate=0.03,
                foreign_side="receive",
            ),
            "RUB_OIS",
            "CNY_XCCY",
        ),
        XccyInstrument(
            "XCS-1Y-DF",
            standard_xccy_swap(
                "XCS-1Y-DF",
                BUSINESS_DATE,
                "1Y",
                XCCY_CONVENTION,
                currency="CNY",
                foreign_notional=5_000_000.0,
                notional_fx_rate=11.25,
                fixed_rate=0.032,
                foreign_side="pay",
            ),
            "RUB_OIS",
            "CNY_DF",
        ),
        CdsInstrument("CDS-GAZ", cds_rub, "RUB_OIS"),
        CdsInstrument("CDS-SBER", cds_usd, "USD_DF"),
        FxFuturesInstrument("Si-12.26", SI, "RUB_OIS", "USD_DF"),
        RateFuturesInstrument("RUON-10.26", RUON, "RUB_OIS", "RUONIA_FUT"),
        RateFuturesInstrument("RF-12.26", RUONIA_INDEX, "RUB_OIS", "RUONIA_FUT"),
        RateFuturesInstrument("KK-10.26", KEYRATE, "KEY_RATE"),
        OfzFuturesInstrument(
            "OFZ6-12.26",
            OFZ_CONTRACT,
            (OfzDeliverable("SU_SHORT", 1.0), OfzDeliverable("SU_LONG", 1.1)),
            "RUB_OIS",
            "RUB_REPO",
        ),
    ]
    if not pinned:
        rows += [
            SwapInstrument("OIS-2C", forward_ois, "MOEX", "RUB_OIS", "RUB_PROJ"),
            SwapInstrument("KRS-2Y", krs, "MOEX", "RUB_OIS", "KEY_RATE"),
            *repos(),
        ]
    return tuple(rows)


def repos() -> tuple[RepoInstrument, ...]:
    """A fixed term reverse repo on SU_LONG and a RUONIA open repo with an assumed end."""
    term = RepoTrade(
        "REPO-TERM",
        "reverse_repo",
        date(2026, 10, 1),
        date(2026, 11, 2),
        50_000_000.0,
        FixedRepoRate(0.155, RepoDayBasis.ACT_365_366),
        2,
        RepoCollateral("SU_LONG", 50_000.0, initial_discount_pct=8.0),
    )
    open_repo = RepoTrade(
        "REPO-OPEN",
        "repo",
        date(2026, 10, 5),
        None,
        30_000_000.0,
        FloatingRepoRate(
            "RUONIA",
            0.001,
            RepoDayBasis.ACT_365_366,
            "previous_settlement_day",
            DayCount.ACT_365_FIXED,
        ),
        2,
    )
    return (
        RepoInstrument("REPO-TERM", term, "MOEX", "RUB_OIS"),
        RepoInstrument(
            "REPO-OPEN",
            open_repo,
            "MOEX",
            "RUB_OIS",
            "RUB_PROJ",
            fixing_id="RUONIA",
            assumed_end_date=date(2026, 12, 15),
        ),
    )


FUTURES_PRICES = {
    # (price, margined price); prices are near the model at small bases.
    "Si-12.26": (83_300.0, 83_150.0),
    "RUON-10.26": (84.33, 84.35),
    "RF-12.26": (4.6312, 4.6290),
    "KK-10.26": (15.10, 15.05),
    "OFZ6-12.26": (9_520.0, 9_505.0),
}


def snapshot(**changes: object) -> MarketSnapshot:
    base = MarketSnapshot(
        "hub-eod-2026-10-15",
        BUSINESS_DATE,
        AS_OF,
        curves=(
            CurveEntry("RUB_OIS", "rub_ruonia_ois_legacy_v1", node_curve(0.15, -0.004)),
            CurveEntry("RUB_REPO", "rub_ruonia_ois_legacy_v1", node_curve(0.155, -0.003)),
            CurveEntry("RUB_PROJ", "rub_ruonia_ois_legacy_v1", ois_request()),
            CurveEntry("KEY_RATE", "rub_key_rate_swap_v1", KEY_RATE_CURVE),
            CurveEntry("USD_DF", "usd_discount_factors_v1", node_curve(0.042, 0.0008)),
            CurveEntry("CNY_DF", "cny_discount_factors_v1", node_curve(0.016, 0.0006)),
            CurveEntry("CNY_XCCY", "cny_ruonia_xccy_v1", xccy_quotes()),
        ),
        calendars=(CalendarEntry("MOEX", CAL), CalendarEntry("FX", CAL)),
        fixings=(FixingEntry("RUONIA", ruonia_history()),),
        fx=(FxRate("USD", FX_RATE_USD),),
        quotes=(
            BondPriceObservation("GAZ-29", 99.4, "waprice"),
            BondPriceObservation("SU_SHORT", 98.0, "close"),
            BondPriceObservation("SU_LONG", 104.0, "close"),
        ),
        fx_spots=tuple(FxSpot(c, r, TOM, "FX") for c, r in SPOT.items()),
        fx_bases=(
            FxBasis("USD", "USD_DF", 0.0),
            FxBasis("CNY", "CNY_DF", 12.0),
            FxBasis("CNY", "CNY_XCCY", 0.0),
        ),
        credit_curves=(
            CreditCurveEntry(
                "GAZPROM",
                "RUB",
                tuple(
                    CdsSpreadQuote(standard_cds_maturity(BUSINESS_DATE, m, roll="semiannual"), s)
                    for m, s in ((12, 0.012), (36, 0.016), (60, 0.019), (120, 0.023))
                ),
                0.4,
                ISDA,
            ),
            CreditCurveEntry(
                "SBER",
                "USD",
                tuple(
                    CdsSpreadQuote(standard_cds_maturity(BUSINESS_DATE, m, roll="semiannual"), s)
                    for m, s in ((12, 0.008), (60, 0.013))
                ),
                0.35,
                ISDA,
            ),
        ),
        futures_quotes=tuple(FuturesQuote(c, p, m) for c, (p, m) in FUTURES_PRICES.items()),
        ruonia_fixings=(RuoniaFixingsEntry("RUONIA_FUT", ruonia_futures_fixings()),),
        ruonia_index=(RuoniaIndexValue(date(2026, 10, 14), 4.5524),),
    )
    return replace(base, **changes)  # type: ignore[arg-type]


def positions(*, pinned: bool = False) -> tuple[Position, ...]:
    def pos(pid: str, iid: str, qty: float, ccy: str = "RUB", rec: float | None = None) -> Position:
        return Position(pid, iid, qty, "DESK", AccountingCategory.TRADING, ccy, rec)

    rows = [
        pos("p-gaz", "GAZ-29", 1000.0, rec=0.4),
        pos("p-ofz-short", "SU_SHORT", -200.0),
        pos("p-ois", "OIS-SEASONED", 1.0),
        pos("p-fwd", "FWD-USD", 1.0, "USD"),
        pos("p-fxswap", "SWAP-CNY", -2.0, "CNY"),
        pos("p-xcs", "XCS-2Y", 1.0, "CNY"),
        pos("p-xcs-df", "XCS-1Y-DF", 1.0, "CNY"),
        pos("p-cds-gaz", "CDS-GAZ", 1.0),
        pos("p-cds-sber", "CDS-SBER", 2.0, "USD", rec=0.25),
        pos("p-si", "Si-12.26", 10.0, "USD"),
        pos("p-ruon", "RUON-10.26", 5.0),
        pos("p-rf", "RF-12.26", -50.0),
        pos("p-kk", "KK-10.26", 20.0),
        pos("p-ofz-fut-1", "OFZ6-12.26", 20.0),
        pos("p-ofz-fut-2", "OFZ6-12.26", -5.0),
    ]
    if not pinned:
        rows += [
            pos("p-ois-2c", "OIS-2C", 1.0),
            pos("p-krs", "KRS-2Y", -1.0),
            pos("p-repo-term", "REPO-TERM", 1.0),
            pos("p-repo-open", "REPO-OPEN", 1.0),
        ]
    return tuple(rows)


PINNED_SCENARIO = PortfolioScenario(
    "rates+25 fx+5",
    BondScenario.parallel(25.0, spread_shift_bp=10.0),
    fx_shocks_pct=(("USD", 5.0),),
    curve_shifts_bp=(("RUB_REPO", tuple(-10.0 for _ in range(11))),),
)


def pinned_v3_result() -> PortfolioRiskResult:
    """The book behind ``tests/fixtures/portfolio_result_v3.json`` (pin the library
    version before calling it: it is part of the job identity)."""
    return value_portfolio(
        run_id="pinned-v3",
        as_of=AS_OF,
        snapshot=snapshot(),
        instruments=instruments(pinned=True),
        positions=positions(pinned=True),
        policy=POLICY,
        scenarios=(PINNED_SCENARIO,),
    )
