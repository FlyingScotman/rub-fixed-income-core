"""Yield time basis as an explicit issue term (BACKLOG #38).

MOEX golden values are in ``test_moex_eod_goldens.py``; these are analytical cases, the
default's bit-for-bit invariance and the serialized form.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date

import pytest

from rub_fixed_income import (
    QUARTERLY,
    BondCashflow,
    CouponPeriod,
    DayCount,
    FixedCouponLeg,
    Instrument,
    PrincipalLeg,
    ProductType,
    Redemption,
    YieldConvention,
    YieldRequest,
    canonical_sha256,
    from_dict,
    solve_effective_annual_yield,
    to_dict,
)

SETTLE = date(2026, 10, 16)
FLOWS = (
    BondCashflow(date(2027, 4, 15), 35.0),
    BondCashflow(date(2027, 10, 15), 35.0),
    BondCashflow(date(2028, 10, 15), 1035.0),
)


def bond(**changes: object) -> Instrument:
    base = Instrument(
        "USD-BOND",
        ProductType.FIXED_BOND,
        "USD",
        1000.0,
        FixedCouponLeg(
            (CouponPeriod(date(2026, 4, 15), date(2026, 10, 15), date(2026, 10, 15), 35.0),)
        ),
        PrincipalLeg((Redemption(date(2028, 10, 15), 1000.0),)),
        "MOEX_STOCK",
    )
    return replace(base, **changes)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("basis", "year"), [(DayCount.ACT_365_FIXED, 365), (DayCount.ACT_360, 360)]
)
def test_single_flow_effective_yield_is_analytical(basis: DayCount, year: int) -> None:
    flow = BondCashflow(date(2028, 1, 20), 1100.0)
    days = (flow.payment_date - SETTLE).days
    request = YieldRequest(SETTLE, 950.0, (flow,), YieldConvention(day_count=basis))
    expected = (1100.0 / 950.0) ** (year / days) - 1.0
    assert solve_effective_annual_yield(request).effective_annual_yield == pytest.approx(
        expected, abs=1e-12
    )


def test_bases_express_one_discount_curve() -> None:
    """Same price, same flows: ``(1 + y360)^(1/360) == (1 + y365)^(1/365)``."""
    y365 = solve_effective_annual_yield(YieldRequest(SETTLE, 990.0, FLOWS)).effective_annual_yield
    y360 = solve_effective_annual_yield(
        YieldRequest(SETTLE, 990.0, FLOWS, YieldConvention(day_count=DayCount.ACT_360))
    ).effective_annual_yield
    assert y360 < y365
    assert (1.0 + y360) ** (1 / 360) == pytest.approx((1.0 + y365) ** (1 / 365), abs=1e-14)


def test_default_basis_is_bit_for_bit_the_legacy_act_365f() -> None:
    legacy = solve_effective_annual_yield(YieldRequest(SETTLE, 990.0, FLOWS))
    explicit = solve_effective_annual_yield(YieldRequest(SETTLE, 990.0, FLOWS, YieldConvention()))
    assert legacy == explicit


def test_effective_yield_rejects_non_annual_compounding_and_bad_types() -> None:
    with pytest.raises(ValueError, match="compounds annually"):
        YieldRequest(SETTLE, 990.0, FLOWS, YieldConvention(compounding=QUARTERLY))
    with pytest.raises(TypeError):
        YieldRequest(SETTLE, 990.0, FLOWS, DayCount.ACT_360)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        bond(yield_convention=DayCount.ACT_360)


def test_default_yield_convention_leaves_the_serialized_instrument_unchanged() -> None:
    default = bond()
    encoded = to_dict(default)
    assert "yield_convention" not in encoded
    assert from_dict(Instrument, encoded) == default
    # An instrument serialized before the field existed decodes to the default.
    assert from_dict(Instrument, encoded).yield_convention == YieldConvention()


def test_explicit_yield_basis_is_part_of_the_terms() -> None:
    act360 = bond(yield_convention=YieldConvention(day_count=DayCount.ACT_360))
    encoded = to_dict(act360)
    assert encoded["yield_convention"] == {
        "compounding": {"periods_per_year": 1},
        "day_count": "ACT/360",
    }
    assert from_dict(Instrument, encoded) == act360
    assert canonical_sha256(act360) != canonical_sha256(bond())
    # Explicitly passing the default is the same instrument with the same hash.
    assert canonical_sha256(bond(yield_convention=YieldConvention())) == canonical_sha256(bond())
    unknown = dict(encoded, unknown_field=1)
    with pytest.raises(ValueError, match="fields do not match"):
        from_dict(Instrument, unknown)
