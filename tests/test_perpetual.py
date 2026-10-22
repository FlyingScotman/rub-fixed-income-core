"""Never-called perpetuals: the endless coupon stream (BACKLOG #125).

Analytic perpetuities on flat curves, the tail against its materialised flows on the
KBD and on shocked curves, the convergence of a truncated stream, the
``never_called`` assumption in the call analytics and in ``value_portfolio``.
"""

from __future__ import annotations

import json
import math
from dataclasses import replace
from datetime import date, timedelta
from itertools import pairwise
from pathlib import Path
from typing import Literal

import pytest
from test_assumed_portfolio import book_positions, book_snapshot, callable_bond, perpetual
from test_portfolio import AS_OF, POLICY, instruments

from rub_fixed_income import (
    CONTINUOUS,
    AssumptionPolicy,
    BondCashflow,
    BondRiskEngine,
    BondRiskRequest,
    BondScenario,
    CouponPeriod,
    DiscountCurve,
    ExerciseAssumption,
    FactorRisk,
    FixedCouponLeg,
    Instrument,
    InstrumentAssumption,
    InvalidTerms,
    Offer,
    OfferKind,
    PortfolioRiskResult,
    ValuationPolicy,
    YieldConvention,
    ZcycParameters,
    ZSpreadConvention,
    call_analysis,
    exercised_instrument,
    from_dict,
    horizon_portfolio,
    kbd_discount_curve,
    perpetual_current_yield,
    portfolio_result_from_json,
    portfolio_result_to_json,
    price_cashflows,
    price_to_worst,
    resolve_exercise,
    solve_z_spread,
    to_dict,
    value_assumed_portfolio,
    value_portfolio,
    verify_portfolio_result,
    yield_never_called,
    yield_to_call,
    yield_to_worst,
)
from rub_fixed_income.dates import add_months
from rub_fixed_income.perpetual import (
    TAIL_SUM_YEARS,
    PerpetualTail,
    perpetual_tail,
    perpetual_yield,
    tail_pv_at_spread,
    tail_pv_at_yield,
)

ANNUAL = ZSpreadConvention()
KBD_FIXTURE = (
    Path(__file__).parent / "fixtures" / "moex_eod_2026-10-20" / "kbd" / "zcyc_2026-10-20.json"
)


def kbd(
    horizon_years: int = 30, zero_shock: Literal["continuous", "annual"] = "continuous"
) -> DiscountCurve:
    row = json.loads(KBD_FIXTURE.read_text(encoding="utf-8"))["params"]["data"][0]
    params = ZcycParameters(
        date.fromisoformat(row[0]), row[2], row[3], row[4], row[5], tuple(row[6:15])
    )
    return kbd_discount_curve(params, horizon_years=horizon_years, zero_shock=zero_shock)


def materialised(
    tail: PerpetualTail, years: int = TAIL_SUM_YEARS
) -> tuple[tuple[date, float], ...]:
    """The tail's flows to ``years`` past its anchor (by default to the end of its sum)."""
    end = add_months(tail.anchor_date, 12 * years)
    out = []
    k = 1
    while (paid := tail.payment_date(k)) <= end:
        out.append((paid, tail.amount))
        k += 1
    return tuple(out)


# --- analytic perpetuities -------------------------------------------------------


def test_tail_at_a_yield_is_the_geometric_perpetuity() -> None:
    anchor = date(2026, 10, 20)
    tail = PerpetualTail(anchor, 60.0, period_days=182)
    y = 0.145
    q = (1.0 + y) ** (-182 / 365)
    assert tail_pv_at_yield(tail, anchor, y, YieldConvention()) == pytest.approx(
        60.0 * q / (1.0 - q), rel=1e-13
    )
    # Macaulay numerator: sum k dt q^k = dt q / (1 - q)^2.
    weighted = tail_pv_at_yield(tail, anchor, y, YieldConvention(), time_weighted=True)
    assert weighted == pytest.approx(60.0 * (182 / 365) * q / (1.0 - q) ** 2, rel=1e-12)


@pytest.mark.parametrize("spread", [0.0, 0.0325, -0.02])
def test_tail_on_a_flat_curve_is_the_geometric_perpetuity(spread: float) -> None:
    anchor = date(2026, 10, 20)
    curve = DiscountCurve.flat(anchor, 0.14)
    tail = PerpetualTail(anchor, 30.0, period_days=91)
    dt = 91 / 365
    # Annual spread over the annual zero rate: (e^r + s)^-t.
    q = (math.exp(0.14) + spread) ** -dt
    assert tail_pv_at_spread(tail, curve, anchor, spread, ANNUAL) == pytest.approx(
        30.0 * q / (1.0 - q), rel=1e-12
    )
    # Continuous spread: e^-(r + s) t.
    q = math.exp(-(0.14 + spread) * dt)
    continuous = ZSpreadConvention(compounding=CONTINUOUS)
    assert tail_pv_at_spread(tail, curve, anchor, spread, continuous) == pytest.approx(
        30.0 * q / (1.0 - q), rel=1e-12
    )


def test_perpetual_yield_inverts_the_perpetuity_and_matches_the_current_yield() -> None:
    """At a coupon date, P = a q / (1 - q): y = ((P + a) / P)^(365 / 182) - 1."""
    anchor = date(2026, 10, 20)
    tail = PerpetualTail(anchor, 60.0, period_days=182)
    for dirty in (700.0, 850.0, 1000.0, 1080.0):
        solved = perpetual_yield(anchor, dirty, (), tail, YieldConvention())
        expected = ((dirty + 60.0) / dirty) ** (365 / 182) - 1.0
        assert solved.effective_yield == pytest.approx(expected, rel=1e-11)
        # Macaulay duration of a perpetuity: dt / (1 - q).
        q = (1.0 + expected) ** (-182 / 365)
        assert solved.macaulay_duration_years == pytest.approx((182 / 365) / (1 - q), rel=1e-9)
        # MOEX's current yield (coupon rate over clean price) is the same rate, simple:
        # compounded over the period it gives the stream's yield.
        current = perpetual_current_yield(60.0 / 1000.0 * 365 / 182, dirty / 10.0)
        compounded = (1.0 + current * 182 / 365) ** (365 / 182) - 1.0
        assert compounded == pytest.approx(solved.effective_yield, rel=1e-11)


def test_a_divergent_stream_is_worth_infinity_and_the_solvers_step_past_it() -> None:
    anchor = date(2026, 10, 20)
    curve = DiscountCurve.flat(anchor, 0.05)
    tail = PerpetualTail(anchor, 30.0, period_days=91)
    assert tail_pv_at_spread(tail, curve, anchor, -0.30, ANNUAL) == math.inf
    assert tail_pv_at_yield(tail, anchor, -0.01, YieldConvention()) == math.inf
    spread = solve_z_spread((), 900.0, curve, settlement_date=anchor, perpetual_tail=tail)
    priced = price_cashflows((), curve, spread=spread, settlement_date=anchor, perpetual_tail=tail)
    assert priced == pytest.approx(900.0, rel=1e-12)
    with pytest.raises(ValueError, match="settlement_date"), pytest.deprecated_call():
        price_cashflows((), curve, perpetual_tail=tail)  # legacy curve-date valuation


# --- exactness against materialised flows ---------------------------------------


@pytest.mark.parametrize("horizon_years", [30, 10])
def test_tail_equals_its_materialised_flows_on_the_kbd(horizon_years: int) -> None:
    curve = kbd(horizon_years)
    settle = curve.curve_date
    tail = PerpetualTail(date(2031, 3, 15), 29.92, period_months=3)
    for spread in (0.0, 0.025, -0.01):
        exact = price_cashflows(
            materialised(tail), curve, spread=spread, settlement_date=settle, convention=ANNUAL
        )
        assert tail_pv_at_spread(tail, curve, settle, spread, ANNUAL) == pytest.approx(
            exact, rel=1e-12
        )


def test_a_curve_that_is_not_flat_forward_past_30_years_is_read_date_by_date() -> None:
    start = date(2026, 10, 20)
    nodes = tuple(
        (start + timedelta(days=365 * y), math.exp(-(0.14 + 0.001 * (y % 7)) * y))
        for y in (1, 5, 10, 20, 30, 40, 45, 50)
    )
    curve = DiscountCurve(start, nodes)
    tail = PerpetualTail(date(2030, 1, 1), 60.0, period_days=182)
    exact = price_cashflows(materialised(tail), curve, spread=0.02, settlement_date=start)
    assert tail_pv_at_spread(tail, curve, start, 0.02, ANNUAL) == pytest.approx(exact, rel=1e-12)


def listed_and_tail() -> tuple[tuple[BondCashflow, ...], PerpetualTail]:
    listed = tuple(BondCashflow(add_months(date(2026, 12, 15), 3 * k), 29.92) for k in range(20))
    return listed, PerpetualTail(listed[-1].payment_date, 29.92, period_months=3)


@pytest.mark.parametrize("zero_shock", ["continuous", "annual"])
def test_risk_engine_with_a_tail_equals_the_engine_on_materialised_flows(
    zero_shock: Literal["continuous", "annual"],
) -> None:
    """Bucket shocks in continuous zero rates and in annual yields (``rub_ofz_kbd_v2``)."""
    curve = kbd(zero_shock=zero_shock)
    listed, tail = listed_and_tail()
    common = {
        "settlement_date": curve.curve_date,
        "curve": curve,
        "spread_convention": ANNUAL,
        "spread_kind": "credit",
        "dirty_price_rub": 870.0,
        "quantity": 10.0,
    }
    ours = BondRiskEngine(BondRiskRequest(listed, perpetual_tail=tail, **common))  # type: ignore[arg-type]
    # 250 years of explicit flows: the rest is below 1e-12 of the price at these rates,
    # and the explicit Z-spread bracket would overflow on 600 years of flows.
    flows = (*listed, *(BondCashflow(d, a) for d, a in materialised(tail, 250)))
    theirs = BondRiskEngine(BondRiskRequest(flows, **common))  # type: ignore[arg-type]
    assert ours.spread_bp == pytest.approx(theirs.spread_bp, abs=1e-8)
    left = ours.risk(difference="central")
    right = theirs.risk(difference="central")
    for a, b in zip(left.buckets, right.buckets, strict=True):
        assert a.dv01_rub == pytest.approx(b.dv01_rub, rel=1e-8, abs=1e-9)
    assert left.cs01_rub == pytest.approx(right.cs01_rub, rel=1e-8)
    assert left.parallel_dv01_rub == pytest.approx(right.parallel_dv01_rub, rel=1e-8)
    scenario = BondScenario((-50.0, 0, 0, 0, 0, 25.0, 0, 0, 0, 40.0, 100.0), 30.0)
    assert ours.reprice(scenario).total_pnl_rub == pytest.approx(
        theirs.reprice(scenario).total_pnl_rub, rel=1e-8
    )
    hessian = ours.second_order().hessian_rub_per_bp2
    for i, row in enumerate(hessian):
        for j, value in enumerate(row):
            assert value == pytest.approx(hessian[j][i], rel=1e-9, abs=1e-12)
    # Every tail coupon is past 20 years or interpolated to it: the 20Y+ bucket
    # holds a large share of the rate risk, the rest sits on 3Y-15Y.
    share = left.buckets[-1].dv01_rub / left.dv01_rub
    assert 0.15 < share < 0.5
    assert ours.base_id != theirs.base_id


def test_truncating_the_stream_converges_to_the_tail_value() -> None:
    """The owner's convergence study: cutting the stream at H years without a tail.

    On the KBD of 2026-10-20 a quarterly 12 % coupon at 87 % of face: a cut at 30 years
    loses about a point of price and over 10 bp of Z-spread, a cut at 75 years still a
    few hundredths of a bp; the tail is exact at any cut.
    """
    curve = kbd(10)
    settle = curve.curve_date
    listed, tail = listed_and_tail()
    flows = (*listed, *(BondCashflow(d, a) for d, a in materialised(tail, 250)))
    dirty = 870.0
    pairs = tuple((f.payment_date, f.amount) for f in listed)
    z_inf = solve_z_spread(
        pairs, dirty, curve, settlement_date=settle, convention=ANNUAL, perpetual_tail=tail
    )
    full = price_cashflows(pairs, curve, spread=z_inf, settlement_date=settle, perpetual_tail=tail)
    gaps = []
    for years in (20, 30, 50, 75, 100):
        cut = add_months(settle, 12 * years)
        kept = tuple((f.payment_date, f.amount) for f in flows if f.payment_date <= cut)
        z = solve_z_spread(kept, dirty, curve, settlement_date=settle, convention=ANNUAL)
        lost = full - price_cashflows(kept, curve, spread=z_inf, settlement_date=settle)
        gaps.append((years, lost / 10.0, (z - z_inf) * 1e4))
    lost_pct = [g[1] for g in gaps]
    assert lost_pct == sorted(lost_pct, reverse=True)
    by_years = {years: (lost, dz) for years, lost, dz in gaps}
    assert by_years[30][0] > 0.5 and by_years[30][1] < -10.0
    assert abs(by_years[75][1]) < 0.1 and abs(by_years[100][1]) < 0.01


# --- the never_called assumption -----------------------------------------------


def perp(
    coupons: int = 12, amount: float = 30.0, offers: tuple[Offer, ...] | None = None
) -> Instrument:
    base = perpetual()
    dates = [add_months(date(2026, 6, 1), 3 * i) for i in range(coupons + 1)]
    leg = FixedCouponLeg(tuple(CouponPeriod(s, e, e, amount) for s, e in pairwise(dates)))
    return replace(base, coupon_leg=leg, offers=base.offers if offers is None else offers)


SETTLE = date(2026, 10, 16)


def test_never_called_tail_continues_the_last_coupon_at_its_step() -> None:
    bond = perp()
    resolved = resolve_exercise(bond, SETTLE, ExerciseAssumption.never_called())
    assert (resolved.exercise, resolved.redemption_date, resolved.price_pct) == (
        "never",
        None,
        None,
    )
    assert resolved.identifier == "exercise:never_called@never"
    exercised = exercised_instrument(bond, resolved)
    assert exercised.instrument.is_perpetual
    assert not exercised.instrument.offers  # the call is gone
    assert exercised.perpetual_tail == PerpetualTail(date(2029, 6, 1), 30.0, period_months=3)
    days = tuple(
        CouponPeriod(
            date(2026, 1, 1) + timedelta(91 * i),
            date(2026, 1, 1) + timedelta(91 * i + 91),
            date(2026, 1, 1) + timedelta(91 * i + 91),
            29.92,
        )
        for i in range(8)
    )
    assert perpetual_tail(days).period_days == 91
    # A dated callable never called runs to maturity.
    dated = resolve_exercise(callable_bond(), SETTLE, ExerciseAssumption.never_called())
    assert dated.exercise == "maturity"


def test_unknown_coupons_are_not_continued() -> None:
    bond = perp()
    leg = bond.coupon_leg
    assert isinstance(leg, FixedCouponLeg)
    unknown = replace(
        bond, coupon_leg=FixedCouponLeg((*leg.periods[:-1], replace(leg.periods[-1], amount=0.0)))
    )
    with pytest.raises(InvalidTerms, match="not known"):
        resolve_exercise(unknown, SETTLE, ExerciseAssumption.never_called())
    gone = perp(coupons=1)  # the only coupon is paid before settlement
    with pytest.raises(InvalidTerms, match="no coupon of the perpetual is listed"):
        resolve_exercise(gone, SETTLE, ExerciseAssumption.never_called())


@pytest.mark.parametrize(("clean", "worst"), [(85.0, "never"), (106.0, "call")])
def test_never_called_is_the_worst_below_par_and_the_call_above(clean: float, worst: str) -> None:
    bond = perp()
    dirty = clean * 10.0 + bond.accrued(SETTLE)
    analysis = call_analysis(bond, SETTLE, dirty)
    assert [s.exercise for s in analysis.scenarios] == ["call", "never"]
    assert analysis.yield_to_maturity is None
    assert analysis.worst.exercise == worst
    assert yield_to_worst(bond, SETTLE, dirty) == analysis.worst
    never = yield_never_called(bond, SETTLE, dirty)
    assert never == analysis.scenarios[-1]
    assert never.macaulay_duration_years > 5.0
    ptw = price_to_worst(bond, SETTLE, analysis.worst.effective_yield)
    assert ptw.dirty_price_rub == pytest.approx(dirty, rel=1e-10)
    assert ptw.exercise == worst
    resolved = resolve_exercise(bond, SETTLE, ExerciseAssumption.to_worst(), dirty_price_rub=dirty)
    assert resolved.exercise == worst


def test_never_called_doubles_the_duration_of_a_five_year_call() -> None:
    """At 85 % the stream's yield is lower and its duration about twice a 5-year call's."""
    bond = perp(coupons=40, offers=(Offer(date(2031, 6, 1), OfferKind.CALL, False),))
    dirty = 850.0 + bond.accrued(SETTLE)
    never = yield_never_called(bond, SETTLE, dirty)
    called = yield_to_call(bond, SETTLE, dirty, date(2031, 6, 1))
    assert never.effective_yield < called.effective_yield - 0.02
    assert 1.6 < never.macaulay_duration_years / called.macaulay_duration_years < 2.4


# --- value_portfolio -------------------------------------------------------------

BOOK = (*instruments()[:3], callable_bond(), perp())
POSITIONS = book_positions()[:5]


def native(policy: ValuationPolicy) -> PortfolioRiskResult:
    return value_portfolio(
        run_id="eod-2026-10-15",
        as_of=AS_OF,
        snapshot=book_snapshot(),
        instruments=BOOK,
        positions=POSITIONS,
        policy=policy,
    )


NEVER = replace(
    POLICY,
    exercise_assumptions=(
        ("CORP-CALL", ExerciseAssumption.never_called()),
        ("PERP", ExerciseAssumption.never_called()),
    ),
)


def test_value_portfolio_values_a_never_called_perpetual_with_its_tail() -> None:
    result = native(NEVER)
    assert result.status == "complete"
    assert verify_portfolio_result(result)
    data = portfolio_result_to_json(result)
    assert portfolio_result_to_json(portfolio_result_from_json(data)) == data
    assert from_dict(ValuationPolicy, to_dict(NEVER)) == NEVER
    rows = {v.position_id: v for v in result.positions}
    row = rows["p-perp"]
    assert row.horizon == "perpetual"
    assert row.horizon_date == date(2029, 6, 1)  # the last listed coupon: the tail's anchor
    assert row.exercise_assumption == "exercise:never_called@never"
    assert row.pv_rub == pytest.approx(50 * row.dirty_price_rub, rel=1e-12)
    assert row.macaulay_duration_years > 5.0
    assert rows["p-call"].horizon == "maturity"  # a dated callable runs to maturity
    # The same bond under a to_call assumption: shorter, higher-yielding, less risky.
    called = native(
        replace(
            POLICY, exercise_assumptions=(("PERP", ExerciseAssumption.to_call(date(2028, 12, 1))),)
        )
    )
    short = {v.position_id: v for v in called.positions}["p-perp"]
    assert row.effective_yield < short.effective_yield
    assert row.macaulay_duration_years > 2 * short.macaulay_duration_years

    def rates(risk: FactorRisk) -> dict[str, float]:
        return {
            f: v for f, v in zip(risk.factors, risk.first_order, strict=True) if f.startswith("IR|")
        }

    ir = rates(row.factor_risk)
    assert ir[next(f for f in ir if f.endswith("|20Y+"))] < 0.0
    assert not any(v for f, v in rates(short.factor_risk).items() if f.endswith("|20Y+"))
    assert abs(sum(ir.values())) > 2 * abs(sum(rates(short.factor_risk).values()))


def test_a_never_called_perpetual_with_an_open_put_is_valued_to_the_put() -> None:
    put = Offer(date(2027, 12, 1), OfferKind.PUT, True)
    book = (*BOOK[:-1], perp(offers=(*perpetual().offers, put)))
    result = value_portfolio(
        run_id="eod-2026-10-15",
        as_of=AS_OF,
        snapshot=book_snapshot(),
        instruments=book,
        positions=POSITIONS,
        policy=NEVER,
    )
    row = {v.position_id: v for v in result.positions}["p-perp"]
    assert (row.horizon, row.horizon_date) == ("confirmed_put", date(2027, 12, 1))


def test_wrappers_and_horizon_reject_a_never_called_perpetual_with_a_reason() -> None:
    wrapped = value_assumed_portfolio(
        run_id="eod-2026-10-15",
        as_of=AS_OF,
        snapshot=book_snapshot(),
        instruments=BOOK,
        positions=POSITIONS,
        policy=POLICY,
        assumptions=AssumptionPolicy(
            overrides=(InstrumentAssumption("PERP", exercise=ExerciseAssumption.never_called()),)
        ),
    )
    codes = {r.position_id: r.code for r in wrapped.coverage.rejections}
    assert codes["p-perp"] == "UNSUPPORTED_PRODUCT"
    horizon = horizon_portfolio(
        snapshot=book_snapshot(),
        instruments=BOOK,
        positions=POSITIONS,
        policy=NEVER,
        horizon_date=date(2026, 12, 15),
    )
    codes = {r.position_id: r.code for r in horizon.coverage.rejections}
    assert codes == {"p-perp": "UNSUPPORTED_PRODUCT"}
