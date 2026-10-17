"""Callable and perpetual bonds under an explicit exercise assumption (package 9b).

The issuer's call schedule is the instrument's ``Offer`` entries of kind
``OfferKind.CALL``: a date and a redemption price in % of the face outstanding just
before that date. Core does not model the issuer's decision. Every value comes from a
named assumption:

- ``to_maturity``: no call is exercised. This is how MOEX yields call bonds without a
  call date it publishes, and how core valued them before 0.18 (the default);
- ``to_call(date)``: the issuer calls on that listed date;
- ``to_worst``: the redemption date (a call or maturity) with the lowest effective
  yield at the given price. It is resolved once, at the quoted price, and then held
  fixed: risk and scenarios do not switch the exercise date.

Yields are effective annual from settlement on the instrument's
``yield_convention`` time basis (default ACT/365F, as MOEX publishes
``CALLOPTIONYIELD`` and ``YIELDATWAP``). Durations are Macaulay and modified at that
yield, on the same basis.

Coupons must be known up to the redemption date. When they are not (a coupon reset
after the call, any coupon of a perpetual after its known periods), the caller passes
the assumed periods (``assumed_coupons``) explicitly; otherwise the scenario is
rejected. A call date must be a coupon payment date: core does not model a call in the
middle of a coupon period.

Option-adjusted spreads need a short-rate model and a lattice, which core does not
have: :func:`option_adjusted_spread` and an ``option_adjusted`` assumption raise
:class:`UnsupportedProduct`.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import date
from itertools import pairwise
from typing import Literal, NoReturn

from ._money import finite_money, positive_money
from .bond import YieldRequest, solve_effective_annual_yield
from .canonical import canonical_sha256
from .cashflows import Cashflow, CashflowKind
from .compatibility import bond_duration
from .conventions import YieldConvention
from .domain import (
    CouponPeriod,
    FixedCouponLeg,
    FloatingCouponLeg,
    Instrument,
    Offer,
    OfferKind,
    PrincipalLeg,
    RateAccrualCouponLeg,
    RateCouponPeriod,
    Redemption,
)
from .errors import InvalidTerms, UnsupportedProduct

__all__ = [
    "CALL_MODEL_VERSION",
    "CallAnalysis",
    "ExerciseAssumption",
    "ExercisedBond",
    "ExtensionRisk",
    "PriceToWorst",
    "RedemptionScenario",
    "ResolvedExercise",
    "ScenarioYield",
    "call_analysis",
    "call_schedule",
    "exercised_instrument",
    "extension_risk",
    "option_adjusted_spread",
    "perpetual_current_yield",
    "price_to_worst",
    "redemption_scenarios",
    "resolve_exercise",
    "yield_to_call",
    "yield_to_maturity",
    "yield_to_worst",
]

CALL_MODEL_VERSION = "rub-call-exercise-v1"

ExerciseKind = Literal["to_maturity", "to_call", "to_worst"]
Exercise = Literal["call", "maturity"]
AssumedCoupon = CouponPeriod | RateCouponPeriod

_OAS_REASON = (
    "option-adjusted valuation needs a short-rate model and a lattice, which core does "
    "not have; value callables under an explicit exercise assumption "
    "(to_maturity, to_call or to_worst)"
)


@dataclass(frozen=True)
class ExerciseAssumption:
    """How a callable or perpetual bond is assumed to be redeemed.

    ``assumed_coupons`` are coupon periods the caller assumes after the instrument's
    known periods (coupon reset terms). They must be of the leg's period type, follow
    the known periods without overlap, and are recorded with the assumption.
    """

    kind: ExerciseKind
    call_date: date | None = None
    assumed_coupons: tuple[AssumedCoupon, ...] = ()

    def __post_init__(self) -> None:
        if self.kind == "option_adjusted":  # type: ignore[comparison-overlap]
            raise UnsupportedProduct(_OAS_REASON)
        if self.kind not in ("to_maturity", "to_call", "to_worst"):
            raise InvalidTerms("exercise kind must be to_maturity, to_call or to_worst")
        if (self.kind == "to_call") != (self.call_date is not None):
            raise InvalidTerms("a to_call assumption needs its call date, and only it")
        object.__setattr__(self, "assumed_coupons", tuple(self.assumed_coupons))
        for period in self.assumed_coupons:
            if not isinstance(period, (CouponPeriod, RateCouponPeriod)):
                raise TypeError("assumed coupons must be CouponPeriod or RateCouponPeriod")
        if len({type(p) for p in self.assumed_coupons}) > 1:
            raise InvalidTerms("assumed coupons must all be of one period type")
        for left, right in pairwise(self.assumed_coupons):
            if right.accrual_start < left.accrual_end or right.payment_date <= left.payment_date:
                raise InvalidTerms("assumed coupons must be ordered and must not overlap")

    @classmethod
    def to_maturity(cls) -> ExerciseAssumption:
        return cls("to_maturity")

    @classmethod
    def to_call(
        cls, call_date: date, assumed_coupons: Sequence[AssumedCoupon] = ()
    ) -> ExerciseAssumption:
        return cls("to_call", call_date, tuple(assumed_coupons))

    @classmethod
    def to_worst(cls, assumed_coupons: Sequence[AssumedCoupon] = ()) -> ExerciseAssumption:
        return cls("to_worst", None, tuple(assumed_coupons))

    @property
    def identifier(self) -> str:
        """Stable label, e.g. ``exercise:to_call:2027-03-25``; assumed coupons by hash."""
        label = f"exercise:{self.kind}"
        if self.call_date is not None:
            label += f":{self.call_date.isoformat()}"
        if self.assumed_coupons:
            label += f":coupons-{canonical_sha256(list(self.assumed_coupons))[:12]}"
        return label


@dataclass(frozen=True)
class RedemptionScenario:
    """One way the bond can be redeemed: at a call (``price_pct`` of outstanding) or maturity."""

    exercise: Exercise
    redemption_date: date
    price_pct: float
    offer: Offer | None
    cashflows: tuple[Cashflow, ...]


@dataclass(frozen=True)
class ScenarioYield:
    """Effective annual yield and durations to one redemption date.

    ``yield_convention`` names the time basis (the instrument's; default ACT/365F).
    """

    exercise: Exercise
    redemption_date: date
    price_pct: float
    effective_yield: float
    macaulay_duration_years: float
    modified_duration_years: float
    yield_convention: str


@dataclass(frozen=True)
class CallAnalysis:
    """Yields to every call after settlement and to maturity, and the worst of them."""

    instrument_id: str
    settlement_date: date
    dirty_price_rub: float
    scenarios: tuple[ScenarioYield, ...]
    yield_to_maturity: ScenarioYield | None
    worst: ScenarioYield
    model_version: str = CALL_MODEL_VERSION


@dataclass(frozen=True)
class PriceToWorst:
    """Lowest price over the redemption scenarios at one effective annual yield."""

    effective_yield: float
    dirty_price_rub: float
    accrued_interest_rub: float
    clean_price_pct: float
    exercise: Exercise
    redemption_date: date


@dataclass(frozen=True)
class ExtensionRisk:
    """What changes if a call is not exercised and the bond runs to maturity.

    ``duration_extension_years`` is the Macaulay duration to maturity minus that to the
    call, each at its own yield for the same price. ``pv_change_if_extended_rub`` is the
    to-maturity flows valued at the yield to call minus the dirty price: the per-bond
    value change if the call is skipped and the yield stays. It is negative for a bond
    whose coupon is below the yield to call, positive above it.
    """

    call_date: date
    maturity_date: date
    yield_to_call: float
    yield_to_maturity: float
    macaulay_to_call_years: float
    macaulay_to_maturity_years: float
    modified_to_call_years: float
    modified_to_maturity_years: float
    duration_extension_years: float
    pv_change_if_extended_rub: float


@dataclass(frozen=True)
class ResolvedExercise:
    """An assumption resolved for one bond at one settlement (and price, for to_worst)."""

    assumption: ExerciseAssumption
    settlement_date: date
    exercise: Exercise
    redemption_date: date
    price_pct: float
    offer: Offer | None

    @property
    def identifier(self) -> str:
        return f"{self.assumption.identifier}@{self.exercise}:{self.redemption_date.isoformat()}"


@dataclass(frozen=True)
class ExercisedBond:
    """The deterministic bond implied by an exercise: no calls, redeemed on one date.

    ``instrument`` keeps the original ID and is valued like any fixed bond (portfolio,
    horizon, risk). ``source_sha256`` identifies the callable terms it came from.
    """

    instrument: Instrument
    resolved: ResolvedExercise
    source_sha256: str
    model_version: str = CALL_MODEL_VERSION


def option_adjusted_spread(*_: object, **__: object) -> NoReturn:
    """Always raises :class:`UnsupportedProduct`: core has no short-rate model (no OAS)."""
    raise UnsupportedProduct(_OAS_REASON)


def call_schedule(instrument: Instrument) -> tuple[Offer, ...]:
    """The issuer calls of ``instrument``, by date."""
    return tuple(o for o in instrument.offers if o.kind is OfferKind.CALL)


def perpetual_current_yield(coupon_rate: float, clean_price_pct: float) -> float:
    """Annual coupon rate over the clean price: MOEX's published yield of perpetuals.

    It is not a yield to call and carries no duration (MOEX publishes none).
    """
    rate = finite_money(coupon_rate, "coupon rate")
    if rate < 0.0:
        raise InvalidTerms("coupon rate cannot be negative")
    return rate / (positive_money(clean_price_pct, "clean price") / 100.0)


def _extended(instrument: Instrument, assumed: tuple[AssumedCoupon, ...]) -> Instrument:
    """The instrument with the assumed coupon periods appended to its leg."""
    leg = instrument.coupon_leg
    if isinstance(leg, FloatingCouponLeg):
        raise UnsupportedProduct("callable floaters are not supported (package 1 and 9)")
    if not assumed:
        return instrument
    if leg is None:
        raise InvalidTerms("a bond without a coupon leg cannot take assumed coupons")
    if isinstance(leg, FixedCouponLeg):
        fixed = [p for p in assumed if type(p) is CouponPeriod]
        if len(fixed) != len(assumed):
            raise InvalidTerms("a FixedCouponLeg takes assumed CouponPeriod entries")
        return replace(instrument, coupon_leg=FixedCouponLeg((*leg.periods, *fixed)))
    rated = [p for p in assumed if type(p) is RateCouponPeriod]
    if len(rated) != len(assumed):
        raise InvalidTerms("a RateAccrualCouponLeg takes assumed RateCouponPeriod entries")
    rate_periods = (*leg.periods, *rated)
    return replace(
        instrument,
        coupon_leg=RateAccrualCouponLeg(rate_periods, leg.accrual_basis),
    )


def _check_coupons(instrument: Instrument, settlement: date, redemption: date) -> None:
    """Coupons must be known, without gaps, up to a coupon date that is the redemption."""
    leg = instrument.coupon_leg
    if leg is None:
        return
    assert not isinstance(leg, FloatingCouponLeg)
    periods: Sequence[CouponPeriod | RateCouponPeriod] = leg.periods
    future = [p for p in periods if settlement < p.payment_date <= redemption]
    for left, right in pairwise(future):
        if right.accrual_start != left.accrual_end:
            raise InvalidTerms(
                f"coupon periods have a gap from {left.accrual_end} to {right.accrual_start}"
            )
    if any(p.payment_date == redemption for p in future):
        return
    if any(p.accrual_start < redemption < p.accrual_end for p in periods):
        raise InvalidTerms(
            f"redemption on {redemption} falls inside a coupon period; "
            "a mid-period call is not modelled"
        )
    raise InvalidTerms(
        f"coupons up to {redemption} are not known; pass the assumed coupons (reset terms)"
    )


def _scenario(
    instrument: Instrument, settlement: date, redemption: date, offer: Offer | None
) -> RedemptionScenario:
    _check_coupons(instrument, settlement, redemption)
    flows = instrument.cashflows(after=settlement, horizon=redemption, offer=offer)
    if not flows or flows[-1].payment_date != redemption or flows[-1].principal <= 0.0:
        raise InvalidTerms(f"no redemption remains on {redemption} after settlement")
    return RedemptionScenario(
        "maturity" if offer is None else "call",
        redemption,
        100.0 if offer is None else offer.price_pct,
        offer,
        flows,
    )


def redemption_scenarios(
    instrument: Instrument,
    settlement: date,
    *,
    assumed_coupons: Sequence[AssumedCoupon] = (),
) -> tuple[RedemptionScenario, ...]:
    """Every call after settlement (by date) and maturity (absent for a perpetual)."""
    bond = _extended(instrument, tuple(assumed_coupons))
    maturity = bond.maturity_date
    out: list[RedemptionScenario] = []
    for call in call_schedule(bond):
        if call.offer_date <= settlement:
            continue
        if maturity is not None and call.offer_date > maturity:
            raise InvalidTerms(f"call on {call.offer_date} is after maturity {maturity}")
        if maturity is not None and call.offer_date == maturity:
            continue  # a call on the maturity date is the maturity redemption
        out.append(_scenario(bond, settlement, call.offer_date, call))
    if maturity is not None:
        if maturity <= settlement:
            raise InvalidTerms("the bond matures on or before settlement")
        out.append(_scenario(bond, settlement, maturity, None))
    if not out:
        raise InvalidTerms("a perpetual bond with no call after settlement has no redemption")
    return tuple(out)


def _yield(
    scenario: RedemptionScenario, settlement: date, dirty: float, convention: YieldConvention
) -> ScenarioYield:
    solved = solve_effective_annual_yield(
        YieldRequest(settlement, dirty, scenario.cashflows, convention)
    )
    duration = bond_duration(
        convention=convention,
        annual_yield=solved.effective_annual_yield,
        settlement_date=settlement,
        cashflows=tuple((f.payment_date, f.amount) for f in scenario.cashflows),
    )
    return ScenarioYield(
        scenario.exercise,
        scenario.redemption_date,
        scenario.price_pct,
        solved.effective_annual_yield,
        duration.macaulay_years,
        duration.modified_years,
        convention.identifier,
    )


def _pv_at_yield(
    cashflows: Sequence[Cashflow],
    settlement: date,
    annual_yield: float,
    convention: YieldConvention,
) -> float:
    if annual_yield <= -1.0:
        raise InvalidTerms("effective yield must be above -100%")
    return math.fsum(
        f.amount
        / (1.0 + annual_yield) ** convention.day_count.year_fraction(settlement, f.payment_date)
        for f in cashflows
    )


def call_analysis(
    instrument: Instrument,
    settlement: date,
    dirty_price_rub: float,
    *,
    assumed_coupons: Sequence[AssumedCoupon] = (),
) -> CallAnalysis:
    """Yield to each call, to maturity and to worst at one dirty price (RUB per bond).

    The worst is the lowest effective yield; ties go to the earliest date.
    """
    dirty = positive_money(dirty_price_rub, "dirty price")
    scenarios = redemption_scenarios(instrument, settlement, assumed_coupons=assumed_coupons)
    yields = tuple(_yield(s, settlement, dirty, instrument.yield_convention) for s in scenarios)
    worst = min(yields, key=lambda y: (y.effective_yield, y.redemption_date))
    maturity = next((y for y in yields if y.exercise == "maturity"), None)
    return CallAnalysis(instrument.instrument_id, settlement, dirty, yields, maturity, worst)


def _pick(
    instrument: Instrument,
    settlement: date,
    redemption: date,
    assumed_coupons: Sequence[AssumedCoupon],
) -> RedemptionScenario:
    """The one scenario redeeming on ``redemption``; other dates are not checked."""
    if redemption <= settlement:
        raise InvalidTerms(f"no call or maturity on {redemption} after settlement")
    bond = _extended(instrument, tuple(assumed_coupons))
    maturity = bond.maturity_date
    if maturity is not None and redemption == maturity:
        return _scenario(bond, settlement, maturity, None)
    if maturity is not None and redemption > maturity:
        raise InvalidTerms(f"{redemption} is after maturity {maturity}")
    for call in call_schedule(bond):
        if call.offer_date == redemption:
            return _scenario(bond, settlement, redemption, call)
    raise InvalidTerms(f"no call or maturity on {redemption} after settlement")


def yield_to_call(
    instrument: Instrument,
    settlement: date,
    dirty_price_rub: float,
    call_date: date,
    *,
    assumed_coupons: Sequence[AssumedCoupon] = (),
) -> ScenarioYield:
    """Effective annual yield and durations if the issuer calls on ``call_date``."""
    if not any(c.offer_date == call_date for c in call_schedule(instrument)):
        raise InvalidTerms(f"{call_date} is not a call date of {instrument.instrument_id}")
    scenario = _pick(instrument, settlement, call_date, assumed_coupons)
    dirty = positive_money(dirty_price_rub, "dirty price")
    return _yield(scenario, settlement, dirty, instrument.yield_convention)


def yield_to_maturity(
    instrument: Instrument,
    settlement: date,
    dirty_price_rub: float,
    *,
    assumed_coupons: Sequence[AssumedCoupon] = (),
) -> ScenarioYield:
    """Effective annual yield and durations if no call is exercised."""
    maturity = instrument.maturity_date
    if maturity is None:
        raise UnsupportedProduct("a perpetual bond has no maturity; name a call date")
    bond = _extended(instrument, tuple(assumed_coupons))
    scenario = _scenario(bond, settlement, maturity, None)
    dirty = positive_money(dirty_price_rub, "dirty price")
    return _yield(scenario, settlement, dirty, instrument.yield_convention)


def yield_to_worst(
    instrument: Instrument,
    settlement: date,
    dirty_price_rub: float,
    *,
    assumed_coupons: Sequence[AssumedCoupon] = (),
) -> ScenarioYield:
    """The lowest yield over every call and maturity (see :func:`call_analysis`)."""
    if instrument.is_perpetual:
        raise UnsupportedProduct(_PERPETUAL_WORST)
    return call_analysis(
        instrument, settlement, dirty_price_rub, assumed_coupons=assumed_coupons
    ).worst


_PERPETUAL_WORST = (
    "the worst case of a perpetual is never being called, which has no yield to a date; "
    "name the call date (to_call)"
)


def price_to_worst(
    instrument: Instrument,
    settlement: date,
    effective_yield: float,
    *,
    assumed_coupons: Sequence[AssumedCoupon] = (),
) -> PriceToWorst:
    """The lowest dirty price over every call and maturity at one effective yield.

    At the yield to worst of a price it returns that price (price and yield are
    inverse on the worst scenario, and every other scenario prices higher).
    """
    if instrument.is_perpetual:
        raise UnsupportedProduct(_PERPETUAL_WORST)
    y = finite_money(effective_yield, "effective yield")
    scenarios = redemption_scenarios(instrument, settlement, assumed_coupons=assumed_coupons)
    priced = [
        (_pv_at_yield(s.cashflows, settlement, y, instrument.yield_convention), s)
        for s in scenarios
    ]
    dirty, worst = min(priced, key=lambda item: (item[0], item[1].redemption_date))
    accrued = instrument.accrued(settlement)
    nominal = instrument.outstanding_face(settlement)
    return PriceToWorst(
        y,
        dirty,
        accrued,
        (dirty - accrued) / nominal * 100.0,
        worst.exercise,
        worst.redemption_date,
    )


def extension_risk(
    instrument: Instrument,
    settlement: date,
    dirty_price_rub: float,
    call_date: date,
    *,
    assumed_coupons: Sequence[AssumedCoupon] = (),
) -> ExtensionRisk:
    """Duration and value change if the call on ``call_date`` is not exercised."""
    to_call = yield_to_call(
        instrument, settlement, dirty_price_rub, call_date, assumed_coupons=assumed_coupons
    )
    to_maturity = yield_to_maturity(
        instrument, settlement, dirty_price_rub, assumed_coupons=assumed_coupons
    )
    bond = _extended(instrument, tuple(assumed_coupons))
    maturity = _scenario(bond, settlement, to_maturity.redemption_date, None)
    extended = _pv_at_yield(
        maturity.cashflows, settlement, to_call.effective_yield, instrument.yield_convention
    )
    return ExtensionRisk(
        call_date,
        to_maturity.redemption_date,
        to_call.effective_yield,
        to_maturity.effective_yield,
        to_call.macaulay_duration_years,
        to_maturity.macaulay_duration_years,
        to_call.modified_duration_years,
        to_maturity.modified_duration_years,
        to_maturity.macaulay_duration_years - to_call.macaulay_duration_years,
        finite_money(extended - positive_money(dirty_price_rub, "dirty price"), "extension PV"),
    )


def resolve_exercise(
    instrument: Instrument,
    settlement: date,
    assumption: ExerciseAssumption,
    *,
    dirty_price_rub: float | None = None,
) -> ResolvedExercise:
    """The redemption date and price an assumption implies for this bond.

    ``to_worst`` needs the dirty price. A perpetual needs ``to_call``: it has no
    maturity, and its worst case (never called) has no redemption date.
    """
    if not isinstance(assumption, ExerciseAssumption):
        raise TypeError("assumption must be an ExerciseAssumption")
    assumed = assumption.assumed_coupons
    if assumption.kind == "to_maturity":
        maturity = instrument.maturity_date
        if maturity is None:
            raise UnsupportedProduct("a perpetual bond has no maturity; name a call date")
        scenario = _scenario(_extended(instrument, assumed), settlement, maturity, None)
    elif assumption.kind == "to_call":
        assert assumption.call_date is not None
        if not any(c.offer_date == assumption.call_date for c in call_schedule(instrument)):
            raise InvalidTerms(
                f"{assumption.call_date} is not a call date of {instrument.instrument_id}"
            )
        scenario = _pick(instrument, settlement, assumption.call_date, assumed)
    else:
        if instrument.is_perpetual:
            raise UnsupportedProduct(_PERPETUAL_WORST)
        if dirty_price_rub is None:
            raise InvalidTerms("a to_worst assumption needs the dirty price")
        worst = call_analysis(
            instrument, settlement, dirty_price_rub, assumed_coupons=assumed
        ).worst
        scenario = _pick(instrument, settlement, worst.redemption_date, assumed)
    return ResolvedExercise(
        assumption,
        settlement,
        scenario.exercise,
        scenario.redemption_date,
        scenario.price_pct,
        scenario.offer,
    )


def exercised_instrument(instrument: Instrument, resolved: ResolvedExercise) -> ExercisedBond:
    """The fixed bond the resolved exercise implies: calls removed, redeemed on its date.

    Coupons (known and assumed) up to the redemption date are kept, earlier
    amortizations are kept, and the face outstanding is redeemed on the date. Puts
    before it stay. A call away from par cannot be written as such a bond (its
    redemptions would not sum to the face), so it is rejected here; the yield
    functions above handle any call price.
    """
    bond = _extended(instrument, resolved.assumption.assumed_coupons)
    redemption = resolved.redemption_date
    _check_coupons(bond, resolved.settlement_date, redemption)
    if resolved.exercise == "maturity":
        # Only the calls go: the legs stay exactly as given, so a bond without calls
        # (or assumed coupons) is returned equal to the input, with the same hash.
        uncalled = replace(bond, offers=tuple(o for o in bond.offers if o.kind is OfferKind.PUT))
        return ExercisedBond(uncalled, resolved, canonical_sha256(instrument))
    if not math.isclose(resolved.price_pct, 100.0, rel_tol=0.0, abs_tol=1e-12):
        raise UnsupportedProduct(
            f"a call at {resolved.price_pct}% of face cannot be valued as an exercised "
            "bond; use the yield-to-call analytics"
        )
    leg = bond.coupon_leg
    new_leg: FixedCouponLeg | RateAccrualCouponLeg | None
    if isinstance(leg, FixedCouponLeg):
        new_leg = FixedCouponLeg(tuple(p for p in leg.periods if p.payment_date <= redemption))
    elif isinstance(leg, RateAccrualCouponLeg):
        new_leg = RateAccrualCouponLeg(
            tuple(p for p in leg.periods if p.payment_date <= redemption), leg.accrual_basis
        )
    else:
        new_leg = None
    earlier = tuple(r for r in bond.principal_leg.redemptions if r.payment_date < redemption)
    remaining = bond.initial_face - math.fsum(r.amount for r in earlier)
    principal = PrincipalLeg((*earlier, Redemption(redemption, remaining, CashflowKind.MATURITY)))
    offers = tuple(o for o in bond.offers if o.kind is OfferKind.PUT and o.offer_date < redemption)
    exercised = replace(bond, coupon_leg=new_leg, principal_leg=principal, offers=offers)
    return ExercisedBond(exercised, resolved, canonical_sha256(instrument))
