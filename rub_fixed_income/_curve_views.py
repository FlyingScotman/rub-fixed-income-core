"""Curve views at a horizon and the FX spot rule, shared by every carry engine (BACKLOG #101).

A position carried to a horizon is valued on two views of today's curves:
:func:`forward_curve` (read forward, ``DF(t) / DF(horizon)``: today's forwards
realised) and :func:`roll_curve` / :func:`roll_key_rate_curve` (moved to the horizon
with the shape held in calendar days, a CBR decision date held: the theta). An FX product also needs its spot at
the horizon, :data:`FxHorizon`. The module depends only on curve types, so product
modules import it without a cycle; :mod:`rub_fixed_income.horizon` re-exports it.
"""

from __future__ import annotations

import math
from dataclasses import replace
from datetime import date, timedelta
from typing import Literal

from .discount import DiscountCurve
from .errors import InvalidTerms, MissingMarketData
from .key_rate import KeyRateForwardCurve, KeyRateForwardSegment

__all__ = [
    "FX_HORIZONS",
    "FxHorizon",
    "forward_curve",
    "fx_horizon",
    "roll_curve",
    "roll_key_rate_curve",
]

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
    """A key-rate forward curve moved to ``horizon_date``, the decision date held (BACKLOG #109).

    The next Bank of Russia decision (``current_rate_end``) is a calendar event and stays
    on its date, the rate in force until it unchanged. The pillar segments are tenor-held:
    each segment end moves by the horizon, and the first one still starts at the
    decision date. A horizon on or after the decision has it realised at today's
    forwards: the first segment's rate is in force at the horizon and becomes the current
    interval (no later decision date is known; its end is the first rolled pillar).
    """
    if horizon_date < curve.curve_date:
        raise InvalidTerms("a curve can only be rolled forward")
    shift = horizon_date - curve.curve_date
    decision = curve.current_rate_end
    segments: list[KeyRateForwardSegment] = []
    start = decision
    for s in curve.segments:
        segments.append(KeyRateForwardSegment(start, s.end_date + shift, s.rate, s.pillar_tenor))
        start = s.end_date + shift
    if horizon_date < decision:
        return KeyRateForwardCurve(
            horizon_date, curve.current_rate, decision, tuple(segments), curve.model_version
        )
    if not segments:
        raise MissingMarketData("key-rate curve ends before the horizon")
    # the first segment ends at its pillar plus the shift, after the horizon
    current = segments[0]
    return KeyRateForwardCurve(
        horizon_date, current.rate, current.end_date, tuple(segments[1:]), curve.model_version
    )
