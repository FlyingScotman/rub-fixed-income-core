# KBD (MOEX zero-coupon yield curve) golden fixtures

Raw public MOEX ISS responses of `/iss/engines/stock/zcyc.json?iss.only=params,yearyields`
(`date=` set for the EOD ones). Copied unchanged from the market-data hub repository
`FiveLimaGolf/market-data-hub`, `tests/fixtures/moex_market/` at commit `c7a9d44`
(16 October 2026):

| File | Source file | Publication |
|---|---|---|
| `zcyc_2022-01-03.json` | `zcyc_2022-01-03.json` | 2022-01-03 18:39:54, EOD |
| `zcyc_2026-10-15.json` | `zcyc_2026-10-15.json` | 2026-10-15 18:54:59, EOD |
| `zcyc_2026-10-16_intraday.json` | `zcyc_current.json` | 2026-10-16 10:23:52, intraday |

`yearyields` are MOEX's published effective annual yields (%, 4 decimals = 0.01 bp). Core
keeps its own copies so its CI never checks out the hub (system doc 02 §7). Core
reproduces them from `params` independently in `tests/test_kbd.py`.
