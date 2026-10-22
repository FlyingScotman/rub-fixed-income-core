# Consumers

Snapshot as of 2026-10-14 (audit). "Editable" consumers execute whatever is in the
`rub-fixed-income-core` main checkout: **merging to `main` is a production deployment** for
them (cron jobs and two long-running MCP servers pick it up on next import/restart).

| Project | Declared pin | How installed | Runs | Imports |
| --- | --- | --- | --- | --- |
| market-data-store | `==0.2.1` | editable path (`../rub-fixed-income-core`) | rub-fixed-income MCP server + many crons | `BusinessCalendar`, `CurveBootstrapRequest`, `DiscountCurve`, `OisQuote`, `bootstrap_discount_curve`, `price_cashflows`, `solve_z_spread`, `clean_price_pct_to_dirty_amount`, `canonical_sha256`, KRS (`KeyRate*`, `build_key_rate_forward_curve`, `price_key_rate_swap`), `dates.add_months` |
| moex-market-data-mcp | `==0.1.0` in pyproject, lock resolves editable 0.2.1 | editable path | MCP server + crons | `solve_nominal_annual_yield`, `bond_duration`, `clean_price_pct_to_dirty_amount`, `canonical_json_bytes`, `canonical_sha256` |
| currency-bond-monitor | `==0.2.1` | editable path | crons | `DiscountCurve`, `solve_z_spread` (annual, after manual curve rebase to settlement), `BondCashflow`, `YieldRequest`, `solve_effective_annual_yield` |
| currency-bond-arbitrage-monitor | – | transitive editable (via market-data-store / currency-bond-monitor) | crons | none directly |
| ofz-market-figures-reporter | – | transitive editable | crons | none directly (via market-data-store) |
| market-snapshot-reporter | – | transitive editable | crons | none directly |
| corporate-bond-trade-monitor (dev env) | – | transitive editable via market-data-store | dev only | none directly |
| corporate-bond-trade-monitor PROD release `deploy/prod/releases/0.6.22-alert-readiness-otc-mds-0.7.3` | frozen core 0.2.0 + market_data_store 0.7.3 | frozen release venv | **not scheduled** (wrapper job `3f0e998fbbf7` absent from jobs.json; DB last written 2026-09-09) | – |
| ofz-swaps-v2 | `==0.2.0` (lock says 0.2.0) | editable path; runs 0.2.1 | app | bootstrap, KRS, financed-bond, `YieldRequest`, `annual_schedule`, `year_fraction`, `add_months` |
| corporate-bond-monitor | `==0.1.0` | **non-editable** path; stale `.venv` with 0.1.0 | Windows desk app | `analyze_bond` (continuous Z), `BondTermsV1`…, `verify_result_sha256`, trade grids, `canonical_*` |
| rgbi-futures-arbitrage-monitor | `==0.1.0` | vendored 0.1.0 wheel (not affected by main) | crons | financed-bond projection, accumulation, clean/dirty conversion (scipy brentq locally; no compatibility/analytics) |

## Desk backend (trading-desk-monitor), from 0.11.0

The desk backend is the target runtime consumer (`docs/SYSTEM_CONTEXT.md`). Its entry point
is `value_portfolio` over `Instrument`, `MarketSnapshot` and `Position`, which its hub adapter
builds; results are stored as `rub-portfolio-risk-result.v2`. It pins a tagged release.

**From 0.17.0** the adapter should choose the coupon leg by board, or MOEX accrued interest
is missed by a kopeck on about one corporate bond in ten:

- OFZ: `FixedCouponLeg` (pro rata on the amount);
- corporate bonds: `RateAccrualCouponLeg` with `AccrualBasis.RATE_ACT_365` and each
  coupon's bondization `valueprc / 100` as `RateCouponPeriod.rate`;
- ACT/360 or 30/360 issues: the matching basis, from the issue terms.

Existing `FixedCouponLeg` instruments keep their serialised form and hashes.

**Result contract v3 (derivatives in the portfolio).** A job with a position on a swap,
FX forward or swap, XCCY, CDS, FX / rate / OFZ future or repo returns
`rub-portfolio-risk-result.v3` (`PortfolioRiskResultV3`, a subclass of
`PortfolioRiskResult`). Bond-only jobs keep v2 byte for byte, so stored v2 results,
their hashes and job IDs are unaffected. A reader that only knows v2 rejects v3 (the
schema version differs); `portfolio_result_from_json` now reads both, and
`verify_portfolio_result_v3` checks v3. The adapter supplies the new snapshot inputs
(FX spots with value dates, FX bases per foreign curve, credit curves with recovery,
futures prices and margined prices, RUONIA fixings with declared unpublished days, key-rate
curves dated the business date) and an assumed end for every open repo. See
[portfolio-v3.md](portfolio-v3.md).

**From 0.33.0** (review: [reviews/CORE_REVIEW_FOR_DESK_MONITOR.md](reviews/CORE_REVIEW_FOR_DESK_MONITOR.md) §7):
nothing the backend calls today changes. What it can move into core:

- one `value_portfolio` call per run: `ValuationPolicy(spread_kinds=((ofz_id, "ofz"), ...))`
  instead of one call per spread kind (split `CS` by the rows' `spread_kind`);
- `g_spread_curve_id=<KBD curve ID>` for the G-spread of RUB fixed bonds discounted on
  RUONIA OIS (instead of MOEX's `GSPREADBP` or its own KBD lookups); omit it on a date
  without KBD parameters;
- `discount_margin=True` for the floater DM, `spread_buckets=True` for CS01 and the OFZ
  basis by tenor (the concept pages split one number over two buckets today);
- `horizon="open_put"` with `Offer(window_start=, window_end=)` from bondization
  `offerdatestart` / `offerdateend`, instead of choosing the offer itself (rule F2);
- `require_known_coupons=True` instead of its own check of unknown coupons (C7);
- `Instrument.accrued(trade_date, market)` / `FloatingLeg.accrued_interest` instead of
  `valuation.bonds._floater_accrued`; corporates with
  `FloatingCouponLeg(accrual_basis=AccrualBasis.RATE_ACT_365)` and
  `FloatingCouponPeriod(published_rate=valueprc / 100)`;
- `explain_horizon_pnl(..., fixings=, end_fixings=, ...)` for floaters next to its flash
  PnL (C5);
- `bond_metrics_batch` for histories of yield, duration, G- and Z-spread.

The backend pins core `==0.32.0` and the commit `c0af388`: move the pin to the 0.33.0
commit to use them.

**From 0.34.0** (decision of 2026-10-22, [proposals/kbd-risk-curve.md](proposals/kbd-risk-curve.md), option C):

- register the KBD under `rub_ofz_kbd_v2` (annual-yield risk) and discount OFZ on it:
  `ValuationPolicy(curve_overrides=((ofz_id, kbd_curve_id), ...))`; `ofz_spr01` /
  `spread_buckets_rub` of an OFZ are then its spread to the KBD; corporates and OFZ-PK
  stay on RUONIA OIS;
- KBD floaters (`benchmark == "ZCYC"`): `FloatingCouponLeg(FloatingCouponRule("kbd", ...,
  index_tenor_years=T, fixing_date=FixingDateRule("accrual_start", N, "business_days"),
  rate_rounding_decimals=2), periods, fixing_id, kbd_curve_id)` with a `FixingHistory("kbd",
  ..., tenor_years=T)` of KBD yields on the ZCYC publication calendar; T, N and margins by
  period from the hub (BACKLOG #123);
- `PortfolioScenario(curve_shifts_bp=((kbd_curve_id, shifts),))` moves OFZ and the KBD
  floaters' projection; a basis scenario is a KBD shift alone;
- `basis_view` for the «RUONIA + базис» view of the same factors;
- the fallback duration proxy `-Mac·PV·1e-4` is a continuous-zero DV01: divide by `1 + y`
  to sum with par and annual risk.

**From 0.36.0** (the monitor pinned the branch build "0.35.0", commit `f3cbabc`, for the
FX book and non-RUB bonds; 0.36.0 is that work plus 0.34.0 and never-called perpetuals):

- move the pin to the 0.36.0 commit; the FX book and non-RUB bond APIs are those of
  `f3cbabc`, so a book that does not use the KBD or `never_called` values as before;
- perpetuals and callables: `ExerciseAssumption.never_called()` in
  `ValuationPolicy.exercise_assumptions` instead of an assumed call (BACKLOG #128);
  `horizon_portfolio` rejects a never-called perpetual until #126.

## Deprecations in 0.12.0 and what consumers should move to

Nothing is removed before 1.0. Each call below now emits a `DeprecationWarning` (hidden by
default outside `__main__` and test runners) and returns exactly what it returned before.

| Deprecated | Replacement | Known users |
| --- | --- | --- |
| `analyze_bond` (V1, continuous Z) | `analyze_bond_v2` (annual Z by default; store the convention id) | corporate-bond-monitor (pinned 0.1.0) |
| `solve_continuous_z_spread` | `solve_z_spread(..., settlement_date=s, convention=ZSpreadConvention(compounding=CONTINUOUS))` | none known |
| `price_cashflows` / `solve_z_spread` without `settlement_date` | pass `settlement_date` | market-data-store, currency-bond-monitor |
| `clean_price_pct_to_dirty_amount` | `clean_price_pct_to_dirty_rub` | market-data-store, moex-market-data-mcp |
| `public_schedule.bind_market_data_store_schedule` | build an `Instrument` from the hub snapshot in the backend adapter | none in production |

market-data-store also pins `<0.9`; widen the pin before its next lock (BACKLOG #35).

## Version-pin behaviour on upgrade

Verified with a throwaway project: a consumer that pins `==0.2.1` on an editable path source
**keeps running** after the core becomes 0.3.0 — `uv run`, `uv run --frozen`, and
`.venv/bin/python` all import 0.3.0 (uv does not re-validate the editable version at run
time). The pin and the lock become *stale* (they lie about the running version) until each
consumer re-pins and refreshes its lock — see BACKLOG.

## Compatibility guarantees for 0.3.0

- Every pre-existing signature still works with the same positional/keyword arguments.
- Default numerical outputs are bit-identical to 0.2.1 for valid input.
- Behaviour changes only on invalid input (now raises `ValueError`) and for accrued interest at
  the coupon payment date (now 0).
- `BondAnalyticsResultV1` field set and hash projection are unchanged.

## Impact of 0.3.1 on consumers

0.3.1 keeps every 0.3.0 number for valid input. It only raises `ValueError` where 0.3.0
returned NaN, infinity or an economically invalid value (full list in `CHANGELOG.md`). Editable
consumers pick it up on merge. Check these call sites before merging:

| Project | Affected imports | New failure that callers must handle |
| --- | --- | --- |
| market-data-store | `clean_price_pct_to_dirty_amount`, `price_cashflows`, `solve_z_spread` | clean price ≤ 0 or non-finite (was a dirty price ≤ AI or `nan`); `price_cashflows` overflow (was `inf`) |
| moex-market-data-mcp | `clean_price_pct_to_dirty_amount`, `solve_nominal_annual_yield`, `bond_duration` | clean price ≤ 0 or non-finite; `bond_duration` overflow (was `nan` durations) |
| currency-bond-monitor | `solve_z_spread`, `solve_effective_annual_yield` | `bool` price or cash flow only |
| ofz-swaps-v2 | `project_financed_bond` | non-finite cash event or overflowing balance (was a `nan`/`inf` horizon value) |
| corporate-bond-monitor | `analyze_bond` | none until it upgrades from 0.1.0. `bool` units then give `INVALID_UNITS` |
| rgbi-futures-arbitrage-monitor | – | none: vendored 0.1.0 wheel |

`dirty_rub_to_clean_price_pct` and `analytics.accrued_interest_rub` (now stricter) are not
imported by any consumer listed above. The companion market-data-store branch
`codex/quote-freshness-money-guards-20261014` needs core 0.3.1 or later, so do not install it
against an older core.

## Verification performed before release

Release gate (per owner instruction, lightweight only): core suite, the OFZ 26238 numeric
parity probe, a bitwise default-path comparison against 0.2.1, and an import/signature smoke
check (every `rub_fixed_income` symbol imported by each consumer exists; legacy positional and
keyword call shapes return byte-identical numbers vs 0.2.1 on a synthetic bond/curve).

Before that instruction, consumer test suites had already been run once with the candidate
first on `PYTHONPATH` (confirmed `rub_fixed_income.__file__` pointed at the candidate). Further
consumer suite runs were then intentionally stopped. Results obtained (baseline → candidate):

| Project | Baseline (0.2.1) | 0.3.0 candidate |
| --- | --- | --- |
| market-data-store | 291 pass / 2 fail (pre-existing HTTPS_PROXY env tests) | 291 / 2 (same tests) |
| moex-market-data-mcp | 270 pass | 270 pass |
| currency-bond-monitor | 70 pass | 70 pass |
| currency-bond-arbitrage-monitor | 25 pass | 25 pass |
| ofz-market-figures-reporter | 132 pass / 1 fail (pre-existing proxy test) | 132 / 1 (same test) |
| corporate-bond-trade-monitor | 599 pass | 599 pass |
| market-snapshot-reporter | 18 pass | 18 pass |
| ofz-swaps-v2 (`uv run --frozen --with truststore`) | 55 pass | 55 pass |
| corporate-bond-monitor (throwaway `uv run --isolated` env, numpy added) | per-file all pass; `tests/test_headless_analytics.py` hangs after 8 tests with both cores (pre-existing) | same |
| rgbi-futures-arbitrage-monitor | vendored 0.1.0, not affected | skipped |
