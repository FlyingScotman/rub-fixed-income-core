"""Amortizing pass-through MBS/ABS under a deterministic constant CPR (package 9b).

This is explicitly a **deterministic-CPR model**: prepayments follow one constant
conditional prepayment rate given by the caller, not a prepayment model of rates,
seasoning or burnout. Rate risk is therefore computed with the projected flows held
fixed, and CPR risk is reported separately (:func:`cpr_sensitivity`).

Per payment period ``k`` (balance ``B`` at the period start, per bond):

- coupon: ``B * rate * days / year`` by the terms' :class:`AccrualBasis` (unrounded);
- scheduled principal: the terms' zero-prepayment amount ``S0_k`` scaled by the
  surviving balance, ``S_k = S0_k * B / B0`` where ``B0`` is the scheduled balance
  at the period start (the standard scheduled-balance ratio); with CPR 0 the flows
  are the schedule exactly;
- prepayment: ``SMM * (B - S_k)`` with ``SMM = 1 - (1 - CPR) ** (1 / payments_per_year)``;
- the last period pays whatever balance remains (legal final maturity);
- with ``exercise_cleanup_call`` and a clean-up threshold in the terms, the issuer
  redeems the whole balance on the first payment date after which the balance would
  be at or below ``cleanup_call_fraction * original_face``.

WAL is ``sum(t_k * principal_k) / sum(principal_k)`` with ``t`` in ACT/365F years
from settlement.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from itertools import pairwise
from typing import Literal

from ._money import finite_money, non_negative_money, positive_money
from ._risk_common import ShockedCurveCache, _base_curve
from .bond import YieldRequest, solve_effective_annual_yield
from .cashflows import BondCashflow, Cashflow, CashflowKind
from .compatibility import bond_duration, price_cashflows, solve_z_spread
from .conventions import ZSpreadConvention
from .dates import year_fraction
from .discount import CurveBootstrapRequest, DiscountCurve
from .domain import (
    AccrualBasis,
    Instrument,
    PrincipalLeg,
    ProductType,
    RateAccrualCouponLeg,
    RateCouponPeriod,
    Redemption,
)
from .errors import InvalidTerms

__all__ = [
    "PREPAYMENT_MODEL_VERSION",
    "CprSensitivity",
    "PassThroughAnalytics",
    "PassThroughPeriod",
    "PassThroughProjection",
    "PassThroughRow",
    "PassThroughTerms",
    "PrepaymentAssumption",
    "cpr_sensitivity",
    "pass_through_analytics",
    "pass_through_instrument",
    "project_pass_through",
    "weighted_average_life",
]

PREPAYMENT_MODEL_VERSION = "rub-constant-cpr-v1"
_BALANCE_EPS = 1e-9


@dataclass(frozen=True)
class PassThroughPeriod:
    """One payment period; ``scheduled_principal`` is per bond at zero prepayment."""

    accrual_start: date
    accrual_end: date
    payment_date: date
    scheduled_principal: float = 0.0

    def __post_init__(self) -> None:
        non_negative_money(self.scheduled_principal, "scheduled principal")
        if self.accrual_end <= self.accrual_start:
            raise InvalidTerms("accrual period must have positive length")
        if self.payment_date < self.accrual_end:
            raise InvalidTerms("a payment cannot precede its accrual period end")


@dataclass(frozen=True)
class PassThroughTerms:
    """A pass-through from its current state: the remaining periods and balance.

    ``current_face`` is the face per bond outstanding at the first period's start
    (the reported pool factor times ``original_face``). Periods are contiguous; the
    last one is the legal final payment. The scheduled principal of all periods
    cannot exceed ``current_face``; the last period pays any remainder.
    """

    instrument_id: str
    currency: str
    original_face: float
    current_face: float
    coupon_rate: float
    accrual_basis: AccrualBasis
    periods: tuple[PassThroughPeriod, ...]
    payments_per_year: int
    calendar_id: str
    settlement_lag_days: int = 1
    cleanup_call_fraction: float | None = None
    isin: str | None = None
    issuer_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.instrument_id, str) or not self.instrument_id.strip():
            raise InvalidTerms("instrument_id must be a non-empty string")
        if not isinstance(self.calendar_id, str) or not self.calendar_id.strip():
            raise InvalidTerms("calendar_id must be a non-empty string")
        positive_money(self.original_face, "original face")
        positive_money(self.current_face, "current face")
        if self.current_face > self.original_face * (1.0 + 1e-12):
            raise InvalidTerms("current face cannot exceed the original face")
        if non_negative_money(self.coupon_rate, "coupon rate") >= 1.0:
            raise InvalidTerms("coupon rate is a decimal below 1")
        if not isinstance(self.accrual_basis, AccrualBasis):
            raise TypeError("accrual_basis must be an AccrualBasis")
        if isinstance(self.payments_per_year, bool) or not isinstance(self.payments_per_year, int):
            raise TypeError("payments_per_year must be an int")
        if self.payments_per_year < 1:
            raise InvalidTerms("payments_per_year must be at least 1")
        if isinstance(self.settlement_lag_days, bool) or not isinstance(
            self.settlement_lag_days, int
        ):
            raise TypeError("settlement_lag_days must be an int")
        if self.settlement_lag_days < 0:
            raise InvalidTerms("settlement lag cannot be negative")
        object.__setattr__(self, "periods", tuple(self.periods))
        if not self.periods:
            raise InvalidTerms("a pass-through needs at least one period")
        for left, right in pairwise(self.periods):
            if right.accrual_start != left.accrual_end:
                raise InvalidTerms("pass-through periods must be contiguous")
            if right.payment_date <= left.payment_date:
                raise InvalidTerms("payment dates must increase")
        scheduled = math.fsum(p.scheduled_principal for p in self.periods)
        if scheduled > self.current_face * (1.0 + 1e-12) + _BALANCE_EPS:
            raise InvalidTerms("scheduled principal exceeds the current face")
        if self.cleanup_call_fraction is not None:
            fraction = finite_money(self.cleanup_call_fraction, "clean-up call fraction")
            if not 0.0 < fraction < 1.0:
                raise InvalidTerms("clean-up call fraction must be between 0 and 1")

    @property
    def legal_final_date(self) -> date:
        return self.periods[-1].payment_date


@dataclass(frozen=True)
class PrepaymentAssumption:
    """A constant annual CPR (decimal, ``0 <= cpr < 1``) and the clean-up call choice."""

    cpr: float
    exercise_cleanup_call: bool = False
    model: Literal["constant_cpr"] = "constant_cpr"

    def __post_init__(self) -> None:
        cpr = finite_money(self.cpr, "CPR")
        if not 0.0 <= cpr < 1.0:
            raise InvalidTerms("CPR must be in [0, 1)")
        if not isinstance(self.exercise_cleanup_call, bool):
            raise TypeError("exercise_cleanup_call must be boolean")
        if self.model != "constant_cpr":
            raise InvalidTerms("only the constant_cpr prepayment model is supported")

    def smm(self, payments_per_year: int) -> float:
        """Single-period prepayment rate: ``1 - (1 - CPR) ** (1 / payments_per_year)``."""
        return 1.0 - math.pow(1.0 - self.cpr, 1.0 / payments_per_year)

    @property
    def identifier(self) -> str:
        cleanup = "cleanup-call" if self.exercise_cleanup_call else "no-cleanup-call"
        return f"prepay:{self.model}:{self.cpr!r}:{cleanup}"


@dataclass(frozen=True)
class PassThroughRow:
    accrual_start: date
    accrual_end: date
    payment_date: date
    opening_balance: float
    coupon: float
    scheduled_principal: float
    prepaid_principal: float
    cleanup_principal: float
    closing_balance: float

    @property
    def principal(self) -> float:
        return self.scheduled_principal + self.prepaid_principal + self.cleanup_principal


@dataclass(frozen=True)
class PassThroughProjection:
    instrument_id: str
    assumption: PrepaymentAssumption
    smm: float
    rows: tuple[PassThroughRow, ...]
    cashflows: tuple[Cashflow, ...]
    model_version: str = PREPAYMENT_MODEL_VERSION


@dataclass(frozen=True)
class PassThroughAnalytics:
    """Prepayment-adjusted yield (effective annual, ACT/365F), WAL and durations."""

    instrument_id: str
    settlement_date: date
    assumption: str
    dirty_price_rub: float
    effective_yield: float
    wal_years: float
    macaulay_duration_years: float
    modified_duration_years: float
    model_version: str = PREPAYMENT_MODEL_VERSION


@dataclass(frozen=True)
class CprSensitivity:
    """PV and WAL at ``cpr`` and at ``cpr +/- cpr_bump``, with the Z-spread held.

    ``dpv_per_cpr_pct_rub`` is the PV change per bond for +1 percentage point of CPR
    (central difference, or forward when ``cpr < cpr_bump``; see ``difference``).
    """

    instrument_id: str
    settlement_date: date
    cpr: float
    cpr_bump: float
    difference: Literal["central", "forward"]
    spread_bp: float
    spread_convention: str
    pv_rub: float
    pv_up_rub: float
    pv_down_rub: float
    dpv_per_cpr_pct_rub: float
    wal_years: float
    wal_up_years: float
    wal_down_years: float
    model_version: str = PREPAYMENT_MODEL_VERSION


def project_pass_through(
    terms: PassThroughTerms, assumption: PrepaymentAssumption
) -> PassThroughProjection:
    """Prepayment-adjusted coupons and principal for every remaining period."""
    if not isinstance(assumption, PrepaymentAssumption):
        raise TypeError("assumption must be a PrepaymentAssumption")
    smm = assumption.smm(terms.payments_per_year)
    threshold = (
        terms.cleanup_call_fraction * terms.original_face
        if assumption.exercise_cleanup_call and terms.cleanup_call_fraction is not None
        else None
    )
    balance = terms.current_face
    scheduled_balance = terms.current_face
    rows: list[PassThroughRow] = []
    paid: list[float] = []
    last = len(terms.periods) - 1
    for index, period in enumerate(terms.periods):
        days, year = terms.accrual_basis.days(period.accrual_start, period.accrual_end)
        coupon = balance * terms.coupon_rate * days / year
        if index == last:
            scheduled, prepaid, cleanup = balance, 0.0, 0.0
        else:
            ratio = balance / scheduled_balance if scheduled_balance > _BALANCE_EPS else 0.0
            scheduled = min(period.scheduled_principal * ratio, balance) if ratio > 0.0 else balance
            prepaid = smm * (balance - scheduled)
            cleanup = 0.0
            after = balance - scheduled - prepaid
            if threshold is not None and after <= threshold + _BALANCE_EPS:
                cleanup = after
        principal = scheduled + prepaid + cleanup
        # The final payment clears the balance exactly, so redemptions sum to the face.
        closing = 0.0 if index == last or cleanup > 0.0 else balance - principal
        if closing <= _BALANCE_EPS:
            closing = 0.0
            principal = terms.current_face - math.fsum(paid)
            scheduled = principal - prepaid - cleanup
        rows.append(
            PassThroughRow(
                period.accrual_start,
                period.accrual_end,
                period.payment_date,
                balance,
                coupon,
                scheduled,
                prepaid,
                cleanup,
                closing,
            )
        )
        paid.append(principal)
        scheduled_balance -= period.scheduled_principal
        balance = closing
        if closing == 0.0:
            break
    flows = tuple(
        Cashflow.split(
            row.payment_date,
            coupon=row.coupon,
            principal=row.principal,
            accrual_start=row.accrual_start,
            accrual_end=row.accrual_end,
            kind=(
                CashflowKind.COUPON
                if row.principal == 0.0
                else CashflowKind.MATURITY
                if row.closing_balance == 0.0
                else CashflowKind.AMORTIZATION
            ),
        )
        for row in rows
    )
    return PassThroughProjection(terms.instrument_id, assumption, smm, tuple(rows), flows)


def weighted_average_life(cashflows: Sequence[Cashflow], settlement: date) -> float:
    """Principal-weighted average time (ACT/365F years) of flows after settlement."""
    future = [f for f in cashflows if f.payment_date > settlement and f.principal > 0.0]
    total = math.fsum(f.principal for f in future)
    if total <= 0.0:
        raise InvalidTerms("no principal remains after settlement")
    return math.fsum(year_fraction(settlement, f.payment_date) * f.principal for f in future) / (
        total
    )


def pass_through_instrument(
    terms: PassThroughTerms, assumption: PrepaymentAssumption
) -> Instrument:
    """The projected flows as a deterministic fixed-coupon bond.

    The bond's ``initial_face`` is the terms' ``current_face`` (the projection starts
    there). Coupons accrue on the rate (``RateAccrualCouponLeg``) on the projected
    balance at each period start, so accrued interest at settlement is the pool's.
    Valued anywhere a fixed bond is (portfolio, horizon, risk), the CPR is held fixed.
    """
    projection = project_pass_through(terms, assumption)
    coupons = tuple(
        RateCouponPeriod(
            r.accrual_start, r.accrual_end, r.payment_date, r.coupon, terms.coupon_rate
        )
        for r in projection.rows
    )
    principal = [r for r in projection.rows if r.principal > 0.0]
    redemptions = tuple(
        Redemption(
            r.payment_date,
            r.principal,
            CashflowKind.MATURITY if i == len(principal) - 1 else CashflowKind.AMORTIZATION,
        )
        for i, r in enumerate(principal)
    )
    return Instrument(
        terms.instrument_id,
        ProductType.FIXED_BOND,
        terms.currency,
        terms.current_face,
        RateAccrualCouponLeg(coupons, terms.accrual_basis),
        PrincipalLeg(redemptions),
        terms.calendar_id,
        settlement_lag_days=terms.settlement_lag_days,
        isin=terms.isin,
        issuer_id=terms.issuer_id,
    )


def _future(flows: Sequence[Cashflow], settlement: date) -> tuple[Cashflow, ...]:
    future = tuple(f for f in flows if f.payment_date > settlement)
    if not future:
        raise InvalidTerms("no cashflows remain after settlement")
    return future


def pass_through_analytics(
    terms: PassThroughTerms,
    assumption: PrepaymentAssumption,
    settlement: date,
    *,
    dirty_price_rub: float,
) -> PassThroughAnalytics:
    """Prepayment-adjusted effective yield, WAL and durations at a dirty price."""
    dirty = positive_money(dirty_price_rub, "dirty price")
    flows = _future(project_pass_through(terms, assumption).cashflows, settlement)
    solved = solve_effective_annual_yield(YieldRequest(settlement, dirty, flows))
    duration = bond_duration(
        annual_yield=solved.effective_annual_yield,
        settlement_date=settlement,
        cashflows=tuple((f.payment_date, f.amount) for f in flows),
    )
    return PassThroughAnalytics(
        terms.instrument_id,
        settlement,
        assumption.identifier,
        dirty,
        solved.effective_annual_yield,
        weighted_average_life(flows, settlement),
        duration.macaulay_years,
        duration.modified_years,
    )


def _pairs(flows: Sequence[BondCashflow]) -> tuple[tuple[date, float], ...]:
    return tuple((f.payment_date, f.amount) for f in flows)


def cpr_sensitivity(
    terms: PassThroughTerms,
    assumption: PrepaymentAssumption,
    settlement: date,
    curve: DiscountCurve | CurveBootstrapRequest,
    *,
    spread_bp: float | None = None,
    dirty_price_rub: float | None = None,
    spread_convention: ZSpreadConvention | None = None,
    cpr_bump: float = 0.01,
    curve_cache: ShockedCurveCache | None = None,
) -> CprSensitivity:
    """PV and WAL sensitivity to the CPR at a held Z-spread over ``curve``.

    Pass exactly one of ``spread_bp`` and ``dirty_price_rub`` (which calibrates the
    spread on the base CPR's flows). ``cpr_bump`` is a decimal (0.01 = 1 pp).
    """
    convention = ZSpreadConvention() if spread_convention is None else spread_convention
    bump = positive_money(cpr_bump, "CPR bump")
    if assumption.cpr + bump >= 1.0:
        raise InvalidTerms("CPR plus the bump must stay below 1")
    base = _base_curve(curve, ShockedCurveCache() if curve_cache is None else curve_cache)
    if settlement < base.curve_date:
        raise InvalidTerms("settlement cannot precede the curve date")
    if (spread_bp is None) == (dirty_price_rub is None):
        raise InvalidTerms("pass exactly one of spread_bp and dirty_price_rub")

    def flows_at(cpr: float) -> tuple[Cashflow, ...]:
        shifted = PrepaymentAssumption(cpr, assumption.exercise_cleanup_call, assumption.model)
        return _future(project_pass_through(terms, shifted).cashflows, settlement)

    flows = flows_at(assumption.cpr)
    if spread_bp is not None:
        spread = finite_money(spread_bp, "spread bp")
    else:
        dirty = positive_money(dirty_price_rub, "dirty price")  # type: ignore[arg-type]
        spread = (
            solve_z_spread(
                _pairs(flows), dirty, base, settlement_date=settlement, convention=convention
            )
            * 10_000.0
        )

    def pv(values: Sequence[Cashflow]) -> float:
        return price_cashflows(
            _pairs(values),
            base,
            spread=spread / 10_000.0,
            settlement_date=settlement,
            convention=convention,
        )

    difference: Literal["central", "forward"] = "central" if assumption.cpr >= bump else "forward"
    down_cpr = assumption.cpr - bump if difference == "central" else assumption.cpr
    up, down = flows_at(assumption.cpr + bump), flows_at(down_cpr)
    pv_base, pv_up, pv_down = pv(flows), pv(up), pv(down)
    width = (assumption.cpr + bump - down_cpr) * 100.0
    return CprSensitivity(
        terms.instrument_id,
        settlement,
        assumption.cpr,
        bump,
        difference,
        spread,
        convention.identifier,
        pv_base,
        pv_up,
        pv_down,
        finite_money((pv_up - pv_down) / width, "CPR sensitivity"),
        weighted_average_life(flows, settlement),
        weighted_average_life(up, settlement),
        weighted_average_life(down, settlement),
    )
