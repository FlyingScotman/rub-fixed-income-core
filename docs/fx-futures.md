# FX futures: interest-rate, basis and spot risk (0.8.1)

`rub_fixed_income.fx_futures` prices an FX future against RUB by covered interest
parity. It gives the rate risk of the position on the RUB curve and on the
foreign-currency curve, the basis risk, the FX delta and exact scenarios. The
framework matches `BondRiskEngine`: explicit curves and risk basis, a calibrated
parameter frozen under shocks, the 11-bucket grid, a shared `ShockedCurveCache`,
exact repricing, a full Hessian and content identities. The module does no I/O.

## Why an FX future has rate risk

The price follows the forward:

```text
F = S * [DF_for(T) / DF_for(s)] / [DF_rub(T) / DF_rub(s)] * exp(b * tau(s, T))
```

- `S` is the spot rate for value date `s` (for example TOM).
- `T` is the delivery date.
- `b` is the basis.

When RUB rates rise, `F` rises; when foreign rates rise, `F` falls. So a long
position in USD/RUB futures carries:

- a positive RUB DV01, about `+F * (T - s)` per unit of rate;
- a negative foreign DV01, of about the same size;

on top of the FX delta. For a short, both signs flip. The RUB and foreign legs do
not net out, because they are risks to different curves.

## Inputs

- `FxFuturesContract(contract_id, currency, contract_size, price_quote_units, delivery_date)`.
  Everything comes from the exchange specification. Nothing is hard-coded.
  - `contract_size` is foreign units per contract.
  - `price_quote_units` is how many foreign units the quoted price refers to. Use
    1000 when a 1000-unit contract is quoted in RUB per 1000 units, and 1 when it
    is quoted in RUB per unit.
  - `multiplier = contract_size / price_quote_units` is the RUB of variation
    margin per contract per point of price.
  - `delivery_date` is the **value date of the spot rate that the final
    settlement price references**, not the last trading day. If the futures
    settle to a fixing on expiry day, it is that fixing's spot value date. The
    caller derives it from the official calendar.
- `FxFuturesRiskRequest(contract, spot_rate, spot_value_date, rub_curve, foreign_curve, futures_price=None, basis_bp=None, basis_day_count=ACT_365_FIXED, quantity=1)`.
  - `spot_rate` uses the same units as the futures price.
  - Supply **exactly one** of `futures_price` and `basis_bp`. A market
    `futures_price` calibrates `b = ln(F_mkt / F_theo) / tau` analytically.
  - Both curves must have the same `curve_date`, and `curve_date <= spot_value_date < delivery_date`.
  - Each curve is a `DiscountCurve` (`continuous_zero` basis) or a
    `CurveBootstrapRequest` (`ois_quote` basis), the same as in
    [risk.md](risk.md). The bases may differ, for example a RUB OIS
    rebootstrap with a foreign curve given as discount factors. Each result
    records both bases.
  - `quantity` is signed contracts.
  - Collections and calendars are copied, as in `BondRiskRequest`.

The caller decides which foreign curve gives the foreign rate (for example a
USD/CNY OIS curve, or an implied curve built from FX swaps). The library does not
certify its source. Covered-parity deviations in the RUB market can be large.
Everything the curves do not explain goes into `b`.

## Risk: `engine.risk(bump_bp=1, difference="forward")`

All money is signed RUB of position variation margin: `quantity * multiplier * dF`.
Margin is daily, so a price change is P&L on the same day and is **not discounted**.

| Field | Meaning |
| --- | --- |
| `futures_price`, `theoretical_price` | Base price, and the price with `b = 0` |
| `basis_bp`, `carry_years` | Calibrated or given basis; `tau(s, T)` in `basis_day_count` |
| `notional_rub` | `quantity * multiplier * F` |
| `fx_delta_foreign` | Equivalent spot position in foreign currency: `quantity * contract_size * F / S` |
| `fx_delta_rub_per_pct` | P&L for a +1 % relative spot move (exact, since F is linear in S) |
| `rub_buckets`, `rub_dv01_rub` | 11 RUB-curve buckets and their sum |
| `rub_parallel_dv01_rub` | Independently repriced parallel RUB shift |
| `foreign_buckets`, `foreign_dv01_rub`, `foreign_parallel_dv01_rub` | The same for the foreign curve |
| `basis01_rub` | +1 bp of basis |

The bucket grid, triangular weights and both shock operations are the same as
for bonds ([risk.md](risk.md)). In a bucket, `krd_years` is the duration of the
**futures price**, `-(1/F) dF/dr * 1e4`. For RUB it is about `-(T - s)`, and for
the foreign curve about `+(T - s)`. It does not depend on quantity.

The settlement-DF shock is included. When `s > curve_date`, a bucket that covers
only `s` (usually 1M) gets a small exposure of the opposite sign. A parallel
shift moves only the interval `[s, T]`.

The calibrated `b` stays frozen under every rate and spot shock. Rate risk
therefore means: the curve moves, and the observed basis stays where it was.

## Scenarios: `engine.reprice(FxFuturesScenario(...))`

`FxFuturesScenario(rub_curve_shifts_bp, foreign_curve_shifts_bp, basis_shift_bp, fx_spot_shift_pct)`
takes:

- 11 + 11 absolute bp shifts;
- a basis shift in bp;
- a relative spot shift in percent (> -100).

`FxFuturesScenario.parallel(...)` builds parallel vectors. The result gives
`shocked_price`, the exact total P&L, and the exact isolated P&L of RUB rates,
foreign rates, basis and spot. `interaction_pnl_rub` is the total minus those four.

## Second order: `engine.second_order(bump_bp=1)`

This gives the central gradient and full symmetric Hessian over 23 factors:
`RUB 1M..20Y+`, `<CCY> 1M..20Y+` and `basis`. Values are RUB/bp and RUB/bp²
including quantity. The formulas are the same as for bonds
([second-order-local-risk.md](second-order-local-risk.md)). Spot is not a factor:
the price is linear in spot, so under a relative spot move `x` every rate and
basis derivative scales by exactly `1 + x`.

## Horizon (carry, rolldown, theta)

`engine.horizon(horizon_date, *, horizon_spot_value_date, fx_horizon="spot_held")` and
`fx_futures_horizon(request, horizon_date=..., horizon_spot_value_date=..., fx_horizon=...)`
return an `FxFuturesHorizonValuation`. The price at the horizon is

```text
F_h = S_h · [DF_for(T) / DF_for(s_h)] / [DF_rub(T) / DF_rub(s_h)] · exp(b · τ(s_h, T))
```

with the calibrated basis `b` held in bp, on today's curves read forward
(`price_forward`, `DF(t) / DF(h)`) or on rolled curves (`price_rolled`, shape held in
calendar days: the theta). `S_h` is today's spot (`"spot_held"`) or today's parity
forward for `horizon_spot_value_date` **without** the futures basis
(`"forwards_realised"`): the basis is contract-specific, so under that rule the carry
is the basis decaying over the horizon. `horizon_spot_value_date` is the spot date on
the horizon date (the caller applies the spot lag); `τ` is signed if delivery falls
before it.

A contract delivered by the horizon has settled at the FX fixing:
`price_forward = price_rolled` = today's spot (`"spot_held"`) or today's parity forward
to delivery without the basis (`"forwards_realised"`, convergence).

A position is margined: `pv_start_rub = received_rub = 0`, and with
`scale = quantity · multiplier` the variation margin splits into
`carry = scale · (price_forward − price_start)` and
`rolldown = scale · (price_rolled − price_forward)`;
`horizon_pnl = scale · (price_rolled − price_start)`. On flat curves the rolldown is
zero. Model version: `rub-fx-futures-horizon-v1`.

## Identity

`base_id` hashes:

- the model version and contract terms (including the multiplier);
- spot and spot date;
- the content and basis of both curves;
- the input price or basis, the calibrated basis, its day count, and the quantity.

`scenario_id` hashes the scenario. Model versions:

- `rub-fx-futures-parity-risk-v1`;
- `rub-fx-futures-parity-second-order-v1`.

## Out of scope

- **Futures vs forward convexity.** Rates are deterministic, so the futures and
  forward prices coincide. There is no rate/FX correlation adjustment.
- **Accrued variation margin and initial margin.** Only the change from the
  current price is counted. Funding of margin is not modelled.
- **OTC FX forwards and swaps.** Their P&L is paid at `T` and must be discounted
  on the RUB curve. That is a different instrument.
- **Exchange specification, calendar, delivery-date calculation.** All of these
  belong to the caller.
- **Local worker.** The portfolio worker and its `rub-local-portfolio-risk-result.v1`
  contract cover fixed RUB bonds only. Putting FX futures into a job needs a new
  result-contract version (BACKLOG #33).

## Executable synthetic example

```python
from datetime import date

from rub_fixed_income import (
    CurveBootstrapRequest, DiscountCurve, FxFuturesContract, FxFuturesRiskEngine,
    FxFuturesRiskRequest, FxFuturesScenario, OisQuote,
)

valuation = date(2026, 10, 15)
rub_quotes = tuple(
    OisQuote(tenor, maturity, rate)
    for tenor, maturity, rate in (
        ("3M", date(2027, 1, 15), 0.165),
        ("6M", date(2027, 4, 15), 0.160),
        ("1Y", date(2027, 10, 15), 0.150),
    )
)
engine = FxFuturesRiskEngine(FxFuturesRiskRequest(
    contract=FxFuturesContract(
        "SYNTH-USDRUB-DEC", "USD", contract_size=1000, price_quote_units=1000,
        delivery_date=date(2026, 12, 18),
    ),
    spot_rate=95_000.0, spot_value_date=date(2026, 10, 16),
    rub_curve=CurveBootstrapRequest(valuation, rub_quotes),
    foreign_curve=DiscountCurve.flat(valuation, 0.04),
    futures_price=97_100.0, quantity=-50,
))
risk = engine.risk()
assert risk.rub_dv01_rub < 0 < risk.foreign_dv01_rub  # short futures
stress = engine.reprice(FxFuturesScenario.parallel(rub_shift_bp=200, fx_spot_shift_pct=-5))
gamma = engine.second_order()
```

## Validation

`tests/test_fx_futures.py` covers:

- analytical parity with flat curves, and opposite-sign RUB and foreign risk;
- the bucket at a pillar, and the shift of DF at the spot date;
- basis calibration and its freeze;
- the basis day count, the multiplier for per-unit quotes, and a signed or zero quantity;
- exact scenarios with interaction;
- the Hessian against exponential derivatives;
- the OIS basis against an independent rebootstrap, and mixed bases with a shared cache;
- identity and input checks.

These are synthetic mathematical references. They are not a reconciliation with
QR/Calypso or MOEX settlement prices.
