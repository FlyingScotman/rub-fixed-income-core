"""Golden tests against MOEX-published bond analytics (roadmap 0.10).

Fixtures and provenance: ``tests/fixtures/moex_bonds/README.md``. Instruments are built
from the exchange's bondization data the way a backend adapter would.
"""

from __future__ import annotations

import json
from dataclasses import replace
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
    FixedCouponLeg,
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
    YieldRequest,
    bond_duration,
    solve_effective_annual_yield,
    value_portfolio,
)

FIXTURES = Path(__file__).parent / "fixtures" / "moex_bonds"
SETTLE = date(2026, 10, 19)  # SETTLEDATE of the 2026-10-16 snapshot (T+1 over a weekend)
PREV_SETTLE = date(2026, 10, 16)  # settlement of the previous session (PREVDATE 2026-10-15)
CALENDAR = BusinessCalendar(version="synthetic-weekends-only")


def rows(block: dict[str, Any]) -> list[dict[str, Any]]:
    return [dict(zip(block["columns"], row, strict=True)) for row in block["data"]]


MARKET = json.loads((FIXTURES / "market_2026-10-16.json").read_text(encoding="utf-8"))
SECURITIES = {(r["SECID"], r["BOARDID"]): r for r in rows(MARKET["securities"])}
MARKETDATA = {(r["SECID"], r["BOARDID"]): r for r in rows(MARKET["marketdata"])}


def instrument(secid: str, offer: date | None = None) -> Instrument:
    """Known coupons and principal from bondization (a floater's known coupons only)."""
    raw = json.loads((FIXTURES / "bondization" / f"{secid}.json").read_text(encoding="utf-8"))
    coupons = [c for c in rows(raw["coupons"]) if c["value"] is not None]
    periods = tuple(
        CouponPeriod(
            date.fromisoformat(c["startdate"]),
            date.fromisoformat(c["coupondate"]),
            date.fromisoformat(c["coupondate"]),
            float(c["value"]),
        )
        for c in coupons
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
    face = float(coupons[0]["initialfacevalue"])
    if not redemptions:  # no maturity date published: a placeholder far redemption
        redemptions = (Redemption(date(2099, 12, 31), face),)
    offers = () if offer is None else (Offer(offer, OfferKind.PUT, confirmed=True),)
    return Instrument(
        secid,
        ProductType.FIXED_BOND,
        "RUB",
        face,
        FixedCouponLeg(periods),
        PrincipalLeg(redemptions),
        "MOEX_STOCK",
        offers=offers,
    )


ACCRUED = [
    ("SU26238RMFS4", "TQOB"),
    ("RU000A0JS4Z7", "TQCB"),
    ("RU000A0JWMZ1", "TQCB"),
    ("RU000A0JR4U9", "TQCB"),
    ("RU000A0JVY04", "TQCB"),
    ("SU29010RMFS4", "TQOB"),
    ("RU000A10DQB6", "TQOY"),
]


@pytest.mark.parametrize(("secid", "board"), ACCRUED)
def test_accrued_interest_matches_moex_to_the_kopeck(secid: str, board: str) -> None:
    published = SECURITIES[(secid, board)]
    assert published["SETTLEDATE"] == SETTLE.isoformat()
    assert instrument(secid).accrued(SETTLE) == pytest.approx(published["ACCRUEDINT"], abs=1e-9)


def test_thirty_360_dollar_bond_accrues_on_the_rate() -> None:
    """MOEX accrues RU000A0JXTS9 on 30/360 (116 of 360 days at 8.5 %), not pro rata."""
    published = SECURITIES[("RU000A0JXTS9", "TQOD")]["ACCRUEDINT"]
    assert instrument("RU000A0JXTS9").accrued(SETTLE) == 2740.44  # pro rata on actual days
    fixed = instrument("RU000A0JXTS9")
    raw = json.loads((FIXTURES / "bondization" / "RU000A0JXTS9.json").read_text(encoding="utf-8"))
    rates = {c["coupondate"]: c["valueprc"] for c in rows(raw["coupons"])}
    assert isinstance(fixed.coupon_leg, FixedCouponLeg)
    leg = RateAccrualCouponLeg(
        tuple(
            RateCouponPeriod(
                p.accrual_start,
                p.accrual_end,
                p.payment_date,
                p.amount,
                rates[p.payment_date.isoformat()] / 100.0,
            )
            for p in fixed.coupon_leg.periods
        ),
        AccrualBasis.RATE_30_360,
    )
    thirty_360 = replace(fixed, currency="USD", coupon_leg=leg)
    assert thirty_360.accrued(SETTLE) == published == 2738.89


def yield_and_duration(
    bond: Instrument, clean_pct: float, settle: date, offer: date | None = None
) -> tuple[float, float]:
    horizon, chosen = bond.horizon("confirmed_put" if offer else "maturity", settle)
    flows = bond.cashflows(after=settle, horizon=horizon, offer=chosen)
    dirty = clean_pct / 100.0 * bond.outstanding_face(settle) + bond.accrued(settle)
    annual = solve_effective_annual_yield(YieldRequest(settle, dirty, flows)).effective_annual_yield
    pairs = tuple((f.payment_date, f.amount) for f in flows)
    years = bond_duration(annual_yield=annual, settlement_date=settle, cashflows=pairs)
    return annual * 100.0, years.macaulay_years * 365.0


def test_ofz_26238_yields_and_duration_match_moex() -> None:
    bond = instrument("SU26238RMFS4")
    sec, md = SECURITIES[("SU26238RMFS4", "TQOB")], MARKETDATA[("SU26238RMFS4", "TQOB")]
    at_wap, duration = yield_and_duration(bond, md["WAPRICE"], SETTLE)
    assert at_wap == pytest.approx(md["YIELDATWAPRICE"], abs=0.005)
    assert duration == pytest.approx(md["DURATION"], abs=1.0)
    at_last, _ = yield_and_duration(bond, md["LAST"], SETTLE)
    assert at_last == pytest.approx(md["YIELD"], abs=0.005)
    at_prev, _ = yield_and_duration(bond, sec["PREVWAPRICE"], PREV_SETTLE)
    assert at_prev == pytest.approx(sec["YIELDATPREVWAPRICE"], abs=0.005)


def test_put_bond_duration_to_the_offer_matches_moex() -> None:
    offer = date.fromisoformat(SECURITIES[("RU000A0JS4Z7", "TQCB")]["OFFERDATE"])
    bond = instrument("RU000A0JS4Z7", offer)
    md = MARKETDATA[("RU000A0JS4Z7", "TQCB")]
    _, duration = yield_and_duration(bond, md["MARKETPRICE"], SETTLE, offer)
    assert duration == pytest.approx(md["DURATION"], abs=1.0)
    # MOEX's YIELDATPREVWAPRICE (9.51 %) is not the effective yield to the offer for a
    # put bond; that one is YIELDTOOFFER / trades_yields.EFFECTIVEYIELD, which core
    # reproduces (tests/test_moex_eod_goldens.py, BACKLOG #36).
    at_prev, _ = yield_and_duration(bond, 107.93, PREV_SETTLE, offer)
    assert 9.2 < at_prev < 9.45


def test_value_portfolio_reproduces_the_ofz_yield_on_the_kbd_curve() -> None:
    params, _ = load("zcyc_2026-10-16_intraday.json")
    md = MARKETDATA[("SU26238RMFS4", "TQOB")]
    snapshot = MarketSnapshot(
        "moex-2026-10-16-intraday",
        params.trade_date,
        datetime(2026, 10, 16, 7, 35, tzinfo=UTC),
        curves=(CurveEntry("RUB_OFZ_KBD", "rub_ofz_kbd_v1", params),),
        calendars=(CalendarEntry("MOEX_STOCK", CALENDAR),),
        quotes=(BondPriceObservation("SU26238RMFS4", md["WAPRICE"], "waprice"),),
    )
    result = value_portfolio(
        run_id="moex-golden",
        as_of=snapshot.known_at,
        snapshot=snapshot,
        instruments=(instrument("SU26238RMFS4"),),
        positions=(Position("p", "SU26238RMFS4", 1, "OFZ", AccountingCategory.TRADING, "RUB"),),
        policy=ValuationPolicy("RUB_OFZ_KBD", spread_kind="ofz", include_second_order=False),
    )
    row = result.positions[0]
    assert row.settlement_date == SETTLE
    assert row.accrued_interest_rub == SECURITIES[("SU26238RMFS4", "TQOB")]["ACCRUEDINT"]
    assert row.effective_yield * 100.0 == pytest.approx(md["YIELDATWAPRICE"], abs=0.005)
    assert row.macaulay_duration_years * 365.0 == pytest.approx(md["DURATION"], abs=1.0)
    assert row.g_spread_bp is not None and abs(row.g_spread_bp) < 100.0
