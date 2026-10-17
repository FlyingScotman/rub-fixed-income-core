# Sources for `cny_ruonia_xccy_v1` and `rub_fixed_income.xccy` (package 7)

Retrieved 2026-10-17 (UTC) through the build proxy with normal TLS verification (the
`fs.moex.com` file with the Russian Trusted Root CA bundle, per request). As for
`rub_ruonia_ois_moex_v2`, the documents are not stored in this repository: MOEX
publishes no licence for reuse, and the NFA library marks its files as for registered
users. The table records where each came from and its SHA-256, so the same bytes can be
fetched and checked again. No test reads them; the tests use synthetic market data.

| Document | Retrieved from | Bytes | SHA-256 |
| --- | --- | --- | --- |
| MOEX, «Методика формирования ценовых кривых СПФИ», version of 25 December 2024 (PDF, current revision of the page, valid from 2024-12-26) | page <https://www.moex.com/ru/documents/27002> (`fs.moex.com/files/27002` redirects there), file <https://www.moex.com/files/40aq5d3rfwqb70b3v8kyd77ddv> | 291859 | `09a26a82a78d1d2ef34197678f38e39617a2e03746bf84cecf3837759032d98c` |
| MOEX, the same methodology, version of 24 March 2025 (PDF) | <https://fs.moex.com/f/21666/20250324-ext-spfi-swaprates.pdf> | 288021 | `c29fbeedbb6dc66680cadf33d918e637990f73072ba8b1ae75125ec735ee3bbb` |
| MOEX, «Продуктовые параметры СПФИ (IRFX)» (XLSX, valid from 2026-06-30; "source: NCC OTC contract specifications") | page <https://www.moex.com/ru/documents/produktovye-parametry-sdelok-pfi-s-tsk>, file <https://www.moex.com/files/4cngy26d4y8pw5c193ze0h2gdv> | 21501 | `4c68870b7da9ef638d776ac318f71178df5a7030dd7a056c3a5cd02917a3d973` |
| MOEX, «Описание механизма сделки валютно-процентный своп с ЦК» (PDF, 1 page) | linked as "XCCY/BASIS (XCCY)" on <https://www.moex.com/s942>, file <https://fs.moex.com/f/21354/xccy-itog.pdf> | 197310 | `2fe7a780362b50116d50286cea15ce6962bdf73027c7649c6837d2a1f9531434` |
| NFA / NAUFOR / ARB, «Стандартные условия срочных сделок на процентные ставки, сделок валютно-процентный своп и сделок свопцион» 2011 (PDF) | <https://nfa.ru/upload/iblock/078/StandardTermsOfInterestRateAndSwaptionTransactions2011.pdf> | 656978 | `929a49ac45b7846083a1aca034b92c9fdc77566cddeecc0ae5b628735877eca2` |
| NFA, «Изменения № 1 в Стандартные условия … 2011 г.», 17 August 2021 (PDF) | <https://nfa.ru/upload/iblock/e06/Izmeneniya-_1-v-Standartnye-usloviya-srochnykh-sdelok-na-protsentnye-stavki-i-sd.-svoptsion-2011.pdf> | 848318 | `f9e2c00d6219453b3f8c9c490ff0fbfa07943f294b833c45b001da7def926414` |
| MOEX ISS, SDFI curve listing (JSON) | <https://iss.moex.com/iss/sdfi/curves.json> | 13908 | `0961f920f8d9808c7f60421474b582c16160e6b74cdb10ac971c5c29973a9394` |

## What each document is used for

- **MOEX methodology, Appendix 1, section 3 «Валютно-процентный своп», column
  `XCS CNYRUB`** (primary). Every term of `xcs_cnyrub_convention`:
  - trade date T+0, start TOM, end start + tenor, MODFOL start/end/periods;
  - calendars SPFI, MOSCOW, BEIJING, RUONIA;
  - notional rate «Центральный курс НКЦ для CNY/RUB на Дату сделки», CSA RUB;
  - CNY fixed leg: ≤1Y one period, >1Y annual, Act/Act (ISDA), +1B payment offset,
    Short Initial stub;
  - RUB floating leg: same periods, `RUONIA-OIS-COMPOUND`, fixing offset 0B, Act/Act
    (ISDA), spread «Не указан», no lookback or shift, +1B, Short Initial.

  Clause (1) defines `CNY-XCS-CNYRUO` as the CNY annual rate from the fixed rates of
  XCS deals with a CNY fixed leg against RUB floating on RUONIA (the quote of
  `XccyParQuote`); (2) gives its tenors 1W–5Y; (29) builds `CNY-HYB-CNYRUO` from FX
  swaps up to 9M and XCS from 1Y (the `short_end` option). Section 3 is identical in
  the 24 March 2025 version.
- **MOEX product parameters**, sheet «Валютные деривативы», section 3 (context and
  structure):
  - deliverable XCCY: exchange of notionals, interim interest in each currency,
    reverse exchange;
  - spread in bp, possibly negative;
  - several spread-compounding variants and lookback / observation-shift options;
    these are not modelled;
  - amortization «с указанием Ведущей и Ведомой валюты»;
  - amounts rounded to 2 decimals, rates and FX rates unrounded;
  - maximum 5 years when a leg is in CNY.

  No mark-to-market notional reset is listed.
- **MOEX one-page mechanism**: the initial exchange, interest payments and the final
  exchange "not earlier than the 3rd business day after the trade".
- **NFA standard terms 2011** (primary for the exchange dates):
  - 6.1, the definition;
  - 6.2(а), final payment date = the termination date unless the confirmation says
    otherwise; 6.2(б), initial payment date = the effective date; 6.2(в)–(г),
    intermediate payment dates are payment dates;
  - 6.4(в)–(д), initial, final and intermediate (amortization) amounts;
  - 7.2, fixed amounts; 7.3(а), floating amount = notional × (floating rate + spread)
    × day-count fraction without a compounding condition.
- **NFA amendments No. 1**, 8.4(б): `RUB-RUONIA-OIS-COMPOUND` =
  `(Π(1 + RUONIA_i × n_i / Act) − 1) × Act / d`, `Act` = 365, or 366 if any part of the
  period falls in a leap year (`RUONIA_OIS_COMPOUND`).
- **ISS listing**:
  - `CNY-XCS-CNYRUO` («Кривая вал.-проц. свопов XCS CNYRUB (CNY v RUONIA)», unit
    Fraction, from 2025-01-03);
  - `CNY-HYB-CNYRUO`;
  - NCC's `CNY-DSC-CSARUB` («Дисконтная кривая CNY, CSA=RUB»), the counterpart of the
    bootstrapped curve.

## Not obtained

- **NCC specification «Валютно-процентный своп»** (`nationalclearingcentre.ru/catalog/0204/274`,
  linked from <https://www.moex.com/s942>): the build proxy refuses the host. It would
  confirm the exchange dates; the code takes NFA's defaults as explicit convention
  fields (`initial_exchange_lag_days=0`, `final_exchange_lag_days=0`).
- **Curve values.** `/iss/sdfi/curves/CNY-XCS-CNYRUO.json?date=2026-10-16` and
  `CNY-DFC-CSARUB` answer with an HTML subscriber page. There is no golden for the
  bootstrap.
- **NCC «Методика расчета рисков на рынке СПФИ»**, which defines the CSA=RUB discount
  curves: not retrieved.
