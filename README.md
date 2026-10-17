# RUB Fixed-Income Core

A deterministic Python 3.11+ core shared by RUB fixed-income applications. It owns ordered cash events, accrued interest and contractual money rounding, clean/dirty conversion, yield-to-horizon, Macaulay/modified duration, discount curves, Z-spread, canonical hashes, validated exact-pricer trade grids, and an identified KRS-to-key-rate forward-average projection.

The KRS module prices the MOEX IRS KEYRATE contract as TOM/MODFOL, Act/Act (ISDA), annual fixed versus quarterly `KEYRATE-AVERAGE`, discounted on an explicit external curve. `build_key_rate_forward_curve(...)` fixes the current official key rate through the next policy effective date and solves one equivalent forward-average state per adjacent KRS market-pillar interval. It requires a full-rank square exposure system, exactly reprices bid/mid/ask quote-side scenarios, and reports SVD/condition/inverse-sensitivity diagnostics. Quote-side scenarios are not promised to be pointwise ordered.

The package intentionally contains no HTTP, SQLite, MCP, FastAPI, market-data adapter, proprietary, application, or UI code. `analyze_bond(...)` is the deep calculation interface; Taylor duration/convexity is not an authoritative metric path.

`result_hash_projection(result)` exposes the canonical public hash projection (all result
fields with `result_sha256` fixed to an empty string). `verify_result_sha256(result)` accepts
either a result DTO or its JSON-deserialized mapping and verifies the declared hash.

Trade grids persist a `GridIdentity` covering terms and cash-flow hashes, settlement,
horizon, metric/units, optional Curve World instance, pricer version, and grid-policy
version. Callers pass the current identity to `lookup_trade_metric`; absent or mismatched
context takes an authoritative exact fallback with a stable reason code. Accepted in-range
lookups use only the build-time held-out/adaptively validated PCHIP grid and its independent
linear guardrail; they do not call the exact solver.

## Documentation

- [System context](docs/SYSTEM_CONTEXT.md) — where this library fits in the Trading Desk Monitor, its obligations, and links to the system architecture
- [Product roadmap](docs/PRODUCT_ROADMAP.md) — **development plan**: foundations (package 0) and product packages 1–9 with acceptance criteria
- [Architecture](docs/ARCHITECTURE.md) — module map, data flow, invariants, error model
- [Conventions](docs/CONVENTIONS.md) — day counts, compounding, calendars, schedules, curve, yield, Z-spread, AI equations
- [API](docs/API.md) — public functions, units, errors
- [Consumers](docs/CONSUMERS.md) — who uses the core and how it is pinned
- [FX futures](docs/fx-futures.md) — rate, basis and spot risk of FX futures against RUB
- [FX forwards and swaps](docs/fx-forwards.md) — OTC FX forwards and FX swap legs by covered parity
- [Swaps](docs/swaps.md) — seasoned RUONIA OIS and key-rate swaps
- [Callables](docs/callables.md) — exercise assumptions, perpetuals and constant-CPR pass-throughs
- [Backlog](docs/BACKLOG.md) — remaining fixes and downstream migrations
- [Changelog](CHANGELOG.md)

Conventions are explicit inputs (`rub_fixed_income.conventions`); defaults reproduce 0.2.1
numbers. Z-spread desk default is annual (`m=1`); monthly (`m=12`) and continuous are available
via `ZSpreadConvention`. `solve_z_spread(..., settlement_date=...)` values at settlement.

## Development

```bash
uv sync --extra dev
uv run pytest -q
uv run ruff check .
uv run mypy rub_fixed_income
uv build
```

The development commands above use `uv`, the Python project/package manager:

- `sync` synchronizes the project environment; `--extra dev` includes the optional development dependencies.
- `run` executes a tool in that environment. `pytest` runs tests; `-q` reduces its output.
- `ruff check .` runs Ruff's lint checks; `check` selects checking and `.` means the current directory.
- `mypy rub_fixed_income` runs the type checker on the named package directory.
- `build` builds the distribution artifacts.

## Fixed-cashflow risk and scenarios (0.4.0)

`BondRiskRequest`, `BondRiskEngine`, and `BondScenario` provide signed RUB DV01/KRD, CS01/OFZ spread risk, and exact same-date repricing. Curve basis and spread convention are explicit; a calibrated base spread stays frozen under curve shocks. See [risk contract and example](docs/risk.md) for inputs, methods, identities and supported scope.

## Second-order risk, JTD and local boundary (0.5.0)

The fixed-RUB engine now provides a full 12-factor Hessian, central Taylor P&L with exact comparison, and signed immediate-default P&L with mandatory recovery. A pure local adapter binds validated schedules, market and curve snapshots to explicit zero or OIS quote factors. See [contract and executable example](docs/second-order-local-risk.md).

## Floating coupons (0.8.0)

`FloatingLeg` prices RUONIA and key-rate floaters. Coupons combine known
fixings, taken from an explicit point-in-time `FixingHistory` with RUONIA gap
checks, with curve projection. `BondRiskRequest(floating_leg=...)` freezes the
known part under every bump. See [floating coupons](docs/floating.md).

## FX futures rate risk (0.8.1)

`FxFuturesRiskEngine` prices FX futures against RUB by covered parity with a
calibrated, frozen basis. It reports RUB-curve and foreign-curve bucket DV01,
basis01, FX delta, exact scenarios and the full Hessian. See [FX futures](docs/fx-futures.md).

Portfolio jobs and the public schedule bridge: [local worker](docs/local-worker.md). 0.7.0 adds shared OIS curve reuse, cooperative cancellation/time budgets and a versioned, hash-verified JSON result contract for storage.
