"""Exact-decimal pro rata accrued interest of fixed legs (issue #16, B#116)."""

from __future__ import annotations

import random
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

from rub_fixed_income import (
    CouponPeriod,
    FixedCouponLeg,
    Instrument,
    PrincipalLeg,
    ProductType,
    Redemption,
    accrued_interest,
)


def one_coupon_bond(start: date, days: int, coupon: float) -> Instrument:
    end = start + timedelta(days=days)
    return Instrument(
        "OFZ-TEST-HALF-KOPECK",
        ProductType.FIXED_BOND,
        "RUB",
        1000.0,
        FixedCouponLeg((CouponPeriod(start, end, end, coupon),)),
        PrincipalLeg((Redemption(end, 1000.0),)),
        "MOEX_STOCK",
    )


def test_exact_half_kopeck_rounds_half_up() -> None:
    # 77.49 * 13 / 182 = 5.535 exactly; the float product is 5.534999..., which the
    # float path rounded down to 5.53.
    assert 77.49 * 13 / 182 < 5.535
    start = date(2026, 1, 15)
    bond = one_coupon_bond(start, 182, 77.49)
    settlement = start + timedelta(days=13)
    assert bond.accrued(settlement) == 5.54
    assert accrued_interest(bond.cashflows(), settlement) == 5.54
    # The raw value stays the float product.
    assert accrued_interest(bond.cashflows(), settlement, quantum=None) == 77.49 * 13 / 182


def test_pro_rata_matches_a_decimal_reference() -> None:
    rng = random.Random(16)
    start = date(2026, 3, 1)
    for _ in range(2_000):
        days = rng.randint(28, 370)
        coupon = rng.randint(1, 99_999) / 100
        elapsed = rng.randint(1, days - 1)
        bond = one_coupon_bond(start, days, coupon)
        expected = float(
            (Decimal(repr(coupon)) * elapsed / days).quantize(Decimal("0.01"), ROUND_HALF_UP)
        )
        assert bond.accrued(start + timedelta(days=elapsed)) == expected
