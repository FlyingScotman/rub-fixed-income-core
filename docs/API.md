# API reference (0.16.0)

Everything below is importable from `rub_fixed_income`. Units: decimal rates/spreads unless
the name ends in `_bp` (basis points) or `_pct` (percent); amounts in RUB per bond; clean
prices in % of current nominal; dates are `datetime.date`. "Raises" lists `ValueError`
unless stated. New in 0.3.0 is marked **(new)**; changed behaviour is marked **(0.3.0)** or
**(0.3.1)**. Monetary inputs reject NaN, infinities and `bool` everywhere since 0.3.1
(`docs/money-validation.md`).

## Conventions — `rub_fixed_income.conventions` (new)

| Name | Description |
| --- | --- |
| `DayCount` enum: `ACT_365_FIXED`, `ACT_360`, `ACT_ACT_ISDA` | `.year_fraction(start, end) -> float`; raises if `end < start`. |
| `Compounding(periods_per_year: int)` | `0` = continuous. `.is_continuous`, `.identifier` (`annual`, `m12`, `continuous`), `.discount_factor(rate, t)`, `.rate_from_discount_factor(df, t)`. Raises `ValueError` if `< 0`, `TypeError` if not int. |
| `ANNUAL`, `SEMI_ANNUAL`, `QUARTERLY`, `MONTHLY`, `CONTINUOUS` | Constants. |
| `BusinessDayConvention` enum | `UNADJUSTED`, `FOLLOWING`, `MODIFIED_FOLLOWING`, `PRECEDING`. |
| `adjust_date(value, convention, calendar) -> date` | Business-day adjustment. |
| `calendar_fingerprint(calendar) -> str` | 12-hex content hash of holidays/working days. |
| `SettlementConvention(lag_business_days=1, calendar=BusinessCalendar())` | `.settlement_date(trade_date)`, `.identifier`. Raises for negative lag. |
| `ScheduleConvention(frequency_months=12, roll="forward", business_day_convention=MODIFIED_FOLLOWING, calendar=BusinessCalendar())` | `.identifier`, `.to_dict()`. Raises for frequency < 1 or unknown roll. |
| `generate_schedule(start, end, convention) -> tuple[date, ...]` | Adjusted payment dates in `(start, end]`, no adjusted-maturity stub. Raises if `end <= start` or adjusted dates collide. |
| `YieldConvention(compounding=ANNUAL, day_count=ACT_365_FIXED)` | Rejects `CONTINUOUS`. `.identifier` e.g. `yield:annual:ACT/365F`, `.to_dict()`. |
| `ZSpreadConvention(compounding=ANNUAL, day_count=ACT_365_FIXED)` | `.identifier` e.g. `zspread:annual:ACT/365F`, `.to_dict()`. |
| `CurveConvention(time_day_count=ACT_365_FIXED, interpolation="log-linear-df", extrapolation="flat-forward", fixed_leg_schedule=None, fixed_leg_day_count=ACT_365_FIXED, ois_swap=None)` | `.identifier`. Only the listed interpolation/extrapolation are accepted. `ois_swap` (0.22.0) reads quotes as spot-starting swaps; the fixed-leg fields must then stay at their defaults. While `None` it is left out of `.identifier`, canonical bytes and `to_dict`, so existing identities are unchanged. |
| `OisSwapConvention(calendar, spot_lag_business_days=1, payment_lag_business_days=1, business_day_convention=MODIFIED_FOLLOWING, single_period_max_months=12, frequency_months=12, stub="short_initial", fixed_day_count=ACT_ACT_ISDA, float_day_count=ACT_ACT_ISDA, float_index="RUB-RUONIA-OIS-COMPOUND")` (0.22.0) | MOEX OIS RUONIA quote convention. `.periods(effective, unadjusted_end, end) -> tuple[OisSwapPeriod, ...]`, `.float_amount_scale(start, end)` (`Act / d × float DCF`, NFA 8.4(б)), `.identifier`. Raises for negative lags, non-positive months, other stubs or indices. |
| `OisSwapPeriod(start, end, payment)` (0.22.0) | One accrual period `[start, end)` paid on `payment`. |

All convention objects are frozen and hashable.

## Dates — `rub_fixed_income.dates`

- `year_fraction(start, end) -> float` — ACT/365F (legacy).
- `add_months(value, months) -> date` — end-of-month clamp.
- `annual_schedule(start, maturity) -> tuple[date, ...]` — legacy; keeps the adjusted-maturity stub. Raises if `maturity <= start`.
- `BusinessCalendar(holidays=frozenset(), working_days=frozenset(), version="weekends-only")` — `.is_business_day`, `.add_business_days(value, n)` (n ≥ 0), `.modified_following`.

## Cash flows

- `EventKey(event_date, ordinal)`, `CashEvent(key, amount_rub_per_bond, event_id)`, `BondCashflow(payment_date, amount)`.
- `clean_price_pct_to_dirty_rub(clean_pct, current_nominal_rub, accrued_interest_rub) -> float`; `dirty_rub_to_clean_price_pct(...)`. Raise for non-positive nominal or negative AI; **(0.3.1)** also for clean/dirty price ≤ 0, dirty ≤ AI (implied clean ≤ 0), non-finite/`bool` input, overflowing result.
- `clean_price_pct_to_dirty_amount(...)` — alias. **Deprecated (0.12.0)**: use `clean_price_pct_to_dirty_rub`.

## Discount curves

- `DiscountCurve(curve_date, nodes, model_version="ois-logdf-v1", _flat_continuous_rate=None, convention=CurveConvention())` **(0.3.0: `convention` field; non-finite DFs rejected)**.
  - `.discount_factor(d)` — raises if `d < curve_date`.
  - `.zero_rate(d, periods_per_year=1)` — int ≥ 1 or `Compounding` (incl. `CONTINUOUS`).
  - `.forward_rate(start, end)` — annual compounding over `τ(start, end)`.
  - `.par_rate(maturity)` / `.par_ois_rate(maturity)`; `.fixed_leg_schedule(maturity)` **(new)**.
  - `.max_repricing_error_bp(quotes)` — uses `.quote_par_rate`.
  - `.quote_par_rate(quote)` (0.22.0) — `.par_rate(quote.maturity)` for the legacy convention; with `convention.ois_swap` the par rate of the spot-starting swap of the quote's tenor traded on `curve_date`.
  - `.ois_swap_par_rate(periods)` (0.22.0) — par fixed rate of a swap with these `OisSwapPeriod`s; raises `ValueError` without `convention.ois_swap`.
  - `DiscountCurve.flat(curve_date, continuous_rate, *, convention=None)` — raises for non-finite rate.
- `OisQuote(tenor, maturity, par_rate)`.
- `CurveBootstrapRequest(curve_date, quotes, convention=CurveConvention())` **(0.3.0: `convention`)**.
- `bootstrap_discount_curve(request) -> DiscountCurve` — raises for empty / non-increasing / non-finite quotes, unbracketed forward; `RuntimeError` if repricing > 1e-7 bp. With `convention.ois_swap` (0.22.0) each quote must be a `D/W/M/Y` tenor whose resolved end is its maturity (`InvalidTerms` otherwise).
- `ois_quote_periods(trade_date, quote, convention) -> tuple[OisSwapPeriod, ...]` (0.22.0) — periods of the spot-starting swap behind `quote` (tenor resolved with `resolve_tenor`).

## Yield, duration, spreads (`compatibility`)

- `solve_nominal_annual_yield(*, dirty_price, settlement_date, cashflows, compounding_frequency=1, day_count_basis=365.0, convention: YieldConvention | None = None) -> float`
  Raises: non-finite/≤0 price, non-finite cash flows, no future positive flow, invalid
  frequency/basis, both `convention` and legacy keywords, **root below −0.99·m or above 1e6 →
  "not bracketed" (0.3.0)**.
- `bond_duration(*, annual_yield, settlement_date, cashflows, compounding_frequency=1, day_count_basis=365.0, convention=None) -> DurationResult(macaulay_years, modified_years, present_value)`
  Raises: non-finite yield **(0.3.0)**, base `1 + y/m <= 0`, no positive PV, non-finite/`bool` flows and overflowing PV or duration **(0.3.1)**.
- `price_cashflows(cashflows, curve, *, spread=0.0, compounding_frequency=1, settlement_date=None, convention: ZSpreadConvention | None = None) -> float` — **deprecated without `settlement_date` (0.12.0)**.
  Raises: non-finite spread/cash flows **(0.3.0)**, settlement before curve date, rate outside compounding domain, `bool` flows and overflowing result **(0.3.1)**. Signed flows netting to 0 return `0.0`.
- `solve_z_spread(cashflows, dirty_price, curve, *, compounding_frequency=1, settlement_date=None, convention=None) -> float`
  Raises: empty flows, non-finite/≤0 price **(0.3.0)**, non-finite flows **(0.3.0)**, no flow after settlement **(0.3.0)**, settlement before curve date, conflicting `compounding_frequency` + `convention`, "Z-spread root is not bracketed" (bracket −95%…500%), non-finite objective mid-solve **(0.3.0)**; `RuntimeError` "did not converge".
- `solve_continuous_z_spread(cashflows, dirty_price, curve, *, settlement_date=None) -> float` — continuous Z over settlement-forward DFs; bracket ±500%. **Deprecated (0.12.0)**: use `solve_z_spread` with a continuous `ZSpreadConvention` and `settlement_date`.

## Effective yield and financing (`bond`)

- `YieldRequest(horizon_date, dirty_price_rub_per_bond, cashflows)`; `solve_effective_annual_yield(request) -> YieldResult(effective_annual_yield, repricing_error_rub_per_bond)`. Raises: no flows, non-finite/≤0 price **(0.3.0: non-finite)**, non-finite flows **(0.3.0)**, flows on/before horizon, "forward yield root is not bracketed".
- `AccumulationPeriod`, `constant_daily_accumulation_periods(start, horizon, boundaries, annual_rate)`.
- `FinancedBondRequest`, `project_financed_bond(request) -> FinancedBondProjection(horizon_dirty_rub_per_bond, steps)`. Raises **(0.3.1)**: non-finite/`bool` cash-event amount, `bool` initial dirty cash or factor, overflowing balance. Signed finite cash events are valid (an expense of −50 adds 50 to the balance).

## Snapshot analytics (`analytics`)

- Inputs: `BondTermsV1(issue_id, current_nominal_rub, maturity_date, horizon, horizon_date, accrual_period_start, accrual_period_end, coupon_amount_rub, terms_hash)`, `MarketObservationV1(clean_price_pct_of_current_nominal, settlement_date, selected_price_basis, market_hash)`, `CurveSnapshotV1(curve_world_instance_id, discount_curve, curve_hash)`, `ValuationContext(valuation_date, settlement_date)`, `CalculationPolicy(money_quantum=Decimal("0.01"), spread_lower_bp=-5000, spread_upper_bp=5000, root_tolerance=1e-13)` (+ `.validate()` **(new)**).
- `analyze_bond(terms, schedule, market, curve, context, policy) -> BondAnalyticsResultV1 | CalculationFailure` — frozen V1 conventions (continuous Z). **Deprecated (0.12.0)**: use `analyze_bond_v2`. **(0.3.0)**: invalid policy → `INVALID_UNITS`; AI 0 on coupon date.
- `analyze_bond_v2(..., *, yield_convention=None, z_spread_convention=None) -> BondAnalyticsResultV2 | CalculationFailure` **(new)** — default annual Z; result adds `yield_convention`, `z_spread_convention`.
- `V1_YIELD_CONVENTION`, `V1_Z_SPREAD_CONVENTION` **(new)** constants.
- Result units: `accrued_interest_rub`, `dirty_price_rub_per_bond` (RUB per bond), `yield_pct` (%), durations (years), `z_spread_bp` (bp), `repricing_error_rub_per_bond`.
- `accrued_interest_rub(terms, settlement) -> float` (module-level in `analytics`) **(0.3.0: 0 at period end)**; raises outside `[start, end]`. **(0.3.1)**: runs `terms.validate()` first, so it also raises for invalid nominal/coupon and a horizon inconsistent with maturity, even on the coupon date.
- `canonical_json_bytes(value)`, `canonical_sha256(value)`, `result_hash_projection(result)`, `verify_result_sha256(result)` — accepts V1 or V2 DTOs or their JSON mappings; field set must exactly match V1 or V2.

## Trade grids (`trade_grid`)

`GridIdentity`, `GridBuildPolicy(metric_error_bp_max, max_nodes=129)`, `TradeGrid`, `TradeGridNode`, `TradeMetricLookup`, `build_trade_grid(*, grid_id, structure, center_metric_bp, exact_price, policy, identity, valid_through=None)`, `lookup_trade_metric(grid, clean_price_pct_of_nominal, exact_metric, *, as_of=None, expected_identity=None)`. Unchanged in 0.3.0.

## Key-rate swaps (`key_rate`)

`KeyRateSwapQuote`, `KeyRateForwardCurve`, `KeyRateForwardSegment`, `KeyRateSwapPricingRequest`, `price_key_rate_swap`, `KeyRateCurveBuildRequest`, `build_key_rate_forward_curve`, result/diagnostic DTOs. Unchanged numerics in 0.3.0 (ACT/ACT ISDA now delegated to `DayCount.ACT_ACT_ISDA`).

## Fixed-cashflow risk and scenarios (0.4.0)

`BondRiskRequest`, `BondRiskEngine`, and `BondScenario` provide signed RUB DV01/KRD, CS01/OFZ spread risk, and exact same-date repricing. Curve basis and spread convention are explicit; a calibrated base spread stays frozen under curve shocks. See [risk contract and example](risk.md) for inputs, methods, identities and supported scope.

## Floating coupons (0.8.0)

`FixingHistory`, `FloatingCouponRule`, `FloatingCouponPeriod`, `FloatingLeg`,
`FloatingCouponProjection` and `FLOATING_MODEL_VERSION`. Risk support comes through
`BondRiskRequest(floating_leg=...)` and `FLOATING_RISK_MODEL_VERSION`. See
[floating coupons](floating.md).

## FX futures (0.8.1)

`FxFuturesContract`, `FxFuturesRiskRequest`, `FxFuturesRiskEngine` (`.risk`, `.reprice`,
`.second_order`), `FxFuturesScenario`, `FxFuturesRiskResult`, `FxFuturesScenarioResult`,
`FxFuturesSecondOrderRisk`, `FX_FUTURES_RISK_MODEL_VERSION` and
`FX_FUTURES_SECOND_ORDER_MODEL_VERSION`. Prices are in RUB per `price_quote_units`
foreign units. Money is signed RUB of variation margin. See [FX futures](fx-futures.md).

## FX forwards and FX swaps (package 4)

Module `rub_fixed_income.fx_forwards`; see [FX forwards and swaps](fx-forwards.md).
Money is signed RUB of PV, discounted on the RUB curve from the payment date.

| Name | Description |
| --- | --- |
| `FxForwardLeg(value_date, notional_foreign, contract_rate, payment_date=None)` | One exchange; notional signed (+ = receive foreign), rate in RUB per `quote_units`. `payment_date` for a cash-settled leg (`None` = deliverable). `.settlement_date`. |
| `FxForward(trade_id, currency, quote_units, leg)`, `FxSwap(trade_id, currency, quote_units, near, far)` | Outright (also an unsettled spot trade) and swap (opposite-sign legs, far later; `.swap_points`, `.far_leg_forward()`). `.legs`. Invalid terms raise `InvalidTerms`. |
| `FxForwardRiskRequest(instrument, spot_rate, spot_value_date, rub_curve, foreign_curve, basis_bp, basis_day_count=ACT_365_FIXED)` | `basis_bp` required (0 on an implied foreign curve). Settled legs (before the curve date) raise `InvalidTerms`. |
| `FxForwardRiskEngine(request, curve_cache=...)` | `.base_pv_rub`, `.base_id`, `.leg_valuations()`, `.risk(bump_bp=1, difference="forward"\|"central") -> FxForwardRiskResult`, `.reprice(FxFuturesScenario) -> FxForwardScenarioResult`, `.second_order(bump_bp=1) -> FxForwardSecondOrderRisk` (24 factors, spot exact). |
| `FxForwardRiskResult`, `FxForwardBucketRisk(bucket, dv01_rub)`, `FxForwardLegValuation`, `FxForwardScenarioResult`, `FxForwardSecondOrderRisk` | Results; `FX_FORWARD_RISK_MODEL_VERSION`, `FX_FORWARD_SECOND_ORDER_MODEL_VERSION`. |
| `fx_forward_factor_risk(risk, rub_curve_id, foreign_curve_id, second_order=None) -> FactorRisk` | `IR\|<rub>\|*`, `IR\|<foreign>\|*`, `FXBASIS\|<ccy>`, `FX\|<ccy>`; the sparse Hessian includes FX × rates terms. |
| `fx_forward_outright(*, spot_rate, spot_value_date, value_date, rub_curve, foreign_curve, basis_bp, basis_day_count=ACT_365_FIXED)`, `fx_forward_basis_bp(*, ..., market_outright, ...)` | Covered-parity outright; basis that reprices a market outright. |
| `cip_outright(S, start, end, *, rub_rate, foreign_rate, rub_day_count, foreign_day_count)`, `cip_implied_rub_rate(S, F, start, end, *, foreign_rate, ...)`, `cip_implied_foreign_rate(S, F, start, end, *, rub_rate, ...)` | Money-market parity with simple rates; day counts explicit. |
| `FxSwapPointsQuote(label, near_date, far_date, points)`, `FxOutrightQuote(label, value_date, outright)` | Caller-supplied curve quotes (MOEX CETS swaps, OTC outrights, FX futures prices). |
| `implied_outrights(spot_rate, spot_value_date, quotes)`, `implied_foreign_curve(*, rub_curve, spot_rate, spot_value_date, quotes, convention=None) -> DiscountCurve` | Outrights chained from the spot; the foreign curve that reprices every quote with zero basis (`FX_IMPLIED_CURVE_MODEL_VERSION`). Must reach the curve date. |
| `joint_calendar(calendars, *, version)` | Business day only if one in every calendar (FX settlement). |
| `fx_value_date(trade_date, code, calendar, *, spot_lag_days=None, business_day_convention=MODIFIED_FOLLOWING)`, `fx_swap_value_dates(trade_date, near, far, calendar, ...)`, `FX_VALUE_DATE_CODES` | `TOD` (settlement days only), `TOM`, `SPT`, or a tenor from spot (`spot_lag_days` required). |

## Interest-rate swaps (package 2)

Seasoned RUONIA OIS and key-rate swaps; see [swaps](swaps.md).

| Name | Description |
| --- | --- |
| `InterestRateSwap(swap_id, side, notional, fixed_rate, effective_date, termination_date, convention, float_spread=0.0, currency="RUB")` | Terms. `side` is `payer` (pays fixed) or `receiver`; `termination_date` is unadjusted; non-RUB raises `UnsupportedProduct`. `.from_tenor(*, swap_id, side, notional, fixed_rate, trade_date, tenor, calendar, convention, float_spread=0.0)` resolves spot start and end with `resolve_tenor`. |
| `SwapConvention(index, averaging, fixed_leg, float_leg, rate_day_count, business_day_convention, spot_lag_days, payment_lag_days, spec_id=None)`, `SwapLegSchedule(frequency_months, roll, day_count, single_period_up_to_months=None)` | Every convention explicit. `index` `ruonia` (compounded) or `key_rate` (arithmetic); `roll` `forward`, `backward` or `legacy-annual-forward`. `.identifier`. |
| `swap_convention(spec_id)` | Convention of `rub_key_rate_swap_v1` or `rub_ruonia_ois_legacy_v1`; other specs raise `UnknownCurveSpec` / `UnsupportedProduct`. |
| `swap_schedule(swap, calendar) -> SwapSchedule(fixed, floating)` | `SwapPeriod(accrual_start, accrual_end, payment_date, accrual_factor)` per leg. |
| `SwapRiskRequest(swap, calendar, discount_curve, projection_curve=None, fixings=None)` | Valuation date = discount curve date. Discount: `DiscountCurve` or `CurveBootstrapRequest`; projection: `None` (single curve), `DiscountCurve` / `CurveBootstrapRequest` (RUONIA) or `KeyRateForwardCurve` (key rate); `fixings` a `FixingHistory` of the index, required once a period has started. |
| `SwapRiskEngine(request, curve_cache=ShockedCurveCache())` | `.valuation() -> SwapValuation` (PV, legs, accrued, clean PV, par rate, annuity, `SwapCashflow`s), `.risk(bump_bp=1, difference="central") -> SwapRiskResult` (`SwapCurveRisk` per curve role with `BucketRisk`s, KRD per notional), `.reprice(SwapScenario) -> SwapScenarioResult`, `.second_order(bump_bp=1) -> SwapSecondOrderRisk` (11 or 22 factors), `.horizon(date) -> SwapHorizonValuation`. |
| `SwapScenario(discount_shifts_bp, projection_shifts_bp)`, `.parallel(discount_bp=0, projection_bp=0)` | Absolute bp bucket shifts per curve. |
| `swap_factor_risk(risk, *, discount_curve_id, projection_curve_id=None, second_order=None) -> FactorRisk` | Desk factors `IR\|<curve_id>\|<bucket>`; one ID for a single curve, two distinct IDs otherwise; central only. |
| `swap_horizon(request, *, horizon_date, curve_cache=None) -> SwapHorizonValuation` | Carry, rolldown, received flows and horizon P&L. |
| `SWAP_MODEL_VERSION`, `SWAP_SECOND_ORDER_MODEL_VERSION`, `SWAP_HORIZON_MODEL_VERSION` | Model versions recorded in results. |

`benchmarks/swap_bench.py --positions 2000` times 1000 OIS and 1000 KRS positions.

## Cross-currency swaps (package 7)

CNY/RUB swaps against RUONIA, module `rub_fixed_income.xccy`; see [xccy](xccy.md).
Money is signed RUB of PV; foreign flows are converted by covered parity at spot.

| Name | Description |
| --- | --- |
| `XccySwap(trade_id, rub_leg, foreign_leg, mark_to_market_resets=False)` | Opposite-side legs. Resets and RUB indices other than RUONIA raise `UnsupportedProduct`. `.currency`. |
| `XccyLeg(currency, side, day_count, periods, exchanges=(), fixed_rate=None, index=None, spread=0.0)` | `side` `receive`/`pay`; exactly one of `fixed_rate` and `index`; `spread` decimal, simple (NFA 7.3(а)). `.sign`, `.is_floating`. |
| `XccyPeriod(accrual_start, accrual_end, payment_date, notional, known_amount=None)`, `NotionalExchange(payment_date, amount)` | Interest period with its notional (a published `known_amount` is frozen); a notional payment, + received by the leg's receiver. |
| `CompoundedIndex(name, weight_basis, annualization_basis)`, `RUONIA_OIS_COMPOUND` | `(prod(1 + r_i n_i / W) - 1) * A / d`; bases `nfa_act` (365, or 366 with a leap day in the period), `act_365f`, `act_360`. `RUONIA_OIS_COMPOUND` is NFA 8.4(б). |
| `IndexFixings(index, valuation_date, fixings, calendar, snapshot_id)` | Fixings of a foreign index; RUONIA uses `FixingHistory`. |
| `xccy_leg_periods(ois_convention, effective, unadjusted_end, end, notional)`, `notional_exchanges(periods, *, initial_date, final_date)` | Periods on the `OisSwapConvention` rule; initial, intermediate (amortization) and final exchanges. |
| `XccyQuoteConvention(swap, rub_index, initial_exchange_lag_days, final_exchange_lag_days, spec_id)`, `xcs_cnyrub_convention(calendar)`, `XCCY_QUOTE_SPEC_ID` | Terms of a quoted swap; the MOEX `XCS CNYRUB` standard swap, spec `cny_ruonia_xccy_v1` (`CurveKind.XCCY_BOOTSTRAP`). |
| `standard_xccy_swap(trade_id, trade_date, tenor, convention, *, currency, foreign_notional, notional_fx_rate, fixed_rate, foreign_side, rub_spread=0.0)` | A standard foreign-fixed vs RUONIA swap from a tenor code. |
| `XccyParQuote(tenor, rate)`, `XccyCurveBootstrapRequest(trade_date, currency, spot_rate, spot_value_date, notional_fx_rate, quotes, convention, short_end=None, curve_convention=CurveConvention())`, `bootstrap_xccy_curve(request, *, rub_discount_curve, rub_projection_curve=None) -> DiscountCurve` | The foreign discount curve (RUB collateral) that reprices every par quote within 1e-7 bp (`XCCY_CURVE_MODEL_VERSION`); `short_end` e.g. an FX-implied curve. |
| `XccyRiskRequest(swap, spot_rate, spot_value_date, rub_discount_curve, foreign_discount_curve, basis_bp, rub_projection_curve=None, foreign_projection_curve=None, rub_fixings=None, foreign_fixings=None, basis_day_count=ACT_365_FIXED)` | Valuation date = curve date; flows paid on or before it are settled. Foreign discount: `DiscountCurve` or `XccyCurveBootstrapRequest` (`xccy_quote` basis). |
| `XccyRiskEngine(request, curve_cache=ShockedCurveCache(), xccy_cache=XccyCurveCache())` | `.base_pv_rub`, `.base_id`, `.roles`, `.fx_today`, `.cashflows() -> XccyCashflow`s, `.risk(bump_bp=1, difference="central") -> XccyRiskResult`, `.reprice(XccyScenario) -> XccyScenarioResult`, `.second_order(bump_bp=1) -> XccySecondOrderRisk`. |
| `XccyRiskResult`, `XccyCurveRisk`, `XccyBucketRisk`, `XccyCashflow`, `XccyScenarioResult`, `XccySecondOrderRisk`, `XccyCurveCache` | Results (`XCCY_MODEL_VERSION`, `XCCY_SECOND_ORDER_MODEL_VERSION`); the shared cache of rebuilt foreign curves. |
| `XccyScenario(rub_shifts_bp, foreign_shifts_bp, basis_shift_bp, fx_spot_shift_pct, rub_projection_shifts_bp=None, foreign_projection_shifts_bp=None)`, `.parallel(...)`, `.from_fx_scenario(FxFuturesScenario)` | Exact scenarios; projection shifts default to the discount shifts. |
| `xccy_par_rate(request, *, leg)` | The fixed rate or spread of `leg` (`rub` / `foreign`) that zeroes the PV. |
| `xccy_factor_risk(risk, curve_ids, second_order=None) -> FactorRisk`, `xccy_basis_factor(ccy)` | `IR\|<curve>\|<bucket>` per role, `XCCYBASIS\|<ccy>`, `FX\|<ccy>`; sparse Hessian with FX × rates and RUB × foreign terms. |

`benchmarks/xccy_bench.py --positions 2000` times a synthetic XCS CNYRUB book.
## Credit default swaps (package 8)

Module `rub_fixed_income.cds`; see [CDS](cds.md). ISDA Standard Model; money in the
contract currency, signed by side (a protection seller is long credit). Every convention
and the recovery are explicit.

| Name | Description |
| --- | --- |
| `CdsConvention(calendar, payment_frequency_months, business_day_convention, accrual_day_count, step_in_days, cash_settlement_days, accrual_on_default, hazard_day_count)`, `.isda_standard(calendar)`, `.identifier` | No defaults; `isda_standard` = quarterly IMM, following, ACT/360, step-in T+1, cash T+3 bd, ISDA accrual on default, ACT/365F hazard. ACT/ACT raises `UnsupportedProduct`. |
| `AccrualOnDefault` | `NONE`, `EXACT`, `ISDA_HALF_DAY_BIAS` (the Standard Model code). |
| `CdsSide` | `BUYER`, `SELLER`; `.sign` (+1 / −1). |
| `previous_imm_date(d)`, `standard_cds_maturity(trade, tenor_months, *, roll)` | `roll` `"quarterly"` or `"semiannual"` (ISDA rule from 20 Dec 2015). |
| `cds_trade_dates(trade, convention, *, maturity_date) -> CdsTradeDates` | Step-in, cash settlement, standard accrual start (latest adjusted IMM date on or before step-in, before maturity). |
| `cds_premium_schedule(accrual_start, maturity, convention) -> tuple[CdsPremiumPeriod, ...]` | `accrual_start`, exclusive `accrual_end` (last = maturity + 1), `payment_date`, `year_fraction`. |
| `HazardCurve(curve_date, nodes, day_count=ACT_365_FIXED)`, `.flat(curve_date, h)` | Piecewise-constant hazard, flat beyond the last node; `.survival_probability(d)`, `.log_survival(d)`, `.hazard_rate(d)`, `.node_dates`, `.hazard_rates`. Negative rates raise `InvalidTerms`. |
| `CdsSpreadQuote(maturity, par_spread)`, `CdsUpfrontQuote(maturity, coupon, upfront)` | Par spread (clean upfront 0); clean upfront per unit notional the buyer pays at cash settlement. |
| `CdsCurveRequest(valuation_date, quotes, recovery_rate, convention)` | Standard contracts traded on the valuation date; recovery in `[0, 1)`, mandatory. |
| `bootstrap_hazard_curve(request, discount_curve) -> HazardCurve` | One hazard per quote; reprices every quote within 1e-12; negative hazard raises `InvalidTerms`. |
| `flat_hazard_from_spread`, `flat_hazard_from_upfront`, `upfront_from_spread`, `spread_from_upfront`, `cds_par_spread` | Quote conversions (keyword `valuation_date`, `maturity_date`, `recovery_rate`, `discount_curve`, `convention`). |
| `CdsContract(contract_id, reference_entity, currency, side, notional, coupon, accrual_start_date, maturity_date, convention)` | Terms; invalid terms raise `InvalidTerms`. |
| `value_cds(contract, *, valuation_date, discount_curve, hazard_curve, recovery_rate) -> CdsValuation` | Legs, accrued premium, dirty `pv`, `clean_pv`, `cash_settlement_amount`, `par_spread`, `risky_annuity`, `points_upfront`. Matured contracts raise `InvalidTerms`. |
| `CdsRiskRequest(contract, valuation_date, discount_curve, credit_curve)` | Discount: `DiscountCurve` or `CurveBootstrapRequest` (dated on the valuation date); credit: a `CdsCurveRequest`. |
| `CdsRiskEngine(request, curve_cache=ShockedCurveCache(), credit_cache=CdsCurveCache())` | `.valuation`, `.hazard_curve`, `.base_pv`, `.base_id`; `.risk(bump_bp=1, recovery_bump=0.01, difference="central") -> CdsRiskResult` (`buckets`, `dv01`, `parallel_dv01`, `cs01`, `quote_buckets`, `recovery01`); `.reprice(CdsScenario) -> CdsScenarioResult`; `.second_order(bump_bp=1) -> CdsSecondOrderRisk` (11 buckets + `CS`); `.jump_to_default(*, recovery_rate) -> CdsDefaultResult`. Quotes are held under every shock. |
| `CdsScenario(curve_shifts_bp, spread_shift_bp=0, recovery_shift=0)`, `.parallel(curve_bp=0, *, spread_shift_bp=0, recovery_shift=0)` | Absolute bp shifts; recovery shift as a decimal. |
| `CdsCurveCache(max_entries=65536)` | Shocked hazard curves shared across a job's positions (about 330 per reference entity with the Hessian). |
| `cds_factor_risk(risk, discount_curve_id, second_order=None) -> FactorRisk` | `IR\|<curve_id>\|<bucket>` and `CS`; central only. |
| `CDS_MODEL_VERSION`, `CDS_RISK_MODEL_VERSION`, `CDS_SECOND_ORDER_MODEL_VERSION` | Model versions recorded in results. |

`benchmarks/cds_bench.py --positions 2000` times a synthetic CDS book.

## Foundations (0.9.0)

| Name | Description |
| --- | --- |
| `BusinessCalendar(holidays, working_days, version, valid_from=None, valid_to=None)` | Bounded calendars raise `CalendarRangeError` outside `[valid_from, valid_to]`. `.is_bounded`, `.covers(d)`, `.require_covered(d)`. Sets are frozen on construction. |
| `calendar_identity(calendar) -> (version, fingerprint)` | What to record in results. |
| `canonical_json_bytes(value)`, `canonical_sha256(value)` | Now in `rub_fixed_income.canonical` (still importable from `analytics`). Enums, sets, nested dataclasses and date keys have canonical forms. |
| `to_dict(value)`, `from_dict(cls, data)` | Strict JSON-ready round trip of core dataclasses; `from_dict` re-runs validation. |
| `CoreRejection` and `UnsupportedProduct`, `MissingMarketData`, `CalendarRangeError`, `UnknownCurveSpec`, `InvalidTerms` | Typed rejections with a stable `.code`; all subclass `ValueError`. |
| `DiscountCurve.flat_continuous_rate` | Rate of a `DiscountCurve.flat` curve, `None` for node curves. |

## Domain model (0.10.0)

| Name | Description |
| --- | --- |
| `Cashflow.split(payment_date, *, coupon, principal, accrual_start, accrual_end, kind)` | A `BondCashflow` with the coupon/principal split; accepted wherever `BondCashflow` is. |
| `CashflowKind` | `COUPON`, `AMORTIZATION`, `MATURITY`, `OFFER_REDEMPTION`. |
| `accrued_interest(cashflows, settlement, *, quantum=Decimal("0.01"))` | Accrued from the split; `quantum=None` returns the raw value. |
| `Instrument(instrument_id, product, currency, initial_face, coupon_leg, principal_leg, calendar_id, offers=(), settlement_lag_days=1, isin=None, issuer_id=None)` | `.cashflows(after=, horizon=, offer=)`, `.outstanding_face(on)`, `.horizon(policy, settlement)`, `.accrued(settlement)`, `.maturity_date`, `.is_perpetual`. |
| `ProductType`, `FixedCouponLeg`, `CouponPeriod`, `FloatingCouponLeg`, `PrincipalLeg`, `Redemption`, `Offer`, `OfferKind` | Instrument terms. |
| `RateAccrualCouponLeg(periods, accrual_basis)`, `RateCouponPeriod(accrual_start, accrual_end, payment_date, amount, rate)`, `AccrualBasis` **(0.17.0)** | Known coupons whose accrued interest comes from the rate: `RATE_ACT_365` (MOEX corporates), `RATE_ACT_360`, `RATE_30_360`. `AccrualBasis.days(start, end) -> (days, year)`. Same cash flows as `FixedCouponLeg`; see `CONVENTIONS.md`. |
| `MarketSnapshot(snapshot_id, business_date, known_at, curves, calendars, fixings, fx, quotes, evidence)` | `.curve(id)`, `.calendar(id)`, `.fixing(id)`, `.fx_rate(ccy)`, `.quote(instrument_id)`; missing keys raise `MissingMarketData`. |
| `CurveEntry`, `CalendarEntry`, `FixingEntry`, `FxRate`, `BondPriceObservation`, `SnapshotEvidence` | Snapshot entries. |
| `Position(position_id, instrument_id, quantity, book, accounting_category, currency, recovery_rate=None)` | `AccountingCategory`: `TRADING`, `HTM`, `REPO`. |

## KBD, curve specs, factors and portfolio valuation (0.11.0)

| Name | Description |
| --- | --- |
| `ZcycParameters(trade_date, b1, b2, b3, t1, g)` | One MOEX KBD publication (`b*`, `g` in bp, `t1` in years). |
| `kbd_zero_rate_bp(params, t)`, `kbd_effective_yield(params, t)` | Continuous zero rate (bp) and effective annual yield (decimal) at `t` years. |
| `kbd_discount_curve(params, curve_date=None, *, horizon_years=30)` | Materialised `DiscountCurve` (ACT/365F), within 0.005 bp of the formula. |
| `g_spread_bp(params, bond_effective_yield, tenor_years)` | Bond yield minus KBD yield at an explicit tenor (MOEX: duration), in bp. |
| `CurveSpec`, `CurveKind`, `CURVE_SPECS`, `get_curve_spec(spec_id)` | Versioned registry; unknown IDs raise `UnknownCurveSpec`. (0.22.0) `rub_ruonia_ois_moex_v2` registered; `CurveSpec` gains `float_leg_day_count`, `stub`, `calendars`, `sources` (default empty). |
| `ois_curve_convention(spec_id, calendar=None) -> CurveConvention` (0.22.0) | The convention of an OIS bootstrap spec: `CurveConvention()` for `rub_ruonia_ois_legacy_v1` (no calendar), the MOEX OIS RUONIA `ois_swap` on the caller's calendar for `rub_ruonia_ois_moex_v2`. `value_portfolio` rejects (`INVALID_TERMS`) an OIS curve whose convention does not match its spec. |
| `ir_factor(curve_id, bucket)`, `CS_FACTOR`, `fx_factor(ccy)`, `fx_basis_factor(ccy)` | Desk factor keys. |
| `FactorRisk(factors, first_order, hessian, difference, bump_bp, has_second_order)` | `HessianEntry(i, j, value)` upper triangle; `.first_order_by_factor()`, `.hessian_by_factor()`. |
| `bond_factor_risk(risk, curve_id, second_order=None)`, `fx_futures_factor_risk(risk, rub_curve_id, foreign_curve_id, second_order=None)` | Convert engine results. |
| `ValuationPolicy(discount_curve_id, spread_kind, spread_convention, horizon, curve_overrides, include_second_order, bump_bp)` | Valuation choices; part of the job identity. |
| `value_portfolio(*, run_id, as_of, snapshot, instruments, positions, policy, scenarios=(), time_budget_seconds=None, should_cancel=None, curve_cache=None)` | `PortfolioRiskResult` with `PositionValuation`s, `PortfolioCoverage` (coded `Rejection`s), `PortfolioScenarioResult`s, `CurveUse`, `CalendarUse`, `FactorTotal`s. |
| `portfolio_result_to_json`, `portfolio_result_from_json`, `verify_portfolio_result` | Contract `rub-portfolio-risk-result.v2`. |

## Caller-held curves (0.13.0)

| Name | Description |
| --- | --- |
| `PrebuiltCurve(request, shifts, curve)` | A bootstrapped OIS curve for a `CurveBootstrapRequest` with 11 bucket shifts (bp). `.request_id` and `.curve_key` are canonical SHA-256 strings for the backend's store; `to_dict`/`from_dict` serialise it. |
| `ShockedCurveCache.export()` | Every cached base and shocked curve as `PrebuiltCurve`s, ordered by `curve_key`. |
| `ShockedCurveCache.seed(entries)` | Adds caller-held curves; each must reprice its (shocked) quotes within 1e-7 bp and match the request's date, convention and model, otherwise `InvalidTerms` and nothing is added. Returns the number added; `.seeded` counts them. |
| `value_portfolio(..., curve_cache=cache)` | Reuses `cache` (seeded or kept in memory across calls). Results are byte-identical with or without it. |

`benchmarks/portfolio_bench.py --positions 2000` measures ms per position (KBD and OIS;
first order, Hessian, scenarios, seeded). CI runs it on windows-latest / Python 3.11 and
writes the table to the job summary (informational, never gating).

## Horizon valuation (0.14.0)

| Name | Description |
| --- | --- |
| `horizon_valuation(instrument, curve, calendar, *, horizon_date, spread_bp=None, dirty_price_rub=None, spread_convention=None, redemption_horizon="maturity", curve_cache=None)` | `HorizonValuation` per bond: `pv_start_rub`, `pv_forward_rub` (today's forwards realised), `pv_rolled_rub` (curve shape held), coupons and principal received in `(S0, S1]`, accrued and nominal at both settlements, `carry_rub`, `rolldown_rub`, `horizon_pnl_rub`. Exactly one of `spread_bp` / `dirty_price_rub`. Fixed-coupon bonds; others raise `UnsupportedProduct`. |
| `explain_horizon_pnl(instrument, start_curve, end_curve, calendar, *, start_spread_bp \| start_dirty_price_rub, end_spread_bp \| end_dirty_price_rub, ...)` | `HorizonPnlExplain`: `carry + rolldown + rates + spread + residual == total` (full revaluation change to the end curve's date). |
| `roll_curve(curve, horizon_date)` | The curve moved forward with its shape in calendar days unchanged. |
| `horizon_portfolio(*, snapshot, instruments, positions, policy, horizon_date, curve_cache=None)` | `PortfolioHorizonResult`: `PositionHorizon` rows (signed RUB, per-bond detail), coverage with the same rejection codes as `value_portfolio`, covered carry / rolldown / horizon P&L and a total when coverage is complete. |

## Tenors (0.16.0)

| Name | Description |
| --- | --- |
| `parse_tenor(code) -> Tenor` | `ON`, `TN`, `SN`, `<n>D/W/M/Y` (case-insensitive); anything else raises `InvalidTerms`. |
| `add_tenor(start, tenor)` | Unadjusted end of a `D/W/M/Y` tenor (month ends clamp). |
| `resolve_tenor(trade_date, tenor, calendar, *, spot_lag_days=1, business_day_convention=MODIFIED_FOLLOWING) -> TenorDates` | `effective_date`, `unadjusted_end`, `end_date`. A bounded calendar raises `CalendarRangeError` outside its range. |
| `ois_quotes(trade_date, [(tenor, rate), ...], calendar, *, spot_lag_days=1, business_day_convention=...)` | `OisQuote`s with resolved maturities, sorted; duplicate tenors or maturities raise. |

## Fixed-RUB risk extensions (0.5.0)

See [second-order and local API](second-order-local-risk.md) for `BondRiskEngine.second_order`, `approximate`, `jump_to_default`, and `prepare_local_bond_risk`.

## Non-RUB bonds and FX delta (package 3)

| Name | Description |
| --- | --- |
| `ValuationPolicy(..., foreign_curves=(("USD", "USD_SOFR"), ...))` | Discount curve ID per non-RUB currency; no fallback. `discount_curve_for(instrument)` raises `MissingMarketData` for an unmapped currency. |
| `SUPPORTED_CURRENCIES` | `RUB`, `CNY`, `USD`, `EUR`. Other bond currencies are `UNSUPPORTED_PRODUCT`. |
| `cny_discount_factors_v1`, `usd_discount_factors_v1`, `eur_discount_factors_v1`, `CurveKind.DISCOUNT_FACTORS` | Caller-built foreign `DiscountCurve`s (log-linear DF nodes, ACT/365F, dated the business date). |
| `PositionValuation.currency_amounts: CurrencyAmounts \| None` | `currency`, `rub_per_unit`, `current_nominal`, `accrued_interest`, `dirty_price` (per bond) and signed `pv`, in the bond currency. The `_rub` fields are these at the snapshot rate. `None` for RUB bonds. |
| `PositionValuation.yield_convention` | Identifier of the issue's yield basis (`Instrument.yield_convention`), on which `effective_yield` and `macaulay_duration_years` are quoted. |
| `PortfolioRiskResult.fx_rates: tuple[FxRate, ...]` | FX rates used, by currency. |
| `PortfolioScenario(name, shock, fx_shocks_pct=(("USD", 10.0), ...))` | Exact revaluation with FX moved by a percent against RUB. |
| `fx_translated_factor_risk(risk, currency, fx_delta_rub_per_pct)` | Adds `FX|<ccy>` and its cross terms (`first_order_i / 100`) to RUB factor risk. |
| `Instrument(..., yield_convention=YieldConvention(day_count=DayCount.ACT_360))`, `YieldRequest(..., convention)`, `DEFAULT_YIELD_CONVENTION` | Yield time basis from the issue terms (BACKLOG #38); default effective annual ACT/365F. |

Rejections: an unmapped currency, a curve or FX rate missing from the snapshot are
`MISSING_MARKET_DATA`; a curve of another currency, a foreign curve not dated the
business date or not a node `DiscountCurve` are `INVALID_TERMS`; a non-annual yield
quote and non-RUB bonds in `horizon_portfolio` are `UNSUPPORTED_PRODUCT`. All new fields
are omitted from the canonical JSON while at their defaults (`canonical.OMIT_IF_DEFAULT`),
so RUB-only results, policies, scenarios and instruments keep their bytes and hashes.
See `CONVENTIONS.md` (Non-RUB bonds and FX) and `risk.md`.

## Inflation-linked bonds (package 9a)

Details, evidence and gaps: [linkers.md](linkers.md).

| Name | Description |
| --- | --- |
| `CpiObservation(month, value, published_on)`, `CpiSeries(series_id, source_id, as_of, observations)` | Point-in-time monthly CPI levels; `.level(month)` (`MissingMarketData` when unpublished), `.known_on(date)`, `.first_month`, `.last_month`, `.content_sha256`. |
| `CpiIndexation(series_id, base_date, lag_months, base_index=None, interpolation="daily_linear", nominal_quantum=Decimal("0.01"))` | OFZ-IN rule: `INDEX = CPI(M-lag-1) + (CPI(M-lag) - CPI(M-lag-1)) * (n-1)/d`, face `= initial * INDEX / INDEX_base`. `.reference_months`, `.reference_index`, `.index_ratio`, `.nominal`, `.identifier`. |
| `IndexedCouponPeriod(accrual_start, accrual_end, payment_date, rate)`, `IndexedCouponLeg(periods, day_count)`, `IndexedPrincipalLeg(maturity_date, indexation, floor_at_initial_face)` | New `Instrument` legs, for `ProductType.INFLATION_LINKED_BOND` only. `Instrument.is_indexed`; `cashflows()` / `accrued()` / `outstanding_face()` raise `InvalidTerms` for these. |
| `indexed_coupon_amount(nominal, period, day_count)`, `indexed_accrued(nominal, period, date, day_count)` | Formulas (4) and (5), rounded half-up to 0.01. |
| `linker_nominal`, `linker_accrued`, `linker_coupon`, `linker_redemption`, `real_cashflows` | Face, accrued, coupons and real flows (at the settlement-date face) of an indexed instrument from a `CpiSeries`. |
| `MarketSnapshot(..., cpi=())`, `.cpi_series(id)` | CPI series in the snapshot; omitted from the hash when empty. |
| `real_factor(curve_id, bucket)`, `linker_factor_risk(risk, real_curve_id, second_order=None)` | New factor type `REAL\|<curve_id>\|<bucket>`. |
| `CpiUse`, `PortfolioRiskResult.cpi` | CPI provenance of a valuation (omitted when empty). |
| `INFLATION_MODEL_VERSION` | `rub-cpi-indexation-v1`. |

`value_portfolio` values a linker on the real curve (`rub_real_discount_factors_v1`) that
`curve_overrides` names for it; nominal scenarios report linker rows as
`UNSUPPORTED_PRODUCT`. `benchmarks/linker_bench.py` times 2k linkers.

## Callables, perpetuals and pass-throughs (package 9b)

Details and formulas: [callables.md](callables.md).

| Name | Description |
| --- | --- |
| `ExerciseAssumption(kind, call_date=None, assumed_coupons=())`; `.to_maturity()`, `.to_call(date, assumed_coupons=())`, `.to_worst(assumed_coupons=())`, `.identifier` | Explicit exercise assumption; `assumed_coupons` are reset coupons after the known periods. `"option_adjusted"` raises `UnsupportedProduct`. |
| `call_schedule(instrument)` | The `OfferKind.CALL` offers, by date (`price_pct` of the face outstanding). |
| `redemption_scenarios(instrument, settlement, *, assumed_coupons=())` | `RedemptionScenario` per call after settlement and maturity, with their `Cashflow`s; raises `InvalidTerms` when coupons to a date are unknown, have a gap or the date is mid-period. |
| `call_analysis(instrument, settlement, dirty_price_rub, *, assumed_coupons=())` | `CallAnalysis`: `ScenarioYield` per scenario (effective yield on the instrument's `yield_convention`, Macaulay and modified duration, `yield_convention` identifier), `yield_to_maturity`, `worst`. |
| `yield_to_call(..., call_date)`, `yield_to_maturity(...)`, `yield_to_worst(...)` | One `ScenarioYield`. Perpetuals: no maturity, no worst (`UnsupportedProduct`). |
| `price_to_worst(instrument, settlement, effective_yield, *, assumed_coupons=())` | `PriceToWorst`: lowest dirty price over the scenarios at the yield, accrued, clean %, the date. Inverse of `yield_to_worst`. |
| `extension_risk(instrument, settlement, dirty_price_rub, call_date, ...)` | `ExtensionRisk`: yields and durations to the call and to maturity, `duration_extension_years`, `pv_change_if_extended_rub`. |
| `resolve_exercise(instrument, settlement, assumption, *, dirty_price_rub=None)` | `ResolvedExercise` (exercise, date, price, `identifier`); `to_worst` needs the price. |
| `exercised_instrument(instrument, resolved)` | `ExercisedBond(instrument, resolved, source_sha256)`: the deterministic fixed bond; par calls only (`UnsupportedProduct` otherwise). |
| `perpetual_current_yield(coupon_rate, clean_price_pct)` | MOEX's published perpetual yield (coupon over clean price). |
| `option_adjusted_spread(...)` | Always raises `UnsupportedProduct` (no short-rate model). |
| `PassThroughTerms(instrument_id, currency, original_face, current_face, coupon_rate, accrual_basis, periods, payments_per_year, calendar_id, settlement_lag_days=1, cleanup_call_fraction=None, isin=None, issuer_id=None)`, `PassThroughPeriod(accrual_start, accrual_end, payment_date, scheduled_principal=0.0)` | Pool terms from the current state; periods contiguous; the last pays the remainder. |
| `PrepaymentAssumption(cpr, exercise_cleanup_call=False)` | Constant annual CPR in `[0, 1)`; `.smm(payments_per_year)`, `.identifier`. |
| `project_pass_through(terms, assumption)` | `PassThroughProjection`: `rows` (`PassThroughRow`: balances, coupon, scheduled, prepaid, clean-up principal), `cashflows`, `smm`. |
| `weighted_average_life(cashflows, settlement)` | Principal-weighted ACT/365F years. |
| `pass_through_analytics(terms, assumption, settlement, *, dirty_price_rub)` | `PassThroughAnalytics`: prepayment-adjusted effective yield, WAL, durations. |
| `cpr_sensitivity(terms, assumption, settlement, curve, *, spread_bp \| dirty_price_rub, spread_convention=None, cpr_bump=0.01, curve_cache=None)` | `CprSensitivity`: PV and WAL at `CPR`, `CPR ± bump`, `dpv_per_cpr_pct_rub`, `difference` (central / forward). |
| `pass_through_instrument(terms, assumption)` | The projected flows as a fixed bond (`initial_face` = current face, rate accrual). |
| `AssumptionPolicy(default_exercise=None, overrides=())`, `InstrumentAssumption(instrument_id, exercise=None, prepayment=None)` | Per-instrument assumptions; `default_exercise` for callables without an override. |
| `prepare_assumed_book(*, snapshot, instruments, policy, pass_throughs=())` | `AssumedBook(instruments, uses)`: exercised / projected bonds and the `AssumptionUse` records. |
| `value_assumed_portfolio(*, run_id, as_of, snapshot, instruments, positions, policy, assumptions, pass_throughs=(), scenarios=(), ...)` | `AssumedPortfolioResult`: the unchanged v2 `portfolio`, `assumptions`, merged `coverage`, `status`, totals, `result_sha256`; `verify_assumed_result`. |
| `horizon_assumed_portfolio(*, snapshot, instruments, positions, policy, assumptions, horizon_date, pass_throughs=(), curve_cache=None)` | `AssumedHorizonResult`: `horizon_portfolio` over the assumed book with merged coverage. |
| `AssumptionUse` | `instrument_id`, `kind` (callable / perpetual / pass_through), `assumption` identifier, `status`, `code`, `message`, `settlement_date`, `exercise`, `redemption_date`, `wal_years`, `source_sha256`, `valued_sha256`. |
| `MissingAssumption` (`MISSING_ASSUMPTION`) | A pass-through without a CPR. |
| `CALL_MODEL_VERSION`, `PREPAYMENT_MODEL_VERSION`, `ASSUMED_BOOK_MODEL_VERSION` | `rub-call-exercise-v1`, `rub-constant-cpr-v1`, `rub-assumed-book-v1`. |

`value_portfolio` itself is unchanged and still rejects callables and perpetuals.
