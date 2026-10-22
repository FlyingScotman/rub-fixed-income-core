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

**The KBD as a risk curve (0.34.0).** Spec `rub_ofz_kbd_v2` is the same curve with bucket
shocks of the effective annual yield MOEX publishes (`CurveConvention.zero_shock="annual"`,
`curve_basis = "annual_zero"`): `Y'(t) = Y(t) + Σ w_i(t)·s_i` on the 11 pillars and hat
weights of RUONIA. Its DV01 sums with par RUONIA OIS DV01 (they agree to 0.5–2 % on OFZ)
and equals annual Z-spread risk on the curve date. `rub_ofz_kbd_v1` keeps continuous
zero shocks, 1 + y times larger, and every stored result on it. Desk decision of
2026-10-22 (docs/proposals/kbd-risk-curve.md, option C): OFZ discounted on the KBD with
their own spread to it (`ofz_spr01` is OFZ rich/cheap to the KBD); corporates on RUONIA
OIS plus credit spread; OFZ-PK on RUONIA; KBD-linked floaters project off the KBD.
`basis_view(risk, base, curve, basis)` shows the same risk on RUONIA and the
KBD − RUONIA basis.

## Desk risk factors (0.11.0)

`IR|<curve_id>|<bucket>` (1 bp at one of the 11 bucket pillars), `CS` (1 bp of own credit or
OFZ spread), `FX|<ccy>` (1 % against RUB), `FXBASIS|<ccy>` (1 bp of an FX futures or forward basis)
and `FUTBASIS|<contract_id>` (1 bp of a rate futures basis, package 5).
Portfolio results report first order and the sparse upper-triangle Hessian from central
differences with the same bump (DEC08). Signs: DV01 is PV(+1 bp) − PV(0), negative for a
long bond. Inflation-linked bonds (package 9a) carry `REAL|<curve_id>|<bucket>` (1 bp at a
pillar of a real discount curve) instead of `IR|…`; it appears only when a linker is
computed.

A two-curve floater (package 1) carries `IR|<discount curve>|<bucket>` and
`IR|<projection curve>|<bucket>` separately, then `CS` (its spread over the discount
curve), with the full 23 x 23 Hessian including the discount/projection cross terms.
Its spread is the spread to the benchmark curves, not a quoted discount margin.

## Floating coupons (package 1)

Every convention is a term of `FloatingCouponRule`; core infers none (see
`docs/floating.md`). The MOEX-confirmed choices (`tests/test_moex_floater_goldens.py`):

| Convention | Explicit input | MOEX finding |
| --- | --- | --- |
| Lag | `ObservationShift(days, "calendar_days" \| "business_days", accrues_over)`, `lookback_business_days`, `ObservationWindow`, `FixingDateRule` | calendar days: 6 (arithmetic), 7 (compounded), 4 for some key-rate bonds; a few use a 4-business-day lookback |
| Window weights and year fraction | `accrues_over="observation"` or `"accrual"` | the shifted window gives both (ACT/ACT ISDA for OFZ-PK) |
| Non-business days | the history's calendar | the latest fixing on or before the day; the RUONIA year-end day without a fixing is a holiday of the RUONIA publication calendar |
| Key rate | rows dated by the CBR **effective date**; `observation_days="business"` or `"calendar"` | |
| Averaging | `compounded`, `arithmetic`, `single_fixing` | arithmetic "Ставка RUONIA" and key rate, compounded "Индекс RUONIA" and term RUONIA 3M |
| Rounding | `rate_rounding_decimals`, `amount_rounding_decimals` (fully fixed coupons only) | in arrears: amount to the kopeck from the unrounded rate; in advance: rate to 0.01 % first |
| Floor / cap | `CouponRateBounds(applies_to, floor, cap)` | not seen in the golden set |
| KBD index (`ZR_YLD_CRV`, 0.34.0) | `index="kbd"`, `index_tenor_years`, `single_fixing` with `FixingDateRule("accrual_start", N, "business_days")` on the ZCYC publication calendar, `rate_rounding_decimals=2`; `FloatingCouponPeriod.margin` for a margin reset at an offer | the effective annual KBD yield at 7Y (N = 5) or 5Y (N = 4) plus the margin, in advance, ACT/365F; KAMAZ listed five coupons of 2022–2024 to 0.0001 % |
| Constant-maturity convexity | `ConstantMaturityConvexity(normal_vol_bp)` (default none: the deterministic forward) | no volatility source decided yet |

**Optionality policy.** Floors and caps are priced at intrinsic value on the projected
rate, accepted only when that rate is at least the explicit `strike_guard_bp`
(`ValuationPolicy.cap_floor_guard_bp`) from every strike; otherwise
`UNSUPPORTED_PRODUCT`. Time value is not modelled.

**Point in time.** A portfolio uses a fixing history only through
`MarketSnapshot.point_in_time_fixing`: dated the business date, `known_at_utc` not after
the snapshot's `known_at`, rows published after it (`published_at`) dropped. A dropped or
missing RUONIA day is `MISSING_MARKET_DATA` (beyond the explicit carry allowance), never
filled.

**Accrued interest.** Core assumes no floater accrual convention: the quote supplies it.

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

## Rate futures (package 5)

See [rate futures](rate-futures.md) for the specifications cited.

- **Units.** RUON prices in % (`100 - rate`), RUONIA and KEYRATE index futures in index
  points, OFZ futures in RUB per lot of 10 bonds, clean. Fixings and curve rates are
  decimal. Money is RUB of variation margin per contract times the signed quantity:
  RUON `Round(W/R; 5)` per point (`W = Round(T/365 * 1e6 * 0.01%; 5)`), RUONIA index
  10,000, KEYRATE 1,000, OFZ 1 RUB per RUB of price. Margin is daily: no discounting.
- **RUONIA days.** A calendar day takes the RUONIA of the latest business day on or
  before it. A business day without a published RUONIA must be declared
  (`RuoniaFixings.unpublished_dates`); it carries the previous fixing (RUON) and is not a
  calculation day of the index (simple interest across it). Projected RUONIA is the
  curve's overnight forward on the Bank of Russia basis `d/366 + (days - d)/365`.
- **Final prices.** RUON rounds half up to 0.01, the RUONIA index futures to 4 decimals,
  KEYRATE (the index) to 0.01; theoretical prices of contracts still projecting are
  unrounded.
- **OFZ.** CF on ACT/365F at the delivery date, 4 decimals, the conversion yield an input;
  the bond spread is continuous ACT/365F over the bond curve, the carry uses the funding
  curve, the basis is continuous ACT/365F on the carry. The cheapest bond minimises
  `10 * (forward - AI(D)) / CF`. Delivery options are not valued.
- **Risk.** Central or forward bucket DV01 per curve role; `FUTBASIS|<contract_id>` for
  1 bp of the calibrated basis; `CS` for 1 bp of every OFZ basket spread. Key-rate curve
  shocks use the bucket hats at the effective date's ACT/ACT ISDA time; decided rates do
  not move.

## Cross-currency swaps (package 7)

See [xccy](xccy.md); sources in [sources/xccy_cnyrub](sources/xccy_cnyrub/README.md).

- **Legs.** Each leg is a bond in its currency: its receiver pays the initial exchange,
  receives the coupons, any amortization (intermediate exchanges, NFA 2011 6.4(д)) and
  the final exchange. Notionals are fixed by the terms; mark-to-market resets are
  rejected.
- **RUONIA float** (`RUB-RUONIA-OIS-COMPOUND`, NFA 8.4(б)):
  `N * ((prod(1 + r_i n_i / Act) * DF_p(c) / DF_p(e) - 1) * Act / d + spread) * DCF`.
  - `Act` is 365, or 366 if any day of the period is in a leap year.
  - `d` is the number of days in the period. `DCF` is the leg's day count.
  - Each day takes the latest business-day fixing on or before it: no offset, no
    lookback.
  - Fixings before the valuation date are known. From the first unobserved day `c` to
    the end `e`, the projection curve compounds.
  - A foreign index states both bases explicitly (`CompoundedIndex`).
- **Spread** is added simply, `N * (rate + spread) * DCF` (NFA 2011, 7.3(а)).
- **Valuation.**
  `PV = sum CF_rub DF_rub(P) + S DF_rub(s) / DF_for(s) * sum CF_for DF_for(P) exp(b tau(s, P))`.
  - `S` is RUB per foreign unit for value date `s`.
  - `b` is the basis in bp: continuous, `basis_day_count`, `tau` signed, with the sign
    of the FX-forward basis.
  - A final-exchange-only swap equals an FX forward.
  - Flows paid on or before the valuation date have settled.
- **`cny_ruonia_xccy_v1` (MOEX `XCS CNYRUB`).**
  - Trade T+0, start TOM, end start + tenor, MODFOL on the joint SPFI/MOSCOW/BEIJING/RUONIA
    calendar.
  - CNY fixed against RUONIA flat. A single period up to 1Y and annual beyond, short
    initial stub, Act/Act ISDA on both legs, +1B payments.
  - The RUB notional is the CNY notional at the NCC central rate of the trade date.
  - Initial exchange on the start date, final exchange on the end date: the NFA 2011
    6.2 defaults, since the NCC text was not reachable.
  - The quote is the CNY fixed par rate.
- **Bootstrap.** One node per quote at its last CNY payment date: a constant continuous
  forward from the previous node, log-linear DF in between. Every quote reprices within
  1e-7 bp.
- **Risk.** `IR|<curve>|<bucket>` per curve role, `XCCYBASIS|<ccy>` (1 bp of `b`) and
  `FX|<ccy>` (1 %, exact, `foreign leg RUB value / 100`), central (DEC08).
  - On a bootstrapped foreign curve (`xccy_quote`), RUB shocks and quote shifts rebuild
    the curve with the quotes, the spot and the notional rate held.
  - Spot moves hold the curve.

## Repo legs (package 6)

See [repo](repo.md); sources in [sources/repo_moex](sources/repo_moex/README.md).

- **Interest days** are the calendar days `i` of `(start, end]` (NFA «Срок РЕПО»), each
  weighted by `RepoDayBasis`: `ACT/365-366` (1/366 in a leap year, else 1/365: MOEX
  `T365/365 + T366/366`, NFA's RUB default), `ACT/365F` or `ACT/360` (NFA's default for
  foreign currencies). Not ACT/ACT ISDA, which counts `[start, end)`.
- **Fixed:** `S2 = S1 * (1 + R * sum_i w_i)` (MOEX Part III 1.5.8.5), rounded once to
  `amount_rounding_decimals`. **Start amount** `S1 = (1 - D/100) * Q * (P0 + a0)`
  (1.5.8.2).
- **Floating:** day `i` takes RUSFAR / RUONIA of the latest settlement day before it
  (the last published fixing on or before that day), or the key rate in force on day
  `i`; plus a simple spread. Simple: `S2 = S1 * (1 + sum_i (r_i + s) w_i)`, rounded once
  when known. Capitalised: on each settlement day the interest of its days on the
  previous balance, rounded to the kopeck when known; weekends accrue simply.
  Optional KSU rule: `r + s <= 0` gives `nonpositive_rate_replacement` (0.0001); a
  projected rate at or below zero is rejected.
- **Projection:** overnight forwards `(DF(o)/DF(next) - 1) / tau` on
  `projection_day_count`, `next` the next publication day (key rate: next calendar
  day). Observations before the valuation date are fixings, never projected.
- **PV** = `sign * sum CF * DF_discount(t)` over flows after the valuation date (start
  leg if forward, end leg, explicit adjustments). The collateral is not in it.
- **Risk:** `IR|<discount>|*` and, for a two-curve floating repo, `IR|<projection>|*`;
  central (DEC08). A lender's discount DV01 is negative.

## Portfolio result contract v3 (derivatives)

The rules the portfolio path adds; the products' own conventions are in their sections.
Full reference: [portfolio-v3.md](portfolio-v3.md).

- **When.** v3 only when a position is on a derivative position type; a bond-only book is
  v2 byte for byte. Derivative instruments that no position references do not switch
  the contract.
- **Dating.** Every curve is dated the snapshot business date, the key-rate projection
  curve included (a TOM calibration is re-dated with `rebase_key_rate_curve`, which
  extends the current-rate interval; a key-rate change effective on TOM is rejected).
  The FX spot carries its own value date (`FxSpot.value_date`) and settlement calendar;
  FX value dates must be business days of that calendar.
- **Quantity.** A multiplier of the contract as booked (futures: signed contracts).
  Repo contract amounts (S1, S2, accrued) are reported per trade.
- **Futures PV.** The pending variation margin from `margined_price` to the snapshot
  price, undiscounted; RUON margins follow the specification's rounding.
- **FX basis.** Always explicit per (currency, foreign curve) (`FxBasis`); 0 on an
  implied or XCCY-bootstrapped curve.
- **JTD.** By bond `issuer_id` / CDS reference entity; one sign rule (DEC07). A CDS uses the
  position's recovery when stated, else the credit curve's.
- **Scenarios.** Per-curve shifts win over the shock for the named curves in every
  role; `spread_shift_bp` moves `CS` of bonds, CDS quotes and OFZ futures baskets;
  `fx_shocks_pct` moves FX spots and conversions; bases are held.
- **Model flags.** Fixed vocabulary listed in [portfolio-v3.md](portfolio-v3.md); sorted.

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

### Derivatives: carry, rolldown and theta (0.30.0–0.31.0, BACKLOG #101)

Every derivative engine has `.horizon(...)` with the bond semantics above, on two views
of today's base curves (`rub_fixed_income._curve_views`, re-exported by `horizon`):

- **Forward view** `forward_curve(curve, h)`: `DF_h(t) = DF(t) / DF(h)`, today's
  forwards realised (exact up to the last node; beyond it the flat forward, exact for
  ACT/365F and ACT/360 curve time). Hazard curves: `forward_hazard_curve`, the same
  rates after `h`, so `Q_h(t) = Q(t) / Q(h)`.
- **Rolled view** `roll_curve(curve, h)`, `roll_key_rate_curve`, `roll_hazard_curve`:
  every node date moved by `h - d0`, the shape held in calendar days. This is the desk
  theta: curves (and CDS spreads per tenor) moved to the next day unchanged. A key-rate
  curve holds its CBR decision date (0.32.0, #109): the rate in force until the meeting
  stays, the tenor pillars roll, and the first segment still starts on the meeting date.
  A horizon past the meeting realises the decision at today's forward (the first
  segment's rate, current until its rolled pillar end).
- **Path to the horizon:** a floating observation before `h` (RUONIA, a repo
  benchmark, a futures day, a key rate effective before `h`) keeps today's forward on
  both views; later observations are projected on the view. Calibrated bases and
  spreads are held in bp. A key rate already decided (before `current_rate_end`) does
  not move.
- `received` = flows paid in `(d0, h]` as undiscounted cash; `carry = pv_forward +
  received - pv_start`, `rolldown = pv_rolled - pv_forward`, `horizon_pnl = carry +
  rolldown` (the theta).
- **FX spot at the horizon** (`FxHorizon`, FX forwards and swaps, XCCY, FX futures):
  - `"spot_held"` (default, sticky spot): `S_h = S0`; forward points roll down, so the
    theta of a forward is mostly its points over the days to the new spot date. Foreign
    cash received by the horizon converts at `S0`.
  - `"forwards_realised"`: `S_h` = today's outright for the horizon spot date (with the
    forward's FX basis; a future's basis is its own and is not in `S_h`). Foreign cash
    received converts at today's outright for its date, and `pv_forward` equals today's
    PV of the remaining flows divided by `DF_rub(h)`: carry is the RUB funding only.
  - The horizon spot date keeps today's lag in business days of the settlement
    calendar (`horizon_spot_date`): a Friday horizon's TOM is Monday.
- **Futures** are margined daily: `pv_start = received = 0` and carry, rolldown and
  theta are the variation margin on the model price (`quantity × margin per point ×
  ΔF`). A contract that settles inside the horizon uses the realised path (today's
  forwards) and its unrounded model price. OFZ futures are carried only to a horizon
  spot settlement before delivery.
- **CDS** amounts are conditional on no default before the horizon; `received` counts
  the coupons that leave the contract's value between today's step-in date and the
  horizon's (`h + step_in_days`), so no coupon is lost or counted twice on the step-in
  boundary. A non-RUB CDS converts at the snapshot FX rate and is carried with
  `"spot_held"` only (its position names no RUB curve for a forward rate).
- **Repo**: `pv_rolled` reprices the repurchase amount with the post-horizon
  observations on the rolled projection curve and discounts on the rolled curve; the
  accrual P&L (`interest_carry`) is reported beside it.

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
  date; resolved once, then held under risk), `never_called` (no call: a dated bond's
  maturity, a perpetual's endless coupons; the desk default).
- **Yields** are effective annual on the instrument's `yield_convention` (default
  ACT/365F); durations Macaulay and modified at that yield.
- **Coupons** must be known to the redemption date, which must be a coupon date; reset
  coupons are explicit `assumed_coupons`, never extrapolated.
- **Perpetuals** are priced to a named call or never called: the listed coupons, then the
  last one at the same step forever, valued exactly (curve read to 30 years, its own
  flat-forward rate beyond; the sum to 600 years plus its geometric remainder). Yield
  and duration are the endless stream's. MOEX publishes their current yield (coupon
  rate / clean price), the same rate uncompounded.
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

## Desk-monitor analytics (0.33.0)

- **Floater accrued interest**: a coupon fixed in advance accrues pro rata on its amount
  in exact decimals (OFZ-PK) or on its rate with `FloatingCouponLeg.accrual_basis`
  (corporates, `notional * rate * days / 365`); a coupon set in arrears accrues the rule
  over `[start, settlement)` with the realised fixings. Rounded half-up to 0.01. Details
  and the MOEX evidence: [floating.md](floating.md), "Accrued interest".
- **G-spread** (`ValuationPolicy.g_spread_curve_id`): the bond's effective annual yield
  (ACT/365F) minus the KBD effective yield at its Macaulay duration in years, the
  definition of MOEX `GSPREADBP` (552 of 564 rows exact on 2026-10-20). Only RUB
  fixed-coupon bonds; floaters and linkers have none.
- **Discount margin** (`discount_margin_bp`): the spread `m` with
  `dirty = Σ CF_i · ((1 + r_proj(t_i) + m)^(-t_i))` on the projection curve's
  settlement-forward zero rates in the policy's Z-spread convention (annual ACT/365F by
  default), the flows being the base projection. On one curve it is the Z-spread.
- **Spread risk by bucket** (`spread_buckets_rub`): `CS · d_k / Σ d`, `d_k` the central
  discount-only bucket sensitivity (coupons held at their base projection, the same
  bump). It sums to the central `CS`.
- **`open_put` horizon**: the first put with `settlement < offer_date < maturity` whose
  `window_end` is None or not before the settlement date; confirmation is ignored.
- **History metrics** (`bond_metrics_batch`): the definitions of
  `solve_effective_annual_yield`, `bond_duration`, `g_spread_bp` and `solve_z_spread`; on
  `ZcycParameters` the discount factors are the formula's, `exp(-G(t) t / 10^4)`.
- **MOEX `DURATION`** is the Macaulay duration at the published (rounded) `YIELDATWAP`,
  rounded to a day; compare core durations at that yield, not at the close.

## Financed-bond accumulation

`constant_daily_accumulation_periods(start, horizon, boundaries, r)`: factor per period
`(1 + r/365)^(days)`. `project_financed_bond`: `B_{k+1} = B_k · factor_k − cash_event_k`,
starting from the initial dirty cash; cash events must terminate accumulation periods.
This daily compounding is neither a repo contract (simple, or credited on settlement
days: [repo legs](#repo-legs-package-6)) nor the desk's TRADING funding rule (simple
interest on the previous day's clean MV, backend `desk_rules`; local audit F13). It is
kept unchanged for compatibility.

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
