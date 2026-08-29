from __future__ import annotations

import pytest

from rub_fixed_income import clean_price_pct_to_dirty_rub, dirty_rub_to_clean_price_pct


def test_clean_dirty_conversion_round_trips_with_current_nominal_and_ai() -> None:
    dirty = clean_price_pct_to_dirty_rub(95.25, 800.0, 12.40)

    assert dirty == pytest.approx(774.40)
    assert dirty_rub_to_clean_price_pct(dirty, 800.0, 12.40) == pytest.approx(95.25)
