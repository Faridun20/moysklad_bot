"""
Бот-команды управления курсами валют и ценами товаров — паритет с WebApp (#24).

  /rates  — посмотреть/задать курсы валют к базовой (ALLOWED_CURRENCIES).
  /prices — посмотреть заданные цены товаров; задать цену новому через
            `/prices ИМЯ` (поиск по каталогу-снапшоту).

Доступ — только boss/admin (как allowed_roles=("admin","boss") в webapp-эндпоинтах
/api/currency/rates/set и /api/products/prices/set). Пользовательский ввод
эскейпится через esc() (CLAUDE.md). Кэш курсов/цен инвалидируется внутри
set_currency_rate/set_product_price.
"""

import asyncio
import logging

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from config import ALLOWED_CURRENCIES, BASE_CURRENCY
from services import async_db as adb
from services import snapshot
from services.roles import is_boss
from handlers._ui import drop_keyboard
from utils.helpers import esc
from utils.formatters import DIV

logger = logging.getLogger(__name__)
router = Router()


class RateFlow(StatesGroup):
    waiting_rate = State()


class PriceFlow(StatesGroup):
    waiting_price = State()


def _num(text: str | None) -> float | None:
    try:
        return float((text or "").strip().replace(",", ".").replace(" ", ""))
    except ValueError:
        return None


# ─── Курсы валют ──────────────────────────────────────────────────────────────


@router.message(Command("rates"))
async def cmd_rates(message: Message):
    if not is_boss(message.from_user.id):
        return await message.answer("⛔ Курсы валют доступны боссу/админу.")
    rates = await adb.get_all_currency_rates()
    by_code = {r["currency_code"]: r for r in rates}

    lines = [DIV, f"💱 <b>Курсы к базовой валюте ({esc(BASE_CURRENCY)})</b>", ""]
    lines.append(f"• <b>{esc(BASE_CURRENCY)}</b> = 1 (база)")
    kb = InlineKeyboardBuilder()
    for code in ALLOWED_CURRENCIES:
        if code == BASE_CURRENCY:
            continue
        r = by_code.get(code)
        val = str(r["rate_to_base"]) if r else "—"
        lines.append(f"• <b>{esc(code)}</b> = {esc(val)}")
        kb.button(text=f"✏️ {code}", callback_data=f"rate_set:{code}")
    kb.adjust(3)
    await message.answer("\n".join(lines), parse_mode="HTML", reply_markup=kb.as_markup())


@router.callback_query(F.data.startswith("rate_set:"))
async def cb_rate_set(call: CallbackQuery, state: FSMContext):
    if not is_boss(call.from_user.id):
        return await call.answer("⛔ Нет доступа", show_alert=True)
    code = call.data.split(":", 1)[1]
    await state.set_state(RateFlow.waiting_rate)
    await state.update_data(code=code)
    await call.answer()
    # T3.2: снимаем клавиатуру списка при входе в FSM. Иначе второй тап по
    # другому коду переключал выбранную валюту посреди ввода курса, и число
    # уходило не туда.
    await drop_keyboard(call)
    await call.message.answer(
        f"💱 Введите курс <b>1 {esc(code)}</b> в {esc(BASE_CURRENCY)} "
        f"(число, напр. <code>0.000079</code>):",
        parse_mode="HTML",
    )


@router.message(RateFlow.waiting_rate)
async def process_rate(message: Message, state: FSMContext):
    if not is_boss(message.from_user.id):
        await state.clear()
        return await message.answer("⛔ Нет доступа — отменено.")
    rate = _num(message.text)
    if rate is None or rate <= 0:
        return await message.answer("❌ Курс должен быть положительным числом. Повторите.")
    data = await state.get_data()
    await state.clear()
    code = data["code"]
    ok, err = await adb.set_currency_rate(code, rate, message.from_user.id)
    if not ok:
        return await message.answer(f"❌ {esc(err or 'не удалось сохранить')}", parse_mode="HTML")
    await message.answer(
        f"✅ Курс <b>{esc(code)}</b> = {rate} {esc(BASE_CURRENCY)} сохранён.",
        parse_mode="HTML",
    )


# ─── Цены товаров ─────────────────────────────────────────────────────────────


@router.message(Command("prices"))
async def cmd_prices(message: Message):
    if not is_boss(message.from_user.id):
        return await message.answer("⛔ Цены товаров доступны боссу/админу.")
    parts = (message.text or "").split(maxsplit=1)
    query = parts[1].strip() if len(parts) > 1 else ""

    if query:
        found = await asyncio.to_thread(snapshot.search_products, query, 10)
        if not found:
            return await message.answer(
                "Ничего не найдено в каталоге. Уточните запрос или обновите снапшот."
            )
        kb = InlineKeyboardBuilder()
        for p in found:
            kb.button(text=(p["name"] or "?")[:48], callback_data=f"price_set:{p['ms_id']}")
        kb.adjust(1)
        return await message.answer(
            "Выберите товар для установки цены:", reply_markup=kb.as_markup()
        )

    prices = await adb.get_all_product_prices()
    if not prices:
        return await message.answer(
            "Цены ещё не заданы.\nЧтобы задать — <code>/prices ИМЯ</code> (поиск по каталогу).",
            parse_mode="HTML",
        )
    lines = [DIV, "🏷 <b>Цены товаров</b> (продажа / себестоимость):", ""]
    kb = InlineKeyboardBuilder()
    for p in prices[:20]:
        sale = p.get("sale_price")
        cost = p.get("cost_price")
        cur = p.get("currency") or BASE_CURRENCY
        sale_s = sale if sale is not None else "—"
        cost_s = cost if cost is not None else "—"
        lines.append(f"• {esc(p['product_name'])}: {sale_s} / {cost_s} {esc(cur)}")
        kb.button(text=f"✏️ {(p['product_name'] or '?')[:40]}", callback_data=f"price_set:{p['ms_id']}")
    kb.adjust(1)
    lines.append("")
    lines.append("Новый товар: <code>/prices ИМЯ</code>")
    await message.answer("\n".join(lines), parse_mode="HTML", reply_markup=kb.as_markup())


@router.callback_query(F.data.startswith("price_set:"))
async def cb_price_set(call: CallbackQuery, state: FSMContext):
    if not is_boss(call.from_user.id):
        return await call.answer("⛔ Нет доступа", show_alert=True)
    ms_id = call.data.split(":", 1)[1]

    existing = await adb.get_product_price(ms_id)
    if existing:
        name = existing.get("product_name") or ms_id
        cur_cost = existing.get("cost_price")
        cur_currency = existing.get("currency") or BASE_CURRENCY
    else:
        prod = await asyncio.to_thread(snapshot.get_product, ms_id)
        name = (prod or {}).get("name") or ms_id
        cur_cost = None
        cur_currency = BASE_CURRENCY

    await state.set_state(PriceFlow.waiting_price)
    await state.update_data(ms_id=ms_id, name=name, cur_cost=cur_cost, cur_currency=cur_currency)
    await call.answer()
    await drop_keyboard(call)  # T3.2: см. cb_rate_set — тот же класс ошибки
    await call.message.answer(
        f"🏷 Товар: <b>{esc(name)}</b>\n"
        f"Введите «<b>продажа себестоимость</b>» через пробел или только продажу.\n"
        f"Напр. <code>150 100</code> или <code>150</code>:",
        parse_mode="HTML",
    )


@router.message(PriceFlow.waiting_price)
async def process_price(message: Message, state: FSMContext):
    if not is_boss(message.from_user.id):
        await state.clear()
        return await message.answer("⛔ Нет доступа — отменено.")
    raw = (message.text or "").strip().replace(",", ".")
    tokens = raw.split()
    sale = _num(tokens[0]) if tokens else None
    if sale is None or sale <= 0:
        return await message.answer("❌ Нужна цена продажи положительным числом. Повторите.")
    data = await state.get_data()
    # Себестоимость не указали → сохраняем прежнюю (set_product_price иначе
    # перезапишет её в NULL: UPSERT пишет переданные значения как есть).
    cost = data.get("cur_cost")
    if len(tokens) > 1:
        c = _num(tokens[1])
        if c is None or c <= 0:
            return await message.answer("❌ Себестоимость должна быть положительным числом.")
        cost = c
    await state.clear()
    ok, err = await adb.set_product_price(
        data["ms_id"],
        data["name"],
        sale,
        cost,
        data.get("cur_currency") or BASE_CURRENCY,
        message.from_user.id,
    )
    if not ok:
        return await message.answer(f"❌ {esc(err or 'ошибка')}", parse_mode="HTML")
    await message.answer(f"✅ Цена для «{esc(data['name'])}» сохранена.", parse_mode="HTML")
