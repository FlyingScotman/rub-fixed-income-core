"""OFZ risk as RUONIA OIS plus a basis, both computed (issue #83: variant Z, ZX tail)."""

from __future__ import annotations

import math
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from itertools import pairwise

import numpy as np
import pytest
from ofz_basis_data import (
    DATE,
    KBD,
    OIS,
    OIS_CALENDAR,
    PARAMS,
    SETTLEMENT,
    kbd_bond,
    ois_request,
    semiannual_flows,
)
from test_floating_kbd import history, periods, rule

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
    CalendarRangeError,
    CouponPeriod,
    CurveBootstrapRequest,
    CurveEntry,
    DiscountCurve,
    FactorRisk,
    FixedCouponLeg,
    FloatingLeg,
    Instrument,
    InvalidTerms,
    MarketSnapshot,
    OfzBasisCurve,
    OfzBasisIds,
    OfzBasisRiskEngine,
    PerpetualTail,
    PortfolioInstrument,
    PortfolioRiskResult,
    PortfolioRiskResultV3,
    PortfolioScenario,
    Position,
    PrincipalLeg,
    ProductType,
    Redemption,
    ShockedCurveCache,
    SwapInstrument,
    ValuationPolicy,
    ZSpreadConvention,
    basis_view,
    bond_factor_risk,
    bootstrap_discount_curve,
    canonical_sha256,
    from_dict,
    kbd_discount_curve,
    ofz_basis_factor_risk,
    ois_curve_convention,
    to_dict,
    value_portfolio,
)
from rub_fixed_income._par_overlay import par_rates
from rub_fixed_income._risk_common import _shock_curve, _shocked_request
from rub_fixed_income.dates import add_months
from rub_fixed_income.discount import DiscountFactorCurve
from rub_fixed_income.ofz_basis import (
    OFZ_BASIS_CURVE_BASIS,
    _annual_par_yield,
    _ComposedCurve,
)
from rub_fixed_income.risk import key_rate_weights

VIEW = OfzBasisCurve(KBD, OIS)
LONG = date(2041, 5, 15)  # a 14.6-year bond, like SU26238
N = len(RISK_BUCKETS)


def unit(bucket: str, size: float = 1.0) -> tuple[float, ...]:
    return tuple(size if b == bucket else 0.0 for b in RISK_BUCKETS)


def engine(maturity: date = LONG, coupon: float = 0.071, **kw: object) -> OfzBasisRiskEngine:
    return OfzBasisRiskEngine(kbd_bond(maturity, coupon, **kw), VIEW)  # type: ignore[arg-type]


# -- the ZX tail -------------------------------------------------------------------------


def test_zx_tail_adds_the_v3_tenors_beyond_the_last_quote_at_the_last_par_spread() -> None:
    last = OIS.quotes[-1]
    assert last.tenor == "10Y"
    spread = _annual_par_yield(KBD, last.maturity) - last.par_rate
    assert VIEW.tail_spread == spread
    assert [q.tenor for q in VIEW.synthetic_quotes] == ["15Y", "20Y"]
    for quote in VIEW.synthetic_quotes:
        years = int(quote.tenor[:-1])
        # The OIS convention's maturity: TOM start, MODFOL end.
        assert quote.maturity == date(2026 + years, 10, 16)
        assert quote.par_rate == _annual_par_yield(KBD, quote.maturity) - spread
    assert VIEW.extended.quotes == (*OIS.quotes, *VIEW.synthetic_quotes)
    # On a roll date the KBD par yield is the v3 instrument's.
    v3 = dict(par_rates(KBD))
    ten = add_months(DATE, 120)
    assert _annual_par_yield(KBD, ten) == pytest.approx(v3[ten], abs=1e-15)
    # A quote set reaching 20Y needs no tenor: the extended request is the plain one.
    long = ois_request(("1Y", "5Y", "10Y", "15Y", "20Y"))
    full = OfzBasisCurve(KBD, long)
    assert full.synthetic_quotes == () and full.tail_spread is None
    assert full.extended == full.ois


def test_ruonia_15y_moves_the_synthetic_quote_and_a_basis_bump_leaves_ois_alone() -> None:
    cache = ShockedCurveCache()
    moved = _shocked_request(VIEW.extended, unit("15Y"))
    by_tenor = {q.tenor: q for q in moved.quotes}
    base = {q.tenor: q for q in VIEW.extended.quotes}
    t15 = (by_tenor["15Y"].maturity - DATE).days / 365.0
    w15 = key_rate_weights(t15)[RISK_BUCKETS.index("15Y")]
    assert 0.99 < w15 <= 1.0
    assert by_tenor["15Y"].par_rate == pytest.approx(base["15Y"].par_rate + w15 * 1e-4, abs=1e-15)
    assert by_tenor["5Y"].par_rate == base["5Y"].par_rate
    # The rebuilt OIS curve moves at 15Y; the composed KBD follows it.
    shocked = VIEW.shocked(unit("15Y"), (0.0,) * N, cache)
    assert isinstance(shocked, _ComposedCurve)
    target = add_months(DATE, 180)
    assert shocked.discount_factor(target) < KBD.discount_factor(target)
    # A basis bump does not rebuild or read the OIS curve: it is the v3 view itself.
    before = cache.misses
    view = VIEW.shocked((0.0,) * N, unit("15Y"), cache)
    assert cache.misses == before
    assert view is _shock_curve(VIEW.kbd, VIEW.kbd, unit("15Y"), cache)


def test_without_the_tail_a_ruonia_bump_swings_the_long_end() -> None:
    """Why ZX: on the plain request a 10Y RUONIA bump moves the 15Y OIS zero by several bp."""
    cache = ShockedCurveCache()
    plain = bootstrap_discount_curve(OIS)
    moved = bootstrap_discount_curve(_shocked_request(OIS, unit("10Y")))
    tail_base = VIEW.ois_curve((0.0,) * N, cache)
    tail = VIEW.ois_curve(unit("10Y"), cache)
    target = add_months(DATE, 180)
    t = (target - DATE).days / 365.0

    def dz(a: DiscountCurve, b: DiscountCurve) -> float:
        moved = b.discount_factor(target) ** (-1 / t) - a.discount_factor(target) ** (-1 / t)
        return float(moved) * 1e4

    assert dz(plain, moved) > 2.5  # flat-forward extrapolation of the 9Y-10Y segment
    # With the tail the 15Y quote holds: the 15Y zero dips a little, as a 10Y par bump moves
    # the KBD's v3 overlay at 15Y (about -1.1 bp).
    assert -1.5 < dz(tail_base, tail) < 0.0


# -- (a) basis = v3 par risk -------------------------------------------------------------


@pytest.mark.parametrize("method", ["bump", "analytic"])
def test_basis_risk_is_the_v3_par_risk_to_the_bit(method: str) -> None:
    ofz = engine()
    kbd = ofz.engine
    risk = ofz.risk(method=method)  # type: ignore[arg-type]
    v3 = kbd.risk(difference="central", method=method)  # type: ignore[arg-type]
    assert risk.basis_buckets == v3.buckets
    assert risk.ofz_spr01_rub == v3.ofz_spr01_rub and risk.pv_rub == v3.pv_rub
    second = ofz.second_order(method=method)  # type: ignore[arg-type]
    own = kbd.second_order(method=method)  # type: ignore[arg-type]
    hessian = np.array(second.hessian_rub_per_bp2)
    assert second.gradient_rub_per_bp[N:] == own.gradient_rub_per_bp
    assert np.array_equal(hessian[N:, N:], np.array(own.hessian_rub_per_bp2))
    # With RUONIA held every price is the engine's own, also under a joint basis/spread move.
    move = tuple(float(i) for i in range(N))
    assert ofz.price((0.0,) * N, move, 7.0) == kbd._price(move, 7.0)


# -- (b) RUONIA against an independent rebuild; analytic against bump ---------------------


def _independent_price(
    flows: tuple[BondCashflow, ...], spread_bp: float, ruonia: tuple[float, ...]
) -> float:
    """Rebuild the extended OIS from shifted quotes by hand, compose, price."""
    quotes = (*OIS.quotes, *VIEW.synthetic_quotes)

    def build(shifts: tuple[float, ...]) -> DiscountCurve:
        moved = tuple(
            replace(
                q,
                par_rate=q.par_rate
                + 1e-4
                * math.fsum(
                    w * s
                    for w, s in zip(key_rate_weights((q.maturity - DATE).days / 365.0), shifts)
                ),
            )
            for q in quotes
        )
        return bootstrap_discount_curve(CurveBootstrapRequest(DATE, moved, OIS.convention))

    base, shocked = build((0.0,) * N), build(ruonia)
    total = 0.0
    df_s = None
    for pay, amount in ((SETTLEMENT, 0.0), *((f.payment_date, f.amount) for f in flows)):
        t = (pay - DATE).days / 365.0
        growth = (
            KBD.discount_factor(pay) ** (-1 / t)
            + shocked.discount_factor(pay) ** (-1 / t)
            - base.discount_factor(pay) ** (-1 / t)
        )
        df = growth ** (-t)
        if df_s is None:
            df_s = df
            continue
        tau = (pay - SETTLEMENT).days / 365.0
        forward = df / df_s
        total += amount * (forward ** (-1 / tau) + spread_bp * 1e-4) ** (-tau)
    return total


def test_ruonia_risk_is_an_independent_bump_and_rebuild() -> None:
    ofz = engine(spread_bp=-18.7)
    risk = ofz.risk()
    flows = semiannual_flows(LONG, 0.071)
    for i, bucket in enumerate(RISK_BUCKETS):
        up = _independent_price(flows, -18.7, unit(bucket))
        down = _independent_price(flows, -18.7, unit(bucket, -1.0))
        expected = (up - down) / 2.0 * 1000.0
        assert risk.ruonia_buckets[i].dv01_rub == pytest.approx(expected, rel=1e-9, abs=1e-9)
    joint = unit("10Y", 3.0)
    assert ofz.price(joint) == pytest.approx(_independent_price(flows, -18.7, joint), rel=1e-12)


@pytest.mark.parametrize("maturity", [LONG, date(2036, 3, 12), date(2029, 3, 14)])
def test_analytic_equals_bump_up_to_the_bump_error(maturity: date) -> None:
    ofz = engine(maturity, 0.12, quantity=-2500.0, spread_bp=41.3)
    first = ofz.risk(method="analytic")
    bumped = ofz.risk()
    for a, b in zip(first.ruonia_buckets, bumped.ruonia_buckets, strict=True):
        assert a.dv01_rub == pytest.approx(b.dv01_rub, rel=1e-5, abs=1e-6)
    # O(bump^2) apart; at 0.1 bp the bumped Hessian's own round-off (~1e-8 RUB/bp^2 on a
    # 1000 RUB price) is what is left.
    for bump, g_tol, h_tol in ((1.0, 1e-5, 1e-5), (0.1, 1e-7, 3e-6)):
        exact = ofz.second_order(bump_bp=bump, method="analytic")
        reference = ofz.second_order(bump_bp=bump)
        g1, g2 = np.array(exact.gradient_rub_per_bp), np.array(reference.gradient_rub_per_bp)
        h1, h2 = np.array(exact.hessian_rub_per_bp2), np.array(reference.hessian_rub_per_bp2)
        assert np.max(np.abs(g1 - g2)) <= g_tol * np.max(np.abs(g2))
        assert np.max(np.abs(h1 - h2)) <= h_tol * np.max(np.abs(h2))
    # First-order analytic is the second order's gradient.
    exact = ofz.second_order(method="analytic")
    assert tuple(b.dv01_rub for b in first.ruonia_buckets) == pytest.approx(
        exact.gradient_rub_per_bp[:N], rel=1e-12, abs=1e-12
    )


# -- (c) RUONIA and basis risk differ, consistently with the research ----------------------


def test_a_long_ofz_has_ruonia_and_basis_risk_that_differ() -> None:
    risk = engine().risk(method="analytic")
    ruonia = {b.bucket: b.dv01_rub for b in risk.ruonia_buckets}
    basis = {b.bucket: b.dv01_rub for b in risk.basis_buckets}
    total_r, total_b = sum(ruonia.values()), sum(basis.values())
    # Totals within 0.5 % (research ZX: 0.04-0.35 %); the main bucket a few % apart
    # (research SU26238 15Y: -406.6 against -417.9 RUB/bp per 1 mn).
    assert abs(total_r / total_b - 1.0) < 0.005
    assert -420.0 < basis["15Y"] < -395.0 and -405.0 < ruonia["15Y"] < -380.0
    assert 0.02 < abs(ruonia["15Y"] / basis["15Y"] - 1.0) < 0.08
    # RUONIA has risk at 1M-6M (OIS quotes there); the basis has none (v3 merges them into 1Y).
    assert all(basis[b] == 0.0 for b in ("1M", "3M", "6M"))
    assert ruonia["6M"] < -1.0
    l1 = sum(abs(ruonia[b] - basis[b]) for b in RISK_BUCKETS) / abs(total_b)
    assert 0.05 < l1 < 0.20
    # Between pillars the two curves split a cash flow differently (2.4Y bond: 2Y/3Y).
    short = engine(date(2029, 3, 14), 0.067).risk(method="analytic")
    r2, r3 = short.ruonia_buckets[4].dv01_rub, short.ruonia_buckets[5].dv01_rub
    b2, b3 = short.basis_buckets[4].dv01_rub, short.basis_buckets[5].dv01_rub
    assert abs(b2) > abs(b3) and abs(r3) > abs(r2)


def test_basis_view_is_the_variant_p_approximation() -> None:
    """basis_view makes RUONIA risk equal to basis risk: the computed view does not."""
    ofz = engine()
    kbd = bond_factor_risk(ofz.engine.risk(difference="central"), "RUB_OFZ_KBD")
    p = basis_view(kbd, "RUB_OIS", "RUB_OFZ_KBD", "RUB_OFZ_BASIS").first_order_by_factor()
    z = ofz_basis_factor_risk(ofz.risk(), "RUB_OIS", "RUB_OFZ_BASIS").first_order_by_factor()
    for b in RISK_BUCKETS:
        assert p[f"IR|RUB_OIS|{b}"] == p[f"IR|RUB_OFZ_BASIS|{b}"] == z[f"IR|RUB_OFZ_BASIS|{b}"]
    assert z["IR|RUB_OIS|15Y"] != z["IR|RUB_OFZ_BASIS|15Y"]


# -- (d) Taylor parity of a joint move -----------------------------------------------------


def _taylor(gradient: tuple[float, ...], hessian: np.ndarray, move: np.ndarray) -> float:
    return float(np.dot(gradient, move) + 0.5 * move @ hessian @ move)


@pytest.mark.parametrize("method", ["analytic", "bump"])
def test_second_order_taylor_explains_a_joint_ruonia_basis_spread_move(method: str) -> None:
    ofz = engine(spread_bp=-18.7)
    second = ofz.second_order(method=method)  # type: ignore[arg-type]
    hessian = np.array(second.hessian_rub_per_bp2)
    ruonia = tuple(5.0 + 0.5 * i for i in range(N))  # a steepening RUONIA move
    basis = tuple(-4.0 if i < 4 else -4.0 + 0.8 * (i - 3) for i in range(N))  # 1M-1Y alike
    spread = 3.0
    residuals = []
    for scale in (1.0, 0.5):
        r = tuple(scale * v for v in ruonia)
        b = tuple(scale * v for v in basis)
        move = np.array((*r, *b, scale * spread))
        exact = ofz.reprice(r, b, scale * spread)
        linear = float(np.dot(second.gradient_rub_per_bp, move))
        taylor = _taylor(second.gradient_rub_per_bp, hessian, move)
        # What is left is third order: under 1 % of the second-order term at 5-10 bp.
        assert abs(exact.total_pnl_rub - taylor) < 1e-2 * abs(exact.total_pnl_rub - linear)
        assert abs(exact.total_pnl_rub - taylor) < 1e-4 * abs(exact.total_pnl_rub)
        residuals.append(exact.total_pnl_rub - taylor)
        # The RUONIA x basis cross terms matter for it.
        cross = float(move[:N] @ hessian[:N, N : 2 * N] @ move[N : 2 * N])
        assert abs(cross) > 10.0 * abs(exact.total_pnl_rub - taylor)
        assert exact.total_pnl_rub == pytest.approx(
            exact.rates_pnl_rub + exact.spread_pnl_rub + exact.interaction_pnl_rub
        )
    # Halving the move divides the residual by about 2^3.
    assert 6.0 < residuals[0] / residuals[1] < 10.0


def test_joint_shifts_add_in_annual_zero() -> None:
    """The KBD zero moves by the basis overlay plus the OIS zero move, at every date."""
    cache = ShockedCurveCache()
    ruonia, basis = unit("5Y", 20.0), unit("10Y", -15.0)
    joint = VIEW.shocked(ruonia, basis, cache)
    only_basis = VIEW.shocked((0.0,) * N, basis, cache)
    only_ruonia = VIEW.shocked(ruonia, (0.0,) * N, cache)
    curves: tuple[DiscountFactorCurve, ...] = (KBD, joint, only_basis, only_ruonia)
    for years in (1, 3, 5, 7, 10, 15, 25):
        target = add_months(DATE, 12 * years)
        t = (target - DATE).days / 365.0
        z0, zj, zb, zr = (c.discount_factor(target) ** (-1 / t) for c in curves)
        assert zj - z0 == pytest.approx((zb - z0) + (zr - z0), abs=1e-15)


# -- other bonds: bump path, refusals ------------------------------------------------------


def test_a_single_curve_kbd_floater_and_a_perpetual_tail_are_bumped() -> None:
    leg = FloatingLeg(rule(), periods(), history())
    floater = BondRiskEngine(
        BondRiskRequest(
            (BondCashflow(leg.periods[-1].payment_date, 1000.0),),
            DATE,
            KBD,
            ZSpreadConvention(),
            "credit",
            spread_bp=250.0,
            floating_leg=leg,
            quantity=1000.0,
        )
    )
    ofz = OfzBasisRiskEngine(floater, VIEW)
    assert ofz.supports_analytic() is not None
    with pytest.raises(InvalidTerms, match="floating"):
        ofz.risk(method="analytic")
    risk = ofz.risk()
    assert risk.basis_buckets == floater.risk(difference="central").buckets
    # The coupon is re-projected off the composed curve: a 7Y-tenor floater keeps little RUONIA
    # risk overall, as on the KBD itself.
    total_r = sum(b.dv01_rub for b in risk.ruonia_buckets)
    total_b = sum(b.dv01_rub for b in risk.basis_buckets)
    assert abs(total_r - total_b) < 0.1 * abs(total_b) + 1.0
    flows = semiannual_flows(date(2031, 9, 17), 0.11)
    tail = PerpetualTail(flows[-2].payment_date, flows[0].amount, period_months=6)
    perpetual = BondRiskEngine(
        BondRiskRequest(
            flows[:-1],
            SETTLEMENT,
            KBD,
            ZSpreadConvention(),
            "credit",
            spread_bp=300.0,
            perpetual_tail=tail,
        )
    )
    perp = OfzBasisRiskEngine(perpetual, VIEW).risk()
    total = sum(b.dv01_rub for b in perp.ruonia_buckets)
    assert perp.ruonia_buckets[-1].dv01_rub < 0.0 and total < 0.0


def test_refusals() -> None:
    with pytest.raises(InvalidTerms, match="annual_par"):
        OfzBasisCurve(kbd_discount_curve(PARAMS, zero_shock="annual"), OIS)
    with pytest.raises(InvalidTerms, match="quotes"):
        OfzBasisCurve(KBD, bootstrap_discount_curve(OIS))  # type: ignore[arg-type]
    bounded = BusinessCalendar(
        holidays=OIS_CALENDAR.holidays,
        version="bounded",
        valid_from=date(2026, 1, 1),
        valid_to=date(2038, 12, 31),
    )
    convention = ois_curve_convention("rub_ruonia_ois_moex_v2", bounded)
    with pytest.raises(CalendarRangeError):  # the 15Y tail quote cannot resolve
        OfzBasisCurve(KBD, replace(OIS, convention=convention))
    with pytest.raises(InvalidTerms, match="same curve date"):
        OfzBasisCurve(KBD, replace(OIS, curve_date=DATE - timedelta(days=1)))
    other = BondRiskEngine(
        BondRiskRequest(
            semiannual_flows(LONG, 0.07),
            SETTLEMENT,
            kbd_discount_curve(PARAMS, zero_shock="annual_par", horizon_years=31),
            ZSpreadConvention(),
            "ofz",
            spread_bp=0.0,
        )
    )
    with pytest.raises(InvalidTerms, match="KBD of the RUONIA"):
        OfzBasisRiskEngine(other, VIEW)
    leg = FloatingLeg(rule(), periods(), history())
    projected = BondRiskEngine(
        BondRiskRequest(
            (BondCashflow(leg.periods[-1].payment_date, 1000.0),),
            DATE,
            KBD,
            ZSpreadConvention(),
            "credit",
            spread_bp=250.0,
            floating_leg=leg,
            projection_curve=kbd_discount_curve(PARAMS, zero_shock="annual"),
        )
    )
    with pytest.raises(InvalidTerms, match="projected off a curve other"):
        OfzBasisRiskEngine(projected, VIEW)
    with pytest.raises(InvalidTerms, match="1M-6M"):
        engine().reprice((0.0,) * N, unit("3M", 5.0))
    for bad in (("A", "A", "B"), ("A", "B|C", "D"), ("", "B", "C")):
        with pytest.raises(InvalidTerms):
            OfzBasisIds(*bad)
    with pytest.raises(InvalidTerms, match="distinct"):
        ofz_basis_factor_risk(engine(date(2029, 3, 14), 0.067).risk(), "X", "X")


# -- portfolio -----------------------------------------------------------------------------

AS_OF = datetime(2026, 10, 15, 16, 30, tzinfo=UTC)
IDS = OfzBasisIds("RUB_OFZ_KBD", "RUB_OIS", "RUB_OFZ_BASIS")


def _bond(instrument_id: str, maturity: date, coupon: float) -> Instrument:
    starts = []
    end = maturity
    while end > DATE:
        starts.append(end)
        end = add_months(end, -6)
    dates = [end, *sorted(starts)]
    legs = tuple(CouponPeriod(a, b, b, round(1000.0 * coupon / 2, 2)) for a, b in pairwise(dates))
    return Instrument(
        instrument_id,
        ProductType.FIXED_BOND,
        "RUB",
        1000.0,
        FixedCouponLeg(legs),
        PrincipalLeg((Redemption(maturity, 1000.0),)),
        "MOEX_STOCK",
    )


def _swap() -> SwapInstrument:
    from portfolio_v3_book import ois_swap_convention

    from rub_fixed_income import InterestRateSwap

    swap = InterestRateSwap.from_tenor(
        swap_id="OIS-10Y",
        side="payer",
        notional=100_000_000.0,
        fixed_rate=0.156,
        trade_date=DATE,
        tenor="10Y",
        calendar=OIS_CALENDAR,
        convention=ois_swap_convention(),
    )
    return SwapInstrument("OIS-10Y", swap, "MOEX", "RUB_OIS")


def _run(
    policy: ValuationPolicy,
    scenarios: tuple[PortfolioScenario, ...] = (),
    *,
    kbd_spec: str = "rub_ofz_kbd_v3",
    ois: object = OIS,
    swap: bool = False,
) -> PortfolioRiskResult:
    instruments: list[PortfolioInstrument] = [
        _bond("SU-LONG", LONG, 0.071),
        _bond("SU-MID", date(2036, 3, 12), 0.12),
        _bond("SU-SHORT", date(2029, 3, 14), 0.067),
    ]
    positions = [
        Position("p-long", "SU-LONG", 1000.0, "RATES", AccountingCategory.TRADING, "RUB"),
        Position("p-mid", "SU-MID", -400.0, "RATES", AccountingCategory.TRADING, "RUB"),
        Position("p-short", "SU-SHORT", 700.0, "RATES", AccountingCategory.TRADING, "RUB"),
        Position("p-short-2", "SU-SHORT", 300.0, "RATES", AccountingCategory.TRADING, "RUB"),
    ]
    if swap:
        instruments.append(_swap())
        positions.append(
            Position("p-swap", "OIS-10Y", 1.0, "RATES", AccountingCategory.TRADING, "RUB")
        )
    snapshot = MarketSnapshot(
        "synthetic-2026-10-15",
        DATE,
        AS_OF,
        curves=(
            CurveEntry("RUB_OFZ_KBD", kbd_spec, PARAMS),
            CurveEntry("RUB_OIS", "rub_ruonia_ois_moex_v2", ois),  # type: ignore[arg-type]
        ),
        calendars=(
            CalendarEntry("MOEX_STOCK", OIS_CALENDAR),
            CalendarEntry("MOEX", OIS_CALENDAR),
        ),
        quotes=(
            BondPriceObservation("SU-LONG", 52.0, "waprice"),
            BondPriceObservation("SU-MID", 81.5, "waprice"),
            BondPriceObservation("SU-SHORT", 86.0, "waprice"),
        ),
    )
    return value_portfolio(
        run_id="ofz-basis",
        as_of=AS_OF,
        snapshot=snapshot,
        instruments=instruments,
        positions=positions,
        policy=policy,
        scenarios=scenarios,
    )


def _policy(**kw: object) -> ValuationPolicy:
    args: dict[str, object] = {
        "discount_curve_id": "RUB_OFZ_KBD",
        "spread_kind": "ofz",
    }
    args.update(kw)
    return ValuationPolicy(**args)  # type: ignore[arg-type]


def test_policy_field_is_omitted_at_its_default_and_round_trips() -> None:
    plain = _policy()
    assert "ofz_basis" not in to_dict(plain)
    opted = _policy(ofz_basis=IDS)
    assert to_dict(opted)["ofz_basis"] == {
        "kbd_curve_id": "RUB_OFZ_KBD",
        "ois_curve_id": "RUB_OIS",
        "basis_id": "RUB_OFZ_BASIS",
    }
    assert from_dict(ValuationPolicy, to_dict(opted)) == opted
    assert canonical_sha256(plain) != canonical_sha256(opted)
    with pytest.raises(TypeError, match="OfzBasisIds"):
        _policy(ofz_basis=("RUB_OFZ_KBD", "RUB_OIS", "B"))


@pytest.mark.parametrize("analytic", [False, True])
def test_value_portfolio_reports_kbd_bonds_as_ruonia_plus_basis(analytic: bool) -> None:
    methods = {"risk_method": "analytic", "second_order_method": "analytic"} if analytic else {}
    plain = _run(_policy(**methods))
    opted = _run(_policy(ofz_basis=IDS, **methods))
    assert plain.status == opted.status == "complete"
    for old, new in zip(plain.positions, opted.positions, strict=True):
        assert new.pv_rub == old.pv_rub and new.spread_bp == old.spread_bp
        assert new.curve_id == "RUB_OFZ_KBD" and new.curve_basis == OFZ_BASIS_CURVE_BASIS
        risk = new.factor_risk
        assert risk.factors == (
            *(f"IR|RUB_OIS|{b}" for b in RISK_BUCKETS),
            *(f"IR|RUB_OFZ_BASIS|{b}" for b in RISK_BUCKETS),
            "CS",
        )
        assert risk.has_second_order and len(risk.hessian) > len(old.factor_risk.hessian)
        before = old.factor_risk.first_order_by_factor()
        after = risk.first_order_by_factor()
        for b in RISK_BUCKETS:
            assert after[f"IR|RUB_OFZ_BASIS|{b}"] == before[f"IR|RUB_OFZ_KBD|{b}"]
        assert after["CS"] == before["CS"]
        total_r = sum(after[f"IR|RUB_OIS|{b}"] for b in RISK_BUCKETS)
        total_b = sum(after[f"IR|RUB_OFZ_BASIS|{b}"] for b in RISK_BUCKETS)
        assert total_r == pytest.approx(total_b, rel=0.01)
        # The basis block of the Hessian is the KBD Hessian.
        old_h = old.factor_risk.hessian_by_factor()
        new_h = risk.hessian_by_factor()
        for (f, g), value in old_h.items():
            rename = {
                f: f.replace("RUB_OFZ_KBD", "RUB_OFZ_BASIS"),
                g: g.replace("RUB_OFZ_KBD", "RUB_OFZ_BASIS"),
            }
            assert new_h[(rename[f], rename[g])] == value
    totals = {t.factor for t in opted.factor_totals}
    assert "IR|RUB_OIS|15Y" in totals and not any(f.startswith("IR|RUB_OFZ_KBD|") for f in totals)


def test_bonds_on_other_curves_are_unchanged() -> None:
    policy = _policy(curve_overrides=(("SU-SHORT", "RUB_OIS"),))
    plain = _run(policy)
    opted = _run(replace(policy, ofz_basis=IDS))
    short_old = [p for p in plain.positions if p.instrument_id == "SU-SHORT"]
    short_new = [p for p in opted.positions if p.instrument_id == "SU-SHORT"]
    for old, new in zip(short_old, short_new, strict=True):
        assert new.curve_id == "RUB_OIS"
        assert new.factor_risk == old.factor_risk and new.curve_basis == old.curve_basis


def test_ruonia_factors_add_up_with_an_ois_swap() -> None:
    result = _run(_policy(ofz_basis=IDS, include_second_order=False), swap=True)
    assert isinstance(result, PortfolioRiskResultV3)
    assert result.status == "complete"
    swap = result.swaps[0].factor_risk.first_order_by_factor()
    bonds = [p.factor_risk.first_order_by_factor() for p in result.positions]
    totals = {t.factor: t.first_order_rub for t in result.factor_totals}
    for b in RISK_BUCKETS:
        key = f"IR|RUB_OIS|{b}"
        assert totals[key] == pytest.approx(swap.get(key, 0.0) + sum(r[key] for r in bonds))
    # A payer swap gains when RUONIA rises; a long OFZ loses.
    assert swap["IR|RUB_OIS|10Y"] > 0 > sum(bonds[0][f"IR|RUB_OIS|{b}"] for b in RISK_BUCKETS)


def _portfolio_taylor(risk: FactorRisk, moves: dict[str, float]) -> float:
    x = [moves.get(f, 0.0) for f in risk.factors]
    total = math.fsum(g * m for g, m in zip(risk.first_order, x, strict=True))
    for e in risk.hessian:
        total += (0.5 if e.i == e.j else 1.0) * e.value * x[e.i] * x[e.j]
    return total


def test_portfolio_scenarios_move_ruonia_and_the_named_basis() -> None:
    ruonia = tuple(4.0 + 0.4 * i for i in range(N))
    basis = (-3.0,) * 4 + tuple(-3.0 + 0.6 * i for i in range(1, N - 3))
    scenarios = (
        PortfolioScenario(
            "joint",
            BondScenario.parallel(0.0, spread_shift_bp=2.0),
            curve_shifts_bp=(("RUB_OIS", ruonia), ("RUB_OFZ_BASIS", basis)),
        ),
        PortfolioScenario("parallel", BondScenario.parallel(10.0)),
        PortfolioScenario(
            "kbd", BondScenario.parallel(0.0), curve_shifts_bp=(("RUB_OFZ_KBD", (5.0,) * N),)
        ),
    )
    result = _run(_policy(ofz_basis=IDS, second_order_method="analytic"), scenarios)
    joint, parallel, kbd = result.scenarios
    assert joint.coverage.status == "complete" and parallel.coverage.status == "complete"
    moves = {f"IR|RUB_OIS|{b}": r for b, r in zip(RISK_BUCKETS, ruonia, strict=True)}
    moves.update({f"IR|RUB_OFZ_BASIS|{b}": s for b, s in zip(RISK_BUCKETS, basis, strict=True)})
    moves["CS"] = 2.0
    for row, pnl in zip(result.positions, joint.positions, strict=True):
        assert pnl.pnl_rub is not None
        taylor = _portfolio_taylor(row.factor_risk, moves)
        linear = math.fsum(
            g * moves.get(f, 0.0)
            for f, g in zip(row.factor_risk.factors, row.factor_risk.first_order)
        )
        assert abs(pnl.pnl_rub - taylor) < 1e-2 * abs(pnl.pnl_rub - linear)
        assert abs(pnl.pnl_rub - taylor) < 1e-4 * abs(pnl.pnl_rub)
    # An unnamed shock moves RUONIA (every curve moves) with the basis held.
    up10 = {f"IR|RUB_OIS|{b}": 10.0 for b in RISK_BUCKETS}
    for row, pnl in zip(result.positions, parallel.positions, strict=True):
        assert pnl.pnl_rub == pytest.approx(_portfolio_taylor(row.factor_risk, up10), rel=1e-4)
    # The KBD is not a factor of these bonds: naming it is refused.
    assert kbd.coverage.status == "failed"
    assert {r.code for r in kbd.coverage.rejections} == {"INVALID_TERMS"}


def test_portfolio_refusals_are_per_position() -> None:
    v1 = _run(_policy(ofz_basis=IDS), kbd_spec="rub_ofz_kbd_v2")
    assert v1.status == "failed"
    assert {r.code for r in v1.coverage.rejections} == {"INVALID_TERMS"}
    assert "annual_par" in v1.coverage.rejections[0].message
    built = _run(_policy(ofz_basis=IDS), ois=bootstrap_discount_curve(OIS))
    assert built.status == "failed" and "quotes" in built.coverage.rejections[0].message
