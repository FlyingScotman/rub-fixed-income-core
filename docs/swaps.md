# Seasoned interest-rate swaps: RUONIA OIS and key-rate swaps (package 2)

`rub_fixed_income.swaps` values outstanding RUB swaps: a fixed leg against a
floating leg on one constant notional. It covers RUONIA OIS (compounded overnight
RUONIA) and key-rate swaps (arithmetic average of the CBR key rate, MOEX IRS
KEYRATE). The framework matches `BondRiskEngine` and `FxFuturesRiskEngine`:

- explicit conventions and curves, with the risk basis chosen by the curve type;
- known fixings frozen under every shock;
- the 11-bucket grid and a shared `ShockedCurveCache`;
- exact scenarios, a full central Hessian and content identities.

The module does no I/O. The backend supplies the OTC terms, the calendar, the
curves and a point-in-time fixing snapshot.

## Terms and conventions

`InterestRateSwap(swap_id, side, notional, fixed_rate, effective_date, termination_date, convention, float_spread=0.0, currency="RUB")`

- `side`: `payer` pays fixed and receives float; `receiver` does the reverse.
- `notional` is RUB and constant. Amortizing notionals are not modelled.
- `fixed_rate` and `float_spread` are decimal annual. The spread is added
  simply over the float accrual factor, `N * spread * alpha`; it is not
  compounded.
- `effective_date` is the adjusted accrual start.
- `termination_date` is the **unadjusted** end. The schedule rolls from it, and
  its own adjustment gives the last accrual end.
- Any currency other than RUB raises `UnsupportedProduct`. Cross-currency swaps
  are package 7.
- `InterestRateSwap.from_tenor(..., trade_date, tenor, calendar, convention)`
  resolves the dates with `resolve_tenor`: the spot start is `spot_lag_days`
  business days after the trade date. `ON`, `TN` and `SN` are rejected.

`SwapConvention(index, averaging, fixed_leg, float_leg, rate_day_count, business_day_convention, spot_lag_days, payment_lag_days, spec_id=None)`
holds every convention. Nothing is defaulted.

- Each leg has a `SwapLegSchedule(frequency_months, roll, day_count, single_period_up_to_months=None)`.
  - `roll="backward"` builds unadjusted dates every `frequency_months` back from
    the termination date. `roll="forward"` builds them on from the effective
    date. Month ends clamp in both.
  - Every date, the termination included, is then adjusted with
    `business_day_convention`. Adjusted dates that do not strictly increase
    raise `InvalidTerms`.
  - `roll="legacy-annual-forward"` is `dates.annual_schedule` exactly: the
    legacy OIS bootstrap's fixed leg, with its iterated 12-month roll and its
    short end stub.
  - `single_period_up_to_months`: a swap whose unadjusted termination is no
    later than that many months after the effective date has a single period.
    This is the KEYRATE fixed leg up to 1Y.
  - `day_count` gives each period's accrual factor.
- Payments fall `payment_lag_days` business days after each accrual end.
- `rate_day_count` weights the daily RUONIA fixings (RUONIA is an ACT/365 rate).
  Key-rate averages are calendar-day averages and take `None`.
- Supported pairs: RUONIA with `compounded`, key rate with `arithmetic`. Any
  other pair raises `UnsupportedProduct`.

`swap_convention(spec_id)` derives the convention from a registered curve spec:

| Spec | Swap convention |
| --- | --- |
| `rub_key_rate_swap_v1` | TOM start (spot lag 1), modified following, ACT/ACT ISDA. Fixed leg annual, backward, a single payment up to 1Y. Float leg quarterly, backward, arithmetic key-rate average. No payment lag. This is the schedule of `price_key_rate_swap`, period by period. |
| `rub_ruonia_ois_legacy_v1` | Start on the curve date (spot lag 0), unadjusted, ACT/365F. Both legs use the legacy annual schedule, and RUONIA compounds on ACT/365F. No payment lag. These are the swaps whose par rates the legacy bootstrap reprices. |

Any other spec raises: `UnknownCurveSpec` for an unregistered ID,
`UnsupportedProduct` for a non-swap spec. The exchange RUONIA OIS spec
(`rub_ruonia_ois_moex_v2`) is not registered yet (BACKLOG #37). Until it is, pass
the conventions from the trade confirmation as an explicit `SwapConvention`.

`swap_schedule(swap, calendar)` returns both legs' `SwapPeriod`s
(`accrual_start`, `accrual_end`, `payment_date`, `accrual_factor`). A bounded
calendar rejects dates outside its range.

## Market and valuation date

`SwapRiskRequest(swap, calendar, discount_curve, projection_curve=None, fixings=None)`

- The **valuation date is the discount curve's date**. A projection curve and
  the fixing history must be dated the same day.
  - For key-rate swaps this is the TOM date of the KRS curve build, as in
    `price_key_rate_swap`.
  - Flows paid on or before the valuation date are settled and left out.
- `discount_curve` is a `DiscountCurve` (`continuous_zero` risk basis) or a
  `CurveBootstrapRequest` (`ois_quote` basis: shift the quotes, then
  rebootstrap), as in [risk.md](risk.md).
- `projection_curve` depends on the index:
  - **RUONIA**, `None`: the discount curve also projects. This is a single
    curve with one set of factors (`discount_and_projection`).
  - **RUONIA**, a `DiscountCurve` or `CurveBootstrapRequest`: a separate
    projection curve, with its own factors.
  - **Key rate**: a `KeyRateForwardCurve` is required (`key_rate_forward`
    basis).
- `fixings` is a [`FixingHistory`](floating.md) of the swap's index, used read-only.
  It is required as soon as a remaining float period has started. A RUONIA gap,
  or no fixing on or before a needed day, raises `MissingMarketData`. Fixings
  are never filled.

## Float coupons

**RUONIA (compounded).**

- Each accrual day takes the fixing of the latest business day on or before it,
  in the history's calendar. A Friday fixing covers the weekend. There is no
  lookback or lockout.
- The known part is `prod(1 + r_i * n_i * tau)` over the business-day segments
  observed before the valuation date (`n_i` days, `tau` from `rate_day_count`).
- From the first unobserved segment start `c` to the accrual end `e`, the
  projection curve gives the compounded overnight forwards `DF(c) / DF(e)`. This
  is exact for periods that start and end on business days.
- Coupon = `N * (known * DF(c) / DF(e) - 1 + spread * alpha)`.

**Key rate (arithmetic, as in `key_rate.py`).**

- The coupon is the calendar-day average of the key rate over the period:
  `N * (average + spread) * alpha`, with `alpha` from the float leg's day count
  (ACT/ACT ISDA for KEYRATE).
- Days before the valuation date take the key rate in force from the history.
  Later days come from `KeyRateForwardCurve.arithmetic_average`.

**Known flows.** A period that ended before the valuation date but has not been
paid (payment lag) is fully known.

## Results

`SwapRiskEngine(request, curve_cache=ShockedCurveCache())`. Share one cache
across engines on the same OIS quotes.

`engine.valuation() -> SwapValuation`. Money is signed RUB for the position.

| Field | Meaning |
| --- | --- |
| `pv_rub`, `fixed_leg_pv_rub`, `float_leg_pv_rub` | PV and the two legs |
| `accrued_fixed_rub`, `accrued_float_rub`, `accrued_rub` | Earned and not yet paid at the valuation date: the accrued part of the current coupon (float: from known fixings), plus any finished period awaiting payment |
| `clean_pv_rub` | `pv_rub - accrued_rub` |
| `par_rate` | The fixed rate that sets the PV of the remaining flows to zero |
| `annuity` | `sum(alpha_i * DF_i)` of the remaining fixed periods, per unit notional |
| `cashflows` | `SwapCashflow` rows: leg, dates, accrual factor, implied rate, signed amount, `source` (`fixed`, `known`, `partially_projected`, `projected`), known and projected days, DF, PV |

`engine.risk(bump_bp=1, difference="central") -> SwapRiskResult`

- **Central difference by default (DEC08).** Desk factors need it, and the
  Hessian uses the same bump.
- `curves` holds one `SwapCurveRisk` per role: `discount_and_projection`
  (single curve), or `discount` and `projection`. Each role carries 11
  `BucketRisk` rows, the bucket sum `dv01_rub` and an independently repriced
  `parallel_dv01_rub`.
- `krd_years = -dv01 / notional * 1e4` is signed and normalised by notional,
  because a swap's PV can be zero.
- `dv01_rub` sums every curve's buckets.
- **Signs:** a payer gains when rates rise, so its DV01 is positive. A receiver's
  is negative and has the same size.

**Projection shocks.**

- RUONIA projection curves move like discount curves: a zero-rate overlay or a
  quote shift with a rebootstrap.
- A key-rate forward curve moves every **projected** day `d` by
  `sum_k w_k(t_d) * s_k`. Here `w_k` are the bucket hats at the day's curve time
  (ACT/ACT ISDA, the spec's time basis).
- The key rate in force until `current_rate_end` (the next policy meeting) is
  treated as set and does not move.
- The key-rate projection is linear in its shifts. Its buckets therefore sum
  exactly to the parallel shift, and its own Hessian block is exactly zero.
- **The two curves are separate factors.** A discount shock does not recalibrate
  the key-rate forward curve, and a projection shock does not move discounting.

`engine.reprice(SwapScenario(discount_shifts_bp, projection_shifts_bp))` reprices
exactly and returns the discount-only, projection-only and interaction P&L. A
single-curve swap rejects projection shifts.

`engine.second_order(bump_bp=1) -> SwapSecondOrderRisk` returns the central
gradient and the full symmetric Hessian:

- a single curve has 11 factors;
- two curves have 22, discount buckets first, then projection;
- for two curves the discount × projection block uses the exact bilinear form
  `PV = N * a(p) . d(q)`; a test checks it against the generic four-point scheme.

`swap_factor_risk(risk, discount_curve_id=..., projection_curve_id=None, second_order=None) -> FactorRisk`
maps the result to the desk factors `IR|<curve_id>|<bucket>`.

- A single-curve swap takes one curve ID.
- A two-curve swap needs two distinct IDs.
- Without `second_order`, the risk must be central.

## Carry and rolldown

`swap_horizon(request, horizon_date=...)` and `engine.horizon(horizon_date)` follow
the [horizon module](CONVENTIONS.md#horizon-valuation-carry-and-rolldown-0140):

- `received_rub`: flows paid in `(valuation date, horizon]`, as cash. The float
  amounts are today's projections.
- `pv_forward_rub`: the remaining flows at the horizon on today's curves read
  forward, `DF(t) / DF(horizon)`.
- `pv_rolled_rub`: the remaining flows on rolled curves, with the shape held in
  calendar days (`roll_curve`; a key-rate forward curve shifts all its dates).
  Fixings between today and the horizon are today's forwards.
- `carry = pv_forward + received - pv_start`, `rolldown = pv_rolled - pv_forward`,
  `horizon_pnl = carry + rolldown`.

On flat curves the rolldown is zero, and the carry is
`sum_{t > h} CF * DF(t) * (1 / DF(h) - 1) + sum_{t <= h} CF * (1 - DF(t))`.

## Identity

`base_id` hashes the following; equal inputs give equal IDs:

- the model version and the swap terms;
- the convention identifier;
- the calendar version and fingerprint;
- both curves' bases and content;
- the fixing history's identity (snapshot ID, `known_at`, content hash).

Model versions: `rub-irs-ois-krs-v1`, `rub-irs-ois-krs-second-order-v1` and
`rub-irs-horizon-carry-v1`.

## Validation

`tests/test_swaps.py` checks:

- **Par swaps at inception.** Every quote of a 9-tenor OIS bootstrap reprices to
  `|PV| < 1e-12 * N`, payer and receiver. Every KRS calibration quote does too,
  and its schedule, annuity and float PV equal `price_key_rate_swap`.
- **Seasoned PVs against an independent reconstruction.**
  - A two-curve OIS: backward quarterly float and annual fixed, modified
    following, a 1-day payment lag, a spread, RUONIA fixings across weekends.
  - A KRS: a key-rate change inside the current period, a forward curve.
  - Accrued and clean PV for both.
- **Risk.**
  - DV01 sign by side, and `dv01 = sum of buckets`.
  - Central DV01 converges with the bump size at second order.
  - The Hessian gradient equals the risk, and a Taylor expansion matches exact
    repricing.
  - Known flows have zero projection risk.
- **Carry.** On flat curves it matches the closed form above.
- **Rejections.** Typed rejections, round trips through `to_dict`/`from_dict`,
  and identities.

## Performance (2k positions)

`benchmarks/swap_bench.py --positions 2000` builds 1000 seasoned single-curve OIS
(17 OIS quotes, quote basis) and 1000 two-curve KRS (OIS quotes for discounting,
a 7-quote KEYRATE forward curve). Each set shares one `ShockedCurveCache`. Linux
x86_64, Python 3.11, ms per position:

| Product | First order | + Hessian (cold cache) | + Hessian (warm cache) |
| --- | --- | --- | --- |
| OIS, 11 factors | 1.5 | 8.6 | 4.7 |
| KRS, 22 factors | 1.9 | 8.5 | 5.4 |

A cold run pays about 250 OIS rebootstraps per curve, about 3.7 s per job. The
warm run repeats the job on the filled cache, which is what a backend sees when
it seeds the cache with persisted shocked curves (`ShockedCurveCache.seed`).
CI also runs the benchmark on windows-latest, Python 3.11. That run is
informational.

## Not covered yet

- **RUONIA OIS to the exchange specification** (`rub_ruonia_ois_moex_v2`): TOM
  start, payment lag, stubs, lookback or lockout (BACKLOG #37). Swaps can
  already carry an explicit spot and payment lag; lookback, lockout and
  observation shift are not modelled.
- **Key-rate quote-basis risk**: shift the KRS quotes and recalibrate, with the
  discount curve coupled. Today the risk is in shifts of the forward key rate.
- **Other terms**: explicit confirmation schedules (instead of
  convention-generated ones), stubs other than the roll's natural one,
  amortizing notionals, compounded spreads, caps and floors.
- **Integration with `value_portfolio` and `domain.Position`**; see the
  package 2 release notes.
