"""
Контейнеры в пути: что едет, что уже здесь и сошёлся ли состав.

Зачем отдельная сущность, а не строка `container_no` в карточке машины: пока
контейнер — это текст в поле, ответить «что сейчас едет» можно только глазами,
а сверить прибывшее с заявленным нельзя вовсе.

Правила, вынесенные в этот слой намеренно:

* **Состав описан двумя числами** — `expected_qty` (заявлено при отправке) и
  `arrived_qty` (проставлено при приёмке). Одно поле «количество» не даёт
  сверки: недостача выглядела бы как правка, а не как расхождение.
* **`arrived_qty IS NULL` — «ещё не считали»**, а не «приехало ноль». Разница
  принципиальна: ноль означает, что позицию искали и не нашли, то есть это
  недостача целиком.
* **Прибытие — переход статуса**, а не просто дата: после него контейнер уходит
  из витрины «в пути», по которой смотрят, чего ждать.
* Номер контейнера нормализуется (upper, без пробелов и дефисов) — он приезжает
  то из накладной, то из мессенджера, и без этого UNIQUE не спасает.

Время пишем `now_str()` — в том же локальном кадре, что и остальные таблицы.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta
from typing import Any

from services import adb_core
from services.database import USE_POSTGRES, add_audit_log, get_role, now_str
from utils.helpers import local_now

logger = logging.getLogger(__name__)

STATUSES = ("in_transit", "arrived")

STATUS_LABELS = {
    "in_transit": "🚢 В пути",
    "arrived": "📦 Прибыл",
}

_NUMBER_SEPARATORS = re.compile(r"[\s\-—–_]+")
_NUMBER_MAX = 32
_NAME_MAX = 200
_UNIT_MAX = 16


def normalize_number(raw: str | None) -> str:
    """Номер контейнера к каноничному виду: upper, без пробелов и дефисов."""
    return _NUMBER_SEPARATORS.sub("", (raw or "")).upper()[:_NUMBER_MAX]


async def _audit(user_id: int, full_name: str, action: str, details: str) -> None:
    role = await asyncio.to_thread(get_role, user_id)
    await asyncio.to_thread(add_audit_log, user_id, full_name, role, action, details)


def _qty(value: Any) -> float | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        return float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return None


# ─── Контейнер ───────────────────────────────────────────────────────────────


async def create_container(
    *,
    number: str,
    created_by: int,
    creator_name: str = "",
    eta_date: str | None = None,
    notes: str | None = None,
) -> dict:
    number_norm = normalize_number(number)
    if not number_norm:
        return {"ok": False, "error": "Номер контейнера обязателен"}
    if await adb_core.fetchrow("SELECT id FROM containers WHERE number = $1", number_norm):
        return {"ok": False, "error": f"Контейнер {number_norm} уже заведён"}

    stamp = now_str()
    sql = (
        "INSERT INTO containers (number, status, eta_date, notes, created_by, "
        "created_at, updated_at) VALUES ($1, $2, $3, $4, $5, $6, $7)"
    )
    values = (number_norm, "in_transit", eta_date, notes, created_by, stamp, stamp)
    async with adb_core.transaction() as txn:
        if USE_POSTGRES:
            container_id = await txn.fetchval(sql + " RETURNING id", *values)
        else:
            await txn.execute(sql, *values)
            container_id = await txn.fetchval("SELECT last_insert_rowid()")

    await _audit(created_by, creator_name, "container_created", number_norm)
    return {"ok": True, "container_id": int(container_id), "number": number_norm}


async def get_container(container_id: int) -> dict | None:
    row = await adb_core.fetchrow("SELECT * FROM containers WHERE id = $1", container_id)
    return dict(row) if row else None


async def list_containers(status: str | None = None, search: str | None = None) -> list[dict]:
    """Список контейнеров со сводкой состава.

    Прибывшие — сверху по дате прибытия, едущие — по ETA: и то и другое отвечает
    на «что ближайшее».

    Сводка расхождений считается ЗДЕСЬ, а не при открытии карточки: иначе
    «сверить контейнеры» значит открыть каждый по очереди, а именно от этого
    раздел и должен избавлять. Один запрос на весь состав, без N+1.
    """
    query = "SELECT * FROM containers"
    params: list[Any] = []
    where = []
    if status:
        params.append(status)
        where.append(f"status = ${len(params)}")
    if (search or "").strip():
        # Ищем и по номеру, и по заметке: номер помнят не всегда, а «запчасти
        # для JCB» помнят.
        params.append(f"%{normalize_number(search)}%")
        params.append(f"%{search.strip().lower()}%")
        where.append(f"(number LIKE ${len(params) - 1} OR lower(notes) LIKE ${len(params)})")
    if where:
        query += " WHERE " + " AND ".join(where)
    query += " ORDER BY COALESCE(arrived_at, eta_date, created_at) DESC, id DESC"
    rows = [dict(r) for r in await adb_core.fetch(query, *params)]
    if not rows:
        return rows

    items = await adb_core.fetch(
        "SELECT container_id, expected_qty, arrived_qty FROM container_items"
    )
    by_container: dict[int, list[dict]] = {}
    for it in items:
        by_container.setdefault(int(it["container_id"]), []).append(dict(it))
    for row in rows:
        row["summary"] = diff_summary(diff(by_container.get(int(row["id"]), [])))
        row["edit_window"] = edit_window(row)
    return rows


async def count_by_status() -> dict[str, int]:
    rows = await adb_core.fetch("SELECT status, COUNT(*) AS n FROM containers GROUP BY status")
    counts = dict.fromkeys(STATUSES, 0)
    for row in rows:
        status = str(row["status"])
        if status in counts:
            counts[status] = int(row["n"])
    counts["all"] = sum(counts[s] for s in STATUSES)
    return counts


async def update_container(
    container_id: int, *, user_id: int, full_name: str = "", **fields: Any
) -> dict:
    allowed = {"eta_date", "notes"}
    unknown = set(fields) - allowed
    if unknown:
        return {"ok": False, "error": f"Нельзя менять поля: {', '.join(sorted(unknown))}"}
    if not fields:
        return {"ok": False, "error": "Нечего менять"}
    keys = sorted(fields)
    assignments = ", ".join(f"{k} = ${i + 1}" for i, k in enumerate(keys))
    params = [fields[k] for k in keys] + [now_str(), container_id]
    rows = await adb_core.execute(
        f"UPDATE containers SET {assignments}, updated_at = ${len(keys) + 1} "
        f"WHERE id = ${len(keys) + 2}",
        *params,
    )
    if not rows:
        return {"ok": False, "error": "Контейнер не найден"}
    await _audit(user_id, full_name, "container_updated", f"#{container_id}: {', '.join(keys)}")
    return {"ok": True}


# Сколько времени приёмку можно править после сохранения сверки. Ошибку в
# количестве замечают в тот же день или на следующее утро; дальше карточка
# закрывается и становится историей, по которой уже приняли решения.
EDIT_WINDOW_HOURS = 24


def edit_window(container: dict | None) -> dict:
    """Можно ли ещё править приёмку и сколько осталось.

    Считаем в Python от `local_now()`: `arrived_at` пишется локальным `now_str()`,
    а `NOW()` в SQL отдаёт UTC — сравнение в разных кадрах молча всегда ложно
    (CLAUDE.md).
    """
    if not container or container.get("status") != "arrived":
        # Пока в пути — правь сколько угодно, ничего ещё не зафиксировано.
        return {"open": True, "closes_at": None, "hours_left": None}
    stamp = str(container.get("arrived_at") or "")
    try:
        arrived = datetime.strptime(stamp[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        # Без разборной отметки времени закрывать нечего — иначе одна кривая
        # строка навсегда запирает карточку.
        return {"open": True, "closes_at": None, "hours_left": None}
    closes = arrived + timedelta(hours=EDIT_WINDOW_HOURS)
    left = (closes - local_now().replace(tzinfo=None)).total_seconds() / 3600
    return {
        "open": left > 0,
        "closes_at": closes.strftime("%Y-%m-%d %H:%M:%S"),
        "hours_left": round(left, 1) if left > 0 else 0,
    }


async def _require_open_window(container_id: int) -> dict | None:
    """Отказ, если окно правки уже закрыто. None — можно менять.

    Один сторож на все операции состава: закрывать карточку в интерфейсе, но
    принимать правки в ручках значит не закрыть её вовсе.
    """
    row = await adb_core.fetchrow(
        "SELECT status, arrived_at FROM containers WHERE id = $1", container_id
    )
    if not row:
        return {"ok": False, "error": "Контейнер не найден"}
    window = edit_window(dict(row))
    if window["open"]:
        return None
    return {
        "ok": False,
        "error": f"Приёмка закрыта — правки принимались {EDIT_WINDOW_HOURS} ч после прибытия",
        "window_closed": True,
    }


async def delete_container(container_id: int, *, user_id: int, full_name: str = "") -> dict:
    """Удалить контейнер вместе с составом.

    Прибывший удаляется, пока открыто окно правки: приёмку могли завести не на
    тот контейнер, и запрет означал бы вечную неверную строку в списке. После
    закрытия окна это история приёмки, по которой уже принимали решения.

    Состав чистим явно — на SQLite внешние ключи по умолчанию выключены, и без
    этого удаление вело бы себя по-разному в тестах и на проде.
    """
    async with adb_core.transaction() as txn:
        row = await txn.fetchrow(
            "SELECT number, status, arrived_at FROM containers WHERE id = $1", container_id
        )
        if not row:
            return {"ok": False, "error": "Контейнер не найден"}
        window = edit_window(dict(row))
        if not window["open"]:
            return {
                "ok": False,
                "error": f"Приёмка закрыта — правки принимались {EDIT_WINDOW_HOURS} ч после прибытия",
            }
        await txn.execute("DELETE FROM container_items WHERE container_id = $1", container_id)
        await txn.execute("DELETE FROM containers WHERE id = $1", container_id)
    await _audit(user_id, full_name, "container_deleted", f"#{container_id} · {row['number']}")
    return {"ok": True}


# ─── Состав ──────────────────────────────────────────────────────────────────


async def add_item(
    container_id: int,
    *,
    name: str,
    expected_qty: float = 0,
    arrived_qty: float | None = None,
    unit: str = "шт",
    note: str | None = None,
) -> dict:
    """Добавить позицию. `arrived_qty` задают, когда позицию нашли в прибывшем
    контейнере, но в заявленном составе её не было (излишек)."""
    clean = (name or "").strip()[:_NAME_MAX]
    if not clean:
        return {"ok": False, "error": "Название позиции обязательно"}
    if expected_qty < 0 or (arrived_qty is not None and arrived_qty < 0):
        return {"ok": False, "error": "Количество не может быть отрицательным"}
    guard = await _require_open_window(container_id)
    if guard:
        return guard

    sql = (
        "INSERT INTO container_items (container_id, name, unit, expected_qty, arrived_qty, "
        "note, created_at) VALUES ($1, $2, $3, $4, $5, $6, $7)"
    )
    values = (
        container_id, clean, (unit or "шт").strip()[:_UNIT_MAX] or "шт",
        float(expected_qty), arrived_qty, note, now_str(),
    )
    async with adb_core.transaction() as txn:
        if USE_POSTGRES:
            item_id = await txn.fetchval(sql + " RETURNING id", *values)
        else:
            await txn.execute(sql, *values)
            item_id = await txn.fetchval("SELECT last_insert_rowid()")
    return {"ok": True, "item_id": int(item_id)}


async def delete_item(container_id: int, item_id: int) -> dict:
    guard = await _require_open_window(container_id)
    if guard:
        return guard
    rows = await adb_core.execute(
        "DELETE FROM container_items WHERE id = $1 AND container_id = $2", item_id, container_id
    )
    return {"ok": bool(rows)} if rows else {"ok": False, "error": "Позиция не найдена"}


async def list_items(container_id: int) -> list[dict]:
    rows = await adb_core.fetch(
        "SELECT * FROM container_items WHERE container_id = $1 ORDER BY id", container_id
    )
    return [dict(r) for r in rows]


def diff(items: list[dict]) -> list[dict]:
    """Состав с расхождениями: сколько заявлено, сколько пришло, что не сошлось.

    Два правила, без которых сверка врёт:

    * `arrived_qty IS NULL` — «ещё не считали»: такая позиция не расхождение, и
      подсвечивать её красным нельзя, иначе непроверенный контейнер выглядит как
      полностью недостающий.
    * `expected_qty == 0` — позицию НЕ ЗАЯВЛЯЛИ, её вписали уже при приёмке.
      Это не излишек: расхождение бывает только с тем, что обещали. Контейнер,
      состав которого вообще не заводили заранее, — это опись прибывшего, а не
      сверка, и красных строк в нём быть не должно.
    """
    out = []
    for it in items:
        expected = float(it.get("expected_qty") or 0)
        arrived = it.get("arrived_qty")
        arrived_f = None if arrived is None else float(arrived)
        delta = None if arrived_f is None else round(arrived_f - expected, 3)
        if delta is None:
            state = "unchecked"
        elif not expected:
            # Не заявляли — значит и не расходится. Пришло 0 при незаявленной
            # позиции — просто пустая строка, тоже не недостача.
            state = "received"
        elif delta == 0:
            state = "match"
        elif delta < 0:
            state = "short"
        else:
            state = "extra"
        out.append({
            **it,
            "expected_qty": expected,
            "arrived_qty": arrived_f,
            "delta": delta,
            "declared": bool(expected),
            "state": state,
        })
    return out


def diff_summary(rows: list[dict]) -> dict:
    """Сводка по расхождениям — то, ради чего сверка и делается."""
    return {
        "total": len(rows),
        "unchecked": sum(1 for r in rows if r["state"] == "unchecked"),
        "short": sum(1 for r in rows if r["state"] == "short"),
        "extra": sum(1 for r in rows if r["state"] == "extra"),
        "received": sum(1 for r in rows if r["state"] == "received"),
        "mismatch": sum(1 for r in rows if r["state"] in ("short", "extra")),
    }


async def set_arrived_quantities(
    container_id: int, quantities: dict[int, Any], *, user_id: int, full_name: str = ""
) -> dict:
    """Проставить фактические количества по позициям приёмки.

    Пустое значение сбрасывает факт в «ещё не считали» — приёмщик должен иметь
    возможность отменить свою же опечатку, а не только записать ноль.
    """
    if not quantities:
        return {"ok": False, "error": "Нечего сохранять"}
    guard = await _require_open_window(container_id)
    if guard:
        return guard
    known = {int(i["id"]) for i in await list_items(container_id)}
    unknown = set(quantities) - known
    if unknown:
        # Позиция из другого контейнера — это подстановка чужого id, а не опечатка.
        return {"ok": False, "error": "Позиция не из этого контейнера"}

    stamp = now_str()
    async with adb_core.transaction() as txn:
        for item_id, raw in quantities.items():
            value = _qty(raw)
            if value is not None and value < 0:
                return {"ok": False, "error": "Количество не может быть отрицательным"}
            await txn.execute(
                "UPDATE container_items SET arrived_qty = $1 WHERE id = $2 AND container_id = $3",
                value, int(item_id), container_id,
            )
        await txn.execute(
            "UPDATE containers SET updated_at = $1 WHERE id = $2", stamp, container_id
        )
    await _audit(
        user_id, full_name, "container_checked", f"#{container_id}: {len(quantities)} позиций"
    )
    return {"ok": True}


async def mark_arrived(container_id: int, *, user_id: int, full_name: str = "") -> dict:
    """Отметить контейнер прибывшим (CAS по статусу).

    После этого он уходит из витрины «в пути»: два человека, отметивших приёмку
    одновременно, не должны получить два разных ответа.
    """
    stamp = now_str()
    rows = await adb_core.execute(
        "UPDATE containers SET status = 'arrived', arrived_at = $1, updated_at = $1 "
        "WHERE id = $2 AND status = 'in_transit'",
        stamp, container_id,
    )
    if not rows:
        current = await adb_core.fetchrow("SELECT status FROM containers WHERE id = $1", container_id)
        if not current:
            return {"ok": False, "error": "Контейнер не найден"}
        return {"ok": False, "error": "Контейнер уже отмечен прибывшим",
                "current": current["status"]}
    await _audit(user_id, full_name, "container_arrived", f"#{container_id}")
    return {"ok": True, "arrived_at": stamp}
