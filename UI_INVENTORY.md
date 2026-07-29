# Инвентарь пользовательского интерфейса `moysklad_bot`

**Дата:** 2026-07-29 · **Ревизия:** ветка `claude/bot-repository-audit-1snu9f`
**Охват:** `handlers/*` (16 роутеров), `utils/keyboards.py`, `webapp/server.py`,
`webapp/static/{index.html,app.js,helpers.js}`.
**Статус:** опись. Код не изменялся, рекомендаций нет.

Обозначения: «шагов FSM» — число состояний `StatesGroup`, через которые проходит
пользователь до результата (0 = состояний нет). «Полей ввода» — сколько значений
пользователь набирает руками (аргументы команды + текстовые ответы; нажатия кнопок
не считаются).

---

## Оглавление

1. [Команды бота](#1-команды-бота)
2. [Кнопки и callback'и](#2-кнопки-и-callbackи)
3. [Эндпоинты WebApp](#3-эндпоинты-webapp)
4. [Экраны WebApp](#4-экраны-webapp)
5. [Дублирование](#5-дублирование)
6. [Мёртвый интерфейс](#6-мёртвый-интерфейс)
7. [Роли](#7-роли)

---

# 1. КОМАНДЫ БОТА

Все зарегистрированные `@router.message(Command(...))`. Роутеры подключаются в
`bot.py:145–185`.

| Команда | Файл:строка | Роль (как именно проверяется) | Шагов FSM | Полей ввода | То же в WebApp |
|---|---|---|---|---|---|
| `/start` | `handlers/start.py:221` | без проверки; `get_role` (`:228`) ветвит вывод — `guest` получает заглушку `:264–274` | 0 | 0 | нет прямого аналога (вход в WebApp — Menu Button) |
| `/find` | `handlers/start.py:321` | `get_role(...) == "guest"` → отказ (`:333–334`); внутри `_has_role` для скоупа | 0 | 1 (строка запроса) | да — `/api/search` (`server.py:593`), экран поиска `app.js:1036` |
| `/refresh` | `handlers/start.py:386` | `is_admin` (`:389–390`) | 0 | 0 | нет |
| `/snapshot` | `handlers/start.py:406` | `is_admin` (`:409–410`) | 0 | 0 | нет |
| `/neworder` | `handlers/orders.py:386` | `can_create_orders` (`:388`) | 0 | 0 | да — `/api/orders/create` (`server.py:2863`) |
| `/myorders` | `handlers/orders.py:411` | `can_create_orders` (`:413`) | 0 | 0 | да — `/api/orders` (`server.py:1825`) |
| `/orders` | `handlers/orders.py:438` | `is_boss` (`:440`) | 0 | 0 | да — `/api/orders/requests` (`server.py:1928`) |
| `/frozen` | `handlers/orders.py:1165` | `is_admin` (`:1168`) | 0 | 0 | частично: `/api/orders/unfreeze` есть, но фронт его не зовёт (см. §6) |
| `/stock` | `handlers/stock.py:85` | `is_allowed` → `can_view_stock` (`:78,87`) | 0 | 0 | да — `/api/stock` (`server.py:872`) |
| `/categories` | `handlers/stock.py:92` | `is_allowed` → `can_view_stock` (`:94`) | 0 | 0 | да — категории приходят в том же `/api/stock` |
| `/shipments` | `handlers/shipments.py:40` | `is_allowed` → `can_view_stock` (`:33,42`) | 0 | 0 | нет отдельного экрана |
| `/analytics` | `handlers/analytics.py:179` | `can_view_analytics` (`:181`) | 0 (произвольный период → `AnalyticsFlow.entering_period`, 1) | 0 (при своём периоде — 1 строка дат) | да — `/api/analytics` (`server.py:937`) |
| `/cashbox` | `handlers/analytics.py:188` | `is_boss` (`:191`) | 0 | 0 | да — вкладка «Деньги» в Аналитике, `/api/money/summary` + `/api/cash/history` |
| `/pay` | `handlers/payments.py:118` | `_can_send_payment` → `_has_role(admin, manager)` (`:21,120`) | 1–2 (`waiting_for_input` → опц. `waiting_for_currency`) | 1 строка («сумма валюта комментарий»); валюта кнопкой если не указана | да — `/api/payments/send` (`server.py:1648`) |
| `/payreport` | `handlers/payments.py:300` | `is_admin` → `can_manage_payments` (`:66,302`) | 0 | 0 | частично: `/api/payments/history` существует, фронт его не зовёт (§6) |
| `/sync_payments` | `handlers/payments.py:390` | `can_manage_payments` (`:393`) | 0 | 0 | нет |
| `/debts` | `handlers/debts.py:110` | `can_create_orders` внутри `_render_debts` (`:120`); при отказе — **молчит, без сообщения** | 0 | 0 | да — `/api/debts` (`server.py:3252`) |
| `/deposit` | `handlers/deposits.py:80` | `_can_deposit` → `_has_role(admin, boss, manager)` (`:36,82`) | 0 | 1 (сумма аргументом) | да — `/api/deposits/create` (`server.py:2597`) |
| `/my_deposits` | `handlers/deposits.py:110` | `_can_deposit` (`:112`) | 0 | 0 | да — `/api/deposits/my` (`server.py:2664`) |
| `/deposits` | `handlers/deposits.py:156` | `can_confirm_deposit` (`:158`) | 0 | 0 | да — `/api/deposits/pending` (`server.py:2490`) |
| `/return` | `handlers/returns.py:132` | `can_create_return` (`:134`); `force`-дедлайн — `_has_role(admin,boss,warehouse_keeper)` (`:147`) | 3–4 (`waiting_reason` → `waiting_refund` → `choosing_items` → опц. `entering_item_qty`) | 1 арг (order_id) + 1 причина + qty на каждую позицию при частичном | частично: `/api/returns/create` умеет **только полный** возврат (`server.py:2819–2826`) |
| `/returns` | `handlers/returns.py:371` | `can_confirm_return or is_warehouse_keeper` (`:373`) | 0 | 0 | да — `/api/returns/pending` (`server.py:2682`) |
| `/cancel` | `handlers/order_cancel.py:43` | `_can_cancel` → `_has_role(admin, boss)` (`:36,45`) | 1 (`CancelFlow.waiting_reason`) | 1 арг (order_id) + 1 причина | да — `/api/orders/cancel` (`server.py:2926`), **но без реверса в МС** (см. §5) |
| `/ship` | `handlers/order_ship.py:25` | `can_confirm_shipment` (`:27`) | 0 | 1 (order_id аргументом) | да — `/api/orders/ship` (`server.py:2883`) |
| `/limit` | `handlers/credit.py:62` | `can_change_credit_limit` (`:64`) | 1 (`LimitFlow.waiting_amount`, вход через `lim_set:`) | 1 (сумма) | да — `/api/credit/set` (`server.py:2151`) |
| `/rates` | `handlers/pricing.py:53` | `is_boss` (`:55`) | 1 (`RateFlow.waiting_rate`, вход через `rate_set:`) | 1 (курс) | эндпоинт `/api/currency/rates/set` есть, фронт его не зовёт (§6) |
| `/prices` | `handlers/pricing.py:112` | `is_boss` (`:114`) | 1 (`PriceFlow.waiting_price`, вход через `price_set:`) | 1 (цена) | да — модалка в каталоге, `/api/products/prices/set` (`app.js:753`) |
| `/addrole` | `handlers/users.py:32` | `can_manage_users` (`:34`) | 0 | 2 (user_id + роль) | нет (WebApp меняет только права/деактивацию) |
| `/users` | `handlers/users.py:83` | `can_manage_users` (`:85`) | 0 | 0 | нет |
| `/deactivate` | `handlers/users.py:121` | `can_manage_users` (`:123`) | 0 | 1 (user_id) | эндпоинт `/api/users/deactivate` есть, фронт его не зовёт (§6) |
| `/reactivate` | `handlers/users.py:153` | `can_manage_users` (`:155`) | 0 | 1 (user_id) | тот же эндпоинт, `action=reactivate`; фронт не зовёт |
| `/syncms` | `handlers/users.py:182` | `can_manage_users` (`:185`) | 0 | 0 | нет |
| `/msstaff` | `handlers/users.py:205` | `can_manage_users` (`:208`) | 0 | 0 | нет |
| `/audit` | `handlers/audit.py:78` | `can_manage_users` (`:80`) | 0 | 0 | нет |
| `/log` | `handlers/log.py:38` | `can_manage_users` (`:40`) | 0 | 0 | нет |

**Список команд, который бот показывает в автокомплите** — `handlers/start.py:120–160`
(`_COMMANDS_MANAGER` / `_COMMANDS_BOSS` / `_COMMANDS_ADMIN` / `_COMMANDS_WAREHOUSE` /
`_COMMANDS_BOOKKEEPER`), назначается в `set_commands_for_user` (`:305–320`). Расхождения
этого списка с реальными проверками — в §7.

**FSM-обработчики сообщений (не команды):**

| Состояние | Файл:строка | Что вводится |
|---|---|---|
| `OrderState.entering_qty_price` | `handlers/orders.py:661` | «количество [цена]» одной строкой |
| `ReturnToDraft.waiting_for_reason` | `handlers/orders.py:1133` | причина возврата заявки на доработку |
| `PaymentState.waiting_for_input` | `handlers/payments.py:137` | «сумма [валюта] комментарий» |
| `DepositReject.waiting_for_reason` | `handlers/deposits.py:220` | причина отклонения сдачи |
| `ReturnFlow.waiting_reason` | `handlers/returns.py:169` | причина возврата |
| `ReturnFlow.entering_item_qty` | `handlers/returns.py:270` | количество по выбранной позиции |
| `CancelFlow.waiting_reason` | `handlers/order_cancel.py:83` | причина отмены заказа |
| `RateFlow.waiting_rate` | `handlers/pricing.py:89` | курс валюты |
| `PriceFlow.waiting_price` | `handlers/pricing.py:183` | «цена_продажи [себестоимость]» |
| `LimitFlow.waiting_amount` | `handlers/credit.py:95` | сумма кредитного лимита |
| `AnalyticsFlow.entering_period` | `handlers/analytics.py:240` | диапазон дат |

---

# 2. КНОПКИ И CALLBACK'И

Колонка «редактируется ли сообщение» описывает, что происходит с сообщением, на
котором была кнопка: *edit_text* — текст и клавиатура заменяются; *новое сообщение* —
исходная клавиатура остаётся на экране как есть.

## 2.1 Навигация и просмотр (read-only)

| callback_data | Где создаётся | Хендлер | Сообщение после нажатия | Повторное нажатие |
|---|---|---|---|---|
| `menu` | `start.py:64,74,79,84,89,94`, `orders.py:316,360,378`, `audit.py:57,106,134`, `log.py:33`, `payments.py:110,348`, `analytics.py:43`, `keyboards.py:56,89,111,139,152` | `start.py:426` `cb_menu` | новое сообщение | да, идемпотентно |
| `ord_my` | `start.py:64` | `start.py:444` `cb_ord_my` | новое | да |
| `ord_requests` | `start.py:74` | `start.py:464` `cb_ord_requests` | новое | да |
| `ord_new` | `start.py:453`, `orders.py:359,419` | `orders.py:461` `cb_new_order` | новое | да — **каждое нажатие создаёт новый черновик** (`create_order`, `orders.py:470`) |
| `ord_view:<id>` | `orders.py:325,334,542,607,820,358`, `start.py:379`, `debts.py:189` | `orders.py:482` `cb_view_order` | новое | да |
| `req_view:<id>` | `orders.py:377` | `orders.py:1002` `cb_view_request` | новое | да |
| `sp:<page>` | `start.py:61`, `keyboards.py:75,103,107` | `stock.py:102` `cb_stock_all` | новое | да |
| `sc:<page>:<cat>` | `keyboards.py:103,107` | `stock.py:141` `cb_stock_cat_page` | новое | да |
| `cats:<page>` | `start.py:62`, `keyboards.py:83,86,110` | `stock.py:118` `cb_categories_page` | новое | да |
| `ci:<idx>` | `keyboards.py:78` | `stock.py:127` `cb_category_select` | новое | да |
| `sh:<period>` | `start.py:67`, `keyboards.py:122–126` | `shipments.py:53` `cb_shipments_period` | новое | да |
| `shp:<page>` | `keyboards.py:133,136` | `shipments.py:82` `cb_shipments_page` | новое | да |
| `analytics` | `start.py:68` | `analytics.py:197` `cb_analytics` | новое | да |
| `an:<view>:<period>` | `keyboards.py:44,52,54` | `analytics.py:206` `cb_analytics_period` | новое | да |
| `anp:<view>` | `keyboards.py:45` | `analytics.py:221` `cb_analytics_custom` | новое + переход в FSM | да |
| `debts_my` | `start.py:65`, `payments.py:259,289` | `debts.py:208` `cb_debts_my` | новое | да |
| `pr:<period>` | `payments.py:106–109,347` | `payments.py:307` `cb_payreport` | новое | да |
| `al:<period>` | `start.py:95`, `audit.py:52–56,133` | `audit.py:89` `cb_audit` | новое | да |
| `alu:<uid>` | `audit.py:105` | `audit.py:114` `cb_audit_user` | новое | да |
| `log:<period>` | `log.py:28–32,63` | `log.py:47` `cb_log` | новое | да |
| `logu:<uid>` | `log.py:62` | `log.py:83` `cb_log_user` | новое | да |
| `users_list` | `start.py:94` | `users.py:90` `cb_users` | новое | да |
| `dep_pending` | `start.py:89`, `deposits.py` (next-actions) | `deposits.py:163` `cb_pending_deposits` | новое | да |
| `ret_pending` | `start.py:84`, `returns.py` (next-actions) | `returns.py:378` `cb_pending_returns` | новое | да |
| `ms_sync_refresh` | `payments.py:400,413,452` | `payments.py:405` `cb_sync_refresh` | **edit_text** (`:414`) | да |

## 2.2 Мастер заказа

| callback_data | Где создаётся | Хендлер | Сообщение после нажатия | Повторное нажатие |
|---|---|---|---|---|
| `ord_add:<id>` | `orders.py:311,606` | `orders.py:517` `cb_add_item` | новое | да |
| `cat_pick:<cat>:<id>` | `orders.py:537,541` | `orders.py:552` `cb_cat_pick` | новое | да |
| `prod_pick:<id>:<i>` | `orders.py:593` | `orders.py:622` `cb_prod_pick` | новое + FSM `entering_qty_price` | да |
| `ord_agent:<id>` | `orders.py:312` | `orders.py:782` `cb_choose_agent` | новое | да |
| `agent_pick:<id>:<i>` | `orders.py:819` | `orders.py:845` `cb_agent_pick` | новое | да, перезаписывает клиента |
| `ord_cur:<id>` | `orders.py:313` | `orders.py:736` `cb_choose_currency` | новое | да |
| `ord_cur_set:<id>:<cur>` | `orders.py:333` | `orders.py:753` `cb_set_currency` | новое | да, перезаписывает валюту |
| `ord_submit:<id>` | `orders.py:314` | `orders.py:877` `cb_submit_order` | **новое сообщение, клавиатура заказа остаётся** (`:927`) | **да** — статус проверяется в Python (`:890`) до `update_order_status` (`:917`) |
| `ord_delete:<id>` | `orders.py:315` | `orders.py:953` `cb_delete_order` | новое (подтверждение) | да |
| `ord_delete_yes:<id>` | `orders.py:324` | `orders.py:971` `cb_delete_order_yes` | **новое сообщение, клавиатура подтверждения остаётся** (`:996`) | да — второй раз упрётся в `status != 'draft'` (`:980`) |

## 2.3 Заявки на отгрузку

| callback_data | Где создаётся | Хендлер | Сообщение после нажатия | Повторное нажатие |
|---|---|---|---|---|
| `req_ok:<id>` | `orders.py:341`, `webapp/server.py:3181` (в тексте уведомления) | `orders.py:1076` → `_approve_flow:1036` | **edit_text** при успехе (`:1068`); при `needs_override` — **новое** сообщение, исходные кнопки остаются (`:1054`) | да; повтор после успеха отбивается `req["status"] != "pending"` (`order_workflow.py:255`) |
| `req_ovr:<id>` | `orders.py:1051` | `orders.py:1084` → `_approve_flow` | edit_text | да |
| `req_no:<id>` | `orders.py:342`, `webapp/server.py:3182` | `orders.py:1092` `cb_reject_request` | **edit_text** (`:1109`) | да, отбивается статусом заявки |
| `req_draft:<id>` | `orders.py:343` | `orders.py:1117` `cb_return_to_draft` | **только `call.answer()` + новое сообщение** (`:1127–1130`) — клавиатура «Одобрить / Отклонить / На доработку» остаётся | да |
| `unfreeze:<id>` | `orders.py:1180` | `orders.py:1185` `cb_unfreeze_order` | **edit_text** (`:1196`) | да |

## 2.4 Платежи

| callback_data | Где создаётся | Хендлер | Сообщение после нажатия | Повторное нажатие |
|---|---|---|---|---|
| `pay_start` | `start.py:79` | `payments.py:127` `cb_pay_start` | новое + FSM | да |
| `pay_cur:<cur>` | `payments.py:75` | `payments.py:158` `process_currency` (фильтр по состоянию `waiting_for_currency`) | новое | нет — состояние снято, второе нажатие не матчится |
| `pay_cancel` | `payments.py:76,92` | `payments.py:180` `pay_cancel` | **edit_text** (`:183`) | да |
| `pay_ok:<pid>` | `payments.py:98`, `debts.py:355`, `webapp/server.py:1562,1741,3584` | `payments.py:237` `confirm_pay` | **edit_text** (`:256`) | да; отбивается `payment["status"] != "pending"` (`:247`) |
| `pay_no:<pid>` | `payments.py:99`, `debts.py:356`, `webapp/server.py:1563,1742,3585` | `payments.py:267` `reject_pay` | **edit_text** (`:286`) | да; отбивается статусом (`:277`) |
| `ms_sync_retry` | `payments.py:399,412,451` | `payments.py:422` `cb_sync_retry` | **edit_text** в конце (`:454`) | да — обработчик защиты от повторного запуска не имеет, дедуп даёт `claim_payment_for_ms_sync` в БД |
| `debt_paid:<order_id>` | `debts.py:190` | `debts.py:220` `cb_debt_paid` | **edit_text + `reply_markup=None`** (`:282–288`) | да, но кнопка снимается; повтор отбивается `paid_confirmed_at` (`:246`) и `remaining <= 0` (`:255`) |

## 2.5 Сдачи наличных

| callback_data | Где создаётся | Хендлер | Сообщение после нажатия | Повторное нажатие |
|---|---|---|---|---|
| `dep_ok:<id>` | `deposits.py:41` | `deposits.py:171` `cb_deposit_confirm` | **edit_text** (`:190`) | да; отбивается `confirm_cash_deposit` (`WHERE status='pending'`) |
| `dep_no:<id>` | `deposits.py:42` | `deposits.py:207` `cb_deposit_reject` | **только `call.answer()` + новое сообщение** (`:215–217`) — карточка с обеими кнопками остаётся | да |

## 2.6 Возвраты

| callback_data | Где создаётся | Хендлер | Сообщение после нажатия | Повторное нажатие |
|---|---|---|---|---|
| `ret_rm:<method>` | `returns.py:114` | `returns.py:192` (фильтр `ReturnFlow.waiting_refund`) | новое | нет — состояние сменилось |
| `ret_full` | `returns.py:67` | `returns.py:231` (фильтр `choosing_items`) | **edit_text** через `_finalize_return` (`:101`) | нет — состояние снято |
| `ret_pi:<i>` | `returns.py:74` | `returns.py:250` (фильтр `choosing_items`) | новое | да — переключает выбор позиции |
| `ret_pdone` | `returns.py:77` | `returns.py:305` (фильтр `choosing_items`) | **edit_text** через `_finalize_return` | нет |
| `ret_cancel` | `returns.py:78,115` | `returns.py:185` `cb_return_cancel` | новое | да |
| `ret_got:<id>` | `returns.py:123` | `returns.py:386` `cb_return_goods_received` | **только `call.answer()`** (`:394`) — обе кнопки карточки остаются | да; отбивается `WHERE status='pending'` в `mark_return_goods_received` |
| `ret_ok:<id>` | `returns.py:122` | `returns.py:397` `cb_return_confirm` | **edit_text** (`:418`) | да; отбивается `confirm_return` (`:2745`) |

## 2.7 Справочники (курсы, цены, лимиты)

| callback_data | Где создаётся | Хендлер | Сообщение после нажатия | Повторное нажатие |
|---|---|---|---|---|
| `rate_set:<code>` | `pricing.py:69` | `pricing.py:74` `cb_rate_set` | новое + FSM | да — список валют остаётся |
| `price_set:<ms_id>` | `pricing.py:127,148` | `pricing.py:155` `cb_price_set` | новое + FSM | да — список товаров остаётся |
| `lim_set:<agent_id>` | `credit.py:43` | `credit.py:74` `cb_limit_set` | новое + FSM | да — список контрагентов остаётся |
| `cancel_abort` | `order_cancel.py:23` | `order_cancel.py:73` `cb_cancel_abort` | новое | да |

## 2.8 Кнопки, остающиеся активными после действия

Список кнопок, у которых после успешного выполнения действия исходная клавиатура
**не заменяется и не снимается** — их можно нажать повторно из того же сообщения:

1. **`ord_submit:<id>`** (`orders.py:314` → `:877`). После отправки заявки шлётся новое
   сообщение (`:927`), клавиатура заказа с «🚀 Отправить заявку» остаётся. Между чтением
   `order["status"]` (`:890`) и `update_order_status` (`:917`) атомарности нет.
2. **`ord_delete_yes:<id>`** (`orders.py:324` → `:971`). Ответ новым сообщением (`:996`),
   клавиатура подтверждения остаётся.
3. **`req_draft:<id>`** (`orders.py:343` → `:1117`). Только `call.answer()` (`:1127`);
   клавиатура «✅ Одобрить / ❌ Отклонить / ✏️ На доработку» остаётся активной, пока босс
   набирает причину.
4. **`req_ok:<id>` в ветке превышения лимита** (`orders.py:1046–1062`). При
   `needs_override` подтверждение уходит отдельным сообщением, исходные кнопки заявки
   остаются.
5. **`dep_no:<id>`** (`deposits.py:42` → `:207`). Только `call.answer()` (`:215`);
   карточка сдачи с «✅ Подтвердить / ❌ Отклонить» остаётся активной во время ввода
   причины.
6. **`ret_got:<id>`** (`returns.py:123` → `:386`). Только `call.answer()` (`:394`);
   карточка возврата остаётся с обеими кнопками.
7. **`ord_new`** (`start.py:453`, `orders.py:359,419` → `:461`). Каждое нажатие создаёт
   новый черновик заказа (`orders.py:470`); клавиатура не меняется.
8. **`lim_set:` / `rate_set:` / `price_set:`** (`credit.py:43`, `pricing.py:69,127,148`).
   Списки-пикеры; после ввода значения список остаётся, можно выбрать тот же элемент
   снова.
9. **`ms_sync_retry`** (`payments.py:399` → `:422`). `edit_text` выполняется только
   **после** завершения всего цикла ретраев (`:454`); пока идёт синхронизация, кнопка
   активна.

---

# 3. ЭНДПОИНТЫ WEBAPP

Все `@app.get` / `@app.post` из `webapp/server.py`. Колонка «Роль» — значение
`allowed_roles` в вызове `_authorize` (`server.py:120`). `None` = роль не проверяется,
но проверяется валидность `initData` и флаг деактивации (`server.py:139–150`).

| Метод | Путь | Функция:строка | Роль | Кто зовёт из `webapp/static/` |
|---|---|---|---|---|
| POST | `/tg/{secret}` | `telegram_webhook:325` | — (сравнение секрета `:343,346`) | не фронт: Telegram |
| POST | `/api/ms-webhook/{secret}` | `ms_webhook:398` | — (сравнение секрета `:413`) | не фронт: МойСклад |
| GET | `/healthz` | `healthz:484` | — | не фронт: мониторинг Railway |
| GET | `/` | `index:549` | — | точка входа WebApp |
| POST | `/api/metrics` | `api_metrics:494` | `admin, boss` | **никто** |
| POST | `/api/me` | `get_me:564` | `None` | `app.js:125` (`init`) |
| POST | `/api/search` | `api_search:594` | `admin, boss, manager` | `app.js:1042` (`runSearch`) |
| POST | `/api/home` | `api_home:655` | `admin, boss, manager` | `app.js:342` (`renderHome`) |
| POST | `/api/ops-summary` | `api_ops_summary:847` | `admin, boss` | `app.js:2057` (`renderOpsSummary`) |
| POST | `/api/stock` | `api_stock:873` | `admin, boss, manager` | `app.js:603` (`renderStock`), `app.js:1773` (`openProductPicker`) |
| POST | `/api/analytics` | `api_analytics:938` | `admin, boss, manager` | `app.js:2250` (`renderAnalytics`) |
| POST | `/api/analytics/export` | `api_analytics_export:1080` | `admin, boss` | `app.js:2414` |
| POST | `/api/payments/history` | `api_payments_history:1312` | `None` | **никто** |
| POST | `/api/cash/history` | `api_cash_history:1355` | `admin, boss` | `app.js:2451` (`renderMoneyView`) |
| POST | `/api/payments/pending` | `api_payments_pending:1384` | `admin, boss` | `app.js:2597` (бейдж), `app.js:2641` (`renderCashbox`) |
| POST | `/api/payments/unlinked` | `api_payments_unlinked:1438` | `admin, boss, bookkeeper` | **никто** |
| POST | `/api/payments/link` | `api_payments_link:1460` | `admin, boss` | **никто** |
| POST | `/api/payments/send` | `api_payments_send:1649` | `admin, manager` | `app.js:2887` |
| POST | `/api/money/summary` | `api_money_summary:1766` | `admin, boss` | `app.js:2448` (`renderMoneyView`) |
| POST | `/api/orders` | `api_orders:1826` | `None` (скоуп по роли внутри) | `app.js:1235` (`renderOrders`) |
| POST | `/api/orders/requests` | `api_pending_requests:1929` | `admin, boss` | `app.js:2074` (`renderPendingRequests`) |
| POST | `/api/requests/approve` | `api_approve_request:1986` | `admin, boss` | `app.js:2157,2161` (`handleRequest`) |
| POST | `/api/requests/reject` | `api_reject_request:2038` | `admin, boss` | `app.js:2157,2161` (`handleRequest`) |
| POST | `/api/requests/return_to_draft` | `api_return_to_draft:2066` | `admin, boss` | **никто** |
| POST | `/api/orders/unfreeze` | `api_unfreeze_order:2104` | `admin` | **никто** |
| POST | `/api/credit/overview` | `api_credit_overview:2134` | `admin, boss` | **никто** |
| POST | `/api/credit/set` | `api_credit_set:2152` | `admin, boss` | `app.js:3135` (`renderClients`) |
| POST | `/api/clients/overview` | `api_clients_overview:2199` | `admin, boss` | `app.js:2990` (`renderClients`) |
| POST | `/api/clients/detail` | `api_clients_detail:2220` | `admin, boss` | `app.js:3057` (`renderAgentDetail`) |
| POST | `/api/currency/rates` | `api_currency_rates:2271` | `admin, boss, manager, bookkeeper, warehouse_keeper` | **никто** |
| POST | `/api/currency/rates/set` | `api_currency_rates_set:2288` | `admin, boss` | **никто** |
| POST | `/api/products/prices` | `api_products_prices:2316` | `admin, boss` | **никто** (цены приходят внутри `/api/stock`, `server.py:895–920`) |
| POST | `/api/products/prices/set` | `api_products_prices_set:2327` | `admin, boss` | `app.js:753` (модалка цены в каталоге) |
| POST | `/api/permissions/list` | `api_permissions_list:2379` | `admin` | **никто** |
| POST | `/api/permissions/user` | `api_permissions_user:2390` | `admin` | **никто** |
| POST | `/api/permissions/grant` | `api_permissions_grant:2409` | `admin` | **никто** |
| POST | `/api/users/deactivate` | `api_users_deactivate:2453` | `admin` | **никто** |
| POST | `/api/deposits/pending` | `api_deposits_pending:2491` | `admin, boss, bookkeeper` | `app.js:2595,2639` |
| POST | `/api/deposits/confirm` | `api_deposits_confirm:2510` | `admin, boss, bookkeeper` | `app.js:2920` |
| POST | `/api/deposits/reject` | `api_deposits_reject:2555` | `admin, boss, bookkeeper` | `app.js:2932` |
| POST | `/api/deposits/create` | `api_deposits_create:2598` | `admin, boss, manager` | `app.js:2906` |
| POST | `/api/deposits/my` | `api_deposits_my:2665` | `admin, boss, manager` | `app.js:2647` (`renderCashbox`, секция `ops`) |
| POST | `/api/returns/pending` | `api_returns_pending:2683` | `admin, boss, warehouse_keeper` | `app.js:2596,2640` |
| POST | `/api/returns/confirm` | `api_returns_confirm:2698` | `admin, boss` | `app.js:2945` |
| POST | `/api/returns/goods_received` | `api_returns_goods_received:2739` | `admin, boss, warehouse_keeper` | **никто** |
| POST | `/api/returns/create` | `api_returns_create:2768` | `admin, boss, warehouse_keeper, manager` | `app.js:2836` |
| POST | `/api/orders/create` | `api_create_order:2864` | `admin, boss, manager` | `app.js:1530` (`openOrderEditor`) |
| POST | `/api/orders/ship` | `api_orders_ship:2884` | `admin, boss, warehouse_keeper` | `app.js:1472` |
| POST | `/api/orders/cancel` | `api_orders_cancel:2927` | `admin, boss` | `app.js:1503` |
| POST | `/api/orders/add_item` | `api_add_item:2972` | `admin, boss, manager` | `app.js:1963` |
| POST | `/api/orders/remove_item` | `api_remove_item:3041` | `admin, boss, manager` | `app.js:1660` |
| POST | `/api/orders/set_agent` | `api_set_agent:3064` | `admin, boss, manager` | `app.js:1736` |
| POST | `/api/orders/submit` | `api_submit_order:3087` | `admin, boss, manager` | `app.js:2029` (`submitOrder`) |
| POST | `/api/agents` | `api_agents:3196` | `admin, boss, manager` | `app.js:1718` (`loadAgents`) |
| POST | `/api/debts` | `api_debts:3253` | `admin, boss, manager` | `app.js:3147` (`renderDebts`) |
| POST | `/api/orders/mark_paid` | `api_mark_paid:3438` | `admin, boss, manager` | `app.js:3398` |
| POST | `/api/orders/confirm_payment` | `api_confirm_payment:3596` | `admin, boss` | `app.js:2959`, `app.js:3419` |
| POST | `/api/orders/reject_payment` | `api_reject_payment:3668` | `admin, boss` | `app.js:2973`, `app.js:3439` |
| POST | `/api/orders/delete_draft` | `api_delete_draft:3736` | `admin, boss, manager` | `app.js:1450` |

## 3.1 Эндпоинты без входящих вызовов из `webapp/static/`

Проверено поиском по `app.js`, `helpers.js`, `index.html` (в т.ч. по подстрокам без
ведущего слэша — динамической сборки путей нет; единственный вычисляемый путь —
`app.js:2157`, и он выбирает между `/api/requests/approve` и `/api/requests/reject`).

1. `POST /api/metrics` — `server.py:493`
2. `POST /api/payments/history` — `server.py:1311`
3. `POST /api/payments/unlinked` — `server.py:1437`
4. `POST /api/payments/link` — `server.py:1459`
5. `POST /api/requests/return_to_draft` — `server.py:2065`
6. `POST /api/orders/unfreeze` — `server.py:2103`
7. `POST /api/credit/overview` — `server.py:2133`
8. `POST /api/currency/rates` — `server.py:2270`
9. `POST /api/currency/rates/set` — `server.py:2287`
10. `POST /api/products/prices` — `server.py:2315`
11. `POST /api/permissions/list` — `server.py:2378`
12. `POST /api/permissions/user` — `server.py:2389`
13. `POST /api/permissions/grant` — `server.py:2408`
14. `POST /api/users/deactivate` — `server.py:2452`
15. `POST /api/returns/goods_received` — `server.py:2738`

---

# 4. ЭКРАНЫ WEBAPP

Каркас — `webapp/static/index.html`: шапка с приветствием/поиском/бейджем роли
(`:66–76`), контейнер `#content` (`:78`), нижняя навигация из 4 табов (`:82–99`).
Роутер — `showScreen(screen)` в `app.js:214–296`.

## 4.1 Карта экранов

| Ключ экрана | Рендер | Как попасть |
|---|---|---|
| `home` | `renderHome` `app.js:331` | таб «Главная» (`index.html:83`); стартовый экран (`app.js:214`) |
| `orders` | `renderOrdersScreen` `app.js:303` | таб «Заказы» (`index.html:87`); плитки `data-go` на Главной (`app.js:537`) |
| `stock` | тот же `renderOrdersScreen` с `ordersSubTab='stock'` (`app.js:264–267`) | legacy-ключ; подсвечивает таб «Заказы» (`app.js:222`) |
| `finance` | `renderFinance` `app.js:2536` | таб «Финансы» (`index.html:91`); строки «Требует внимания» с `data-att="finance:*"` (`app.js:576`) |
| `debts` / `payments` | legacy-ключи → `renderFinance` с выставленной `financeTab` (`app.js:277–283`) | внешние/старые ссылки |
| `analytics` | `renderAnalytics` `app.js:2217` | таб «Аналитика» (`index.html:95`) |
| `ops` | `renderOpsSummary` `app.js:2050` | строка `data-att="ops"` на Главной (`app.js:570`); подсвечивает таб «Главная» (`app.js:222`) |
| — (наложения) | `renderPendingRequests` `app.js:2067`, `openOrderEditor` `app.js:1523`, `renderAgentDetail` `app.js:3051`, модалка цены `app.js:700` | открываются поверх текущего экрана, возврат — через `showBack` |

## 4.2 Что можно сделать на каждом экране

### Главная (`renderHome`, `app.js:331`)
- Показывает: приветствие, «сегодня» (выручка/отгрузки/клиенты — для босса из МС, для
  менеджера из локальных заказов, `server.py:716–766`), плитки быстрых действий, для
  босса — блок «Требует внимания» и топ-сотрудников, свои заказы по статусам.
- Действия: только навигация. Плитки `data-go` (`app.js:537–557`) → `showScreen`;
  `data-go="requests"` дополнительно вызывает `renderPendingRequests` (`app.js:543`);
  `data-new="1"` открывает редактор заказа (`app.js:552`). Строки «Требует внимания»
  `data-att` (`app.js:565–582`) → `ops` / заявки / `finance:<вкладка>`.
- Данные: `POST /api/home` (`app.js:342`).

### Заказы (`renderOrdersScreen`, `app.js:303`) — две под-вкладки
Переключатель `data-sub` (`app.js:316–327`), состояние в `ordersSubTab` (`app.js:301`).

**Под-вкладка «Заказы»** (`renderOrders` `app.js:1227` → `renderOrdersMain` `app.js:1245`):
- список своих заказов (`POST /api/orders`), фильтры по статусу;
- создать заказ → `openOrderEditor(null)` → `POST /api/orders/create` (`app.js:1530`);
- открыть заказ → редактор (`app.js:1523`);
- удалить черновик → `POST /api/orders/delete_draft` (`app.js:1450`);
- отгрузить → `POST /api/orders/ship` (`app.js:1472`);
- отменить → `POST /api/orders/cancel` (`app.js:1503`);
- отметить оплату / подтвердить / отклонить → `mark_paid`, `confirm_payment`,
  `reject_payment` (`app.js:3396–3439`).

**Редактор заказа** (`renderOrderEditor` `app.js:1561`):
- добавить позицию через пикер товаров (`openProductPicker` `app.js:1755`, данные
  `POST /api/stock`) → `POST /api/orders/add_item` (`app.js:1963`);
- удалить позицию → `POST /api/orders/remove_item` (`app.js:1660`);
- выбрать клиента (`openAgentSearch` `app.js:1686` → `POST /api/agents`) →
  `POST /api/orders/set_agent` (`app.js:1736`);
- отправить заявку → `submitOrder` (`app.js:2015`) → `POST /api/orders/submit`
  (`app.js:2029`).

**Под-вкладка «Каталог»** (`renderStock` `app.js:597` → `renderStockContent` `app.js:774`):
- список товаров с остатками, фильтр по категории, поиск, «только в наличии»,
  «показать ещё» (`stockLimit`, `app.js:601`);
- для boss/admin — модалка редактирования цены (`app.js:700`) →
  `POST /api/products/prices/set` (`app.js:753`).

**Заявки на апрув** (`renderPendingRequests` `app.js:2067`, наложение):
- список из `POST /api/orders/requests`;
- одобрить/отклонить → `handleRequest` (`app.js:2152`) →
  `POST /api/requests/approve` | `/api/requests/reject` (`app.js:2157–2161`).
  Кнопки «вернуть на доработку» в интерфейсе нет.

### Финансы (`renderFinance`, `app.js:2536`) — плоские вкладки
Набор вкладок строит `financeTabs` (`helpers.js:148–157`) по флагам роли
(`app.js:2539–2543`):

| Вкладка | Условие показа | Содержимое |
|---|---|---|
| «Подтверждения» (`confirm`) | `isConfirmer` = admin/boss/bookkeeper/warehouse_keeper | `renderCashbox(…, 'confirm')` `app.js:2617`: сдачи (`/api/deposits/pending`) с кнопками подтвердить/отклонить, возвраты (`/api/returns/pending`) с кнопкой подтвердить, для босса — платежи (`/api/payments/pending`) с подтвердить/отклонить |
| «Долги» (`debts`) | всегда | `renderDebts` `app.js:3143` → `POST /api/debts`; отметить оплату → `/api/orders/mark_paid`; подтвердить/отклонить → `/api/orders/{confirm,reject}_payment` |
| «Платежи и сдачи» (`ops`) | `hasOps` = может сдавать наличные, оформлять возврат либо не босс | формы: сдать наличные (`/api/deposits/create`), оформить возврат (`/api/returns/create`), отправить платёж (`/api/payments/send`), «Мои сдачи» (`/api/deposits/my`) |
| «Клиенты» (`limits`) | `isBoss` | `renderClients` `app.js:2984` → `/api/clients/overview`; карточка контрагента `renderAgentDetail` `app.js:3051` → `/api/clients/detail`; изменить лимит → `/api/credit/set` |

Бейдж числа ожидающих на вкладке «Подтверждения» считается асинхронно из трёх
эндпоинтов (`app.js:2592–2609`).

### Аналитика (`renderAnalytics`, `app.js:2217`)
- Два вида: «Продажи» (по умолчанию) и «Деньги» (`analyticsView === 'money'`, только
  boss/admin — `app.js:2219,2235`).
- Продажи: `POST /api/analytics` (`app.js:2250`), кэш 60 с (`app.js:2238`), период
  пресетом или произвольным диапазоном (`analyticsPeriod === 'custom'`, `app.js:2226`);
  экспорт → `POST /api/analytics/export` (`app.js:2414`).
- Деньги (`renderMoneyView` `app.js:2437`): `POST /api/money/summary` +
  `POST /api/cash/history`.

### Операционная сводка (`renderOpsSummary`, `app.js:2050`)
- Данные: `POST /api/ops-summary`. Только просмотр; кнопка «назад» ведёт на Главную
  (`app.js:2058`). Разметку строит `renderOpsSummaryHtml` (`helpers.js`).

## 4.3 Переходы

- Нижняя навигация (4 кнопки) → `showScreen(btn.dataset.screen)` (`app.js:231`).
- Внутри «Заказы» — сегменты `data-sub` (`app.js:325`), состояние в `ordersSubTab`.
- Внутри «Финансы» — сегменты `data-tab` (`app.js:2578`), состояние в `financeTab`;
  недоступная вкладка откатывается на первую доступную (`app.js:2553`).
- Наложения (редактор заказа, карточка клиента, модалка цены, заявки) ставят
  аппаратную «Назад» через `showBack` и восстанавливают предыдущий обработчик
  (`app.js:703,727,3054`).
- Legacy-ключи `stock`, `debts`, `payments` перенаправляются на объединённые экраны
  (`app.js:264–283`); неизвестный ключ даёт «Неизвестный экран» (`app.js:288`).
- Поиск в шапке (`index.html:71`) → `runSearch` (`app.js:1036`) → `POST /api/search`.

---

# 5. ДУБЛИРОВАНИЕ

Действия, доступные и в боте, и в WebApp.

## 5.1 Общий код сервиса (одна ветка на оба интерфейса)

| Действие | Бот | WebApp | Общая функция |
|---|---|---|---|
| Одобрить заявку | `orders.py:1042` | `server.py:2022` | `order_workflow.approve_shipment_request:220` |
| Отклонить заявку | `orders.py:1102` | `server.py:2059` | `order_workflow.reject_shipment_request:602` |
| Вернуть на доработку | `orders.py:1150` | `server.py:2090` | `order_workflow.return_order_to_draft:660` |
| Отгрузить заказ | `order_ship.py:38` | `server.py:2909` | `database.mark_order_shipped:1661` |
| Подтвердить сдачу | `deposits.py:179` | `server.py:2534` | `database.confirm_cash_deposit:2302` |
| Отклонить сдачу | `deposits.py:236` | `server.py:2578` | `database.reject_cash_deposit:2365` |
| Подтвердить возврат | `returns.py:404` | `server.py:2721` | `database.confirm_return:2731` + `ms_returns.create_salesreturn` в обоих (`returns.py:414`, `server.py:2729`) |
| Товар по возврату получен | `returns.py:391` | `server.py:2758` | `database.mark_return_goods_received:2720` |
| Разморозить заказ | `orders.py:1191` | `server.py:2124` | `database.unfreeze_order:1606` |
| Деактивировать / вернуть юзера | `users.py:136,166` | `server.py:2471,2473` | `database.deactivate_user:3112` / `reactivate_user:3125` |
| Цена товара | `pricing.py:184` | `server.py:2327` | `database.set_product_price:2083` |
| Курс валюты | `pricing.py:90` | `server.py:2288` | `database.set_currency_rate:1984` |

## 5.2 Разные ветки — и разный результат

### 5.2.1 Удаление черновика
- **Бот** `orders.py:971` `cb_delete_order_yes`: `update_order_status(order_id, 'rejected')`
  (`:986`). Строка заказа остаётся в БД, `order_items` остаются, платежи не проверяются.
  Аудит пишет «Удалён черновик заказа #N» (`:993`).
- **WebApp** `server.py:3756` → `database.delete_order:4396`: физический `DELETE` заказа
  **и** ручной каскад по `order_items` (`:4423`), плюс отказ, если по заказу есть платежи
  (`:4419`).
- **Разница результата:** после бота заказ существует со статусом `rejected` и попадает
  в выборки по статусам; после WebApp записи нет вовсе. Бот-путь не откажет при наличии
  платежей (но и ничего не удаляет, поэтому осиротевших платежей не создаёт).

### 5.2.2 Отмена заказа
- **Бот** `order_cancel.py:99`: `cancel_order`, затем **`ms_cancel.reverse_customerorder`**
  (`:105–110`).
- **WebApp** `server.py:2951`: только `cancel_order`. Вызова `ms_cancel` в файле нет.
- **Разница результата:** после отмены из бота `orders.ms_cancel_synced_at` проставлен и
  документ в МойСклад удалён; после отмены из WebApp документ в МС остаётся, поле пустое.

### 5.2.3 Отправка заявки на отгрузку
- **Бот** `orders.py:912–925`: `create_shipment_request` + `update_order_status('pending')`,
  без записи типа оплаты и без идемпотентности.
- **WebApp** `server.py:3134–3162`: дополнительно валидирует `payment_type` / `due_date`
  (`:3136–3151`), вызывает `set_order_payment` **до** создания заявки (`:3154`), и
  кэширует результат по `idempotency_key` (`:3108–3112,3190`).
- **Разница результата:** заявка, отправленная из бота, всегда идёт с тем типом оплаты,
  который был на заказе ранее (по умолчанию `paid`, `database.py:722`); указать
  «в долг» с датой из бота нельзя. Повторный тап в боте ничем не гасится.

### 5.2.4 Подтверждение / отклонение платежа
- **Бот** `payments.py:251,281`: `confirm_payment(payment_id)` / `reject_payment(payment_id)`
  — **один конкретный** платёж из кнопки уведомления.
- **WebApp** `server.py:3638,3706`: `confirm_all_pending_payments_for_order(order_id)` /
  `reject_all_pending_payments_for_order(order_id)` — **все pending платежи заказа**
  (`database.py:5489,5516`).
- **Разница результата:** при нескольких частичных платежах по одному заказу нажатие в
  WebApp закрывает их пачкой и шлёт уведомление каждому владельцу платежа
  (`server.py:3646–3659`), нажатие в боте — только по одному платежу.

### 5.2.5 Отправка платежа в кассу
- **Бот** `payments.py:187` `_finalize_payment`: одна сумма, одна валюта, `add_payment`,
  уведомление через `notify.notify_payment_sent`.
- **WebApp** `server.py:1649`: та же одиночная ветка (`:1721`) **плюс** мульти-валютный
  batch (`_send_payments_batch:1572`, до 20 строк) и DB-идемпотентность через
  `idem_claim` (`:1603,1713`).
- **Разница результата:** из WebApp можно одним действием создать до 20 платежей в
  разных валютах и получить одно сводное уведомление боссу (`_notify_batch_payments:1546`);
  из бота — только один платёж за раз, без защиты от повторной отправки.

### 5.2.6 Создание возврата
- **Бот** `returns.py:83` `_finalize_return`: `return_type` = `full` **или** `partial`,
  позиции и количества выбираются в FSM (`:250,270,305`).
- **WebApp** `server.py:2819–2836`: жёстко `"full"`, `ret_items` собирается из всех
  позиций заказа целиком.
- **Разница результата:** частичный возврат оформляется только через бота.
  Флаг `force` (обход дедлайна) считается одинаково: `_has_role(admin,boss,warehouse_keeper)`
  в боте (`returns.py:86`) и `get_role(...) in (...)` в WebApp (`server.py:2814`).

### 5.2.7 Сдача наличных
- **Бот** `deposits.py:97`: `create_cash_deposit(user_id, amount)`. Валидация суммы —
  локальный `float(...) > 0` (`:91–95`).
- **WebApp** `server.py:2637`: тот же вызов, но перед ним `math.isfinite` + потолок 10 млн
  (`:2613–2620`) и DB-идемпотентность `idem_claim` (`:2629`).
- **Разница результата:** повторный POST из WebApp вернёт тот же `deposit_id`; повторная
  команда `/deposit` в боте создаст вторую сдачу. Верхняя граница суммы в боте
  проверяется только внутри `_validate_amount` (`database.py:1912`).

### 5.2.8 Кредитный лимит
- **Бот** `credit.py:96` `process_limit_amount`: `float(raw)`, отказ только при `< 0`
  (`:99–102`); роль после входа в FSM **повторно не проверяется**; `set_credit_limit`
  вызывается для любого `agent_id` из списка.
- **WebApp** `server.py:2152`: cap длины `agent_id`/`agent_name` (`:2166–2167`), проверка
  `agent_has_order` (`:2173`), `math.isfinite` + диапазон `0..10M` (`:2182–2187`).
- **Разница результата:** через бота проходит нечисловая граница `inf` (условие `amount < 0`
  для `inf` ложно) и лимит контрагенту, по которому нет заказов; через WebApp — нет.

### 5.2.9 Отметка оплаты по долгу
- **Бот** `debts.py:269`: `mark_order_paid(..., amount=None)` — всегда закрывает **весь
  остаток** (комментарий `:266–268`).
- **WebApp** `server.py:3498`: `amount` берётся из запроса (`:3493–3496`), т.е. возможна
  частичная сумма.
- Функция общая (`database.mark_order_paid:5329`), различается только аргумент.

### 5.2.10 Добавление позиции в заказ
- Обе стороны зовут `add_order_item` и обе проверяют минимальную цену:
  бот — `product["sale_min"]` из пикера (`orders.py:686–696`), WebApp — `get_product_price`
  по `ms_id` (`server.py:3007–3017`).
- **Разница:** WebApp дополнительно валидирует количество через `_validate_quantity`
  (`server.py:2992`, `isfinite` + `0 < qty < 1e6`) и фиксирует валюту заказа из payload
  (`:3023–3026`); бот парсит «кол-во цена» регуляркой `_parse_qty_price` (`orders.py:61`)
  и валюту не трогает. WebApp также требует `status == 'draft'` через `_require_draft_order`
  (`:2990`), бот такой проверки в этом обработчике не делает — он полагается на то, что в
  FSM попадают только из клавиатуры черновика.

### 5.2.11 Аналитика и долги — полностью раздельные реализации
- Аналитика: бот — `handlers/analytics.py:264` (`show_company_analytics`) и `:528`
  (`show_manager_analytics`); WebApp — `server.py:971` (`_company_analytics_payload`) и
  `:1192` (`_personal_analytics`). Общего кода нет.
- Долги: бот — `handlers/debts.py:116` `_render_debts`; WebApp — `server.py:3252`
  `api_debts`. Формулы остатка разные: WebApp вычитает подтверждённые платежи, сдачи и
  возвраты (`server.py:3307–3313`); бот в карточке долга использует
  `get_order_payment_summary` (`debts.py:70` `_format_debt_card`), которая сдачи не
  учитывает (`database.py:3495–3550`).

## 5.3 Действия без пары

| Только в боте | Только в WebApp |
|---|---|
| `/addrole` (смена роли), `/users`, `/log`, `/audit`, `/syncms`, `/msstaff`, `/refresh`, `/snapshot`, `/payreport`, `/sync_payments` + кнопки `ms_sync_retry`/`ms_sync_refresh`, `/shipments`, `/frozen`, частичный возврат | «Требует внимания» на Главной (`app.js:565`), операционная сводка (`renderOpsSummary`), экспорт аналитики (`/api/analytics/export`), карточка контрагента (`/api/clients/detail`), мульти-валютный batch платежей |

---

# 6. МЁРТВЫЙ ИНТЕРФЕЙС

## 6.1 Команда, объявленная без обработчика

| Что | Где объявлено | Почему считаю мёртвым |
|---|---|---|
| `/reports` («📈 Отчёты») | `handlers/start.py:135`, входит в `_COMMANDS_BOSS` и `_COMMANDS_ADMIN` | Ни одного `@router.message(Command("reports"))` в `handlers/` нет (проверено полным грепом). Команда видна в автокомплите Telegram у boss/admin, но нажатие ничего не вызывает. `config.py:123–125` фиксирует, что расписанные отчёты и `tasks/run_report` были удалены |

## 6.2 Эндпоинты без вызовов из фронта

15 штук — полный список в [§3.1](#31-эндпоинты-без-входящих-вызовов-из-webappstatic).
Основания по группам:

- **`/api/permissions/list`, `/api/permissions/user`, `/api/permissions/grant`** —
  ни одной ссылки в `webapp/static/`. Дополнительно: функция, которая должна была бы
  читать выданные права, `services/roles.has_permission:247`, не вызывается ни из одной
  точки авторизации (все решения принимают `_authorize(allowed_roles=…)`,
  `is_boss`/`_has_role`). То есть у этих трёх эндпоинтов нет ни UI, ни потребителя
  результата.
- **`/api/users/deactivate`** — нет ссылки во фронте; та же операция выполняется
  командами `/deactivate` и `/reactivate` в боте.
- **`/api/orders/unfreeze`** — нет ссылки; операция доступна кнопкой `unfreeze:` в боте
  (`orders.py:1180`).
- **`/api/requests/return_to_draft`** — нет ссылки; в `renderPendingRequests`
  (`app.js:2067`) кнопки «на доработку» нет, `handleRequest` (`app.js:2152`) умеет только
  approve/reject. Операция доступна кнопкой `req_draft:` в боте.
- **`/api/returns/goods_received`** — нет ссылки; в карточке возврата на вкладке
  «Подтверждения» отрисована только кнопка «Подтвердить возврат» (`app.js:2686`).
  Операция доступна кнопкой `ret_got:` в боте.
- **`/api/currency/rates`, `/api/currency/rates/set`** — нет ссылок; курсы задаются
  командой `/rates` в боте. При этом `/api/currency/rates` — единственный эндпоинт,
  открытый всем пяти ролям (`server.py:2278`).
- **`/api/products/prices`** — нет ссылок; список цен фронту не нужен, потому что
  `sale_price`/`cost_price` подмешиваются прямо в ответ `/api/stock`
  (`server.py:895–920`).
- **`/api/credit/overview`** — нет ссылок; вкладка «Клиенты» использует
  `/api/clients/overview` (`app.js:2990`), которая возвращает и долг, и лимит.
- **`/api/payments/history`, `/api/payments/unlinked`, `/api/payments/link`** — нет
  ссылок. `/api/payments/link` — единственный способ ретроспективно привязать платёж к
  заказу (`database.link_payment_to_order:4482`), и вызвать его неоткуда: в боте такой
  команды тоже нет.
- **`/api/metrics`** — нет ссылок; предназначен для ручной диагностики.

## 6.3 Экраны и ветки без входящих ссылок

| Что | Где | Почему |
|---|---|---|
| `showScreen('payments')` | `app.js:281` | Ключ обрабатывается, но ни одна кнопка `data-screen`/`data-go`/`data-att` его не выдаёт — в `index.html:82–99` только `home/orders/finance/analytics`. Комментарий в коде помечает его как legacy |
| `showScreen('debts')` | `app.js:277` | То же; переходы на долги идут через `data-att="finance:debts"` (`app.js:576`), а не через этот ключ |
| `showScreen('stock')` | `app.js:264` | То же; каталог открывается сегментом `data-sub="stock"` внутри экрана «Заказы» (`app.js:317`) |
| ветка `financeTab === 'my'` / `'overview'` / `'cashbox'` / `'payments'` | `app.js:2547–2549`, `2622–2624` | Ключи только мигрируются в актуальные; `financeTabs` (`helpers.js:148`) их больше не выдаёт |

## 6.4 Элементы UI, ссылающиеся на несуществующие сущности

| Что | Где | Почему |
|---|---|---|
| Роль `employee` в `ROLE_NAMES` | `handlers/start.py:38`, `handlers/users.py:24` | `VALID_ROLES` (`database.py:2963`) = `admin, boss, bookkeeper, warehouse_keeper, manager, guest`. Значения `employee` в БД появиться не может — `set_role` отвергает его (`database.py:3071`) |
| Подсказка `/addrole [ID] [admin/boss/manager/employee]` | `handlers/users.py:117` | Предлагает несуществующую роль `employee`; при вводе получаем отказ `handlers/users.py:58` |
| `ROLE_NAMES` без ключа `guest` | `handlers/start.py:32–39` | Фолбэк `:194` подставляет «👤 Сотрудник» — но до этой строки гость не доходит: ветка `role == "guest"` выходит раньше (`:264`). В `handlers/users.py:25` ключ `guest` есть |

---

# 7. РОЛИ

## 7.1 Матрица «действие × роль»

`✔` — доступно, `—` — недоступно. В скобках — место в коде, принимающее решение.
Столбцы `bookkeeper` и `warehouse_keeper` вынесены в отдельную таблицу 7.2.

| Действие | admin | boss | manager | guest | Где решается |
|---|---|---|---|---|---|
| Войти в WebApp | ✔ | ✔ | ✔ | — | `_authorize` `server.py:151–154`; для гостя Menu Button не ставится `start.py:245` |
| Смотреть каталог/остатки | ✔ | ✔ | ✔ | — | бот: `can_view_stock` `roles.py:117`; web: `allowed_roles` `server.py:879` |
| Смотреть отгрузки | ✔ | ✔ | ✔ | — | `can_view_stock` `roles.py:117` (`shipments.py:33`) |
| Аналитика продаж | ✔ | ✔ | ✔ | — | бот: `can_view_analytics` `roles.py:121`; web: `server.py:948` |
| Касса/дебиторка, «Деньги» | ✔ | ✔ | — | — | бот: `is_boss` `analytics.py:191`; web: `server.py:1775`, `1364`, фронт `app.js:2219` |
| Операционная сводка | ✔ | ✔ | — | — | `server.py:858` |
| Экспорт аналитики | ✔ | ✔ | — | — | `server.py:1087` |
| Глобальный поиск | ✔ | ✔ | ✔ | — | бот: `role == "guest"` `start.py:333`; web: `server.py:605` |
| Создать заказ / позиции / клиент | ✔ | ✔ | ✔ | — | бот: `can_create_orders` `roles.py:141`; web: `server.py:2866,2978,3067` |
| Отправить заявку | ✔ | ✔ | ✔ | — | бот: `can_create_orders` `orders.py:879`; web: `server.py:3091` |
| Удалить черновик | ✔¹ | ✔¹ | ✔¹ | — | бот: только владелец `orders.py:978`; web: `allowed_roles` `server.py:3746` + владелец в `delete_order` `database.py:4414` |
| Одобрить / отклонить заявку | ✔ | ✔ | — | — | бот: `is_boss` `orders.py:1078,1093`; web: `server.py:1997,2043` |
| Вернуть заявку на доработку | ✔ | ✔ | — | — | бот: `is_boss` `orders.py:1120` + повторно `:1137`; web: `server.py:2071` |
| Отгрузить заказ | ✔ | ✔ | — | — | бот: `can_confirm_shipment` `roles.py:162`; web: `server.py:2890` |
| Отменить заказ | ✔ | ✔ | — | — | бот: `_can_cancel` `order_cancel.py:36` + повторно `:86`; web: `server.py:2933` |
| Разморозить заказ | ✔ | — | — | — | бот: `is_admin` `orders.py:1187`; web: `server.py:2109` |
| Отправить платёж в кассу | ✔ | — | ✔ | — | бот: `_can_send_payment` `payments.py:21`; web: `server.py:1662` |
| Подтвердить / отклонить платёж | ✔ | ✔ | — | — | бот: `is_admin` → `can_manage_payments` `payments.py:66`, `roles.py:125`; web: `server.py:3608,3676` |
| Отметить оплату по долгу | ✔ | ✔ | ✔² | — | бот: владелец или `is_boss` `debts.py:236–240`; web: владелец или boss `server.py:3478–3481` |
| Смотреть долги | ✔ | ✔ | ✔² | — | бот: `can_create_orders` `debts.py:120`, скоуп `is_boss` `:123`; web: `server.py:3271`, скоуп `:3278` |
| Сдать наличные | ✔ | ✔ | ✔ | — | бот: `_can_deposit` `deposits.py:36`; web: `server.py:2606` |
| Подтвердить / отклонить сдачу | ✔ | ✔ | — | — | бот: `can_confirm_deposit` `roles.py:157`; web: `server.py:2517,2562` |
| Оформить возврат | ✔ | ✔ | ✔ | — | бот: `can_create_return` `roles.py:167`; web: `server.py:2776` |
| Подтвердить возврат | ✔ | ✔ | — | — | бот: `can_confirm_return` `roles.py:172`; web: `server.py:2705` |
| Отметить «товар получен» | ✔ | ✔ | — | — | бот: `is_warehouse_keeper or can_confirm_return` `returns.py:388`; web: `server.py:2746` |
| Кредитный лимит | ✔ | ✔ | — | — | бот: `can_change_credit_limit` `roles.py:177`; web: `server.py:2159` |
| Курсы валют (запись) | ✔ | ✔ | — | — | бот: `is_boss` `pricing.py:55,76,91`; web: `server.py:2299` |
| Цены товаров (запись) | ✔ | ✔ | — | — | бот: `is_boss` `pricing.py:114,157,185`; web: `server.py:2338` |
| Себестоимость видна | ✔ | ✔ | — | — | `server.py:900,918` (`cost_price` только для boss/admin) |
| Клиенты / кредит-обзор | ✔ | ✔ | — | — | `server.py:2204,2227,2140` |
| Пользователи, роли, аудит, лог | ✔ | — | — | — | `can_manage_users` `roles.py:130` (`users.py:34,85,123,155,185,208`, `audit.py:80`, `log.py:40`) |
| Деактивация пользователя | ✔ | — | — | — | бот: `can_manage_users` `users.py:123`; web: `server.py:2459` |
| Права (`/api/permissions/*`) | ✔ | — | — | — | `server.py:2385,2399,2422` |
| `/refresh`, `/snapshot` | ✔ | — | — | — | `is_admin` `start.py:389,409` |
| `/api/metrics` | ✔ | ✔ | — | — | `server.py:514` |

¹ Дополнительно требуется владение заказом и статус `draft`.
² Менеджер — только свои заказы.

**Сквозные условия для всех строк:**
- `services/roles._has_role:87` возвращает `True` для любого `user_id` из `ADMIN_IDS`
  **до** обращения к БД — env-админ проходит любую проверку через `is_*`/`can_*`.
- `database.get_role:2966` возвращает `guest`, если у пользователя стоит
  `deactivated_at` (`:2986–2988`) — деактивированный теряет все строки матрицы.
- Роль в боте читается из кэша с TTL 60 с (`roles.py:29,33`), в WebApp — тот же кэш
  плюс отдельная проверка деактивации с TTL 30 с (`roles.py:68,72`, вызов
  `server.py:147–150`).

## 7.2 bookkeeper и warehouse_keeper

| Действие | bookkeeper | warehouse_keeper | Где решается |
|---|---|---|---|
| Войти в WebApp | ✔ (через `allowed_roles=None`-эндпоинты) | ✔ | `server.py:574,1324,1829` |
| Каталог / аналитика / поиск / заказы | — | — | `allowed_roles` не включает их: `server.py:879,948,605,2866` |
| Подтвердить / отклонить сдачу | ✔ | — | `can_confirm_deposit` `roles.py:157`; `server.py:2517,2562` |
| Смотреть очередь сдач | ✔ | — | `deposits.py:158`, `server.py:2496` |
| Смотреть очередь возвратов | — | ✔ | `returns.py:373`, `server.py:2688` |
| Оформить возврат | — | ✔ | `can_create_return` `roles.py:167`, `server.py:2776` |
| Подтвердить возврат | — | — | `can_confirm_return` = admin/boss `roles.py:172`; `server.py:2705` |
| Отметить «товар получен» | — | ✔ | `returns.py:388`, `server.py:2746` |
| Отгрузить заказ | — | ✔ | `can_confirm_shipment` `roles.py:162`, `server.py:2890` |
| Непривязанные платежи | ✔ | — | `server.py:1449` |
| Курсы валют (чтение) | ✔ | ✔ | `server.py:2278` |
| Сдать наличные | — | — | бот: `_can_deposit` `deposits.py:36`; web: `server.py:2606` |

## 7.3 Расхождения между показанным UI и реальной проверкой

| # | Что показывает UI | Что проверяется на самом деле | Где |
|---|---|---|---|
| 1 | `/reports` в меню команд у boss и admin | обработчика нет вообще | `start.py:135` vs. отсутствие `Command("reports")` |
| 2 | `/pay` в меню команд у boss (`_COMMANDS_BOSS` = `_COMMANDS_MANAGER` + …) | `_can_send_payment` = `_has_role(admin, manager)` — boss получает «⛔ Отправлять платежи могут менеджеры» | `start.py:132` (наследование `:120–131`) vs. `payments.py:21,120` |
| 3 | Вкладка «Платежи и сдачи» в WebApp предлагает форму сдачи наличных кладовщику (`canDeposit = manager \|\| warehouse_keeper`) | `/api/deposits/create` разрешён только `admin, boss, manager` — кладовщик получит 403 | `app.js:2540`, `app.js:2630` vs. `server.py:2606` |
| 4 | Форма сдачи наличных **не** показывается боссу (`canDeposit` его не включает) | `/api/deposits/create` боссу разрешён | `app.js:2630` vs. `server.py:2606` |
| 5 | Подсказка `/addrole [ID] [admin/boss/manager/employee]` | `employee` не входит в `VALID_ROLES`, `set_role` вернёт `False` | `users.py:117` vs. `database.py:2963,3071` |
| 6 | Карточка возврата в боте показывает обе кнопки «Подтвердить возврат» и «Товар получен» всем получателям уведомления | `ret_ok:` требует `can_confirm_return` (admin/boss), `ret_got:` — `is_warehouse_keeper or can_confirm_return`; кладовщик, нажав «Подтвердить возврат», получит «⛔ Нет доступа» | `returns.py:120–126`, рассылка `:335` vs. `returns.py:388,399` |
| 7 | `/debts` при отсутствии прав не отвечает ничем | проверка `can_create_orders` есть, но ветка `return` — без сообщения пользователю | `debts.py:120–121` |
| 8 | В WebApp админ выдаёт/отзывает права через `/api/permissions/grant`, ответ `/api/permissions/user` показывает `granted: true, source: override` | ни одна точка авторизации не читает `user_permissions`: `has_permission` не вызывается нигде вне своего модуля | `server.py:2408–2449`, `roles.py:247` vs. `_authorize` `server.py:151–154` и `is_*`/`can_*` `roles.py:109–182` |
| 9 | `ROLE_DEFAULTS` объявляет у bookkeeper право `manage_payments` | подтверждение платежей проверяется через `can_manage_payments` = `_has_role(admin, boss)`; bookkeeper не проходит | `roles.py:216` vs. `roles.py:125`, `server.py:3608` |
| 10 | `ROLE_DEFAULTS` объявляет у warehouse_keeper право `confirm_return` | `can_confirm_return` = `_has_role(admin, boss)` | `roles.py:217` vs. `roles.py:172` |
| 11 | Кнопка «✅ Одобрить» на карточке заявки видна каждому получателю рассылки boss/admin | проверка `is_boss` в обработчике; расхождения нет, но рассылка идёт по `get_notify_recipients`, который отбирает по `role in ("admin","boss")` из БД — то есть по другому источнику, чем `_has_role` (`ADMIN_IDS` из env в рассылку не попадают) | `notifier.py:154–157` vs. `roles.py:87,93` |
| 12 | Экран «Заявки» в WebApp показывает только «Одобрить» и «Отклонить» | эндпоинт `return_to_draft` существует и разрешён admin/boss, но кнопки нет | `app.js:2152–2161` vs. `server.py:2065` |
| 13 | `/api/currency/rates` доступен всем пяти рабочим ролям | фронт этот эндпоинт не вызывает; курсы в UI не показываются нигде | `server.py:2278` vs. отсутствие ссылок в `webapp/static/` |
| 14 | В боте FSM-шаги перепроверяют роль после ввода текста в `deposits.py:225`, `order_cancel.py:86`, `orders.py:1137`, `pricing.py:91,185` | в `credit.py:96` (`LimitFlow.waiting_amount`) повторной проверки нет — роль, снятая между нажатием `lim_set:` и вводом суммы, не помешает записать лимит | `credit.py:95–113` |
