"""
Заведение контрагента в МойСклад из карточки клиента.

Зачем отдельно: до сих пор контрагенты приезжали только синхронизацией — их
заводили руками в МойСклад, а бот лишь читал справочник. Из-за этого клиент,
написавший в Telegram, оставался «лидом» и никогда не встречался со своими
заказами: в переписке он Telegram-аккаунт, в заказах — контрагент, и общих полей
у них нет.

Решения, определяющие модуль:

* **Заводит ЧЕЛОВЕК кнопкой, а не автоматика.** Каждый написавший — не клиент;
  автосоздание превратило бы справочник в свалку из случайных собеседников.
* **Дубликат ловим по снапшоту** и кладём свежесозданного туда же сразу: иначе
  до ночной синхронизации того же клиента завели бы второй раз, и заказы
  разъехались бы по двум карточкам.
* **Телефон необязателен.** Telegram номер собеседника не отдаёт, и требовать
  его значит не дать завести контрагента вовсе.
"""

from __future__ import annotations

import json
import logging

from services import snapshot
from services.moysklad import MS_BASE, get_session, redact_ms_error

logger = logging.getLogger(__name__)

_NAME_MAX = 255


def normalize_name(raw: str | None) -> str:
    return " ".join(str(raw or "").split())[:_NAME_MAX]


async def create_counterparty(name: str, *, phone: str | None = None) -> dict:
    """Завести контрагента. Возвращает `{ok, ms_id, name, existed}`.

    `existed=True` — нашли одноимённого в снапшоте и вернули его: заводить
    второго с тем же именем значит развести заказы одного клиента по двум
    карточкам, а склеить их потом нечем.
    """
    clean = normalize_name(name)
    if not clean:
        return {"ok": False, "error": "Название контрагента обязательно"}

    exact = [
        c for c in snapshot.get_counterparties(clean, limit=25)
        if normalize_name(c.get("name")).casefold() == clean.casefold()
    ]
    if exact:
        return {"ok": True, "ms_id": exact[0]["ms_id"],
                "name": exact[0].get("name") or clean, "existed": True}

    payload: dict = {"name": clean}
    if (phone or "").strip():
        payload["phone"] = phone.strip()[:255]
    try:
        sess = await get_session()
        async with sess.post(f"{MS_BASE}/entity/counterparty", json=payload) as resp:
            body = await resp.text()
            if resp.status >= 400:
                safe = redact_ms_error(body)
                logger.error("MS counterparty HTTP %s: %s", resp.status, safe)
                return {"ok": False, "error": f"МойСклад отказал: {safe}"}
            created = json.loads(body)
    except Exception as e:
        logger.warning("Контрагент «%s» не заведён: %s", clean, e)
        return {"ok": False, "error": "МойСклад недоступен, попробуйте позже"}

    ms_id = str(created.get("id") or "")
    if not ms_id:
        return {"ok": False, "error": "МойСклад не вернул id контрагента"}
    await snapshot.remember_counterparty(
        ms_id, name=created.get("name") or clean, phone=payload.get("phone")
    )
    return {"ok": True, "ms_id": ms_id, "name": created.get("name") or clean,
            "existed": False}
