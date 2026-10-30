"""OFZ curve risk as RUONIA OIS plus a basis, both computed (issue #83: variant Z, ZX tail).

The KBD on par factors (``rub_ofz_kbd_v3``, ``zero_shock="annual_par"``) is read as the RUONIA
OIS curve plus a basis: 22 rate factors (11 RUONIA quote buckets of the OIS curve, 11 basis par
buckets) instead of its 11 par buckets. Under RUONIA bucket shifts ``s`` and basis bucket shifts
``q`` (bp) the KBD's effective annual zero yield at curve time ``t`` (its day count from its
curve date) is

    Y'(t) = Y(t) + sum_n w_n(t) x_n(q) + [Z_s(t) - Z_0(t)],      DF'(t) = (1 + Y'(t))^(-t),

- ``x(q)``: the v3 par overlay solved on the base KBD (``_par_overlay``). With RUONIA held the
  shocked curve is the v3 view itself, so a basis bucket is a v3 par bucket and basis risk is the
  KBD's v3 par risk, to the bit; 1M-6M basis buckets have no instrument, as in v3;
- ``Z_s(t) = DF_s(t)^(-1/t) - 1``: the effective annual zero yield, on the KBD's curve time, of
  the RUONIA OIS curve rebootstrapped from quotes moved by ``s`` (``_shocked_request``: each
  quote by the bucket hats at its maturity), the rule of the OIS curve's own quote risk.

Shifts add in annual zero: the overlay is solved on the base KBD and the OIS move is added to it,
so after a joint move the KBD par yields are base + basis shift only up to the par effect of the
OIS move. First order does not depend on this convention; cross gammas and joint scenarios use it.

**Long end (ZX).** The OIS request used here is extended beyond its last quote with one synthetic
quote per v3 par tenor (``PAR_YEARS``) that matures later (15Y and 20Y after a 10Y last quote):
``KBD par(T) - s_tail``, where ``s_tail = KBD par(T_last) - quote(T_last)`` at the last real quote.
``KBD par(m)`` is the par yield of an annual-coupon bond on the base KBD from its curve date to
``m``: coupon dates rolled yearly from the curve date unadjusted, a roll date within 180 days
before ``m`` dropped (the last period is long), accrual on the KBD's day count (ACT/365F), DF
from the KBD; at a maturity on a roll date it is the v3 instrument. It is compared with the OIS
par quote as published (the spot-starting swap of its convention). A synthetic quote matures where
its tenor resolves under the OIS convention (spot lag and business-day rule of ``ois_swap``;
legacy requests: yearly from the curve date). The synthetic quotes are fixed at the base: RUONIA
buckets move them by their hats like any quote (15Y, 20Y+), basis buckets do not move the OIS
curve at all. Without them the OIS curve extrapolates flat-forward from its last segment and a
RUONIA bucket bump swings the long KBD by several bp of the opposite sign (research 22.10.2026).

The extended request differs from the OIS curve's own whenever a tenor is added, so its shocked
curves are separate :class:`ShockedCurveCache` entries (22 for first order, 220 more for the full
RUONIA Hessian per job); OIS positions keep the plain request. With no tenor to add the extended
request is the plain one and the curves are shared: so it is under ``ValuationPolicy.ois_tail``
(issue #84, :mod:`rub_fixed_income.ois_tail`), which extends the OIS entry itself by this rule.

:class:`OfzBasisRiskEngine` gives the risk of a bond on the KBD in these coordinates: by
bump-and-reprice (any bond the engine prices: fixed, perpetual tail, single-curve floaters on the
KBD, floaters projected off the same RUONIA request, and, mirrored (issue #90), KBD floaters
discounted on the RUONIA quotes: their discount curve is the plain OIS request rebootstrapped from
the shifted quotes, as an OIS swap's, and their projection the composed KBD above, whose OIS zero
move is that of the extended ZX request) or in closed form for fixed cash flows
(``method="analytic"``): the bond's annual-zero derivatives at its flow dates, mapped to basis
par factors by the v3 Jacobian with the instruments' curvature (``_analytic_risk``) and to RUONIA
factors by ``dZ/ds`` and ``d2Z/ds2`` of the OIS zero at each date, central differences of the
cached shocked OIS curves.

``factors.basis_view`` (variant P) stays as an approximation: it makes RUONIA risk equal to basis
risk bucket by bucket.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from datetime import date
from typing import Final, Literal, cast

import numpy as np

from ._analytic_risk import (
    _log_df_derivatives,
    _par_inverse,
    _par_map,
    analytic_second_order,
    flow_partials,
    supports_analytic,
)
from ._money import finite_money, positive_money
from ._par_overlay import PAR_FIRST_BUCKET
from ._risk_common import (
    _ZERO_SHOCK,
    RISK_BUCKETS,
    ShockedCurveCache,
    _base_curve,
    _central_derivatives,
    _check_scenario_shifts,
    _curve_identity,
    _overlay_shifts,
    _shifts,
    _shock_curve,
    _snapshot_curve,
    _zero_shock_terms,
)
from .canonical import canonical_sha256
from .compatibility import price_cashflows
from .discount import CurveBootstrapRequest, DiscountCurve, DiscountFactorCurve, OisQuote
from .errors import InvalidTerms
from .ois_tail import annual_par_yield, kbd_tail_quotes, tenor_maturity
from .perpetual import PerpetualTail
from .risk import (
    BondRiskEngine,
    BondRiskResult,
    BondScenarioResult,
    BondSecondOrderRisk,
    BucketRisk,
    TwoCurveBondRiskResult,
)

__all__ = [
    "OFZ_BASIS_CURVE_BASIS",
    "OFZ_BASIS_MODEL_VERSION",
    "OfzBasisCurve",
    "OfzBasisIds",
    "OfzBasisRiskEngine",
    "OfzBasisRiskResult",
]

OFZ_BASIS_MODEL_VERSION = "rub-ofz-ruonia-basis-v1"
OFZ_BASIS_ANALYTIC_MODEL_VERSION = "rub-ofz-ruonia-basis-analytic-v1"
_SECOND_ORDER_MODELS = {
    "bump": "rub-ofz-ruonia-basis-second-order-v1",
    "analytic": "rub-ofz-ruonia-basis-second-order-analytic-v1",
}
OFZ_BASIS_CURVE_BASIS: Final = "ruonia_ois_quote+kbd_par_basis"
"""``curve_basis`` of results in these coordinates (RUONIA OIS quotes plus v3 par basis)."""

_BP = 1e-4
_N = len(RISK_BUCKETS)
_STUB_DAYS = 180
# Bounds of the memos kept on an OfzBasisCurve (memory only, never numbers).
_VIEWS = 2048
_DF_MEMO = 4096
_JET_MEMO = 65_536


@dataclass(frozen=True)
class OfzBasisIds:
    """The curve IDs of the RUONIA + basis view (``ValuationPolicy.ofz_basis``).

    Bonds discounted on ``kbd_curve_id`` (a ``rub_ofz_kbd_v3`` KBD), and floaters discounted on
    ``ois_curve_id`` and projected off ``kbd_curve_id`` (issue #90), report
    ``IR|<ois_curve_id>|b`` (RUONIA, the OIS curve's own factors, so they add up with OIS swaps'
    RUONIA risk) and ``IR|<basis_id>|b`` (the basis) instead of ``IR|<kbd_curve_id>|b``.
    ``ois_curve_id`` must hold RUONIA OIS quotes (a ``CurveBootstrapRequest``); ``basis_id`` is a
    name, not a snapshot curve.
    """

    kbd_curve_id: str
    ois_curve_id: str
    basis_id: str

    def __post_init__(self) -> None:
        for label in ("kbd_curve_id", "ois_curve_id", "basis_id"):
            value = getattr(self, label)
            if not isinstance(value, str) or not value or "|" in value:
                raise InvalidTerms(f"{label} must be a non-empty string without '|'")
        if len({self.kbd_curve_id, self.ois_curve_id, self.basis_id}) != 3:
            raise InvalidTerms("the KBD, OIS and basis IDs must differ")


# Moved to ``ois_tail`` (issue #84), which the plain OIS tail shares; the names stay here.
_annual_par_yield = annual_par_yield
_tenor_maturity = tenor_maturity


@dataclass(frozen=True)
class _ComposedCurve:
    """The KBD under RUONIA shifts (through ``ois``, the shocked extended OIS curve) and the
    basis overlay's annual-zero node shifts ``zero_shifts`` (bp)."""

    owner: OfzBasisCurve
    zero_shifts: tuple[float, ...]
    ois: DiscountCurve

    @property
    def curve_date(self) -> date:
        return self.owner.kbd.curve_date

    def discount_factor(self, target: date) -> float:
        memo = self.__dict__.get("_df_memo")
        if memo is None:
            memo = {}
            object.__setattr__(self, "_df_memo", memo)
        cached = memo.get(target)
        if cached is not None:
            return cast(float, cached)
        value = self._discount_factor(target)
        if len(memo) < _DF_MEMO:
            memo[target] = value
        return value

    def _discount_factor(self, target: date) -> float:
        time, weights, base_df = _zero_shock_terms(self.owner.kbd, target)
        if time <= 0.0:
            return base_df
        shifts = self.zero_shifts
        shift = (
            finite_money(math.fsum(w * shifts[i] for i, w in weights), "interpolated shift") * 1e-4
            if any(shifts)
            else 0.0
        )
        try:
            moved = self.ois.discount_factor(target) ** (-1.0 / time)
            growth = base_df ** (-1.0 / time) + shift + (moved - self.owner._base_growth(target))
            if growth <= 0.0:
                raise ValueError("shocked annual yield must exceed -100 %")
            result = growth ** (-time)
        except OverflowError as exc:
            raise ValueError("shocked discount factor must be positive and finite") from exc
        return positive_money(result, "shocked discount factor")


@dataclass(frozen=True)
class OfzBasisCurve:
    """The KBD (v3) as RUONIA OIS plus a basis: variant Z with the ZX tail (module docstring).

    ``kbd`` is a ``DiscountCurve`` with ``zero_shock="annual_par"``; ``ois`` the RUONIA OIS
    quotes on the same date. ``extended`` is the ZX request (``ois`` plus ``synthetic_quotes``),
    ``tail_spread`` the decimal ``KBD par - OIS quote`` at the last real quote (None when no
    tenor is added). Build one per job and share it: views and the OIS derivatives per date are
    memoised on it.
    """

    kbd: DiscountCurve
    ois: CurveBootstrapRequest
    extended: CurveBootstrapRequest = field(init=False)
    synthetic_quotes: tuple[OisQuote, ...] = field(init=False)
    tail_spread: float | None = field(init=False)

    def __post_init__(self) -> None:
        if (
            not isinstance(self.kbd, DiscountCurve)
            or self.kbd.convention.zero_shock != "annual_par"
        ):
            raise InvalidTerms(
                "the RUONIA + basis view needs the KBD on par factors "
                "(zero_shock='annual_par', rub_ofz_kbd_v3)"
            )
        if not isinstance(self.ois, CurveBootstrapRequest):
            raise InvalidTerms("the RUONIA + basis view needs RUONIA OIS quotes to rebootstrap")
        if self.ois.curve_date != self.kbd.curve_date:
            raise InvalidTerms("the KBD and the OIS quotes must have the same curve date")
        kbd = cast(DiscountCurve, _snapshot_curve(self.kbd))
        ois = cast(CurveBootstrapRequest, _snapshot_curve(self.ois))
        object.__setattr__(self, "kbd", kbd)
        object.__setattr__(self, "ois", ois)
        synthetic, spread = kbd_tail_quotes(kbd, ois)
        object.__setattr__(self, "synthetic_quotes", synthetic)
        object.__setattr__(self, "tail_spread", spread if synthetic else None)
        extended = ois if not synthetic else replace(ois, quotes=(*ois.quotes, *synthetic))
        object.__setattr__(self, "extended", _snapshot_curve(extended))

    def identity(self) -> dict[str, object]:
        """Content identity: the KBD, the OIS quotes and the synthetic tail."""
        return {
            "model": OFZ_BASIS_MODEL_VERSION,
            "kbd": _curve_identity(self.kbd),
            "ois": _curve_identity(self.ois),
            "synthetic_quotes": [(q.tenor, q.maturity, q.par_rate) for q in self.synthetic_quotes],
        }

    def _memo(self, name: str) -> dict[object, object]:
        memo = self.__dict__.get(name)
        if memo is None:
            memo = {}
            object.__setattr__(self, name, memo)
        return cast(dict[object, object], memo)

    def ois_curve(self, shifts: tuple[float, ...], cache: ShockedCurveCache) -> DiscountCurve:
        """The extended OIS curve rebootstrapped from quotes moved by RUONIA ``shifts`` (cached)."""
        base = _base_curve(self.extended, cache)
        return cast(DiscountCurve, _shock_curve(self.extended, base, shifts, cache))

    def _base_growth(self, target: date) -> float:
        """``DF_0(target)^(-1/t)`` of the base extended OIS curve on the KBD's curve time."""
        memo = self._memo("_memo_growth")
        cached = memo.get(target)
        if cached is None:
            time = _zero_shock_terms(self.kbd, target)[0]
            base = cast(DiscountCurve, self.__dict__["_base_ois"])
            cached = base.discount_factor(target) ** (-1.0 / time)
            if len(memo) < _JET_MEMO:
                memo[target] = cached
        return cast(float, cached)

    def shocked(
        self,
        ruonia_shifts: tuple[float, ...],
        basis_shifts: tuple[float, ...],
        cache: ShockedCurveCache,
    ) -> DiscountFactorCurve:
        """The KBD under RUONIA and basis bucket shifts (bp). With RUONIA held it is the v3
        view of the basis shifts itself (``_risk_common._shock_curve``)."""
        if not any(ruonia_shifts):
            return _shock_curve(self.kbd, self.kbd, basis_shifts, cache)
        if "_base_ois" not in self.__dict__:
            object.__setattr__(self, "_base_ois", _base_curve(self.extended, cache))
        views = self._memo("_memo_views")
        key = (ruonia_shifts, basis_shifts)
        view = views.get(key)
        if view is None:
            zero = _overlay_shifts(self.kbd, basis_shifts) if any(basis_shifts) else _ZERO_SHOCK
            view = _ComposedCurve(self, zero, self.ois_curve(ruonia_shifts, cache))
            if len(views) < _VIEWS:
                views[key] = view
        return cast(_ComposedCurve, view)

    def _jet_curves(
        self, bump: float, cache: ShockedCurveCache, second: bool
    ) -> dict[tuple[float, ...], DiscountCurve]:
        """The shocked extended OIS curves central differences of ``bump`` read: single buckets
        and, with ``second``, the four corners of every bucket pair (the shift vectors of the
        bumped Hessian, so both paths share the cache entries)."""
        memo = self._memo("_memo_jet_curves")
        curves = cast(dict[tuple[float, ...], DiscountCurve] | None, memo.get((bump, second)))
        if curves is not None:
            return curves
        vectors: list[tuple[float, ...]] = [_ZERO_SHOCK]
        for i in range(_N):
            for sign in (1, -1):
                vectors.append(tuple(sign * bump if k == i else 0.0 for k in range(_N)))
            if second:
                for j in range(i):
                    for si, sj in ((1, 1), (1, -1), (-1, 1), (-1, -1)):
                        pair = [0.0] * _N
                        pair[i], pair[j] = si * bump, sj * bump
                        vectors.append(tuple(pair))
        curves = {v: self.ois_curve(v, cache) for v in vectors}
        memo[(bump, second)] = curves
        if "_base_ois" not in self.__dict__:
            object.__setattr__(self, "_base_ois", curves[_ZERO_SHOCK])
        return curves

    def ois_jets(
        self, target: date, bump: float, cache: ShockedCurveCache, *, second: bool = True
    ) -> tuple[np.ndarray, np.ndarray | None]:
        """``dZ/ds`` (decimal per bp) and, with ``second``, ``d2Z/ds2`` (decimal per bp^2) of the
        OIS annual zero at ``target`` on the KBD's curve time: central differences of ``bump``."""
        memo = self._memo("_memo_jets")
        key = (target, bump, second)
        cached = memo.get(key)
        if cached is not None:
            return cast(tuple[np.ndarray, np.ndarray | None], cached)
        time = _zero_shock_terms(self.kbd, target)[0]
        if time <= 0.0:
            result: tuple[np.ndarray, np.ndarray | None] = (
                np.zeros(_N),
                np.zeros((_N, _N)) if second else None,
            )
        else:
            curves = self._jet_curves(bump, cache, second)
            power = -1.0 / time

            def growth(vector: tuple[float, ...]) -> float:
                return cast(float, curves[vector].discount_factor(target) ** power)

            def unit(*moves: tuple[int, float]) -> tuple[float, ...]:
                vector = [0.0] * _N
                for k, value in moves:
                    vector[k] = value
                return tuple(vector)

            g0 = growth(_ZERO_SHOCK)
            ups = [growth(unit((i, bump))) for i in range(_N)]
            downs = [growth(unit((i, -bump))) for i in range(_N)]
            jet = np.array([(u - d) / (2.0 * bump) for u, d in zip(ups, downs, strict=True)])
            hess = None
            if second:
                square = bump * bump
                hess = np.zeros((_N, _N))
                for i in range(_N):
                    hess[i, i] = ((ups[i] - g0) + (downs[i] - g0)) / square
                    for j in range(i):
                        corners = [
                            growth(unit((i, si * bump), (j, sj * bump)))
                            for si, sj in ((1, 1), (1, -1), (-1, 1), (-1, -1))
                        ]
                        mixed = ((corners[0] - corners[1]) - (corners[2] - corners[3])) / (
                            4.0 * square
                        )
                        hess[i, j] = hess[j, i] = mixed
            result = (jet, hess)
        if len(memo) < _JET_MEMO:
            memo[key] = result
        return result


@dataclass(frozen=True)
class OfzBasisRiskResult:
    """Central risk of a bond on the KBD in RUONIA + basis coordinates, RUB/bp signed by quantity.

    ``basis_buckets`` and the spread risk are the KBD's v3 par risk (``BondRiskEngine.risk``)
    exactly (for a KBD floater discounted on RUONIA: its projection buckets); ``ruonia_buckets``
    the RUONIA quote buckets; ``dv01_rub`` sums all 22.
    """

    base_id: str
    curve_basis: str
    spread_convention: str
    difference: Literal["central"]
    bump_bp: float
    pv_rub: float
    spread_bp: float
    ruonia_buckets: tuple[BucketRisk, ...]
    basis_buckets: tuple[BucketRisk, ...]
    dv01_rub: float
    cs01_rub: float
    ofz_spr01_rub: float
    model_version: str = OFZ_BASIS_MODEL_VERSION


@dataclass(frozen=True)
class OfzBasisRiskEngine:
    """Risk of a bond on the KBD, in RUONIA + basis coordinates.

    ``engine`` is calibrated on ``curve.kbd`` (its spread is held, as in every risk engine); its
    ``curve_cache`` holds the shocked OIS curves. Factors: 11 RUONIA buckets, 11 basis buckets,
    then the spread. A floater may project off the KBD itself or off ``curve.ois`` (then a RUONIA
    shift moves its projection too); any other projection curve is :class:`InvalidTerms`.

    The mirrored case (issue #90, ``discounted_on == "ois"``): a KBD floater (``ZR_YLD_CRV``)
    discounted on ``curve.ois`` (the plain RUONIA quotes, its spread over them held) and projected
    off ``curve.kbd``. A RUONIA shift ``s`` moves both legs: the discount curve is the plain OIS
    request rebootstrapped from quotes moved by ``s`` (the bond's own ``IR|<ois>|*`` rule and an
    OIS swap's, the same cache entries) and the projection is the composed KBD of
    :meth:`OfzBasisCurve.shocked` (its OIS zero move from the extended ZX request, as for an OFZ). A
    basis shift moves the projection only, as a v3 par shift of the KBD with RUONIA held: basis
    risk is the engine's own projection risk (``projection_buckets``) to the bit. With no tenor
    added the two requests are one and both legs read the same shocked OIS curve. Bumped only
    (floating coupons re-project).
    """

    engine: BondRiskEngine
    curve: OfzBasisCurve
    base_id: str = field(init=False)
    discounted_on: Literal["kbd", "ois"] = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.engine, BondRiskEngine):
            raise TypeError("engine must be a BondRiskEngine")
        if not isinstance(self.curve, OfzBasisCurve):
            raise TypeError("curve must be an OfzBasisCurve")
        req = self.engine.request
        if _same(req.curve, self.curve.ois):
            if req.floating_leg is None or not _same(req.projection_curve, self.curve.kbd):
                raise InvalidTerms(
                    "a bond discounted on the RUONIA OIS quotes is decomposed into RUONIA + "
                    "basis only as a floater projected off the KBD of the view"
                )
            object.__setattr__(self, "discounted_on", "ois")
        else:
            if not isinstance(req.curve, DiscountCurve) or not _same(req.curve, self.curve.kbd):
                raise InvalidTerms(
                    "the bond must be priced on the KBD of the RUONIA + basis view (or on its "
                    "RUONIA OIS quotes as a floater projected off that KBD)"
                )
            if req.projection_curve is not None and req.projection_curve != self.curve.ois:
                raise InvalidTerms(
                    "a floater projected off a curve other than the KBD or the RUONIA OIS quotes "
                    "is not decomposed into RUONIA + basis"
                )
            object.__setattr__(self, "discounted_on", "kbd")
        object.__setattr__(
            self,
            "base_id",
            canonical_sha256({"bond": self.engine.base_id, "view": self.curve.identity()}),
        )

    @property
    def cache(self) -> ShockedCurveCache:
        return self.engine.curve_cache

    def _flows(
        self, curve: DiscountFactorCurve, projection: tuple[float, ...]
    ) -> tuple[tuple[tuple[date, float], ...], PerpetualTail | None]:
        """Flows and perpetual tail (a floating tail at its last projected coupon)."""
        eng = self.engine
        req = eng.request
        fixed = eng._fixed_flows()
        leg = req.floating_leg
        if leg is None:
            return fixed, req.perpetual_tail
        if req.projection_curve is None:
            coupons = tuple(
                leg.coupon(i, curve, check_guard=False) for i in leg.remaining(req.settlement_date)
            )
        else:
            moved = _shock_curve(req.projection_curve, eng._projection, projection, self.cache)
            coupons = eng._coupons(moved, projection)
        flows = tuple(sorted((*fixed, *((c.payment_date, c.amount_rub) for c in coupons))))
        return flows, eng.tail_for(coupons)

    def price(
        self,
        ruonia_shifts: tuple[float, ...],
        basis_shifts: tuple[float, ...] = _ZERO_SHOCK,
        spread_shift: float = 0.0,
        projection_shifts: tuple[float, ...] | None = None,
    ) -> float:
        """Dirty price per bond under RUONIA and basis bucket shifts and a spread shift (bp).

        A floater projected off the RUONIA quotes moves its projection by ``projection_shifts``
        (default: the RUONIA shifts). With RUONIA held this is the engine's own price on the v3
        view, to the bit. A KBD floater discounted on RUONIA (``discounted_on == "ois"``) is
        discounted on the plain OIS request moved by ``ruonia_shifts`` and projected off the
        composed KBD under ``projection_shifts`` and ``basis_shifts`` (:meth:`_ois_price`).
        """
        eng = self.engine
        projection = ruonia_shifts if projection_shifts is None else projection_shifts
        if self.discounted_on == "ois":
            return self._ois_price(ruonia_shifts, basis_shifts, spread_shift, projection)
        if not any(ruonia_shifts):
            return eng._price(basis_shifts, spread_shift, projection)
        req = eng.request
        try:
            curve = self.curve.shocked(ruonia_shifts, basis_shifts, self.cache)
            flows, tail = self._flows(curve, projection)
            return positive_money(
                price_cashflows(
                    flows,
                    curve,
                    spread=finite_money(eng.spread_bp + spread_shift, "shocked spread") * 1e-4,
                    settlement_date=req.settlement_date,
                    convention=req.spread_convention,
                    perpetual_tail=tail,
                ),
                "bond price",
            )
        except (OverflowError, ZeroDivisionError) as exc:
            raise ValueError("shock is outside the supported numerical domain") from exc

    def _ois_price(
        self,
        ruonia_shifts: tuple[float, ...],
        basis_shifts: tuple[float, ...],
        spread_shift: float,
        projection_ruonia: tuple[float, ...],
    ) -> float:
        """Price of a KBD floater discounted on the RUONIA quotes (``discounted_on == "ois"``).

        Discount: the plain OIS request under ``ruonia_shifts`` (``engine._shocked_curve``).
        Projection: the composed KBD under ``projection_ruonia`` and ``basis_shifts``; with no
        RUONIA move there it is the v3 view of the basis shifts and the price is the engine's
        two-curve price with the projection moved by them, to the bit.
        """
        eng = self.engine
        if not any(projection_ruonia):
            return eng._price(ruonia_shifts, spread_shift, basis_shifts)
        req = eng.request
        leg = req.floating_leg
        assert leg is not None
        try:
            discount = eng._shocked_curve(ruonia_shifts)
            projection = self.curve.shocked(projection_ruonia, basis_shifts, self.cache)
            coupons = tuple(
                leg.coupon(i, projection, check_guard=False)
                for i in leg.remaining(req.settlement_date)
            )
            flows = tuple(
                sorted((*eng._fixed_flows(), *((c.payment_date, c.amount_rub) for c in coupons)))
            )
            return positive_money(
                price_cashflows(
                    flows,
                    discount,
                    spread=finite_money(eng.spread_bp + spread_shift, "shocked spread") * 1e-4,
                    settlement_date=req.settlement_date,
                    convention=req.spread_convention,
                    perpetual_tail=eng.tail_for(coupons),
                ),
                "bond price",
            )
        except (OverflowError, ZeroDivisionError) as exc:
            raise ValueError("shock is outside the supported numerical domain") from exc

    def supports_analytic(self) -> str | None:
        """None when the closed form applies (fixed cash flows), else the reason it does not."""
        return supports_analytic(self.engine)

    def risk(
        self,
        *,
        bump_bp: float = 1.0,
        method: Literal["bump", "analytic"] = "bump",
        kbd_risk: BondRiskResult | None = None,
    ) -> OfzBasisRiskResult:
        """Central risk per RUONIA and basis bucket and to the spread.

        The basis buckets and the spread risk are ``engine.risk(difference="central")`` with the
        same bump and method (its ``projection_buckets`` for a KBD floater discounted on RUONIA,
        whose discount buckets are inside the RUONIA ones); pass that result as ``kbd_risk`` to
        reuse it. RUONIA buckets are central differences of :meth:`price` or, with
        ``method="analytic"`` (fixed cash flows), the closed form (``dZ/ds`` from central
        differences of the OIS curves with ``bump_bp``).
        """
        bump = positive_money(bump_bp, "risk bump bp")
        if method not in ("bump", "analytic"):
            raise ValueError("method must be bump or analytic")
        eng = self.engine
        if kbd_risk is not None and (
            kbd_risk.base_id != eng.base_id
            or kbd_risk.difference != "central"
            or kbd_risk.bump_bp != bump
        ):
            raise InvalidTerms("kbd_risk must be this engine's central risk with the same bump")
        kbd = (
            kbd_risk
            if kbd_risk is not None
            else eng.risk(bump_bp=bump, difference="central", method=method)
        )
        if method == "analytic":
            units = tuple(float(v) for v in self._ruonia_terms(bump, second=False)[0])
        else:
            units = tuple(
                finite_money(
                    (self.price(_unit(i, bump)) - self.price(_unit(i, -bump))) / (2.0 * bump),
                    "unit sensitivity",
                )
                for i in range(_N)
            )
        quantity = eng.request.quantity
        ruonia = tuple(
            BucketRisk(
                bucket,
                finite_money(unit * quantity, "bucket DV01"),
                finite_money(-unit / eng.base_price_rub * 10_000.0, "key rate duration"),
            )
            for bucket, unit in zip(RISK_BUCKETS, units, strict=True)
        )
        if self.discounted_on == "ois":
            # The basis is the KBD projection's v3 par risk; the discount buckets are in RUONIA.
            assert isinstance(kbd, TwoCurveBondRiskResult)
            basis = kbd.projection_buckets
        else:
            basis = kbd.buckets
        return OfzBasisRiskResult(
            base_id=self.base_id,
            curve_basis=OFZ_BASIS_CURVE_BASIS,
            spread_convention=eng.request.spread_convention.identifier,
            difference="central",
            bump_bp=bump,
            pv_rub=kbd.pv_rub,
            spread_bp=eng.spread_bp,
            ruonia_buckets=ruonia,
            basis_buckets=basis,
            dv01_rub=finite_money(
                math.fsum(b.dv01_rub for b in (*ruonia, *basis)), "bucket sum DV01"
            ),
            cs01_rub=kbd.cs01_rub,
            ofz_spr01_rub=kbd.ofz_spr01_rub,
            model_version=(
                OFZ_BASIS_ANALYTIC_MODEL_VERSION
                if method == "analytic"
                else OFZ_BASIS_MODEL_VERSION
            ),
        )

    def second_order(
        self, *, bump_bp: float = 1.0, method: Literal["bump", "analytic"] = "bump"
    ) -> BondSecondOrderRisk:
        """Central gradient and full Hessian over 11 RUONIA buckets, 11 basis buckets and the
        spread (23 x 23), RUB per bp and bp^2 signed by quantity.

        ``method="bump"``: central differences of :meth:`price` with ``bump_bp`` (any bond; for a
        KBD floater discounted on RUONIA the basis and spread block is the engine's own
        projection and spread block, the same repricings).
        ``method="analytic"`` (fixed cash flows): one pass over the flows. The basis and spread
        block is ``engine.second_order(method="analytic")`` to the bit; the RUONIA rows use the
        bond's annual-zero derivatives at its flow dates and ``dZ/ds``, ``d2Z/ds2`` of the OIS
        zero there (central differences of the shocked OIS curves with ``bump_bp``).
        """
        bump = positive_money(bump_bp, "second-order bump bp")
        if method not in ("bump", "analytic"):
            raise ValueError("method must be bump or analytic")
        eng = self.engine
        if method == "analytic":
            gradient, hessian = self._analytic_second_order(bump)
        else:

            def price(moves: list[float]) -> float:
                return self.price(tuple(moves[:_N]), tuple(moves[_N : 2 * _N]), moves[-1])

            gradient, hessian = _central_derivatives(
                price, 2 * _N + 1, bump, eng.base_price_rub, eng.request.quantity
            )
        return BondSecondOrderRisk(
            base_id=self.base_id,
            curve_basis=OFZ_BASIS_CURVE_BASIS,
            spread_convention=eng.request.spread_convention.identifier,
            factors=(
                *(f"ruonia {b}" for b in RISK_BUCKETS),
                *(f"basis {b}" for b in RISK_BUCKETS),
                eng.request.spread_kind + "_spread",
            ),
            bump_bp=bump,
            gradient_rub_per_bp=tuple(finite_money(g, "gradient") for g in gradient),
            hessian_rub_per_bp2=tuple(
                tuple(finite_money(v, "gamma") for v in row) for row in hessian
            ),
            model_version=_SECOND_ORDER_MODELS[method],
        )

    def reprice(
        self,
        ruonia_shifts: tuple[float, ...],
        basis_shifts: tuple[float, ...] = _ZERO_SHOCK,
        spread_shift: float = 0.0,
        projection_shifts: tuple[float, ...] | None = None,
    ) -> BondScenarioResult:
        """Exact same-date P&L of a joint RUONIA, basis and spread move (bp).

        ``projection_shifts`` (default: the RUONIA shifts) is the RUONIA move of a floater's
        projection: the OIS curve it projects off or, for a KBD floater discounted on RUONIA, the
        OIS zero move inside its composed KBD. Basis shifts follow the v3 scenario rule: 1M-6M
        zero or equal to 1Y (``InvalidTerms`` otherwise). ``rates_pnl_rub`` moves both rate
        families.
        """
        ruonia = _shifts(tuple(ruonia_shifts))
        basis = _shifts(tuple(basis_shifts))
        projection = None if projection_shifts is None else _shifts(tuple(projection_shifts))
        spread = finite_money(spread_shift, "spread shift bp")
        _check_scenario_shifts(self.curve.kbd, basis)
        eng = self.engine
        quantity = eng.request.quantity
        base = finite_money(eng.base_price_rub * quantity, "base PV")
        shocked = finite_money(
            self.price(ruonia, basis, spread, projection) * quantity, "shocked PV"
        )
        rates = finite_money(
            (self.price(ruonia, basis, 0.0, projection) - eng.base_price_rub) * quantity,
            "rates PnL",
        )
        credit = finite_money(
            (self.price(_ZERO_SHOCK, _ZERO_SHOCK, spread, _ZERO_SHOCK) - eng.base_price_rub)
            * quantity,
            "spread PnL",
        )
        total = finite_money(shocked - base, "total PnL")
        return BondScenarioResult(
            base_id=self.base_id,
            scenario_id=canonical_sha256(
                {
                    "ruonia_shifts_bp": ruonia,
                    "basis_shifts_bp": basis,
                    "spread_shift_bp": spread,
                    "projection_shifts_bp": projection,
                }
            ),
            curve_basis=OFZ_BASIS_CURVE_BASIS,
            spread_convention=eng.request.spread_convention.identifier,
            base_pv_rub=base,
            shocked_pv_rub=shocked,
            rates_pnl_rub=rates,
            spread_pnl_rub=credit,
            interaction_pnl_rub=finite_money(total - rates - credit, "interaction PnL"),
            total_pnl_rub=total,
            model_version=OFZ_BASIS_MODEL_VERSION,
        )

    # -- closed form ---------------------------------------------------------------------

    def _ruonia_terms(
        self, bump: float, *, second: bool
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        """Per bond (not signed by quantity), over RUONIA buckets ``s`` and the zero overlay
        nodes ``x`` of the KBD: ``dV/ds``, ``d2V/ds2``, ``d2V/dx ds`` and ``d2V/ds dZ``.

        Every flow is a function of ``L = ln DF'(t_k) - ln DF'(t_settle)`` with
        ``ln DF'(t) = -t ln(G(t) + w(t).x 1e-4 + dZ(t; s))``, ``G = DF^(-1/t)``.
        """
        reason = supports_analytic(self.engine)
        if reason is not None:
            raise InvalidTerms(reason)
        eng = self.engine
        req = eng.request
        kbd = eng._curve
        cache = self.cache
        settlement = req.settlement_date
        convention = req.spread_convention

        def terms(target: date) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
            """``d ln DF'/ds``, ``d2 ln DF'/ds2`` and ``d2 ln DF'/dx ds`` at ``target``."""
            time, weights, base_df = _zero_shock_terms(kbd, target)
            if time <= 0.0:
                return np.zeros(_N), np.zeros((_N, _N)), np.zeros((_N, _N))
            jet, hess = self.curve.ois_jets(target, bump, cache, second=second)
            growth = base_df ** (-1.0 / time)
            first = -time / growth
            if not second:
                return first * jet, np.zeros((_N, _N)), np.zeros((_N, _N))
            assert hess is not None
            w = np.zeros(_N)
            for i, value in weights:
                w[i] = value
            curvature = time / (growth * growth)
            return (
                first * jet,
                curvature * np.outer(jet, jet) + first * hess,
                curvature * np.outer(w * _BP, jet),
            )

        a_s, b_ss, b_xs = terms(settlement)
        x_s = _log_df_derivatives(kbd, settlement)[0] if second else np.zeros(_N)
        df_s = kbd.discount_factor(settlement)
        spread = eng.spread_bp * _BP
        g_s = np.zeros(_N)
        h_ss = np.zeros((_N, _N))
        h_xs = np.zeros((_N, _N))
        h_sz = np.zeros(_N)
        price = 0.0
        for payment, amount in eng._fixed_flows():
            if payment <= settlement or amount == 0.0:
                continue
            a_k, b_k, c_k = terms(payment)
            grad_s = a_k - a_s
            forward = kbd.discount_factor(payment) / df_s
            tau = convention.day_count.year_fraction(settlement, payment)
            value, p_l, p_ll, _, _, p_lz = flow_partials(amount, forward, tau, spread, convention)
            price += value
            g_s += p_l * grad_s
            if second:
                grad_x = _log_df_derivatives(kbd, payment)[0] - x_s
                h_ss += p_ll * np.outer(grad_s, grad_s) + p_l * (b_k - b_ss)
                h_xs += p_ll * np.outer(grad_x, grad_s) + p_l * (c_k - b_xs)
                h_sz += p_lz * _BP * grad_s
        if not np.isclose(price, eng.base_price_rub, rtol=1e-9, atol=1e-9):
            raise ValueError("closed-form price does not reproduce the engine's base price")
        return g_s, h_ss, h_xs, h_sz

    def _analytic_second_order(
        self, bump: float
    ) -> tuple[tuple[float, ...], tuple[tuple[float, ...], ...]]:
        kbd_gradient, kbd_hessian = analytic_second_order(self.engine)
        g_s, h_ss, h_xs, h_sz = self._ruonia_terms(bump, second=True)
        q = self.engine.request.quantity
        # Basis factors are the par quotes: d2V/dq ds = J^-T E^T d2V/dx ds (x(q) is free of s).
        h_qs = np.zeros((_N, _N))
        h_qs[PAR_FIRST_BUCKET:, :] = _par_inverse(self.engine._curve).T @ (_par_map().T @ h_xs)
        size = 2 * _N + 1
        full = np.zeros((size, size))
        full[:_N, :_N] = (h_ss + h_ss.T) / 2.0 * q
        full[_N:, _N:] = np.asarray(kbd_hessian)
        full[_N : 2 * _N, :_N] = h_qs * q
        full[:_N, _N : 2 * _N] = h_qs.T * q
        full[:_N, 2 * _N] = full[2 * _N, :_N] = h_sz * q
        gradient = (*(float(v) * q for v in g_s), *kbd_gradient)
        return gradient, tuple(tuple(float(v) for v in row) for row in full)


def _same(curve: object, other: object) -> bool:
    return curve is other or (type(curve) is type(other) and curve == other)


def _unit(index: int, bump: float) -> tuple[float, ...]:
    return tuple(bump if k == index else 0.0 for k in range(_N))
