"""The swap side of ``rub_ruonia_ois_moex_v2`` (BACKLOG #61).

``swap_convention("rub_ruonia_ois_moex_v2")`` derives the MOEX OIS RUONIA standard swap
with the NFA ``RUB-RUONIA-OIS-COMPOUND`` float leg: each fixing weighs ``n_i / Act`` with
``Act`` = 366 if any day of the period is in a leap year, and the float amount is
``(growth - 1) * Act / d * Act/Act ISDA``. Checks: par swaps reprice the v2 bootstrap's
quotes, schedules equal the bootstrap's periods, a seasoned swap's current coupon and a
leap-year period match an independent reconstruction, legacy identities are unchanged.
"""

from __future__ import annotations

from calendar import isleap
from dataclasses import asdict, replace
from datetime import date, timedelta

import pytest

from rub_fixed_income import (
    BusinessCalendar,
    CurveBootstrapRequest,
    DayCount,
    FixingHistory,
    InterestRateSwap,
    InvalidTerms,
    OisQuote,
    SwapConvention,
    SwapRiskEngine,
    SwapRiskRequest,
    bootstrap_discount_curve,
    canonical_json_bytes,
    from_dict,
    ois_curve_convention,
    ois_quote_periods,
    ois_quotes,
    swap_convention,
    swap_schedule,
    to_dict,
)

CAL = BusinessCalendar(
    holidays=frozenset({date(2026, 11, 4), date(2027, 1, 1), date(2028, 1, 3)}),
    version="synthetic-spfi-moscow-ruonia-v2-swaps",
    valid_from=date(2025, 1, 1),
    valid_to=date(2040, 12, 31),
)
V2 = "rub_ruonia_ois_moex_v2"
MOEX_V2 = swap_convention(V2)
TRADE = date(2026, 10, 16)  # Friday; TOM is Monday 19 October
TENORS = ["1W", "1M", "3M", "6M", "1Y", "2Y", "3Y", "5Y", "7Y", "10Y"]
NOTIONAL = 1_000_000_000.0
ACT_ACT = DayCount.ACT_ACT_ISDA


def quotes() -> tuple[OisQuote, ...]:
    return tuple(ois_quotes(TRADE, [(t, 0.161 - 0.0019 * i) for i, t in enumerate(TENORS)], CAL))


def curve_request() -> CurveBootstrapRequest:
    return CurveBootstrapRequest(TRADE, quotes(), ois_curve_convention(V2, CAL))


def par_swap(tenor: str, rate: float, trade: date = TRADE) -> InterestRateSwap:
    return InterestRateSwap.from_tenor(
        swap_id=f"v2-{tenor}",
        side="payer",
        notional=NOTIONAL,
        fixed_rate=rate,
        trade_date=trade,
        tenor=tenor,
        calendar=CAL,
        convention=MOEX_V2,
    )


def act(start: date, end: date) -> int:
    last = end - timedelta(days=1)
    return 366 if any(isleap(y) for y in range(start.year, last.year + 1)) else 365


def test_par_v2_swaps_reprice_the_v2_bootstrap_quotes() -> None:
    curve = bootstrap_discount_curve(curve_request())
    for quote in quotes():
        swap = par_swap(quote.tenor, quote.par_rate)
        engine = SwapRiskEngine(SwapRiskRequest(swap, CAL, curve))
        valuation = engine.valuation()
        error_bp = abs(valuation.pv_rub) / (NOTIONAL * valuation.annuity) * 1e4
        assert error_bp < 1e-6, quote
        assert valuation.par_rate == pytest.approx(quote.par_rate, abs=1e-10)
    # The OIS quote request reprices the same way on the quote risk basis.
    engine = SwapRiskEngine(
        SwapRiskRequest(par_swap("5Y", quotes()[7].par_rate), CAL, curve_request())
    )
    assert abs(engine.base_pv) / NOTIONAL < 1e-10


@pytest.mark.parametrize("tenor", ["3M", "1Y", "2Y", "7Y"])
def test_schedule_equals_the_bootstrap_periods(tenor: str) -> None:
    quote = next(q for q in quotes() if q.tenor == tenor)
    expected = ois_quote_periods(TRADE, quote, ois_curve_convention(V2, CAL))
    schedule = swap_schedule(par_swap(tenor, 0.15), CAL)
    for leg in (schedule.fixed, schedule.floating):
        assert [(p.accrual_start, p.accrual_end, p.payment_date) for p in leg] == [
            (p.start, p.end, p.payment) for p in expected
        ]
        assert [p.accrual_factor for p in leg] == [
            ACT_ACT.year_fraction(p.start, p.end) for p in expected
        ]


def history(valuation: date) -> FixingHistory:
    rows = []
    day, k = date(2025, 10, 1), 0
    while day < valuation:
        if CAL.is_business_day(day):
            rows.append((day, 0.17 + 0.0003 * (k % 9)))
            k += 1
        day += timedelta(days=1)
    return FixingHistory("ruonia", valuation, tuple(rows), CAL, "ruonia-v2-test")


def test_seasoned_coupon_and_leap_year_period_match_an_independent_reconstruction() -> None:
    valuation = TRADE
    fixings = history(valuation)
    curve = bootstrap_discount_curve(curve_request())
    swap = InterestRateSwap(
        "v2-seasoned",
        "receiver",
        NOTIONAL,
        0.165,
        date(2025, 10, 20),
        date(2028, 10, 20),
        MOEX_V2,
        float_spread=0.001,
    )
    engine = SwapRiskEngine(SwapRiskRequest(swap, CAL, curve, None, fixings))
    rows = [r for r in engine.valuation().cashflows if r.leg == "float"]
    first, _, leap = rows
    # Current period [2025-10-20, 2026-10-20): known fixings, then the curve.
    start, end = first.accrual_start, first.accrual_end
    assert (start, end) == (date(2025, 10, 20), date(2026, 10, 20))
    a = act(start, end)
    assert a == 365
    rates = dict(fixings.fixings)
    growth, day = 1.0, start
    while day < valuation:
        observation = day
        while not CAL.is_business_day(observation):
            observation -= timedelta(days=1)
        nxt = CAL.add_business_days(observation, 1)
        stop = min(nxt, end)
        growth *= 1.0 + rates[observation] * (stop - day).days / a
        day = stop
    known_growth, known_end = growth, day
    growth *= curve.discount_factor(day) / curve.discount_factor(end)
    d = (end - start).days
    alpha = ACT_ACT.year_fraction(start, end)
    expected = NOTIONAL * ((growth - 1.0) * a / d * alpha + 0.001 * alpha)
    assert first.source == "partially_projected"
    assert first.amount_rub == pytest.approx(-expected, rel=1e-13)  # a receiver pays float
    # The last period touches 2028, a leap year: Act = 366.
    start, end = leap.accrual_start, leap.accrual_end
    assert act(start, end) == 366
    projected = curve.discount_factor(start) / curve.discount_factor(end) - 1.0
    alpha = ACT_ACT.year_fraction(start, end)
    expected = NOTIONAL * (projected * 366 / (end - start).days * alpha + 0.001 * alpha)
    assert leap.amount_rub == pytest.approx(-expected, rel=1e-13)
    # Accrued float: the index rate of the known days times Act/Act from the period start.
    first_start = date(2025, 10, 20)
    known_days = (known_end - first_start).days
    assert known_end == valuation and known_days > 0
    index_rate = (known_growth - 1.0) * 365 / known_days
    accrued = NOTIONAL * (index_rate + 0.001) * ACT_ACT.year_fraction(first_start, valuation)
    totals = engine.valuation()
    assert totals.accrued_float_rub == pytest.approx(-accrued, rel=1e-12)
    assert totals.clean_pv_rub == pytest.approx(totals.pv_rub - totals.accrued_rub)


def test_legacy_conventions_keep_their_identity_bytes() -> None:
    legacy = swap_convention("rub_ruonia_ois_legacy_v1")
    krs = swap_convention("rub_key_rate_swap_v1")
    for convention in (legacy, krs):
        swap = par_swap("2Y", 0.15)
        swap = replace(swap, convention=convention)
        before = asdict(swap)
        del before["convention"]["float_compounding"]
        assert canonical_json_bytes(swap) == canonical_json_bytes(before)
        assert "float_compounding" not in to_dict(convention)
        assert ":compound=" not in convention.identifier
    assert from_dict(SwapConvention, to_dict(MOEX_V2)) == MOEX_V2
    assert to_dict(MOEX_V2)["float_compounding"] == "nfa_act"


def test_nfa_compounding_validation() -> None:
    with pytest.raises(InvalidTerms):
        replace(MOEX_V2, rate_day_count=DayCount.ACT_365_FIXED)
    with pytest.raises(InvalidTerms):
        replace(swap_convention("rub_key_rate_swap_v1"), float_compounding="nfa_act")
    with pytest.raises(InvalidTerms):
        replace(MOEX_V2, float_compounding="daily")  # type: ignore[arg-type]
    with pytest.raises(InvalidTerms):
        replace(MOEX_V2, float_compounding="rate_day_count")  # compounded needs a day count
