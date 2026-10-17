"""Portfolio and horizon valuation under explicit assumptions (package 9b)."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta
from itertools import pairwise

import pytest
from test_portfolio import (
    AS_OF,
    BUSINESS_DATE,
    POLICY,
    bond,
    instruments,
    periods,
    positions,
    run,
    snapshot,
)

from rub_fixed_income import (
    AccountingCategory,
    AccrualBasis,
    AssumedPortfolioResult,
    AssumptionPolicy,
    BondPriceObservation,
    BondScenario,
    ExerciseAssumption,
    Instrument,
    InstrumentAssumption,
    InvalidTerms,
    MarketSnapshot,
    Offer,
    OfferKind,
    PassThroughPeriod,
    PassThroughTerms,
    PortfolioScenario,
    Position,
    PrepaymentAssumption,
    PrincipalLeg,
    canonical_sha256,
    horizon_assumed_portfolio,
    horizon_portfolio,
    pass_through_instrument,
    prepare_assumed_book,
    value_assumed_portfolio,
    value_portfolio,
    verify_assumed_result,
    verify_portfolio_result,
    yield_to_worst,
)

SETTLE = date(2026, 10, 16)
CALLS = periods(date(2026, 9, 1), 10, 75.0)  # semi-annual to 2031-09-01
FIRST_CALL = CALLS[3].payment_date  # 2028-09-01


def callable_bond(instrument_id: str = "CORP-CALL") -> Instrument:
    return bond(
        instrument_id,
        CALLS,
        offers=(
            Offer(FIRST_CALL, OfferKind.CALL, confirmed=False),
            Offer(CALLS[5].payment_date, OfferKind.CALL, confirmed=False),
        ),
    )


def perpetual() -> Instrument:
    plain = bond("PERP", periods(date(2026, 6, 1), 6, 60.0))
    return replace(
        plain,
        principal_leg=PrincipalLeg(()),
        offers=(Offer(date(2028, 12, 1), OfferKind.CALL, confirmed=False),),
    )


def pool() -> PassThroughTerms:
    dates = [date(2026, 10, 1) + timedelta(days=30 * i) for i in range(37)]
    return PassThroughTerms(
        "MBS-1",
        "RUB",
        1000.0,
        640.0,
        0.11,
        AccrualBasis.RATE_ACT_365,
        tuple(PassThroughPeriod(s, e, e, 10.0) for s, e in pairwise(dates)),
        12,
        "MOEX_STOCK",
    )


def book_snapshot() -> MarketSnapshot:
    base = snapshot()
    extra = (
        BondPriceObservation("CORP-CALL", 104.0, "waprice"),
        BondPriceObservation("PERP", 97.0, "waprice"),
        BondPriceObservation("MBS-1", 99.5, "waprice"),
    )
    return replace(base, quotes=(*base.quotes, *extra))


def book_positions() -> tuple[Position, ...]:
    def pos(pid: str, iid: str, qty: float) -> Position:
        return Position(pid, iid, qty, "CREDIT", AccountingCategory.TRADING, "RUB")

    return (
        *positions()[:3],
        pos("p-call", "CORP-CALL", 100),
        pos("p-perp", "PERP", 50),
        pos("p-mbs", "MBS-1", 200),
    )


POLICY_FULL = AssumptionPolicy(
    default_exercise=ExerciseAssumption.to_worst(),
    overrides=(
        InstrumentAssumption("PERP", exercise=ExerciseAssumption.to_call(date(2028, 12, 1))),
        InstrumentAssumption("MBS-1", prepayment=PrepaymentAssumption(0.15)),
    ),
)


def assumed(policy: AssumptionPolicy = POLICY_FULL, **kw: object) -> AssumedPortfolioResult:
    args: dict[str, object] = {
        "run_id": "eod-2026-10-15",
        "as_of": AS_OF,
        "snapshot": book_snapshot(),
        "instruments": (*instruments()[:3], callable_bond(), perpetual()),
        "positions": book_positions(),
        "policy": POLICY,
        "assumptions": policy,
        "pass_throughs": (pool(),),
    }
    args.update(kw)
    return value_assumed_portfolio(**args)  # type: ignore[arg-type]


def test_a_book_without_callables_is_valued_exactly_as_by_value_portfolio() -> None:
    plain = run()
    wrapped = value_assumed_portfolio(
        run_id="eod-2026-10-15",
        as_of=AS_OF,
        snapshot=snapshot(),
        instruments=instruments(),
        positions=positions()[:6],
        policy=POLICY,
        assumptions=POLICY_FULL,
        scenarios=(PortfolioScenario("parallel+100", BondScenario.parallel(100)),),
    )
    assert wrapped.portfolio == plain
    assert wrapped.portfolio.result_sha256 == plain.result_sha256
    assert wrapped.assumptions == ()
    assert wrapped.coverage == plain.coverage
    assert wrapped.total_pv_rub is None and wrapped.covered_pv_rub == plain.covered_pv_rub


def test_callables_perpetuals_and_pass_throughs_are_valued_under_recorded_assumptions() -> None:
    result = assumed()
    assert result.status == "complete" and result.coverage.completed == 6
    assert verify_assumed_result(result)
    assert verify_portfolio_result(result.portfolio)
    rows = {p.position_id: p for p in result.portfolio.positions}
    uses = {u.instrument_id: u for u in result.assumptions}
    # A 7.5 % semi-annual coupon bond at 104 is worst to its first call.
    call = callable_bond()
    dirty = 1040.0 + call.accrued(SETTLE)
    worst = yield_to_worst(call, SETTLE, dirty)
    assert worst.redemption_date == FIRST_CALL
    assert uses["CORP-CALL"].assumption == f"exercise:to_worst@call:{FIRST_CALL.isoformat()}"
    assert rows["p-call"].horizon_date == FIRST_CALL
    assert rows["p-call"].effective_yield == pytest.approx(worst.effective_yield, abs=1e-12)
    assert uses["CORP-CALL"].source_sha256 == canonical_sha256(call)
    assert uses["PERP"].kind == "perpetual" and rows["p-perp"].horizon_date == date(2028, 12, 1)
    mbs = uses["MBS-1"]
    assert mbs.kind == "pass_through" and mbs.wal_years is not None and 0.0 < mbs.wal_years < 3.0
    projected = pass_through_instrument(pool(), PrepaymentAssumption(0.15))
    assert mbs.valued_sha256 == canonical_sha256(projected)
    assert rows["p-mbs"].current_nominal_rub == 640.0
    assert rows["p-mbs"].pv_rub == pytest.approx(
        200 * (0.995 * 640.0 + projected.accrued(SETTLE)), rel=1e-12
    )
    assert result.total_pv_rub == result.portfolio.covered_pv_rub
    tampered = replace(result, total_pv_rub=1.0)
    assert not verify_assumed_result(tampered)


def test_rate_risk_holds_the_exercise_and_the_cpr_fixed() -> None:
    result = assumed()
    rows = {p.position_id: p for p in result.portfolio.positions}
    exercised = prepare_assumed_book(
        snapshot=book_snapshot(),
        instruments=(callable_bond(),),
        policy=POLICY_FULL,
    ).instruments[0]
    direct = value_portfolio(
        run_id="eod-2026-10-15",
        as_of=AS_OF,
        snapshot=book_snapshot(),
        instruments=(exercised,),
        positions=(book_positions()[3],),
        policy=POLICY,
    )
    (row,) = direct.positions
    assert row.factor_risk == rows["p-call"].factor_risk
    long_end = [
        v
        for k, v in row.factor_risk.first_order_by_factor().items()
        if k.endswith(("|5Y", "|7Y", "|10Y"))
    ]
    assert long_end and all(v == 0.0 for v in long_end)  # nothing after the 2028 call


def test_missing_or_failing_assumptions_are_typed_rejections_in_the_merged_coverage() -> None:
    result = assumed(
        AssumptionPolicy(
            overrides=(
                InstrumentAssumption("PERP", exercise=ExerciseAssumption.to_call(date(2030, 6, 1))),
            )
        )
    )
    codes = {r.position_id: r.code for r in result.coverage.rejections}
    messages = {r.position_id: r.message for r in result.coverage.rejections}
    assert codes == {
        "p-call": "UNSUPPORTED_PRODUCT",  # no assumption: value_portfolio rejects as before
        "p-perp": "INVALID_TERMS",  # 2030-06-01 is not a call date
        "p-mbs": "MISSING_ASSUMPTION",
    }
    assert messages["p-call"] == "callable bonds are not supported (package 9)"
    assert result.status == "partial" and result.total_pv_rub is None
    assert {p.position_id for p in result.portfolio.positions} == {
        "p-bullet",
        "p-amort",
        "p-put",
    }
    uses = {u.instrument_id: u for u in result.assumptions}
    assert uses["PERP"].status == "rejected" and uses["PERP"].valued_sha256 is None
    no_quote = assumed(snapshot=snapshot())
    rejected = {r.position_id: r.code for r in no_quote.coverage.rejections}
    assert rejected["p-call"] == "MISSING_MARKET_DATA"  # to_worst needs the quote


def test_an_exercise_cannot_be_given_to_a_pass_through_and_vice_versa() -> None:
    with pytest.raises(InvalidTerms, match="exactly one"):
        InstrumentAssumption("X")
    with pytest.raises(InvalidTerms, match="not a pass-through"):
        prepare_assumed_book(
            snapshot=book_snapshot(),
            instruments=(callable_bond(),),
            policy=AssumptionPolicy(
                overrides=(InstrumentAssumption("CORP-CALL", prepayment=PrepaymentAssumption(0.1)),)
            ),
        )
    with pytest.raises(InvalidTerms, match="duplicate"):
        AssumptionPolicy(
            overrides=(
                InstrumentAssumption("A", exercise=ExerciseAssumption.to_maturity()),
                InstrumentAssumption("A", exercise=ExerciseAssumption.to_maturity()),
            )
        )


def test_horizon_of_the_assumed_book_redeems_at_the_exercise() -> None:
    horizon = date(2027, 4, 15)
    result = horizon_assumed_portfolio(
        snapshot=book_snapshot(),
        instruments=(*instruments()[:3], callable_bond(), perpetual()),
        positions=book_positions(),
        policy=POLICY,
        assumptions=POLICY_FULL,
        horizon_date=horizon,
        pass_throughs=(pool(),),
    )
    assert result.coverage.status == "complete" and result.total_horizon_pnl_rub is not None
    rows = {p.position_id: p for p in result.horizon.positions}
    assert rows["p-call"].per_bond.redemption_date == FIRST_CALL
    assert rows["p-perp"].per_bond.redemption_date == date(2028, 12, 1)
    assert rows["p-mbs"].per_bond.principal_received_rub > 0.0
    plain = horizon_portfolio(
        snapshot=book_snapshot(),
        instruments=instruments()[:3],
        positions=book_positions()[:3],
        policy=POLICY,
        horizon_date=horizon,
    )
    for row in plain.positions:
        assert rows[row.position_id] == row
    assert BUSINESS_DATE < horizon
