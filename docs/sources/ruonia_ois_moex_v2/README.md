# Sources for `rub_ruonia_ois_moex_v2` (BACKLOG #37)

Retrieved 2026-10-17 (UTC) through the build proxy with normal TLS verification. The
documents are not stored in this repository: MOEX publishes no licence for reuse, and the
NFA library marks its files as for registered users. The table records where each one
came from and its SHA-256, so the same bytes can be fetched and checked again.

| Document | Retrieved from | Bytes | SHA-256 |
| --- | --- | --- | --- |
| MOEX, «Методика формирования ценовых кривых СПФИ», version of 25 December 2024 (PDF, 9 pages) | page <https://www.moex.com/ru/documents/27002>, file <https://www.moex.com/files/40aq5d3rfwqb70b3v8kyd77ddv> | 291859 | `09a26a82a78d1d2ef34197678f38e39617a2e03746bf84cecf3837759032d98c` |
| NFA, «Изменения № 1 в Стандартные условия срочных сделок на процентные ставки и сделок свопцион 2011 г.», published 17 August 2021 (PDF, 52 pages) | <https://nfa.ru/upload/iblock/e06/Izmeneniya-_1-v-Standartnye-usloviya-srochnykh-sdelok-na-protsentnye-stavki-i-sd.-svoptsion-2011.pdf>, listed on <https://nfa.ru/councils_and_committees/rspfi/> | 848318 | `f9e2c00d6219453b3f8c9c490ff0fbfa07943f294b833c45b001da7def926414` |
| NFA / NAUFOR / ARB, «Стандартные условия срочных сделок на процентные ставки, сделок валютно-процентный своп и сделок свопцион» 2011 (PDF, 43 pages) | <https://nfa.ru/upload/iblock/078/StandardTermsOfInterestRateAndSwaptionTransactions2011.pdf>, same listing | 656978 | `929a49ac45b7846083a1aca034b92c9fdc77566cddeecc0ae5b628735877eca2` |
| MOEX, «Продуктовые параметры сделок ПФИ с ЦК» (XLSX; "source: NCC OTC contract specifications") | page <https://www.moex.com/ru/documents/produktovye-parametry-sdelok-pfi-s-tsk>, file <https://www.moex.com/files/4cngy26d4y8pw5c193ze0h2gdv> | 21501 | `4c68870b7da9ef638d776ac318f71178df5a7030dd7a056c3a5cd02917a3d973` |
| NFA, ROISfix document package (ZIP; methodology and test-trade rules of 23 April 2021) | <https://nfa.ru/upload/iblock/e36/Roisfix.zip>, listed on <https://nfa.ru/library/?topic=ROISfix> | 1664917 | `d80a0ca6f836ceffca4faf0ac91a9ae2fe1243c110c04e82632925486b604f4e` |

## What each document is used for

- **MOEX methodology** (primary): clause (2), the RUB-OIS-RUONIA tenor set; Appendix 1
  «Описание спецификаций», column «OIS RUONIA», every swap term of the spec (start TOM,
  MODFOL, calendars SPFI/MOSCOW/RUONIA, ≤1Y single period and annual beyond, Act/Act
  ISDA on both legs, +1B payment offset on both legs, Short Initial stub, index
  RUONIA-OIS-COMPOUND, fixing offset 0B, no lookback or shift). The document states it is
  informational («носит ознакомительный характер»).
- **NFA amendments No. 1** (primary): 8.4(б), the RUB-RUONIA-OIS-COMPOUND formula
  `(Π(1 + RUONIA_i × n_i / Act) − 1) × Act / d`, `Act` = 365, or 366 if any part of the
  period falls in a leap year; 7.8(д), a lookback period only when agreed.
- **NFA standard terms 2011** (primary): 2.2, interest periods (start inclusive, end
  exclusive); 7.2 and 7.3(а), amounts = notional × rate × day-count fraction; 7.4(е),
  Фактическое/Фактическое (Act/Act ISDA).
- **MOEX product parameters** (context only): the range of terms an NCC-cleared OIS may
  take (payment offset +1 or +2, several day counts, optional lookback or observation
  shift, maximum 10 years); it names the governing document, the NCC «Спецификация
  внебиржевых договоров «Процентный своп»», which was not reachable.
- **ROISfix package** (context only): the test-trade rules (1.8) say the terms of ROISfix
  test trades are set by the NCC specification of OTC RUONIA swaps. ROISfix tenors are
  1W–1Y; no conventions are taken from it.

## Not obtained

- NCC (НКО НКЦ (АО)) specification of OTC interest-rate swaps: `nationalclearingcentre.ru`
  and `nkcbank.ru` are refused by the build proxy.
- RUB-OIS-RUONIA curve values: ISS lists the curve at `/iss/sdfi/curves` (free; SHA-256 of
  the 2026-10-17 listing `0961f920f8d9808c7f60421474b582c16160e6b74cdb10ac971c5c29973a9394`),
  but `/iss/sdfi/curves/RUB-OIS-RUONIA.json?date=…` answers with "The information is
  available only to subscribers" for current and historical dates. No quote snapshot is
  stored; the tests use synthetic quotes.
- `fs.moex.com` was not used: the MOEX methodology came from its `www.moex.com/files/…`
  link.
