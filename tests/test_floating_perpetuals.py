"""Never-called perpetuals with a coupon formula (0.41.0, BACKLOG #127, issue #29).

A RUONIA, a key-rate and a KBD-reset perpetual (``floating_perpetual_data``): the leg
continued past its listed periods to 35 years, then a tail at the last projected coupon,
re-projected on every curve. Values and risk are checked against an independent
revaluation of the leg written out to 100 years without a tail.

Tolerance. Past the explicit periods every curve is flat-forward, so a projected coupon
differs from the last one only by its accrual: a quarter has 89 to 92 days, a half-year
181 to 184. The tail pays the last coupon scaled to the mean step, so each tail coupon is
off by at most the largest relative difference of those day counts (``DAY_SPREAD``) and
the tail by at most its value times that; a sensitivity by that fraction of the tail's
part of it. The 100-year cut itself is below 1e-6 of the price. Measured: 1e-5 of the
price and 7e-4 of CS01 per bucket (RUONIA, compounded on business days), 1e-6 and 5e-6
for the key-rate and KBD-reset perpetuals.
"""

from __future__ import annotations

import time
from dataclasses import replace
from datetime import date

import pytest
from floating_perpetual_data import (
    KBD_RATE,
    NEVER,
    POLICY,
    PRICES,
    RESET,
    instruments,
    kbd_leg,
    kbd_perpetual,
    key_perpetual,
    ruonia_perpetual,
    snapshot,
)
from test_perpetual import materialised
from test_portfolio import AS_OF, PARAMS
from test_portfolio_floaters import KEY_PROJECTION, OIS

from rub_fixed_income import (
    RISK_BUCKETS,
    AccountingCategory,
    AssumptionPolicy,
    BondCashflow,
    BondRiskEngine,
    BondRiskRequest,
    BondScenario,
    CouponPeriod,
    DiscountCurve,
    ExerciseAssumption,
    FloatingCouponLeg,
    FloatingCouponPeriod,
    FloatingLeg,
    Instrument,
    InstrumentAssumption,
    InvalidTerms,
    PortfolioScenario,
    Position,
    TwoCurveBondRiskResult,
    UnsupportedProduct,
    ZSpreadConvention,
    exercised_instrument,
    horizon_portfolio,
    horizon_valuation,
    kbd_discount_curve,
    price_cashflows,
    resolve_exercise,
    value_assumed_portfolio,
    value_portfolio,
    verify_assumed_result,
    yield_never_called,
)
from rub_fixed_income.dates import BusinessCalendar, add_months
from rub_fixed_income.perpetual import (
    FLOATING_EXPLICIT_YEARS,
    PerpetualTail,
    floating_perpetual_tail,
    tail_pv_at_spread,
)

ANNUAL = ZSpreadConvention()
SETTLE = date(2026, 10, 16)
KBD = kbd_discount_curve(PARAMS, zero_shock="annual")
PROJECTION: dict[str, DiscountCurve | None] = {
    "PERP-RUONIA": None,  # single curve: RUONIA OIS projects and discounts
    "PERP-KEY": KEY_PROJECTION,
    "PERP-KBD": KBD,
}
DAY_SPREAD = {"PERP-RUONIA": 3 / 89, "PERP-KEY": 3 / 89, "PERP-KBD": 3 / 181}


def never(bond: Instrument) -> tuple[Instrument, PerpetualTail]:
    resolved = resolve_exercise(bond, SETTLE, NEVER)
    exercised = exercised_instrument(bond, resolved)
    assert exercised.perpetual_tail is not None
    return exercised.instrument, exercised.perpetual_tail


def written_out(bond: Instrument, years: int = 100) -> tuple[FloatingCouponPeriod, ...]:
    """The listed periods continued by hand to ``years`` after settlement, without a tail."""
    leg = bond.coupon_leg
    assert isinstance(leg, FloatingCouponLeg)
    first = leg.periods[0]
    kbd = bond.instrument_id == "PERP-KBD"
    step = 6 if kbd else 3
    out, k = list(leg.periods), len(leg.periods)
    while add_months(first.accrual_start, step * k) < add_months(SETTLE, 12 * years):
        start = add_months(first.accrual_start, step * k)
        end = add_months(first.accrual_start, step * (k + 1))
        anchor = add_months(RESET, 60 * (k // 10)) if kbd else None
        out.append(FloatingCouponPeriod(start, end, end, 1000.0, fixing_anchor=anchor))
        k += 1
    return tuple(out)


def request(bond: Instrument, leg: FloatingLeg, **kw: object) -> BondRiskRequest:
    args: dict[str, object] = {
        "cashflows": (),
        "settlement_date": SETTLE,
        "curve": OIS,
        "spread_convention": ANNUAL,
        "spread_kind": "credit",
        "floating_leg": leg,
        "projection_curve": PROJECTION[bond.instrument_id],
        "quantity": 1000.0,
    }
    args.update(kw)
    return BondRiskRequest(**args)  # type: ignore[arg-type]


def dirty(bond: Instrument) -> float:
    """The quote's dirty price: clean plus the accrued of the coupon in progress."""
    return PRICES[bond.instrument_id] * 10.0 + bond.accrued(SETTLE, snapshot())


def engines(bond: Instrument) -> tuple[BondRiskEngine, BondRiskEngine, float]:
    """The engine with the tail at the quote, the written-out leg at its spread, the bound."""
    continued, tail = never(bond)
    leg = continued.coupon_leg
    assert isinstance(leg, FloatingCouponLeg)
    history = snapshot().point_in_time_fixing(leg.fixing_id)
    ours = BondRiskEngine(
        request(
            bond,
            FloatingLeg(leg.rule, leg.periods, history),
            perpetual_tail=tail,
            dirty_price_rub=dirty(bond),
        )
    )
    periods = written_out(bond)
    assert periods[: len(leg.periods)] == leg.periods  # the same continuation, by hand
    theirs = BondRiskEngine(
        request(
            bond,
            FloatingLeg(leg.rule, periods, history),
            # A floater needs a positive flow: a negligible one at the 100-year end.
            cashflows=(BondCashflow(periods[-1].payment_date, 1e-9),),
            spread_bp=ours.spread_bp,
        )
    )
    base_tail = ours.tail_for(ours.projected_coupons)
    assert base_tail is not None
    tail_value = tail_pv_at_spread(base_tail, ours._curve, SETTLE, ours.spread_bp * 1e-4, ANNUAL)
    return ours, theirs, tail_value * DAY_SPREAD[bond.instrument_id]


def test_the_leg_is_continued_to_35_years_and_by_whole_reset_cycles() -> None:
    bond, tail = never(kbd_perpetual())
    leg = bond.coupon_leg
    assert isinstance(leg, FloatingCouponLeg)
    assert not bond.offers  # the call is gone
    periods = leg.periods
    assert periods[:6] == kbd_leg().periods
    target = add_months(SETTLE, 12 * FLOATING_EXPLICIT_YEARS)
    # The leg ends with the first whole cycle that starts 35 years past settlement.
    assert periods[-20].fixing_anchor < target <= periods[-1].fixing_anchor  # type: ignore[operator]
    # Ten semi-annual periods a cycle, each on its cycle's start; the leg ends a cycle.
    for k, period in enumerate(periods):
        assert period.fixing_anchor == add_months(RESET, 60 * (k // 10))
    assert len(periods) % 10 == 0
    assert periods[-1].accrual_end == add_months(RESET, 6 * len(periods))
    assert tail == PerpetualTail(periods[-1].payment_date, None, period_months=6)
    assert tail.floating
    with pytest.raises(InvalidTerms, match="projected with its floating leg"):
        _ = tail.coupon
    ruonia, quarterly = never(ruonia_perpetual())
    ruonia_leg = ruonia.coupon_leg
    assert isinstance(ruonia_leg, FloatingCouponLeg)
    assert quarterly.period_months == 3
    assert all(p.fixing_anchor is None for p in ruonia_leg.periods)
    assert ruonia_leg.periods[-1].accrual_start >= target > ruonia_leg.periods[-2].accrual_start


@pytest.mark.parametrize("instrument_id", sorted(PRICES))
def test_value_and_risk_reconcile_with_the_leg_written_out_to_100_years(
    instrument_id: str,
) -> None:
    bond = next(i for i in instruments() if i.instrument_id == instrument_id)
    ours, theirs, bound = engines(bond)
    assert ours.base_price_rub == pytest.approx(dirty(bond), rel=1e-12)
    assert 0.0 < bound < 1.0  # under 0.1 % of the price
    assert abs(theirs.base_price_rub - ours.base_price_rub) <= bound
    left = ours.risk(difference="central")
    right = theirs.risk(difference="central")
    # A sensitivity is off by at most the same fraction of the tail's part of it: the tail
    # is ``bound / DAY_SPREAD`` of the price and, starting 35 years out, under ten times
    # as long as the bond (duration above 3 years), so of CS01 at most:
    tolerance = bound / ours.base_price_rub * 10.0 * abs(left.cs01_rub)
    pairs = list(zip(left.buckets, right.buckets, strict=True))
    if isinstance(left, TwoCurveBondRiskResult):
        assert isinstance(right, TwoCurveBondRiskResult)
        pairs += zip(left.projection_buckets, right.projection_buckets, strict=True)
    for a, b in pairs:
        assert abs(a.dv01_rub - b.dv01_rub) <= tolerance, (a, b)
    assert abs(left.cs01_rub - right.cs01_rub) <= tolerance
    scenario = BondScenario((0, 0, 0, 0, 25.0, 0, 0, 0, 40.0, 0, 0), 30.0)
    ours_pnl = ours.reprice(scenario).total_pnl_rub
    assert ours_pnl == pytest.approx(theirs.reprice(scenario).total_pnl_rub, rel=0.01)


def test_kbd_reset_risk_is_short_at_the_reset_fixing_and_long_at_the_forward_tenors() -> None:
    ours, _, _ = engines(kbd_perpetual())
    risk = ours.risk(difference="central")
    assert isinstance(risk, TwoCurveBondRiskResult)
    projection = dict(zip(RISK_BUCKETS, (b.dv01_rub for b in risk.projection_buckets), strict=True))
    # The next reset fixes on 2029-12-10 (3Y) and reads the KBD to 2034 (7Y-10Y); every
    # later reset is short at its fixing and long five years on, the last one long 20Y+.
    assert all(projection[b] == 0.0 for b in ("1M", "3M", "6M", "1Y", "2Y"))
    assert projection["3Y"] < 0.0
    assert min(projection[b] for b in ("7Y", "10Y", "15Y", "20Y+")) > 0.0
    assert sum(projection.values()) > 0.0
    # The current cycle is fixed by history at 15 % + 3.25 %; published coupons are cash.
    coupons = ours.projected_coupons
    assert coupons[0].source == "fixed_by_history"
    assert coupons[0].coupon_rate == pytest.approx(KBD_RATE + 0.0325)
    assert all(c.coupon_rate == pytest.approx(0.1825) for c in coupons[:7])
    assert coupons[7].source == "projected"  # the 2029-12-16 reset
    listed = list(kbd_leg().periods)
    listed[4] = replace(listed[4], published_coupon_rub=95.0)
    bond = replace(kbd_perpetual(), coupon_leg=replace(kbd_leg(), periods=tuple(listed)))
    published, _, _ = engines(bond)
    assert published.projected_coupons[1].source == "published"  # 2027-06-16
    assert published.projected_coupons[1].amount_rub == 95.0


def _position(instrument_id: str) -> Position:
    return Position(
        f"p-{instrument_id}", instrument_id, 1000.0, "CREDIT", AccountingCategory.TRADING, "RUB"
    )


def test_value_portfolio_values_and_risks_the_three_floating_perpetuals() -> None:
    positions = tuple(_position(i) for i in PRICES)
    result = value_portfolio(
        run_id="floating-perpetuals",
        as_of=AS_OF,
        snapshot=snapshot(),
        instruments=instruments(),
        positions=positions,
        policy=POLICY,
        scenarios=(PortfolioScenario("up10", BondScenario.parallel(10.0)),),
    )
    assert result.status == "complete", result.coverage.rejections
    rows = {v.instrument_id: v for v in result.positions}
    for instrument_id, row in rows.items():
        assert row.horizon == "perpetual"
        assert row.exercise_assumption == "exercise:never_called@never"
        bond = next(i for i in instruments() if i.instrument_id == instrument_id)
        assert row.dirty_price_rub == pytest.approx(dirty(bond), rel=1e-12)
        assert row.pv_rub == pytest.approx(row.dirty_price_rub * 1000.0, rel=1e-10)
        ours, _, _ = engines(bond)
        assert row.spread_bp == pytest.approx(ours.spread_bp, abs=1e-9)
        first = row.factor_risk.first_order_by_factor()
        assert first["CS"] < 0.0
        assert row.factor_risk.hessian  # second order computed
        assert row.macaulay_duration_years > 3.0
    key = rows["PERP-KEY"].factor_risk.first_order_by_factor()
    assert all(key[f"IR|KEY_PROJ|{b}"] > 0.0 for b in ("1Y", "5Y", "10Y", "20Y+"))
    kbd = rows["PERP-KBD"].factor_risk.first_order_by_factor()
    assert kbd["IR|RUB_KBD|3Y"] < 0.0 < kbd["IR|RUB_KBD|20Y+"]
    ruonia = rows["PERP-RUONIA"].factor_risk.factors
    assert not any("KEY_PROJ" in f or "RUB_KBD" in f for f in ruonia)  # single curve
    assert result.scenarios


def test_horizon_carry_of_floating_perpetuals() -> None:
    horizon = date(2026, 12, 15)
    result = horizon_portfolio(
        snapshot=snapshot(),
        instruments=instruments(),
        positions=tuple(_position(i) for i in PRICES),
        policy=POLICY,
        horizon_date=horizon,
    )
    assert result.coverage.status == "complete", result.coverage.rejections
    for row in result.positions:
        per_bond = row.per_bond
        assert per_bond.redemption_horizon == "perpetual"
        bond = next(i for i in instruments() if i.instrument_id == row.instrument_id)
        assert per_bond.pv_start_rub == pytest.approx(dirty(bond), rel=1e-10)
        assert per_bond.carry_rub == pytest.approx(
            per_bond.pv_forward_rub + per_bond.received_rub - per_bond.pv_start_rub, abs=1e-9
        )
    # Independent pv_forward of the KBD perpetual: the leg projected on today's KBD, the tail
    # at its last coupon (scaled to the mean step), valued at the horizon settlement.
    bond, tail = never(kbd_perpetual())
    leg = bond.coupon_leg
    assert isinstance(leg, FloatingCouponLeg)
    calendar = BusinessCalendar(version="weekdays")
    discount = kbd_discount_curve(PARAMS)
    carried = horizon_valuation(
        bond,
        discount,
        calendar,
        horizon_date=horizon,
        dirty_price_rub=960.0,
        fixings=snapshot().point_in_time_fixing("KBD_5Y"),
        projection_curve=KBD,
        perpetual_tail=tail,
    )
    at = carried.horizon_settlement_date
    model = FloatingLeg(leg.rule, leg.periods, snapshot().point_in_time_fixing("KBD_5Y"))
    coupons = model.project(KBD, settlement_date=at)
    flows = (
        *((c.payment_date, c.amount_rub) for c in coupons),
        *materialised(tail.projected(coupons[-1]), 250),
    )
    expected = price_cashflows(
        flows, discount, spread=carried.spread_bp * 1e-4, settlement_date=at, convention=ANNUAL
    )
    assert carried.pv_forward_rub == pytest.approx(expected, rel=1e-10)


def test_validation_and_what_stays_unsupported() -> None:
    bond, tail = never(key_perpetual())
    leg = bond.coupon_leg
    assert isinstance(leg, FloatingCouponLeg)
    model = FloatingLeg(leg.rule, leg.periods, snapshot().point_in_time_fixing("KEY_RATE"))
    common = {"dirty_price_rub": 990.0}
    with pytest.raises(ValueError, match="pass amount=None"):
        request(bond, model, perpetual_tail=tail.with_amount(40.0), **common)
    with pytest.raises(ValueError, match="anchor is the leg's last payment date"):
        request(bond, model, perpetual_tail=replace(tail, anchor_date=date(2060, 1, 1)), **common)
    with pytest.raises(ValueError, match="continues a floating leg"):
        BondRiskRequest(
            (BondCashflow(date(2027, 1, 1), 30.0),),
            SETTLE,
            OIS,
            ANNUAL,
            "credit",
            dirty_price_rub=990.0,
            perpetual_tail=tail,
        )
    with pytest.raises(InvalidTerms, match="assumed coupons are for fixed"):
        resolve_exercise(
            key_perpetual(),
            SETTLE,
            ExerciseAssumption.never_called(
                (CouponPeriod(date(2027, 6, 15), date(2027, 9, 15), date(2027, 9, 15), 30.0),)
            ),
        )
    # A reset cycle is the index tenor in whole months.
    odd = replace(kbd_leg(), rule=replace(kbd_leg().rule, index_tenor_years=5.01))
    with pytest.raises(InvalidTerms, match="whole number of months"):
        floating_perpetual_tail(odd, SETTLE)
    # The yield functions, calls and to_worst of a floating perpetual need #28.
    with pytest.raises(UnsupportedProduct):
        yield_never_called(key_perpetual(), SETTLE, 990.0)
    with pytest.raises(UnsupportedProduct, match="callable floaters"):
        resolve_exercise(key_perpetual(), SETTLE, ExerciseAssumption.to_call(date(2031, 9, 15)))


def test_timings_per_never_called_floating_perpetual() -> None:
    """Value with the Hessian, one position at a time (recorded in the CHANGELOG)."""
    for instrument_id in PRICES:
        started = time.perf_counter()
        result = value_portfolio(
            run_id="timing",
            as_of=AS_OF,
            snapshot=snapshot(),
            instruments=instruments(),
            positions=(_position(instrument_id),),
            policy=POLICY,
        )
        assert result.status == "complete"
        assert time.perf_counter() - started < 30.0


def test_the_assumed_book_values_a_floating_perpetual_natively() -> None:
    result = value_assumed_portfolio(
        run_id="assumed",
        as_of=AS_OF,
        snapshot=snapshot(),
        instruments=instruments(),
        positions=(_position("PERP-KBD"),),
        policy=replace(POLICY, exercise_assumptions=()),
        assumptions=AssumptionPolicy(overrides=(InstrumentAssumption("PERP-KBD", exercise=NEVER),)),
    )
    assert result.status == "complete", result.coverage.rejections
    assert verify_assumed_result(result)
    (row,) = result.portfolio.positions
    assert row.horizon == "perpetual"
    assert row.dirty_price_rub == pytest.approx(dirty(kbd_perpetual()), rel=1e-12)
