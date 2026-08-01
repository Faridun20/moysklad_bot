# QA-чеклист WebApp по ролям (UI-WP-33)

**Сгенерирован из кода** — `python scripts/gen_role_matrix.py`. Источник:
`allowed_roles=(...)` в `_authorize(...)` по каждому эндпоинту
`webapp/server.py`. Переписанный руками список прав устаревает первым же PR'ом,
поэтому план пересборки требует сверять его по коду.

## Как проверять

Под каждой ролью пройти S2–S5 (Главная → Заказы/Каталог → Финансы → Аналитика)
и убедиться, что:

1. экран открывается и не показывает `errorBox` вместо данных;
2. чего роли не положено — не отрисовано (кнопки/секции нет, а не «нажимается
   и отвечает 403»);
3. пустые состояния объясняют, что делать, а не просто «нет данных»;
4. в плотных списках (Долги, Клиенты, Курсы) строка не мельче 44px;
5. в тёмной и светлой теме карточка не сливается с фоном страницы.

`guest` проверяется отдельно: он обязан видеть ТОЛЬКО экран «Доступ не выдан»
(`renderNoAccess`) — без нижней навигации и поиска.


## Доступ по ролям

_Всего эндпоинтов: 89._


### Админ (`admin`) — 82 эндпоинтов

<details><summary>Показать список</summary>

- `/api/agents`
- `/api/analytics`
- `/api/analytics/export`
- `/api/cash/history`
- `/api/clients/detail`
- `/api/clients/overview`
- `/api/clients/shipment`
- `/api/containers/arrive`
- `/api/containers/card`
- `/api/containers/check`
- `/api/containers/create`
- `/api/containers/delete`
- `/api/containers/item_add`
- `/api/containers/item_delete`
- `/api/containers/list`
- `/api/containers/supplier`
- `/api/containers/supply`
- `/api/containers/update`
- `/api/credit/overview`
- `/api/credit/set`
- `/api/currency/rates`
- `/api/currency/rates/set`
- `/api/debts`
- `/api/deposits/confirm`
- `/api/deposits/create`
- `/api/deposits/my`
- `/api/deposits/pending`
- `/api/deposits/reject`
- `/api/home`
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
- `/api/products/prices`
- `/api/products/prices/set`
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
- `/api/users/deactivate`

</details>


### Руководитель (`boss`) — 79 эндпоинтов

<details><summary>Показать список</summary>

- `/api/agents`
- `/api/analytics`
- `/api/analytics/export`
- `/api/cash/history`
- `/api/clients/detail`
- `/api/clients/overview`
- `/api/clients/shipment`
- `/api/containers/arrive`
- `/api/containers/card`
- `/api/containers/check`
- `/api/containers/create`
- `/api/containers/delete`
- `/api/containers/item_add`
- `/api/containers/item_delete`
- `/api/containers/list`
- `/api/containers/supplier`
- `/api/containers/supply`
- `/api/containers/update`
- `/api/credit/overview`
- `/api/credit/set`
- `/api/currency/rates`
- `/api/currency/rates/set`
- `/api/debts`
- `/api/deposits/confirm`
- `/api/deposits/create`
- `/api/deposits/my`
- `/api/deposits/pending`
- `/api/deposits/reject`
- `/api/home`
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
- `/api/orders/reject_payment`
- `/api/orders/remove_item`
- `/api/orders/requests`
- `/api/orders/set_agent`
- `/api/orders/ship`
- `/api/orders/submit`
- `/api/payments/link`
- `/api/payments/pending`
- `/api/payments/unlinked`
- `/api/products/prices`
- `/api/products/prices/set`
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

</details>


### Менеджер (`manager`) — 36 эндпоинтов

<details><summary>Показать список</summary>

- `/api/agents`
- `/api/analytics`
- `/api/containers/arrive`
- `/api/containers/card`
- `/api/containers/check`
- `/api/containers/create`
- `/api/containers/item_add`
- `/api/containers/item_delete`
- `/api/containers/list`
- `/api/containers/supplier`
- `/api/containers/supply`
- `/api/containers/update`
- `/api/currency/rates`
- `/api/debts`
- `/api/deposits/create`
- `/api/deposits/my`
- `/api/home`
- `/api/machines/card`
- `/api/machines/create`
- `/api/machines/hours`
- `/api/machines/list`
- `/api/machines/photo`
- `/api/machines/photo_upload`
- `/api/money/receivables`
- `/api/orders/add_item`
- `/api/orders/create`
- `/api/orders/delete_draft`
- `/api/orders/mark_paid`
- `/api/orders/remove_item`
- `/api/orders/set_agent`
- `/api/orders/submit`
- `/api/payments/send`
- `/api/returns/create`
- `/api/returns/positions`
- `/api/search`
- `/api/stock`

</details>


### Кладовщик (`warehouse_keeper`) — 6 эндпоинтов

<details><summary>Показать список</summary>

- `/api/currency/rates`
- `/api/orders/ship`
- `/api/returns/create`
- `/api/returns/goods_received`
- `/api/returns/pending`
- `/api/returns/positions`

</details>


### Бухгалтер (`bookkeeper`) — 5 эндпоинтов

<details><summary>Показать список</summary>

- `/api/currency/rates`
- `/api/deposits/confirm`
- `/api/deposits/pending`
- `/api/deposits/reject`
- `/api/payments/unlinked`

</details>


### Доступно любой активной роли

- `/api/me`
- `/api/orders`
- `/api/payments/history`



### Без авторизации

- `/`
- `/api/ms-webhook/{secret}`
- `/healthz`
- `/tg/{secret}`



## Экраны против прав

| Экран | Кто открывает | Ключевой эндпоинт |
|---|---|---|
| Главная | все активные | `/api/home` |
| Заказы (список) | admin, boss, manager | `/api/orders` |
| Заявки на апрув | admin, boss | `/api/orders/requests` |
| Редактор заказа | admin, boss, manager | `/api/orders/create` |
| Каталог/Склад | admin, boss, manager, warehouse_keeper | `/api/stock` |
| Финансы → Касса | admin, boss, bookkeeper | `/api/deposits/pending` |
| Финансы → Долги | admin, boss, manager | `/api/debts` |
| Финансы → Клиенты | admin, boss | `/api/clients/overview` |
| Курсы валют | admin, boss (правка) | `/api/currency/rates` |
| Аналитика | admin, boss, manager | `/api/analytics` |
| Деньги (лента) | admin, boss | `/api/money/summary` |
| Операционная сводка | admin, boss | `/api/ops-summary` |
| Возвраты (приёмка) | admin, boss, warehouse_keeper | `/api/returns/pending` |
| Заказы → Техника | admin, boss, manager | `/api/machines/list` |
| Техника → карточка | admin, boss, manager | `/api/machines/card` |
| Техника → сделки | admin, boss | `/api/machines/deal` |

Таблица экранов ручная (какой экран какой эндпоинт зовёт — это знание фронта), списки выше машинные. При расхождении верить спискам.
