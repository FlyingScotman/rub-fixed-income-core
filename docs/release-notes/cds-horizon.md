# CDS horizon: carry, rolldown and theta (BACKLOG #101)

## Additions

All are in `rub_fixed_income.cds` and exported from `rub_fixed_income`.

- `CDS_HORIZON_MODEL_VERSION = "rub-cds-horizon-v1"`.
- `CdsHorizonValuation` (frozen) has these fields: `base_id`, `contract_id`,
  `currency`, `side`, `valuation_date`, `horizon_date`, `pv_start`, `pv_forward`,
  `pv_rolled`, `received`, `accrued_start`, `accrued_horizon`, `carry`, `rolldown`,
  `horizon_pnl` and `model_version`.
- `CdsRiskEngine.horizon(horizon_date)`.
- `cds_horizon(request, *, horizon_date, curve_cache=None, cds_cache=None)`.
- `forward_hazard_curve(curve, horizon_date)` and `roll_hazard_curve(curve,
  horizon_date)`: the two hazard-curve views.

No existing number, field, identity or `base_id` changes.

## Conventions

These match the bond and swap horizons (`horizon.py`, `SwapRiskEngine.horizon`):

- `carry = pv_forward + received - pv_start`;
- `rolldown = pv_rolled - pv_forward`;
- `horizon_pnl = pv_rolled + received - pv_start`.

- **Start value.** `pv_start` is the engine's base dirty `pv`.
- **Curves.** Only the base curves are used, never shocks. Recovery is the credit
  request's.
- **Forward curves.** The discount curve is `forward_curve(base, h)`. The hazard curve
  keeps the same rates on the same node dates after `h`, and the rate in force at `h`
  applies from `h`. Survival is `Q(t)/Q(h)` exactly.
- **Rolled curves.** The discount curve is `roll_curve(base, h)`. The hazard curve has
  every node date shifted by `h - curve_date`, so the spread shape is held in calendar
  days. `horizon_pnl` is the desk theta.
- **No default.** Every amount is conditional on no default before the horizon.
  `received` is undiscounted cash and is not weighted by survival.
- **Received window.** `received` counts the premium periods that leave the contract's
  value between the two step-in dates: live at today's step-in, with accrual ending by
  `horizon + step_in_days`. The task stated payments in `(valuation, horizon]`. The two
  windows differ only on the step-in boundary days, and the step-in window keeps the
  identities free of a lost or doubled coupon:
  - with the standard 1-day step-in, a coupon paid on the horizon's step-in day is not in
    the horizon's ISDA value, so it is counted as received;
  - a coupon paid on today's step-in day is not in `pv_start`, so it is not counted.
- **Matured at the horizon.** When `horizon + step_in_days` is after the maturity, every
  remaining coupon is received and `pv_forward = pv_rolled = accrued_horizon = 0`.
  `value_cds` is not called.
- **Accrued premium.** `accrued_start` is the engine's `CdsValuation.accrued_premium`.
  `accrued_horizon` is the rolled valuation's (the forward valuation's is the same: it is
  date arithmetic only).
- **Validation.** A horizon on or before the valuation date, or one that is not a date,
  raises `InvalidTerms`.

## Open questions

- **Portfolio.** `horizon_portfolio` still reports CDS as `UNSUPPORTED_PRODUCT`. An
  adapter would call `CdsRiskEngine.horizon` on the engine `value_portfolio` builds.
- **Default over the window.** Expected (default-weighted) carry is not computed: the
  survival-weighted premium, and protection and accrual paid on default over
  `(valuation, horizon]`. The figures here are the no-default path, the desk
  convention.
- **Spread-held rolling.** The rolled curve holds the hazard shape. Holding the quoted
  par spreads per tenor and rebootstrapping at the horizon on the rolled discount curve
  would differ slightly when rates are not flat.
- **Received window.** Confirm the step-in boundary choice above against the swap and
  bond `(valuation, horizon]` window.
