# MOEX OFZ basket futures golden fixtures (package 5)

Raw public MOEX ISS and Bank of Russia responses, unchanged, fetched on 2026-10-17 at
about 14:45 UTC. They cover the December 2021 series (last trading day 2021-12-03,
delivery 2021-12-06) of all five OFZ futures: OFZ2 (`O2Z1`), OFZ4 (`O4Z1`), OFZ6 (`O6Z1`),
OF10 (`OXZ1`) and OF15 (`OVZ1`). MOEX has listed no OFZ basket futures since the June
2022 expiry (none trades on 2022-12-15, 2023-06-15, 2024-06-14 or 2026-10-15), so this is
the most recent series with a full set of published delivery data that predates the
2022 market disruption.

| Path | ISS endpoint | Content |
|---|---|---|
| `forts/history_<SECID>.json` | `/iss/history/engines/futures/markets/forts/securities/<SECID>.json?iss.meta=off&iss.only=history&from=2021-12-02&till=2021-12-03` | settlement prices on 2021-12-02 and the last trading day |
| `forts/description_<SECID>.json` | `/iss/securities/<SECID>.json?iss.meta=off&iss.only=description` | lot 10, quote in RUB per lot, last trading and delivery days |
| `sitenews/38375.json` | `/iss/sitenews/38375.json?iss.meta=off` | "Определены выпуски по умолчанию для поставки по фьючерсам на ОФЗ 6 декабря 2021 года" (2021-12-02 21:28) |
| `sitenews/38431.json` | `/iss/sitenews/38431.json?iss.meta=off` | "6 декабря 2021 года состоится исполнение фьючерсных контрактов на корзину ОФЗ" (2021-12-03 20:11): the basket of every series, the delivery price of every bond and the settlement prices |
| `tqob/<SECID>.json` | `/iss/history/engines/stock/markets/bonds/boards/TQOB/securities/<SECID>.json?from=2021-12-02&till=2021-12-03&iss.meta=off&iss.only=history` | EOD rows of the 25 basket bonds |
| `bondization/<SECID>.json` | `/iss/statistics/engines/stock/markets/bonds/bondization/<SECID>.json?iss.meta=off&limit=unlimited` | coupons and redemption |
| `kbd/zcyc_<DATE>.json` | `/iss/engines/stock/zcyc.json?iss.only=params,yearyields&date=<DATE>&iss.meta=off` | KBD of 2021-12-02 and 2021-12-03 |
| `cbr/KeyRateXML_2021-11-29_2021-12-06.xml` | Bank of Russia `DailyInfo.asmx`, `KeyRateXML` | key rate 7.50 % |

## What the data establish

- **Delivery prices.** Every published delivery price equals `F / 10 * CF / 1000 * 100`
  rounded half up to four decimals (printed with five), `F` the settlement price of the
  last trading day. The specification says "thousandths"; MOEX published four decimals.
- **Conversion factors.** The specification formula with ACT/365F time and one round
  yield per series (OFZ2 6.8 %, OFZ4 6.6 %, OFZ6 7.0 %, OF10 7.1 %, OF15 8.7 %) gives 19
  of the 25 published factors exactly and the other six (OF10 / OF15) within two units
  of the fourth decimal. The yields are "set by decision of the exchange"; these values
  are inferred from the published prices, not quoted from a decision.
- **Default delivery issue.** The specification (item 4.4) picks the lowest closing
  price on the trading day before the last trading day divided by the conversion factor.
  With `LEGALCLOSEPRICE` (MOEX's closing price) of 2021-12-02 this reproduces all five
  published default issues; with `CLOSE` (the last trade) it would not (OFZ6, OF15).
- **Settlement price.** On the last trading day spot settles on the delivery day, so
  `min(10 * closing price / CF)` is the no-carry theoretical price. It is within 0.07 %
  of the settlement price for OFZ2 and OF15 and up to 0.66 % (OF10) for the others: the
  settlement price comes from the evening clearing, after the bond close, and OFZ fell
  that evening (the OF10 cheapest bond, OFZ 26235, closed at 84.98 % official, was
  delivered at 84.27 % and closed at 84.26 % on 2021-12-06).

## Specification (not committed; cited)

"Спецификация фьючерсных контрактов на корзину облигаций федерального займа", approved
by the MOEX Management Board on 24 April 2015 (minutes No. 21), in force 15 November 2015
to 27 February 2023: `https://www.moex.com/ru/documents/6381`, file
`https://www.moex.com/files/49a2tbeyfc91tyk0vwyzkt01nw` (fetched 2026-10-17).
