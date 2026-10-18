"""Cross-currency swap carry, rolldown and theta to a horizon (BACKLOG #101)."""

from __future__ import annotations

import math
from calendar import isleap
from dataclasses import replace
from datetime import date, timedelta
from itertools import pairwise
from typing import get_args, get_type_hints

import pytest

from rub_fixed_income import (
    FX_HORIZONS,
    RUONIA_OIS_COMPOUND,
    XCCY_HORIZON_MODEL_VERSION,
    BusinessCalendar,
    DayCount,
    DiscountCurve,
    FixingHistory,
    FxHorizon,
    InvalidTerms,
    XccyCurveBootstrapRequest,
    XccyHorizonValuation,
    XccyLeg,
    XccyParQuote,
    XccyPeriod,
    XccyRiskEngine,
    XccyRiskRequest,
    XccySwap,
    bootstrap_xccy_curve,
    from_dict,
    joint_calendar,
    notional_exchanges,
    roll_curve,
    standard_xccy_swap,
    to_dict,
    xccy_horizon,
    xcs_cnyrub_convention,
)

C = date(2026, 10, 15)  # Thursday: trade and valuation date
RU_HOLIDAYS = frozenset({date(2026, 11, 4), date(2027, 1, 1), date(2027, 1, 7), date(2028, 1, 3)})
CN_HOLIDAYS = frozenset({date(2027, 2, 8), date(2027, 2, 9), date(2027, 10, 1), date(2027, 10, 4)})
RU = BusinessCalendar(RU_HOLIDAYS, frozenset(), "ru-test", date(2025, 1, 1), date(2034, 12, 31))
CN = BusinessCalendar(CN_HOLIDAYS, frozenset(), "cn-test", date(2025, 1, 1), date(2034, 12, 31))
JOINT = joint_calendar((RU, CN), version="spfi-moscow-beijing-ruonia-test")
CONVENTION = xcs_cnyrub_convention(JOINT)
SPOT_DATE = JOINT.add_business_days(C, 1)
R_RUB = 0.15
R_CNY = 0.03
S = 11.2  # RUB per CNY
RUB = DiscountCurve.flat(C, R_RUB)
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


def df(rate: float, day: date, start: date = C) -> float:
    return math.exp(-rate * (day - start).days / 365.0)


def node_curve(rates: tuple[tuple[int, float], ...]) -> DiscountCurve:
    """A log-DF node curve from (days, continuous zero rate) pairs."""
    return DiscountCurve(
        C, tuple((C + timedelta(days=d), math.exp(-r * d / 365.0)) for d, r in rates)
    )


RUB_SLOPED = node_curve(((91, 0.17), (365, 0.155), (730, 0.14), (1095, 0.13), (1825, 0.125)))
RUB_PROJ_SLOPED = node_curve(((91, 0.175), (365, 0.16), (730, 0.145), (1095, 0.135)))
CNY_SLOPED = node_curve(((91, 0.018), (365, 0.022), (730, 0.026), (1095, 0.029), (1825, 0.032)))


def act_act_isda(start: date, end: date) -> float:
    total = 0.0
    day = start
    while day < end:
        total += 1.0 / (366.0 if isleap(day.year) else 365.0)
        day += timedelta(days=1)
    return total


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


def par_swap(tenor: str, rate: float) -> XccySwap:
    return standard_xccy_swap(
        f"XCS-{tenor}",
        C,
        tenor,
        CONVENTION,
        currency="CNY",
        foreign_notional=N_CNY,
        notional_fx_rate=FX_NOTIONAL,
        fixed_rate=rate,
        foreign_side="receive",
    )


# A fixed/fixed swap: receive CNY 3 %, pay RUB 16 %, ACT/365F, two semiannual periods.
P1 = (SPOT_DATE, date(2027, 4, 16), date(2027, 4, 19))
P2 = (date(2027, 4, 16), date(2027, 10, 18), date(2027, 10, 19))
FINAL = date(2027, 10, 18)
N_RUB = N_CNY * FX_NOTIONAL


def fixed_fixed() -> XccySwap:
    def leg(currency: str, side: str, notional: float, rate: float) -> XccyLeg:
        periods = tuple(XccyPeriod(s, e, p, notional) for s, e, p in (P1, P2))
        return XccyLeg(
            currency,
            side,  # type: ignore[arg-type]
            DayCount.ACT_365_FIXED,
            periods,
            notional_exchanges(periods, initial_date=SPOT_DATE, final_date=FINAL),
            fixed_rate=rate,
        )

    return XccySwap("FF", leg("RUB", "pay", N_RUB, 0.16), leg("CNY", "receive", N_CNY, 0.03))


def fixed_fixed_flows() -> tuple[list[tuple[date, float]], list[tuple[date, float]]]:
    """(RUB flows, CNY flows), signed for the position, by hand."""
    rub = [(SPOT_DATE, N_RUB), (FINAL, -N_RUB)]
    cny = [(SPOT_DATE, -N_CNY), (FINAL, N_CNY)]
    for s, e, p in (P1, P2):
        alpha = (e - s).days / 365.0
        rub.append((p, -N_RUB * 0.16 * alpha))
        cny.append((p, N_CNY * 0.03 * alpha))
    return rub, cny


def assert_identities(v: XccyHorizonValuation) -> None:
    assert v.carry_rub + v.rolldown_rub == pytest.approx(
        v.horizon_pnl_rub,
        rel=1e-12,
        abs=1e-6,
    )
    assert v.carry_rub == pytest.approx(
        v.pv_forward_rub + v.received_rub - v.pv_start_rub,
        abs=1e-6,
    )


# --- Hand-computed fixed/fixed swap on flat curves ---------------------------------------


@pytest.mark.parametrize("fx_rule", ["spot_held", "forwards_realised"])
def test_fixed_fixed_swap_against_a_hand_computation(fx_rule: FxHorizon) -> None:
    b = -35.0
    h = date(2027, 5, 14)
    s_h = JOINT.add_business_days(h, 1)
    req = request(fixed_fixed(), basis_bp=b)
    engine = XccyRiskEngine(req)
    v = engine.horizon(h, horizon_spot_value_date=s_h, fx_horizon=fx_rule)
    rub, cny = fixed_fixed_flows()

    def outright(day: date) -> float:
        return (
            S
            * (df(R_CNY, day) / df(R_CNY, SPOT_DATE))
            / (df(R_RUB, day) / df(R_RUB, SPOT_DATE))
            * math.exp(b * 1e-4 * (day - SPOT_DATE).days / 365.0)
        )

    spot_h = S if fx_rule == "spot_held" else outright(s_h)
    received = math.fsum(a for d, a in rub if d <= h) + math.fsum(
        a * (S if fx_rule == "spot_held" else outright(d)) for d, a in cny if d <= h
    )
    # Flat curves: read forward and rolled are the same curve dated h.
    remaining = math.fsum(a * df(R_RUB, d, h) for d, a in rub if d > h) + spot_h * df(
        R_RUB, s_h, h
    ) * math.fsum(
        a * df(R_CNY, d, s_h) * math.exp(b * 1e-4 * (d - s_h).days / 365.0) for d, a in cny if d > h
    )
    start = math.fsum(a * df(R_RUB, d) for d, a in rub) + S * df(R_RUB, SPOT_DATE) / df(
        R_CNY, SPOT_DATE
    ) * math.fsum(
        a * df(R_CNY, d) * math.exp(b * 1e-4 * (d - SPOT_DATE).days / 365.0) for d, a in cny
    )
    assert v.pv_start_rub == pytest.approx(start, rel=1e-12)
    assert v.horizon_spot_rate == pytest.approx(spot_h, rel=1e-14)
    assert v.received_rub == pytest.approx(received, rel=1e-12)
    assert v.pv_forward_rub == pytest.approx(remaining, rel=1e-12)
    assert v.pv_rolled_rub == pytest.approx(remaining, rel=1e-12)
    assert abs(v.rolldown_rub) < 1e-12 * N_RUB  # flat curves: no rolldown
    assert_identities(v)
    assert (v.base_id, v.trade_id, v.currency) == (engine.base_id, "FF", "CNY")
    assert (v.valuation_date, v.horizon_date, v.fx_horizon) == (C, h, fx_rule)
    assert (v.spot_rate, v.spot_value_date, v.horizon_spot_value_date) == (S, SPOT_DATE, s_h)
    assert v.model_version == XCCY_HORIZON_MODEL_VERSION == "rub-xccy-horizon-v1"
    via_function = xccy_horizon(
        req,
        horizon_date=h,
        horizon_spot_value_date=s_h,
        fx_horizon=fx_rule,
    )
    assert via_function == v


def test_flat_curves_have_no_rolldown_on_a_floating_rub_leg() -> None:
    engine = XccyRiskEngine(request(par_swap("3Y", 0.03), rub_projection_curve=RUB))
    h = date(2027, 3, 15)
    for rule in FX_HORIZONS:
        v = engine.horizon(
            h, horizon_spot_value_date=JOINT.add_business_days(h, 1), fx_horizon=rule
        )
        assert abs(v.rolldown_rub) < 1e-9 * N_RUB
        assert_identities(v)


# --- Forwards realised: pv_forward is today's PV carried at the RUB rate ------------------


def sloped_request(**changes: object) -> XccyRiskRequest:
    return request(
        par_swap("3Y", 0.035),
        CNY_SLOPED,
        rub_discount_curve=RUB_SLOPED,
        rub_projection_curve=RUB_PROJ_SLOPED,
        basis_bp=-20.0,
        **changes,
    )


@pytest.mark.parametrize("h", [date(2027, 1, 15), date(2027, 10, 20), date(2028, 6, 1)])
def test_forwards_realised_forward_pv_is_todays_pv_over_the_rub_discount_factor(h: date) -> None:
    engine = XccyRiskEngine(sloped_request())
    s_h = JOINT.add_business_days(h, 1)
    v = engine.horizon(h, horizon_spot_value_date=s_h, fx_horizon="forwards_realised")
    later = [r.pv_rub for r in engine.cashflows() if r.payment_date > h]
    expected = math.fsum(later) / RUB_SLOPED.discount_factor(h)
    gross = math.fsum(abs(p) for p in later) / RUB_SLOPED.discount_factor(h)
    assert abs(v.pv_forward_rub - expected) <= 1e-12 * gross
    assert v.pv_forward_rub == pytest.approx(expected, rel=1e-11)
    assert abs(v.rolldown_rub) > 1.0  # sloped curves roll down
    assert_identities(v)


# --- Spot rule: only the spot of the remaining and received foreign flows differs ---------


def test_spot_held_and_forwards_realised_differ_by_the_spot_effect() -> None:
    req = sloped_request()
    h = date(2027, 1, 15)
    s_h = JOINT.add_business_days(h, 1)
    held = XccyRiskEngine(req).horizon(h, horizon_spot_value_date=s_h)
    real = XccyRiskEngine(req).horizon(
        h, horizon_spot_value_date=s_h, fx_horizon="forwards_realised"
    )
    assert held.fx_horizon == "spot_held" and held.horizon_spot_rate == S
    assert held.pv_start_rub == real.pv_start_rub
    # The horizon values are affine in the horizon spot: the foreign part scales with it.
    doubled = XccyRiskEngine(replace(req, spot_rate=2.0 * S)).horizon(
        h, horizon_spot_value_date=s_h
    )
    for name in ("pv_forward_rub", "pv_rolled_rub"):
        unit = (getattr(doubled, name) - getattr(held, name)) / S  # foreign PV per spot unit
        expected = getattr(held, name) + (real.horizon_spot_rate - S) * unit
        assert getattr(real, name) == pytest.approx(expected, rel=1e-10)
    # Received foreign flows (the initial exchange on the spot date) convert at outrights.
    engine = XccyRiskEngine(req)
    rows = [r for r in engine.cashflows() if r.leg == "foreign" and r.payment_date <= h]
    assert rows
    outright_gain = math.fsum(
        r.amount
        * (
            S
            * CNY_SLOPED.discount_factor(r.payment_date)
            / CNY_SLOPED.discount_factor(SPOT_DATE)
            / (RUB_SLOPED.discount_factor(r.payment_date) / RUB_SLOPED.discount_factor(SPOT_DATE))
            * math.exp(-20e-4 * (r.payment_date - SPOT_DATE).days / 365.0)
            - S
        )
        for r in rows
    )
    assert real.received_rub - held.received_rub == pytest.approx(outright_gain, abs=1e-6)
    expected_spot = (
        S
        * CNY_SLOPED.discount_factor(s_h)
        / CNY_SLOPED.discount_factor(SPOT_DATE)
        / (RUB_SLOPED.discount_factor(s_h) / RUB_SLOPED.discount_factor(SPOT_DATE))
        * math.exp(-20e-4 * (s_h - SPOT_DATE).days / 365.0)
    )
    assert real.horizon_spot_rate == pytest.approx(expected_spot, rel=1e-14)


# --- Past maturity ----------------------------------------------------------------------


@pytest.mark.parametrize("fx_rule", ["spot_held", "forwards_realised"])
def test_horizon_past_maturity_receives_every_flow(fx_rule: FxHorizon) -> None:
    engine = XccyRiskEngine(sloped_request())
    rows = engine.cashflows()
    h = max(r.payment_date for r in rows) + timedelta(days=10)
    s_h = JOINT.add_business_days(h, 1)
    v = engine.horizon(h, horizon_spot_value_date=s_h, fx_horizon=fx_rule)
    assert v.pv_forward_rub == 0.0 and v.pv_rolled_rub == 0.0 and v.rolldown_rub == 0.0

    def rate(day: date) -> float:
        if fx_rule == "spot_held":
            return S
        return (
            S
            * CNY_SLOPED.discount_factor(day)
            / CNY_SLOPED.discount_factor(SPOT_DATE)
            / (RUB_SLOPED.discount_factor(day) / RUB_SLOPED.discount_factor(SPOT_DATE))
            * math.exp(-20e-4 * (day - SPOT_DATE).days / 365.0)
        )

    received = math.fsum(
        r.amount * (rate(r.payment_date) if r.leg == "foreign" else 1.0) for r in rows
    )
    assert v.received_rub == pytest.approx(received, rel=1e-12)
    assert v.carry_rub == v.horizon_pnl_rub
    assert v.carry_rub == pytest.approx(received - v.pv_start_rub, abs=1e-6)


# --- Floating RUB leg with a period straddling the horizon --------------------------------

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
ENDS = (date(2026, 8, 18), date(2027, 8, 18), date(2028, 8, 18))
N_SEASONED_RUB = N_CNY * 12.0


def seasoned_swap() -> XccySwap:
    starts = (SEASONED_START, *ENDS[:-1])

    def periods(notional: float) -> tuple[XccyPeriod, ...]:
        return tuple(
            XccyPeriod(s, e, JOINT.add_business_days(e, 1), notional)
            for s, e in zip(starts, ENDS, strict=True)
        )

    rub_periods = periods(N_SEASONED_RUB)
    cny_periods = periods(N_CNY)
    return XccySwap(
        "SEASONED",
        rub_leg=XccyLeg(
            "RUB",
            "pay",
            DayCount.ACT_ACT_ISDA,
            rub_periods,
            notional_exchanges(rub_periods, initial_date=SEASONED_START, final_date=ENDS[-1]),
            index=RUONIA_OIS_COMPOUND,
            spread=0.002,
        ),
        foreign_leg=XccyLeg(
            "CNY",
            "receive",
            DayCount.ACT_ACT_ISDA,
            cny_periods,
            notional_exchanges(cny_periods, initial_date=SEASONED_START, final_date=ENDS[-1]),
            fixed_rate=0.031,
        ),
    )


def test_floating_rub_period_straddling_the_horizon_rolls_only_after_it() -> None:
    req = request(seasoned_swap(), CNY, rub_projection_curve=RUB_PROJ_SLOPED, rub_fixings=HISTORY)
    engine = XccyRiskEngine(req)
    h = date(2027, 2, 15)
    s_h = JOINT.add_business_days(h, 1)
    v = engine.horizon(h, horizon_spot_value_date=s_h)
    proj = RUB_PROJ_SLOPED

    def rolled(day: date) -> float:  # the rolled projection: today's shape from h
        return proj.discount_factor(C + (day - h))

    today = {
        (r.leg, r.accrual_start): r.amount
        for r in engine.cashflows()
        if r.kind == "coupon" and r.leg == "rub"
    }
    rub_flows = []
    for start, end in pairwise(ENDS):
        dcf = act_act_isda(start, end)
        days = (end - start).days
        act = 366 if end.year == 2028 else 365  # NFA: 366 if any day is in a leap year
        const = -N_SEASONED_RUB * 0.002 * dcf
        coef = -N_SEASONED_RUB * act / days * dcf
        if start < h:  # straddles: today's forwards over [C, h), rolled over [h, end)
            known_x_df_start = (
                (today[("rub", start)] - const) / coef + 1.0
            ) * proj.discount_factor(end)
            growth = known_x_df_start / proj.discount_factor(h) * rolled(h) / rolled(end)
        else:
            growth = rolled(start) / rolled(end)
        rub_flows.append((JOINT.add_business_days(end, 1), const + coef * (growth - 1.0)))
    rub_flows.append((ENDS[-1], -N_SEASONED_RUB))
    cny_flows = [
        (JOINT.add_business_days(e, 1), N_CNY * 0.031 * act_act_isda(st, e))
        for st, e in pairwise(ENDS)
    ]
    cny_flows.append((ENDS[-1], N_CNY))
    expected = math.fsum(a * df(R_RUB, d, h) for d, a in rub_flows) + S * df(
        R_RUB, s_h, h
    ) * math.fsum(a * df(R_CNY, d, s_h) for d, a in cny_flows)
    assert v.pv_rolled_rub == pytest.approx(expected, rel=1e-11)
    # Today's projection for the forward view: the straddling coupon is today's amount.
    forward_rub = [(r.payment_date, r.amount) for r in engine.cashflows() if r.leg == "rub"]
    forward = math.fsum(a * df(R_RUB, d, h) for d, a in forward_rub if d > h) + S * df(
        R_RUB, s_h, h
    ) * math.fsum(a * df(R_CNY, d, s_h) for d, a in cny_flows)
    assert v.pv_forward_rub == pytest.approx(forward, rel=1e-11)
    assert v.received_rub == 0.0  # nothing is paid between the valuation and the horizon
    assert abs(v.rolldown_rub) > 1.0
    # The straddling coupon's rolled amount differs from today's projection.
    assert rub_flows[0][1] != pytest.approx(today[("rub", ENDS[0])], rel=1e-9)
    assert_identities(v)


# --- Bootstrapped foreign curve -----------------------------------------------------------


def bootstrap_request() -> XccyCurveBootstrapRequest:
    return XccyCurveBootstrapRequest(
        trade_date=C,
        currency="CNY",
        spot_rate=S,
        spot_value_date=SPOT_DATE,
        notional_fx_rate=FX_NOTIONAL,
        quotes=tuple(XccyParQuote(t, r) for t, r in QUOTES),
        convention=CONVENTION,
    )


def test_bootstrapped_foreign_curve_is_carried_as_built() -> None:
    swap = par_swap("3Y", 0.035)
    quoted = request(swap, bootstrap_request())
    built = request(swap, bootstrap_xccy_curve(bootstrap_request(), rub_discount_curve=RUB))
    h = date(2027, 4, 15)
    s_h = JOINT.add_business_days(h, 1)
    for rule in FX_HORIZONS:
        a = xccy_horizon(quoted, horizon_date=h, horizon_spot_value_date=s_h, fx_horizon=rule)
        b = xccy_horizon(built, horizon_date=h, horizon_spot_value_date=s_h, fx_horizon=rule)
        assert a.base_id != b.base_id
        for name in ("pv_start_rub", "pv_forward_rub", "pv_rolled_rub", "received_rub"):
            assert getattr(a, name) == pytest.approx(getattr(b, name), rel=1e-12, abs=1e-6)
        assert abs(a.rolldown_rub) > 1.0  # the bootstrapped curve is not flat
        assert_identities(a)
    engine = XccyRiskEngine(quoted)
    v = engine.horizon(h, horizon_spot_value_date=s_h, fx_horizon="forwards_realised")
    later = [r.pv_rub for r in engine.cashflows() if r.payment_date > h]
    gross = math.fsum(abs(p) for p in later)
    assert abs(v.pv_forward_rub * RUB.discount_factor(h) - math.fsum(later)) <= 1e-12 * gross
    # The rolled view of a node curve keeps its shape: roll_curve of the built curve.
    curve = bootstrap_xccy_curve(bootstrap_request(), rub_discount_curve=RUB)
    rolled = roll_curve(curve, h)
    assert rolled.discount_factor(h + timedelta(days=200)) == pytest.approx(
        curve.discount_factor(C + timedelta(days=200)), rel=1e-15
    )


# --- Validation ---------------------------------------------------------------------------


def test_horizon_validation() -> None:
    engine = XccyRiskEngine(request(fixed_fixed()))
    h = date(2027, 1, 15)
    s_h = JOINT.add_business_days(h, 1)
    with pytest.raises(InvalidTerms):
        engine.horizon(C, horizon_spot_value_date=s_h)
    with pytest.raises(InvalidTerms):
        engine.horizon(C - timedelta(days=1), horizon_spot_value_date=s_h)
    with pytest.raises(InvalidTerms):
        engine.horizon("2027-01-15", horizon_spot_value_date=s_h)  # type: ignore[arg-type]
    with pytest.raises(InvalidTerms):
        engine.horizon(h, horizon_spot_value_date=h - timedelta(days=1))
    with pytest.raises(InvalidTerms):
        engine.horizon(h, horizon_spot_value_date="2027-01-18")  # type: ignore[arg-type]
    with pytest.raises(InvalidTerms):
        engine.horizon(h, horizon_spot_value_date=s_h, fx_horizon="sticky")  # type: ignore[arg-type]
    # The horizon spot date cannot predate today's spot date.
    late_spot = XccyRiskEngine(request(fixed_fixed(), spot_value_date=date(2026, 10, 19)))
    with pytest.raises(InvalidTerms):
        late_spot.horizon(date(2026, 10, 16), horizon_spot_value_date=date(2026, 10, 16))
    v = late_spot.horizon(date(2026, 10, 16), horizon_spot_value_date=date(2026, 10, 19))
    assert v.horizon_spot_value_date == date(2026, 10, 19)
    with pytest.raises(InvalidTerms):
        xccy_horizon(request(fixed_fixed()), horizon_date=C, horizon_spot_value_date=s_h)


def test_horizon_does_not_change_the_engine() -> None:
    engine = XccyRiskEngine(sloped_request())
    pv, rows = engine.base_pv_rub, engine.cashflows()
    engine.horizon(date(2027, 6, 1), horizon_spot_value_date=date(2027, 6, 2))
    assert engine.base_pv_rub == pv and engine.cashflows() == rows


def test_fx_horizon_type_matches_the_horizon_module_and_round_trips() -> None:
    hint = get_type_hints(XccyHorizonValuation)["fx_horizon"]
    assert get_args(hint) == FX_HORIZONS
    engine = XccyRiskEngine(request(fixed_fixed()))
    v = engine.horizon(date(2027, 1, 15), horizon_spot_value_date=date(2027, 1, 18))
    assert from_dict(XccyHorizonValuation, to_dict(v)) == v
