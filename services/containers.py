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
from typing import Any

from services import adb_core
from services.database import USE_POSTGRES, add_audit_log, get_role, now_str

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


async def list_containers(status: str | None = None) -> list[dict]:
    """Список контейнеров. Прибывшие — сверху по дате прибытия, едущие — по ETA:
    и то и другое отвечает на «что ближайшее»."""
    query = "SELECT * FROM containers"
    params: list[Any] = []
    if status:
        params.append(status)
        query += " WHERE status = $1"
    query += " ORDER BY COALESCE(arrived_at, eta_date, created_at) DESC, id DESC"
    return [dict(r) for r in await adb_core.fetch(query, *params)]


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


async def delete_container(container_id: int, *, user_id: int, full_name: str = "") -> dict:
    """Удалить контейнер вместе с составом.

    Прибывший не удаляем: это уже история приёмки, по которой сверяли товар.
    Состав чистим явно — на SQLite внешние ключи по умолчанию выключены, и без
    этого удаление вело бы себя по-разному в тестах и на проде.
    """
    async with adb_core.transaction() as txn:
        row = await txn.fetchrow("SELECT number, status FROM containers WHERE id = $1", container_id)
        if not row:
            return {"ok": False, "error": "Контейнер не найден"}
        if row["status"] == "arrived":
            return {"ok": False, "error": "Прибывший контейнер — это история приёмки"}
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
    if not await adb_core.fetchrow("SELECT id FROM containers WHERE id = $1", container_id):
        return {"ok": False, "error": "Контейнер не найден"}

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

    `arrived_qty IS NULL` — «ещё не считали»: такая позиция не расхождение, и
    подсвечивать её красным нельзя, иначе непроверенный контейнер выглядит как
    полностью недостающий.
    """
    out = []
    for it in items:
        expected = float(it.get("expected_qty") or 0)
        arrived = it.get("arrived_qty")
        arrived_f = None if arrived is None else float(arrived)
        delta = None if arrived_f is None else round(arrived_f - expected, 3)
        out.append({
            **it,
            "expected_qty": expected,
            "arrived_qty": arrived_f,
            "delta": delta,
            "state": (
                "unchecked" if delta is None
                else "match" if delta == 0
                else "short" if delta < 0
                else "extra"
            ),
        })
    return out


def diff_summary(rows: list[dict]) -> dict:
    """Сводка по расхождениям — то, ради чего сверка и делается."""
    return {
        "total": len(rows),
        "unchecked": sum(1 for r in rows if r["state"] == "unchecked"),
        "short": sum(1 for r in rows if r["state"] == "short"),
        "extra": sum(1 for r in rows if r["state"] == "extra"),
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
