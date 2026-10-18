"""Derivatives in ``value_portfolio`` and result contract v3 (BACKLOG #33, #43, #45, #55,
#66, #71, #83, #87).

Every derivative row is checked against its product engine run standalone on the same
inputs (fresh caches, built here from the raw terms): PV, desk factors and Hessian.
Totals are checked against sums of those, the v3 contract round-trips and verifies, and
a bond-only book stays v2.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path
from typing import TypeVar

import pytest
from portfolio_v3_book import (
    AS_OF,
    BUSINESS_DATE,
    CAL,
    FUTURES_PRICES,
    FX_RATE_USD,
    KEY_RATE_CURVE,
    POLICY,
    SPOT,
    TOM,
    instruments,
    node_curve,
    positions,
    ruonia_futures_fixings,
    ruonia_history,
    snapshot,
    xccy_quotes,
)

from rub_fixed_income import (
    RISK_BUCKETS,
    BondScenario,
    CurveEntry,
    FactorRisk,
    PortfolioHorizonResult,
    PortfolioRiskResult,
    PortfolioScenario,
    Position,
    PositionHorizon,
    ShockedCurveCache,
    canonical_sha256,
    horizon_portfolio,
    portfolio_result_from_json,
    portfolio_result_to_json,
    value_portfolio,
    verify_portfolio_result,
)
from rub_fixed_income.cds import (
    CdsCurveRequest,
    CdsRiskEngine,
    CdsRiskRequest,
    CdsScenario,
    cds_factor_risk,
)
from rub_fixed_income.domain import CreditCurveEntry, FxSpot, Instrument, RuoniaFixingsEntry
from rub_fixed_income.factors import fx_futures_factor_risk, fx_translated_factor_risk
from rub_fixed_income.fx_forwards import (
    FxForwardRiskEngine,
    FxForwardRiskRequest,
    fx_forward_factor_risk,
)
from rub_fixed_income.fx_futures import FxFuturesRiskEngine, FxFuturesRiskRequest, FxFuturesScenario
from rub_fixed_income.key_rate import rebase_key_rate_curve
from rub_fixed_income.ofz_futures import (
    DeliverableBond,
    OfzFuturesRiskEngine,
    OfzFuturesRiskRequest,
    OfzFuturesScenario,
    ofz_futures_factor_risk,
)
from rub_fixed_income.portfolio import (
    PORTFOLIO_RESULT_SCHEMA_V3,
    PortfolioRiskResultV3,
    verify_portfolio_result_v3,
)
from rub_fixed_income.portfolio_instruments import (
    CdsInstrument,
    FxForwardInstrument,
    FxFuturesInstrument,
    OfzFuturesInstrument,
    RateFuturesInstrument,
    RepoInstrument,
    SwapInstrument,
    XccyInstrument,
)
from rub_fixed_income.portfolio_products import HorizonRow, scale_factor_risk
from rub_fixed_income.rate_futures import (
    RateFuturesRiskEngine,
    RateFuturesRiskRequest,
    RateFuturesScenario,
    RuoniaIndexValue,
    rate_futures_factor_risk,
)
from rub_fixed_income.repo import (
    RepoCarry,
    RepoRiskEngine,
    RepoRiskRequest,
    RepoScenario,
    repo_carry,
    repo_factor_risk,
)
from rub_fixed_income.swaps import (
    SwapHorizonValuation,
    SwapRiskEngine,
    SwapRiskRequest,
    SwapScenario,
    swap_factor_risk,
    swap_horizon,
)
from rub_fixed_income.xccy import (
    XccyRiskEngine,
    XccyRiskRequest,
    XccyScenario,
    xccy_factor_risk,
    xccy_par_rate,
)

T = TypeVar("T")
FIXTURE = Path(__file__).parent / "fixtures" / "portfolio_result_v3.json"
SCENARIO = PortfolioScenario(
    "rates+50 spread+20 fx+5",
    BondScenario.parallel(50.0, spread_shift_bp=20.0),
    fx_shocks_pct=(("USD", 5.0), ("CNY", -3.0)),
)


def run(**kw: object) -> PortfolioRiskResult:
    args: dict[str, object] = {
        "run_id": "eod-2026-10-15",
        "as_of": AS_OF,
        "snapshot": snapshot(),
        "instruments": instruments(),
        "positions": positions(),
        "policy": POLICY,
        "scenarios": (SCENARIO,),
    }
    args.update(kw)
    return value_portfolio(**args)  # type: ignore[arg-type]


_RESULT: list[PortfolioRiskResultV3] = []


def book() -> PortfolioRiskResultV3:
    """The full book valued once per test session."""
    if not _RESULT:
        result = run()
        assert isinstance(result, PortfolioRiskResultV3)
        _RESULT.append(result)
    return _RESULT[0]


def by_position(result: PortfolioRiskResult) -> dict[str, object]:
    assert isinstance(result, PortfolioRiskResultV3)
    return {r.position_id: r for r in (*result.positions, *result.derivative_rows())}


def instrument(instrument_id: str) -> object:
    return next(i for i in instruments() if i.instrument_id == instrument_id)


def curve(curve_id: str) -> object:
    return snapshot().curve(curve_id).curve


# --- standalone engines on the raw inputs ----------------------------------------------


def standalone(position_id: str) -> tuple[float, FactorRisk, Callable[[], float]]:
    """(PV RUB, factor risk, scenario P&L) of one derivative position, computed directly."""
    pos = next(p for p in positions() if p.position_id == position_id)
    inst = instrument(pos.instrument_id)
    q = pos.quantity
    bump = POLICY.bump_bp
    shift = SCENARIO.shock.curve_shifts_bp
    if isinstance(inst, SwapInstrument):
        projection = None if inst.projection_curve_id is None else curve(inst.projection_curve_id)
        engine = SwapRiskEngine(
            SwapRiskRequest(
                inst.swap,
                CAL,
                curve(inst.discount_curve_id),  # type: ignore[arg-type]
                projection,  # type: ignore[arg-type]
                ruonia_history() if inst.fixing_id else None,
            )
        )
        risk = swap_factor_risk(
            engine.risk(bump_bp=bump),
            discount_curve_id=inst.discount_curve_id,
            projection_curve_id=inst.projection_curve_id,
            second_order=engine.second_order(bump_bp=bump),
        )
        proj = shift if projection is not None else SwapScenario().projection_shifts_bp
        return (
            engine.base_pv * q,
            scale_factor_risk(risk, q),
            lambda: engine.reprice(SwapScenario(shift, proj)).total_pnl_rub * q,
        )
    if isinstance(inst, FxForwardInstrument):
        ccy = inst.currency
        basis = 12.0 if inst.foreign_curve_id == "CNY_DF" else 0.0
        fe = FxForwardRiskEngine(
            FxForwardRiskRequest(
                inst.trade,
                SPOT[ccy] * inst.trade.quote_units,
                TOM,
                curve(inst.rub_curve_id),  # type: ignore[arg-type]
                curve(inst.foreign_curve_id),  # type: ignore[arg-type]
                basis,
            )
        )
        risk = fx_forward_factor_risk(
            fe.risk(bump_bp=bump, difference="central"),
            inst.rub_curve_id,
            inst.foreign_curve_id,
            fe.second_order(bump_bp=bump),
        )
        move = SCENARIO.fx_move_pct(ccy)
        return (
            fe.base_pv_rub * q,
            scale_factor_risk(risk, q),
            lambda: fe.reprice(FxFuturesScenario(shift, shift, 0.0, move)).total_pnl_rub * q,
        )
    if isinstance(inst, XccyInstrument):
        foreign = curve(inst.foreign_discount_curve_id)
        basis = 12.0 if inst.foreign_discount_curve_id == "CNY_DF" else 0.0
        request = XccyRiskRequest(
            inst.swap,
            SPOT["CNY"],
            TOM,
            curve(inst.rub_discount_curve_id),  # type: ignore[arg-type]
            foreign,  # type: ignore[arg-type]
            basis,
        )
        xe = XccyRiskEngine(request)
        ids = {"rub_discount": inst.rub_discount_curve_id}
        ids["foreign_discount"] = inst.foreign_discount_curve_id
        risk = xccy_factor_risk(xe.risk(bump_bp=bump), ids, xe.second_order(bump_bp=bump))
        move = SCENARIO.fx_move_pct("CNY")
        return (
            xe.base_pv_rub * q,
            scale_factor_risk(risk, q),
            lambda: xe.reprice(XccyScenario(shift, shift, 0.0, move)).total_pnl_rub * q,
        )
    if isinstance(inst, CdsInstrument):
        contract = inst.contract
        entry = snapshot().credit_curve(contract.reference_entity, contract.currency)
        ce = CdsRiskEngine(
            CdsRiskRequest(
                contract,
                BUSINESS_DATE,
                curve(inst.discount_curve_id),  # type: ignore[arg-type]
                CdsCurveRequest(BUSINESS_DATE, entry.quotes, entry.recovery_rate, entry.convention),
            )
        )
        fx = 1.0 if contract.currency == "RUB" else FX_RATE_USD
        risk = scale_factor_risk(
            cds_factor_risk(
                ce.risk(bump_bp=bump), inst.discount_curve_id, ce.second_order(bump_bp=bump)
            ),
            q * fx,
        )
        if fx != 1.0:
            risk = fx_translated_factor_risk(risk, contract.currency, ce.base_pv * q * fx / 100.0)
        move = SCENARIO.fx_move_pct(contract.currency) if fx != 1.0 else 0.0

        def cds_pnl() -> float:
            result = ce.reprice(CdsScenario(shift, SCENARIO.shock.spread_shift_bp))
            return (result.shocked_pv * (1.0 + move / 100.0) - result.base_pv) * q * fx

        return ce.base_pv * q * fx, risk, cds_pnl
    if isinstance(inst, FxFuturesInstrument):
        contract_fx = inst.contract
        fu = FxFuturesRiskEngine(
            FxFuturesRiskRequest(
                contract_fx,
                SPOT["USD"] * contract_fx.price_quote_units,
                TOM,
                curve(inst.rub_curve_id),  # type: ignore[arg-type]
                curve(inst.foreign_curve_id),  # type: ignore[arg-type]
                futures_price=FUTURES_PRICES[contract_fx.contract_id][0],
                quantity=q,
            )
        )
        risk = fx_futures_factor_risk(
            fu.risk(bump_bp=bump, difference="central"),
            inst.rub_curve_id,
            inst.foreign_curve_id,
            fu.second_order(bump_bp=bump),
        )
        price, margined = FUTURES_PRICES[contract_fx.contract_id]
        move = SCENARIO.fx_move_pct("USD")
        return (
            q * contract_fx.multiplier * (price - margined),
            risk,
            lambda: fu.reprice(FxFuturesScenario(shift, shift, 0.0, move)).total_pnl_rub,
        )
    if isinstance(inst, RateFuturesInstrument):
        contract_r = inst.contract
        price, _ = FUTURES_PRICES[contract_r.contract_id]
        if inst.fixing_id is None:
            request_r = RateFuturesRiskRequest(contract_r, KEY_RATE_CURVE, futures_price=price)
        else:
            anchor = (
                RuoniaIndexValue(date(2026, 10, 14), 4.5524)
                if contract_r.contract_id == "RF-12.26"
                else None
            )
            request_r = RateFuturesRiskRequest(
                contract_r,
                curve(inst.projection_curve_id),  # type: ignore[arg-type]
                ruonia_futures_fixings(),
                anchor,
                futures_price=price,
            )
        re = RateFuturesRiskEngine(replace(request_r, quantity=q))
        risk = rate_futures_factor_risk(
            re.risk(bump_bp=bump, difference="central"),
            inst.projection_curve_id,
            re.second_order(bump_bp=bump),
        )
        return (
            math.nan,
            risk,
            lambda: re.reprice(RateFuturesScenario(shift)).total_pnl_rub,
        )
    if isinstance(inst, RepoInstrument):
        floating = inst.fixing_id is not None
        engine_r = RepoRiskEngine(
            RepoRiskRequest(
                inst.trade,
                CAL,
                curve(inst.discount_curve_id),  # type: ignore[arg-type]
                curve(inst.projection_curve_id) if inst.projection_curve_id else None,  # type: ignore[arg-type]
                ruonia_history() if floating else None,
                inst.assumed_end_date,
            )
        )
        risk = repo_factor_risk(
            engine_r.risk(bump_bp=bump),
            discount_curve_id=inst.discount_curve_id,
            projection_curve_id=inst.projection_curve_id,
            second_order=engine_r.second_order(bump_bp=bump),
        )
        proj_r = shift if inst.projection_curve_id else RepoScenario().projection_shifts_bp
        return (
            engine_r.valuation().pv_rub * q,
            scale_factor_risk(risk, q),
            lambda: engine_r.reprice(RepoScenario(shift, proj_r)).total_pnl_rub * q,
        )
    assert isinstance(inst, OfzFuturesInstrument)
    basket = tuple(
        DeliverableBond(
            instrument(item.instrument_id),  # type: ignore[arg-type]
            item.conversion_factor,
            snapshot().quote(item.instrument_id).clean_price_pct,
        )
        for item in inst.basket
    )
    oe = OfzFuturesRiskEngine(
        OfzFuturesRiskRequest(
            inst.contract,
            basket,
            TOM,
            curve(inst.bond_curve_id),  # type: ignore[arg-type]
            curve(inst.funding_curve_id),  # type: ignore[arg-type]
            futures_price=FUTURES_PRICES[inst.contract.contract_id][0],
            quantity=q,
        )
    )
    risk = ofz_futures_factor_risk(
        oe.risk(bump_bp=bump, difference="central"),
        inst.bond_curve_id,
        inst.funding_curve_id,
        oe.second_order(bump_bp=bump),
    )
    price, margined = FUTURES_PRICES[inst.contract.contract_id]
    return (
        q * (price - margined),
        risk,
        lambda: (
            oe.reprice(
                OfzFuturesScenario(shift, shift, SCENARIO.shock.spread_shift_bp)
            ).total_pnl_rub
        ),
    )


DERIVATIVES = [p.position_id for p in positions() if p.position_id not in ("p-gaz", "p-ofz-short")]


@pytest.mark.parametrize("position_id", DERIVATIVES)
def test_each_derivative_row_equals_its_engine_run_standalone(position_id: str) -> None:
    pv, risk, pnl = standalone(position_id)
    row = by_position(book())[position_id]
    if not math.isnan(pv):
        assert row.pv_rub == pytest.approx(pv, rel=1e-12, abs=1e-6)  # type: ignore[attr-defined]
    got: FactorRisk = row.factor_risk  # type: ignore[attr-defined]
    assert got.factors == risk.factors
    assert got.first_order == pytest.approx(risk.first_order, rel=1e-12, abs=1e-9)
    assert got.hessian_by_factor() == pytest.approx(risk.hessian_by_factor(), rel=1e-12, abs=1e-9)
    assert got.has_second_order
    scenario_rows = {r.position_id: r for r in book().scenarios[0].positions}
    assert scenario_rows[position_id].pnl_rub == pytest.approx(pnl(), rel=1e-12, abs=1e-6)


def test_book_is_v3_complete_and_covers_every_position() -> None:
    result = book()
    assert result.schema_version == PORTFOLIO_RESULT_SCHEMA_V3
    assert result.status == "complete"
    assert result.coverage.expected == result.coverage.completed == len(positions())
    rows = by_position(result)
    assert set(rows) == {p.position_id for p in positions()}
    assert result.total_pv_rub == pytest.approx(math.fsum(r.pv_rub for r in rows.values()))  # type: ignore[attr-defined]
    assert [m.position_id for m in result.position_models] == [p.position_id for p in positions()]
    assert {m.product for m in result.position_models} == {
        "fixed_bond",
        "irs",
        "fx_forward",
        "fx_swap",
        "xccy_swap",
        "cds",
        "fx_future",
        "rate_future",
        "bond_future",
        "repo",
    }
    assert {c.curve_id for c in result.curves} >= {
        "RUB_OIS",
        "RUB_PROJ",
        "KEY_RATE",
        "USD_DF",
        "CNY_DF",
        "CNY_XCCY",
        "RUB_REPO",
    }
    assert [s.currency for s in result.fx_spots] == ["CNY", "USD"]
    assert [(c.reference_entity, c.currency) for c in result.credit_curves] == [
        ("GAZPROM", "RUB"),
        ("SBER", "USD"),
    ]
    assert [r.currency for r in result.fx_rates] == ["USD"]


def test_factor_totals_and_hessian_are_the_sums_over_every_position() -> None:
    result = book()
    rows = by_position(result)
    risks: list[FactorRisk] = [r.factor_risk for r in rows.values()]  # type: ignore[attr-defined]
    expected: dict[str, list[float]] = {}
    hessian: dict[tuple[str, str], list[float]] = {}
    for risk in risks:
        for factor, value in risk.first_order_by_factor().items():
            expected.setdefault(factor, []).append(value)
        for (a, b), value in risk.hessian_by_factor().items():
            hessian.setdefault((min(a, b), max(a, b)), []).append(value)
    assert {t.factor: t.first_order_rub for t in result.factor_totals} == {
        k: math.fsum(v) for k, v in expected.items()
    }
    assert {(h.factor_i, h.factor_j): h.value_rub for h in result.hessian_totals} == {
        k: math.fsum(v) for k, v in hessian.items()
    }
    factors = {t.factor for t in result.factor_totals}
    for prefix in ("IR|", "CS", "FX|", "FXBASIS|", "XCCYBASIS|", "FUTBASIS|"):
        assert any(f.startswith(prefix) for f in factors), prefix
    # Standalone engines: the totals of derivative factors match the sum of their risks.
    standalone_sum: dict[str, float] = {}
    for pid in DERIVATIVES:
        for factor, value in standalone(pid)[1].first_order_by_factor().items():
            standalone_sum[factor] = standalone_sum.get(factor, 0.0) + value
    for pid in ("p-gaz", "p-ofz-short"):
        for factor, value in rows[pid].factor_risk.first_order_by_factor().items():  # type: ignore[attr-defined]
            standalone_sum[factor] = standalone_sum.get(factor, 0.0) + value
    totals = {t.factor: t.first_order_rub for t in result.factor_totals}
    for factor, value in standalone_sum.items():
        assert totals[factor] == pytest.approx(value, rel=1e-9, abs=1e-6), factor


def test_cross_product_hessian_terms_are_present() -> None:
    entries = {(h.factor_i, h.factor_j) for h in book().hessian_totals}
    assert ("FX|USD", "IR|USD_DF|5Y") in entries  # FX forward, CDS: FX x rates
    assert ("CS", "IR|RUB_OIS|5Y") in entries  # bonds and CDS share CS
    assert ("FUTBASIS|OFZ6-12.26", "IR|RUB_REPO|1M") in entries


def test_jtd_totals_add_bond_and_cds_jtd_by_entity_with_one_sign_rule() -> None:
    result = book()
    rows = by_position(result)
    bond = rows["p-gaz"].jtd_pnl_rub  # type: ignore[attr-defined]
    cds_buy = rows["p-cds-gaz"].jtd_pnl_rub  # type: ignore[attr-defined]
    cds_sell = rows["p-cds-sber"].jtd_pnl_rub  # type: ignore[attr-defined]
    assert bond < 0.0 < cds_buy and cds_sell < 0.0  # long credit loses, protection gains
    totals = {t.entity: t for t in result.jtd_totals}
    assert totals["GAZPROM"].positions == 2
    assert totals["GAZPROM"].jtd_pnl_rub == pytest.approx(bond + cds_buy)
    assert totals["SBER"].jtd_pnl_rub == cds_sell
    assert result.jtd_total_rub == pytest.approx(bond + cds_buy + cds_sell)
    # The SBER position states its own realised recovery; GAZPROM's CDS uses the curve's.
    assert rows["p-cds-sber"].jtd_recovery_rate == 0.25  # type: ignore[attr-defined]
    assert rows["p-cds-gaz"].jtd_recovery_rate == 0.4  # type: ignore[attr-defined]


def test_product_rows_carry_their_engine_fields() -> None:
    rows = by_position(book())
    swap = rows["p-ois"]
    assert swap.fixing_snapshot_id == "cbr-ruonia"  # type: ignore[attr-defined]
    assert swap.clean_pv_rub == pytest.approx(swap.pv_rub - swap.accrued_rub)  # type: ignore[attr-defined]
    assert 0.0 < swap.par_rate < 0.3  # type: ignore[attr-defined]
    krs = rows["p-krs"]
    assert krs.projection_curve_basis == "key_rate_forward"  # type: ignore[attr-defined]
    cds = rows["p-cds-gaz"]
    assert cds.points_upfront != 0.0 and cds.par_spread > 0.0  # type: ignore[attr-defined]
    assert cds.recovery01_rub != 0.0 and cds.accrued_premium_rub < 0.0  # type: ignore[attr-defined]
    si = rows["p-si"]
    assert si.variation_margin_rub == si.pv_rub == 10 * 1.0 * (83_300.0 - 83_150.0)  # type: ignore[attr-defined]
    xcs = rows["p-xcs"]
    request = XccyRiskRequest(
        instrument("XCS-2Y").swap,  # type: ignore[attr-defined]
        SPOT["CNY"],
        TOM,
        curve("RUB_OIS"),  # type: ignore[arg-type]
        xccy_quotes(),
        0.0,
    )
    assert xcs.foreign_leg_par_rate == pytest.approx(xccy_par_rate(request, leg="foreign"))  # type: ignore[attr-defined]
    assert xcs.curve_ids == (("rub_discount", "RUB_OIS"), ("foreign_discount", "CNY_XCCY"))  # type: ignore[attr-defined]
    ofz1, ofz2 = rows["p-ofz-fut-1"], rows["p-ofz-fut-2"]
    assert ofz1.cheapest_to_deliver == ofz2.cheapest_to_deliver  # type: ignore[attr-defined]
    assert ofz1.basis_bp == ofz2.basis_bp  # type: ignore[attr-defined]
    ruon = rows["p-ruon"]
    assert ruon.kind == "ruonia_average" and ruon.fixed_days > 0  # type: ignore[attr-defined]
    rf = rows["p-rf"]
    assert rf.index_anchor_date == date(2026, 10, 14)  # type: ignore[attr-defined]
    fwd = rows["p-fxswap"]
    assert fwd.kind == "swap" and len(fwd.legs) == 2  # type: ignore[attr-defined]
    assert fwd.legs[0].notional_foreign == 20_000_000.0  # type: ignore[attr-defined]


def test_quantity_scales_every_derivative_linearly() -> None:
    double = tuple(replace(p, quantity=2 * p.quantity) for p in positions())
    result = run(positions=double, scenarios=())
    base = by_position(book())
    for pid, row in by_position(result).items():
        if pid in ("p-gaz", "p-ofz-short"):
            continue
        assert row.pv_rub == pytest.approx(2 * base[pid].pv_rub, rel=1e-9, abs=1e-6)  # type: ignore[attr-defined]
        assert row.factor_risk.first_order == pytest.approx(  # type: ignore[attr-defined]
            tuple(2 * v for v in base[pid].factor_risk.first_order),  # type: ignore[attr-defined]
            rel=1e-9,
            abs=1e-6,
        )


# --- scenarios -------------------------------------------------------------------------


def test_per_curve_shifts_move_only_the_named_curves() -> None:
    up = tuple(100.0 for _ in RISK_BUCKETS)
    only_usd = PortfolioScenario(
        "usd+100", BondScenario.parallel(0.0), curve_shifts_bp=(("USD_DF", up),)
    )
    result = run(scenarios=(only_usd,))
    pnl = {r.position_id: r.pnl_rub for r in result.scenarios[0].positions}
    usd_readers = {"p-fwd", "p-cds-sber", "p-si"}
    for pid, value in pnl.items():
        if pid in usd_readers:
            assert value != 0.0, pid
        else:
            assert value == 0.0, pid
    # The same vector as the default shock reproduces the parallel scenario.
    named_all = PortfolioScenario(
        "all",
        BondScenario.parallel(0.0),
        curve_shifts_bp=tuple(
            (c, up)
            for c in ("RUB_OIS", "RUB_REPO", "RUB_PROJ", "KEY_RATE", "USD_DF", "CNY_DF", "CNY_XCCY")
        ),
    )
    parallel = PortfolioScenario("parallel", BondScenario.parallel(100.0))
    both = run(scenarios=(named_all, parallel))
    a, b = (s.positions for s in both.scenarios)
    for left, right in zip(a, b, strict=True):
        assert left.pnl_rub == pytest.approx(right.pnl_rub, rel=1e-12, abs=1e-9)


def test_per_curve_shifts_leave_bond_only_bytes_alone() -> None:
    scenario = PortfolioScenario("s", BondScenario.parallel(10.0))
    assert canonical_sha256(scenario) == canonical_sha256(
        PortfolioScenario("s", BondScenario.parallel(10.0), curve_shifts_bp=())
    )
    with pytest.raises(ValueError, match="duplicate"):
        PortfolioScenario(
            "s",
            BondScenario.parallel(1.0),
            curve_shifts_bp=(("A", (0.0,) * 11), ("A", (1.0,) * 11)),
        )
    with pytest.raises(ValueError):
        PortfolioScenario("s", BondScenario.parallel(1.0), curve_shifts_bp=(("A", (0.0,)),))


# --- coverage --------------------------------------------------------------------------


def _codes(**kw: object) -> dict[str, str]:
    result = run(scenarios=(), **kw)
    return {r.position_id: r.code for r in result.coverage.rejections}


def _without(entries: tuple[T, ...], key: Callable[[T], bool]) -> tuple[T, ...]:
    return tuple(e for e in entries if not key(e))


def test_rejection_matrix_has_stable_codes() -> None:
    base = snapshot()
    cases: list[tuple[dict[str, object], str, str]] = [
        ({"snapshot": replace(base, fx_spots=())}, "p-fwd", "MISSING_MARKET_DATA"),
        ({"snapshot": replace(base, fx_bases=())}, "p-xcs", "MISSING_MARKET_DATA"),
        ({"snapshot": replace(base, credit_curves=())}, "p-cds-gaz", "MISSING_MARKET_DATA"),
        ({"snapshot": replace(base, futures_quotes=())}, "p-si", "MISSING_MARKET_DATA"),
        ({"snapshot": replace(base, ruonia_index=())}, "p-rf", "MISSING_MARKET_DATA"),
        (
            {"snapshot": replace(base, ruonia_fixings=())},
            "p-ruon",
            "MISSING_MARKET_DATA",
        ),
        (
            {
                "snapshot": replace(
                    base,
                    ruonia_fixings=(
                        RuoniaFixingsEntry(
                            "RUONIA_FUT", ruonia_futures_fixings(drop=date(2026, 10, 1))
                        ),
                    ),
                )
            },
            "p-ruon",
            "UNDECLARED_FIXING_GAP",
        ),
        (
            {
                "snapshot": replace(
                    base,
                    quotes=_without(base.quotes, lambda q: q.instrument_id == "SU_LONG"),
                )
            },
            "p-ofz-fut-1",
            "MISSING_BASKET_PRICE",
        ),
        (
            {
                "snapshot": replace(
                    base,
                    curves=(
                        *_without(base.curves, lambda c: c.curve_id == "KEY_RATE"),
                        CurveEntry(
                            "KEY_RATE",
                            "rub_key_rate_swap_v1",
                            replace(KEY_RATE_CURVE, curve_date=TOM),
                        ),
                    ),
                )
            },
            "p-krs",
            "INVALID_TERMS",
        ),
        (
            {
                "snapshot": replace(
                    base,
                    curves=(
                        *_without(base.curves, lambda c: c.curve_id == "CNY_XCCY"),
                        CurveEntry(
                            "CNY_XCCY", "cny_ruonia_xccy_v1", replace(xccy_quotes(), spot_rate=11.0)
                        ),
                    ),
                )
            },
            "p-xcs",
            "INVALID_TERMS",
        ),
        (
            {"snapshot": replace(base, fx_spots=(FxSpot("USD", 81.5, TOM, "NO_CAL"),))},
            "p-si",
            "MISSING_MARKET_DATA",
        ),
    ]
    for changes, position_id, code in cases:
        only = tuple(p for p in positions() if p.position_id == position_id)
        assert _codes(positions=only, **changes) == {position_id: code}, (position_id, code)


def test_settled_and_expired_trades_are_rejected_not_zeroed() -> None:
    later = replace(
        snapshot(),
        business_date=date(2026, 12, 21),
        curves=(
            CurveEntry(
                "RUB_OIS", "rub_ruonia_ois_legacy_v1", node_curve(0.15, -0.004, date(2026, 12, 21))
            ),
            CurveEntry(
                "USD_DF", "usd_discount_factors_v1", node_curve(0.042, 0.0008, date(2026, 12, 21))
            ),
        ),
        fixings=(),
        fx_spots=(
            FxSpot("USD", 81.5, date(2026, 12, 22), "FX"),
            FxSpot("CNY", 11.2, date(2026, 12, 22), "FX"),
        ),
        ruonia_fixings=(),
        ruonia_index=(),
    )
    keep = {"p-fxswap", "p-si", "p-ruon", "p-kk", "p-ofz-fut-1", "p-repo-term"}
    codes = _codes(
        snapshot=later,
        positions=tuple(p for p in positions() if p.position_id in keep),
    )
    assert codes == {
        "p-fxswap": "SETTLED_LEG",
        "p-si": "EXPIRED_CONTRACT",
        "p-ruon": "EXPIRED_CONTRACT",
        "p-kk": "EXPIRED_CONTRACT",
        "p-ofz-fut-1": "EXPIRED_CONTRACT",
        "p-repo-term": "SETTLED_LEG",
    }


def test_position_and_instrument_mismatches() -> None:
    wrong_ccy = tuple(
        replace(p, currency="RUB") if p.position_id == "p-fwd" else p for p in positions()
    )
    assert _codes(positions=wrong_ccy[3:4])["p-fwd"] == "INVALID_TERMS"
    # An unknown position type (a product added later, e.g. REPO) is unsupported, never zero.
    odd = (*instruments(), _Alien("WEIRD"))
    category = positions()[0].accounting_category
    with_odd = (Position("p-weird", "WEIRD", 1.0, "D", category, "RUB"),)
    codes = _codes(instruments=odd, positions=with_odd)
    assert codes["p-weird"] == "UNSUPPORTED_PRODUCT"
    # A wrong-currency foreign curve and a real curve are invalid terms.
    swapped = tuple(
        replace(i, foreign_curve_id="CNY_DF") if isinstance(i, FxForwardInstrument) else i
        for i in instruments()
    )
    assert _codes(instruments=swapped, positions=positions()[3:4]) == {"p-fwd": "INVALID_TERMS"}


@dataclass(frozen=True)
class _Alien:
    """A position type value_portfolio has no adapter for."""

    instrument_id: str


HORIZON = date(2026, 11, 16)
CARRIED = (
    *("p-ois", "p-ois-2c", "p-krs", "p-repo-term", "p-repo-open", "p-cds-gaz", "p-cds-sber"),
    *("p-ruon", "p-rf", "p-kk", "p-ofz-fut-1", "p-ofz-fut-2"),
)


def horizon_book(**kw: object) -> PortfolioHorizonResult:
    args: dict[str, object] = {
        "snapshot": snapshot(),
        "instruments": instruments(),
        "positions": positions(),
        "policy": POLICY,
        "horizon_date": HORIZON,
    }
    args.update(kw)
    return horizon_portfolio(**args)  # type: ignore[arg-type]


def standalone_carry(position_id: str, horizon: date) -> SwapHorizonValuation | RepoCarry:
    """The engine's carry of one swap or repo trade on the raw inputs (per trade)."""
    pos = next(p for p in positions() if p.position_id == position_id)
    inst = instrument(pos.instrument_id)
    if isinstance(inst, SwapInstrument):
        projection = None if inst.projection_curve_id is None else curve(inst.projection_curve_id)
        request = SwapRiskRequest(
            inst.swap,
            CAL,
            curve(inst.discount_curve_id),  # type: ignore[arg-type]
            projection,  # type: ignore[arg-type]
            ruonia_history() if inst.fixing_id else None,
        )
        return swap_horizon(request, horizon_date=horizon)
    assert isinstance(inst, RepoInstrument)
    floating = inst.fixing_id is not None
    collateral = inst.trade.collateral
    price = None
    if collateral is not None:  # the portfolio passes the collateral's dirty price
        bond = instrument(collateral.instrument_id)
        assert isinstance(bond, Instrument)
        price = 104.0 / 100.0 * 1000.0 + bond.accrued(TOM)
    repo_request = RepoRiskRequest(
        inst.trade,
        CAL,
        curve(inst.discount_curve_id),  # type: ignore[arg-type]
        curve(inst.projection_curve_id) if inst.projection_curve_id else None,  # type: ignore[arg-type]
        ruonia_history() if floating else None,
        inst.assumed_end_date,
        price,
    )
    return repo_carry(repo_request, horizon_date=horizon)


def test_horizon_portfolio_carries_swaps_and_repos() -> None:
    result = horizon_book()
    codes = {r.position_id: r.code for r in result.coverage.rejections}
    assert set(codes) == set(DERIVATIVES) - set(CARRIED)
    assert set(codes.values()) == {"UNSUPPORTED_PRODUCT"}
    quantities = {p.position_id: p.quantity for p in positions()}
    assert [r.position_id for r in result.swaps] == ["p-ois", "p-ois-2c", "p-krs"]
    for row in result.swaps:
        q = quantities[row.position_id]
        direct = standalone_carry(row.position_id, HORIZON)
        assert isinstance(direct, SwapHorizonValuation)
        assert row.per_trade == direct
        assert row.quantity == q
        assert row.carry_rub == pytest.approx(direct.carry_rub * q, rel=1e-15)
        assert row.rolldown_rub == pytest.approx(direct.rolldown_rub * q, rel=1e-15)
        assert row.received_rub == pytest.approx(direct.received_rub * q, rel=1e-15)
        assert row.horizon_pnl_rub == pytest.approx(row.carry_rub + row.rolldown_rub, abs=1e-6)
    assert result.swaps[2].discount_curve_id == "RUB_OIS"
    assert result.swaps[2].projection_curve_id == "KEY_RATE"
    assert [r.position_id for r in result.repos] == ["p-repo-term", "p-repo-open"]
    for repo_row in result.repos:
        q = quantities[repo_row.position_id]
        direct_r = standalone_carry(repo_row.position_id, HORIZON)
        assert isinstance(direct_r, RepoCarry)
        assert repo_row.per_trade == direct_r
        assert repo_row.carry_rub == pytest.approx(direct_r.pv_carry_rub * q, rel=1e-15)
        assert repo_row.rolldown_rub == pytest.approx(direct_r.rolldown_rub * q, rel=1e-15)
        assert repo_row.horizon_pnl_rub == pytest.approx(
            repo_row.carry_rub + repo_row.rolldown_rub, abs=1e-6
        )
        assert repo_row.interest_carry_rub == pytest.approx(
            direct_r.interest_carry_rub * q, rel=1e-15
        )
    # The term repo ends before the horizon: its repurchase amount is received.
    assert result.repos[0].per_trade.pv_forward_rub == 0.0
    assert result.repos[0].received_rub > 0.0
    # Bond rows are those of a bond-only book; totals add every carried row.
    bonds_only = horizon_book(
        positions=tuple(p for p in positions() if p.position_id in ("p-gaz", "p-ofz-short"))
    )
    assert result.positions == bonds_only.positions
    rows: list[PositionHorizon | HorizonRow] = [*result.positions, *result.derivative_rows()]
    assert result.covered_carry_rub == pytest.approx(math.fsum(r.carry_rub for r in rows))
    assert result.covered_rolldown_rub == pytest.approx(math.fsum(r.rolldown_rub for r in rows))
    assert result.covered_horizon_pnl_rub == pytest.approx(
        math.fsum(r.horizon_pnl_rub for r in rows)
    )
    assert result.covered_horizon_pnl_rub == pytest.approx(
        result.covered_carry_rub + result.covered_rolldown_rub, abs=1e-6
    )
    assert result.total_horizon_pnl_rub is None


def test_horizon_portfolio_of_bonds_swaps_and_repos_is_complete() -> None:
    carried = tuple(p for p in positions() if p.position_id in ("p-gaz", "p-ofz-short", *CARRIED))
    result = horizon_book(positions=carried)
    assert result.coverage.status == "complete"
    assert result.total_horizon_pnl_rub == result.covered_horizon_pnl_rub
    assert [c.calendar_id for c in result.calendars] == ["MOEX"]
    assert horizon_book(positions=carried, curve_cache=ShockedCurveCache()) == result


def test_horizon_portfolio_derivative_rejections_carry_codes() -> None:
    no_end = tuple(
        replace(i, assumed_end_date=None)
        if isinstance(i, RepoInstrument) and i.trade.is_open
        else i
        for i in instruments()
    )
    only = tuple(p for p in positions() if p.position_id in ("p-repo-open", "p-krs"))
    result = horizon_book(instruments=no_end, positions=only)
    codes = {r.position_id: r.code for r in result.coverage.rejections}
    assert codes == {"p-repo-open": "MISSING_ASSUMPTION"}
    # The key-rate projection curve ends on 2031-01-01.
    late = horizon_book(positions=only, horizon_date=date(2031, 2, 3))
    late_codes = {r.position_id: r.code for r in late.coverage.rejections}
    assert late_codes == {"p-krs": "MISSING_MARKET_DATA"}
    assert [r.position_id for r in late.repos] == ["p-repo-open"]
    assert late.repos[0].per_trade.pv_forward_rub == 0.0
    wrong_ccy = tuple(replace(p, currency="USD") for p in only[:1])
    assert {r.code for r in horizon_book(positions=wrong_ccy).coverage.rejections} == {
        "INVALID_TERMS"
    }


def test_key_rate_curve_rebased_from_tom_values_the_swap() -> None:
    tom_curve = replace(KEY_RATE_CURVE, curve_date=TOM)
    rebased = rebase_key_rate_curve(tom_curve, BUSINESS_DATE, rate_in_force=0.165)
    assert rebased == KEY_RATE_CURVE
    with pytest.raises(ValueError):
        rebase_key_rate_curve(tom_curve, BUSINESS_DATE, rate_in_force=0.16)
    with pytest.raises(ValueError):
        rebase_key_rate_curve(KEY_RATE_CURVE, TOM, rate_in_force=0.165)


# --- contract --------------------------------------------------------------------------


def test_bond_only_book_stays_v2() -> None:
    bonds = tuple(i for i in instruments() if isinstance(i, Instrument))
    result = run(
        instruments=bonds,
        positions=tuple(p for p in positions() if p.position_id in ("p-gaz", "p-ofz-short")),
    )
    assert type(result) is PortfolioRiskResult
    assert result.schema_version == "rub-portfolio-risk-result.v2"
    assert verify_portfolio_result(result)
    raw = json.loads(portfolio_result_to_json(result))
    assert "position_models" not in raw and "swaps" not in raw
    # Derivative instruments that no position references do not switch the contract.
    assert run(positions=result_positions_bonds()).schema_version.endswith(".v2")


def result_positions_bonds() -> tuple[Position, ...]:
    return tuple(p for p in positions() if p.position_id in ("p-gaz", "p-ofz-short"))


def test_v3_round_trip_hash_and_verification() -> None:
    result = book()
    data = portfolio_result_to_json(result)
    decoded = portfolio_result_from_json(data)
    assert isinstance(decoded, PortfolioRiskResultV3)
    assert decoded == result
    assert portfolio_result_to_json(decoded) == data
    raw = json.loads(data)
    assert verify_portfolio_result_v3(raw) and not verify_portfolio_result(raw)
    assert raw["schema_version"] == PORTFOLIO_RESULT_SCHEMA_V3
    tampered = dict(raw, jtd_total_rub=raw["jtd_total_rub"] + 1.0)
    assert not verify_portfolio_result_v3(tampered)
    with pytest.raises(ValueError, match="verify"):
        portfolio_result_from_json(json.dumps(tampered))
    assert run().result_sha256 == result.result_sha256  # deterministic
    assert run(curve_cache=ShockedCurveCache()).result_sha256 == result.result_sha256


def test_frozen_v3_fixture_parses_and_reserialises_byte_for_byte() -> None:
    data = FIXTURE.read_bytes()
    decoded = portfolio_result_from_json(data)
    assert isinstance(decoded, PortfolioRiskResultV3)
    assert portfolio_result_to_json(decoded) == data
    # The v2 fixture still decodes as v2.
    v2 = (Path(__file__).parent / "fixtures" / "portfolio_result_v2.json").read_bytes()
    assert type(portfolio_result_from_json(v2)) is PortfolioRiskResult


def test_snapshot_with_derivative_inputs_round_trips_and_old_snapshots_keep_bytes() -> None:
    from rub_fixed_income import from_dict, to_dict
    from rub_fixed_income.domain import MarketSnapshot

    full = snapshot()
    assert from_dict(MarketSnapshot, to_dict(full)) == full
    bare = replace(
        full,
        fx_spots=(),
        fx_bases=(),
        credit_curves=(),
        futures_quotes=(),
        ruonia_fixings=(),
        ruonia_index=(),
        curves=tuple(c for c in full.curves if c.curve_id not in ("KEY_RATE", "CNY_XCCY")),
    )
    raw = to_dict(bare)
    for name in ("fx_spots", "fx_bases", "credit_curves", "futures_quotes", "ruonia_fixings"):
        assert name not in raw
    with pytest.raises(ValueError, match="duplicate"):
        replace(full, fx_spots=(FxSpot("USD", 1.0, TOM, "FX"), FxSpot("USD", 2.0, TOM, "FX")))
    with pytest.raises(ValueError):
        CreditCurveEntry("X", "RUB", (), 0.4, full.credit_curves[0].convention)


def test_repo_legs_value_cash_only_and_open_repos_need_an_assumed_end() -> None:
    rows = by_position(book())
    term = rows["p-repo-term"]
    collateral = term.collateral  # type: ignore[attr-defined]
    assert collateral is not None and collateral.instrument_id == "SU_LONG"
    bond = instrument("SU_LONG")
    dirty = 104.0 / 100.0 * 1000.0 + bond.accrued(TOM)  # type: ignore[attr-defined]
    assert collateral.market_value_rub == pytest.approx(50_000.0 * dirty)
    # The collateral price enters no PV: the standalone engine without it agrees.
    assert term.pv_rub == pytest.approx(standalone("p-repo-term")[0], rel=1e-12)  # type: ignore[attr-defined]
    assert term.pv_rub > 0.0 and rows["p-repo-open"].pv_rub < 0.0  # type: ignore[attr-defined]
    assert rows["p-repo-open"].end_date_assumed  # type: ignore[attr-defined]
    models = {m.position_id: m.flags for m in book().position_models}
    assert (
        "assumed_end" in models["p-repo-open"] and "collateral_reference" in models["p-repo-term"]
    )
    no_end = tuple(
        replace(i, assumed_end_date=None)
        if isinstance(i, RepoInstrument) and i.trade.is_open
        else i
        for i in instruments()
    )
    only = tuple(p for p in positions() if p.position_id == "p-repo-open")
    assert _codes(instruments=no_end, positions=only) == {"p-repo-open": "MISSING_ASSUMPTION"}
    with pytest.raises(ValueError, match="term repo"):
        replace(instrument("REPO-TERM"), assumed_end_date=date(2026, 12, 1))  # type: ignore[type-var]
