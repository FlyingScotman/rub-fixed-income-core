# Repo and reverse-repo legs (package 6)

`rub_fixed_income.repo` values outstanding RUB repo legs: fixed or floating (RUSFAR,
RUONIA, the key rate), simple or capitalised, term or open, with the collateral
linked as a reference. It is **instrument valuation only**. The framework matches
`SwapRiskEngine`: explicit conventions, known fixings frozen under every shock, the
11-bucket grid, a shared `ShockedCurveCache`, exact scenarios, a central Hessian and
content identities. No I/O: the backend supplies the trade, the settlement calendar,
the curves and a point-in-time fixing snapshot.

**Boundary (roadmap, package 6).** The desk's funding economics (funding base,
calendar-day rates, OCP transfers) stay in the backend's `desk_rules`. Core
values the repo contract the desk has signed; it does not compute the desk's internal
funding charge of a bond position. See [F13](#relation-to-project_financed_bond-f13).

Sources (URLs, dates, SHA-256): [sources/repo_moex](sources/repo_moex/README.md).

## Terms

`RepoTrade(trade_id, side, start_date, end_date, start_amount_rub, rate, amount_rounding_decimals, collateral=None, cash_adjustments=(), currency="RUB")`

- `side`: `reverse_repo` lends cash (pays `S1` on the start date, receives the
  repurchase amount `S2` on the end date); `repo` borrows it. Every signed amount is
  `+1` / `-1` times the lender's.
- `start_date`: settlement of the first leg; `end_date`: of the second leg, or `None`
  for an open repo. Both must be settlement days of the request's calendar.
- `start_amount_rub` is `S1` ("Сумма РЕПО"). `repo_start_amount(quantity, dirty_price,
  discount_pct)` computes it from the collateral (MOEX Part III 1.5.8.2:
  `S = (1 - D / 100) * Q * (P0 + a0)`, to the kopeck); `repo_initial_discount_pct`
  inverts it.
- `amount_rounding_decimals` rounds amounts fully fixed by the terms and fixings
  (MOEX: 2). It is a required field. Projected amounts are never rounded.
- `collateral`: `RepoCollateral(instrument_id, quantity, initial_discount_pct=None, pool=None)`,
  the bonds (or GC certificates) currently delivered, after any securities margin.
- `cash_adjustments`: `RepoCashAdjustment(payment_date, amount_rub, kind)`, signed for
  the cash lender (+ received). `kind` is `margin_call`, `income_compensation` (a coupon
  passed back to the seller, NFA 10.1) or `repurchase_adjustment` (an agreed change of
  `S2`, NFA 10.2). Core never derives a margin call.
- Any currency other than RUB raises `UnsupportedProduct`.

### Rates

`FixedRepoRate(rate, day_basis, interest="simple")`

`FloatingRepoRate(benchmark, spread, day_basis, observation, projection_day_count, interest="simple", nonpositive_rate_replacement=None)`

| Field | Values | Source |
| --- | --- | --- |
| `day_basis` (`RepoDayBasis`) | `ACT/365-366` (each day of `(start, end]` over the length of its calendar year), `ACT/365F`, `ACT/360` | MOEX Part III 1.5.8.5 `T365/365 + T366/366`; NFA «База начисления процентов» (RUB: 365 or 366; foreign currency: 360) |
| `benchmark` | `RUSFAR`, `RUONIA`, `KEY_RATE` | MOEX floating-rate instruments, slide 5 |
| `observation` | `previous_settlement_day` (RUSFAR, RUONIA), `calendar_day` (key rate) | «за предыдущий Расчетный день для RUSFAR и RUONIA, а для Ключевой ставки БР за текущий»; the EQM06 example applies the key rate of each calendar day |
| `interest` | `simple`: `S2 = S1 (1 + sum_i (r_i + s) w_i)`; `capitalised`: interest credited on each settlement day | MOEX slide 6; the 2026 compounding presentation (NCC Clearing Rules II 21.12.2) |
| `spread` | decimal, added simply to the benchmark | «спред + индикатор» |
| `nonpositive_rate_replacement` | `None`, or the rate used when `benchmark + spread <= 0` (MOEX KSU repo: `0.0001`) | «ставка РЕПО с КСУ принимается равной 0.01%. Для РЕПО с ЦК и РЕПО-М таких ограничений нет» |
| `projection_day_count` | the rate basis of the projection curve's overnight forwards | core (curve convention) |

Only the confirmed benchmark/observation pairs are accepted; anything else is
`UnsupportedProduct`, as are capitalised key-rate repos (FKPO applies the closing
settlement day's key rate to a whole period; not implemented) and term indicators
(RUSFAR 1W…3M reset once per indicator term).

## Interest

Interest accrues by calendar day `i` of `(start, end]`: the NFA «Срок РЕПО» runs from
the day after the first leg to the second leg inclusive, which is also the MOEX sum
`i = T1 + 1 … T2`. Each day weighs `w_i` from the day basis.

**Benchmark of a day.**

- `previous_settlement_day`: `s` = the latest settlement day before day `i`; the
  observation is the latest fixing date on or before `s` on the history's publication
  calendar («если … расчет значения индикатора не производится, его значение
  принимается равным последнему известному значению»). A Friday fixing covers Saturday,
  Sunday and Monday.
- `calendar_day`: the key rate in force on day `i` (rows by CBR effective date).
- Observations dated before the valuation date come from the `FixingHistory` (its gap
  and carry rules; a missing RUSFAR or RUONIA day raises `MissingMarketData`, never
  filled). Later ones are the projection curve's overnight forwards
  `(DF(o) / DF(next) - 1) / tau(o, next)`, `next` the next publication day (key rate:
  the next calendar day), `tau` on `projection_day_count`, plus the spread.

**Simple** (`S2 = S1 * (1 + sum_i (r_i + spread) * w_i)`): one rounding of `S2` when
every rate is known (the EQM06 total 6 468 388,60 is the exact sum rounded once, not
the sum of the rounded lines). A fixed rate gives the MOEX
`S2 = S1 (1 + R (T365 / 365 + T366 / 366))` (`repo_end_amount`).

**Capitalised**: on each settlement day `d_k`,
`I_k = B_{k-1} * sum_{i in (d_{k-1}, d_k]} (r_i + spread) * w_i`, rounded to the
kopeck when known, `B_k = B_{k-1} + I_k`; non-settlement days accrue simply into the
next credit (3 days on Monday).

**Accrued** (`accrued_interest_rub`): the interest of the days up to the valuation date
inclusive (a capitalised open period accrues simply on the last balance);
`current_repurchase_amount_rub = S1 + accrued` is MOEX's «сумма к исполнению»,
rounded when its rates are all known. `known_interest_rub` is the part of `S2` from
fixed rates and known fixings (MOEX «известная часть»).

## Market and valuation date

`RepoRiskRequest(trade, calendar, discount_curve, projection_curve=None, fixings=None, assumed_end_date=None, collateral_dirty_price_rub_per_bond=None)`

- The **valuation date is the discount curve's date**; the projection curve and the
  fixing history must be dated the same day. Flows on or before it have settled and
  are excluded; a repo whose end is on or before it is rejected (`InvalidTerms`).
- `calendar`: settlement days («Расчетные дни»), on which capitalised interest is
  credited and the legs settle.
- `discount_curve` / `projection_curve`: a `DiscountCurve` (`continuous_zero` risk) or
  a `CurveBootstrapRequest` (`ois_quote`: shift the quotes, rebootstrap), as for
  swaps. A fixed repo takes no projection curve; a floating repo without one projects
  on the discount curve (single curve). A key-rate repo projects on
  `key_rate_projection_curve(forward, calendar, rate_day_count=..., observation_days="calendar")`.
- `fixings`: required for a floating repo (it may be empty). **RUSFAR and RUONIA use
  `FixingHistory(index="ruonia", …)`**: `FixingHistory` has no `rusfar` index, and its
  `ruonia` rules (a fixing on every business day of the publication calendar, trailing
  unpublished days carried only within the explicit allowance) are the
  daily-publication rules RUSFAR follows. Its calendar must be the RUSFAR calculation
  calendar: no RUSFAR on non-trading days, trading Saturdays and Sundays, or the last
  trading day of the year (methodology, section 3 «Расписание расчета»). The snapshot's `fixing_id` names the
  benchmark. The key rate uses `index="key_rate"`.
- `assumed_end_date`: required for an open repo, rejected for a term one, part of the
  identity. MOEX open repo: settlement code `YOD`, at most 360 days, a closing order
  executes on the next settlement day.
- `collateral_dirty_price_rub_per_bond`: only for the collateral reference.

## Results

`RepoRiskEngine(request, curve_cache=ShockedCurveCache())`

`engine.valuation() -> RepoValuation`

| Field | Meaning |
| --- | --- |
| `pv_rub`, `start_leg_pv_rub`, `end_leg_pv_rub`, `adjustments_pv_rub` | Signed PV of the remaining flows on the discount curve: `sign * sum(a_j * DF(t_j))` |
| `start_amount_rub`, `end_amount_rub`, `end_amount_source` | `S1`, the expected `S2`; `fixed`, `known`, `partially_projected` or `projected` |
| `interest_rub`, `known_interest_rub` | `S2 - S1` and its fixed or known part |
| `accrued_interest_rub`, `current_repurchase_amount_rub` | Interest to the valuation date and `S1` plus it |
| `repurchase_price_rub_per_bond` | `S2 / quantity` (dirty, per collateral bond) |
| `cashflows` | `RepoCashflow(kind, payment_date, amount_rub, source, discount_factor, pv_rub)` |
| `collateral` | `RepoCollateralReference` (below) |

`engine.risk(bump_bp=1, difference="central") -> RepoRiskResult`: per curve role
(`discount`; `discount_and_projection` for a single-curve floating repo; `discount`
and `projection` for two curves) 11 `BucketRisk` rows, their sum and a repriced
parallel DV01; `krd_years = -dv01 / S1 * 1e4`. Signs: a cash lender's discount DV01
is negative; its projection DV01 is positive (more interest received when rates
rise). Known fixings carry no projection risk.

`engine.reprice(RepoScenario(discount_shifts_bp, projection_shifts_bp))`: exact, with
the discount-only, projection-only and interaction P&L. `engine.second_order()`: the
central gradient and full Hessian (11 or 22 factors; mixed points combine memoized
single-curve vectors). `repo_factor_risk(risk, discount_curve_id=..., projection_curve_id=None, second_order=None)`
maps them to `IR|<discount_curve>|<bucket>` and `IR|<projection_curve>|<bucket>`
(central only, DEC08).

`engine.carry(horizon_date)` / `repo_carry(request, horizon_date=...) -> RepoCarry`:

- `interest_carry_rub`: repo interest accrued over `(valuation, horizon]`, up to the
  end, on known fixings and today's projections: the accrual P&L of the leg;
- `received_rub`, `pv_forward_rub` (remaining flows on today's curve read forward to
  the horizon) and `pv_carry_rub = pv_forward + received - pv_start`.

There is no rolldown term: a repo leg has at most two flows and its projection is
overnight. Model versions: `rub-repo-leg-v1`, `rub-repo-leg-second-order-v1`.

**Identity.** `base_id` hashes the model, the trade, the calendar version and
fingerprint, the assumed end, both curves' bases and content, the fixing history's
identity and the collateral price.

## Collateral and booking (no double counting)

The repo leg values **cash only**. `RepoCollateralReference` reports the collateral
next to it: instrument, quantity, pool, initial discount, the given dirty price,
`market_value_rub = quantity * price`, `exposure_rub = current repurchase amount -
market value` (the lender's unsecured amount; positive when under-collateralised) and
`coverage_ratio`. Nothing in it enters `pv_rub` or the risk.

How the backend books a repo (the legal title passes, the economics do not):

| Party | Bond position | Repo position |
| --- | --- | --- |
| Cash borrower (`repo`, sells on the first leg) | keeps the bond in its book, valued and risked by the bond engines (`value_portfolio`) as if never sold; coupons paid during the repo come back as `income_compensation` or reduce `S2` | the repo leg: a liability, `pv_rub < 0`, discount DV01 > 0 |
| Cash lender (`reverse_repo`) | **no** bond position: the collateral is only the reference | the repo leg: a receivable, `pv_rub > 0` |

Booking the received collateral as a bond position of the lender, or the delivered
bond as a short of the borrower, would count the bond twice (once as a bond, once
through the repurchase amount). GC repo with the CCP (KSU) delivers certificates of a
pool (`GC Bonds` `RU000A0JW4Z1`, `GC Shares` `RU000A0JWKG5`, `GC Expanded`
`RU000A0ZYP02` on ISS); the certificate is priced at its nominal for repo sums, and the
underlying bonds stay in the participant's pool.

## MOEX repo with the CCP

From ISS (`/iss/engines/stock/markets/gcc/boards`, `…/ccp/boards`) and the sources:

- **Fixed KSU boards:** `GCRP` (1 day), `GCOW`, `GCSW`, `GCOM`, `GCSM`, `GCTM`, `GCUM`,
  `GCNM`, `GCOY` (7 days … 1 year), the same in CNY/USD/EUR (`GY…`, `GU…`, `GE…`),
  addressed `PSGC`. Repo with the CCP without KSU: `EQRP`, `EQWP`, `EQMP`, `EQTP`,
  addressed `PSRP`.
- **Floating KSU boards (RUSFAR + spread):** `FCOW`, `FCSW`, `FCOM`, `FCSM`, `FCTM`,
  `FCUM`, `FCNM`, `FCOY` (term), `FRPO` (open, compounding); key rate: `FKPO` (open,
  compounding), `FAOM`, `FATM` (1 and 3 months); CNY: `FY…` on RUSFARCNY.
- **Settlement codes:** RUSFAR is computed from KSU-bond repo `Y0/Y1` (first leg today,
  second tomorrow) up to 12:30; open repo uses `YOD`. Core takes the resulting
  dates, not the codes.
- **Benchmarks:** RUSFAR (ON) and its term rates (1W, 2W, 1M, 3M), RUSFARCNY; the
  REAL TIME and REAL TIME COMPOUND series are intraday averages of the same order book
  and are not repo benchmarks.

## Relation to `project_financed_bond` (F13)

`project_financed_bond` (unchanged) compounds a dirty balance daily,
`B * (1 + r / 365)^days`, between cash events. It is neither:

- the **desk's funding engine** (simple interest at calendar-day rates on a configured
  base; `desk_rules` in the backend, local audit F13); nor
- a **repo contract**: simple interest on the start amount (term repo, MOEX 1.5.8.5)
  or interest credited on settlement days with simple accrual between them
  (compounding open repo).

For 100 mln at 16 % over 31 days the daily compounding gives about 8 970 RUB more
than the repo's simple interest (`tests/test_repo.py`). The backend must not use it
to value a repo or to charge desk funding; core keeps it for compatibility and
documents the difference instead of changing its numbers.

## Validation

- **MOEX goldens** (`tests/test_moex_repo_goldens.py`): the compounding RUSFAR open
  repo of slide 17 on the ISS RUSFAR history (credits 3 884,93 / 3 905,63 / 11 885,96,
  `S2` 10 019 676,52 to the kopeck); slide 4's balances on its stated rates; slide 10's
  «сумма к исполнению» 30 011 580,82; the EQM06 key-rate repo: the known parts and
  `S2` on report days 1, 2, 3, 6, 7 and 8 within half a kopeck (projected parts are
  unrounded in core).
- **Reconstructions** (`tests/test_repo.py`): the leap-year day basis against
  ACT/ACT ISDA; fixed, forward-start and adjusted PVs; simple floating single- and
  two-curve `S2` from the MOEX formula written in the test; capitalised credits;
  the nonpositive-rate rule.
- **Risk:** the fixed DV01 equals `-t * PV * 1e-4` on a flat curve, signs by side,
  bucket sums, projection DV01 of the projected days only, bump-size convergence,
  gradient = risk, a Taylor expansion against exact repricing, factor keys.
- **Carry, collateral, F13, identities, serialization and typed rejections.**

## Performance (2k positions)

`benchmarks/repo_bench.py --positions 2000` builds 1000 fixed repos (17-tenor OIS
quotes, quote basis, 11 factors) and 1000 RUSFAR repos (simple and capitalised, a
third open; OIS quotes for discounting and a RUSFAR projection curve, 22 factors),
each set sharing one `ShockedCurveCache`. Linux x86_64, Python 3.11, ms per position:

| Product | First order | + Hessian (cold cache) | + Hessian (warm cache) |
| --- | --- | --- | --- |
| Fixed, 11 factors | 0.85 | 4.9 | 1.8 |
| Floating, 22 factors | 2.2 | 14.1 | 10.7 |

A cold run pays the OIS rebootstraps of the job once; the warm run is what a backend
sees when it seeds the cache with persisted shocked curves. The floating Hessian is
dominated by the 22 x 22 central scheme over the projected days (overnight forwards
are memoised on the shared shocked curves, as in `floating`); a fixed or fully known
repo reads two discount factors per shock.

## Portfolio integration

`value_portfolio` takes repo legs as `RepoInstrument` positions (result contract v3,
[portfolio-v3.md](portfolio-v3.md)): an open repo needs `assumed_end_date` on the
position type, else the position is rejected `MISSING_ASSUMPTION`; an ended repo is
`SETTLED_LEG`. The collateral reference is reported with the row and never enters PV or
risk.

## Not covered

- Term indicators (RUSFAR 1W…3M), RUSFARCNY and foreign-currency repos; the
  capitalised key-rate repo (FKPO).
- Margin calls and income transfers derived from the NCC Clearing Rules (not
  reachable): they are explicit `RepoCashAdjustment` inputs.
- The option value of the nonpositive-rate replacement (a projected rate at or below
  zero is rejected), early termination and open-repo end optionality (the end is an
  explicit assumption).
- Desk funding economics (`desk_rules`), OCP transfers, collateral valuation.
