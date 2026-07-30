"""
Хэндлеры: кредитные лимиты контрагентов (IMPLEMENTATION.md §3).

Босс/админ: /limit — сводка по клиентам (лимит + текущий долг + свободный
остаток). Кнопка «✏️ Изменить» → ввод новой суммы → set_credit_limit.

Логика и расчёт долга — в services.database (get_credit_overview /
set_credit_limit); тут только Telegram-UI.
"""

import math
import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from services import async_db as adb
from services.roles import can_change_credit_limit
from utils.formatters import DIV
from handlers._ui import drop_keyboard
from utils.helpers import esc

logger = logging.getLogger(__name__)
router = Router()


class LimitFlow(StatesGroup):
    waiting_amount = State()


def _fmt(x: float) -> str:
    from services import money

    return money.format_cents(money.to_cents(x or 0), decimals=2, sep=" ")


def _overview_keyboard(agents: list[dict]):
    kb = InlineKeyboardBuilder()
    for a in agents[:20]:
        mark = "🔴" if a["over_limit"] else "✏️"
        kb.button(text=f"{mark} {a['agent_name'][:24]}", callback_data=f"lim_set:{a['agent_id']}")
    kb.adjust(1)
    return kb.as_markup()


def _render_overview(agents: list[dict]) -> str:
    if not agents:
        return f"{DIV}\n💳 <b>Кредитные лимиты</b>\n\nНет контрагентов с активными заказами."
    lines = [f"{DIV}", "💳 <b>Кредитные лимиты</b>", ""]
    for a in agents[:20]:
        mark = "🔴" if a["over_limit"] else "🟢"
        lines.append(
            f"{mark} <b>{esc(a['agent_name'])}</b>\n"
            f"   лимит {_fmt(a['limit'])} · долг {_fmt(a['debt'])} · "
            f"свободно {_fmt(a['free'])} USD"
        )
    return "\n".join(lines)


@router.message(Command("limit"))
async def cmd_limit(message: Message):
    if not can_change_credit_limit(message.from_user.id):
        return await message.answer("⛔ Управление лимитами доступно боссу/админу.")
    agents = await adb.get_credit_overview()
    await message.answer(
        _render_overview(agents),
        parse_mode="HTML",
        reply_markup=_overview_keyboard(agents) if agents else None,
    )


@router.callback_query(F.data.startswith("lim_set:"))
async def cb_limit_set(call: CallbackQuery, state: FSMContext):
    if not can_change_credit_limit(call.from_user.id):
        return await call.answer("⛔ Нет доступа", show_alert=True)
    agent_id = call.data.split(":", 1)[1]
    agents = await adb.get_credit_overview()
    agent = next((a for a in agents if a["agent_id"] == agent_id), None)
    if not agent:
        return await call.answer("Контрагент не найден", show_alert=True)
    await state.clear()
    await state.set_state(LimitFlow.waiting_amount)
    await state.update_data(agent_id=agent_id, agent_name=agent["agent_name"])
    await call.answer()
    # T3.2: снимаем клавиатуру списка. Второй тап по другому контрагенту
    # переписывал agent_id в FSM — сумма уходила не тому клиенту.
    await drop_keyboard(call)
    await call.message.answer(
        f"💳 <b>{esc(agent['agent_name'])}</b>\n"
        f"Текущий лимит: <b>{_fmt(agent['limit'])} USD</b>\n\n"
        f"Введите новый лимит (число, USD):",
        parse_mode="HTML",
    )


@router.message(LimitFlow.waiting_amount)
async def process_limit_amount(message: Message, state: FSMContext):
    # T2.10: повторный role-check после FSM-перехода — как в deposits.py и
    # pricing.py. Между нажатием `lim_set:` и вводом суммы админ мог снять
    # роль; это был единственный FSM в проекте без такой перепроверки.
    if not can_change_credit_limit(message.from_user.id):
        await state.clear()
        return await message.answer("⛔ Нет доступа — действие отменено.")

    raw = (message.text or "").strip().replace(",", ".").replace(" ", "")
    # Проверка была `amount < 0`, а `float('inf') < 0` — ложь, поэтому
    # «inf»/«nan» проходили насквозь. Бесконечный лимит делает ЛЮБОЙ долг
    # «свободным»: кредит-чек перестаёт срабатывать, а overview ломается на
    # арифметике с inf (§5.2.8). Валидация — та же, что в /api/credit/set.
    try:
        amount = float(raw)
    except ValueError:
        return await message.answer("❌ Лимит должен быть числом. Повторите.")
    if not (math.isfinite(amount) and 0 <= amount < 10_000_000):
        return await message.answer(
            "❌ Лимит должен быть числом от 0 до 10 000 000. Повторите."
        )

    data = await state.get_data()
    await state.clear()
    agent_id = data.get("agent_id")
    agent_name = data.get("agent_name", "")
    # Лимит — только контрагенту, на которого реально был заказ (паритет с
    # WebApp): иначе в credit_limits заводится сирота, которого нет в overview
    # и который никогда не всплывёт в интерфейсе.
    if not agent_id or not await adb.agent_has_order(agent_id):
        return await message.answer(
            "❌ Лимит можно задать только контрагенту, на которого есть заказ."
        )
    await adb.set_credit_limit(
        agent_id, agent_name, amount, set_by=message.from_user.id, notes="через /limit"
    )
    await message.answer(
        f"✅ Лимит контрагента <b>{esc(agent_name)}</b> установлен: <b>{_fmt(amount)} USD</b>.",
        parse_mode="HTML",
    )
