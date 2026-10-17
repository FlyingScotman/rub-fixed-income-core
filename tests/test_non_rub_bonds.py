"""Non-RUB bonds in the portfolio path and the FX delta (roadmap package 3)."""

from __future__ import annotations

import json
import math
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest
from test_kbd import load

from rub_fixed_income import (
    RISK_BUCKETS,
    AccountingCategory,
    BondCashflow,
    BondPriceObservation,
    BondRiskEngine,
    BondRiskRequest,
    BondScenario,
    BusinessCalendar,
    CalendarEntry,
    CouponPeriod,
    CurveEntry,
    DayCount,
    DiscountCurve,
    FactorRisk,
    FixedCouponLeg,
    FxRate,
    HessianEntry,
    Instrument,
    MarketSnapshot,
    PortfolioRiskResult,
    PortfolioScenario,
    Position,
    PositionValuation,
    PrincipalLeg,
    ProductType,
    Redemption,
    ValuationPolicy,
    YieldConvention,
    ZSpreadConvention,
    clean_price_pct_to_dirty_rub,
    from_dict,
    fx_translated_factor_risk,
    portfolio_result_from_json,
    portfolio_result_to_json,
    price_cashflows,
    to_dict,
    value_portfolio,
    verify_portfolio_result,
)
from rub_fixed_income.dates import add_months

BUSINESS_DATE = date(2026, 10, 15)  # Thursday
SETTLE = date(2026, 10, 16)
AS_OF = datetime(2026, 10, 15, 16, 30, tzinfo=UTC)
CALENDAR = BusinessCalendar(
    version="synthetic-weekends-2026-2045",
    valid_from=date(2026, 1, 1),
    valid_to=date(2045, 12, 31),
)
FX = {"USD": 81.5, "CNY": 11.4, "EUR": 94.2}
CURVE_IDS = {"USD": "USD_SOFR", "CNY": "CNY_QUOTES", "EUR": "EUR_ESTR", "RUB": "RUB_OIS"}
SPECS = {
    "USD": "usd_discount_factors_v1",
    "CNY": "cny_discount_factors_v1",
    "EUR": "eur_discount_factors_v1",
    "RUB": "rub_ruonia_ois_legacy_v1",
}
LEVELS = {
    "USD": (0.042, 0.0008),
    "CNY": (0.016, 0.0006),
    "EUR": (0.022, 0.0005),
    "RUB": (0.15, -0.004),
}


def node_curve(currency: str, curve_date: date = BUSINESS_DATE) -> DiscountCurve:
    """A sloped continuous zero curve on caller nodes (as the backend would build it)."""
    level, slope = LEVELS[currency]
    months = (1, 3, 6, 12, 24, 36, 60, 84, 120, 180, 240, 360)
    nodes = []
    for m in months:
        node = add_months(curve_date, m)
        t = (node - curve_date).days / 365.0
        nodes.append((node, math.exp(-(level + slope * min(t, 10.0)) * t)))
    return DiscountCurve(curve_date, tuple(nodes))


def bond(
    instrument_id: str,
    currency: str,
    *,
    coupon: float = 25.0,
    months: int = 6,
    count: int = 8,
    yield_basis: DayCount = DayCount.ACT_365_FIXED,
) -> Instrument:
    start = date(2026, 7, 1)
    periods = tuple(
        CouponPeriod(
            add_months(start, months * i),
            add_months(start, months * (i + 1)),
            add_months(start, months * (i + 1)),
            coupon,
        )
        for i in range(count)
    )
    return Instrument(
        instrument_id,
        ProductType.FIXED_BOND,
        currency,
        1000.0,
        FixedCouponLeg(periods),
        PrincipalLeg((Redemption(periods[-1].payment_date, 1000.0),)),
        "MOEX_STOCK",
        yield_convention=YieldConvention(day_count=yield_basis),
    )


INSTRUMENTS = (
    bond("USD-BOND", "USD"),
    bond("CNY-BOND", "CNY", coupon=32.0, months=12, count=4),
    bond("EUR-BOND", "EUR", coupon=15.0, count=10),
    bond("RUB-BOND", "RUB", coupon=60.0),
    bond("GBP-BOND", "GBP"),
)
PRICES = {"USD-BOND": 97.8, "CNY-BOND": 101.4, "EUR-BOND": 99.2, "RUB-BOND": 93.1, "GBP-BOND": 98.0}


def snapshot(**changes: object) -> MarketSnapshot:
    base = MarketSnapshot(
        "hub-eod-2026-10-15",
        BUSINESS_DATE,
        AS_OF,
        curves=tuple(CurveEntry(CURVE_IDS[c], SPECS[c], node_curve(c)) for c in CURVE_IDS),
        calendars=(CalendarEntry("MOEX_STOCK", CALENDAR),),
        fx=tuple(FxRate(c, r) for c, r in FX.items()),
        quotes=tuple(BondPriceObservation(i, p, "waprice") for i, p in PRICES.items()),
    )
    return replace(base, **changes)  # type: ignore[arg-type]


POLICY = ValuationPolicy(
    "RUB_OIS",
    foreign_curves=(("USD", "USD_SOFR"), ("CNY", "CNY_QUOTES"), ("EUR", "EUR_ESTR")),
)


def position(pid: str, iid: str, quantity: float, currency: str) -> Position:
    return Position(pid, iid, quantity, "FX-BONDS", AccountingCategory.TRADING, currency, 0.35)


POSITIONS = (
    position("p-usd", "USD-BOND", 300, "USD"),
    position("p-usd-short", "USD-BOND", -120, "USD"),
    position("p-cny", "CNY-BOND", 2000, "CNY"),
    position("p-eur", "EUR-BOND", 150, "EUR"),
    position("p-rub", "RUB-BOND", 500, "RUB"),
)


def run(**kw: object) -> PortfolioRiskResult:
    args: dict[str, object] = {
        "run_id": "fx-bonds",
        "as_of": AS_OF,
        "snapshot": snapshot(),
        "instruments": INSTRUMENTS,
        "positions": POSITIONS,
        "policy": POLICY,
    }
    args.update(kw)
    return value_portfolio(**args)  # type: ignore[arg-type]


def row(result: PortfolioRiskResult, pid: str) -> PositionValuation:
    return next(p for p in result.positions if p.position_id == pid)


def test_fx_delta_is_pv_in_currency_times_fx_over_100() -> None:
    result = run()
    assert result.status == "complete"
    for pid, currency in (
        ("p-usd", "USD"),
        ("p-usd-short", "USD"),
        ("p-cny", "CNY"),
        ("p-eur", "EUR"),
    ):
        valued = row(result, pid)
        amounts = valued.currency_amounts
        assert amounts is not None and amounts.currency == currency
        assert amounts.rub_per_unit == FX[currency]
        delta = valued.factor_risk.first_order_by_factor()[f"FX|{currency}"]
        assert delta == pytest.approx(amounts.pv * FX[currency] / 100.0, rel=1e-15)
        assert delta == pytest.approx(valued.pv_rub / 100.0, rel=1e-12)
        assert valued.pv_rub == pytest.approx(amounts.pv * FX[currency], rel=1e-12)
        assert (delta > 0) == (valued.quantity > 0)
    assert "FX|RUB" not in row(result, "p-rub").factor_risk.factors
    assert row(result, "p-rub").currency_amounts is None


def test_currency_amounts_and_their_rub_conversion() -> None:
    valued = row(run(), "p-usd")
    amounts = valued.currency_amounts
    assert amounts is not None
    usd = INSTRUMENTS[0]
    assert amounts.current_nominal == 1000.0 and amounts.accrued_interest == usd.accrued(SETTLE)
    assert amounts.dirty_price == pytest.approx(978.0 + usd.accrued(SETTLE))
    assert amounts.pv == pytest.approx(300 * amounts.dirty_price)
    assert valued.current_nominal_rub == 1000.0 * FX["USD"]
    assert valued.accrued_interest_rub == pytest.approx(amounts.accrued_interest * FX["USD"])
    assert valued.dirty_price_rub == pytest.approx(amounts.dirty_price * FX["USD"], rel=1e-14)
    assert valued.curve_id == "USD_SOFR" and valued.spec_id == "usd_discount_factors_v1"
    assert valued.factor_risk.factors[0] == "IR|USD_SOFR|1M"
    assert valued.jtd_pnl_rub == pytest.approx(300 * (0.35 * 1000.0 - amounts.dirty_price) * 81.5)


def test_rub_risk_equals_independent_currency_risk_times_fx() -> None:
    """Price in USD on the USD curve with no FX at all, then convert: same numbers."""
    valued = row(run(), "p-usd")
    usd = INSTRUMENTS[0]
    flows = usd.cashflows(after=SETTLE)
    dirty_usd = clean_price_pct_to_dirty_rub(97.8, 1000.0, usd.accrued(SETTLE))
    engine = BondRiskEngine(
        BondRiskRequest(
            cashflows=tuple(BondCashflow(f.payment_date, f.amount) for f in flows),
            settlement_date=SETTLE,
            curve=node_curve("USD"),
            spread_convention=ZSpreadConvention(),
            spread_kind="credit",
            dirty_price_rub=dirty_usd,  # the engine is unit-free; amounts here are USD
            quantity=300,
        )
    )
    in_usd = engine.second_order()
    first = valued.factor_risk.first_order_by_factor()
    for i, bucket in enumerate(RISK_BUCKETS):
        expected = in_usd.gradient_rub_per_bp[i] * FX["USD"]
        assert first[f"IR|USD_SOFR|{bucket}"] == pytest.approx(expected, rel=1e-9, abs=1e-9)
    assert first["CS"] == pytest.approx(in_usd.gradient_rub_per_bp[-1] * FX["USD"], rel=1e-9)
    assert valued.spread_bp == pytest.approx(engine.spread_bp, abs=1e-9)
    hessian = valued.factor_risk.hessian_by_factor()
    assert hessian[("CS", "CS")] == pytest.approx(
        in_usd.hessian_rub_per_bp2[-1][-1] * 81.5, rel=1e-6
    )


def test_sum_of_buckets_and_portfolio_factor_totals() -> None:
    result = run(scenarios=(PortfolioScenario("par+1", BondScenario.parallel(1.0)),))
    valued = row(result, "p-usd")
    first = valued.factor_risk.first_order_by_factor()
    bucket_sum = math.fsum(first[f"IR|USD_SOFR|{b}"] for b in RISK_BUCKETS)
    # The exact +1 bp parallel P&L (rates on every position's own curve) is the bucket
    # sum up to convexity.
    exact = next(r.pnl_rub for r in result.scenarios[0].positions if r.position_id == "p-usd")
    assert exact is not None and bucket_sum < 0
    assert bucket_sum == pytest.approx(exact, rel=2e-4)
    totals = {t.factor: t.first_order_rub for t in result.factor_totals}
    usd_rows = [row(result, "p-usd"), row(result, "p-usd-short")]
    assert totals["FX|USD"] == pytest.approx(
        math.fsum(r.factor_risk.first_order_by_factor()["FX|USD"] for r in usd_rows), rel=1e-15
    )
    assert {"FX|USD", "FX|CNY", "FX|EUR", "IR|RUB_OIS|5Y", "IR|EUR_ESTR|10Y", "CS"} <= set(totals)
    assert result.total_pv_rub == pytest.approx(math.fsum(p.pv_rub for p in result.positions))
    assert [c.curve_id for c in result.curves] == ["CNY_QUOTES", "EUR_ESTR", "RUB_OIS", "USD_SOFR"]
    assert result.fx_rates == tuple(FxRate(c, FX[c]) for c in ("CNY", "EUR", "USD"))


def test_fx_cross_gamma_signs_and_values() -> None:
    result = run()
    for pid, sign in (("p-usd", -1.0), ("p-usd-short", 1.0)):
        risk = row(result, pid).factor_risk
        first = risk.first_order_by_factor()
        hessian = risk.hessian_by_factor()
        assert ("FX|USD", "FX|USD") not in hessian  # RUB value is linear in the rate
        assert math.copysign(1.0, hessian[("CS", "FX|USD")]) == sign
        assert hessian[("CS", "FX|USD")] == first["CS"] / 100.0
        exposed = [b for b in RISK_BUCKETS if first[f"IR|USD_SOFR|{b}"] != 0.0]
        assert exposed
        for bucket in exposed:
            factor = f"IR|USD_SOFR|{bucket}"
            assert hessian[(factor, "FX|USD")] == first[factor] / 100.0
        rates_cross = math.fsum(hessian[(f"IR|USD_SOFR|{b}", "FX|USD")] for b in exposed)
        assert math.copysign(1.0, rates_cross) == sign
        for entry in risk.hessian:
            assert entry.i <= entry.j


def test_fx_cross_term_matches_central_differences_of_exact_repricing() -> None:
    """Mixed difference of exact scenario P&L in rates (1 bp) and FX (1 %)."""

    def pnl(rates: float, fx: float) -> float:
        scenario = PortfolioScenario("x", BondScenario.parallel(rates), (("USD", fx),))
        result = run(positions=POSITIONS[:1], scenarios=(scenario,))
        value = result.scenarios[0].total_pnl_rub
        assert value is not None
        return value

    mixed = (pnl(1, 1) - pnl(1, -1) - pnl(-1, 1) + pnl(-1, -1)) / 4.0
    risk = run(positions=POSITIONS[:1]).positions[0].factor_risk
    hessian = risk.hessian_by_factor()
    cross = math.fsum(hessian.get((f"IR|USD_SOFR|{b}", "FX|USD"), 0.0) for b in RISK_BUCKETS)
    assert mixed == pytest.approx(cross, rel=1e-6)
    fx_only = (pnl(0, 1) - pnl(0, -1)) / 2.0
    assert fx_only == pytest.approx(risk.first_order_by_factor()["FX|USD"], rel=1e-12)
    assert pnl(0, 1) + pnl(0, -1) == pytest.approx(0.0, abs=1e-6)  # no FX gamma


def test_taylor_with_fx_cross_terms_matches_exact_rates_and_fx_scenario() -> None:
    shock, move = 25.0, 8.0
    scenario = PortfolioScenario("rates+fx", BondScenario.parallel(shock), (("USD", move),))
    result = run(positions=POSITIONS[:1], scenarios=(scenario,))
    exact = result.scenarios[0].total_pnl_rub
    assert exact is not None
    risk = result.positions[0].factor_risk
    moves = [shock if f.startswith("IR|") else move if f == "FX|USD" else 0.0 for f in risk.factors]
    linear = math.fsum(d * x for d, x in zip(risk.first_order, moves))
    second = math.fsum(
        (0.5 if e.i == e.j else 1.0) * e.value * moves[e.i] * moves[e.j] for e in risk.hessian
    )
    fx = risk.factors.index("FX|USD")
    cross = math.fsum(e.value * moves[e.i] * moves[e.j] for e in risk.hessian if e.j == fx)
    assert linear + second == pytest.approx(exact, rel=5e-5)
    assert abs(linear + second - exact) < abs(linear + second - cross - exact) / 10


def test_rub_value_scales_with_the_fx_rate() -> None:
    doubled = snapshot(fx=tuple(FxRate(c, 2 * r) for c, r in FX.items()))
    base, high = row(run(), "p-cny"), row(run(snapshot=doubled), "p-cny")
    assert high.pv_rub == pytest.approx(2 * base.pv_rub, rel=1e-12)
    assert high.spread_bp == pytest.approx(base.spread_bp, abs=1e-8)
    for f, value in base.factor_risk.first_order_by_factor().items():
        assert high.factor_risk.first_order_by_factor()[f] == pytest.approx(
            2 * value, rel=1e-8, abs=1e-9
        )
    assert base.currency_amounts is not None
    assert high.currency_amounts == replace(base.currency_amounts, rub_per_unit=2 * FX["CNY"])


def test_price_spread_price_round_trip_on_the_foreign_curve() -> None:
    valued = row(run(), "p-eur")
    eur = INSTRUMENTS[2]
    amounts = valued.currency_amounts
    assert amounts is not None
    flows = tuple((f.payment_date, f.amount) for f in eur.cashflows(after=SETTLE))
    price = price_cashflows(
        flows,
        node_curve("EUR"),
        spread=valued.spread_bp * 1e-4,
        settlement_date=SETTLE,
        convention=ZSpreadConvention(),
    )
    assert price == pytest.approx(amounts.dirty_price, rel=1e-10)
    # The yield reprices the currency price too.
    years = [((d - SETTLE).days / 365.0, a) for d, a in flows]
    at_yield = math.fsum(a / (1 + valued.effective_yield) ** t for t, a in years)
    assert at_yield == pytest.approx(amounts.dirty_price, rel=1e-10)


def test_act_360_yield_convention_in_the_portfolio() -> None:
    act360 = replace(INSTRUMENTS[0], yield_convention=YieldConvention(day_count=DayCount.ACT_360))
    base = row(run(), "p-usd")
    quoted = row(run(instruments=(act360,)), "p-usd")
    assert base.yield_convention == "yield:annual:ACT/365F"
    assert quoted.yield_convention == "yield:annual:ACT/360"
    assert (1 + quoted.effective_yield) ** (1 / 360) == pytest.approx(
        (1 + base.effective_yield) ** (1 / 365), abs=1e-14
    )
    assert quoted.macaulay_duration_years * 360 == pytest.approx(
        base.macaulay_duration_years * 365, rel=1e-9
    )
    # Valuation and risk do not depend on the quote basis.
    assert quoted.pv_rub == base.pv_rub and quoted.factor_risk == base.factor_risk


def test_g_spread_to_the_kbd_stays_on_act_365f() -> None:
    params, _ = load("zcyc_2026-10-15.json")
    rub = INSTRUMENTS[3]
    quoted = replace(rub, yield_convention=YieldConvention(day_count=DayCount.ACT_360))
    kbd = snapshot(curves=(CurveEntry("RUB_OFZ_KBD", "rub_ofz_kbd_v1", params),))
    policy = ValuationPolicy("RUB_OFZ_KBD", spread_kind="ofz")
    base = row(run(snapshot=kbd, policy=policy, positions=POSITIONS[4:]), "p-rub")
    other = row(
        run(snapshot=kbd, policy=policy, positions=POSITIONS[4:], instruments=(quoted,)), "p-rub"
    )
    assert other.effective_yield < base.effective_yield
    assert other.g_spread_bp == base.g_spread_bp


def test_coverage_rejects_missing_curves_fx_and_unsupported_currencies() -> None:
    usd_on_rub = replace(INSTRUMENTS[0], instrument_id="USD-ON-RUB")
    stale = snapshot(
        curves=(
            CurveEntry(
                "USD_SOFR", "usd_discount_factors_v1", node_curve("USD", date(2026, 10, 14))
            ),
        )
    )
    cases = [
        # (policy, snapshot, instruments, position, expected code)
        (
            POLICY,
            snapshot(),
            INSTRUMENTS,
            position("gbp", "GBP-BOND", 1, "GBP"),
            "UNSUPPORTED_PRODUCT",
        ),
        (ValuationPolicy("RUB_OIS"), snapshot(), INSTRUMENTS, POSITIONS[0], "MISSING_MARKET_DATA"),
        (
            POLICY,
            snapshot(fx=(FxRate("CNY", 11.4),)),
            INSTRUMENTS,
            POSITIONS[0],
            "MISSING_MARKET_DATA",
        ),
        (
            replace(POLICY, foreign_curves=(("USD", "NOT_IN_SNAPSHOT"),)),
            snapshot(),
            INSTRUMENTS,
            POSITIONS[0],
            "MISSING_MARKET_DATA",
        ),
        (
            replace(POLICY, curve_overrides=(("USD-ON-RUB", "RUB_OIS"),)),
            snapshot(),
            (usd_on_rub,),
            position("x", "USD-ON-RUB", 1, "USD"),
            "INVALID_TERMS",
        ),
        (
            replace(POLICY, foreign_curves=(("USD", "EUR_ESTR"),)),
            snapshot(),
            INSTRUMENTS,
            POSITIONS[0],
            "INVALID_TERMS",
        ),
        (POLICY, stale, INSTRUMENTS, POSITIONS[0], "INVALID_TERMS"),
        (POLICY, snapshot(), INSTRUMENTS, position("ccy", "USD-BOND", 1, "RUB"), "INVALID_TERMS"),
    ]
    for policy, snap, instruments, pos, code in cases:
        result = run(policy=policy, snapshot=snap, instruments=instruments, positions=(pos,))
        assert result.status == "failed" and result.positions == ()
        assert [r.code for r in result.coverage.rejections] == [code], (pos, code)
    flat = snapshot(
        curves=(
            CurveEntry(
                "USD_SOFR", "usd_discount_factors_v1", DiscountCurve.flat(BUSINESS_DATE, 0.04)
            ),
        )
    )
    result = run(snapshot=flat, positions=POSITIONS[:1])
    assert [r.code for r in result.coverage.rejections] == ["INVALID_TERMS"]


def test_non_annual_yield_quote_and_horizon_are_typed_rejections() -> None:
    from rub_fixed_income import SEMI_ANNUAL, horizon_portfolio

    semi = replace(INSTRUMENTS[0], yield_convention=YieldConvention(compounding=SEMI_ANNUAL))
    result = run(instruments=(semi,), positions=POSITIONS[:1])
    assert [r.code for r in result.coverage.rejections] == ["UNSUPPORTED_PRODUCT"]
    horizon = horizon_portfolio(
        snapshot=snapshot(),
        instruments=INSTRUMENTS,
        positions=POSITIONS,
        policy=POLICY,
        horizon_date=BUSINESS_DATE + timedelta(days=30),
    )
    codes = {r.position_id: r.code for r in horizon.coverage.rejections}
    assert codes == {p.position_id: "UNSUPPORTED_PRODUCT" for p in POSITIONS[:4]}


def test_policy_and_scenario_validation() -> None:
    with pytest.raises(ValueError, match="other than RUB"):
        ValuationPolicy("RUB_OIS", foreign_curves=(("RUB", "X"),))
    with pytest.raises(ValueError, match="duplicate"):
        ValuationPolicy("RUB_OIS", foreign_curves=(("USD", "A"), ("USD", "B")))
    with pytest.raises(ValueError, match="ISO 4217"):
        ValuationPolicy("RUB_OIS", foreign_curves=(("usd", "A"),))
    with pytest.raises(ValueError):
        PortfolioScenario("x", BondScenario(), (("USD", -100.0),))
    with pytest.raises(ValueError):
        PortfolioScenario("x", BondScenario(), (("USD", float("nan")),))
    with pytest.raises(ValueError, match="duplicate"):
        PortfolioScenario("x", BondScenario(), (("USD", 1.0), ("USD", 2.0)))


def test_fx_translation_helper_rejects_rub_and_duplicates() -> None:
    risk = FactorRisk(("CS",), (-3.0,), (HessianEntry(0, 0, 0.1),), "central", 1.0, True)
    added = fx_translated_factor_risk(risk, "USD", 12.0)
    assert added.factors == ("CS", "FX|USD") and added.first_order == (-3.0, 12.0)
    assert added.hessian == (HessianEntry(0, 0, 0.1), HessianEntry(0, 1, -0.03))
    first_only = replace(risk, hessian=(), has_second_order=False)
    assert fx_translated_factor_risk(first_only, "USD", 12.0).hessian == ()
    with pytest.raises(ValueError):
        fx_translated_factor_risk(risk, "RUB", 1.0)
    with pytest.raises(ValueError):
        fx_translated_factor_risk(added, "USD", 1.0)


def test_results_round_trip_and_rub_encodings_are_unchanged() -> None:
    scenario = PortfolioScenario("fx", BondScenario.parallel(5.0), (("USD", 3.0),))
    result = run(scenarios=(scenario,))
    assert verify_portfolio_result(result)
    data = portfolio_result_to_json(result)
    assert portfolio_result_from_json(data) == result
    raw = json.loads(data)
    rub_row = next(p for p in raw["positions"] if p["position_id"] == "p-rub")
    assert "currency_amounts" not in rub_row and "yield_convention" not in rub_row
    usd_row = next(p for p in raw["positions"] if p["position_id"] == "p-usd")
    assert usd_row["currency_amounts"]["currency"] == "USD"
    assert raw["fx_rates"] == [
        {"currency": c, "rub_per_unit": FX[c]} for c in ("CNY", "EUR", "USD")
    ]
    # A RUB-only book has none of the new keys: its bytes are those of earlier releases.
    rub_only = json.loads(portfolio_result_to_json(run(positions=POSITIONS[4:])))
    assert "fx_rates" not in rub_only
    assert "foreign_curves" not in to_dict(ValuationPolicy("RUB_OIS"))
    assert "fx_shocks_pct" not in to_dict(PortfolioScenario("p", BondScenario()))
    assert from_dict(ValuationPolicy, to_dict(POLICY)) == POLICY
    assert from_dict(PortfolioScenario, to_dict(scenario)) == scenario
