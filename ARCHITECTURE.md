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
отгрузками и складом. Внутренний инструмент компании: менеджеры собирают
заказы и фиксируют оплаты, руководители одобряют отгрузки и подтверждают
поступление денег.

Учёт **полностью локальный**: каталог, остатки, контрагенты, приходные и
расходные накладные живут в нашем Postgres. Раньше всё это было в облачном
МойСклад, и бот лишь создавал там документы; интеграция удалена целиком —
остался только одноразовый скрипт переноса `scripts/migrate_from_moysklad.py`.

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

> Пошаговая инструкция по выкату (что нажимать, в каком порядке, что проверить
> после) — в [DEPLOY.md](DEPLOY.md). Здесь — устройство и обоснование.

Проект `strong-wisdom` в Railway содержит четыре сервиса:

```
                ┌──────────────────┐
                │  moysklad_bot    │  Python, BOT_MODE=bot
                │  - polling       │  Telegram Bot API → бот
                │                  │  фоновых циклов нет: синхронизировать
                │                  │  не с чем, учёт локальный
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
| `DATABASE_URL` | Postgres-подключение (`${{Postgres.DATABASE_URL}}`) |
| `REDIS_URL` | Redis (`${{Redis.REDIS_URL}}`). Если пусто — FSM работает в памяти |
| `WEBAPP_URL` | Публичный домен webapp-сервиса (без `/` в конце) |
| `TG_USE_WEBHOOK` | `1` → бот принимает апдейты через webhook, иначе polling |
| `TG_WEBHOOK_SECRET` | Секрет для проверки запросов от Telegram (обязателен в webhook-режиме) |
| `BOT_MODE` | `all` / `bot` / `webapp` — что запускает контейнер |
| ~~`NIXPACKS_PYTHON_VERSION`~~ | Не нужна: билд на Railpack, версия Python — из `runtime.txt` |
| `ADMIN_IDS`, `BOSS_IDS`, `MANAGER_IDS` | CSV Telegram user_id для bootstrap ролей |
| `ALLOWED_USERS` | CSV id, кому давать роль `manager` по умолчанию |
| `BASE_CURRENCY` | По умолчанию `USD`, валюта для UI |
| `TZ` | Часовой пояс контейнера. Им же пишется `created_at` (`utils.helpers.local_now`), поэтому должен совпадать с бизнес-зоной |
| `PG_POOL_MIN`, `PG_POOL_MAX` | Размер пула psycopg2 (default 1/10) |
| `SQL_SLOW_MS` | Порог логирования медленных запросов, мс (default 200) |

### Как `BOT_MODE` разводит код

В `bot.py` функция `main()` ветвится по `BOT_MODE`:

- `all` (по умолчанию) — один процесс делает всё: Telegram-loop, FastAPI,
  фоновые задачи. Удобно для локальной разработки и маленьких деплоев.
- `bot` — только Telegram (polling). Не поднимает FastAPI. Используется в
  `moysklad_bot`-сервисе на Railway.
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
| `boss` | Одобряет/отклоняет заявки на отгрузку, подтверждает поступление денег по заказам, видит компанию в Аналитике, видит все долги, видит журнал действий (`/api/audit_log`, «Настройки → Журнал действий» — тот же аудит, что у admin в боте `/audit`). Получает уведомления о новых отгрузках и платежах. |
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
moysklad_employee_id TEXT      -- legacy: связь с сотрудником МойСклад, не читается
ms_sync_status TEXT            -- legacy, там же
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
agent_id TEXT                  -- counterparties.id СТРОКОЙ (у старых строк — UUID МС,
                               --   пока их не переписал backfill_local_identifiers)
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
product_href TEXT              -- legacy: ссылка на товар МойСклад. Карточка нашей
                               --   номенклатуры — в order_item_products(item_id → product_id)
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
двигает связанный заказ, а `services/order_shipment.py` проводит расходную
накладную — она и списывает остаток.

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

Колонки `ms_paymentin_id` / `ms_sync_status` / `ms_sync_error` остались от
интеграции с МойСклад и больше не читаются: платежи никуда не уезжают.

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

### Локальный склад

```
products          (номенклатура: name, category, sku, unit, legacy_ms_id)
counterparties    (контрагенты: name, type, phone, telegram_id, legacy_ms_id)
warehouses        (склады; по умолчанию один — сеет seed_warehouses)
stock             (остаток по паре товар+склад, PK (product_id, warehouse_id))
invoices          (накладная: type incoming|outgoing, номер, дата, статус, сумма)
invoice_items     (позиции накладной)
invoice_counters  (счётчик номеров по паре тип+год)
stock_writeoffs   (списание/излишек: причина, фото, себестоимость потери —
                   sidecar к накладной, которая и двигает товар)
stock_counts      (сессия инвентаризации: шапка)
stock_count_lines (посчитанный ФАКТ по товару в сессии)
```

Движение остатка проходит ТОЛЬКО через `services/warehouse.py`: номер, шапка,
строки и остаток пишутся одной транзакцией, остаток не уходит в минус,
параллельные накладные по одному товару сериализуются `FOR UPDATE`. Списание и
инвентаризация (`services/inventory.py`) — не исключение: они складывают
обычную расходную (или приходную для излишка) накладную, а своя таблица хранит
лишь то, чего в накладной нет, — причину.

Связки, из-за которых нельзя было обойтись колонкой (инкрементальных миграций
в проекте нет):

```
order_item_products    (позиция заказа → карточка товара)
order_shipment         (заказ → расходная накладная; failed_at/error для дайджеста)
container_item_products(позиция контейнера → карточка товара)
container_receipt      (контейнер → поставщик + приходная накладная)
ms_id_map              (UUID МойСклад → наш id; артефакт миграции)
payment_part_accounts  (строка разбивки оплаты «карта/на счёт» → карта/счёт acc_accounts)
machine_receipt_accounts (поступление по рассрочке → карта/счёт acc_accounts)
acc_account_details    (номер расчётного счёта, ИНН, МФО к acc_accounts)
warehouse_archived     (склад в архиве — sidecar, а не колонка warehouses.archived; B8)
stock_transfers        (история перемещений остатка между складами; B8)
order_warehouse        (склад отгрузки заказа, если менеджер выбрал не дефолтный; B8)
supplier_invoice_terms (условия оплаты приходной накладной: «в долг» + срок либо
                        «уже оплачено»; строки нет — значит в долг)
supplier_payment_parts (выплата поставщику: способ, карта/счёт, С КОТОРОГО ушли
                        деньги, курс и сумма в валюте долга; 1:1 с supplier_payments)
```

Долга перед поставщиком отдельной таблицей нет: это приходная накладная с
контрагентом, а выплаты по ней — `supplier_payments` (туда же пишет разовый
перенос истории из МойСклад). Считает всё это `services/supplier_debts.py` —
зеркало дебиторки `services/receivables.py`. В `payments` исходящие деньги не
попадают никогда: там деньги ОТ клиентов, и на них считается вся дебиторка.
Во фронте это второй уровень вкладки «Деньги → Долги» («Клиенты ·
Поставщикам»), а не своя вкладка: ряд вкладок раздела ограничен четырьмя
пунктами (CLAUDE.md, «Вкладок в разделе — не больше ЧЕТЫРЁХ»).

Карты и счета «куда поступили» — тот же справочник `acc_accounts`, что у
бухгалтерии, но работает и при выключенной бухгалтерии: номер карты хранится
только последними 4 цифрами, расчётный счёт — целиком (CLAUDE.md, «Куда
поступили деньги»).

### Индексы

`_create_indexes()` (idempotent, гоняется на старте). Объявлены прямо в схеме:
боевой базы нет, поэтому `CONCURRENTLY` не нужен.

**Уникальность — это инвариант, а не оптимизация:**
`idx_shipment_requests_one_pending (order_id) WHERE status='pending'` (закрывает
двойной сабмит на уровне БД), `idx_returns_one_pending (order_id) WHERE
status='pending'`, `idx_orders_ms_customerorder` / `idx_orders_ms_demand`
(partial, `WHERE ... IS NOT NULL` — остались от МойСклад, обратного поиска по
ним больше нет), unique `idx_payments_ms_paymentin_unique`.

**Под реальные фильтры:** `idx_orders_debt_lookup (payment_type, status,
paid_confirmed_at)` — под `get_open_debts`; `idx_orders_status`,
`idx_orders_agent_id`, `idx_orders_user_created`; `idx_payments_order_status
(order_id, status)` — он же покрывает «все платежи по заказу» как префикс;
`idx_cash_deposit_orders_order (order_id)` — PK `(deposit_id, order_id)` для
поиска по `order_id` не работает; `idx_shipment_requests_status`, индексы
`created_at` денежных лент, `idx_orders_created (created_at, id)` (все заказы
свежими вперёд), `idx_payments_user_created`, частичный
`idx_payments_confirmed_period` по `COALESCE(confirmed_at, created_at)` (итог
«Деньги» и лента), `idx_invoices_type_status_date` (отгрузки за период),
`idx_order_item_products_*` и `idx_order_shipment_failed` (дайджест «остаток не
списан»).

**Одна накладная — один владелец:** частичные UNIQUE по `invoice_id` у
`order_shipment`, `return_receipt`, `container_receipt`; UNIQUE `return_items
(return_id, order_item_id)`, `acc_day_closes(doc_id)` (на `(account_id,
close_date)` — нет: пересчёт кассы дважды за день законен).

Убранные индексы (`database.DROPPED_INDEXES`: дубли UNIQUE и индексы без
запросов) на существующей базе снимает разовый `scripts/apply_constraints
--apply`; он же переводит количества REAL→NUMERIC и ставит FK/CHECK. Индекс,
который не создался, `_create_indexes` пишет ERROR, а сверка старта
(`startup_checks`) называет его в алерте — вместе с типами колонок
(NUMERIC/BIGINT) и ICU-коллацией.

---

## 5. Воркфлоу

### 5.1 Заказ → отгрузка → закрытие

```
[менеджер]
   │
   │ создаёт заказ в WebApp
   ▼
order: status=draft, agent, items, payment_type, due_date?
   │
   │ нажал «🚀 Отправить заявку»
   ▼
shipment_request: status=pending
order: status=pending
push → все boss/admin
   │
   ├─ [boss] одобрил (видит цены, сумму, тип оплаты и скидку к прайсу;
   │     скидка ≥ app_settings.order_discount_requires_approval_pct помечена
   │     и требует ВТОРОГО, явного нажатия — services/order_discounts.py)
   │     shipment_request: status=approved
   │     order: status=approved («к отгрузке»), платежей не создаётся
   │     order_shipment.ship_order → расходная накладная, остаток списан
   │     │
   │     ├─ «оплата сразу»: менеджер вносит разбивку (наличные/карта/
   │     │   перечисление, USD/UZS, курс) на ВСЮ сумму → payments + payment_parts
   │     │   → только тогда «Отгрузить» (сервер: code=payment_required)
   │     └─ «в долг»: «Отгрузить» сразу; поступления — той же разбивкой
   │     order: status=shipped
   │     наличные → «Сдать наличные» (cash_deposit_parts, FIFO) → подтверждение
   │     сдачи подтверждает их платежи; карта/счёт → «Подтвердить» руководителя
   │
   └─ [boss] отклонил
         shipment_request: status=rejected
         order: status=rejected
         (конец — заказ можно пересоздать)
```

Подробности разбивки, сдачи по строкам и правил — CLAUDE.md, раздел «Оплата
заказа — разбивка „как получены деньги“». Схема ниже (5.2) — историческая
(`paid_at`/`paid_confirmed_at` остались производными отметками).

**Поперёк этой цепочки — ежедневная сверка кассы** (`services/
cash_reconciliation.py`, таблица `daily_cash_counts`, «Деньги → Сверка»):
менеджер пересчитывает наличные РУКАМИ и сравнивает с тем, что система считает
у него на руках (`order_payments.cash_on_hand`). Всё, что выше, знает только
про деньги, КОТОРЫЕ ЗАНЕСЛИ: оплата, не введённая в систему вовсе, не всплывает
нигде — её ловит только физический пересчёт. Сверка ничего не двигает (ни
платежа, ни сдачи, ни долга) и подтверждения не требует. От
`accounting_enabled` не зависит. См. CLAUDE.md, «Ежедневная сверка кассы».

### 5.2 Оплата по credit-заказу (двухступенчатая)

```
[menager] approved/shipped credit order, due_date=2026-05-20
   │
   │ Открытый долг — виден в WebApp «Финансы → Долги»
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

## 6. Склад

### 6.1 Движение остатка

`services/warehouse.py` — единственное место, где меняется `stock`.

- `create_invoice` / `cancel_invoice` — публичные обёртки; номер, шапка,
  строки и остаток пишутся ОДНОЙ транзакцией.
- `create_invoice_in` / `cancel_invoice_in` — то же внутри уже открытой
  транзакции; при отказе БРОСАЮТ `InvoiceError`, а не возвращают «не ок»
  словарём, который вызывающий спокойно закоммитит вместе со своими записями.
- Остаток не уходит в минус: нехватка хотя бы по одной позиции откатывает ВСЮ
  накладную — частичных списаний не бывает.
- Позиции сортируются по `product_id` перед захватом `FOR UPDATE`: без этого
  две накладные с составом [A,B] и [B,A] берут строки в обратном порядке и
  получают взаимный deadlock на Postgres.
- Повторы одного товара схлопываются ДО проверки остатка: `[A×6, A×6]` при
  остатке 10 иначе проходит обе построчные проверки и уводит остаток в −2.
- Номер (`IN-2026-0001` / `OUT-2026-0001`) выдаёт UPSERT по
  `invoice_counters` внутри той же транзакции: два параллельных создателя
  получают разные номера, а откат не оставляет дырки в нумерации.

### 6.2 Отгрузка заказа

`services/order_shipment.py:ship_order` списывает одобренный заказ расходной
накладной. Идемпотентность — PRIMARY KEY `order_shipment.order_id`: повторное
одобрение (два босса, ретрай, старая кнопка) не спишет товар дважды.

Позиция без карточки номенклатуры в накладную не попадает и возвращается в
`skipped` — босс видит это в тексте одобрения. Не списалось совсем (не хватило
остатка, ничего не сопоставлено) — заказ помечается `failed_at`/`error` и
попадает в дайджест «нужна доделка»; само одобрение при этом НЕ откатывается:
его принял человек.

Отмена заказа (`order_workflow.cancel_order_full`) откатывает накладную и
возвращает остаток. Best-effort и ПОСЛЕ локальной отмены: ошибка склада не
должна отменять то, что оператор уже подтвердил.

### 6.3 Приёмка контейнера

`services/container_receipt.py:receive` — приходная накладная по посчитанному
контейнеру. Повторная приёмка = отмена прежней накладной + создание новой
одной транзакцией: количества правят сутки (`containers.EDIT_WINDOW_HOURS`), и
остаток обязан ехать за ними.

Контейнер, оприходованный ещё в МойСклад (`received_at` стоит, `invoice_id`
пуст), к повторному приходу не допускается: его остаток приехал миграцией, и
вторая накладная прибавила бы товар второй раз.

### 6.3а Списание с причиной и инвентаризация

`services/inventory.py` — единственное, что умеет убрать товар со склада не
продажей и вернуть его туда не приходом от поставщика:

- **списание** (`create_writeoff`) — расходная накладная с ценой 0 плюс строка
  `stock_writeoffs` с обязательной причиной («бой», «порча», «недостача», своя
  формулировка), необязательным снимком и — при включённом учёте —
  себестоимостью потери из FIFO (`sale_costs` этой накладной). Снимок уезжает
  в тот же приватный канал-хранилище, что фото техники и товаров
  (`PHOTOS_TG_CHAT_ID`), ещё ДО проведения: ручка отдаёт `file_id`, форма несёт
  его в создание, а показывает снимок прокси со скоупом по номеру записи;
- **инвентаризация** (`start_count` → `set_count_line` → `apply_count`) —
  сессия `stock_counts` со строками «посчитанного факта». Дельта считается в
  момент проведения, от живого остатка: недостача уходит списанием, излишек —
  приходной накладной, обе с причиной «инвентаризация» и одним `count_id`,
  всё одной транзакцией;
- **сторно** (`void_writeoff`) — отмена той же накладной плюс `cancelled_at`;
  автору сутки (`VOID_WINDOW_HOURS`), дальше и чужое — руководителю.

Накладные списания исключены из выручки и отчёта о прибыли
(`warehouse.not_writeoff_sql`): это не продажа, и нулевая сумма портила бы
средний чек, а себестоимость — прибыль.

### 6.4 Каталог и аналитика продаж

Читающая часть — там же, в `warehouse.py`:

- `get_catalog` — остаток, резерв и доступное по каждому товару. Резерв
  локально это одобренные, но не отгруженные заказы; доступное = остаток −
  резерв, и в каталог идёт именно оно.
- `sales_stats` / `list_shipments` / `counterparty_purchases` — выручка, топ
  товаров и клиентов по расходным накладным. Отменённые накладные в выручку не
  идут: отмена вернула товар на склад, продажи не было.
- Границы периода считает `_upper_bound`: `invoice_date` — ДАТА, а границы
  приходят моментами, и полуинтервал с обрезкой до дня ломает «сегодня».
  Полночь исключаем, любое другое время дня включаем.

### 6.5 Разовый перенос из МойСклад

`scripts/migrate_from_moysklad.py` — единственное, что ещё знает про МС.
Держит СВОЙ минимальный HTTP-клиент: он обязан пережить удаление интеграции,
иначе перенос перестанет воспроизводиться ровно тогда, когда ещё может
понадобиться (аккаунт МС живёт месяц-другой после переключения).

Сверка с нулевым допуском: расхождение в остатках даёт ненулевой код выхода и
обязано блокировать переключение — обратной синхронизации после перехода нет.

`database.backfill_local_identifiers` (из `tasks/migrate`) переводит старые
ссылки — `orders.agent_id`, `credit_limits.agent_id`, `leads.agent_ms_id`,
`machine_deals.agent_ms_id`, `product_prices.ms_id`, `product_photos.ms_id`,
позиции заказов — с UUID МойСклад на наши id. Пока этого не сделано, один и
тот же клиент существует под двумя ключами, и долг по нему считается дважды
по половинке.

---

## 7. Telegram-слой

### 7.1 aiogram

Версия aiogram 3.x. Структура:

- `bot.py` — точка входа, регистрирует роутеры, поднимает middleware и
  ветвится по `BOT_MODE`.
T3.3: бот срезан до того, чего нет в WebApp. Экраны-дубли (создание заказа с
каталогом, списки заказов и заявок, остатки, долги, аналитика, касса,
сдачи, возвраты, кредит-лимиты, курсы, цены) удалены вместе с
`handlers/{analytics,stock,credit,pricing,debts}.py`; их команды отвечают
подсказкой `handlers.start.cmd_retired` со ссылкой на экран WebApp.

- `handlers/start.py` — `/start`, меню (вход в WebApp), `/find`, подсказки по
  снятым командам (`cmd_retired`) и по удалённым вместе с МойСклад
  (`cmd_removed`: `/syncms`, `/msstaff`, `/refresh`, `/snapshot`,
  `/sync_payments`).
- `handlers/orders.py` — решения по заявке (одобрить / отклонить / на доработку /
  с превышением лимита), карточка заказа для чтения, `/frozen` + разморозка.
  Плюс форматтеры карточек, которые зовёт WebApp при создании заявки.
- `handlers/payments.py` — `/pay` (платёж в кассу), подтверждение/отклонение
  платежа кнопками.
- `handlers/deposits.py` — подтверждение/отклонение сдачи наличных кнопками.
- `handlers/returns.py` — приёмка товара и подтверждение возврата кнопками.
- `handlers/shipments.py` — `/shipments`, просмотр новых отгрузок.
- `handlers/order_ship.py` / `handlers/order_cancel.py` — `/ship`, `/cancel`.
- `handlers/users.py` — `/addrole`, `/users`, `/deactivate`, `/syncms`.
- `handlers/audit.py` — `/audit`, просмотр аудит-лога.
- `handlers/log.py` — `/log`, последние записи логов.
- `handlers/_ui.py` — общие приёмы работы с inline-клавиатурами (T3.2) и
  кнопка входа в WebApp; сборщики без Telegram-вызовов (неактивные кнопки
  исхода, `settle_markup`, `prompt_keyboard` с force_reply — Bot API 10.3) —
  в `utils/keyboards.py`, их зовут и сервисы. Правила — CLAUDE.md, «Кнопки
  карточек».

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
| `/api/stock` | POST | каталог: остаток, резерв, доступное, цены |
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
| `/api/requests/approve` | POST | boss одобряет заявку (DB + накладная + PDF + уведомления) |
| `/api/requests/reject` | POST | boss отклоняет заявку |
| `/api/payments/pending` | POST | paid-заказы, ждущие подтверждения оплаты (boss) |
| `/api/debts` | POST | список долгов + суммы получено/ожидает |
| `/api/suppliers/debts` | POST | «мы должны»: итог, сроки, долги по приходам, авансы, приход без цены и лента выплат одним ответом (admin/boss) |
| `/api/suppliers/payment` | POST | выплата поставщику: способ, валюта, курс, карта/счёт-источник; с привязкой к приходу или общая (идемпотентно) |
| `/api/suppliers/terms` | POST | условия оплаты прихода: «в долг» + срок либо «уже оплачено» |
| `/api/agents` | POST | поиск контрагентов |
| `/api/wh/invoices` | POST | список накладных; `/api/wh/invoices/{get,create,cancel,send}` — карточка, проведение, отмена (менеджеру — пока выключен `delete_requires_boss`), PDF клиенту |
| `/api/stock/writeoffs` | POST | журнал списаний и излишков; `/api/stock/writeoffs/{create,void}` — списать с причиной и сторнировать; `/api/stock/writeoffs/{photo,photo_view}` — прикрепить снимок до проведения и отдать его байтами |
| `/api/stock/counts` | POST | сессии инвентаризации; `/api/stock/counts/{start,card,line,line_remove,confirm,cancel}` — открыть, посмотреть дельты, вводить факт, провести, отменить |
| `/api/machines/deal` | POST | бронь/продажа/рассрочка: менеджер → заявка на одобрение, руководство → сразу |
| `/api/machines/deals/{pending,approve,rework,reject,resubmit,cancel}` | POST | заявки на сделки по технике: список и решения (`services/machine_deal_requests.py`) |
| `/api/settings/delete_requires_boss` | POST | руководство: удаление техники/товаров/накладных — только руководителю |
| `/api/pay_accounts` | POST | карты и счета «куда поступили» + последний выбор человека (`include_archived` — руководству) |
| `/api/pay_accounts/{create,update,archive}` | POST | завести (менеджер и руководство; тёзка → `existed`), править и архив (руководство; менеджер — без руководителя) |
| `/api/audit_log` | POST | журнал действий, read-only, admin/boss (`database.get_audit_log_page` — фильтры дата/сотрудник + пагинация, «Настройки → Журнал действий») |
| `/api/orders/timeline` | POST | история заказа: заявка/оплата/отгрузка/сдача/возврат/отмена одной лентой (`services/order_timeline.py`); доступ — как у карточки заказа (руководству — любой, менеджеру — свой) |
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

Принудительная инвалидация:

- При изменении роли — `services.database` лениво зовёт
  `services.roles.invalidate_role(user_id)`.

Складских кэшей нет и не нужно: запросы локальные. Прежние TTL-кэши поверх
МойСклад ушли вместе с интеграцией — вместе с классом ошибок «показали
устаревшие цифры, потому что кэш не сбросился».

---

## 11. Что где искать (quick reference)

| Хочу… | Смотреть |
|---|---|
| Изменить логику ролей / прав | `services/roles.py` (предикаты), `services/database.py` (хранение, ensure_user) |
| Добавить новое поле в заказ | `services/database.py:init_db` (тут CREATE TABLE + migrations) + использовать в `webapp/server.py` API endpoints |
| Добавить экран в WebApp | `webapp/static/index.html` (nav button), `webapp/static/app.js` (`case '...':` + `render*()`), `webapp/static/style.css` |
| Добавить команду в боте | новый файл в `handlers/`, зарегистрировать в `bot.py:register_routers` |
| Изменить печать документов | `services/printing.py` (CUPS) + `handlers/printing.py` (кнопка и `/printer`) |
| Изменить движение остатка | `services/warehouse.py` (накладные), `services/order_shipment.py` (отгрузка заказа), `services/container_receipt.py` (приёмка) |
| Изменить дневной пинг / операционную сводку | `tasks/run_ops_monitor.py` (пинг) + `services/ops_summary.py` (сбор) + `webapp/server.py:/api/ops-summary` |
| Найти ошибку в проде | Railway Logs у нужного сервиса. Долгие SQL логируются как `SQL slow ...` через `SQL_SLOW_MS` (default 200мс) |
| Понять что сейчас в БД | `services/database.py:init_db` — все таблицы там же |
| Локально запустить | `BOT_MODE=all`, `DATABASE_URL=` (пусто) → SQLite, `REDIS_URL=` (пусто) → MemoryStorage |

---

## 12. Чего НЕТ сейчас (заметки на будущее)

- **Полное** удаление psycopg2. Денежное ядро (order/payment/кредит/сдачи/
  возвраты) и склад уже на native async `asyncpg` (`services/adb_core.py`);
  psycopg2 остаётся для startup/миграций и backup-fallback.
- Связь между `payments` (отдельные платежи в кассу) и `orders` — разные сущности.
- Полноценная per-permission система помимо фиксированных ролей (есть
  per-user permission overrides, но не полный RBAC).
- Логи длиннее 7 дней — Better Stack / Axiom log drain.
- Перевод бота на webhook на проде (код готов, `TG_USE_WEBHOOK=1`; сейчас polling).

> Реализовано (раньше было «нет»): частичные оплаты; подтверждение оплаты;
> событийные уведомления; **возвраты** (полные/частичные);
> **сдачи наличных** (FIFO-закрытие); **кредит-лимиты с энфорсом** (override
> боссом); **reject→draft + freeze**; **деактивация юзеров** (=guest);
> **аналитика по менеджерам** (из локальных orders, WebApp); **конвертация
> валют в сводных** («≈ X USD» через `convert_to_base`); **полностью локальный
> складской учёт** (каталог, остатки, накладные, PDF) вместо МойСклад;
> asyncpg money-core; pytest+CI.

---

## 13. Качество: тесты, линт, типы

Конфиг тулчейна — в `pyproject.toml`; версии dev-тулов запинены в
`requirements-dev.txt`.

- **pytest** (`tests/`, fixture `isolated_db` — SQLite в tmp; env-заглушки в
  `conftest.py`). Принцип: мокаем **границу с сетью** (`aioresponses` для
  aiohttp, `tg_send_message` на верхнем уровне), а не свой код — иначе баг в
  обёртке проходит CI (так и случилось с `tg_send_message`/`base_url`). Покрыты:
  денежные инварианты, инварианты склада (остаток не в минус, порядок блокировок,
  идемпотентность отгрузки), регрессии безопасности (все `/api/*` требуют
  initData; HTML-escape).
- **ruff** — строгий гейт (`E9,F63,F7,F82`) + полный набор (`E9,F,B,ASYNC,UP,SIM`).
- **mypy** — точечно по `order_workflow/database/warehouse/order_shipment/
  container_receipt/counterparties/server`, **блокирующий**
  (0 ошибок). `pre-commit` — локальная первая линия.
- **CI** (`.github/workflows/ci.yml`): ruff + mypy + pytest с coverage-«храповиком»
  (`--cov-fail-under=55`).
- **Стартовый self-check** (`bot.py:_startup_selfcheck`): логирует `BOT_MODE` и
  проверяет, что Telegram-URL уведомлений собирается — ловит регресс на старте,
  а не «когда полезли в логи».
