"""XCCY RUB leg on any compounded index and on the averaged key rate (0.43.0).

Closed forms by hand on synthetic curves: the key-rate leg is the calendar-day arithmetic
average (the MOEX IRS KEYRATE terms of ``swaps``), RUSFAR-like legs compound; RUONIA
results are not touched (``test_xccy`` and the goldens guard them).
"""

from __future__ import annotations

import math
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest
from portfolio_v3_book import BUSINESS_DATE, CAL
from test_fx_portfolio_v036 import RUB_CURVE, built, market, pos, run

from rub_fixed_income import (
    RUB_KEY_RATE_AVERAGE,
    RUONIA_OIS_COMPOUND,
    AveragedIndex,
    BusinessCalendar,
    CalendarEntry,
    CompoundedIndex,
    CurveEntry,
    DayCount,
    DiscountCurve,
    FixingEntry,
    FixingHistory,
    IndexFixingEntry,
    IndexFixings,
    InvalidTerms,
    KeyRateForwardCurve,
    KeyRateForwardSegment,
    MarketSnapshot,
    MissingMarketData,
    XccyInstrument,
    XccyLeg,
    XccyPeriod,
    XccyRiskEngine,
    XccyRiskRequest,
    XccyScenario,
    XccySwap,
    from_dict,
    key_rate_projection_curve,
    notional_exchanges,
    to_dict,
    xccy_horizon,
)

C = date(2026, 10, 15)
RU = BusinessCalendar(
    frozenset({date(2026, 11, 4), date(2027, 1, 1), date(2027, 1, 7)}),
    frozenset(),
    "ru-test",
    date(2025, 1, 1),
    date(2034, 12, 31),
)
R_RUB = 0.15
S = 11.2
SPOT_DATE = date(2026, 10, 16)
RUB = DiscountCurve.flat(C, R_RUB)
CNY = DiscountCurve.flat(C, 0.03)
N_CNY = 1_000_000.0
N_RUB = N_CNY * 11.25
RUSFAR = CompoundedIndex("RUB-RUSFAR-OIS-COMPOUND", "nfa_act", "nfa_act")
CNY_X = CompoundedIndex("CNY-X", "act_365f", "act_365f")
ACT360 = DayCount.ACT_360
ACT365 = DayCount.ACT_365_FIXED

STEP = date(2026, 12, 18)
FORWARD = KeyRateForwardCurve(
    C,
    0.16,
    STEP,
    (KeyRateForwardSegment(STEP, date(2029, 1, 1), 0.14, "1Y"),),
)
KEY_PROJ = key_rate_projection_curve(
    FORWARD, RU, rate_day_count=ACT365, observation_days="calendar"
)
FLAT_FORWARD = KeyRateForwardCurve(
    C,
    0.16,
    date(2026, 10, 20),
    (KeyRateForwardSegment(date(2026, 10, 20), date(2029, 1, 1), 0.16, "1Y"),),
)
FLAT_PROJ = key_rate_projection_curve(
    FLAT_FORWARD, RU, rate_day_count=ACT365, observation_days="calendar"
)


def df(rate: float, day: date) -> float:
    return math.exp(-rate * (day - C).days / 365.0)


def swap(
    rub_index: CompoundedIndex | AveragedIndex | None,
    *,
    start: date = SPOT_DATE,
    end: date = date(2027, 4, 16),
    spread: float = 0.002,
    foreign_index: CompoundedIndex | None = None,
    foreign_spread: float = 0.0,
) -> XccySwap:
    pay = end + timedelta(days=1)
    rub_periods = (XccyPeriod(start, end, pay, N_RUB),)
    cny_periods = (XccyPeriod(start, end, pay, N_CNY),)
    return XccySwap(
        "RUBIDX",
        rub_leg=XccyLeg(
            "RUB",
            "pay",
            ACT360,
            rub_periods,
            notional_exchanges(rub_periods, initial_date=None, final_date=end),
            index=rub_index,
            fixed_rate=0.16 if rub_index is None else None,
            spread=0.0 if rub_index is None else spread,
        ),
        foreign_leg=XccyLeg(
            "CNY",
            "receive",
            ACT365,
            cny_periods,
            notional_exchanges(cny_periods, initial_date=None, final_date=end),
            index=foreign_index,
            fixed_rate=None if foreign_index else 0.03,
            spread=foreign_spread if foreign_index else 0.0,
        ),
    )


def request(s: XccySwap, **changes: object) -> XccyRiskRequest:
    fields: dict[str, object] = {
        "swap": s,
        "spot_rate": S,
        "spot_value_date": SPOT_DATE,
        "rub_discount_curve": RUB,
        "foreign_discount_curve": CNY,
        "basis_bp": 0.0,
    }
    fields.update(changes)
    return XccyRiskRequest(**fields)  # type: ignore[arg-type]


def key_history(rows: tuple[tuple[date, float], ...] | None = None) -> FixingHistory:
    rows = rows or ((date(2026, 6, 1), 0.18), (date(2026, 9, 15), 0.165))
    return FixingHistory("key_rate", C, rows, RU, "key-snap")


def rub_coupons(engine: XccyRiskEngine) -> list[float]:
    return [row.amount for row in engine.cashflows() if row.leg == "rub" and row.kind == "coupon"]


def hand_average(start: date, end: date, known: dict[date, float] | None = None) -> float:
    total, day = 0.0, start
    while day < end:
        total += FORWARD.rate_at(day) if day >= C else (known or {})[day]
        day += timedelta(days=1)
    return total / (end - start).days


def in_force(rows: tuple[tuple[date, float], ...], start: date, end: date) -> dict[date, float]:
    out, day = {}, start
    while day < end:
        out[day] = [r for d, r in rows if d <= day][-1]
        day += timedelta(days=1)
    return out


# --- Averaged key rate ------------------------------------------------------------------


def test_averaged_key_rate_coupon_is_the_calendar_day_average_plus_spread() -> None:
    s = swap(RUB_KEY_RATE_AVERAGE)
    engine = XccyRiskEngine(request(s, rub_projection_curve=KEY_PROJ))
    start, end = SPOT_DATE, date(2027, 4, 16)
    avg = hand_average(start, end)
    alpha = (end - start).days / 360.0
    assert rub_coupons(engine) == [pytest.approx(-N_RUB * (avg + 0.002) * alpha, rel=1e-12)]
    # the average is an arithmetic one: a compounded key rate would be ~1.3 pp higher
    assert 0.14 < avg < 0.16
    # PV of the RUB leg: the coupon and the final exchange on the flat RUB curve
    pay = end + timedelta(days=1)
    expected = -N_RUB * (avg + 0.002) * alpha * df(R_RUB, pay) - N_RUB * df(R_RUB, end)
    assert engine.risk().rub_leg_pv_rub == pytest.approx(expected, rel=1e-12)


def test_flat_key_curve_gives_the_flat_rate() -> None:
    engine = XccyRiskEngine(
        request(swap(RUB_KEY_RATE_AVERAGE, spread=0.0), rub_projection_curve=FLAT_PROJ)
    )
    alpha = (date(2027, 4, 16) - SPOT_DATE).days / 360.0
    assert rub_coupons(engine) == [pytest.approx(-N_RUB * 0.16 * alpha, rel=1e-12)]
    rows = [r for r in engine.cashflows() if r.leg == "rub" and r.kind == "coupon"]
    assert rows[0].rate == pytest.approx(0.16, rel=1e-12)


def test_started_period_takes_known_days_from_the_history_and_projects_the_rest() -> None:
    rows = ((date(2026, 6, 1), 0.18), (date(2026, 9, 15), 0.165), (date(2026, 10, 9), 0.155))
    start, end = date(2026, 9, 1), date(2027, 3, 1)
    s = swap(RUB_KEY_RATE_AVERAGE, start=start, end=end)
    engine = XccyRiskEngine(
        request(s, rub_projection_curve=KEY_PROJ, rub_fixings=key_history(rows))
    )
    known = in_force(rows, start, C)
    avg = hand_average(start, end, known)
    alpha = (end - start).days / 360.0
    assert rub_coupons(engine) == [pytest.approx(-N_RUB * (avg + 0.002) * alpha, rel=1e-12)]
    flow = engine.cashflows()
    assert any(f.source == "partially_projected" for f in flow if f.leg == "rub")
    # the decision inside the period (16.5 -> 15.5) is in the known part: average moves
    plain = hand_average(start, end, in_force(rows[:2], start, C))
    assert avg < plain


def test_started_period_without_history_or_with_a_gap_is_missing_market_data() -> None:
    s = swap(RUB_KEY_RATE_AVERAGE, start=date(2026, 9, 1), end=date(2027, 3, 1))
    with pytest.raises(MissingMarketData):
        XccyRiskEngine(request(s, rub_projection_curve=KEY_PROJ))
    late = key_history(((date(2026, 9, 15), 0.165),))  # nothing in force on 1 Sep
    with pytest.raises(MissingMarketData):
        XccyRiskEngine(request(s, rub_projection_curve=KEY_PROJ, rub_fixings=late))


def test_period_fully_known_is_a_fixed_amount() -> None:
    rows = ((date(2026, 6, 1), 0.18), (date(2026, 9, 15), 0.165))
    start, end = date(2026, 9, 1), date(2026, 10, 10)
    s = swap(RUB_KEY_RATE_AVERAGE, start=start, end=end)
    late = (replace(s.rub_leg.periods[0], payment_date=date(2026, 10, 20)),)
    s = replace(s, rub_leg=replace(s.rub_leg, periods=late, exchanges=()))
    engine = XccyRiskEngine(
        request(s, rub_projection_curve=KEY_PROJ, rub_fixings=key_history(rows))
    )
    known = in_force(rows, start, end)
    avg = sum(known.values()) / (end - start).days
    alpha = (end - start).days / 360.0
    assert rub_coupons(engine) == [pytest.approx(-N_RUB * (avg + 0.002) * alpha, rel=1e-12)]
    assert [r.source for r in engine.cashflows() if r.leg == "rub"] == ["fixed_by_history"]


def test_averaged_leg_requires_a_projection_curve_and_the_key_rate_history() -> None:
    s = swap(RUB_KEY_RATE_AVERAGE)
    with pytest.raises(MissingMarketData, match="rub_projection_curve"):
        request(s)
    ruonia = FixingHistory("ruonia", C, (), RU, "ruonia-snap")
    with pytest.raises(InvalidTerms, match="key-rate"):
        request(s, rub_projection_curve=KEY_PROJ, rub_fixings=ruonia)
    idx = IndexFixings(RUSFAR.name, C, (), RU, "x")
    with pytest.raises(TypeError):
        request(s, rub_projection_curve=KEY_PROJ, rub_fixings=idx)
    with pytest.raises(InvalidTerms, match="dated the valuation date"):
        request(
            s,
            rub_projection_curve=KEY_PROJ,
            rub_fixings=FixingHistory("key_rate", C + timedelta(days=1), (), RU, "x"),
        )


def test_key_history_is_rejected_on_a_ruonia_leg_and_vice_versa() -> None:
    s = swap(RUONIA_OIS_COMPOUND)
    with pytest.raises(InvalidTerms, match="RUONIA"):
        request(s, rub_fixings=key_history())
    with pytest.raises(TypeError):
        request(s, rub_fixings=IndexFixings(RUSFAR.name, C, (), RU, "x"))


def test_averaged_index_is_a_rub_leg_index_only() -> None:
    s = swap(RUB_KEY_RATE_AVERAGE)
    with pytest.raises(InvalidTerms, match="RUB leg only"):
        XccyLeg(
            "CNY",
            "receive",
            ACT365,
            s.foreign_leg.periods,
            s.foreign_leg.exchanges,
            index=RUB_KEY_RATE_AVERAGE,
        )
    with pytest.raises(InvalidTerms):
        AveragedIndex("X", "ruonia")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        AveragedIndex("X", rate_day_count="ACT/365")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        replace(s.rub_leg, index="RUB-KEYRATE-AVERAGE")  # type: ignore[arg-type]


def test_averaged_swap_round_trips_and_has_its_own_identity() -> None:
    s = swap(RUB_KEY_RATE_AVERAGE)
    assert from_dict(XccySwap, to_dict(s)) == s
    a = XccyRiskEngine(request(s, rub_projection_curve=KEY_PROJ)).base_id
    b = XccyRiskEngine(request(s, rub_projection_curve=FLAT_PROJ)).base_id
    assert a != b


def test_projection_curve_bump_moves_the_pv_like_a_hand_calculation() -> None:
    s = swap(RUB_KEY_RATE_AVERAGE)
    engine = XccyRiskEngine(request(s, rub_projection_curve=KEY_PROJ))
    assert "rub_projection" in engine.roles
    assert engine.risk().curve("rub_projection").dv01_rub != 0.0
    shift = 10.0
    moved = engine.reprice(XccyScenario(rub_projection_shifts_bp=(shift,) * len(_zero())))
    # parallel shift of the continuous zero rates by 10 bp: every node DF * exp(-shift t)
    bumped = replace(
        KEY_PROJ,
        nodes=tuple(
            (d, f * math.exp(-shift * 1e-4 * (d - C).days / 365.0)) for d, f in KEY_PROJ.nodes
        ),
    )
    base = XccyRiskEngine(request(s, rub_projection_curve=KEY_PROJ))
    hand = XccyRiskEngine(request(s, rub_projection_curve=bumped))
    assert moved.shocked_pv_rub == pytest.approx(hand.base_pv_rub, rel=1e-9)
    assert moved.base_pv_rub == pytest.approx(base.base_pv_rub, rel=1e-12)
    assert hand.base_pv_rub < base.base_pv_rub  # the position pays the floating leg


def _zero() -> tuple[float, ...]:
    return XccyScenario().rub_shifts_bp


# --- Other compounded RUB indices ---------------------------------------------------------


def rusfar_fixings(level: float = 0.158) -> IndexFixings:
    rows = []
    day = date(2026, 9, 1)
    while day < C:
        if RU.is_business_day(day):
            rows.append((day, level))
        day += timedelta(days=1)
    return IndexFixings(RUSFAR.name, C, tuple(rows), RU, "rusfar-snap")


def hand_compounded(
    index: CompoundedIndex, start: date, end: date, fixing: float, projection_rate: float
) -> float:
    """Growth by hand: business-day segments, fixings before C, the projection after."""
    growth, day = 1.0, start
    weight = index.weight_days(start, end)
    while day < end:
        nxt = day + timedelta(days=1)
        while nxt < end and not RU.is_business_day(nxt):
            nxt += timedelta(days=1)
        if day < C:
            growth *= 1.0 + fixing * (nxt - day).days / weight
        else:
            growth *= df(projection_rate, day) / df(projection_rate, nxt)
        day = nxt
    return growth


@pytest.mark.parametrize("projection", [None, DiscountCurve.flat(C, 0.155)])
def test_rusfar_leg_against_a_fixed_cny_leg(projection: DiscountCurve | None) -> None:
    start, end = date(2026, 10, 1), date(2027, 4, 1)
    s = swap(RUSFAR, start=start, end=end)
    engine = XccyRiskEngine(
        request(s, rub_projection_curve=projection, rub_fixings=rusfar_fixings())
    )
    rate = R_RUB if projection is None else 0.155
    growth = hand_compounded(RUSFAR, start, end, 0.158, rate)
    alpha = (end - start).days / 360.0
    coupon = N_RUB * ((growth - 1.0) * RUSFAR.annualization(start, end) + 0.002) * alpha
    assert rub_coupons(engine) == [pytest.approx(-coupon, rel=1e-12)]


def test_rusfar_leg_not_started_needs_no_fixings() -> None:
    engine = XccyRiskEngine(
        request(swap(RUSFAR), rub_projection_curve=DiscountCurve.flat(C, 0.155))
    )
    start, end = SPOT_DATE, date(2027, 4, 16)
    growth = df(0.155, start) / df(0.155, end)
    alpha = (end - start).days / 360.0
    coupon = N_RUB * ((growth - 1.0) * RUSFAR.annualization(start, end) + 0.002) * alpha
    assert rub_coupons(engine) == [pytest.approx(-coupon, rel=1e-12)]


def test_rusfar_fixings_must_be_complete_and_of_the_leg_index() -> None:
    start, end = date(2026, 10, 1), date(2027, 4, 1)
    s = swap(RUSFAR, start=start, end=end)
    with pytest.raises(MissingMarketData):
        XccyRiskEngine(request(s))
    full = rusfar_fixings()
    gap = IndexFixings(
        full.index, C, tuple(r for r in full.fixings if r[0] != date(2026, 10, 8)), RU, "gap"
    )
    with pytest.raises(MissingMarketData):
        XccyRiskEngine(request(s, rub_fixings=gap))
    other = IndexFixings("RUB-OTHER", C, full.fixings, RU, "other")
    with pytest.raises(InvalidTerms, match="RUB leg's index"):
        request(s, rub_fixings=other)
    with pytest.raises(TypeError):
        request(s, rub_fixings=key_history())
    with pytest.raises(TypeError):
        request(s, rub_fixings=FixingHistory("ruonia", C, (), RU, "r"))


def test_rusfar_against_a_floating_cny_leg() -> None:
    cny_fixings = IndexFixings(CNY_X.name, C, (), RU, "cny-snap")
    s = swap(RUSFAR, foreign_index=CNY_X, foreign_spread=0.001)
    engine = XccyRiskEngine(
        request(
            s,
            foreign_projection_curve=DiscountCurve.flat(C, 0.02),
            foreign_fixings=cny_fixings,
        )
    )
    assert "foreign_projection" in engine.roles and "rub_projection" not in engine.roles
    assert engine.base_pv_rub == pytest.approx(
        engine.risk().rub_leg_pv_rub + engine.risk().foreign_leg_pv_rub, rel=1e-12
    )


def test_ruonia_on_the_foreign_leg_is_still_rejected() -> None:
    s = swap(RUSFAR)
    with pytest.raises(InvalidTerms, match="RUB index"):
        replace(
            s,
            foreign_leg=replace(s.foreign_leg, index=RUONIA_OIS_COMPOUND, fixed_rate=None),
        )


def test_unknown_rub_index_no_longer_raises_unsupported() -> None:
    assert swap(CompoundedIndex("ANY-RUB-INDEX", "act_365f", "act_365f"))


# --- Horizon --------------------------------------------------------------------------------


def test_horizon_of_an_averaged_leg_keeps_todays_forwards_up_to_the_horizon() -> None:
    s = swap(RUB_KEY_RATE_AVERAGE)
    req = request(s, rub_projection_curve=KEY_PROJ)
    h = C + timedelta(days=30)
    spot_h = h + timedelta(days=1)
    valuation = xccy_horizon(req, horizon_date=h, horizon_spot_value_date=spot_h)
    assert valuation.received_rub == 0.0
    # hand: the first 30 days (from the period start) at today's path, the rest on the
    # path rolled by 30 days
    start, end = SPOT_DATE, date(2027, 4, 16)
    total = 0.0
    day = start
    while day < end:
        if day < h:
            total += FORWARD.rate_at(day)
        else:
            total += FORWARD.rate_at(day - timedelta(days=30))
        day += timedelta(days=1)
    avg = total / (end - start).days
    alpha = (end - start).days / 360.0
    coupon = -N_RUB * (avg + 0.002) * alpha
    rolled_rub = DiscountCurve.flat(h, R_RUB)
    pay = end + timedelta(days=1)
    rub_pv = coupon * rolled_rub.discount_factor(pay) - N_RUB * rolled_rub.discount_factor(end)
    foreign = N_CNY * 0.0
    del foreign
    base = XccyRiskEngine(req)
    flows = base.cashflows()
    assert flows  # sanity
    # the RUB leg of the rolled PV: compare through the difference with a fixed-leg twin
    twin = swap(None)
    twin_req = request(twin)
    twin_val = xccy_horizon(twin_req, horizon_date=h, horizon_spot_value_date=spot_h)
    twin_pv = XccyRiskEngine(twin_req)
    twin_coupon = -N_RUB * 0.16 * alpha
    twin_rub = twin_coupon * rolled_rub.discount_factor(pay) - N_RUB * rolled_rub.discount_factor(
        end
    )
    assert valuation.pv_rolled_rub - twin_val.pv_rolled_rub == pytest.approx(
        rub_pv - twin_rub, rel=1e-9
    )
    assert twin_pv.base_pv_rub


def test_horizon_after_the_payment_pays_todays_amount() -> None:
    s = swap(RUB_KEY_RATE_AVERAGE, end=date(2026, 12, 16))
    req = request(s, rub_projection_curve=KEY_PROJ)
    h = date(2026, 12, 21)
    valuation = xccy_horizon(req, horizon_date=h, horizon_spot_value_date=h + timedelta(days=1))
    flows = [f for f in XccyRiskEngine(req).cashflows() if f.leg == "rub"]
    coupon = next(f.amount for f in flows if f.kind == "coupon")
    exchange = next(f.amount for f in flows if f.kind == "exchange")
    foreign = [f for f in XccyRiskEngine(req).cashflows() if f.leg == "foreign"]
    assert valuation.received_rub == pytest.approx(
        coupon + exchange + sum(f.amount for f in foreign) * S, rel=1e-6
    )


# --- value_portfolio ----------------------------------------------------------------------


KNOWN = "2026-10-15T12:00:00Z"
AS_OF = datetime(2026, 10, 15, 16, 30, tzinfo=UTC)


def portfolio_market(
    rows: tuple[tuple[date, float], ...] | None = None, **changes: object
) -> MarketSnapshot:
    rows = rows or ((date(2026, 6, 1), 0.18), (date(2026, 9, 15), 0.165))
    proj = key_rate_projection_curve(
        KeyRateForwardCurve(
            BUSINESS_DATE,
            0.16,
            STEP,
            (KeyRateForwardSegment(STEP, date(2029, 1, 1), 0.14, "1Y"),),
        ),
        CAL,
        rate_day_count=ACT365,
        observation_days="calendar",
    )
    history = FixingHistory("key_rate", BUSINESS_DATE, rows, CAL, "key-snap", known_at_utc=KNOWN)
    base = market("CNY", calendars=(CalendarEntry("FX", CAL), CalendarEntry("MOEX", CAL)))
    base = replace(
        base,
        curves=(*base.curves, CurveEntry("KEY_PROJ", "rub_key_rate_projection_v1", proj)),
        fixings=(FixingEntry("KEY_RATE", history),),
    )
    return replace(base, **changes)  # type: ignore[arg-type]


def portfolio_swap(start: date, end: date) -> XccySwap:
    rub_periods = (XccyPeriod(start, end, end, N_RUB),)
    cny_periods = (XccyPeriod(start, end, end, N_CNY),)
    return XccySwap(
        "XKEY",
        XccyLeg(
            "RUB",
            "pay",
            ACT360,
            rub_periods,
            notional_exchanges(rub_periods, initial_date=None, final_date=end),
            index=RUB_KEY_RATE_AVERAGE,
            spread=0.002,
        ),
        XccyLeg(
            "CNY",
            "receive",
            ACT365,
            cny_periods,
            notional_exchanges(cny_periods, initial_date=None, final_date=end),
            fixed_rate=0.03,
        ),
    )


def test_value_portfolio_prices_the_key_rate_leg_with_projection_risk() -> None:
    start, end = date(2026, 9, 1), date(2027, 3, 1)
    inst = XccyInstrument(
        "XKEY",
        portfolio_swap(start, end),
        "RUB_OIS",
        "CNY_DF",
        rub_projection_curve_id="KEY_PROJ",
        rub_fixing_id="KEY_RATE",
    )
    result = run(portfolio_market(), [inst], [pos("p", "XKEY", 1.0, "CNY")])
    assert result.coverage.status == "complete", result.coverage.rejections
    row = result.xccy_swaps[0]
    assert ("rub_projection", "KEY_PROJ") in row.curve_ids
    assert any(f.startswith("IR|KEY_PROJ|") for f in row.factor_risk.factors)
    # RUB leg PV by hand on the snapshot's RUB curve
    rows = ((date(2026, 6, 1), 0.18), (date(2026, 9, 15), 0.165))
    known = in_force(rows, start, BUSINESS_DATE)
    total = sum(known.values())
    day = BUSINESS_DATE
    while day < end:
        total += FORWARD.rate_at(day)
        day += timedelta(days=1)
    avg = total / (end - start).days
    alpha = (end - start).days / 360.0
    rub_curve = built(CurveEntry("x", "rub_ruonia_ois_legacy_v1", RUB_CURVE))
    expected = -N_RUB * ((avg + 0.002) * alpha + 1.0) * rub_curve.discount_factor(end)
    assert row.rub_leg_pv_rub == pytest.approx(expected, rel=1e-10)
    # without the history the position is rejected, not guessed
    missing = run(portfolio_market(fixings=()), [inst], [pos("p", "XKEY", 1.0, "CNY")])
    assert missing.coverage.rejections[0].code == "MISSING_MARKET_DATA"


def test_instrument_rules_for_the_new_legs() -> None:
    s = portfolio_swap(date(2026, 9, 1), date(2027, 3, 1))
    with pytest.raises(InvalidTerms, match="rub_projection_curve_id"):
        XccyInstrument("X", s, "RUB_OIS", "CNY_DF", rub_fixing_id="KEY_RATE")
    rusfar = swap(RUSFAR)
    # a compounded non-RUONIA leg may project on the discount curve
    XccyInstrument("X", rusfar, "RUB_OIS", "CNY_DF", rub_fixing_id="RUSFAR")


def test_non_ruonia_compounded_leg_reads_snapshot_index_fixings() -> None:
    from portfolio_v3_book import TOM

    start, end = date(2026, 10, 1), date(2027, 4, 1)
    cal = CAL
    rows = tuple(
        (d, 0.158)
        for d in (start + timedelta(days=i) for i in range((BUSINESS_DATE - start).days))
        if cal.is_business_day(d)
    )
    fixings = IndexFixings(RUSFAR.name, BUSINESS_DATE, rows, cal, "rusfar-snap")
    s = swap(RUSFAR, start=start, end=end)
    inst = XccyInstrument("XR", s, "RUB_OIS", "CNY_DF", rub_fixing_id="RUSFAR")
    snap = replace(portfolio_market(), index_fixings=(IndexFixingEntry("RUSFAR", fixings),))
    result = run(snap, [inst], [pos("p", "XR", 1.0, "CNY")])
    assert result.coverage.status == "complete", result.coverage.rejections
    assert not any(role == "rub_projection" for role, _ in result.xccy_swaps[0].curve_ids)
    assert TOM
