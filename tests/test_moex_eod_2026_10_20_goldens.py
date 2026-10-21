"""Golden tests on the MOEX EOD of 2026-10-20 (desk-monitor review, 0.33.0).

Fixtures and provenance: ``tests/fixtures/moex_eod_2026-10-20/README.md``. The bonds are
a subset of the 312-bond stratified sample checked in
``docs/reviews/CORE_REVIEW_FOR_DESK_MONITOR.md`` (every kind the desk holds: OFZ-PD,
OFZ-AD, OFZ-PK in arrears, in advance and on term RUONIA, key-rate and RUONIA corporate
floaters, corporate bullets, amortizers, puts with and without exercise windows, calls).
The ISS payloads are parsed here, test side; the library does no I/O.

What is checked against MOEX:

- ``ACCINT`` (accrued on the trade date) to the kopeck: fixed legs (OFZ pro rata on the
  amount, corporates on the rate) and floaters (``FloatingLeg.accrued_interest``);
- ``YIELDCLOSE`` (to maturity) and ``YIELDTOOFFER`` (puts) at ``CLOSE``, settlement T+1,
  effective annual ACT/365F, within the published rounding;
- ``DURATION``: the Macaulay days at the published (rounded) ``YIELDATWAP``, rounded to
  a day, exactly; it is not the duration at ``CLOSE`` (RU000A107PU5: 1233, at CLOSE 1211).
  Put bonds: at the published ``YIELDTOOFFER``;
- ``GSPREADBP`` of the live board rows still on the 2026-10-20 EOD KBD (``ZCYCMOMENT``):
  the effective yield minus the KBD yield at the duration, in whole bp.
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from functools import cache
from pathlib import Path
from typing import Any

import pytest

from rub_fixed_income import (
    AccountingCategory,
    AccrualBasis,
    BondCashflow,
    BondPriceObservation,
    BusinessCalendar,
    CalendarEntry,
    CashflowKind,
    CouponPeriod,
    CurveEntry,
    DayCount,
    FixedCouponLeg,
    FixingEntry,
    FixingHistory,
    FloatingCouponLeg,
    FloatingCouponPeriod,
    FloatingCouponRule,
    Instrument,
    MarketSnapshot,
    ObservationShift,
    ObservationWindow,
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
    ZcycParameters,
    bond_duration,
    g_spread_bp,
    solve_effective_annual_yield,
    value_portfolio,
)

FIXTURES = Path(__file__).parent / "fixtures" / "moex_eod_2026-10-20"
TRADE = date(2026, 10, 20)
SETTLE = date(2026, 10, 21)
OFZ = ("SU",)

FIXED = {
    "TQOB": ("SU26207RMFS9", "SU26230RMFS1", "SU26238RMFS4", "SU26248RMFS3", "SU46012RMFS9"),
    "TQCB": (
        "RU000A107PU5",
        "RU000A10C6W2",
        "RU000A0JXN21",
        "RU000A10FG76",
        "RU000A0JX199",
        "RU000A10FBR9",
        "RU000A10DPT0",
        "RU000A10BLP1",
        "RU000A10EXU0",
    ),
}
PUTS = ("RU000A0JT6B2", "RU000A10BZT3", "RU000A100Q50")
# Two scheduled offers a week apart (2027-02-19, 2027-02-26), no windows: MOEX's
# YIELDTOOFFER is to the second, its DURATION to the first.
TWO_OFFERS = "RU000A10CJ84"


# --- fixture parsing (test side) -------------------------------------------------------------


def _rows(block: dict[str, Any]) -> list[dict[str, Any]]:
    return [dict(zip(block["columns"], row, strict=True)) for row in block["data"]]


def _json(*parts: str) -> dict[str, Any]:
    return dict(json.loads(FIXTURES.joinpath(*parts).read_text("utf-8")))


def _day(text: str | None) -> date | None:
    return None if not text or text.startswith("0000") else date.fromisoformat(text[:10])


def eod(secid: str, board: str) -> dict[str, Any]:
    (row,) = _rows(_json("history", f"{secid}_{board}.json")["history"])
    assert row["TRADEDATE"] == TRADE.isoformat()
    return row


def bondization(secid: str) -> dict[str, list[dict[str, Any]]]:
    raw = _json("bondization", f"{secid}.json")
    return {k: _rows(raw[k]) for k in ("coupons", "amortizations", "offers")}


def description(secid: str) -> dict[str, str]:
    return {r["name"]: r["value"] for r in _rows(_json("description", f"{secid}.json")["description"])}


def coupon_periods(secid: str) -> list[tuple[date, date, date, dict[str, Any]]]:
    """(start, accrual end, payment, row): a period ends at the next start when its
    payment date was moved past it (a holiday)."""
    rows = sorted(
        (c for c in bondization(secid)["coupons"] if _day(c["coupondate"]) and _day(c["startdate"])),
        key=lambda c: c["coupondate"],
    )
    out = []
    for i, c in enumerate(rows):
        start, pay = _day(c["startdate"]), _day(c["coupondate"])
        assert start is not None and pay is not None
        nxt = _day(rows[i + 1]["startdate"]) if i + 1 < len(rows) else None
        end = nxt if nxt is not None and start < nxt < pay else pay
        out.append((start, end, pay, c))
    return out


def principal(secid: str) -> PrincipalLeg:
    reds = sorted(
        (date.fromisoformat(a["amortdate"]), float(a["value"]))
        for a in bondization(secid)["amortizations"]
    )
    last = len(reds) - 1
    return PrincipalLeg(
        tuple(
            Redemption(d, v, CashflowKind.MATURITY if i == last else CashflowKind.AMORTIZATION)
            for i, (d, v) in enumerate(reds)
        )
    )


def fixed_bond(secid: str, offer: Offer | None = None) -> Instrument:
    """The bond as the desk adapter builds it: OFZ pro rata on the amount, corporates on
    the rate (ACT/365); coupons up to the horizon, redemptions after the trade date."""
    ofz = secid.startswith(OFZ)
    horizon = None if offer is None else offer.offer_date
    periods: list[CouponPeriod | RateCouponPeriod] = []
    for start, end, pay, c in coupon_periods(secid):
        if horizon is not None and pay > horizon:
            break
        if ofz:
            periods.append(CouponPeriod(start, end, pay, float(c["value"])))
        else:
            periods.append(
                RateCouponPeriod(start, end, pay, float(c["value"]), float(c["valueprc"]) / 100)
            )
    legs = principal(secid)
    leg = (
        FixedCouponLeg(tuple(periods))  # type: ignore[arg-type]
        if ofz
        else RateAccrualCouponLeg(tuple(periods), AccrualBasis.RATE_ACT_365)  # type: ignore[arg-type]
    )
    face = sum(r.amount for r in legs.redemptions)
    return Instrument(
        secid,
        ProductType.FIXED_BOND,
        "RUB",
        face,
        leg,
        legs,
        "MOEX_STOCK",
        offers=() if offer is None else (offer,),
    )


def first_put(secid: str) -> Offer:
    rows = sorted(
        (o for o in bondization(secid)["offers"] if (_day(o["offerdate"]) or TRADE) > SETTLE),
        key=lambda o: o["offerdate"],
    )
    o = rows[0]
    return Offer(
        _day(o["offerdate"]),  # type: ignore[arg-type]
        OfferKind.PUT,
        False,
        float(o["price"] or 100.0),
        window_start=_day(o["offerdatestart"]),
        window_end=_day(o["offerdateend"]),
    )


def days_at(instrument: Instrument, yield_pct: float, policy: str = "maturity") -> float:
    """Macaulay duration in days at a given (published) effective yield."""
    horizon, offer = instrument.horizon(policy, SETTLE)  # type: ignore[arg-type]
    flows = instrument.cashflows(after=SETTLE, horizon=horizon, offer=offer)
    return bond_duration(
        annual_yield=yield_pct / 100.0,
        settlement_date=SETTLE,
        cashflows=tuple((f.payment_date, f.amount) for f in flows),
    ).macaulay_years * 365.0


def yield_and_days(instrument: Instrument, clean: float, policy: str = "maturity") -> tuple[float, float]:
    horizon, offer = instrument.horizon(policy, SETTLE)  # type: ignore[arg-type]
    flows = instrument.cashflows(after=SETTLE, horizon=horizon, offer=offer)
    dirty = clean / 100.0 * instrument.outstanding_face(SETTLE) + instrument.accrued(SETTLE)
    request = YieldRequest(SETTLE, dirty, tuple(BondCashflow(f.payment_date, f.amount) for f in flows))
    y = solve_effective_annual_yield(request).effective_annual_yield
    days = bond_duration(
        annual_yield=y,
        settlement_date=SETTLE,
        cashflows=tuple((f.payment_date, f.amount) for f in flows),
    ).macaulay_years * 365.0
    return y * 100.0, days


# --- fixed-coupon bonds ----------------------------------------------------------------------


@pytest.mark.parametrize(("board", "secid"), [(b, s) for b, ids in FIXED.items() for s in ids])
def test_fixed_accrued_yield_and_duration(board: str, secid: str) -> None:
    row = eod(secid, board)
    bond = fixed_bond(secid)
    assert bond.accrued(TRADE) == row["ACCINT"]
    close_yield, _ = yield_and_days(bond, row["CLOSE"])
    assert close_yield == pytest.approx(row["YIELDCLOSE"], abs=0.005 + 1e-9)
    wap_yield, wap_days = yield_and_days(bond, row["WAPRICE"])
    assert wap_yield == pytest.approx(row["YIELDATWAP"], abs=0.005 + 1e-9)
    assert wap_days == pytest.approx(row["DURATION"], abs=1.0)
    assert round(days_at(bond, row["YIELDATWAP"])) == row["DURATION"]


def test_duration_is_at_the_weighted_average_price_not_the_close() -> None:
    row = eod("RU000A107PU5", "TQCB")
    bond = fixed_bond("RU000A107PU5")
    _, close_days = yield_and_days(bond, row["CLOSE"])
    _, wap_days = yield_and_days(bond, row["WAPRICE"])
    assert row["DURATION"] == 1233 and round(wap_days) == 1233 and round(close_days) == 1211
    assert round(days_at(bond, row["YIELDATWAP"])) == 1233


@pytest.mark.parametrize("secid", PUTS)
def test_put_bonds_yield_to_the_open_offer(secid: str) -> None:
    row = eod(secid, "TQCB")
    offer = first_put(secid)
    bond = fixed_bond(secid, offer)
    assert offer.is_open(TRADE)
    assert bond.horizon("open_put", SETTLE) == (offer.offer_date, offer)
    assert bond.accrued(TRADE) == row["ACCINT"]
    to_offer, days = yield_and_days(bond, row["CLOSE"], "open_put")
    assert to_offer == pytest.approx(row["YIELDTOOFFER"], abs=0.00005 + 1e-9)
    assert days == pytest.approx(row["DURATION"], abs=1.0)
    assert round(days_at(bond, row["YIELDTOOFFER"], "open_put")) == row["DURATION"]


def test_two_scheduled_offers_yield_to_the_second() -> None:
    row = eod(TWO_OFFERS, "TQCB")
    offers = sorted(_day(o["offerdate"]) for o in bondization(TWO_OFFERS)["offers"])  # type: ignore[type-var]
    assert offers == [date(2027, 2, 19), date(2027, 2, 26)]
    measured = {}
    for when in offers:
        put = Offer(when, OfferKind.PUT, True)  # type: ignore[arg-type]
        measured[when] = yield_and_days(fixed_bond(TWO_OFFERS, put), row["CLOSE"], "open_put")
    assert measured[date(2027, 2, 26)][0] == pytest.approx(row["YIELDTOOFFER"], abs=0.00005)
    assert measured[date(2027, 2, 19)][1] == pytest.approx(row["DURATION"], abs=0.5)


# --- floaters ----------------------------------------------------------------------------------


def _cbr(name: str, tag: str) -> list[dict[str, str]]:
    root = ET.parse(FIXTURES / "cbr" / name).getroot()
    return [{c.tag: c.text or "" for c in r} for r in root.iter(tag)]


@cache
def calendar() -> BusinessCalendar:
    """Business days from the RUONIA rate dates, plus 2025-12-30: CBR computes no RUONIA
    on the last business day of the year (a publication holiday, not a market one)."""
    dates = {date.fromisoformat(r["D0"][:10]) for r in _cbr("RuoniaXML.xml", "ro")}
    first, last = min(dates), max(dates)
    holidays, working, day = set(), set(), first
    while day <= last:
        if day.weekday() >= 5 and day in dates:
            working.add(day)
        if day.weekday() < 5 and day not in dates and day != date(2025, 12, 30):
            holidays.add(day)
        day += timedelta(days=1)
    return BusinessCalendar(
        frozenset(holidays), frozenset(working), "cbr-ruonia-dates-2025-12", valid_from=first,
        valid_to=date(2026, 10, 21),
    )


@cache
def history(index: str) -> FixingHistory:
    if index == "ruonia":
        rows = {date.fromisoformat(r["D0"][:10]): float(r["ruo"]) / 100 for r in _cbr("RuoniaXML.xml", "ro")}
        rows[date(2025, 12, 30)] = rows[max(d for d in rows if d < date(2025, 12, 30))]
    else:
        rows = {date.fromisoformat(r["DT"][:10]): float(r["Rate"]) / 100 for r in _cbr("KeyRateXML.xml", "KR")}
    fixings = tuple(sorted((d, v) for d, v in rows.items() if d < TRADE))
    return FixingHistory(index, TRADE, fixings, calendar(), f"cbr-{index}",  # type: ignore[arg-type]
                         known_at_utc="2026-10-21T14:46:00Z")


OLD_OFZ_PK = ("SU29007RMFS0", "SU29008RMFS8")
ACT365, ACT_ACT = DayCount.ACT_365_FIXED, DayCount.ACT_ACT_ISDA


def rule(secid: str) -> FloatingCouponRule:
    """The family conventions of ``tests/test_moex_floater_goldens.py`` and the desk."""
    fields = description(secid)
    bench, margin = fields["COUPON_BENCHMARK"], float(fields["COUPON_BENCHMARK_SPREAD"]) / 100
    if secid in OLD_OFZ_PK:
        return FloatingCouponRule("ruonia", "arithmetic", margin, ACT365, ACT365,
                                  rate_rounding_decimals=2, amount_rounding_decimals=2,
                                  observation_window=ObservationWindow(2, "business_days", 6))
    if bench == "RUONIA3M":
        return FloatingCouponRule("ruonia", "compounded", margin, ACT365, ACT365,
                                  amount_rounding_decimals=2,
                                  observation_shift=ObservationShift(7, "calendar_days", "observation"))
    day_count = ACT_ACT if secid.startswith(OFZ) else ACT365
    index = "key_rate" if bench == "RREFKEYR" else "ruonia"
    assert bench in ("RREFKEYR", "RUONIA")
    return FloatingCouponRule(index, "arithmetic", margin, day_count, day_count,  # type: ignore[arg-type]
                              amount_rounding_decimals=2,
                              observation_shift=ObservationShift(6, "calendar_days", "observation"))


def floater(secid: str) -> Instrument:
    periods = []
    for start, end, pay, c in coupon_periods(secid):
        published = float(c["value"]) if c["value"] and start <= TRADE else None
        rate = float(c["valueprc"]) / 100 if published is not None and c["valueprc"] else None
        periods.append(FloatingCouponPeriod(start, end, pay, float(c["facevalue"]), published, rate))
    the_rule = rule(secid)
    legs = principal(secid)
    return Instrument(
        secid,
        ProductType.FLOATING_BOND,
        "RUB",
        sum(r.amount for r in legs.redemptions),
        FloatingCouponLeg(
            the_rule,
            tuple(periods),
            "KEY_RATE" if the_rule.index == "key_rate" else "RUONIA",
            accrual_basis=None if secid.startswith(OFZ) else AccrualBasis.RATE_ACT_365,
        ),
        legs,
        "MOEX_STOCK",
    )


def floater_market() -> MarketSnapshot:
    return MarketSnapshot(
        "cbr-2026-10-20",
        TRADE,
        datetime(2026, 10, 21, 15, 0, tzinfo=UTC),
        calendars=(CalendarEntry("MOEX_STOCK", calendar()),),
        fixings=(FixingEntry("RUONIA", history("ruonia")), FixingEntry("KEY_RATE", history("key_rate"))),
    )


FLOATERS = {
    "TQOB": ("SU29007RMFS0", "SU29008RMFS8", "SU29013RMFS8", "SU29015RMFS3", "SU29026RMFS0"),
    "TQCB": (
        "RU000A105MP6",
        "RU000A107DS5",
        "RU000A10AHM8",
        "RU000A1038M5",
        "RU000A108DE3",
        "RU000A107050",
        "RU000A107XA1",
    ),
}
# Known gaps (review §3): the first coupon of a new OFZ-PK issue (as SU29026/27, BACKLOG
# #85), and a published corporate coupon without a published rate (accrued 1 kopeck off).
FLOATER_GAPS = {"SU29031RMFS0": ("TQOB", 32.13, 31.62), "RU000A109R19": ("TQCB", 36.57, 36.56)}


@pytest.mark.parametrize(("board", "secid"), [(b, s) for b, ids in FLOATERS.items() for s in ids])
def test_floater_accrued_matches_moex(board: str, secid: str) -> None:
    row = eod(secid, board)
    assert floater(secid).accrued(TRADE, floater_market()) == row["ACCINT"]


@pytest.mark.parametrize("secid", sorted(FLOATER_GAPS))
def test_floater_accrued_known_gaps(secid: str) -> None:
    board, moex, core = FLOATER_GAPS[secid]
    assert eod(secid, board)["ACCINT"] == moex
    assert floater(secid).accrued(TRADE, floater_market()) == core


def test_rate_basis_matters_for_a_corporate_floater() -> None:
    """RU000A108DE3: 40.89 RUB at 16.40 % on 1000; on the rate 36.39 (MOEX), pro rata 36.40."""
    bond = floater("RU000A108DE3")
    leg = bond.coupon_leg
    assert isinstance(leg, FloatingCouponLeg)
    pro_rata = Instrument(
        bond.instrument_id, bond.product, "RUB", bond.initial_face,
        FloatingCouponLeg(leg.rule, leg.periods, leg.fixing_id), bond.principal_leg, "MOEX_STOCK",
    )
    assert bond.accrued(TRADE, floater_market()) == eod("RU000A108DE3", "TQCB")["ACCINT"] == 36.39
    assert pro_rata.accrued(TRADE, floater_market()) == 36.40


# --- G-spread and the portfolio path -------------------------------------------------------


@cache
def kbd() -> ZcycParameters:
    block = _json("kbd", "zcyc_2026-10-20.json")["params"]
    row = dict(zip(block["columns"], block["data"][0], strict=True))
    g = tuple(float(row[f"G{i}"]) for i in range(1, 10))
    return ZcycParameters(
        date.fromisoformat(row["tradedate"]), row["B1"], row["B2"], row["B3"], row["T1"],
        (g[0], g[1], g[2], g[3], g[4], g[5], g[6], g[7], g[8]),
    )


def test_g_spread_matches_moex_on_the_eod_kbd() -> None:
    moment = "2026-10-20 18:54:55"
    rows = [r for r in _rows(_json("marketdata_yields_TQCB.json")["marketdata_yields"])
            if r["ZCYCMOMENT"] == moment]
    assert len(rows) >= 20
    for r in rows:
        g = g_spread_bp(kbd(), r["EFFECTIVEYIELD"] / 100.0, r["DURATION"] / 365.0)
        assert round(g) == r["GSPREADBP"], r["SECID"]


def test_value_portfolio_reproduces_moex_at_the_trade_date_and_t_plus_1() -> None:
    """The desk values at the trade date (settlement lag 0, core brief C2) with MOEX's
    accrued; yields at T+1 (lag 1) are MOEX's. The G-spread is to the EOD KBD."""
    secids = FIXED["TQOB"][:4]
    snapshot_rows = {s: eod(s, "TQOB") for s in secids}
    for lag in (0, 1):
        bonds = [replace(fixed_bond(s), settlement_lag_days=lag) for s in secids]
        result = value_portfolio(
            run_id="eod-2026-10-20",
            as_of=datetime(2026, 10, 21, 15, 0, tzinfo=UTC),
            snapshot=MarketSnapshot(
                "moex-2026-10-20",
                TRADE,
                datetime(2026, 10, 21, 15, 0, tzinfo=UTC),
                curves=(CurveEntry("KBD", "rub_ofz_kbd_v1", kbd()),),
                calendars=(CalendarEntry("MOEX_STOCK", BusinessCalendar(version="weekends")),),
                quotes=tuple(
                    BondPriceObservation(s, snapshot_rows[s]["CLOSE"], "close") for s in secids
                ),
            ),
            instruments=bonds,
            positions=[
                Position(f"p-{s}", s, 1.0, "OFZ", AccountingCategory.TRADING, "RUB") for s in secids
            ],
            policy=ValuationPolicy("KBD", spread_kind="ofz", include_second_order=False,
                                   g_spread_curve_id="KBD"),
        )
        assert result.status == "complete"
        for row in result.positions:
            moex = snapshot_rows[row.instrument_id]
            if lag == 0:
                assert row.settlement_date == TRADE and row.accrued_interest_rub == moex["ACCINT"]
            else:
                assert row.settlement_date == SETTLE
                assert row.effective_yield * 100 == pytest.approx(moex["YIELDCLOSE"], abs=0.005 + 1e-9)
            expected = g_spread_bp(kbd(), row.effective_yield, row.macaulay_duration_years)
            assert row.g_spread_bp == pytest.approx(expected, abs=1e-9)
