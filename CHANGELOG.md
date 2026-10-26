# Changelog

## Unreleased

### KBD floaters discounted on RUONIA in RUONIA + basis coordinates (#90)

The mirror of #83 (FlyingScotman/rub-fixed-income-core#90): a KBD floater (MOEX `ZR_YLD_CRV`,
`FloatingCouponRule(index="kbd")`) discounted on the RUONIA OIS quotes and projected off the
v3 KBD. Under 0.39.0 `ValuationPolicy.ofz_basis` left it in plain factors (`IR|<kbd>|*`
projection, `IR|<ois>|*` discount), so a consumer could only approximate its RUONIA risk
(variant P). Policies without `ofz_basis`, and every other position under it, are unchanged
(the suite's goldens and pinned hashes pass unchanged).

- `OfzBasisRiskEngine(engine, curve)` also takes a floater whose discount curve is
  `curve.ois` and projection curve `curve.kbd` (`.discounted_on == "ois"`; a fixed bond on the
  OIS quotes or a floater on them projected off another curve is `InvalidTerms`). A RUONIA
  bucket moves both legs: the discount is the **plain** OIS request rebootstrapped from the
  shifted quotes (the bond's own `IR|<ois>|*` rule and OIS swaps', the same cache entries),
  the projection the composed KBD `Y + overlay(q) + [Z_s − Z_0]` of `OfzBasisCurve.shocked`
  with the **extended** (ZX) request, as for an OFZ (a 7Y KBD index reads the KBD past the
  last 10Y quote). A basis bucket moves the projection only: basis risk, the basis and spread
  Hessian block and basis-only scenarios are the engine's own projection numbers to the bit.
  When the quotes reach 20Y no tenor is added and both legs read the same shocked OIS curve.
  Bumped only (floating coupons re-project); `reprice(..., projection_shifts=)` moves the
  RUONIA part of the projection separately.
- `value_portfolio` with `ofz_basis`: such a floater (discounted on `ois_curve_id`, projected
  off `kbd_curve_id`) reports `IR|<ois>|*`, `IR|<basis>|*` and `CS` (row `curve_basis =
  OFZ_BASIS_CURVE_BASIS`, v3 model flag `ruonia_basis`); PV, spread and JTD unchanged.
  Scenarios: the discount by the OIS curve's shift (named, else the shock's), the projection's
  RUONIA by its projection shift (named, else `projection_curve_shifts_bp`, else the shock's),
  the basis by `curve_shifts_bp` of the basis ID; naming the KBD is `INVALID_TERMS` for it, and
  a view that cannot be built (non-v3 KBD, OIS without quotes) too, as for an OFZ. A KBD
  floater projected off another KBD ID, key-rate and RUONIA floaters keep their numbers.
- Tests (`tests/test_ofz_basis_kbd_floater.py`): basis = plain projection factors to the bit;
  RUONIA buckets against an independent rebuild of both OIS curves, the composed KBD and the
  coupons (1e-9); parallel moves equal the factor sums; joint scenarios in the portfolio equal
  the independent revaluation (1e-9). Synthetic 7Y floater (`docs/ofz-basis.md`): RUONIA total
  +36.9 against discount −365.9 plus projection +404.8 RUB/bp per 1 mn; the projection's RUONIA
  leg +402.9.

## 0.39.0 — OFZ risk as RUONIA plus a basis, both computed

### OFZ risk as RUONIA plus a basis, both computed (#83)

The owner's decision of 2026-10-25 (FlyingScotman/rub-fixed-income-core#83; the monitor side
is FlyingScotman/trading-desk-monitor#213): an OFZ's RUONIA risk and its basis risk must be
computed, not identical. Until now the «RUONIA + basis» view was `basis_view`, a linear
change of coordinates that makes RUONIA risk ≡ basis risk ≡ KBD v3 par risk bucket by bucket
(variant P). A research run on hub data of 2026-10-22 chose variant Z with the ZX tail
(`docs/ofz-basis.md`; the table in `docs/proposals/ofz-curve-risk-factors.md` §11). Opt-in:
the new policy field is omitted at its default, so earlier policies, snapshots, job IDs,
results and pinned hashes are unchanged.

- **The scheme (variant Z).** The basis is a spread in effective annual zero yields. The
  KBD's annual zero under RUONIA shifts `s` and basis shifts `q` is
  `base(t) + overlay_v3(q)(t) + [Z_s(t) − Z_0(t)]`: the v3 par overlay solved on the base KBD
  plus the annual-zero move, on the KBD's curve time, of the RUONIA OIS curve rebootstrapped
  from quotes moved by the bucket hats (`_shocked_request`, the OIS curve's own quote-risk
  rule). With RUONIA held the shocked curve is the v3 view itself, so basis risk, its
  Hessian and every basis-only price are today's v3 numbers to the bit; only the RUONIA leg
  is new. Joint shifts add in annual zero (the overlay is solved on the base KBD and the
  OIS move added); cross gammas and joint scenarios follow this rule.
- **The long end (ZX).** The OIS request used here is extended with one synthetic quote per
  v3 par tenor after the last quote (15Y and 20Y after 10Y): KBD par at the quote's maturity
  minus `s_tail = KBD par(T_last) − quote(T_last)`. KBD par is that of an annual-coupon bond
  on the base KBD from its curve date (yearly unadjusted rolls, a roll within 180 days of
  maturity dropped, ACT/365F); the OIS quote is as published; maturities resolve under the OIS
  convention. The synthetic quotes are fixed at the base: RUONIA 15Y/20Y+ move them by their
  hats, basis buckets never rebuild the OIS curve. Without them a 10Y RUONIA bump moves the
  15Y OIS zero by +3.9 bp (flat-forward extrapolation) and a 15Y OFZ shows ±700 RUB/bp of
  RUONIA 7Y/10Y risk.
- `ofz_basis.OfzBasisCurve(kbd, ois)`: the v3 KBD (`zero_shock="annual_par"`, else
  `InvalidTerms`) and RUONIA OIS quotes; `.extended`, `.synthetic_quotes`, `.tail_spread`,
  `.shocked(ruonia, basis, cache)`, `.ois_curve`, `.ois_jets` (`∂ΔZ/∂s`, `∂²ΔZ/∂s²` at a date).
- `ofz_basis.OfzBasisRiskEngine(engine, curve)`: a `BondRiskEngine` on that KBD over 11 RUONIA
  buckets, 11 basis buckets and the spread: `.price`, `.risk(method=..., kbd_risk=...)`
  (`OfzBasisRiskResult`), `.second_order(method=...)` (23 × 23 `BondSecondOrderRisk`),
  `.reprice` (`BondScenarioResult`; the v3 1M–6M rule on the basis shifts). `method="bump"`
  for any bond the engine prices (fixed, perpetual tails, single-curve KBD floaters,
  floaters projected off the same RUONIA quotes; other projections `InvalidTerms`);
  `method="analytic"` for fixed cash flows: annual-zero derivatives at the flow dates, the
  basis block through the v3 par Jacobian with the instruments' curvature (the engine's own
  analytic Hessian, to the bit), the RUONIA rows through `∂ΔZ/∂s` and `∂²ΔZ/∂s²` of the OIS
  zero per date, central differences of the cached shocked OIS curves (memoised per date).
- `factors.ofz_basis_factor_risk(risk, ois_curve_id, basis_id, second_order=None)`: factors
  `IR|<ois>|*` (the OIS curve's own, so they add up with OIS swaps), `IR|<basis>|*`, `CS`.
- `ValuationPolicy(ofz_basis=OfzBasisIds(kbd_curve_id, ois_curve_id, basis_id))` (omitted at
  None): `value_portfolio` reports every bond discounted on the KBD in these factors instead
  of `IR|<kbd>|*` (row `curve_basis = "ruonia_ois_quote+kbd_par_basis"`, v3 model flag
  `ruonia_basis`); closed form where `risk_method` / `second_order_method` ask for it. PV,
  spread, yields, G-spread, `spread_buckets_rub` and JTD are unchanged. Scenarios move RUONIA
  by the OIS curve's shift (named, else the shock's) and the basis by `curve_shifts_bp` of the
  basis ID (held when not named); naming the KBD is `INVALID_TERMS` for these bonds. A KBD
  that is not `rub_ofz_kbd_v3` or an OIS curve without quotes is `INVALID_TERMS` for them; an
  OIS calendar that cannot resolve the tail tenors is `CALENDAR_OUT_OF_RANGE`.
- **Shocked OIS curves.** The extended request is a separate `ShockedCurveCache` request
  (same keys, persisted by `export` / `seed`): 22 rebuilds per job for first order, 220 more
  for the RUONIA Hessian, shared by the closed form and the bump path. OIS positions keep the
  plain request (their numbers and keys are unchanged); quotes that already reach 20Y add no
  tenor and share every curve.
- **Accuracy** (`tests/test_ofz_basis.py`): RUONIA buckets equal an independent
  rebuild-compose-reprice to 1e-9; closed form against bump 2.6e-6 relative at 1 bp and
  2.6e-8 at 0.1 bp (O(bump²)); a second-order Taylor expansion of a joint RUONIA + basis +
  spread move of 4–10 bp leaves a third-order residual (halving the move divides it by ~8).
  On synthetic OIS quotes shaped like the KBD of 2026-10-15, RUONIA and basis totals differ by
  0.2–0.3 % and bucket by bucket by 4–22 % of DV01 (14.6Y bond: 15Y −391.1 against −409.1
  RUB/bp per 1 mn), the pattern of the research (SU26238: −406.6 against −417.9).
- **Cost** (`benchmarks/portfolio_bench.py --cases kbd3,ofzbasis,ofzbasis-bump`, 2000 bonds
  on the v3 KBD and 17 MOEX RUONIA quotes, ms per position; noisy shared machine): closed
  form, first order 3.65 (plain v3 2.43), with the Hessian 7.26 (v3 3.97), with five
  scenarios 7.50 (v3 3.53); by bumps 4.47 / 26.62 / 24.41 (the 23 × 23 bumped Hessian is
  about 1 060 repricings per bond). The ~243 extended OIS rebuilds are ~1.2 s per job.
  The default cases are unchanged (within machine noise of 0.38.0).

### Changed

- `basis_view`: numbers unchanged; documented as the variant-P approximation (RUONIA ≡ basis
  bucket by bucket), kept for comparison and for curves without quotes to rebootstrap.
- `portfolio.py`: three pre-existing over-long lines reformatted (`ruff format`); no
  behaviour change.
- `local_risk.CurveRiskBasis` and `RiskRunContext` accept `ruonia_ois_quote+kbd_par_basis`
  (`OFZ_BASIS_CURVE_BASIS`, now `Final`), so a row's `curve_basis` in these coordinates can
  be carried into a local run context; the value is no longer hidden from type checks.

### Not covered

- A floater discounted on RUONIA and projected off the KBD keeps `IR|<kbd>|*` projection
  factors under `ofz_basis`; `rub_ofz_kbd_v2` is not accepted (it would compose the same
  way); OIS positions do not use the extended request; `horizon_portfolio` does not report
  factors and is unchanged.

## 0.38.0 — faster RUONIA rebuilds, curve risk in quotes for key-rate and FX curves, closed-form first order

The second wave of the desk's 2026-10-23 decision (risk of every curve in its par quotes)
and the follow-ups of 0.37.0: FlyingScotman/rub-fixed-income-core#77,
FlyingScotman/rub-fixed-income-core#75 (review of 0.37.0) and
FlyingScotman/rub-fixed-income-core#76. Every new field is omitted at its default and the
bootstrap speed-up is exact: earlier policies, snapshots, job IDs, results and pinned
hashes are unchanged. One behaviour change: a scenario on a par-yield KBD
(`rub_ofz_kbd_v3`) whose 1M–6M shifts differ from zero and from its 1Y shift is refused
instead of silently dropped (see Changed).

### Faster OIS rebuilds (#77, part 1)

The desk monitor now passes RUONIA OIS quotes as a `CurveBootstrapRequest`, so every
bucket bump of its Hessian rebuilds the curve: about 245 coupled spot-OIS bootstraps per
job at ~75 ms each. Both changes below are exact: no new field, and every curve, result,
job ID and pinned hash is bit-identical to 0.37.0.

- `bootstrap_discount_curve` no longer builds a `DiscountCurve` per bisection trial. The
  objective reads its discount factors from a private evaluator that does the same float
  operations as `DiscountCurve.discount_factor` and computes once everything that does
  not move with the solved node; the swap terms are summed in the same order, and the
  spot-OIS swap layout (periods, day-count fractions, floating scales) is cached per
  tenor set and convention. Same bisection, same objective values, same nodes:
  `tests/test_bootstrap_exact.py` keeps the 0.37.0 bootstrap as the reference and
  compares by `float.hex` (steep, inverted, negative, humped and zero curves, 1W–30Y
  pillars, bracket expansion, bucket shocks and their pairs). 17 MOEX RUONIA swaps:
  ~77 ms → ~5 ms per bootstrap; the legacy convention ~11 ms → ~1 ms.
- `ShockedCurveCache` bootstraps each distinct shocked quote set once: shift vectors that
  leave the same quotes (a bucket past the last quote, alone or in a Hessian pair) share
  the curve, within the same `max_entries`. Keys, `hits`/`misses`, `export` and `seed`
  are unchanged. An 11-bucket
  Hessian on 17 MOEX RUONIA swaps: 201 bootstraps instead of 243.
- Together, `value_portfolio` with the Hessian on 17 MOEX RUONIA quotes: 50 bonds
  ~19.5 s → ~1.4 s; the 2000-bond benchmark book 36.3 s → 15.3 s (now pricing-bound).
- `benchmarks/portfolio_bench.py`: case `ruonia`, the same 17 tenors as MOEX standard
  swaps under `rub_ruonia_ois_moex_v2` (the monitor's curve risk in quotes).

### Added

- `key_rate.KeyRateCurveRequest(build, business_date, rate_in_force)`: KRS quotes (a
  `KeyRateCurveBuildRequest`) as the risk source of a key-rate projection curve.
  `.curve()` is the mid calibration re-dated with `rebase_key_rate_curve`; a bucket shift
  moves each mid quote by the bucket hats at its maturity (ACT/ACT ISDA from
  `business_date`, as an OIS quote) and recalibrates on the same discount curve
  (`.shocked`, the solve and checks of `build_key_rate_forward_curve`); bid, ask, the
  calibration's discount curve and the decided rate do not move. The calibration is
  linear in the quotes (`.shift_weights`), so key-rate swaps keep their exact linear
  projection and a zero own Hessian block. `SwapRiskRequest` and `RateFuturesRiskRequest`
  (KEYRATE) take it; curve basis `key_rate_quote`; carry on the mid curve.
- XCCY par quotes (`XccyCurveBootstrapRequest`) as the foreign curve of
  `FxForwardRiskRequest`, `FxCrossRiskRequest` and `FxFuturesRiskRequest` (basis
  `xccy_quote`): rebuilt under every shock on the engine's shocked RUB curve, quotes held
  under RUB shocks and moved by the bucket hats at their end dates under foreign ones, as
  `XccyRiskEngine` does, so every product on the CNY quotes shares one factor unit.
- `ParYieldCurveRequest` as a curve input (basis `par_yield_quote`, `.shocked(shifts)`):
  each par yield moved by the bucket hats at its maturity's spec time, then
  `bootstrap_par_yield_curve`. Bonds (`BondRiskRequest.curve`), CDS, FX products and XCCY
  swaps take it; `local_risk.CurveRiskBasis` adds `par_yield_quote`.
- `CurveEntry` takes a `KeyRateCurveRequest` under `rub_key_rate_swap_v1` (its
  `business_date` must be the snapshot's) and a `ParYieldCurveRequest` under a
  `*_par_proxy_v1` spec.
- `ValuationPolicy.quote_curve_risk` (omitted at `False`): in `value_portfolio` and
  `horizon_portfolio` a curve the snapshot holds as quotes is the risk source of every
  position on it: key-rate swaps and KEYRATE futures on KRS quotes, FX forwards, swaps,
  crosses and futures naming XCCY quotes (`INVALID_TERMS` without the flag, as before),
  bonds, CDS and FX fallbacks on par-yield proxy quotes. Model flag `quote_curve` on the
  derivative rows rebuilt from quotes (XCCY or par-yield proxy quotes, KRS quotes); bond
  and CDS rows carry `curve_basis = "par_yield_quote"`.
- Private module `_quote_curves` (`QuoteCurve`, the base of `XccyCurveBootstrapRequest`
  and `ParYieldCurveRequest`): rebuilt curves memoised on the request per RUB curve and
  shift vectors (at most 4096 per RUB curve, for the last four RUB curves); `_risk_common` shocks requests that build alone.
- `BondRiskEngine.risk(method="analytic")`: the exact first derivatives of a
  fixed-cashflow bond on a `DiscountCurve` (bucket DV01, KRD, parallel DV01, spread risk):
  the gradient pass of `second_order(method="analytic")` without the Hessian, `J^-T` on
  `annual_par`. The limit of the central difference (1e-7 relative from it on the tests)
  and equal to the analytic second order's gradient bit for bit; `parallel_dv01_rub` is
  the bucket sum, `bump_bp` only recorded; `difference` is central when omitted
  (`"forward"` is a `ValueError`). Model `ANALYTIC_RISK_MODEL_VERSION`. `InvalidTerms`
  with the reason for floaters, perpetual tails and OIS quote curves. About 3.5 times
  faster per call (0.13–0.18 ms instead of 0.5–0.6 ms for a bond).
- `ValuationPolicy.risk_method` (`"bump"` default, `"analytic"`): the closed-form first
  order for every bond that has one in `value_portfolio` (`pv_rub`, `spread_buckets_rub`,
  the factor first order without a Hessian); others are bumped. A field of its own, so
  0.37.0 policies with `second_order_method="analytic"` keep their results. On the KBD
  benchmark book, 2.3 instead of 2.7 ms per position without the Hessian, 2.8 instead of
  3.5 ms with the analytic Hessian.
- `benchmark_equivalents` positions: floaters that project or discount on the benchmarks'
  curve, for example a KBD `ZR_YLD_CRV` floater projected off the KBD and discounted on
  RUONIA OIS (coupons re-projected off the overlaid curve, node risk by central
  differences of `FLOATER_NODE_BUMP_BP` = 0.1 bp), and OFZ basket futures
  (`OfzFuturesRiskEngine`) whose bond or funding curve it is (closed form through the
  cheapest-to-deliver bond, spread and basis held). At benchmarks on the 3Y–10Y pillars
  both equal the engines' own central bucket DV01 to 1e-7. A new warning names positions
  with a curve other than the benchmarks' (held: its risk is not in the view). Benchmarks
  stay fixed-coupon bonds; anything else is `InvalidTerms` with the reason.

### Fixed

- Par-yield shocks (`zero_shock="annual_par"`, `rub_ofz_kbd_v3`): the overlay's Newton is
  damped (a step is halved until the shocked annual yields at the instruments' coupon dates
  stay above -100 % and the residual norm falls, Armijo). Realistic shocks take every full
  step, so their results are bit-identical to 0.37.0 (checked on the 2022 and 2026 KBD for
  2022-size and twice 2022-size shapes); where a large shock is damped (random shapes of
  150–300 bp, 20Y -500 bp) the solution differs from 0.37.0 by at most 1e-8 bp; a 20Y
  -1500 bp shock, which overshot to a domain error, now converges. A shock beyond the overlay's reach (for example the 20Y+ par yield
  more than about 180 bp above the 15Y on a 16 % curve, 75 bp after +1000 bp, or a
  +-500 bp saw-tooth) raises `ValueError` "par-yield shock did not converge" naming the
  instrument that misses and by how much, instead of "shocked annual yield must exceed
  -100 %".
- `benchmark_equivalents`: a benchmark engine with `quantity=0` raised `LinAlgError`; the
  benchmarks' Jacobian no longer depends on their quantity, as documented.
- `second_order` docstring and proposal: a fixed-coupon par instrument's own gamma is that
  of its annuity (`-2 dA/dq` in its quote), not zero.

### Changed

- Scenarios on an `annual_par` curve: the 1M, 3M and 6M shifts (no instrument there) must
  each be zero or equal to the 1Y shift (within 1e-9 bp); any other value, which 0.37.0
  dropped silently, raises `InvalidTerms` naming the buckets. A parallel shift and any shift uniform from 1M
  to 1Y stay valid; risk bumps of the short buckets still read zero. Checked in the
  scenario paths of every engine (`BondRiskEngine.reprice` / `approximate`, repo, swaps,
  CDS, XCCY, FX forwards, crosses and futures, OFZ and rate futures) for each curve they shift; in
  `value_portfolio` such a position fails that scenario (`CALCULATION_FAILED`, with the
  message). Other curves and specs are unchanged.
- `benchmark_equivalents` warnings: `CONDITION_WARNING` (cond(J) > 50) is removed; it never
  fired (nodes at the benchmarks' maturities keep cond(J) near 3 even a day apart).
  `AMPLIFICATION_WARNING = 3.0` warns when the gross benchmark risk exceeds the positions'
  gross node risk more than 3 times (large neighbouring hedges of opposite sign; 5.9 for an
  11Y zero against 25 % coupon benchmarks, 1.0-1.7 for coupon bonds and close benchmarks).
  New warnings name positions that mature before the first benchmark or pay after the last
  (the end benchmark carries that risk). `condition_number` is still reported.
- `basis_view` docstring and docs: on `rub_ofz_kbd_v3` the basis is the par-par spread
  exactly at the nodes 1Y-20Y+ only; between nodes the KBD overlay and the OIS quotes
  spread a bucket shift differently (a 4Y par yield takes 0.49/0.51 of the 3Y/5Y shifts on
  the KBD, 0.50/0.50 on OIS; 13Y: 0.37/0.64 against 0.40/0.60).
- `benchmark_equivalents` compares curves by canonical value: an equal curve built apart
  is the same curve (it was refused as another curve object).
- The par Jacobian's inverse is cached on the curve; the analytic second order is
  unchanged to the bit.
- `build_key_rate_forward_curve` shares its quote-independent calibration with
  `KeyRateCurveRequest`; its numbers are unchanged.
- `ProductContext.discount_curve` rejects par-yield quotes for products that read only
  built curves (swaps, repos, futures, XCCY projection); `quoted_discount_curve` passes
  them to bonds, CDS and FX products.

### Not covered

- Curves implied from FX swaps (`FxImpliedCurveRequest`) are still built once and shifted
  on their zero rates; the KRS calibration's own discount curve does not move with the
  discount factors; rebuilt KRS, XCCY-for-FX and proxy curves are not persisted across
  jobs (`ShockedCurveCache.export` holds OIS curves only).

## 0.37.0 — OFZ curve risk in par quotes, closed-form gamma, benchmark equivalents

The desk's decision of 2026-10-23 on how OFZ curve risk is bumped
([proposals/ofz-curve-risk-factors.md](docs/proposals/ofz-curve-risk-factors.md), research
on hub data of 2024–2026): the KBD's risk factors become par quotes, like RUONIA's; the
Hessian is computed in zero space and mapped to them exactly; a hedge view in benchmark
bonds. Every new field is omitted at its default: earlier policies, job IDs, results and
pinned hashes are unchanged, and `rub_ofz_kbd_v1` / `v2` value and risk exactly as before.

### Added

- `CurveConvention.zero_shock="annual_par"` (`curve_basis = "annual_par"`) and spec
  `rub_ofz_kbd_v3`: a bucket shock moves the par yield of an annual-coupon instrument on
  the curve at 1Y, 2Y, 3Y, 5Y, 7Y, 10Y, 15Y and 20Y+ (curve-date annual roll, unadjusted,
  curve day count, at par). The curve is not refitted: an annual-zero overlay (hat nodes
  1Y–20Y, flat below 1Y) solved by Newton reprices all eight at their shifted yields;
  1M–6M have no instrument (the KBD has no OFZ there), zero risk, and moving them moves
  nothing. Every engine reads it through `_shock_curve`; the overlay is cached per curve
  and shift vector. `kbd_discount_curve(..., zero_shock="annual_par")`; `local_risk` and
  `local_worker` accept the basis.
- `BondRiskEngine.second_order(method="analytic")`: the exact gradient and Hessian of a
  fixed-cashflow bond on a `DiscountCurve` (any zero basis, any Z-spread convention,
  settlement after the curve date) from one pass over the flows; on `annual_par` mapped
  through the par Jacobian with the instruments' second-order term. Equal to the bumped
  second order up to its O(bump²) error (1e-7 relative). Model
  `ANALYTIC_SECOND_ORDER_MODEL_VERSION`. `InvalidTerms` with the reason for floaters,
  perpetual tails and OIS quote curves.
- `ValuationPolicy.second_order_method` (`"bump"` default, `"analytic"`): the closed form
  for every bond that has one in `value_portfolio` (the KBD benchmark book: 4.9 ms instead
  of 11.1 ms per position with the Hessian).
- Module `hedge_equivalents`: `benchmark_equivalents(benchmarks, positions)` →
  `BenchmarkHedge` / `BenchmarkEquivalent`: risk per benchmark bond's own effective annual
  yield (overlay nodes at the benchmarks' maturities, every spread held), yield DV01,
  hedge quantity, condition number with a warning above 50 (replaced in 0.38.0).
- `docs/proposals/ofz-curve-risk-factors.md`: the research, the owner's decisions and
  what this release implements.

### Changed

- `basis_view` docstring: with `rub_ofz_kbd_v3` the basis is par against par, exact at the nodes 1Y–20Y+.
- Floater coupon reuse in `BondRiskEngine` keys on the zero shifts a shock applies (a par
  shock moves nodes past its own); unchanged for the zero bases.

## 0.36.0 — CNY / USD / EUR bonds, FX forwards for any currency, never-called perpetuals

One release for the three pieces of work after 0.34.0: never-called perpetuals (merged
to `main` after 0.34.0), bonds in CNY / USD / EUR on their own currency's curve and the
FX book (forwards of any currency, crosses without RUB, NDF fixings). The bonds and the
FX book were built on a branch beside `main` as "0.34.0" and "0.35.0"; `main` released
0.34.0 for the KBD first, and the Trading Desk Monitor pinned the branch's "0.35.0"
(commit `f3cbabc`, without the KBD or perpetuals). 0.35.0 is therefore not released from
`main`, so a result stamped 0.35.0 names that branch build only. Every new field of an
existing type is omitted at its default: earlier encodings, job IDs, pinned hashes and
stored results are unchanged, and a book that uses none of the additions values exactly
as 0.34.0.

### Never-called perpetuals (BACKLOG #125)

The owner's position: Russian perpetuals are practically never called, so their value
and risk assume no call. Every new field is omitted at its default: earlier policies,
job IDs, results and pinned hashes are unchanged, and a policy without `never_called`
values exactly as before.

#### Added

- `ExerciseAssumption.never_called(assumed_coupons=())`: no call is exercised. A dated
  callable runs to maturity (as `to_maturity`); a perpetual pays its listed coupons and
  then the last one at the same step forever. Resolved as `exercise="never"` (no
  redemption date or price), identifier `exercise:never_called@never`.
- Module `perpetual`: `PerpetualTail(anchor_date, amount, period_days | period_months)`,
  `perpetual_tail(periods)` (the step of the last periods, the last known amount; an
  unknown coupon is not continued), `perpetual_yield` / `PerpetualYield` (yield and
  durations of the stream), `PERPETUAL_TAIL_MODEL_VERSION`. The tail is valued exactly:
  the curve is read to 30 years, its flat-forward rate beyond, the sum runs to 600
  years plus its geometric remainder; a divergent stream is worth infinity and the
  solvers start above it.
- `BondRiskRequest.perpetual_tail`, `FixedRubBondInput.perpetual_tail` (horizon
  `"perpetual"`), `price_cashflows(..., perpetual_tail=)`, `solve_z_spread(...,
  perpetual_tail=)`: the tail in the Z-spread, bucket DV01, CS01, Hessian and scenarios.
- `yield_never_called`; for a perpetual `call_analysis` adds the never-called scenario
  and `yield_to_worst`, `price_to_worst` and `to_worst` consider it (below par it is the
  worst). `ScenarioYield`, `ResolvedExercise` and `PriceToWorst` take `redemption_date`
  None for it; `ExercisedBond.perpetual_tail`.
- `value_portfolio` values a never-called perpetual: `horizon="perpetual"`,
  `horizon_date` its last listed coupon, yield and Macaulay duration of the stream,
  model flag `perpetual_tail`; to the put when the policy's horizon picks one.

#### Changed

- `yield_to_worst`, `price_to_worst` and `to_worst` no longer reject a perpetual: they
  include its calls and never being called (a call whose coupons are unknown is still
  rejected).
- `horizon_portfolio` and `value_assumed_portfolio` reject a never-called perpetual
  with `UNSUPPORTED_PRODUCT` (BACKLOG #126).
- `docs/callables.md`: the portfolio section said `value_portfolio` rejects callables;
  it has valued them under `exercise_assumptions` since 0.29.0.

### CNY / USD / EUR bonds on their own currency's curve

Package 3, phase 2 (Trading Desk Monitor R8 phase 2, work package P5). Every new field of
an existing type is omitted at its default, so earlier encodings, job IDs, pinned hashes
and `portfolio_result_v2.json` / `portfolio_result_v3.json` are unchanged.

#### Added

- **CNY bonds on the XCCY curve**: a built `cny_ruonia_xccy_v1` curve (the
  `DiscountCurve` of `bootstrap_xccy_curve`, CNY under RUB collateral) now discounts CNY
  bonds in `value_portfolio`; rate risk is `IR|<that curve>|<bucket>`, CS01 is `CS`
  against it. An entry holding XCCY quotes stays a rejection for bonds (`INVALID_TERMS`,
  "a bond needs the built curve").
- **USD / EUR par-yield proxies** (`proxy_curves`): specs `usd_ust_par_proxy_v1` (US
  Treasury par yields: semiannual bond-equivalent, bill BEY up to 6M) and
  `eur_ecb_aaa_par_proxy_v1` (ECB AAA par yields, continuously compounded),
  `CurveKind.PAR_YIELD_PROXY`, `CurveSpec.proxy` and `.par_coupon_frequency`;
  `ParYieldQuote`, `ParYieldCurveRequest`, `bootstrap_par_yield_curve` (every quote
  reprices to about 1e-12) and `par_yield`; `PAR_YIELD_PROXY_MODEL_VERSION =
  "par-proxy-logdf-v1"`. Conventions and sources in `CONVENTIONS.md`.
- **Proxy flag in results**: `CurveUse.proxy` (omitted when False).
- **Explicit fallbacks for a missing foreign curve**:
  `ValuationPolicy.foreign_curve_fallbacks` (`(currency, curve_id)` pairs, tried in
  order within a currency) and `.fallback_curves_for(ccy)`. A fallback is used only when
  the `foreign_curves` curve is absent from the snapshot, never for a present but unusable
  one, and never across currencies (a RUB curve is rejected as before); the row names the
  missing curve in `PositionValuation.curve_fallback_from`. No usable curve:
  `MISSING_MARKET_DATA` naming every curve tried.

#### Unchanged

- Without fallbacks a missing foreign curve is still `MISSING_MARKET_DATA`; RUB bonds,
  derivatives and their numbers are untouched. Horizon and the local worker still reject
  non-RUB bonds (BACKLOG #41).

### FX forwards for any currency, crosses and NDF fixings

What the desk monitor needs to value the FX book through `value_portfolio` (FX-forward
gap analysis, core packages C1–C3; roadmap review package P7: BACKLOG #46, #49, #103,
#131). Every new field of an existing type is omitted
at its default, so earlier encodings, job IDs, pinned hashes and
`portfolio_result_v2.json` / `portfolio_result_v3.json` are unchanged.

#### Added

- **Any currency (C1).** `get_curve_spec` resolves `<ccy>_discount_factors_v1` (caller
  discount factors) and `<ccy>_fx_implied_v1` (new `CurveKind.FX_IMPLIED`, model
  `fx-implied-logdf-v1`) for every three-letter code other than RUB, so CHF, GBP, HKD,
  JPY, KZT, AED or TRY forwards go through `value_portfolio` (tested equal to the
  standalone engine); `foreign_discount_spec_id(ccy)`, `fx_implied_spec_id(ccy)`. The
  generated specs are not listed in `CURVE_SPECS`.
- **Implied curves from swap points in the snapshot.** `FxImpliedCurveRequest(currency,
  curve_date, rub_curve_id, quotes, convention=None)` under a `<ccy>_fx_implied_v1`
  entry: `value_portfolio` builds `implied_foreign_curve` once per job on the snapshot
  `FxSpot` and the named RUB curve, so every quote reprices with zero basis; on such a
  curve an absent `FxBasis` is zero and a nonzero one is rejected. A built implied curve
  also discounts a bond of its currency.
- **FX crosses without RUB (C2)**, module `fx_crosses`: `FxCrossForward`, `FxCrossSwap`,
  `fx_cross_rub_legs` (the exact X/RUB + Y/RUB decomposition, any pivot),
  `FxCrossRiskEngine` (risk on the RUB, base and quote curves, both bases, FX delta per
  currency against RUB, exact scenarios `FxCrossScenario`, 37-factor Hessian, carry,
  points-held risk), `fx_cross_factor_risk`; position type `FxCrossInstrument`
  (`calendar_id` = the cross's settlement calendar), rows `PortfolioRiskResultV3.fx_crosses`
  and `PortfolioHorizonResult.fx_crosses`; model flag `fx_cross`.
- **NDF fixings (C3, BACKLOG #49 part).** `FxForwardLeg(..., fixing_date, fixing_source,
  fixing_rate)` on a cash-settled leg; a fixed leg is frozen at `N/q · (fixing − K) ·
  DF_rub(P)` (RUB risk only, no FX delta, outstanding to its payment date, no PV jump at
  a fixing equal to the forward). `NdfConvention(fixing_source, fixing_lag_days,
  value_lag_days)` with `from_payment` / `from_fixing` -> `NdfDates` and `.leg(...)`; no
  source or lag is built in. Snapshot `fx_fixings` (`FxFixing(currency, source,
  fixing_date, rub_per_unit, published_at=None)`, `MarketSnapshot.fx_fixing`):
  `value_portfolio` freezes a leg at its published fixing (`known_fixings`), projects one
  fixing today before publication (`fixing_pending`), and rejects one past its fixing
  date without a fixing (`MISSING_MARKET_DATA`) or with a fixing in its terms that
  differs from the snapshot's (`INVALID_TERMS`). Row legs report `fixing_date` and
  `fixing_rate`.
- **Points-held RUB risk (BACKLOG #46).** `FxForwardRiskEngine.points_held_rub_risk()`
  (`FxForwardPointsHeldRisk`) and `FxCrossRiskEngine.points_held_rub_risk()`: every
  outright held under the RUB shift, equal to rebuilding an implied curve at its quote
  dates; `ValuationPolicy(fx_points_held_risk=True)` adds
  `points_held_rub_buckets` to the rows (the factors keep the foreign curve held).
- **Basis scenarios (BACKLOG #103).** `PortfolioScenario(basis_shifts_bp=((factor, bp),
  ...))` moves `FXBASIS|<ccy>` (FX forwards, crosses, FX futures), `XCCYBASIS|<ccy>` and
  `FUTBASIS|<contract>` (rate and OFZ futures); `.basis_shift_bp(factor)`.
- **Fallback curves for FX products (BACKLOG #131).**
  `ValuationPolicy(fx_curve_fallbacks=((curve_id, fallback_id), ...))`: tried in order
  when an FX forward's, cross's, XCCY swap's or FX future's foreign curve is absent from
  the snapshot; a par-yield proxy is accepted only as such a fallback and needs its own
  calibrated `FxBasis`. Rows name the replaced curve (`curve_fallback_from` on FX forward,
  XCCY and FX futures rows; `base_/quote_curve_fallback_from` on crosses); model flag
  `curve_fallback`.

#### Changed

- An FX product's foreign curve may be any `<ccy>_discount_factors_v1` or
  `<ccy>_fx_implied_v1` curve; before, only CNY / USD / EUR had a spec.
- `docs/fx-forwards.md`: the stale "horizon_portfolio does not" carry FX forwards line
  is gone.

#### Unchanged

- Without the new terms, fields and options every number, encoding and hash is as
  without this section. Not done here: SOFR / €STR bootstraps (#40, no quotes in the
  hub), derivatives in the local worker (#102), key-rate / RUONIA basis swaps (#113).

## 0.34.0 — the KBD as a risk curve, KBD-linked floaters

The desk's decision of 2026-10-22 on `docs/proposals/kbd-risk-curve.md` (option C): the
KBD (`RUB OFZ`) becomes a full risk curve in the units of RUONIA OIS, OFZ are discounted
on it, and floaters whose coupon is the KBD at a constant maturity (MOEX `ZR_YLD_CRV`)
project off it. Every new field is omitted at its default and `rub_ofz_kbd_v1` is
unchanged, so earlier encodings, job IDs, pinned hashes and stored results are unchanged.

### Added

- **Annual-yield curve risk.** `CurveConvention.zero_shock` (`"continuous"` default,
  `"annual"`): a bucket shock of a `DiscountCurve` moves the effective annual zero yield,
  `DF' = (DF^(-1/t) + s)^(-t)`, reported as `curve_basis = "annual_zero"`. A
  continuous-zero bp is `1 + y` (14–16 %) larger than a par OIS or an annual Z-spread bp;
  annual-yield risk sums with them (0.5–2 % apart on OFZ).
- **Spec `rub_ofz_kbd_v2`**: the KBD from its ZCYC parameters with annual-yield risk;
  `kbd_discount_curve(..., zero_shock=)`; `CurveSpec.zero_shock`.
- **KBD index in floating coupons**: `FixingIndex` `"kbd"`,
  `FloatingCouponRule(index_tenor_years=, convexity=)`, `FixingHistory(tenor_years=)`;
  a projected fixing is the forward effective annual KBD yield over the tenor.
  `MarketSnapshot.projection_curve` accepts a KBD spec for it, so `value_portfolio` values
  KBD floaters (two-curve: KBD projection, RUONIA OIS discount).
- **`ConstantMaturityConvexity(normal_vol_bp)`**: an optional convexity adjustment of a
  projected constant-maturity fixing, `0.5 sigma^2 t (T + 1) / (1 + F)`; none by default.
- **`FloatingCouponPeriod.margin`**: a period's margin replacing the rule's (a margin
  reset at an offer).
- **`basis_view(risk, base_curve_id, curve_id, basis_id)`**: a `FactorRisk` on
  `(base, curve − base)` instead of `(base, curve)`, first order and Hessian (`J^T H J`).
- **Goldens of KBD floaters** (`tests/test_moex_kbd_floater_goldens.py`): 126 coupons of
  ten bonds to the kopeck from the hub's ZCYC history: KBD 7Y fixed on the 5th
  publication day before the coupon (KAMAZ, RUSNANO, RZD 001R) and 5Y on the 4th (RZD
  001B, margins reset at offers).

### Changed

- `local_risk` / `local_worker` accept `curve_basis="annual_zero"`; `RiskRunContext` of a
  bond on an annual-shock curve must say so.

## 0.33.0 — what the Trading Desk Monitor needs: review, floater accrued and explain, history at scale

Review of the core against the desk monitor's target interface
(`docs/reviews/CORE_REVIEW_FOR_DESK_MONITOR.md`, in Russian), with a fresh MOEX ISS
validation on the 2026-10-20 EOD (312-bond stratified sample, every traded RUB floater,
OFZ-IN, live G-spreads) and the gaps it found that are sound to close now: core brief
items C1, C7, C8 and BACKLOG #78, #81. Every new field of an existing type is omitted at
its default, so earlier encodings, job IDs, pinned hashes and
`portfolio_result_v2.json` / `portfolio_result_v3.json` are unchanged; a policy that
uses none of the additions values exactly as 0.32.0.

### Added

- **Floater accrued interest** (BACKLOG #81): `FloatingLeg.accrued_interest(settlement,
  curve=None, *, rate_basis=None)` and `Instrument.accrued(settlement, market)` for a
  floating leg. A coupon fixed in advance (published, or a `fixing_date` /
  `observation_window` rule) accrues pro rata on its amount in exact decimals (OFZ-PK) or,
  with `rate_basis`, on its rate (corporates); a coupon set in arrears accrues the rule
  over the elapsed part with the realised fixings. `FloatingCouponPeriod.published_rate`
  and `FloatingCouponLeg.accrual_basis` carry the published rate and the basis.
  `value_portfolio` and `horizon_portfolio` now value a floater whose quote has no
  accrued (they rejected it with `MISSING_MARKET_DATA` before). MOEX `ACCINT` on
  2026-10-20: 430 of the 453 traded RUB floaters on modelled benchmarks exact, 443 within a kopeck.
- **Floater P&L explain** (BACKLOG #78): `explain_horizon_pnl(..., fixings=,
  end_fixings=, projection_curve=, end_projection_curve=, strike_guard_bp=)`; at the end
  market the coupons are re-projected with the fixings realised over the horizon, which
  sit in `rates`. A floater without both fixings is still rejected.
- **`ValuationPolicy` desk options**, each off by default:
  - `spread_kinds`: `(instrument_id, "credit" | "ofz")` pairs, one call for a mixed
    book (core brief C8); `ValuationPolicy.spread_kind_for`;
  - `g_spread_curve_id`: a KBD curve every RUB fixed-coupon bond reports its G-spread to,
    whatever its discount curve (a bond discounted on RUONIA OIS had none);
  - `discount_margin`: `PositionValuation.discount_margin_bp` for floaters, the spread
    over the projection curve (`BondRiskEngine.discount_margin_bp()`);
  - `spread_buckets`: `PositionValuation.spread_buckets_rub`, the `CS` risk split by
    bucket in proportion to the discount-only key-rate risk
    (`BondRiskEngine.spread_bucket_risk`): CS01 by tenor, and the OFZ–RUONIA basis by
    tenor for OFZ spread to RUONIA OIS (OWNER_REQUESTS R18);
  - `require_known_coupons`: a fixed-coupon bond whose coupons do not run without a gap
    to its horizon is rejected (`MISSING_MARKET_DATA`) instead of valued without them
    (core brief C7);
  - `horizon="open_put"`: the first put whose exercise window is still open on the
    settlement date, confirmed or not (core brief C1); rows say `horizon="open_put"`.
- **Offer exercise windows** (core brief C1): `Offer(window_start=None, window_end=None)`,
  `Offer.is_open(on)`; `Instrument.horizon("open_put", settlement)`; `HorizonPolicy` and
  `horizon_valuation(redemption_horizon=)` accept `"open_put"`.
- **Spread-kind scenario shifts**: `PortfolioScenario(spread_kind_shifts_bp=(("ofz",
  20.0),))` moves the spreads of one kind only (an OFZ-basis or a credit-spread shock);
  `PortfolioScenario.bond_shock(kind)`.
- **History at scale**: `bond_metrics_batch(inputs, *, kbd=None, curve=None,
  yield_convention=..., spread_convention=None)` with `BondMetricsInput` (and
  `.from_instrument`) and `BondMetrics`: effective yield, Macaulay and modified
  duration, G-spread and Z-spread of many bonds at once with numpy, equal to the scalar
  functions to about 1e-12; an unsolvable bond gets None and a reason. 300 bonds × 250
  dates (74,430 bond-dates): 1.4 s, against 43 s for the scalar functions and about
  250 s for first-order `value_portfolio` (`benchmarks/history_bench.py`).
  `kbd_effective_yields(params, tenors)`: the KBD yield at many tenors.
- **MOEX goldens of 2026-10-20** (`tests/test_moex_eod_2026_10_20_goldens.py`,
  `tests/fixtures/moex_eod_2026-10-20/`): 32 bonds of every kind the desk holds;
  `DURATION` is identified as the Macaulay days at the published `YIELDATWAP`, rounded
  (155 of 155 bonds to maturity), and `GSPREADBP` as the yield minus the KBD at the
  duration (552 of 564 live rows exact).

### Changed

- `value_portfolio` / `horizon_portfolio`: a floater quote without `accrued_per_bond`
  takes the floater's own accrued instead of a `MISSING_MARKET_DATA` rejection.
- `FloatingLeg.identity` builds its periods with the canonical mapping (bytes equal to
  `asdict` for every period without `published_rate`).

## 0.32.0 — CBR decision dates held in the key-rate roll

BACKLOG #109 completed (owner decision: a CBR meeting date does not move with the
roll). No `value_portfolio` result, bond number or pinned hash moves; key-rate swap and
KEYRATE futures horizon numbers do.

### Changed

- **`roll_key_rate_curve(curve, h)`** keeps the next decision date (`current_rate_end`)
  and the rate in force until it; only the tenor pillars roll (each segment end moves by
  `h - d0`, the first segment still starts at the decision date). A horizon on or after
  the decision realises it at today's forward: the first segment's rate becomes the
  current rate, until its rolled pillar end (no later meeting date is known to the
  curve). A curve with no forward segment cannot be rolled past its decision
  (`MissingMarketData`).
- On the test book a 1-day theta of 20 KK-10.26 contracts (effective on the 26 October
  decision) is now 0 instead of +30,000 RUB; the 2Y key-rate swap's 1-day rolldown is
  −15.9k instead of −27.8k RUB (the rest was the meeting date sliding).
- Model versions `rub-irs-horizon-carry-v2` (key-rate swaps move; OIS swaps do not) and
  `rub-rate-futures-horizon-v2` (KEYRATE futures move; RUONIA futures do not).

## 0.31.0 — carry, rolldown and theta of every derivative

BACKLOG #101 completed. Every derivative engine carries to a horizon with the bond
semantics: `pv_forward` on today's curves read forward (today's forwards realised),
`pv_rolled` on curves moved to the horizon with their shape held (the desk theta),
flows received as cash, `carry + rolldown = horizon_pnl`. `horizon_portfolio` now
carries every product; FX products take a stated FX spot rule. No `value_portfolio`
result (v2 or v3), bond number, swap number or pinned hash moves.

### Added

- **FX spot rule at a horizon** `FxHorizon`: `"spot_held"` (default, sticky spot:
  forward points roll down) or `"forwards_realised"` (the spot moves to today's
  outright for the horizon spot date, so carry is the RUB funding only); `FX_HORIZONS`,
  `fx_horizon()`.
- **Curve views** `forward_curve(curve, h)` and `roll_key_rate_curve(curve, h)` beside
  `roll_curve`, in the new cycle-free module `_curve_views` (re-exported by `horizon`).
- **Engine horizons**, each with a result type, a convenience function and a model
  version `rub-<product>-horizon-v1`:
  - `FxForwardRiskEngine.horizon` / `fx_forward_horizon` (forwards and FX swaps; a
    cash-settled leg fixed by the horizon keeps today's forwards for its fixing);
  - `FxFuturesRiskEngine.horizon` / `fx_futures_horizon` (futures basis held; a contract
    delivered by the horizon settles at the spot or today's forward without the basis);
  - `XccyRiskEngine.horizon` / `xccy_horizon` (RUB and foreign floating legs keep
    today's forwards before the horizon; a quote-built foreign curve is rolled as built);
  - `CdsRiskEngine.horizon` / `cds_horizon` with `forward_hazard_curve` and
    `roll_hazard_curve` (no default before the horizon; coupons leaving the value
    between the two step-in dates are received);
  - `RateFuturesRiskEngine.horizon` / `rate_futures_horizon` (RUON, RUONIA index,
    KEYRATE: rates before the horizon realised at today's projection, so carry is zero
    and the theta is rolldown);
  - `OfzFuturesRiskEngine.horizon` / `ofz_futures_horizon` (bond spreads and basis held,
    cheapest bond reported per view; horizon settlement before delivery only).
  - Futures are margined: `pv_start` and `received` are zero, the money is the
    variation margin on the model price.
- **Repo theta**: `RepoCarry.pv_rolled_rub`, `.rolldown_rub` and `.horizon_pnl_rub`
  (observations after the horizon on the rolled projection curve, rolled discounting;
  `OMIT_IF_DEFAULT`).
- **`horizon_portfolio(..., fx_horizon="spot_held")`** carries FX forwards and swaps,
  XCCY, CDS, FX futures, rate futures and OFZ futures beside swaps and repos: row types
  `FxForwardPositionHorizon`, `XccyPositionHorizon`, `CdsPositionHorizon`,
  `FxFuturesPositionHorizon`, `RateFuturesPositionHorizon`, `OfzFuturesPositionHorizon`
  in fields `fx_forwards`, `xccy_swaps`, `cds`, `fx_futures`, `rate_futures`,
  `ofz_futures` (`OMIT_IF_DEFAULT`), `PortfolioHorizonResult.fx_horizon` and
  `.derivative_rows()`. The mixed v3 test book is carried with complete coverage
  (0.5 ms per position warm, 380 positions). `HORIZON_ROW_FIELDS`,
  `DerivativeHorizonHeader` and `horizon_spot_date` (today's spot lag from the horizon)
  in `portfolio_products`.

### Changed

- `RepoPositionHorizon` reports `rolldown_rub` and its `horizon_pnl_rub` is the theta
  (it was the PV carry in 0.30.0); covered rolldown sums every row. The carry rows of
  0.30.0 lead with the shared header fields (`carry_rub`, `rolldown_rub`,
  `received_rub`, `horizon_pnl_rub`) before their curve IDs.
- `HORIZON_ADAPTERS` take the FX rule: `(position, instrument, context, horizon_date,
  fx_horizon)`.
- The valuation adapters build their engines through shared builders reused by carry
  (no number moves).

### Open (BACKLOG #108–#110)

- A non-RUB CDS is carried with `"spot_held"` only; OFZ futures only before delivery; a
  future settling inside the horizon reports its unrounded model price.
- `roll_key_rate_curve` shifts the next CBR decision date with the horizon (as key-rate
  swaps have since 0.21.0); holding meeting dates fixed is an owner decision (done in
  0.32.0).
- Default-weighted CDS carry, rebootstrapping CDS and XCCY curves at the horizon, and
  funding of received cash and margin are not modelled.

## 0.30.0 — swap and repo carry in `horizon_portfolio`

BACKLOG #101, first part. `horizon_portfolio` now carries swaps and repo legs on the
engines `value_portfolio` builds for them; the other derivatives stay
`UNSUPPORTED_PRODUCT`. No number of a bond, a `value_portfolio` result (v2 or v3) or a
pinned hash moves.

### Added

- **Derivative carry in `horizon_portfolio`.** `instruments` takes any
  `PortfolioInstrument`. Swap positions (RUONIA OIS, two-curve OIS, key-rate swaps) get a
  `SwapPositionHorizon` row from `SwapRiskEngine.horizon` (carry with today's forwards
  realised, rolldown with the curve shape held, flows received to the horizon, horizon
  P&L); repo legs get a `RepoPositionHorizon` row from `RepoRiskEngine.carry` (PV carry,
  which is the horizon P&L, interest carry and flows received; the engine has no rolldown
  term, BACKLOG #100). Money is signed RUB for the position, the trade's result is in
  `per_trade`. The engines resolve curves, calendars, fixings and collateral prices
  exactly as in `value_portfolio`, with the same rejection codes.
- `PortfolioHorizonResult.swaps` and `.repos` (`OMIT_IF_DEFAULT`, empty for bond-only
  books). Covered carry and horizon P&L sum bonds, swaps and repos; covered rolldown sums
  bonds and swaps. The total is reported when coverage is complete, so a book of bonds,
  swaps and repos now has one.
- `HORIZON_ADAPTERS` and `horizon_adapter_for` (`portfolio_products`): the carry registry
  beside `PRODUCT_ADAPTERS`; adapters share the engine builders of the valuation
  adapters.

### Changed

- The `UNSUPPORTED_PRODUCT` message for a derivative in `horizon_portfolio` names what is
  carried ("horizon_portfolio carries bonds, swaps and repos").

## 0.29.0 — derivatives in `value_portfolio`, result contract v3

Portfolio integration of packages 2, 4, 5, 6, 7 and 8. A book with any derivative
position returns `rub-portfolio-risk-result.v3`; a bond-only book still returns v2 with
the bytes, `job_id` and hashes of 0.26.0 (pinned, and checked against a clean 0.26.0
checkout on Python 3.11 and 3.13 on Linux; on Windows the C maths library changes the
last bit, so there the tests check schema, hash consistency and a byte-exact round trip).
See `docs/portfolio-v3.md`.

### Added

- **Derivatives in `value_portfolio`, result contract `rub-portfolio-risk-result.v3`**
  (`docs/portfolio-v3.md`).
  - Position types (`rub_fixed_income.portfolio_instruments`): `SwapInstrument`
    (RUONIA OIS and key-rate swaps; projection curve and fixing IDs on the position),
    `FxForwardInstrument` (`FxForward` / `FxSwap`), `XccyInstrument` (a curve ID per
    role, RUONIA and foreign index fixings), `CdsInstrument`, `FxFuturesInstrument`,
    `RateFuturesInstrument` (RUON, RUONIA index, KEYRATE), `OfzFuturesInstrument`
    (`OfzDeliverable` basket by instrument ID and conversion factor) and
    `RepoInstrument` (assumed end of an open repo on the position).
    `value_portfolio(instruments=...)` takes any mix (`PortfolioInstrument`).
  - `MarketSnapshot` inputs, all optional and omitted at their defaults: `fx_spots`
    (`FxSpot` with value date and settlement calendar), `fx_bases` (`FxBasis` per
    currency and foreign curve), `credit_curves` (`CreditCurveEntry` by reference entity
    and currency, explicit recovery), `futures_quotes` (`FuturesQuote`: price and
    margined price), `index_fixings`, `ruonia_fixings` (declared unpublished days) and
    `ruonia_index`; `CurveEntry` takes key-rate forward curves (`rub_key_rate_swap_v1`)
    and XCCY quote sets (`cny_ruonia_xccy_v1`).
  - `PortfolioRiskResultV3` (a subclass of `PortfolioRiskResult`): per-product rows
    (`swaps`, `fx_forwards`, `xccy_swaps`, `cds`, `fx_futures`, `rate_futures`,
    `ofz_futures`, `repos`), factor totals over every product (`IR`, `REAL`, `CS`, `FX`,
    `FXBASIS`, `XCCYBASIS`, `FUTBASIS`), the book's sparse Hessian by factor name
    (`hessian_totals`), jump-to-default by entity for bonds and CDS (`jtd_totals`,
    `jtd_total_rub`, one sign rule), `position_models` (engine model version and flags
    per computed position), FX spots and credit curves used. Coverage and scenarios over
    every position. `verify_portfolio_result_v3`; `portfolio_result_to_json` writes and
    `portfolio_result_from_json` reads both v2 and v3. Pinned fixture
    `tests/fixtures/portfolio_result_v3.json`.
  - One engine per futures contract and job for rate and OFZ futures (`for_quantity`);
    shared shocked-curve, XCCY and CDS curve caches per job.
  - Coded rejections `EXPIRED_CONTRACT`, `SETTLED_LEG`, `UNDECLARED_FIXING_GAP`,
    `MISSING_BASKET_PRICE` (and `MISSING_ASSUMPTION` for an open repo without an end).
  - The `PRODUCT_ADAPTERS` registry (`rub_fixed_income.portfolio_products`): a new
    product adds a position type, a row type, an adapter and an `OMIT_IF_DEFAULT` row
    field (repo legs were added this way).
- **Per-curve scenario shifts** (`PortfolioScenario.curve_shifts_bp`, BACKLOG #43): named
  curves move by their own vector in every role and for every product; a real curve moves
  when named. `spread_shift_bp` moves CDS quotes and OFZ futures baskets too;
  `fx_shocks_pct` moves FX spots.
- **Native exercise assumptions** (BACKLOG #50): `ValuationPolicy.exercise_assumptions`
  values callable and perpetual bonds in `value_portfolio` and `horizon_portfolio` as the
  bond the resolved exercise implies; `PositionValuation.exercise_assumption` and
  `horizon="call"`. Numbers equal `value_assumed_portfolio`'s (unchanged).
- **The swap side of `rub_ruonia_ois_moex_v2`** (BACKLOG #61):
  `swap_convention("rub_ruonia_ois_moex_v2")`; `SwapConvention.float_compounding="nfa_act"`
  (NFA `RUB-RUONIA-OIS-COMPOUND`: `n_i / Act` weights, `Act` = 366 if any day of the
  period is in a leap year, amount `(growth - 1) * Act / d * Act/Act ISDA`). Par v2 swaps
  reprice the v2 bootstrap within 1e-6 bp.
- `key_rate.rebase_key_rate_curve(curve, business_date, *, rate_in_force)`: re-dates a
  TOM-calibrated key-rate curve to the business date (BACKLOG #55 dating rule).
- `benchmarks/portfolio_bench.py --cases mixed`: a 2k mixed book.

### Changed

- `SwapRiskEngine.base_id` hashes the swap through the canonical form instead of
  `dataclasses.asdict` (identical bytes for every existing convention; needed so the new
  `float_compounding` field is omitted at its default).

## 0.28.0 — repo and reverse-repo legs

Package 6 (core part), instrument valuation only: desk funding economics stay in the
backend's `desk_rules`. No pinned hash or existing number changes. Repo legs are not yet
in `value_portfolio` (BACKLOG #94).

- **Repo and reverse-repo legs** (`rub_fixed_income.repo`, package 6, instrument
  valuation only; `docs/repo.md`).
  - `RepoTrade(trade_id, side, start_date, end_date, start_amount_rub, rate, amount_rounding_decimals, collateral=None, cash_adjustments=(), currency="RUB")`:
    `reverse_repo` lends cash, `repo` borrows it; `end_date=None` is an open repo,
    valued to an explicit `RepoRiskRequest(assumed_end_date=)`.
  - `FixedRepoRate(rate, day_basis, interest="simple")`: MOEX Trading Rules Part III
    1.5.8.5, `S2 = S1 (1 + R (T365/365 + T366/366))`; `interest="capitalised"` credits
    the interest on every settlement day.
  - `FloatingRepoRate(benchmark, spread, day_basis, observation, projection_day_count, interest="simple", nonpositive_rate_replacement=None)`:
    RUSFAR or RUONIA of the previous settlement day (the last published value on days
    without one), or the key rate in force on each calendar day, plus a simple spread;
    simple (`S2 = S1 (1 + sum (r_i + s) w_i)`) or capitalised on settlement days
    (MOEX FRPO open repo); the MOEX KSU rule `r + s <= 0 -> 0.01 %`. Known fixings come
    from a read-only `FixingHistory` (RUSFAR in `index="ruonia"`, its daily-publication
    rules, on the RUSFAR calculation calendar); the rest are projection-curve overnight
    forwards, memoised on shared shocked curves with `floating`'s keys.
  - `RepoDayBasis`: `ACT/365-366` (each day of `(start, end]` over its calendar year:
    MOEX `T365/T366`, NFA's RUB default), `ACT/365F`, `ACT/360`.
  - `RepoCollateral(instrument_id, quantity, initial_discount_pct, pool)` and
    `RepoCollateralReference` (market value, exposure, coverage): a separate position
    reference, never in the PV or risk; `RepoCashAdjustment` for explicit margin calls,
    income compensation and repurchase adjustments (NFA 8, 10.1, 10.2).
  - `RepoRiskEngine`: `.valuation()` (PV of the remaining cash legs on the RUB discount
    curve, `S1`, `S2` and its source, known and accrued interest, current repurchase
    amount, repurchase price per bond, cashflows), `.risk()` (bucket DV01 per curve
    role, central), `.reprice(RepoScenario)`, `.second_order()` (11 or 22 factors),
    `.carry(horizon)` (accrual and forward-PV carry); `repo_factor_risk` to
    `IR|<discount>|<bucket>` and `IR|<projection>|<bucket>`; `repo_carry`.
  - `repo_start_amount`, `repo_initial_discount_pct` (MOEX 1.5.8.2,
    `S = (1 - D/100) Q (P0 + a0)`), `repo_end_amount` (1.5.8.5).
  - Model versions `rub-repo-leg-v1`, `rub-repo-leg-second-order-v1`.
- **MOEX repo goldens** (`tests/test_moex_repo_goldens.py`, fixture
  `tests/fixtures/moex_repo/`): the compounding RUSFAR open repo of MOEX's 10 September
  2026 presentation on the ISS RUSFAR history to the kopeck (credits 3 884,93,
  3 905,63, 11 885,96; `S2` 10 019 676,52), its slide 4 balances and slide 10
  «сумма к исполнению» 30 011 580,82; the EQM06 key-rate CCP repo example (known parts
  and `S2` of report days 1–3 and 6–8 within half a kopeck).
- `benchmarks/repo_bench.py` (2k positions). Sources with SHA-256 in
  `docs/sources/repo_moex/README.md`.

### Documented

- `project_financed_bond` compounds daily (`(1 + r/365)^days`) and is neither a repo
  contract nor the desk's funding engine (simple interest on a configured base, backend
  `desk_rules`; local audit F13). Unchanged; `docs/CONVENTIONS.md`,
  `docs/repo.md`.
- Booking rule: the cash borrower keeps the bond as an ordinary bond position, the cash
  lender has none; the collateral reference never enters PV or risk, so nothing is
  counted twice.

## 0.27.0 — OFZ, RUONIA and key-rate futures

Package 5 (rest, core part). New modules `rate_futures` and `ofz_futures`
(`docs/rate-futures.md`); no pinned hash or existing number changes. MOEX has listed no
OFZ futures since the June 2022 expiry, so the OFZ golden uses the December 2021 series.
Futures are not yet in `value_portfolio` (BACKLOG #87).

- `rate_futures`: MOEX one-month RUONIA futures (RUON), RUONIA index futures and KEYRATE
  index futures, terms from the MOEX specifications in force (interest-rate futures of
  10 March 2026; debt and money-market index futures of 8 September 2026) and the Bank of
  Russia / MOEX index methodologies. `RuoniaAverageFuturesContract`,
  `RuoniaIndexFuturesContract`, `KeyRateFuturesContract`,
  `ruonia_average_settlement_period`, step values and spec variation margin
  (`rate_futures_variation_margin`).
- Point-in-time RUONIA (`RuoniaFixings`): every business day before the valuation date
  needs a fixing; days the Bank of Russia publishes none (the last business day of a
  year) must be declared and carry the previous fixing. Undeclared gaps raise
  `MissingFixing`.
- Final settlement and index reproduction: `ruonia_average_final_price`,
  `ruonia_index_value` (Bank of Russia formula with the leap-year basis,
  `cbr_ruonia_year_fraction`), `ruonia_index_final_price`, `key_rate_index_value`.
- `RateFuturesRiskEngine`: price from fixings plus a RUONIA projection curve
  (`DiscountCurve` or OIS request) or a `KeyRateForwardCurve`, partially fixed periods,
  a calibrated basis frozen under shocks, bucket DV01, parallel DV01, basis01, exact
  scenarios, 12-factor Hessian, `for_quantity` sharing repricings.
- `ofz_futures`: OFZ basket futures (OFZ2/OFZ4/OFZ6/OF10/OF15) per the MOEX basket
  specification of 24 April 2015: `ofz_conversion_factor` (Appendix 4 formula, ACT/365F,
  exchange yield as input), `OfzFuturesContract.delivery_price_pct`,
  `ofz_default_delivery_issue` (item 4.4), `OfzFuturesRiskEngine` with carry on a
  funding curve, the cheapest to deliver, implied repo, gross and net basis, bond-curve
  and funding-curve buckets, CS01, basis01, exact scenarios (with a switch of the
  cheapest bond) and the 24-factor Hessian. Delivery options are not valued.
- Desk factors `rate_futures_factor_risk`, `ofz_futures_factor_risk` and a new factor
  key `FUTBASIS|<contract_id>` (`futures_basis_factor`).
- Goldens (`tests/test_rate_futures_goldens.py`, `tests/test_ofz_futures_goldens.py`)
  from ISS FORTS history, ISS news and the Bank of Russia web service: RUON final
  settlements of RRZ5..RRU6 (9 of 10 exact), RFU6 final settlement exact, the RUONIA
  index to 2e-15 including the 2025-12-30 gap, the KEYRATE index; December 2021 OFZ
  futures: 19 of 25 conversion factors exact (6 within 2e-4), all 25 delivery prices,
  all 5 default delivery issues, settlement prices within 0.04-0.7 %.
- `benchmarks/rate_futures_bench.py`: 2,000 positions, 12 contracts; with engines shared
  per contract 0.36 ms/position first order, 1.05 ms with the Hessian (zero curve),
  3.0 ms with an OIS projection rebootstrapped under shocks; one engine per position
  12.6 / 14.5 ms with the Hessian (Linux, Python 3.13).

## 0.26.0 — floaters: bounds, shifts, two curves, portfolio and MOEX goldens

Package 1 (floaters) and BACKLOG #29 (core part). Every new field of an existing type is
omitted at its default, so earlier encodings, pinned hashes and
`portfolio_result_v2.json` are unchanged. The single-curve floater risk path is
bit-identical to 0.8.0.

### Added

- **Floors and caps** (`CouponRateBounds(applies_to, floor, cap)`, `FloatingCouponRule(bounds=)`):
  per period, on the benchmark average (`"index_rate"`, before multiplier and margin) or
  the all-in rate (`"coupon_rate"`), applied before rounding. Priced at intrinsic value
  on the projected rate, and only when that rate is at least an explicit guard
  (`FloatingLeg(strike_guard_bp=)`, `ValuationPolicy.cap_floor_guard_bp`) from every
  strike; otherwise `UnsupportedProduct`. Fully fixed coupons are bounded exactly.
  `FloatingCouponProjection.bound`, `FloatingLeg.has_optionality`.
- **Index multiplier** (`FloatingCouponRule(multiplier=1.0)`).
- **Observation shifts** (`ObservationShift(days, unit, accrues_over)`): the period moves
  back in calendar or business days, rates and weights together;
  `accrues_over="observation"` accrues the amount over the shifted window (the MOEX
  OFZ-PK and corporate rule), `"accrual"` over the accrual period (ISDA).
- **In-advance observation windows** (`ObservationWindow(end_offset_days, end_unit, length_months=None)`):
  rate from a window ending before the accrual start, amount over the accrual period
  (old OFZ-PK 29007-29010: 6 months to the 2nd business day before the start; monthly
  corporates: a coupon-length window ending 6 calendar days before it).
- **Single fixings in advance or in arrears** (`averaging="single_fixing"`,
  `FixingDateRule(anchor, offset_days, unit)`) and **calendar-day key-rate observation**
  (`observation_days="calendar"`: each day takes the key rate in force from its CBR
  effective date).
- **Point-in-time fixings**: `FixingHistory(published_at=())` per-row publication times,
  `.published_by(cut)`, `.known_at`; `MarketSnapshot.point_in_time_fixing(fixing_id)`
  (history dated the business date, `known_at_utc` not after the cut, rows published
  after it dropped, neither given rejected). `MissingFixing` (a `MissingMarketData`
  and a `LookupError`) for a missing required fixing; nothing is ever filled.
- **Separate projection and discount curves** (`BondRiskRequest(projection_curve=)`):
  `TwoCurveBondRiskResult` with `projection_buckets`; a 23-factor central Hessian;
  `reprice` / `approximate(projection_curve_shifts_bp=None)`;
  `bond_factor_risk(..., projection_curve_id=)` emits `IR|<discount>|*`,
  `IR|<projection>|*` and `CS` with the cross terms;
  `FLOATING_TWO_CURVE_RISK_MODEL_VERSION = "rub-floating-two-curve-risk-v1"`.
- **Key-rate projection curve**: `key_rate_projection_curve(forward, calendar, *, rate_day_count, observation_days)`
  turns a `KeyRateForwardCurve` into a `DiscountCurve` whose overnight forwards are the
  key-rate path; registered spec `rub_key_rate_projection_v1` (projection only).
- **Floaters in the portfolio path**: `FloatingCouponLeg(projection_curve_id=None)`;
  `Instrument.cashflows(..., market=, strike_guard_bp=)` projects floating coupons;
  `Instrument.floating_leg(market, ...)`; `MarketSnapshot.projection_curve(curve_id, index)`.
  `value_portfolio` values RUB floaters (`SUPPORTED_PRODUCTS` adds `FLOATING_BOND`),
  single-curve when the leg projects off its discount curve and two-curve otherwise;
  `PortfolioScenario(projection_curve_shifts_bp=None)`. Coverage codes: no projection
  curve, non-RUB floater, near-the-money floor/cap `UNSUPPORTED_PRODUCT`; missing or
  unpublished fixings, accrued not supplied with the quote `MISSING_MARKET_DATA`;
  late-known or undated fixings, wrong projection spec `INVALID_TERMS`.
- **Floaters at the horizon**: `horizon_valuation(..., fixings=, projection_curve=, strike_guard_bp=)`
  and `horizon_portfolio` carry floaters (today's forwards realised up to the horizon,
  later observations off the rolled projection curve). `explain_horizon_pnl` rejects
  floaters with `UnsupportedProduct`.
- `benchmarks/floater_bench.py`: 2k synthetic floaters, single-curve OIS, KBD + RUONIA OIS
  and KBD + key-rate projection, first order and Hessian.
- Tests: `tests/test_floating_terms.py`, `tests/test_floating_two_curve.py`,
  `tests/test_portfolio_floaters.py`; the MOEX floater goldens now project every coupon
  on its real accrual dates with `ObservationShift` / `ObservationWindow`.
- **MOEX floater goldens** (`tests/test_moex_floater_goldens.py`, fixtures in
  `tests/fixtures/floaters/`: CBR DailyInfo RUONIA, key rate and term RUONIA; ISS
  bondization, descriptions and board rows, all fetched on 2026-10-17). `FixingHistory` and
  `FloatingLeg` reproduce **328 published coupons of 21 floaters to the kopeck** (70 of
  them OFZ-PK), the rates and amounts of 19 coupons of 4 in-advance OFZ-PK, and every
  running coupon the boards show as fixed (9 bonds):
  - OFZ-PK 29013, 29015, 29017, 29022, 29025 (RUONIA, quarterly): arithmetic RUONIA over
    the coupon period shifted back 6 calendar days, each day taking the latest fixing on
    or before it, with the day weights and the year fraction (ACT/ACT, so 1/366 in 2024)
    over that window. The rate is not rounded before the amount. Checked on 237 of 237
    coupons of 12 bonds from 2020 to 2026 during the analysis, 54 of them in the tests.
  - OFZ-PK 29026–29028 ("Срочная ставка RUONIA 3m") and VEB.RF "Индекс RUONIA" bonds:
    compounded RUONIA over the period shifted 7 days. `FloatingLeg` compounding equals
    CBR's published term RUONIA 3M to 1e-12 on over 200 dates, and the coupons follow the
    shifted period, not the published 3M rate (SU29026, which pays on the 4th, shows the
    difference).
  - Old OFZ-PK 29007–29010 (semiannual, set in advance): mean RUONIA over the 6 months to
    the 2nd business day before the coupon start, plus the margin, rounded to 0.01 %;
    amount on days/365 (46 of 46 coupons since 2021 in the analysis, the next-best
    window 43). The leg now reproduces the amounts too, with `ObservationWindow`.
  - Corporate RUONIA floaters (VEB.RF, VTB): the OFZ-PK formula plus the ISS margin.
    Monthly in-advance ones (Russian Railways, Rosagroleasing): the same-length window
    ending 6 days before the start, rate rounded to 0.01 %.
  - Key-rate floaters: period shifted 6 days (2 bonds), 4 days (1), and a native
    4-business-day lookback (2).
  - Also asserted: the business calendar is the RUONIA publication calendar (CBR computes
    no RUONIA on the last business day of the year, the working Saturday 2024-12-28
    included); a business-day lookback or observation shift does not reproduce the
    calendar-day shift; 2024 OFZ-PK coupons need ACT/ACT; in-arrears amounts use the
    unrounded rate.
  Every coupon is projected on its real accrual dates. The first coupons of SU29026 and
  SU29027 (December 2024: MOEX 52.66 and 52.71, core 52.72 and 52.75) stay a pinned,
  unexplained gap.

### Changed

- `HorizonValuation.accrued_start_rub` / `accrued_end_rub` are `float | None` (None for
  floaters: core assumes no floater accrual convention). Fixed bonds are unchanged.
- The single-curve floater risk path is bit-identical to 0.8.0 (PV, spread, `base_id`,
  risk, Hessian, scenarios, approximations; checked against the 0.17.0 code on flat,
  node and OIS curves with lookback and rounding), and faster: coupons that read only
  discount factors a shock leaves unchanged are reused, and overnight forwards are
  memoised on each shared shocked curve.
- `curve_specs._check_ois_spec_convention` (moved from `portfolio.py`) also validates
  RUONIA OIS projection curves.

### Policy decisions

- **Cap/floor optionality**: intrinsic value with an explicit moneyness guard (no
  default; None rejects every projected bounded coupon). The guard is checked on the
  base projection only; shocked curves and scenarios apply the bound deterministically.
- **Spread** of a two-curve floater is a Z-spread over the discount curve (`CS`), not a
  quoted discount margin over the projection curve.
- **Scenarios**: a scenario's curve shifts move a two-curve floater's projection curve
  too unless projection shifts are given; for single-curve floaters projection shifts
  are ignored in the portfolio and rejected by the engine.
- **Projection specs**: RUONIA projects only off a RUONIA OIS spec, the key rate only
  off `rub_key_rate_projection_v1`; KBD never projects.
- **Accrued interest of floaters** must come with the quote (`MISSING_MARKET_DATA`).
- **Yield and duration** of a floater in `PositionValuation` are on its base projected
  flows; `g_spread_bp` is None.
- **RUONIA publication calendar**: documented requirement; no-publication days are
  holidays of the history's calendar, never filled.

### Benchmark

`benchmarks/floater_bench.py`, 2k floaters (quarterly 1–7 years, current coupon partly
fixed, 6-day calendar shift, every fifth with a far floor), Linux, Python 3.13, ms per
position: RUONIA OIS single curve 17.8 first order / 74.5 with the Hessian; KBD discount
+ RUONIA OIS projection (23 factors) 19.6 / 100.5; KBD + key-rate projection 17.2 / 59.3.
Above the 10 ms fixed-bond target (BACKLOG #86).

## 0.25.0 — cross-currency swaps CNY/RUB vs RUONIA

Package 7 (core part). New module `rub_fixed_income.xccy` (`docs/xccy.md`). No existing
number, serialised form or pinned hash changes. XCCY swaps are not in `value_portfolio`.

- **Terms.** `XccySwap` with a RUB leg and a foreign leg (`XccyLeg`) of opposite sides.
  - The RUB leg is fixed, or floats on `RUB-RUONIA-OIS-COMPOUND`
    (`RUONIA_OIS_COMPOUND`, NFA 8.4(б): `Act` = 366 when a leap day is in the period).
  - The foreign leg is fixed, or floats on an explicit `CompoundedIndex` with
    `IndexFixings`.
  - Per-period notionals (`XccyPeriod`) and explicit `NotionalExchange`s: initial,
    intermediate (amortization) and final, built by `notional_exchanges` (NFA 2011
    6.4).
  - A simple spread on a floating leg (NFA 7.3(а)). Published amounts
    (`known_amount`) and known fixings are frozen.
  - Mark-to-market resets and non-RUONIA RUB indices raise `UnsupportedProduct`.
- **Standard swap.** `xcs_cnyrub_convention(calendar)` is MOEX `XCS CNYRUB`, from the SPFI
  curve methodology (25 December 2024, Appendix 1 §3): TOM start, MODFOL on the joint
  SPFI/MOSCOW/BEIJING/RUONIA calendar, CNY fixed vs RUONIA flat, single period to 1Y then
  annual with a short initial stub, Act/Act ISDA, +1B payments, notional at the NCC
  central rate of the trade date. The exchanges fall on the start and end dates, the NFA
  2011 6.2 defaults. `standard_xccy_swap` builds one from a tenor code. Spec
  `cny_ruonia_xccy_v1` is registered (`CurveKind.XCCY_BOOTSTRAP`) with its sources.
- **Valuation.** Foreign flows by covered parity at spot on a foreign discount curve
  under RUB collateral: `S * DF_rub(s) / DF_for(s) * DF_for(P) * exp(b tau)`. A swap
  with only a final exchange equals `FxForwardRiskEngine` exactly: PV, buckets, basis,
  FX delta and Hessian.
- **Bootstrap.** `bootstrap_xccy_curve(XccyCurveBootstrapRequest(...))` builds the CNY
  discount curve from the hub's `cny_ruonia_xccy` par quotes (`XccyParQuote`, the CNY
  fixed rate of MOEX `CNY-XCS-CNYRUO`).
  - One node per quote; every quote reprices within 1e-7 bp.
  - An optional FX-implied short end gives MOEX's hybrid curve (FX swaps up to 9M).
- **Par basis.** `xccy_par_rate(request, leg=...)` returns the par fixed rate or the par
  spread of either leg.
- **Risk.** `XccyRiskEngine`:
  - `cashflows()`;
  - bucket and parallel DV01 per curve role (RUB discount, RUB projection, foreign
    discount, foreign projection), `basis01`;
  - the exact FX delta (foreign leg RUB value / 100);
  - exact `XccyScenario`s (also from an `FxFuturesScenario`);
  - the central Hessian with exact FX × rates and RUB × CNY cross terms;
  - `xccy_factor_risk`: `IR|<curve>|<bucket>`, `XCCYBASIS|<ccy>`, `FX|<ccy>`.

  On a bootstrapped curve (`xccy_quote` basis) RUB shocks and quote shifts rebuild it,
  with the quotes held, through the shared `XccyCurveCache`.
- **Benchmark.** `benchmarks/xccy_bench.py --positions 2000`, Linux, Python 3.11:
  - 1.0 ms per position first order, 3.8 ms with the Hessian on discount-factor curves;
  - 1.4 ms and 9.5 ms on 17 OIS + 10 XCCY quotes (cold caches).
- **Tests.** `tests/test_xccy.py` (27): par repricing of 10 quotes on both bases and
  sides; a seasoned swap against an independent reconstruction (RUONIA by hand, leap
  year, two RUB curves, spread) and against `FloatingLeg`; the FX-forward identity; FX
  delta, signs, scaling, bucket sums, second-order bump convergence, the Hessian bit for
  bit against the generic helper; quote-basis risk; scenarios; rejections; round trip.
  Sources and SHA-256: `docs/sources/xccy_cnyrub/README.md`.
- Remaining work is BACKLOG #71–#77.

## 0.24.0 — single-name CDS

Package 8 (model).

Model only: no licensed CDS data source exists, so quotes, recovery and the discount
curve come from the backend. Every existing number and pinned hash is unchanged.

- **`rub_fixed_income.cds`: the ISDA CDS Standard Model.**
  - Hazard rates are piecewise constant. Discount factors and survival are log-linear in
    days between the merged node dates.
  - Protection runs from the start of the step-in day to the end of the maturity day.
  - The premium accrues ACT/360, and the last period includes the maturity date.
  - Accrual on default follows the Standard Model code (half-day bias). `EXACT` and
    `NONE` are explicit alternatives.
- **Conventions.** `CdsConvention` has no defaults. `CdsConvention.isda_standard(calendar)`
  gives the 2009 standard contract: quarterly IMM, following, ACT/360, step-in T+1,
  cash settlement T+3 business days, ACT/365F hazard time. Recovery is mandatory
  everywhere (DEC07).
- **Dates.**
  - `previous_imm_date`;
  - `standard_cds_maturity(..., roll="quarterly" | "semiannual")`, with the ISDA 2015
    semiannual roll;
  - `cds_trade_dates`, which gives step-in, cash settlement and the standard full-coupon
    accrual start;
  - `cds_premium_schedule`.
- **Curves and quotes.**
  - `HazardCurve` and `bootstrap_hazard_curve` from `CdsSpreadQuote` (par spread) and
    `CdsUpfrontQuote` (standard coupon plus clean upfront), with a stated recovery. Each
    quote reprices within 1e-12 of notional, and negative hazards are rejected.
  - Conversions `upfront_from_spread`, `spread_from_upfront`, `flat_hazard_from_spread`,
    `flat_hazard_from_upfront` and `cds_par_spread`.
- **Valuation.** `CdsContract` and `value_cds` value an outstanding contract by side.
  `CdsValuation` holds the legs, accrued premium, dirty and clean PV, cash settlement
  amount, par spread, risky annuity and points upfront.
- **Risk.** `CdsRiskEngine` holds the quotes and rebootstraps the hazard curve under
  every shock. It gives:
  - IR DV01 on the 11 desk buckets of the discount curve, plus parallel DV01, on the
    zero-overlay or OIS-quote basis;
  - CS01, parallel and per quote (desk factor `CS`);
  - `recovery01` (central, per 0.01);
  - exact scenarios and a 12-factor central Hessian;
  - JTD with the bond sign rule: a protection seller loses on default like a long bond,
    and a buyer gains.

  `cds_factor_risk` maps the results to `IR|curve|bucket` and `CS`. `CdsCurveCache` and
  `ShockedCurveCache` share shocked curves across a job's positions.
- **`_solver.illinois`.** A bracketed regula falsi with the `bisect` contract, used by
  the hazard bootstrap. Under a shock the bracket starts around the unshocked hazards:
  about 6 leg evaluations per quote, against about 7 from a cold bracket.
- **External goldens.** These are ISDA Standard Model values published in QuantLib's
  test suite (`test-suite/creditdefaultswap.cpp`):
  - 20 Markit upfronts (trade date 21 May 2009, 1Y–10Y, 10 and 1000 bp, recovery
    20 % and 40 %), reproduced to their ten published digits;
  - a negative-rate EUR reconciliation record (−16070.7, accrual 1000);
  - ten accrued-premium results from the ISDA CDS model website.
- **Tests** (`tests/test_cds.py`, 43):
  - the exact credit triangle at zero rates, the credit-triangle approximation, the
    zero-hazard limit;
  - a par contract has zero clean value, the bootstrap reprices every quote, the round
    trips;
  - buyer and seller mirror each other, bump convergence, buckets sum to the totals;
  - scenarios against risk, gradient against risk, a Taylor check, JTD, identical
    results with caches, rejections.
- **Benchmark** (`benchmarks/cds_bench.py`, 2,000 positions on 100 entities, Linux), ms per
  position: first order 3.9 cold / 2.4 warm on a zero curve, 4.6 / 2.9 on OIS quotes;
  with the 12×12 Hessian 34 and 40.5, about 288 hazard rebootstraps per entity
  (BACKLOG #70).
- **Docs.** `docs/cds.md`; `docs/API.md`,
  `docs/CONVENTIONS.md` and `docs/risk.md` are updated.

## 0.23.0 — OFZ-IN inflation linkers

Package 9, OFZ-IN (core part). The indexation rule is Minfin order No. 80n of 22.05.2015
as restated by the Bank of Russia's January 2021 methodological comment (the order
itself and Rosstat's index levels are not reachable from the build environment). Books
without linkers keep their factor set, bytes and hashes.

- `rub_fixed_income.inflation`: `CpiObservation` and `CpiSeries` (point-in-time monthly
  CPI levels: consecutive months, publication dates, no look-ahead, `MissingMarketData`
  for an unpublished month, `known_on`); `CpiIndexation` (the OFZ-IN rule of Minfin order
  No. 80n of 22.05.2015 as restated by the Bank of Russia: `INDEX = CPI(M-4) +
  (CPI(M-3) - CPI(M-4)) * (n-1)/d`, face `= initial * INDEX / INDEX_base` in kopecks,
  explicit `base_index` when published); `IndexedCouponPeriod`, `IndexedCouponLeg`,
  `IndexedPrincipalLeg` (deflation floor at the initial face); `indexed_coupon_amount`
  and `indexed_accrued` (formulas 4 and 5); `INFLATION_MODEL_VERSION`.
- `rub_fixed_income.linkers`: `linker_nominal`, `linker_accrued`, `linker_coupon`,
  `linker_redemption`, `real_cashflows` (flows at the settlement-date face held constant).
- `Instrument` accepts the indexed legs for `ProductType.INFLATION_LINKED_BOND` (new
  union members, no new fields; existing instruments serialize and hash unchanged);
  `Instrument.is_indexed`.
- `MarketSnapshot.cpi` and `.cpi_series(id)` (omitted from canonical JSON when empty).
- Curve spec `rub_real_discount_factors_v1` (`CurveKind.REAL_DISCOUNT_FACTORS`).
- **New desk factor type** `REAL|<curve_id>|<bucket>` (`real_factor`,
  `linker_factor_risk`). It appears only for computed linkers.
- `value_portfolio` values inflation-linked bonds on the real curve named in
  `curve_overrides`: indexed face and accrued from the snapshot CPI, real yield and
  duration, `REAL|…` + `CS` risk with the Hessian, JTD on the indexed face. It records
  `PortfolioRiskResult.cpi` (`CpiUse`; omitted when empty).
- MOEX/CBR goldens (`tests/test_ofz_in_goldens.py`, fixtures `tests/fixtures/ofz_in/`):
  the four OFZ-IN in circulation over their whole history (5,927 EOD rows). `ACCINT` is
  exact on every row and every paid coupon (45) is exact. The 3-month lag is confirmed
  (lags 2 and 4 rejected by the data). The face from CBR CPI is within 2.6e-4. The
  2026-10-16 accrued from CPI is exact. Real yield at `CLOSE` matches `YIELDCLOSE`
  within 0.5 bp and `DURATION` within 0.2 days.
- `benchmarks/linker_bench.py`: 2k linkers, 2.7 ms/position first order and 11.6 ms with
  the Hessian (Linux, Python 3.13); 3.2 / 11.8 ms on Python 3.11.
- **Face accuracy.** The face is within 2.6e-4 of MOEX (worst 0.36 RUB), not to the
  kopeck, because the only reachable official CPI (CBR) is month-on-month rounded to
  0.01 pp; Minfin indexes on Rosstat's levels (BACKLOG #62). Accrued interest, coupons,
  the lag and the real yield match.

### Changed

- `PortfolioScenario` P&L of a linker position is `UNSUPPORTED_PRODUCT` (scenario shifts
  are nominal); a nominal bond on a real curve and a linker on a nominal curve are
  `INVALID_TERMS`.
- `callables` rejects indexed instruments with `UNSUPPORTED_PRODUCT` (it could not
  receive one before: linkers did not construct).
- Contract: books without linkers keep their factor set, bytes and hashes (pinned
  fixtures unchanged). With linkers, `factor_totals` gains `REAL|…` keys and the result
  carries `cpi`. Schema id unchanged (`rub-portfolio-risk-result.v2`; new fields are
  omitted at their defaults).

## 0.22.0 — the MOEX RUONIA OIS spec

Roadmap item 0.5, audit F08, BACKLOG #37. The legacy spec `rub_ruonia_ois_legacy_v1`, the
default bootstrap, every curve identity, number and pinned hash are unchanged.

- **`rub_ruonia_ois_moex_v2`** is registered from primary texts retrieved on 2026-10-17
  (sources and SHA-256 in `docs/sources/ruonia_ois_moex_v2/README.md`; the documents are
  not stored):
  - MOEX «Методика формирования ценовых кривых СПФИ» (25 December 2024), Appendix 1,
    column "OIS RUONIA" (https://www.moex.com/ru/documents/27002);
  - NFA amendments No. 1 (17 August 2021) to the 2011 interest-rate standard terms,
    8.4(б), `RUB-RUONIA-OIS-COMPOUND`, with the 2011 terms 2.2, 7.2–7.3 and 7.4(е).

  Each quote is the par rate of the standard swap traded on the curve date: start TOM,
  end start + tenor; modified following on the caller's joint SPFI / MOSCOW / RUONIA
  calendar; a single period up to 1Y, annual periods beyond, short initial stub;
  Act/Act ISDA on both legs, both paid one business day after each period end; floating
  leg compounded RUONIA with no fixing offset or lookback, annualised by NFA's `Act / d`
  (`Act` = 366 if any day of the period falls in a leap year).
- **`CurveConvention(ois_swap=OisSwapConvention(calendar, ...))`**: an optional quote
  convention for the bootstrap's par-rate leg (spot lag, payment lag, business-day
  convention, single-period limit, frequency, stub, day counts, index).
  `ois_curve_convention(spec_id, calendar=None)` builds the convention of a registered OIS
  spec. While `ois_swap` is `None` it is left out of the identifier, canonical bytes and
  `to_dict` (`OMIT_IF_DEFAULT`).
- **Bootstrap.** With `ois_swap`, each quote's tenor is resolved with `resolve_tenor` and
  must land on the quote maturity (`InvalidTerms` otherwise, and for `ON`/`TN`/`SN`).
  Nodes stay at the maturities; because a quote's last payment falls a business day
  after its node, the sequential solve repeats until every quote reprices within
  1e-9 bp. A 17-tenor curve takes about 85 ms, against 20 ms for the legacy bootstrap.
- New API: `OisSwapConvention`, `OisSwapPeriod`, `ois_quote_periods`,
  `DiscountCurve.quote_par_rate` (now used by `max_repricing_error_bp`; legacy numbers
  identical), `DiscountCurve.ois_swap_par_rate`, `ois_curve_convention`, and `CurveSpec`
  fields `float_leg_day_count`, `stub`, `calendars`, `sources` (empty by default).
- `value_portfolio` rejects (`INVALID_TERMS`) an OIS curve whose convention does not match
  its spec.
- Tests (`tests/test_ruonia_ois_moex_v2.py`, 10): periods, payment lag over a holiday, the
  short initial stub, MODFOL month ends, the leap-year `Act` rule, exact repricing of the
  17 MOEX tenors with an independent par computation for 1W and 3Y, unchanged legacy
  bytes, round trip, rejections, `value_portfolio` on v2 with seeded shocked curves
  byte-identical to a cold run. `swaps.swap_convention` does not yet derive a swap
  convention from v2 (BACKLOG #61).

## 0.21.0 — seasoned interest-rate swaps

Package 2 (core part). New module `rub_fixed_income.swaps` (`docs/swaps.md`); no existing
number, serialised form or pinned hash changes. Swaps are not yet in `value_portfolio`
(BACKLOG #55).

- **Terms.** `InterestRateSwap`: payer (pays fixed) or receiver, constant notional, fixed
  rate, simple float spread, effective date and unadjusted termination date;
  `.from_tenor` resolves the dates with `resolve_tenor`. Non-RUB raises
  `UnsupportedProduct`.
- **Conventions.** `SwapConvention` and `SwapLegSchedule` make every convention explicit:
  index and averaging (RUONIA compounded, key rate arithmetic), each leg's frequency,
  roll (`backward`, `forward`, `legacy-annual-forward`), day count and single period of
  short swaps, the business-day convention, spot lag and payment lag.
  `swap_convention(spec_id)` derives it from `rub_key_rate_swap_v1` (the schedule of
  `price_key_rate_swap`, period by period) and `rub_ruonia_ois_legacy_v1` (the legacy
  bootstrap's swaps); `swap_schedule` returns both legs' periods.
- **Known resets** from a point-in-time `FixingHistory`. RUONIA compounds per
  business-day segment up to the valuation date, then the projection curve's compounded
  overnight forwards; the key rate averages calendar days, then
  `KeyRateForwardCurve.arithmetic_average`. A started period without fixings or a
  RUONIA gap raises `MissingMarketData`; nothing is filled.
- **Two curves.** A discount curve (`DiscountCurve` or OIS quotes) and a projection
  curve: `None` (single curve), a separate RUONIA curve, or a `KeyRateForwardCurve`.
  The valuation date is the curves' date.
- **`SwapRiskEngine`**: `valuation()` (PV, legs, accrued, clean PV, par rate, annuity,
  cashflows by source), `risk()` (central bucket DV01 per curve role, KRD per notional,
  payer DV01 positive), `reprice(SwapScenario)` (exact, split into discount, projection
  and interaction P&L), `second_order()` (11 or 22 factors; the cross block uses the
  exact bilinear form) and `horizon(date)` / `swap_horizon` (carry and rolldown).
  `swap_factor_risk` maps to `IR|<curve_id>|<bucket>`. Key-rate projection risk shifts
  the forward key rate after the next meeting; a discount shock does not recalibrate
  the key-rate curve.
- **Tests** (`tests/test_swaps.py`, 30): par OIS and KRS swaps reprice every quote within
  `1e-12` of the notional; seasoned OIS (two curves, payment lag, spread, weekend
  fixings) and KRS (a key-rate change inside the period) match an independent cashflow
  reconstruction; DV01 signs, bucket sums, bump convergence, Taylor vs exact.
- **Benchmark** (`benchmarks/swap_bench.py`, 1000 OIS + 1000 KRS, Linux, Python 3.11),
  ms per position: OIS 1.5 first order, 8.6 / 4.7 with the Hessian cold / warm; KRS 1.9,
  8.5 / 5.4. CI runs it on Windows next to the portfolio benchmark (informational).

## 0.20.0 — callables, perpetuals and constant-CPR pass-throughs

Package 9, callables and MBS/ABS (core part). New modules only: `value_portfolio`,
`horizon_portfolio`, every result contract, serialized instrument and pinned hash are
unchanged, and callables and perpetuals passed to `value_portfolio` are still rejected.

- **`callables`.** Explicit exercise assumptions with a recorded identifier:
  `ExerciseAssumption.to_maturity()` (default, today's behaviour), `.to_call(date)`,
  `.to_worst()`, each with optional explicit `assumed_coupons` for coupon resets. The
  call schedule is the existing `Offer(kind=CALL, price_pct=...)`.
  - `call_analysis`, `yield_to_call`, `yield_to_maturity`, `yield_to_worst` (with the
    worst date) and `price_to_worst`, on the instrument's yield convention;
  - `extension_risk`: duration to maturity minus duration to call, and the value change
    if the call is skipped;
  - `resolve_exercise` and `exercised_instrument`: the plain bond an assumption implies,
    under the same ID. Coupons must be known up to the redemption date, which must be a
    coupon date, otherwise `InvalidTerms`; core never extrapolates coupons.
  - The exercise is fixed for risk: `to_worst` is resolved once at the quoted price and
    held through bumps, scenarios and the Hessian.
- **Perpetuals** are priced to a named call (`to_call`); reset coupons are explicit or
  the call is rejected; `to_maturity` and `to_worst` are `UnsupportedProduct`.
  `perpetual_current_yield` reproduces MOEX's perpetual yield (coupon rate / price).
- **OAS is rejected** with a typed reason (`option_adjusted_spread`): core has no
  short-rate model.
- **`pass_through`.** Amortizing pass-through MBS/ABS under a deterministic constant CPR
  (`PassThroughTerms`, `PassThroughPeriod`, `PrepaymentAssumption`): monthly prepayment
  `1 − (1 − CPR)^(1/payments_per_year)`, scheduled principal scaled by the surviving
  balance, optional clean-up call; `project_pass_through`, `weighted_average_life`,
  `pass_through_analytics` (prepayment-adjusted yield, WAL, durations), `cpr_sensitivity`
  (PV and WAL per 1 pp CPR at a held Z-spread), `pass_through_instrument`.
- **`assumed_portfolio`.** `value_assumed_portfolio` and `horizon_assumed_portfolio`
  value a book whose callables, perpetuals and pass-throughs carry assumptions
  (`AssumptionPolicy`, `InstrumentAssumption`) through the unchanged portfolio
  functions. `AssumedPortfolioResult` records each `AssumptionUse` (identifier, resolved
  date, WAL, hashes of the given and valued terms), coverage over all positions and its
  own hash (`verify_assumed_result`). New rejection code `MISSING_ASSUMPTION`
  (`MissingAssumption`). Only par calls are accepted on this path.
- **MOEX findings and goldens** (`tests/test_moex_call_goldens.py`, fixtures in
  `tests/fixtures/moex_calls/`, 2026-10-15):
  - of 156 TQCB call bonds, 7 carry `CALLOPTIONDATE`; for each traded one
    `YIELDATWAP == CALLOPTIONYIELD` and `DURATION == CALLOPTIONDURATION`. MOEX yields to
    the call once the call date is set, to maturity otherwise;
  - RU000A1087A7 and the ABS RU000A10AST0 match `CALLOPTIONYIELD`, `YIELDCLOSE` and
    `CALLOPTIONDURATION` within 0.5 bp and a day, directly and through
    `value_assumed_portfolio`; RU000A10B7W1 to maturity still matches;
  - perpetual yields of RU000A0ZZE87 and RU000A1034P7 (MOEX publishes no duration).
  No MBS golden: ISS does not publish the CPR behind issuers' projected amortizations.
- Benchmark (`benchmarks/assumptions_bench.py`, 2k positions on the KBD, half callables
  to worst, a quarter perpetuals, a quarter 5-year monthly pass-throughs at 15 % CPR):
  1.2 ms per position to prepare, 5.2 ms first order, 13.9 ms with the Hessian.
- Docs: `docs/callables.md`, sections in `docs/API.md`, `docs/CONVENTIONS.md`,
  `docs/risk.md`.

## 0.19.0 — OTC FX forwards and FX swaps

Package 4 (core part) and BACKLOG #34. New module `rub_fixed_income.fx_forwards`; FX
futures numbers and every pinned hash are unchanged.

- **Instruments.** `FxForwardLeg`, `FxForward` (also an unsettled spot trade) and `FxSwap`
  (near and far legs of opposite sign; `swap_points`, `far_leg_forward()` once the near
  leg has settled). Notionals are signed foreign units (+ = receive foreign), rates are
  RUB per an explicit `quote_units`.
- **Settlement differs from futures.** A leg pays `N/q · (F(T) − K)` on its value date, or
  on an explicit payment date for a cash-settled leg, discounted on the RUB curve;
  futures margin stays daily and undiscounted. `F` is the covered-parity outright of
  `fx_futures` with a flat basis frozen under every shock. Legs dated before the curve
  date are rejected as settled. `basis_bp` is a required input: zero is right only on an
  implied foreign curve.
- **Risk.** `FxForwardRiskEngine`: PV with per-leg outright and points; RUB and foreign
  11-bucket DV01 and parallel shifts; `basis01_rub`; exact FX delta per 1 %; exact
  scenarios with `FxFuturesScenario` (one scenario shocks both books); a 24-factor
  Hessian (RUB, foreign, basis, spot) whose spot row is exact, which gives the FX × rates
  cross terms. Factor pairs the trade does not read are skipped, bit-identically.
  `fx_forward_factor_risk` maps to `IR|<curve>|<bucket>`, `FXBASIS|<ccy>` and `FX|<ccy>`.
- **Value dates** on explicit calendars: `joint_calendar` (a business day in every
  calendar), `fx_value_date` (`TOD` only on settlement days, `TOM`, `SPT`, tenors from an
  explicit spot lag) and `fx_swap_value_dates`.
- **Parity helpers:** `fx_forward_outright`, `fx_forward_basis_bp`, and money-market
  `cip_outright`, `cip_implied_rub_rate`, `cip_implied_foreign_rate` with both day counts
  explicit.
- **Implied foreign curve** from caller-supplied swap points, OTC outrights or FX futures
  prices (`FxSwapPointsQuote`, `FxOutrightQuote`, `implied_outrights`,
  `implied_foreign_curve`). It reprices every quote exactly with zero basis and must be
  anchored at the curve date (a TOD/TOM swap).
- **MOEX goldens** (`tests/test_moex_fx_swap_goldens.py`, raw ISS fixtures in
  `tests/fixtures/moex_fx_swaps/`):
  - all 91 CNY/RUB TOD/TOM periods from 2026-06-01 to 2026-10-16, including the 8-day
    National Day period, run on the joint Russian and Chinese calendar, and the board's
    settlement dates match `fx_value_date`;
  - MOEX's `SRATE_CNY_ON` implied RUB rate is ACT/365F money-market parity on its
    published points with no CNY rate term: mean error −1.0 bp, median 3.4 bp, max 22 bp
    against the day's TOD WAP spot. ACT/360 and a CNY rate term are rejected by the data.
- **Benchmark** (`benchmarks/fx_forward_bench.py`, 2k CNY/RUB forwards and swaps, Linux,
  Python 3.11), ms per position: 0.5–0.6 first order; 1.3 (RUB discount factors) and 1.8
  (17-tenor OIS quotes) with the Hessian; 1.6 and 2.5 with five scenarios.
- Docs: `docs/fx-forwards.md`, `docs/API.md`, `docs/CONVENTIONS.md`.

## 0.18.0 — non-RUB bonds, FX delta and the yield time basis

Package 3 (core part) and BACKLOG #38. RUB-only instruments, policies, scenarios and
results serialize and hash exactly as before.

- **Yield time basis as an issue term (BACKLOG #38).** `Instrument.yield_convention`
  (default `DEFAULT_YIELD_CONVENTION`, effective annual ACT/365F) and
  `YieldRequest(..., convention=...)` set the time basis of the effective annual yield
  independently of the accrual basis. ACT/360 and ACT/ACT-ISDA apply only when given
  explicitly, and only annual compounding is accepted. The basis comes from the backend's
  static issue terms; core never infers it.
- **MOEX golden for USD replacement bonds.** Nine more TQOD bonds of 2026-10-15 from ISS
  (raw, provenance in `tests/fixtures/moex_eod/README.md`). On their issue basis all 11
  USD bonds match `YIELDATWAP` within 0.005 and `DURATION` within a day, seven on ACT/360
  and four on ACT/365F; on the other basis they miss by 5 to 40 bp. Also end to end
  through `value_portfolio`. The known-gap test became
  `test_usd_bond_yield_on_its_issue_time_basis_matches_moex`.
- **Non-RUB bonds in `value_portfolio`.** CNY, USD and EUR fixed bonds (bullet,
  amortizing, confirmed put) are discounted on the curve that
  `ValuationPolicy.foreign_curves` names for their currency, with no fallback, and valued
  in RUB at the snapshot FX rate (`FxRate`, RUB per unit):
  - PV, `IR|<curve>|<bucket>`, `CS`, the Hessian, scenarios and JTD are in RUB;
  - `FX|<ccy>` = `PV_ccy · FX / 100` per 1 %, with FX × rates and FX × spread cross terms
    (`first_order_i / 100`) and no FX gamma; they equal central differences exactly;
  - new `PositionValuation.currency_amounts` (`CurrencyAmounts`),
    `PositionValuation.yield_convention`, `PortfolioRiskResult.fx_rates`,
    `PortfolioScenario.fx_shocks_pct`, `SUPPORTED_CURRENCIES`,
    `fx_translated_factor_risk`.
- Curve specs `cny_discount_factors_v1`, `usd_discount_factors_v1`,
  `eur_discount_factors_v1` (`CurveKind.DISCOUNT_FACTORS`) take a caller-built
  `DiscountCurve` dated the business date. No foreign bootstrap is registered: its quote
  conventions are not in hand.
- Typed rejections: no curve mapped for the currency, or curve or FX rate missing →
  `MISSING_MARKET_DATA`; a curve of another currency, not dated the business date or not
  a node curve → `INVALID_TERMS`; other currencies, non-annual yield quotes and non-RUB
  bonds in `horizon_portfolio` → `UNSUPPORTED_PRODUCT`. `BondRiskRequest` stays RUB-only.
- `value_portfolio` reports `effective_yield` and `macaulay_duration_years` on the issue's
  yield basis (ACT/360 years = days / 360); the G-spread to the KBD always uses ACT/365F.
- `canonical.OMIT_IF_DEFAULT` field metadata: a field added to an existing type is left
  out of the canonical JSON while it holds its default, and `from_dict` accepts it as
  absent. `canonical_json_bytes` converts dataclasses field by field (byte-identical to
  the former `asdict` path).
- The result schema stays `rub-portfolio-risk-result.v2`. A result with non-RUB rows
  carries the new keys, which an older release's strict decoder rejects; whether the
  backend needs a `v3` is its contract call.
- Benchmark (`benchmarks/portfolio_bench.py`, new `fx` case, 2k CNY/USD/EUR positions,
  Linux, Python 3.11): 1.91 ms per position first order, 7.52 ms with the Hessian,
  7.85 ms with scenarios including a 10 % FX move. The RUB rows are unchanged
  (OIS + Hessian 9.49 ms, KBD 9.51 ms).

## 0.17.0 — rate-based accrued interest and MOEX end-of-day goldens

Roadmap item 0.10 (the golden bond set) and BACKLOG #36. New API only; existing instruments,
their serialised form and every pinned hash are unchanged.

- **Accrued interest on the coupon rate.** MOEX EOD data show two accrual rules:
  - OFZ accrue pro rata on the coupon amount (the Minfin rule, as `FixedCouponLeg` already
    does);
  - corporate bonds accrue `face * rate * days / 365` on the face outstanding at the period
    start. When the coupon amount was rounded, that can be a kopeck away from pro rata. In
    a sample of 145 RUB corporates on 2026-10-15, 144 match this rule, and 16 of them were
    a kopeck off under the old one.

  New types:
  - `RateCouponPeriod`: a `CouponPeriod` plus its annual `rate`;
  - `RateAccrualCouponLeg(periods, accrual_basis)`;
  - `AccrualBasis`: `RATE_ACT_365` (MOEX corporates), `RATE_ACT_360`, and `RATE_30_360`
    (30/360 bond basis, used by replacement bonds of USD eurobonds).

  `Instrument.accrued` uses the leg's basis, and so do `value_portfolio` and the horizon
  functions. Cash flows are the same as with `FixedCouponLeg`. These are new types, not
  new fields, so a 0.16.0 `Instrument` decodes and hashes exactly as before.
- **MOEX end-of-day goldens** (`tests/test_moex_eod_goldens.py`, fixtures in
  `tests/fixtures/moex_eod/` fetched from ISS). 18 bonds, trade date 2026-10-15:
  - `ACCINT` to the kopeck for all of them: 5 OFZ-PD, corporates, 2 amortizers, a call
    bond, 5 put bonds, an ACT/360 bond, 2 USD bonds on 30/360. Each board needs its own
    rule, and the tests show the other rule a kopeck off.
  - `YIELDATWAP` within 0.5 bp and `DURATION` within a day for 12 bonds to maturity (a
    call is not assumed exercised, as on MOEX). For OFZ, also `YIELDCLOSE` at `CLOSE`, and
    end to end through `value_portfolio` on the EOD KBD.
  - Put bonds: `YIELDTOOFFER` at `CLOSE` within 0.06 bp and `DURATION` to the offer.
  - Per trade (RU000A10C5Y0, session of 17 October): `EFFECTIVEYIELD` to the offer within
    0.006 bp, `DURATION`, and `GSPREADBP` within 0.5 bp on the KBD the exchange used
    (`tests/fixtures/kbd/zcyc_2026-10-16.json`).
- **BACKLOG #36 resolved.**
  - The 30/360 gap on RU000A0JXTS9 is closed with `RATE_30_360`.
  - The short put "gap" is not a core error. For put bonds, MOEX's `YIELD` and
    `YIELDATWAP` / `YIELDATPREVWAPRICE` are a different, higher measure. The effective
    yield to the offer is published as `YIELDTOOFFER` and `trades_yields.EFFECTIVEYIELD`,
    and core reproduces both.
  - Recorded, not built: some USD bonds are quoted on an ACT/360 time basis, others on
    ACT/365F, and ISS does not say which (BACKLOG #38, package 3).

## 0.16.0 — tenor resolver

Roadmap item 0.5, part. New API only.

- `tenors.py` adds:
  - `parse_tenor` and `Tenor`;
  - `add_tenor`;
  - `resolve_tenor(trade_date, tenor, calendar, *, spot_lag_days=1, business_day_convention=MODIFIED_FOLLOWING)`,
    which returns `TenorDates` and handles `ON`, `TN`, `SN` and `<n>D/W/M/Y`;
  - `ois_quotes`, which turns `(tenor, rate)` pairs into sorted `OisQuote`s with
    resolved maturities.

  The backend can therefore pass the hub's tenor codes and a calendar instead of
  hand-made maturities.
- KEYRATE swap dates now come from `resolve_tenor`. A test checks that they are
  identical to the previous private helper on 40 trade dates × 11 tenors. Overnight codes
  stay rejected there.
- Not in this release: the exchange-conformant `rub_ruonia_ois_moex_v2` spec and spot /
  payment lags in the OIS bootstrap. The MOEX and NFA specification pages are
  unreachable from the build environment, and the audit summary leaves the fixed-leg
  frequency and float compounding open, so nothing was built on guessed conventions
  (BACKLOG #37).

## 0.15.0 — golden tests against MOEX-published bond analytics

Roadmap item 0.10. Tests and evidence only; no library code changes.

- `tests/fixtures/moex_bonds/` holds MOEX ISS bond data for 16 October 2026
  (settlement 19 October), copied from the hub at `c7a9d44` with its provenance.
- `tests/test_moex_goldens.py` builds instruments from the exchange's bondization data,
  as a backend adapter would, and checks:
  - **Accrued interest:** `ACCRUEDINT` to the kopeck for 7 bonds. They include an OFZ-PD,
    corporates with and without a maturity, an amortizer on its amortised face, a fixed
    bond, an OFZ-PK with a known current coupon, and a CNY bond in its own currency.
  - **OFZ 26238 yield:** the effective yield at `WAPRICE`, `LAST` and `PREVWAPRICE`
    (previous settlement), each within 0.5 bp of MOEX's 0.01 % rounding.
  - **OFZ 26238 duration:** `DURATION` within one day.
  - **Put bond:** duration to the offer within one day.
  - **End to end:** `value_portfolio` on the KBD curve of the same morning reproduces the
    OFZ 26238 accrued, yield and duration.
- Two known gaps are recorded by tests rather than hidden (BACKLOG #36):
  - 30/360 accrual on a USD bond (MOEX 2738.89, core 2740.44);
  - yield to offer on a short put bond (MOEX 9.51 %, core about 9.3–9.4 %).
- Roadmap status records the Windows CI benchmark for 0.13.0 (2k positions):
  - OIS with Hessian 11.8 ms per position cold and 9.9 ms seeded;
  - KBD with Hessian 11.0 ms.

## 0.14.0 — horizon valuation: carry, rolldown and P&L explain

Roadmap item 0.8. New API only; nothing existing changes.

- `horizon_valuation` carries a fixed-coupon bond from the curve date to a horizon
  business date with the Z-spread held. The spread is given or calibrated to the start
  dirty price. The result separates:
  - **carry**: today's forwards are realised;
  - **rolldown**: the curve keeps its shape in calendar days (`roll_curve`);
  - coupons and principal received in `(S0, S1]` (amortization included);
  - accrued interest and outstanding nominal at both settlements.

  With `redemption_horizon="confirmed_put"` a put bond is valued to the offer
  (yield-to-offer).
- `explain_horizon_pnl` splits the full revaluation change to an end curve and spread
  into carry, rolldown, rates, spread and a cross-term residual. The parts sum exactly to
  the total.
- `horizon_portfolio` applies this to a book. Positions resolve as in `value_portfolio`
  (same checks, curve, calendar, quote, accrued and rejection codes). Results are signed
  RUB per position; totals are given only when coverage is complete.
- Tests cover:
  - analytical carry of a zero-coupon bond on a flat curve, and no rolldown there;
  - a coupon and an amortization inside the horizon;
  - positive rolldown on an upward-sloping curve;
  - the put horizon and calibration from dirty price;
  - exact reconciliation and a second-order residual;
  - portfolio resolution identical to `value_portfolio`.

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
