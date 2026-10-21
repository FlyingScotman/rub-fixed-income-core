"""Desk-monitor additions of 0.33.0 (docs/reviews/CORE_REVIEW_FOR_DESK_MONITOR.md).

Per-instrument spread kinds, a G-spread to the KBD whatever the discount curve, the
floater discount margin, the known-coupons check, the open-window put horizon, spread
risk by bucket, spread-kind scenario shifts and floater accrued interest. Every addition
is off by default and leaves earlier results byte for byte (``test_hash_stability``).
"""

from __future__ import annotations

import math
from dataclasses import replace
from datetime import date, timedelta

import pytest
from test_portfolio import AS_OF, BUSINESS_DATE, PARAMS, bond, periods
from test_portfolio_floaters import CALENDAR as FRN_CALENDAR
from test_portfolio_floaters import KNOWN, OIS, floater, ruonia
from test_portfolio_floaters import snapshot as floater_snapshot

from rub_fixed_income import (
    RISK_BUCKETS,
    AccountingCategory,
    AccrualBasis,
    BondPriceObservation,
    BondScenario,
    BusinessCalendar,
    CalendarEntry,
    CurveEntry,
    DayCount,
    DiscountCurve,
    FixingHistory,
    FloatingCouponLeg,
    FloatingCouponPeriod,
    FloatingCouponRule,
    FloatingLeg,
    Instrument,
    InvalidTerms,
    MarketSnapshot,
    MissingMarketData,
    ObservationShift,
    Offer,
    OfferKind,
    PortfolioScenario,
    Position,
    Redemption,
    ValuationPolicy,
    bootstrap_discount_curve,
    canonical_sha256,
    explain_horizon_pnl,
    g_spread_bp,
    price_cashflows,
    value_portfolio,
)
from rub_fixed_income.serialization import from_dict, to_dict

CALENDAR = BusinessCalendar(
    holidays=frozenset({date(2026, 11, 4)}),
    version="synthetic-desk-monitor-2026",
    valid_from=date(2026, 1, 1),
    valid_to=date(2032, 12, 31),
)


def fixed_snapshot(*instrument_ids: str, **changes: object) -> MarketSnapshot:
    base = MarketSnapshot(
        "desk-monitor-2026-10-15",
        BUSINESS_DATE,
        AS_OF,
        curves=(
            CurveEntry("RUB_OFZ_KBD", "rub_ofz_kbd_v1", PARAMS),
            CurveEntry("RUB_OIS", "rub_ruonia_ois_legacy_v1", OIS),
        ),
        calendars=(CalendarEntry("MOEX_STOCK", CALENDAR),),
        quotes=tuple(BondPriceObservation(i, 97.5, "waprice") for i in instrument_ids),
    )
    return replace(base, **changes)  # type: ignore[arg-type]


def book() -> tuple[Instrument, ...]:
    return (
        bond("OFZ-A", periods(date(2026, 4, 15), 10, 35.4)),
        bond("CORP-B", periods(date(2026, 6, 1), 8, 70.0)),
    )


def positions(*ids: str) -> list[Position]:
    return [
        Position(f"p-{i}", i, 100.0, "RATES", AccountingCategory.TRADING, "RUB", 0.4) for i in ids
    ]


def run(instruments: tuple[Instrument, ...], policy: ValuationPolicy, **kw: object):  # type: ignore[no-untyped-def]
    ids = [i.instrument_id for i in instruments]
    args: dict[str, object] = {
        "run_id": "desk-monitor",
        "as_of": AS_OF,
        "snapshot": fixed_snapshot(*ids),
        "instruments": instruments,
        "positions": positions(*ids),
        "policy": policy,
    }
    args.update(kw)
    return value_portfolio(**args)  # type: ignore[arg-type]


# --- C8: spread kind per instrument --------------------------------------------------------


def test_spread_kinds_value_a_mixed_book_in_one_call_as_two_calls_did() -> None:
    mixed = ValuationPolicy(
        "RUB_OIS", spread_kind="credit", spread_kinds=(("OFZ-A", "ofz"),), include_second_order=False
    )
    one = run(book(), mixed)
    assert one.status == "complete"
    rows = {r.instrument_id: r for r in one.positions}
    assert rows["OFZ-A"].spread_kind == "ofz" and rows["CORP-B"].spread_kind == "credit"
    ofz = run(book()[:1], ValuationPolicy("RUB_OIS", spread_kind="ofz", include_second_order=False))
    credit = run(book()[1:], replace(mixed, spread_kinds=()))
    for single, row in ((ofz.positions[0], rows["OFZ-A"]), (credit.positions[0], rows["CORP-B"])):
        assert row.pv_rub == single.pv_rub and row.spread_bp == single.spread_bp
        assert row.factor_risk.first_order == single.factor_risk.first_order
    assert mixed.spread_kind_for("OFZ-A") == "ofz" and mixed.spread_kind_for("X") == "credit"


def test_spread_kinds_are_validated_and_left_out_at_their_default() -> None:
    with pytest.raises(InvalidTerms):
        ValuationPolicy("RUB_OIS", spread_kinds=(("A", "gov"),))  # type: ignore[arg-type]
    with pytest.raises(InvalidTerms):
        ValuationPolicy("RUB_OIS", spread_kinds=(("A", "ofz"), ("A", "credit")))
    plain = ValuationPolicy("RUB_OIS")
    assert "spread_kinds" not in to_dict(plain) and "g_spread_curve_id" not in to_dict(plain)
    full = ValuationPolicy(
        "RUB_OIS",
        spread_kinds=(("B", "ofz"), ("A", "credit")),
        g_spread_curve_id="RUB_OFZ_KBD",
        discount_margin=True,
        require_known_coupons=True,
        spread_buckets=True,
        horizon="open_put",
    )
    assert full.spread_kinds == (("A", "credit"), ("B", "ofz"))
    assert from_dict(ValuationPolicy, to_dict(full)) == full


# --- G-spread to the KBD on any discount curve -----------------------------------------------


def test_g_spread_curve_gives_a_g_spread_to_bonds_discounted_on_ruonia() -> None:
    base = ValuationPolicy("RUB_OIS", include_second_order=False)
    assert all(r.g_spread_bp is None for r in run(book(), base).positions)
    result = run(book(), replace(base, g_spread_curve_id="RUB_OFZ_KBD"))
    on_kbd = run(book(), replace(base, discount_curve_id="RUB_OFZ_KBD"))
    for row, kbd_row in zip(result.positions, on_kbd.positions, strict=True):
        assert row.curve_id == "RUB_OIS"
        expected = g_spread_bp(PARAMS, row.effective_yield, row.macaulay_duration_years)
        assert row.g_spread_bp == pytest.approx(expected, abs=1e-12)
        # The G-spread depends on the yield only, not on the discount curve.
        assert row.g_spread_bp == pytest.approx(kbd_row.g_spread_bp, abs=1e-9)
    assert result.job_id != run(book(), base).job_id


def test_g_spread_curve_must_be_a_kbd_given_by_its_parameters() -> None:
    policy = ValuationPolicy("RUB_OIS", include_second_order=False, g_spread_curve_id="RUB_OIS")
    codes = {r.code for r in run(book(), policy).coverage.rejections}
    assert codes == {"INVALID_TERMS"}
    missing = replace(policy, g_spread_curve_id="NO_SUCH_CURVE")
    assert {r.code for r in run(book(), missing).coverage.rejections} == {"MISSING_MARKET_DATA"}


# --- C7: coupons known to the horizon ----------------------------------------------------------


def test_require_known_coupons_rejects_gaps_and_unknown_amounts() -> None:
    legs = periods(date(2026, 4, 15), 10, 35.4)
    gap = bond("GAP", legs[:4] + legs[5:])
    zero = bond("ZERO", legs[:6] + (replace(legs[6], amount=0.0),) + legs[7:])
    short = bond("SHORT", legs[:6], redemptions=(Redemption(legs[-1].payment_date, 1000.0),))
    complete = bond("FULL", legs)
    instruments = (gap, zero, short, complete)
    plain = ValuationPolicy("RUB_OFZ_KBD", include_second_order=False)
    assert run(instruments, plain).status == "complete"  # valued without the coupons
    strict = run(instruments, replace(plain, require_known_coupons=True))
    codes = {r.instrument_id: r.code for r in strict.coverage.rejections}
    assert codes == {
        "GAP": "MISSING_MARKET_DATA",
        "ZERO": "MISSING_MARKET_DATA",
        "SHORT": "MISSING_MARKET_DATA",
    }
    messages = {r.instrument_id: r.message for r in strict.coverage.rejections}
    assert "no coupon for" in messages["GAP"] and "has no amount" in messages["ZERO"]


def test_known_coupons_check_stops_at_a_put_horizon() -> None:
    legs = periods(date(2026, 4, 15), 10, 35.4)
    put = Offer(legs[3].payment_date, OfferKind.PUT, confirmed=True)
    unknown_after_put = bond(
        "PUT", legs[:4] + tuple(replace(p, amount=0.0) for p in legs[4:]), offers=(put,)
    )
    policy = ValuationPolicy(
        "RUB_OFZ_KBD", include_second_order=False, horizon="confirmed_put", require_known_coupons=True
    )
    assert run((unknown_after_put,), policy).status == "complete"
    to_maturity = replace(policy, horizon="maturity")
    assert run((unknown_after_put,), to_maturity).coverage.rejections[0].code == (
        "MISSING_MARKET_DATA"
    )


# --- C1: puts with exercise windows -------------------------------------------------------------


def test_offer_windows_validate_and_stay_out_of_the_bytes_when_absent() -> None:
    plain = Offer(date(2027, 4, 15), OfferKind.PUT, True)
    assert "window_end" not in to_dict(plain)
    assert canonical_sha256(plain) == canonical_sha256(
        {"offer_date": "2027-04-15", "kind": "put", "confirmed": True, "price_pct": 100.0}
    )
    windowed = Offer(date(2027, 4, 15), OfferKind.PUT, False, window_start=date(2027, 4, 1),
                     window_end=date(2027, 4, 8))
    assert from_dict(Offer, to_dict(windowed)) == windowed
    assert windowed.is_open(date(2027, 4, 8)) and not windowed.is_open(date(2027, 4, 9))
    with pytest.raises(InvalidTerms):
        Offer(date(2027, 4, 15), OfferKind.PUT, True, window_end=date(2027, 4, 16))
    with pytest.raises(InvalidTerms):
        Offer(date(2027, 4, 15), OfferKind.PUT, True, window_start=date(2027, 4, 9),
              window_end=date(2027, 4, 8))


def test_open_put_skips_a_put_whose_window_has_closed() -> None:
    legs = periods(date(2026, 4, 15), 10, 35.4)
    closed = Offer(legs[1].payment_date, OfferKind.PUT, True, window_end=date(2026, 10, 14))
    unconfirmed = Offer(legs[3].payment_date, OfferKind.PUT, False, window_end=date(2027, 10, 1))
    instrument = bond("PUT2", legs, offers=(closed, unconfirmed))
    settlement = CALENDAR.add_business_days(BUSINESS_DATE, 1)
    # confirmed_put takes the first confirmed put, closed or not; open_put the first open one.
    assert instrument.horizon("confirmed_put", settlement) == (closed.offer_date, closed)
    assert instrument.horizon("open_put", settlement) == (unconfirmed.offer_date, unconfirmed)
    policy = ValuationPolicy("RUB_OFZ_KBD", include_second_order=False, horizon="open_put")
    row = run((instrument,), policy).positions[0]
    assert (row.horizon, row.horizon_date) == ("open_put", unconfirmed.offer_date)
    late = bond("PUT3", legs, offers=(closed,))
    assert run((late,), policy).positions[0].horizon == "maturity"
    assert late.horizon("open_put", settlement)[1] is None


# --- R18 / CS01 by bucket ------------------------------------------------------------------------


def test_spread_buckets_split_cs_by_the_discount_key_rate_shape() -> None:
    policy = ValuationPolicy(
        "RUB_OIS", spread_kinds=(("OFZ-A", "ofz"),), spread_buckets=True, include_second_order=False
    )
    result = run(book(), policy)
    for row in result.positions:
        first = row.factor_risk.first_order_by_factor()
        assert len(row.spread_buckets_rub) == len(RISK_BUCKETS)
        assert math.fsum(row.spread_buckets_rub) == pytest.approx(first["CS"], rel=1e-12)
        dv01 = [first[f"IR|RUB_OIS|{b}"] for b in RISK_BUCKETS]
        for share, rate in zip(row.spread_buckets_rub, dv01, strict=True):
            assert share == pytest.approx(first["CS"] * rate / math.fsum(dv01), rel=1e-9, abs=1e-9)
    plain = run(book(), replace(policy, spread_buckets=False))
    assert all(r.spread_buckets_rub == () for r in plain.positions)
    assert [r.pv_rub for r in plain.positions] == [r.pv_rub for r in result.positions]


def test_single_curve_floater_spread_buckets_hold_the_projection() -> None:
    """On one curve a floater's rate risk is small (coupons re-project); its spread or
    basis risk is not: the buckets hold the coupons and carry the whole CS."""
    policy = ValuationPolicy(
        "RUB_OIS", spread_buckets=True, include_second_order=False, discount_margin=True
    )
    result = value_portfolio(
        run_id="frn-buckets",
        as_of=AS_OF,
        snapshot=floater_snapshot(),
        instruments=(floater("FRN-1C", "RUB_OIS"),),
        positions=positions("FRN-1C"),
        policy=policy,
    )
    row = result.positions[0]
    first = row.factor_risk.first_order_by_factor()
    rates = math.fsum(first[f"IR|RUB_OIS|{b}"] for b in RISK_BUCKETS)
    spread = math.fsum(row.spread_buckets_rub)
    assert spread == pytest.approx(first["CS"], rel=1e-9)
    assert abs(rates) < 0.2 * abs(spread)  # a floater: little rate risk, full spread risk
    assert row.discount_margin_bp == row.spread_bp  # single curve: DM is the Z-spread


def test_discount_margin_of_a_key_rate_floater_is_the_spread_over_its_projection() -> None:
    policy = ValuationPolicy("RUB_OIS", discount_margin=True, include_second_order=False)
    result = value_portfolio(
        run_id="frn-dm",
        as_of=AS_OF,
        snapshot=floater_snapshot(),
        instruments=(floater("FRN-KEY", "KEY_PROJ", index="key_rate", fixing_id="KEY_RATE"),),
        positions=positions("FRN-KEY"),
        policy=policy,
    )
    row = result.positions[0]
    assert row.discount_margin_bp is not None and row.discount_margin_bp != row.spread_bp
    snapshot = floater_snapshot()
    instrument = floater("FRN-KEY", "KEY_PROJ", index="key_rate", fixing_id="KEY_RATE")
    projection = snapshot.projection_curve("KEY_PROJ", "key_rate")
    flows = instrument.cashflows(after=row.settlement_date, market=snapshot)
    price = price_cashflows(
        tuple((f.payment_date, f.amount) for f in flows),
        projection,  # type: ignore[arg-type]
        spread=row.discount_margin_bp / 10_000.0,
        settlement_date=row.settlement_date,
    )
    assert price == pytest.approx(row.dirty_price_rub, rel=1e-11)
    off = value_portfolio(
        run_id="frn-dm",
        as_of=AS_OF,
        snapshot=snapshot,
        instruments=(instrument,),
        positions=positions("FRN-KEY"),
        policy=replace(policy, discount_margin=False),
    )
    assert off.positions[0].discount_margin_bp is None


# --- spread-kind scenario shifts --------------------------------------------------------------


def test_spread_kind_shifts_move_only_the_named_kind() -> None:
    policy = ValuationPolicy("RUB_OIS", spread_kinds=(("OFZ-A", "ofz"),), include_second_order=False)
    basis = PortfolioScenario(
        "ofz basis +20", BondScenario.parallel(0.0), spread_kind_shifts_bp=(("ofz", 20.0),)
    )
    everything = PortfolioScenario("spread +20", BondScenario.parallel(0.0, spread_shift_bp=20.0))
    result = run(book(), policy, scenarios=(basis, everything))
    moved = {p.position_id: p.pnl_rub for p in result.scenarios[0].positions}
    both = {p.position_id: p.pnl_rub for p in result.scenarios[1].positions}
    assert moved["p-CORP-B"] == 0.0
    assert moved["p-OFZ-A"] == both["p-OFZ-A"] and moved["p-OFZ-A"] < 0.0
    with pytest.raises(InvalidTerms):
        PortfolioScenario("x", BondScenario.parallel(0.0), spread_kind_shifts_bp=(("gov", 1.0),))
    assert basis.bond_shock("credit") is basis.shock


# --- #81: floater accrued interest -------------------------------------------------------------


def _leg(rule: FloatingCouponRule, *periods_: FloatingCouponPeriod) -> FloatingLeg:
    return FloatingLeg(rule, periods_, ruonia())


RULE6 = FloatingCouponRule(
    "ruonia",
    "arithmetic",
    0.01,
    DayCount.ACT_365_FIXED,
    DayCount.ACT_365_FIXED,
    amount_rounding_decimals=2,
    observation_shift=ObservationShift(6, "calendar_days", "observation"),
)


def test_in_arrears_accrued_is_the_formula_over_the_elapsed_part() -> None:
    start, end = date(2026, 9, 15), date(2026, 12, 15)
    leg = _leg(RULE6, FloatingCouponPeriod(start, end, end, 1000.0))
    on = date(2026, 10, 15)
    part = FloatingLeg(RULE6, (FloatingCouponPeriod(start, on, on, 1000.0),), ruonia())
    expected = part.project(None, settlement_date=start)[0].amount_rub
    assert leg.accrued_interest(on) == expected
    # 16 % RUONIA + 1 % margin over the elapsed 30 days.
    assert expected == pytest.approx(1000.0 * 0.17 * 30 / 365, abs=0.006)
    assert leg.accrued_interest(start) == 0.0
    assert leg.accrued_interest(date(2026, 9, 1)) == 0.0
    assert leg.accrued_interest(end) == 0.0


def test_published_accrued_is_exact_pro_rata_or_on_the_published_rate() -> None:
    start, end = date(2026, 10, 7), date(2027, 4, 7)
    published = FloatingCouponPeriod(start, end, end, 1000.0, 77.49, 0.1554)
    leg = _leg(RULE6, published)
    on = date(2026, 10, 20)  # 13 of 182 days: 77.49 * 13 / 182 = 5.535 exactly
    assert 77.49 * 13 / 182 < 5.535  # binary floating point falls below the half kopeck
    assert leg.accrued_interest(on) == 5.54  # MOEX SU29008RMFS8 on 2026-10-20
    assert leg.accrued_interest(on, rate_basis=AccrualBasis.RATE_ACT_365) == 5.53
    no_rate = _leg(RULE6, replace(published, published_rate=None))
    assert no_rate.accrued_interest(on, rate_basis=AccrualBasis.RATE_ACT_365) == 5.54
    with pytest.raises(ValueError, match="published rate needs"):
        FloatingCouponPeriod(start, end, end, 1000.0, None, 0.15)
    with pytest.raises(ValueError, match="decimal"):
        FloatingCouponPeriod(start, end, end, 1000.0, 77.49, 15.54)


def test_accrued_that_needs_a_projection_asks_for_a_curve() -> None:
    start, end = date(2026, 10, 1), date(2027, 1, 1)
    no_shift = replace(RULE6, observation_shift=None)
    leg = _leg(no_shift, FloatingCouponPeriod(start, end, end, 1000.0))
    on = date(2026, 10, 20)  # observations from 15 October are not fixed yet
    with pytest.raises(MissingMarketData, match="projection curve"):
        leg.accrued_interest(on)
    curve = bootstrap_discount_curve(OIS)
    assert leg.accrued_interest(on, curve) > 0.0


def test_instrument_accrued_of_a_floater_reads_the_market() -> None:
    instrument = floater("FRN-1C", "RUB_OIS")
    snapshot = floater_snapshot()
    settlement = FRN_CALENDAR.add_business_days(BUSINESS_DATE, 1)
    with pytest.raises(InvalidTerms):
        instrument.accrued(settlement)
    leg = instrument.floating_leg(snapshot)
    # Settlement is after the fixings' date: the last day is projected on RUB_OIS.
    with pytest.raises(MissingMarketData):
        leg.accrued_interest(settlement)
    curve = bootstrap_discount_curve(OIS)
    assert instrument.accrued(settlement, snapshot) == leg.accrued_interest(settlement, curve)
    assert instrument.accrued(BUSINESS_DATE, snapshot) == leg.accrued_interest(BUSINESS_DATE)
    rate_leg = replace(
        instrument.coupon_leg,  # type: ignore[type-var]
        accrual_basis=AccrualBasis.RATE_ACT_365,
    )
    assert isinstance(rate_leg, FloatingCouponLeg)
    assert "accrual_basis" not in to_dict(instrument.coupon_leg)
    assert to_dict(rate_leg)["accrual_basis"] == "rate:ACT/365F"


# --- #78: floater P&L explain ---------------------------------------------------------------


def _realised(rate: float, horizon: date, bump: float = 0.0) -> FixingHistory:
    """The start fixings plus each business day to ``horizon`` at the flat curve's
    overnight forward (``bump`` added): the forwards carry assumed, realised."""
    start = ruonia()
    rows = list(start.fixings)
    day = BUSINESS_DATE
    while day < horizon:
        following = FRN_CALENDAR.add_business_days(day, 1)
        span = (following - day).days
        forward = (math.exp(rate * span / 365.0) - 1.0) * 365.0 / span
        rows.append((day, forward + bump))
        day = following
    return FixingHistory("ruonia", horizon, tuple(rows), FRN_CALENDAR, "cbr-ruonia",
                         known_at_utc=KNOWN)


def test_floater_explain_on_an_unchanged_market_is_all_carry() -> None:
    instrument = floater("FRN-2C", "RUB_OIS")
    horizon = BUSINESS_DATE + timedelta(days=30)
    for projection in (None, DiscountCurve.flat(BUSINESS_DATE, 0.14)):
        rate = 0.15 if projection is None else 0.14
        start = DiscountCurve.flat(BUSINESS_DATE, 0.15)
        end = DiscountCurve.flat(horizon, 0.15)
        explain = explain_horizon_pnl(
            instrument,
            start,
            end,
            FRN_CALENDAR,
            start_spread_bp=40.0,
            end_spread_bp=40.0,
            fixings=ruonia(),
            end_fixings=_realised(rate, horizon),
            projection_curve=projection,
            end_projection_curve=None if projection is None else DiscountCurve.flat(horizon, 0.14),
        )
        parts = (explain.carry_rub, explain.rolldown_rub, explain.rates_rub, explain.spread_rub,
                 explain.residual_rub)
        assert math.fsum(parts) == pytest.approx(explain.total_rub, abs=1e-9)
        assert explain.rolldown_rub == pytest.approx(0.0, abs=1e-9)
        assert explain.rates_rub == pytest.approx(0.0, abs=1e-6)
        assert explain.spread_rub == 0.0 and explain.residual_rub == pytest.approx(0.0, abs=1e-6)


def test_floater_explain_puts_realised_fixings_against_forwards_in_rates() -> None:
    instrument = floater("FRN-2C", "RUB_OIS")
    horizon = BUSINESS_DATE + timedelta(days=30)
    start = DiscountCurve.flat(BUSINESS_DATE, 0.15)
    end = DiscountCurve.flat(horizon, 0.15)
    common = {"start_spread_bp": 40.0, "end_spread_bp": 40.0, "fixings": ruonia()}
    base = explain_horizon_pnl(instrument, start, end, FRN_CALENDAR,
                               end_fixings=_realised(0.15, horizon), **common)  # type: ignore[arg-type]
    up = explain_horizon_pnl(instrument, start, end, FRN_CALENDAR,
                             end_fixings=_realised(0.15, horizon, 0.01), **common)  # type: ignore[arg-type]
    # +100 bp on the days the new fixings cover (the last one runs over a weekend) of the
    # running compounded coupon: about 1000 x 1 % x days / 365.
    last = max(d for d, _ in _realised(0.15, horizon).fixings)
    covered = (FRN_CALENDAR.add_business_days(last, 1) - BUSINESS_DATE).days
    assert up.rates_rub - base.rates_rub == pytest.approx(1000.0 * 0.01 * covered / 365, rel=0.03)
    assert up.carry_rub == base.carry_rub and up.rolldown_rub == base.rolldown_rub
    with pytest.raises(InvalidTerms, match="floaters only"):
        explain_horizon_pnl(bond("OFZ-A", periods(date(2026, 4, 15), 10, 35.4)), start, end,
                            FRN_CALENDAR, start_spread_bp=0.0, end_spread_bp=0.0,
                            fixings=ruonia())
    with pytest.raises(InvalidTerms, match="dated at the end"):
        explain_horizon_pnl(instrument, start, end, FRN_CALENDAR, end_fixings=ruonia(), **common)  # type: ignore[arg-type]
