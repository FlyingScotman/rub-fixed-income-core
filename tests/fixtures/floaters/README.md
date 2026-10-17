# Floater coupon golden fixtures (roadmap package 1)

Raw public responses, unchanged, fetched on 2026-10-17 at about 13:56 UTC directly from
`www.cbr.ru` and `iss.moex.com`. Used by `tests/test_moex_floater_goldens.py`, which
parses them on the test side (the library does no I/O).

## CBR fixings (`cbr/`)

SOAP 1.1 responses of the Bank of Russia DailyInfo web service
`POST https://www.cbr.ru/DailyInfoWebServ/DailyInfo.asmx` with
`Content-Type: text/xml; charset=utf-8`, `SOAPAction: "http://web.cbr.ru/<Method>"` and
the body

```xml
<?xml version="1.0" encoding="utf-8"?><soap:Envelope xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xmlns:xsd="http://www.w3.org/2001/XMLSchema" xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/"><soap:Body><Method xmlns="http://web.cbr.ru/"><fromDate>FROM</fromDate><ToDate>TO</ToDate></Method></soap:Body></soap:Envelope>
```

| File | Method, FROM, TO | Content | sha256 |
|---|---|---|---|
| `ruonia_2023-11-01_2026-10-16.xml` | `RuoniaXML`, 2023-11-01, 2026-10-16 | RUONIA by rate date `D0` (`ruo`, % p.a.), volume, publication time `DateUpdate` (Moscow); 728 rows, 2023-11-01 to 2026-10-15 (the 2026-10-16 fixing is published on 2026-10-19) | `045927d6…` |
| `keyrate_2023-11-01_2026-10-16.xml` | `KeyRateXML`, 2023-11-01, 2026-10-16 | key rate in force by date `DT` (`Rate`, %); 754 rows | `da8a3d29…` |
| `ruonia_sv_2025-06-01_2026-10-16.xml` | `RuoniaSVXML`, 2025-06-01, 2026-10-16 | RUONIA index and term RUONIA 1M/3M/6M (`RUONIA_AVG_3M`, %) for every calendar day | `29b137b4…` |

Cross-check: on the whole overlap the RUONIA and key-rate values equal the HTML tables
`https://www.cbr.ru/hd_base/ruonia/dynamics/?UniDbQuery.Posted=True&UniDbQuery.From=…&UniDbQuery.To=…`
and `https://www.cbr.ru/hd_base/KeyRate/?UniDbQuery.Posted=True&UniDbQuery.From=…&UniDbQuery.To=…`
(fetched 2026-10-17 for 2020-01-01 to 2026-10-17, not committed).

The tests take the business calendar from the RUONIA rate dates: weekdays without RUONIA
are holidays, weekend days with RUONIA are working Saturdays. This matches the official
calendar except on the last business day of each year (2023-12-29, 2024-12-28, a
working Saturday, and 2025-12-30): CBR computes no RUONIA on it, and the coupons carry the
previous fixing over it.

## MOEX ISS (`iss/`)

| Path | ISS endpoint | Content |
|---|---|---|
| `bondization/<SECID>.json` | `/iss/statistics/engines/stock/markets/bonds/bondization/<SECID>.json?iss.meta=off&iss.only=coupons&limit=unlimited&from=2023-11-01&till=2027-06-30` | coupon periods with paying dates from 2023-11-01 to 2027-06-30: `startdate`, `coupondate`, `facevalue`, `value` (RUB), `valueprc` |
| `description/<SECID>.json` | `/iss/securities/<SECID>.json?iss.meta=off&iss.only=description&description.columns=name,value` | `COUPON_BENCHMARK`, `COUPON_BENCHMARK_SPREAD` (margin, %), `COUPON_DETAILS`, dates |
| `board_TQOB.json`, `board_TQCB.json` | `/iss/engines/stock/markets/bonds/boards/<BOARD>/securities.json?iss.meta=off&iss.only=securities&securities=<SECIDs>&securities.columns=SECID,BOARDID,FACEVALUE,COUPONPERIOD,NEXTCOUPON,COUPONVALUE,COUPONPERCENT` | the running coupon as the board shows it (0 when not fixed yet) |

The request parameters select the blocks, columns, securities and dates; the responses
themselves are not edited. sha256: `board_TQOB.json` `dfd943ad…`, `board_TQCB.json`
`d787b7f7…`.

Notes on the ISS fields, established on these bonds:

- `value` is the coupon in RUB to the kopeck. For in-arrears coupons `valueprc` is the
  amount's rate on 365 days (SU29013 2026-06-24: 34.99 → 14.03 %, while the formula's rate
  is 14.0356 %). For in-advance coupons it is the coupon rate itself.
- Bondization repeats the current in-advance rate in every future coupon row (OFZ-PK
  29007–29010): only coupons that have started are fixed.
- A coupon fixed for the running period can appear on the board before bondization
  (RU000A1042W6 shows 38.67 for 2026-10-21; its bondization row is still empty).

## Bonds

| SECID | Issuer / kind | ISS benchmark, margin | Rule found |
|---|---|---|---|
| SU29013RMFS8, SU29015RMFS3, SU29017RMFS9, SU29022RMFS9, SU29025RMFS2 | OFZ-PK, quarterly | RUONIA, 0 | arithmetic RUONIA over the period shifted 6 days, ACT/ACT on the window |
| SU29007RMFS0, SU29008RMFS8, SU29009RMFS6, SU29010RMFS4 | OFZ-PK, semiannual, in advance | RUONIA, +1.30 to +1.60 | arithmetic RUONIA over 6 months to the 2nd business day before the start, rate rounded to 0.01 %, amount on days/365 |
| SU29026RMFS0, SU29027RMFS8, SU29028RMFS6 | OFZ-PK, quarterly | RUONIA3M ("Срочная ставка RUONIA 3m"), 0 | compounded RUONIA over the period shifted 7 days; first coupons of the December 2024 issues not reproduced |
| RU000A1093L2, RU000A10ACF3, RU000A1042W6 | VEB.RF | RUONIA, 0 / +2.00 / +1.55 | as OFZ-PK 29013+ plus margin |
| RU000A10ABH1 | VTB | RUONIA, +2.80 | same |
| RU000A106K43, RU000A108447 | Russian Railways, Rosagroleasing, monthly | RUONIA, +1.30 / +1.80 | in advance: the same-length window ending 6 days before the start, rate rounded to 0.01 % |
| RU000A10B8D9, RU000A10C6S0 | VEB.RF | RUONIA_IND ("Индекс RUONIA"), +2.30 / +1.75 | compounded RUONIA over the period shifted 7 days |
| RU000A10A4S7, RU000A109JY6 | key-rate floaters | RREFKEYR, +2.00 / +2.50 | arithmetic key rate over the period shifted 6 days |
| RU000A10C0V7 | key-rate floater | RREFKEYR, +3.00 | shifted 4 days |
| RU000A10EDW8, RU000A1079S6 | key-rate floaters | RREFKEYR, +1.35 / +1.20 | each day observes the key rate 4 business days earlier (native `lookback_business_days=4`) |
