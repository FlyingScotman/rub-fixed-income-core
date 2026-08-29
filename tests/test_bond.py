from __future__ import annotations

from datetime import date

import pytest

from rub_fixed_income import (
    AccumulationPeriod,
    CashEvent,
    EventKey,
    FinancedBondRequest,
    constant_daily_accumulation_periods,
    project_financed_bond,
)


def test_legacy_nominal_yield_and_duration_facade_preserves_requested_compounding() -> None:
    from rub_fixed_income import bond_duration, solve_nominal_annual_yield

    cashflows = ((date(2026, 1, 1), 5.0), (date(2027, 1, 1), 105.0))
    solved = solve_nominal_annual_yield(
        dirty_price=100.0,
        settlement_date=date(2025, 1, 1),
        cashflows=cashflows,
        compounding_frequency=2,
    )
    duration = bond_duration(
        annual_yield=solved,
        settlement_date=date(2025, 1, 1),
        cashflows=cashflows,
        compounding_frequency=2,
    )

    assert abs(solved - 0.04939015319191986) < 1e-12
    assert duration.macaulay_years > duration.modified_years
    assert abs(duration.present_value - 100.0) < 1e-10


def test_legacy_yield_facade_preserves_non_365_day_count_basis() -> None:
    from rub_fixed_income import solve_nominal_annual_yield

    solved = solve_nominal_annual_yield(
        dirty_price=100.0 / 1.10,
        settlement_date=date(2025, 1, 1),
        cashflows=((date(2025, 12, 31), 100.0),),
        compounding_frequency=1,
        day_count_basis=364.0,
    )

    assert solved == pytest.approx(0.10, abs=1e-12)


def test_d002_coupon_timing_projects_1009_4000() -> None:
    start = EventKey(date(2026, 1, 1), 0)
    coupon = EventKey(date(2026, 7, 1), 0)
    horizon = EventKey(date(2027, 1, 1), 0)
    request = FinancedBondRequest(
        start=start,
        horizon=horizon,
        initial_dirty_rub_per_bond=1000.0,
        cash_events=(CashEvent(coupon, 40.0, "coupon-1"),),
        accumulation_periods=(
            AccumulationPeriod(start, coupon, 1.02, "funding-1"),
            AccumulationPeriod(coupon, horizon, 1.03, "funding-2"),
        ),
    )

    result = project_financed_bond(request)

    assert result.horizon_dirty_rub_per_bond == pytest.approx(1009.4)
    assert [step.closing_balance_rub_per_bond for step in result.steps] == pytest.approx(
        [980.0, 1009.4]
    )


def test_projection_rejects_cash_event_outside_horizon() -> None:
    start = EventKey(date(2026, 1, 1), 0)
    horizon = EventKey(date(2027, 1, 1), 0)
    request = FinancedBondRequest(
        start,
        horizon,
        1000.0,
        (CashEvent(EventKey(date(2027, 7, 1), 0), 40.0, "late-coupon"),),
        (AccumulationPeriod(start, horizon, 1.10, "funding"),),
    )

    with pytest.raises(
        ValueError, match="cash events must be after start and on or before horizon"
    ):
        project_financed_bond(request)


def test_projection_rejects_gapped_accumulation_timeline() -> None:
    start = EventKey(date(2026, 1, 1), 0)
    event = EventKey(date(2026, 7, 1), 0)
    horizon = EventKey(date(2027, 1, 1), 0)
    request = FinancedBondRequest(
        start,
        horizon,
        1000.0,
        (),
        (AccumulationPeriod(event, horizon, 1.03, "funding-2"),),
    )

    with pytest.raises(ValueError, match="ordered without gaps or overlaps"):
        project_financed_bond(request)


@pytest.mark.parametrize("factor", [0.0, -1.0, float("inf"), float("nan")])
def test_projection_rejects_invalid_accumulation_factor(factor: float) -> None:
    start = EventKey(date(2026, 1, 1), 0)
    horizon = EventKey(date(2027, 1, 1), 0)
    request = FinancedBondRequest(
        start,
        horizon,
        1000.0,
        (),
        (AccumulationPeriod(start, horizon, factor, "funding"),),
    )

    with pytest.raises(ValueError, match="accumulation factors must be positive and finite"):
        project_financed_bond(request)


def test_projection_rejects_ambiguous_same_key_cash_events() -> None:
    start = EventKey(date(2026, 1, 1), 0)
    coupon = EventKey(date(2026, 7, 1), 0)
    horizon = EventKey(date(2027, 1, 1), 0)
    request = FinancedBondRequest(
        start,
        horizon,
        1000.0,
        (CashEvent(coupon, 40.0, "coupon"), CashEvent(coupon, 100.0, "amortization")),
        (
            AccumulationPeriod(start, coupon, 1.02, "funding-1"),
            AccumulationPeriod(coupon, horizon, 1.03, "funding-2"),
        ),
    )

    with pytest.raises(ValueError, match="cash-event keys must be strictly ordered and unique"):
        project_financed_bond(request)


def test_projection_rejects_missing_same_day_ordinal() -> None:
    start = EventKey(date(2026, 7, 1), 0)
    horizon = EventKey(date(2026, 7, 1), 2)
    request = FinancedBondRequest(
        start,
        horizon,
        1000.0,
        (),
        (AccumulationPeriod(start, horizon, 1.0, "same-day"),),
    )

    with pytest.raises(ValueError, match="same-day event ordinals must be contiguous"):
        project_financed_bond(request)


def test_projection_rejects_duplicate_period_ids() -> None:
    start = EventKey(date(2026, 1, 1), 0)
    reset = EventKey(date(2026, 7, 1), 0)
    horizon = EventKey(date(2027, 1, 1), 0)
    request = FinancedBondRequest(
        start,
        horizon,
        1000.0,
        (),
        (
            AccumulationPeriod(start, reset, 1.02, "funding"),
            AccumulationPeriod(reset, horizon, 1.03, "funding"),
        ),
    )

    with pytest.raises(ValueError, match="accumulation-period IDs must be unique"):
        project_financed_bond(request)


def test_projection_rejects_cash_event_without_timeline_boundary() -> None:
    start = EventKey(date(2026, 1, 1), 0)
    coupon = EventKey(date(2026, 7, 1), 0)
    horizon = EventKey(date(2027, 1, 1), 0)
    request = FinancedBondRequest(
        start,
        horizon,
        1000.0,
        (CashEvent(coupon, 40.0, "coupon"),),
        (AccumulationPeriod(start, horizon, 1.10, "funding"),),
    )

    with pytest.raises(ValueError, match="every cash event must terminate an accumulation period"):
        project_financed_bond(request)


def test_constant_daily_partition_is_event_partition_invariant() -> None:
    start = EventKey(date(2026, 1, 1), 0)
    observation = EventKey(date(2026, 7, 1), 0)
    horizon = EventKey(date(2027, 1, 1), 0)
    unsplit = project_financed_bond(
        FinancedBondRequest(
            start,
            horizon,
            1000.0,
            (),
            constant_daily_accumulation_periods(start, horizon, (), 0.14),
        )
    )
    partitioned = project_financed_bond(
        FinancedBondRequest(
            start,
            horizon,
            1000.0,
            (),
            constant_daily_accumulation_periods(start, horizon, (observation,), 0.14),
        )
    )

    assert partitioned.horizon_dirty_rub_per_bond == pytest.approx(
        unsplit.horizon_dirty_rub_per_bond, abs=1e-12
    )


def test_projection_rejects_duplicate_cash_event_ids() -> None:
    start = EventKey(date(2026, 1, 1), 0)
    coupon = EventKey(date(2026, 7, 1), 0)
    amortization = EventKey(date(2026, 7, 1), 1)
    horizon = EventKey(date(2027, 1, 1), 0)
    request = FinancedBondRequest(
        start,
        horizon,
        1000.0,
        (CashEvent(coupon, 40.0, "payment"), CashEvent(amortization, 100.0, "payment")),
        (
            AccumulationPeriod(start, coupon, 1.02, "funding-1"),
            AccumulationPeriod(coupon, amortization, 1.0, "funding-2"),
            AccumulationPeriod(amortization, horizon, 1.03, "funding-3"),
        ),
    )

    with pytest.raises(ValueError, match="cash-event IDs must be unique"):
        project_financed_bond(request)


def test_same_day_coupon_amortization_and_horizon_follow_explicit_ordinals() -> None:
    start = EventKey(date(2026, 1, 1), 0)
    coupon = EventKey(date(2026, 7, 1), 0)
    amortization = EventKey(date(2026, 7, 1), 1)
    horizon = EventKey(date(2026, 7, 1), 2)
    result = project_financed_bond(
        FinancedBondRequest(
            start,
            horizon,
            1000.0,
            (CashEvent(coupon, 40.0, "coupon"), CashEvent(amortization, 100.0, "amort")),
            (
                AccumulationPeriod(start, coupon, 1.02, "to-payment-day"),
                AccumulationPeriod(coupon, amortization, 1.0, "coupon-before-amort"),
                AccumulationPeriod(amortization, horizon, 1.0, "amort-before-horizon"),
            ),
        )
    )

    assert [step.cash_event_id for step in result.steps] == ["coupon", "amort", None]
    assert result.horizon_dirty_rub_per_bond == pytest.approx(880.0)


@pytest.mark.parametrize("initial_dirty", [0.0, -1.0, float("inf"), float("nan")])
def test_projection_rejects_invalid_initial_dirty_cash(initial_dirty: float) -> None:
    start = EventKey(date(2026, 1, 1), 0)
    horizon = EventKey(date(2027, 1, 1), 0)
    request = FinancedBondRequest(
        start,
        horizon,
        initial_dirty,
        (),
        (AccumulationPeriod(start, horizon, 1.10, "funding"),),
    )

    with pytest.raises(ValueError, match="initial dirty cash must be positive and finite"):
        project_financed_bond(request)
