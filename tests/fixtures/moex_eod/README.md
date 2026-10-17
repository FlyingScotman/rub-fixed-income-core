# MOEX end-of-day bond golden fixtures (roadmap 0.10, BACKLOG #36)

Raw public MOEX ISS responses, unchanged, fetched directly from `iss.moex.com` on
2026-10-17 at about 11:25 UTC:

| Path | ISS endpoint | Content |
|---|---|---|
| `history/<SECID>_<BOARD>.json` | `/iss/history/engines/stock/markets/bonds/boards/<BOARD>/securities/<SECID>.json?from=2026-10-15&till=2026-10-15&iss.meta=off&iss.only=history` | the EOD row for trade date 2026-10-15 |
| `bondization/<SECID>.json` | `/iss/statistics/engines/stock/markets/bonds/bondization/<SECID>.json?iss.meta=off&limit=unlimited` | coupons (amount and rate), amortizations, offers |
| `trades/RU000A10C5Y0_TQCB.json` | `/iss/engines/stock/markets/bonds/boards/TQCB/securities/RU000A10C5Y0/trades.json?iss.meta=off` | trades of the Saturday session of 2026-10-17 (settlement 2026-10-19), with `trades_yields` |

The KBD publication these use is `tests/fixtures/kbd/zcyc_2026-10-15.json` (EOD) and
`zcyc_2026-10-16.json` (EOD of 2026-10-16 18:54:59, the `ZCYCMOMENT` of the trades), the
latter fetched from `/iss/engines/stock/zcyc.json?iss.only=params,yearyields&date=2026-10-16&iss.meta=off`.
Each history row was also checked to equal the same row in the full board listing
(`/iss/history/engines/stock/markets/bonds/boards/<BOARD>/securities.json?date=2026-10-15`).

## What the EOD fields mean

Established on a sample of about 240 bonds from the full 2026-10-15 boards (all fixed
OFZ-PD, random corporates to maturity, put, call and amortizing bonds, TQOD bonds), not
only on the bonds kept here:

- `ACCINT` is the accrued interest on the **trade date** (2026-10-15). Yields and
  durations are at settlement (T+1, 2026-10-16).
- **Accrual rules differ by board.** OFZ accrue pro rata on the coupon amount (Minfin
  rule: `amount * elapsed / period days`); corporate bonds on the rate
  (`face * rate * days / 365` on the face outstanding at the period start). All 30 fixed
  OFZ-PD match the first rule; 144 of 145 sampled RUB corporates match the second, and 16
  of them would be a kopeck off under the first. The remaining one, RU000A1089U1, accrues
  ACT/360.
  Replacement bonds of USD eurobonds on TQOD accrue 30/360. On TQCB, `ACCINT` of a
  currency bond is in RUB; on TQOD it is in the bond currency.
- `YIELDATWAP` / `YIELDCLOSE` / `DURATION`: effective annual yield ACT/365F and Macaulay
  duration in days, to maturity for bonds without a put. Call options are not assumed
  exercised (all 25 call bonds sampled match to maturity).
- **Put bonds.** `YIELDTOOFFER` is the effective yield to the offer at `CLOSE`, and
  `trades_yields.EFFECTIVEYIELD` (`YIELDDATETYPE` OFFER) the same per trade. Core
  reproduces `YIELDTOOFFER` within 0.06 bp for 50 of 57 sampled put bonds (the other
  seven differ by 2 bp to 8.5 % and are not explained) and every
  `EFFECTIVEYIELD` of the archived trades. `YIELDATWAP` and `trades.YIELD` are a
  different, higher measure that is not identified. `DURATION` is to the offer.
- `GSPREADBP` per trade is the effective yield minus the KBD yield at the duration, in
  whole bp, on the KBD publication named by `ZCYCMOMENT`.
- Some USD bonds are quoted on an ACT/360 time basis (RU000A105RH2), others on ACT/365F
  (RU000A10A877). ISS does not say which; core uses ACT/365F (BACKLOG #36, package 3).

## Bonds

| SECID | Board | Kind | Checked |
|---|---|---|---|
| SU26207RMFS9, SU26218RMFS6, SU26224RMFS4, SU26238RMFS4, SU26252RMFS5 | TQOB | OFZ-PD, 0.3 to 14.6 years | `ACCINT` (Minfin rule), yield at `WAPRICE` and `CLOSE`, `DURATION`; three end to end on the EOD KBD |
| RU000A10AA10, RU000A10F7V9 | TQCB | monthly corporates | `ACCINT` on ACT/365 (pro rata is a kopeck off), yield, duration |
| RU000A1089U1 | TQCB | face 100 000, ACT/360 | `ACCINT`, yield, duration |
| RU000A10B7W1 | TQCB | call bond | `ACCINT`; yield and duration to maturity |
| RU000A10ASC6, RU000A10AC75 | TQCB | amortizers (face 800, 750) | `ACCINT` on the amortised face, yield, duration |
| RU000A10C5Y0, RU000A10G2W7, RU000A0ZYYE3, RU000A1043Z7, RU000A101012 | TQCB | put bonds, 0.1 to 3 years to the offer | `ACCINT`; `YIELDTOOFFER` at `CLOSE`, `DURATION` to the offer; RU000A10C5Y0 per trade: `EFFECTIVEYIELD`, `DURATION`, `GSPREADBP` |
| RU000A10A877, RU000A105RH2 | TQOD | USD replacement bonds | `ACCINT` on 30/360; RU000A10A877 yield and duration; RU000A105RH2's ACT/360 yield is a recorded gap |
