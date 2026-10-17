# Sources for `rub_fixed_income.repo` (package 6)

Retrieved 2026-10-17 (UTC) through the build proxy with normal TLS verification
(`fs.moex.com` files with the Russian Trusted Root CA bundle, per request). As for the
other packages, the documents are not stored in this repository: MOEX publishes no
licence for reuse, and the NFA library is for its members. The table records where
each came from and its SHA-256, so the same bytes can be fetched and checked again.
The only committed data is the raw ISS RUSFAR history in `tests/fixtures/moex_repo/`.

| Document | Retrieved from | Bytes | SHA-256 |
| --- | --- | --- | --- |
| MOEX, «Правила проведения торгов на фондовом рынке, рынке депозитов и рынке кредитов ПАО Московская Биржа. Часть III. Секция рынка РЕПО», approved by the Supervisory Board on 10 August 2026 (Protocol No. 3), current version of the page (PDF, 34 pages) | page <https://www.moex.com/ru/documents/17020>, file <https://www.moex.com/files/4ykjcbad5mqnk2e6p9ggzc29mh> | 492415 | `0e4c73fc93b131649328f1e2b1fe0df33bac8bbf7b59d0299349a5d63f1304af` |
| MOEX, «Методология расчета индикатора RUSFAR», approved 13 November 2025 (Protocol No. 84), in force from 19.01.2026 (DOCX) | page <https://www.moex.com/ru/documents/metodologiyu-rascheta-indikatora-rusfar>, file <https://www.moex.com/files/4jdzyv9zz2dqzrc2wg2n7tfaw3> | 84117 | `55b47b6da0054a582bea05b2a3f360509a8d30ed1d6df43694f11b7075724cc4` |
| MOEX, «Методика расчета индикаторов ставки РЕПО с ЦК», approved 14 August 2025 (Protocol No. 63) (DOCX) | page <https://www.moex.com/ru/documents/8804>, file <https://www.moex.com/files/4q2js3xw8ard51ntf7wtbynm58> | 92363 | `c4fb930660b86565e085a8b779ceece5c810c4f417d4223f1238f4795288bd9d` |
| MOEX, «РЕПО с КСУ с автоматической капитализацией процентов», 10 September 2026 (PDF presentation) | <https://www.moex.com/media/repo-s-kompaundingom-i-od.pdf> | 825385 | `74abd10ea3f1baa7cc71c26f6da14275261801a91036ce80437935c34c64d298` |
| MOEX, «Инструменты с плавающими ставками на денежном рынке», 28.08.2023 (PDF presentation) | linked on <https://www.moex.com/msn/float>, file <https://fs.moex.com/f/18688/plavajushie-stavki.pdf> | 2477959 | `2ede7400f9538f302c8697c42db266a0b87d31ad95c0c22f20c45cfbdde65a95` |
| MOEX, «РЕПО с плавающими ставками в EQM06», 21.11.2023 (PDF presentation) | linked on <https://www.moex.com/msn/float>, file <https://fs.moex.com/f/19175/repo-s-plavajushimi-stavkami-v-eqm06-1.pdf> | 1705714 | `22c8b0c67fb8405a082645718cf32b77ab930be608afd7b61542331742743e43` |
| MOEX, «РЕПО с открытой датой», 25.09.2023 (PDF presentation) | linked on <https://www.moex.com/msn/float>, file <https://fs.moex.com/f/18689/repo-c-otkrytoy-datoy.pdf> | 870944 | `c53e75dd582b65db57dc1f7104eebdb5a0cbc507cd32d1b8e51517493cd31f59` |
| MOEX, «Новый клиринговый отчет EQM64 и доработанный EQM44 для сделок РЕПО с плавающими ставками», 16.04.2024 (PDF) | linked on <https://www.moex.com/msn/float>, file <https://fs.moex.com/f/19997/eqm64-eqm44.pdf> | 323815 | `c3a97d07af71da777d0cc91bf89a7a3d4b686fad6a6faf16d4ece7aec495201d` |
| NFA / NAUFOR / Russian Repo Council, «Примерные условия договоров РЕПО на российском финансовом рынке (3-я редакция)», 2024 (PDF) | page <https://nfa.ru/councils_and_committees/repodocs/>, file <https://nfa.ru/upload/iblock/f1c/lzthmb468qm1ndgfmkj794dmhici6rb1/Primernye-usloviya-dogovorov-REPO-na-rossiyskom-finansovom-rynke-_-3_ya-redaktsiya_.pdf> | 976824 | `d9b3732d3e7220c8e9d6e9b6caa799d59509a520956a96d6ae5a34eb0e225c1b` |
| NFA, «Изменения № 1 в Примерные условия договоров РЕПО … (3-я редакция)» (PDF) | <https://nfa.ru/upload/iblock/e5f/1giruir622sjjzyqrzlll63joc4q4k41/Izmeneniya-_-1-v-Primernye-usloviya-dogovorov-REPO-na-rossiyskom-finansovom-rynke-_-3_ya-redaktsiya_1_.pdf> | 225938 | `e1f70220017b3032e0854d270c6e6a2e82839a56c811ebf5fb7e7eaed2c92f98` |
| MOEX ISS, boards of the market «РЕПО с ЦК с КСУ» (JSON) | <https://iss.moex.com/iss/engines/stock/markets/gcc/boards.json?iss.meta=off> | 6246 | `719a3d6d5f7ec2f1baa4a202488ab998e2926db85eeac0dcdfd7406f068e9c43` |

## What each document is used for

- **MOEX Trading Rules, Part III** (primary, the exchange's own rules).
  - 1.1.1.10 Срок сделки РЕПО (calendar days from the first leg's settlement),
    1.1.1.11 Ставка РЕПО (positive, negative or zero; a floating rate is an indicative
    rate plus a deviation), 1.1.1.13 Сумма РЕПО.
  - 1.5.8.2: `S = (1 - Dn / 100) * Q * (P0 + a0) * e0 / r0`, `S` to the hundredth,
    `Q` rounded up, `a0` the accrued at the first leg; `Dn = (1 - S / (Q (P0 + a0) e0 / r0)) * 100`.
    `repo_start_amount` / `repo_initial_discount_pct` (RUB: `e0 = r0 = 1`). For KSU
    (GC pool certificates) the price is the nominal value.
  - 1.5.8.5 (and 1.7.4.6 for repo with the CCP; 1.7.4.1 gives its amount formula):
    `S2 = S1 * (1 + R / 100 * (T365 / 365 + T366 / 366))`, `T365` / `T366` the days
    between the two legs in 365- and 366-day years: `FixedRepoRate` on
    `RepoDayBasis.ACT_365_366`, `repo_end_amount`. 1.5.8.6: the repurchase amount may
    change in the cases of the Clearing Rules (not reachable, see below).
- **NFA Примерные условия (3-я редакция), 2.1 definitions.**
  - «База начисления процентов – для сделок в российских рублях – фактическое количество
    дней в году (365 или 366), для сделок в иностранных валютах – 360 дней, кроме
    случаев, когда стороны … договорились об ином»: the default bases, always an
    explicit `RepoDayBasis` in core.
  - «Срок РЕПО – период в календарных днях, исчисляемый со дня, следующего за Датой
    исполнения первой части … по Дату исполнения второй части … включительно»: interest
    days are `(start, end]`, which fixes how `T365` / `T366` split a year boundary.
  - «Начальный коэффициент обеспечения» (purchase amount over the securities' current
    value at the trade), «Переоценка» (margin calls by agreement, section 8), «Договор
    РЕПО с открытой датой»; section 10 (income on the securities passes to the seller,
    or reduces the repurchase amount, 10.2): `RepoCollateral`, `RepoCashAdjustment`.
- **MOEX «Инструменты с плавающими ставками», slides 5–8** (floating repo rules).
  - Benchmarks RUSFAR (ON, 1W, 2W, 1M, 3M), RUSFARCNY, RUONIA, the key rate.
  - «Значение индикатора применяется за предыдущий Расчетный день для RUSFAR и RUONIA,
    а для Ключевой ставки БР за текущий»; «если в текущем дне расчет значения индикатора
    не производится, его значение принимается равным последнему известному значению»;
    «в случае если сумма индикатора и спреда меньше или равна нулю, то ставка РЕПО с
    КСУ принимается равной 0.01%. Для РЕПО с ЦК и РЕПО-М таких ограничений нет».
  - Slide 6, overnight indicators and the key rate:
    `Сумма 2 = Сумма 1 * (1 + sum_{i=T1+1}^{T2} (r_i + спред) / (N_i * 100))`, `N_i` the
    days of the calendar year of day `i`. Term indicators (1W…3M) reset once per
    indicator term (`K` periods): not implemented.
- **MOEX «РЕПО с плавающими ставками в EQM06», Пример 1** (golden): a 7-day key-rate
  CCP repo with every day's known and projected interest; confirms the per-calendar-day
  key rate (13 % on Saturday and Sunday, 17 % from Monday) and a single rounding of the
  total (6 468 388,60 = round(S1 + 9 330,3256 + 9 118,2704)).
- **MOEX «РЕПО с КСУ с автоматической капитализацией процентов»** (golden): the
  compounding open repo (FRPO RUSFAR, FKPO key rate), clause 21.12.2 of the NCC Clearing
  Rules part II quoted on slide 3: interest credited on each settlement day with
  `Y_j` days in the year, simple over non-settlement days; slide 4 and slide 17 worked
  examples to the kopeck; slide 10 «ОКРУГЛ(30 000 000*(1+(13,99+0,1)/100/365);2)».
- **MOEX «РЕПО с открытой датой»**: settlement code `YOD`, maximum term 360 days,
  execution on the next settlement day after a closing order, coupon income passed by
  the CCP or by reducing the second leg (REPO-M). Core asks for an explicit
  `assumed_end_date`.
- **RUSFAR methodology**: the indicator is computed from orders and trades of KSU bond
  repo with the CCP (settlement code `Y0/Y1`, 1 day) until 12:30, published to two
  decimals; «расчет … не осуществляется в случае, если первая или вторая часть сделки
  РЕПО приходится на торговый нерабочий день, торговые субботы и/или воскресенья, а также
  в последний торговый день календарного года» (section 3): the RUSFAR publication calendar of
  the `FixingHistory`. On insufficient data the indicator equals the key rate (continuity section),
  which needs no special handling (it is still the published fixing).
- **Methodology of the CCP repo indicators**: RUSFAR REAL TIME and RUSFAR REAL TIME
  COMPOUND are intraday averages of the same order book ("COMPOUND" accumulates the
  day's trades), not compounded rates; they are not repo benchmarks in core.
- **ISS boards** (`gcc`): the floating KSU boards `FCOW`…`FCOY` (RUSFAR, term), `FRPO`
  (RUSFAR, open), `FKPO` (key rate, open), `FAOM` / `FATM` (key rate) and the fixed
  boards `GCRP`, `GCOW`…`GCOY`; documented in `docs/repo.md`.

## Not reachable

- NCC Clearing Rules (`nkcbank.ru`, `nationalclearingcentre.ru`), including clause
  21.12.2 of part II (quoted in the 2026 presentation) and the margin (компенсационные
  взносы) and income-transfer rules for repo with the CCP: refused by the build proxy.
- ROISfix was not looked up: it is an OIS fixing, not a repo benchmark, and no repo
  convention in core depends on it.
