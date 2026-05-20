"""
Order state machine — единый источник правил переходов и прав.

Вместо разбросанных проверок `is_boss()` + ручного UPDATE status
используется:
  - TRANSITIONS — что за чем может следовать
  - ROLE_FOR_TRANSITION — кто имеет право инициировать переход
  - validate_transition() — синхронная проверка допустимости (без DB)
  - can_transition() — проверка прав роли на конкретный переход
  - approve_shipment_request() / reject_shipment_request() — полный
    жизненный цикл апрува/реджекта одной заявки (DB + МойСклад +
    уведомления + PDF). Вызывается и из bot handler'а, и из webapp
    endpoint — единственная точка истины.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)

# Допустимые переходы: текущий_статус → список разрешённых следующих
TRANSITIONS: dict[str, list[str]] = {
    "draft":    ["pending", "rejected"],   # отправить или отменить (удалить)
    "pending":  ["approved", "rejected"],  # решение боса
    "approved": ["shipped"],               # фиксация отгрузки
    "rejected": [],
    "shipped":  [],
}

# Какие роли могут инициировать какие переходы
_ROLE_TRANSITIONS: dict[str, set[str]] = {
    "manager": {"draft→pending"},
    "boss":    {"pending→approved", "pending→rejected"},
    "admin":   {
        "draft→pending", "pending→approved",
        "pending→rejected", "approved→shipped",
        "draft→rejected",
    },
    "guest":   set(),
}


def can_transition(order: dict, new_status: str, role: str) -> bool:
    """True если роль `role` вправе перевести заказ в `new_status`.

    Проверяет сразу две вещи:
    1. Переход допустим по TRANSITIONS (например нельзя shipped→draft)
    2. Роль авторизована для этого перехода
    """
    current = order.get("status", "")
    if new_status not in TRANSITIONS.get(current, []):
        return False
    key = f"{current}→{new_status}"
    return key in _ROLE_TRANSITIONS.get(role, set())


def validate_transition(order: dict, new_status: str) -> str | None:
    """Вернуть строку ошибки если переход недопустим, иначе None.

    Используется для pre-check ДО обращения к БД — быстрый fail-fast.
    """
    current = order.get("status", "")
    if new_status not in TRANSITIONS.get(current, []):
        return f"Переход {current!r}→{new_status!r} недопустим"
    return None


# ─── Полный жизненный цикл апрува/реджекта ──────────────────────────────────
#
# До рефакторинга логика жила в handlers/orders.py:cb_approve_request
# (~250 строк) и продублировать её в webapp endpoint'е было неподъёмно.
# Теперь — один сервис, который вызывают и Telegram callback, и /api/.


async def approve_shipment_request(
    req_id: int,
    boss_user_id: int,
    boss_name: str,
    bot: Any,
) -> dict:
    """Полный апрув заявки: DB, МойСклад, уведомления, PDF, авто-payment.

    Параметры:
        req_id        — id заявки в shipment_requests
        boss_user_id  — telegram id одобряющего (для аудита и PDF)
        boss_name     — отображаемое имя одобряющего
        bot           — aiogram.Bot (или совместимый, у которого есть
                        send_message/send_document). Может быть None,
                        тогда уведомления и PDF не отправляются.

    Возвращает dict:
        {
          "ok": bool,
          "error": str | None,   # ключи валидации/race conditions
          "req_id": int,
          "order_id": int | None,
          "now": str,            # local_now() — для UI handler'а
          "demand_line": str,    # текст для concat в Telegram-сообщение
          "ms_co_id": str | None,
          "ms_demand_id": str | None,
        }
    """
    from services import async_db as adb
    from utils.helpers import local_now, esc

    req = await adb.get_shipment_request(req_id)
    if not req:
        return {"ok": False, "error": "Заявка не найдена", "req_id": req_id, "order_id": None}
    if req["status"] != "pending":
        return {
            "ok": False, "error": "Заявка уже обработана",
            "req_id": req_id, "order_id": req.get("order_id"),
        }

    # Атомарный UPDATE ... WHERE status='pending' — защита от race condition,
    # когда два босса одновременно жмут «Одобрить». Только один из них
    # получит rowcount==1, остальные — False.
    ok = await adb.approve_shipment_request(req_id, boss_user_id, boss_name)
    if not ok:
        return {
            "ok": False, "error": "Заявка уже обработана другим пользователем",
            "req_id": req_id, "order_id": req.get("order_id"),
        }

    now_str = local_now().strftime("%d.%m.%Y %H:%M")

    order, boss_role = await asyncio.gather(
        adb.get_order(req["order_id"]),
        adb.get_role(boss_user_id),
    )
    items = await adb.get_order_items(req["order_id"]) if order else []
    manager_name = (order or {}).get("full_name") or req.get("full_name") or "—"
    manager_user_id = (order or {}).get("user_id") or req.get("user_id")

    # Цепочка в МойСклад:
    #   1) customerorder — для PDF печатной формы
    #   2) demand линкованный с customerorder — чтобы СРАЗУ списать остатки
    # Backend URL'ы в чат не отправляем — только PDF файлом.
    from services.ms_demand import is_ready as ms_ready, create_demand_from_request
    from services.ms_customerorder import create_customerorder_from_request

    demand_line = ""
    pdf_to_send: tuple[bytes, str] | None = None
    co_id: str | None = None
    demand_id: str | None = None

    if order and items and ms_ready():
        co_result = await create_customerorder_from_request(
            order, items, manager_name, telegram_user_id=manager_user_id,
        )
        if co_result.get("ok"):
            co_id = co_result.get("customerorder_id")
            # Audit СРАЗУ после успешного POST в МойСклад — до того
            # как пытаемся сохранить co_id в БД (см. SECURITY.md H6).
            if co_id:
                await asyncio.gather(
                    adb.add_audit_log(
                        boss_user_id, boss_name, boss_role,
                        "ms_co_created",
                        f"Заявка #{req_id} → customerorder {co_id} (до db-write)",
                    ),
                    adb.set_order_ms_customerorder_id(order["id"], co_id),
                )

            if co_result.get("pdf_bytes"):
                pdf_to_send = (
                    co_result["pdf_bytes"],
                    co_result.get("pdf_filename") or f"order_{order['id']}.pdf",
                )

            from services.moysklad import MS_BASE
            co_href = f"{MS_BASE}/entity/customerorder/{co_id}" if co_id else None
            demand_result = await create_demand_from_request(
                order, items, manager_name,
                telegram_user_id=manager_user_id,
                customerorder_href=co_href,
            )
            if demand_result.get("ok"):
                demand_id = demand_result.get("demand_id")
                if demand_id:
                    await asyncio.gather(
                        adb.add_audit_log(
                            boss_user_id, boss_name, boss_role,
                            "ms_demand_created",
                            f"Заявка #{req_id} → demand {demand_id} (до db-write)",
                        ),
                        adb.set_order_ms_demand_id(order["id"], demand_id),
                    )
                demand_line = (
                    f"\n📄 Заказ покупателя создан в МойСклад"
                    f"\n📦 Отгрузка проведена, остатки списаны"
                )
                if pdf_to_send:
                    demand_line += " — печатная форма ниже 👇"
                await adb.add_audit_log(
                    boss_user_id, boss_name, boss_role,
                    "ms_co_and_demand_created",
                    f"Заявка #{req_id} → customerorder {co_id} + demand {demand_id}",
                )
            else:
                reason = demand_result.get("reason", "неизвестная ошибка")
                logger.warning(
                    "Customerorder создан, но demand упал для #%s: %s",
                    req_id, reason,
                )
                demand_line = (
                    f"\n📄 Заказ покупателя создан в МойСклад"
                    f"\n⚠️ <b>Отгрузка НЕ создана автоматически:</b>\n"
                    f"<code>{esc(reason[:200])}</code>\n"
                    f"Остатки не списались — создайте demand вручную в МойСклад "
                    f"(или из карточки этого заказа покупателя)."
                )
                if pdf_to_send:
                    demand_line += "\nПечатная форма ниже 👇"
                await adb.add_audit_log(
                    boss_user_id, boss_name, boss_role,
                    "ms_demand_failed",
                    f"Заявка #{req_id} → CO {co_id} ok, demand fail: {reason[:200]}",
                )
                if bot is not None:
                    try:
                        await bot.send_message(
                            boss_user_id,
                            f"⚠️ MS demand fail для заявки #{req_id} "
                            f"(customerorder {(co_id or '')[:8]} создан):\n"
                            f"<code>{esc(reason[:500])}</code>",
                            parse_mode="HTML",
                        )
                    except Exception:
                        pass
        else:
            reason = co_result.get("reason", "неизвестная ошибка")
            logger.warning(
                "Не удалось создать customerorder для заявки #%s: %s",
                req_id, reason,
            )
            demand_line = (
                f"\n⚠️ <b>Не удалось создать заказ в МойСклад:</b>\n"
                f"<code>{esc(reason[:300])}</code>\n"
                f"Заявка в боте одобрена — заказ нужно завести вручную."
            )
            if bot is not None:
                try:
                    await bot.send_message(
                        boss_user_id,
                        f"⚠️ MS customerorder fail для заявки #{req_id}:\n"
                        f"<code>{esc(reason[:500])}</code>",
                        parse_mode="HTML",
                    )
                except Exception:
                    pass
    elif not ms_ready():
        logger.info(
            "MS context не готов — не создаём документы для заявки #%s", req_id,
        )

    # Уведомляем менеджера
    if bot is not None and req.get("user_id"):
        from services.notify import notify_order_approved
        try:
            await notify_order_approved(
                bot, req["user_id"], req_id, boss_name, now_str, demand_line,
            )
        except Exception:
            logger.exception("notify_order_approved failed for req #%s", req_id)

    # PDF менеджеру и боссу (одной и той же сборкой)
    if pdf_to_send and bot is not None:
        try:
            from aiogram.types import BufferedInputFile
            pdf_bytes, pdf_name = pdf_to_send
            caption = f"📄 Печатная форма — заявка #{req_id}"
            try:
                file1 = BufferedInputFile(pdf_bytes, filename=pdf_name)
                await bot.send_document(
                    chat_id=req["user_id"], document=file1, caption=caption,
                )
            except Exception:
                logger.exception("Не удалось отправить PDF менеджеру")
            if boss_user_id != req["user_id"]:
                try:
                    file2 = BufferedInputFile(pdf_bytes, filename=pdf_name)
                    await bot.send_document(
                        chat_id=boss_user_id, document=file2, caption=caption,
                    )
                except Exception:
                    logger.exception("Не удалось отправить PDF боссу")
        except Exception:
            logger.exception("PDF dispatch failed for req #%s", req_id)

    # Для paid-заказов автоматически создаём payment-pending,
    # чтобы босс одной кнопкой зафиксировал реальное получение денег.
    if order and (order.get("payment_type") or "paid") == "paid":
        total = sum(
            float(it.get("quantity", 0)) * float(it.get("price", 0) or 0)
            for it in items
        )
        if total > 0.01:
            currency = order.get("currency") or "USD"
            try:
                existing = [
                    p for p in await adb.get_payments_for_order(order["id"])
                    if p["status"] in ("pending", "confirmed")
                ]
                if not existing:
                    payment_id = await adb.add_payment(
                        user_id=order["user_id"],
                        username="",
                        full_name=manager_name,
                        amount=total,
                        currency=currency,
                        comment=f"Оплата по заказу #{order['id']} (отгрузка одобрена)",
                        order_id=order["id"],
                    )
                    if bot is not None:
                        from handlers.debts import _push_payment_confirmation
                        await _push_payment_confirmation(
                            bot, order["id"], manager_name, payment_id,
                        )
            except Exception:
                logger.exception(
                    "Не удалось создать auto-payment для paid-заказа #%s",
                    order["id"],
                )

    return {
        "ok": True,
        "error": None,
        "req_id": req_id,
        "order_id": req.get("order_id"),
        "now": now_str,
        "demand_line": demand_line,
        "ms_co_id": co_id,
        "ms_demand_id": demand_id,
    }


async def reject_shipment_request(
    req_id: int,
    boss_user_id: int,
    boss_name: str,
    bot: Any,
) -> dict:
    """Отклонить заявку: атомарный UPDATE + уведомление менеджеру.

    Возвращает {ok, error, req_id, order_id, now}.
    """
    from services import async_db as adb
    from utils.helpers import local_now

    req = await adb.get_shipment_request(req_id)
    if not req:
        return {"ok": False, "error": "Заявка не найдена", "req_id": req_id, "order_id": None}
    if req["status"] != "pending":
        return {
            "ok": False, "error": "Заявка уже обработана",
            "req_id": req_id, "order_id": req.get("order_id"),
        }

    ok = await adb.reject_shipment_request(req_id, boss_user_id, boss_name)
    if not ok:
        return {
            "ok": False, "error": "Заявка уже обработана другим пользователем",
            "req_id": req_id, "order_id": req.get("order_id"),
        }

    now_str = local_now().strftime("%d.%m.%Y %H:%M")

    if bot is not None and req.get("user_id"):
        from services.notify import notify_order_rejected
        try:
            await notify_order_rejected(
                bot, req["user_id"], req_id, boss_name, now_str,
            )
        except Exception:
            logger.exception("notify_order_rejected failed for req #%s", req_id)

    return {
        "ok": True,
        "error": None,
        "req_id": req_id,
        "order_id": req.get("order_id"),
        "now": now_str,
    }
