# Changelog

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
