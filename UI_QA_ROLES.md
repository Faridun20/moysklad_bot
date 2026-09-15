# QA-чеклист WebApp по ролям (UI-WP-33)

**Сгенерирован из кода** — `python scripts/gen_role_matrix.py`. Источник:
`allowed_roles=(...)` в `_authorize(...)` по каждому эндпоинту
`webapp/server.py`. Переписанный руками список прав устаревает первым же PR'ом,
поэтому план пересборки требует сверять его по коду.

## Как проверять

Под каждой ролью пройти все её разделы (Сегодня → Продажи → Склад → Деньги →
Клиенты; у руководства ещё Решения и Настройки, а работа менеджера — за
выключателем «Рабочие действия» в «Меню», проверить в обоих положениях; набор
зависит от роли) и убедиться, что:

1. экран открывается и не показывает `errorBox` вместо данных;
2. чего роли не положено — не отрисовано (кнопки/секции нет, а не «нажимается
   и отвечает 403»);
3. пустые состояния объясняют, что делать, а не просто «нет данных»;
4. в плотных списках (Долги, Клиенты, Курсы) строка не мельче 44px;
5. в тёмной и светлой теме карточка не сливается с фоном страницы.

`guest` проверяется отдельно: он обязан видеть ТОЛЬКО экран «Доступ не выдан»
(`renderNoAccess`) — без нижней навигации и поиска.


## Доступ по ролям

_Всего эндпоинтов: 152._


> **Временное совмещение ролей** (`services/roles.py::ROLE_ALSO_ACTS_AS`): `manager` = + `warehouse_keeper`, `bookkeeper`. Кладовщика и бухгалтера в штате пока нет, их работу делает менеджер; списки ниже уже учитывают это. Роли `warehouse_keeper`/`bookkeeper` через `/addrole` не назначаются. `*` в таблице экранов — доступ через совмещение.


### Админ (`admin`) — 131 эндпоинтов

<details><summary>Показать список</summary>

- `/api/agents`
- `/api/analytics`
- `/api/analytics/export`
- `/api/cash/history`
- `/api/channel/draft`
- `/api/channel/history`
- `/api/channel/publish`
- `/api/channel/stale`
- `/api/clients/detail`
- `/api/clients/overview`
- `/api/clients/shipment`
- `/api/containers/arrive`
- `/api/containers/card`
- `/api/containers/check`
- `/api/containers/create`
- `/api/containers/delete`
- `/api/containers/item_add`
- `/api/containers/item_create_product`
- `/api/containers/item_delete`
- `/api/containers/item_link`
- `/api/containers/list`
- `/api/containers/supplier`
- `/api/containers/supply`
- `/api/containers/update`
- `/api/costing/container`
- `/api/costing/container/save`
- `/api/costing/product`
- `/api/costing/report`
- `/api/costing/settings`
- `/api/costing/settings/set`
- `/api/credit/overview`
- `/api/credit/set`
- `/api/currency/rates`
- `/api/currency/rates/set`
- `/api/debts`
- `/api/deposits/confirm`
- `/api/deposits/create`
- `/api/deposits/my`
- `/api/deposits/on_hand`
- `/api/deposits/pending`
- `/api/deposits/reject`
- `/api/docs/company/set`
- `/api/docs/create`
- `/api/docs/list`
- `/api/docs/print`
- `/api/docs/send`
- `/api/docs/types`
- `/api/home`
- `/api/leads/agents`
- `/api/leads/call_add`
- `/api/leads/call_delete`
- `/api/leads/call_link`
- `/api/leads/calls`
- `/api/leads/card`
- `/api/leads/create_agent`
- `/api/leads/funnel`
- `/api/leads/link`
- `/api/leads/list`
- `/api/leads/status`
- `/api/machines/arrive`
- `/api/machines/buyer`
- `/api/machines/card`
- `/api/machines/create`
- `/api/machines/deal`
- `/api/machines/deal_close`
- `/api/machines/deals_open`
- `/api/machines/delete`
- `/api/machines/hours`
- `/api/machines/list`
- `/api/machines/payment`
- `/api/machines/photo`
- `/api/machines/photo_delete`
- `/api/machines/photo_upload`
- `/api/machines/receipt`
- `/api/machines/receipt_delete`
- `/api/machines/status`
- `/api/machines/update`
- `/api/metrics`
- `/api/money/discipline`
- `/api/money/forecast`
- `/api/money/receivables`
- `/api/money/summary`
- `/api/ops-summary`
- `/api/orders/add_item`
- `/api/orders/cancel`
- `/api/orders/confirm_payment`
- `/api/orders/create`
- `/api/orders/delete_draft`
- `/api/orders/mark_paid`
- `/api/orders/payment`
- `/api/orders/payment_context`
- `/api/orders/reject_payment`
- `/api/orders/remove_item`
- `/api/orders/requests`
- `/api/orders/set_agent`
- `/api/orders/ship`
- `/api/orders/submit`
- `/api/orders/unfreeze`
- `/api/payments/link`
- `/api/payments/pending`
- `/api/payments/send`
- `/api/payments/unlinked`
- `/api/prefs/set`
- `/api/products/photo`
- `/api/products/photo_delete`
- `/api/products/photo_upload`
- `/api/products/photos`
- `/api/products/prices`
- `/api/products/prices/set`
- `/api/products/search`
- `/api/requests/approve`
- `/api/requests/reject`
- `/api/requests/return_to_draft`
- `/api/returns/confirm`
- `/api/returns/create`
- `/api/returns/goods_received`
- `/api/returns/pending`
- `/api/returns/positions`
- `/api/search`
- `/api/stock`
- `/api/today`
- `/api/users/deactivate`
- `/api/wh/counterparties`
- `/api/wh/counterparties/create`
- `/api/wh/invoices`
- `/api/wh/invoices/cancel`
- `/api/wh/invoices/create`
- `/api/wh/invoices/get`
- `/api/wh/invoices/print`
- `/api/wh/invoices/send`
- `/api/wh/stock`

</details>


### Руководитель (`boss`) — 128 эндпоинтов

<details><summary>Показать список</summary>

- `/api/agents`
- `/api/analytics`
- `/api/analytics/export`
- `/api/cash/history`
- `/api/channel/draft`
- `/api/channel/history`
- `/api/channel/publish`
- `/api/channel/stale`
- `/api/clients/detail`
- `/api/clients/overview`
- `/api/clients/shipment`
- `/api/containers/arrive`
- `/api/containers/card`
- `/api/containers/check`
- `/api/containers/create`
- `/api/containers/delete`
- `/api/containers/item_add`
- `/api/containers/item_create_product`
- `/api/containers/item_delete`
- `/api/containers/item_link`
- `/api/containers/list`
- `/api/containers/supplier`
- `/api/containers/supply`
- `/api/containers/update`
- `/api/costing/container`
- `/api/costing/container/save`
- `/api/costing/product`
- `/api/costing/report`
- `/api/costing/settings`
- `/api/costing/settings/set`
- `/api/credit/overview`
- `/api/credit/set`
- `/api/currency/rates`
- `/api/currency/rates/set`
- `/api/debts`
- `/api/deposits/confirm`
- `/api/deposits/create`
- `/api/deposits/my`
- `/api/deposits/on_hand`
- `/api/deposits/pending`
- `/api/deposits/reject`
- `/api/docs/company/set`
- `/api/docs/create`
- `/api/docs/list`
- `/api/docs/print`
- `/api/docs/send`
- `/api/docs/types`
- `/api/home`
- `/api/leads/agents`
- `/api/leads/call_add`
- `/api/leads/call_delete`
- `/api/leads/call_link`
- `/api/leads/calls`
- `/api/leads/card`
- `/api/leads/create_agent`
- `/api/leads/funnel`
- `/api/leads/link`
- `/api/leads/list`
- `/api/leads/status`
- `/api/machines/arrive`
- `/api/machines/buyer`
- `/api/machines/card`
- `/api/machines/create`
- `/api/machines/deal`
- `/api/machines/deal_close`
- `/api/machines/deals_open`
- `/api/machines/delete`
- `/api/machines/hours`
- `/api/machines/list`
- `/api/machines/payment`
- `/api/machines/photo`
- `/api/machines/photo_delete`
- `/api/machines/photo_upload`
- `/api/machines/receipt`
- `/api/machines/receipt_delete`
- `/api/machines/status`
- `/api/machines/update`
- `/api/metrics`
- `/api/money/discipline`
- `/api/money/forecast`
- `/api/money/receivables`
- `/api/money/summary`
- `/api/ops-summary`
- `/api/orders/add_item`
- `/api/orders/cancel`
- `/api/orders/confirm_payment`
- `/api/orders/create`
- `/api/orders/delete_draft`
- `/api/orders/mark_paid`
- `/api/orders/payment`
- `/api/orders/payment_context`
- `/api/orders/reject_payment`
- `/api/orders/remove_item`
- `/api/orders/requests`
- `/api/orders/set_agent`
- `/api/orders/ship`
- `/api/orders/submit`
- `/api/payments/link`
- `/api/payments/pending`
- `/api/payments/unlinked`
- `/api/prefs/set`
- `/api/products/photo`
- `/api/products/photo_delete`
- `/api/products/photo_upload`
- `/api/products/photos`
- `/api/products/prices`
- `/api/products/prices/set`
- `/api/products/search`
- `/api/requests/approve`
- `/api/requests/reject`
- `/api/requests/return_to_draft`
- `/api/returns/confirm`
- `/api/returns/create`
- `/api/returns/goods_received`
- `/api/returns/pending`
- `/api/returns/positions`
- `/api/search`
- `/api/stock`
- `/api/today`
- `/api/wh/counterparties`
- `/api/wh/counterparties/create`
- `/api/wh/invoices`
- `/api/wh/invoices/cancel`
- `/api/wh/invoices/create`
- `/api/wh/invoices/get`
- `/api/wh/invoices/print`
- `/api/wh/invoices/send`
- `/api/wh/stock`

</details>


### Менеджер (`manager`) — 79 эндпоинтов

<details><summary>Показать список</summary>

- `/api/agents`
- `/api/analytics`
- `/api/containers/arrive`
- `/api/containers/card`
- `/api/containers/check`
- `/api/containers/create`
- `/api/containers/item_add`
- `/api/containers/item_create_product`
- `/api/containers/item_delete`
- `/api/containers/item_link`
- `/api/containers/list`
- `/api/containers/supplier`
- `/api/containers/supply`
- `/api/containers/update`
- `/api/currency/rates`
- `/api/debts`
- `/api/deposits/confirm`
- `/api/deposits/create`
- `/api/deposits/my`
- `/api/deposits/on_hand`
- `/api/deposits/pending`
- `/api/deposits/reject`
- `/api/docs/create`
- `/api/docs/list`
- `/api/docs/print`
- `/api/docs/send`
- `/api/docs/types`
- `/api/home`
- `/api/leads/agents`
- `/api/leads/call_add`
- `/api/leads/call_delete`
- `/api/leads/call_link`
- `/api/leads/calls`
- `/api/leads/card`
- `/api/leads/create_agent`
- `/api/leads/link`
- `/api/leads/list`
- `/api/leads/status`
- `/api/machines/arrive`
- `/api/machines/card`
- `/api/machines/create`
- `/api/machines/hours`
- `/api/machines/list`
- `/api/machines/photo`
- `/api/machines/photo_upload`
- `/api/money/receivables`
- `/api/orders/add_item`
- `/api/orders/confirm_payment`
- `/api/orders/create`
- `/api/orders/delete_draft`
- `/api/orders/mark_paid`
- `/api/orders/payment`
- `/api/orders/payment_context`
- `/api/orders/reject_payment`
- `/api/orders/remove_item`
- `/api/orders/set_agent`
- `/api/orders/ship`
- `/api/orders/submit`
- `/api/payments/pending`
- `/api/payments/send`
- `/api/payments/unlinked`
- `/api/products/photo`
- `/api/products/photos`
- `/api/products/search`
- `/api/returns/create`
- `/api/returns/goods_received`
- `/api/returns/pending`
- `/api/returns/positions`
- `/api/search`
- `/api/stock`
- `/api/today`
- `/api/wh/counterparties`
- `/api/wh/counterparties/create`
- `/api/wh/invoices`
- `/api/wh/invoices/create`
- `/api/wh/invoices/get`
- `/api/wh/invoices/print`
- `/api/wh/invoices/send`
- `/api/wh/stock`

</details>


### Кладовщик (сейчас не назначается) (`warehouse_keeper`) — 7 эндпоинтов

<details><summary>Показать список</summary>

- `/api/currency/rates`
- `/api/orders/ship`
- `/api/returns/create`
- `/api/returns/goods_received`
- `/api/returns/pending`
- `/api/returns/positions`
- `/api/today`

</details>


### Бухгалтер (сейчас не назначается) (`bookkeeper`) — 9 эндпоинтов

<details><summary>Показать список</summary>

- `/api/currency/rates`
- `/api/deposits/confirm`
- `/api/deposits/pending`
- `/api/deposits/reject`
- `/api/orders/confirm_payment`
- `/api/orders/reject_payment`
- `/api/payments/pending`
- `/api/payments/unlinked`
- `/api/today`

</details>


### Доступно любой активной роли

- `/api/me`
- `/api/orders`
- `/api/payments/history`



### Без авторизации

- `/`
- `/api/acc/accounts`
- `/api/acc/accounts/archive`
- `/api/acc/accounts/save`
- `/api/acc/balances`
- `/api/acc/close_day`
- `/api/acc/doc`
- `/api/acc/expense`
- `/api/acc/journal`
- `/api/acc/rates`
- `/api/acc/receipt`
- `/api/acc/receipt_targets`
- `/api/acc/settings`
- `/api/acc/state`
- `/api/acc/transfer`
- `/api/acc/void`
- `/healthz`
- `/tg/{secret}`



## Экраны против прав

| Экран | Кто открывает | Ключевой эндпоинт |
|---|---|---|
| Главная | все активные | `/api/home` |
| Заказы (список) | admin, boss, manager | `/api/orders` |
| Решения (заявки, оплаты, сдачи, возвраты) | admin, boss | `/api/orders/requests` |
| Меню → «Рабочие действия» (вид, не права) | admin, boss | `/api/prefs/set` |
| Редактор заказа | admin, boss, manager | `/api/orders/create` |
| Каталог/Склад | admin, boss, manager, warehouse_keeper | `/api/stock` |
| Деньги → Подтвердить | admin, boss, bookkeeper, manager* | `/api/deposits/pending` |
| Финансы → Долги | admin, boss, manager | `/api/debts` |
| Финансы → Клиенты | admin, boss | `/api/clients/overview` |
| Курсы валют | admin, boss (правка) | `/api/currency/rates` |
| Аналитика | admin, boss, manager | `/api/analytics` |
| Деньги (лента) | admin, boss | `/api/money/summary` |
| Операционная сводка | admin, boss | `/api/ops-summary` |
| Возвраты (приёмка) | admin, boss, warehouse_keeper, manager* | `/api/returns/pending` |
| Заказы → «Отгрузить» (руководство — с «Рабочими действиями») | admin, boss, warehouse_keeper, manager* | `/api/orders/ship` |
| Заказы → Техника | admin, boss, manager | `/api/machines/list` |
| Техника → карточка | admin, boss, manager | `/api/machines/card` |
| Техника → сделки | admin, boss | `/api/machines/deal` |

Таблица экранов ручная (какой экран какой эндпоинт зовёт — это знание фронта), списки выше машинные. При расхождении верить спискам.
