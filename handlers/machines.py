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
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from config import WEBAPP_URL
from handlers._ui import (
    drop_keyboard,
    finish_card,
    finish_message,
    outcome_label,
    prompt_keyboard,
    set_message_markup,
    settle_card,
    settle_markup,
    webapp_keyboard,
)
from services import machine_deal_requests as mdr
from services import machines, money
from services.roles import cached_role, can_create_orders, is_boss
from utils.formatters import DIV
from utils.helpers import esc
from utils.keyboards import machine_request_callbacks, machine_request_keyboard

logger = logging.getLogger(__name__)
router = Router()


# ─── Форматирование ──────────────────────────────────────────────────────────


def _money(cents: int | None, currency: str = "USD") -> str:
    if not cents:
        return "—"
    return f"{money.format_cents(int(cents), decimals=0, sep=' ')} {esc(currency)}"


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
    pending = await mdr.list_requests(statuses=("pending",))
    if not deals and not pending:
        return await message.answer("✅ Открытых рассрочек и заявок по технике нет.")
    lines = [f"{DIV}"]
    if pending:
        # Заявки на одобрении — счётчиком и списком: решают их кнопками на
        # карточке-уведомлении или в WebApp, дублировать карточки здесь незачем.
        lines += [f"⏳ <b>На одобрении: {len(pending)}</b>", ""]
        for r in pending:
            lines.append(
                f"• #{r['id']} · {esc(mdr.KIND_LABELS.get(r['kind'], r['kind']))} · "
                f"{esc(r.get('machine_name') or '—')} · {esc(r.get('buyer_name') or '—')}"
            )
        lines.append("")
    if not deals:
        lines.append("<i>Решить заявки — в WebApp: «Склад → Техника».</i>")
        return await message.answer("\n".join(lines), parse_mode="HTML", reply_markup=webapp_keyboard())
    lines += ["💳 <b>Рассрочки по технике:</b>", ""]
    for d in deals:
        lines.append(
            f"• #{d['id']} · {esc(d['name'])} ({esc(d['vin'])})\n"
            f"  {_money(d['price_cents'], d.get('currency') or 'USD')} · "
            f"{esc(d['buyer_name'])} · до {esc(str(d.get('due_date') or '—'))}"
        )
    lines.append("")
    lines.append("<i>Закрыть рассрочку — в WebApp: «Заказы → Техника».</i>")
    await message.answer("\n".join(lines), parse_mode="HTML", reply_markup=webapp_keyboard())




# ─── Заявки на сделки: решение руководителя по карточке ──────────────────────
# Карточку шлёт `services.machine_deal_requests.notify_decision_card` (из
# процесса WebApp). Правила решения — там же: руководитель решает всегда,
# менеджер — только пока руководителя в системе нет. Бот и WebApp зовут одни и
# те же функции сервиса.


class MachineRework(StatesGroup):
    waiting_for_reason = State()  # руководитель пишет, что доработать


_REQUEST_SETTLED = {
    "approved": "✅ Заявка уже одобрена",
    "rejected": "❌ Заявка уже отклонена",
    "rework": "↩️ Заявка уже на доработке",
    "cancelled": "✖️ Заявка отозвана",
}


def _request_id(call: CallbackQuery) -> int | None:
    try:
        return int((call.data or "").split(":", 1)[1])
    except (IndexError, ValueError):
        return None


async def _settle_stale_machine_request(call: CallbackQuery, request_id: int) -> bool:
    """Заявка уже не ждёт решения (решили в WebApp или вторым руководителем) —
    погасить кнопки карточки исходом. Ждёт — кнопки не трогаем."""
    req = await mdr.get_request(request_id)
    if req and req.get("status") == "pending":
        return False
    label = _REQUEST_SETTLED.get((req or {}).get("status") or "", "ℹ️ Заявка уже решена")
    await settle_card(
        call, machine_request_callbacks(request_id), label,
        tail=webapp_keyboard("🌐 Техника — в WebApp"),
    )
    return True


def _actor(call_or_message) -> tuple[int, str, str]:
    user = call_or_message.from_user
    return user.id, (user.full_name or str(user.id)), cached_role(user.id)


async def _decide(call: CallbackQuery, op: str) -> None:
    if not can_create_orders(call.from_user.id):
        return await call.answer("Нет доступа", show_alert=True)
    request_id = _request_id(call)
    if request_id is None:
        return await call.answer("Некорректный запрос", show_alert=True)
    uid, name, role = _actor(call)
    fn = mdr.approve if op == "approve" else mdr.reject
    res = await fn(request_id, actor_id=uid, actor_name=name, actor_role=role)
    if not res.get("ok"):
        await call.answer(f"⚠️ {res.get('error')}", show_alert=True)
        await _settle_stale_machine_request(call, request_id)
        return
    if op == "approve":
        verb = "✅ Одобрено"
        note = "✅ <b>Одобрено</b>" + (" — руководителя нет, решили вы" if res.get("self_approved") else "")
    else:
        verb, note = "❌ Отклонено", "❌ <b>Отклонено</b> — машина в прежнем статусе"
    await call.answer(verb)
    base = getattr(call.message, "html_text", None) or getattr(call.message, "text", "") or ""
    markup = settle_markup(
        getattr(call.message, "reply_markup", None), machine_request_callbacks(request_id),
        outcome_label(verb, call.from_user), tail=webapp_keyboard("🌐 Техника — в WebApp"),
    )
    try:
        await call.message.edit_text(
            f"{base}\n\n{DIV}\n{note} — {esc(name)}", parse_mode="HTML", reply_markup=markup
        )
    except Exception:
        logger.debug("mdr: карточка не отредактирована", exc_info=True)
        try:
            await call.message.edit_reply_markup(reply_markup=markup)
        except Exception:
            logger.debug("mdr: клавиатуру заменить не удалось", exc_info=True)


@router.callback_query(F.data.startswith("mdr_ok:"))
async def cb_machine_request_approve(call: CallbackQuery):
    await _decide(call, "approve")


@router.callback_query(F.data.startswith("mdr_no:"))
async def cb_machine_request_reject(call: CallbackQuery):
    await _decide(call, "reject")


@router.callback_query(F.data.startswith("mdr_rw:"))
async def cb_machine_request_rework(call: CallbackQuery, state: FSMContext):
    """«На доработку» — причину спрашиваем одним сообщением (force_reply)."""
    if not can_create_orders(call.from_user.id):
        return await call.answer("Нет доступа", show_alert=True)
    request_id = _request_id(call)
    if request_id is None:
        return await call.answer("Некорректный запрос", show_alert=True)
    # Устаревшая карточка: не заводим ввод причины, который кончится отказом.
    if await _settle_stale_machine_request(call, request_id):
        return await call.answer("⚠️ Заявка уже обработана", show_alert=True)
    uid, _name, role = _actor(call)
    rights = await mdr.decision_rights(uid, role)
    if not rights["can_decide"]:
        return await call.answer("⛔ Решение по заявке принимает руководитель", show_alert=True)
    await state.set_state(MachineRework.waiting_for_reason)
    await state.update_data(
        mdr_id=request_id, msg_chat=call.message.chat.id, msg_id=call.message.message_id
    )
    await call.answer()
    await drop_keyboard(call, status="✍️ Ждём причину доработки…")
    prompt = await call.message.answer(
        "✍️ Что доработать в заявке? Одним сообщением — менеджер увидит причину:",
        reply_markup=prompt_keyboard(
            InlineKeyboardButton(text="✖️ Не возвращать", callback_data="mdr_rw_abort")
        ),
    )
    if prompt is not None:
        await state.update_data(prompt_chat=prompt.chat.id, prompt_id=prompt.message_id)


@router.callback_query(F.data == "mdr_rw_abort")
async def cb_machine_request_rework_abort(call: CallbackQuery, state: FSMContext, bot: Bot):
    """Нажали «На доработку» по ошибке: вернуть карточке кнопки, если ждёт."""
    data = await state.get_data()
    request_id = data.get("mdr_id")
    if await state.get_state() != MachineRework.waiting_for_reason.state or not request_id:
        await call.answer("Уже неактуально")
        await set_message_markup(bot, call.message.chat.id, call.message.message_id, None)
        return
    await state.clear()
    await call.answer("Возврат отменён")
    req = await mdr.get_request(int(request_id))
    if req and req.get("status") == "pending":
        await set_message_markup(
            bot, data.get("msg_chat"), data.get("msg_id"), machine_request_keyboard(int(request_id))
        )
    try:
        await call.message.edit_text(
            f"↩️ Возврат заявки #{int(request_id)} на доработку отменён — кнопки решения снова на карточке."
        )
    except Exception:
        logger.debug("mdr_rw_abort: вопрос не отредактирован", exc_info=True)


@router.message(MachineRework.waiting_for_reason)
async def process_machine_request_rework(message: Message, state: FSMContext, bot: Bot):
    if not can_create_orders(message.from_user.id):
        await state.clear()
        return await message.answer("⛔ Нет доступа — действие отменено.")
    reason = (message.text or "").strip()[:500]
    if len(reason) < 3:
        return await message.answer("❌ Причина слишком короткая. Повторите.")
    data = await state.get_data()
    await state.clear()
    request_id = int(data.get("mdr_id") or 0)
    uid, name, role = _actor(message)
    res = await mdr.return_for_rework(
        request_id, actor_id=uid, actor_name=name, actor_role=role, reason=reason
    )
    await set_message_markup(bot, data.get("prompt_chat"), data.get("prompt_id"), None)
    if not res.get("ok"):
        return await message.answer(f"⚠️ {esc(res.get('error') or '')}", parse_mode="HTML")
    note = f"↩️ Заявка #{request_id} возвращена на доработку: {esc(reason)}"
    if not await finish_message(
        bot, data.get("msg_chat"), data.get("msg_id"), note,
        outcome=outcome_label("↩️ На доработку", message.from_user),
    ):
        await message.answer(note, parse_mode="HTML")
