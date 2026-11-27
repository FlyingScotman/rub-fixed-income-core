"""Issue #120: one KRS market, rebuilt independently under both quote shifts."""

from collections import defaultdict
from collections.abc import Callable
from dataclasses import replace
from datetime import date, timedelta
from inspect import signature
from typing import Any

import numpy as np
import pytest
from portfolio_v3_book import AS_OF, BUSINESS_DATE, CAL, TOM, key_rate_quotes
from portfolio_v3_book import instruments as mixed_instruments
from portfolio_v3_book import positions as mixed_positions
from portfolio_v3_book import snapshot as mixed_snapshot
from test_xccy_rub_index import portfolio_swap

from rub_fixed_income import (
    RISK_BUCKETS,
    RUONIA_OIS_COMPOUND,
    AccountingCategory,
    BondCashflow,
    BondPriceObservation,
    BondRiskEngine,
    BondRiskRequest,
    BondScenario,
    CurveBootstrapRequest,
    CurveEntry,
    DayCount,
    DiscountCurve,
    FactorRisk,
    FixingEntry,
    FixingHistory,
    FloatingCouponLeg,
    FloatingCouponPeriod,
    FloatingCouponRule,
    FloatingLeg,
    FloatingRepoRate,
    Instrument,
    InterestRateSwap,
    InvalidTerms,
    KeyRateCurveBuildRequest,
    KeyRateCurveRequest,
    KeyRateForwardCurve,
    KeyRateFuturesContract,
    KeyRateProjection,
    MarketSnapshot,
    OisTail,
    PortfolioInstrument,
    PortfolioRiskResult,
    PortfolioRiskResultV3,
    Position,
    PrincipalLeg,
    ProductType,
    RateFuturesRiskEngine,
    RateFuturesRiskRequest,
    Redemption,
    RepoDayBasis,
    RepoInstrument,
    RepoRiskEngine,
    RepoRiskRequest,
    RepoTrade,
    ShockedCurveCache,
    SwapInstrument,
    SwapRiskEngine,
    SwapRiskRequest,
    SwapScenario,
    TwoCurveBondRiskResult,
    ValuationPolicy,
    XccyInstrument,
    XccyRiskEngine,
    XccyRiskRequest,
    ZSpreadConvention,
    bootstrap_discount_curve,
    build_key_rate_forward_curve,
    key_rate_projection_curve,
    key_rate_weights,
    ois_curve_convention,
    ois_quotes,
    portfolio,
    portfolio_products,
    rebase_key_rate_curve,
    swap_convention,
    value_portfolio,
)
from rub_fixed_income.dates import add_months
from rub_fixed_income.floating import ObservationDays
from rub_fixed_income.ois_tail import extend_ois_request
from rub_fixed_income.tenors import resolve_tenor

COUNT = DayCount.ACT_365_FIXED
OIS_SPEC = "rub_ruonia_ois_moex_v2"
KRS_SPEC = "rub_key_rate_swap_v1"
SIZE = len(RISK_BUCKETS)
ZERO = (0.0,) * SIZE
QUANTITY = 1_000_000.0


def ruonia(curve_tenors: tuple[str, ...] = RISK_BUCKETS[:7]) -> CurveBootstrapRequest:
    quotes = ois_quotes(
        BUSINESS_DATE,
        tuple((tenor, 0.156 - 0.001 * i) for i, tenor in enumerate(curve_tenors)),
        CAL,
    )
    return CurveBootstrapRequest(BUSINESS_DATE, quotes, ois_curve_convention(OIS_SPEC, CAL))


def bucket(index: int, bp: float) -> tuple[float, ...]:
    return tuple(bp if index == i else 0.0 for i in range(SIZE))


def independent(
    ois: CurveBootstrapRequest,
    krs: KeyRateCurveRequest,
    rub: tuple[float, ...] = ZERO,
    key: tuple[float, ...] = ZERO,
) -> tuple[DiscountCurve, KeyRateForwardCurve]:
    def bump(when: date, dc: DayCount, shifts: tuple[float, ...]) -> float:
        weights = key_rate_weights(dc.year_fraction(BUSINESS_DATE, when))
        return 1e-4 * sum(w * s for w, s in zip(weights, shifts, strict=True))

    moved_ois = tuple(
        replace(q, par_rate=q.par_rate + bump(q.maturity, ois.convention.time_day_count, rub))
        for q in ois.quotes
    )
    discount = bootstrap_discount_curve(replace(ois, quotes=moved_ois))
    at_tom = discount.discount_factor(TOM)
    tom_curve = replace(
        discount,
        curve_date=TOM,
        nodes=tuple((d, df / at_tom) for d, df in discount.nodes if d > TOM),
    )
    moved_krs = []
    for q in krs.build.quotes:
        maturity = resolve_tenor(BUSINESS_DATE, q.tenor, CAL).end_date
        rate = bump(maturity, DayCount.ACT_ACT_ISDA, key)
        moved_krs.append(replace(q, bid=q.bid + rate, mid=q.mid + rate, ask=q.ask + rate))
    mid = build_key_rate_forward_curve(
        replace(krs.build, discount_curve=tom_curve, quotes=tuple(moved_krs))
    ).mid.curve
    return discount, rebase_key_rate_curve(mid, BUSINESS_DATE, rate_in_force=krs.rate_in_force)


def key_history() -> FixingHistory:
    return FixingHistory(
        "key_rate",
        BUSINESS_DATE,
        ((BUSINESS_DATE - timedelta(days=1), 0.165),),
        CAL,
        "test-key",
        known_at_utc="2026-10-15T12:00:00Z",
    )


def floater(
    curve_id: str,
    observation_days: ObservationDays = "business",
    *,
    periods: tuple[FloatingCouponPeriod, ...] | None = None,
) -> Instrument:
    if periods is None:
        periods = tuple(
            FloatingCouponPeriod(
                add_months(TOM, i), add_months(TOM, i + 1), add_months(TOM, i + 1), 1000.0
            )
            for i in range(31)
        )
    rule = FloatingCouponRule(
        "key_rate", "arithmetic", 0.0165, COUNT, COUNT, observation_days=observation_days
    )
    return Instrument(
        "TEST-FRN",
        ProductType.FLOATING_BOND,
        "RUB",
        1000.0,
        FloatingCouponLeg(rule, periods, "TEST-KEY", curve_id),
        PrincipalLeg((Redemption(periods[-1].payment_date, 1000.0),)),
        "MOEX",
    )


def bond_request(
    instrument: Instrument,
    ois: CurveBootstrapRequest,
    krs: KeyRateCurveRequest,
) -> BondRiskRequest:
    leg = instrument.coupon_leg
    assert isinstance(leg, FloatingCouponLeg)
    return BondRiskRequest(
        cashflows=tuple(
            BondCashflow(f.payment_date, f.amount) for f in instrument.cashflows(after=TOM)
        ),
        settlement_date=TOM,
        curve=ois,
        spread_kind="credit",
        spread_convention=ZSpreadConvention(),
        dirty_price_rub=1000.0,
        quantity=QUANTITY,
        floating_leg=FloatingLeg(leg.rule, leg.periods, key_history()),
        projection_curve=KeyRateProjection(krs, CAL, COUNT, leg.rule.observation_days),
    )


def position(inst: PortfolioInstrument, quantity: float = 1.0) -> Position:
    return Position(
        "p-" + inst.instrument_id,
        inst.instrument_id,
        quantity,
        "TEST",
        AccountingCategory.TRADING,
        inst.currency,
    )


def book_market(ois: CurveBootstrapRequest, quotes: KeyRateCurveRequest) -> MarketSnapshot:
    market = mixed_snapshot()
    return replace(
        market,
        curves=(
            *(c for c in market.curves if c.curve_id not in ("KEY_RATE", quotes.ruonia_curve_id)),
            CurveEntry(str(quotes.ruonia_curve_id), OIS_SPEC, ois),
            CurveEntry("KEY_RATE", KRS_SPEC, quotes),
        ),
        fixings=(*market.fixings, FixingEntry("TEST-KEY", key_history())),
        quotes=(
            *market.quotes,
            BondPriceObservation("TEST-FRN", 100.0, "test", accrued_per_bond=0.0),
        ),
    )


def bond_pv(engine: BondRiskEngine, discount: DiscountCurve, projection: DiscountCurve) -> float:
    request = replace(
        engine.request,
        curve=discount,
        projection_curve=projection,
        spread_bp=engine.spread_bp,
        dirty_price_rub=None,
    )
    return BondRiskEngine(request).base_price_rub * request.quantity


def swap(tenor: str, rate: float, spec: str = KRS_SPEC) -> InterestRateSwap:
    return InterestRateSwap.from_tenor(
        swap_id=f"{spec}/{tenor}",
        side="payer",
        notional=100_000_000.0,
        fixed_rate=rate,
        trade_date=BUSINESS_DATE,
        tenor=tenor,
        calendar=CAL,
        convention=swap_convention(spec),
    )


def key_repo() -> RepoTrade:
    return RepoTrade(
        "TEST-REPO",
        "reverse_repo",
        TOM,
        date(2027, 4, 16),
        80_000_000.0,
        FloatingRepoRate("KEY_RATE", 0.005, RepoDayBasis.ACT_365_FIXED, "calendar_day", COUNT),
        None,
    )


def test_key_rate_spread_buckets_hold_projected_coupons_in_engine_and_portfolio() -> None:
    ois, quotes = ruonia(), key_rate_quotes("RUONIA")
    instrument = floater("KEY_RATE")
    engine = BondRiskEngine(bond_request(instrument, ois, quotes))
    risk = engine.risk(difference="central")
    held = engine.spread_bucket_risk(risk=None)
    assert engine.spread_bucket_risk(risk=risk) == held
    assert sum(held) == pytest.approx(risk.cs01_rub, abs=1e-8)

    result = value_portfolio(
        run_id="key-rate-spread-buckets",
        as_of=AS_OF,
        snapshot=book_market(ois, quotes),
        instruments=(instrument,),
        positions=(position(instrument, QUANTITY),),
        policy=ValuationPolicy("RUONIA", include_second_order=False, spread_buckets=True),
    )
    assert result.status == "complete", result.coverage.rejections
    (row,) = result.positions
    assert row.spread_buckets_rub == held


def test_key_rate_derivatives_reject_a_different_discount_id() -> None:
    ois, quotes = ruonia(), key_rate_quotes("RUONIA")
    market = book_market(ois, quotes)
    # The OIS inputs are identical. The product must still name the linked curve ID.
    market = replace(market, curves=(*market.curves, CurveEntry("OTHER", OIS_SPEC, ois)))
    instruments = (
        SwapInstrument("TEST-KRS", swap("2Y", 0.15), "MOEX", "OTHER", "KEY_RATE"),
        RepoInstrument("TEST-REPO", key_repo(), "MOEX", "OTHER", "KEY_RATE", "TEST-KEY"),
        XccyInstrument(
            "TEST-XCCY",
            portfolio_swap(TOM, date(2027, 7, 16)),
            "OTHER",
            "CNY_DF",
            rub_projection_curve_id="KEY_RATE",
        ),
    )
    result = value_portfolio(
        run_id="key-rate-wrong-discount",
        as_of=AS_OF,
        snapshot=market,
        instruments=instruments,
        positions=tuple(position(i) for i in instruments),
        policy=ValuationPolicy("RUONIA", include_second_order=False),
    )
    assert result.status == "failed"
    assert len(result.coverage.rejections) == len(instruments)
    for rejection in result.coverage.rejections:
        assert rejection.code == "INVALID_TERMS"
        assert "links to RUONIA RUONIA; product discount OTHER must match" in rejection.message


def test_key_rate_future_on_krs_quotes_requires_ruonia() -> None:
    contract = KeyRateFuturesContract("TEST-KK", date(2027, 1, 15), date(2027, 1, 18))
    with pytest.raises(InvalidTerms, match="need ruonia_curve"):
        RateFuturesRiskRequest(contract, key_rate_quotes("RUONIA"), basis_bp=0.0)


def test_key_rate_projection_is_rejected_on_other_indices() -> None:
    ois, quotes = ruonia(), key_rate_quotes("RUONIA")
    bond = bond_request(floater("KEY_RATE"), ois, quotes)
    assert bond.floating_leg is not None
    ruonia_leg = FloatingLeg(
        replace(bond.floating_leg.rule, index="ruonia"),
        bond.floating_leg.periods,
        FixingHistory("ruonia", BUSINESS_DATE, (), CAL, "test-ruonia"),
    )
    with pytest.raises(InvalidTerms, match="requires a key-rate floating leg"):
        replace(bond, floating_leg=ruonia_leg)

    projection = KeyRateProjection(quotes, CAL, COUNT, "calendar")
    repo = replace(
        key_repo(),
        rate=FloatingRepoRate(
            "RUONIA", 0.005, RepoDayBasis.ACT_365_FIXED, "previous_settlement_day", COUNT
        ),
    )
    with pytest.raises(InvalidTerms, match="requires a KEY_RATE repo"):
        RepoRiskRequest(repo, CAL, ois, projection)

    ccs = portfolio_swap(TOM, date(2027, 7, 16))
    ccs = replace(ccs, rub_leg=replace(ccs.rub_leg, index=RUONIA_OIS_COMPOUND))
    with pytest.raises(InvalidTerms, match="requires an averaged key-rate RUB leg"):
        XccyRiskRequest(
            ccs,
            11.2,
            TOM,
            ois,
            DiscountCurve.flat(BUSINESS_DATE, 0.03),
            0.0,
            rub_projection_curve=projection,
        )


def test_calendar_day_projection_shares_cache_and_identity_across_calendars() -> None:
    ois, quotes = ruonia(), key_rate_quotes("RUONIA")
    context = portfolio._product_context(
        book_market(ois, quotes),
        ValuationPolicy("RUONIA"),
        "calendar-grid",
        ShockedCurveCache(),
        {},
        {},
        {},
        {},
        {},
    )
    other = replace(CAL, holidays=CAL.holidays | {date(2026, 11, 5)})
    first = context.key_rate_projection("KEY_RATE", "RUONIA", CAL, COUNT, "calendar")
    assert context.key_rate_projection("KEY_RATE", "RUONIA", other, COUNT, "calendar") is first
    assert replace(first, calendar=other)._quote_identity() == first._quote_identity()

    business = context.key_rate_projection("KEY_RATE", "RUONIA", CAL, COUNT, "business")
    shifted = context.key_rate_projection("KEY_RATE", "RUONIA", other, COUNT, "business")
    assert business is not shifted
    assert business._quote_identity() != shifted._quote_identity()


def test_all_five_products_match_independent_rebuild_by_bucket() -> None:
    ois, quotes = ruonia(), key_rate_quotes("RUONIA")
    bond = BondRiskEngine(bond_request(floater("KEY_RATE"), ois, quotes))
    shared = bond.curve_cache
    krs = SwapRiskEngine(SwapRiskRequest(swap("2Y", 0.15), CAL, ois, quotes), shared)
    calendar_projection = KeyRateProjection(quotes, CAL, COUNT, "calendar")
    repo = RepoRiskEngine(
        RepoRiskRequest(key_repo(), CAL, ois, calendar_projection, key_history()), shared
    )
    ccs = XccyRiskEngine(
        XccyRiskRequest(
            portfolio_swap(TOM, date(2027, 7, 16)),
            11.2,
            TOM,
            ois,
            DiscountCurve.flat(BUSINESS_DATE, 0.03),
            0.0,
            rub_projection_curve=calendar_projection,
        ),
        shared,
    )
    future = RateFuturesRiskEngine(
        RateFuturesRiskRequest(
            KeyRateFuturesContract("TEST-KK", date(2027, 1, 15), date(2027, 1, 18)),
            quotes,
            basis_bp=3.0,
            ruonia_curve=ois,
        ),
        shared,
    )
    assert isinstance(bond.request.projection_curve, KeyRateProjection)
    assert krs.request.projection_curve is bond.request.projection_curve.quotes
    assert isinstance(repo.request.projection_curve, KeyRateProjection)
    assert isinstance(ccs.request.rub_projection_curve, KeyRateProjection)
    assert repo.request.projection_curve.quotes is krs.request.projection_curve
    assert ccs.request.rub_projection_curve.quotes is krs.request.projection_curve
    assert future.request.projection_curve is krs.request.projection_curve
    horizon = date(2026, 11, 16)
    assert krs.horizon(horizon).model_version == "rub-irs-krs-quote-horizon-carry-v3"
    assert future.horizon(horizon).model_version == "rub-rate-futures-krs-quote-horizon-v3"
    assert (
        ccs.horizon(
            horizon, horizon_spot_value_date=CAL.add_business_days(horizon, 1)
        ).model_version
        == "rub-xccy-krs-quote-horizon-v2"
    )

    def pv(rub: tuple[float, ...], key: tuple[float, ...]) -> np.ndarray:
        discount, forward = independent(ois, quotes, rub, key)
        business = key_rate_projection_curve(
            forward, CAL, rate_day_count=COUNT, observation_days="business"
        )
        calendar = key_rate_projection_curve(
            forward, CAL, rate_day_count=COUNT, observation_days="calendar"
        )
        return np.array(
            (
                bond_pv(bond, discount, business),
                SwapRiskEngine(
                    replace(krs.request, discount_curve=discount, projection_curve=forward)
                ).base_pv,
                RepoRiskEngine(
                    replace(repo.request, discount_curve=discount, projection_curve=calendar)
                ).base_pv,
                XccyRiskEngine(
                    replace(ccs.request, rub_discount_curve=discount, rub_projection_curve=calendar)
                ).base_pv_rub,
                RateFuturesRiskEngine(
                    replace(
                        future.request,
                        projection_curve=forward,
                        ruonia_curve=None,
                    )
                ).base_price
                * future.request.contract.margin_per_point_rub,
            )
        )

    rb = bond.risk(difference="central")
    assert isinstance(rb, TwoCurveBondRiskResult)
    rs, rr, rx = krs.risk(), repo.risk(), ccs.risk()
    rf = future.risk(difference="central")
    rows = (
        (rb.buckets, rb.projection_buckets),
        (rs.curves[0].buckets, rs.curves[1].buckets),
        (rr.curves[0].buckets, rr.curves[1].buckets),
        (rx.curves[0].buckets, rx.curves[1].buckets),
        (rf.ruonia_buckets, rf.buckets),
    )
    for i in range(SIZE):
        for role in range(2):
            up = (bucket(i, 1.0), ZERO) if role == 0 else (ZERO, bucket(i, 1.0))
            down = (bucket(i, -1.0), ZERO) if role == 0 else (ZERO, bucket(i, -1.0))
            expected = tuple((pv(*up) - pv(*down)) / 2)
            actual = tuple(pair[role][i].dv01_rub for pair in rows)
            assert actual == pytest.approx(expected, rel=1e-6, abs=2e-6), (RISK_BUCKETS[i], role)
    assert rf.ruonia_curve_basis == "ois_quote"
    assert (
        rb.projection_curve_basis
        == rs.curves[1].curve_basis
        == rr.curves[1].curve_basis
        == (rx.curves[1].curve_basis)
        == rf.curve_basis
        == "key_rate_quote"
    )
    parallel_up, parallel_down = (1.0,) * SIZE, (-1.0,) * SIZE
    assert rb.parallel_dv01_rub == pytest.approx(
        (pv(parallel_up, parallel_up)[0] - pv(parallel_down, parallel_down)[0]) / 2, rel=1e-6
    )
    for matrix in (
        bond.second_order().hessian_rub_per_bp2,
        repo.second_order().hessian_rub_per_bp2,
        ccs.second_order().hessian_rub,
        future.second_order().hessian_rub_per_bp2,
    ):
        assert np.array_equal(matrix, np.transpose(matrix))

    shock = BondScenario.parallel(10.0)
    approx = bond.approximate(shock, projection_curve_shifts_bp=(10.0,) * SIZE)
    exact = bond.reprice(shock, projection_curve_shifts_bp=(10.0,) * SIZE)
    assert approx.exact_pnl_rub == exact.total_pnl_rub
    assert approx.approximate_pnl_rub == pytest.approx(exact.total_pnl_rub, rel=0.003, abs=0.5)


def test_krs_cross_block_matches_general_central_differences() -> None:
    ois, quotes = ruonia(), key_rate_quotes("RUONIA")
    engine = SwapRiskEngine(SwapRiskRequest(swap("3Y", 0.15), CAL, ois, quotes))
    matrix = np.asarray(engine.second_order().hessian_rub_per_bp2)
    assert np.array_equal(matrix, matrix.T)
    for i in range(SIZE):
        for j in range(SIZE):
            corners = tuple(
                engine.reprice(SwapScenario(bucket(i, a), bucket(j, b))).shocked_pv_rub
                for a, b in ((1, 1), (1, -1), (-1, 1), (-1, -1))
            )
            cross = ((corners[0] - corners[1]) - (corners[2] - corners[3])) / 4
            assert matrix[i, SIZE + j] == pytest.approx(cross, rel=3e-6, abs=1e-8)
    assert np.max(np.abs(matrix[:SIZE, SIZE:])) > 0.01
    assert np.count_nonzero(matrix[SIZE:, SIZE:]) == 0


def test_par_krs_uses_the_same_ruonia_extrapolation_beyond_the_last_ois_quote() -> None:
    # A single OIS node spans a leap-year boundary. Reconstructing the TOM curve
    # from the remaining node would change its extrapolated forward slope.
    ois, quotes = ruonia(("2Y",)), key_rate_quotes("RUONIA")
    quote = quotes.build.quotes[-1]
    engine = SwapRiskEngine(SwapRiskRequest(swap(quote.tenor, quote.mid), CAL, ois, quotes))
    assert engine.base_pv == pytest.approx(0.0, abs=1e-7)
    for move in (-10.0, 10.0):
        shocked = engine.reprice(SwapScenario.parallel(discount_bp=move))
        assert shocked.shocked_pv_rub == pytest.approx(0.0, abs=1e-7)


def test_par_swaps_hedge_the_floater_in_both_quote_bases() -> None:
    ois, quotes = ruonia((*RISK_BUCKETS[:-1], "20Y")), key_rate_quotes("RUONIA")
    bond = BondRiskEngine(bond_request(floater("KEY_RATE"), ois, quotes))
    cache = bond.curve_cache
    key_swaps = [
        SwapRiskEngine(SwapRiskRequest(swap(q.tenor, q.mid), CAL, ois, quotes), cache)
        for q in quotes.build.quotes
    ]
    ois_swaps = [
        SwapRiskEngine(SwapRiskRequest(swap(q.tenor, q.par_rate, OIS_SPEC), CAL, ois), cache)
        for q in ois.quotes
    ]
    delta_bp = 0.01
    target = bond.risk(bump_bp=delta_bp, difference="central")
    assert isinstance(target, TwoCurveBondRiskResult)
    target_gradient = np.array([b.dv01_rub for b in (*target.buckets, *target.projection_buckets)])
    gradient = []
    for engine in [*ois_swaps, *key_swaps]:
        risk = engine.risk(bump_bp=delta_bp)
        assert abs(engine.base_pv) < 1e-5
        discount = tuple(b.dv01_rub for b in risk.curves[0].buckets)
        projection = ZERO
        if len(risk.curves) == 2:
            assert max(abs(x) for x in discount) < 1e-5
            projection = tuple(b.dv01_rub for b in risk.curves[1].buckets)
        gradient.append((*discount, *projection))
    hedge_matrix = np.transpose(gradient)
    sizes, *_ = np.linalg.lstsq(hedge_matrix, target_gradient, rcond=None)
    assert target_gradient - hedge_matrix @ sizes == pytest.approx(np.zeros(2 * SIZE), abs=1e-6)

    def hedged_pnl(rub: tuple[float, ...], key: tuple[float, ...]) -> float:
        pnl = bond.reprice(BondScenario(rub), projection_curve_shifts_bp=key).total_pnl_rub
        hedge_pnls = [
            *(e.reprice(SwapScenario(rub)).total_pnl_rub for e in ois_swaps),
            *(e.reprice(SwapScenario(rub, key)).total_pnl_rub for e in key_swaps),
        ]
        return float(pnl - sizes @ hedge_pnls)

    rng = np.random.default_rng(120)
    for _ in range(3):
        rub = tuple(rng.normal(size=SIZE))
        key = tuple(rng.normal(size=SIZE))
        full = hedged_pnl(rub, key)
        half = hedged_pnl(tuple(s / 2.0 for s in rub), tuple(s / 2.0 for s in key))
        assert abs(full) > 0.01
        assert abs(half) == pytest.approx(abs(full) / 4, rel=0.02, abs=0.002)


@pytest.mark.parametrize("observation_days", ("business", "calendar"))
def test_portfolio_builds_the_projection_on_the_leg_observation_grid(
    observation_days: ObservationDays,
) -> None:
    ois = ruonia()
    quotes = key_rate_quotes("RUONIA")
    saturday = date(2026, 10, 24)
    quotes = replace(quotes, build=replace(quotes.build, next_policy_effective_date=saturday))
    friday, monday = saturday - timedelta(days=1), saturday + timedelta(days=2)
    instrument = floater(
        "KEY_RATE",
        observation_days,
        periods=(FloatingCouponPeriod(friday, monday, monday, 1000.0),),
    )
    market = book_market(ois, quotes)

    def run(market: MarketSnapshot, policy: ValuationPolicy) -> PortfolioRiskResult:
        return value_portfolio(
            run_id="observation-grid",
            as_of=AS_OF,
            snapshot=market,
            instruments=(instrument,),
            positions=(position(instrument, QUANTITY),),
            policy=policy,
        )

    policy = ValuationPolicy("RUONIA", include_second_order=False)
    result = run(market, policy)
    (row,) = result.positions
    assert row.projection_curve_id == "KEY_RATE"
    assert row.projection_curve_basis == "key_rate_quote"
    discount, forward = independent(ois, quotes)
    req = bond_request(instrument, ois, quotes)
    projection = key_rate_projection_curve(
        forward, CAL, rate_day_count=COUNT, observation_days=observation_days
    )
    assert req.floating_leg is not None
    (coupon,) = req.floating_leg.project(projection, settlement_date=TOM)
    expected = forward.rate_at(friday)
    if observation_days == "calendar":
        expected = (expected + 2 * forward.rate_at(saturday)) / 3
    assert coupon.coupon_rate == pytest.approx(expected + 0.0165, abs=1e-12)
    explicit = BondRiskEngine(replace(req, curve=discount, projection_curve=projection))
    assert row.spread_bp == pytest.approx(explicit.spread_bp, abs=1e-7)

    legacy = replace(
        market,
        curves=(
            *(c for c in market.curves if c.curve_id != "KEY_RATE"),
            CurveEntry("KEY_RATE", "rub_key_rate_projection_v1", projection),
        ),
    )
    (rejection,) = run(legacy, policy).coverage.rejections
    assert rejection.code == "INVALID_TERMS" and "KeyRateCurveRequest" in rejection.message
    duplicate = (*market.curves, CurveEntry("OTHER", OIS_SPEC, ois))
    (rejection,) = run(
        replace(market, curves=duplicate), replace(policy, discount_curve_id="OTHER")
    ).coverage.rejections
    assert rejection.code == "INVALID_TERMS" and "must match" in rejection.message
    assert isinstance(req.projection_curve, KeyRateProjection)
    bound = req.projection_curve._with_discount(ois)
    with pytest.raises(InvalidTerms, match="must match"):
        replace(req, curve=replace(ois, quotes=ois.quotes[1:]), projection_curve=bound)


def test_mixed_book_uses_one_basis_per_curve_and_obeys_ois_tail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Several derivative rows serialize curve IDs only. Capture the actual basis
    # from each engine's risk result at the point where the adapter assigns IDs.
    observed: dict[str, dict[str, str]] = {}
    current_position: str | None = None
    adapter_for = portfolio_products.adapter_for

    def track_adapter(instrument: object) -> portfolio_products.Adapter:
        adapter = adapter_for(instrument)

        def track(
            pos: Position, instrument: object, context: portfolio_products.ProductContext
        ) -> portfolio_products.ProductOutcome:
            nonlocal current_position
            current_position = pos.position_id
            try:
                return adapter(pos, instrument, context)
            finally:
                current_position = None

        return track

    def read_swap_or_repo(risk: Any, ids: dict[str, Any]) -> dict[str, str]:
        return {
            ids[
                "projection_curve_id" if c.curve_role == "projection" else "discount_curve_id"
            ]: c.curve_basis
            for c in risk.curves
        }

    def read_fx(risk: Any, ids: dict[str, Any]) -> dict[str, str]:
        return {
            ids["rub_curve_id"]: risk.rub_curve_basis,
            ids["foreign_curve_id"]: risk.foreign_curve_basis,
        }

    def read_rate_future(risk: Any, ids: dict[str, Any]) -> dict[str, str]:
        bases = {ids["curve_id"]: risk.curve_basis}
        if ids["ruonia_curve_id"] is not None:
            assert risk.ruonia_curve_basis == "ois_quote"
            bases[ids["ruonia_curve_id"]] = risk.ruonia_curve_basis
        else:
            assert risk.ruonia_curve_basis is None
        return bases

    def track_basis(name: str, read: Callable[[Any, dict[str, Any]], dict[str, str]]) -> None:
        factor_risk: Callable[..., FactorRisk] = getattr(portfolio_products, name)
        params = signature(factor_risk)

        def track(risk: Any, *args: Any, **kwargs: Any) -> FactorRisk:
            assert current_position is not None and current_position not in observed
            bound = params.bind(risk, *args, **kwargs)
            bound.apply_defaults()
            observed[current_position] = read(risk, bound.arguments)
            return factor_risk(risk, *args, **kwargs)

        monkeypatch.setattr(portfolio_products, name, track)

    monkeypatch.setattr(portfolio, "adapter_for", track_adapter)
    track_basis("swap_factor_risk", read_swap_or_repo)
    track_basis("repo_factor_risk", read_swap_or_repo)
    track_basis("fx_forward_factor_risk", read_fx)
    track_basis("fx_futures_factor_risk", read_fx)
    track_basis("rate_futures_factor_risk", read_rate_future)
    track_basis(
        "xccy_factor_risk",
        lambda r, ids: {ids["curve_ids"][c.role]: c.curve_basis for c in r.curves},
    )
    track_basis("cds_factor_risk", lambda r, ids: {ids["discount_curve_id"]: r.curve_basis})
    track_basis(
        "ofz_futures_factor_risk",
        lambda r, ids: {
            ids["bond_curve_id"]: r.bond_curve_basis,
            ids["funding_curve_id"]: r.funding_curve_basis,
        },
    )

    ois = ruonia(("1M", "3M", "6M", "1Y", "2Y", "3Y"))
    quotes = key_rate_quotes("RUB_PROJ")
    market = book_market(ois, quotes)
    bond = floater("KEY_RATE")
    repo = RepoInstrument("TEST-REPO", key_repo(), "MOEX", "RUB_PROJ", "KEY_RATE", "TEST-KEY")
    ccs = XccyInstrument(
        "TEST-XCCY",
        portfolio_swap(TOM, date(2027, 7, 16)),
        "RUB_PROJ",
        "CNY_DF",
        rub_projection_curve_id="KEY_RATE",
    )
    policy = ValuationPolicy(
        "RUB_OIS",
        include_second_order=False,
        curve_overrides=((bond.instrument_id, "RUB_PROJ"),),
        ois_tail=OisTail("RUB_PROJ"),
    )
    result = value_portfolio(
        run_id="one-quote-basis",
        as_of=AS_OF,
        snapshot=market,
        instruments=(*mixed_instruments(), bond, repo, ccs),
        positions=(*mixed_positions(), position(bond, QUANTITY), position(repo), position(ccs)),
        policy=policy,
    )
    assert isinstance(result, PortfolioRiskResultV3)
    assert result.status == "complete", result.coverage.rejections
    expected = {
        "RUB_OIS": "continuous_zero",
        "RUB_REPO": "continuous_zero",
        "RUB_PROJ": "ois_quote",
        "KEY_RATE": "key_rate_quote",
        "USD_DF": "continuous_zero",
        "CNY_DF": "continuous_zero",
        "CNY_XCCY": "xccy_quote",
    }
    for bond_row in result.positions:
        bases = {bond_row.curve_id: bond_row.curve_basis}
        if bond_row.projection_curve_id is not None:
            assert bond_row.projection_curve_basis == "key_rate_quote"
            bases[bond_row.projection_curve_id] = bond_row.projection_curve_basis
        observed[bond_row.position_id] = bases
    seen: dict[str, set[str]] = defaultdict(set)
    key_users = set()
    for row in (*result.positions, *result.derivative_rows()):
        factor_curve_ids = {f.split("|")[1] for f in row.factor_risk.factors if f.startswith("IR|")}
        assert factor_curve_ids == observed[row.position_id].keys(), row.position_id
        if "KEY_RATE" in factor_curve_ids:
            assert (
                getattr(row, "projection_curve_basis", None)
                == observed[row.position_id]["KEY_RATE"]
            )
            key_users.add(row.position_id)
        for curve_id in factor_curve_ids:
            basis = observed[row.position_id][curve_id]
            assert basis == expected[curve_id], (row.position_id, curve_id)
            seen[curve_id].add(basis)
    assert key_users == {"p-TEST-FRN", "p-TEST-REPO", "p-TEST-XCCY", "p-krs", "p-kk"}
    assert seen == {
        t.factor.split("|")[1]: {expected[t.factor.split("|")[1]]}
        for t in result.factor_totals
        if t.factor.startswith("IR|")
    }

    extended, _ = extend_ois_request(ois, None)
    engine = BondRiskEngine(bond_request(bond, extended, quotes))
    (row,) = [r for r in result.positions if r.position_id == "p-TEST-FRN"]
    moves = bucket(5, 1.0)
    down = bucket(5, -1.0)
    values = []
    for shift in (moves, down):
        discount, forward = independent(extended, quotes, rub=shift)
        projection = key_rate_projection_curve(
            forward, CAL, rate_day_count=COUNT, observation_days="business"
        )
        values.append(bond_pv(engine, discount, projection))
    assert row.factor_risk.first_order_by_factor()["IR|RUB_PROJ|3Y"] == pytest.approx(
        (values[0] - values[1]) / 2, rel=1e-6
    )


def test_editable_consumer_signatures() -> None:
    assert tuple(signature(KeyRateCurveBuildRequest).parameters) == (
        "trade_date",
        "current_rate",
        "next_policy_effective_date",
        "quotes",
        "discount_curve",
        "calendar",
        "rate_floor",
        "rate_cap",
        "fit_tolerance_bp",
    )
    assert tuple(signature(build_key_rate_forward_curve).parameters) == ("request",)
    assert build_key_rate_forward_curve(key_rate_quotes("RUONIA").build).usable
