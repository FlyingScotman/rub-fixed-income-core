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

29. **P1 — fixing-aware floater core and API consistency (C05).** *Core part done
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

33. **FX futures in the local worker and the monitor.** The 0.8.1 core
    (`docs/fx-futures.md`) gives rate risk on the RUB and foreign curves, basis,
    FX delta, scenarios and the Hessian for a single position. Remaining:
    - a public FX futures snapshot (contract terms from the exchange specification,
      settlement price, spot and its value date, both curves, evidence);
    - the delivery date from the official calendar (P0 #1);
    - positions and coverage in `run_local_risk`;
    - a new result-contract version, because v1 fixes the bond field set;
    - an agreed choice of the foreign curve (OIS, or implied from FX swaps).
34. **OTC FX forwards and swaps.** Their P&L is paid at `T` and needs discounting
    on the RUB curve. This is a separate settlement style, not the futures model.
35. *(downstream, done in market-data-store PR #5)* **market-data-store core pin.** It
    pinned `>=0.8.0,<0.9`; the PR widens it to `<1.0`, regenerates the lock and moves off
    the `clean_price_pct_to_dirty_amount` alias. Its `price_cashflows`/`solve_z_spread`
    calls without `settlement_date` still warn; moving them is a behaviour change for the
    store's owner before core 1.0.
