"""Cashflow split and domain types (roadmap 0.2, 0.3)."""

from __future__ import annotations

import json
import math
import random
from datetime import UTC, date, datetime, timedelta

import pytest

from rub_fixed_income import (
    AccountingCategory,
    BondCashflow,
    BondPriceObservation,
    BusinessCalendar,
    CalendarEntry,
    Cashflow,
    CashflowKind,
    CouponPeriod,
    CurveEntry,
    DayCount,
    DiscountCurve,
    FixedCouponLeg,
    FixingEntry,
    FixingHistory,
    FloatingCouponLeg,
    FloatingCouponPeriod,
    FloatingCouponRule,
    FxRate,
    Instrument,
    InvalidTerms,
    MarketSnapshot,
    MissingMarketData,
    Offer,
    OfferKind,
    Position,
    PrincipalLeg,
    ProductType,
    Redemption,
    SnapshotEvidence,
    accrued_interest,
    canonical_sha256,
    from_dict,
    to_dict,
)
from rub_fixed_income.analytics import BondTermsV1, accrued_interest_rub

NOW = datetime(2026, 3, 2, 18, tzinfo=UTC)


def semiannual_periods(start: date, count: int, amount: float) -> tuple[CouponPeriod, ...]:
    periods = []
    for i in range(count):
        begin = date(
            start.year + (start.month - 1 + 6 * i) // 12, (start.month - 1 + 6 * i) % 12 + 1, 1
        )
        end = date(begin.year + (begin.month - 1 + 6) // 12, (begin.month - 1 + 6) % 12 + 1, 1)
        periods.append(CouponPeriod(begin, end, end, amount))
    return tuple(periods)


def bullet() -> Instrument:
    periods = semiannual_periods(date(2025, 10, 1), 6, 40.0)
    return Instrument(
        "OFZ-TEST-BULLET",
        ProductType.FIXED_BOND,
        "RUB",
        1000.0,
        FixedCouponLeg(periods),
        PrincipalLeg((Redemption(periods[-1].payment_date, 1000.0),)),
        "MOEX_STOCK",
        offers=(Offer(date(2027, 4, 1), OfferKind.PUT, confirmed=True),),
    )


def amortizer() -> Instrument:
    periods = semiannual_periods(date(2025, 10, 1), 4, 30.0)
    return Instrument(
        "CORP-TEST-AMORT",
        ProductType.FIXED_BOND,
        "RUB",
        1000.0,
        FixedCouponLeg(periods),
        PrincipalLeg(
            (
                Redemption(periods[1].payment_date, 500.0, CashflowKind.AMORTIZATION),
                Redemption(periods[3].payment_date, 500.0),
            )
        ),
        "MOEX_STOCK",
    )


def test_split_cashflow_is_a_bond_cashflow_with_coupon_plus_principal() -> None:
    flow = Cashflow.split(
        date(2026, 4, 1),
        coupon=40.0,
        principal=1000.0,
        accrual_start=date(2025, 10, 1),
        accrual_end=date(2026, 4, 1),
    )
    assert isinstance(flow, BondCashflow)
    assert flow.amount == 1040.0
    assert flow.kind is CashflowKind.MATURITY
    with pytest.raises(InvalidTerms, match="coupon \\+ principal"):
        Cashflow(date(2026, 4, 1), 1.0, coupon=2.0)
    with pytest.raises(InvalidTerms, match="COUPON flow has no principal"):
        Cashflow.split(date(2026, 4, 1), principal=1.0, kind=CashflowKind.COUPON)
    with pytest.raises(InvalidTerms, match="accrual period"):
        Cashflow.split(date(2026, 4, 1), coupon=1.0)


def test_instrument_cashflows_merge_coupons_and_principal() -> None:
    flows = bullet().cashflows()
    assert len(flows) == 6
    assert all(f.coupon == 40.0 for f in flows)
    assert [f.principal for f in flows] == [0.0] * 5 + [1000.0]
    assert flows[-1].kind is CashflowKind.MATURITY
    assert math.fsum(f.amount for f in flows) == 6 * 40.0 + 1000.0
    for flow in flows:
        assert flow.coupon + flow.principal == flow.amount


def test_amortizer_outstanding_face_and_kinds() -> None:
    bond = amortizer()
    flows = bond.cashflows()
    assert [f.kind for f in flows] == [
        CashflowKind.COUPON,
        CashflowKind.AMORTIZATION,
        CashflowKind.COUPON,
        CashflowKind.MATURITY,
    ]
    assert bond.outstanding_face(date(2026, 10, 1)) == 500.0
    assert bond.outstanding_face(date(2026, 9, 30)) == 1000.0
    after = bond.cashflows(after=date(2026, 10, 1))
    assert [f.payment_date for f in after] == [date(2027, 4, 1), date(2027, 10, 1)]


def test_confirmed_put_horizon_redeems_outstanding_face_at_the_offer() -> None:
    bond = bullet()
    horizon, offer = bond.horizon("confirmed_put", date(2026, 3, 3))
    assert horizon == date(2027, 4, 1) and offer is not None
    flows = bond.cashflows(after=date(2026, 3, 3), offer=offer)
    assert flows[-1].payment_date == date(2027, 4, 1)
    assert flows[-1].kind is CashflowKind.OFFER_REDEMPTION
    assert flows[-1].principal == 1000.0 and flows[-1].coupon == 40.0
    assert bond.horizon("maturity", date(2026, 3, 3)) == (date(2028, 10, 1), None)
    assert bond.horizon("confirmed_put", date(2027, 4, 1)) == (date(2028, 10, 1), None)


@pytest.mark.parametrize(
    "settlement", [date(2025, 10, 1), date(2026, 1, 15), date(2026, 3, 31), date(2026, 4, 1)]
)
def test_accrued_from_split_matches_the_analytics_formula(settlement: date) -> None:
    bond = bullet()
    expected_raw = (
        0.0
        if settlement == date(2026, 4, 1)
        else accrued_interest_rub(
            BondTermsV1(
                "X",
                1000.0,
                date(2028, 4, 1),
                "maturity",
                date(2028, 4, 1),
                date(2025, 10, 1),
                date(2026, 4, 1),
                40.0,
                "t",
            ),
            settlement,
        )
    )
    assert accrued_interest(bond.cashflows(), settlement, quantum=None) == pytest.approx(
        expected_raw, abs=1e-12
    )
    assert bond.accrued(settlement) == round(expected_raw + 1e-12, 2)


def test_accrued_rejects_settlement_before_the_first_period() -> None:
    with pytest.raises(InvalidTerms, match="no coupon accrual period"):
        accrued_interest(bullet().cashflows(), date(2025, 9, 1))
    assert accrued_interest(bullet().cashflows(), date(2029, 1, 1)) == 0.0
    assert accrued_interest((BondCashflow(date(2026, 1, 1), 5.0),), date(2025, 1, 1)) == 0.0


def test_instrument_validation() -> None:
    with pytest.raises(InvalidTerms, match="sum to the initial face"):
        Instrument(
            "X",
            ProductType.FIXED_BOND,
            "RUB",
            1000.0,
            None,
            PrincipalLeg((Redemption(date(2027, 1, 1), 900.0),)),
            "MOEX_STOCK",
        )
    with pytest.raises(InvalidTerms, match="ISO 4217"):
        Instrument("X", ProductType.FIXED_BOND, "rub", 1.0, None, PrincipalLeg(()), "C")
    with pytest.raises(InvalidTerms, match="last redemption must be the maturity"):
        PrincipalLeg((Redemption(date(2027, 1, 1), 1.0, CashflowKind.AMORTIZATION),))
    with pytest.raises(InvalidTerms, match="overlap"):
        FixedCouponLeg(
            (
                CouponPeriod(date(2026, 1, 1), date(2026, 7, 1), date(2026, 7, 1), 1.0),
                CouponPeriod(date(2026, 6, 1), date(2027, 1, 1), date(2027, 1, 1), 1.0),
            )
        )
    perpetual = Instrument(
        "PERP", ProductType.FIXED_BOND, "RUB", 1000.0, None, PrincipalLeg(()), "MOEX_STOCK"
    )
    assert perpetual.is_perpetual and perpetual.maturity_date is None
    with pytest.raises(InvalidTerms, match="perpetual"):
        perpetual.horizon("maturity", date(2026, 1, 1))


def snapshot() -> MarketSnapshot:
    calendar = BusinessCalendar(
        version="synthetic", valid_from=date(2026, 1, 1), valid_to=date(2026, 12, 31)
    )
    history = FixingHistory(
        "ruonia",
        date(2026, 3, 2),
        ((date(2026, 2, 27), 0.16),),
        BusinessCalendar(version="synthetic"),
        "fix-1",
    )
    return MarketSnapshot(
        "snap-1",
        date(2026, 3, 2),
        NOW,
        curves=(
            CurveEntry("RUB_OFZ_KBD", "rub_ofz_kbd_v1", DiscountCurve.flat(date(2026, 3, 2), 0.14)),
        ),
        calendars=(CalendarEntry("MOEX_STOCK", calendar),),
        fixings=(FixingEntry("RUONIA", history),),
        fx=(FxRate("CNY", 11.2), FxRate("USD", 80.5)),
        quotes=(BondPriceObservation("OFZ-TEST-BULLET", 98.5, "waprice"),),
        evidence=(SnapshotEvidence("snap-1", "hub", NOW, NOW, NOW + timedelta(days=1)),),
    )


def test_snapshot_lookups_and_missing_data() -> None:
    snap = snapshot()
    assert snap.curve("RUB_OFZ_KBD").spec_id == "rub_ofz_kbd_v1"
    assert snap.fx_rate("RUB") == 1.0 and snap.fx_rate("USD") == 80.5
    assert snap.quote("OFZ-TEST-BULLET").clean_price_pct == 98.5
    assert snap.calendar("MOEX_STOCK").is_bounded
    assert snap.fixing("RUONIA").snapshot_id == "fix-1"
    for lookup in (
        lambda: snap.curve("RUB_OIS"),
        lambda: snap.calendar("MOEX_FX"),
        lambda: snap.fixing("KEY_RATE"),
        lambda: snap.fx_rate("EUR"),
        lambda: snap.quote("NOPE"),
    ):
        with pytest.raises(MissingMarketData):
            lookup()
    with pytest.raises(InvalidTerms, match="duplicate curve_id"):
        MarketSnapshot("s", date(2026, 1, 1), NOW, curves=(snap.curves[0], snap.curves[0]))
    with pytest.raises(InvalidTerms, match="UTC offset"):
        MarketSnapshot("s", date(2026, 1, 1), datetime(2026, 1, 1))  # noqa: DTZ001
    with pytest.raises(InvalidTerms, match="factor-key separator"):
        CurveEntry("A|B", "rub_ofz_kbd_v1", DiscountCurve.flat(date(2026, 1, 1), 0.1))


def test_position_validation() -> None:
    position = Position(
        "p1", "OFZ-TEST-BULLET", -50, "RATES", AccountingCategory.TRADING, "RUB", 0.4
    )
    assert position.quantity == -50
    with pytest.raises(InvalidTerms, match="recovery_rate"):
        Position("p1", "X", 1, "B", AccountingCategory.HTM, "RUB", 1.5)


def floater() -> Instrument:
    rule = FloatingCouponRule(
        "ruonia", "compounded", 0.01, DayCount.ACT_365_FIXED, DayCount.ACT_365_FIXED
    )
    period = FloatingCouponPeriod(date(2026, 1, 1), date(2026, 4, 1), date(2026, 4, 1), 1000.0)
    return Instrument(
        "FLOAT",
        ProductType.FLOATING_BOND,
        "RUB",
        1000.0,
        FloatingCouponLeg(rule, (period,), "RUONIA"),
        PrincipalLeg((Redemption(date(2026, 4, 1), 1000.0),)),
        "MOEX_STOCK",
    )


def test_floating_leg_contributes_principal_only_and_rejects_accrued() -> None:
    bond = floater()
    flows = bond.cashflows()
    assert len(flows) == 1 and flows[0].coupon == 0.0
    with pytest.raises(InvalidTerms, match="floating accrued"):
        bond.accrued(date(2026, 2, 1))


@pytest.mark.parametrize(
    "value",
    [
        bullet,
        amortizer,
        floater,
        snapshot,
        lambda: Position("p", "X", 1.5, "B", AccountingCategory.REPO, "RUB"),
    ],
)
def test_domain_types_round_trip_and_hash_stably(value: object) -> None:
    original = value()  # type: ignore[operator]
    data = json.loads(json.dumps(to_dict(original)))
    rebuilt = from_dict(type(original), data)
    assert rebuilt == original
    assert canonical_sha256(rebuilt) == canonical_sha256(original)


@pytest.mark.parametrize("seed", range(25))
def test_seeded_random_bonds_conserve_principal_and_round_trip(seed: int) -> None:
    rng = random.Random(seed)
    count = rng.randrange(1, 9)
    periods = semiannual_periods(
        date(2025, rng.choice((1, 4, 7, 10)), 1), count, rng.uniform(0, 80)
    )
    cuts = sorted(rng.sample(range(count - 1), k=rng.randrange(0, count))) if count > 1 else []
    face = 1000.0
    amounts = [round(face / (len(cuts) + 1), 2)] * len(cuts)
    redemptions = [
        Redemption(periods[i].payment_date, a, CashflowKind.AMORTIZATION)
        for i, a in zip(cuts, amounts, strict=True)
    ]
    redemptions.append(Redemption(periods[-1].payment_date, face - math.fsum(amounts)))
    bond = Instrument(
        f"R{seed}",
        ProductType.FIXED_BOND,
        "RUB",
        face,
        FixedCouponLeg(periods),
        PrincipalLeg(tuple(redemptions)),
        "MOEX_STOCK",
    )
    flows = bond.cashflows()
    assert math.isclose(math.fsum(f.principal for f in flows), face)
    assert math.isclose(math.fsum(f.coupon for f in flows), math.fsum(p.amount for p in periods))
    assert from_dict(Instrument, json.loads(json.dumps(to_dict(bond)))) == bond


def test_split_flows_price_and_risk_exactly_like_totals() -> None:
    from rub_fixed_income import BondRiskEngine, BondRiskRequest, ZSpreadConvention

    settlement = date(2026, 3, 3)
    split = bullet().cashflows(after=settlement)
    totals = tuple(BondCashflow(f.payment_date, f.amount) for f in split)
    curve = DiscountCurve.flat(settlement, 0.14)

    def risk(flows: tuple[BondCashflow, ...]) -> tuple[float, str]:
        engine = BondRiskEngine(
            BondRiskRequest(
                flows, settlement, curve, ZSpreadConvention(), "ofz", dirty_price_rub=1001.0
            )
        )
        return engine.risk().dv01_rub, engine.base_id

    assert risk(split) == risk(totals)
