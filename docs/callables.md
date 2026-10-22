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
| `to_worst()` | the call or maturity with the lowest yield at the quoted price | resolved once, at the price, then held fixed; a perpetual's candidates are its calls and never being called |
| `never_called()` | no call, ever: a dated bond's maturity, a perpetual's endless coupons | the desk default for Russian perpetuals and callables (BACKLOG #125); see [Perpetuals](#perpetuals) |

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
- `yield_to_call`, `yield_to_maturity`, `yield_to_worst`, `yield_never_called` (a dated
  bond's yield to maturity; a perpetual's endless-stream yield, `exercise="never"`,
  without a redemption date or price).
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

- `to_maturity` (and `never_called` on a dated bond): only the calls are removed. A bond
  without calls (and without assumed coupons) comes back equal to the input, with the
  same hash;
- `never_called` on a perpetual: the calls are removed, the bond stays a perpetual with
  its listed coupons and puts, and `ExercisedBond.perpetual_tail` holds the coupons
  after them (below);
- a call: coupons up to the date, earlier amortizations, and the face outstanding
  redeemed on the date. Puts before it stay; calls go.

A call away from par cannot be written as such a bond (its redemptions would not sum to
the face), so `exercised_instrument` rejects it (`UnsupportedProduct`). The yield
functions handle any call price; the portfolio path is par calls only. The four MOEX call
bonds examined for the goldens all call at 100 %.

## Perpetuals

An `Instrument` with an empty principal leg. It takes `to_call(date)`, `never_called()`
or `to_worst()` over both; it has no maturity (`to_maturity` raises
`UnsupportedProduct`). Coupons past the known periods (resets) for a call must be passed
as `assumed_coupons`; otherwise the call is rejected.

### Never called: the endless coupon stream (BACKLOG #125)

Russian perpetuals (bank AT1 and others) are rarely called, so the desk values them by
default as never called: the listed coupons, then the **last listed coupon again, at
the same step, forever** (`perpetual.PerpetualTail(anchor_date, amount, period_days |
period_months)`, built by `perpetual_tail(periods)`):

- the step is the last period in days when the last two periods are equally long in days
  (91 / 182-day schedules), else whole months when the last two payments fall on the
  same day of the month;
- the listed coupons must run without a gap from settlement and all be known: an
  unknown coupon (amount 0, as MOEX lists a coupon not set yet) is not continued; such a
  bond usually has a put and is valued to it (`ValuationPolicy.horizon`). A perpetual
  whose coupon follows a formula (a floater, a reset on the KBD) needs its projection
  and is not continued (BACKLOG #127).

**Exact tail.** The tail is valued exactly, not truncated (a stream cut at 30 years
loses about a point of price and 12–36 bp of Z-spread at RUB rates; 50–60 years are
needed for 1 bp). Each tail coupon is discounted as any flow (the settlement-forward
zero rate in the spread convention plus the Z-spread). Discount factors are read from
the curve up to 30 years from the curve date (`TAIL_CURVE_YEARS`); beyond, where every
curve core builds is flat-forward (KBD nodes to 30 years, RUONIA OIS to 10) and every
bucket shock is flat (20Y+), they follow the curve's own flat-forward rate, read over
ten years past the 30-year point and checked at the midpoint (a curve that is not
flat-forward there is read date by date). The sum runs to 600 years past the anchor
(`TAIL_SUM_YEARS`) plus the geometric remainder of its last two terms. A stream whose
total rate is at or below zero is worth infinity; the solvers start above it.

`perpetual_yield(settlement, dirty, cashflows, tail, convention)` solves the effective
annual yield of the stream, `P = sum CF (1 + y)^-t + sum_tail a (1 + y)^-t`, with its
Macaulay and modified duration. On a coupon date with an even step it is
`((P + a) / P)^(1 / dt) - 1`, the current yield compounded over the period. Below par
it is the worst case (`to_worst` resolves to `"never"`); above par the first call is.

**Risk.** The tail is part of the base price and of every bump: bucket DV01 (the tail
past 20 years is all `20Y+`; at RUB rates 20–40 % of the DV01, the rest on 3Y–15Y),
CS01, the Hessian and scenarios reprice it exactly
(`BondRiskRequest.perpetual_tail`). At 85 % of face a 12 % coupon's Macaulay duration is
about 7.6 years against 3.8 for a call in five years.

MOEX publishes a perpetual's yield as the **current yield**, coupon rate over clean price
(`perpetual_current_yield`), with no duration. It is not a yield to call; it is the
never-called yield before compounding.

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

`value_portfolio` values callable and perpetual bonds under
`ValuationPolicy.exercise_assumptions` (BACKLOG #50, since 0.29.0) and rejects them
without one (`UNSUPPORTED_PRODUCT`). A never-called perpetual reads `horizon="perpetual"`,
`horizon_date` its last listed coupon (the tail's anchor), `exercise_assumption`
`exercise:never_called@never`, the model flag `perpetual_tail`; its effective yield and
Macaulay duration are the stream's. When the policy's `horizon` (`confirmed_put`,
`open_put`) picks a put, the perpetual is valued to the put instead. Its horizon P&L
(`horizon_portfolio`) is not modelled yet (`UNSUPPORTED_PRODUCT`, BACKLOG #126).

`value_assumed_portfolio` wraps `value_portfolio` without assumptions in the policy (and
rejects a never-called perpetual: only the native path prices its tail):

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
