# Changelog

## 0.13.0 — caller-held curves and the 2k-position benchmark

Roadmap item 0.7 (completed). No identifier or number changes: every pinned hash and
fixture still matches, and results are byte-identical with or without the new caches.

- **Caller-held curves.** `PrebuiltCurve(request, shifts, curve)` with stable string keys
  `request_id` / `curve_key`; `ShockedCurveCache.export()` and `.seed(entries)`; and
  `value_portfolio(..., curve_cache=...)`. The backend can persist the base and shocked
  OIS curves of one job and seed the next with them. `seed` accepts a curve only if it
  reprices its (shocked) quotes within 1e-7 bp and matches the request's date,
  convention and model. Otherwise it raises `InvalidTerms` and adds nothing.
- **Speed without changing numbers.** Every position on a curve now shares one
  validated snapshot of it (for immutable inputs), so the per-curve caches below are
  shared too:
  - the canonical identity of a curve (a 1,100-node KBD curve was re-hashed for every
    position);
  - bucket weights and base discount factors of zero-rate shocks;
  - one shared view per shock vector;
  - a bounded discount-factor memo per curve.

  The shocked-curve cache skips rehashing the job's request. All of these caches live
  outside the dataclass fields and stay out of equality, hashing and pickles.
- **Benchmark.** `benchmarks/portfolio_bench.py` values a synthetic book of distinct
  bonds (bullets, amortizers, confirmed puts, 1–15Y) on KBD and on 17 RUONIA OIS quotes.
  At 2,000 positions on Linux / Python 3.11, in ms per position:

  | Curve | First order | Hessian | Hessian + 5 scenarios | Seeded cache |
  |---|---|---|---|---|
  | KBD | 3.2 | 9.3 | 9.6 | — |
  | OIS | 2.3 | 10.0 | 10.3 | 8.1 |

  Before 0.13.0 at 200 positions: KBD 9.1 / 30.4, OIS 5.4 / 31.3 ms per position.
  A non-gating CI job runs the benchmark on windows-latest / Python 3.11 and posts the
  table to the job summary.
- mypy now also checks `benchmarks/`.

## 0.12.0 — API consolidation (deprecations) and documentation

Roadmap item 0.9. No behaviour or identifier changes: every deprecated call returns
exactly what it returned before and only adds a `DeprecationWarning` at the caller's line
(hidden by default outside `__main__` and test runners). Removal is planned for 1.0.

- Deprecated: `analyze_bond` (V1, continuous Z; use `analyze_bond_v2`),
  `solve_continuous_z_spread`, `price_cashflows` and `solve_z_spread` without
  `settlement_date`, the `clean_price_pct_to_dirty_amount` alias, and
  `public_schedule.bind_market_data_store_schedule` (the retired market-data-store payload;
  the backend adapter builds an `Instrument` instead, ADR-003).
- The test suite turns this library's deprecation warnings into errors; tests that pin
  legacy behaviour say so explicitly, and `tests/test_deprecations.py` checks every warning.
- Private risk helpers shared by `risk` and `fx_futures` (and `local_worker`) moved to
  `_risk_common.py`; nothing imports private names from `risk` any more. Public names
  (`RISK_BUCKETS`, `RISK_PILLARS`, `ShockedCurveCache`, `key_rate_weights`, ...) are still
  importable from `rub_fixed_income.risk`. `ShockedCurveCache.__module__` is now
  `rub_fixed_income._risk_common`.
- Docs: API reference, architecture module map and data flow, consumer migration table,
  backlog and roadmap status refreshed for 0.9.0–0.12.0.

## 0.11.0 — KBD curve, curve-spec registry, desk factors, portfolio valuation

Roadmap items 0.6, a minimal 0.5 and §2 (factor vocabulary, coverage codes): the
core side of the M1 vertical slice (an OFZ book on the KBD curve).

- **KBD / G-curve** (`kbd.py`): `ZcycParameters`, `kbd_zero_rate_bp`,
  `kbd_effective_yield`, `g_spread_bp` and `kbd_discount_curve`. It reproduces MOEX's
  published standard yields within 0.01 bp on archived publications
  (`tests/fixtures/kbd/`, copied from the hub's fixtures). The materialised curve
  (daily nodes to 2Y, weekly to 5Y, monthly to 30Y) is within 0.005 bp of the formula
  on every day to 30Y, so Z-spread, bucket risk, the Hessian and scenarios work on it
  unchanged. Model `moex-kbd-gcurve-v1`.
- **Curve-spec registry** (`curve_specs.py`): `CurveSpec`, `CURVE_SPECS`,
  `get_curve_spec` (raises `UnknownCurveSpec`). Registered: `rub_ofz_kbd_v1`,
  `rub_ruonia_ois_legacy_v1` (today's bootstrap, named exactly) and
  `rub_key_rate_swap_v1`. The exchange-conformant `rub_ruonia_ois_moex_v2` follows later.
- **Desk factors** (`factors.py`): `ir_factor` (`IR|<curve>|<bucket>`), `CS`,
  `fx_factor` (`FX|<ccy>`), `fx_basis_factor`; `FactorRisk` with a sparse upper-triangle
  Hessian; `bond_factor_risk` and `fx_futures_factor_risk`. First order and Hessian are
  central with the same bump (DEC08).
- **Portfolio valuation** (`portfolio.py`): `value_portfolio(run_id, as_of, snapshot,
  instruments, positions, policy, scenarios)` over the 0.10.0 domain model. It values
  fixed-coupon RUB bonds (bullet, amortizing, confirmed put) on a KBD curve, a prebuilt
  curve or OIS quotes, with settlement from the instrument's calendar. Each position
  gets PV, prices, accrued, Z-spread, effective yield, Macaulay duration, G-spread on
  KBD, desk-factor risk and JTD. Everything else is a coded `Rejection` in coverage
  (`UNSUPPORTED_PRODUCT`, `MISSING_MARKET_DATA`, `CALENDAR_OUT_OF_RANGE`,
  `UNKNOWN_CURVE_SPEC`, `INVALID_TERMS`, `CALCULATION_FAILED`, `CANCELLED`,
  `TIME_BUDGET_EXCEEDED`). Results carry the curves and calendar identities used and
  covered factor totals, and are hash-verified as `rub-portfolio-risk-result.v2`
  (`portfolio_result_to_json` / `_from_json` / `verify_portfolio_result`).
  `run_local_risk` and the v1 contract are unchanged.
- `CurveEntry.curve` may also hold `ZcycParameters`; the valuation builds it once.
- `DiscountCurve.discount_factor` finds the node interval by bisection over cached log
  discount factors instead of a linear scan. Results are bit-identical (same values,
  same interval); dense curves such as KBD become cheap to read.
- Indicative speed (Linux, Python 3.11, 200 positions on KBD): 9 ms per position with
  first-order risk, 29 ms with the Hessian. The 2k-position benchmark on Windows is
  roadmap item 0.7.

## 0.10.0 — domain model and the coupon/principal split

Roadmap package 0, items 0.2 and 0.3. Additive: existing entry points, results and
identifiers are unchanged.

- **`Cashflow`** (`cashflows.py`): a `BondCashflow` split into `coupon` and
  `principal`, with the coupon's `accrual_start`/`accrual_end` and a
  `CashflowKind` (`COUPON`, `AMORTIZATION`, `MATURITY`, `OFFER_REDEMPTION`). Build it
  with `Cashflow.split(...)`; `amount` is always `coupon + principal`. Because it is a
  `BondCashflow`, every pricing and risk entry point accepts it and gives the same
  numbers and identifiers as the totals.
- **`accrued_interest(cashflows, settlement)`**: linear accrued interest from the
  split, rounded half-up to 0.01 (same formula as `analytics.accrued_interest_rub`).
- **Domain types** (`domain.py`):
  - `Instrument` (terms only): `ProductType`, currency, initial face, a
    `FixedCouponLeg` of known `CouponPeriod`s or a `FloatingCouponLeg` (rule,
    periods, fixing ID), a `PrincipalLeg` of `Redemption`s, `Offer`s, calendar ID and
    settlement lag. `cashflows(after, horizon, offer)`, `outstanding_face(on)`,
    `horizon("maturity" | "confirmed_put", settlement)`, `accrued(settlement)`.
  - `MarketSnapshot`, shared by all positions: `CurveEntry` (curve ID + curve-spec
    ID), `CalendarEntry`, `FixingEntry`, `FxRate`, `BondPriceObservation`,
    `SnapshotEvidence`, `snapshot_id`, `known_at`. Lookups raise `MissingMarketData`.
  - `Position`: signed quantity, book, `AccountingCategory`, currency, recovery.
  - All validate on construction (typed `InvalidTerms`) and round-trip through
    `to_dict`/`from_dict`.
- `SnapshotEvidence` moved from `local_worker` to `domain`; `local_worker` re-exports it.

## 0.9.0 — foundations: calendars, canonical hashing, serialization, typed rejections

Roadmap package 0, items 0.1 and 0.4, and part of 0.9. Every identifier that
hashed before is byte-identical (pinned in `tests/test_hash_stability.py`).

- **Calendars with a covered range.** `BusinessCalendar` gains `valid_from` and
  `valid_to` (both or neither). A bounded calendar raises `CalendarRangeError` for
  any date outside the range instead of assuming Mon–Fri; settlement, schedules
  and date adjustment inherit this. Unbounded calendars behave as before.
  - `calendar_fingerprint` includes the range only for bounded calendars, so
    existing fingerprints and every identifier built on them are unchanged.
  - New `calendar_identity(calendar) -> (version, fingerprint)` for results.
  - Holidays and working days are frozen on construction. A caller's mutable set
    can no longer change a calendar after it is built.
  - New validation: a non-empty `version`, dates only, no date that is both a
    holiday and a working day, overrides inside the range.
- **Canonical hashing** moved to `rub_fixed_income.canonical`.
  `rub_fixed_income.analytics.canonical_json_bytes` / `canonical_sha256` remain
  importable. Values that used to raise `TypeError` now have canonical forms:
  enums as their value, sets as sorted lists, dataclasses nested in mappings or
  sequences, and date or enum mapping keys.
- **Serialization.** New `to_dict(value)` / `from_dict(cls, data)` for core's own
  dataclasses (strict field sets, enums by value, ISO dates, UTC-aware
  timestamps, frozensets, Decimal strings, unions resolved by shape). The local
  worker's result decoder now uses it; the v1 result fixture round-trips byte for
  byte. `ScheduleConvention.to_dict()` (the provenance summary) is unchanged.
- **Typed rejections** in `rub_fixed_income.errors`: `CoreRejection` with a stable
  `code`, and `UnsupportedProduct`, `MissingMarketData`, `CalendarRangeError`,
  `UnknownCurveSpec`, `InvalidTerms`. All are `ValueError` subclasses.
- `DiscountCurve.flat_continuous_rate` is public; `risk.py` no longer reads the
  private field. The dead `analytics._price_at_yield` is removed.
- Tooling: CI on Windows and Ubuntu with Python 3.11; `uv run mypy` passes on the
  package and tests; the QR probe test is no longer shipped in the sdist.
- **Version pin:** market-data-store pins `>=0.8.0,<0.9`. Its editable install keeps
  running, but its next `uv lock` needs the pin widened (BACKLOG #35).

## 0.8.1 — FX futures rate risk

- New `rub_fixed_income.fx_futures`: `FxFuturesContract`, `FxFuturesRiskRequest`,
  `FxFuturesRiskEngine`, `FxFuturesScenario` and their result types.
  - The price is covered parity, `S * DF_for(T)/DF_for(s) / (DF_rub(T)/DF_rub(s)) * exp(b*tau)`.
    The basis `b` is calibrated from a market futures price and frozen under shocks.
  - Risk: 11-bucket and parallel DV01 for both the RUB and the foreign curve, basis01,
    FX delta in foreign units and in RUB per 1 %. Each curve is in `continuous_zero` or
    `ois_quote` basis, as for bonds, and the two may differ.
  - Exact scenarios split P&L into RUB rates, foreign rates, basis, spot and their
    interaction. The full 23-factor Hessian is available.
  - Daily variation margin: P&L is not discounted. Model `rub-fx-futures-parity-risk-v1`.
  - See `docs/fx-futures.md`.
- Internal: curve snapshotting, shocking and the central Hessian in `risk.py` are now
  shared helpers. Bond numbers are bit-identical to 0.8.0 (risk, Hessian, scenarios,
  Taylor comparison in both bases).
- The version stays below 0.9 so that market-data-store's `>=0.8.0,<0.9` pin still
  resolves. The change is additive.

## 0.8.0 — fixing-aware floating coupons

- New `rub_fixed_income.floating`: `FixingHistory`, `FloatingCouponRule`,
  `FloatingCouponPeriod`, `FloatingLeg` and `FloatingCouponProjection`.
  - Coupons join realized RUONIA or key-rate fixings with overnight forwards of a
    projection curve.
  - Daily rates use business-day carry, with an optional lookback.
  - Averaging is compounded or arithmetic; margin and both day counts are explicit.
  - Rounding is half-up and applies only to coupons fully fixed by history.
  - Published coupons are used as cash, and a zero placeholder is rejected.
- History is point-in-time. A fixing on or after the valuation date is rejected,
  and a RUONIA gap before the latest fixing is a `LookupError`. The latest fixing
  carries over at most `max_unpublished_business_days` trailing unpublished days,
  and those days are reported.
- `BondRiskRequest(floating_leg=...)` re-projects coupons off every shocked curve
  and freezes known fixings and published coupons. The model is single-curve, with
  the spread as a discount margin. Results carry `rub-floating-coupon-risk-v1`.
- Fixed-cashflow results, identities and the local worker are unchanged.
  `BondRiskEngine` gains `projected_coupons`, which is empty for fixed bonds.
  See `docs/floating.md`.

## 0.7.0 — worker integration readiness

- Share shocked OIS curves across all positions of a job (`ShockedCurveCache`);
  each engine also reuses its own. Numbers are byte-identical to 0.6.0. On the
  synthetic 10-position, 11-quote, 3-scenario job in `docs/local-worker.md` OIS
  risk with Hessian drops from ~27 s to ~2.2 s (Linux container, Python 3.13).
- `run_local_risk(..., should_cancel=..., time_budget_seconds=...)`: checks
  before each position and scenario; skipped work stays in coverage as failed
  rows and `interruption` names the reason. `job_id` is unchanged by either.
- Versioned result contract `rub-local-portfolio-risk-result.v1`:
  `schema_version`, `result_sha256`, `verify_local_risk_result`,
  `local_risk_result_to_json` and strict `local_risk_result_from_json`
  (unknown schema, missing or extra fields and hash mismatches are rejected).
  A frozen fixture in `tests/fixtures/` guards the v1 field set.
- `LocalRiskRunResult` gains `interruption`, `schema_version` and
  `result_sha256` as trailing defaulted fields; positional construction and
  every 0.6.0 number are unchanged. `job_id` includes the library version, so it
  changes with this release as with any other.

## 0.6.0 — local portfolio worker

- Join validated public fixed-RUB snapshots to local signed positions, calculate
  central risk, optional full Hessian, explicit-recovery JTD and exact scenarios.
- Preserve per-position and per-scenario failures with complete/partial/failed
  coverage, content-bound job identity and nullable incomplete portfolio totals.
- Add a strict bridge from market-data-store published fixed schedules; preserve
  amortization/confirmed puts and reject projected or mismatched inputs.
- Record floater fixing API, historical known-at selection and RUONIA coverage gaps.
- Existing pricing defaults and floater service behavior remain unchanged.

## 0.5.0 — 2026-10-15

- Add the full fixed-RUB rate/spread Hessian and a central Taylor P&L comparison against exact scenarios.
- Add signed immediate-default P&L with mandatory recovery on explicit outstanding principal.
- Add a pure local fixed-bond adapter with clean/dirty conversion, snapshot/run identities, horizon guards and required matching zero/OIS factor basis.
- Preserve existing pricing and forward risk defaults. Scope and synthetic numerical references are documented in `docs/second-order-local-risk.md`.

## 0.4.0

- Add explicit fixed-cashflow RUB risk: 11 triangular key-rate buckets, normalized KRD, signed DV01, CS01/OFZ spread sensitivity, and full same-settlement scenarios.
- Support continuous-zero overlays and original OIS quote bumps with rebootstrap; freeze a supplied or calibrated spread under curve shocks.
- Record basis, spread convention, finite-difference method and content identities; validate and snapshot inputs. Existing pricing defaults are unchanged.
- Add independent analytical risk/scenario tests and document scope, units and limitations in `docs/risk.md`.

## 0.3.1 — 2026-10-14

Validation release. Numbers for valid input are unchanged from 0.3.0; every change below
turns a NaN/inf result or a silently wrong value into `ValueError`. Contract summary:
`docs/money-validation.md`.

### Fixed (behaviour changes)

- `clean_price_pct_to_dirty_rub` / `clean_price_pct_to_dirty_amount` now reject a clean price
  `<= 0` (was accepted: `clean=-5` → dirty `-40.0`), any NaN/inf/`bool` input, and a result
  that overflows.
- `dirty_rub_to_clean_price_pct` now rejects a dirty price `<= 0`, a dirty price at or below
  accrued interest (implied clean `<= 0`; was returned as `0.0` or negative), any NaN/inf/`bool`
  input, and an overflowing result. NaN input used to return `nan`.
- `analytics.accrued_interest_rub` now runs the full `BondTermsV1.validate()` first, so it
  also raises for a horizon inconsistent with maturity (`"maturity horizon date must equal
  maturity date"`, `"confirmed put horizon must precede maturity"`) and for invalid nominal
  or coupon, including on the coupon payment date. It used to return accrued interest for such
  terms. `analyze_bond` / `analyze_bond_v2` already validated terms, so they are unaffected.
- `project_financed_bond` validates every cash-event amount (NaN/inf/`bool` → `"cash-event
  amount must be finite"`; was propagated into a `nan` horizon value), rejects `bool`
  initial dirty cash and accumulation factors, and raises `"financing balance result must be
  finite"` instead of returning `inf`. Signed finite cash events stay valid: an expense of
  `-50` increases the funding balance.
- `price_cashflows` raises `"cashflow price result must be finite"` instead of returning
  `inf`; `bond_duration` raises instead of returning `nan` durations with `inf` PV. Net
  zero PV from signed flows still returns `0.0`.
- `bool` prices and cash flows are rejected by `solve_nominal_annual_yield`, `solve_z_spread`,
  `solve_continuous_z_spread`, `solve_effective_annual_yield`, `price_cashflows`,
  `bond_duration`, and `BondTermsV1` / `MarketObservationV1.validate()` (so `analyze_bond`
  returns `INVALID_UNITS`).

### Changed

- `rub_fixed_income.__version__`, `pyproject.toml` and `uv.lock` all report 0.3.1. A
  contract test now keeps `__version__` equal to the project version. The lock had still
  said 0.2.1 at 0.3.0.

## 0.3.0 — 2026-10-14

Additive release. Defaults reproduce 0.2.1 numbers bit for bit for valid input; behaviour
changes only on invalid input and for accrued interest on the coupon date.

### Added

- `conventions` module: `DayCount` (ACT/365F, ACT/360, ACT/ACT-ISDA), `Compounding` with
  `ANNUAL`/`SEMI_ANNUAL`/`QUARTERLY`/`MONTHLY`/`CONTINUOUS`, `BusinessDayConvention`,
  `adjust_date`, `SettlementConvention` (T+n on a calendar), `ScheduleConvention` +
  `generate_schedule` (unadjusted roll → adjusted dates, no adjusted-maturity stub),
  `YieldConvention`, `ZSpreadConvention`, `CurveConvention`, `calendar_fingerprint`. All
  immutable, hashable, with stable `identifier` strings.
- `solve_z_spread` / `price_cashflows`: keyword `settlement_date` (settlement-forward DFs,
  time from settlement, flows on/before settlement dropped) and `convention`
  (`ZSpreadConvention`, incl. continuous and alternative day counts).
- `solve_nominal_annual_yield` / `bond_duration`: keyword `convention` (`YieldConvention`).
- `DiscountCurve.convention` and `CurveBootstrapRequest.convention`: curve time day count,
  fixed-leg schedule (legacy or adjusted) and fixed-leg day count; `DiscountCurve.flat(...,
  convention=)`; `DiscountCurve.fixed_leg_schedule`; `zero_rate` accepts `CONTINUOUS`.
- `analyze_bond_v2` and `BondAnalyticsResultV2` with `yield_convention` and
  `z_spread_convention` fields; default Z-spread **annual**. `verify_result_sha256` accepts
  V1 and V2. `V1_YIELD_CONVENTION`, `V1_Z_SPREAD_CONVENTION` constants;
  `CalculationPolicy.validate()`.
- Single internal solver `_solver.bisect` (bracket check, non-finite detection, iteration
  count, max-iteration error) replacing five duplicated bisection loops.
- Documentation: `docs/ARCHITECTURE.md`, `docs/CONVENTIONS.md`, `docs/API.md`,
  `docs/CONSUMERS.md`, `docs/BACKLOG.md`.

### Fixed (behaviour changes)

- `solve_z_spread`, `solve_continuous_z_spread`, `price_cashflows`, `solve_nominal_annual_yield`,
  `bond_duration`, `solve_effective_annual_yield`, `bootstrap_discount_curve`,
  `DiscountCurve(...)` now raise `ValueError` for NaN/inf prices, cash flows, spreads, yields,
  quotes and discount factors. Previously e.g. `solve_z_spread(flows, nan, curve)` returned
  `4.99999…` (500 %) and `solve_nominal_annual_yield(dirty_price=nan)` returned ≈ −1.0.
- `solve_nominal_annual_yield` raises "not bracketed" when the root would lie below
  `−0.99·m` instead of silently returning the domain edge.
- Every solver raises on a non-finite objective value during bisection instead of continuing.
- `solve_z_spread` raises when no cash flow lies after settlement.
- `accrued_interest_rub` returns 0.0 when settlement equals the coupon payment date (was the
  full coupon; MOEX convention is 0). `analyze_bond` results change only for that date.
- `analyze_bond` returns `CalculationFailure("INVALID_UNITS")` for a non-finite or inverted
  `CalculationPolicy`.
- Error messages for unbracketed roots now read `"<solver> root is not bracketed"` (all
  still contain "not bracketed"); `analyze_bond` failure codes are unchanged.

### Unchanged on purpose

- `analyze_bond` (V1) still reports a **continuous** Z-spread so stored V1 hashes stay
  reproducible; migrate to `analyze_bond_v2` for annual (see `docs/BACKLOG.md`).
- `annual_schedule` keeps its legacy stub; default OIS bootstrap uses it.
- `solve_z_spread` / `price_cashflows` without `settlement_date` value at the curve date.

## 0.2.1 and earlier

Not tagged: repository history begins at a commit already versioned 0.2.1 (see BACKLOG).
