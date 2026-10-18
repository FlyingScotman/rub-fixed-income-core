# Cross-currency swaps: CNY/RUB against RUONIA (package 7)

`rub_fixed_income.xccy` values outstanding cross-currency swaps with one RUB leg and
one foreign leg. It also bootstraps the CNY discount curve under RUB collateral from
XCCY par quotes (the hub's `cny_ruonia_xccy`), and gives the risk on every curve, the
basis and FX, with cross terms. The framework is that of
[FX forwards](fx-forwards.md) and [seasoned swaps](swaps.md):

- explicit terms, curves and risk bases;
- the 11-bucket grid and a shared `ShockedCurveCache`;
- exact scenarios, a central Hessian and content identities.

The module does no I/O.

## The product

A leg is a bond in its currency. Its receiver:

1. pays the notional at the start (initial exchange);
2. receives fixed or floating coupons;
3. receives the notional back at the end (final exchange), and any amortization in
   between (intermediate exchanges).

The two legs have opposite sides. The MOEX/NCC product parameters describe the same
structure: exchange of notionals in two currencies, interim interest payments in each
currency, reverse exchange at the end. The NFA 2011 standard terms say the same in
6.1–6.4.

- **RUB leg.** Fixed, or floating on `RUB-RUONIA-OIS-COMPOUND`.
- **Foreign leg.** Fixed, or floating on an explicit compounded overnight index of its
  currency (for example `RUSFARCNY`).
- **Spread.** On either floating leg. It is added simply:
  `N * (rate + spread) * alpha` (NFA 2011, 7.3(а), no compounding condition).
- **Notionals** are fixed by the terms, per period.
  - A notional that falls between periods is repaid by an intermediate exchange
    (NFA 6.4(д)).
  - A mark-to-market (resettable) swap raises `UnsupportedProduct`. The NCC parameters
    list no notional reset, and its FX fixing would be unspecified.

## Terms

| Type | Fields |
| --- | --- |
| `XccyPeriod(accrual_start, accrual_end, payment_date, notional, known_amount=None)` | One interest period `[start, end)` and its outstanding notional. `known_amount` is a published or confirmed coupon (unsigned). It is used as is. |
| `NotionalExchange(payment_date, amount)` | Received by the leg's receiver when positive, paid when negative (the initial exchange). |
| `XccyLeg(currency, side, day_count, periods, exchanges=(), fixed_rate=None, index=None, spread=0.0)` | `side` is `receive` or `pay`, for the position. Give exactly one of `fixed_rate` and `index`. A fixed leg has no spread. |
| `CompoundedIndex(name, weight_basis, annualization_basis)` | `rate = (prod(1 + r_i * n_i / W) - 1) * A / d`. The bases are `nfa_act`, `act_365f` or `act_360` (see below). |
| `XccySwap(trade_id, rub_leg, foreign_leg, mark_to_market_resets=False)` | Opposite sides. A RUB index other than RUONIA raises `UnsupportedProduct`. |

`RUONIA_OIS_COMPOUND = CompoundedIndex("RUB-RUONIA-OIS-COMPOUND", "nfa_act", "nfa_act")`
is NFA amendments No. 1 (2021), 8.4(б):

```text
rate = (prod(1 + RUONIA_i * n_i / Act) - 1) * Act / d
```

- `n_i` is the number of calendar days each business-day fixing applies.
- `d` is the number of days in the period.
- `Act` is 365, or 366 if any day of the period falls in a leap year.
- The coupon is `N * (rate + spread) * DCF`, with `DCF` from the leg's day count
  (Act/Act ISDA for the standard swap).

### Observation and projection

These are the rules of `swaps.py`:

- Each accrual day takes the fixing of the latest business day on or before it, in the
  fixing history's calendar. There is no fixing offset and no lookback.
- Observations before the valuation date come from the fixings:
  - a `FixingHistory` for RUONIA;
  - `IndexFixings(index, valuation_date, fixings, calendar, snapshot_id)` for a foreign
    index, which needs every observed business day.
- A missing fixing raises `MissingMarketData` and is never filled.
- From the first unobserved day `c` to the period end `e`, the projection curve gives
  `DF(c) / DF(e)`. This is exact for business-day boundaries, whatever the bases.
- A coupon is reported as `fixed`, `known`, `fixed_by_history`,
  `partially_projected` or `projected`.

### Schedules and the standard swap

- `xccy_leg_periods(ois_convention, effective, unadjusted_end, end, notional)` uses the
  period rule of `OisSwapConvention`:
  - a single period up to `single_period_max_months`;
  - otherwise periods rolled back from the unadjusted end (short initial stub);
  - each date adjusted;
  - payments `payment_lag_business_days` after each period end.
- `notional_exchanges(periods, initial_date=..., final_date=...)` builds the exchanges
  from the period notionals. A rising notional raises `InvalidTerms`.
- `XccyQuoteConvention(swap, rub_index, initial_exchange_lag_days, final_exchange_lag_days, spec_id)`
  holds every term of a quoted swap. Nothing is defaulted.
- `xcs_cnyrub_convention(calendar)` is the MOEX standard swap `XCS CNYRUB` (spec
  `cny_ruonia_xccy_v1`, registered as `CurveKind.XCCY_BOOTSTRAP`). The terms are in
  the table below.
- `standard_xccy_swap(trade_id, trade_date, tenor, convention, *, currency, foreign_notional, notional_fx_rate, fixed_rate, foreign_side, rub_spread=0.0)`
  builds one standard swap. The RUB notional is `foreign_notional * notional_fx_rate`.

| Term | `XCS CNYRUB` | Source |
| --- | --- | --- |
| Trade date, start, end | T+0; start TOM (1 business day); end = start + tenor | MOEX methodology, App. 1 §3 |
| Business days | MODFOL for the start, the end and the periods; calendars SPFI, MOSCOW, BEIJING, RUONIA (the caller's joint calendar) | same |
| Notional rate | NCC central CNY/RUB rate of the trade date (an input) | same |
| CNY leg | fixed; a single period up to 1Y, annual beyond; Short Initial stub; Act/Act ISDA; +1B payment | same |
| RUB leg | `RUONIA-OIS-COMPOUND`, fixing offset 0B, no spread, no lookback or shift; periods, stub, day count and payment as the CNY leg | same; formula NFA 8.4(б) |
| Exchanges | initial exchange on the start date, final exchange on the end date | MOEX product parameters (structure); NFA 2011 6.2(а)–(б) defaults (dates) |
| Quote | the CNY fixed par rate (MOEX curve `CNY-XCS-CNYRUO`, unit "Fraction") | MOEX methodology (1), ISS `/iss/sdfi/curves` listing |
| Term | 1W–5Y (maximum 5Y when a leg is in CNY) | methodology (2); product parameters, note |

The NCC specification of OTC cross-currency swaps would confirm the exchange dates.
It could not be reached (see [sources](sources/xccy_cnyrub/README.md)). The exchange
lags are therefore explicit fields of the convention.

## Valuation

`XccyRiskRequest(swap, spot_rate, spot_value_date, rub_discount_curve, foreign_discount_curve, basis_bp, rub_projection_curve=None, foreign_projection_curve=None, rub_fixings=None, foreign_fixings=None, basis_day_count=ACT_365_FIXED)`

- The **valuation date is the curve date**, shared by every curve and by the fixings.
- Flows paid on or before the valuation date have settled and are left out, as in
  `swaps.py`. A swap with nothing left raises `InvalidTerms`.
- `spot_rate` is RUB per one foreign unit for value date `spot_value_date`.

The value is:

```text
PV = sum_RUB CF * DF_rub(P)
   + S * DF_rub(s) / DF_for(s) * sum_foreign CF * DF_for(P) * exp(b * tau(s, P))
```

- The second sum applies covered parity to every foreign flow. A flow paid at `P` is
  worth its outright `F(P)` in RUB, discounted on the RUB curve.
- `b` is `basis_bp` (continuous, `basis_day_count`, `tau` signed). It has the sign and
  form of the FX-forward basis.
- `fx_today = S * DF_rub(s) / DF_for(s)` converts the foreign leg's value today
  (`foreign_leg_pv`) to RUB.
- **Consistency with FX forwards.** A swap with only a final exchange is exactly an FX
  forward, for any basis. A test checks the PV, every bucket, the basis, the FX delta
  and the Hessian against `FxForwardRiskEngine`.

### Curves and risk bases

| Role | Input | Risk basis |
| --- | --- | --- |
| `rub_discount` | `DiscountCurve` or OIS `CurveBootstrapRequest` | `continuous_zero` or `ois_quote` |
| `rub_projection` | as above; `None` = the discount curve (one set of factors) | as above |
| `foreign_discount` | a `DiscountCurve` (caller-built, implied with `implied_foreign_curve`, or bootstrapped here), or an `XccyCurveBootstrapRequest` | `continuous_zero`, or `xccy_quote` |
| `foreign_projection` | required for a floating foreign leg | `continuous_zero` or `ois_quote` |

On the `xccy_quote` basis the foreign curve is rebuilt under every shock. It uses the
shocked RUB curves and the XCCY quotes moved by the bucket shift at each quote's end
date. The quotes, the spot and the notional rate are held.

- A quoted par swap therefore stays at par under RUB shocks: its RUB risk is zero.
- Its CNY risk is that of its own quote.
- `XccyCurveCache` shares the rebuilt curves across positions.
- Under spot moves the curve is held (sticky curve), so the FX delta stays exact.

## Bootstrap of the CNY curve

`bootstrap_xccy_curve(XccyCurveBootstrapRequest(...), *, rub_discount_curve, rub_projection_curve=None) -> DiscountCurve`

`XccyCurveBootstrapRequest(trade_date, currency, spot_rate, spot_value_date, notional_fx_rate, quotes, convention, short_end=None, curve_convention=CurveConvention())`
takes `XccyParQuote(tenor, rate)` rows.

1. Each quote is a standard swap: receive the CNY fixed rate, pay RUONIA flat, both
   notionals exchanged, the RUB notional at `notional_fx_rate`.
2. Quotes are solved in order of their last CNY payment date. Each adds a node there:
   a constant continuous forward from the previous node.
3. Between nodes the DF is log-linear, as in the OIS bootstrap. The model is
   `xccy-cny-logdf-v1`.
4. RUONIA is projected on the RUB projection curve (default: the discount curve).
5. Every quote must reprice within 1e-7 bp; otherwise `RuntimeError`.

Only ratios to the spot date enter the swaps. The segment from the trade date to the
first node is that node's forward.

`short_end` (for example `implied_foreign_curve` from FX swaps up to 9M) supplies the
nodes before the first quote. This is how MOEX's hybrid curve `CNY-HYB-CNYRUO` takes
FX swaps up to 9M and XCS from 1Y.

## Par rates

`xccy_par_rate(request, *, leg="rub" | "foreign")` returns the leg's fixed rate (fixed
leg) or spread (floating leg) that sets the PV to zero. The PV is affine in either, so
two valuations find the root and a third checks it. For example:

- `leg="foreign"` on a standard swap returns the par CNY rate;
- `leg="rub"` returns the RUONIA spread that makes an off-market swap par (the par
  basis).

## Risk

`engine = XccyRiskEngine(request, curve_cache=ShockedCurveCache(), xccy_cache=XccyCurveCache())`

`engine.cashflows()` returns `XccyCashflow` rows:

- leg, kind (`coupon` or `exchange`), currency, dates and notional;
- the implied rate, the signed amount and the source;
- the discount factor and `pv_rub`.

The rows sum to the PV.

`engine.risk(bump_bp=1, difference="central") -> XccyRiskResult`

| Field | Meaning |
| --- | --- |
| `pv_rub`, `rub_leg_pv_rub`, `foreign_leg_pv_rub`, `foreign_leg_pv`, `fx_today` | PV and its legs |
| `curves` | One `XccyCurveRisk` per role: 11 buckets, their sum `dv01_rub` and a repriced `parallel_dv01_rub` |
| `basis01_rub` | +1 bp of `b` |
| `fx_delta_rub_per_pct` | Exactly `foreign_leg_pv_rub / 100`, the PV change for +1 % spot, since PV is linear in spot |

**Signs.** Receiving CNY fixed, paying RUONIA:

- CNY DV01 is negative (long a CNY bond);
- the FX delta is positive;
- the basis01 is positive (the CNY flows are worth more);
- a separate RUONIA projection curve has a negative DV01 (paying higher forwards).

The opposite side flips every number.

**Second order.** `engine.second_order(bump_bp=1) -> XccySecondOrderRisk`:

- factors: 11 per role, then the basis, then the spot;
- rates and basis by the central scheme of the other engines;
- the spot is exact: its diagonal is zero, and its cross term with a rate factor is 1 %
  of the central derivative of the foreign leg's RUB value;
- a factor whose single bumps leave both legs bit-identical is skipped, and its entries
  are exact zeros.

A test checks the result bit for bit against the full central helper.

**Scenarios.** `engine.reprice(XccyScenario(...))` reprices exactly. It reports RUB
rates, foreign rates, basis and spot P&L in isolation, and their interaction.

- `XccyScenario(rub_shifts_bp, foreign_shifts_bp, basis_shift_bp, fx_spot_shift_pct, rub_projection_shifts_bp=None, foreign_projection_shifts_bp=None)`.
- A projection shift of `None` moves the projection curve with the discount curve.
- `XccyScenario.parallel(...)` builds a parallel scenario.
- `XccyScenario.from_fx_scenario(FxFuturesScenario)` applies one desk scenario to
  futures, forwards and XCCY.

**Desk factors.** `xccy_factor_risk(risk, curve_ids, second_order=None) -> FactorRisk`

- `IR|<curve_id>|<bucket>` for each role, with `curve_ids` mapping roles to IDs.
  Distinct roles need distinct IDs.
- `XCCYBASIS|<ccy>` (`xccy_basis_factor`): 1 bp of `b`.
- `FX|<ccy>`: 1 % against RUB.
- With `second_order`, the sparse upper-triangle Hessian carries the FX × rates and
  RUB × CNY cross terms.

`XCCYBASIS|CNY` and `FXBASIS|CNY` are the same functional form. They are kept as
separate factors because they are calibrated to different instruments (XCS quotes vs
FX outrights). The backend can alias them if its desk treats them as one.

## Horizon (carry, rolldown, theta)

`xccy_horizon(request, horizon_date=..., horizon_spot_value_date=..., fx_horizon="spot_held")`
and `engine.horizon(horizon_date, horizon_spot_value_date=..., fx_horizon=...)` return
an `XccyHorizonValuation` (`rub-xccy-horizon-v1`). They follow the
[horizon module](CONVENTIONS.md#horizon-valuation-carry-and-rolldown-0140) and the
swaps' [carry and rolldown](swaps.md#carry-and-rolldown):

- `received_rub`: flows paid in `(valuation date, horizon]`, as cash (undiscounted, not
  reinvested). Floating amounts are today's projections.
- `pv_forward_rub`: the remaining flows at the horizon on today's curves read forward,
  `DF(t) / DF(h)`, with today's projections.
- `pv_rolled_rub`: the remaining flows at the horizon on rolled curves (`roll_curve`,
  shape held in calendar days): the desk theta. On both legs, observations before the
  horizon keep today's forwards and later ones are projected on the rolled projection
  curve.
- `carry = pv_forward + received - pv_start`, `rolldown = pv_rolled - pv_forward`,
  `horizon_pnl = pv_rolled + received - pv_start`.

The spot at the horizon `S_h`, for value date `s_h = horizon_spot_value_date` (on or
after the horizon and today's spot date), follows `fx_horizon`:

| `fx_horizon` | `S_h` | received foreign flow paid at `P` converts at |
| --- | --- | --- |
| `spot_held` (sticky spot) | `S` | `S` |
| `forwards_realised` | today's outright `F(s_h)` | today's outright `F(P)` |

with `F(d) = S * [DF_for(d) / DF_for(s)] / [DF_rub(d) / DF_rub(s)] * exp(b * tau(s, d))`.
A remaining foreign flow is worth, at the horizon on either view of the curves,

```text
CF * DF_for(t) / DF_for(s_h) * exp(b * tau(s_h, t)) * S_h * DF_rub(s_h)
```

and a RUB flow `CF * DF_rub(t)`. The basis is held in bp. With `forwards_realised`,
`pv_forward` is today's PV of the flows paid after `h` divided by `DF_rub(h)` (exact
for an additive basis day count).

Only base curves are used. A bootstrapped foreign curve (`XccyCurveBootstrapRequest`) is
read forward and rolled as built; its quotes are not rebootstrapped at the horizon.
`tests/test_xccy_horizon.py` checks a hand-computed fixed/fixed swap in both spot
rules, the `forwards_realised` identity, zero rolldown on flat curves, a horizon past
maturity, a RUB floating period straddling the horizon, a bootstrapped foreign curve
and the spot effect between the two rules.

## Identity

`base_id` hashes the following:

- the model version and the swap terms;
- the spot and its date;
- each role's basis and curve content (an `XccyCurveBootstrapRequest` by its quotes,
  convention identifier, spot, notional rate and short end);
- the basis and its day count;
- the fixings' identities.

Model versions: `rub-xccy-v1`, `rub-xccy-second-order-v1`, `rub-xccy-horizon-v1`, and
`xccy-cny-logdf-v1` for the curve. A horizon result carries the engine's `base_id`.

## Validation (`tests/test_xccy.py`, synthetic)

- **Standard terms.** The MOEX terms of `xcs_cnyrub_convention` and of the registered
  spec. The 1Y single period, MODFOL across a weekend, +1B payments, annual periods,
  the 18M short initial stub, a Chinese holiday on the joint calendar, amortizing
  exchanges.
- **Par at inception.** Every one of 10 quotes (1W–5Y) has `|PV| < 1e-10 * notional`
  on the bootstrapped curve and on the `xccy_quote` basis, on both sides. The par
  solver returns each quote to 1e-12. A separate RUONIA projection curve and an
  FX-implied short end (hybrid) also reprice. The RUONIA par spread zeroes an
  off-market swap.
- **Seasoned swap against an independent reconstruction.**
  - A 3Y swap started 14 months ago, on two RUB curves, with a spread.
  - RUONIA is compounded by hand across weekends, with `Act = 366` in the 2028 period.
  - Act/Act ISDA is computed day by day.
  - The PV matches to 1e-11, and every flow matches.
  - The current RUONIA coupon agrees with `floating.FloatingLeg`.
- **Known fixings and amounts frozen.** A coupon fixed by history has zero projection
  risk. Missing fixings raise.
- **A floating CNY leg** on an explicit index, against a closed form.
- **Consistency with `fx_forwards`.** The final-exchange-only swap with basis 0 and
  −35 bp.
- **Risk.**
  - The FX delta identity, and the exact +1 % scenario.
  - Signs by side, scaling with the notional.
  - Bucket sums equal the parallel shifts.
  - Central DV01 against an analytic derivative, with error ratio 4 per halving.
  - The forward difference is the exact +1 bp P&L.
  - The Hessian equals the generic helper bit for bit. Exact FX cross terms. A Taylor
    expansion of a joint move.
  - Quote-basis risk of a par swap: zero RUB risk, its own bucket only. Cache reuse.
  - Scenarios decompose exactly.
- **Interfaces.** Factor mapping, identity, serialization round trip, typed
  rejections.

No external golden values are possible. MOEX publishes `CNY-XCS-CNYRUO` and NCC's
`CNY-DSC-CSARUB` discount curve on ISS for subscribers only (see the sources README).

## Performance

`benchmarks/xccy_bench.py --positions 2000`: synthetic XCS CNYRUB book, half seasoned
with RUONIA history, tenors 3M–5Y, both sides. Every position shares the curves and
both caches. Linux x86_64, Python 3.11, ms per position:

| Curves | first order | + Hessian | + Hessian + 5 scenarios |
| --- | --- | --- | --- |
| discount factors (`continuous_zero` RUB and CNY) | 1.02 | 3.81 | 4.19 |
| 17 OIS quotes + 10 XCCY quotes (`ois_quote`, `xccy_quote`) | 1.44 | 9.46 | 9.41 |

On the quote basis, a cold job rebuilds about 140 OIS curves and 530 XCCY curves.
Seeding persisted OIS curves (`ShockedCurveCache.seed`) removes the OIS part. XCCY
curves are not persisted yet.

## Not covered

- Mark-to-market (resettable) notionals; RUB indices other than RUONIA (KEYRATE,
  RUSFAR); lookback, lockout or observation shift; compounded spreads (the NCC
  parameters list "с учётом Спреда" variants); the 0.0001 % rounding of settled RUONIA
  indices and the 2-decimal rounding of amounts.
- Bootstrapping from basis quotes of RUONIA plus a spread against a CNY floating index
  (MOEX `XCCY/BASIS` with `RUSFARCNY`). Only CNY fixed par quotes are bootstrapped.
- Persisted XCCY shocked curves; rebootstrapping the foreign curve from its quotes at a
  horizon.
- Portfolio carry: `value_portfolio` takes XCCY swaps (`XccyInstrument`, result contract
  v3, [portfolio-v3.md](portfolio-v3.md)); `horizon_portfolio` does not.
