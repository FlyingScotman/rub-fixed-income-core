# MOEX end-of-day golden fixtures, trade date 2026-10-20 (desk-monitor review, 0.33.0)

Raw public responses, unchanged, fetched on 2026-10-21 at about 14:40 UTC directly from
`iss.moex.com` and `www.cbr.ru`. Used by `tests/test_moex_eod_2026_10_20_goldens.py`, which
parses them on the test side (the library does no I/O). The bonds are a subset of the
312-bond stratified sample and the 466-floater universe checked in
`docs/reviews/CORE_REVIEW_FOR_DESK_MONITOR.md` §3.

| Path | Source | Content |
|---|---|---|
| `history/<SECID>_<BOARD>.json` | `/iss/history/engines/stock/markets/bonds/boards/<BOARD>/securities/<SECID>.json?from=2026-10-20&till=2026-10-20&iss.meta=off&iss.only=history` | the EOD row of trade date 2026-10-20 (`ACCINT`, `CLOSE`, `WAPRICE`, `YIELDCLOSE`, `YIELDATWAP`, `YIELDTOOFFER`, `DURATION`, `FACEVALUE`) |
| `bondization/<SECID>.json` | `/iss/statistics/engines/stock/markets/bonds/bondization/<SECID>.json?iss.meta=off&limit=unlimited&from=2026-06-01` | coupons paid from 2026-06-01 (`startdate`, `coupondate`, `facevalue`, `value`, `valueprc`), amortizations, offers with their exercise windows (`offerdatestart`, `offerdateend`); `from` filters on the payment date |
| `description/<SECID>.json` (floaters) | `/iss/securities/<SECID>.json?iss.meta=off&iss.only=description&description.columns=name,value` | `COUPON_BENCHMARK`, `COUPON_BENCHMARK_SPREAD` |
| `kbd/zcyc_2026-10-20.json` | `/iss/engines/stock/zcyc.json?iss.only=params,yearyields&date=2026-10-20&iss.meta=off` | the EOD KBD publication, 2026-10-20 18:54:55; SHA-256 `92a17e7b10f7273a07198488b7f4a97fe61d10b5349658b5a0afd9b73a9853a8` |
| `marketdata_yields_TQCB.json` | `/iss/engines/stock/markets/bonds/boards/TQCB/securities.json?iss.meta=off&iss.only=marketdata_yields&securities=<24 SECIDs>` | live rows of 24 RUB fixed-coupon corporates, 23 still on the 2026-10-20 EOD KBD (`ZCYCMOMENT` 2026-10-20 18:54:55): `EFFECTIVEYIELD`, `DURATION`, `GSPREADBP`; SHA-256 `6098aaf3fdd83edf41d3e8a9ec151f4c59ed630630d448287d8906fde787c5c3` |
| `cbr/RuoniaXML.xml`, `cbr/KeyRateXML.xml` | CBR DailyInfo SOAP (`https://www.cbr.ru/DailyInfoWebServ/DailyInfo.asmx`), methods `RuoniaXML` / `KeyRateXML`, `fromDate` 2025-12-01, `ToDate` 2026-10-21 (request body as in `tests/fixtures/floaters/README.md`) | RUONIA by rate date with publication time, 2025-12-01 to 2026-10-20; the key rate in force by date. SHA-256 `903a82e6…` (RUONIA), `d5919ab1…` (key rate) |

## Bonds

| Kind | SECIDs | Checked |
|---|---|---|
| OFZ-PD, OFZ-AD (amortizer) | SU26207RMFS9, SU26230RMFS1, SU26238RMFS4, SU26248RMFS3, SU46012RMFS9 | `ACCINT`, `YIELDCLOSE`, `YIELDATWAP`, `DURATION`; end to end through `value_portfolio` (trade-date and T+1 settlement, G-spread to the KBD) |
| Corporate bullets, amortizers, calls | RU000A107PU5, RU000A10C6W2, RU000A0JXN21, RU000A10FG76, RU000A0JX199, RU000A10FBR9, RU000A10DPT0, RU000A10BLP1, RU000A10EXU0 | the same; calls are not assumed exercised (MOEX yields to maturity) |
| Puts | RU000A0JT6B2, RU000A10BZT3 (open exercise windows), RU000A100Q50 (no window); RU000A10CJ84 (two offers a week apart) | `YIELDTOOFFER` at `CLOSE` (4 decimals) and `DURATION` with `horizon="open_put"` |
| OFZ-PK | SU29007RMFS0, SU29008RMFS8 (in advance), SU29013RMFS8, SU29015RMFS3 (RUONIA in arrears), SU29026RMFS0 (term RUONIA 3M), SU29031RMFS0 (first coupon: known gap) | `ACCINT` by `FloatingLeg.accrued_interest` |
| Corporate floaters | RU000A105MP6, RU000A107DS5, RU000A10AHM8 (key rate, running coupon not published), RU000A1038M5, RU000A108DE3 (published, accrued on the rate), RU000A107050, RU000A107XA1 (RUONIA), RU000A109R19 (known gap) | `ACCINT` |

## What the fields mean (established on the full samples, not only these bonds)

- `DURATION` is the Macaulay duration in days at the **published** `YIELDATWAP` (the
  weighted-average price, rounded to 0.01 %), rounded to a day: 155 of 155 bonds to
  maturity. It is not the duration at `CLOSE` (up to 22 days apart). Put bonds: at the
  published `YIELDTOOFFER`.
- `GSPREADBP` is the effective yield minus the KBD yield at the duration in years, on the
  KBD of `ZCYCMOMENT`, rounded to a bp (552 of 564 rows on the 2026-10-20 EOD KBD; the 12
  others are currency bonds, floaters and two amortizers, within 3 bp).
- `ZSPREAD` / `ZSPREADATWAPRICE` in the history are not identified (not bp; not the
  annual or continuous Z-spread to the KBD); they are not compared.
- On `TQOD` the settlement currency is USD, so `ACCINT` of a CNY or EUR bond is in USD
  (a constant ratio to the accrued in the bond currency), not in the face currency.
- Floater `ACCINT`: a coupon fixed in advance accrues pro rata on its amount (OFZ-PK) or
  on its rate (corporates). A coupon set in arrears accrues the realised formula over the
  elapsed days, and keeps doing so after MOEX has published the full coupon: on the 13
  bonds where pro rata on the published amount is a kopeck off, the realised formula is
  exact. Pro rata stays the default for a published coupon, because the family formula
  is not every bond's (BACKLOG #84): with it, 157 of 170 published corporate coupons are
  exact, with the formula 155.
