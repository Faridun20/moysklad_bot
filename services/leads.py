"""
Воронка клиентов: кто написал, кому ответили, кто купил.

Данные приходят из Telegram Business — бот подключён к личному аккаунту
менеджера и ВИДИТ переписку, но не участвует в ней. Он не отвечает, не
подсказывает и ничего не отправляет от имени менеджера; его задача — считать
состояния, чтобы было видно, сколько обращений превращается в сделки.

Правила, вынесенные в этот слой намеренно:

* **Текстов сообщений мы не храним.** Для «сколько написали, сколько купили,
  кто просто спросил, кто вернулся через месяц» достаточно «кто, когда, в какую
  сторону». Хранить переписку клиентов — ответственность без выгоды, и однажды
  за неё придётся отвечать.
* **Лид — это человек, а не переписка.** Один ряд на `tg_user_id`: если клиент
  написал двум менеджерам, «сколько клиентов обратилось» не должно удвоиться.
  `manager_id` — менеджер последнего контакта.
* **Состояния считаем, а не проставляем руками.** «Ответили», «висит без
  ответа», «спросил и пропал», «вернулся» выводятся из отметок времени. Руками
  ставится только исход сделки, которого в переписке не видно.
* **Возврат клиента фиксируем событием в момент записи.** Он определяется
  паузой между сообщениями, а восстановить её задним числом по агрегатам уже
  нельзя.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from services import adb_core
from services.database import USE_POSTGRES, add_audit_log, get_role, now_str
from utils.helpers import local_now

logger = logging.getLogger(__name__)

STATUSES = ("new", "won", "lost")

STATUS_LABELS = {
    "new": "🆕 В работе",
    "won": "✅ Купил",
    "lost": "🚫 Не купил",
}

# Событие воронки. `reengaged` пишется только в момент нового обращения после
# паузы — задним числом её из агрегатов не восстановить.
EVENT_KINDS = ("inbound", "outbound", "reengaged", "won", "lost", "linked")

# Пауза, после которой новое сообщение считается ВОЗВРАТОМ, а не продолжением
# разговора. Две недели: внутри этого срока переписка про одну покупку — обычное
# дело, дальше это уже новое обращение.
REENGAGE_AFTER_DAYS = 14

# Сколько часов без ответа считаем «висит». Полсуток: письмо утром должно быть
# отвечено в тот же день.
NO_REPLY_HOURS = 12


def _parse(stamp: str | None) -> datetime | None:
    if not stamp:
        return None
    try:
        return datetime.strptime(str(stamp)[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


# ─── Подключение аккаунта менеджера ──────────────────────────────────────────


async def save_connection(
    connection_id: str, *, manager_id: int, user_chat_id: int | None,
    is_enabled: bool, can_read: bool,
) -> dict:
    """Запомнить подключение бота к аккаунту менеджера.

    Без этой записи `business_connection_id` из апдейта — просто строка: сам
    апдейт сообщения не говорит, чей это аккаунт.
    """
    stamp = now_str()
    existing = await adb_core.fetchrow(
        "SELECT connection_id FROM business_connections WHERE connection_id = $1", connection_id
    )
    if existing:
        await adb_core.execute(
            "UPDATE business_connections SET manager_id = $1, user_chat_id = $2, "
            "is_enabled = $3, can_read = $4, updated_at = $5 WHERE connection_id = $6",
            manager_id, user_chat_id, int(is_enabled), int(can_read), stamp, connection_id,
        )
    else:
        await adb_core.execute(
            "INSERT INTO business_connections (connection_id, manager_id, user_chat_id, "
            "is_enabled, can_read, connected_at, updated_at) VALUES ($1, $2, $3, $4, $5, $6, $6)",
            connection_id, manager_id, user_chat_id, int(is_enabled), int(can_read), stamp,
        )
    return {"ok": True}


async def get_connection(connection_id: str) -> dict | None:
    row = await adb_core.fetchrow(
        "SELECT * FROM business_connections WHERE connection_id = $1", connection_id
    )
    return dict(row) if row else None


async def list_connections() -> list[dict]:
    rows = await adb_core.fetch(
        "SELECT * FROM business_connections ORDER BY connected_at DESC"
    )
    return [dict(r) for r in rows]


# ─── Запись активности ───────────────────────────────────────────────────────


async def record_message(
    *, tg_user_id: int, manager_id: int, inbound: bool,
    username: str | None = None, display_name: str | None = None,
    at: str | None = None,
) -> dict:
    """Записать факт сообщения. Текст сюда не передаётся и не принимается.

    Возвращает `{lead_id, reengaged}` — `reengaged` значит «клиент вернулся
    после паузы», и это событие уже записано.
    """
    stamp = at or now_str()
    lead = await adb_core.fetchrow("SELECT * FROM leads WHERE tg_user_id = $1", tg_user_id)

    if lead is None:
        sql = (
            "INSERT INTO leads (tg_user_id, manager_id, username, display_name, status, "
            "first_seen_at, last_inbound_at, last_outbound_at, first_reply_at, "
            "created_at, updated_at) VALUES ($1, $2, $3, $4, 'new', $5, $6, $7, $8, $5, $5)"
        )
        values = (
            tg_user_id, manager_id, username, display_name, stamp,
            stamp if inbound else None,
            None if inbound else stamp,
            None,
        )
        async with adb_core.transaction() as txn:
            if USE_POSTGRES:
                lead_id = await txn.fetchval(sql + " RETURNING id", *values)
            else:
                await txn.execute(sql, *values)
                lead_id = await txn.fetchval("SELECT last_insert_rowid()")
        await _event(int(lead_id), "inbound" if inbound else "outbound", manager_id, stamp)
        return {"lead_id": int(lead_id), "reengaged": False, "created": True}

    lead_id = int(lead["id"])
    reengaged = False
    if inbound:
        # Возврат считаем от ЛЮБОЙ последней активности, а не только от входящих:
        # если менеджер написал вчера, сегодняшний ответ клиента — продолжение
        # разговора, а не новое обращение.
        last = max(
            filter(None, [_parse(lead["last_inbound_at"]), _parse(lead["last_outbound_at"])]),
            default=None,
        )
        now = _parse(stamp) or local_now().replace(tzinfo=None)
        reengaged = bool(last and (now - last) >= timedelta(days=REENGAGE_AFTER_DAYS))

    fields: dict[str, Any] = {
        "manager_id": manager_id,
        "updated_at": stamp,
    }
    if username:
        fields["username"] = username
    if display_name:
        fields["display_name"] = display_name
    if inbound:
        fields["last_inbound_at"] = stamp
    else:
        fields["last_outbound_at"] = stamp
        if not lead["first_reply_at"] and lead["last_inbound_at"]:
            # Первый ответ засчитываем только после обращения клиента: иначе
            # холодная рассылка выглядела бы как мгновенная реакция.
            fields["first_reply_at"] = stamp

    keys = sorted(fields)
    assignments = ", ".join(f"{k} = ${i + 1}" for i, k in enumerate(keys))
    await adb_core.execute(
        f"UPDATE leads SET {assignments} WHERE id = ${len(keys) + 1}",
        *[fields[k] for k in keys], lead_id,
    )
    await _event(lead_id, "inbound" if inbound else "outbound", manager_id, stamp)
    if reengaged:
        await _event(lead_id, "reengaged", manager_id, stamp)
    return {"lead_id": lead_id, "reengaged": reengaged, "created": False}


async def _event(lead_id: int, kind: str, manager_id: int | None, at: str) -> None:
    await adb_core.execute(
        "INSERT INTO lead_events (lead_id, kind, manager_id, at, created_at) "
        "VALUES ($1, $2, $3, $4, $5)",
        lead_id, kind, manager_id, at, now_str(),
    )


# ─── Состояния и чтение ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class LeadState:
    """Производные признаки лида. Ни один из них не хранится — все выводятся из
    отметок времени, поэтому не могут разойтись с фактами."""

    replied: bool          # менеджер вообще отвечал
    awaiting_reply: bool   # последнее слово за клиентом и висит дольше порога
    silent: bool           # написал и пропал: ответили, а он больше не пишет
    never_answered: bool   # обратился, и ему ни разу не ответили


def lead_state(lead: dict, now: datetime | None = None) -> LeadState:
    now = now or local_now().replace(tzinfo=None)
    inbound = _parse(lead.get("last_inbound_at"))
    outbound = _parse(lead.get("last_outbound_at"))
    replied = bool(lead.get("first_reply_at")) or bool(outbound and inbound)
    awaiting = bool(
        inbound and (not outbound or outbound < inbound)
        and (now - inbound) >= timedelta(hours=NO_REPLY_HOURS)
    )
    return LeadState(
        replied=replied,
        awaiting_reply=awaiting,
        silent=bool(inbound and outbound and outbound > inbound
                    and (now - outbound) >= timedelta(days=REENGAGE_AFTER_DAYS)),
        never_answered=bool(inbound and not outbound),
    )


def visible_lead(lead: dict, now: datetime | None = None) -> dict:
    data = dict(lead)
    state = lead_state(data, now)
    data["state"] = {
        "replied": state.replied,
        "awaiting_reply": state.awaiting_reply,
        "silent": state.silent,
        "never_answered": state.never_answered,
    }
    return data


# Отборы по состоянию разговора. Исход (`status`) и состояние — разные вопросы:
# «купил ли» и «на ком сейчас ход». Смешивать их в один фильтр значит требовать
# от человека держать в голове, какое из двух он сейчас выбирает.
STATE_FILTERS = ("awaiting_reply", "silent", "never_answered")


async def list_leads(
    *,
    manager_id: int | None = None,
    status: str | None = None,
    state: str | None = None,
    limit: int = 100,
) -> list[dict]:
    """Список лидов. `state` — один из `STATE_FILTERS`, отбор по состоянию.

    Состояние считается в Python над теми же строками (`lead_state`), а не
    условием в SQL: пороги «12 часов» и «две недели» живут в одном месте, и
    второй их экземпляр в запросе разъехался бы с первым при первой правке.
    Из-за этого лимит применяется ПОСЛЕ отбора — иначе фильтр резал бы уже
    урезанную выборку и терял хвост.
    """
    query = "SELECT * FROM leads"
    params: list[Any] = []
    where = []
    if manager_id is not None:
        params.append(manager_id)
        where.append(f"manager_id = ${len(params)}")
    if status:
        params.append(status)
        where.append(f"status = ${len(params)}")
    if where:
        query += " WHERE " + " AND ".join(where)
    # Потолок выборки: при отборе по состоянию берём с запасом, потому что
    # отсев идёт уже в Python.
    params.append(max(limit, 500) if state else limit)
    query += f" ORDER BY COALESCE(last_inbound_at, first_seen_at) DESC LIMIT ${len(params)}"
    now = local_now().replace(tzinfo=None)
    rows = [visible_lead(dict(r), now) for r in await adb_core.fetch(query, *params)]
    if state in STATE_FILTERS:
        rows = [r for r in rows if r["state"].get(state)][:limit]
    return rows


async def get_lead(lead_id: int) -> dict | None:
    row = await adb_core.fetchrow("SELECT * FROM leads WHERE id = $1", lead_id)
    if not row:
        return None
    lead = visible_lead(dict(row))
    events = await adb_core.fetch(
        "SELECT * FROM lead_events WHERE lead_id = $1 ORDER BY at DESC, id DESC LIMIT 50",
        lead_id,
    )
    lead["events"] = [dict(e) for e in events]
    return lead


# ─── Исход и привязка ────────────────────────────────────────────────────────


async def set_status(
    lead_id: int, status: str, *, user_id: int, full_name: str = ""
) -> dict:
    """Отметить исход. Руками — потому что в переписке его не видно: клиент
    может согласиться голосом, а может пропасть без слова."""
    if status not in STATUSES:
        return {"ok": False, "error": f"Статус: {' / '.join(STATUSES)}"}
    row = await adb_core.fetchrow("SELECT status FROM leads WHERE id = $1", lead_id)
    if not row:
        return {"ok": False, "error": "Лид не найден"}
    if row["status"] == status:
        return {"ok": True, "changed": False}
    await adb_core.execute(
        "UPDATE leads SET status = $1, updated_at = $2 WHERE id = $3",
        status, now_str(), lead_id,
    )
    if status in ("won", "lost"):
        await _event(lead_id, status, user_id, now_str())
    role = await _role(user_id)
    await _audit(user_id, full_name, role, "lead_status", f"#{lead_id}: {row['status']} → {status}")
    return {"ok": True, "changed": True}


async def link_agent(
    lead_id: int, agent_ms_id: str | None, *, user_id: int, full_name: str = ""
) -> dict:
    """Связать лид с контрагентом МойСклад.

    Нужно, чтобы «написал» и «купил» встретились: в переписке клиент — это
    Telegram-аккаунт, а в заказах — контрагент, и других общих полей у них нет.
    """
    if not await adb_core.fetchrow("SELECT id FROM leads WHERE id = $1", lead_id):
        return {"ok": False, "error": "Лид не найден"}
    await adb_core.execute(
        "UPDATE leads SET agent_ms_id = $1, updated_at = $2 WHERE id = $3",
        (agent_ms_id or None), now_str(), lead_id,
    )
    await _event(lead_id, "linked", user_id, now_str())
    role = await _role(user_id)
    await _audit(user_id, full_name, role, "lead_linked", f"#{lead_id} → {agent_ms_id or '—'}")
    return {"ok": True}


async def _role(user_id: int) -> str:
    import asyncio

    return await asyncio.to_thread(get_role, user_id)


async def _audit(user_id: int, full_name: str, role: str, action: str, details: str) -> None:
    import asyncio

    await asyncio.to_thread(add_audit_log, user_id, full_name, role, action, details)


# ─── Воронка ─────────────────────────────────────────────────────────────────


async def first_touch_directions(lead_ids: list[int]) -> dict[int, str]:
    """Кто заговорил первым по каждому лиду: `inbound` — клиент, `outbound` — мы.

    Различать обязательно. Лид заводит ЛЮБОЕ первое сообщение, в том числе наше
    собственное: позвонил клиент, менеджер написал ему в Telegram — карточка
    создана исходящим. Считать такого наравне с тем, кто написал сам, значит
    раздувать «обратились» тем сильнее, чем активнее работают по телефону, —
    и занижать конверсию, которая на самом деле не падала.

    Одним запросом на всю выборку: по лиду в цикле это N+1 на каждый отчёт.
    """
    if not lead_ids:
        return {}
    placeholders = ", ".join(f"${i + 1}" for i in range(len(lead_ids)))
    rows = await adb_core.fetch(
        f"SELECT lead_id, kind, at, id FROM lead_events "
        f"WHERE lead_id IN ({placeholders}) AND kind IN ('inbound', 'outbound') "
        f"ORDER BY lead_id, at, id",
        *lead_ids,
    )
    out: dict[int, str] = {}
    for row in rows:
        # Первый ряд на лида и есть первое касание — дальше по этому лиду не смотрим.
        out.setdefault(int(row["lead_id"]), str(row["kind"]))
    return out


def _percentile(values: list[float], share: float) -> float | None:
    """Значение, ниже которого лежит `share` выборки. Без numpy — он тут не нужен."""
    if not values:
        return None
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, round(share * (len(ordered) - 1))))
    return ordered[idx]


def reply_speed(leads: list[dict]) -> dict:
    """Как быстро отвечаем: типичное время и хвост.

    Медиана и p90, а НЕ среднее: один клиент, забытый на три дня, утаскивает
    среднее так, что оно перестаёт описывать хоть один реальный случай.

    Лиды без ответа в расчёт НЕ идут. Считать их нулём нельзя (мгновенный ответ
    там, где ответа не было), бесконечностью — тоже: показатель тогда улучшался
    бы оттого, что человеку так и не ответили. Их место — в отдельном счётчике
    `never_answered`, который уже есть.
    """
    minutes: list[float] = []
    for row in leads:
        first_seen = _parse(row.get("first_seen_at"))
        replied_at = _parse(row.get("first_reply_at"))
        if not first_seen or not replied_at:
            continue
        delta = (replied_at - first_seen).total_seconds() / 60
        if delta < 0:
            # Часы на сервере переводили или строка кривая — молча пропускаем,
            # отрицательное «время ответа» не бывает.
            continue
        minutes.append(delta)
    return {
        "answered": len(minutes),
        "median_minutes": _percentile(minutes, 0.5),
        "p90_minutes": _percentile(minutes, 0.9),
    }


async def funnel(since: str, until: str) -> dict:
    """Воронка за период: обратились → ответили → купили.

    Считаем по ПЕРВОМУ обращению в периоде, а не по всем событиям: иначе клиент,
    который пишет каждый день, выглядел бы как десять обращений, и конверсия
    падала бы от активности, а не от результата.

    `contacted` оставлен как был — это общее число клиентов за период, и старые
    отчёты по нему сравнимы. Разделение на «написал сам» и «написали мы» едет
    ОТДЕЛЬНЫМИ полями: менять смысл существующей цифры значит сломать
    сравнимость с прошлыми месяцами в день выката.
    """
    rows = await adb_core.fetch(
        "SELECT * FROM leads WHERE first_seen_at >= $1 AND first_seen_at <= $2",
        since, f"{until} 23:59:59",
    )
    leads = [dict(r) for r in rows]
    now = local_now().replace(tzinfo=None)
    states = [lead_state(row, now) for row in leads]

    contacted = len(leads)
    replied = sum(1 for s in states if s.replied)
    won = sum(1 for row in leads if row["status"] == "won")
    lost = sum(1 for row in leads if row["status"] == "lost")

    directions = await first_touch_directions([int(r["id"]) for r in leads])
    inbound_leads = [r for r in leads if directions.get(int(r["id"])) == "inbound"]
    outbound_leads = [r for r in leads if directions.get(int(r["id"])) == "outbound"]
    by_direction = {
        "inbound": _direction_block(inbound_leads, now),
        "outbound": _direction_block(outbound_leads, now),
    }

    reengaged_rows = await adb_core.fetch(
        "SELECT DISTINCT lead_id FROM lead_events WHERE kind = 'reengaged' "
        "AND at >= $1 AND at <= $2",
        since, f"{until} 23:59:59",
    )
    reengaged_ids = {int(r["lead_id"]) for r in reengaged_rows}
    won_ids = {int(row["id"]) for row in leads if row["status"] == "won"}

    return {
        "period": {"since": since, "until": until},
        "contacted": contacted,
        "replied": replied,
        "won": won,
        "lost": lost,
        "in_progress": contacted - won - lost,
        # «Спросил и пропал» — обратился, ответа не получил или получил и молчит.
        "never_answered": sum(1 for s in states if s.never_answered),
        "awaiting_reply": sum(1 for s in states if s.awaiting_reply),
        "silent": sum(1 for s in states if s.silent),
        "reengaged": len(reengaged_ids),
        "reengaged_won": len(reengaged_ids & won_ids),
        "reply_rate": round(replied / contacted, 3) if contacted else None,
        "win_rate": round(won / contacted, 3) if contacted else None,
        # Две ступени вместо одной: у написавшего самому интерес уже есть, а
        # того, кому написали мы, ещё надо заинтересовать. Одной конверсией их
        # не описать.
        "by_direction": by_direction,
        "speed": reply_speed(leads),
    }


def _direction_block(leads: list[dict], now: datetime) -> dict:
    """Счётчики по одной половине воронки — тем, кто написал сам, или тем, кому
    написали мы. Форма совпадает с общей, чтобы экран рисовал их одинаково."""
    total = len(leads)
    states = [lead_state(row, now) for row in leads]
    won = sum(1 for row in leads if row["status"] == "won")
    return {
        "contacted": total,
        "replied": sum(1 for s in states if s.replied),
        "won": won,
        "lost": sum(1 for row in leads if row["status"] == "lost"),
        "win_rate": round(won / total, 3) if total else None,
        "speed": reply_speed(leads),
    }


async def by_manager(since: str, until: str) -> list[dict]:
    """Тот же разрез по менеджерам — у кого обращения превращаются в сделки."""
    rows = await adb_core.fetch(
        "SELECT * FROM leads WHERE first_seen_at >= $1 AND first_seen_at <= $2",
        since, f"{until} 23:59:59",
    )
    now = local_now().replace(tzinfo=None)
    grouped: dict[int, list[dict]] = {}
    for row in rows:
        if row["manager_id"] is None:
            continue
        grouped.setdefault(int(row["manager_id"]), []).append(dict(row))

    directions = await first_touch_directions(
        [int(r["id"]) for items in grouped.values() for r in items]
    )
    out = []
    for manager_id, items in grouped.items():
        states = [lead_state(i, now) for i in items]
        contacted = len(items)
        won = sum(1 for i in items if i["status"] == "won")
        out.append({
            "manager_id": manager_id,
            "contacted": contacted,
            "replied": sum(1 for s in states if s.replied),
            "won": won,
            "awaiting_reply": sum(1 for s in states if s.awaiting_reply),
            "win_rate": round(won / contacted, 3) if contacted else None,
            # Сколько из них написали сами: менеджер, который весь месяц звонил
            # и писал первым, и менеджер, к которому пришли, работали по-разному.
            "inbound": sum(1 for i in items if directions.get(int(i["id"])) == "inbound"),
            "speed": reply_speed(items),
        })
    out.sort(key=lambda r: (-r["contacted"], r["manager_id"]))
    return out


async def awaiting_reply(limit: int = 20) -> list[dict]:
    """Кто ждёт ответа дольше порога — единственное, что требует действия
    сегодня."""
    now = local_now().replace(tzinfo=None)
    rows = await adb_core.fetch(
        "SELECT * FROM leads WHERE last_inbound_at IS NOT NULL AND status = 'new' "
        "ORDER BY last_inbound_at LIMIT 200"
    )
    out = [visible_lead(dict(r), now) for r in rows]
    return [r for r in out if r["state"]["awaiting_reply"]][:limit]
