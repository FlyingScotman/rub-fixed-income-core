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

## Development

```bash
uv sync --extra dev
uv run pytest -q
uv run ruff check rub_fixed_income tests
uv run mypy rub_fixed_income
uv build
```
