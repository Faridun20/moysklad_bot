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

from services import async_db as adb

logger = logging.getLogger(__name__)

# Типы событий: только они обрабатываются здесь; остальные игнорируются.
# demand — только DELETE (бот-созданная отгрузка удалена в МС → фантом);
# CREATE/UPDATE demand идут отдельным путём (остатки + уведомления о новых).
_HANDLED = {"paymentin", "customerorder", "demand"}

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
        elif entity_type == "demand":
            # Только DELETE: бот-созданная отгрузка удалена в МС → заказ-фантом.
            if action == "DELETE":
                tasks.append(_handle_demand_deleted(entity_id))

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
    from services.notifier import aget_notify_recipients, tg_send_message

    payment = await adb.find_payment_by_ms_paymentin_id(paymentin_id)
    if not payment:
        logger.debug(
            "paymentin.DELETE %s — не найден в локальной БД, пропускаем",
            paymentin_id,
        )
        return

    payment_id = payment["id"]
    await adb.reset_payment_ms_sync(payment_id)

    order_id = payment.get("order_id")
    order_label = f" по заказу #{order_id}" if order_id else ""
    amount = float(payment.get("amount") or 0)
    currency = payment.get("currency") or ""
    manager = payment.get("full_name") or "—"

    await adb.add_audit_log(
        0,
        "МойСклад",
        "system",
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
        paymentin_id,
        payment_id,
    )


async def _handle_paymentin_updated(paymentin_id: str) -> None:
    """Входящий платёж изменён в МойСклад — фиксируем в audit_log."""
    payment = await adb.find_payment_by_ms_paymentin_id(paymentin_id)
    if not payment:
        return

    await adb.add_audit_log(
        0,
        "МойСклад",
        "system",
        "ms_paymentin_updated",
        f"paymentin {paymentin_id} изменён в МойСклад (платёж #{payment['id']}).",
    )
    logger.info(
        "paymentin.UPDATE %s → платёж #%d (только audit_log)",
        paymentin_id,
        payment["id"],
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
    from services.moysklad import ms_get
    from services.notifier import tg_send_message

    order = await adb.find_order_by_ms_customerorder_id(co_id)
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

    # Этап 2a: дрифт суммы — кто-то отредактировал позиции/цены в МС. Сигнал
    # (флаг + уведомление), деньги/статус НЕ меняем молча.
    await _check_order_drift(order, co_data)

    state = co_data.get("state") or {}
    state_type = state.get("stateType", "")
    state_name = state.get("name", "")
    new_status = _STATE_TYPE_TO_STATUS.get(state_type)

    order_id = order["id"]
    if not new_status:
        # Regular/кастомный stateType — не маппим (без конфигурации аккаунта не
        # знаем смысла). Но ЛОГИРУЕМ для наблюдаемости (дыра #4): видно, какие
        # статусы реально шлёт МС, чтобы при необходимости добавить маппинг.
        if state_name or state_type:
            logger.info(
                "customerorder.UPDATE %s заказ #%s: непереводимый статус МС "
                "state='%s' stateType=%s — локальный статус не тронут",
                co_id, order_id, state_name, state_type,
            )
        return
    if new_status == local_status:
        return

    # Гейт по машине состояний: системный сигнал из МС не должен делать нелегальный
    # переход. Прежде Unsuccessful маппился в 'rejected' и применялся даже к
    # approved-заказу (approved→rejected НЕ легален: rejected — только из pending) →
    # реальная отгрузка/остаток в МС оставались, а локально заказ «отклонён» без
    # отката. Нелегальный переход не применяем — алертим boss/admin на ручную
    # разборку.
    from services.order_workflow import TRANSITIONS

    if new_status not in TRANSITIONS.get(local_status, []):
        logger.warning(
            "customerorder.UPDATE %s заказ #%s: нелегальный переход %s→%s по сигналу "
            "МС (state='%s', stateType=%s) — пропускаю, нужна ручная разборка",
            co_id, order_id, local_status, new_status, state_name, state_type,
        )
        # Дедуп: помечаем «нужна ручная разборка» (тем же флагом, что дрифт) и
        # алертим ОДИН раз. Иначе каждый последующий customerorder.UPDATE по
        # застрявшему заказу слал бы повторный алерт. set_order_ms_drift
        # идемпотентен (True только при первой установке).
        if await adb.set_order_ms_drift(order_id):
            from services.notifier import aget_notify_recipients
            from utils.helpers import esc

            for uid in await aget_notify_recipients():
                await tg_send_message(
                    uid,
                    f"⚠️ Заказ #{order_id}: МойСклад сообщил статус "
                    f"«{esc(state_name)}», но локальный статус <b>{local_status}</b> "
                    f"нельзя перевести в <b>{new_status}</b>. Разберите вручную.",
                )
        return

    # CAS против снимка (TOCTOU): между чтением local_status и записью был сетевой
    # round-trip к МС — статус мог измениться (напр. confirm_payment → paid).
    # Применяем только если статус не изменился; иначе пропускаем без отката.
    if not await adb.cas_order_status(order_id, new_status, local_status):
        logger.info(
            "customerorder.UPDATE %s заказ #%s: статус изменился конкурентно "
            "(ожидался %s) — пропускаю",
            co_id, order_id, local_status,
        )
        return
    await adb.add_audit_log(
        0,
        "МойСклад",
        "system",
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
        co_id,
        order_id,
        local_status,
        new_status,
        state_name,
    )


async def _handle_customerorder_deleted(co_id: str) -> None:
    """Заказ покупателя удалён в МойСклад.

    Если локальный заказ ещё `approved` (документ создан, но не отгружён) —
    ОТМЕНЯЕМ его локально, чтобы бот не показывал «живой» заказ, которого в МС
    уже нет. `ms_cancel_synced_at` выставляем сразу (реверс в МС не нужен —
    документ уже удалён). Для shipped/paid (деньги/остатки уже двигались) статус
    НЕ трогаем — только снимаем ссылку и предупреждаем boss/admin для ручной
    разборки. (cancel_order разрешает переход только из approved.)
    """
    order = await adb.find_order_by_ms_customerorder_id(co_id)
    if not order:
        return
    await apply_ms_customerorder_delete(order, co_id)


async def apply_ms_customerorder_delete(order: dict, co_id: str) -> None:
    """Применить «CO удалён в МС» к локальному заказу. Общий путь для вебхука
    (customerorder.DELETE) и cron-реконсиляции (tasks/run_ms_reconcile). approved
    → отмена локально (idempotent через cancel_order WHERE status='approved');
    иначе — снять ссылку + предупредить."""
    from services.notifier import aget_notify_recipients, tg_send_message
    from utils.helpers import esc

    order_id = order["id"]
    status = order.get("status", "")
    agent = esc(order.get("agent_name") or "—")

    # Идемпотентность алерта/аудита: повтор (вебхук + cron, либо дважды в батче)
    # не должен слать второе уведомление. НО только для уже-обработанных статусов:
    # approved-заказ всё равно надо отменить, даже если ms_deleted_at уже выставил
    # demand.DELETE (он помечает флаг, но НЕ отменяет approved). cancel_order ниже
    # идемпотентен (WHERE status='approved'), поэтому повтор approved безопасен.
    if status != "approved" and order.get("ms_deleted_at"):
        return

    if status == "approved":
        res = await adb.cancel_order(
            order_id, 0, "МойСклад", "Заказ покупателя удалён в МойСклад"
        )
        if res.get("ok"):
            # Документ в МС уже удалён → reverse не нужен, помечаем синком.
            await adb.set_order_ms_cancel_synced(order_id)
        await adb.clear_order_ms_customerorder_id(order_id)
        # Помечаем как удалённый в МС → исключаем из аналитики менеджеров.
        await adb.set_order_ms_deleted(order_id)
        await adb.add_audit_log(
            0,
            "МойСклад",
            "system",
            "ms_customerorder_deleted",
            f"CO {co_id} удалён в МС → заказ #{order_id} отменён локально (был approved).",
        )
        text = (
            f"⚠️ <b>Заказ покупателя удалён в МойСклад</b>\n\n"
            f"Заказ #{order_id} (клиент: {agent}) был <b>approved</b> — "
            f"автоматически отменён в боте (статус → cancelled)."
        )
        for uid in await aget_notify_recipients():
            await tg_send_message(uid, text)
        logger.info("customerorder.DELETE %s → заказ #%d отменён локально", co_id, order_id)
        return

    # Не approved (shipped/paid/cancelled/…): статус не трогаем, только ссылка.
    await adb.clear_order_ms_customerorder_id(order_id)
    # Помечаем как удалённый в МС → заказ уходит из аналитики, списков, а также
    # из учёта долгов/лимитов (нет заказа в МС → нет долга по нему). Деньги/
    # остатки в боте не трогаем — попадает в ночной ops-дайджест для ручной сверки.
    await adb.set_order_ms_deleted(order_id)
    await adb.add_audit_log(
        0,
        "МойСклад",
        "system",
        "ms_customerorder_deleted",
        f"CO {co_id} удалён в МС → заказ #{order_id} (status={status}): ссылка снята, "
        f"статус не тронут (требует ручной проверки).",
    )
    text = (
        f"⚠️ <b>Заказ покупателя удалён в МойСклад</b>\n\n"
        f"Заказ #{order_id} (клиент: {agent}), локальный статус: <b>{esc(status)}</b>.\n"
        f"Документ удалён вручную в МС. Связь снята, но статус НЕ изменён "
        f"(деньги/остатки могли двигаться) — проверьте вручную."
    )
    for uid in await aget_notify_recipients():
        await tg_send_message(uid, text)
    logger.info(
        "customerorder.DELETE %s → заказ #%d (status=%s): только ссылка снята",
        co_id,
        order_id,
        status,
    )


# Порог дрифта суммы: игнорируем мелочь (округления/НДС-копейки), флагуем только
# материальное расхождение — больше max(1% суммы, 1 у.е.).
_DRIFT_ABS_MIN_CENTS = 100


async def _check_order_drift(order: dict, co_data: dict) -> None:
    """Сверить сумму локального заказа с документом МойСклад. При материальном
    расхождении — пометить `ms_drift_at` (идемпотентно) + уведомить boss/admin.
    Деньги/статус НЕ трогаем: это сигнал «заказ отредактирован в МС вручную»."""
    order_id = order["id"]
    if order.get("ms_drift_at"):
        return  # уже помечен — одно уведомление на заказ
    try:
        ms_sum = int(co_data.get("sum") or 0)
    except (TypeError, ValueError):
        return
    if ms_sum <= 0:
        return
    local_cents = await adb.get_order_total_cents(order_id)
    if local_cents <= 0:
        return
    if abs(ms_sum - local_cents) <= max(_DRIFT_ABS_MIN_CENTS, local_cents // 100):
        return  # в пределах допуска
    if not await adb.set_order_ms_drift(order_id):
        return  # гонка: кто-то уже пометил

    from services.notifier import aget_notify_recipients, tg_send_message
    from utils.helpers import esc

    agent = esc(order.get("agent_name") or "—")
    await adb.add_audit_log(
        0,
        "МойСклад",
        "system",
        "ms_order_drift",
        f"Заказ #{order_id}: сумма в МС ({ms_sum / 100:,.0f}) ≠ локальной "
        f"({local_cents / 100:,.0f}) — отредактирован в МС вручную.",
    )
    text = (
        f"⚠️ <b>Заказ изменён в МойСклад</b>\n\n"
        f"Заказ #{order_id} (клиент: {agent}).\n"
        f"Сумма в МС: <b>{ms_sum / 100:,.0f}</b>, в боте: <b>{local_cents / 100:,.0f}</b>.\n"
        f"Кто-то отредактировал позиции/цены в МС. Деньги в боте НЕ тронуты — "
        f"проверьте и поправьте вручную при необходимости."
    )
    for uid in await aget_notify_recipients():
        await tg_send_message(uid, text)
    logger.info(
        "customerorder drift заказ #%d: МС=%d local=%d (помечен ms_drift_at)",
        order_id, ms_sum, local_cents,
    )


# ─── Demand (отгрузка) ──────────────────────────────────────────────────────────


async def _handle_demand_deleted(demand_id: str) -> None:
    """Вебхук demand.DELETE: находим заказ по ms_demand_id и применяем
    apply_ms_demand_delete. Идемпотентно (флаг уже стоит → тихо выходим)."""
    order = await adb.find_order_by_ms_demand_id(demand_id)
    if not order:
        return
    await apply_ms_demand_delete(order, demand_id)


async def apply_ms_demand_delete(order: dict, demand_id: str) -> None:
    """Применить «отгрузка (demand) удалена в МС» к локальному заказу. Общий путь
    для вебхука (demand.DELETE) и cron-реконсиляции (run_ms_reconcile проверяет
    существование demand-документа, не только customerorder).

    Статус/деньги/остатки НЕ трогаем (отгрузка их двигала), но помечаем заказ
    `ms_deleted_at` → он уходит из аналитики выручки, списков заказов И из учёта
    долгов/лимитов (фантом: нет отгрузки → нет долга). Уведомляем boss/admin.
    Идемпотентно: при уже выставленном флаге — тихо выходим (без дубль-алерта)."""
    from services.notifier import aget_notify_recipients, tg_send_message
    from utils.helpers import esc

    if order.get("ms_deleted_at"):
        return  # уже помечен — не дублируем уведомление
    order_id = order["id"]
    status = order.get("status", "")
    agent = esc(order.get("agent_name") or "—")

    await adb.set_order_ms_deleted(order_id)
    await adb.add_audit_log(
        0,
        "МойСклад",
        "system",
        "ms_demand_deleted",
        f"demand {demand_id} удалён в МС → заказ #{order_id} (status={status}) "
        f"помечен ms_deleted_at (вне аналитики), статус не тронут.",
    )
    text = (
        f"⚠️ <b>Отгрузка удалена в МойСклад</b>\n\n"
        f"Заказ #{order_id} (клиент: {agent}), статус: <b>{esc(status)}</b>.\n"
        f"Отгрузка удалена вручную в МС. Заказ убран из аналитики выручки; "
        f"деньги/остатки НЕ тронуты — проверьте вручную."
    )
    for uid in await aget_notify_recipients():
        await tg_send_message(uid, text)
    logger.info("demand.DELETE %s → заказ #%d помечен ms_deleted_at", demand_id, order_id)
