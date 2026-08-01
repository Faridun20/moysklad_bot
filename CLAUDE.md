# CLAUDE.md

## Commands

```bash
pip install -r requirements.txt -r requirements-dev.txt   # dev-зависимости запинены

pytest tests/                              # тесты (SQLite в /tmp, isolated_db fixture; env-дефолты в conftest)
ruff check .                               # полный набор из pyproject.toml (E9,F,B,ASYNC,UP,SIM)
ruff check --select=E9,F63,F7,F82 .        # строгий минимум-гейт (как блокирующий шаг CI)
mypy                                       # точечно по order_workflow/database/moysklad/server — гейт, 0 ошибок
pre-commit install && pre-commit install --hook-type pre-push   # локальная первая линия (опц.)

# Фронт-тесты WebApp (Vitest, webapp/static/__tests__) — гоняет CI (npm test).
# Локально: на машине нет Node/пакетных менеджеров → ставим portable Node в .tools/
powershell -ExecutionPolicy Bypass -File scripts/setup-node.ps1   # 1× : portable Node LTS в .tools/node (gitignore)
powershell -ExecutionPolicy Bypass -File scripts/test-js.ps1      # npm install (1×) + vitest run; scripts/test-js.cmd — из cmd

python bot.py                              # локально: без Postgres → SQLite, без Redis → MemoryStorage
python -m tasks.migrate                    # schema + data миграции, ДО старта сервисов на проде

# Cron CLIs (Railway Cron Jobs)
python -m tasks.run_debts_notify
python -m tasks.run_ms_sync_retry
python -m tasks.run_ops_monitor          # дневной ПИНГ (1×/день): короткое «N событий — откройте WebApp» + web_app-кнопка. Сами данные — в WebApp (/api/ops-summary + блок «Требует внимания» на главной). Отчёты продаж/склада убраны из бота — смотрят в Аналитике WebApp.
python -m tasks.run_maintenance          # janitor: чистка дедупа/аудита/soft-deleted (ночью)
python -m tasks.run_ms_reconcile         # страховка: approved-заказы с ms_customerorder_id, 404 в МС → отмена локально (ежечасно)
python -m tasks.run_backup               # дамп БД → gzip → приватный TG-канал (ночью)
python -m tasks.run_machines_archive     # техника: проданное >90 дней назад → archived (ночью, T4.3)
python -m tasks.run_money_report         # «Где деньги» руководству Rich Message'ом (понедельник)
```

**Сборка/деплой:** Railway **Railpack** (не Nixpacks) — `railpack.json`
(`deploy.aptPackages: [postgresql-client]`), версия Python — `runtime.txt`.
Attestations mise отключены в репозитории (`mise.toml`), а НЕ через Shared
Variable: новые сервисы (cron'ы) переменную не наследуют и валили билд.
Пошаговый выкат с нуля — `DEPLOY.md`.

Конфиг тулчейна — в `pyproject.toml` (`[tool.ruff]`, `[tool.pytest.ini_options]`,
`[tool.mypy]`, `[tool.coverage]`). Версии dev-тулов запинены в `requirements-dev.txt`.

## Architecture

Детали в `ARCHITECTURE.md`. Главное: `BOT_MODE` env переключает процесс между `all` / `bot` / `webapp`. На Railway prod два сервиса (`moysklad_bot` + `Webapp`) с разными `BOT_MODE`, общий Postgres + Redis через Project Shared Variables.

**Разделение бот ↔ WebApp (T3.3).** Бот = уведомления + решения по ним + то,
чего в WebApp нет. Работа (заказы, каталог, финансы, аналитика, сдачи,
возвраты, лимиты, курсы, цены) — только в WebApp; дублирующие экраны из бота
вырезаны вместе с `handlers/{analytics,stock,credit,pricing,debts}.py`.
Не добавляй в бота экран, который уже есть в WebApp: снятые команды перечислены
в `handlers.start._RETIRED_COMMANDS` и отвечают подсказкой, где искать
операцию. В боте остаются: кнопки-решения на push-карточках (`req_*`, `pay_*`,
`dep_*`, `ret_*`, `unfreeze:`), ops/admin-команды без API-аналога (`/audit`,
`/log`, `/users`, `/addrole`, `/deactivate`, `/reactivate`, `/syncms`,
`/msstaff`, `/sync_payments`, `/frozen`, `/refresh`, `/snapshot`),
`/find`, `/ship`, `/cancel`, `/shipments`, `/pay`, а из техники — только
просмотр (`/machines`, карточка, `/machine_deals`) и `/hours <id> <часы>`
(показание снимают с площадки, где открыть WebApp дольше, чем набрать два
числа).
Новую callback-кнопку рисуешь — проверь, что на неё есть хендлер:
`tests/test_bot_trimmed.py::test_no_dangling_callback_buttons_in_bot_code`
сканирует все `callback_data` в `handlers/`, `tasks/`, `utils/`.
Кнопка входа в WebApp — `handlers._ui.webapp_keyboard()` (только https-URL,
иначе Bot API отвергнет сообщение целиком).

## Conventions (нетривиальные)

**DB:**
- `init_db()` делает ТОЛЬКО `CREATE TABLE IF NOT EXISTS`. `ALTER TABLE` и backfill → в `run_migrations()` / `run_backfills()`, вызываются из `tasks/migrate.py`. **Не добавляй ALTER в init_db.**
- Webapp endpoint'ы: `services.async_db as adb` (`await adb.get_user(uid)`) — обёртка через `asyncio.to_thread`. В bot handlers — то же или явный `asyncio.to_thread`.
- Роль читай через `services.roles.cached_role(user_id)` (TTL 60s) и предикаты `is_boss / can_create_orders / ...`. НЕ через `services.database.get_role` напрямую — обойдёшь кэш. **Деактивация:** `get_role` отдаёт `guest` если `user_roles.deactivated_at` стоит → деактивированный теряет ВСЕ права. `deactivate_user/reactivate_user` (адмін, `/deactivate`/`/reactivate` + webapp `/api/users/deactivate`).
- Race-чувствительные операции (`mark_order_paid`, `confirm_payment`, `confirm_cash_deposit`) используют `SELECT ... FOR UPDATE` / advisory-lock — сохраняй паттерн.

**Native async DB (asyncpg, задача #21):** денежное ядро (order/payment reads+writes, кредит, сдачи, возвраты) — на `services/adb_core.py` (asyncpg на проде / aiosqlite в тестах; `$N`-плейсхолдеры; `fetch/fetchrow/fetchval/execute`; `transaction()` CM с commit/rollback). Остаток (`get_setting/get_role/add_audit_log`, notifier, snapshot-reads) — sync, мостится через `async_db.__getattr__` (`asyncio.to_thread`; coroutine-функции пропускаются как есть). `_to_thread(db.X, ...)`-форма НЕ ловится grep'ом `db.X(` — аудитируй её при флипе функции в async. `snapshot.refresh_*` теперь native async внутри `adb_core.transaction()` (атомарный DELETE+INSERT); read-функции snapshot остаются sync (зовутся через to_thread).
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
- Кастомные атрибуты привязаны к сущности (`demand` vs `customerorder`) — нельзя переиспользовать meta, иначе HTTP 400. На `salesreturn` атрибуты demand НЕ ставим (`services/ms_returns.py` их не шлёт).
- Подтверждение возврата создаёт «Возврат покупателя» (`entity/salesreturn`) — `services.ms_returns.create_salesreturn` (best-effort, gated через `ms_demand.is_ready()`, идемпотентно по `moysklad_return_id`, линкуется с исходной отгрузкой). Боевая проверка — `python -m tasks.verify_ms_returns` (read-only) → `--return-id N --create`.
- Любой paginated MS endpoint (`limit=100`-max) — крути offset-loop. Real пример: `services/moysklad.get_shipment_positions` (демонд может иметь >100 line items в B2B-заказе, иначе хвост молча теряется в `top_products` аналитики). Pattern — `services/snapshot._fetch_all`.
- Концурентность к МС ограничивай через `_get_positions_semaphore()`-style helper (loop-keyed lazy semaphore), а НЕ module-level `asyncio.Semaphore()` — последний биндится к первому loop'у при contention и валит cross-loop тесты с `RuntimeError: bound to a different event loop`. Предел общий — `_MS_PARALLEL_LIMIT` (4), не заводи локальные цифры: лимит МС — 5 параллельных запросов на пользователя, а поверх одного аккаунта работают бот, webapp и cron'ы.
- **Бюджет запросов (волна 7).** Токен пользовательский: сейчас ≈7,3 запроса/с, с 01.09.2026 ≈5,0, с 01.12.2026 ≈3,7. Больше 200 отказов в минуту час подряд → МойСклад отключает аккаунту API до обращения в поддержку. Поэтому: 429 разбирается по коду (1049 — темп, ждём по `X-Lognex-Retry-After`; 1073 — параллельность, ждать бесполезно), `X-RateLimit-Remaining` на успешных ответах даёт упреждающую паузу, а брейкер открывается по ДОЛЕ 429 в окне (режим «каждый третий — 429» подряд-счётчик не ловит).
- Массовые проверки документов — батчами: повторное `=` по одному полю в фильтре МС значит ИЛИ, поэтому `filter=id=a;id=b…&limit=100` закрывает сотню документов одним запросом (`tasks/run_ms_reconcile._alive_ids`). Отсутствие id в ответе = удалён, но «не смогли спросить» ≠ «удалено»: при любой неуверенности (сеть, урезанная пагинация, пустой ответ на большой батч) функция возвращает None и прогон пропускается.
- Остатки: горячий путь — дельта `report/stock/all/current?changedSince=…&stockType=…` (`snapshot.refresh_stock_delta`), полный `report/stock/all` стоит 5 единиц бюджета за страницу и берётся раз в сутки. `changedSince` — в МСК (не в локальном кадре!), окно с нахлёстом, глубже 24 часов не работает.
- **Удаление заказа в МС → отмена в боте:** вебхук `customerorder.DELETE` (`ms_sync_handler.apply_ms_customerorder_delete`, общий с cron-реконсиляцией): `approved`-заказ отменяется локально (`cancel_order` + `set_order_ms_cancel_synced`, чтобы reverse в МС не пытался удалить уже-удалённое); shipped/paid — статус не трогаем (деньги/остатки двигались), только снимаем ссылку + предупреждаем. Раньше хендлер только снимал `ms_customerorder_id` → заказ висел `approved` (баг). `tasks/run_ms_reconcile` — страховка от пропущенных вебхуков.

**Кредит-лимиты (энфорс):** при одобрении credit-заявки `approve_shipment_request(..., override)` считает `check_credit_limit`; при превышении возвращает `needs_override` и НЕ одобряет → босс жмёт «Одобрить с превышением» (`req_ovr:` / webapp `override=true`), это ставит `orders.credit_limit_override` + audit. `get_agent_current_debt` считает долг батчем (items/payments/returns), без N+1.

**Техника (волна 4):** `machines/machine_hours/machine_photos/machine_deals` —
единственная часть схемы с FOREIGN KEY (связи строго иерархические). На SQLite
FK не энфорсятся, поэтому `services.machines.delete_machine` чистит детей явно.
Себестоимость (`cost_cents`) режет `visible_machine` в СЕРВИСЕ, не на фронте —
иначе любой новый вызов вернёт её обратно. Моточасы: история в `machine_hours`,
последнее значение дублируется в `machines.hours`; показание меньше предыдущего
отвергается (опечатка), откат — только boss через `force=True`. Фото храним
`tg_file_id` + обязательный `file_unique_id` (переживает смену сервера Bot API,
tg_file_id — нет). VIN нормализуется (upper, без пробелов/дефисов) перед
записью, длина НЕ фиксируется 17 символами — у корейской техники серийники
другие. **Решение (волна 7): остаёмся на облачном Bot API**, локальный сервер
не поднимаем — `file_id` привязан к паре «бот + сервер», переезд обнулил бы все
`tg_file_id`; фиксируем сейчас, пока таблица пустая.

**Техника в WebApp (раздел «Заказы → Техника»).** Формы, фотографии и сделки —
в WebApp (`/api/machines/*`), в боте остались просмотр и `/hours`. Что помнить:
- **Граф переходов статуса — один**, `machines.NEXT_STATUSES` / `next_status_options`
  (подпись зависит от ПАРЫ статусов: «на склад» и «снять бронь» ведут в один
  `in_stock`). Проверяется он **в ручке**, а не в `set_status`: внутренние
  вызовы (`create_deal`, `close_deal`) двигают статус в обход ручного графа
  законно. Продажи/рассрочки в графе нет — им нужны цена и покупатель.
- **Роль режется в слое чтения**, как себестоимость: `visible_machine`
  (`cost_cents`) и `visible_deal` (`buyer_passport`). На запись — тоже: что
  менеджер не видит, того он и не задаёт (иначе поле возвращается через форму).
- **`tg_file_id` наружу не выходит.** Файловый URL Telegram содержит токен
  бота, поэтому фото проксируются `/api/machines/photo`; `photo_id` ищется
  среди фото заявленной машины (гейт от подстановки чужого id), кэш — по
  `file_unique_id`. Текст ошибки Bot API логируй через `utils.helpers.redact_token`.
- **Загрузка — base64 в JSON**, не multipart: `python-multipart` в зависимостях
  нет, `UploadFile`/`Form` без него роняют приложение на старте. Тип проверяем
  по сигнатуре байтов, не по mime из data-URL. Канал-хранилище —
  `MACHINE_PHOTOS_TG_CHAT_ID`; без него загрузка выключена (`can_upload_photo`
  в ответе карточки), фото по-прежнему шлют боту.
- Ручки пиши **в `webapp/server.py`**: `scripts/gen_role_matrix.py` парсит
  `allowed_roles` только оттуда (константы вроде `_MACHINE_ROLES` он резолвит).
- **VIN правится отдельной функцией** `change_vin`, а не полем в whitelist
  `update_machine_fields`: у него нормализация и проверка уникальности. Ручка
  `/api/machines/update` вызывает её ПЕРВОЙ — иначе при занятом серийнике
  карточка осталась бы изменённой наполовину. Удаление (`/api/machines/delete`)
  отказывает машине со сделкой: продажа — денежный факт, такие уводят в архив.
- **Марки, модели и контейнера в форме машины нет**: первые два входят в
  название, контейнер — отдельная сущность. У старых карточек поля остались,
  карточка показывает их, только если заполнены.

**Рассрочка: план и факт — разные вещи.** График (`machine_deal_payments`) это
план, поступления (`machine_payment_receipts`) — факт. Клиент платит не «платёж
№3», а деньги: в один месяц больше, в другой меньше, поэтому поступления копятся
общей суммой и гасят график по порядку (`machines.allocate_receipts`).
Переплата уходит в следующие месяцы, недоплата оставляет платёж покрытым
частично (`covered_cents`). `paid_at` в графике остаётся ПРОИЗВОДНОЙ отметкой —
на неё опираются напоминания и дебиторка, и переписывать их незачем; пересчёт
делает `_sync_schedule_state`. Удаление поступления, которым сделка была
закрыта, ОТКРЫВАЕТ её обратно (`_reopen_deal`): иначе долг исчезает из
напоминаний, хотя денег нет. Кнопка «оплачен» — обёртка над поступлением на
плановую сумму.

**Приёмка контейнера в МойСклад (`services/ms_supply.py`).** Документ —
«Приёмка» (`entity/supply`), потому что это закупка и она должна попасть в
себестоимость. Цены НЕ спрашиваем при подсчёте (человек считает коробки, а не
деньги) — документ уходит с нулевыми ценами, суммы вписывают позже в МС.
Поставщик обязателен для `supply`, поэтому задаётся заранее, в карточке
контейнера (`container_supply` — отдельная таблица, не колонки в `containers`:
инкрементальных миграций нет). `sync_supply` не создаёт, а СИНХРОНИЗИРУЕТ:
количества правятся сутки, и документ едет за ними (POST → PUT по
`ms_supply_id`).

**Товар выбирает ЧЕЛОВЕК, а не поиск по названию.** Привязка позиции к карточке
номенклатуры лежит в `container_item_links` (снова отдельная таблица — колонка
в существующую `container_items` до прода не доехала бы), и `match_items` берёт
`ms_id` оттуда. Сопоставление по имени осталось ЗАПАСНЫМ путём для позиций,
заведённых до появления выбора: оно требуется точное, похожие имена не
склеиваем — угадывание означает приход не на ту карточку. Свободный ввод
остаётся законным: в контейнере регулярно едет то, чего в номенклатуре ещё нет,
и требовать карточку до сохранения значит остановить приёмку. Несопоставленное
возвращается списком с `item_id`, чтобы его можно было починить прямо из
карточки, — молча пропущенная позиция это остаток, которого нет.
Карточку заводит `ms_supply.create_product` по кнопке, НЕ автоматически:
автосоздание из приёмки превратило бы каждую опечатку в новый товар справочника.
Дубликат ловим по снапшоту, и свежесозданная карточка кладётся туда сразу
(`snapshot.remember_product`) — иначе вторая строка того же контейнера завела бы
второй такой же товар. Подсказки при вводе читают снапшот
(`/api/products/search`), а не МойСклад: запрос на каждое нажатие съел бы бюджет.

**Окно правки приёмки — 24 часа** (`containers.EDIT_WINDOW_HOURS`). После него
карточка закрывается: правки, удаление и добавление позиций отвергает
`_require_open_window` — один сторож на все операции, потому что закрыть
карточку в интерфейсе и принимать правки в ручках значит не закрыть её вовсе.
Расхождением считается только то, что ЗАЯВЛЯЛИ: позиция с `expected_qty == 0`
получает состояние `received`, а не `extra` — контейнер, состав которого не
заводили заранее, это опись прибывшего, а не сверка.

**Рассрочка техники (график).** Взнос — платёж `seq = 0` в
`machine_deal_payments`, а НЕ колонка в `machine_deals`: одно описание вместо
двух, и новая таблица доезжает до прода обычным `CREATE TABLE IF NOT EXISTS`
(инкрементальных миграций в проекте нет — колонка в существующую таблицу просто
не приехала бы). Помечен оплаченным сразу, иначе попал бы в напоминания в день
сделки. Остаток делится поровну, копейки от деления — в ПОСЛЕДНИЙ платёж (сумма
графика обязана сойтись с ценой). Дата прижимается к концу короткого месяца
(`_add_months`): у сделки 31 января иначе нет февральского платежа. Дату
закрытия сделки считает сервис, а не форма. Последний полученный платёж
закрывает сделку и двигает машину в `sold`. Напоминания — в `run_debts_notify`
(отдельным сообщением руководству), условие `due_date <= today` и отметка
`notified_at` ПОСЛЕ отправки: пропущенный прогон не должен съесть напоминание,
а ретрай — прислать дубль.

**Удаление родителя — чисти ВСЕХ детей явно.** На SQLite внешние ключи по
умолчанию выключены, на Postgres энфорсятся: забытая дочерняя таблица не падает
ни в одном тесте и всплывает 500-й на проде (так было с `container_supply`).
Дочерние таблицы перечислены списком (`containers.CHILD_TABLES`), а сторожа —
`test_delete_leaves_no_orphans_anywhere` (контейнеры) и
`test_machine_delete_leaves_no_orphans_anywhere` (техника) — собирают детей ИЗ
СХЕМЫ через `PRAGMA foreign_key_list` и ловят следующую такую таблицу сами.
**Порядок в списке — от листьев к корню**: `container_item_links` ссылается и на
контейнер, и на позицию, поэтому удаляется первой. Порядок проверяет
`test_child_tables_are_ordered_leaves_first`, тоже по схеме, — «нет сирот» на
SQLite проходит при любой перестановке.

**Контейнеры (`services/containers.py`).** Состав описан ДВУМЯ числами:
`expected_qty` (заявлено) и `arrived_qty` (факт). `arrived_qty IS NULL` — «ещё
не считали», а НЕ «приехало ноль»: иначе непроверенный контейнер выглядит как
полностью недостающий, а ноль перестаёт значить недостачу. Расхождения считает
`diff`/`diff_summary`, и сводка отдаётся ещё и в списке — иначе «сверить»
значит открыть каждый контейнер по очереди. Прибытие — CAS по статусу;
прибывший контейнер не удаляется (история приёмки). Позиции всегда ищутся
ВНУТРИ заявленного контейнера — гейт от подстановки чужого id.

**Канал (`services/channel.py`).** Публикует ЧЕЛОВЕК кнопкой — автопостинга в
канал компании нет. Черновик собирает СЕРВЕР (`/api/channel/draft`), а не фронт:
правило «наружу не уходит ни одна цифра количества» должно жить в одном месте.
Остатки, объёмы поставок и номер контейнера — внутреннее: клиенту не нужны, а
конкуренту рассказывают, сколько вы способны отгрузить. Сторож —
`channel.contains_quantities()` и тест `test_no_builder_ever_emits_quantities`,
который прогоняет все шаблоны разом. Цена — не количество, её печатаем.
Текст поста фронт может править: запрещать это значит заставлять публиковать не
то, что хотели; гарантия относится к сборщику, а не к тому, что человек допишет
руками. Анонс берёт позиции с `arrived_qty > 0` — обещать недоехавшее нельзя, и
само число наружу не идёт. `channel_posts` хранит историю, чтобы один контейнер
не ушёл в канал дважды. Кнопка под постом ведёт в личку менеджера — её
переписку уже считает воронка.
Фото товаров — `services/product_photos.py`, тот же приём, что у техники (файл
в Telegram, у нас идентификаторы). Загружаются руками: выгрузка картинок всего
каталога из МС — сотни запросов при падающем бюджете. Канал-хранилище —
`PHOTOS_TG_CHAT_ID` (старое `MACHINE_PHOTOS_TG_CHAT_ID` продолжает работать),
публичный канал — `CHANNEL_ID`.

**Воронка обращений (`services/leads.py` + `handlers/business.py`).** Бот
подключён к личному аккаунту менеджера через Telegram Business и ТОЛЬКО
наблюдает. Два инварианта держатся тестами (`tests/test_leads.py`):
`handlers/business.py` не содержит ни одного вызова отправки (проверка по AST) и
не читает `message.text`/`caption` — в сервис уходят только id, имя и
направление. **Текстов переписки в БД нет и быть не должно**: для метрик хватает
«кто, когда, в какую сторону», а хранение чужих сообщений это ответственность
без выгоды.
Лид — это ЧЕЛОВЕК (`leads.tg_user_id` UNIQUE), а не переписка: иначе «сколько
клиентов обратилось» удвоится, когда один написал двум менеджерам.
Состояния (`replied`, `awaiting_reply`, `silent`, `never_answered`) НЕ хранятся —
выводятся из отметок времени `lead_state()`, поэтому не расходятся с фактами.
Руками ставится только исход (`won`/`lost`): в переписке его не видно. Возврат
клиента (`reengaged`) фиксируется событием В МОМЕНТ сообщения — по агрегатам
паузу задним числом не восстановить. Воронка считает по ПЕРВОМУ обращению в
периоде, иначе активный клиент выглядел бы десятью обращениями и конверсия
падала бы от активности, а не от результата.
Требует Telegram Premium у менеджера; `business_connections.can_read = 0` —
типичная причина «клиенты не пишут», поэтому состояние подключений отдаётся
боссу в `/api/leads/list`.

**Деньги: слой дебиторки (`services/receivables.py`).** Заказы в кредит и
рассрочки по технике — два учёта, один вопрос. Всё, что отвечает «где деньги»
(экран «Долги», аналитика «Деньги», отчёт в чат), считается ЗДЕСЬ, а не в
ручках. Правила: остаток по заказу берётся из `services.debts` (не пересчитывай
— это пятый способ посчитать долг); суммы в разных валютах не складываются
молча, блок отдаёт `by_currency` + `base_total` + `partial`, и `partial` обязан
доезжать до экрана; границы просрочки считаются в Python от `local_now()`.
Просроченное в прогноз будущих поступлений не попадает. Дисциплина считается
только по технике: у заказа один `due_date` на весь долг, и приравнивать
частичную оплату к плановому платежу значит выдумать метрику. Покупатель
техники опознаётся по имени (`buyer_key` схлопывает регистр и пробелы) — иного
идентификатора у него нет, пока сделка не привязана к контрагенту МС.

**Rich Message (Bot API 10.1, aiogram 3.30).** Бот умеет слать в чат статьи с
заголовками, списками и ТАБЛИЦАМИ (`services/money_report.py`). Это снимает
причину, по которой T3.3 уводил всю глубину в WebApp, но не отменяет само
правило: в чат уходит СВОДКА, действия остаются в WebApp. **Фолбэк
обязателен** — при любой ошибке `send_rich_message` тот же отчёт уходит текстом
через `tg_send_message`; отчёт, который не дошёл, хуже некрасивого. Текстовая
версия несёт те же разделы, а не огрызок, и экранирует имена (`esc`): сообщение
идёт с parse_mode=HTML, и контрагент «ООО <Строй>» иначе рушит разметку целиком.

**Аналитика менеджеров:** только из ЛОКАЛЬНЫХ `orders` (`get_manager_performance`, GROUP BY `user_id`) — в demand'ах МС нет надёжной привязки к менеджеру (`telegram_full_name` ставится лишь когда demand создал бот). Сурфейсится в webapp company-аналитике (`top_managers`).

**Backup:**
- `tasks/run_backup`: если `pg_dump` упал (нет бинаря — `FileNotFoundError`, ИЛИ `server version mismatch` — `RuntimeError`: бинарь старее managed-Postgres) → fallback на version-independent pure-Python COPY-dump (`_pg_dump_pure_python` через libpq). Лови ОБА исключения.
- `tasks/run_maintenance`: janitor чистит дедуп отгрузок, аудит старше ретеншена (`prune_audit_log`, `audit_log_retention_months`) и soft-deleted. Внешний архив аудита (Google Drive) убран.

**Time:** `utils.helpers.utc_now()` вместо deprecated `datetime.utcnow()`. `utils.helpers.local_now()` для сравнений с `created_at` (пишется в local TZ через `now_str()`).
- В SQL **не** сравнивай `confirmed_at`/`created_at` (local TZ string) с `datetime('now',...)` SQLite (UTC) или `NOW()` Postgres — лекс-сравнение в разных TZ молча всегда False (silent bug). Вычисляй порог в Python через `datetime.now() - timedelta(...)` и передавай параметром. Пример — `services/database.reset_stale_in_progress_payments`.

**Idempotency:** `/api/orders/{confirm_payment,mark_paid,reject_payment}` принимают `idempotency_key` через `_idem_get/_idem_set`. ВАЖНО: всегда вызывай `_idem_set` ПОСЛЕ успеха (был баг — `reject_payment` делал только `_idem_get` → ретрай слал дубль-уведомление). Применяй паттерн для новых write-endpoint'ов.

**Logging:**
- НЕ дёргай root logger (`logging.warning(...)`/`logging.info(...)`) на module-import time. Первый же вызов авто-триггерит `basicConfig(level=WARNING)` с дефолт-форматтером, и любой последующий `logging.basicConfig(level=INFO, ...)` в bot.py/tasks/run_*.py становится no-op → ВСЕ INFO глушатся на проде. Используй `logger = logging.getLogger(__name__)` — propagation идёт через `lastResort`, root остаётся чист.
- В cron-CLI (`tasks/run_*.py`) ставь `logging.basicConfig(level=INFO, format=...)` ДО любого импорта, который потенциально логирует на module-уровне. Сейчас все cron-CLI и `bot.py` это делают корректно.

**Cron-CLI стабильность:**
- Перед основной логикой вызывай идемпотентный reaper для своих долгоживущих in-flight состояний. Пример — `services.database.reset_stale_in_progress_payments(30)` в начале `tasks/run_ms_sync_retry`: сбрасывает orphan'ов 'in_progress' (claim был, но процесс убили до set_failed). Без него стуки залипают навсегда, потому что `claim_payment_for_ms_sync` отвергает уже-'in_progress'.
- В `tasks/run_ms_sync_retry`: pending-fetch ДО любого МС-вызова, `return 0` на пустой очереди ДО `init_demand_context()` — иначе 96 cron-тиков/день делают ~200-400 МС API calls впустую.
- `init_demand_context()` сам по себе НЕ raises (helpers `_pick_first`/`_ensure_custom_attribute` глотают exception); инспектируй возвращаемый dict (`ready/org/store/attribute_name/attribute_uid`), а не оборачивай в `try/except` (dead code).

## Security audit

`SECURITY.md` — исходный отчёт + Closed-таблица. Все пункты исходного аудита закрыты; новые фиксы этой сессии (esc agent_name в HTML, энфорс кредит-лимитов, деактивация=guest, reject_payment idempotency, индексы на горячих путях) дописаны в Closed.

## CI

`.github/workflows/ci.yml` на push в `main` или `claude/*`:
- `ruff` строгий гейт (`E9,F63,F7,F82`) + полный набор (из `pyproject.toml`);
- `mypy` — **блокирующий** (модули вычищены до 0 ошибок, тип-регрессии валят CI);
- `pytest` + coverage с «храповиком» `--cov-fail-under=55` (факт после T3.3 ~67%, буфер ~12%: срез бот-дублей убрал в основном непокрытый код).

`TELEGRAM_TOKEN=0:fake` и `MS_TOKEN=fake` — заглушки для импорта config (в workflow и в `tests/conftest.py` через `os.environ.setdefault`).

## Тесты (конвенция)

Мокай ГРАНИЦУ с внешним миром, а не свой код. Урок: баг в `tg_send_message` пережил CI, потому что тесты мокали саму `tg_send_message`. Для исходящих HTTP — `aioresponses` (мок транспорта, реально исполняется сборка URL/payload). БД — настоящая (`isolated_db`), не мок.

Если добавляешь module-level `asyncio.Semaphore`/`Lock` — добавь регресс-тест с 2× `asyncio.run` и contention >cap (см. `tests/test_analytics_parallel.py::test_positions_semaphore_survives_multiple_asyncio_run_with_contention`). Без waiter'а в очереди loop-binding не воспроизводится и landmine ждёт первого «толстого» теста.

**Фронт — дизайн-система (UI_REBUILD_PLAN, S0–S6).** Поверхности и строки
списков: `.c-surface` / `.c-surface--list` / `.c-surface--pad` / `.c-row`
(+`.c-row--tap` для кликабельных, там же min-height 44px). Старые имена
(`.card-row`, `.stock-row`, `.debt-card`, …) — алиасы на те же правила,
переходный период; новые экраны пишут примитивы напрямую. Статус (заказа,
долга, остатка, движения денег) — атрибутом `data-status`, цвет выводится из
`--status-c`/`--status-bg`; НЕ заводи для нового состояния свой класс с цветом.
Переключатели: `.seg` (уровень 1) и `.subseg` (уровень 2 в Финансах) — третьего
языка быть не должно; `.cat-btn`/`.cur-btn` — задокументированные исключения.
Формы: поле — `.c-field`, ряд кнопок — `.c-actions` (`.price-*` остались
алиасами), ошибка показывается ВНУТРИ формы (`.c-error`), а не системным
алертом — он закрывает диалог вместе с уже введёнными данными.
Деньги форматируй `formatMoney`, МС-баланс — `msBalanceLabel`; пустое/ошибка/
скелетон — `emptyState`/`errorBoxHtml`/`skeleton` из `helpers.js` (они
экранируют вход). Запросы: `api()` бросает Error(detail) — этого хватает
почти везде; `apiResult()` отдаёт полное тело и код, он нужен там, где ответ
409 несёт данные для решения (`needs_force`, `current`).
**Навигация — пять разделов по ПРЕДМЕТАМ**: Сегодня · Продажи · Склад · Деньги ·
Клиенты. Раздела «Аналитика» нет: отчёт лежит вкладкой внутри того раздела,
который он описывает (`Продажи → Отчёт`, `Деньги → Отчёт`) — раздельность
«данные тут, отчёт о них там» и делала интерфейс ненаходимым. Таблица разделов и
их вкладок — в `helpers.js` (`NAV_SECTIONS`, `salesTabs`/`stockTabs`/`moneyTabs`/
`clientsTabs`), кнопки панели строит `buildNav()` под роль: таб, который
гарантированно ответит 403, не рисуем (у кладовщика нет «Сегодня» — `/api/home`
ему не отвечает). Старые имена экранов держит `LEGACY_SCREENS` — ссылки из бота
и пушей продолжают работать, алиас `'sales:report'` переводит и на раздел, и на
вкладку.
Шелл раздела (`sectionNavHtml` через `salesShellHtml`/`stockShellHtml`/
`sectionShell`) обязан входить в КАЖДЫЙ `innerHTML` ветки, включая скелетон и
ошибку — иначе первый же ре-рендер уносит переключатель (UI-BUG-04).

**«Сегодня» — очередь дел, а не витрина** (`services/work_queue.py` →
`/api/today`). Состав и ПОРЯДОК считает сервер: порядок это и есть ответ на «с
чего начать», а в шаблоне он разъедется с ролями при первой правке.
Просроченные деньги (`crit`) выше ждущего решения (`warn`), оно выше «клиент
молчит» (`info`); внутри одной срочности — по величине счётчика. Каждый пункт
несёт `screen` с вкладкой (`money:debts`) — счётчик без адреса заставляет
искать руками то, о чём сам же сообщил. Сбой одного счётчика гасится в лог:
экран с четырьмя пунктами из пяти полезнее, чем ошибка вместо всех пяти.
Ручка отвечает ВСЕМ рабочим ролям — за счёт этого «Сегодня» есть и у
кладовщика с бухгалтером, которым `/api/home` не отвечает (у них экран
состоит из одной очереди).
**Стекло (S7) — только на неподвижном.** `backdrop-filter` стоит на нижней
панели, переключателе вкладок и утилите `.u-glass`; на строках списка его нет и
быть не должно — размытие полусотни строк роняет прокрутку в WebView, а текст на
полупрозрачном фоне теряет контраст. Тинт (`--glass-bg`/`--glass-edge`/
`--glass-spec`) включается ТОЛЬКО внутри `@supports (backdrop-filter)`: без
размытия полупрозрачная панель нечитаема, поэтому базовое правило красится
плотным `--bg-card`. Под стеклом лежит собственное поле (точечная сетка + две
диагональные полосы на `body`): в Telegram фон задаёт тема пользователя и он
часто плоский — размывать было бы нечего. Поле выводится из `--accent` через
`color-mix`, и он ОБЯЗАН быть за `@supports (color: color-mix(…))`: невалидное
значение делает custom property guaranteed-invalid, и `background` со ссылкой на
неё отваливается целиком — страница осталась бы вообще без фона. Украшения
снимает `@media (prefers-contrast: more)`.
Состояние строки списка показывает полоса слева (`.c-row[data-status]::before`),
а не только цвет текста: на площадке при ярком солнце цвет теряется первым.
Инварианты держит `webapp/static/__tests__/design-system.test.js`:
забытый цвет статуса, отсутствие тёмного варианта, таргет мельче 44px,
размытие не на том элементе и color-mix без фолбэка валят CI.
Статус, объявленный только у конкретного класса (как `in_stock` у
`.stock-badge`), в ОБЩЕЙ матрице не появляется — для него нужна отдельная
строка, иначе элемент останется бесцветным при формально «объявленном» статусе.
Права по ролям для ручного QA: `python scripts/gen_role_matrix.py` → `UI_QA_ROLES.md`.

**Фронт (Vitest):** чистые хелперы `webapp/static/helpers.js` + jsdom-смоук загрузки `app.js` — в `webapp/static/__tests__/`. Гоняет CI (`npm test`). Локально Node нет → `scripts/setup-node.ps1` ставит portable Node в `.tools/node` (gitignore, ~35MB zip с nodejs.org, без admin), `scripts/test-js.ps1` делает `npm install` (1×) + `vitest run`. Скрипты — UTF-8 **с BOM** (иначе PowerShell 5.1 читает их как ANSI и кириллица в Write-Host превращается в кракозябры).
