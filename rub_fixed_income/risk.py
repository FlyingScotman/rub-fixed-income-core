"""RUB fixed-cashflow risk and same-settlement full revaluation.

Shocks are absolute basis points. Spread compounding and the curve risk basis
are explicit inputs. No market access, position storage, or implicit FX conversion.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from datetime import date
from itertools import pairwise
from typing import Literal

from ._money import finite_money, non_negative_money, positive_money
from .analytics import canonical_sha256
from .cashflows import BondCashflow
from .compatibility import price_cashflows, solve_z_spread
from .conventions import CurveConvention, ZSpreadConvention
from .discount import (
    CurveBootstrapRequest,
    DiscountCurve,
    DiscountFactorCurve,
    bootstrap_discount_curve,
)

RISK_BUCKETS = ("1M", "3M", "6M", "1Y", "2Y", "3Y", "5Y", "7Y", "10Y", "15Y", "20Y+")
RISK_PILLARS = (1 / 12, 0.25, 0.5, 1.0, 2.0, 3.0, 5.0, 7.0, 10.0, 15.0, 20.0)
RISK_MODEL_VERSION = "rub-fixed-cashflow-risk-v1"
_ZERO_SHOCK = (0.0,) * len(RISK_BUCKETS)


def _shifts(values: tuple[float, ...]) -> tuple[float, ...]:
    if len(values) != len(RISK_BUCKETS):
        raise ValueError("curve shock must contain exactly 11 bucket shifts")
    return tuple(finite_money(value, "curve shift bp") for value in values)


def key_rate_weights(time_years: float) -> tuple[float, ...]:
    """Linear hats, flat left/right tails; weights sum to one even beyond 20Y."""
    time = non_negative_money(time_years, "curve time")
    weights = [0.0] * len(RISK_PILLARS)
    if time <= RISK_PILLARS[0]:
        weights[0] = 1.0
    elif time >= RISK_PILLARS[-1]:
        weights[-1] = 1.0
    else:
        for i, (left, right) in enumerate(pairwise(RISK_PILLARS)):
            if left <= time <= right:
                fraction = (time - left) / (right - left)
                weights[i], weights[i + 1] = 1.0 - fraction, fraction
                break
    return tuple(weights)


def _shift_at(time: float, shifts: tuple[float, ...]) -> float:
    return finite_money(
        math.fsum(w * s for w, s in zip(key_rate_weights(time), shifts)), "interpolated shift"
    )


@dataclass(frozen=True)
class _ZeroShockCurve:
    base: DiscountCurve
    shifts: tuple[float, ...]

    @property
    def curve_date(self) -> date:
        return self.base.curve_date

    def discount_factor(self, target: date) -> float:
        time = self.base.convention.time_day_count.year_fraction(self.curve_date, target)
        shift = _shift_at(time, self.shifts) * 1e-4
        try:
            result = self.base.discount_factor(target) * math.exp(-shift * time)
        except OverflowError as exc:
            raise ValueError("shocked discount factor must be positive and finite") from exc
        return positive_money(result, "shocked discount factor")


def _snapshot_convention(convention: CurveConvention) -> CurveConvention:
    schedule = convention.fixed_leg_schedule
    if schedule is None:
        return convention
    calendar = replace(
        schedule.calendar,
        holidays=frozenset(schedule.calendar.holidays),
        working_days=frozenset(schedule.calendar.working_days),
    )
    return replace(convention, fixed_leg_schedule=replace(schedule, calendar=calendar))


@dataclass(frozen=True)
class BondRiskRequest:
    """Amounts are RUB per bond; quantity is signed bonds, price is dirty RUB/bond.

    Supply exactly one of spread_bp and dirty_price_rub. A DiscountCurve selects
    continuous-zero risk; a CurveBootstrapRequest selects par-OIS quote risk.
    Payments on/before settlement are excluded. Put/amortization schedules must
    already contain the contractual redemption and no post-horizon payments.
    """

    cashflows: tuple[BondCashflow, ...]
    settlement_date: date
    curve: DiscountCurve | CurveBootstrapRequest
    spread_convention: ZSpreadConvention
    spread_kind: Literal["credit", "ofz"]
    spread_bp: float | None = None
    dirty_price_rub: float | None = None
    quantity: float = 1.0
    currency: Literal["RUB"] = "RUB"

    def __post_init__(self) -> None:
        if self.currency != "RUB":
            raise ValueError("fixed-cashflow risk supports RUB only; convert explicitly upstream")
        if self.spread_kind not in ("credit", "ofz"):
            raise ValueError("spread_kind must be credit or ofz")
        if not isinstance(self.curve, (DiscountCurve, CurveBootstrapRequest)):
            raise TypeError("curve must be DiscountCurve or CurveBootstrapRequest")
        if not isinstance(self.spread_convention, ZSpreadConvention):
            raise TypeError("an explicit ZSpreadConvention is required")
        if self.settlement_date < self.curve.curve_date:
            raise ValueError("settlement cannot predate curve valuation")
        if (self.spread_bp is None) == (self.dirty_price_rub is None):
            raise ValueError("supply exactly one of spread_bp and dirty_price_rub")
        if self.spread_bp is not None:
            object.__setattr__(self, "spread_bp", finite_money(self.spread_bp, "spread bp"))
        if self.dirty_price_rub is not None:
            object.__setattr__(
                self, "dirty_price_rub", positive_money(self.dirty_price_rub, "dirty price")
            )
        object.__setattr__(self, "quantity", finite_money(self.quantity, "quantity"))
        flows = tuple(
            BondCashflow(flow.payment_date, non_negative_money(flow.amount, "bond cashflow"))
            for flow in self.cashflows
        )
        if not any(f.payment_date > self.settlement_date and f.amount > 0.0 for f in flows):
            raise ValueError("at least one positive future bond cashflow is required")
        object.__setattr__(self, "cashflows", tuple(sorted(flows, key=lambda f: f.payment_date)))
        # Copy collection inputs so later caller mutations cannot change a calibrated state.
        object.__setattr__(
            self,
            "curve",
            replace(self.curve, convention=_snapshot_convention(self.curve.convention)),
        )
        if isinstance(self.curve, CurveBootstrapRequest):
            quotes = tuple(sorted(self.curve.quotes, key=lambda q: q.maturity))
            for quote in quotes:
                finite_money(quote.par_rate, "OIS par rate")
            object.__setattr__(self, "curve", replace(self.curve, quotes=quotes))
        else:
            nodes = tuple((d, positive_money(df, "discount factor")) for d, df in self.curve.nodes)
            if self.curve._flat_continuous_rate is not None:
                finite_money(self.curve._flat_continuous_rate, "flat continuous rate")
            object.__setattr__(self, "curve", replace(self.curve, nodes=nodes))


@dataclass(frozen=True)
class BucketRisk:
    bucket: str
    dv01_rub: float
    krd_years: float


@dataclass(frozen=True)
class BondRiskResult:
    base_id: str
    curve_basis: str
    spread_convention: str
    difference: str
    bump_bp: float
    pv_rub: float
    spread_bp: float
    buckets: tuple[BucketRisk, ...]
    dv01_rub: float
    parallel_dv01_rub: float
    cs01_rub: float
    ofz_spr01_rub: float
    model_version: str = RISK_MODEL_VERSION


@dataclass(frozen=True)
class BondScenario:
    curve_shifts_bp: tuple[float, ...] = _ZERO_SHOCK
    spread_shift_bp: float = 0.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "curve_shifts_bp", _shifts(self.curve_shifts_bp))
        object.__setattr__(
            self, "spread_shift_bp", finite_money(self.spread_shift_bp, "spread shift bp")
        )

    @classmethod
    def parallel(cls, curve_shift_bp: float, *, spread_shift_bp: float = 0.0) -> BondScenario:
        return cls((curve_shift_bp,) * len(RISK_BUCKETS), spread_shift_bp)


@dataclass(frozen=True)
class BondScenarioResult:
    base_id: str
    scenario_id: str
    curve_basis: str
    spread_convention: str
    base_pv_rub: float
    shocked_pv_rub: float
    rates_pnl_rub: float
    spread_pnl_rub: float
    interaction_pnl_rub: float
    total_pnl_rub: float
    model_version: str = RISK_MODEL_VERSION


@dataclass(frozen=True)
class BondSecondOrderRisk:
    """Central gradient (RUB/bp) and full symmetric Hessian (RUB/bp squared)."""

    base_id: str
    curve_basis: str
    spread_convention: str
    factors: tuple[str, ...]
    bump_bp: float
    gradient_rub_per_bp: tuple[float, ...]
    hessian_rub_per_bp2: tuple[tuple[float, ...], ...]
    model_version: str = "rub-fixed-cashflow-second-order-v1"


@dataclass(frozen=True)
class BondScenarioApproximation:
    base_id: str
    scenario_id: str
    curve_basis: str
    second_order_id: str
    linear_pnl_rub: float
    rates_gamma_pnl_rub: float
    spread_gamma_pnl_rub: float
    rates_spread_cross_pnl_rub: float
    approximate_pnl_rub: float
    exact_pnl_rub: float
    residual_pnl_rub: float


@dataclass(frozen=True)
class BondDefaultResult:
    """Immediate recovery of outstanding principal; signed dirty-PV change."""

    base_id: str
    default_id: str
    outstanding_nominal_rub_per_bond: float
    recovery_rate: float
    base_pv_rub: float
    recovery_pv_rub: float
    jtd_pnl_rub: float
    model_version: str = "bond-immediate-principal-recovery-v1"


def _curve_identity(curve: DiscountCurve | CurveBootstrapRequest) -> dict[str, object]:
    payload: dict[str, object] = {
        "curve_date": curve.curve_date,
        "convention": curve.convention.identifier,
    }
    if isinstance(curve, DiscountCurve):
        payload.update(
            nodes=curve.nodes, flat_rate=curve._flat_continuous_rate, model=curve.model_version
        )
    else:
        payload["quotes"] = [(q.tenor, q.maturity, q.par_rate) for q in curve.quotes]
    return payload


@dataclass(frozen=True)
class BondRiskEngine:
    """Calibrate once, then hold the base spread fixed across every curve shock."""

    request: BondRiskRequest
    spread_bp: float = field(init=False)
    base_price_rub: float = field(init=False)
    base_id: str = field(init=False)
    _curve: DiscountCurve = field(init=False, repr=False)

    def __post_init__(self) -> None:
        req = self.request
        curve = (
            bootstrap_discount_curve(req.curve)
            if isinstance(req.curve, CurveBootstrapRequest)
            else req.curve
        )
        object.__setattr__(self, "_curve", curve)
        spread = req.spread_bp
        if spread is None:
            assert req.dirty_price_rub is not None
            spread = (
                solve_z_spread(
                    self._flows(),
                    req.dirty_price_rub,
                    curve,
                    settlement_date=req.settlement_date,
                    convention=req.spread_convention,
                )
                * 10_000.0
            )
        object.__setattr__(self, "spread_bp", finite_money(spread, "calibrated spread"))
        price = self._price(_ZERO_SHOCK, 0.0)
        object.__setattr__(self, "base_price_rub", positive_money(price, "base price"))
        if req.dirty_price_rub is not None and not math.isclose(
            price, req.dirty_price_rub, rel_tol=1e-10, abs_tol=1e-8
        ):
            raise ValueError("base spread calibration failed to reprice the dirty price")
        object.__setattr__(
            self,
            "base_id",
            canonical_sha256(
                {
                    "model": RISK_MODEL_VERSION,
                    "curve_basis": self.curve_basis,
                    "curve": _curve_identity(req.curve),
                    "cashflows": self._flows(),
                    "settlement": req.settlement_date,
                    "spread_convention": req.spread_convention.identifier,
                    "spread_kind": req.spread_kind,
                    "input_spread_bp": req.spread_bp,
                    "input_dirty_price": req.dirty_price_rub,
                    "calibrated_spread_bp": spread,
                    "quantity": req.quantity,
                    "currency": req.currency,
                }
            ),
        )

    @property
    def curve_basis(self) -> str:
        return (
            "ois_quote"
            if isinstance(self.request.curve, CurveBootstrapRequest)
            else "continuous_zero"
        )

    def _flows(self) -> tuple[tuple[date, float], ...]:
        return tuple((f.payment_date, f.amount) for f in self.request.cashflows)

    def _shocked_curve(self, shifts: tuple[float, ...]) -> DiscountFactorCurve:
        if not any(shifts):
            return self._curve
        source = self.request.curve
        if isinstance(source, CurveBootstrapRequest):
            quotes = tuple(
                replace(
                    q,
                    par_rate=finite_money(
                        q.par_rate
                        + _shift_at(
                            source.convention.time_day_count.year_fraction(
                                source.curve_date, q.maturity
                            ),
                            shifts,
                        )
                        * 1e-4,
                        "shocked OIS quote",
                    ),
                )
                for q in source.quotes
            )
            return bootstrap_discount_curve(replace(source, quotes=quotes))
        return _ZeroShockCurve(self._curve, shifts)

    def _price(self, shifts: tuple[float, ...], spread_shift: float) -> float:
        try:
            return positive_money(
                price_cashflows(
                    self._flows(),
                    self._shocked_curve(shifts),
                    spread=finite_money(self.spread_bp + spread_shift, "shocked spread") * 1e-4,
                    settlement_date=self.request.settlement_date,
                    convention=self.request.spread_convention,
                ),
                "bond price",
            )
        except (OverflowError, ZeroDivisionError) as exc:
            raise ValueError("shock is outside the supported numerical domain") from exc

    def risk(
        self, *, bump_bp: float = 1.0, difference: Literal["forward", "central"] = "forward"
    ) -> BondRiskResult:
        """Signed RUB/bp. Default is exact +1bp P&L; central is a derivative estimate.

        dv01 is the bucket sum. parallel_dv01 is independently repriced: finite
        bump convexity means these need not be identical. KRD = -unit_dv01/P/1e-4.
        """
        bump = positive_money(bump_bp, "risk bump bp")
        if difference not in ("forward", "central"):
            raise ValueError("difference must be forward or central")

        def sensitivity(shifts: tuple[float, ...], spread: float) -> float:
            up = self._price(shifts, spread)
            down = (
                self._price(tuple(-s for s in shifts), -spread)
                if difference == "central"
                else self.base_price_rub
            )
            denominator = 2.0 * bump if difference == "central" else bump
            return finite_money((up - down) / denominator, "unit sensitivity")

        quantity = self.request.quantity
        buckets = []
        for i, bucket in enumerate(RISK_BUCKETS):
            shifts = tuple(bump if i == j else 0.0 for j in range(len(RISK_BUCKETS)))
            unit = sensitivity(shifts, 0.0)
            buckets.append(
                BucketRisk(
                    bucket,
                    finite_money(unit * quantity, "bucket DV01"),
                    finite_money(-unit / self.base_price_rub * 10_000.0, "key rate duration"),
                )
            )
        parallel = finite_money(sensitivity((bump,) * len(RISK_BUCKETS), 0.0) * quantity, "DV01")
        spread = finite_money(sensitivity(_ZERO_SHOCK, bump) * quantity, "spread sensitivity")
        return BondRiskResult(
            base_id=self.base_id,
            curve_basis=self.curve_basis,
            spread_convention=self.request.spread_convention.identifier,
            difference=difference,
            bump_bp=bump,
            pv_rub=finite_money(self.base_price_rub * quantity, "position PV"),
            spread_bp=self.spread_bp,
            buckets=tuple(buckets),
            dv01_rub=finite_money(math.fsum(b.dv01_rub for b in buckets), "bucket sum DV01"),
            parallel_dv01_rub=parallel,
            cs01_rub=spread if self.request.spread_kind == "credit" else 0.0,
            ofz_spr01_rub=spread if self.request.spread_kind == "ofz" else 0.0,
        )

    def reprice(self, scenario: BondScenario) -> BondScenarioResult:
        """Exact same-date P&L, isolated rate/spread effects and their cross residual."""
        shifts, spread = scenario.curve_shifts_bp, scenario.spread_shift_bp
        quantity = self.request.quantity
        base = finite_money(self.base_price_rub * quantity, "base PV")
        shocked = finite_money(self._price(shifts, spread) * quantity, "shocked PV")
        rates = finite_money(
            (self._price(shifts, 0.0) - self.base_price_rub) * quantity, "rates PnL"
        )
        credit = finite_money(
            (self._price(_ZERO_SHOCK, spread) - self.base_price_rub) * quantity, "spread PnL"
        )
        total = finite_money(shocked - base, "total PnL")
        interaction = finite_money(total - rates - credit, "interaction PnL")
        return BondScenarioResult(
            base_id=self.base_id,
            scenario_id=canonical_sha256(scenario),
            curve_basis=self.curve_basis,
            spread_convention=self.request.spread_convention.identifier,
            base_pv_rub=base,
            shocked_pv_rub=shocked,
            rates_pnl_rub=rates,
            spread_pnl_rub=credit,
            interaction_pnl_rub=interaction,
            total_pnl_rub=total,
        )

    def second_order(self, *, bump_bp: float = 1.0) -> BondSecondOrderRisk:
        """Compute every rate/rate, rate/spread and spread/spread second derivative.

        Base calibration stays frozen. No sparsity is assumed. These central
        deltas deliberately differ from the default one-sided risk() result.
        """
        bump = positive_money(bump_bp, "second-order bump bp")
        square = positive_money(bump * bump, "squared second-order bump")
        positive_money(4 * square, "mixed-difference denominator")
        size = len(RISK_BUCKETS) + 1
        quantity = self.request.quantity

        def price(moves: list[float]) -> float:
            return self._price(tuple(moves[:-1]), moves[-1])

        gradient = []
        hessian = [[0.0] * size for _ in range(size)]
        for i in range(size):
            moves = [0.0] * size
            moves[i] = bump
            up = price(moves)
            moves[i] = -bump
            down = price(moves)
            gradient.append(finite_money((up - down) / (2 * bump) * quantity, "gradient"))
            hessian[i][i] = finite_money(
                ((up - self.base_price_rub) + (down - self.base_price_rub)) / square * quantity,
                "diagonal gamma",
            )
            for j in range(i):
                values = []
                for sign_i, sign_j in ((1, 1), (1, -1), (-1, 1), (-1, -1)):
                    pair = [0.0] * size
                    pair[i], pair[j] = sign_i * bump, sign_j * bump
                    values.append(price(pair))
                mixed = finite_money(
                    ((values[0] - values[1]) - (values[2] - values[3])) / (4 * square) * quantity,
                    "cross gamma",
                )
                hessian[i][j] = hessian[j][i] = mixed
        return BondSecondOrderRisk(
            base_id=self.base_id,
            curve_basis=self.curve_basis,
            spread_convention=self.request.spread_convention.identifier,
            factors=(*RISK_BUCKETS, self.request.spread_kind + "_spread"),
            bump_bp=bump,
            gradient_rub_per_bp=tuple(gradient),
            hessian_rub_per_bp2=tuple(tuple(row) for row in hessian),
        )

    def jump_to_default(
        self, *, outstanding_nominal_rub_per_bond: float, recovery_rate: float
    ) -> BondDefaultResult:
        """Signed quantity * (recovery * current principal - dirty unit PV).

        Recovery is an explicit fraction in [0, 1], paid immediately at settlement.
        No recovery of coupons/accrued, default delay or subordination inference.
        """
        nominal = positive_money(outstanding_nominal_rub_per_bond, "outstanding nominal")
        recovery = non_negative_money(recovery_rate, "recovery fraction")
        if recovery > 1:
            raise ValueError("recovery fraction must be between 0 and 1")
        quantity = self.request.quantity
        base = finite_money(self.base_price_rub * quantity, "base PV")
        recovered = finite_money(nominal * recovery * quantity, "recovery PV")
        return BondDefaultResult(
            base_id=self.base_id,
            default_id=canonical_sha256(
                {
                    "base_id": self.base_id,
                    "outstanding_nominal_rub_per_bond": nominal,
                    "recovery_rate": recovery,
                    "model": "bond-immediate-principal-recovery-v1",
                }
            ),
            outstanding_nominal_rub_per_bond=nominal,
            recovery_rate=recovery,
            base_pv_rub=base,
            recovery_pv_rub=recovered,
            jtd_pnl_rub=finite_money(recovered - base, "JTD PnL"),
        )

    def approximate(
        self, scenario: BondScenario, *, bump_bp: float = 1.0
    ) -> BondScenarioApproximation:
        """Central delta + 1/2 x'Hx, with exact repricing and a signed residual."""
        risk = self.second_order(bump_bp=bump_bp)
        moves = (*scenario.curve_shifts_bp, scenario.spread_shift_bp)
        hessian = risk.hessian_rub_per_bp2
        try:
            linear = finite_money(
                math.fsum(d * x for d, x in zip(risk.gradient_rub_per_bp, moves)), "linear PnL"
            )
            rates = finite_money(
                0.5
                * math.fsum(
                    moves[i] * hessian[i][j] * moves[j] for i in range(11) for j in range(11)
                ),
                "rates gamma PnL",
            )
            spread = finite_money(0.5 * moves[11] ** 2 * hessian[11][11], "spread gamma PnL")
            cross = finite_money(
                math.fsum(moves[i] * hessian[i][11] * moves[11] for i in range(11)),
                "rate/spread cross PnL",
            )
            approximate = finite_money(math.fsum((linear, rates, spread, cross)), "approximate PnL")
        except OverflowError as exc:
            raise ValueError("approximation is outside the supported numerical domain") from exc
        exact = self.reprice(scenario)
        return BondScenarioApproximation(
            base_id=self.base_id,
            scenario_id=exact.scenario_id,
            curve_basis=self.curve_basis,
            second_order_id=canonical_sha256(risk),
            linear_pnl_rub=linear,
            rates_gamma_pnl_rub=rates,
            spread_gamma_pnl_rub=spread,
            rates_spread_cross_pnl_rub=cross,
            approximate_pnl_rub=approximate,
            exact_pnl_rub=exact.total_pnl_rub,
            residual_pnl_rub=finite_money(
                exact.total_pnl_rub - approximate, "approximation residual"
            ),
        )
