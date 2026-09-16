"""
РАЗОВЫЙ сброс бизнес-данных перед повторным переносом из МойСклад.

Решение владельца (сентябрь 2026): «Потом нужно будет перенести базу из
МойСклад снова, удалить существующую, и начать с новой базы». Всё, что сейчас
в боте, — тестовые данные. СОХРАНЯЮТСЯ только сотрудники и их роли (и то, без
чего вход и сама система не работают); всё деловое стирается и заново
приезжает переносом из МойСклад или вводится руками.

НЕ часть кода бота: ничем не импортируется, со старта не вызывается. Только
Postgres (на SQLite сбрасывать нечего — там тестовые базы).

Использование:
    python -m scripts.reset_business_data                  # = dry-run: план и счётчики
    python -m scripts.reset_business_data --apply \\
        --i-understand-this-deletes-everything \\
        --backup /backups/all_2026-09-20_07-00.sql.gz       # свежий бэкап обязателен

Что делает --apply (ОДНОЙ транзакцией — упало что угодно, не удалено ничего):
    1. Проверки ДО транзакции: флаг-подтверждение; бэкап существует, не пустой,
       моложе `--backup-max-age-hours` (2 ч), читается целиком (gzip с CRC) и
       содержит схему бота; к базе не подключён никто, кроме нас (бот, WebApp,
       cron обязаны быть остановлены — иначе они пишут посреди сброса); в
       `user_roles` есть хотя бы один активный admin/boss/manager (иначе после
       сброса в систему некому войти).
    2. Транзакция: `LOCK TABLE … ACCESS EXCLUSIVE` на всё, что стирается
       (`lock_timeout` 15 с — ждать чужой замок вечно незачем), счётчики,
       `DELETE` в порядке «дети раньше родителей» — порядок выводится из
       внешних ключей САМОЙ базы (`pg_constraint`), а не из головы: на проде
       FK/CHECK ставит `scripts/apply_constraints`, в DDL их нет, и ручной
       список разошёлся бы с базой на первой новой связи.
    3. Сверка внутри той же транзакции: стираемые таблицы пусты, сохраняемые —
       с теми же счётчиками. Иначе — откат.
    4. Одна строка `audit_log` «business_data_reset» (кто, какой бэкап, сколько
       строк) — первая запись новой истории: через месяц на вопрос «куда делись
       старые заказы» ответ будет в самой базе.

═══════════════════════════════════════════════════════════════════════════
ЧТО СОХРАНЯЕТСЯ И ПОЧЕМУ
═══════════════════════════════════════════════════════════════════════════
* `user_roles` — сотрудники и роли: решение владельца, ради этого всё и
  затевается (вход в WebApp и бота — по `user_roles`).
* `user_permissions` — персональные права сотрудника; та же сущность, что роль.
* `user_prefs` — личные настройки ВИДА сотрудника («Рабочие действия»,
  счётчик подсказки). Не бизнес-данные, и ни на одну удалённую строку не
  ссылаются.
* `business_connections` — подключение Telegram Business личного аккаунта
  менеджера. Telegram присылает его только при подключении/смене прав; сотрёшь
  — воронка решит, что подключения нет, пока менеджер не переподключит бота.
  Сами обращения (`leads`, `lead_*`) стираются.
* `currency_rates`, `currency_rate_daily` — курсы ЦБ РУз: техника, а не дело
  компании. Архив НУЖЕН переносу истории: если учётная валюта МС не
  BASE_CURRENCY, курс на дату документа берётся отсюда. После сброса всё равно
  гоняется `tasks.run_fx_sync` (догнать сегодняшний курс).
* `cron_runs`, `ops_monitor_runs` — журнал прогонов cron (панель «Резервные
  копии» читает последний `run_backup` отсюда) и отметка дневного пинга:
  эксплуатация, не бизнес.
* `document_templates` — реестр шаблонов юр. документов (путь к файлу в образе
  или своём томе); засевается кодом, деловых данных в нём нет. Созданные по
  шаблонам документы (`generated_documents`) стираются.
* `app_settings` — ЧАСТИЧНО, только технические ключи:
  - `backfill_done:*` — отметки разовых data-миграций. Стереть их значит
    заново прогнать `ONE_TIME_BACKFILLS` при следующем `tasks.migrate`. На
    пустой базе это ничего не сделало бы, но если бы `tasks.migrate`
    случился ПОСЛЕ переноса истории (а он идёт на каждом `docker compose up`),
    `legacy_paid_confirmed` закрыл бы долг любому перенесённому заказу с
    `paid_at` и без платежей — ровно тот случай, от которого отметки и заведены.
    Держать отметку дешевле, чем помнить порядок.
  - `fx_sync_last_run`, `boss_digest_last_run_at` — отметки прогонов cron
    (мониторинг курса, «дайджест уже отправлен сегодня»).
  Всё остальное в `app_settings` — бизнес: пороги, время дайджеста, выключатели
  (`accounting_enabled`, `delete_requires_boss`…), реквизиты `company_*`. По
  решению владельца стирается; `tasks.migrate` (`seed_app_settings`) засевает
  значения по умолчанию, реквизиты и «Наши карты и счета» владелец вводит заново.

═══════════════════════════════════════════════════════════════════════════
ЧТО СТИРАЕТСЯ
═══════════════════════════════════════════════════════════════════════════
Всё остальное (список `WIPE` ниже, с группами): заказы и заявки, платежи и
разбивка, сдачи, возвраты, долги поставщикам, накладные, остатки, склады,
перемещения, списания и пересчёты, товары, цены и фото, клиенты и поставщики,
кредит-лимиты, контейнеры, техника и сделки, бухгалтерия и «Наши карты и
счета», сверки кассы, обращения и звонки, посты канала, документы, аудит,
ключи идемпотентности, карта переноса `ms_id_map` и мёртвые зеркала МойСклад
(`ms_*`, `notified_shipments`).

* `ms_id_map` стирается ОБЯЗАТЕЛЬНО: по `MIN(migrated_at)` перенос
  справочников отличает «до переноса» от «живой работы», а перенос истории —
  уже перенесённые накладные. Старая карта указывала бы на удалённые строки.
* `idempotency_keys` — ответы, закэшированные под ключ запроса, ссылаются на
  удалённые заказы/платежи: повтор старого запроса получил бы «успех» по
  несуществующей записи.
* `warehouses` — склад тоже деловой справочник (его переименовывают);
  `tasks.migrate` (`seed_warehouses`) засевает «Основной склад» в пустую таблицу.
* Файлы созданных документов в томе `/app/data` (`DOCUMENTS_DIR`) база не
  хранит — их удаление, если нужно, отдельный шаг (см. runbook).

═══════════════════════════════════════════════════════════════════════════
СЧЁТЧИКИ: ЧТО НАЧИНАЕТСЯ ЗАНОВО, А ЧТО НЕТ
═══════════════════════════════════════════════════════════════════════════
* `invoice_counters` стирается → номера накладных, которые видит человек
  (`OUT-2026-0001`), начинаются заново. Этот номер — только текст документа:
  кнопки и ссылки несут id накладной, а перенос истории нумерует свою серию
  `MS-D-*`/`MS-S-*` мимо счётчиков.
* SERIAL-последовательности id (заказы, платежи, заявки, сдачи, возвраты,
  накладные…) НЕ сбрасываются — и это осознанно. id сущности сидит в
  callback_data кнопок уже отправленных Telegram-карточек (`req_ok:<id>`,
  `pay_ok:<id>`, `dep_ok:<id>`, `ret_ok:<id>`, `mdr_ok:<id>`, `unfreeze:<id>`,
  `prn:inv:<id>`…). Карточки в чатах не удалишь, а хендлер ищет сущность по
  id: начни нумерацию с 1 — и нажатие на старую тестовую кнопку «Одобрить»
  одобрило бы НОВУЮ заявку с тем же номером. С продолжением нумерации старая
  кнопка честно отвечает «не найдено». Цена — номер первого нового заказа не
  «#1», а продолжение (косметика).

Код возврата: 0 — dry-run прошёл / сброс выполнен; 1 — отказ проверки или
ошибка (при --apply в базе ничего не изменилось); 2 — не Postgres.
"""

from __future__ import annotations

import argparse
import gzip
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

# Сброс идёт одной транзакцией и печатает план между командами — таймаут
# простоя в транзакции синхронного пула ему не нужен. До импорта
# services.database: пул читает значение при создании.
os.environ.setdefault("PG_IDLE_IN_TX_TIMEOUT_MS", "0")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("reset_business_data")

CONFIRM_FLAG = "--i-understand-this-deletes-everything"
DEFAULT_BACKUP_MAX_AGE_HOURS = 2.0
# Роли, с которыми после сброса можно войти и работать (guest прав не имеет).
LOGIN_ROLES = ("admin", "boss", "manager")
AUDIT_ACTION = "business_data_reset"
# Маркер дампа, в котором есть наша схема: pg_dump пишет `CREATE TABLE public.<имя>`.
BACKUP_SCHEMA_MARKER = b"CREATE TABLE public.user_roles"


# ─── Классификация таблиц ─────────────────────────────────────────────────────
#
# КАЖДАЯ таблица базы обязана быть в одном из списков. Таблица, которой нет
# нигде, — отказ: новую фичу добавили после этого скрипта, и решать, деловые
# у неё данные или системные, должен человек, а не умолчание.

KEEP: dict[str, str] = {
    "user_roles": "сотрудники и роли — решение владельца",
    "user_permissions": "персональные права сотрудников",
    "user_prefs": "личные настройки вида сотрудников",
    "business_connections": "подключение Telegram Business аккаунта менеджера",
    "currency_rates": "курсы ЦБ (техника; нужны переносу истории)",
    "currency_rate_daily": "архив курсов ЦБ (перенос истории берёт курс на дату)",
    "cron_runs": "журнал прогонов cron (панель резервных копий)",
    "ops_monitor_runs": "отметка дневного пинга",
    "document_templates": "реестр шаблонов юр. документов (засевается кодом)",
}

# app_settings чистится построчно: технические ключи остаются.
SETTINGS_TABLE = "app_settings"
KEEP_SETTING_PREFIXES: tuple[str, ...] = ("backfill_done:",)
KEEP_SETTING_KEYS: dict[str, str] = {
    "fx_sync_last_run": "отметка прогона синхронизации курса",
    "boss_digest_last_run_at": "отметка «дайджест за сегодня отправлен»",
}

WIPE: dict[str, str] = {
    # Заказы и заявки
    "orders": "заказы",
    "order_items": "позиции заказов",
    "order_item_products": "привязка позиций к товарам",
    "order_shipment": "отгрузки заказов",
    "order_warehouse": "склад отгрузки заказа",
    "order_photos": "фото к заказам",
    "order_change_log": "журнал правок заказов",
    "shipment_requests": "заявки на отгрузку",
    "credit_limits": "кредит-лимиты клиентов",
    "client_debt_reminders": "напоминания клиентам о долге",
    # Деньги от клиентов
    "payments": "платежи",
    "payment_parts": "разбивка оплаты",
    "payment_part_accounts": "на какую карту/счёт",
    "cash_deposits": "сдачи в кассу",
    "cash_deposit_orders": "распределение сдач по заказам",
    "cash_deposit_parts": "наличные строки в сдачах",
    "cash_deposit_currency": "валюта сдачи",
    "daily_cash_counts": "сверки наличных",
    "returns": "возвраты",
    "return_items": "позиции возвратов",
    "return_receipt": "приход по возврату",
    # Поставщики
    "supplier_invoice_terms": "условия оплаты приходов",
    "supplier_payments": "выплаты поставщикам",
    "supplier_payment_parts": "способ и счёт выплаты",
    # Склад
    "invoices": "накладные (движения)",
    "invoice_items": "позиции накладных",
    "invoice_counters": "нумерация накладных — начнётся заново",
    "stock": "остатки",
    "warehouses": "склады (засеет tasks.migrate)",
    "warehouse_archived": "архив складов",
    "stock_transfers": "перемещения",
    "stock_writeoffs": "списания",
    "stock_counts": "пересчёты склада",
    "stock_count_lines": "строки пересчётов",
    # Каталог и контрагенты
    "products": "товары",
    "product_prices": "цены",
    "product_photos": "фото товаров",
    "counterparties": "клиенты и поставщики",
    # Себестоимость
    "cost_batches": "партии себестоимости",
    "sale_costs": "себестоимость отгрузок",
    # Контейнеры
    "containers": "контейнеры",
    "container_items": "состав контейнеров",
    "container_item_products": "привязка позиций контейнера к товарам",
    "container_item_links": "старая привязка к карточкам МС",
    "container_item_costs": "цены позиций контейнера",
    "container_costing": "курс и валюта контейнера",
    "container_receipt": "приёмка контейнера",
    "container_supply": "старая приёмка через МС",
    "channel_posts": "посты в канал",
    # Техника
    "machines": "техника",
    "machine_hours": "моточасы",
    "machine_photos": "фото техники",
    "machine_deals": "сделки по технике",
    "machine_deal_payments": "графики рассрочки",
    "machine_deal_requests": "заявки на сделки",
    "machine_payment_receipts": "поступления по рассрочке",
    "machine_receipt_methods": "способ поступления",
    "machine_receipt_accounts": "счёт поступления",
    # Бухгалтерия и «Наши карты и счета»
    "acc_accounts": "счета бухгалтерии и «Наши карты и счета»",
    "acc_account_details": "реквизиты счетов",
    "acc_docs": "документы бухгалтерии",
    "acc_entries": "движения по счетам",
    "acc_day_closes": "пересчёты кассы бухгалтерии",
    # Обращения
    "leads": "обращения",
    "lead_events": "события обращений",
    "lead_calls": "звонки",
    "lead_lost": "причины отказа",
    # Документы, аудит, техника запросов
    "generated_documents": "созданные юр. документы",
    "audit_log": "журнал действий (после сброса — одна запись о сбросе)",
    "idempotency_keys": "ответы на повтор запроса — ссылаются на удалённые записи",
    # Перенос из МойСклад и его мёртвые зеркала
    "ms_id_map": "карта переноса МС — обязана быть пустой перед новым переносом",
    "ms_products": "зеркало МС (код не читает)",
    "ms_counterparties": "зеркало МС (код не читает)",
    "ms_stock": "зеркало МС (код не читает)",
    "ms_employees": "зеркало МС (код не читает)",
    "ms_categories": "зеркало МС (код не читает)",
    "ms_snapshot_meta": "зеркало МС (код не читает)",
    "notified_shipments": "дедуп старого поллера отгрузок (код не читает)",
}


def keep_setting(key: str) -> bool:
    return key in KEEP_SETTING_KEYS or any(key.startswith(p) for p in KEEP_SETTING_PREFIXES)


# ─── План ─────────────────────────────────────────────────────────────────────


class ResetRefused(RuntimeError):
    """Сброс нельзя выполнять: проверка не прошла. В базе ничего не менялось."""


@dataclass
class Plan:
    delete_order: list[str]
    counts: dict[str, int]
    keep_counts: dict[str, int]
    settings_keep: list[str]
    settings_delete: list[str]
    missing: list[str] = field(default_factory=list)        # в списках, но нет в базе
    unclassified: list[str] = field(default_factory=list)   # в базе, но нет в списках
    bad_links: list[str] = field(default_factory=list)      # сохраняемая → стираемая
    users: list[dict] = field(default_factory=list)
    other_sessions: list[dict] = field(default_factory=list)

    @property
    def total_rows(self) -> int:
        return sum(self.counts.values()) + len(self.settings_delete)


def _rows(cur, sql: str, params: tuple = ()) -> list[dict]:
    cur.execute(sql, params)
    return [dict(r) for r in cur.fetchall()]


def _tables(cur) -> set[str]:
    return {
        r["table_name"]
        for r in _rows(
            cur,
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = current_schema() AND table_type = 'BASE TABLE'",
        )
    }


def _foreign_keys(cur) -> list[tuple[str, str, str]]:
    """(дочерняя, родительская, имя) по ВСЕМ внешним ключам схемы."""
    return [
        (r["child"], r["parent"], r["name"])
        for r in _rows(
            cur,
            "SELECT c.conrelid::regclass::text AS child, c.confrelid::regclass::text AS parent, "
            "       c.conname AS name "
            "FROM pg_constraint c JOIN pg_namespace n ON n.oid = c.connamespace "
            "WHERE c.contype = 'f' AND n.nspname = current_schema()",
        )
    ]


def delete_order(tables: set[str], fks: list[tuple[str, str, str]]) -> list[str]:
    """Порядок DELETE: каждая таблица — раньше всех, на кого она ссылается.

    Топологическая сортировка (Kahn) по рёбрам «ребёнок → родитель» между
    стираемыми таблицами; среди равных — по имени, чтобы план dry-run и
    боевой прогон совпадали построчно. Самоссылки не мешают (DELETE всей
    таблицы их не нарушает). Цикл между разными таблицами — отказ: такой
    граф одним проходом DELETE не удалить, и молча выбрать порядок нельзя.
    """
    parents: dict[str, set[str]] = {t: set() for t in tables}
    children: dict[str, set[str]] = {t: set() for t in tables}
    for child, parent, _name in fks:
        if child in tables and parent in tables and child != parent:
            parents[child].add(parent)
            children[parent].add(child)
    # Удалять можно таблицу, на которую уже никто из оставшихся не ссылается.
    waiting = {t: len(children[t]) for t in tables}
    ready = sorted(t for t, n in waiting.items() if n == 0)
    out: list[str] = []
    while ready:
        t = ready.pop(0)
        out.append(t)
        for p in parents[t]:
            waiting[p] -= 1
            if waiting[p] == 0:
                ready.append(p)
                ready.sort()
    if len(out) != len(tables):
        stuck = sorted(set(tables) - set(out))
        raise ResetRefused(f"Цикл внешних ключей между таблицами: {', '.join(stuck)}")
    return out


def build_plan(cur) -> Plan:
    present = _tables(cur)
    classified = set(KEEP) | set(WIPE) | {SETTINGS_TABLE}
    unclassified = sorted(present - classified)
    missing = sorted(classified - present)
    wipe = {t for t in WIPE if t in present}
    fks = _foreign_keys(cur)
    bad_links = sorted(
        f"{child} → {parent} ({name})"
        for child, parent, name in fks
        if child not in wipe and parent in wipe
    )
    order = delete_order(wipe, fks)
    counts = {t: _count(cur, t) for t in order}
    keep_counts = {t: _count(cur, t) for t in sorted(KEEP) if t in present}
    keys = (
        [r["key"] for r in _rows(cur, f"SELECT key FROM {SETTINGS_TABLE} ORDER BY key")]
        if SETTINGS_TABLE in present else []
    )
    users = (
        _rows(
            cur,
            "SELECT user_id, full_name, role, deactivated_at FROM user_roles ORDER BY user_id",
        )
        if "user_roles" in present else []
    )
    sessions = _rows(
        cur,
        "SELECT pid, usename, application_name, client_addr::text AS client_addr, state, "
        "       backend_start::text AS since "
        "FROM pg_stat_activity "
        "WHERE datname = current_database() AND pid <> pg_backend_pid() "
        "  AND backend_type = 'client backend' ORDER BY pid",
    )
    return Plan(
        delete_order=order,
        counts=counts,
        keep_counts=keep_counts,
        settings_keep=[k for k in keys if keep_setting(k)],
        settings_delete=[k for k in keys if not keep_setting(k)],
        missing=missing,
        unclassified=unclassified,
        bad_links=bad_links,
        users=users,
        other_sessions=sessions,
    )


def _count(cur, table: str) -> int:
    cur.execute(f"SELECT COUNT(*) AS n FROM {table}")
    return int(cur.fetchone()["n"])


# ─── Проверки ─────────────────────────────────────────────────────────────────


def check_backup(path: str | None, max_age_hours: float, now: float | None = None) -> str:
    """Бэкап годится для отката: существует, не пустой, свежий, читается, наш.

    Возвращает описание для отчёта; иначе `ResetRefused` с причиной. gzip
    читается ЦЕЛИКОМ: обрезанный архив (закончилось место на диске во время
    дампа) открывается и начало читает без ошибок — CRC проверяется только в
    конце потока.
    """
    if not path:
        raise ResetRefused("Нужен --backup <путь к свежему бэкапу> — без него откатывать нечем")
    p = Path(path)
    if not p.is_file():
        raise ResetRefused(f"Бэкап {path} не найден (или это не файл)")
    st = p.stat()
    if st.st_size <= 0:
        raise ResetRefused(f"Бэкап {path} пустой")
    age_h = ((now if now is not None else time.time()) - st.st_mtime) / 3600.0
    if age_h > max_age_hours:
        raise ResetRefused(
            f"Бэкап {path} сделан {age_h:.1f} ч назад — старше {max_age_hours:g} ч. "
            "Снимите свежий (pg-backup.sh) сразу перед сбросом: всё, что записано после "
            "бэкапа, откат потеряет"
        )
    if age_h < -0.1:
        raise ResetRefused(f"Бэкап {path} датирован будущим — часы или файл не те")
    found = False
    tail = b""
    try:
        opener = gzip.open if p.name.endswith(".gz") else open
        with opener(p, "rb") as fh:
            while True:
                chunk = fh.read(1 << 20)
                if not chunk:
                    break
                if not found:
                    found = BACKUP_SCHEMA_MARKER in (tail + chunk)
                    tail = chunk[-len(BACKUP_SCHEMA_MARKER):]
    except (OSError, EOFError) as e:
        raise ResetRefused(f"Бэкап {path} не читается целиком: {e}") from e
    if not found:
        raise ResetRefused(
            f"В бэкапе {path} нет таблиц бота ({BACKUP_SCHEMA_MARKER.decode()}) — это не тот дамп"
        )
    return f"{path} ({st.st_size} байт, {age_h * 60:.0f} мин назад, читается, схема бота есть)"


def refusals(plan: Plan, *, ignore_sessions: bool = False) -> list[str]:
    """Что мешает --apply в текущем состоянии базы (бэкап проверяется отдельно)."""
    out: list[str] = []
    if plan.unclassified:
        out.append(
            "таблицы не классифицированы (добавьте в KEEP или WIPE скрипта): "
            + ", ".join(plan.unclassified)
        )
    if plan.bad_links:
        out.append(
            "сохраняемая таблица ссылается на стираемую: " + "; ".join(plan.bad_links)
        )
    active = [
        u for u in plan.users if not u.get("deactivated_at") and u.get("role") in LOGIN_ROLES
    ]
    if not active:
        out.append(
            "в user_roles нет ни одного активного admin/boss/manager — после сброса "
            "в систему некому войти"
        )
    if plan.other_sessions and not ignore_sessions:
        who = ", ".join(
            f"pid {s['pid']} {s.get('application_name') or '—'} {s.get('client_addr') or 'local'}"
            for s in plan.other_sessions
        )
        out.append(
            f"к базе подключены другие клиенты ({who}) — остановите bot, webapp и cron "
            "(docker compose stop), иначе они пишут посреди сброса"
        )
    return out


# ─── Выполнение ───────────────────────────────────────────────────────────────


def apply_reset(conn, *, backup_note: str, ignore_sessions: bool = False) -> dict:
    """Стереть бизнес-данные ОДНОЙ транзакцией. Возвращает {таблица: удалено}.

    План пересчитывается внутри транзакции после блокировок: счётчики dry-run
    могли устареть, а удаляется ровно то, что видно под замком.
    """
    from services.database import get_cursor, now_str

    cur = get_cursor(conn)
    try:
        cur.execute("SET LOCAL lock_timeout = '15s'")
        pre = build_plan(cur)
        problems = refusals(pre, ignore_sessions=ignore_sessions)
        if problems:
            raise ResetRefused("; ".join(problems))
        locked = [*pre.delete_order, SETTINGS_TABLE, *pre.keep_counts]
        cur.execute(
            "LOCK TABLE " + ", ".join(t for t in locked if t) + " IN ACCESS EXCLUSIVE MODE"
        )
        plan = build_plan(cur)

        deleted: dict[str, int] = {}
        for table in plan.delete_order:
            cur.execute(f"DELETE FROM {table}")
            deleted[table] = max(int(cur.rowcount or 0), 0)
        if plan.settings_delete:
            cur.execute(
                f"DELETE FROM {SETTINGS_TABLE} WHERE key = ANY(%s)", (plan.settings_delete,)
            )
            deleted[SETTINGS_TABLE] = max(int(cur.rowcount or 0), 0)

        leftovers = {t: n for t in plan.delete_order if (n := _count(cur, t))}
        if leftovers:
            raise RuntimeError(f"после DELETE остались строки: {leftovers}")
        changed = {
            t: (n, now_n) for t, n in plan.keep_counts.items() if (now_n := _count(cur, t)) != n
        }
        if changed:
            raise RuntimeError(f"сохраняемые таблицы изменились: {changed}")
        kept_settings = [
            r["key"] for r in _rows(cur, f"SELECT key FROM {SETTINGS_TABLE} ORDER BY key")
        ]
        if kept_settings != plan.settings_keep:
            raise RuntimeError(
                f"app_settings: осталось {kept_settings}, ожидалось {plan.settings_keep}"
            )

        details = json.dumps(
            {
                "backup": backup_note,
                "rows_deleted": sum(deleted.values()),
                "tables": {t: n for t, n in deleted.items() if n},
                "kept": plan.keep_counts,
                "settings_kept": plan.settings_keep,
            },
            ensure_ascii=False,
        )
        cur.execute(
            "INSERT INTO audit_log (user_id, full_name, role, action, details, created_at) "
            "VALUES (0, %s, 'system', %s, %s, %s)",
            ("Сброс перед переносом из МойСклад", AUDIT_ACTION, details, now_str()),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return deleted


# ─── Отчёт ────────────────────────────────────────────────────────────────────

NEXT_STEPS = """\
Дальше — строго по docs/RUNBOOK_MOYSKLAD_RESET.md (сервисы остаются остановленными):
  1. python -m tasks.migrate                                     # схема, склад, настройки по умолчанию
  2. MS_TOKEN=… python -m scripts.migrate_from_moysklad --dry-run  → --apply   # товары, клиенты, остатки, цены
  3. MS_TOKEN=… python -m scripts.migrate_history_from_moysklad --dry-run
     MS_TOKEN=… python -m scripts.migrate_history_from_moysklad --apply --supplier-history <ledger|settled>
  4. python -m scripts.apply_constraints                          → --apply
  5. python -m tasks.run_fx_sync
  6. docker compose up -d, проверки из runbook; владелец вводит реквизиты и «Наши карты и счета»."""


def print_plan(plan: Plan) -> None:
    logger.info("══ Порядок удаления (дети раньше родителей), строк ══")
    width = max((len(t) for t in plan.delete_order), default=10)
    for i, t in enumerate(plan.delete_order, 1):
        logger.info("  %2d. %-*s %7d  — %s", i, width, t, plan.counts[t], WIPE.get(t, ""))
    logger.info(
        "  %2d. %-*s %7d  — ключи: %s",
        len(plan.delete_order) + 1, width, f"{SETTINGS_TABLE} (частично)",
        len(plan.settings_delete), ", ".join(plan.settings_delete) or "—",
    )
    logger.info("══ Сохраняется ══")
    for t, n in plan.keep_counts.items():
        logger.info("  %-*s %7d  — %s", width, t, n, KEEP[t])
    logger.info(
        "  %-*s %7d  — ключи: %s", width, f"{SETTINGS_TABLE} (частично)",
        len(plan.settings_keep), ", ".join(plan.settings_keep) or "—",
    )
    logger.info("══ Сотрудники, которые останутся ══")
    for u in plan.users:
        logger.info(
            "  %s  %s  %s%s", u["user_id"], u.get("role"), u.get("full_name") or "—",
            "  (деактивирован)" if u.get("deactivated_at") else "",
        )
    if plan.missing:
        logger.info("Нет в этой базе (пропускаются): %s", ", ".join(plan.missing))
    logger.info(
        "Итого к удалению: %d строк в %d таблицах + %d ключей настроек",
        sum(plan.counts.values()), len(plan.delete_order), len(plan.settings_delete),
    )


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(
        description="Сброс бизнес-данных перед повторным переносом из МойСклад (разово)"
    )
    p.add_argument("--apply", action="store_true", help="выполнить (по умолчанию — dry-run)")
    p.add_argument(CONFIRM_FLAG, dest="confirmed", action="store_true",
                   help="обязательное подтверждение для --apply")
    p.add_argument("--backup", help="путь к свежему бэкапу (pg_dumpall .sql.gz), обязателен для --apply")
    p.add_argument("--backup-max-age-hours", type=float, default=DEFAULT_BACKUP_MAX_AGE_HOURS,
                   help=f"насколько свежим должен быть бэкап (по умолчанию {DEFAULT_BACKUP_MAX_AGE_HOURS:g} ч)")
    p.add_argument("--ignore-active-connections", action="store_true",
                   help="не отказывать, если к базе подключены другие клиенты (только для репетиции)")
    args = p.parse_args(argv)

    # Флаги и бэкап — ДО подключения к базе: отказ по ним не должен зависеть
    # от того, доступна ли база и что в ней.
    backup_note = ""
    if args.apply:
        if not args.confirmed:
            logger.error("ОТКАЗ: --apply стирает все бизнес-данные; добавьте %s", CONFIRM_FLAG)
            return 1
        try:
            backup_note = check_backup(args.backup, args.backup_max_age_hours)
        except ResetRefused as e:
            logger.error("ОТКАЗ: %s", e)
            return 1

    from services import database as db

    if not db.USE_POSTGRES:
        logger.error("Нужен Postgres (DATABASE_URL): сбрасывать SQLite незачем")
        return 2

    started = time.monotonic()
    try:
        with db.get_conn() as conn:
            plan = build_plan(db.get_cursor(conn))
            conn.rollback()
    except ResetRefused as e:
        logger.error("ОТКАЗ: %s", e)
        return 1
    except Exception:
        logger.exception("База недоступна или схема неожиданная")
        return 1

    print_plan(plan)
    problems = refusals(plan, ignore_sessions=args.ignore_active_connections)

    if not args.apply:
        if args.backup:
            try:
                logger.info("Бэкап: %s", check_backup(args.backup, args.backup_max_age_hours))
            except ResetRefused as e:
                problems.append(str(e))
        for msg in problems:
            logger.warning("--apply сейчас откажет: %s", msg)
        logger.info(
            "dry-run: база не менялась (%.1f с). Выполнить: --apply %s --backup <путь>",
            time.monotonic() - started, CONFIRM_FLAG,
        )
        return 0

    if problems:
        for msg in problems:
            logger.error("ОТКАЗ: %s", msg)
        return 1
    logger.info("Бэкап: %s", backup_note)

    try:
        with db.get_conn() as conn:
            deleted = apply_reset(
                conn, backup_note=backup_note, ignore_sessions=args.ignore_active_connections
            )
    except ResetRefused as e:
        logger.error("ОТКАЗ (база не менялась): %s", e)
        return 1
    except Exception:
        logger.exception("Сброс упал — транзакция откатилась, база в прежнем состоянии")
        return 1

    logger.info(
        "✓ Сброс выполнен за %.1f с: удалено %d строк (таблиц с данными: %d)",
        time.monotonic() - started, sum(deleted.values()), sum(1 for n in deleted.values() if n),
    )
    logger.info("%s", NEXT_STEPS)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
