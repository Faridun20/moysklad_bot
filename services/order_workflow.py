"""
Order state machine — единый источник правил переходов и прав.

Вместо разбросанных проверок `is_boss()` + ручного UPDATE status
используется:
  - TRANSITIONS — что за чем может следовать
  - ROLE_FOR_TRANSITION — кто имеет право инициировать переход
  - validate_transition() — синхронная проверка допустимости (без DB)
  - can_transition() — проверка прав роли на конкретный переход
  - approve_shipment_request() / reject_shipment_request() — полный
    жизненный цикл апрува/реджекта одной заявки (DB + накладная +
    уведомления + PDF). Вызывается и из bot handler'а, и из webapp
    endpoint — единственная точка истины.
  - ship_order_now() — «Отгрузить» без одобрения: те же шаги (заявка,
    одобрение, накладная, оплата, отгрузка) одной операцией менеджера;
    руководитель нужен только при скидке выше порога или долге сверх лимита.
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
    # Совмещение ролей (менеджер пока замещает кладовщика и бухгалтера) —
    # рёбра замещаемых ролей добавляются к своим.
    from services.roles import effective_roles

    return any(key in _ROLE_TRANSITIONS.get(r, set()) for r in effective_roles(role))


def validate_transition(order: dict, new_status: str) -> str | None:
    """Вернуть строку ошибки если переход недопустим, иначе None.

    Используется для pre-check ДО обращения к БД — быстрый fail-fast.
    """
    current = order.get("status", "")
    if new_status not in TRANSITIONS.get(current, []):
        return (
            f"Заказ «{_STATUS_RU.get(current, current)}» нельзя перевести "
            f"в «{_STATUS_RU.get(new_status, new_status)}»"
        )
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


def _print_keyboard(invoice_id: int | None, lang: str | None = None):
    """Кнопки «Распечатать» под печатной формой. `None` — печать недоступна.

    Кнопки нет, если в контейнере не стоит клиент CUPS: обещать действие,
    которое гарантированно ответит отказом, хуже, чем не предлагать его.
    Язык товарной накладной выбирают здесь же: первая кнопка — язык, который
    человек выбирал последним (одно касание), ниже — два других.
    """
    from services import printing
    from services.invoice_pdf import DOC_LANG_LABELS, DOC_LANGS, normalize_lang

    if not invoice_id or not printing.is_available():
        return None
    from aiogram.utils.keyboard import InlineKeyboardBuilder

    first = normalize_lang(lang) or DOC_LANGS[0]
    kb = InlineKeyboardBuilder()
    kb.button(
        text=f"🖨 Распечатать · {DOC_LANG_LABELS[first]}",
        callback_data=printing.invoice_callback(int(invoice_id), first),
    )
    for other in DOC_LANGS:
        if other != first:
            kb.button(text=f"🖨 {DOC_LANG_LABELS[other]}", callback_data=printing.invoice_callback(int(invoice_id), other))
    kb.adjust(1, 2)
    return kb.as_markup()


async def deliver_shipment_pdf(
    bot: Any, *, invoice_id: int, order_id: int, req_id: int, manager_id: int | None, boss_id: int
) -> dict:
    """Собрать печатную форму накладной и разослать менеджеру и боссу.

    Работа ПОСЛЕ коммита отгрузки и ВНЕ ответа на одобрение: накладная уже
    проведена, остатки списаны, и ждать рендера weasyprint (сотни миллисекунд
    CPU) плюс двух отправок в Telegram боссу незачем — он видит «одобрено»
    сразу, PDF догоняет следом. Никогда не бросает: отказ PDF — не откат
    одобрения. Возвращает {built, sent_to} для логов и тестов.
    """
    # Язык — тот, что менеджер выбирал последним: накладную клиенту несёт он.
    from services import user_prefs

    lang = await asyncio.to_thread(user_prefs.doc_lang, manager_id or boss_id)
    pdf = await _build_invoice_pdf(invoice_id, order_id, lang)
    if not pdf or bot is None:
        return {"built": bool(pdf), "sent_to": []}
    sent_to = await _send_shipment_pdf(
        bot, pdf, invoice_id=invoice_id, req_id=req_id, manager_id=manager_id, boss_id=boss_id,
        lang=lang,
    )
    return {"built": True, "sent_to": sent_to}


async def _send_shipment_pdf(
    bot: Any, pdf: tuple[bytes, str], *, invoice_id: int, req_id: int,
    manager_id: int | None, boss_id: int, lang: str | None = None,
) -> list[int]:
    """Разослать собранный PDF менеджеру и боссу. Не бросает."""
    sent_to: list[int] = []
    try:
        from aiogram.types import BufferedInputFile

        pdf_bytes, pdf_name = pdf
        caption = f"📄 Печатная форма — заявка #{req_id}"
        # Печать — ПО КНОПКЕ, а не автоматически: половина печатных форм
        # уходит на проверку перед отправкой клиенту, и печатать их все
        # значит переводить бумагу. Формат callback_data — в services.printing,
        # чтобы producer и хендлер не разъехались.
        markup = _print_keyboard(invoice_id, lang)
        recipients = [r for r in (manager_id, boss_id) if r]
        if boss_id == manager_id:
            recipients = [boss_id]
        for chat_id in recipients:
            try:
                await bot.send_document(
                    chat_id=chat_id,
                    document=BufferedInputFile(pdf_bytes, filename=pdf_name),
                    caption=caption,
                    reply_markup=markup,
                )
                sent_to.append(chat_id)
            except Exception:
                logger.exception("Не удалось отправить PDF по заявке #%s в чат %s", req_id, chat_id)
    except Exception:
        logger.exception("PDF dispatch failed for req #%s", req_id)
    return sent_to


async def _build_invoice_pdf(
    invoice_id: int | None, order_id: int, lang: str | None = None,
) -> tuple[bytes, str] | None:
    """Печатная форма накладной: (bytes, имя файла) или None.

    Best-effort: заказ уже одобрен и склад списан, и отсутствие PDF не повод
    ронять весь апрув. WeasyPrint синхронный и тяжёлый — уводим в поток, иначе
    он держит event loop на время рендера.
    """
    if not invoice_id:
        return None
    try:
        from services import warehouse, waybill

        invoice = await warehouse.get_invoice(int(invoice_id))
        if not invoice:
            return None
        # Товарная накладная по бланку: реквизиты, клиент, «Основание: Счёт на
        # оплату № {заказ}» — `waybill.render` (рендер в потоке).
        return await waybill.render(invoice, lang)
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


async def orders_credit_context(orders: list[tuple[dict, float]]) -> dict[int, dict]:
    """То же, что `order_credit_context`, но для СПИСКА заказов двумя запросами.

    Список заявок босса считал контекст по каждой заявке отдельно — долг
    контрагента и лимит, по 6 запросов на строку. Здесь долги и лимиты всех
    контрагентов берутся батчем (`get_agents_current_debt`, `get_credit_limits`),
    а формула — та же, что в одиночной версии. Ключ результата — order id;
    paid-заказы и заказы без agent_id в него не попадают.
    """
    from services import async_db as adb
    from services.database import convert_to_base

    credit = [
        (o, total) for o, total in orders
        if (o.get("payment_type") or "paid") == "credit" and o.get("agent_id")
    ]
    if not credit:
        return {}
    agent_ids = [str(o["agent_id"]) for o, _ in credit]
    debts = await adb.get_agents_current_debt(agent_ids)
    limits = await adb.get_credit_limits(agent_ids)
    out: dict[int, dict] = {}
    for o, total in credit:
        agent = str(o["agent_id"])
        debt = float(debts.get(agent, 0.0))
        limit = float(limits.get(agent, 0.0))
        total_base = total
        if o.get("currency"):
            conv = convert_to_base(total, o["currency"])
            if conv is not None:
                total_base = conv
        counted = o.get("status") in {"pending", "approved", "shipped", "partially_returned"}
        effective = debt if counted else debt + total_base
        out[int(o["id"])] = {
            "current_debt": debt,
            "limit": limit,
            "effective_debt": effective,
            "over_limit": effective > limit,
        }
    return out


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
            if idem_key:
                await adb.idem_release(idem_key)  # сбой до записи — ретрай возможен
            raise
        # Уникальный индекс из T1.8: вторая pending-заявка по тому же заказу.
        # Это не 500, а «уже отправлено».
        logger.info("submit_order: заявка по заказу #%s уже существует", order_id)
        result = {"ok": False, "error": "Заявка уже отправлена"}

    if idem_key:
        if result.get("ok"):
            await adb.idem_store(idem_key, result)
        else:
            # Отказ ничего не записал — ключ освобождаем. Иначе ключ формы (он
            # живёт с черновиком) навсегда отдавал бы «Выберите клиента», даже
            # когда клиента уже выбрали.
            await adb.idem_release(idem_key)
    return result




# ─── Отмена заказа ───────────────────────────────────────────────────────────


async def cancel_order_full(
    order_id: int, user_id: int, user_name: str, reason: str
) -> dict:
    """Отменить заказ и вернуть списанный товар на склад. Общий код для обоих
    входов — бота и `/api/orders/cancel` (T2.6).

    Статус и возврат остатка — одна транзакция (`database.cancel_order`):
    раньше отмена коммитилась первой, а накладная откатывалась «best-effort»
    потом, и сбой между ними оставлял отменённый заказ со списанным навсегда
    товаром. Отказ склада теперь отменяет и саму отмену — текстом оператору.

    Возвращает `{ok, error, stock_reverse}` — что вышло со складом (для логов
    и текста оператору).
    """
    from services import async_db as adb
    from services import order_shipment

    # Историческая отгрузка (МойСклад) — отказ сразу, понятным текстом. Склад
    # и сам откажет в отмене её накладной (warehouse.cancel_invoice_in, code
    # «historical») и откатит всю отмену, но у заказа со 2-й и далее отгрузкой
    # МС накладной в order_shipment нет — ловим по ms_demand_id здесь.
    historical = await order_shipment.historical_cancel_refusal(order_id)
    if historical:
        return {"ok": False, "error": historical, "code": "historical"}

    res = await adb.cancel_order(order_id, user_id, user_name, reason)
    if not res.get("ok") and res.get("stock_reverse"):
        logger.warning(
            "Заказ #%s не отменён — склад отказал: %s",
            order_id, res["stock_reverse"].get("reason"),
        )
    return res


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
    return "Эту заявку уже решил кто-то другой — обновите список"


async def approve_shipment_request(
    req_id: int,
    boss_user_id: int,
    boss_name: str,
    bot: Any,
    override: bool = False,
    *,
    discount_ack: bool = False,
    pdf_delivery: str = "background",
    without_approval: bool = False,
    notify_manager: bool = True,
) -> dict:
    """Полный апрув заявки: DB, склад, уведомления, PDF, авто-payment.

    Параметры:
        req_id        — id заявки в shipment_requests
        boss_user_id  — telegram id одобряющего (для аудита и PDF)
        boss_name     — отображаемое имя одобряющего
        bot           — aiogram.Bot (или совместимый, у которого есть
                        send_message/send_document). Может быть None,
                        тогда уведомления и PDF не отправляются.
        override      — одобрить, несмотря на превышение кредит-лимита
        discount_ack  — одобрить, несмотря на скидку выше порога
                        (`app_settings.order_discount_requires_approval_pct`,
                        services/order_discounts.py). Как и `override`, это
                        ЯВНОЕ второе нажатие одобряющего, а не новый статус.
        pdf_delivery  — "background": печатная форма собирается и
                        рассылается фоновой задачей ПОСЛЕ ответа (в
                        результате — `pdf_task`, его можно дождаться);
                        "inline": как раньше, внутри вызова. Отгрузка и
                        одобрение от режима не зависят.
        without_approval — заявку проводит сама отгрузка
                        (`ship_order_now`), а не решение руководителя: в журнал
                        пишется «оформлена без одобрения».
        notify_manager — слать ли менеджеру «Заявка одобрена» и боссу
                        отдельное «склад не списан». `ship_order_now` отгружает
                        в том же действии и отвечает человеку сам — сообщение
                        «одобрено, внесите оплату» пришло бы уже после отгрузки.

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
        return {"ok": False, "error": "Заявка не найдена — обновите список", "req_id": req_id, "order_id": None}
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
    # Позиции нужны обеим проверкам до одобрения — сумме для кредит-лимита и
    # скидке к прайсу, — поэтому читаются ОДИН раз.
    pre_items = await adb.get_order_items(req["order_id"]) if order_pre else []
    if (
        order_pre
        and order_pre.get("agent_id")
        and (order_pre.get("payment_type") or "paid") == "credit"
        and not order_pre.get("credit_limit_override")
    ):
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

    # Скидка к прайсу выше порога — второе, ЯВНОЕ нажатие одобряющего (C5).
    # Порядок с кредит-лимитом: сначала лимит, потом скидка; заявку, где
    # пробито и то и другое, одобряют двумя подтверждениями подряд, и оба
    # факта уходят в аудит. Нового «нельзя отгрузить» тут не появляется —
    # состояние остаётся одно: заявка ждёт решения руководителя.
    from services import order_discounts

    discount_info: dict | None = None
    if order_pre:
        discount_info = await order_discounts.order_discount(
            pre_items, order_pre.get("currency")
        )
        if discount_info.get("flagged") and not discount_ack:
            refusal = order_discounts.refusal(discount_info, req["order_id"])
            refusal.update({"req_id": req_id, "order_id": req["order_id"]})
            return refusal

    # Атомарный UPDATE ... WHERE status='pending' — защита от race condition,
    # когда два босса одновременно жмут «Одобрить». Только один из них
    # получит rowcount==1, остальные — False.
    decision = await adb.approve_shipment_request(
        req_id, boss_user_id, boss_name, credit_override=bool(override and over_info),
        without_approval=without_approval,
    )
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
    # Одобрено с превышением лимита: отметку override поставило само одобрение
    # (тем же UPDATE'ом заказа), здесь — только аудит.
    if override and over_info:
        await adb.add_audit_log(
            boss_user_id,
            boss_name,
            boss_role,
            "credit_override",
            f"Заявка #{req_id}: одобрено с превышением лимита "
            f"(долг {over_info['current_debt']:.0f}, лимит {over_info['limit']:.0f}, "
            f"проекция {over_info['projected']:.0f})",
        )
    # Скидка выше порога: процент НЕ храним колонкой (устареет при первой же
    # правке прайса) — фиксируем в аудите то, что было в момент решения.
    if discount_info and discount_info.get("flagged"):
        await adb.add_audit_log(
            boss_user_id,
            boss_name,
            boss_role,
            "discount_approved",
            f"Заявка #{req_id} (заказ #{req['order_id']}): одобрено со скидкой "
            f"{discount_info.get('max_pct')}% по позиции, средняя "
            f"{discount_info.get('avg_pct')}% при пороге "
            f"{discount_info.get('threshold_pct')}%",
        )
    items = await adb.get_order_items(req["order_id"]) if order else []

    # Отгрузка — расходная накладная нашего склада. Раньше здесь создавалась
    # пара документов в МойСклад: customerorder (ради печатной формы) и
    # связанный с ним demand (ради списания остатка). Оба документа теперь
    # наши, и пары не нужно: локальная накладная и печатается, и двигает склад.
    from services import order_shipment

    demand_line = ""
    pdf_to_send: tuple[bytes, str] | None = None
    pdf_task: Any = None
    invoice_id: int | None = None
    invoice_number: str | None = None
    # Позиции без карточки номенклатуры в накладную не попадают. Молчать об
    # этом нельзя: недосписанный заказ — это расхождение склада.
    skipped_names: list[str] = []
    # Заказ отменили между одобрением и списанием — ни накладной, ни автоплатежа.
    order_moved = False

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
                    f"Заявка #{req_id} → отгрузка {invoice_number} (#{invoice_id})",
                )
                demand_line = (
                    f"\n📦 Отгрузка {esc(str(invoice_number))} оформлена, товар списан со склада"
                )
                if pdf_delivery == "inline":
                    from services import user_prefs

                    inline_lang = await asyncio.to_thread(
                        user_prefs.doc_lang, req.get("user_id") or boss_user_id
                    )
                    pdf_to_send = await _build_invoice_pdf(invoice_id, order["id"], inline_lang)
                    if pdf_to_send:
                        demand_line += " — печатная форма ниже 👇"
                elif bot is not None and invoice_id:
                    # Рендер и рассылка — фоном: одобрение уже состоялось, и
                    # боссу незачем ждать weasyprint и два вызова Telegram.
                    from utils.background import spawn

                    pdf_task = spawn(
                        deliver_shipment_pdf(
                            bot, invoice_id=int(invoice_id), order_id=int(order["id"]),
                            req_id=req_id, manager_id=req.get("user_id"), boss_id=boss_user_id,
                        ),
                        name=f"shipment-pdf-{req_id}",
                    )
                    demand_line += " — печатная форма придёт следом 👇"
        elif ship.get("code") == "order_moved":
            order_moved = True
            # Заказ успели отменить между одобрением и списанием (проверено под
            # замком строки заказа). Списывать отменённое нельзя, и «нужна
            # доделка» тут нет — докладываем как есть.
            logger.info("Заявка #%s: %s — склад не списываем", req_id, ship.get("reason"))
            demand_line = f"\n⚠️ {esc(str(ship.get('reason') or 'Заказ уже не одобрен'))} — остатки не списаны"
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
            if bot is not None and notify_manager:
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

    # «Оплата сразу»: автоплатежа на всю сумму при одобрении БОЛЬШЕ НЕТ. Он
    # заявлял деньги без способа («неизвестно как»), закрывал весь остаток для
    # сдачи наличных (сдачи уходили «Заказы: —») и позволял отгрузить заказ, не
    # сказав, как клиент заплатил. Теперь заказ ждёт в «одобрен — к отгрузке»,
    # пока менеджер не введёт разбивку (services.order_payments), и только
    # после неё отгрузка (`mark_order_shipped`) проходит.
    if (
        notify_manager and order and not order_moved
        and (order.get("payment_type") or "paid") == "paid"
    ):
        demand_line += (
            "\n💳 <b>Оплата сразу:</b> перед отгрузкой внесите в WebApp, как клиент "
            "заплатил — наличные, карта или перечисление (Продажи → заказ → «Внести оплату»)."
        )

    # Уведомляем менеджера
    if bot is not None and notify_manager and req.get("user_id"):
        from services.notify import approved_order_keyboard, notify_order_approved

        payment_type = (order.get("payment_type") or "paid") if order and not order_moved else None
        try:
            await notify_order_approved(
                bot,
                req["user_id"],
                req_id,
                boss_name,
                now_str,
                demand_line,
                payment_type=payment_type,
                reply_markup=approved_order_keyboard(payment_type) if payment_type else None,
            )
        except Exception:
            logger.exception("notify_order_approved failed for req #%s", req_id)

    # PDF менеджеру и боссу (inline-режим; в background его шлёт задача).
    if pdf_to_send and bot is not None and invoice_id:
        await _send_shipment_pdf(
            bot, pdf_to_send, invoice_id=int(invoice_id), req_id=req_id,
            manager_id=req.get("user_id"), boss_id=boss_user_id,
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
        "pdf_task": pdf_task,
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
        return {"ok": False, "error": "Заявка не найдена — обновите список", "req_id": req_id, "order_id": None}
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

    Заказ и заявка переводятся одной транзакцией в `reject_order_to_draft`
    (FOR UPDATE заказа, CAS обоих статусов). Если другой босс уже обработал
    заказ или заявку — не меняется ничего.

    Возвращает {ok, error, req_id, order_id, now, frozen, rejection_count}.
    """
    from services import async_db as adb
    from services import database as db
    from utils.helpers import local_now

    req = await adb.get_shipment_request(req_id)
    if not req:
        return {"ok": False, "error": "Заявка не найдена — обновите список", "req_id": req_id, "order_id": None}
    if req["status"] != "pending":
        return {
            "ok": False,
            "error": "Заявка уже обработана",
            "req_id": req_id,
            "order_id": req.get("order_id"),
        }

    order_id = req["order_id"]
    # Заказ → draft и заявка → returned — одной транзакцией (req_id передаём
    # внутрь): иначе сбой между ними оставлял pending-заявку при черновике.
    res = await db.reject_order_to_draft(
        order_id, boss_user_id, boss_name, comment, req_id=req_id
    )
    if not res.get("ok"):
        return {
            "ok": False,
            "error": res.get("error") or "Не удалось вернуть заказ в черновик",
            "req_id": req_id,
            "order_id": order_id,
        }

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


# ─── Отгрузка без одобрения ─────────────────────────────────────────────────
#
# Решение владельца (сентябрь 2026): «Одобрение отгрузки не нужно. Сам менеджер
# отмечает, что отгрузил товар, боссу приходит уведомление». На решение
# руководителя заказ уходит, только если без него нельзя: скидка к прайсу выше
# порога (`services/order_discounts.py`) и долг клиента сверх кредитного лимита
# (энфорс лимита — тот же `needs_override`, что и раньше при одобрении).
#
# Путь выбран такой: «Отгрузить» проводит ТЕ ЖЕ шаги, что раньше делали два
# человека, — `submit_order` (CAS draft→pending, заявка), `approve_shipment_request`
# (CAS pending→approved, расходная накладная под замком отгрузки, идемпотентно
# по `order_shipment.order_id`), `order_payments.record_payment_parts` (под
# `lock_orders`) и `database.mark_order_shipped` (CAS approved→shipped, проверка
# оплаты). Нового ребра графа статусов нет, схема и CHECK'и прода не меняются, а
# всё, что опирается на `approved` (резерв, «нужна доделка», отмена с возвратом
# остатка, долги, сдачи, себестоимость по партиям), работает как было. Каждый
# шаг — своя транзакция, поэтому операция ВОЗОБНОВЛЯЕМА: прервалась на середине
# — заказ остался в `pending`/`approved`, и повторное «Отгрузить» продолжит с
# того шага, на котором встало. Всё, что может отказать по вине формы (клиент,
# позиции, остаток, сумма оплаты, решение руководителя), проверяется ДО первого
# перехода: отказ оставляет черновик черновиком.

DECISION_REQUIRED = "decision_required"


def _fmt_base(amount: float) -> str:
    from config import BASE_CURRENCY

    return f"{money.format_cents(money.to_cents(amount or 0), decimals=0, sep=' ')} {BASE_CURRENCY}"


def decision_reasons_from(discount: dict | None, credit: dict | None) -> list[dict]:
    """Причины, по которым заказ не отгрузить без руководителя. [] — не нужно."""
    reasons: list[dict] = []
    if discount and discount.get("flagged"):
        worst = discount.get("max_pct")
        thr = discount.get("threshold_pct")
        reasons.append({
            "code": "discount",
            "text": (
                f"скидка {float(worst):g}% при пороге {float(thr):g}%"
                if worst is not None and thr is not None else "скидка выше порога"
            ),
        })
    if credit and credit.get("over_limit"):
        reasons.append({
            "code": "credit_limit",
            "text": (
                f"долг клиента станет {_fmt_base(credit['effective_debt'])} "
                f"при лимите {_fmt_base(credit['limit'])}"
            ),
        })
    return reasons


def decision_text(reasons: list[dict]) -> str:
    """«скидка 20% при пороге 15%; долг клиента станет …» — одной строкой."""
    return "; ".join(r["text"] for r in reasons)


def _order_total(items: list[dict]) -> float:
    return sum(float(it.get("quantity", 0) or 0) * float(it.get("price", 0) or 0) for it in items)


async def decision_reasons(order: dict, items: list[dict]) -> tuple[list[dict], dict]:
    """Нужно ли решение руководителя по заказу. → (причины, сводка скидки).

    `order` — с теми условиями оплаты, с которыми его отгружают (у черновика
    они приходят из формы). Лимит долга не проверяется у заказа, уже
    одобренного сверх лимита (`credit_limit_override`) — как в
    `approve_shipment_request`.
    """
    from services import order_discounts

    discount = await order_discounts.order_discount(items, order.get("currency"))
    credit = None
    if not order.get("credit_limit_override"):
        credit = await order_credit_context(order, _order_total(items))
    return decision_reasons_from(discount, credit), discount


async def requests_needing_decision() -> list[dict]:
    """Заявки `pending`, которые действительно ждут руководителя (скидка выше
    порога или долг сверх лимита), — с полем `reasons`.

    Остальные заявки — это заказы, отправленные «на одобрение» до того, как
    оно стало необязательным: их отгружает сам менеджер, и в «Решениях»,
    очереди дел и ежедневном пинге руководителю они не числятся. Батчем: заказы,
    позиции, прайсы и долги — по запросу на весь список.
    """
    from services import async_db as adb
    from services import order_discounts

    requests = await adb.get_pending_requests()
    if not requests:
        return []
    order_ids = sorted({int(r["order_id"]) for r in requests})
    orders = await adb.get_orders_by_ids(order_ids)
    items_by = await adb.get_order_items_by_ids(order_ids)
    prices = await order_discounts.load_reference_prices(*items_by.values())
    threshold = await order_discounts.current_threshold_pct()
    credit = await orders_credit_context([
        (orders[oid], _order_total(items_by.get(oid, [])))
        for oid in order_ids
        if oid in orders and not orders[oid].get("credit_limit_override")
    ])
    out: list[dict] = []
    for r in requests:
        oid = int(r["order_id"])
        order = orders.get(oid)
        if order is None:
            continue
        discount = order_discounts.summarize(
            items_by.get(oid, []), prices, order.get("currency"), threshold=threshold
        )
        reasons = decision_reasons_from(discount, credit.get(oid))
        if reasons:
            out.append({**r, "reasons": reasons})
    return out


def _actor_payment(actor_id: int, actor_name: str, role: str, username: str):
    from services import order_payments

    return order_payments.Actor(
        user_id=int(actor_id), name=actor_name, role=role or "", username=username or "",
    )


async def ship_order_now(
    order_id: int,
    actor_id: int,
    actor_name: str,
    bot: Any,
    *,
    actor_role: str | None = None,
    actor_username: str = "",
    payment_type: str | None = None,
    due_date: str | None = None,
    parts: Any = None,
) -> dict:
    """«Отгрузить» — из черновика, из заявки без решения или одобренного заказа.

    Черновик и `pending` отгружает автор заказа (`pending` — ещё руководитель),
    одобренный — как раньше: кладовщик (менеджер его замещает), руководитель,
    автор. `payment_type`/`due_date` — условия оплаты из формы черновика (без
    них берутся сохранённые у заказа). `parts` — разбивка «как клиент заплатил»
    для «оплаты сразу»: без неё такой заказ не отгружается, и ответ
    `payment_required` ничего не меняет в черновике.

    Возвращает `{ok: True, order_id, status: "shipped", payment, notify_task}`
    либо `{ok: False, code, error, http_status, ...}`. Коды отказа:
    `not_found`, `forbidden`, `bad_terms`, `frozen`, `no_client`, `no_items`,
    `status`, `busy`, `decision_required` (+ `reasons`, `discount`),
    `payment_required` (+ `gap_cents`), отказы склада (`insufficient_stock`,
    `unlinked_positions`, `stock_not_written_off`) и формы оплаты
    (`order_payments.PaymentError.code`).
    """
    from services import async_db as adb
    from services import order_payments
    from services.roles import role_allowed

    def fail(code: str, error: str, status: int = 409, **extra: Any) -> dict:
        return {"ok": False, "code": code, "error": error, "http_status": status,
                "order_id": order_id, **extra}

    order = await adb.get_order(order_id)
    if not order:
        return fail("not_found", "Заказ не найден — обновите список", 404)
    role = actor_role if actor_role is not None else await adb.get_role(actor_id)
    boss = role_allowed(role, ("admin", "boss"))
    author = int(order.get("user_id") or 0) == int(actor_id)
    status = order.get("status") or ""

    if status in ("draft", "pending"):
        if status == "draft" and not author:
            return fail("forbidden", "Отгрузить черновик может только менеджер, который его собрал", 403)
        if status == "pending" and not (author or boss):
            return fail("forbidden", "Отгрузить этот заказ может его менеджер или руководитель", 403)
        moved = await _approve_for_shipment(
            order, actor_id, actor_name, bot, boss=boss, role=role, username=actor_username,
            payment_type=payment_type, due_date=due_date, parts=parts, fail=fail,
        )
        if moved is not None:
            return moved
    elif status == "approved":
        if not (author or boss or role_allowed(role, ("warehouse_keeper",))):
            return fail("forbidden", "Отметить отгрузку может кладовщик, руководитель или менеджер заказа", 403)
    else:
        human = _STATUS_RU.get(status, status)
        return fail("status", f"Заказ #{order_id} уже «{human}» — отгружать нечего. Обновите список")

    order = await adb.get_order(order_id) or order
    payment: dict | None = None
    if (order.get("payment_type") or "paid") == "paid":
        gap = (await order_payments.payment_gap_cents([order_id])).get(order_id, 0)
        if gap > 0 and parts:
            try:
                payment = await order_payments.record_payment_parts(
                    order_id, _actor_payment(actor_id, actor_name, role, actor_username), parts,
                )
            except order_payments.PaymentError as e:
                # Двойное нажатие: оплату уже записал первый запрос — отгружаем.
                gap = (await order_payments.payment_gap_cents([order_id])).get(order_id, 0)
                if gap > 0:
                    return fail(e.code or "payment", e.message, e.status)
    res = await adb.mark_order_shipped(order_id, actor_id, actor_name)
    if not res.get("ok"):
        extra = {"gap_cents": res["gap_cents"]} if res.get("gap_cents") is not None else {}
        return fail(res.get("code") or "status", res.get("error") or "Заказ уже обработан — обновите список",
                    payment_recorded=payment is not None, **extra)

    notify_task = None
    if bot is not None:
        from services.notify import notify_order_shipped
        from utils.background import spawn

        notify_task = spawn(
            notify_order_shipped(bot, order_id, actor_id, actor_name),
            name=f"order-shipped-notify-{order_id}",
        )
    return {"ok": True, "order_id": order_id, "status": "shipped", "payment": payment,
            "already_in_ms": bool(res.get("already_in_ms")), "notify_task": notify_task}


async def _approve_for_shipment(
    order: dict, actor_id: int, actor_name: str, bot: Any, *, boss: bool, role: str,
    username: str, payment_type: str | None, due_date: str | None, parts: Any, fail: Any,
) -> dict | None:
    """Черновик или заявка → `approved` с проведённой накладной. None — готово,
    иначе словарь отказа (заказ при отказе проверок не меняется)."""
    from services import async_db as adb
    from services import order_payments, order_shipment

    order_id = int(order["id"])
    status = order["status"]
    if status == "draft":
        if payment_type is None:
            payment_type, due_date = order.get("payment_type"), order.get("due_date")
        ptype, due, err = validate_payment_terms(payment_type, due_date)
        if err:
            return fail("bad_terms", err, 400)
        if order.get("frozen"):
            return fail("frozen", "Заказ заморожен после серии отклонений — обратитесь к администратору")
        if not (order.get("agent_name") or "").strip():
            return fail("no_client", "Выберите клиента", 400)
    else:
        ptype, due = (order.get("payment_type") or "paid"), order.get("due_date")

    items = await adb.get_order_items(order_id)
    if not items:
        return fail("no_items", "Добавьте товары", 400)

    reasons, discount = await decision_reasons({**order, "payment_type": ptype, "due_date": due}, items)
    if reasons:
        text = decision_text(reasons)
        if status == "draft":
            msg = (f"Нужно решение руководителя: {text}. Без одобрения этот заказ не отгрузить — "
                   "отправьте заявку на отгрузку")
        elif boss:
            msg = f"Заказ #{order_id} ждёт вашего решения: {text}. Решите его в «Решениях»"
        else:
            msg = f"Заказ #{order_id} ждёт решения руководителя: {text}. Отгрузить можно после одобрения"
        return fail(DECISION_REQUIRED, msg, reasons=reasons, discount=discount, order_status=status)

    pre = await order_shipment.preflight(order_id, items)
    if not pre.get("ok"):
        return fail(pre.get("code") or "stock", pre.get("reason") or "Склад не спишет этот заказ")

    if ptype == "paid":
        due_cents = (await order_payments.payment_gap_cents([order_id])).get(order_id, 0)
        if due_cents > 0:
            if not parts:
                cur = (order.get("currency") or "").upper()
                return fail(
                    "payment_required",
                    f"Заказ #{order_id} «оплата сразу»: сначала введите, как клиент заплатил "
                    f"(наличные, карта, перечисление). Не внесено: {order_payments.fmt_cents(due_cents, cur)}",
                    gap_cents=due_cents,
                )
            try:
                await order_payments.check_payment_parts(
                    order_id, _actor_payment(actor_id, actor_name, role, username), parts, due_cents,
                )
            except order_payments.PaymentError as e:
                return fail(e.code or "payment", e.message, e.status)

    busy = f"Заказ #{order_id} уже оформляется — обновите список через пару секунд"
    if status == "draft":
        sub = await submit_order(order_id, actor_id, actor_name, payment_type=ptype, due_date=due)
        if not sub.get("ok"):
            if sub.get("status"):
                return fail("busy", busy)
            return fail("submit", sub.get("error") or "Заказ не отправлен — обновите экран и повторите", 400)
        req_id = int(sub["req_id"])
    else:
        pending = [r for r in await adb.get_shipment_requests_for_order(order_id) if r.get("status") == "pending"]
        if not pending:
            return fail("busy", busy)
        req_id = int(pending[-1]["id"])

    appr = await approve_shipment_request(
        req_id, actor_id, actor_name, bot,
        without_approval=not boss, notify_manager=False,
    )
    if not appr.get("ok"):
        if appr.get("needs_override") or appr.get("needs_discount_ack"):
            # Условия поменялись между проверкой и одобрением (прайс, лимит).
            return fail(DECISION_REQUIRED,
                        f"Заказ #{order_id} ждёт решения руководителя — условия изменились. "
                        "Отгрузить можно после одобрения")
        return fail("busy", busy)
    return None
