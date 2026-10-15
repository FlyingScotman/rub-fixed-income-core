# Floating coupons: known fixings plus curve projection (0.8.0)

`rub_fixed_income.floating` computes contractual RUONIA and key-rate floater coupons
from an explicit point-in-time fixing history and a projection curve. Known fixings
and published coupons stay fixed under risk bumps; only unfixed observations move
with the curve. The module does no I/O. The caller supplies the fixing snapshot,
the publication calendar and the coupon terms.

## Inputs

- `FixingHistory(index, valuation_date, fixings, calendar, snapshot_id, max_unpublished_business_days=0, known_at_utc=None)`
  - Rates are decimal annual (0.15 = 15 %).
  - **Every fixing must be dated before `valuation_date`.** A RUONIA fixing for day T
    is published on T+1, and rejecting later dates prevents look-ahead.
  - `snapshot_id` and `known_at_utc` name the caller's point-in-time selection, for
    example `CurveRepository.fixing_snapshot(...)` in market-data-store.
  - **RUONIA** must have a fixing on every `calendar` business day up to its latest
    fixing; a missing business day is a `LookupError`. Only the trailing business days
    before `valuation_date` with no published fixing yet may carry the latest fixing,
    and at most `max_unpublished_business_days` of them. Carried dates are reported.
    A RUONIA fixing dated on a non-business day is rejected as a calendar mismatch.
  - **The key rate** stays in force until changed, so gaps between its fixings are
    expected. A missing change in the input cannot be detected.
- `FloatingCouponRule(index, averaging, margin, coupon_day_count, rate_day_count, lookback_business_days=0, rate_rounding_decimals=None, amount_rounding_decimals=None)`
  - `margin` is decimal annual (0.015 = +150 bp).
  - `rate_day_count` weights each daily rate. Use `ACT_365_FIXED` for RUONIA, or
    `ACT_ACT_ISDA` for 366-day leap years.
  - `coupon_day_count` is the coupon accrual basis.
- `FloatingCouponPeriod(accrual_start, accrual_end, payment_date, notional_rub, published_coupon_rub=None)`
  - Amounts are RUB per bond, on the notional outstanding during the period.
  - **A published coupon is cash and is used as is.** MOEX shows 0 for coupons not
    yet fixed, so `0.0` is rejected; pass `None` for those.
- `FloatingLeg(rule, periods, history)`
  - Periods must be ordered and must not overlap. Principal is not part of the leg.
  - `leg.project(curve, settlement_date=...)` returns `FloatingCouponProjection`
    rows for the coupons paid after settlement. Earlier periods need no history.

## Observation and coupon formula

**Which rate applies.** Each accrual day takes the rate of the latest business day
on or before it, so a Friday rate covers the weekend. With `lookback_business_days = L`,
that business day observes the fixing L business days earlier. Rates move back, but
the weights stay on accrual days: this is lookback without observation shift.

**Known or projected.** An observation dated before `valuation_date` comes from the
history. An observation on or after it is the curve's overnight forward over
`[observation, next business day)`:

```text
f = (DF(observation) / DF(next business day) - 1) / tau
```

**Index return and coupon.** Each rate `r_i` applies for the days its accrual
segment covers, and `tau_i` is that span in `rate_day_count`. The index return is:

```text
compounded:  prod(1 + r_i * tau_i) - 1
arithmetic:  sum(r_i * tau_i)

index_rate  = index_return / coupon_day_count(accrual_start, accrual_end)
coupon_rate = index_rate + margin
amount      = notional * coupon_rate * accrual
```

**Consistency with the curve.** When every observation is projected, `L = 0` and
the boundaries are business days, a compounded coupon equals
`notional * (DF(start) / DF(end) - 1)` exactly. A coupon set on its reset date on a
single curve therefore prices at par and has no rate risk.

**Rounding.** Rounding is half-up: the coupon rate in percentage points, then the
amount in RUB. It applies only to coupons whose every observation is known
(`source = "fixed_by_history"`). A partially or fully projected coupon stays
unrounded, because rounding a forecast does not preserve its expectation and would
make finite-difference risk noisy.

**Source of each coupon.** Each projection reports one of `published`,
`fixed_by_history`, `partially_projected` or `projected`. It also reports realized
and projected day counts and the carried RUONIA business days.

Floors, caps, payment-lag observation shifts and in-arrears key-rate fixings are not
modelled. A negative projected coupon raises an error.

## Risk

`BondRiskRequest(..., floating_leg=leg)` projects the coupons off the risk curve,
both the base curve and every shocked one, and adds them to the fixed `cashflows`,
which then carry principal and any fixed amounts. The fixing history must be
dated at the curve date. The model is single-curve: the curve projects and
discounts, and the calibrated spread is a discount margin.

Bucket DV01 therefore covers coupon re-projection. A reset-date par floater has
about zero DV01, the known part of the current coupon behaves like a short fixed
cash flow, and CS01 stays about the annuity.

Results carry `model_version = "rub-floating-coupon-risk-v1"`, or
`rub-floating-coupon-second-order-v1` for the Hessian. `base_id` binds the rule,
the periods, the history identity (snapshot, known-at, calendar fingerprint, fixings
hash) and the base projected coupons. Fixed-cashflow requests keep
`rub-fixed-cashflow-risk-v1` and are byte-identical to 0.7.0.

A key-rate floater needs a key-rate projection curve as its risk curve, with its
spread as a discount margin over it. Separate projection and discount curves with
their own risk factors are not supported yet.

On a 5-year quarterly floater with 6 OIS quotes (Linux container, Python 3.13), a
cold engine takes:

| Step | Time |
| --- | --- |
| Calibration | 0.01 s |
| `risk()` | 0.07 s |
| Hessian | 1.6 s |

The local portfolio worker (`FixedRubBondInput`, `run_local_risk`) still accepts
fixed coupons only.
