# MOEX rate futures: RUONIA, KEYRATE and OFZ basket futures (package 5)

Two modules price exchange interest-rate futures from point-in-time inputs and give
their risk in the desk factor vocabulary:

- `rub_fixed_income.rate_futures`: one-month RUONIA (RUON), RUONIA index and KEYRATE
  index futures;
- `rub_fixed_income.ofz_futures`: deliverable OFZ basket futures (OFZ2, OFZ4, OFZ6, OF10,
  OF15).

Both follow `fx_futures`: variation margin is daily and **not discounted**, a calibrated
basis is frozen under every shock, the 11 buckets and shock operations are those of
[risk.md](risk.md), scenarios reprice exactly, and the Hessian is a full central
difference with the same bump (DEC08). No I/O: contract dates, fixings, curves and
prices are inputs.

## Contract terms and sources

Every term below is from a MOEX specification (fetched 2026-10-17); nothing is assumed.

| Contract | Specification | Price | Final settlement | Step, step value |
|---|---|---|---|---|
| RUON (`RR*`) one-month RUONIA | "Фьючерсные контракты на процентные ставки", order MB-P-2026-843 of 10 March 2026, in force from 23 March 2026 ([document](https://www.moex.com/ru/documents/fyuchersnyj-kontrakt-na-protsentnye-stavki)); same terms in the RUONIA futures specification of 20 June 2022 ([3292](https://www.moex.com/ru/documents/3292)) | `100 - R`, `R` the expected mean RUONIA of the settlement month, % | `100 - sum(RUONIA_i)/T` over the `T` calendar days of the month; a day without a RUONIA takes the latest earlier one | 0.01; `W = Round(T/365 * 1,000,000 * 0.01%; 5)` |
| RUONIA index (`RF*`) | "Фьючерсные контракты на индексы долгового и денежного рынка", order MB-P-2026-3630 of 8 September 2026, in force from 14 September 2026 ([document](https://www.moex.com/ru/documents/fyuchersnye-kontrakty-na-indeksy-dolgovogo-i-denezhnogo-rynka)) | index points, 4 decimals | the Bank of Russia RUONIA index published on the last trading day (first trading day of March, June, September, December), rounded half up to 4 decimals | 0.0001, 1 RUB |
| KEYRATE (`KK*`) | same specification | index points | the MOEX KEYRATE index on the last trading day, a scheduled Bank of Russia meeting | 0.01, 10 RUB |
| OFZ2 / OFZ4 / OFZ6 / OF10 / OF15 | "Фьючерсные контракты на корзину облигаций федерального займа", 24 April 2015, in force 15 November 2015 to 27 February 2023 ([6381](https://www.moex.com/ru/documents/6381)) | RUB per lot of 10 bonds, clean | delivery of one basket issue at `F / 10 * CF_i` per bond plus accrued interest | 1 RUB, 1 RUB |

- **RUON settlement month**: from the last trading day of the previous month
  (included) to the last trading day of the expiry month (excluded), which is the last
  trading day. `ruonia_average_settlement_period(year, month, calendar)` derives it.
  Variation margin: `Round(P_t * Round(W/R; 5); 2) - Round(P_{t-1} * Round(W/R; 5); 2)`
  (`rate_futures_variation_margin`). The specification states no rounding of the final
  price; ISS final prices are rounded half up to the 0.01 step, and core does that.
- **RUONIA index** (Bank of Russia methodology of 12 April 2022, order OD-731):
  `I_t = I_T * (1 + RUONIA_T * (d/366 + (t - T - d)/365))`, `T` the previous day with a
  RUONIA, `d` the leap-year days of `[T, t)` (`cbr_ruonia_year_fraction`). Days without
  RUONIA accrue by simple interest from the previous one.
- **KEYRATE index** (MOEX methodology approved 13 August 2026): `I = R * 100`, rounded to
  0.01, `R` the key rate set at the last meeting before the day; on a scheduled meeting
  date the rate set for the first business day after it (`key_rate_index_value`). So a
  KEYRATE future settles on the decision of its meeting.
- **OFZ conversion factor** (Appendix 4):
  `CF_i = [sum_k C_k/(1+r)^t_k + N_i/(1+r)^T_i - AI_i] / N_i` at the delivery date, 4
  decimals; `r` "set by decision of the exchange" per series (an input),
  `t_k` in years: ACT/365F (`ofz_conversion_factor`). Default delivery issue (item 4.4):
  lowest closing price on the day before the last trading day divided by CF
  (`ofz_default_delivery_issue`).

MOEX has listed no OFZ basket futures since the June 2022 expiry. RGBI index futures are
cash-settled on a bond index and are a different product (not modelled).

## Short-rate futures: `RateFuturesRiskEngine`

```python
RateFuturesRiskRequest(contract, projection_curve, fixings=None, index_anchor=None,
                       futures_price=None, basis_bp=None, quantity=1.0)
```

- `contract`: `RuoniaAverageFuturesContract(contract_id, period_start, period_end)`,
  `RuoniaIndexFuturesContract(contract_id, last_trading_day)` or
  `KeyRateFuturesContract(contract_id, meeting_date, effective_date)`.
- RUONIA contracts: `projection_curve` a `DiscountCurve` or an OIS `CurveBootstrapRequest`
  dated the valuation date, and `fixings` a `RuoniaFixings(valuation_date, fixings,
  calendar, snapshot_id, unpublished_dates=frozenset())`. Fixings are decimal, keyed by
  the business day they were calculated for, all before the valuation date. **Every
  business day before the valuation date needs a fixing** unless it is listed in
  `unpublished_dates` (the Bank of Russia publishes no RUONIA for the last business day
  of a year, e.g. 2025-12-30); a listed day carries the previous fixing, an unlisted gap
  raises `MissingFixing` (a `MissingMarketData`). Nothing is filled silently.
- Index contract: also `index_anchor = RuoniaIndexValue(index_date, value)`, a published
  index on a RUONIA calculation day on or before the valuation date.
- Key-rate contract: a `KeyRateForwardCurve`; the price is `100 * rate_at(effective_date)`.
- Exactly one of `futures_price` (a settlement price; calibrates the basis) and `basis_bp`.
  A fully fixed contract (all its days observed) does not depend on a basis: pass
  `basis_bp=0`.

**Projection.** A RUONIA day `T` after the last fixing takes the curve's overnight
forward `(DF(T)/DF(T_next) - 1) / tau(T, T_next) + basis`, `T_next` the next calculation
day and `tau` the RUONIA basis above. For the index this telescopes: the projected growth
is exactly `DF(first)/DF(last)` at zero basis. Rates are deterministic, so there is no
futures convexity adjustment.

**Outputs.** `engine.risk(bump_bp=1, difference="forward"|"central")` gives
`RateFuturesRiskResult`: futures and theoretical price, basis, expected rate, fixed and
projected days, RUB of margin per price point, 11 `BucketRisk`s, their sum, a parallel
DV01 repriced independently and basis01. Money is signed RUB of variation margin:
`quantity * margin_per_point * dF`. A long RUON loses when rates rise (negative DV01); a
long RUONIA index or KEYRATE future gains. `engine.reprice(RateFuturesScenario(...))` is
exact; `engine.second_order()` gives the 12-factor Hessian (11 buckets + basis).
`engine.for_quantity(q)` derives another position on the same contract and market and
shares every repricing.

Key-rate curve shocks follow `swaps`: the bucket hats at the effective date's ACT/ACT
ISDA time from the curve date; a rate already decided (before `current_rate_end`) does
not move.

## OFZ basket futures: `OfzFuturesRiskEngine`

```python
OfzFuturesRiskRequest(contract, basket, spot_settlement_date, bond_curve, funding_curve,
                      futures_price=None, basis_bp=None, quantity=1.0)
```

- `contract = OfzFuturesContract(contract_id, last_trading_day, delivery_date, bonds_per_lot=10)`.
- `basket`: `DeliverableBond(instrument, conversion_factor, clean_price_pct)` per bond, the
  exchange's published CF and the spot clean price (% of face) for
  `spot_settlement_date` (T+1 on TQOB). Bonds must be RUB bullets with known fixed coupons
  (`FixedCouponLeg`) maturing after delivery.
- `bond_curve` prices each bond with a continuous spread (ACT/365F) calibrated to its
  dirty price and frozen; `funding_curve` carries it to delivery:
  `forward = dirty * G(s) - sum(coupons in (s, D] * G(c))`, `G(x) = DF_f(x)/DF_f(D) * exp(b * tau(x, D))`.
- Theoretical futures price: `min_i 10 * (forward_i - AI_i(D)) / CF_i`; the minimiser is
  the cheapest to deliver. At spot settlement on the delivery day this is
  `min_i 10 * clean_i / CF_i`.

`engine.basket()` gives per bond the forward, theoretical price, implied repo (simple
ACT/365F, coupons reinvested at it), gross basis (`clean - F/10 * CF`, RUB per bond) and
net basis (`forward clean - F/10 * CF`). `engine.risk()` gives bond-curve and
funding-curve buckets, CS01 (all basket spreads +1 bp), basis01, the cheapest bond by the
model and by implied repo. DV01 is the cheapest bond's: `10/CF_ctd` times its forward
dirty change. `reprice(OfzFuturesScenario(...))` is exact and reports the cheapest bond
after the shock (it can switch); `second_order()` is the 24-factor Hessian (11 + 11
buckets, spread, basis). At a switch of the cheapest bond inside the bump the price has a
kink and the Hessian is only local.

**Delivery options are not valued.** The seller chooses the issue (quality option) and
delivers over the delivery day's three periods; with deterministic rates and spreads the
cheapest bond is certain, so the model price is the price without option value (an upper
bound of the true futures price). No option model is stated or applied.

## Horizon (carry, rolldown, theta)

`engine.horizon(horizon_date)` (`rate_futures_horizon(request, *, horizon_date,
curve_cache=None)`) and `engine.horizon(horizon_date, *, horizon_spot_settlement_date)`
(`ofz_futures_horizon(...)`) carry a position to a later date on the two curve views of
[`horizon`](../rub_fixed_income/horizon.py), as for bonds and swaps: **forward** reads
today's curves forward (`forward_curve`, `DF(t)/DF(h)`: today's forwards realised) and
**rolled** keeps their shape in calendar days (`roll_curve`, `roll_key_rate_curve`; the
desk theta). Base curves only (an OIS request uses the engine's built curve); the
calibrated basis and bond spreads are held. `horizon_date` must follow the valuation date.

A futures position is margined daily, so nothing is invested or received:
`pv_start_rub = received_rub = 0`, and with `scale = quantity * margin_per_point`
`carry = scale * (price_forward - price_start)`, `rolldown = scale * (price_rolled -
price_forward)`, `horizon_pnl = scale * (price_rolled - price_start)`. Prices are model
prices, unrounded (no final-price rounding even for a contract settled by the horizon).

- **Short-rate futures** (`RateFuturesHorizonValuation`, `rub-rate-futures-horizon-v1`):
  a RUONIA business day `T < h` (an index span starting before `h`, a key rate effective
  before `h`) is the path to the horizon, realised at today's projection plus basis in
  both views; later rates are projected on the view plus basis. Known fixings stay. So
  `price_forward` equals today's price up to rounding (carry is zero: a future has no
  carry under today's forwards) and the rolldown is the whole theta: positive for a long
  RUON on an upward curve, negative for a long RUONIA index, positive for a long KEYRATE
  on an easing path. A key rate already decided (before `current_rate_end`) does not
  move. A contract whose last trading day is on or before `h` settles on the realised
  path: `price_forward == price_rolled`.
- **OFZ futures** (`OfzFuturesHorizonValuation`, `rub-ofz-futures-horizon-v1`): the
  caller gives the basket's spot settlement for the horizon `s_h` (`h <= s_h`, not before
  today's spot, **before delivery**: carry through delivery is not modelled, and no OFZ
  futures are listed since 2022). Each bond's dirty price at `s_h` is its flows after
  `s_h` on the bond-curve view anchored at `s_h` with its spread held; it is carried to
  delivery on the funding view with the basis held (coupons in `(s_h, D]` removed), and
  the price is `min_i 10 * (forward_i - AI_i(D)) / CF_i`. The carry is the spread of the
  bond yield over funding; the cheapest bond can switch, and `cheapest_start`,
  `cheapest_forward`, `cheapest_rolled` report it.

`for_quantity` engines carry the same prices at their own scale. Tests:
`tests/test_futures_horizon.py`.

## Desk factors

- `rate_futures_factor_risk(risk, curve_id, second_order=None)`: `IR|<curve_id>|<bucket>`
  and `FUTBASIS|<contract_id>`.
- `ofz_futures_factor_risk(risk, bond_curve_id, funding_curve_id, second_order=None)`:
  `IR|<bond curve>|*`, `IR|<funding curve>|*` (distinct IDs), `CS`, `FUTBASIS|<contract_id>`.

`FUTBASIS|<contract_id>` (1 bp of a calibrated futures basis) is a new factor key; the
portfolio result contract v3 carries it ([portfolio-v3.md](portfolio-v3.md)).

## Validation

- `tests/test_rate_futures.py`, `tests/test_ofz_futures.py`: analytic prices (flat curves,
  telescoping index growth, carry with a coupon, CF = 1 at par on a coupon date, implied
  repo = funding rate), signs, bucket sums vs parallel shifts, basis linearity, exact
  scenarios and interaction, CTD switch, Hessian symmetry and its gradient, quantity
  scaling with shared repricings, rejections.
- `tests/test_rate_futures_goldens.py`, `tests/test_ofz_futures_goldens.py`: golden values
  from ISS FORTS history, ISS news and the Bank of Russia
  (`tests/fixtures/moex_rate_futures/README.md`, `tests/fixtures/moex_ofz_futures/README.md`):

| Golden | Result |
|---|---|
| RUON final settlement, RRZ5..RRU6 (Dec 2025..Sep 2026) | 9 of 10 exact; RRK6 (May 2026, no open interest) 85.82 vs 85.79, unexplained |
| RUONIA index, every day 2026-05-04..2026-10-16 and across the 2025-12-30 gap | 2e-15 relative |
| RUONIA index futures RFU6 final (2026-09-01) | 4.4697, exact |
| KEYRATE index 2026-08-24..2026-10-16 from Bank of Russia decisions | exact (14.00 throughout; no KEYRATE future has expired yet) |
| RFZ6 live mark 2026-10-16 vs the key-rate path of the KEYRATE futures + RUONIA spread | 4.6278 vs 4.6246 (0.0032, 0.07 %); a market mark of an untraded contract; basis -54 bp |
| OFZ conversion factors, December 2021, 25 bonds | 19 exact, 6 (OF10/OF15) within 2e-4 |
| OFZ delivery prices from published CFs | all 25 exact |
| OFZ default delivery issues (5 series) | all 5 by the spec rule and by the model's cheapest bond |
| OFZ settlement 2021-12-02 (carry 3 days, funding 7.5 %) | within 0.5 % |
| OFZ settlement on the last trading day (no carry) | 0.04-0.07 % (OFZ2, OF15), 0.3-0.7 % (OFZ4, OFZ6, OF10): evening settlement after the bond close |

The settlement price of a live contract is a market price (or, without trades, the
exchange's own mark); only final settlement prices are fully determined by inputs.

## Performance

`benchmarks/rate_futures_bench.py --positions 2000` (Linux x86_64, Python 3.13; Python
3.11 within 15 %): a book of 12 contracts (RUON, RUONIA index, KEYRATE, two OFZ series on a five-bond basket).

| RUONIA curve | engines | first order | + Hessian | + 5 scenarios |
|---|---|---|---|---|
| discount factors | shared per contract (`for_quantity`) | 0.36 ms | 1.05 ms | 1.29 ms |
| discount factors | one engine per position | 1.45 ms | 12.6 ms | 12.9 ms |
| OIS quotes (rebootstrap) | shared | 0.55 ms | 3.0 ms | 2.9 ms |
| OIS quotes (rebootstrap) | one engine per position | 1.6 ms | 14.5 ms | 14.5 ms |

per position. An OFZ engine with the 24-factor Hessian costs about 35 ms; positions on
one contract should share an engine.

## Out of scope

- Futures in the local worker. `value_portfolio` takes them (`RateFuturesInstrument`,
  `OfzFuturesInstrument`, result contract v3 with `FUTBASIS`,
  [portfolio-v3.md](portfolio-v3.md)).
- Delivery-option value, futures convexity (stochastic rates), margin funding.
- RUSFAR futures (`1MFR`, same specification as RUON): the RUSFAR fixing is a MOEX
  indicator; core has no RUSFAR fixings type yet.
- The exchange calendar, the meeting calendar and the conversion yield of an OFZ series:
  caller inputs.
