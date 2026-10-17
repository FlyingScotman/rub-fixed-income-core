# MOEX short-rate futures golden fixtures (package 5)

Raw public responses, unchanged, fetched on 2026-10-17 between 14:30 and 14:40 UTC.

## Bank of Russia (`cbr/`)

SOAP 1.1 responses of `https://www.cbr.ru/DailyInfoWebServ/DailyInfo.asmx` (method,
`fromDate`, `ToDate`):

| File | Method | Content |
|---|---|---|
| `RuoniaXML_2025-11-01_2026-10-16.xml` | `RuoniaXML` | RUONIA (`ruo`, % a year) by rate date `D0`, volume, update time |
| `RuoniaSVXML_2026-05-01_2026-10-16.xml` | `RuoniaSVXML` | RUONIA index (`RUONIA_Index`) and term RUONIA for every calendar day |
| `RuoniaSVXML_2025-12-01_2026-01-31.xml` | `RuoniaSVXML` | the same across the year end |
| `KeyRateXML_2026-01-01_2026-10-16.xml` | `KeyRateXML` | key rate in force by date |

Facts used by the tests:

- RUONIA is published for every business day of the Russian production calendar
  except 2025-12-30, the last business day of 2025 (no RUONIA is calculated for the last
  business day of a year). 2025-11-01 was a working Saturday and has a fixing.
- The RUONIA index follows the Bank of Russia methodology of 12 April 2022 (order
  OD-731, `https://www.cbr.ru/Content/Document/File/135651/Meth_ruonia_20220429.pdf`):
  `I_t = I_T (1 + RUONIA_T (d/366 + (t - T - d)/365))`, `T` the previous day with a
  RUONIA. Core reproduces every published value from 2026-05-04 to 2026-10-16 and across
  the 2025-12-30 gap to 2e-15 relative.

## MOEX ISS (`iss/`)

| Files | Endpoint |
|---|---|
| `history_<SECID>_<DATE>.json` | `/iss/history/engines/futures/markets/forts/securities/<SECID>.json?iss.meta=off&iss.only=history&from=<DATE>&till=<DATE>` |
| `description_<SECID>.json` | `/iss/securities/<SECID>.json?iss.meta=off&iss.only=description` |
| `history_KEYRATE_2026-08-24_2026-10-16.json` | `/iss/history/engines/stock/markets/index/securities/KEYRATE.json?iss.meta=off&iss.only=history&from=2026-08-24&till=2026-10-16` |

Contracts:

- **RUON** (one-month RUONIA, `RR*`): the final-settlement rows of RRZ5 (2025-12-30)
  to RRU6 (2026-09-30), each on its last trading day. Nine of ten are reproduced exactly
  from the fixings by the specification formula. RRK6 (May 2026, no open interest)
  settled at 85.79 against 85.82 by the formula; no settlement window or carry rule
  reproduces it while keeping the other months exact. It is recorded as an outlier.
  The specification does not state a rounding of the final price; ISS shows it rounded
  half up to the 0.01 price step (84.985 -> 84.99 for RRH6).
- **RUONIA** index futures (`RF*`): RFU6 final settlement (2026-09-01, 4.4697 = the
  index of that day rounded to four decimals); live RFZ6, RFH7, RFM7, RFU7 on 2026-10-16.
- **KEYRATE** futures (`KK*`): KKV6, KKZ6, KKG7 on 2026-10-16; none has expired yet
  (the first expiry is the meeting of 2026-10-23). Their last trading days are the
  scheduled Bank of Russia meetings. The KEYRATE index (MOEX, from 2026-08-24) is 14.00
  every day of the fixture, the key rate in force.

The last trading days in the descriptions (`LSTTRADE`) are the ones the tests derive from
the calendar.

## Specifications (not committed; cited)

- "Спецификация фьючерсных контрактов на процентные ставки", order MB-P-2026-843 of
  10 March 2026, in force from 23 March 2026:
  `https://www.moex.com/ru/documents/fyuchersnyj-kontrakt-na-protsentnye-stavki`
  (file `/files/4z9jjch6p40ztxwn2dx6jdyy53`). The settlement month and price formula are
  the same in the RUONIA futures specification of 20 June 2022 (order MB-P-2022-1210,
  `https://www.moex.com/ru/documents/3292`), which governed RRZ5..RRG6.
- "Спецификация фьючерсных контрактов на индексы долгового и денежного рынка", order
  MB-P-2026-3630 of 8 September 2026, in force from 14 September 2026:
  `https://www.moex.com/ru/documents/fyuchersnye-kontrakty-na-indeksy-dolgovogo-i-denezhnogo-rynka`
  (file `/files/4hvvjj2cs2ct2r5xmws3jf8jns`; the previous edition from 19 May 2026,
  `/files/4ezh2awnts7t4tyf62nntw25d6`, has the same RUONIA terms and governed RFU6).
- "Методика расчета Индекса МосБиржи Ключевой ставки", approved 13 August 2026:
  `https://www.moex.com/ru/documents/metodika-rascheta-indeksa-mosbirzhi-klyuchevoj-stavki`
  (file `/files/4bd25q737h1wt19z240q0116m8`).
