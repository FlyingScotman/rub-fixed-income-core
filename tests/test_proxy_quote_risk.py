"""USD / EUR par-yield proxies as quotes: curve risk in the par yields (issue #77).

The independent check moves each par yield by the bucket hats at its maturity's ACT/365F
time, rebuilds the curve with ``bootstrap_par_yield_curve`` and reprices on a plain engine.
"""

from __future__ import annotations

import math
from dataclasses import replace
from datetime import date

import pytest
from portfolio_v3_book import AS_OF, POLICY, instruments, positions, snapshot
from test_foreign_bond_curves import (
    CNY_ENTRY,
    ECB_AAA_PAR,
    ECB_ENTRY,
    OWN_CURVES,
    UST_ENTRY,
    UST_PAR,
    foreign_snapshot,
    own_curve_run,
)
from test_fx_portfolio_v036 import FALLBACK, _proxy_market, pos
from test_fx_portfolio_v036 import run as fx_run
from test_non_rub_bonds import BUSINESS_DATE, row
from test_xccy import bootstrap_request

from rub_fixed_income import (
    RISK_BUCKETS,
    BondCashflow,
    BondRiskEngine,
    BondRiskRequest,
    CurveEntry,
    DiscountCurve,
    FxForward,
    FxForwardInstrument,
    FxForwardLeg,
    FxForwardRiskEngine,
    FxForwardRiskRequest,
    ParYieldCurveRequest,
    ParYieldQuote,
    ZSpreadConvention,
    bootstrap_par_yield_curve,
    from_dict,
    to_dict,
    value_portfolio,
)
from rub_fixed_income._risk_common import _shift_at
from rub_fixed_income.conventions import DayCount
from rub_fixed_income.dates import add_months
from rub_fixed_income.domain import MarketSnapshot
from rub_fixed_income.tenors import add_tenor

TOM = date(2026, 10, 16)


def quotes(
    spec_id: str = "usd_ust_par_proxy_v1",
    par: tuple[tuple[str, float], ...] = UST_PAR,
    shift: tuple[float, ...] | None = None,
) -> ParYieldCurveRequest:
    rows = []
    for tenor, value in par:
        bp = 0.0
        if shift is not None:
            time = DayCount.ACT_365_FIXED.year_fraction(
                BUSINESS_DATE, add_tenor(BUSINESS_DATE, tenor)
            )
            bp = _shift_at(time, shift)
        rows.append(ParYieldQuote(tenor, value + bp * 1e-4))
    return ParYieldCurveRequest(spec_id, BUSINESS_DATE, tuple(rows))


def rebuilt(shift: tuple[float, ...], **kw: object) -> DiscountCurve:
    return bootstrap_par_yield_curve(quotes(shift=shift, **kw))  # type: ignore[arg-type]


def bucket(k: int, bp: float) -> tuple[float, ...]:
    return tuple(bp if i == k else 0.0 for i in range(len(RISK_BUCKETS)))


FLOWS = tuple(
    BondCashflow(add_months(date(2026, 11, 15), 6 * k), 2.25 if k < 16 else 102.25)
    for k in range(17)
)


def bond(
    curve: DiscountCurve | ParYieldCurveRequest,
    spread_bp: float | None = None,
    dirty_price_rub: float | None = 99.0,
) -> BondRiskEngine:
    request = BondRiskRequest(
        FLOWS,
        TOM,
        curve,
        ZSpreadConvention(),
        "credit",
        spread_bp=spread_bp,
        dirty_price_rub=dirty_price_rub,
        quantity=1000.0,
    )
    return BondRiskEngine(request)


def test_shocked_quotes_are_the_hats_at_each_maturity() -> None:
    shift = (1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0, 11.0)
    assert quotes().shocked(shift) == quotes(shift=shift)
    request = quotes()
    assert request.shocked((0.0,) * 11) is request


@pytest.mark.parametrize(
    ("spec_id", "par"),
    [("usd_ust_par_proxy_v1", UST_PAR), ("eur_ecb_aaa_par_proxy_v1", ECB_AAA_PAR)],
)
def test_bond_on_par_yields_equals_bump_and_rebuild(
    spec_id: str, par: tuple[tuple[str, float], ...]
) -> None:
    engine = bond(quotes(spec_id, par))
    plain = bond(bootstrap_par_yield_curve(quotes(spec_id, par)))
    assert engine.spread_bp == plain.spread_bp
    assert engine.base_price_rub == plain.base_price_rub
    risk = engine.risk(bump_bp=1.0, difference="central")
    assert risk.curve_basis == "par_yield_quote"
    for k, row_k in enumerate(risk.buckets):
        up, down = (
            bond(
                rebuilt(bucket(k, s), spec_id=spec_id, par=par),
                spread_bp=engine.spread_bp,
                dirty_price_rub=None,
            ).base_price_rub
            for s in (1.0, -1.0)
        )
        assert row_k.dv01_rub == pytest.approx((up - down) / 2.0 * 1000.0, rel=1e-8, abs=1e-8)
    assert math.fsum(b.dv01_rub for b in risk.buckets) < 0.0


def test_fx_forward_on_par_yields_equals_bump_and_rebuild() -> None:
    trade = FxForward("F-Q", "USD", 1.0, FxForwardLeg(date(2028, 1, 17), 1e6, 84.0))
    rub = DiscountCurve.flat(BUSINESS_DATE, 0.15)

    def engine(foreign: DiscountCurve | ParYieldCurveRequest) -> FxForwardRiskEngine:
        return FxForwardRiskEngine(FxForwardRiskRequest(trade, 81.5, TOM, rub, foreign, -25.0))

    quoted = engine(quotes())
    assert quoted.base_pv_rub == engine(bootstrap_par_yield_curve(quotes())).base_pv_rub
    assert quoted.foreign_curve_basis == "par_yield_quote"
    risk = quoted.risk(difference="central")
    for k, row_k in enumerate(risk.foreign_buckets):
        up, down = (engine(rebuilt(bucket(k, s))).base_pv_rub for s in (1.0, -1.0))
        assert row_k.dv01_rub == pytest.approx((up - down) / 2.0, rel=1e-8, abs=1e-6)


def test_bonds_take_quotes_that_build_alone_only() -> None:
    """XCCY quotes are rebuilt on a RUB curve, which a bond does not have."""
    with pytest.raises(TypeError, match="par yields"):
        bond(bootstrap_request())  # type: ignore[arg-type]


# --- portfolio ------------------------------------------------------------------------


def quote_entries() -> tuple[CurveEntry, CurveEntry]:
    return (
        CurveEntry("USD_UST", "usd_ust_par_proxy_v1", quotes()),
        CurveEntry(
            "EUR_AAA", "eur_ecb_aaa_par_proxy_v1", quotes("eur_ecb_aaa_par_proxy_v1", ECB_AAA_PAR)
        ),
    )


def test_snapshot_with_par_yield_quotes_round_trips() -> None:
    snap = foreign_snapshot(CNY_ENTRY, *quote_entries())
    assert isinstance(snap, MarketSnapshot)
    assert from_dict(MarketSnapshot, to_dict(snap)) == snap


def test_bonds_on_par_yield_quotes_without_the_flag_value_as_the_built_curve() -> None:
    built = own_curve_run()
    from_quotes = own_curve_run(snapshot=foreign_snapshot(CNY_ENTRY, *quote_entries()))
    for pid in ("p-usd", "p-eur", "p-cny", "p-rub"):
        a, b = row(built, pid), row(from_quotes, pid)
        assert (a.pv_rub, a.spread_bp, a.factor_risk) == (b.pv_rub, b.spread_bp, b.factor_risk)
        assert a.curve_basis == b.curve_basis


def test_bonds_on_par_yield_quotes_with_the_flag_take_par_yield_risk() -> None:
    zero = own_curve_run(snapshot=foreign_snapshot(CNY_ENTRY, UST_ENTRY, ECB_ENTRY))
    policy = replace(OWN_CURVES, quote_curve_risk=True)
    par = own_curve_run(snapshot=foreign_snapshot(CNY_ENTRY, *quote_entries()), policy=policy)
    assert par.status == "complete"
    for pid, curve_id in (("p-usd", "USD_UST"), ("p-eur", "EUR_AAA")):
        a, b = row(zero, pid), row(par, pid)
        assert b.curve_basis == "par_yield_quote" and a.curve_basis == "continuous_zero"
        assert b.pv_rub == a.pv_rub and b.spread_bp == a.spread_bp
        ir_a = [a.factor_risk.first_order_by_factor()[f"IR|{curve_id}|{k}"] for k in RISK_BUCKETS]
        ir_b = [b.factor_risk.first_order_by_factor()[f"IR|{curve_id}|{k}"] for k in RISK_BUCKETS]
        assert ir_a != ir_b
        # A parallel par shift moves the curve about as much as a parallel zero shift.
        assert math.fsum(ir_b) == pytest.approx(math.fsum(ir_a), rel=0.05)
    # Curves held as built curves keep their risk.
    assert row(par, "p-cny").factor_risk == row(zero, "p-cny").factor_risk


def test_fx_fallback_to_par_yield_quotes_follows_the_flag() -> None:
    entries = tuple(c for c in _proxy_market().curves if c.curve_id != "USD_UST")
    market = replace(_proxy_market(), curves=(*entries, quote_entries()[0]))
    trade = FxForward("F", "USD", 1.0, FxForwardLeg(date(2027, 4, 16), 1e6, 84.0))
    inst = FxForwardInstrument("F", trade, "RUB_OIS", "USD_SOFR")
    positions = [pos("f", "F", 1.0, "USD")]
    off = fx_run(market, [inst], positions, FALLBACK)
    on = fx_run(market, [inst], positions, replace(FALLBACK, quote_curve_risk=True))
    for result in (off, on):
        assert result.coverage.status == "complete", result.coverage
        assert result.fx_forwards[0].curve_fallback_from == "USD_SOFR"
    assert off.fx_forwards[0].pv_rub == on.fx_forwards[0].pv_rub
    off_ir = off.fx_forwards[0].factor_risk.first_order_by_factor()
    on_ir = on.fx_forwards[0].factor_risk.first_order_by_factor()
    assert off_ir["IR|USD_UST|1Y"] != on_ir["IR|USD_UST|1Y"]
    assert off_ir["IR|RUB_OIS|1Y"] == on_ir["IR|RUB_OIS|1Y"]


def test_cds_on_par_yield_quotes_follows_the_flag() -> None:
    base = snapshot()
    proxy = replace(quotes(), curve_date=base.business_date)
    curves = tuple(
        CurveEntry("USD_DF", "usd_ust_par_proxy_v1", proxy) if c.curve_id == "USD_DF" else c
        for c in base.curves
    )
    picked = tuple(p for p in positions() if p.position_id == "p-cds-sber")

    def risk(policy: object) -> dict[str, float]:
        result = value_portfolio(
            run_id="cds-proxy",
            as_of=AS_OF,
            snapshot=replace(base, curves=curves),
            instruments=instruments(),
            positions=picked,
            policy=policy,  # type: ignore[arg-type]
        )
        assert result.coverage.status == "complete", result.coverage
        return {t.factor: t.first_order_rub for t in result.factor_totals}

    off, on = risk(POLICY), risk(replace(POLICY, quote_curve_risk=True))
    assert off["IR|USD_DF|5Y"] != on["IR|USD_DF|5Y"]
    assert off["CS"] == pytest.approx(on["CS"], rel=1e-12)
