# Local fixed-RUB portfolio worker (0.6.0, extended in 0.7.0)

`rub_fixed_income.local_worker.run_local_risk` joins public per-instrument snapshots
to local signed positions and runs central DV01/KRD, CS01 or OFZ spread risk, the
full Hessian (optional), explicit-recovery JTD and exact same-settlement scenarios.
The module performs no network, database or filesystem operations.

## Input boundary

- `PublicBondSnapshot` contains per-bond RUB amounts, clean price in percent of
  current nominal, accrued interest in RUB, resolved contractual cashflows,
  explicit spread convention, curve and evidence. No positions are stored in it.
- Each market/schedule/curve `SnapshotEvidence` has source/snapshot IDs and
  offset-aware observation, known and validity timestamps. The worker requires
  `observed_at <= known_at <= as_of <= valid_until` independently for each input.
  The caller chooses the freshness policy and supplies validity; none is guessed.
- Complete schedule, confirmed redemption and a calendar version are mandatory
  assertions from the upstream data validator. The worker cannot independently
  establish official holidays, issue terms, currency, missing coupons or source
  authenticity. Evidence checks are not a historical revision-selection API.
- `LocalPosition` contains position/instrument IDs, signed number of bonds and
  explicit principal-recovery fraction. Two books may own the same instrument
  under different position IDs. Zero quantities remain in coverage.
- A run explicitly selects `continuous_zero` or `ois_quote`. The former accepts
  discount factors; the latter accepts original OIS quotes and reboots the curve
  under each shock. These exposures and shock vectors must not be mixed.

`rub_fixed_income.public_schedule.bind_market_data_store_schedule` accepts the
actual response shape from `get_projected_bond_cashflows`. Pass an empty-flow
`PublicBondSnapshot` with independently validated terms/quote/curve evidence and
the financing curve build ID from that curve's provenance. The bridge checks
instrument, settlement, horizon, fixed formula classification, exact-published
quality, warnings, absence of projection, build ID, ordered dates, nonnegative
coupon/principal amounts, totals and positive terminal principal. It preserves
amortization and confirmed-put payments and hashes the full provider response.
It neither fetches data nor treats the service's quality label as proof of
contractual completeness. Unsupported/missing source records must retain their
positions in the worker request so they appear as explicit coverage failures.

## Result and failure semantics

`status` describes the whole requested job. `coverage` describes base risk, and
each scenario has its own coverage. An instrument failure preserves successful
instruments; a scenario failure preserves base risk and other scenarios. A job
with successful base risk but a failed scenario has `status="partial"`.

`total_pv_rub` and `total_pnl_rub` are `None` unless their own coverage is complete.
`covered_pv_rub` and `covered_pnl_rub` are labelled subtotals, never zero-filled
portfolio totals. A complete empty book is distinct from a wholly failed book.
Counts measure positions, not market-value coverage (failed values are unknown).
Duplicate IDs and malformed job-level inputs raise before calculation. Unexpected
programming errors propagate; they are not disguised as financial data failures.

Base spread is calibrated once per position and frozen across all scenarios.
Central first derivatives use the same bump as the Hessian; legacy default forward
risk remains unchanged. JTD is quantity × (recovery × outstanding principal −
dirty unit PV). No implicit recovery, funding, carry, elapsed-time scenarios,
floating/FX-linked cashflows or optional-exercise model is introduced.

The public bundle ID binds all supplied normalized records and source payload IDs.
The job ID also binds library/model versions, run/as-of, basis, all positions
(including missing instruments), recovery, scenarios and calculation settings.
Outer per-position calculation IDs and scenario IDs bind the job. Nested core
IDs retain their narrower mathematical meaning; use outer IDs for storage/cache
keys. Corrections under reused provider labels still change content IDs.

## Performance: shared shocked curves (0.7.0)

In `ois_quote` basis every shocked price needs a rebootstrapped curve. Central
risk, the full Hessian and scenarios repeat the same ~250 shift vectors for every
position on the same curve. `run_local_risk` therefore keeps one
`ShockedCurveCache` per job, keyed by exact curve request content and shift
vector; each standalone `BondRiskEngine` keeps its own. Bootstrapping is
deterministic, so results are byte-identical to an uncached run. Failed
bootstraps are not cached; past `max_entries` (default 4096) curves are built
but not stored.

Synthetic job (10 positions × 10 flows, 11 OIS quotes, 3 scenarios, Linux
container, Python 3.13, best of two runs including calibration):

| Basis | Without Hessian | With Hessian |
| --- | --- | --- |
| `continuous_zero` | ~31 ms | ~187 ms |
| `ois_quote`, 0.6.0 | ~2.8 s | ~27.2 s |
| `ois_quote`, 0.7.0 | ~0.25 s | ~2.2 s |

The OIS cost no longer grows with the number of positions on one curve. What
remains is ~250 bootstraps per distinct curve (BACKLOG #12). Timings on this
machine differ from the macOS figures in the 0.6.0 handoff. Measure on the
target Windows machine and the real universe.

## Cancellation and time budget (0.7.0)

`should_cancel` (a zero-argument callable) and `time_budget_seconds` (measured
with `time.monotonic` from the start of the call) are checked before each
position and before each scenario repricing. A position calculation that has
started, including its Hessian, runs to completion. After a stop, every
remaining position and scenario row is kept with the error
`job cancelled before calculation` or `job time budget exceeded before
calculation`, so coverage, `total_*` nullability and status follow the usual
rules. `interruption` is `cancelled`, `time_budget_exceeded` or `None`. It is
`None` when no work was skipped. Neither setting enters `job_id`: an interrupted
result is not the job's complete answer, so do not cache it under `job_id`.
Exceptions raised by `should_cancel` propagate.

## Versioned result contract (0.7.0)

Every `LocalRiskRunResult` carries `schema_version`
(`rub-local-portfolio-risk-result.v1`) and `result_sha256`, the SHA-256 of the
canonical JSON of all fields with `result_sha256` set to `""`.

- `local_risk_result_to_json(result)` returns canonical UTF-8 JSON (sorted keys,
  exact float repr, ISO dates and offset-aware timestamps) after verifying it.
- `local_risk_result_from_json(data)` rejects invalid JSON, another schema
  version, a hash mismatch, and any missing or extra field at any nesting level.
  It then rebuilds the frozen dataclasses. The rebuilt result is equal to the
  original and re-serializes to the same bytes.
- `verify_local_risk_result(result_or_mapping)` checks schema and hash only.

The hash detects accidental corruption and edits that do not recompute the
hash. It is not a signature. `tests/fixtures/local_risk_result_v1.json` freezes
the v1 field set: changing any persisted field requires a new schema version.
Persist `status`, `coverage`, `interruption`, `curve_basis` and the identities
with the numbers. Partial results must not populate tables or views that imply
full portfolio repricing.

## Executable synthetic example

```python
from datetime import UTC, date, datetime, timedelta

from rub_fixed_income import BondCashflow, BondScenario, DiscountCurve, ZSpreadConvention
from rub_fixed_income.analytics import canonical_json_bytes
from rub_fixed_income.local_worker import (
    LocalPosition, LocalScenario, PublicBondSnapshot, SnapshotEvidence,
    local_risk_result_from_json, local_risk_result_to_json, run_local_risk,
)

as_of = datetime(2026, 1, 1, 12, tzinfo=UTC)
evidence = SnapshotEvidence("synthetic-1", "synthetic", as_of, as_of,
                            as_of + timedelta(hours=1))
snapshot = PublicBondSnapshot(
    instrument_id="SYNTHETIC", market=evidence, schedule=evidence,
    curve_evidence=evidence, calendar_version="synthetic-calendar",
    schedule_complete=True, redemption_confirmed=True,
    cashflows=(BondCashflow(date(2027, 1, 1), 1100),),
    settlement_date=date(2026, 1, 1), horizon_date=date(2027, 1, 1),
    horizon="maturity", cashflow_currency="RUB", coupon_model="fixed",
    current_nominal_rub=1000, clean_price_pct=100, accrued_interest_rub=0,
    curve=DiscountCurve.flat(date(2026, 1, 1), 0.1),
    spread_convention=ZSpreadConvention(), spread_kind="credit",
)
result = run_local_risk(
    run_id="synthetic-run", as_of=as_of, curve_basis="continuous_zero",
    public_snapshots=(snapshot,),
    positions=(LocalPosition("position-1", "SYNTHETIC", 2, 0.4),),
    scenarios=(LocalScenario("rates-up", "continuous_zero", BondScenario.parallel(100)),),
)
assert result.status == "complete"
assert abs(result.total_pv_rub - 2000) < 1e-7
assert result.scenarios[0].total_pnl_rub < 0
serialized_local_result = canonical_json_bytes(result)
assert local_risk_result_from_json(local_risk_result_to_json(result)) == result
```

## Integration boundary still outstanding

This is a reusable in-process worker plus a tested service-response bridge. It is
not installed in the monitor's worker/service/UI or connected to live source
collection. Publishing into the monitor requires its actual checkout, a versioned
result contract with coverage and factor-basis fields, and private local-only
result storage. Do not blindly feed partial results into a legacy table that
assumes complete repricing. Windows execution and real-universe performance must
be measured on their actual environments; synthetic timing is not that evidence.
