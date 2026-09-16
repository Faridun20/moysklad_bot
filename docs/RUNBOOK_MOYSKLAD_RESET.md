# Runbook: сброс бизнес-данных и повторный перенос из МойСклад

Разовая операция. Выполняется **только в день явного «да» владельца**. Всё
деловое в базе стирается и заново приезжает из МойСклад; остаются сотрудники и
их роли (и то, без чего вход и сама система не работают — список ниже).

Сервер: docker compose в `/srv/docker/moysklad_bot`, Postgres — внешний
контейнер `postgres` (сеть `backend`, база `moysklad_bot`, роль `farid_admin`).
Все команды ниже — от пользователя `fara`, из `/srv/docker/moysklad_bot`, если
не сказано иное.

---

## 0. Что сохраняется и что стирается

Решение принимает `scripts/reset_business_data.py`, а не этот документ: каждая
таблица базы обязана быть в его списке `KEEP` или `WIPE`, иначе `--apply`
отказывает (новая таблица без решения — стоп, а не молчаливое стирание).

**Сохраняется:**

| Таблица | Почему |
|---|---|
| `user_roles` | сотрудники и роли, вход в бот и WebApp (в т.ч. деактивации — это колонка `deactivated_at` той же таблицы) |
| `user_permissions` | персональные права сотрудника — часть «роли» |
| `user_prefs` | личные настройки вида сотрудника; ни на что деловое не ссылаются |
| `business_connections` | подключение Telegram Business менеджера; Telegram присылает его только при подключении, стерев — придётся переподключать |
| `currency_rates`, `currency_rate_daily` | курсы ЦБ РУз — техника; архив нужен переносу истории (курс на дату документа) |
| `cron_runs`, `ops_monitor_runs` | журнал cron (панель «Резервные копии») и отметка дневного пинга |
| `document_templates` | реестр шаблонов юр. документов, засевается кодом |
| `app_settings` — **частично** | только `backfill_done:*` (иначе разовые data-миграции повторились бы на перенесённых данных), `fx_sync_last_run`, `boss_digest_last_run_at` |

**Стирается** (78 таблиц): заказы, заявки, позиции, отгрузки; платежи, разбивка
оплаты, сдачи, сверки кассы, возвраты; долги и выплаты поставщикам; накладные,
остатки, склады, перемещения, списания, пересчёты; товары, цены, фото;
клиенты и поставщики, кредит-лимиты; себестоимость; контейнеры; техника и
рассрочки; бухгалтерия и «Наши карты и счета»; обращения; созданные документы;
`audit_log` (после сброса в нём одна запись о самом сбросе); ключи
идемпотентности; `ms_id_map` и мёртвые зеркала `ms_*`; `invoice_counters`
(номера накладных начнутся с 0001). Бизнес-настройки `app_settings` (пороги,
время дайджеста, выключатели, **реквизиты компании `company_*`**) стираются —
`tasks.migrate` засевает значения по умолчанию, реквизиты и «Наши карты и
счета» владелец вводит заново.

**id-последовательности по умолчанию НЕ сбрасываются**: id сидят в кнопках уже
отправленных Telegram-карточек (`req_ok:<id>`, `pay_ok:<id>`…), и старая кнопка
«Одобрить» с нумерацией с 1 одобрила бы новую заявку с тем же номером.
Продолжение нумерации — старая кнопка честно отвечает «не найдено». Если
владелец хочет номера с 1 и старых карточек не жалко — флаг `--restart-ids`
(dry-run показывает, какие последовательности где стоят).

Файлы созданных документов в томе `appdata` (`/app/data`) база не хранит — их
удаление отдельное и необязательное (п. 9).

---

## 1. Заранее (за день и раньше)

1. **Код на проде.** Ветка с `scripts/reset_business_data.py` и обновлёнными
   `migrate_*_from_moysklad.py` влита в `main` и выкачена автодеплоем.
   Проверка: `docker compose run --rm --no-deps migrate ls scripts/reset_business_data.py`.
2. **Токен МойСклад убрать из `.env`.** Сейчас в `/srv/docker/moysklad_bot/.env`
   есть строка `MS_TOKEN=…` (40 символов) — токен лежит на диске и через
   `env_file` попадает в окружение КАЖДОГО контейнера. Коду бота он не нужен
   (читает только перенос). Удалить строку (`sed -i '/^MS_TOKEN=/d' .env`),
   после переноса — перевыпустить токен в МойСклад (старый считать засвеченным).
3. **Пробный dry-run сброса на проде** (ничего не меняет, сервисы можно не
   останавливать — он только предупредит о подключениях):
   ```bash
   docker compose run --rm --no-deps migrate python -m scripts.reset_business_data
   ```
   Смотреть: нет строки «таблицы не классифицированы», есть хотя бы один
   активный admin/boss/manager в «Сотрудники, которые останутся». Сейчас
   (бэкап 16.09 18:48) активный вход один: `941599419 manager`;
   `8129841604` — guest.
4. **Решения владельца до начала** (их спросит перенос истории):
   * `--supplier-history ledger|settled` — по предпросмотру (п. 6.3):
     `ledger` — приходы МС становятся долгами поставщикам и гасятся выплатами;
     `settled` — история закрыта, приходы «уже оплачено».
   * `--orders-owner <telegram id>` — если компанию ведёт один сотрудник с ролью
     manager: иначе перенесённые заказы (user_id 0) ему не видны ни в
     «Заказах», ни в «Долгах» (видит только руководство). При одном
     `941599419 manager` — нужен.
   * `--restart-ids` — нет по умолчанию (см. п. 0).
5. Предупредить сотрудников: окно ~30–45 минут бот и WebApp недоступны.

---

## 2. Остановить всё, что пишет в базу

```bash
cd /srv/docker/moysklad_bot
T0=$(date +%s)

# 2.1 Автодеплой и cron этого проекта — на паузу (копия crontab — рядом с бэкапом).
mkdir -p /srv/backups/pre-moysklad-reset && chmod 700 /srv/backups/pre-moysklad-reset
crontab -l > /srv/backups/pre-moysklad-reset/crontab.before
crontab -l | sed -E 's@^([^#].*(moysklad_bot|moysklad_bot_autodeploy).*)$@#MSRESET \1@' | crontab -
crontab -l | grep -c '^#MSRESET'          # ожидается 9 (7 cron + boss-digest + автодеплой)
#   pg-backup, weekly-backup и мониторинг хоста (/srv/ops) остаются: они в базу
#   бота не пишут. host_alert/deadman могут прислать алерт «webapp недоступен» —
#   на время окна это ожидаемо.

# 2.2 Дождаться, что сейчас не идёт выкат и ни один cron-прогон:
flock /srv/docker/moysklad_bot_deploy.log.lock true
docker ps --format '{{.Names}}' | grep -E 'moysklad_bot-cron' || echo "cron-прогонов нет"

# 2.3 Бот и WebApp — стоп (redis оставляем: он не пишет в Postgres).
docker compose stop bot webapp
docker compose ps
```

---

## 3. Отдельная копия бэкапа ВНЕ 14-дневной ротации

Ночные `all_*.sql.gz` в `/srv/backups/postgres/` удаляются через 14 дней —
откат через месяц был бы невозможен. Дамп снимается **после** остановки
сервисов (п. 2), чтобы в нём было всё до последней записи.

```bash
S=$(date +%s)
DUMP=/srv/backups/pre-moysklad-reset/all_pre_reset_$(date +%Y-%m-%d_%H-%M).sql.gz
( umask 077; docker exec postgres pg_dumpall -U farid_admin | gzip > "$DUMP" )
ls -l "$DUMP" && gzip -t "$DUMP" && zcat "$DUMP" | grep -c '^CREATE TABLE public\.'   # 88 на 16.09.2026
echo "дамп: $(( $(date +%s) - S )) с"
```

Этот файл НЕ удалять вместе с ротацией; хранить минимум до конца месяца после
переноса. `find … -mtime +14 -delete` из `pg-backup.sh` смотрит только в
`/srv/backups/postgres/`.

---

## 4. Сброс: dry-run → apply

Скрипт сам откажет (код 1, база не тронута), если: нет
`--i-understand-this-deletes-everything`; бэкап не найден, пустой, старше 2 ч,
не читается целиком (gzip CRC) или это не дамп бота; к базе подключён кто-то
ещё; есть неклассифицированная таблица; в `user_roles` нет ни одного активного
admin/boss/manager. **Почему отказ, а не предупреждение:** предупреждение
читается глазами в конце длинного вывода и пролистывается, а отката без
бэкапа нет. Всё выполняется ОДНОЙ транзакцией (`LOCK … ACCESS EXCLUSIVE`,
`DELETE` в порядке внешних ключей самой базы, сверка «стёрто / сохранено»,
запись в `audit_log`): упало что угодно — не удалено ничего.

```bash
B=$(basename "$DUMP")
RUN="docker compose run --rm --no-deps -v /srv/backups/pre-moysklad-reset:/backup:ro migrate"

# 4.1 dry-run: план, счётчики, сотрудники, последовательности, проверка бэкапа
$RUN python -m scripts.reset_business_data --backup /backup/$B
#   Проверить: нет «--apply сейчас откажет»; сотрудники в списке — те.

# 4.2 apply
$RUN python -m scripts.reset_business_data --apply \
    --i-understand-this-deletes-everything --backup /backup/$B
#   Ожидается: «✓ Сброс выполнен за … с: удалено N строк». Код 0.

# 4.3 схема, склад по умолчанию, настройки по умолчанию
docker compose run --rm --no-deps migrate
```

Контейнер приложения работает под uid 1000 = `fara`, поэтому дамп с правами
0600 ему читаем; монтирование — только на чтение.

---

## 5. Токен МойСклад — только в памяти одного запуска

Правило: токен **никогда** не пишется на диск — ни в `.env`, ни в историю
shell, ни в конфиг контейнера. `docker compose run -e MS_TOKEN=…` для этого не
годится: значение окружения сохраняется в `config.v2.json` контейнера на диске
(пока контейнер существует). Поэтому токен вводится ВНУТРИ контейнера через
`read -s`, живёт в памяти одного процесса и исчезает вместе с `--rm`:

```bash
ms() {  # ms <модуль> <аргументы…>  — токен спросит сам, на экран не выводит
  docker compose run --rm --no-deps migrate bash -c \
    'printf "Токен МойСклад: " >&2; read -rs MS_TOKEN; echo >&2; export MS_TOKEN; exec python -m "$@"' _ "$@"
}
```

Функция живёт только в текущем shell; токен вставляется по запросу на каждом
из четырёх запусков ниже.

---

## 6. Перенос из МойСклад

Темп — 3 запроса/с (бюджет лимитов МС), параллельности нет. Боевой объём
(≈26 заказов, ≈421 отгрузка, ≈326 платежей, ≈60 поступлений) — десятки
запросов на выгрузку; закладывать до 5–10 минут на оба скрипта с dry-run.

```bash
# 6.1 Справочники: товары, клиенты/поставщики, остатки, цены
ms scripts.migrate_from_moysklad --dry-run
ms scripts.migrate_from_moysklad --apply
```
Проверить в отчёте:
* «✓ Сверка сошлась»;
* **«ОТРИЦАТЕЛЬНЫЙ ОСТАТОК в МС: N позиций записаны нулём»** — список товаров с
  минусом в МС. Минус в нашу базу не пишется (иначе не ставится
  `stock_quantity_chk`, а продажа из минуса ломала бы остаток); эти позиции
  надо пересчитать на складе и оформить инвентаризацией. Список сохранить
  (скопировать из вывода) и отдать кладовщику. В прошлый раз было 32;
* «дубли артикулов» — артикул оставлен у первой карточки, остальные в списке.

```bash
# 6.2 История: предпросмотр
ms scripts.migrate_history_from_moysklad --dry-run
```
Проверить:
* «НЕ СОПОСТАВЛЕНО» — категории разбора руками (не ошибка скрипта);
* «ЧТО ПОКАЖЕТ ПРИЛОЖЕНИЕ»: долги поставщикам в режимах ledger/settled и
  «Долги» клиентов — по ним владелец выбирает `--supplier-history`;
* «СВЕРКА С БАЛАНСАМИ КОНТРАГЕНТОВ В МС» — расхождения обычно объясняются
  возвратами (они не переносятся и перечислены).

```bash
# 6.3 История: запись (решения из п. 1.4)
ms scripts.migrate_history_from_moysklad --apply --supplier-history settled --orders-owner 941599419
#   или --supplier-history ledger; без --orders-owner, если в штате есть admin/boss
```
Ожидается «✓ Сверка сошлась», сводки «ЗАКАЗЫ ПО ТИПУ ОПЛАТЫ», должники, расчёты
с поставщиками. Исторические платежи пишутся `confirmed` БЕЗ разбивки
(`payment_parts`) — это штатно: подтверждённый платёж объясняет деньги заказа
и без неё, `migrate_payment_breakdown` их не требует.

Если что-то пошло не так на 6.x — каждый `--apply` одной транзакцией; повторный
запуск идемпотентен (по ключам документов МС). Решение переделать с нуля —
вернуться к п. 4 (сброс ещё раз, бэкап тот же, если моложе 2 ч, иначе
`--backup-max-age-hours`).

---

## 7. Ограничения базы

```bash
docker compose run --rm --no-deps migrate python -m scripts.apply_constraints            # dry-run
#   Ожидается: нарушителей нет; «Будет применено: 1» — stock_quantity_chk
docker compose run --rm --no-deps migrate python -m scripts.apply_constraints --apply
docker exec postgres psql -U farid_admin -d moysklad_bot -Atc \
  "SELECT conname, convalidated FROM pg_constraint WHERE conname = 'stock_quantity_chk';
   SELECT 'NOT VALID: ' || count(*) FROM pg_constraint WHERE NOT convalidated;"
#   stock_quantity_chk|t   и   NOT VALID: 0
docker compose run --rm --no-deps migrate python -m tasks.run_fx_sync   # сегодняшний курс ЦБ
```

---

## 8. Запуск и проверки

```bash
docker compose up -d
crontab /srv/backups/pre-moysklad-reset/crontab.before && crontab -l | grep -c '^#MSRESET'   # 0
echo "простой: $(( ($(date +%s) - T0) / 60 )) мин"

curl -s http://127.0.0.1:8080/healthz          # {"ok":true,"version":"<коммит>" …}
docker compose logs --since 5m bot webapp | grep -E 'ERROR|Traceback' || echo "ошибок нет"
```

SQL-проверки (только чтение):
```bash
docker exec postgres psql -U farid_admin -d moysklad_bot <<'SQL'
-- остатки: минуса нет, строк примерно как позиций с остатком в МС
SELECT count(*) AS stock_rows, count(*) FILTER (WHERE quantity < 0) AS negative FROM stock;
-- сотрудники на месте
SELECT user_id, role, deactivated_at IS NOT NULL AS deactivated FROM user_roles ORDER BY user_id;
-- заказы: кто автор, сколько в долг
SELECT user_id, payment_type, currency, count(*) FROM orders GROUP BY 1,2,3 ORDER BY 1,2,3;
-- платежи и выплаты поставщикам не перепутаны
SELECT (SELECT count(*) FROM payments) AS payments, (SELECT count(*) FROM supplier_payments) AS supplier_payments;
-- запись о сбросе — первая в журнале
SELECT id, action, created_at FROM audit_log ORDER BY id LIMIT 3;
SQL
```

В WebApp (под владельцем):
* «Склад» — товары и остатки; позиции из списка «отрицательный остаток» стоят
  нулём;
* «Деньги → Долги → Клиентам» — суммы по валютам сходятся с «Долги» клиентов из
  предпросмотра 6.2; «Поставщикам» (admin/boss) — с режимом, выбранным в 6.3;
* «Заказы» — перенесённые заказы видны (при `--orders-owner` — менеджеру);
* «Резервные копии» — панель на месте (журнал cron сохранён).
* Владелец вводит реквизиты компании и «Наши карты и счета», проверяет пороги
  в настройках (сброшены к значениям по умолчанию).

Сверка балансов с МС: вывод 6.2 («СВЕРКА С БАЛАНСАМИ») сохранить и пройтись
по расхождениям с бухгалтером/владельцем.

---

## 9. Необязательно

* Файлы созданных юр. документов старой базы в томе `appdata`: посмотреть
  `docker compose run --rm --no-deps migrate ls -la /app/data` и удалить
  сгенерированные документы, если не нужны (логотип и шаблоны не трогать).
* Черновики заказов бота (FSM в Redis) ссылаются на старые товары:
  `docker compose exec redis redis-cli --scan --pattern 'fsm:*' | head` — если
  есть, `… | xargs -r docker compose exec -T redis redis-cli del`.
* Перевыпустить токен МойСклад (см. п. 1.2).

---

## 10. План отката

Когда: что-то в п. 4–8 пошло не так, и чинить на месте нельзя/долго.

```bash
cd /srv/docker/moysklad_bot
docker compose stop bot webapp
crontab -l | grep -q '^#MSRESET' || crontab -l | sed -E 's@^([^#].*(moysklad_bot|moysklad_bot_autodeploy).*)$@#MSRESET \1@' | crontab -

docker exec postgres psql -U farid_admin -d postgres -c 'DROP DATABASE moysklad_bot WITH (FORCE)'
zcat "$DUMP" | docker exec -i postgres psql -U farid_admin -d postgres -q 2>&1 | grep ERROR
#   Допустимо только: ERROR:  role "farid_admin" already exists
docker exec postgres psql -U farid_admin -d moysklad_bot -Atc \
  "SELECT count(*) FROM information_schema.tables WHERE table_schema='public'"   # 88

docker compose up -d
crontab /srv/backups/pre-moysklad-reset/crontab.before
curl -s http://127.0.0.1:8080/healthz
```

`$DUMP` — файл из п. 3 (если shell закрыт — `ls -t /srv/backups/pre-moysklad-reset/*.sql.gz | head -1`).
На репетиции откат вернул все 88 таблиц с теми же счётчиками строк и теми же
156 CHECK/FK.

---

## 11. Репетиция на копии (16.09.2026)

Отдельный `postgres:16-alpine` (`docker run --rm`, tmpfs, своя сеть, порт
127.0.0.1:55439), восстановлен `all_2026-09-16_18-48.sql.gz` (41 КБ, 88
таблиц, 32 отрицательных остатка, ограничения CHECK/FK 156). Код — боевой
образ `moysklad-bot:local` (7d5319d) с `scripts/` из ветки, пользователь
uid 1000. МойСклад — синтетическая выгрузка в форме API (800 товаров, 286
контрагентов, 700 строк остатка из них 31 в минусе, 26 заказов, 418
отгрузок, 326 входящих и 57 исходящих денег, 60 поступлений, 5 возвратов);
сеть к МС не использовалась. Прод-БД и `/srv/docker/moysklad_bot` не
затрагивались.

| Шаг | Время |
|---|---|
| восстановление бэкапа в копию | 0,5 с |
| `tasks.migrate` на восстановленной базе | 0,6 с |
| дамп перед сбросом (`pg_dumpall \| gzip`) | 0,3 с |
| reset dry-run (с запуском контейнера) | 0,6 с |
| reset `--apply` без `--backup` | отказ, код 1 |
| reset `--apply` (с запуском контейнера; сама транзакция 0,1 с, 902 строки) | 0,7 с |
| `tasks.migrate` после сброса | 0,6 с |
| справочники dry-run / apply | 1,2 / 1,1 с |
| история dry-run / apply `settled --orders-owner` | 2,4 / 2,0 с |
| `apply_constraints` dry-run / apply (`stock_quantity_chk` встал, NOT VALID 0) | 1,0 / 1,0 с |
| `run_fx_sync` | 1,0 с |
| старт WebApp + проверки API (healthz через 2 с) | 3,4 с |
| откат: DROP DATABASE + восстановление дампа | 0,5 с |

Итого вся цепочка — 23 с машинного времени. На проде к этому добавляются
`docker compose run` (~1–2 с на запуск), выгрузка из МС (минуты, темп 3
запроса/с) и ручные проверки; окно простоя закладывать 30–45 минут.

После сброса: `app_settings` 4 (технические ключи), `audit_log` 1, `cron_runs`
83, курсы 2+6, `document_templates` 3, `ops_monitor_runs` 2, `user_roles` 2 —
остальное пусто. После переноса менеджер `941599419` видит 421 заказ и 205
открытых долгов (с `--orders-owner`), `/api/stock` — 800 товаров, ошибок и
500 в логах WebApp нет; отрицательных остатков 0.
