# FX forward, FX swap and FX futures horizon (BACKLOG #101)

## Added

- `FxForwardRiskEngine.horizon(horizon_date, *, horizon_spot_value_date, fx_horizon="spot_held")`,
  `fx_forward_horizon(request, ...)` and the frozen result `FxForwardHorizonValuation`
  (`rub_fixed_income.fx_forwards`), model version `rub-fx-forward-horizon-v1`.
- `FxFuturesRiskEngine.horizon(...)`, `fx_futures_horizon(request, ...)` and
  `FxFuturesHorizonValuation` (`rub_fixed_income.fx_futures`), model version
  `rub-fx-futures-horizon-v1`.
- All six names are exported from `rub_fixed_income`. No existing number, field or
  `base_id` changes; the results carry the engine's `base_id`.

## Conventions

The horizon semantics are those of bonds and swaps (`horizon`, `swaps`):

- `pv_start`: the engine's PV at the curve date (zero for a margined futures position).
- `received`: legs paid in `(valuation, horizon]`, as cash, undiscounted, not
  reinvested (zero for futures: the variation margin is the price change).
- `pv_forward`: the remaining legs at the horizon on today's curves read forward,
  `DF(t) / DF(h)`; `pv_rolled`: on rolled curves (`roll_curve`, shape held in calendar
  days), the desk theta.
- `carry = pv_forward + received − pv_start`, `rolldown = pv_rolled − pv_forward`,
  `horizon_pnl = pv_rolled + received − pv_start`; for futures the same with
  `quantity · multiplier` times prices.
- Base (unshocked) curves only; a `CurveBootstrapRequest` uses the engine's built curve.
  The FX basis of a forward and the calibrated basis of a future are held in bp.
- `horizon_date` must follow the valuation date; `horizon_spot_value_date` (the spot
  date on the horizon date, from the caller's spot lag) must not precede the horizon or
  today's spot date. Errors are `InvalidTerms`.

The FX spot at the horizon is a stated rule (`FxHorizon`, checked with `fx_horizon()`):

- `"spot_held"`: today's spot (sticky spot: forward points roll down). Foreign cash
  received by the horizon is converted at today's spot.
- `"forwards_realised"`: today's outright for the horizon spot value date, with the
  forward's basis (`exp(b · τ(s0, s_h))`); for futures without the basis, which is
  contract-specific. Foreign cash received by the horizon is converted at the leg's
  today forward rate (basis included). Then `pv_forward` of a forward or swap is
  today's PV of the legs paid after the horizon divided by `DF_rub(h)` (exact, by
  additivity of `τ`).

Remaining legs at the horizon run by parity from `S_h` at `s_h` on the chosen view,
basis `τ` measured from `s_h` (signed), discounted to `h` on the RUB view.

Choices made where the specification left room:

- **Cash-settled leg fixed by the horizon, paid after it** (value date `≤ h <` payment
  date): it keeps the engine's backward-parity formula from `s_h`. The curve dates
  before the horizon (its value date) read today's curves forward on **both** views
  (the rolled curves start at `h`), as swap fixings before the horizon keep today's
  forwards. A leg is received only when its payment date is `≤ h`.
- **Futures delivered by the horizon**: settled at the FX fixing, today's spot
  (`"spot_held"`) or today's parity forward to delivery without the basis
  (`"forwards_realised"`); `price_forward = price_rolled`, so the rolldown is zero.
- **Futures delivery between the horizon and its spot date**: the price runs backward
  by parity from `s_h` (signed `τ`), as forwards do for a leg before the spot date.
- The forward view reads `DF(t) / DF(h)` off the base curves directly rather than
  through `forward_curve`: the two agree, and the ratio also covers dates before `h`.

## Open questions

- **Known fixings.** A cash-settled leg fixed before the horizon is projected, not
  read from a published fixing; the same holds for futures delivered by the horizon.
- **Rolled view for dates before the horizon** (only the value date of such a leg): the
  bridge through today's forwards is a convention; a desk may prefer to freeze the
  fixing at the received-leg rule.
- **Portfolio.** `horizon_portfolio` does not take FX forwards, swaps or futures yet;
  an adapter can wrap these engine methods (the spot lag must be supplied per position).
- **Funding of received cash and of variation margin** is not modelled (not reinvested).
