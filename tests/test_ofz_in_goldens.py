"""OFZ-IN goldens against MOEX ISS and Bank of Russia CPI (package 9a).

Fixtures and provenance: ``tests/fixtures/ofz_in/README.md``. Conventions under test
are formulas (1)-(5) of the Minfin terms as restated by the Bank of Russia
(``rub_fixed_income/inflation.py``, ``docs/linkers.md``).

What is exact and what is not:

- accrued interest and every paid coupon reproduce MOEX to the kopeck from the indexed
  face, on every EOD row of the four bonds in circulation;
- the indexed face from CPI is within 2.6e-4 of MOEX's (0.36 RUB at worst over 5,927
  days): the only reachable official CPI (cbr.ru) is month on month to 2 decimals, while
  Minfin indexes on Rosstat's index levels, whose sources are refused by this
  environment's proxy. That gap is recorded, not tuned away;
- the 3-month lag is the only one consistent with the data;
- the real yield and duration at ``CLOSE`` match MOEX ``YIELDCLOSE`` / ``DURATION``.
"""

from __future__ import annotations

import calendar
import math
from datetime import UTC, date, datetime

import pytest
from ofz_in_data import (
    CPI_ID,
    LINKERS,
    board,
    bondization,
    cbr_cpi_series,
    cbr_month_on_month,
    history,
    linker,
    rows,
)

from rub_fixed_income import (
    AccountingCategory,
    BondPriceObservation,
    BusinessCalendar,
    CalendarEntry,
    CurveEntry,
    DayCount,
    DiscountCurve,
    IndexedCouponLeg,
    MarketSnapshot,
    Position,
    ValuationPolicy,
    indexed_accrued,
    indexed_coupon_amount,
    linker_accrued,
    linker_coupon,
    linker_nominal,
    real_cashflows,
    value_portfolio,
)
from rub_fixed_income.inflation import add_months

TRADE = date(2026, 10, 16)  # Friday; settlement T+1 is Monday 2026-10-19
SETTLE = date(2026, 10, 19)
CALENDAR = BusinessCalendar(version="synthetic-weekends-only")


def leg(secid: str) -> IndexedCouponLeg:
    coupon_leg = linker(secid).coupon_leg
    assert isinstance(coupon_leg, IndexedCouponLeg)
    return coupon_leg


@pytest.mark.parametrize("secid", LINKERS)
def test_accrued_matches_moex_on_every_eod_row_from_the_indexed_face(secid: str) -> None:
    """Formula (5): ``round(N(date) * 2.5 % * days / 365, 0.01)``, N = MOEX FACEVALUE."""
    coupon_leg = leg(secid)
    checked = 0
    for row in history(secid):
        on = date.fromisoformat(row["TRADEDATE"])
        period = coupon_leg.period_on(on)
        assert period is not None
        accrued = indexed_accrued(row["FACEVALUE"], period, on, DayCount.ACT_365_FIXED)
        assert accrued == pytest.approx(row["ACCINT"], abs=1e-9), on
        checked += 1
    assert checked == len(history(secid)) > 900


@pytest.mark.parametrize("secid", LINKERS)
def test_paid_coupons_use_the_nominal_of_the_payment_date(secid: str) -> None:
    """Formula (4), checked on every coupon paid so far (45 across the four bonds).

    A 2015 MOEX trading note says the coupon uses the nominal of the last day of the
    calculation month; the paid amounts contradict it, and agree with the terms.
    """
    face = {row["TRADEDATE"]: row["FACEVALUE"] for row in history(secid)}
    paid = [c for c in rows(bondization(secid)["coupons"]) if c["coupondate"] <= "2026-10-16"]
    assert paid
    for coupon, period in zip(paid, leg(secid).periods, strict=False):
        nominal = face[coupon["coupondate"]]
        amount = indexed_coupon_amount(nominal, period, DayCount.ACT_365_FIXED)
        assert amount == pytest.approx(coupon["value"], abs=1e-9), coupon["coupondate"]
        month_end = period.payment_date.replace(day=1) - (date(2000, 1, 2) - date(2000, 1, 1))
        if month_end.isoformat() in face and coupon["coupondate"] in (
            "2018-08-15",
            "2019-02-13",
        ):
            wrong = indexed_coupon_amount(
                face[month_end.isoformat()], period, DayCount.ACT_365_FIXED
            )
            assert wrong != pytest.approx(coupon["value"], abs=1e-9)


def _face_errors(secid: str, lag_months: int) -> list[float]:
    bond = linker(secid, lag_months=lag_months)
    series = cbr_cpi_series()
    errors = []
    for row in history(secid):
        on = date.fromisoformat(row["TRADEDATE"])
        errors.append(linker_nominal(bond, series, on) / row["FACEVALUE"] - 1.0)
    return errors


@pytest.mark.parametrize("secid", LINKERS)
def test_indexed_face_from_cbr_cpi_tracks_moex_with_the_three_month_lag(secid: str) -> None:
    """Every EOD face of the bond from CPI, with lag 3, is within 5e-4 of MOEX.

    Observed worst case 2.6e-4 (SU52004RMFS7, 0.36 RUB): the CBR m/m figures are rounded
    to 0.01 pp and Minfin's reference levels are finer. A lag of 2 or 4 months is off by
    far more, so the lag itself is confirmed by the data.
    """
    assert max(abs(e) for e in _face_errors(secid, 3)) < 5e-4
    for wrong in (2, 4):
        assert max(abs(e) for e in _face_errors(secid, wrong)) > 3e-3


def test_monthly_growth_of_the_face_is_the_cpi_change_three_months_earlier() -> None:
    """Within each month the MOEX face moves linearly by day ((n - 1) / days, formula 3)
    and the month's growth equals CBR's m/m CPI of month M - 3 within 0.02 pp."""
    month_on_month = cbr_month_on_month()
    checked = 0
    for secid in LINKERS:
        by_month: dict[date, list[tuple[int, float]]] = {}
        for row in history(secid):
            on = date.fromisoformat(row["TRADEDATE"])
            by_month.setdefault(on.replace(day=1), []).append((on.day - 1, row["FACEVALUE"]))
        for month, points in by_month.items():
            reference = add_months(month, -3)
            if len(points) < 8 or reference not in month_on_month:
                continue
            n = len(points)
            mean_x = sum(x for x, _ in points) / n
            mean_y = sum(y for _, y in points) / n
            slope = sum((x - mean_x) * (y - mean_y) for x, y in points) / sum(
                (x - mean_x) ** 2 for x, _ in points
            )
            start = mean_y - slope * mean_x
            # Residuals of a least-squares line through kopeck-rounded faces; a step or a
            # wrong day count would leave residuals of tenths of a rouble.
            assert max(abs(start + slope * x - y) for x, y in points) < 0.01
            days = calendar.monthrange(month.year, month.month)[1]
            growth_pct = slope * days / start * 100.0
            assert growth_pct == pytest.approx(month_on_month[reference], abs=0.02), month
            checked += 1
    assert checked > 250


@pytest.mark.parametrize("secid", LINKERS)
def test_trade_date_face_and_accrued_from_cpi(secid: str) -> None:
    """On 2026-10-16 the CPI-indexed face is within 1e-4 of FACEVALUE and the accrued,
    computed end to end from CPI, equals MOEX ``ACCINT`` to the kopeck."""
    row = board()[secid]
    assert row["FACEVALUE_TYPE"] == "Индексированный"
    bond, series = linker(secid), cbr_cpi_series()
    assert linker_nominal(bond, series, TRADE) == pytest.approx(row["FACEVALUE"], rel=1e-4)
    assert linker_accrued(bond, series, TRADE) == pytest.approx(row["ACCINT"], abs=1e-9)


def test_next_coupon_is_known_once_its_cpi_months_are_published() -> None:
    """SU52002's 2027-02-03 coupon needs CPI for September and October 2026: not yet."""
    bond, series = linker("SU52002RMFS1"), cbr_cpi_series()
    coupon_leg = leg("SU52002RMFS1")
    upcoming = next(p for p in coupon_leg.periods if p.payment_date > TRADE)
    with pytest.raises(ValueError, match="2026-10"):
        linker_coupon(bond, series, upcoming)
    paid = next(p for p in coupon_leg.periods if p.payment_date == date(2026, 8, 5))
    assert linker_coupon(bond, series, paid) == pytest.approx(21.87, abs=0.011)


def real_curve(rate: float) -> DiscountCurve:
    """A flat continuous real curve as caller-built nodes (spec rub_real_discount_factors_v1)."""
    nodes = tuple(
        (date(TRADE.year + k, TRADE.month, TRADE.day), math.exp(-rate * k * 365.25 / 365))
        for k in range(1, 31)
    )
    return DiscountCurve(TRADE, nodes)


def _snapshot(curve: DiscountCurve) -> MarketSnapshot:
    quotes = tuple(
        BondPriceObservation(secid, board()[secid]["CLOSE"], "CLOSE") for secid in LINKERS
    )
    return MarketSnapshot(
        "moex-eod-2026-10-16",
        TRADE,
        datetime(2026, 10, 16, 20, 0, tzinfo=UTC),
        curves=(CurveEntry("RUB_REAL", "rub_real_discount_factors_v1", curve),),
        calendars=(CalendarEntry("MOEX_STOCK", CALENDAR),),
        quotes=quotes,
        cpi=(cbr_cpi_series(),),
    )


def test_real_yield_and_duration_match_moex_end_to_end() -> None:
    """``value_portfolio`` on the 2026-10-16 board: the effective yield of the real flows
    at ``CLOSE`` is MOEX ``YIELDCLOSE`` (published to 0.01 %) and the Macaulay duration
    is ``DURATION`` (days). The yield does not depend on the real curve."""
    snapshot = _snapshot(real_curve(0.06))
    policy = ValuationPolicy(
        "UNUSED_NOMINAL",
        spread_kind="ofz",
        curve_overrides=tuple((secid, "RUB_REAL") for secid in LINKERS),
    )
    positions = [
        Position(f"p-{secid}", secid, 10.0, "linkers", AccountingCategory.TRADING, "RUB")
        for secid in LINKERS
    ]
    result = value_portfolio(
        run_id="ofz-in-golden",
        as_of=datetime(2026, 10, 16, 21, 0, tzinfo=UTC),
        snapshot=snapshot,
        instruments=[linker(s) for s in LINKERS],
        positions=positions,
        policy=policy,
    )
    assert result.status == "complete", result.coverage.rejections
    assert [c.series_id for c in result.cpi] == [CPI_ID]
    for valuation in result.positions:
        row = board()[valuation.instrument_id]
        assert valuation.settlement_date == SETTLE
        assert valuation.effective_yield * 100.0 == pytest.approx(row["YIELDCLOSE"], abs=0.006)
        assert valuation.macaulay_duration_years * 365.0 == pytest.approx(row["DURATION"], abs=1.0)
        assert valuation.g_spread_bp is None
        factors = valuation.factor_risk.factors
        assert factors[0] == "REAL|RUB_REAL|1M" and factors[-1] == "CS"
        assert not any(f.startswith("IR|") for f in factors)


@pytest.mark.parametrize("secid", LINKERS)
def test_real_flows_hold_the_settlement_nominal(secid: str) -> None:
    bond, series = linker(secid), cbr_cpi_series()
    nominal = linker_nominal(bond, series, SETTLE)
    flows = real_cashflows(bond, series, SETTLE)
    assert flows[-1].principal == nominal > 1000.0
    for flow in flows:
        assert flow.coupon == pytest.approx(nominal * 0.025 * 182 / 365, abs=0.006)
