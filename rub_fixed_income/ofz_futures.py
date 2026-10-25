"""MOEX OFZ basket futures: conversion factors, cheapest to deliver, carry and risk.

Terms from the MOEX "Specification of futures contracts on a basket of federal loan
bonds" (edition approved 24 April 2015, in force 15 November 2015 to 27 February 2023,
www.moex.com/ru/documents/6381), which covered OFZ2, OFZ4, OFZ6, OF10 and OF15:

- deliverable OFZ chosen by the exchange (bullet, constant coupon), lot 10 bonds, price in
  RUB per lot without accrued interest, variation margin ``(P - P_prev) * W / R``
  rounded to kopecks (W = R = 1 RUB);
- delivery price of bond ``i``: ``P_i = F / N * CF_i`` (Appendix 3), ``N`` = 10 bonds;
- conversion factor (Appendix 4): ``CF_i = P_i(r) / N_i`` with
  ``P_i(r) = sum_k C_k / (1 + r)^t_k + N_i / (1 + r)^T_i - AI_i``, valued on the delivery
  date, rounded to 4 decimals. The yield ``r`` is "set by decision of the exchange" for
  each contract series: it is an input here. ``t_k`` are years; ACT/365F reproduces the
  MOEX-published conversion factors of December 2021 best (19 of 25 exactly, the rest
  within 2 units of the 4th decimal, ``tests/test_ofz_futures_goldens.py``);
- default delivery issue (item 4.4): the bond with the lowest closing price on the
  trading day before the last trading day divided by its conversion factor
  (:func:`ofz_default_delivery_issue`).

MOEX listed no OFZ futures after the June 2022 expiry; RGBI index futures are a different
product (cash-settled on an index) and are not modelled here.

The theoretical price is the cheapest of the basket's forward clean prices divided by
their conversion factors: each bond is priced on its curve with a calibrated spread
(frozen under shocks), carried to delivery on a funding curve (coupons received before
delivery are reinvested on it), plus an optional calibrated basis. The seller's delivery
options (choice of bond, delivery within three periods of the delivery day) are **not
valued**: deterministic rates make the cheapest bond certain, so the model price is an
upper bound of a price with option value. Shocks can switch the cheapest bond, and the
exact scenarios show it.

:meth:`OfzFuturesRiskEngine.horizon` carries a position to a horizon before delivery on
the two curve views of ``horizon`` (today's curves read forward, and rolled); see
:class:`OfzFuturesHorizonValuation`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal

from ._curve_views import forward_curve, roll_curve
from ._money import finite_money, positive_money
from ._risk_common import (
    _ZERO_SHOCK,
    RISK_BUCKETS,
    ShockedCurveCache,
    _base_curve,
    _central_derivatives,
    _check_scenario_shifts,
    _curve_basis,
    _curve_identity,
    _shifts,
    _shock_curve,
    _snapshot_curve,
)
from ._solver import bisect
from .canonical import canonical_sha256
from .conventions import DayCount
from .discount import CurveBootstrapRequest, DiscountCurve, DiscountFactorCurve
from .domain import FixedCouponLeg, Instrument, PrincipalLeg
from .errors import InvalidTerms
from .factors import CS_FACTOR, FactorRisk, HessianEntry, ir_factor
from .rate_futures import futures_basis_factor
from .risk import BucketRisk

__all__ = [
    "OFZ_FUTURES_HORIZON_MODEL_VERSION",
    "OFZ_FUTURES_RISK_MODEL_VERSION",
    "OFZ_FUTURES_SECOND_ORDER_MODEL_VERSION",
    "DeliverableAnalysis",
    "DeliverableBond",
    "OfzFuturesContract",
    "OfzFuturesHorizonValuation",
    "OfzFuturesRiskEngine",
    "OfzFuturesRiskRequest",
    "OfzFuturesRiskResult",
    "OfzFuturesScenario",
    "OfzFuturesScenarioResult",
    "OfzFuturesSecondOrderRisk",
    "ofz_conversion_factor",
    "ofz_default_delivery_issue",
    "ofz_futures_factor_risk",
    "ofz_futures_horizon",
]

OFZ_FUTURES_RISK_MODEL_VERSION = "rub-ofz-futures-ctd-v1"
OFZ_FUTURES_SECOND_ORDER_MODEL_VERSION = "rub-ofz-futures-ctd-second-order-v1"
OFZ_FUTURES_HORIZON_MODEL_VERSION = "rub-ofz-futures-horizon-v1"
_ACT365 = DayCount.ACT_365_FIXED


def _bullet_terms(instrument: Instrument) -> tuple[date, float]:
    if not isinstance(instrument, Instrument):
        raise TypeError("instrument must be an Instrument")
    if instrument.currency != "RUB" or not isinstance(instrument.coupon_leg, FixedCouponLeg):
        raise InvalidTerms("deliverable OFZ must be RUB bonds with known fixed coupons")
    leg = instrument.principal_leg
    if not isinstance(leg, PrincipalLeg):
        raise InvalidTerms("deliverable OFZ have a fixed (not indexed) principal")
    redemptions = leg.redemptions
    if len(redemptions) != 1:
        raise InvalidTerms("deliverable OFZ must be bullet bonds without amortization")
    return redemptions[0].payment_date, instrument.initial_face


def ofz_conversion_factor(
    instrument: Instrument,
    delivery_date: date,
    conversion_yield: float,
    *,
    day_count: DayCount = _ACT365,
    decimals: int | None = 4,
) -> float:
    """MOEX conversion factor ``(sum C_k (1+r)^-t_k + N (1+r)^-T - AI) / N`` at delivery.

    ``conversion_yield`` is the decimal yield the exchange set for the contract series;
    coupons paid after ``delivery_date`` count; ``AI`` is the issue's accrued interest on
    the delivery date. Rounded half up to ``decimals`` (None keeps the raw value).
    """
    maturity, face = _bullet_terms(instrument)
    rate = finite_money(conversion_yield, "conversion yield")
    if rate <= -1.0:
        raise InvalidTerms("conversion yield must exceed -100 %")
    if maturity <= delivery_date:
        raise InvalidTerms("the bond matures on or before delivery")
    assert isinstance(instrument.coupon_leg, FixedCouponLeg)
    value = math.fsum(
        period.amount / (1.0 + rate) ** day_count.year_fraction(delivery_date, period.payment_date)
        for period in instrument.coupon_leg.periods
        if period.payment_date > delivery_date
    )
    value += face / (1.0 + rate) ** day_count.year_fraction(delivery_date, maturity)
    factor = float((value - instrument.accrued(delivery_date)) / face)
    if decimals is None:
        return factor
    return float(Decimal(repr(factor)).quantize(Decimal(1).scaleb(-decimals), ROUND_HALF_UP))


def ofz_default_delivery_issue(basket: tuple[DeliverableBond, ...]) -> str:
    """Instrument ID of the bond with the lowest ``clean price / CF`` (specification 4.4).

    Pass the closing prices of the trading day before the last trading day; the exchange
    delivers that issue when a seller names none.
    """
    bonds = tuple(basket)
    if not bonds or not all(isinstance(b, DeliverableBond) for b in bonds):
        raise InvalidTerms("the basket needs at least one DeliverableBond")
    best = min(bonds, key=lambda b: b.clean_price_pct / b.conversion_factor)
    return best.instrument.instrument_id


@dataclass(frozen=True)
class OfzFuturesContract:
    """A deliverable OFZ futures series; ``futures price`` is RUB per lot, clean.

    ``delivery_date`` is the delivery (settlement) day of the delivery trades, the first
    trading day after ``last_trading_day``.
    """

    contract_id: str
    last_trading_day: date
    delivery_date: date
    bonds_per_lot: int = 10

    def __post_init__(self) -> None:
        if not isinstance(self.contract_id, str) or not self.contract_id.strip():
            raise InvalidTerms("contract_id must be nonempty text")
        if "|" in self.contract_id:
            raise InvalidTerms("contract_id must not contain '|'")
        if self.delivery_date <= self.last_trading_day:
            raise InvalidTerms("delivery follows the last trading day")
        lot = self.bonds_per_lot
        if isinstance(lot, bool) or not isinstance(lot, int) or lot < 1:
            raise InvalidTerms("bonds_per_lot must be a positive integer")

    def delivery_price_pct(
        self, futures_price: float, conversion_factor: float, face: float
    ) -> float:
        """Delivery clean price in % of face: ``F / N * CF / face * 100``, unrounded.

        Computed in decimal arithmetic from the decimal inputs, so rounding the result
        reproduces MOEX's published prices (which carry four decimals).
        """
        price = Decimal(repr(positive_money(futures_price, "futures price")))
        factor = Decimal(repr(positive_money(conversion_factor, "conversion factor")))
        nominal = Decimal(repr(positive_money(face, "face")))
        return float(price / self.bonds_per_lot * factor / nominal * 100)


@dataclass(frozen=True)
class DeliverableBond:
    """A basket bond, its exchange conversion factor and its spot clean price (% of face)."""

    instrument: Instrument
    conversion_factor: float
    clean_price_pct: float

    def __post_init__(self) -> None:
        _bullet_terms(self.instrument)
        object.__setattr__(
            self,
            "conversion_factor",
            positive_money(self.conversion_factor, "conversion factor"),
        )
        object.__setattr__(
            self, "clean_price_pct", positive_money(self.clean_price_pct, "clean price")
        )


RiskCurveInput = DiscountCurve | CurveBootstrapRequest


@dataclass(frozen=True)
class OfzFuturesRiskRequest:
    """One OFZ futures position, quantity in signed contracts.

    ``spot_settlement_date`` is the settlement date of the basket prices (T+1 for TQOB);
    it may equal the delivery date (the last trading day, no carry left). ``bond_curve``
    prices the bonds (with a spread calibrated per bond); ``funding_curve`` carries them
    to delivery. Both curves share one curve date, on or before spot settlement. Supply
    exactly one of ``futures_price`` (calibrates the basis) and ``basis_bp``.
    """

    contract: OfzFuturesContract
    basket: tuple[DeliverableBond, ...]
    spot_settlement_date: date
    bond_curve: RiskCurveInput
    funding_curve: RiskCurveInput
    futures_price: float | None = None
    basis_bp: float | None = None
    quantity: float = 1.0

    def __post_init__(self) -> None:
        if not isinstance(self.contract, OfzFuturesContract):
            raise TypeError("contract must be an OfzFuturesContract")
        basket = tuple(self.basket)
        if not basket or not all(isinstance(b, DeliverableBond) for b in basket):
            raise InvalidTerms("the basket needs at least one DeliverableBond")
        ids = [b.instrument.instrument_id for b in basket]
        if len(set(ids)) != len(ids):
            raise InvalidTerms("basket bonds must be distinct")
        for name in ("bond_curve", "funding_curve"):
            if not isinstance(getattr(self, name), (DiscountCurve, CurveBootstrapRequest)):
                raise TypeError(f"{name} must be DiscountCurve or CurveBootstrapRequest")
        if self.bond_curve.curve_date != self.funding_curve.curve_date:
            raise InvalidTerms("bond and funding curves must share one valuation date")
        settlement, delivery = self.spot_settlement_date, self.contract.delivery_date
        if not self.bond_curve.curve_date <= settlement <= delivery:
            raise InvalidTerms("need curve date <= spot settlement <= delivery date")
        for bond in basket:
            maturity, _ = _bullet_terms(bond.instrument)
            if maturity <= delivery:
                raise InvalidTerms("a basket bond matures on or before delivery")
        object.__setattr__(self, "basket", basket)
        if (self.futures_price is None) == (self.basis_bp is None):
            raise InvalidTerms("supply exactly one of futures_price and basis_bp")
        if self.futures_price is not None:
            object.__setattr__(
                self, "futures_price", positive_money(self.futures_price, "futures price")
            )
        if self.basis_bp is not None:
            object.__setattr__(self, "basis_bp", finite_money(self.basis_bp, "basis bp"))
        object.__setattr__(self, "quantity", finite_money(self.quantity, "quantity"))
        object.__setattr__(self, "bond_curve", _snapshot_curve(self.bond_curve))
        object.__setattr__(self, "funding_curve", _snapshot_curve(self.funding_curve))


@dataclass(frozen=True)
class OfzFuturesScenario:
    """Absolute bp shifts: bond-curve and funding-curve buckets, basket spread, basis."""

    bond_curve_shifts_bp: tuple[float, ...] = _ZERO_SHOCK
    funding_curve_shifts_bp: tuple[float, ...] = _ZERO_SHOCK
    spread_shift_bp: float = 0.0
    basis_shift_bp: float = 0.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "bond_curve_shifts_bp", _shifts(tuple(self.bond_curve_shifts_bp)))
        object.__setattr__(
            self, "funding_curve_shifts_bp", _shifts(tuple(self.funding_curve_shifts_bp))
        )
        object.__setattr__(self, "spread_shift_bp", finite_money(self.spread_shift_bp, "spread"))
        object.__setattr__(self, "basis_shift_bp", finite_money(self.basis_shift_bp, "basis"))

    @classmethod
    def parallel(
        cls,
        *,
        bond_shift_bp: float = 0.0,
        funding_shift_bp: float = 0.0,
        spread_shift_bp: float = 0.0,
        basis_shift_bp: float = 0.0,
    ) -> OfzFuturesScenario:
        size = len(RISK_BUCKETS)
        return cls(
            (bond_shift_bp,) * size, (funding_shift_bp,) * size, spread_shift_bp, basis_shift_bp
        )


@dataclass(frozen=True)
class DeliverableAnalysis:
    """One basket bond at the base market, RUB per bond unless stated.

    ``theoretical_futures_price`` is RUB per lot if this bond were delivered;
    ``implied_repo_rate`` the simple ACT/365F rate that turns buying the bond, holding it
    (coupons reinvested at that rate) and delivering it at the futures price into zero
    profit; ``gross_basis_rub`` = spot clean - futures price / lot * CF; ``net_basis_rub``
    = forward clean - futures price / lot * CF. None when spot settles on delivery.
    """

    instrument_id: str
    conversion_factor: float
    clean_price_pct: float
    spread_bp: float
    dirty_spot_rub: float
    forward_dirty_rub: float
    accrued_at_delivery_rub: float
    theoretical_futures_price: float
    implied_repo_rate: float | None
    gross_basis_rub: float
    net_basis_rub: float
    cheapest: bool


@dataclass(frozen=True)
class OfzFuturesRiskResult:
    """Signed RUB variation margin per 1 bp; DV01 runs through the cheapest bond."""

    base_id: str
    contract_id: str
    bond_curve_basis: str
    funding_curve_basis: str
    difference: str
    bump_bp: float
    futures_price: float
    theoretical_price: float
    basis_bp: float
    cheapest_to_deliver: str
    cheapest_by_implied_repo: str
    bond_buckets: tuple[BucketRisk, ...]
    bond_dv01_rub: float
    bond_parallel_dv01_rub: float
    funding_buckets: tuple[BucketRisk, ...]
    funding_dv01_rub: float
    funding_parallel_dv01_rub: float
    cs01_rub: float
    basis01_rub: float
    basket: tuple[DeliverableAnalysis, ...]
    model_version: str = OFZ_FUTURES_RISK_MODEL_VERSION


@dataclass(frozen=True)
class OfzFuturesScenarioResult:
    base_id: str
    scenario_id: str
    base_price: float
    shocked_price: float
    base_cheapest: str
    shocked_cheapest: str
    bond_rates_pnl_rub: float
    funding_rates_pnl_rub: float
    spread_pnl_rub: float
    basis_pnl_rub: float
    interaction_pnl_rub: float
    total_pnl_rub: float
    model_version: str = OFZ_FUTURES_RISK_MODEL_VERSION


@dataclass(frozen=True)
class OfzFuturesSecondOrderRisk:
    """Central gradient and full Hessian over 11 bond, 11 funding buckets, spread, basis.

    A switch of the cheapest bond inside the bump makes the price kinked there; the
    Hessian then reflects the kink and is only a local description.
    """

    base_id: str
    factors: tuple[str, ...]
    bump_bp: float
    gradient_rub_per_bp: tuple[float, ...]
    hessian_rub_per_bp2: tuple[tuple[float, ...], ...]
    model_version: str = OFZ_FUTURES_SECOND_ORDER_MODEL_VERSION


@dataclass(frozen=True)
class OfzFuturesHorizonValuation:
    """Carry and rolldown of the position from the valuation date to ``horizon_date``.

    At the horizon each basket bond is priced for ``horizon_spot_settlement_date``
    ``s_h``: its flows after ``s_h`` on the bond-curve view anchored at ``s_h``
    (``DF(t) / DF(s_h)``) with its calibrated spread held, carried to delivery on the
    funding-curve view with the basis held (coupons in ``(s_h, D]`` removed). The price is
    the cheapest of ``lot * (forward - AI(D)) / CF``, unrounded; ``cheapest_*`` name the
    cheapest bond at the start and in each view (it can switch). Views: ``forward`` reads
    today's curves forward (today's forwards realised), ``rolled`` rolls them (shape held
    in calendar days).

    The position is margined daily: ``pv_start_rub`` and ``received_rub`` are zero,
    ``carry = quantity * (price_forward - price_start)``, ``rolldown = quantity *
    (price_rolled - price_forward)`` and ``horizon_pnl = quantity * (price_rolled -
    price_start)`` (1 RUB of margin per contract per RUB of price).
    """

    base_id: str
    contract_id: str
    valuation_date: date
    horizon_date: date
    horizon_spot_settlement_date: date
    quantity: float
    price_start: float
    price_forward: float
    price_rolled: float
    cheapest_start: str
    cheapest_forward: str
    cheapest_rolled: str
    pv_start_rub: float
    received_rub: float
    carry_rub: float
    rolldown_rub: float
    horizon_pnl_rub: float
    model_version: str = OFZ_FUTURES_HORIZON_MODEL_VERSION


@dataclass(frozen=True)
class _Bond:
    instrument_id: str
    conversion_factor: float
    clean_price_pct: float
    face: float
    dirty_spot: float
    accrued_delivery: float
    flows: tuple[tuple[date, float, float], ...]  # (date, amount, ACT/365F time from s)
    carry_coupons: tuple[tuple[date, float], ...]  # coupons paid in (s, D]


_Moves = tuple[tuple[float, ...], tuple[float, ...], float, float]
_NO_MOVE: _Moves = (_ZERO_SHOCK, _ZERO_SHOCK, 0.0, 0.0)


@dataclass(frozen=True)
class OfzFuturesRiskEngine:
    """Calibrate bond spreads (and the basis), then reprice the basket exactly per shock."""

    request: OfzFuturesRiskRequest
    curve_cache: ShockedCurveCache = field(
        default_factory=ShockedCurveCache, compare=False, repr=False
    )
    basis_bp: float = field(init=False)
    theoretical_price: float = field(init=False)
    base_price: float = field(init=False)
    base_id: str = field(init=False)
    spreads: tuple[float, ...] = field(init=False)
    _bonds: tuple[_Bond, ...] = field(init=False, repr=False)
    _bond_curve: DiscountCurve = field(init=False, repr=False)
    _funding_curve: DiscountCurve = field(init=False, repr=False)
    _memo: dict[_Moves, tuple[float, int]] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        req = self.request
        if not isinstance(self.curve_cache, ShockedCurveCache):
            raise TypeError("curve_cache must be a ShockedCurveCache")
        object.__setattr__(self, "_memo", {})
        object.__setattr__(self, "_bond_curve", _base_curve(req.bond_curve, self.curve_cache))
        object.__setattr__(self, "_funding_curve", _base_curve(req.funding_curve, self.curve_cache))
        settlement, delivery = req.spot_settlement_date, req.contract.delivery_date
        bonds = []
        for item in req.basket:
            instrument = item.instrument
            face = instrument.outstanding_face(settlement)
            flows = tuple(
                (cf.payment_date, cf.amount, _ACT365.year_fraction(settlement, cf.payment_date))
                for cf in instrument.cashflows(after=settlement)
            )
            carry = tuple((d, a) for d, a, _ in flows if d <= delivery)
            bonds.append(
                _Bond(
                    instrument.instrument_id,
                    item.conversion_factor,
                    item.clean_price_pct,
                    face,
                    item.clean_price_pct / 100.0 * face + instrument.accrued(settlement),
                    instrument.accrued(delivery),
                    flows,
                    carry,
                )
            )
        object.__setattr__(self, "_bonds", tuple(bonds))
        object.__setattr__(self, "spreads", tuple(self._calibrate_spread(b) for b in self._bonds))
        object.__setattr__(self, "basis_bp", 0.0)
        object.__setattr__(self, "theoretical_price", self._price(_NO_MOVE)[0])
        basis = req.basis_bp
        if basis is None:
            assert req.futures_price is not None
            basis = self._calibrate_basis(req.futures_price)
        object.__setattr__(self, "basis_bp", finite_money(basis, "calibrated basis"))
        self._memo.clear()
        object.__setattr__(self, "base_price", self._price(_NO_MOVE)[0])
        object.__setattr__(self, "base_id", self._identity())

    # -- calibration

    def _dirty(
        self,
        bond: _Bond,
        curve: DiscountFactorCurve,
        spread: float,
        settlement: date | None = None,
    ) -> float:
        """Dirty price at ``settlement`` (default spot) of ``bond.flows`` (times from it)."""
        at = self.request.spot_settlement_date if settlement is None else settlement
        settlement_df = curve.discount_factor(at)
        return math.fsum(
            amount * curve.discount_factor(paid) / settlement_df * math.exp(-spread * time)
            for paid, amount, time in bond.flows
        )

    def _calibrate_spread(self, bond: _Bond) -> float:
        curve = self._bond_curve

        def error(spread: float) -> float:
            return self._dirty(bond, curve, spread) - bond.dirty_spot

        return bisect(
            error, -2.0, 2.0, tolerance=1e-10, x_tolerance=1e-14, label="OFZ futures spread"
        ).root

    def _calibrate_basis(self, market: float) -> float:
        if self.request.spot_settlement_date == self.request.contract.delivery_date:
            raise InvalidTerms(
                "spot settles on the delivery date: no carry for a basis to explain; "
                "pass basis_bp=0"
            )

        def error(basis: float) -> float:
            object.__setattr__(self, "basis_bp", basis)
            self._memo.clear()
            return self._price(_NO_MOVE)[0] - market

        return bisect(
            error, -50_000.0, 50_000.0, tolerance=1e-9, x_tolerance=1e-10, label="OFZ futures basis"
        ).root

    def _identity(self) -> str:
        req = self.request
        return canonical_sha256(
            {
                "model": OFZ_FUTURES_RISK_MODEL_VERSION,
                "contract": req.contract,
                "basket": [
                    {
                        "instrument": canonical_sha256(b.instrument),
                        "conversion_factor": b.conversion_factor,
                        "clean_price_pct": b.clean_price_pct,
                    }
                    for b in req.basket
                ],
                "spot_settlement_date": req.spot_settlement_date,
                "bond_curve_basis": self.bond_curve_basis,
                "bond_curve": _curve_identity(req.bond_curve),
                "funding_curve_basis": self.funding_curve_basis,
                "funding_curve": _curve_identity(req.funding_curve),
                "input_futures_price": req.futures_price,
                "input_basis_bp": req.basis_bp,
                "calibrated_basis_bp": self.basis_bp,
                "quantity": req.quantity,
            }
        )

    # -- pricing

    @property
    def bond_curve_basis(self) -> str:
        return _curve_basis(self.request.bond_curve)

    @property
    def funding_curve_basis(self) -> str:
        return _curve_basis(self.request.funding_curve)

    @property
    def _scale(self) -> float:
        return self.request.quantity  # 1 RUB of margin per contract per RUB of price

    def for_quantity(self, quantity: float) -> OfzFuturesRiskEngine:
        """The same contract and market for another position size, sharing repricings."""
        twin = OfzFuturesRiskEngine.__new__(OfzFuturesRiskEngine)
        for name in (
            "curve_cache",
            "basis_bp",
            "theoretical_price",
            "base_price",
            "spreads",
            "_bonds",
            "_bond_curve",
            "_funding_curve",
            "_memo",
        ):
            object.__setattr__(twin, name, getattr(self, name))
        object.__setattr__(twin, "request", replace(self.request, quantity=quantity))
        object.__setattr__(twin, "base_id", twin._identity())
        return twin

    def _forward_dirty(
        self,
        bond: _Bond,
        dirty: float,
        funding: DiscountFactorCurve,
        basis: float,
        settlement: date | None = None,
    ) -> float:
        """``dirty`` at ``settlement`` (default spot) carried to delivery, coupons removed."""
        delivery = self.request.contract.delivery_date
        if settlement is None:
            settlement = self.request.spot_settlement_date

        def growth(start: date) -> float:
            ratio = funding.discount_factor(start) / funding.discount_factor(delivery)
            return ratio * math.exp(basis * _ACT365.year_fraction(start, delivery))

        return dirty * growth(settlement) - math.fsum(
            amount * growth(paid) for paid, amount in bond.carry_coupons
        )

    def _bond_prices(self, moves: _Moves) -> tuple[float, ...]:
        bond_shifts, funding_shifts, spread_shift, basis_shift = moves
        req = self.request
        bond_view: DiscountFactorCurve = _shock_curve(
            req.bond_curve, self._bond_curve, bond_shifts, self.curve_cache
        )
        funding_view: DiscountFactorCurve = _shock_curve(
            req.funding_curve, self._funding_curve, funding_shifts, self.curve_cache
        )
        basis = (self.basis_bp + basis_shift) * 1e-4
        lot = req.contract.bonds_per_lot
        prices = []
        for bond, spread in zip(self._bonds, self.spreads, strict=True):
            dirty = self._dirty(bond, bond_view, spread + spread_shift * 1e-4)
            forward = self._forward_dirty(bond, dirty, funding_view, basis)
            prices.append(lot * (forward - bond.accrued_delivery) / bond.conversion_factor)
        return tuple(prices)

    def _price(self, moves: _Moves) -> tuple[float, int]:
        cached = self._memo.get(moves)
        if cached is not None:
            return cached
        try:
            prices = self._bond_prices(moves)
        except (OverflowError, ZeroDivisionError) as exc:
            raise ValueError("shock is outside the supported numerical domain") from exc
        index = min(range(len(prices)), key=prices.__getitem__)
        value = (positive_money(prices[index], "OFZ futures price"), index)
        if len(self._memo) < 8192:
            self._memo[moves] = value
        return value

    # -- outputs

    def basket(self) -> tuple[DeliverableAnalysis, ...]:
        """Per-bond carry, implied repo and basis against the futures price."""
        req = self.request
        settlement, delivery = req.spot_settlement_date, req.contract.delivery_date
        futures = req.futures_price if req.futures_price is not None else self.base_price
        lot = req.contract.bonds_per_lot
        theoretical = self._bond_prices(_NO_MOVE)
        cheapest = self._price(_NO_MOVE)[1]
        funding = self._funding_curve
        rows = []
        for i, bond in enumerate(self._bonds):
            invoice = futures / lot * bond.conversion_factor + bond.accrued_delivery
            coupons = math.fsum(a for _, a in bond.carry_coupons)
            weight = bond.dirty_spot * _ACT365.year_fraction(settlement, delivery) - math.fsum(
                a * _ACT365.year_fraction(d, delivery) for d, a in bond.carry_coupons
            )
            implied = (invoice + coupons - bond.dirty_spot) / weight if weight > 0.0 else None
            forward = self._forward_dirty(bond, bond.dirty_spot, funding, self.basis_bp * 1e-4)
            delivered = futures / lot * bond.conversion_factor
            rows.append(
                DeliverableAnalysis(
                    instrument_id=bond.instrument_id,
                    conversion_factor=bond.conversion_factor,
                    clean_price_pct=bond.clean_price_pct,
                    spread_bp=self.spreads[i] * 1e4,
                    dirty_spot_rub=bond.dirty_spot,
                    forward_dirty_rub=forward,
                    accrued_at_delivery_rub=bond.accrued_delivery,
                    theoretical_futures_price=theoretical[i],
                    implied_repo_rate=implied,
                    gross_basis_rub=bond.clean_price_pct / 100.0 * bond.face - delivered,
                    net_basis_rub=forward - bond.accrued_delivery - delivered,
                    cheapest=i == cheapest,
                )
            )
        return tuple(rows)

    def risk(
        self, *, bump_bp: float = 1.0, difference: Literal["forward", "central"] = "forward"
    ) -> OfzFuturesRiskResult:
        """Bucket DV01 on the bond and funding curves, CS01 (all basket spreads) and basis01."""
        bump = positive_money(bump_bp, "risk bump bp")
        if difference not in ("forward", "central"):
            raise ValueError("difference must be forward or central")
        size = len(RISK_BUCKETS)

        def unit(moves: _Moves) -> float:
            up = self._price(moves)[0]
            if difference == "central":
                bond, funding, spread, basis = moves
                down = self._price(
                    (tuple(-s for s in bond), tuple(-s for s in funding), -spread, -basis)
                )[0]
                return finite_money((up - down) / (2.0 * bump), "unit sensitivity")
            return finite_money((up - self.base_price) / bump, "unit sensitivity")

        def buckets(curve_index: int) -> tuple[BucketRisk, ...]:
            rows = []
            for i, bucket in enumerate(RISK_BUCKETS):
                shifts = tuple(bump if i == j else 0.0 for j in range(size))
                moves: _Moves = (
                    (shifts, _ZERO_SHOCK, 0.0, 0.0)
                    if curve_index == 0
                    else (_ZERO_SHOCK, shifts, 0.0, 0.0)
                )
                sensitivity = unit(moves)
                rows.append(
                    BucketRisk(
                        bucket,
                        finite_money(sensitivity * self._scale, "bucket DV01"),
                        finite_money(-sensitivity / self.base_price * 1e4, "price duration"),
                    )
                )
            return tuple(rows)

        parallel = (bump,) * size
        bond_rows, funding_rows = buckets(0), buckets(1)
        analysis = self.basket()
        cheapest = self._bonds[self._price(_NO_MOVE)[1]].instrument_id
        candidates = [
            (a.implied_repo_rate, a.instrument_id)
            for a in analysis
            if a.implied_repo_rate is not None
        ]
        by_repo = max(candidates)[1] if candidates else cheapest
        return OfzFuturesRiskResult(
            base_id=self.base_id,
            contract_id=self.request.contract.contract_id,
            bond_curve_basis=self.bond_curve_basis,
            funding_curve_basis=self.funding_curve_basis,
            difference=difference,
            bump_bp=bump,
            futures_price=self.base_price,
            theoretical_price=self.theoretical_price,
            basis_bp=self.basis_bp,
            cheapest_to_deliver=cheapest,
            cheapest_by_implied_repo=by_repo,
            bond_buckets=bond_rows,
            bond_dv01_rub=finite_money(math.fsum(b.dv01_rub for b in bond_rows), "bond DV01"),
            bond_parallel_dv01_rub=finite_money(
                unit((parallel, _ZERO_SHOCK, 0.0, 0.0)) * self._scale, "bond parallel DV01"
            ),
            funding_buckets=funding_rows,
            funding_dv01_rub=finite_money(
                math.fsum(b.dv01_rub for b in funding_rows), "funding DV01"
            ),
            funding_parallel_dv01_rub=finite_money(
                unit((_ZERO_SHOCK, parallel, 0.0, 0.0)) * self._scale, "funding parallel DV01"
            ),
            cs01_rub=finite_money(
                unit((_ZERO_SHOCK, _ZERO_SHOCK, bump, 0.0)) * self._scale, "CS01"
            ),
            basis01_rub=finite_money(
                unit((_ZERO_SHOCK, _ZERO_SHOCK, 0.0, bump)) * self._scale, "basis01"
            ),
            basket=analysis,
        )

    def reprice(self, scenario: OfzFuturesScenario) -> OfzFuturesScenarioResult:
        """Exact variation margin, factor groups isolated, and the cheapest bond after it."""
        bond, funding = scenario.bond_curve_shifts_bp, scenario.funding_curve_shifts_bp
        spread, basis = scenario.spread_shift_bp, scenario.basis_shift_bp
        _check_scenario_shifts(self.request.bond_curve, bond)
        _check_scenario_shifts(self.request.funding_curve, funding)
        shocked, index = self._price((bond, funding, spread, basis))

        def pnl(moves: _Moves, label: str) -> float:
            return finite_money((self._price(moves)[0] - self.base_price) * self._scale, label)

        parts = (
            pnl((bond, _ZERO_SHOCK, 0.0, 0.0), "bond rates PnL"),
            pnl((_ZERO_SHOCK, funding, 0.0, 0.0), "funding rates PnL"),
            pnl((_ZERO_SHOCK, _ZERO_SHOCK, spread, 0.0), "spread PnL"),
            pnl((_ZERO_SHOCK, _ZERO_SHOCK, 0.0, basis), "basis PnL"),
        )
        total = finite_money((shocked - self.base_price) * self._scale, "total PnL")
        return OfzFuturesScenarioResult(
            base_id=self.base_id,
            scenario_id=canonical_sha256(scenario),
            base_price=self.base_price,
            shocked_price=shocked,
            base_cheapest=self._bonds[self._price(_NO_MOVE)[1]].instrument_id,
            shocked_cheapest=self._bonds[index].instrument_id,
            bond_rates_pnl_rub=parts[0],
            funding_rates_pnl_rub=parts[1],
            spread_pnl_rub=parts[2],
            basis_pnl_rub=parts[3],
            interaction_pnl_rub=finite_money(total - math.fsum(parts), "interaction PnL"),
            total_pnl_rub=total,
        )

    def second_order(self, *, bump_bp: float = 1.0) -> OfzFuturesSecondOrderRisk:
        """Central gradient and full Hessian over 24 factors (11 + 11 buckets, spread, basis)."""
        bump = positive_money(bump_bp, "second-order bump bp")
        size = len(RISK_BUCKETS)

        def price(moves: list[float]) -> float:
            return self._price(
                (
                    tuple(moves[:size]),
                    tuple(moves[size : 2 * size]),
                    moves[2 * size],
                    moves[2 * size + 1],
                )
            )[0]

        gradient, hessian = _central_derivatives(
            price, 2 * size + 2, bump, self.base_price, self._scale
        )
        return OfzFuturesSecondOrderRisk(
            base_id=self.base_id,
            factors=(
                *(f"bond {b}" for b in RISK_BUCKETS),
                *(f"funding {b}" for b in RISK_BUCKETS),
                "spread",
                "basis",
            ),
            bump_bp=bump,
            gradient_rub_per_bp=gradient,
            hessian_rub_per_bp2=hessian,
        )

    # -- horizon

    def _horizon_prices(
        self, settlement: date, bond_view: DiscountFactorCurve, funding_view: DiscountFactorCurve
    ) -> tuple[float, int]:
        """The cheapest price and its index with spot at ``settlement`` on the two views."""
        delivery = self.request.contract.delivery_date
        basis = self.basis_bp * 1e-4
        lot = self.request.contract.bonds_per_lot
        prices = []
        for item, bond, spread in zip(self.request.basket, self._bonds, self.spreads, strict=True):
            flows = tuple(
                (cf.payment_date, cf.amount, _ACT365.year_fraction(settlement, cf.payment_date))
                for cf in item.instrument.cashflows(after=settlement)
            )
            carry = tuple((d, a) for d, a, _ in flows if d <= delivery)
            moved = replace(bond, flows=flows, carry_coupons=carry)
            dirty = self._dirty(moved, bond_view, spread, settlement)
            forward = self._forward_dirty(moved, dirty, funding_view, basis, settlement)
            prices.append(lot * (forward - bond.accrued_delivery) / bond.conversion_factor)
        index = min(range(len(prices)), key=prices.__getitem__)
        return positive_money(prices[index], "OFZ futures price"), index

    def horizon(
        self, horizon_date: date, *, horizon_spot_settlement_date: date
    ) -> OfzFuturesHorizonValuation:
        """Carry and rolldown to ``horizon_date`` (see :class:`OfzFuturesHorizonValuation`).

        ``horizon_spot_settlement_date`` is the basket's spot settlement for the horizon
        trade date (the caller applies the settlement lag). It must precede delivery:
        carry through delivery is not modelled (no OFZ futures are listed since 2022).
        """
        req = self.request
        valuation = self._bond_curve.curve_date
        if not isinstance(horizon_date, date) or horizon_date <= valuation:
            raise InvalidTerms("horizon date must be after the valuation date")
        settlement = horizon_spot_settlement_date
        if not isinstance(settlement, date) or settlement < horizon_date:
            raise InvalidTerms("horizon spot settlement must not precede the horizon date")
        if settlement < req.spot_settlement_date:
            raise InvalidTerms("horizon spot settlement must not precede today's spot settlement")
        if settlement >= req.contract.delivery_date:
            raise InvalidTerms(
                "horizon spot settlement on or after delivery is not supported: "
                "the contract has delivered"
            )
        bond, funding = self._bond_curve, self._funding_curve
        try:
            price_forward, cheapest_forward = self._horizon_prices(
                settlement, forward_curve(bond, horizon_date), forward_curve(funding, horizon_date)
            )
            price_rolled, cheapest_rolled = self._horizon_prices(
                settlement, roll_curve(bond, horizon_date), roll_curve(funding, horizon_date)
            )
        except (OverflowError, ZeroDivisionError) as exc:
            raise ValueError("horizon is outside the supported numerical domain") from exc
        start, scale = self.base_price, self._scale
        ids = [b.instrument_id for b in self._bonds]
        return OfzFuturesHorizonValuation(
            base_id=self.base_id,
            contract_id=req.contract.contract_id,
            valuation_date=valuation,
            horizon_date=horizon_date,
            horizon_spot_settlement_date=settlement,
            quantity=req.quantity,
            price_start=start,
            price_forward=price_forward,
            price_rolled=price_rolled,
            cheapest_start=ids[self._price(_NO_MOVE)[1]],
            cheapest_forward=ids[cheapest_forward],
            cheapest_rolled=ids[cheapest_rolled],
            pv_start_rub=0.0,
            received_rub=0.0,
            carry_rub=finite_money(scale * (price_forward - start), "carry"),
            rolldown_rub=finite_money(scale * (price_rolled - price_forward), "rolldown"),
            horizon_pnl_rub=finite_money(scale * (price_rolled - start), "horizon P&L"),
        )


def ofz_futures_horizon(
    request: OfzFuturesRiskRequest,
    *,
    horizon_date: date,
    horizon_spot_settlement_date: date,
    curve_cache: ShockedCurveCache | None = None,
) -> OfzFuturesHorizonValuation:
    """Carry and rolldown of an OFZ futures position to ``horizon_date``."""
    engine = OfzFuturesRiskEngine(
        request, ShockedCurveCache() if curve_cache is None else curve_cache
    )
    return engine.horizon(horizon_date, horizon_spot_settlement_date=horizon_spot_settlement_date)


def _upper(matrix: tuple[tuple[float, ...], ...]) -> tuple[HessianEntry, ...]:
    return tuple(
        HessianEntry(i, j, row[j])
        for i, row in enumerate(matrix)
        for j in range(i, len(row))
        if row[j] != 0.0
    )


def ofz_futures_factor_risk(
    risk: OfzFuturesRiskResult,
    bond_curve_id: str,
    funding_curve_id: str,
    second_order: OfzFuturesSecondOrderRisk | None = None,
) -> FactorRisk:
    """``IR|<bond curve>|*``, ``IR|<funding curve>|*``, ``CS`` and ``FUTBASIS|<contract>``."""
    if bond_curve_id == funding_curve_id:
        raise InvalidTerms("bond and funding curves need distinct curve IDs")
    factors = (
        *(ir_factor(bond_curve_id, b) for b in RISK_BUCKETS),
        *(ir_factor(funding_curve_id, b) for b in RISK_BUCKETS),
        CS_FACTOR,
        futures_basis_factor(risk.contract_id),
    )
    if second_order is not None:
        if second_order.base_id != risk.base_id:
            raise InvalidTerms("risk and second-order results describe different calibrations")
        return FactorRisk(
            factors,
            second_order.gradient_rub_per_bp,
            _upper(second_order.hessian_rub_per_bp2),
            "central",
            second_order.bump_bp,
            True,
        )
    if risk.difference != "central":
        raise InvalidTerms("desk factors need central-difference risk (DEC08)")
    first = (
        *(b.dv01_rub for b in risk.bond_buckets),
        *(b.dv01_rub for b in risk.funding_buckets),
        risk.cs01_rub,
        risk.basis01_rub,
    )
    return FactorRisk(factors, first, (), "central", risk.bump_bp, False)
