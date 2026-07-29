# Аудит репозитория `moysklad_bot`

**Дата:** 2026-07-29
**Ревизия:** `55c06c4` (ветка `claude/bot-repository-audit-1snu9f`)
**Объём:** ~43 000 строк. Прочитано: точки входа (`bot.py`, `webapp/server.py`, `tasks/*`),
16 роутеров-хендлеров, слой БД (`services/database.py`, `adb_core.py`, `async_db.py`),
МС-интеграция (`moysklad`, `ms_demand`, `ms_customerorder`, `ms_payments`, `ms_returns`,
`ms_cancel`, `ms_sync_handler`, `ms_webhooks`), `snapshot`, все cron-CLI.

**Статус:** только анализ. Код не изменялся.

---

## Оглавление

1. [Карта потоков](#1-карта-потоков)
2. [Логические ошибки](#2-логические-ошибки)
3. [База данных](#3-база-данных)
4. [Лишнее](#4-лишнее)
5. [Приоритеты](#5-приоритеты)
6. [Чего не хватило для выводов](#6-чего-не-хватило-для-выводов)

---

# 1. КАРТА ПОТОКОВ

## 1.1 Старт и роли

```
/start → handlers/start.py → ensure_user() (database.py:3142)
       → роль: ADMIN_IDS→admin | BOSS_IDS→boss | ALLOWED_USERS→manager | иначе guest
       → все последующие проверки: services/roles.cached_role (TTL 60 c, per-process)
```

**Где рвётся порядок.** Кэш ролей у бота и у webapp — разные объекты в разных
контейнерах. Понижение роли доходит до второго процесса за ≤60 c. Для деактивации это
залатано отдельным кэшем (`cached_is_deactivated`, TTL 30 c), но он вызывается только в
`_authorize` webapp'а. Бот-хендлеры проверяют деактивацию лишь косвенно — через
`get_role`, возвращающий `guest`, с тем же 60-секундным лагом.

## 1.2 Создание заказа → заявка на отгрузку

```
/neworder | WebApp → create_order (status='draft')
  → add_order_item ×N          (webapp: _require_draft_order, server.py:2990)
  → update_order_agent
  → submit:
      bot:    handlers/orders.py:877  — читает order, проверяет status=='draft' в Python
      webapp: server.py:3086          — то же + set_order_payment
      ↓
      create_shipment_request()               (INSERT, database.py:5837)
      ↓
      update_order_status(order_id,'pending') (БЕЗ CAS, database.py:5733)
      ↓
      tg_send_message боссам, кнопки req_ok: / req_no:
```

**Разрыв порядка №1 (критичный).** Между чтением статуса и `update_order_status` нет
атомарности. Двойной тап → **две записи в `shipment_requests` со `status='pending'`** на
один заказ. Уникального индекса нет.

**Разрыв порядка №2.** `create_shipment_request` и `update_order_status` — две отдельные
транзакции. Краш между ними: заявка `pending`, заказ `draft`.

## 1.3 Одобрение заявки (самый длинный путь)

`services/order_workflow.approve_shipment_request` (`:220`) — общий код для бот-callback
`req_ok:` и для `/api/requests/approve`:

```
1.  get_shipment_request → status=='pending'?                       (:252)
2.  кредит-чек (только payment_type='credit')                       (:266–299) → needs_override
3.  АТОМАРНО: UPDATE shipment_requests ... WHERE status='pending'   (:304)
      ↳ внутри database.py:5896 — update_order_status(order,'approved') БЕЗ guard'а
4.  POST entity/customerorder в МойСклад                            (:351)
5.  add_audit_log + set_order_ms_customerorder_id                   (:365–374)
6.  POST entity/demand (линк на customerorder)                      (:385)
7.  add_audit_log + set_order_ms_demand_id                          (:398–407)
8.  notify_order_approved менеджеру                                 (:513)
9.  send_document PDF менеджеру и боссу                             (:525–551)
10. для payment_type='paid': add_payment(pending) + push боссу      (:555–588)
```

**Разрывы порядка (шаги 3→7).** Три независимых сетевых вызова и четыре коммита без
общей транзакции. Краш после шага 4 — документ в МС есть, `ms_customerorder_id` пуст;
ретрай создаст **дубль**. У `ms_payments` для этого есть детерминированный `syncId` +
pre-search (`ms_payments.py:161,180`); у `customerorder`/`demand` — нет.

## 1.4 Отгрузка

Три независимых входа, не знающих друг о друге:

| Вход | Код | Guard |
|---|---|---|
| вебхук МС `customerorder.UPDATE`, stateType=Successful | `ms_sync_handler.py:257,268` | `cas_order_status` + `set_order_shipped_meta` |
| `/ship N` | `handlers/order_ship.py:38` | `mark_order_shipped` (`WHERE status='approved'`) |
| `/api/orders/ship` | `server.py:2909` | то же |

## 1.5 Деньги (кредит-заказ)

```
mark_order_paid (database.py:5329)
  ├ FOR UPDATE на orders
  ├ пересчёт total / used / returns в копейках
  ├ INSERT payments(status='pending')
  └ orders.paid_at = COALESCE(paid_at, now)
     ↓ push боссу (pay_ok: / pay_no:)
confirm_payment (database.py:4437)
  ├ UPDATE payments ... WHERE status='pending'        (атомарно)
  ├ _maybe_close_order_after_payment                  (FOR UPDATE + платежи+сдачи−возвраты)
  └ _trigger_ms_paymentin_sync → POST entity/paymentin (fire-and-forget)
```

**Разрыв.** Шаг МС — best-effort вне транзакции. Страховка сделана аккуратно:
`run_ms_sync_retry` + `claim_payment_for_ms_sync` + reaper `reset_stale_in_progress_payments`
+ детерминированный `syncId`.

## 1.6 Сдача наличных / возврат

- `create_cash_deposit` (`:2229`) — advisory-lock по менеджеру + FIFO + INSERT в одной транзакции.
- `confirm_cash_deposit` (`:2302`) — UPDATE сдачи, затем **отдельными транзакциями** закрытие каждого покрытого заказа.
- `create_return` (`:2621`) — advisory-lock по заказу + dup-check + INSERT.
- `confirm_return` (`:2731`) — одна транзакция: статус возврата + `returned_qty` с overshoot-guard + статус заказа + cash-refund. Восстановление партий и `_maybe_close_order_after_payment` — после коммита.

## 1.7 Уведомления об отгрузках

`demand.CREATE` вебхук (webapp-процесс) → `notify_new_shipment`, дедуп через таблицу
`notified_shipments`. Резервный канал — поллер в bot-процессе каждые `CHECK_INTERVAL_SEC`
(900 c).

## 1.8 Cron

`run_debts_notify`, `run_ms_sync_retry`, `run_ops_monitor` (+ `claim_ops_monitor_run`),
`run_maintenance`, `run_ms_reconcile`, `run_backup` — все через
`tasks/_cron_runner.run_cron` с телеметрией в `cron_runs`.

---

# 2. ЛОГИЧЕСКИЕ ОШИБКИ

## 2.1 Двойная заявка → двойное списание остатков в МойСклад ⛔

**Файлы:** `handlers/orders.py:884–917`, `webapp/server.py:3103–3162`,
`services/order_workflow.py:252–311`, `services/database.py:5882–5904`,
`services/database.py:3677`.

**Сценарий.** Менеджер дважды тапает «Отправить заявку» (или клиент ретраит POST). Оба
вызова читают `status='draft'`, оба делают `create_shipment_request` → в
`shipment_requests` две строки `pending` на один `order_id`. Босс видит два одинаковых
уведомления и жмёт «Одобрить» на обоих. `approve_shipment_request` проверяет **только**
`req["status"]=='pending'` — про статус заказа не спрашивает, про уже проставленный
`ms_customerorder_id` не спрашивает.

**Что на выходе.** Два `customerorder` + два `demand` в МойСклад → **остатки списаны
дважды**, клиенту выставлено вдвое. `set_order_ms_customerorder_id` (`:3677`)
перезаписывает ссылку без guard'а → первый документ становится сиротой, которого не
найдёт ни `find_order_by_ms_customerorder_id`, ни реконсиляция. Локально в БД при этом
всё выглядит нормально.

Идемпотентность в webapp есть, но это `_IDEM_CACHE` — **in-memory, TTL 30 c, per-worker**
(`server.py:52–89`). Бот-путь идемпотентности не имеет вообще.

## 2.2 Статус заказа перезаписывается без проверки текущего ⛔

**Файлы:** `services/database.py:5896` и `:5921`.

```python
update_order_status(req["order_id"], "approved")   # безусловный UPDATE
```

**Сценарий.** МойСклад прислал `Unsuccessful` → `ms_sync_handler.py:257` переводит заказ
`pending → rejected`. Заявка при этом остаётся `pending` (её никто не трогает). Босс
открывает старое сообщение в чате и жмёт «Одобрить» → заявка становится `approved`, а
заказ **возвращается в `approved` из `rejected`** и получает новые документы в МС.

Симметрично `reject_shipment_request` (`:5921`) продавливает `rejected` из любого статуса.

## 2.3 Отмена из WebApp не откатывает документ в МойСклад ⛔

**Файлы:** `webapp/server.py:2926–2968` против `handlers/order_cancel.py:104–110`.

Бот после `cancel_order` вызывает `ms_cancel.reverse_customerorder`. WebApp — **не
вызывает**. Заказ в боте `cancelled`, `ms_cancel_synced_at` пуст, `customerorder` в МС жив
с резервом товара. Дальше `run_ms_reconcile` его не тронет
(`get_orders_with_ms_customerorder` исключает `cancelled`), и рассинхрон остаётся навсегда.

## 2.4 «Удалить черновик» делает разное в боте и в WebApp ⚠️

**Файлы:** `handlers/orders.py:971–996` против `database.py:4396–4434`.

- **Бот:** `update_order_status(order_id, 'rejected')` — заказ остаётся в БД, позиции
  остаются, при этом аудит пишет «Удалён черновик заказа #N».
- **WebApp:** реальный `DELETE` + ручной каскад по `order_items` + **отказ, если по заказу
  есть платежи** (`:4419`).

У бот-версии проверки на платежи нет, но и удаления нет — осиротевшего платежа не
возникнет. Расходится результат для пользователя и для аналитики: `rejected`-заказы
попадают в выборки, «удалённые» — нет.

## 2.5 `_stock_dirty` не переживает границу процессов ⛔

**Файлы:** `services/snapshot.py:280–321`, `bot.py:216` (`start_background_tasks`),
`webapp/server.py:454,459`.

`mark_stock_dirty()` и `invalidate_ms_cache()` вызываются в **webapp-процессе**
(обработчик MS-вебхука). `_stock_debounce_loop` запускается только из
`start_background_tasks`, а он вызывается только в `main()`, из которой `BOT_MODE=webapp`
выходит раньше (`bot.py:396`).

**На проде (два сервиса, как описано в CLAUDE.md):** флаг ставится в одном процессе,
читается в другом. Вебхуки по складским документам не дают ничего; остатки обновляются
только 2-часовым safety-net'ом (`tasks/scheduled.py`, `STOCK_INTERVAL`). TTL-кэш МС в
bot-процессе тоже не инвалидируется. Пользователь видит остатки давностью до 2 часов и
уверен, что они «онлайн».

## 2.6 Circuit breaker МойСклад практически не срабатывает ⚠️

**Файл:** `services/moysklad.py:182–225`.

`resp.raise_for_status()` (`:203`) бросает `aiohttp.ClientResponseError` внутри `try`, чей
`except` ловит **только** `TimeoutError` и `ClientConnectionError`. Исключение выходит из
функции, минуя `_circuit.record_failure()` на `:222`.

Итог: при 4xx и при 429/5xx на последней попытке брейкер не считает провал (и
`record_success` тоже не вызывает — счётчик просто застывает). Открывается он **только**
на таймаутах и сетевых обрывах. Заявленное в докстринге «после 5 подряд провальных
запросов» не выполняется.

## 2.7 Выдача прав через WebApp ничего не даёт ⛔

**Файлы:** `services/roles.py:247` (`has_permission`), `webapp/server.py:2378–2449`.

`has_permission()` **не вызывается ни из одной точки авторизации** — проверено по всему
коду вне тестов. Реальные решения принимают `is_boss` / `_has_role` / `allowed_roles=(...)`.

Админ в UI жмёт «выдать право», запись ложится в `user_permissions`,
`/api/permissions/user` честно показывает `granted: true, source: override` — а доступ не
меняется. Отзыв права (`revoke`) точно так же ничего не отзывает.

Побочно: `ROLE_DEFAULTS` (`roles.py:211`) даёт bookkeeper'у `manage_payments`, но
`can_manage_payments` (`:125`) — это `admin|boss`. Два источника истины, расходящиеся
между собой.

## 2.8 `/addrole` затирает имя пользователя и не снимает деактивацию ⚠️

**Файлы:** `handlers/users.py:60` → `database.py:3080–3083`.

```python
ok = await adb.set_role(target_id, "", "", role)   # username="", full_name=""
...
ON CONFLICT(user_id) DO UPDATE SET
    username = EXCLUDED.username,
    full_name = EXCLUDED.full_name,
    role = EXCLUDED.role
```

После `/addrole 123456 boss` у существующего пользователя `full_name` и `username`
становятся пустыми строками; `/users` показывает голый ID до следующего `/start`.

Второе: `set_role` не трогает `deactivated_at`. Админ назначает роль деактивированному →
`get_role` (`:2986`) всё равно отдаёт `guest`. Бот отвечает «✅ назначена роль
Руководитель», человек не может ничего сделать, причина нигде не видна.

## 2.9 Напоминание о долгах показывает полную сумму заказа ⚠️

**Файл:** `tasks/run_debts_notify.py:62–73`.

```python
total = sum(float(it.get("quantity",0)) * float(it.get("price",0) or 0) for it in items)
```

Ни подтверждённые платежи, ни сдачи, ни возвраты не вычитаются. `/api/debts`
(`server.py:3307–3313`) считает правильно. Клиент внёс 80% — в утреннем TG-напоминании
всё равно висит 100%, в WebApp 20%.

Там же `:127`: `bosses = [u for u in users if u["role"] in ("admin","boss")]` — **без
фильтра `deactivated_at`**, в отличие от `get_notify_recipients` (`notifier.py:154–157`).
Уволенный руководитель продолжает ежедневно получать полную сводку долгов компании.

## 2.10 `get_order_payment_summary` не знает про сдачи наличных ⚠️

**Файлы:** `database.py:3495–3550` (учитывает платежи и возвраты) против
`_maybe_close_order_after_payment:4754` и `confirm_cash_deposit:2340` (учитывают ещё и
`cash_deposit_orders`).

Заказ, закрытый сдачей налички, в БД имеет `status='paid'`, а в карточке заказа и в пуше
боссу (`handlers/debts.py:323`, `server.py:3550`) показывает `remaining > 0` — «клиент ещё
должен».

## 2.11 `submitted_at` читается, но никогда не записывается ⚠️

Колонка объявлена `database.py:812`, читается в `get_stale_pending_orders:1755` через
`COALESCE(submitted_at, created_at)`. Записи нет нигде в продакшн-коде (только в
`tasks/smoke_impl.py:126`).

**Последствие.** Заказ, созданный неделю назад, отклонённый и переотправленный сегодня,
`run_ops_monitor` немедленно объявит «зависшей заявкой (>48 ч)». Ложные срабатывания на
всех переотправленных заказах.

## 2.12 Возврат можно подтвердить, не приняв товар ⚠️

`mark_return_goods_received` (`:2720`) ставит `goods_received=1`, но `confirm_return`
(`:2731`) этот флаг **не проверяет**. Босс из WebApp (`/api/returns/confirm`) подтверждает
возврат → `returned_qty` вырос, статус заказа `returned`, деньги из кассы выданы
(`refund_method='cash'` → отрицательная сдача, `:2829`) — при том что товар физически
может не приехать.

## 2.13 Потенциальная взаимоблокировка пула в `create_cash_deposit` ⚠️

**Файлы:** `database.py:2261–2278` + `adb_core.py:139–141`.

Внутри `async with adb_core.transaction()` (коннект захвачен на всё время) вызывается
`await get_manager_open_orders_for_deposit(...)`, который делает **4 отдельных запроса на
каждый открытый заказ**, и каждый берёт свой коннект из того же пула. При `PG_POOL_MAX`
(10) одновременных сдачах все коннекты держат транзакции, внутренние запросы ждут
свободного — `asyncpg.Pool.acquire()` без таймаута ждёт вечно. Сценарий редкий,
восстановление — только рестарт.

Тот же участок — N+1 в чистом виде (`:1887–1890`).

## 2.14 Гонка reject→draft оставляет заявку в очереди ⚠️

**Файл:** `order_workflow.py:695–707`.

`reject_order_to_draft` коммитит перевод заказа в `draft`, затем **отдельным вызовом**
`mark_shipment_request_returned`. Краш между ними: заказ `draft`, заявка `pending` — висит
в очереди у босса и при одобрении переводит draft-заказ в `approved` (см. 2.2).

## 2.15 Поллер отгрузок теряет окно ⚙️

**Файл:** `notifier.py:373–374`.

```python
shipments = await get_shipments(last_check)
last_check = datetime.now()     # после round-trip'а
```

Отгрузки, созданные во время запроса, в следующее окно не попадут. Окно — секунды, дедуп
тут не помогает (это потеря, а не дубль). Основной канал — вебхук, практический риск
низкий.

## 2.16 Молча проглоченные исключения (только значимые места)

| Место | Что скрывает |
|---|---|
| `database.py:621–627` | Любая ошибка `CREATE TABLE` логируется как «таблица уже существует». Нет прав / кривой тип → таблицы нет, узнаём по 500-й в рантайме |
| `database.py:848–854` | `run_migrations`: `except: rollback()` **вообще без лога**. Неудавшийся `ALTER` (блокировка, а не «колонка есть») невидим |
| `database.py:683–689` | Индексы — то же, на уровне `debug` |
| `database.py:199–202` | `_invalidate_role_cache`: при поломке импорта роль остаётся закэшированной, повышение/понижение не применяется, ни строчки в логах |
| `database.py:4667–4669` | `_trigger_ms_paymentin_sync`, ветка cron: `except: pass` без лога |
| `bot.py:106,141,258` | `except Exception: pass` |
| `ms_demand.py:258–262, 322–325` | При ошибке `skipped` **не возвращается** в результате → `order_workflow.py:392` не покажет боссу, какие позиции не попали в МС |

## 2.17 Недостижимые / бесполезные ветки

- **`ms_sync_handler.py:36–43`.** `Successful → shipped`, а `_UPDATABLE_STATUSES` включает
  `pending`. Но `TRANSITIONS['pending']` = `[approved, rejected, draft]` —
  `pending→shipped` нелегален, поэтому такое событие **всегда** уходит в ветку
  «нелегальный переход» + алерт боссу. Для аккаунтов МС, где заказ помечают «Успешным» до
  апрува в боте, синк не работает никогда, только сыплются предупреждения.
- **`run_ops_monitor.py:101`** `build_expiring_batches_block` и вся FEFO-логика — мертвы,
  потому что `product_batches` никто не заполняет (см. §4).
- **`purge_soft_deleted` (`:3650`)** — мертва: `deleted_at` **не записывается нигде в
  коде** (проверено grep'ом по всему проекту). Все ~15 фильтров `(deleted_at IS NULL)`
  всегда истинны.

---

# 3. БАЗА ДАННЫХ

## 3.1 Схема vs. реальные операции

**`REAL` на Postgres — это `float4` (одинарная точность).** Это прямо отмечено в
докстринге `services/money.py:7`, и канонические `*_cents BIGINT` действительно введены.
Но часть read-путей всё ещё читает старые REAL-колонки:

| Место | Что читает |
|---|---|
| `webapp/server.py:3307–3310` | `/api/debts`: total из `it["price"]`, confirmed/pending из `p["amount"]` |
| `webapp/server.py:3414` | `_money_summary`: `SUM(p.amount)` — сводка «получено/ожидает» |
| `tasks/run_debts_notify.py:64` | суммы в утреннем напоминании |
| `tasks/run_ops_monitor.py:67,70,81` | суммы сдач и возвратов в дайджесте |
| `handlers/deposits.py:121,142,150` | `/my_deposits`, список сдач |
| `database.py:902–906` | recovery-backfill сравнивает `SUM(amount)` с `SUM(quantity*price)` |

При `ALLOWED_CURRENCIES` с UZS суммы в миллионы: float4 точно представляет целые до
2²⁴ ≈ 16.7 млн, но дробные значения уже искажает (`1234.56` хранится как
`1234.5599975585938`). Расхождение между «денежным ядром» (копейки) и этими экранами
гарантировано на копейки-единицы и накапливается при массовом суммировании.

## 3.2 Транзакции

**Сделано хорошо:** `mark_order_paid`, `_maybe_close_order_after_payment`,
`confirm_return`, `create_return`, `create_cash_deposit`, `delete_order` — единые
транзакции с `FOR UPDATE` / advisory-lock.

**Не покрыто:**

- **Submit заказа** — `create_shipment_request` + `update_order_status` в двух транзакциях (§2.1).
- **Approve** — `set_order_ms_customerorder_id`, `set_order_ms_demand_id`,
  `clear_order_ms_demand_failed`, аудит: четыре отдельных коммита вокруг двух сетевых вызовов.
- **`confirm_cash_deposit`** — UPDATE сдачи коммитится, потом по одной транзакции на
  закрываемый заказ (`:2344`). Краш посередине → сдача `confirmed`, часть заказов не
  закрыта. Реконсиляции для этого нет.
- **`return_order_to_draft`** — `reject_order_to_draft` + `mark_shipment_request_returned` (§2.14).
- **Advisory-lock'и работают только на Postgres** — `create_return:2686`,
  `create_cash_deposit:2262` обёрнуты в `if USE_POSTGRES`. На SQLite защиты нет
  (принимается: локалка однопроцессная).

## 3.3 Сироты и связи

FK нет нигде (сознательное решение, задокументировано). Последствия:

- `purge_soft_deleted` (`:3650`) удаляет строки только из `orders`, `cash_deposits`,
  `returns` — **не трогает** `order_items`, `return_items`, `cash_deposit_orders`,
  `payments`, `order_change_log`. Правда, эта функция и так мертва (§2.17), так что сирот
  пока не производит.
- Бот-путь «удаления» черновика (`handlers/orders.py:986`) не удаляет ничего — просто
  меняет статус.
- `orders.ms_customerorder_id` / `ms_demand_id` без UNIQUE:
  `find_order_by_ms_customerorder_id` (`:4283`) берёт `LIMIT 1` из потенциально
  нескольких строк.

## 3.4 Отсутствующие уникальные индексы

| Нужен | Зачем |
|---|---|
| `shipment_requests(order_id) WHERE status='pending'` | закрывает §2.1 на уровне БД |
| `returns(order_id) WHERE status='pending'` | сейчас только advisory-lock, и только на Postgres |
| `orders(ms_customerorder_id)`, `orders(ms_demand_id)` | однозначность обратного поиска |

## 3.5 Поля, которые пишутся и не читаются (или наоборот)

| Колонка | Статус |
|---|---|
| `orders.deleted_at` | **читается в ~15 запросах, не пишется нигде** |
| `orders.submitted_at` | читается (`:1755`), не пишется (§2.11) |
| `orders.approved_by`, `approved_at` | миграция `:813–814`, не пишутся |
| `orders.price_check_warnings`, `client_notification_sent` | не пишутся, не читаются |
| `user_roles.active` | миграция `:767`, нигде не используется (вместо неё `deactivated_at`) |
| `user_roles.email`, `phone` | не пишутся, не читаются |
| `order_items.stock_snap`, `price_at_submit`, `price_at_submit_cents`, `batch_id` | объявлены и бэкфиллятся (`:928`), но **не заполняются** ни при добавлении позиции, ни при сабмите. `batch_id` читается в `confirm_return:2847` — всегда NULL |
| `returns.goods_received` | пишется, не проверяется при подтверждении (§2.12) |
| `idempotency_keys.expires_at` | пишется (`:3031`), **нигде не читается**; ключи не чистятся ни в `run_maintenance:39–44`, ни где-либо ещё → таблица растёт вечно |
| `cron_runs.metadata` | в него кладётся `str(duration_ms)` (`:3945`) — отдельной колонки для длительности нет |

## 3.6 Даты и таймзоны

Конвенция (`utc_now`/`local_now`, пороги считаются в Python и передаются параметром) в
новых местах соблюдается аккуратно. Оставшиеся расхождения:

- `orders.due_date` сравнивается с `date.today().isoformat()` (`server.py:3281`,
  `run_debts_notify.py:98`). `date.today()` берёт TZ процесса. Bot и WebApp — **разные
  контейнеры Railway**; если у них разойдётся `TZ`, «просрочено сегодня» будет отличаться
  на день.
- `snapshot_refresh_task` использует `utc_now()` для «06:00» (`tasks/scheduled.py`), всё
  остальное — local. При `TZ=Asia/Tashkent` ежедневный рефреш справочников приходится на
  11:00 по местному.
- `ms_demand.py:275`: `moment` документа пишется как `utc_now()`, а МойСклад ожидает время
  в TZ аккаунта → документы в МС датируются со смещением (для UZ — минус 5 часов).

## 3.7 Единицы измерения

- В МС цена уходит как `int(round(price_major * 100))` (`ms_demand.py:241`,
  `ms_customerorder.py:252`) — **банковское округление Python** на float. Каноническая
  функция `money.to_cents` (Decimal, ROUND_HALF_UP) и уже готовое `price_cents`
  игнорируются. На границе (`x.xx5`) документ в МС расходится с локальной суммой на копейку.
- `cash_deposits` не имеет колонки валюты — касса ведётся в BASE_CURRENCY, и это корректно
  защищено `_is_base_currency` (`:1854`) в FIFO и конверсией в `confirm_return:2822`. Но в
  UI валюта захардкожена как `USD`: `handlers/deposits.py:104,121,142,150`,
  `run_ops_monitor.py:68,70,82`. При `BASE_CURRENCY != USD` цифры подписаны неверно.

## 3.8 Индексы и N+1

**Отсутствуют индексы под реальные фильтры:**

- `cash_deposit_orders(order_id)` — PK `(deposit_id, order_id)` для поиска по `order_id`
  не работает; а по нему идут `_order_confirmed_deposit_cents:1791`,
  `_order_allocated_deposit_cents:1824`, `get_confirmed_deposit_cents_for_orders:1182`.
- `payments(status)` и `payments(order_id, status)` — `get_payments_needing_ms_sync`,
  `confirm_all_pending_*`.
- `idx_orders_credit_due` — это `(payment_type, paid_at, due_date)`, а
  `get_open_debts:5663` фильтрует по `payment_type` + `paid_confirmed_at` + `status`.
  Индекс запрос не покрывает.
- `user_roles(role)` — `get_all_users()` делает полный скан с сортировкой и вызывается
  **на каждое уведомление** через `get_notify_recipients`.

**N+1:**

- `get_manager_open_orders_for_deposit:1877` — 4 запроса × N заказов.
- `handlers/deposits.py:139` — `get_cash_deposit_orders` в цикле, при том что батч-хелпер
  `get_cash_deposit_orders_batch` существует и используется в webapp.
- `/api/home` для босса (`server.py:793–802`) — четыре полных `SELECT *`
  (`get_pending_cash_deposits`, `get_pending_returns`,
  `get_paid_orders_awaiting_confirmation`, `get_open_debts`) **только чтобы взять
  `len()`**. Плюс `get_user_orders` (`:5182`) без `LIMIT`. Rate-limit на эндпоинте —
  120/мин.

## 3.9 Неограниченный рост нагрузки на МС

`get_payments_with_ms_paymentin:4230`, `get_orders_with_ms_customerorder:4304`,
`get_orders_with_ms_demand:4321` — **без окна по дате и без LIMIT**.
`tasks/run_ms_reconcile` делает по одному GET в МойСклад на каждую строку, **ежечасно**.
Через год работы это тысячи запросов в час → 429 → (при работающем брейкере) каскадный
отказ остальных МС-функций. Нужно окно вида «изменённые за последние N дней» или курсор.

---

# 4. ЛИШНЕЕ

| Что | Почему считаю неиспользуемым |
|---|---|
| **Партии/FEFO целиком**: таблица `product_batches` (`:486`), `upsert_product_batch` (`:2485`), `select_batches_fefo` (`:2537`), `get_batches_expiring_within` (`:2576`), `order_items.batch_id`, `build_expiring_batches_block` (`run_ops_monitor.py:101`) | `upsert_product_batch` и `select_batches_fefo` не вызываются ниоткуда вне тестов; `batch_id` не записывается → `confirm_return:2847` всегда получает NULL; таблица всегда пуста → блок в дайджесте всегда `None` |
| Таблица `client_contacts` (`:536`) | ни одного SELECT/INSERT в коде |
| Таблица `audit_archive_exports` (`:523`) | ни одного обращения; CLAUDE.md прямо пишет «внешний архив аудита (Google Drive) убран», таблица осталась |
| Таблица `failed_notifications` (`:508`) + индекс `:663` | ни одного обращения; retry-крона для неё нет |
| **Система per-user permissions**: таблица `user_permissions`, `has_permission`, `ROLE_DEFAULTS`, 3 эндпоинта `/api/permissions/*` | `has_permission` не вызывается ни из одной точки авторизации (§2.7). Это не просто мёртвый код — это **вводящий в заблуждение** UI |
| `get_pending_confirmations` (`:5625`), `confirm_payment_received` (`:5534`), `reject_payment_received` (`:5581`) | не вызываются нигде вне тестов. Это старая одноступенчатая схема `paid_at → paid_confirmed_at`, вытесненная моделью `payments`. Живой код закрывает заказ через `_maybe_close_order_after_payment` |
| `is_order_returnable` (`:2611`), `remove_user` (`:3233`), `get_unsynced_managers` (`:3225`), `archive_payment` (`:4825`) | ноль вызовов вне определения |
| `notifier.send_to_recipients` (`:218`) | ноль вызовов; рассылка идёт через `notify._broadcast` и `tg_send_message` |
| `purge_soft_deleted` (`:3650`) + настройка `soft_delete_retention_days` + все `(deleted_at IS NULL)` | `deleted_at` не записывается нигде → механизм soft-delete существует только на бумаге |
| `_load_predefined_users` (`:5966`) в части `PREDEFINED_USERS` | `config.py` эту переменную не определяет ни в одной ветке → список всегда пуст, `__import__("config").PREDEFINED_USERS` всегда падает в `except` |
| `user_roles.active`, `.email`, `.phone` | добавлены миграцией, ни read, ни write |

**Не считаю лишним** (проверено, используется): `tasks/verify_ms_returns.py`,
`verify_ms_cancel.py`, `diagnose_ms_debts.py`, `seed_dev.py`, `smoke_impl.py` — это
диагностические CLI, они по определению не вызываются из продакшн-кода.

---

# 5. ПРИОРИТЕТЫ

| # | Проблема | Серьёзность | Сложность починки | Что сломается, если не чинить |
|---|---|---|---|---|
| 1 | §2.1 Двойной сабмит → две заявки → двойные `customerorder`+`demand` в МС | **критично** | средняя (UNIQUE-индекс + CAS `WHERE status='draft'` + guard на `ms_customerorder_id` в approve) | Двойное списание остатков, задвоенная отгрузка клиенту, расхождение склада с МС. Восстанавливается только вручную |
| 2 | §2.2 `update_order_status` в approve/reject без проверки текущего статуса | **критично** | низкая (добавить `WHERE status = ...`) | Отменённый/отклонённый заказ воскресает в `approved` и получает новые документы в МС |
| 3 | §2.3 Отмена из WebApp не делает реверс в МойСклад | **критично** | низкая (вызвать `ms_cancel.reverse_customerorder`, как в боте) | Отменённые заказы копятся в МС с резервом товара; реконсиляция их не ловит |
| 4 | §2.7 Гранты прав через WebApp не влияют на доступ | **критично** | средняя (подключить `has_permission` в точки авторизации либо убрать UI) | Админ считает, что выдал/отозвал доступ. Отозванный доступ на деле остаётся — дыра в контроле |
| 5 | §2.5 `_stock_dirty` не работает между процессами | **важно** | средняя (флаг в Redis или дебаунс-цикл в webapp-процессе) | Остатки в WebApp устаревают до 2 часов; менеджеры продают то, чего нет |
| 6 | §3.9 Реконсиляция МС без окна/лимита | **важно** | низкая (окно по `updated_at` + LIMIT) | Через месяцы работы — 429 от МойСклад ежечасно, каскад на остальные МС-операции |
| 7 | §2.9 Долги в TG-напоминании — полная сумма вместо остатка; деактивированные боссы получают рассылку | **важно** | низкая (переиспользовать формулу из `/api/debts`; добавить фильтр `deactivated_at`) | Менеджер требует с клиента уже уплаченное; уволенный сотрудник видит финансы компании |
| 8 | §2.11 `submitted_at` не пишется | **важно** | низкая (проставлять при submit) | Все переотправленные заказы ложно попадают в «зависшие заявки», дайджест теряет доверие |
| 9 | §2.10 Сдачи не учтены в `get_order_payment_summary` | **важно** | низкая (добавить слагаемое, как в `_maybe_close`) | Закрытый сдачей заказ показывает долг; менеджер повторно требует деньги |
| 10 | §2.13/§2.1 In-memory идемпотентность на денежных ручках (`mark_paid`, `approve`, `confirm_payment`) | **важно** | средняя (перевести на `idem_claim`, как в `deposits/create`) | Double-click при двух воркерах / после рестарта → лишний платёж, лишний документ в МС |
| 11 | §2.8 `/addrole` затирает имя и молчит про деактивацию | **важно** | низкая (`COALESCE` в UPSERT + предупреждение) | Список пользователей превращается в список ID; назначенная роль «не работает» без объяснения |
| 12 | §2.6 Circuit breaker не считает HTTP-ошибки | **важно** | низкая (перенести `record_failure` в правильную ветку) | При деградации МС event loop копит 30-секундные таймауты — защита, которая есть на бумаге, не включится |
| 13 | §2.12 Возврат подтверждается без `goods_received` | **важно** | низкая (guard в `confirm_return`) | Деньги из кассы выданы за товар, который не приехал |
| 14 | §3.5 `idempotency_keys` не чистятся | **важно** | низкая (добавить в `run_maintenance`) | Таблица растёт бесконечно; протухший ключ отдаёт старый результат |
| 15 | §3.1 REAL-колонки на денежных экранах | **важно** | средняя (перевести read-пути на `*_cents`) | Расхождение сумм между экранами; для UZS — видимая погрешность |
| 16 | §3.8 N+1 и полные выборки на `/api/home`, `/deposits`, FIFO-сдач | важно | низкая–средняя | Главный экран замедляется линейно с ростом БД; при 120 req/min и пуле из 10 коннектов — 500-е |
| 17 | §2.13 Deadlock пула в `create_cash_deposit` | важно | низкая (вынести FIFO-расчёт из транзакции или передать `txn`) | При всплеске сдач процесс зависает намертво до рестарта |
| 18 | §2.16 Глушение ошибок DDL/миграций | важно | низкая (логировать реальную ошибку, отличать «колонка есть») | Провалившаяся миграция выглядит как успешная; ошибка вылезает в рантайме как 500 |
| 19 | §2.14 Разрыв reject→draft; §2.4 расхождение «удалить черновик»; §2.15 окно поллера | косметика/важно | низкая | Единичные подвисшие заявки, разное поведение одной кнопки в двух UI |
| 20 | §3.6 TZ: `date.today()` в двух контейнерах, `moment` в UTC | косметика | низкая | Просрочка «уезжает» на день; документы в МС датированы со смещением |
| 21 | §3.7 Захардкоженный «USD» в UI сдач/дайджеста | косметика | низкая | При смене `BASE_CURRENCY` цифры подписаны неверной валютой |
| 22 | §4 Мёртвый код: партии/FEFO, 3 пустые таблицы, soft-delete, 8 неиспользуемых функций | косметика | низкая | Растёт цена чтения кода; ложное ощущение, что FEFO и soft-delete работают |

---

# 6. ЧЕГО НЕ ХВАТИЛО ДЛЯ ВЫВОДОВ

1. **Дампа боевой схемы Postgres.** Все выводы про типы (`REAL` = float4) сделаны по
   `CREATE TABLE` в `_create_tables` — но прод-БД проходила через `run_migrations`, и я не
   знаю, не менялись ли типы вручную. Если есть `\d+ orders`, `\d+ payments` — §3.1 можно
   будет уточнить.
2. **Реальной топологии Railway.** Из CLAUDE.md следует два сервиса (`BOT_MODE=bot` +
   `BOT_MODE=webapp`). Выводы §2.5 (`_stock_dirty`) верны именно для неё; при
   `BOT_MODE=all` проблемы нет.
3. **Числа воркеров uvicorn.** `start_webapp` (`server.py:3768`) поднимает один процесс, но
   если на Railway стоит внешний `--workers > 1`, то `_IDEM_CACHE`, `_role_cache`,
   `_settings_cache` и `rate_limit` расходятся между воркерами — §2.1 и §2.13 становятся
   заметно вероятнее.
4. **Порядка cron-расписаний в Railway.** Я опирался на комментарии в докстрингах
   (`*/15`, `0 * * * *`, `0 3 * * *`). Оценка §3.9 («тысячи запросов в час») зависит от
   того, действительно ли `run_ms_reconcile` ежечасный.
