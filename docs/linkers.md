# Inflation-linked bonds: OFZ-IN (package 9a)

OFZ-IN (`SU52…`) pay a fixed real coupon on a face indexed to the Russian CPI. Core models
them as an `Instrument` with two new leg types, reads the CPI from a point-in-time series
in the snapshot, and values them on a **real** discount curve with a new desk factor type,
`REAL|<curve_id>|<bucket>`. Modules: `rub_fixed_income/inflation.py` (CPI series, the
indexation rule, the legs, formulas 4–5) and `rub_fixed_income/linkers.py` (face, accrued,
coupons and real flows of an indexed instrument). Sources: [sources/ofz_in](sources/ofz_in/README.md).
Evidence: `tests/test_ofz_in_goldens.py`, fixtures in `tests/fixtures/ofz_in/`.

## The rule (Minfin terms, order No. 80n of 22.05.2015)

The terms text was not reachable. The formulas below are the Bank of Russia's
restatement of it (methodological comment, January 2021, section 3, which names the
order), and the data confirm each of them:

| Quantity | Formula | Confirmed by |
|---|---|---|
| Reference index on day `n` of month `M` (`d` days) | `INDEX = CPI(M-4) + (CPI(M-3) - CPI(M-4)) * (n-1)/d` | the monthly growth of MOEX `FACEVALUE` is the CBR m/m CPI of month M-3 (lags 2 and 4 are off by up to 6 pp); the face moves linearly by day inside every month |
| Index ratio | `I = INDEX(date) / INDEX(placement date)` | the face is 1000 at placement; 5,927 faces within 2.6e-4 from CBR CPI |
| Indexed face | `N = 1000 * I`, published in kopecks | MOEX and Minfin publish kopecks; half-up rounding is an assumption (terms not read) |
| Coupon | `round(N(payment date) * 2.5 % * days / 365, 0.01)` | all 45 coupons paid by the four bonds, to the kopeck |
| Accrued | `round(N(date) * 2.5 % * (date - period start) / 365, 0.01)` | all 5,927 MOEX `ACCINT`, to the kopeck |
| Redemption | `max(N(maturity), 1000)` | MOEX OFZ-IN page ("minimum nominal at redemption 1000"); no bond has redeemed under deflation |

`CPI` is Rosstat's monthly index level; only ratios matter, so any fixed base works.

## API

| Name | Description |
| --- | --- |
| `CpiObservation(month, value, published_on)` | One monthly level (`month` is the 1st). It cannot be published before the month ends. |
| `CpiSeries(series_id, source_id, as_of, observations)` | Consecutive months. Every observation must be published on or before `as_of`, so there is no look-ahead. `.level(month)` raises `MissingMarketData` for an unpublished month, and the gap is never filled. `.known_on(date)` gives an earlier cut, `.content_sha256` the cached hash. |
| `CpiIndexation(series_id, base_date, lag_months, base_index=None, interpolation="daily_linear", nominal_quantum=Decimal("0.01"))` | The rule; `lag_months=3` for OFZ-IN. `base_index` is the issue's published `INDEX_base`; `None` computes it from the series on `base_date`. Methods: `.reference_months(d)`, `.reference_index(series, d)`, `.index_ratio(...)`, `.nominal(face, series, d)`, `.identifier`. |
| `IndexedCouponPeriod(accrual_start, accrual_end, payment_date, rate)`, `IndexedCouponLeg(periods, day_count)` | Real-rate coupons on the indexed face, with `day_count` `ACT_365_FIXED` (OFZ-IN) or `ACT_360`. |
| `IndexedPrincipalLeg(maturity_date, indexation, floor_at_initial_face)` | Bullet redemption of the indexed face, floored at the initial face when the terms say so. |
| `indexed_coupon_amount(nominal, period, day_count)`, `indexed_accrued(nominal, period, date, day_count)` | Formulas (4) and (5) for a given face. |
| `linker_nominal(instrument, series, date)`, `linker_accrued(...)`, `linker_coupon(instrument, series, period)`, `linker_redemption(instrument, nominal)` | Face, accrued and coupons of an indexed `Instrument` from CPI. A coupon whose CPI months are not yet published raises `MissingMarketData`. |
| `real_cashflows(instrument, series, settlement)` | Flows after settlement at the settlement-date face, held constant (see below). |
| `MarketSnapshot(..., cpi=(CpiSeries, ...))`, `.cpi_series(id)` | Omitted from canonical JSON when empty, so existing snapshots keep their hash. A series cut after the business date is rejected. |
| `real_factor(curve_id, bucket)`, `linker_factor_risk(risk, real_curve_id, second_order=None)` | The new factor keys and the mapping of a linker's risk. |
| `CpiUse(series_id, source_id, as_of, first_month, last_month)`, `PortfolioRiskResult.cpi` | The CPI series used by a valuation. Omitted when empty. |
| `rub_real_discount_factors_v1` (`CurveKind.REAL_DISCOUNT_FACTORS`) | A caller-built RUB real discount curve. |

`Instrument` takes `IndexedCouponLeg` and `IndexedPrincipalLeg` only with
`ProductType.INFLATION_LINKED_BOND` (and the product needs them). Offers on an indexed
face are rejected. Its market-independent `cashflows()`, `accrued()` and
`outstanding_face()` raise `InvalidTerms` for an indexed bond: use `linkers`. Existing
instruments serialize and hash as before (new union members, no new fields).

## Valuation: real flows on a real curve

`value_portfolio` sends an `INFLATION_LINKED_BOND` to a separate path:

1. **Settlement** is the instrument's lag on its calendar. The face `N(S)` and the accrued
   come from the snapshot's CPI series. A quote's `accrued_per_bond` wins, as for other
   bonds. The quote is % of the indexed face.
2. **Real flows.** Each future coupon is computed by formula (4) on `N(S)`, and the
   redemption is `max(N(S), initial face)`. These are the flows in settlement-date money.
   Their effective annual ACT/365F yield is the **real yield**. It matches MOEX
   `YIELDCLOSE` for all four OFZ-IN within 0.5 bp of its 0.01 % precision, and the
   Macaulay duration matches `DURATION` within 0.2 days.
3. **Discounting** is on the real curve that `ValuationPolicy.curve_overrides` names for
   the bond. It must have spec kind `real_discount_factors`; anything else is
   `INVALID_TERMS`, and a nominal bond on a real curve is rejected too. The spread (`CS`)
   is calibrated over it to the dirty price.
4. **Risk:** `REAL|<curve>|<bucket>` (11 buckets, continuous-zero shifts of the real curve)
   and `CS`. It is central, with the Hessian from the same bump (DEC08). JTD is on `N(S)`.
   There is no G-spread, because the KBD is nominal.

Not projected: future indexation (the real curve carries it), the convexity of the
indexation lag, and the time value of the deflation floor (applied at intrinsic value
only). The part of the next months' indexation that is already published is treated like
the rest. The real curve's builder is responsible for consistency with it.

## Contract change and coverage

- **New factor type** `REAL|<curve_id>|<bucket>`. It appears in `factor_risk` and
  `factor_totals` only when a linker is computed. A book without linkers has exactly the
  factors, bytes and hashes it had before (the pinned `tests/fixtures/portfolio_result_v2.json`
  is unchanged). Desk aggregation must treat `REAL|…` as its own risk class, not as `IR|…`.
- **Scenarios:** `PortfolioScenario` shifts are nominal-curve shocks, so linker rows are
  `UNSUPPORTED_PRODUCT` (P&L `None`). Scenario totals are then `None`, and the job status
  is `partial`. A real-curve scenario vector is not defined yet.
- **Rejections:** no CPI series, or an unpublished CPI month →
  `MISSING_MARKET_DATA`; non-RUB linker, a non-annual `yield_convention` or a horizon run
  (`horizon_portfolio`) → `UNSUPPORTED_PRODUCT`; a callable on an indexed face →
  `UNSUPPORTED_PRODUCT` (in `callables`).

## Indexed corporates RU000A1062M5, RU000A108RQ7 (not covered)

Both are Seligdar gold-linked bonds (GOLD01, GOLD03), not CPI-linked. Their face
(`FACEVALUE` 11 012.12 on 2026-10-17) equals the Bank of Russia's accounting price of one
gram of gold of a slightly earlier date (`https://www.cbr.ru/hd_base/metall/metall_base_new/`).
The offset is not stable in the data: the 2026-10-16 EOD face is the CBR price of 13.10
(three business days earlier), while the face for settlement on 19.10 is the price of
16.10 (one business day earlier). Coupons are 5.5 % on the face. The issue terms on
`e-disclosure.ru` are refused by the proxy, so the formula is not unambiguous and is not
modelled. `CpiIndexation` does not represent a daily commodity index.

## Performance (2k positions)

`uv run python benchmarks/linker_bench.py --positions 2000`, on 2k distinct OFZ-IN-like bonds
(1–15 years, semi-annual), one real curve and one CPI series:

| Python | first order | with Hessian |
|---|---|---|
| 3.13 (Linux) | 2.7 ms/position | 11.6 ms/position |
| 3.11 (Linux) | 3.2 ms/position | 11.8 ms/position |

On the same machine, the nominal KBD book of `portfolio_bench.py` costs 3.1 / 8.2 ms. The
Hessian gap comes from the linker book's longer semi-annual schedules: the 12×12 central
Hessian reprices each bond about 290 times over all its flows. In a profile, the
indexation (face, accrued, real flows) is a few percent of the time.

## Gaps

- The terms text and Rosstat's CPI levels are unreachable from this environment (see the
  sources README). The face from the reachable CBR CPI is within 2.6e-4 (0.36 RUB) of
  MOEX, not to the kopeck. Kopeck agreement with the official levels is expected but not
  verified.
- The nominal is rounded half up to kopecks, which is not confirmed from the text. The
  rounding of `INDEX_base` is unknown; pass the published base when the hub has it.
- MOEX `IRICPICLOSE` and `BEICLOSE` (inflation figures) are not identified.
- There is no breakeven-inflation sensitivity and no real-curve builder, and no carry,
  horizon or scenario support for linkers yet.
