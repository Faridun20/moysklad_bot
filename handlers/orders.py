"""
Хэндлеры: решения по заявкам на отгрузку + заморозка.

T3.3: создание и редактирование заказа (каталог МойСклад, выбор клиента и
валюты, отправка заявки, удаление черновика) и списки (/myorders, /orders)
вырезаны — это WebApp (`openOrderEditor`, экран «Заявки»). В боте осталось то,
ради чего он и нужен: push-карточка заявки с кнопками решения (одобрить /
отклонить / на доработку / одобрить с превышением лимита), карточка заказа для
чтения (её открывает /find) и разморозка заказов — экрана заморозки в WebApp
нет.

Форматтеры (`format_order`, `format_request_notify`, `build_credit_context`) и
`request_approve_keyboard` остаются здесь: их зовёт webapp/server.py, когда
заявка создана из WebApp, — карточка боссу в любом случае уходит в Telegram.
"""

import logging

from config import BASE_CURRENCY as _BASE_CURRENCY


# Единая реализация в utils.helpers.esc — оставлен _esc-алиас чтобы
# не править каждый callsite в этом большом файле.
from handlers._ui import drop_keyboard, finish_card, finish_message, webapp_keyboard
from utils.helpers import esc as _esc  # noqa: E402


def _cur(amount: float, currency: str | None = None) -> str:
    """Форматирует сумму с валютой: «150 USD». Если валюта не передана —
    дефолт из BASE_CURRENCY (для обратной совместимости старых вызовов)."""
    return f"{_fmt_num(amount)} {currency or _BASE_CURRENCY}"


from aiogram import Bot, Router, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from services.roles import is_boss, is_admin
from services import async_db as adb
from utils.formatters import DIV, DIV2

logger = logging.getLogger(__name__)
router = Router()


class ReturnToDraft(StatesGroup):
    waiting_for_reason = State()  # босс вводит причину «на доработку»


# ─── Форматирование ───────────────────────────────────────────────────────────


STATUS_EMOJI = {
    "draft": "📝",
    "pending": "⏳",
    "approved": "✅",
    "rejected": "❌",
    "shipped": "🚚",
}

STATUS_NAME = {
    "draft": "Черновик",
    "pending": "На рассмотрении",
    "approved": "Одобрено",
    "rejected": "Отклонено",
    "shipped": "Отгружено",
}


def _line_total(item: dict) -> float:
    return float(item.get("quantity", 0)) * float(item.get("price", 0) or 0)


def _fmt_num(n: float) -> str:
    """Без бесконечных нулей: 150.0 → 150, 49.99 → 49.99."""
    from services import money

    return money.format_cents(money.to_cents(n or 0), decimals=2, grouping=False, trim=True)


def _order_payment_block(order: dict, summary: dict | None) -> str:
    """Блок оплаты для карточки заказа (UX: вся инфа в одном месте, без /debts).
    Показывает тип оплаты + срок (для credit) + оплачено/остаток (если есть
    summary). Пусто для draft/rejected/cancelled."""
    status = order.get("status")
    if status in ("draft", "rejected", "cancelled"):
        return ""
    currency = order.get("currency") or _BASE_CURRENCY
    ptype = order.get("payment_type") or "paid"
    parts = [DIV2]
    if ptype == "credit":
        due = order.get("due_date") or "—"
        parts.append(f"💳 <b>В долг</b> до <b>{_esc(due)}</b>")
    else:
        parts.append("💵 Оплата сразу")
    if summary:
        confirmed = summary.get("confirmed", 0) or 0
        pending = summary.get("pending", 0) or 0
        remaining = summary.get("remaining", 0) or 0
        if confirmed > 0 or pending > 0:
            extra = f" · ⏳ В подтверждении: {_cur(pending, currency)}" if pending > 0 else ""
            parts.append(
                f"💵 Оплачено: <b>{_cur(confirmed, currency)}</b>{extra}"
                f" · 📎 Остаток: <b>{_cur(remaining, currency)}</b>"
            )
        elif remaining > 0 and ptype == "credit":
            parts.append(f"📎 Остаток: <b>{_cur(remaining, currency)}</b>")
    return "\n".join(parts)


def format_order(order: dict, items: list[dict], summary: dict | None = None) -> str:
    status_emoji = STATUS_EMOJI.get(order["status"], "📋")
    status_name = STATUS_NAME.get(order["status"], order["status"])
    agent_str = (
        f"\n👤 Клиент: <b>{_esc(order['agent_name'])}</b>" if order.get("agent_name") else ""
    )
    comment_str = f"\n📝 {_esc(order['comment'])}" if order.get("comment") else ""
    currency = order.get("currency") or _BASE_CURRENCY

    # Заказ вернули на доработку → показываем причину и счётчик попыток.
    returned_str = ""
    if order.get("status") == "draft" and order.get("rejection_comment"):
        rc = int(order.get("rejection_count") or 0)
        if order.get("frozen"):
            returned_str = (
                f"\n🧊 <b>Заморожен</b> (отклонений: {rc}) — "
                f"переотправка заблокирована, нужна разморозка админом"
                f"\n↩️ Причина: <i>{_esc(order['rejection_comment'])}</i>"
            )
        else:
            returned_str = (
                f"\n↩️ <b>Возвращено на доработку</b> (попытка {rc}): "
                f"<i>{_esc(order['rejection_comment'])}</i>"
            )

    lines = [
        DIV,
        f"{status_emoji} <b>Заказ #{order['id']}</b>   <code>{_esc(status_name)}</code>",
        f"<code>{_esc(order['created_at'][:16])}</code> · <code>{_esc(currency)}</code>",
        f"👤 Менеджер: {_esc(order['full_name'])}{agent_str}{comment_str}{returned_str}",
        "",
    ]

    if items:
        lines.append(f"<b>📦 Товары ({len(items)}):</b>")
        lines.append(DIV2)
        total_items = len(items)
        for i, item in enumerate(items[:10]):
            note_str = f"  <i>{_esc(item['note'])}</i>" if item.get("note") else ""
            price = float(item.get("price", 0) or 0)
            qty = float(item.get("quantity", 0))
            unit = _esc(item.get("unit") or "шт")
            if price > 0:
                price_str = (
                    f"     <code>{_fmt_num(qty)} {unit} × "
                    f"{_cur(price, currency)} = {_cur(qty * price, currency)}</code>"
                )
            else:
                price_str = f"     <code>{_fmt_num(qty)} {unit}</code>"
            lines.append(f"  {i + 1}. <b>{_esc(item['product_name'])}</b>\n{price_str}{note_str}")
        if total_items > 10:
            lines.append(f"  <i>...и ещё {total_items - 10} позиций</i>")

        grand_total = sum(_line_total(it) for it in items)
        if grand_total > 0:
            lines.append(DIV2)
            lines.append(f"<b>💰 Итого: {_cur(grand_total, currency)}</b>")
    else:
        lines.append("<i>Товары не добавлены</i>")

    pay_block = _order_payment_block(order, summary)
    if pay_block:
        lines.append(pay_block)

    return "\n".join(lines)


def format_request_notify(order: dict, items: list[dict], req_id: int) -> str:
    """Сообщение боссу о новой заявке. Видны цены — нужны для апрува."""
    currency = order.get("currency") or _BASE_CURRENCY
    lines = []
    for it in items[:10]:
        qty = float(it.get("quantity", 0))
        price = float(it.get("price", 0) or 0)
        sub = qty * price
        name = _esc(it["product_name"])
        unit = _esc(it.get("unit") or "шт")
        if price > 0:
            lines.append(
                f"  • {name}: {_fmt_num(qty)} {unit} "
                f"× {_cur(price, currency)} = <b>{_cur(sub, currency)}</b>"
            )
        else:
            lines.append(f"  • {name}: {_fmt_num(qty)} {unit} <i>(цена не указана)</i>")
    items_text = "\n".join(lines)
    grand_total = sum(_line_total(it) for it in items)
    if len(items) > 10:
        items_text += f"\n  ...и ещё {len(items) - 10} поз."

    agent_str = (
        f"\n👤 Клиент: <b>{_esc(order['agent_name'])}</b>" if order.get("agent_name") else ""
    )
    comment_str = f"\n📝 {_esc(order['comment'])}" if order.get("comment") else ""
    total_str = f"\n\n<b>💰 Итого: {_cur(grand_total, currency)}</b>" if grand_total > 0 else ""

    # Тип оплаты: для credit'а явно показываем дату возврата,
    # чтобы босс ещё на этапе апрува видел условия и решал,
    # давать ли клиенту в долг.
    payment_type = order.get("payment_type") or "paid"
    if payment_type == "credit":
        due = order.get("due_date") or "—"
        payment_str = f"\n💳 <b>В долг</b>, погасить до <b>{_esc(due)}</b>"
    else:
        payment_str = "\n💵 Оплата сразу"

    return (
        f"{DIV}\n"
        f"🔔 <b>Новая заявка на отгрузку #{req_id}</b>\n"
        f"\n"
        f"👨‍💼 Менеджер: <b>{_esc(order['full_name'])}</b>{agent_str}{comment_str}"
        f"{payment_str}\n"
        f"\n"
        f"<b>📦 Товары:</b>\n{items_text}"
        f"{total_str}"
    )


async def build_credit_context(order: dict, items: list[dict]) -> str:
    """Кредит-контекст клиента прямо в заявке боссу (долг с учётом заявки / лимит)
    — чтобы решать одобрение НЕ выходя в /limit. '' для paid-заказов / без agent_id.
    Использует order_credit_context (без double-count, см. там)."""
    from services.order_workflow import order_credit_context

    total = sum(_line_total(it) for it in items)
    try:
        ctx = await order_credit_context(order, total)
    except Exception:
        return ""
    if not ctx:
        return ""
    flag = (
        "🔴 <b>ПРЕВЫШЕНИЕ ЛИМИТА</b>" if ctx["over_limit"] else "🟢 в пределах лимита"
    )
    return (
        f"\n\n📊 <b>Кредит клиента</b> ({_BASE_CURRENCY}):"
        f"\n   долг с учётом заявки: <b>{_fmt_num(ctx['effective_debt'])}</b>"
        f" / лимит {_fmt_num(ctx['limit'])}"
        f"\n   {flag}"
    )


def request_approve_keyboard(req_id: int):
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Одобрить", callback_data=f"req_ok:{req_id}")
    kb.button(text="❌ Отклонить", callback_data=f"req_no:{req_id}")
    kb.button(text="✏️ На доработку", callback_data=f"req_draft:{req_id}")
    kb.adjust(2, 1)
    return kb.as_markup()


@router.callback_query(F.data.startswith("ord_view:"))
async def cb_view_order(call: CallbackQuery):
    await call.answer()
    try:
        order_id = int(call.data.split(":")[1])
    except (IndexError, ValueError):
        return await call.message.answer("❌ Некорректный запрос.")

    order = await adb.get_order(order_id)
    if not order:
        return await call.message.answer("❌ Заказ не найден.")

    # Защита от информации utечки: видеть заказ может только владелец
    # или босс/админ. Раньше любой Telegram-юзер с угадаенным ID видел
    # содержимое заявок чужой компании.
    is_owner = order["user_id"] == call.from_user.id
    if not is_owner and not is_boss(call.from_user.id):
        return await call.message.answer("⛔ Нет доступа к этому заказу.")

    items = await adb.get_order_items(order_id)
    # Для не-черновиков подтягиваем сводку оплаты, чтобы карточка показала
    # оплачено/остаток/срок — без перехода в другой экран.
    summary = (
        await adb.get_order_payment_summary(order_id)
        if order["status"] not in ("draft", "rejected", "cancelled")
        else None
    )
    # T3.3: карточка только для чтения — правка состава/клиента/валюты и
    # отправка заявки живут в WebApp. Кнопки «➕ Добавить товар» и т.п. отсюда
    # убраны вместе с их хендлерами.
    await call.message.answer(
        format_order(order, items, summary=summary),
        parse_mode="HTML",
        reply_markup=webapp_keyboard(),
    )


# ─── Callback: одобрение/отклонение заявки ────────────────────────────────────


async def _approve_flow(call: CallbackQuery, bot: Bot, req_id: int, override: bool) -> None:
    """Общий путь одобрения заявки (обычное / с превышением лимита)."""
    boss_name = call.from_user.full_name or str(call.from_user.id)
    # Вся логика (DB, МойСклад, уведомления, PDF, авто-payment) — в сервисе.
    from services.order_workflow import approve_shipment_request

    result = await approve_shipment_request(
        req_id, call.from_user.id, boss_name, bot, override=override
    )

    if not result["ok"]:
        # Превышение кредитного лимита — предлагаем явное подтверждение.
        if result.get("needs_override"):
            over = result["over"]
            kb = InlineKeyboardBuilder()
            kb.button(text="✅ Одобрить с превышением", callback_data=f"req_ovr:{req_id}")
            kb.adjust(1)
            await call.answer("⚠️ Превышение лимита", show_alert=True)
            # T3.2: снимаем кнопки заявки. Решение теперь принимается ТОЛЬКО
            # через «Одобрить с превышением» в следующем сообщении — иначе
            # босс мог обойти явное подтверждение, повторно нажав «Одобрить»
            # на старой карточке.
            await finish_card(call, "⚠️ Превышение лимита — нужно подтверждение")
            return await call.message.answer(
                f"⚠️ <b>Превышение кредитного лимита</b>\n"
                f"Текущий долг: <b>{_fmt_num(over['current_debt'])}</b>\n"
                f"Лимит: <b>{_fmt_num(over['limit'])}</b>\n"
                f"После заказа: <b>{_fmt_num(over['projected'])}</b>\n\n"
                f"Одобрить всё равно?",
                parse_mode="HTML",
                reply_markup=kb.as_markup(),
            )
        return await call.answer(f"⚠️ {result['error']}", show_alert=True)

    await call.answer("✅ Заявка одобрена")
    suffix = " (с превышением лимита)" if override else ""
    base = getattr(call.message, "html_text", None) or call.message.text or ""
    await call.message.edit_text(
        base
        + f"\n\n{DIV}\n✅ <b>Одобрено{suffix}</b>  <code>{result['now']}</code>  — {_esc(boss_name)}",
        parse_mode="HTML",
        reply_markup=webapp_keyboard("🌐 Ещё заявки — в WebApp"),
    )


@router.callback_query(F.data.startswith("req_ok:"))
async def cb_approve_request(call: CallbackQuery, bot: Bot):
    if not is_boss(call.from_user.id):
        return await call.answer("Нет доступа", show_alert=True)
    req_id = int(call.data.split(":")[1])
    await _approve_flow(call, bot, req_id, override=False)


@router.callback_query(F.data.startswith("req_ovr:"))
async def cb_approve_request_override(call: CallbackQuery, bot: Bot):
    if not is_boss(call.from_user.id):
        return await call.answer("Нет доступа", show_alert=True)
    req_id = int(call.data.split(":")[1])
    await _approve_flow(call, bot, req_id, override=True)


@router.callback_query(F.data.startswith("req_no:"))
async def cb_reject_request(call: CallbackQuery, bot: Bot):
    if not is_boss(call.from_user.id):
        return await call.answer("Нет доступа", show_alert=True)

    req_id = int(call.data.split(":")[1])
    boss_name = call.from_user.full_name or str(call.from_user.id)

    from services.order_workflow import reject_shipment_request

    result = await reject_shipment_request(req_id, call.from_user.id, boss_name, bot)

    if not result["ok"]:
        return await call.answer(f"⚠️ {result['error']}", show_alert=True)

    await call.answer("❌ Заявка отклонена")
    base = getattr(call.message, "html_text", None) or call.message.text or ""
    await call.message.edit_text(
        base
        + f"\n\n{DIV}\n❌ <b>Отклонено</b>  <code>{result['now']}</code>  — {_esc(boss_name)}",
        parse_mode="HTML",
        reply_markup=webapp_keyboard("🌐 Ещё заявки — в WebApp"),
    )


@router.callback_query(F.data.startswith("req_draft:"))
async def cb_return_to_draft(call: CallbackQuery, state: FSMContext):
    """Босс возвращает заявку на доработку — спрашиваем причину одним сообщением."""
    if not is_boss(call.from_user.id):
        return await call.answer("Нет доступа", show_alert=True)
    req_id = int(call.data.split(":")[1])
    await state.set_state(ReturnToDraft.waiting_for_reason)
    await state.update_data(
        req_id=req_id, msg_chat=call.message.chat.id, msg_id=call.message.message_id
    )
    await call.answer()
    # T3.2: снимаем кнопки заявки на время ввода причины — иначе ту же заявку
    # можно одобрить, пока босс печатает, что доработать.
    await drop_keyboard(call)
    await call.message.answer(
        "✍️ Укажите, что нужно доработать (одним сообщением) — менеджер увидит причину:"
    )


@router.message(ReturnToDraft.waiting_for_reason)
async def process_return_to_draft_reason(message: Message, state: FSMContext, bot: Bot):
    # Повторный role-check после FSM-перехода (роль могли снять между callback'ом
    # и сообщением) — как в handlers/deposits.py.
    if not is_boss(message.from_user.id):
        await state.clear()
        return await message.answer("⛔ Нет доступа — действие отменено.")
    reason = (message.text or "").strip()[:500]
    if len(reason) < 3:
        return await message.answer("❌ Причина слишком короткая. Повторите.")
    data = await state.get_data()
    await state.clear()
    req_id = data.get("req_id")
    boss_name = message.from_user.full_name or str(message.from_user.id)

    from services.order_workflow import return_order_to_draft

    result = await return_order_to_draft(req_id, message.from_user.id, boss_name, reason, bot)
    if not result["ok"]:
        return await message.answer(f"⚠️ {result['error']}")

    frozen_tail = "  🧊 <b>ЗАМОРОЖЕН</b>" if result.get("frozen") else ""
    note = (
        f"↩️ Заявка #{req_id} возвращена на доработку"
        f" (попытка {result.get('rejection_count')}).{frozen_tail}"
    )
    # T3.2: помечаем саму карточку заявки (её кнопки сняты на входе в FSM) —
    # иначе в истории она остаётся «на согласовании» без следа решения.
    if not await finish_message(bot, data.get("msg_chat"), data.get("msg_id"), note):
        await message.answer(note, parse_mode="HTML")


# ─── Заморозка: просмотр и разморозка (admin) ─────────────────────────────────


@router.message(Command("frozen"))
async def cmd_frozen(message: Message):
    """Список замороженных заказов с кнопкой разморозки (только admin)."""
    if not is_admin(message.from_user.id):
        return await message.answer("⛔ Команда только для администратора.")
    orders = await adb.get_frozen_orders()
    if not orders:
        return await message.answer("🧊 Замороженных заказов нет.")
    kb = InlineKeyboardBuilder()
    lines = [f"{DIV}", "🧊 <b>Замороженные заказы:</b>", ""]
    for o in orders[:20]:
        agent = _esc(o.get("agent_name") or "—")
        lines.append(
            f"• <b>#{o['id']}</b> · {agent} · отклонений: {int(o.get('rejection_count') or 0)}"
        )
        kb.button(text=f"🔓 Разморозить #{o['id']}", callback_data=f"unfreeze:{o['id']}")
    kb.adjust(1)
    await message.answer("\n".join(lines), parse_mode="HTML", reply_markup=kb.as_markup())


@router.callback_query(F.data.startswith("unfreeze:"))
async def cb_unfreeze_order(call: CallbackQuery, bot: Bot):
    if not is_admin(call.from_user.id):
        return await call.answer("⛔ Нет доступа", show_alert=True)
    order_id = int(call.data.split(":")[1])
    name = call.from_user.full_name or str(call.from_user.id)
    result = await adb.unfreeze_order(order_id, call.from_user.id, name)
    if not result.get("ok"):
        return await call.answer(f"⚠️ {result.get('error')}", show_alert=True)
    await call.answer("🔓 Разморожен")
    base = getattr(call.message, "html_text", None) or call.message.text or ""
    await call.message.edit_text(
        base + f"\n\n{DIV}\n🔓 <b>Заказ #{order_id} разморожен</b> — {_esc(name)}",
        parse_mode="HTML",
    )
    # Best-effort: сообщаем менеджеру, что можно переотправить.
    order = await adb.get_order(order_id)
    if order and order.get("user_id"):
        try:
            await bot.send_message(
                order["user_id"],
                f"🔓 Заказ #{order_id} разморожен администратором — "
                f"можно отредактировать и отправить заново.",
            )
        except Exception:
            pass
