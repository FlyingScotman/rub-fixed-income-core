"""Cross-currency swaps (package 7): independent closed forms and reconstructions."""

from __future__ import annotations

import math
from calendar import isleap
from dataclasses import replace
from datetime import date, timedelta

import pytest

from rub_fixed_income import (
    RISK_BUCKETS,
    RUONIA_OIS_COMPOUND,
    XCCY_CURVE_MODEL_VERSION,
    XCCY_QUOTE_SPEC_ID,
    BusinessCalendar,
    CompoundedIndex,
    CurveBootstrapRequest,
    CurveKind,
    DayCount,
    DiscountCurve,
    FixingHistory,
    FloatingCouponPeriod,
    FloatingCouponRule,
    FloatingLeg,
    FxForward,
    FxForwardLeg,
    FxForwardRiskEngine,
    FxForwardRiskRequest,
    FxFuturesScenario,
    FxSwapPointsQuote,
    IndexFixings,
    InvalidTerms,
    MissingMarketData,
    NotionalExchange,
    OisQuote,
    ShockedCurveCache,
    UnsupportedProduct,
    XccyCurveBootstrapRequest,
    XccyCurveCache,
    XccyLeg,
    XccyParQuote,
    XccyPeriod,
    XccyRiskEngine,
    XccyRiskRequest,
    XccyScenario,
    XccySwap,
    bootstrap_discount_curve,
    bootstrap_xccy_curve,
    from_dict,
    fx_forward_factor_risk,
    get_curve_spec,
    implied_foreign_curve,
    joint_calendar,
    notional_exchanges,
    ois_curve_convention,
    ois_quotes,
    standard_xccy_swap,
    to_dict,
    xccy_basis_factor,
    xccy_factor_risk,
    xccy_leg_periods,
    xccy_par_rate,
    xcs_cnyrub_convention,
)
from rub_fixed_income._risk_common import _central_derivatives, key_rate_weights

C = date(2026, 10, 15)  # Thursday: trade and valuation date
RU_HOLIDAYS = frozenset({date(2026, 11, 4), date(2027, 1, 1), date(2027, 1, 7), date(2028, 1, 3)})
CN_HOLIDAYS = frozenset({date(2027, 2, 8), date(2027, 2, 9), date(2027, 10, 1), date(2027, 10, 4)})
RU = BusinessCalendar(RU_HOLIDAYS, frozenset(), "ru-test", date(2025, 1, 1), date(2034, 12, 31))
CN = BusinessCalendar(CN_HOLIDAYS, frozenset(), "cn-test", date(2025, 1, 1), date(2034, 12, 31))
JOINT = joint_calendar((RU, CN), version="spfi-moscow-beijing-ruonia-test")
CONVENTION = xcs_cnyrub_convention(JOINT)
SPOT_DATE = JOINT.add_business_days(C, 1)
R_RUB = 0.15
R_PROJ = 0.155
R_CNY = 0.03
S = 11.2  # RUB per CNY
RUB = DiscountCurve.flat(C, R_RUB)
RUB_PROJ = DiscountCurve.flat(C, R_PROJ)
CNY = DiscountCurve.flat(C, R_CNY)
N_CNY = 10_000_000.0
FX_NOTIONAL = 11.25
QUOTES = (
    ("1W", 0.0200),
    ("1M", 0.0205),
    ("3M", 0.0210),
    ("6M", 0.0220),
    ("9M", 0.0228),
    ("1Y", 0.0235),
    ("2Y", 0.0255),
    ("3Y", 0.0270),
    ("4Y", 0.0282),
    ("5Y", 0.0290),
)


def df(rate: float, day: date) -> float:
    return math.exp(-rate * (day - C).days / 365.0)


def act_act_isda(start: date, end: date) -> float:
    total = 0.0
    day = start
    while day < end:
        total += 1.0 / (366.0 if isleap(day.year) else 365.0)
        day += timedelta(days=1)
    return total


def bootstrap_request(**changes: object) -> XccyCurveBootstrapRequest:
    base = XccyCurveBootstrapRequest(
        trade_date=C,
        currency="CNY",
        spot_rate=S,
        spot_value_date=SPOT_DATE,
        notional_fx_rate=FX_NOTIONAL,
        quotes=tuple(XccyParQuote(t, r) for t, r in QUOTES),
        convention=CONVENTION,
    )
    return replace(base, **changes)  # type: ignore[arg-type]


def par_swap(tenor: str, rate: float, side: str = "receive") -> XccySwap:
    return standard_xccy_swap(
        f"XCS-{tenor}",
        C,
        tenor,
        CONVENTION,
        currency="CNY",
        foreign_notional=N_CNY,
        notional_fx_rate=FX_NOTIONAL,
        fixed_rate=rate,
        foreign_side=side,  # type: ignore[arg-type]
    )


def request(swap: XccySwap, foreign: object = CNY, **changes: object) -> XccyRiskRequest:
    fields: dict[str, object] = {
        "swap": swap,
        "spot_rate": S,
        "spot_value_date": SPOT_DATE,
        "rub_discount_curve": RUB,
        "foreign_discount_curve": foreign,
        "basis_bp": 0.0,
    }
    fields.update(changes)
    return XccyRiskRequest(**fields)  # type: ignore[arg-type]


# --- Standard swap terms -------------------------------------------------------------


def test_xcs_cnyrub_convention_matches_the_moex_specification() -> None:
    swap = CONVENTION.swap
    assert CONVENTION.spec_id == XCCY_QUOTE_SPEC_ID == "cny_ruonia_xccy_v1"
    assert swap.spot_lag_business_days == 1 and swap.payment_lag_business_days == 1
    assert swap.single_period_max_months == 12 and swap.frequency_months == 12
    assert swap.stub == "short_initial"
    assert swap.fixed_day_count is DayCount.ACT_ACT_ISDA
    assert swap.float_day_count is DayCount.ACT_ACT_ISDA
    assert CONVENTION.rub_index == RUONIA_OIS_COMPOUND
    assert (CONVENTION.initial_exchange_lag_days, CONVENTION.final_exchange_lag_days) == (0, 0)
    assert "cny_ruonia_xccy_v1" in CONVENTION.identifier


def test_registered_spec_matches_the_convention() -> None:
    spec = get_curve_spec(XCCY_QUOTE_SPEC_ID)
    swap = CONVENTION.swap
    assert spec.kind is CurveKind.XCCY_BOOTSTRAP and spec.currency == "CNY"
    assert spec.model_version == XCCY_CURVE_MODEL_VERSION
    assert spec.spot_lag_business_days == swap.spot_lag_business_days
    assert spec.payment_lag_business_days == swap.payment_lag_business_days
    assert spec.fixed_leg_frequency_months == swap.frequency_months
    assert spec.fixed_leg_day_count is swap.fixed_day_count
    assert spec.float_leg_day_count is swap.float_day_count
    assert spec.business_day_convention == swap.business_day_convention.value
    assert spec.stub == swap.stub
    assert spec.calendars == ("SPFI", "MOSCOW", "BEIJING", "RUONIA")
    assert spec.time_day_count is bootstrap_request().curve_convention.time_day_count


def test_standard_swap_schedule_and_exchanges() -> None:
    one_year = par_swap("1Y", 0.02)
    leg = one_year.foreign_leg
    assert len(leg.periods) == 1  # <= 1Y: a single period
    period = leg.periods[0]
    assert period.accrual_start == SPOT_DATE == date(2026, 10, 16)
    assert period.accrual_end == date(2027, 10, 18)  # 16 Oct 2027 is a Saturday: MODFOL
    assert period.payment_date == date(2027, 10, 19)  # +1B
    assert leg.exchanges == (
        NotionalExchange(SPOT_DATE, -N_CNY),
        NotionalExchange(date(2027, 10, 18), N_CNY),
    )
    rub = one_year.rub_leg
    assert rub.side == "pay" and leg.side == "receive"
    assert rub.periods[0].notional == pytest.approx(N_CNY * FX_NOTIONAL)
    assert rub.index == RUONIA_OIS_COMPOUND and rub.fixed_rate is None
    two_year = par_swap("2Y", 0.02).foreign_leg.periods
    assert [p.accrual_end for p in two_year] == [date(2027, 10, 18), date(2028, 10, 16)]
    # An 18M swap: annual periods rolled back from the end, short initial stub.
    stub = par_swap("18M", 0.02).foreign_leg.periods
    assert [(p.accrual_start, p.accrual_end) for p in stub] == [
        (date(2026, 10, 16), date(2027, 4, 16)),
        (date(2027, 4, 16), date(2028, 4, 17)),
    ]
    # A Chinese holiday is not a settlement day of the joint calendar.
    assert not JOINT.is_business_day(date(2027, 10, 1)) and RU.is_business_day(date(2027, 10, 1))


def test_notional_exchanges_from_amortizing_periods() -> None:
    periods = (
        XccyPeriod(date(2027, 1, 4), date(2027, 7, 5), date(2027, 7, 6), 100.0),
        XccyPeriod(date(2027, 7, 5), date(2028, 1, 4), date(2028, 1, 5), 60.0),
        XccyPeriod(date(2028, 1, 4), date(2028, 7, 4), date(2028, 7, 5), 60.0),
    )
    exchanges = notional_exchanges(
        periods, initial_date=date(2027, 1, 4), final_date=date(2028, 7, 4)
    )
    assert exchanges == (
        NotionalExchange(date(2027, 1, 4), -100.0),
        NotionalExchange(date(2027, 7, 6), 40.0),
        NotionalExchange(date(2028, 7, 4), 60.0),
    )
    assert (
        notional_exchanges(periods, initial_date=None, final_date=date(2028, 7, 4))[0].amount == 40
    )
    rising = (periods[1], replace(periods[2], notional=80.0))
    with pytest.raises(InvalidTerms):
        notional_exchanges(rising, initial_date=None, final_date=date(2028, 7, 4))


# --- Bootstrap and par -------------------------------------------------------------


def test_bootstrap_reprices_every_quote_and_the_par_solver_returns_it() -> None:
    req = bootstrap_request()
    curve = bootstrap_xccy_curve(req, rub_discount_curve=RUB)
    assert curve.model_version == XCCY_CURVE_MODEL_VERSION and curve.curve_date == C
    assert len(curve.nodes) == len(QUOTES)
    for tenor, rate in QUOTES:
        for side in ("receive", "pay"):
            swap = par_swap(tenor, rate, side)
            for foreign in (curve, req):
                engine = XccyRiskEngine(request(swap, foreign))
                assert abs(engine.base_pv_rub) < 1e-10 * N_CNY * S
            assert xccy_par_rate(request(swap, curve), leg="foreign") == pytest.approx(
                rate, abs=1e-12
            )
        # The RUONIA spread that sets an off-market swap to par undoes the rate change.
        off = par_swap(tenor, rate + 0.001)
        spread = xccy_par_rate(request(off, curve), leg="rub")
        assert spread > 0.0
        repriced = replace(off, rub_leg=replace(off.rub_leg, spread=spread))
        assert abs(XccyRiskEngine(request(repriced, curve)).base_pv_rub) < 1e-6


def test_bootstrap_with_a_separate_projection_curve_and_an_fx_implied_short_end() -> None:
    curve = bootstrap_xccy_curve(
        bootstrap_request(), rub_discount_curve=RUB, rub_projection_curve=RUB_PROJ
    )
    swap = par_swap("3Y", 0.027)
    engine = XccyRiskEngine(request(swap, curve, rub_projection_curve=RUB_PROJ))
    assert abs(engine.base_pv_rub) < 1e-10 * N_CNY * S
    # Projecting RUONIA higher than discounting makes the CNY leg worth more at par.
    single = bootstrap_xccy_curve(bootstrap_request(), rub_discount_curve=RUB)
    assert curve.discount_factor(date(2029, 10, 17)) > single.discount_factor(date(2029, 10, 17))
    # Hybrid: FX swap points up to 9M, XCCY quotes from 1Y (MOEX CNY-HYB-CNYRUO).
    tom_9m = JOINT.add_business_days(C, 1)
    nine_m = date(2027, 7, 16)
    short = implied_foreign_curve(
        rub_curve=RUB,
        spot_rate=S,
        spot_value_date=SPOT_DATE,
        quotes=(
            FxSwapPointsQuote("TODTOM", C, tom_9m, 0.0043),
            FxSwapPointsQuote("TOM9M", tom_9m, nine_m, 1.05),
        ),
    )
    long_quotes = tuple(XccyParQuote(t, r) for t, r in QUOTES if t.endswith("Y"))
    hybrid_req = bootstrap_request(quotes=long_quotes, short_end=short)
    hybrid = bootstrap_xccy_curve(hybrid_req, rub_discount_curve=RUB)
    assert hybrid.nodes[: len(short.nodes)] == short.nodes
    for tenor, rate in QUOTES[5:]:
        pv = XccyRiskEngine(request(par_swap(tenor, rate), hybrid)).base_pv_rub
        assert abs(pv) < 1e-10 * N_CNY * S
    with pytest.raises(InvalidTerms, match="short end"):
        bootstrap_xccy_curve(bootstrap_request(short_end=short), rub_discount_curve=RUB)


def test_bootstrap_rejections() -> None:
    with pytest.raises(InvalidTerms):
        bootstrap_request(quotes=(XccyParQuote("1Y", 0.02), XccyParQuote("12M", 0.02)))
    with pytest.raises(InvalidTerms):
        bootstrap_request(quotes=())
    with pytest.raises(InvalidTerms):
        XccyParQuote("ON", 0.02)
    with pytest.raises(InvalidTerms):
        bootstrap_request(currency="RUB")
    with pytest.raises(InvalidTerms, match="trade date"):
        bootstrap_xccy_curve(
            bootstrap_request(), rub_discount_curve=DiscountCurve.flat(SPOT_DATE, 0.1)
        )
    with pytest.raises(InvalidTerms, match="spot"):
        request(par_swap("1Y", 0.02), bootstrap_request(spot_rate=S + 0.1))
    with pytest.raises(InvalidTerms, match="currency"):
        XccyRiskRequest(
            replace(
                par_swap("1Y", 0.02),
                foreign_leg=replace(par_swap("1Y", 0.02).foreign_leg, currency="USD"),
            ),
            S,
            SPOT_DATE,
            RUB,
            bootstrap_request(),
            0.0,
        )


# --- Independent cashflow reconstruction of a seasoned swap -----------------------------


SEASONED_START = date(2025, 8, 18)


def ruonia_fixings() -> tuple[tuple[date, float], ...]:
    rows = []
    day = date(2025, 8, 1)
    k = 0
    while day < C:
        if RU.is_business_day(day):
            rows.append((day, 0.16 + 0.0005 * math.sin(k)))
            k += 1
        day += timedelta(days=1)
    return tuple(rows)


HISTORY = FixingHistory("ruonia", C, ruonia_fixings(), RU, "ruonia-snap")


def seasoned_swap() -> XccySwap:
    ends = (date(2026, 8, 18), date(2027, 8, 18), date(2028, 8, 18))
    starts = (SEASONED_START, *ends[:-1])
    n_rub = N_CNY * 12.0

    def periods(notional: float) -> tuple[XccyPeriod, ...]:
        return tuple(
            XccyPeriod(s, e, JOINT.add_business_days(e, 1), notional)
            for s, e in zip(starts, ends, strict=True)
        )

    rub_periods = periods(n_rub)
    cny_periods = periods(N_CNY)
    return XccySwap(
        "SEASONED",
        rub_leg=XccyLeg(
            "RUB",
            "pay",
            DayCount.ACT_ACT_ISDA,
            rub_periods,
            notional_exchanges(rub_periods, initial_date=SEASONED_START, final_date=ends[-1]),
            index=RUONIA_OIS_COMPOUND,
            spread=0.002,
        ),
        foreign_leg=XccyLeg(
            "CNY",
            "receive",
            DayCount.ACT_ACT_ISDA,
            cny_periods,
            notional_exchanges(cny_periods, initial_date=SEASONED_START, final_date=ends[-1]),
            fixed_rate=0.031,
        ),
    )


def seasoned_request(**changes: object) -> XccyRiskRequest:
    base = request(seasoned_swap(), CNY, rub_projection_curve=RUB_PROJ, rub_fixings=HISTORY)
    return replace(base, **changes)  # type: ignore[arg-type]


def independent_ruonia_growth(start: date, end: date) -> float:
    """NFA 8.4(б) by hand: business-day segments, Act = 366 if any day is in a leap year."""
    act = (
        366
        if any(isleap(y) for y in range(start.year, (end - timedelta(days=1)).year + 1))
        else 365
    )
    fixing = dict(HISTORY.fixings)
    growth = 1.0
    day = start
    while day < end:
        nxt = day + timedelta(days=1)
        while nxt < end and not RU.is_business_day(nxt):
            nxt += timedelta(days=1)
        if day < C:
            growth *= 1.0 + fixing[day] * (nxt - day).days / act
        else:  # overnight forward of the projection curve over [day, nxt)
            growth *= df(R_PROJ, day) / df(R_PROJ, nxt)
        day = nxt
    return growth


def test_seasoned_swap_pv_equals_an_independent_reconstruction() -> None:
    engine = XccyRiskEngine(seasoned_request())
    n_rub = N_CNY * 12.0
    s = 0.002
    expected_rub = []
    for start, end in (
        (date(2026, 8, 18), date(2027, 8, 18)),
        (date(2027, 8, 18), date(2028, 8, 18)),
    ):
        days = (end - start).days
        act = 366 if end.year == 2028 else 365
        dcf = act_act_isda(start, end)
        growth = independent_ruonia_growth(start, end)
        coupon = n_rub * ((growth - 1.0) * act / days * dcf + s * dcf)
        pay = JOINT.add_business_days(end, 1)
        expected_rub.append((pay, -coupon))
    expected_rub.append((date(2028, 8, 18), -n_rub))
    expected_cny = [
        (JOINT.add_business_days(e, 1), N_CNY * 0.031 * act_act_isda(st, e))
        for st, e in (
            (date(2026, 8, 18), date(2027, 8, 18)),
            (date(2027, 8, 18), date(2028, 8, 18)),
        )
    ]
    expected_cny.append((date(2028, 8, 18), N_CNY))
    conversion = S * df(R_RUB, SPOT_DATE) / df(R_CNY, SPOT_DATE)
    pv = math.fsum(a * df(R_RUB, d) for d, a in expected_rub) + conversion * math.fsum(
        a * df(R_CNY, d) for d, a in expected_cny
    )
    assert engine.base_pv_rub == pytest.approx(pv, rel=1e-11)
    rows = engine.cashflows()
    rub_rows = sorted((r.payment_date, r.amount) for r in rows if r.leg == "rub")
    for (d1, a1), (d2, a2) in zip(rub_rows, sorted(expected_rub), strict=True):
        assert d1 == d2 and a1 == pytest.approx(a2, rel=1e-12)
    sources = {(r.leg, r.accrual_start): r.source for r in rows if r.kind == "coupon"}
    assert sources[("rub", date(2026, 8, 18))] == "partially_projected"
    assert sources[("rub", date(2027, 8, 18))] == "projected"
    assert sources[("foreign", date(2026, 8, 18))] == "fixed"
    assert math.fsum(r.pv_rub for r in rows) == pytest.approx(engine.base_pv_rub, rel=1e-12)


def test_current_ruonia_coupon_agrees_with_the_floating_module() -> None:
    """A non-leap period on ACT/ACT ISDA: amount = N * (growth - 1 + spread * DCF)."""
    start, end = date(2026, 8, 18), date(2027, 8, 18)
    leg = FloatingLeg(
        FloatingCouponRule(
            "ruonia", "compounded", 0.0, DayCount.ACT_365_FIXED, DayCount.ACT_365_FIXED
        ),
        (FloatingCouponPeriod(start, end, JOINT.add_business_days(end, 1), 1.0),),
        HISTORY,
    )
    (projection,) = leg.project(RUB_PROJ, settlement_date=C)
    row = next(
        r
        for r in XccyRiskEngine(seasoned_request()).cashflows()
        if r.accrual_start == start and r.leg == "rub"
    )
    n_rub = N_CNY * 12.0
    assert -row.amount / n_rub - 0.002 * (end - start).days / 365 == pytest.approx(
        projection.amount_rub, rel=1e-11
    )


def test_known_fixings_and_amounts_do_not_move() -> None:
    engine = XccyRiskEngine(seasoned_request())
    base = {(r.leg, r.payment_date): r.amount for r in engine.cashflows()}
    shocked = replace(seasoned_request(), rub_projection_curve=DiscountCurve.flat(C, 0.25))
    moved = {(r.leg, r.payment_date): r.amount for r in XccyRiskEngine(shocked).cashflows()}
    assert moved[("foreign", date(2027, 8, 19))] == base[("foreign", date(2027, 8, 19))]
    assert moved[("rub", date(2027, 8, 19))] != base[("rub", date(2027, 8, 19))]
    # A published coupon is used as is.
    swap = seasoned_swap()
    periods = list(swap.rub_leg.periods)
    periods[1] = replace(periods[1], known_amount=1_234_567.0)
    known = replace(swap, rub_leg=replace(swap.rub_leg, periods=tuple(periods)))
    rows = XccyRiskEngine(replace(seasoned_request(), swap=known)).cashflows()
    row = next(r for r in rows if r.leg == "rub" and r.accrual_start == date(2026, 8, 18))
    assert row.amount == -1_234_567.0 and row.source == "known"
    # A coupon fixed by history: projection risk is zero.
    ended = XccyPeriod(date(2026, 9, 1), date(2026, 10, 1), date(2026, 10, 16), 1e6)
    fixed_leg = XccyLeg("RUB", "pay", DayCount.ACT_ACT_ISDA, (ended,), index=RUONIA_OIS_COMPOUND)
    cny_leg = XccyLeg(
        "CNY",
        "receive",
        DayCount.ACT_ACT_ISDA,
        (),
        (NotionalExchange(date(2026, 10, 16), 1e5),),
        fixed_rate=0.0,
    )
    req = request(
        XccySwap("K", fixed_leg, cny_leg), CNY, rub_projection_curve=RUB_PROJ, rub_fixings=HISTORY
    )
    risk = XccyRiskEngine(req).risk()
    assert risk.curve("rub_projection").dv01_rub == 0.0
    assert XccyRiskEngine(req).cashflows()[0].source == "fixed_by_history"


def test_missing_fixings_are_rejected() -> None:
    with pytest.raises(MissingMarketData):
        XccyRiskEngine(seasoned_request(rub_fixings=None))
    gap = tuple(row for row in HISTORY.fixings if row[0] != date(2026, 9, 10))
    with pytest.raises(MissingMarketData):
        XccyRiskEngine(seasoned_request(rub_fixings=replace(HISTORY, fixings=gap)))


def test_leap_year_ruonia_annualization_is_366() -> None:
    assert RUONIA_OIS_COMPOUND.annualization(date(2027, 8, 18), date(2028, 8, 18)) == 366 / 366
    assert RUONIA_OIS_COMPOUND.annualization(date(2026, 8, 18), date(2027, 8, 18)) == 365 / 365
    assert RUONIA_OIS_COMPOUND.weight_days(date(2027, 12, 1), date(2028, 1, 1)) == 365
    other = CompoundedIndex("CNY-X", "act_360", "act_365f")
    assert other.weight_days(C, SPOT_DATE) == 360
    assert other.annualization(C, date(2026, 10, 25)) == 365 / 10


# --- Floating foreign leg ----------------------------------------------------------


def test_floating_cny_leg_on_an_explicit_index() -> None:
    index = CompoundedIndex("CNY-RUSFARCNY-OIS-COMPOUND", "act_365f", "act_365f")
    start, end = date(2026, 9, 15), date(2027, 3, 15)
    calendar = CN
    fixings = []
    day = start
    while day < C:
        if calendar.is_business_day(day):
            fixings.append((day, 0.018))
        day += timedelta(days=1)
    history = IndexFixings(index.name, C, tuple(fixings), calendar, "rusfarcny-snap")
    period = XccyPeriod(start, end, date(2027, 3, 16), N_CNY)
    cny_leg = XccyLeg(
        "CNY",
        "receive",
        DayCount.ACT_365_FIXED,
        (period,),
        (NotionalExchange(end, N_CNY),),
        index=index,
        spread=0.001,
    )
    rub_period = XccyPeriod(start, end, date(2027, 3, 16), N_CNY * 11.0)
    rub_leg = XccyLeg(
        "RUB",
        "pay",
        DayCount.ACT_365_FIXED,
        (rub_period,),
        (NotionalExchange(end, N_CNY * 11.0),),
        fixed_rate=0.16,
    )
    cny_proj = DiscountCurve.flat(C, 0.02)
    req = request(
        XccySwap("CNYFLT", rub_leg, cny_leg),
        CNY,
        foreign_projection_curve=cny_proj,
        foreign_fixings=history,
    )
    engine = XccyRiskEngine(req)
    assert engine.roles == ("rub_discount", "foreign_discount", "foreign_projection")
    # Independent: realized growth by business-day segments, projection as a DF ratio.
    growth = 1.0
    day = start
    while day < C:
        obs = day
        while not calendar.is_business_day(obs):
            obs -= timedelta(days=1)
        nxt = calendar.add_business_days(obs, 1)
        growth *= 1.0 + 0.018 * (nxt - day).days / 365
        day = nxt
    growth *= math.exp(0.02 * (end - day).days / 365)
    days = (end - start).days
    coupon = N_CNY * ((growth - 1.0) + 0.001 * days / 365)
    row = next(r for r in engine.cashflows() if r.leg == "foreign" and r.kind == "coupon")
    assert row.amount == pytest.approx(coupon, rel=1e-12)
    assert row.source == "partially_projected"
    risk = engine.risk()
    assert risk.curve("foreign_projection").dv01_rub > 0.0  # receive the floating coupon
    with pytest.raises(MissingMarketData):
        replace(req, foreign_projection_curve=None)
    with pytest.raises(MissingMarketData):
        XccyRiskEngine(replace(req, foreign_fixings=replace(history, fixings=history.fixings[:-3])))
    with pytest.raises(InvalidTerms):
        replace(req, foreign_fixings=replace(history, index="OTHER"))


# --- Consistency with FX forwards -------------------------------------------------


def final_exchange_only(maturity: date, notional: float, strike: float) -> XccySwap:
    return XccySwap(
        "ZC",
        rub_leg=XccyLeg(
            "RUB",
            "pay",
            DayCount.ACT_365_FIXED,
            (),
            (NotionalExchange(maturity, notional * strike),),
            fixed_rate=0.0,
        ),
        foreign_leg=XccyLeg(
            "CNY",
            "receive",
            DayCount.ACT_365_FIXED,
            (),
            (NotionalExchange(maturity, notional),),
            fixed_rate=0.0,
        ),
    )


@pytest.mark.parametrize("basis_bp", [0.0, -35.0])
def test_zero_coupon_xccy_is_an_fx_forward(basis_bp: float) -> None:
    maturity = date(2028, 4, 18)
    strike = 12.4
    swap = final_exchange_only(maturity, N_CNY, strike)
    rub_ois = CurveBootstrapRequest(
        C, (OisQuote("1Y", date(2027, 10, 15), 0.14), OisQuote("3Y", date(2029, 10, 15), 0.13))
    )
    xccy = XccyRiskEngine(request(swap, CNY, basis_bp=basis_bp, rub_discount_curve=rub_ois))
    forward = FxForwardRiskEngine(
        FxForwardRiskRequest(
            FxForward("ZC", "CNY", 1.0, FxForwardLeg(maturity, N_CNY, strike)),
            S,
            SPOT_DATE,
            rub_ois,
            CNY,
            basis_bp,
        )
    )
    assert xccy.base_pv_rub == pytest.approx(forward.base_pv_rub, rel=1e-12)
    a, b = xccy.risk(), forward.risk(difference="central")
    assert a.fx_delta_rub_per_pct == pytest.approx(b.fx_delta_rub_per_pct, rel=1e-12)
    assert a.basis01_rub == pytest.approx(b.basis01_rub, rel=1e-9)
    for mine, theirs in zip(a.curve("rub_discount").buckets, b.rub_buckets, strict=True):
        assert mine.dv01_rub == pytest.approx(theirs.dv01_rub, rel=1e-7, abs=1e-6)
    for mine, theirs in zip(a.curve("foreign_discount").buckets, b.foreign_buckets, strict=True):
        assert mine.dv01_rub == pytest.approx(theirs.dv01_rub, rel=1e-7, abs=1e-6)
    so_x, so_f = xccy.second_order(), forward.second_order()
    for g1, g2 in zip(so_x.gradient_rub, so_f.gradient_rub, strict=True):
        assert g1 == pytest.approx(g2, rel=1e-7, abs=1e-6)
    scale = max(abs(v) for row in so_f.hessian_rub for v in row)
    for r1, r2 in zip(so_x.hessian_rub, so_f.hessian_rub, strict=True):
        for v1, v2 in zip(r1, r2, strict=True):
            assert abs(v1 - v2) <= 1e-6 * scale
    desk_x = xccy_factor_risk(a, {"rub_discount": "RUB_OIS", "foreign_discount": "CNY_XCCY"})
    desk_f = fx_forward_factor_risk(b, "RUB_OIS", "CNY_XCCY")
    assert desk_x.factors[:22] == desk_f.factors[:22]
    assert desk_x.factors[22:] == ("XCCYBASIS|CNY", "FX|CNY")


# --- Risk ----------------------------------------------------------------------------


def off_market() -> XccySwap:
    return par_swap("3Y", 0.035)


def test_fx_delta_identity_and_exact_spot_scenario() -> None:
    engine = XccyRiskEngine(
        request(seasoned_swap(), CNY, rub_projection_curve=RUB_PROJ, rub_fixings=HISTORY)
    )
    risk = engine.risk()
    assert risk.fx_delta_rub_per_pct == risk.foreign_leg_pv_rub / 100.0
    assert risk.foreign_leg_pv_rub == pytest.approx(risk.foreign_leg_pv * risk.fx_today, rel=1e-14)
    assert risk.pv_rub == pytest.approx(risk.foreign_leg_pv_rub + risk.rub_leg_pv_rub, rel=1e-14)
    up = engine.reprice(XccyScenario(fx_spot_shift_pct=1.0))
    assert up.total_pnl_rub == pytest.approx(risk.fx_delta_rub_per_pct, rel=1e-10)
    assert up.fx_spot_pnl_rub == up.total_pnl_rub and up.interaction_pnl_rub == pytest.approx(
        0.0, abs=1e-6
    )


def test_signs_and_scaling_by_side_and_notional() -> None:
    receive = XccyRiskEngine(request(off_market())).risk()
    assert receive.pv_rub > 0.0  # receive CNY 3.5% when par is about 2.7%
    assert receive.curve("foreign_discount").dv01_rub < 0.0  # long a CNY fixed bond
    assert receive.fx_delta_rub_per_pct > 0.0
    pay = XccyRiskEngine(request(par_swap("3Y", 0.035, "pay"))).risk()
    assert pay.pv_rub == pytest.approx(-receive.pv_rub, rel=1e-12)
    for c1, c2 in zip(receive.curves, pay.curves, strict=True):
        assert c2.dv01_rub == pytest.approx(-c1.dv01_rub, rel=1e-9)
    double = standard_xccy_swap(
        "D",
        C,
        "3Y",
        CONVENTION,
        currency="CNY",
        foreign_notional=2 * N_CNY,
        notional_fx_rate=FX_NOTIONAL,
        fixed_rate=0.035,
        foreign_side="receive",
    )
    doubled = XccyRiskEngine(request(double)).risk()
    assert doubled.pv_rub == pytest.approx(2 * receive.pv_rub, rel=1e-12)
    assert doubled.basis01_rub == pytest.approx(2 * receive.basis01_rub, rel=1e-9)
    assert receive.basis01_rub > 0.0  # a higher basis raises the CNY flows' RUB value


def test_bucket_sums_and_parallel_shifts() -> None:
    risk = XccyRiskEngine(
        request(seasoned_swap(), CNY, rub_projection_curve=RUB_PROJ, rub_fixings=HISTORY)
    ).risk()
    assert [c.role for c in risk.curves] == ["rub_discount", "rub_projection", "foreign_discount"]
    for curve in risk.curves:
        assert curve.dv01_rub == pytest.approx(
            math.fsum(b.dv01_rub for b in curve.buckets), rel=1e-14
        )
        assert curve.dv01_rub == pytest.approx(curve.parallel_dv01_rub, rel=1e-5)
    assert risk.dv01_rub == pytest.approx(math.fsum(c.dv01_rub for c in risk.curves), rel=1e-14)
    # Paying RUONIA on a separate projection curve: higher forwards cost.
    assert risk.curve("rub_projection").dv01_rub < 0.0


def analytic_cny_bucket(engine: XccyRiskEngine, bucket: int) -> float:
    """d PV / d (1 bp at one CNY bucket) on flat curves, by hand."""
    fx_today = S * df(R_RUB, SPOT_DATE) / df(R_CNY, SPOT_DATE)
    total = 0.0
    foreign = 0.0
    for row in engine.cashflows():
        if row.leg != "foreign":
            continue
        t = (row.payment_date - C).days / 365.0
        value = fx_today * row.amount * df(R_CNY, row.payment_date)
        foreign += value
        total -= value * key_rate_weights(t)[bucket] * t * 1e-4
    t_s = (SPOT_DATE - C).days / 365.0
    return total + foreign * key_rate_weights(t_s)[bucket] * t_s * 1e-4


def test_bump_convergence_is_second_order() -> None:
    engine = XccyRiskEngine(request(off_market()))
    k = RISK_BUCKETS.index("3Y")
    exact = analytic_cny_bucket(engine, k)
    errors = [
        abs(engine.risk(bump_bp=h).curve("foreign_discount").buckets[k].dv01_rub - exact)
        for h in (40.0, 20.0, 10.0)
    ]
    assert errors[0] / errors[1] == pytest.approx(4.0, rel=0.02)
    assert errors[1] / errors[2] == pytest.approx(4.0, rel=0.05)
    assert engine.risk().curve("foreign_discount").buckets[k].dv01_rub == pytest.approx(
        exact, rel=1e-6
    )
    # Forward difference is the exact +1bp PV change.
    forward = engine.risk(difference="forward").curve("foreign_discount").buckets[k].dv01_rub
    shifted = tuple(1.0 if i == k else 0.0 for i in range(len(RISK_BUCKETS)))
    assert forward == pytest.approx(
        engine.reprice(XccyScenario(foreign_shifts_bp=shifted)).total_pnl_rub, rel=1e-12
    )


def test_hessian_matches_the_full_central_scheme_with_exact_fx_terms() -> None:
    engine = XccyRiskEngine(
        request(seasoned_swap(), CNY, rub_projection_curve=RUB_PROJ, rub_fixings=HISTORY)
    )
    so = engine.second_order()
    size = len(RISK_BUCKETS)
    count = len(engine.roles)
    rates = size * count + 1
    assert len(so.factors) == rates + 1

    def price(vector: list[float]) -> float:
        scenario = XccyScenario(
            rub_shifts_bp=tuple(vector[:size]),
            rub_projection_shifts_bp=tuple(vector[size : 2 * size]),
            foreign_shifts_bp=tuple(vector[2 * size : 3 * size]),
            basis_shift_bp=vector[-1],
        )
        return engine.reprice(scenario).shocked_pv_rub

    gradient, hessian = _central_derivatives(price, rates, 1.0, engine.base_pv_rub, 1.0)
    assert so.gradient_rub[:rates] == gradient
    for i in range(rates):
        assert so.hessian_rub[i][:rates] == hessian[i]
    risk = engine.risk()
    assert so.gradient_rub[-1] == risk.fx_delta_rub_per_pct
    assert so.hessian_rub[-1][-1] == 0.0
    # FX x rates: 1 % of the foreign leg's central rate derivative.
    k = 2 * size + RISK_BUCKETS.index("2Y")
    assert so.hessian_rub[k][-1] == pytest.approx(
        risk.curve("foreign_discount").buckets[RISK_BUCKETS.index("2Y")].dv01_rub / 100.0, rel=1e-3
    )
    # A Taylor expansion reproduces a small joint move.
    move = [0.0] * (rates + 1)
    move[RISK_BUCKETS.index("1Y")] = 3.0
    move[2 * size + RISK_BUCKETS.index("2Y")] = -2.0
    move[-1] = 0.5  # +0.5 % spot
    taylor = sum(g * m for g, m in zip(so.gradient_rub, move, strict=True)) + 0.5 * sum(
        so.hessian_rub[i][j] * move[i] * move[j] for i in range(rates + 1) for j in range(rates + 1)
    )
    exact = engine.reprice(
        XccyScenario(
            rub_shifts_bp=tuple(move[:size]),
            rub_projection_shifts_bp=tuple(move[size : 2 * size]),
            foreign_shifts_bp=tuple(move[2 * size : 3 * size]),
            fx_spot_shift_pct=0.5,
        )
    ).total_pnl_rub
    assert taylor == pytest.approx(exact, rel=1e-5)
    desk = xccy_factor_risk(
        risk,
        {"rub_discount": "RUB_OIS", "rub_projection": "RUONIA_FWD", "foreign_discount": "CNY_XCCY"},
        so,
    )
    assert desk.has_second_order and desk.factors[-2:] == ("XCCYBASIS|CNY", "FX|CNY")
    entries = desk.hessian_by_factor()
    assert ("IR|CNY_XCCY|2Y", "FX|CNY") in entries
    # RUB rates reach the CNY leg only through DF_rub(spot): the 1M bucket.
    assert ("IR|RUB_OIS|1M", "IR|CNY_XCCY|2Y") in entries
    assert ("IR|RUB_OIS|1Y", "IR|CNY_XCCY|2Y") not in entries


def test_quote_basis_risk_on_a_bootstrapped_curve() -> None:
    req = bootstrap_request()
    cache = XccyCurveCache()
    par = XccyRiskEngine(request(par_swap("3Y", 0.027), req), xccy_cache=cache).risk()
    # Quotes held: a quoted par swap stays at par under RUB shocks, so its RUB risk is zero,
    # and its quote risk is that of one quote: the 3Y bucket.
    assert par.curve("foreign_discount").curve_basis == "xccy_quote"
    assert abs(par.curve("rub_discount").dv01_rub) < 1e-6
    buckets = {b.bucket: b.dv01_rub for b in par.curve("foreign_discount").buckets}
    assert buckets["3Y"] < 0.0
    assert all(abs(v) < 1e-6 for k, v in buckets.items() if k not in ("2Y", "3Y", "5Y"))
    # Against the plain curve, the off-market swap keeps the CNY risk and gains RUB risk.
    off = XccyRiskEngine(request(off_market(), req), xccy_cache=cache).risk()
    assert off.curve("rub_discount").dv01_rub != 0.0
    hits = cache.hits
    again = XccyRiskEngine(request(off_market(), req), xccy_cache=cache).risk()
    assert cache.hits > hits and again == off
    fresh = XccyRiskEngine(request(off_market(), req)).risk()
    assert fresh == off


def test_scenarios_decompose_exactly() -> None:
    engine = XccyRiskEngine(
        request(seasoned_swap(), CNY, rub_projection_curve=RUB_PROJ, rub_fixings=HISTORY)
    )
    scenario = XccyScenario.parallel(
        rub_shift_bp=150.0, foreign_shift_bp=-40.0, basis_shift_bp=10.0, fx_spot_shift_pct=-12.0
    )
    result = engine.reprice(scenario)
    parts = (
        result.rub_rates_pnl_rub
        + result.foreign_rates_pnl_rub
        + result.basis_pnl_rub
        + result.fx_spot_pnl_rub
    )
    assert result.total_pnl_rub == pytest.approx(parts + result.interaction_pnl_rub, rel=1e-12)
    via_fx = engine.reprice(
        XccyScenario.from_fx_scenario(
            FxFuturesScenario.parallel(
                rub_shift_bp=150.0,
                foreign_shift_bp=-40.0,
                basis_shift_bp=10.0,
                fx_spot_shift_pct=-12.0,
            )
        )
    )
    assert via_fx.total_pnl_rub == result.total_pnl_rub
    only_discount = engine.reprice(
        XccyScenario(rub_shifts_bp=(50.0,) * 11, rub_projection_shifts_bp=(0.0,) * 11)
    )
    assert only_discount.total_pnl_rub != result.rub_rates_pnl_rub
    with pytest.raises(InvalidTerms):
        XccyRiskEngine(request(off_market())).reprice(
            XccyScenario(rub_projection_shifts_bp=(1.0,) * 11)
        )


def test_ois_quote_rub_curve_and_cache_reuse() -> None:
    rub_ois = CurveBootstrapRequest(
        C,
        tuple(
            OisQuote(t, d, r)
            for t, d, r in (
                ("6M", date(2027, 4, 15), 0.145),
                ("1Y", date(2027, 10, 15), 0.14),
                ("3Y", date(2029, 10, 15), 0.13),
                ("5Y", date(2031, 10, 15), 0.125),
            )
        ),
    )
    cache = ShockedCurveCache()
    swap = off_market()
    a = XccyRiskEngine(request(swap, CNY, rub_discount_curve=rub_ois), cache).risk()
    assert a.curve("rub_discount").curve_basis == "ois_quote"
    misses = cache.misses
    b = XccyRiskEngine(request(swap, CNY, rub_discount_curve=rub_ois), cache).risk()
    assert cache.misses == misses and a == b
    plain = XccyRiskEngine(
        request(swap, CNY, rub_discount_curve=bootstrap_discount_curve(rub_ois))
    ).risk()
    assert a.pv_rub == pytest.approx(plain.pv_rub, rel=1e-12)


# --- Factors, identity, rejections ----------------------------------------------


def test_factor_mapping_and_identity() -> None:
    engine = XccyRiskEngine(request(off_market()))
    risk = engine.risk()
    desk = xccy_factor_risk(risk, {"rub_discount": "RUB_OIS", "foreign_discount": "CNY_XCCY"})
    assert len(desk.factors) == 24 and desk.factors[0] == "IR|RUB_OIS|1M"
    assert desk.first_order_by_factor()["FX|CNY"] == risk.fx_delta_rub_per_pct
    assert desk.first_order_by_factor()["XCCYBASIS|CNY"] == risk.basis01_rub
    assert xccy_basis_factor("CNY") == "XCCYBASIS|CNY"
    with pytest.raises(InvalidTerms):
        xccy_factor_risk(risk, {"rub_discount": "X", "foreign_discount": "X"})
    with pytest.raises(InvalidTerms):
        xccy_factor_risk(risk, {"rub_discount": "X"})
    with pytest.raises(InvalidTerms):
        xccy_factor_risk(
            engine.risk(difference="forward"), {"rub_discount": "A", "foreign_discount": "B"}
        )
    other = XccyRiskEngine(request(par_swap("3Y", 0.036)))
    with pytest.raises(InvalidTerms):
        xccy_factor_risk(risk, {"rub_discount": "A", "foreign_discount": "B"}, other.second_order())
    assert XccyRiskEngine(request(off_market())).base_id == engine.base_id
    assert other.base_id != engine.base_id
    assert XccyRiskEngine(request(off_market(), basis_bp=1.0)).base_id != engine.base_id


def test_serialization_round_trip() -> None:
    swap = seasoned_swap()
    assert from_dict(XccySwap, to_dict(swap)) == swap


def test_typed_rejections() -> None:
    swap = off_market()
    with pytest.raises(UnsupportedProduct, match="mark-to-market"):
        replace(swap, mark_to_market_resets=True)
    with pytest.raises(UnsupportedProduct):
        replace(
            swap,
            rub_leg=replace(
                swap.rub_leg, index=CompoundedIndex("KEYRATE-AVERAGE", "act_365f", "act_365f")
            ),
        )
    with pytest.raises(InvalidTerms):
        replace(swap, rub_leg=replace(swap.rub_leg, side="receive"))
    with pytest.raises(InvalidTerms):
        replace(swap, rub_leg=swap.foreign_leg, foreign_leg=swap.rub_leg)
    with pytest.raises(InvalidTerms):
        replace(swap.foreign_leg, spread=0.001)
    with pytest.raises(InvalidTerms):
        replace(swap.foreign_leg, index=RUONIA_OIS_COMPOUND)
    with pytest.raises(InvalidTerms):
        XccyPeriod(C, C, C, 1.0)
    with pytest.raises(InvalidTerms):
        NotionalExchange(C, 0.0)
    with pytest.raises(InvalidTerms):
        request(swap, rub_projection_curve=None, foreign_projection_curve=CNY)
    with pytest.raises(InvalidTerms):
        request(swap, CNY, spot_value_date=C - timedelta(days=1))
    with pytest.raises(InvalidTerms):
        request(swap, DiscountCurve.flat(SPOT_DATE, 0.03))
    with pytest.raises(InvalidTerms):
        request(
            final_exchange_only(date(2027, 1, 18), 1.0, 12.0), CNY, rub_projection_curve=RUB_PROJ
        )
    with pytest.raises(InvalidTerms):
        standard_xccy_swap(
            "x",
            C,
            "ON",
            CONVENTION,
            currency="CNY",
            foreign_notional=1.0,
            notional_fx_rate=1.0,
            fixed_rate=0.02,
            foreign_side="receive",
        )
    with pytest.raises(InvalidTerms):
        xccy_leg_periods(CONVENTION.swap, C, C, C, 1.0)
    # Flows paid on or before the valuation date have settled and are left out.
    settled = final_exchange_only(C, 1.0, 12.0)
    with pytest.raises(InvalidTerms, match="settled"):
        XccyRiskEngine(request(settled))


def test_bootstrap_on_a_moex_v2_ruonia_ois_curve() -> None:
    """The XCCY curve on the exchange RUONIA OIS spec, with the quote basis on both curves."""
    convention = ois_curve_convention("rub_ruonia_ois_moex_v2", RU)
    rates = (("1M", 0.15), ("3M", 0.148), ("6M", 0.145), ("1Y", 0.14), ("2Y", 0.135), ("5Y", 0.13))
    rub_ois = CurveBootstrapRequest(C, ois_quotes(C, rates, RU), convention)
    rub_curve = bootstrap_discount_curve(rub_ois)
    req = bootstrap_request()
    curve = bootstrap_xccy_curve(req, rub_discount_curve=rub_curve)
    for tenor, rate in QUOTES:
        swap = par_swap(tenor, rate)
        on_curve = XccyRiskEngine(request(swap, curve, rub_discount_curve=rub_curve))
        on_quotes = XccyRiskEngine(request(swap, req, rub_discount_curve=rub_ois))
        assert abs(on_curve.base_pv_rub) < 1e-10 * N_CNY * S
        assert on_quotes.base_pv_rub == pytest.approx(on_curve.base_pv_rub, abs=1e-6)
    risk = XccyRiskEngine(request(off_market(), req, rub_discount_curve=rub_ois)).risk()
    assert risk.curve("rub_discount").curve_basis == "ois_quote"
    assert risk.curve("foreign_discount").curve_basis == "xccy_quote"
