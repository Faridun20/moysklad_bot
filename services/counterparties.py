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
