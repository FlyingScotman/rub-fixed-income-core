# Handoff для следующей сессии

Состояние на 17 октября 2026, после релиза **0.17.0** (ветка `claude/blissful-brown-a0gh2o`,
PR в `main`). Сначала прочитайте `docs/PRODUCT_ROADMAP.md` (таблица статуса) и
`docs/BACKLOG.md` (#36–#38). Этот файл — короткая сводка того, что выяснено и что делать
дальше; когда пункт сделан, обновите или удалите его здесь.

## Что сделано в 0.17.0

- `RateCouponPeriod`, `RateAccrualCouponLeg`, `AccrualBasis` (`RATE_ACT_365`, `RATE_ACT_360`,
  `RATE_30_360`) в `rub_fixed_income/domain.py`. НКД по ставке на непогашенный номинал
  на начало периода. Это новые типы, а не новые поля: инструменты 0.16.0 сериализуются и
  хешируются как раньше (проверено в `tests/test_accrual_basis.py`).
- EOD golden-тесты MOEX: `tests/test_moex_eod_goldens.py`, фикстуры
  `tests/fixtures/moex_eod/` (18 облигаций, торги 2026-10-15) и
  `tests/fixtures/kbd/zcyc_2026-10-16.json`. Провенанс и смысл полей ISS — в
  `tests/fixtures/moex_eod/README.md`.
- BACKLOG #36 закрыт, заведён #38.

## Что установлено про данные MOEX ISS (не перепроверять заново)

- История `/iss/history/engines/stock/markets/bonds/boards/<BOARD>/securities.json?date=`:
  `ACCINT` — НКД на **дату торгов**; `YIELDATWAP`, `YIELDCLOSE`, `DURATION` — на дату
  расчётов (T+1), эффективная годовая ACT/365F, Macaulay в днях.
- НКД: ОФЗ — пропорционально сумме купона (Минфин, `FixedCouponLeg`); корпоративные —
  `номинал × ставка × дни / 365` (`RateAccrualCouponLeg`, ставка = `valueprc / 100` из
  bondization). Есть выпуски ACT/360 (RU000A1089U1) и 30/360 (замещающие USD-бонды на TQOD).
- На TQCB `ACCINT` валютной облигации в рублях, на TQOD — в валюте бумаги (CNY-бумаги на
  TQOD выглядят пересчитанными в USD; не использовать).
- Колл-облигации MOEX считает к погашению (колл не исполняется).
- Пут-облигации: `YIELDTOOFFER` = эффективная доходность к оферте по `CLOSE`;
  `trades_yields.EFFECTIVEYIELD` — то же по сделке. `YIELDATWAP` и `trades.YIELD` у
  пут-облигаций — **другая, неопознанная метрика**, с ядром не сравнивать. 7 из 57 пут-бумаг
  выборки не сходятся и по `YIELDTOOFFER` (RU000A10EKX1, RU000A106ZQ4, RU000A1023K1,
  RU000A10CDX0, RU000A102LF6, RU000A10EA16, RU000A100DZ5) — не разобрано.
- `trades_yields.GSPREADBP` = эффективная доходность − КБД на дюрации (в годах /365), на
  публикации КБД из `ZCYCMOMENT`; ядро совпадает до целого бп. Поля `ZSPREAD` /
  `ZSPREADATWAPRICE` в истории ОФЗ с Z-спредом ядра к КБД **не совпадают** (MOEX даёт
  доли бп, ядро — десятки бп); их смысл не выяснен.
- Bondization: в `offers` бывает `offerdate == "0000-00-00"` (тип «Оферта/Погашение») —
  пропускать. У некоторых бумаг первые купоны без `valueprc`.
- Бумаги с растущим номиналом (ОФЗ-ИН `SU52…` и индексируемые корпоративные, например
  RU000A108RQ7, RU000A1062M5) отвергаются `Instrument` («redemptions must sum to the
  initial face») — это пакет 9, не баг.

## Доступ к сети из облачной среды

- Работают: `iss.moex.com`, `www.cbr.ru` (проверено 2026-10-17).
- Отклоняются прокси: `fs.moex.com`, `www.moex.com`, `nfa.ru`. Без них BACKLOG #37 не сдвинуть.
- Полезные адреса ЦБ (все отвечают 200):
  - RUONIA: `https://www.cbr.ru/hd_base/ruonia/dynamics/?UniDbQuery.Posted=True&UniDbQuery.From=DD.MM.YYYY&UniDbQuery.To=DD.MM.YYYY` (HTML-таблица);
  - ключевая ставка: `https://www.cbr.ru/hd_base/KeyRate/?UniDbQuery.Posted=True&UniDbQuery.From=...&UniDbQuery.To=...`;
  - веб-сервис: `https://www.cbr.ru/DailyInfoWebServ/DailyInfo.asmx` (методы `Ruonia`, `KeyRate` и др.);
  - курсы: `https://www.cbr.ru/scripts/XML_daily.asp?date_req=DD/MM/YYYY`.
- Скачанные данные класть в отдельную пустую папку, скрипты держать в другой, Python
  запускать с `-I`. Фикстуры в репозитории — сырые ответы без изменений, с провенансом в
  README рядом.

## Как проверять перед пушем

CI: Windows и Ubuntu, Python 3.11 (+3.12 на Ubuntu); `pytest`, `ruff check`, `mypy`
(только 3.11), `uv build`. Системный Python в контейнере 3.13, и mypy на нём падает на
стабах numpy, поэтому mypy гонять в отдельном окружении 3.11:

```bash
UV_PROJECT_ENVIRONMENT=<scratchpad>/venv311 uv sync --python 3.11 --extra dev --locked
UV_PROJECT_ENVIRONMENT=<scratchpad>/venv311 uv run --python 3.11 mypy
uv run pytest -q && uv run ruff check . && uv build
```

`ruff format --check` ругается на `rub_fixed_income/key_rate.py` и
`tests/test_qr_bond_reconciliation_probe.py` — это было до 0.17.0, в CI формат не проверяется.

Релиз = один коммит `Release X.Y.Z: ...` с версией в `pyproject.toml`,
`rub_fixed_income/_version.py`, `uv.lock` и записями в `CHANGELOG.md`,
`docs/PRODUCT_ROADMAP.md`, `docs/BACKLOG.md`, при необходимости `docs/API.md`,
`docs/CONVENTIONS.md`, `docs/CONSUMERS.md`. Хеши в `tests/test_hash_stability.py` и
фикстура `tests/fixtures/portfolio_result_v2.json` не должны меняться.

## Следующие шаги (по приоритету)

1. **Golden-тесты флоатеров (пакет 1, критерий приёмки «reproduce MOEX-published current
   coupons for sample OFZ-PK and corporate floaters»).** Данные теперь доступны: RUONIA и
   ключевая ставка с `www.cbr.ru`, текущие купоны и даты периодов — bondization ISS
   (ОФЗ-ПК `SU29…`; корпоративные с `COUPON_DETAILS` «Ставка RUONIA» / «Ключевая
   ставка»). Модуль `rub_fixed_income/floating.py` (`docs/floating.md`) уже считает купон
   по истории фиксингов. Нужно: зафиксировать сырые выгрузки ЦБ как фикстуры, найти
   формулы купонов (лаг, усреднение, маржа) из условий выпусков — у ОФЗ-ПК они
   стандартные (Минфин), у корпоративных брать только те, где формула однозначна, —
   и сверить опубликованные купоны. Не угадывать конвенции: если формула не
   подтверждается данными, записать в BACKLOG.
2. **BACKLOG #37** — как только откроют `fs.moex.com` / `www.moex.com` / `nfa.ru`.
3. **BACKLOG #38** (база времени доходности валютных бумаг) — вместе с пакетом 3; базис
   должен прийти из условий выпуска, ISS его не даёт.
4. Разобрать 7 непонятных пут-облигаций и смысл `YIELDATWAP` у пут-бумаг — низкий
   приоритет, ядро с ним не сравнивается.
5. Перенос bridge- и fixing-тестов из неотслеживаемой папки `implementation/` (её нет в
   этом чекауте) — остаток пункта 0.10.
