"""Portfolio valuation under explicit call, perpetual and prepayment assumptions.

``value_portfolio`` keeps rejecting callable and perpetual bonds and knows nothing of
pass-throughs: its result contract (v2) has no field for an exercise or prepayment
assumption, and its numbers and hashes must not move. This module wraps it:

1. :func:`prepare_assumed_book` turns each callable or perpetual bond that has an
   :class:`ExerciseAssumption` into the deterministic bond the assumption implies
   (:func:`exercised_instrument`), and each :class:`PassThroughTerms` with a
   :class:`PrepaymentAssumption` into its projected bond
   (:func:`pass_through_instrument`). Every other instrument passes through untouched.
2. :func:`value_assumed_portfolio` / :func:`horizon_assumed_portfolio` value the book
   with the unchanged ``value_portfolio`` / ``horizon_portfolio`` and attach the
   assumptions used (:class:`AssumptionUse`), with coverage over every position.

A callable without an assumption is still rejected by ``value_portfolio`` as before.
An assumption that cannot be applied (no coupons to the call, a perpetual without a
call date, a missing CPR, a to_worst bond without a quote) is a typed rejection of
its positions, recorded in the merged coverage. Exercise dates and CPRs are held
fixed under every risk bump and scenario.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime
from typing import Literal

from ._money import finite_money
from .callables import (
    ExerciseAssumption,
    call_schedule,
    exercised_instrument,
    resolve_exercise,
)
from .canonical import canonical_sha256
from .cashflows import clean_price_pct_to_dirty_rub
from .domain import Instrument, MarketSnapshot, Position, ProductType
from .errors import CoreRejection, InvalidTerms
from .pass_through import (
    PassThroughTerms,
    PrepaymentAssumption,
    pass_through_instrument,
    project_pass_through,
    weighted_average_life,
)
from .portfolio import (
    CALCULATION_FAILED,
    PortfolioCoverage,
    PortfolioHorizonResult,
    PortfolioRiskResult,
    PortfolioScenario,
    Rejection,
    ValuationPolicy,
    horizon_portfolio,
    value_portfolio,
    verify_portfolio_result,
)
from .risk import ShockedCurveCache

__all__ = [
    "ASSUMED_BOOK_MODEL_VERSION",
    "AssumedBook",
    "AssumedHorizonResult",
    "AssumedPortfolioResult",
    "AssumptionPolicy",
    "AssumptionUse",
    "InstrumentAssumption",
    "MissingAssumption",
    "horizon_assumed_portfolio",
    "prepare_assumed_book",
    "value_assumed_portfolio",
    "verify_assumed_result",
]

ASSUMED_BOOK_MODEL_VERSION = "rub-assumed-book-v1"
AssumptionKind = Literal["callable", "perpetual", "pass_through"]
CoverageStatus = Literal["complete", "partial", "failed"]


class MissingAssumption(InvalidTerms):
    """A pass-through has no prepayment assumption; core never picks a CPR."""

    code = "MISSING_ASSUMPTION"


@dataclass(frozen=True)
class InstrumentAssumption:
    """The assumption for one instrument: an exercise or a prepayment, not both."""

    instrument_id: str
    exercise: ExerciseAssumption | None = None
    prepayment: PrepaymentAssumption | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.instrument_id, str) or not self.instrument_id.strip():
            raise InvalidTerms("instrument_id must be a non-empty string")
        if (self.exercise is None) == (self.prepayment is None):
            raise InvalidTerms("give exactly one of an exercise and a prepayment assumption")


@dataclass(frozen=True)
class AssumptionPolicy:
    """Per-instrument assumptions, and an optional exercise for every other callable.

    ``default_exercise`` applies to callable bonds without an override; perpetuals
    need an override (``to_call`` with their date) and pass-throughs a prepayment.
    """

    default_exercise: ExerciseAssumption | None = None
    overrides: tuple[InstrumentAssumption, ...] = ()

    def __post_init__(self) -> None:
        overrides = tuple(sorted(self.overrides, key=lambda o: o.instrument_id))
        object.__setattr__(self, "overrides", overrides)
        if len({o.instrument_id for o in overrides}) != len(overrides):
            raise InvalidTerms("duplicate instrument assumption")

    def override(self, instrument_id: str) -> InstrumentAssumption | None:
        for entry in self.overrides:
            if entry.instrument_id == instrument_id:
                return entry
        return None


@dataclass(frozen=True)
class AssumptionUse:
    """What was assumed for one instrument, what it resolved to, or why it failed.

    ``source_sha256`` hashes the terms given; ``valued_sha256`` the deterministic bond
    that was valued in their place. ``assumption`` is the assumption's identifier.
    """

    instrument_id: str
    kind: AssumptionKind
    assumption: str
    status: Literal["applied", "rejected"]
    code: str | None
    message: str | None
    settlement_date: date | None
    exercise: Literal["call", "maturity"] | None
    redemption_date: date | None
    wal_years: float | None
    source_sha256: str
    valued_sha256: str | None


@dataclass(frozen=True)
class AssumedBook:
    """Instruments ready for ``value_portfolio`` and the assumptions behind them.

    Instruments whose assumption was rejected are left out; their positions are
    rejected with the code in ``uses``.
    """

    instruments: tuple[Instrument, ...]
    uses: tuple[AssumptionUse, ...]
    model_version: str = ASSUMED_BOOK_MODEL_VERSION

    def rejection(self, instrument_id: str) -> AssumptionUse | None:
        for use in self.uses:
            if use.instrument_id == instrument_id and use.status == "rejected":
                return use
        return None


@dataclass(frozen=True)
class AssumedPortfolioResult:
    """``value_portfolio`` over the assumed book, with coverage over every position.

    ``portfolio`` is the unchanged v2 result for the positions whose assumptions
    applied; ``coverage`` adds the positions rejected for their assumption. Totals are
    ``None`` unless every position is covered.
    """

    portfolio: PortfolioRiskResult
    assumptions: tuple[AssumptionUse, ...]
    status: CoverageStatus
    coverage: PortfolioCoverage
    covered_pv_rub: float
    total_pv_rub: float | None
    model_version: str = ASSUMED_BOOK_MODEL_VERSION
    result_sha256: str = ""


@dataclass(frozen=True)
class AssumedHorizonResult:
    """``horizon_portfolio`` over the assumed book, with merged coverage."""

    horizon: PortfolioHorizonResult
    assumptions: tuple[AssumptionUse, ...]
    coverage: PortfolioCoverage
    covered_horizon_pnl_rub: float
    total_horizon_pnl_rub: float | None
    model_version: str = ASSUMED_BOOK_MODEL_VERSION


def _settlement(snapshot: MarketSnapshot, calendar_id: str, lag: int) -> date:
    return snapshot.calendar(calendar_id).add_business_days(snapshot.business_date, lag)


def _dirty(snapshot: MarketSnapshot, instrument: Instrument, settlement: date) -> float:
    quote = snapshot.quote(instrument.instrument_id)
    accrued = (
        quote.accrued_per_bond
        if quote.accrued_per_bond is not None
        else instrument.accrued(settlement)
    )
    return clean_price_pct_to_dirty_rub(
        quote.clean_price_pct, instrument.outstanding_face(settlement), accrued
    )


def _rejected(
    instrument_id: str, kind: AssumptionKind, assumption: str, source: str, exc: Exception
) -> AssumptionUse:
    code = exc.code if isinstance(exc, CoreRejection) else CALCULATION_FAILED
    message = exc.message if isinstance(exc, CoreRejection) else str(exc)
    return AssumptionUse(
        instrument_id,
        kind,
        assumption,
        "rejected",
        code,
        message,
        None,
        None,
        None,
        None,
        source,
        None,
    )


def _exercise(
    instrument: Instrument, assumption: ExerciseAssumption, snapshot: MarketSnapshot
) -> tuple[Instrument, AssumptionUse]:
    settlement = _settlement(snapshot, instrument.calendar_id, instrument.settlement_lag_days)
    dirty = _dirty(snapshot, instrument, settlement) if assumption.kind == "to_worst" else None
    resolved = resolve_exercise(instrument, settlement, assumption, dirty_price_rub=dirty)
    bond = exercised_instrument(instrument, resolved)
    use = AssumptionUse(
        instrument.instrument_id,
        "perpetual" if instrument.is_perpetual else "callable",
        resolved.identifier,
        "applied",
        None,
        None,
        settlement,
        resolved.exercise,
        resolved.redemption_date,
        None,
        bond.source_sha256,
        canonical_sha256(bond.instrument),
    )
    return bond.instrument, use


def _pass_through(
    terms: PassThroughTerms, assumption: PrepaymentAssumption, snapshot: MarketSnapshot
) -> tuple[Instrument, AssumptionUse]:
    settlement = _settlement(snapshot, terms.calendar_id, terms.settlement_lag_days)
    bond = pass_through_instrument(terms, assumption)
    wal = weighted_average_life(project_pass_through(terms, assumption).cashflows, settlement)
    use = AssumptionUse(
        terms.instrument_id,
        "pass_through",
        assumption.identifier,
        "applied",
        None,
        None,
        settlement,
        None,
        bond.maturity_date,
        wal,
        canonical_sha256(terms),
        canonical_sha256(bond),
    )
    return bond, use


def prepare_assumed_book(
    *,
    snapshot: MarketSnapshot,
    instruments: Sequence[Instrument],
    policy: AssumptionPolicy,
    pass_throughs: Sequence[PassThroughTerms] = (),
) -> AssumedBook:
    """Apply the policy's assumptions; see the module docstring.

    Settlement is the instrument's lag in its calendar from the snapshot's business
    date, as in ``value_portfolio``. ``to_worst`` is resolved at the snapshot quote.
    """
    ids = [i.instrument_id for i in instruments] + [t.instrument_id for t in pass_throughs]
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate instrument identifiers")
    out: list[Instrument] = []
    uses: list[AssumptionUse] = []
    for instrument in instruments:
        override = policy.override(instrument.instrument_id)
        if override is not None and override.prepayment is not None:
            raise InvalidTerms(
                f"{instrument.instrument_id} is not a pass-through; it takes an exercise"
            )
        optional = instrument.product is ProductType.FIXED_BOND and (
            instrument.is_perpetual or bool(call_schedule(instrument))
        )
        assumption = (
            override.exercise
            if override is not None
            else policy.default_exercise
            if optional and not instrument.is_perpetual
            else None
        )
        if assumption is None:
            out.append(instrument)
            continue
        kind: AssumptionKind = "perpetual" if instrument.is_perpetual else "callable"
        try:
            valued, use = _exercise(instrument, assumption, snapshot)
        except (CoreRejection, ValueError, ArithmeticError) as exc:
            uses.append(
                _rejected(
                    instrument.instrument_id,
                    kind,
                    assumption.identifier,
                    canonical_sha256(instrument),
                    exc,
                )
            )
            continue
        out.append(valued)
        uses.append(use)
    for terms in pass_throughs:
        override = policy.override(terms.instrument_id)
        source = canonical_sha256(terms)
        if override is None or override.prepayment is None:
            uses.append(
                _rejected(
                    terms.instrument_id,
                    "pass_through",
                    "",
                    source,
                    MissingAssumption(
                        "a pass-through needs an explicit prepayment assumption (constant CPR)"
                    ),
                )
            )
            continue
        try:
            valued, use = _pass_through(terms, override.prepayment, snapshot)
        except (CoreRejection, ValueError, ArithmeticError) as exc:
            uses.append(
                _rejected(
                    terms.instrument_id,
                    "pass_through",
                    override.prepayment.identifier,
                    source,
                    exc,
                )
            )
            continue
        out.append(valued)
        uses.append(use)
    return AssumedBook(tuple(out), tuple(sorted(uses, key=lambda u: u.instrument_id)))


def _merge(
    positions: Sequence[Position], book: AssumedBook, base: PortfolioCoverage
) -> PortfolioCoverage:
    by_position = {r.position_id: r for r in base.rejections}
    rejections: list[Rejection] = []
    for position in positions:
        use = book.rejection(position.instrument_id)
        if use is not None:
            assert use.code is not None and use.message is not None
            rejections.append(
                Rejection(position.position_id, position.instrument_id, use.code, use.message)
            )
        elif position.position_id in by_position:
            rejections.append(by_position[position.position_id])
    failed = {r.position_id for r in rejections}
    completed = sum(1 for p in positions if p.position_id not in failed)
    status: CoverageStatus = "complete" if not failed else "partial" if completed else "failed"
    return PortfolioCoverage(status, len(positions), completed, tuple(rejections))


def _split(positions: Sequence[Position], book: AssumedBook) -> tuple[Position, ...]:
    if len({p.position_id for p in positions}) != len(positions):
        raise ValueError("duplicate position identifiers")
    return tuple(p for p in positions if book.rejection(p.instrument_id) is None)


def value_assumed_portfolio(
    *,
    run_id: str,
    as_of: datetime,
    snapshot: MarketSnapshot,
    instruments: Sequence[Instrument],
    positions: Sequence[Position],
    policy: ValuationPolicy,
    assumptions: AssumptionPolicy,
    pass_throughs: Sequence[PassThroughTerms] = (),
    scenarios: Sequence[PortfolioScenario] = (),
    time_budget_seconds: float | None = None,
    should_cancel: Callable[[], bool] | None = None,
    curve_cache: ShockedCurveCache | None = None,
) -> AssumedPortfolioResult:
    """Value a book whose callables, perpetuals and pass-throughs carry assumptions.

    With no callable, perpetual or pass-through in the book, ``portfolio`` equals
    ``value_portfolio`` on the same inputs, hash for hash.
    """
    book = prepare_assumed_book(
        snapshot=snapshot, instruments=instruments, policy=assumptions, pass_throughs=pass_throughs
    )
    base = value_portfolio(
        run_id=run_id,
        as_of=as_of,
        snapshot=snapshot,
        instruments=book.instruments,
        positions=_split(positions, book),
        policy=policy,
        scenarios=scenarios,
        time_budget_seconds=time_budget_seconds,
        should_cancel=should_cancel,
        curve_cache=curve_cache,
    )
    coverage = _merge(positions, book, base.coverage)
    status: CoverageStatus = (
        "complete"
        if coverage.status == "complete" and base.status == "complete"
        else "partial"
        if coverage.completed
        else "failed"
    )
    result = AssumedPortfolioResult(
        base,
        book.uses,
        status,
        coverage,
        base.covered_pv_rub,
        base.covered_pv_rub if coverage.status == "complete" else None,
    )
    return replace(result, result_sha256=canonical_sha256(replace(result, result_sha256="")))


def verify_assumed_result(result: AssumedPortfolioResult) -> bool:
    """Both hashes verify: the embedded v2 result's and this wrapper's."""
    if not verify_portfolio_result(result.portfolio):
        return False
    return result.result_sha256 == canonical_sha256(replace(result, result_sha256=""))


def horizon_assumed_portfolio(
    *,
    snapshot: MarketSnapshot,
    instruments: Sequence[Instrument],
    positions: Sequence[Position],
    policy: ValuationPolicy,
    assumptions: AssumptionPolicy,
    horizon_date: date,
    pass_throughs: Sequence[PassThroughTerms] = (),
    curve_cache: ShockedCurveCache | None = None,
) -> AssumedHorizonResult:
    """Carry and rolldown of the assumed book (an exercised call is redeemed on its date)."""
    book = prepare_assumed_book(
        snapshot=snapshot, instruments=instruments, policy=assumptions, pass_throughs=pass_throughs
    )
    base = horizon_portfolio(
        snapshot=snapshot,
        instruments=book.instruments,
        positions=_split(positions, book),
        policy=policy,
        horizon_date=horizon_date,
        curve_cache=curve_cache,
    )
    coverage = _merge(positions, book, base.coverage)
    covered = finite_money(base.covered_horizon_pnl_rub, "covered horizon P&L")
    return AssumedHorizonResult(
        base,
        book.uses,
        coverage,
        covered,
        covered if coverage.status == "complete" else None,
    )
