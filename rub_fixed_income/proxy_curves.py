"""Government par-yield proxy curves for USD and EUR bonds (package 3, phase 2).

The market-data hub has no SOFR or ESTR swap quotes; it publishes government par
yields instead (US Treasury par yield curve, ECB euro area AAA par yields). A USD or EUR
bond can be discounted on such a curve as a stated **proxy**: the curve carries
government credit and liquidity, not the overnight rate, and the bond's Z-spread
absorbs the difference. The spec says so (``CurveSpec.proxy``), and so does every
result that uses it (``CurveUse.proxy``), so a reader never mistakes it for OIS.

:func:`bootstrap_par_yield_curve` solves one discount-factor node per quote, in
maturity order, so that a par bond of the spec's convention prices at 100:

- ``usd_ust_par_proxy_v1`` (``par_coupon_frequency=2``): bond-equivalent yields of
  semiannual-coupon securities. A tenor of at most 6 months is one period,
  ``DF = 1 / (1 + y * days / 365)`` (the bill bond-equivalent yield); a longer one pays
  ``y / 2`` on a schedule rolled back from the maturity in 6-month steps, a short first
  period paying its day fraction of the coupon.
- ``eur_ecb_aaa_par_proxy_v1`` (``par_coupon_frequency=0``): the ECB's continuously
  compounded par yield ``y(T) = (1 - DF(T)) / integral_0^T DF(t) dt``, solved exactly on
  log-linear discount factors.

Tenors run from the curve date by calendar months, weeks or days, unadjusted, with no
settlement lag: the curve is a proxy, dated the observation date. Between nodes the
curve is log-linear in the discount factor and flat-forward beyond the last node, as
every core curve. :func:`par_yield` reads a curve's par yield back on the same
convention (every quote reprices to about 1e-12).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from datetime import date
from itertools import pairwise
from typing import ClassVar

from ._money import finite_money
from ._quote_curves import QuoteCurve
from ._risk_common import _shift_at
from ._solver import illinois
from .canonical import canonical_sha256
from .conventions import CurveConvention
from .curve_specs import PAR_YIELD_PROXY_MODEL_VERSION, CurveKind, CurveSpec, get_curve_spec
from .dates import add_months
from .discount import DiscountCurve, DiscountFactorCurve
from .errors import InvalidTerms
from .tenors import add_tenor, parse_tenor

__all__ = [
    "PAR_YIELD_PROXY_MODEL_VERSION",
    "ParYieldCurveRequest",
    "ParYieldQuote",
    "bootstrap_par_yield_curve",
    "par_yield",
]

_SOLVER_TOLERANCE = 1e-15


@dataclass(frozen=True)
class ParYieldQuote:
    """A published par yield: ``tenor`` (``<n>D``, ``<n>W``, ``<n>M`` or ``<n>Y``) and
    ``par_yield`` as a decimal a year (4.25 % is ``0.0425``)."""

    tenor: str
    par_yield: float

    def __post_init__(self) -> None:
        parsed = parse_tenor(self.tenor)
        if parsed.unit not in ("D", "W", "M", "Y"):
            raise InvalidTerms(f"par-yield tenor {self.tenor!r} must be D, W, M or Y")
        object.__setattr__(self, "tenor", parsed.code)
        rate = finite_money(self.par_yield, "par yield")
        if not -0.5 < rate < 1.0:
            raise InvalidTerms("par yield must be a decimal rate between -50 % and 100 %")
        object.__setattr__(self, "par_yield", rate)


@dataclass(frozen=True)
class ParYieldCurveRequest(QuoteCurve):
    """Par-yield quotes of one proxy spec (kind ``PAR_YIELD_PROXY``) on ``curve_date``.

    Engines take it in place of the built curve for risk in the par yields (basis
    ``par_yield_quote``, issue #77): a bucket shift moves each quote by the bucket hats at
    its maturity's curve time (the spec's day count from ``curve_date``), as an OIS quote,
    and :func:`bootstrap_par_yield_curve` rebuilds the curve.
    """

    quote_basis: ClassVar[str] = "par_yield_quote"

    spec_id: str
    curve_date: date
    quotes: tuple[ParYieldQuote, ...]

    def __post_init__(self) -> None:
        spec = get_curve_spec(self.spec_id)
        if spec.kind is not CurveKind.PAR_YIELD_PROXY:
            raise InvalidTerms(f"{self.spec_id} is not a par-yield proxy spec")
        quotes = tuple(self.quotes)
        if not quotes:
            raise InvalidTerms("a par-yield curve needs at least one quote")
        for quote in quotes:
            if not isinstance(quote, ParYieldQuote):
                raise TypeError("quotes must be ParYieldQuote values")
        maturities = [add_tenor(self.curve_date, q.tenor) for q in quotes]
        if len(set(maturities)) != len(maturities):
            raise InvalidTerms("two par-yield quotes have the same maturity")
        object.__setattr__(self, "quotes", quotes)

    @property
    def spec(self) -> CurveSpec:
        return get_curve_spec(self.spec_id)

    def nodes(self) -> tuple[tuple[date, ParYieldQuote], ...]:
        """(maturity, quote) in maturity order."""
        return tuple(
            sorted(
                ((add_tenor(self.curve_date, q.tenor), q) for q in self.quotes),
                key=lambda pair: pair[0],
            )
        )

    def shocked(self, shifts: tuple[float, ...]) -> ParYieldCurveRequest:
        """The quotes moved by bucket ``shifts`` (bp) at each maturity's curve time."""
        if not any(shifts):
            return self
        day_count, start = self.spec.time_day_count, self.curve_date
        return replace(
            self,
            quotes=tuple(
                ParYieldQuote(
                    q.tenor,
                    q.par_yield
                    + _shift_at(day_count.year_fraction(start, add_tenor(start, q.tenor)), shifts)
                    * 1e-4,
                )
                for q in self.quotes
            ),
        )

    def _quote_build(
        self, shifts: tuple[float, ...], rub: DiscountFactorCurve | None
    ) -> DiscountCurve:
        return bootstrap_par_yield_curve(self.shocked(shifts))

    def _quote_identity(self) -> object:
        return canonical_sha256(self)


def _frequency(spec: CurveSpec) -> int:
    frequency = spec.par_coupon_frequency
    if frequency not in (0, 2):
        raise InvalidTerms(f"{spec.spec_id}: unsupported par coupon frequency {frequency}")
    return frequency


def _years(start: date, end: date) -> float:
    return (end - start).days / 365.0


def _semiannual_value(curve: DiscountCurve, maturity: date, rate: float) -> float:
    """PV per unit face of a semiannual bond-equivalent par bond at ``rate``."""
    start = curve.curve_date
    if add_months(maturity, -6) <= start:
        return curve.discount_factor(maturity) * (1.0 + rate * _years(start, maturity))
    coupons: list[date] = []
    k = 0
    while True:
        day = add_months(maturity, -6 * k)
        if day <= start:
            regular_start = day
            break
        coupons.append(day)
        k += 1
    coupons.reverse()
    first = coupons[0]
    fraction = (first - start).days / (first - regular_start).days
    value = rate / 2.0 * fraction * curve.discount_factor(first)
    value += math.fsum(rate / 2.0 * curve.discount_factor(day) for day in coupons[1:])
    return value + curve.discount_factor(maturity)


def _annuity_continuous(curve: DiscountCurve, maturity: date) -> float:
    """integral of DF(t) dt from the curve date to ``maturity`` (ACT/365F years), exact on
    a log-linear (or flat-forward) curve: each segment contributes
    ``tau * (DF_a - DF_b) / ln(DF_a / DF_b)``."""
    start = curve.curve_date
    bounds = [start, *(d for d, _ in curve.nodes if start < d < maturity), maturity]
    total = []
    left = 1.0
    for a, b in pairwise(bounds):
        right = curve.discount_factor(b)
        tau = _years(a, b)
        ratio = math.log(left / right)
        total.append(tau * left if abs(ratio) < 1e-14 else tau * (left - right) / ratio)
        left = right
    return math.fsum(total)


def _bond_value(curve: DiscountCurve, maturity: date, rate: float, frequency: int) -> float:
    if frequency == 2:
        return _semiannual_value(curve, maturity, rate)
    return rate * _annuity_continuous(curve, maturity) + curve.discount_factor(maturity)


def bootstrap_par_yield_curve(request: ParYieldCurveRequest) -> DiscountCurve:
    """The proxy discount curve on which every quote's par bond prices at 100.

    The result is dated ``request.curve_date`` with model version
    ``par-proxy-logdf-v1`` and ACT/365F curve time; place it in the snapshot under the
    request's spec ID. Quotes that cannot be met by a positive discount factor are an
    ``InvalidTerms`` rejection.
    """
    if not isinstance(request, ParYieldCurveRequest):
        raise TypeError("request must be a ParYieldCurveRequest")
    spec = request.spec
    frequency = _frequency(spec)
    convention = CurveConvention(time_day_count=spec.time_day_count)
    nodes: list[tuple[date, float]] = []
    for maturity, quote in request.nodes():

        def residual(df: float, maturity: date = maturity, rate: float = quote.par_yield) -> float:
            trial = DiscountCurve(
                request.curve_date,
                (*nodes, (maturity, df)),
                PAR_YIELD_PROXY_MODEL_VERSION,
                convention=convention,
            )
            return _bond_value(trial, maturity, rate, frequency) - 1.0

        try:
            root = illinois(
                residual, 1e-12, 50.0, tolerance=_SOLVER_TOLERANCE, label="par-yield node"
            )
        except (ValueError, RuntimeError) as exc:
            raise InvalidTerms(
                f"par yield {quote.tenor} = {quote.par_yield} cannot be bootstrapped: {exc}"
            ) from None
        nodes.append((maturity, root.root))
    return DiscountCurve(
        request.curve_date, tuple(nodes), PAR_YIELD_PROXY_MODEL_VERSION, convention=convention
    )


def par_yield(curve: DiscountCurve, spec_id: str, maturity: date) -> float:
    """The par yield of ``curve`` at ``maturity`` on the convention of proxy ``spec_id``."""
    spec = get_curve_spec(spec_id)
    if spec.kind is not CurveKind.PAR_YIELD_PROXY:
        raise InvalidTerms(f"{spec_id} is not a par-yield proxy spec")
    if maturity <= curve.curve_date:
        raise InvalidTerms("par-yield maturity must be after the curve date")
    frequency = _frequency(spec)
    zero = _bond_value(curve, maturity, 0.0, frequency)
    one = _bond_value(curve, maturity, 1.0, frequency)
    return (1.0 - zero) / (one - zero)
