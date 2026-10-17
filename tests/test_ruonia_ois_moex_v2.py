"""MOEX OIS RUONIA quote convention and ``rub_ruonia_ois_moex_v2`` (BACKLOG #37).

Sources: MOEX "Методика формирования ценовых кривых СПФИ" (25.12.2024), Appendix 1,
OIS RUONIA; NFA "Изменения № 1" (17.08.2021) to the 2011 IR standard terms, 8.4(б).
See docs/CONVENTIONS.md, "RUONIA OIS, exchange specification".
"""

from __future__ import annotations

import json
from dataclasses import asdict, replace
from datetime import date

import pytest
from test_portfolio import POLICY, positions, run, snapshot

from rub_fixed_income import (
    BusinessCalendar,
    CurveBootstrapRequest,
    CurveConvention,
    CurveEntry,
    DayCount,
    InvalidTerms,
    OisQuote,
    OisSwapConvention,
    OisSwapPeriod,
    PrebuiltCurve,
    ScheduleConvention,
    ShockedCurveCache,
    bootstrap_discount_curve,
    canonical_json_bytes,
    from_dict,
    get_curve_spec,
    ois_curve_convention,
    ois_quote_periods,
    ois_quotes,
    portfolio_result_to_json,
    to_dict,
)

# Synthetic test data, not an official calendar: weekends plus two holidays.
CAL = BusinessCalendar(
    holidays=frozenset({date(2026, 11, 4), date(2027, 10, 20)}),
    version="synthetic-spfi-moscow-ruonia",
    valid_from=date(2026, 1, 1),
    valid_to=date(2037, 12, 31),
)
V2 = "rub_ruonia_ois_moex_v2"
CONVENTION = ois_curve_convention(V2, CAL)
TRADE = date(2026, 10, 16)  # Friday; spot (TOM) is Monday 19 October
MOEX_TENORS = ["1W", "2W", "1M", "2M", "3M", "6M", "9M", "1Y"] + [f"{y}Y" for y in range(2, 11)]


def _quotes(trade: date = TRADE, tenors: list[str] = MOEX_TENORS) -> tuple[OisQuote, ...]:
    rates = [(t, 0.1625 - 0.0021 * i) for i, t in enumerate(tenors)]
    return ois_quotes(trade, rates, CAL)


def _request(**changes: object) -> CurveBootstrapRequest:
    base = CurveBootstrapRequest(TRADE, _quotes(), CONVENTION)
    return replace(base, **changes)  # type: ignore[arg-type]


def _actact(start: date, end: date) -> float:
    return DayCount.ACT_ACT_ISDA.year_fraction(start, end)


def test_spec_is_registered_with_the_source_conventions() -> None:
    spec = get_curve_spec(V2)
    assert spec.spot_lag_business_days == 1 and spec.payment_lag_business_days == 1
    assert spec.business_day_convention == "MODIFIED_FOLLOWING"
    assert spec.fixed_leg_frequency_months == 12 and spec.stub == "short_initial"
    assert spec.fixed_leg_day_count is DayCount.ACT_ACT_ISDA
    assert spec.float_leg_day_count is DayCount.ACT_ACT_ISDA
    assert spec.calendars == ("SPFI", "MOSCOW", "RUONIA")
    assert any("moex.com/ru/documents/27002" in s for s in spec.sources)
    ois = CONVENTION.ois_swap
    assert ois is not None and ois.calendar is CAL
    assert ois_curve_convention("rub_ruonia_ois_legacy_v1") == CurveConvention()
    with pytest.raises(InvalidTerms):
        ois_curve_convention(V2)
    with pytest.raises(InvalidTerms):
        ois_curve_convention("rub_ruonia_ois_legacy_v1", CAL)
    with pytest.raises(InvalidTerms):
        ois_curve_convention("rub_ofz_kbd_v1", CAL)


def test_periods_single_up_to_one_year_then_annual_with_payment_lag() -> None:
    by_tenor = {q.tenor: q for q in _quotes()}
    (week,) = ois_quote_periods(TRADE, by_tenor["1W"], CONVENTION)
    assert week == OisSwapPeriod(date(2026, 10, 19), date(2026, 10, 26), date(2026, 10, 27))
    # 1Y: one period; its end + 1B skips the (synthetic) 20 October 2027 holiday.
    (year,) = ois_quote_periods(TRADE, by_tenor["1Y"], CONVENTION)
    assert year == OisSwapPeriod(date(2026, 10, 19), date(2027, 10, 19), date(2027, 10, 21))
    three = ois_quote_periods(TRADE, by_tenor["3Y"], CONVENTION)
    assert three == (
        OisSwapPeriod(date(2026, 10, 19), date(2027, 10, 19), date(2027, 10, 21)),
        OisSwapPeriod(date(2027, 10, 19), date(2028, 10, 19), date(2028, 10, 20)),
        OisSwapPeriod(date(2028, 10, 19), date(2029, 10, 19), date(2029, 10, 22)),
    )
    # 19 October 2030 is a Saturday: the roll date is adjusted MODFOL to Monday 21st.
    four = ois_quote_periods(TRADE, by_tenor["4Y"], CONVENTION)
    assert four[-1] == OisSwapPeriod(date(2029, 10, 19), date(2030, 10, 21), date(2030, 10, 22))


def test_short_initial_stub_and_modified_following_month_end() -> None:
    ois = CONVENTION.ois_swap
    assert ois is not None
    # 18M from 19 October 2026: rolled back from the end, so the 6M stub comes first.
    assert ois.periods(date(2026, 10, 19), date(2028, 4, 19), date(2028, 4, 19)) == (
        OisSwapPeriod(date(2026, 10, 19), date(2027, 4, 19), date(2027, 4, 20)),
        OisSwapPeriod(date(2027, 4, 19), date(2028, 4, 19), date(2028, 4, 20)),
    )
    # Trade Thu 30 July 2026, spot Fri 31 July; 1Y ends Sat 31 July 2027 -> Fri 30 July.
    (quote,) = ois_quotes(date(2026, 7, 30), [("1Y", 0.15)], CAL)
    assert quote.maturity == date(2027, 7, 30)
    (period,) = ois_quote_periods(date(2026, 7, 30), quote, CONVENTION)
    assert period == OisSwapPeriod(date(2026, 7, 31), date(2027, 7, 30), date(2027, 8, 2))


def test_ruonia_ois_compound_scale_follows_nfa_act_rule() -> None:
    ois = CONVENTION.ois_swap
    assert ois is not None
    # Inside one year Act / d x Act/Act ISDA is exactly 1 (no leap-year part).
    assert ois.float_amount_scale(date(2026, 10, 19), date(2027, 10, 19)) == 1.0
    # A period ending on 1 January 2028 has no day in the leap year: Act = 365.
    assert ois.float_amount_scale(date(2027, 3, 1), date(2028, 1, 1)) == 1.0
    # Straddling into the leap year 2028: Act = 366 for the whole period (NFA 8.4(б)).
    start, end = date(2028, 10, 19), date(2029, 10, 19)
    assert ois.float_amount_scale(start, end) == pytest.approx(
        366 / 365 * (74 / 366 + 291 / 365), abs=1e-15
    )
    start, end = date(2027, 10, 19), date(2028, 10, 19)
    assert ois.float_amount_scale(start, end) == pytest.approx(
        366 / 366 * (74 / 365 + 292 / 366), abs=1e-15
    )


def test_seventeen_moex_tenors_bootstrap_and_reprice_exactly() -> None:
    request = _request()
    curve = bootstrap_discount_curve(request)
    assert [q.tenor for q in request.quotes] == MOEX_TENORS
    assert [d for d, _ in curve.nodes] == [q.maturity for q in request.quotes]
    assert curve.max_repricing_error_bp(request.quotes) < 1e-7
    assert curve.convention == CONVENTION
    # Order of the quotes does not matter.
    shuffled = replace(request, quotes=tuple(reversed(request.quotes)))
    assert bootstrap_discount_curve(shuffled).nodes == curve.nodes


def test_par_rates_are_independently_reproduced() -> None:
    curve = bootstrap_discount_curve(_request())
    df = curve.discount_factor
    by_tenor = {q.tenor: q for q in _quotes()}
    # 1W: one period inside 2026, so the par rate is simple compounded growth.
    start, end = date(2026, 10, 19), date(2026, 10, 26)
    assert (df(start) / df(end) - 1.0) / (7 / 365) == pytest.approx(
        by_tenor["1W"].par_rate, abs=1e-13
    )
    # 3Y: periods and amounts written out by hand.
    rows = [
        (date(2026, 10, 19), date(2027, 10, 19), date(2027, 10, 21), 365),
        (date(2027, 10, 19), date(2028, 10, 19), date(2028, 10, 20), 366),
        (date(2028, 10, 19), date(2029, 10, 19), date(2029, 10, 22), 366),
    ]
    fixed = sum(_actact(s, e) * df(p) for s, e, p, _ in rows)
    floating = sum(
        (df(s) / df(e) - 1.0) * act / (e - s).days * _actact(s, e) * df(p) for s, e, p, act in rows
    )
    assert floating / fixed == pytest.approx(by_tenor["3Y"].par_rate, abs=1e-12)


def test_legacy_convention_numbers_and_identity_are_unchanged() -> None:
    quotes = _quotes()
    legacy = CurveBootstrapRequest(TRADE, quotes)
    assert legacy.convention.identifier == (
        "curve:time=ACT/365F:interp=log-linear-df:extrap=flat-forward:"
        "fixed=legacy-annual-forward:ACT/365F"
    )
    raw = asdict(legacy)
    assert raw["convention"].pop("ois_swap") is None
    assert canonical_json_bytes(legacy) == canonical_json_bytes(raw)
    assert b"ois_swap" not in canonical_json_bytes(legacy)
    assert "ois_swap" not in to_dict(legacy)["convention"]
    curve = bootstrap_discount_curve(legacy)
    assert all(curve.quote_par_rate(q) == curve.par_rate(q.maturity) for q in quotes)
    v2 = bootstrap_discount_curve(_request())
    assert v2.nodes != curve.nodes
    assert CONVENTION.identifier.startswith(
        "curve:time=ACT/365F:interp=log-linear-df:extrap=flat-forward:ois:T+1:pay+1B:"
    )


def test_v2_request_serializes_round_trip() -> None:
    request = _request()
    data = json.loads(json.dumps(to_dict(request)))
    assert data["convention"]["ois_swap"]["payment_lag_business_days"] == 1
    assert from_dict(CurveBootstrapRequest, data) == request
    legacy = to_dict(CurveBootstrapRequest(TRADE, _quotes()))
    assert from_dict(CurveBootstrapRequest, legacy).convention == CurveConvention()
    legacy["convention"]["ois_swap"] = None
    assert from_dict(CurveBootstrapRequest, legacy).convention == CurveConvention()


def test_invalid_quotes_and_conventions_are_rejected() -> None:
    quotes = list(_quotes())
    moved = replace(quotes[3], maturity=quotes[3].maturity.replace(day=20))
    with pytest.raises(InvalidTerms, match="resolves to"):
        bootstrap_discount_curve(_request(quotes=(*quotes[:3], moved, *quotes[4:])))
    with pytest.raises(InvalidTerms, match="not a spot-starting"):
        bootstrap_discount_curve(_request(quotes=(OisQuote("ON", date(2026, 10, 19), 0.16),)))
    with pytest.raises(ValueError, match="finite"):
        bootstrap_discount_curve(_request(quotes=(replace(quotes[0], par_rate=float("nan")),)))
    with pytest.raises(ValueError, match="ois_swap sets the fixed leg"):
        CurveConvention(
            fixed_leg_schedule=ScheduleConvention(calendar=CAL), ois_swap=CONVENTION.ois_swap
        )
    with pytest.raises(ValueError, match="ois_swap sets the fixed leg"):
        CurveConvention(fixed_leg_day_count=DayCount.ACT_360, ois_swap=CONVENTION.ois_swap)
    with pytest.raises(ValueError):
        OisSwapConvention(CAL, payment_lag_business_days=-1)
    with pytest.raises(ValueError):
        OisSwapConvention(CAL, frequency_months=0)
    with pytest.raises(ValueError):
        OisSwapConvention(CAL, stub="long_final")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        OisSwapConvention("weekends")  # type: ignore[arg-type]


PORTFOLIO_TRADE = date(2026, 10, 15)
OIS_POLICY = replace(POLICY, curve_overrides=(("OFZ-BULLET", "RUB_OIS"),))


def _ois_snapshot(spec_id: str, request: CurveBootstrapRequest) -> object:
    base = snapshot()
    curves = tuple(c for c in base.curves if c.curve_id != "RUB_OIS")
    return replace(base, curves=(*curves, CurveEntry("RUB_OIS", spec_id, request)))


def _portfolio_request() -> CurveBootstrapRequest:
    tenors = ["1M", "3M", "6M", "1Y", "2Y", "3Y", "5Y"]
    return CurveBootstrapRequest(PORTFOLIO_TRADE, _quotes(PORTFOLIO_TRADE, tenors), CONVENTION)


def test_value_portfolio_on_v2_and_spec_convention_checks() -> None:
    request = _portfolio_request()
    cache = ShockedCurveCache()
    result = run(
        snapshot=_ois_snapshot(V2, request),
        positions=positions()[6:7],
        policy=OIS_POLICY,
        scenarios=(),
        curve_cache=cache,
    )
    assert result.coverage.rejections == ()
    (row,) = result.positions
    assert row.curve_basis == "ois_quote" and row.factor_risk.factors[0] == "IR|RUB_OIS|1M"
    # Persisted shocked curves (to_dict/from_dict) reprice their quotes when seeded.
    text = json.dumps([to_dict(e) for e in cache.export()])
    seeded = ShockedCurveCache()
    seeded.seed(tuple(from_dict(PrebuiltCurve, item) for item in json.loads(text)))
    again = run(
        snapshot=_ois_snapshot(V2, request),
        positions=positions()[6:7],
        policy=OIS_POLICY,
        scenarios=(),
        curve_cache=seeded,
    )
    assert portfolio_result_to_json(again) == portfolio_result_to_json(result)
    legacy_request = CurveBootstrapRequest(PORTFOLIO_TRADE, request.quotes)
    for spec_id, source in (("rub_ruonia_ois_legacy_v1", request), (V2, legacy_request)):
        rejected = run(
            snapshot=_ois_snapshot(spec_id, source),
            positions=positions()[6:7],
            policy=OIS_POLICY,
            scenarios=(),
        )
        assert {r.code for r in rejected.coverage.rejections} == {"INVALID_TERMS"}
