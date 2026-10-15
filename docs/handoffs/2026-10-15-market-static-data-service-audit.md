# Handoff: аудит сервиса рыночных и статических данных, план живого теста

Дата: 2026-10-15. Сессия была только аудитом: код не менялся, коммитов в data-репо нет.

## Цель следующей сессии

Поднять и протестировать сервис рыночных и статических данных в облачной машине
Claude Code. Затем закрыть P1-дефекты из аудита отдельными PR, каждый с тестом.

## Где что лежит

`rub-fixed-income-core` — чистая расчётная библиотека. Загрузчиков в ней нет
(так задумано, см. README). Данные загружают соседние репо:

| Репо (GitHub `FiveLimaGolf/…`) | Версия | Роль |
|---|---|---|
| `market-data-store` | 0.13.0 | Рыночные данные → SQLite, сборка кривых, MCP `rub-fixed-income-mcp`, HTTP API |
| `moex-market-data-mcp` | 1.4.2 | Статика облигаций (каталог, купоны, амортизации, оферты, Cbonds, реестр ЦБ) и рыночные данные MOEX через MCP |
| `moex-live-bonds` | 0.2.1 | Транспорт APIM и MOEX Passport, загрузка учётных данных |
| `moex-bond-turnover` | — | Импортируется `moex-market-data-mcp/server.py` на верхнем уровне |
| `rub-fixed-income-core` | 0.8.0 | Расчёты. Отношение к данным имеют только `tools/qr_bond_reconciliation_probe.py` (Calypso/QR, Windows, `CALYPSO_LOGIN`/`CALYPSO_PASSWORD`) и `public_schedule.py` (мост для расписаний из market-data-store) |

Зависимости подключены как editable-пути `../<repo>`. Все пять репо должны лежать
рядом, например в `/home/user/<repo>`, иначе `uv` не соберёт окружение.

Предыдущие handoff-документы, которые всё ещё актуальны:
- `market-data-store/docs/handoffs/2026-08-21-key-rate-curves-and-bond-zspread-reconciliation.md`.
  Не закрыто: RU000A106037 (у нас 184.8 bp, у QR около 220). Нужны кривые QR за
  2026-08-21 и зафиксированные value date и НКД. `projection_curve_build_id` в
  происхождение данных монитора не добавлен.
- `moex-market-data-mcp/docs/handoffs/2026-09-04-cnyrub-tom-realtime-access-findings.md`.
  Права на non-display использование MOEX Passport не подтверждены.
- `docs/local-worker.md` в core ссылается на «0.6.0 handoff». Ни в одном репо его нет.

## Проверенное состояние (2026-10-15)

Офлайн-тесты, `uv run --frozen pytest -q`:

| Репо | Результат |
|---|---|
| rub-fixed-income-core | 469 passed |
| market-data-store | 379 passed, 4 failed |
| moex-market-data-mcp | 251 passed (нужны оба соседа) |
| moex-live-bonds | 18 passed |

4 падения в market-data-store загружают cron-обёртки `~/.hermes/scripts/run_gcurve_ingest.py`
и `run_intraday_fx_ingest.py`. Они лежат на прод-хосте вне git (`tests/test_cron_scripts.py:80`,
`tests/test_intraday_fx_cron_wrapper.py:10`).

Все HTTP-тесты используют моки (`httpx.MockTransport` и фикстуры). Живых тестов нет ни в одном репо.

Живая проверка из облака не удалась. `CbrClient.fetch_key_rate`, `fetch_ruonia` и
`MoexGCurveClient.fetch_latest` вернули `ProxyError 403`: сетевая политика окружения
не пускает на `www.cbr.ru` и `iss.moex.com`.

## Подводные камни

- `uv sync` без `--frozen` переписывает `uv.lock` в moex-market-data-mcp
  (rub-fixed-income-core 0.2.1 → 0.8.0). Всегда используйте `--frozen` или откатывайте lock.
- В moex-market-data-mcp `pyproject.toml:14` пин `rub-fixed-income-core==0.1.0`, а lock
  указывает 0.2.1. uv не проверяет версию editable-зависимости, поэтому реально работает 0.8.0.
- На прод-хосте MOEX ходит через локальный прокси `HTTPS_PROXY=http://127.0.0.1:1080`
  или `socks5h://127.0.0.1:1080` (`moex-market-data-mcp/scripts/run_bond_catalog_weekly.py:40`).
  Вероятно, ради российского IP. Из облака возможна геоблокировка, это нужно проверить.
- `connect_database` (`market-data-store/src/market_data_store/storage.py:1570-1580`)
  всегда создаёт файл, делает chmod, включает WAL и накатывает миграции. Даже `status`
  и HTTP API пишут в базу. Все `ingest-*` команды пишут в БД и запускают сборку кривых
  (`ingest.py:45-46`).
- Скрипты зашивают пути `/home/tako/...` (cron_*.sh, run_bond_catalog_weekly.py:42,
  check_formula_fallbacks.py:14-18).

## Секреты (только имена, значения не копировать никуда)

| Источник | Где ищет код |
|---|---|
| Ratescalcs | env `RATESCALC_BASE_URL`, `RATESCALC_USERNAME`, `RATESCALC_PASSWORD` (на проде из `~/.config/lima/secrets/ratescalc.env`) |
| OTC Monitor | файл `~/.config/lima/secrets/otc-monitor-token`, права 0600, каталог закрыт от группы и всех |
| MOEX APIM / AlgoPack | файл `~/.config/lima/secrets/moex-api-key` (0600); `moex-live-bonds` также читает env `MOEX_API_KEY` или `.env` |
| MOEX Passport realtime | `~/.config/lima/secrets/moex-realtime.json` и `moex-bond-orderbook.json` (`{"login","password"}`, 0600) |
| Cbonds | `~/.config/lima/secrets/cbonds.env` (`CBONDS_LOGIN`, `CBONDS_PASSWORD`, 0600) |
| Andromeda | `~/.config/lima/secrets/andromeda/storage-state.json`; README:252 указывает другой путь |
| `market-data-store ingest-moex-current` | требует env `MOEX_APIM_KEY`, но не использует его (см. P1) |

ISS MOEX и ЦБ публичные. В облаке секреты задаются переменными окружения в настройках
окружения. Для источников, которые читают файлы, нужен setup-скрипт, который разложит
переменные по файлам с правами 0600. Это делать только после решения пользователя
о лицензиях (Cbonds, Passport).

## Дефекты P1 (проверены по коду, если не помечено иначе)

market-data-store (`src/market_data_store/`):
1. `cbr.py:74-87` (и парсер заседаний `:151-173`) молча пропускают строки; `_request`
   (`:259-277`) не проверяет content-type; `store_cbr_fixings` и `store_policy_meetings`
   принимают пустой ввод. Заглушка с HTTP 200 даёт код 0 и `changed: 0`, и фиксинги
   ключевой ставки устаревают.
2. `intraday_fx.py`: `fetched_at` берётся до запроса (`:1310`), а `:305` отклоняет
   `SYSTIME > fetched_at` как время из будущего. `:250` превращает это в
   `PassportRealtimeUnavailable`, а `:1319` молча глотает. Realtime CNY, вероятно, часто
   выпадает (вживую не проверено).
3. `cli.py:541` требует `MOEX_APIM_KEY`, но `MoexApimClient` его не использует
   (`moex_prices.py:256-262`), а данные приходят из Passport. В `source_url` записано
   `https://apim.moex.com` (`cli.py:558`).
4. `moex_prices.py` (по отчёту агента, выборочно подтверждено):
   - нет фильтра BOARDID (`:228`), поэтому строки второго режима за ту же дату теряются;
   - цена в % от номинала хранится с `currency="RUB"` (`:81-82,124-125`);
   - хеш считается от всей страницы (`:74`), что даёт лишние ревизии.
5. Ratescalcs: нет времени источника и нет дедупликации, протухшие данные выглядят свежими.
6. `POST /v1/backfills` сохраняет запросы, но их никто не обрабатывает (`price_service.py:231-245`).
7. Cron-обёртки вне git, `cron_ingest_ratescalcs.sh` дважды обращается к источнику
   (сначала prod, затем dev).

moex-market-data-mcp (`src/moex_market_data/`):
1. `service.py:690-699`: `bond_schedule` режет купоны, амортизации и оферты до `limit=100`, флага усечения нет.
2. `service.py:727` (`known = value is not None`), а также `catalog_analytics.py:398,434`,
   `weekly_cashflow_report.py:103`, Cbonds `_normalize_flow`: нулевой купон-заглушка
   флоатера считается опубликованным.
3. `cbr.py:294-295,309`: `findtext(...) or "0"` превращает отсутствующий курс в 0.0.
   При HTML с кодом 200 вылетает необёрнутый `ParseError`.
4. `cbr_registry.py:11-12`: зашит `list_reestrcb_20260630.zip`, а `as_of` равен 2026-06-01.
5. `cbonds_enrichment.py`:
   - нет сверки MOEX и Cbonds (`:301-310`);
   - прогон помечается `completed` до загрузки потоков и оферт (`:138` против `:170,:197`);
   - больше 50 ISIN роняет весь прогон (`cbonds_service.py:134`);
   - нет повторов.
6. `catalog_client.py:197-246`: пагинация не сверяется с TOTAL.
7. `client.py:74-79` (ISS) не повторяет запросы на 429/5xx.

P2 (оба репо): поддельный UA `"OpenAI File Downloader, XaiImageApiFetch/1.0"`,
`date.today()` или UTC вместо MSK, праздники зашиты без переносов, логирования нет,
`set -a` при загрузке `ratescalc.env`.

Подтверждённых P0 (утечка секретов, отключённый TLS, порча данных) нет.

## План (выполнять по шагам, следующий только после успеха предыдущего)

1. **Доступность публичных источников из облака.** Пользователь добавляет
   `iss.moex.com` и `www.cbr.ru` в Allowed domains окружения. Проверка: одиночные
   вызовы `CbrClient(max_attempts=1).fetch_key_rate(...)` и
   `MoexGCurveClient(max_attempts=1).fetch_latest(...)` без БД.
   - Сработало: идём дальше.
   - 403 или геоблок: живой тест возможен только на прод-хосте через прокси 1080;
     переходим к шагам 2 и 4, а `probe` готовим для запуска на проде.
2. **Read-only команда `probe`** в market-data-store и moex-market-data-mcp:
   - по одному ограниченному запросу на источник, без `--database`;
   - проверки: content-type, обязательные блоки, непустой результат, возраст данных по MSK;
   - вывод: OK/FAIL, число строк, возраст, хеш, без тел ответов и секретов;
   - источник без секрета получает статус SKIP;
   - Andromeda выключена по умолчанию.

   Тесты на моках обязательны.
3. **Сквозной тест на публичных данных:** временная SQLite в scratchpad, затем
   ingest ЦБ и G-curve, затем сборка кривых, затем запрос через MCP или HTTP API.
4. **Исправления P1**, каждое с RED-тестом, отдельными PR.
   Порядок: ЦБ-пустой-парсинг (оба репо) → `or "0"` → Passport SYSTIME → флаг
   усечения `bond_schedule` → нулевые купоны → `MOEX_APIM_KEY`.
5. **Платные источники.** Только после решения пользователя о лицензиях и хранении
   секретов в облаке.

## Решения, которые ждут пользователя

- Открыть домены `iss.moex.com` и `www.cbr.ru` (обязательно для шага 1).
- Можно ли держать доступы Cbonds, MOEX Passport, Ratescalcs и OTC в облачном окружении.
- Права на non-display использование MOEX Passport (realtime CNY).
- Кривые QR за 2026-08-21 для RU000A106037: выгружать или закрыть вопрос.
- На будущее: объединить data-стек в один репо (uv workspace) вместо сетки путей `../`.

## Как начать новую сессию

- Основной репо: `FiveLimaGolf/market-data-store`.
- Подключить: `moex-market-data-mcp`, `moex-live-bonds`, `moex-bond-turnover`, `rub-fixed-income-core`.
- Клонировать все пять в `/home/user/<repo>`, затем выполнить `uv sync --all-extras --frozen` в каждом.
- Ветки для изменений создавать в соответствующих репо, PR открывать только по просьбе пользователя.
- Пользователь просит объяснять каждую команду, флаг и операнд, и отвечать кратко.
