# Backlog — remaining fixes and updates

Prioritised (P0 = production correctness risk, P1 = important, P2 = hygiene). Sources: the
2026-10-14 shared-core audit and the 0.3.0 release work. Items marked *(downstream)* live in
other repositories and must be changed there, not in this core.

## P0

1. **Official versioned business calendar.** *Core side done in 0.9.0–0.11.0*: bounded
   calendars reject dates outside their range, and `value_portfolio` records each calendar's
   version and fingerprint. The hub publishes the calendar per snapshot (system doc 02).
   Original item: the core ships no holidays (default weekends-only). Wire an official,
   versioned MOEX/CBR trading/settlement calendar source
   (with transfers and working weekends) into a `BusinessCalendar` builder owned by a data
   project (e.g. market-data-store) and record `version` + `calendar_fingerprint` in outputs.
   Do not hard-code holiday lists from memory.
2. *(downstream)* **ofz-rates-app `ofz_analytics/fwd_fwd.py` `forward_swap_rate`** treats OIS
   par rates as annual zero rates with linear interpolation — LIVE cron `237311c0a59a`;
   ~28–31 bp bias vs the core bootstrap in a probe. Migrate to `bootstrap_discount_curve` +
   DF-ratio forwards (ofz-swaps-v2 already did).
3. *(downstream)* **Silent solver garbage in vendored `ofz_analytics`** (4 copies:
   ofz-analytics, ofz-rates-app, ofz-tracker, ofz-swaps-v2) — `models.py` `OFZBond.ytm` /
   `accrued_interest` use brentq and return −0.99 / 5.0 when unbracketed (models.py:201-205).
   Replace with `solve_nominal_annual_yield` (now raises).
4. *(downstream)* **ofz-tracker `backend/historical_calc.py` `historical_ytm` returns 0.0 on
   failure**, used live by ofz-market-figures-reporter. Raise / mark missing instead.
5. *(downstream)* **T+1 settlement computed as weekday-only or calendar+1** in ofz-rates-app,
   ofz-tracker, ofz-analytics, rgbi `current_application.py:216`, reporter `charts.py:376` and
   `corporate.py:927`. Use `SettlementConvention(1, official_calendar).settlement_date(...)`.
6. *(downstream)* **Z-spread convention split.** corporate-bond-monitor uses the continuous Z
   from `analyze_bond` while market-data-store / currency-bond-monitor / reporter use annual
   (16.2 bp gap in the OFZ 26238 probe). Migrate corporate-bond-monitor to
   `analyze_bond_v2` (annual default) with a stored convention id; plan the hash migration.

## P1 — in-core

7. **Switch `analyze_bond` default to annual Z in the next major version (1.0)**; until then
   V1 stays continuous for hash reproducibility and V2 is the recommended entry point.
8. **ACT/ACT-ICMA day count** (needs reference coupon period / frequency in the API contract).
9. **Business-day-adjusted OIS fixed-leg schedule by default.** Today the default bootstrap
   keeps the legacy unadjusted forward schedule (with the 1-day stub for adjusted maturities)
   to preserve numbers; opt-in via `CurveConvention(fixed_leg_schedule=ScheduleConvention(...))`.
   Decide desk default with a QR/Calypso golden comparison, then flip in a major version.
10. **External golden benchmark** of annual and monthly Z-spread, yield, and bootstrap against
    VTB QR / Calypso on frozen inputs (`tools/qr_bond_reconciliation_probe.py` is the start).
11. **Decimal cash arithmetic** — cash amounts are floats; AI rounding uses `Decimal(str(x))`.
    Define a decimal contract for money if required by consumers.
12. **Bootstrap is O(n²)** — each candidate rebuilds a `DiscountCurve` and re-walks nodes.
    Fine for ≤ 30 pillars; optimise if used intraday at scale. Since 0.7.0 the worker
    shares shocked curves across positions, so OIS jobs pay ~250 bootstraps per distinct
    curve (~2.2 s with Hessian on the synthetic job). A faster bootstrap is the next lever.
    0.13.0: one bootstrap costs ~15 ms; at 2k positions the ~250 shocked curves add
    ~1.9 ms per position cold, and nothing when the backend seeds them
    (`ShockedCurveCache.seed`). A faster bootstrap changes curve bytes, so it needs a new
    model version.
13. `price_cashflows` legacy path (no `settlement_date`) counts a flow on the curve date at face
    and ignores earlier flows; settlement-aware callers should always pass `settlement_date`.
    *Deprecated in 0.12.0* (warns; removal in 1.0).
14. Curve interpolation weights are calendar days even if `time_day_count` ≠ ACT/365F
    (documented). Revisit if a non-ACT/365F curve time basis is used in production.
15. `zero_rate`/`forward_rate` compounding for forwards is fixed annual; add a `Compounding`
    argument if a consumer needs it.
16. *Done after 0.8.1.* `uv run mypy` passes: the untyped probe test is excluded in
    `[tool.mypy]` and from the sdist. Run it in a Python 3.11 venv
    (`uv sync --extra dev --python 3.11`); numpy stubs need Python ≥ 3.12 syntax support
    when the venv is newer than `python_version = "3.11"`. CI runs Windows and Ubuntu on 3.11.
17. **Git tags for historic versions are absent.** The repository history starts at 0.2.1
    (root commit `1baac88` already declares 0.2.1), so 0.1.0 / 0.2.0 cannot be tagged with
    certainty. 0.3.0 is the first tag.

## P1 — downstream migration to 0.3.0 *(downstream)*

18. **Re-pin and refresh locks** (`uv lock` in each project, by its owner):
    - corporate-bond-monitor pins `==0.1.0` non-editable path, stale `.venv` with 0.1.0 and
      numpy missing from the venv.
    - rgbi-futures-arbitrage-monitor pins `==0.1.0` via a vendored wheel (scipy brentq; no
      compatibility/analytics) — rebuild the vendored wheel from tag `v0.3.0` when migrating.
    - moex-market-data-mcp pyproject says `==0.1.0` but its lock resolves editable 0.2.1.
    - ofz-swaps-v2 pins `==0.2.0` (lock 0.2.0, runs 0.2.1).
    - currency-bond-monitor and market-data-store pin `==0.2.1` editable.
    - Transitive editable: currency-bond-arbitrage-monitor, ofz-market-figures-reporter,
      market-snapshot-reporter, corporate-bond-trade-monitor (dev).
    Editable consumers already run 0.3.0 after merge (verified: uv does not re-check the
    editable version at run time), but the pins/locks are misleading until refreshed.
19. currency-bond-monitor `curves.py:54-66` manually rebases curves to settlement — replace
    with `solve_z_spread(..., settlement_date=settle)` (proven identical by tests).
20. market-data-store: `storage.py:1801` `_next_moscow_business_day` hard-codes holidays without
    transfers; `curves.py` has its own bisection, KRS-path spread and floater pricing — migrate
    to core solvers / calendar input.
21. ofz-market-figures-reporter `corporate.py:451` `_local_fixed_yield` own bisection → use
    `solve_nominal_annual_yield`.
22. Three copies of `implied_key_rates/moscow.py` home-grown holiday calendar (includes
    Radunitsa) → replace with the official calendar source (item 1).
23. corporate-bond-trade-monitor PROD release `deploy/prod/releases/0.6.22-alert-readiness-otc-mds-0.7.3`
    runs frozen core 0.2.0 + market_data_store 0.7.3 (source is 0.11.2) and is **not
    scheduled** (job `3f0e998fbbf7` absent from jobs.json; DB last written 2026-09-09). Decide:
    redeploy on current stack or retire. ~30 stale release venvs under `deploy/prod` to prune.
24. Consumers that catch `ValueError` from core solvers should log the new explicit messages;
    previously some invalid inputs returned silent garbage (e.g. 500 % spread).

## P2 — test environment hygiene *(downstream)*

25. corporate-bond-monitor: numpy missing in venv; `tests/test_headless_analytics.py` hangs
    after 8 tests (independent of core version).
26. ofz-swaps-v2 and ofz-analytics: `truststore` missing → test collection fails without
    `--with truststore`.
27. ofz-rates-app: pytest is not a dev dependency.
28. market-data-store (2) and ofz-market-figures-reporter (1): tests asserting
    `HTTPS_PROXY` routing in cron wrappers fail in the current environment.

## Floater audit and local worker follow-up — 2026-10-15

29. *(core completed in 0.26.0: bounds, shifts, two curves, portfolio and horizon; follow-ups #78–#86)* **P1 — fixing-aware floater core and API consistency (C05).** *Core part done
    in 0.8.0* (`docs/floating.md`): explicit point-in-time `FixingHistory`,
    business-day observation with lookback, compounded/arithmetic averaging,
    margin, day counts, rounding of fixed coupons only, published-coupon and
    zero-placeholder handling, missing-history failures, and single-curve risk
    with known fixings frozen. market-data-store 0.13.0 migrates both callers (the
    service floater coupons and `curves.price_floater`/`solve_floater_spread`).
    Remaining: contractual lag/rounding/floor terms from the bond catalog (callers
    still label coupons `*_proxy_missing_lag_rounding`); separate projection and
    discount curves with their own risk factors; floaters in the local worker and
    the schedule bridge. Original item: The service
    already joins realized RUONIA/key-rate accrual with projection; known full
    coupons are respected. The separate market-data-store `curves.py` API has no
    history input and rejects elapsed periods with current-date curves. Introduce
    explicit fixing history/as-of and contractual observation rules in core,
    migrate both callers, and freeze known fixings under risk bumps. Cover full
    and partial periods, margin, zero published coupons, lag, day count, rounding
    and missing-history failure. Do not classify projected floaters as fixed risk.
30. *Done in market-data-store 0.12.0.* **P0 — point-in-time fixing selection (downstream, C05/HIST-01).**
    `CurveRepository.index_fixings` selects by fixing date only; future-ingested
    observations can enter historical results. Add known-at/revision selection,
    preserve corrected observations and bind fixing snapshots to calculation IDs.
31. *Done in market-data-store 0.12.0 (carry limit 2 business days in 0.12.1).*
    **P0 — RUONIA observation gaps (downstream, C05/DATA-01).**
    `_realized_ruonia` silently carries a stale fixing across missing weekdays.
    Validate required observations with an official calendar and explicit
    permitted carry rules; distinguish overnight fixings from key-rate changes.
32. **Worker integration:** 0.7.0 adds the core side: shared OIS curves,
    cooperative cancellation/time budget and the versioned, hash-verified result
    contract. 0.6.0 supplies the pure portfolio worker and strict
    market-data-store schedule bridge (`docs/local-worker.md`). Remaining: wire
    validated live public bundles and private local positions into the actual
    monitor, persist coverage/basis/content identity, validate Windows and measure
    the real supported universe. No source-authenticity or complete-history claim
    follows from snapshot labels or synthetic integration tests.

## FX futures — 2026-10-15

33. *(portfolio part done in 0.29.0; local worker open, #102)* **FX futures in the local worker and the monitor.** The 0.8.1 core
    (`docs/fx-futures.md`) gives rate risk on the RUB and foreign curves, basis,
    FX delta, scenarios and the Hessian for a single position. Remaining:
    - a public FX futures snapshot (contract terms from the exchange specification,
      settlement price, spot and its value date, both curves, evidence);
    - the delivery date from the official calendar (P0 #1);
    - positions and coverage in `run_local_risk`;
    - a new result-contract version, because v1 fixes the bond field set;
    - an agreed choice of the foreign curve (OIS, or implied from FX swaps).
34. *(core done in 0.19.0, `fx_forwards`; portfolio integration is #45)* **OTC FX forwards and swaps.** Their P&L is paid at `T` and needs discounting
    on the RUB curve. This is a separate settlement style, not the futures model.
35. *(downstream, done in market-data-store PR #5)* **market-data-store core pin.** It
    pinned `>=0.8.0,<0.9`; the PR widens it to `<1.0`, regenerates the lock and moves off
    the `clean_price_pct_to_dirty_amount` alias. Its `price_cashflows`/`solve_z_spread`
    calls without `settlement_date` still warn; moving them is a behaviour change for the
    store's owner before core 1.0.
36. *(done in 0.17.0)* **MOEX golden gaps (0.15.0).**
    - **USD bond RU000A0JXTS9 (30/360).** Closed: `RateAccrualCouponLeg` with
      `AccrualBasis.RATE_30_360` gives MOEX's 2738.89. EOD data showed the wider rule:
      corporate bonds accrue on the rate (ACT/365, a kopeck off pro rata in about one
      case in ten), OFZ pro rata on the amount.
    - **Short put bond RU000A0JS4Z7.** Not a core error. For put bonds MOEX's `YIELD` and
      `YIELDATWAP` / `YIELDATPREVWAPRICE` are a different, higher measure. The effective
      yield to the offer is `YIELDTOOFFER` (at `CLOSE`) and `trades_yields.EFFECTIVEYIELD`,
      and core reproduces both. The other measure is still unidentified; do not compare
      core yields with it.
    - **Coverage.** 18 bonds on the 2026-10-15 EOD (`tests/test_moex_eod_goldens.py`).
      The backend adapter must choose the leg type by board: `FixedCouponLeg` for OFZ,
      `RateAccrualCouponLeg` with the bondization `valueprc` for corporates.
37. *(done in 0.22.0 from the MOEX methodology and NFA terms; follow-ups #59–#61)* **RUONIA OIS exchange spec (`rub_ruonia_ois_moex_v2`, audit F08).**
    - **What is known.** Only a summary survives in this checkout: TOM start, modified
      following, ACT/ACT with a stub, +1 business-day payment lag. The fixed-leg
      frequency, float compounding (lookback or lockout) and the stub side are not stated.
    - **What is missing.** The MOEX methodology files (`fs.moex.com/files/26130`,
      `27002`) and NFA standard terms. On 2026-10-17 the build environment reaches
      `iss.moex.com` and `www.cbr.ru`, but `fs.moex.com`, `www.moex.com` and `nfa.ru`
      are still refused, and ISS does not carry the specification text.
    - **To do.** With the text: add spot and payment lags and stub handling to the
      bootstrap's par-rate leg. New convention fields must stay out of existing curve
      identities. Then register the spec and test par repricing on the hub's ratescalcs
      snapshot (17 tenors, `ois_quotes`).
38. *(done in 0.18.0: `Instrument.yield_convention`, 11 USD goldens)* **Yield time basis of non-RUB bonds (package 3).** On the 2026-10-15 EOD, some USD
    replacement bonds on TQOD are quoted on an ACT/360 time basis (RU000A105RH2,
    RU000A108P79, RU000A105R62, RU000A105JT4, RU000A105A95, RU000A1059N9, RU000A1059P4),
    others on ACT/365F (RU000A10A877, RU000A108Y11, RU000A107B43, RU000A106G56). Accrual
    and yield basis do not coincide: several ACT/360-quoted bonds accrue 30/360. Core
    solves yields on ACT/365F only, so it is about 1.4 % of the yield high on the first
    group (`test_usd_bond_yield_time_basis_is_a_known_gap`). ISS does not publish the
    basis; it has to come from the issue terms (backend static data), then be a yield
    convention input.

## Non-RUB bonds — 0.18.0

39. *(downstream)* **Yield basis in the backend's static data.** The adapter must set
    `Instrument.yield_convention` from the issue terms of every non-RUB bond. The seven
    ACT/360 bonds in the goldens are evidence, not a rule; ISS does not publish the basis.
40. *(partly done: CNY bonds on the built `cny_ruonia_xccy_v1` curve and USD / EUR
    government par-yield proxies in 0.34.0)* **Bootstrapped foreign curve specs.** SOFR /
    €STR OIS as registered specs once their quote conventions (spot and payment lags,
    fixed frequency, day counts) are confirmed from primary sources. Until then the backend
    builds the discount factors (`*_discount_factors_v1`) or uses a proxy.
41. *(partly done in 0.29.0: every product in value_portfolio; horizon and local worker open)* **Non-RUB horizon and local worker.** FX forward carry in `horizon_portfolio`, and
    non-RUB bonds in `local_worker.run_local_risk`; both still reject them.
42. *(downstream)* **FX rate semantics.** Core values at the snapshot's `FxRate` as given,
    with no FX settlement lag or forward points. The backend chooses the CBR official or
    the market TOM rate and records it in the evidence.
43. *(done in 0.29.0)* **Per-curve scenario shocks.** A `PortfolioScenario` shifts every position's own
    discount curve; desk scenarios that move only the RUB or one foreign curve need
    per-curve shift vectors.
44. *(decided in 0.29.0: v3 only for books with derivatives, bond-only books stay v2)* *(downstream decision)* **Result schema for non-RUB rows.** 0.18.0 keeps
    `rub-portfolio-risk-result.v2` with optional keys, so RUB-only output is
    byte-identical; a 0.17 reader rejects a result with non-RUB rows. Bump to `v3` if the
    backend needs every result readable by older releases.

## FX forwards and swaps — 0.19.0

45. *(done in 0.29.0)* **FX forwards in the portfolio path** (extends #33). Needs `Instrument` terms for
    `FX_FORWARD` / `FX_SWAP` (legs, quote units, trade ID) or a separate position type; an
    FX spot **with its value date** in `MarketSnapshot` (`FxRate` has none); RUB and
    foreign curve IDs, settlement calendar ID and basis (or an implied-curve policy) per
    pair; coverage codes for settled legs and missing implied curves; factor totals shared
    with bonds and futures (`IR|curve|bucket`, `FX|ccy`, `FXBASIS|ccy`); one shocked-curve
    cache and implied curve per pair per job; a new result-contract version.
46. *(done in 0.35.0 beside the factors; as the factor basis: #128)* **Points-fixed RUB
    risk.** On an implied foreign curve a RUB shock holds the implied
    foreign zero rates, so the points move. A "points held" basis would rebuild the
    implied curve under RUB shocks and needs a factor for the points.
    `points_held_rub_risk` (engines) and `ValuationPolicy.fx_points_held_risk` (rows)
    hold every outright (`DF_for' = DF_for · DF_rub' / DF_rub`, equal to a rebuild of the
    implied curve at its quote dates).
47. **SRATE methodology.** Confirm the indicator's spot and time window from the MOEX
    methodology (now reachable on `fs.moex.com` with the Russian Trusted Root CA), then
    tighten the golden from a statistical to a per-day check.
48. **CETS term swaps.** A 1W–6M points golden needs days with trades, or MOEX's
    indicative term rates if ISS publishes them; `PREVPRICE` is usually days old.
49. *(partly done in 0.35.0: NDF fixing date and source on the leg, `NdfConvention`
    date rule, a published fixing freezes the leg, `FxFixing` in the snapshot)*
    **USD/RUB, EUR/RUB, NDFs, end-of-month rule.** Spot lag and USD calendar from
    confirmations (OTC only since 2024); NDF fixing source and lag, and freezing a
    published fixing; the end-of-month rule for FX tenors. Open: the spot lag and
    calendars, the end-of-month rule, and the firm's NDF source and lags (#126).

## Callables, perpetuals, pass-throughs — 0.20.0

50. *(done in 0.29.0)* **Native exercise assumptions in `value_portfolio`.** With `OMIT_IF_DEFAULT` the
    assumption can live in the portfolio itself: optional per-instrument assumptions on
    `ValuationPolicy`, `PositionValuation.exercise_assumption`, a `"call"` value of
    `horizon` (and `local_risk`'s `"issuer_call"` label), all omitted at their defaults.
    Today `value_assumed_portfolio` wraps the portfolio, and an exercised bond reads
    `horizon = "maturity"` with `horizon_date` = the call date.
51. **Non-par calls in the portfolio.** `exercised_instrument` rejects calls away from
    100 % (redemptions must sum to the face); redeem at a price as offers already do.
52. **OAS / option value.** Needs a stated short-rate model (e.g. Hull-White on the KBD
    or OIS curve) and a lattice; until then rejected. `to_worst` does not switch the
    exercise date under bumps (yield-to-worst risk, by design).
53. **Prepayment models and goldens.** Rate-dependent or seasoned prepayment (PSA-style)
    beyond constant CPR, a `CPR` desk factor in the result contract, a yield convention
    on `PassThroughTerms`, and DOM.RF mortgage-bond goldens (ISS does not publish the
    CPR behind projected amortizations; SFO TB-11 coupons imply a schedule ISS lacks).
54. *(downstream)* **Adapter guidance for calls.** A MOEX call is a bondization `offers`
    row with a real date; its `price` is % of the initial face. An ABS may list the call
    redemption as an amortization on the call date and zero at legal maturity: the
    adapter moves the remaining face to legal maturity and adds the call. Coupons after
    the call are often unset: omit them (to-maturity is then rejected) or pass resets.

## Seasoned swaps — 0.21.0

55. *(done in 0.29.0)* **Swaps in `value_portfolio`.** Needs a `SwapInstrument` (`Instrument` is bond-shaped)
    with the projection curve and fixing IDs on the instrument, not on
    `ValuationPolicy` (the policy is part of `job_id`); key-rate projection curves in
    `CurveEntry` (`_build_curve` rejects `KEY_RATE_SWAP` specs today); a rule for KRS
    curves dated TOM against OIS curves dated the business date (a TOM discount curve
    from the backend, or values at a date after the curve date); a swap row type and
    per-curve scenario shifts, i.e. result contract `v3`. `FactorTotal` aggregation and
    `swap_horizon` already fit.
56. **Key-rate quote-basis risk.** Today key-rate projection risk shifts the forward key
    rate. Add shifts of the KRS quotes with recalibration, cached per shift vector, and
    decide whether a discount shock recalibrates the key-rate curve.
57. **Swap terms from confirmations.** Explicit period schedules, other front and back
    stubs, amortizing or accreting notionals, compounded spreads, caps and floors.
58. **Seasoned-swap golden.** No public golden exists; reconcile against a bank system
    (QR / Calypso) on frozen inputs when the desk can share one (with #10).

## MOEX RUONIA OIS spec — 0.22.0

59. **NCC contract specification.** NCC's «Спецификация внебиржевых договоров
    «Процентный своп»» governs cleared contracts; `nationalclearingcentre.ru` and
    `nkcbank.ru` are refused by the proxy. Core uses NFA's `RUB-RUONIA-OIS-COMPOUND`
    (`Act` = 366 if any day is in a leap year) on the strength of the index name. If NCC
    annualises differently, only periods crossing a leap-year boundary change (factor
    1.00219 for 19.10.2028–19.10.2029).
60. **Live par repricing of v2.** ISS publishes `/iss/sdfi/curves/RUB-OIS-RUONIA`, but its
    values are for subscribers only, and the hub's ratescalcs snapshot is not in this
    checkout. With either, add a raw fixture and test exact par repricing of the 17
    tenors; compare with `RUB-DSC-CSARUB` for MOEX's own curve construction. The backend
    builds the joint SPFI / MOSCOW / RUONIA calendar from official holiday lists.
61. *(done in 0.29.0)* **The swap side of v2.** `swaps.swap_convention("rub_ruonia_ois_moex_v2")` raises
    `UnsupportedProduct`: the swap float leg does not yet apply the NFA leap-year `Act`
    rule. Build seasoned v2 legs from `OisSwapConvention.periods` and
    `float_amount_scale`, then derive the convention. v2 bootstraps cost about four times
    the legacy one (coupled refinement passes; see #12).

## OFZ-IN linkers — 0.23.0

62. **Official CPI levels and the terms text.** Minfin order No. 80n, the per-issue
    orders and Rosstat's CPI levels (`minfin.gov.ru`, `rosstat.gov.ru`, legal databases)
    are refused by the proxy. Re-run the face golden on the official levels to confirm
    kopeck agreement, confirm the half-up rounding of the face and the precision of
    `INDEX_base`. *(downstream)* The hub needs a point-in-time CPI dataset with
    publication dates, ideally with Minfin's monthly nominal table as a cross-check.
63. **Linker risk beyond the real curve.** A real-curve builder (from OFZ-IN real yields
    or nominal KBD minus breakeven) with a registered spec; a breakeven / inflation
    factor (`INFL|<series>`) and projected-CPI valuation on the nominal curve; real-curve
    scenario vectors; linker carry and rolldown (`horizon_portfolio` rejects them); the
    option value of the deflation floor (intrinsic only today); MOEX `IRICPICLOSE` /
    `BEICLOSE`, whose meaning is not identified.
64. **Gold-linked corporates** (Seligdar RU000A1062M5, RU000A108RQ7). The face tracks the
    CBR gold accounting price per gram with an offset of 1–3 business days that is not
    stable in the data; the terms on `e-disclosure.ru` are refused. Model them as a
    daily-index rule once the terms are available.
65. *(downstream)* **Adapter mapping for linkers.** Map `SU52…` (`FACEVALUE_TYPE`
    "Индексированный", `BONDTYPE` "Линкер") to `INFLATION_LINKED_BOND` with
    `IndexedCouponLeg` / `IndexedPrincipalLeg`, base date = `ISSUEDATE`, and give each
    linker a real-curve override (`docs/CONSUMERS.md`).

## CDS — 0.24.0

66. *(done in 0.29.0)* **CDS in `value_portfolio`.** Needs a `ProductType.CDS` and a contract form (or a CDS
    position list in the job), credit quotes keyed by reference entity in
    `MarketSnapshot`, a recovery source with no default, the contract currency's discount
    curve ID, a `CdsCurveCache` per job, coverage codes and result fields (side, points
    upfront, par spread, `recovery01`, accrued premium). Non-RUB contracts can reuse
    `fx_translated_factor_risk`; JTD adds up with bond JTD (same sign rule). Until then
    `value_portfolio` rejects CDS as `UNSUPPORTED_PRODUCT`.
67. *(partly done in 0.29.0: CDS CS shared with bonds; recovery and per-tenor factors open)* **Credit factor contract.** Recovery sensitivity is reported beside the factors; a
    desk factor (e.g. `REC|<entity>`) is a contract change. `CS` on a CDS is all of the
    entity's quoted spreads together; per-tenor credit risk as factors needs a key such
    as `CS|<entity>|<tenor>`.
68. **CDS products out of scope:** index CDS (CDX / iTraxx), tranches, options, quanto
    CDS, recovery term structures, default before step-in and the settlement lag of
    default payments, the 60-day backstop; ACT/ACT premium day counts.
69. **CDS golden against a bank system** or Bloomberg CDSW on frozen inputs once a data
    source exists. Current evidence is the ISDA / Markit values published in QuantLib's
    test suite; cdsmodel.com was not reachable.
70. *(partly: shared within a job in 0.29.0; across jobs open)* **Credit-curve export / seed.** The cold Hessian costs about 288 hazard rebootstraps
    per entity (34–40 ms per position). Keeping a `CdsCurveCache` across jobs needs an
    export / seed API with a repricing check, like `ShockedCurveCache.seed`.

## Cross-currency swaps — 0.25.0

71. *(done in 0.29.0)* **XCCY in `value_portfolio`.** Needs an `XccyInstrument` (or a separate position
    type), an FX spot with its value date in `MarketSnapshot` (as #45), curve IDs per
    role (RUB discount, RUB projection, CNY curve as `cny_discount_factors_v1` or
    `cny_ruonia_xccy_v1` with its quotes, the NCC notional rate and the spot), RUONIA and
    foreign index fixings, coverage codes, factor totals shared with bonds, FX forwards
    and swaps (`XCCYBASIS|CNY`, or an alias to `FXBASIS|CNY` if the desk treats them as
    one), one `XccyCurveCache` per job, and result contract `v3`.
72. **NCC XCCY specification** («Валютно-процентный своп»,
    `nationalclearingcentre.ru/catalog/0204/274`, refused by the proxy): confirm the
    exchange dates (today NFA 2011 6.2 defaults, no +1B), the amortization rule and the
    rounding.
73. **XCCY golden.** `CNY-XCS-CNYRUO`, `CNY-HYB-CNYRUO` and NCC's `CNY-DSC-CSARUB` are on
    ISS for subscribers only. With access, test the bootstrapped curve against NCC's
    CSA=RUB CNY discount factors and repricing of MOEX quotes.
74. **Basis against `RUSFARCNY`.** MOEX lists `XCCY/BASIS` vs RUSFARCNY; a basis curve
    needs the RUSFARCNY-OIS-COMPOUND formula from a primary source and a CNY projection
    spec (`CNY-OIS-RUSFCN`).
75. **More XCCY terms:** mark-to-market resets (FX fixing source and lag), the
    compounded-spread variants of the NCC parameters, lookback and observation shift, the
    0.0001 % rounding of settled RUONIA indices (NFA 8.4(б)) and 2-decimal amounts,
    KEYRATE or RUSFAR RUB legs.
76. *(partly: shared within a job in 0.29.0; carry and across jobs open)* **XCCY carry and persisted curves.** Horizon / carry for XCCY, and export / seed of
    shocked XCCY curves across jobs (a cold quote-basis job rebuilds about 530).
77. **Quote-fixed FX risk.** On the quote basis the CNY curve is held under spot moves
    (sticky curve, linear FX delta). A desk that holds the quotes under FX moves needs
    the curve rebuilt under spot shocks.

## Floaters — 0.26.0

78. *(done in 0.33.0: `explain_horizon_pnl(..., fixings=, end_fixings=, projection_curve=,
    end_projection_curve=)`)* **Floater P&L explain.** `explain_horizon_pnl` rejected
    floaters; it needs the fixings realised over the horizon and the end projection curve.
79. **Cap / floor time value.** A volatility model (e.g. Bachelier on the period average);
    today a bound nearer than the explicit guard is rejected.
80. **Index-ratio averaging and announced key-rate decisions.** `RUONIA_INDEX` fixings
    and term RUONIA as an index are not modelled. History rows must precede the
    valuation date, so a key-rate decision announced before the cut but effective later
    is projected from the curve; CBR meeting dates are not an input.
81. *(done in 0.33.0: `FloatingLeg.accrued_interest`, `Instrument.accrued(settlement,
    market)`; 430 of 453 traded RUB floaters exact on 2026-10-20, 443 within a kopeck)*
    **Floater accrued convention** (MOEX `ACCINT` for in-arrears floaters), so quotes need
    not carry the accrued. Open: `HorizonValuation.accrued_*` is still None for floaters;
    a published in-arrears coupon accrues the realised formula at MOEX (#120).
82. **Floaters in the local worker and non-RUB floaters.** `FixedRubBondInput` is fixed
    only; `value_portfolio` rejects non-RUB floaters.
83. *(done in 0.29.0)* **Per-position model flags** in the portfolio result (e.g. `has_optionality`, the
    two-curve model) need result contract `v3`.
84. *(downstream)* **Floater data.** The per-bond formula (lag in calendar or business
    days, averaging, in advance or in arrears, rounding, bounds) from the hub's
    `FloaterFormula` or curated static data: ISS gives only the benchmark and margin, and
    issuers differ for the same index. The RUONIA publication calendar (CBR computes no
    RUONIA on the last business day of the year) in the hub's calendar feed.
85. **Unexplained floater coupons.** The first coupons of SU29026 / SU29027 (December
    2024), 6 of 47 sampled RUONIA corporates and 11 of 45 key-rate corporates (caps,
    floors, multipliers or ACT/360 suspected). Revisit with the issue terms when
    `minfin.gov.ru` / `e-disclosure.ru` are reachable. The leap-year basis of corporate
    floaters is unconfirmed (data cover 2025–2026).
86. **Floater risk speed.** 2k floaters cost 17–20 ms per position first order and
    59–100 ms with the Hessian (23 factors on two curves), above the 10 ms target.
    Telescoped compounding or prefix sums would help but change results in the last
    bit, so they need a new model version; a sparse Hessian is the other lever.

## Rate futures — 0.27.0

87. *(portfolio part done in 0.29.0; local worker open, #102)* **Rate futures in `value_portfolio` and the local worker.** Needs terms for
    `RATE_FUTURE` (RUON / RUONIA index / KEYRATE) and `OFZ_FUTURE` (contract dates, basket
    ISINs with published CFs) or a futures position type; snapshot entries for RUONIA
    fixings with declared unpublished days, a published RUONIA index value, the key-rate
    forward curve, basket closing prices and the settlement price; curve roles
    (projection, bond, funding) per contract; `FUTBASIS|<contract_id>` in the result
    contract and factor totals; coverage codes for expired contracts, undeclared RUONIA
    gaps and baskets without prices; one engine per contract (`for_quantity`).
88. **RRK6 (RUON-5.26) final settlement.** ISS shows 85.79, the specification formula on
    CBR fixings gives 85.82 (no open interest); nine other expiries match exactly. Ask
    MOEX whether a decision changed it or a fixing was revised.
89. **OFZ conversion-factor time basis for long bonds.** ACT/365F with one round yield per
    series reproduces 19 of 25 December 2021 factors; six OF10/OF15 factors are 1–2 units
    of the fourth decimal off. Pricing uses published factors. The per-series conversion
    yields (6.6–8.7 %) were inferred from prices; the exchange decisions were not found.
90. **Delivery-option value for OFZ futures** (quality and timing) needs a stated
    stochastic model; today the model price carries none.
91. **Live settlement marks.** RF* / KK* settlement prices without trades are MOEX marks
    (RFZ6 on 2026-10-16 implies RUONIA 54 bp below the KEYRATE path); a tighter
    mid-life golden needs MOEX's mark methodology or traded days.
92. **RUSFAR one-month futures (`1MFR`)** share the RUON specification but need a RUSFAR
    fixings type (MOEX indicator calculated at 12:30 for the same day).
93. **First KEYRATE expiry (KKV6, 2026-10-23).** Add its final settlement to the goldens:
    it should equal the key rate decided that day.

## Repo legs — 0.28.0

94. *(done in 0.29.0)* **Repo in `value_portfolio`.** Needs a repo instrument form (trade, collateral
    reference), discount and projection curve, calendar and fixing IDs, an open-repo end
    policy, one engine per trade on the shared shocked-curve cache, result fields (S1,
    S2, accrued interest, repurchase amount, collateral exposure and coverage), the
    booking rule (the collateral bond is booked separately by the cash borrower) and
    coverage codes; result contract `v3`.
95. **A typed RUSFAR fixing index.** `FixingHistory.index` names only `ruonia` and
    `key_rate`; repo takes RUSFAR as `index="ruonia"` with its publication rules. Add
    `rusfar` (and `rusfar_cny`); it changes no number. *(downstream)* The hub needs the
    RUSFAR calculation calendar (none on non-trading days, trading weekends and the last
    trading day of the year).
96. **Term RUSFAR and other currencies.** RUSFAR 1W / 2W / 1M / 3M resets; RUSFARCNY and
    USD / EUR / CNY repos (ACT/360 per NFA) with FX on the cash leg.
97. **Capitalised key-rate repo (MOEX FKPO).** Its compounding applies the key rate of the
    closing settlement day to the whole period; rejected today.
98. **NCC Clearing Rules** (`nkcbank.ru`, refused): compounding repo clause 21.12.2,
    margin calls, income transfers on CCP repo, the forecast rate of the risk parameters.
    Margins and income are explicit inputs today.
99. **Repo optionality.** Early termination, the open-repo end (an explicit assumption
    today), the value of the KSU 0.01 % floor near zero (rejected today).
100. **Floating repo Hessian speed and carry explain.** 22 factors cost about 10.7 ms per
     position warm; the interest is linear in the forwards, so an analytic or sparse
     projection block would cut it. *(Rolldown and theta done in 0.31.0.)* Carry has no
     P&L explain.

## Portfolio v3 — 0.29.0

101. *(done in 0.30.0–0.31.0)* **Derivative carry in `horizon_portfolio`.** Every
     product has an engine `.horizon(...)` and a carry adapter; FX products take an
     explicit `fx_horizon` (`"spot_held"` or `"forwards_realised"`). Limits in #108.
102. **Derivatives in the local worker** (`run_local_risk`; its contract v1 is fixed RUB
     bonds), including FX futures (#33) and rate futures (#87).
103. *(done in 0.35.0: `PortfolioScenario.basis_shifts_bp` by factor key)* **Basis
     scenarios.** `PortfolioScenario` cannot shift `FXBASIS`, `XCCYBASIS` or
     `FUTBASIS`; desk scenarios that move a basis need shift entries per basis key.
104. **Shared FX futures engines.** Rate and OFZ futures share one engine per contract;
     FX futures still build one per position (`fx_futures` has no `for_quantity`).
105. **RUSFAR projection spec.** A RUSFAR repo projects on a curve under a RUONIA OIS spec;
     register a RUSFAR projection spec together with the typed `rusfar` index (#95).
106. **Heavy engines in the portfolio.** With the Hessian, CDS cost 30–34 ms per position,
     a two-curve OIS swap and an open floating repo on OIS quotes about 45 ms (engine
     costs, not portfolio overhead); see also #70, #86, #100.
107. *(downstream)* **v3 consumer migration.** The backend stores v3 next to v2, supplies
     the new snapshot inputs (FX spots with value dates, bases, credit curves, futures
     prices, RUONIA fixings with declared gaps) and maps desk products to the position
     types; run imported and computed derivatives in parallel before switching them to
     `computed`.

## Derivative carry and theta — 0.31.0

108. **Carry limits.** A non-RUB CDS is carried with `fx_horizon="spot_held"` only (its
     position names no RUB curve for a forward FX rate); OFZ futures only to a horizon
     spot settlement before delivery; a future that settles inside the horizon reports
     its unrounded model price, not the exchange-rounded final price.
109. *(done in 0.32.0: the decision date is held, the tenor pillars roll)* **Key-rate curve roll.** `roll_key_rate_curve` (used since 0.21.0 by key-rate swaps,
     and now by KEYRATE futures) moves every date by the horizon, the next decision
     date (`current_rate_end`) included. CBR meeting dates are fixed events, so a desk
     may prefer to hold them (a date-anchored key-rate path, rolldown only from the
     tenor pillars). On the test book a 1-day theta of 20 KK-10.26 contracts is +30,000
     RUB from the shifted decision date alone. Owner decision; changing it moves
     key-rate swap horizon numbers (new horizon model version).
110. **Carry model refinements.** Default-weighted (expected) CDS carry over the window;
     rebootstrapping CDS and XCCY curves from held quotes at the horizon instead of
     rolling the built curves; funding of cash received and of variation margin to the
     horizon; a hashed horizon result contract if the backend persists it.

## Desk-monitor review — 0.33.0

Source: `docs/reviews/CORE_REVIEW_FOR_DESK_MONITOR.md` (§6 has the priorities and
estimates). Done in 0.33.0: core brief C1 (open-window puts), C7 (known coupons), C8
(spread kind per instrument), #78, #81, G-spread on any discount curve, floater discount
margin, CS01 / OFZ basis by bucket, spread-kind scenario shifts, vectorised history
metrics.

111. **OFZ–RUONIA basis as a curve factor (R18, full).** 0.33.0 gives the basis risk by
     bucket (`spread_buckets_rub`, first order) and a parallel basis scenario
     (`spread_kind_shifts_bp`). A named basis curve `IR|<basis>|<bucket>` with its
     Hessian block and bucket scenarios needs the discount-only factor split in the
     engine (two-curve treatment of a single-curve floater) and a contract decision on
     factor names. P1, about 3 days.
112. **Callable floaters.** `exercised_instrument` rejects floating legs; the desk redeems
     a callable floater at its call itself (`redeem_at_horizon`). Support `to_call` /
     `maturity` for floating legs (`to_worst` needs projected yields). P2, 1–2 days.
113. **Key rate vs RUONIA basis swaps.** A float-float swap is two swaps today (the
     interface has a «Базис КС/RUONIA» row). A `BasisSwap` on the swap engine with both
     projection curves and a spread leg. P1 when the book holds them, 2–3 days.
114. **Floaters in the history batch.** `bond_metrics_batch` takes fixed flows; a
     floater's history of DM or Z needs its coupons re-projected per date (today about
     14 ms per bond-date through `value_portfolio`). Vectorise the projection over dates
     for a fixed formula. P2, 3 days.
115. **Input build for history.** `BondMetricsInput.from_instrument` costs about 0.09 ms
     per bond-date (`Instrument.cashflows` and `accrued`), five times the batch solve.
     A per-instrument flow table sliced by date would remove it. P2, 1 day.
116. **Exact-decimal pro rata for fixed legs.** `accrued_interest` (OFZ pro rata) rounds
     a float product; a product that is exactly a half kopeck can round down (the floater
     path is exact since 0.33.0: SU29008 77.49 × 13 / 182). No OFZ-PD case was seen on
     2026-10-15 or 2026-10-20 (30 of 30 exact), but fixing it changes accrued in the last
     kopeck, so it needs a stated model change. P2, half a day.
117. **Rate duration of floaters.** `macaulay_duration_years` of a floater is on its
     projected flows (long); the interface needs the rate duration (about the time to the
     next reset). The desk can take `-ΣIR / PV × 10⁴` from the factors; a row field would
     make it explicit. P2, half a day.
118. **MOEX `ZSPREAD` in the EOD history** is not identified (not bp; neither the annual
     nor the continuous Z-spread to the KBD of the date). Until it is, compare core
     Z-spreads with nothing published. P2, investigation.
119. *(downstream)* **ISS data rules found in the review.** `TQOD` settles in USD, so
     `ACCINT` of CNY / EUR bonds there is in USD; `DURATION` is at the published
     `YIELDATWAP`; two offers a week apart without windows (RU000A10CJ84) give
     `YIELDTOOFFER` to the second and `DURATION` to the first; bondization `offertype`
     does not separate puts from calls («Оферта», «Оферта/Погашение»). The adapter
     should pass `Offer.window_start/window_end` from `offerdatestart/offerdateend`.
120. **Floater accrued after publication.** MOEX accrues a coupon set in arrears by the
     realised formula even when the full coupon is published (13 bonds); core keeps pro
     rata on a published amount because family formulas are not per bond (#84). With
     curated formulas, an `in_arrears` flag on the leg could switch it. P2, half a day,
     after #84.
121. **First coupon of new OFZ-PK issues** (SU29031 2026-10-20, 0.51 RUB; as SU29026/27,
     #85): the observation of the first period is not identified without the issue
     terms. P2.

## Non-RUB bonds, phase 2 — 0.34.0

122. **Bund par proxy.** The monitor's EUR fallback `eur_bund_par` (Bundesbank) has no
     registered spec: the Bundesbank's published series and their coupon / compounding
     convention need a primary source before a `eur_bund_par_proxy_v1`. Until then only
     ECB AAA par yields are a EUR proxy. P2.
123. *(done in 0.35.0: `ValuationPolicy.fx_curve_fallbacks`; a proxy only as a fallback,
     with a calibrated `FxBasis`)* **Proxies for FX products.** `ProductContext.foreign_fx_curve` (FX forwards, XCCY)
     accepts `*_discount_factors_v1` and XCCY curves only; a desk that wants FX forwards on
     a UST proxy needs a decision (proxy credit in covered parity) and the spec kind in
     that check. Belongs with the FX package (P7). P2.
124. *(downstream)* **Monitor migration (R8 phase 2 / P5).** Lift `not_rub` in
     `valuation/bonds.py` for CNY / USD / EUR bonds; build `cny_ruonia_xccy_v1` with
     `bootstrap_xccy_curve` from the hub's `cny_ruonia_xccy` quotes and, when the setting
     `proxy_curves_for_risk` is on, the UST / ECB AAA proxies with
     `bootstrap_par_yield_curve` (hub par yields are percent: divide by 100); name them in
     `ValuationPolicy.foreign_curves` (or as `foreign_curve_fallbacks` behind a future
     SOFR / €STR curve); map `IR|<curve>|<bucket>` to the published currency curves; show
     `CurveUse.proxy` and `curve_fallback_from`; set `Instrument.yield_convention` and the
     accrual basis from issue terms (#39). The FX rate is the backend's (#42; R26: MOEX
     19:00). CS01 of a foreign bond is now `CS` on its own curve (owner question R8 open:
     keep or zero).
125. **Proxy refinements.** Treasury's monotone convex spline and T+1 settlement are not
     reproduced (log-linear DF nodes from the curve date); the bill BEY uses a 365-day
     year. Numbers move only inside the proxy's own basis; tighten if a desk compares
     with Treasury's published spot rates. P2.

## FX for any currency, crosses, NDF fixings — 0.35.0

Done in 0.35.0 (FX-forward gap analysis, core packages C1–C3; roadmap review P7):
per-currency foreign specs for every currency (`<ccy>_discount_factors_v1`,
`<ccy>_fx_implied_v1` with `FxImpliedCurveRequest`), crosses without RUB
(`fx_crosses`, `FxCrossInstrument`), NDF fixings (#49, part), points-held RUB risk (#46),
basis scenarios (#103), FX fallbacks to proxies (#123).

126. *(owner, downstream)* **The firm's NDF terms.** Fixing source (CBR official rate,
     MOEX fixing, other), fixing lag and the value date the fixing refers to, and the
     settlement currency of the book's `FXNDF` trades; the TradeLevel report carries
     `FXNDF Reset Date`, `Reset Price` and `Settlement Currency`. Core takes them as
     `NdfConvention` / leg terms; the backend maps the source to `FxFixing.source`.
127. **NDFs settled in a foreign currency and cross NDFs.** An offshore-style USD/RUB NDF
     settles `N · (1 − K / fixing)` USD (a quanto-like ratio, not linear in the forward);
     a cross NDF needs both fixings. Both are rejected today. P2, when the book has them.
128. **Points-held risk as the factor basis.** Today the points-held RUB risk is a row
     field beside the factors; a points-held factor set (`IR|<rub>|*` with points held,
     the foreign curve as the points factor) would need its Hessian and an owner decision
     on what the desk's RUB DV01 means on an implied curve. P2.
129. *(downstream)* **Hub and monitor inputs for FX.** FX swap points in the snapshot
     (hub WP-H1) mapped to `FxSwapPointsQuote` in RUB per one unit; FX spots with value
     dates and settlement calendars (WP-H2, H5); CBR / MOEX fixings by date as `FxFixing`
     with `published_at`; FORTS FX futures prices as `FxOutrightQuote` for USD / EUR.
