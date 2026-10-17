"""CDS: ISDA standard model goldens, analytical limits, bootstrap, quotes and risk."""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import FrozenInstanceError, replace
from datetime import date, timedelta
from typing import Any, Literal

import pytest

from rub_fixed_income import (
    CS_FACTOR,
    RISK_BUCKETS,
    BusinessCalendar,
    BusinessDayConvention,
    CalendarRangeError,
    CurveBootstrapRequest,
    DayCount,
    DiscountCurve,
    InvalidTerms,
    OisQuote,
    ShockedCurveCache,
    UnsupportedProduct,
    adjust_date,
    ir_factor,
)
from rub_fixed_income._solver import illinois
from rub_fixed_income.cds import (
    CDS_MODEL_VERSION,
    CDS_RISK_MODEL_VERSION,
    CDS_SECOND_ORDER_MODEL_VERSION,
    AccrualOnDefault,
    CdsContract,
    CdsConvention,
    CdsCurveCache,
    CdsCurveRequest,
    CdsRiskEngine,
    CdsRiskRequest,
    CdsScenario,
    CdsSide,
    CdsSpreadQuote,
    CdsUpfrontQuote,
    HazardCurve,
    bootstrap_hazard_curve,
    cds_factor_risk,
    cds_par_spread,
    cds_premium_schedule,
    cds_trade_dates,
    flat_hazard_from_spread,
    flat_hazard_from_upfront,
    previous_imm_date,
    spread_from_upfront,
    standard_cds_maturity,
    upfront_from_spread,
    value_cds,
)
from rub_fixed_income.dates import add_months

WEEKENDS = BusinessCalendar(version="weekends-only")
ISDA = CdsConvention.isda_standard(WEEKENDS)
MF = BusinessDayConvention.MODIFIED_FOLLOWING


# ---------------------------------------------------------------- ISDA-style IR curve
#
# The ISDA Standard Model builds its discount curve from money-market deposits
# (ACT/360, simple, from spot T+2) and par swaps (fixed leg 30/360, single curve)
# with log-linear discount factors. QuantLib's test suite builds the same curve
# from "Markit published rates" for its ISDA reconciliation cases; this helper
# rebuilds it independently on the library's DiscountCurve.


def _days_30_360(start: date, end: date) -> float:
    d1, d2 = min(start.day, 30), end.day
    if d2 == 31 and d1 >= 30:
        d2 = 30
    return (360 * (end.year - start.year) + 30 * (end.month - start.month) + d2 - d1) / 360.0


def _root(function: Callable[[float], float], lower: float, upper: float) -> float:
    low = function(lower)
    for _ in range(200):
        middle = (lower + upper) / 2.0
        value = function(middle)
        if value == 0.0 or upper - lower < 1e-16:
            return middle
        if (value < 0.0) == (low < 0.0):
            lower, low = middle, value
        else:
            upper = middle
    return (lower + upper) / 2.0


def isda_curve(
    trade: date,
    deposits: list[tuple[int, float]],
    swaps: list[tuple[int, float]],
    fixed_months: int,
) -> DiscountCurve:
    spot = WEEKENDS.add_business_days(trade, 2)
    nodes: list[tuple[date, float]] = []

    def solve(maturity: date, residual: Callable[[DiscountCurve], float]) -> None:
        def objective(log_df: float) -> float:
            return residual(DiscountCurve(trade, (*nodes, (maturity, math.exp(log_df)))))

        nodes.append((maturity, math.exp(_root(objective, -5.0, 1.0))))

    for months, rate in deposits:
        end = adjust_date(add_months(spot, months), MF, WEEKENDS)

        def deposit(curve: DiscountCurve, end: date = end, rate: float = rate) -> float:
            growth = curve.discount_factor(spot) / curve.discount_factor(end)
            return growth - (1.0 + rate * (end - spot).days / 360.0)

        solve(end, deposit)
    for years, rate in swaps:
        maturity = add_months(spot, 12 * years)
        count = 12 * years // fixed_months
        rolls = [add_months(maturity, -fixed_months * k) for k in range(count - 1, 0, -1)]
        dates = [adjust_date(d, MF, WEEKENDS) for d in (*rolls, maturity)]

        def swap(curve: DiscountCurve, dates: list[date] = dates, rate: float = rate) -> float:
            previous, annuity = spot, 0.0
            for payment in dates:
                annuity += _days_30_360(previous, payment) * curve.discount_factor(payment)
                previous = payment
            return rate * annuity - (curve.discount_factor(spot) - curve.discount_factor(dates[-1]))

        solve(dates[-1], swap)
    return DiscountCurve(trade, tuple(nodes))


USD_2009 = date(2009, 5, 21)


@pytest.fixture(scope="module")
def usd_2009_curve() -> DiscountCurve:
    return isda_curve(
        USD_2009,
        list(zip((1, 2, 3, 6, 9, 12), (0.003081, 0.005525, 0.007163, 0.012413, 0.014, 0.015488))),
        list(
            zip(
                (2, 3, 4, 5, 6, 7, 8, 9, 10, 12, 15, 20, 25, 30),
                (
                    0.011907,
                    0.01699,
                    0.021198,
                    0.02444,
                    0.026937,
                    0.028967,
                    0.030504,
                    0.031719,
                    0.03279,
                    0.034535,
                    0.036217,
                    0.036981,
                    0.037246,
                    0.037605,
                ),
            )
        ),
        6,
    )


# ---------------------------------------------------------------- external goldens

# Source: QuantLib test-suite/creditdefaultswap.cpp, test case "testIsdaEngine"
# (github.com/lballabio/QuantLib, master, fetched 2026-10-17). Trade date 21 May 2009,
# USD curve from "Markit published rates", notional 10,000,000, running coupon 100 bp,
# quoted (conventional) spreads 10 bp and 1000 bp, recovery 20% and 40%. The values,
# labelled ``markitValues`` there, are the clean upfront the protection buyer pays at
# cash settlement (``notional * fairUpfront``), as output by the ISDA Standard Model.
MARKIT_2009 = {
    date(2010, 6, 20): (-97798.29358, -97776.11889, 914971.5977, 894985.6298),
    date(2011, 6, 20): (-186921.3594, -186839.8148, 1646623.672, 1579803.626),
    date(2012, 6, 20): (-274298.9203, -274122.4725, 2279730.93, 2147972.527),
    date(2016, 6, 20): (-592420.2297, -591571.2294, 3993550.206, 3545843.418),
    date(2019, 6, 20): (-797501.1422, -795915.9787, 4702034.688, 4042340.999),
}
QUOTED_CASES = ((0.001, 0.2), (0.001, 0.4), (0.1, 0.2), (0.1, 0.4))


@pytest.mark.parametrize("maturity", sorted(MARKIT_2009))
def test_isda_standard_model_upfronts_match_markit_values(
    usd_2009_curve: DiscountCurve, maturity: date
) -> None:
    for (spread, recovery), expected in zip(QUOTED_CASES, MARKIT_2009[maturity]):
        upfront = upfront_from_spread(
            spread,
            0.01,
            valuation_date=USD_2009,
            maturity_date=maturity,
            recovery_rate=recovery,
            discount_curve=usd_2009_curve,
            convention=ISDA,
        )
        # Published to ten significant digits; the model reproduces every one of them.
        assert upfront * 1e7 == pytest.approx(expected, rel=1e-9, abs=5e-4)


def test_markit_upfronts_round_trip_to_their_quoted_spreads(usd_2009_curve: DiscountCurve) -> None:
    for maturity, values in MARKIT_2009.items():
        for (spread, recovery), expected in zip(QUOTED_CASES, values):
            back = spread_from_upfront(
                expected / 1e7,
                0.01,
                valuation_date=USD_2009,
                maturity_date=maturity,
                recovery_rate=recovery,
                discount_curve=usd_2009_curve,
                convention=ISDA,
            )
            assert back == pytest.approx(spread, abs=1e-9)


def test_isda_reconciliation_record_with_negative_rates() -> None:
    """QuantLib test case ``testIsdaCalculatorReconcileSingleQuote`` (same source):
    trade 26 July 2021, EUR curve from Markit rates, maturity 20 June 2026, coupon
    100 bp, conventional spread 67.13 bp, recovery 40%, notional 1,000,000; Markit
    value -16070.7 (the clean value on the trade date) and accrual 1000."""
    trade = date(2021, 7, 26)
    curve = isda_curve(
        trade,
        list(zip((1, 3, 6, 12), (-0.0056, -0.00544, -0.00519, -0.00493))),
        list(
            zip(
                (2, 3, 4, 5, 6, 7, 8, 9, 10, 12, 15, 20, 30),
                (
                    -0.00482,
                    -0.00442,
                    -0.00399,
                    -0.00352,
                    -0.00297,
                    -0.00237,
                    -0.00176,
                    -0.00114,
                    -0.00054,
                    0.00057,
                    0.00188,
                    0.00294,
                    0.00282,
                ),
            )
        ),
        12,
    )
    maturity = date(2026, 6, 20)
    hazard = flat_hazard_from_spread(
        0.006713,
        valuation_date=trade,
        maturity_date=maturity,
        recovery_rate=0.4,
        discount_curve=curve,
        convention=ISDA,
    )
    dates = cds_trade_dates(trade, ISDA, maturity_date=maturity)
    assert dates.cash_settlement_date == date(2021, 7, 29)
    assert dates.accrual_start_date == date(2021, 6, 21)
    contract = CdsContract(
        "EUR-REC",
        "ENTITY",
        "EUR",
        CdsSide.BUYER,
        1e6,
        0.01,
        dates.accrual_start_date,
        maturity,
        ISDA,
    )
    value = value_cds(
        contract,
        valuation_date=trade,
        discount_curve=curve,
        hazard_curve=HazardCurve.flat(trade, hazard),
        recovery_rate=0.4,
    )
    assert value.clean_pv == pytest.approx(-16070.7, abs=0.05)
    assert value.accrued_premium == pytest.approx(-1000.0, abs=1e-9)
    assert value.par_spread == pytest.approx(0.006713, abs=1e-12)


# Source: QuantLib test case ``testAccrualRebateAmounts`` (same file): "accrual values
# are taken from various test results on the ISDA CDS model website" for a 10,000,000
# contract with 100 bp coupon maturing 20 June 2014, keyed by trade date.
ACCRUAL_2009 = {
    date(2009, 3, 18): 24166.67,
    date(2009, 3, 19): 0.00,
    date(2009, 3, 20): 277.78,
    date(2009, 3, 23): 1111.11,
    date(2009, 6, 19): 25555.56,
    date(2009, 6, 20): 25833.33,
    date(2009, 6, 21): 0.00,
    date(2009, 6, 22): 277.78,
    date(2014, 6, 18): 25277.78,
    date(2014, 6, 19): 25555.56,
}


@pytest.mark.parametrize("trade", sorted(ACCRUAL_2009))
def test_standard_accrued_premium_matches_isda_test_results(trade: date) -> None:
    maturity = date(2014, 6, 20)
    dates = cds_trade_dates(trade, ISDA, maturity_date=maturity)
    contract = CdsContract(
        "ACC", "ENTITY", "USD", CdsSide.BUYER, 1e7, 0.01, dates.accrual_start_date, maturity, ISDA
    )
    value = value_cds(
        contract,
        valuation_date=trade,
        discount_curve=DiscountCurve.flat(trade, 0.02),
        hazard_curve=HazardCurve.flat(trade, 0.02),
        recovery_rate=0.4,
    )
    assert -value.accrued_premium == pytest.approx(ACCRUAL_2009[trade], abs=0.005)


# ---------------------------------------------------------------- dates and schedule


def test_imm_dates_and_standard_maturities() -> None:
    assert previous_imm_date(date(2009, 5, 21)) == date(2009, 3, 20)
    assert previous_imm_date(date(2009, 3, 20)) == date(2009, 3, 20)
    assert previous_imm_date(date(2009, 3, 19)) == date(2008, 12, 20)
    assert previous_imm_date(date(2009, 1, 5)) == date(2008, 12, 20)
    semi: Literal["semiannual"] = "semiannual"
    assert standard_cds_maturity(date(2016, 3, 21), 60, roll=semi) == date(2021, 6, 20)
    assert standard_cds_maturity(date(2016, 9, 19), 60, roll=semi) == date(2021, 6, 20)
    assert standard_cds_maturity(date(2016, 9, 20), 60, roll=semi) == date(2021, 12, 20)
    assert standard_cds_maturity(date(2016, 3, 19), 60, roll=semi) == date(2020, 12, 20)
    assert standard_cds_maturity(date(2016, 7, 1), 60, roll="quarterly") == date(2021, 9, 20)
    assert standard_cds_maturity(date(2016, 6, 20), 12, roll="quarterly") == date(2017, 9, 20)
    with pytest.raises(InvalidTerms):
        standard_cds_maturity(date(2016, 7, 1), 7, roll=semi)
    with pytest.raises(InvalidTerms):
        standard_cds_maturity(date(2016, 7, 1), 12, roll="monthly")  # type: ignore[arg-type]


def test_premium_schedule_adjusts_payments_and_includes_the_maturity_day() -> None:
    periods = cds_premium_schedule(date(2009, 3, 20), date(2010, 6, 20), ISDA)
    starts = [p.accrual_start for p in periods]
    assert starts == [
        date(2009, 3, 20),
        date(2009, 6, 22),  # 20 June 2009 is a Saturday
        date(2009, 9, 21),
        date(2009, 12, 21),
        date(2010, 3, 22),
    ]
    last = periods[-1]
    assert last.accrual_end == date(2010, 6, 21)  # maturity + 1 day
    assert last.payment_date == date(2010, 6, 21)  # Sunday maturity paid Monday
    assert last.year_fraction == pytest.approx(91 / 360)
    assert periods[0].year_fraction == pytest.approx(94 / 360)
    stub = cds_premium_schedule(date(2009, 5, 1), date(2009, 12, 20), ISDA)
    assert stub[0].accrual_start == date(2009, 5, 1)
    assert stub[0].accrual_end == date(2009, 6, 22)


def test_trade_dates_follow_the_convention() -> None:
    dates = cds_trade_dates(USD_2009, ISDA, maturity_date=date(2014, 6, 20))
    assert dates.step_in_date == date(2009, 5, 22)
    assert dates.cash_settlement_date == date(2009, 5, 26)
    assert dates.accrual_start_date == date(2009, 3, 20)
    monthly = replace(ISDA, payment_frequency_months=1)
    with pytest.raises(UnsupportedProduct):
        cds_trade_dates(USD_2009, monthly, maturity_date=date(2014, 6, 20))
    bounded = BusinessCalendar(
        version="bounded", valid_from=date(2009, 1, 1), valid_to=date(2009, 5, 24)
    )
    with pytest.raises(CalendarRangeError):
        cds_trade_dates(
            USD_2009, CdsConvention.isda_standard(bounded), maturity_date=date(2014, 6, 20)
        )


# ---------------------------------------------------------------- hazard curve


def test_hazard_curve_survival_is_exponential_in_act_365_time() -> None:
    start = date(2026, 1, 1)
    flat = HazardCurve.flat(start, 0.03)
    assert flat.survival_probability(start) == 1.0
    assert flat.survival_probability(date(2027, 1, 1)) == pytest.approx(math.exp(-0.03))
    curve = HazardCurve(start, ((date(2027, 1, 1), 0.01), (date(2029, 1, 1), 0.05)))
    two_years = (date(2029, 1, 1) - date(2027, 1, 1)).days / 365.0
    assert curve.survival_probability(date(2029, 1, 1)) == pytest.approx(
        math.exp(-0.01 - 0.05 * two_years)
    )
    beyond = (date(2030, 1, 1) - date(2027, 1, 1)).days / 365.0
    assert curve.survival_probability(date(2030, 1, 1)) == pytest.approx(
        math.exp(-0.01 - 0.05 * beyond)
    )
    assert curve.hazard_rate(date(2026, 6, 1)) == 0.01
    assert curve.hazard_rate(date(2028, 6, 1)) == 0.05
    with pytest.raises(InvalidTerms):
        HazardCurve(start, ((date(2027, 1, 1), -0.01),))
    with pytest.raises(InvalidTerms):
        curve.survival_probability(date(2025, 12, 31))
    with pytest.raises(UnsupportedProduct):
        HazardCurve(start, ((date(2027, 1, 1), 0.01),), DayCount.ACT_ACT_ISDA)


# ---------------------------------------------------------------- analytical cases

VAL = date(2026, 10, 15)


def _contract(
    *,
    side: CdsSide = CdsSide.BUYER,
    coupon: float = 0.01,
    maturity: date | None = None,
    accrual_start: date | None = None,
    convention: CdsConvention = ISDA,
    notional: float = 1e7,
) -> CdsContract:
    maturity = maturity or standard_cds_maturity(VAL, 60, roll="semiannual")
    if accrual_start is None:
        accrual_start = cds_trade_dates(VAL, convention, maturity_date=maturity).accrual_start_date
    return CdsContract(
        "CDS-1", "ENTITY", "USD", side, notional, coupon, accrual_start, maturity, convention
    )


def test_exact_credit_triangle_at_zero_rates() -> None:
    """Zero rates, flat hazard, exact accrual on default, accrual from the step-in date:
    expected premium time equals expected protection exactly, so the par spread is
    ``hazard * (1 - R) * 360 / 365`` (ACT/360 premium on ACT/365F hazard time)."""
    exact = replace(ISDA, accrual_on_default=AccrualOnDefault.EXACT)
    for hazard, recovery in ((0.02, 0.4), (0.15, 0.25), (1.0, 0.0)):
        contract = _contract(convention=exact, accrual_start=VAL + timedelta(days=1))
        value = value_cds(
            contract,
            valuation_date=VAL,
            discount_curve=DiscountCurve.flat(VAL, 0.0),
            hazard_curve=HazardCurve.flat(VAL, hazard),
            recovery_rate=recovery,
        )
        assert value.par_spread == pytest.approx(hazard * (1 - recovery) * 360 / 365, rel=1e-12)


def test_credit_triangle_approximation_with_standard_conventions() -> None:
    curve = DiscountCurve.flat(VAL, 0.05)
    maturity = standard_cds_maturity(VAL, 60, roll="semiannual")
    for spread, recovery in ((0.005, 0.4), (0.02, 0.4), (0.05, 0.25)):
        hazard = flat_hazard_from_spread(
            spread,
            valuation_date=VAL,
            maturity_date=maturity,
            recovery_rate=recovery,
            discount_curve=curve,
            convention=ISDA,
        )
        assert hazard == pytest.approx(spread / (1 - recovery) * 365 / 360, rel=0.01)


def test_zero_hazard_limit() -> None:
    curve = DiscountCurve.flat(VAL, 0.08)
    contract = _contract(coupon=0.05)
    value = value_cds(
        contract,
        valuation_date=VAL,
        discount_curve=curve,
        hazard_curve=HazardCurve.flat(VAL, 0.0),
        recovery_rate=0.4,
    )
    periods = cds_premium_schedule(contract.accrual_start_date, contract.maturity_date, ISDA)
    annuity = math.fsum(p.year_fraction * curve.discount_factor(p.payment_date) for p in periods)
    assert value.protection_leg_pv == 0.0
    assert value.par_spread == 0.0
    assert value.premium_leg_pv == pytest.approx(1e7 * 0.05 * annuity, rel=1e-13)
    assert value.pv == pytest.approx(-1e7 * 0.05 * annuity, rel=1e-13)
    assert (
        flat_hazard_from_spread(
            0.0,
            valuation_date=VAL,
            maturity_date=contract.maturity_date,
            recovery_rate=0.4,
            discount_curve=curve,
            convention=ISDA,
        )
        == 0.0
    )


def test_contract_at_its_par_spread_has_zero_clean_value() -> None:
    curve = DiscountCurve.flat(VAL, 0.12)
    hazard = HazardCurve(VAL, ((date(2028, 12, 20), 0.02), (date(2031, 12, 20), 0.04)))
    maturity = date(2031, 12, 20)
    par = cds_par_spread(
        hazard,
        valuation_date=VAL,
        maturity_date=maturity,
        recovery_rate=0.35,
        discount_curve=curve,
        convention=ISDA,
    )
    value = value_cds(
        _contract(coupon=par, maturity=maturity),
        valuation_date=VAL,
        discount_curve=curve,
        hazard_curve=hazard,
        recovery_rate=0.35,
    )
    assert value.clean_pv == pytest.approx(0.0, abs=1e-7)
    assert value.points_upfront == pytest.approx(0.0, abs=1e-15)
    # Dirty value is the accrued premium the buyer owes, discounted from cash settlement.
    assert value.pv == pytest.approx(
        value.accrued_premium * curve.discount_factor(value.cash_settlement_date), rel=1e-9
    )


def test_buyer_and_seller_are_exact_mirrors() -> None:
    curve = DiscountCurve.flat(VAL, 0.1)
    hazard = HazardCurve.flat(VAL, 0.03)
    buyer = value_cds(
        _contract(),
        valuation_date=VAL,
        discount_curve=curve,
        hazard_curve=hazard,
        recovery_rate=0.4,
    )
    seller = value_cds(
        _contract(side=CdsSide.SELLER),
        valuation_date=VAL,
        discount_curve=curve,
        hazard_curve=hazard,
        recovery_rate=0.4,
    )
    for name in ("pv", "clean_pv", "cash_settlement_amount", "accrued_premium"):
        assert getattr(seller, name) == -getattr(buyer, name)
    for name in ("par_spread", "risky_annuity", "points_upfront", "protection_leg_pv"):
        assert getattr(seller, name) == getattr(buyer, name)
    assert buyer.accrued_premium < 0.0 < seller.accrued_premium


def test_cash_settlement_amount_is_pv_forward_to_cash_settlement() -> None:
    curve = DiscountCurve.flat(VAL, 0.1)
    value = value_cds(
        _contract(coupon=0.05),
        valuation_date=VAL,
        discount_curve=curve,
        hazard_curve=HazardCurve.flat(VAL, 0.03),
        recovery_rate=0.4,
    )
    df = curve.discount_factor(value.cash_settlement_date)
    assert value.cash_settlement_amount == pytest.approx(value.pv / df, rel=1e-14)
    assert value.points_upfront * 1e7 == pytest.approx(value.clean_pv / df, rel=1e-12)


def test_half_day_bias_adds_half_a_day_of_accrual_on_default() -> None:
    curve = DiscountCurve.flat(VAL, 0.0)
    hazard = HazardCurve.flat(VAL, 0.05)

    def premium(mode: AccrualOnDefault) -> float:
        contract = _contract(convention=replace(ISDA, accrual_on_default=mode))
        return value_cds(
            contract,
            valuation_date=VAL,
            discount_curve=curve,
            hazard_curve=hazard,
            recovery_rate=0.4,
        ).premium_leg_pv

    none, exact, biased = (premium(m) for m in AccrualOnDefault)
    assert none < exact < biased
    # At zero rates the bias is half a day of coupon on every default in the protection
    # window, from the start of the step-in day to the end of the maturity day.
    default_probability = 1.0 - hazard.survival_probability(_contract().maturity_date)
    assert biased - exact == pytest.approx(0.5 / 360 * 0.01 * 1e7 * default_probability, rel=1e-9)


# ---------------------------------------------------------------- bootstrap

MATURITIES = tuple(standard_cds_maturity(VAL, 12 * y, roll="semiannual") for y in (1, 3, 5, 7, 10))


def _quotes() -> tuple[CdsSpreadQuote | CdsUpfrontQuote, ...]:
    return (
        CdsSpreadQuote(MATURITIES[0], 0.008),
        CdsSpreadQuote(MATURITIES[1], 0.012),
        CdsUpfrontQuote(MATURITIES[2], 0.01, 0.035),
        CdsSpreadQuote(MATURITIES[3], 0.022),
        CdsUpfrontQuote(MATURITIES[4], 0.05, -0.12),
    )


def test_bootstrap_reprices_spread_and_upfront_quotes() -> None:
    curve = DiscountCurve.flat(VAL, 0.15)
    request = CdsCurveRequest(VAL, _quotes(), 0.4, ISDA)
    hazard = bootstrap_hazard_curve(request, curve)
    assert hazard.node_dates == MATURITIES
    assert all(rate > 0 for rate in hazard.hazard_rates)
    common: dict[str, Any] = {"valuation_date": VAL, "recovery_rate": 0.4, "discount_curve": curve}
    for quote in request.quotes:
        if isinstance(quote, CdsSpreadQuote):
            par = cds_par_spread(hazard, maturity_date=quote.maturity, convention=ISDA, **common)
            assert par == pytest.approx(quote.par_spread, abs=1e-12)
        else:
            contract = _contract(coupon=quote.coupon, maturity=quote.maturity)
            value = value_cds(contract, hazard_curve=hazard, **common)
            assert value.points_upfront == pytest.approx(quote.upfront, abs=1e-12)


def test_single_flat_quote_gives_a_flat_curve() -> None:
    curve = DiscountCurve.flat(VAL, 0.1)
    hazard = flat_hazard_from_spread(
        0.02,
        valuation_date=VAL,
        maturity_date=MATURITIES[2],
        recovery_rate=0.4,
        discount_curve=curve,
        convention=ISDA,
    )
    par = cds_par_spread(
        HazardCurve.flat(VAL, hazard),
        valuation_date=VAL,
        maturity_date=MATURITIES[2],
        recovery_rate=0.4,
        discount_curve=curve,
        convention=ISDA,
    )
    assert par == pytest.approx(0.02, abs=1e-13)


def test_upfront_spread_and_hazard_round_trips() -> None:
    curve = DiscountCurve.flat(VAL, 0.07)
    common: dict[str, Any] = {
        "valuation_date": VAL,
        "maturity_date": MATURITIES[2],
        "recovery_rate": 0.4,
        "discount_curve": curve,
        "convention": ISDA,
    }
    for spread in (0.0005, 0.01, 0.05, 0.3):
        for coupon in (0.01, 0.05):
            upfront = upfront_from_spread(spread, coupon, **common)
            assert spread_from_upfront(upfront, coupon, **common) == pytest.approx(
                spread, abs=1e-11
            )
            hazard_a = flat_hazard_from_spread(spread, **common)
            hazard_b = flat_hazard_from_upfront(upfront, coupon, **common)
            assert hazard_a == pytest.approx(hazard_b, rel=1e-10)
            # Spread above the coupon: the buyer pays upfront; at the coupon, nothing.
            if spread == coupon:
                assert upfront == pytest.approx(0.0, abs=1e-13)
            else:
                assert (upfront > 0) == (spread > coupon)


def test_bootstrap_rejections() -> None:
    curve = DiscountCurve.flat(VAL, 0.1)
    with pytest.raises(InvalidTerms, match="negative hazard"):
        bootstrap_hazard_curve(
            CdsCurveRequest(VAL, (CdsUpfrontQuote(MATURITIES[2], 0.01, -0.5),), 0.4, ISDA), curve
        )
    with pytest.raises(InvalidTerms):
        CdsCurveRequest(VAL, (), 0.4, ISDA)
    with pytest.raises(InvalidTerms):
        CdsCurveRequest(
            VAL,
            (CdsSpreadQuote(MATURITIES[0], 0.01), CdsSpreadQuote(MATURITIES[0], 0.02)),
            0.4,
            ISDA,
        )
    with pytest.raises(InvalidTerms):
        CdsCurveRequest(VAL, (CdsSpreadQuote(MATURITIES[0], 0.01),), 1.0, ISDA)
    with pytest.raises(ValueError):
        CdsCurveRequest(VAL, (CdsSpreadQuote(MATURITIES[0], 0.01),), math.nan, ISDA)
    with pytest.raises(ValueError):
        CdsSpreadQuote(MATURITIES[0], -0.01)
    with pytest.raises(InvalidTerms):
        CdsUpfrontQuote(MATURITIES[0], 0.01, 1.2)
    with pytest.raises(InvalidTerms):
        CdsCurveRequest(VAL, (CdsSpreadQuote(VAL, 0.01),), 0.4, ISDA)
    with pytest.raises(InvalidTerms, match="dated on the valuation date"):
        bootstrap_hazard_curve(
            CdsCurveRequest(VAL, (CdsSpreadQuote(MATURITIES[0], 0.01),), 0.4, ISDA),
            DiscountCurve.flat(VAL - timedelta(days=1), 0.1),
        )


def test_convention_and_contract_validation() -> None:
    with pytest.raises(UnsupportedProduct):
        replace(ISDA, accrual_day_count=DayCount.ACT_ACT_ISDA)
    with pytest.raises(InvalidTerms):
        replace(ISDA, payment_frequency_months=4)
    with pytest.raises(TypeError):
        replace(ISDA, step_in_days=True)
    with pytest.raises(InvalidTerms):
        replace(ISDA, cash_settlement_days=-1)
    with pytest.raises(InvalidTerms):
        replace(_contract(), currency="usd")
    with pytest.raises(ValueError):
        replace(_contract(), notional=0.0)
    with pytest.raises(ValueError):
        replace(_contract(), coupon=math.inf)
    with pytest.raises(InvalidTerms):
        replace(_contract(), accrual_start_date=date(2040, 1, 1))
    with pytest.raises(FrozenInstanceError):
        _contract().notional = 1.0  # type: ignore[misc]
    matured = _contract(maturity=date(2026, 9, 20), accrual_start=date(2026, 6, 22))
    with pytest.raises(InvalidTerms, match="matured"):
        value_cds(
            matured,
            valuation_date=VAL,
            discount_curve=DiscountCurve.flat(VAL, 0.1),
            hazard_curve=HazardCurve.flat(VAL, 0.01),
            recovery_rate=0.4,
        )
    assert ISDA.identifier.startswith("cds:3M:FOLLOWING:ACT/360:stepin+1d:cash+3bd:")


# ---------------------------------------------------------------- risk


def _engine(
    *,
    side: CdsSide = CdsSide.BUYER,
    coupon: float = 0.01,
    discount: DiscountCurve | CurveBootstrapRequest | None = None,
    quotes: tuple[CdsSpreadQuote | CdsUpfrontQuote, ...] | None = None,
    notional: float = 1e7,
    caches: tuple[ShockedCurveCache, CdsCurveCache] | None = None,
) -> CdsRiskEngine:
    request = CdsRiskRequest(
        _contract(side=side, coupon=coupon, notional=notional),
        VAL,
        DiscountCurve.flat(VAL, 0.12) if discount is None else discount,
        CdsCurveRequest(VAL, _quotes() if quotes is None else quotes, 0.4, ISDA),
    )
    if caches is None:
        return CdsRiskEngine(request)
    return CdsRiskEngine(request, caches[0], caches[1])


def test_risk_signs_and_identities() -> None:
    buyer = _engine().risk()
    seller = _engine(side=CdsSide.SELLER).risk()
    assert buyer.model_version == CDS_RISK_MODEL_VERSION
    assert buyer.valuation.model_version == CDS_MODEL_VERSION
    assert buyer.cs01 > 0.0 > seller.cs01
    assert buyer.cs01 == pytest.approx(-seller.cs01, rel=1e-12)
    assert buyer.dv01 == pytest.approx(-seller.dv01, rel=1e-9)
    assert buyer.recovery01 == pytest.approx(-seller.recovery01, rel=1e-9)
    assert buyer.dv01 == pytest.approx(math.fsum(b.dv01 for b in buyer.buckets), rel=1e-15)
    # Total = sum of buckets, up to the finite-bump curvature of the parallel reprice.
    assert buyer.parallel_dv01 == pytest.approx(buyer.dv01, rel=1e-6)
    quote_sum = math.fsum(q.cs01 for q in buyer.quote_buckets)
    assert quote_sum == pytest.approx(buyer.cs01, rel=1e-4)
    # Quotes beyond the contract's maturity carry no risk.
    assert buyer.quote_buckets[-1].cs01 == pytest.approx(0.0, abs=1e-9)
    assert buyer.quote_buckets[-2].cs01 == pytest.approx(0.0, abs=1e-9)
    assert [b.bucket for b in buyer.buckets] == list(RISK_BUCKETS)
    # Beyond 7Y the contract has no rate exposure.
    assert all(b.dv01 == 0.0 for b in buyer.buckets[8:])


def test_cs01_against_a_risky_annuity_estimate() -> None:
    """For a flat curve, d(clean PV)/d(spread) is close to notional * clean annuity."""
    engine = _engine(quotes=(CdsSpreadQuote(MATURITIES[2], 0.02),), coupon=0.02)
    risk = engine.risk()
    estimate = 1e7 * engine.valuation.risky_annuity * 1e-4
    assert risk.cs01 == pytest.approx(estimate, rel=0.01)


def test_bump_size_convergence() -> None:
    engine = _engine()
    values = [engine.risk(bump_bp=h) for h in (8.0, 4.0, 2.0, 1.0)]
    cs = [v.cs01 for v in values]
    # Central differences: the error shrinks by about 4 per halving of the bump.
    for wide, mid, narrow in zip(cs, cs[1:], cs[2:]):
        assert abs(mid - narrow) == pytest.approx(abs(wide - mid) / 4.0, rel=0.05)
    assert abs(cs[-2] - cs[-1]) < 1e-5 * abs(cs[-1])
    # This contract's rate risk is nearly linear: every bump agrees to bootstrap precision.
    dv = [v.parallel_dv01 for v in values]
    assert max(dv) - min(dv) < 1e-6 * abs(dv[-1])
    forward = engine.risk(difference="forward")
    central = engine.risk()
    assert forward.cs01 == pytest.approx(central.cs01, rel=1e-3)
    assert forward.difference == "forward"
    with pytest.raises(ValueError):
        engine.risk(difference="backward")  # type: ignore[arg-type]


def test_scenarios_are_exact_and_consistent_with_risk() -> None:
    engine = _engine()
    risk = engine.risk(difference="forward")
    up = engine.reprice(CdsScenario.parallel(0.0, spread_shift_bp=1.0))
    assert up.total_pnl == pytest.approx(risk.cs01, rel=1e-12)
    rates = engine.reprice(CdsScenario.parallel(1.0))
    assert rates.total_pnl == pytest.approx(risk.parallel_dv01, rel=1e-12)
    big = engine.reprice(CdsScenario.parallel(150.0, spread_shift_bp=-40.0, recovery_shift=0.1))
    assert big.total_pnl == pytest.approx(
        big.rates_pnl + big.spread_pnl + big.recovery_pnl + big.interaction_pnl, abs=1e-6
    )
    assert big.base_pv == engine.base_pv
    rec = engine.reprice(CdsScenario.parallel(recovery_shift=0.01))
    down = engine.reprice(CdsScenario.parallel(recovery_shift=-0.01))
    assert risk.recovery01 == pytest.approx((rec.total_pnl - down.total_pnl) / 2, rel=1e-12)
    with pytest.raises(InvalidTerms):
        engine.reprice(CdsScenario.parallel(recovery_shift=0.7))
    with pytest.raises(InvalidTerms):
        engine.reprice(CdsScenario.parallel(spread_shift_bp=-500.0))
    with pytest.raises(ValueError):
        CdsScenario((1.0,) * 10)


def test_recovery_sensitivity_vanishes_at_par_and_has_the_annuity_sign_off_market() -> None:
    # Quotes held: a contract at its quoted par spread has zero clean value whatever the
    # recovery, so recovery risk is only rounding.
    at_par = _engine(quotes=(CdsSpreadQuote(MATURITIES[2], 0.01),), coupon=0.01).risk()
    assert abs(at_par.recovery01) < 1e-4
    # Off market the buyer's value is about (spread - coupon) * annuity; higher recovery
    # needs a higher hazard to keep the spread, which shortens the annuity.
    off = _engine(quotes=(CdsSpreadQuote(MATURITIES[2], 0.05),), coupon=0.01).risk()
    assert off.recovery01 < -1.0
    cheap = _engine(quotes=(CdsSpreadQuote(MATURITIES[2], 0.002),), coupon=0.01).risk()
    assert cheap.recovery01 > 0.0


def test_second_order_matches_first_order_and_maps_to_desk_factors() -> None:
    engine = _engine()
    second = engine.second_order()
    risk = engine.risk()
    assert second.model_version == CDS_SECOND_ORDER_MODEL_VERSION
    assert second.factors == (*RISK_BUCKETS, CS_FACTOR)
    for i, bucket in enumerate(risk.buckets):
        assert second.gradient_per_bp[i] == pytest.approx(bucket.dv01, rel=1e-9, abs=1e-9)
    assert second.gradient_per_bp[-1] == pytest.approx(risk.cs01, rel=1e-12)
    hessian = second.hessian_per_bp2
    for i in range(12):
        for j in range(12):
            assert hessian[i][j] == hessian[j][i]
    factors = cds_factor_risk(risk, "usd_ois", second)
    assert factors.factors[0] == ir_factor("usd_ois", "1M")
    assert factors.factors[-1] == "CS"
    assert factors.has_second_order
    first = cds_factor_risk(risk, "usd_ois")
    assert first.first_order_by_factor()["CS"] == risk.cs01
    with pytest.raises(InvalidTerms):
        cds_factor_risk(engine.risk(difference="forward"), "usd_ois")
    with pytest.raises(InvalidTerms):
        cds_factor_risk(_engine(notional=2e7).risk(), "usd_ois", second)
    # Taylor from gradient and Hessian approximates a small exact scenario.
    moves = [2.0] * 11 + [3.0]
    taylor = math.fsum(g * x for g, x in zip(second.gradient_per_bp, moves)) + 0.5 * math.fsum(
        moves[i] * hessian[i][j] * moves[j] for i in range(12) for j in range(12)
    )
    exact = engine.reprice(CdsScenario.parallel(2.0, spread_shift_bp=3.0)).total_pnl
    assert taylor == pytest.approx(exact, rel=1e-5)


def test_jump_to_default_sign_follows_bond_convention() -> None:
    buyer = _engine()
    seller = _engine(side=CdsSide.SELLER)
    contract = buyer.request.contract
    accrued = -buyer.valuation.accrued_premium / contract.notional
    jtd = buyer.jump_to_default(recovery_rate=0.4)
    expected = contract.notional * (0.6 - accrued) - buyer.base_pv
    assert jtd.jtd_pnl == pytest.approx(expected, rel=1e-14)
    assert jtd.jtd_pnl > 0.0
    short_credit = seller.jump_to_default(recovery_rate=0.4)
    assert short_credit.jtd_pnl == pytest.approx(-jtd.jtd_pnl, rel=1e-12)
    # Selling protection is long credit, like a bond held: zero recovery loses the notional
    # (less the accrued premium received) on top of the contract value.
    zero = seller.jump_to_default(recovery_rate=0.0)
    assert zero.jtd_pnl < short_credit.jtd_pnl < 0.0
    full = buyer.jump_to_default(recovery_rate=1.0)
    assert full.jtd_pnl == pytest.approx(-contract.notional * accrued - buyer.base_pv)
    assert jtd.default_id != zero.default_id
    with pytest.raises(InvalidTerms):
        buyer.jump_to_default(recovery_rate=1.5)
    with pytest.raises(ValueError):
        buyer.jump_to_default(recovery_rate=math.nan)


def test_ois_quote_basis_and_shared_caches_change_no_number() -> None:
    ois = CurveBootstrapRequest(
        VAL,
        tuple(OisQuote(f"{y}Y", add_months(VAL, 12 * y), 0.14 + 0.002 * y) for y in range(1, 11)),
    )
    alone = _engine(discount=ois)
    caches = (ShockedCurveCache(), CdsCurveCache())
    shared_a = _engine(discount=ois, caches=caches)
    shared_b = _engine(discount=ois, caches=caches, side=CdsSide.SELLER)
    risk = alone.risk()
    assert risk.curve_basis == "ois_quote"
    assert shared_a.risk() == risk
    assert shared_b.risk().cs01 == pytest.approx(-risk.cs01, rel=1e-12)
    assert caches[1].hits > 0
    assert shared_a.base_id == alone.base_id
    assert alone.base_id != _engine(discount=ois, notional=2e7).base_id
    # Buckets with no OIS quote below them carry no risk in quote mode.
    assert risk.buckets[0].dv01 == 0.0


def test_risk_request_rejections() -> None:
    contract = _contract()
    credit = CdsCurveRequest(VAL, _quotes(), 0.4, ISDA)
    with pytest.raises(InvalidTerms):
        CdsRiskRequest(contract, VAL, DiscountCurve.flat(VAL + timedelta(days=1), 0.1), credit)
    with pytest.raises(InvalidTerms):
        CdsRiskRequest(
            contract,
            VAL,
            DiscountCurve.flat(VAL, 0.1),
            replace(credit, valuation_date=VAL + timedelta(days=1)),
        )
    with pytest.raises(TypeError):
        CdsRiskRequest(contract, VAL, HazardCurve.flat(VAL, 0.1), credit)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        CdsRiskEngine(CdsRiskRequest(contract, VAL, DiscountCurve.flat(VAL, 0.1), credit), {})  # type: ignore[arg-type]


# ---------------------------------------------------------------- solver


def test_illinois_solver_contract() -> None:
    result = illinois(lambda x: x**3 - 2.0, 0.0, 2.0, tolerance=1e-15)
    assert result.root == pytest.approx(2.0 ** (1 / 3), abs=1e-14)
    assert result.iterations < 20
    assert illinois(lambda x: x, 0.0, 1.0, tolerance=0.0).root == 0.0
    with pytest.raises(ValueError, match="not bracketed"):
        illinois(lambda x: x * x + 1.0, -1.0, 1.0, tolerance=1e-12)
    with pytest.raises(ValueError, match="finite and ordered"):
        illinois(lambda x: x, 1.0, 0.0, tolerance=1e-12)
    with pytest.raises(ValueError, match="non-finite"):
        illinois(lambda x: math.inf if 0.0 < x < 1.0 else x - 0.5, 0.0, 1.0, tolerance=1e-12)
