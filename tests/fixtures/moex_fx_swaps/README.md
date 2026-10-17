# MOEX CNY/RUB swap fixtures (package 4, BACKLOG #34)

Raw public MOEX ISS responses, unchanged, fetched directly from `iss.moex.com` on
2026-10-17 at 13:44 UTC (a Saturday; the last session was 2026-10-16). Used by
`tests/test_moex_fx_swap_goldens.py`.

| File | ISS endpoint | Content |
|---|---|---|
| `srate_cny_on.json` | `/iss/statistics/engines/currency/markets/fixing/SRATE_CNY_ON.json?from=2026-06-01&till=2026-10-16&iss.meta=off` | MOEX indicative CNY/RUB overnight swap rate, 92 trade dates: `rate` (TOD/TOM swap points, RUB per CNY) and `rateprc` (implied RUB rate, %) |
| `cets_cnyrub_tod_history.json` | `/iss/history/engines/currency/markets/selt/boards/CETS/securities/CNY000000TOD.json?from=2026-06-01&till=2026-10-16&iss.meta=off` | CNYRUB_TOD daily history (open, low, high, close, trades, WAP) |
| `cets_cnyrub_tom_history.json` | the same for `CNYRUB_TOM` | CNYRUB_TOM daily history |
| `cets_cnyrub_todtom_history.json` | the same for `CNYRUBTODTOM` | CNY_TODTOM swap daily history (points); kept for reference |
| `cets_cny_securities.json` | `/iss/engines/currency/markets/selt/boards/CETS/securities.json?securities=CNY000000TOD,CNYRUB_TOM,CNYRUB_SPT,CNYRUBTODTOM,CNYRUB_TOM1D,CNYRUB_TOM1W,CNYRUB_TOM2W,CNYRUB_TOM1M,CNYRUB_TOM2M,CNYRUB_TOM3M,CNYRUB_TOM6M&iss.only=securities&iss.meta=off` | instrument list of the 2026-10-16 session with `SETTLEDATE` (value date; near-leg date for swaps) |

Each history file holds a single page (`history.cursor` TOTAL below the page size of 100).

SHA-256 at download:

```text
de42d1dcf0ac893d14a010251625ee869a0fa86615ef36a43b3685f9a7518e69  srate_cny_on.json
e986fcc1e7f46bd37ea7c83a7394a6f81791a1863c7c51bc536ec539edbe1cc7  cets_cnyrub_tod_history.json
e76ae93986d245525179be7aec28cb642b8d0c9713def8fe52ad34b45dee11d0  cets_cnyrub_tom_history.json
0882645c4fab02cf9d2384031c0e230c363e57b07ab4f4ab5bfff609686ab0b9  cets_cnyrub_todtom_history.json
bae235ead2ecd0dee3ea533f222e16f805b8cd52fdc93ea78255597897f124e1  cets_cny_securities.json
```

## What the data establish

The methodology of `SRATE_*` is published on `fs.moex.com`, which the build
environment cannot reach, so the following is established from the data only:

- **Value dates.** CNY/RUB settles on the joint Russian and Chinese calendar. TOD
  did not trade on Chinese holidays (2026-06-19 Dragon Boat, 2026-09-25 Mid-Autumn,
  2026-10-01..07 National Day) while TOM did; the TOD/TOM period from 2026-09-30 is
  8 days (to 2026-10-08). On 2026-06-12 (Russia Day) neither traded. On the
  2026-10-16 session TOD, TOM and SPT settle on 10-16, 10-19 and 10-20.
- **`rateprc`.** It is the money-market parity rate on ACT/365F with no CNY rate term:
  `rateprc / 100 = rate / S * 365 / days`. With `S` = the day's TOD WAP the core
  reproduces it with a mean error of -1.0 bp, a median absolute error of 3.4 bp and a
  maximum of 22 bp over 91 days. ACT/360 is off by -18 bp on average, and adding even a
  1 % CNY rate by about +100 bp, so both are rejected. The remaining scatter is not
  explained: `rate` is rounded to 1e-5 RUB (about 1.5 bp on one day), and the spot and
  time window behind the indicator are not the daily WAP (the indicator's `rate` also
  differs from the daily WAP of the swap).
- Term swaps (`CNYRUB_TOM1W` ... `TOM6M`) rarely trade on CETS; their `PREVPRICE` is
  often days old, so they are not used as goldens.
