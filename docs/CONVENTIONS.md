# Conventions

All conventions are **inputs**. Defaults reproduce rub-fixed-income-core 0.2.1 exactly.
Units: rates and spreads are **decimals** in function APIs (`0.0125` = 125 bp) unless the
name says `_bp` or `_pct`; cash amounts are **RUB per bond**; clean prices are **% of current
nominal**; times are year fractions.

## Day counts (`DayCount`)

| Member | Year fraction |
| --- | --- |
| `ACT_365_FIXED` (`"ACT/365F"`, default everywhere) | `days / 365` |
| `ACT_360` | `days / 360` |
| `ACT_ACT_ISDA` | Σ over calendar years of `days_in_year_segment / (366 if leap else 365)` (used by the KRS module) |

`ACT/ACT-ICMA` is **not implemented** (needs a reference coupon period; see BACKLOG).
`dates.year_fraction` remains ACT/365F and is kept for compatibility.

## Compounding (`Compounding`)

`Compounding(m)` with `m >= 1` periods per year, or `m = 0` = continuous. Constants:
`ANNUAL` (1), `SEMI_ANNUAL` (2), `QUARTERLY` (4), `MONTHLY` (12), `CONTINUOUS` (0).

- periodic: `DF = (1 + r/m)^(-m·t)`, `r = m·(DF^(-1/(m·t)) − 1)`
- continuous: `DF = e^(−r·t)`, `r = −ln(DF)/t`

## Business days

`BusinessCalendar(holidays, working_days, version)` – weekends are non-business unless listed
in `working_days`; `holidays` are non-business. **The library ships no holiday list.** The
default calendar is weekends-only (`version="weekends-only"`). Production callers must build
the calendar from an official versioned MOEX/CBR source (see BACKLOG).

`BusinessDayConvention`: `UNADJUSTED`, `FOLLOWING`, `MODIFIED_FOLLOWING`, `PRECEDING`;
`adjust_date(d, bdc, calendar)` applies one.

`SettlementConvention(lag_business_days=1, calendar=...)` →
`settlement_date(trade_date) = calendar.add_business_days(trade_date, lag)`. T+1 must not be
computed as `trade_date + 1 calendar day` or weekday-only.

Identifiers include `calendar.version` **and** `calendar_fingerprint(calendar)` (hash of the
holiday/working-day sets), so two calendars with the same label but different content are
distinguishable in provenance.

**Covered range (0.9.0).** `BusinessCalendar(..., valid_from, valid_to)` declares the dates
the calendar actually knows. Outside that range every business-day query raises
`CalendarRangeError`; it is never assumed Mon–Fri. The hub publishes the range with each
calendar version, and MOEX weekend trading sessions are **not** business days. For a bounded
calendar the fingerprint also covers the range. `calendar_identity(calendar)` returns
`(version, fingerprint)` for recording in results.

## Schedules

- Legacy `dates.annual_schedule(start, end)`: forward annual roll from `start`, unadjusted,
  final period ends at `end`. If `end` was business-day adjusted past an anniversary it
  creates a short stub: `annual_schedule(2026-10-15, 2028-10-16)` →
  `(2027-10-15, 2028-10-15, 2028-10-16)`. Kept unchanged for compatibility.
- `generate_schedule(start, end, ScheduleConvention(...))`: unadjusted roll dates
  (`roll="forward"` from `start`, or `"backward"` from `end`), adjusted with the BDC on the
  calendar, any roll on/after `end` merged into the final period. Default
  `ScheduleConvention()` = 12M forward, MODIFIED_FOLLOWING, weekends-only →
  `(2027-10-15, 2028-10-16)` for the example above. `UNADJUSTED` forward reproduces
  `annual_schedule`.

## Discount curve (`CurveConvention`, `DiscountCurve`)

- Nodes `(date_i, DF_i)`, `DF(curve_date) = 1`.
- **Interpolation**: log-linear in DF by calendar days:
  `ln DF(t) = ln DF_L + (t − t_L)/(t_R − t_L) · (ln DF_R − ln DF_L)`.
- **Extrapolation** beyond the last node: flat forward from the last two nodes (or from the
  curve date for one node): `f = −(ln DF_n − ln DF_{n−1}) / τ(n−1, n)`,
  `DF(t) = DF_n · e^(−f · τ(n, t))`.
- `τ` = `CurveConvention.time_day_count` (default ACT/365F); also used by zero/forward rates
  and flat curves. Interpolation weights are calendar days regardless of day count.
- `zero_rate(t, m)` from curve date; `m` an int ≥ 1 or a `Compounding` (incl. `CONTINUOUS`).
- `forward_rate(s, e) = (DF(s)/DF(e))^(1/τ) − 1` (annual).

### OIS bootstrap (`bootstrap_discount_curve`)

Par condition for each quote `K` with fixed-leg dates `T_1..T_n`:
`K · Σ α_i · DF(T_i) = 1 − DF(T_n)`, `α_i` = `CurveConvention.fixed_leg_day_count`
(default ACT/365F). Fixed-leg dates: `fixed_leg_schedule=None` → legacy `annual_schedule`
from the curve date (default, unchanged numbers); or a `ScheduleConvention` → adjusted,
stub-free `generate_schedule`. Sequential: each new node's DF is solved via a constant
continuous forward from the previous node; result must reprice all quotes within 1e-7 bp.

## Yield

`solve_nominal_annual_yield`: `P_dirty = Σ CF_i / (1 + y/m)^(m·t_i)`, `t_i` from settlement.
Default `m = 1`, `t = days/365` (`day_count_basis=365.0`). Alternatives: keyword
`compounding_frequency`, `day_count_basis` (legacy) **or** `convention=YieldConvention(...)`
(not both). Root must satisfy `y > −0.99·m`, otherwise "not bracketed".

`solve_effective_annual_yield` (forward horizon): `P = Σ CF_i/(1+y)^{t_i}`, ACT/365F.

`bond_duration`: Macaulay `D = Σ t_i·PV_i / Σ PV_i`; modified `D / (1 + y/m)`.

## Z-spread

Desk standard: **annual (m = 1) spread compounding, ACT/365F**; monthly (m = 12) supported.

Periodic (`solve_z_spread`, `price_cashflows`):

```
DF_s(t) = DF(t) / DF(s)                       # settlement-forward discount factor
z_s(t)  = m · (DF_s(t)^(−1/(m·τ)) − 1)         # τ = day_count(s, t)
P       = Σ CF_i · (1 + (z_s(t_i) + Z)/m)^(−m·τ_i)
```

Continuous (`solve_continuous_z_spread`, or `ZSpreadConvention(compounding=CONTINUOUS)`):

```
P = Σ CF_i · DF(t_i)/DF(s) · e^(−Z·τ_i)
```

Settlement handling:

- `settlement_date=None` (default): `s = curve_date`; flows on the curve date count at face,
  earlier flows ignored (legacy behaviour).
- `settlement_date=s` (`s >= curve_date`): flows on/before `s` are dropped; result is identical
  (to 1e-12) to rebasing the curve to `s` manually, and identical bit for bit to the legacy path
  when `s == curve_date`.

The continuous and annual spreads differ materially (OFZ 26238 probe: annual 139.1 bp, monthly
124.2 bp, continuous 122.9 bp). Always record the convention identifier
(`ZSpreadConvention.identifier`, e.g. `zspread:annual:ACT/365F`) next to stored spreads.

## Accrued interest

`accrued_interest_rub(terms, s) = coupon · (s − start)/(end − start)` for
`start <= s < end`; **0 at `s == end`** (coupon date: the buyer does not receive that
coupon; MOEX convention). `analyze_bond` rounds AI half-up to `CalculationPolicy.money_quantum`
(default 0.01 RUB).

### Accrued interest from split cashflows (0.10.0)

`accrued_interest(cashflows, settlement)` and `Instrument.accrued(settlement)` use the
coupon whose `[accrual_start, accrual_end)` contains the settlement date:
`coupon * elapsed_days / period_days`, rounded half-up to 0.01 per bond. Settlement on the
period end gives 0. A settlement before the first accrual period is rejected
(`InvalidTerms`); one after the last coupon gives 0.

### Rate-based accrued interest (0.17.0)

MOEX accrues bonds by two rules, and they can differ by a kopeck when the coupon amount
was rounded:

| Bonds | Rule | Core type |
|---|---|---|
| OFZ (Minfin) | `amount * elapsed / period days` | `FixedCouponLeg` |
| Corporate bonds | `face * rate * elapsed / 365` | `RateAccrualCouponLeg(..., AccrualBasis.RATE_ACT_365)` |
| Issues with ACT/360 terms | `face * rate * elapsed / 360` | `AccrualBasis.RATE_ACT_360` |
| Replacement bonds of USD eurobonds | `face * rate * days30 / 360`, 30/360 bond basis | `AccrualBasis.RATE_30_360` |

`face` is the face outstanding at the period start, after any amortization paid on or
before it. `rate` is the decimal annual rate of the period (`RateCouponPeriod.rate`, the
bondization `valueprc` / 100). The result is rounded half-up to 0.01. The coupon paid is
always the period's `amount`. Evidence: `tests/fixtures/moex_eod/README.md`.

## `analyze_bond` conventions

- `analyze_bond` (V1, frozen): yield = effective annual ACT/365F; Z = **continuous** ACT/365F
  over settlement-forward DFs (`V1_YIELD_CONVENTION`, `V1_Z_SPREAD_CONVENTION`). Kept to
  preserve V1 result hashes.
- `analyze_bond_v2(..., yield_convention=None, z_spread_convention=None)`: default yield
  annual ACT/365F, default Z **annual** ACT/365F; the result carries `yield_convention` and
  `z_spread_convention` identifiers and its own hash. Reproduce V1 spreads with
  `z_spread_convention=ZSpreadConvention(compounding=CONTINUOUS)`.

## KBD (MOEX G-curve) (0.11.0)

`G(t) = B1 + B2·L + B3·(L − e^(−t/T1)) + Σ G_i·exp(−((t − a_i)/b_i)²)`, `L = T1/t·(1 − e^(−t/T1))`,
`a_1 = 0`, `a_2 = 0.6`, `a_(i+1) = a_i + 0.6·1.6^(i−1)`, `b_1 = 0.6`, `b_(i+1) = 1.6·b_i`.
`G` is a continuously compounded zero rate in bp; MOEX publishes `exp(G/10⁴) − 1`. Time `t`
is ACT/365F years from the trade date. The discount curve is `DF(t) = exp(−G(t)/10⁴·t)` on
the node grid, log-linear between nodes, flat-forward beyond 30Y. G-spread is the bond's
effective annual yield minus the KBD yield at an explicit tenor (the portfolio uses the
Macaulay duration at that yield).

## Desk risk factors (0.11.0)

`IR|<curve_id>|<bucket>` (1 bp at one of the 11 bucket pillars), `CS` (1 bp of own credit or
OFZ spread), `FX|<ccy>` (1 % against RUB) and `FXBASIS|<ccy>` (1 bp of a futures basis).
Portfolio results report first order and the sparse upper-triangle Hessian from central
differences with the same bump (DEC08). Signs: DV01 is PV(+1 bp) − PV(0), negative for a
long bond.

## Tenor codes (0.16.0)

`resolve_tenor` maps a market tenor code to dates on a business calendar:

- **Spot.** The spot date is `spot_lag_days` business days after the trade date. TOM is 1.
- **`<n>D/W/M/Y` tenors** run from spot by calendar days, weeks or months; month ends
  clamp. The end date is then adjusted with the business-day convention (default
  modified following).
- **Overnight codes:**
  - `ON` runs from the trade date to the next business day;
  - `TN` runs from T+1 to T+2;
  - `SN` runs from spot to the next business day.

KEYRATE swaps resolve their dates through it, unchanged. RUONIA OIS quotes still enter
the bootstrap as maturities (`ois_quotes` builds them). The bootstrap has no spot or
payment lag until the exchange specification is confirmed (BACKLOG #37).

## Horizon valuation: carry and rolldown (0.14.0)

Start at settlement `S0` of business date `d0`; horizon at settlement `S1` of `d1` (the
instrument's settlement lag in its calendar). The Z-spread is held.

- **Received:** coupons and principal paid in `(S0, S1]`, as cash (no reinvestment).
- **Carry** = `PV(S1 | today's curve read forward from S1) + received - PV(S0)`: the return
  if today's forwards are realised. For a zero-coupon bond on a flat continuous curve
  with a continuous spread it is `PV(S0) * (exp((r + s) * h) - 1)`, `h` in ACT/365F.
- **Rolldown** = `PV(S1 | rolled curve) - PV(S1 | forwards)`. The rolled curve keeps
  today's shape in calendar days: `DF_rolled(d1 + k) / DF_rolled(d1) = DF(d0 + k) / DF(d0)`.
  It is zero on a flat curve and positive for a long bond on an upward-sloping curve.
- **Explain** to an end curve (dated `d1`) and end spread `s1`:
  - `rates = V(end curve, s0) - V(rolled, s0)`;
  - `spread = V(rolled, s1) - V(rolled, s0)`;
  - `residual` is the cross term.

  The parts sum exactly to `V(end, s1) + received - PV(S0)`.
- **Put bonds:** `redemption_horizon="confirmed_put"` values to the first confirmed put
  (yield-to-offer), as the desk quotes them.

## Financed-bond accumulation

`constant_daily_accumulation_periods(start, horizon, boundaries, r)`: factor per period
`(1 + r/365)^(days)`. `project_financed_bond`: `B_{k+1} = B_k · factor_k − cash_event_k`,
starting from the initial dirty cash; cash events must terminate accumulation periods.

## KRS (MOEX IRS KEYRATE)

TOM effective date (`calendar.add_business_days(trade, 1)`), MODFOL adjustment, ACT/ACT ISDA,
annual fixed (single payment up to 1Y) vs quarterly key-rate-average floating. Convention id
`moex-irs-keyrate-tom-modfol-actactisda-v1` (unchanged; now uses `DayCount.ACT_ACT_ISDA`).

## Examples

```python
from rub_fixed_income import (
    MONTHLY, CONTINUOUS, Compounding, BusinessCalendar, SettlementConvention, ZSpreadConvention,
    YieldConvention, DayCount, solve_z_spread, solve_nominal_annual_yield,
)

cal = BusinessCalendar(holidays=frozenset(official_holidays), version="moex-2026-v3")
settle = SettlementConvention(lag_business_days=1, calendar=cal).settlement_date(trade_date)

z_annual = solve_z_spread(flows, dirty, curve, settlement_date=settle)            # desk default
z_monthly = solve_z_spread(flows, dirty, curve, settlement_date=settle,
                           convention=ZSpreadConvention(compounding=MONTHLY))
z_cont = solve_z_spread(flows, dirty, curve, settlement_date=settle,
                        convention=ZSpreadConvention(compounding=CONTINUOUS))
ytm_sa = solve_nominal_annual_yield(dirty_price=dirty, settlement_date=settle, cashflows=flows,
                                    convention=YieldConvention(compounding=Compounding(2)))
```
