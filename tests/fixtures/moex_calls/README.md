# MOEX call and perpetual bond fixtures (package 9b)

Raw public MOEX ISS responses, unchanged, fetched directly from `iss.moex.com` on
2026-10-17 at about 13:46 UTC, for trade date 2026-10-15 (settlement T+1, 2026-10-16):

| Path | ISS endpoint |
|---|---|
| `history/<SECID>_TQCB.json` | `/iss/history/engines/stock/markets/bonds/boards/TQCB/securities/<SECID>.json?from=2026-10-15&till=2026-10-15&iss.meta=off&iss.only=history` |
| `bondization/<SECID>.json` | `/iss/statistics/engines/stock/markets/bonds/bondization/<SECID>.json?iss.meta=off&limit=unlimited` |

## What the fields mean

Established on the full TQCB board of 2026-10-15
(`/iss/history/engines/stock/markets/bonds/boards/TQCB/securities.json?date=2026-10-15`,
3024 rows, not committed):

- **Call bonds.** 156 rows have `BONDSUBTYPE` "До оферты (call)". Only 7 carry a
  `CALLOPTIONDATE`, and for each of the 6 that traded `YIELDATWAP == CALLOPTIONYIELD`
  and `DURATION == CALLOPTIONDURATION`: MOEX yields a bond to its call when a call
  date is set in its offers (`offertype` "Оферта/Погашение" with a real date), and to
  maturity otherwise (the 25 call bonds of `tests/fixtures/moex_eod/`). The yield is
  the effective annual yield on ACT/365F, the duration Macaulay in days, both at
  settlement. `YIELDCLOSE` is the same measure at `CLOSE`.
- **Perpetual bonds** ("Бессрочные", empty `MATDATE`). `YIELDCLOSE` / `YIELDATWAP` is
  the current yield `COUPONPERCENT / price`, rounded to 0.01, for all 32 published
  values; `DURATION` is empty. It is not a yield to a call date.

## Bonds

| SECID | Kind | Checked |
|---|---|---|
| RU000A1087A7 | fixed corporate, call 2027-03-25 at 100; coupons after the call not set | `CALLOPTIONYIELD` (at `WAPRICE`), `YIELDCLOSE`, `CALLOPTIONDURATION`; to maturity is rejected (unknown coupons) |
| RU000A10AST0 | ABS (Совкомбанк securitization, class A), scheduled amortization, clean-up call 2028-01-25 | as above, on the amortised face 701 |
| RU000A0ZZE87, RU000A1034P7 | perpetual subordinated bonds | `YIELDCLOSE` / `YIELDATWAP` = current yield |

RU000A10AST0's bondization lists the call redemption itself (99 per bond on
2028-01-25) as an amortization, and a zero amortization on the legal maturity
2030-12-31. The test builds the instrument as an adapter would: amortizations up to
2027-12-25, the remaining 99 redeemed at the legal maturity, and the call on
2028-01-25 at 100 % of the face outstanding (`value` 99 over the face then
outstanding; the offer's `price` 9.9 is % of the initial face).
