# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Tests (SQLite в /tmp, fixture в tests/conftest.py делает isolated_db)
pytest tests/                              # все 27 smoke-тестов
pytest tests/test_lifecycle.py             # один файл
pytest tests/test_lifecycle.py::test_X     # один тест
pytest tests/ -v --tb=short                # как в CI

# Lint (минимальный, как в CI — не whole-codebase enforcement)
ruff check --select=E9,F63,F7,F82 --statistics .

# Локальный запуск (без Postgres → SQLite, без Redis → MemoryStorage)
python bot.py

# Миграции БД — отдельный процесс, ПЕРЕД стартом сервисов на проде
python -m tasks.migrate

# Cron CLIs (использовать в Railway Cron Jobs)
python -m tasks.run_report {daily|weekly|monthly}
python -m tasks.run_debts_notify
python -m tasks.run_ms_sync_retry
```

## Architecture (high-level)

Подробно в `ARCHITECTURE.md`. Главное что нужно держать в голове:

**Три режима процесса через `BOT_MODE` env:**
- `all` — default, всё в одном процессе (локалка, маленький деплой)
- `bot` — только Telegram-loop + фоновые задачи, без FastAPI
- `webapp` — только FastAPI, опционально приём webhook'ов от Telegram

На Railway prod: два сервиса (`moysklad_bot` + `Webapp`) с разными `BOT_MODE` и общим Postgres + Redis через Project Shared Variables.

**Поток заказа:** менеджер собирает в WebApp → отправляет на approve → босс одобряет → бот создаёт **customerorder** в МойСклад (для PDF) + связанный **demand** (для списания остатков) + сразу же auto-создаёт `payment` для подтверждения «деньги получены». Босс подтверждает платёж → создаётся `paymentin` в МойСклад (синк через `services.ms_payments`).

Credit-долги ходят отдельной двух-ступенчатой схемой через `mark_order_paid` → `confirm_payment` с поддержкой частичных платежей.

## Conventions (нетривиальные, прочитай перед правкой)

**DB-слой:**
- `services/database.py` — psycopg2 (sync) с `ThreadedConnectionPool`. На рантайме `init_db()` делает ТОЛЬКО `CREATE TABLE IF NOT EXISTS` — schema migrations (`ALTER TABLE`) и data-миграции (backfill/recovery) живут в `run_migrations()` / `run_backfills()` и вызываются из `tasks/migrate.py`. **Не добавляй ALTER в init_db.**
- В webapp endpoint'ах используй `services.async_db as adb`: `await adb.get_user(uid)`. Под капотом `asyncio.to_thread`-обёртка через module-level `__getattr__`, не блокирует event loop. В bot handlers — либо `services.async_db`, либо явный `asyncio.to_thread(sync_fn, ...)`.
- Роль читай только через `services.roles.cached_role(user_id)` (TTL 60s) и предикаты `is_boss / can_create_orders / ...` — НЕ через `services.database.get_role` напрямую (тогда обходишь кэш).
- Race-чувствительные операции (`mark_order_paid`, `confirm_payment`) уже используют `SELECT ... FOR UPDATE` — сохраняй паттерн для новых.

**Telegram + WebApp:**
- Любой пользовательский ввод в HTML-сообщения → `utils.helpers.esc()`. Никогда не интерполируй `full_name` / `comment` / `agent_name` / `details` напрямую в `parse_mode="HTML"`.
- Каждый `/api/*` endpoint проверяет роль через `_authorize(data, allowed_roles=..., rate_limit_scope=...)`. Default role for new users is `guest` (нулевые права).
- Telegram WebApp initData валидируется в `webapp/auth.py` через `hmac.compare_digest`.

**МойСклад:**
- Не отправляй ссылки `https://online.moysklad.ru/app/#.../edit?id=...` в чат — это backend access leak. PDF печатной формы качается из `services/ms_customerorder._try_get_print_pdf` и отправляется файлом.
- Error bodies от МойСклад в логах ВСЕГДА через `services.moysklad.redact_ms_error(body)` — PII redaction.
- Кастомные атрибуты в МойСклад привязаны к конкретной сущности (`demand` vs `customerorder`) — нельзя переиспользовать meta между ними, иначе HTTP 400 «wrong type».

**Time:**
- `utils.helpers.utc_now()` вместо deprecated `datetime.utcnow()`.
- `utils.helpers.local_now()` для сравнений с `created_at` (тот пишется в local TZ через `now_str()`).

**Idempotency:**
- `/api/orders/confirm_payment` принимает `idempotency_key` для защиты от double-click. Если добавляешь новые write-endpoint'ы — рассмотри тот же паттерн через `_idem_get/_idem_set`.

## Что было закрыто и что осталось

`SECURITY.md` содержит исходный аудит-отчёт + Closed-таблицу со ссылками на коммиты. На момент последнего обновления все 35 пунктов (3 Critical, 12 High, 16 Medium, 4 Low) закрыты. Если хочешь оценить риски новых изменений — сверяйся с этим списком.

## CI

`.github/workflows/ci.yml` гоняет `ruff check --select=E9,F63,F7,F82` + `pytest tests/` на каждый push в `main` или ветки `claude/*`. Тестам нужны `TELEGRAM_TOKEN=0:fake` и `MS_TOKEN=fake` (заглушки для импорта config) — это уже прописано в workflow.
