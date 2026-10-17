# MOEX bond golden fixtures (roadmap 0.10)

These are raw public MOEX ISS responses, copied unchanged from the market-data hub repository
`FiveLimaGolf/market-data-hub`, `tests/fixtures/bonds_static/` at commit `c7a9d44`
(16 October 2026).

| File | Source file | Content |
|---|---|---|
| `market_2026-10-16.json` | `market.json` | `securities` + `marketdata` blocks for 14 board rows, `dataversion` trade date 2026-10-16 about 10:33 (intraday), `SETTLEDATE` 2026-10-19 |
| `bondization/<SECID>.json` | `bondization/<SECID>.json` | coupons, amortizations and offers per bond |

`tests/test_moex_goldens.py` checks core against the exchange-published values:

| SECID | Kind | What is checked |
|---|---|---|
| SU26238RMFS4 | OFZ-PD, bullet | `ACCRUEDINT`. Effective yield at `WAPRICE`, `LAST` and `PREVWAPRICE`, each within 0.5 bp of MOEX's rounding to 0.01 %. `DURATION` within 1 day. Also checked end to end through `value_portfolio` on the KBD curve. |
| RU000A0JS4Z7 | corporate, put offer 2027-08-26 | `ACCRUEDINT`; `DURATION` to the offer within 1 day |
| RU000A0JWMZ1 | corporate, no maturity date | `ACCRUEDINT` |
| RU000A0JR4U9 | amortizer, current coupon known | `ACCRUEDINT` on the amortised face (375) |
| RU000A0JVY04 | fixed coupon, current coupon known | `ACCRUEDINT` |
| SU29010RMFS4 | OFZ-PK, current coupon known | `ACCRUEDINT` |
| RU000A10DQB6 | CNY bond, board TQOY | `ACCRUEDINT` in the bond's currency |

Known differences, kept as tests of the gap rather than hidden:

- **RU000A0JXTS9 (USD bond, board TQOD).** MOEX's `ACCRUEDINT` 2738.89 is 30/360
  (116 of 180 days of a 4250 coupon). Core's split accrual is actual/actual within the
  period and gives 2740.44 (BACKLOG #36).
- **RU000A0JS4Z7 yield to offer.** `YIELDATPREVWAPRICE` is 9.51 % at 107.93. Core gives
  9.30–9.39 % on the same flows (coupons to 2027-08-24, the offer at 100 on 2027-08-26),
  depending on the settlement assumed. The duration still matches. The MOEX method for
  short put bonds is not yet identified (BACKLOG #36).

The data is intraday. `YIELD` and `DURATION` follow live prices, and many bonds show 0
for them, so only consistent price/yield pairs are used. Floaters with unknown current
coupons (SU29025, RU000A10A356), linkers (SU52002, which needs an index ratio) and
RU000A0NNH09 (not in `market.json`) are not used.
