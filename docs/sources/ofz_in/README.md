# Sources for the OFZ-IN indexation rule (package 9a)

Retrieved 2026-10-17 (UTC) through the build proxy with normal TLS verification (the
`*.moex.com` file host with the Russian trusted root CA, per request). The documents are
not stored in this repository. The table records where each came from and its SHA-256,
so the same bytes can be fetched and checked again. The raw data used by tests is in
`tests/fixtures/ofz_in/` (see its README).

| Document | Retrieved from | Bytes | SHA-256 |
| --- | --- | --- | --- |
| Bank of Russia, «Оценка вмененной инфляции из цен на индексируемые на инфляцию облигации», methodological comment, January 2021 (PDF) | <https://www.cbr.ru/Content/Document/File/118262/methodological_comment_20210127.pdf> | 702846 | `eae978be27b972a8754e1946e692577598cac50e66b88cc00f6e7f8520bab27b` |
| MOEX, «Описание технологии торгов ОФЗ с индексируемым номиналом», valid from 2015-06-23 (PDF, 2 pages) | page <https://www.moex.com/ru/documents/9958>, file <https://www.moex.com/files/4f0zpd1ve2n01xj6vxq1059mwe> | 195716 | `80bc06e69b1ad31c758acbbb9fbb46099a069b2142926b2b87736bf754ba3b79` |
| MOEX, «ОФЗ с индексируемым номиналом (ОФЗ – ИН)» (HTML page) | <https://www.moex.com/s2261> | 686930 | `e173194be5962f7521417c83c5f58039486748173ca20dc0892ce70774750b5c` |

## What each document is used for

- **Bank of Russia comment** (official restatement of the terms). Section 3 says
  formulas (1)–(5) "задаются условиями эмиссии и обращения облигаций федерального займа
  с индексируемым номиналом", footnote 5: "Утверждена приказом Минфина России от
  22.05.2015 № 80н". The formulas:
  - (1) `N_i = 1000 * I_i`;
  - (2) `I_i = INDEX_i / INDEX_base`, base = the placement date;
  - (3) `INDEX_i = Ref_CPI(M(i)-4) + (Ref_CPI(M(i)-3) - Ref_CPI(M(i)-4)) * (n-1) / d(i)`,
    with `n` the day of the month, `d(i)` the days in the month, and `Ref_CPI` the CPI
    of Russia "к среднегодовым ценам 2000 г." for the month;
  - (4) `CF_m = 2.5 * N_im * D_m / 365` (in %), `N_im` the nominal on the payment date of
    coupon `m`, `D_m` its days;
  - (5) `AI_i = N_i * 2.5 * (i - t) / 365`, `t` the start of the current period.

  Section 2 states the 3-month lag ("Лаг в индексации — 3 месяца", source Minfin) and
  daily indexation, so that each calendar month's total matches the monthly Rosstat CPI
  with a 3-month lag.
- **MOEX OFZ-IN page**: the deflation floor ("Минимальное значение номинала на дату
  погашения - 1000 Ᵽ"); coupon from the indexed nominal; accrued rounded to 1 kopeck by
  "mathematical rounding" (half up); `FACEVALUE` carries the daily indexed nominal. It
  links Minfin order No. 80n of 22.05.2015 (the terms) and order No. 214 of 15.07.2015
  (issue 52001) on minfin.ru.
- **MOEX trading note (2015)**: accrued from the nominal of the settlement date, rounded
  to the kopeck half up. Its sentence "Сумма купона рассчитывается исходя из номинала,
  установленного на последний день расчетного месяца" is **contradicted** by every coupon
  paid since (`tests/test_ofz_in_goldens.py`): they use the nominal of the payment date,
  as formula (4) says.

## Not obtained

- **The terms themselves** (Minfin order No. 80n of 22.05.2015, «Условия эмиссии и
  обращения облигаций федерального займа с индексируемым номиналом») and the per-issue
  orders (No. 149 of 16.03.2018 for 52002, No. 385 of 02.09.2021 for 52004):
  `minfin.gov.ru` / `www.minfin.ru` and the legal databases (`pravo.gov.ru`, `garant.ru`,
  `consultant.ru`, `docs.cntd.ru`, `normativ.kontur.ru`) are refused by the proxy
  (HTTP 403 on CONNECT). The web-fetch tool cannot resolve them either. Not confirmed from
  the text: the rounding of the nominal (kopecks, half up assumed; MOEX publishes kopecks
  and the data agree) and of `INDEX_base` (a search-engine excerpt of the terms says "с
  точностью до пяти знаков после запятой", which could not be read in context). Core takes
  `base_index` as an explicit term when it is published.
- **Rosstat CPI levels** "к среднегодовым ценам 2000 г." (what Minfin indexes on):
  `rosstat.gov.ru`, `fedstat.ru` and `gks.ru` are refused. The CBR file used instead has
  month-on-month figures to 0.01 pp (see the fixture README for the precision this gives).
- **Minfin's monthly table of nominal values** («Номинальная стоимость и купонный доход по
  ОФЗ-ИН»): on minfin.ru, refused.
- **Issue terms of the indexed corporates** RU000A1062M5 (Seligdar GOLD01) and
  RU000A108RQ7 (GOLD03): `e-disclosure.ru` is refused. See `docs/linkers.md`.
