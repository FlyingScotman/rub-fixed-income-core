from __future__ import annotations

import math
from dataclasses import FrozenInstanceError, replace
from datetime import date, timedelta

import pytest

from rub_fixed_income import (
    CONTINUOUS,
    FX_FORWARD_RISK_MODEL_VERSION,
    FX_FORWARD_SECOND_ORDER_MODEL_VERSION,
    FX_IMPLIED_CURVE_MODEL_VERSION,
    RISK_BUCKETS,
    BusinessCalendar,
    BusinessDayConvention,
    CalendarRangeError,
    CurveBootstrapRequest,
    DayCount,
    DiscountCurve,
    FxForward,
    FxForwardLeg,
    FxForwardRiskEngine,
    FxForwardRiskRequest,
    FxFuturesContract,
    FxFuturesRiskEngine,
    FxFuturesRiskRequest,
    FxFuturesScenario,
    FxOutrightQuote,
    FxSwap,
    FxSwapPointsQuote,
    InvalidTerms,
    OisQuote,
    ShockedCurveCache,
    bootstrap_discount_curve,
    cip_implied_foreign_rate,
    cip_implied_rub_rate,
    cip_outright,
    fx_forward_basis_bp,
    fx_forward_factor_risk,
    fx_forward_outright,
    fx_swap_value_dates,
    fx_value_date,
    implied_foreign_curve,
    implied_outrights,
    joint_calendar,
)
from rub_fixed_income._risk_common import _central_derivatives

C = date(2026, 1, 1)
R = 0.15  # RUB continuous zero rate
Q = 0.03  # CNY continuous zero rate
S = 12.5  # RUB per CNY
T = C + timedelta(days=365)
N = 1_000_000.0  # CNY bought forward
K = 13.9
RUB = DiscountCurve.flat(C, R)
CNY = DiscountCurve.flat(C, Q)


def forward(**changes: object) -> FxForward:
    leg = FxForwardLeg(T, N, K)
    base = FxForward("FWD-1", "CNY", 1.0, leg)
    return replace(base, **changes)  # type: ignore[arg-type]


def request(**changes: object) -> FxForwardRiskRequest:
    base = FxForwardRiskRequest(
        instrument=forward(),
        spot_rate=S,
        spot_value_date=C,
        rub_curve=RUB,
        foreign_curve=CNY,
        basis_bp=0.0,
    )
    return replace(base, **changes)  # type: ignore[arg-type]


def engine(**changes: object) -> FxForwardRiskEngine:
    return FxForwardRiskEngine(request(**changes))


def flat_pv(
    rub: float = R,
    cny: float = Q,
    spot: float = S,
    basis_bp: float = 0.0,
    notional: float = N,
    rate: float = K,
    spot_days: int = 0,
    days: int = 365,
) -> float:
    """Independent closed form: deliverable leg valued leg by leg, both curves flat."""
    s, t = spot_days / 365.0, days / 365.0
    foreign_leg = (
        notional * spot * math.exp(-rub * s) * math.exp(-cny * (t - s))
        * math.exp(basis_bp * 1e-4 * (t - s))
    )
    return foreign_leg - notional * rate * math.exp(-rub * t)


# --- valuation -------------------------------------------------------------------------


def test_flat_parity_value_points_and_leg_detail() -> None:
    e = engine()
    risk = e.risk()
    outright = S * math.exp(R - Q)
    assert risk.pv_rub == pytest.approx(N * (outright - K) * math.exp(-R), rel=1e-13)
    assert risk.pv_rub == pytest.approx(flat_pv(), rel=1e-13)
    (leg,) = risk.legs
    assert leg.forward_rate == pytest.approx(outright, rel=1e-14)
    assert leg.forward_points == pytest.approx(outright - S, rel=1e-12)
    assert leg.rub_discount_factor == pytest.approx(math.exp(-R), rel=1e-15)
    assert leg.pv_rub == pytest.approx(risk.pv_rub, rel=1e-12)
    assert risk.model_version == FX_FORWARD_RISK_MODEL_VERSION
    assert risk.currency == "CNY" and risk.trade_id == "FWD-1"


def test_at_market_forward_and_swap_have_zero_pv() -> None:
    outright = fx_forward_outright(
        spot_rate=S, spot_value_date=C, value_date=T, rub_curve=RUB, foreign_curve=CNY,
        basis_bp=0.0,
    )
    assert engine(instrument=forward(leg=FxForwardLeg(T, N, outright))).base_pv_rub == (
        pytest.approx(0.0, abs=1e-6)
    )
    near_date = C + timedelta(days=30)
    near = fx_forward_outright(
        spot_rate=S, spot_value_date=C, value_date=near_date, rub_curve=RUB, foreign_curve=CNY,
        basis_bp=0.0,
    )
    swap = FxSwap(
        "SWP-1", "CNY", 1.0, FxForwardLeg(near_date, N, near), FxForwardLeg(T, -N, outright)
    )
    assert swap.swap_points == pytest.approx(outright - near)
    assert engine(instrument=swap).base_pv_rub == pytest.approx(0.0, abs=1e-6)


def test_swap_is_the_sum_of_its_legs_and_far_leg_survives_the_near() -> None:
    near_date = C + timedelta(days=91)
    swap = FxSwap("SWP-2", "CNY", 1.0, FxForwardLeg(near_date, -N, 12.9), FxForwardLeg(T, N, K))
    total = engine(instrument=swap).base_pv_rub
    near = engine(instrument=forward(leg=swap.near)).base_pv_rub
    far = engine(instrument=forward(leg=swap.far)).base_pv_rub
    assert total == pytest.approx(near + far, rel=1e-12)
    assert near == pytest.approx(flat_pv(notional=-N, rate=12.9, days=91), rel=1e-12)
    remaining = swap.far_leg_forward()
    assert remaining.legs == (swap.far,) and remaining.trade_id == "SWP-2"


def test_spot_after_curve_date_discounts_spot_on_rub_and_carry_on_foreign() -> None:
    spot_date = C + timedelta(days=2)
    e = engine(spot_value_date=spot_date)
    assert e.base_pv_rub == pytest.approx(flat_pv(spot_days=2), rel=1e-13)
    # A leg between the curve date and the spot date (TOD against a TOM spot) is valid.
    today = engine(spot_value_date=spot_date, instrument=forward(leg=FxForwardLeg(C, N, K)))
    assert today.base_pv_rub == pytest.approx(flat_pv(spot_days=2, days=0), rel=1e-12)
    assert today.risk().legs[0].forward_points < 0.0  # RUB rates above CNY: TOD below TOM


def test_quote_units_scale_amounts_not_rates() -> None:
    per_hundred = engine(
        instrument=forward(quote_units=100.0, leg=FxForwardLeg(T, N, 100 * K)),
        spot_rate=100 * S,
    )
    assert per_hundred.base_pv_rub == pytest.approx(engine().base_pv_rub, rel=1e-13)


def test_cash_settled_leg_pays_difference_on_its_payment_date() -> None:
    pay = T + timedelta(days=10)
    e = engine(instrument=forward(leg=FxForwardLeg(T, N, K, payment_date=pay)))
    outright = S * math.exp(R - Q)
    assert e.base_pv_rub == pytest.approx(N * (outright - K) * math.exp(-R * 375 / 365), rel=1e-13)
    assert e.risk().legs[0].payment_date == pay


def test_basis_calibration_reprices_a_market_outright_and_is_frozen() -> None:
    market = S * math.exp(R - Q) * 0.99
    basis = fx_forward_basis_bp(
        spot_rate=S, spot_value_date=C, value_date=T, market_outright=market,
        rub_curve=RUB, foreign_curve=CNY,
    )
    assert basis == pytest.approx(math.log(0.99) * 1e4, rel=1e-12)
    e = engine(basis_bp=basis)
    assert e.risk().legs[0].forward_rate == pytest.approx(market, rel=1e-13)
    assert e.base_pv_rub == pytest.approx(flat_pv(basis_bp=basis), rel=1e-12)
    # +1 bp of basis: d/db of N * F * DF over one year, long CNY.
    risk = e.risk(difference="central")
    assert risk.basis01_rub == pytest.approx(N * market * math.exp(-R) * 1e-4, rel=1e-8)
    assert risk.basis01_rub > 0
    with pytest.raises(InvalidTerms):
        fx_forward_basis_bp(
            spot_rate=S, spot_value_date=C, value_date=C, market_outright=S,
            rub_curve=RUB, foreign_curve=CNY,
        )


def test_basis_day_count_changes_only_the_basis_accrual() -> None:
    e360 = engine(basis_bp=50.0, basis_day_count=DayCount.ACT_360)
    expected = N * S * math.exp(-Q) * math.exp(50e-4 * 365 / 360) - N * K * math.exp(-R)
    assert e360.base_pv_rub == pytest.approx(expected, rel=1e-13)


def test_forward_outright_equals_the_fx_futures_price() -> None:
    futures = FxFuturesRiskEngine(
        FxFuturesRiskRequest(
            contract=FxFuturesContract("SYN-CNY", "CNY", 1000.0, 1.0, T),
            spot_rate=S,
            spot_value_date=C + timedelta(days=1),
            rub_curve=RUB,
            foreign_curve=CNY,
            basis_bp=-35.0,
        )
    )
    e = engine(spot_value_date=C + timedelta(days=1), basis_bp=-35.0)
    assert e.risk().legs[0].forward_rate == pytest.approx(futures.base_price, rel=1e-15)


# --- first-order risk ------------------------------------------------------------------


def test_rate_risk_signs_and_analytic_derivatives_for_a_long_forward() -> None:
    risk = engine().risk(difference="central")
    # PV = N (S e^{-q} - K e^{-r}) over one year with spot today.
    assert risk.rub_dv01_rub == pytest.approx(N * K * math.exp(-R) * 1e-4, rel=1e-7)
    assert risk.foreign_dv01_rub == pytest.approx(-N * S * math.exp(-Q) * 1e-4, rel=1e-7)
    assert risk.rub_dv01_rub > 0 > risk.foreign_dv01_rub
    short = engine(instrument=forward(leg=FxForwardLeg(T, -N, K))).risk(difference="central")
    assert short.rub_dv01_rub == pytest.approx(-risk.rub_dv01_rub, rel=1e-12)
    assert short.foreign_dv01_rub == pytest.approx(-risk.foreign_dv01_rub, rel=1e-12)
    assert short.fx_delta_rub_per_pct == pytest.approx(-risk.fx_delta_rub_per_pct, rel=1e-12)


def test_one_year_leg_sits_in_the_one_year_bucket_and_buckets_sum_to_parallel() -> None:
    risk = engine().risk()
    by_bucket = {b.bucket: b.dv01_rub for b in risk.rub_buckets}
    assert set(by_bucket) == set(RISK_BUCKETS)
    assert all(v == 0.0 for k, v in by_bucket.items() if k != "1Y")
    assert risk.rub_dv01_rub == pytest.approx(risk.rub_parallel_dv01_rub, rel=1e-12)
    assert risk.foreign_dv01_rub == pytest.approx(risk.foreign_parallel_dv01_rub, rel=1e-12)


def test_off_pillar_leg_splits_between_buckets_and_sums_to_parallel() -> None:
    e = engine(
        instrument=forward(leg=FxForwardLeg(C + timedelta(days=500), N, K)),
        spot_value_date=C + timedelta(days=2),
    )
    risk = e.risk(difference="central")
    nonzero = [b.bucket for b in risk.rub_buckets if b.dv01_rub != 0.0]
    assert "1Y" in nonzero and "2Y" in nonzero
    # A spot after the curve date exposes the 1M bucket with the opposite sign.
    assert next(b.dv01_rub for b in risk.rub_buckets if b.bucket == "1M") < 0
    # Central bucket sums and parallel shifts differ only by third-order terms.
    assert risk.rub_dv01_rub == pytest.approx(risk.rub_parallel_dv01_rub, rel=1e-7)
    assert risk.foreign_dv01_rub == pytest.approx(risk.foreign_parallel_dv01_rub, rel=1e-7)


def test_forward_difference_is_the_exact_one_bp_pv_change() -> None:
    risk = engine().risk(bump_bp=1.0)
    shifted = flat_pv(rub=R + 1e-4) - flat_pv()
    assert risk.rub_parallel_dv01_rub == pytest.approx(shifted, rel=1e-10)


def test_bump_convergence_of_central_dv01() -> None:
    exact = N * K * math.exp(-R) * 1e-4
    errors = [
        abs(engine().risk(bump_bp=bump, difference="central").rub_dv01_rub - exact)
        for bump in (8.0, 4.0, 2.0)
    ]
    assert errors[0] > errors[1] > errors[2]
    assert errors[0] / errors[1] == pytest.approx(4.0, rel=0.01)  # second order in the bump
    assert errors[1] / errors[2] == pytest.approx(4.0, rel=0.01)


def test_fx_delta_is_the_foreign_leg_pv_per_percent() -> None:
    spot_date = C + timedelta(days=2)
    e = engine(spot_value_date=spot_date)
    risk = e.risk()
    foreign_leg = N * S * math.exp(-R * 2 / 365) * math.exp(-Q * 363 / 365)
    assert risk.fx_delta_rub_per_pct == pytest.approx(foreign_leg / 100.0, rel=1e-12)
    up = e.reprice(FxFuturesScenario.parallel(fx_spot_shift_pct=1.0)).total_pnl_rub
    assert risk.fx_delta_rub_per_pct == pytest.approx(up, rel=1e-12)
    # Spot-equivalent amount: the CNY notional discounted from T to the spot date.
    assert risk.fx_delta_foreign == pytest.approx(N * math.exp(-Q * 363 / 365), rel=1e-12)
    assert risk.fx_delta_foreign * S / 100.0 * math.exp(-R * 2 / 365) == pytest.approx(
        risk.fx_delta_rub_per_pct, rel=1e-12
    )


# --- scenarios and second order --------------------------------------------------------


def test_exact_scenario_matches_independent_revaluation() -> None:
    e = engine(basis_bp=-20.0)
    scenario = FxFuturesScenario.parallel(
        rub_shift_bp=150.0, foreign_shift_bp=-60.0, basis_shift_bp=10.0, fx_spot_shift_pct=-7.0
    )
    result = e.reprice(scenario)
    base = flat_pv(basis_bp=-20.0)
    shocked = flat_pv(R + 0.015, Q - 0.006, S * 0.93, -10.0)
    assert result.base_pv_rub == pytest.approx(base, rel=1e-12)
    assert result.total_pnl_rub == pytest.approx(shocked - base, rel=1e-10)
    assert result.rub_rates_pnl_rub == pytest.approx(
        flat_pv(R + 0.015, basis_bp=-20.0) - base, rel=1e-10
    )
    assert result.fx_spot_pnl_rub == pytest.approx(
        flat_pv(spot=S * 0.93, basis_bp=-20.0) - base, rel=1e-10
    )
    parts = (
        result.rub_rates_pnl_rub + result.foreign_rates_pnl_rub + result.basis_pnl_rub
        + result.fx_spot_pnl_rub + result.interaction_pnl_rub
    )
    assert parts == pytest.approx(result.total_pnl_rub, rel=1e-12)
    assert result.interaction_pnl_rub != 0.0
    assert result.base_id == e.base_id and len(result.scenario_id) == 64


def _flat_hessian_entries(spot_days: int) -> dict[str, float]:
    """Analytic derivatives for the one-year leg (bucket 1Y only when spot is today)."""
    s, t = spot_days / 365.0, 1.0
    foreign_leg = N * S * math.exp(-R * s) * math.exp(-Q * (t - s))
    rub_leg = N * K * math.exp(-R * t)
    return {
        "rub_rub": (foreign_leg * s * s - rub_leg * t * t) * 1e-8,
        "cny_cny": foreign_leg * (t - s) ** 2 * 1e-8,
        "rub_cny": foreign_leg * s * (t - s) * 1e-8,
        "fx_cny": -foreign_leg * (t - s) * 1e-4 / 100.0,
        "fx_rub": -foreign_leg * s * 1e-4 / 100.0,
        "fx": foreign_leg / 100.0,
    }


def test_second_order_against_analytic_derivatives_with_fx_cross_terms() -> None:
    second = engine().second_order()
    index = {name: i for i, name in enumerate(second.factors)}
    assert second.factors[-2:] == ("basis", "spot") and len(second.factors) == 24
    h = second.hessian_rub
    exact = _flat_hessian_entries(0)
    rub, cny, fx = index["RUB 1Y"], index["CNY 1Y"], index["spot"]
    assert h[rub][rub] == pytest.approx(exact["rub_rub"], rel=1e-5)
    assert h[cny][cny] == pytest.approx(exact["cny_cny"], rel=1e-5)
    assert h[cny][fx] == pytest.approx(exact["fx_cny"], rel=1e-7)
    assert h[fx][cny] == h[cny][fx]
    assert h[rub][fx] == 0.0  # spot today: the foreign leg does not depend on RUB rates
    assert h[fx][fx] == 0.0
    assert second.gradient_rub[fx] == pytest.approx(exact["fx"], rel=1e-13)
    assert all(h[i][j] == h[j][i] for i in range(24) for j in range(24))
    assert second.model_version == FX_FORWARD_SECOND_ORDER_MODEL_VERSION


def test_second_order_with_spot_after_curve_date_has_rub_fx_cross_term() -> None:
    e = engine(instrument=forward(leg=FxForwardLeg(T, N, K)), spot_value_date=C + timedelta(days=30))
    second = e.second_order()
    index = {name: i for i, name in enumerate(second.factors)}
    exact = _flat_hessian_entries(30)
    fx = index["spot"]
    rub_row = sum(second.hessian_rub[index[f"RUB {b}"]][fx] for b in RISK_BUCKETS)
    cny_row = sum(second.hessian_rub[index[f"CNY {b}"]][fx] for b in RISK_BUCKETS)
    assert rub_row == pytest.approx(exact["fx_rub"], rel=1e-6)
    assert cny_row == pytest.approx(exact["fx_cny"], rel=1e-6)
    assert rub_row < 0 and cny_row < 0


def test_taylor_expansion_of_gradient_and_hessian_explains_a_joint_move() -> None:
    e = engine(spot_value_date=C + timedelta(days=2), basis_bp=15.0)
    second = e.second_order()
    shift = [0.0] * 24
    shift[3], shift[11 + 3], shift[22], shift[23] = 5.0, -4.0, 3.0, 0.8  # 1Y, 1Y, basis, spot %
    size = len(RISK_BUCKETS)
    exact = e.reprice(
        FxFuturesScenario(
            tuple(shift[:size]), tuple(shift[size : 2 * size]), shift[22], shift[23]
        )
    ).total_pnl_rub
    taylor = sum(g * x for g, x in zip(second.gradient_rub, shift, strict=True)) + 0.5 * sum(
        second.hessian_rub[i][j] * shift[i] * shift[j] for i in range(24) for j in range(24)
    )
    first = sum(g * x for g, x in zip(second.gradient_rub, shift, strict=True))
    assert abs(exact - taylor) < 1e-3 * abs(exact - first)


@pytest.mark.parametrize("ois", [False, True])
def test_pruned_hessian_equals_the_full_central_difference(ois: bool) -> None:
    """Pairs of factors a trade does not read are skipped; the numbers are unchanged."""
    swap = FxSwap(
        "SWP-P", "CNY", 1.0, FxForwardLeg(C + timedelta(days=20), N, 12.6),
        FxForwardLeg(C + timedelta(days=200), -N, 13.4),
    )
    e = engine(
        instrument=swap,
        spot_value_date=C + timedelta(days=1),
        basis_bp=12.0,
        **({"rub_curve": _rub_ois()} if ois else {}),
    )
    second = e.second_order(bump_bp=2.0)
    size = len(RISK_BUCKETS)

    def pv(vector: list[float]) -> float:
        moves = (tuple(vector[:size]), tuple(vector[size : 2 * size]), vector[2 * size], 0.0)
        return e._pv(moves)

    gradient, hessian = _central_derivatives(pv, 2 * size + 1, 2.0, e.base_pv_rub, 1.0)
    assert second.gradient_rub[:-1] == gradient
    assert tuple(row[:-1] for row in second.hessian_rub[:-1]) == hessian
    assert sum(v != 0.0 for row in hessian for v in row) < 200  # most pairs are inert


def test_desk_factor_mapping_with_sparse_hessian() -> None:
    e = engine(spot_value_date=C + timedelta(days=2))
    risk = e.risk(difference="central")
    second = e.second_order()
    factors = fx_forward_factor_risk(risk, "rub_ois", "cny_fx_implied", second)
    assert factors.factors[0] == "IR|rub_ois|1M"
    assert factors.factors[11] == "IR|cny_fx_implied|1M"
    assert factors.factors[-2:] == ("FXBASIS|CNY", "FX|CNY")
    assert factors.has_second_order and factors.difference == "central"
    hessian = factors.hessian_by_factor()
    assert ("IR|cny_fx_implied|1Y", "FX|CNY") in hessian
    assert ("FX|CNY", "FX|CNY") not in hessian  # exact zero omitted
    assert all(entry.i <= entry.j for entry in factors.hessian)
    first = fx_forward_factor_risk(risk, "rub_ois", "cny_fx_implied").first_order_by_factor()
    assert first["FX|CNY"] == risk.fx_delta_rub_per_pct
    assert first["IR|rub_ois|1Y"] == pytest.approx(
        factors.first_order_by_factor()["IR|rub_ois|1Y"], rel=1e-12
    )
    with pytest.raises(InvalidTerms):
        fx_forward_factor_risk(e.risk(), "rub_ois", "cny")  # forward difference
    with pytest.raises(InvalidTerms):
        fx_forward_factor_risk(risk, "same", "same")
    with pytest.raises(InvalidTerms):
        fx_forward_factor_risk(engine().risk(difference="central"), "a", "b", second)


# --- curve bases and cache -------------------------------------------------------------


def _rub_ois() -> CurveBootstrapRequest:
    return CurveBootstrapRequest(
        C,
        (
            OisQuote("3M", date(2026, 4, 1), 0.16),
            OisQuote("6M", date(2026, 7, 1), 0.155),
            OisQuote("1Y", date(2027, 1, 1), 0.15),
            OisQuote("2Y", date(2028, 1, 1), 0.14),
        ),
    )


def test_ois_basis_buckets_match_an_independent_rebootstrap() -> None:
    ois = _rub_ois()
    cache = ShockedCurveCache()
    e = FxForwardRiskEngine(request(rub_curve=ois), curve_cache=cache)
    risk = e.risk()
    assert risk.rub_curve_basis == "ois_quote" and risk.foreign_curve_basis == "continuous_zero"
    bucket = RISK_BUCKETS.index("1Y")
    shocked = replace(
        ois,
        quotes=tuple(
            replace(q, par_rate=q.par_rate + 1e-4) if q.tenor == "1Y" else q for q in ois.quotes
        ),
    )
    reference = FxForwardRiskEngine(request(rub_curve=bootstrap_discount_curve(shocked)))
    assert risk.rub_buckets[bucket].dv01_rub == pytest.approx(
        reference.base_pv_rub - e.base_pv_rub, rel=1e-9
    )
    hits = cache.hits
    FxForwardRiskEngine(request(rub_curve=ois), curve_cache=cache).risk()
    assert cache.hits > hits


# --- implied foreign curve -------------------------------------------------------------


def _rub_nodes() -> DiscountCurve:
    return DiscountCurve(
        C,
        (
            (date(2026, 1, 2), math.exp(-0.17 / 365)),
            (date(2026, 2, 2), math.exp(-0.165 * 32 / 365)),
            (date(2026, 7, 2), math.exp(-0.155 * 182 / 365)),
            (date(2027, 1, 4), math.exp(-0.15 * 368 / 365)),
        ),
    )


def _quotes(spot_date: date) -> tuple[FxSwapPointsQuote | FxOutrightQuote, ...]:
    return (
        FxSwapPointsQuote("TODTOM", C, spot_date, 0.0047),
        FxSwapPointsQuote("TOM1W", spot_date, spot_date + timedelta(days=7), 0.032),
        FxSwapPointsQuote("TOM1M", spot_date, date(2026, 2, 2), 0.135),
        FxSwapPointsQuote("1M3M", date(2026, 2, 2), date(2026, 4, 2), 0.27),
        FxOutrightQuote("FUT-JUN", date(2026, 6, 18), 13.32),
        FxSwapPointsQuote("TOM1Y", spot_date, date(2027, 1, 4), 1.55),
    )


def test_implied_outrights_chain_backwards_and_forwards() -> None:
    spot_date = date(2026, 1, 2)
    outrights = dict(implied_outrights(S, spot_date, _quotes(spot_date)))
    assert outrights[C] == pytest.approx(S - 0.0047)
    assert outrights[date(2026, 4, 2)] == pytest.approx(S + 0.135 + 0.27)
    assert outrights[date(2026, 6, 18)] == 13.32
    assert list(outrights) == sorted(outrights)


def test_implied_foreign_curve_round_trips_every_quote() -> None:
    spot_date = date(2026, 1, 2)
    rub = _rub_nodes()
    quotes = _quotes(spot_date)
    cny = implied_foreign_curve(rub_curve=rub, spot_rate=S, spot_value_date=spot_date, quotes=quotes)
    assert cny.model_version == FX_IMPLIED_CURVE_MODEL_VERSION and cny.curve_date == C

    def outright(day: date) -> float:
        return fx_forward_outright(
            spot_rate=S, spot_value_date=spot_date, value_date=day, rub_curve=rub,
            foreign_curve=cny, basis_bp=0.0,
        )

    for quote in quotes:
        if isinstance(quote, FxSwapPointsQuote):
            points = outright(quote.far_date) - outright(quote.near_date)
            assert points == pytest.approx(quote.points, rel=1e-10, abs=1e-12)
        else:
            assert outright(quote.value_date) == pytest.approx(quote.outright, rel=1e-14)
    # A forward struck at the quoted outright has zero PV on the implied curve.
    at_market = FxForward("M", "CNY", 1.0, FxForwardLeg(date(2027, 1, 4), N, S + 1.55))
    e = FxForwardRiskEngine(
        FxForwardRiskRequest(at_market, S, spot_date, rub, cny, basis_bp=0.0)
    )
    assert e.base_pv_rub == pytest.approx(0.0, abs=1e-6)
    # Implied CNY one-year zero rate: RUB growth deflated by the forward premium.
    assert cny.zero_rate(date(2027, 1, 4), CONTINUOUS) == pytest.approx(
        0.15 - math.log((S + 1.55) / (S - 0.0047)) * 365 / 368, abs=1e-12
    )


def test_implied_curve_with_a_bootstrapped_rub_curve() -> None:
    cny = implied_foreign_curve(
        rub_curve=_rub_ois(), spot_rate=S, spot_value_date=C,
        quotes=(FxSwapPointsQuote("1Y", C, date(2027, 1, 1), 1.4),),
    )
    rub = bootstrap_discount_curve(_rub_ois())
    expected = rub.discount_factor(date(2027, 1, 1)) * (S + 1.4) / S
    assert cny.discount_factor(date(2027, 1, 1)) == pytest.approx(expected, rel=1e-14)


@pytest.mark.parametrize(
    ("quotes", "message"),
    [
        ((FxSwapPointsQuote("A", date(2026, 3, 1), date(2026, 4, 1), 0.1),), "not connected"),
        (
            (
                FxSwapPointsQuote("A", C, date(2026, 2, 1), 0.1),
                FxOutrightQuote("B", date(2026, 2, 1), 12.7),
            ),
            "already fixed",
        ),
        (
            (
                FxSwapPointsQuote("A", C, date(2026, 2, 1), 0.1),
                FxSwapPointsQuote("A", C, date(2026, 3, 1), 0.2),
            ),
            "duplicate",
        ),
        ((FxSwapPointsQuote("A", C, date(2026, 2, 1), -13.0),), "positive"),
    ],
)
def test_implied_outrights_reject_inconsistent_quotes(
    quotes: tuple[FxSwapPointsQuote | FxOutrightQuote, ...], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        implied_outrights(S, C, quotes)


def test_implied_curve_needs_an_anchor_at_the_curve_date() -> None:
    spot_date = date(2026, 1, 2)
    with pytest.raises(InvalidTerms, match="curve date"):
        implied_foreign_curve(
            rub_curve=_rub_nodes(), spot_rate=S, spot_value_date=spot_date,
            quotes=(FxSwapPointsQuote("TOM1M", spot_date, date(2026, 2, 2), 0.13),),
        )


# --- money-market parity ---------------------------------------------------------------


def test_cip_simple_rates_round_trip_and_match_curve_parity() -> None:
    start, end = date(2026, 1, 2), date(2026, 4, 2)
    f = cip_outright(
        S, start, end, rub_rate=0.16, foreign_rate=0.02,
        rub_day_count=DayCount.ACT_365_FIXED, foreign_day_count=DayCount.ACT_360,
    )
    assert f == pytest.approx(S * (1 + 0.16 * 90 / 365) / (1 + 0.02 * 90 / 360), rel=1e-15)
    assert cip_implied_rub_rate(
        S, f, start, end, foreign_rate=0.02,
        rub_day_count=DayCount.ACT_365_FIXED, foreign_day_count=DayCount.ACT_360,
    ) == pytest.approx(0.16, abs=1e-15)
    assert cip_implied_foreign_rate(
        S, f, start, end, rub_rate=0.16,
        rub_day_count=DayCount.ACT_365_FIXED, foreign_day_count=DayCount.ACT_360,
    ) == pytest.approx(0.02, abs=1e-15)
    # The same parity through discount curves built from those simple rates.
    rub = DiscountCurve(start, ((end, 1 / (1 + 0.16 * 90 / 365)),))
    usd = DiscountCurve(start, ((end, 1 / (1 + 0.02 * 90 / 360)),))
    assert fx_forward_outright(
        spot_rate=S, spot_value_date=start, value_date=end, rub_curve=rub, foreign_curve=usd,
        basis_bp=0.0,
    ) == pytest.approx(f, rel=1e-14)
    with pytest.raises(InvalidTerms):
        cip_outright(
            S, end, start, rub_rate=0.1, foreign_rate=0.0,
            rub_day_count=DayCount.ACT_365_FIXED, foreign_day_count=DayCount.ACT_365_FIXED,
        )


# --- value dates -----------------------------------------------------------------------

RU = BusinessCalendar(
    frozenset({date(2026, 6, 12)}), frozenset({date(2026, 6, 6)}), "ru", date(2026, 6, 1),
    date(2026, 12, 31),
)
CN = BusinessCalendar(
    frozenset({date(2026, 6, 19), *(date(2026, 10, d) for d in range(1, 8))}),
    frozenset({date(2026, 6, 6), date(2026, 10, 10)}),
    "cn",
    date(2026, 1, 1),
    date(2026, 11, 30),
)


def test_joint_calendar_unites_holidays_and_intersects_working_weekends() -> None:
    joint = joint_calendar((RU, CN), version="rub-cny")
    assert joint.version == "rub-cny"
    assert (joint.valid_from, joint.valid_to) == (date(2026, 6, 1), date(2026, 11, 30))
    assert not joint.is_business_day(date(2026, 6, 12))  # RU holiday
    assert not joint.is_business_day(date(2026, 6, 19))  # CN holiday
    assert joint.is_business_day(date(2026, 6, 6))  # Saturday worked in both
    assert not joint.is_business_day(date(2026, 10, 10))  # Saturday worked in CN only
    with pytest.raises(CalendarRangeError):
        joint.is_business_day(date(2026, 12, 15))
    unbounded = joint_calendar((BusinessCalendar(), BusinessCalendar()), version="w")
    assert not unbounded.is_bounded
    with pytest.raises(InvalidTerms):
        joint_calendar((), version="none")
    with pytest.raises(InvalidTerms):
        joint_calendar(
            (RU, BusinessCalendar(version="x", valid_from=C, valid_to=date(2026, 2, 1))),
            version="disjoint",
        )


def test_fx_value_dates_on_a_joint_calendar() -> None:
    joint = joint_calendar((RU, CN), version="rub-cny")
    assert fx_value_date(date(2026, 9, 30), "TOD", joint) == date(2026, 9, 30)
    assert fx_value_date(date(2026, 9, 30), "TOM", joint) == date(2026, 10, 8)
    assert fx_value_date(date(2026, 10, 1), "tom", joint) == date(2026, 10, 8)
    assert fx_value_date(date(2026, 10, 16), "SPT", joint) == date(2026, 10, 20)
    with pytest.raises(InvalidTerms, match="no TOD"):
        fx_value_date(date(2026, 10, 1), "TOD", joint)
    assert fx_value_date(date(2026, 6, 29), "1M", joint, spot_lag_days=1) == date(2026, 7, 30)
    # 30 Sep + 1M from a TOM spot of 8 Oct; month ends clamp, then modified following.
    assert fx_value_date(date(2026, 8, 28), "1M", joint, spot_lag_days=1) == date(2026, 9, 30)
    assert fx_value_date(
        date(2026, 8, 28), "1M", joint, spot_lag_days=1,
        business_day_convention=BusinessDayConvention.FOLLOWING,
    ) == date(2026, 9, 30)
    with pytest.raises(InvalidTerms, match="spot_lag_days"):
        fx_value_date(date(2026, 6, 29), "1M", joint)
    with pytest.raises(InvalidTerms, match="deposit"):
        fx_value_date(date(2026, 6, 29), "ON", joint, spot_lag_days=1)
    assert fx_swap_value_dates(date(2026, 9, 30), "TOD", "TOM", joint) == (
        date(2026, 9, 30),
        date(2026, 10, 8),
    )
    assert fx_swap_value_dates(date(2026, 10, 16), "TOM", "1W", joint, spot_lag_days=1) == (
        date(2026, 10, 19),
        date(2026, 10, 26),
    )
    with pytest.raises(InvalidTerms):
        fx_swap_value_dates(date(2026, 10, 16), "TOM", "TOD", joint)


# --- identity and validation -----------------------------------------------------------


def test_identity_tracks_inputs() -> None:
    base = engine().base_id
    assert engine().base_id == base
    assert engine(basis_bp=1.0).base_id != base
    assert engine(spot_rate=S * 1.01).base_id != base
    assert engine(instrument=forward(leg=FxForwardLeg(T, 2 * N, K))).base_id != base
    assert engine(basis_day_count=DayCount.ACT_360).base_id != base
    assert engine(foreign_curve=DiscountCurve.flat(C, Q + 1e-4)).base_id != base
    with pytest.raises(FrozenInstanceError):
        engine().base_pv_rub = 0.0  # type: ignore[misc]


def test_input_validation() -> None:
    with pytest.raises(InvalidTerms):
        forward(currency="RUB")
    with pytest.raises(InvalidTerms):
        forward(currency="cny")
    with pytest.raises(InvalidTerms):
        FxForwardLeg(T, 0.0, K)
    with pytest.raises(ValueError):
        FxForwardLeg(T, N, 0.0)
    with pytest.raises(ValueError):
        FxForwardLeg(T, math.nan, K)
    with pytest.raises(InvalidTerms, match="opposite"):
        FxSwap("S", "CNY", 1.0, FxForwardLeg(C, N, K), FxForwardLeg(T, N, K))
    with pytest.raises(InvalidTerms, match="after"):
        FxSwap("S", "CNY", 1.0, FxForwardLeg(T, N, K), FxForwardLeg(C, -N, K))
    with pytest.raises(InvalidTerms, match="settled"):
        engine(rub_curve=DiscountCurve.flat(T + timedelta(days=1), R),
               foreign_curve=DiscountCurve.flat(T + timedelta(days=1), Q),
               spot_value_date=T + timedelta(days=1))
    with pytest.raises(InvalidTerms, match="settled"):
        engine(
            instrument=forward(leg=FxForwardLeg(T, N, K, payment_date=C - timedelta(days=1)))
        )
    with pytest.raises(InvalidTerms, match="one valuation date"):
        engine(foreign_curve=DiscountCurve.flat(C + timedelta(days=1), Q))
    with pytest.raises(InvalidTerms, match="spot value date"):
        engine(spot_value_date=C - timedelta(days=1))
    with pytest.raises(TypeError):
        engine(instrument="FWD")
    with pytest.raises(TypeError):
        engine(basis_day_count="ACT/365F")
    with pytest.raises(ValueError):
        engine(spot_rate=0.0)
    with pytest.raises(ValueError):
        engine().risk(difference="backward")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        engine().reprice("shock")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        FxForwardRiskEngine(request(), curve_cache="cache")  # type: ignore[arg-type]
