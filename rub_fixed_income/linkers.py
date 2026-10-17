"""Inflation-linked bonds over the domain model (package 9a): face, accrued, flows.

An :class:`~rub_fixed_income.domain.Instrument` with an
:class:`~rub_fixed_income.inflation.IndexedPrincipalLeg` has no market-independent
cash flows: its face on a date is the CPI-indexed nominal. These functions read it from
a caller-supplied :class:`~rub_fixed_income.inflation.CpiSeries`.

**Real cash flows.** For valuation the future flows are taken at the indexed nominal of
the settlement date, held constant: coupons ``N(S) * rate * days / year`` (rounded as a
coupon is) and the redemption ``max(N(S), initial face)`` when the terms floor it. These
are the flows in settlement-date money, discounted on a *real* curve, and their
effective yield is the real yield MOEX publishes for OFZ-IN (``YIELDCLOSE``). Future
indexation is not projected; the deflation floor is applied at intrinsic value only.
"""

from __future__ import annotations

from datetime import date

from .cashflows import Cashflow, CashflowKind
from .domain import Instrument, ProductType
from .errors import InvalidTerms
from .inflation import (
    CpiSeries,
    IndexedCouponLeg,
    IndexedCouponPeriod,
    IndexedPrincipalLeg,
    indexed_accrued,
    indexed_coupon_amount,
)

__all__ = [
    "linker_accrued",
    "linker_coupon",
    "linker_nominal",
    "linker_redemption",
    "real_cashflows",
]


def _principal(instrument: Instrument) -> IndexedPrincipalLeg:
    if not isinstance(instrument, Instrument):
        raise TypeError("instrument must be an Instrument")
    leg = instrument.principal_leg
    if not isinstance(leg, IndexedPrincipalLeg):
        raise InvalidTerms(f"{instrument.instrument_id} has no indexed principal leg")
    if instrument.product is not ProductType.INFLATION_LINKED_BOND:  # pragma: no cover
        raise InvalidTerms("an indexed principal leg belongs to an inflation-linked bond")
    return leg


def linker_nominal(instrument: Instrument, series: CpiSeries, on: date) -> float:
    """Indexed face per bond on ``on`` (formulas 1-3), rounded as the terms publish it."""
    leg = _principal(instrument)
    return leg.indexation.nominal(instrument.initial_face, series, on)


def linker_coupon(instrument: Instrument, series: CpiSeries, period: IndexedCouponPeriod) -> float:
    """The coupon of ``period``: formula (4) on the nominal indexed to its payment date.

    Needs the CPI months of the payment date; raises ``MissingMarketData`` when they
    are not yet published (the coupon is then not known).
    """
    leg = instrument.coupon_leg
    if not isinstance(leg, IndexedCouponLeg) or period not in leg.periods:
        raise InvalidTerms("the period is not a coupon of this instrument")
    nominal = linker_nominal(instrument, series, period.payment_date)
    return indexed_coupon_amount(nominal, period, leg.day_count)


def linker_accrued(instrument: Instrument, series: CpiSeries, on: date) -> float:
    """Accrued interest per bond on ``on`` (formula 5), on the nominal of ``on``.

    0 on a period end and after the last period, as for every other bond; a date before
    the first accrual period is rejected.
    """
    _principal(instrument)
    leg = instrument.coupon_leg
    if leg is None:
        return 0.0
    if not isinstance(leg, IndexedCouponLeg):  # pragma: no cover - Instrument validates
        raise InvalidTerms("an indexed principal pays coupons from an indexed coupon leg")
    period = leg.period_on(on)
    if period is None:
        if on >= leg.periods[-1].accrual_end:
            return 0.0
        if on < leg.periods[0].accrual_start:
            raise InvalidTerms("no coupon accrual period contains the date")
        return 0.0  # between periods: nothing accrues
    return indexed_accrued(linker_nominal(instrument, series, on), period, on, leg.day_count)


def linker_redemption(instrument: Instrument, nominal: float) -> float:
    """Principal paid at maturity for an indexed nominal ``nominal`` on that date."""
    return _principal(instrument).redemption(instrument.initial_face, nominal)


def real_cashflows(
    instrument: Instrument, series: CpiSeries, settlement: date
) -> tuple[Cashflow, ...]:
    """Flows paid after ``settlement`` at the settlement-date nominal, held constant.

    See the module docstring: coupons by formula (4) with ``N(settlement)`` and the
    redemption ``max(N(settlement), initial face)`` when floored. The result is empty
    when nothing is paid after settlement.
    """
    leg = _principal(instrument)
    if settlement >= leg.maturity_date:
        return ()
    nominal = linker_nominal(instrument, series, settlement)
    coupons: dict[date, IndexedCouponPeriod] = {}
    coupon_leg = instrument.coupon_leg
    if isinstance(coupon_leg, IndexedCouponLeg):
        coupons = {p.payment_date: p for p in coupon_leg.periods if p.payment_date > settlement}
    maturity = leg.maturity_date
    flows: list[Cashflow] = []
    for paid in sorted(set(coupons) | ({maturity} if maturity > settlement else set())):
        period = coupons.get(paid)
        coupon = (
            0.0
            if period is None or not isinstance(coupon_leg, IndexedCouponLeg)
            else indexed_coupon_amount(nominal, period, coupon_leg.day_count)
        )
        principal = leg.redemption(instrument.initial_face, nominal) if paid == maturity else 0.0
        if coupon == 0.0 and principal == 0.0:
            continue
        flows.append(
            Cashflow.split(
                paid,
                coupon=coupon,
                principal=principal,
                accrual_start=None if period is None else period.accrual_start,
                accrual_end=None if period is None else period.accrual_end,
                kind=CashflowKind.MATURITY if principal else CashflowKind.COUPON,
            )
        )
    return tuple(flows)
