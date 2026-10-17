# FX forwards and FX swaps: covered-parity value and risk (package 4)

`rub_fixed_income.fx_forwards` values outstanding OTC FX forwards and FX swap legs
against RUB by covered interest parity, and gives their RUB and foreign curve risk,
basis risk, FX delta, exact scenarios and a Hessian with FX × rates cross terms. It
also resolves FX value dates on an explicit calendar and builds an implied foreign
curve from swap points. The framework is the one of FX futures
([fx-futures.md](fx-futures.md)): explicit curves and risk bases, the 11-bucket grid,
a shared `ShockedCurveCache`, exact repricing and content identities. No I/O.

## Settlement: why this is not the futures model

An FX future pays variation margin every day, so a price change is P&L today and is
not discounted. An OTC forward pays nothing until its value date `T`: the exchange of
`N` foreign units for `N·K` RUB happens then, so its value today is discounted on the
RUB curve:

```text
PV = Σ_legs  N / q · (F(T) − K) · DF_rub(P)
F(T) = S · [DF_for(T) / DF_for(s)] / [DF_rub(T) / DF_rub(s)] · exp(b · τ(s, T))
```

- `S` is the spot rate for value date `s` (MOEX `TOM`, or `SPT`), in RUB per `q`
  foreign units; `K` is the contract rate in the same units.
- `N` is the signed foreign notional of the leg: positive = receive foreign, pay RUB.
- `P` is the payment date: the value date for a deliverable leg, or an explicit date
  for a cash-settled leg, which pays `N/q · (F(T) − K)` in RUB.
- `b` is the basis in bp, continuous on `basis_day_count` (default ACT/365F), and
  `τ(s, T)` is signed, so a leg before the spot date (TOD against a TOM spot) works.

`F(T)` is the same outright as the FX futures price for the same inputs; a test
checks that the two modules agree.

## Inputs

- `FxForwardLeg(value_date, notional_foreign, contract_rate, payment_date=None)`.
- `FxForward(trade_id, currency, quote_units, leg)`. An unsettled spot trade is a
  forward whose value date is its spot date.
- `FxSwap(trade_id, currency, quote_units, near, far)`: legs of opposite sign, the
  far one later. `swap_points` is `far.contract_rate − near.contract_rate`. Once the
  near leg has settled, value `swap.far_leg_forward()`.
- `quote_units` comes from the confirmation (1 for USD or CNY; 100 where a currency is
  quoted per 100 units). Nothing is inferred.
- `FxForwardRiskRequest(instrument, spot_rate, spot_value_date, rub_curve, foreign_curve, basis_bp, basis_day_count=ACT_365_FIXED)`.
  - `basis_bp` is required. It is `0` when the foreign curve is **implied from FX
    swaps** (`implied_foreign_curve`), because that curve already carries the whole
    cross-currency basis. With an independent foreign curve (for example a CNY OIS
    curve), calibrate it with `fx_forward_basis_bp` from a market outright.
  - Both curves share one `curve_date`, and `curve_date <= spot_value_date`.
  - A leg whose value or payment date precedes the curve date has settled and is
    rejected with `InvalidTerms`; a leg on the curve date is still outstanding.
  - Each curve is a `DiscountCurve` (`continuous_zero` risk basis) or a
    `CurveBootstrapRequest` (`ois_quote` basis, rebootstrapped under shocks), as for
    bonds and futures.

## Risk: `engine.risk(bump_bp=1, difference="forward" | "central")`

All money is signed RUB of PV.

| Field | Meaning |
| --- | --- |
| `pv_rub`, `legs` | PV; per leg the outright `forward_rate`, `forward_points = F − S`, `rub_discount_factor` and `pv_rub` |
| `fx_delta_rub_per_pct` | PV change for a +1 % relative spot move: exactly the foreign legs' RUB PV / 100, since PV is linear in spot |
| `fx_delta_foreign` | Spot-equivalent foreign amount for value date `s`: for a deliverable leg its notional discounted from `T` to `s` on the foreign curve |
| `rub_buckets`, `rub_dv01_rub`, `rub_parallel_dv01_rub` | RUB per bp per bucket, their sum, and an independently repriced parallel shift |
| `foreign_buckets`, `foreign_dv01_rub`, `foreign_parallel_dv01_rub` | The same for the foreign curve |
| `basis01_rub` | +1 bp of basis |

Signs for a long foreign forward (buy CNY, spot today): RUB DV01 `≈ +N·K·T·DF_rub(T)`
per unit rate (the RUB payment shrinks), foreign DV01 `≈ −N·S·T·DF_for(T)`, FX delta
positive. A short flips all three. When `s` is after the curve date the spot-date
discount factor adds a small opposite exposure in the bucket of `s` (usually 1M), as
for futures. Desk factors need `difference="central"` (DEC08).

## Scenarios: `engine.reprice(FxFuturesScenario(...))`

The scenario type is the FX futures one (11 + 11 bp shifts, basis shift in bp, relative
spot shift in percent), so one scenario applies to both books. The result gives the
exact total and the isolated RUB rates, foreign rates, basis and spot P&L;
`interaction_pnl_rub` is the remainder. The basis stays frozen under every shock.

## Second order: `engine.second_order(bump_bp=1)`

Gradient and full symmetric Hessian over 24 factors: `RUB 1M..20Y+`, `<CCY> 1M..20Y+`,
`basis` (bp) and `spot` (1 % relative move). Rates and basis use the central
differences of the bond and futures engines. Spot is exact: `PV(S(1+x)) = A·(1+x) − B`
with `A` the foreign legs' and `B` the RUB legs' value, so the spot diagonal is zero and
each FX × rate entry is 1 % of the central rate derivative of `A`. With the spot on the
curve date `A` does not depend on RUB rates, and the RUB × FX entries are zero.

A factor whose single bumps leave both leg values bit-identical (zero bucket weight at
every date the trade reads) is skipped in the pair loop; its entries are exact zeros
and the result equals the full evaluation (tested).

`fx_forward_factor_risk(risk, rub_curve_id, foreign_curve_id, second_order=None)`
maps the result to `IR|<rub>|<bucket>`, `IR|<foreign>|<bucket>`, `FXBASIS|<ccy>` and
`FX|<ccy>`, with the sparse upper-triangle Hessian including the FX cross terms.

## Value dates

- `joint_calendar(calendars, version=...)`: a day is a business day only if it is one
  in every calendar. Holidays are united, weekend working days intersected, covered
  ranges intersected. CNY/RUB at MOEX settles on the Russian and Chinese calendars
  together (see the golden below).
- `fx_value_date(trade_date, code, calendar, *, spot_lag_days=None, business_day_convention=MODIFIED_FOLLOWING)`:
  `TOD` (only on a settlement day, else `InvalidTerms`), `TOM` (+1 business day),
  `SPT` (+2), or a `<n>D/W/M/Y` tenor from the spot date `spot_lag_days` business days
  after the trade (1 for MOEX `TOM…` swaps). Tenors use `resolve_tenor`'s rule
  (month-end clamping, then the convention). No end-of-month rule is applied; when a
  confirmation says otherwise, pass its dates.
- `fx_swap_value_dates(trade_date, near, far, calendar, ...)`: for example
  `("TOD", "TOM")` or `("TOM", "1M")`.

## Parity helpers and the implied foreign curve

- `fx_forward_outright(...)` and `fx_forward_basis_bp(...)` on curves.
- Money-market parity with simple rates and explicit day counts:
  `cip_outright(S, start, end, rub_rate=, foreign_rate=, rub_day_count=, foreign_day_count=)`
  `= S·(1 + r_rub·τ_rub)/(1 + r_for·τ_for)`, and its inverses
  `cip_implied_rub_rate` and `cip_implied_foreign_rate`.
- `FxSwapPointsQuote(label, near_date, far_date, points)` (far outright minus near
  outright) and `FxOutrightQuote(label, value_date, outright)` (an OTC outright, or an
  FX futures price, which equals the forward with deterministic rates).
- `implied_outrights(spot, spot_value_date, quotes)` chains the points from the spot
  and from each other, forwards and backwards (a `TOD/TOM` swap gives the TOD outright
  from a TOM spot). Two quotes fixing one date, a quote no chain reaches, or a
  non-positive outright raise `InvalidTerms`.
- `implied_foreign_curve(rub_curve=, spot_rate=, spot_value_date=, quotes=, convention=None)`
  sets `DF_for(T)/DF_for(s) = (F(T)/S)·DF_rub(T)/DF_rub(s)` at every outright date, so
  parity with `basis_bp=0` reprices every quote exactly. The outrights must reach the
  curve date so the curve is anchored at `DF = 1` (add the `TOD/TOM` swap); otherwise
  `InvalidTerms`. Log-linear DF between nodes, flat forward beyond the last;
  `model_version` is `fx-implied-logdf-v1`.

The implied curve's risk is a zero-rate overlay (`continuous_zero`): a RUB shock with
the implied foreign curve held fixed means the swap points move. Holding the points
fixed instead (rebuilding the foreign curve under RUB shocks) is not offered.

## Identity

`base_id` hashes the model version, the instrument type and terms, spot and spot
date, the content and basis of both curves, the basis and its day count. Model
versions: `rub-fx-forward-parity-risk-v1`, `rub-fx-forward-parity-second-order-v1`.

## Executable synthetic example

```python
from datetime import date

from rub_fixed_income import (
    BusinessCalendar, CurveBootstrapRequest, FxForward, FxForwardLeg,
    FxForwardRiskEngine, FxForwardRiskRequest, FxFuturesScenario, FxSwap,
    FxSwapPointsQuote, OisQuote, ShockedCurveCache, bootstrap_discount_curve,
    fx_forward_factor_risk, fx_swap_value_dates, implied_foreign_curve, joint_calendar,
)

trade = date(2026, 10, 15)
ru = BusinessCalendar(frozenset(), frozenset(), "ru-synthetic", date(2026, 1, 1), date(2027, 12, 31))
cn = BusinessCalendar(frozenset(), frozenset(), "cn-synthetic", date(2026, 1, 1), date(2027, 12, 31))
cny_rub = joint_calendar((ru, cn), version="rub-cny-synthetic")
today, tom = fx_swap_value_dates(trade, "TOD", "TOM", cny_rub)
_, tom_3m = fx_swap_value_dates(trade, "TOM", "3M", cny_rub, spot_lag_days=1)

rub = CurveBootstrapRequest(trade, (
    OisQuote("3M", date(2027, 1, 15), 0.14), OisQuote("1Y", date(2027, 10, 15), 0.135),
))
cny = implied_foreign_curve(
    rub_curve=bootstrap_discount_curve(rub), spot_rate=12.5, spot_value_date=tom,
    quotes=(FxSwapPointsQuote("TODTOM", today, tom, 0.0047),
            FxSwapPointsQuote("TOM3M", tom, tom_3m, 0.38)),
)
swap = FxSwap("SWAP-1", "CNY", 1.0,
              FxForwardLeg(tom, -10_000_000, 12.5), FxForwardLeg(tom_3m, 10_000_000, 12.88))
cache = ShockedCurveCache()
engine = FxForwardRiskEngine(
    FxForwardRiskRequest(swap, 12.5, tom, rub, cny, basis_bp=0.0), curve_cache=cache
)
risk = engine.risk(difference="central")
stress = engine.reprice(FxFuturesScenario.parallel(rub_shift_bp=200, fx_spot_shift_pct=-10))
desk = fx_forward_factor_risk(risk, "RUB_OIS", "CNY_FX_IMPLIED", engine.second_order())
```

## Validation

`tests/test_fx_forwards.py` (synthetic, independent closed forms):

- PV against a leg-by-leg closed form with flat curves; at-market forward and swap at
  zero; a swap as the sum of its legs; spot after the curve date; quote units;
  cash-settled payment dates; agreement with the FX futures price;
- basis calibration, its freeze, basis day count;
- signs by side, the bucket at a pillar, bucket sums against parallel shifts, the
  exact forward difference, bump convergence (error ratio 4 per halving);
- FX delta against the foreign leg's PV and an exact +1 % scenario;
- exact scenarios against closed forms, with interaction;
- the Hessian against analytic second derivatives, including FX × foreign and
  FX × RUB cross terms; a Taylor check of a joint move; the pruned Hessian against
  the full central-difference helper (bit-identical, zero and OIS bases);
- OIS bucket risk against an independent rebootstrap, cache reuse;
- the implied curve: chaining, exact round trip of every quote, rejections;
- money-market parity round trips; value dates on a joint calendar; identity; input
  checks.

`tests/test_moex_fx_swap_goldens.py` (MOEX ISS, `tests/fixtures/moex_fx_swaps`, see
its README):

- the CNY/RUB TOD/TOM period of every `SRATE_CNY_ON` publication from 2026-06-01 to
  2026-10-16 (91 periods, including the 8-day period over China's National Day) runs
  on the joint RU + CN calendar; TOD is absent exactly on the Chinese holidays; the
  board's `SETTLEDATE`s for TOD, TOM, SPT and the TOM swaps match `fx_value_date`;
- MOEX's published implied RUB rate (`rateprc`) is reproduced from its published
  points by `cip_implied_rub_rate` on ACT/365F without a CNY rate: mean error -1.0 bp,
  median absolute 3.4 bp, maximum 22 bp with the day's TOD WAP as spot (the spot behind
  the indicator is not published). ACT/360 (mean -18 bp) and a 1 % CNY rate
  (about +100 bp) are rejected by the same data;
- the reverse direction and an implied CNY curve on which a leg struck at MOEX's
  points is at market.

The scatter of single days is the indicator's unpublished spot and window, not core
error; the methodology text (`fs.moex.com`) is not reachable from the build
environment.

## Performance

`benchmarks/fx_forward_bench.py --positions 2000`: synthetic CNY/RUB book (60 %
forwards, 40 % swaps, 1 week to 2 years) on one RUB curve and one implied CNY curve,
shared cache. Linux x86_64, Python 3.11, ms per position:

| RUB curve | first order | + Hessian | + Hessian + 5 scenarios |
|---|---|---|---|
| discount factors (`continuous_zero`) | 0.52 | 1.27 | 1.60 |
| 17-tenor OIS quotes (`ois_quote`) | 0.58 | 1.78 | 2.50 |

## Out of scope

- **Points-fixed RUB risk** (rebuilding the implied foreign curve under RUB shocks).
- **NDF fixing conventions** (fixing source, fixing date and its lag): the caller passes
  the fixing's value date as `value_date` and the payment date explicitly.
- **Known fixings** of cash-settled legs after the fixing date (the leg is projected
  from parity, not from a published fixing).
- **Portfolio integration.** `value_portfolio` and the v2 result contract cover bonds;
  FX forwards need instrument, snapshot and coverage support there (see the release
  notes).
