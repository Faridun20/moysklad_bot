"""
Реверс «Заказа покупателя» (entity/customerorder) в МойСклад при отмене заказа
(IMPLEMENTATION.md §6.7, MS-интеграция).

Поток:
  заказ отменён в БД (cancel_order: approved→cancelled) → handler best-effort
  зовёт reverse_customerorder(order_id). Отмена доступна только для approved
  (НЕ отгруженных) заказов — значит в МС есть customerorder, но НЕ demand
  (отгрузка не создавалась). «Реверс» = удалить customerorder (МС перемещает
  его в Корзину — операция обратимая).

Принципы (как у ms_returns/ms_payments):
  • контекст org/store берём из ms_demand._CTX, gate через ms_demand.is_ready()
    — без него no-op (тесты/локалка без токена);
  • идемпотентность: orders.ms_cancel_synced_at стоит → не повторяем;
  • best-effort: ошибка МС не ломает бизнес-флоу (отмена уже зафиксирована в
    БД), только лог через redact_ms_error. МС сам обеспечивает целостность —
    если к заказу привязаны документы (платёж), DELETE вернёт ошибку, и мы её
    проглатываем (бухгалтер закроет вручную). DELETE обратим (Корзина МС).
"""

import logging

from services import database as db
from services import ms_demand
from services.metrics import measure_async
from services.moysklad import MS_BASE, get_session, redact_ms_error

logger = logging.getLogger(__name__)


@measure_async("ms.reverse_customerorder")
async def reverse_customerorder(order_id: int) -> dict:
    """Отменить (удалить в Корзину) customerorder в МойСклад при отмене заказа.

    Возвращает {ok, skipped?} / {ok: False, reason}. Безопасно вызывать повторно.
    """
    if not ms_demand.is_ready():
        return {"ok": False, "reason": "MS context не готов (org/store)"}

    order = await db.get_order(order_id)  # native async (asyncpg Stage 19)
    if not order:
        return {"ok": False, "reason": "Заказ не найден"}
    if order.get("ms_cancel_synced_at"):
        return {"ok": True, "skipped": "already-synced"}
    co_id = order.get("ms_customerorder_id")
    if not co_id:
        # Заказ не доехал до МС (или legacy demand-флоу) — реверсить нечего.
        return {"ok": True, "skipped": "no-customerorder"}

    try:
        sess = await get_session()
        async with sess.delete(f"{MS_BASE}/entity/customerorder/{co_id}") as resp:
            body = await resp.text()
            # 200/204 — успех. 404 — документ уже удалён в МС → считаем синком.
            if resp.status in (200, 204, 404):
                await db.set_order_ms_cancel_synced(order_id)  # native async (asyncpg #21)
                return {"ok": True, "ms_id": co_id}
            safe = redact_ms_error(body)
            logger.error("MS reverse customerorder HTTP %s: %s", resp.status, safe)
            return {"ok": False, "reason": f"HTTP {resp.status}: {safe}"}
    except Exception as e:
        logger.exception("reverse customerorder failed")
        return {"ok": False, "reason": f"{type(e).__name__}: {redact_ms_error(str(e)[:200])}"}
