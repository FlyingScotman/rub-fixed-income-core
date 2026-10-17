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

With `CurveConvention(ois_swap=...)` the quotes are spot-starting swaps instead; see
[RUONIA OIS, exchange specification](#ruonia-ois-exchange-specification-rub_ruonia_ois_moex_v2).

## Yield

`solve_nominal_annual_yield`: `P_dirty = Σ CF_i / (1 + y/m)^(m·t_i)`, `t_i` from settlement.
Default `m = 1`, `t = days/365` (`day_count_basis=365.0`). Alternatives: keyword
`compounding_frequency`, `day_count_basis` (legacy) **or** `convention=YieldConvention(...)`
(not both). Root must satisfy `y > −0.99·m`, otherwise "not bracketed".

`solve_effective_annual_yield` (forward horizon): `P = Σ CF_i/(1+y)^{t_i}`, ACT/365F by
default; `YieldRequest(..., convention=YieldConvention(day_count=...))` sets another time
basis (annual compounding only).

### Yield time basis is an issue term (BACKLOG #38)

The time basis of a quoted yield is independent of the accrual basis. MOEX quotes most
bonds effective annual on ACT/365F, but some USD replacement bonds on TQOD on ACT/360
while they accrue 30/360 (RU000A105RH2 and six others in `tests/test_moex_eod_goldens.py`).
ISS does not publish which, so the backend takes it from the issue terms and sets
`Instrument.yield_convention` (default `YieldConvention()`, effective annual ACT/365F,
`DEFAULT_YIELD_CONVENTION`). Core never infers it. `value_portfolio` reports
`effective_yield` and `macaulay_duration_years` on that basis (ACT/360 years are
`days / 360`; Macaulay duration in days does not depend on the basis). The G-spread to
the KBD always uses the ACT/365F yield and duration, the KBD's own basis. Left at the
default, the field is not serialized, so instruments and their hashes are unchanged.

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
OFZ spread), `FX|<ccy>` (1 % against RUB) and `FXBASIS|<ccy>` (1 bp of an FX futures or forward basis).
Portfolio results report first order and the sparse upper-triangle Hessian from central
differences with the same bump (DEC08). Signs: DV01 is PV(+1 bp) − PV(0), negative for a
long bond. Inflation-linked bonds (package 9a) carry `REAL|<curve_id>|<bucket>` (1 bp at a
pillar of a real discount curve) instead of `IR|…`; it appears only when a linker is
computed.

## Non-RUB bonds and FX (package 3)

- **FX rate.** `FxRate.rub_per_unit` is RUB per one unit of the currency, from the
  snapshot. `FX|<ccy>` is a relative 1 % move: the rate goes to `FX · 1.01`.
- **Curve.** A CNY, USD or EUR bond is discounted on the curve that
  `ValuationPolicy.foreign_curves` names for its currency (`curve_overrides` win for an
  instrument). The curve's spec must be of that currency: `cny_/usd_/eur_discount_factors_v1`,
  a caller-built `DiscountCurve` (log-linear DF nodes, model `ois-logdf-v1`, ACT/365F,
  dated the snapshot business date). Core registers no foreign bootstrap.
- **Valuation.** The bond is priced in its currency, with the spread calibrated to its
  quote on its own curve. Every `_rub` amount is the currency amount times `FX`:
  `PV_rub = FX · PV_ccy`; DV01, CS01, the Hessian and JTD likewise.
  `PositionValuation.currency_amounts` keeps nominal, accrued, dirty price and PV in the
  currency. Accrued interest in RUB is not rounded again.
- **FX delta and cross terms.** `FX delta = PV_ccy · FX / 100` RUB per 1 %. The cross
  term of FX with a rates or spread factor is that factor's RUB first order divided by
  100, so for a long bond it has the sign of its DV01 (negative). The FX gamma is zero:
  the RUB value is linear in the rate. These are the exact central differences with a
  1 % FX bump and the rates bump, so first order and Hessian stay consistent (DEC08).
- **Scenarios.** `PortfolioScenario.fx_shocks_pct` moves listed currencies by a percent;
  a position in one is revalued as `FX · (1 + s / 100) · PV_ccy(shocked curve)`. The
  bucket shifts apply to every position's own discount curve, foreign ones included.
- **Not covered.** FX carry and horizon valuation of non-RUB bonds (`horizon_portfolio`
  rejects them), quanto or FX-linked payments, a cross-currency basis, other currencies.

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

KEYRATE swaps resolve their dates through it, unchanged. RUONIA OIS quotes enter the
bootstrap as maturities (`ois_quotes` builds them). The legacy convention has no spot or
payment lag; `rub_ruonia_ois_moex_v2` (below) resolves each quote's tenor again and
checks it against the maturity.

## RUONIA OIS, exchange specification (`rub_ruonia_ois_moex_v2`)

`ois_curve_convention("rub_ruonia_ois_moex_v2", calendar)` gives
`CurveConvention(ois_swap=OisSwapConvention(calendar))`. Each quote is the par fixed rate
of the MOEX standard OIS RUONIA traded on the curve date `T`.

**Sources** (retrieved 2026-10-17; URLs and SHA-256 in
[`docs/sources/ruonia_ois_moex_v2/README.md`](sources/ruonia_ois_moex_v2/README.md); the
documents are not redistributed):

1. MOEX, «Методика формирования ценовых кривых СПФИ», version of 25 December 2024,
   <https://www.moex.com/ru/documents/27002>. Clause (2) lists the RUB-OIS-RUONIA tenors
   (1W 2W 1M 2M 3M 6M 9M 1Y 2Y…10Y, 17 in all); Appendix 1 «Описание спецификаций»,
   column «OIS RUONIA», states the swap terms quoted below. The document calls itself
   informational («носит ознакомительный характер»).
2. NFA (СРО НФА), «Изменения № 1 в Стандартные условия срочных сделок на процентные ставки
   и сделок свопцион 2011 г.», published 17 August 2021, clause 8.4(б)
   «RUB-RUONIA-OIS-COMPOUND» (the index formula), and the 2011 Standard Terms themselves:
   2.2 (interest periods: start inclusive, end exclusive), 7.2 and 7.3(а) (fixed and
   floating amounts = notional × rate × day-count fraction), 7.4(е) (Фактическое/
   Фактическое = Act/Act ISDA), 7.8(д) added by the 2021 amendments (a lookback applies
   only if the parties agree one).

| Term (MOEX Appendix 1, OIS RUONIA) | Text | Core |
| --- | --- | --- |
| Дата сделки / Начало | T+0 / Дата сделки + TOM | `spot_lag_business_days=1` |
| Окончание | Начало + Срок | `resolve_tenor` from spot |
| Конвенция (начало, окончание, периоды) | MODFOL | `MODIFIED_FOLLOWING` on start, end and roll dates |
| Календарь рабочих дней | SPFI, MOSCOW, RUONIA | one caller-built joint `BusinessCalendar` |
| Процентный период (fixed and floating) | ≤1Y – в конце срока; >1Y – ежегодно | one period up to 12 months, else 12M periods |
| Конвенция Stub | Short Initial | roll back from the unadjusted end; stub first |
| Конвенция дней (fixed and floating) | Act/Act (ISDA) | `DayCount.ACT_ACT_ISDA` |
| Смещение для Даты платежа (both legs) | +1B | payment = adjusted period end + 1 business day |
| Индекс ставки | RUONIA-OIS-COMPOUND | NFA 8.4(б) formula below |
| Смещение фиксинга / Сдвиг (Lookback/Shift) | 0B / не указан | none |
| Спред, Доп. платеж, Ставка первого периода | не указан | none |

**Par rate.** Periods `[s_i, e_i)` paid on `p_i`:
`K · Σ α_i · DF(p_i) = Σ (DF(s_i)/DF(e_i) − 1) · (Act_i / d_i) · β_i · DF(p_i)`, with `α_i`,
`β_i` the Act/Act ISDA fractions of the fixed and floating periods, `d_i` the calendar days
of the period and `Act_i` = 366 if any day of `[s_i, e_i)` falls in a leap year, else 365.
This is NFA 8.4(б): the index is `(Π(1 + RUONIA_i · n_i / Act) − 1) · Act / d`, and the
compounded overnight growth over a period is `DF(s)/DF(e)` on a single curve. Inside one
calendar year `(Act/d) · β = 1`; across a leap-year boundary it is not (for example
1.00219 for 19.10.2028–19.10.2029). The 0.0001 % rounding of the index applies to settled
fixings, not to projections.

**Bootstrap.** Nodes stay at the quote maturities (log-linear DF, flat-forward
extrapolation, ACT/365F curve time, as the legacy spec). A quote's last payment is a
business day after its node, so the sequential solve is repeated with the other nodes
held until every quote reprices within 1e-9 bp (then the usual 1e-7 bp check).

**Not covered by the texts above** (not implemented as conventions): the NCC
specification «Спецификация внебиржевых договоров «Процентный своп»», which governs the
cleared contracts, was not reachable (NCC hosts refused by the build proxy); its
RUONIA-OIS-COMPOUND formula is assumed to be the NFA one only through the shared index
name. The joint SPFI/MOSCOW/RUONIA holiday list is the caller's. Live RUB-OIS-RUONIA
values (`/iss/sdfi/curves/RUB-OIS-RUONIA`) are for ISS subscribers only, so the tests use
synthetic quotes.

## FX forwards, swaps and value dates (package 4)

- **Value dates.** `TOD` = the trade date, which must be a settlement business day;
  `TOM` and `SPT` = 1 and 2 business days later; tenors run from the spot date
  (`spot_lag_days` explicit: 1 for MOEX `TOM` swaps) with `resolve_tenor`'s month-end
  clamping and business-day convention, without an end-of-month rule. The calendar is
  the pair's settlement calendar: `joint_calendar` makes a day a business day only if
  it is one in every input calendar. CNY/RUB at MOEX settles on the Russian and
  Chinese calendars together (TOD does not trade on Chinese holidays; TOM then settles
  on the next joint business day).
- **Units.** Spot, outrights, contract rates and swap points are RUB per `quote_units`
  foreign units; notionals are signed foreign units (+ = receive foreign).
- **Parity.** `F(T) = S · [DF_for(T)/DF_for(s)] / [DF_rub(T)/DF_rub(s)] · exp(b·τ(s, T))`,
  `b` continuous in `basis_day_count` (default ACT/365F), `τ` signed. Money-market
  parity (`cip_*`): `F = S·(1 + r_rub·τ_rub)/(1 + r_for·τ_for)`, simple rates, both day
  counts explicit. MOEX's `SRATE_CNY_ON` implied RUB rate is this formula on ACT/365F
  with no CNY rate term (established from ISS data, see
  `tests/fixtures/moex_fx_swaps/README.md`).
- **Settlement.** A forward leg's P&L is paid on its value date (or explicit payment
  date) and discounted on the RUB curve: `PV = N/q · (F − K) · DF_rub(P)`. FX futures
  margin is daily and undiscounted. Legs before the curve date have settled and are
  rejected; legs on it are outstanding.
- **Risk.** Buckets, shocks and signs as for bonds; the spot factor `FX|<ccy>` is a 1 %
  relative move and is exact (PV is linear in spot), and the Hessian carries FX × rates
  cross terms. An implied foreign curve has zero basis by construction.

## Interest-rate swaps (package 2)

`SwapConvention` holds every swap convention (or `swap_convention(spec_id)` derives it
from a registered spec); see [swaps](swaps.md).

- **Schedule.** Unadjusted roll dates `backward` from the unadjusted termination date or
  `forward` from the effective date (month ends clamp), each adjusted with the business-day
  convention, the termination included; `legacy-annual-forward` is `annual_schedule`.
  `single_period_up_to_months` gives one period to short swaps (KEYRATE fixed leg up to
  1Y). Payment = accrual end + payment lag in business days.
- **RUONIA float**: `N * (prod(1 + r_i * tau_i) * DF_p(c) / DF_p(e) - 1 + spread * alpha)`, `tau_i` the days a fixing applies in `rate_day_count`.
  Each day takes the latest business-day fixing on or before it (no lookback); fixings
  observed before the valuation date are known, the rest are the projection curve's
  compounded overnight forwards from `c` to the accrual end `e`.
- **Key-rate float**: `N * (calendar-day average + spread) * alpha`; days before the
  valuation date from the history, later days from the `KeyRateForwardCurve`.
- **Spread** is added simply over the float accrual factor, not compounded.
- **Valuation date** = the curves' date; flows paid on or before it are excluded.
  Accrued = earned and unpaid at the valuation date.
- **Signs.** Payer pays fixed. DV01 = central difference per curve role, positive for a
  payer; `krd_years = -dv01 / notional * 1e4`. Key-rate projection shifts move the
  forward key rate after the current-rate interval by the bucket hats at each day's
  ACT/ACT ISDA time.

| Spec | Swap convention |
| --- | --- |
| `rub_key_rate_swap_v1` | spot 1, MODFOL, ACT/ACT ISDA; fixed 12M backward, single period up to 12M; float 3M backward arithmetic; no payment lag |
| `rub_ruonia_ois_legacy_v1` | spot 0, unadjusted, ACT/365F; both legs legacy annual; RUONIA compounded ACT/365F; no payment lag |

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

## Inflation-linked bonds: OFZ-IN (package 9a)

From the Minfin terms (order No. 80n of 22.05.2015) as restated by the Bank of Russia
(January 2021 methodological comment), each confirmed on MOEX data
([linkers.md](linkers.md), [sources/ofz_in](sources/ofz_in/README.md)):

- **Reference index** on day `n` of month `M` with `d` days:
  `INDEX = CPI(M-4) + (CPI(M-3) - CPI(M-4)) * (n-1)/d`. This is a 3-month lag: the face
  grows during month `M` by the CPI change of month `M-3`. `CpiIndexation(lag_months=3)`.
- **Face** `N = initial * INDEX(date) / INDEX(placement date)`, in kopecks (half up; the
  rounding is not confirmed from the text). A published `INDEX_base` is an explicit term.
- **Coupon** `round(N(payment date) * rate * days / 365, 0.01)`; **accrued**
  `round(N(date) * rate * elapsed / 365, 0.01)`, on the face of the same date. MOEX
  `ACCINT` is on the trade date with the trade-date face, and `FACEVALUE` in the history
  is the trade-date face.
- **Redemption** `max(N(maturity), initial face)` (`floor_at_initial_face=True`).
- **Real yield** (MOEX `YIELDCLOSE` for OFZ-IN): effective annual ACT/365F yield at
  settlement of the flows at the settlement-date face held constant (`real_cashflows`);
  Macaulay duration on the same flows (`DURATION`, days).
- **Real curve** `rub_real_discount_factors_v1`: caller-built RUB real discount factors,
  ACT/365F, log-linear DF. Risk factor `REAL|<curve_id>|<bucket>`.

## Callables, perpetuals and pass-throughs (package 9b)

- **Call schedule:** `Offer(kind=CALL)` dates; `price_pct` is % of the face outstanding
  just before the date. Discrete dates only.
- **Exercise assumption** (explicit, recorded): `to_maturity` (default; MOEX's
  treatment without a set call date), `to_call(date)` (MOEX's `CALLOPTIONYIELD` when the
  call date is set), `to_worst` (lowest yield at the quoted price; ties to the earliest
  date; resolved once, then held under risk).
- **Yields** are effective annual on the instrument's `yield_convention` (default
  ACT/365F); durations Macaulay and modified at that yield.
- **Coupons** must be known to the redemption date, which must be a coupon date; reset
  coupons are explicit `assumed_coupons`, never extrapolated.
- **Perpetuals** are priced to a named call; MOEX publishes their current yield
  (coupon rate / clean price).
- **Pass-throughs:** constant annual CPR, `SMM = 1 - (1 - CPR)^(1/payments_per_year)`;
  scheduled principal scaled by actual over scheduled balance; prepayment on the balance
  after scheduled principal; coupon `B * rate * days / year` (unrounded) on the period-start
  balance; WAL principal-weighted in ACT/365F years from settlement.

Formulas and rejections: [callables.md](callables.md).

## Credit default swaps (package 8)

ISDA CDS Standard Model; details in [cds.md](cds.md). All conventions are fields of
`CdsConvention` (no defaults); `CdsConvention.isda_standard(calendar)` is the standard
contract: premiums quarterly on 20 Mar/Jun/Sep/Dec rolled back from the unadjusted
maturity, adjusted following, ACT/360, last accrual period including the maturity date;
step-in T+1 calendar day; cash settlement T+3 business days; standard accrual start the
latest adjusted IMM date on or before step-in (full first coupon, accrued rebated);
accrual on default as in the Standard Model code (half-day bias); hazard time ACT/365F.

- Hazard piecewise constant; survival and discount factors log-linear in days between
  the merged node dates; protection from the start of the step-in day to the end of the
  maturity day; survival for a premium observed the day before its accrual end.
- Clean upfront (buyer pays at cash settlement, per unit notional)
  `U = ((1 - R) Prot - c A)/DF_cs + c α_acc`; par spread `(1 - R) Prot / (A - α_acc DF_cs)`.
- Quoted spread → upfront uses one flat hazard (the market convention); a curve uses
  one hazard per quote.
- Recovery is always an input; there is no default recovery.
- Risk: quotes held and hazard rebootstrapped under every shock; `CS` = every quoted spread
  +1 bp; IR buckets on the discount curve; recovery01 per 0.01, central.
- JTD = `side · N · ((1 - R) - c α_acc) - pv`, the bond rule's "value after minus value
  before" (DEC07): a protection seller loses on default like a bondholder.

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
