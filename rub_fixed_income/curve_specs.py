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

Package 3, phase 2 (0.36.0): a CNY bond is also discounted on the built
``cny_ruonia_xccy_v1`` curve (the :class:`DiscountCurve` of
:func:`~rub_fixed_income.xccy.bootstrap_xccy_curve`), and a USD or EUR bond on a
government par-yield **proxy** (kind ``PAR_YIELD_PROXY``, ``proxy=True``) built by
:func:`~rub_fixed_income.proxy_curves.bootstrap_par_yield_curve` from published par yields:
``usd_ust_par_proxy_v1`` (US Treasury daily par yield curve, semiannual bond-equivalent)
and ``eur_ecb_aaa_par_proxy_v1`` (ECB euro area AAA par yields, continuously compounded).
A proxy is not an OIS curve: results name it (``CurveUse.proxy``) so a reader can say so.

``rub_key_rate_projection_v1`` is a projection-only curve for key-rate floaters: the
discount factors of :func:`~rub_fixed_income.floating.key_rate_projection_curve`, whose
overnight forwards are a key-rate path. It never discounts a bond.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType

from .conventions import CurveConvention, DayCount, OisSwapConvention
from .dates import BusinessCalendar
from .errors import InvalidTerms, UnknownCurveSpec
from .floating import KEY_RATE_PROJECTION_MODEL_VERSION
from .fx_forwards import FX_IMPLIED_CURVE_MODEL_VERSION
from .kbd import KBD_MODEL_VERSION
from .key_rate import CONVENTION_ID as KEY_RATE_SWAP_CONVENTION_ID

__all__ = [
    "CURVE_SPECS",
    "PAR_YIELD_PROXY_MODEL_VERSION",
    "CurveKind",
    "CurveSpec",
    "foreign_discount_spec_id",
    "fx_implied_spec_id",
    "get_curve_spec",
    "ois_curve_convention",
]


class CurveKind(Enum):
    KBD_PARAMETRIC = "kbd_parametric"
    OIS_BOOTSTRAP = "ois_bootstrap"
    KEY_RATE_SWAP = "key_rate_swap"
    XCCY_BOOTSTRAP = "xccy_bootstrap"
    DISCOUNT_FACTORS = "discount_factors"
    KEY_RATE_PROJECTION = "key_rate_projection"
    REAL_DISCOUNT_FACTORS = "real_discount_factors"
    PAR_YIELD_PROXY = "par_yield_proxy"
    FX_IMPLIED = "fx_implied"


@dataclass(frozen=True)
class CurveSpec:
    """A registered curve spec.

    ``proxy`` marks a curve that stands in for the currency's discount curve (a
    government par-yield curve, not OIS). ``par_coupon_frequency`` is the coupon
    frequency of a par-yield spec's quotes (2: semiannual; 0: paid continuously).
    """

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
    proxy: bool = False
    par_coupon_frequency: int | None = None
    # Unit of a bucket shock of the built DiscountCurve (``CurveConvention.zero_shock``).
    zero_shock: str = "continuous"


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
        spec_id="rub_ofz_kbd_v2",
        kind=CurveKind.KBD_PARAMETRIC,
        currency="RUB",
        time_day_count=DayCount.ACT_365_FIXED,
        interpolation="kbd-formula; materialised log-linear-df (daily to 2Y, weekly to 5Y, "
        "monthly to 30Y), flat-forward beyond",
        model_version=KBD_MODEL_VERSION,
        description="The KBD of rub_ofz_kbd_v1 as a risk curve: bucket shocks move the "
        "effective annual zero yields MOEX publishes (annual_zero), the unit that sums with "
        "par RUONIA OIS risk",
        zero_shock="annual",
    ),
    CurveSpec(
        spec_id="rub_ofz_kbd_v3",
        kind=CurveKind.KBD_PARAMETRIC,
        currency="RUB",
        time_day_count=DayCount.ACT_365_FIXED,
        interpolation="kbd-formula; materialised log-linear-df (daily to 2Y, weekly to 5Y, "
        "monthly to 30Y), flat-forward beyond",
        model_version=KBD_MODEL_VERSION,
        description="The KBD of rub_ofz_kbd_v1 as a par-quote risk curve: a bucket shock moves "
        "the par yield of an annual-coupon instrument of 1Y-20Y on the KBD (annual_par, 1M-6M "
        "merged into 1Y), the factor and unit of par RUONIA OIS quotes",
        zero_shock="annual_par",
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
    CurveSpec(
        spec_id="rub_key_rate_projection_v1",
        kind=CurveKind.KEY_RATE_PROJECTION,
        currency="RUB",
        time_day_count=DayCount.ACT_365_FIXED,
        interpolation="log-linear-df on business- or calendar-day nodes, flat-forward beyond",
        model_version=KEY_RATE_PROJECTION_MODEL_VERSION,
        description="Key-rate projection curve for floaters: DiscountCurve from "
        "key_rate_projection_curve, one node per observation day with "
        "DF(next) = DF(day) / (1 + key rate in force * tau). Projection only, never a "
        "discount curve; risk shifts its continuous zero rates",
    ),
)

_DISCOUNT_SOURCES = {
    "CNY": "the hub's CNY quote sets",
    "USD": "SOFR data",
    "EUR": "ESTR data",
}


def _discount_factors_spec(currency: str, source: str) -> CurveSpec:
    return CurveSpec(
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


def _fx_implied_spec(currency: str) -> CurveSpec:
    return CurveSpec(
        spec_id=f"{currency.lower()}_fx_implied_v1",
        kind=CurveKind.FX_IMPLIED,
        currency=currency,
        time_day_count=DayCount.ACT_365_FIXED,
        interpolation="log-linear-df between the outright dates, flat-forward extrapolation",
        model_version=FX_IMPLIED_CURVE_MODEL_VERSION,
        description=f"{currency} discount curve under RUB collateral implied from {currency}/RUB "
        "FX swap points and outrights by covered parity on a RUB curve "
        "(fx_forwards.implied_foreign_curve): DF(T)/DF(s) = (F(T)/S) * DF_rub(T)/DF_rub(s) "
        "at every outright date, anchored at DF = 1 on the curve date. Held as the built "
        "DiscountCurve, or as an FxImpliedCurveRequest (quotes, the RUB curve ID) that "
        "core builds on the snapshot FX spot. The FX basis on it is zero by construction; "
        "risk shifts its continuous zero rates",
    )


_FOREIGN_SPECS = tuple(
    _discount_factors_spec(currency, source) for currency, source in _DISCOUNT_SOURCES.items()
)
_GENERIC_SPEC = re.compile(r"^([a-z]{3})_(discount_factors|fx_implied)_v1$")


def _currency_code(currency: str) -> str:
    if (
        not isinstance(currency, str)
        or len(currency) != 3
        or not currency.isascii()
        or not currency.isalpha()
        or not currency.isupper()
    ):
        raise InvalidTerms("currency must be a three-letter upper-case code such as CHF")
    if currency == "RUB":
        raise InvalidTerms("RUB has no foreign curve spec")
    return currency


def foreign_discount_spec_id(currency: str) -> str:
    """``<ccy>_discount_factors_v1``: caller-built discount factors of ``currency``."""
    return f"{_currency_code(currency).lower()}_discount_factors_v1"


def fx_implied_spec_id(currency: str) -> str:
    """``<ccy>_fx_implied_v1``: the curve of ``currency`` implied from FX swaps against RUB."""
    return f"{_currency_code(currency).lower()}_fx_implied_v1"


def _generic_spec(spec_id: str) -> CurveSpec | None:
    match = _GENERIC_SPEC.match(spec_id) if isinstance(spec_id, str) else None
    if match is None or match.group(1) == "rub":
        return None
    currency = match.group(1).upper()
    if match.group(2) == "fx_implied":
        return _fx_implied_spec(currency)
    return _discount_factors_spec(currency, "the hub's data")


# Package 7: the CNY discount curve under RUB collateral, bootstrapped from MOEX XCS
# CNYRUB par quotes (``xccy.bootstrap_xccy_curve``, ``xccy.xcs_cnyrub_convention``).
_XCCY_SPECS = (
    CurveSpec(
        spec_id="cny_ruonia_xccy_v1",
        kind=CurveKind.XCCY_BOOTSTRAP,
        currency="CNY",
        time_day_count=DayCount.ACT_365_FIXED,
        interpolation="log-linear-df, flat-forward extrapolation",
        model_version="xccy-cny-logdf-v1",
        description="MOEX XCS CNYRUB standard swap: trade date T+0, start TOM, end start + "
        "tenor, MODFOL, CNY fixed vs RUB RUONIA-OIS-COMPOUND flat, single period up to 1Y and "
        "annual beyond with a short initial stub, Act/Act ISDA both legs, +1 business-day "
        "payment lag, notional at the trade date's NCC central rate, initial exchange on the "
        "start and final exchange on the end date; quote = CNY fixed par rate "
        "(CNY-XCS-CNYRUO); CNY discount curve under RUB collateral. Built "
        "(DiscountCurve), it also discounts CNY bonds (0.36.0)",
        fixed_leg_frequency_months=12,
        fixed_leg_day_count=DayCount.ACT_ACT_ISDA,
        spot_lag_business_days=1,
        payment_lag_business_days=1,
        business_day_convention="MODIFIED_FOLLOWING",
        float_averaging="RUB-RUONIA-OIS-COMPOUND (daily compounded, no lookback)",
        float_leg_day_count=DayCount.ACT_ACT_ISDA,
        stub="short_initial",
        calendars=("SPFI", "MOSCOW", "BEIJING", "RUONIA"),
        sources=(
            (
                "MOEX, Методика формирования ценовых кривых СПФИ, 2024-12-25, Appendix 1, "
                "section 3, XCS CNYRUB; https://www.moex.com/ru/documents/27002"
            ),
            (
                "NFA, Изменения № 1 в Стандартные условия срочных сделок на процентные "
                "ставки и сделок свопцион 2011 г., 2021-08-17, 8.4(б) RUB-RUONIA-OIS-COMPOUND"
            ),
            (
                "NFA/NAUFOR/ARB, Стандартные условия срочных сделок на процентные ставки, "
                "сделок валютно-процентный своп и сделок свопцион 2011, 6.1-6.4 (exchanges)"
            ),
        ),
    ),
)

# Package 3, phase 2: USD / EUR government par-yield proxies (no SOFR / ESTR OIS data).
PAR_YIELD_PROXY_MODEL_VERSION = "par-proxy-logdf-v1"
_PROXY_SPECS = (
    CurveSpec(
        spec_id="usd_ust_par_proxy_v1",
        kind=CurveKind.PAR_YIELD_PROXY,
        currency="USD",
        time_day_count=DayCount.ACT_365_FIXED,
        interpolation="log-linear-df between the quote maturities, flat-forward extrapolation",
        model_version=PAR_YIELD_PROXY_MODEL_VERSION,
        description="PROXY, not OIS: USD discount curve bootstrapped from the US Treasury "
        "daily par yield curve. Quotes are bond-equivalent par yields of securities paying "
        "semiannual coupons: a tenor up to 6M is one period (DF = 1 / (1 + y * days / 365)), "
        "a longer one a par bond with coupons y / 2 on a schedule rolled back from the "
        "maturity in 6-month steps (a short first period pays its fraction); tenors from the "
        "curve date, unadjusted, no settlement lag. Government credit and liquidity, not "
        "SOFR",
        fixed_leg_frequency_months=6,
        spot_lag_business_days=0,
        business_day_convention="UNADJUSTED",
        stub="short_initial",
        sources=(
            (
                "US Treasury, Interest Rates - Frequently Asked Questions: the par yield "
                "curve is based on securities that pay interest on a semiannual basis; all "
                "yield curve rates are bond-equivalent yields; https://home.treasury.gov/"
                "policy-issues/financing-the-government/interest-rate-statistics/"
                "interest-rates-frequently-asked-questions"
            ),
        ),
        proxy=True,
        par_coupon_frequency=2,
    ),
    CurveSpec(
        spec_id="eur_ecb_aaa_par_proxy_v1",
        kind=CurveKind.PAR_YIELD_PROXY,
        currency="EUR",
        time_day_count=DayCount.ACT_365_FIXED,
        interpolation="log-linear-df between the quote maturities, flat-forward extrapolation",
        model_version=PAR_YIELD_PROXY_MODEL_VERSION,
        description="PROXY, not OIS: EUR discount curve bootstrapped from the ECB euro area "
        "yield curve of AAA-rated central government bonds, par yields. The ECB's par yield "
        "is continuously compounded, y(T) = (1 - DF(T)) / integral_0^T DF(t) dt; core solves "
        "that relation exactly on log-linear discount factors; tenors from the curve date, "
        "unadjusted, no settlement lag. Government credit, not ESTR",
        spot_lag_business_days=0,
        business_day_convention="UNADJUSTED",
        sources=(
            (
                "ECB, Euro area yield curves, Technical notes (2025-12-09): par yield "
                "y_par(TTM) = C (1 - disc(TTM)) / integral_0^TTM disc(t) dt, continuously "
                "compounded; https://www.ecb.europa.eu/stats/financial_markets_and_"
                "interest_rates/euro_area_yield_curves/shared/pdf/technical_notes.pdf"
            ),
        ),
        proxy=True,
        par_coupon_frequency=0,
    ),
)

_REAL_SPECS = (
    CurveSpec(
        spec_id="rub_real_discount_factors_v1",
        kind=CurveKind.REAL_DISCOUNT_FACTORS,
        currency="RUB",
        time_day_count=DayCount.ACT_365_FIXED,
        interpolation="log-linear-df between the caller's nodes, flat-forward extrapolation",
        model_version="ois-logdf-v1",
        description="RUB real (CPI-adjusted) discount factors built upstream and passed as a "
        "DiscountCurve dated the snapshot business date, ACT/365F curve time. Discounts the "
        "real cash flows of inflation-linked bonds (flows at the settlement-date indexed "
        "nominal); risk shifts its continuous zero rates as REAL|<curve_id>|<bucket>. "
        "Core does not build or certify it, and never discounts a nominal bond on it",
    ),
)

CURVE_SPECS = MappingProxyType(
    {
        spec.spec_id: spec
        for spec in (*_SPECS, *_FOREIGN_SPECS, *_XCCY_SPECS, *_REAL_SPECS, *_PROXY_SPECS)
    }
)


def get_curve_spec(spec_id: str) -> CurveSpec:
    """The registered spec, or a generated per-currency foreign spec (see the module doc)."""
    spec = CURVE_SPECS.get(spec_id)
    if spec is None:
        spec = _generic_spec(spec_id)
    if spec is None:
        raise UnknownCurveSpec(f"unknown curve spec {spec_id!r}")
    return spec


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


def _check_ois_spec_convention(curve_id: str, spec_id: str, convention: CurveConvention) -> None:
    """A spot-OIS convention belongs to ``rub_ruonia_ois_moex_v2`` and nothing else."""
    ois = convention.ois_swap
    if spec_id == "rub_ruonia_ois_legacy_v1":
        if ois is not None:
            raise InvalidTerms(f"curve {curve_id}: {spec_id} has no spot-starting OIS convention")
        return
    if ois is None or convention != ois_curve_convention(spec_id, ois.calendar):
        raise InvalidTerms(f"curve {curve_id}: convention does not match spec {spec_id}")
