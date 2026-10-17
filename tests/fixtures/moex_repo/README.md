# MOEX ISS RUSFAR history (repo goldens)

Raw, unmodified ISS response used by `tests/test_moex_repo_goldens.py`.

| File | Request | Retrieved (UTC) | Bytes | SHA-256 |
| --- | --- | --- | --- | --- |
| `rusfar_2026-08-25_2026-10-16.json` | `https://iss.moex.com/iss/history/engines/stock/markets/index/securities/RUSFAR.json?iss.meta=off&from=2026-08-25&till=2026-10-16` | 2026-10-17 | 6736 | `a53a589cde6986e3b9f6d906ccba5d4e543c26d8547c831f88d65310dffb2671` |

Fields: `TRADEDATE` is the RUSFAR calculation date, `CLOSE` the value in percent per
annum (two decimals, methodology section 2), board `MMIX`. One row per weekday: there is no
Russian public holiday in the window, and RUSFAR is not computed on weekends
(methodology section 3). The history cursor reports 39 rows in one page.

The 2026-09-01 to 2026-09-07 values (13,99 / 13,88 / 13,95 / 14,15 / 13,99) are the
fixings in MOEX's worked examples of the compounding open repo
(`docs/sources/repo_moex/README.md`, presentation of 10 September 2026, slides 10 and
17). Core reads them through a `FixingHistory(index="ruonia", ...)`, which applies the
daily-publication rules (a fixing on every publication-calendar business day, never
filled) that RUSFAR follows.
