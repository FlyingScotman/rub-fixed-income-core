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
        "rub_key_rate_swap_v1",
    }
    kbd = get_curve_spec("rub_ofz_kbd_v1")
    assert kbd.kind is CurveKind.KBD_PARAMETRIC and kbd.model_version == KBD_MODEL_VERSION
    assert get_curve_spec("rub_key_rate_swap_v1").model_version == CONVENTION_ID
    legacy = get_curve_spec("rub_ruonia_ois_legacy_v1")
    assert legacy.spot_lag_business_days == 0 and legacy.payment_lag_business_days == 0
    for spec_id, spec in CURVE_SPECS.items():
        assert spec.spec_id == spec_id


def test_unknown_spec_is_a_typed_rejection() -> None:
    with pytest.raises(UnknownCurveSpec, match="rub_ruonia_ois_moex_v2"):
        get_curve_spec("rub_ruonia_ois_moex_v2")
    with pytest.raises(TypeError):
        CURVE_SPECS["x"] = CURVE_SPECS["rub_ofz_kbd_v1"]  # type: ignore[index]
