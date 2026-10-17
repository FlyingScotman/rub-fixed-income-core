"""Versioned curve-spec registry keyed by core-owned spec IDs (roadmap 0.5).

A spec fixes how a curve of a type is built and read: its time basis, interpolation
and, for swap curves, the quote conventions. The backend maps each market-data hub
``dataset`` code to one of these IDs; core never sees hub codes (ADR-003/004). A spec
ID is never reused for different conventions: a change gets a new version.

``rub_ruonia_ois_legacy_v1`` names the bootstrap of 0.2.1-0.11.0 exactly and is kept for
parity. ``rub_ruonia_ois_moex_v2`` is the MOEX OIS RUONIA standard swap: TOM start,
modified following, a single period up to 1Y and annual periods beyond with a short
initial stub, Act/Act ISDA on both legs, +1 business-day payment lag, floating leg
``RUB-RUONIA-OIS-COMPOUND`` without lookback. Sources (``docs/CONVENTIONS.md``):

- MOEX, "Методика формирования ценовых кривых СПФИ", version of 25 December 2024,
  Appendix 1 "Описание спецификаций", column "OIS RUONIA"
  (https://www.moex.com/ru/documents/27002);
- NFA, "Изменения № 1 в Стандартные условия срочных сделок на процентные ставки и
  сделок свопцион 2011 г." of 17 August 2021, clause 8.4(б) (the index formula), with the
  2011 Standard Terms, clauses 2.2 (interest periods), 7.2-7.3 (amounts) and 7.4(е)
  (Act/Act).

Build the matching :class:`CurveConvention` with :func:`ois_curve_convention`.

Foreign discount curves (package 3) are registered as caller-built discount factors,
one spec per currency (``cny_discount_factors_v1``, ``usd_discount_factors_v1``,
``eur_discount_factors_v1``). The backend builds them from the hub's data (CNY quote
sets, SOFR, ESTR) and passes a :class:`~rub_fixed_income.discount.DiscountCurve`; core
only reads, interpolates and shifts it. Their quote conventions are the builder's, so
core registers no foreign bootstrap.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType

from .conventions import CurveConvention, DayCount, OisSwapConvention
from .dates import BusinessCalendar
from .errors import InvalidTerms, UnknownCurveSpec
from .kbd import KBD_MODEL_VERSION
from .key_rate import CONVENTION_ID as KEY_RATE_SWAP_CONVENTION_ID

__all__ = ["CURVE_SPECS", "CurveKind", "CurveSpec", "get_curve_spec", "ois_curve_convention"]


class CurveKind(Enum):
    KBD_PARAMETRIC = "kbd_parametric"
    OIS_BOOTSTRAP = "ois_bootstrap"
    KEY_RATE_SWAP = "key_rate_swap"
    DISCOUNT_FACTORS = "discount_factors"


@dataclass(frozen=True)
class CurveSpec:
    spec_id: str
    kind: CurveKind
    currency: str
    time_day_count: DayCount
    interpolation: str
    model_version: str
    description: str
    fixed_leg_frequency_months: int | None = None
    fixed_leg_day_count: DayCount | None = None
    spot_lag_business_days: int | None = None
    payment_lag_business_days: int | None = None
    business_day_convention: str | None = None
    float_averaging: str | None = None
    float_leg_day_count: DayCount | None = None
    stub: str | None = None
    calendars: tuple[str, ...] = ()
    sources: tuple[str, ...] = ()


_SPECS = (
    CurveSpec(
        spec_id="rub_ofz_kbd_v1",
        kind=CurveKind.KBD_PARAMETRIC,
        currency="RUB",
        time_day_count=DayCount.ACT_365_FIXED,
        interpolation="kbd-formula; materialised log-linear-df (daily to 2Y, weekly to 5Y, "
        "monthly to 30Y), flat-forward beyond",
        model_version=KBD_MODEL_VERSION,
        description="MOEX zero-coupon OFZ yield curve (KBD/G-curve) from B1..B3, T1, G1..G9; "
        "continuous zero rate, effective annual published yields",
    ),
    CurveSpec(
        spec_id="rub_ruonia_ois_legacy_v1",
        kind=CurveKind.OIS_BOOTSTRAP,
        currency="RUB",
        time_day_count=DayCount.ACT_365_FIXED,
        interpolation="log-linear-df, flat-forward extrapolation",
        model_version="ois-logdf-v1",
        description="RUONIA OIS par bootstrap as in 0.2.1-0.11.0: fixed leg from the curve "
        "date, annual unadjusted roll with a short end stub, ACT/365F, no spot or payment lag, "
        "single curve. Not the exchange convention (audit F08)",
        fixed_leg_frequency_months=12,
        fixed_leg_day_count=DayCount.ACT_365_FIXED,
        spot_lag_business_days=0,
        payment_lag_business_days=0,
        business_day_convention="UNADJUSTED",
        float_averaging="compounded (single-curve telescoped)",
    ),
    CurveSpec(
        spec_id="rub_ruonia_ois_moex_v2",
        kind=CurveKind.OIS_BOOTSTRAP,
        currency="RUB",
        time_day_count=DayCount.ACT_365_FIXED,
        interpolation="log-linear-df, flat-forward extrapolation",
        model_version="ois-logdf-v1",
        description="MOEX OIS RUONIA standard swap: trade date T+0, start TOM, end start + "
        "tenor, MODFOL start/end/periods, single period up to 1Y and annual beyond with a "
        "short initial stub, Act/Act ISDA fixed and floating, +1 business-day payment lag on "
        "both legs, RUB-RUONIA-OIS-COMPOUND with no fixing offset or lookback; single curve",
        fixed_leg_frequency_months=12,
        fixed_leg_day_count=DayCount.ACT_ACT_ISDA,
        spot_lag_business_days=1,
        payment_lag_business_days=1,
        business_day_convention="MODIFIED_FOLLOWING",
        float_averaging="RUB-RUONIA-OIS-COMPOUND (daily compounded, single-curve telescoped)",
        float_leg_day_count=DayCount.ACT_ACT_ISDA,
        stub="short_initial",
        calendars=("SPFI", "MOSCOW", "RUONIA"),
        sources=(
            (
                "MOEX, Методика формирования ценовых кривых СПФИ, 2024-12-25, Appendix 1, "
                "OIS RUONIA; https://www.moex.com/ru/documents/27002"
            ),
            (
                "NFA, Изменения № 1 в Стандартные условия срочных сделок на процентные "
                "ставки и сделок свопцион 2011 г., 2021-08-17, 8.4(б) RUB-RUONIA-OIS-COMPOUND"
            ),
        ),
    ),
    CurveSpec(
        spec_id="rub_key_rate_swap_v1",
        kind=CurveKind.KEY_RATE_SWAP,
        currency="RUB",
        time_day_count=DayCount.ACT_ACT_ISDA,
        interpolation="piecewise-constant forward key-rate average between pillars",
        model_version=KEY_RATE_SWAP_CONVENTION_ID,
        description="MOEX IRS KEYRATE: TOM start, modified following, ACT/ACT ISDA, annual "
        "fixed (single payment up to 1Y) vs quarterly KEYRATE-AVERAGE, on an external "
        "discount curve",
        fixed_leg_frequency_months=12,
        fixed_leg_day_count=DayCount.ACT_ACT_ISDA,
        spot_lag_business_days=1,
        payment_lag_business_days=0,
        business_day_convention="MODIFIED_FOLLOWING",
        float_averaging="arithmetic key-rate average",
    ),
)

_DISCOUNT_SOURCES = {
    "CNY": "the hub's CNY quote sets",
    "USD": "SOFR data",
    "EUR": "ESTR data",
}
_FOREIGN_SPECS = tuple(
    CurveSpec(
        spec_id=f"{currency.lower()}_discount_factors_v1",
        kind=CurveKind.DISCOUNT_FACTORS,
        currency=currency,
        time_day_count=DayCount.ACT_365_FIXED,
        interpolation="log-linear-df between the caller's nodes, flat-forward extrapolation",
        model_version="ois-logdf-v1",
        description=f"{currency} discount factors built upstream (by the backend from "
        f"{source}) and passed as a DiscountCurve dated the snapshot business date, "
        "ACT/365F curve time. Core does not bootstrap or certify it; risk shifts its "
        "continuous zero rates",
    )
    for currency, source in _DISCOUNT_SOURCES.items()
)

CURVE_SPECS = MappingProxyType({spec.spec_id: spec for spec in (*_SPECS, *_FOREIGN_SPECS)})


def get_curve_spec(spec_id: str) -> CurveSpec:
    try:
        return CURVE_SPECS[spec_id]
    except KeyError:
        raise UnknownCurveSpec(f"unknown curve spec {spec_id!r}") from None


def ois_curve_convention(spec_id: str, calendar: BusinessCalendar | None = None) -> CurveConvention:
    """The :class:`CurveConvention` of an OIS bootstrap spec.

    ``rub_ruonia_ois_legacy_v1`` takes no calendar. ``rub_ruonia_ois_moex_v2`` needs the
    caller's joint business-day calendar (SPFI, MOSCOW and RUONIA days, built from
    official sources).
    """
    spec = get_curve_spec(spec_id)
    if spec.kind is not CurveKind.OIS_BOOTSTRAP:
        raise InvalidTerms(f"{spec_id} is not an OIS bootstrap spec")
    if spec_id == "rub_ruonia_ois_legacy_v1":
        if calendar is not None:
            raise InvalidTerms(f"{spec_id} has no calendar")
        return CurveConvention()
    if calendar is None:
        raise InvalidTerms(f"{spec_id} needs a business-day calendar")
    return CurveConvention(
        time_day_count=spec.time_day_count,
        ois_swap=OisSwapConvention(
            calendar,
            spot_lag_business_days=1,
            payment_lag_business_days=1,
            single_period_max_months=12,
            frequency_months=12,
            stub="short_initial",
            fixed_day_count=DayCount.ACT_ACT_ISDA,
            float_day_count=DayCount.ACT_ACT_ISDA,
        ),
    )
