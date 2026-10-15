from __future__ import annotations

import math
from dataclasses import FrozenInstanceError, replace
from datetime import date, timedelta

import pytest

from rub_fixed_income import (
    FX_FUTURES_RISK_MODEL_VERSION,
    FX_FUTURES_SECOND_ORDER_MODEL_VERSION,
    RISK_BUCKETS,
    CurveBootstrapRequest,
    DayCount,
    DiscountCurve,
    FxFuturesContract,
    FxFuturesRiskEngine,
    FxFuturesRiskRequest,
    FxFuturesScenario,
    OisQuote,
    ShockedCurveCache,
    bootstrap_discount_curve,
)

START = date(2026, 1, 1)
RUB_RATE = 0.15
USD_RATE = 0.04
SPOT = 95_000.0  # synthetic RUB per 1000 USD
ONE_YEAR = START + timedelta(days=365)


def contract(**changes: object) -> FxFuturesContract:
    base = FxFuturesContract("SYNTH-USD", "USD", 1000.0, 1000.0, ONE_YEAR)
    return replace(base, **changes)  # type: ignore[arg-type]


def request(**changes: object) -> FxFuturesRiskRequest:
    base = FxFuturesRiskRequest(
        contract=contract(),
        spot_rate=SPOT,
        spot_value_date=START,
        rub_curve=DiscountCurve.flat(START, RUB_RATE),
        foreign_curve=DiscountCurve.flat(START, USD_RATE),
        basis_bp=0.0,
        quantity=3.0,
    )
    return replace(base, **changes)  # type: ignore[arg-type]


def engine(**changes: object) -> FxFuturesRiskEngine:
    return FxFuturesRiskEngine(request(**changes))


def parity(tau: float, basis_bp: float = 0.0) -> float:
    return SPOT * math.exp((RUB_RATE - USD_RATE + basis_bp * 1e-4) * tau)


def test_covered_parity_price_notional_and_fx_delta() -> None:
    e = engine()
    risk = e.risk()
    price = parity(1.0)
    assert e.base_price == pytest.approx(price, rel=1e-14)
    assert risk.theoretical_price == pytest.approx(price, rel=1e-14)
    assert risk.carry_years == 1.0
    assert risk.notional_rub == pytest.approx(3 * price, rel=1e-14)
    assert risk.fx_delta_foreign == pytest.approx(3 * 1000 * price / SPOT, rel=1e-14)
    assert risk.fx_delta_rub_per_pct == pytest.approx(3 * price * 0.01, rel=1e-10)
    assert risk.model_version == FX_FUTURES_RISK_MODEL_VERSION
    assert (risk.rub_curve_basis, risk.foreign_curve_basis) == ("continuous_zero",) * 2


def test_rub_and_foreign_rate_risk_have_opposite_signs_and_analytic_size() -> None:
    risk = engine().risk()
    price = parity(1.0)
    rub_move = 3 * price * math.expm1(1e-4)
    usd_move = 3 * price * math.expm1(-1e-4)
    assert risk.rub_parallel_dv01_rub == pytest.approx(rub_move, rel=1e-10)
    assert risk.foreign_parallel_dv01_rub == pytest.approx(usd_move, rel=1e-10)
    assert risk.rub_dv01_rub == pytest.approx(rub_move, rel=1e-10)
    assert risk.foreign_dv01_rub == pytest.approx(usd_move, rel=1e-10)
    assert risk.rub_dv01_rub > 0 > risk.foreign_dv01_rub
    # Delivery exactly at the 1Y pillar: all exposure sits in that bucket.
    one_year = RISK_BUCKETS.index("1Y")
    for buckets in (risk.rub_buckets, risk.foreign_buckets):
        assert [b.bucket for b in buckets] == list(RISK_BUCKETS)
        assert all(b.dv01_rub == 0.0 for i, b in enumerate(buckets) if i != one_year)
    assert risk.rub_buckets[one_year].krd_years == pytest.approx(-math.expm1(1e-4) * 1e4)
    assert risk.foreign_buckets[one_year].krd_years == pytest.approx(-math.expm1(-1e-4) * 1e4)
    # The basis accrues over the same year in ACT/365F, so it moves like RUB rates.
    assert risk.basis01_rub == pytest.approx(rub_move, rel=1e-10)


def test_spot_date_after_curve_date_rebases_both_curves() -> None:
    spot_date = START + timedelta(days=1)
    e = engine(spot_value_date=spot_date, contract=contract(delivery_date=ONE_YEAR + timedelta(1)))
    risk = e.risk()
    t_spot = 1 / 365
    price = parity(1.0)
    assert e.base_price == pytest.approx(price, rel=1e-13)
    # The 1M bucket only touches DF(spot date): F ~ DF_rub(s), so a RUB rise lowers F.
    assert risk.rub_buckets[0].dv01_rub == pytest.approx(
        3 * price * math.expm1(-1e-4 * t_spot), rel=1e-8
    )
    assert risk.foreign_buckets[0].dv01_rub == pytest.approx(
        3 * price * math.expm1(1e-4 * t_spot), rel=1e-8
    )
    # A parallel shift moves only the carry period between spot and delivery.
    assert risk.rub_parallel_dv01_rub == pytest.approx(3 * price * math.expm1(1e-4), rel=1e-9)
    assert risk.carry_years == 1.0


def test_market_price_calibrates_a_frozen_basis() -> None:
    market = parity(1.0) * 1.003
    calibrated = engine(basis_bp=None, futures_price=market)
    assert calibrated.base_price == pytest.approx(market, rel=1e-14)
    assert calibrated.basis_bp == pytest.approx(math.log(1.003) * 1e4, rel=1e-12)
    explicit = engine(basis_bp=calibrated.basis_bp)
    a, b = calibrated.risk(), explicit.risk()
    assert a.rub_dv01_rub == pytest.approx(b.rub_dv01_rub, rel=1e-12)
    assert a.foreign_parallel_dv01_rub == pytest.approx(b.foreign_parallel_dv01_rub, rel=1e-12)
    assert a.theoretical_price == pytest.approx(parity(1.0), rel=1e-14)
    shocked = calibrated.reprice(FxFuturesScenario.parallel(rub_shift_bp=100))
    assert shocked.shocked_price == pytest.approx(market * math.exp(0.01), rel=1e-12)


def test_basis_uses_its_own_day_count() -> None:
    e = engine(basis_bp=50.0, basis_day_count=DayCount.ACT_360)
    tau = 365 / 360
    assert e.carry_years == tau
    assert e.base_price == pytest.approx(parity(1.0) * math.exp(0.005 * tau), rel=1e-13)
    assert e.risk().basis01_rub == pytest.approx(3 * e.base_price * math.expm1(1e-4 * tau))


def test_quantity_scales_and_signs_money_but_not_durations() -> None:
    long, short, flat = (engine(quantity=q).risk() for q in (3.0, -6.0, 0.0))
    assert short.rub_dv01_rub == pytest.approx(-2 * long.rub_dv01_rub, rel=1e-14)
    assert short.fx_delta_foreign == pytest.approx(-2 * long.fx_delta_foreign, rel=1e-14)
    assert [b.krd_years for b in short.rub_buckets] == [b.krd_years for b in long.rub_buckets]
    assert flat.rub_dv01_rub == flat.foreign_dv01_rub == flat.notional_rub == 0.0
    assert long.base_id != short.base_id


def test_price_quote_units_set_the_multiplier() -> None:
    per_unit = engine(
        contract=contract(currency="CNY", price_quote_units=1.0), spot_rate=13.5, quantity=2
    )
    assert per_unit.request.contract.multiplier == 1000.0
    risk = per_unit.risk()
    price = 13.5 * math.exp(RUB_RATE - USD_RATE)
    assert risk.notional_rub == pytest.approx(2 * 1000 * price, rel=1e-14)
    assert risk.fx_delta_foreign == pytest.approx(2 * 1000 * price / 13.5, rel=1e-14)
    assert risk.rub_parallel_dv01_rub == pytest.approx(2000 * price * math.expm1(1e-4), rel=1e-9)
    assert [b.bucket for b in risk.foreign_buckets] == list(RISK_BUCKETS)


def test_exact_scenario_isolates_each_factor_and_reports_interaction() -> None:
    e = engine(basis_bp=25.0)
    scenario = FxFuturesScenario.parallel(
        rub_shift_bp=100, foreign_shift_bp=-50, basis_shift_bp=20, fx_spot_shift_pct=10
    )
    result = e.reprice(scenario)
    base = e.base_price
    assert result.rub_rates_pnl_rub == pytest.approx(3 * base * math.expm1(0.01), rel=1e-10)
    assert result.foreign_rates_pnl_rub == pytest.approx(3 * base * math.expm1(0.005), rel=1e-10)
    assert result.basis_pnl_rub == pytest.approx(3 * base * math.expm1(0.002), rel=1e-10)
    assert result.fx_spot_pnl_rub == pytest.approx(3 * base * 0.1, rel=1e-12)
    total = 3 * base * (1.1 * math.exp(0.017) - 1)
    assert result.total_pnl_rub == pytest.approx(total, rel=1e-10)
    isolated = (
        result.rub_rates_pnl_rub
        + result.foreign_rates_pnl_rub
        + result.basis_pnl_rub
        + result.fx_spot_pnl_rub
    )
    assert result.interaction_pnl_rub == pytest.approx(total - isolated, abs=1e-6)
    assert result.interaction_pnl_rub > 0
    assert result.scenario_id == e.reprice(scenario).scenario_id
    zero = e.reprice(FxFuturesScenario())
    assert zero.total_pnl_rub == 0.0 and zero.shocked_price == base


def test_full_hessian_matches_exponential_derivatives() -> None:
    e = engine(basis_bp=10.0)
    second = e.second_order()
    size = len(RISK_BUCKETS)
    assert len(second.factors) == 2 * size + 1
    assert second.factors[0] == "RUB 1M" and second.factors[size] == "USD 1M"
    assert second.factors[-1] == "basis"
    assert second.model_version == FX_FUTURES_SECOND_ORDER_MODEL_VERSION
    exposure = [0.0] * (2 * size + 1)
    one_year = RISK_BUCKETS.index("1Y")
    exposure[one_year], exposure[size + one_year], exposure[-1] = 1e-4, -1e-4, 1e-4
    value = 3 * e.base_price
    for i, g_i in enumerate(exposure):
        assert second.gradient_rub_per_bp[i] == pytest.approx(value * g_i, rel=1e-8, abs=1e-12)
        for j, g_j in enumerate(exposure):
            assert second.hessian_rub_per_bp2[i][j] == pytest.approx(
                value * g_i * g_j, rel=1e-5, abs=1e-12
            )
            assert second.hessian_rub_per_bp2[i][j] == second.hessian_rub_per_bp2[j][i]


def test_central_risk_is_symmetric_derivative() -> None:
    risk = engine().risk(difference="central", bump_bp=2.0)
    price = parity(1.0)
    assert risk.rub_parallel_dv01_rub == pytest.approx(3 * price * math.sinh(2e-4) / 2, rel=1e-10)
    assert risk.difference == "central" and risk.bump_bp == 2.0


def _quotes(level: float, shift_bp: float = 0.0) -> tuple[OisQuote, ...]:
    tenors = ((3, "3M"), (6, "6M"), (12, "1Y"), (24, "2Y"))
    return tuple(
        OisQuote(name, START + timedelta(days=30 * months), level + 0.001 * k + shift_bp * 1e-4)
        for k, (months, name) in enumerate(tenors)
    )


def test_ois_quote_basis_matches_independent_rebootstrap() -> None:
    end = START + timedelta(days=270)
    e = engine(
        contract=contract(delivery_date=end),
        rub_curve=CurveBootstrapRequest(START, _quotes(0.15)),
        foreign_curve=CurveBootstrapRequest(START, _quotes(0.04)),
    )
    risk = e.risk()
    assert (risk.rub_curve_basis, risk.foreign_curve_basis) == ("ois_quote", "ois_quote")

    def price(rub_shift: float, usd_shift: float) -> float:
        rub = bootstrap_discount_curve(CurveBootstrapRequest(START, _quotes(0.15, rub_shift)))
        usd = bootstrap_discount_curve(CurveBootstrapRequest(START, _quotes(0.04, usd_shift)))
        # Spot value date is the curve date, so F = S * DF_usd(T) / DF_rub(T).
        return SPOT * usd.discount_factor(end) / rub.discount_factor(end)

    base = price(0, 0)
    assert e.base_price == pytest.approx(base, rel=1e-13)
    assert risk.rub_parallel_dv01_rub == pytest.approx(3 * (price(1, 0) - base), rel=1e-9)
    assert risk.foreign_parallel_dv01_rub == pytest.approx(3 * (price(0, 1) - base), rel=1e-9)
    assert risk.rub_parallel_dv01_rub > 0 > risk.foreign_parallel_dv01_rub


def test_mixed_curve_bases_and_shared_cache_are_identical() -> None:
    changes: dict[str, object] = {"rub_curve": CurveBootstrapRequest(START, _quotes(0.15))}
    cache = ShockedCurveCache()
    first = FxFuturesRiskEngine(request(**changes), cache).risk()
    second = FxFuturesRiskEngine(request(**changes, quantity=-1.0), cache).risk()
    standalone = engine(**changes).risk()
    assert first == standalone
    assert (first.rub_curve_basis, first.foreign_curve_basis) == ("ois_quote", "continuous_zero")
    assert cache.hits > 0
    assert second.rub_dv01_rub == pytest.approx(-first.rub_dv01_rub / 3, rel=1e-14)


def test_identity_is_deterministic_and_results_are_frozen() -> None:
    a, b = engine(), engine()
    assert a.base_id == b.base_id
    assert a.base_id != engine(spot_rate=SPOT + 1).base_id
    assert a.base_id != engine(contract=contract(contract_id="OTHER")).base_id
    result = a.risk()
    with pytest.raises(FrozenInstanceError):
        result.rub_dv01_rub = 0.0  # type: ignore[misc]


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"basis_bp": None}, "exactly one"),
        ({"futures_price": 1.0}, "exactly one"),
        ({"spot_rate": 0.0}, "spot rate"),
        ({"spot_rate": math.nan}, "spot rate"),
        ({"spot_rate": True}, "spot rate"),
        ({"quantity": math.inf}, "quantity"),
        ({"basis_bp": math.nan}, "basis bp"),
        ({"spot_value_date": START - timedelta(1)}, "predate"),
        ({"spot_value_date": ONE_YEAR}, "follow the spot"),
        ({"foreign_curve": DiscountCurve.flat(START + timedelta(1), USD_RATE)}, "one valuation"),
    ],
)
def test_invalid_requests_raise(changes: dict[str, object], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        request(**changes)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"currency": "RUB"}, "foreign currency"),
        ({"currency": "usd"}, "ISO"),
        ({"currency": "US"}, "ISO"),
        ({"contract_id": " "}, "contract_id"),
        ({"contract_size": -1000.0}, "contract size"),
        ({"price_quote_units": 0.0}, "price quote units"),
    ],
)
def test_invalid_contracts_raise(changes: dict[str, object], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        contract(**changes)


def test_invalid_types_and_scenarios_raise() -> None:
    with pytest.raises(TypeError, match="DiscountCurve or CurveBootstrapRequest"):
        request(rub_curve=0.15)
    with pytest.raises(TypeError, match="DayCount"):
        request(basis_day_count="ACT/365F")
    with pytest.raises(ValueError, match="exactly 11"):
        FxFuturesScenario(rub_curve_shifts_bp=(1.0,))
    with pytest.raises(ValueError, match="-100"):
        FxFuturesScenario(fx_spot_shift_pct=-100.0)
    with pytest.raises(ValueError, match="difference"):
        engine().risk(difference="backward")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="bump"):
        engine().risk(bump_bp=0.0)
    with pytest.raises(ValueError, match="numerical domain|discount factor"):
        engine().reprice(FxFuturesScenario.parallel(rub_shift_bp=1e7))
