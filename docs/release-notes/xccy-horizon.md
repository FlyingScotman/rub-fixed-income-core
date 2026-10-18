# XCCY horizon: carry, rolldown and theta (BACKLOG #101)

## Additions

- `XccyRiskEngine.horizon(horizon_date, *, horizon_spot_value_date, fx_horizon="spot_held")`
  and `xccy_horizon(request, *, horizon_date, horizon_spot_value_date,
  fx_horizon="spot_held", curve_cache=None, xccy_cache=None)`.
- `XccyHorizonValuation` (frozen): `base_id`, `trade_id`, `currency`,
  `valuation_date`, `horizon_date`, `fx_horizon`, `spot_rate`, `spot_value_date`,
  `horizon_spot_rate`, `horizon_spot_value_date`, `pv_start_rub`, `pv_forward_rub`,
  `pv_rolled_rub`, `received_rub`, `carry_rub`, `rolldown_rub`, `horizon_pnl_rub`,
  `model_version`.
- `XCCY_HORIZON_MODEL_VERSION = "rub-xccy-horizon-v1"`.
- Exported from `rub_fixed_income.xccy` and the package root. No existing number,
  identity or field changes.

## Conventions

- The common horizon semantics of bonds and swaps: `received` is cash paid in
  `(valuation, horizon]` (undiscounted, not reinvested, today's projections);
  `pv_forward` reads today's curves forward (`DF(t) / DF(h)`); `pv_rolled` uses
  `roll_curve` (shape held in calendar days), the theta. `carry + rolldown =
  horizon_pnl`.
- Floating legs (RUB RUONIA and a floating foreign leg) on rolled curves: observations
  before the horizon keep today's forwards, later ones project on the rolled projection
  curve, `growth = known * DF_today(ps) / DF_today(m) * DF_rolled(m) / DF_rolled(pe)`
  with `m = min(max(ps, h), pe)`, as in swaps.
- FX at the horizon (`FxHorizon`): `spot_held` keeps today's spot; `forwards_realised`
  takes today's outright for `horizon_spot_value_date`, basis included. Received foreign
  flows convert at today's spot (`spot_held`) or at today's outright for their payment
  date (`forwards_realised`).
- Remaining foreign flows at the horizon: `CF * DF_for(t) / DF_for(s_h) *
  exp(b * tau(s_h, t)) * S_h * DF_rub(s_h)` on the forward or rolled view; the basis is
  held in bp. With `forwards_realised`, `pv_forward` equals today's PV of the flows after
  the horizon over `DF_rub(h)` (exact for an additive basis day count such as ACT/365F).
- Base curves only. A bootstrapped foreign curve is read forward and rolled as built;
  its quotes are not rebootstrapped at the horizon.
- `horizon_spot_value_date` is explicit (the request has no spot calendar); it must be
  on or after the horizon date and today's spot value date. A horizon on or before the
  valuation date, a non-date horizon or an unknown `fx_horizon` raise `InvalidTerms`.

## Open questions

- Portfolio wiring: `horizon_portfolio` does not take XCCY positions yet (no adapter
  in `portfolio_products.py`); the adapter must derive `horizon_spot_value_date` from
  the spot lag and calendar.
- `FxHorizon` is restated in `xccy.py` as the same `Literal` (`horizon` imports
  `domain`, which imports `xccy`); a test pins it to `FX_HORIZONS`. Moving the alias to
  a leaf module would remove the copy.
- Rebootstrapping the foreign curve from held quotes at the horizon (a "quotes held"
  rolldown) is not offered.
- A basis day count that is not additive (30/360) breaks the exact
  `forwards_realised` identity by the day-count mismatch only.
- Remaining flows paid between the horizon and `s_h` are valued with the same
  signed-tau formula (DF ratios above one), as today's PV treats flows before the spot
  date.
