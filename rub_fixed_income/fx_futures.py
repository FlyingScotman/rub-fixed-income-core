"""FX futures against RUB: covered-parity price, rate, basis and spot risk.

Shocks to rates and basis are absolute basis points; the spot shock is a relative
percent. Both discount curves, the spot rate and its value date, and the delivery
date are explicit inputs. Variation margin is daily, so a price change is RUB
P&L on the day and is not discounted. No exchange specification, calendar or
market data is looked up here.

``FxFuturesRiskEngine.horizon`` carries the price to a horizon (BACKLOG #101) on
today's curves read forward and on rolled curves, with the calibrated basis held and
the horizon spot set by a stated ``FxHorizon`` rule (see :class:`FxFuturesHorizonValuation`).
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Literal

from ._curve_views import FxHorizon, fx_horizon, roll_curve
from ._money import finite_money, positive_money
from ._risk_common import (
    _ZERO_SHOCK,
    RISK_BUCKETS,
    ShockedCurveCache,
    _base_curve,
    _central_derivatives,
    _curve_basis,
    _curve_identity,
    _shifts,
    _shock_curve,
    _snapshot_curve,
)
from .canonical import canonical_sha256
from .conventions import DayCount
from .discount import CurveBootstrapRequest, DiscountCurve, DiscountFactorCurve
from .errors import InvalidTerms
from .risk import BucketRisk

FX_FUTURES_RISK_MODEL_VERSION = "rub-fx-futures-parity-risk-v1"
FX_FUTURES_SECOND_ORDER_MODEL_VERSION = "rub-fx-futures-parity-second-order-v1"
FX_FUTURES_HORIZON_MODEL_VERSION = "rub-fx-futures-horizon-v1"


@dataclass(frozen=True)
class FxFuturesContract:
    """Contract terms as the exchange specifies them; nothing is inferred.

    ``contract_size`` is foreign-currency units per contract. ``price_quote_units``
    is how many foreign units the quoted price buys: 1000 when a 1000-unit contract
    is quoted in RUB per 1000, 1 when it is quoted in RUB per unit. ``delivery_date``
    is the value date of the spot rate the final settlement price references.
    """

    contract_id: str
    currency: str
    contract_size: float
    price_quote_units: float
    delivery_date: date

    def __post_init__(self) -> None:
        if not isinstance(self.contract_id, str) or not self.contract_id.strip():
            raise ValueError("contract_id must be nonempty text")
        if (
            not isinstance(self.currency, str)
            or len(self.currency) != 3
            or not self.currency.isascii()
            or not self.currency.isalpha()
            or not self.currency.isupper()
        ):
            raise ValueError("currency must be a three-letter upper-case ISO code")
        if self.currency == "RUB":
            raise ValueError("an FX future needs a foreign currency, not RUB")
        if not isinstance(self.delivery_date, date):
            raise TypeError("delivery_date must be a date")
        object.__setattr__(
            self, "contract_size", positive_money(self.contract_size, "contract size")
        )
        object.__setattr__(
            self,
            "price_quote_units",
            positive_money(self.price_quote_units, "price quote units"),
        )

    @property
    def multiplier(self) -> float:
        """RUB of variation margin per contract per one point of quoted price."""
        return self.contract_size / self.price_quote_units


@dataclass(frozen=True)
class FxFuturesRiskRequest:
    """Prices are RUB per ``price_quote_units`` foreign units; quantity is signed contracts.

    Supply exactly one of ``futures_price`` (market, calibrates the basis) and
    ``basis_bp``. Both curves must be valued on the same date; each curve's type
    selects its risk basis as in ``BondRiskRequest``. ``spot_rate`` is quoted in
    the futures' price units for value date ``spot_value_date``.
    """

    contract: FxFuturesContract
    spot_rate: float
    spot_value_date: date
    rub_curve: DiscountCurve | CurveBootstrapRequest
    foreign_curve: DiscountCurve | CurveBootstrapRequest
    futures_price: float | None = None
    basis_bp: float | None = None
    basis_day_count: DayCount = DayCount.ACT_365_FIXED
    quantity: float = 1.0

    def __post_init__(self) -> None:
        if not isinstance(self.contract, FxFuturesContract):
            raise TypeError("contract must be an FxFuturesContract")
        for name in ("rub_curve", "foreign_curve"):
            if not isinstance(getattr(self, name), (DiscountCurve, CurveBootstrapRequest)):
                raise TypeError(f"{name} must be DiscountCurve or CurveBootstrapRequest")
        if not isinstance(self.basis_day_count, DayCount):
            raise TypeError("basis_day_count must be a DayCount")
        if self.rub_curve.curve_date != self.foreign_curve.curve_date:
            raise ValueError("RUB and foreign curves must share one valuation date")
        if self.spot_value_date < self.rub_curve.curve_date:
            raise ValueError("spot value date cannot predate curve valuation")
        if self.contract.delivery_date <= self.spot_value_date:
            raise ValueError("delivery date must follow the spot value date")
        object.__setattr__(self, "spot_rate", positive_money(self.spot_rate, "spot rate"))
        if (self.futures_price is None) == (self.basis_bp is None):
            raise ValueError("supply exactly one of futures_price and basis_bp")
        if self.futures_price is not None:
            object.__setattr__(
                self, "futures_price", positive_money(self.futures_price, "futures price")
            )
        if self.basis_bp is not None:
            object.__setattr__(self, "basis_bp", finite_money(self.basis_bp, "basis bp"))
        object.__setattr__(self, "quantity", finite_money(self.quantity, "quantity"))
        object.__setattr__(self, "rub_curve", _snapshot_curve(self.rub_curve))
        object.__setattr__(self, "foreign_curve", _snapshot_curve(self.foreign_curve))


@dataclass(frozen=True)
class FxFuturesScenario:
    """Absolute bp shifts per curve bucket and basis; relative percent spot move."""

    rub_curve_shifts_bp: tuple[float, ...] = _ZERO_SHOCK
    foreign_curve_shifts_bp: tuple[float, ...] = _ZERO_SHOCK
    basis_shift_bp: float = 0.0
    fx_spot_shift_pct: float = 0.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "rub_curve_shifts_bp", _shifts(self.rub_curve_shifts_bp))
        object.__setattr__(self, "foreign_curve_shifts_bp", _shifts(self.foreign_curve_shifts_bp))
        object.__setattr__(
            self, "basis_shift_bp", finite_money(self.basis_shift_bp, "basis shift bp")
        )
        spot = finite_money(self.fx_spot_shift_pct, "FX spot shift percent")
        if spot <= -100.0:
            raise ValueError("FX spot shift must stay above -100 percent")
        object.__setattr__(self, "fx_spot_shift_pct", spot)

    @classmethod
    def parallel(
        cls,
        *,
        rub_shift_bp: float = 0.0,
        foreign_shift_bp: float = 0.0,
        basis_shift_bp: float = 0.0,
        fx_spot_shift_pct: float = 0.0,
    ) -> FxFuturesScenario:
        size = len(RISK_BUCKETS)
        return cls(
            (rub_shift_bp,) * size, (foreign_shift_bp,) * size, basis_shift_bp, fx_spot_shift_pct
        )


@dataclass(frozen=True)
class FxFuturesRiskResult:
    """Signed RUB variation-margin sensitivities of the position.

    ``krd_years`` in a bucket is ``-(1/F) dF/dr * 1e4`` for the futures price F:
    about minus the carry time for RUB rates and plus it for foreign rates.
    """

    base_id: str
    contract_id: str
    currency: str
    rub_curve_basis: str
    foreign_curve_basis: str
    difference: str
    bump_bp: float
    futures_price: float
    theoretical_price: float
    spot_rate: float
    basis_bp: float
    carry_years: float
    notional_rub: float
    fx_delta_foreign: float
    fx_delta_rub_per_pct: float
    rub_buckets: tuple[BucketRisk, ...]
    rub_dv01_rub: float
    rub_parallel_dv01_rub: float
    foreign_buckets: tuple[BucketRisk, ...]
    foreign_dv01_rub: float
    foreign_parallel_dv01_rub: float
    basis01_rub: float
    model_version: str = FX_FUTURES_RISK_MODEL_VERSION


@dataclass(frozen=True)
class FxFuturesScenarioResult:
    base_id: str
    scenario_id: str
    rub_curve_basis: str
    foreign_curve_basis: str
    base_price: float
    shocked_price: float
    rub_rates_pnl_rub: float
    foreign_rates_pnl_rub: float
    basis_pnl_rub: float
    fx_spot_pnl_rub: float
    interaction_pnl_rub: float
    total_pnl_rub: float
    model_version: str = FX_FUTURES_RISK_MODEL_VERSION


@dataclass(frozen=True)
class FxFuturesSecondOrderRisk:
    """Central gradient (RUB/bp) and full symmetric Hessian (RUB/bp squared)."""

    base_id: str
    rub_curve_basis: str
    foreign_curve_basis: str
    factors: tuple[str, ...]
    bump_bp: float
    gradient_rub_per_bp: tuple[float, ...]
    hessian_rub_per_bp2: tuple[tuple[float, ...], ...]
    model_version: str = FX_FUTURES_SECOND_ORDER_MODEL_VERSION


@dataclass(frozen=True)
class FxFuturesHorizonValuation:
    """Carry, rolldown and theta of the position from the valuation date to ``horizon_date``.

    The price at the horizon is ``S_h * growth(s_h, T) * exp(b * tau(s_h, T))`` with the
    calibrated basis ``b`` held, on today's curves read forward (``price_forward``) or on
    rolled curves (``price_rolled``, shape held in calendar days: the theta). ``S_h`` is
    ``horizon_spot_rate`` for ``horizon_spot_value_date``: today's spot (``"spot_held"``)
    or today's parity forward for that date without the futures basis
    (``"forwards_realised"``), since the basis belongs to the contract. A contract
    delivered by the horizon has settled at the FX fixing: today's spot, or today's
    parity forward to delivery (convergence: the basis is gone).

    A position is margined, so ``pv_start_rub`` and ``received_rub`` are zero and the
    variation margin, ``quantity * multiplier`` times the price change, is the carry
    (``price_forward - price_start``) plus the rolldown (``price_rolled - price_forward``).
    """

    base_id: str
    contract_id: str
    valuation_date: date
    horizon_date: date
    fx_horizon: FxHorizon
    spot_rate: float
    horizon_spot_rate: float
    horizon_spot_value_date: date
    quantity: float
    price_start: float
    price_forward: float
    price_rolled: float
    pv_start_rub: float
    received_rub: float
    carry_rub: float
    rolldown_rub: float
    horizon_pnl_rub: float
    model_version: str = FX_FUTURES_HORIZON_MODEL_VERSION


@dataclass(frozen=True)
class _HorizonView:
    """Discount factors seen from the horizon ``curve_date`` on a base curve dated earlier.

    The forward view (``rolled`` None) reads the base curve forward, ``DF(t) / DF(h)``;
    the rolled view reads ``rolled`` (``roll_curve``: shape held in calendar days). A
    date before the horizon (the value date of a cash-settled FX leg fixed by then)
    reads the base curve forward on both views, as swap fixings before the horizon
    keep today's forwards.
    """

    curve_date: date
    base: DiscountCurve
    rolled: DiscountCurve | None = None

    def discount_factor(self, target: date) -> float:
        if self.rolled is not None and target >= self.curve_date:
            return self.rolled.discount_factor(target)
        return self.base.discount_factor(target) / self.base.discount_factor(self.curve_date)


def _horizon_views(curve: DiscountCurve, horizon: date) -> tuple[_HorizonView, _HorizonView]:
    """(forward view, rolled view) of a base curve at ``horizon``."""
    return _HorizonView(horizon, curve), _HorizonView(horizon, curve, roll_curve(curve, horizon))


def _horizon_rule(
    valuation: date,
    spot_value_date: date,
    horizon_date: date,
    horizon_spot_value_date: date,
    rule: object,
) -> FxHorizon:
    """The checked FX horizon rule; the horizon and its spot date must follow today's."""
    checked = fx_horizon(rule)
    if not isinstance(horizon_date, date) or horizon_date <= valuation:
        raise InvalidTerms("horizon date must be after the valuation date")
    if not isinstance(horizon_spot_value_date, date):
        raise InvalidTerms("horizon spot value date must be a date")
    if horizon_spot_value_date < horizon_date:
        raise InvalidTerms("the horizon spot value date cannot precede the horizon date")
    if horizon_spot_value_date < spot_value_date:
        raise InvalidTerms("the horizon spot value date cannot precede today's spot value date")
    return checked


def _parity_growth(
    rub: DiscountFactorCurve, foreign: DiscountFactorCurve, start: date, end: date
) -> float:
    """``[DF_for(end) / DF_for(start)] / [DF_rub(end) / DF_rub(start)]`` (no basis)."""
    return (
        foreign.discount_factor(end)
        / foreign.discount_factor(start)
        * (rub.discount_factor(start) / rub.discount_factor(end))
    )


_Moves = tuple[tuple[float, ...], tuple[float, ...], float, float]
_NO_MOVE: _Moves = (_ZERO_SHOCK, _ZERO_SHOCK, 0.0, 0.0)


@dataclass(frozen=True)
class FxFuturesRiskEngine:
    """Calibrate the basis once, then hold it fixed across every rate and spot shock.

    ``F = S * [DF_for(T) / DF_for(s)] / [DF_rub(T) / DF_rub(s)] * exp(b * tau(s, T))``
    with spot value date s, delivery date T and basis b in ``basis_day_count``.
    """

    request: FxFuturesRiskRequest
    curve_cache: ShockedCurveCache = field(
        default_factory=ShockedCurveCache, compare=False, repr=False
    )
    basis_bp: float = field(init=False)
    carry_years: float = field(init=False)
    theoretical_price: float = field(init=False)
    base_price: float = field(init=False)
    base_id: str = field(init=False)
    _rub_curve: DiscountCurve = field(init=False, repr=False)
    _foreign_curve: DiscountCurve = field(init=False, repr=False)

    def __post_init__(self) -> None:
        req = self.request
        if not isinstance(self.curve_cache, ShockedCurveCache):
            raise TypeError("curve_cache must be a ShockedCurveCache")
        object.__setattr__(self, "_rub_curve", _base_curve(req.rub_curve, self.curve_cache))
        object.__setattr__(self, "_foreign_curve", _base_curve(req.foreign_curve, self.curve_cache))
        carry = positive_money(
            req.basis_day_count.year_fraction(req.spot_value_date, req.contract.delivery_date),
            "basis accrual time",
        )
        object.__setattr__(self, "carry_years", carry)
        object.__setattr__(self, "basis_bp", 0.0)
        theoretical = self._price(_NO_MOVE)
        object.__setattr__(self, "theoretical_price", theoretical)
        basis = req.basis_bp
        if basis is None:
            assert req.futures_price is not None
            basis = math.log(req.futures_price / theoretical) / carry * 10_000.0
        object.__setattr__(self, "basis_bp", finite_money(basis, "calibrated basis"))
        price = self._price(_NO_MOVE)
        object.__setattr__(self, "base_price", price)
        if req.futures_price is not None and not math.isclose(
            price, req.futures_price, rel_tol=1e-12, abs_tol=0.0
        ):
            raise ValueError("basis calibration failed to reprice the futures price")
        contract = asdict(req.contract)
        contract["multiplier"] = req.contract.multiplier
        object.__setattr__(
            self,
            "base_id",
            canonical_sha256(
                {
                    "model": FX_FUTURES_RISK_MODEL_VERSION,
                    "contract": contract,
                    "spot_rate": req.spot_rate,
                    "spot_value_date": req.spot_value_date,
                    "rub_curve_basis": self.rub_curve_basis,
                    "rub_curve": _curve_identity(req.rub_curve),
                    "foreign_curve_basis": self.foreign_curve_basis,
                    "foreign_curve": _curve_identity(req.foreign_curve),
                    "input_futures_price": req.futures_price,
                    "input_basis_bp": req.basis_bp,
                    "calibrated_basis_bp": basis,
                    "basis_day_count": req.basis_day_count.value,
                    "quantity": req.quantity,
                }
            ),
        )

    @property
    def rub_curve_basis(self) -> str:
        return _curve_basis(self.request.rub_curve)

    @property
    def foreign_curve_basis(self) -> str:
        return _curve_basis(self.request.foreign_curve)

    @property
    def _scale(self) -> float:
        """RUB of position P&L per point of futures price."""
        return self.request.quantity * self.request.contract.multiplier

    def _price(self, moves: _Moves) -> float:
        rub_shifts, foreign_shifts, basis_shift, spot_shift = moves
        req = self.request
        start, end = req.spot_value_date, req.contract.delivery_date
        try:
            rub: DiscountFactorCurve = _shock_curve(
                req.rub_curve, self._rub_curve, rub_shifts, self.curve_cache
            )
            foreign: DiscountFactorCurve = _shock_curve(
                req.foreign_curve, self._foreign_curve, foreign_shifts, self.curve_cache
            )
            rub_growth = rub.discount_factor(start) / rub.discount_factor(end)
            foreign_growth = foreign.discount_factor(start) / foreign.discount_factor(end)
            spot = req.spot_rate * (1.0 + spot_shift / 100.0)
            basis = finite_money(self.basis_bp + basis_shift, "shocked basis") * 1e-4
            return positive_money(
                spot * rub_growth / foreign_growth * math.exp(basis * self.carry_years),
                "futures price",
            )
        except (OverflowError, ZeroDivisionError) as exc:
            raise ValueError("shock is outside the supported numerical domain") from exc

    def risk(
        self, *, bump_bp: float = 1.0, difference: Literal["forward", "central"] = "forward"
    ) -> FxFuturesRiskResult:
        """Signed RUB/bp per bucket of each curve, parallel per curve and basis.

        Forward is the exact +1bp variation margin; central estimates a derivative.
        Bucket sums and independently repriced parallel shifts are both reported.
        """
        bump = positive_money(bump_bp, "risk bump bp")
        if difference not in ("forward", "central"):
            raise ValueError("difference must be forward or central")
        size = len(RISK_BUCKETS)

        def sensitivity(moves: _Moves) -> float:
            up = self._price(moves)
            if difference == "central":
                rub, foreign, basis, spot = moves
                down = self._price(
                    (tuple(-s for s in rub), tuple(-s for s in foreign), -basis, spot)
                )
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
                unit = sensitivity(moves)
                rows.append(
                    BucketRisk(
                        bucket,
                        finite_money(unit * self._scale, "bucket DV01"),
                        finite_money(-unit / self.base_price * 10_000.0, "price rate duration"),
                    )
                )
            return tuple(rows)

        parallel = (bump,) * size
        rub_buckets = buckets(0)
        foreign_buckets = buckets(1)
        spot_up = self._price((_ZERO_SHOCK, _ZERO_SHOCK, 0.0, 1.0))
        return FxFuturesRiskResult(
            base_id=self.base_id,
            contract_id=self.request.contract.contract_id,
            currency=self.request.contract.currency,
            rub_curve_basis=self.rub_curve_basis,
            foreign_curve_basis=self.foreign_curve_basis,
            difference=difference,
            bump_bp=bump,
            futures_price=self.base_price,
            theoretical_price=self.theoretical_price,
            spot_rate=self.request.spot_rate,
            basis_bp=self.basis_bp,
            carry_years=self.carry_years,
            notional_rub=finite_money(self.base_price * self._scale, "notional"),
            fx_delta_foreign=finite_money(
                self.request.quantity
                * self.request.contract.contract_size
                * self.base_price
                / self.request.spot_rate,
                "FX delta",
            ),
            fx_delta_rub_per_pct=finite_money(
                (spot_up - self.base_price) * self._scale, "FX delta RUB"
            ),
            rub_buckets=rub_buckets,
            rub_dv01_rub=finite_money(math.fsum(b.dv01_rub for b in rub_buckets), "RUB DV01"),
            rub_parallel_dv01_rub=finite_money(
                sensitivity((parallel, _ZERO_SHOCK, 0.0, 0.0)) * self._scale, "RUB parallel DV01"
            ),
            foreign_buckets=foreign_buckets,
            foreign_dv01_rub=finite_money(
                math.fsum(b.dv01_rub for b in foreign_buckets), "foreign DV01"
            ),
            foreign_parallel_dv01_rub=finite_money(
                sensitivity((_ZERO_SHOCK, parallel, 0.0, 0.0)) * self._scale,
                "foreign parallel DV01",
            ),
            basis01_rub=finite_money(
                sensitivity((_ZERO_SHOCK, _ZERO_SHOCK, bump, 0.0)) * self._scale, "basis01"
            ),
        )

    def reprice(self, scenario: FxFuturesScenario) -> FxFuturesScenarioResult:
        """Exact variation margin, each factor group isolated, and their interaction."""
        rub, foreign = scenario.rub_curve_shifts_bp, scenario.foreign_curve_shifts_bp
        basis, spot = scenario.basis_shift_bp, scenario.fx_spot_shift_pct
        shocked = self._price((rub, foreign, basis, spot))

        def pnl(moves: _Moves, label: str) -> float:
            return finite_money((self._price(moves) - self.base_price) * self._scale, label)

        rub_pnl = pnl((rub, _ZERO_SHOCK, 0.0, 0.0), "RUB rates PnL")
        foreign_pnl = pnl((_ZERO_SHOCK, foreign, 0.0, 0.0), "foreign rates PnL")
        basis_pnl = pnl((_ZERO_SHOCK, _ZERO_SHOCK, basis, 0.0), "basis PnL")
        spot_pnl = pnl((_ZERO_SHOCK, _ZERO_SHOCK, 0.0, spot), "FX spot PnL")
        total = finite_money((shocked - self.base_price) * self._scale, "total PnL")
        return FxFuturesScenarioResult(
            base_id=self.base_id,
            scenario_id=canonical_sha256(scenario),
            rub_curve_basis=self.rub_curve_basis,
            foreign_curve_basis=self.foreign_curve_basis,
            base_price=self.base_price,
            shocked_price=shocked,
            rub_rates_pnl_rub=rub_pnl,
            foreign_rates_pnl_rub=foreign_pnl,
            basis_pnl_rub=basis_pnl,
            fx_spot_pnl_rub=spot_pnl,
            interaction_pnl_rub=finite_money(
                total - math.fsum((rub_pnl, foreign_pnl, basis_pnl, spot_pnl)),
                "interaction PnL",
            ),
            total_pnl_rub=total,
        )

    def second_order(self, *, bump_bp: float = 1.0) -> FxFuturesSecondOrderRisk:
        """Full central gradient and Hessian over 11 RUB, 11 foreign and the basis factor.

        Spot is not a factor: the price is linear in spot, so every rate and basis
        derivative scales by exactly ``1 + x`` under a relative spot move x.
        """
        bump = positive_money(bump_bp, "second-order bump bp")
        size = len(RISK_BUCKETS)

        def price(moves: list[float]) -> float:
            return self._price(
                (tuple(moves[:size]), tuple(moves[size : 2 * size]), moves[2 * size], 0.0)
            )

        gradient, hessian = _central_derivatives(
            price, 2 * size + 1, bump, self.base_price, self._scale
        )
        currency = self.request.contract.currency
        return FxFuturesSecondOrderRisk(
            base_id=self.base_id,
            rub_curve_basis=self.rub_curve_basis,
            foreign_curve_basis=self.foreign_curve_basis,
            factors=(
                *(f"RUB {b}" for b in RISK_BUCKETS),
                *(f"{currency} {b}" for b in RISK_BUCKETS),
                "basis",
            ),
            bump_bp=bump,
            gradient_rub_per_bp=gradient,
            hessian_rub_per_bp2=hessian,
        )

    def horizon(
        self,
        horizon_date: date,
        *,
        horizon_spot_value_date: date,
        fx_horizon: FxHorizon = "spot_held",
    ) -> FxFuturesHorizonValuation:
        """Carry, rolldown and theta to ``horizon_date`` (see :class:`FxFuturesHorizonValuation`).

        ``horizon_spot_value_date`` is the spot value date on the horizon date (the
        caller applies the spot lag); it must not precede the horizon or today's spot date.
        """
        req = self.request
        rub, foreign = self._rub_curve, self._foreign_curve
        spot, spot_date = req.spot_rate, req.spot_value_date
        horizon_spot_date, delivery = horizon_spot_value_date, req.contract.delivery_date
        rule = _horizon_rule(rub.curve_date, spot_date, horizon_date, horizon_spot_date, fx_horizon)
        held = rule == "spot_held"
        horizon_spot = positive_money(
            spot if held else spot * _parity_growth(rub, foreign, spot_date, horizon_spot_date),
            "horizon spot rate",
        )
        if delivery <= horizon_date:
            # Settled at the FX fixing for the delivery date: the basis has converged.
            settled = spot if held else spot * _parity_growth(rub, foreign, spot_date, delivery)
            price_forward = price_rolled = positive_money(settled, "settlement price")
        else:
            day_count = req.basis_day_count
            tau = (
                day_count.year_fraction(horizon_spot_date, delivery)
                if delivery >= horizon_spot_date
                else -day_count.year_fraction(delivery, horizon_spot_date)
            )
            basis = math.exp(self.basis_bp * 1e-4 * tau)
            rub_forward, rub_rolled = _horizon_views(rub, horizon_date)
            foreign_forward, foreign_rolled = _horizon_views(foreign, horizon_date)

            def price(rub_view: _HorizonView, foreign_view: _HorizonView, label: str) -> float:
                growth = _parity_growth(rub_view, foreign_view, horizon_spot_date, delivery)
                return positive_money(horizon_spot * growth * basis, label)

            price_forward = price(rub_forward, foreign_forward, "forward futures price")
            price_rolled = price(rub_rolled, foreign_rolled, "rolled futures price")
        start, scale = self.base_price, self._scale
        return FxFuturesHorizonValuation(
            base_id=self.base_id,
            contract_id=req.contract.contract_id,
            valuation_date=rub.curve_date,
            horizon_date=horizon_date,
            fx_horizon=rule,
            spot_rate=spot,
            horizon_spot_rate=horizon_spot,
            horizon_spot_value_date=horizon_spot_date,
            quantity=req.quantity,
            price_start=start,
            price_forward=price_forward,
            price_rolled=price_rolled,
            pv_start_rub=0.0,
            received_rub=0.0,
            carry_rub=finite_money(scale * (price_forward - start), "carry"),
            rolldown_rub=finite_money(scale * (price_rolled - price_forward), "rolldown"),
            horizon_pnl_rub=finite_money(scale * (price_rolled - start), "horizon P&L"),
        )


def fx_futures_horizon(
    request: FxFuturesRiskRequest,
    *,
    horizon_date: date,
    horizon_spot_value_date: date,
    fx_horizon: FxHorizon = "spot_held",
    curve_cache: ShockedCurveCache | None = None,
) -> FxFuturesHorizonValuation:
    """Carry, rolldown and theta of an FX futures position to ``horizon_date``."""
    engine = FxFuturesRiskEngine(
        request, ShockedCurveCache() if curve_cache is None else curve_cache
    )
    return engine.horizon(
        horizon_date, horizon_spot_value_date=horizon_spot_value_date, fx_horizon=fx_horizon
    )
