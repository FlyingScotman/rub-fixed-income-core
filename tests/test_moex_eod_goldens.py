"""Golden tests against MOEX end-of-day bond analytics (roadmap 0.10, BACKLOG #36, #38).

Fixtures and provenance: ``tests/fixtures/moex_eod/README.md``. The history rows are the
exchange's EOD figures for trade date 2026-10-15 (settlement T+1, 2026-10-16). In them
``ACCINT`` is the accrued interest on the trade date, while yields and durations are
at settlement. Instruments are built from bondization data as a backend adapter would.
"""

from __future__ import annotations

import json
import math
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
from test_kbd import load

from rub_fixed_income import (
    AccountingCategory,
    AccrualBasis,
    BondPriceObservation,
    BusinessCalendar,
    CalendarEntry,
    CashflowKind,
    CouponPeriod,
    CurveEntry,
    DayCount,
    DiscountCurve,
    FixedCouponLeg,
    FxRate,
    Instrument,
    MarketSnapshot,
    Offer,
    OfferKind,
    Position,
    PrincipalLeg,
    ProductType,
    RateAccrualCouponLeg,
    RateCouponPeriod,
    Redemption,
    ValuationPolicy,
    YieldConvention,
    YieldRequest,
    bond_duration,
    g_spread_bp,
    solve_effective_annual_yield,
    value_portfolio,
)

FIXTURES = Path(__file__).parent / "fixtures" / "moex_eod"
TRADE = date(2026, 10, 15)
SETTLE = date(2026, 10, 16)
CALENDAR = BusinessCalendar(version="synthetic-weekends-only")

MINFIN = None  # OFZ: pro rata on the coupon amount (FixedCouponLeg)
ACT_365 = AccrualBasis.RATE_ACT_365
ACT_360 = AccrualBasis.RATE_ACT_360
THIRTY_360 = AccrualBasis.RATE_30_360


def rows(block: dict[str, Any]) -> list[dict[str, Any]]:
    return [dict(zip(block["columns"], row, strict=True)) for row in block["data"]]


def history(secid: str, board: str) -> dict[str, Any]:
    raw = json.loads((FIXTURES / "history" / f"{secid}_{board}.json").read_text("utf-8"))
    (row,) = rows(raw["history"])
    assert row["TRADEDATE"] == TRADE.isoformat()
    return row


def bondization(secid: str) -> dict[str, Any]:
    return dict(json.loads((FIXTURES / "bondization" / f"{secid}.json").read_text("utf-8")))


def next_offer(secid: str, after: date) -> date:
    dates = [
        date.fromisoformat(o["offerdate"])
        for o in rows(bondization(secid)["offers"])
        if o["offerdate"] != "0000-00-00" and o["offertype"] == "Оферта"
    ]
    return min(d for d in dates if d > after)


def instrument(
    secid: str,
    basis: AccrualBasis | None,
    offer: date | None = None,
    currency: str = "RUB",
    yield_basis: DayCount = DayCount.ACT_365_FIXED,
) -> Instrument:
    """Known coupons and principal from bondization; ``basis`` None is the Minfin rule.

    ``yield_basis`` is the yield time basis of the issue terms (default ACT/365F).

    A rate-accrual leg keeps the coupons that publish a rate (RU000A105RH2's first,
    short coupon of 2023 does not; it is long paid).
    """
    raw = bondization(secid)
    coupons = [
        c
        for c in rows(raw["coupons"])
        if c["value"] is not None and (basis is None or c["valueprc"] is not None)
    ]
    dates = [
        (date.fromisoformat(c["startdate"]), date.fromisoformat(c["coupondate"])) for c in coupons
    ]
    leg: FixedCouponLeg | RateAccrualCouponLeg
    if basis is None:
        leg = FixedCouponLeg(
            tuple(CouponPeriod(s, e, e, float(c["value"])) for (s, e), c in zip(dates, coupons))
        )
    else:
        leg = RateAccrualCouponLeg(
            tuple(
                RateCouponPeriod(s, e, e, float(c["value"]), c["valueprc"] / 100.0)
                for (s, e), c in zip(dates, coupons)
            ),
            basis,
        )
    amortizations = rows(raw["amortizations"])
    redemptions = tuple(
        Redemption(
            date.fromisoformat(a["amortdate"]),
            float(a["value"]),
            CashflowKind.MATURITY if i == len(amortizations) - 1 else CashflowKind.AMORTIZATION,
        )
        for i, a in enumerate(amortizations)
    )
    offers = () if offer is None else (Offer(offer, OfferKind.PUT, confirmed=True),)
    return Instrument(
        secid,
        ProductType.FIXED_BOND,
        currency,
        float(coupons[0]["initialfacevalue"]),
        leg,
        PrincipalLeg(redemptions),
        "MOEX_STOCK",
        offers=offers,
        yield_convention=YieldConvention(day_count=yield_basis),
    )


DAYS_PER_YEAR = {DayCount.ACT_365_FIXED: 365.0, DayCount.ACT_360: 360.0}


def yield_and_duration(
    bond: Instrument, clean_pct: float, settle: date, *, to_offer: bool = False
) -> tuple[float, float]:
    """Effective annual yield (%) and Macaulay duration (days) at a clean price.

    Both are on the bond's own yield time basis (``Instrument.yield_convention``).
    """
    horizon, chosen = bond.horizon("confirmed_put" if to_offer else "maturity", settle)
    flows = bond.cashflows(after=settle, horizon=horizon, offer=chosen)
    dirty = clean_pct / 100.0 * bond.outstanding_face(settle) + bond.accrued(settle)
    convention = bond.yield_convention
    annual = solve_effective_annual_yield(
        YieldRequest(settle, dirty, flows, convention)
    ).effective_annual_yield
    pairs = tuple((f.payment_date, f.amount) for f in flows)
    years = bond_duration(
        annual_yield=annual, settlement_date=settle, cashflows=pairs, convention=convention
    )
    return annual * 100.0, years.macaulay_years * DAYS_PER_YEAR[convention.day_count]


ACCRUED = [
    ("SU26207RMFS9", "TQOB", MINFIN, "OFZ-PD, 0.3 years"),
    ("SU26218RMFS6", "TQOB", MINFIN, "OFZ-PD"),
    ("SU26224RMFS4", "TQOB", MINFIN, "OFZ-PD; rate ACT/365 would be a kopeck off"),
    ("SU26238RMFS4", "TQOB", MINFIN, "OFZ-PD; rate ACT/365 would be a kopeck off"),
    ("SU26252RMFS5", "TQOB", MINFIN, "OFZ-PD; rate ACT/365 would be a kopeck off"),
    ("RU000A10AA10", "TQCB", ACT_365, "monthly 25 %; pro rata would be a kopeck off"),
    ("RU000A10F7V9", "TQCB", ACT_365, "monthly; pro rata would be a kopeck off"),
    ("RU000A10B7W1", "TQCB", ACT_365, "call bond; pro rata would be a kopeck off"),
    ("RU000A10ASC6", "TQCB", ACT_365, "amortizer, face 800"),
    ("RU000A10AC75", "TQCB", ACT_365, "amortizer on face 750; pro rata a kopeck off"),
    ("RU000A10C5Y0", "TQCB", ACT_365, "put bond"),
    ("RU000A10G2W7", "TQCB", ACT_365, "put bond"),
    ("RU000A0ZYYE3", "TQCB", ACT_365, "put bond"),
    ("RU000A1043Z7", "TQCB", ACT_365, "put bond; pro rata would be a kopeck off"),
    ("RU000A101012", "TQCB", ACT_365, "put bond; pro rata would be a kopeck off"),
    ("RU000A1089U1", "TQCB", ACT_360, "ACT/360 terms, face 100 000"),
    ("RU000A10A877", "TQOD", THIRTY_360, "USD, replacement of a eurobond"),
    ("RU000A105RH2", "TQOD", THIRTY_360, "USD, replacement of a eurobond"),
]


@pytest.mark.parametrize(("secid", "board", "basis", "note"), ACCRUED)
def test_accrued_interest_matches_moex_eod_to_the_kopeck(
    secid: str, board: str, basis: AccrualBasis | None, note: str
) -> None:
    published = history(secid, board)
    currency = "USD" if board == "TQOD" else "RUB"
    assert published["FACEUNIT"] == currency, note
    bond = instrument(secid, basis, currency=currency)
    assert bond.accrued(TRADE) == pytest.approx(published["ACCINT"], abs=1e-9), note


@pytest.mark.parametrize(
    ("secid", "board", "basis", "wrong"),
    [
        ("RU000A10AA10", "TQCB", ACT_365, MINFIN),
        ("RU000A10F7V9", "TQCB", ACT_365, MINFIN),
        ("RU000A10AC75", "TQCB", ACT_365, MINFIN),
        ("RU000A101012", "TQCB", ACT_365, MINFIN),
        ("SU26224RMFS4", "TQOB", MINFIN, ACT_365),
        ("SU26238RMFS4", "TQOB", MINFIN, ACT_365),
        ("SU26252RMFS5", "TQOB", MINFIN, ACT_365),
    ],
)
def test_each_board_needs_its_own_accrual_rule(
    secid: str, board: str, basis: AccrualBasis | None, wrong: AccrualBasis | None
) -> None:
    published = history(secid, board)["ACCINT"]
    assert instrument(secid, basis).accrued(TRADE) == pytest.approx(published, abs=1e-9)
    off = instrument(secid, wrong).accrued(TRADE) - published
    assert abs(off) == pytest.approx(0.01, abs=1e-9)


TO_MATURITY = [
    ("SU26207RMFS9", "TQOB", MINFIN),
    ("SU26218RMFS6", "TQOB", MINFIN),
    ("SU26224RMFS4", "TQOB", MINFIN),
    ("SU26238RMFS4", "TQOB", MINFIN),
    ("SU26252RMFS5", "TQOB", MINFIN),
    ("RU000A10AA10", "TQCB", ACT_365),
    ("RU000A10F7V9", "TQCB", ACT_365),
    ("RU000A1089U1", "TQCB", ACT_360),
    ("RU000A10B7W1", "TQCB", ACT_365),  # a call is not assumed exercised, as on MOEX
    ("RU000A10ASC6", "TQCB", ACT_365),
    ("RU000A10AC75", "TQCB", ACT_365),
    ("RU000A10A877", "TQOD", THIRTY_360),
]


@pytest.mark.parametrize(("secid", "board", "basis"), TO_MATURITY)
def test_yield_and_duration_at_waprice_match_moex(
    secid: str, board: str, basis: AccrualBasis | None
) -> None:
    published = history(secid, board)
    currency = "USD" if board == "TQOD" else "RUB"
    bond = instrument(secid, basis, currency=currency)
    at_wap, duration = yield_and_duration(bond, published["WAPRICE"], SETTLE)
    assert at_wap == pytest.approx(published["YIELDATWAP"], abs=0.005)
    assert duration == pytest.approx(published["DURATION"], abs=1.0)


@pytest.mark.parametrize(
    "secid", ["SU26207RMFS9", "SU26218RMFS6", "SU26224RMFS4", "SU26238RMFS4", "SU26252RMFS5"]
)
def test_ofz_yield_at_close_matches_moex(secid: str) -> None:
    published = history(secid, "TQOB")
    at_close, _ = yield_and_duration(instrument(secid, MINFIN), published["CLOSE"], SETTLE)
    assert at_close == pytest.approx(published["YIELDCLOSE"], abs=0.005)


PUTS = ["RU000A10C5Y0", "RU000A10G2W7", "RU000A0ZYYE3", "RU000A1043Z7", "RU000A101012"]


@pytest.mark.parametrize("secid", PUTS)
def test_put_bond_yield_to_offer_at_close_and_duration_match_moex(secid: str) -> None:
    """``YIELDTOOFFER`` is the effective yield to the offer at ``CLOSE`` (4 decimals)."""
    published = history(secid, "TQCB")
    bond = instrument(secid, ACT_365, next_offer(secid, SETTLE))
    at_close, duration = yield_and_duration(bond, published["CLOSE"], SETTLE, to_offer=True)
    assert at_close == pytest.approx(published["YIELDTOOFFER"], abs=0.0006)
    assert duration == pytest.approx(published["DURATION"], abs=1.0)


def test_put_bond_yieldatwap_is_not_the_effective_yield_to_offer() -> None:
    """For put bonds MOEX's ``YIELD``/``YIELDATWAP`` is another measure (BACKLOG #36).

    Its trades carry both: ``trades.YIELD`` and ``trades_yields.EFFECTIVEYIELD`` with
    ``YIELDDATETYPE`` OFFER. Core reproduces the effective yield exactly.
    """
    raw = json.loads((FIXTURES / "trades" / "RU000A10C5Y0_TQCB.json").read_text("utf-8"))
    trades = {t["TRADENO"]: t for t in rows(raw["trades"])}
    effective = rows(raw["trades_yields"])
    assert {t["TRADENO"] for t in effective} == set(trades)
    bond = instrument("RU000A10C5Y0", ACT_365, next_offer("RU000A10C5Y0", SETTLE))
    for published in effective:
        trade = trades[published["TRADENO"]]
        settle = date.fromisoformat(trade["TRADE_SESSION_DATE"])
        assert settle == date(2026, 10, 19)  # Saturday session of 17 October, settles Monday
        assert published["YIELDDATETYPE"] == "OFFER"
        ours, duration = yield_and_duration(bond, trade["PRICE"], settle, to_offer=True)
        assert ours == pytest.approx(published["EFFECTIVEYIELD"], abs=0.00006)
        assert duration == pytest.approx(published["DURATION"], abs=1.0)
        assert trade["YIELD"] - ours > 0.2  # the other measure is ~22 bp higher here


def test_trade_g_spread_matches_moex_on_the_published_kbd() -> None:
    raw = json.loads((FIXTURES / "trades" / "RU000A10C5Y0_TQCB.json").read_text("utf-8"))
    params, _ = load("zcyc_2026-10-16.json")
    for published in rows(raw["trades_yields"]):
        assert published["ZCYCMOMENT"] == "2026-10-16 18:54:59"
        tenor = published["DURATION"] / 365.0
        ours = g_spread_bp(params, published["EFFECTIVEYIELD"] / 100.0, tenor)
        assert ours == pytest.approx(published["GSPREADBP"], abs=0.5)


def test_value_portfolio_reproduces_eod_ofz_figures_on_the_eod_kbd() -> None:
    params, _ = load("zcyc_2026-10-15.json")
    ids = ["SU26207RMFS9", "SU26238RMFS4", "SU26252RMFS5"]
    published = {s: history(s, "TQOB") for s in ids}
    snapshot = MarketSnapshot(
        "moex-2026-10-15-eod",
        params.trade_date,
        datetime(2026, 10, 15, 16, 0, tzinfo=UTC),
        curves=(CurveEntry("RUB_OFZ_KBD", "rub_ofz_kbd_v1", params),),
        calendars=(CalendarEntry("MOEX_STOCK", CALENDAR),),
        quotes=tuple(BondPriceObservation(s, published[s]["WAPRICE"], "waprice") for s in ids),
    )
    result = value_portfolio(
        run_id="moex-eod-golden",
        as_of=snapshot.known_at,
        snapshot=snapshot,
        instruments=tuple(instrument(s, MINFIN) for s in ids),
        positions=tuple(Position(s, s, 1, "OFZ", AccountingCategory.TRADING, "RUB") for s in ids),
        policy=ValuationPolicy("RUB_OFZ_KBD", spread_kind="ofz", include_second_order=False),
    )
    for row in result.positions:
        moex = published[row.position_id]
        assert row.settlement_date == SETTLE
        assert row.effective_yield * 100.0 == pytest.approx(moex["YIELDATWAP"], abs=0.005)
        assert row.macaulay_duration_years * 365.0 == pytest.approx(moex["DURATION"], abs=1.0)


# Yield time basis of the USD replacement bonds on TQOD, from their issue terms (BACKLOG
# #38; ISS does not publish it, the backend's static data carries it). All of them accrue
# 30/360, so the accrual and the yield basis are independent terms.
USD_YIELD_BASIS = [
    ("RU000A105RH2", DayCount.ACT_360),
    ("RU000A108P79", DayCount.ACT_360),
    ("RU000A105R62", DayCount.ACT_360),
    ("RU000A105JT4", DayCount.ACT_360),
    ("RU000A105A95", DayCount.ACT_360),
    ("RU000A1059N9", DayCount.ACT_360),
    ("RU000A1059P4", DayCount.ACT_360),
    ("RU000A10A877", DayCount.ACT_365_FIXED),
    ("RU000A108Y11", DayCount.ACT_365_FIXED),
    ("RU000A107B43", DayCount.ACT_365_FIXED),
    ("RU000A106G56", DayCount.ACT_365_FIXED),
]
OTHER_BASIS = {DayCount.ACT_360: DayCount.ACT_365_FIXED, DayCount.ACT_365_FIXED: DayCount.ACT_360}


@pytest.mark.parametrize(("secid", "yield_basis"), USD_YIELD_BASIS)
def test_usd_bond_yield_on_its_issue_time_basis_matches_moex(
    secid: str, yield_basis: DayCount
) -> None:
    """``ACCINT`` on 30/360, ``YIELDATWAP`` and ``DURATION`` on the issue's time basis."""
    published = history(secid, "TQOD")
    assert published["FACEUNIT"] == "USD"
    bond = instrument(secid, THIRTY_360, currency="USD", yield_basis=yield_basis)
    assert bond.accrued(TRADE) == pytest.approx(published["ACCINT"], abs=1e-9)
    at_wap, duration = yield_and_duration(bond, published["WAPRICE"], SETTLE)
    assert at_wap == pytest.approx(published["YIELDATWAP"], abs=0.005)
    assert duration == pytest.approx(published["DURATION"], abs=1.0)
    # The basis is a real term: the other one misses MOEX by 5 to 40 bp.
    other = instrument(secid, THIRTY_360, currency="USD", yield_basis=OTHER_BASIS[yield_basis])
    wrong, _ = yield_and_duration(other, published["WAPRICE"], SETTLE)
    assert abs(wrong - published["YIELDATWAP"]) > 0.05


def test_act_360_effective_yield_is_independently_reproduced() -> None:
    """RU000A105RH2: core's ACT/360 yield equals a hand-rolled bisection on days / 360."""
    published = history("RU000A105RH2", "TQOD")
    bond = instrument("RU000A105RH2", THIRTY_360, currency="USD", yield_basis=DayCount.ACT_360)
    ours, _ = yield_and_duration(bond, published["WAPRICE"], SETTLE)
    flows = bond.cashflows(after=SETTLE)
    dirty = published["WAPRICE"] / 100.0 * bond.outstanding_face(SETTLE) + bond.accrued(SETTLE)
    low, high = 0.0, 1.0
    for _ in range(100):
        mid = (low + high) / 2.0
        pv = math.fsum(
            f.amount / (1.0 + mid) ** ((f.payment_date - SETTLE).days / 360.0) for f in flows
        )
        low, high = (mid, high) if pv > dirty else (low, mid)
    assert ours == pytest.approx(low * 100.0, abs=1e-9)
    assert published["YIELDATWAP"] == 9.59 and ours == pytest.approx(9.5887, abs=1e-4)


def test_value_portfolio_reports_usd_bond_yields_on_their_issue_basis() -> None:
    """End to end on a synthetic USD curve and FX rate: yields do not depend on either."""
    usd_curve = DiscountCurve(
        TRADE,
        tuple((date(TRADE.year + n, TRADE.month, TRADE.day), 1.045**-n) for n in range(1, 31)),
    )
    published = {s: history(s, "TQOD") for s, _ in USD_YIELD_BASIS}
    snapshot = MarketSnapshot(
        "moex-2026-10-15-eod-usd",
        TRADE,
        datetime(2026, 10, 15, 16, 0, tzinfo=UTC),
        curves=(CurveEntry("USD_SYNTHETIC", "usd_discount_factors_v1", usd_curve),),
        calendars=(CalendarEntry("MOEX_STOCK", CALENDAR),),
        fx=(FxRate("USD", 81.0),),
        quotes=tuple(
            BondPriceObservation(s, published[s]["WAPRICE"], "waprice") for s in published
        ),
    )
    result = value_portfolio(
        run_id="moex-eod-usd-golden",
        as_of=snapshot.known_at,
        snapshot=snapshot,
        instruments=tuple(
            instrument(s, THIRTY_360, currency="USD", yield_basis=b) for s, b in USD_YIELD_BASIS
        ),
        positions=tuple(
            Position(s, s, 1, "EUROBONDS", AccountingCategory.TRADING, "USD")
            for s, _ in USD_YIELD_BASIS
        ),
        policy=ValuationPolicy(
            "RUB_UNUSED", foreign_curves=(("USD", "USD_SYNTHETIC"),), include_second_order=False
        ),
    )
    assert result.status == "complete"
    basis = dict(USD_YIELD_BASIS)
    for row in result.positions:
        moex = published[row.position_id]
        assert row.yield_convention == YieldConvention(day_count=basis[row.position_id]).identifier
        assert row.effective_yield * 100.0 == pytest.approx(moex["YIELDATWAP"], abs=0.005)
        days = row.macaulay_duration_years * DAYS_PER_YEAR[basis[row.position_id]]
        assert days == pytest.approx(moex["DURATION"], abs=1.0)
        assert row.currency_amounts is not None
        accrued = instrument(row.position_id, THIRTY_360, currency="USD").accrued(SETTLE)
        assert row.currency_amounts.accrued_interest == accrued
        assert row.factor_risk.first_order_by_factor()["FX|USD"] == pytest.approx(
            row.currency_amounts.pv * 81.0 / 100.0
        )
