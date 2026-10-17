"""End-to-end M1 slice: a synthetic OFZ book on the KBD curve (roadmap §4, step 1)."""

from __future__ import annotations

import json
import math
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from test_kbd import load

from rub_fixed_income import (
    RISK_BUCKETS,
    AccountingCategory,
    BondPriceObservation,
    BondScenario,
    BusinessCalendar,
    CalendarEntry,
    CashflowKind,
    CouponPeriod,
    CurveBootstrapRequest,
    CurveEntry,
    DayCount,
    FixedCouponLeg,
    FloatingCouponLeg,
    FloatingCouponPeriod,
    FloatingCouponRule,
    Instrument,
    MarketSnapshot,
    Offer,
    OfferKind,
    OisQuote,
    PortfolioRiskResult,
    PortfolioScenario,
    Position,
    PrincipalLeg,
    ProductType,
    Redemption,
    ValuationPolicy,
    canonical_sha256,
    g_spread_bp,
    kbd_effective_yield,
    portfolio_result_from_json,
    portfolio_result_to_json,
    value_portfolio,
    verify_portfolio_result,
)
from rub_fixed_income.dates import add_months

PARAMS, _ = load("zcyc_2026-10-15.json")
BUSINESS_DATE = PARAMS.trade_date  # Thursday 15 October 2026
AS_OF = datetime(2026, 10, 15, 16, 30, tzinfo=UTC)
CALENDAR = BusinessCalendar(
    holidays=frozenset({date(2026, 11, 4)}),
    version="synthetic-moex-stock-2026",
    valid_from=date(2026, 1, 1),
    valid_to=date(2027, 12, 31),
)
FIXTURE = Path(__file__).parent / "fixtures" / "portfolio_result_v2.json"


def periods(first_start: date, count: int, coupon: float) -> tuple[CouponPeriod, ...]:
    out = []
    for i in range(count):
        start, end = add_months(first_start, 6 * i), add_months(first_start, 6 * (i + 1))
        out.append(CouponPeriod(start, end, end, coupon))
    return tuple(out)


def bond(instrument_id: str, legs: tuple[CouponPeriod, ...], **kw: object) -> Instrument:
    redemptions = kw.pop("redemptions", None) or (Redemption(legs[-1].payment_date, 1000.0),)
    return Instrument(
        instrument_id,
        ProductType.FIXED_BOND,
        "RUB",
        1000.0,
        FixedCouponLeg(legs),
        PrincipalLeg(redemptions),  # type: ignore[arg-type]
        "MOEX_STOCK",
        **kw,  # type: ignore[arg-type]
    )


def instruments() -> tuple[Instrument, ...]:
    amort = periods(date(2026, 7, 1), 6, 30.0)
    rule = FloatingCouponRule(
        "ruonia", "compounded", 0.013, DayCount.ACT_365_FIXED, DayCount.ACT_365_FIXED
    )
    return (
        bond("OFZ-BULLET", periods(date(2026, 4, 15), 10, 35.4)),
        bond(
            "OFZ-AMORT",
            amort,
            redemptions=(
                Redemption(amort[2].payment_date, 500.0, CashflowKind.AMORTIZATION),
                Redemption(amort[-1].payment_date, 500.0),
            ),
        ),
        bond(
            "CORP-PUT",
            periods(date(2026, 9, 1), 8, 70.0),
            offers=(Offer(date(2028, 3, 1), OfferKind.PUT, confirmed=True),),
        ),
        bond("OFZ-NOQUOTE", periods(date(2026, 4, 15), 4, 30.0)),
        Instrument(
            "OFZ-PK",
            ProductType.FLOATING_BOND,
            "RUB",
            1000.0,
            FloatingCouponLeg(
                rule,
                (
                    FloatingCouponPeriod(
                        date(2026, 8, 1), date(2026, 11, 1), date(2026, 11, 1), 1000.0
                    ),
                ),
                "RUONIA",
            ),
            PrincipalLeg((Redemption(date(2029, 11, 1), 1000.0),)),
            "MOEX_STOCK",
        ),
    )


def snapshot(**changes: object) -> MarketSnapshot:
    base = MarketSnapshot(
        "hub-eod-2026-10-15",
        BUSINESS_DATE,
        AS_OF,
        curves=(
            CurveEntry("RUB_OFZ_KBD", "rub_ofz_kbd_v1", PARAMS),
            CurveEntry("BAD_SPEC", "rub_unknown_v9", PARAMS),
            CurveEntry(
                "RUB_OIS",
                "rub_ruonia_ois_legacy_v1",
                CurveBootstrapRequest(
                    BUSINESS_DATE,
                    tuple(
                        OisQuote(f"{y}Y", BUSINESS_DATE + timedelta(days=365 * y), 0.14 - 0.002 * y)
                        for y in (1, 2, 3, 5, 7, 10)
                    ),
                ),
            ),
        ),
        calendars=(CalendarEntry("MOEX_STOCK", CALENDAR),),
        quotes=(
            BondPriceObservation("OFZ-BULLET", 92.15, "waprice"),
            BondPriceObservation("OFZ-AMORT", 98.4, "close"),
            BondPriceObservation("CORP-PUT", 99.1, "waprice", accrued_per_bond=17.5),
            BondPriceObservation("OFZ-PK", 100.2, "waprice"),
        ),
    )
    return replace(base, **changes)  # type: ignore[arg-type]


def positions() -> tuple[Position, ...]:
    def pos(pid: str, iid: str, qty: float, recovery: float | None = None) -> Position:
        return Position(pid, iid, qty, "RATES", AccountingCategory.TRADING, "RUB", recovery)

    return (
        pos("p-bullet", "OFZ-BULLET", 1000, 0.3),
        pos("p-amort", "OFZ-AMORT", -400),
        pos("p-put", "CORP-PUT", 250, 0.4),
        pos("p-noquote", "OFZ-NOQUOTE", 10),
        pos("p-floater", "OFZ-PK", 10),
        pos("p-unknown", "NOT-SUPPLIED", 10),
        pos("p-ois", "OFZ-BULLET", 50),
        pos("p-badspec", "OFZ-AMORT", 5),
    )


POLICY = ValuationPolicy(
    "RUB_OFZ_KBD",
    spread_kind="ofz",
    horizon="confirmed_put",
    curve_overrides=(),
)


def run(**kw: object) -> PortfolioRiskResult:
    args: dict[str, object] = {
        "run_id": "eod-2026-10-15",
        "as_of": AS_OF,
        "snapshot": snapshot(),
        "instruments": instruments(),
        "positions": positions()[:6],
        "policy": POLICY,
        "scenarios": (PortfolioScenario("parallel+100", BondScenario.parallel(100)),),
    }
    args.update(kw)
    return value_portfolio(**args)  # type: ignore[arg-type]


def test_book_coverage_records_every_rejection_with_a_stable_code() -> None:
    result = run()
    assert result.status == "partial"
    assert result.coverage.expected == 6 and result.coverage.completed == 3
    codes = {r.position_id: r.code for r in result.coverage.rejections}
    assert codes == {
        "p-noquote": "MISSING_MARKET_DATA",
        "p-floater": "UNSUPPORTED_PRODUCT",
        "p-unknown": "INVALID_TERMS",
    }
    assert result.total_pv_rub is None
    assert result.covered_pv_rub == pytest.approx(math.fsum(p.pv_rub for p in result.positions))
    scenario = result.scenarios[0]
    assert scenario.total_pnl_rub is None
    assert {r.position_id for r in scenario.positions if r.pnl_rub is None} == set(codes)


def test_bullet_valuation_is_independently_reproduced() -> None:
    row = next(p for p in run().positions if p.position_id == "p-bullet")
    assert row.settlement_date == date(2026, 10, 16)
    assert row.curve_id == "RUB_OFZ_KBD" and row.spec_id == "rub_ofz_kbd_v1"
    # Period 15 Oct 2026 -> 15 Apr 2027, one day elapsed of 182.
    assert row.accrued_interest_rub == round(35.4 * 1 / 182, 2)
    assert row.dirty_price_rub == pytest.approx(921.5 + row.accrued_interest_rub)
    assert row.pv_rub == pytest.approx(1000 * row.dirty_price_rub)
    assert row.g_spread_bp == pytest.approx(
        g_spread_bp(PARAMS, row.effective_yield, row.macaulay_duration_years)
    )
    assert row.g_spread_bp == pytest.approx(
        (row.effective_yield - kbd_effective_yield(PARAMS, row.macaulay_duration_years)) * 1e4
    )
    # Price below par on a curve near 16 %: a positive OFZ spread and long-position DV01 < 0.
    assert row.factor_risk.factors[0] == "IR|RUB_OFZ_KBD|1M"
    assert sum(row.factor_risk.first_order[:-1]) < 0
    assert row.jtd_pnl_rub == pytest.approx(1000 * (0.3 * 1000 - row.dirty_price_rub))


def test_put_bond_is_valued_to_the_confirmed_offer_with_the_supplied_accrued() -> None:
    row = next(p for p in run().positions if p.position_id == "p-put")
    assert row.horizon == "confirmed_put" and row.horizon_date == date(2028, 3, 1)
    assert row.accrued_interest_rub == 17.5
    first = row.factor_risk.first_order_by_factor()
    assert first["IR|RUB_OFZ_KBD|20Y+"] == 0.0


def test_amortizer_uses_outstanding_face_and_negative_quantity() -> None:
    row = next(p for p in run().positions if p.position_id == "p-amort")
    assert row.current_nominal_rub == 1000.0
    assert row.pv_rub < 0
    assert sum(row.factor_risk.first_order[:-1]) > 0  # short position gains when rates rise


def test_taylor_from_factors_matches_exact_scenario() -> None:
    result = run(
        positions=positions()[:3], scenarios=(PortfolioScenario("up10", BondScenario.parallel(10)),)
    )
    assert result.status == "complete" and result.total_pv_rub is not None
    shock = 10.0
    taylor = 0.0
    for row in result.positions:
        risk = row.factor_risk
        ir = [i for i, f in enumerate(risk.factors) if f.startswith("IR|")]
        taylor += sum(risk.first_order[i] for i in ir) * shock
        for e in risk.hessian:
            if e.i in ir and e.j in ir:
                taylor += (0.5 if e.i == e.j else 1.0) * e.value * shock * shock
    exact = result.scenarios[0].total_pnl_rub
    assert exact is not None
    assert taylor == pytest.approx(exact, rel=2e-4)
    totals = {t.factor: t.first_order_rub for t in result.factor_totals}
    assert set(totals) == {f"IR|RUB_OFZ_KBD|{b}" for b in RISK_BUCKETS} | {"CS"}


def test_curve_overrides_ois_path_and_spec_rejections() -> None:
    policy = replace(POLICY, curve_overrides=(("OFZ-AMORT", "BAD_SPEC"), ("OFZ-BULLET", "RUB_OIS")))
    result = run(positions=positions()[6:], policy=policy, scenarios=())
    codes = {r.position_id: r.code for r in result.coverage.rejections}
    assert codes == {"p-badspec": "UNKNOWN_CURVE_SPEC"}
    (row,) = result.positions
    assert row.curve_basis == "ois_quote" and row.g_spread_bp is None
    assert row.factor_risk.factors[0] == "IR|RUB_OIS|1M"
    assert [c.curve_id for c in result.curves] == ["RUB_OIS"]


def test_calendar_identity_and_out_of_range_dates() -> None:
    result = run()
    (calendar,) = result.calendars
    assert calendar.version == "synthetic-moex-stock-2026"
    assert calendar.valid_to == date(2027, 12, 31)
    late = BusinessCalendar(
        version="2025-only", valid_from=date(2025, 1, 1), valid_to=date(2025, 12, 31)
    )
    stale = run(snapshot=snapshot(calendars=(CalendarEntry("MOEX_STOCK", late),)), scenarios=())
    assert {r.code for r in stale.coverage.rejections} >= {"CALENDAR_OUT_OF_RANGE"}
    assert stale.positions == ()


def test_interruption_rejects_remaining_positions() -> None:
    result = run(time_budget_seconds=0.0)
    assert result.status == "failed" and result.interruption == "time_budget_exceeded"
    assert {r.code for r in result.coverage.rejections} == {"TIME_BUDGET_EXCEEDED"}


def test_result_is_deterministic_hash_verified_and_round_trips() -> None:
    first, second = run(), run()
    assert first == second
    assert verify_portfolio_result(first)
    data = portfolio_result_to_json(first)
    assert portfolio_result_from_json(data) == first
    tampered = json.loads(data)
    tampered["covered_pv_rub"] += 1.0
    assert not verify_portfolio_result(tampered)
    with pytest.raises(ValueError, match="does not verify"):
        portfolio_result_from_json(json.dumps(tampered))
    assert canonical_sha256(snapshot()) == canonical_sha256(snapshot())


def test_frozen_v2_fixture_still_parses_and_reserialises_byte_for_byte() -> None:
    data = FIXTURE.read_bytes()
    assert portfolio_result_to_json(portfolio_result_from_json(data)) == data
