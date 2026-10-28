"""Horizon valuation: carry, rolldown and P&L explain (roadmap 0.8).

A position valued at settlement ``S0`` of business date ``d0`` is carried to the
settlement ``S1`` of a later business date ``d1`` with its Z-spread held constant:

- ``pv_forward``: the remaining flows valued at ``S1`` on today's curve, i.e. today's
  forwards are realised (the curve read forward from ``S1``);
- ``pv_rolled``: the remaining flows valued at ``S1`` on the **rolled** curve, which keeps
  today's shape in calendar days: ``DF_rolled(d1 + k) = DF_today(d0 + k)``;
- flows paid in ``(S0, S1]`` are received as cash (coupons and principal, not reinvested).

Then ``carry = pv_forward + received - pv_start`` and ``rolldown = pv_rolled - pv_forward``:
carry is the return at today's forwards plus spread, rolldown the gain from the curve
keeping its shape. For a zero-coupon bond on a flat curve with a continuous spread,
carry is ``pv_start * (exp((r + s) * h) - 1)`` and rolldown is zero.

:func:`explain_horizon_pnl` adds the market move to an end curve and spread:
``rates = V(end curve, s0) - V(rolled curve, s0)``,
``spread = V(rolled curve, s1) - V(rolled curve, s0)`` and ``residual`` the cross
term, so ``carry + rolldown + rates + spread + residual`` equals the full revaluation
change ``pv_end + received - pv_start`` exactly.

A RUB floater is carried the same way. Its coupons are projected off today's
projection curve for ``pv_start``, ``pv_forward`` and the coupons received (today's
forwards realised). For ``pv_rolled`` the observations before ``d1`` keep today's
forwards (the path to the horizon) and later ones are projected off the rolled
projection curve; the discount curve rolls as for a fixed bond. Accrued interest of a
floater is left to the caller here, so its accrued fields are None
(``Instrument.accrued(settlement, market)`` computes it since 0.33.0). P&L explain of
a floater (0.33.0) takes the fixings at both dates: at the end market its coupons are
re-projected with the fixings realised over the horizon.

A never-called perpetual (0.41.0, BACKLOG #126) is carried with its tail
(``perpetual_tail``, :class:`~rub_fixed_income.perpetual.PerpetualTail`): every value
adds the tail coupons after its settlement, priced exactly as ``value_portfolio`` prices
them (the curve read from that curve's date, flat-forward past 30 years, the sum to 600
years); tail coupons paid by the horizon settlement are received. Unless the horizon
policy picks a put, which it is then valued to, as in ``value_portfolio``.

Amounts are RUB per bond; :func:`horizon_portfolio` scales by position quantity.

Derivative engines carry to a horizon on the same two curve views (BACKLOG #101):
:func:`forward_curve` (today's curve read forward, ``DF(t) / DF(horizon)``) and
:func:`roll_curve` (shape held). ``horizon_pnl`` on rolled curves is the theta. An FX
product also needs the spot at the horizon, a stated :data:`FxHorizon` rule:
``"spot_held"`` keeps today's spot (sticky spot: forward points roll down) and
``"forwards_realised"`` moves it to today's forward for the horizon's spot value date.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import date
from typing import Literal

from ._curve_views import (
    FX_HORIZONS,
    FxHorizon,
    forward_curve,
    fx_horizon,
    roll_curve,
    roll_key_rate_curve,
)
from ._money import finite_money, positive_money
from ._risk_common import ShockedCurveCache, _base_curve
from .compatibility import price_cashflows, solve_z_spread
from .conventions import ZSpreadConvention
from .dates import BusinessCalendar
from .discount import CurveBootstrapRequest, DiscountCurve
from .domain import FloatingCouponLeg, Instrument, Offer, OfferKind, ProductType
from .errors import InvalidTerms, UnsupportedProduct
from .floating import FixingHistory, FloatingLeg
from .perpetual import PerpetualTail

__all__ = [
    "FX_HORIZONS",
    "HORIZON_MODEL_VERSION",
    "FxHorizon",
    "HorizonPnlExplain",
    "HorizonValuation",
    "explain_horizon_pnl",
    "forward_curve",
    "fx_horizon",
    "horizon_valuation",
    "roll_curve",
    "roll_key_rate_curve",
]

HORIZON_MODEL_VERSION = "rub-horizon-carry-v1"
HorizonPolicy = Literal["maturity", "confirmed_put", "open_put"]
# What a bond was valued to: a horizon policy, or a never-called perpetual's tail (0.41.0).
HorizonLabel = Literal["maturity", "confirmed_put", "open_put", "perpetual"]


@dataclass(frozen=True)
class HorizonValuation:
    """Carry and rolldown of one bond from ``settlement_date`` to the horizon settlement.

    A never-called perpetual reads ``redemption_horizon="perpetual"`` and
    ``redemption_date`` its tail's anchor (the last listed coupon), as in
    ``value_portfolio``; its received coupons include the tail coupons paid by the
    horizon settlement.
    """

    instrument_id: str
    start_date: date
    horizon_date: date
    settlement_date: date
    horizon_settlement_date: date
    redemption_horizon: HorizonLabel
    redemption_date: date
    spread_bp: float
    spread_convention: str
    pv_start_rub: float
    pv_forward_rub: float
    pv_rolled_rub: float
    coupons_received_rub: float
    principal_received_rub: float
    accrued_start_rub: float | None
    accrued_end_rub: float | None
    nominal_start_rub: float
    nominal_end_rub: float
    carry_rub: float
    rolldown_rub: float
    horizon_pnl_rub: float
    model_version: str = HORIZON_MODEL_VERSION

    @property
    def received_rub(self) -> float:
        return self.coupons_received_rub + self.principal_received_rub


@dataclass(frozen=True)
class HorizonPnlExplain:
    """Full revaluation change from start to the end market, split into its drivers."""

    horizon: HorizonValuation
    end_spread_bp: float
    pv_end_rub: float
    carry_rub: float
    rolldown_rub: float
    rates_rub: float
    spread_rub: float
    residual_rub: float
    total_rub: float
    model_version: str = field(default=HORIZON_MODEL_VERSION)


@dataclass(frozen=True)
class _Setup:
    instrument: Instrument
    settlement: date
    horizon_settlement: date
    redemption_date: date
    policy: HorizonLabel
    remaining: tuple[tuple[date, float], ...]
    received_coupons: float
    received_principal: float
    pv_flows: tuple[tuple[date, float], ...]
    # Floaters: the start projection curve and the remaining flows re-projected off a
    # rolled projection curve (observations before the horizon at today's forwards).
    projection: DiscountCurve | None = None
    reproject: Callable[[DiscountCurve], tuple[tuple[date, float], ...]] | None = None
    # A never-called perpetual: the coupons after its listed ones, forever.
    tail: PerpetualTail | None = None
    # The put valued to, if any.
    offer: Offer | None = None


@dataclass(frozen=True)
class _FloatingInputs:
    history: FixingHistory
    projection: DiscountCurve
    strike_guard_bp: float | None


def _setup(
    instrument: Instrument,
    calendar: BusinessCalendar,
    start_date: date,
    horizon_date: date,
    policy: HorizonPolicy,
    floating: _FloatingInputs | None = None,
    tail: PerpetualTail | None = None,
) -> _Setup:
    floater = instrument.product is ProductType.FLOATING_BOND
    if not (instrument.product is ProductType.FIXED_BOND or (floater and floating is not None)):
        raise UnsupportedProduct(
            f"{instrument.product.value} horizon valuation is not supported"
            + (" without its fixing history (fixings=)" if floater else "")
        )
    if tail is not None and not isinstance(tail, PerpetualTail):
        raise TypeError("perpetual_tail must be a PerpetualTail")
    if instrument.is_perpetual and tail is None:
        raise UnsupportedProduct(
            "perpetual bonds need a call assumption or a never-called tail (perpetual_tail=)"
        )
    if tail is not None and not instrument.is_perpetual:
        raise InvalidTerms("a perpetual tail continues a perpetual bond only")
    if horizon_date <= start_date:
        raise InvalidTerms("horizon date must be after the start date")
    settlement = calendar.add_business_days(start_date, instrument.settlement_lag_days)
    horizon_settlement = calendar.add_business_days(horizon_date, instrument.settlement_lag_days)
    label: HorizonLabel
    if tail is None:
        redemption_date, offer = instrument.horizon(policy, settlement)
        label = "maturity" if offer is None else policy
    else:
        offer = _perpetual_put(instrument, policy, settlement)
        if offer is None:
            redemption_date, label = tail.anchor_date, "perpetual"
        else:  # valued to the put the policy picks, as in value_portfolio
            redemption_date, label, tail = offer.offer_date, policy, None
    flows = instrument.cashflows(after=settlement, horizon=redemption_date, offer=offer)
    if not flows and tail is None:
        raise InvalidTerms("no cashflows remain after settlement")
    received = [f for f in flows if f.payment_date <= horizon_settlement]
    coupons_received = math.fsum(f.coupon for f in received)
    if tail is not None:
        coupons_received += _tail_received(tail, settlement, horizon_settlement)
    pairs = tuple((f.payment_date, f.amount) for f in flows)
    reproject = None
    if floating is not None:
        leg = _floating_leg(instrument, floating, start_date, redemption_date)
        start_coupons = leg.project(floating.projection, settlement_date=settlement)
        coupons_received = math.fsum(
            c.amount_rub for c in start_coupons if c.payment_date <= horizon_settlement
        )
        pairs = tuple(sorted((*pairs, *((c.payment_date, c.amount_rub) for c in start_coupons))))
        principal_left = tuple(
            (f.payment_date, f.amount) for f in flows if f.payment_date > horizon_settlement
        )
        start_curve = floating.projection

        def reproject(rolled: DiscountCurve) -> tuple[tuple[date, float], ...]:
            coupons = leg.project(
                rolled,
                settlement_date=horizon_settlement,
                check_guard=False,
                bridge=(start_curve, horizon_date),
            )
            return tuple(
                sorted((*principal_left, *((c.payment_date, c.amount_rub) for c in coupons)))
            )

    remaining = tuple((d, a) for d, a in pairs if d > horizon_settlement)
    if not remaining and tail is None:
        raise InvalidTerms("the bond is redeemed before the horizon settlement")
    return _Setup(
        instrument,
        settlement,
        horizon_settlement,
        redemption_date,
        label,
        remaining,
        coupons_received,
        math.fsum(f.principal for f in received),
        pairs,
        None if floating is None else floating.projection,
        reproject,
        tail,
        offer,
    )


def _perpetual_put(instrument: Instrument, policy: HorizonPolicy, settlement: date) -> Offer | None:
    """The put a never-called perpetual is valued to under ``policy``, or None."""
    if policy == "maturity" or not any(o.kind is OfferKind.PUT for o in instrument.offers):
        return None
    try:
        return instrument.horizon(policy, settlement)[1]
    except InvalidTerms:  # no put chosen: a perpetual has no maturity horizon
        return None


def _tail_received(tail: PerpetualTail, settlement: date, horizon_settlement: date) -> float:
    """The tail coupons paid in ``(settlement, horizon_settlement]``, received as cash."""
    total, k = 0.0, 1
    while (paid := tail.payment_date(k)) <= horizon_settlement:
        if paid > settlement:
            total += tail.amount
        k += 1
    return total


def _floating_leg(
    instrument: Instrument, floating: _FloatingInputs, start_date: date, redemption_date: date
) -> FloatingLeg:
    terms = instrument.coupon_leg
    if not isinstance(terms, FloatingCouponLeg):
        raise InvalidTerms("a floating bond needs a floating coupon leg")
    history = floating.history
    if history.valuation_date != start_date:
        raise InvalidTerms("the fixing history must be dated at the start curve date")
    if history.index != terms.rule.index:
        raise InvalidTerms(f"the fixings are {history.index}, the coupon is on {terms.rule.index}")
    periods = tuple(p for p in terms.periods if p.payment_date <= redemption_date)
    if not periods:
        raise InvalidTerms("no floating coupon period is paid by the redemption date")
    return FloatingLeg(terms.rule, periods, history, strike_guard_bp=floating.strike_guard_bp)


def _price(
    flows: tuple[tuple[date, float], ...],
    curve: DiscountCurve,
    spread_bp: float,
    settlement: date,
    convention: ZSpreadConvention,
    tail: PerpetualTail | None = None,
) -> float:
    return price_cashflows(
        flows,
        curve,
        spread=spread_bp / 10_000.0,
        settlement_date=settlement,
        convention=convention,
        perpetual_tail=tail,
    )


def _start_spread(
    setup: _Setup,
    curve: DiscountCurve,
    convention: ZSpreadConvention,
    spread_bp: float | None,
    dirty_price_rub: float | None,
) -> float:
    if (spread_bp is None) == (dirty_price_rub is None):
        raise InvalidTerms("pass exactly one of spread_bp and dirty_price_rub")
    if spread_bp is not None:
        return finite_money(spread_bp, "spread bp")
    dirty = positive_money(dirty_price_rub, "dirty price")  # type: ignore[arg-type]
    return (
        solve_z_spread(
            setup.pv_flows,
            dirty,
            curve,
            settlement_date=setup.settlement,
            convention=convention,
            perpetual_tail=setup.tail,
        )
        * 10_000.0
    )


def horizon_valuation(
    instrument: Instrument,
    curve: DiscountCurve | CurveBootstrapRequest,
    calendar: BusinessCalendar,
    *,
    horizon_date: date,
    spread_bp: float | None = None,
    dirty_price_rub: float | None = None,
    spread_convention: ZSpreadConvention | None = None,
    redemption_horizon: HorizonPolicy = "maturity",
    curve_cache: ShockedCurveCache | None = None,
    fixings: FixingHistory | None = None,
    projection_curve: DiscountCurve | CurveBootstrapRequest | None = None,
    strike_guard_bp: float | None = None,
    perpetual_tail: PerpetualTail | None = None,
) -> HorizonValuation:
    """Carry a fixed-coupon bond or a RUB floater from ``curve.curve_date`` to ``horizon_date``.

    The spread is held: pass it (``spread_bp``) or the start dirty price to calibrate
    it. Settlements are the instrument's lag in business days of ``calendar``.
    ``redemption_horizon="confirmed_put"`` values to the first confirmed put
    (yield-to-offer, the market convention for put bonds).

    A floater needs ``fixings`` dated at the curve date. Its coupons project off
    ``projection_curve`` (same date) or, when None, off ``curve`` itself;
    ``strike_guard_bp`` is the cap/floor policy of :class:`FloatingLeg`.

    A never-called perpetual (0.41.0) needs ``perpetual_tail``, the coupons after its
    listed ones (``ExercisedBond.perpetual_tail``): each value adds the tail after its
    settlement (``pv_rolled`` on the rolled curve, read from the horizon date), and tail
    coupons paid by the horizon settlement are received. A put the horizon policy picks
    is valued to instead, as in ``value_portfolio``.
    """
    convention = ZSpreadConvention() if spread_convention is None else spread_convention
    cache = ShockedCurveCache() if curve_cache is None else curve_cache
    base = _base_curve(curve, cache)
    floating = None
    if fixings is not None:
        projection = base if projection_curve is None else _base_curve(projection_curve, cache)
        if projection.curve_date != base.curve_date:
            raise InvalidTerms("projection and discount curves must have the same date")
        floating = _FloatingInputs(fixings, projection, strike_guard_bp)
    elif projection_curve is not None:
        raise InvalidTerms("a projection curve needs the floater's fixings")
    setup = _setup(
        instrument,
        calendar,
        base.curve_date,
        horizon_date,
        redemption_horizon,
        floating,
        perpetual_tail,
    )
    spread = _start_spread(setup, base, convention, spread_bp, dirty_price_rub)
    return _valuation(setup, base, horizon_date, spread, convention)


def _valuation(
    setup: _Setup,
    base: DiscountCurve,
    horizon_date: date,
    spread: float,
    convention: ZSpreadConvention,
) -> HorizonValuation:
    tail = setup.tail
    pv_start = _price(setup.pv_flows, base, spread, setup.settlement, convention, tail)
    pv_forward = _price(setup.remaining, base, spread, setup.horizon_settlement, convention, tail)
    rolled = roll_curve(base, horizon_date)
    rolled_flows = setup.remaining
    if setup.reproject is not None:
        assert setup.projection is not None
        rolled_projection = (
            rolled if setup.projection is base else roll_curve(setup.projection, horizon_date)
        )
        rolled_flows = setup.reproject(rolled_projection)
    pv_rolled = _price(rolled_flows, rolled, spread, setup.horizon_settlement, convention, tail)
    received = setup.received_coupons + setup.received_principal
    carry = finite_money(pv_forward + received - pv_start, "carry")
    rolldown = finite_money(pv_rolled - pv_forward, "rolldown")
    instrument = setup.instrument
    return HorizonValuation(
        instrument.instrument_id,
        base.curve_date,
        horizon_date,
        setup.settlement,
        setup.horizon_settlement,
        setup.policy,
        setup.redemption_date,
        spread,
        convention.identifier,
        pv_start,
        pv_forward,
        pv_rolled,
        setup.received_coupons,
        setup.received_principal,
        None if setup.reproject is not None else instrument.accrued(setup.settlement),
        # Past the last listed coupon a perpetual accrues a tail coupon the terms do not list.
        None
        if setup.reproject is not None
        or (tail is not None and setup.horizon_settlement >= tail.anchor_date)
        else instrument.accrued(setup.horizon_settlement),
        instrument.outstanding_face(setup.settlement),
        instrument.outstanding_face(setup.horizon_settlement),
        carry,
        rolldown,
        finite_money(pv_rolled + received - pv_start, "horizon P&L"),
    )


def explain_horizon_pnl(
    instrument: Instrument,
    start_curve: DiscountCurve | CurveBootstrapRequest,
    end_curve: DiscountCurve | CurveBootstrapRequest,
    calendar: BusinessCalendar,
    *,
    start_spread_bp: float | None = None,
    start_dirty_price_rub: float | None = None,
    end_spread_bp: float | None = None,
    end_dirty_price_rub: float | None = None,
    spread_convention: ZSpreadConvention | None = None,
    redemption_horizon: HorizonPolicy = "maturity",
    curve_cache: ShockedCurveCache | None = None,
    fixings: FixingHistory | None = None,
    end_fixings: FixingHistory | None = None,
    projection_curve: DiscountCurve | CurveBootstrapRequest | None = None,
    end_projection_curve: DiscountCurve | CurveBootstrapRequest | None = None,
    strike_guard_bp: float | None = None,
    perpetual_tail: PerpetualTail | None = None,
) -> HorizonPnlExplain:
    """Explain the change from the start market to the end market (curve and spread).

    The horizon is the end curve's date. The end spread is given or calibrated from
    the end dirty price at the horizon settlement.

    A RUB floater (0.33.0, BACKLOG #78) needs ``fixings`` dated at the start curve date
    and ``end_fixings`` dated at the end curve date; its coupons project off
    ``projection_curve`` / ``end_projection_curve`` (same dates), or off the discount
    curves when those are None. Carry and rolldown are :func:`horizon_valuation`'s. At
    the end market the remaining coupons are re-projected with the end fixings on the
    end projection curve: ``rates`` is that repricing at the start spread, so it holds
    the projection-curve move and the fixings published over the horizon against the
    forwards that carry assumed; ``spread`` reprices the rolled flows at the end spread.

    ``perpetual_tail`` (0.41.0): a never-called perpetual's tail, as in
    :func:`horizon_valuation`; the end market prices the same tail at the end curve.
    """
    convention = ZSpreadConvention() if spread_convention is None else spread_convention
    cache = ShockedCurveCache() if curve_cache is None else curve_cache
    start = _base_curve(start_curve, cache)
    end = _base_curve(end_curve, cache)
    horizon_date = end.curve_date
    floating = None
    if instrument.product is ProductType.FLOATING_BOND:
        if fixings is None or end_fixings is None:
            raise UnsupportedProduct(
                "P&L explain of a floater needs its fixings at the start and at the end "
                "(fixings=, end_fixings=)"
            )
        start_projection = (
            start if projection_curve is None else _base_curve(projection_curve, cache)
        )
        end_projection = (
            end if end_projection_curve is None else _base_curve(end_projection_curve, cache)
        )
        if start_projection.curve_date != start.curve_date:
            raise InvalidTerms("projection and discount curves must have the same date")
        if end_projection.curve_date != horizon_date:
            raise InvalidTerms("end projection and end discount curves must have the same date")
        if end_fixings.valuation_date != horizon_date:
            raise InvalidTerms("the end fixings must be dated at the end curve date")
        floating = _FloatingInputs(fixings, start_projection, strike_guard_bp)
    elif any(x is not None for x in (fixings, end_fixings, projection_curve, end_projection_curve)):
        raise InvalidTerms("fixings and projection curves are for floaters only")
    setup = _setup(
        instrument,
        calendar,
        start.curve_date,
        horizon_date,
        redemption_horizon,
        floating,
        perpetual_tail,
    )
    tail = setup.tail
    s0 = _start_spread(setup, start, convention, start_spread_bp, start_dirty_price_rub)
    valuation = _valuation(setup, start, horizon_date, s0, convention)
    rolled = roll_curve(start, horizon_date)
    at = setup.horizon_settlement
    end_flows, rolled_flows = setup.remaining, setup.remaining
    if floating is not None:
        assert end_fixings is not None and setup.reproject is not None
        end_inputs = _FloatingInputs(end_fixings, end_projection, strike_guard_bp)
        end_leg = _floating_leg(instrument, end_inputs, horizon_date, setup.redemption_date)
        coupons = end_leg.project(end_projection, settlement_date=at)
        offer = setup.offer
        principal = tuple(
            (f.payment_date, f.amount)
            for f in instrument.cashflows(
                after=at, horizon=setup.redemption_date, offer=offer
            )
        )
        end_flows = tuple(
            sorted((*principal, *((c.payment_date, c.amount_rub) for c in coupons)))
        )
        assert setup.projection is not None
        rolled_projection = (
            rolled if setup.projection is start else roll_curve(setup.projection, horizon_date)
        )
        rolled_flows = setup.reproject(rolled_projection)
    end_setup = replace(setup, settlement=at, pv_flows=end_flows)
    s1 = _start_spread(end_setup, end, convention, end_spread_bp, end_dirty_price_rub)
    pv_end = _price(end_flows, end, s1, at, convention, tail)
    rates = _price(end_flows, end, s0, at, convention, tail) - valuation.pv_rolled_rub
    spread = _price(rolled_flows, rolled, s1, at, convention, tail) - valuation.pv_rolled_rub
    total = pv_end + valuation.received_rub - valuation.pv_start_rub
    residual = total - (valuation.carry_rub + valuation.rolldown_rub + rates + spread)
    return HorizonPnlExplain(
        valuation,
        s1,
        pv_end,
        valuation.carry_rub,
        valuation.rolldown_rub,
        finite_money(rates, "rates P&L"),
        finite_money(spread, "spread P&L"),
        finite_money(residual, "residual P&L"),
        finite_money(total, "total P&L"),
    )
