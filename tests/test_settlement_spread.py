"""F2/F3: explicit Z-spread compounding and settlement-aligned discounting."""

from __future__ import annotations

from datetime import date
from math import exp

import pytest

from rub_fixed_income import (
    CONTINUOUS,
    MONTHLY,
    DayCount,
    DiscountCurve,
    ZSpreadConvention,
    price_cashflows,
    solve_continuous_z_spread,
    solve_z_spread,
)
from rub_fixed_income.dates import year_fraction

CURVE_DATE = date(2026, 8, 28)
CURVE = DiscountCurve(
    CURVE_DATE,
    ((date(2027, 8, 28), 0.88), (date(2029, 8, 28), 0.70), (date(2031, 8, 28), 0.55)),
)
FLOWS = (
    (date(2027, 2, 28), 60.0),
    (date(2027, 8, 28), 60.0),
    (date(2028, 8, 28), 60.0),
    (date(2029, 8, 28), 1060.0),
)

# These tests pin the behaviour of APIs deprecated in 0.12.0 (kept until 1.0);
# tests/test_deprecations.py checks that each one warns.
pytestmark = pytest.mark.filterwarnings("ignore:rub_fixed_income:DeprecationWarning")


def _rebased(curve: DiscountCurve, settlement: date) -> DiscountCurve:
    """Mirror of currency-bond-monitor curves.py manual settlement rebase."""
    settlement_df = curve.discount_factor(settlement)
    nodes = tuple((d, df / settlement_df) for d, df in curve.nodes if d > settlement)
    return DiscountCurve(settlement, nodes)


@pytest.mark.parametrize("frequency", [1, 2, 12])
def test_settlement_equal_to_curve_date_is_bitwise_legacy(frequency: int) -> None:
    legacy = solve_z_spread(FLOWS, 980.0, CURVE, compounding_frequency=frequency)
    explicit = solve_z_spread(
        FLOWS, 980.0, CURVE, compounding_frequency=frequency, settlement_date=CURVE_DATE
    )
    assert explicit == legacy
    assert price_cashflows(
        FLOWS, CURVE, spread=0.01, compounding_frequency=frequency, settlement_date=CURVE_DATE
    ) == price_cashflows(FLOWS, CURVE, spread=0.01, compounding_frequency=frequency)


@pytest.mark.parametrize("frequency", [1, 12])
def test_settlement_date_matches_manual_curve_rebase(frequency: int) -> None:
    settlement = date(2026, 9, 3)
    manual = solve_z_spread(
        FLOWS, 980.0, _rebased(CURVE, settlement), compounding_frequency=frequency
    )
    direct = solve_z_spread(
        FLOWS, 980.0, CURVE, compounding_frequency=frequency, settlement_date=settlement
    )
    assert direct == pytest.approx(manual, abs=1e-12)


def test_settlement_aligned_one_flow_has_zero_spread() -> None:
    settlement, payment = date(2026, 12, 1), date(2027, 8, 28)
    dirty = 1000.0 * CURVE.discount_factor(payment) / CURVE.discount_factor(settlement)
    for convention in (ZSpreadConvention(), ZSpreadConvention(compounding=MONTHLY)):
        spread = solve_z_spread(
            ((payment, 1000.0),), dirty, CURVE, settlement_date=settlement, convention=convention
        )
        assert spread == pytest.approx(0.0, abs=1e-12)
    # the legacy curve-date path reports a spurious non-zero spread
    assert abs(solve_z_spread(((payment, 1000.0),), dirty, CURVE)) > 1e-3


def test_settlement_drops_flows_on_or_before_settlement() -> None:
    settlement = date(2027, 2, 28)
    with_paid = solve_z_spread(FLOWS, 980.0, CURVE, settlement_date=settlement)
    without = solve_z_spread(FLOWS[1:], 980.0, CURVE, settlement_date=settlement)
    assert with_paid == without


def test_settlement_before_curve_date_is_rejected() -> None:
    with pytest.raises(ValueError, match="settlement"):
        solve_z_spread(FLOWS, 980.0, CURVE, settlement_date=date(2026, 8, 1))


def test_no_flow_after_settlement_is_rejected() -> None:
    with pytest.raises(ValueError, match="after settlement"):
        solve_z_spread(FLOWS, 980.0, CURVE, settlement_date=date(2029, 8, 28))


def test_continuous_convention_matches_continuous_solver_exactly() -> None:
    settlement = date(2026, 9, 3)
    expected = solve_continuous_z_spread(FLOWS, 980.0, CURVE, settlement_date=settlement)
    solved = solve_z_spread(
        FLOWS,
        980.0,
        CURVE,
        settlement_date=settlement,
        convention=ZSpreadConvention(compounding=CONTINUOUS),
    )
    assert solved == expected


def test_monthly_convention_object_equals_frequency_keyword() -> None:
    assert solve_z_spread(
        FLOWS, 980.0, CURVE, convention=ZSpreadConvention(compounding=MONTHLY)
    ) == solve_z_spread(FLOWS, 980.0, CURVE, compounding_frequency=12)


def test_convention_and_conflicting_frequency_keyword_are_rejected() -> None:
    with pytest.raises(ValueError, match="either"):
        solve_z_spread(
            FLOWS,
            980.0,
            CURVE,
            compounding_frequency=12,
            convention=ZSpreadConvention(),
        )


def test_spread_day_count_is_an_input() -> None:
    convention = ZSpreadConvention(compounding=CONTINUOUS, day_count=DayCount.ACT_360)
    settlement = CURVE_DATE
    target_spread = 0.015
    dirty = sum(
        amount * CURVE.discount_factor(d) * exp(-target_spread * (d - settlement).days / 360.0)
        for d, amount in FLOWS
    )
    solved = solve_z_spread(FLOWS, dirty, CURVE, settlement_date=settlement, convention=convention)
    assert solved == pytest.approx(target_spread, abs=1e-12)
    act365 = solve_z_spread(
        FLOWS,
        dirty,
        CURVE,
        settlement_date=settlement,
        convention=ZSpreadConvention(compounding=CONTINUOUS),
    )
    assert act365 == pytest.approx(target_spread * 365 / 360, rel=1e-9)
    assert year_fraction(settlement, FLOWS[0][0]) > 0
