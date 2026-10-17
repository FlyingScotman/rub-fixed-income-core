"""Golden tests: MOEX OFZ basket futures, December 2021 series (last OFZ futures: June 2022).

Fixtures and provenance: ``tests/fixtures/moex_ofz_futures/README.md``. MOEX published the
delivery price of every basket bond (``F / 10 * CF`` in % of face) and the default delivery
issues; core reproduces the conversion factors from the specification formula and the
cheapest bond from its carry model.
"""

from __future__ import annotations

import html
import json
import math
import re
from dataclasses import replace
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

import pytest

from rub_fixed_income import (
    CashflowKind,
    CouponPeriod,
    DeliverableBond,
    DiscountCurve,
    FixedCouponLeg,
    Instrument,
    OfzFuturesContract,
    OfzFuturesRiskEngine,
    OfzFuturesRiskRequest,
    PrincipalLeg,
    ProductType,
    Redemption,
    ZcycParameters,
    kbd_discount_curve,
    ofz_conversion_factor,
    ofz_default_delivery_issue,
)

FIXTURES = Path(__file__).parent / "fixtures" / "moex_ofz_futures"
LTD = date(2021, 12, 3)
DELIVERY = date(2021, 12, 6)

# Contract series: FORTS code and the conversion yield. The specification leaves the
# yield to an exchange decision; these round values are the only ones that reproduce
# every published delivery price of the series (one yield per series, ACT/365F).
SERIES = {
    "OFZ2-12.21": ("O2Z1", 0.068),
    "OFZ4-12.21": ("O4Z1", 0.066),
    "OFZ6-12.21": ("O6Z1", 0.070),
    "OF10-12.21": ("OXZ1", 0.071),
    "OF15-12.21": ("OVZ1", 0.087),
}


def rows(block: dict[str, Any]) -> list[dict[str, Any]]:
    return [dict(zip(block["columns"], row, strict=True)) for row in block["data"]]


def news_text(news_id: int) -> list[str]:
    raw = json.loads((FIXTURES / "sitenews" / f"{news_id}.json").read_text("utf-8"))
    (row,) = rows(raw["content"])
    cells = re.split(r"<[^>]+>", row["body"])
    return [html.unescape(c).strip() for c in cells if html.unescape(c).strip()]


def delivery_prices() -> tuple[dict[str, dict[str, float]], dict[str, float]]:
    """Per series: {OFZ number: delivery price %}; and the settlement prices."""
    tokens = news_text(38431)
    baskets: dict[str, dict[str, float]] = {}
    settles: dict[str, float] = {}
    current: str | None = None
    in_settles = False
    for i, token in enumerate(tokens):
        if token.startswith("Расчетные цены"):
            in_settles = True
        elif token in SERIES:
            if in_settles:
                settles[token] = float(tokens[i + 1])
            else:
                current = token
                baskets[current] = {}
        elif token.startswith("ОФЗ ") and current is not None and not in_settles:
            baskets[current][token.split()[1]] = float(tokens[i + 1])
    return baskets, settles


def default_issues() -> dict[str, str]:
    tokens = news_text(38375)
    return {
        token: tokens[i + 1].split()[1]
        for i, token in enumerate(tokens)
        if token in SERIES and tokens[i + 1].startswith("ОФЗ ")
    }


def secid(number: str) -> str:
    (path,) = (FIXTURES / "bondization").glob(f"SU{number}RMFS*.json")
    return path.stem


def instrument(number: str) -> Instrument:
    sec = secid(number)
    raw = json.loads((FIXTURES / "bondization" / f"{sec}.json").read_text("utf-8"))
    coupons = rows(raw["coupons"])
    (maturity,) = rows(raw["amortizations"])
    return Instrument(
        sec,
        ProductType.FIXED_BOND,
        "RUB",
        float(maturity["value"]),
        FixedCouponLeg(
            tuple(
                CouponPeriod(
                    date.fromisoformat(c["startdate"]),
                    date.fromisoformat(c["coupondate"]),
                    date.fromisoformat(c["coupondate"]),
                    float(c["value"]),
                )
                for c in coupons
            )
        ),
        PrincipalLeg(
            (
                Redemption(
                    date.fromisoformat(maturity["amortdate"]),
                    float(maturity["value"]),
                    CashflowKind.MATURITY,
                ),
            )
        ),
        "MOEX_STOCK",
    )


def close(number: str, day: date) -> float:
    """The official closing price (``LEGALCLOSEPRICE``), not the last trade (``CLOSE``)."""
    raw = json.loads((FIXTURES / "tqob" / f"{secid(number)}.json").read_text("utf-8"))
    (row,) = [r for r in rows(raw["history"]) if r["TRADEDATE"] == day.isoformat()]
    return float(row["LEGALCLOSEPRICE"])


def forts_settle(code: str, day: date) -> float:
    raw = json.loads((FIXTURES / "forts" / f"history_{code}.json").read_text("utf-8"))
    (row,) = [r for r in rows(raw["history"]) if r["TRADEDATE"] == day.isoformat()]
    return float(row["SETTLEPRICE"])


def kbd(day: date) -> DiscountCurve:
    raw = json.loads((FIXTURES / "kbd" / f"zcyc_{day.isoformat()}.json").read_text("utf-8"))
    row = raw["params"]["data"][0]
    params = ZcycParameters(
        date.fromisoformat(row[0]), row[2], row[3], row[4], row[5], tuple(row[6:15])
    )
    return kbd_discount_curve(params)


BASKETS, SETTLES = delivery_prices()


def test_news_and_forts_agree_on_the_settlement_prices() -> None:
    assert len(BASKETS) == 5 and sum(len(b) for b in BASKETS.values()) == 25
    for series, (code, _) in SERIES.items():
        assert SETTLES[series] == forts_settle(code, LTD)


def published_factor(series: str, number: str) -> Decimal:
    """CF behind a published delivery price: price / (F / 10 / face * 100), 4 places."""
    ratio = Decimal(str(BASKETS[series][number])) / (Decimal(str(SETTLES[series])) / 100)
    return ratio.quantize(Decimal("0.0001"), ROUND_HALF_UP)


def test_conversion_factors_reproduce_published_delivery_prices() -> None:
    """The specification formula, ACT/365F, one round yield per series.

    19 of 25 factors match to the published four decimals; six are one or two units off
    in the fourth decimal, all OF10 / OF15 bonds of 7 to 15 years. No single yield per series fixes
    them, and ACT/ACT, 365.25 or anniversary-year time bases fit worse, so MOEX's exact
    time convention for long bonds is not fully identified. Pricing uses the published
    factors (``DeliverableBond.conversion_factor``), never recomputed ones.
    """
    exact = 0
    for series, (_, conversion_yield) in SERIES.items():
        for number in BASKETS[series]:
            factor = ofz_conversion_factor(instrument(number), DELIVERY, conversion_yield)
            published = float(published_factor(series, number))
            assert factor == pytest.approx(published, abs=2.0001e-4), (series, number)
            exact += factor == published
    assert exact == 19


def test_delivery_prices_follow_from_published_factors() -> None:
    for series, (code, _) in SERIES.items():
        contract = OfzFuturesContract(code, LTD, DELIVERY)
        for number, published in BASKETS[series].items():
            price = contract.delivery_price_pct(
                SETTLES[series], float(published_factor(series, number)), 1000.0
            )
            rounded = Decimal(repr(price)).quantize(Decimal("0.0001"), ROUND_HALF_UP)
            assert float(rounded) == published, (series, number)


def basket(series: str, day: date) -> tuple[DeliverableBond, ...]:
    """Basket bonds with the exchange's (published) conversion factors."""
    return tuple(
        DeliverableBond(instrument(n), float(published_factor(series, n)), close(n, day))
        for n in sorted(BASKETS[series])
    )


@pytest.mark.parametrize("series", sorted(SERIES))
def test_cheapest_to_deliver_is_moex_default_issue(series: str) -> None:
    """News of 2021-12-02 21:28: default issues from that day's closing prices.

    The specification (item 4.4) names the rule: the lowest closing price / CF on the
    trading day before the last trading day. MOEX's closing price is ``LEGALCLOSEPRICE``;
    on the last trade (``CLOSE``) the rule would pick other bonds for OFZ6 and OF15.

    Spot settles T+1 (2021-12-03), carried to delivery (2021-12-06) on a flat 7.5 %
    funding curve (the key rate; three days of carry move no ranking here).
    """
    day = date(2021, 12, 2)
    code, _ = SERIES[series]
    engine = OfzFuturesRiskEngine(
        OfzFuturesRiskRequest(
            OfzFuturesContract(code, LTD, DELIVERY),
            basket(series, day),
            date(2021, 12, 3),
            kbd(day),
            DiscountCurve.flat(day, math.log1p(0.075)),
            basis_bp=0.0,
        )
    )
    cheapest = next(a for a in engine.basket() if a.cheapest)
    assert cheapest.instrument_id == secid(default_issues()[series])
    # Specification 4.4: lowest closing price / CF on the day before the last trading day.
    assert ofz_default_delivery_issue(basket(series, day)) == cheapest.instrument_id
    # The settlement price that evening: within 0.5 % of core's price (OF10 0.15 %).
    gap = engine.theoretical_price / forts_settle(code, day) - 1.0
    assert abs(gap) < 0.005, gap
    calibrated = OfzFuturesRiskEngine(
        replace(engine.request, basis_bp=None, futures_price=forts_settle(code, day))
    )
    risk = calibrated.risk(difference="central")
    assert risk.cheapest_to_deliver == risk.cheapest_by_implied_repo == cheapest.instrument_id


# Observed gap |F_theoretical / F_settle - 1| on the last trading day. Spot settles on
# the delivery day, so the theoretical price is min(10 * clean / CF): no carry, no model
# parameter. The settlement price is set in the evening clearing, after the bond close;
# on 2021-12-03 OFZ fell into the evening (the OF10 cheapest bond closed 85.12 % but
# delivered at 84.27 %, and closed 84.26 % on 2021-12-06).
LTD_TOLERANCE = {
    "OFZ2-12.21": 0.0010,
    "OFZ4-12.21": 0.0040,
    "OFZ6-12.21": 0.0070,
    "OF10-12.21": 0.0070,
    "OF15-12.21": 0.0010,
}


@pytest.mark.parametrize("series", sorted(SERIES))
def test_theoretical_price_against_settlement_on_the_last_trading_day(series: str) -> None:
    code, _ = SERIES[series]
    engine = OfzFuturesRiskEngine(
        OfzFuturesRiskRequest(
            OfzFuturesContract(code, LTD, DELIVERY),
            basket(series, LTD),
            DELIVERY,
            kbd(LTD),
            DiscountCurve.flat(LTD, math.log1p(0.075)),
            basis_bp=0.0,
        )
    )
    expected = min(10 * b.clean_price_pct * 10 / b.conversion_factor for b in basket(series, LTD))
    assert engine.theoretical_price == pytest.approx(expected, rel=1e-12)
    gap = engine.theoretical_price / forts_settle(code, LTD) - 1.0
    assert abs(gap) <= LTD_TOLERANCE[series], gap
    risk = engine.risk(difference="central")
    assert risk.bond_dv01_rub < 0.0 and risk.funding_dv01_rub == pytest.approx(0.0, abs=1e-9)
