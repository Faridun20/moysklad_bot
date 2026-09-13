"""
Журнал звонков: обращения, которых не видно в переписке.

Наблюдатель (`handlers/business.py`) знает только про Telegram. Клиент, который
позвонил, для воронки не существовал вовсе — а по нему часто и идёт основной
поток: звонок, потом менеджер сам пишет в Telegram и шлёт фото товара.

Правила, вынесённые в этот слой намеренно:

* **Звонок — самостоятельный факт, а не половина лида.** `lead_id` необязателен:
  человека может не быть в Telegram вовсе. Такой звонок живёт в списке «кому
  перезвонить», а не притворяется перепиской.
* **В общий счётчик обращений звонки НЕ идут.** Подмешать их значит сломать
  сравнимость с прошлыми месяцами ровно в день выката: владелец увидит «упала
  конверсия», хотя упало определение знаменателя. Они едут отдельной строкой.
* **Телефон храним и как ввели, и нормализованным.** Первое — чтобы перезвонить,
  второе — чтобы искать: один и тот же номер приходит то `+998 90 123-45-67`,
  то `901234567`, и без ключа поиск не находит ничего.
* **Источник — закрытый короткий список.** Свободный текст неанализируем, а
  анализ и есть цель. Длинный список не заполняют.
* Заметка о звонке — законна: это запись менеджера о СВОЁМ разговоре, а не
  сохранённое сообщение клиента. На переписку это послабление не переносится.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from services import adb_core
from services.database import USE_POSTGRES, now_str

logger = logging.getLogger(__name__)

DIRECTIONS = ("in", "out")

DIRECTION_LABELS = {
    "in": "📞 Звонил клиент",
    "out": "📲 Звонили мы",
}

# Откуда клиент про нас узнал. Не путать со способом связи (звонок или Telegram)
# — это разные вопросы, и смешивать их в одном поле значит не ответить ни на один.
SOURCES = ("channel", "referral", "ads", "repeat", "other")

SOURCE_LABELS = {
    "channel": "Наш канал",
    "referral": "Посоветовали",
    "ads": "Реклама",
    "repeat": "Уже покупал",
    "other": "Другое",
}

_PHONE_TAIL = 9  # столько цифр в узбекском номере после кода страны
_NOTE_MAX = 500
_NAME_MAX = 200


def phone_key(raw: str | None) -> str:
    """Номер к сравнимому виду: последние девять цифр.

    Один и тот же номер приходит как `+998 90 123-45-67`, `998901234567` и
    `901234567`. Общее у всех форм — девятизначный хвост, он и есть ключ.
    Коды страны и восьмёрку отбрасываем вместе со всем, что левее хвоста.
    """
    digits = re.sub(r"\D", "", str(raw or ""))
    if not digits:
        return ""
    return digits[-_PHONE_TAIL:]


async def add_call(
    *,
    manager_id: int,
    phone: str | None = None,
    display_name: str | None = None,
    direction: str = "in",
    source: str | None = None,
    interest: str | None = None,
    lead_id: int | None = None,
    note: str | None = None,
    at: str | None = None,
) -> dict:
    """Записать звонок. Ничего, кроме менеджера, не обязательно.

    Требовать телефон нельзя: половину звонков записывают постфактум, когда
    номер уже не под рукой, а «звонок без номера» — всё ещё обращение.
    Обязательное поле здесь означало бы, что звонки перестанут записывать.
    """
    if direction not in DIRECTIONS:
        return {"ok": False, "error": f"Направление: {' / '.join(DIRECTIONS)}"}
    if source and source not in SOURCES:
        return {"ok": False, "error": f"Источник: {' / '.join(SOURCES)}"}
    if lead_id is not None:
        if not await adb_core.fetchrow("SELECT id FROM leads WHERE id = $1", lead_id):
            return {"ok": False, "error": "Лид не найден"}

    clean_phone = (phone or "").strip()[:64] or None
    stamp = at or now_str()
    sql = (
        "INSERT INTO lead_calls (lead_id, phone, phone_key, display_name, direction, "
        "source, interest, manager_id, at, note, created_at) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)"
    )
    values = (
        lead_id, clean_phone, phone_key(clean_phone),
        (display_name or "").strip()[:_NAME_MAX] or None,
        direction, source, (interest or "").strip()[:_NAME_MAX] or None,
        manager_id, stamp, (note or "").strip()[:_NOTE_MAX] or None, now_str(),
    )
    async with adb_core.transaction() as txn:
        if USE_POSTGRES:
            call_id = await txn.fetchval(sql + " RETURNING id", *values)
        else:
            await txn.execute(sql, *values)
            call_id = await txn.fetchval("SELECT last_insert_rowid()")

    if lead_id is not None:
        # Событие пишем только привязанному: `lead_events.lead_id` NOT NULL, и
        # звонку без лида там места нет — он живёт своей строкой в журнале.
        from services.leads import _event

        await _event(int(lead_id), "call", manager_id, stamp)
    return {"ok": True, "call_id": int(call_id)}


async def link_call(call_id: int, lead_id: int, *, user_id: int) -> dict:
    """Связать записанный звонок с телеграм-лидом.

    Руками, а не по телефону: Telegram номер собеседника не отдаёт, и общего
    поля у звонка с перепиской нет. Угадывать здесь нечего — угадывание
    означало бы чужой звонок в чужой карточке.
    """
    call = await adb_core.fetchrow("SELECT * FROM lead_calls WHERE id = $1", call_id)
    if not call:
        return {"ok": False, "error": "Звонок не найден"}
    if not await adb_core.fetchrow("SELECT id FROM leads WHERE id = $1", lead_id):
        return {"ok": False, "error": "Лид не найден"}

    await adb_core.execute(
        "UPDATE lead_calls SET lead_id = $1 WHERE id = $2", lead_id, call_id
    )
    from services.leads import _event

    await _event(lead_id, "call_linked", user_id, now_str())
    return {"ok": True, "call_id": call_id, "lead_id": lead_id}


async def delete_call(call_id: int) -> dict:
    rows = await adb_core.execute("DELETE FROM lead_calls WHERE id = $1", call_id)
    return {"ok": True} if rows else {"ok": False, "error": "Звонок не найден"}


async def list_calls(
    *, lead_id: int | None = None, unlinked: bool = False, limit: int = 100
) -> list[dict]:
    """Журнал. `unlinked` — только те, кого ещё не нашли в Telegram."""
    query = "SELECT * FROM lead_calls"
    params: list[Any] = []
    where = []
    if lead_id is not None:
        params.append(lead_id)
        where.append(f"lead_id = ${len(params)}")
    elif unlinked:
        where.append("lead_id IS NULL")
    if where:
        query += " WHERE " + " AND ".join(where)
    params.append(limit)
    query += f" ORDER BY at DESC, id DESC LIMIT ${len(params)}"
    return [dict(r) for r in await adb_core.fetch(query, *params)]


async def count_unlinked(since: str, until: str) -> int:
    """Сколько звонков за период так и не нашли своего телеграм-лида.

    Отдельный счётчик, а не слагаемое в «обратились»: звонок и сообщение —
    разные события, и сложить их в одну ступень значит выдумать метрику.
    """
    value = await adb_core.fetchval(
        "SELECT COUNT(*) FROM lead_calls "
        "WHERE lead_id IS NULL AND at >= $1 AND at <= $2",
        since, f"{until} 23:59:59",
    )
    return int(value or 0)


async def sources_breakdown(since: str, until: str) -> list[dict]:
    """Откуда узнали о нас — по звонкам за период.

    Пока это единственный источник таких данных: у телеграм-лида источника нет
    и взяться ему неоткуда, пока его не начнут проставлять руками.
    """
    rows = await adb_core.fetch(
        "SELECT source, COUNT(*) AS n FROM lead_calls "
        "WHERE at >= $1 AND at <= $2 AND source IS NOT NULL "
        "GROUP BY source",
        since, f"{until} 23:59:59",
    )
    out = [
        {"source": str(r["source"]), "label": SOURCE_LABELS.get(str(r["source"]), str(r["source"])),
         "count": int(r["n"])}
        for r in rows
    ]
    out.sort(key=lambda r: (-r["count"], r["source"]))
    return out
