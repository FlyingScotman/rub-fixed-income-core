# Credit default swaps (package 8)

`rub_fixed_income.cds` prices single-name CDS with the ISDA CDS Standard Model:

- it bootstraps a piecewise-constant hazard curve from par-spread or upfront quotes with
  a stated recovery;
- it converts quotes (par spread, upfront for a standard coupon, flat hazard);
- it values an outstanding contract for either side;
- it gives IR DV01 on the discount curve's desk buckets, CS01, recovery sensitivity,
  JTD, exact scenarios and a 12-factor Hessian.

No licensed CDS data source exists today, so this package delivers the model only.
It is verified against values the ISDA Standard Model publishes (below) and against
analytical cases. Quotes, recovery and the discount curve come from the backend.
Nothing is looked up or inferred from the reference entity's name.

## Conventions are inputs

`CdsConvention` has no defaults. `CdsConvention.isda_standard(calendar)` collects the
standard contract of the 2009 ISDA "Big Bang" protocol (SNAC/STEC) and the Standard
Model's choices:

| Field | Standard value | Meaning |
|---|---|---|
| `payment_frequency_months` | 3 | Premium periods roll back from the unadjusted maturity on the 20 Mar/Jun/Sep/Dec cycle |
| `business_day_convention` | `FOLLOWING` | Adjusts every premium date except the maturity. The last payment is the adjusted maturity |
| `accrual_day_count` | `ACT_360` | Premium accrual. Only ACT/360 and ACT/365F are accepted |
| `step_in_days` | 1 | Step-in = trade date + 1 calendar day. Protection starts there, and accrued premium is counted to it |
| `cash_settlement_days` | 3 | Upfront paid T+3 business days on `calendar` |
| `accrual_on_default` | `ISDA_HALF_DAY_BIAS` | Accrued premium paid on default, integrated as in the Standard Model's C code |
| `hazard_day_count` | `ACT_365_FIXED` | Time basis of hazard rates |

The calendar is always the caller's. Nothing here encodes holidays.

Other standard-contract facts are inputs too:

- the running coupon (100 or 500 bp in North America; 25, 100, 500 or 1000 bp in Europe);
- recovery (40 % senior, 20–25 % subordinated or emerging-market in market practice).

Neither has a default. Recovery is mandatory everywhere (DEC07).

### Dates

- `previous_imm_date(d)`: the latest unadjusted 20 March, June, September or December on
  or before `d`.
- `standard_cds_maturity(trade, tenor_months, roll=...)`: `roll` is required.
  - `"quarterly"`: the first IMM date after the trade date, plus the tenor.
  - `"semiannual"`: the ISDA rule from 20 December 2015. On-the-run maturities roll only
    on 20 March and 20 September, so a trade from 20 March to 19 September matures on
    20 June + tenor, and later trades on 20 December + tenor.
- `cds_trade_dates(trade, convention, maturity_date=...)`: step-in, cash settlement and
  the standard accrual start. The accrual start is the latest *adjusted* IMM date on or
  before the step-in date and before the maturity. This gives a full first coupon: the
  buyer pays the whole coupon and the seller rebates the accrued part at cash settlement.
- `cds_premium_schedule(accrual_start, maturity, convention)`: `CdsPremiumPeriod`s.
  - Roll dates run back from the maturity and are adjusted.
  - The first period may be a short front stub.
  - The **last accrual period includes the maturity date**: it ends the day after.

## The model

The valuation date `T` is the curve date of both curves. Every probability and discount
factor is read at the end of a day.

- **Hazard curve.** `HazardCurve(curve_date, nodes, day_count)` has nodes
  `(end_date, h)`. The rate `h` applies on `(previous end, end]`, and the last rate
  extends flat. Survival is `Q(d) = exp(-∫h)`, with time in `day_count` from
  `curve_date`.
- **Integration grid.** Between two contract dates, the grid is the union of the
  discount curve's node dates and the hazard curve's node dates. On each grid interval
  the log discount factor and the log survival are linear in days. So the forward rate
  and the hazard are constant there (the Standard Model's "piecewise" assumption), and
  each piece integrates in closed form. For a node curve (`DiscountCurve`, or a
  rebootstrapped OIS curve) this is exact. A zero-shocked view (`continuous_zero` basis)
  is read at the same grid.
- **Protection leg**, per unit loss. It runs from the start of the step-in day to the
  end of the maturity day, from `d0 = step-in - 1` to `maturity`:
  `Σ ĥ/(ĥ+f̂) · P0 Q0 · (1 - e^-(ĥ+f̂))`, with `ĥ = ln Q0/Q1` and `f̂ = ln P0/P1` on each
  piece. Near zero, a series replaces the ratio.
- **Premium leg**, per unit coupon. It sums `α_i · P(pay_i) · Q(accrual_end_i - 1)` over
  the periods not ended at step-in, plus the accrual on default. In a period starting at
  `s`, the accrued premium at the end of day `u` is `(u - (s - 1) + b)/360`. Here
  `b = 0.5` for `ISDA_HALF_DAY_BIAS` and `0` for `EXACT`, and `NONE` drops the term. This
  is integrated against the default density on the same pieces:
  `ĥ P0 Q0 [(u0 - a) E1(k) + n E2(k)] / 360`, where `k = ĥ + f̂`,
  `E1 = (1 - e^-k)/k` and `E2 = (1 - e^-k (1 + k))/k²`.
- **Accrued premium** `α_acc`: the accrual day count from the start of the period
  holding the step-in date to the step-in date.

Write `A` for the risky annuity above, `Prot` for the protection leg, `N` for the
notional, `c` for the coupon, `R` for the recovery and `DF_cs` for the discount factor
to cash settlement. Then:

- buyer's dirty value today: `V = N[(1 - R) Prot - c A]`;
- clean value: `V + N c α_acc DF_cs` (the accrual rebate);
- **clean upfront** (buyer pays at cash settlement, per unit notional; points / 100):
  `U = ((1 - R) Prot - c A)/DF_cs + c α_acc`;
- **par spread**: the coupon with `U = 0`, so `s = (1 - R) Prot / (A - α_acc DF_cs)`.

A seller's values are the buyer's negated. The par spread, `risky_annuity` (the clean
annuity per unit coupon) and `points_upfront` do not depend on the side.

### Quotes and bootstrap

- `CdsSpreadQuote(maturity, par_spread)` is a standard contract with coupon equal to the
  spread and `U = 0`.
- `CdsUpfrontQuote(maturity, coupon, upfront)` is a standard coupon with a clean upfront.
- `CdsCurveRequest(valuation_date, quotes, recovery_rate, convention)` holds the quotes.
  Each one is a standard contract traded on the valuation date: standard accrual start,
  the quote's maturity.
- `bootstrap_hazard_curve(request, discount_curve)` solves one hazard per quote, in
  maturity order. Nodes sit at the quote maturities. It uses the bracketed Illinois
  method (`_solver.illinois`, the same contract as `bisect`). Every quote is repriced
  within 1e-12 of notional. A quote below its zero-hazard value implies a negative
  hazard and raises `InvalidTerms`.
- **Conversions** use the market's flat-curve convention, with recovery always given:
  - `flat_hazard_from_spread` and `flat_hazard_from_upfront`;
  - `upfront_from_spread(spread, coupon, ...)`, the ISDA quoted-spread to upfront;
  - `spread_from_upfront(upfront, coupon, ...)`;
  - `cds_par_spread(hazard_curve, ...)`.

### Outstanding contracts

`CdsContract(contract_id, reference_entity, currency, side, notional, coupon,
accrual_start_date, maturity_date, convention)` holds the terms. For a standard trade,
`accrual_start_date` is `cds_trade_dates(trade_date, ...).accrual_start_date`. Older
periods are simply ended at valuation.

`value_cds(contract, valuation_date=..., discount_curve=..., hazard_curve=...,
recovery_rate=...)` returns a `CdsValuation` with these fields:

- `protection_leg_pv`, `premium_leg_pv`;
- `accrued_premium`, negative for a buyer, who owes it;
- `pv`, the dirty value today;
- `clean_pv`;
- `cash_settlement_amount`, which is `pv / DF_cs` (the unwind cash);
- `par_spread`, `risky_annuity`, `points_upfront`.

Money is in the contract currency.

## Risk

`CdsRiskEngine(CdsRiskRequest(contract, valuation_date, discount_curve, credit_curve),
curve_cache=ShockedCurveCache(), credit_cache=CdsCurveCache())`.

- **The credit input is always the quotes.** Every shock holds them and rebootstraps the
  hazard curve. This is the market practice for quoted CDS: IR DV01 is at constant
  spreads, and recovery risk is at constant spreads.
- **The discount input selects the rate basis**, as for bonds: a `DiscountCurve` uses
  the zero overlay, and a `CurveBootstrapRequest` bumps the OIS quotes and
  rebootstraps.
- **Caches.** Share one `ShockedCurveCache` and one `CdsCurveCache` across a job's
  positions. Each shocked credit curve is then built once per reference entity, and
  results do not change.

`engine.risk(bump_bp=1, recovery_bump=0.01, difference="central")` returns a
`CdsRiskResult`. Central is the default (DEC08); `forward` gives the exact +1 bp P&L.

| Field | Shock |
|---|---|
| `buckets` (`CdsBucketRisk(bucket, dv01)`), `dv01` | One of the 11 desk buckets of the discount curve, 1 bp; `dv01` is their sum |
| `parallel_dv01` | All buckets together, repriced |
| `cs01` | Every quoted spread +1 bp (desk factor `CS`). An upfront quote moves in its flat quoted spread: convert, shift, convert back |
| `quote_buckets` (`CdsQuoteRisk(maturity, cs01)`) | One quote at a time. They sum to about `cs01` |
| `recovery01` | Recovery ±`recovery_bump`, central, scaled to 0.01; quotes held |

- **Signs.** A protection buyer has positive CS01, and a seller (long credit, like a
  bondholder) negative.
- `engine.reprice(CdsScenario(curve_shifts_bp, spread_shift_bp, recovery_shift))` gives
  exact P&L. Rates, spread and recovery are each isolated, with the interaction as the
  remainder.
- `engine.second_order(bump_bp=1)` returns the central gradient and full Hessian over
  the 11 buckets and `CS`, from `_central_derivatives` like the other engines.
- `cds_factor_risk(risk, discount_curve_id, second_order=None)` maps the results to
  `IR|<curve_id>|<bucket>` and `CS`. Recovery is not a desk factor; it stays in
  `recovery01`.

### Jump to default (DEC07)

`engine.jump_to_default(recovery_rate=R)` models default at the step-in date with an
explicit realised recovery:

```text
JTD = side · N · ((1 - R) - c · α_acc) - pv
```

The buyer receives the loss given default and pays the accrued premium, and the contract
value is lost. The bond rule is `quantity · (R · principal - dirty PV)`: value after
default minus value before. This is the same rule, so a long bond and a protection
seller both lose on default, and a protection buyer gains. A bond hedged with bought
protection on the same notional nets to about zero JTD.

### Identity

`base_id` hashes:

- the model version;
- the contract and the identifiers of both conventions;
- the valuation date;
- the curve basis and the discount curve's identity;
- the credit quotes and the recovery.

It hashes inputs only, not the bootstrapped hazards. `scenario_id` hashes the scenario,
and `default_id` hashes the base and the recovery.

## Validation

**External goldens** are ISDA Standard Model values published in QuantLib's test suite
(`test-suite/creditdefaultswap.cpp`, github.com/lballabio/QuantLib, fetched 17 October
2026). The tests rebuild that suite's ISDA-style curve independently: deposits ACT/360
from T+2, single-curve par swaps 30/360, log-linear discount factors.

- **`testIsdaEngine`.** These are 20 Markit values:
  - trade date 21 May 2009, USD rates "from Markit published rates";
  - maturities June 2010, 2011, 2012, 2016 and 2019;
  - quoted spreads 10 and 1000 bp, recovery 20 % and 40 %, coupon 100 bp, notional 10 MM.

  The values are clean upfronts at cash settlement. `upfront_from_spread` reproduces all
  20 to their published ten digits (relative error ≤ 3e-10). `spread_from_upfront`
  inverts them to 1e-9.
- **`testIsdaCalculatorReconcileSingleQuote`.** Trade date 26 July 2021, with a negative
  EUR curve, maturity 20 June 2026, spread 67.13 bp, recovery 40 %, notional 1 MM. The
  Markit value is −16070.7 (the clean value on the trade date) and the accrual is 1000.
  Core gives −16070.725 and 1000.
- **`testAccrualRebateAmounts`.** These are ten accrued-premium amounts "from various
  test results on the ISDA CDS model website", for trade dates around IMM dates and
  maturity. Core reproduces them all to the cent. They pin down the standard accrual
  start and the inclusive last period.

The match also confirms the timing choices listed above:

- the survival observed at accrual end − 1;
- protection from the start of the step-in day;
- the half-day accrual-on-default bias;
- the full first coupon with rebate.

**Analytical tests:**

- the exact credit triangle at zero rates: `s = h (1 - R) 360/365` with exact accrual on
  default;
- `h ≈ s/(1 - R)` under standard conventions;
- the zero-hazard limit;
- a contract at its par spread has zero clean value;
- the bootstrap reprices mixed spread and upfront quotes;
- the round trips upfront ↔ spread ↔ hazard;
- buyer and seller are exact mirrors;
- bump-size convergence (central error ÷ 4 per halving);
- buckets sum to the parallel shift, and quote buckets sum to the parallel CS01;
- forward risk equals the exact +1 bp scenario;
- the gradient equals the central risk, and the Hessian is symmetric;
- a Taylor expansion against an exact scenario;
- the JTD formula and its signs;
- results are identical with shared caches.

## Benchmark

`benchmarks/cds_bench.py --positions 2000 --entities 100` builds a synthetic book of
2,000 positions on 100 reference entities, each with five spread quotes. Measured on
Linux, Python 3.13, ms per position:

| Run | Zero curve | OIS quotes |
|---|---|---|
| first order (valuation, 11 buckets, parallel, CS01, 5 quote CS01s, recovery01), cold | 3.9 | 4.6 |
| same, warm caches | 2.4 | 2.9 |
| first order + 12×12 Hessian, cold | 34.0 | 40.5 |

The cold cost is mostly hazard rebootstraps, because every rate shock rebootstraps at
fixed spreads:

- about 40 per entity for first order (11 buckets and the parallel shift both ways,
  CS01, five quote CS01s, recovery);
- 288 for the Hessian.

They are shared by the entity's positions, so a book with more positions per entity
amortises better.

## Not covered (typed rejections or out of scope)

- **Index CDS and tranches**, CDS options, quanto/currency-mismatched CDS (the discount
  curve is the caller's choice of the contract currency's curve), restructuring-clause
  differences, and sovereign or emerging-market specifics.
- **Standard maturities.** A contract's maturity and accrual start are always inputs.
  `standard_cds_maturity` and `cds_trade_dates` are helpers the caller applies; nothing
  rolls a tenor implicitly.
- **Day counts.** ACT/ACT accrual or hazard time raises `UnsupportedProduct`.
- **Recovery curves.** Recovery is one number per curve. There is no term structure and
  no random recovery.
- **Default timing.** No default before the step-in date or backstop look-back, and no
  settlement-date lag of the default payment. JTD is immediate, at step-in.
- **Portfolio worker.** `value_portfolio` does not take CDS yet; see the release notes.
