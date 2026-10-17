"""Golden tests against MOEX yields to call and perpetual yields (package 9b).

Fixtures and provenance: ``tests/fixtures/moex_calls/README.md``. Trade date
2026-10-15, settlement T+1 2026-10-16. MOEX yields a bond to its call when the call
date is set (``CALLOPTIONYIELD`` == ``YIELDATWAP``), and a perpetual by its current
yield.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
from test_kbd import load
from test_moex_eod_goldens import ACT_365, SETTLE, TRADE, yield_and_duration
from test_moex_eod_goldens import instrument as eod_instrument

from rub_fixed_income import (
    AccountingCategory,
    AccrualBasis,
    AssumptionPolicy,
    BondPriceObservation,
    BusinessCalendar,
    CalendarEntry,
    CashflowKind,
    CurveEntry,
    ExerciseAssumption,
    Instrument,
    InstrumentAssumption,
    InvalidTerms,
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
    perpetual_current_yield,
    resolve_exercise,
    value_assumed_portfolio,
    yield_to_call,
    yield_to_maturity,
    yield_to_worst,
)

FIXTURES = Path(__file__).parent / "fixtures" / "moex_calls"
CALENDAR = BusinessCalendar(version="synthetic-weekends-only")


def rows(block: dict[str, Any]) -> list[dict[str, Any]]:
    return [dict(zip(block["columns"], row, strict=True)) for row in block["data"]]


def history(secid: str) -> dict[str, Any]:
    raw = json.loads((FIXTURES / "history" / f"{secid}_TQCB.json").read_text("utf-8"))
    (row,) = rows(raw["history"])
    assert row["TRADEDATE"] == TRADE.isoformat()
    return row


def bondization(secid: str) -> dict[str, Any]:
    return dict(json.loads((FIXTURES / "bondization" / f"{secid}.json").read_text("utf-8")))


def call_bond(secid: str) -> Instrument:
    """Known coupons (rate ACT/365), amortizations and the call, as an adapter builds them.

    The call is the bondization offer "Оферта/Погашение" with a real date. Its price
    in % of the face outstanding then is ``value / face then``, or the offer's
    ``price`` when no amount is given. An amortization listed on the call date is
    the call redemption itself; the face it would redeem is due at the legal maturity.
    """
    raw = bondization(secid)
    coupons = [c for c in rows(raw["coupons"]) if c["value"] is not None]
    leg = RateAccrualCouponLeg(
        tuple(
            RateCouponPeriod(
                date.fromisoformat(c["startdate"]),
                date.fromisoformat(c["coupondate"]),
                date.fromisoformat(c["coupondate"]),
                float(c["value"]),
                c["valueprc"] / 100.0,
            )
            for c in coupons
        ),
        ACT_365,
    )
    face = float(coupons[0]["initialfacevalue"])
    (offer,) = [
        o
        for o in rows(raw["offers"])
        if o["offerdate"] != "0000-00-00" and o["offertype"] == "Оферта/Погашение"
    ]
    call_date = date.fromisoformat(offer["offerdate"])
    amortizations = [a for a in rows(raw["amortizations"]) if a["value"]]
    maturity = date.fromisoformat(history(secid)["MATDATE"])
    before = [a for a in amortizations if date.fromisoformat(a["amortdate"]) < call_date]
    paid = sum(float(a["value"]) for a in before)
    outstanding = face - paid
    redemptions = (
        *(
            Redemption(
                date.fromisoformat(a["amortdate"]), float(a["value"]), CashflowKind.AMORTIZATION
            )
            for a in before
        ),
        Redemption(maturity, outstanding),
    )
    price = 100.0 * offer["value"] / outstanding if offer["value"] else float(offer["price"])
    return Instrument(
        secid,
        ProductType.FIXED_BOND,
        "RUB",
        face,
        leg,
        PrincipalLeg(redemptions),
        "MOEX_STOCK",
        offers=(Offer(call_date, OfferKind.CALL, confirmed=True, price_pct=price),),
    )


def dirty(bond: Instrument, clean_pct: float) -> float:
    return clean_pct / 100.0 * bond.outstanding_face(SETTLE) + bond.accrued(SETTLE)


CALLS = ["RU000A1087A7", "RU000A10AST0"]


@pytest.mark.parametrize("secid", CALLS)
def test_yield_to_call_matches_moex_calloptionyield(secid: str) -> None:
    published = history(secid)
    bond = call_bond(secid)
    call_date = date.fromisoformat(published["CALLOPTIONDATE"])
    assert bond.offers[0].price_pct == 100.0
    assert bond.accrued(TRADE) == pytest.approx(published["ACCINT"], abs=1e-9)
    at_wap = yield_to_call(bond, SETTLE, dirty(bond, published["WAPRICE"]), call_date)
    assert at_wap.effective_yield * 100.0 == pytest.approx(published["CALLOPTIONYIELD"], abs=0.005)
    assert published["CALLOPTIONYIELD"] == published["YIELDATWAP"]
    assert at_wap.macaulay_duration_years * 365.0 == pytest.approx(
        published["CALLOPTIONDURATION"], abs=1.0
    )
    at_close = yield_to_call(bond, SETTLE, dirty(bond, published["CLOSE"]), call_date)
    assert at_close.effective_yield * 100.0 == pytest.approx(published["YIELDCLOSE"], abs=0.005)


@pytest.mark.parametrize("secid", CALLS)
def test_moex_call_bonds_have_no_coupons_to_maturity_so_only_to_call_applies(secid: str) -> None:
    bond = call_bond(secid)
    price = dirty(bond, history(secid)["WAPRICE"])
    with pytest.raises(InvalidTerms, match="not known"):
        yield_to_maturity(bond, SETTLE, price)
    with pytest.raises(InvalidTerms, match="not known"):
        yield_to_worst(bond, SETTLE, price)
    with pytest.raises(InvalidTerms, match="not known"):
        resolve_exercise(bond, SETTLE, ExerciseAssumption.to_maturity())


def snapshot(secids: list[str], prices: dict[str, float]) -> MarketSnapshot:
    params, _ = load("zcyc_2026-10-15.json")
    return MarketSnapshot(
        "moex-2026-10-15-eod",
        params.trade_date,
        datetime(2026, 10, 15, 16, 0, tzinfo=UTC),
        curves=(CurveEntry("RUB_OFZ_KBD", "rub_ofz_kbd_v1", params),),
        calendars=(CalendarEntry("MOEX_STOCK", CALENDAR),),
        quotes=tuple(BondPriceObservation(s, prices[s], "waprice") for s in secids),
    )


def test_value_assumed_portfolio_reproduces_moex_yields_to_call() -> None:
    published = {s: history(s) for s in CALLS}
    result = value_assumed_portfolio(
        run_id="moex-calls",
        as_of=datetime(2026, 10, 15, 16, 0, tzinfo=UTC),
        snapshot=snapshot(CALLS, {s: published[s]["WAPRICE"] for s in CALLS}),
        instruments=tuple(call_bond(s) for s in CALLS),
        positions=tuple(
            Position(s, s, 10, "CREDIT", AccountingCategory.TRADING, "RUB") for s in CALLS
        ),
        policy=ValuationPolicy("RUB_OFZ_KBD", include_second_order=False),
        assumptions=AssumptionPolicy(
            overrides=tuple(
                InstrumentAssumption(
                    s,
                    exercise=ExerciseAssumption.to_call(
                        date.fromisoformat(published[s]["CALLOPTIONDATE"])
                    ),
                )
                for s in CALLS
            )
        ),
    )
    assert result.status == "complete" and result.total_pv_rub is not None
    for row in result.portfolio.positions:
        moex = published[row.position_id]
        assert row.horizon_date == date.fromisoformat(moex["CALLOPTIONDATE"])
        assert row.effective_yield * 100.0 == pytest.approx(moex["CALLOPTIONYIELD"], abs=0.005)
        assert row.macaulay_duration_years * 365.0 == pytest.approx(
            moex["CALLOPTIONDURATION"], abs=1.0
        )
    assert [u.assumption for u in result.assumptions] == [
        f"exercise:to_call:{published[s]['CALLOPTIONDATE']}@call:{published[s]['CALLOPTIONDATE']}"
        for s in CALLS
    ]


def test_call_bond_without_a_set_call_date_is_valued_to_maturity_as_before() -> None:
    """RU000A10B7W1: MOEX yields it to maturity (no call date set). With a call added
    and the default to_maturity assumption, the portfolio reproduces the same figures
    as the plain bond, which ``value_portfolio`` alone would reject as callable."""
    published = json.loads(
        (
            Path(__file__).parent / "fixtures" / "moex_eod" / "history" / "RU000A10B7W1_TQCB.json"
        ).read_text("utf-8")
    )
    (moex,) = rows(published["history"])
    plain = eod_instrument("RU000A10B7W1", ACT_365)
    leg = plain.coupon_leg
    assert isinstance(leg, RateAccrualCouponLeg) and leg.accrual_basis is AccrualBasis.RATE_ACT_365
    a_coupon_date = next(p.payment_date for p in leg.periods if p.payment_date.year == 2027)
    callable_bond = Instrument(
        plain.instrument_id,
        plain.product,
        plain.currency,
        plain.initial_face,
        plain.coupon_leg,
        plain.principal_leg,
        plain.calendar_id,
        offers=(Offer(a_coupon_date, OfferKind.CALL, confirmed=False),),
    )
    secid = plain.instrument_id
    common = {
        "run_id": "moex-call-to-maturity",
        "as_of": datetime(2026, 10, 15, 16, 0, tzinfo=UTC),
        "snapshot": snapshot([secid], {secid: moex["WAPRICE"]}),
        "positions": (Position(secid, secid, 1, "CREDIT", AccountingCategory.TRADING, "RUB"),),
        "policy": ValuationPolicy("RUB_OFZ_KBD", include_second_order=False),
    }
    assumed = value_assumed_portfolio(
        instruments=(callable_bond,),
        assumptions=AssumptionPolicy(default_exercise=ExerciseAssumption.to_maturity()),
        **common,  # type: ignore[arg-type]
    )
    baseline = value_assumed_portfolio(
        instruments=(plain,),
        assumptions=AssumptionPolicy(),
        **common,  # type: ignore[arg-type]
    )
    unassumed = value_assumed_portfolio(
        instruments=(callable_bond,),
        assumptions=AssumptionPolicy(),
        **common,  # type: ignore[arg-type]
    )
    (row,) = assumed.portfolio.positions
    (base_row,) = baseline.portfolio.positions
    assert row.horizon_date == plain.maturity_date
    assert row.pv_rub == base_row.pv_rub and row.factor_risk == base_row.factor_risk
    assert row.effective_yield == base_row.effective_yield
    at_wap, duration = yield_and_duration(plain, moex["WAPRICE"], SETTLE)
    assert row.effective_yield * 100.0 == pytest.approx(at_wap, abs=1e-9)
    assert row.effective_yield * 100.0 == pytest.approx(moex["YIELDATWAP"], abs=0.005)
    assert row.macaulay_duration_years * 365.0 == pytest.approx(moex["DURATION"], abs=1.0)
    assert duration == pytest.approx(row.macaulay_duration_years * 365.0, abs=1e-9)
    (rejection,) = unassumed.coverage.rejections
    assert rejection.message == "callable bonds are not supported (package 9)"


PERPETUALS = ["RU000A0ZZE87", "RU000A1034P7"]


@pytest.mark.parametrize("secid", PERPETUALS)
def test_moex_perpetual_yield_is_the_current_yield(secid: str) -> None:
    published = history(secid)
    assert published["MATDATE"] is None and published["DURATION"] is None
    assert rows(bondization(secid)["amortizations"]) == []
    rate = published["COUPONPERCENT"] / 100.0
    for price, measure in (("CLOSE", "YIELDCLOSE"), ("WAPRICE", "YIELDATWAP")):
        ours = perpetual_current_yield(rate, published[price]) * 100.0
        assert ours == pytest.approx(published[measure], abs=0.005)
