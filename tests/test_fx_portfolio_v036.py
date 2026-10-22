"""FX products in ``value_portfolio`` (0.36.0): any currency, implied curves from swap
points, crosses, NDF fixings, fallbacks to proxies, points-held risk, basis scenarios.

Each row is checked against its engine run standalone on the same inputs.
"""

from __future__ import annotations

import json
import math
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest
from portfolio_v3_book import AS_OF, BUSINESS_DATE, CAL, POLICY, TOM, node_curve
from portfolio_v3_book import instruments as book_instruments
from portfolio_v3_book import positions as book_positions
from portfolio_v3_book import snapshot as book_snapshot

from rub_fixed_income import (
    PAR_YIELD_PROXY_MODEL_VERSION,
    AccountingCategory,
    BondScenario,
    CalendarEntry,
    CurveEntry,
    DiscountCurve,
    FxBasis,
    FxCrossForward,
    FxCrossInstrument,
    FxCrossRiskEngine,
    FxCrossRiskRequest,
    FxFixing,
    FxForward,
    FxForwardInstrument,
    FxForwardLeg,
    FxForwardRiskEngine,
    FxForwardRiskRequest,
    FxFuturesScenario,
    FxImpliedCurveRequest,
    FxSpot,
    FxSwapPointsQuote,
    InvalidTerms,
    MarketSnapshot,
    PortfolioInstrument,
    PortfolioRiskResultV3,
    PortfolioScenario,
    Position,
    ShockedCurveCache,
    ValuationPolicy,
    fx_cross_factor_risk,
    fx_cross_rub_legs,
    fx_forward_factor_risk,
    horizon_portfolio,
    implied_foreign_curve,
    portfolio_result_from_json,
    portfolio_result_to_json,
    to_dict,
    value_portfolio,
    verify_portfolio_result_v3,
)
from rub_fixed_income.portfolio_products import scale_factor_risk

RUB_CURVE = node_curve(0.15, -0.004)
FAR = date(2027, 4, 15)
SPOTS = {
    "USD": (81.5, 1.0),
    "CNY": (11.2, 1.0),
    "CHF": (101.3, 1.0),
    "GBP": (108.4, 1.0),
    "HKD": (10.45, 1.0),
    "JPY": (0.545, 100.0),
    "KZT": (0.158, 100.0),
    "AED": (22.2, 1.0),
    "TRY": (1.95, 1.0),
}
LEVELS = {
    "USD": 0.042,
    "CNY": 0.016,
    "CHF": 0.004,
    "GBP": 0.04,
    "HKD": 0.035,
    "JPY": 0.006,
    "KZT": 0.16,
    "AED": 0.04,
    "TRY": 0.38,
}


def built(entry: CurveEntry) -> DiscountCurve:
    assert isinstance(entry.curve, DiscountCurve)
    return entry.curve


def df_curve(currency: str) -> CurveEntry:
    return CurveEntry(
        f"{currency}_DF",
        f"{currency.lower()}_discount_factors_v1",
        node_curve(LEVELS[currency], 0.0005),
    )


def market(*currencies: str, **changes: object) -> MarketSnapshot:
    base = MarketSnapshot(
        "fx-0350",
        BUSINESS_DATE,
        AS_OF,
        curves=(
            CurveEntry("RUB_OIS", "rub_ruonia_ois_legacy_v1", RUB_CURVE),
            *(df_curve(c) for c in currencies),
        ),
        calendars=(CalendarEntry("FX", CAL),),
        fx_spots=tuple(FxSpot(c, SPOTS[c][0], TOM, "FX") for c in currencies),
        fx_bases=tuple(FxBasis(c, f"{c}_DF", 3.0) for c in currencies),
    )
    return replace(base, **changes)  # type: ignore[arg-type]


def pos(pid: str, iid: str, qty: float, ccy: str) -> Position:
    return Position(pid, iid, qty, "DESK", AccountingCategory.TRADING, ccy)


def run(
    snapshot: MarketSnapshot,
    instruments: Sequence[PortfolioInstrument],
    positions: list[Position],
    policy: ValuationPolicy = POLICY,
    scenarios: tuple[PortfolioScenario, ...] = (),
) -> PortfolioRiskResultV3:
    result = value_portfolio(
        run_id="fx-0350",
        as_of=AS_OF,
        snapshot=snapshot,
        instruments=instruments,
        positions=positions,
        policy=policy,
        scenarios=scenarios,
    )
    assert isinstance(result, PortfolioRiskResultV3)
    return result


def standalone(
    trade: FxForward, foreign: DiscountCurve, basis: float, spot: float
) -> FxForwardRiskEngine:
    return FxForwardRiskEngine(
        FxForwardRiskRequest(trade, spot * trade.quote_units, TOM, RUB_CURVE, foreign, basis),
        ShockedCurveCache(),
    )


# --- C1: any currency -------------------------------------------------------------------


@pytest.mark.parametrize("currency", ["CHF", "GBP", "HKD", "JPY", "KZT", "AED", "TRY"])
def test_forward_in_any_currency_values_like_the_engine(currency: str) -> None:
    spot, units = SPOTS[currency]
    trade = FxForward(
        f"FWD-{currency}", currency, units, FxForwardLeg(FAR, 2_000_000.0, spot * units * 1.05)
    )
    inst = FxForwardInstrument(f"FWD-{currency}", trade, "RUB_OIS", f"{currency}_DF")
    result = run(market(currency), [inst], [pos("p", inst.instrument_id, -1.5, currency)])
    assert result.coverage.status == "complete"
    row = result.fx_forwards[0]
    engine = standalone(trade, built(df_curve(currency)), 3.0, spot)
    risk = engine.risk(difference="central")
    assert row.pv_rub == pytest.approx(-1.5 * engine.base_pv_rub, rel=1e-13)
    assert row.fx_delta_foreign == pytest.approx(-1.5 * risk.fx_delta_foreign, rel=1e-13)
    expected = scale_factor_risk(
        fx_forward_factor_risk(risk, "RUB_OIS", f"{currency}_DF", engine.second_order()), -1.5
    )
    assert row.factor_risk == expected
    assert sorted((c.curve_id, c.spec_id) for c in result.curves) == sorted(
        [
            (f"{currency}_DF", f"{currency.lower()}_discount_factors_v1"),
            ("RUB_OIS", "rub_ruonia_ois_legacy_v1"),
        ]
    )
    by_factor = {t.factor: t.first_order_rub for t in result.factor_totals}
    assert by_factor[f"FX|{currency}"] < 0.0  # short the currency
    assert sum(v for k, v in by_factor.items() if k.startswith(f"IR|{currency}_DF|")) > 0.0


def _chf_quotes() -> tuple[FxSwapPointsQuote, ...]:
    return (
        FxSwapPointsQuote("TODTOM", BUSINESS_DATE, TOM, 0.04),
        FxSwapPointsQuote("TOM3M", TOM, date(2027, 1, 18), 3.4),
        FxSwapPointsQuote("TOM6M", TOM, FAR, 6.9),
        FxSwapPointsQuote("TOM1Y", TOM, date(2027, 10, 18), 14.2),
    )


def implied_market(**changes: object) -> MarketSnapshot:
    request = FxImpliedCurveRequest("CHF", BUSINESS_DATE, "RUB_OIS", _chf_quotes())
    base = market(
        curves=(
            CurveEntry("RUB_OIS", "rub_ruonia_ois_legacy_v1", RUB_CURVE),
            CurveEntry("CHF_FXI", "chf_fx_implied_v1", request),
        ),
        fx_spots=(FxSpot("CHF", SPOTS["CHF"][0], TOM, "FX"),),
        fx_bases=(),
    )
    return replace(base, **changes)  # type: ignore[arg-type]


def test_chf_forward_on_a_curve_implied_from_swap_points_in_the_snapshot() -> None:
    spot = SPOTS["CHF"][0]
    at_market = spot + 6.9  # the TOM/6M outright: the quote reprices exactly
    trade = FxForward("FWD-CHF", "CHF", 1.0, FxForwardLeg(FAR, 1_000_000.0, at_market))
    off = FxForward("FWD-CHF-2", "CHF", 1.0, FxForwardLeg(date(2027, 2, 1), -3_000_000.0, 103.0))
    insts = [FxForwardInstrument(t.trade_id, t, "RUB_OIS", "CHF_FXI") for t in (trade, off)]
    result = run(
        implied_market(),
        insts,
        [pos("a", "FWD-CHF", 1.0, "CHF"), pos("b", "FWD-CHF-2", 1.0, "CHF")],
    )
    assert result.coverage.status == "complete"
    rows = {r.instrument_id: r for r in result.fx_forwards}
    assert abs(rows["FWD-CHF"].pv_rub) < 1e-6
    assert rows["FWD-CHF"].basis_bp == 0.0  # implied: zero without an FxBasis entry
    curve = implied_foreign_curve(
        rub_curve=RUB_CURVE, spot_rate=spot, spot_value_date=TOM, quotes=_chf_quotes()
    )
    engine = standalone(off, curve, 0.0, spot)
    assert rows["FWD-CHF-2"].pv_rub == pytest.approx(engine.base_pv_rub, rel=1e-13)
    assert rows["FWD-CHF-2"].factor_risk == fx_forward_factor_risk(
        engine.risk(difference="central"), "RUB_OIS", "CHF_FXI", engine.second_order()
    )
    use = {c.curve_id: c for c in result.curves}["CHF_FXI"]
    assert (use.spec_id, use.model_version, use.proxy) == (
        "chf_fx_implied_v1",
        "fx-implied-logdf-v1",
        False,
    )
    assert [s.currency for s in result.fx_spots] == ["CHF"]


def test_implied_curve_rejections() -> None:
    trade = FxForward("F", "CHF", 1.0, FxForwardLeg(FAR, 1e6, 108.0))
    inst = FxForwardInstrument("F", trade, "RUB_OIS", "CHF_FXI")
    p = [pos("a", "F", 1.0, "CHF")]

    def code(snapshot: MarketSnapshot) -> tuple[str, str]:
        result = run(snapshot, [inst], p)
        rejection = result.coverage.rejections[0]
        return rejection.code, rejection.message

    nonzero = implied_market(fx_bases=(FxBasis("CHF", "CHF_FXI", 2.0),))
    assert code(nonzero)[0] == "INVALID_TERMS" and "zero" in code(nonzero)[1]
    zero = implied_market(fx_bases=(FxBasis("CHF", "CHF_FXI", 0.0),))
    assert run(zero, [inst], p).coverage.status == "complete"
    unanchored = FxImpliedCurveRequest("CHF", BUSINESS_DATE, "RUB_OIS", _chf_quotes()[1:])
    snap = implied_market(
        curves=(
            CurveEntry("RUB_OIS", "rub_ruonia_ois_legacy_v1", RUB_CURVE),
            CurveEntry("CHF_FXI", "chf_fx_implied_v1", unanchored),
        )
    )
    assert code(snap)[0] == "INVALID_TERMS" and "anchor" in code(snap)[1]
    stale = replace(unanchored, curve_date=BUSINESS_DATE - timedelta(days=1), quotes=_chf_quotes())
    snap = implied_market(
        curves=(
            CurveEntry("RUB_OIS", "rub_ruonia_ois_legacy_v1", RUB_CURVE),
            CurveEntry("CHF_FXI", "chf_fx_implied_v1", stale),
        )
    )
    assert code(snap) == (
        "INVALID_TERMS",
        "FX swap quotes CHF_FXI are not dated the snapshot business date",
    )
    wrong_spec = implied_market(
        curves=(
            CurveEntry("RUB_OIS", "rub_ruonia_ois_legacy_v1", RUB_CURVE),
            CurveEntry("CHF_FXI", "chf_discount_factors_v1", unanchored),
        )
    )
    assert code(wrong_spec)[0] == "INVALID_TERMS"
    no_spot = implied_market(fx_spots=())
    assert code(no_spot)[0] == "MISSING_MARKET_DATA"


def test_snapshot_with_implied_quotes_and_fixings_round_trips() -> None:
    from rub_fixed_income import from_dict

    snap = implied_market(
        fx_fixings=(
            FxFixing("USD", "CBR", BUSINESS_DATE, 81.2, datetime(2026, 10, 15, 12, 30, tzinfo=UTC)),
        )
    )
    assert from_dict(MarketSnapshot, to_dict(snap)) == snap
    assert "fx_fixings" not in to_dict(market("USD"))


# --- C2: crosses ------------------------------------------------------------------------


def _cross_book() -> tuple[FxCrossInstrument, MarketSnapshot]:
    trade = FxCrossForward("X-USDCNY", "USD", "CNY", 1.0, FxForwardLeg(FAR, 5_000_000.0, 7.05))
    return FxCrossInstrument("X-USDCNY", trade, "RUB_OIS", "USD_DF", "CNY_DF", "FX"), market(
        "USD", "CNY"
    )


def test_cross_in_value_portfolio_matches_the_engine_and_the_two_rub_legs() -> None:
    inst, snap = _cross_book()
    result = run(snap, [inst], [pos("x", "X-USDCNY", 2.0, "USD")])
    assert result.coverage.status == "complete"
    row = result.fx_crosses[0]
    req = FxCrossRiskRequest(
        inst.trade,
        81.5,
        TOM,
        11.2,
        TOM,
        RUB_CURVE,
        built(df_curve("USD")),
        built(df_curve("CNY")),
        3.0,
        3.0,
    )
    engine = FxCrossRiskEngine(req, ShockedCurveCache())
    risk = engine.risk(difference="central")
    assert row.pv_rub == pytest.approx(2.0 * engine.base_pv_rub, rel=1e-13)
    assert row.factor_risk == scale_factor_risk(
        fx_cross_factor_risk(risk, "RUB_OIS", "USD_DF", "CNY_DF", engine.second_order()), 2.0
    )
    assert row.fx_delta_base == pytest.approx(2.0 * risk.fx_delta_base, rel=1e-13)
    assert row.fx_delta_quote == pytest.approx(2.0 * risk.fx_delta_quote, rel=1e-13)
    assert row.legs[0].base_notional == 10_000_000.0
    assert row.legs[0].quote_notional == pytest.approx(-70_500_000.0)
    # The same trade as two RUB forwards (any pivot): same PV, the RUB legs' risk cancels.
    base, quote = fx_cross_rub_legs(inst.trade, pivot_rate=80.0)
    legs = [
        FxForwardInstrument("B", base, "RUB_OIS", "USD_DF"),
        FxForwardInstrument("Q", quote, "RUB_OIS", "CNY_DF"),
    ]
    pair = run(snap, legs, [pos("b", "B", 2.0, "USD"), pos("q", "Q", 2.0, "CNY")])
    assert pair.covered_pv_rub == pytest.approx(result.covered_pv_rub, rel=1e-12)
    single = {t.factor: t.first_order_rub for t in result.factor_totals}
    split = {t.factor: t.first_order_rub for t in pair.factor_totals}
    assert set(single) == set(split)
    for factor, value in single.items():
        assert split[factor] == pytest.approx(value, rel=1e-6, abs=1e-3), factor
    assert [m.flags for m in result.position_models] == [("covered_parity", "fx_cross")]
    assert result.position_models[0].product == "fx_forward"
    data = portfolio_result_to_json(result)
    assert portfolio_result_from_json(data) == result
    assert verify_portfolio_result_v3(json.loads(data))


def test_cross_rejections_and_horizon() -> None:
    inst, snap = _cross_book()
    wrong = run(snap, [inst], [pos("x", "X-USDCNY", 1.0, "CNY")])
    assert wrong.coverage.rejections[0].code == "INVALID_TERMS"
    no_quote = run(
        replace(snap, fx_spots=snap.fx_spots[1:]), [inst], [pos("x", "X-USDCNY", 1.0, "USD")]
    )
    assert no_quote.coverage.rejections[0].code == "MISSING_MARKET_DATA"
    holiday = replace(
        inst,
        trade=FxCrossForward(
            "X-USDCNY", "USD", "CNY", 1.0, FxForwardLeg(date(2026, 11, 4), 1e6, 7.1)
        ),
    )
    rejected = run(snap, [holiday], [pos("x", "X-USDCNY", 1.0, "USD")])
    assert "not a business day" in rejected.coverage.rejections[0].message
    carry = horizon_portfolio(
        snapshot=snap,
        instruments=[inst],
        positions=[pos("x", "X-USDCNY", 1.0, "USD")],
        policy=POLICY,
        horizon_date=date(2026, 11, 16),
    )
    assert carry.coverage.status == "complete"
    row = carry.fx_crosses[0]
    assert row.per_trade.base_horizon_spot_value_date == date(2026, 11, 17)
    assert row.horizon_pnl_rub == pytest.approx(row.carry_rub + row.rolldown_rub, abs=1e-6)


# --- C3: NDF fixings in the portfolio ---------------------------------------------------


def _ndf(fixing_date: date, **changes: object) -> FxForwardInstrument:
    leg = FxForwardLeg(
        fixing_date + timedelta(days=1),
        1_000_000.0,
        80.0,
        fixing_date + timedelta(days=2),
        fixing_date=fixing_date,
        fixing_source="CBR",
    )
    trade = FxForward("NDF", "USD", 1.0, replace(leg, **changes))  # type: ignore[arg-type]
    return FxForwardInstrument("NDF", trade, "RUB_OIS", "USD_DF")


def _ndf_run(inst: FxForwardInstrument, *fixings: FxFixing) -> PortfolioRiskResultV3:
    return run(market("USD", fx_fixings=fixings), [inst], [pos("n", "NDF", 1.0, "USD")])


def test_published_fixing_freezes_the_ndf_leg() -> None:
    fixed_on = BUSINESS_DATE - timedelta(days=1)  # Wednesday; value Thursday, paid Friday
    result = _ndf_run(_ndf(fixed_on), FxFixing("USD", "CBR", fixed_on, 81.0))
    row = result.fx_forwards[0]
    leg = row.legs[0]
    assert (leg.fixing_date, leg.fixing_rate, leg.forward_rate) == (fixed_on, 81.0, 81.0)
    df = RUB_CURVE.discount_factor(TOM)
    assert row.pv_rub == pytest.approx(1_000_000.0 * 1.0 * df, rel=1e-13)
    assert row.fx_delta_foreign == 0.0
    first = row.factor_risk.first_order_by_factor()
    assert first["FX|USD"] == 0.0 and first["FXBASIS|USD"] == 0.0
    assert all(v == 0.0 for k, v in first.items() if k.startswith("IR|USD_DF|"))
    assert result.position_models[0].flags == ("covered_parity", "known_fixings")
    assert "fixing_rate" in to_dict(leg) and "fixing_rate" not in to_dict(
        run(
            market("USD"),
            [
                FxForwardInstrument(
                    "F",
                    FxForward("F", "USD", 1.0, FxForwardLeg(FAR, 1.0, 80.0)),
                    "RUB_OIS",
                    "USD_DF",
                )
            ],
            [pos("f", "F", 1.0, "USD")],
        )
        .fx_forwards[0]
        .legs[0]
    )
    # Quote units scale the published per-unit rate.
    base_trade = _ndf(fixed_on).trade
    assert isinstance(base_trade, FxForward)
    trade_100 = FxForward("NDF", "USD", 100.0, replace(base_trade.leg, contract_rate=8000.0))
    per_100 = replace(_ndf(fixed_on), trade=trade_100)
    hundred = _ndf_run(per_100, FxFixing("USD", "CBR", fixed_on, 81.0)).fx_forwards[0]
    assert hundred.legs[0].fixing_rate == 8100.0
    # N / q * (fixing - K): 1e6 / 100 * (8100 - 8000), the same RUB amount.
    assert hundred.pv_rub == pytest.approx(row.pv_rub, rel=1e-13)


def test_fixing_due_today_is_pending_until_published_and_missing_after() -> None:
    today = _ndf(BUSINESS_DATE)
    pending = _ndf_run(today)
    assert pending.coverage.status == "complete"
    assert pending.position_models[0].flags == ("covered_parity", "fixing_pending")
    assert pending.fx_forwards[0].fx_delta_foreign != 0.0
    late = FxFixing("USD", "CBR", BUSINESS_DATE, 81.0, published_at=AS_OF + timedelta(hours=1))
    assert _ndf_run(today, late).position_models[0].flags == ("covered_parity", "fixing_pending")
    seen = replace(late, published_at=AS_OF - timedelta(hours=1))
    assert _ndf_run(today, seen).fx_forwards[0].legs[0].fixing_rate == 81.0
    yesterday = _ndf(BUSINESS_DATE - timedelta(days=1))
    missing = _ndf_run(yesterday, FxFixing("USD", "MOEX", BUSINESS_DATE - timedelta(days=1), 81.0))
    rejection = missing.coverage.rejections[0]
    assert rejection.code == "MISSING_MARKET_DATA" and "fixing (CBR)" in rejection.message
    no_source = _ndf(BUSINESS_DATE - timedelta(days=1), fixing_source=None)
    assert _ndf_run(no_source).coverage.rejections[0].code == "MISSING_MARKET_DATA"


def test_fixing_in_the_terms_is_used_and_checked_against_the_snapshot() -> None:
    fixed_on = BUSINESS_DATE - timedelta(days=1)
    inst = _ndf(fixed_on, fixing_rate=81.0)
    assert _ndf_run(inst).fx_forwards[0].legs[0].fixing_rate == 81.0
    assert _ndf_run(inst, FxFixing("USD", "CBR", fixed_on, 81.0)).coverage.status == "complete"
    clash = _ndf_run(inst, FxFixing("USD", "CBR", fixed_on, 81.3))
    assert clash.coverage.rejections[0].code == "INVALID_TERMS"
    paid = _ndf(BUSINESS_DATE - timedelta(days=5), fixing_rate=81.0)
    assert _ndf_run(paid).coverage.rejections[0].code == "SETTLED_LEG"


def test_fixed_ndf_carries_its_known_amount_in_horizon_portfolio() -> None:
    fixed_on = BUSINESS_DATE - timedelta(days=1)
    snap = market("USD", fx_fixings=(FxFixing("USD", "CBR", fixed_on, 81.0),))
    carry = horizon_portfolio(
        snapshot=snap,
        instruments=[_ndf(fixed_on)],
        positions=[pos("n", "NDF", 1.0, "USD")],
        policy=POLICY,
        horizon_date=date(2026, 10, 19),
    )
    row = carry.fx_forwards[0]
    assert row.received_rub == pytest.approx(1_000_000.0, rel=1e-14)


# --- #131: fallbacks to proxy curves ----------------------------------------------------


def _proxy_market(**changes: object) -> MarketSnapshot:
    proxy = DiscountCurve(
        BUSINESS_DATE, node_curve(0.039, 0.0004).nodes, PAR_YIELD_PROXY_MODEL_VERSION
    )
    base = market(
        "CNY",
        curves=(
            CurveEntry("RUB_OIS", "rub_ruonia_ois_legacy_v1", RUB_CURVE),
            CurveEntry("USD_UST", "usd_ust_par_proxy_v1", proxy),
            df_curve("CNY"),
        ),
        fx_spots=(FxSpot("USD", 81.5, TOM, "FX"), FxSpot("CNY", 11.2, TOM, "FX")),
        fx_bases=(FxBasis("USD", "USD_UST", -25.0), FxBasis("CNY", "CNY_DF", 3.0)),
    )
    return replace(base, **changes)  # type: ignore[arg-type]


FALLBACK = ValuationPolicy(
    "RUB_OIS", fx_curve_fallbacks=(("USD_SOFR", "USD_MISSING"), ("USD_SOFR", "USD_UST"))
)


def test_fx_forward_falls_back_to_a_proxy_only_when_its_curve_is_absent() -> None:
    trade = FxForward("F", "USD", 1.0, FxForwardLeg(FAR, 1e6, 84.0))
    inst = FxForwardInstrument("F", trade, "RUB_OIS", "USD_SOFR")
    result = run(_proxy_market(), [inst], [pos("f", "F", 1.0, "USD")], FALLBACK)
    row = result.fx_forwards[0]
    assert (row.foreign_curve_id, row.curve_fallback_from, row.basis_bp) == (
        "USD_UST",
        "USD_SOFR",
        -25.0,
    )
    engine = standalone(trade, built(_proxy_market().curve("USD_UST")), -25.0, 81.5)
    assert row.pv_rub == pytest.approx(engine.base_pv_rub, rel=1e-13)
    assert any(f.startswith("IR|USD_UST|") for f in row.factor_risk.factors)
    assert {c.curve_id: c.proxy for c in result.curves}["USD_UST"] is True
    assert result.position_models[0].flags == ("covered_parity", "curve_fallback")
    without = run(_proxy_market(), [inst], [pos("f", "F", 1.0, "USD")])
    assert without.coverage.rejections[0].code == "MISSING_MARKET_DATA"
    no_basis = run(_proxy_market(fx_bases=()), [inst], [pos("f", "F", 1.0, "USD")], FALLBACK)
    assert no_basis.coverage.rejections[0].code == "MISSING_MARKET_DATA"
    bare = _proxy_market(curves=(CurveEntry("RUB_OIS", "rub_ruonia_ois_legacy_v1", RUB_CURVE),))
    none_left = run(bare, [inst], [pos("f", "F", 1.0, "USD")], FALLBACK)
    message = none_left.coverage.rejections[0].message
    assert "'USD_SOFR', 'USD_MISSING', 'USD_UST'" in message
    primary_proxy = replace(inst, foreign_curve_id="USD_UST")
    assert (
        run(_proxy_market(), [primary_proxy], [pos("f", "F", 1.0, "USD")])
        .coverage.rejections[0]
        .code
        == "INVALID_TERMS"
    )


def test_cross_and_xccy_fall_back_too() -> None:
    from rub_fixed_income import XccyInstrument, standard_xccy_swap, xcs_cnyrub_convention

    trade = FxCrossForward("X", "USD", "CNY", 1.0, FxForwardLeg(FAR, 1e6, 7.1))
    cross = FxCrossInstrument("X", trade, "RUB_OIS", "USD_SOFR", "CNY_DF", "FX")
    row = run(_proxy_market(), [cross], [pos("x", "X", 1.0, "USD")], FALLBACK).fx_crosses[0]
    assert (row.base_curve_id, row.base_curve_fallback_from, row.quote_curve_fallback_from) == (
        "USD_UST",
        "USD_SOFR",
        None,
    )
    swap = standard_xccy_swap(
        "XCS",
        BUSINESS_DATE,
        "1Y",
        xcs_cnyrub_convention(CAL),
        currency="CNY",
        foreign_notional=1e6,
        notional_fx_rate=11.25,
        fixed_rate=0.03,
        foreign_side="receive",
    )
    xccy = XccyInstrument("XCS", swap, "RUB_OIS", "CNY_OIS")
    policy = ValuationPolicy("RUB_OIS", fx_curve_fallbacks=(("CNY_OIS", "CNY_DF"),))
    result = run(
        _proxy_market(calendars=(CalendarEntry("FX", CAL), CalendarEntry("MOEX", CAL))),
        [xccy],
        [pos("s", "XCS", 1.0, "CNY")],
        policy,
    )
    assert result.coverage.status == "complete", result.coverage.rejections
    xrow = result.xccy_swaps[0]
    assert xrow.curve_fallback_from == "CNY_OIS"
    assert dict(xrow.curve_ids)["foreign_discount"] == "CNY_DF"
    with pytest.raises(InvalidTerms, match="own fallback"):
        ValuationPolicy("RUB_OIS", fx_curve_fallbacks=(("A", "A"),))
    with pytest.raises(InvalidTerms, match="duplicate"):
        ValuationPolicy("RUB_OIS", fx_curve_fallbacks=(("A", "B"), ("A", "B")))


# --- #46 points held, #103 basis scenarios ----------------------------------------------


def test_points_held_rows_only_when_the_policy_asks() -> None:
    trade = FxForward("F", "CHF", 1.0, FxForwardLeg(FAR, 1e6, 104.0))
    inst = FxForwardInstrument("F", trade, "RUB_OIS", "CHF_DF")
    plain = run(market("CHF"), [inst], [pos("f", "F", 2.0, "CHF")]).fx_forwards[0]
    assert plain.points_held_rub_buckets == () and "points_held_rub_buckets" not in to_dict(plain)
    held = run(
        market("CHF"),
        [inst],
        [pos("f", "F", 2.0, "CHF")],
        ValuationPolicy("RUB_OIS", fx_points_held_risk=True),
    ).fx_forwards[0]
    engine = standalone(trade, built(df_curve("CHF")), 3.0, SPOTS["CHF"][0])
    expected = engine.points_held_rub_risk().rub_buckets
    assert [b.bucket for b in held.points_held_rub_buckets] == [b.bucket for b in expected]
    for got, want in zip(held.points_held_rub_buckets, expected, strict=True):
        assert got.dv01_rub == pytest.approx(2.0 * want.dv01_rub, rel=1e-13)
    assert held.factor_risk == plain.factor_risk


def test_policy_and_scenario_defaults_keep_their_bytes() -> None:
    assert set(to_dict(ValuationPolicy("RUB_OIS"))) == set(
        to_dict(
            replace(ValuationPolicy("RUB_OIS"), fx_curve_fallbacks=(), fx_points_held_risk=False)
        )
    )
    assert "fx_curve_fallbacks" not in to_dict(ValuationPolicy("RUB_OIS"))
    scenario = PortfolioScenario("s", BondScenario.parallel(1.0))
    assert "basis_shifts_bp" not in to_dict(scenario)


def test_basis_scenarios_move_each_basis_by_its_factor_key() -> None:
    shift = PortfolioScenario(
        "basis",
        BondScenario.parallel(0.0),
        basis_shifts_bp=(
            ("FXBASIS|USD", 10.0),
            ("XCCYBASIS|CNY", -5.0),
            ("FUTBASIS|RUON-10.26", 3.0),
        ),
    )
    result = value_portfolio(
        run_id="b",
        as_of=AS_OF,
        snapshot=book_snapshot(),
        instruments=book_instruments(pinned=True),
        positions=book_positions(pinned=True),
        policy=POLICY,
        scenarios=(shift,),
    )
    assert isinstance(result, PortfolioRiskResultV3)
    pnl = {p.position_id: p.pnl_rub for p in result.scenarios[0].positions}
    rows = {r.position_id: r for r in result.derivative_rows()}
    # FX forward: exact repricing with the basis shift, against its first order.
    fwd = rows["p-fwd"]
    first = fwd.factor_risk.first_order_by_factor()
    assert pnl["p-fwd"] == pytest.approx(10.0 * first["FXBASIS|USD"], rel=1e-3)
    assert pnl["p-si"] == pytest.approx(
        10.0 * rows["p-si"].factor_risk.first_order_by_factor()["FXBASIS|USD"], rel=1e-3
    )
    assert pnl["p-xcs"] == pytest.approx(
        -5.0 * rows["p-xcs"].factor_risk.first_order_by_factor()["XCCYBASIS|CNY"], rel=1e-3
    )
    assert pnl["p-ruon"] == pytest.approx(
        3.0 * rows["p-ruon"].factor_risk.first_order_by_factor()["FUTBASIS|RUON-10.26"], rel=1e-3
    )
    for untouched in ("p-fxswap", "p-ois", "p-gaz", "p-kk", "p-rf", "p-cds-gaz"):
        assert pnl[untouched] == 0.0, untouched
    for bad in ("CS", "FXBASIS|", "IR|X|1Y", "FXBASIS|USD|X"):
        with pytest.raises(InvalidTerms, match="basis shifts"):
            PortfolioScenario("b", BondScenario.parallel(0.0), basis_shifts_bp=((bad, 1.0),))
    with pytest.raises(InvalidTerms, match="duplicate"):
        PortfolioScenario(
            "b",
            BondScenario.parallel(0.0),
            basis_shifts_bp=(("FXBASIS|USD", 1.0), ("FXBASIS|USD", 2.0)),
        )
    # The standalone FX forward engine agrees exactly.
    snap = book_snapshot()
    inst = next(i for i in book_instruments(pinned=True) if i.instrument_id == "FWD-USD")
    assert isinstance(inst, FxForwardInstrument)
    engine = FxForwardRiskEngine(
        FxForwardRiskRequest(
            inst.trade, 81.5, TOM, built(snap.curve("RUB_OIS")), built(snap.curve("USD_DF")), 0.0
        )
    )
    assert pnl["p-fwd"] == pytest.approx(
        engine.reprice(FxFuturesScenario(basis_shift_bp=10.0)).total_pnl_rub, rel=1e-12
    )


def test_generic_curve_specs_name_any_currency() -> None:
    from rub_fixed_income import (
        CURVE_SPECS,
        CurveKind,
        UnknownCurveSpec,
        foreign_discount_spec_id,
        fx_implied_spec_id,
        get_curve_spec,
    )

    chf = get_curve_spec("chf_discount_factors_v1")
    assert (chf.kind, chf.currency, chf.model_version) == (
        CurveKind.DISCOUNT_FACTORS,
        "CHF",
        "ois-logdf-v1",
    )
    kzt = get_curve_spec(fx_implied_spec_id("KZT"))
    assert (kzt.spec_id, kzt.kind, kzt.model_version) == (
        "kzt_fx_implied_v1",
        CurveKind.FX_IMPLIED,
        "fx-implied-logdf-v1",
    )
    assert get_curve_spec("cny_discount_factors_v1") is CURVE_SPECS["cny_discount_factors_v1"]
    assert foreign_discount_spec_id("GBP") == "gbp_discount_factors_v1"
    assert "chf_discount_factors_v1" not in CURVE_SPECS
    for unknown in (
        "rub_fx_implied_v1",
        "chf_discount_factors_v2",
        "chfx_fx_implied_v1",
        "CHF_fx_implied_v1",
    ):
        with pytest.raises(UnknownCurveSpec):
            get_curve_spec(unknown)
    for bad in ("RUB", "chf", "CH"):
        with pytest.raises(InvalidTerms):
            fx_implied_spec_id(bad)


def test_bond_on_a_built_implied_curve_and_quotes_rejected() -> None:
    from rub_fixed_income import (
        BondPriceObservation,
        CashflowKind,
        CouponPeriod,
        FixedCouponLeg,
        FxRate,
        Instrument,
        PrincipalLeg,
        ProductType,
        Redemption,
    )

    bond = Instrument(
        "CNY-BOND",
        ProductType.FIXED_BOND,
        "CNY",
        100.0,
        FixedCouponLeg((CouponPeriod(date(2026, 4, 1), date(2027, 4, 1), date(2027, 4, 1), 2.5),)),
        PrincipalLeg((Redemption(date(2027, 4, 1), 100.0, CashflowKind.MATURITY),)),
        "MOEX",
    )
    built = implied_foreign_curve(
        rub_curve=RUB_CURVE,
        spot_rate=11.2,
        spot_value_date=TOM,
        quotes=(
            FxSwapPointsQuote("TODTOM", BUSINESS_DATE, TOM, 0.004),
            FxSwapPointsQuote("1Y", TOM, date(2027, 10, 18), 0.4),
        ),
    )
    snap = MarketSnapshot(
        "b",
        BUSINESS_DATE,
        AS_OF,
        curves=(
            CurveEntry("RUB_OIS", "rub_ruonia_ois_legacy_v1", RUB_CURVE),
            CurveEntry("CNY_FXI", "cny_fx_implied_v1", built),
        ),
        calendars=(CalendarEntry("MOEX", CAL),),
        fx=(FxRate("CNY", 11.2),),
        quotes=(BondPriceObservation("CNY-BOND", 101.0, "close"),),
    )
    policy = ValuationPolicy("RUB_OIS", foreign_curves=(("CNY", "CNY_FXI"),))
    result = value_portfolio(
        run_id="b",
        as_of=AS_OF,
        snapshot=snap,
        instruments=[bond],
        positions=[pos("b", "CNY-BOND", 10.0, "CNY")],
        policy=policy,
    )
    assert result.coverage.status == "complete", result.coverage.rejections
    assert result.positions[0].curve_id == "CNY_FXI"
    quotes = FxImpliedCurveRequest(
        "CNY", BUSINESS_DATE, "RUB_OIS", (FxSwapPointsQuote("TODTOM", BUSINESS_DATE, TOM, 0.004),)
    )
    rejected = value_portfolio(
        run_id="b",
        as_of=AS_OF,
        snapshot=replace(
            snap,
            curves=(
                CurveEntry("RUB_OIS", "rub_ruonia_ois_legacy_v1", RUB_CURVE),
                CurveEntry("CNY_FXI", "cny_fx_implied_v1", quotes),
            ),
        ),
        instruments=[bond],
        positions=[pos("b", "CNY-BOND", 10.0, "CNY")],
        policy=policy,
    )
    assert rejected.coverage.rejections[0].code == "INVALID_TERMS"
    assert math.isfinite(result.positions[0].pv_rub)
