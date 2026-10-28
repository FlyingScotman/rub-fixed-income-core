"""Synthetic never-called floating perpetuals (0.41.0, BACKLOG #127) on the floater book.

Three perpetuals with a coupon formula, all discounted on RUONIA OIS:

- ``PERP-RUONIA``: RUONIA compounded + 2.00 %, quarterly, projected off RUONIA OIS;
- ``PERP-KEY``: the key rate (arithmetic, business days) + 3.00 %, quarterly, projected
  off the key-rate projection curve;
- ``PERP-KBD``: reset every five years to the KBD 5Y + 3.25 %, paid semi-annually,
  fixed four publications before each reset (``fixing_anchor``); the current cycle
  (from 2024-12-16) is fixed by history.
"""

from __future__ import annotations

from datetime import date, timedelta

from test_portfolio import (
    AS_OF,
    BUSINESS_DATE,
    PARAMS,
)
from test_portfolio_floaters import CALENDAR, KEY_PROJECTION, KNOWN, OIS, key_rate, ruonia

from rub_fixed_income import (
    BondPriceObservation,
    CalendarEntry,
    CurveEntry,
    DayCount,
    ExerciseAssumption,
    FixingDateRule,
    FixingEntry,
    FixingHistory,
    FloatingCouponLeg,
    FloatingCouponPeriod,
    FloatingCouponRule,
    Instrument,
    MarketSnapshot,
    Offer,
    OfferKind,
    PrincipalLeg,
    ProductType,
    ValuationPolicy,
)
from rub_fixed_income.dates import add_months

ACT365 = DayCount.ACT_365_FIXED
RESET = date(2024, 12, 16)
KBD_FIXING = date(2024, 12, 10)  # four business days before the reset
KBD_RATE = 0.15
NEVER = ExerciseAssumption.never_called()


def back(day: date, count: int) -> date:
    for _ in range(count):
        day -= timedelta(days=1)
        while not CALENDAR.is_business_day(day):
            day -= timedelta(days=1)
    return day


assert back(RESET, 4) == KBD_FIXING


def kbd_history() -> FixingHistory:
    return FixingHistory(
        "kbd",
        BUSINESS_DATE,
        ((KBD_FIXING, KBD_RATE),),
        CALENDAR,
        "hub-zcyc",
        known_at_utc=KNOWN,
        tenor_years=5.0,
    )


def kbd_rule() -> FloatingCouponRule:
    return FloatingCouponRule(
        "kbd",
        "single_fixing",
        0.0325,
        ACT365,
        ACT365,
        rate_rounding_decimals=2,
        amount_rounding_decimals=2,
        fixing_date=FixingDateRule("accrual_start", 4, "business_days"),
        index_tenor_years=5.0,
    )


def perpetual(instrument_id: str, leg: FloatingCouponLeg, call: date) -> Instrument:
    return Instrument(
        instrument_id,
        ProductType.FLOATING_BOND,
        "RUB",
        1000.0,
        leg,
        PrincipalLeg(()),
        "MOEX_STOCK",
        offers=(Offer(call, OfferKind.CALL, confirmed=False),),
    )


def kbd_periods(count: int = 6) -> tuple[FloatingCouponPeriod, ...]:
    """The listed periods: semi-annual from the reset, all in its cycle."""
    return tuple(
        FloatingCouponPeriod(
            add_months(RESET, 6 * k),
            add_months(RESET, 6 * (k + 1)),
            add_months(RESET, 6 * (k + 1)),
            1000.0,
            fixing_anchor=RESET,
        )
        for k in range(count)
    )


def kbd_leg() -> FloatingCouponLeg:
    return FloatingCouponLeg(kbd_rule(), kbd_periods(), "KBD_5Y", "RUB_KBD")


def kbd_perpetual() -> Instrument:
    return perpetual("PERP-KBD", kbd_leg(), add_months(RESET, 60))


def quarterly(first: date, count: int) -> tuple[FloatingCouponPeriod, ...]:
    return tuple(
        FloatingCouponPeriod(
            add_months(first, 3 * k),
            add_months(first, 3 * (k + 1)),
            add_months(first, 3 * (k + 1)),
            1000.0,
        )
        for k in range(count)
    )


def ruonia_rule() -> FloatingCouponRule:
    return FloatingCouponRule(
        "ruonia", "compounded", 0.02, ACT365, ACT365, amount_rounding_decimals=2
    )


def key_rule() -> FloatingCouponRule:
    return FloatingCouponRule(
        "key_rate", "arithmetic", 0.03, ACT365, ACT365, amount_rounding_decimals=2
    )


def ruonia_perpetual() -> Instrument:
    leg = FloatingCouponLeg(ruonia_rule(), quarterly(date(2026, 9, 15), 3), "RUONIA", "RUB_OIS")
    return perpetual("PERP-RUONIA", leg, date(2031, 9, 15))


def key_perpetual() -> Instrument:
    leg = FloatingCouponLeg(key_rule(), quarterly(date(2026, 9, 15), 3), "KEY_RATE", "KEY_PROJ")
    return perpetual("PERP-KEY", leg, date(2031, 9, 15))


def instruments() -> tuple[Instrument, ...]:
    return (ruonia_perpetual(), key_perpetual(), kbd_perpetual())


PRICES = {"PERP-RUONIA": 101.0, "PERP-KEY": 99.0, "PERP-KBD": 96.0}


def snapshot() -> MarketSnapshot:
    return MarketSnapshot(
        "floating-perpetuals-2026-10-15",
        BUSINESS_DATE,
        AS_OF,
        curves=(
            CurveEntry("RUB_OIS", "rub_ruonia_ois_legacy_v1", OIS),
            CurveEntry("KEY_PROJ", "rub_key_rate_projection_v1", KEY_PROJECTION),
            CurveEntry("RUB_KBD", "rub_ofz_kbd_v2", PARAMS),
        ),
        calendars=(CalendarEntry("MOEX_STOCK", CALENDAR),),
        fixings=(
            FixingEntry("RUONIA", ruonia()),
            FixingEntry("KEY_RATE", key_rate()),
            FixingEntry("KBD_5Y", kbd_history()),
        ),
        quotes=tuple(BondPriceObservation(i, p, "waprice") for i, p in PRICES.items()),
    )


POLICY = ValuationPolicy("RUB_OIS", exercise_assumptions=tuple((i, NEVER) for i in PRICES))
