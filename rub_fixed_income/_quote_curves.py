"""Curves rebuilt from their par quotes under every shock (not public API).

A quote request (:class:`QuoteCurve`: ``xccy.XccyCurveBootstrapRequest``,
``proxy_curves.ParYieldCurveRequest``) given to an engine in place of a built curve makes
its buckets risk to those quotes: a bucket shift moves every quote by the bucket hats at
its maturity and the curve is rebuilt, as an OIS ``CurveBootstrapRequest`` is
rebootstrapped. ``_risk_common`` shocks requests that build alone; a request whose build
reads the RUB curve (``needs_rub``: XCCY par swaps against RUONIA) is rebuilt by the FX
engines on their shocked RUB curve, its quotes held under RUB shocks. Rebuilt curves are
memoised on the request per RUB curve object and shift vectors.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar, cast

from .errors import InvalidTerms

if TYPE_CHECKING:
    from datetime import date

    from .discount import DiscountCurve, DiscountFactorCurve

_Shifts = tuple[float, ...]
_NO_SHIFT: _Shifts = (0.0,) * 11
_QUOTE_CURVES = 4096


class QuoteCurve:
    """Base of the quote requests engines rebuild under shocks.

    Subclasses are frozen dataclasses of the quotes. ``_quote_build(shifts, rub)`` is the
    curve from the quotes moved by bucket ``shifts`` (bp at each quote's maturity), built
    on the RUB discount curve ``rub`` when ``needs_rub``; ``_quote_identity()`` their
    content identity (canonical payload); ``quote_basis`` the risk-basis label.
    """

    __slots__ = ()
    quote_basis: ClassVar[str] = "quote"
    needs_rub: ClassVar[bool] = False

    if TYPE_CHECKING:

        @property
        def curve_date(self) -> date: ...

    def _quote_build(self, shifts: _Shifts, rub: DiscountFactorCurve | None) -> DiscountCurve:
        raise NotImplementedError

    def _quote_identity(self) -> object:
        raise NotImplementedError


def quote_curve(
    source: QuoteCurve,
    shifts: _Shifts,
    rub_source: object = None,
    rub: DiscountFactorCurve | None = None,
    rub_shifts: _Shifts = _NO_SHIFT,
) -> DiscountCurve:
    """``source`` rebuilt from quotes moved by ``shifts``; on ``rub`` (the view of
    ``rub_source`` under ``rub_shifts``) when it needs a RUB curve. Memoised."""
    if not source.needs_rub:
        rub_source, rub, rub_shifts = None, None, _NO_SHIFT
    elif rub is None:
        raise InvalidTerms(f"{type(source).__name__} is rebuilt on a RUB curve")
    store = source.__dict__.get("_quote_curves")
    if store is None:
        store = {}
        object.__setattr__(source, "_quote_curves", store)
    groups = cast(dict[int, tuple[object, dict[tuple[_Shifts, _Shifts], "DiscountCurve"]]], store)
    group = groups.get(id(rub_source))
    if group is None or group[0] is not rub_source:
        group = (rub_source, {})
        groups[id(rub_source)] = group
    curves = group[1]
    key = (tuple(rub_shifts), tuple(shifts))
    curve = curves.get(key)
    if curve is None:
        curve = source._quote_build(key[1], rub)
        if len(curves) < _QUOTE_CURVES:
            curves[key] = curve
    return curve
