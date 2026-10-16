# API reference (0.9.0)

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
| `CurveConvention(time_day_count=ACT_365_FIXED, interpolation="log-linear-df", extrapolation="flat-forward", fixed_leg_schedule=None, fixed_leg_day_count=ACT_365_FIXED)` | `.identifier`. Only the listed interpolation/extrapolation are accepted. |

All convention objects are frozen and hashable.

## Dates — `rub_fixed_income.dates`

- `year_fraction(start, end) -> float` — ACT/365F (legacy).
- `add_months(value, months) -> date` — end-of-month clamp.
- `annual_schedule(start, maturity) -> tuple[date, ...]` — legacy; keeps the adjusted-maturity stub. Raises if `maturity <= start`.
- `BusinessCalendar(holidays=frozenset(), working_days=frozenset(), version="weekends-only")` — `.is_business_day`, `.add_business_days(value, n)` (n ≥ 0), `.modified_following`.

## Cash flows

- `EventKey(event_date, ordinal)`, `CashEvent(key, amount_rub_per_bond, event_id)`, `BondCashflow(payment_date, amount)`.
- `clean_price_pct_to_dirty_rub(clean_pct, current_nominal_rub, accrued_interest_rub) -> float`; `dirty_rub_to_clean_price_pct(...)`. Raise for non-positive nominal or negative AI; **(0.3.1)** also for clean/dirty price ≤ 0, dirty ≤ AI (implied clean ≤ 0), non-finite/`bool` input, overflowing result.
- `clean_price_pct_to_dirty_amount(...)` — alias.

## Discount curves

- `DiscountCurve(curve_date, nodes, model_version="ois-logdf-v1", _flat_continuous_rate=None, convention=CurveConvention())` **(0.3.0: `convention` field; non-finite DFs rejected)**.
  - `.discount_factor(d)` — raises if `d < curve_date`.
  - `.zero_rate(d, periods_per_year=1)` — int ≥ 1 or `Compounding` (incl. `CONTINUOUS`).
  - `.forward_rate(start, end)` — annual compounding over `τ(start, end)`.
  - `.par_rate(maturity)` / `.par_ois_rate(maturity)`; `.fixed_leg_schedule(maturity)` **(new)**.
  - `.max_repricing_error_bp(quotes)`.
  - `DiscountCurve.flat(curve_date, continuous_rate, *, convention=None)` — raises for non-finite rate.
- `OisQuote(tenor, maturity, par_rate)`.
- `CurveBootstrapRequest(curve_date, quotes, convention=CurveConvention())` **(0.3.0: `convention`)**.
- `bootstrap_discount_curve(request) -> DiscountCurve` — raises for empty / non-increasing / non-finite quotes, unbracketed forward; `RuntimeError` if repricing > 1e-7 bp.

## Yield, duration, spreads (`compatibility`)

- `solve_nominal_annual_yield(*, dirty_price, settlement_date, cashflows, compounding_frequency=1, day_count_basis=365.0, convention: YieldConvention | None = None) -> float`
  Raises: non-finite/≤0 price, non-finite cash flows, no future positive flow, invalid
  frequency/basis, both `convention` and legacy keywords, **root below −0.99·m or above 1e6 →
  "not bracketed" (0.3.0)**.
- `bond_duration(*, annual_yield, settlement_date, cashflows, compounding_frequency=1, day_count_basis=365.0, convention=None) -> DurationResult(macaulay_years, modified_years, present_value)`
  Raises: non-finite yield **(0.3.0)**, base `1 + y/m <= 0`, no positive PV, non-finite/`bool` flows and overflowing PV or duration **(0.3.1)**.
- `price_cashflows(cashflows, curve, *, spread=0.0, compounding_frequency=1, settlement_date=None, convention: ZSpreadConvention | None = None) -> float`
  Raises: non-finite spread/cash flows **(0.3.0)**, settlement before curve date, rate outside compounding domain, `bool` flows and overflowing result **(0.3.1)**. Signed flows netting to 0 return `0.0`.
- `solve_z_spread(cashflows, dirty_price, curve, *, compounding_frequency=1, settlement_date=None, convention=None) -> float`
  Raises: empty flows, non-finite/≤0 price **(0.3.0)**, non-finite flows **(0.3.0)**, no flow after settlement **(0.3.0)**, settlement before curve date, conflicting `compounding_frequency` + `convention`, "Z-spread root is not bracketed" (bracket −95%…500%), non-finite objective mid-solve **(0.3.0)**; `RuntimeError` "did not converge".
- `solve_continuous_z_spread(cashflows, dirty_price, curve, *, settlement_date=None) -> float` — continuous Z over settlement-forward DFs; bracket ±500%.

## Effective yield and financing (`bond`)

- `YieldRequest(horizon_date, dirty_price_rub_per_bond, cashflows)`; `solve_effective_annual_yield(request) -> YieldResult(effective_annual_yield, repricing_error_rub_per_bond)`. Raises: no flows, non-finite/≤0 price **(0.3.0: non-finite)**, non-finite flows **(0.3.0)**, flows on/before horizon, "forward yield root is not bracketed".
- `AccumulationPeriod`, `constant_daily_accumulation_periods(start, horizon, boundaries, annual_rate)`.
- `FinancedBondRequest`, `project_financed_bond(request) -> FinancedBondProjection(horizon_dirty_rub_per_bond, steps)`. Raises **(0.3.1)**: non-finite/`bool` cash-event amount, `bool` initial dirty cash or factor, overflowing balance. Signed finite cash events are valid (an expense of −50 adds 50 to the balance).

## Snapshot analytics (`analytics`)

- Inputs: `BondTermsV1(issue_id, current_nominal_rub, maturity_date, horizon, horizon_date, accrual_period_start, accrual_period_end, coupon_amount_rub, terms_hash)`, `MarketObservationV1(clean_price_pct_of_current_nominal, settlement_date, selected_price_basis, market_hash)`, `CurveSnapshotV1(curve_world_instance_id, discount_curve, curve_hash)`, `ValuationContext(valuation_date, settlement_date)`, `CalculationPolicy(money_quantum=Decimal("0.01"), spread_lower_bp=-5000, spread_upper_bp=5000, root_tolerance=1e-13)` (+ `.validate()` **(new)**).
- `analyze_bond(terms, schedule, market, curve, context, policy) -> BondAnalyticsResultV1 | CalculationFailure` — frozen V1 conventions (continuous Z). **(0.3.0)**: invalid policy → `INVALID_UNITS`; AI 0 on coupon date.
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

## Foundations (0.9.0)

| Name | Description |
| --- | --- |
| `BusinessCalendar(holidays, working_days, version, valid_from=None, valid_to=None)` | Bounded calendars raise `CalendarRangeError` outside `[valid_from, valid_to]`. `.is_bounded`, `.covers(d)`, `.require_covered(d)`. Sets are frozen on construction. |
| `calendar_identity(calendar) -> (version, fingerprint)` | What to record in results. |
| `canonical_json_bytes(value)`, `canonical_sha256(value)` | Now in `rub_fixed_income.canonical` (still importable from `analytics`). Enums, sets, nested dataclasses and date keys have canonical forms. |
| `to_dict(value)`, `from_dict(cls, data)` | Strict JSON-ready round trip of core dataclasses; `from_dict` re-runs validation. |
| `CoreRejection` and `UnsupportedProduct`, `MissingMarketData`, `CalendarRangeError`, `UnknownCurveSpec`, `InvalidTerms` | Typed rejections with a stable `.code`; all subclass `ValueError`. |
| `DiscountCurve.flat_continuous_rate` | Rate of a `DiscountCurve.flat` curve, `None` for node curves. |

## Fixed-RUB risk extensions (0.5.0)

See [second-order and local API](second-order-local-risk.md) for `BondRiskEngine.second_order`, `approximate`, `jump_to_default`, and `prepare_local_bond_risk`.
