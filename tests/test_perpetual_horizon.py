"""Horizon P&L of a never-called perpetual (0.41.0, BACKLOG #126, issue #27).

Carry and rolldown price the tail at each settlement, exactly as ``value_portfolio``
prices it; they reconcile with a full revaluation of the materialised coupon stream at
the horizon, and the assumed-book wrappers value the perpetual natively.
"""

from __future__ import annotations

import math
from dataclasses import replace
from datetime import date

import pytest
from test_assumed_portfolio import book_positions, book_snapshot, perpetual
from test_perpetual import BOOK, NEVER, POSITIONS, kbd, materialised, native, perp
from test_portfolio import AS_OF, POLICY

from rub_fixed_income import (
    CONTINUOUS,
    AssumptionPolicy,
    BusinessCalendar,
    DiscountCurve,
    ExerciseAssumption,
    Instrument,
    InstrumentAssumption,
    InvalidTerms,
    Offer,
    OfferKind,
    UnsupportedProduct,
    ZSpreadConvention,
    exercised_instrument,
    explain_horizon_pnl,
    horizon_assumed_portfolio,
    horizon_portfolio,
    horizon_valuation,
    price_cashflows,
    resolve_exercise,
    roll_curve,
    value_assumed_portfolio,
    verify_assumed_result,
)
from rub_fixed_income.perpetual import PerpetualTail

ANNUAL = ZSpreadConvention()
CALENDAR = BusinessCalendar(version="weekdays")
CURVE = kbd()  # the KBD of 2026-10-20
START = CURVE.curve_date


def never(bond: Instrument) -> tuple[Instrument, PerpetualTail]:
    settlement = CALENDAR.add_business_days(START, bond.settlement_lag_days)
    resolved = resolve_exercise(bond, settlement, ExerciseAssumption.never_called())
    exercised = exercised_instrument(bond, resolved)
    assert exercised.perpetual_tail is not None
    return exercised.instrument, exercised.perpetual_tail


def stream(bond: Instrument, tail: PerpetualTail, after: date) -> tuple[tuple[date, float], ...]:
    """Listed coupons and 250 years of tail coupons paid after ``after``, as plain flows."""
    listed = tuple((f.payment_date, f.amount) for f in bond.cashflows(after=after))
    return tuple((d, a) for d, a in (*listed, *materialised(tail, 250)) if d > after)


def revalue(
    flows: tuple[tuple[date, float], ...], curve: DiscountCurve, bp: float, at: date
) -> float:
    return price_cashflows(flows, curve, spread=bp * 1e-4, settlement_date=at, convention=ANNUAL)


@pytest.mark.parametrize("horizon", [date(2026, 12, 15), date(2027, 10, 20), date(2030, 1, 15)])
def test_carry_and_rolldown_reconcile_with_the_materialised_stream(horizon: date) -> None:
    bond, tail = never(perp())
    result = horizon_valuation(
        bond, CURVE, CALENDAR, horizon_date=horizon, dirty_price_rub=880.0, perpetual_tail=tail
    )
    assert (result.redemption_horizon, result.redemption_date) == ("perpetual", tail.anchor_date)
    s0, s1 = result.settlement_date, result.horizon_settlement_date
    spread = result.spread_bp
    assert result.pv_start_rub == pytest.approx(880.0, rel=1e-12)
    assert revalue(stream(bond, tail, s0), CURVE, spread, s0) == pytest.approx(880.0, rel=1e-10)
    # Today's forwards realised: the stream after the horizon settlement on today's curve.
    later = stream(bond, tail, s1)
    assert result.pv_forward_rub == pytest.approx(revalue(later, CURVE, spread, s1), rel=1e-10)
    rolled = roll_curve(CURVE, horizon)
    assert result.pv_rolled_rub == pytest.approx(revalue(later, rolled, spread, s1), rel=1e-10)
    # Coupons received: the listed ones and, past the anchor, the tail's.
    paid = [a for d, a in stream(bond, tail, s0) if d <= s1]
    assert result.coupons_received_rub == pytest.approx(sum(paid), abs=1e-9)
    assert result.principal_received_rub == 0.0
    assert result.carry_rub == pytest.approx(
        result.pv_forward_rub + result.received_rub - result.pv_start_rub, abs=1e-9
    )
    assert result.rolldown_rub == pytest.approx(result.pv_rolled_rub - result.pv_forward_rub)
    if horizon > tail.anchor_date:
        assert sum(1 for d, _ in materialised(tail, 1) if s0 < d <= s1) == 2
        assert result.accrued_end_rub is None  # the terms list no coupon for that date
    else:
        assert result.accrued_end_rub == bond.accrued(s1)


def test_flat_curve_carry_is_the_spread_and_rate_return_without_rolldown() -> None:
    """Flat continuous curve and spread: every flow after S1 grows by exp((r + s) h)."""
    curve = DiscountCurve.flat(START, 0.14)
    bond, tail = never(perp())
    continuous = ZSpreadConvention(compounding=CONTINUOUS)
    result = horizon_valuation(
        bond,
        curve,
        CALENDAR,
        horizon_date=date(2027, 4, 20),
        spread_bp=300.0,
        spread_convention=continuous,
        perpetual_tail=tail,
    )
    s0, s1 = result.settlement_date, result.horizon_settlement_date
    h = (s1 - s0).days / 365.0
    growth = math.exp((0.14 + 0.03) * h)
    remaining_at_start = result.pv_start_rub - sum(
        a * math.exp(-0.17 * (d - s0).days / 365.0) for d, a in stream(bond, tail, s0) if d <= s1
    )
    assert result.pv_forward_rub == pytest.approx(remaining_at_start * growth, rel=1e-11)
    assert result.rolldown_rub == pytest.approx(0.0, abs=1e-8)


def test_explain_reconciles_with_a_full_revaluation_at_the_end_market() -> None:
    bond, tail = never(perp())
    horizon = date(2027, 4, 20)
    end = DiscountCurve.flat(horizon, 0.13)
    explain = explain_horizon_pnl(
        bond,
        CURVE,
        end,
        CALENDAR,
        start_dirty_price_rub=880.0,
        end_spread_bp=420.0,
        perpetual_tail=tail,
    )
    at = explain.horizon.horizon_settlement_date
    assert explain.pv_end_rub == pytest.approx(
        revalue(stream(bond, tail, at), end, 420.0, at), rel=1e-10
    )
    assert explain.total_rub == pytest.approx(
        explain.pv_end_rub + explain.horizon.received_rub - explain.horizon.pv_start_rub, abs=1e-9
    )
    parts = explain.carry_rub + explain.rolldown_rub + explain.rates_rub + explain.spread_rub
    assert parts + explain.residual_rub == pytest.approx(explain.total_rub, abs=1e-9)


def test_a_put_the_policy_picks_is_the_horizon_and_a_tail_needs_a_perpetual() -> None:
    put = Offer(date(2027, 12, 1), OfferKind.PUT, True)
    bond, tail = never(perp(offers=(*perpetual().offers, put)))
    result = horizon_valuation(
        bond,
        CURVE,
        CALENDAR,
        horizon_date=date(2026, 12, 15),
        dirty_price_rub=990.0,
        redemption_horizon="confirmed_put",
        perpetual_tail=tail,
    )
    assert (result.redemption_horizon, result.redemption_date) == ("confirmed_put", put.offer_date)
    with pytest.raises(UnsupportedProduct, match="never-called tail"):
        horizon_valuation(bond, CURVE, CALENDAR, horizon_date=date(2026, 12, 15), spread_bp=0.0)
    dated = BOOK[0]
    with pytest.raises(InvalidTerms, match="perpetual bond only"):
        horizon_valuation(
            dated,
            CURVE,
            CALENDAR,
            horizon_date=date(2026, 12, 15),
            spread_bp=0,
            perpetual_tail=tail,
        )


def test_horizon_portfolio_carries_the_never_called_perpetual() -> None:
    horizon = horizon_portfolio(
        snapshot=book_snapshot(),
        instruments=BOOK,
        positions=POSITIONS,
        policy=NEVER,
        horizon_date=date(2026, 12, 15),
    )
    assert horizon.coverage.status == "complete", horizon.coverage.rejections
    rows = {r.position_id: r for r in horizon.positions}
    row = rows["p-perp"]
    assert row.per_bond.redemption_horizon == "perpetual"
    assert row.per_bond.redemption_date == date(2029, 6, 1)
    valued = {v.position_id: v for v in native(NEVER).positions}["p-perp"]
    # The start value is the native valuation's dirty price, its tail included.
    assert row.per_bond.pv_start_rub == pytest.approx(valued.dirty_price_rub, rel=1e-10)
    assert row.per_bond.spread_bp == pytest.approx(valued.spread_bp, abs=1e-6)
    assert row.carry_rub == pytest.approx(50 * row.per_bond.carry_rub, rel=1e-12)
    assert row.per_bond.carry_rub > 0.0


def test_the_assumed_book_values_and_carries_a_never_called_perpetual_natively() -> None:
    assumptions = AssumptionPolicy(
        overrides=(InstrumentAssumption("PERP", exercise=ExerciseAssumption.never_called()),)
    )
    book = (*BOOK[:-1], perpetual())
    common = {
        "snapshot": book_snapshot(),
        "instruments": book,
        "positions": book_positions()[:5],
        "policy": replace(
            POLICY, exercise_assumptions=(("CORP-CALL", ExerciseAssumption.never_called()),)
        ),
        "assumptions": assumptions,
    }
    wrapped = value_assumed_portfolio(run_id="eod-2026-10-15", as_of=AS_OF, **common)  # type: ignore[arg-type]
    assert wrapped.status == "complete", wrapped.coverage.rejections
    assert verify_assumed_result(wrapped)
    use = next(u for u in wrapped.assumptions if u.instrument_id == "PERP")
    assert (use.status, use.exercise, use.redemption_date) == ("applied", "never", None)
    row = {v.position_id: v for v in wrapped.portfolio.positions}["p-perp"]
    assert row.horizon == "perpetual"
    assert row.exercise_assumption == "exercise:never_called@never"
    carried = horizon_assumed_portfolio(horizon_date=date(2026, 12, 15), **common)  # type: ignore[arg-type]
    assert carried.coverage.status == "complete", carried.coverage.rejections
    per_bond = {r.position_id: r for r in carried.horizon.positions}["p-perp"].per_bond
    assert per_bond.redemption_horizon == "perpetual"
    assert per_bond.pv_start_rub == pytest.approx(row.dirty_price_rub, rel=1e-10)


def test_the_tail_must_continue_the_listed_coupons() -> None:
    bond, tail = never(perp())
    horizon = date(2026, 12, 15)
    early = replace(tail, anchor_date=date(2028, 6, 1))  # would drop listed coupons
    with pytest.raises(InvalidTerms, match="last listed coupon"):
        horizon_valuation(
            bond, CURVE, CALENDAR, horizon_date=horizon, spread_bp=300.0, perpetual_tail=early
        )
    floating = replace(tail, amount=None)  # a fixed leg's tail has its amount
    with pytest.raises(InvalidTerms, match="amount=None"):
        horizon_valuation(
            bond, CURVE, CALENDAR, horizon_date=horizon, spread_bp=300.0, perpetual_tail=floating
        )
