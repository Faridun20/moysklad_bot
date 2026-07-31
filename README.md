# МойСклад · Telegram-бот учёта заказов

Внутренний инструмент компании для оперативного учёта заказов поверх облачного
сервиса [МойСклад](https://moysklad.ru). Менеджеры собирают заказы прямо в
Telegram, руководители одобряют отгрузки и подтверждают поступление денег,
бот автоматически создаёт документы в МойСклад и держит баланс по контрагентам
в актуальном состоянии.

## Что умеет

| Роль | Действия |
|---|---|
| **Менеджер** | Собирает заказы (товары + клиент + цены), отправляет на одобрение. Помечает оплаты по credit-заказам. Сдаёт наличные, оформляет возвраты — в WebApp; платёж в кассу — `/pay` в боте. |
| **Босс** | Одобряет/отклоняет заявки кнопками в уведомлении (+ «✏️ На доработку», «✅ Одобрить с превышением лимита»). Подтверждает деньги. Кредит-лимиты, курсы, цены, долги, аналитика — в WebApp; аудит — `/audit`. |
| **Бухгалтер** | Подтверждает сдачи наличных; ведёт цены/курсы. |
| **Кладовщик** | Фиксирует отгрузку (`/ship`), обрабатывает возвраты, «товар получен». |
| **Админ** | Всё выше + роли (`/addrole`), деактивация юзеров (`/deactivate`), `/frozen` (разморозка), синк с МойСклад, аудит. |

**Главные сценарии:**
- Менеджер → заказ → push боссу → одобрение → автоматическое создание `customerorder` + `demand` в МойСклад с PDF печатной формой в чате
- **Reject→draft + freeze:** босс возвращает заявку на доработку с причиной; после 3 циклов заказ замораживается до разморозки админом
- **Кредит-лимиты с энфорсом:** превышение лимита требует явного одобрения «с превышением» (в аудите)
- **Возвраты** (полные/частичные) → «Возврат покупателя» в МойСклад; **сдачи наличных** закрывают заказы (FIFO)
- Учёт долгов с напоминаниями: ежедневно в 9:00 менеджер — свои неоплаченные, босс — сводку (+ единый остаток «≈ X USD» по всем валютам)
- Частичные оплаты; двухступенчатое подтверждение → `paymentin` в МойСклад
- **Синхронизация удаления:** удалил заказ покупателя в МойСклад → бот отменяет локальный (вебхук + ежечасная реконсиляция)
- **Аналитика:** ежедневные/недельные/месячные отчёты (Cron) + WebApp-дашборд с разрезом **по менеджерам** (заказы/выручка/долг)

## Стек

- **Python 3.11**, asyncio
- **aiogram 3.7** — Telegram Bot API, FSM с Redis или MemoryStorage
- **FastAPI + uvicorn** — WebApp (Telegram Mini App)
- **PostgreSQL**: денежное ядро (заказы/платежи/кредит/сдачи/возвраты) — на **`asyncpg`** (native async, `services/adb_core.py`; в тестах `aiosqlite`). Остальное — `psycopg2-binary` + `ThreadedConnectionPool`, обёрнут в `asyncio.to_thread`.
- **Redis** (опционально) — FSM storage и кэши
- **МойСклад REST API 1.2** — отгрузки, заказы покупателей, входящие платежи, справочники, webhook'и (создание/изменение/удаление документов)
- **Деплой**: [Railway](https://railway.app), **Railpack** билд (`railpack.json`) — пошагово в [DEPLOY.md](DEPLOY.md)

## Быстрый старт

### Локально (для разработки)

1. Склонируй репо:
   ```bash
   git clone https://github.com/Faridun20/moysklad_bot.git
   cd moysklad_bot
   ```
2. Установи зависимости:
   ```bash
   pip install -r requirements.txt
   ```
3. Создай `config_local.py` (этот файл в `.gitignore`):
   ```python
   TELEGRAM_TOKEN = "1234:AAA..."
   MS_TOKEN = "ваш-токен-МойСклад"
   ADMIN_IDS = [123456789]  # твой Telegram user_id
   BOSS_IDS = []
   MANAGER_IDS = []
   ALLOWED_USERS = []
   BASE_CURRENCY = "USD"
   TZ_OFFSET = 5
   CHECK_INTERVAL_SEC = 900   # интервал РЕЗЕРВНОГО поллера отгрузок (осн. канал — вебхук)
   ```
   Без `DATABASE_URL` бот использует SQLite в `/tmp/payments.db`.
   Без `REDIS_URL` — `MemoryStorage` (FSM сбрасывается на рестарте).
4. Запусти:
   ```bash
   python bot.py
   ```
5. В Telegram напиши боту `/start` — должен ответить.

### Визуальная проверка WebApp в браузере (без Telegram)

Чтобы посмотреть интерфейс WebApp локально в обычном браузере (вёрстка, тёмная
тема, экраны Главная/Заказы/Финансы/Аналитика), нужен **dev-обход авторизации** и
**примерные данные** — Telegram-авторизация (`initData`) в браузере недоступна, а
свежая БД пуста.

PowerShell (Windows):
```powershell
$env:TELEGRAM_TOKEN='0:fake'; $env:MS_TOKEN='fake'   # для UI-only достаточно заглушек
$env:DEV_AUTH_BYPASS='1'; $env:DEV_USER_ID='999000001'
$env:BOT_MODE='webapp'        # ТОЛЬКО FastAPI: без Telegram-поллинга (фейк-токен иначе уронит процесс)
python -m tasks.seed_dev      # наполнить локальную SQLite примерными данными (идемпотентно)
python bot.py                 # uvicorn на http://localhost:8080
```
bash/macOS/Linux:
```bash
export TELEGRAM_TOKEN='0:fake' MS_TOKEN='fake' DEV_AUTH_BYPASS=1 DEV_USER_ID=999000001 BOT_MODE=webapp
python -m tasks.seed_dev && python bot.py
```
Открой **http://localhost:8080**. Тёмная тема — переключи тему ОС (CSS реагирует на
`prefers-color-scheme` вне Telegram).

> **Важно:** `BOT_MODE=webapp` обязателен с заглушкой-токеном — иначе `bot.py`
> (режим `all` по умолчанию) попытается запустить Telegram-поллинг и упадёт с
> `TelegramUnauthorizedError: invalid token`. В режиме `webapp` поднимается только
> FastAPI (это и нужно для просмотра UI). MS-ошибки `401 Unauthorized` в логе при
> фейковом `MS_TOKEN` — ожидаемы и не мешают.

- `DEV_AUTH_BYPASS=1` пускает синтетического dev-юзера (роль берётся из БД; сид даёт
  ему `admin`). **Предохранитель:** при заданном `DATABASE_URL` (прод/Postgres) обход
  автоматически отключается — на проде он бесполезен и безопасен.
- Без реального `MS_TOKEN` экраны **Каталог/сток** и **MS-балансы** во вкладке
  «Клиенты» останутся пустыми — данные тянутся из МойСклад вживую. Остальные экраны
  (заказы, долги, платежи, аналитика по локальным заказам) наполняются сидом.
- **Аналитика:** у `admin`/`boss` вкладка «Продажи» считается из МойСклад (локально
  пустая). Личная аналитика менеджера (`_personal_analytics`) — из локальных заказов.
  Чтобы увидеть графики/топ-товары на сид-данных, открой под ролью менеджера:
  `$env:DEV_USER_ID='999000002'` (Алиса, manager) — сид заводит ей заказы по дням.
- БД по умолчанию — `%TEMP%/payments.db` (Windows) / `/tmp/payments.db`. Задай
  `$env:DB_PATH` (или `DB_PATH=`), чтобы файл переживал перезапуски.

### Деплой на Railway

Пошаговый выкат с нуля — **[DEPLOY.md](DEPLOY.md)** (порядок сервисов, семь
cron-задач, проверка после выката, грабли). Почему топология именно такая —
[ARCHITECTURE.md → раздел 2](ARCHITECTURE.md#2-топология-railway).

Кратко:
1. Создай проект, добавь `Postgres`, `Redis` (опц.), сервисы `bot`/`webapp` и cron'ы.
2. Прогони `python -m tasks.migrate` ДО первого старта сервисов.
3. Переменные окружения — в **Project Shared Variables** (список ниже).
4. `BOT_MODE=bot` и `BOT_MODE=webapp` — переменными конкретных сервисов.
5. На `webapp` — Settings → Healthcheck Path: `/healthz`, затем положи выданный
   домен в `WEBAPP_URL` и **передеплой bot-сервис** (он регистрирует вебхуки МС).

## Переменные окружения

Полный список с пояснениями — в [ARCHITECTURE.md → раздел 2](ARCHITECTURE.md#2-топология-railway). Минимум для запуска:

| Переменная | Назначение |
|---|---|
| `TELEGRAM_TOKEN` | Токен бота от @BotFather |
| `MS_TOKEN` | API-токен МойСклад |
| `DATABASE_URL` | Postgres-подключение, `${{Postgres.DATABASE_URL}}` в Railway |
| `WEBAPP_URL` | Публичный домен webapp-сервиса (с `https://`, без `/` в конце) |
| `MS_WEBHOOK_SECRET` | Секрет для URL вебхука МойСклад (любая случайная строка ≥32 символов) |
| `ADMIN_IDS` | CSV Telegram ID админов (через запятую) |

Опционально:
| `REDIS_URL` | Если задан — FSM в Redis, иначе в памяти |
| `TG_USE_WEBHOOK=1` + `TG_WEBHOOK_SECRET` | Переключить с polling на webhook |
| `BOT_MODE` | `all` (default) / `bot` / `webapp` — что запускать в этом процессе |
| `BASE_CURRENCY` | По умолчанию `USD` |
| `TZ` | `Asia/Tashkent` или другой |
| `PG_POOL_MIN`, `PG_POOL_MAX` | Размер пула коннектов Postgres (default 1/10) |
| `BACKUP_TG_CHAT_ID` | ID приватного TG-канала для ежедневного backup БД |
| `MACHINE_PHOTOS_TG_CHAT_ID` | ID приватного TG-канала для фото техники, загруженных из WebApp. Без него загрузка выключена (фото можно прислать боту) |
| `DEV_AUTH_BYPASS=1` + `DEV_USER_ID` | **Только локально:** обход Telegram-авторизации для просмотра WebApp в браузере (самоблокируется при `DATABASE_URL`). См. «Визуальная проверка WebApp» выше |

> **Telegram webhook** (вместо polling, опц., только ops): `TG_USE_WEBHOOK=1` +
> `TG_WEBHOOK_SECRET` при заданном публичном `WEBAPP_URL`. Работает ТОЛЬКО в
> одиночном `BOT_MODE=all`: процесс с `BOT_MODE=bot` не поднимает FastAPI,
> уходит в polling и снимает чужой вебхук перед стартом. Подробно — в
> [DEPLOY.md](DEPLOY.md#грабли-на-которые-легко-наступить).

## Backup БД в Telegram-канал

Ежедневный backup Postgres (или SQLite для dev) — `pg_dump | gzip` → upload в приватный Telegram-канал через того же бота. Без AWS/Google Cloud, восстановление = `gunzip | psql`.

**Setup (5 минут):**
1. Создать **приватный** канал в Telegram
2. Добавить бота как Administrator (right: Post Messages)
3. Получить `chat_id` канала: forward любое сообщение оттуда в [@userinfobot](https://t.me/userinfobot), id вида `-100xxxxxxxxxx`
4. Поставить в Railway env cron-сервиса:
   - `BACKUP_TG_CHAT_ID=-100xxxxxxxxxx`
5. Создать Railway Cron Job:
   - Command: `python -m tasks.run_backup`
   - Schedule: `0 3 * * *` (3:00 UTC = 8:00 Ташкент)

**Лимит:** Telegram Bot API = 50 MB на файл. У БД сейчас сильный запас, при росте >40 MB cron шлёт WARNING; при >50 MB upload не запускается (нужен переход на B2/R2).

**Мониторинг:** `tasks/run_backup` интегрирован с `cron_runs` — если backup не прошёл, `cron-ops` дайджест покажет «🛑 backup: failed».

**Два режима дампа:**
1. **pg_dump** (full schema + data) — если postgresql client есть в образе (Railpack `railpack.json` → `deploy.aptPackages: [postgresql-client]`)
2. **pure-Python COPY-dump** (data-only) — fallback через psycopg2/libpq, если `pg_dump` **отсутствует** ИЛИ **упал** (частый кейс: `server version mismatch` — managed-Postgres новее, чем pg_dump из apt). Версионно-независим, работает без системных бинарей.

**Восстановление:**
```bash
gunzip moysklad-bot-postgres-YYYYMMDD-HHMMSS.sql.gz

# Если дамп был через pg_dump (full):
psql $DATABASE_URL < moysklad-bot-postgres-YYYYMMDD-HHMMSS.sql

# Если дамп pure-Python (data-only) — сначала создать схему:
python -m tasks.migrate
psql $DATABASE_URL < moysklad-bot-postgres-YYYYMMDD-HHMMSS.sql
```
Тип дампа виден в первой строке файла (`-- Pure-Python COPY dump` или `-- PostgreSQL database dump`).


## Команды бота

Бот — уведомления и решения по ним; вся работа (заказы, каталог, финансы,
аналитика) в WebApp. Снятые в T3.3 команды (`/neworder`, `/myorders`, `/orders`,
`/stock`, `/categories`, `/debts`, `/deposit`, `/deposits`, `/my_deposits`,
`/return`, `/returns`, `/limit`, `/rates`, `/prices`, `/analytics`, `/cashbox`,
`/payreport`) отвечают подсказкой, на каком экране WebApp искать операцию.

### Менеджер
- `/start` — главное меню (кнопка входа в WebApp)
- `/pay` — платёж в кассу (не привязан к заказу)
- `/find` — поиск (заказ / платёж / клиент)

### Босс
- Всё выше +
- `/ship` — отгрузить · `/shipments` — последние отгрузки
- `/cancel` — отменить заказ
- `/audit` — аудит-лог · `/sync_payments` — статус синка (+ Retry)
- `/snapshot`, `/refresh` — кэш МойСклад

### Бухгалтер / кладовщик
- Решения приходят кнопками в уведомлении: подтвердить/отклонить сдачу,
  отметить приёмку товара, подтвердить возврат.
- Кладовщик: `/ship`, `/shipments`.

### Только админ
- `/addrole <user_id> <admin|boss|manager|bookkeeper|warehouse_keeper|guest>` — роль
- `/users` — список · `/deactivate <id>` / `/reactivate <id>` — доступ
- `/frozen` — замороженные заказы (разморозка)
- `/syncms`, `/msstaff` — синхронизация с сотрудниками МойСклад

## Структура репо

```
.
├── bot.py                    Точка входа, ветвление по BOT_MODE
├── config.py                 Все env-переменные в одном месте
├── ARCHITECTURE.md           Подробная архитектура (для разработчиков)
│
├── handlers/                 aiogram-роутеры (Telegram-команды)
│   ├── start.py              /start, главное меню
│   ├── orders.py             Создание/одобрение заказов
│   ├── debts.py              Долги + двухступенчатое подтверждение
│   ├── payments.py           /pay (отдельные платежи) + /sync_payments
│   ├── shipments.py          Просмотр отгрузок
│   ├── analytics.py          Аналитика
│   ├── reports.py            Ручной запуск отчётов
│   ├── users.py              Управление ролями (/addrole, /users)
│   ├── audit.py              Аудит-лог (/audit)
│   └── log.py                Логи в чате (/log)
│
├── services/                 Бизнес-логика и интеграции
│   ├── database.py           Postgres/SQLite + ThreadedConnectionPool (sync-часть)
│   ├── adb_core.py           Native async DB (asyncpg/aiosqlite) — денежное ядро
│   ├── async_db.py           Async-обёртка для sync-функций (через to_thread)
│   ├── money.py              Деньги в копейках (*_cents), конвертация
│   ├── roles.py              Роли + TTL-кэш на 60с + деактивация
│   ├── ms_cancel.py          Реверс customerorder при отмене заказа
│   ├── ms_returns.py         «Возврат покупателя» (salesreturn)
│   ├── moysklad.py           Базовый HTTP-клиент МойСклад
│   ├── ms_demand.py          Создание отгрузок (demand)
│   ├── ms_customerorder.py   Создание заказов покупателей + PDF
│   ├── ms_payments.py        Синхронизация платежей (paymentin)
│   ├── ms_webhooks.py        Подписки на webhook'и МойСклад
│   ├── ms_sync.py            Связь менеджеров с сотрудниками МойСклад
│   ├── ms_sync_handler.py    Обработка вебхук-событий (paymentin/customerorder)
│   ├── snapshot.py           Локальный кэш справочников МойСклад
│   ├── order_workflow.py     Машина состояний заказа + апрув/реджект заявки
│   ├── notifier.py           Событийные уведомления об отгрузках + резервный поллер + TG-сессия
│   ├── notify.py             Хелперы уведомлений менеджеру (approved/rejected)
│   └── rate_limit.py         In-memory rate-limiter
│
├── tasks/                    Фоновые задачи + CLI для Railway Cron
│   ├── migrate.py            Schema + data миграции (ДО старта сервисов)
│   ├── scheduled.py          In-process snapshot-refresh (отчёты убраны → WebApp)
│   ├── run_debts_notify.py   CLI: утреннее напоминание о долгах
│   ├── run_ms_sync_retry.py  CLI: ретрай failed paymentin-синков
│   ├── run_ms_reconcile.py   CLI: реконсиляция удалённых в МС заказов
│   ├── run_ops_monitor.py    CLI: операционный дайджест с кнопками
│   ├── run_maintenance.py    CLI: janitor (чистка дедупа/аудита/soft-deleted)
│   └── run_backup.py         CLI: дамп БД → приватный TG-канал
│
├── webapp/                   FastAPI + статика (Telegram Mini App)
│   ├── server.py             API endpoint'ы
│   ├── auth.py               Валидация Telegram.WebApp.initData
│   └── static/               vanilla JS, index.html, style.css
│
├── utils/                    Утилиты
│   ├── helpers.py            extract_id_from_href, safe-error и т.п.
│   ├── formatters.py         HTML/Markdown-форматтеры сообщений
│   └── keyboards.py          aiogram-клавиатуры
│
├── tests/                    pytest (isolated_db fixture, мок транспорта)
├── pyproject.toml            конфиг ruff / pytest / mypy / coverage
├── requirements-dev.txt      запиненные dev-тулзы (pytest, ruff, mypy, aioresponses…)
└── .pre-commit-config.yaml   ruff + ruff-format (+ pytest на pre-push)
```

## Поток данных при типичном заказе

```
[Менеджер]
    ↓ (WebApp: Заказы → «Новый заказ»)
order (status=draft)
    ↓ (Отправить заявку)
shipment_request (pending)  →  push боссу
    ↓ (Босс: Одобрить)
1. POST /entity/customerorder  → customerorder в МойСклад
2. GET PDF печатной формы      → file в чат менеджеру + боссу
3. POST /entity/demand         → demand линкованный с customerorder
   (остатки списываются)
4. order.status=approved
    ↓ (если credit) Менеджер: «Деньги получил» (полная или частичная сумма)
payment (status=pending)  →  push боссу
    ↓ (Босс: Принять)
1. payment.status=confirmed
2. order.paid_at, paid_confirmed_at = now
3. POST /entity/paymentin      → платёж в МойСклад (привязан к customerorder)
4. Если сумма confirmed == total → order закрыт полностью
```

Подробно с диаграммами состояний — [ARCHITECTURE.md → раздел 5](ARCHITECTURE.md#5-воркфлоу).

## Безопасность

- WebApp использует `Telegram.WebApp.initData` HMAC-подпись для аутентификации (`webapp/auth.py`)
- Все API endpoint'ы валидируют initData + проверяют роль через `_authorize`
- МойСклад-webhook защищён `MS_WEBHOOK_SECRET` в URL
- Telegram-webhook (если включён) — двойная защита: секрет в URL + header `X-Telegram-Bot-Api-Secret-Token`
- Никакие пароли/токены не логируются в открытом виде
- Edit-URL на бэкенд МойСклад **не** отправляются пользователям (только PDF-файлы и текстовые описания)

Найденные при последнем аудите вопросы (Critical / High / Medium / Low) —
см. **[SECURITY.md](SECURITY.md)**. Там же лежит приоритизированный
top-5 «что чинить первым» и таблица **Closed** с ссылками на коммиты.
Последние раунды (4-5, май 2026) закрыли cron-стабильность
(`run_ms_sync_retry` orphan-reaper + init-context + early-noop), MS
`/positions` pagination и race в `_ms_ttl_cache.cache_clear`.

## Документация

- **[DEPLOY.md](DEPLOY.md)** — выкат на Railway с нуля: порядок сервисов, cron-задачи, проверка после деплоя, известные грабли.
- **[ARCHITECTURE.md](ARCHITECTURE.md)** — полная техническая архитектура: топология сервисов, схема БД, API endpoint'ы, workflow'ы, интеграции, операционные нюансы. Читать если ты новый разработчик.
- **[SECURITY.md](SECURITY.md)** — security & code audit с приоритизацией: уязвимости, race-conditions, технический долг. Раньше всего нового кода — проверь, не закрывает ли он что-то из top-5.
- **Этот README** — лицо проекта: что, как запустить, базовые команды.

## Дополнительно

### Тестирование

В репо есть pytest-набор (`tests/`) и CI (`.github/workflows/ci.yml`):

```bash
pip install -r requirements.txt -r requirements-dev.txt
pytest tests/          # SQLite в /tmp (isolated_db), env-заглушки в conftest
ruff check .           # линт (E9,F,B,ASYNC,UP,SIM из pyproject.toml)
mypy                   # типы по «денежным»/API-модулям (блокирующий гейт)
```

Принцип тестов: мокаем **границу с внешним миром** (HTTP-транспорт через `aioresponses`,
Telegram-`tg_send_message` на верхнем уровне), а БД — настоящая. Покрываются денежные
инварианты, контракт МойСклад, регрессии безопасности (`_authorize`, HTML-escape) и
дедуп уведомлений.

**Фронт WebApp (Vitest).** Хелперы `webapp/static/helpers.js` и jsdom-смоук загрузки
`app.js` лежат в `webapp/static/__tests__/` и гоняются в CI (`npm test`). Локально на
Windows без Node/пакетных менеджеров — два PowerShell-скрипта (portable Node ставится
в `.tools/`, в репозиторий не попадает):

```powershell
powershell -ExecutionPolicy Bypass -File scripts/setup-node.ps1   # 1×: portable Node LTS → .tools/node
powershell -ExecutionPolicy Bypass -File scripts/test-js.ps1      # npm install (1×) + vitest run
# из cmd / двойным кликом: scripts\test-js.cmd
```

### Логи и мониторинг

- Railway хранит логи 7 дней
- Опционально: `Better Stack` / `Axiom` через Railway Log Drain (см. ARCHITECTURE.md)
- В коде включён `SQL_SLOW_MS=200` — все запросы дольше 200мс пишутся как WARNING

### Поддержка

Это внутренний инструмент компании, не предназначен для широкого использования.
По вопросам — обращайся к авторам репозитория.

## Лицензия

Внутренний код компании, не предназначен для публичного распространения.
