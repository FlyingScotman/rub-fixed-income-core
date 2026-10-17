"""Rate-based accrued interest: ``RateAccrualCouponLeg`` and ``AccrualBasis`` (0.17.0).

MOEX evidence for each basis is in ``tests/test_moex_eod_goldens.py``; these are the
analytical cases, edges and compatibility checks.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from rub_fixed_income import (
    AccountingCategory,
    AccrualBasis,
    BondPriceObservation,
    BusinessCalendar,
    CalendarEntry,
    CashflowKind,
    CouponPeriod,
    CurveEntry,
    DiscountCurve,
    FixedCouponLeg,
    Instrument,
    InvalidTerms,
    MarketSnapshot,
    Position,
    PrincipalLeg,
    ProductType,
    RateAccrualCouponLeg,
    RateCouponPeriod,
    Redemption,
    ValuationPolicy,
    canonical_sha256,
    from_dict,
    horizon_valuation,
    to_dict,
    value_portfolio,
)


def monthly_bond(basis: AccrualBasis, rate: float = 0.25) -> Instrument:
    """1000 face, 30-day coupons of 20.55 (1000 * 25 % * 30 / 365 = 20.5479...)."""
    starts = [date(2026, 8, 31), date(2026, 9, 30), date(2026, 10, 30)]
    periods = tuple(
        RateCouponPeriod(s, e, e, 20.55, rate)
        for s, e in zip(starts, [*starts[1:], date(2026, 11, 29)])
    )
    return Instrument(
        "M",
        ProductType.FIXED_BOND,
        "RUB",
        1000.0,
        RateAccrualCouponLeg(periods, basis),
        PrincipalLeg((Redemption(date(2026, 11, 29), 1000.0),)),
        "X",
    )


def rate_periods(bond: Instrument) -> tuple[RateCouponPeriod, ...]:
    assert isinstance(bond.coupon_leg, RateAccrualCouponLeg)
    return bond.coupon_leg.periods


def test_act_365_accrues_on_the_rate_not_the_rounded_amount() -> None:
    bond = monthly_bond(AccrualBasis.RATE_ACT_365)
    # 15 days: 1000 * 0.25 * 15 / 365 = 10.2739..., while 20.55 * 15 / 30 = 10.275 -> 10.28.
    assert bond.accrued(date(2026, 10, 15)) == 10.27
    pro_rata = Instrument(
        "M",
        ProductType.FIXED_BOND,
        "RUB",
        1000.0,
        FixedCouponLeg(
            tuple(
                CouponPeriod(p.accrual_start, p.accrual_end, p.payment_date, p.amount)
                for p in rate_periods(bond)
            )
        ),
        bond.principal_leg,
        "X",
    )
    assert pro_rata.accrued(date(2026, 10, 15)) == 10.28
    assert pro_rata.cashflows() == bond.cashflows()  # same flows; only accrual differs


def test_act_360_and_30_360() -> None:
    assert monthly_bond(AccrualBasis.RATE_ACT_360).accrued(date(2026, 10, 15)) == round(
        1000 * 0.25 * 15 / 360, 2
    )
    # 30/360 from 30 September to 15 October is 15 days.
    assert monthly_bond(AccrualBasis.RATE_30_360).accrued(date(2026, 10, 15)) == 10.42


@pytest.mark.parametrize(
    ("start", "end", "days"),
    [
        (date(2026, 1, 31), date(2026, 2, 28), 28),  # D1 31 -> 30
        (date(2026, 1, 30), date(2026, 3, 31), 60),  # D2 31 -> 30 when D1 is 30
        (date(2026, 1, 29), date(2026, 3, 31), 62),  # D2 stays 31 when D1 < 30
        (date(2026, 2, 28), date(2026, 8, 28), 180),
        (date(2026, 8, 16), date(2027, 2, 16), 180),
        (date(2026, 5, 2), date(2026, 5, 2), 0),
    ],
)
def test_thirty_360_bond_basis_day_count(start: date, end: date, days: int) -> None:
    assert AccrualBasis.RATE_30_360.days(start, end) == (days, 360)


def test_rate_accrual_uses_the_face_outstanding_at_the_period_start() -> None:
    periods = (
        RateCouponPeriod(date(2026, 1, 1), date(2026, 7, 1), date(2026, 7, 1), 50.0, 0.1),
        RateCouponPeriod(date(2026, 7, 1), date(2027, 1, 1), date(2027, 1, 1), 25.0, 0.1),
    )
    bond = Instrument(
        "A",
        ProductType.FIXED_BOND,
        "RUB",
        1000.0,
        RateAccrualCouponLeg(periods, AccrualBasis.RATE_ACT_365),
        PrincipalLeg(
            (
                Redemption(date(2026, 7, 1), 500.0, CashflowKind.AMORTIZATION),
                Redemption(date(2027, 1, 1), 500.0),
            )
        ),
        "X",
    )
    assert bond.accrued(date(2026, 3, 1)) == round(1000 * 0.1 * 59 / 365, 2)
    assert bond.accrued(date(2026, 9, 1)) == round(500 * 0.1 * 62 / 365, 2)


def test_period_edges_and_rejections() -> None:
    bond = monthly_bond(AccrualBasis.RATE_ACT_365)
    assert bond.accrued(date(2026, 8, 31)) == 0.0  # first day of a period
    assert bond.accrued(date(2026, 9, 30)) == 0.0  # coupon date: the buyer does not get it
    assert bond.accrued(date(2026, 11, 29)) == 0.0  # after the last period
    with pytest.raises(InvalidTerms, match="no coupon accrual period"):
        bond.accrued(date(2026, 8, 1))
    with pytest.raises(InvalidTerms, match="rate cannot be negative"):
        RateCouponPeriod(date(2026, 1, 1), date(2026, 2, 1), date(2026, 2, 1), 1.0, -0.01)
    with pytest.raises(InvalidTerms, match="paid before"):
        RateCouponPeriod(date(2026, 1, 1), date(2026, 2, 1), date(2026, 1, 15), 1.0, 0.1)
    with pytest.raises(TypeError, match="AccrualBasis"):
        RateAccrualCouponLeg((), "rate:ACT/365F")  # type: ignore[arg-type]
    first, second = rate_periods(bond)[:2]
    with pytest.raises(InvalidTerms, match="ordered"):
        RateAccrualCouponLeg((second, first), AccrualBasis.RATE_ACT_365)


def test_round_trips_and_leaves_fixed_leg_bytes_unchanged() -> None:
    bond = monthly_bond(AccrualBasis.RATE_30_360)
    assert from_dict(Instrument, to_dict(bond)) == bond
    assert to_dict(bond)["coupon_leg"]["accrual_basis"] == "rate:30/360"
    fixed = FixedCouponLeg(
        (CouponPeriod(date(2026, 1, 1), date(2026, 7, 1), date(2026, 7, 1), 50.0),)
    )
    plain = Instrument(
        "F",
        ProductType.FIXED_BOND,
        "RUB",
        1000.0,
        fixed,
        PrincipalLeg((Redemption(date(2026, 7, 1), 1000.0),)),
        "X",
    )
    decoded = from_dict(Instrument, to_dict(plain))
    assert isinstance(decoded.coupon_leg, FixedCouponLeg) and decoded == plain
    # A FixedCouponLeg serialises as in 0.16.0: periods only, four fields each.
    assert to_dict(plain)["coupon_leg"] == {
        "periods": [
            {
                "accrual_start": "2026-01-01",
                "accrual_end": "2026-07-01",
                "payment_date": "2026-07-01",
                "amount": 50.0,
            }
        ]
    }
    assert canonical_sha256(plain) == canonical_sha256(from_dict(Instrument, to_dict(plain)))


def test_portfolio_and_horizon_use_the_rate_accrual() -> None:
    bond = monthly_bond(AccrualBasis.RATE_ACT_365)
    curve = DiscountCurve(date(2026, 10, 14), ((date(2027, 10, 14), 0.8),))
    calendar = BusinessCalendar(version="weekends")
    result = horizon_valuation(
        bond, curve, calendar, horizon_date=date(2026, 10, 22), spread_bp=0.0
    )
    assert result.accrued_start_rub == bond.accrued(date(2026, 10, 15)) == 10.27
    assert result.accrued_end_rub == bond.accrued(date(2026, 10, 23))
    snapshot = MarketSnapshot(
        "s",
        date(2026, 10, 14),
        datetime(2026, 10, 14, 16, tzinfo=UTC),
        curves=(CurveEntry("RUB_OIS", "rub_ruonia_ois_legacy_v1", curve),),
        calendars=(CalendarEntry("X", calendar),),
        quotes=(BondPriceObservation("M", 99.5, "waprice"),),
    )
    valued = value_portfolio(
        run_id="r",
        as_of=snapshot.known_at,
        snapshot=snapshot,
        instruments=(bond,),
        positions=(Position("p", "M", 10, "B", AccountingCategory.TRADING, "RUB"),),
        policy=ValuationPolicy("RUB_OIS", include_second_order=False),
    )
    (row,) = valued.positions
    assert row.settlement_date == date(2026, 10, 15)
    assert row.accrued_interest_rub == 10.27
