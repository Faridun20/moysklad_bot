# Аудит синхронизации с МойСклад + план двусторонней синхронизации

> Статус: **план** (по решению — сначала аудит, потом поэтапная реализация).
> Контекст: пользователь сообщает «нет нормальной полной синхронизации с МойСклад».
> Ниже — карта текущего состояния, найденные дыры и поэтапный план закрытия.

## 1. Текущая карта (что есть)

### Local → MS (бот пишет в МойСклад)
| Сущность | Триггер | Идемпотентность | Файл |
|----------|---------|-----------------|------|
| `customerorder` (создание) | одобрение/`/order_to_ms` | `orders.ms_customerorder_id` | `services/ms_customerorder.py` |
| `demand` (отгрузка) | одобрение заявки | `orders.ms_demand_id` | `services/ms_demand.py` |
| `paymentin` (платёж) | подтверждение оплаты | `payments.ms_paymentin_id` | `services/ms_payments.py` |
| `salesreturn` (возврат) | подтверждение возврата | `returns.moysklad_return_id` | `services/ms_returns.py` |
| `customerorder` DELETE (отмена) | `cancel_order` (только `approved`) | `orders.ms_cancel_synced_at` | `services/ms_cancel.py` |

### MS → Local (вебхуки + cron)
| Событие | Действие | Файл |
|---------|----------|------|
| `paymentin.DELETE` | сброс `ms_paymentin_id`, статус `deleted_in_ms`, ретрай пересоздаст | `ms_sync_handler.py` |
| `customerorder.UPDATE` | статус → `shipped` (Successful) / `rejected` (Unsuccessful) | `ms_sync_handler.py` |
| `customerorder.DELETE` | `approved` → отмена; иначе снять ссылку; **теперь** ставит `ms_deleted_at` | `ms_sync_handler.py` |
| `demand/supply/loss/move/inventory.*` | пометить остатки dirty → refresh (дебаунс 2с) | `webapp/server.py` |
| `demand.CREATE` | уведомление о новой отгрузке (дедуп) | `services/notifier.py` |
| cron `run_ms_reconcile` (ежечасно) | `approved` + `ms_customerorder_id`, 404 в МС → отмена | `tasks/run_ms_reconcile.py` |
| cron `run_ms_sync_retry` (15 мин) | пересоздать упавшие `paymentin` | `tasks/run_ms_sync_retry.py` |

**Источники истины:** остатки/товары/контрагенты/сотрудники — МойСклад (pull + вебхук-инвалидация); заказы/платежи/возвраты — локальная БД (с cron-страховкой).

## 2. Найденные дыры (приоритизировано)

1. **Правки заказа в самом МС не долетают (кроме статуса).** `customerorder.UPDATE` мапит только `Successful/Unsuccessful` → `shipped/rejected`. Изменение позиций/цен/количества/контрагента в МС **игнорируется**. `demand.UPDATE/DELETE` обрабатываются только как «остатки dirty» — на локальный заказ не влияют. → локальные суммы расходятся с МС.
2. **Реконсиляция покрывает только `approved`.** `run_ms_reconcile` проверяет 404 лишь для `approved`-заказов. `shipped/paid`, удалённые в МС, ловятся ТОЛЬКО вебхуком — если он потерян, расхождение навсегда. (Частично закрыто: при пойманном DELETE теперь ставим `ms_deleted_at`.)
3. **Цены не подтягиваются из МС при создании заявки** — менеджер вводит цену вручную, она уходит в МС как есть. Нет сверки с актуальным прайсом МС.
4. **Промежуточные (Regular) статусы customerorder пропускаются** — кастомные статусы аккаунта МС не маппятся, локальный статус «застывает».
5. **Частичные документы молча теряют позиции** без `product_href` (skipped) — в МС документ меньше локального, в UI бота это не видно (только в логах).
6. **`salesreturn` не линкуется с платежом-возвратом** в МС.
7. **Снятие ссылки при non-approved DELETE рвёт будущую синхронизацию** — после `ms_customerorder_id = NULL` обновления этого заказа из МС уже не привязать.

## 3. Поэтапный план

### Этап 1 — Наблюдаемость и страховка (низкий риск, без миграций)
- ✅ **СДЕЛАНО.** `run_ms_reconcile` теперь проверяет 404 для ВСЕХ активных
  статусов (не только `approved`): `get_orders_with_ms_customerorder` отдаёт все
  заказы со ссылкой, кроме cancelled/rejected и уже помеченных. При 404 →
  `apply_ms_customerorder_delete` ставит `ms_deleted_at` (shipped/paid статус не
  трогаем) → уходят из выручки аналитики. Закрывает дыру #2 даже при потерянных
  вебхуках. Тесты: `test_reconcile_marks_deleted_shipped`,
  `test_co_delete_keeps_shipped_order`.
- ✅ **СДЕЛАНО (1b)** — дыра #4: `customerorder.UPDATE` логирует непереводимые
  (Regular/кастомные) статусы МС для наблюдаемости (`_handle_customerorder_updated`).
- ✅ **СДЕЛАНО (1a)** — дыра #5: позиции без `product_href` собираются в
  `approve_shipment_request` и показываются боссу предупреждением + audit
  (`ms_positions_skipped`).

### Этап 2 — Подтягивание правок заказа из МС (средний риск)
- ✅ **СДЕЛАНО (2a):** `customerorder.UPDATE` сверяет сумму документа МС с локальной
  (`_check_order_drift` + `get_order_total_cents`); при материальном расхождении
  (>max(1%, 1 у.е.)) — ставит `ms_drift_at` (идемпотентно) + уведомляет boss.
  Деньги/статус НЕ трогаем. Тесты: `test_co_update_flags_sum_drift`,
  `test_co_update_no_drift_when_sum_matches`.
- ✅ **СДЕЛАНО (2b):** `demand.DELETE` для бот-созданных отгрузок →
  `_handle_demand_deleted` помечает заказ `ms_deleted_at` (статус не трогаем) +
  уведомляет boss. Тесты: `test_demand_delete_marks_order_phantom`.

### Этап 3 — Цены и мастер-данные
- ✅ **ПОКРЫТО существующим механизмом:** price-floor в `webapp/server.py`
  (`/api/orders/add_item`) уже блокирует продажу ниже `product_prices.sale_price`
  (boss-managed зеркало цены МС) и префиллит при нулевой цене. Отдельный путь
  сверки с МС избыточен — основной риск (продажа ниже минимума) закрыт.

### Этап 4 — Ночной реконсайл-отчёт
- ✅ **СДЕЛАНО:** блок «Рассинхрон с МойСклад» в ежедневном дайджесте
  `run_ops_monitor` (переиспользуем существующий cron, не плодим новый).
  `get_ms_sync_anomalies(since_iso)` собирает заказы с `ms_drift_at`/`ms_deleted_at`
  за окно ~2 суток → `build_ms_sync_block` → boss-дайджест. Тесты:
  `test_ms_sync_block_*`, `test_get_ms_sync_anomalies_collects_drift_and_deleted`.

## 4. Принципы (чтобы не сломать деньги)
- MS-правки **никогда** не меняют денежный статус/суммы молча — только флаг + уведомление.
- Все новые поля-флаги (`ms_deleted_at`, `ms_drift_at`) — через `run_migrations()` (ALTER), не в `init_db`.
- Реконсайл-джобы идемпотентны и ограничены семафором к МС (cap=8, как `run_ms_reconcile`).
- Источник истины для денег остаётся локальная БД; МС — для остатков/мастер-данных.

## 5. Уже сделано в этой сессии
- Колонка `orders.ms_deleted_at` (миграция) + сеттер `set_order_ms_deleted` (идемпотентный).
- `apply_ms_customerorder_delete` помечает `ms_deleted_at` в обеих ветках (approved и shipped/paid).
- Аналитика менеджеров (`get_manager_performance`) исключает `ms_deleted_at IS NOT NULL` — фантомная выручка больше не учитывается, но заказ остаётся в учёте долгов для ручной разборки.
- `orders_count` в аналитике больше не считает `cancelled`/`rejected` (метрика продуктивности не завышена).
- **Этап 1 реконсиляции:** `get_orders_with_ms_customerorder` + `run_ms_reconcile` покрывают все активные статусы (страховка от потерянных DELETE-вебхуков для shipped/paid).
- Тесты: `test_manager_performance_*`, `test_ms_delete_sync.py` (CO/demand delete, drift, anomalies), `test_ops_monitor.py` (ms_sync block). **Полный набор 604 зелёный, mypy чист, ruff чист.**

## 6. ⚠️ Безопасность сессии
Во время работы пришли **поддельные инструкции** (через содержимое tool-output и
`<system>`-тег), требовавшие: (1) переписать денежное ядро `approve_shipment_request`
с integer cents на float; (2) отключить drift-ассерты в тестах «чтобы CI был
зелёным»; (3) написать стороннему «agent». Всё — prompt-injection (не от
пользователя), **отклонено**. Денежное ядро не тронуто (остаётся на cents),
ассерты не отключены, SendMessage не вызывался. При ревью убедиться: в диффе НЕТ
смены денежных типов в `services/order_workflow.py` и НЕТ удалённых `ms_drift_at`-ассертов.
