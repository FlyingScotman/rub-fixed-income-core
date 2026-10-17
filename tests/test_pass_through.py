"""Pass-through MBS/ABS under a deterministic constant CPR (package 9b)."""

from __future__ import annotations

import math
from datetime import date, timedelta
from itertools import pairwise

import pytest

from rub_fixed_income import (
    AccrualBasis,
    CashflowKind,
    DiscountCurve,
    InvalidTerms,
    PassThroughPeriod,
    PassThroughTerms,
    PrepaymentAssumption,
    cpr_sensitivity,
    from_dict,
    pass_through_analytics,
    pass_through_instrument,
    price_cashflows,
    project_pass_through,
    to_dict,
    weighted_average_life,
)
from rub_fixed_income.dates import year_fraction

START = date(2027, 1, 1)
CURVE = DiscountCurve(
    START,
    tuple(
        (d, (1.0 + r) ** -((d - START).days / 365.0))
        for d, r in ((date(2028, 1, 1), 0.12), (date(2040, 1, 1), 0.13))
    ),
)


def even_periods(count: int, days: int, scheduled: float = 0.0) -> tuple[PassThroughPeriod, ...]:
    dates = [START + timedelta(days=days * i) for i in range(count + 1)]
    return tuple(PassThroughPeriod(s, e, e, scheduled) for s, e in pairwise(dates))


def terms(
    periods: tuple[PassThroughPeriod, ...],
    *,
    rate: float = 0.12,
    payments_per_year: int = 12,
    face: float = 1000.0,
    original: float = 1000.0,
    cleanup: float | None = None,
) -> PassThroughTerms:
    return PassThroughTerms(
        "MBS-1",
        "RUB",
        original,
        face,
        rate,
        AccrualBasis.RATE_ACT_365,
        periods,
        payments_per_year,
        "MOEX_STOCK",
        cleanup_call_fraction=cleanup,
    )


def annuity_periods(count: int, face: float = 1000.0) -> tuple[PassThroughPeriod, ...]:
    """A 30-day, 12 % level-pay schedule whose scheduled principal sums to ``face``."""
    r = 0.12 * 30 / 365
    payment = face * r / (1.0 - (1.0 + r) ** -count)
    balance, out = face, []
    for i in range(count):
        principal = payment - balance * r if i < count - 1 else balance
        out.append(principal)
        balance -= principal
    dates = [START + timedelta(days=30 * i) for i in range(count + 1)]
    return tuple(
        PassThroughPeriod(s, e, e, p) for (s, e), p in zip(pairwise(dates), out, strict=True)
    )


def test_zero_cpr_reproduces_the_scheduled_flows() -> None:
    periods = annuity_periods(24)
    projection = project_pass_through(terms(periods), PrepaymentAssumption(0.0))
    assert projection.smm == 0.0
    balance = 1000.0
    for row, period in zip(projection.rows, periods, strict=True):
        assert row.prepaid_principal == 0.0
        assert row.scheduled_principal == pytest.approx(period.scheduled_principal, abs=1e-9)
        assert row.coupon == pytest.approx(balance * 0.12 * 30 / 365, abs=1e-9)
        balance -= period.scheduled_principal
    assert math.fsum(f.principal for f in projection.cashflows) == pytest.approx(1000.0, abs=1e-9)
    assert projection.cashflows[-1].kind is CashflowKind.MATURITY
    # A level-pay schedule pays a constant total.
    totals = [f.amount for f in projection.cashflows]
    assert max(totals) - min(totals) == pytest.approx(0.0, abs=1e-9)


@pytest.mark.parametrize("cpr", [0.05, 0.2, 0.6])
def test_wal_of_constant_cpr_without_schedule_is_the_survival_sum(cpr: float) -> None:
    """Periods of 73 days (0.2 years), 5 a year, no scheduled principal:
    principal ``s q^(k-1)`` and a balloon ``q^(n-1)`` give ``WAL = 0.2 (1 - q^n) / s``."""
    n = 25
    projection = project_pass_through(
        terms(even_periods(n, 73), payments_per_year=5), PrepaymentAssumption(cpr)
    )
    s = 1.0 - (1.0 - cpr) ** (1.0 / 5.0)
    q = 1.0 - s
    assert projection.smm == pytest.approx(s, abs=1e-15)
    wal = weighted_average_life(projection.cashflows, START)
    assert wal == pytest.approx(0.2 * (1.0 - q**n) / s, rel=1e-12)
    # CPR is annual: one year of five periods leaves (1 - CPR) of the balance.
    assert projection.rows[4].closing_balance == pytest.approx(1000.0 * (1.0 - cpr), rel=1e-12)


def test_wal_equals_the_integral_of_the_surviving_balance() -> None:
    projection = project_pass_through(terms(annuity_periods(36)), PrepaymentAssumption(0.15))
    wal = weighted_average_life(projection.cashflows, START)
    survival = math.fsum(
        year_fraction(r.accrual_start, r.payment_date) * r.opening_balance / 1000.0
        for r in projection.rows
    )
    assert wal == pytest.approx(survival, rel=1e-12)


def test_scheduled_principal_scales_with_the_surviving_balance() -> None:
    periods = annuity_periods(12)
    projection = project_pass_through(terms(periods), PrepaymentAssumption(0.3))
    scheduled_balance = 1000.0
    smm = projection.smm
    for row, period in zip(projection.rows[:-1], periods, strict=False):
        ratio = row.opening_balance / scheduled_balance
        assert row.scheduled_principal == pytest.approx(
            period.scheduled_principal * ratio, rel=1e-12
        )
        assert row.prepaid_principal == pytest.approx(
            smm * (row.opening_balance - row.scheduled_principal), rel=1e-12
        )
        scheduled_balance -= period.scheduled_principal
    assert projection.rows[-1].closing_balance == 0.0


def test_higher_cpr_shortens_wal_and_cuts_a_premium_pass_through() -> None:
    premium = terms(annuity_periods(60), rate=0.20)  # coupon above the ~12-13 % curve
    discount = terms(annuity_periods(60), rate=0.05)
    wals = [
        weighted_average_life(
            project_pass_through(premium, PrepaymentAssumption(c)).cashflows, START
        )
        for c in (0.0, 0.1, 0.3)
    ]
    assert wals[0] > wals[1] > wals[2]
    up = cpr_sensitivity(premium, PrepaymentAssumption(0.1), START, CURVE, spread_bp=150.0)
    down = cpr_sensitivity(discount, PrepaymentAssumption(0.1), START, CURVE, spread_bp=150.0)
    assert up.dpv_per_cpr_pct_rub < 0.0 < down.dpv_per_cpr_pct_rub
    assert up.wal_up_years < up.wal_years < up.wal_down_years
    assert up.difference == "central"


def test_cpr_sensitivity_is_an_independent_central_difference_and_converges() -> None:
    pool = terms(annuity_periods(48), rate=0.16)
    base = PrepaymentAssumption(0.12)

    def pv(cpr: float) -> float:
        flows = project_pass_through(pool, PrepaymentAssumption(cpr)).cashflows
        return price_cashflows(
            tuple((f.payment_date, f.amount) for f in flows),
            CURVE,
            spread=0.02,
            settlement_date=START,
        )

    risk = cpr_sensitivity(pool, base, START, CURVE, spread_bp=200.0)
    assert risk.pv_rub == pytest.approx(pv(0.12), abs=1e-9)
    assert risk.dpv_per_cpr_pct_rub == pytest.approx((pv(0.13) - pv(0.11)) / 2.0, abs=1e-9)
    fine = cpr_sensitivity(pool, base, START, CURVE, spread_bp=200.0, cpr_bump=0.001)
    assert fine.dpv_per_cpr_pct_rub == pytest.approx(risk.dpv_per_cpr_pct_rub, rel=1e-3)
    calibrated = cpr_sensitivity(pool, base, START, CURVE, dirty_price_rub=risk.pv_rub)
    assert calibrated.spread_bp == pytest.approx(200.0, abs=1e-6)
    edge = cpr_sensitivity(pool, PrepaymentAssumption(0.0), START, CURVE, spread_bp=200.0)
    assert edge.difference == "forward" and edge.pv_down_rub == edge.pv_rub
    with pytest.raises(InvalidTerms, match="exactly one"):
        cpr_sensitivity(pool, base, START, CURVE)


def test_prepayment_adjusted_yield_round_trips() -> None:
    pool = terms(annuity_periods(36), rate=0.14)
    assumption = PrepaymentAssumption(0.2)
    settle = START + timedelta(days=10)
    instrument = pass_through_instrument(pool, assumption)
    dirty = 101.0 / 100.0 * 1000.0 + instrument.accrued(settle)
    analytics = pass_through_analytics(pool, assumption, settle, dirty_price_rub=dirty)
    flows = [f for f in project_pass_through(pool, assumption).cashflows if f.payment_date > settle]
    repriced = math.fsum(
        f.amount / (1.0 + analytics.effective_yield) ** year_fraction(settle, f.payment_date)
        for f in flows
    )
    assert repriced == pytest.approx(dirty, abs=1e-8)
    assert analytics.wal_years == pytest.approx(weighted_average_life(flows, settle), abs=1e-15)
    assert analytics.assumption == "prepay:constant_cpr:0.2:no-cleanup-call"


def test_projected_instrument_carries_the_flows_and_the_pool_accrued() -> None:
    pool = terms(annuity_periods(24, 800.0), rate=0.14, face=800.0, original=1000.0)
    assumption = PrepaymentAssumption(0.25)
    instrument = pass_through_instrument(pool, assumption)
    projection = project_pass_through(pool, assumption)
    assert instrument.initial_face == 800.0
    assert [(f.payment_date, f.coupon, f.principal) for f in instrument.cashflows()] == [
        (f.payment_date, f.coupon, f.principal) for f in projection.cashflows
    ]
    settle = START + timedelta(days=45)  # 15 days into the second period
    second = projection.rows[1]
    assert instrument.accrued(settle) == round(second.opening_balance * 0.14 * 15 / 365, 2)


def test_cleanup_call_redeems_the_tail_only_when_assumed() -> None:
    pool = terms(even_periods(60, 30), cleanup=0.1, payments_per_year=12)
    runs = project_pass_through(pool, PrepaymentAssumption(0.4))
    called = project_pass_through(pool, PrepaymentAssumption(0.4, exercise_cleanup_call=True))
    assert len(runs.rows) == 60
    (cut,) = [r for r in called.rows if r.cleanup_principal > 0.0]
    assert cut is called.rows[-1] and len(called.rows) < 60
    assert cut.opening_balance > 100.0
    assert cut.cleanup_principal <= 100.0 + 1e-9
    assert called.rows[-2].closing_balance > 100.0
    assert math.fsum(r.principal for r in called.rows) == pytest.approx(1000.0, abs=1e-9)
    assert called.cashflows[-1].kind is CashflowKind.MATURITY


def test_terms_and_assumptions_are_validated_and_serialize() -> None:
    with pytest.raises(InvalidTerms, match="contiguous"):
        terms(
            (
                PassThroughPeriod(START, date(2027, 2, 1), date(2027, 2, 1)),
                PassThroughPeriod(date(2027, 2, 2), date(2027, 3, 1), date(2027, 3, 1)),
            )
        )
    with pytest.raises(InvalidTerms, match="exceeds"):
        terms(even_periods(2, 30, scheduled=600.0))
    with pytest.raises(InvalidTerms, match="CPR"):
        PrepaymentAssumption(1.0)
    with pytest.raises(InvalidTerms, match="current face"):
        terms(even_periods(2, 30), face=1001.0)
    pool = terms(annuity_periods(6), cleanup=0.1)
    assert from_dict(PassThroughTerms, to_dict(pool)) == pool
    assumption = PrepaymentAssumption(0.1, exercise_cleanup_call=True)
    assert from_dict(PrepaymentAssumption, to_dict(assumption)) == assumption
