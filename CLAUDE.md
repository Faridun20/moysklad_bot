# CLAUDE.md

## Commands

```bash
pip install -r requirements.txt -r requirements-dev.txt   # dev-зависимости запинены

pytest tests/                              # тесты (SQLite в /tmp, isolated_db fixture; env-дефолты в conftest)
ruff check .                               # полный набор из pyproject.toml (E9,F,B,ASYNC,UP,SIM)
ruff check --select=E9,F63,F7,F82 .        # строгий минимум-гейт (как блокирующий шаг CI)
mypy                                       # точечно по order_workflow/database/moysklad/server — гейт, 0 ошибок
pre-commit install && pre-commit install --hook-type pre-push   # локальная первая линия (опц.)

# Фронт-тесты WebApp (Vitest, webapp/static/__tests__) — гоняет CI (npm test).
# Локально: на машине нет Node/пакетных менеджеров → ставим portable Node в .tools/
powershell -ExecutionPolicy Bypass -File scripts/setup-node.ps1   # 1× : portable Node LTS в .tools/node (gitignore)
powershell -ExecutionPolicy Bypass -File scripts/test-js.ps1      # npm install (1×) + vitest run; scripts/test-js.cmd — из cmd

python bot.py                              # локально: без Postgres → SQLite, без Redis → MemoryStorage
python -m tasks.migrate                    # schema + data миграции, ДО старта сервисов на проде

# Cron CLIs (Railway Cron Jobs)
python -m tasks.run_debts_notify
python -m tasks.run_ms_sync_retry
python -m tasks.run_ops_monitor          # дневной ПИНГ (1×/день): короткое «N событий — откройте WebApp» + web_app-кнопка. Сами данные — в WebApp (/api/ops-summary + блок «Требует внимания» на главной). Отчёты продаж/склада убраны из бота — смотрят в Аналитике WebApp.
python -m tasks.run_maintenance          # janitor: чистка дедупа/аудита/soft-deleted (ночью)
python -m tasks.run_ms_reconcile         # страховка: approved-заказы с ms_customerorder_id, 404 в МС → отмена локально (ежечасно)
python -m tasks.run_backup               # дамп БД → gzip → приватный TG-канал (ночью)
```

**Сборка/деплой:** Railway **Railpack** (не Nixpacks) — `railway.json`
(`deploy.aptPackages: [postgresql-client]`). Требует Shared Variable
`MISE_PYTHON_GITHUB_ATTESTATIONS=false` (иначе mise падает на attestations
Python 3.11.9).

Конфиг тулчейна — в `pyproject.toml` (`[tool.ruff]`, `[tool.pytest.ini_options]`,
`[tool.mypy]`, `[tool.coverage]`). Версии dev-тулов запинены в `requirements-dev.txt`.

## Architecture

Детали в `ARCHITECTURE.md`. Главное: `BOT_MODE` env переключает процесс между `all` / `bot` / `webapp`. На Railway prod два сервиса (`moysklad_bot` + `Webapp`) с разными `BOT_MODE`, общий Postgres + Redis через Project Shared Variables.

## Conventions (нетривиальные)

**DB:**
- `init_db()` делает ТОЛЬКО `CREATE TABLE IF NOT EXISTS`. `ALTER TABLE` и backfill → в `run_migrations()` / `run_backfills()`, вызываются из `tasks/migrate.py`. **Не добавляй ALTER в init_db.**
- Webapp endpoint'ы: `services.async_db as adb` (`await adb.get_user(uid)`) — обёртка через `asyncio.to_thread`. В bot handlers — то же или явный `asyncio.to_thread`.
- Роль читай через `services.roles.cached_role(user_id)` (TTL 60s) и предикаты `is_boss / can_create_orders / ...`. НЕ через `services.database.get_role` напрямую — обойдёшь кэш. **Деактивация:** `get_role` отдаёт `guest` если `user_roles.deactivated_at` стоит → деактивированный теряет ВСЕ права. `deactivate_user/reactivate_user` (адмін, `/deactivate`/`/reactivate` + webapp `/api/users/deactivate`).
- Race-чувствительные операции (`mark_order_paid`, `confirm_payment`, `confirm_cash_deposit`) используют `SELECT ... FOR UPDATE` / advisory-lock — сохраняй паттерн.

**Native async DB (asyncpg, задача #21):** денежное ядро (order/payment reads+writes, кредит, сдачи, возвраты) — на `services/adb_core.py` (asyncpg на проде / aiosqlite в тестах; `$N`-плейсхолдеры; `fetch/fetchrow/fetchval/execute`; `transaction()` CM с commit/rollback). Остаток (`get_setting/get_role/add_audit_log`, notifier, snapshot-reads) — sync, мостится через `async_db.__getattr__` (`asyncio.to_thread`; coroutine-функции пропускаются как есть). `_to_thread(db.X, ...)`-форма НЕ ловится grep'ом `db.X(` — аудитируй её при флипе функции в async. `snapshot.refresh_*` теперь native async внутри `adb_core.transaction()` (атомарный DELETE+INSERT); read-функции snapshot остаются sync (зовутся через to_thread).
- Дедуп уведомлений об отгрузках: `notified_shipments(demand_id PK)` + `mark_shipment_notified(id)` (атомарный INSERT-if-absent). И MS-вебхук (webapp), и резервный поллер (bot) пишут сюда — общий Postgres гарантирует одно уведомление на demand. Старьё чистит `prune_notified_shipments()`.

**Уведомления об отгрузках (событийные):**
- Основной канал — MS-вебхук `demand.CREATE` → `notify_new_shipment()` (мгновенно). `shipment_notifier` — РЕЗЕРВНЫЙ поллер (`CHECK_INTERVAL_SEC`, default 900), добирает пропущенное; дедуп общий.
- Шлём через `services.notifier.tg_send_message` (работает в любом процессе; токен не светится — `_redact_token`). Бот-созданные demand'ы (есть атрибут `telegram_user_id`) не уведомляем — `_is_bot_created`.

**Telegram + WebApp:**
- Пользовательский ввод в HTML → `utils.helpers.esc()`. Никогда не интерполируй `full_name` / `comment` / `agent_name` / `details` напрямую в `parse_mode="HTML"`.
- Каждый `/api/*` endpoint → `_authorize(data, allowed_roles=..., rate_limit_scope=...)`. Default role для новых юзеров — `guest` (нулевые права).
- Telegram WebApp initData валидируется в `webapp/auth.py` через `hmac.compare_digest`.

**МойСклад:**
- Не слать ссылки `https://online.moysklad.ru/app/#.../edit?id=...` в чат — backend access leak. PDF из `services/ms_customerorder._try_get_print_pdf` отправляется файлом.
- Error bodies в логах ВСЕГДА через `services.moysklad.redact_ms_error(body)`.
- Кастомные атрибуты привязаны к сущности (`demand` vs `customerorder`) — нельзя переиспользовать meta, иначе HTTP 400. На `salesreturn` атрибуты demand НЕ ставим (`services/ms_returns.py` их не шлёт).
- Подтверждение возврата создаёт «Возврат покупателя» (`entity/salesreturn`) — `services.ms_returns.create_salesreturn` (best-effort, gated через `ms_demand.is_ready()`, идемпотентно по `moysklad_return_id`, линкуется с исходной отгрузкой). Боевая проверка — `python -m tasks.verify_ms_returns` (read-only) → `--return-id N --create`.
- Любой paginated MS endpoint (`limit=100`-max) — крути offset-loop. Real пример: `services/moysklad.get_shipment_positions` (демонд может иметь >100 line items в B2B-заказе, иначе хвост молча теряется в `top_products` аналитики). Pattern — `services/snapshot._fetch_all`.
- Концурентность к МС ограничивай через `_get_positions_semaphore()`-style helper (loop-keyed lazy semaphore), а НЕ module-level `asyncio.Semaphore()` — последний биндится к первому loop'у при contention и валит cross-loop тесты с `RuntimeError: bound to a different event loop`. Cap=8 для positions сейчас.
- **Удаление заказа в МС → отмена в боте:** вебхук `customerorder.DELETE` (`ms_sync_handler.apply_ms_customerorder_delete`, общий с cron-реконсиляцией): `approved`-заказ отменяется локально (`cancel_order` + `set_order_ms_cancel_synced`, чтобы reverse в МС не пытался удалить уже-удалённое); shipped/paid — статус не трогаем (деньги/остатки двигались), только снимаем ссылку + предупреждаем. Раньше хендлер только снимал `ms_customerorder_id` → заказ висел `approved` (баг). `tasks/run_ms_reconcile` — страховка от пропущенных вебхуков.

**Кредит-лимиты (энфорс):** при одобрении credit-заявки `approve_shipment_request(..., override)` считает `check_credit_limit`; при превышении возвращает `needs_override` и НЕ одобряет → босс жмёт «Одобрить с превышением» (`req_ovr:` / webapp `override=true`), это ставит `orders.credit_limit_override` + audit. `get_agent_current_debt` считает долг батчем (items/payments/returns), без N+1.

**Аналитика менеджеров:** только из ЛОКАЛЬНЫХ `orders` (`get_manager_performance`, GROUP BY `user_id`) — в demand'ах МС нет надёжной привязки к менеджеру (`telegram_full_name` ставится лишь когда demand создал бот). Сурфейсится в webapp company-аналитике (`top_managers`).

**Backup:**
- `tasks/run_backup`: если `pg_dump` упал (нет бинаря — `FileNotFoundError`, ИЛИ `server version mismatch` — `RuntimeError`: бинарь старее managed-Postgres) → fallback на version-independent pure-Python COPY-dump (`_pg_dump_pure_python` через libpq). Лови ОБА исключения.
- `tasks/run_maintenance`: janitor чистит дедуп отгрузок, аудит старше ретеншена (`prune_audit_log`, `audit_log_retention_months`) и soft-deleted. Внешний архив аудита (Google Drive) убран.

**Time:** `utils.helpers.utc_now()` вместо deprecated `datetime.utcnow()`. `utils.helpers.local_now()` для сравнений с `created_at` (пишется в local TZ через `now_str()`).
- В SQL **не** сравнивай `confirmed_at`/`created_at` (local TZ string) с `datetime('now',...)` SQLite (UTC) или `NOW()` Postgres — лекс-сравнение в разных TZ молча всегда False (silent bug). Вычисляй порог в Python через `datetime.now() - timedelta(...)` и передавай параметром. Пример — `services/database.reset_stale_in_progress_payments`.

**Idempotency:** `/api/orders/{confirm_payment,mark_paid,reject_payment}` принимают `idempotency_key` через `_idem_get/_idem_set`. ВАЖНО: всегда вызывай `_idem_set` ПОСЛЕ успеха (был баг — `reject_payment` делал только `_idem_get` → ретрай слал дубль-уведомление). Применяй паттерн для новых write-endpoint'ов.

**Logging:**
- НЕ дёргай root logger (`logging.warning(...)`/`logging.info(...)`) на module-import time. Первый же вызов авто-триггерит `basicConfig(level=WARNING)` с дефолт-форматтером, и любой последующий `logging.basicConfig(level=INFO, ...)` в bot.py/tasks/run_*.py становится no-op → ВСЕ INFO глушатся на проде. Используй `logger = logging.getLogger(__name__)` — propagation идёт через `lastResort`, root остаётся чист.
- В cron-CLI (`tasks/run_*.py`) ставь `logging.basicConfig(level=INFO, format=...)` ДО любого импорта, который потенциально логирует на module-уровне. Сейчас все cron-CLI и `bot.py` это делают корректно.

**Cron-CLI стабильность:**
- Перед основной логикой вызывай идемпотентный reaper для своих долгоживущих in-flight состояний. Пример — `services.database.reset_stale_in_progress_payments(30)` в начале `tasks/run_ms_sync_retry`: сбрасывает orphan'ов 'in_progress' (claim был, но процесс убили до set_failed). Без него стуки залипают навсегда, потому что `claim_payment_for_ms_sync` отвергает уже-'in_progress'.
- В `tasks/run_ms_sync_retry`: pending-fetch ДО любого МС-вызова, `return 0` на пустой очереди ДО `init_demand_context()` — иначе 96 cron-тиков/день делают ~200-400 МС API calls впустую.
- `init_demand_context()` сам по себе НЕ raises (helpers `_pick_first`/`_ensure_custom_attribute` глотают exception); инспектируй возвращаемый dict (`ready/org/store/attribute_name/attribute_uid`), а не оборачивай в `try/except` (dead code).

## Security audit

`SECURITY.md` — исходный отчёт + Closed-таблица. Все пункты исходного аудита закрыты; новые фиксы этой сессии (esc agent_name в HTML, энфорс кредит-лимитов, деактивация=guest, reject_payment idempotency, индексы на горячих путях) дописаны в Closed.

## CI

`.github/workflows/ci.yml` на push в `main` или `claude/*`:
- `ruff` строгий гейт (`E9,F63,F7,F82`) + полный набор (из `pyproject.toml`);
- `mypy` — **блокирующий** (модули вычищены до 0 ошибок, тип-регрессии валят CI);
- `pytest` + coverage с «храповиком» `--cov-fail-under=35` (факт ~41%, буфер 5-6%).

`TELEGRAM_TOKEN=0:fake` и `MS_TOKEN=fake` — заглушки для импорта config (в workflow и в `tests/conftest.py` через `os.environ.setdefault`).

## Тесты (конвенция)

Мокай ГРАНИЦУ с внешним миром, а не свой код. Урок: баг в `tg_send_message` пережил CI, потому что тесты мокали саму `tg_send_message`. Для исходящих HTTP — `aioresponses` (мок транспорта, реально исполняется сборка URL/payload). БД — настоящая (`isolated_db`), не мок.

Если добавляешь module-level `asyncio.Semaphore`/`Lock` — добавь регресс-тест с 2× `asyncio.run` и contention >cap (см. `tests/test_analytics_parallel.py::test_positions_semaphore_survives_multiple_asyncio_run_with_contention`). Без waiter'а в очереди loop-binding не воспроизводится и landmine ждёт первого «толстого» теста.

**Фронт (Vitest):** чистые хелперы `webapp/static/helpers.js` + jsdom-смоук загрузки `app.js` — в `webapp/static/__tests__/`. Гоняет CI (`npm test`). Локально Node нет → `scripts/setup-node.ps1` ставит portable Node в `.tools/node` (gitignore, ~35MB zip с nodejs.org, без admin), `scripts/test-js.ps1` делает `npm install` (1×) + `vitest run`. Скрипты — UTF-8 **с BOM** (иначе PowerShell 5.1 читает их как ANSI и кириллица в Write-Host превращается в кракозябры).
