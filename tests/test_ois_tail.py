"""RUONIA OIS continued past its last quote (issue #84, ``ValuationPolicy.ois_tail``).

Synthetic data of ``ofz_basis_data`` (OIS quotes 1W-10Y, the KBD of 15.10.2026). Without the
tail the curve extrapolates flat-forward from 9Y-10Y and the 7Y/10Y buckets of a position past
10Y swing with opposite signs; with it the risk past the last quote sits on the tail buckets.
"""

from __future__ import annotations

import math
from dataclasses import replace
from datetime import date, timedelta
from typing import Any

import numpy as np
import pytest
from floating_perpetual_data import RESET, kbd_periods, kbd_rule, perpetual
from ofz_basis_data import DATE, KBD, OIS, OIS_CALENDAR, PARAMS
from test_ofz_basis import AS_OF, IDS, _bond
from test_portfolio_floaters import KNOWN

from rub_fixed_income import (
    OIS_TAIL_MODELS,
    RISK_BUCKETS,
    AccountingCategory,
    BondPriceObservation,
    BondScenario,
    CalendarEntry,
    CurveEntry,
    ExerciseAssumption,
    FixingEntry,
    FixingHistory,
    FloatingCouponLeg,
    InvalidTerms,
    MarketSnapshot,
    OfzBasisCurve,
    OfzBasisIds,
    OisTail,
    PortfolioRiskResult,
    PortfolioScenario,
    Position,
    ValuationPolicy,
    bootstrap_discount_curve,
    canonical_json_bytes,
    extend_ois_request,
    horizon_portfolio,
    value_portfolio,
)
from rub_fixed_income.dates import add_months
from rub_fixed_income.perpetual import _sum_with_remainder

TAIL = OisTail("RUB_OIS", "RUB_OFZ_KBD")
BUCKETS = RISK_BUCKETS


def unit(bucket: str, size: float = 10.0) -> tuple[float, ...]:
    return tuple(size if b == bucket else 0.0 for b in BUCKETS)


def snapshot(*, kbd: bool = True) -> MarketSnapshot:
    history = FixingHistory(
        "kbd",
        DATE,
        ((date(2024, 12, 10), 0.15),),
        OIS_CALENDAR,
        "hub-zcyc",
        known_at_utc=KNOWN,
        tenor_years=5.0,
    )
    curves = [CurveEntry("RUB_OIS", "rub_ruonia_ois_moex_v2", OIS)]
    if kbd:
        curves.append(CurveEntry("RUB_OFZ_KBD", "rub_ofz_kbd_v3", PARAMS))
    return MarketSnapshot(
        "synthetic-ois-tail",
        DATE,
        AS_OF,
        curves=tuple(curves),
        calendars=(CalendarEntry("MOEX_STOCK", OIS_CALENDAR),),
        fixings=(FixingEntry("KBD_5Y", history),),
        quotes=(
            BondPriceObservation("CORP-15Y", 88.0, "waprice"),
            BondPriceObservation("PERP-KBD", 95.0, "waprice", accrued_per_bond=0.0),
            BondPriceObservation("SU-LONG", 52.0, "waprice"),
        ),
    )


def kbd_perpetual() -> object:
    """A never-called perpetual reset every 5 years to the KBD 5Y + 3.25 %."""
    leg = FloatingCouponLeg(kbd_rule(), kbd_periods(), "KBD_5Y", "RUB_OFZ_KBD")
    return perpetual("PERP-KBD", leg, add_months(RESET, 60))


CORP = _bond("CORP-15Y", date(2041, 10, 15), 0.14)
OFZ = _bond("SU-LONG", date(2041, 5, 15), 0.071)
SCENARIOS = (
    PortfolioScenario(
        "ruonia-10y", BondScenario.parallel(0.0), curve_shifts_bp=(("RUB_OIS", unit("10Y")),)
    ),
)


def value(
    instrument_ids: tuple[str, ...], *, kbd: bool = True, **policy: Any
) -> PortfolioRiskResult:
    instruments = {"CORP-15Y": CORP, "PERP-KBD": kbd_perpetual(), "SU-LONG": OFZ}
    args: dict[str, object] = {
        "discount_curve_id": "RUB_OIS",
        "spread_kind": "credit",
        "exercise_assumptions": (("PERP-KBD", ExerciseAssumption.never_called()),),
        "include_second_order": False,
    }
    args.update(policy)
    result = value_portfolio(
        run_id="ois-tail",
        as_of=AS_OF,
        snapshot=snapshot(kbd=kbd),
        instruments=tuple(instruments[i] for i in instrument_ids),  # type: ignore[misc]
        positions=tuple(
            Position(f"p-{i}", i, 1000.0, "CREDIT", AccountingCategory.TRADING, "RUB")
            for i in instrument_ids
        ),
        policy=ValuationPolicy(**args),  # type: ignore[arg-type]
        scenarios=SCENARIOS,
    )
    return result


def ruonia(result: PortfolioRiskResult, instrument_id: str) -> dict[str, float]:
    row = next(p for p in result.positions if p.instrument_id == instrument_id)
    first = row.factor_risk.first_order_by_factor()
    return {b: first.get(f"IR|RUB_OIS|{b}", 0.0) for b in BUCKETS}


def scenario_pnl(result: PortfolioRiskResult, instrument_id: str) -> float:
    (scenario,) = result.scenarios
    pnl = next(p.pnl_rub for p in scenario.positions if p.position_id == f"p-{instrument_id}")
    assert pnl is not None
    return pnl


def model_of(result: PortfolioRiskResult, curve_id: str) -> str:
    return next(c.model_version for c in result.curves if c.curve_id == curve_id)


# -- the tail quotes ----------------------------------------------------------------------------


def test_the_kbd_rule_is_the_zx_tail_of_the_ruonia_basis_view() -> None:
    extended, rule = extend_ois_request(OIS, KBD)
    view = OfzBasisCurve(KBD, OIS)
    assert rule == "kbd"
    assert extended.quotes == (*OIS.quotes, *view.synthetic_quotes)
    assert [q.tenor for q in view.synthetic_quotes] == ["15Y", "20Y"]
    # The view on the extended quotes adds nothing: discount and projection share one tail.
    assert OfzBasisCurve(KBD, extended).synthetic_quotes == ()


def test_the_flat_zero_rule_continues_the_last_zero_rate() -> None:
    extended, rule = extend_ois_request(OIS, None)
    assert rule == "flat_zero"
    plain = bootstrap_discount_curve(OIS)
    curve = bootstrap_discount_curve(extended)
    last = OIS.quotes[-1].maturity
    zero = plain.zero_rate(last)
    assert curve.zero_rate(last) == pytest.approx(zero, abs=1e-9)
    for years in (12, 15, 20, 30, 50):
        assert curve.zero_rate(add_months(DATE, 12 * years)) == pytest.approx(zero, abs=2e-7)
    # The plain curve's tail drifts with the 9Y-10Y forward instead.
    assert curve.max_repricing_error_bp(OIS.quotes) < 1e-6


def test_a_request_past_the_last_par_tenor_is_unchanged() -> None:
    extended, _ = extend_ois_request(OIS, KBD)
    again, rule = extend_ois_request(extended, KBD)
    assert again is extended and rule == "kbd"


# -- risk past the last quote ------------------------------------------------------------------


def test_a_15y_bond_on_ruonia_no_longer_swings_at_7y_and_10y() -> None:
    before = value(("CORP-15Y",))
    after = value(("CORP-15Y",), ois_tail=TAIL)
    old, new = ruonia(before, "CORP-15Y"), ruonia(after, "CORP-15Y")
    total = math.fsum(new.values())
    assert total == pytest.approx(math.fsum(old.values()), rel=0.01)  # sum unchanged
    # Before: 7Y and 10Y of opposite signs, each the size of the whole DV01.
    assert old["7Y"] > 0.5 * abs(total) and old["10Y"] < -1.5 * abs(total)
    # After: the risk sits on the 15Y bucket, the one of its maturity.
    assert abs(new["7Y"]) + abs(new["10Y"]) < 0.02 * abs(total)
    assert new["15Y"] == pytest.approx(total, rel=0.02)
    # A full revaluation agrees: RUONIA 10Y +10 bp barely moves a 15Y bond now.
    assert abs(scenario_pnl(before, "CORP-15Y")) > 15.0 * abs(total)
    assert abs(scenario_pnl(after, "CORP-15Y")) < 0.1 * abs(total)
    assert model_of(after, "RUB_OIS") == "ois-logdf-v1+" + OIS_TAIL_MODELS["kbd"]
    assert model_of(before, "RUB_OIS") == "ois-logdf-v1"


def test_a_perpetual_kbd_floater_in_ruonia_plus_basis_follows_one_tail() -> None:
    policy: dict[str, Any] = {
        "ofz_basis": IDS,
        "curve_overrides": (("PERP-KBD", "RUB_OIS"),),
    }
    before = value(("PERP-KBD",), **policy)
    after = value(("PERP-KBD",), ois_tail=TAIL, **policy)
    old, new = ruonia(before, "PERP-KBD"), ruonia(after, "PERP-KBD")
    assert math.fsum(new.values()) == pytest.approx(math.fsum(old.values()), rel=0.02)
    swing_before = abs(old["7Y"]) + abs(old["10Y"])
    swing_after = abs(new["7Y"]) + abs(new["10Y"])
    assert swing_before > 800.0  # RUB/bp per 1 mn: 7Y about +354, 10Y about -564
    assert swing_after < 0.1 * swing_before
    assert abs(scenario_pnl(after, "PERP-KBD")) < 0.02 * abs(scenario_pnl(before, "PERP-KBD"))
    # The basis ladder (the KBD's own par risk) is the same with either tail.
    row = next(p for p in after.positions if p.instrument_id == "PERP-KBD")
    basis = row.factor_risk.first_order_by_factor()
    row0 = next(p for p in before.positions if p.instrument_id == "PERP-KBD")
    basis0 = row0.factor_risk.first_order_by_factor()
    for bucket in BUCKETS:
        key = f"IR|RUB_OFZ_BASIS|{bucket}"
        assert basis[key] == pytest.approx(basis0[key], abs=0.02 * abs(basis0["CS"]))


def test_ofz_on_the_kbd_keep_their_ruonia_and_basis_ladders() -> None:
    policy: dict[str, Any] = {
        "ofz_basis": IDS,
        "discount_curve_id": "RUB_OFZ_KBD",
        "spread_kind": "ofz",
        "risk_method": "analytic",
    }
    before = value(("SU-LONG",), **policy)
    after = value(("SU-LONG",), ois_tail=TAIL, **policy)
    (old,), (new,) = before.positions, after.positions
    assert new.pv_rub == old.pv_rub
    assert new.factor_risk.factors == old.factor_risk.factors
    assert new.factor_risk.first_order == old.factor_risk.first_order


def test_without_a_kbd_the_tail_is_flat_zero_and_recorded() -> None:
    result = value(("CORP-15Y",), kbd=False, ois_tail=TAIL)
    assert model_of(result, "RUB_OIS") == "ois-logdf-v1+" + OIS_TAIL_MODELS["flat_zero"]
    risk = ruonia(result, "CORP-15Y")
    assert abs(risk["7Y"]) + abs(risk["10Y"]) < 0.02 * abs(math.fsum(risk.values()))
    named = value(("CORP-15Y",), kbd=False, ois_tail=OisTail("RUB_OIS"))
    assert ruonia(named, "CORP-15Y") == risk


def test_off_by_default_and_the_job_identity() -> None:
    policy = ValuationPolicy(discount_curve_id="RUB_OIS")
    assert b"ois_tail" not in canonical_json_bytes(policy)
    plain = value(("CORP-15Y",))
    tailed = value(("CORP-15Y",), ois_tail=TAIL)
    assert plain.job_id != tailed.job_id
    again = value(("CORP-15Y",), ois_tail=None)
    assert again.job_id == plain.job_id


def test_horizon_portfolio_reads_the_tail_too() -> None:
    kwargs: dict[str, object] = {
        "snapshot": snapshot(),
        "instruments": (CORP,),
        "positions": (
            Position("p", "CORP-15Y", 1000.0, "CREDIT", AccountingCategory.TRADING, "RUB"),
        ),
        "horizon_date": DATE + timedelta(days=30),
    }
    plain = horizon_portfolio(
        policy=ValuationPolicy(discount_curve_id="RUB_OIS"),
        **kwargs,  # type: ignore[arg-type]
    )
    tailed = horizon_portfolio(
        policy=ValuationPolicy(discount_curve_id="RUB_OIS", ois_tail=TAIL),
        **kwargs,  # type: ignore[arg-type]
    )
    (a,), (b,) = plain.positions, tailed.positions
    assert a.per_bond.pv_start_rub == pytest.approx(b.per_bond.pv_start_rub, rel=1e-12)  # the price
    assert a.rolldown_rub != b.rolldown_rub


def test_validation() -> None:
    for bad in ("", "A|B"):
        with pytest.raises(InvalidTerms):
            OisTail(bad)
        with pytest.raises(InvalidTerms):
            OisTail("RUB_OIS", bad)
    with pytest.raises(InvalidTerms, match="must differ"):
        OisTail("RUB_OIS", "RUB_OIS")
    with pytest.raises(TypeError):
        ValuationPolicy(discount_curve_id="RUB_OIS", ois_tail="RUB_OIS")  # type: ignore[arg-type]
    with pytest.raises(InvalidTerms, match="kbd_curve_id"):
        ValuationPolicy(
            discount_curve_id="RUB_OIS",
            ofz_basis=OfzBasisIds("RUB_OFZ_KBD", "RUB_OIS", "B"),
            ois_tail=OisTail("RUB_OIS", "OTHER_KBD"),
        )
    # A tail on a curve that is not OIS quotes rejects the positions on it.
    result = value(("CORP-15Y",), ois_tail=OisTail("RUB_OFZ_KBD"), discount_curve_id="RUB_OFZ_KBD")
    assert result.coverage.rejections[0].code == "INVALID_TERMS"
    # A KBD ID naming a curve that is not a KBD.
    result = value(("CORP-15Y",), ois_tail=OisTail("RUB_OIS", "RUB_OIS_X"))
    assert model_of(result, "RUB_OIS").endswith(OIS_TAIL_MODELS["flat_zero"])


def test_subnormal_tail_terms_are_not_divergence() -> None:
    tiny = np.full(4, 5e-324)
    assert _sum_with_remainder(np.array([1.0, 1e-300, *tiny])) == pytest.approx(1.0)
    assert _sum_with_remainder(np.array([1.0, 2.0, 3.0])) == math.inf
    assert _sum_with_remainder(np.array([1.0, 0.5])) == pytest.approx(2.0)


def test_extend_needs_matching_dates() -> None:
    other = replace(OIS, curve_date=DATE - timedelta(days=1))
    with pytest.raises(InvalidTerms, match="same curve date"):
        extend_ois_request(other, KBD)
