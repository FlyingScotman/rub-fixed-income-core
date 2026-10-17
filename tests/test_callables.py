"""Callable and perpetual bonds under explicit exercise assumptions (package 9b)."""

from __future__ import annotations

import math
from dataclasses import replace
from datetime import date
from itertools import pairwise

import pytest

from rub_fixed_income import (
    AccrualBasis,
    CashflowKind,
    CouponPeriod,
    DayCount,
    ExerciseAssumption,
    FixedCouponLeg,
    Instrument,
    InvalidTerms,
    Offer,
    OfferKind,
    PrincipalLeg,
    ProductType,
    RateAccrualCouponLeg,
    RateCouponPeriod,
    Redemption,
    UnsupportedProduct,
    YieldConvention,
    call_analysis,
    call_schedule,
    canonical_sha256,
    exercised_instrument,
    extension_risk,
    from_dict,
    option_adjusted_spread,
    perpetual_current_yield,
    price_to_worst,
    redemption_scenarios,
    resolve_exercise,
    to_dict,
    yield_to_call,
    yield_to_maturity,
    yield_to_worst,
)
from rub_fixed_income.dates import year_fraction

# Annual periods of exactly 365 days from 2029 to 2032; 2032-2033 has 366 days.
STARTS = [date(2029 + i, 1, 1) for i in range(5)]
SETTLE = date(2029, 1, 1)
FIRST_CALL, SECOND_CALL, MATURITY = date(2031, 1, 1), date(2032, 1, 1), date(2033, 1, 1)


def annual(coupon: float, last: date = MATURITY) -> tuple[CouponPeriod, ...]:
    return tuple(CouponPeriod(s, e, e, coupon) for s, e in pairwise(STARTS) if e <= last)


def callable_bond(
    coupon: float = 100.0,
    calls: tuple[Offer, ...] = (
        Offer(FIRST_CALL, OfferKind.CALL, confirmed=False),
        Offer(SECOND_CALL, OfferKind.CALL, confirmed=False),
    ),
    *,
    last_coupon: date = MATURITY,
    redemptions: tuple[Redemption, ...] = (Redemption(MATURITY, 1000.0),),
    offers: tuple[Offer, ...] = (),
) -> Instrument:
    return Instrument(
        "CALLABLE",
        ProductType.FIXED_BOND,
        "RUB",
        1000.0,
        FixedCouponLeg(annual(coupon, last_coupon)),
        PrincipalLeg(redemptions),
        "MOEX_STOCK",
        offers=(*calls, *offers),
    )


def pv(flows: tuple[tuple[date, float], ...], y: float, settle: date = SETTLE) -> float:
    return math.fsum(a / (1.0 + y) ** ((d - settle).days / 365.0) for d, a in flows)


def test_par_bond_called_at_par_yields_its_coupon_rate() -> None:
    bond = callable_bond()
    for call in (FIRST_CALL, SECOND_CALL):
        ytc = yield_to_call(bond, SETTLE, 1000.0, call)
        assert ytc.exercise == "call" and ytc.redemption_date == call
        assert ytc.effective_yield == pytest.approx(0.10, abs=1e-12)
    # Macaulay duration of a two-year par annual bond at 10 %: (1 * 100/1.1 + 2 * 1100/1.21) / 1000.
    two_year = yield_to_call(bond, SETTLE, 1000.0, FIRST_CALL)
    assert two_year.macaulay_duration_years == pytest.approx(
        (100 / 1.1 + 2 * 1100 / 1.21) / 1000, abs=1e-10
    )
    assert two_year.modified_duration_years == pytest.approx(
        two_year.macaulay_duration_years / 1.1, abs=1e-12
    )


def test_yield_to_worst_is_the_minimum_and_picks_the_right_date() -> None:
    bond = callable_bond()
    premium = call_analysis(bond, SETTLE, 1050.0)
    assert [s.redemption_date for s in premium.scenarios] == [FIRST_CALL, SECOND_CALL, MATURITY]
    assert premium.worst.effective_yield == min(s.effective_yield for s in premium.scenarios)
    assert premium.worst.redemption_date == FIRST_CALL  # a premium bond is called early
    assert premium.yield_to_maturity is not None
    assert premium.yield_to_maturity.redemption_date == MATURITY
    discount = call_analysis(bond, SETTLE, 950.0)
    assert discount.worst.redemption_date == MATURITY  # a discount bond runs to maturity
    assert yield_to_worst(bond, SETTLE, 950.0) == discount.worst
    for s in premium.scenarios:
        assert s.effective_yield < 0.10
    for s in discount.scenarios:
        assert s.effective_yield > 0.10


@pytest.mark.parametrize("dirty", [930.0, 1000.0, 1004.0, 1080.0])
def test_price_to_worst_inverts_yield_to_worst(dirty: float) -> None:
    bond = callable_bond()
    worst = yield_to_worst(bond, SETTLE, dirty)
    ptw = price_to_worst(bond, SETTLE, worst.effective_yield)
    assert ptw.dirty_price_rub == pytest.approx(dirty, abs=1e-7)
    assert ptw.accrued_interest_rub == 0.0
    assert ptw.clean_price_pct == pytest.approx(dirty / 10.0, abs=1e-8)
    for scenario in redemption_scenarios(bond, SETTLE):
        flows = tuple((f.payment_date, f.amount) for f in scenario.cashflows)
        assert pv(flows, worst.effective_yield) >= dirty - 1e-7


def test_yield_to_call_reprices_independently_and_handles_a_premium_call() -> None:
    premium_call = callable_bond(calls=(Offer(FIRST_CALL, OfferKind.CALL, False, 102.0),))
    ytc = yield_to_call(premium_call, SETTLE, 990.0, FIRST_CALL)
    flows = ((date(2030, 1, 1), 100.0), (FIRST_CALL, 100.0 + 1020.0))
    assert pv(flows, ytc.effective_yield) == pytest.approx(990.0, abs=1e-8)
    at_par = yield_to_call(callable_bond(), SETTLE, 990.0, FIRST_CALL)
    assert ytc.effective_yield > at_par.effective_yield  # the call premium is paid to the holder
    (scenario,) = (s for s in redemption_scenarios(premium_call, SETTLE) if s.exercise == "call")
    assert scenario.price_pct == 102.0
    assert scenario.cashflows[-1].kind is CashflowKind.OFFER_REDEMPTION
    resolved = resolve_exercise(premium_call, SETTLE, ExerciseAssumption.to_call(FIRST_CALL))
    with pytest.raises(UnsupportedProduct, match="102.0%"):
        exercised_instrument(premium_call, resolved)


def test_extension_risk_signs() -> None:
    bond = callable_bond()
    premium = extension_risk(bond, SETTLE, 1050.0, FIRST_CALL)
    discount = extension_risk(bond, SETTLE, 950.0, FIRST_CALL)
    for risk in (premium, discount):
        assert risk.duration_extension_years > 0.0
        assert risk.macaulay_to_maturity_years > risk.macaulay_to_call_years
        assert risk.maturity_date == MATURITY and risk.call_date == FIRST_CALL
    # Coupon 10 %: above the yield to call of a premium bond, below that of a discount bond.
    assert premium.yield_to_call < 0.10 < discount.yield_to_call
    assert premium.pv_change_if_extended_rub > 0.0
    assert discount.pv_change_if_extended_rub < 0.0
    flows = tuple(
        (f.payment_date, f.amount) for f in bond.cashflows(after=SETTLE, horizon=MATURITY)
    )
    assert premium.pv_change_if_extended_rub == pytest.approx(
        pv(flows, premium.yield_to_call) - 1050.0, abs=1e-8
    )


def test_unknown_coupons_after_the_call_are_rejected_unless_assumed() -> None:
    bond = callable_bond(calls=(Offer(FIRST_CALL, OfferKind.CALL, False),), last_coupon=FIRST_CALL)
    assert yield_to_call(bond, SETTLE, 1000.0, FIRST_CALL).effective_yield == pytest.approx(0.10)
    with pytest.raises(InvalidTerms, match="not known"):
        yield_to_maturity(bond, SETTLE, 1000.0)
    with pytest.raises(InvalidTerms, match="not known"):
        yield_to_worst(bond, SETTLE, 1000.0)
    with pytest.raises(InvalidTerms, match="not known"):
        resolve_exercise(bond, SETTLE, ExerciseAssumption.to_maturity())
    reset = tuple(CouponPeriod(s, e, e, 80.0) for s, e in pairwise(STARTS[2:]))
    ytm = yield_to_maturity(bond, SETTLE, 1000.0, assumed_coupons=reset)
    assert ytm.effective_yield < 0.10  # the assumed reset to 8 % lowers the yield to maturity
    assumption = ExerciseAssumption.to_worst(reset)
    resolved = resolve_exercise(bond, SETTLE, assumption, dirty_price_rub=1000.0)
    assert resolved.redemption_date == MATURITY
    assert resolved.identifier.startswith("exercise:to_worst:coupons-")
    exercised = exercised_instrument(bond, resolved).instrument
    assert exercised.maturity_date == MATURITY and not call_schedule(exercised)
    assert [p.amount for p in exercised.coupon_leg.periods] == [100.0, 100.0, 80.0, 80.0]  # type: ignore[union-attr]


def test_coupon_gaps_mid_period_calls_and_wrong_period_types_are_rejected() -> None:
    gap = Instrument(
        "GAP",
        ProductType.FIXED_BOND,
        "RUB",
        1000.0,
        FixedCouponLeg(
            (
                CouponPeriod(STARTS[0], STARTS[1], STARTS[1], 100.0),
                CouponPeriod(date(2030, 2, 1), STARTS[2], STARTS[2], 90.0),
            )
        ),
        PrincipalLeg((Redemption(STARTS[2], 1000.0),)),
        "MOEX_STOCK",
        offers=(Offer(date(2030, 6, 1), OfferKind.CALL, False),),
    )
    with pytest.raises(InvalidTerms, match="gap"):
        yield_to_maturity(gap, SETTLE, 1000.0)
    with pytest.raises(InvalidTerms, match="inside a coupon period"):
        yield_to_call(gap, SETTLE, 1000.0, date(2030, 6, 1))
    rate_period = RateCouponPeriod(STARTS[4], date(2034, 1, 1), date(2034, 1, 1), 100.0, 0.1)
    with pytest.raises(InvalidTerms, match="CouponPeriod"):
        yield_to_maturity(callable_bond(), SETTLE, 1000.0, assumed_coupons=(rate_period,))
    with pytest.raises(InvalidTerms, match="not a call date"):
        yield_to_call(callable_bond(), SETTLE, 1000.0, MATURITY)
    with pytest.raises(InvalidTerms, match="call date"):
        ExerciseAssumption("to_call")
    with pytest.raises(InvalidTerms, match="call date"):
        ExerciseAssumption("to_worst", FIRST_CALL)
    with pytest.raises(InvalidTerms, match="dirty price"):
        resolve_exercise(callable_bond(), SETTLE, ExerciseAssumption.to_worst())


def perpetual(**kw: object) -> Instrument:
    return Instrument(
        "PERP",
        ProductType.FIXED_BOND,
        "RUB",
        1000.0,
        RateAccrualCouponLeg(
            tuple(RateCouponPeriod(s, e, e, 120.0, 0.12) for s, e in pairwise(STARTS[:4])),
            AccrualBasis.RATE_ACT_365,
        ),
        PrincipalLeg(()),
        "MOEX_STOCK",
        offers=(
            Offer(FIRST_CALL, OfferKind.CALL, False),
            Offer(date(2036, 1, 1), OfferKind.CALL, False),
        ),
        **kw,  # type: ignore[arg-type]
    )


def test_perpetual_is_priced_to_a_named_call_only() -> None:
    perp = perpetual()
    assert perp.is_perpetual
    to_first = yield_to_call(perp, SETTLE, 1000.0, FIRST_CALL)
    assert to_first.effective_yield == pytest.approx(0.12, abs=1e-12)
    with pytest.raises(UnsupportedProduct, match="no maturity"):
        resolve_exercise(perp, SETTLE, ExerciseAssumption.to_maturity())
    with pytest.raises(UnsupportedProduct, match="never being called"):
        resolve_exercise(perp, SETTLE, ExerciseAssumption.to_worst(), dirty_price_rub=1000.0)
    with pytest.raises(UnsupportedProduct, match="never being called"):
        price_to_worst(perp, SETTLE, 0.12)
    # Coupons after 2032 are not known: the 2036 call needs reset terms.
    with pytest.raises(InvalidTerms, match="reset terms"):
        resolve_exercise(perp, SETTLE, ExerciseAssumption.to_call(date(2036, 1, 1)))
    dates = [date(2032 + i, 1, 1) for i in range(5)]
    reset = tuple(RateCouponPeriod(s, e, e, 90.0, 0.09) for s, e in pairwise(dates))
    late = ExerciseAssumption.to_call(date(2036, 1, 1), reset)
    resolved = resolve_exercise(perp, SETTLE, late)
    bond = exercised_instrument(perp, resolved)
    assert bond.instrument.maturity_date == date(2036, 1, 1)
    assert bond.instrument.principal_leg == PrincipalLeg((Redemption(date(2036, 1, 1), 1000.0),))
    assert bond.source_sha256 == canonical_sha256(perp)
    flows = bond.instrument.cashflows(after=SETTLE)
    assert [f.coupon for f in flows] == [120.0] * 3 + [90.0] * 4
    ytc = yield_to_call(perp, SETTLE, 1000.0, date(2036, 1, 1), assumed_coupons=reset)
    assert 0.09 < ytc.effective_yield < 0.12


def test_moex_perpetual_yield_is_the_current_yield() -> None:
    assert perpetual_current_yield(0.15, 96.99) == pytest.approx(0.15 / 0.9699)
    with pytest.raises(ValueError):
        perpetual_current_yield(0.15, 0.0)


def test_option_adjusted_requests_are_rejected_with_a_typed_reason() -> None:
    with pytest.raises(UnsupportedProduct, match="short-rate model") as caught:
        option_adjusted_spread(callable_bond(), SETTLE, 1000.0)
    assert caught.value.code == "UNSUPPORTED_PRODUCT"
    with pytest.raises(UnsupportedProduct, match="short-rate model"):
        ExerciseAssumption("option_adjusted")  # type: ignore[arg-type]


def test_to_maturity_of_a_bond_without_calls_is_the_bond_itself() -> None:
    plain = callable_bond(calls=())
    resolved = resolve_exercise(plain, SETTLE, ExerciseAssumption.to_maturity())
    exercised = exercised_instrument(plain, resolved)
    assert exercised.instrument == plain
    assert canonical_sha256(exercised.instrument) == canonical_sha256(plain)


def test_exercised_call_keeps_earlier_puts_amortization_and_the_call_flows() -> None:
    put = Offer(date(2030, 1, 1), OfferKind.PUT, confirmed=True)
    late_put = Offer(SECOND_CALL, OfferKind.PUT, confirmed=True)
    bond = callable_bond(
        redemptions=(
            Redemption(date(2030, 1, 1), 400.0, CashflowKind.AMORTIZATION),
            Redemption(MATURITY, 600.0),
        ),
        offers=(put, late_put),
    )
    resolved = resolve_exercise(bond, SETTLE, ExerciseAssumption.to_call(FIRST_CALL))
    exercised = exercised_instrument(bond, resolved).instrument
    assert exercised.offers == (put,)
    assert exercised.principal_leg == PrincipalLeg(
        (
            Redemption(date(2030, 1, 1), 400.0, CashflowKind.AMORTIZATION),
            Redemption(FIRST_CALL, 600.0),
        )
    )
    (scenario,) = (s for s in redemption_scenarios(bond, SETTLE) if s.redemption_date == FIRST_CALL)
    assert [(f.payment_date, f.amount) for f in exercised.cashflows(after=SETTLE)] == [
        (f.payment_date, f.amount) for f in scenario.cashflows
    ]
    assert exercised.instrument_id == bond.instrument_id


def test_exercise_assumption_round_trips_through_to_dict() -> None:
    reset = (RateCouponPeriod(STARTS[3], STARTS[4], STARTS[4], 90.0, 0.09),)
    for assumption in (
        ExerciseAssumption.to_maturity(),
        ExerciseAssumption.to_call(FIRST_CALL),
        ExerciseAssumption.to_worst(reset),
    ):
        assert from_dict(ExerciseAssumption, to_dict(assumption)) == assumption
    assert ExerciseAssumption.to_call(FIRST_CALL).identifier == "exercise:to_call:2031-01-01"


def test_durations_use_act_365f_from_settlement() -> None:
    bond = callable_bond()
    ytc = yield_to_call(bond, SETTLE, 1010.0, FIRST_CALL)
    flows = ((date(2030, 1, 1), 100.0), (FIRST_CALL, 1100.0))
    weighted = math.fsum(
        year_fraction(SETTLE, d) * a / (1 + ytc.effective_yield) ** year_fraction(SETTLE, d)
        for d, a in flows
    )
    assert ytc.macaulay_duration_years == pytest.approx(weighted / 1010.0, abs=1e-12)


def test_call_yields_follow_the_instruments_yield_time_basis() -> None:
    act_360 = YieldConvention(day_count=DayCount.ACT_360)
    bond = replace(callable_bond(), yield_convention=act_360)
    ytc = yield_to_call(bond, SETTLE, 1000.0, FIRST_CALL)
    assert ytc.yield_convention == "yield:annual:ACT/360"
    flows = ((date(2030, 1, 1), 100.0), (FIRST_CALL, 1100.0))
    repriced = math.fsum(
        a / (1.0 + ytc.effective_yield) ** ((d - SETTLE).days / 360.0) for d, a in flows
    )
    assert repriced == pytest.approx(1000.0, abs=1e-8)
    assert ytc.effective_yield < 0.10  # the same flows over longer ACT/360 time
    worst = yield_to_worst(bond, SETTLE, 1000.0)
    assert price_to_worst(bond, SETTLE, worst.effective_yield).dirty_price_rub == pytest.approx(
        1000.0, abs=1e-7
    )
    assert yield_to_call(callable_bond(), SETTLE, 1000.0, FIRST_CALL).yield_convention == (
        "yield:annual:ACT/365F"
    )
