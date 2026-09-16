"""
Фотографии к заказу — подписанная расписка, накладная, акт передачи и т.п.

Тот же приём, что у фото техники и товаров (`services/machines`,
`services/product_photos`): своего файлового хранилища нет, файл живёт в
Telegram, мы храним только идентификаторы. `file_unique_id` обязателен — он
переживает смену сервера Bot API, в отличие от `tg_file_id`, и по нему же
ловится повторная отправка того же снимка (UNIQUE в схеме).

Удаление: автор — в течение `DELETE_WINDOW_HOURS` после загрузки (иначе
случайно стёртый документ, который уже показали клиенту/бухгалтеру, некому
восстановить), руководство (admin/boss) — всегда. Тот же срок и то же
рассуждение, что у окна правки приёмки контейнера
(`services.containers.EDIT_WINDOW_HOURS`).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from services import adb_core
from services.database import now_str
from utils.helpers import local_now

logger = logging.getLogger(__name__)

DELETE_WINDOW_HOURS = 24


async def add_photo(
    order_id: int, *, tg_file_id: str, file_unique_id: str,
    uploaded_by: int, caption: str | None = None,
) -> dict:
    if not tg_file_id or not file_unique_id:
        return {"ok": False, "error": "Фото не загрузилось — попробуйте ещё раз"}
    if not await adb_core.fetchrow("SELECT id FROM orders WHERE id = $1", order_id):
        return {"ok": False, "error": "Заказ не найден"}
    existing = await adb_core.fetchrow(
        "SELECT id FROM order_photos WHERE order_id = $1 AND file_unique_id = $2",
        order_id, file_unique_id,
    )
    if existing:
        # Переслал тот же снимок второй раз — не ошибка и не дубль в карточке.
        return {"ok": True, "photo_id": int(existing["id"]), "duplicate": True}
    await adb_core.execute(
        "INSERT INTO order_photos (order_id, tg_file_id, file_unique_id, caption, "
        "uploaded_by, uploaded_at) VALUES ($1, $2, $3, $4, $5, $6)",
        order_id, tg_file_id, file_unique_id, caption, uploaded_by, now_str(),
    )
    return {"ok": True, "duplicate": False}


async def list_photos(order_id: int) -> list[dict]:
    rows = await adb_core.fetch(
        "SELECT * FROM order_photos WHERE order_id = $1 ORDER BY id", order_id
    )
    return [dict(r) for r in rows]


async def photos_by_orders(order_ids: list[int]) -> dict[int, list[dict]]:
    """Фото сразу для списка заказов — батчем, как позиции заказа
    (`get_order_items_by_ids`): без него карточка списка дала бы N+1 на
    открытии вкладки «Заказы»."""
    if not order_ids:
        return {}
    unique_ids = list({int(i) for i in order_ids})
    placeholders = ", ".join(f"${i + 1}" for i in range(len(unique_ids)))
    rows = await adb_core.fetch(
        f"SELECT * FROM order_photos WHERE order_id IN ({placeholders}) ORDER BY id",
        *unique_ids,
    )
    out: dict[int, list[dict]] = {}
    for r in rows:
        out.setdefault(int(r["order_id"]), []).append(dict(r))
    return out


def can_delete(photo: dict, *, user_id: int, role: str) -> bool:
    """Автор — пока не истекло окно, руководство — всегда."""
    if role in ("admin", "boss"):
        return True
    if int(photo.get("uploaded_by") or 0) != int(user_id):
        return False
    return _within_delete_window(photo)


def _within_delete_window(photo: dict) -> bool:
    stamp = str(photo.get("uploaded_at") or "")
    try:
        uploaded = datetime.strptime(stamp[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        # Без разборной отметки времени закрывать право на удаление нельзя —
        # иначе одна кривая строка навсегда запирает фото автору.
        return True
    left = (uploaded + timedelta(hours=DELETE_WINDOW_HOURS)) - local_now().replace(tzinfo=None)
    return left.total_seconds() > 0


async def delete_photo(order_id: int, photo_id: int, *, user_id: int, role: str) -> dict:
    """Удаление скоупится заказом — иначе `photo_id` из формы стирает чужой
    снимок."""
    row = await adb_core.fetchrow(
        "SELECT * FROM order_photos WHERE id = $1 AND order_id = $2", photo_id, order_id
    )
    if not row:
        return {"ok": False, "error": "Фото не найдено"}
    photo = dict(row)
    if not can_delete(photo, user_id=user_id, role=role):
        return {
            "ok": False,
            "error": f"Удалить может автор в течение {DELETE_WINDOW_HOURS} ч после "
                     "загрузки, позже — только руководитель",
        }
    rows = await adb_core.execute("DELETE FROM order_photos WHERE id = $1", photo_id)
    return {"ok": True} if rows else {"ok": False, "error": "Фото не найдено"}
