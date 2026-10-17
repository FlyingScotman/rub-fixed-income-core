"""CPI-indexed nominal and inflation-linked bonds (package 9a): analytical and contract tests.

MOEX and Bank of Russia evidence is in ``tests/test_ofz_in_goldens.py``.
"""

from __future__ import annotations

import json
import math
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from itertools import pairwise

import pytest

from rub_fixed_income import (
    CONTINUOUS,
    AccountingCategory,
    BondPriceObservation,
    BondRiskEngine,
    BondRiskRequest,
    BondScenario,
    BusinessCalendar,
    CalendarEntry,
    CashflowKind,
    CouponPeriod,
    CpiIndexation,
    CpiObservation,
    CpiSeries,
    CurveEntry,
    DayCount,
    DiscountCurve,
    FixedCouponLeg,
    IndexedCouponLeg,
    IndexedCouponPeriod,
    IndexedPrincipalLeg,
    Instrument,
    InvalidTerms,
    MarketSnapshot,
    MissingMarketData,
    Offer,
    OfferKind,
    PortfolioRiskResult,
    PortfolioScenario,
    Position,
    PrincipalLeg,
    ProductType,
    Redemption,
    ValuationPolicy,
    ZSpreadConvention,
    bond_factor_risk,
    canonical_sha256,
    from_dict,
    horizon_portfolio,
    indexed_accrued,
    indexed_coupon_amount,
    linker_accrued,
    linker_coupon,
    linker_factor_risk,
    linker_nominal,
    linker_redemption,
    portfolio_result_from_json,
    portfolio_result_to_json,
    real_cashflows,
    real_factor,
    to_dict,
    value_portfolio,
)
from rub_fixed_income.inflation import add_months

BASE = date(2025, 1, 15)
TRADE = date(2026, 10, 16)
SETTLE = date(2026, 10, 19)
CALENDAR = BusinessCalendar(version="synthetic-weekends-only")
SERIES = "CPI_TEST"


def levels(
    monthly_growth: float = 0.005, start: date = date(2024, 1, 1), months: int = 32
) -> tuple[CpiObservation, ...]:
    """Monthly levels 100 * (1 + g)^k, each published on the 12th of the next month."""
    return tuple(
        CpiObservation(
            add_months(start, k),
            100.0 * (1.0 + monthly_growth) ** k,
            add_months(start, k + 1).replace(day=12),
        )
        for k in range(months)
    )


def series(monthly_growth: float = 0.005, as_of: date = date(2026, 10, 16)) -> CpiSeries:
    rows = tuple(r for r in levels(monthly_growth) if r.published_on <= as_of)
    return CpiSeries(SERIES, "synthetic", as_of, rows)


def periods() -> tuple[IndexedCouponPeriod, ...]:
    dates = [BASE, date(2025, 7, 16), date(2026, 1, 14), date(2026, 7, 15), date(2027, 1, 13)]
    return tuple(IndexedCouponPeriod(s, e, e, 0.025) for s, e in pairwise(dates))


def bond(*, floor: bool = True, base_index: float | None = None, lag: int = 3) -> Instrument:
    indexation = CpiIndexation(SERIES, BASE, lag, base_index)
    return Instrument(
        "LINKER",
        ProductType.INFLATION_LINKED_BOND,
        "RUB",
        1000.0,
        IndexedCouponLeg(periods(), DayCount.ACT_365_FIXED),
        IndexedPrincipalLeg(date(2027, 1, 13), indexation, floor),
        "MOEX_STOCK",
    )


def fixed_bond() -> Instrument:
    return Instrument(
        "FIXED",
        ProductType.FIXED_BOND,
        "RUB",
        1000.0,
        FixedCouponLeg((CouponPeriod(date(2026, 7, 1), date(2027, 1, 1), date(2027, 1, 1), 50.0),)),
        PrincipalLeg((Redemption(date(2027, 1, 1), 1000.0),)),
        "MOEX_STOCK",
    )


# --- indexation formula ------------------------------------------------------------------


def test_reference_index_interpolates_from_m_minus_4_to_m_minus_3_by_day() -> None:
    cpi = series()
    rule = CpiIndexation(SERIES, BASE, 3)
    october = date(2026, 10, 1)
    low, high = cpi.level(date(2026, 6, 1)), cpi.level(date(2026, 7, 1))
    assert rule.reference_months(date(2026, 10, 16)) == (date(2026, 6, 1), date(2026, 7, 1))
    assert float(rule.reference_index(cpi, october)) == pytest.approx(low, rel=1e-15)
    on = date(2026, 10, 16)
    expected = low + (high - low) * 15 / 31
    assert float(rule.reference_index(cpi, on)) == pytest.approx(expected, rel=1e-15)
    # The 1st of the next month starts from CPI(M - 3): the path is continuous.
    november = float(rule.reference_index(cpi, date(2026, 11, 1)))
    assert november == pytest.approx(high, rel=1e-15)
    last = float(rule.reference_index(cpi, date(2026, 10, 31)))
    assert last == pytest.approx(low + (high - low) * 30 / 31, rel=1e-15)


def test_face_is_initial_face_times_the_index_ratio_rounded_half_up_to_kopecks() -> None:
    cpi = series()
    instrument = bond()
    assert linker_nominal(instrument, cpi, BASE) == 1000.0
    rule = CpiIndexation(SERIES, BASE, 3)
    ratio = rule.index_ratio(cpi, TRADE)
    raw = Decimal(1000) * ratio
    assert linker_nominal(instrument, cpi, TRADE) == float(raw.quantize(Decimal("0.01")))
    unrounded = CpiIndexation(SERIES, BASE, 3, nominal_quantum=None)
    assert unrounded.nominal(1000.0, cpi, TRADE) == pytest.approx(float(raw), rel=1e-15)
    # 21 months of 0.5 % from the base reference to the trade reference, interpolated.
    assert 1100.0 < linker_nominal(instrument, cpi, TRADE) < 1120.0


def test_explicit_base_index_is_used_as_published() -> None:
    cpi = series()
    computed = CpiIndexation(SERIES, BASE, 3).base(cpi)
    published = bond(base_index=float(computed) * 1.01)
    assert linker_nominal(published, cpi, TRADE) == pytest.approx(
        linker_nominal(bond(), cpi, TRADE) / 1.01, abs=0.011
    )


def test_rounding_is_half_up() -> None:
    period = IndexedCouponPeriod(date(2026, 1, 1), date(2026, 7, 1), date(2026, 7, 1), 0.025)
    # 1000 * 0.025 * 73 / 365 = 5.0 exactly; 1002 * 0.025 * 73 / 365 = 5.01
    on = date(2026, 3, 15)
    assert indexed_accrued(1000.0, period, on, DayCount.ACT_365_FIXED) == 5.0
    assert indexed_accrued(1002.0, period, on, DayCount.ACT_365_FIXED) == 5.01
    # 1001 * 0.025 * 73 / 365 = 5.005 -> 5.01 (half up)
    assert indexed_accrued(1001.0, period, on, DayCount.ACT_365_FIXED) == 5.01
    assert indexed_coupon_amount(1000.0, period, DayCount.ACT_360) == pytest.approx(
        round(1000 * 0.025 * 181 / 360, 2)
    )
    with pytest.raises(InvalidTerms):
        indexed_accrued(1000.0, period, date(2026, 7, 1), DayCount.ACT_365_FIXED)


def test_accrued_and_coupons_follow_formulas_four_and_five() -> None:
    cpi, instrument = series(), bond()
    period = periods()[3]
    face = linker_nominal(instrument, cpi, TRADE)
    days = (TRADE - period.accrual_start).days
    assert linker_accrued(instrument, cpi, TRADE) == round(face * 0.025 * days / 365 + 1e-12, 2)
    assert linker_accrued(instrument, cpi, period.accrual_start) == 0.0
    assert linker_accrued(instrument, cpi, date(2027, 1, 13)) == 0.0
    paid = periods()[2]
    face_paid = linker_nominal(instrument, cpi, paid.payment_date)
    assert linker_coupon(instrument, cpi, paid) == round(face_paid * 0.025 * 182 / 365, 2)
    with pytest.raises(InvalidTerms, match="not a coupon"):
        linker_coupon(instrument, cpi, IndexedCouponPeriod(BASE, TRADE, TRADE, 0.025))


def test_principal_floor_at_the_initial_face_under_deflation() -> None:
    falling = series(monthly_growth=-0.004)
    floored, unfloored = bond(floor=True), bond(floor=False)
    face = linker_nominal(floored, falling, SETTLE)
    assert face < 1000.0
    assert linker_redemption(floored, face) == 1000.0
    assert linker_redemption(unfloored, face) == face
    assert real_cashflows(floored, falling, SETTLE)[-1].principal == 1000.0
    assert real_cashflows(unfloored, falling, SETTLE)[-1].principal == face


def test_real_cashflows_hold_the_settlement_nominal() -> None:
    cpi, instrument = series(), bond()
    face = linker_nominal(instrument, cpi, SETTLE)
    flows = real_cashflows(instrument, cpi, SETTLE)
    assert [f.payment_date for f in flows] == [date(2027, 1, 13)]
    (last,) = flows
    assert last.kind is CashflowKind.MATURITY and last.principal == face
    assert last.coupon == round(face * 0.025 * 182 / 365, 2)
    assert real_cashflows(instrument, cpi, date(2027, 1, 13)) == ()


# --- typed rejections and point in time ------------------------------------------------


def test_missing_cpi_is_a_typed_rejection_never_a_fill() -> None:
    cpi = series(as_of=date(2026, 8, 20))  # July 2026 published 2026-08-12, August not yet
    instrument = bond()
    assert linker_nominal(instrument, cpi, date(2026, 10, 31)) > 1000.0
    with pytest.raises(MissingMarketData, match="2026-08"):
        linker_nominal(instrument, cpi, date(2026, 11, 1))
    with pytest.raises(InvalidTerms, match="base"):
        linker_nominal(instrument, cpi, date(2025, 1, 14))
    other = CpiSeries("OTHER", "x", cpi.as_of, cpi.observations)
    with pytest.raises(InvalidTerms, match="indexes to"):
        linker_nominal(instrument, other, TRADE)


def test_cpi_series_validation_and_point_in_time_cut() -> None:
    rows = levels()
    with pytest.raises(InvalidTerms, match="consecutive"):
        CpiSeries(SERIES, "x", date(2026, 10, 16), (rows[0], rows[2]))
    with pytest.raises(InvalidTerms, match="after the series as-of"):
        CpiSeries(SERIES, "x", date(2026, 9, 1), rows)
    with pytest.raises(InvalidTerms, match="first day"):
        CpiObservation(date(2026, 1, 2), 100.0, date(2026, 2, 12))
    with pytest.raises(InvalidTerms, match="before its month ends"):
        CpiObservation(date(2026, 1, 1), 100.0, date(2026, 1, 31))
    with pytest.raises(ValueError):
        CpiObservation(date(2026, 1, 1), math.nan, date(2026, 2, 12))
    with pytest.raises(InvalidTerms, match="'|'"):
        CpiSeries("A|B", "x", date(2026, 10, 16), rows[:1])
    full = series()
    cut = full.known_on(date(2026, 8, 20))
    assert cut.last_month == date(2026, 7, 1) and cut.as_of == date(2026, 8, 20)
    with pytest.raises(InvalidTerms):
        full.known_on(date(2026, 12, 1))
    with pytest.raises(MissingMarketData):
        full.known_on(date(2023, 1, 1))


def test_instrument_accepts_indexed_legs_only_on_a_linker() -> None:
    instrument = bond()
    assert instrument.is_indexed and instrument.maturity_date == date(2027, 1, 13)
    assert not instrument.is_perpetual and not fixed_bond().is_indexed
    principal, coupons = instrument.principal_leg, instrument.coupon_leg
    with pytest.raises(InvalidTerms, match="inflation-linked"):
        replace(instrument, product=ProductType.FIXED_BOND)
    with pytest.raises(InvalidTerms, match="indexed principal leg"):
        replace(fixed_bond(), product=ProductType.INFLATION_LINKED_BOND)
    with pytest.raises(InvalidTerms, match="indexed principal leg"):
        replace(fixed_bond(), coupon_leg=coupons)
    with pytest.raises(InvalidTerms, match="indexed coupon leg"):
        replace(instrument, coupon_leg=fixed_bond().coupon_leg)
    with pytest.raises(InvalidTerms, match="offers"):
        replace(instrument, offers=(Offer(date(2026, 1, 14), OfferKind.PUT, True),))
    assert isinstance(principal, IndexedPrincipalLeg)
    early = IndexedCouponPeriod(date(2025, 1, 1), date(2025, 7, 16), date(2025, 7, 16), 0.025)
    with pytest.raises(InvalidTerms, match="before the indexation base"):
        replace(instrument, coupon_leg=IndexedCouponLeg((early,), DayCount.ACT_365_FIXED))
    with pytest.raises(InvalidTerms, match="ACT/365F or ACT/360"):
        IndexedCouponLeg(periods(), DayCount.ACT_ACT_ISDA)
    with pytest.raises(InvalidTerms, match="maturity must follow"):
        IndexedPrincipalLeg(BASE, principal.indexation, True)
    for call in (
        lambda: instrument.cashflows(),
        lambda: instrument.accrued(TRADE),
        lambda: instrument.outstanding_face(TRADE),
    ):
        with pytest.raises(InvalidTerms, match="CPI"):
            call()


def test_indexation_rule_validation() -> None:
    with pytest.raises(InvalidTerms):
        CpiIndexation(SERIES, BASE, 0)
    with pytest.raises(TypeError):
        CpiIndexation(SERIES, BASE, True)
    with pytest.raises(InvalidTerms):
        CpiIndexation(SERIES, BASE, 3, nominal_quantum=Decimal("-0.01"))
    with pytest.raises(InvalidTerms):
        CpiIndexation(SERIES, BASE, 3, interpolation="monthly")  # type: ignore[arg-type]
    assert CpiIndexation(SERIES, BASE, 3).identifier == "cpi:lag3:daily_linear:nominal-0.01"


def test_linker_terms_round_trip_and_existing_types_keep_their_bytes() -> None:
    instrument = bond(base_index=123.45678)
    assert from_dict(Instrument, to_dict(instrument)) == instrument
    cpi = series()
    assert from_dict(CpiSeries, to_dict(cpi)) == cpi
    assert canonical_sha256(from_dict(CpiSeries, to_dict(cpi))) == canonical_sha256(cpi)
    plain = _snapshot(cpi=())
    assert "cpi" not in to_dict(plain)  # omitted at its default: snapshot hashes unchanged
    with_cpi = _snapshot()
    assert from_dict(MarketSnapshot, to_dict(with_cpi)) == with_cpi
    fixed = fixed_bond()
    assert from_dict(Instrument, to_dict(fixed)) == fixed


def test_snapshot_rejects_future_cut_cpi_and_duplicates() -> None:
    late = CpiSeries(SERIES, "x", date(2026, 10, 20), levels())
    with pytest.raises(InvalidTerms, match="after the snapshot business date"):
        _snapshot(cpi=(late,))
    with pytest.raises(InvalidTerms, match="duplicate"):
        _snapshot(cpi=(series(), series()))
    with pytest.raises(MissingMarketData):
        _snapshot().cpi_series("NONE")


# --- factors -----------------------------------------------------------------------------


def test_real_factor_keys() -> None:
    assert real_factor("RUB_REAL", "5Y") == "REAL|RUB_REAL|5Y"
    with pytest.raises(InvalidTerms):
        real_factor("RUB_REAL", "4Y")
    with pytest.raises(InvalidTerms):
        real_factor("A|B", "5Y")


def test_linker_factor_risk_renames_the_same_numbers() -> None:
    flows = real_cashflows(bond(), series(), SETTLE)
    engine = BondRiskEngine(
        BondRiskRequest(
            flows,
            SETTLE,
            real_curve(0.03),
            ZSpreadConvention(),
            "ofz",
            dirty_price_rub=1100.0,
        )
    )
    risk, second = engine.risk(difference="central"), engine.second_order()
    nominal, real = bond_factor_risk(risk, "C", second), linker_factor_risk(risk, "C", second)
    assert real.first_order == nominal.first_order and real.hessian == nominal.hessian
    assert real.factors == tuple(f.replace("IR|", "REAL|") for f in nominal.factors)


# --- portfolio ---------------------------------------------------------------------------


def real_curve(rate: float, curve_date: date = TRADE) -> DiscountCurve:
    nodes = tuple(
        (date(curve_date.year + k, curve_date.month, curve_date.day), math.exp(-rate * k))
        for k in range(1, 21)
    )
    return DiscountCurve(curve_date, nodes)


def _snapshot(cpi: tuple[CpiSeries, ...] | None = None) -> MarketSnapshot:
    return MarketSnapshot(
        "snap",
        TRADE,
        datetime(2026, 10, 16, 18, 0, tzinfo=UTC),
        curves=(
            CurveEntry("RUB_REAL", "rub_real_discount_factors_v1", real_curve(0.03)),
            CurveEntry("RUB_OIS", "rub_ruonia_ois_legacy_v1", real_curve(0.15)),
        ),
        calendars=(CalendarEntry("MOEX_STOCK", CALENDAR),),
        quotes=(
            BondPriceObservation("LINKER", 98.5, "CLOSE"),
            BondPriceObservation("FIXED", 99.0, "CLOSE"),
        ),
        cpi=(series(),) if cpi is None else cpi,
    )


def _value(
    instruments: list[Instrument],
    overrides: tuple[tuple[str, str], ...] = (("LINKER", "RUB_REAL"),),
    snapshot: MarketSnapshot | None = None,
    scenarios: tuple[PortfolioScenario, ...] = (),
    quantity: float = 10.0,
    spread_convention: ZSpreadConvention | None = None,
) -> PortfolioRiskResult:
    positions = [
        Position(
            f"p-{i.instrument_id}",
            i.instrument_id,
            quantity,
            "b",
            AccountingCategory.TRADING,
            "RUB",
        )
        for i in instruments
    ]
    return value_portfolio(
        run_id="run",
        as_of=datetime(2026, 10, 16, 19, 0, tzinfo=UTC),
        snapshot=_snapshot() if snapshot is None else snapshot,
        instruments=instruments,
        positions=positions,
        policy=ValuationPolicy(
            "RUB_OIS",
            spread_kind="ofz",
            curve_overrides=overrides,
            spread_convention=ZSpreadConvention()
            if spread_convention is None
            else spread_convention,
        ),
        scenarios=scenarios,
    )


def test_linker_in_value_portfolio_carries_real_factors_and_cpi_provenance() -> None:
    # A continuous spread makes the zero-shift DV01 analytical: -tau * PV * 1e-4.
    result = _value(
        [bond(), fixed_bond()], spread_convention=ZSpreadConvention(compounding=CONTINUOUS)
    )
    assert result.status == "complete", result.coverage.rejections
    linker, fixed = result.positions
    cpi = series()
    face = linker_nominal(bond(), cpi, SETTLE)
    assert linker.current_nominal_rub == face
    assert linker.accrued_interest_rub == linker_accrued(bond(), cpi, SETTLE)
    assert linker.dirty_price_rub == pytest.approx(0.985 * face + linker.accrued_interest_rub)
    assert linker.pv_rub == pytest.approx(10 * linker.dirty_price_rub)
    assert linker.spec_id == "rub_real_discount_factors_v1" and linker.g_spread_bp is None
    real = linker.factor_risk.first_order_by_factor()
    assert set(real) == {*(f"REAL|RUB_REAL|{b}" for b in _buckets()), "CS"}
    assert all(not k.startswith("REAL|") for k in fixed.factor_risk.factors)
    totals = {t.factor for t in result.factor_totals}
    assert "REAL|RUB_REAL|1Y" in totals and "IR|RUB_OIS|1Y" in totals
    # A long bond loses value when real rates rise; buckets sum to the total.
    dv01 = math.fsum(v for k, v in real.items() if k.startswith("REAL|"))
    assert dv01 < 0.0 and real["CS"] < 0.0
    # Analytical: DF(T)/DF(S) under a 1 bp continuous zero shift moves PV by -tau*PV*1e-4.
    (flow,) = real_cashflows(bond(), cpi, SETTLE)
    tau = (flow.payment_date - SETTLE).days / 365
    assert dv01 == pytest.approx(-tau * linker.pv_rub * 1e-4, rel=1e-6)
    assert real["CS"] == pytest.approx(-tau * linker.pv_rub * 1e-4, rel=1e-6)
    assert [c.series_id for c in result.cpi] == [SERIES]
    assert result.cpi[0].last_month == date(2026, 8, 1)
    restored = portfolio_result_from_json(portfolio_result_to_json(result))
    assert restored == result


def test_book_without_linkers_has_no_cpi_field_or_real_factors() -> None:
    result = _value([fixed_bond()], overrides=())
    assert result.cpi == ()
    raw = json.loads(portfolio_result_to_json(result))
    assert "cpi" not in raw
    assert not any(t.factor.startswith("REAL|") for t in result.factor_totals)


def test_curve_kinds_are_not_mixed() -> None:
    result = _value([bond(), fixed_bond()], overrides=(("FIXED", "RUB_REAL"),))
    codes = {r.instrument_id: r for r in result.coverage.rejections}
    assert codes["LINKER"].code == "INVALID_TERMS" and "real curve" in codes["LINKER"].message
    assert codes["FIXED"].code == "INVALID_TERMS" and "real curve" in codes["FIXED"].message


def test_missing_cpi_series_is_recorded_in_coverage() -> None:
    result = _value([bond()], snapshot=_snapshot(cpi=()))
    (rejection,) = result.coverage.rejections
    assert rejection.code == "MISSING_MARKET_DATA" and result.total_pv_rub is None
    stale = series(as_of=date(2026, 7, 20))  # June is the last month; settlement needs July
    result = _value([bond()], snapshot=_snapshot(cpi=(stale,)))
    (rejection,) = result.coverage.rejections
    assert rejection.code == "MISSING_MARKET_DATA" and "2026-07" in rejection.message


def test_nominal_scenarios_do_not_move_linkers() -> None:
    shock = PortfolioScenario("up", BondScenario.parallel(100.0))
    result = _value([bond(), fixed_bond()], scenarios=(shock,))
    (scenario,) = result.scenarios
    rows = {r.position_id: r for r in scenario.positions}
    assert rows["p-LINKER"].pnl_rub is None and rows["p-LINKER"].code == "UNSUPPORTED_PRODUCT"
    assert rows["p-FIXED"].pnl_rub is not None and rows["p-FIXED"].pnl_rub < 0.0
    assert scenario.total_pnl_rub is None and result.status == "partial"


def test_quote_accrued_overrides_and_horizon_is_rejected() -> None:
    snapshot = replace(
        _snapshot(),
        quotes=(BondPriceObservation("LINKER", 98.5, "CLOSE", accrued_per_bond=7.77),),
    )
    result = _value([bond()], snapshot=snapshot)
    assert result.positions[0].accrued_interest_rub == 7.77
    horizon = horizon_portfolio(
        snapshot=_snapshot(),
        instruments=[bond()],
        positions=[Position("p", "LINKER", 1.0, "b", AccountingCategory.TRADING, "RUB")],
        policy=ValuationPolicy("RUB_OIS", curve_overrides=(("LINKER", "RUB_REAL"),)),
        horizon_date=date(2026, 11, 16),
    )
    assert horizon.coverage.rejections[0].code == "UNSUPPORTED_PRODUCT"


def _buckets() -> tuple[str, ...]:
    from rub_fixed_income import RISK_BUCKETS

    return tuple(RISK_BUCKETS)
