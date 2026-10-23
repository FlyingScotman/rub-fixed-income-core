"""Shared internals of the bond and FX-futures risk engines (not public API).

Bucket weights, curve snapshotting and shocking, the shocked-curve cache, central
derivatives and curve identities. ``rub_fixed_income.risk`` re-exports the public names.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from datetime import date
from itertools import pairwise
from typing import cast

from ._money import finite_money, non_negative_money, positive_money
from ._par_overlay import par_overlay_shifts
from .canonical import _Canonical, _precanonical, canonical_sha256
from .conventions import CurveConvention
from .dates import BusinessCalendar
from .discount import (
    CurveBootstrapRequest,
    DiscountCurve,
    DiscountFactorCurve,
    bootstrap_discount_curve,
)
from .errors import InvalidTerms

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


_ZeroShockTerms = tuple[float, tuple[tuple[int, float], ...], float]
_ZERO_SHOCK_MEMO_SIZE = 4096
_ZERO_SHOCK_VIEWS = 512


def _zero_shock_terms(base: DiscountCurve, target: date) -> _ZeroShockTerms:
    """Time, nonzero bucket weights and base DF at ``target``, cached on the base curve.

    Every shocked view of one base curve shares these; only the shift differs. Zero
    weights add exact zeros to the shift sum, so leaving them out changes no result.
    """
    cache = base.__dict__.get("_zero_shock_cache")
    if cache is None:
        cache = {}
        object.__setattr__(base, "_zero_shock_cache", cache)
    terms = cast(dict[date, _ZeroShockTerms], cache).get(target)
    if terms is None:
        time = base.convention.time_day_count.year_fraction(base.curve_date, target)
        weights = tuple((i, w) for i, w in enumerate(key_rate_weights(time)) if w != 0.0)
        terms = (time, weights, base.discount_factor(target))
        cache[target] = terms
    return terms


@dataclass(frozen=True)
class _ZeroShockCurve:
    base: DiscountCurve
    shifts: tuple[float, ...]

    @property
    def curve_date(self) -> date:
        return self.base.curve_date

    def discount_factor(self, target: date) -> float:
        memo = self.__dict__.get("_df_memo")
        if memo is None:
            memo = {}
            object.__setattr__(self, "_df_memo", memo)
        cached = memo.get(target)
        if cached is not None:
            return cast(float, cached)
        value = self._discount_factor(target)
        if len(memo) < _ZERO_SHOCK_MEMO_SIZE:
            memo[target] = value
        return value

    def _discount_factor(self, target: date) -> float:
        time, weights, base_df = _zero_shock_terms(self.base, target)
        shifts = self.shifts
        shift = (
            finite_money(math.fsum(w * shifts[i] for i, w in weights), "interpolated shift") * 1e-4
        )
        try:
            if self.base.convention.zero_shock != "continuous" and time > 0.0:
                # Effective annual yield 1 + Y = DF^(-1/t), moved by the shift.
                growth = base_df ** (-1.0 / time) + shift
                if growth <= 0.0:
                    raise ValueError("shocked annual yield must exceed -100 %")
                result = growth ** (-time)
            else:
                result = base_df * math.exp(-shift * time)
        except OverflowError as exc:
            raise ValueError("shocked discount factor must be positive and finite") from exc
        return positive_money(result, "shocked discount factor")


CurveBuilder = Callable[[], DiscountCurve]


_PREBUILT_REPRICING_TOLERANCE_BP = 1e-7


@dataclass(frozen=True)
class PrebuiltCurve:
    """A bootstrapped OIS curve for ``request`` with bucket ``shifts`` (bp) applied.

    The backend persists these across jobs (``to_dict``/``from_dict``) and seeds a
    :class:`ShockedCurveCache` with them. ``curve_key`` is a stable string key:
    the canonical hash of the request and the shift vector.
    """

    request: CurveBootstrapRequest
    shifts: tuple[float, ...]
    curve: DiscountCurve

    def __post_init__(self) -> None:
        object.__setattr__(self, "shifts", _shifts(tuple(self.shifts)))

    @property
    def request_id(self) -> str:
        return canonical_sha256(self.request)

    @property
    def curve_key(self) -> str:
        return canonical_sha256({"request": self.request, "shifts": self.shifts})


def _shocked_request(
    source: CurveBootstrapRequest, shifts: tuple[float, ...]
) -> CurveBootstrapRequest:
    """The OIS request with each quote moved by the bucket shift at its maturity."""
    if not any(shifts):
        return source
    quotes = tuple(
        replace(
            q,
            par_rate=finite_money(
                q.par_rate
                + _shift_at(
                    source.convention.time_day_count.year_fraction(source.curve_date, q.maturity),
                    shifts,
                )
                * 1e-4,
                "shocked OIS quote",
            ),
        )
        for q in source.quotes
    )
    return replace(source, quotes=quotes)


def _check_prebuilt(entry: PrebuiltCurve) -> None:
    request, curve = entry.request, entry.curve
    if curve.curve_date != request.curve_date or curve.convention != request.convention:
        raise InvalidTerms("prebuilt curve date or convention differs from its request")
    if curve.model_version != DiscountCurve.model_version:
        raise InvalidTerms(f"prebuilt curve has model version {curve.model_version!r}")
    shocked = _shocked_request(request, entry.shifts)
    ordered = sorted(shocked.quotes, key=lambda q: q.maturity)
    if curve.max_repricing_error_bp(ordered) > _PREBUILT_REPRICING_TOLERANCE_BP:
        raise InvalidTerms("prebuilt curve does not reprice its shocked OIS quotes")


class ShockedCurveCache:
    """Memoize OIS rebootstraps by exact curve content and shift vector.

    Bootstrapping is deterministic, so equal (request, shifts) keys give identical
    curves. Share one cache across engines on the same curve within a job; risk,
    gamma and scenarios repeat the same shocked curves for every position. Failed
    bootstraps are not cached. Once ``max_entries`` is reached new curves are built
    but not stored, which bounds memory without changing any number.

    Across jobs, :meth:`export` hands the curves to the caller and :meth:`seed` takes
    them back; a seeded curve must reprice its (shocked) quotes exactly, as a fresh
    bootstrap does, so results with and without seeding are identical.
    """

    def __init__(self, max_entries: int = 4096) -> None:
        if isinstance(max_entries, bool) or not isinstance(max_entries, int) or max_entries < 0:
            raise ValueError("max_entries must be a nonnegative integer")
        self.max_entries = max_entries
        self.hits = 0
        self.misses = 0
        self.seeded = 0
        self._size = 0
        # Two levels so that the many lookups of one job's request skip rehashing it.
        self._curves: dict[CurveBootstrapRequest, dict[tuple[float, ...], DiscountCurve]] = {}
        self._last: tuple[CurveBootstrapRequest, dict[tuple[float, ...], DiscountCurve]] | None
        self._last = None

    def __len__(self) -> int:
        return self._size

    def _shocks(self, request: CurveBootstrapRequest) -> dict[tuple[float, ...], DiscountCurve]:
        last = self._last
        if last is not None and last[0] is request:
            return last[1]
        shocks = self._curves.setdefault(request, {})
        self._last = (request, shocks)
        return shocks

    def _store(
        self, request: CurveBootstrapRequest, shifts: tuple[float, ...], curve: DiscountCurve
    ) -> bool:
        shocks = self._shocks(request)
        if shifts in shocks or self._size >= self.max_entries:
            return False
        shocks[shifts] = curve
        self._size += 1
        return True

    def bootstrap(
        self, request: CurveBootstrapRequest, shifts: tuple[float, ...], build: CurveBuilder
    ) -> DiscountCurve:
        cached = self._shocks(request).get(shifts)
        if cached is not None:
            self.hits += 1
            return cached
        self.misses += 1
        curve = build()
        self._store(request, shifts, curve)
        return curve

    def export(self) -> tuple[PrebuiltCurve, ...]:
        """Every cached curve, ordered by ``curve_key``."""
        entries = (
            PrebuiltCurve(r, s, c) for r, shocks in self._curves.items() for s, c in shocks.items()
        )
        return tuple(sorted(entries, key=lambda e: e.curve_key))

    def seed(self, entries: Iterable[PrebuiltCurve]) -> int:
        """Add caller-held curves after checking each one; returns how many were added.

        Raises :class:`InvalidTerms` on the first curve that does not belong to its
        request; nothing from the batch is added then. Entries already cached or beyond
        ``max_entries`` are skipped.
        """
        batch = tuple(entries)
        for entry in batch:
            if not isinstance(entry, PrebuiltCurve):
                raise TypeError("seed expects PrebuiltCurve entries")
            _check_prebuilt(entry)
        added = sum(self._store(e.request, e.shifts, e.curve) for e in batch)
        self.seeded += added
        return added


def _snapshot_calendar(calendar: BusinessCalendar) -> BusinessCalendar:
    return replace(
        calendar,
        holidays=frozenset(calendar.holidays),
        working_days=frozenset(calendar.working_days),
    )


def _snapshot_convention(convention: CurveConvention) -> CurveConvention:
    ois = convention.ois_swap
    if ois is not None:
        convention = replace(
            convention, ois_swap=replace(ois, calendar=_snapshot_calendar(ois.calendar))
        )
    schedule = convention.fixed_leg_schedule
    if schedule is None:
        return convention
    calendar = _snapshot_calendar(schedule.calendar)
    return replace(convention, fixed_leg_schedule=replace(schedule, calendar=calendar))


RiskCurve = DiscountCurve | CurveBootstrapRequest


def _snapshot_curve(curve: RiskCurve) -> RiskCurve:
    """Validate and copy a risk curve input so caller mutations cannot reach it.

    A curve whose containers are already immutable tuples is snapshotted once and the
    snapshot reused, so every position on it shares one curve object and the caches
    kept on it (identity, zero-shock terms). A snapshot is its own snapshot.
    """
    cached = curve.__dict__.get("_risk_snapshot")
    if cached is not None:
        return cast(RiskCurve, cached)
    snapshot = _copy_curve(curve)
    if _is_immutable(curve):
        object.__setattr__(curve, "_risk_snapshot", snapshot)
    object.__setattr__(snapshot, "_risk_snapshot", snapshot)
    return snapshot


def _is_immutable(curve: RiskCurve) -> bool:
    if isinstance(curve, CurveBootstrapRequest):
        return type(curve.quotes) is tuple
    return type(curve.nodes) is tuple and all(type(node) is tuple for node in curve.nodes)


def _copy_curve(curve: RiskCurve) -> RiskCurve:
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


_CURVE_BASES = {"continuous": "continuous_zero", "annual": "annual_zero", "annual_par": "annual_par"}


def _curve_basis(curve: RiskCurve) -> str:
    if isinstance(curve, CurveBootstrapRequest):
        return "ois_quote"
    return _CURVE_BASES[curve.convention.zero_shock]


def _overlay_shifts(base: DiscountCurve, shifts: tuple[float, ...]) -> tuple[float, ...]:
    """The zero-yield bucket shifts a bucket shock of ``base`` applies: the shock itself, or
    for par-yield shocks (``zero_shock="annual_par"``) the solved overlay (``_par_overlay``)."""
    if base.convention.zero_shock != "annual_par" or not any(shifts):
        return shifts
    return par_overlay_shifts(base, shifts)


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
    """Zero-rate overlay for a DiscountCurve, quote shift and rebootstrap for OIS.

    A par-yield DiscountCurve (``zero_shock="annual_par"``) gets the annual-zero overlay that
    reprices its par instruments at the shocked yields; the view keeps the solved zero shifts
    and is cached under the par shifts.
    """
    if not any(shifts):
        return base
    if isinstance(source, CurveBootstrapRequest):

        def build() -> DiscountCurve:
            return bootstrap_discount_curve(_shocked_request(source, shifts))

        return cache.bootstrap(source, shifts, build)
    # One shared view per (base curve, shifts): positions on the same curve then reuse
    # its memoised discount factors. Bounded; beyond the bound views are not kept.
    views = base.__dict__.get("_zero_shock_views")
    if views is None:
        views = {}
        object.__setattr__(base, "_zero_shock_views", views)
    view = cast(dict[tuple[float, ...], _ZeroShockCurve], views).get(shifts)
    if view is None:
        moved = _overlay_shifts(base, shifts)
        if not any(moved):  # par shifts of the 1M-6M buckets only: nothing moves
            return base
        view = _ZeroShockCurve(base, moved)
        if len(views) < _ZERO_SHOCK_VIEWS:
            views[shifts] = view
    return view


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


def _curve_identity(curve: DiscountCurve | CurveBootstrapRequest) -> _Canonical:
    # Converted once per curve object and reused by every position's identity hash;
    # the canonical bytes are those of the plain payload.
    cached = curve.__dict__.get("_identity_canonical")
    if cached is not None:
        return cast(_Canonical, cached)
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
    identity = _precanonical(payload)
    object.__setattr__(curve, "_identity_canonical", identity)
    return identity
