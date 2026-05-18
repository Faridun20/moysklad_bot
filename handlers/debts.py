"""
Долги: команда /debts и кнопка «Отметить оплачено».

Парная задача к WebApp-экрану «Долги» — здесь то же самое доступно
прямо в чате бота, чтобы можно было быстро посмотреть и закрыть
долг без открытия WebApp.

Команда /debts:
  - менеджер видит только СВОИ открытые долги
  - boss/admin — все долги по компании
  - для каждого долга inline-кнопка «✅ Оплачено» с callback debt_paid:<id>

Callback debt_paid:<id>:
  - права: автор заказа ИЛИ boss/admin
  - идемпотентно (повторный тап ничего не ломает)
  - после успеха редактирует сообщение, чтобы кнопка пропала
"""

from datetime import date
import logging

from aiogram import Bot, Router, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder

from services.roles import can_create_orders, is_boss
from services.database import (
    get_open_debts, get_order, mark_order_paid,
    get_order_items, get_order_items_by_ids, get_role,
    get_order_payment_summary,
)
from services.notifier import get_notify_recipients
from config import BASE_CURRENCY
from utils.helpers import user_safe_error
from utils.formatters import DIV

logger = logging.getLogger(__name__)
router = Router()


# ─── Форматирование ──────────────────────────────────────────────────────────


def _esc(s: str) -> str:
    """HTML-escape — Telegram parse_mode=HTML."""
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _fmt_amount(n: float) -> str:
    """1234567 → '1 234 567'."""
    return f"{int(round(n)):,}".replace(",", " ")


def _format_due(due_str: str | None, today_str: str) -> str:
    """Дата возврата + emoji-индикатор статуса."""
    if not due_str:
        return "—"
    if due_str < today_str:
        return f"⚠️ <b>{_esc(_to_ru(due_str))}</b> (просрочен)"
    if due_str == today_str:
        return f"⏰ <b>сегодня</b>"
    return f"📅 {_esc(_to_ru(due_str))}"


def _to_ru(iso: str) -> str:
    """YYYY-MM-DD → ДД.ММ.ГГГГ."""
    if not iso or len(iso) < 10:
        return iso or ""
    y, m, d = iso[:10].split("-")
    return f"{d}.{m}.{y}"


def _format_debt_card(
    order: dict,
    items: list[dict],
    today_str: str,
    show_owner: bool,
) -> str:
    """HTML-карточка одного долга для сообщения в боте.
    Показывает уже оплаченное и остаток (для частичных оплат)."""
    summary = get_order_payment_summary(order["id"])
    currency = order.get("currency") or BASE_CURRENCY
    agent = _esc(order.get("agent_name") or "—")
    due_str = _format_due(order.get("due_date"), today_str)
    lines = [
        f"#{order['id']} · 🏢 <b>{agent}</b>",
        f"💰 Сумма: <b>{_fmt_amount(summary['total'])} {_esc(currency)}</b>",
    ]
    if summary["confirmed"] > 0 or summary["pending"] > 0:
        lines.append(
            f"💵 Оплачено: <b>{_fmt_amount(summary['confirmed'])}</b>"
            + (
                f" · ⏳ В подтверждении: <b>{_fmt_amount(summary['pending'])}</b>"
                if summary["pending"] > 0 else ""
            )
            + f" · 📎 Остаток: <b>{_fmt_amount(summary['remaining'])}</b>"
        )
    lines.append(f"📅 Срок: {due_str}")
    if show_owner:
        lines.append(f"👨‍💼 {_esc(order.get('full_name') or '—')}")
    return "\n".join(lines)


# ─── /debts ──────────────────────────────────────────────────────────────────


@router.message(Command("debts"))
async def cmd_debts(message: Message):
    """Показать открытые долги. Менеджер — свои, boss/admin — все."""
    user_id = message.from_user.id
    if not can_create_orders(user_id):
        return  # нет прав вообще — молчим, как принято в этом боте

    role = get_role(user_id)
    boss_view = is_boss(user_id)

    # Менеджер ограничен своими долгами. Босс/админ видит всю компанию.
    debts = get_open_debts(user_id=None if boss_view else user_id)

    if not debts:
        await message.answer(
            f"{DIV}\n💳 <b>Долги</b>\n\nОткрытых долгов нет.",
            parse_mode="HTML",
        )
        return

    # Подтягиваем позиции батчем — нужны для сумм
    items_by_order = get_order_items_by_ids([d["id"] for d in debts])
    today_str = date.today().isoformat()

    # Отдельным сообщением на каждый долг — чтобы у каждого была своя
    # кнопка «Оплачено». В одном сообщении мы не сможем понять, на чью
    # кнопку нажали без передачи order_id в callback (что и делаем).
    # Перед карточками — короткая сводка.
    overdue = sum(1 for d in debts if d.get("due_date") and d["due_date"] < today_str)
    due_today = sum(1 for d in debts if d.get("due_date") == today_str)
    upcoming = len(debts) - overdue - due_today
    scope_str = "все долги" if boss_view else "ваши долги"
    summary = (
        f"{DIV}\n"
        f"💳 <b>Долги</b> ({scope_str})\n\n"
        f"⚠️ Просрочено: <b>{overdue}</b>\n"
        f"⏰ Сегодня: <b>{due_today}</b>\n"
        f"📅 Будущие: <b>{upcoming}</b>"
    )
    await message.answer(summary, parse_mode="HTML")

    for d in debts:
        items = items_by_order.get(d["id"], [])
        text = _format_debt_card(d, items, today_str, show_owner=boss_view)

        kb = InlineKeyboardBuilder()
        kb.button(text="✅ Отметить оплачено", callback_data=f"debt_paid:{d['id']}")
        kb.adjust(1)

        try:
            await message.answer(
                text,
                parse_mode="HTML",
                reply_markup=kb.as_markup(),
            )
        except Exception:
            # Если одно сообщение не ушло — продолжаем со следующим,
            # лучше частично показать, чем вообще ничего.
            logger.exception("Не удалось показать долг #%s", d["id"])


# ─── Callback: отметить оплачено ─────────────────────────────────────────────


@router.callback_query(F.data.startswith("debt_paid:"))
async def cb_debt_paid(callback: CallbackQuery, bot: Bot):
    """Закрыть долг по нажатию кнопки. Менеджер может закрыть свой,
    boss/admin — любой."""
    user_id = callback.from_user.id
    try:
        order_id = int(callback.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await callback.answer("Некорректные данные", show_alert=True)
        return

    order = get_order(order_id)
    if not order:
        await callback.answer("Заказ не найден", show_alert=True)
        return

    boss_view = is_boss(user_id)
    is_owner = order["user_id"] == user_id
    if not (is_owner or boss_view):
        await callback.answer("Нет доступа к этому заказу", show_alert=True)
        return

    if order.get("payment_type") != "credit":
        await callback.answer("Этот заказ не в долг", show_alert=True)
        return

    if order.get("paid_confirmed_at"):
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        await callback.answer("Долг уже закрыт")
        return

    summary = get_order_payment_summary(order_id)
    if summary["remaining"] <= 0:
        await callback.answer("Нечего оплачивать — остаток ноль")
        return

    full_name = (
        f"{callback.from_user.first_name or ''} "
        f"{callback.from_user.last_name or ''}".strip()
        or callback.from_user.username or str(user_id)
    )
    username = callback.from_user.username or ""

    # Из бот-кнопки сумму не спросить (нет input UI). Закрываем целиком
    # остаток. Если нужно частично — менеджер открывает WebApp и вводит
    # точную сумму.
    ok, payment_id = mark_order_paid(
        order_id, user_id, full_name, amount=None, username=username,
    )
    if not ok:
        await callback.answer("Не удалось создать платёж")
        return

    currency = order.get("currency") or BASE_CURRENCY
    try:
        await callback.message.edit_text(
            (callback.message.html_text or callback.message.text or "")
            + f"\n\n⏳ <b>Платёж #{payment_id} ждёт подтверждения</b> "
              f"({_esc(full_name)}, {_fmt_amount(summary['remaining'])} {_esc(currency)})",
            parse_mode="HTML",
            reply_markup=None,
        )
    except Exception:
        pass
    await callback.answer("Отправлено на подтверждение боссу")

    # Push боссу — через стандартный payment-approval flow
    await _push_payment_confirmation(bot, order_id, full_name, payment_id)


async def _push_payment_confirmation(
    bot: Bot, order_id: int, manager_name: str, payment_id: int,
) -> None:
    """Уведомление boss/admin со стандартными кнопками pay_ok/pay_no
    (как при обычном /pay платеже — handlers/payments.py). После
    confirm_payment(payment_id) хелпер автоматически проверит, не
    закрылся ли заказ полностью."""
    from aiogram.utils.keyboard import InlineKeyboardBuilder

    order = get_order(order_id)
    if not order:
        return
    summary = get_order_payment_summary(order_id)
    from services.database import get_payment
    payment = get_payment(payment_id)
    if not payment:
        return
    currency = order.get("currency") or BASE_CURRENCY
    agent = _esc(order.get("agent_name") or "—")
    due = order.get("due_date") or "—"
    amount = float(payment.get("amount") or 0)
    confirmed_before = max(0.0, summary["confirmed"])
    # summary["remaining"] = total - confirmed (без учёта pending).
    # «Останется после подтверждения ЭТОГО платежа» — отнимаем amount.
    remaining_after = max(0.0, summary["remaining"] - amount)

    lines = [
        f"{DIV}",
        "💳 <b>Требуется подтверждение оплаты</b>",
        "",
        f"Заказ #{order_id}",
        f"👨‍💼 Менеджер: <b>{_esc(manager_name)}</b>",
        f"🏢 Клиент: <b>{agent}</b>",
        f"💵 Сумма платежа: <b>{_fmt_amount(amount)} {_esc(currency)}</b>",
        f"📦 По заказу всего: <b>{_fmt_amount(summary['total'])} {_esc(currency)}</b>",
    ]
    if confirmed_before > 0:
        lines.append(f"✅ Уже оплачено ранее: <b>{_fmt_amount(confirmed_before)} {_esc(currency)}</b>")
    if remaining_after <= 0:
        lines.append("🎉 Этот платёж <b>закрывает долг полностью</b>")
    else:
        lines.append(f"📎 Останется к получению: <b>{_fmt_amount(remaining_after)} {_esc(currency)}</b>")
    lines.append(f"📅 Срок: {_esc(_to_ru(due))}")
    lines.append("")
    lines.append("Подтвердите, что эта сумма реально пришла в кассу.")
    text = "\n".join(lines)
    # Используем существующие callback'и pay_ok/pay_no из handlers/payments.py
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Принять",   callback_data=f"pay_ok:{payment_id}")
    kb.button(text="❌ Отклонить", callback_data=f"pay_no:{payment_id}")
    kb.adjust(2)

    for uid in get_notify_recipients():
        try:
            await bot.send_message(
                uid, text, parse_mode="HTML", reply_markup=kb.as_markup(),
            )
        except Exception:
            logger.exception("Не удалось уведомить %s о подтверждении #%s", uid, order_id)
