"""Strict, dependency-free bridge from market-data-store schedule responses."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import replace
from datetime import date

from ._money import non_negative_money
from .analytics import canonical_sha256
from .cashflows import BondCashflow
from .local_worker import PublicBondSnapshot


def _amount(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be a finite nonnegative number")
    return non_negative_money(value, name)


def bind_market_data_store_schedule(
    terms: PublicBondSnapshot,
    payload: Mapping[str, object],
    *,
    financing_curve_build_id: int,
) -> PublicBondSnapshot:
    """Bind a published fixed schedule to independently validated terms/quote/curve.

    ``terms.cashflows`` must be empty. The upstream adapter must still establish
    complete contractual coverage, currency, calendar and confirmed redemption;
    an ``exact_published`` service label alone is insufficient evidence of those.
    The expected build ID must come from the supplied curve's own provenance.
    """
    if terms.cashflows:
        raise ValueError("terms must have empty cashflows before binding")
    if terms.cashflow_currency != "RUB" or terms.coupon_model != "fixed":
        raise ValueError("only validated fixed RUB terms are supported")
    if terms.schedule_complete is not True or terms.redemption_confirmed is not True:
        raise ValueError("complete schedule and confirmed redemption are required")
    if payload.get("secid") != terms.instrument_id:
        raise ValueError("schedule instrument does not match terms")
    if payload.get("settlement_date") != terms.settlement_date.isoformat():
        raise ValueError("schedule settlement does not match terms")
    if terms.curve.curve_date != terms.settlement_date:
        raise ValueError("market-data-store financing curve must be effective at settlement")
    if payload.get("horizon_date") != terms.horizon_date.isoformat():
        raise ValueError("schedule horizon date does not match terms")
    horizon = {"maturity": "maturity", "confirmed_investor_put": "offer"}.get(terms.horizon)
    if horizon is None or payload.get("horizon_type") != horizon:
        raise ValueError("schedule horizon type does not match terms")
    if payload.get("formula_type") not in ("fixed", "fixed_rate"):
        raise ValueError("explicit fixed formula classification is required")
    if payload.get("calculation_quality") != "exact_published" or payload.get("warnings") != []:
        raise ValueError("schedule must be exact_published without unresolved warnings")
    if payload.get("projection_curve") is not None:
        raise ValueError("projected cashflows cannot enter fixed-cashflow risk")
    if (
        isinstance(financing_curve_build_id, bool)
        or not isinstance(financing_curve_build_id, int)
        or financing_curve_build_id <= 0
        or type(payload.get("financing_curve_build_id")) is not int
        or payload.get("financing_curve_build_id") != financing_curve_build_id
    ):
        raise ValueError("schedule financing curve build does not match supplied curve")
    rows = payload.get("cashflows")
    if not isinstance(rows, list) or not rows:
        raise ValueError("schedule cashflows are missing")
    flows: list[BondCashflow] = []
    previous: date | None = None
    terminal_principal = 0.0
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("payment_date"), str):
            raise TypeError("invalid cashflow row")
        payment = date.fromisoformat(row["payment_date"])
        if previous is not None and payment <= previous:
            raise ValueError("cashflow dates must be strictly increasing")
        if payment < terms.settlement_date or payment > terms.horizon_date:
            raise ValueError("cashflow outside the settlement/redemption horizon")
        previous = payment
        coupon = _amount(row.get("coupon"), "coupon")
        principal = _amount(row.get("principal"), "principal")
        total = _amount(row.get("total"), "total")
        if not math.isclose(coupon + principal, total, rel_tol=1e-12, abs_tol=1e-10):
            raise ValueError("cashflow total does not equal coupon plus principal")
        if row.get("coupon_source") not in (None, "published_fixed"):
            raise ValueError("coupon is not published fixed cash")
        if coupon > 0 and row.get("coupon_source") != "published_fixed":
            raise ValueError("positive coupon has no published fixed source")
        if payment == terms.horizon_date:
            terminal_principal = principal
        flows.append(BondCashflow(payment, total))
    if terminal_principal <= 0:
        raise ValueError("positive principal redemption at the horizon is required")
    return replace(
        terms,
        cashflows=tuple(flows),
        source_payload_id=canonical_sha256(dict(payload)),
    )
