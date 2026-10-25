"""Foreign curves rebuilt from their par quotes under every shock (not public API).

A quote request (:class:`QuoteCurve`, e.g. ``xccy.XccyCurveBootstrapRequest``) given to an
FX engine in place of a built foreign curve makes the engine's foreign buckets risk to
those quotes: each bucket shift moves every quote by the bucket hats at its maturity and
the curve is rebuilt. A request whose build reads the RUB curve (``needs_rub``, XCCY par
swaps against RUONIA) is rebuilt on the engine's shocked RUB curve, its quotes held, so
RUB buckets are risk with the quotes held too, as in ``xccy.XccyRiskEngine``. Rebuilt
curves are memoised on the request per RUB curve object and shift vectors.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar, cast

from ._risk_common import (
    _ZERO_SHOCK,
    RiskCurve,
    ShockedCurveCache,
    _base_curve,
    _curve_basis,
    _curve_identity,
    _shock_curve,
    _snapshot_curve,
)
from .discount import DiscountCurve, DiscountFactorCurve
from .errors import InvalidTerms

if TYPE_CHECKING:
    from datetime import date

_Shifts = tuple[float, ...]
_QUOTE_CURVES = 4096


class QuoteCurve:
    """Base of the quote requests FX engines rebuild under shocks.

    Subclasses are frozen dataclasses of the quotes. ``_quote_build(shifts, rub)`` is the
    curve from the quotes moved by bucket ``shifts`` (bp at each quote's maturity), built
    on the RUB discount curve ``rub`` when ``needs_rub``; ``_quote_identity()`` their
    content identity; ``quote_basis`` the risk-basis label.
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


ForeignSource = RiskCurve | QuoteCurve


def quote_curve(
    source: QuoteCurve,
    shifts: _Shifts,
    rub_source: object = None,
    rub: DiscountFactorCurve | None = None,
    rub_shifts: _Shifts = _ZERO_SHOCK,
) -> DiscountCurve:
    """``source`` rebuilt from quotes moved by ``shifts``; on ``rub`` (the view of
    ``rub_source`` under ``rub_shifts``) when it needs a RUB curve. Memoised."""
    if not source.needs_rub:
        rub_source, rub, rub_shifts = None, None, _ZERO_SHOCK
    elif rub is None:
        raise InvalidTerms(f"{type(source).__name__} is rebuilt on a RUB curve")
    store = source.__dict__.get("_quote_curves")
    if store is None:
        store = {}
        object.__setattr__(source, "_quote_curves", store)
    groups = cast(dict[int, tuple[object, dict[tuple[_Shifts, _Shifts], DiscountCurve]]], store)
    group = groups.get(id(rub_source))
    if group is None or group[0] is not rub_source:
        group = (rub_source, {})
        groups[id(rub_source)] = group
    curves = group[1]
    key = (rub_shifts, shifts)
    curve = curves.get(key)
    if curve is None:
        curve = source._quote_build(shifts, rub)
        if len(curves) < _QUOTE_CURVES:
            curves[key] = curve
    return curve


def foreign_base(
    source: ForeignSource, rub_source: RiskCurve, rub: DiscountCurve, cache: ShockedCurveCache
) -> DiscountCurve:
    """The base foreign curve: built from quotes, or as ``_risk_common`` builds it."""
    if isinstance(source, QuoteCurve):
        return quote_curve(source, _ZERO_SHOCK, rub_source, rub)
    return _base_curve(source, cache)


def foreign_view(
    source: ForeignSource,
    base: DiscountCurve,
    shifts: _Shifts,
    rub_source: RiskCurve,
    rub: DiscountFactorCurve,
    rub_shifts: _Shifts,
    cache: ShockedCurveCache,
) -> DiscountFactorCurve:
    """The foreign curve under ``shifts`` with the RUB curve ``rub`` under ``rub_shifts``."""
    if isinstance(source, QuoteCurve):
        return quote_curve(source, shifts, rub_source, rub, rub_shifts)
    return _shock_curve(source, base, shifts, cache)


def foreign_basis(source: ForeignSource) -> str:
    return source.quote_basis if isinstance(source, QuoteCurve) else _curve_basis(source)


def foreign_identity(source: ForeignSource) -> object:
    return source._quote_identity() if isinstance(source, QuoteCurve) else _curve_identity(source)


def snapshot_foreign(source: ForeignSource) -> ForeignSource:
    """Requests are validated immutable values; built curves are snapshotted."""
    return source if isinstance(source, QuoteCurve) else _snapshot_curve(source)
