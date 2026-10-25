# Fixed RUB risk extensions (0.5.0)

This release extends the fixed-RUB-cashflow subset with full second-order risk,
immediate-default P&L, and a pure adapter for a local backend. It does not add
floating/FX-linked bonds, swaps, CDS, funding, market access or a portfolio worker.

## Decisions confirmed by the owner on 15 October 2026

- Keep both `continuous_zero` and `ois_quote`; callers must select explicitly.
  The local adapter rejects a curve or scenario with a different factor basis.
- JTD is signed quantity times `(recovery * outstanding principal - dirty PV)`.
  Recovery is mandatory for every call, expressed as a fraction in `[0, 1]`.
  There is no default recovery, including for subordinated issues or OFZ.

## Second-order risk

`engine.second_order(bump_bp=1)` returns 12 central first derivatives and the
complete symmetric 12-by-12 Hessian. The factor order is `RISK_BUCKETS`, then
`credit_spread` or `ofz_spread`. Values are signed RUB/bp and RUB/bp squared,
including position quantity. The spread is calibrated only at the base.

For position value V and a bump h in basis points:

- Gradient: `(V(+h_i) - V(-h_i)) / (2h)`.
- Diagonal: `(V(+h_i) - 2V(0) + V(-h_i)) / h²`.
- Mixed: `(V(+h_i,+h_j) - V(+h_i,-h_j) - V(-h_i,+h_j) + V(-h_i,-h_j)) / (4h²)`.

There are 288 bumped price evaluations. No Hessian entry is removed by a
sparsity assumption. Settlement rebasing alone can create a cross derivative
between distant zero-rate factors; OIS bootstrapping also couples factors.
All OIS quote shocks are applied to the original quotes and rebootstrap the curve.
A bump that moves no quote (the `20Y+` bucket on quotes up to 10Y) reuses the curve
of the quotes it leaves, bit for bit; with 17 MOEX RUONIA swaps the 11-bucket Hessian
bootstraps 201 distinct curves instead of 243.

The central gradient is intentionally separate from the forward +1bp P&L
returned by `risk()` by default. Do not add gamma to a one-sided sensitivity
as if it were an exact first derivative. Existing `risk()` behavior is unchanged.

`engine.approximate(scenario, bump_bp=1)` evaluates the central Taylor expression
`gradient · x + 0.5 * x' H x`, split into linear P&L, rate/rate gamma,
spread/spread gamma and rate/spread cross P&L. Symmetric cross terms are counted
once after the factor of one half. It also performs full repricing and reports
`residual_pnl_rub = exact_pnl_rub - approximate_pnl_rub`.

This comparison method is intended for calibration/validation, not as a cheap
browser runtime: it recomputes derivatives and the exact scenario. A consumer
can retain the returned gradient/Hessian for its own local scenario matrix
multiplication, preserving base identity and factor basis. No universal error
band, maximum shock size, or acceptance threshold is claimed. Verify bump
convergence and material stresses on each supported product. Very small bumps
can lose precision through subtraction. Nonfinite inputs/results and numerical
domain failures raise errors; do not replace them with zero risk.

## Default P&L

`engine.jump_to_default(outstanding_nominal_rub_per_bond=600, recovery_rate=0.4)`
uses the supplied **current unpaid principal**, not original face value or total
remaining cashflows. Recovery is paid immediately at settlement. Accrued
interest and future coupons are not recovered separately. There is no modeled
default delay, recovery discount curve, or legal/subordination inference.

For a long holding with zero recovery, JTD equals minus dirty position PV;
for a short it is positive. A recovery value above the dirty price can produce
positive P&L even for a long. The result is signed P&L, not a positive loss metric.
The default identity includes base identity, principal, recovery and model version.
This bond convention does not select the recovery used by a CDS model.

## Local boundary and source responsibilities

`prepare_local_bond_risk(FixedRubBondInput(...))` accepts already validated
fixed RUB flows, explicit clean price in percent of current principal, accrued
interest in RUB per bond, quantity, settlement, redemption horizon, curve and
spread conventions. It checks the declared currency/coupon model, horizon,
positive terminal payment, money inputs and curve basis. Clean price is converted
to dirty RUB before base spread calibration. Payments on/before settlement are
excluded by the core. Input collections are copied by the prepared engine.

`RiskRunContext` requires run, instrument, market, schedule and curve snapshot
IDs and the selected basis. The prepared `calculation_id` binds these IDs,
library release, core input content, clean/accrued decomposition and horizon.
`prepared.reprice(scenario, curve_basis=...)` requires the scenario basis and
returns the context and a scenario ID bound to this calculation.

The adapter is a reusable in-process boundary, not a connection to the actual
monitor application or market database. Upstream must validate contractual
principal redemption, coupon completeness, calendar adjustments, freshness,
source entitlement, currency and fixed-coupon eligibility. A positive terminal
payment alone does not prove a complete contractual schedule. Projected floaters
and FX-linked flows must not be relabeled fixed. Source IDs are provenance labels,
not proof of source quality or historical as-known selection. No private file is
read or sent anywhere by this module. Caller errors propagate; a future portfolio
worker must record failed/partial coverage explicitly.

## Synthetic local example

```python
from datetime import date
from rub_fixed_income import (
    CONTINUOUS, BondCashflow, BondScenario, DiscountCurve, FixedRubBondInput,
    RiskRunContext, ZSpreadConvention, prepare_local_bond_risk,
)

prepared = prepare_local_bond_risk(FixedRubBondInput(
    context=RiskRunContext(
        "synthetic-run", "synthetic-bond", "market-v1", "terms-v1", "curve-v1",
        "continuous_zero",
    ),
    cashflows=(BondCashflow(date(2027, 1, 1), 1000),),
    settlement_date=date(2025, 1, 1),
    horizon_date=date(2027, 1, 1), horizon="maturity",
    cashflow_currency="RUB", coupon_model="fixed",
    current_nominal_rub=1000, clean_price_pct=78, accrued_interest_rub=10,
    quantity=-2, curve=DiscountCurve.flat(date(2025, 1, 1), 0.10),
    spread_convention=ZSpreadConvention(compounding=CONTINUOUS), spread_kind="credit",
))
exact = prepared.reprice(BondScenario.parallel(100), curve_basis="continuous_zero")
gamma = prepared.engine.second_order()
comparison = prepared.engine.approximate(BondScenario.parallel(100, spread_shift_bp=50))
default = prepared.engine.jump_to_default(
    outstanding_nominal_rub_per_bond=prepared.current_nominal_rub, recovery_rate=0.4,
)
```

For quote-factor calculations, supply `CurveBootstrapRequest` with the original
OIS quotes and choose `ois_quote` in the context and scenario. Preserve
`prepared.calculation_id` and context alongside lower-level risk/default results.

## Numerical evidence

Tests compare every Hessian entry against exponential zero-coupon derivatives;
the OIS test uses analytical two-node bootstrap derivatives. Additional cases
cover settlement-induced distant cross terms, bump convergence, calibration
freeze, negative/zero quantities, OFZ labeling, both signs of large shocks,
Taylor components, JTD endpoints and input/provenance guards.

These are independent mathematical references, not an independent desk pricer
or human review. Real public-feed ingestion, Windows, a Python 3.11 runtime and
a fresh dependency installation require separate downstream verification.
