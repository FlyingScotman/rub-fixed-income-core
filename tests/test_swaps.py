"""Seasoned RUONIA OIS and key-rate swaps (package 2).

Independent checks: par swaps reprice the OIS bootstrap and KRS calibration quotes,
seasoned swaps match a cashflow reconstruction written here from the formulas,
risk signs, bucket sums, bump-size convergence, Hessian consistency and carry.
"""

from __future__ import annotations

import math
from dataclasses import replace
from datetime import date, timedelta
from itertools import pairwise
from typing import Literal

import pytest

from rub_fixed_income import (
    RISK_BUCKETS,
    BusinessCalendar,
    BusinessDayConvention,
    CurveBootstrapRequest,
    DayCount,
    DiscountCurve,
    FixingHistory,
    InterestRateSwap,
    InvalidTerms,
    KeyRateCurveBuildRequest,
    KeyRateForwardCurve,
    KeyRateForwardSegment,
    KeyRateSwapPeriod,
    KeyRateSwapPricingRequest,
    KeyRateSwapQuote,
    MissingMarketData,
    OisQuote,
    ShockedCurveCache,
    SwapConvention,
    SwapLegSchedule,
    SwapPeriod,
    SwapRiskEngine,
    SwapRiskRequest,
    SwapScenario,
    UnknownCurveSpec,
    UnsupportedProduct,
    bootstrap_discount_curve,
    build_key_rate_forward_curve,
    from_dict,
    get_curve_spec,
    price_key_rate_swap,
    swap_convention,
    swap_factor_risk,
    swap_horizon,
    swap_schedule,
    to_dict,
)
from rub_fixed_income._risk_common import _central_derivatives

Side = Literal["payer", "receiver"]
HOLIDAYS = frozenset({date(2026, 11, 4), date(2027, 1, 1), date(2027, 1, 7)})
CALENDAR = BusinessCalendar(holidays=HOLIDAYS, version="synthetic-swap-test")
NOTIONAL = 1_000_000_000.0
TODAY = date(2026, 10, 15)
TENORS = (
    ("1M", 30),
    ("3M", 91),
    ("6M", 182),
    ("1Y", 365),
    ("2Y", 731),
    ("3Y", 1096),
    ("5Y", 1827),
    ("7Y", 2557),
    ("10Y", 3653),
)


def is_business_day(day: date) -> bool:
    return day.weekday() < 5 and day not in HOLIDAYS


def ois_request(curve_date: date = TODAY) -> CurveBootstrapRequest:
    quotes = tuple(
        OisQuote(tenor, curve_date + timedelta(days=days), 0.155 - 0.0015 * i)
        for i, (tenor, days) in enumerate(TENORS)
    )
    return CurveBootstrapRequest(curve_date, quotes)


LEGACY = swap_convention("rub_ruonia_ois_legacy_v1")
KRS = swap_convention("rub_key_rate_swap_v1")


def seasoned_ois_convention(payment_lag: int = 1) -> SwapConvention:
    return SwapConvention(
        index="ruonia",
        averaging="compounded",
        fixed_leg=SwapLegSchedule(12, "backward", DayCount.ACT_365_FIXED),
        float_leg=SwapLegSchedule(3, "backward", DayCount.ACT_365_FIXED),
        rate_day_count=DayCount.ACT_365_FIXED,
        business_day_convention=BusinessDayConvention.MODIFIED_FOLLOWING,
        spot_lag_days=1,
        payment_lag_days=payment_lag,
    )


def seasoned_ois(side: Side = "payer", **changes: object) -> InterestRateSwap:
    swap = InterestRateSwap(
        "OIS-SEASONED",
        "payer",
        NOTIONAL,
        0.152,
        date(2026, 1, 15),
        date(2028, 1, 15),
        seasoned_ois_convention(),
        float_spread=0.0025,
    )
    return replace(swap, side=side, **changes)  # type: ignore[arg-type]


def ruonia_history(valuation: date, first: date = date(2026, 9, 1)) -> FixingHistory:
    rows = []
    day = first
    k = 0
    while day < valuation:
        if is_business_day(day):
            rows.append((day, 0.16 + 0.0001 * (k % 7)))
            k += 1
        day += timedelta(days=1)
    return FixingHistory("ruonia", valuation, tuple(rows), CALENDAR, "ruonia-test")


# --- conventions and schedules ---------------------------------------------------


def test_spec_conventions_agree_with_the_registry() -> None:
    for convention in (LEGACY, KRS):
        spec = get_curve_spec(str(convention.spec_id))
        assert convention.fixed_leg.frequency_months == spec.fixed_leg_frequency_months
        assert convention.fixed_leg.day_count is spec.fixed_leg_day_count
        assert convention.spot_lag_days == spec.spot_lag_business_days
        assert convention.payment_lag_days == spec.payment_lag_business_days
        assert convention.business_day_convention.value == spec.business_day_convention
    assert LEGACY.fixed_leg.roll == "legacy-annual-forward"
    assert LEGACY.rate_day_count is DayCount.ACT_365_FIXED
    assert (KRS.float_leg.frequency_months, KRS.fixed_leg.single_period_up_to_months) == (3, 12)
    assert KRS.rate_day_count is None
    with pytest.raises(UnknownCurveSpec):
        swap_convention("rub_ruonia_ois_moex_v2")
    with pytest.raises(UnsupportedProduct):
        swap_convention("rub_ofz_kbd_v1")


def test_schedule_backward_roll_modified_following_and_payment_lag() -> None:
    schedule = swap_schedule(seasoned_ois(), CALENDAR)
    ends = [
        date(2026, 4, 15),
        date(2026, 7, 15),
        date(2026, 10, 15),
        date(2027, 1, 15),
        date(2027, 4, 15),
        date(2027, 7, 15),
        date(2027, 10, 15),
        date(2028, 1, 17),  # Saturday 15th, modified following
    ]
    assert [p.accrual_end for p in schedule.floating] == ends
    assert [p.accrual_start for p in schedule.floating] == [date(2026, 1, 15), *ends[:-1]]
    pays = [p.payment_date for p in schedule.floating]
    assert pays[2] == date(2026, 10, 16)
    assert pays[3] == date(2027, 1, 18)  # Friday + 1 business day
    assert [p.accrual_end for p in schedule.fixed] == [date(2027, 1, 15), date(2028, 1, 17)]
    assert schedule.fixed[1].accrual_factor == (date(2028, 1, 17) - date(2027, 1, 15)).days / 365


def test_legacy_schedule_is_the_bootstrap_schedule() -> None:
    curve = bootstrap_discount_curve(ois_request())
    for quote in ois_request().quotes:
        swap = InterestRateSwap("x", "payer", NOTIONAL, 0.1, TODAY, quote.maturity, LEGACY)
        schedule = swap_schedule(swap, CALENDAR)
        assert tuple(p.payment_date for p in schedule.fixed) == curve.fixed_leg_schedule(
            quote.maturity
        )


def test_key_rate_schedule_is_the_krs_pricer_schedule() -> None:
    discount = DiscountCurve.flat(CALENDAR.add_business_days(TODAY, 1), 0.14)
    curve = key_rate_curve(discount)
    for tenor in ("1M", "3M", "9M", "1Y", "18M", "2Y", "3Y"):
        swap = InterestRateSwap.from_tenor(
            swap_id="k",
            side="payer",
            notional=NOTIONAL,
            fixed_rate=0.15,
            trade_date=TODAY,
            tenor=tenor,
            calendar=CALENDAR,
            convention=KRS,
        )
        reference = price_key_rate_swap(
            KeyRateSwapPricingRequest(TODAY, tenor, curve, discount, CALENDAR)
        )
        schedule = swap_schedule(swap, CALENDAR)

        def periods(rows: tuple[KeyRateSwapPeriod, ...]) -> tuple[SwapPeriod, ...]:
            return tuple(
                SwapPeriod(r.accrual_start, r.accrual_end, r.payment_date, r.accrual_factor)
                for r in rows
            )

        assert schedule.fixed == periods(reference.fixed_periods)
        assert schedule.floating == periods(reference.floating_periods)


# --- par swaps at inception --------------------------------------------------------


@pytest.mark.parametrize("side", ["payer", "receiver"])
def test_par_ois_swaps_reprice_the_bootstrap_quotes(side: Side) -> None:
    request = ois_request()
    cache = ShockedCurveCache()
    for quote in request.quotes:
        swap = InterestRateSwap(
            f"ois-{quote.tenor}", side, NOTIONAL, quote.par_rate, TODAY, quote.maturity, LEGACY
        )
        engine = SwapRiskEngine(SwapRiskRequest(swap, CALENDAR, request), cache)
        valuation = engine.valuation()
        assert abs(valuation.pv_rub) < 1e-12 * NOTIONAL
        assert valuation.par_rate == pytest.approx(quote.par_rate, abs=1e-13)
        assert valuation.accrued_rub == 0.0
        assert {c.source for c in valuation.cashflows if c.leg == "float"} == {"projected"}
    assert cache.misses == 1  # one bootstrap shared by every swap


def key_rate_quotes() -> tuple[KeyRateSwapQuote, ...]:
    mids = (("3M", 0.165), ("6M", 0.16), ("1Y", 0.152), ("2Y", 0.143), ("3Y", 0.138))
    return tuple(KeyRateSwapQuote(t, m - 0.0005, m, m + 0.0005) for t, m in mids)


def key_rate_curve(discount: DiscountCurve) -> KeyRateForwardCurve:
    built = build_key_rate_forward_curve(
        KeyRateCurveBuildRequest(
            TODAY, 0.165, date(2026, 10, 27), key_rate_quotes(), discount, CALENDAR
        )
    )
    return built.mid.curve


@pytest.mark.parametrize("side", ["payer", "receiver"])
def test_par_key_rate_swaps_reprice_the_krs_quotes(side: Side) -> None:
    tom = CALENDAR.add_business_days(TODAY, 1)
    discount = bootstrap_discount_curve(ois_request(tom))
    curve = key_rate_curve(discount)
    for quote in key_rate_quotes():
        swap = InterestRateSwap.from_tenor(
            swap_id=f"krs-{quote.tenor}",
            side=side,
            notional=NOTIONAL,
            fixed_rate=quote.mid,
            trade_date=TODAY,
            tenor=quote.tenor,
            calendar=CALENDAR,
            convention=KRS,
        )
        valuation = SwapRiskEngine(SwapRiskRequest(swap, CALENDAR, discount, curve)).valuation()
        reference = price_key_rate_swap(
            KeyRateSwapPricingRequest(TODAY, quote.tenor, curve, discount, CALENDAR)
        )
        assert abs(valuation.pv_rub) < 1e-12 * NOTIONAL
        assert valuation.par_rate == pytest.approx(quote.mid, abs=1e-12)
        assert valuation.annuity == pytest.approx(reference.fixed_annuity, rel=1e-15)
        sign = 1.0 if side == "payer" else -1.0
        assert valuation.float_leg_pv_rub == pytest.approx(
            sign * NOTIONAL * reference.floating_pv, rel=1e-14
        )


# --- seasoned swaps against an independent reconstruction --------------------------

R_PROJ = 0.148
R_DISC = 0.141


def flat(rate: float, on: date) -> DiscountCurve:
    return DiscountCurve.flat(on, rate)


def reconstruct_ois(swap: InterestRateSwap, valuation: date, history: FixingHistory) -> float:
    """PV from the formulas, written independently of the engine."""
    fixings = dict(history.fixings)

    def df(curve_rate: float, start: date, end: date) -> float:
        return math.exp(-curve_rate * (end - start).days / 365.0)

    def latest_business_day(day: date) -> date:
        while not is_business_day(day):
            day -= timedelta(days=1)
        return day

    float_periods = [
        (date(2026, 10, 15), date(2027, 1, 15), date(2027, 1, 18)),
        (date(2027, 1, 15), date(2027, 4, 15), date(2027, 4, 16)),
        (date(2027, 4, 15), date(2027, 7, 15), date(2027, 7, 16)),
        (date(2027, 7, 15), date(2027, 10, 15), date(2027, 10, 18)),
        (date(2027, 10, 15), date(2028, 1, 17), date(2028, 1, 18)),
    ]
    fixed_periods = [
        (date(2026, 1, 15), date(2027, 1, 15), date(2027, 1, 18)),
        (date(2027, 1, 15), date(2028, 1, 17), date(2028, 1, 18)),
    ]
    pv = 0.0
    for start, end, pay in float_periods:
        days_by_observation: dict[date, int] = {}
        day = start
        while day < min(valuation, end):
            observation = latest_business_day(day)
            days_by_observation[observation] = days_by_observation.get(observation, 0) + 1
            day += timedelta(days=1)
        growth = math.prod(1 + fixings[o] * n / 365 for o, n in days_by_observation.items())
        projected_from = max(start, valuation)
        growth *= math.exp(R_PROJ * (end - projected_from).days / 365)
        amount = NOTIONAL * (growth - 1 + swap.float_spread * (end - start).days / 365)
        pv += amount * df(R_DISC, valuation, pay)
    for start, end, pay in fixed_periods:
        amount = NOTIONAL * swap.fixed_rate * (end - start).days / 365
        pv -= amount * df(R_DISC, valuation, pay)
    return pv


def seasoned_request(side: Side = "payer") -> SwapRiskRequest:
    valuation = date(2026, 11, 2)
    return SwapRiskRequest(
        seasoned_ois(side),
        CALENDAR,
        flat(R_DISC, valuation),
        flat(R_PROJ, valuation),
        ruonia_history(valuation),
    )


def test_seasoned_ois_matches_the_independent_reconstruction() -> None:
    request = seasoned_request()
    valuation = SwapRiskEngine(request).valuation()
    expected = reconstruct_ois(request.swap, date(2026, 11, 2), cast_history(request))
    assert valuation.pv_rub == pytest.approx(expected, rel=1e-12, abs=1e-4)
    current = next(c for c in valuation.cashflows if c.leg == "float")
    assert (current.source, current.known_days, current.projected_days) == (
        "partially_projected",
        18,
        74,
    )
    assert [c.source for c in valuation.cashflows if c.leg == "float"][1:] == ["projected"] * 4
    receiver = SwapRiskEngine(seasoned_request("receiver")).valuation()
    assert receiver.pv_rub == pytest.approx(-valuation.pv_rub, rel=1e-15)


def cast_history(request: SwapRiskRequest) -> FixingHistory:
    assert request.fixings is not None
    return request.fixings


def test_seasoned_ois_accrued_and_clean_pv() -> None:
    request = seasoned_request()
    valuation = SwapRiskEngine(request).valuation()
    swap = request.swap
    fixed = NOTIONAL * swap.fixed_rate * (date(2026, 11, 2) - date(2026, 1, 15)).days / 365
    history = dict(cast_history(request).fixings)
    # Each business day's fixing compounds over the days to the next business day.
    growth = 1.0
    day = date(2026, 10, 15)
    while day < date(2026, 11, 2):
        nxt = day + timedelta(days=1)
        while not is_business_day(nxt):
            nxt += timedelta(days=1)
        growth *= 1 + history[day] * (nxt - day).days / 365
        day = nxt
    spread = swap.float_spread * 18 / 365
    assert valuation.accrued_fixed_rub == pytest.approx(-fixed, rel=1e-14)
    assert valuation.accrued_float_rub == pytest.approx(NOTIONAL * (growth - 1 + spread), rel=1e-13)
    assert valuation.clean_pv_rub == pytest.approx(
        valuation.pv_rub - valuation.accrued_rub, rel=1e-15
    )


def test_seasoned_key_rate_swap_matches_the_independent_reconstruction() -> None:
    valuation_date = date(2026, 11, 3)  # TOM of 2 November
    swap = InterestRateSwap.from_tenor(
        swap_id="KRS-SEASONED",
        side="receiver",
        notional=NOTIONAL,
        fixed_rate=0.171,
        trade_date=date(2026, 7, 14),
        tenor="1Y",
        calendar=CALENDAR,
        convention=KRS,
        float_spread=-0.001,
    )
    assert (swap.effective_date, swap.termination_date) == (date(2026, 7, 15), date(2027, 7, 15))
    history = FixingHistory(
        "key_rate",
        valuation_date,
        ((date(2026, 7, 1), 0.18), (date(2026, 9, 15), 0.17), (date(2026, 10, 27), 0.165)),
        CALENDAR,
        "key-rate-test",
    )
    curve = KeyRateForwardCurve(
        valuation_date,
        0.165,
        date(2026, 12, 22),
        (
            KeyRateForwardSegment(date(2026, 12, 22), date(2027, 3, 20), 0.155, "6M"),
            KeyRateForwardSegment(date(2027, 3, 20), date(2027, 8, 1), 0.145, "1Y"),
        ),
    )
    request = SwapRiskRequest(swap, CALENDAR, flat(R_DISC, valuation_date), curve, history)
    valuation = SwapRiskEngine(request).valuation()

    def key_rate(day: date) -> float:
        if day < date(2026, 9, 15):
            return 0.18
        if day < date(2026, 10, 27):
            return 0.17
        if day < date(2026, 12, 22):
            return 0.165
        return 0.155 if day < date(2027, 3, 20) else 0.145

    def isda(start: date, end: date) -> float:
        total = 0.0
        day = start
        while day < end:
            total += 1 / (366 if day.year % 4 == 0 else 365)
            day += timedelta(days=1)
        return total

    pv = 0.0
    for start, end in (
        (date(2026, 10, 15), date(2027, 1, 15)),
        (date(2027, 1, 15), date(2027, 4, 15)),
        (date(2027, 4, 15), date(2027, 7, 15)),
    ):
        days = [start + timedelta(days=k) for k in range((end - start).days)]
        average = sum(key_rate(d) for d in days) / len(days)
        amount = NOTIONAL * (average + swap.float_spread) * isda(start, end)
        pv -= amount * math.exp(-R_DISC * (end - valuation_date).days / 365)
    fixed_end = date(2027, 7, 15)
    fixed = NOTIONAL * swap.fixed_rate * isda(date(2026, 7, 15), fixed_end)
    pv += fixed * math.exp(-R_DISC * (fixed_end - valuation_date).days / 365)
    assert valuation.pv_rub == pytest.approx(pv, rel=1e-12)
    current = next(c for c in valuation.cashflows if c.leg == "float")
    assert (current.source, current.known_days, current.projected_days) == (
        "partially_projected",
        19,
        73,
    )
    known = 12 * 0.17 + 7 * 0.165
    expected_accrued = -NOTIONAL * (known / 92 * isda(date(2026, 10, 15), date(2027, 1, 15)))
    expected_accrued -= NOTIONAL * swap.float_spread * 19 / 365
    assert valuation.accrued_float_rub == pytest.approx(expected_accrued, rel=1e-12)
    assert valuation.accrued_fixed_rub == pytest.approx(
        NOTIONAL * 0.171 * isda(date(2026, 7, 15), valuation_date), rel=1e-12
    )


def test_float_spread_is_added_simply_over_the_float_accrual() -> None:
    base = seasoned_request()
    without = replace(base, swap=replace(base.swap, float_spread=0.0))
    engine, plain = SwapRiskEngine(base), SwapRiskEngine(without)
    expected = (
        NOTIONAL
        * 0.0025
        * math.fsum(
            c.accrual_factor * c.discount_factor
            for c in engine.valuation().cashflows
            if c.leg == "float"
        )
    )
    assert engine.base_pv - plain.base_pv == pytest.approx(expected, rel=1e-10)


# --- fixings ----------------------------------------------------------------------


def test_a_started_period_needs_its_fixings() -> None:
    request = seasoned_request()
    with pytest.raises(MissingMarketData, match="fixing history is required"):
        SwapRiskEngine(replace(request, fixings=None))
    gappy = cast_history(request)
    rows = tuple(r for r in gappy.fixings if r[0] != date(2026, 10, 20))
    with pytest.raises(MissingMarketData, match="missing RUONIA"):
        SwapRiskEngine(replace(request, fixings=replace(gappy, fixings=rows)))
    late = ruonia_history(date(2026, 11, 2), first=date(2026, 10, 19))
    with pytest.raises(MissingMarketData, match="no RUONIA fixing"):
        SwapRiskEngine(replace(request, fixings=late))


def test_known_flows_do_not_move_under_projection_shocks() -> None:
    valuation = date(2026, 10, 15)  # the period ended today, paid tomorrow
    swap = seasoned_ois(termination_date=date(2026, 10, 15))
    request = SwapRiskRequest(
        swap,
        CALENDAR,
        flat(R_DISC, valuation),
        flat(R_PROJ, valuation),
        ruonia_history(valuation, first=date(2026, 7, 1)),
    )
    engine = SwapRiskEngine(request)
    flows = engine.valuation().cashflows
    assert [(c.leg, c.source) for c in flows] == [("fixed", "fixed"), ("float", "known")]
    projection = engine.risk().curves[1]
    assert projection.curve_role == "projection"
    assert all(b.dv01_rub == 0.0 for b in projection.buckets)
    assert engine.reprice(SwapScenario.parallel(projection_bp=300.0)).total_pnl_rub == 0.0


def test_key_rate_known_until_the_next_meeting_has_no_projection_risk() -> None:
    tom = CALENDAR.add_business_days(TODAY, 1)
    curve = KeyRateForwardCurve(
        tom,
        0.165,
        date(2027, 1, 20),
        (KeyRateForwardSegment(date(2027, 1, 20), date(2028, 1, 1), 0.15, "1Y"),),
    )
    short = InterestRateSwap.from_tenor(
        swap_id="short",
        side="payer",
        notional=NOTIONAL,
        fixed_rate=0.16,
        trade_date=TODAY,
        tenor="3M",
        calendar=CALENDAR,
        convention=KRS,
    )
    engine = SwapRiskEngine(SwapRiskRequest(short, CALENDAR, flat(R_DISC, tom), curve))
    assert engine.risk().curves[1].dv01_rub == 0.0
    longer = replace(short, termination_date=date(2027, 10, 16))
    risk = SwapRiskEngine(SwapRiskRequest(longer, CALENDAR, flat(R_DISC, tom), curve)).risk()
    assert risk.curves[1].dv01_rub > 0.0


# --- risk ------------------------------------------------------------------------


def ois_engine(
    side: Side,
    curve: DiscountCurve | CurveBootstrapRequest | None = None,
    cache: ShockedCurveCache | None = None,
) -> SwapRiskEngine:
    request = ois_request() if curve is None else curve
    quote = ois_request().quotes[6]  # 5Y
    swap = InterestRateSwap("ois-5y", side, NOTIONAL, 0.15, TODAY, quote.maturity, LEGACY)
    return SwapRiskEngine(
        SwapRiskRequest(swap, CALENDAR, request), ShockedCurveCache() if cache is None else cache
    )


@pytest.mark.parametrize("basis", ["quote", "zero"])
def test_dv01_sign_by_side_and_bucket_sum(basis: str) -> None:
    cache = ShockedCurveCache()
    curve = ois_request() if basis == "quote" else bootstrap_discount_curve(ois_request())
    payer = ois_engine("payer", curve, cache).risk()
    receiver = ois_engine("receiver", curve, cache).risk()
    (payer_curve,) = payer.curves
    assert payer_curve.curve_role == "discount_and_projection"
    assert payer_curve.curve_basis == ("ois_quote" if basis == "quote" else "continuous_zero")
    assert payer.dv01_rub > 0.0 > receiver.dv01_rub
    assert payer.dv01_rub == pytest.approx(-receiver.dv01_rub, rel=1e-12)
    assert payer.dv01_rub == math.fsum(b.dv01_rub for b in payer_curve.buckets)
    assert payer_curve.parallel_dv01_rub == pytest.approx(payer.dv01_rub, rel=1e-4)
    if basis == "quote":  # a 1 bp par-quote move is worth about notional * annuity * 1 bp
        annuity = ois_engine("payer", curve, cache).valuation().annuity
        assert payer.dv01_rub == pytest.approx(NOTIONAL * annuity * 1e-4, rel=0.01)
    krd = sum(b.krd_years for b in payer_curve.buckets)
    assert krd == pytest.approx(-payer.dv01_rub / NOTIONAL * 1e4, rel=1e-12)


def test_two_curve_risk_splits_discount_and_projection() -> None:
    request = seasoned_request()
    engine = SwapRiskEngine(request)
    risk = engine.risk()
    discount, projection = risk.curves
    assert (discount.curve_role, projection.curve_role) == ("discount", "projection")
    assert (discount.curve_basis, projection.curve_basis) == ("continuous_zero",) * 2
    assert projection.dv01_rub > 0.0  # a payer receives float
    assert risk.dv01_rub == discount.dv01_rub + projection.dv01_rub
    # Both curves together equal the single-curve sensitivity to first order.
    both = engine.reprice(SwapScenario.parallel(1.0, 1.0)).total_pnl_rub
    down = engine.reprice(SwapScenario.parallel(-1.0, -1.0)).total_pnl_rub
    assert (both - down) / 2 == pytest.approx(risk.dv01_rub, rel=1e-3)


def test_central_dv01_converges_with_the_bump_size() -> None:
    engine = ois_engine("payer", bootstrap_discount_curve(ois_request()))
    values = [engine.risk(bump_bp=h).curves[0].parallel_dv01_rub for h in (4.0, 2.0, 1.0, 0.5)]
    steps = [abs(a - b) for a, b in pairwise(values)]
    assert steps[0] > steps[1] > steps[2] > 0.0
    assert steps[0] / steps[1] == pytest.approx(4.0, rel=0.05)  # second-order error
    forward = engine.risk(bump_bp=0.5, difference="forward").curves[0].parallel_dv01_rub
    assert forward == pytest.approx(values[-1], rel=1e-3)


def test_hessian_matches_risk_and_a_taylor_expansion() -> None:
    engine = SwapRiskEngine(seasoned_request())
    second = engine.second_order()
    risk = engine.risk()
    first = [b.dv01_rub for c in risk.curves for b in c.buckets]
    assert second.gradient_rub_per_bp == pytest.approx(first, rel=1e-12, abs=1e-9)
    size = len(second.factors)
    assert (
        size == 22 and second.factors[0] == "discount 1M" and second.factors[11] == "projection 1M"
    )
    h = second.hessian_rub_per_bp2
    assert all(h[i][j] == h[j][i] for i in range(size) for j in range(size))
    moves = [10.0] * size
    taylor = sum(g * m for g, m in zip(second.gradient_rub_per_bp, moves, strict=True))
    taylor += 0.5 * sum(h[i][j] * moves[i] * moves[j] for i in range(size) for j in range(size))
    exact = engine.reprice(SwapScenario.parallel(10.0, 10.0)).total_pnl_rub
    linear = sum(g * m for g, m in zip(second.gradient_rub_per_bp, moves, strict=True))
    assert abs(exact - taylor) < 1e-3 * abs(exact - linear)


@pytest.mark.parametrize("product", ["ois", "krs"])
def test_two_curve_hessian_equals_the_generic_central_scheme(product: str) -> None:
    if product == "ois":
        engine = SwapRiskEngine(seasoned_request())
    else:
        tom = CALENDAR.add_business_days(TODAY, 1)
        discount = bootstrap_discount_curve(ois_request(tom))
        swap = InterestRateSwap.from_tenor(
            swap_id="krs",
            side="payer",
            notional=NOTIONAL,
            fixed_rate=0.15,
            trade_date=TODAY,
            tenor="3Y",
            calendar=CALENDAR,
            convention=KRS,
        )
        engine = SwapRiskEngine(
            SwapRiskRequest(swap, CALENDAR, ois_request(tom), key_rate_curve(discount))
        )
    second = engine.second_order(bump_bp=2.0)

    def price(moves: list[float]) -> float:
        return engine._pv(tuple(moves[:11]), tuple(moves[11:]))

    gradient, hessian = _central_derivatives(price, 22, 2.0, engine.base_pv, 1.0)
    assert second.gradient_rub_per_bp == gradient
    scale = max(abs(v) for row in hessian for v in row)
    for row, expected in zip(second.hessian_rub_per_bp2, hessian, strict=True):
        assert row == pytest.approx(expected, abs=1e-9 * scale)
    if product == "krs":  # linear projection: its own block is exactly zero
        assert all(v == 0.0 for row in second.hessian_rub_per_bp2[11:] for v in row[11:])
        assert any(v != 0.0 for row in second.hessian_rub_per_bp2[:11] for v in row[11:])


def test_scenario_parts_sum_and_single_curve_rejects_projection_shifts() -> None:
    engine = SwapRiskEngine(seasoned_request())
    result = engine.reprice(SwapScenario(tuple(5.0 * i for i in range(11)), (-20.0,) * 11))
    parts = result.discount_pnl_rub + result.projection_pnl_rub + result.interaction_pnl_rub
    assert parts == pytest.approx(result.total_pnl_rub, rel=1e-12)
    assert result.shocked_pv_rub - result.base_pv_rub == pytest.approx(result.total_pnl_rub)
    single = ois_engine("payer")
    with pytest.raises(InvalidTerms, match="single-curve"):
        single.reprice(SwapScenario.parallel(projection_bp=1.0))


def test_key_rate_projection_risk_is_linear_and_buckets_sum_to_parallel() -> None:
    tom = CALENDAR.add_business_days(TODAY, 1)
    discount = bootstrap_discount_curve(ois_request(tom))
    curve = key_rate_curve(discount)
    swap = InterestRateSwap.from_tenor(
        swap_id="krs-3y",
        side="receiver",
        notional=NOTIONAL,
        fixed_rate=0.14,
        trade_date=TODAY,
        tenor="3Y",
        calendar=CALENDAR,
        convention=KRS,
    )
    engine = SwapRiskEngine(SwapRiskRequest(swap, CALENDAR, discount, curve))
    projection = engine.risk().curves[1]
    assert projection.curve_basis == "key_rate_forward"
    assert projection.dv01_rub < 0.0
    assert projection.parallel_dv01_rub == pytest.approx(projection.dv01_rub, rel=1e-12)
    # +1 bp on every projected key-rate day: minus the float accrual after the meeting.
    pnl = engine.reprice(SwapScenario.parallel(projection_bp=1.0)).total_pnl_rub
    expected = (
        -NOTIONAL
        * 1e-4
        * math.fsum(
            c.accrual_factor
            * c.discount_factor
            * max(0, (c.accrual_end - max(c.accrual_start, date(2026, 10, 27))).days)
            / (c.accrual_end - c.accrual_start).days
            for c in engine.valuation().cashflows
            if c.leg == "float"
        )
    )
    assert pnl == pytest.approx(expected, rel=1e-9)
    assert projection.parallel_dv01_rub == pytest.approx(expected, rel=1e-9)


# --- desk factors ------------------------------------------------------------------


def test_factor_mapping_single_and_two_curve() -> None:
    single = ois_engine("payer", bootstrap_discount_curve(ois_request()))
    risk = single.risk()
    factors = swap_factor_risk(risk, discount_curve_id="RUB_OIS")
    assert factors.factors == tuple(f"IR|RUB_OIS|{b}" for b in RISK_BUCKETS)
    assert not factors.has_second_order
    with_hessian = swap_factor_risk(
        risk, discount_curve_id="RUB_OIS", second_order=single.second_order()
    )
    assert with_hessian.has_second_order and with_hessian.hessian
    with pytest.raises(InvalidTerms):
        swap_factor_risk(risk, discount_curve_id="RUB_OIS", projection_curve_id="OTHER")
    with pytest.raises(InvalidTerms, match="DEC08"):
        swap_factor_risk(single.risk(difference="forward"), discount_curve_id="RUB_OIS")

    two = SwapRiskEngine(seasoned_request())
    two_risk = two.risk()
    mapped = swap_factor_risk(
        two_risk,
        discount_curve_id="RUB_OIS_DISC",
        projection_curve_id="RUB_RUONIA_PROJ",
        second_order=two.second_order(),
    )
    assert len(mapped.factors) == 22
    assert mapped.factors[11] == "IR|RUB_RUONIA_PROJ|1M"
    with pytest.raises(InvalidTerms, match="distinct"):
        swap_factor_risk(two_risk, discount_curve_id="X", projection_curve_id="X")
    with pytest.raises(InvalidTerms, match="different"):
        swap_factor_risk(
            two_risk,
            discount_curve_id="A",
            projection_curve_id="B",
            second_order=single.second_order(),
        )


# --- carry ------------------------------------------------------------------------


def test_carry_on_a_flat_single_curve_has_no_rolldown() -> None:
    rate = 0.15
    swap = InterestRateSwap("flat", "receiver", NOTIONAL, 0.16, TODAY, date(2028, 10, 15), LEGACY)
    request = SwapRiskRequest(swap, CALENDAR, flat(rate, TODAY))
    horizon_date = date(2027, 1, 15)
    horizon = swap_horizon(request, horizon_date=horizon_date)
    flows = SwapRiskEngine(request).valuation().cashflows
    df_h = math.exp(-rate * (horizon_date - TODAY).days / 365)
    expected = math.fsum(
        c.amount_rub * c.discount_factor * (1 / df_h - 1)
        if c.payment_date > horizon_date
        else c.amount_rub * (1 - c.discount_factor)
        for c in flows
    )
    assert horizon.rolldown_rub == pytest.approx(0.0, abs=1e-6)
    assert horizon.carry_rub == pytest.approx(expected, rel=1e-10)
    assert horizon.horizon_pnl_rub == pytest.approx(horizon.carry_rub + horizon.rolldown_rub)
    assert horizon.received_rub == 0.0


def test_carry_receives_flows_paid_before_the_horizon() -> None:
    request = seasoned_request()
    horizon = swap_horizon(request, horizon_date=date(2027, 2, 1))
    flows = SwapRiskEngine(request).valuation().cashflows
    paid = math.fsum(c.amount_rub for c in flows if c.payment_date <= date(2027, 2, 1))
    assert horizon.received_rub == pytest.approx(paid, rel=1e-15)
    assert horizon.carry_rub + horizon.rolldown_rub == pytest.approx(horizon.horizon_pnl_rub)
    # Flat projection and discount curves: rolling keeps every forward, so no rolldown.
    assert horizon.rolldown_rub == pytest.approx(0.0, abs=1e-5)
    with pytest.raises(InvalidTerms):
        swap_horizon(request, horizon_date=date(2026, 11, 2))


def test_key_rate_rolldown_follows_the_curve_slope() -> None:
    tom = CALENDAR.add_business_days(TODAY, 1)
    discount = flat(0.14, tom)
    curve = key_rate_curve(discount)  # key rate falling with maturity
    swap = InterestRateSwap.from_tenor(
        swap_id="krs-2y",
        side="receiver",
        notional=NOTIONAL,
        fixed_rate=0.143,
        trade_date=TODAY,
        tenor="2Y",
        calendar=CALENDAR,
        convention=KRS,
    )
    horizon = swap_horizon(
        SwapRiskRequest(swap, CALENDAR, discount, curve), horizon_date=date(2026, 12, 16)
    )
    assert horizon.rolldown_rub < 0.0  # rolled forwards are higher: the receiver loses


# --- validation -------------------------------------------------------------------


def test_typed_rejections() -> None:
    with pytest.raises(UnsupportedProduct):
        replace(seasoned_ois(), currency="CNY")
    with pytest.raises(UnsupportedProduct):
        replace(seasoned_ois_convention(), averaging="arithmetic", rate_day_count=None)
    with pytest.raises(InvalidTerms):
        replace(seasoned_ois(), termination_date=date(2026, 1, 15))
    with pytest.raises(InvalidTerms):
        SwapLegSchedule(6, "legacy-annual-forward", DayCount.ACT_365_FIXED)
    with pytest.raises(InvalidTerms):
        InterestRateSwap.from_tenor(
            swap_id="x",
            side="payer",
            notional=1.0,
            fixed_rate=0.1,
            trade_date=TODAY,
            tenor="ON",
            calendar=CALENDAR,
            convention=KRS,
        )
    request = seasoned_request()
    with pytest.raises(InvalidTerms, match="valuation date"):
        replace(request, projection_curve=flat(R_PROJ, TODAY))
    with pytest.raises(InvalidTerms, match="KeyRateForwardCurve"):
        SwapRiskRequest(replace(request.swap, convention=KRS), CALENDAR, flat(R_DISC, TODAY))
    with pytest.raises(InvalidTerms, match="index"):
        replace(
            request,
            fixings=FixingHistory("key_rate", date(2026, 11, 2), (), CALENDAR, "k"),
        )
    with pytest.raises(InvalidTerms, match="no swap flows"):
        SwapRiskEngine(
            replace(
                request,
                discount_curve=flat(R_DISC, date(2029, 1, 1)),
                projection_curve=None,
                fixings=None,
            )
        )
    tom = CALENDAR.add_business_days(TODAY, 1)
    short_curve = KeyRateForwardCurve(tom, 0.165, date(2027, 1, 1), ())
    long_swap = InterestRateSwap.from_tenor(
        swap_id="k",
        side="payer",
        notional=NOTIONAL,
        fixed_rate=0.15,
        trade_date=TODAY,
        tenor="2Y",
        calendar=CALENDAR,
        convention=KRS,
    )
    with pytest.raises(MissingMarketData, match="does not cover"):
        SwapRiskEngine(SwapRiskRequest(long_swap, CALENDAR, flat(R_DISC, tom), short_curve))


def test_terms_round_trip_and_identity() -> None:
    swap = seasoned_ois()
    assert from_dict(InterestRateSwap, to_dict(swap)) == swap
    krs = InterestRateSwap.from_tenor(
        swap_id="k",
        side="receiver",
        notional=NOTIONAL,
        fixed_rate=0.15,
        trade_date=TODAY,
        tenor="2Y",
        calendar=CALENDAR,
        convention=KRS,
    )
    assert from_dict(InterestRateSwap, to_dict(krs)) == krs
    first, second = SwapRiskEngine(seasoned_request()), SwapRiskEngine(seasoned_request())
    assert first.base_id == second.base_id
    moved = seasoned_request()
    moved = replace(moved, swap=replace(moved.swap, fixed_rate=0.153))
    assert SwapRiskEngine(moved).base_id != first.base_id
