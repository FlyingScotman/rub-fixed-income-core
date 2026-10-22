"""NDF fixings (BACKLOG #49), points-held RUB risk (#46): engine level, closed forms."""

from __future__ import annotations

import math
from dataclasses import replace
from datetime import date, timedelta

import pytest

from rub_fixed_income import (
    RISK_BUCKETS,
    BusinessCalendar,
    DiscountCurve,
    FxForward,
    FxForwardLeg,
    FxForwardRiskEngine,
    FxForwardRiskRequest,
    FxFuturesScenario,
    FxSwap,
    FxSwapPointsQuote,
    InvalidTerms,
    NdfConvention,
    NdfDates,
    canonical_sha256,
    implied_foreign_curve,
    to_dict,
)

C = date(2026, 1, 1)
R = 0.15
Q = 0.04
S = 80.0
N = 1_000_000.0
K = 88.0
T = C + timedelta(days=365)
P = T + timedelta(days=2)
RUB = DiscountCurve.flat(C, R)
USD = DiscountCurve.flat(C, Q)
CAL = BusinessCalendar(frozenset({date(2026, 3, 9)}), version="ndf-test")


def ndf(**leg_changes: object) -> FxForward:
    leg = FxForwardLeg(T, N, K, P, fixing_date=T - timedelta(days=1), fixing_source="CBR")
    return FxForward("NDF-1", "USD", 1.0, replace(leg, **leg_changes))  # type: ignore[arg-type]


def engine(
    trade: FxForward | FxSwap, curve_date: date = C, **changes: object
) -> FxForwardRiskEngine:
    rub = RUB if curve_date == C else DiscountCurve.flat(curve_date, R)
    usd = USD if curve_date == C else DiscountCurve.flat(curve_date, Q)
    base = FxForwardRiskRequest(trade, S, curve_date, rub, usd, 0.0)
    return FxForwardRiskEngine(replace(base, **changes))  # type: ignore[arg-type]


# --- terms -----------------------------------------------------------------------------


def test_leg_without_fixing_terms_keeps_its_encoding_and_hash() -> None:
    leg = FxForwardLeg(T, N, K, P)
    assert to_dict(leg) == {
        "value_date": T.isoformat(),
        "notional_foreign": N,
        "contract_rate": K,
        "payment_date": P.isoformat(),
    }
    old = {"value_date": T, "notional_foreign": N, "contract_rate": K, "payment_date": P}
    assert canonical_sha256(leg) == canonical_sha256(old)
    assert not leg.is_fixed
    fixed = ndf(fixing_rate=90.0).leg
    assert fixed.is_fixed and to_dict(fixed)["fixing_rate"] == 90.0


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"payment_date": None}, "cash-settled"),
        ({"fixing_date": None}, "needs its fixing_date"),
        ({"fixing_date": T + timedelta(days=1)}, "cannot follow"),
        ({"fixing_source": " "}, "fixing_source"),
    ],
)
def test_fixing_terms_are_checked(changes: dict[str, object], message: str) -> None:
    with pytest.raises(InvalidTerms, match=message):
        ndf(**changes)
    with pytest.raises(ValueError, match="fixing rate must be positive"):
        ndf(fixing_rate=-1.0)


def test_ndf_convention_dates_both_ways_on_a_calendar() -> None:
    # Payment Wednesday 11 March 2026; Monday 9 March is a holiday of the calendar.
    rule = NdfConvention("CBR", fixing_lag_days=2, value_lag_days=1)
    dates = rule.from_payment(date(2026, 3, 11), CAL)
    assert dates == NdfDates(date(2026, 3, 6), date(2026, 3, 10), date(2026, 3, 11))
    assert rule.from_fixing(date(2026, 3, 6), CAL) == dates
    spot_for_payment = NdfConvention("EMTA-STYLE", fixing_lag_days=2, value_lag_days=2)
    same = spot_for_payment.from_payment(date(2026, 3, 11), CAL)
    assert same.value_date == same.payment_date == date(2026, 3, 11)
    leg = rule.leg(dates, N, K, fixing_rate=90.0)
    assert (leg.value_date, leg.payment_date, leg.fixing_date) == (
        dates.value_date,
        dates.payment_date,
        dates.fixing_date,
    )
    assert leg.fixing_source == "CBR" and leg.fixing_rate == 90.0
    with pytest.raises(InvalidTerms, match="not a business day"):
        rule.from_payment(date(2026, 3, 9), CAL)
    with pytest.raises(InvalidTerms, match="not a business day"):
        rule.from_fixing(date(2026, 3, 7), CAL)
    with pytest.raises(InvalidTerms, match="nonnegative integer"):
        NdfConvention("CBR", -1, 0)


# --- valuation --------------------------------------------------------------------------


def test_unfixed_ndf_is_the_cash_settled_parity_value() -> None:
    e = engine(ndf())
    t, p = 1.0, 367 / 365
    outright = S * math.exp((R - Q) * t)
    assert e.base_pv_rub == pytest.approx(N * (outright - K) * math.exp(-R * p), rel=1e-13)
    assert e.risk().legs[0].fixing_rate is None


def test_fixed_leg_is_frozen_with_rub_discounting_only() -> None:
    fixing = 91.25
    e = engine(ndf(fixing_rate=fixing))
    p = 367 / 365
    assert e.base_pv_rub == pytest.approx(N * (fixing - K) * math.exp(-R * p), rel=1e-14)
    risk = e.risk(difference="central")
    assert risk.fx_delta_foreign == 0.0 and risk.fx_delta_rub_per_pct == 0.0
    assert all(b.dv01_rub == 0.0 for b in risk.foreign_buckets)
    assert risk.foreign_parallel_dv01_rub == 0.0 and risk.basis01_rub == 0.0
    # d/dr of N (F - K) e^{-r p} per bp: the RUB discounting of the known amount only.
    analytic = -N * (fixing - K) * p * math.exp(-R * p) * 1e-4
    assert risk.rub_parallel_dv01_rub == pytest.approx(analytic, rel=1e-6)
    leg = risk.legs[0]
    assert (leg.forward_rate, leg.fixing_rate, leg.fixing_date) == (
        fixing,
        fixing,
        T - timedelta(days=1),
    )
    shocked = e.reprice(FxFuturesScenario.parallel(foreign_shift_bp=100, fx_spot_shift_pct=-10))
    assert shocked.total_pnl_rub == 0.0
    second = e.second_order()
    spot = second.factors.index("spot")
    assert second.gradient_rub[spot] == 0.0
    assert all(second.hessian_rub[spot][j] == 0.0 for j in range(len(second.factors)))


def test_no_jump_when_the_fixing_equals_the_forward() -> None:
    unfixed = engine(ndf())
    forward = unfixed.risk().legs[0].forward_rate
    fixed = engine(ndf(fixing_rate=forward))
    assert fixed.base_pv_rub == pytest.approx(unfixed.base_pv_rub, rel=1e-13, abs=1e-6)


def test_fixed_leg_may_have_a_value_date_before_the_curve_date() -> None:
    # Fixed for a value date already past (rate set for TOM), paid later: still outstanding.
    curve_date = date(2026, 6, 10)
    leg = FxForwardLeg(
        date(2026, 6, 9), N, K, date(2026, 6, 11), fixing_date=date(2026, 6, 8), fixing_rate=90.0
    )
    e = engine(FxForward("NDF-2", "USD", 1.0, leg), curve_date)
    assert e.base_pv_rub == pytest.approx(N * 2.0 * math.exp(-R / 365), rel=1e-13)
    unfixed = replace(leg, fixing_rate=None)
    with pytest.raises(InvalidTerms, match="settled before the curve date"):
        engine(FxForward("NDF-2", "USD", 1.0, unfixed), curve_date)
    pending = replace(unfixed, value_date=date(2026, 6, 11))
    with pytest.raises(InvalidTerms, match="pass its published fixing_rate"):
        engine(FxForward("NDF-2", "USD", 1.0, pending), curve_date)
    paid = replace(leg, payment_date=date(2026, 6, 9))
    with pytest.raises(InvalidTerms, match="settled before the curve date"):
        engine(FxForward("NDF-2", "USD", 1.0, paid), curve_date)


def test_swap_with_a_fixed_near_leg_keeps_the_far_leg_live() -> None:
    near = FxForwardLeg(
        C + timedelta(days=30),
        -N,
        81.0,
        C + timedelta(days=32),
        fixing_date=C + timedelta(days=29),
        fixing_rate=81.5,
    )
    far = FxForwardLeg(T, N, K)
    e = engine(FxSwap("SW-1", "USD", 1.0, near, far))
    far_only = engine(FxForward("SW-1", "USD", 1.0, far))
    known = -N * (81.5 - 81.0) * math.exp(-R * 32 / 365)
    assert e.base_pv_rub == pytest.approx(far_only.base_pv_rub + known, rel=1e-13)
    assert e.risk().fx_delta_foreign == pytest.approx(far_only.risk().fx_delta_foreign, rel=1e-13)


def test_horizon_of_a_fixed_leg_carries_the_known_amount() -> None:
    fixing = 91.25
    e = engine(ndf(fixing_rate=fixing))
    before = e.horizon(C + timedelta(days=30), horizon_spot_value_date=C + timedelta(days=30))
    days = 367 - 30
    assert before.pv_forward_rub == pytest.approx(
        N * (fixing - K) * math.exp(-R * days / 365), rel=1e-12
    )
    assert before.received_rub == 0.0
    after = e.horizon(P, horizon_spot_value_date=P, fx_horizon="forwards_realised")
    assert after.received_rub == pytest.approx(N * (fixing - K), rel=1e-14)
    assert after.pv_forward_rub == 0.0


# --- points-held RUB risk (#46) ---------------------------------------------------------


def test_points_held_rub_risk_of_a_deliverable_forward_is_the_discounting_of_its_pv() -> None:
    leg = FxForwardLeg(T, N, K)
    e = engine(FxForward("F-1", "USD", 1.0, leg))
    held = e.points_held_rub_risk()
    # PV = N (F - K) e^{-rT} with F fixed: dPV/dr = -T PV, per bp.
    assert held.rub_parallel_dv01_rub == pytest.approx(-1.0 * e.base_pv_rub * 1e-4, rel=1e-6)
    assert held.rub_dv01_rub == pytest.approx(held.rub_parallel_dv01_rub, rel=1e-6)
    default = e.risk(difference="central")
    assert abs(default.rub_parallel_dv01_rub) > 10 * abs(held.rub_parallel_dv01_rub)
    at_market = engine(
        FxForward("F-2", "USD", 1.0, replace(leg, contract_rate=e.risk().legs[0].forward_rate))
    )
    assert abs(at_market.points_held_rub_risk().rub_parallel_dv01_rub) < 1e-6


def test_points_held_equals_rebuilding_the_implied_curve_at_its_quote_dates() -> None:
    rub = DiscountCurve(
        C,
        tuple(
            (C + timedelta(days=d), math.exp(-(0.14 + 0.00001 * d) * d / 365))
            for d in (30, 91, 182, 365, 730)
        ),
    )
    quotes = (
        FxSwapPointsQuote("ON", C, C + timedelta(days=1), 0.03),
        FxSwapPointsQuote("3M", C + timedelta(days=1), C + timedelta(days=91), 2.4),
        FxSwapPointsQuote("1Y", C + timedelta(days=1), C + timedelta(days=365), 8.9),
    )
    spot_date = C + timedelta(days=1)

    def implied(curve: DiscountCurve) -> DiscountCurve:
        return implied_foreign_curve(
            rub_curve=curve, spot_rate=S, spot_value_date=spot_date, quotes=quotes
        )

    leg = FxForwardLeg(C + timedelta(days=365), N, 92.0)
    trade = FxForward("F-3", "USD", 1.0, leg)
    base = FxForwardRiskEngine(FxForwardRiskRequest(trade, S, spot_date, rub, implied(rub), 0.0))
    held = base.points_held_rub_risk(difference="forward")
    shifted = DiscountCurve(
        C, tuple((d, df * math.exp(-1e-4 * (d - C).days / 365)) for d, df in rub.nodes)
    )
    rebuilt = FxForwardRiskEngine(
        FxForwardRiskRequest(trade, S, spot_date, shifted, implied(shifted), 0.0)
    )
    # A parallel 1 bp of the RUB node zero rates, the implied curve rebuilt from the same points.
    assert held.rub_parallel_dv01_rub == pytest.approx(
        rebuilt.base_pv_rub - base.base_pv_rub, rel=1e-6
    )
    assert len(held.rub_buckets) == len(RISK_BUCKETS)
    fixed = engine(ndf(fixing_rate=90.0))
    assert fixed.points_held_rub_risk().rub_buckets == fixed.risk(difference="central").rub_buckets
