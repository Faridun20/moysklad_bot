# Архитектура

Этот документ описывает, как устроено приложение на сегодняшний день. Цель —
дать новому разработчику или себе через полгода полную карту: какие сервисы
крутятся в продакшене, какие таблицы в БД, какие потоки данных проходят
через систему, и где искать конкретные вещи.

Документ описывает реальное состояние кода, а не план. При значимых
изменениях архитектуры — обновляй здесь же.

---

## 1. Что это вообще такое

Telegram-бот + Web App (Mini App в чате) для управления заказами и
отгрузками поверх облачного товароучётного сервиса **МойСклад**. Внутренний
инструмент компании: менеджеры собирают заказы и фиксируют оплаты,
руководители одобряют отгрузки и подтверждают поступление денег. Снимок
склада/каталога локально кешируется в Postgres, чтобы UI работал мгновенно
и не упирался в rate-limit'ы МойСклад.

Ключевые сущности:

- **Заказ** (`orders`) — что менеджер собрал. Может быть в долг.
- **Заявка на отгрузку** (`shipment_requests`) — заказ, отправленный
  на одобрение руководителю.
- **Платёж** (`payments`) — отдельная сущность для произвольных
  переводов денег между менеджером и кассой (не связаны с заказами
  напрямую).
- **Долг** — не отдельная таблица: это заказ с
  `payment_type='credit' AND paid_confirmed_at IS NULL`.

---

## 2. Топология Railway

Проект `strong-wisdom` в Railway содержит четыре сервиса:

```
                ┌──────────────────┐
                │  moysklad_bot    │  Python, BOT_MODE=bot
                │  - polling       │  Telegram Bot API → бот
                │  - notifier      │  фон: РЕЗЕРВНЫЙ поллер отгрузок
                │  - snapshot      │  фон: обновление кеша справочников
                └────────┬─────────┘
                         │
                         │ DB-ссылки
                         ▼
        ┌────────────────┐        ┌────────────┐
        │   Postgres     │◄──────►│   Redis    │  aiogram FSM storage
        │  postgres-     │        │  redis-    │  (черновики заказов
        │   volume       │        │   volume   │   переживают редеплой)
        └────────────────┘        └────────────┘
                         ▲
                         │ DB-ссылки
                         │
                ┌────────┴─────────┐
                │     Webapp       │  Python, BOT_MODE=webapp
                │  - FastAPI       │  принимает /api/* запросы от UI
                │  - /healthz      │  healthcheck Railway
                │  - /api/ms-webhook/<secret>  ← вебхук от МойСклад
                │  - статика app.js/index.html │
                └──────────────────┘
                         ▲
                         │  HTTPS
                         │
              ┌──────────┴───────────┐
              │  Пользователь        │
              │  Telegram → WebApp   │
              └──────────────────────┘
```

Дополнительно (Railway Cron Jobs):

- **`cron-debts`** — `python -m tasks.run_debts_notify` ежедневно ~6:00 UTC.
- **`cron-ops`** — `python -m tasks.run_ops_monitor` 1×/день: короткий пинг
  «есть N событий — откройте WebApp» (сводки/отчёты смотрят в WebApp).
- **`cron-ms-reconcile`** — `python -m tasks.run_ms_reconcile` ежечасно.
- (Отдельных `cron-daily/weekly/monthly` для отчётов больше нет — отчёты и
  аналитику смотрят в WebApp.)

Все cron-сервисы — однократные процессы Railway Cron Jobs, поднимаются
по расписанию, выполняют действие, завершаются.

### Переменные окружения

Хранятся как Project Shared Variables, оба основных сервиса наследуют их
через `${{shared.NAME}}`:

| Переменная | Назначение |
|---|---|
| `TELEGRAM_TOKEN` | Токен бота от @BotFather |
| `MS_TOKEN` | API-токен МойСклад |
| `DATABASE_URL` | Postgres-подключение (`${{Postgres.DATABASE_URL}}`) |
| `REDIS_URL` | Redis (`${{Redis.REDIS_URL}}`). Если пусто — FSM работает в памяти |
| `MS_WEBHOOK_SECRET` | Секрет в URL вебхука МойСклад |
| `WEBAPP_URL` | Публичный домен webapp-сервиса (без `/` в конце) |
| `TG_USE_WEBHOOK` | `1` → бот принимает апдейты через webhook, иначе polling |
| `TG_WEBHOOK_SECRET` | Секрет для проверки запросов от Telegram (обязателен в webhook-режиме) |
| `BOT_MODE` | `all` / `bot` / `webapp` — что запускает контейнер |
| `NIXPACKS_PYTHON_VERSION` | `3.11` — пин версии Python в Railway |
| `ADMIN_IDS`, `BOSS_IDS`, `MANAGER_IDS` | CSV Telegram user_id для bootstrap ролей |
| `ALLOWED_USERS` | CSV id, кому давать роль `manager` по умолчанию |
| `BASE_CURRENCY` | По умолчанию `USD`, валюта для UI |
| `TZ`, `TZ_OFFSET` | Часовой пояс для логов и отчётов |
| `CHECK_INTERVAL_SEC` | Интервал РЕЗЕРВНОГО поллера отгрузок (осн. канал — вебхук), сек, default 900 |
| `PG_POOL_MIN`, `PG_POOL_MAX` | Размер пула psycopg2 (default 1/10) |
| `SQL_SLOW_MS` | Порог логирования медленных запросов, мс (default 200) |

### Как `BOT_MODE` разводит код

В `bot.py` функция `main()` ветвится по `BOT_MODE`:

- `all` (по умолчанию) — один процесс делает всё: Telegram-loop, FastAPI,
  фоновые задачи. Удобно для локальной разработки и маленьких деплоев.
- `bot` — только Telegram (polling) + фоновые задачи (notifier, snapshot).
  Не поднимает FastAPI. Используется в `moysklad_bot`-сервисе на Railway.
- `webapp` — только FastAPI. Не обрабатывает Telegram-апдейты (если не
  включён `TG_USE_WEBHOOK=1`). Используется в `Webapp`-сервисе.

В webhook-режиме (`TG_USE_WEBHOOK=1` + `BOT_MODE=webapp`) FastAPI принимает
POST'ы от Telegram и кормит их в локально собранный aiogram Dispatcher
через `webapp.server.set_telegram_dispatcher(...)`. Сейчас используется
**polling** в bot-сервисе — webhook-режим работает, но не задействован.

---

## 3. Роли и права

Четыре роли. Хранятся в `user_roles.role`. Проверка идёт через предикаты
в `services/roles.py`, у которых есть TTL-кэш (60 сек) — `get_role` не
ходит в БД при каждом обращении.

| Роль | Что может |
|---|---|
| `admin` | Всё. Управляет ролями (`/addrole`), видит весь аудит, делает любое действие любого пользователя. ADMIN_IDS из env автоматически считаются admin'ами (shortcut в `_has_role`). |
| `boss` | Одобряет/отклоняет заявки на отгрузку, подтверждает поступление денег по заказам, видит компанию в Аналитике, видит все долги. Получает уведомления о новых отгрузках и платежах. |
| `manager` | Создаёт заказы и заявки на отгрузку, отправляет платежи на подтверждение, видит свои заказы и свои долги. Отмечает «деньги получил» — но не закрывает долг сам. |
| `guest` | Дефолтная роль для новых юзеров если они НЕ в `ADMIN_IDS / BOSS_IDS / MANAGER_IDS / ALLOWED_USERS`. Нулевые права — даже `/start` выводит «обратитесь к админу». |

Логика назначения роли при первом контакте — `services.database.ensure_user`:
проверяет user_id в ADMIN_IDS → BOSS_IDS → ALLOWED_USERS, иначе `guest`.
Если `ALLOWED_USERS` пустой (legacy-режим) — даём `manager` (открытый бот).

### Изменение роли в рантайме

- Только `admin` может через `/addrole <user_id> <role>`.
- `services.database.set_role` сразу инвалидирует `services.roles` TTL-кэш
  через `_invalidate_role_cache`, поэтому новая роль действует сразу.

---

## 4. Модель данных

Все таблицы в Postgres (на проде) или SQLite (локально). Схема в
`services/database.py:init_db()`. Миграции добавляются в список
`migrations = [...]` — `ALTER TABLE ADD COLUMN`, идемпотентные.

### `user_roles`

```
user_id BIGINT PK
username TEXT
full_name TEXT
role TEXT NOT NULL DEFAULT 'manager'
moysklad_employee_id TEXT      -- связь с сотрудником МойСклад
ms_sync_status TEXT            -- 'pending' | 'linked'
created_at TEXT
```

### `orders` — заказы

```
id SERIAL PK
user_id BIGINT NOT NULL        -- автор-менеджер
full_name TEXT                 -- его имя (для отображения)
status TEXT NOT NULL DEFAULT 'draft'
                               -- draft | pending | approved | rejected | shipped
comment TEXT
agent_id TEXT                  -- контрагент МойСклад (UUID)
agent_name TEXT
currency TEXT                  -- USD | UZS | RUB | EUR
payment_type TEXT NOT NULL DEFAULT 'paid'   -- 'paid' | 'credit'
due_date TEXT                  -- ISO YYYY-MM-DD, только для credit
paid_at TEXT                   -- менеджер отметил «получил»
paid_confirmed_at TEXT         -- босс подтвердил
paid_confirmed_by BIGINT
paid_confirmed_by_name TEXT
created_at TEXT NOT NULL
updated_at TEXT NOT NULL

INDEX idx_orders_credit_due (payment_type, paid_at, due_date)
```

Состояния по оплате:

| paid_at | paid_confirmed_at | Что это |
|---------|-------------------|---------|
| NULL | NULL | Деньги не получены (для credit — открытый долг) |
| NOT NULL | NULL | Менеджер сказал «получил», ждём подтверждения босса |
| NOT NULL | NOT NULL | Деньги подтверждены, заказ полностью закрыт |

### `order_items`

```
id SERIAL PK
order_id BIGINT
product_name TEXT
product_href TEXT              -- ссылка на товар в МойСклад
quantity REAL DEFAULT 1
unit TEXT DEFAULT 'шт'
price REAL DEFAULT 0           -- цена за единицу в order.currency
note TEXT
```

### `shipment_requests` — заявки на отгрузку

```
id SERIAL PK
order_id BIGINT
user_id BIGINT                 -- кто отправил
full_name TEXT
status TEXT DEFAULT 'pending'  -- pending | approved | rejected
comment TEXT
approved_by BIGINT
approved_by_name TEXT
created_at TEXT
approved_at TEXT
```

Когда `boss` одобряет заявку, `update_order_status(order_id, 'approved')`
двигает связанный заказ. Из одобренного заказа `services/ms_demand.py`
создаёт документ `demand` в МойСклад.

### `payments` — отдельные платежи (не привязаны к заказам)

```
id SERIAL PK
user_id BIGINT
username TEXT
full_name TEXT
amount REAL
currency TEXT
comment TEXT
status TEXT                    -- pending | confirmed | rejected | archived
created_at TEXT
confirmed_at TEXT
```

Это **другая концепция**, не путать с `paid_at` на заказе. `payments` — это
произвольные платежи в кассу, не связанные с заказом (`/pay` в боте).
Менеджер их создаёт, босс подтверждает.

Дополнительные колонки для синхронизации с МойСклад:
- `ms_paymentin_id TEXT` — id входящего платежа в МС (NULL = ещё не sync'нут)
- `ms_sync_status TEXT` — `NULL` / `'in_progress'` / `'synced'` / `'failed'`
- `ms_sync_error TEXT` — текст последней причины фейла (для `/sync_payments` UI)

**Жизненный цикл `ms_sync_status`:**
- `NULL` → `'in_progress'` (атомарный `claim_payment_for_ms_sync` ставит «застолблено»)
- `'in_progress'` → `'synced'` (после успешного POST `entity/paymentin`, заодно ставится `ms_paymentin_id`)
- `'in_progress'` → `'failed'` (HTTP/network error; `ms_sync_error` записывается)
- `'failed'` → попадает обратно в очередь `get_payments_needing_ms_sync` (фильтр на `ms_paymentin_id IS NULL`)

**Reaper для orphan'ов:** если процесс убили mid-claim (Railway SIGTERM, OOM,
длинный init_demand_context'а), строка останется в `'in_progress'` навсегда — 
`claim_payment_for_ms_sync` отвергает уже-`'in_progress'`. Защита — функция
`reset_stale_in_progress_payments(older_than_minutes=30)`, вызывается в
самом начале `tasks/run_ms_sync_retry.main()`. Сбрасывает orphan-строки
обратно в `NULL`, следующий cron-tick их подберёт.

### `audit_log`

```
id SERIAL PK
user_id BIGINT
full_name TEXT
role TEXT
action TEXT                    -- payment_confirmed, shipment_approved,
                               -- debt_paid, payment_rejected_received...
details TEXT                   -- свободный текст
created_at TEXT
```

Пишется на каждое чувствительное действие. Просмотр через `/audit` в боте.

### Snapshot МойСклад

Локальная копия справочников, обновляется фоном:

```
ms_products       (товары: ms_id, name, folder_id, code, unit, href, updated_at)
ms_categories     (папки товаров)
ms_counterparties (контрагенты)
ms_employees      (сотрудники)
ms_stock          (остатки: stock, reserve по товару)
ms_snapshot_meta  (когда последний раз обновлялось, dataset → last_refresh)
```

Зачем: WebApp и handlers поверх snapshot работают мгновенно. Live API
МойСклад дёргается только если snapshot пуст (первый старт) или если
нужны live-данные (создание demand).

### `notified_shipments` — дедуп уведомлений об отгрузках

```
demand_id   TEXT PRIMARY KEY
notified_at TEXT
```

Один demand → одно уведомление, независимо от источника. И MS-вебхук
(webapp-процесс), и резервный поллер (bot-процесс) перед отправкой делают
атомарный `mark_shipment_notified(demand_id)` (INSERT-if-absent); PRIMARY KEY
+ общий Postgres решают гонку между процессами. Старьё чистит
`prune_notified_shipments()` (≈раз в сутки из поллера).

### Индексы

`_create_indexes()` (idempotent, гоняется на старте): `idx_orders_credit_due`
`(payment_type, paid_at, due_date)`, `idx_orders_status (status)`,
`idx_shipment_requests_status (status)`, `idx_payments_order_id`,
unique `idx_payments_ms_paymentin_unique`, + индексы snapshot-таблиц.

---

## 5. Воркфлоу

### 5.1 Заказ → отгрузка → закрытие

```
[менеджер]
   │
   │ создаёт заказ в WebApp (или /neworder)
   ▼
order: status=draft, agent, items, payment_type, due_date?
   │
   │ нажал «🚀 Отправить заявку»
   ▼
shipment_request: status=pending
order: status=pending
push → все boss/admin
   │
   ├─ [boss] одобрил
   │     shipment_request: status=approved
   │     order: status=approved
   │     ms_demand.create_demand_from_request → demand-документ в МойСклад
   │
   └─ [boss] отклонил
         shipment_request: status=rejected
         order: status=rejected
         (конец — заказ можно пересоздать)
```

### 5.2 Оплата по credit-заказу (двухступенчатая)

```
[menager] approved/shipped credit order, due_date=2026-05-20
   │
   │ Открытый долг — виден в /debts и WebApp «Долги»
   ▼
   │ Менеджер нажал «✅ Отметить оплачено»
   ▼
order.paid_at = NOW()
push → все boss/admin: «Требуется подтверждение оплаты»
state: awaiting_confirmation
   │
   ├─ [boss] нажал «✅ Подтверждаю»
   │     order.paid_confirmed_at = NOW()
   │     order.paid_confirmed_by = boss.id
   │     push менеджеру: «✅ Босс подтвердил оплату»
   │     debt closed
   │
   └─ [boss] нажал «❌ Отклонить»
         order.paid_at = NULL          ← сбрасываем
         push менеджеру: «⚠️ Босс отклонил, долг снова открыт»
         цикл начинается заново
```

### 5.3 Ежедневное напоминание о долгах

Cron `cron-debts` ежедневно дёргает `python -m tasks.run_debts_notify`:

1. `get_open_debts(due_through=today)` — все долги к оплате сегодня и
   просроченные (включая awaiting_confirmation, чтобы босс помнил).
2. Группирует по `user_id` менеджера.
3. Шлёт менеджеру **только его** долги, требующие действия (без awaiting).
4. Шлёт каждому boss/admin **всю компанию** + блок «требуют подтверждения».

Один процесс, один UPDATE, потом завершается. Не зависит от состояния
основного бота.

### 5.4 Сводки и отчёты → WebApp + дневной пинг

Отдельные текстовые отчёты в Telegram убраны. Продажи/склад/аналитику смотрят
в WebApp (вкладка «Аналитика», раздел «Деньги» → «Обзор»), операционную сводку —
на главной («Требует внимания») и в `/api/ops-summary`.

Бот шлёт лишь ОДИН короткий дневной пинг: `cron-ops` → `python -m
tasks.run_ops_monitor` собирает счётчики (`services/ops_summary.gather_ops_summary`,
всё локально, без МС API) и рассылает по роли «есть N событий — откройте WebApp»
с inline-кнопкой `web_app`. Идемпотентно (`claim_ops_monitor_run` 1×/день).

---

## 6. Интеграция с МойСклад

### 6.1 HTTP-слой

`services/moysklad.py` — общая обёртка над REST API МойСклад.

- Один persistent `aiohttp.ClientSession` (`get_session()`).
- Ретраи с экспоненциальной задержкой на 429/5xx и сетевых ошибках.
- TTL-кэш с inflight-coalescing для `get_shipments` / `get_sales_stats`
  / `get_shipment_positions` (декоратор `_ms_ttl_cache`). Это спасает
  от 429, когда несколько боссов одновременно открывают «Аналитику».
- `get_shipment_positions(demand_id)` пагинирует через offset-loop
  (`limit=100` на страницу) — крупные B2B-заказы с >100 line items
  собираются полностью, иначе хвост молча терялся в `top_products`.
- Concurrency `/positions` ограничен через `_get_positions_semaphore()`
  (lazy semaphore по `id(running_loop)`, cap=8). Без него `asyncio.gather`
  на 15+ demand'ов в одном `/api/analytics` стабильно бьёт 429-rate-limit
  МС, вызывая retry-цепочки 0.5/1.0/2.0с и заметную latency у боссов.
  Не используй module-level `asyncio.Semaphore()` — он биндится к первому
  loop'у при contention и валит cross-loop тесты (`asyncio.run` ×N).
- `cache_clear()` декоратора `_ms_ttl_cache` чистит ТОЛЬКО `cache`, не
  `locks`. Иначе MS-webhook `invalidate_ms_cache()` race'ит с in-flight
  winner'ом: winner держит локальный `lock`, мы стираем dict-entry,
  следующий caller создаёт свежий lock и запускает второй HTTP
  параллельно → inflight-coalescing нарушено. Stale `locks[key]` сидят
  безвредно до stale-prune при `len(cache)>200`.
- На каждом cache-miss при exception в `await fn(...)` делается
  `locks.pop(key, None)` (BaseException-safe, под `try/except`) — иначе
  для consistently-failing demand_id'ов (404, deleted) lock leaks вечно.

### 6.2 Snapshot

`services/snapshot.py` — локальная копия справочников.

- `refresh_*` функции качают данные постранично и пишут в БД.
- `snapshot_refresh_task` (фон в bot-процессе):
  - раз в день в 06:00 UTC — `refresh_reference()` (товары, категории,
    контрагенты, сотрудники).
  - каждые 2 часа — `refresh_stock()` как safety-net.
- `_stock_debounce_loop` — реагирует на флаг `mark_stock_dirty()` который
  ставится из webhook-handler'а; делает `refresh_stock()` через 5 секунд
  после первого события (батч).

### 6.3 Webhook от МойСклад

`webapp/server.py:ms_webhook` принимает POST'ы от МойСклад. URL:
`{WEBAPP_URL}/api/ms-webhook/{MS_WEBHOOK_SECRET}`.

При получении события:
1. Проверяет секрет в URL (404 если не совпадает — молчим).
2. Логирует тип событий.
3. Ставит `mark_stock_dirty()` (фоновый refresh подхватит).
4. Сбрасывает `invalidate_ms_cache()` — все аналитические кэши
   протухают, чтобы свежий запрос показал актуальные цифры.
5. На `demand.CREATE` / `retaildemand.CREATE` запускает (fire-and-forget)
   `notifier.notify_new_shipment(demand_id)` — уведомление boss/admin о новой
   отгрузке **мгновенно** (раньше это делал поллер раз в N секунд). Дедуп через
   `notified_shipments`; бот-созданные demand'ы (атрибут `telegram_user_id`)
   пропускаются. `shipment_notifier` остаётся резервом на случай пропущенного
   вебхука.

Подписка регистрируется автоматически на старте бота через
`services.ms_webhooks.ensure_subscriptions()`. Идемпотентно: при смене
`WEBAPP_URL` или `MS_WEBHOOK_SECRET` старые подписки удаляются, новые
ставятся.

### 6.4 Создание demand

`services/ms_demand.py:create_demand_from_request` создаёт документ
«Отгрузка» в МойСклад из одобренной заявки.

- Резолвит первую доступную организацию, склад, ставит кастомный атрибут
  `telegram_full_name` (его потом группирует аналитика, иначе все
  отгрузки прилипают к owner=API-token).
- Идёт в `entity/demand`, прикладывает позиции.

Контекст организации/склада подгружается один раз при старте через
`init_demand_context()`. Функция НЕ бросает exception (helpers
`_pick_first` / `_ensure_custom_attribute` глотают сетевые сбои и
возвращают None), вместо этого возвращает dict с булевыми флагами
`ready/org/store/attribute_name/attribute_uid`. Callers инспектируют
dict-возврат, а не оборачивают вызов в `try/except` (был dead code в
старой версии `run_ms_sync_retry`).

В bot-процессе `init_demand_context()` вызывается в `main()` при старте.
В cron-CLI (`tasks/run_ms_sync_retry`) — лениво: только если есть
pending платежи. Без этой оптимизации 96 cron-тиков/день делали бы
~200-400 МС API calls впустую на noop-прогонах.

---

## 7. Telegram-слой

### 7.1 aiogram

Версия aiogram 3.x. Структура:

- `bot.py` — точка входа, регистрирует роутеры, поднимает middleware и
  ветвится по `BOT_MODE`.
- `handlers/start.py` — `/start`, главное меню, выбор экрана.
- `handlers/orders.py` — создание заказа, добавление позиций, отправка
  на одобрение, FSM для выбора количества/цены/клиента.
- `handlers/debts.py` — `/debts`, кнопки «Отметить оплачено» /
  «Подтверждаю» / «Отклонить».
- `handlers/payments.py` — `/pay` для отдельных платежей в кассу.
- `handlers/shipments.py` — `/shipments` для boss, просмотр новых отгрузок.
- `handlers/analytics.py` — `/analytics`, агрегаты МойСклад.
- `handlers/users.py` — `/addrole`, `/users`, `/syncms`.
- `handlers/audit.py` — `/audit`, просмотр аудит-лога.
- `handlers/log.py` — `/log`, последние записи логов.

### 7.2 Middlewares

- `RateLimitMiddleware` (в `bot.py`) — 30 действий/мин на пользователя
  на сообщение/callback. Бьёт спам кнопками.

### 7.3 FSM storage

- Если `REDIS_URL` задан → `RedisStorage.from_url(REDIS_URL)`. Состояния
  переживают редеплой.
- Иначе → `MemoryStorage`. После рестарта черновики теряются.

### 7.4 Режимы получения апдейтов

- **Polling** (по умолчанию) — `dp.start_polling(bot)`.
- **Webhook** — если `TG_USE_WEBHOOK=1 + WEBAPP_URL + TG_WEBHOOK_SECRET`:
  - `bot.py` зовёт `webapp_server.set_telegram_dispatcher(bot, dp)`.
  - `bot.set_webhook(url=f"{WEBAPP_URL}/tg/{TG_WEBHOOK_SECRET}", ...)`.
  - FastAPI endpoint `/tg/{secret}` принимает Update, проверяет секрет
    в URL И в заголовке `X-Telegram-Bot-Api-Secret-Token`, кормит в
    dispatcher через `feed_webhook_update`.
  - Работает только когда FastAPI поднят в том же процессе
    (`BOT_MODE=all` или `webapp`).

---

## 8. WebApp (FastAPI + статика)

### 8.1 Бэкенд

`webapp/server.py` — FastAPI приложение.

- Авторизация по `Telegram.WebApp.initData`: верификация подписи в
  `webapp/auth.py`. Любой `/api/*` endpoint должен звать
  `verify_init_data()` или helper `_authorize()`.
- Роль читается из `services.roles.cached_role` (TTL-кэш 60 сек),
  **не** напрямую из БД.
- DB-вызовы из endpoint'ов идут через `services.async_db` (асинхронная
  обёртка через `asyncio.to_thread`), чтобы не блокировать event loop
  на синхронном psycopg2.

### 8.2 API endpoints

| Endpoint | Метод | Что |
|---|---|---|
| `/healthz` | GET | health-check для Railway |
| `/` | GET | главная HTML страница (с cache-busting по git SHA) |
| `/static/...` | GET | CSS/JS с Cache-Control 24h |
| `/api/me` | POST | вернуть user_id + role |
| `/api/home` | POST | главный экран (свод дня, мои заказы, лидерборд для босса) |
| `/api/stock` | POST | список товаров + категорий (через snapshot) |
| `/api/analytics` | POST | агрегаты продаж за период |
| `/api/payments/history` | POST | история платежей юзера |
| `/api/payments/send` | POST | отправить платёж на подтверждение |
| `/api/orders` | POST | список заказов (свои или все для boss) |
| `/api/orders/requests` | POST | заявки на одобрение (только boss) |
| `/api/orders/create` | POST | создать draft-заказ |
| `/api/orders/add_item` | POST | добавить позицию |
| `/api/orders/remove_item` | POST | удалить позицию |
| `/api/orders/set_agent` | POST | выбрать клиента |
| `/api/orders/submit` | POST | отправить на одобрение (тут принимаются payment_type + due_date) |
| `/api/orders/mark_paid` | POST | менеджер отметил «деньги получил» |
| `/api/orders/confirm_payment` | POST | boss подтверждает поступление (idempotency_key) |
| `/api/orders/reject_payment` | POST | boss отклоняет |
| `/api/orders/delete_draft` | POST | удалить черновик (каскадно) |
| `/api/requests/approve` | POST | boss одобряет заявку (DB + МойСклад + PDF + уведомления) |
| `/api/requests/reject` | POST | boss отклоняет заявку |
| `/api/payments/pending` | POST | paid-заказы, ждущие подтверждения оплаты (boss) |
| `/api/debts` | POST | список долгов + суммы получено/ожидает |
| `/api/agents` | POST | поиск контрагентов |
| `/api/ms-webhook/{secret}` | POST | вебхук от МойСклад |
| `/tg/{secret}` | POST | вебхук от Telegram (только если включён режим) |

### 8.3 Фронт

`webapp/static/` — vanilla JS, без сборщика.

- `index.html` — единый layout с `<header>`, `<main id="content">` и
  `<nav class="bottom-nav">` (5 кнопок: Главная / Склад и заказы /
  Долги / Аналитика / Платежи).
- `app.js` — switch по экранам, для каждого свой `render*()`-fn.
  Cache-bust через `?v={{VERSION}}` — версия = git SHA из
  `RAILWAY_GIT_COMMIT_SHA`.
- `style.css` — переменные через `--accent`, `--bg-card` и т.п.,
  поддержка тёмной темы Telegram.

---

## 9. Запросы к БД и блокировка event loop

Драйвер `psycopg2` — синхронный. Прямые вызовы из async-функций
блокируют event loop на время SQL.

Решение в два уровня:

1. **`psycopg2.pool.ThreadedConnectionPool`** (в `services/database.py`):
   убирает overhead на установку нового соединения (~30-80мс) на каждый
   запрос. Размер `PG_POOL_MIN..PG_POOL_MAX` (1..10 по умолчанию).
2. **`services/async_db.py`** — module-level `__getattr__`-обёртка:
   `await adb.get_user(uid)` автоматически запускает sync-функцию
   `services.database.get_user(uid)` в thread pool через
   `asyncio.to_thread`. Event loop остаётся свободным.

В webapp **все** DB-вызовы из endpoint'ов идут через `async_db`. В
handlers бота и фоновых задачах — sync вызовы (там event-loop-блок не
критичен, нагрузка низкая).

Полная миграция на `asyncpg` (native async driver, без thread pool)
**не сделана** — это отдельная работа на будущее. Сейчас связка
psycopg2 + threadpool + кэш ролей закрывает реальные пики нагрузки.

---

## 10. Кэши

В нескольких местах закладки `time-to-live`-кэшей:

| Где | Что | TTL |
|---|---|---|
| `services/roles.py` | роль по `user_id` | 60 сек |
| `services/moysklad.py` `_api_get_all_stock` | сырой ответ `/report/stock/all` | 30 сек |
| `services/moysklad.py` `get_shipments`, `get_sales_stats`, `get_employee_*` | через декоратор `_ms_ttl_cache` | 60 сек |
| `services/moysklad.py` `get_shipment_positions` | через декоратор | 1 час (позиции иммутабельны) |
| `services/snapshot.py` | snapshot МойСклад | day (справочники) / 2h (остатки) |

Принудительная инвалидация:

- При изменении роли — `services.database` лениво зовёт
  `services.roles.invalidate_role(user_id)`.
- При вебхуке от МойСклад — `invalidate_ms_cache()` чистит все
  `_ms_ttl_cache` сразу.

Concurrency-ограничители (не TTL, но рядом по смыслу):

- `services/moysklad._get_positions_semaphore()` — lazy `asyncio.Semaphore(8)`
  по `id(running_loop)`, ограничивает cold-cache fan-out `get_shipment_positions`.
  Cache-hit семафор не трогает (декоратор `_ms_ttl_cache` отдаёт до тела).
  Lazy-by-loop — чтобы short-lived `asyncio.run()` в CLI и per-test loops
  в pytest не словили `RuntimeError: bound to a different event loop`.

---

## 11. Что где искать (quick reference)

| Хочу… | Смотреть |
|---|---|
| Изменить логику ролей / прав | `services/roles.py` (предикаты), `services/database.py` (хранение, ensure_user) |
| Добавить новое поле в заказ | `services/database.py:init_db` (тут CREATE TABLE + migrations) + использовать в `webapp/server.py` API endpoints |
| Добавить экран в WebApp | `webapp/static/index.html` (nav button), `webapp/static/app.js` (`case '...':` + `render*()`), `webapp/static/style.css` |
| Добавить команду в боте | новый файл в `handlers/`, зарегистрировать в `bot.py:register_routers` |
| Изменить вебхук от МойСклад | `webapp/server.py:ms_webhook` + `services/ms_webhooks.py:ensure_subscriptions` |
| Изменить дневной пинг / операционную сводку | `tasks/run_ops_monitor.py` (пинг) + `services/ops_summary.py` (сбор) + `webapp/server.py:/api/ops-summary` |
| Найти ошибку в проде | Railway Logs у нужного сервиса. Долгие SQL логируются как `SQL slow ...` через `SQL_SLOW_MS` (default 200мс) |
| Понять что сейчас в БД | `services/database.py:init_db` — все таблицы там же |
| Локально запустить | `BOT_MODE=all`, `DATABASE_URL=` (пусто) → SQLite, `REDIS_URL=` (пусто) → MemoryStorage |

---

## 12. Чего НЕТ сейчас (заметки на будущее)

- **Полное** удаление psycopg2. Денежное ядро (order/payment/кредит/сдачи/
  возвраты + `snapshot.refresh_*`) уже на native async `asyncpg` (`services/adb_core.py`);
  psycopg2 остаётся для startup/миграций, snapshot-reads и backup-fallback.
- Связь между `payments` (отдельные платежи в кассу) и `orders` — разные сущности.
- Полноценная per-permission система помимо фиксированных ролей (есть
  per-user permission overrides, но не полный RBAC).
- Логи длиннее 7 дней — Better Stack / Axiom log drain.
- Перевод бота на webhook на проде (код готов, `TG_USE_WEBHOOK=1`; сейчас polling).

> Реализовано (раньше было «нет»): частичные оплаты; подтверждение оплаты;
> событийные уведомления; **возвраты** (полные/частичные) + salesreturn;
> **сдачи наличных** (FIFO-закрытие); **кредит-лимиты с энфорсом** (override
> боссом); **reject→draft + freeze**; **деактивация юзеров** (=guest);
> **аналитика по менеджерам** (из локальных orders, WebApp); **конвертация
> валют в сводных** («≈ X USD» через `convert_to_base`); **синхронизация
> удаления заказа в МС** (вебхук + cron-реконсиляция); asyncpg money-core; pytest+CI.

---

## 13. Качество: тесты, линт, типы

Конфиг тулчейна — в `pyproject.toml`; версии dev-тулов запинены в
`requirements-dev.txt`.

- **pytest** (`tests/`, fixture `isolated_db` — SQLite в tmp; env-заглушки в
  `conftest.py`). Принцип: мокаем **границу с сетью** (`aioresponses` для
  aiohttp, `tg_send_message` на верхнем уровне), а не свой код — иначе баг в
  обёртке проходит CI (так и случилось с `tg_send_message`/`base_url`). Покрыты:
  денежные инварианты, контракт МойСклад (meta demand/customerorder), регрессии
  безопасности (все `/api/*` требуют initData; HTML-escape), дедуп уведомлений.
- **ruff** — строгий гейт (`E9,F63,F7,F82`) + полный набор (`E9,F,B,ASYNC,UP,SIM`).
- **mypy** — точечно по `order_workflow/database/moysklad/server`, **блокирующий**
  (0 ошибок). `pre-commit` — локальная первая линия.
- **CI** (`.github/workflows/ci.yml`): ruff + mypy + pytest с coverage-«храповиком»
  (`--cov-fail-under=25`).
- **Стартовый self-check** (`bot.py:_startup_selfcheck`): логирует `BOT_MODE` и
  проверяет, что Telegram-URL уведомлений собирается — ловит регресс на старте,
  а не «когда полезли в логи».
