"""Versioned curve-spec registry keyed by core-owned spec IDs (roadmap 0.5, minimal).

A spec fixes how a curve of a type is built and read: its time basis, interpolation
and, for swap curves, the quote conventions. The backend maps each market-data hub
``dataset`` code to one of these IDs; core never sees hub codes (ADR-003/004). A spec
ID is never reused for different conventions: a change gets a new version.

0.11.0 registers the curves the M1 slice and the existing bootstraps need. The
exchange-conformant RUONIA OIS spec (``rub_ruonia_ois_moex_v2``) is not registered: the
audit's summary (F08: TOM start, modified following, ACT/ACT with a stub, +1 business-day
payment lag) leaves the fixed-leg frequency and float compounding open, and the MOEX
specification text was not available to confirm it (BACKLOG #37). Until then
``rub_ruonia_ois_legacy_v1`` names today's bootstrap exactly.

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

from .conventions import DayCount
from .errors import UnknownCurveSpec
from .kbd import KBD_MODEL_VERSION
from .key_rate import CONVENTION_ID as KEY_RATE_SWAP_CONVENTION_ID

__all__ = ["CURVE_SPECS", "CurveKind", "CurveSpec", "get_curve_spec"]


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
