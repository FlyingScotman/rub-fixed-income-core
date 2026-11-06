"""IRS legs on their own dates (FlyingScotman/rub-fixed-income-core#106).

Each leg rolls its schedule from its own effective and termination dates; one leg's dates
are never aligned to the other's. A leg that has not started takes no fixings and projects
its first period on the curve. Swaps on common dates keep their results and identities.
"""

from __future__ import annotations

import math
from dataclasses import replace
from datetime import date, timedelta

import pytest
from portfolio_v3_book import AS_OF, POLICY, ois_swap_convention, snapshot
from portfolio_v3_book import CAL as BOOK_CAL
from portfolio_v3_book import ruonia_history as book_ruonia_history
from test_swaps import (
    CALENDAR,
    KRS,
    NOTIONAL,
    R_DISC,
    R_PROJ,
    flat,
    is_business_day,
    ruonia_history,
    seasoned_ois,
    seasoned_request,
)

from rub_fixed_income import (
    RISK_BUCKETS,
    AccountingCategory,
    FixingHistory,
    InterestRateSwap,
    InvalidTerms,
    KeyRateForwardCurve,
    KeyRateForwardSegment,
    MissingMarketData,
    PortfolioRiskResultV3,
    Position,
    SwapPeriod,
    SwapRiskEngine,
    SwapRiskRequest,
    SwapScenario,
    from_dict,
    horizon_portfolio,
    swap_schedule,
    to_dict,
    value_portfolio,
)
from rub_fixed_income.portfolio_instruments import SwapInstrument

VALUATION = date(2026, 11, 2)


def df(start: date, end: date, rate: float = R_DISC) -> float:
    return math.exp(-rate * (end - start).days / 365.0)


def latest_business_day(day: date) -> date:
    while not is_business_day(day):
        day -= timedelta(days=1)
    return day


def float_leg_pv(
    swap: InterestRateSwap,
    periods: list[tuple[date, date, date]],
    history: FixingHistory | None,
) -> float:
    """Receiver-of-float PV of a RUONIA leg from the formulas (ACT/365F, flat curves)."""
    fixings = {} if history is None else dict(history.fixings)
    pv = 0.0
    for start, end, pay in periods:
        days_by_observation: dict[date, int] = {}
        day = start
        while day < min(VALUATION, end):
            observation = latest_business_day(day)
            days_by_observation[observation] = days_by_observation.get(observation, 0) + 1
            day += timedelta(days=1)
        growth = math.prod(1 + fixings[o] * n / 365 for o, n in days_by_observation.items())
        growth *= math.exp(R_PROJ * (end - max(start, VALUATION)).days / 365)
        amount = NOTIONAL * (growth - 1 + swap.float_spread * (end - start).days / 365)
        pv += amount * df(VALUATION, pay)
    return pv


def fixed_leg_pv(swap: InterestRateSwap, periods: list[tuple[date, date, date]]) -> float:
    """Receiver-of-fixed PV of the fixed leg (ACT/365F, flat discount curve)."""
    return math.fsum(
        NOTIONAL * swap.fixed_rate * (end - start).days / 365 * df(VALUATION, pay)
        for start, end, pay in periods
    )


def request(swap: InterestRateSwap, history: FixingHistory | None = None) -> SwapRiskRequest:
    return SwapRiskRequest(
        swap, CALENDAR, flat(R_DISC, VALUATION), flat(R_PROJ, VALUATION), history
    )


def remaining(periods: tuple[SwapPeriod, ...]) -> list[tuple[date, date, date]]:
    return [
        (p.accrual_start, p.accrual_end, p.payment_date)
        for p in periods
        if p.payment_date > VALUATION
    ]


# --- common dates: unchanged -----------------------------------------------------------


def test_common_dates_keep_results_and_identities() -> None:
    """Pinned before the leg dates existed: PV, legs, par rate, DV01 and base_id."""
    engine = SwapRiskEngine(seasoned_request())
    valuation = engine.valuation()
    assert engine.base_id == "ae2ebd781e0bd230cddde0b781c23a46988478689fb995682e498e20032c234e"
    assert valuation.pv_rub == pytest.approx(-101163078.95744203, rel=1e-14)
    assert valuation.fixed_leg_pv_rub == pytest.approx(-276388980.15507704, rel=1e-14)
    assert valuation.float_leg_pv_rub == pytest.approx(175225901.19763497, rel=1e-14)
    assert valuation.par_rate == pytest.approx(0.0963654085162746, rel=1e-13)
    assert engine.risk().dv01_rub == pytest.approx(119889.36502411962, rel=1e-10)
    swap = seasoned_ois()
    assert swap.common_dates
    assert "float_effective_date" not in to_dict(swap)


def test_leg_dates_equal_to_the_common_dates_are_the_common_swap() -> None:
    swap = seasoned_ois()
    explicit = replace(
        swap,
        fixed_effective_date=swap.effective_date,
        fixed_termination_date=swap.termination_date,
        float_effective_date=swap.effective_date,
        float_termination_date=swap.termination_date,
    )
    assert explicit == swap
    assert explicit.float_termination_date is None
    assert to_dict(explicit) == to_dict(swap)
    base = SwapRiskEngine(seasoned_request())
    same = SwapRiskEngine(replace(seasoned_request(), swap=explicit))
    assert same.base_id == base.base_id
    assert same.valuation() == base.valuation()


# --- legs ending one day apart ---------------------------------------------------------


def one_day_apart() -> InterestRateSwap:
    # Fixed leg to Monday 17 January 2028, float leg to Tuesday 18 January 2028.
    return seasoned_ois(
        termination_date=date(2028, 1, 17), float_termination_date=date(2028, 1, 18)
    )


# Remaining periods (accrual start, accrual end, payment), modified following, +1 day.
# Fixed: annual, rolled back from 17 January 2028 (the initial stub to 19 January 2026 is paid).
ONE_DAY_FIXED = [
    (date(2026, 1, 19), date(2027, 1, 18), date(2027, 1, 19)),
    (date(2027, 1, 18), date(2028, 1, 17), date(2028, 1, 18)),
]
# Float: quarterly, rolled back from 18 January 2028.
ONE_DAY_FLOAT = [
    (date(2026, 10, 19), date(2027, 1, 18), date(2027, 1, 19)),
    (date(2027, 1, 18), date(2027, 4, 19), date(2027, 4, 20)),
    (date(2027, 4, 19), date(2027, 7, 19), date(2027, 7, 20)),
    (date(2027, 7, 19), date(2027, 10, 18), date(2027, 10, 19)),
    (date(2027, 10, 18), date(2028, 1, 18), date(2028, 1, 19)),
]


def test_legs_ending_one_day_apart_roll_their_own_schedules() -> None:
    swap = one_day_apart()
    assert not swap.common_dates
    assert swap.leg_dates("fixed") == (date(2026, 1, 15), date(2028, 1, 17))
    assert swap.leg_dates("float") == (date(2026, 1, 15), date(2028, 1, 18))
    schedule = swap_schedule(swap, CALENDAR)
    assert remaining(schedule.fixed) == ONE_DAY_FIXED
    assert remaining(schedule.floating) == ONE_DAY_FLOAT
    # Each leg is the leg of a common-date swap on that leg's dates: nothing is aligned.
    fixed_only = swap_schedule(seasoned_ois(termination_date=date(2028, 1, 17)), CALENDAR)
    float_only = swap_schedule(seasoned_ois(termination_date=date(2028, 1, 18)), CALENDAR)
    assert schedule.fixed == fixed_only.fixed
    assert schedule.floating == float_only.floating


def test_legs_ending_one_day_apart_match_a_hand_valuation() -> None:
    history = ruonia_history(VALUATION)
    swap = one_day_apart()
    valuation = SwapRiskEngine(request(swap, history)).valuation()
    fixed = -fixed_leg_pv(swap, ONE_DAY_FIXED)  # payer pays fixed
    floating = float_leg_pv(swap, ONE_DAY_FLOAT, history)
    assert valuation.fixed_leg_pv_rub == pytest.approx(fixed, rel=1e-12)
    assert valuation.float_leg_pv_rub == pytest.approx(floating, rel=1e-12)
    assert valuation.pv_rub == pytest.approx(fixed + floating, rel=1e-12)
    current = next(c for c in valuation.cashflows if c.leg == "float")
    assert (current.source, current.known_days, current.projected_days) == (
        "partially_projected",
        14,
        77,
    )
    # Per leg, the PV is that of the common-date swap on the leg's own dates.
    on_fixed = SwapRiskEngine(
        request(seasoned_ois(termination_date=date(2028, 1, 17)), history)
    ).valuation()
    on_float = SwapRiskEngine(
        request(seasoned_ois(termination_date=date(2028, 1, 18)), history)
    ).valuation()
    assert valuation.fixed_leg_pv_rub == on_fixed.fixed_leg_pv_rub
    assert valuation.float_leg_pv_rub == on_float.float_leg_pv_rub
    assert valuation.accrued_fixed_rub == on_fixed.accrued_fixed_rub
    assert valuation.accrued_float_rub == on_float.accrued_float_rub
    assert valuation.annuity == on_fixed.annuity


# --- a float leg starting a month after the fixed leg ----------------------------------


def split_start() -> InterestRateSwap:
    # Fixed leg from 15 October 2026 (started); float leg from 16 November 2026 (not yet).
    return seasoned_ois(
        effective_date=date(2026, 10, 15),
        termination_date=date(2027, 10, 15),
        float_effective_date=date(2026, 11, 16),
    )


FORWARD_FLOAT = [
    (date(2026, 11, 16), date(2027, 1, 15), date(2027, 1, 18)),
    (date(2027, 1, 15), date(2027, 4, 15), date(2027, 4, 16)),
    (date(2027, 4, 15), date(2027, 7, 15), date(2027, 7, 16)),
    (date(2027, 7, 15), date(2027, 10, 15), date(2027, 10, 18)),
]


def test_forward_starting_float_leg_needs_no_fixings() -> None:
    swap = split_start()
    schedule = swap_schedule(swap, CALENDAR)
    assert remaining(schedule.fixed) == [
        (date(2026, 10, 15), date(2027, 10, 15), date(2027, 10, 18))
    ]
    assert remaining(schedule.floating) == FORWARD_FLOAT
    valuation = SwapRiskEngine(request(swap)).valuation()  # no fixing history
    fixed = -fixed_leg_pv(swap, [(date(2026, 10, 15), date(2027, 10, 15), date(2027, 10, 18))])
    floating = float_leg_pv(swap, FORWARD_FLOAT, None)
    assert valuation.fixed_leg_pv_rub == pytest.approx(fixed, rel=1e-12)
    assert valuation.float_leg_pv_rub == pytest.approx(floating, rel=1e-12)
    assert valuation.pv_rub == pytest.approx(fixed + floating, rel=1e-12)
    flows = [c for c in valuation.cashflows if c.leg == "float"]
    assert {c.source for c in flows} == {"projected"}
    assert all(c.known_days == 0 for c in flows)
    # The fixed leg has accrued since its own start; the float leg nothing.
    fixed_accrued = NOTIONAL * swap.fixed_rate * (VALUATION - date(2026, 10, 15)).days / 365
    assert valuation.accrued_fixed_rub == pytest.approx(-fixed_accrued, rel=1e-14)
    assert valuation.accrued_float_rub == 0.0
    # A fixing history changes nothing for a leg that has not started.
    with_history = SwapRiskEngine(request(swap, ruonia_history(VALUATION))).valuation()
    assert with_history.float_leg_pv_rub == valuation.float_leg_pv_rub
    assert with_history.fixed_leg_pv_rub == valuation.fixed_leg_pv_rub


def test_float_leg_started_before_the_fixed_leg_reads_its_fixings() -> None:
    swap = seasoned_ois(
        effective_date=date(2026, 11, 16),
        termination_date=date(2027, 10, 15),
        float_effective_date=date(2026, 10, 15),
    )
    with pytest.raises(MissingMarketData, match="has started"):
        SwapRiskEngine(request(swap))
    history = ruonia_history(VALUATION)
    valuation = SwapRiskEngine(request(swap, history)).valuation()
    floating = [
        (date(2026, 10, 15), date(2027, 1, 15), date(2027, 1, 18)),
        *FORWARD_FLOAT[1:],
    ]
    assert valuation.float_leg_pv_rub == pytest.approx(
        float_leg_pv(swap, floating, history), rel=1e-12
    )
    assert valuation.accrued_fixed_rub == 0.0
    assert valuation.accrued_float_rub > 0.0


def test_split_swap_risk_scenarios_and_carry_are_consistent() -> None:
    engine = SwapRiskEngine(request(split_start()))
    risk = engine.risk()
    for curve in risk.curves:
        assert math.fsum(b.dv01_rub for b in curve.buckets) == pytest.approx(
            curve.parallel_dv01_rub, rel=1e-6
        )
    assert risk.dv01_rub > 0.0  # a payer gains when rates rise
    shocked = engine.reprice(SwapScenario.parallel(25.0, -10.0))
    assert shocked.discount_pnl_rub + shocked.projection_pnl_rub + shocked.interaction_pnl_rub == (
        pytest.approx(shocked.total_pnl_rub, rel=1e-12)
    )
    second = engine.second_order()
    assert len(second.gradient_rub_per_bp) == 2 * len(RISK_BUCKETS)
    carry = engine.horizon(date(2026, 12, 15))  # the float leg starts before the horizon
    assert carry.carry_rub == pytest.approx(
        carry.pv_forward_rub + carry.received_rub - carry.pv_start_rub, rel=1e-12
    )
    assert carry.horizon_pnl_rub == pytest.approx(
        carry.pv_rolled_rub + carry.received_rub - carry.pv_start_rub, rel=1e-12
    )


def test_key_rate_swap_with_a_forward_starting_float_leg() -> None:
    valuation_date = date(2026, 11, 3)
    swap = InterestRateSwap(
        "KRS-SPLIT",
        "receiver",
        NOTIONAL,
        0.16,
        date(2026, 10, 15),
        date(2027, 10, 15),
        KRS,
        float_effective_date=date(2026, 12, 15),
    )
    curve = KeyRateForwardCurve(
        valuation_date,
        0.165,
        date(2026, 12, 22),
        (KeyRateForwardSegment(date(2026, 12, 22), date(2028, 1, 1), 0.15, "1Y"),),
    )
    engine = SwapRiskEngine(SwapRiskRequest(swap, CALENDAR, flat(R_DISC, valuation_date), curve))
    valuation = engine.valuation()
    floats = [c for c in valuation.cashflows if c.leg == "float"]
    assert floats[0].accrual_start == date(2026, 12, 15)
    assert {c.source for c in floats} == {"projected"}
    # The first float period, 15 December 2026 to 15 January 2027 (stub), from the curve.
    first = floats[0]
    days = (first.accrual_end - first.accrual_start).days
    average = (7 * 0.165 + (days - 7) * 0.15) / days
    assert first.rate == pytest.approx(average, rel=1e-12)
    assert engine.risk().dv01_rub != 0.0


# --- validation and serialization ------------------------------------------------------


def test_leg_date_validation_and_round_trip() -> None:
    with pytest.raises(InvalidTerms, match="float leg termination"):
        seasoned_ois(float_effective_date=date(2028, 2, 1))
    with pytest.raises(InvalidTerms, match="fixed leg termination"):
        seasoned_ois(fixed_termination_date=date(2025, 12, 1))
    with pytest.raises(TypeError, match="float_termination_date"):
        seasoned_ois(float_termination_date="2028-01-18")
    with pytest.raises(InvalidTerms, match="leg must be"):
        seasoned_ois().leg_dates("both")  # type: ignore[arg-type]
    for swap in (one_day_apart(), split_start()):
        data = to_dict(swap)
        assert from_dict(InterestRateSwap, data) == swap
    assert to_dict(split_start())["float_effective_date"] == "2026-11-16"
    first = SwapRiskEngine(request(one_day_apart(), ruonia_history(VALUATION)))
    common = SwapRiskEngine(
        request(seasoned_ois(termination_date=date(2028, 1, 17)), ruonia_history(VALUATION))
    )
    assert first.base_id != common.base_id


# --- portfolio path --------------------------------------------------------------------


def test_portfolio_values_and_carries_swaps_with_their_own_leg_dates() -> None:
    def swap(
        swap_id: str, end: date = date(2028, 1, 17), float_end: date | None = None
    ) -> InterestRateSwap:
        return InterestRateSwap(
            swap_id,
            "payer",
            1_000_000_000.0,
            0.152,
            date(2026, 1, 15),
            end,
            ois_swap_convention(),
            float_spread=0.0025,
            float_termination_date=float_end,
        )

    terms = {
        "SPLIT": swap("SPLIT", float_end=date(2028, 1, 18)),
        "FIXED-DATES": swap("FIXED-DATES"),
        "FLOAT-DATES": swap("FLOAT-DATES", date(2028, 1, 18)),
    }
    instruments = tuple(
        SwapInstrument(name, terms[name], "MOEX", "RUB_OIS", fixing_id="RUONIA") for name in terms
    )
    positions = tuple(
        Position(f"p-{name}", name, 1.0, "DESK", AccountingCategory.TRADING, "RUB")
        for name in terms
    )
    result = value_portfolio(
        run_id="leg-dates",
        as_of=AS_OF,
        snapshot=snapshot(),
        instruments=instruments,
        positions=positions,
        policy=POLICY,
    )
    assert isinstance(result, PortfolioRiskResultV3)
    rows = {row.instrument_id: row for row in result.swaps}
    assert rows["SPLIT"].fixed_leg_pv_rub == rows["FIXED-DATES"].fixed_leg_pv_rub
    assert rows["SPLIT"].float_leg_pv_rub == rows["FLOAT-DATES"].float_leg_pv_rub
    assert rows["SPLIT"].pv_rub == pytest.approx(
        rows["FIXED-DATES"].fixed_leg_pv_rub + rows["FLOAT-DATES"].float_leg_pv_rub, rel=1e-12
    )
    direct = SwapRiskEngine(
        SwapRiskRequest(
            terms["SPLIT"],
            BOOK_CAL,
            snapshot().curve("RUB_OIS").curve,  # type: ignore[arg-type]
            None,
            book_ruonia_history(),
        )
    )
    assert rows["SPLIT"].pv_rub == direct.base_pv
    carried = horizon_portfolio(
        snapshot=snapshot(),
        instruments=instruments,
        positions=positions,
        policy=POLICY,
        horizon_date=date(2026, 11, 16),
    )
    split = next(r for r in carried.swaps if r.position_id == "p-SPLIT")
    assert split.per_trade == direct.horizon(date(2026, 11, 16))
