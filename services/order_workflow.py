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
from decimal import Decimal
from typing import Any

from services import money

logger = logging.getLogger(__name__)

# Допустимые переходы: текущий_статус → список разрешённых следующих.
# IMPLEMENTATION.md §5.3 (адаптировано). Новые статусы добавлены аддитивно,
# существующие рёбра сохранены — старые тесты не ломаются.
TRANSITIONS: dict[str, list[str]] = {
    "draft": ["pending", "rejected"],  # отправить или отменить (удалить)
    "pending": ["approved", "rejected", "draft"],  # +draft = reject с комментарием
    "approved": ["shipped", "cancelled"],  # отгрузка или отмена (4ч-окно)
    "shipped": ["paid", "cancelled", "partially_returned", "returned"],
    "paid": ["partially_returned", "returned"],
    "partially_returned": ["returned"],
    "rejected": [],
    "cancelled": [],
    "returned": [],
}

# Рёбра возвратов — общие для boss/warehouse_keeper/admin.
_RETURN_EDGES = {
    "shipped→partially_returned",
    "shipped→returned",
    "paid→partially_returned",
    "paid→returned",
    "partially_returned→returned",
}
_BOSS_EDGES = {
    "pending→approved",
    "pending→rejected",
    "pending→draft",
    "approved→shipped",
    "approved→cancelled",
    "shipped→paid",
    "shipped→cancelled",
} | _RETURN_EDGES

# Какие роли могут инициировать какие переходы (ключ "from→to").
_ROLE_TRANSITIONS: dict[str, set[str]] = {
    "manager": {"draft→pending"},
    "boss": set(_BOSS_EDGES),
    # Бухгалтер: подтверждает поступление денег (shipped→paid).
    "bookkeeper": {"shipped→paid"},
    # Кладовщик: фиксирует отгрузку и обрабатывает возвраты.
    "warehouse_keeper": {"approved→shipped"} | _RETURN_EDGES,
    # Админ — всё вышеперечисленное + удаление черновика.
    "admin": _BOSS_EDGES | {"draft→pending", "draft→rejected"},
    "guest": set(),
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


def _items_key(item: dict) -> str:
    """Ключ позиции для diff'а: предпочитаем product_href, иначе имя."""
    return str(item.get("product_href") or item.get("product_name") or "")


def compute_resubmit_summary(
    before_items: list[dict],
    after_items: list[dict],
    before_total: float,
    after_total: float,
    payment_type_changed: bool = False,
) -> dict:
    """Diff между до-reject и текущим состоянием заказа (IMPLEMENTATION.md §6.5).

    Чистая функция (без БД) — легко тестировать. Позиции матчатся по
    product_href (fallback — имя); modified = совпал ключ, но изменились
    qty/price.
    """
    before_by = {_items_key(i): i for i in before_items}
    after_by = {_items_key(i): i for i in after_items}

    added = len(after_by.keys() - before_by.keys())
    removed = len(before_by.keys() - after_by.keys())
    modified = 0
    for key in before_by.keys() & after_by.keys():
        b, a = before_by[key], after_by[key]
        # Количество дробное — точное сравнение через Decimal(str()).
        qty_changed = Decimal(str(b.get("quantity", 0) or 0)) != Decimal(
            str(a.get("quantity", 0) or 0)
        )
        # Цена — в копейках: float `!=` давал ложные «изменено» из-за дрейфа.
        price_changed = money.to_cents(b.get("price", 0) or 0) != money.to_cents(
            a.get("price", 0) or 0
        )
        if qty_changed or price_changed:
            modified += 1

    before_cents = money.to_cents(before_total)
    after_cents = money.to_cents(after_total)
    return {
        "items_added": added,
        "items_removed": removed,
        "items_modified": modified,
        # Канонические копейки.
        "total_before_cents": before_cents,
        "total_after_cents": after_cents,
        "total_diff_cents": after_cents - before_cents,
        # Legacy float-поля — для совместимости текущих вызывающих/тестов.
        "total_before": round(float(before_total), 2),
        "total_after": round(float(after_total), 2),
        "total_diff": round(float(after_total) - float(before_total), 2),
        "payment_type_changed": bool(payment_type_changed),
    }


async def order_credit_context(order: dict, total: float) -> dict | None:
    """Кредит-контекст клиента для заказа (для показа боссу при одобрении).
    None для paid-заказов / без agent_id.

    ВАЖНО про double-count: get_agent_current_debt суммирует заказы в статусах
    pending/approved/shipped/partially_returned. Если ЭТОТ заказ уже в таком
    статусе — он УЖЕ в current_debt, поэтому «долг с учётом заявки» = current_debt
    (не current_debt + total, иначе заказ посчитается дважды). Для draft (не
    считается в долге) — current_debt + total. Возвращает
    {current_debt, limit, effective_debt, over_limit}."""
    from services import async_db as adb

    if (order.get("payment_type") or "paid") != "credit" or not order.get("agent_id"):
        return None
    chk = await adb.check_credit_limit(order["agent_id"], total, order.get("currency"))
    counted = order.get("status") in {"pending", "approved", "shipped", "partially_returned"}
    # effective в базовой валюте: для уже-учтённых статусов = current_debt; иначе
    # current_debt + сумма заказа в базовой (chk["projected"]).
    effective = float(chk["current_debt"]) if counted else float(chk["projected"])
    return {
        "current_debt": float(chk["current_debt"]),
        "limit": float(chk["limit"]),
        "effective_debt": effective,
        "over_limit": effective > float(chk["limit"]),
    }


async def resubmit_diff_line(order_id: int, items: list[dict]) -> str:
    """Строка-сводка изменений с момента прошлого reject→draft (#30). Возвращает
    '' если заказ не реджектился ранее. Показывается боссу в уведомлении о
    переотправленной заявке — видно, что менеджер реально исправил."""
    from services import async_db as adb

    snap = await adb.get_last_reject_snapshot(order_id)
    if not snap:
        return ""
    before_items = snap.get("items", [])
    before_total = float(snap.get("total", 0) or 0)
    after_total = sum(
        float(it.get("quantity", 0) or 0) * float(it.get("price", 0) or 0) for it in items
    )
    s = compute_resubmit_summary(before_items, items, before_total, after_total)
    parts = []
    if s["items_added"]:
        parts.append(f"+{s['items_added']} поз")
    if s["items_removed"]:
        parts.append(f"−{s['items_removed']} поз")
    if s["items_modified"]:
        parts.append(f"~{s['items_modified']} изм")
    changes = ", ".join(parts) if parts else "позиции без изменений"
    diff = s["total_diff"]
    if abs(diff) >= 0.005:
        sign = "+" if diff > 0 else "−"
        money_part = f"сумма {sign}{abs(diff):.0f}"
    else:
        money_part = "сумма без изменений"
    return f"\n♻️ <b>После доработки:</b> {changes}; {money_part}"


# ─── Полный жизненный цикл апрува/реджекта ──────────────────────────────────
#
# До рефакторинга логика жила в handlers/orders.py:cb_approve_request
# (~250 строк) и продублировать её в webapp endpoint'е было неподъёмно.
# Теперь — один сервис, который вызывают и Telegram callback, и /api/.



# Человекочитаемые названия статусов — для объяснения боссу, почему кнопка
# из старого сообщения больше не срабатывает.
_STATUS_RU: dict[str, str] = {
    "draft": "черновик",
    "pending": "на согласовании",
    "approved": "одобрен",
    "shipped": "отгружен",
    "paid": "оплачен",
    "rejected": "отклонён",
    "cancelled": "отменён",
    "partially_returned": "частично возвращён",
    "returned": "возвращён",
}


def _decision_error(decision, order_id) -> str:
    """Сообщение боссу по отказу CAS (T2.2).

    Различаем два случая: заявку разобрал другой человек — или заказ уехал в
    другой статус (напр. МойСклад прислал Unsuccessful и заказ стал rejected),
    а кнопка осталась в старом сообщении чата.
    """
    if decision.reason == "order_moved":
        human = _STATUS_RU.get(decision.order_status or "", decision.order_status or "?")
        return (
            f"Заказ #{order_id} уже в статусе «{human}» — решение по заявке "
            f"больше не применимо. Откройте заказ и посмотрите актуальное состояние."
        )
    return "Заявка уже обработана другим пользователем"


async def approve_shipment_request(
    req_id: int,
    boss_user_id: int,
    boss_name: str,
    bot: Any,
    override: bool = False,
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
            "ok": False,
            "error": "Заявка уже обработана",
            "req_id": req_id,
            "order_id": req.get("order_id"),
        }

    # Энфорс кредитного лимита: для credit-заказов проверяем превышение ДО
    # одобрения. over_limit НЕ блокирует жёстко — требует явного override боссом
    # (наименее разрушительный дефолт). Уже-override'нутый заказ не перепроверяем.
    over_info = None
    order_pre = await adb.get_order(req["order_id"])
    if (
        order_pre
        and order_pre.get("agent_id")
        and (order_pre.get("payment_type") or "paid") == "credit"
        and not order_pre.get("credit_limit_override")
    ):
        pre_items = await adb.get_order_items(req["order_id"])
        total = sum(
            float(it.get("quantity", 0)) * float(it.get("price", 0) or 0) for it in pre_items
        )
        chk = await adb.check_credit_limit(order_pre["agent_id"], total, order_pre.get("currency"))
        # Заказ на этом шаге УЖЕ в статусе pending → его сумма уже входит в
        # current_debt (get_agent_current_debt считает pending). check_credit_limit
        # прибавляет сумму заказа ещё раз → двойной счёт и ложное «превышение».
        # Для уже-учтённого статуса прогноз = current_debt (всё в базовой валюте).
        _counted = order_pre.get("status") in {
            "pending", "approved", "shipped", "partially_returned",
        }
        if _counted:
            chk["projected"] = chk["current_debt"]
            chk["over_limit"] = chk["current_debt"] > chk["limit"]
        if chk.get("over_limit"):
            over_info = chk
            if not override:
                return {
                    "ok": False,
                    "error": "Превышение кредитного лимита",
                    "needs_override": True,
                    "over": chk,
                    "req_id": req_id,
                    "order_id": req["order_id"],
                }

    # Атомарный UPDATE ... WHERE status='pending' — защита от race condition,
    # когда два босса одновременно жмут «Одобрить». Только один из них
    # получит rowcount==1, остальные — False.
    decision = await adb.approve_shipment_request(req_id, boss_user_id, boss_name)
    if not decision.applied:
        return {
            "ok": False,
            "error": _decision_error(decision, req.get("order_id")),
            "req_id": req_id,
            "order_id": req.get("order_id"),
        }

    now_str = local_now().strftime("%d.%m.%Y %H:%M")

    order, boss_role = await asyncio.gather(
        adb.get_order(req["order_id"]),
        adb.get_role(boss_user_id),
    )
    # Одобрено с превышением лимита → фиксируем override + аудит.
    if override and over_info:
        await adb.set_order_credit_override(req["order_id"], boss_user_id)
        await adb.add_audit_log(
            boss_user_id,
            boss_name,
            boss_role,
            "credit_override",
            f"Заявка #{req_id}: одобрено с превышением лимита "
            f"(долг {over_info['current_debt']:.0f}, лимит {over_info['limit']:.0f}, "
            f"проекция {over_info['projected']:.0f})",
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
    # Этап 1a: позиции без product_href молча выпадали из МС-документа (в МС
    # заказ меньше локального). Собираем их и показываем боссу предупреждением.
    skipped_names: list[str] = []

    if order and items and ms_ready():
        co_result = await create_customerorder_from_request(
            order,
            items,
            manager_name,
            telegram_user_id=manager_user_id,
        )
        for nm in co_result.get("skipped") or []:
            if nm not in skipped_names:
                skipped_names.append(nm)
        if co_result.get("ok"):
            co_id = co_result.get("customerorder_id")
            # Audit СРАЗУ после успешного POST в МойСклад — до того
            # как пытаемся сохранить co_id в БД (см. SECURITY.md H6).
            if co_id:
                await asyncio.gather(
                    adb.add_audit_log(
                        boss_user_id,
                        boss_name,
                        boss_role,
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
                order,
                items,
                manager_name,
                telegram_user_id=manager_user_id,
                customerorder_href=co_href,
            )
            for nm in demand_result.get("skipped") or []:
                if nm not in skipped_names:
                    skipped_names.append(nm)
            if demand_result.get("ok"):
                demand_id = demand_result.get("demand_id")
                if demand_id:
                    await asyncio.gather(
                        adb.add_audit_log(
                            boss_user_id,
                            boss_name,
                            boss_role,
                            "ms_demand_created",
                            f"Заявка #{req_id} → demand {demand_id} (до db-write)",
                        ),
                        adb.set_order_ms_demand_id(order["id"], demand_id),
                    )
                # R4: отгрузка прошла — снимаем флаг «нужна доделка», если стоял
                # (напр. demand упал на прошлой попытке, эта — успешна).
                await adb.clear_order_ms_demand_failed(order["id"])
                demand_line = (
                    "\n📄 Заказ покупателя создан в МойСклад"
                    "\n📦 Отгрузка проведена, остатки списаны"
                )
                if pdf_to_send:
                    demand_line += " — печатная форма ниже 👇"
                await adb.add_audit_log(
                    boss_user_id,
                    boss_name,
                    boss_role,
                    "ms_co_and_demand_created",
                    f"Заявка #{req_id} → customerorder {co_id} + demand {demand_id}",
                )
            else:
                reason = demand_result.get("reason", "неизвестная ошибка")
                logger.warning(
                    "Customerorder создан, но demand упал для #%s: %s",
                    req_id,
                    reason,
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
                    boss_user_id,
                    boss_name,
                    boss_role,
                    "ms_demand_failed",
                    f"Заявка #{req_id} → CO {co_id} ok, demand fail: {reason[:200]}",
                )
                # R4: персистентный флаг — заказ попадёт в ночной дайджест
                # «нужна доделка demand», даже если уведомление боссу потеряется.
                await adb.set_order_ms_demand_failed(order["id"])
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
                req_id,
                reason,
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
            "MS context не готов — не создаём документы для заявки #%s",
            req_id,
        )

    # Этап 1a: если часть позиций не попала в МС-документ (нет product_href) —
    # предупреждаем явно, иначе расхождение «локальный заказ > МС» молчит.
    if skipped_names and co_id:
        preview = ", ".join(esc(n) for n in skipped_names[:5])
        more = f" +{len(skipped_names) - 5}" if len(skipped_names) > 5 else ""
        demand_line += (
            f"\n⚠️ <b>{len(skipped_names)} поз. не попали в МойСклад</b> "
            f"(нет привязки к товару МС): {preview}{more}.\n"
            f"Добавьте их в документ вручную."
        )
        await adb.add_audit_log(
            boss_user_id,
            boss_name,
            boss_role,
            "ms_positions_skipped",
            f"Заявка #{req_id} → CO {co_id}: пропущено {len(skipped_names)} поз. "
            f"без product_href: {', '.join(skipped_names[:10])}",
        )

    # Уведомляем менеджера
    if bot is not None and req.get("user_id"):
        from services.notify import notify_order_approved

        try:
            await notify_order_approved(
                bot,
                req["user_id"],
                req_id,
                boss_name,
                now_str,
                demand_line,
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
                    chat_id=req["user_id"],
                    document=file1,
                    caption=caption,
                )
            except Exception:
                logger.exception("Не удалось отправить PDF менеджеру")
            if boss_user_id != req["user_id"]:
                try:
                    file2 = BufferedInputFile(pdf_bytes, filename=pdf_name)
                    await bot.send_document(
                        chat_id=boss_user_id,
                        document=file2,
                        caption=caption,
                    )
                except Exception:
                    logger.exception("Не удалось отправить PDF боссу")
        except Exception:
            logger.exception("PDF dispatch failed for req #%s", req_id)

    # Для paid-заказов автоматически создаём payment-pending,
    # чтобы босс одной кнопкой зафиксировал реальное получение денег.
    if order and (order.get("payment_type") or "paid") == "paid":
        total = sum(float(it.get("quantity", 0)) * float(it.get("price", 0) or 0) for it in items)
        if total > 0.01:
            currency = order.get("currency") or "USD"
            try:
                existing = [
                    p
                    for p in await adb.get_payments_for_order(order["id"])
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
                            bot,
                            order["id"],
                            manager_name,
                            payment_id,
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
            "ok": False,
            "error": "Заявка уже обработана",
            "req_id": req_id,
            "order_id": req.get("order_id"),
        }

    decision = await adb.reject_shipment_request(req_id, boss_user_id, boss_name)
    if not decision.applied:
        return {
            "ok": False,
            "error": _decision_error(decision, req.get("order_id")),
            "req_id": req_id,
            "order_id": req.get("order_id"),
        }

    now_str = local_now().strftime("%d.%m.%Y %H:%M")

    if bot is not None and req.get("user_id"):
        from services.notify import notify_order_rejected

        try:
            await notify_order_rejected(
                bot,
                req["user_id"],
                req_id,
                boss_name,
                now_str,
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


async def return_order_to_draft(
    req_id: int,
    boss_user_id: int,
    boss_name: str,
    comment: str,
    bot: Any,
) -> dict:
    """Вернуть заявку менеджеру на доработку (IMPLEMENTATION.md §6.4–6.5).

    В отличие от reject_shipment_request (terminal: заказ → 'rejected'), здесь
    заказ возвращается в 'draft' с причиной и счётчиком; после reject_max_cycles
    заказ замораживается. Менеджер правит черновик и отправляет заново.

    Порядок: сперва атомарный reject_order_to_draft (race-guard на orders), и
    только при успехе помечаем заявку 'returned'. Если другой босс уже обработал
    заказ — reject_order_to_draft вернёт ошибку, заявку не трогаем.

    Возвращает {ok, error, req_id, order_id, now, frozen, rejection_count}.
    """
    from services import async_db as adb
    from services import database as db
    from utils.helpers import local_now

    req = await adb.get_shipment_request(req_id)
    if not req:
        return {"ok": False, "error": "Заявка не найдена", "req_id": req_id, "order_id": None}
    if req["status"] != "pending":
        return {
            "ok": False,
            "error": "Заявка уже обработана",
            "req_id": req_id,
            "order_id": req.get("order_id"),
        }

    order_id = req["order_id"]
    res = await db.reject_order_to_draft(order_id, boss_user_id, boss_name, comment)
    if not res.get("ok"):
        return {
            "ok": False,
            "error": res.get("error") or "Не удалось вернуть заказ в черновик",
            "req_id": req_id,
            "order_id": order_id,
        }

    # Заказ уже в 'draft' — снимаем заявку с pending-очереди (статус заказа не трогаем).
    await asyncio.to_thread(
        db.mark_shipment_request_returned, req_id, boss_user_id, boss_name
    )

    now_str = local_now().strftime("%d.%m.%Y %H:%M")
    frozen = bool(res.get("frozen"))
    rejection_count = int(res.get("rejection_count") or 0)

    if bot is not None and req.get("user_id"):
        from services.notify import notify_order_returned

        try:
            await notify_order_returned(
                bot,
                req["user_id"],
                req_id,
                boss_name,
                comment,
                now_str,
                frozen,
                rejection_count,
            )
        except Exception:
            logger.exception("notify_order_returned failed for req #%s", req_id)

    return {
        "ok": True,
        "error": None,
        "req_id": req_id,
        "order_id": order_id,
        "now": now_str,
        "frozen": frozen,
        "rejection_count": rejection_count,
    }
