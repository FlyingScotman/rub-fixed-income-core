# Product roadmap: rub-fixed-income-core

> **Canonical home of this plan.** It was dispatched on 16 October 2026 from the system architecture of the Trading Desk Monitor (`trading_desk_monitor/docs/architecture/07_CORE_PRODUCT_COVERAGE.md`, commit `740a476`). From now on, maintain it **here**. The system copy is a frozen snapshot. For this library's role in the system, read [SYSTEM_CONTEXT.md](SYSTEM_CONTEXT.md).

Written against core v0.6.0 (HEAD `454a78c`, 16 October 2026), then reconciled with **0.8.1** and updated release by release from 0.9.0.

## Status after 0.7.0–0.25.0

| Release | Delivered | Roadmap item | Remaining |
|---|---|---|---|
| 0.7.0 | `ShockedCurveCache`: shocked OIS curves are shared across all positions of a job; cancellation and time budgets; versioned, hash-verified result contract `rub-local-portfolio-risk-result.v1` | 0.7 (shared shocked curves), largely | Accept prebuilt base and shocked curves from the caller, so the backend can persist them across jobs; Windows / Python 3.11 benchmark for 2k positions |
| 0.8.0 | `floating`: `FixingHistory` (point-in-time, RUONIA gap checks), `FloatingCouponRule`, `FloatingLeg`; compounded or arithmetic averaging, lookback; known fixings and published coupons frozen under bumps; `BondRiskRequest(floating_leg=...)` | Package 1 (floaters), core part | Floors and caps; payment-lag observation shifts; in-arrears key-rate fixings; **separate projection and discount curves** (today single-curve, with the spread as a discount margin); reproduce MOEX-published current coupons for sample OFZ-PK and corporate floaters |
| 0.8.1 | `fx_futures`: FX futures against RUB by covered parity with a calibrated, frozen basis; RUB and foreign curve DV01, basis01, FX delta, exact scenarios, 23-factor Hessian; daily variation margin | Package 5, FX futures part (also feeds packages 3–4) | OFZ, RUONIA and key-rate futures; FX forwards and FX swaps (package 4) |
| 0.9.0 | Bounded `BusinessCalendar` (`valid_from`/`valid_to`, `CalendarRangeError`), `calendar_identity`; canonical hashing for enums/sets/nested dataclasses in `canonical.py`; `to_dict`/`from_dict`; typed rejections (`errors.py`); CI on Windows/Ubuntu Python 3.11 | 0.1 (calendar type), 0.4, part of 0.9 | Record calendar identity in results of the new portfolio path (0.11.0); pass calendars through Instrument schedules (0.10.0) |
| 0.10.0 | `Cashflow` with coupon/principal split, accrual period and kind; `accrued_interest` from the split; `Instrument`, `MarketSnapshot`, `Position` and their parts in `domain.py` | 0.2, 0.3 | Floating coupons are not projected by `Instrument.cashflows` (package 1 in the portfolio path); `public_schedule` still collapses the split (it is deprecated) |
| 0.11.0 | KBD evaluator, G-spread and materialised curve (golden: MOEX standard yields within 0.01 bp); curve-spec registry (`rub_ofz_kbd_v1`, `rub_ruonia_ois_legacy_v1`, `rub_key_rate_swap_v1`); desk factors `IR\|curve\|bucket`, `CS`, `FX\|ccy` with sparse Hessian; `value_portfolio` over `Instrument`/`MarketSnapshot`/`Position` with coded coverage and result contract v2 | 0.6, minimal 0.5, §2 items 4 and 6 | Full 0.5 (`rub_ruonia_ois_moex_v2`, spot/payment lags, tenor resolver); prebuilt shocked curves across calls and the 2k Windows benchmark (0.7); floaters and FX futures in `value_portfolio` |
| 0.12.0 | `DeprecationWarning` on `analyze_bond` V1, `solve_continuous_z_spread`, the curve-date `price_cashflows`/`solve_z_spread` path, the `clean_price_pct_to_dirty_amount` alias and `bind_market_data_store_schedule`; shared risk internals in `_risk_common.py`; docs refreshed | 0.9 | Removal of the deprecated APIs in 1.0; V1 → annual Z default (BACKLOG #7) |
| 0.13.0 | Caller-held curves: `PrebuiltCurve`, `ShockedCurveCache.export`/`seed` (verified by exact repricing), `value_portfolio(curve_cache=...)`; shared curve snapshots and per-curve memos (numbers unchanged); `benchmarks/portfolio_bench.py` with a Windows CI report. 2k positions, Linux: OIS with Hessian 10.0 ms/position cold, 8.1 ms seeded; KBD 9.3 ms | 0.7 (completed) | Faster OIS bootstrap (BACKLOG #12) would cut the cold cost further. Windows CI (windows-latest, Python 3.11, 2k positions): OIS + Hessian 11.8 ms cold / 9.9 ms seeded, KBD + Hessian 11.0 ms, first order 2.8–3.7 ms |
| 0.14.0 | `horizon_valuation`, `explain_horizon_pnl`, `roll_curve`, `horizon_portfolio`: carry (forwards realised), rolldown (constant curve shape), coupons and amortization received, accrued at both ends, yield-to-offer for confirmed puts; explain into carry + rolldown + rates + spread + residual that sums exactly | 0.8 | Floaters at the horizon (package 1); a hashed horizon result contract if the backend persists it; Taylor (factor) attribution of `rates` for PnL explain (M5, DEC05) |
| 0.15.0 | MOEX goldens (`tests/test_moex_goldens.py`, fixtures from the hub): `ACCRUEDINT` to the kopeck for 7 bonds (OFZ-PD, corporates, amortizer, OFZ-PK current coupon, CNY bond); OFZ 26238 yield at three prices within 0.5 bp and duration within a day, also end to end through `value_portfolio` on KBD; put-bond duration to offer | 0.10, partly | About 12 bonds on fixed EOD dates need EOD ISS exports (only one intraday snapshot is archived); 30/360 accrual and the MOEX short-put yield method (BACKLOG #36); the bridge and fixing tests from the untracked `implementation/` folder are not in this checkout |
| 0.16.0 | `parse_tenor`, `add_tenor`, `resolve_tenor` (spot lag, business-day convention, `ON`/`TN`/`SN`), `ois_quotes`; KRS dates routed through it unchanged | 0.5, part | *Done in 0.22.0* (`rub_ruonia_ois_moex_v2`) |
| 0.17.0 | `RateAccrualCouponLeg`, `RateCouponPeriod`, `AccrualBasis` (rate ACT/365 for MOEX corporates, ACT/360, 30/360); EOD goldens for 18 bonds on 2026-10-15 from ISS: `ACCINT` to the kopeck, yield and duration to maturity, put bonds' `YIELDTOOFFER` and per-trade `EFFECTIVEYIELD` / `GSPREADBP`; BACKLOG #36 resolved | 0.10 (golden bonds completed) | Yield time basis of non-RUB bonds (BACKLOG #38, package 3); the bridge and fixing tests from the untracked `implementation/` folder are still not in this checkout |
| 0.18.0 | Non-RUB bonds (CNY, USD, EUR) in `value_portfolio` on caller-built foreign discount curves (`*_discount_factors_v1`), RUB valuation at the snapshot FX rate, `FX\|ccy` delta = PV_ccy × FX / 100 with FX × rates and FX × spread cross terms, FX scenarios, typed coverage; yield time basis as an issue term (`Instrument.yield_convention`) with 11 USD MOEX goldens (BACKLOG #38 resolved) | Package 3 (core part) | Bootstrapped foreign curve specs (SOFR / €STR / CNY conventions, BACKLOG #40); non-RUB horizon and local worker (#41); per-curve scenario shocks (#43) |
| 0.19.0 | `fx_forwards`: outstanding FX forwards and FX swap legs by covered parity, P&L at the value date discounted on RUB; value dates on a joint calendar; implied foreign curve from swap points, outrights or futures; RUB and foreign KRD, basis01, exact FX delta, exact scenarios, 24-factor Hessian with FX × rates cross terms; MOEX `SRATE_CNY_ON` and CNY/RUB value-date goldens; 2k benchmark ≤ 2.5 ms/position | Package 4 (core part); covered-parity acceptance met for the overnight CNY/RUB swap | Portfolio integration (BACKLOG #45); points-fixed RUB risk (#46); per-day SRATE golden (#47); CETS term-swap goldens (#48); USD/EUR pairs, NDFs, end-of-month rule (#49) |
| 0.20.0 | `callables` (yield to call / worst, price to worst, explicit exercise assumptions, extension risk), perpetuals to an explicit call, `pass_through` (deterministic constant CPR, WAL, CPR sensitivity), `value_assumed_portfolio` / `horizon_assumed_portfolio` with recorded assumptions; MOEX `CALLOPTIONYIELD` / `CALLOPTIONDURATION` and perpetual-yield goldens | Package 9: callables, perpetuals, MBS/ABS (core part) | Native assumption fields in `value_portfolio` (BACKLOG #50); non-par calls in the portfolio (#51); OAS (#52); prepayment models beyond constant CPR and a CPR factor (#53) |
| 0.21.0 | `swaps`: seasoned RUONIA OIS and key-rate swaps; explicit conventions or registered specs; known resets from point-in-time fixings; separate discount and projection curves; bucket DV01 per curve, 11/22-factor Hessian, exact scenarios, carry and rolldown; `IR\|curve\|bucket` factors; par swaps reprice the OIS and KRS quotes, seasoned PVs match an independent reconstruction | Package 2 (core part) | `value_portfolio` integration (BACKLOG #55); key-rate quote-basis risk (#56); confirmation schedules, amortizing notionals (#57); a bank-system golden (#58) |
| 0.22.0 | `rub_ruonia_ois_moex_v2` from the MOEX SPFI curve methodology and NFA `RUB-RUONIA-OIS-COMPOUND`: TOM start, MODFOL, single period to 1Y then annual with a short initial stub, Act/Act ISDA, +1 business-day payment lag, compounded RUONIA without lookback; `OisSwapConvention` in the bootstrap; legacy spec unchanged | 0.5 (exchange OIS spec, resolves F08) | NCC contract text (BACKLOG #59); par repricing on live MOEX curve values, subscriber-only on ISS (#60); the swap side of v2 in `swaps` (#61); `cny_ruonia_xccy_v1` |
| 0.23.0 | OFZ-IN: point-in-time `CpiSeries`, `CpiIndexation` (3-month lag, linear by day), indexed coupon and principal legs with the deflation floor, valuation on a caller-built real curve with the new factor type `REAL\|curve\|bucket`; MOEX goldens over the whole history of SU52002–52005 (`ACCINT` and 45 coupons exact, face within 2.6e-4 from CBR CPI, real yield within 0.5 bp) | Package 9: OFZ-IN linkers (core part) | Official CPI levels and terms text (BACKLOG #62); real-curve builder, breakeven factor, linker scenarios and horizon (#63); gold-linked corporates (#64) |
| 0.24.0 | `cds`: ISDA Standard Model single-name CDS; explicit conventions (`isda_standard`); hazard bootstrap from par spreads and upfronts with a stated recovery; spread ↔ upfront ↔ hazard; outstanding contracts by side; IR bucket DV01, CS01 (`CS`) parallel and per quote, recovery01, scenarios, 12-factor Hessian, JTD with the bond sign (DEC07); reproduces 20 published Markit upfronts | Package 8 (model) | Licensed CDS quotes and recovery (backend data); `value_portfolio` integration (BACKLOG #66); recovery and per-tenor credit factors (#67); index CDS (#68); a bank-system golden (#69); credit-curve export / seed (#70) |
| 0.25.0 | `xccy`: CNY/RUB cross-currency swaps vs RUONIA (fixed or RUONIA RUB leg, fixed or explicit-index CNY leg, initial / intermediate / final exchanges, simple spread, frozen fixings); MOEX `XCS CNYRUB` as spec `cny_ruonia_xccy_v1`; CNY discount-curve bootstrap from XCCY par quotes; par rate / spread solver; KRD per curve role, `XCCYBASIS`, exact FX delta, scenarios, Hessian; a final-exchange-only swap equals the FX forward | Package 7 (core part) | Portfolio integration (BACKLOG #71); NCC XCCY text (#72); live `CNY-XCS-CNYRUO` golden (#73); basis vs `RUSFARCNY` (#74); MTM resets, compounded spreads, lookback, rounding (#75); XCCY carry and persisted curves (#76); quote-fixed FX risk (#77) |

The sections below keep the original 0.6.0 baseline for traceability. Where they conflict with this table, **this table is current**.

System context: core is the pure pricing and risk library of the Trading Desk Monitor. A desk backend calls it in-process. Market and static data arrive as **core domain objects** built by the backend's adapter from the market-data hub's snapshots. Core never parses hub payloads and never does I/O. See the system architecture in `trading_desk_monitor/docs/architecture/` (documents 02 and 03, ADR-003 and ADR-004).

## 1 Coverage today

| Area | v0.6.0 state |
|---|---|
| Fixed-coupon RUB bonds | Price/yield/accrued, Z-spread (annual/monthly/continuous), settlement-aware; caller supplies resolved cashflows (date, total). Bullet, amortizing and confirmed investor put supported **as cashflows**; no schedule generation, no optionality |
| Curves | `DiscountCurve` with log-linear DF and flat-forward extrapolation; RUONIA OIS bootstrap (default: unadjusted annual ACT/365F schedule, no spot/payment lag). The core project's local audit (finding F08, untracked `audit/FINLIB_AUDIT.md`) reports that the MOEX RUONIA OIS specification is TOM start, modified-following, ACT/ACT with a stub and a +1 business-day payment lag; verify this against the exchange specification. **No KBD/ZCYC evaluator** |
| Key-rate | KEYRATE swap par-quote pricing and forward-curve build. **No seasoned-trade pricer**: no notional, side or known resets (local audit F12) |
| Risk | 11-bucket KRD/DV01 (1M…20Y+, identical to the desk buckets); CS01 / OFZ-spread01; exact same-date scenario repricing; full 12×12 central-difference Hessian; Taylor-vs-exact residual; signed JTD with mandatory recovery |
| Portfolio | `local_worker.run_local_risk` with complete/partial/failed coverage semantics; per-instrument `PublicBondSnapshot` embeds its own curve |
| Calendars | `BusinessCalendar` is weekends-only; holiday data must be supplied (BACKLOG P0 #1) |
| Explicitly rejected | Floaters, OFZ-IN, non-RUB, FX, swaps on positions, futures, REPO, XCCY, CDS, callables/OAS (`local_risk.py`, `local_worker.py`, `public_schedule.py` gates) |
| Performance | **0.7.0:** OIS risk with Hessian on the synthetic 10-position job dropped from about 27 s to 2.2 s with `ShockedCurveCache`. Earlier baseline: measured on 16 October 2026 with a scratch benchmark (not in the repository; reproduce it as part of 0.7). Zero-curve basis: 1.4 ms per position first-order, 7.5 ms with Hessian and 5 scenarios. OIS-quote basis: **55 ms / 500 ms**, because every position re-bootstraps the same shocked curves |

Until a work package lands, the desk backend imports that product's PV and risk from bank systems and labels it `imported`. Core must keep rejecting unsupported products explicitly. It must never return zero sensitivities for something it cannot price.

## 2 Common acceptance criteria (apply to every package)

1. **Pure and typed.** Frozen dataclasses, `mypy --strict`, ruff clean, no I/O, no global market state.
2. **Explicit conventions.** Every convention is an explicit input or comes from the versioned curve-spec registry. Defaults are documented.
3. **Units** follow `docs/CONVENTIONS.md`: decimal rates, `_bp`, `_pct`, prices as % of current nominal, amounts per bond or per notional unit.
4. **Risk is emitted in the desk factor vocabulary:**
   - `IR|<curve_id>|<bucket>` for 1 bp pillar shifts on the 11 buckets;
   - `CS` for own spread at 1 bp;
   - `FX|<ccy>` for a 1% move against RUB.

   Plus sparse Hessian entries `(factor_i, factor_j, d2PV)`, upper triangle. State whether DV01 is central-difference or one-sided (DEC08), and keep it consistent with the Hessian.
5. **Independent tests:**
   - analytical cases;
   - round trips (price↔yield↔spread);
   - sign and scaling checks;
   - total = sum of buckets;
   - bump-size convergence;
   - **external golden values** where public (MOEX-published accrued, yields, durations; exchange settlement prices);
   - matching the implementation against itself is not independent.
6. **Coverage semantics.** Unsupported features raise a typed rejection. The portfolio worker reports them in `Coverage`, never as zero.
7. **Performance.** Benchmark per package on 2k synthetic positions of the product. Shared curve builds must be reused across positions.
8. **Docs.** Each package updates `docs/API.md`, `CONVENTIONS.md` and `CHANGELOG.md`, and bumps the minor version.

## 3 Work packages

The book holds **all** the products below (owner, 16 October 2026), so packages 1–8 are all required and package 9 is gated by DEC10. The order below is the default; re-order only if MV weights in the book argue for it. Packages 1–4 assume package 0.

**Runtime constraint:** the desk runs core on Windows with **Python 3.11**. Keep `requires-python >= 3.11`, and test on Windows 3.11 in CI.

### Package 0: foundations (prerequisite for everything)

| Item | Scope | Acceptance |
|---|---|---|
| 0.1 Calendar plumbing | `BusinessCalendar(holidays, working_days, version)` already exists. Add a covered date range (`valid_from`/`valid_to`; dates outside are rejected, never assumed Mon–Fri). Pass the calendar through every schedule, settlement and accrual path. Record version and fingerprint in every result. The calendar comes from the hub's calendars via the backend (holidays, plus transferred working Saturdays as `working_days`; MOEX weekend trading sessions are **not** business days) | Settlement, accrual and schedule tests on known Russian holidays and working Saturdays; out-of-range dates rejected |
| 0.2 Cashflow split | `Cashflow(date, coupon, principal, accrual_start, accrual_end, kind)` replacing total-only `BondCashflow` (keep a compatibility path) | Coupon + principal = total; accrued from the split; PnL-explain consumers can see coupon vs principal |
| 0.3 Domain types | `Instrument` (terms: face, currency, coupon leg (fixed periods or formula), principal leg, offers/calls, calendar ID, conventions); `MarketSnapshot` (curves by ID, fixings by index, FX, calendars, evidence, snapshot_id); `Position` (ID, instrument, signed quantity, book, accounting category, currency, recovery) | Positions reference a shared `MarketSnapshot`; no per-instrument embedded curve |
| 0.4 Serialization | `to_dict`/`from_dict` for core's own types; canonical hashing handles enums and frozensets | Round-trip property tests; stable hashes across runs |
| 0.5 Curve-spec registry | Versioned specs keyed by **core-owned spec IDs** (for example `rub_ruonia_ois_moex_v2`, `rub_key_rate_swap_v1`, `rub_ofz_kbd_v1`, `cny_ruonia_xccy_v1`): day count, fixed-leg frequency, spot lag, payment lag, business-day convention, float compounding/averaging, interpolation; tenor-code → maturity resolution with a calendar. Core never sees market-data codes; the backend maps them to spec IDs | RUONIA OIS bootstrap conforming to the exchange specification (resolves F08) behind a new spec version; legacy spec kept for parity |
| 0.6 KBD / ZCYC evaluator | MOEX parametric G-curve (B1, B2, B3, T1, G1…G9) → zero rates and effective yields; a curve object usable for discounting and spreads; G-spread | Reproduces MOEX `standard_yields` within 0.01 bp for archived snapshots |
| 0.7 Shared shocked curves | Pure API: within one portfolio call, build each shocked curve once per (curve, shock vector) and share it across positions; accept prebuilt base and shocked curves from the caller (the backend persists them). No global state; process-pool friendly. Check in a benchmark script | OIS-quote basis with Hessian ≤ 10 ms per position amortised for 2k positions |
| 0.8 Horizon valuation | Value at T+h with the curve and spread held constant (carry and rolldown), including accrued, coupons received and amortization; offer-horizon handling (yield-to-offer as the market convention for put bonds) | Analytical carry for a zero-coupon bond; carry + rolldown + rates + spread + residual reconcile to the full revaluation change |
| 0.9 API consolidation | Deprecate `analyze_bond` V1, `solve_continuous_z_spread`, the legacy `price_cashflows` curve-date path and aliases; delete dead `_price_at_yield`; move hashing out of `analytics.py`; stop `risk.py` reaching into private curve fields; **deprecate `public_schedule.bind_market_data_store_schedule`** (targets the retired market-data-store) | No consumer breakage without a deprecation release |
| 0.10 External golden tests | About 12 OFZ and corporate bonds (bullet, amortizer, put): MOEX-published accrued, yield, duration on fixed dates; move the bridge and fixing tests out of the git-ignored `implementation/` folder into `tests/` | In CI |

### Package 1: floaters on RUONIA and the key rate (OFZ-PK, corporates)

- **Scope:**
  - coupon formulas from the hub's `FloaterFormula`: benchmark, spread, multiplier, `lag_days`, averaging (arithmetic / compounded / index-ratio), lookback, floor and cap, fixing day;
  - known vs projected fixings, with point-in-time selection (no fixing published after the valuation cut);
  - projection from the RUONIA and key-rate forward curves;
  - the current period partially fixed.
- **Risk:**
  - discount-curve and projection-curve KRDs as separate `IR|curve|bucket` factors;
  - spread to benchmark as `CS`;
  - cap/floor optionality flagged and either excluded or priced with a stated model.
- **Inputs:** fixings (`RUONIA`, `RUONIA_INDEX`, `KEY_RATE`, term RUONIA) with observation, effective and published dates; CBR meeting dates for key-rate paths.
- **Acceptance:**
  - reproduce MOEX-published current coupons for sample OFZ-PK and corporate floaters;
  - reject when a required RUONIA observation is missing; never fill it;
  - no look-ahead: core uses only the fixings in the `MarketSnapshot` it is given, and the snapshot's `known_at` makes selection point-in-time;
  - bonds without a usable formula (curated or inferred) are rejected, not projected.
- **Core BACKLOG reference:** #29 (fixing-aware floater core and API consistency).
- **Depends on:** 0.2, 0.3, 0.5.

### Package 2: seasoned IRS (RUONIA OIS, key-rate swaps)

- **Scope:** payer/receiver, notional, fixed rate, start/end, schedules from the spec, known resets and accrued float, valuation from the discount and projection curves.
- **Risk:** KRD on both curves; Hessian; carry.
- **Inputs:** private OTC terms (backend), curves from 0.5.
- **Acceptance:** a par swap at inception has PV ≈ 0; seasoned swap PV against an independent cashflow reconstruction; DV01 sign by side.
- **Depends on:** 0.5, Package 1 (fixing projection).

### Package 3: non-RUB bonds and FX delta

- **Scope:** CNY, USD and EUR bonds; foreign discount curves (CNY from hub quote sets; USD/EUR from SOFR/€STR data if added to the hub); RUB valuation via FX; `FX|ccy` delta and FX × rates cross terms.
- **Acceptance:** FX delta equals PV in currency × FX / 100 per 1%; cross-gamma signs.
- **Depends on:** 0.3, 0.5; hub foreign curves.

### Package 4: FX spot, forwards, FX swaps

- **Scope:** forward points; implied yield curves from MOEX CETS swap instruments and FX futures (hub data); valuation of outstanding forwards and swap legs.
- **Risk:** `FX|ccy` delta; RUB and foreign curve KRDs.
- **Acceptance:** covered-interest parity round trip against MOEX swap rates.
- **Depends on:** Package 3.

### Package 5: exchange futures (FORTS)

- **Scope:** OFZ futures (cheapest-to-deliver basket from exchange specs), RUONIA and key-rate futures; daily settlement (variation margin) semantics.
- **Acceptance:** reproduce exchange settlement prices from inputs within tolerance.
- **Depends on:** 0.5, 0.6, Package 1.

### Package 6: REPO and funding legs

- **Scope:** valuation of repo legs (fixed, floating on RUSFAR/RUONIA), collateral linkage.
- **Boundary:** the desk's **funding economics** (previous-day clean MV base, calendar-day rates, OCP transfers) stay in the backend's `desk_rules`. Core supplies only instrument valuation. The current `project_financed_bond` compounds daily, which is not the desk's TRADING funding rule of simple interest on the previous clean MV (local audit F13).
- **Depends on:** Package 1.

### Package 7: XCCY (CNY/RUB vs RUONIA)

- **Scope:** two-currency legs, notional exchanges, basis spread, the hub's `cny_ruonia_xccy` quotes.
- **Risk:** both curves plus FX, with cross terms.
- **Depends on:** Packages 3, 4.

### Package 8: CDS

- **Scope:** standard CDS pricing (hazard-rate bootstrap), quote-to-upfront with the stated recovery, JTD consistent with bond JTD sign conventions (DEC07).
- **Blocked on:** *model delivered in 0.24.0* (`docs/cds.md`); production use is blocked on a licensed CDS data source (quotes, recovery, entity mapping) in the backend.
- **Depends on:** 0.3.

### Package 9: owner-gated special products (requires DEC10)

- **OFZ-IN linkers:** CPI series and indexation lag, indexed nominal, real-yield curve factor (new factor type, contract change). Needs a CPI series in the hub.
- **Callables and perpetuals:** yield-to-call / worst, call schedule, extension-risk measure; perpetuals priced to call with an explicit assumption.
- **MBS/ABS:** CPR/WAL; explicitly deferred unless the book holds them.

## 4 Suggested sequencing and size

| Order | Package | Rough size | Unblocks |
|---|---|---|---|
| 1 | 0.1–0.4, 0.6, minimal 0.5 (`rub_ofz_kbd_v1`), 0.9 | 1–2 weeks | Backend adapter, M1 vertical slice (OFZ on the KBD curve) |
| 2 | rest of 0.5, 0.7, 0.8, 0.10 | 2–3 weeks | Exact OIS risk at speed, carry/rolldown, PnL explain |
| 3 | 1 Floaters | 2–3 weeks | Most of a typical RUB bond book |
| 4 | 2 IRS | 1–2 weeks | Hedging swaps |
| 5 | 3 + 4 Non-RUB, FX | 2–3 weeks | FX book, OCP accuracy |
| 6 | 5, 6, 7 | as needed by book mix | — |
| 7 | 8, 9 | gated | — |

Each package ends with a tagged release, a CHANGELOG entry, benchmark results and the golden test evidence. The backend moves the product from `imported` to `computed` only after a period in which both paths run in parallel and their differences are reviewed.
