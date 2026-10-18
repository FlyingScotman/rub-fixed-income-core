"""Repo and reverse-repo legs (package 6).

Independent checks: contract amounts and PVs against reconstructions written here
from the MOEX / NFA formulas, the day basis on a leap-year boundary, risk signs and
closed forms on flat curves, bucket sums, bump-size convergence, Hessian and Taylor
consistency, exact scenarios, carry, the collateral reference (never in the PV),
typed rejections, identities and serialization. MOEX-published amounts are in
``tests/test_moex_repo_goldens.py``.
"""

from __future__ import annotations

import math
from dataclasses import replace
from datetime import date, timedelta

import pytest

from rub_fixed_income import (
    RISK_BUCKETS,
    BusinessCalendar,
    CurveBootstrapRequest,
    DayCount,
    DiscountCurve,
    EventKey,
    FinancedBondRequest,
    FixedRepoRate,
    FixingHistory,
    FloatingRepoRate,
    InvalidTerms,
    MissingMarketData,
    OisQuote,
    RepoCashAdjustment,
    RepoCollateral,
    RepoDayBasis,
    RepoRiskEngine,
    RepoRiskRequest,
    RepoScenario,
    RepoTrade,
    ShockedCurveCache,
    UnsupportedProduct,
    constant_daily_accumulation_periods,
    from_dict,
    project_financed_bond,
    repo_carry,
    repo_end_amount,
    repo_factor_risk,
    repo_initial_discount_pct,
    repo_start_amount,
    to_dict,
)

HOLIDAYS = frozenset({date(2026, 11, 4), date(2027, 1, 1), date(2027, 1, 7)})
CALENDAR = BusinessCalendar(holidays=HOLIDAYS, version="synthetic-repo-test")
VALUATION = date(2026, 10, 19)  # Monday
RATE = 0.15
FIXED = FixedRepoRate(0.16, RepoDayBasis.ACT_365_366)
FLOAT = FloatingRepoRate(
    "RUSFAR",
    0.002,
    RepoDayBasis.ACT_365_366,
    "previous_settlement_day",
    DayCount.ACT_365_FIXED,
)


def _history(valuation: date = VALUATION, start: date = date(2026, 9, 1)) -> FixingHistory:
    rows = []
    day = start
    level = 0.14
    while day < valuation:
        if CALENDAR.is_business_day(day):
            rows.append((day, level))
            level += 0.0001
        day += timedelta(days=1)
    return FixingHistory("ruonia", valuation, tuple(rows), CALENDAR, "synthetic-rusfar")


def _trade(**changes: object) -> RepoTrade:
    base = RepoTrade(
        "R1",
        "reverse_repo",
        date(2026, 10, 12),
        date(2026, 11, 12),
        100_000_000.0,
        FIXED,
        2,
        RepoCollateral("SU26238RMFS4", 160_000.0, initial_discount_pct=10.0),
    )
    return replace(base, **changes)  # type: ignore[arg-type]


def _request(trade: RepoTrade, **changes: object) -> RepoRiskRequest:
    fixings = _history() if isinstance(trade.rate, FloatingRepoRate) else None
    base: dict[str, object] = {
        "trade": trade,
        "calendar": CALENDAR,
        "discount_curve": DiscountCurve.flat(VALUATION, RATE),
        "fixings": fixings,
    }
    base.update(changes)
    return RepoRiskRequest(**base)  # type: ignore[arg-type]


def _engine(trade: RepoTrade, **changes: object) -> RepoRiskEngine:
    return RepoRiskEngine(_request(trade, **changes))


def _df(day: date, rate: float = RATE, valuation: date = VALUATION) -> float:
    return math.exp(-rate * (day - valuation).days / 365.0)


# --- contract amounts -------------------------------------------------------------


def test_day_basis_on_a_leap_year_boundary() -> None:
    start, end = date(2027, 12, 15), date(2028, 1, 14)
    # Days of (start, end]: 16-31 December 2027 (16) and 1-14 January 2028 (14).
    assert RepoDayBasis.ACT_365_366.year_fraction(start, end) == pytest.approx(
        16 / 365 + 14 / 366, abs=1e-15
    )
    # ACT/ACT ISDA counts [start, end): 17 days in 2027 and 13 in 2028.
    assert DayCount.ACT_ACT_ISDA.year_fraction(start, end) == pytest.approx(17 / 365 + 13 / 366)
    assert RepoDayBasis.ACT_365_FIXED.year_fraction(start, end) == 30 / 365
    assert RepoDayBasis.ACT_360.year_fraction(start, end) == 30 / 360
    weights = math.fsum(
        RepoDayBasis.ACT_365_366.day_weight(start + timedelta(days=k)) for k in range(1, 31)
    )
    assert weights == pytest.approx(RepoDayBasis.ACT_365_366.year_fraction(start, end))
    assert RepoDayBasis.ACT_365_366.year_fraction(date(2027, 12, 31), date(2028, 1, 1)) == 1 / 366
    assert RepoDayBasis.ACT_365_366.year_fraction(date(2028, 12, 30), date(2028, 12, 31)) == 1 / 366


def test_fixed_end_amount_moex_formula() -> None:
    # S2 = S1 * (1 + R / 100 * (T365 / 365 + T366 / 366)), MOEX Part III 1.5.8.5.
    s2 = repo_end_amount(
        50_000_000.0,
        0.155,
        date(2027, 12, 15),
        date(2028, 1, 14),
        RepoDayBasis.ACT_365_366,
        decimals=2,
    )
    assert s2 == round(50_000_000.0 * (1 + 0.155 * (16 / 365 + 14 / 366)), 2)
    unrounded = repo_end_amount(
        1.0, 0.1, date(2026, 1, 1), date(2026, 1, 8), RepoDayBasis.ACT_360, decimals=None
    )
    assert unrounded == 1.0 + 0.1 * 7 / 360
    with pytest.raises(InvalidTerms):
        repo_end_amount(
            1.0, 0.1, date(2026, 1, 8), date(2026, 1, 8), RepoDayBasis.ACT_360, decimals=2
        )


def test_start_amount_and_discount_round_trip() -> None:
    # S = (1 - D / 100) * Q * (P0 + a0), MOEX Part III 1.5.8.2.
    quantity, dirty = 160_000.0, 702.1734
    amount = repo_start_amount(quantity, dirty, 10.0)
    assert amount == round(0.9 * quantity * dirty, 2)
    assert repo_initial_discount_pct(amount, quantity, dirty) == pytest.approx(10.0, abs=1e-8)
    with pytest.raises(InvalidTerms):
        repo_start_amount(quantity, dirty, 100.0)


def test_fixed_valuation_reconstruction_and_sides() -> None:
    trade = _trade()
    end_amount = round(100_000_000.0 * (1 + 0.16 * 31 / 365), 2)
    engine = _engine(trade)
    valuation = engine.valuation()
    assert valuation.end_amount_rub == end_amount
    assert valuation.end_amount_source == "fixed"
    assert valuation.pv_rub == pytest.approx(end_amount * _df(date(2026, 11, 12)), rel=1e-14)
    assert valuation.start_leg_pv_rub == 0.0
    # Accrued: 7 days (13..19 October) of the 31.
    accrued = round(100_000_000.0 * (1 + 0.16 * 7 / 365), 2) - 100_000_000.0
    assert valuation.accrued_interest_rub == pytest.approx(accrued, abs=1e-6)
    assert valuation.current_repurchase_amount_rub == pytest.approx(100_000_000.0 + accrued)
    assert valuation.repurchase_price_rub_per_bond == pytest.approx(end_amount / 160_000.0)
    borrower = _engine(replace(trade, side="repo")).valuation()
    assert borrower.pv_rub == -valuation.pv_rub
    assert borrower.end_amount_rub == valuation.end_amount_rub
    assert borrower.cashflows[0].amount_rub == -end_amount


def test_forward_start_and_adjustments() -> None:
    adjustments = (
        RepoCashAdjustment(date(2026, 11, 2), 1_500_000.0, "margin_call"),
        RepoCashAdjustment(date(2026, 11, 20), -2_000_000.0, "income_compensation"),
    )
    trade = _trade(
        start_date=date(2026, 10, 26),
        end_date=date(2026, 11, 26),
        cash_adjustments=adjustments,
    )
    end_amount = round(100_000_000.0 * (1 + 0.16 * 31 / 365), 2)
    expected = (
        -100_000_000.0 * _df(date(2026, 10, 26))
        + end_amount * _df(date(2026, 11, 26))
        + 1_500_000.0 * _df(date(2026, 11, 2))
        - 2_000_000.0 * _df(date(2026, 11, 20))
    )
    valuation = _engine(trade).valuation()
    assert valuation.pv_rub == pytest.approx(expected, rel=1e-13)
    assert [row.kind for row in valuation.cashflows] == [
        "start_leg",
        "end_leg",
        "adjustment",
        "adjustment",
    ]
    assert valuation.accrued_interest_rub == 0.0
    assert valuation.adjustments_pv_rub == pytest.approx(
        1_500_000.0 * _df(date(2026, 11, 2)) - 2_000_000.0 * _df(date(2026, 11, 20))
    )
    # A settled adjustment no longer counts.
    settled = _trade(cash_adjustments=(RepoCashAdjustment(date(2026, 10, 15), 9e6, "margin_call"),))
    assert _engine(settled).valuation().pv_rub == _engine(_trade()).valuation().pv_rub


def _float_reconstruction(trade: RepoTrade, curve_rate: float) -> tuple[float, float]:
    """Simple floating S2 written from the MOEX formula, and the known interest."""
    history = _history()
    known_rates = dict(history.fixings)
    total, known = 0.0, 0.0
    day = trade.start_date + timedelta(days=1)
    assert trade.end_date is not None
    rate = trade.rate
    assert isinstance(rate, FloatingRepoRate)
    while day <= trade.end_date:
        settlement = day - timedelta(days=1)
        while not CALENDAR.is_business_day(settlement):
            settlement -= timedelta(days=1)
        weight = 1 / 365  # 2026 is not a leap year
        if settlement < VALUATION:
            value = known_rates[settlement] + rate.spread
            known += value * weight
        else:
            following = CALENDAR.add_business_days(settlement, 1)
            tau = (following - settlement).days / 365
            ratio = math.exp(curve_rate * (following - settlement).days / 365)
            value = (ratio - 1) / tau + rate.spread
        total += value * weight
        day += timedelta(days=1)
    return trade.start_amount_rub * (1 + total), trade.start_amount_rub * known


def test_simple_floating_single_curve_reconstruction() -> None:
    trade = _trade(rate=FLOAT)
    expected_end, expected_known = _float_reconstruction(trade, RATE)
    valuation = _engine(trade).valuation()
    assert valuation.end_amount_source == "partially_projected"
    assert valuation.end_amount_rub == pytest.approx(expected_end, rel=1e-14)
    assert valuation.known_interest_rub == pytest.approx(expected_known, rel=1e-12)
    assert valuation.pv_rub == pytest.approx(expected_end * _df(date(2026, 11, 12)), rel=1e-14)
    assert valuation.projection_curve_basis == "single_curve"
    # The accrued part up to the valuation date uses known fixings only here.
    assert valuation.current_repurchase_amount_rub == pytest.approx(
        round(100_000_000.0 + expected_known, 2)
    )


def test_two_curve_floating_reconstruction() -> None:
    trade = _trade(rate=FLOAT)
    engine = _engine(trade, projection_curve=DiscountCurve.flat(VALUATION, 0.17))
    expected_end, _ = _float_reconstruction(trade, 0.17)
    valuation = engine.valuation()
    assert valuation.end_amount_rub == pytest.approx(expected_end, rel=1e-14)
    assert valuation.pv_rub == pytest.approx(expected_end * _df(date(2026, 11, 12)), rel=1e-14)


def test_capitalised_fixed_rate_credits_on_settlement_days() -> None:
    rate = FixedRepoRate(0.16, RepoDayBasis.ACT_365_366, "capitalised")
    trade = _trade(rate=rate, start_date=date(2026, 10, 16), end_date=date(2026, 10, 21))
    balance = 100_000_000.0
    # Credits on 19 Oct (Sat, Sun, Mon: 3 days), 20 Oct and 21 Oct.
    for days in (3, 1, 1):
        balance += round(balance * 0.16 * days / 365, 2)
    valuation = _engine(trade).valuation()
    assert valuation.end_amount_rub == pytest.approx(round(balance, 2), abs=1e-9)
    simple = _engine(replace(trade, rate=FIXED)).valuation()
    assert valuation.end_amount_rub > simple.end_amount_rub


def test_nonpositive_rate_replacement() -> None:
    saturday = date(2026, 10, 17)
    history = FixingHistory(
        "ruonia",
        saturday,
        tuple((d, -0.003) for d, _ in _history(saturday).fixings),
        CALENDAR,
        "negative",
    )
    rate = replace(FLOAT, nonpositive_rate_replacement=0.0001)
    trade = _trade(rate=rate, start_date=date(2026, 10, 12), end_date=date(2026, 10, 19))
    curve = DiscountCurve.flat(saturday, RATE)
    # Every day of (12, 19 Oct] is observed before the Saturday at -0.3 % + 0.2 %.
    valuation = _engine(trade, fixings=history, discount_curve=curve).valuation()
    assert valuation.end_amount_source == "known"
    assert valuation.end_amount_rub == round(100_000_000.0 * (1 + 0.0001 * 7 / 365), 2)
    without = _engine(replace(trade, rate=FLOAT), fixings=history, discount_curve=curve)
    assert without.valuation().end_amount_rub == round(100_000_000.0 * (1 - 0.001 * 7 / 365), 2)
    projected = _trade(rate=rate, start_date=date(2026, 10, 19), end_date=date(2026, 10, 26))
    with pytest.raises(UnsupportedProduct):
        _engine(projected, discount_curve=DiscountCurve.flat(VALUATION, -0.01))


# --- risk -------------------------------------------------------------------------


def test_fixed_risk_closed_form_and_signs() -> None:
    engine = _engine(_trade())
    risk = engine.risk()
    end = date(2026, 11, 12)
    years = (end - VALUATION).days / 365
    analytic = -years * engine.base_pv * 1e-4
    assert risk.dv01_rub == pytest.approx(analytic, rel=1e-7)
    assert risk.curves[0].curve_role == "discount"
    assert risk.curves[0].parallel_dv01_rub == pytest.approx(risk.dv01_rub, rel=1e-9)
    assert sum(b.dv01_rub for b in risk.curves[0].buckets) == pytest.approx(risk.dv01_rub)
    assert risk.dv01_rub < 0.0
    borrower = _engine(_trade(side="repo")).risk()
    assert borrower.dv01_rub == pytest.approx(-risk.dv01_rub, rel=1e-12)
    # The 1-month flow sits between the 1M and 3M pillars only.
    nonzero = {b.bucket for b in risk.curves[0].buckets if b.dv01_rub != 0.0}
    assert nonzero == {"1M"}


def test_floating_two_curve_risk() -> None:
    trade = _trade(rate=FLOAT, end_date=date(2027, 1, 12))
    engine = _engine(trade, projection_curve=DiscountCurve.flat(VALUATION, 0.17))
    risk = engine.risk()
    discount, projection = risk.curves
    assert (discount.curve_role, projection.curve_role) == ("discount", "projection")
    assert discount.dv01_rub < 0.0
    assert projection.dv01_rub > 0.0  # the lender receives more interest when rates rise
    assert projection.parallel_dv01_rub == pytest.approx(projection.dv01_rub, rel=1e-6)
    borrower = _engine(
        replace(trade, side="repo"), projection_curve=DiscountCurve.flat(VALUATION, 0.17)
    ).risk()
    assert borrower.curves[1].dv01_rub == pytest.approx(-projection.dv01_rub, rel=1e-12)
    # Projected interest over ~85 days: about S1 * 85 / 365 * 1e-4 per bp at the end DF.
    end_df = _df(date(2027, 1, 12))
    projected_days = (date(2027, 1, 12) - VALUATION).days  # days 20 Oct .. 12 Jan
    assert projection.dv01_rub == pytest.approx(
        100_000_000.0 * projected_days / 365 * 1e-4 * end_df, rel=0.02
    )


def test_known_fixings_carry_no_projection_risk() -> None:
    trade = _trade(rate=FLOAT, start_date=date(2026, 10, 5), end_date=date(2026, 10, 19 + 1))
    # Every observation of (5 Oct, 20 Oct] is a settlement day before 19 Oct except
    # the one for 20 Oct (19 Oct itself), which is projected.
    engine = _engine(trade, projection_curve=DiscountCurve.flat(VALUATION, 0.17))
    projection = engine.risk().curves[1]
    one_day = 100_000_000.0 * (1 / 365) * 1e-4 * _df(date(2026, 10, 20))
    assert projection.dv01_rub == pytest.approx(one_day, rel=0.01)
    known = _trade(rate=FLOAT, start_date=date(2026, 10, 5), end_date=date(2026, 10, 19))
    with pytest.raises(InvalidTerms):
        _engine(known)  # settled on the valuation date


def test_bump_convergence_and_central_default() -> None:
    engine = _engine(_trade(rate=FLOAT, end_date=date(2027, 4, 12)))
    small = engine.risk(bump_bp=0.1).dv01_rub
    for bump in (1.0, 5.0):
        assert engine.risk(bump_bp=bump).dv01_rub == pytest.approx(small, rel=1e-4 * bump**2)
    forward = engine.risk(difference="forward")
    assert forward.difference == "forward"
    with pytest.raises(InvalidTerms):
        repo_factor_risk(forward, discount_curve_id="RUB_OIS")


def test_second_order_taylor_and_scenarios() -> None:
    trade = _trade(rate=FLOAT, end_date=date(2027, 10, 12))
    engine = _engine(trade, projection_curve=DiscountCurve.flat(VALUATION, 0.17))
    second = engine.second_order()
    assert len(second.factors) == 22
    risk = engine.risk()
    first = [b.dv01_rub for c in risk.curves for b in c.buckets]
    assert second.gradient_rub_per_bp == pytest.approx(first, rel=1e-12, abs=1e-12)
    shifts = tuple(5.0 * (i % 3 - 1) for i in range(len(RISK_BUCKETS)))
    proj = tuple(3.0 for _ in RISK_BUCKETS)
    scenario = RepoScenario(shifts, proj)
    exact = engine.reprice(scenario)
    moves = (*shifts, *proj)
    taylor = sum(g * m for g, m in zip(second.gradient_rub_per_bp, moves, strict=True))
    taylor += 0.5 * sum(
        second.hessian_rub_per_bp2[i][j] * moves[i] * moves[j] for i in range(22) for j in range(22)
    )
    assert exact.total_pnl_rub == pytest.approx(taylor, rel=1e-5)
    assert exact.total_pnl_rub == pytest.approx(
        exact.discount_pnl_rub + exact.projection_pnl_rub + exact.interaction_pnl_rub
    )
    factor = repo_factor_risk(
        risk, discount_curve_id="RUB_OIS", projection_curve_id="RUSFAR_OIS", second_order=second
    )
    assert factor.factors[0] == "IR|RUB_OIS|1M"
    assert factor.factors[11] == "IR|RUSFAR_OIS|1M"
    assert factor.has_second_order
    with pytest.raises(InvalidTerms):
        repo_factor_risk(risk, discount_curve_id="RUB_OIS")


def test_single_curve_factors_and_scenarios() -> None:
    engine = _engine(_trade())
    risk = engine.risk()
    factor = repo_factor_risk(risk, discount_curve_id="RUB_OIS")
    assert factor.factors == tuple(f"IR|RUB_OIS|{b}" for b in RISK_BUCKETS)
    assert factor.first_order == tuple(b.dv01_rub for b in risk.curves[0].buckets)
    second = engine.second_order()
    assert len(second.factors) == 11
    parallel = engine.reprice(RepoScenario.parallel(100.0))
    years = (date(2026, 11, 12) - VALUATION).days / 365
    assert parallel.shocked_pv_rub == pytest.approx(
        engine.base_pv * math.exp(-0.01 * years), rel=1e-13
    )
    with pytest.raises(InvalidTerms):
        engine.reprice(RepoScenario.parallel(0.0, 1.0))


def test_quote_basis_shares_the_curve_cache() -> None:
    quotes = tuple(
        OisQuote(tenor, VALUATION + timedelta(days=days), 0.15 + 0.001 * k)
        for k, (tenor, days) in enumerate((("1M", 31), ("3M", 92), ("6M", 182), ("1Y", 365)))
    )
    request = CurveBootstrapRequest(VALUATION, quotes)
    cache = ShockedCurveCache()
    first = RepoRiskEngine(_request(_trade(), discount_curve=request), cache)
    misses = cache.misses
    second = RepoRiskEngine(_request(_trade(trade_id="R2"), discount_curve=request), cache)
    assert cache.misses == misses
    assert first.risk().curves[0].curve_basis == "ois_quote"
    assert second.risk().dv01_rub < 0.0


# --- carry --------------------------------------------------------------------------


def test_carry_fixed() -> None:
    request = _request(_trade())
    horizon = date(2026, 10, 26)
    carry = repo_carry(request, horizon_date=horizon)
    assert carry.interest_carry_rub == pytest.approx(100_000_000.0 * 0.16 * 7 / 365, rel=1e-9)
    end_amount = round(100_000_000.0 * (1 + 0.16 * 31 / 365), 2)
    pv_forward = end_amount * _df(date(2026, 11, 12)) / _df(horizon)
    assert carry.pv_forward_rub == pytest.approx(pv_forward, rel=1e-13)
    assert carry.received_rub == 0.0
    assert carry.pv_carry_rub == pytest.approx(pv_forward - carry.pv_start_rub, rel=1e-12)
    beyond = repo_carry(request, horizon_date=date(2026, 12, 1))
    assert beyond.received_rub == pytest.approx(end_amount)
    assert beyond.accrued_horizon_rub == pytest.approx(end_amount - 100_000_000.0, abs=0.005)
    borrower = repo_carry(_request(_trade(side="repo")), horizon_date=horizon)
    assert borrower.interest_carry_rub == -carry.interest_carry_rub
    with pytest.raises(InvalidTerms):
        repo_carry(request, horizon_date=VALUATION)


def test_carry_capitalised_floating_partial_period() -> None:
    rate = replace(FLOAT, interest="capitalised")
    trade = _trade(rate=rate, start_date=date(2026, 10, 14), end_date=None)
    engine = _engine(trade, assumed_end_date=date(2026, 11, 2))
    carry = engine.carry(date(2026, 10, 24))  # a Saturday: the weekend accrues simply
    assert carry.accrued_horizon_rub > carry.accrued_start_rub > 0.0
    full = engine.carry(date(2026, 10, 26))
    assert full.accrued_horizon_rub > carry.accrued_horizon_rub


def _sloped(valuation: date = VALUATION) -> DiscountCurve:
    nodes = []
    for days, rate in ((7, 0.15), (30, 0.16), (90, 0.18), (365, 0.17)):
        nodes.append((valuation + timedelta(days=days), math.exp(-rate * days / 365)))
    return DiscountCurve(valuation, tuple(nodes))


def test_theta_fixed_rolls_the_discount_curve() -> None:
    request = _request(_trade(), discount_curve=_sloped())
    horizon = date(2026, 10, 26)
    carry = repo_carry(request, horizon_date=horizon)
    end_amount = round(100_000_000.0 * (1 + 0.16 * 31 / 365), 2)
    shift = horizon - VALUATION
    rolled_df = _sloped().discount_factor(date(2026, 11, 12) - shift)
    assert carry.pv_rolled_rub == pytest.approx(end_amount * rolled_df, rel=1e-14)
    assert carry.rolldown_rub == pytest.approx(carry.pv_rolled_rub - carry.pv_forward_rub)
    assert carry.horizon_pnl_rub == pytest.approx(carry.pv_carry_rub + carry.rolldown_rub, abs=1e-6)
    # On a flat curve rolling and reading forward agree.
    flat = repo_carry(_request(_trade()), horizon_date=horizon)
    assert flat.pv_rolled_rub == pytest.approx(flat.pv_forward_rub, rel=1e-15)
    beyond = repo_carry(request, horizon_date=date(2026, 12, 1))
    assert beyond.pv_rolled_rub == 0.0 and beyond.horizon_pnl_rub == beyond.pv_carry_rub


def test_theta_floating_keeps_the_path_and_rolls_the_projection() -> None:
    trade = _trade(rate=FLOAT)
    projection = _sloped()
    horizon = date(2026, 10, 28)  # Wednesday
    shift = horizon - VALUATION
    carry = _engine(trade, projection_curve=projection).carry(horizon)
    history = dict(_history().fixings)
    total = 0.0
    day = trade.start_date + timedelta(days=1)
    assert trade.end_date is not None
    while day <= trade.end_date:
        settlement = day - timedelta(days=1)
        while not CALENDAR.is_business_day(settlement):
            settlement -= timedelta(days=1)
        if settlement < VALUATION:
            value = history[settlement]
        else:
            following = CALENDAR.add_business_days(settlement, 1)
            tau = (following - settlement).days / 365
            if settlement < horizon:  # today's forward: the path to the horizon
                ratio = projection.discount_factor(settlement) / projection.discount_factor(
                    following
                )
            else:  # the rolled projection curve
                ratio = projection.discount_factor(settlement - shift) / (
                    projection.discount_factor(following - shift)
                )
            value = (ratio - 1) / tau
        total += (value + FLOAT.spread) / 365
        day += timedelta(days=1)
    end_amount = trade.start_amount_rub * (1 + total)
    rolled_df = DiscountCurve.flat(VALUATION, RATE).discount_factor(trade.end_date - shift)
    assert carry.pv_rolled_rub == pytest.approx(end_amount * rolled_df, rel=1e-13)
    assert carry.rolldown_rub != 0.0
    # Single curve on a flat curve: no rolldown.
    flat = _engine(trade).carry(horizon)
    assert flat.rolldown_rub == pytest.approx(0.0, abs=1e-6)


# --- collateral, F13 and identity -----------------------------------------------------


def test_collateral_reference_is_not_in_the_pv() -> None:
    priced = _engine(_trade(), collateral_dirty_price_rub_per_bond=700.0)
    plain = _engine(_trade())
    assert priced.valuation().pv_rub == plain.valuation().pv_rub
    assert priced.risk().dv01_rub == plain.risk().dv01_rub
    reference = priced.valuation().collateral
    assert reference is not None
    current = priced.valuation().current_repurchase_amount_rub
    assert reference.market_value_rub == 160_000.0 * 700.0
    assert reference.exposure_rub == pytest.approx(current - 112_000_000.0)
    assert reference.coverage_ratio == pytest.approx(112_000_000.0 / current)
    assert reference.initial_discount_pct == 10.0
    assert "borrower" in reference.booking
    assert plain.valuation().collateral is not None
    assert plain.valuation().collateral.market_value_rub is None  # type: ignore[union-attr]
    with pytest.raises(InvalidTerms):
        _engine(_trade(collateral=None), collateral_dirty_price_rub_per_bond=700.0)


def test_financed_bond_projection_differs_from_repo_interest_f13() -> None:
    """``project_financed_bond`` compounds daily (unchanged); repo interest is simple."""
    start, horizon = EventKey(date(2026, 10, 12), 0), EventKey(date(2026, 11, 12), 0)
    periods = constant_daily_accumulation_periods(start, horizon, (), 0.16)
    projection = project_financed_bond(
        FinancedBondRequest(start, horizon, 100_000_000.0, (), periods)
    )
    assert projection.horizon_dirty_rub_per_bond == pytest.approx(
        100_000_000.0 * (1 + 0.16 / 365) ** 31, rel=1e-14
    )
    simple = repo_end_amount(
        100_000_000.0,
        0.16,
        date(2026, 10, 12),
        date(2026, 11, 12),
        RepoDayBasis.ACT_365_366,
        decimals=None,
    )
    assert projection.horizon_dirty_rub_per_bond - simple == pytest.approx(
        100_000_000.0 * ((1 + 0.16 / 365) ** 31 - 1 - 0.16 * 31 / 365), rel=1e-6
    )
    assert projection.horizon_dirty_rub_per_bond > simple


def test_identity_and_serialization() -> None:
    trade = _trade(
        rate=FLOAT,
        cash_adjustments=(RepoCashAdjustment(date(2026, 11, 2), 1e6, "margin_call"),),
    )
    assert from_dict(RepoTrade, to_dict(trade)) == trade
    open_trade = _trade(end_date=None, rate=replace(FLOAT, interest="capitalised"))
    assert from_dict(RepoTrade, to_dict(open_trade)) == open_trade
    first = _engine(trade).base_id
    assert _engine(trade).base_id == first
    assert _engine(replace(trade, start_amount_rub=99e6)).base_id != first
    assert _engine(trade, fixings=_history(start=date(2026, 9, 2))).base_id != first
    assumed = _engine(open_trade, assumed_end_date=date(2026, 11, 12)).base_id
    assert _engine(open_trade, assumed_end_date=date(2026, 11, 13)).base_id != assumed
    assert _engine(trade).valuation().base_id == first


# --- rejections ---------------------------------------------------------------------


def test_typed_rejections() -> None:
    with pytest.raises(InvalidTerms):
        _engine(_trade(end_date=None))  # open repo without an assumed end
    with pytest.raises(InvalidTerms):
        _engine(_trade(), assumed_end_date=date(2026, 11, 13))
    with pytest.raises(InvalidTerms):
        _engine(_trade(start_date=date(2026, 10, 11)))  # Sunday
    with pytest.raises(InvalidTerms):
        _engine(_trade(), projection_curve=DiscountCurve.flat(VALUATION, 0.1))
    with pytest.raises(InvalidTerms):
        _engine(_trade(), fixings=_history())
    with pytest.raises(MissingMarketData):
        _engine(_trade(rate=FLOAT), fixings=None)
    key_history = FixingHistory("key_rate", VALUATION, (), CALENDAR, "kr")
    with pytest.raises(InvalidTerms):
        _engine(_trade(rate=FLOAT), fixings=key_history)
    with pytest.raises(InvalidTerms):
        _engine(_trade(), discount_curve=DiscountCurve.flat(date(2026, 11, 12), 0.1))
    with pytest.raises(UnsupportedProduct):
        replace(FLOAT, observation="calendar_day")
    with pytest.raises(UnsupportedProduct):
        FloatingRepoRate(
            "KEY_RATE",
            0.0,
            RepoDayBasis.ACT_365_366,
            "calendar_day",
            DayCount.ACT_365_FIXED,
            "capitalised",
        )
    with pytest.raises(UnsupportedProduct):
        _trade(currency="USD")
    with pytest.raises(InvalidTerms):
        _trade(end_date=date(2026, 10, 12))
    with pytest.raises(InvalidTerms):
        _trade(cash_adjustments=(RepoCashAdjustment(date(2026, 12, 1), 1.0, "margin_call"),))
    with pytest.raises(InvalidTerms):
        RepoCollateral("X", 1.0, initial_discount_pct=100.0)
    with pytest.raises(InvalidTerms):
        _trade(amount_rounding_decimals=-1)
    with pytest.raises(ValueError):
        FixedRepoRate(math.nan, RepoDayBasis.ACT_360)
    # A RUSFAR gap is never filled.
    gap = FixingHistory(
        "ruonia",
        VALUATION,
        tuple(row for row in _history().fixings if row[0] != date(2026, 10, 13)),
        CALENDAR,
        "gap",
    )
    with pytest.raises(MissingMarketData):
        _engine(_trade(rate=FLOAT), fixings=gap)
