# Floating coupons: known fixings plus curve projection

`rub_fixed_income.floating` computes contractual RUONIA and key-rate floater coupons
from an explicit point-in-time fixing history and a projection curve. Known fixings
and published coupons stay fixed under risk bumps; only unfixed observations move
with the curve. The module does no I/O. The caller supplies the fixing snapshot,
the publication calendar and the coupon terms.

The core model shipped in 0.8.0. Package 1 adds floors and caps, a multiplier,
observation shifts in calendar or business days, in-advance observation windows,
single fixings in advance or in arrears, calendar-day key-rate observation,
publication timestamps, separate projection and discount curves, and floaters in
`Instrument`, `value_portfolio` and the horizon functions. Every new term is an
optional field left out of the canonical form at its default
(`metadata=OMIT_IF_DEFAULT`), so 0.8.0 rules, legs and histories keep their bytes,
hashes and identifiers.

## Inputs

- `FixingHistory(index, valuation_date, fixings, calendar, snapshot_id, max_unpublished_business_days=0, known_at_utc=None, published_at=())`
  - Rates are decimal annual (0.15 = 15 %).
  - **Every fixing must be dated before `valuation_date`.** A RUONIA fixing for day T
    is published on T+1, and rejecting later dates prevents look-ahead.
  - `snapshot_id` and `known_at_utc` name the caller's point-in-time selection.
    `known_at_utc` is an ISO 8601 timestamp with an offset (`history.known_at` parses it).
  - `published_at` optionally gives every row's publication time (UTC-aware, aligned
    with `fixings`). `history.published_by(cut)` drops the rows published after `cut`.
  - An empty history is allowed. It projects coupons observed wholly on or after
    `valuation_date`; any known observation it lacks raises `MissingFixing`.
  - **RUONIA** must have a fixing on every `calendar` business day up to its latest
    fixing; a missing business day raises `MissingFixing`. Only the trailing business
    days before `valuation_date` with no published fixing yet may carry the latest
    fixing, and at most `max_unpublished_business_days` of them. Carried dates are
    reported. A RUONIA fixing dated on a non-business day is a calendar mismatch.
  - **`calendar` is the publication calendar of the index**: its business days are
    the days with a fixing. CBR does not compute RUONIA on the last business day of the
    year (2023-12-29, the working Saturday 2024-12-28, 2025-12-30); mark it a holiday
    and the previous fixing covers it, as in MOEX coupons. With the official calendar
    the day is a missing fixing and raises. Core never fills a missing fixing.
  - **The key rate**: rows are dated by the **effective date** of each CBR decision,
    and a rate applies from that date until the next row, so gaps are expected. A
    missing change in the input cannot be detected.
- `FloatingCouponRule(index, averaging, margin, coupon_day_count, rate_day_count, lookback_business_days=0, rate_rounding_decimals=None, amount_rounding_decimals=None, multiplier=1.0, bounds=None, observation_shift=None, observation_days="business", fixing_date=None, observation_window=None)`
  - `averaging`: `compounded`, `arithmetic`, or `single_fixing` (one rate, see
    `fixing_date`).
  - `margin` is decimal annual (0.015 = +150 bp); `multiplier` scales the index rate
    and must be positive.
  - `rate_day_count` weights each daily rate. Use `ACT_365_FIXED` for RUONIA, or
    `ACT_ACT_ISDA` for 366-day leap years. `coupon_day_count` annualises the index and
    accrues the amount.
  - `observation_days`: `business` (each day takes the latest business day on or
    before it) or `calendar` (key rate only: each calendar day takes the rate in force
    on it).
  - At most one of `lookback_business_days`, `observation_shift`,
    `observation_window` and `fixing_date` shapes the observation.
- `CouponRateBounds(applies_to, floor=None, cap=None)`: per-period floor and/or cap,
  decimal annual. `applies_to="index_rate"` bounds the benchmark average before the
  multiplier and margin; `"coupon_rate"` bounds the all-in rate after them. Applied
  before rounding.
- `ObservationShift(days, unit, accrues_over)`: the observation period is
  `[start - L, end - L)`, `L` in `business_days` (strictly before each boundary) or
  `calendar_days`. Rates **and** day weights come from the shifted window, whose
  `coupon_day_count` annualises the index. `accrues_over="observation"` accrues the
  amount over the window too: `amount = notional * (index_return + margin * tau_window)`,
  the MOEX OFZ-PK rule; `"accrual"` accrues it over the accrual period (the ISDA
  observation-period shift).
- `ObservationWindow(end_offset_days, end_unit, length_months=None)`: an in-advance
  window ending `end_offset_days` before the accrual start, lasting `length_months`
  months or the coupon's length in calendar days. The rate comes from the window,
  the amount accrues over the accrual period.
- `FixingDateRule(anchor, offset_days, unit)`: the single fixing date, `offset_days`
  before `accrual_start` (in advance), `accrual_end` or `payment_date` (in arrears).
- `FloatingCouponPeriod(accrual_start, accrual_end, payment_date, notional_rub, published_coupon_rub=None)`
  - Amounts are RUB per bond, on the notional outstanding during the period.
  - **A published coupon is cash and is used as is.** MOEX shows 0 for coupons not
    yet fixed, so `0.0` is rejected; pass `None` for those.
- `FloatingLeg(rule, periods, history, strike_guard_bp=None)`
  - Periods must be ordered and must not overlap. Principal is not part of the leg.
  - `leg.project(curve, settlement_date=...)` returns `FloatingCouponProjection`
    rows for the coupons paid after settlement. Earlier periods need no history.
  - `strike_guard_bp` is the floor/cap model policy (below).

## Observation and coupon formula

**Which rate applies.** Each day of the observation period takes the rate of the
latest business day on or before it, so a Friday rate covers the weekend. With
`lookback_business_days = L`, that business day observes the fixing L business days
earlier: rates move back, weights stay on accrual days (lookback without observation
shift). With an observation shift or window the whole period moves. With calendar-day
observation each day observes itself.

**Known or projected.** An observation dated before `valuation_date` comes from the
history. An observation on or after it is the curve's overnight forward over
`[observation, next)`, `next` the following business day (or calendar day):

```text
f = (DF(observation) / DF(next) - 1) / tau(observation, next)
```

**Index return and coupon.** Each rate `r_i` applies for the days its segment covers,
and `tau_i` is that span in `rate_day_count`:

```text
compounded:     prod(1 + r_i * tau_i) - 1
arithmetic:     sum(r_i * tau_i)
index_rate      = index_return / coupon_day_count(observation period)
single_fixing:  index_rate = r(fixing date)

bounded_index   = clamp(index_rate)                    if bounds apply to index_rate
coupon_rate     = multiplier * bounded_index + margin
coupon_rate     = clamp(coupon_rate)                   if bounds apply to coupon_rate
amount          = notional * coupon_rate * coupon_day_count(accrual period, or the
                  shifted window when accrues_over="observation")
```

The observation period is the accrual period unless shifted or windowed.

**Consistency with the curve.** When every observation is projected, there is no
lookback or shift and the boundaries are business days, a compounded coupon equals
`notional * (DF(start) / DF(end) - 1)` exactly. A coupon set on its reset date on a
single curve therefore prices at par and has no rate risk.

**Rounding.** Rounding is half-up: the coupon rate in percentage points, then the
amount in RUB. It applies only to coupons whose every observation is known
(`source = "fixed_by_history"`). A partially or fully projected coupon stays
unrounded, because rounding a forecast does not preserve its expectation and would
make finite-difference risk noisy.

**Source of each coupon.** Each projection reports one of `published`,
`fixed_by_history`, `partially_projected` or `projected`, the realized and projected
day counts, the carried RUONIA business days and `bound` (`floor`, `cap` or None).

A negative coupon raises an error; give the floor if the terms have one.

## MOEX-confirmed rules

`tests/test_moex_floater_goldens.py` reproduces 328 published coupons of 21 floaters
to the kopeck on their real accrual dates, plus the in-advance OFZ-PK 29007–29010
rates and amounts (`docs/release-notes/floater-goldens.md`):

| Bonds | Rule |
| --- | --- |
| OFZ-PK 29013+ ("Ставка RUONIA") | arithmetic, `ObservationShift(6, "calendar_days", "observation")`, ACT/ACT ISDA, amount half-up from the unrounded rate |
| OFZ-PK 29026–29028, VEB.RF "Индекс RUONIA" | compounded, 7-day calendar shift over the window |
| OFZ-PK 29007–29010 (in advance) | arithmetic over `ObservationWindow(2, "business_days", length_months=6)`, rate rounded to 0.01 %, ACT/365F amount over the coupon |
| Monthly corporates in advance | `ObservationWindow(6, "calendar_days")`, rate rounded to 0.01 % |
| Key-rate corporates | 6- or 4-day calendar shift, or a 4-business-day lookback |

A business-day lookback or business-day shift does not reproduce the calendar-day
rules. The first coupons of SU29026 and SU29027 (December 2024) stay a pinned gap.

## Floors and caps: intrinsic value with a guard

Core has no volatility model, so a floor or cap is priced at **intrinsic value on the
projected rate**: the deterministic forward rate is bounded. That is close to the
option's value only when the option is deep in or out of the money. The policy is
explicit: a projected coupon (partially or fully) with a bound is accepted only when
its unbounded projected rate (index or all-in, as the bound applies) is at least
`strike_guard_bp` from every strike. Otherwise, or when the guard is None, projection
raises `UnsupportedProduct` (`UNSUPPORTED_PRODUCT` in coverage). Coupons fully fixed
by history are bounded exactly. `leg.has_optionality` flags a leg with bounds and
`FloatingCouponProjection.bound` names the strike that set a coupon.

The guard is checked on the base projection only. Shocked curves for risk and exact
scenarios apply the bound deterministically, so a large scenario can cross a strike;
its P&L is then the intrinsic-value P&L. Option time value is not modelled.

## Risk

`BondRiskRequest(..., floating_leg=leg)` projects the coupons off the risk curve,
both the base curve and every shocked one, and adds them to the fixed `cashflows`,
which then carry principal and any fixed amounts. The fixing history must be
dated at the curve date.

**Single curve** (`projection_curve=None`): the curve projects and discounts, and the
calibrated spread is a discount margin. Results carry `model_version =
"rub-floating-coupon-risk-v1"` (`rub-floating-coupon-second-order-v1` for the Hessian).
This path is numerically unchanged from 0.8.0: PV, spread, `base_id`, risk, Hessian,
scenarios and approximations are bit-identical for every 0.8.0 rule.

**Two curves** (`projection_curve=...`, a `DiscountCurve` or OIS quotes dated like
`curve`): the coupons project off the projection curve, `curve` discounts, and the
spread is a margin over `curve`. Each curve is shocked in its own basis with its own
11 buckets:

- `engine.risk()` returns a `TwoCurveBondRiskResult` (a `BondRiskResult` subclass):
  `buckets` are the discount curve's, `projection_buckets` the projection curve's,
  `dv01_rub` sums all 22 and `parallel_dv01_rub` moves both curves together;
- `engine.second_order()` is central over 23 factors
  (`discount <bucket>`, `projection <bucket>`, the spread);
- `engine.reprice(scenario, projection_curve_shifts_bp=None)` moves the projection
  curve by the given shifts or, when None, by the scenario's curve shifts too;
  `approximate` takes the same argument;
- `bond_factor_risk(risk, discount_id, second, projection_curve_id=...)` maps to
  `IR|<discount>|*`, `IR|<projection>|*` and `CS`, with the cross Hessian;
- `model_version = "rub-floating-two-curve-risk-v1"`
  (`rub-floating-two-curve-second-order-v1`); `base_id` adds the projection curve.

A reset-date floater has large and opposite discount and projection DV01 that nearly
cancel when both curves move together; CS01 stays about the annuity.

**Key-rate projection.** `key_rate_projection_curve(forward, calendar, rate_day_count=, observation_days=)`
turns a `KeyRateForwardCurve` into a `DiscountCurve` with one node per business (or
calendar) day, `DF(next) = DF(day) / (1 + rate_at(day) * tau)`, so a leg observed the
same way reproduces the key-rate path exactly. Register it under
`rub_key_rate_projection_v1`; risk shifts its continuous zero rates.

**Performance.** Coupons that read only discount factors unchanged by a shock are
reused, and overnight forwards are memoised on each shared shocked curve, without
changing a bit of the result. `benchmarks/floater_bench.py` times 2k floaters.

## Portfolio and horizon

- `FloatingCouponLeg(rule, periods, fixing_id, projection_curve_id=None)` in
  `Instrument`: `projection_curve_id` names the snapshot curve that projects (a
  RUONIA OIS spec for RUONIA, `rub_key_rate_projection_v1` for the key rate).
- `MarketSnapshot.point_in_time_fixing(fixing_id)`: the history must be dated the
  business date and say when it was known: `known_at_utc` after the snapshot's
  `known_at` is rejected (`INVALID_TERMS`), rows with `published_at` after it are
  dropped, and a history with neither is rejected.
  `MarketSnapshot.projection_curve(curve_id, index)` checks the spec.
- `Instrument.cashflows(..., market=snapshot, strike_guard_bp=None)` projects the
  coupons; without `market` a floating leg still yields principal only.
  `Instrument.floating_leg(market, horizon=, strike_guard_bp=)` builds the model.
- `value_portfolio` values RUB floaters (`FLOATING_BOND`). Single-curve when the leg
  projects off the position's discount curve, two-curve otherwise. Accrued interest
  must come with the quote (`MISSING_MARKET_DATA` otherwise: core assumes no floater
  accrual convention). `ValuationPolicy.cap_floor_guard_bp` is the guard;
  `PortfolioScenario.projection_curve_shifts_bp` moves two-curve projection curves.
  Yield and duration are on the base projected flows; `g_spread_bp` is None. A leg
  without `projection_curve_id`, a non-RUB floater and a near-the-money bound are
  `UNSUPPORTED_PRODUCT`; missing fixings `MISSING_MARKET_DATA`.
- `horizon_valuation(..., fixings=, projection_curve=, strike_guard_bp=)` carries a
  floater: start, forward and received coupons at today's forwards; the rolled value
  keeps today's forwards up to the horizon and projects later observations off the
  rolled projection curve. Floater accrued fields are None. `horizon_portfolio`
  handles floaters as `value_portfolio` does. `explain_horizon_pnl` rejects floaters
  (`UnsupportedProduct`): it would need the fixings realised over the horizon.

The local portfolio worker (`FixedRubBondInput`, `run_local_risk`) still accepts
fixed coupons only.
