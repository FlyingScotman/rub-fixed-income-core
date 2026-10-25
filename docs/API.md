# API reference (0.33.0)

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
- `bootstrap_discount_curve(request) -> DiscountCurve` — raises for empty / non-increasing / non-finite quotes, unbracketed forward; `RuntimeError` if repricing > 1e-7 bp. With `convention.ois_swap` (0.22.0) each quote must be a `D/W/M/Y` tenor whose resolved end is its maturity (`InvalidTerms` otherwise). Since FlyingScotman/rub-fixed-income-core#77 the root search reads its trial discount factors without building a curve per trial: the same bisection on bit-identical objective values, so the nodes and every hash are unchanged, about 10x faster for 17 MOEX RUONIA swaps.
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

Package 1 (floaters) adds, all optional fields omitted from the canonical form at
their default:

| Name | Description |
| --- | --- |
| `CouponRateBounds(applies_to, floor=None, cap=None)` | Per-period floor/cap on `"index_rate"` (before multiplier and margin) or `"coupon_rate"`. `FloatingCouponRule(bounds=...)`. Priced at intrinsic value only outside `FloatingLeg(strike_guard_bp=...)` of every strike, else `UnsupportedProduct`. `FloatingCouponProjection.bound`, `FloatingLeg.has_optionality`. |
| `ObservationShift(days, unit, accrues_over)` | Shift the observation period by business or calendar days; `accrues_over="observation"` (MOEX OFZ-PK) or `"accrual"` (ISDA). `FloatingCouponRule(observation_shift=...)`. |
| `ObservationWindow(end_offset_days, end_unit, length_months=None)` | In-advance window ending before the accrual start; `.bounds(calendar, start, end)`. `FloatingCouponRule(observation_window=...)`. |
| `FixingDateRule(anchor, offset_days, unit)` | Single fixing in advance or in arrears, with `averaging="single_fixing"`. |
| `FloatingCouponRule(multiplier=1.0, observation_days="business")` | Index multiplier; `"calendar"` observes each calendar day's key rate in force. |
| `FixingHistory(published_at=())`, `.published_by(cut)`, `.known_at` | Per-row publication times; drop rows published after a cut; parsed `known_at_utc`. |
| `MissingFixing` | `MissingMarketData` (code `MISSING_MARKET_DATA`) and `LookupError`: a required fixing is absent. |
| `key_rate_projection_curve(forward, calendar, *, rate_day_count, observation_days)`, `KEY_RATE_PROJECTION_MODEL_VERSION` | `DiscountCurve` whose overnight forwards are a `KeyRateForwardCurve` path; spec `rub_key_rate_projection_v1`. |
| `BondRiskRequest(projection_curve=...)`, `TwoCurveBondRiskResult`, `FLOATING_TWO_CURVE_RISK_MODEL_VERSION` | Separate projection curve: `risk()` adds `projection_buckets`; `second_order()` is 23 x 23; `reprice(scenario, projection_curve_shifts_bp=None)` and `approximate(...)` take projection shifts (None: the scenario's curve shifts). |
| `bond_factor_risk(risk, curve_id, second_order=None, *, projection_curve_id=None)` | Two-curve risk maps to `IR|<curve_id>|*`, `IR|<projection_curve_id>|*`, `CS`. |

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
| `FxForwardLeg(..., fixing_date=None, fixing_source=None, fixing_rate=None)`, `.is_fixed` | **0.36.0**, NDF fixing of a cash-settled leg (BACKLOG #49), omitted at None. A fixed leg is frozen at `N/q · (fixing − K) · DF_rub(P)`: RUB risk only; outstanding until its payment date. An unfixed leg past its fixing date raises `InvalidTerms`. `FxForwardLegValuation.fixing_date` / `.fixing_rate` (omitted at None). |
| `NdfConvention(fixing_source, fixing_lag_days, value_lag_days)`, `.from_payment(payment_date, calendar)`, `.from_fixing(fixing_date, calendar)` -> `NdfDates(fixing_date, value_date, payment_date)`, `.leg(dates, notional, contract_rate, *, fixing_rate=None)` | **0.36.0**. The confirmation's NDF date rule in settlement business days; no source or lag is built in. |
| `FxForwardRiskEngine.points_held_rub_risk(*, bump_bp=1, difference="central") -> FxForwardPointsHeldRisk` | **0.36.0** (BACKLOG #46). RUB buckets and parallel with every outright held (`DF_for' = DF_for · DF_rub' / DF_rub`). |
| `FxImpliedCurveRequest(currency, curve_date, rub_curve_id, quotes, convention=None)`, `.build(rub_curve, spot_rate, spot_value_date)` | **0.36.0**. Swap points / outrights (RUB per one unit) of a `<ccy>_fx_implied_v1` curve entry; `value_portfolio` builds it on the snapshot `FxSpot` and the named RUB curve. |
| `FxCrossForward(trade_id, base_currency, quote_currency, quote_units, leg)`, `FxCrossSwap(..., near, far)`, `fx_cross_rub_legs(trade, *, pivot_rate=1.0)` | **0.36.0**, module `fx_crosses`. Crosses without RUB (USD/CNY); the exact X/RUB + Y/RUB decomposition. |
| `FxCrossRiskRequest(instrument, base_spot_rate, base_spot_value_date, quote_spot_rate, quote_spot_value_date, rub_curve, base_curve, quote_curve, base_basis_bp, quote_basis_bp, base_basis_day_count=ACT_365F, quote_basis_day_count=ACT_365F)`, `FxCrossRiskEngine(request, curve_cache=...)` | **0.36.0**. `.risk() -> FxCrossRiskResult` (RUB / base / quote buckets, both bases, `fx_delta_base`, `fx_delta_quote`), `.reprice(FxCrossScenario)`, `.second_order()` (37 factors), `.points_held_rub_risk()`, `.horizon(h, *, base_horizon_spot_value_date, quote_horizon_spot_value_date, fx_horizon)`; `fx_cross_factor_risk(risk, rub_id, base_id, quote_id, second_order=None)`; `FX_CROSS_*_MODEL_VERSION`. |
| `fx_value_date(trade_date, code, calendar, *, spot_lag_days=None, business_day_convention=MODIFIED_FOLLOWING)`, `fx_swap_value_dates(trade_date, near, far, calendar, ...)`, `FX_VALUE_DATE_CODES` | `TOD` (settlement days only), `TOM`, `SPT`, or a tenor from spot (`spot_lag_days` required). |

## Interest-rate swaps (package 2)

Seasoned RUONIA OIS and key-rate swaps; see [swaps](swaps.md).

| Name | Description |
| --- | --- |
| `InterestRateSwap(swap_id, side, notional, fixed_rate, effective_date, termination_date, convention, float_spread=0.0, currency="RUB")` | Terms. `side` is `payer` (pays fixed) or `receiver`; `termination_date` is unadjusted; non-RUB raises `UnsupportedProduct`. `.from_tenor(*, swap_id, side, notional, fixed_rate, trade_date, tenor, calendar, convention, float_spread=0.0)` resolves spot start and end with `resolve_tenor`. |
| `SwapConvention(index, averaging, fixed_leg, float_leg, rate_day_count, business_day_convention, spot_lag_days, payment_lag_days, spec_id=None)`, `SwapLegSchedule(frequency_months, roll, day_count, single_period_up_to_months=None)` | Every convention explicit. `index` `ruonia` (compounded) or `key_rate` (arithmetic); `roll` `forward`, `backward` or `legacy-annual-forward`. `.identifier`. |
| `swap_convention(spec_id)` | Convention of `rub_key_rate_swap_v1` or `rub_ruonia_ois_legacy_v1`; other specs raise `UnknownCurveSpec` / `UnsupportedProduct`. |
| `swap_schedule(swap, calendar) -> SwapSchedule(fixed, floating)` | `SwapPeriod(accrual_start, accrual_end, payment_date, accrual_factor)` per leg. |
| `SwapRiskRequest(swap, calendar, discount_curve, projection_curve=None, fixings=None)` | Valuation date = discount curve date. Discount: `DiscountCurve` or `CurveBootstrapRequest`; projection: `None` (single curve), `DiscountCurve` / `CurveBootstrapRequest` (RUONIA) or `KeyRateForwardCurve` / `KeyRateCurveRequest` (key rate: forward or KRS-quote risk); `fixings` a `FixingHistory` of the index, required once a period has started. |
| `SwapRiskEngine(request, curve_cache=ShockedCurveCache())` | `.valuation() -> SwapValuation` (PV, legs, accrued, clean PV, par rate, annuity, `SwapCashflow`s), `.risk(bump_bp=1, difference="central") -> SwapRiskResult` (`SwapCurveRisk` per curve role with `BucketRisk`s, KRD per notional), `.reprice(SwapScenario) -> SwapScenarioResult`, `.second_order(bump_bp=1) -> SwapSecondOrderRisk` (11 or 22 factors), `.horizon(date) -> SwapHorizonValuation`. |
| `SwapScenario(discount_shifts_bp, projection_shifts_bp)`, `.parallel(discount_bp=0, projection_bp=0)` | Absolute bp bucket shifts per curve. |
| `swap_factor_risk(risk, *, discount_curve_id, projection_curve_id=None, second_order=None) -> FactorRisk` | Desk factors `IR\|<curve_id>\|<bucket>`; one ID for a single curve, two distinct IDs otherwise; central only. |
| `swap_horizon(request, *, horizon_date, curve_cache=None) -> SwapHorizonValuation` | Carry, rolldown, received flows and horizon P&L. |
| `SWAP_MODEL_VERSION`, `SWAP_SECOND_ORDER_MODEL_VERSION`, `SWAP_HORIZON_MODEL_VERSION` | Model versions recorded in results. |

`benchmarks/swap_bench.py --positions 2000` times 1000 OIS and 1000 KRS positions.

## Rate futures (package 5)

MOEX RUON, RUONIA index, KEYRATE and OFZ basket futures; see [rate futures](rate-futures.md).
Money is signed RUB of daily variation margin (not discounted).

| Name | Description |
| --- | --- |
| `RuoniaAverageFuturesContract(contract_id, period_start, period_end, notional_rub=1e6, price_step_pct=0.01, final_price_decimals=2)` | One-month RUONIA futures; `.step_value_rub` (`W`), `.margin_per_point_rub` (`Round(W/R; 5)`), `.last_trading_day`. `ruonia_average_settlement_period(year, month, calendar)` gives the period. |
| `RuoniaIndexFuturesContract(contract_id, last_trading_day)`, `KeyRateFuturesContract(contract_id, meeting_date, effective_date)` | RUONIA index futures (10,000 RUB per point) and KEYRATE futures (1,000 RUB per point). |
| `RuoniaFixings(valuation_date, fixings, calendar, snapshot_id, unpublished_dates=frozenset())`, `RuoniaIndexValue(index_date, value)` | Point-in-time RUONIA (decimal); undeclared gaps raise `MissingFixing`, declared ones carry the previous fixing. |
| `ruonia_average_final_price(contract, fixings)`, `ruonia_index_value(anchor, fixings, target)`, `ruonia_index_final_price(contract, anchor, fixings)`, `key_rate_index_value(day, key_rate_history, scheduled_meetings, calendar)`, `cbr_ruonia_year_fraction(start, end)` | Final settlement and index reproduction per the specifications and methodologies. |
| `rate_futures_variation_margin(contract, previous_price, price, quantity=1)` | Variation margin per the specification (RUON rounds each leg to kopecks). |
| `RateFuturesRiskRequest(contract, projection_curve, fixings=None, index_anchor=None, futures_price=None, basis_bp=None, quantity=1)` | RUONIA contracts: `DiscountCurve` / `CurveBootstrapRequest` + fixings (+ anchor); KEYRATE: `KeyRateForwardCurve` or `KeyRateCurveRequest` (KRS-quote risk). Exactly one of `futures_price` / `basis_bp`. |
| `RateFuturesRiskEngine(request, curve_cache=ShockedCurveCache())` | `.base_price`, `.theoretical_price`, `.basis_bp`, `.risk(bump_bp=1, difference=...) -> RateFuturesRiskResult`, `.reprice(RateFuturesScenario) -> RateFuturesScenarioResult`, `.second_order() -> RateFuturesSecondOrderRisk` (12 factors), `.for_quantity(q)`. |
| `RateFuturesScenario(curve_shifts_bp, basis_shift_bp)`, `.parallel(shift_bp, basis_shift_bp)` | Exact scenarios. |
| `OfzFuturesContract(contract_id, last_trading_day, delivery_date, bonds_per_lot=10)` | `.delivery_price_pct(F, CF, face)` = `F / N * CF / face * 100`. |
| `DeliverableBond(instrument, conversion_factor, clean_price_pct)`, `ofz_conversion_factor(instrument, delivery_date, conversion_yield, *, day_count=ACT_365_FIXED, decimals=4)`, `ofz_default_delivery_issue(basket)` | Basket bond with the exchange CF; the Appendix 4 CF formula; the default delivery issue rule. |
| `OfzFuturesRiskRequest(contract, basket, spot_settlement_date, bond_curve, funding_curve, futures_price=None, basis_bp=None, quantity=1)`, `OfzFuturesRiskEngine(request, curve_cache=...)` | `.basket() -> DeliverableAnalysis` rows (forward, theoretical price, implied repo, gross / net basis, cheapest), `.risk() -> OfzFuturesRiskResult` (bond and funding buckets, CS01, basis01, cheapest bond), `.reprice(OfzFuturesScenario)`, `.second_order()` (24 factors), `.for_quantity(q)`. |
| `rate_futures_factor_risk(risk, curve_id, second_order=None)`, `ofz_futures_factor_risk(risk, bond_curve_id, funding_curve_id, second_order=None)`, `futures_basis_factor(contract_id)` | Desk factors `IR\|<curve>\|<bucket>`, `CS`, `FUTBASIS\|<contract_id>`. |
| `RATE_FUTURES_RISK_MODEL_VERSION`, `RATE_FUTURES_SECOND_ORDER_MODEL_VERSION`, `OFZ_FUTURES_RISK_MODEL_VERSION`, `OFZ_FUTURES_SECOND_ORDER_MODEL_VERSION` | Model versions recorded in results. |

`benchmarks/rate_futures_bench.py --positions 2000` times a book of 12 contracts.

## Repo and reverse-repo legs (package 6)

Module `rub_fixed_income.repo`; see [repo](repo.md). Instrument valuation only: money
is signed RUB of PV of the remaining cash legs (+ for the cash lender); the collateral
is a reference, never in the PV. Desk funding economics stay in the backend.

| Name | Description |
| --- | --- |
| `RepoTrade(trade_id, side, start_date, end_date, start_amount_rub, rate, amount_rounding_decimals, collateral=None, cash_adjustments=(), currency="RUB")` | `side` `reverse_repo` (lends cash) or `repo`; `end_date=None` is an open repo. `.sign`, `.is_open`. Non-RUB raises `UnsupportedProduct`. |
| `FixedRepoRate(rate, day_basis, interest="simple")` | Decimal rate; `interest` `simple` (MOEX Part III 1.5.8.5) or `capitalised` (credited on settlement days). |
| `FloatingRepoRate(benchmark, spread, day_basis, observation, projection_day_count, interest="simple", nonpositive_rate_replacement=None)` | `RUSFAR` / `RUONIA` with `previous_settlement_day`, `KEY_RATE` with `calendar_day`; other pairs, capitalised key rate raise `UnsupportedProduct`. The replacement is the MOEX KSU 0.01 % rule. |
| `RepoDayBasis` (`ACT_365_366`, `ACT_365_FIXED`, `ACT_360`) | `.day_weight(day)`, `.year_fraction(start, end)` over the days of `(start, end]`; `ACT/365-366` = `T365/365 + T366/366`. |
| `RepoCollateral(instrument_id, quantity, initial_discount_pct=None, pool=None)`, `RepoCashAdjustment(payment_date, amount_rub, kind)` | Collateral reference; explicit cash flows signed for the lender (`margin_call`, `income_compensation`, `repurchase_adjustment`). |
| `RepoRiskRequest(trade, calendar, discount_curve, projection_curve=None, fixings=None, assumed_end_date=None, collateral_dirty_price_rub_per_bond=None)` | Valuation date = discount curve date; `calendar` = settlement days; RUSFAR and RUONIA fixings in a `FixingHistory(index="ruonia")` on the benchmark's publication calendar, the key rate in `index="key_rate"`; `assumed_end_date` required for, and only for, an open repo. |
| `RepoRiskEngine(request, curve_cache=ShockedCurveCache())` | `.valuation() -> RepoValuation` (PV and legs, `S1`, `S2` and its source, known and accrued interest, current repurchase amount, repurchase price per bond, `RepoCashflow`s, `RepoCollateralReference`), `.risk(bump_bp=1, difference="central") -> RepoRiskResult` (`RepoCurveRisk` per role), `.reprice(RepoScenario) -> RepoScenarioResult`, `.second_order(bump_bp=1) -> RepoSecondOrderRisk` (11 or 22 factors), `.carry(horizon_date) -> RepoCarry`. |
| `RepoScenario(discount_shifts_bp, projection_shifts_bp)`, `.parallel(discount_bp=0, projection_bp=0)` | Absolute bp bucket shifts; a fixed or single-curve repo rejects projection shifts. |
| `repo_factor_risk(risk, *, discount_curve_id, projection_curve_id=None, second_order=None) -> FactorRisk` | `IR\|<discount>\|<bucket>` (and `IR\|<projection>\|<bucket>`); central only. |
| `repo_carry(request, *, horizon_date, curve_cache=None) -> RepoCarry` | Interest carry over `(valuation, horizon]`, received flows, forward PV and PV carry. |
| `repo_start_amount(quantity, dirty_price_rub_per_bond, discount_pct, *, decimals=2)`, `repo_initial_discount_pct(start_amount_rub, quantity, dirty_price_rub_per_bond)`, `repo_end_amount(start_amount_rub, rate, start_date, end_date, day_basis, *, decimals)` | MOEX Part III 1.5.8.2 and 1.5.8.5. |
| `REPO_MODEL_VERSION`, `REPO_SECOND_ORDER_MODEL_VERSION` | `rub-repo-leg-v1`, `rub-repo-leg-second-order-v1`. |

`benchmarks/repo_bench.py --positions 2000` times 1000 fixed and 1000 RUSFAR repos.

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
| `ProductType`, `FixedCouponLeg`, `CouponPeriod`, `FloatingCouponLeg`, `PrincipalLeg`, `Redemption`, `Offer`, `OfferKind` | Instrument terms. `FloatingCouponLeg(rule, periods, fixing_id, projection_curve_id=None)`: the snapshot curve that projects (package 1). |
| `Instrument.cashflows(..., market=None, strike_guard_bp=None)`, `Instrument.floating_leg(market, *, horizon=None, strike_guard_bp=None)` (package 1) | With `market`, floating coupons are projected off the leg's projection curve with point-in-time fixings; without it a floating leg yields principal only. |
| `MarketSnapshot.point_in_time_fixing(fixing_id)`, `MarketSnapshot.projection_curve(curve_id, index)` (package 1) | Fixings as known at `known_at` (history dated the business date; `known_at_utc` not after the cut; rows published after it dropped; neither given: `InvalidTerms`). A projection curve checked against its spec (RUONIA OIS for RUONIA, `rub_key_rate_projection_v1` for the key rate). |
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
| `ValuationPolicy(discount_curve_id, spread_kind, spread_convention, horizon, curve_overrides, include_second_order, bump_bp)` | Valuation choices; part of the job identity. Package 1: `cap_floor_guard_bp=None`, the floater floor/cap guard. |
| `PortfolioScenario(name, shock, ..., projection_curve_shifts_bp=None)` (package 1) | Shifts of two-curve floaters' projection curves; None moves them with `shock.curve_shifts_bp`. |
| `SUPPORTED_PRODUCTS` | `FIXED_BOND` and, with package 1, `FLOATING_BOND` (RUB; single- or two-curve; quote must carry accrued). |
| `value_portfolio(*, run_id, as_of, snapshot, instruments, positions, policy, scenarios=(), time_budget_seconds=None, should_cancel=None, curve_cache=None)` | `PortfolioRiskResult` with `PositionValuation`s, `PortfolioCoverage` (coded `Rejection`s), `PortfolioScenarioResult`s, `CurveUse`, `CalendarUse`, `FactorTotal`s. |
| `portfolio_result_to_json`, `portfolio_result_from_json`, `verify_portfolio_result` | Contract `rub-portfolio-risk-result.v2`. |

## Caller-held curves (0.13.0)

| Name | Description |
| --- | --- |
| `PrebuiltCurve(request, shifts, curve)` | A bootstrapped OIS curve for a `CurveBootstrapRequest` with 11 bucket shifts (bp). `.request_id` and `.curve_key` are canonical SHA-256 strings for the backend's store; `to_dict`/`from_dict` serialise it. |
| `ShockedCurveCache.export()` | Every cached base and shocked curve as `PrebuiltCurve`s, ordered by `curve_key`. |
| `ShockedCurveCache.seed(entries)` | Adds caller-held curves; each must reprice its (shocked) quotes within 1e-7 bp and match the request's date, convention and model, otherwise `InvalidTerms` and nothing is added. Returns the number added; `.seeded` counts them. |
| `value_portfolio(..., curve_cache=cache)` | Reuses `cache` (seeded or kept in memory across calls). Results are byte-identical with or without it. Shift vectors that leave the same shocked quotes (a bucket past the last quote, alone or in a Hessian pair) share one bootstrap; the cache still keeps and exports one entry per shift vector. |

`benchmarks/portfolio_bench.py --positions 2000` measures ms per position (KBD and OIS;
first order, Hessian, scenarios, seeded). CI runs it on windows-latest / Python 3.11 and
writes the table to the job summary (informational, never gating).

## Horizon valuation (0.14.0)

| Name | Description |
| --- | --- |
| `horizon_valuation(instrument, curve, calendar, *, horizon_date, spread_bp=None, dirty_price_rub=None, spread_convention=None, redemption_horizon="maturity", curve_cache=None, fixings=None, projection_curve=None, strike_guard_bp=None)` | `HorizonValuation` per bond: `pv_start_rub`, `pv_forward_rub` (today's forwards realised), `pv_rolled_rub` (curve shape held), coupons and principal received in `(S0, S1]`, accrued and nominal at both settlements, `carry_rub`, `rolldown_rub`, `horizon_pnl_rub`. Exactly one of `spread_bp` / `dirty_price_rub`. Fixed-coupon bonds and (package 1) RUB floaters with `fixings`; others raise `UnsupportedProduct`. A floater's `accrued_start_rub` / `accrued_end_rub` are None (the fields are `float | None`). `explain_horizon_pnl` rejects floaters. |
| `explain_horizon_pnl(instrument, start_curve, end_curve, calendar, *, start_spread_bp \| start_dirty_price_rub, end_spread_bp \| end_dirty_price_rub, ...)` | `HorizonPnlExplain`: `carry + rolldown + rates + spread + residual == total` (full revaluation change to the end curve's date). |
| `roll_curve(curve, horizon_date)` | The curve moved forward with its shape in calendar days unchanged. |
| `horizon_portfolio(*, snapshot, instruments, positions, policy, horizon_date, curve_cache=None)` | `PortfolioHorizonResult`: `PositionHorizon` rows (signed RUB, per-bond detail), coverage with the same rejection codes as `value_portfolio`, covered carry / rolldown / horizon P&L and a total when coverage is complete. **(0.30.0)** `instruments` takes any `PortfolioInstrument`: swaps and repo legs are carried (`swaps`, `repos` rows, below); other derivatives are `UNSUPPORTED_PRODUCT`. |

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
| `ValuationPolicy(..., foreign_curves=(("USD", "USD_SOFR"), ...))` | Discount curve ID per non-RUB currency. `discount_curve_for(instrument)` raises `MissingMarketData` for an unmapped currency. |
| `ValuationPolicy(..., foreign_curve_fallbacks=(("USD", "USD_UST"), ...))`, `.fallback_curves_for(ccy)` | 0.36.0. Same-currency alternatives tried in order when the `foreign_curves` curve is **absent** from the snapshot (a present but unusable curve is still a rejection); each needs a `foreign_curves` entry, cannot repeat it and cannot be RUB. Default `()`: no fallback. |
| `PositionValuation.curve_fallback_from: str \| None` | 0.36.0. The missing curve a fallback replaced; `curve_id` is the fallback. Omitted when None. |
| `CurveUse.proxy: bool`, `CurveSpec.proxy`, `CurveSpec.par_coupon_frequency` | 0.36.0. A stand-in curve (government par yields, not OIS); `proxy` omitted when False. |
| `cny_ruonia_xccy_v1` (`CurveKind.XCCY_BOOTSTRAP`) for bonds | 0.36.0. The built `DiscountCurve` of `bootstrap_xccy_curve` (model `xccy-cny-logdf-v1`) discounts CNY bonds; an `XccyCurveBootstrapRequest` (quotes) under that ID is `INVALID_TERMS` for a bond. |
| `usd_ust_par_proxy_v1`, `eur_ecb_aaa_par_proxy_v1` (`CurveKind.PAR_YIELD_PROXY`, `PAR_YIELD_PROXY_MODEL_VERSION`) | 0.36.0. USD / EUR proxy discount curves from US Treasury par yields (semiannual bond-equivalent) and ECB AAA par yields (continuously compounded). |
| `ParYieldQuote(tenor, par_yield)`, `ParYieldCurveRequest(spec_id, curve_date, quotes)`, `bootstrap_par_yield_curve(request) -> DiscountCurve`, `par_yield(curve, spec_id, maturity)` | 0.36.0, `proxy_curves`. One log-linear DF node per quote so that each par bond prices at 100 (repricing ~1e-12); `par_yield` reads a curve back on the spec's convention. Decimal yields; `D/W/M/Y` tenors from the curve date, unadjusted. |
| `SUPPORTED_CURRENCIES` | `RUB`, `CNY`, `USD`, `EUR`. Other bond currencies are `UNSUPPORTED_PRODUCT`. |
| `get_curve_spec("<ccy>_discount_factors_v1")`, `get_curve_spec("<ccy>_fx_implied_v1")`, `foreign_discount_spec_id(ccy)`, `fx_implied_spec_id(ccy)`, `CurveKind.FX_IMPLIED` | **0.36.0**. Per-currency foreign specs for every three-letter code other than RUB, generated on lookup (not listed in `CURVE_SPECS`); a bond may be discounted on a built `<ccy>_fx_implied_v1` curve. |
| `cny_discount_factors_v1`, `usd_discount_factors_v1`, `eur_discount_factors_v1`, `CurveKind.DISCOUNT_FACTORS` | Caller-built foreign `DiscountCurve`s (log-linear DF nodes, ACT/365F, dated the business date). |
| `PositionValuation.currency_amounts: CurrencyAmounts \| None` | `currency`, `rub_per_unit`, `current_nominal`, `accrued_interest`, `dirty_price` (per bond) and signed `pv`, in the bond currency. The `_rub` fields are these at the snapshot rate. `None` for RUB bonds. |
| `PositionValuation.yield_convention` | Identifier of the issue's yield basis (`Instrument.yield_convention`), on which `effective_yield` and `macaulay_duration_years` are quoted. |
| `PortfolioRiskResult.fx_rates: tuple[FxRate, ...]` | FX rates used, by currency. |
| `PortfolioScenario(name, shock, fx_shocks_pct=(("USD", 10.0), ...))` | Exact revaluation with FX moved by a percent against RUB. |
| `fx_translated_factor_risk(risk, currency, fx_delta_rub_per_pct)` | Adds `FX|<ccy>` and its cross terms (`first_order_i / 100`) to RUB factor risk. |
| `Instrument(..., yield_convention=YieldConvention(day_count=DayCount.ACT_360))`, `YieldRequest(..., convention)`, `DEFAULT_YIELD_CONVENTION` | Yield time basis from the issue terms (BACKLOG #38); default effective annual ACT/365F. |

Rejections: an unmapped currency, a curve (and every fallback) or FX rate missing from
the snapshot are `MISSING_MARKET_DATA`; a curve of another currency, a foreign curve not dated the
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
| `ExerciseAssumption(kind, call_date=None, assumed_coupons=())`; `.to_maturity()`, `.to_call(date, assumed_coupons=())`, `.to_worst(assumed_coupons=())`, `.never_called(assumed_coupons=())`, `.identifier` | Explicit exercise assumption; `assumed_coupons` are reset coupons after the known periods. `"option_adjusted"` raises `UnsupportedProduct`. |
| `call_schedule(instrument)` | The `OfferKind.CALL` offers, by date (`price_pct` of the face outstanding). |
| `redemption_scenarios(instrument, settlement, *, assumed_coupons=())` | `RedemptionScenario` per call after settlement and maturity, with their `Cashflow`s; raises `InvalidTerms` when coupons to a date are unknown, have a gap or the date is mid-period. |
| `call_analysis(instrument, settlement, dirty_price_rub, *, assumed_coupons=())` | `CallAnalysis`: `ScenarioYield` per scenario (effective yield on the instrument's `yield_convention`, Macaulay and modified duration, `yield_convention` identifier), `yield_to_maturity`, `worst`. |
| `yield_to_call(..., call_date)`, `yield_to_maturity(...)`, `yield_to_worst(...)`, `yield_never_called(...)` | One `ScenarioYield`. Perpetuals: no maturity (`UnsupportedProduct`); never called is `exercise="never"` with `redemption_date` and `price_pct` None, and a candidate of the worst. |
| `price_to_worst(instrument, settlement, effective_yield, *, assumed_coupons=())` | `PriceToWorst`: lowest dirty price over the scenarios at the yield, accrued, clean %, the date. Inverse of `yield_to_worst`. |
| `extension_risk(instrument, settlement, dirty_price_rub, call_date, ...)` | `ExtensionRisk`: yields and durations to the call and to maturity, `duration_extension_years`, `pv_change_if_extended_rub`. |
| `resolve_exercise(instrument, settlement, assumption, *, dirty_price_rub=None)` | `ResolvedExercise` (exercise, date, price, `identifier`); `to_worst` needs the price. |
| `exercised_instrument(instrument, resolved)` | `ExercisedBond(instrument, resolved, source_sha256)`: the deterministic fixed bond; par calls only (`UnsupportedProduct` otherwise). |
| `perpetual_current_yield(coupon_rate, clean_price_pct)` | MOEX's published perpetual yield (coupon over clean price). |
| `PerpetualTail(anchor_date, amount, period_days=None, period_months=None)`, `perpetual_tail(periods)`, `perpetual_yield(settlement, dirty_price, cashflows, tail, convention)` → `PerpetualYield(effective_yield, macaulay_duration_years, modified_duration_years)`, `PERPETUAL_TAIL_MODEL_VERSION` | A never-called perpetual's coupons after the listed ones, forever (module `perpetual`, BACKLOG #125); `ExercisedBond.perpetual_tail`; `BondRiskRequest(..., perpetual_tail=)`, `price_cashflows` / `solve_z_spread(..., perpetual_tail=)`. |
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
| `MissingAssumption` (`MISSING_ASSUMPTION`) | A pass-through without a CPR; an open repo without an assumed end. |
| `CALL_MODEL_VERSION`, `PREPAYMENT_MODEL_VERSION`, `ASSUMED_BOOK_MODEL_VERSION` | `rub-call-exercise-v1`, `rub-constant-cpr-v1`, `rub-assumed-book-v1`. |

`value_portfolio` rejects callables and perpetuals unless `ValuationPolicy.exercise_assumptions`
names an assumption for them (below); `MissingAssumption` now lives in `errors` (re-exported).

## Derivatives in `value_portfolio`, result contract v3

Reference: [portfolio-v3.md](portfolio-v3.md). Bond-only books keep `rub-portfolio-risk-result.v2`
byte for byte; a book with a derivative position returns v3.

| Name | Description |
| --- | --- |
| `SwapInstrument`, `FxForwardInstrument`, `FxCrossInstrument` (0.36.0), `XccyInstrument`, `CdsInstrument`, `FxFuturesInstrument`, `RateFuturesInstrument`, `OfzFuturesInstrument` (`OfzDeliverable(instrument_id, conversion_factor)`), `RepoInstrument` | Derivative position types (`portfolio_instruments`): the product terms plus the curve, calendar and fixing IDs they read; `.product`, `.currency`. `PortfolioInstrument` / `DerivativeInstrument` are the unions. |
| `value_portfolio(*, ..., instruments: Sequence[PortfolioInstrument], ...)` | Any mix; IDs unique across all. Returns `PortfolioRiskResult` (v2) for bond-only books, `PortfolioRiskResultV3` otherwise. |
| `FxSpot(currency, rub_per_unit, value_date, calendar_id, evidence_id=None)`, `FxBasis(currency, foreign_curve_id, basis_bp, day_count=ACT_365F)`, `CreditCurveEntry(reference_entity, currency, quotes, recovery_rate, convention, evidence_id=None)`, `FuturesQuote(contract_id, price, margined_price, evidence_id=None)`, `IndexFixingEntry(fixing_id, fixings)`, `RuoniaFixingsEntry(fixing_id, fixings)` | New optional `MarketSnapshot` collections `fx_spots`, `fx_bases`, `credit_curves`, `futures_quotes`, `index_fixings`, `ruonia_fixings` and `ruonia_index` (`RuoniaIndexValue`s); accessors `fx_spot`, `fx_basis`, `credit_curve`, `futures_quote`, `index_fixing`, `ruonia_fixing`, `latest_ruonia_index`. `CurveEntry.curve` also takes a `KeyRateForwardCurve` (`rub_key_rate_swap_v1`) and an `XccyCurveBootstrapRequest` (`cny_ruonia_xccy_v1`). All omitted at their defaults. |
| `rebase_key_rate_curve(curve, business_date, *, rate_in_force)` | Re-dates a TOM-calibrated key-rate curve to the business date by extending its current-rate interval; `rate_in_force` must equal the curve's current rate. |
| `PortfolioRiskResultV3` | v2 fields plus `swaps`, `fx_forwards`, `fx_crosses` (0.36.0), `xccy_swaps`, `cds`, `fx_futures`, `rate_futures`, `ofz_futures`, `repos` (row types `SwapPositionValuation` … `RepoPositionValuation`), `position_models` (`PositionModel`), `hessian_totals` (`HessianTotal`), `jtd_totals` (`JtdTotal`), `jtd_total_rub`, `fx_spots`, `credit_curves` (`CreditCurveUse`); `.derivative_rows()`. |
| `PORTFOLIO_RESULT_SCHEMA_V3`, `verify_portfolio_result_v3(result)` | `rub-portfolio-risk-result.v3` and its hash check (`verify_portfolio_result` stays v2-only). `portfolio_result_to_json` writes both versions; `portfolio_result_from_json` reads both. |
| `PortfolioScenario(..., curve_shifts_bp=((curve_id, shifts), ...))`, `.curve_shift(curve_id, *, projection=False)`, `.named_curve_shift(curve_id)`, `.spread_shift_bp` | Per-curve shift vectors for every product (BACKLOG #43); omitted at the default. |
| `ValuationPolicy(..., exercise_assumptions=((instrument_id, ExerciseAssumption), ...))`, `.exercise_for(instrument_id)`; `PositionValuation.exercise_assumption`, `horizon="call"` / `"perpetual"` | Native exercise assumptions (BACKLOG #50, #125); omitted at the defaults. |
| `PRODUCT_ADAPTERS` | Position type -> valuation adapter (`portfolio_products`); the extension point for new products. |
| `FxFixing(currency, source, fixing_date, rub_per_unit, published_at=None, evidence_id=None)`; `MarketSnapshot(..., fx_fixings=())`, `.fx_fixing(currency, source, fixing_date)` | **0.36.0**. Published NDF fixings; point-in-time with `published_at`. A leg fixing today without one is projected (`fixing_pending`), after its fixing date `MISSING_MARKET_DATA`. |
| `ValuationPolicy(..., fx_curve_fallbacks=((curve_id, fallback_id), ...), fx_points_held_risk=False)` | **0.36.0** (BACKLOG #131, #46). Same-currency fallbacks of FX products' foreign curves when absent (a par-yield proxy only so), rows `curve_fallback_from`; per-row `points_held_rub_buckets`. Omitted at the defaults. |
| `PortfolioScenario(..., basis_shifts_bp=((factor, bp), ...))`, `.basis_shift_bp(factor)` | **0.36.0** (BACKLOG #103). Shifts `FXBASIS\|<ccy>`, `XCCYBASIS\|<ccy>`, `FUTBASIS\|<contract>`; omitted at the default. |
| `FxCrossPositionValuation`, `FxCrossPositionHorizon`; `FxForwardPositionValuation.curve_fallback_from`, `.points_held_rub_buckets`; `XccyPositionValuation.curve_fallback_from`; `FxFuturesPositionValuation.curve_fallback_from` | **0.36.0** rows; the new fields of existing rows are omitted at their defaults. |
| `ExpiredContract` (`EXPIRED_CONTRACT`), `SettledLeg` (`SETTLED_LEG`), `UndeclaredFixingGap` (`UNDECLARED_FIXING_GAP`), `MissingBasketPrice` (`MISSING_BASKET_PRICE`) | New coded rejections (subclasses of `InvalidTerms`, `MissingFixing`, `MissingMarketData`). |
| `swap_convention("rub_ruonia_ois_moex_v2")`; `SwapConvention(..., float_compounding="rate_day_count" \| "nfa_act")` | The v2 swap side (BACKLOG #61): NFA `RUB-RUONIA-OIS-COMPOUND`, `n_i / Act` weights and `(growth - 1) * Act / d * Act/Act ISDA`; the field is omitted at its default. |
| `SwapPositionHorizon`, `RepoPositionHorizon`; `PortfolioHorizonResult.swaps`, `.repos` **(0.30.0)** | Swap and repo carry rows; extended to every product in 0.31.0 (below). |

## Derivative carry and theta (0.31.0, BACKLOG #101)

Conventions: [CONVENTIONS.md](CONVENTIONS.md), "Derivatives: carry, rolldown and theta".
Each result has `pv_start`, `pv_forward` (today's curves read forward), `pv_rolled`
(rolled curves), `received`, `carry`, `rolldown` and `horizon_pnl` (the theta).

| Name | Description |
| --- | --- |
| `FxHorizon`, `FX_HORIZONS`, `fx_horizon(value)` | The FX spot at a horizon: `"spot_held"` or `"forwards_realised"`. |
| `forward_curve(curve, horizon_date)`, `roll_curve`, `roll_key_rate_curve(curve, horizon_date)` | Curve views at a horizon (`rub_fixed_income._curve_views`, re-exported by `horizon`). The key-rate roll holds the CBR decision date **(0.32.0)**. |
| `FxForwardRiskEngine.horizon(h, *, horizon_spot_value_date, fx_horizon="spot_held")`, `fx_forward_horizon(request, ...)` -> `FxForwardHorizonValuation` | FX forwards and swaps; `horizon_spot_rate` reports the spot used. |
| `FxFuturesRiskEngine.horizon(h, *, horizon_spot_value_date, fx_horizon="spot_held")`, `fx_futures_horizon(request, ...)` -> `FxFuturesHorizonValuation` | Prices `price_start` / `price_forward` / `price_rolled`; money is the variation margin. |
| `XccyRiskEngine.horizon(h, *, horizon_spot_value_date, fx_horizon="spot_held")`, `xccy_horizon(request, ...)` -> `XccyHorizonValuation` | Cross-currency swaps; a quote-built foreign curve is read forward and rolled as built. |
| `CdsRiskEngine.horizon(h)`, `cds_horizon(request, ...)` -> `CdsHorizonValuation`; `forward_hazard_curve`, `roll_hazard_curve` | Contract currency, conditional on no default; `accrued_start`, `accrued_horizon`. |
| `RateFuturesRiskEngine.horizon(h)`, `rate_futures_horizon(request, ...)` -> `RateFuturesHorizonValuation` | RUON, RUONIA index and KEYRATE; works on `for_quantity` engines. |
| `OfzFuturesRiskEngine.horizon(h, *, horizon_spot_settlement_date)`, `ofz_futures_horizon(request, ...)` -> `OfzFuturesHorizonValuation` | Spreads and basis held; `cheapest_start` / `_forward` / `_rolled`; a horizon settlement on or after delivery raises `InvalidTerms`. |
| `RepoCarry.pv_rolled_rub`, `.rolldown_rub`, `.horizon_pnl_rub` | Repo theta (fields omitted at their defaults). |
| `horizon_portfolio(..., fx_horizon="spot_held")`; `PortfolioHorizonResult.fx_forwards`, `.fx_crosses` (0.36.0), `.xccy_swaps`, `.cds`, `.fx_futures`, `.rate_futures`, `.ofz_futures`, `.fx_horizon`, `.derivative_rows()` | Every derivative carried; rows `FxForwardPositionHorizon`, `XccyPositionHorizon`, `CdsPositionHorizon`, `FxFuturesPositionHorizon`, `RateFuturesPositionHorizon`, `OfzFuturesPositionHorizon` (shape: `DerivativeHorizonHeader`). Covered rolldown now sums every row. |
| `HORIZON_ADAPTERS`, `HORIZON_ROW_FIELDS`, `horizon_spot_date(business_date, spot, calendar, horizon_date)` | Carry registry and the horizon spot date (today's lag) in `portfolio_products`. |

## The KBD as a risk curve (0.34.0)

Why: [proposals/kbd-risk-curve.md](proposals/kbd-risk-curve.md) (in Russian). Every new
field is omitted at its default.

| Name | Description |
| --- | --- |
| `CurveConvention(..., zero_shock="continuous" \| "annual")` | The unit of a bucket shock of a `DiscountCurve`: continuous zero rates, or effective annual zero yields (`curve_basis = "annual_zero"`), which sum with par OIS risk. |
| spec `rub_ofz_kbd_v2`; `CurveSpec.zero_shock`; `kbd_discount_curve(params, curve_date=None, *, horizon_years=30, zero_shock="continuous")` | The KBD as a risk curve in annual yields. |
| `FloatingCouponRule(index="kbd", ..., index_tenor_years=T, convexity=None)`; `FixingHistory("kbd", ..., tenor_years=T)` | MOEX `ZR_YLD_CRV`: the KBD yield at a constant maturity, a single fixing; projected as the forward yield over the tenor ([floating.md](floating.md)). |
| `ConstantMaturityConvexity(normal_vol_bp)`, `.adjust(forward, years_to_fixing, tenor_years)` | Optional convexity adjustment of a projected constant-maturity fixing. |
| `FloatingCouponPeriod(..., margin=None)` | The period's margin, replacing the rule's. |
| `basis_view(risk, base_curve_id, curve_id, basis_id)` | The same `FactorRisk` on the base curve and the basis `curve − base` (first order and Hessian). |

## OFZ curve risk in par quotes (0.37.0)

Why: [proposals/ofz-curve-risk-factors.md](proposals/ofz-curve-risk-factors.md) (in
Russian). Every new field is omitted at its default.

| Name | Description |
| --- | --- |
| `CurveConvention(..., zero_shock="annual_par")`; spec `rub_ofz_kbd_v3`; `kbd_discount_curve(..., zero_shock="annual_par")` | Bucket shocks of the par yields of annual-coupon instruments 1Y–20Y+ on the curve (`curve_basis = "annual_par"`; 1M–6M merged into 1Y). Also `local_risk` / `local_worker` accept `annual_par`. |
| `BondRiskEngine.second_order(*, bump_bp=1.0, method="bump" \| "analytic")`, `ANALYTIC_SECOND_ORDER_MODEL_VERSION` | `analytic`: the exact gradient and Hessian of a fixed-cashflow bond on a `DiscountCurve` (else `InvalidTerms` with the reason); on `annual_par` through the par Jacobian with the instruments' curvature. Same factors as `bump`. |
| `ValuationPolicy(..., second_order_method="bump" \| "analytic")` | `value_portfolio` takes the closed form for every bond that has one; others are bumped. |
| `BondRiskEngine.risk(*, bump_bp=1.0, difference=..., method="bump" \| "analytic")`, `ANALYTIC_RISK_MODEL_VERSION` (0.38.0) | `analytic` (`difference` central when omitted; `"forward"` is a `ValueError`): the exact first derivatives of the same bonds, the gradient of `second_order(method="analytic")` without the Hessian; equal to the central bumps up to their O(bump²) error, `parallel_dv01_rub` the bucket sum. |
| `ValuationPolicy(..., risk_method="bump" \| "analytic")` (0.38.0) | `value_portfolio` takes the closed-form first order for every bond that has one (`pv_rub`, `spread_buckets_rub`, and the factor first order without a Hessian); others are bumped. Separate from `second_order_method`, so 0.37.0 policies keep their results. |
| `benchmark_equivalents(benchmarks, positions) -> BenchmarkHedge`; `BenchmarkHedge(curve_date, equivalents, total_risk_rub_per_bp, condition_number, warnings)`, `BenchmarkEquivalent(instrument_id, maturity, maturity_years, yield_dv01_rub, risk_rub_per_bp, hedge_quantity)`, `BENCHMARK_EQUIVALENTS_MODEL_VERSION` | The hedge view: risk per benchmark bond's own yield and the offsetting quantities. `benchmarks` are `(instrument_id, BondRiskEngine)` (for example `prepare_local_bond_risk(...).engine`), positions fixed-cashflow bond engines on the same curve; from 0.38.0 also floaters that project or discount on it (a KBD `ZR_YLD_CRV` floater: coupons re-projected, central differences of `FLOATER_NODE_BUMP_BP` = 0.1 bp) and `OfzFuturesRiskEngine` whose bond or funding curve it is (closed form through the cheapest-to-deliver bond); curves compare by value. Warnings name positions that mature before the first benchmark or pay after the last (that risk is carried by the end benchmark), positions with a curve other than the benchmarks' (held; for example a floater's RUONIA discount curve) and hedges that amplify the positions' node risk more than 3 times (`AMPLIFICATION_WARNING`; large opposite-sign neighbours). `condition_number` is reported only: close benchmarks keep it near 3. |

## Curve risk in par quotes: key-rate and FX curves (0.38.0, issue #77)

The owner's decision of 2026-10-23: the risk of every curve in its par quotes, as RUONIA
OIS (`CurveBootstrapRequest`) and the KBD (`rub_ofz_kbd_v3`). Off by default: every new
field is omitted at its default, so earlier policies, snapshots, job IDs and results keep
their bytes.

| Name | Description |
| --- | --- |
| `KeyRateCurveRequest(build, business_date, rate_in_force)`; `.curve()`, `.shocked(shifts)`, `.shift_weights(start, end)`, `.identity`, `.curve_date` | KRS quotes (`KeyRateCurveBuildRequest`) as the risk source of a key-rate projection curve. `curve()` is the mid calibration re-dated to `business_date` (`rebase_key_rate_curve`). A bucket shift moves each mid quote by the bucket hats at its maturity (ACT/ACT ISDA time from `business_date`, as an OIS quote) and recalibrates on the same discount curve (`shocked`, the solve of `build_key_rate_forward_curve` with its checks); bid, ask, the calibration's discount curve and the decided rate do not move. Linear in the quotes. Curve basis `key_rate_quote`. |
| `SwapRiskRequest(..., projection_curve=KeyRateCurveRequest)`, `RateFuturesRiskRequest(KeyRateFuturesContract, KeyRateCurveRequest, ...)` | Key-rate swaps and KEYRATE futures with risk, scenarios and Hessian in the KRS quotes; valuation and carry on `curve()`. |
| `CurveEntry(curve_id, "rub_key_rate_swap_v1", KeyRateCurveRequest)` | KRS quotes in the snapshot, `business_date` equal to the snapshot's (else `INVALID_TERMS`). |
| `FxForwardRiskRequest(..., foreign_curve=XccyCurveBootstrapRequest)`, `FxCrossRiskRequest(..., base_curve=/quote_curve=XccyCurveBootstrapRequest)`, `FxFuturesRiskRequest(..., foreign_curve=XccyCurveBootstrapRequest)` | XCCY par quotes as the foreign curve (`xccy_quote`): rebuilt under every shock on the engine's shocked RUB curve (discounting and projecting RUONIA), quotes held under RUB shocks and moved by the bucket hats at their end dates under foreign ones, as in `XccyRiskEngine`. Valuation and carry on the curve built from the quotes. |
| `ParYieldCurveRequest` as a curve input; `.shocked(shifts)` | Par-yield proxy quotes (USD / EUR) as the risk source of bonds (`BondRiskRequest.curve`), CDS (`CdsRiskRequest.discount_curve`), FX forwards, swaps, crosses, FX futures and XCCY swaps (foreign curve): basis `par_yield_quote`, each par yield moved by the bucket hats at its maturity and the curve rebuilt (`bootstrap_par_yield_curve`). `CurveEntry` takes it under a `*_par_proxy_v1` spec. `local_risk.CurveRiskBasis` adds `par_yield_quote`. |
| `ValuationPolicy(..., quote_curve_risk=False)` | `True`: a curve the snapshot holds as quotes is risked in them (model flag `quote_curve` on derivatives): KRS quotes of key-rate swaps and KEYRATE futures; XCCY quotes named by FX forwards, swaps, crosses and futures (otherwise `INVALID_TERMS`, as before); par-yield proxy quotes for every position on the proxy (bond and CDS rows' `curve_basis = "par_yield_quote"`, FX and XCCY rows flagged `quote_curve`). `False`: KRS and par-yield quotes are built once and the built curve takes zero-rate (KRS: forward-rate) bucket shifts, the same numbers as the built curve in the snapshot. |

## Desk-monitor additions (0.33.0)

Why each exists and what the desk should call: [the review](reviews/CORE_REVIEW_FOR_DESK_MONITOR.md),
§6–§7. Every new field is omitted at its default; a policy using none of them values as
0.32.0, byte for byte.

| Name | Description |
| --- | --- |
| `ValuationPolicy(..., spread_kinds=((instrument_id, "credit" \| "ofz"), ...))`, `.spread_kind_for(instrument_id)` | Spread kind per instrument (core brief C8): one `value_portfolio` call for OFZ and corporates. Rows keep their own `spread_kind`; factor `CS` (and `factor_totals["CS"]`) sums own-spread risk of both kinds. |
| `ValuationPolicy(..., g_spread_curve_id=None)` | A KBD curve (`rub_ofz_kbd_v1` from `ZcycParameters`) every RUB fixed-coupon bond reports `g_spread_bp` to, whatever its discount curve. Not a KBD from its parameters: `INVALID_TERMS`; absent: `MISSING_MARKET_DATA` (omit the field on a date without a KBD). |
| `ValuationPolicy(..., discount_margin=False)`; `PositionValuation.discount_margin_bp`; `BondRiskEngine.discount_margin_bp()` | A floater's spread over its projection curve, in the policy's spread convention, that reprices its projected flows (published coupons, realised fixings, today's forwards, principal) to the dirty price. Equal to `spread_bp` on a single curve. |
| `ValuationPolicy(..., spread_buckets=False)`; `PositionValuation.spread_buckets_rub`; `BondRiskEngine.spread_bucket_risk(*, bump_bp=1.0, risk=None)` | The central `CS` sensitivity split by `RISK_BUCKETS` in proportion to the discount-only key-rate risk (coupons held at their base projection). CS01 by tenor; the OFZ basis by tenor for an OFZ spread to RUONIA OIS (R18). Sums to the `CS` first order. |
| `ValuationPolicy(..., require_known_coupons=False)` | Rejects (`MISSING_MARKET_DATA`) a fixed-coupon bond whose coupon periods have a gap, a zero amount or end before the horizon (core brief C7). |
| `ValuationPolicy(horizon="open_put")`; `Offer(..., window_start=None, window_end=None)`, `Offer.is_open(on)`; `Instrument.horizon("open_put", settlement)`; `PositionValuation.horizon == "open_put"` | The first put after settlement, before maturity, whose exercise window has not closed on the settlement date, confirmed or not (core brief C1). `confirmed_put` is unchanged. |
| `PortfolioScenario(..., spread_kind_shifts_bp=(("credit" \| "ofz", bp), ...))`, `.bond_shock(spread_kind)` | Bond spreads shifted by kind instead of `shock.spread_shift_bp` (an OFZ-basis or a credit-only shock). |
| `FloatingLeg.accrued_interest(settlement, curve=None, *, rate_basis=None)`; `Instrument.accrued(settlement, market=None)`; `FloatingCouponPeriod(..., published_rate=None)`; `FloatingCouponLeg(..., accrual_basis=None)` | Floater accrued interest, rounded half-up to 0.01 (BACKLOG #81; conventions in [floating.md](floating.md)). A sub-period that needs a projected rate takes it from `curve` (the instrument path: its projection curve); without one, `MissingMarketData`. `value_portfolio` / `horizon_portfolio` use it when the quote has no accrued. |
| `explain_horizon_pnl(..., fixings=None, end_fixings=None, projection_curve=None, end_projection_curve=None, strike_guard_bp=None)` | Floater P&L explain (BACKLOG #78). |
| `BondMetricsInput(settlement_date, dirty_price, cashflows, key="")`, `.from_instrument(instrument, settlement_date, clean_price_pct, *, accrued_rub=None, horizon="maturity", key=None)`; `BondMetrics(key, effective_yield, macaulay_years, modified_duration, g_spread_bp, z_spread_bp, error=None)`; `bond_metrics_batch(inputs, *, kbd=None, curve=None, yield_convention=DEFAULT_YIELD_CONVENTION, spread_convention=None)`; `BATCH_MODEL_VERSION` | Vectorised history metrics (module `batch`): the scalar definitions, solved with numpy for many bonds at once; `curve` is a discount curve or KBD parameters (the formula's discount factors). An unsolvable bond gets None and `error`; the batch never fails as a whole. |
| `kbd_effective_yields(params, tenors_years)` | `kbd_effective_yield` at many tenors (numpy), agreeing to about 1e-15. |
