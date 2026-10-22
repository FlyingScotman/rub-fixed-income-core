"""RUB bond risk and same-settlement full revaluation.

Shocks are absolute basis points. Spread compounding and the curve risk basis
are explicit inputs. No market access, position storage, or implicit FX conversion.
Floating coupons are re-projected off every shocked curve; known fixings and
published coupons stay frozen. A floater may project off its own curve, separate
from the discount curve; both then carry their own 11 bucket factors.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date
from typing import Literal, cast

from ._money import finite_money, non_negative_money, positive_money
from ._risk_common import (
    _ZERO_SHOCK,
    RISK_BUCKETS,
    RISK_PILLARS,
    CurveBuilder,
    PrebuiltCurve,
    RiskCurve,
    ShockedCurveCache,
    _base_curve,
    _central_derivatives,
    _curve_basis,
    _curve_identity,
    _shifts,
    _shock_curve,
    _snapshot_curve,
    _zero_shock_terms,
    key_rate_weights,
)
from .canonical import OMIT_IF_DEFAULT, _canonical_value, canonical_sha256
from .cashflows import BondCashflow
from .compatibility import price_cashflows, solve_z_spread
from .conventions import ZSpreadConvention
from .discount import (
    CurveBootstrapRequest,
    DiscountCurve,
    DiscountFactorCurve,
)
from .errors import InvalidTerms
from .floating import FloatingCouponProjection, FloatingLeg
from .perpetual import PerpetualTail

# Bucket weights, the shocked-curve cache and curve helpers live in ._risk_common since
# 0.12.0 (shared with fx_futures); the public names stay importable from here.
__all__ = [
    "FLOATING_RISK_MODEL_VERSION",
    "FLOATING_TWO_CURVE_RISK_MODEL_VERSION",
    "RISK_BUCKETS",
    "RISK_MODEL_VERSION",
    "RISK_PILLARS",
    "BondDefaultResult",
    "BondRiskEngine",
    "BondRiskRequest",
    "BondRiskResult",
    "BondScenario",
    "BondScenarioApproximation",
    "BondScenarioResult",
    "BondSecondOrderRisk",
    "BucketRisk",
    "CurveBuilder",
    "PrebuiltCurve",
    "RiskCurve",
    "ShockedCurveCache",
    "TwoCurveBondRiskResult",
    "key_rate_weights",
]

RISK_MODEL_VERSION = "rub-fixed-cashflow-risk-v1"
FLOATING_RISK_MODEL_VERSION = "rub-floating-coupon-risk-v1"
FLOATING_TWO_CURVE_RISK_MODEL_VERSION = "rub-floating-two-curve-risk-v1"
_SECOND_ORDER_MODELS = {
    RISK_MODEL_VERSION: "rub-fixed-cashflow-second-order-v1",
    FLOATING_RISK_MODEL_VERSION: "rub-floating-coupon-second-order-v1",
    FLOATING_TWO_CURVE_RISK_MODEL_VERSION: "rub-floating-two-curve-second-order-v1",
}
# Bounds on the per-engine memo of re-projected coupons (memory only, never numbers).
_COUPON_MEMO_SIZE = 65_536
_CURVE_MEMO_SIZE = 1024


@dataclass(frozen=True)
class BondRiskRequest:
    """Amounts are RUB per bond; quantity is signed bonds, price is dirty RUB/bond.

    Supply exactly one of spread_bp and dirty_price_rub. A DiscountCurve selects
    continuous-zero risk; a CurveBootstrapRequest selects par-OIS quote risk.
    Payments on/before settlement are excluded. Put/amortization schedules must
    already contain the contractual redemption and no post-horizon payments.

    ``floating_leg`` adds coupons projected off the risk curve itself (single-curve:
    the curve projects and discounts, the spread is a discount margin). Its fixing
    history must be dated at the curve date; ``cashflows`` then carry principal
    and any fixed amounts only.

    ``projection_curve`` (with a floating leg) projects the coupons off a separate
    curve, for example RUONIA OIS or a key-rate projection curve, while ``curve``
    discounts and the spread stays a margin over ``curve``. Both curves are dated
    alike and each is shocked in its own basis with its own 11 buckets.

    ``perpetual_tail`` (BACKLOG #125) adds a never-called perpetual's coupons after its
    last listed one, valued exactly on every shocked curve and spread
    (``perpetual.tail_pv_at_spread``); ``cashflows`` are then the listed coupons. Fixed
    coupons only.
    """

    cashflows: tuple[BondCashflow, ...]
    settlement_date: date
    curve: DiscountCurve | CurveBootstrapRequest
    spread_convention: ZSpreadConvention
    spread_kind: Literal["credit", "ofz"]
    spread_bp: float | None = None
    dirty_price_rub: float | None = None
    quantity: float = 1.0
    currency: Literal["RUB"] = "RUB"
    floating_leg: FloatingLeg | None = None
    projection_curve: DiscountCurve | CurveBootstrapRequest | None = field(
        default=None, metadata=OMIT_IF_DEFAULT
    )
    perpetual_tail: PerpetualTail | None = field(default=None, metadata=OMIT_IF_DEFAULT)

    def __post_init__(self) -> None:
        if self.currency != "RUB":
            raise ValueError("fixed-cashflow risk supports RUB only; convert explicitly upstream")
        if self.floating_leg is not None:
            if not isinstance(self.floating_leg, FloatingLeg):
                raise TypeError("floating_leg must be a FloatingLeg")
            if self.floating_leg.history.valuation_date != self.curve.curve_date:
                raise ValueError("floating fixing history must be dated at the curve date")
        if self.spread_kind not in ("credit", "ofz"):
            raise ValueError("spread_kind must be credit or ofz")
        if not isinstance(self.curve, (DiscountCurve, CurveBootstrapRequest)):
            raise TypeError("curve must be DiscountCurve or CurveBootstrapRequest")
        if not isinstance(self.spread_convention, ZSpreadConvention):
            raise TypeError("an explicit ZSpreadConvention is required")
        if self.settlement_date < self.curve.curve_date:
            raise ValueError("settlement cannot predate curve valuation")
        if (self.spread_bp is None) == (self.dirty_price_rub is None):
            raise ValueError("supply exactly one of spread_bp and dirty_price_rub")
        if self.spread_bp is not None:
            object.__setattr__(self, "spread_bp", finite_money(self.spread_bp, "spread bp"))
        if self.dirty_price_rub is not None:
            object.__setattr__(
                self, "dirty_price_rub", positive_money(self.dirty_price_rub, "dirty price")
            )
        object.__setattr__(self, "quantity", finite_money(self.quantity, "quantity"))
        flows = tuple(
            BondCashflow(flow.payment_date, non_negative_money(flow.amount, "bond cashflow"))
            for flow in self.cashflows
        )
        if self.perpetual_tail is not None:
            if not isinstance(self.perpetual_tail, PerpetualTail):
                raise TypeError("perpetual_tail must be a PerpetualTail")
            if self.floating_leg is not None:
                raise ValueError("a perpetual tail continues fixed coupons only")
            if any(f.payment_date > self.perpetual_tail.anchor_date for f in flows):
                raise ValueError("listed cashflows must not follow the perpetual tail's anchor")
        elif not any(f.payment_date > self.settlement_date and f.amount > 0.0 for f in flows):
            raise ValueError("at least one positive future bond cashflow is required")
        object.__setattr__(self, "cashflows", tuple(sorted(flows, key=lambda f: f.payment_date)))
        if self.projection_curve is not None:
            if self.floating_leg is None:
                raise ValueError("a projection curve needs a floating leg")
            if not isinstance(self.projection_curve, (DiscountCurve, CurveBootstrapRequest)):
                raise TypeError("projection_curve must be DiscountCurve or CurveBootstrapRequest")
            if self.projection_curve.curve_date != self.curve.curve_date:
                raise ValueError("projection and discount curves must have the same date")
            object.__setattr__(self, "projection_curve", _snapshot_curve(self.projection_curve))
        # Copy collection inputs so later caller mutations cannot change a calibrated state.
        object.__setattr__(self, "curve", _snapshot_curve(self.curve))


@dataclass(frozen=True)
class BucketRisk:
    bucket: str
    dv01_rub: float
    krd_years: float


@dataclass(frozen=True)
class BondRiskResult:
    base_id: str
    curve_basis: str
    spread_convention: str
    difference: str
    bump_bp: float
    pv_rub: float
    spread_bp: float
    buckets: tuple[BucketRisk, ...]
    dv01_rub: float
    parallel_dv01_rub: float
    cs01_rub: float
    ofz_spr01_rub: float
    model_version: str = RISK_MODEL_VERSION


@dataclass(frozen=True)
class TwoCurveBondRiskResult(BondRiskResult):
    """Risk of a floater whose coupons project off a separate curve.

    ``buckets`` are the discount curve's, ``projection_buckets`` the projection
    curve's; ``dv01_rub`` sums all 22 and ``parallel_dv01_rub`` moves both curves
    together. ``BondRiskResult`` itself keeps its fields, so stored results of
    single-curve risk are unchanged.
    """

    projection_curve_basis: str = "continuous_zero"
    projection_buckets: tuple[BucketRisk, ...] = ()


@dataclass(frozen=True)
class BondScenario:
    curve_shifts_bp: tuple[float, ...] = _ZERO_SHOCK
    spread_shift_bp: float = 0.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "curve_shifts_bp", _shifts(self.curve_shifts_bp))
        object.__setattr__(
            self, "spread_shift_bp", finite_money(self.spread_shift_bp, "spread shift bp")
        )

    @classmethod
    def parallel(cls, curve_shift_bp: float, *, spread_shift_bp: float = 0.0) -> BondScenario:
        return cls((curve_shift_bp,) * len(RISK_BUCKETS), spread_shift_bp)


@dataclass(frozen=True)
class BondScenarioResult:
    base_id: str
    scenario_id: str
    curve_basis: str
    spread_convention: str
    base_pv_rub: float
    shocked_pv_rub: float
    rates_pnl_rub: float
    spread_pnl_rub: float
    interaction_pnl_rub: float
    total_pnl_rub: float
    model_version: str = RISK_MODEL_VERSION


@dataclass(frozen=True)
class BondSecondOrderRisk:
    """Central gradient (RUB/bp) and full symmetric Hessian (RUB/bp squared)."""

    base_id: str
    curve_basis: str
    spread_convention: str
    factors: tuple[str, ...]
    bump_bp: float
    gradient_rub_per_bp: tuple[float, ...]
    hessian_rub_per_bp2: tuple[tuple[float, ...], ...]
    model_version: str = "rub-fixed-cashflow-second-order-v1"


@dataclass(frozen=True)
class BondScenarioApproximation:
    base_id: str
    scenario_id: str
    curve_basis: str
    second_order_id: str
    linear_pnl_rub: float
    rates_gamma_pnl_rub: float
    spread_gamma_pnl_rub: float
    rates_spread_cross_pnl_rub: float
    approximate_pnl_rub: float
    exact_pnl_rub: float
    residual_pnl_rub: float


@dataclass(frozen=True)
class BondDefaultResult:
    """Immediate recovery of outstanding principal; signed dirty-PV change."""

    base_id: str
    default_id: str
    outstanding_nominal_rub_per_bond: float
    recovery_rate: float
    base_pv_rub: float
    recovery_pv_rub: float
    jtd_pnl_rub: float
    model_version: str = "bond-immediate-principal-recovery-v1"


@dataclass(frozen=True)
class _Reuse:
    """A remaining coupon's base projection and what its projection reads."""

    index: int
    buckets: tuple[int, ...]
    last: date | None
    base: FloatingCouponProjection


@dataclass(frozen=True)
class BondRiskEngine:
    """Calibrate once, then hold the base spread fixed across every curve shock."""

    request: BondRiskRequest
    curve_cache: ShockedCurveCache = field(
        default_factory=ShockedCurveCache, compare=False, repr=False
    )
    spread_bp: float = field(init=False)
    base_price_rub: float = field(init=False)
    base_id: str = field(init=False)
    projected_coupons: tuple[FloatingCouponProjection, ...] = field(init=False)
    _curve: DiscountCurve = field(init=False, repr=False)
    _projection: DiscountCurve = field(init=False, repr=False, compare=False)
    _reuse: tuple[_Reuse, ...] = field(init=False, repr=False, compare=False)
    _coupon_memo: dict[object, object] = field(
        init=False, repr=False, compare=False, default_factory=dict
    )

    def __post_init__(self) -> None:
        req = self.request
        if not isinstance(self.curve_cache, ShockedCurveCache):
            raise TypeError("curve_cache must be a ShockedCurveCache")
        curve = _base_curve(req.curve, self.curve_cache)
        object.__setattr__(self, "_curve", curve)
        projection = (
            curve
            if req.projection_curve is None
            else _base_curve(req.projection_curve, self.curve_cache)
        )
        object.__setattr__(self, "_projection", projection)
        object.__setattr__(self, "projected_coupons", self._project(projection))
        object.__setattr__(self, "_reuse", self._reuse_plan())
        spread = req.spread_bp
        if spread is None:
            assert req.dirty_price_rub is not None
            spread = (
                solve_z_spread(
                    self._flows(curve, projection, _ZERO_SHOCK),
                    req.dirty_price_rub,
                    curve,
                    settlement_date=req.settlement_date,
                    convention=req.spread_convention,
                    perpetual_tail=req.perpetual_tail,
                )
                * 10_000.0
            )
        object.__setattr__(self, "spread_bp", finite_money(spread, "calibrated spread"))
        price = self._price(_ZERO_SHOCK, 0.0)
        object.__setattr__(self, "base_price_rub", positive_money(price, "base price"))
        if req.dirty_price_rub is not None and not math.isclose(
            price, req.dirty_price_rub, rel_tol=1e-10, abs_tol=1e-8
        ):
            raise ValueError("base spread calibration failed to reprice the dirty price")
        identity: dict[str, object] = {
            "model": self.model_version,
            "curve_basis": self.curve_basis,
            "curve": _curve_identity(req.curve),
            "cashflows": self._fixed_flows(),
            "settlement": req.settlement_date,
            "spread_convention": req.spread_convention.identifier,
            "spread_kind": req.spread_kind,
            "input_spread_bp": req.spread_bp,
            "input_dirty_price": req.dirty_price_rub,
            "calibrated_spread_bp": spread,
            "quantity": req.quantity,
            "currency": req.currency,
        }
        if req.floating_leg is not None:
            identity["floating_leg"] = req.floating_leg.identity
            # Extension fields at their default are left out, as in the 0.8.0 identity.
            identity["projected_coupons"] = [_canonical_value(c) for c in self.projected_coupons]
        if req.projection_curve is not None:
            identity["projection_curve_basis"] = self.projection_curve_basis
            identity["projection_curve"] = _curve_identity(req.projection_curve)
        if req.perpetual_tail is not None:
            identity["perpetual_tail"] = req.perpetual_tail
        object.__setattr__(self, "base_id", canonical_sha256(identity))

    @property
    def curve_basis(self) -> str:
        return _curve_basis(self.request.curve)

    @property
    def two_curve(self) -> bool:
        """Whether coupons project off a curve separate from the discount curve."""
        return self.request.projection_curve is not None

    def spread_bucket_risk(
        self, *, bump_bp: float = 1.0, risk: BondRiskResult | None = None
    ) -> tuple[float, ...]:
        """Own-spread risk by bucket, RUB per bp for the position (signed by quantity).

        The central parallel spread sensitivity (the ``CS`` factor: CS01, or the OFZ
        spread risk) split over :data:`RISK_BUCKETS` in proportion to the discount-only
        key-rate risk: the central sensitivity to a bucket shift of the discount curve
        with every coupon held at its base projection. The buckets sum to the parallel
        sensitivity. A fixed bond's discount-only risk is its bucket DV01; a floater's
        excludes the coupon re-projection, so on a single curve (a RUONIA floater on
        RUONIA OIS, an OFZ-PK) it is the risk of the spread or basis over that curve.

        ``risk`` (central, the same bump) reuses a result already computed for this
        engine where it holds the discount-only buckets (fixed bonds and two-curve
        floaters).
        """
        bump = positive_money(bump_bp, "risk bump bp")
        quantity = self.request.quantity
        single_floater = self.request.floating_leg is not None and not self.two_curve
        if (
            risk is not None
            and risk.difference == "central"
            and risk.bump_bp == bump
            and not single_floater
        ):
            units = [b.dv01_rub for b in risk.buckets]
            parallel = risk.cs01_rub if self.request.spread_kind == "credit" else risk.ofz_spr01_rub
        else:
            held = self._flows(self._projection, self._projection, _ZERO_SHOCK)

            def price(shifts: tuple[float, ...], spread: float) -> float:
                curve = self._shocked_curve(shifts)
                return price_cashflows(
                    held,
                    curve,
                    spread=finite_money(self.spread_bp + spread, "shocked spread") * 1e-4,
                    settlement_date=self.request.settlement_date,
                    convention=self.request.spread_convention,
                    perpetual_tail=self.request.perpetual_tail,
                )

            units = []
            for i in range(len(RISK_BUCKETS)):
                up = tuple(bump if i == j else 0.0 for j in range(len(RISK_BUCKETS)))
                down = tuple(-s for s in up)
                units.append((price(up, 0.0) - price(down, 0.0)) / (2.0 * bump) * quantity)
            parallel = (
                (self._price(_ZERO_SHOCK, bump) - self._price(_ZERO_SHOCK, -bump))
                / (2.0 * bump)
                * quantity
            )
        total = math.fsum(units)
        if total == 0.0:
            return tuple(0.0 for _ in units)
        return tuple(finite_money(parallel * u / total, "spread bucket risk") for u in units)

    def discount_margin_bp(self) -> float:
        """A floater's discount margin, bp: the spread over its projection curve.

        The base projected flows (published coupons, realised fixings and today's
        forwards, principal) discounted on the projection curve plus this spread, in the
        request's spread convention, reprice the base price. On a single curve it is the
        calibrated spread itself; with a separate projection curve (a key-rate floater
        discounted on RUONIA OIS) it is the margin over the key-rate forwards.
        """
        if self.request.floating_leg is None:
            raise InvalidTerms("a discount margin needs a floating leg")
        if not self.two_curve:
            return self.spread_bp
        projection = self._projection
        flows = self._flows(projection, projection, _ZERO_SHOCK)
        margin = solve_z_spread(
            flows,
            self.base_price_rub,
            projection,
            settlement_date=self.request.settlement_date,
            convention=self.request.spread_convention,
        )
        return finite_money(margin * 10_000.0, "discount margin")

    @property
    def projection_curve_basis(self) -> str | None:
        source = self.request.projection_curve
        return None if source is None else _curve_basis(source)

    @property
    def model_version(self) -> str:
        if self.request.floating_leg is None:
            return RISK_MODEL_VERSION
        if self.two_curve:
            return FLOATING_TWO_CURVE_RISK_MODEL_VERSION
        return FLOATING_RISK_MODEL_VERSION

    def _fixed_flows(self) -> tuple[tuple[date, float], ...]:
        return tuple((f.payment_date, f.amount) for f in self.request.cashflows)

    def _project(self, curve: DiscountFactorCurve) -> tuple[FloatingCouponProjection, ...]:
        leg = self.request.floating_leg
        if leg is None:
            return ()
        return leg.project(curve, settlement_date=self.request.settlement_date)

    def _reuse_plan(self) -> tuple[_Reuse, ...]:
        """Per remaining coupon: the buckets and the last date its projection reads.

        A zero-rate shock leaves every discount factor outside its buckets' support
        bit-for-bit unchanged, and a rebootstrapped OIS curve agrees with the base up
        to its first changed node, so a coupon that reads only unchanged factors is
        reused exactly instead of re-projected.
        """
        leg = self.request.floating_leg
        if leg is None:
            return ()
        indexes = leg.remaining(self.request.settlement_date)
        plan = []
        for index, base in zip(indexes, self.projected_coupons, strict=True):
            dates = leg.projection_dates(index)
            buckets: set[int] = set()
            for target in dates:
                buckets.update(i for i, _ in _zero_shock_terms(self._projection, target)[1])
            plan.append(_Reuse(index, tuple(sorted(buckets)), dates[-1] if dates else None, base))
        return tuple(plan)

    def _coupons(
        self, curve: DiscountFactorCurve, shifts: tuple[float, ...]
    ) -> tuple[FloatingCouponProjection, ...]:
        if curve is self._projection:
            return self.projected_coupons
        leg = self.request.floating_leg
        assert leg is not None
        memo = self._coupon_memo
        req = self.request
        source = req.curve if req.projection_curve is None else req.projection_curve
        if isinstance(source, DiscountCurve):
            coupons = []
            for reuse in self._reuse:
                key = (reuse.index, tuple(shifts[k] for k in reuse.buckets))
                if not any(key[1]):
                    coupons.append(reuse.base)
                    continue
                cached = memo.get(key)
                if cached is None:
                    cached = leg.coupon(reuse.index, curve, check_guard=False)
                    if len(memo) < _COUPON_MEMO_SIZE:
                        memo[key] = cached
                coupons.append(cast(FloatingCouponProjection, cached))
            return tuple(coupons)
        held = cast(tuple[object, tuple[FloatingCouponProjection, ...]] | None, memo.get(id(curve)))
        if held is not None and held[0] is curve:
            return held[1]
        agreed = _agreement_date(self._projection, curve)
        result = tuple(
            reuse.base
            if reuse.last is None or (agreed is not None and reuse.last <= agreed)
            else leg.coupon(reuse.index, curve, check_guard=False)
            for reuse in self._reuse
        )
        if len(memo) < _CURVE_MEMO_SIZE:
            memo[id(curve)] = (curve, result)
        return result

    def _flows(
        self,
        curve: DiscountFactorCurve,
        projection: DiscountFactorCurve,
        projection_shifts: tuple[float, ...],
    ) -> tuple[tuple[date, float], ...]:
        fixed = self._fixed_flows()
        if self.request.floating_leg is None:
            return fixed
        coupons = self._coupons(projection, projection_shifts)
        return tuple(sorted((*fixed, *((c.payment_date, c.amount_rub) for c in coupons))))

    def _shocked_curve(self, shifts: tuple[float, ...]) -> DiscountFactorCurve:
        return _shock_curve(self.request.curve, self._curve, shifts, self.curve_cache)

    def _price(
        self,
        shifts: tuple[float, ...],
        spread_shift: float,
        projection_shifts: tuple[float, ...] = _ZERO_SHOCK,
    ) -> float:
        """Price with the discount curve shifted by ``shifts``.

        Single-curve, the shocked discount curve also projects and
        ``projection_shifts`` is ignored; two-curve, they shift the projection curve.
        """
        try:
            curve = self._shocked_curve(shifts)
            source = self.request.projection_curve
            projection: DiscountFactorCurve
            if source is None:
                projection, moves = curve, shifts
            else:
                projection = _shock_curve(
                    source, self._projection, projection_shifts, self.curve_cache
                )
                moves = projection_shifts
            return positive_money(
                price_cashflows(
                    self._flows(curve, projection, moves),
                    curve,
                    spread=finite_money(self.spread_bp + spread_shift, "shocked spread") * 1e-4,
                    settlement_date=self.request.settlement_date,
                    convention=self.request.spread_convention,
                    perpetual_tail=self.request.perpetual_tail,
                ),
                "bond price",
            )
        except (OverflowError, ZeroDivisionError) as exc:
            raise ValueError("shock is outside the supported numerical domain") from exc

    def risk(
        self, *, bump_bp: float = 1.0, difference: Literal["forward", "central"] = "forward"
    ) -> BondRiskResult:
        """Signed RUB/bp. Default is exact +1bp P&L; central is a derivative estimate.

        dv01 is the bucket sum. parallel_dv01 is independently repriced: finite
        bump convexity means these need not be identical. KRD = -unit_dv01/P/1e-4.
        With a separate projection curve, ``buckets`` are the discount curve's and
        ``projection_buckets`` the projection curve's; dv01 sums both and
        parallel_dv01 moves both curves together.
        """
        bump = positive_money(bump_bp, "risk bump bp")
        if difference not in ("forward", "central"):
            raise ValueError("difference must be forward or central")

        def sensitivity(
            shifts: tuple[float, ...], spread: float, projection: tuple[float, ...] = _ZERO_SHOCK
        ) -> float:
            up = self._price(shifts, spread, projection)
            down = (
                self._price(tuple(-s for s in shifts), -spread, tuple(-s for s in projection))
                if difference == "central"
                else self.base_price_rub
            )
            denominator = 2.0 * bump if difference == "central" else bump
            return finite_money((up - down) / denominator, "unit sensitivity")

        quantity = self.request.quantity

        def bucket_risk(bucket: str, unit: float) -> BucketRisk:
            return BucketRisk(
                bucket,
                finite_money(unit * quantity, "bucket DV01"),
                finite_money(-unit / self.base_price_rub * 10_000.0, "key rate duration"),
            )

        buckets = []
        projection_buckets = []
        for i, bucket in enumerate(RISK_BUCKETS):
            shifts = tuple(bump if i == j else 0.0 for j in range(len(RISK_BUCKETS)))
            buckets.append(bucket_risk(bucket, sensitivity(shifts, 0.0)))
            if self.two_curve:
                projection_buckets.append(
                    bucket_risk(bucket, sensitivity(_ZERO_SHOCK, 0.0, shifts))
                )
        everything = (bump,) * len(RISK_BUCKETS)
        parallel = finite_money(
            sensitivity(everything, 0.0, everything if self.two_curve else _ZERO_SHOCK) * quantity,
            "DV01",
        )
        spread = finite_money(sensitivity(_ZERO_SHOCK, bump) * quantity, "spread sensitivity")
        result = BondRiskResult(
            base_id=self.base_id,
            curve_basis=self.curve_basis,
            spread_convention=self.request.spread_convention.identifier,
            difference=difference,
            bump_bp=bump,
            pv_rub=finite_money(self.base_price_rub * quantity, "position PV"),
            spread_bp=self.spread_bp,
            buckets=tuple(buckets),
            dv01_rub=finite_money(
                math.fsum(b.dv01_rub for b in (*buckets, *projection_buckets)), "bucket sum DV01"
            ),
            parallel_dv01_rub=parallel,
            cs01_rub=spread if self.request.spread_kind == "credit" else 0.0,
            ofz_spr01_rub=spread if self.request.spread_kind == "ofz" else 0.0,
            model_version=self.model_version,
        )
        if not self.two_curve:
            return result
        return TwoCurveBondRiskResult(
            base_id=result.base_id,
            curve_basis=result.curve_basis,
            spread_convention=result.spread_convention,
            difference=result.difference,
            bump_bp=result.bump_bp,
            pv_rub=result.pv_rub,
            spread_bp=result.spread_bp,
            buckets=result.buckets,
            dv01_rub=result.dv01_rub,
            parallel_dv01_rub=result.parallel_dv01_rub,
            cs01_rub=result.cs01_rub,
            ofz_spr01_rub=result.ofz_spr01_rub,
            model_version=result.model_version,
            projection_curve_basis=cast(str, self.projection_curve_basis),
            projection_buckets=tuple(projection_buckets),
        )

    def reprice(
        self,
        scenario: BondScenario,
        *,
        projection_curve_shifts_bp: tuple[float, ...] | None = None,
    ) -> BondScenarioResult:
        """Exact same-date P&L, isolated rate/spread effects and their cross residual.

        A two-curve floater's projection curve moves by ``projection_curve_shifts_bp``
        or, when None, by the scenario's curve shifts too: a rates scenario then moves
        every curve the position depends on, and ``rates_pnl_rub`` covers both.
        """
        shifts, spread = scenario.curve_shifts_bp, scenario.spread_shift_bp
        projection = self._scenario_projection(scenario, projection_curve_shifts_bp)
        quantity = self.request.quantity
        base = finite_money(self.base_price_rub * quantity, "base PV")
        shocked = finite_money(self._price(shifts, spread, projection) * quantity, "shocked PV")
        rates = finite_money(
            (self._price(shifts, 0.0, projection) - self.base_price_rub) * quantity, "rates PnL"
        )
        credit = finite_money(
            (self._price(_ZERO_SHOCK, spread) - self.base_price_rub) * quantity, "spread PnL"
        )
        total = finite_money(shocked - base, "total PnL")
        interaction = finite_money(total - rates - credit, "interaction PnL")
        return BondScenarioResult(
            base_id=self.base_id,
            scenario_id=canonical_sha256(
                scenario
                if projection_curve_shifts_bp is None
                else {"scenario": scenario, "projection_curve_shifts_bp": projection}
            ),
            curve_basis=self.curve_basis,
            spread_convention=self.request.spread_convention.identifier,
            base_pv_rub=base,
            shocked_pv_rub=shocked,
            rates_pnl_rub=rates,
            spread_pnl_rub=credit,
            interaction_pnl_rub=interaction,
            total_pnl_rub=total,
            model_version=self.model_version,
        )

    def _scenario_projection(
        self, scenario: BondScenario, projection: tuple[float, ...] | None
    ) -> tuple[float, ...]:
        if not self.two_curve:
            if projection is not None:
                raise ValueError("projection curve shifts need a separate projection curve")
            return _ZERO_SHOCK
        return scenario.curve_shifts_bp if projection is None else _shifts(tuple(projection))

    def second_order(self, *, bump_bp: float = 1.0) -> BondSecondOrderRisk:
        """Compute every rate/rate, rate/spread and spread/spread second derivative.

        Base calibration stays frozen. No sparsity is assumed. These central
        deltas deliberately differ from the default one-sided risk() result.
        Two-curve, the factors are 11 discount buckets, 11 projection buckets and
        the spread (23 x 23).
        """
        bump = positive_money(bump_bp, "second-order bump bp")
        count = len(RISK_BUCKETS)
        size = count + 1 + (count if self.two_curve else 0)

        def price(moves: list[float]) -> float:
            if self.two_curve:
                return self._price(tuple(moves[:count]), moves[-1], tuple(moves[count : 2 * count]))
            return self._price(tuple(moves[:-1]), moves[-1])

        gradient, hessian = _central_derivatives(
            price, size, bump, self.base_price_rub, self.request.quantity
        )
        rates = (
            (*(f"discount {b}" for b in RISK_BUCKETS), *(f"projection {b}" for b in RISK_BUCKETS))
            if self.two_curve
            else RISK_BUCKETS
        )
        return BondSecondOrderRisk(
            base_id=self.base_id,
            curve_basis=self.curve_basis,
            spread_convention=self.request.spread_convention.identifier,
            factors=(*rates, self.request.spread_kind + "_spread"),
            bump_bp=bump,
            gradient_rub_per_bp=gradient,
            hessian_rub_per_bp2=hessian,
            model_version=_SECOND_ORDER_MODELS[self.model_version],
        )

    def jump_to_default(
        self, *, outstanding_nominal_rub_per_bond: float, recovery_rate: float
    ) -> BondDefaultResult:
        """Signed quantity * (recovery * current principal - dirty unit PV).

        Recovery is an explicit fraction in [0, 1], paid immediately at settlement.
        No recovery of coupons/accrued, default delay or subordination inference.
        """
        nominal = positive_money(outstanding_nominal_rub_per_bond, "outstanding nominal")
        recovery = non_negative_money(recovery_rate, "recovery fraction")
        if recovery > 1:
            raise ValueError("recovery fraction must be between 0 and 1")
        quantity = self.request.quantity
        base = finite_money(self.base_price_rub * quantity, "base PV")
        recovered = finite_money(nominal * recovery * quantity, "recovery PV")
        return BondDefaultResult(
            base_id=self.base_id,
            default_id=canonical_sha256(
                {
                    "base_id": self.base_id,
                    "outstanding_nominal_rub_per_bond": nominal,
                    "recovery_rate": recovery,
                    "model": "bond-immediate-principal-recovery-v1",
                }
            ),
            outstanding_nominal_rub_per_bond=nominal,
            recovery_rate=recovery,
            base_pv_rub=base,
            recovery_pv_rub=recovered,
            jtd_pnl_rub=finite_money(recovered - base, "JTD PnL"),
        )

    def approximate(
        self,
        scenario: BondScenario,
        *,
        bump_bp: float = 1.0,
        projection_curve_shifts_bp: tuple[float, ...] | None = None,
    ) -> BondScenarioApproximation:
        """Central delta + 1/2 x'Hx, with exact repricing and a signed residual."""
        projection = self._scenario_projection(scenario, projection_curve_shifts_bp)
        risk = self.second_order(bump_bp=bump_bp)
        rate_moves = (
            (*scenario.curve_shifts_bp, *projection) if self.two_curve else scenario.curve_shifts_bp
        )
        moves = (*rate_moves, scenario.spread_shift_bp)
        n = len(rate_moves)
        hessian = risk.hessian_rub_per_bp2
        try:
            linear = finite_money(
                math.fsum(d * x for d, x in zip(risk.gradient_rub_per_bp, moves)), "linear PnL"
            )
            rates = finite_money(
                0.5
                * math.fsum(
                    moves[i] * hessian[i][j] * moves[j] for i in range(n) for j in range(n)
                ),
                "rates gamma PnL",
            )
            spread = finite_money(0.5 * moves[n] ** 2 * hessian[n][n], "spread gamma PnL")
            cross = finite_money(
                math.fsum(moves[i] * hessian[i][n] * moves[n] for i in range(n)),
                "rate/spread cross PnL",
            )
            approximate = finite_money(math.fsum((linear, rates, spread, cross)), "approximate PnL")
        except OverflowError as exc:
            raise ValueError("approximation is outside the supported numerical domain") from exc
        exact = self.reprice(scenario, projection_curve_shifts_bp=projection_curve_shifts_bp)
        return BondScenarioApproximation(
            base_id=self.base_id,
            scenario_id=exact.scenario_id,
            curve_basis=self.curve_basis,
            second_order_id=canonical_sha256(risk),
            linear_pnl_rub=linear,
            rates_gamma_pnl_rub=rates,
            spread_gamma_pnl_rub=spread,
            rates_spread_cross_pnl_rub=cross,
            approximate_pnl_rub=approximate,
            exact_pnl_rub=exact.total_pnl_rub,
            residual_pnl_rub=finite_money(
                exact.total_pnl_rub - approximate, "approximation residual"
            ),
        )


def _agreement_date(base: DiscountCurve, shocked: DiscountFactorCurve) -> date | None:
    """Last date up to which ``shocked`` reads the same discount factors as ``base``.

    Two node curves on the same date and convention interpolate a date from the nodes
    up to the first node on or after it, so they agree up to the node before the first
    one that differs. None when nothing is shared.
    """
    if (
        not isinstance(shocked, DiscountCurve)
        or base.flat_continuous_rate is not None
        or shocked.flat_continuous_rate is not None
        or shocked.curve_date != base.curve_date
        or shocked.convention != base.convention
        or len(shocked.nodes) != len(base.nodes)
    ):
        return None
    for position, (left, right) in enumerate(zip(base.nodes, shocked.nodes, strict=True)):
        if left != right:
            return None if position == 0 else base.nodes[position - 1][0]
    return date.max
