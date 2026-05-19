"""
Обработчик входящих webhook-событий от МойСклад (paymentin / customerorder).

Вызывается из webapp/server.py:ms_webhook() через asyncio.create_task(),
чтобы не задерживать ответ 200 к МойСклад.

Обрабатываемые события:
  paymentin.DELETE  → сбросить ms_paymentin_id у платежа; cron-retry
                      (run_ms_sync_retry) автоматически воссоздаст его.
                      Уведомить boss/admin в Telegram.
  paymentin.UPDATE  → записать в audit_log (информационно).
  customerorder.UPDATE → подтянуть статус из МойСклад; если stateType
                         Successful/Unsuccessful — обновить локальный статус
                         заказа и уведомить менеджера.
  customerorder.DELETE → снять ссылку ms_customerorder_id; уведомить.

Тип события определяется полем meta.type из payload'а webhook'а.
Entity ID извлекается из meta.href (последний сегмент пути).
"""

import asyncio
import logging

logger = logging.getLogger(__name__)

# Типы событий: только они обрабатываются здесь; остальные игнорируются.
_HANDLED = {"paymentin", "customerorder"}

# stateType → наш локальный статус.
# Regular-состояния (промежуточные в МойСклад) — пропускаем: не знаем их
# смысла без конкретной конфигурации аккаунта.
_STATE_TYPE_TO_STATUS = {
    "Successful": "shipped",
    "Unsuccessful": "rejected",
}

# Статусы заказа, из которых допустим переход по сигналу из МойСклад.
# Уже отгруженные / отклонённые — не откатываем.
_UPDATABLE_STATUSES = {"pending", "approved"}


async def handle_ms_events(events: list[dict]) -> None:
    """Диспетчер: маршрутизирует каждое событие своему обработчику.

    Запускает обработчики параллельно через gather(); исключения ловятся
    per-task и логируются — один сбой не прерывает остальные.
    """
    tasks = []
    for event in events:
        action = (event.get("action") or "").upper()
        meta = event.get("meta") or {}
        entity_type = (meta.get("type") or "").lower()
        href = meta.get("href", "")
        entity_id = href.rstrip("/").split("/")[-1] if href else None

        if entity_type not in _HANDLED or not entity_id:
            continue

        if entity_type == "paymentin":
            if action == "DELETE":
                tasks.append(_handle_paymentin_deleted(entity_id))
            elif action == "UPDATE":
                tasks.append(_handle_paymentin_updated(entity_id))
        elif entity_type == "customerorder":
            if action == "UPDATE":
                tasks.append(_handle_customerorder_updated(entity_id))
            elif action == "DELETE":
                tasks.append(_handle_customerorder_deleted(entity_id))

    if not tasks:
        return

    results = await asyncio.gather(*tasks, return_exceptions=True)
    for r in results:
        if isinstance(r, Exception):
            logger.exception("ms_sync_handler: task failed: %s", r)


# ─── Paymentin ────────────────────────────────────────────────────────────────


async def _handle_paymentin_deleted(paymentin_id: str) -> None:
    """Входящий платёж удалён в МойСклад.

    Сбрасываем ms_paymentin_id → NULL; следующий запуск run_ms_sync_retry
    (или /sync_payments) создаст его повторно. Уведомляем boss/admin, чтобы
    они знали о ручном вмешательстве в МойСклад.
    """
    from services.database import (
        find_payment_by_ms_paymentin_id,
        reset_payment_ms_sync,
        add_audit_log,
    )
    from services.notifier import aget_notify_recipients, tg_send_message

    payment = await asyncio.to_thread(find_payment_by_ms_paymentin_id, paymentin_id)
    if not payment:
        logger.debug(
            "paymentin.DELETE %s — не найден в локальной БД, пропускаем",
            paymentin_id,
        )
        return

    payment_id = payment["id"]
    await asyncio.to_thread(reset_payment_ms_sync, payment_id)

    order_id = payment.get("order_id")
    order_label = f" по заказу #{order_id}" if order_id else ""
    amount = float(payment.get("amount") or 0)
    currency = payment.get("currency") or ""
    manager = payment.get("full_name") or "—"

    await asyncio.to_thread(
        add_audit_log,
        0, "МойСклад", "system",
        "ms_paymentin_deleted",
        (
            f"paymentin {paymentin_id} удалён в МойСклад. "
            f"Платёж #{payment_id}{order_label} ({amount:,.0f} {currency}) сброшен "
            f"→ ms_paymentin_id=NULL, статус='deleted_in_ms'. "
            f"Будет воссоздан при следующей синхронизации."
        ),
    )

    text = (
        f"⚠️ <b>Платёж удалён в МойСклад</b>\n\n"
        f"Локальный платёж: #{payment_id}{order_label}\n"
        f"Сумма: <b>{amount:,.0f} {currency}</b>\n"
        f"Менеджер: {manager}\n\n"
        f"Входящий платёж был удалён вручную в МойСклад. "
        f"Ссылка сброшена — он будет автоматически воссоздан при "
        f"следующем запуске синхронизации (или через /sync_payments)."
    )
    for uid in await aget_notify_recipients():
        await tg_send_message(uid, text)

    logger.info(
        "paymentin.DELETE %s → платёж #%d сброшен (ms_paymentin_id=NULL)",
        paymentin_id, payment_id,
    )


async def _handle_paymentin_updated(paymentin_id: str) -> None:
    """Входящий платёж изменён в МойСклад — фиксируем в audit_log."""
    from services.database import find_payment_by_ms_paymentin_id, add_audit_log

    payment = await asyncio.to_thread(find_payment_by_ms_paymentin_id, paymentin_id)
    if not payment:
        return

    await asyncio.to_thread(
        add_audit_log,
        0, "МойСклад", "system",
        "ms_paymentin_updated",
        f"paymentin {paymentin_id} изменён в МойСклад (платёж #{payment['id']}).",
    )
    logger.info(
        "paymentin.UPDATE %s → платёж #%d (только audit_log)",
        paymentin_id, payment["id"],
    )


# ─── Customerorder ────────────────────────────────────────────────────────────


async def _handle_customerorder_updated(co_id: str) -> None:
    """Заказ покупателя обновлён в МойСклад.

    Подтягиваем актуальный документ через API. Если stateType изменился
    на Successful (выполнен) или Unsuccessful (отменён) — обновляем статус
    локального заказа и уведомляем менеджера-автора.

    Переходы только из {"pending", "approved"}: не откатываем уже
    отгруженные или отклонённые заказы.
    """
    from services.database import (
        find_order_by_ms_customerorder_id,
        update_order_status,
        add_audit_log,
    )
    from services.moysklad import ms_get
    from services.notifier import tg_send_message

    order = await asyncio.to_thread(find_order_by_ms_customerorder_id, co_id)
    if not order:
        return

    local_status = order.get("status", "")
    if local_status not in _UPDATABLE_STATUSES:
        return

    try:
        co_data = await ms_get(f"entity/customerorder/{co_id}")
    except Exception as e:
        logger.warning("customerorder.UPDATE %s: не удалось получить документ: %s", co_id, e)
        return

    state = co_data.get("state") or {}
    state_type = state.get("stateType", "")
    state_name = state.get("name", "")
    new_status = _STATE_TYPE_TO_STATUS.get(state_type)

    if not new_status or new_status == local_status:
        return

    order_id = order["id"]
    await asyncio.to_thread(update_order_status, order_id, new_status)
    await asyncio.to_thread(
        add_audit_log,
        0, "МойСклад", "system",
        "ms_order_status_synced",
        (
            f"Заказ #{order_id}: статус {local_status} → {new_status} "
            f"(МойСклад state='{state_name}', stateType={state_type})"
        ),
    )

    manager_id = order.get("user_id")
    if manager_id:
        status_ru = {"shipped": "Отгружен ✅", "rejected": "Отклонён ❌"}.get(
            new_status, new_status
        )
        await tg_send_message(
            manager_id,
            f"📦 Статус заказа #{order_id} обновлён в МойСклад: <b>{status_ru}</b>\n"
            f"Клиент: {order.get('agent_name') or '—'}",
        )

    logger.info(
        "customerorder.UPDATE %s → заказ #%d: %s → %s (state=%s)",
        co_id, order_id, local_status, new_status, state_name,
    )


async def _handle_customerorder_deleted(co_id: str) -> None:
    """Заказ покупателя удалён в МойСклад.

    Снимаем ссылку ms_customerorder_id с локального заказа, уведомляем
    boss/admin. При следующем одобрении заявки будет создан новый документ.
    """
    from services.database import (
        find_order_by_ms_customerorder_id,
        clear_order_ms_customerorder_id,
        add_audit_log,
    )
    from services.notifier import aget_notify_recipients, tg_send_message

    order = await asyncio.to_thread(find_order_by_ms_customerorder_id, co_id)
    if not order:
        return

    order_id = order["id"]
    await asyncio.to_thread(clear_order_ms_customerorder_id, order_id)
    await asyncio.to_thread(
        add_audit_log,
        0, "МойСклад", "system",
        "ms_customerorder_deleted",
        (
            f"Заказ покупателя ms_customerorder_id={co_id} удалён в МойСклад. "
            f"Ссылка с заказа #{order_id} снята."
        ),
    )

    agent = order.get("agent_name") or "—"
    for uid in await aget_notify_recipients():
        await tg_send_message(
            uid,
            f"⚠️ <b>Заказ покупателя удалён в МойСклад</b>\n\n"
            f"Заказ #{order_id} (клиент: {agent})\n\n"
            f"Документ «Заказ покупателя» был удалён вручную в МойСклад. "
            f"Связь сброшена — при повторном одобрении заявки бот "
            f"создаст новый заказ покупателя автоматически.",
        )

    logger.info(
        "customerorder.DELETE %s → заказ #%d: ms_customerorder_id сброшен",
        co_id, order_id,
    )
