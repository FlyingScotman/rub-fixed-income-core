"""Synthetic references for MOEX short-rate futures (RUON, RUONIA index, KEYRATE)."""

from __future__ import annotations

import math
from datetime import date, timedelta

import pytest

from rub_fixed_income import (
    RISK_BUCKETS,
    BusinessCalendar,
    CurveBootstrapRequest,
    DiscountCurve,
    InvalidTerms,
    KeyRateForwardCurve,
    KeyRateForwardSegment,
    KeyRateFuturesContract,
    MissingMarketData,
    OisQuote,
    RateFuturesRiskEngine,
    RateFuturesRiskRequest,
    RateFuturesScenario,
    RuoniaAverageFuturesContract,
    RuoniaFixings,
    RuoniaIndexFuturesContract,
    RuoniaIndexValue,
    ShockedCurveCache,
    cbr_ruonia_year_fraction,
    futures_basis_factor,
    rate_futures_factor_risk,
    rate_futures_variation_margin,
    ruonia_average_final_price,
    ruonia_average_settlement_period,
)

CAL = BusinessCalendar(version="weekends-only")
VALUATION = date(2026, 10, 14)  # Wednesday


def fixings(valuation: date = VALUATION, rate: float = 0.14) -> RuoniaFixings:
    rows = []
    day = date(2026, 8, 1)
    while day < valuation:
        if CAL.is_business_day(day):
            rows.append((day, rate))
        day += timedelta(days=1)
    return RuoniaFixings(valuation, tuple(rows), CAL, "synthetic")


def ruon_october() -> RuoniaAverageFuturesContract:
    start, end = ruonia_average_settlement_period(2026, 10, CAL)
    return RuoniaAverageFuturesContract("RUON-10.26", start, end)


def flat(rate: float) -> DiscountCurve:
    return DiscountCurve.flat(VALUATION, rate)


def test_settlement_period_and_step_value_follow_the_specification() -> None:
    contract = ruon_october()
    assert (contract.period_start, contract.period_end) == (date(2026, 9, 30), date(2026, 10, 30))
    assert contract.period_days == 30
    # W = Round(30/365 * 1e6 * 0.01/100; 5) = 8.21918; W/R = 821.918
    assert contract.step_value_rub == 8.21918
    assert contract.margin_per_point_rub == 821.918
    vm = rate_futures_variation_margin(contract, 85.98, 85.99, quantity=3)
    assert vm == pytest.approx(3 * (round(85.99 * 821.918, 2) - round(85.98 * 821.918, 2)))


def test_ruon_price_joins_fixed_and_projected_days() -> None:
    contract = ruon_october()
    rate = 0.12
    engine = RateFuturesRiskEngine(
        RateFuturesRiskRequest(contract, flat(rate), fixings=fixings(), basis_bp=0.0)
    )
    fixed_days = (VALUATION - contract.period_start).days
    # Projected business-day forwards of a flat continuous curve.
    projected = 0.0
    day = VALUATION
    while day < contract.period_end:
        nxt = CAL.add_business_days(day, 1)
        tau = cbr_ruonia_year_fraction(day, nxt)
        forward = (math.exp(rate * (nxt - day).days / 365.0) - 1.0) / tau
        projected += forward * min((nxt - day).days, (contract.period_end - day).days)
        day = nxt
    expected = 100.0 - 100.0 * (0.14 * fixed_days + projected) / contract.period_days
    assert engine.base_price == pytest.approx(expected, abs=1e-12)
    risk = engine.risk(difference="central")
    assert risk.fixed_days == fixed_days and risk.projected_days == 30 - fixed_days
    assert risk.dv01_rub < 0.0  # long RUON loses when rates rise
    assert risk.basis01_rub == pytest.approx(
        -0.01 * risk.projected_days / 30 * contract.margin_per_point_rub, rel=1e-9
    )
    assert risk.dv01_rub == pytest.approx(risk.parallel_dv01_rub, rel=1e-6)


def test_final_price_needs_every_fixing() -> None:
    contract = ruon_october()
    with pytest.raises(MissingMarketData):
        ruonia_average_final_price(contract, fixings())
    full = fixings(date(2026, 10, 30), rate=0.1391)
    assert ruonia_average_final_price(contract, full) == 86.09


def test_undeclared_gap_is_rejected_and_declared_gap_carries() -> None:
    rows = tuple(r for r in fixings().fixings if r[0] != date(2026, 10, 1))
    gap = RuoniaFixings(VALUATION, rows, CAL, "gap")
    with pytest.raises(MissingMarketData, match="2026-10-01"):
        RateFuturesRiskEngine(
            RateFuturesRiskRequest(ruon_october(), flat(0.1), fixings=gap, basis_bp=0.0)
        )
    declared = RuoniaFixings(VALUATION, rows, CAL, "gap", frozenset({date(2026, 10, 1)}))
    assert declared.known_rate(date(2026, 10, 1)) == 0.14
    with pytest.raises(InvalidTerms):
        RuoniaFixings(VALUATION, fixings().fixings, CAL, "x", frozenset({date(2026, 10, 1)}))


def index_engine(
    curve: DiscountCurve | CurveBootstrapRequest, **kw: object
) -> RateFuturesRiskEngine:
    request = RateFuturesRiskRequest(
        RuoniaIndexFuturesContract("RUONIA-12.26", date(2026, 12, 1)),
        curve,
        fixings=fixings(),
        index_anchor=RuoniaIndexValue(date(2026, 10, 9), 4.5),
        **kw,  # type: ignore[arg-type]
    )
    return RateFuturesRiskEngine(request)


def test_index_projection_telescopes_to_discount_factors() -> None:
    curve = flat(0.13)
    engine = index_engine(curve, basis_bp=0.0)
    fixed = 1 + 0.14 * cbr_ruonia_year_fraction(date(2026, 10, 9), date(2026, 10, 12))
    for day in (date(2026, 10, 12), date(2026, 10, 13)):
        fixed *= 1 + 0.14 * cbr_ruonia_year_fraction(day, day + timedelta(days=1))
    expected = (
        4.5 * fixed * curve.discount_factor(VALUATION) / curve.discount_factor(date(2026, 12, 1))
    )
    assert engine.base_price == pytest.approx(expected, rel=1e-14)
    risk = engine.risk(difference="central")
    assert risk.dv01_rub > 0.0  # a long index future gains when rates rise
    assert risk.margin_per_point_rub == 10_000.0


def test_key_rate_futures_are_linear_in_the_curve() -> None:
    curve = KeyRateForwardCurve(
        VALUATION,
        0.14,
        date(2026, 10, 26),
        (KeyRateForwardSegment(date(2026, 10, 26), date(2026, 12, 21), 0.1375, "2M"),),
    )
    contract = KeyRateFuturesContract("KEYRATE-10.26", date(2026, 10, 23), date(2026, 10, 26))
    engine = RateFuturesRiskEngine(
        RateFuturesRiskRequest(contract, curve, basis_bp=0.0, quantity=-4)
    )
    assert engine.base_price == pytest.approx(13.75)
    risk = engine.risk(difference="central")
    assert risk.parallel_dv01_rub == pytest.approx(-4 * 0.01 * 1000.0)
    assert risk.dv01_rub == pytest.approx(risk.parallel_dv01_rub, abs=1e-9)
    assert sum(1 for b in risk.buckets if b.dv01_rub) <= 2
    gamma = engine.second_order()
    assert all(abs(v) < 1e-6 for row in gamma.hessian_rub_per_bp2 for v in row)
    scenario = engine.reprice(RateFuturesScenario.parallel(25.0, basis_shift_bp=10.0))
    assert scenario.total_pnl_rub == pytest.approx(-4 * 0.35 * 1000.0)
    assert scenario.interaction_pnl_rub == pytest.approx(0.0, abs=1e-9)
    expired = KeyRateFuturesContract("old", date(2026, 9, 11), date(2026, 9, 14))
    with pytest.raises(InvalidTerms):
        RateFuturesRiskRequest(expired, curve, basis_bp=0.0)


def test_basis_calibration_reprices_and_freezes() -> None:
    engine = index_engine(flat(0.13), futures_price=4.58)
    assert engine.base_price == pytest.approx(4.58, abs=1e-12)
    assert engine.theoretical_price != pytest.approx(4.58)
    shocked = engine.reprice(RateFuturesScenario.parallel(100.0))
    assert shocked.basis_pnl_rub == 0.0 and shocked.rates_pnl_rub > 0.0
    fixed = RateFuturesRiskRequest(
        ruon_october(),
        DiscountCurve.flat(date(2026, 10, 30), 0.1),
        fixings=fixings(date(2026, 10, 30)),
        futures_price=86.0,
    )
    with pytest.raises(InvalidTerms, match="fully fixed"):
        RateFuturesRiskEngine(fixed)


def test_scenarios_bucket_sums_and_hessian_are_consistent() -> None:
    engine = RateFuturesRiskEngine(
        RateFuturesRiskRequest(
            ruon_october(), flat(0.12), fixings=fixings(), basis_bp=-5.0, quantity=7
        )
    )
    risk = engine.risk(difference="central")
    gamma = engine.second_order()
    assert gamma.factors == (*RISK_BUCKETS, "basis")
    for bucket, gradient in zip(risk.buckets, gamma.gradient_rub_per_bp, strict=False):
        assert gradient == pytest.approx(bucket.dv01_rub, rel=1e-9, abs=1e-12)
    for i, row in enumerate(gamma.hessian_rub_per_bp2):
        for j, value in enumerate(row):
            assert value == gamma.hessian_rub_per_bp2[j][i]
    scenario = engine.reprice(RateFuturesScenario.parallel(200.0))
    assert scenario.total_pnl_rub == pytest.approx(
        (scenario.shocked_price - scenario.base_price) * 7 * ruon_october().margin_per_point_rub
    )
    bump = engine.reprice(RateFuturesScenario.parallel(1.0)).total_pnl_rub
    assert bump == pytest.approx(engine.risk().parallel_dv01_rub)
    factors = rate_futures_factor_risk(risk, "RUB_RUONIA_OIS", gamma)
    assert factors.factors[-1] == futures_basis_factor("RUON-10.26") == "FUTBASIS|RUON-10.26"
    assert factors.factors[0] == "IR|RUB_RUONIA_OIS|1M"
    with pytest.raises(InvalidTerms):
        rate_futures_factor_risk(engine.risk(), "RUB_RUONIA_OIS")


def test_ois_projection_shares_the_cache_and_quantity_scales() -> None:
    quotes = (
        OisQuote("1M", date(2026, 11, 14), 0.139),
        OisQuote("3M", date(2027, 1, 14), 0.137),
        OisQuote("6M", date(2027, 4, 14), 0.134),
    )
    cache = ShockedCurveCache()
    request = RateFuturesRiskRequest(
        ruon_october(), CurveBootstrapRequest(VALUATION, quotes), fixings=fixings(), basis_bp=0.0
    )
    engine = RateFuturesRiskEngine(request, cache)
    one = engine.risk()
    misses = cache.misses
    ten = engine.for_quantity(-10.0).risk()
    assert cache.misses == misses  # every repricing came from the shared memo
    assert ten.dv01_rub == pytest.approx(-10.0 * one.dv01_rub)
    assert ten.base_id != one.base_id
    assert RateFuturesRiskEngine(request, cache).base_id == engine.base_id


def test_requests_reject_inconsistent_inputs() -> None:
    contract = ruon_october()
    with pytest.raises(InvalidTerms):
        RateFuturesRiskRequest(contract, flat(0.1), fixings=fixings())
    with pytest.raises(MissingMarketData):
        RateFuturesRiskRequest(contract, flat(0.1), basis_bp=0.0)
    with pytest.raises(InvalidTerms):
        RateFuturesRiskRequest(
            contract, flat(0.1), fixings=fixings(date(2026, 10, 15)), basis_bp=0.0
        )
    with pytest.raises(MissingMarketData):
        RateFuturesRiskRequest(
            RuoniaIndexFuturesContract("X", date(2026, 12, 1)),
            flat(0.1),
            fixings=fixings(),
            basis_bp=0.0,
        )
    with pytest.raises(InvalidTerms):
        RuoniaAverageFuturesContract("X", date(2026, 10, 30), date(2026, 10, 30))
    with pytest.raises(InvalidTerms):
        RuoniaFixings(VALUATION, ((date(2026, 10, 10), 0.1),), CAL, "weekend")
