from __future__ import annotations

import math
from dataclasses import FrozenInstanceError, replace
from datetime import date, timedelta
from unittest.mock import patch

import pytest

from rub_fixed_income import (
    ANNUAL,
    CONTINUOUS,
    MONTHLY,
    RISK_BUCKETS,
    RISK_PILLARS,
    BondCashflow,
    BondRiskEngine,
    BondRiskRequest,
    BondScenario,
    BusinessCalendar,
    Compounding,
    CurveBootstrapRequest,
    CurveConvention,
    DayCount,
    DiscountCurve,
    OisQuote,
    ScheduleConvention,
    ShockedCurveCache,
    ZSpreadConvention,
    key_rate_weights,
)

START = date(2025, 1, 1)


def engine(**changes: object) -> BondRiskEngine:
    request = BondRiskRequest(
        cashflows=(BondCashflow(START + timedelta(days=730), 1000.0),),
        settlement_date=START,
        curve=DiscountCurve.flat(START, 0.10),
        spread_convention=ZSpreadConvention(compounding=CONTINUOUS),
        spread_kind="credit",
        spread_bp=200.0,
    )
    return BondRiskEngine(replace(request, **changes))  # type: ignore[arg-type]


def test_zero_coupon_signed_dv01_cs01_and_duration_analytical() -> None:
    risk = engine().risk()
    pv = 1000 * math.exp(-0.12 * 2)
    move = pv * math.expm1(-0.0001 * 2)
    assert risk.pv_rub == pytest.approx(pv, abs=1e-10)
    assert risk.dv01_rub == pytest.approx(move, abs=1e-10)
    assert risk.parallel_dv01_rub == pytest.approx(move, abs=1e-10)
    assert risk.cs01_rub == pytest.approx(move, abs=1e-10)
    assert risk.ofz_spr01_rub == 0.0
    assert [r.bucket for r in risk.buckets] == list(RISK_BUCKETS)
    assert risk.buckets[4].krd_years == pytest.approx(-math.expm1(-0.0002) * 10000)
    assert all(r.dv01_rub == 0 for i, r in enumerate(risk.buckets) if i != 4)
    assert risk.dv01_rub < 0


@pytest.mark.parametrize("time", [0.0, 0.001, *RISK_PILLARS, 1.5, 4.0, 25.0, 100.0])
def test_triangular_partition_of_unity_and_tails(time: float) -> None:
    weights = key_rate_weights(time)
    assert sum(weights) == pytest.approx(1.0, abs=1e-15)
    assert all(0.0 <= w <= 1.0 for w in weights)
    assert sum(w > 0 for w in weights) <= 2
    if time <= RISK_PILLARS[0]:
        assert weights[0] == 1.0
    if time >= RISK_PILLARS[-1]:
        assert weights[-1] == 1.0


def test_between_pillars_analytical_and_finite_bump_nonadditivity() -> None:
    e = engine(cashflows=(BondCashflow(START + timedelta(days=1460), 1000),))
    result = e.risk()
    pv = 1000 * math.exp(-0.12 * 4)
    bucket_move = pv * math.expm1(-0.0001 * 4 / 2)
    assert result.buckets[5].dv01_rub == pytest.approx(bucket_move, abs=1e-10)
    assert result.buckets[6].dv01_rub == pytest.approx(bucket_move, abs=1e-10)
    assert result.dv01_rub == math.fsum(b.dv01_rub for b in result.buckets)
    assert result.parallel_dv01_rub == pytest.approx(pv * math.expm1(-0.0004), abs=1e-10)
    assert abs(result.dv01_rub - result.parallel_dv01_rub) > 1e-6
    assert abs(result.dv01_rub / result.parallel_dv01_rub - 1) < 0.001


@pytest.mark.parametrize("method", ["forward", "central"])
def test_bump_convergence_against_derivative(method: str) -> None:
    e = engine()
    expected = -2 * 1000 * math.exp(-0.24) * 1e-4
    coarse = e.risk(bump_bp=10, difference=method).dv01_rub  # type: ignore[arg-type]
    fine = e.risk(bump_bp=1, difference=method).dv01_rub  # type: ignore[arg-type]
    assert abs(fine - expected) < abs(coarse - expected) / (90 if method == "central" else 9)
    if method == "central":
        assert fine == pytest.approx(expected, rel=1e-8)


@pytest.mark.parametrize("quantity", [-7.0, 0.0, 2.5])
def test_quantity_scales_risk_and_scenario_but_not_duration(quantity: float) -> None:
    unit = engine().risk()
    e = engine(quantity=quantity)
    risk = e.risk()
    assert risk.dv01_rub == pytest.approx(unit.dv01_rub * quantity)
    assert risk.cs01_rub == pytest.approx(unit.cs01_rub * quantity)
    assert risk.pv_rub == pytest.approx(unit.pv_rub * quantity)
    assert risk.buckets[4].krd_years == unit.buckets[4].krd_years
    scenario = BondScenario.parallel(300, spread_shift_bp=200)
    assert e.reprice(scenario).total_pnl_rub == pytest.approx(
        engine().reprice(scenario).total_pnl_rub * quantity
    )


def test_ofz_routes_spread_sensitivity_exactly_once() -> None:
    risk = engine(spread_kind="ofz").risk()
    assert risk.cs01_rub == 0
    assert risk.ofz_spr01_rub == pytest.approx(engine().risk().cs01_rub)
    assert risk.ofz_spr01_rub < 0


@pytest.mark.parametrize("compounding", [ANNUAL, MONTHLY, CONTINUOUS])
def test_spread_conventions_calibration_freeze_and_large_scenario(compounding: Compounding) -> None:
    t = 2.0
    z, r = 0.02, 0.1

    def independent_price(rate: float, spread: float) -> float:
        if compounding.is_continuous:
            return 1000 * math.exp(-(rate + spread) * t)
        m = compounding.periods_per_year
        return float(1000 * (math.exp(rate / m) + spread / m) ** (-m * t))

    dirty = independent_price(r, z)
    e = engine(
        spread_bp=None,
        dirty_price_rub=dirty,
        spread_convention=ZSpreadConvention(compounding=compounding),
    )
    assert e.spread_bp == pytest.approx(200, abs=1e-7)
    # Any recalibration under a bump would erase risk; prohibit it after preparation.
    with patch("rub_fixed_income.risk.solve_z_spread", side_effect=AssertionError("recalibrated")):
        risk = e.risk()
        result = e.reprice(BondScenario.parallel(900, spread_shift_bp=400))
    assert risk.cs01_rub == pytest.approx(independent_price(r, z + 0.0001) - dirty, abs=1e-8)
    assert result.shocked_pv_rub == pytest.approx(independent_price(r + 0.09, z + 0.04), abs=1e-8)
    assert result.total_pnl_rub == pytest.approx(result.shocked_pv_rub - dirty, abs=1e-8)
    assert result.total_pnl_rub == pytest.approx(
        result.rates_pnl_rub + result.spread_pnl_rub + result.interaction_pnl_rub, abs=1e-10
    )


def test_settlement_rebases_bumped_df_and_excludes_paid_coupon() -> None:
    settlement = START + timedelta(days=365)
    # Shift only 1Y. The 2Y payment DF stays fixed but settlement DF changes.
    e = engine(
        settlement_date=settlement,
        cashflows=(
            BondCashflow(START, 1e6),
            BondCashflow(settlement, 1e6),
            BondCashflow(START + timedelta(days=730), 1000),
        ),
    )
    risk = e.risk()
    pv = 1000 * math.exp(-0.12)
    assert risk.pv_rub == pytest.approx(pv)
    assert risk.buckets[3].dv01_rub == pytest.approx(pv * math.expm1(0.0001), abs=1e-10)
    assert risk.buckets[4].dv01_rub == pytest.approx(pv * math.expm1(-0.0002), abs=1e-10)
    assert risk.parallel_dv01_rub == pytest.approx(pv * math.expm1(-0.0001), abs=1e-10)


def test_amortizing_put_horizon_irregular_leap_flows_and_twist() -> None:
    start = date(2028, 1, 17)
    settlement = date(2028, 2, 29)
    flows = (BondCashflow(date(2028, 8, 31), 340), BondCashflow(date(2029, 2, 28), 735))
    e = engine(
        cashflows=flows,
        settlement_date=settlement,
        curve=DiscountCurve.flat(start, 0.11),
    )
    shifts = (100.0, 100.0, 100.0, -50.0, -50.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)

    # Independent piecewise shift for these dates (all <2Y); no production weights/pricer.
    def shock(d: date) -> float:
        time = (d - start).days / 365
        return 0.01 if time <= 0.5 else (0.01 - 0.015 * (time - 0.5) / 0.5 if time < 1 else -0.005)

    ts = (settlement - start).days / 365
    expected = sum(
        f.amount
        * math.exp(
            -0.13 * (f.payment_date - settlement).days / 365
            - shock(f.payment_date) * (f.payment_date - start).days / 365
            + shock(settlement) * ts
        )
        for f in flows
    )
    scenario = e.reprice(BondScenario(shifts))
    assert scenario.shocked_pv_rub == pytest.approx(expected, abs=1e-9)
    assert all(b.dv01_rub == 0 for b in e.risk().buckets[5:])


def test_curve_day_count_is_distinct_from_spread_day_count() -> None:
    end = START + timedelta(days=360)
    e = engine(
        cashflows=(BondCashflow(end, 1000),),
        curve=DiscountCurve.flat(
            START, 0.1, convention=CurveConvention(time_day_count=DayCount.ACT_360)
        ),
    )
    risk = e.risk()
    pv = 1000 * math.exp(-0.1 - 0.02 * 360 / 365)
    assert risk.pv_rub == pytest.approx(pv)
    assert risk.buckets[3].dv01_rub == pytest.approx(pv * math.expm1(-0.0001), abs=1e-10)
    assert risk.cs01_rub == pytest.approx(pv * math.expm1(-0.0001 * 360 / 365), abs=1e-10)


def test_ois_quote_rebootstrap_against_two_year_analytical_bootstrap() -> None:
    quotes = (OisQuote("1Y", date(2026, 1, 1), 0.10), OisQuote("2Y", date(2027, 1, 1), 0.12))
    e = engine(curve=CurveBootstrapRequest(START, quotes), spread_bp=0.0)

    # D1=1/(1+q1); q2*(D1+D2)=1-D2 => D2=(1-q2*D1)/(1+q2).
    def price(q1: float, q2: float) -> float:
        return 1000 * (1 - q2 / (1 + q1)) / (1 + q2)

    base = price(0.10, 0.12)
    risk = e.risk()
    assert risk.curve_basis == "ois_quote"
    assert risk.pv_rub == pytest.approx(base, abs=1e-8)
    assert risk.buckets[3].dv01_rub == pytest.approx(price(0.1001, 0.12) - base, abs=1e-8)
    assert risk.buckets[4].dv01_rub == pytest.approx(price(0.10, 0.1201) - base, abs=1e-8)
    assert risk.parallel_dv01_rub == pytest.approx(price(0.1001, 0.1201) - base, abs=1e-8)
    result = e.reprice(BondScenario.parallel(200))
    assert result.shocked_pv_rub == pytest.approx(price(0.12, 0.14), abs=1e-8)
    assert e.request.curve == CurveBootstrapRequest(START, quotes)


def test_ois_quote_between_pillars_and_repricing_every_shocked_quote() -> None:
    maturity = START + timedelta(days=1460)
    quote = OisQuote("4Y", maturity, 0.1)
    e = engine(
        curve=CurveBootstrapRequest(START, (quote,)), cashflows=(BondCashflow(maturity, 1000),)
    )
    # A +100bp 3Y pillar contributes half of its move to a 4Y quote.
    shock = BondScenario((0.0, 0.0, 0.0, 0.0, 0.0, 100.0, 0.0, 0.0, 0.0, 0.0, 0.0))
    expected_engine = engine(
        curve=CurveBootstrapRequest(START, (replace(quote, par_rate=0.105),)),
        cashflows=(BondCashflow(maturity, 1000),),
    )
    assert e.reprice(shock).shocked_pv_rub == pytest.approx(
        expected_engine.base_price_rub, abs=1e-8
    )


def test_nonflat_curve_zero_shock_preserves_base_curve_shape() -> None:
    curve = DiscountCurve(
        START, ((START + timedelta(days=365), 0.9), (START + timedelta(days=1095), 0.7))
    )
    e = engine(curve=curve, spread_bp=0.0)
    expected = 1000 * math.sqrt(0.9 * 0.7)
    assert e.base_price_rub == pytest.approx(expected)
    assert e.reprice(BondScenario.parallel(500)).shocked_pv_rub == pytest.approx(
        expected * math.exp(-0.1)
    )
    assert curve.nodes[-1][1] == 0.7


def test_noop_and_reproducible_input_scenario_identity() -> None:
    e = engine()
    result = e.reprice(BondScenario())
    assert result.total_pnl_rub == 0
    assert result.rates_pnl_rub == result.spread_pnl_rub == result.interaction_pnl_rub == 0
    assert e.base_id == engine().base_id
    changes: list[dict[str, object]] = [
        {"quantity": -1},
        {"spread_bp": 201},
        {"spread_kind": "ofz"},
        {"spread_convention": ZSpreadConvention(compounding=MONTHLY)},
        {"settlement_date": START + timedelta(days=1)},
        {"curve": DiscountCurve.flat(START, 0.11)},
        {"cashflows": (BondCashflow(START + timedelta(days=730), 1001),)},
    ]
    for change in changes:
        assert engine(**change).base_id != e.base_id
    assert result.scenario_id != e.reprice(BondScenario.parallel(1)).scenario_id
    assert result.scenario_id != e.reprice(BondScenario(spread_shift_bp=1)).scenario_id
    assert len(result.base_id) == len(result.scenario_id) == 64
    with pytest.raises(FrozenInstanceError):
        e.spread_bp = 0  # type: ignore[misc]


@pytest.mark.parametrize("bad", [True, float("nan"), float("inf"), -float("inf")])
def test_invalid_numeric_inputs_rejected(bad: float) -> None:
    variants: list[dict[str, object]] = [
        {"quantity": bad},
        {"spread_bp": bad},
        {"spread_bp": None, "dirty_price_rub": bad},
        {"cashflows": (BondCashflow(START + timedelta(days=1), bad),)},
    ]
    for changes in variants:
        with pytest.raises(ValueError):
            engine(**changes)
    with pytest.raises(ValueError):
        BondScenario.parallel(bad)
    with pytest.raises(ValueError):
        BondScenario(spread_shift_bp=bad)
    with pytest.raises(ValueError):
        engine().risk(bump_bp=bad)
    with pytest.raises(ValueError):
        key_rate_weights(bad)


@pytest.mark.parametrize(
    "changes",
    [
        {"spread_bp": None},
        {"dirty_price_rub": 1000},
        {"currency": "USD"},
        {"spread_kind": "other"},
        {"settlement_date": START - timedelta(days=1)},
        {"spread_bp": None, "dirty_price_rub": 0},
        {"cashflows": ()},
        {"cashflows": (BondCashflow(START, 1000),)},
        {"cashflows": (BondCashflow(START + timedelta(days=1), -1000),)},
    ],
)
def test_invalid_request_contract(changes: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        engine(**changes)


def test_invalid_shock_bump_method_and_numerical_domain() -> None:
    for values in [(), (0.0,) * 10, (0.0,) * 12]:
        with pytest.raises(ValueError, match="11"):
            BondScenario(values)
    for bump in [0.0, -1.0]:
        with pytest.raises(ValueError):
            engine().risk(bump_bp=bump)
    with pytest.raises(ValueError, match="difference"):
        engine().risk(difference="unknown")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        engine().reprice(BondScenario.parallel(-1e308))
    with pytest.raises(ValueError):
        engine().reprice(BondScenario.parallel(1e308))
    with pytest.raises(ValueError):
        engine(spread_convention=ZSpreadConvention()).reprice(BondScenario(spread_shift_bp=-20000))
    with pytest.raises(ValueError):
        engine(quantity=1e308).risk()


def test_mutable_input_collections_are_copied() -> None:
    flows = [BondCashflow(START + timedelta(days=730), 1000)]
    shifts = [0.0] * 11
    e = engine(cashflows=flows)
    scenario = BondScenario(shifts)  # type: ignore[arg-type]
    identity = e.base_id
    flows.clear()
    shifts[0] = 1e6
    assert e.base_id == identity
    assert len(e.request.cashflows) == 1
    assert e.reprice(scenario).total_pnl_rub == 0


def test_calendar_snapshot_cannot_change_after_calibration() -> None:
    holidays: set[date] = set()
    calendar = BusinessCalendar(holidays=holidays)  # type: ignore[arg-type]
    convention = CurveConvention(fixed_leg_schedule=ScheduleConvention(calendar=calendar))
    source = CurveBootstrapRequest(
        START,
        (
            OisQuote("1Y", date(2026, 1, 1), 0.10),
            OisQuote("2Y", date(2027, 1, 1), 0.12),
        ),
        convention,
    )
    e = engine(curve=source)
    before = e.reprice(BondScenario.parallel(100))
    holidays.add(date(2026, 1, 1))
    after = e.reprice(BondScenario.parallel(100))
    assert after == before
    assert engine(curve=source).base_id != e.base_id


@pytest.mark.parametrize("days,index", [(1, 0), (365 * 25, 10)])
def test_risk_tail_cashflows_preserve_parallel_exposure(days: int, index: int) -> None:
    e = engine(cashflows=(BondCashflow(START + timedelta(days=days), 1000),))
    risk = e.risk()
    expected = 1000 * math.exp(-0.12 * days / 365) * math.expm1(-0.0001 * days / 365)
    assert risk.buckets[index].dv01_rub == pytest.approx(expected, abs=1e-10)
    assert risk.dv01_rub == pytest.approx(expected, abs=1e-10)
    assert risk.parallel_dv01_rub == risk.dv01_rub


def test_positive_and_negative_stresses_have_asymmetric_exact_pnl() -> None:
    e = engine()
    for rate, spread in [(500, 200), (-500, -200), (0, -1200)]:
        result = e.reprice(BondScenario.parallel(rate, spread_shift_bp=spread))
        expected = 1000 * math.exp(-(0.12 + (rate + spread) / 10000) * 2)
        assert result.shocked_pv_rub == pytest.approx(expected, abs=1e-9)
    up = e.reprice(BondScenario.parallel(500)).total_pnl_rub
    down = e.reprice(BondScenario.parallel(-500)).total_pnl_rub
    assert down > -up > 0


def _ois_engine(cache: ShockedCurveCache, quantity: float = 1.0) -> BondRiskEngine:
    quotes = (OisQuote("1Y", date(2026, 1, 1), 0.10), OisQuote("2Y", date(2027, 1, 1), 0.12))
    request = replace(
        engine().request,
        curve=CurveBootstrapRequest(START, quotes),
        spread_bp=None,
        dirty_price_rub=760.0,
        quantity=quantity,
    )
    return BondRiskEngine(request, cache)


def test_shared_curve_cache_reuses_rebootstraps_without_changing_numbers() -> None:
    uncached = _ois_engine(ShockedCurveCache(max_entries=0))
    shared = ShockedCurveCache()
    first, second = _ois_engine(shared), _ois_engine(shared, quantity=-3)
    expected = (uncached.risk(difference="central"), uncached.second_order())
    assert (first.risk(difference="central"), first.second_order()) == expected
    built = shared.misses
    assert built == len(shared) <= 1 + 2 * 11 + 4 * 55 + 2
    second.second_order()
    second.risk(difference="central")
    assert shared.misses == built
    assert shared.hits > built
    assert uncached.curve_cache.hits == 0 and len(uncached.curve_cache) == 0
    assert first == _ois_engine(ShockedCurveCache())


def test_curve_cache_bound_and_failed_builds() -> None:
    cache = ShockedCurveCache(max_entries=1)
    e = _ois_engine(cache)
    e.reprice(BondScenario.parallel(10))
    assert len(cache) == 1
    with pytest.raises(ValueError):
        e.reprice(BondScenario.parallel(-1e9))
    assert len(cache) == 1
    for bad in (-1, 1.5, True):
        with pytest.raises(ValueError, match="max_entries"):
            ShockedCurveCache(bad)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="curve_cache"):
        BondRiskEngine(engine().request, {})  # type: ignore[arg-type]
