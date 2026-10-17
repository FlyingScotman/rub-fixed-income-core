"""Separate projection and discount curves for floaters: PV, bucket risk on both curves,
the 23-factor Hessian, desk factors and the exactness of reused coupon projections."""

from __future__ import annotations

import math
from dataclasses import replace
from datetime import date, timedelta

import pytest

from rub_fixed_income import (
    ANNUAL,
    RISK_BUCKETS,
    BondCashflow,
    BondRiskEngine,
    BondRiskRequest,
    BondScenario,
    BusinessCalendar,
    CurveBootstrapRequest,
    DayCount,
    DiscountCurve,
    FixingHistory,
    FloatingCouponPeriod,
    FloatingCouponRule,
    FloatingLeg,
    InvalidTerms,
    OisQuote,
    TwoCurveBondRiskResult,
    ZSpreadConvention,
    bond_factor_risk,
    risk,
)
from rub_fixed_income.risk import FLOATING_TWO_CURVE_RISK_MODEL_VERSION

VALUATION = date(2026, 8, 5)
CALENDAR = BusinessCalendar(holidays=frozenset({date(2026, 11, 4)}), version="test")
DISCOUNT = DiscountCurve.flat(VALUATION, 0.15)
PROJECTION = DiscountCurve.flat(VALUATION, 0.14)
ACT365 = DayCount.ACT_365_FIXED
RULE = FloatingCouponRule("ruonia", "compounded", 0.0, ACT365, ACT365)
HISTORY = FixingHistory("ruonia", VALUATION, (), CALENDAR, "empty")


def _periods(years: int = 3) -> tuple[FloatingCouponPeriod, ...]:
    out, cursor = [], VALUATION
    for quarter in range(1, 4 * years + 1):
        month = VALUATION.month - 1 + 3 * quarter
        end = CALENDAR.modified_following(
            date(VALUATION.year + month // 12, month % 12 + 1, VALUATION.day)
        )
        out.append(FloatingCouponPeriod(cursor, end, end, 1000.0))
        cursor = end
    return tuple(out)


def _request(
    discount: DiscountCurve | CurveBootstrapRequest = DISCOUNT,
    projection: DiscountCurve | CurveBootstrapRequest | None = PROJECTION,
    **kw: object,
) -> BondRiskRequest:
    periods = _periods()
    args: dict[str, object] = {
        "cashflows": (BondCashflow(periods[-1].payment_date, 1000.0),),
        "settlement_date": VALUATION,
        "curve": discount,
        "spread_convention": ZSpreadConvention(compounding=ANNUAL),
        "spread_kind": "credit",
        "spread_bp": 0.0,
        "floating_leg": FloatingLeg(RULE, periods, HISTORY),
        "projection_curve": projection,
        "quantity": 10.0,
    }
    args.update(kw)
    return BondRiskRequest(**args)  # type: ignore[arg-type]


def test_two_curve_pv_matches_an_independent_reconstruction() -> None:
    engine = BondRiskEngine(_request())
    assert engine.two_curve and engine.model_version == FLOATING_TWO_CURVE_RISK_MODEL_VERSION
    pv = 0.0
    for period in _periods():
        # Business-day boundaries, no lookback: compounding telescopes on the projection curve.
        coupon = 1000.0 * (
            PROJECTION.discount_factor(period.accrual_start)
            / PROJECTION.discount_factor(period.accrual_end)
            - 1.0
        )
        pv += coupon * DISCOUNT.discount_factor(period.payment_date)
    pv += 1000.0 * DISCOUNT.discount_factor(_periods()[-1].payment_date)
    assert engine.base_price_rub == pytest.approx(pv, rel=1e-12)
    # Projection below discount: the floater is below par.
    assert engine.base_price_rub < 1000.0


def test_bucket_risk_on_both_curves_signs_and_totals() -> None:
    result = BondRiskEngine(_request()).risk(difference="central")
    assert isinstance(result, TwoCurveBondRiskResult)
    assert result.projection_curve_basis == "continuous_zero"
    discount = math.fsum(b.dv01_rub for b in result.buckets)
    projection = math.fsum(b.dv01_rub for b in result.projection_buckets)
    assert discount < 0.0 < projection  # long: higher coupons gain, higher discounting loses
    assert result.dv01_rub == pytest.approx(discount + projection, rel=1e-14)
    assert result.parallel_dv01_rub == pytest.approx(result.dv01_rub, rel=1e-4)
    # A reset-date floater is nearly immune to a common move of both curves.
    assert abs(result.dv01_rub) < 0.02 * abs(discount)
    assert result.cs01_rub < 0.0
    for bucket in result.projection_buckets:
        unit = bucket.dv01_rub / 10.0
        assert bucket.krd_years == pytest.approx(-unit / (result.pv_rub / 10.0) * 1e4, rel=1e-12)


def test_equal_curves_split_the_single_curve_risk_between_them() -> None:
    twin = DiscountCurve.flat(VALUATION, 0.15)
    two = BondRiskEngine(_request(projection=twin))
    one = BondRiskEngine(_request(projection=None))
    assert two.base_price_rub == pytest.approx(one.base_price_rub, rel=1e-14)
    split, single = two.risk(difference="central"), one.risk(difference="central")
    for d, p, s in zip(split.buckets, split.projection_buckets, single.buckets, strict=True):  # type: ignore[attr-defined]
        assert d.dv01_rub + p.dv01_rub == pytest.approx(s.dv01_rub, rel=1e-6, abs=1e-7)
    assert split.parallel_dv01_rub == pytest.approx(single.parallel_dv01_rub, rel=1e-9, abs=1e-9)


def test_bump_size_convergence_on_the_projection_curve() -> None:
    engine = BondRiskEngine(_request())
    coarse = engine.risk(bump_bp=1.0, difference="central")
    fine = engine.risk(bump_bp=0.25, difference="central")
    for a, b in zip(coarse.projection_buckets, fine.projection_buckets, strict=True):  # type: ignore[attr-defined]
        assert a.dv01_rub == pytest.approx(b.dv01_rub, rel=1e-5, abs=1e-8)


def test_hessian_desk_factors_and_taylor_against_exact_repricing() -> None:
    engine = BondRiskEngine(_request())
    second = engine.second_order()
    assert len(second.factors) == 23
    assert second.factors[0] == "discount 1M" and second.factors[10] == "discount 20Y+"
    assert second.factors[11] == "projection 1M" and second.factors[-1] == "credit_spread"
    central = engine.risk(difference="central")
    first = (
        *(b.dv01_rub for b in central.buckets),
        *(b.dv01_rub for b in central.projection_buckets),  # type: ignore[attr-defined]
        central.cs01_rub,
    )
    assert second.gradient_rub_per_bp == pytest.approx(first, rel=1e-12, abs=1e-12)
    h = second.hessian_rub_per_bp2
    assert all(h[i][j] == h[j][i] for i in range(23) for j in range(23))
    factors = bond_factor_risk(central, "RUB_KBD", second, projection_curve_id="RUB_RUONIA")
    assert factors.factors[0] == "IR|RUB_KBD|1M" and factors.factors[11] == "IR|RUB_RUONIA|1M"
    assert factors.factors[-1] == "CS" and factors.has_second_order
    cross = factors.hessian_by_factor()
    assert any(i.startswith("IR|RUB_KBD") and j.startswith("IR|RUB_RUONIA") for i, j in cross)
    first_only = bond_factor_risk(central, "RUB_KBD", projection_curve_id="RUB_RUONIA")
    assert first_only.first_order == pytest.approx(first, rel=1e-14)
    scenario = BondScenario.parallel(10.0, spread_shift_bp=5.0)
    approx = engine.approximate(scenario)
    assert abs(approx.residual_pnl_rub) < 1e-3 * abs(approx.exact_pnl_rub) + 1e-6
    for bad in ({}, {"projection_curve_id": "RUB_KBD"}):
        with pytest.raises(InvalidTerms):
            bond_factor_risk(central, "RUB_KBD", second, **bad)


def test_projection_only_scenarios() -> None:
    engine = BondRiskEngine(_request())
    up = (25.0,) * len(RISK_BUCKETS)
    projected = engine.reprice(BondScenario(), projection_curve_shifts_bp=up)
    both = engine.reprice(BondScenario(up))
    discount_only = engine.reprice(BondScenario(up), projection_curve_shifts_bp=(0.0,) * 11)
    assert projected.rates_pnl_rub > 0.0 > discount_only.rates_pnl_rub
    # The two moves nearly offset; what is left is their small cross term.
    cross = both.rates_pnl_rub - projected.rates_pnl_rub - discount_only.rates_pnl_rub
    assert abs(both.rates_pnl_rub) < 0.05 * projected.rates_pnl_rub
    assert abs(cross) < 0.01 * projected.rates_pnl_rub
    assert projected.scenario_id != engine.reprice(BondScenario()).scenario_id
    single = BondRiskEngine(_request(projection=None))
    with pytest.raises(ValueError, match="separate projection curve"):
        single.reprice(BondScenario(), projection_curve_shifts_bp=up)


def _ois(rate: float) -> CurveBootstrapRequest:
    quotes = tuple(
        OisQuote(
            f"{y}Y", date(VALUATION.year + y, VALUATION.month, VALUATION.day), rate + 0.001 * y
        )
        for y in (1, 2, 3, 5)
    )
    return CurveBootstrapRequest(VALUATION, quotes)


@pytest.mark.parametrize("projection", [PROJECTION, _ois(0.135)], ids=["zero", "ois"])
def test_reused_coupon_projections_are_bit_identical(
    projection: DiscountCurve | CurveBootstrapRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    fast = BondRiskEngine(_request(projection=projection))
    fast_risk, fast_second = fast.risk(difference="central"), fast.second_order()
    # Force a full re-projection on every shocked curve: no agreement, every bucket.
    monkeypatch.setattr(risk, "_agreement_date", lambda base, shocked: None)
    slow = BondRiskEngine(_request(projection=projection))
    object.__setattr__(
        slow, "_reuse", tuple(replace(r, buckets=tuple(range(11))) for r in slow._reuse)
    )
    assert slow.risk(difference="central") == fast_risk
    assert slow.second_order() == fast_second


def test_two_curve_request_validation() -> None:
    with pytest.raises(ValueError, match="needs a floating leg"):
        _request(floating_leg=None)
    with pytest.raises(ValueError, match="same date"):
        _request(projection=DiscountCurve.flat(VALUATION - timedelta(days=1), 0.14))
    engine = BondRiskEngine(_request())
    assert engine.base_id != BondRiskEngine(_request(projection=_ois(0.14))).base_id
    assert engine.base_id == BondRiskEngine(_request()).base_id
