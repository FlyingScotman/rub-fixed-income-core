# Rate and OFZ futures horizon (BACKLOG #101)

Carry, rolldown and theta to a horizon for MOEX short-rate futures (RUON, RUONIA index,
KEYRATE) and OFZ basket futures. No existing number, `base_id` or field moves.

## Added

- `RateFuturesRiskEngine.horizon(horizon_date)` and
  `rate_futures_horizon(request, *, horizon_date, curve_cache=None)`, returning the frozen
  `RateFuturesHorizonValuation` (`RATE_FUTURES_HORIZON_MODEL_VERSION =
  "rub-rate-futures-horizon-v1"`).
- `OfzFuturesRiskEngine.horizon(horizon_date, *, horizon_spot_settlement_date)` and
  `ofz_futures_horizon(request, *, horizon_date, horizon_spot_settlement_date,
  curve_cache=None)`, returning `OfzFuturesHorizonValuation`
  (`OFZ_FUTURES_HORIZON_MODEL_VERSION = "rub-ofz-futures-horizon-v1"`), which also names
  the cheapest bond at the start and in each view.
- Both work on engines made by `for_quantity` (same prices, own scale).
- Docs: "Horizon (carry, rolldown, theta)" in `docs/rate-futures.md`; tests:
  `tests/test_futures_horizon.py`.

## Conventions

- The two curve views of `horizon`: forward (`forward_curve`, today's forwards realised)
  and rolled (`roll_curve` / `roll_key_rate_curve`, shape held in calendar days). Base
  curves only (an OIS request uses the engine's built curve); basis and bond spreads held.
- Daily margining: `pv_start_rub = received_rub = 0`; `carry = scale * (price_forward -
  price_start)`, `rolldown = scale * (price_rolled - price_forward)`, `horizon_pnl =
  scale * (price_rolled - price_start)`, `scale` = quantity times margin per point.
- Rate futures: rates observed before the horizon (RUONIA business day `T < h`, index span
  starting before `h`, key rate effective before `h`) keep today's projection plus basis
  in both views; later ones are projected on the view plus basis. `price_forward` therefore
  equals today's price up to rounding (zero carry). A decided key rate (before
  `current_rate_end`) does not move. A contract whose last trading day is on or before the
  horizon settles on the realised path (`price_forward == price_rolled`); its price is the
  unrounded model price (the final-price rounding to the step is not applied).
- OFZ futures: each bond priced at the caller's horizon spot settlement `s_h` (bond view
  anchored at `s_h`, spread held, continuous ACT/365F from `s_h`), carried to delivery on
  the funding view with the basis held, coupons in `(s_h, D]` removed; the price is the
  basket minimum of `lot * (forward - AI(D)) / CF`.
- Validation: `horizon_date` must follow the valuation date; for OFZ,
  `horizon_date <= s_h`, `s_h` not before today's spot settlement and strictly before the
  delivery date (`InvalidTerms`).

## Open questions

- OFZ carry through delivery (`s_h >= D`) is rejected: after delivery the position is a
  bond, not a future. MOEX has listed no OFZ futures since 2022, so this stays a
  documented limitation.
- The rolled key-rate curve reaches every effective date the request accepts (rolling
  shifts its end forward too), so the `MissingMarketData` guard for a rolled curve that
  does not reach the effective date is defensive and has no test.
- `horizon_portfolio` does not carry futures yet (rows for `RateFuturesInstrument` and
  `OfzFuturesInstrument` are a follow-up in `portfolio_products`).
- Should a contract settled inside the horizon report its exchange-rounded final price?
  Kept unrounded here so carry and rolldown stay continuous in the horizon date.
