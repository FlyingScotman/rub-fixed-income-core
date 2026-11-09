"""NDFs settled in a foreign currency and cross NDFs (BACKLOG #135): closed forms, the
two-leg replication, bump-and-reprice risk on every curve, fixings and missing inputs."""

from __future__ import annotations

import math
from dataclasses import replace
from datetime import date, timedelta

import pytest
from portfolio_v3_book import BUSINESS_DATE
from test_fx_portfolio_v036 import market, pos, run

from rub_fixed_income import (
    DiscountCurve,
    FxCrossForward,
    FxCrossInstrument,
    FxCrossRiskEngine,
    FxCrossRiskRequest,
    FxCrossScenario,
    FxCrossSwap,
    FxFixing,
    FxForward,
    FxForwardInstrument,
    FxForwardLeg,
    FxForwardRiskEngine,
    FxForwardRiskRequest,
    FxFuturesScenario,
    InvalidTerms,
    NdfConvention,
    NdfDates,
    canonical_sha256,
    fx_cross_factor_risk,
    fx_cross_rub_legs,
    fx_forward_factor_risk,
    to_dict,
)

C = date(2026, 1, 1)
T = C + timedelta(days=365)
P = T + timedelta(days=30)
R, QU, QC = 0.15, 0.04, 0.015
SU, SC = 80.0, 11.0  # RUB per USD, RUB per CNY
N = 1_000_000.0
K = 88.0  # RUB per USD
KX = 7.1  # CNY per USD
RUB = DiscountCurve.flat(C, R)
USD = DiscountCurve.flat(C, QU)
CNY = DiscountCurve.flat(C, QC)
t, p = 1.0, 395 / 365  # ACT/365F years to T and P


def leg(**changes: object) -> FxForwardLeg:
    base = FxForwardLeg(T, N, K, P, fixing_date=T - timedelta(days=1), fixing_source="CBR")
    return replace(base, **changes)  # type: ignore[arg-type]


def usd_ndf(**changes: object) -> FxForward:
    return FxForward("NDF-USD", "USD", 1.0, leg(settlement_currency="USD", **changes))


def engine(trade: FxForward, *, spot_date: date = C, **changes: object) -> FxForwardRiskEngine:
    base = FxForwardRiskRequest(trade, SU, spot_date, RUB, USD, 0.0)
    return FxForwardRiskEngine(replace(base, **changes))  # type: ignore[arg-type]


def cross(settle: str | None, *, payment: date = P, **changes: object) -> FxCrossForward:
    fixing = (
        {} if settle is None else {"fixing_date": T - timedelta(days=1), "fixing_source": "CBR"}
    )
    terms = FxForwardLeg(T, N, KX, payment, settlement_currency=settle, **fixing)  # type: ignore[arg-type]
    return FxCrossForward("X-NDF", "USD", "CNY", 1.0, replace(terms, **changes))  # type: ignore[arg-type]


def cross_engine(trade: FxCrossForward | FxCrossSwap, **changes: object) -> FxCrossRiskEngine:
    base = FxCrossRiskRequest(trade, SU, C, SC, C, RUB, USD, CNY, 0.0, 0.0)
    return FxCrossRiskEngine(replace(base, **changes))  # type: ignore[arg-type]


def forward_usd(years: float) -> float:
    return SU * math.exp((R - QU) * years)


def value(currency: str, years: float) -> float:
    """RUB today of one unit paid in ``years`` (spots on the curve date, flat curves)."""
    spot, rate = {"USD": (SU, QU), "CNY": (SC, QC), "RUB": (1.0, R)}[currency]
    return spot * math.exp(-rate * years)


# --- terms -----------------------------------------------------------------------------


def test_settlement_currency_terms_and_encoding() -> None:
    rub = leg(settlement_currency="RUB")
    assert rub.settlement_currency is None and rub == leg()
    assert canonical_sha256(rub) == canonical_sha256(leg())
    assert "settlement_currency" not in to_dict(leg())
    assert to_dict(usd_ndf().leg)["settlement_currency"] == "USD"
    with pytest.raises(InvalidTerms, match="cash-settled"):
        FxForwardLeg(T, N, K, settlement_currency="USD")
    with pytest.raises(InvalidTerms, match="settlement_currency must be"):
        leg(settlement_currency="usd")
    with pytest.raises(InvalidTerms, match="settles in RUB or USD, not CNY"):
        FxForward("A", "USD", 1.0, leg(settlement_currency="CNY"))
    with pytest.raises(InvalidTerms, match="settles in RUB, USD or CNY, not EUR"):
        cross("EUR")
    # A RUB-settled cross NDF would need two RUB fixings: still rejected.
    with pytest.raises(InvalidTerms, match="not supported"):
        FxCrossForward("X", "USD", "CNY", 1.0, leg(contract_rate=KX))
    with pytest.raises(InvalidTerms, match="not a sum of two RUB-settled legs"):
        fx_cross_rub_legs(cross("CNY"))
    rule = NdfConvention("CBR", fixing_lag_days=2, value_lag_days=2)
    built = rule.leg(NdfDates(T, P, P), N, K, settlement_currency="USD")
    assert built.settlement_currency == "USD" and built.fixing_source == "CBR"


# --- X/RUB settled in the foreign currency -----------------------------------------------


def test_usd_settled_ndf_is_the_hand_computation() -> None:
    e = engine(usd_ndf())
    forward = forward_usd(t)
    # N * (1 - K / F(T)) USD paid at P, worth S * DF_usd(P) RUB per USD today.
    assert e.base_pv_rub == pytest.approx(N * (1 - K / forward) * value("USD", p), rel=1e-13)
    row = e.leg_valuations()[0]
    assert row.settlement_currency == "USD"
    assert row.settlement_amount == pytest.approx(N * (1 - K / forward), rel=1e-13)
    assert row.forward_rate == pytest.approx(forward, rel=1e-14)
    assert row.pv_rub == pytest.approx(e.base_pv_rub, rel=1e-12)
    assert row.rub_discount_factor == RUB.discount_factor(P)
    # At the forward the value is zero.
    assert engine(usd_ndf(contract_rate=forward)).base_pv_rub == pytest.approx(0.0, abs=1e-6)
    # Paid on its value date it is worth the RUB-settled NDF and the deliverable forward.
    same_day = engine(usd_ndf(payment_date=T)).base_pv_rub
    rub_settled = engine(FxForward("R", "USD", 1.0, leg(payment_date=T))).base_pv_rub
    deliverable = engine(FxForward("D", "USD", 1.0, FxForwardLeg(T, N, K))).base_pv_rub
    assert same_day == pytest.approx(rub_settled, rel=1e-12)
    assert same_day == pytest.approx(deliverable, rel=1e-12)


def test_usd_settled_ndf_with_basis_and_spot_after_the_curve_date() -> None:
    spot_date = C + timedelta(days=1)
    e = engine(usd_ndf(), spot_date=spot_date, basis_bp=-25.0)
    s, b = 1 / 365, -25e-4
    spot_pv = SU * math.exp(-R * s) / math.exp(-QU * s)

    def growth(years: float) -> float:
        return math.exp(-QU * years) * math.exp(b * (years - s))

    forward = spot_pv * growth(t) / math.exp(-R * t)
    expected = N * (1 - K / forward) * spot_pv * growth(p)
    assert e.base_pv_rub == pytest.approx(expected, rel=1e-12)


def test_usd_settled_risk_on_both_curves_by_bump_and_reprice() -> None:
    e = engine(usd_ndf())
    risk = e.risk()
    up = engine(usd_ndf(), rub_curve=DiscountCurve.flat(C, R + 1e-4)).base_pv_rub
    assert risk.rub_parallel_dv01_rub == pytest.approx(up - e.base_pv_rub, rel=1e-12)
    up = engine(usd_ndf(), foreign_curve=DiscountCurve.flat(C, QU + 1e-4)).base_pv_rub
    assert risk.foreign_parallel_dv01_rub == pytest.approx(up - e.base_pv_rub, rel=1e-12)
    # RUB rates raise F and the USD amount; USD rates lower both.
    assert risk.rub_dv01_rub > 0.0 > risk.foreign_dv01_rub
    central = e.risk(difference="central")
    assert central.rub_dv01_rub == pytest.approx(central.rub_parallel_dv01_rub, rel=1e-8)
    assert central.foreign_dv01_rub == pytest.approx(central.foreign_parallel_dv01_rub, rel=1e-8)
    # Analytic: dPV/dr_rub = N * K / F * t * S * DF_usd(P); dPV/dr_usd = -p * PV - that.
    rub_dr = N * K / forward_usd(t) * t * value("USD", p)
    assert central.rub_dv01_rub == pytest.approx(rub_dr * 1e-4, rel=1e-6)
    assert central.foreign_dv01_rub == pytest.approx((-p * e.base_pv_rub - rub_dr) * 1e-4, rel=1e-6)
    # PV is linear in spot: the FX delta is exact, the spot Hessian diagonal zero.
    spot_up = e.reprice(FxFuturesScenario(fx_spot_shift_pct=1.0)).total_pnl_rub
    assert risk.fx_delta_rub_per_pct == pytest.approx(spot_up, rel=1e-12)
    assert risk.fx_delta_foreign == pytest.approx(N * math.exp(-QU * p), rel=1e-13)
    second = e.second_order()
    assert second.hessian_rub[-1][-1] == 0.0
    desk = fx_forward_factor_risk(central, "RUB_OIS", "USD_FX", second)
    first = desk.first_order_by_factor()
    assert first["IR|RUB_OIS|1Y"] != 0.0 and first["IR|USD_FX|1Y"] != 0.0
    assert first["FX|USD"] == pytest.approx(risk.fx_delta_rub_per_pct, rel=1e-12)


def test_fixed_usd_settled_ndf_is_a_known_usd_amount() -> None:
    fixing = 90.0
    e = engine(usd_ndf(fixing_rate=fixing))
    amount = N * (1 - K / fixing)
    assert e.base_pv_rub == pytest.approx(amount * value("USD", p), rel=1e-13)
    row = e.leg_valuations()[0]
    assert (row.fixing_rate, row.forward_rate) == (fixing, fixing)
    assert row.settlement_amount == pytest.approx(amount, rel=1e-14)
    risk = e.risk(difference="central")
    # Risk only on the settlement (USD) curve; still a USD position.
    assert all(b.dv01_rub == 0.0 for b in risk.rub_buckets)
    assert risk.foreign_dv01_rub == pytest.approx(-p * e.base_pv_rub * 1e-4, rel=1e-6)
    assert risk.fx_delta_foreign == pytest.approx(amount * math.exp(-QU * p), rel=1e-13)
    assert risk.basis01_rub == pytest.approx(e.base_pv_rub * p * 1e-4, rel=1e-6)
    # The fixed leg stays outstanding after its value date, until it is paid.
    later = C + timedelta(days=370)
    late = FxForward("L", "USD", 1.0, leg(settlement_currency="USD", fixing_rate=fixing))
    rub, usd = DiscountCurve.flat(later, R), DiscountCurve.flat(later, QU)
    seasoned = FxForwardRiskEngine(FxForwardRiskRequest(late, SU, later, rub, usd, 0.0))
    assert seasoned.base_pv_rub == pytest.approx(amount * SU * usd.discount_factor(P), rel=1e-13)


def test_usd_settled_points_held_and_horizon() -> None:
    e = engine(usd_ndf())
    # Outrights held: PV = N/q * (F(T) - K) * DF_rub(T) * F(P)/F(T) * DF_rub(P)/DF_rub(T) ...
    # ... = (F(T) - K) * N * DF_rub(P) * F(P) / F(T): the RUB curve only discounts from P.
    held = e.points_held_rub_risk()
    assert held.rub_parallel_dv01_rub == pytest.approx(-p * e.base_pv_rub * 1e-4, rel=1e-4)
    horizon = C + timedelta(days=40)
    realised = e.horizon(horizon, horizon_spot_value_date=horizon, fx_horizon="forwards_realised")
    assert realised.pv_forward_rub == pytest.approx(
        e.base_pv_rub / RUB.discount_factor(horizon), rel=1e-12
    )
    assert realised.received_rub == 0.0
    # Paid by the horizon: N * (1 - K / FX(T)) USD at FX(P).
    after = P + timedelta(days=1)
    held_spot = e.horizon(after, horizon_spot_value_date=after)
    assert held_spot.received_rub == pytest.approx(N * (SU - K), rel=1e-13)
    paid = e.horizon(after, horizon_spot_value_date=after, fx_horizon="forwards_realised")
    expected = N * (1 - K / forward_usd(t)) * forward_usd(p)
    assert paid.received_rub == pytest.approx(expected, rel=1e-12)
    fixed = engine(usd_ndf(fixing_rate=90.0)).horizon(after, horizon_spot_value_date=after)
    assert fixed.received_rub == pytest.approx(N * (1 - K / 90.0) * SU, rel=1e-13)


def test_usd_settled_missing_and_settled_inputs() -> None:
    later = T  # after the fixing date, on the value date
    rub, usd = DiscountCurve.flat(later, R), DiscountCurve.flat(later, QU)
    with pytest.raises(InvalidTerms, match="pass its published fixing_rate"):
        FxForwardRiskRequest(usd_ndf(), SU, later, rub, usd, 0.0)
    gone = P + timedelta(days=1)
    with pytest.raises(InvalidTerms, match="settled before the curve date"):
        FxForwardRiskRequest(
            usd_ndf(fixing_rate=90.0),
            SU,
            gone,
            DiscountCurve.flat(gone, R),
            DiscountCurve.flat(gone, QU),
            0.0,
        )


# --- cross NDFs -----------------------------------------------------------------------------


def deliverable_cross_pv(years: float) -> float:
    """N * V_X(T) - N * K * V_Y(T): the deliverable USD/CNY at ``years``."""
    return N * value("USD", years) - N * KX * value("CNY", years)


@pytest.mark.parametrize("settle", ["CNY", "USD", None])
def test_cross_ndf_is_the_deliverable_cross_carried_on_the_settlement_curve(
    settle: str | None,
) -> None:
    e = cross_engine(cross(settle))
    z = "RUB" if settle is None else settle
    carry = value(z, p) / value(z, t)
    assert e.base_pv_rub == pytest.approx(deliverable_cross_pv(t) * carry, rel=1e-12)
    # Paid on the value date: the deliverable cross, i.e. its two X/RUB legs.
    on_t = cross_engine(cross(settle, payment=T)).base_pv_rub
    deliverable = FxCrossForward("D", "USD", "CNY", 1.0, FxForwardLeg(T, N, KX))
    base, quote = fx_cross_rub_legs(deliverable, pivot_rate=80.0)
    two_legs = sum(
        FxForwardRiskEngine(FxForwardRiskRequest(trade, spot, C, RUB, curve, 0.0)).base_pv_rub
        for trade, spot, curve in ((base, SU, USD), (quote, SC, CNY))
    )
    assert on_t == pytest.approx(two_legs, rel=1e-10)
    assert on_t == pytest.approx(cross_engine(deliverable).base_pv_rub, rel=1e-12)
    # At the cross forward the value is zero.
    at_market = cross_engine(cross(settle)).leg_valuations()[0].forward_rate
    assert cross_engine(cross(settle, contract_rate=at_market)).base_pv_rub == pytest.approx(
        0.0, abs=1e-6
    )


def test_cross_ndf_settlement_amounts() -> None:
    forward = value("USD", t) / value("CNY", t)  # CNY per USD (q = 1)
    cny = cross_engine(cross("CNY")).leg_valuations()[0]
    assert cny.settlement_amount == pytest.approx(N * (forward - KX), rel=1e-11)
    assert cny.forward_rate == pytest.approx(forward, rel=1e-13)
    usd = cross_engine(cross("USD")).leg_valuations()[0]
    assert usd.settlement_amount == pytest.approx(N * (1 - KX / forward), rel=1e-11)
    assert usd.pv_rub == pytest.approx(cross_engine(cross("USD")).base_pv_rub, rel=1e-13)
    assert "settlement_amount" not in to_dict(cross_engine(cross(None)).leg_valuations()[0])


@pytest.mark.parametrize("settle", ["CNY", "USD"])
def test_cross_ndf_risk_on_every_curve_by_bump_and_reprice(settle: str) -> None:
    trade = cross(settle)
    e = cross_engine(trade, base_spot_value_date=C + timedelta(days=1))
    risk = e.risk()
    for field_name, curve_field, curve in (
        ("rub_parallel_dv01_rub", "rub_curve", DiscountCurve.flat(C, R + 1e-4)),
        ("base_parallel_dv01_rub", "base_curve", DiscountCurve.flat(C, QU + 1e-4)),
        ("quote_parallel_dv01_rub", "quote_curve", DiscountCurve.flat(C, QC + 1e-4)),
    ):
        bumped = cross_engine(
            trade, base_spot_value_date=C + timedelta(days=1), **{curve_field: curve}
        )
        assert getattr(risk, field_name) == pytest.approx(
            bumped.base_pv_rub - e.base_pv_rub, rel=1e-10
        )
    assert risk.base_dv01_rub != 0.0 and risk.quote_dv01_rub != 0.0 and risk.rub_dv01_rub != 0.0
    central = e.risk(difference="central")
    for rows, total in (
        (central.rub_buckets, central.rub_parallel_dv01_rub),
        (central.base_buckets, central.base_parallel_dv01_rub),
        (central.quote_buckets, central.quote_parallel_dv01_rub),
    ):
        assert math.fsum(b.dv01_rub for b in rows) == pytest.approx(total, rel=1e-8)
    # Linear in each spot: FX deltas are the exact +1 % P&L of each currency.
    for pct, scenario in (
        (risk.fx_delta_base_rub_per_pct, FxCrossScenario(base_spot_shift_pct=1.0)),
        (risk.fx_delta_quote_rub_per_pct, FxCrossScenario(quote_spot_shift_pct=1.0)),
    ):
        assert e.reprice(scenario).total_pnl_rub == pytest.approx(pct, rel=1e-12)
    second = e.second_order()
    assert second.hessian_rub[-1][-1] == 0.0 and second.hessian_rub[-2][-2] == 0.0
    desk = fx_cross_factor_risk(e.risk(difference="central"), "RUB", "USD_FX", "CNY_FX", second)
    first = desk.first_order_by_factor()
    assert first["IR|USD_FX|1Y"] != 0.0 and first["IR|CNY_FX|1Y"] != 0.0
    assert first["FX|USD"] > 0.0 > first["FX|CNY"]


def test_cross_ndf_with_basis_against_a_hand_computation() -> None:
    bb, qb = 10.0, -20.0
    e = cross_engine(cross("CNY"), base_basis_bp=bb, quote_basis_bp=qb)

    def v(currency: str, years: float) -> float:
        b = {"USD": bb, "CNY": qb}[currency] * 1e-4
        return value(currency, years) * math.exp(b * years)

    expected = (N * v("USD", t) - N * KX * v("CNY", t)) * v("CNY", p) / v("CNY", t)
    assert e.base_pv_rub == pytest.approx(expected, rel=1e-12)


def test_fixed_cross_ndf_is_a_known_amount_on_its_settlement_curve() -> None:
    fixing = 7.3
    cny = cross_engine(cross("CNY", fixing_rate=fixing))
    assert cny.base_pv_rub == pytest.approx(N * (fixing - KX) * value("CNY", p), rel=1e-13)
    risk = cny.risk(difference="central")
    assert all(b.dv01_rub == 0.0 for b in (*risk.rub_buckets, *risk.base_buckets))
    assert risk.quote_dv01_rub == pytest.approx(-p * cny.base_pv_rub * 1e-4, rel=1e-6)
    assert risk.fx_delta_base == 0.0 and risk.fx_delta_quote != 0.0
    row = cny.leg_valuations()[0]
    assert (row.fixing_rate, row.forward_rate, row.fixing_date) == (
        fixing,
        fixing,
        T - timedelta(1),
    )
    usd = cross_engine(cross("USD", fixing_rate=fixing))
    assert usd.base_pv_rub == pytest.approx(N * (1 - KX / fixing) * value("USD", p), rel=1e-13)
    assert usd.risk().fx_delta_quote == 0.0


def test_cross_ndf_points_held_and_horizon() -> None:
    e = cross_engine(cross("CNY"))
    held = e.points_held_rub_risk()
    assert held.rub_parallel_dv01_rub == pytest.approx(-p * e.base_pv_rub * 1e-4, rel=1e-4)
    horizon = C + timedelta(days=40)
    realised = e.horizon(
        horizon,
        base_horizon_spot_value_date=horizon,
        quote_horizon_spot_value_date=horizon,
        fx_horizon="forwards_realised",
    )
    assert realised.pv_forward_rub == pytest.approx(
        e.base_pv_rub / RUB.discount_factor(horizon), rel=1e-12
    )
    after = P + timedelta(days=1)
    paid = e.horizon(after, base_horizon_spot_value_date=after, quote_horizon_spot_value_date=after)
    # Spots held: N * (S_X / S_Y - K) CNY worth S_Y each, i.e. N * S_X - N * K * S_Y.
    assert paid.received_rub == pytest.approx(N * SU - N * KX * SC, rel=1e-12)
    assert paid.pv_forward_rub == 0.0
    fixed = cross_engine(cross("USD", fixing_rate=7.3)).horizon(
        after, base_horizon_spot_value_date=after, quote_horizon_spot_value_date=after
    )
    assert fixed.received_rub == pytest.approx(N * (1 - KX / 7.3) * SU, rel=1e-13)


def test_pair_settled_swap_checks_its_deliverable_leg_too() -> None:
    near = FxForwardLeg(C - timedelta(days=31), -N, 7.0)  # settled before the curve date
    far = FxForwardLeg(T, N, KX, P, settlement_currency="CNY")
    with pytest.raises(InvalidTerms, match="a leg settled before the curve date"):
        cross_engine(FxCrossSwap("XS", "USD", "CNY", 1.0, near, far))
    deliverable = FxCrossSwap("XS", "USD", "CNY", 1.0, near, FxForwardLeg(T, N, KX))
    with pytest.raises(InvalidTerms, match="a leg settled before the curve date"):
        cross_engine(deliverable)
    # Outstanding: the swap is the sum of its legs, each valued on the pair-settled path.
    live = FxCrossSwap("XS", "USD", "CNY", 1.0, replace(near, value_date=C + timedelta(5)), far)
    parts = sum(
        cross_engine(FxCrossForward("X", "USD", "CNY", 1.0, part)).base_pv_rub for part in live.legs
    )
    assert cross_engine(live).base_pv_rub == pytest.approx(parts, rel=1e-12)


def test_cross_ndf_missing_and_settled_inputs() -> None:
    later = T  # after the fixing date, on the value date
    curves = {
        "rub_curve": DiscountCurve.flat(later, R),
        "base_curve": DiscountCurve.flat(later, QU),
        "quote_curve": DiscountCurve.flat(later, QC),
        "base_spot_value_date": later,
        "quote_spot_value_date": later,
    }
    with pytest.raises(InvalidTerms, match="pass its published fixing_rate"):
        cross_engine(cross("CNY"), **curves)
    assert cross_engine(cross("CNY", fixing_rate=7.3), **curves).base_pv_rub != 0.0
    gone = {
        k: (
            replace(v, curve_date=P + timedelta(1))
            if isinstance(v, DiscountCurve)
            else P + timedelta(1)
        )
        for k, v in curves.items()
    }
    with pytest.raises(InvalidTerms, match="settled before the curve date"):
        cross_engine(cross("CNY", fixing_rate=7.3), **gone)


# --- portfolio path -------------------------------------------------------------------------


def _pf_leg(fixing_date: date, **changes: object) -> FxForwardLeg:
    terms = FxForwardLeg(
        fixing_date + timedelta(days=1),
        1_000_000.0,
        80.0,
        fixing_date + timedelta(days=2),
        fixing_date=fixing_date,
        fixing_source="CBR",
        settlement_currency="USD",
    )
    return replace(terms, **changes)  # type: ignore[arg-type]


def test_usd_settled_ndf_in_value_portfolio() -> None:
    far = BUSINESS_DATE + timedelta(days=200)
    inst = FxForwardInstrument(
        "NDF", FxForward("NDF", "USD", 1.0, _pf_leg(far)), "RUB_OIS", "USD_DF"
    )
    result = run(market("USD"), [inst], [pos("n", "NDF", 2.0, "USD")])
    row = result.fx_forwards[0]
    assert result.coverage.status == "complete"
    leg_row = row.legs[0]
    assert leg_row.settlement_currency == "USD"
    alone = run(market("USD"), [inst], [pos("n", "NDF", 1.0, "USD")]).fx_forwards[0]
    one = alone.legs[0].settlement_amount
    assert one is not None and leg_row.settlement_amount == pytest.approx(2 * one, rel=1e-15)
    first = row.factor_risk.first_order_by_factor()
    assert any(v != 0.0 for k, v in first.items() if k.startswith("IR|USD_DF|"))
    assert any(v != 0.0 for k, v in first.items() if k.startswith("IR|RUB_OIS|"))
    assert row.fx_delta_foreign != 0.0
    # Fixed from the snapshot: a known USD amount.
    fixed_on = BUSINESS_DATE - timedelta(days=1)
    inst = FxForwardInstrument(
        "NDF", FxForward("NDF", "USD", 1.0, _pf_leg(fixed_on)), "RUB_OIS", "USD_DF"
    )
    snap = market("USD", fx_fixings=(FxFixing("USD", "CBR", fixed_on, 81.0),))
    fixed = run(snap, [inst], [pos("n", "NDF", 1.0, "USD")])
    assert fixed.position_models[0].flags == ("covered_parity", "known_fixings")
    assert fixed.fx_forwards[0].legs[0].settlement_amount == pytest.approx(
        1_000_000.0 * (1 - 80.0 / 81.0), rel=1e-13
    )
    missing = run(market("USD"), [inst], [pos("n", "NDF", 1.0, "USD")])
    assert missing.coverage.rejections[0].code == "MISSING_MARKET_DATA"


def _cross_inst(fixing_date: date, **changes: object) -> FxCrossInstrument:
    terms = _pf_leg(fixing_date, contract_rate=7.1, settlement_currency="CNY", **changes)
    trade = FxCrossForward("XN", "USD", "CNY", 1.0, terms)
    return FxCrossInstrument("XN", trade, "RUB_OIS", "USD_DF", "CNY_DF", "FX")


def test_cross_ndf_in_value_portfolio_fixes_from_two_rub_fixings() -> None:
    far = BUSINESS_DATE + timedelta(days=200)
    open_row = run(market("USD", "CNY"), [_cross_inst(far)], [pos("x", "XN", 1.0, "USD")])
    assert open_row.coverage.status == "complete"
    first = open_row.fx_crosses[0].factor_risk.first_order_by_factor()
    for curve in ("USD_DF", "CNY_DF"):
        assert any(v != 0.0 for k, v in first.items() if k.startswith(f"IR|{curve}|"))
    assert open_row.fx_crosses[0].legs[0].settlement_currency == "CNY"
    fixed_on = BUSINESS_DATE - timedelta(days=1)
    fixings = (FxFixing("USD", "CBR", fixed_on, 81.0), FxFixing("CNY", "CBR", fixed_on, 11.25))
    snap = market("USD", "CNY", fx_fixings=fixings)
    result = run(snap, [_cross_inst(fixed_on)], [pos("x", "XN", 1.0, "USD")])
    assert result.coverage.status == "complete"
    row = result.fx_crosses[0]
    assert row.legs[0].fixing_rate == pytest.approx(81.0 / 11.25, rel=1e-15)
    assert "known_fixings" in result.position_models[0].flags
    first = row.factor_risk.first_order_by_factor()
    assert all(v == 0.0 for k, v in first.items() if k.startswith("IR|USD_DF|"))
    assert first["FX|USD"] == 0.0 and first["FX|CNY"] != 0.0
    # A direct cross fixing in the terms is used; it must agree with the snapshot's.
    direct = run(
        market("USD", "CNY"),
        [_cross_inst(fixed_on, fixing_rate=7.25)],
        [pos("x", "XN", 1.0, "USD")],
    )
    assert direct.fx_crosses[0].legs[0].fixing_rate == 7.25
    clash = run(snap, [_cross_inst(fixed_on, fixing_rate=7.25)], [pos("x", "XN", 1.0, "USD")])
    assert clash.coverage.rejections[0].code == "INVALID_TERMS"
    one = market("USD", "CNY", fx_fixings=fixings[:1])
    missing = run(one, [_cross_inst(fixed_on)], [pos("x", "XN", 1.0, "USD")])
    rejection = missing.coverage.rejections[0]
    assert rejection.code == "MISSING_MARKET_DATA" and "no CNY fixing (CBR)" in rejection.message
    none = run(market("USD", "CNY"), [_cross_inst(fixed_on)], [pos("x", "XN", 1.0, "USD")])
    assert "no USD and CNY fixings (CBR)" in none.coverage.rejections[0].message
    pending = run(market("USD", "CNY"), [_cross_inst(BUSINESS_DATE)], [pos("x", "XN", 1.0, "USD")])
    assert "fixing_pending" in pending.position_models[0].flags


def test_missing_curve_or_spot_of_a_currency_is_missing_market_data() -> None:
    far = BUSINESS_DATE + timedelta(days=200)
    no_cny = run(market("USD"), [_cross_inst(far)], [pos("x", "XN", 1.0, "USD")])
    assert no_cny.coverage.rejections[0].code == "MISSING_MARKET_DATA"
    snap = market("USD", "CNY")
    no_spot = replace(snap, fx_spots=tuple(s for s in snap.fx_spots if s.currency != "CNY"))
    result = run(no_spot, [_cross_inst(far)], [pos("x", "XN", 1.0, "USD")])
    assert result.coverage.rejections[0].code == "MISSING_MARKET_DATA"
    inst = FxForwardInstrument(
        "NDF", FxForward("NDF", "USD", 1.0, _pf_leg(far)), "RUB_OIS", "USD_DF"
    )
    no_usd = run(market("CNY"), [inst], [pos("n", "NDF", 1.0, "USD")])
    assert no_usd.coverage.rejections[0].code == "MISSING_MARKET_DATA"
