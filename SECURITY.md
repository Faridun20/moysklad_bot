# Security & Code Audit

Систематический проход по кодовой базе на момент `2026-05-19`. Цель — найти
уязвимости, потенциальные ошибки бизнес-логики и места, которые стоит
переписать. Все находки классифицированы по серьёзности; в конце —
приоритизированный план «что чинить первым».

> **Этот документ — снимок состояния, а не roadmap.** По мере того как
> отдельные пункты закрываются, отмечай их в коммит-сообщениях и обновляй
> файл (или переноси в раздел «Closed» внизу).

---

## 🔴 Critical

Обход контроля доступа, потенциальный leak полной БД.

### C1. «Manager-by-default» в `ensure_user` — открытый бот при пустом `ALLOWED_USERS`

[`services/database.py:578-581`](services/database.py)

Когда `ALLOWED_USERS` пуст (legacy mode), любому новому юзеру ставится роль
`manager`. У `manager` почти все права: создание заказов, доступ к каталогу,
просмотр своих платежей, дёргание МойСклад API. Достаточно знать `@username`
бота — `/start` → ты внутри.

**Когда стреляет**: если на проде забыли выставить `ALLOWED_USERS` или
очистили его.

**Фикс**: дефолтная роль — всегда `guest`. Legacy-режим включается явным
флагом, например `LEGACY_OPEN_BOT=1`, и громко логируется при старте.

### C2. Расхождение валидных ролей между `cmd_addrole` и `set_role` — silent failure

`handlers/users.py:50` принимает `admin/boss/manager/employee`,
`services/database.py:500` валидирует `admin/boss/manager/guest`. Запрос
«сделай Васю employee» молча проваливается (`set_role` возвращает False),
но бот пишет «✅ Пользователю … назначена роль …».

Админ уверен, что роль выдал — на деле user остался в прежнем состоянии.

**Фикс**: единый whitelist ролей в `config.py` или константа в `roles.py`;
`cmd_addrole` проверяет возврат `set_role` и сообщает об ошибке.

### C3. Telegram-webhook: фейковая «двойная защита» секретом

[`webapp/server.py:124-157`](webapp/server.py)

Секрет в URL И в заголовке `X-Telegram-Bot-Api-Secret-Token` — это
**один и тот же** `TG_WEBHOOK_SECRET`. Утечка одного = утечка обоих.
Комментарий в коде («двойная защита») вводит в заблуждение.

Реальный эффект — лишний риск засветить секрет в access-логах прокси
(URL логируется чаще, чем header).

**Фикс**: либо в URL — непредсказуемый путь (`sha256(TG_WEBHOOK_SECRET)`
обрезанный, без раскрытия исходного), либо честно описать что это
проверка с двух мест с одним секретом.

---

## 🟠 High

Race conditions, утечка данных, сломанные допущения.

### H1. Race: `mark_order_paid` — два менеджера превышают остаток

[`services/database.py:1258-1338`](services/database.py)

`get_order_payment_summary` (несколько SELECT'ов) → `add_payment` →
`UPDATE orders.paid_at`. Между шагами нет блокировки. Два менеджера,
одновременно отметившие частичные оплаты, могут оба пройти проверку
`amount ≤ remaining` и в сумме отправить больше остатка.

**Фикс**: `SELECT … FOR UPDATE` на `orders` в начале транзакции, либо
проверка остатка после INSERT с откатом.

### H2. Race: `confirm_payment` + `_maybe_close_order_after_payment`

[`services/database.py:917-979`](services/database.py)

Атомарный UPDATE-with-WHERE для платежа есть, но дальше summary считается
вне транзакции. Concurrent confirm двух платежей одного заказа: каждый
видит `remaining > 0` (потому что другой ещё не applied), заказ не
закрывается автоматически даже когда сумма достигла total.

**Фикс**: пересчёт + UPDATE заказа в одной транзакции с `SELECT … FOR UPDATE`.
Опционально — фоновая задача-«дворник», которая раз в N минут переcчитывает
открытые заказы и закрывает те, у кого confirmed ≥ total.

### H3. Race: `create_paymentin_for_payment` — двойной paymentin в МойСклад

[`services/ms_payments.py:78-181`](services/ms_payments.py)

Проверка `if payment.ms_paymentin_id` идёт ДО POST в МойСклад. Между
проверкой и записью результата нет блокировки. Если cron-retry и hook
из `confirm_payment` стартанут параллельно — два paymentin'а на один платёж.

**Фикс**: атомарно ставить `ms_sync_status='in_progress'` через
UPDATE-WHERE (`WHERE ms_paymentin_id IS NULL AND ms_sync_status IS DISTINCT FROM 'in_progress'`).
Только тот, чей UPDATE дал rowcount=1, идёт в МойСклад.

### H4. Race: одновременный init нескольких процессов

[`services/database.py:393-454`](services/database.py)

При rolling deploy bot/webapp/cron стартуют параллельно. Все одновременно
гоняют миграции, backfill, recovery UPDATE по `orders`. Recovery читает
payments в подзапросе без lock — concurrent `confirm_payment` между
проверкой и UPDATE может оставить заказ в кривом состоянии.

**Фикс**: вынести миграции в отдельный `tasks/migrate.py`, запускать
одним процессом ДО старта остальных. Либо `pg_advisory_lock` вокруг
DDL-секции.

### H5. Race: `_ensure_custom_attribute` при первичном init

[`services/ms_demand.py:80-119`](services/ms_demand.py)

Два процесса параллельно: оба читают metadata/attributes, оба не находят
нужный, оба POSTят. МойСклад вернёт ошибку второму, но если второй
успеет до сохранения — будут два атрибута с одинаковым именем (разные
UUID).

**Фикс**: при ошибке POST повторно вычитать список attributes — если
кто-то уже создал, использовать его meta.

### H6. Race: `cb_approve_request` — orphan customerorder в МойСклад

[`handlers/orders.py:862-1010`](handlers/orders.py)

`approve_shipment_request` атомарен, но если bot упадёт между approve
и `set_order_ms_customerorder_id`, в МойСклад будет CO без ms_id в БД —
к нему никто никогда не привяжет paymentin.

**Фикс**: сохранять состояние «approve done, ms create pending» в БД
ДО вызова МойСклад. Если упало — retry-cron подберёт.

### H7. Утечка PII в логах через МойСклад error body

[`services/ms_payments.py:152-156`](services/ms_payments.py), [`services/moysklad.py:75-94`](services/moysklad.py)

`logger.error("MS paymentin failed: %s", err)` — `err` содержит первые
250 символов body от МойСклад. В payload иногда упоминаются другие
контрагенты, имена, UUID, что может попасть в долговременный log drain
(Better Stack / Axiom).

**Фикс**: либо более агрессивная обрезка / маркировка `[REDACTED]`,
либо whitelisting полей JSON-ответа МойСклад перед логированием.

### H8. Утечка `TELEGRAM_TOKEN` через URL

[`services/notifier.py:78`](services/notifier.py)

`f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"` — URL
строится inline на каждый запрос. Если aiohttp или wrapper в какой-то
момент попадёт с этим URL в exception traceback, токен утечёт в логи.

**Фикс**: использовать `aiohttp.ClientSession(base_url=...)` с базовым
URL `https://api.telegram.org`, а POST на `/bot{TOKEN}/sendMessage` —
относительно. Так токен не появляется в `repr(request)`.

> ⚠️ **Регрессия и повторный фикс (2026-05-21):** первая итерация задала
> `base_url="https://api.telegram.org/bot{TOKEN}"` — с **path-частью**, что
> aiohttp запрещает: каждый `sess.post` падал ещё до сети, и ВСЕ
> webapp-уведомления молча не доходили (баг жил, т.к. тесты мокали саму
> `tg_send_message`). Исправлено в `aa8b981`: `base_url` = только origin,
> токен в пути запроса, а в `except` строка ошибки прогоняется через
> `_redact_token()` (цель H8 сохранена). Добавлен тест на сетевой границе
> (`aioresponses`) + регресс-гард «base_url без path».

### H9. `MS_WEBHOOK_SECRET` fallback: ephemeral секрет между сервисами

[`services/ms_webhooks.py:39-53`](services/ms_webhooks.py)

При пустом `MS_WEBHOOK_SECRET` каждый процесс генерирует свой случайный
per-process. В split-сетапе (bot + webapp) у них **разные** секреты:
подписки регистрируются с одним, проверяются другим → 404 на webhook'и
от МойСклад.

**Фикс**: при пустом `MS_WEBHOOK_SECRET` падать на старте если выглядит
как production (`DATABASE_URL` задан, или явный `ENV=production`).
Random-fallback оставить только для локалки.

### H10. `_trigger_ms_paymentin_sync` использует deprecated API

[`services/database.py:953-978`](services/database.py)

`asyncio.get_event_loop()` deprecated в 3.12+, может бросить
`DeprecationWarning` или `RuntimeError`. Из thread (через `async_db`
wrapper) создаст новый loop в потоке — неожиданное поведение.

**Фикс**: `asyncio.get_running_loop()` (бросит RuntimeError если не в
async context — это нужный signal), `asyncio.run()` иначе.

### H11. SSRF/abuse через `search` в `api_agents`

[`webapp/server.py:1077-1110`](webapp/server.py)

Параметр `search` проходит без санитизации в `ms_get` с поиском по
МойСклад. Manager может через специально подобранные search-запросы
устроить пачку дорогих API-вызовов (rate-limit 30/мин есть, но
дорогие запросы быстрее съедают rate-limit самой МойСклад).

**Фикс**: длина ≤ 50 символов, whitelist разрешённых символов
(буквы, цифры, дефис, пробел).

### H12. XSS через `details` в audit log

[`utils/formatters.py`](utils/formatters.py), `services/database.py:1106-1113`

`details` сохраняется в БД и потом рендерится в HTML через `format_audit_entry`
без escape. Менеджер с правом `/pay` может протащить HTML в audit log,
который видит админ — `<a href="evil">` ссылка в Telegram-сообщении.

**Фикс**: универсальная `_esc` в `utils/helpers.py`, применять везде
где `parse_mode="HTML"` + пользовательский input. Сегодня `_esc`
дублируется в 4+ файлах.

---

## 🟡 Medium

Корректность, надёжность, читаемость.

### Timezone consistency

- **`services/database.py:134-135`** — `now_str()` использует `datetime.now()` без TZ. На Railway TZ зависит от региона деплоя; рассинхронизация с UTC-расчётами в WebApp (`datetime.utcnow()` в [`webapp/server.py:310, 520`](webapp/server.py)).
- **`webapp/server.py:351-357`** — `today_iso` от UTC vs `o["created_at"][:10]` (local) → заказы около полуночи могут «не попасть» в today фильтр менеджера.
- **`tasks/scheduled.py:29-43`** — хардкод UTC, но комментарий «09:00 UTC (12:00 Ташкент)» рассинхронизирован с `TZ_OFFSET` env var. Если кто-то поменяет offset, scheduled останется в UTC.
- **`services/notifier.py:131`** — `last_check = datetime.now()` без TZ → пропуски/дубли при разных регионах.

**Фикс**: одна конвенция — всё в UTC внутри БД и логики, конвертация в local только на представлении. Использовать `datetime.now(timezone.utc)`.

### `datetime.utcnow()` deprecated в 3.12+

Несколько мест в `webapp/server.py`, `services/database.py`. Заменить на
`datetime.now(timezone.utc)`.

### Дубли отчётов: `ENABLE_SCHEDULED_REPORTS` + Cron

[`bot.py:141-151`](bot.py), [`tasks/scheduled.py`](tasks/scheduled.py)

Если включены оба (env-флаг и Railway Cron) — отчёты уйдут дважды.
В коде нет защиты. Минимум — `logger.warning` при старте, если детектится
обе среды.

### `init_db` — гигантская функция с DDL + миграциями + backfill

[`services/database.py:153-457`](services/database.py)

300+ строк в одной функции. Хаос DDL/UPDATE при concurrent старте.
Recovery UPDATE запускается на каждом старте, перечитывая всю таблицу
`orders` через несколько подзапросов — медленно при росте.

**Фикс**: вынести в `tasks/migrate.py`. Прогонять одним процессом перед
стартом сервисов. В `init_db` оставить только `CREATE TABLE IF NOT EXISTS`.

### `webapp/server.py:1217-1252` — динамический WHERE через f-string

Хоть параметризация `?` корректная, динамическая сборка SQL — анти-pattern.
Лучше — фиксированный SQL вида `WHERE (? IS NULL OR user_id = ?)`,
параметры дважды.

### Cache cleanup в МойСклад TTL

[`services/moysklad.py:165-173`](services/moysklad.py)

При >200 keys чистим протухшие. Для текущего usage хватит навечно, но
при росте — потенциальный memory leak. Лучше — LRU.

### `webapp/server.py:1395-1423` (`api_confirm_payment`) — нет идемпотентности

Босс может два раза кликнуть approve; второй вызов вернёт 0 confirm'ов.
UX OK, но фронт во время первого может показывать спиннер, юзер
рефрешит — рассинхрон возможен.

**Фикс**: client-side `idempotency-key` (uuid), сохранять в `payments`
для предотвращения дублей.

### `handlers/orders.py:803-823` (cb_delete_order) vs WebApp endpoint

В боте `cb_delete_order` использует `update_order_status(..., "rejected")` —
оставляет позиции в БД. В WebApp `api_delete_draft` использует настоящий
`delete_order` (каскадно сносит). Несогласованное поведение.

### Sync DB-вызовы в async handlers

`handlers/orders.py:285, 309, 332, 262-271` — `create_order`,
`get_user_orders`, `get_pending_requests` синхронно блокируют event loop.
ARCHITECTURE.md говорит «в боте OK», но при росте — проблема. Используйте
`asyncio.to_thread` или `services.async_db`.

### `webapp/server.py:163-180` — non-constant-time secret compare

`if secret != get_webhook_secret()` — теоретически timing-attack.
Маловероятно через HTTP, но `hmac.compare_digest` дешевле.

### `webapp/server.py:163-206` (ms_webhook) — нет лимита payload size

DoS-вектор: МойСклад не пришлёт мегабайты, но кто-то с известным
секретом может. FastAPI без middleware не лимитирует.

### `webapp/auth.py:38-39` — `e` в логе может содержать фрагмент initData

При ошибке парсинга exception text иногда включает payload. Маленький
риск, но логи фильтруются.

### МойСклад error messages в чат боссу

[`handlers/orders.py:925-936`](handlers/orders.py)

`reason` от МойСклад API целиком пишется в чат через `<code>`. Может
содержать UUID других сущностей. Низкий риск (видит только босс),
но обрезать стоит.

### `services/database.py:692-709` (`get_payments_for_orders`)

Параметр `order_ids` без dedup; `placeholders count = len(order_ids)`
но при дублях SQL валиден только из-за повторных placeholder'ов.
Сделать `set(order_ids)`.

### `services/snapshot.py:36-63` (`meta_set`) — SET clause из **kwargs

Если когда-нибудь имена ключей придут из внешнего источника — SQL
injection. Сейчас все вызовы — литералы, но whitelist ключей правильнее.

### `services/snapshot.py:354-365` (`stats`) — таблица в f-string

`cur.execute(f"SELECT COUNT(*) AS c FROM {tbl}")` — таблица из локального
whitelist, безопасно, но f-string в SQL — анти-pattern.

### `_read_index_html` cache forever

[`webapp/server.py:227-236`](webapp/server.py)

HTML с подставленной `APP_VERSION` кэшируется в памяти процесса навечно.
После rolling deploy старый процесс отдаёт старую версию пока не убьют.
Незначительно — все равно процессы умирают.

---

## 🟢 Low / Nit

Стиль, дубли, мёртвый код.

### Дубли `_esc`

Определён в 4+ местах: `handlers/orders.py:25`, `handlers/debts.py:45`,
`handlers/payments.py:365`, `tasks/run_debts_notify.py:39`. Все делают
одно и то же. Вынести в `utils/helpers.py`.

### Дубли `PAGE_SIZE`

`handlers/stock.py:25`, `utils/formatters.py:13`, `utils/keyboards.py:7` —
все = 10. В `config.py`.

### Мёртвый код

- `services/database.py:461-472` — `_migrate` функция нигде не вызывается.
- `handlers/users.py:11`, `handlers/payments.py:15` — `from config import ADMIN_IDS` неиспользуется.
- `requirements.txt` — `asyncpg==0.29.0` установлен, но нигде не импортируется (упомянут только в комментариях async_db.py как «этап 2 миграции»). Удалить.

### `handlers/log.py` vs `handlers/audit.py`

Почти одинаковая логика просмотра audit log с разными UI. Дубли
`format_audit_entry` / `format_log_entry`, дубли `filter_by_period`.
Объединить.

### Async-task без хранения reference

- `bot.py:265-279` (`initial_snapshot`) — `asyncio.create_task(...)` без
  сохранения, потенциальное `RuntimeWarning: Task was destroyed`.
- `webapp/server.py:1107` — `snapshot.refresh_counterparties()` то же.

Хотя бы хранить в set + `task.add_done_callback(set.discard)`.

### Mixed sync/async stack

`handlers/orders.py:1063-1100` — широкий `try/except: Exception` скрывает
баги. `tasks/scheduled.py:140` — `asyncio.get_event_loop().time()`
deprecated, лучше `time.monotonic()`.

### Type hints

В большинстве handlers и многих services нет аннотаций возврата.
Хотя бы публичный API стоит аннотировать.

### Дубли `get_sales_stats` в `api_analytics`

[`webapp/server.py:540`](webapp/server.py) — `gather` вызывает
`get_sales_stats(since, now)` и `get_shipments(since, now)`, но
`get_sales_stats` уже зовёт `get_shipments` внутри. Тот же диапазон
запрашивается дважды. Кэш `_ms_ttl_cache` спасает, но логически
неуклюже.

### Конфигурация

- `config.py:6-9` — `try/except ImportError` для `config_local` поймает
  только ImportError. SyntaxError проложит экзепшен мимо.
- `config.py:24` — парсер user_id не парсит отрицательные (для users
  не нужно, но для chat_id групп — да).

### `_load_predefined_users` поведение

При снижении роли админом вручную, после рестарта `_load_predefined_users`
**не** перезатрёт (ON CONFLICT DO NOTHING). Это корректно, но в
коментарии стоит описать.

### Нет тестов и CI — ✅ ЗАКРЫТО

> Историческая находка. Сейчас: pytest-набор (`tests/`, ~89 тестов на
> `isolated_db`), CI `.github/workflows/ci.yml` (ruff + mypy + pytest с
> coverage-«храповиком»), `pre-commit`. Тесты мокают **границу с сетью**
> (`aioresponses`), а не свои обёртки — иначе баг проходит CI (урок H8).
> Покрыты денежные инварианты, контракт МойСклад, регрессии безопасности
> (`_authorize`, HTML-escape), state-machine, circuit breaker, дедуп
> уведомлений. Остаётся непокрытым UI WebApp (проверяется вручную).

---

## ✅ Что хорошо сделано

Чтобы не было ощущения сплошной критики — список того, что трогать
**не нужно**:

- Валидация Telegram `initData` в `webapp/auth.py` — корректная:
  timing-safe `hmac.compare_digest`, проверка свежести `auth_date`
  (24ч), HMAC-SHA256 с правильным двойным деривированием.
- `_authorize` в webapp избавляет от копипасты role-check + rate-limit.
- `user_safe_error` (`utils/helpers.py`) — exception целиком в лог,
  юзеру generic-текст. Применяется почти везде.
- Параметризованный SQL практически везде. Динамика (`placeholders`,
  `set_clause`) — из контролируемых внутренних значений.
- TTL-кэш + inflight-coalescing в `_ms_ttl_cache` (`services/moysklad.py`):
  per-key lock, double-check после lock'а, защита от дубль-fetch.
- Backfill / recovery в `init_db` сопровождён подробными комментариями,
  объясняющими почему.
- `ENABLE_SCHEDULED_REPORTS` + cron-сервисы — правильное разделение
  для надёжности (cron не зависит от состояния бота).
- Persistent aiohttp session для МойСклад + Telegram — экономит
  TCP+TLS handshake'и.
- `ms_payments.py`: идемпотентность через `ms_paymentin_id`, явные
  статусы `synced/failed/NULL` + retry cron — продуманная схема.
- `tasks/run_*.py` — короткоживущие cron-процессы вместо вечных циклов.
- ThreadedConnectionPool для psycopg2 — экономит overhead подключений.
- Snapshot-стратегия с debounce + webhook + safety-net pull — хорошее
  решение для rate-limit'ов МойСклад.
- Лимиты bot middleware (30 действий/мин) и per-endpoint rate-limit
  в webapp — защита от спама.

---

## 🎯 Top-5 приоритетов

В порядке: «сначала закрыть критические дыры, потом надёжность, потом
качество кода».

### 1. Закрыть «manager-by-default» (C1) + синхронизировать роли (C2)

Дефолт `guest`, legacy-mode только через явный env-флаг. Единый
whitelist ролей в `roles.py`. `cmd_addrole` проверяет возврат `set_role`.

**Эффект**: невозможно случайно открыть бот всему интернету.

### 2. Решить race-conditions в платежах (H1, H2, H3)

`mark_order_paid`, `confirm_payment`, `_maybe_close_order_after_payment` —
одна транзакция с `SELECT … FOR UPDATE` на `orders`. Уникальный constraint
на `(order_id, ms_paymentin_id)` чтобы БД защитила от двойной синхронизации.

**Эффект**: невозможны двойные paymentin'ы в МойСклад, заказы корректно
закрываются после параллельных confirm.

### 3. Вынести миграции из `init_db` (H4, M5)

`tasks/migrate.py` — единый migration runner, один раз перед стартом.
В `init_db` остаются только `CREATE TABLE IF NOT EXISTS`.

**Эффект**: при rolling deploy нет hazard'а конкурентных DDL/UPDATE.

### 4. HTML-escape везде (H12, M раздел про escape)

Универсальный `_esc` в `utils/helpers.py`. Применить во всех форматтерах
(`utils/formatters.py`, `services/notifier.py`, handlers с
`parse_mode="HTML"`).

**Эффект**: невозможно протащить произвольный HTML через имя клиента
или комментарий менеджера; нет «can't parse entities»-падений.

### 5. Минимальный smoke-test + CI

```
tests/
├── test_auth.py       # HMAC валидный/просроченный/битый
├── test_lifecycle.py  # order → mark_paid → confirm → close на SQLite
├── test_rate_limit.py # acquire/window edge cases
└── conftest.py        # fixtures для in-memory БД

.github/workflows/ci.yml  # ruff + pytest на каждый push
```

**Эффект**: backfill recovery / race-fix будущих коммитов не сломают
прод втихую.

---

## Closed

Закрытые после первоначального аудита — со ссылкой на коммит. Если
заново всплывут — открой новой записью в Critical/High/Medium/Low.

| ID | Что | Коммит |
|---|---|---|
| C1 | `manager-by-default` → `guest` при пустом ALLOWED_USERS, явный legacy-флаг | round 1 |
| C2 | role mismatch `employee` vs `guest` — единый whitelist | round 1 |
| C3 | Telegram webhook «двойная защита» — честный комментарий, не пропадает | round 1 |
| H1 | mark_order_paid race → `SELECT … FOR UPDATE` на orders | round 1 |
| H2 | confirm_payment + _maybe_close_order race → одна транзакция | round 1 |
| H3 | create_paymentin double-write → UNIQUE constraint на ms_paymentin_id | round 1 |
| H4 | init_db DDL/UPDATE race → `tasks/migrate.py` + split init_db | [042bcd5](https://github.com/Faridun20/moysklad_bot/commit/042bcd5) |
| H5 | _ensure_custom_attribute race → re-read on POST failure | [042bcd5](https://github.com/Faridun20/moysklad_bot/commit/042bcd5) |
| H6 | orphan customerorder → audit_log до db-write | [042bcd5](https://github.com/Faridun20/moysklad_bot/commit/042bcd5) |
| H7 | PII в MS error logs → `redact_ms_error` | [042bcd5](https://github.com/Faridun20/moysklad_bot/commit/042bcd5) |
| H8 | TELEGRAM_TOKEN в URL → base_url origin + токен в пути + `_redact_token`. Round-1 фикс (path в base_url) сломал отправку — повторно исправлено + тест на границе | round 1, [aa8b981](https://github.com/Faridun20/moysklad_bot/commit/aa8b981) |
| H9 | MS_WEBHOOK_SECRET fail-fast в проде | round 1 |
| H11 | `/api/agents` search санитизация (len ≤ 50, char whitelist) | round 1 |
| H12 | XSS через audit log → universal `_esc` в utils/helpers | round 1 |
| M (HTML escape) | escape во всех форматтерах | round 1 |
| M (TZ) | datetime.utcnow → utc_now; today_iso → local_now в webapp | [042bcd5](https://github.com/Faridun20/moysklad_bot/commit/042bcd5) |
| M (idempotency) | /api/confirm_payment принимает idempotency_key | [042bcd5](https://github.com/Faridun20/moysklad_bot/commit/042bcd5) |
| M (dedup IN) | get_orders_by_ids / get_payments_for_orders / get_order_items_by_ids дедупликация | [042bcd5](https://github.com/Faridun20/moysklad_bot/commit/042bcd5) |
| M (sync DB in async) | handlers/orders — to_thread на cmd_new_order/myorders/orders | [042bcd5](https://github.com/Faridun20/moysklad_bot/commit/042bcd5) |
| Low (PAGE_SIZE) | вынесен в config.PAGE_SIZE | [f86ad7a](https://github.com/Faridun20/moysklad_bot/commit/f86ad7a) |
| Low (_migrate dead code) | удалён | round 1 |
| Low (asyncpg unused) | удалён из requirements.txt | round 1 |
| Low (audit/log dup) | format_log_entry удалён, общий format_audit_entry + chunk_messages | [d4e474f](https://github.com/Faridun20/moysklad_bot/commit/d4e474f) |
| Low (cache forever) | _read_index_html keys cache by mtime | [d4e474f](https://github.com/Faridun20/moysklad_bot/commit/d4e474f) |
| Low (dual reports) | warning при ENABLE_SCHEDULED_REPORTS unset + Railway | [d4e474f](https://github.com/Faridun20/moysklad_bot/commit/d4e474f) |
| Low (type hints) | type annotations на add_payment и публичные DB-функции | [d4e474f](https://github.com/Faridun20/moysklad_bot/commit/d4e474f) |
| Tests + CI | pytest + GitHub Actions | round 1 |
| Medium (cron-ms-retry context) | init_demand_context() в cron-CLI (раньше: «Контекст ms_demand не готов» застрял на 2 платежах >25 ч) | PR #35 [a920fd3](https://github.com/Faridun20/moysklad_bot/commit/a920fd3) |
| Medium (cron-ms-retry orphans) | `reset_stale_in_progress_payments(30)` reaper — orphan'ы `in_progress` от mid-claim SIGTERM сбрасываются за 30 мин (раньше залипали навсегда: claim-UPDATE отвергает `in_progress`) | PR #36 [36600d5](https://github.com/Faridun20/moysklad_bot/commit/36600d5) |
| Medium (silent ms_sync_error overwrite) | cron при `init_demand_context().ready == False` делает early-return до цикла, не перезаписывает реальный `ms_sync_error` (типа «429») на «Контекст не готов» | PR #36 [36600d5](https://github.com/Faridun20/moysklad_bot/commit/36600d5) |
| Medium (MS /positions truncation) | `get_shipment_positions` пагинирует offset-loop'ом — крупные B2B-заказы >100 line items больше не теряют хвост в `top_products` аналитики | PR #36 [36600d5](https://github.com/Faridun20/moysklad_bot/commit/36600d5) |
| Medium (cache_clear race breaks inflight) | `cache_clear` чистит только `cache`, не `locks` — MS-webhook `invalidate_ms_cache()` больше не race'ит с in-flight winner'ом и не запускает второй параллельный HTTP | PR #36 [36600d5](https://github.com/Faridun20/moysklad_bot/commit/36600d5) |
| Low (INFO log silencing) | `config.py` использует named logger, не root → entrypoint'ы `bot.py`/`tasks/run_*.py` корректно ставят `basicConfig(level=INFO)`. Раньше `WARNING:root:config:...` на импорте триггерил неявный `basicConfig(level=WARNING)` и весь INFO глушился на проде | PR #35 [a920fd3](https://github.com/Faridun20/moysklad_bot/commit/a920fd3), PR #36 [36600d5](https://github.com/Faridun20/moysklad_bot/commit/36600d5) (`config:` префикс возвращён в message) |
| Low (MS positions 429-spam) | `_get_positions_semaphore()` lazy per-loop, cap=8 — cold-cache `/api/analytics` без retry-chain 0.5/1.0/2.0с | PR #35 cap=4 → PR #36 cap=8 + lazy [36600d5](https://github.com/Faridun20/moysklad_bot/commit/36600d5) |
| Low (locks dict memory leak) | `_ms_ttl_cache.locks` чистится в `try/except BaseException` вокруг `await fn()` — для consistently-failing keys (404, deleted demands) lock больше не leaks вечно | PR #36 [36600d5](https://github.com/Faridun20/moysklad_bot/commit/36600d5) |
| Low (module-level Semaphore landmine) | `_get_positions_semaphore()` lazy по `id(running_loop)` — future tests с >cap acquire'ами через `asyncio.run` больше не упадут «bound to a different event loop» | PR #36 [36600d5](https://github.com/Faridun20/moysklad_bot/commit/36600d5) |
| Round 6 RACE-1 (cash_deposit double-allocation) | `create_cash_deposit` берёт `pg_advisory_xact_lock(manager_id)` — два параллельных /deposit от одного менеджера сериализуются, FIFO-расчёт + INSERT внутри одной транзакции | PR #38 |
| Round 6 RACE-2 (create_return TOCTOU) | `create_return` берёт `pg_advisory_xact_lock(order_id)` + commit/rollback внутри одной tx — двойной pending по одному заказу больше невозможен | PR #38 |
| Round 6 RACE-3 (set_return_ms_id race) | Conditional UPDATE: `... WHERE moysklad_return_id IS NULL` — два параллельных create_salesreturn'а: второй вернёт False, caller знает что надо чистить orphan | PR #38 |
| Round 6 RACE-4 (ops_monitor double-digest) | Idempotency-таблица `ops_monitor_runs(run_date PK)` + `claim_ops_monitor_run(today)` — повторный запуск за тот же день делает noop | PR #38 |
| Round 6 L_R2 (confirm_return overshoot) | Атомарный UPDATE `... WHERE returned_qty + ? <= quantity` с проверкой rowcount — overshoot из concurrent confirm двух return'ов одного заказа невозможен | PR #38 |
| Round 6 L_R6 (mark_order_shipped двойной audit) | Если `ms_demand_id` уже стоит — audit пишет «sync» а не «отмечен вручную»; убирает спам менеджеру | PR #38 |
| Round 6 S1 (edit_text round-trip HTML) | handlers `cb_deposit_confirm`/`cb_return_confirm` используют `call.message.html_text` (а не decoded `.text`) — повторный parse_mode="HTML" не ломается на `<`/`>` в именах | PR #38 |
| Round 6 S2 (no length cap on reason/comment) | Cap'ы 500/1000 chars на API-edge: deposits/reject, returns/create, orders/cancel, payments/send, credit/set + одноимённые FSM-handlers в боте | PR #38 |
| Round 6 S3 (amount overflow → inf/NaN) | Новый `_validate_amount` в БД + `math.isfinite()` + верхний лимит 10М на API-edge: deposits/create, credit/set, payments/send — `1e308`/inf/NaN отвергаются 400 | PR #38 |
| Round 6 S4 (agent_id/agent_name unbounded) | Cap'ы 64/200 chars на `agent_id`/`agent_name` в /api/credit/set и /api/orders/set_agent | PR #38 |
| Round 6 S5 (ms_returns exception leaks repr) | `redact_ms_error(str(e))` в exception-path `create_salesreturn` — единообразно с HTTP-error path | PR #38 |
| Round 6 L_R1 (FSM-handler role-degraded) | `process_*_reason` (deposits/returns/order_cancel) повторяют role-check после FSM-перехода — если админ снял роль во время ввода причины, действие не пройдёт | PR #38 |
| Round 6 L_R5 (api_add_item etc no role-check) | `_authorize(roles=admin/boss/manager)` добавлен в api_add_item / api_remove_item / api_set_agent / api_submit_order — guest не может сабмитить старый draft под видом manager'а | PR #38 |

## История изменений этого документа

| Дата | Что | Кто |
|---|---|---|
| 2026-05-19 | Первоначальный аудит | Claude (general-purpose agent) |
| 2026-05-19 | Раунд 1 фиксов: 3 Critical, тесты+CI, payment races, HTML escape | Claude |
| 2026-05-19 | Раунд 2 фиксов: H4-H7, H11, TZ, idempotency, dedup, sync→async, PAGE_SIZE | Claude |
| 2026-05-19 | Раунд 3: audit/log dedup, cache mtime, type hints, dual-reports warning | Claude |
| 2026-05-21 | H8 regression re-fix (aiohttp base_url); защитные сетки: pytest-набор + CI (ruff/mypy/coverage), мок сетевой границы, mypy-гейт; событийные уведомления об отгрузках с дедупом | Claude |
| 2026-05-23 | Раунд 4 (Railway logs review): PR #35 — cron-ms-retry init_demand_context, INFO-логи (named logger в config), `_POSITIONS_CONCURRENCY=Semaphore(4)`; вживую проверены прод-логи через Railway CLI | Claude |
| 2026-05-24 | Раунд 5 (self-code-review PR #35 → 10 находок): PR #36 — orphan-reaper для 'in_progress', noop-skip для init_demand_context, /positions pagination, cache_clear narrow, lazy-by-loop Semaphore(8), locks-pop на except, `config:` префикс в message; +6 тестов (194 total) | Claude |
| 2026-05-24 | Раунд 6 (security audit на новых модулях deposits/returns/cancel/ship/credit/ms_returns/ops_monitor): 4 race (advisory-lock'и, conditional UPDATE, idempotency-table), 4 input-validation (amount upper bound, length caps, html_text round-trip), 5 role/authz фиксов (process_*_reason role-recheck, _authorize на add_item etc.) — итого 13 пунктов в одном PR | Claude |
