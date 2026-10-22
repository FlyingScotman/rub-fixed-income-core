# Derivatives in the portfolio path: result contract v3

`value_portfolio` values bonds and, from this work, every derivative product core
prices: seasoned RUONIA OIS and key-rate swaps, FX forwards and FX swaps, CNY/RUB
cross-currency swaps, single-name CDS, FX futures, RUON / RUONIA index / KEYRATE futures,
OFZ basket futures and repo legs. Each product is valued by its own engine
(`swaps`, `fx_forwards`, `xccy`, `cds`, `fx_futures`, `rate_futures`, `ofz_futures`,
`repo`) on the inputs its position type names and the shared `MarketSnapshot`. No
convention is re-derived in the portfolio layer.

A book whose positions are all bonds is valued exactly as before and returns
`rub-portfolio-risk-result.v2`, byte for byte (same `job_id`, `result_sha256` and JSON
bytes as 0.26.0; pinned in `tests/test_hash_stability.py`). A book with at least one
position on a derivative returns `rub-portfolio-risk-result.v3`.

## Position types (`rub_fixed_income.portfolio_instruments`)

`Instrument` is bond-shaped and its encodings are fixed, so each derivative has its own
frozen position type. `value_portfolio(instruments=...)` takes any mix
(`PortfolioInstrument`); instrument IDs are unique across all of them.

| Type | Wraps | Market data it names |
|---|---|---|
| `SwapInstrument(instrument_id, swap, calendar_id, discount_curve_id, projection_curve_id=None, fixing_id=None)` | `swaps.InterestRateSwap` | schedule calendar; RUB discount curve; RUONIA projection curve (None: single curve) or, for a key-rate swap (required), a `rub_key_rate_swap_v1` curve; the fixing history |
| `FxForwardInstrument(instrument_id, trade, rub_curve_id, foreign_curve_id)` | `FxForward` or `FxSwap` | RUB curve, foreign curve; the snapshot `FxSpot` and `FxBasis` of the currency |
| `XccyInstrument(instrument_id, swap, rub_discount_curve_id, foreign_discount_curve_id, rub_projection_curve_id=None, foreign_projection_curve_id=None, rub_fixing_id=None, foreign_fixing_id=None)` | `xccy.XccySwap` | a curve per role; RUONIA fixings, foreign index fixings; `FxSpot`, `FxBasis` |
| `CdsInstrument(instrument_id, contract, discount_curve_id)` | `cds.CdsContract` | the discount curve of the contract currency; the snapshot `CreditCurveEntry` of (reference entity, currency); `FxRate` for a non-RUB contract |
| `FxFuturesInstrument(instrument_id, contract, rub_curve_id, foreign_curve_id, basis_day_count=ACT_365_FIXED)` | `fx_futures.FxFuturesContract` | both curves, `FxSpot`, the snapshot `FuturesQuote` |
| `RateFuturesInstrument(instrument_id, contract, projection_curve_id, fixing_id=None)` | RUON / RUONIA index / KEYRATE contract | RUONIA OIS projection curve and `RuoniaFixingsEntry` (RUONIA contracts), the latest `RuoniaIndexValue` (index contract), a `rub_key_rate_swap_v1` curve (KEYRATE); `FuturesQuote` |
| `OfzFuturesInstrument(instrument_id, contract, basket, bond_curve_id, funding_curve_id)` | `ofz_futures.OfzFuturesContract`; `basket` of `OfzDeliverable(instrument_id, conversion_factor)` | the basket bonds' `Instrument` terms (in the same `instruments` list) and snapshot quotes; bond and funding curves; `FuturesQuote` |
| `RepoInstrument(instrument_id, trade, calendar_id, discount_curve_id, projection_curve_id=None, fixing_id=None, assumed_end_date=None)` | `repo.RepoTrade` | settlement calendar, discount curve, projection curve and fixing history of a floating repo; `assumed_end_date` for an open repo |

The trade's own choices (projection curve, fixing history, assumed end of an open repo)
live on the position type, not on `ValuationPolicy`, which is part of the job identity
and applies to the whole book. `ValuationPolicy.bump_bp` and `include_second_order` apply
to every product.

**Position.** `Position.currency` must equal the type's `currency`: RUB for swaps,
rate and OFZ futures and repos, the foreign currency of an FX forward, FX swap, XCCY
swap or FX future, the contract currency of a CDS. `Position.quantity` multiplies the
contract as booked (1 = the trade, -1 its mirror); for a future it is the signed number
of contracts. A CDS position's `recovery_rate`, when given, is the realised recovery of
its JTD; otherwise the credit curve's stated recovery is used (never a default).

## Snapshot inputs (`MarketSnapshot`, all optional, omitted at their defaults)

| Field | Entry | Use |
|---|---|---|
| `fx_spots` | `FxSpot(currency, rub_per_unit, value_date, calendar_id, evidence_id=None)` | the spot with its value date and the pair's settlement calendar (a snapshot calendar); FX forwards, XCCY, FX futures. Bonds still convert at `FxRate`, which has no value date |
| `fx_bases` | `FxBasis(currency, foreign_curve_id, basis_bp, day_count=ACT_365F)` | the covered-parity basis of a pair on one foreign curve: 0 on an implied or XCCY-bootstrapped curve, calibrated on, say, a foreign OIS curve; required, never defaulted |
| `credit_curves` | `CreditCurveEntry(reference_entity, currency, quotes, recovery_rate, convention, evidence_id=None)` | par-spread / upfront quotes of one entity in one currency with the stated recovery; one hazard bootstrap (and its shocked versions) per entity and job |
| `futures_quotes` | `FuturesQuote(contract_id, price, margined_price, evidence_id=None)` | the settlement price or mark the basis is calibrated to, and the price positions were last margined at |
| `index_fixings` | `IndexFixingEntry(fixing_id, IndexFixings)` | foreign overnight index fixings of a floating XCCY foreign leg, dated the business date |
| `ruonia_fixings` | `RuoniaFixingsEntry(fixing_id, RuoniaFixings)` | RUONIA with the declared unpublished days (rate futures); an undeclared gap rejects |
| `ruonia_index` | `RuoniaIndexValue(index_date, value)` | published RUONIA index values; the index future grows the latest one |
| `curves` | `CurveEntry` with a `KeyRateForwardCurve` under `rub_key_rate_swap_v1`, or an `XccyCurveBootstrapRequest` under `cny_ruonia_xccy_v1` | key-rate projection of swaps and KEYRATE futures; the CNY curve from XCCY par quotes (quote-basis risk) |

Fixing IDs are unique across `fixings`, `index_fixings` and `ruonia_fixings`. Existing
snapshots keep their bytes and hashes; `to_dict` / `from_dict` round-trip the new
entries.

**Foreign curves of FX products.** A foreign curve is discount factors of its currency
(`<ccy>_discount_factors_v1`, the spec's model or a curve from
`fx_forwards.implied_foreign_curve`) or a built XCCY curve (`cny_ruonia_xccy_v1`), dated
the business date. An `XccyCurveBootstrapRequest` is rebuilt only by cross-currency
swaps, whose spot must equal the snapshot `FxSpot`.

**Dating of key-rate curves (BACKLOG #55).** `build_key_rate_forward_curve` dates its
curve at the KRS start (TOM) and needs a TOM-dated discount curve; the portfolio values
everything on the business date. Decision: every curve in the snapshot, including the
key-rate projection curve, is dated the business date. The backend re-dates a TOM
calibration with `key_rate.rebase_key_rate_curve(curve, business_date,
rate_in_force=...)`, which extends the current-rate interval back to the business date
and requires the key rate in force on the business date to equal the curve's current
rate (a decision effective on TOM itself cannot be represented that way and is
rejected). Bucket shifts are then read at times from the business date. A key-rate curve
not dated the business date is `INVALID_TERMS`; core never moves a curve in time
silently.

## Result contract v3 (`PortfolioRiskResultV3`)

`PortfolioRiskResultV3` is a subclass of `PortfolioRiskResult` with
`schema_version = "rub-portfolio-risk-result.v3"`. It keeps every v2 field, with these
meanings over the whole book:

- `positions`: the bond rows (unchanged type `PositionValuation`);
- `coverage`, `scenarios`, `covered_pv_rub`, `total_pv_rub`: every position, bonds and
  derivatives;
- `factor_totals`: the sum of every position's first order, in one factor space
  (`IR|<curve>|<bucket>`, `REAL|…`, `CS`, `FX|<ccy>`, `FXBASIS|<ccy>`,
  `XCCYBASIS|<ccy>`, `FUTBASIS|<contract>`);
- `curves`, `calendars`, `fx_rates`, `cpi`: everything read.

Added (product tuples omitted while empty):

| Field | Content |
|---|---|
| `swaps` | `SwapPositionValuation`: PV, clean PV, accrued, leg PVs, par rate, annuity, curve IDs and bases, fixing ID and the fixing history's `snapshot_id` |
| `fx_forwards` | `FxForwardPositionValuation`: kind (forward / swap), spot and value date, settlement calendar, basis, FX delta in currency, legs (forward rate, points, RUB DF, PV) |
| `xccy_swaps` | `XccyPositionValuation`: spot, basis, `fx_today`, curve ID per role, leg PVs, the foreign leg's par rate or spread |
| `cds` | `CdsPositionValuation`: side, notional, coupon, curve recovery, PV in currency and RUB, clean PV, accrued premium, points upfront, par spread, risky annuity, `recovery01_rub`, JTD and its realised recovery |
| `fx_futures` | `FxFuturesPositionValuation`: model and margined price, calibrated basis, notional, `variation_margin_rub` (= `pv_rub`) |
| `rate_futures` | `RateFuturesPositionValuation`: kind, model and margined price, basis, expected rate, fixed and projected days, fixing snapshot, index anchor date, variation margin (RUON rounding included) |
| `ofz_futures` | `OfzFuturesPositionValuation`: model and margined price, basis, cheapest to deliver (by model and by implied repo), basket analysis, variation margin |
| `repos` | `RepoPositionValuation`: S1, S2 (expected) and its source, known interest, accrued, current repurchase amount (per trade), and the collateral reference, which is not in PV or risk |
| `position_models` | `PositionModel(position_id, instrument_id, product, model_version, flags)` for every computed position, in position order (BACKLOG #83) |
| `hessian_totals` | `HessianTotal(factor_i, factor_j, value_rub)`, `factor_i <= factor_j` by name: the sum of every position's sparse Hessian (bonds included); empty without `include_second_order` |
| `jtd_totals`, `jtd_total_rub` | `JtdTotal(entity, jtd_pnl_rub, positions)` by bond `issuer_id` / CDS reference entity (bonds without an issuer under `None`), and their sum. Bonds count when the position states a recovery; bonds and CDS share the sign rule (DEC07): long credit loses on default |
| `fx_spots`, `credit_curves` | the `FxSpot`s and `CreditCurveUse(reference_entity, currency, recovery_rate, quotes, convention, evidence_id)` read |

Every derivative row starts with the same fields (`DerivativeRowHeader`):
`position_id`, `instrument_id`, `book`, `accounting_category`, `calculation_id`,
`quantity`, `pv_rub`, `factor_risk`. Money is signed RUB for the position (product
amounts times the quantity; a non-RUB CDS at the snapshot `FxRate`).

**Futures PV.** A daily-margined future has no PV beyond the margin not yet paid:
`pv_rub = variation_margin_rub = margin per point * contracts * (price -
margined_price)`, undiscounted (the margin of the next clearing). Its risk is the
variation margin's sensitivity.

**Identity and verification.** `job_id` hashes the same inputs as v2 with the model
`portfolio-risk-v3`; `result_sha256` is SHA-256 of the canonical JSON with the hash
blanked, as in v2. `verify_portfolio_result_v3` checks it (and the schema);
`verify_portfolio_result` stays v2-only. `portfolio_result_to_json` writes either
version; `portfolio_result_from_json` reads both (v2 into `PortfolioRiskResult`, v3 into
`PortfolioRiskResultV3`). `tests/fixtures/portfolio_result_v3.json` is pinned.

### Model flags (`PositionModel.flags`)

| Flag | Meaning |
|---|---|
| `two_curve` / `single_curve` | projection on a curve separate from discounting / on the discount curve |
| `floating` | a floating-coupon bond |
| `has_optionality` | an embedded option valued without time value: a floater's floor or cap (intrinsic, outside the guard), OFZ futures' delivery options (not valued) |
| `to_confirmed_put` | a bond valued to a confirmed put |
| `perpetual_tail` | a never-called perpetual: its coupons continue forever after the listed ones (BACKLOG #125) |
| `fx_translated` | amounts converted to RUB at the snapshot `FxRate` (non-RUB bond, non-RUB CDS) |
| `real_curve` | an inflation-linked bond on a real curve |
| `known_fixings` | fixings from the snapshot entered the value |
| `key_rate_projection` | projected on a key-rate forward curve |
| `covered_parity` | FX forward, FX swap, XCCY or FX future valued by covered parity |
| `quote_curve` | the foreign curve is rebuilt from XCCY par quotes under shocks |
| `hazard_bootstrap` | CDS on a hazard curve bootstrapped from quotes |
| `daily_margined` | a future; PV is the pending variation margin |
| `implied_basis` / `fully_fixed` | the futures basis is calibrated to the snapshot price / the rate period is fully fixed and the fixings determine the price |
| `cheapest_to_deliver` | OFZ futures priced on the cheapest bond |
| `assumed_end` | an open repo valued to its assumed end |
| `collateral_reference` | a repo with a collateral reference (not in PV or risk) |

`model_version` is the engine's (`rub-fixed-cashflow-risk-v1`,
`rub-floating-two-curve-risk-v1`, `rub-irs-ois-krs-v1`, `rub-fx-forward-parity-risk-v1`,
`rub-xccy-v1`, `cds-isda-standard-risk-v1`, `rub-fx-futures-parity-risk-v1`,
`rub-rate-futures-v1`, `rub-ofz-futures-ctd-v1`, `rub-repo-leg-v1`).

## Scenarios

`PortfolioScenario` applies to every product:

- `shock.curve_shifts_bp` moves every curve a position reads (discount, projection,
  RUB and foreign, as before for bonds); `projection_curve_shifts_bp`, when given,
  moves projection curves instead;
- **per-curve shifts (BACKLOG #43):** `curve_shifts_bp=((curve_id, 11 shifts), ...)`
  (omitted at its default) gives named curves their own vector, in every role and for
  every product. A real curve moves only when it is named (linkers stay
  `UNSUPPORTED_PRODUCT` in a scenario that does not name their real curve);
- `shock.spread_shift_bp` moves `CS`: bond spreads, all quoted spreads of every CDS
  credit curve, the basket spreads of OFZ futures;
- `fx_shocks_pct` moves the spot of FX forwards, XCCY and FX futures, and the conversion
  of non-RUB bonds and CDS;
- futures and FX bases are held (no basis shocks yet).

Every position reprices exactly with its engine; a reprice failure is coded per
position.

## Exercise assumptions (BACKLOG #50)

`ValuationPolicy.exercise_assumptions=((instrument_id, ExerciseAssumption), ...)`
(omitted at its default) values a callable or perpetual bond as the deterministic bond
its resolved exercise implies (`callables.resolve_exercise`, `exercised_instrument`;
`to_worst` at the snapshot quote), held under every bump and scenario, in
`value_portfolio` and `horizon_portfolio`. The row records
`PositionValuation.exercise_assumption` (the resolved identifier) and, for a call,
`horizon="call"` with the call date; a never-called perpetual `horizon="perpetual"` with
its last listed coupon (BACKLOG #125; not in `horizon_portfolio` yet). The numbers equal `value_assumed_portfolio`'s, which
is unchanged and remains the path for pass-throughs and a book-wide default exercise.
A bond-only book with exercise assumptions stays v2 (the new keys appear only on the
exercised rows).

## Coverage codes

| Code | When |
|---|---|
| `MISSING_MARKET_DATA` | a curve, FX spot, FX basis, credit curve, futures quote, fixing history, index fixings or RUONIA index value is absent |
| `UNDECLARED_FIXING_GAP` | a RUONIA business day without a fixing that is not declared unpublished (rate futures) |
| `MISSING_BASKET_PRICE` | an OFZ futures basket bond has no snapshot quote |
| `MISSING_ASSUMPTION` | an open repo without `assumed_end_date` |
| `EXPIRED_CONTRACT` | a future past its last trading day (FX futures: delivery), or an OFZ basket settling after delivery |
| `SETTLED_LEG` | an FX leg settled before the business date, a repo that has ended |
| `INVALID_TERMS` | a position currency other than the instrument's; a curve of the wrong currency, kind or date (a key-rate curve not dated the business date); XCCY quotes with a spot other than the snapshot's; a value date off the settlement calendar |
| `UNSUPPORTED_PRODUCT` | a position type without an adapter; any product limit of its engine; in `horizon_portfolio`, a non-RUB CDS with `fx_horizon="forwards_realised"` |
| `CALCULATION_FAILED` | a numerical failure |

## Extension point

`rub_fixed_income.portfolio_products.PRODUCT_ADAPTERS` maps each position type to an
adapter `(position, instrument, ProductContext) -> ProductOutcome`. A new product adds a
frozen position type (`instrument_id`, `product`, `currency`), a row type starting with
the `DerivativeRowHeader` fields, an adapter that builds its engine from the context
(curves by ID with their checks, calendars, FX, fixings, the shared `ShockedCurveCache`,
`shared_engine` for one engine per contract), its entry in `PRODUCT_ADAPTERS` and
`ROW_FIELDS`, and a tuple field on `PortfolioRiskResultV3` declared with
`OMIT_IF_DEFAULT` so that existing v3 results keep their bytes. Factor totals, the
Hessian, JTD, coverage, scenarios and model records then include it. Repo legs were
added this way after the other products.

Carry has its own registry, `HORIZON_ADAPTERS` (0.30.0; every product since 0.31.0): an
adapter `(position, instrument, ProductContext, horizon_date, fx_horizon) -> row` builds
the same engine as the valuation adapter, calls its `.horizon(...)` and returns a row
whose first fields are those of `DerivativeHorizonHeader` (`carry_rub`, `rolldown_rub`,
`received_rub`, `horizon_pnl_rub`, signed RUB for the position) and whose last is the
engine's result (`per_trade`). Its type is listed in `HORIZON_ROW_FIELDS` with a tuple
field on `PortfolioHorizonResult` (`swaps`, `fx_forwards`, `xccy_swaps`, `cds`,
`fx_futures`, `rate_futures`, `ofz_futures`, `repos`); `.derivative_rows()` returns
them all. FX products take the spot at the horizon from `horizon_portfolio(...,
fx_horizon=...)` and the horizon spot date from `horizon_spot_date` (today's lag). The
conventions are in [CONVENTIONS.md](CONVENTIONS.md), "Derivatives: carry, rolldown and
theta".

## Performance

`benchmarks/portfolio_bench.py --positions 2000 --cases mixed` cycles through the
positions of the mixed test book (`tests/portfolio_v3_book.py`): every OTC trade is its
own instrument, futures positions share their contract. Linux x86_64, Python 3.13:

| Mode | ms / position |
|---|---|
| first order | 1.6 |
| + Hessian | 8.0 |
| + Hessian, 3 scenarios | 7.9 |

Per product (40 positions of one kind, Hessian, including per-job set-up): CDS 30–34 ms
(hazard rebootstraps per shock, BACKLOG #70), a two-curve OIS swap on OIS quotes 45 ms,
an open RUONIA repo on OIS quotes 44 ms, XCCY on quotes 12 ms, the others 1–6 ms. The
engines dominate; the portfolio layer adds no repricing.

## Not covered

- Derivatives in `local_worker.run_local_risk` (contract v1). Carry of a non-RUB CDS
  with `fx_horizon="forwards_realised"`, of OFZ futures through delivery and of non-RUB
  bonds (BACKLOG #41, #108).
- Basis shocks (`FXBASIS`, `XCCYBASIS`, `FUTBASIS`) in `PortfolioScenario`; recovery as a
  factor (`recovery01_rub` is reported beside the factors, BACKLOG #67).
- One shared engine per FX futures contract (each position builds its own; rate and OFZ
  futures share).
- RUSFAR projection curves: a RUSFAR repo projects on a curve under a RUONIA OIS spec;
  no RUSFAR spec is registered.
- Persisting XCCY and CDS shocked curves across jobs (BACKLOG #70, #76).
