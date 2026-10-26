"""A KBD floater discounted on RUONIA OIS in RUONIA + basis coordinates (issue #90).

The mirror of the OFZ case (issue #83, ``test_ofz_basis.py``): a floater whose coupons project off
the v3 KBD (``ZR_YLD_CRV``) and which is discounted on the RUONIA OIS quotes plus its Z-spread. A
RUONIA bucket moves the discount curve (the plain OIS request rebootstrapped) and the projection
(the composed KBD: base + v3 overlay + the extended OIS zero move); a basis bucket moves the
projection only, as a v3 par bucket of the KBD.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from datetime import date, timedelta

import numpy as np
import pytest
from ofz_basis_data import DATE, KBD, OIS, OIS_CALENDAR, PARAMS, SETTLEMENT, ois_request
from test_floating_kbd import history, periods, rule
from test_ofz_basis import AS_OF, IDS, _bond, _portfolio_taylor
from test_portfolio_floaters import KEY_PROJECTION, KNOWN, floater, key_rate, ruonia

from rub_fixed_income import (
    RISK_BUCKETS,
    AccountingCategory,
    BondCashflow,
    BondPriceObservation,
    BondRiskEngine,
    BondRiskRequest,
    BondScenario,
    CalendarEntry,
    CurveBootstrapRequest,
    CurveEntry,
    DayCount,
    DiscountCurve,
    FixingDateRule,
    FixingEntry,
    FixingHistory,
    FloatingCouponLeg,
    FloatingCouponPeriod,
    FloatingCouponRule,
    FloatingLeg,
    Instrument,
    InvalidTerms,
    MarketSnapshot,
    OfzBasisCurve,
    OfzBasisRiskEngine,
    OisQuote,
    PortfolioRiskResult,
    PortfolioScenario,
    Position,
    PositionValuation,
    PrincipalLeg,
    ProductType,
    Redemption,
    ShockedCurveCache,
    TwoCurveBondRiskResult,
    ValuationPolicy,
    ZSpreadConvention,
    bond_factor_risk,
    bootstrap_discount_curve,
    kbd_discount_curve,
    ofz_basis_factor_risk,
    value_portfolio,
)
from rub_fixed_income._risk_common import _shock_curve
from rub_fixed_income.discount import DiscountFactorCurve
from rub_fixed_income.ofz_basis import OFZ_BASIS_CURVE_BASIS
from rub_fixed_income.risk import key_rate_weights

VIEW = OfzBasisCurve(KBD, OIS)
N = len(RISK_BUCKETS)
ZERO = (0.0,) * N


def unit(bucket: str, size: float = 1.0) -> tuple[float, ...]:
    return tuple(size if b == bucket else 0.0 for b in RISK_BUCKETS)


def ois_floater(
    *,
    discount: CurveBootstrapRequest = OIS,
    projection: DiscountCurve = KBD,
    spread_bp: float = 250.0,
) -> BondRiskEngine:
    """A 7-year KBD 7Y + 0.5 % floater (14 semiannual coupons), 1 mn RUB face."""
    leg = FloatingLeg(rule(), periods(), history())
    return BondRiskEngine(
        BondRiskRequest(
            (BondCashflow(leg.periods[-1].payment_date, 1000.0),),
            SETTLEMENT,
            discount,
            ZSpreadConvention(),
            "credit",
            spread_bp=spread_bp,
            floating_leg=leg,
            projection_curve=projection,
            quantity=1000.0,
        )
    )


# -- an independent revaluation ------------------------------------------------------------


def _rebuild(quotes: tuple[OisQuote, ...], shifts: tuple[float, ...]) -> DiscountCurve:
    """Bootstrap the OIS curve by hand from quotes moved by the bucket hats at maturity."""
    moved = tuple(
        replace(
            q,
            par_rate=q.par_rate
            + 1e-4
            * math.fsum(
                w * s
                for w, s in zip(
                    key_rate_weights((q.maturity - DATE).days / 365.0),
                    shifts,
                    strict=True,
                )
            ),
        )
        for q in quotes
    )
    return bootstrap_discount_curve(CurveBootstrapRequest(DATE, moved, OIS.convention))


@dataclass(frozen=True)
class _Composed:
    """The KBD (a v3 view) plus the annual-zero move of a rebuilt extended OIS curve."""

    kbd: DiscountFactorCurve
    base: DiscountCurve
    shocked: DiscountCurve

    @property
    def curve_date(self) -> date:
        return DATE

    def discount_factor(self, target: date) -> float:
        t = (target - DATE).days / 365.0
        kbd = self.kbd.discount_factor(target)
        if t <= 0.0:
            return float(kbd)
        growth = (
            kbd ** (-1 / t)
            + self.shocked.discount_factor(target) ** (-1 / t)
            - self.base.discount_factor(target) ** (-1 / t)
        )
        return float(growth ** (-t))


def _independent(
    engine: BondRiskEngine,
    ruonia_shifts: tuple[float, ...] = ZERO,
    basis_shifts: tuple[float, ...] = ZERO,
    spread_shift: float = 0.0,
    *,
    view: OfzBasisCurve = VIEW,
) -> float:
    """Dirty price per bond, every curve rebuilt by hand: the plain OIS for the discount, the
    extended OIS for the composed KBD that projects the coupons."""
    req = engine.request
    leg = req.floating_leg
    assert leg is not None
    extended = (*view.ois.quotes, *view.synthetic_quotes)
    discount = _rebuild(view.ois.quotes, ruonia_shifts)
    kbd = _shock_curve(KBD, KBD, basis_shifts, ShockedCurveCache())  # the basis is a v3 shift
    projection = _Composed(kbd, _rebuild(extended, ZERO), _rebuild(extended, ruonia_shifts))
    coupons = [
        leg.coupon(i, projection, check_guard=False) for i in leg.remaining(req.settlement_date)
    ]
    flows = [*((f.payment_date, f.amount) for f in req.cashflows)]
    flows += [(c.payment_date, c.amount_rub) for c in coupons]
    settle = req.settlement_date
    df_s = discount.discount_factor(settle)
    spread = (engine.spread_bp + spread_shift) * 1e-4
    total = 0.0
    for pay, amount in flows:
        if pay <= settle:
            continue
        tau = (pay - settle).days / 365.0
        forward = discount.discount_factor(pay) / df_s
        total += amount * (forward ** (-1 / tau) + spread) ** (-tau)
    return total


# -- the engine ------------------------------------------------------------------------------


def test_basis_factors_are_the_plain_kbd_projection_factors() -> None:
    plain = ois_floater()
    ofz = OfzBasisRiskEngine(plain, VIEW)
    assert ofz.discounted_on == "ois"
    own = plain.risk(difference="central")
    assert isinstance(own, TwoCurveBondRiskResult)
    risk = ofz.risk()
    assert risk.curve_basis == OFZ_BASIS_CURVE_BASIS
    # The same repricings: equal to the bit (the issue asks for 1e-9 relative).
    assert risk.basis_buckets == own.projection_buckets
    for mine, theirs in zip(risk.basis_buckets, own.projection_buckets, strict=True):
        assert mine.dv01_rub == pytest.approx(theirs.dv01_rub, rel=1e-9, abs=1e-12)
    assert risk.cs01_rub == own.cs01_rub and risk.pv_rub == own.pv_rub
    assert all(b.dv01_rub == 0.0 for b in risk.basis_buckets[:3])  # v3: no instrument below 1Y
    # RUONIA carries both legs: about the plain discount plus projection totals.
    total_r = math.fsum(b.dv01_rub for b in risk.ruonia_buckets)
    plain_total = math.fsum(b.dv01_rub for b in (*own.buckets, *own.projection_buckets))
    assert total_r == pytest.approx(plain_total, rel=0.1)
    # ... and differs from the discount alone (the projection's RUONIA leg is real).
    discount_total = math.fsum(b.dv01_rub for b in own.buckets)
    assert abs(total_r - discount_total) > 0.5 * abs(discount_total)
    # The Hessian: basis and spread block = the engine's projection and spread block.
    second = ofz.second_order()
    two = plain.second_order()
    h, p = np.array(second.hessian_rub_per_bp2), np.array(two.hessian_rub_per_bp2)
    assert np.array_equal(h[N:, N:], p[N:, N:])
    assert second.gradient_rub_per_bp[N:] == two.gradient_rub_per_bp[N:]
    assert second.factors[:N] == tuple(f"ruonia {b}" for b in RISK_BUCKETS)
    factors = ofz_basis_factor_risk(risk, "RUB_OIS", "RUB_OFZ_BASIS", second)
    kbd = bond_factor_risk(own, "RUB_OIS", two, projection_curve_id="RUB_OFZ_KBD")
    old = kbd.first_order_by_factor()
    for b in RISK_BUCKETS:
        assert (
            factors.first_order_by_factor()[f"IR|RUB_OFZ_BASIS|{b}"] == old[f"IR|RUB_OFZ_KBD|{b}"]
        )


def test_ruonia_factors_are_a_full_rebuild_of_discount_and_projection() -> None:
    ofz = OfzBasisRiskEngine(ois_floater(spread_bp=180.0), VIEW)
    eng = ofz.engine
    assert _independent(eng) == pytest.approx(eng.base_price_rub, rel=1e-12)
    risk = ofz.risk()
    for i, bucket in enumerate(RISK_BUCKETS):
        up = _independent(eng, unit(bucket))
        down = _independent(eng, unit(bucket, -1.0))
        expected = (up - down) / 2.0 * 1000.0
        assert risk.ruonia_buckets[i].dv01_rub == pytest.approx(expected, rel=1e-9, abs=1e-9)
    # Joint prices too, with the basis and the spread.
    move = tuple(2.0 + 0.3 * i for i in range(N))
    basis = (-3.0,) * 4 + tuple(-3.0 + 0.5 * i for i in range(1, N - 3))
    assert ofz.price(move, basis, 4.0) == pytest.approx(
        _independent(eng, move, basis, 4.0), rel=1e-12
    )
    # The long buckets reach the projection only through the ZX tail (the plain request has
    # no 15Y quote): the discount does not move there, the projection does.
    plain = eng.risk(difference="central")
    assert plain.buckets[RISK_BUCKETS.index("15Y")].dv01_rub == 0.0
    assert abs(risk.ruonia_buckets[RISK_BUCKETS.index("15Y")].dv01_rub) > 10.0


def test_the_discount_reads_the_plain_request_and_shares_the_ois_cache() -> None:
    """The discount leg is the plain OIS quotes rebootstrapped, the curves an OIS swap reads; the
    projection's OIS zero comes from the extended request. Without added tenors they coincide."""
    cache = ShockedCurveCache()
    view = OfzBasisCurve(KBD, OIS)  # its own memo of composed views
    eng = BondRiskEngine(ois_floater().request, cache)
    OfzBasisRiskEngine(eng, view).price(unit("5Y"))
    assert view.extended != OIS
    assert cache.bootstrap(OIS, unit("5Y"), lambda: pytest.fail("not cached")) is not None
    assert cache.bootstrap(view.extended, unit("5Y"), lambda: pytest.fail("not cached"))
    assert len(cache) == 4  # the base and the shocked curve of each request
    # Quotes to 20Y: the extended request is the plain one; both legs read one shocked curve.
    long = ois_request((*(f"{y}Y" for y in range(1, 11)), "15Y", "20Y"))
    full = OfzBasisCurve(KBD, long)
    assert full.extended is full.ois
    shared = ShockedCurveCache()
    eng = BondRiskEngine(ois_floater(discount=long).request, shared)
    OfzBasisRiskEngine(eng, full).price(unit("10Y"))
    assert len(shared) == 2  # the base and one shocked curve, for both legs
    risk = OfzBasisRiskEngine(eng, full).risk()
    up = _independent(eng, unit("10Y"), view=full)
    down = _independent(eng, unit("10Y", -1.0), view=full)
    assert risk.ruonia_buckets[RISK_BUCKETS.index("10Y")].dv01_rub == pytest.approx(
        (up - down) / 2.0 * 1000.0, rel=1e-9
    )


def test_parallel_moves_sum_the_factors() -> None:
    ofz = OfzBasisRiskEngine(ois_floater(), VIEW)
    risk = ofz.risk()
    q = ofz.engine.request.quantity
    ones = (1.0,) * N
    # RUONIA everywhere (discount and projection), the basis held: the RUONIA sum.
    parallel = (ofz.price(ones) - ofz.price(tuple(-v for v in ones))) / 2.0 * q
    total_r = math.fsum(b.dv01_rub for b in risk.ruonia_buckets)
    # Up to the O(bump^2) terms of the joint move; the total nets two legs of ~400 RUB/bp.
    gross = math.fsum(abs(b.dv01_rub) for b in (*risk.ruonia_buckets, *risk.basis_buckets))
    assert total_r == pytest.approx(parallel, abs=2e-6 * gross)
    full = _independent(ofz.engine, ones) - _independent(ofz.engine, tuple(-v for v in ones))
    assert parallel == pytest.approx(full / 2.0 * q, rel=1e-9)
    # Every rate (RUONIA and the basis) by 1 bp: the sum over both curves.
    every = ofz.price(ones, ones) - ofz.price(tuple(-v for v in ones), tuple(-v for v in ones))
    assert risk.dv01_rub == pytest.approx(every / 2.0 * q, abs=2e-6 * gross)


def test_a_joint_scenario_is_a_full_revaluation_and_taylor_explains_it() -> None:
    ofz = OfzBasisRiskEngine(ois_floater(), VIEW)
    eng = ofz.engine
    r = tuple(5.0 + 0.5 * i for i in range(N))
    b = tuple(-4.0 if i < 4 else -4.0 + 0.8 * (i - 3) for i in range(N))
    result = ofz.reprice(r, b, 3.0)
    expected = (_independent(eng, r, b, 3.0) - eng.base_price_rub) * 1000.0
    assert result.total_pnl_rub == pytest.approx(expected, rel=1e-9)
    # A projection RUONIA move of its own (a scenario's projection shift).
    other = tuple(1.0 for _ in range(N))
    split = ofz.reprice(r, b, 0.0, other)
    assert split.total_pnl_rub != ofz.reprice(r, b, 0.0).total_pnl_rub
    second = ofz.second_order()
    move = np.array((*r, *b, 3.0))
    taylor = float(
        np.dot(second.gradient_rub_per_bp, move)
        + 0.5 * move @ np.array(second.hessian_rub_per_bp2) @ move
    )
    assert abs(result.total_pnl_rub - taylor) < 1e-3 * abs(result.total_pnl_rub)
    with pytest.raises(InvalidTerms, match="1M-6M"):
        ofz.reprice(ZERO, unit("3M", 5.0))


def test_other_floaters_are_refused_as_before() -> None:
    with pytest.raises(InvalidTerms, match="closed form"):
        OfzBasisRiskEngine(ois_floater(), VIEW).risk(method="analytic")
    # On the OIS quotes but projected off another KBD, or a fixed bond on them: refused.
    other = kbd_discount_curve(PARAMS, zero_shock="annual_par", horizon_years=31)
    with pytest.raises(InvalidTerms, match="floater projected off the KBD"):
        OfzBasisRiskEngine(ois_floater(projection=other), VIEW)
    fixed = BondRiskEngine(
        BondRiskRequest(
            (BondCashflow(date(2030, 1, 15), 1000.0),),
            SETTLEMENT,
            OIS,
            ZSpreadConvention(),
            "credit",
            spread_bp=100.0,
        )
    )
    with pytest.raises(InvalidTerms, match="floater projected off the KBD"):
        OfzBasisRiskEngine(fixed, VIEW)
    # Discounted on other OIS quotes: neither the KBD nor the view's OIS.
    shifted = ois_request(("1Y", "2Y", "5Y", "10Y"))
    with pytest.raises(InvalidTerms, match="KBD of the RUONIA"):
        OfzBasisRiskEngine(ois_floater(discount=shifted), VIEW)


# -- the portfolio ---------------------------------------------------------------------------


def _kbd_floater(instrument_id: str, projection: str) -> Instrument:
    rule = FloatingCouponRule(
        "kbd",
        "single_fixing",
        0.005,
        DayCount.ACT_365_FIXED,
        DayCount.ACT_365_FIXED,
        rate_rounding_decimals=2,
        amount_rounding_decimals=2,
        fixing_date=FixingDateRule("accrual_start", 5, "business_days"),
        index_tenor_years=7.0,
    )
    first = date(2026, 10, 23)
    legs = tuple(
        FloatingCouponPeriod(
            first + timedelta(days=182 * k),
            first + timedelta(days=182 * (k + 1)),
            first + timedelta(days=182 * (k + 1)),
            1000.0,
        )
        for k in range(14)
    )
    return Instrument(
        instrument_id,
        ProductType.FLOATING_BOND,
        "RUB",
        1000.0,
        FloatingCouponLeg(rule, legs, "KBD_7Y", projection),
        PrincipalLeg((Redemption(legs[-1].payment_date, 1000.0),)),
        "MOEX_STOCK",
    )


FLOATERS = ("FRN-KBD", "FRN-ALT", "FRN-KEY", "FRN-RUO")


def _snapshot() -> MarketSnapshot:
    kbd_history = FixingHistory(
        "kbd", DATE, (), OIS_CALENDAR, "hub-zcyc", known_at_utc=KNOWN, tenor_years=7.0
    )
    return MarketSnapshot(
        "synthetic-2026-10-15-floaters",
        DATE,
        AS_OF,
        curves=(
            CurveEntry("RUB_OFZ_KBD", "rub_ofz_kbd_v3", PARAMS),
            CurveEntry("RUB_KBD_ALT", "rub_ofz_kbd_v3", PARAMS),
            CurveEntry("RUB_OIS", "rub_ruonia_ois_moex_v2", OIS),
            CurveEntry("KEY_PROJ", "rub_key_rate_projection_v1", KEY_PROJECTION),
        ),
        calendars=(CalendarEntry("MOEX_STOCK", OIS_CALENDAR),),
        fixings=(
            FixingEntry("KBD_7Y", kbd_history),
            FixingEntry("KEY_RATE", key_rate()),
            FixingEntry("RUONIA", ruonia()),
        ),
        quotes=(
            BondPriceObservation("SU-LONG", 52.0, "waprice"),
            BondPriceObservation("FRN-KBD", 100.3, "waprice", accrued_per_bond=0.0),
            BondPriceObservation("FRN-ALT", 100.3, "waprice", accrued_per_bond=0.0),
            BondPriceObservation("FRN-KEY", 99.6, "waprice", accrued_per_bond=5.3),
            BondPriceObservation("FRN-RUO", 99.6, "waprice", accrued_per_bond=5.3),
        ),
    )


def _instruments() -> tuple[Instrument, ...]:
    return (
        _bond("SU-LONG", date(2041, 5, 15), 0.071),
        _kbd_floater("FRN-KBD", "RUB_OFZ_KBD"),
        _kbd_floater("FRN-ALT", "RUB_KBD_ALT"),
        floater("FRN-KEY", "KEY_PROJ", index="key_rate", fixing_id="KEY_RATE"),
        floater("FRN-RUO", "RUB_OIS"),
    )


def _positions(floaters: bool = True) -> tuple[Position, ...]:
    rows = [("SU-LONG", 1000.0)]
    if floaters:
        rows += [("FRN-KBD", 1000.0), ("FRN-ALT", -300.0), ("FRN-KEY", 200.0), ("FRN-RUO", 50.0)]
    return tuple(
        Position(f"p-{i}", i, q, "RATES", AccountingCategory.TRADING, "RUB") for i, q in rows
    )


def _floater_policy(**kw: object) -> ValuationPolicy:
    args: dict[str, object] = {
        "discount_curve_id": "RUB_OFZ_KBD",
        "spread_kind": "ofz",
        "curve_overrides": tuple((i, "RUB_OIS") for i in FLOATERS),
        "spread_kinds": tuple((i, "credit") for i in FLOATERS),
    }
    args.update(kw)
    return ValuationPolicy(**args)  # type: ignore[arg-type]


def _value(
    policy: ValuationPolicy,
    scenarios: tuple[PortfolioScenario, ...] = (),
    *,
    floaters: bool = True,
) -> PortfolioRiskResult:
    return value_portfolio(
        run_id="ofz-basis-floater",
        as_of=AS_OF,
        snapshot=_snapshot(),
        instruments=_instruments(),
        positions=_positions(floaters),
        policy=policy,
        scenarios=scenarios,
    )


def _rows(result: PortfolioRiskResult) -> dict[str, PositionValuation]:
    return {p.instrument_id: p for p in result.positions}


def _numbers(row: PositionValuation) -> PositionValuation:
    """A row without its calculation ID (that hashes the job, which differs by policy)."""
    return replace(row, calculation_id="")


def test_value_portfolio_reports_the_kbd_floater_as_ruonia_plus_basis() -> None:
    plain = _value(_floater_policy())
    opted = _value(_floater_policy(ofz_basis=IDS))
    assert plain.status == opted.status == "complete", opted.coverage.rejections
    old, new = _rows(plain)["FRN-KBD"], _rows(opted)["FRN-KBD"]
    assert new.curve_id == "RUB_OIS" and new.curve_basis == OFZ_BASIS_CURVE_BASIS
    assert new.pv_rub == old.pv_rub and new.spread_bp == old.spread_bp
    risk = new.factor_risk
    assert risk.factors == (
        *(f"IR|RUB_OIS|{b}" for b in RISK_BUCKETS),
        *(f"IR|RUB_OFZ_BASIS|{b}" for b in RISK_BUCKETS),
        "CS",
    )
    before = old.factor_risk.first_order_by_factor()
    after = risk.first_order_by_factor()
    for b in RISK_BUCKETS:
        assert after[f"IR|RUB_OFZ_BASIS|{b}"] == pytest.approx(
            before[f"IR|RUB_OFZ_KBD|{b}"], rel=1e-9, abs=1e-12
        )
    assert after["CS"] == before["CS"]
    old_h = old.factor_risk.hessian_by_factor()
    new_h = risk.hessian_by_factor()
    for (f, g), value in old_h.items():
        if f.startswith("IR|RUB_OIS|") or g.startswith("IR|RUB_OIS|"):
            continue  # the discount buckets are inside the RUONIA ones now
        key = (f.replace("RUB_OFZ_KBD", "RUB_OFZ_BASIS"), g.replace("RUB_OFZ_KBD", "RUB_OFZ_BASIS"))
        assert new_h[key] == value
    # Every other row is the 0.39 row: the OFZ (already RUONIA + basis), the floater on another
    # KBD, the key-rate and the RUONIA floaters.
    reference = _value(_floater_policy(ofz_basis=IDS), floaters=False)
    assert _numbers(_rows(opted)["SU-LONG"]) == _numbers(_rows(reference)["SU-LONG"])
    for instrument in ("FRN-ALT", "FRN-KEY", "FRN-RUO"):
        assert _numbers(_rows(opted)[instrument]) == _numbers(_rows(plain)[instrument])
    alt = _rows(opted)["FRN-ALT"].factor_risk.factors
    assert alt[N] == "IR|RUB_KBD_ALT|1M"
    totals = {t.factor for t in opted.factor_totals}
    assert not any(f.startswith("IR|RUB_OFZ_KBD|") for f in totals)


def test_portfolio_floater_matches_a_full_revaluation() -> None:
    """The row's RUONIA factors and scenario P&L against an independent rebuild of the bond."""
    scenarios = (
        PortfolioScenario(
            "joint",
            BondScenario.parallel(0.0, spread_shift_bp=2.0),
            curve_shifts_bp=(
                ("RUB_OIS", tuple(4.0 + 0.4 * i for i in range(N))),
                ("RUB_OFZ_BASIS", (-3.0,) * 4 + tuple(-3.0 + 0.6 * i for i in range(1, N - 3))),
            ),
        ),
        PortfolioScenario("parallel", BondScenario.parallel(10.0)),
        PortfolioScenario(
            "basis", BondScenario.parallel(0.0), curve_shifts_bp=(("RUB_OFZ_BASIS", (6.0,) * N),)
        ),
        PortfolioScenario(
            "kbd", BondScenario.parallel(0.0), curve_shifts_bp=(("RUB_OFZ_KBD", (5.0,) * N),)
        ),
    )
    opted = _value(_floater_policy(ofz_basis=IDS), scenarios)
    row = _rows(opted)["FRN-KBD"]
    snapshot = _snapshot()
    instrument = _instruments()[1]
    settlement = row.settlement_date
    engine = BondRiskEngine(
        BondRiskRequest(
            tuple(
                BondCashflow(f.payment_date, f.amount)
                for f in instrument.cashflows(after=settlement, horizon=row.horizon_date)
            ),
            settlement,
            OIS,
            ZSpreadConvention(),
            "credit",
            spread_bp=row.spread_bp,
            floating_leg=instrument.floating_leg(snapshot, horizon=row.horizon_date),
            projection_curve=KBD,
            quantity=1000.0,
        )
    )
    base = _independent(engine)
    assert base * 1000.0 == pytest.approx(row.pv_rub, rel=1e-12)
    first = row.factor_risk.first_order_by_factor()
    for bucket in RISK_BUCKETS:
        up = _independent(engine, unit(bucket))
        down = _independent(engine, unit(bucket, -1.0))
        assert first[f"IR|RUB_OIS|{bucket}"] == pytest.approx(
            (up - down) / 2.0 * 1000.0, rel=1e-9, abs=1e-9
        )
    joint, parallel, basis, kbd = opted.scenarios
    by_id = {s.name: {p.position_id: p for p in s.positions} for s in opted.scenarios}
    shifts = dict(scenarios[0].curve_shifts_bp)
    expected = _independent(engine, shifts["RUB_OIS"], shifts["RUB_OFZ_BASIS"], 2.0) - base
    assert by_id["joint"]["p-FRN-KBD"].pnl_rub == pytest.approx(expected * 1000.0, rel=1e-9)
    # An unnamed shock moves RUONIA (both legs), the basis held.
    tens = (10.0,) * N
    expected = _independent(engine, tens) - base
    assert by_id["parallel"]["p-FRN-KBD"].pnl_rub == pytest.approx(expected * 1000.0, rel=1e-9)
    taylor = _portfolio_taylor(row.factor_risk, {f"IR|RUB_OIS|{b}": 10.0 for b in RISK_BUCKETS})
    assert by_id["parallel"]["p-FRN-KBD"].pnl_rub == pytest.approx(taylor, rel=1e-3)
    # The basis alone is the plain run's KBD projection shift, to the bit.
    plain = _value(
        _floater_policy(),
        (
            PortfolioScenario(
                "basis",
                BondScenario.parallel(0.0),
                curve_shifts_bp=(("RUB_OFZ_KBD", (6.0,) * N),),
            ),
        ),
    )
    plain_pnl = {p.position_id: p for p in plain.scenarios[0].positions}["p-FRN-KBD"].pnl_rub
    assert by_id["basis"]["p-FRN-KBD"].pnl_rub == plain_pnl
    # Naming the KBD is refused for the bonds reported as RUONIA + basis: the OFZ and the floater.
    assert {r.position_id for r in kbd.coverage.rejections} == {"p-SU-LONG", "p-FRN-KBD"}
    assert {r.code for r in kbd.coverage.rejections} == {"INVALID_TERMS"}
    assert joint.coverage.status == parallel.coverage.status == basis.coverage.status == "complete"


def test_portfolio_floater_needs_the_view() -> None:
    """As for an OFZ: a KBD that is not v3 makes the floater ``INVALID_TERMS``; other rows
    are valued."""
    snapshot = _snapshot()
    curves = tuple(
        replace(c, spec_id="rub_ofz_kbd_v2") if c.curve_id == "RUB_OFZ_KBD" else c
        for c in snapshot.curves
    )
    result = value_portfolio(
        run_id="ofz-basis-floater",
        as_of=AS_OF,
        snapshot=replace(snapshot, curves=curves),
        instruments=_instruments(),
        positions=_positions(),
        policy=_floater_policy(ofz_basis=IDS),
    )
    rejected = {r.position_id: r.code for r in result.coverage.rejections}
    assert rejected == {"p-SU-LONG": "INVALID_TERMS", "p-FRN-KBD": "INVALID_TERMS"}
