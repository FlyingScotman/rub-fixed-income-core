# Money validation in 0.3.1

Clean/dirty conversion requires positive finite unit prices and current nominal,
and finite nonnegative accrued interest. NaN, infinities, booleans, negative unit
prices and zero nominal raise ValueError. Inverse conversion also rejects a dirty
amount at or below accrued interest, since that would imply a nonpositive clean
price. Finite inputs that overflow the result are rejected.

Financing validates every cash-event amount before processing periods, rejects
boolean accumulation factors and checks each closing balance for overflow.
Signed finite cash events remain valid: an expense of -50 increases the funding
balance by 50. Price/NKI/nominal describe one bond; a short position's sign belongs
in its external position quantity, rather than a negative unit price.

The standalone accrued-interest helper validates its bond terms even on a coupon
payment date. Public PV and duration calculations reject non-finite cashflows and
overflowing results. Signed net cashflows, including zero net PV, remain supported
by the generic PV function. Existing convention, golden-value and reproducibility
tests remain unchanged.

Validation: full suite 255 passed, including regressions for each conversion
input, invalid cash events, valid signed expenses, finite-input overflow, accrued
interest and PV/duration overflow. Core financing regressions fail against the
unchanged GitHub main, confirming the original defect. Changed source files pass
Ruff and mypy with the local Python 3.13 target; lock consistency passes offline.
The configured Python 3.11 type-check target is incompatible with the locally
installed NumPy stubs, so it was not used for this check.

The companion market-data-store change consumes this patch as core 0.3.1 and
guards its own valuation inputs and observation storage boundaries.
