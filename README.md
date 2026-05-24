# МойСклад · Telegram-бот учёта заказов

Внутренний инструмент компании для оперативного учёта заказов поверх облачного
сервиса [МойСклад](https://moysklad.ru). Менеджеры собирают заказы прямо в
Telegram, руководители одобряют отгрузки и подтверждают поступление денег,
бот автоматически создаёт документы в МойСклад и держит баланс по контрагентам
в актуальном состоянии.

## Что умеет

| Роль | Действия |
|---|---|
| **Менеджер** | Собирает заказы (товары + клиент + цены), отправляет на одобрение. Помечает оплаты по credit-заказам. Отдельные платежи в кассу через `/pay`. |
| **Босс / админ** | Одобряет/отклоняет заявки на отгрузку. Подтверждает поступление денег в кассу. Видит долги, аналитику, аудит. |
| **Админ** | Управляет ролями (`/addrole`), синхронизирует менеджеров с МойСклад, видит весь аудит. |

**Главные сценарии:**
- Менеджер → заказ → push боссу → одобрение → автоматическое создание `customerorder` + `demand` в МойСклад с PDF печатной формой в чате
- Учёт долгов с напоминаниями: ежедневно в 9:00 каждый менеджер получает свои неоплаченные, босс — сводку по компании
- Частичные оплаты: клиент платит частями, бот ведёт остаток
- Двухступенчатое подтверждение: менеджер отметил «деньги получил» → босс жмёт «Принять» → автоматически создаётся `paymentin` в МойСклад
- Sales-аналитика: ежедневные, еженедельные, месячные отчёты (Cron Jobs)

## Стек

- **Python 3.11**, asyncio
- **aiogram 3.7** — Telegram Bot API, FSM с Redis или MemoryStorage
- **FastAPI + uvicorn** — WebApp (Telegram Mini App)
- **PostgreSQL** через `psycopg2-binary` с `ThreadedConnectionPool`. Async-обёртка через `asyncio.to_thread` для FastAPI endpoint'ов.
- **Redis** (опционально) — FSM storage и кэши
- **МойСклад REST API 1.2** — отгрузки, заказы покупателей, входящие платежи, справочники
- **Деплой**: [Railway](https://railway.app), Nixpacks билд

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

### Деплой на Railway

См. подробный гайд в [ARCHITECTURE.md → раздел 2: Топология Railway](ARCHITECTURE.md#2-топология-railway).

Кратко:
1. Создай проект, добавь сервисы: `bot`, `webapp`, `Postgres`, `Redis` (опц.), + cron-сервисы.
2. Подключи репо к каждому сервису через GitHub integration.
3. Переменные окружения — в **Project Shared Variables** (см. полный список ниже).
4. На `bot` и `webapp` сервисах поставь `BOT_MODE=bot` и `BOT_MODE=webapp` соответственно.
5. На `webapp` — Settings → Healthcheck Path: `/healthz`.

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
| `ENABLE_SCHEDULED_REPORTS` | `0` чтобы отчёты не дублировались с cron-сервисом |
| `PG_POOL_MIN`, `PG_POOL_MAX` | Размер пула коннектов Postgres (default 1/10) |

## Команды бота

### Менеджер
- `/start` — главное меню (открыть WebApp / «Мои заказы» / «Создать заказ»)
- `/neworder` — быстро создать новый заказ
- `/myorders` — мои заказы
- `/pay` — отправить платёж в кассу (не привязан к заказу)
- `/debts` — мои открытые долги

### Босс / админ
- Всё перечисленное выше +
- `/orders` — все заказы компании
- `/shipments` — последние отгрузки в МойСклад
- `/analytics` — аналитика продаж
- `/audit` — аудит-лог действий
- `/sync_payments` — статус синхронизации платежей с МойСклад (+ кнопка Retry)
- `/snapshot` — статистика локального кэша МойСклад
- `/refresh` — принудительно обновить snapshot

### Только админ
- `/addrole <user_id> <admin|boss|manager|guest>` — назначить роль
- `/users` — список пользователей
- `/syncms` — синхронизировать менеджеров с сотрудниками МойСклад
- `/msstaff` — список сотрудников из МойСклад

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
│   ├── database.py           Postgres/SQLite + ThreadedConnectionPool
│   ├── async_db.py           Async-обёртка для FastAPI (через to_thread)
│   ├── roles.py              Роли + TTL-кэш на 60с
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
│   ├── scheduled.py          In-process daily/weekly/monthly отчёты
│   ├── run_report.py         CLI: `python -m tasks.run_report daily`
│   ├── run_debts_notify.py   CLI: утреннее напоминание о долгах
│   └── run_ms_sync_retry.py  CLI: ретрай failed paymentin-синков
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
    ↓ (WebApp / /neworder)
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
дедуп уведомлений. UI WebApp всё ещё проверяется вручную после деплоя.

### Логи и мониторинг

- Railway хранит логи 7 дней
- Опционально: `Better Stack` / `Axiom` через Railway Log Drain (см. ARCHITECTURE.md)
- В коде включён `SQL_SLOW_MS=200` — все запросы дольше 200мс пишутся как WARNING

### Поддержка

Это внутренний инструмент компании, не предназначен для широкого использования.
По вопросам — обращайся к авторам репозитория.

## Лицензия

Внутренний код компании, не предназначен для публичного распространения.
