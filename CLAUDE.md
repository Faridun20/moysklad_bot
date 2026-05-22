# CLAUDE.md

## Commands

```bash
pip install -r requirements.txt -r requirements-dev.txt   # dev-зависимости запинены

pytest tests/                              # тесты (SQLite в /tmp, isolated_db fixture; env-дефолты в conftest)
ruff check .                               # полный набор из pyproject.toml (E9,F,B,ASYNC,UP,SIM)
ruff check --select=E9,F63,F7,F82 .        # строгий минимум-гейт (как блокирующий шаг CI)
mypy                                       # точечно по order_workflow/database/moysklad/server — гейт, 0 ошибок
pre-commit install && pre-commit install --hook-type pre-push   # локальная первая линия (опц.)

python bot.py                              # локально: без Postgres → SQLite, без Redis → MemoryStorage
python -m tasks.migrate                    # schema + data миграции, ДО старта сервисов на проде

# Cron CLIs (Railway Cron Jobs)
python -m tasks.run_report {daily|weekly|monthly}
python -m tasks.run_debts_notify
python -m tasks.run_ms_sync_retry
python -m tasks.run_ops_monitor          # дайджест: зависшие заявки/сдачи/возвраты/партии (1×/день)
python -m tasks.run_maintenance          # janitor: чистка дедупа/аудита/soft-deleted (ночью)
```

Конфиг тулчейна — в `pyproject.toml` (`[tool.ruff]`, `[tool.pytest.ini_options]`,
`[tool.mypy]`, `[tool.coverage]`). Версии dev-тулов запинены в `requirements-dev.txt`.

## Architecture

Детали в `ARCHITECTURE.md`. Главное: `BOT_MODE` env переключает процесс между `all` / `bot` / `webapp`. На Railway prod два сервиса (`moysklad_bot` + `Webapp`) с разными `BOT_MODE`, общий Postgres + Redis через Project Shared Variables.

## Conventions (нетривиальные)

**DB:**
- `init_db()` делает ТОЛЬКО `CREATE TABLE IF NOT EXISTS`. `ALTER TABLE` и backfill → в `run_migrations()` / `run_backfills()`, вызываются из `tasks/migrate.py`. **Не добавляй ALTER в init_db.**
- Webapp endpoint'ы: `services.async_db as adb` (`await adb.get_user(uid)`) — обёртка через `asyncio.to_thread`. В bot handlers — то же или явный `asyncio.to_thread`.
- Роль читай через `services.roles.cached_role(user_id)` (TTL 60s) и предикаты `is_boss / can_create_orders / ...`. НЕ через `services.database.get_role` напрямую — обойдёшь кэш.
- Race-чувствительные операции (`mark_order_paid`, `confirm_payment`) используют `SELECT ... FOR UPDATE` — сохраняй паттерн.
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
- Кастомные атрибуты привязаны к сущности (`demand` vs `customerorder`) — нельзя переиспользовать meta, иначе HTTP 400.

**Time:** `utils.helpers.utc_now()` вместо deprecated `datetime.utcnow()`. `utils.helpers.local_now()` для сравнений с `created_at` (пишется в local TZ через `now_str()`).

**Idempotency:** `/api/orders/confirm_payment` принимает `idempotency_key` через `_idem_get/_idem_set`. Применяй тот же паттерн для новых write-endpoint'ов.

## Security audit

`SECURITY.md` — исходный отчёт + Closed-таблица. На последнее обновление все 35 пунктов закрыты.

## CI

`.github/workflows/ci.yml` на push в `main` или `claude/*`:
- `ruff` строгий гейт (`E9,F63,F7,F82`) + полный набор (из `pyproject.toml`);
- `mypy` — **блокирующий** (модули вычищены до 0 ошибок, тип-регрессии валят CI);
- `pytest` + coverage с «храповиком» `--cov-fail-under=25`.

`TELEGRAM_TOKEN=0:fake` и `MS_TOKEN=fake` — заглушки для импорта config (в workflow и в `tests/conftest.py` через `os.environ.setdefault`).

## Тесты (конвенция)

Мокай ГРАНИЦУ с внешним миром, а не свой код. Урок: баг в `tg_send_message` пережил CI, потому что тесты мокали саму `tg_send_message`. Для исходящих HTTP — `aioresponses` (мок транспорта, реально исполняется сборка URL/payload). БД — настоящая (`isolated_db`), не мок.
