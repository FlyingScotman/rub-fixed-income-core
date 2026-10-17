# Callables, perpetuals and pass-throughs (package 9b)

Modules: `rub_fixed_income/callables.py`, `rub_fixed_income/pass_through.py`,
`rub_fixed_income/assumed_portfolio.py`. Units follow `docs/CONVENTIONS.md`: amounts per
bond in the instrument currency, prices in % of the current face, decimal rates.

Core does not model the issuer's call decision or the borrowers' prepayment
behaviour. Every value of a callable, a perpetual or a pass-through comes from an
**explicit assumption** that the caller passes and that the result records.

## Call schedule

The call schedule is the instrument's `Offer` entries of kind `OfferKind.CALL`: a date and
a redemption price `price_pct` in % of the face outstanding just before that date (100 for
par). The `confirmed` flag is kept as given; yield to worst considers every call after
settlement either way. Discrete call dates only: a continuous (American) call period,
make-whole calls and notice periods are not modelled.

## Exercise assumptions

`ExerciseAssumption`:

| Assumption | Redemption | Notes |
|---|---|---|
| `to_maturity()` | maturity, no call exercised | MOEX's treatment of call bonds without a set call date, and core's behaviour before (the default) |
| `to_call(date)` | the listed call on `date` | MOEX's treatment when the call date is set (`CALLOPTIONYIELD`) |
| `to_worst()` | the call or maturity with the lowest yield at the quoted price | resolved once, at the price, then held fixed |

`assumed_coupons` (on any of the three) are coupon periods the caller assumes after the
instrument's known periods, for example the coupon after a reset. They must be of the
leg's period type (`CouponPeriod` for `FixedCouponLeg`, `RateCouponPeriod` for
`RateAccrualCouponLeg`) and follow the known periods. They are part of the assumption's
identity (`identifier` carries their hash).

**Coupon coverage.** Every scenario needs its coupons known, without gaps, up to the
redemption date, and the redemption date must be a coupon payment date. Otherwise the
scenario is rejected with `InvalidTerms`: "coupons up to D are not known" (pass reset
terms), "falls inside a coupon period" (a mid-period call is not modelled) or "gap".
Floating legs are rejected (`UnsupportedProduct`).

**Held fixed under risk.** `to_worst` picks the date at the base price. Rate bumps,
spread bumps, scenarios and the Hessian then reprice the exercised bond: the exercise
date does not switch. This is yield-to-worst risk, not option-adjusted risk.

**No OAS.** An option-adjusted spread needs a short-rate model and a lattice, which core
does not have. `option_adjusted_spread(...)` and `ExerciseAssumption("option_adjusted")`
raise `UnsupportedProduct` with that reason.

## Analytics

Yields are effective annual, `P = sum CF / (1 + y)^t`, with `t` on the instrument's
`yield_convention` time basis (default ACT/365F, the MOEX quote basis). Durations are
Macaulay and modified at that yield.

- `call_analysis(instrument, settlement, dirty)`: yields and durations to every call
  and to maturity (`ScenarioYield`), the yield to maturity and the worst (lowest yield;
  ties go to the earliest date).
- `yield_to_call`, `yield_to_maturity`, `yield_to_worst`.
- `price_to_worst(instrument, settlement, y)`: the lowest dirty price over the
  scenarios at yield `y`, with accrued and clean %. At `y = YTW(P)` it returns `P`.
- `extension_risk(instrument, settlement, dirty, call_date)`: durations to the call and
  to maturity, `duration_extension_years` (maturity minus call), and
  `pv_change_if_extended_rub`, the to-maturity flows valued at the yield to call minus
  the dirty price (positive when the coupon is above the yield to call).

## Exercised bonds

`resolve_exercise(instrument, settlement, assumption, dirty_price_rub=None)` gives the
redemption date, kind and price. `exercised_instrument(instrument, resolved)` builds the
deterministic bond it implies, with the same instrument ID:

- `to_maturity`: only the calls are removed. A bond without calls (and without assumed
  coupons) comes back equal to the input, with the same hash;
- a call: coupons up to the date, earlier amortizations, and the face outstanding
  redeemed on the date. Puts before it stay; calls go.

A call away from par cannot be written as such a bond (its redemptions would not sum to
the face), so `exercised_instrument` rejects it (`UnsupportedProduct`). The yield
functions handle any call price; the portfolio path is par calls only. The four MOEX call
bonds examined for the goldens all call at 100 %.

## Perpetuals

An `Instrument` with an empty principal leg. It needs `to_call(date)`: it has no
maturity (`to_maturity` raises `UnsupportedProduct`), and its worst case is never being
called, which has no yield to a date (`to_worst` and `price_to_worst` raise). Coupons
past the known periods (resets) must be passed as `assumed_coupons`; otherwise the call
is rejected.

MOEX publishes a perpetual's yield as the **current yield**, coupon rate over clean price
(`perpetual_current_yield`), with no duration. It is not a yield to call.

## Pass-through MBS/ABS (deterministic constant CPR)

`PassThroughTerms` describe the pool from its current state: original and current face
per bond, the pass-through rate and its `AccrualBasis`, contiguous remaining
`PassThroughPeriod`s with the scheduled (zero-prepayment) principal per period,
`payments_per_year`, an optional clean-up call threshold (fraction of the original
face). `PrepaymentAssumption(cpr, exercise_cleanup_call=False)` is the only model:
a **constant annual CPR**.

Per period, with `B` the balance at the period start and `B0` the scheduled balance:

```
coupon     = B * rate * days / year            (AccrualBasis; unrounded)
scheduled  = S0 * B / B0                       (scheduled-balance ratio)
SMM        = 1 - (1 - CPR) ** (1 / payments_per_year)
prepaid    = SMM * (B - scheduled)
last period pays the remaining balance; with the clean-up call exercised, the whole
balance is paid on the first date after which it would be <= fraction * original face
WAL        = sum(t_k * principal_k) / sum(principal_k),  t in ACT/365F from settlement
```

CPR 0 reproduces the schedule exactly. `project_pass_through`, `weighted_average_life`,
`pass_through_analytics` (prepayment-adjusted effective yield, WAL, durations at a
dirty price), `cpr_sensitivity` (PV and WAL at `CPR +/- bump` with the Z-spread held;
central, or forward when `CPR < bump`) and `pass_through_instrument` (the projected
flows as a fixed bond whose `initial_face` is the current face; coupons accrue on the
rate on the projected balance, so accrued interest is the pool's).

Rate risk of a pass-through holds the CPR fixed. CPR risk is reported only by
`cpr_sensitivity`; there is no `CPR` desk factor (that would be a contract change).

## Portfolio and horizon

`value_portfolio` is unchanged: it still rejects callable and perpetual bonds
(`UNSUPPORTED_PRODUCT`, "callable bonds are not supported (package 9)") and its result
contract v2 has no field for an assumption. `value_assumed_portfolio` wraps it:

1. `prepare_assumed_book` applies an `AssumptionPolicy` (`default_exercise` for callables
   without an override; per-instrument `InstrumentAssumption` with an exercise or a
   prepayment): callables and perpetuals become their exercised bonds, pass-throughs
   their projected bonds; everything else passes through untouched. Settlement is the
   instrument's lag in its calendar; `to_worst` resolves at the snapshot quote.
2. The unchanged `value_portfolio` values the prepared book. The result
   `AssumedPortfolioResult` holds that v2 result, an `AssumptionUse` per assumed
   instrument (identifier, resolved exercise and date, WAL, the hashes of the given
   terms and of the bond valued), merged coverage over every position, totals and its
   own `result_sha256` (`verify_assumed_result` checks both hashes).

A callable without an assumption keeps its old rejection. An assumption that cannot be
applied rejects its positions with a typed code: `INVALID_TERMS` (no coupons to the
call, not a call date), `UNSUPPORTED_PRODUCT` (perpetual to maturity, non-par call),
`MISSING_MARKET_DATA` (to_worst without a quote), `MISSING_ASSUMPTION` (a pass-through
without a CPR). With no callable, perpetual or pass-through in the book the embedded
result equals `value_portfolio`'s, hash for hash.

`horizon_assumed_portfolio` does the same for `horizon_portfolio`: an exercised call is
redeemed (and carried) to its date, a pass-through amortizes at its CPR.

## Evidence

`tests/test_moex_call_goldens.py` (fixtures `tests/fixtures/moex_calls/`): MOEX
`CALLOPTIONYIELD` / `YIELDCLOSE` / `CALLOPTIONDURATION` of RU000A1087A7 and the ABS
RU000A10AST0 within 0.5 bp and one day, directly and end to end through
`value_assumed_portfolio`; the perpetual current yield of RU000A0ZZE87 and RU000A1034P7;
RU000A10B7W1 with a call added reproduces MOEX's to-maturity figures under `to_maturity`.
Analytical tests: `tests/test_callables.py`, `tests/test_pass_through.py`,
`tests/test_assumed_portfolio.py`.

## Performance

`benchmarks/assumptions_bench.py`, 2 000 positions (half callables to worst, a quarter
perpetuals to call, a quarter 5-year monthly pass-throughs at 15 % CPR) on the KBD,
Linux, Python 3.13: preparing the book 1.2 ms per position; `value_assumed_portfolio`
5.2 ms per position first order, 13.9 ms with the Hessian (the 60-flow pass-throughs
dominate; plain fixed bonds run about 9 ms with the Hessian).
