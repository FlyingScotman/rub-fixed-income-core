"""Synthetic references for OFZ basket futures: CF, carry, CTD, risk and scenarios."""

from __future__ import annotations

import math
from datetime import date
from itertools import pairwise

import pytest

from rub_fixed_income import (
    CashflowKind,
    CouponPeriod,
    DeliverableBond,
    DiscountCurve,
    FixedCouponLeg,
    Instrument,
    InvalidTerms,
    OfzFuturesContract,
    OfzFuturesRiskEngine,
    OfzFuturesRiskRequest,
    OfzFuturesScenario,
    PrincipalLeg,
    ProductType,
    Redemption,
    ofz_conversion_factor,
    ofz_futures_factor_risk,
)

VALUATION = date(2026, 10, 15)
SPOT = date(2026, 10, 16)
DELIVERY = date(2026, 12, 7)
CONTRACT = OfzFuturesContract("OFZ6-12.26", date(2026, 12, 4), DELIVERY)


def annual_bond(name: str, coupon: float, first: date, years: int) -> Instrument:
    """Annual coupons ``coupon`` RUB on 1000 face, paid on ``first`` and anniversaries."""
    starts = [date(first.year - 1 + k, first.month, first.day) for k in range(years + 1)]
    periods = tuple(CouponPeriod(s, e, e, coupon) for s, e in pairwise(starts))
    return Instrument(
        name,
        ProductType.FIXED_BOND,
        "RUB",
        1000.0,
        FixedCouponLeg(periods),
        PrincipalLeg((Redemption(starts[-1], 1000.0, CashflowKind.MATURITY),)),
        "MOEX_STOCK",
    )


SHORT = annual_bond("SU_SHORT", 70.0, date(2026, 11, 20), 5)  # coupon before delivery
LONG = annual_bond("SU_LONG", 90.0, date(2027, 6, 1), 7)


def engine(
    bonds: tuple[tuple[Instrument, float, float], ...] = ((SHORT, 1.0, 98.0), (LONG, 1.1, 104.0)),
    **kw: object,
) -> OfzFuturesRiskEngine:
    request = OfzFuturesRiskRequest(
        CONTRACT,
        tuple(DeliverableBond(i, cf, px) for i, cf, px in bonds),
        SPOT,
        DiscountCurve.flat(VALUATION, 0.14),
        DiscountCurve.flat(VALUATION, 0.15),
        **({"basis_bp": 0.0} | kw),  # type: ignore[arg-type]
    )
    return OfzFuturesRiskEngine(request)


def test_conversion_factor_is_one_at_par_on_a_coupon_date() -> None:
    bond = annual_bond("PAR", 70.0, date(2027, 12, 7), 1)
    assert ofz_conversion_factor(bond, DELIVERY, 0.07) == 1.0
    assert (
        ofz_conversion_factor(bond, DELIVERY, 0.08)
        < 1.0
        < ofz_conversion_factor(bond, DELIVERY, 0.06)
    )
    raw = ofz_conversion_factor(bond, date(2027, 6, 7), 0.07, decimals=None)
    assert raw != 1.0
    accrued = bond.accrued(date(2027, 6, 7))
    leg, maturity = bond.coupon_leg, bond.maturity_date
    assert isinstance(leg, FixedCouponLeg) and maturity is not None
    # Between coupons the clean price at the coupon yield is not par: check the formula.
    assert raw == pytest.approx(
        (
            sum(
                p.amount / 1.07 ** ((p.payment_date - date(2027, 6, 7)).days / 365)
                for p in leg.periods
                if p.payment_date > date(2027, 6, 7)
            )
            + 1000.0 / 1.07 ** ((maturity - date(2027, 6, 7)).days / 365)
            - accrued
        )
        / 1000.0
    )


def test_carry_with_a_coupon_before_delivery() -> None:
    e = engine(((SHORT, 1.0, 98.0),))
    funding = DiscountCurve.flat(VALUATION, 0.15)
    dirty = 980.0 + SHORT.accrued(SPOT)

    def growth(start: date) -> float:
        return funding.discount_factor(start) / funding.discount_factor(DELIVERY)

    forward = dirty * growth(SPOT) - 70.0 * growth(date(2026, 11, 20))
    expected = 10 * (forward - SHORT.accrued(DELIVERY)) / 1.0
    assert e.theoretical_price == pytest.approx(expected, rel=1e-12)
    (row,) = e.basket()
    assert row.forward_dirty_rub == pytest.approx(forward, rel=1e-12)
    assert row.net_basis_rub == pytest.approx(0.0, abs=1e-9)
    assert row.gross_basis_rub == pytest.approx(980.0 - e.base_price / 10, rel=1e-12)


def test_implied_repo_equals_the_funding_rate_without_coupons() -> None:
    e = engine(((LONG, 1.1, 104.0),))
    (row,) = e.basket()
    funding = DiscountCurve.flat(VALUATION, 0.15)
    tau = (DELIVERY - SPOT).days / 365
    simple = (funding.discount_factor(SPOT) / funding.discount_factor(DELIVERY) - 1) / tau
    assert row.implied_repo_rate == pytest.approx(simple, rel=1e-10)


def test_dv01_runs_through_the_cheapest_bond() -> None:
    e = engine()
    risk = e.risk(difference="central")
    cheapest = next(a for a in risk.basket if a.cheapest)
    assert risk.cheapest_to_deliver == cheapest.instrument_id == "SU_LONG"
    assert risk.cheapest_by_implied_repo == "SU_LONG"
    # Parallel bond-curve DV01: 10 / CF times the change of the forward of the cheapest.
    shocked = e.reprice(OfzFuturesScenario.parallel(bond_shift_bp=1.0))
    assert shocked.total_pnl_rub == pytest.approx(e.risk().bond_parallel_dv01_rub)
    assert risk.bond_dv01_rub < 0.0 < risk.funding_dv01_rub
    assert risk.cs01_rub == pytest.approx(risk.bond_parallel_dv01_rub, rel=0.02)
    assert risk.bond_dv01_rub == pytest.approx(risk.bond_parallel_dv01_rub, rel=1e-3)
    assert sum(b.dv01_rub for b in risk.funding_buckets) == pytest.approx(risk.funding_dv01_rub)


def test_large_shift_switches_the_cheapest_bond_exactly() -> None:
    e = engine(((SHORT, 0.97, 96.0), (LONG, 1.1, 110.0)), quantity=-3)
    base = e.risk()
    assert base.cheapest_to_deliver == "SU_SHORT"
    up = e.reprice(OfzFuturesScenario.parallel(bond_shift_bp=300.0))
    assert up.base_cheapest == "SU_SHORT" and up.shocked_cheapest == "SU_LONG"
    assert up.total_pnl_rub == pytest.approx(-3 * (up.shocked_price - up.base_price))
    assert up.total_pnl_rub > 0.0  # a short futures gains when yields rise
    mixed = e.reprice(OfzFuturesScenario.parallel(bond_shift_bp=50.0, funding_shift_bp=-25.0))
    assert mixed.total_pnl_rub == pytest.approx(
        mixed.bond_rates_pnl_rub
        + mixed.funding_rates_pnl_rub
        + mixed.spread_pnl_rub
        + mixed.basis_pnl_rub
        + mixed.interaction_pnl_rub
    )


def test_second_order_and_desk_factors() -> None:
    e = engine(quantity=5)
    risk = e.risk(difference="central")
    gamma = e.second_order()
    assert len(gamma.factors) == 24
    assert gamma.gradient_rub_per_bp[:11] == pytest.approx(
        [b.dv01_rub for b in risk.bond_buckets], rel=1e-9, abs=1e-12
    )
    for i, row in enumerate(gamma.hessian_rub_per_bp2):
        for j, value in enumerate(row):
            assert value == gamma.hessian_rub_per_bp2[j][i]
    factors = ofz_futures_factor_risk(risk, "RUB_KBD", "RUB_REPO", gamma)
    assert factors.factors[0] == "IR|RUB_KBD|1M" and factors.factors[11] == "IR|RUB_REPO|1M"
    assert factors.factors[-2:] == ("CS", "FUTBASIS|OFZ6-12.26")
    with pytest.raises(InvalidTerms):
        ofz_futures_factor_risk(risk, "RUB_KBD", "RUB_KBD")
    with pytest.raises(InvalidTerms):
        ofz_futures_factor_risk(e.risk(), "RUB_KBD", "RUB_REPO")


def test_basis_calibration_and_quantity_sharing() -> None:
    e = engine(futures_price=9500.0, basis_bp=None)
    assert e.base_price == pytest.approx(9500.0, abs=1e-6)
    assert e.reprice(OfzFuturesScenario.parallel(bond_shift_bp=10)).basis_pnl_rub == 0.0
    twin = e.for_quantity(-7)
    assert twin.risk().bond_dv01_rub == pytest.approx(-7 * e.risk().bond_dv01_rub)
    with pytest.raises(InvalidTerms, match="no carry"):
        OfzFuturesRiskEngine(
            OfzFuturesRiskRequest(
                CONTRACT,
                (DeliverableBond(LONG, 1.1, 104.0),),
                DELIVERY,
                DiscountCurve.flat(VALUATION, 0.14),
                DiscountCurve.flat(VALUATION, 0.15),
                futures_price=9500.0,
            )
        )


def test_requests_reject_invalid_baskets() -> None:
    curve = DiscountCurve.flat(VALUATION, 0.14)
    matured = annual_bond("OLD", 70.0, date(2026, 6, 1), 1)
    with pytest.raises(InvalidTerms):
        OfzFuturesRiskRequest(
            CONTRACT, (DeliverableBond(matured, 1.0, 99.0),), SPOT, curve, curve, basis_bp=0.0
        )
    with pytest.raises(InvalidTerms):
        OfzFuturesRiskRequest(
            CONTRACT,
            (DeliverableBond(LONG, 1.1, 104.0), DeliverableBond(LONG, 1.1, 104.0)),
            SPOT,
            curve,
            curve,
            basis_bp=0.0,
        )
    with pytest.raises(InvalidTerms):
        OfzFuturesRiskRequest(CONTRACT, (DeliverableBond(LONG, 1.1, 104.0),), SPOT, curve, curve)
    with pytest.raises(InvalidTerms):
        OfzFuturesContract("X", DELIVERY, DELIVERY)
    with pytest.raises(InvalidTerms):
        ofz_conversion_factor(LONG, DELIVERY, -1.5)
    assert math.isfinite(ofz_conversion_factor(LONG, DELIVERY, 0.07))
