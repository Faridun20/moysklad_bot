"""
Справочник контрагентов — локальный. Заменяет `ms_counterparty` + читающую
часть `snapshot` (`get_counterparties`/`get_counterparty`/`remember_*`).

Решения, определяющие модуль:

* **Ищем и по названию, и по ТЕЛЕФОНУ.** Клиента чаще помнят по номеру, чем по
  тому, как он записан в справочнике («ООО Бахор Савдо» против «Азиз»).
  Сравниваем по цифрам: номер лежит свободным текстом со скобками, дефисами и
  подписями вроде «раб.», и поиск по сырой строке не находит ничего.
* **Заводит ЧЕЛОВЕК кнопкой, а не автоматика.** Каждый написавший — ещё не
  клиент; автосоздание превратило бы справочник в свалку из случайных
  собеседников.
* **Тёзку не заводим.** Второй контрагент с тем же именем разводит заказы
  одного клиента по двум карточкам, а склеить их потом нечем. UNIQUE на имени
  при этом нет: справочник приехал из МойСклад, и запретить то, что там уже
  есть, значит не дать сохранить ни одной правки.

`balance_cents` у контрагента больше нет: это было сальдо взаиморасчётов
МойСклад. «Сколько должен» считает `services.debts`/`receivables` по нашим же
заказам — второй ответ на тот же вопрос расходился бы с первым.
"""

from __future__ import annotations

import logging

from services import adb_core, money
from services.database import USE_POSTGRES, now_str

logger = logging.getLogger(__name__)

_NAME_MAX = 255

# Чистка телефона прямо в SQL: колонки с нормализованным номером в таблице нет
# (инкрементальных миграций в проекте нет), а контрагентов тысячи — полный скан
# с REPLACE здесь дешевле отдельной таблицы ради индекса.
_PHONE_DIGITS = (
    "REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(COALESCE(phone,''), "
    "' ', ''), '-', ''), '(', ''), ')', ''), '+', '')"
)

_COLS = "id, name, type, phone, telegram_id, notes"


def normalize_name(raw: str | None) -> str:
    return " ".join(str(raw or "").split())[:_NAME_MAX]


async def search(query: str | None = None, limit: int = 50) -> list[dict]:
    """Контрагенты по названию или телефону. Пустой запрос — первые `limit`."""
    limit = max(1, min(int(limit or 50), 200))
    text = (query or "").strip()
    digits = "".join(ch for ch in text if ch.isdigit())
    # Порядок — по-русски (ICU на Postgres, см. adb_core.order_by_name), id —
    # развязка тёзок, чтобы LIMIT отдавал одну и ту же выборку.
    order = f"ORDER BY {adb_core.order_by_name('name')}, id"
    name_like = adb_core.name_search_sql("name")
    if text and len(digits) >= 4:
        return await adb_core.fetch(
            f"SELECT {_COLS} FROM counterparties "
            f"WHERE {_PHONE_DIGITS} LIKE $1 OR {name_like} LIKE $2 "
            f"{order} LIMIT $3",
            f"%{digits}%",
            adb_core.name_search_param(text),
            limit,
        )
    if text:
        return await adb_core.fetch(
            f"SELECT {_COLS} FROM counterparties WHERE {name_like} LIKE $1 "
            f"{order} LIMIT $2",
            adb_core.name_search_param(text),
            limit,
        )
    return await adb_core.fetch(f"SELECT {_COLS} FROM counterparties {order} LIMIT $1", limit)


async def get(counterparty_id: int | str | None) -> dict | None:
    """Один контрагент по id. Принимает и строку: `orders.agent_id` — TEXT."""
    if counterparty_id is None or str(counterparty_id).strip() == "":
        return None
    try:
        cid = int(counterparty_id)
    except (TypeError, ValueError):
        # Неконвертируемое значение — это legacy-uuid МойСклад, оставшийся у
        # строки, которую backfill не сматчил. Не «ошибка», а «не найден».
        return None
    return await adb_core.fetchrow(f"SELECT {_COLS} FROM counterparties WHERE id = $1", cid)


async def create(
    name: str, *, phone: str | None = None, cp_type: str = "customer",
    telegram_id: int | None = None,
) -> dict:
    """Завести контрагента. Возвращает `{ok, counterparty_id, name, existed}`.

    `existed=True` — нашли одноимённого и вернули его.
    """
    clean = normalize_name(name)
    if not clean:
        return {"ok": False, "error": "Название контрагента обязательно"}
    if cp_type not in ("customer", "supplier"):
        return {"ok": False, "error": f"Неизвестный тип контрагента: {cp_type}"}

    async with adb_core.transaction() as txn:
        # Проверка тёзки внутри транзакции: карточку заводят кнопкой, а кнопку
        # можно нажать дважды — и без этого в справочнике оказались бы два
        # одинаковых клиента, между которыми разъехались бы заказы.
        # Тёзку ловим замком, а не только SELECT'ом: на Postgres (READ
        # COMMITTED) две одновременные транзакции обе не видят дубля и обе
        # вставляют — проверка внутри транзакции сама по себе гонку не
        # закрывает. Advisory-lock по нормализованному имени сериализует
        # именно тёзок, а не все вставки подряд. UNIQUE на имени по-прежнему
        # нет и быть не должно: справочник приехал из МойСклад с тёзками.
        if USE_POSTGRES:
            await txn.execute(
                "SELECT pg_advisory_xact_lock(hashtext($1))",
                f"counterparty:name:{clean.lower()}",
            )
        dup = await txn.fetchrow(
            "SELECT id, name FROM counterparties WHERE lower(name) = $1", clean.lower()
        )
        if dup:
            return {
                "ok": True,
                "counterparty_id": int(dup["id"]),
                "name": dup["name"],
                "existed": True,
            }
        await txn.execute(
            "INSERT INTO counterparties (name, type, phone, telegram_id, created_at) "
            "VALUES ($1, $2, $3, $4, $5)",
            clean,
            cp_type,
            (phone or "").strip()[:64] or None,
            telegram_id,
            now_str(),
        )
        new_id = await txn.fetchval(
            "SELECT id FROM counterparties WHERE lower(name) = $1 ORDER BY id DESC", clean.lower()
        )
    logger.info("Заведён контрагент #%s «%s»", new_id, clean)
    return {"ok": True, "counterparty_id": int(new_id), "name": clean, "existed": False}


async def set_telegram_id(counterparty_id: int, telegram_id: int | None) -> bool:
    """Привязать контрагента к Telegram-аккаунту (для отправки PDF накладной)."""
    n = await adb_core.execute(
        "UPDATE counterparties SET telegram_id = $1 WHERE id = $2",
        telegram_id,
        int(counterparty_id),
    )
    return n > 0


async def export_rows() -> list[dict]:
    """Контрагенты с оборотом и текущим долгом — для Excel-выгрузки (B6).

    Оборот — сумма позиций по НЕ черновым/отменённым заказам, сгруппированная
    по валюте (валюты не складываем молча, как и остальные денежные сводки
    проекта). Долг — остаток открытых долгов (`services.debts`, тот же расчёт,
    что в «Долгах» и утренней напоминалке), тоже по валютам. Контрагент без
    заказов и без долга в выборку не попадает — экспортировать пустые строки
    незачем.
    """
    from services.database import get_open_debts
    from services.debts import calc_order_balances

    purchases = await adb_core.fetch(
        "SELECT o.agent_id AS agent_id, o.currency AS currency, "
        "SUM(oi.price_cents * oi.quantity) AS total_cents, "
        "COUNT(DISTINCT o.id) AS orders_count "
        "FROM orders o JOIN order_items oi ON oi.order_id = o.id "
        "WHERE o.agent_id IS NOT NULL AND o.status NOT IN ('draft', 'cancelled') "
        "GROUP BY o.agent_id, o.currency"
    )
    purchases_by_agent: dict[str, list[dict]] = {}
    orders_count_by_agent: dict[str, int] = {}
    for r in purchases:
        aid = str(r["agent_id"])
        purchases_by_agent.setdefault(aid, []).append(
            {"currency": r["currency"], "total": float(money.from_cents(int(r["total_cents"] or 0)))}
        )
        orders_count_by_agent[aid] = orders_count_by_agent.get(aid, 0) + int(r["orders_count"] or 0)

    debts = await get_open_debts()
    debt_ids = [d["id"] for d in debts]
    balances = await calc_order_balances(debt_ids) if debt_ids else {}
    debt_by_agent: dict[str, dict[str, int]] = {}
    for d in debts:
        bal = balances.get(d["id"])
        if bal is None or bal.remaining_cents <= 0:
            continue
        aid = str(d.get("agent_id") or "").strip()
        if not aid:
            continue
        cur = bal.currency or d.get("currency") or "USD"
        debt_by_agent.setdefault(aid, {})
        debt_by_agent[aid][cur] = debt_by_agent[aid].get(cur, 0) + bal.remaining_cents

    ids: set[int] = set()
    for aid in set(purchases_by_agent) | set(debt_by_agent):
        try:
            ids.add(int(aid))
        except ValueError:
            continue
    if not ids:
        return []
    id_list = sorted(ids)
    placeholders = ",".join(f"${i + 1}" for i in range(len(id_list)))
    cps = await adb_core.fetch(
        f"SELECT id, name, phone FROM counterparties WHERE id IN ({placeholders})", *id_list
    )

    rows = []
    for cp in cps:
        aid = str(cp["id"])
        rows.append(
            {
                "counterparty_id": int(cp["id"]),
                "name": cp["name"],
                "phone": cp.get("phone"),
                "orders_count": orders_count_by_agent.get(aid, 0),
                "purchases": purchases_by_agent.get(aid, []),
                "debts": [
                    {"currency": cur, "total": float(money.from_cents(cents))}
                    for cur, cents in (debt_by_agent.get(aid, {})).items()
                ],
            }
        )
    rows.sort(key=lambda r: str(r["name"] or "").casefold())
    return rows


async def get_telegram_ids(counterparty_ids: list[int]) -> dict[int, int]:
    """Batch: {id: telegram_id} для контрагентов с привязанным аккаунтом.

    Контрагент без telegram_id или с неконвертируемым id в результат не
    попадает (B3 — напоминание о долге шлём только туда, где есть кому).
    """
    ids_set: set[int] = set()
    for i in counterparty_ids:
        try:
            ids_set.add(int(i))
        except (TypeError, ValueError):
            continue  # legacy МойСклад uuid, не сматченный backfill'ом
    ids = sorted(ids_set)
    if not ids:
        return {}
    placeholders = ",".join(f"${i + 1}" for i in range(len(ids)))
    rows = await adb_core.fetch(
        f"SELECT id, telegram_id FROM counterparties WHERE id IN ({placeholders}) "
        "AND telegram_id IS NOT NULL",
        *ids,
    )
    return {int(r["id"]): int(r["telegram_id"]) for r in rows}


# ─── Список покупателей («Клиенты» в WebApp) ─────────────────────────────────
#
# Жалоба владельца: «Я нигде не нашёл, где можно посмотреть клиентов. Сколько
# отдано, когда была проведена отгрузка, на какую общую сумму он покупал». Все
# три цифры в базе были — но добраться до них можно было только через лупу,
# зная имя наизусть: раздел «Клиенты» был про ЛИДОВ (воронка, обращения), а
# списка ПОКУПАТЕЛЕЙ не было нигде.
#
# Поэтому здесь — одна строка на покупателя: сколько купил за всё время
# (по валютам, складывать USD и UZS нельзя), сколько должен сейчас и когда
# отгружали в последний раз.
#
# Ни одного запроса на клиента: справочник, покупки и долги считаются тремя
# групповыми выборками (`get_credit_overview` внутри себя — ещё несколько), а
# склейка идёт в Python. N+1 здесь означал бы сотни запросов на открытие
# первого же экрана раздела.

# Потолок скана справочника. Считаем ПОСЛЕ агрегации (сортировка по долгу и
# дате отгрузки, а не по имени), поэтому взять «первые N по алфавиту» нельзя —
# берём всех и режем уже отсортированных. Тысячи строк по три колонки дешевле
# одного лишнего round-trip, но неограниченного скана в коде быть не должно.
_BUYERS_SCAN_MAX = 5000


def _buyers_sort_key_name(row: dict) -> str:
    return str(row.get("name") or "").casefold()


async def buyers_list(query: str | None = None, limit: int = 100) -> dict:
    """Покупатели с итогами: `{clients, total, shown, base_currency}`.

    `query` — имя или телефон (та же пара условий, что в `search`).
    Порядок: сначала должники (кто больше должен — выше), за ними остальные по
    дате последней отгрузки. Это ответ на «с кем разбираться»: список, ровный
    по алфавиту, заставлял бы искать должника глазами, а он и есть причина, по
    которой карточку открывают.

    Контрагент без отгрузок и без долга из списка НЕ выпадает — он просто в
    конце. Справочник и есть список клиентов, и «его тут нет» читалось бы как
    «его не завели».
    """
    from services.database import convert_to_base, get_credit_overview

    limit = max(1, min(int(limit or 100), 500))
    text = (query or "").strip()
    digits = "".join(ch for ch in text if ch.isdigit())
    name_like = adb_core.name_search_sql("name")
    args: list = []
    cond = ""
    if text and len(digits) >= 4:
        args += [f"%{digits}%", adb_core.name_search_param(text)]
        cond = f" AND ({_PHONE_DIGITS} LIKE $1 OR {name_like} LIKE $2)"
    elif text:
        args.append(adb_core.name_search_param(text))
        cond = f" AND {name_like} LIKE $1"
    args.append(_BUYERS_SCAN_MAX)
    # Тип берём НЕ фильтром SQL: поставщик, которому однажды продали, — тоже
    # покупатель, и отсутствие его в поиске читалось бы как «его не завели».
    # Строка остаётся, если это `customer` ИЛИ на него есть заказы (проверка
    # ниже, когда известен overview).
    cp_rows = await adb_core.fetch(
        f"SELECT id, name, phone, type FROM counterparties WHERE 1 = 1{cond} "
        f"ORDER BY id LIMIT ${len(args)}",
        *args,
    )

    # Покупки — один GROUP BY по всем расходным накладным (списания не в счёт:
    # это не продажа). Валюты не складываем.
    from services.warehouse import not_writeoff_sql

    buys = await adb_core.fetch(
        "SELECT i.counterparty_id AS cid, i.currency AS currency, "
        "SUM(i.total_amount_cents) AS sum_cents, COUNT(*) AS cnt, "
        "MAX(i.invoice_date) AS last_date "
        "FROM invoices i WHERE i.type = 'outgoing' AND i.status = 'confirmed' "
        f"AND i.counterparty_id IS NOT NULL AND {not_writeoff_sql('i')} "
        "GROUP BY i.counterparty_id, i.currency"
    )
    bought: dict[str, dict] = {}
    for b in buys:
        cid = str(b["cid"])
        d = bought.setdefault(cid, {"by_currency": {}, "count": 0, "last": ""})
        cur = (b["currency"] or "").upper() or "USD"
        d["by_currency"][cur] = d["by_currency"].get(cur, 0) + int(b["sum_cents"] or 0)
        d["count"] += int(b["cnt"] or 0)
        d["last"] = max(d["last"], str(b["last_date"] or ""))

    # Долг — та же батч-формула, что у `/api/debts` и карточки клиента
    # (`get_agent_current_debt`): второго ответа на «сколько должен» быть не
    # должно, иначе список и карточка разойдутся на копейку и доверия не будет.
    overview = {str(r["agent_id"]): r for r in await get_credit_overview()}

    from config import BASE_CURRENCY

    base_cur = (BASE_CURRENCY or "USD").upper()

    def _row(agent_id: str, name: str, phone: str) -> dict:
        buy = bought.get(agent_id) or {"by_currency": {}, "count": 0, "last": ""}
        ov = overview.get(agent_id) or {}
        by_cur = [
            {"currency": c, "amount_cents": v}
            for c, v in sorted(buy["by_currency"].items(), key=lambda kv: kv[1], reverse=True)
        ]
        base_total = 0.0
        for item in by_cur:
            conv = convert_to_base(float(money.from_cents(item["amount_cents"])), item["currency"])
            if conv is not None:
                base_total += conv
        return {
            "agent_id": agent_id,
            "name": name,
            "phone": phone or "",
            "bought_by_currency": by_cur,
            "bought_base": round(base_total, 2),
            "shipments": buy["count"],
            "last_shipment": buy["last"] or None,
            "debt": float(ov.get("debt") or 0.0),
            "debt_by_currency": ov.get("debt_by_currency") or [],
            "limit": float(ov.get("limit") or 0.0),
            "over_limit": bool(ov.get("over_limit")),
        }

    out = [
        _row(str(c["id"]), c["name"] or "—", c.get("phone") or "")
        for c in cp_rows
        if (c.get("type") or "customer") == "customer" or str(c["id"]) in overview
    ]
    # Агент с заказами, которого нет в справочнике как `customer` (старый
    # uuid МойСклад, контрагент с типом supplier, которому всё же продали):
    # имя берём из заказа. Пропустить его значило бы спрятать живой долг.
    known = {r["agent_id"] for r in out}
    if not text:
        for aid, ov in overview.items():
            if aid not in known:
                out.append(_row(aid, str(ov.get("agent_name") or aid), ""))

    # Стабильные сортировки от младшего ключа к старшему: имя → дата отгрузки →
    # долг. Так «сначала должники» не ломает порядок внутри групп.
    out.sort(key=_buyers_sort_key_name)
    out.sort(key=lambda r: str(r["last_shipment"] or ""), reverse=True)
    out.sort(key=lambda r: (0 if r["debt"] > 0 else 1, -r["debt"]))
    return {
        "clients": out[:limit],
        "total": len(out),
        "shown": min(len(out), limit),
        "base_currency": base_cur,
    }
