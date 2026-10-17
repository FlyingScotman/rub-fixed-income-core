# Fixed-cashflow RUB risk and scenarios

Available in 0.4.0. `BondRiskEngine` prices a supplied fixed RUB cashflow schedule,
calibrates a base Z-spread once if requested, and computes 11-bucket key-rate
DV01/KRD, CS01 or OFZ spread risk, and exact same-settlement scenarios.

## Scope and inputs

`BondRiskRequest` requires the cashflows, settlement date, curve input, explicit
`ZSpreadConvention`, and `spread_kind` (`credit` or `ofz`). Supply exactly one of:

- `spread_bp`: the base spread in **basis points**, unlike the decimal spread
  accepted by the existing `price_cashflows` helper;
- `dirty_price_rub`: market dirty price **per bond**, in RUB. The existing
  `solve_z_spread` solver calibrates the spread on the base curve. Its supported
  bracket/domain limits still apply; an unbracketed calibration fails.

Cashflow amounts are nonnegative **RUB per bond**. Quantity is a finite signed
number of bonds (default 1); a short reverses all monetary results. It does not
change KRD. Zero quantity returns zero monetary risk but still validates and
prices the instrument. At least one positive future payment is required.
All flows, including excluded historical payments, are validated. Booleans,
NaN, infinities, nonpositive dirty prices, ambiguous price/spread inputs, and
non-RUB currency labels are rejected. Monetary results are checked for finiteness.

The caller must supply the contractual fixed schedule, already adjusted for
payment calendars, amortization and a confirmed put redemption horizon. This
module does not construct put terms or exercise optionality. Floating coupons
enter only through `floating_leg` (see [floating coupons](floating.md)); it does
not model FX-linked payments, calls, swaps, funding, carry or historical
market selection. Converting foreign payments to RUB at today's FX does not
turn them into a supported fixed RUB instrument.

Dates and amounts remain fixed under shocks. Payments on or before settlement
are excluded, and discounting uses `DF(payment) / DF(settlement)`. The settlement
DF is shocked too. Clean/dirty conversion and accrued-interest calculation
remain in the existing helpers; accrued interest is fixed in these same-date
scenarios, so clean and dirty monetary price changes coincide.

## Curve risk basis is explicit

The input type selects the basis, which is recorded in every result:

| Curve input | `curve_basis` | Shock operation |
| --- | --- | --- |
| `DiscountCurve` | `continuous_zero` | Overlay a piecewise-linear shift on continuous zero rates: `DF'(t) = DF(t) exp(-s(t) t / 10000)` |
| `CurveBootstrapRequest` | `ois_quote` | Interpolate the shift to each original OIS quote's maturity, add it to the decimal par rate, then bootstrap again using the same conventions |

The zero overlay evaluates the original curve at each required date, preserving
its interpolation and extrapolation; it does not replace the original curve by
11 display nodes. Curve time in both modes uses `CurveConvention.time_day_count`
from the **curve date**. Spread time and compounding use the separately supplied
`ZSpreadConvention` from **settlement**. Annual, monthly and continuous spreads
therefore remain distinct. No library-wide spread default is changed.

The fixed grid is:

| Bucket | 1M | 3M | 6M | 1Y | 2Y | 3Y | 5Y | 7Y | 10Y | 15Y | 20Y+ |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Years | 1/12 | 0.25 | 0.5 | 1 | 2 | 3 | 5 | 7 | 10 | 15 | 20 |

Each interior bucket has a triangular weight between adjacent pillars. The
first and last weights extend flat to time zero and beyond 20 years. Weights
sum to one everywhere; a parallel 11-vector therefore covers the tails too.
Grid years follow the curve's day count, not rounded calendar-month dates.

In quote mode a bucket with no quote exposure can have zero risk. Bootstrap
coupling can spread quote risk across maturities. There is no claim of a
universal sparse Hessian. Quote risk and zero-rate risk are not interchangeable:
downstream scenario/market-move vectors must use the same basis as the risks.
The caller identifies whether its actual curve represents RUONIA; this generic
math module does not certify a market curve's source or quality.

## Risk definitions

`engine.risk(bump_bp=1, difference="forward")` is the contract-compatible default.
For unit price `P`, signed quantity `q`, and a bump of `h` basis points:

- Forward sensitivity: `q * (P(+h) - P(0)) / h` RUB/bp.
- Central sensitivity: `q * (P(+h) - P(-h)) / (2h)` RUB/bp, selected explicitly
  by `difference="central"`. This estimates a derivative, not exact +1bp P&L.
- `BucketRisk.krd_years = -unit_sensitivity / P(0) * 10000`. It is normalized
  by dirty unit PV and does not change with quantity or position direction.
- `dv01_rub` is the sum of all 11 `buckets[].dv01_rub`.
- `parallel_dv01_rub` is independently repriced with all pillars bumped together.
  Finite-bump curvature makes it differ slightly from the bucket sum. No
  rescaling conceals that difference.
- `cs01_rub` bumps the base spread if `spread_kind="credit"`; `ofz_spr01_rub`
  does so if `spread_kind="ofz"`. The other field is zero, avoiding double count.

For ordinary positive cashflows, parallel DV01 and spread sensitivity are
negative for a long and positive for a short. Individual buckets can have a
different sign because of settlement rebasing or bootstrap coupling. All
prices and risks are unrounded floats; reporting/cash rounding belongs after
calculation. Choose a numerically meaningful bump and check convergence for
the intended instruments; arbitrarily tiny finite differences lose precision.

## Scenarios and identity

`BondScenario` contains exactly 11 curve shifts and one resolved own-spread
shift, all in absolute basis points. `BondScenario.parallel()` constructs the
parallel vector. The local desk layer resolves rating/sector/issuer rules and
selects the credit versus OFZ shock before calling this API.

`engine.reprice(scenario)` retains the calibrated base spread and adds only
the requested spread shock. It returns exact repricing under this model:

- `total_pnl_rub = shocked_pv_rub - base_pv_rub`;
- `rates_pnl_rub`: exact isolated curve-shock P&L at unchanged base spread;
- `spread_pnl_rub`: exact isolated spread-shock P&L at unchanged base curve;
- `interaction_pnl_rub`: total minus those two isolated effects.

The isolated effects already include their own nonlinearities. Do not map the
interaction field alone to the monitor's sensitivity-based `convexity` field.
Publishing only the exact total is supported by the monitor contract. No
universal error band for a first/second-order approximation is asserted.

`base_id` hashes model version, input curve content/conventions/calendar
fingerprint, cashflows, settlement, spread convention/type, input dirty price
or spread, calibrated spread, quantity and currency. `scenario_id` hashes the
shock vector and spread shift. Results are frozen dataclasses. Cashflow/quote/
node collections and calendar overrides are copied into immutable snapshots.
The local backend must additionally bind these content identities to its run,
market snapshot, instrument identifier and installed library release. These
IDs are not a historical snapshot store or a scenario job manager.

Invalid shocks, out-of-domain discounting or failed bootstraps/calibration
raise errors rather than returning a partial result or zero risk. The caller
must record failed coverage explicitly.

## Example (synthetic)

```python
from datetime import date
from rub_fixed_income import (
    CONTINUOUS, BondCashflow, BondRiskEngine, BondRiskRequest,
    BondScenario, DiscountCurve, ZSpreadConvention,
)

engine = BondRiskEngine(BondRiskRequest(
    cashflows=(BondCashflow(date(2027, 1, 1), 1000.0),),
    settlement_date=date(2025, 1, 1),
    curve=DiscountCurve.flat(date(2025, 1, 1), 0.10),
    spread_convention=ZSpreadConvention(compounding=CONTINUOUS),
    spread_kind="credit",
    spread_bp=200.0,
))
risk = engine.risk()
stress = engine.reprice(BondScenario.parallel(300, spread_shift_bp=200))
```

Use `CurveBootstrapRequest(curve_date, original_quotes, convention)` in the same
request to obtain quote-bump/rebootstrap risk instead. Use `dirty_price_rub`
in place of `spread_bp` to calibrate on the base market.

## Validation

`tests/test_risk.py` includes independent exponential zero-coupon formulas,
annual/monthly spread formulas, two-node analytical OIS bootstrapping,
irregular/leap amortizing cashflows with a supplied put horizon, settlement-day
coupon exclusion and settlement-DF shocks, twists, short/zero quantities,
tail exposure, day-count separation, central/forward bump convergence,
material positive/negative stresses, frozen-spread enforcement, immutable
calendar/input snapshots, deterministic identity and invalid-input guards.

These synthetic checks establish the supported mathematics. They do not
validate live RUONIA quotes, real issue terms, Windows execution, foreign
products or an independent desk pricer.

## Extensions in 0.5.0

Full fixed-RUB gamma, exact-versus-Taylor comparison, explicit-recovery JTD and a pure local input adapter are documented in [second-order-local-risk.md](second-order-local-risk.md). The limitations above describe the original 0.4.0 interface.

## Non-RUB bonds in the portfolio (package 3)

`BondRiskRequest` still accepts RUB only. `value_portfolio` values a CNY, USD or EUR bond
by converting its flows, nominal and accrued at the snapshot FX rate *explicitly* and
discounting them on the bond's own currency curve (never the RUB curve). PV, bucket DV01,
CS01, the Hessian, scenarios and JTD are therefore RUB at today's rate. Because the RUB
value is `FX · PV_ccy`, the FX factor is added analytically:
`FX delta = PV_ccy · FX / 100`, cross terms `dV/dx_i / 100`, no FX gamma (see
`CONVENTIONS.md`). The tests check this against an independent USD-only engine converted
afterwards and against central differences of exact rates-and-FX scenarios.

## Callables, perpetuals and pass-throughs (package 9b)

Risk of a callable or perpetual is the risk of the **exercised bond** under an explicit
assumption (`docs/callables.md`): `to_worst` resolves the date at the base price and the
date is then held through every bump, scenario and the Hessian (no switching, no option
value; OAS is rejected). A pass-through's rate risk holds its CPR fixed; CPR risk is the
separate `cpr_sensitivity` (central difference per 1 pp of CPR, forward at CPR below the
bump) and is not a desk factor. Extension risk is reported by `extension_risk`
(duration to maturity minus duration to the call, and the value change if the call is
skipped at the yield to call).

## Floaters on separate projection and discount curves (package 1)

`BondRiskRequest(floating_leg=..., projection_curve=...)` projects coupons off
`projection_curve` and discounts on `curve`; the spread is a margin over `curve`. Each
curve is shocked in its own basis (zero-rate overlay or OIS quote rebootstrap) on the 11
buckets. `risk()` returns a `TwoCurveBondRiskResult` with `projection_buckets` beside the
discount `buckets`; `dv01_rub` sums both and `parallel_dv01_rub` moves both curves
together. `second_order()` is central over 23 factors (`discount <b>`, `projection <b>`,
spread) with the same bump as central `risk()`, so `bond_factor_risk(...,
projection_curve_id=...)` gives a consistent Taylor expansion. `reprice` and
`approximate` take `projection_curve_shifts_bp`; when None the projection curve moves by
the scenario's curve shifts. Without `projection_curve` the single-curve floater path is
bit-identical to 0.8.0. See [floating coupons](floating.md).

## Inflation-linked bonds (package 9a)

An OFZ-IN position is a fixed-flow risk in **real** terms (`docs/linkers.md`). Its flows
are taken at the settlement-date indexed face, held constant, and discounted on a
caller-built real curve (`rub_real_discount_factors_v1`). Shifts of that curve's
continuous zero rates give `REAL|<curve_id>|<bucket>`, and the own spread over it gives
`CS`. Both are central, with the Hessian from the same bump. The identity
`ΣREAL = −τ·PV·10⁻⁴` holds for a single-flow bond with a continuous spread
(`tests/test_inflation.py`). CPI is not a bumped factor. Breakeven inflation is not
modelled: the real curve absorbs it. Nominal `PortfolioScenario` shocks do not move
linkers; their rows are `UNSUPPORTED_PRODUCT`.

## Credit default swaps (package 8)

`CdsRiskEngine` (`docs/cds.md`) reports in the same vocabulary: `IR|<curve>|<bucket>` on
the discount curve (zero overlay or OIS quote rebootstrap, as selected by the curve
input) and `CS` = all quoted spreads of the reference entity +1 bp. Central differences
by default; the Hessian covers the 11 buckets and `CS` with the same bump (DEC08). Every
shock holds the CDS quotes and rebootstraps the hazard curve, so IR DV01 is at constant
spreads. Recovery sensitivity (`recovery01`, per 0.01, quotes held) is reported beside
the factors, not as one.

JTD follows the bond rule, value after default minus value before (DEC07):
`side · N · ((1 - R) - c · accrued) - pv`, with the recovery an explicit argument. A
protection seller (long credit) has negative JTD like a long bond; a buyer positive.

## Repo and reverse-repo legs (package 6)

`RepoRiskEngine` (`docs/repo.md`) reports `IR|<discount curve>|<bucket>` on the remaining
cash legs and, for a floating repo on a separate projection curve,
`IR|<projection curve>|<bucket>` on the projected overnight benchmark days; a
single-curve floating repo has one set of factors. Central differences by default; the
Hessian covers 11 or 22 factors with the same bump (DEC08). Known fixings and the fixed
rate never move. A cash lender (`reverse_repo`) has negative discount DV01 and positive
projection DV01; the borrower the reverse. The collateral carries no risk here: the bond
stays a position of the cash borrower and is risked by the bond engines, so the repo
leg and the bond are never counted twice. There is no `CS`, JTD or FX factor.

## Derivatives in the portfolio (result contract v3)

`value_portfolio` maps every product's engine risk to the desk factors with the product
module's own mapping (`swap_factor_risk`, `fx_forward_factor_risk`, `xccy_factor_risk`,
`cds_factor_risk`, `fx_futures_factor_risk`, `rate_futures_factor_risk`,
`ofz_futures_factor_risk`, `repo_factor_risk`), scaled by the position quantity (futures
engines include it). A non-RUB CDS is scaled by the snapshot `FxRate` and gains `FX|<ccy>`
with its cross terms (`fx_translated_factor_risk`), as non-RUB bonds. Bonds and
derivatives share one factor space: `factor_totals` sums every position's first order,
`hessian_totals` every position's sparse Hessian by factor name (upper triangle by name);
cross terms within one position (FX x rates of FX products, `CS` x rates of CDS,
discount x projection of two-curve swaps) are those of its engine. All first orders and
Hessians are central with the policy's bump (DEC08). Recovery sensitivity of a CDS is
reported as `recovery01_rub` beside the factors. See [portfolio-v3.md](portfolio-v3.md).
