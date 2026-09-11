"""
Хэндлеры: учёт экскаваторов — быстрый просмотр из чата.

Раздел живёт в WebApp: карточка машины — это десяток полей, фотографии, сделка
с покупателем и история моточасов, то есть работа для экрана, а не для команды
вида `/sell 12 25000 Иванов Пётр`, где ошибку в поле нельзя исправить, только
повторить целиком.

В боте осталось то, ради чего его открывают между делом:

* `/machines` и карточка — посмотреть, что где стоит, не открывая WebApp;
* `/hours <id> <часы>` — показание снимают с площадки телефоном, одним числом;
  диалог из двух сообщений здесь только мешал бы;
* `/machine_deals` — открытые рассрочки: босс смотрит, кому напоминать.

Заведение машины, фотографии, статусы и сделки вырезаны — они в WebApp
(`handlers.start._RETIRED_COMMANDS` подскажет набравшему по памяти, куда идти).

Парк 10–25 машин, поэтому список — простой перечень кнопками: ни пагинации, ни
поиска, ни кэша (T4.2 прямо запрещает их добавлять).

Роли: смотреть и вводить моточасы — менеджер и выше. Себестоимость режет
`services.machines`, здесь её просто не запрашивают отдельно.
"""

import logging

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from config import WEBAPP_URL
from handlers._ui import finish_card, webapp_keyboard
from services import machines, money
from services.roles import cached_role, can_create_orders, is_boss
from utils.formatters import DIV
from utils.helpers import esc

logger = logging.getLogger(__name__)
router = Router()


# ─── Форматирование ──────────────────────────────────────────────────────────


def _money(cents: int | None, currency: str = "USD") -> str:
    if not cents:
        return "—"
    return f"{money.format_cents(int(cents), decimals=0, sep=' ')} {currency}"


def format_machine(m: dict, *, photos: int = 0) -> str:
    """Карточка машины. `cost_cents` печатается, только если он в словаре —
    для менеджера сервис его уже убрал, так что забыть про роль здесь нельзя."""
    currency = m.get("currency") or "USD"
    status = machines.STATUS_LABELS.get(m.get("status"), m.get("status") or "—")
    lines = [
        DIV,
        f"🚜 <b>{esc(m.get('name') or '—')}</b>   <code>{status}</code>",
        f"VIN: <code>{esc(m.get('vin') or '—')}</code>",
    ]
    spec = " · ".join(
        part
        for part in (
            esc(m.get("brand") or ""),
            esc(m.get("model") or ""),
            str(m["year"]) if m.get("year") else "",
        )
        if part
    )
    if spec:
        lines.append(f"🔧 {spec}")
    if m.get("hours") is not None:
        lines.append(f"⏱ Моточасы: <b>{m['hours']}</b>")
    lines.append(f"💰 Цена: <b>{_money(m.get('price_cents'), currency)}</b>")
    if "cost_cents" in m:
        lines.append(f"🏷 Себестоимость: <b>{_money(m.get('cost_cents'), currency)}</b>")
    for label, key in (("📍", "location"), ("📦", "container_no"), ("🗓", "eta_date")):
        if m.get(key):
            lines.append(f"{label} {esc(str(m[key]))}")
    if m.get("notes"):
        lines.append(f"📝 {esc(m['notes'])}")
    lines.append(f"📷 Фото: {photos}")
    return "\n".join(lines)


def _card_keyboard(machine_id: int):
    """Кнопки карточки: назад к списку и вход в WebApp.

    Действия (статус, сделка, фото, правка) переехали в WebApp — рисовать их
    здесь значит обещать операцию, которой в боте больше нет. web_app-кнопку
    Bot API принимает только с https-URL, иначе отвергает сообщение целиком.
    """
    kb = InlineKeyboardBuilder()
    kb.button(text="🚜 Все машины", callback_data="mach_list")
    if WEBAPP_URL and WEBAPP_URL.startswith("https://"):
        from aiogram.types import WebAppInfo

        kb.button(text="🌐 Открыть в WebApp", web_app=WebAppInfo(url=WEBAPP_URL))
    kb.adjust(1)
    return kb.as_markup()


# ─── Список и карточка ───────────────────────────────────────────────────────


async def _send_list(target: Message, user_id: int) -> None:
    role = cached_role(user_id)
    rows = await machines.list_machines(role=role)
    if not rows:
        return await target.answer(
            "🚜 Машин пока нет.\n\nЗавести — в WebApp: «Заказы → Техника».",
            parse_mode="HTML",
        )
    kb = InlineKeyboardBuilder()
    for m in rows:
        status = machines.STATUS_LABELS.get(m.get("status"), "")
        kb.button(text=f"{status} {m['name']} · {m['vin']}"[:60], callback_data=f"mach:{m['id']}")
    kb.adjust(1)
    await target.answer(
        f"🚜 <b>Машины ({len(rows)}):</b>", parse_mode="HTML", reply_markup=kb.as_markup()
    )


@router.message(Command("machines"))
async def cmd_machines(message: Message):
    if not can_create_orders(message.from_user.id):
        return await message.answer("⛔ Нет доступа.")
    await _send_list(message, message.from_user.id)


@router.callback_query(F.data == "mach_list")
async def cb_machines_list(call: CallbackQuery):
    if not can_create_orders(call.from_user.id):
        return await call.answer("Нет доступа", show_alert=True)
    await call.answer()
    await _send_list(call.message, call.from_user.id)


async def _show_card(target: Message, machine_id: int, user_id: int) -> None:
    role = cached_role(user_id)
    machine = await machines.get_machine(machine_id, role=role)
    if not machine:
        return await target.answer("❌ Машина не найдена.")
    photos = await machines.list_photos(machine_id)
    await target.answer(
        format_machine(machine, photos=len(photos)),
        parse_mode="HTML",
        reply_markup=_card_keyboard(machine_id),
    )


@router.callback_query(F.data.startswith("mach:"))
async def cb_machine_card(call: CallbackQuery):
    if not can_create_orders(call.from_user.id):
        return await call.answer("Нет доступа", show_alert=True)
    await call.answer()
    await _show_card(call.message, int(call.data.split(":")[1]), call.from_user.id)


# ─── Моточасы ────────────────────────────────────────────────────────────────


@router.message(Command("hours"))
async def cmd_hours(message: Message):
    """`/hours <id> <часы>` — показание одной строкой.

    Единственное действие, оставшееся в боте: моточасы снимают с площадки, где
    открыть WebApp дольше, чем набрать два числа. Диалог из двух сообщений
    здесь только мешал бы — как у `/pay` и `/deposit`.

    Откат показания (счётчик заменили) подтверждает руководитель кнопкой:
    показание меньше предыдущего почти всегда опечатка.
    """
    if not can_create_orders(message.from_user.id):
        return await message.answer("⛔ Нет доступа.")
    parts = (message.text or "").split()
    if len(parts) < 3 or not parts[1].isdigit() or not parts[2].replace(" ", "").isdigit():
        return await message.answer(
            "⏱ Формат: <code>/hours 12 15200</code>\n"
            "Номер машины — из <code>/machines</code>.",
            parse_mode="HTML",
        )
    machine_id, hours = int(parts[1]), int(parts[2])
    res = await machines.add_hours(
        machine_id, hours,
        user_id=message.from_user.id,
        full_name=message.from_user.full_name or "",
    )
    if not res["ok"]:
        if res.get("needs_force") and is_boss(message.from_user.id):
            kb = InlineKeyboardBuilder()
            kb.button(
                text="✅ Всё верно, счётчик заменён",
                callback_data=f"mach_hours_f:{machine_id}:{hours}",
            )
            kb.adjust(1)
            return await message.answer(
                f"⚠️ {esc(res['error'])}\n\n"
                f"Если счётчик меняли — подтвердите, запись уйдёт в аудит.",
                parse_mode="HTML",
                reply_markup=kb.as_markup(),
            )
        return await message.answer(f"⚠️ {esc(res['error'])}", parse_mode="HTML")
    await message.answer(f"✅ Моточасы: <b>{res['hours']}</b>", parse_mode="HTML")
    await _show_card(message, machine_id, message.from_user.id)


@router.callback_query(F.data.startswith("mach_hours_f:"))
async def cb_force_hours(call: CallbackQuery):
    """Подтверждение отката моточасов (замена счётчика) — только босс."""
    if not is_boss(call.from_user.id):
        return await call.answer("⛔ Только руководитель", show_alert=True)
    _, machine_id, hours = call.data.split(":")
    res = await machines.add_hours(
        int(machine_id), int(hours),
        user_id=call.from_user.id,
        full_name=call.from_user.full_name or "",
        force=True,
    )
    if not res["ok"]:
        return await call.answer(f"⚠️ {res['error']}", show_alert=True)
    await call.answer("✅ Записано")
    await finish_card(call, f"⏱ Моточасы: {hours} (замена счётчика)")


# ─── Рассрочки ───────────────────────────────────────────────────────────────


@router.message(Command("machine_deals"))
async def cmd_open_credits(message: Message, bot: Bot):
    """Незакрытые рассрочки по технике — босс смотрит, кому напоминать.

    Закрывают рассрочку в WebApp: там видно сумму, срок и всю карточку машины.
    """
    if not is_boss(message.from_user.id):
        return await message.answer("⛔ Нет доступа.")
    deals = await machines.get_open_credit_deals(role=cached_role(message.from_user.id))
    if not deals:
        return await message.answer("✅ Открытых рассрочек по технике нет.")
    lines = [f"{DIV}", "💳 <b>Рассрочки по технике:</b>", ""]
    for d in deals:
        lines.append(
            f"• #{d['id']} · {esc(d['name'])} ({esc(d['vin'])})\n"
            f"  {_money(d['price_cents'], d.get('currency') or 'USD')} · "
            f"{esc(d['buyer_name'])} · до {esc(str(d.get('due_date') or '—'))}"
        )
    lines.append("")
    lines.append("<i>Закрыть рассрочку — в WebApp: «Заказы → Техника».</i>")
    await message.answer("\n".join(lines), parse_mode="HTML", reply_markup=webapp_keyboard())


