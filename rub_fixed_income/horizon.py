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
floater has no core convention, so its accrued fields are None. P&L explain of a
floater is not supported: it needs the fixings realised over the horizon.

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
from datetime import date, timedelta
from typing import Literal

from ._money import finite_money, positive_money
from ._risk_common import ShockedCurveCache, _base_curve
from .compatibility import price_cashflows, solve_z_spread
from .conventions import ZSpreadConvention
from .dates import BusinessCalendar
from .discount import CurveBootstrapRequest, DiscountCurve
from .domain import FloatingCouponLeg, Instrument, ProductType
from .errors import InvalidTerms, UnsupportedProduct
from .floating import FixingHistory, FloatingLeg
from .key_rate import KeyRateForwardCurve, KeyRateForwardSegment

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
HorizonPolicy = Literal["maturity", "confirmed_put"]
FxHorizon = Literal["spot_held", "forwards_realised"]
FX_HORIZONS: tuple[FxHorizon, ...] = ("spot_held", "forwards_realised")
"""The FX spot at a horizon: today's spot, or today's forward for the horizon spot date."""


def fx_horizon(value: object) -> FxHorizon:
    """``value`` checked as an :data:`FxHorizon`."""
    if value == "spot_held" or value == "forwards_realised":
        return value
    raise InvalidTerms(f"fx_horizon must be one of {', '.join(FX_HORIZONS)}")


def roll_curve(curve: DiscountCurve, horizon_date: date) -> DiscountCurve:
    """The curve moved to ``horizon_date`` with its shape in calendar days unchanged."""
    if horizon_date < curve.curve_date:
        raise InvalidTerms("a curve can only be rolled forward")
    shift = timedelta(days=(horizon_date - curve.curve_date).days)
    return replace(
        curve,
        curve_date=horizon_date,
        nodes=tuple((node + shift, df) for node, df in curve.nodes),
    )


def forward_curve(curve: DiscountCurve, horizon_date: date) -> DiscountCurve:
    """Today's curve read forward from ``horizon_date``: ``DF_h(t) = DF(t) / DF(horizon)``.

    Exact for ``t`` up to the last node (log-DF interpolation is linear in calendar
    days); beyond it the flat forward is kept, which is exact for curve time bases
    linear in days (ACT/365F, ACT/360).
    """
    if horizon_date < curve.curve_date:
        raise InvalidTerms("a curve can only be read forward")
    if horizon_date == curve.curve_date:
        return curve
    flat = curve.flat_continuous_rate
    if flat is not None:
        return DiscountCurve.flat(horizon_date, flat, convention=curve.convention)
    anchor = curve.discount_factor(horizon_date)
    later = tuple((node, df / anchor) for node, df in curve.nodes if node > horizon_date)
    if later:
        return replace(curve, curve_date=horizon_date, nodes=later)
    if len(curve.nodes) == 1:
        prior_date, prior_log_df = curve.curve_date, 0.0
    else:
        prior_date, prior_df = curve.nodes[-2]
        prior_log_df = math.log(prior_df)
    last_date, last_df = curve.nodes[-1]
    years = curve.convention.time_day_count.year_fraction(prior_date, last_date)
    rate = -(math.log(last_df) - prior_log_df) / years
    return DiscountCurve.flat(horizon_date, rate, convention=curve.convention)


def roll_key_rate_curve(curve: KeyRateForwardCurve, horizon_date: date) -> KeyRateForwardCurve:
    """A key-rate forward curve moved to ``horizon_date`` with its dates shifted alike."""
    if horizon_date < curve.curve_date:
        raise InvalidTerms("a curve can only be rolled forward")
    shift = horizon_date - curve.curve_date
    return KeyRateForwardCurve(
        horizon_date,
        curve.current_rate,
        curve.current_rate_end + shift,
        tuple(
            KeyRateForwardSegment(s.start_date + shift, s.end_date + shift, s.rate, s.pillar_tenor)
            for s in curve.segments
        ),
        curve.model_version,
    )


@dataclass(frozen=True)
class HorizonValuation:
    """Carry and rolldown of one bond from ``settlement_date`` to the horizon settlement."""

    instrument_id: str
    start_date: date
    horizon_date: date
    settlement_date: date
    horizon_settlement_date: date
    redemption_horizon: HorizonPolicy
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
    policy: HorizonPolicy
    remaining: tuple[tuple[date, float], ...]
    received_coupons: float
    received_principal: float
    pv_flows: tuple[tuple[date, float], ...]
    # Floaters: the start projection curve and the remaining flows re-projected off a
    # rolled projection curve (observations before the horizon at today's forwards).
    projection: DiscountCurve | None = None
    reproject: Callable[[DiscountCurve], tuple[tuple[date, float], ...]] | None = None


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
) -> _Setup:
    floater = instrument.product is ProductType.FLOATING_BOND
    if not (instrument.product is ProductType.FIXED_BOND or (floater and floating is not None)):
        raise UnsupportedProduct(
            f"{instrument.product.value} horizon valuation is not supported"
            + (" without its fixing history (fixings=)" if floater else "")
        )
    if instrument.is_perpetual:
        raise UnsupportedProduct("perpetual bonds need a call assumption (package 9)")
    if horizon_date <= start_date:
        raise InvalidTerms("horizon date must be after the start date")
    settlement = calendar.add_business_days(start_date, instrument.settlement_lag_days)
    horizon_settlement = calendar.add_business_days(horizon_date, instrument.settlement_lag_days)
    redemption_date, offer = instrument.horizon(policy, settlement)
    flows = instrument.cashflows(after=settlement, horizon=redemption_date, offer=offer)
    if not flows:
        raise InvalidTerms("no cashflows remain after settlement")
    received = [f for f in flows if f.payment_date <= horizon_settlement]
    coupons_received = math.fsum(f.coupon for f in received)
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
    if not remaining:
        raise InvalidTerms("the bond is redeemed before the horizon settlement")
    return _Setup(
        instrument,
        settlement,
        horizon_settlement,
        redemption_date,
        "maturity" if offer is None else "confirmed_put",
        remaining,
        coupons_received,
        math.fsum(f.principal for f in received),
        pairs,
        None if floating is None else floating.projection,
        reproject,
    )


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
) -> float:
    return price_cashflows(
        flows, curve, spread=spread_bp / 10_000.0, settlement_date=settlement, convention=convention
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
            setup.pv_flows, dirty, curve, settlement_date=setup.settlement, convention=convention
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
) -> HorizonValuation:
    """Carry a fixed-coupon bond or a RUB floater from ``curve.curve_date`` to ``horizon_date``.

    The spread is held: pass it (``spread_bp``) or the start dirty price to calibrate
    it. Settlements are the instrument's lag in business days of ``calendar``.
    ``redemption_horizon="confirmed_put"`` values to the first confirmed put
    (yield-to-offer, the market convention for put bonds).

    A floater needs ``fixings`` dated at the curve date. Its coupons project off
    ``projection_curve`` (same date) or, when None, off ``curve`` itself;
    ``strike_guard_bp`` is the cap/floor policy of :class:`FloatingLeg`.
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
        instrument, calendar, base.curve_date, horizon_date, redemption_horizon, floating
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
    pv_start = _price(setup.pv_flows, base, spread, setup.settlement, convention)
    pv_forward = _price(setup.remaining, base, spread, setup.horizon_settlement, convention)
    rolled = roll_curve(base, horizon_date)
    rolled_flows = setup.remaining
    if setup.reproject is not None:
        assert setup.projection is not None
        rolled_projection = (
            rolled if setup.projection is base else roll_curve(setup.projection, horizon_date)
        )
        rolled_flows = setup.reproject(rolled_projection)
    pv_rolled = _price(rolled_flows, rolled, spread, setup.horizon_settlement, convention)
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
        None if setup.reproject is not None else instrument.accrued(setup.horizon_settlement),
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
) -> HorizonPnlExplain:
    """Explain the change from the start market to the end market (curve and spread).

    The horizon is the end curve's date. The end spread is given or calibrated from
    the end dirty price at the horizon settlement. Floaters are rejected: explaining
    their P&L needs the fixings realised over the horizon.
    """
    if instrument.product is ProductType.FLOATING_BOND:
        raise UnsupportedProduct(
            "P&L explain of a floater is not supported: it needs the fixings realised over "
            "the horizon and the end projection curve"
        )
    convention = ZSpreadConvention() if spread_convention is None else spread_convention
    cache = ShockedCurveCache() if curve_cache is None else curve_cache
    start = _base_curve(start_curve, cache)
    end = _base_curve(end_curve, cache)
    horizon_date = end.curve_date
    setup = _setup(instrument, calendar, start.curve_date, horizon_date, redemption_horizon)
    s0 = _start_spread(setup, start, convention, start_spread_bp, start_dirty_price_rub)
    valuation = _valuation(setup, start, horizon_date, s0, convention)
    end_setup = replace(setup, settlement=setup.horizon_settlement, pv_flows=setup.remaining)
    s1 = _start_spread(end_setup, end, convention, end_spread_bp, end_dirty_price_rub)
    rolled = roll_curve(start, horizon_date)
    at = setup.horizon_settlement
    pv_end = _price(setup.remaining, end, s1, at, convention)
    rates = _price(setup.remaining, end, s0, at, convention) - valuation.pv_rolled_rub
    spread = _price(setup.remaining, rolled, s1, at, convention) - valuation.pv_rolled_rub
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
