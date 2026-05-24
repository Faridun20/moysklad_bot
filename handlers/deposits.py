"""
Хэндлеры: сдача наличных в кассу (cash deposits, IMPLEMENTATION.md §7).

Менеджер: /deposit <сумма> — создать сдачу (авто-распределение по своим
открытым заказам). /my_deposits — список своих сдач.
Босс/бухгалтер: кнопки «Подтвердить»/«Отклонить» под уведомлением.

Логика — в services.database (create/confirm/reject_cash_deposit); тут только
Telegram-UI и уведомления.
"""

import logging

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from services import async_db as adb
from services.roles import _has_role, can_confirm_deposit
from utils.helpers import esc
from utils.formatters import DIV

logger = logging.getLogger(__name__)
router = Router()


class DepositReject(StatesGroup):
    waiting_for_reason = State()


def _can_deposit(user_id: int) -> bool:
    return _has_role(user_id, "admin", "boss", "manager")


def _confirm_keyboard(deposit_id: int):
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Подтвердить", callback_data=f"dep_ok:{deposit_id}")
    kb.button(text="❌ Отклонить", callback_data=f"dep_no:{deposit_id}")
    kb.adjust(2)
    return kb.as_markup()


def _fmt_amount(x: float) -> str:
    return f"{x:,.2f}".replace(",", " ")


async def _notify_confirmers(bot: Bot, deposit_id: int, manager_name: str, amount: float):
    """Отправить боссам/бухгалтерам карточку сдачи с кнопками."""
    allocations = await adb.get_cash_deposit_orders(deposit_id)
    orders_line = (
        "\n".join(
            f"  • заказ #{a['order_id']} — {_fmt_amount(a['amount_allocated'])} USD"
            for a in allocations
        )
        or "  <i>нет открытых заказов для привязки</i>"
    )
    text = (
        f"{DIV}\n"
        f"💵 <b>Сдача наличных #{deposit_id}</b>\n\n"
        f"👨‍💼 Менеджер: <b>{esc(manager_name)}</b>\n"
        f"💰 Сумма: <b>{_fmt_amount(amount)} USD</b>\n"
        f"📦 Закрывает заказы:\n{orders_line}"
    )
    recipients = await adb.get_deposit_confirmers()
    for uid in recipients:
        try:
            await bot.send_message(
                uid, text, parse_mode="HTML", reply_markup=_confirm_keyboard(deposit_id)
            )
        except Exception as e:
            logger.warning("deposit notify %d failed: %s", uid, e)


@router.message(Command("deposit"))
async def cmd_deposit(message: Message):
    if not _can_deposit(message.from_user.id):
        return await message.answer("⛔ Сдавать наличные могут менеджеры.")
    parts = (message.text or "").strip().split()
    if len(parts) != 2:
        return await message.answer(
            "💵 Формат: <code>/deposit СУММА</code>\nНапример: <code>/deposit 500</code>",
            parse_mode="HTML",
        )
    try:
        amount = float(parts[1].replace(",", ".").replace(" ", ""))
        if amount <= 0:
            raise ValueError
    except ValueError:
        return await message.answer("❌ Сумма должна быть положительным числом.")

    res = await adb.create_cash_deposit(message.from_user.id, amount)
    if not res.get("ok"):
        return await message.answer(f"⚠️ {res.get('error', 'не удалось создать сдачу')}")

    manager_name = message.from_user.full_name or str(message.from_user.id)
    await _notify_confirmers(message.bot, res["deposit_id"], manager_name, amount)
    await message.answer(
        f"✅ Сдача #{res['deposit_id']} на <b>{_fmt_amount(amount)} USD</b> создана "
        f"и отправлена на подтверждение.",
        parse_mode="HTML",
    )


@router.message(Command("my_deposits"))
async def cmd_my_deposits(message: Message):
    if not _can_deposit(message.from_user.id):
        return await message.answer("⛔ Нет доступа.")
    deposits = await adb.get_manager_cash_deposits(message.from_user.id)
    if not deposits:
        return await message.answer("📭 Сдач пока нет.")
    emoji = {"pending": "⏳", "confirmed": "✅", "rejected": "❌"}
    lines = [f"{DIV}", "💵 <b>Мои сдачи:</b>\n"]
    for d in deposits:
        st = d.get("status", "pending")
        line = f"{emoji.get(st, '•')} #{d['id']} — {_fmt_amount(d['amount'])} USD  ·  {st}"
        if st == "rejected" and d.get("reject_reason"):
            line += f"\n   <i>{esc(d['reject_reason'])}</i>"
        lines.append(line)
    await message.answer("\n".join(lines), parse_mode="HTML")


@router.callback_query(F.data.startswith("dep_ok:"))
async def cb_deposit_confirm(call: CallbackQuery, bot: Bot):
    if not can_confirm_deposit(call.from_user.id):
        return await call.answer("⛔ Нет доступа", show_alert=True)
    deposit_id = int(call.data.split(":")[1])
    name = call.from_user.full_name or str(call.from_user.id)

    dep = await adb.get_cash_deposit(deposit_id)
    res = await adb.confirm_cash_deposit(deposit_id, call.from_user.id, name)
    if not res.get("ok"):
        return await call.answer(f"⚠️ {res.get('error', 'уже обработано')}", show_alert=True)

    await call.answer("✅ Подтверждено")
    # Round 6 (S1): html_text сохраняет HTML-entities из оригинала; .text
    # отдаёт Telegram-decoded строку, на которой повторный parse_mode="HTML"
    # ломается если в оригинале был `<` или `>` (например, esc(<Имя>) в
    # карточке от _notify_confirmers). getattr с fallback — для совместимости
    # с тестовыми моками без свойства html_text.
    original = getattr(call.message, "html_text", None) or call.message.text or ""
    await call.message.edit_text(
        original + f"\n\n{DIV}\n✅ <b>Подтверждено</b> — {esc(name)}",
        parse_mode="HTML",
    )
    if dep and dep.get("manager_id"):
        closed = res.get("closed_orders") or []
        extra = f" Закрыты заказы: {', '.join('#' + str(o) for o in closed)}." if closed else ""
        try:
            await bot.send_message(
                dep["manager_id"],
                f"✅ Ваша сдача #{deposit_id} подтверждена.{extra}",
            )
        except Exception:
            pass


@router.callback_query(F.data.startswith("dep_no:"))
async def cb_deposit_reject(call: CallbackQuery, state: FSMContext):
    if not can_confirm_deposit(call.from_user.id):
        return await call.answer("⛔ Нет доступа", show_alert=True)
    deposit_id = int(call.data.split(":")[1])
    await state.set_state(DepositReject.waiting_for_reason)
    await state.update_data(
        deposit_id=deposit_id, msg_chat=call.message.chat.id, msg_id=call.message.message_id
    )
    await call.answer()
    await call.message.answer("✍️ Укажите причину отклонения сдачи (одним сообщением):")


@router.message(DepositReject.waiting_for_reason)
async def process_deposit_reject_reason(message: Message, state: FSMContext, bot: Bot):
    # Round 6 (L_R1): повторный role-check после FSM-перехода. Между callback'ом
    # (cb_deposit_reject) и сообщением с причиной админ мог снять роль; без
    # этого юзер успевает выполнить reject уже не будучи confirmer'ом.
    if not can_confirm_deposit(message.from_user.id):
        await state.clear()
        return await message.answer("⛔ Нет доступа — действие отменено.")
    # Round 6 (L_R8): жёсткий cap на reason — DB-колонка TEXT, шлётся в Telegram.
    reason = (message.text or "").strip()[:500]
    if len(reason) < 3:
        return await message.answer("❌ Причина слишком короткая. Повторите.")
    data = await state.get_data()
    await state.clear()
    deposit_id = data.get("deposit_id")
    name = message.from_user.full_name or str(message.from_user.id)

    dep = await adb.get_cash_deposit(deposit_id)
    res = await adb.reject_cash_deposit(deposit_id, message.from_user.id, name, reason)
    if not res.get("ok"):
        return await message.answer(f"⚠️ {res.get('error', 'уже обработано')}")

    await message.answer(f"❌ Сдача #{deposit_id} отклонена.")
    if dep and dep.get("manager_id"):
        try:
            await bot.send_message(
                dep["manager_id"],
                f"❌ Ваша сдача #{deposit_id} отклонена.\nПричина: {esc(reason)}",
                parse_mode="HTML",
            )
        except Exception:
            pass
