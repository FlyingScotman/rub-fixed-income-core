# Money validation contract (since 0.3.1)

Every rule below raises `ValueError`. "Invalid" means NaN, ±infinity, or a Python `bool`
(`True`/`False` are `int` subclasses and would otherwise pass as 1/0).

## Clean/dirty conversion

`clean_price_pct_to_dirty_rub`, `clean_price_pct_to_dirty_amount`, `dirty_rub_to_clean_price_pct`:

- Clean price (%) and dirty price (RUB per bond) must be finite and strictly positive.
- Current nominal must be finite and strictly positive.
- Accrued interest must be finite and non-negative.
- The inverse conversion rejects dirty ≤ accrued interest, because that implies a clean
  price ≤ 0.
- A result that overflows to infinity is rejected, even when every input is finite.

Prices, accrued interest and nominal describe one bond. The sign of a short position belongs
in the caller's position quantity, never in a negative unit price.

## Financing

`project_financed_bond`:

- Every cash-event amount is validated before any period is processed. Signed finite amounts
  are valid: an expense of −50 increases the funding balance by 50.
- Initial dirty cash must be finite and positive. Accumulation factors must be finite,
  positive and not `bool`.
- Each closing balance must be finite; overflow raises `"financing balance result must be
  finite"`.

## Accrued interest

`analytics.accrued_interest_rub` validates the full `BondTermsV1` (nominal, coupon, accrual
period, horizon versus maturity) before computing, including on the coupon payment date
where it returns 0.

## PV, duration and solvers

- `price_cashflows` and `bond_duration` reject invalid cash flows and overflowing PV or
  duration. Signed net cash flows remain supported, including a net PV of exactly 0.
- `solve_nominal_annual_yield`, `solve_z_spread`, `solve_continuous_z_spread` and
  `solve_effective_annual_yield` reject invalid prices and cash flows.
- `BondTermsV1.validate()` and `MarketObservationV1.validate()` reject `bool` nominal, coupon
  and clean price; `analyze_bond` / `analyze_bond_v2` report these as `INVALID_UNITS`.

Valid input produces the same numbers as 0.3.0. Regression coverage lives in
`tests/test_money_validation.py`.
