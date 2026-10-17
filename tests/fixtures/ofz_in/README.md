# OFZ-IN golden fixtures (package 9a)

Raw public responses, unchanged, fetched on 2026-10-17 between about 13:40 and 13:50 UTC.
The tests read them through `tests/ofz_in_data.py` and `tests/test_ofz_in_goldens.py`.

| Path | Source | Content |
|---|---|---|
| `history/<SECID>_<start>.json` | `https://iss.moex.com/iss/history/engines/stock/markets/bonds/boards/TQOB/securities/<SECID>.json?from=2015-01-01&till=2026-10-16&iss.meta=off&iss.only=history,history.cursor&history.columns=TRADEDATE,FACEVALUE,ACCINT,CLOSE,YIELDCLOSE,DURATION,COUPONVALUE&start=<start>` | every EOD row of the bond since it was listed (pages of 100; `SU52002RMFS1_1900.json` was fetched again after an empty reply) |
| `bondization/<SECID>.json` | `https://iss.moex.com/iss/statistics/engines/stock/markets/bonds/bondization/<SECID>.json?iss.meta=off&limit=unlimited` | coupon periods with the paid amounts, rate 2.5 % |
| `securities/<SECID>.json` | `https://iss.moex.com/iss/securities/<SECID>.json?iss.meta=off` | `ISSUEDATE` (the placement date, the indexation base), `MATDATE`, `INITIALFACEVALUE` |
| `board/TQOB_2026-10-16.json` | `https://iss.moex.com/iss/history/engines/stock/markets/bonds/boards/TQOB/securities.json?date=2026-10-16&iss.meta=off&limit=100` | the full TQOB board on trade date 2026-10-16, 61 bonds (`CLOSE`, `YIELDCLOSE`, `DURATION`, `FACEVALUE`, `ACCINT`, `IRICPICLOSE`, `BEICLOSE`) |
| `cbr/reg_cpd.xlsx` | `https://www.cbr.ru/Content/Document/File/175223/reg_cpd.xlsx` (Bank of Russia, "Показатели динамики потребительских цен по регионам"; linked from `https://www.cbr.ru/statistics/ddkp/aipd/`), `Last-Modified: Wed, 14 Oct 2026 16:45:00 GMT`, SHA-256 `77d73ee0ff54133e68c0413bffcf4e2396aa4a902d6914df5138ef3dc1c8c0de` | sheet `мм`, row `Россия`: CPI month on month in %, Rosstat data, 2016-01 to 2026-09, 2 decimals |

Bonds: SU52002RMFS1, SU52003RMFS9, SU52004RMFS7, SU52005RMFS4. These are all the OFZ-IN
on TQOB on 2026-10-16; 52001 matured in 2023.

## What the fields mean

Established on these four bonds over their whole history (5,927 EOD rows):

- `FACEVALUE` in the history is the indexed nominal on the **trade date**: inside a month it
  moves linearly by calendar day, with residuals at the kopeck rounding level. A
  settlement-date nominal would jump over weekends. `/iss/securities/` shows the nominal of
  the current session's settlement date instead (2026-10-19 for the Saturday session of
  2026-10-17).
- `ACCINT` is `round_half_up(FACEVALUE * 2.5 % * (date - period start) / 365, 0.01)` on
  every row: the accrued interest on the trade date, on that date's nominal.
- Every paid coupon in bondization is `round(N(payment date) * 2.5 % * days / 365, 0.01)`,
  with N from the history on the payment date. Future coupons in bondization (`22.21` and
  so on) are MOEX placeholders: the terms fix the amount only from the nominal of the
  payment date.
- The monthly growth of `FACEVALUE` is the CPI change of month M - 3: within 0.02 pp of
  the CBR m/m figure in every month. Lags 2 and 4 are off by up to 6 pp.
- `YIELDCLOSE` / `DURATION` are the effective annual yield (ACT/365F, at settlement T+1)
  and the Macaulay duration in days of the flows held at the indexed nominal: the
  **real** yield. Core reproduces all four within 0.5 bp of the published 0.01 % and
  within 0.2 days.
- `IRICPICLOSE` and `BEICLOSE` (inflation figures by MOEX) are not identified and are not
  compared.

## Why the face is not reproduced to the kopeck

Minfin indexes on Rosstat's monthly index levels ("к среднегодовым ценам 2000 г.").
Those are published on `rosstat.gov.ru` and `minfin.gov.ru`, and this environment's proxy
refuses both (HTTP 403 on CONNECT, checked 2026-10-17), as it does the legal databases
(`pravo.gov.ru`, `garant.ru`, `consultant.ru`) and `e-disclosure.ru`. The reachable
official CPI, the CBR file above, is month on month to 0.01 pp. Chained, it gives the
face within 2.6e-4 (0.36 RUB at worst, typically a few kopecks to a few tens of kopecks).
Implied monthly growth differs from the rounded CBR figures by up to 0.02 pp, more than
their rounding: the official levels are finer than the m/m figures. With the official
levels from the hub, the same code is expected to match to the kopeck, but that is
**not verified** here.
