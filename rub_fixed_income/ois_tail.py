"""RUONIA OIS beyond its last quote: synthetic tail quotes (issue #84).

A curve bootstrapped from quotes to 10Y extrapolates flat-forward from its last segment
(9Y-10Y). A RUONIA bucket bump moves the quotes by the bucket hats, so the 10Y bucket moves the
forward of that segment strongly and carries it over the whole tail: +1 bp in the 10Y bucket moved
the 20Y annual zero by about +5 bp, the 7Y bucket moved the 15Y zero by about -2.6 bp, and the
bucket risk of every position discounted or projected on RUONIA past 10Y swung by hundreds of
RUB/bp with opposite signs (the sum stayed right).

:class:`OisTail` (``ValuationPolicy.ois_tail``, off by default) extends the OIS request with one
synthetic par quote per v3 par tenor (``PAR_YEARS``) that matures after the last real quote
(15Y and 20Y after a 10Y last quote), fixed at the base; RUONIA buckets move them by their hats
like any quote, so a 15Y position's risk lands on 10Y/15Y and nothing past the last quote
follows the 9Y-10Y forward:

- ``kbd`` rule (``kbd_curve_id`` given and in the snapshot): ``KBD par(T) - s_tail``, with
  ``s_tail = KBD par(T_last) - quote(T_last)`` at the last real quote: a constant par spread to
  the KBD, the ZX tail of the RUONIA + basis view (issue #83, :mod:`rub_fixed_income.ofz_basis`)
  to the bit. ``KBD par(m)`` is :func:`annual_par_yield` on the KBD;
- ``flat_zero`` rule (no KBD): the par rates, under the request's own quote convention, of the
  base curve continued from its last node at the last node's zero rate (a flat zero curve past
  the last quote: a log-linear DF node at the last synthetic maturity reproduces it exactly).

A synthetic quote matures where its tenor resolves under the OIS convention
(:func:`tenor_maturity`): the OIS calendar must cover 20Y plus the spot lag.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from typing import Final, Literal

from ._money import finite_money
from ._par_overlay import PAR_YEARS
from .canonical import OMIT_IF_DEFAULT
from .dates import add_months
from .discount import CurveBootstrapRequest, DiscountCurve, OisQuote, bootstrap_discount_curve
from .errors import InvalidTerms

__all__ = [
    "OIS_TAIL_MODELS",
    "OisTail",
    "OisTailRule",
    "annual_par_yield",
    "extend_ois_request",
    "flat_zero_tail_quotes",
    "kbd_tail_quotes",
    "tenor_maturity",
]

OisTailRule = Literal["kbd", "flat_zero"]
OIS_TAIL_MODELS: Final = {"kbd": "ois-tail-kbd-par-spread-v1", "flat_zero": "ois-tail-flat-zero-v1"}
"""Model tag of each rule, appended to the OIS curve's ``CurveUse.model_version``."""

_STUB_DAYS = 180


@dataclass(frozen=True)
class OisTail:
    """Continue the OIS curve ``ois_curve_id`` past its last quote (``ValuationPolicy.ois_tail``).

    ``kbd_curve_id`` names a KBD curve of the snapshot (any ``rub_ofz_kbd_*`` spec): the tail
    keeps the par spread to it at the last quote. Without it, or on a date whose snapshot lacks
    that curve, the tail is a flat zero rate from the last node. The rule used is recorded in
    the result (``CurveUse.model_version`` of the OIS curve ends ``+<OIS_TAIL_MODELS[rule]>``).
    """

    ois_curve_id: str
    kbd_curve_id: str | None = field(default=None, metadata=OMIT_IF_DEFAULT)

    def __post_init__(self) -> None:
        for label in ("ois_curve_id", "kbd_curve_id"):
            value = getattr(self, label)
            if value is None and label == "kbd_curve_id":
                continue
            if not isinstance(value, str) or not value or "|" in value:
                raise InvalidTerms(f"{label} must be a non-empty string without '|'")
        if self.ois_curve_id == self.kbd_curve_id:
            raise InvalidTerms("the OIS and KBD curve IDs must differ")


def annual_par_yield(curve: DiscountCurve, maturity: date) -> float:
    """Par yield of an annual-coupon bond on ``curve`` from its curve date to ``maturity``.

    Coupon dates roll yearly from the curve date, unadjusted; a roll date within 180 days
    before ``maturity`` is dropped (a long last period). Accrual on the curve's time day count.
    """
    start = curve.curve_date
    day_count = curve.convention.time_day_count
    dates: list[date] = []
    k = 1
    while (roll := add_months(start, 12 * k)) < maturity - timedelta(days=_STUB_DAYS):
        dates.append(roll)
        k += 1
    dates.append(maturity)
    previous = [start, *dates[:-1]]
    annuity = math.fsum(
        day_count.year_fraction(a, b) * curve.discount_factor(b) for a, b in zip(previous, dates)
    )
    return (1.0 - curve.discount_factor(maturity)) / annuity


def tenor_maturity(request: CurveBootstrapRequest, years: int) -> date:
    """Where a ``years``-year OIS quote of ``request``'s convention matures."""
    ois = request.convention.ois_swap
    if ois is None:
        return add_months(request.curve_date, 12 * years)
    from .tenors import resolve_tenor

    return resolve_tenor(
        request.curve_date,
        f"{years}Y",
        ois.calendar,
        spot_lag_days=ois.spot_lag_business_days,
        business_day_convention=ois.business_day_convention,
    ).end_date


def _tail_maturities(request: CurveBootstrapRequest) -> list[tuple[int, date]]:
    last = max(q.maturity for q in request.quotes)
    found = []
    for years in PAR_YEARS:
        maturity = tenor_maturity(request, years)
        if maturity > last:
            found.append((years, maturity))
    return found


def kbd_tail_quotes(
    kbd: DiscountCurve, request: CurveBootstrapRequest
) -> tuple[tuple[OisQuote, ...], float]:
    """The ``kbd`` rule's synthetic quotes and the decimal par spread ``KBD par - quote`` at
    the last real quote (the ZX tail of :class:`rub_fixed_income.ofz_basis.OfzBasisCurve`)."""
    last = request.quotes[-1]
    spread = annual_par_yield(kbd, last.maturity) - last.par_rate
    synthetic = []
    for years, maturity in _tail_maturities(request):
        par = finite_money(annual_par_yield(kbd, maturity) - spread, "synthetic OIS quote")
        synthetic.append(OisQuote(f"{years}Y", maturity, par))
    return tuple(synthetic), spread


def flat_zero_tail_quotes(request: CurveBootstrapRequest) -> tuple[OisQuote, ...]:
    """The ``flat_zero`` rule's synthetic quotes: par rates of the base curve continued at the
    zero rate of its last node."""
    targets = _tail_maturities(request)
    if not targets:
        return ()
    base = bootstrap_discount_curve(request)
    last_date, last_df = base.nodes[-1]
    time = base.convention.time_day_count.year_fraction
    far = targets[-1][1]
    far_df = math.exp(
        math.log(last_df) * time(base.curve_date, far) / time(base.curve_date, last_date)
    )
    flat = DiscountCurve(
        base.curve_date, (*base.nodes, (far, far_df)), base.model_version, None, base.convention
    )
    return tuple(
        OisQuote(
            f"{years}Y",
            maturity,
            finite_money(
                flat.quote_par_rate(OisQuote(f"{years}Y", maturity, 0.0)), "synthetic OIS quote"
            ),
        )
        for years, maturity in targets
    )


def extend_ois_request(
    request: CurveBootstrapRequest, kbd: DiscountCurve | None
) -> tuple[CurveBootstrapRequest, OisTailRule]:
    """``request`` with its tail quotes: the ``kbd`` rule on ``kbd``, else ``flat_zero``.

    A request whose last quote is at or past the last par tenor is returned as given.
    """
    if not request.quotes:
        raise InvalidTerms("an OIS tail needs at least one quote")
    ordered = replace(request, quotes=tuple(sorted(request.quotes, key=lambda q: q.maturity)))
    if kbd is not None:
        if kbd.curve_date != request.curve_date:
            raise InvalidTerms("the KBD and the OIS quotes must have the same curve date")
        synthetic, _ = kbd_tail_quotes(kbd, ordered)
        rule: OisTailRule = "kbd"
    else:
        synthetic = flat_zero_tail_quotes(ordered)
        rule = "flat_zero"
    if not synthetic:
        return request, rule
    return replace(request, quotes=(*request.quotes, *synthetic)), rule
