"""
Фотографии товаров каталога — для витрины в канале.

Своего файлового хранилища у проекта нет и не нужно: файл живёт в Telegram, мы
держим идентификаторы. Тот же приём, что у фото техники (`services/machines`),
и по той же причине — файловая система Railway эфемерна.

Почему фото загружают руками, а не тянутся из МойСклад: выгрузка картинок всего
каталога — это сотни запросов в момент, когда бюджет МС ужимается до 3,7 в
секунду. В канал идут десятки позиций, а не весь каталог, и снимки для них
проще принести один раз.

`file_unique_id` обязателен: он переживает смену сервера Bot API, в отличие от
`tg_file_id`, и по нему же ловится повторная отправка того же снимка.
"""

from __future__ import annotations

import logging

from services import adb_core
from services.database import now_str

logger = logging.getLogger(__name__)


async def add_photo(
    ms_id: str, *, tg_file_id: str, file_unique_id: str,
    uploaded_by: int, caption: str | None = None,
) -> dict:
    if not (ms_id or "").strip():
        return {"ok": False, "error": "Не указан товар"}
    if not tg_file_id or not file_unique_id:
        return {"ok": False, "error": "Нужны tg_file_id и file_unique_id"}

    existing = await adb_core.fetchrow(
        "SELECT id FROM product_photos WHERE ms_id = $1 AND file_unique_id = $2",
        ms_id, file_unique_id,
    )
    if existing:
        # Переслал тот же снимок второй раз — не ошибка и не дубль в карточке.
        return {"ok": True, "photo_id": int(existing["id"]), "duplicate": True}
    await adb_core.execute(
        "INSERT INTO product_photos (ms_id, tg_file_id, file_unique_id, caption, "
        "uploaded_by, uploaded_at) VALUES ($1, $2, $3, $4, $5, $6)",
        ms_id, tg_file_id, file_unique_id, caption, uploaded_by, now_str(),
    )
    return {"ok": True, "duplicate": False}


async def list_photos(ms_id: str) -> list[dict]:
    rows = await adb_core.fetch(
        "SELECT * FROM product_photos WHERE ms_id = $1 ORDER BY id", ms_id
    )
    return [dict(r) for r in rows]


async def first_photo(ms_id: str) -> dict | None:
    rows = await list_photos(ms_id)
    return rows[0] if rows else None


async def delete_photo(ms_id: str, photo_id: int) -> dict:
    """Удаление скоупится товаром — иначе `photo_id` из формы стирает чужой
    снимок."""
    rows = await adb_core.execute(
        "DELETE FROM product_photos WHERE id = $1 AND ms_id = $2", photo_id, ms_id
    )
    return {"ok": True} if rows else {"ok": False, "error": "Фото не найдено"}


async def photos_by_products(ms_ids: list[str]) -> dict[str, int]:
    """Сколько фото у каждого товара — батчем, для списка каталога.

    Без этого экран выбора товара для витрины дал бы N+1 на первом же
    открытии.
    """
    if not ms_ids:
        return {}
    placeholders = ", ".join(f"${i + 1}" for i in range(len(ms_ids)))
    rows = await adb_core.fetch(
        f"SELECT ms_id, COUNT(*) AS n FROM product_photos "
        f"WHERE ms_id IN ({placeholders}) GROUP BY ms_id",
        *ms_ids,
    )
    return {str(r["ms_id"]): int(r["n"]) for r in rows}
