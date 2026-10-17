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
