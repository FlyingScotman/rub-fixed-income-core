"""Shared internals of the bond and FX-futures risk engines (not public API).

Bucket weights, curve snapshotting and shocking, the shocked-curve cache, central
derivatives and curve identities. ``rub_fixed_income.risk`` re-exports the public names.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import date
from itertools import pairwise

from ._money import finite_money, non_negative_money, positive_money
from .conventions import CurveConvention
from .discount import (
    CurveBootstrapRequest,
    DiscountCurve,
    DiscountFactorCurve,
    bootstrap_discount_curve,
)

RISK_BUCKETS = ("1M", "3M", "6M", "1Y", "2Y", "3Y", "5Y", "7Y", "10Y", "15Y", "20Y+")
RISK_PILLARS = (1 / 12, 0.25, 0.5, 1.0, 2.0, 3.0, 5.0, 7.0, 10.0, 15.0, 20.0)
_ZERO_SHOCK = (0.0,) * len(RISK_BUCKETS)


def _shifts(values: tuple[float, ...]) -> tuple[float, ...]:
    if len(values) != len(RISK_BUCKETS):
        raise ValueError("curve shock must contain exactly 11 bucket shifts")
    return tuple(finite_money(value, "curve shift bp") for value in values)


def key_rate_weights(time_years: float) -> tuple[float, ...]:
    """Linear hats, flat left/right tails; weights sum to one even beyond 20Y."""
    time = non_negative_money(time_years, "curve time")
    weights = [0.0] * len(RISK_PILLARS)
    if time <= RISK_PILLARS[0]:
        weights[0] = 1.0
    elif time >= RISK_PILLARS[-1]:
        weights[-1] = 1.0
    else:
        for i, (left, right) in enumerate(pairwise(RISK_PILLARS)):
            if left <= time <= right:
                fraction = (time - left) / (right - left)
                weights[i], weights[i + 1] = 1.0 - fraction, fraction
                break
    return tuple(weights)


def _shift_at(time: float, shifts: tuple[float, ...]) -> float:
    return finite_money(
        math.fsum(w * s for w, s in zip(key_rate_weights(time), shifts)), "interpolated shift"
    )


@dataclass(frozen=True)
class _ZeroShockCurve:
    base: DiscountCurve
    shifts: tuple[float, ...]

    @property
    def curve_date(self) -> date:
        return self.base.curve_date

    def discount_factor(self, target: date) -> float:
        time = self.base.convention.time_day_count.year_fraction(self.curve_date, target)
        shift = _shift_at(time, self.shifts) * 1e-4
        try:
            result = self.base.discount_factor(target) * math.exp(-shift * time)
        except OverflowError as exc:
            raise ValueError("shocked discount factor must be positive and finite") from exc
        return positive_money(result, "shocked discount factor")


CurveBuilder = Callable[[], DiscountCurve]


class ShockedCurveCache:
    """Memoize OIS rebootstraps by exact curve content and shift vector.

    Bootstrapping is deterministic, so equal (request, shifts) keys give identical
    curves. Share one cache across engines on the same curve within a job; risk,
    gamma and scenarios repeat the same shocked curves for every position. Failed
    bootstraps are not cached. Once ``max_entries`` is reached new curves are built
    but not stored, which bounds memory without changing any number.
    """

    def __init__(self, max_entries: int = 4096) -> None:
        if isinstance(max_entries, bool) or not isinstance(max_entries, int) or max_entries < 0:
            raise ValueError("max_entries must be a nonnegative integer")
        self.max_entries = max_entries
        self.hits = 0
        self.misses = 0
        self._curves: dict[tuple[CurveBootstrapRequest, tuple[float, ...]], DiscountCurve] = {}

    def __len__(self) -> int:
        return len(self._curves)

    def bootstrap(
        self, request: CurveBootstrapRequest, shifts: tuple[float, ...], build: CurveBuilder
    ) -> DiscountCurve:
        key = (request, shifts)
        cached = self._curves.get(key)
        if cached is not None:
            self.hits += 1
            return cached
        self.misses += 1
        curve = build()
        if len(self._curves) < self.max_entries:
            self._curves[key] = curve
        return curve


def _snapshot_convention(convention: CurveConvention) -> CurveConvention:
    schedule = convention.fixed_leg_schedule
    if schedule is None:
        return convention
    calendar = replace(
        schedule.calendar,
        holidays=frozenset(schedule.calendar.holidays),
        working_days=frozenset(schedule.calendar.working_days),
    )
    return replace(convention, fixed_leg_schedule=replace(schedule, calendar=calendar))


RiskCurve = DiscountCurve | CurveBootstrapRequest


def _snapshot_curve(curve: RiskCurve) -> RiskCurve:
    """Validate and copy a risk curve input so caller mutations cannot reach it."""
    curve = replace(curve, convention=_snapshot_convention(curve.convention))
    if isinstance(curve, CurveBootstrapRequest):
        quotes = tuple(sorted(curve.quotes, key=lambda q: q.maturity))
        for quote in quotes:
            finite_money(quote.par_rate, "OIS par rate")
        return replace(curve, quotes=quotes)
    nodes = tuple((d, positive_money(df, "discount factor")) for d, df in curve.nodes)
    if curve.flat_continuous_rate is not None:
        finite_money(curve.flat_continuous_rate, "flat continuous rate")
    return replace(curve, nodes=nodes)


def _curve_basis(curve: RiskCurve) -> str:
    return "ois_quote" if isinstance(curve, CurveBootstrapRequest) else "continuous_zero"


def _base_curve(source: RiskCurve, cache: ShockedCurveCache) -> DiscountCurve:
    if isinstance(source, CurveBootstrapRequest):
        return cache.bootstrap(source, _ZERO_SHOCK, lambda: bootstrap_discount_curve(source))
    return source


def _shock_curve(
    source: RiskCurve,
    base: DiscountCurve,
    shifts: tuple[float, ...],
    cache: ShockedCurveCache,
) -> DiscountFactorCurve:
    """Zero-rate overlay for a DiscountCurve, quote shift and rebootstrap for OIS."""
    if not any(shifts):
        return base
    if isinstance(source, CurveBootstrapRequest):

        def build() -> DiscountCurve:
            quotes = tuple(
                replace(
                    q,
                    par_rate=finite_money(
                        q.par_rate
                        + _shift_at(
                            source.convention.time_day_count.year_fraction(
                                source.curve_date, q.maturity
                            ),
                            shifts,
                        )
                        * 1e-4,
                        "shocked OIS quote",
                    ),
                )
                for q in source.quotes
            )
            return bootstrap_discount_curve(replace(source, quotes=quotes))

        return cache.bootstrap(source, shifts, build)
    return _ZeroShockCurve(base, shifts)


def _central_derivatives(
    price: Callable[[list[float]], float],
    size: int,
    bump: float,
    base_price: float,
    quantity: float,
) -> tuple[tuple[float, ...], tuple[tuple[float, ...], ...]]:
    """Central gradient and full Hessian of quantity * price over ``size`` bp factors."""
    square = positive_money(bump * bump, "squared second-order bump")
    positive_money(4 * square, "mixed-difference denominator")
    gradient = []
    hessian = [[0.0] * size for _ in range(size)]
    for i in range(size):
        moves = [0.0] * size
        moves[i] = bump
        up = price(moves)
        moves[i] = -bump
        down = price(moves)
        gradient.append(finite_money((up - down) / (2 * bump) * quantity, "gradient"))
        hessian[i][i] = finite_money(
            ((up - base_price) + (down - base_price)) / square * quantity,
            "diagonal gamma",
        )
        for j in range(i):
            values = []
            for sign_i, sign_j in ((1, 1), (1, -1), (-1, 1), (-1, -1)):
                pair = [0.0] * size
                pair[i], pair[j] = sign_i * bump, sign_j * bump
                values.append(price(pair))
            mixed = finite_money(
                ((values[0] - values[1]) - (values[2] - values[3])) / (4 * square) * quantity,
                "cross gamma",
            )
            hessian[i][j] = hessian[j][i] = mixed
    return tuple(gradient), tuple(tuple(row) for row in hessian)


def _curve_identity(curve: DiscountCurve | CurveBootstrapRequest) -> dict[str, object]:
    payload: dict[str, object] = {
        "curve_date": curve.curve_date,
        "convention": curve.convention.identifier,
    }
    if isinstance(curve, DiscountCurve):
        payload.update(
            nodes=curve.nodes, flat_rate=curve.flat_continuous_rate, model=curve.model_version
        )
    else:
        payload["quotes"] = [(q.tenor, q.maturity, q.par_rate) for q in curve.quotes]
    return payload
