"""Floaters in the portfolio path (package 1): projection off a snapshot curve with
point-in-time fixings, single- and two-curve desk factors, coded rejections, scenarios
and carry."""

from __future__ import annotations

import math
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest
from test_portfolio import AS_OF, BUSINESS_DATE, PARAMS

from rub_fixed_income import (
    RISK_BUCKETS,
    AccountingCategory,
    BondPriceObservation,
    BondScenario,
    BusinessCalendar,
    CalendarEntry,
    CouponRateBounds,
    CurveBootstrapRequest,
    CurveEntry,
    DayCount,
    FixingEntry,
    FixingHistory,
    FloatingCouponLeg,
    FloatingCouponPeriod,
    FloatingCouponRule,
    Instrument,
    KeyRateForwardCurve,
    KeyRateForwardSegment,
    MarketSnapshot,
    OisQuote,
    PortfolioScenario,
    Position,
    PrincipalLeg,
    ProductType,
    Redemption,
    UnsupportedProduct,
    ValuationPolicy,
    explain_horizon_pnl,
    horizon_portfolio,
    key_rate_projection_curve,
    portfolio_result_from_json,
    portfolio_result_to_json,
    value_portfolio,
    verify_portfolio_result,
)
from rub_fixed_income.dates import add_months

ACT365 = DayCount.ACT_365_FIXED
# Unbounded: projections and the key-rate curve run to 2031.
CALENDAR = BusinessCalendar(holidays=frozenset({date(2026, 11, 4)}), version="synthetic-floaters")
KNOWN = "2026-10-15T12:00:00Z"
OIS = CurveBootstrapRequest(
    BUSINESS_DATE,
    tuple(
        OisQuote(f"{y}Y", BUSINESS_DATE + timedelta(days=365 * y), 0.14 - 0.002 * y)
        for y in (1, 2, 3, 5, 7, 10)
    ),
)
KEY_FORWARD = KeyRateForwardCurve(
    BUSINESS_DATE,
    0.165,
    date(2026, 10, 26),
    (
        KeyRateForwardSegment(date(2026, 10, 26), date(2027, 6, 1), 0.15, "1Y"),
        KeyRateForwardSegment(date(2027, 6, 1), date(2031, 1, 1), 0.13, "5Y"),
    ),
)
KEY_PROJECTION = key_rate_projection_curve(
    KEY_FORWARD, CALENDAR, rate_day_count=ACT365, observation_days="business"
)


def _business_days(start: date, end: date) -> list[date]:
    out, day = [], start
    while day < end:
        if CALENDAR.is_business_day(day):
            out.append(day)
        day += timedelta(days=1)
    return out


def ruonia(**kw: object) -> FixingHistory:
    rows = tuple((d, 0.16) for d in _business_days(date(2026, 6, 1), BUSINESS_DATE))
    args: dict[str, object] = {"known_at_utc": KNOWN}
    args.update(kw)
    return FixingHistory("ruonia", BUSINESS_DATE, rows, CALENDAR, "cbr-ruonia", **args)  # type: ignore[arg-type]


def key_rate() -> FixingHistory:
    rows = ((date(2026, 6, 1), 0.18), (date(2026, 9, 15), 0.165))
    return FixingHistory("key_rate", BUSINESS_DATE, rows, CALENDAR, "cbr-key", known_at_utc=KNOWN)


def floater(
    instrument_id: str,
    projection: str | None,
    *,
    index: str = "ruonia",
    first: date = date(2026, 9, 15),
    bounds: CouponRateBounds | None = None,
    fixing_id: str = "RUONIA",
) -> Instrument:
    rule = FloatingCouponRule(
        index,  # type: ignore[arg-type]
        "compounded" if index == "ruonia" else "arithmetic",
        0.012,
        ACT365,
        ACT365,
        amount_rounding_decimals=2,
        bounds=bounds,
    )
    periods = tuple(
        FloatingCouponPeriod(
            add_months(first, 3 * k),
            add_months(first, 3 * (k + 1)),
            add_months(first, 3 * (k + 1)),
            1000.0,
        )
        for k in range(12)
    )
    return Instrument(
        instrument_id,
        ProductType.FLOATING_BOND,
        "RUB",
        1000.0,
        FloatingCouponLeg(rule, periods, fixing_id, projection),
        PrincipalLeg((Redemption(periods[-1].payment_date, 1000.0),)),
        "MOEX_STOCK",
    )


def instruments() -> tuple[Instrument, ...]:
    return (
        floater("FRN-1C", "RUB_OIS"),
        floater("FRN-2C", "RUB_OIS"),
        floater("FRN-KEY", "KEY_PROJ", index="key_rate", fixing_id="KEY_RATE"),
        floater("FRN-FLOOR", "RUB_OIS", bounds=CouponRateBounds("coupon_rate", floor=0.05)),
    )


def snapshot(**changes: object) -> MarketSnapshot:
    base = MarketSnapshot(
        "floaters-2026-10-15",
        BUSINESS_DATE,
        AS_OF,
        curves=(
            CurveEntry("RUB_OFZ_KBD", "rub_ofz_kbd_v1", PARAMS),
            CurveEntry("RUB_OIS", "rub_ruonia_ois_legacy_v1", OIS),
            CurveEntry("KEY_PROJ", "rub_key_rate_projection_v1", KEY_PROJECTION),
        ),
        calendars=(CalendarEntry("MOEX_STOCK", CALENDAR),),
        fixings=(FixingEntry("RUONIA", ruonia()), FixingEntry("KEY_RATE", key_rate())),
        quotes=tuple(
            BondPriceObservation(i, 99.6, "waprice", accrued_per_bond=5.3)
            for i in ("FRN-1C", "FRN-2C", "FRN-KEY", "FRN-FLOOR")
        ),
    )
    return replace(base, **changes)  # type: ignore[arg-type]


def positions() -> tuple[Position, ...]:
    return tuple(
        Position(f"p-{i}", i, q, "RATES", AccountingCategory.TRADING, "RUB", 0.4)
        for i, q in (("FRN-1C", 100.0), ("FRN-2C", 200.0), ("FRN-KEY", -50.0), ("FRN-FLOOR", 10.0))
    )


POLICY = ValuationPolicy(
    "RUB_OFZ_KBD", curve_overrides=(("FRN-1C", "RUB_OIS"),), cap_floor_guard_bp=25.0
)


def run(**kw: object):  # type: ignore[no-untyped-def]
    args: dict[str, object] = {
        "run_id": "floaters",
        "as_of": AS_OF,
        "snapshot": snapshot(),
        "instruments": instruments(),
        "positions": positions(),
        "policy": POLICY,
        "scenarios": (PortfolioScenario("up10", BondScenario.parallel(10.0)),),
    }
    args.update(kw)
    return value_portfolio(**args)  # type: ignore[arg-type]


def test_floaters_are_valued_with_single_and_two_curve_factors() -> None:
    result = run()
    assert result.status == "complete", result.coverage.rejections
    rows = {p.position_id: p for p in result.positions}
    single = rows["p-FRN-1C"].factor_risk
    assert len(single.factors) == 12 and single.factors[0] == "IR|RUB_OIS|1M"
    two = rows["p-FRN-2C"].factor_risk
    assert two.factors[:11] == tuple(f"IR|RUB_OFZ_KBD|{b}" for b in RISK_BUCKETS)
    assert two.factors[11:22] == tuple(f"IR|RUB_OIS|{b}" for b in RISK_BUCKETS)
    assert two.factors[22] == "CS" and two.has_second_order
    key = rows["p-FRN-KEY"].factor_risk
    assert key.factors[11] == "IR|KEY_PROJ|1M"
    # A long two-curve floater gains when projected rates rise and loses on discounting.
    first = two.first_order_by_factor()
    assert sum(first[f"IR|RUB_OIS|{b}"] for b in RISK_BUCKETS) > 0.0
    assert sum(first[f"IR|RUB_OFZ_KBD|{b}"] for b in RISK_BUCKETS) < 0.0
    assert {c.curve_id for c in result.curves} == {"RUB_OFZ_KBD", "RUB_OIS", "KEY_PROJ"}
    for row in result.positions:
        assert row.g_spread_bp is None and row.accrued_interest_rub == 5.3
        assert row.pv_rub == pytest.approx(row.quantity * row.dirty_price_rub, rel=1e-10)
    totals = {t.factor: t.first_order_rub for t in result.factor_totals}
    ois_total = math.fsum(
        p.factor_risk.first_order_by_factor().get("IR|RUB_OIS|2Y", 0.0) for p in result.positions
    )
    assert totals["IR|RUB_OIS|2Y"] == pytest.approx(ois_total, rel=1e-12)
    assert verify_portfolio_result(result)
    assert portfolio_result_from_json(portfolio_result_to_json(result)) == result
    assert run() == result


def test_taylor_from_both_curves_matches_the_exact_scenario() -> None:
    result = run(scenarios=(PortfolioScenario("up10", BondScenario.parallel(10.0)),))
    taylor = 0.0
    for row in result.positions:
        risk = row.factor_risk
        ir = [i for i, f in enumerate(risk.factors) if f.startswith("IR|")]
        taylor += sum(risk.first_order[i] for i in ir) * 10.0
        for e in risk.hessian:
            if e.i in ir and e.j in ir:
                taylor += (0.5 if e.i == e.j else 1.0) * e.value * 100.0
    exact = result.scenarios[0].total_pnl_rub
    assert exact is not None
    assert taylor == pytest.approx(exact, rel=2e-3, abs=0.05)
    only = run(
        scenarios=(
            PortfolioScenario(
                "projection only", BondScenario(), projection_curve_shifts_bp=(10.0,) * 11
            ),
        )
    )
    pnl = {r.position_id: r.pnl_rub for r in only.scenarios[0].positions}
    assert pnl["p-FRN-2C"] is not None and pnl["p-FRN-2C"] > 0.0
    assert pnl["p-FRN-1C"] == pytest.approx(0.0, abs=1e-9)  # single-curve: shock is zero


def test_instrument_cashflows_project_the_coupons_from_the_snapshot() -> None:
    bond = floater("FRN-2C", "RUB_OIS")
    settlement = CALENDAR.add_business_days(BUSINESS_DATE, 1)
    flows = bond.cashflows(after=settlement, market=snapshot())
    assert len(flows) == 12 and flows[-1].principal == 1000.0
    assert all(f.coupon > 0.0 for f in flows)
    first = flows[0]  # 15 Sep - 15 Dec: 30 realized days at 16 %, the rest projected
    assert first.accrual_start == date(2026, 9, 15)
    assert 1000.0 * (0.16 + 0.012) * 0.2 < first.coupon < 1000.0 * (0.16 + 0.012) * 0.3
    assert bond.cashflows(after=settlement)[0].coupon == 0.0  # no market: principal only
    with pytest.raises(UnsupportedProduct, match="names no projection curve"):
        floater("X", None).cashflows(after=settlement, market=snapshot())


FIRST_ORDER = replace(POLICY, include_second_order=False)


def _codes(**kw: object) -> dict[str, str]:
    kw.setdefault("policy", FIRST_ORDER)
    result = run(scenarios=(), **kw)
    return {r.position_id: r.code for r in result.coverage.rejections}


def test_point_in_time_fixings_and_coded_rejections() -> None:
    late = ruonia(known_at_utc="2026-10-15T17:00:00Z")
    assert (
        _codes(
            snapshot=snapshot(
                fixings=(FixingEntry("RUONIA", late), FixingEntry("KEY_RATE", key_rate()))
            )
        )["p-FRN-2C"]
        == "INVALID_TERMS"
    )
    undated = ruonia(known_at_utc=None)
    assert (
        _codes(
            snapshot=snapshot(
                fixings=(FixingEntry("RUONIA", undated), FixingEntry("KEY_RATE", key_rate()))
            )
        )["p-FRN-1C"]
        == "INVALID_TERMS"
    )
    gap = replace(ruonia(), fixings=tuple(r for r in ruonia().fixings if r[0] != date(2026, 10, 1)))
    codes = _codes(
        snapshot=snapshot(fixings=(FixingEntry("RUONIA", gap), FixingEntry("KEY_RATE", key_rate())))
    )
    assert codes["p-FRN-2C"] == "MISSING_MARKET_DATA" and "p-FRN-KEY" not in codes
    assert (
        _codes(snapshot=snapshot(fixings=(FixingEntry("KEY_RATE", key_rate()),)))["p-FRN-1C"]
        == "MISSING_MARKET_DATA"
    )
    no_accrued = snapshot(quotes=(BondPriceObservation("FRN-1C", 99.6, "waprice"),))
    assert _codes(snapshot=no_accrued, positions=positions()[:1]) == {
        "p-FRN-1C": "MISSING_MARKET_DATA"
    }
    wrong_spec = (floater("FRN-KEY", "RUB_OIS", index="key_rate", fixing_id="KEY_RATE"),)
    assert _codes(instruments=wrong_spec, positions=positions()[2:3]) == {
        "p-FRN-KEY": "INVALID_TERMS"
    }


def test_publication_times_after_the_cut_drop_fixings_without_filling() -> None:
    history = ruonia(known_at_utc=None)
    stamps = tuple(
        datetime(d.year, d.month, d.day, 10, tzinfo=UTC) + timedelta(days=1)
        for d, _ in history.fixings
    )
    published = replace(history, published_at=stamps)
    # 14 October is published at 10:00 UTC on 15 October, before the 16:30 cut.
    ok = snapshot(fixings=(FixingEntry("RUONIA", published), FixingEntry("KEY_RATE", key_rate())))
    assert _codes(snapshot=ok, positions=positions()[:2]) == {}
    early = replace(ok, known_at=datetime(2026, 10, 15, 9, tzinfo=UTC))
    run_early = run(
        snapshot=early,
        positions=positions()[:2],
        scenarios=(),
        as_of=early.known_at,
        policy=FIRST_ORDER,
    )
    assert {r.code for r in run_early.coverage.rejections} == {"MISSING_MARKET_DATA"}
    carry = replace(published, max_unpublished_business_days=1)
    allowed = replace(
        early, fixings=(FixingEntry("RUONIA", carry), FixingEntry("KEY_RATE", key_rate()))
    )
    allowed_run = run(
        snapshot=allowed,
        positions=positions()[:2],
        scenarios=(),
        as_of=early.known_at,
        policy=FIRST_ORDER,
    )
    assert allowed_run.status == "complete"


def test_caps_and_floors_follow_the_guard_policy() -> None:
    near = (floater("FRN-FLOOR", "RUB_OIS", bounds=CouponRateBounds("coupon_rate", floor=0.135)),)
    assert _codes(instruments=near, positions=positions()[3:]) == {
        "p-FRN-FLOOR": "UNSUPPORTED_PRODUCT"
    }
    no_guard = replace(FIRST_ORDER, cap_floor_guard_bp=None)
    assert _codes(policy=no_guard, positions=positions()[3:]) == {
        "p-FRN-FLOOR": "UNSUPPORTED_PRODUCT"
    }
    assert _codes(positions=positions()[3:]) == {}
    with pytest.raises(ValueError, match="cannot be negative"):
        replace(POLICY, cap_floor_guard_bp=-1.0)


def test_floaters_carry_to_the_horizon_and_explain_is_rejected() -> None:
    horizon = BUSINESS_DATE + timedelta(days=30)
    carry = horizon_portfolio(
        snapshot=snapshot(),
        instruments=instruments(),
        positions=positions(),
        policy=POLICY,
        horizon_date=horizon,
    )
    assert carry.coverage.status == "complete", carry.coverage.rejections
    risk = run(scenarios=())
    for row, valued in zip(carry.positions, risk.positions, strict=True):
        assert row.per_bond.pv_start_rub == pytest.approx(valued.dirty_price_rub, rel=1e-10)
        assert row.per_bond.accrued_start_rub is None and row.per_bond.accrued_end_rub is None
        assert row.horizon_pnl_rub == pytest.approx(row.per_bond.horizon_pnl_rub * row.quantity)
    with pytest.raises(UnsupportedProduct, match="P&L explain of a floater"):
        explain_horizon_pnl(
            instruments()[0], OIS, OIS, CALENDAR, start_spread_bp=0.0, end_spread_bp=0.0
        )


def test_floater_rolldown_is_zero_on_flat_curves_and_carry_reconciles() -> None:
    from rub_fixed_income import DiscountCurve, horizon_valuation

    flat = DiscountCurve.flat(BUSINESS_DATE, 0.15)
    projection = DiscountCurve.flat(BUSINESS_DATE, 0.14)
    bond = floater("FRN-2C", "RUB_OIS")
    horizon = BUSINESS_DATE + timedelta(days=70)  # one coupon (15 Dec) is received
    for proj in (None, projection):
        result = horizon_valuation(
            bond,
            flat,
            CALENDAR,
            horizon_date=horizon,
            spread_bp=40.0,
            fixings=ruonia(),
            projection_curve=proj,
        )
        # A flat curve rolls onto itself and the bridge realises its own forwards.
        assert result.rolldown_rub == pytest.approx(0.0, abs=1e-9)
        assert result.coupons_received_rub > 0.0 and result.principal_received_rub == 0.0
        assert result.carry_rub == pytest.approx(
            result.pv_forward_rub + result.received_rub - result.pv_start_rub, abs=1e-12
        )
        assert result.horizon_pnl_rub == pytest.approx(result.carry_rub, abs=1e-9)
    with pytest.raises(UnsupportedProduct, match="fixing history"):
        horizon_valuation(bond, flat, CALENDAR, horizon_date=horizon, spread_bp=40.0)
