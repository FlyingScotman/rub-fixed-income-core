"""FX products on XCCY par quotes: foreign curve risk in the quotes (issue #77).

The independent check rebuilds the CNY curve with ``bootstrap_xccy_curve``: on quotes
moved by the bucket hats at their end dates (foreign buckets), or on the shocked RUB
curve with the quotes held (RUB buckets), and reprices on a plain engine.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date

import pytest
from portfolio_v3_book import (
    AS_OF,
    BUSINESS_DATE,
    POLICY,
    SPOT,
    TOM,
    XCCY_CONVENTION,
    instruments,
    node_curve,
    positions,
    snapshot,
    xccy_quotes,
)

from rub_fixed_income import (
    DiscountCurve,
    FxCrossForward,
    FxCrossRiskEngine,
    FxCrossRiskRequest,
    FxForward,
    FxForwardInstrument,
    FxForwardLeg,
    FxForwardRiskEngine,
    FxForwardRiskRequest,
    FxFuturesContract,
    FxFuturesInstrument,
    FxFuturesRiskEngine,
    FxFuturesRiskRequest,
    FxFuturesScenario,
    FxSwap,
    PortfolioRiskResultV3,
    Position,
    ShockedCurveCache,
    canonical_sha256,
    value_portfolio,
)
from rub_fixed_income._risk_common import RISK_BUCKETS, _shift_at, _shock_curve
from rub_fixed_income.domain import AccountingCategory, FuturesQuote
from rub_fixed_income.portfolio_instruments import PortfolioInstrument
from rub_fixed_income.xccy import (
    XccyCurveBootstrapRequest,
    XccyParQuote,
    bootstrap_xccy_curve,
    standard_xccy_swap,
)

RUB = node_curve(0.15, -0.004)
SWAP = FxSwap(
    "SWAP-CNY-Q",
    "CNY",
    1.0,
    FxForwardLeg(date(2026, 12, 16), -10_000_000.0, 11.3),
    FxForwardLeg(date(2028, 4, 17), 10_000_000.0, 12.4),
)
FORWARD = FxForward("FWD-CNY-Q", "CNY", 1.0, FxForwardLeg(date(2027, 10, 18), 20_000_000.0, 11.8))


def bucket(k: int, bp: float) -> tuple[float, ...]:
    return tuple(bp if i == k else 0.0 for i in range(len(RISK_BUCKETS)))


def moved_quotes(shift: tuple[float, ...]) -> XccyCurveBootstrapRequest:
    """The quotes moved by the bucket hats at each quote swap's end (curve ACT/365F time)."""
    request = xccy_quotes()
    moved = []
    for quote in request.quotes:
        swap = standard_xccy_swap(
            "probe",
            BUSINESS_DATE,
            quote.tenor,
            XCCY_CONVENTION,
            currency="CNY",
            foreign_notional=1.0,
            notional_fx_rate=request.notional_fx_rate,
            fixed_rate=1.0,
            foreign_side="receive",
        )
        end = swap.foreign_leg.periods[-1].accrual_end
        time = request.curve_convention.time_day_count.year_fraction(BUSINESS_DATE, end)
        moved.append(XccyParQuote(quote.tenor, quote.rate + _shift_at(time, shift) * 1e-4))
    return replace(request, quotes=tuple(moved))


def forward_request(
    trade: FxForward | FxSwap, foreign: DiscountCurve | XccyCurveBootstrapRequest
) -> FxForwardRiskRequest:
    return FxForwardRiskRequest(trade, SPOT["CNY"], TOM, RUB, foreign, 0.0)


@pytest.mark.parametrize("trade", [SWAP, FORWARD], ids=["fx_swap", "forward"])
def test_fx_forward_on_xccy_quotes_equals_bump_and_rebuild(trade: FxForward | FxSwap) -> None:
    engine = FxForwardRiskEngine(forward_request(trade, xccy_quotes()))
    built = bootstrap_xccy_curve(xccy_quotes(), rub_discount_curve=RUB)
    plain = FxForwardRiskEngine(forward_request(trade, built))
    assert engine.base_pv_rub == plain.base_pv_rub
    assert engine.foreign_curve_basis == "xccy_quote"
    assert engine.base_id != plain.base_id
    risk = engine.risk(bump_bp=1.0, difference="central")
    for k, row in enumerate(risk.foreign_buckets):
        up, down = (
            FxForwardRiskEngine(
                forward_request(
                    trade,
                    bootstrap_xccy_curve(moved_quotes(bucket(k, s)), rub_discount_curve=RUB),
                )
            ).base_pv_rub
            for s in (1.0, -1.0)
        )
        assert row.dv01_rub == pytest.approx((up - down) / 2.0, rel=1e-9, abs=1e-6)
    assert sum(abs(r.dv01_rub) for r in risk.foreign_buckets) > 0.0
    cache = ShockedCurveCache()
    for k, row in enumerate(risk.rub_buckets):
        pvs = []
        for s in (1.0, -1.0):
            shift = bucket(k, s)
            view = _shock_curve(RUB, RUB, shift, cache)
            rebuilt = bootstrap_xccy_curve(xccy_quotes(), rub_discount_curve=view)
            shocked = FxForwardRiskEngine(forward_request(trade, rebuilt)).reprice(
                FxFuturesScenario(rub_curve_shifts_bp=shift)
            )
            pvs.append(shocked.shocked_pv_rub)
        assert row.dv01_rub == pytest.approx((pvs[0] - pvs[1]) / 2.0, rel=1e-9, abs=1e-6)


def test_fx_forward_hessian_on_xccy_quotes_is_symmetric_with_the_risk_gradient() -> None:
    engine = FxForwardRiskEngine(forward_request(FORWARD, xccy_quotes()))
    second = engine.second_order()
    hessian = second.hessian_rub
    assert all(hessian[i][j] == hessian[j][i] for i in range(len(hessian)) for j in range(i))
    risk = engine.risk(difference="central")
    first = (*risk.rub_buckets, *risk.foreign_buckets)
    for row, value in zip(first, second.gradient_rub, strict=False):
        assert value == row.dv01_rub


def test_fx_futures_on_xccy_quotes_equals_bump_and_rebuild() -> None:
    contract = FxFuturesContract("CR-12.26", "CNY", 1000.0, 1.0, date(2026, 12, 18))

    def engine(foreign: DiscountCurve | XccyCurveBootstrapRequest) -> FxFuturesRiskEngine:
        request = FxFuturesRiskRequest(contract, SPOT["CNY"], TOM, RUB, foreign, basis_bp=5.0)
        return FxFuturesRiskEngine(request)

    quoted = engine(xccy_quotes())
    assert quoted.foreign_curve_basis == "xccy_quote"
    built = bootstrap_xccy_curve(xccy_quotes(), rub_discount_curve=RUB)
    assert quoted.base_price == engine(built).base_price
    risk = quoted.risk(bump_bp=1.0, difference="central")
    for k, row in enumerate(risk.foreign_buckets):
        up, down = (
            engine(
                bootstrap_xccy_curve(moved_quotes(bucket(k, s)), rub_discount_curve=RUB)
            ).base_price
            for s in (1.0, -1.0)
        )
        expected = (up - down) / 2.0 * contract.multiplier
        assert row.dv01_rub == pytest.approx(expected, rel=1e-9, abs=1e-9)


# --- portfolio ------------------------------------------------------------------------


def book_run(policy: object) -> PortfolioRiskResultV3:
    extra: list[PortfolioInstrument] = [
        FxForwardInstrument("SWAP-CNY-Q", SWAP, "RUB_OIS", "CNY_XCCY"),
        FxFuturesInstrument(
            "CR-12.26",
            FxFuturesContract("CR-12.26", "CNY", 1000.0, 1.0, date(2026, 12, 18)),
            "RUB_OIS",
            "CNY_XCCY",
        ),
    ]
    snap = snapshot()
    snap = replace(
        snap, futures_quotes=(*snap.futures_quotes, FuturesQuote("CR-12.26", 11.45, 11.44))
    )
    trading = AccountingCategory.TRADING
    picked = (
        Position("p-swap-q", "SWAP-CNY-Q", 1.0, "DESK", trading, "CNY"),
        Position("p-cr", "CR-12.26", 10.0, "DESK", trading, "CNY"),
        *(p for p in positions() if p.position_id == "p-xcs"),
    )
    result = value_portfolio(
        run_id="xccy-quotes",
        as_of=AS_OF,
        snapshot=snap,
        instruments=(*instruments(), *extra),
        positions=picked,
        policy=policy,  # type: ignore[arg-type]
    )
    assert isinstance(result, PortfolioRiskResultV3)
    return result


def test_portfolio_rejects_fx_products_on_xccy_quotes_without_the_flag() -> None:
    result = book_run(POLICY)
    codes = {r.position_id: r.code for r in result.coverage.rejections}
    assert codes == {"p-swap-q": "INVALID_TERMS", "p-cr": "INVALID_TERMS"}


def test_portfolio_values_fx_products_on_xccy_quotes_with_the_flag() -> None:
    policy = replace(POLICY, quote_curve_risk=True)
    result = book_run(policy)
    assert result.coverage.status == "complete", result.coverage
    rows = {r.position_id: r for r in result.derivative_rows()}
    factors = rows["p-swap-q"].factor_risk.first_order_by_factor()
    standalone = FxForwardRiskEngine(forward_request(SWAP, xccy_quotes())).risk(
        difference="central"
    )
    for k, b in enumerate(RISK_BUCKETS):
        assert factors[f"IR|CNY_XCCY|{b}"] == pytest.approx(
            standalone.foreign_buckets[k].dv01_rub, rel=1e-9, abs=1e-6
        )
    models = {m.position_id: m for m in result.position_models}
    for pid in ("p-swap-q", "p-cr", "p-xcs"):
        assert "quote_curve" in models[pid].flags
    assert "IR|CNY_XCCY|1Y" in {t.factor for t in result.factor_totals}
    assert canonical_sha256(policy) != canonical_sha256(POLICY)


def test_fx_cross_takes_xccy_quotes_as_its_cny_curve() -> None:
    usd = node_curve(0.042, 0.0008)
    trade = FxCrossForward(
        "X-Q", "USD", "CNY", 1.0, FxForwardLeg(date(2027, 10, 18), 1_000_000.0, 7.1)
    )

    def engine(cny: DiscountCurve | XccyCurveBootstrapRequest) -> FxCrossRiskEngine:
        request = FxCrossRiskRequest(trade, 81.5, TOM, SPOT["CNY"], TOM, RUB, usd, cny, 0.0, 0.0)
        return FxCrossRiskEngine(request)

    quoted = engine(xccy_quotes())
    built = engine(bootstrap_xccy_curve(xccy_quotes(), rub_discount_curve=RUB))
    assert quoted.base_pv_rub == built.base_pv_rub
    assert quoted.quote_curve_basis == "xccy_quote"
    assert quoted.base_id != built.base_id
    risk = quoted.risk(difference="central")
    for k, row in enumerate(risk.quote_buckets):
        up, down = (
            engine(
                bootstrap_xccy_curve(moved_quotes(bucket(k, s)), rub_discount_curve=RUB)
            ).base_pv_rub
            for s in (1.0, -1.0)
        )
        assert row.dv01_rub == pytest.approx((up - down) / 2.0, rel=1e-9, abs=1e-6)
