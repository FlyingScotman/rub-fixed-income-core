"""FX forwards and FX swaps between two foreign currencies (crosses such as USD/CNY), in RUB.

A cross leg receives ``N`` units of the base currency ``X`` on its value date ``T``
against ``N * K / q`` units of the quote currency ``Y`` (``K`` quoted as ``Y`` per ``q``
units of ``X``); ``N`` is signed, negative = deliver ``X``. Under RUB collateral each
currency amount is worth its covered-parity forward in RUB, so the leg is exactly two
X/RUB and Y/RUB forwards whose RUB amounts cancel (:func:`fx_cross_rub_legs`, any pivot
rate), and

``PV = N * F_X(T) * DF_rub(P) - (N * K / q) * F_Y(T) * DF_rub(P)``,

``F_C(T) = S_C * [DF_C(T) / DF_C(s_C)] / [DF_rub(T) / DF_rub(s_C)] * exp(b_C * tau(s_C, T))``

for each currency ``C`` on its own curve, spot (RUB per one unit, value date ``s_C``)
and basis: both legs are discounted on their own currency's curve, and no RUB amount
is left. The RUB curve still matters through ``DF_rub(s_C)`` and the payment date ``P``
(the value date for a deliverable leg; an explicit date for a leg cash-settled in RUB,
which pays ``N / q * (F_XY(T) - K) * F_Y(T)`` RUB with ``F_XY = q * F_X / F_Y``).

Risk: ``IR|<rub>|*``, ``IR|<base>|*``, ``IR|<quote>|*``, ``FXBASIS|X``, ``FXBASIS|Y``,
``FX|X``, ``FX|Y``: the FX delta is per currency against RUB, never a cross delta.
The engine composes two :class:`~rub_fixed_income.fx_forwards.FxForwardRiskEngine` on the
decomposition and reads only their foreign-leg values, so its PV does not depend on
the pivot. NDF fixings of crosses are not supported (a fixing would need both rates).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date
from typing import Literal

from ._curve_views import FxHorizon
from ._money import finite_money, positive_money
from ._quote_curves import QuoteCurve, foreign_identity
from ._risk_common import _ZERO_SHOCK, RISK_BUCKETS, ShockedCurveCache, _curve_identity, _shifts
from .canonical import canonical_sha256
from .conventions import DayCount
from .discount import CurveBootstrapRequest, DiscountCurve
from .errors import InvalidTerms
from .factors import FactorRisk, _upper, fx_basis_factor, fx_factor, ir_factor
from .fx_forwards import (
    FxForward,
    FxForwardBucketRisk,
    FxForwardLeg,
    FxForwardRiskEngine,
    FxForwardRiskRequest,
    FxSwap,
    _foreign_currency,
    _text,
)

__all__ = [
    "FX_CROSS_HORIZON_MODEL_VERSION",
    "FX_CROSS_RISK_MODEL_VERSION",
    "FX_CROSS_SECOND_ORDER_MODEL_VERSION",
    "FxCrossForward",
    "FxCrossHorizonValuation",
    "FxCrossLegValuation",
    "FxCrossPointsHeldRisk",
    "FxCrossRiskEngine",
    "FxCrossRiskRequest",
    "FxCrossRiskResult",
    "FxCrossScenario",
    "FxCrossScenarioResult",
    "FxCrossSecondOrderRisk",
    "FxCrossSwap",
    "fx_cross_factor_risk",
    "fx_cross_rub_legs",
]

FX_CROSS_RISK_MODEL_VERSION = "rub-fx-cross-parity-risk-v1"
FX_CROSS_SECOND_ORDER_MODEL_VERSION = "rub-fx-cross-parity-second-order-v1"
FX_CROSS_HORIZON_MODEL_VERSION = "rub-fx-cross-horizon-v1"


def _pair(base: object, quote: object) -> None:
    _foreign_currency(base)
    _foreign_currency(quote)
    if base == quote:
        raise InvalidTerms("a cross needs two different currencies")


def _cross_leg(leg: object) -> FxForwardLeg:
    if not isinstance(leg, FxForwardLeg):
        raise TypeError("cross legs must be FxForwardLeg values")
    if leg.fixing_date is not None:
        raise InvalidTerms("NDF fixings of a cross are not supported; pass a deliverable leg")
    return leg


@dataclass(frozen=True)
class FxCrossForward:
    """An outstanding cross forward: ``leg.notional_foreign`` is the signed base notional,
    ``leg.contract_rate`` the quote currency per ``quote_units`` base units.

    ``payment_date`` on the leg makes it cash-settled in RUB on that date (the module
    doc); otherwise both currencies are delivered on the value date.
    """

    trade_id: str
    base_currency: str
    quote_currency: str
    quote_units: float
    leg: FxForwardLeg

    def __post_init__(self) -> None:
        _text(self.trade_id, "trade_id")
        _pair(self.base_currency, self.quote_currency)
        _cross_leg(self.leg)
        object.__setattr__(self, "quote_units", positive_money(self.quote_units, "quote units"))

    @property
    def legs(self) -> tuple[FxForwardLeg, ...]:
        return (self.leg,)


@dataclass(frozen=True)
class FxCrossSwap:
    """Near and far cross legs of opposite base sign; only outstanding legs are valued.

    Once the near leg has settled, value :meth:`far_leg_forward`.
    """

    trade_id: str
    base_currency: str
    quote_currency: str
    quote_units: float
    near: FxForwardLeg
    far: FxForwardLeg

    def __post_init__(self) -> None:
        _text(self.trade_id, "trade_id")
        _pair(self.base_currency, self.quote_currency)
        _cross_leg(self.near)
        _cross_leg(self.far)
        if self.far.value_date <= self.near.value_date:
            raise InvalidTerms("the far leg must settle after the near leg")
        if (self.near.notional_foreign > 0.0) == (self.far.notional_foreign > 0.0):
            raise InvalidTerms("near and far legs of an FX swap must have opposite signs")
        object.__setattr__(self, "quote_units", positive_money(self.quote_units, "quote units"))

    @property
    def legs(self) -> tuple[FxForwardLeg, ...]:
        return (self.near, self.far)

    def far_leg_forward(self) -> FxCrossForward:
        return FxCrossForward(
            self.trade_id, self.base_currency, self.quote_currency, self.quote_units, self.far
        )


FxCross = FxCrossForward | FxCrossSwap


def _quote_notional(leg: FxForwardLeg, quote_units: float) -> float:
    return finite_money(-leg.notional_foreign * leg.contract_rate / quote_units, "quote notional")


def fx_cross_rub_legs(
    trade: FxCrossForward | FxCrossSwap, *, pivot_rate: float = 1.0
) -> tuple[FxForward | FxSwap, FxForward | FxSwap]:
    """The base/RUB and quote/RUB trades whose RUB amounts cancel (rates per one unit).

    The base leg exchanges ``N`` base units at ``pivot_rate`` RUB, the quote leg
    ``-N * K / q`` quote units at ``pivot_rate * N / (N * K / q)``: their RUB amounts are
    equal and opposite, so their sum is the cross for every positive pivot.
    """
    if not isinstance(trade, (FxCrossForward, FxCrossSwap)):
        raise TypeError("trade must be an FxCrossForward or an FxCrossSwap")
    pivot = positive_money(pivot_rate, "pivot rate")
    base_legs = []
    quote_legs = []
    for leg in trade.legs:
        quote = _quote_notional(leg, trade.quote_units)
        base_legs.append(
            FxForwardLeg(leg.value_date, leg.notional_foreign, pivot, leg.payment_date)
        )
        quote_legs.append(
            FxForwardLeg(
                leg.value_date,
                quote,
                positive_money(pivot * leg.notional_foreign / -quote, "quote pivot rate"),
                leg.payment_date,
            )
        )
    base_id = f"{trade.trade_id}/{trade.base_currency}"
    quote_id = f"{trade.trade_id}/{trade.quote_currency}"
    if isinstance(trade, FxCrossSwap):
        return (
            FxSwap(base_id, trade.base_currency, 1.0, base_legs[0], base_legs[1]),
            FxSwap(quote_id, trade.quote_currency, 1.0, quote_legs[0], quote_legs[1]),
        )
    return (
        FxForward(base_id, trade.base_currency, 1.0, base_legs[0]),
        FxForward(quote_id, trade.quote_currency, 1.0, quote_legs[0]),
    )


@dataclass(frozen=True)
class FxCrossRiskRequest:
    """Inputs of one outstanding cross; spots are RUB per **one** unit of each currency.

    Each currency has its own curve, spot (with its value date) and basis, as in an
    :class:`~rub_fixed_income.fx_forwards.FxForwardRiskRequest`; the three curves share
    one valuation date. A foreign curve may be XCCY par quotes, as for a forward.
    """

    instrument: FxCrossForward | FxCrossSwap
    base_spot_rate: float
    base_spot_value_date: date
    quote_spot_rate: float
    quote_spot_value_date: date
    rub_curve: DiscountCurve | CurveBootstrapRequest
    base_curve: DiscountCurve | CurveBootstrapRequest | QuoteCurve
    quote_curve: DiscountCurve | CurveBootstrapRequest | QuoteCurve
    base_basis_bp: float
    quote_basis_bp: float
    base_basis_day_count: DayCount = DayCount.ACT_365_FIXED
    quote_basis_day_count: DayCount = DayCount.ACT_365_FIXED

    def __post_init__(self) -> None:
        if not isinstance(self.instrument, (FxCrossForward, FxCrossSwap)):
            raise TypeError("instrument must be an FxCrossForward or an FxCrossSwap")
        object.__setattr__(
            self, "base_spot_rate", positive_money(self.base_spot_rate, "base spot rate")
        )
        object.__setattr__(
            self, "quote_spot_rate", positive_money(self.quote_spot_rate, "quote spot rate")
        )
        object.__setattr__(self, "base_basis_bp", finite_money(self.base_basis_bp, "base basis"))
        object.__setattr__(self, "quote_basis_bp", finite_money(self.quote_basis_bp, "quote basis"))


@dataclass(frozen=True)
class FxCrossLegValuation:
    """One cross leg: ``forward_rate`` is the cross outright ``q * F_X(T) / F_Y(T)``."""

    value_date: date
    payment_date: date
    base_notional: float
    quote_notional: float
    contract_rate: float
    base_forward_rate: float
    quote_forward_rate: float
    forward_rate: float
    forward_points: float
    rub_discount_factor: float
    pv_rub: float


@dataclass(frozen=True)
class FxCrossRiskResult:
    """Signed RUB PV sensitivities of a cross (see :class:`FxForwardRiskResult`).

    ``fx_delta_base`` / ``fx_delta_quote`` are the spot-equivalent amounts of each
    currency for its spot value date (the quote one is negative for a bought base);
    ``*_rub_per_pct`` the exact PV change for +1 % of that currency against RUB.
    ``cross_spot_rate`` is ``q * S_X / S_Y``.
    """

    base_id: str
    trade_id: str
    base_currency: str
    quote_currency: str
    difference: str
    bump_bp: float
    cross_spot_rate: float
    pv_rub: float
    legs: tuple[FxCrossLegValuation, ...]
    fx_delta_base: float
    fx_delta_quote: float
    fx_delta_base_rub_per_pct: float
    fx_delta_quote_rub_per_pct: float
    rub_buckets: tuple[FxForwardBucketRisk, ...]
    rub_dv01_rub: float
    rub_parallel_dv01_rub: float
    base_buckets: tuple[FxForwardBucketRisk, ...]
    base_dv01_rub: float
    base_parallel_dv01_rub: float
    quote_buckets: tuple[FxForwardBucketRisk, ...]
    quote_dv01_rub: float
    quote_parallel_dv01_rub: float
    base_basis01_rub: float
    quote_basis01_rub: float
    model_version: str = FX_CROSS_RISK_MODEL_VERSION


@dataclass(frozen=True)
class FxCrossScenario:
    """Bucket shifts (bp) of the RUB, base and quote curves, basis shifts (bp) and
    relative spot moves (percent) of each currency against RUB."""

    rub_curve_shifts_bp: tuple[float, ...] = _ZERO_SHOCK
    base_curve_shifts_bp: tuple[float, ...] = _ZERO_SHOCK
    quote_curve_shifts_bp: tuple[float, ...] = _ZERO_SHOCK
    base_basis_shift_bp: float = 0.0
    quote_basis_shift_bp: float = 0.0
    base_spot_shift_pct: float = 0.0
    quote_spot_shift_pct: float = 0.0

    def __post_init__(self) -> None:
        for name in ("rub_curve_shifts_bp", "base_curve_shifts_bp", "quote_curve_shifts_bp"):
            object.__setattr__(self, name, _shifts(tuple(getattr(self, name))))
        for name in ("base_basis_shift_bp", "quote_basis_shift_bp"):
            object.__setattr__(self, name, finite_money(getattr(self, name), name))
        for name in ("base_spot_shift_pct", "quote_spot_shift_pct"):
            move = finite_money(getattr(self, name), name)
            if move <= -100.0:
                raise ValueError("FX spot shift must stay above -100 percent")
            object.__setattr__(self, name, move)


@dataclass(frozen=True)
class FxCrossScenarioResult:
    base_id: str
    scenario_id: str
    base_pv_rub: float
    shocked_pv_rub: float
    rub_rates_pnl_rub: float
    base_rates_pnl_rub: float
    quote_rates_pnl_rub: float
    basis_pnl_rub: float
    fx_spot_pnl_rub: float
    interaction_pnl_rub: float
    total_pnl_rub: float
    model_version: str = FX_CROSS_RISK_MODEL_VERSION


@dataclass(frozen=True)
class FxCrossSecondOrderRisk:
    """Gradient and full Hessian over 11 RUB, 11 base, 11 quote buckets, both bases (bp)
    and both spots (1 % each, exact: PV is linear in each spot)."""

    base_id: str
    factors: tuple[str, ...]
    bump_bp: float
    gradient_rub: tuple[float, ...]
    hessian_rub: tuple[tuple[float, ...], ...]
    model_version: str = FX_CROSS_SECOND_ORDER_MODEL_VERSION


@dataclass(frozen=True)
class FxCrossPointsHeldRisk:
    """RUB curve risk of a cross with both currencies' swap points held (BACKLOG #46)."""

    base_id: str
    difference: str
    bump_bp: float
    rub_buckets: tuple[FxForwardBucketRisk, ...]
    rub_dv01_rub: float
    rub_parallel_dv01_rub: float
    model_version: str = FX_CROSS_RISK_MODEL_VERSION


@dataclass(frozen=True)
class FxCrossHorizonValuation:
    """Carry, rolldown and theta of a cross: the sum of its two RUB-leg trades'
    :class:`~rub_fixed_income.fx_forwards.FxForwardHorizonValuation` (their RUB amounts
    cancel), each currency with its own horizon spot rule."""

    base_id: str
    trade_id: str
    base_currency: str
    quote_currency: str
    valuation_date: date
    horizon_date: date
    fx_horizon: FxHorizon
    base_horizon_spot_rate: float
    base_horizon_spot_value_date: date
    quote_horizon_spot_rate: float
    quote_horizon_spot_value_date: date
    pv_start_rub: float
    pv_forward_rub: float
    pv_rolled_rub: float
    received_rub: float
    carry_rub: float
    rolldown_rub: float
    horizon_pnl_rub: float
    model_version: str = FX_CROSS_HORIZON_MODEL_VERSION


# rub, base, quote shifts; base basis, quote basis; base spot, quote spot
_Moves = tuple[tuple[float, ...], tuple[float, ...], tuple[float, ...], float, float, float, float]
_NO_MOVE: _Moves = (_ZERO_SHOCK, _ZERO_SHOCK, _ZERO_SHOCK, 0.0, 0.0, 0.0, 0.0)


@dataclass(frozen=True)
class FxCrossRiskEngine:
    """Value an outstanding cross on two foreign curves and the RUB curve, bases held.

    ``PV = A_X + A_Y``: the RUB value of the base and quote currency amounts, each from
    its own :class:`FxForwardRiskEngine` on :func:`fx_cross_rub_legs` (whose cancelling
    RUB amounts are never added). Share one ``curve_cache`` across engines.
    """

    request: FxCrossRiskRequest
    curve_cache: ShockedCurveCache = field(
        default_factory=ShockedCurveCache, compare=False, repr=False
    )
    base_pv_rub: float = field(init=False)
    base_id: str = field(init=False)
    _base: FxForwardRiskEngine = field(init=False, repr=False)
    _quote: FxForwardRiskEngine = field(init=False, repr=False)
    _base_values: tuple[float, float] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        req = self.request
        if not isinstance(self.curve_cache, ShockedCurveCache):
            raise TypeError("curve_cache must be a ShockedCurveCache")
        base_trade, quote_trade = fx_cross_rub_legs(req.instrument)
        base = FxForwardRiskEngine(
            FxForwardRiskRequest(
                base_trade,
                req.base_spot_rate,
                req.base_spot_value_date,
                req.rub_curve,
                req.base_curve,
                req.base_basis_bp,
                req.base_basis_day_count,
            ),
            self.curve_cache,
        )
        quote = FxForwardRiskEngine(
            FxForwardRiskRequest(
                quote_trade,
                req.quote_spot_rate,
                req.quote_spot_value_date,
                req.rub_curve,
                req.quote_curve,
                req.quote_basis_bp,
                req.quote_basis_day_count,
            ),
            self.curve_cache,
        )
        object.__setattr__(self, "_base", base)
        object.__setattr__(self, "_quote", quote)
        values = self._values(_NO_MOVE)
        object.__setattr__(self, "_base_values", values)
        object.__setattr__(self, "base_pv_rub", finite_money(values[0] + values[1], "PV"))
        object.__setattr__(
            self,
            "base_id",
            canonical_sha256(
                {
                    "model": FX_CROSS_RISK_MODEL_VERSION,
                    "instrument_type": type(req.instrument).__name__,
                    "instrument": req.instrument,
                    "base_spot_rate": req.base_spot_rate,
                    "base_spot_value_date": req.base_spot_value_date,
                    "quote_spot_rate": req.quote_spot_rate,
                    "quote_spot_value_date": req.quote_spot_value_date,
                    "rub_curve_basis": base.rub_curve_basis,
                    "rub_curve": _curve_identity(base.request.rub_curve),
                    "base_curve_basis": base.foreign_curve_basis,
                    "base_curve": foreign_identity(base.request.foreign_curve),
                    "quote_curve_basis": quote.foreign_curve_basis,
                    "quote_curve": foreign_identity(quote.request.foreign_curve),
                    "base_basis_bp": req.base_basis_bp,
                    "base_basis_day_count": req.base_basis_day_count.value,
                    "quote_basis_bp": req.quote_basis_bp,
                    "quote_basis_day_count": req.quote_basis_day_count.value,
                }
            ),
        )

    @property
    def rub_curve_basis(self) -> str:
        return self._base.rub_curve_basis

    @property
    def base_curve_basis(self) -> str:
        return self._base.foreign_curve_basis

    @property
    def quote_curve_basis(self) -> str:
        return self._quote.foreign_curve_basis

    def _values(self, moves: _Moves) -> tuple[float, float]:
        """(base amounts' RUB PV, quote amounts' RUB PV) under the moves."""
        rub, base, quote, base_basis, quote_basis, base_spot, quote_spot = moves
        return (
            self._base._values((rub, base, base_basis, base_spot))[0],
            self._quote._values((rub, quote, quote_basis, quote_spot))[0],
        )

    def _pv(self, moves: _Moves) -> float:
        base, quote = self._values(moves)
        return base + quote

    def leg_valuations(self) -> tuple[FxCrossLegValuation, ...]:
        trade = self.request.instrument
        rows = []
        cross_spot = trade.quote_units * self.request.base_spot_rate / self.request.quote_spot_rate
        base_legs = self._base.leg_valuations()
        quote_legs = self._quote.leg_valuations()
        for leg, b, q in zip(trade.legs, base_legs, quote_legs, strict=True):
            forward = trade.quote_units * b.forward_rate / q.forward_rate
            df = b.rub_discount_factor
            pv = (b.notional_foreign * b.forward_rate + q.notional_foreign * q.forward_rate) * df
            rows.append(
                FxCrossLegValuation(
                    value_date=leg.value_date,
                    payment_date=leg.settlement_date,
                    base_notional=leg.notional_foreign,
                    quote_notional=q.notional_foreign,
                    contract_rate=leg.contract_rate,
                    base_forward_rate=b.forward_rate,
                    quote_forward_rate=q.forward_rate,
                    forward_rate=forward,
                    forward_points=forward - cross_spot,
                    rub_discount_factor=df,
                    pv_rub=finite_money(pv, "leg PV"),
                )
            )
        return tuple(rows)

    def risk(
        self, *, bump_bp: float = 1.0, difference: Literal["forward", "central"] = "forward"
    ) -> FxCrossRiskResult:
        """Signed RUB per bucket of each curve, parallel per curve, both bases and spots."""
        bump = positive_money(bump_bp, "risk bump bp")
        if difference not in ("forward", "central"):
            raise ValueError("difference must be forward or central")
        size = len(RISK_BUCKETS)

        def sensitivity(moves: _Moves) -> float:
            up = self._pv(moves)
            if difference == "central":
                rub, base, quote, bb, qb, bs, qs = moves
                down = self._pv(
                    (
                        tuple(-s for s in rub),
                        tuple(-s for s in base),
                        tuple(-s for s in quote),
                        -bb,
                        -qb,
                        bs,
                        qs,
                    )
                )
                return finite_money((up - down) / (2.0 * bump), "sensitivity")
            return finite_money((up - self.base_pv_rub) / bump, "sensitivity")

        def curve_moves(index: int, shifts: tuple[float, ...]) -> _Moves:
            curves = [_ZERO_SHOCK, _ZERO_SHOCK, _ZERO_SHOCK]
            curves[index] = shifts
            return (curves[0], curves[1], curves[2], 0.0, 0.0, 0.0, 0.0)

        def buckets(index: int) -> tuple[FxForwardBucketRisk, ...]:
            return tuple(
                FxForwardBucketRisk(
                    bucket,
                    sensitivity(
                        curve_moves(index, tuple(bump if i == j else 0.0 for j in range(size)))
                    ),
                )
                for i, bucket in enumerate(RISK_BUCKETS)
            )

        def total(rows: tuple[FxForwardBucketRisk, ...], label: str) -> float:
            return finite_money(math.fsum(b.dv01_rub for b in rows), label)

        req = self.request
        parallel = (bump,) * size
        rub_rows, base_rows, quote_rows = buckets(0), buckets(1), buckets(2)
        base_pv, quote_pv = self._base_values
        rub = self._base._rub_curve
        trade = req.instrument
        return FxCrossRiskResult(
            base_id=self.base_id,
            trade_id=trade.trade_id,
            base_currency=trade.base_currency,
            quote_currency=trade.quote_currency,
            difference=difference,
            bump_bp=bump,
            cross_spot_rate=trade.quote_units * req.base_spot_rate / req.quote_spot_rate,
            pv_rub=self.base_pv_rub,
            legs=self.leg_valuations(),
            fx_delta_base=finite_money(
                base_pv / (req.base_spot_rate * rub.discount_factor(req.base_spot_value_date)),
                "base FX delta",
            ),
            fx_delta_quote=finite_money(
                quote_pv / (req.quote_spot_rate * rub.discount_factor(req.quote_spot_value_date)),
                "quote FX delta",
            ),
            fx_delta_base_rub_per_pct=finite_money(base_pv / 100.0, "base FX delta RUB"),
            fx_delta_quote_rub_per_pct=finite_money(quote_pv / 100.0, "quote FX delta RUB"),
            rub_buckets=rub_rows,
            rub_dv01_rub=total(rub_rows, "RUB DV01"),
            rub_parallel_dv01_rub=sensitivity(curve_moves(0, parallel)),
            base_buckets=base_rows,
            base_dv01_rub=total(base_rows, "base DV01"),
            base_parallel_dv01_rub=sensitivity(curve_moves(1, parallel)),
            quote_buckets=quote_rows,
            quote_dv01_rub=total(quote_rows, "quote DV01"),
            quote_parallel_dv01_rub=sensitivity(curve_moves(2, parallel)),
            base_basis01_rub=sensitivity(
                (_ZERO_SHOCK, _ZERO_SHOCK, _ZERO_SHOCK, bump, 0.0, 0.0, 0.0)
            ),
            quote_basis01_rub=sensitivity(
                (_ZERO_SHOCK, _ZERO_SHOCK, _ZERO_SHOCK, 0.0, bump, 0.0, 0.0)
            ),
        )

    def points_held_rub_risk(
        self, *, bump_bp: float = 1.0, difference: Literal["forward", "central"] = "central"
    ) -> FxCrossPointsHeldRisk:
        """RUB risk with both currencies' outrights held: the sum over the two RUB-leg
        trades (their cancelling RUB amounts carry equal and opposite risk)."""
        base = self._base.points_held_rub_risk(bump_bp=bump_bp, difference=difference)
        quote = self._quote.points_held_rub_risk(bump_bp=bump_bp, difference=difference)
        rows = tuple(
            FxForwardBucketRisk(b.bucket, finite_money(b.dv01_rub + q.dv01_rub, "DV01"))
            for b, q in zip(base.rub_buckets, quote.rub_buckets, strict=True)
        )
        return FxCrossPointsHeldRisk(
            base_id=self.base_id,
            difference=base.difference,
            bump_bp=base.bump_bp,
            rub_buckets=rows,
            rub_dv01_rub=finite_money(math.fsum(b.dv01_rub for b in rows), "RUB DV01"),
            rub_parallel_dv01_rub=finite_money(
                base.rub_parallel_dv01_rub + quote.rub_parallel_dv01_rub, "RUB parallel DV01"
            ),
        )

    def reprice(self, scenario: FxCrossScenario) -> FxCrossScenarioResult:
        """Exact PV change, each factor group isolated, and their interaction."""
        if not isinstance(scenario, FxCrossScenario):
            raise TypeError("scenario must be an FxCrossScenario")
        rub, base, quote = (
            scenario.rub_curve_shifts_bp,
            scenario.base_curve_shifts_bp,
            scenario.quote_curve_shifts_bp,
        )
        bb, qb = scenario.base_basis_shift_bp, scenario.quote_basis_shift_bp
        bs, qs = scenario.base_spot_shift_pct, scenario.quote_spot_shift_pct
        z = _ZERO_SHOCK
        shocked = finite_money(self._pv((rub, base, quote, bb, qb, bs, qs)), "shocked PV")

        def pnl(moves: _Moves, label: str) -> float:
            return finite_money(self._pv(moves) - self.base_pv_rub, label)

        parts = (
            pnl((rub, z, z, 0.0, 0.0, 0.0, 0.0), "RUB rates PnL"),
            pnl((z, base, z, 0.0, 0.0, 0.0, 0.0), "base rates PnL"),
            pnl((z, z, quote, 0.0, 0.0, 0.0, 0.0), "quote rates PnL"),
            pnl((z, z, z, bb, qb, 0.0, 0.0), "basis PnL"),
            pnl((z, z, z, 0.0, 0.0, bs, qs), "FX spot PnL"),
        )
        total = finite_money(shocked - self.base_pv_rub, "total PnL")
        return FxCrossScenarioResult(
            base_id=self.base_id,
            scenario_id=canonical_sha256(scenario),
            base_pv_rub=self.base_pv_rub,
            shocked_pv_rub=shocked,
            rub_rates_pnl_rub=parts[0],
            base_rates_pnl_rub=parts[1],
            quote_rates_pnl_rub=parts[2],
            basis_pnl_rub=parts[3],
            fx_spot_pnl_rub=parts[4],
            interaction_pnl_rub=finite_money(total - math.fsum(parts), "interaction PnL"),
            total_pnl_rub=total,
        )

    def second_order(self, *, bump_bp: float = 1.0) -> FxCrossSecondOrderRisk:
        """Central gradient and full Hessian over 35 rate/basis factors plus both spots."""
        bump = positive_money(bump_bp, "second-order bump bp")
        size = len(RISK_BUCKETS)
        rates = 3 * size + 2
        base_spot, quote_spot = rates, rates + 1

        def values(vector: list[float]) -> tuple[float, float]:
            return self._values(
                (
                    tuple(vector[:size]),
                    tuple(vector[size : 2 * size]),
                    tuple(vector[2 * size : 3 * size]),
                    vector[3 * size],
                    vector[3 * size + 1],
                    0.0,
                    0.0,
                )
            )

        square = positive_money(bump * bump, "squared second-order bump")
        positive_money(4 * square, "mixed-difference denominator")
        base = self._base_values
        base_pv = self.base_pv_rub
        gradient = [0.0] * rates
        hessian = [[0.0] * (rates + 2) for _ in range(rates + 2)]
        active = []
        for i in range(rates):
            vector = [0.0] * rates
            vector[i] = bump
            up = values(vector)
            vector[i] = -bump
            down = values(vector)
            if up == base and down == base:
                continue
            active.append(i)
            up_pv, down_pv = up[0] + up[1], down[0] + down[1]
            gradient[i] = finite_money((up_pv - down_pv) / (2 * bump), "gradient")
            hessian[i][i] = finite_money(
                ((up_pv - base_pv) + (down_pv - base_pv)) / square, "diagonal gamma"
            )
            # PV(S_X(1+x), S_Y(1+y)) = A_X(1+x) + A_Y(1+y): spot terms exact, central in rates.
            for spot, k in ((base_spot, 0), (quote_spot, 1)):
                cross = finite_money((up[k] - down[k]) / (2 * bump) / 100.0, "FX cross gamma")
                hessian[i][spot] = hessian[spot][i] = cross
        for position, i in enumerate(active):
            for j in active[:position]:
                corners = []
                for sign_i, sign_j in ((1, 1), (1, -1), (-1, 1), (-1, -1)):
                    pair = [0.0] * rates
                    pair[i], pair[j] = sign_i * bump, sign_j * bump
                    a, b = values(pair)
                    corners.append(a + b)
                hessian[i][j] = hessian[j][i] = finite_money(
                    ((corners[0] - corners[1]) - (corners[2] - corners[3])) / (4 * square),
                    "cross gamma",
                )
        trade = self.request.instrument
        x, y = trade.base_currency, trade.quote_currency
        return FxCrossSecondOrderRisk(
            base_id=self.base_id,
            factors=(
                *(f"RUB {b}" for b in RISK_BUCKETS),
                *(f"{x} {b}" for b in RISK_BUCKETS),
                *(f"{y} {b}" for b in RISK_BUCKETS),
                f"{x} basis",
                f"{y} basis",
                f"{x} spot",
                f"{y} spot",
            ),
            bump_bp=bump,
            gradient_rub=(
                *gradient,
                finite_money(base[0] / 100.0, "base FX delta RUB"),
                finite_money(base[1] / 100.0, "quote FX delta RUB"),
            ),
            hessian_rub=tuple(tuple(row) for row in hessian),
        )

    def horizon(
        self,
        horizon_date: date,
        *,
        base_horizon_spot_value_date: date,
        quote_horizon_spot_value_date: date,
        fx_horizon: FxHorizon = "spot_held",
    ) -> FxCrossHorizonValuation:
        """Carry, rolldown and theta to ``horizon_date``; each currency's horizon spot date
        is its own spot lag from the horizon (the caller applies it)."""
        base = self._base.horizon(
            horizon_date,
            horizon_spot_value_date=base_horizon_spot_value_date,
            fx_horizon=fx_horizon,
        )
        quote = self._quote.horizon(
            horizon_date,
            horizon_spot_value_date=quote_horizon_spot_value_date,
            fx_horizon=fx_horizon,
        )
        start = self.base_pv_rub
        pv_forward = finite_money(base.pv_forward_rub + quote.pv_forward_rub, "forward PV")
        pv_rolled = finite_money(base.pv_rolled_rub + quote.pv_rolled_rub, "rolled PV")
        received = finite_money(base.received_rub + quote.received_rub, "received")
        trade = self.request.instrument
        return FxCrossHorizonValuation(
            base_id=self.base_id,
            trade_id=trade.trade_id,
            base_currency=trade.base_currency,
            quote_currency=trade.quote_currency,
            valuation_date=base.valuation_date,
            horizon_date=horizon_date,
            fx_horizon=base.fx_horizon,
            base_horizon_spot_rate=base.horizon_spot_rate,
            base_horizon_spot_value_date=base.horizon_spot_value_date,
            quote_horizon_spot_rate=quote.horizon_spot_rate,
            quote_horizon_spot_value_date=quote.horizon_spot_value_date,
            pv_start_rub=start,
            pv_forward_rub=pv_forward,
            pv_rolled_rub=pv_rolled,
            received_rub=received,
            carry_rub=finite_money(pv_forward + received - start, "carry"),
            rolldown_rub=finite_money(pv_rolled - pv_forward, "rolldown"),
            horizon_pnl_rub=finite_money(pv_rolled + received - start, "horizon P&L"),
        )


def fx_cross_factor_risk(
    risk: FxCrossRiskResult,
    rub_curve_id: str,
    base_curve_id: str,
    quote_curve_id: str,
    second_order: FxCrossSecondOrderRisk | None = None,
) -> FactorRisk:
    """Map cross risk to ``IR|rub|*``, ``IR|base|*``, ``IR|quote|*``, ``FXBASIS|X``,
    ``FXBASIS|Y``, ``FX|X``, ``FX|Y``. Otherwise as ``fx_forward_factor_risk``."""
    if len({rub_curve_id, base_curve_id, quote_curve_id}) != 3:
        raise InvalidTerms("the RUB, base and quote curves need distinct curve IDs")
    x, y = risk.base_currency, risk.quote_currency
    factors = (
        *(ir_factor(rub_curve_id, b) for b in RISK_BUCKETS),
        *(ir_factor(base_curve_id, b) for b in RISK_BUCKETS),
        *(ir_factor(quote_curve_id, b) for b in RISK_BUCKETS),
        fx_basis_factor(x),
        fx_basis_factor(y),
        fx_factor(x),
        fx_factor(y),
    )
    if second_order is not None:
        if second_order.base_id != risk.base_id:
            raise InvalidTerms("risk and second-order results describe different positions")
        if len(second_order.factors) != len(factors):
            raise InvalidTerms("unexpected second-order factor set")
        return FactorRisk(
            factors,
            second_order.gradient_rub,
            _upper(second_order.hessian_rub),
            "central",
            second_order.bump_bp,
            True,
        )
    if risk.difference != "central":
        raise InvalidTerms("desk factors need central-difference risk (DEC08)")
    first = (
        *(b.dv01_rub for b in risk.rub_buckets),
        *(b.dv01_rub for b in risk.base_buckets),
        *(b.dv01_rub for b in risk.quote_buckets),
        risk.base_basis01_rub,
        risk.quote_basis01_rub,
        risk.fx_delta_base_rub_per_pct,
        risk.fx_delta_quote_rub_per_pct,
    )
    return FactorRisk(factors, first, (), "central", risk.bump_bp, False)
