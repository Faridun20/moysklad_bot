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

from services import adb_core, money

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
    """Ключ позиции для diff'а: предпочитаем карточку товара, иначе имя.

    `product_id` — привязка к нашей номенклатуре, `product_href` остался у
    позиций, заведённых до перехода со МойСклад: оба однозначно опознают товар,
    имя — уже нет («Кабель PV 0.6» и «Кабель PV 0,6» это одна позиция, которую
    переименовали, а не добавленная и удалённая).
    """
    pid = item.get("product_id")
    if pid:
        return f"p:{pid}"
    return str(item.get("product_href") or item.get("product_name") or "")


async def _build_invoice_pdf(invoice_id: int | None, order_id: int) -> tuple[bytes, str] | None:
    """Печатная форма накладной: (bytes, имя файла) или None.

    Best-effort: заказ уже одобрен и склад списан, и отсутствие PDF не повод
    ронять весь апрув. WeasyPrint синхронный и тяжёлый — уводим в поток, иначе
    он держит event loop на время рендера.
    """
    if not invoice_id:
        return None
    try:
        from services import warehouse
        from services.invoice_pdf import invoice_filename, render_invoice_pdf

        invoice = await warehouse.get_invoice(int(invoice_id))
        if not invoice:
            return None
        pdf = await asyncio.to_thread(render_invoice_pdf, invoice)
        return pdf, invoice_filename(invoice)
    except Exception:
        logger.exception("PDF накладной по заказу #%s не собран", order_id)
        return None


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

# ─── Submit заявки на отгрузку ───────────────────────────────────────────────


class _SubmitAbort(Exception):
    """Откат транзакции сабмита с сообщением пользователю."""

    def __init__(self, message: str, status: str | None = None):
        super().__init__(message)
        self.message = message
        self.status = status


def validate_payment_terms(
    payment_type: str | None, due_date: str | None
) -> tuple[str, str | None, str | None]:
    """Нормализовать и проверить условия оплаты. → (payment_type, due_date, error).

    Единая валидация для обоих входов. Раньше она была только в WebApp, а бот
    сабмитил вообще не трогая payment_type — заказ оставался на схемном
    дефолте 'paid', и рассрочка молча учитывалась как оплаченная (§5.2.3).
    """
    from datetime import date

    ptype = (payment_type or "paid").lower()
    due = (due_date or "").strip() or None
    if ptype not in ("paid", "credit"):
        return ptype, due, "Неверный тип оплаты"
    if ptype != "credit":
        # Для paid срок долга не имеет смысла — обнуляем, чтобы не тащить
        # хвост от прошлого credit-состояния заказа.
        return ptype, None, None
    if not due:
        return ptype, due, "Укажите дату возврата долга"
    try:
        parsed = date.fromisoformat(due)
    except ValueError:
        return ptype, due, "Неверный формат даты (нужно YYYY-MM-DD)"
    if parsed < date.today():
        return ptype, due, "Дата возврата не может быть в прошлом"
    return ptype, due, None


def _is_unique_violation(exc: BaseException) -> bool:
    """Нарушение UNIQUE — на обоих бэкендах (sqlite3.IntegrityError /
    asyncpg.UniqueViolationError). Ловим по типу и по тексту, чтобы не тащить
    в импорты драйвер, которого может не быть."""
    name = type(exc).__name__
    if name in ("IntegrityError", "UniqueViolationError"):
        return True
    text = str(exc).lower()
    return "unique" in text and "constraint" in text


async def submit_order(
    order_id: int,
    user_id: int,
    full_name: str,
    *,
    payment_type: str | None = None,
    due_date: str | None = None,
    comment: str = "",
    idem_key: str | None = None,
) -> dict:
    """Отправить заказ на согласование. ЕДИНСТВЕННЫЙ путь сабмита (T2.3).

    Всё в одной транзакции: SELECT ... FOR UPDATE на заказе, проверка
    status='draft', запись типа оплаты, перевод в 'pending' с submitted_at,
    вставка заявки. Либо всё, либо ничего.

    Что чинится:
      • бот не записывал payment_type — рассрочка учитывалась как оплаченная
        (§5.2.3, схемный дефолт 'paid');
      • create_shipment_request и update_order_status шли двумя транзакциями:
        краш между ними оставлял заявку 'pending' при заказе 'draft', а двойной
        тап успевал создать ДВЕ заявки (§2.1) — теперь CAS на draft отсекает
        второй сабмит, а уникальный индекс из T1.8 страхует на уровне БД;
      • submitted_at не записывался вовсе (§2.11), из-за чего переотправленный
        заказ немедленно объявлялся «зависшей заявкой» по старому created_at.

    submitted_at пишем `now_str()` — в ОДНОМ кадре с created_at (локальное
    наивное время). get_stale_pending_orders сравнивает
    COALESCE(submitted_at, created_at) с порогом, посчитанным в Python через
    datetime.now(); UTC здесь разъехался бы с локальным на величину смещения
    и досрочно помечал заявки зависшими.

    Возвращает {"ok": True, "req_id": N, "payment_type", "due_date"} либо
    {"ok": False, "error": "...", "status": <текущий статус>}.
    """
    from services import async_db as adb
    from services.database import USE_POSTGRES, now_str

    ptype, due, err = validate_payment_terms(payment_type, due_date)
    if err:
        return {"ok": False, "error": err}

    if idem_key:
        cached = await adb.idem_claim(idem_key, "order_submit", user_id)
        if cached is not None:
            # Ключ уже застолблён — повторная отправка того же запроса.
            return cached or {"ok": False, "error": "Заявка уже отправлена"}

    result: dict
    try:
        async with adb_core.transaction() as txn:
            lock = " FOR UPDATE" if USE_POSTGRES else ""
            order = await txn.fetchrow(
                f"SELECT id, status, agent_name, frozen FROM orders WHERE id = $1{lock}",
                order_id,
            )
            if not order:
                raise _SubmitAbort("Заказ не найден")
            if order["status"] != "draft":
                raise _SubmitAbort(
                    "Заказ уже отправлен — повторная отправка не нужна",
                    status=order["status"],
                )
            if order.get("frozen"):
                raise _SubmitAbort(
                    "Заказ заморожен после серии отклонений — обратитесь к администратору"
                )
            if not (order.get("agent_name") or "").strip():
                raise _SubmitAbort("Выберите клиента")

            n_items = int(
                await txn.fetchval(
                    "SELECT COUNT(*) FROM order_items WHERE order_id = $1", order_id
                )
                or 0
            )
            if n_items == 0:
                raise _SubmitAbort("Добавьте товары")

            stamp = now_str()
            moved = await txn.execute(
                "UPDATE orders SET status = 'pending', payment_type = $1, due_date = $2, "
                "submitted_at = $3, updated_at = $4 WHERE id = $5 AND status = 'draft'",
                ptype, due, stamp, stamp, order_id,
            )
            if not moved:
                # Кто-то успел между SELECT и UPDATE (на SQLite нет FOR UPDATE).
                raise _SubmitAbort("Заказ уже отправлен — повторная отправка не нужна")

            await txn.execute(
                "INSERT INTO shipment_requests "
                "(order_id, user_id, full_name, status, comment, created_at) "
                "VALUES ($1, $2, $3, 'pending', $4, $5)",
                order_id, user_id, full_name, comment, stamp,
            )
            req_id = int(
                await txn.fetchval(
                    "SELECT id FROM shipment_requests WHERE order_id = $1 "
                    "AND status = 'pending'",
                    order_id,
                )
                or 0
            )
            result = {"ok": True, "req_id": req_id, "payment_type": ptype, "due_date": due}
    except _SubmitAbort as e:
        result = {"ok": False, "error": e.message, "status": e.status}
    except Exception as e:  # noqa: BLE001 — нужен именно разбор причины
        if not _is_unique_violation(e):
            raise
        # Уникальный индекс из T1.8: вторая pending-заявка по тому же заказу.
        # Это не 500, а «уже отправлено».
        logger.info("submit_order: заявка по заказу #%s уже существует", order_id)
        result = {"ok": False, "error": "Заявка уже отправлена"}

    if idem_key:
        await adb.idem_store(idem_key, result)
    return result




# ─── Отмена заказа ───────────────────────────────────────────────────────────


async def cancel_order_full(
    order_id: int, user_id: int, user_name: str, reason: str
) -> dict:
    """Отменить заказ и вернуть списанный товар на склад. Общий код для обоих
    входов — бота и `/api/orders/cancel` (T2.6).

    Раньше откат делал только бот, а WebApp — нет, и отменённый оттуда заказ
    оставлял в МойСклад живой customerorder с резервом товара НАВСЕГДА (§5.2.2).
    Теперь откатывать нужно СВОЮ расходную накладную, и забыть про это стоило бы
    ещё дороже: товар остался бы списанным по отменённому заказу.

    Возврат остатка — best-effort и намеренно ПОСЛЕ локальной отмены: ошибка
    склада не должна откатывать то, что оператор уже подтвердил. Отмена
    накладной идемпотентна (повторный вызов вернёт `already_cancelled`).

    Возвращает результат `cancel_order` плюс `stock_reverse` — что вышло со
    складом (для логов и текста оператору).
    """
    from services import async_db as adb

    res = await adb.cancel_order(order_id, user_id, user_name, reason)
    if not res.get("ok"):
        return res

    from services import order_shipment

    try:
        rev = await order_shipment.cancel_shipment(order_id, user_id=user_id)
    except Exception as e:  # noqa: BLE001 — отмена уже применена, склад догоним
        logger.warning("Возврат остатка по заказу #%s не прошёл", order_id, exc_info=True)
        rev = {"ok": False, "reason": type(e).__name__}
    if not rev.get("ok"):
        logger.warning(
            "Заказ #%s отменён, но остаток не вернулся на склад: %s",
            order_id, rev.get("reason"),
        )
    return {**res, "stock_reverse": rev}


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
          "invoice_id": int | None,      # расходная накладная склада
          "invoice_number": str | None,
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

    # Отгрузка — расходная накладная нашего склада. Раньше здесь создавалась
    # пара документов в МойСклад: customerorder (ради печатной формы) и
    # связанный с ним demand (ради списания остатка). Оба документа теперь
    # наши, и пары не нужно: локальная накладная и печатается, и двигает склад.
    from services import order_shipment

    demand_line = ""
    pdf_to_send: tuple[bytes, str] | None = None
    invoice_id: int | None = None
    invoice_number: str | None = None
    # Позиции без карточки номенклатуры в накладную не попадают. Молчать об
    # этом нельзя: недосписанный заказ — это расхождение склада.
    skipped_names: list[str] = []

    if order and items:
        # Идемпотентно по `order_shipment.order_id`: повторное одобрение (два
        # босса, ретрай, старая кнопка) не спишет товар второй раз.
        ship = await order_shipment.ship_order(order, items, user_id=boss_user_id)
        skipped_names = list(ship.get("skipped") or [])

        if ship.get("ok"):
            invoice_id = ship.get("invoice_id")
            invoice_number = ship.get("invoice_number")
            if ship.get("already_shipped"):
                logger.info(
                    "Заявка #%s: накладная по заказу уже есть — повторно не списываем", req_id
                )
            else:
                await adb.add_audit_log(
                    boss_user_id,
                    boss_name,
                    boss_role,
                    "order_shipped",
                    f"Заявка #{req_id} → накладная {invoice_number} (#{invoice_id})",
                )
                demand_line = (
                    f"\n📦 Накладная {esc(str(invoice_number))} проведена, остатки списаны"
                )
                pdf_to_send = await _build_invoice_pdf(invoice_id, order["id"])
                if pdf_to_send:
                    demand_line += " — печатная форма ниже 👇"
        else:
            reason = ship.get("reason", "неизвестная ошибка")
            logger.warning("Заявка #%s одобрена, но склад не списан: %s", req_id, reason)
            demand_line = (
                f"\n⚠️ <b>Остатки НЕ списаны:</b>\n"
                f"<code>{esc(reason[:300])}</code>\n"
                f"Заявка одобрена — накладную нужно провести вручную."
            )
            await adb.add_audit_log(
                boss_user_id,
                boss_name,
                boss_role,
                "order_shipment_failed",
                f"Заявка #{req_id}: {reason[:200]}",
            )
            if bot is not None:
                try:
                    await bot.send_message(
                        boss_user_id,
                        f"⚠️ Склад не списан по заявке #{req_id}:\n"
                        f"<code>{esc(reason[:500])}</code>",
                        parse_mode="HTML",
                    )
                except Exception:
                    pass

    if skipped_names:
        preview = ", ".join(esc(n) for n in skipped_names[:5])
        more = f" +{len(skipped_names) - 5}" if len(skipped_names) > 5 else ""
        demand_line += (
            f"\n⚠️ <b>{len(skipped_names)} поз. не списано</b> "
            f"(нет карточки в номенклатуре): {preview}{more}.\n"
            f"Заведите товар и проведите накладную вручную."
        )
        await adb.add_audit_log(
            boss_user_id,
            boss_name,
            boss_role,
            "shipment_positions_skipped",
            f"Заявка #{req_id}: пропущено {len(skipped_names)} поз. без карточки: "
            f"{', '.join(skipped_names[:10])}",
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
                        from services.notify import notify_payment_confirmation_needed

                        await notify_payment_confirmation_needed(
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
        "invoice_id": invoice_id,
        "invoice_number": invoice_number,
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
