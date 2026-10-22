"""CNY / USD / EUR bonds on their own currency's curve (package 3, phase 2; 0.34.0).

- CNY bonds on the built XCCY curve ``cny_ruonia_xccy_v1``;
- USD / EUR bonds on government par-yield proxies (``proxy_curves``), flagged as proxies;
- explicit same-currency fallbacks when a foreign curve is absent, never a RUB curve.
"""

from __future__ import annotations

import json
import math
from dataclasses import replace
from datetime import date

import pytest
from test_non_rub_bonds import (
    BUSINESS_DATE,
    FX,
    POLICY,
    POSITIONS,
    node_curve,
    row,
    run,
    snapshot,
)
from test_xccy import RUB as XCCY_RUB
from test_xccy import bootstrap_request

from rub_fixed_income import (
    RISK_BUCKETS,
    BondScenario,
    CurveEntry,
    CurveUse,
    DiscountCurve,
    ParYieldCurveRequest,
    ParYieldQuote,
    PortfolioScenario,
    ValuationPolicy,
    bootstrap_par_yield_curve,
    bootstrap_xccy_curve,
    from_dict,
    get_curve_spec,
    par_yield,
    portfolio_result_from_json,
    portfolio_result_to_json,
    to_dict,
    verify_portfolio_result,
)
from rub_fixed_income.tenors import add_tenor

UST_PAR = (
    ("1M", 0.0418),
    ("6W", 0.0416),
    ("3M", 0.0410),
    ("6M", 0.0398),
    ("1Y", 0.0381),
    ("2Y", 0.0365),
    ("3Y", 0.0361),
    ("5Y", 0.0368),
    ("7Y", 0.0382),
    ("10Y", 0.0401),
    ("20Y", 0.0447),
    ("30Y", 0.0462),
)
ECB_AAA_PAR = (
    ("3M", 0.0195),
    ("6M", 0.0193),
    ("9M", 0.0192),
    ("1Y", 0.0191),
    ("2Y", 0.0198),
    ("5Y", 0.0225),
    ("10Y", 0.0262),
    ("30Y", 0.0298),
)


def proxy_curve(spec_id: str, quotes: tuple[tuple[str, float], ...]) -> DiscountCurve:
    request = ParYieldCurveRequest(
        spec_id, BUSINESS_DATE, tuple(ParYieldQuote(t, y) for t, y in quotes)
    )
    return bootstrap_par_yield_curve(request)


UST = proxy_curve("usd_ust_par_proxy_v1", UST_PAR)
ECB = proxy_curve("eur_ecb_aaa_par_proxy_v1", ECB_AAA_PAR)
CNY_XCCY = bootstrap_xccy_curve(bootstrap_request(), rub_discount_curve=XCCY_RUB)


# --- par-yield proxy bootstrap -------------------------------------------------------


@pytest.mark.parametrize(
    ("spec_id", "quotes", "curve"),
    [("usd_ust_par_proxy_v1", UST_PAR, UST), ("eur_ecb_aaa_par_proxy_v1", ECB_AAA_PAR, ECB)],
)
def test_every_par_quote_reprices(
    spec_id: str, quotes: tuple[tuple[str, float], ...], curve: DiscountCurve
) -> None:
    assert curve.curve_date == BUSINESS_DATE
    assert curve.model_version == get_curve_spec(spec_id).model_version == "par-proxy-logdf-v1"
    assert len(curve.nodes) == len(quotes)
    for tenor, rate in quotes:
        maturity = add_tenor(BUSINESS_DATE, tenor)
        assert par_yield(curve, spec_id, maturity) == pytest.approx(rate, abs=1e-12)
    dfs = [df for _, df in curve.nodes]
    assert all(0.0 < b < a <= 1.0 for a, b in zip([1.0, *dfs], dfs, strict=False))


def test_ust_short_end_is_the_bill_bond_equivalent_yield() -> None:
    for tenor in ("1M", "3M", "6M"):
        maturity = add_tenor(BUSINESS_DATE, tenor)
        rate = dict(UST_PAR)[tenor]
        days = (maturity - BUSINESS_DATE).days
        assert UST.discount_factor(maturity) == pytest.approx(
            1.0 / (1.0 + rate * days / 365.0), rel=1e-13
        )


def test_ust_coupon_bond_is_semiannual_bond_equivalent() -> None:
    """A 2Y par bond at the quote pays y/2 every 6 months and prices at 100."""
    maturity = add_tenor(BUSINESS_DATE, "2Y")
    rate = dict(UST_PAR)["2Y"]
    coupons = [add_tenor(BUSINESS_DATE, f"{m}M") for m in (6, 12, 18, 24)]
    value = math.fsum(rate / 2.0 * UST.discount_factor(d) for d in coupons)
    assert value + UST.discount_factor(maturity) == pytest.approx(1.0, abs=1e-12)


def test_ecb_flat_continuous_par_yield_is_the_continuous_zero_rate() -> None:
    """With coupons paid continuously, a flat par yield y means DF(T) = exp(-y T)."""
    flat = proxy_curve("eur_ecb_aaa_par_proxy_v1", tuple((t, 0.025) for t, _ in ECB_AAA_PAR))
    for node, df in flat.nodes:
        t = (node - BUSINESS_DATE).days / 365.0
        assert df == pytest.approx(math.exp(-0.025 * t), rel=1e-12)
    later = date(2070, 1, 15)  # flat-forward beyond the last node
    t = (later - BUSINESS_DATE).days / 365.0
    assert flat.discount_factor(later) == pytest.approx(math.exp(-0.025 * t), rel=1e-11)
    assert par_yield(flat, "eur_ecb_aaa_par_proxy_v1", later) == pytest.approx(0.025, abs=1e-12)


def test_negative_eur_par_yields_bootstrap() -> None:
    curve = proxy_curve("eur_ecb_aaa_par_proxy_v1", (("1Y", -0.006), ("5Y", -0.002)))
    assert curve.nodes[0][1] > 1.0
    assert par_yield(curve, "eur_ecb_aaa_par_proxy_v1", curve.nodes[1][0]) == pytest.approx(
        -0.002, abs=1e-12
    )


def test_quote_order_does_not_matter_and_requests_are_validated() -> None:
    shuffled = proxy_curve("usd_ust_par_proxy_v1", tuple(reversed(UST_PAR)))
    assert shuffled == UST
    with pytest.raises(ValueError, match="not a par-yield proxy"):
        ParYieldCurveRequest(
            "usd_discount_factors_v1", BUSINESS_DATE, (ParYieldQuote("1Y", 0.04),)
        )
    with pytest.raises(ValueError, match="at least one"):
        ParYieldCurveRequest("usd_ust_par_proxy_v1", BUSINESS_DATE, ())
    with pytest.raises(ValueError, match="same maturity"):
        ParYieldCurveRequest(
            "usd_ust_par_proxy_v1",
            BUSINESS_DATE,
            (ParYieldQuote("12M", 0.04), ParYieldQuote("1Y", 0.041)),
        )
    with pytest.raises(ValueError, match="D, W, M or Y"):
        ParYieldQuote("ON", 0.04)
    with pytest.raises(ValueError, match="finite"):
        ParYieldQuote("1Y", float("nan"))
    with pytest.raises(ValueError, match="between"):
        ParYieldQuote("1Y", 4.0)  # a percent passed as a decimal
    with pytest.raises(ValueError, match="cannot be bootstrapped"):
        proxy_curve("eur_ecb_aaa_par_proxy_v1", (("30Y", -0.49),))
    with pytest.raises(ValueError, match="not a par-yield proxy"):
        par_yield(UST, "usd_discount_factors_v1", date(2030, 1, 1))
    assert ParYieldQuote(" 10y ", 0.04).tenor == "10Y"


# --- bonds on the XCCY CNY curve and on proxies ----------------------------------------


def foreign_snapshot(*extra: CurveEntry, drop: tuple[str, ...] = ()) -> object:
    base = snapshot()
    kept = tuple(c for c in base.curves if c.curve_id not in drop)
    return replace(base, curves=(*kept, *extra))


CNY_ENTRY = CurveEntry("CNY_XCCY", "cny_ruonia_xccy_v1", CNY_XCCY)
UST_ENTRY = CurveEntry("USD_UST", "usd_ust_par_proxy_v1", UST)
ECB_ENTRY = CurveEntry("EUR_AAA", "eur_ecb_aaa_par_proxy_v1", ECB)
OWN_CURVES = ValuationPolicy(
    "RUB_OIS",
    foreign_curves=(("CNY", "CNY_XCCY"), ("USD", "USD_UST"), ("EUR", "EUR_AAA")),
)


def own_curve_run(**kw: object):  # type: ignore[no-untyped-def]
    args: dict[str, object] = {
        "snapshot": foreign_snapshot(CNY_ENTRY, UST_ENTRY, ECB_ENTRY),
        "policy": OWN_CURVES,
    }
    args.update(kw)
    return run(**args)


def test_each_currency_is_discounted_and_risked_on_its_own_curve() -> None:
    result = own_curve_run()
    assert result.status == "complete"
    expected = {
        "p-cny": ("CNY_XCCY", "cny_ruonia_xccy_v1"),
        "p-usd": ("USD_UST", "usd_ust_par_proxy_v1"),
        "p-eur": ("EUR_AAA", "eur_ecb_aaa_par_proxy_v1"),
        "p-rub": ("RUB_OIS", "rub_ruonia_ois_legacy_v1"),
    }
    for pid, (curve_id, spec_id) in expected.items():
        valued = row(result, pid)
        assert (valued.curve_id, valued.spec_id, valued.curve_basis) == (
            curve_id,
            spec_id,
            "continuous_zero",
        )
        assert valued.curve_fallback_from is None
        first = valued.factor_risk.first_order_by_factor()
        ir = {k for k in first if k.startswith("IR|")}
        # Rate risk on the bond's own curve only: none leaks to RUB (or any other) curve.
        assert ir == {f"IR|{curve_id}|{b}" for b in RISK_BUCKETS}
        assert first["CS"] < 0  # CS01 against the bond's own currency curve
        assert math.fsum(first[k] for k in ir) < 0
    totals = {t.factor for t in result.factor_totals}
    assert {"IR|CNY_XCCY|5Y", "IR|USD_UST|10Y", "IR|EUR_AAA|10Y", "IR|RUB_OIS|5Y"} <= totals
    assert not any(k.startswith(("IR|CNY_QUOTES", "IR|USD_SOFR", "IR|EUR_ESTR")) for k in totals)
    uses = {c.curve_id: c for c in result.curves}
    assert uses["USD_UST"] == CurveUse(
        "USD_UST", "usd_ust_par_proxy_v1", "par-proxy-logdf-v1", True
    )
    assert uses["EUR_AAA"].proxy and not uses["CNY_XCCY"].proxy and not uses["RUB_OIS"].proxy
    assert uses["CNY_XCCY"].model_version == "xccy-cny-logdf-v1"


def test_cny_on_xccy_equals_the_same_factors_as_caller_discount_factors() -> None:
    """The XCCY spec only names where the curve came from: same nodes, same numbers."""
    on_xccy = row(own_curve_run(), "p-cny")
    as_factors = CurveEntry(
        "CNY_XCCY", "cny_discount_factors_v1", replace(CNY_XCCY, model_version="ois-logdf-v1")
    )
    plain = row(own_curve_run(snapshot=foreign_snapshot(as_factors, UST_ENTRY, ECB_ENTRY)), "p-cny")
    assert on_xccy.pv_rub == plain.pv_rub and on_xccy.spread_bp == plain.spread_bp
    assert on_xccy.factor_risk == plain.factor_risk
    amounts = on_xccy.currency_amounts
    assert amounts is not None and amounts.currency == "CNY" and amounts.rub_per_unit == FX["CNY"]


def test_proxy_scenario_moves_only_the_proxy_curve() -> None:
    shock = PortfolioScenario(
        "ust+10", BondScenario(), curve_shifts_bp=(("USD_UST", (10.0,) * len(RISK_BUCKETS)),)
    )
    result = own_curve_run(scenarios=(shock,))
    pnl = {p.position_id: p.pnl_rub for p in result.scenarios[0].positions}
    assert pnl["p-usd"] is not None and pnl["p-usd"] < 0
    assert pnl["p-cny"] == pnl["p-eur"] == pnl["p-rub"] == 0.0
    first = row(result, "p-usd").factor_risk.first_order_by_factor()
    linear = 10.0 * math.fsum(first[f"IR|USD_UST|{b}"] for b in RISK_BUCKETS)
    assert pnl["p-usd"] == pytest.approx(linear, rel=5e-3)


def test_xccy_quotes_stale_curves_and_cross_currency_curves_are_rejected() -> None:
    quotes = CurveEntry("CNY_XCCY", "cny_ruonia_xccy_v1", bootstrap_request())
    stale = CurveEntry(
        "USD_UST",
        "usd_ust_par_proxy_v1",
        DiscountCurve(date(2026, 10, 14), UST.nodes, UST.model_version, convention=UST.convention),
    )
    wrong_model = CurveEntry("USD_UST", "usd_ust_par_proxy_v1", node_curve("USD"))
    eur_as_usd = CurveEntry("USD_UST", "eur_ecb_aaa_par_proxy_v1", ECB)
    cases = [
        (quotes, "p-cny", "holds XCCY quotes"),
        (stale, "p-usd", "not dated the snapshot business date"),
        (wrong_model, "p-usd", "does not match spec"),
        (eur_as_usd, "p-usd", "is a EUR curve; the bond pays USD"),
    ]
    for entry, pid, message in cases:
        others = tuple(e for e in (CNY_ENTRY, UST_ENTRY, ECB_ENTRY) if e.curve_id != entry.curve_id)
        result = own_curve_run(
            snapshot=foreign_snapshot(entry, *others),
            positions=tuple(p for p in POSITIONS if p.position_id == pid),
        )
        [rejection] = result.coverage.rejections
        assert rejection.code == "INVALID_TERMS" and message in rejection.message, rejection


# --- missing curves: explicit fallback, never silent, never RUB ------------------------


FALLBACK = ValuationPolicy(
    "RUB_OIS",
    foreign_curves=(("USD", "USD_SOFR"), ("CNY", "CNY_QUOTES"), ("EUR", "EUR_ESTR")),
    foreign_curve_fallbacks=(("USD", "USD_UST"), ("EUR", "EUR_AAA"), ("EUR", "EUR_BUND")),
)


def test_absent_curve_falls_back_to_the_named_proxy_and_says_so() -> None:
    snap = foreign_snapshot(UST_ENTRY, ECB_ENTRY, drop=("USD_SOFR",))
    result = run(snapshot=snap, policy=FALLBACK)
    assert result.status == "complete"
    usd = row(result, "p-usd")
    assert (usd.curve_id, usd.curve_fallback_from) == ("USD_UST", "USD_SOFR")
    assert "IR|USD_UST|5Y" in usd.factor_risk.factors
    # The EUR primary is present: no fallback even though one is configured.
    eur = row(result, "p-eur")
    assert (eur.curve_id, eur.curve_fallback_from) == ("EUR_ESTR", None)
    assert {c.curve_id for c in result.curves} >= {"USD_UST", "EUR_ESTR"}
    assert "USD_SOFR" not in {c.curve_id for c in result.curves}
    raw = json.loads(portfolio_result_to_json(result))
    rows = {p["position_id"]: p for p in raw["positions"]}
    assert rows["p-usd"]["curve_fallback_from"] == "USD_SOFR"
    assert "curve_fallback_from" not in rows["p-eur"]
    curves = {c["curve_id"]: c for c in raw["curves"]}
    assert curves["USD_UST"]["proxy"] is True and "proxy" not in curves["EUR_ESTR"]


def test_fallbacks_are_tried_in_order_within_a_currency() -> None:
    bund = CurveEntry("EUR_BUND", "eur_ecb_aaa_par_proxy_v1", ECB)
    only_bund = foreign_snapshot(bund, drop=("EUR_ESTR",))
    eur = row(run(snapshot=only_bund, policy=FALLBACK), "p-eur")
    assert (eur.curve_id, eur.curve_fallback_from) == ("EUR_BUND", "EUR_ESTR")
    both = foreign_snapshot(bund, ECB_ENTRY, drop=("EUR_ESTR",))
    assert row(run(snapshot=both, policy=FALLBACK), "p-eur").curve_id == "EUR_AAA"


def test_no_curve_is_a_missing_data_rejection_naming_every_curve_tried() -> None:
    result = run(
        snapshot=foreign_snapshot(drop=("EUR_ESTR",)),
        policy=FALLBACK,
        positions=(POSITIONS[3],),
    )
    [rejection] = result.coverage.rejections
    assert rejection.code == "MISSING_MARKET_DATA"
    assert all(c in rejection.message for c in ("EUR_ESTR", "EUR_AAA", "EUR_BUND"))
    # Without fallbacks the behaviour of earlier releases: the curve lookup's rejection.
    [plain] = run(
        snapshot=foreign_snapshot(drop=("EUR_ESTR",)), positions=(POSITIONS[3],)
    ).coverage.rejections
    assert plain.code == "MISSING_MARKET_DATA" and "EUR_ESTR" in plain.message


def test_a_present_but_unusable_curve_does_not_fall_back() -> None:
    stale = CurveEntry("USD_SOFR", "usd_discount_factors_v1", node_curve("USD", date(2026, 10, 14)))
    snap = foreign_snapshot(stale, UST_ENTRY, drop=("USD_SOFR",))
    [rejection] = run(snapshot=snap, policy=FALLBACK, positions=POSITIONS[:1]).coverage.rejections
    assert rejection.code == "INVALID_TERMS" and "USD_SOFR" in rejection.message


def test_a_fallback_never_substitutes_a_rub_or_other_currency_curve() -> None:
    to_rub = replace(FALLBACK, foreign_curve_fallbacks=(("USD", "RUB_OIS"),))
    [rejection] = run(
        snapshot=foreign_snapshot(drop=("USD_SOFR",)), policy=to_rub, positions=POSITIONS[:1]
    ).coverage.rejections
    assert rejection.code == "INVALID_TERMS"
    assert "is a RUB curve; the bond pays USD" in rejection.message
    with pytest.raises(ValueError, match="other than RUB"):
        ValuationPolicy(
            "RUB_OIS",
            foreign_curves=(("USD", "A"),),
            foreign_curve_fallbacks=(("RUB", "B"),),
        )


def test_fallback_policy_validation_and_identity() -> None:
    with pytest.raises(ValueError, match="needs a foreign_curves entry"):
        ValuationPolicy("RUB_OIS", foreign_curve_fallbacks=(("USD", "USD_UST"),))
    with pytest.raises(ValueError, match="is its primary curve"):
        ValuationPolicy(
            "RUB_OIS", foreign_curves=(("USD", "A"),), foreign_curve_fallbacks=(("USD", "A"),)
        )
    with pytest.raises(ValueError, match="duplicate fallback"):
        ValuationPolicy(
            "RUB_OIS",
            foreign_curves=(("USD", "A"),),
            foreign_curve_fallbacks=(("USD", "B"), ("USD", "B")),
        )
    with pytest.raises(ValueError, match="non-empty"):
        ValuationPolicy(
            "RUB_OIS", foreign_curves=(("USD", "A"),), foreign_curve_fallbacks=(("USD", ""),)
        )
    # Currencies sort, the order within a currency is kept.
    policy = ValuationPolicy(
        "RUB_OIS",
        foreign_curves=(("USD", "A"), ("EUR", "E")),
        foreign_curve_fallbacks=(("USD", "C"), ("EUR", "F"), ("USD", "B")),
    )
    assert policy.foreign_curve_fallbacks == (("EUR", "F"), ("USD", "C"), ("USD", "B"))
    assert policy.fallback_curves_for("USD") == ("C", "B")
    assert from_dict(ValuationPolicy, to_dict(FALLBACK)) == FALLBACK
    assert "foreign_curve_fallbacks" not in to_dict(POLICY)


def test_results_round_trip_and_earlier_encodings_are_unchanged() -> None:
    result = own_curve_run()
    assert verify_portfolio_result(result)
    assert portfolio_result_from_json(portfolio_result_to_json(result)) == result
    earlier = json.loads(portfolio_result_to_json(run()))
    assert all("proxy" not in c for c in earlier["curves"])
    assert all("curve_fallback_from" not in p for p in earlier["positions"])
