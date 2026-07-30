"""
Хэндлеры: учёт экскаваторов (волна 4, T4.2).

Почему это в боте, хотя волна 3 переносила экраны в WebApp: у машин в WebApp
экрана нет вовсе, а два ключевых действия по своей природе телеграмные —
фотография приезжает как `file_id` (в WebApp её пришлось бы заливать через
свой сторедж, на Railway эфемерная ФС) и моточасы вводят с площадки телефоном,
одним числом. Остальное — просмотр и решения, то есть ровно то, что бот и так
делает лучше всего.

Парк 10–25 машин, поэтому список — простой перечень кнопками: ни пагинации, ни
поиска, ни кэша (T4.2 прямо запрещает их добавлять).

Роли: менеджер — завести машину, добавить фото и моточасы. Босс/админ — статусы,
сделки и себестоимость. Срез себестоимости делает `services.machines`, здесь её
просто не запрашивают отдельно.
"""

import logging

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from handlers._ui import drop_keyboard, finish_card
from services import machines, money
from services.roles import cached_role, can_create_orders, is_boss
from utils.formatters import DIV
from utils.helpers import esc

logger = logging.getLogger(__name__)
router = Router()


class MachineFlow(StatesGroup):
    waiting_hours = State()
    waiting_photo = State()


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


def _card_keyboard(machine_id: int, *, boss: bool, status: str):
    kb = InlineKeyboardBuilder()
    kb.button(text="⏱ Моточасы", callback_data=f"mach_hours:{machine_id}")
    kb.button(text="📷 Фото", callback_data=f"mach_photo:{machine_id}")
    rows = [2]
    if boss:
        # Показываем только осмысленные переходы — «продать» делается командой
        # /sell или /credit, там нужны цена и покупатель.
        nexts = {
            "in_transit": [("🏗 На склад", "in_stock")],
            "in_stock": [("🔒 Бронь", "reserved")],
            "reserved": [("🏗 Снять бронь", "in_stock")],
            "on_credit": [],
            "sold": [("📦 В архив", "archived")],
            "archived": [],
        }.get(status, [])
        for text, target in nexts:
            kb.button(text=text, callback_data=f"mach_st:{machine_id}:{target}")
        if nexts:
            rows.append(len(nexts))
    kb.button(text="🚜 Все машины", callback_data="mach_list")
    rows.append(1)
    kb.adjust(*rows)
    return kb.as_markup()


# ─── Список и карточка ───────────────────────────────────────────────────────


async def _send_list(target: Message, user_id: int) -> None:
    role = cached_role(user_id)
    rows = await machines.list_machines(role=role)
    if not rows:
        return await target.answer(
            "🚜 Машин пока нет.\n\nЗавести: <code>/newmachine VIN Название</code>",
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
        reply_markup=_card_keyboard(
            machine_id, boss=is_boss(user_id), status=machine.get("status") or ""
        ),
    )


@router.callback_query(F.data.startswith("mach:"))
async def cb_machine_card(call: CallbackQuery):
    if not can_create_orders(call.from_user.id):
        return await call.answer("Нет доступа", show_alert=True)
    await call.answer()
    await _show_card(call.message, int(call.data.split(":")[1]), call.from_user.id)


# ─── Заведение машины ────────────────────────────────────────────────────────


@router.message(Command("newmachine"))
async def cmd_new_machine(message: Message):
    """`/newmachine <VIN> <название>` — одной строкой, как /pay и /deposit.

    Остальные поля (цена, год, локация) правит босс в карточке: на этапе
    приёмки контейнера известны только серийник и модель.
    """
    if not can_create_orders(message.from_user.id):
        return await message.answer("⛔ Нет доступа.")
    parts = (message.text or "").split(maxsplit=2)
    if len(parts) < 3:
        return await message.answer(
            "🚜 Формат: <code>/newmachine VIN Название</code>\n"
            "Например: <code>/newmachine JCB3CX-12345 JCB 3CX 2019</code>",
            parse_mode="HTML",
        )
    user = message.from_user
    res = await machines.create_machine(
        vin=parts[1],
        name=parts[2][:120],
        created_by=user.id,
        creator_name=user.full_name or str(user.id),
    )
    if not res["ok"]:
        return await message.answer(f"⚠️ {esc(res['error'])}", parse_mode="HTML")
    await message.answer(f"✅ Машина заведена: <code>{esc(res['vin'])}</code>", parse_mode="HTML")
    await _show_card(message, res["machine_id"], user.id)


# ─── Моточасы ────────────────────────────────────────────────────────────────


@router.callback_query(F.data.startswith("mach_hours:"))
async def cb_add_hours(call: CallbackQuery, state: FSMContext):
    if not can_create_orders(call.from_user.id):
        return await call.answer("Нет доступа", show_alert=True)
    machine_id = int(call.data.split(":")[1])
    await state.set_state(MachineFlow.waiting_hours)
    await state.update_data(machine_id=machine_id)
    await call.answer()
    # T3.2: гасим кнопки карточки на время ввода — иначе второй тап по другой
    # машине перепишет machine_id в state, и часы уедут не туда.
    await drop_keyboard(call)
    await call.message.answer("⏱ Введите текущие моточасы (целое число):")


@router.message(MachineFlow.waiting_hours)
async def process_hours(message: Message, state: FSMContext):
    if not can_create_orders(message.from_user.id):
        await state.clear()
        return await message.answer("⛔ Нет доступа — действие отменено.")
    raw = (message.text or "").strip().replace(" ", "")
    if not raw.isdigit():
        return await message.answer("❌ Нужно целое число, например <code>15200</code>.",
                                    parse_mode="HTML")
    data = await state.get_data()
    machine_id = int(data.get("machine_id") or 0)
    await state.clear()
    res = await machines.add_hours(
        machine_id, int(raw),
        user_id=message.from_user.id,
        full_name=message.from_user.full_name or "",
    )
    if not res["ok"]:
        if res.get("needs_force") and is_boss(message.from_user.id):
            kb = InlineKeyboardBuilder()
            kb.button(
                text="✅ Всё верно, счётчик заменён",
                callback_data=f"mach_hours_f:{machine_id}:{raw}",
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


# ─── Фото ────────────────────────────────────────────────────────────────────


@router.callback_query(F.data.startswith("mach_photo:"))
async def cb_add_photo(call: CallbackQuery, state: FSMContext):
    if not can_create_orders(call.from_user.id):
        return await call.answer("Нет доступа", show_alert=True)
    await state.set_state(MachineFlow.waiting_photo)
    await state.update_data(machine_id=int(call.data.split(":")[1]))
    await call.answer()
    await drop_keyboard(call)
    await call.message.answer(
        "📷 Пришлите фотографии машины. Можно несколько подряд.\n"
        "Когда закончите — /done"
    )


@router.message(MachineFlow.waiting_photo, F.photo)
async def process_photo(message: Message, state: FSMContext):
    """Фотографии принимаем пачкой: альбом приходит отдельными сообщениями,
    поэтому состояние не сбрасываем до /done."""
    if not can_create_orders(message.from_user.id):
        await state.clear()
        return await message.answer("⛔ Нет доступа — действие отменено.")
    data = await state.get_data()
    machine_id = int(data.get("machine_id") or 0)
    # Берём самый крупный вариант: Telegram отдаёт лестницу размеров.
    photo = message.photo[-1]
    res = await machines.add_photo(
        machine_id,
        tg_file_id=photo.file_id,
        file_unique_id=photo.file_unique_id,
        uploaded_by=message.from_user.id,
        caption=(message.caption or None),
    )
    if not res["ok"]:
        await state.clear()
        return await message.answer(f"⚠️ {esc(res['error'])}", parse_mode="HTML")
    if res.get("duplicate"):
        return await message.answer("📷 Эта фотография уже в карточке.")
    total = len(await machines.list_photos(machine_id))
    await message.answer(f"📷 Добавлено. Всего фото: {total}. Ещё или /done")


@router.message(MachineFlow.waiting_photo, Command("done"))
async def cmd_photo_done(message: Message, state: FSMContext):
    data = await state.get_data()
    await state.clear()
    machine_id = int(data.get("machine_id") or 0)
    await _show_card(message, machine_id, message.from_user.id)


@router.message(MachineFlow.waiting_photo)
async def process_photo_wrong_type(message: Message):
    await message.answer("❌ Пришлите именно фотографию (не файлом) или /done.")


# ─── Статусы ─────────────────────────────────────────────────────────────────


@router.callback_query(F.data.startswith("mach_st:"))
async def cb_set_status(call: CallbackQuery):
    if not is_boss(call.from_user.id):
        return await call.answer("⛔ Только руководитель", show_alert=True)
    _, machine_id, target = call.data.split(":")
    machine = await machines.get_machine(int(machine_id), role="boss")
    if not machine:
        return await call.answer("Машина не найдена", show_alert=True)
    res = await machines.set_status(
        int(machine_id), target,
        user_id=call.from_user.id,
        full_name=call.from_user.full_name or "",
        expected=machine.get("status"),
    )
    if not res["ok"]:
        return await call.answer(f"⚠️ {res['error']}", show_alert=True)
    await call.answer("✅ Статус обновлён")
    label = machines.STATUS_LABELS.get(target, target)
    # T3.2: кнопки старого статуса больше не применимы — гасим карточку и
    # показываем свежую.
    await finish_card(call, f"Статус: {label}")
    await _show_card(call.message, int(machine_id), call.from_user.id)


# ─── Сделки ──────────────────────────────────────────────────────────────────


def _parse_deal_args(text: str, *, with_due: bool) -> tuple[dict | None, str]:
    """`/sell <id> <цена> <покупатель>` / `/credit <id> <цена> <ГГГГ-ММ-ДД> <покупатель>`.

    Разбор одной строкой — как у /pay и /deposit: сделку оформляют с телефона,
    пятишаговый диалог здесь только мешал бы.
    """
    parts = (text or "").split(maxsplit=4 if with_due else 3)
    need = 5 if with_due else 4
    if len(parts) < need:
        return None, "формат"
    if not parts[1].isdigit():
        return None, "id"
    price_cents = money.parse_amount(parts[2])
    if not price_cents:
        return None, "цена"
    data = {"machine_id": int(parts[1]), "price_cents": price_cents}
    if with_due:
        due = parts[3]
        if len(due) != 10 or due[4] != "-" or due[7] != "-":
            return None, "срок"
        data["due_date"] = due
        data["buyer_name"] = parts[4][:120]
    else:
        data["buyer_name"] = parts[3][:120]
    return data, ""


async def _create_deal(message: Message, kind: str, with_due: bool, usage: str) -> None:
    if not is_boss(message.from_user.id):
        return await message.answer("⛔ Сделки оформляет руководитель.")
    data, err = _parse_deal_args(message.text or "", with_due=with_due)
    if data is None:
        hint = {
            "id": "Номер машины — число (см. /machines).",
            "цена": "Цена — положительное число.",
            "срок": "Срок — в формате ГГГГ-ММ-ДД.",
        }.get(err, "")
        return await message.answer(
            f"🚫 {hint}\nФормат: <code>{usage}</code>", parse_mode="HTML"
        )
    res = await machines.create_deal(
        data["machine_id"],
        kind=kind,
        price_cents=data["price_cents"],
        buyer_name=data["buyer_name"],
        due_date=data.get("due_date"),
        created_by=message.from_user.id,
        creator_name=message.from_user.full_name or "",
    )
    if not res["ok"]:
        return await message.answer(f"⚠️ {esc(res['error'])}", parse_mode="HTML")
    await message.answer(
        f"✅ Сделка #{res['deal_id']} оформлена: "
        f"<b>{_money(data['price_cents'])}</b> · {esc(data['buyer_name'])}",
        parse_mode="HTML",
    )
    await _show_card(message, data["machine_id"], message.from_user.id)


@router.message(Command("sell"))
async def cmd_sell(message: Message):
    await _create_deal(message, "sale", False, "/sell 12 25000 Иванов Пётр")


@router.message(Command("credit"))
async def cmd_credit(message: Message):
    await _create_deal(message, "credit", True, "/credit 12 25000 2026-12-31 Иванов Пётр")


@router.message(Command("machine_deals"))
async def cmd_open_credits(message: Message, bot: Bot):
    """Незакрытые рассрочки по технике — босс смотрит, кому напоминать."""
    if not is_boss(message.from_user.id):
        return await message.answer("⛔ Нет доступа.")
    deals = await machines.get_open_credit_deals()
    if not deals:
        return await message.answer("✅ Открытых рассрочек по технике нет.")
    lines = [f"{DIV}", "💳 <b>Рассрочки по технике:</b>", ""]
    kb = InlineKeyboardBuilder()
    for d in deals:
        lines.append(
            f"• #{d['id']} · {esc(d['name'])} ({esc(d['vin'])})\n"
            f"  {_money(d['price_cents'], d.get('currency') or 'USD')} · "
            f"{esc(d['buyer_name'])} · до {esc(str(d.get('due_date') or '—'))}"
        )
        kb.button(text=f"✅ Закрыть #{d['id']}", callback_data=f"mach_deal_close:{d['id']}")
    kb.adjust(1)
    await message.answer("\n".join(lines), parse_mode="HTML", reply_markup=kb.as_markup())


@router.callback_query(F.data.startswith("mach_deal_close:"))
async def cb_close_deal(call: CallbackQuery):
    if not is_boss(call.from_user.id):
        return await call.answer("⛔ Только руководитель", show_alert=True)
    deal_id = int(call.data.split(":")[1])
    res = await machines.close_deal(
        deal_id, user_id=call.from_user.id, full_name=call.from_user.full_name or ""
    )
    if not res["ok"]:
        return await call.answer(f"⚠️ {res['error']}", show_alert=True)
    await call.answer("✅ Рассрочка закрыта")
    await finish_card(call, f"✅ Рассрочка #{deal_id} закрыта")
