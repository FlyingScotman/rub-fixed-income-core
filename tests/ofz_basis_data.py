"""Synthetic RUONIA OIS quotes shaped like the KBD of 15.10.2026 (issue #83 tests).

Each quote is the KBD annual par yield at its maturity plus a basis: OIS above the KBD at the
short end (the KBD there is MOEX's extrapolation), 40-60 bp below it from 2Y on, roughly the
par-par basis seen on hub data in October 2026. Quotes run 1W-10Y under
``rub_ruonia_ois_moex_v2`` on a weekday calendar (no range bound, so 15Y/20Y resolve).
"""

from __future__ import annotations

import math
from dataclasses import replace
from datetime import date, timedelta

from test_kbd import load

from rub_fixed_income import (
    BondCashflow,
    BondRiskEngine,
    BondRiskRequest,
    BusinessCalendar,
    CurveBootstrapRequest,
    ZSpreadConvention,
    kbd_discount_curve,
    ois_curve_convention,
    ois_quotes,
)
from rub_fixed_income.dates import add_months
from rub_fixed_income.ofz_basis import _annual_par_yield

PARAMS, _ = load("zcyc_2026-10-15.json")
DATE = PARAMS.trade_date
KBD = kbd_discount_curve(PARAMS, zero_shock="annual_par")
OIS_CALENDAR = BusinessCalendar(holidays=frozenset({date(2026, 11, 4)}), version="synthetic-ois")
TENORS = ("1W", "2W", "1M", "2M", "3M", "6M", "9M", "1Y", *(f"{y}Y" for y in range(2, 11)))
SETTLEMENT = DATE + timedelta(days=1)


def basis(years: float) -> float:
    """OIS minus KBD par, decimal."""
    return 0.015 * math.exp(-years / 0.5) - 0.006 * (1.0 - math.exp(-years / 1.5))


def ois_request(tenors: tuple[str, ...] = TENORS) -> CurveBootstrapRequest:
    quotes = ois_quotes(DATE, [(t, 0.15) for t in tenors], OIS_CALENDAR)
    quotes = tuple(
        replace(
            q,
            par_rate=_annual_par_yield(KBD, q.maturity) + basis((q.maturity - DATE).days / 365.0),
        )
        for q in quotes
    )
    convention = ois_curve_convention("rub_ruonia_ois_moex_v2", OIS_CALENDAR)
    return CurveBootstrapRequest(DATE, quotes, convention)


OIS = ois_request()


def semiannual_flows(maturity: date, coupon: float) -> tuple[BondCashflow, ...]:
    """A semiannual bullet of face 1000 paying ``coupon`` (decimal a year)."""
    dates = []
    k = 0
    while (pay := add_months(maturity, -6 * k)) > SETTLEMENT:
        dates.append(pay)
        k += 1
    flows = [BondCashflow(d, 1000.0 * coupon / 2.0) for d in sorted(dates)]
    flows.append(BondCashflow(maturity, 1000.0))
    return tuple(flows)


def kbd_bond(
    maturity: date, coupon: float, *, quantity: float = 1000.0, spread_bp: float = 0.0
) -> BondRiskEngine:
    """The bond on the v3 KBD, 1 mn RUB face by default (risk in RUB/bp per 1 mn)."""
    request = BondRiskRequest(
        semiannual_flows(maturity, coupon),
        SETTLEMENT,
        KBD,
        ZSpreadConvention(),
        "ofz",
        spread_bp=spread_bp,
        quantity=quantity,
    )
    return BondRiskEngine(request)
