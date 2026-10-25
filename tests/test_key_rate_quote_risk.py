"""Key-rate projection curve risk in KRS quotes (``KeyRateCurveRequest``, issue #77).

Every check rebuilds the curve independently: ``build_key_rate_forward_curve`` on KRS
quotes moved by the bucket hats at their maturities, re-dated with
``rebase_key_rate_curve``, and the position repriced on that curve.
"""

from __future__ import annotations

import math
from dataclasses import replace
from datetime import date

import pytest
from portfolio_v3_book import AS_OF, BUSINESS_DATE, CAL, POLICY, TOM, instruments, positions
from portfolio_v3_book import snapshot as book_snapshot

from rub_fixed_income import (
    CurveEntry,
    DiscountCurve,
    InterestRateSwap,
    KeyRateCurveBuildRequest,
    KeyRateCurveRequest,
    KeyRateFuturesContract,
    KeyRateSwapQuote,
    PortfolioRiskResultV3,
    RateFuturesRiskEngine,
    RateFuturesRiskRequest,
    SwapRiskEngine,
    SwapRiskRequest,
    SwapScenario,
    ValuationPolicy,
    build_key_rate_forward_curve,
    canonical_sha256,
    from_dict,
    horizon_portfolio,
    swap_convention,
    to_dict,
    value_portfolio,
)
from rub_fixed_income._risk_common import RISK_BUCKETS, _shift_at
from rub_fixed_income.conventions import DayCount
from rub_fixed_income.domain import MarketSnapshot
from rub_fixed_income.key_rate import KeyRateForwardCurve, rebase_key_rate_curve
from rub_fixed_income.portfolio_instruments import SwapInstrument

RATE = 0.145
NOTIONAL = 300_000_000.0
KRS = swap_convention("rub_key_rate_swap_v1")
MIDS = (("3M", 0.163), ("6M", 0.158), ("1Y", 0.151), ("2Y", 0.143), ("3Y", 0.139))
NEXT_POLICY = date(2026, 10, 26)


def build(shift: tuple[float, ...] | None = None) -> KeyRateCurveBuildRequest:
    """KRS quotes on a flat TOM discount curve; ``shift`` moves every side by the bucket
    hats at each quote's ACT/ACT ISDA time from the business date (the core's rule)."""
    request = KeyRateCurveBuildRequest(
        BUSINESS_DATE,
        0.165,
        NEXT_POLICY,
        tuple(KeyRateSwapQuote(t, m - 0.0004, m, m + 0.0004) for t, m in MIDS),
        DiscountCurve.flat(TOM, RATE),
        CAL,
    )
    if shift is None:
        return request
    maturities = build_key_rate_forward_curve(request).maturities
    moved = []
    for quote, maturity in zip(request.quotes, maturities, strict=True):
        time = DayCount.ACT_ACT_ISDA.year_fraction(BUSINESS_DATE, maturity)
        bp = _shift_at(time, shift) * 1e-4
        moved.append(KeyRateSwapQuote(quote.tenor, quote.bid + bp, quote.mid + bp, quote.ask + bp))
    return replace(request, quotes=tuple(moved))


def rebuilt(shift: tuple[float, ...] | None = None) -> KeyRateForwardCurve:
    curve = build_key_rate_forward_curve(build(shift)).mid.curve
    return rebase_key_rate_curve(curve, BUSINESS_DATE, rate_in_force=0.165)


def quotes() -> KeyRateCurveRequest:
    return KeyRateCurveRequest(build(), BUSINESS_DATE, 0.165)


def bucket(k: int, bp: float) -> tuple[float, ...]:
    return tuple(bp if i == k else 0.0 for i in range(len(RISK_BUCKETS)))


def krs(tenor: str = "2Y", fixed_rate: float = 0.145) -> InterestRateSwap:
    return InterestRateSwap.from_tenor(
        swap_id=f"KRS-{tenor}",
        side="payer",
        notional=NOTIONAL,
        fixed_rate=fixed_rate,
        trade_date=BUSINESS_DATE,
        tenor=tenor,
        calendar=CAL,
        convention=KRS,
    )


def swap_pv(swap: InterestRateSwap, projection: KeyRateForwardCurve) -> float:
    discount = DiscountCurve.flat(BUSINESS_DATE, RATE)
    return SwapRiskEngine(SwapRiskRequest(swap, CAL, discount, projection)).base_pv


def quote_engine(swap: InterestRateSwap) -> SwapRiskEngine:
    discount = DiscountCurve.flat(BUSINESS_DATE, RATE)
    return SwapRiskEngine(SwapRiskRequest(swap, CAL, discount, quotes()))


def test_request_curve_is_the_rebased_mid_calibration_and_shocks_are_rebuilds() -> None:
    request = quotes()
    assert request.curve() == rebuilt()
    for shift in (bucket(3, 1.0), bucket(5, -1.0), (2.0,) * 11, bucket(0, 25.0)):
        shocked = request.shocked(shift)
        expected = rebuilt(shift)
        assert shocked.current_rate == expected.current_rate == 0.165
        for mine, theirs in zip(shocked.segments, expected.segments, strict=True):
            assert mine.rate == pytest.approx(theirs.rate, abs=1e-15)
    assert request.shocked((0.0,) * 11) is request.curve()


def test_swap_quote_risk_equals_bump_and_rebuild_per_bucket() -> None:
    swap = krs()
    engine = quote_engine(swap)
    assert engine.base_pv == pytest.approx(swap_pv(swap, rebuilt()), rel=1e-14, abs=1e-6)
    assert engine.projection_curve_basis == "key_rate_quote"
    risk = engine.risk(bump_bp=1.0)
    projection = next(c for c in risk.curves if c.curve_role == "projection")
    assert projection.curve_basis == "key_rate_quote"
    for k, row in enumerate(projection.buckets):
        up = swap_pv(swap, rebuilt(bucket(k, 1.0)))
        down = swap_pv(swap, rebuilt(bucket(k, -1.0)))
        assert row.dv01_rub == pytest.approx((up - down) / 2.0, rel=1e-9, abs=1e-4)
    # The 2Y swap sits between the 1Y and 2Y quotes and 3Y is beyond it: buckets past
    # 3Y move no quote, so they carry no risk; the 2Y bucket dominates.
    by_bucket = {b.bucket: b.dv01_rub for b in projection.buckets}
    assert all(by_bucket[b] == 0.0 for b in ("5Y", "7Y", "10Y", "15Y", "20Y+"))
    assert by_bucket["2Y"] > 0.0


def test_parallel_quote_shift_moves_a_par_swap_by_its_annuity() -> None:
    """Every quote moves by exactly 1 bp, so a par KRS of a quoted tenor reprices at its
    quote plus 1 bp: its PV moves by notional * annuity * 1 bp (TOM and business-date
    flat curves give the same par rates)."""
    swap = krs("2Y", 0.143)
    engine = quote_engine(swap)
    valuation = engine.valuation()
    assert abs(valuation.pv_rub) < 1e-6
    risk = engine.risk(bump_bp=1.0)
    projection = next(c for c in risk.curves if c.curve_role == "projection")
    expected = NOTIONAL * valuation.annuity * 1e-4
    assert projection.parallel_dv01_rub == pytest.approx(expected, rel=1e-9)
    assert projection.dv01_rub == pytest.approx(expected, rel=1e-9)
    shocked = engine.reprice(SwapScenario.parallel(projection_bp=10.0))
    assert shocked.total_pnl_rub == pytest.approx(10.0 * expected, rel=1e-9)


def test_quote_risk_is_linear_and_its_hessian_block_is_zero() -> None:
    engine = quote_engine(krs())
    second = engine.second_order(bump_bp=1.0)
    size = len(RISK_BUCKETS)
    for i in range(size, 2 * size):
        for j in range(size, 2 * size):
            assert second.hessian_rub_per_bp2[i][j] == 0.0
    first = next(c for c in engine.risk().curves if c.curve_role == "projection")
    for k, row in enumerate(first.buckets):
        assert second.gradient_rub_per_bp[size + k] == pytest.approx(row.dv01_rub, rel=1e-12)


def test_quote_shocks_outside_the_calibration_domain_are_rejected() -> None:
    engine = quote_engine(krs())
    with pytest.raises(ValueError, match="outside the supported domain"):
        engine.reprice(SwapScenario.parallel(projection_bp=-2000.0))


def test_keyrate_futures_quote_risk_equals_bump_and_rebuild() -> None:
    contract = KeyRateFuturesContract("KK-12.26", date(2026, 12, 18), date(2026, 12, 21))

    def price(curve: KeyRateForwardCurve | KeyRateCurveRequest) -> RateFuturesRiskEngine:
        return RateFuturesRiskEngine(RateFuturesRiskRequest(contract, curve, basis_bp=3.0))

    engine = price(quotes())
    assert engine.curve_basis == "key_rate_quote"
    assert engine.base_price == price(rebuilt()).base_price
    risk = engine.risk(bump_bp=1.0, difference="central")
    for k, row in enumerate(risk.buckets):
        up = price(rebuilt(bucket(k, 1.0))).base_price
        down = price(rebuilt(bucket(k, -1.0))).base_price
        expected = (up - down) / 2.0 * contract.margin_per_point_rub
        assert row.dv01_rub == pytest.approx(expected, rel=1e-9, abs=1e-9)
    assert sum(abs(b.dv01_rub) for b in risk.buckets) > 0.0
    assert engine.horizon(date(2026, 11, 16)).horizon_pnl_rub == pytest.approx(
        price(rebuilt()).horizon(date(2026, 11, 16)).horizon_pnl_rub, abs=1e-9
    )


def test_policy_default_keeps_its_bytes_and_the_flag_round_trips() -> None:
    assert "quote_curve_risk" not in to_dict(POLICY)
    assert canonical_sha256(POLICY) == canonical_sha256(ValuationPolicy("RUB_OIS"))
    on = replace(POLICY, quote_curve_risk=True)
    assert to_dict(on)["quote_curve_risk"] is True
    assert from_dict(ValuationPolicy, to_dict(on)) == on
    assert from_dict(ValuationPolicy, to_dict(POLICY)) == POLICY
    with pytest.raises(TypeError):
        replace(POLICY, quote_curve_risk=1)  # type: ignore[arg-type]


def test_snapshot_with_krs_quotes_round_trips() -> None:
    snap = quote_snapshot()
    assert from_dict(MarketSnapshot, to_dict(snap)) == snap


# --- portfolio ------------------------------------------------------------------------


def quote_snapshot() -> MarketSnapshot:
    snap = book_snapshot()
    curves = tuple(
        CurveEntry("KEY_RATE", "rub_key_rate_swap_v1", quotes()) if c.curve_id == "KEY_RATE" else c
        for c in snap.curves
    )
    return replace(snap, curves=curves)


def built_snapshot() -> MarketSnapshot:
    snap = book_snapshot()
    curves = tuple(
        CurveEntry("KEY_RATE", "rub_key_rate_swap_v1", rebuilt()) if c.curve_id == "KEY_RATE" else c
        for c in snap.curves
    )
    return replace(snap, curves=curves)


def run(snap: MarketSnapshot, policy: ValuationPolicy) -> PortfolioRiskResultV3:
    picked = [p for p in positions() if p.position_id in ("p-krs", "p-kk", "p-ois")]
    result = value_portfolio(
        run_id="krs-quotes",
        as_of=AS_OF,
        snapshot=snap,
        instruments=instruments(),
        positions=tuple(picked),
        policy=policy,
    )
    assert isinstance(result, PortfolioRiskResultV3)
    assert result.coverage.status == "complete", result.coverage
    return result


def key_rate_factors(result: PortfolioRiskResultV3) -> dict[str, float]:
    return {
        t.factor: t.first_order_rub
        for t in result.factor_totals
        if t.factor.startswith("IR|KEY_RATE|")
    }


def test_portfolio_without_the_flag_takes_forward_risk_of_the_built_mid_curve() -> None:
    from_quotes = run(quote_snapshot(), POLICY)
    from_curve = run(built_snapshot(), POLICY)
    assert key_rate_factors(from_quotes) == key_rate_factors(from_curve)
    assert from_quotes.total_pv_rub == from_curve.total_pv_rub
    assert all("quote_curve" not in m.flags for m in from_quotes.position_models)


def test_portfolio_with_the_flag_takes_krs_quote_risk() -> None:
    policy = replace(POLICY, quote_curve_risk=True)
    result = run(quote_snapshot(), policy)
    forward = run(quote_snapshot(), POLICY)
    assert result.total_pv_rub == forward.total_pv_rub
    assert result.job_id != forward.job_id
    rows = {r.position_id: r for r in result.derivative_rows()}
    krs_row = rows["p-krs"]
    q = krs_row.quantity
    swap = next(i for i in instruments() if i.instrument_id == "KRS-2Y")
    assert isinstance(swap, SwapInstrument)
    discount = book_snapshot().curve("RUB_OIS").curve
    assert isinstance(discount, DiscountCurve)
    engine = SwapRiskEngine(SwapRiskRequest(swap.swap, CAL, discount, quotes()))
    projection = next(c for c in engine.risk().curves if c.curve_role == "projection")
    factors = krs_row.factor_risk.first_order_by_factor()
    second = engine.second_order()
    for k, b in enumerate(RISK_BUCKETS):
        assert factors[f"IR|KEY_RATE|{b}"] == pytest.approx(
            q * second.gradient_rub_per_bp[len(RISK_BUCKETS) + k], rel=1e-12, abs=1e-9
        )
        assert q * projection.buckets[k].dv01_rub == pytest.approx(
            factors[f"IR|KEY_RATE|{b}"], rel=1e-9, abs=1e-6
        )
    assert key_rate_factors(result) != key_rate_factors(forward)
    models = {m.position_id: m for m in result.position_models}
    assert "quote_curve" in models["p-krs"].flags
    assert "quote_curve" in models["p-kk"].flags
    assert "quote_curve" not in models["p-ois"].flags
    assert math.isfinite(sum(key_rate_factors(result).values()))


def test_portfolio_rejects_krs_quotes_of_another_business_date() -> None:
    snap = quote_snapshot()
    other = KeyRateCurveRequest(
        replace(
            build(),
            trade_date=date(2026, 10, 14),
            discount_curve=DiscountCurve.flat(BUSINESS_DATE, RATE),
        ),
        date(2026, 10, 14),
        0.165,
    )
    curves = tuple(
        CurveEntry("KEY_RATE", "rub_key_rate_swap_v1", other) if c.curve_id == "KEY_RATE" else c
        for c in snap.curves
    )
    picked = tuple(p for p in positions() if p.position_id == "p-krs")
    result = value_portfolio(
        run_id="krs-quotes",
        as_of=AS_OF,
        snapshot=replace(snap, curves=curves),
        instruments=instruments(),
        positions=picked,
        policy=replace(POLICY, quote_curve_risk=True),
    )
    assert result.coverage.status == "failed"
    assert result.coverage.rejections[0].code == "INVALID_TERMS"


def test_horizon_portfolio_carries_on_the_mid_curve_of_krs_quotes() -> None:
    picked = tuple(p for p in positions() if p.position_id in ("p-krs", "p-kk"))

    def carry(snap: MarketSnapshot, policy: ValuationPolicy) -> list[float]:
        result = horizon_portfolio(
            snapshot=snap,
            instruments=instruments(),
            positions=picked,
            policy=policy,
            horizon_date=date(2026, 11, 16),
        )
        rows = sorted(result.derivative_rows(), key=lambda r: r.position_id)
        assert len(rows) == 2
        return [r.horizon_pnl_rub for r in rows]

    expected = carry(built_snapshot(), POLICY)
    assert carry(quote_snapshot(), replace(POLICY, quote_curve_risk=True)) == expected
