"""One calibrated risk engine serves every position in an instrument (issue #110).

Positions in one instrument differ in quantity (and book), so ``value_portfolio`` calibrates
the engine and computes its per-bond differences once and multiplies by each quantity. The
tests hold that to the engine built for every position: the same bytes, down to the last bit.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterator, Mapping
from dataclasses import replace
from datetime import date, timedelta

import pytest
import test_portfolio
import test_portfolio_floaters

from rub_fixed_income import (
    BondCashflow,
    BondRiskEngine,
    BondRiskRequest,
    BondScenario,
    DiscountCurve,
    PortfolioRiskResult,
    PortfolioScenario,
    Position,
    ShockedCurveCache,
    ZSpreadConvention,
    canonical_json_bytes,
)
from rub_fixed_income import portfolio as portfolio_module
from rub_fixed_income import risk as risk_module
from rub_fixed_income.dates import add_months
from rub_fixed_income.risk import _EngineFamilies

QUANTITIES = (1000.0, -250.0, 0.5, 3.0e6, 7.0, -0.001, 123456.789)


class _OwnEngines:
    """What ``value_portfolio`` did before: an engine built for every position."""

    def __init__(self, counts: Mapping[str, int]) -> None:
        pass

    def engine(
        self, key: str, request: BondRiskRequest, curve_cache: ShockedCurveCache
    ) -> BondRiskEngine:
        return BondRiskEngine(request, curve_cache)

    def done(self, key: str) -> None:
        pass


def _repeated(positions: tuple[Position, ...]) -> tuple[Position, ...]:
    """Every position again for each quantity of ``QUANTITIES``, in another book."""
    rows = []
    for k, quantity in enumerate(QUANTITIES):
        for position in positions:
            rows.append(
                replace(
                    position,
                    position_id=f"{position.position_id}#{k}",
                    quantity=quantity * (1.0 if position.quantity >= 0 else -1.0),
                    book=f"BOOK-{k % 3}",
                )
            )
    return tuple(rows)


@pytest.fixture
def siblings(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[int]]:
    """Counts the engines taken from a donor, so a test cannot pass without any sharing."""
    taken: list[int] = []
    original = BondRiskEngine._sibling

    def spy(self: BondRiskEngine, request: BondRiskRequest) -> BondRiskEngine:
        taken.append(1)
        return original(self, request)

    monkeypatch.setattr(BondRiskEngine, "_sibling", spy)
    yield taken


def _both(
    monkeypatch: pytest.MonkeyPatch, value: Callable[[], PortfolioRiskResult]
) -> tuple[PortfolioRiskResult, PortfolioRiskResult]:
    shared = value()
    with monkeypatch.context() as patch:
        patch.setattr(portfolio_module, "_EngineFamilies", _OwnEngines)
        own = value()
    return shared, own


@pytest.mark.parametrize("second_order", [False, True])
def test_fixed_bonds_repeated_in_many_positions_give_the_same_bytes(
    monkeypatch: pytest.MonkeyPatch, siblings: list[int], second_order: bool
) -> None:
    policy = replace(test_portfolio.POLICY, include_second_order=second_order)
    book = _repeated(test_portfolio.positions()[:4])
    scenarios = (
        PortfolioScenario("parallel+100", BondScenario.parallel(100.0)),
        PortfolioScenario("spread+50", BondScenario.parallel(0.0, spread_shift_bp=50.0)),
    )
    shared, own = _both(
        monkeypatch,
        lambda: test_portfolio.run(positions=book, policy=policy, scenarios=scenarios),
    )
    assert siblings, "no engine was shared"
    assert shared.coverage.completed > len(QUANTITIES)
    assert canonical_json_bytes(shared) == canonical_json_bytes(own)
    assert shared.result_sha256 == own.result_sha256


@pytest.mark.parametrize("second_order", [False, True])
def test_floaters_repeated_in_many_positions_give_the_same_bytes(
    monkeypatch: pytest.MonkeyPatch, siblings: list[int], second_order: bool
) -> None:
    policy = replace(test_portfolio_floaters.POLICY, include_second_order=second_order)
    book = _repeated(test_portfolio_floaters.positions())
    shared, own = _both(
        monkeypatch,
        lambda: test_portfolio_floaters.run(positions=book, policy=policy),
    )
    assert siblings, "no engine was shared"
    assert shared.coverage.completed == len(book)
    assert canonical_json_bytes(shared) == canonical_json_bytes(own)


def test_a_rejected_position_in_the_family_does_not_change_the_others(
    monkeypatch: pytest.MonkeyPatch, siblings: list[int]
) -> None:
    book = _repeated(test_portfolio.positions()[:6])  # no-quote and unknown instruments reject
    shared, own = _both(monkeypatch, lambda: test_portfolio.run(positions=book))
    assert shared.coverage.rejections
    assert canonical_json_bytes(shared) == canonical_json_bytes(own)


START = date(2025, 1, 1)


def _request(**changes: object) -> BondRiskRequest:
    request = BondRiskRequest(
        cashflows=tuple(
            BondCashflow(add_months(START, 6 * k), 40.0 + (1000.0 if k == 8 else 0.0))
            for k in range(1, 9)
        ),
        settlement_date=START,
        curve=DiscountCurve(
            START,
            tuple(
                (START + timedelta(days=365 * y), math.exp(-(0.10 + 0.004 * y) * y))
                for y in (1, 2, 3, 5, 10)
            ),
        ),
        spread_convention=ZSpreadConvention(),
        spread_kind="credit",
        dirty_price_rub=970.0,
    )
    return replace(request, **changes)  # type: ignore[arg-type]


@pytest.mark.parametrize("method", ["bump", "analytic"])
def test_a_sibling_engine_is_the_engine_built_for_its_own_quantity(method: str) -> None:
    cache = ShockedCurveCache()
    families = _EngineFamilies({"BOND": len(QUANTITIES)})
    scenario = BondScenario((10.0,) * 5 + (-5.0,) * 6, 12.5)
    for quantity in QUANTITIES:
        request = _request(quantity=quantity)
        shared = families.engine("BOND", request, cache)
        own = BondRiskEngine(request, cache)
        assert shared == own
        assert shared.base_id == own.base_id
        assert shared.risk(difference="central", method=method) == own.risk(  # type: ignore[arg-type]
            difference="central",
            method=method,  # type: ignore[arg-type]
        )
        assert shared.second_order(method=method) == own.second_order(method=method)  # type: ignore[arg-type]
        assert shared.reprice(scenario) == own.reprice(scenario)
        assert shared.spread_bucket_risk() == own.spread_bucket_risk()
        assert shared.jump_to_default(
            outstanding_nominal_rub_per_bond=1000.0, recovery_rate=0.35
        ) == own.jump_to_default(outstanding_nominal_rub_per_bond=1000.0, recovery_rate=0.35)
        families.done("BOND")


def test_engines_are_shared_only_among_requests_that_differ_in_quantity() -> None:
    cache = ShockedCurveCache()
    families = _EngineFamilies({"BOND": 4})
    first = families.engine("BOND", _request(quantity=5.0), cache)
    second = families.engine("BOND", _request(quantity=-3.0), cache)
    assert second._family_memo is first._family_memo is not None
    assert second.projected_coupons is first.projected_coupons
    assert second.request.quantity == -3.0 and first.request.quantity == 5.0
    # Another price, another cash flow, another curve cache: not the donor's engine.
    other_price = families.engine("BOND", _request(dirty_price_rub=960.0), cache)
    assert other_price._family_memo is not first._family_memo
    assert other_price.spread_bp != first.spread_bp
    elsewhere = BondRiskEngine(_request(quantity=2.0), ShockedCurveCache())
    assert families.engine("BOND", _request(quantity=2.0), ShockedCurveCache()) == elsewhere
    # A key held by one position gets an ordinary engine.
    alone = _EngineFamilies({"BOND": 1}).engine("BOND", _request(quantity=2.0), cache)
    assert alone._family_memo is None


def test_prices_are_held_while_the_family_is_open_and_dropped_after_its_last_position() -> None:
    cache = ShockedCurveCache()
    families = _EngineFamilies({"BOND": 2})
    engine = families.engine("BOND", _request(quantity=1.0), cache)
    memo = engine._family_memo
    assert memo is not None
    engine.risk()
    assert memo.prices and memo.room[0] == risk_module._PRICE_MEMO_ROOM - len(memo.prices)
    families.done("BOND")
    assert memo.prices
    families.done("BOND")
    assert not memo.prices and not memo.units
    assert memo.room[0] == risk_module._PRICE_MEMO_ROOM
    # The engine still answers, and starts a small memo of its own again.
    assert engine.reprice(BondScenario.parallel(25.0)) == BondRiskEngine(
        _request(quantity=1.0), cache
    ).reprice(BondScenario.parallel(25.0))


def test_the_price_memo_never_changes_a_price_when_it_is_full(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(risk_module, "_PRICE_MEMO_ROOM", 3)
    cache = ShockedCurveCache()
    families = _EngineFamilies({"BOND": 2})
    engine = families.engine("BOND", _request(quantity=4.0), cache)
    assert engine.risk() == BondRiskEngine(_request(quantity=4.0), cache).risk()
    assert engine._family_memo is not None and len(engine._family_memo.prices) == 3


def test_signed_zero_shocks_have_their_own_memo_entries() -> None:
    engine = BondRiskEngine(_request(), ShockedCurveCache())
    object.__setattr__(engine, "_family_memo", risk_module._FamilyMemo([100]))
    zero = (0.0,) * 11
    negative_zero = tuple(-s for s in zero)
    engine._price(zero, 0.0)
    engine._price(negative_zero, -0.0)
    assert engine._family_memo is not None and len(engine._family_memo.prices) == 2


def test_the_default_book_has_a_family_per_repeated_instrument(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[dict[str, int]] = []
    original = _EngineFamilies.__init__

    def init(self: _EngineFamilies, counts: Mapping[str, int]) -> None:
        seen.append(dict(counts))
        original(self, counts)

    monkeypatch.setattr(_EngineFamilies, "__init__", init)
    test_portfolio.run(
        positions=(
            test_portfolio.positions()[0],
            replace(test_portfolio.positions()[0], position_id="again"),
            test_portfolio.positions()[1],
        )
    )
    assert seen == [{"OFZ-BULLET": 2, "OFZ-AMORT": 1}]
