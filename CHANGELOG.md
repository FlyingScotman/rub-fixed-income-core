# Changelog

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
