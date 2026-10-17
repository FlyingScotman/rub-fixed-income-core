"""Curve-spec registry (roadmap 0.5, minimal)."""

from __future__ import annotations

import pytest

from rub_fixed_income import (
    CURVE_SPECS,
    KBD_MODEL_VERSION,
    CurveKind,
    UnknownCurveSpec,
    get_curve_spec,
)
from rub_fixed_income.key_rate import CONVENTION_ID


def test_registered_specs() -> None:
    assert set(CURVE_SPECS) == {
        "rub_ofz_kbd_v1",
        "rub_ruonia_ois_legacy_v1",
        "rub_ruonia_ois_moex_v2",
        "rub_key_rate_swap_v1",
        "cny_discount_factors_v1",
        "usd_discount_factors_v1",
        "eur_discount_factors_v1",
    }
    kbd = get_curve_spec("rub_ofz_kbd_v1")
    assert kbd.kind is CurveKind.KBD_PARAMETRIC and kbd.model_version == KBD_MODEL_VERSION
    assert get_curve_spec("rub_key_rate_swap_v1").model_version == CONVENTION_ID
    legacy = get_curve_spec("rub_ruonia_ois_legacy_v1")
    assert legacy.spot_lag_business_days == 0 and legacy.payment_lag_business_days == 0
    moex = get_curve_spec("rub_ruonia_ois_moex_v2")
    assert moex.kind is CurveKind.OIS_BOOTSTRAP
    assert moex.spot_lag_business_days == 1 and moex.payment_lag_business_days == 1
    for spec_id, spec in CURVE_SPECS.items():
        assert spec.spec_id == spec_id


def test_unknown_spec_is_a_typed_rejection() -> None:
    with pytest.raises(UnknownCurveSpec, match="rub_ruonia_ois_moex_v3"):
        get_curve_spec("rub_ruonia_ois_moex_v3")
    with pytest.raises(TypeError):
        CURVE_SPECS["x"] = CURVE_SPECS["rub_ofz_kbd_v1"]  # type: ignore[index]


def test_foreign_discount_specs_are_caller_built_curves_per_currency() -> None:
    for currency in ("CNY", "USD", "EUR"):
        spec = get_curve_spec(f"{currency.lower()}_discount_factors_v1")
        assert spec.kind is CurveKind.DISCOUNT_FACTORS and spec.currency == currency
        assert spec.model_version == "ois-logdf-v1"
        assert spec.fixed_leg_frequency_months is None  # no bootstrap conventions assumed
