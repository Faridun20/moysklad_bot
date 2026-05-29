"""
Хэндлеры: аналитика продаж
- boss/admin видит общую аналитику компании
- manager видит только свою личную аналитику
"""

import asyncio
import logging
import re
from datetime import datetime, timedelta

from aiogram import Bot, Router, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder

from services.roles import can_view_analytics, cached_role, is_boss
from services.moysklad import get_sales_stats
from services.database import (
    get_user_orders,
    get_order_items_by_ids,
    get_existing_ms_product_ids,
    get_open_debts,
    get_payments_for_orders,
    get_cashbox_stats,
    _price_cents,
    _amount_cents,
)
from services import money
from utils.formatters import format_sales_report, format_cashbox_report
from utils.keyboards import analytics_keyboard
from utils.helpers import user_safe_error, local_now, extract_id_from_href


class AnalyticsFlow(StatesGroup):
    entering_period = State()  # ввод произвольного диапазона дат


def _abort_period_keyboard():
    kb = InlineKeyboardBuilder()
    kb.button(text="🏠 Меню", callback_data="menu")
    return kb.as_markup()


def _parse_period_input(text: str) -> tuple[datetime, datetime] | None:
    """'01.01.2026 31.03.2026' / '01.01.2026-31.03.2026' → (since 00:00, until 23:59:59).

    Принимаем разделители . / - внутри даты и любой разделитель между двумя
    датами. Возвращаем None, если дат не ровно две, дата невалидна,
    from > to, либо диапазон абсурдно большой (защита от опечатки в годе).
    """
    found = re.findall(r"\d{1,2}[.\-/]\d{1,2}[.\-/]\d{2,4}", text or "")
    if len(found) != 2:
        return None

    def _one(s: str) -> datetime | None:
        parts = re.split(r"[.\-/]", s)
        if len(parts) != 3:
            return None
        try:
            d, m, y = (int(p) for p in parts)
        except ValueError:
            return None
        if y < 100:
            y += 2000
        try:
            return datetime(y, m, d)
        except ValueError:
            return None

    a, b = _one(found[0]), _one(found[1])
    if not a or not b:
        return None
    since = a.replace(hour=0, minute=0, second=0, microsecond=0)
    until = b.replace(hour=23, minute=59, second=59, microsecond=0)
    if since > until or (until - since).days > 366 * 5:
        return None
    return since, until


logger = logging.getLogger(__name__)
router = Router()


def get_period(period: str, now: datetime) -> tuple:
    """Вернуть (since, until, prev_since, prev_until, label) для периода."""
    periods = {
        "week": (
            now - timedelta(weeks=1),
            now,
            now - timedelta(weeks=2),
            now - timedelta(weeks=1),
            "Эта неделя",
        ),
        "month": (
            now - timedelta(days=30),
            now,
            now - timedelta(days=60),
            now - timedelta(days=30),
            "Этот месяц",
        ),
        "3month": (
            now - timedelta(days=90),
            now,
            now - timedelta(days=180),
            now - timedelta(days=90),
            "3 месяца",
        ),
        "6month": (
            now - timedelta(days=182),
            now,
            now - timedelta(days=365),
            now - timedelta(days=182),
            "Полгода",
        ),
        "year": (
            now - timedelta(days=365),
            now,
            now - timedelta(days=730),
            now - timedelta(days=365),
            "Год",
        ),
    }
    return periods.get(
        period,
        (
            now - timedelta(days=30),
            now,
            now - timedelta(days=60),
            now - timedelta(days=30),
            "Месяц",
        ),
    )


async def _run_analytics(
    bot: Bot,
    chat_id: int,
    user_id: int,
    period: str,
    view: str = "sales",
    since: datetime | None = None,
    until: datetime | None = None,
    label: str | None = None,
):
    """Маршрутизатор аналитики.

    period — пресет ("week"/"month"/…) ИЛИ "custom" (тогда since/until заданы
    явно и тренд к прошлому периоду не считаем). view — "sales" или "cash"
    (касса/дебиторка, только для босса).
    """
    # Local time — совпадает с DB now_str(). Раньше был datetime.utcnow(),
    # и сегодняшние одобренные заказы (созданные в локальной TZ) выпадали
    # из «окна» аналитики на величину TZ-offset.
    now = local_now()
    if since is None or until is None:
        since, until, prev_since, prev_until, label = get_period(period, now)
    else:
        # Произвольный период — без сравнения с «предыдущим».
        prev_since = prev_until = None
        if label is None:
            label = f"{since:%d.%m.%Y} – {until:%d.%m.%Y}"

    role = cached_role(user_id)
    if view == "cash" and is_boss(user_id):
        await show_cashbox_analytics(bot, chat_id, since, until, label, period)
    elif role == "manager":
        await show_manager_analytics(
            bot, chat_id, user_id, since, until, prev_since, prev_until, label, period
        )
    else:
        await show_company_analytics(
            bot, chat_id, since, until, prev_since, prev_until, label, period
        )


@router.message(Command("analytics"))
async def cmd_analytics(message: Message, bot: Bot, state: FSMContext):
    if not can_view_analytics(message.from_user.id):
        return await message.answer("⛔ Нет доступа.")
    await state.clear()
    # Сразу показываем «этот месяц»; период переключается чипами под отчётом.
    await _run_analytics(bot, message.chat.id, message.from_user.id, "month")


@router.message(Command("cashbox"))
async def cmd_cashbox(message: Message, bot: Bot, state: FSMContext):
    """Касса/дебиторка — только для босса/админа."""
    if not is_boss(message.from_user.id):
        return await message.answer("⛔ Касса доступна только руководителю.")
    await state.clear()
    await _run_analytics(bot, message.chat.id, message.from_user.id, "month", view="cash")


@router.callback_query(F.data == "analytics")
async def cb_analytics(call: CallbackQuery, bot: Bot, state: FSMContext):
    if not can_view_analytics(call.from_user.id):
        return await call.answer("Нет доступа", show_alert=True)
    await call.answer()
    await state.clear()
    await _run_analytics(bot, call.message.chat.id, call.from_user.id, "month")


@router.callback_query(F.data.startswith("an:"))
async def cb_analytics_period(call: CallbackQuery, bot: Bot, state: FSMContext):
    if not can_view_analytics(call.from_user.id):
        return await call.answer("Нет доступа", show_alert=True)
    await call.answer()
    await state.clear()
    parts = call.data.split(":")
    # an:<period> (legacy) | an:<view>:<period>
    if len(parts) == 2:
        view, period = "sales", parts[1]
    else:
        view, period = parts[1], parts[2]
    await _run_analytics(bot, call.message.chat.id, call.from_user.id, period, view=view)


@router.callback_query(F.data.startswith("anp:"))
async def cb_analytics_custom(call: CallbackQuery, state: FSMContext):
    """Запросить произвольный диапазон дат (FSM)."""
    if not can_view_analytics(call.from_user.id):
        return await call.answer("Нет доступа", show_alert=True)
    await call.answer()
    view = call.data.split(":")[1] if ":" in call.data else "sales"
    await state.set_state(AnalyticsFlow.entering_period)
    await state.update_data(view=view)
    await call.message.answer(
        "📅 <b>Свой период</b>\n\n"
        "Пришлите две даты — начало и конец — одним сообщением:\n"
        "<code>01.01.2026 31.03.2026</code>\n\n"
        "<i>Можно через тире: 01.01.2026-31.03.2026</i>",
        parse_mode="HTML",
        reply_markup=_abort_period_keyboard(),
    )


@router.message(AnalyticsFlow.entering_period)
async def process_custom_period(message: Message, bot: Bot, state: FSMContext):
    if not can_view_analytics(message.from_user.id):
        await state.clear()
        return await message.answer("⛔ Нет доступа.")
    parsed = _parse_period_input(message.text or "")
    if not parsed:
        return await message.answer(
            "❌ Не понял даты. Пример: <code>01.01.2026 31.03.2026</code>\n"
            "Две даты ДД.ММ.ГГГГ через пробел или тире.",
            parse_mode="HTML",
            reply_markup=_abort_period_keyboard(),
        )
    since, until = parsed
    data = await state.get_data()
    view = data.get("view", "sales")
    await state.clear()
    label = f"{since:%d.%m.%Y} – {until:%d.%m.%Y}"
    await _run_analytics(
        bot, message.chat.id, message.from_user.id,
        "custom", view=view, since=since, until=until, label=label,
    )


async def show_company_analytics(
    bot: Bot,
    chat_id: int,
    since: datetime,
    until: datetime,
    prev_since: datetime | None,
    prev_until: datetime | None,
    label: str,
    period: str = "month",
):
    """Общая аналитика компании для boss/admin."""
    await bot.send_message(
        chat_id,
        f"⏳ Считаю аналитику компании за {label}…\n<i>До 30 секунд</i>",
        parse_mode="HTML",
    )
    try:
        # Текущий период — основной (если упадёт, покажем ошибку). Предыдущий
        # период (для тренда) — НЕ критичен: его сбой не должен обнулять весь
        # отчёт. Раньше asyncio.gather рушил оба, и «за год» показывал пусто,
        # когда запрос позапрошлого периода (1-2 года назад) падал/таймаутил.
        # Для произвольного периода (custom) prev_* = None → тренд не считаем.
        current_stats = await get_sales_stats(since, until)
        prev_stats = None
        if prev_since is not None and prev_until is not None:
            try:
                prev_stats = await get_sales_stats(prev_since, prev_until)
            except Exception as e:
                logger.warning("prev-period stats failed (тренд пропущен): %s", e)
                prev_stats = None
        txt = format_sales_report(f"🏢 Компания · {label}", current_stats, prev_stats)
        await bot.send_message(
            chat_id,
            txt,
            parse_mode="HTML",
            reply_markup=analytics_keyboard("sales", period, is_boss=True),
        )
    except RuntimeError as e:
        if "circuit breaker" in str(e).lower() or "временно недоступен" in str(e):
            await bot.send_message(
                chat_id,
                "⚠️ <b>МойСклад временно недоступен.</b>\n"
                "Данные аналитики недоступны, попробуйте через несколько минут.",
                parse_mode="HTML",
                reply_markup=analytics_keyboard("sales", period, is_boss=True),
            )
        else:
            await bot.send_message(chat_id, user_safe_error(e, "company_analytics"))
    except Exception as e:
        await bot.send_message(chat_id, user_safe_error(e, "company_analytics"))


async def show_cashbox_analytics(
    bot: Bot,
    chat_id: int,
    since: datetime,
    until: datetime,
    label: str,
    period: str = "month",
):
    """Касса/дебиторка для босса: сколько получили за период + кто ещё должен.

    Поступления (касса) — из локальной БД по CONFIRMED платежам в окне.
    Дебиторка — снапшот открытых кредит-долгов «на сейчас» (не за период):
    отвечает на «за какие заказы ещё не отдали деньги».
    """
    await bot.send_message(
        chat_id,
        f"⏳ Считаю кассу за {label}…",
        parse_mode="HTML",
    )
    try:
        since_iso = since.strftime("%Y-%m-%d %H:%M:%S")
        until_iso = until.strftime("%Y-%m-%d %H:%M:%S")
        cashbox, receivables = await asyncio.gather(
            asyncio.to_thread(get_cashbox_stats, since_iso, until_iso),
            asyncio.to_thread(_receivables_from_local, None),
        )
        txt = format_cashbox_report(label, cashbox, receivables)
        await bot.send_message(
            chat_id,
            txt,
            parse_mode="HTML",
            reply_markup=analytics_keyboard("cash", period, is_boss=True),
        )
    except Exception as e:
        await bot.send_message(chat_id, user_safe_error(e, "cashbox_analytics"))


def _receivables_from_local(user_id: int | None = None) -> dict:
    """Дебиторка: открытые долги (товар отгружен в кредит, оплата не
    подтверждена). Снапшот «на сейчас», не за период.

    Для каждого открытого кредит-заказа: остаток = сумма заказа −
    подтверждённые платежи. Возвращаем суммарный остаток, число заказов,
    число/сумму просроченных и топ-должников (по убыванию остатка).

    Производительность: позиции и платежи грузим батч-запросами (как в
    _personal_stats_from_local), без N+1. Всё в копейках.
    """
    orders = get_open_debts(user_id)
    if not orders:
        return {
            "total_cents": 0,
            "count": 0,
            "overdue_count": 0,
            "overdue_cents": 0,
            "debtors": [],
        }

    ids = [o["id"] for o in orders]
    items_by_order = get_order_items_by_ids(ids)
    payments_by_order = get_payments_for_orders(ids)
    today = local_now().strftime("%Y-%m-%d")

    total_cents = 0
    overdue_cents = 0
    overdue_count = 0
    debtors: list[dict] = []
    for o in orders:
        items = items_by_order.get(o["id"], [])
        order_total = sum(
            money.mul_qty(_price_cents(it), it.get("quantity", 0) or 0) for it in items
        )
        paid = sum(
            _amount_cents(p)
            for p in payments_by_order.get(o["id"], [])
            if p.get("status") == "confirmed"
        )
        remaining = max(0, order_total - paid)
        if remaining <= 0:
            continue
        due = o.get("due_date")
        is_overdue = bool(due) and str(due)[:10] < today
        total_cents += remaining
        if is_overdue:
            overdue_cents += remaining
            overdue_count += 1
        debtors.append(
            {
                "order_id": o["id"],
                "agent_name": o.get("agent_name") or "—",
                "remaining_cents": remaining,
                "due_date": str(due)[:10] if due else "",
                "overdue": is_overdue,
            }
        )
    debtors.sort(key=lambda d: d["remaining_cents"], reverse=True)
    return {
        "total_cents": total_cents,
        "count": len(debtors),
        "overdue_count": overdue_count,
        "overdue_cents": overdue_cents,
        "debtors": debtors[:8],
    }


def _safe_ts(o: dict) -> str:
    """Timestamp как строка YYYY-MM-DD HH:MM:SS. Защищаемся от
    Postgres-datetime/None/T-разделителя."""
    raw = o.get("updated_at") or o.get("created_at") or ""
    if raw is None:
        return ""
    s = str(raw)
    if len(s) >= 11 and s[10] == "T":
        s = s[:10] + " " + s[11:]
    return s[:19]


def _personal_stats_from_local(user_id: int, since: datetime, until: datetime) -> dict:
    """
    Личные показатели менеджера из локальной БД — суммы и счётчики по
    одобренным/отгруженным заказам в период.

    Источник истины — наша БД (orders + order_items), без зависимости
    от связки МойСклад employee.

    Производительность: order_items грузятся одним батч-запросом для
    всех релевантных заказов (раньше был N+1 — на каждый заказ свой
    SELECT, что на Railway Postgres давало многосекундные задержки).
    """
    orders = get_user_orders(user_id)
    since_iso = since.strftime("%Y-%m-%d %H:%M:%S")
    until_iso = until.strftime("%Y-%m-%d %H:%M:%S")

    relevant = [
        o
        for o in orders
        if o["status"] in ("approved", "shipped") and since_iso <= _safe_ts(o) <= until_iso
    ]
    logger.info(
        "analytics.bot user=%s total_orders=%d approved=%d relevant=%d period=[%s..%s]",
        user_id,
        len(orders),
        sum(1 for o in orders if o["status"] in ("approved", "shipped")),
        len(relevant),
        since_iso,
        until_iso,
    )
    if not relevant:
        return {"total": 0, "count": 0, "clients": 0, "top_products": []}

    items_by_order = get_order_items_by_ids([o["id"] for o in relevant])

    # Считаем в копейках через money.mul_qty(price_cents, qty) — без float-дрейфа
    # и без финального ×100. И total, и sum топ-товаров теперь в копейках:
    # format_sales_report делит оба через format_price (раньше top-sum был в
    # мажорных единицах → топ-товары отображались в 100× меньше — баг).
    total_cents = 0
    count = 0
    clients: set[str] = set()
    products_agg: dict[str, dict] = {}
    all_ms_ids: set[str] = set()

    for o in relevant:
        items = items_by_order.get(o["id"], [])
        order_sum_cents = sum(
            money.mul_qty(_price_cents(it), it.get("quantity", 0) or 0) for it in items
        )
        total_cents += order_sum_cents
        count += 1
        if o.get("agent_name"):
            clients.add(o["agent_name"])
        for it in items:
            name = it.get("product_name", "—")
            mid = extract_id_from_href(it.get("product_href") or "")
            if mid:
                all_ms_ids.add(mid)
            qty = float(it.get("quantity", 0) or 0)
            line_cents = money.mul_qty(_price_cents(it), qty)
            agg = products_agg.setdefault(name, {"sum": 0, "qty": 0.0, "ms_id": mid})
            agg["sum"] += line_cents
            agg["qty"] += qty
            if mid and not agg.get("ms_id"):
                agg["ms_id"] = mid

    # Прячем из топа товары, удалённые в МойСклад (нет в снапшоте ms_products).
    # Фильтруем ТОЛЬКО если снапшот сматчил хоть один товар (значит он
    # заполнен) — иначе при пустом/несинхронизированном снапшоте спрятали бы
    # всё. Позиции без ms_id (legacy/без href) не прячем — определить нельзя.
    existing = get_existing_ms_product_ids(list(all_ms_ids)) if all_ms_ids else set()
    if existing:
        visible = {
            name: d
            for name, d in products_agg.items()
            if not d.get("ms_id") or d["ms_id"] in existing
        }
    else:
        visible = products_agg
    top_products = sorted(visible.items(), key=lambda kv: kv[1]["sum"], reverse=True)[:5]

    # Формат совместим с format_sales_report — total и sum в минорных единицах.
    return {
        "total": total_cents,
        "count": count,
        "clients": len(clients),
        "top_products": top_products,
    }


async def show_manager_analytics(
    bot: Bot,
    chat_id: int,
    user_id: int,
    since: datetime,
    until: datetime,
    prev_since: datetime | None,
    prev_until: datetime | None,
    label: str,
    period: str = "month",
):
    """Персональная аналитика менеджера — из локальной БД.

    Раньше требовалось чтобы аккаунт был привязан к employee в МойСклад,
    что часто не работало (создание сотрудников требует платного тарифа).
    Теперь считаем по одобренным заявкам бота — данные всегда доступны.

    Для произвольного периода (prev_* = None) тренд к прошлому периоду не
    считаем — сравнивать не с чем.
    """
    await bot.send_message(
        chat_id,
        f"⏳ Считаю вашу аналитику за {label}…",
        parse_mode="HTML",
    )
    try:
        current_stats = await asyncio.to_thread(
            _personal_stats_from_local, user_id, since, until
        )
        prev_stats = None
        if prev_since is not None and prev_until is not None:
            prev_stats = await asyncio.to_thread(
                _personal_stats_from_local, user_id, prev_since, prev_until
            )

        if current_stats["count"] == 0 and (prev_stats is None or prev_stats["count"] == 0):
            return await bot.send_message(
                chat_id,
                f"📊 За «{label}» нет ваших одобренных отгрузок.\n"
                f"Аналитика появится после первой одобренной заявки.",
                reply_markup=analytics_keyboard("sales", period, is_boss=False),
            )

        txt = format_sales_report(f"👤 Моя аналитика · {label}", current_stats, prev_stats)
        await bot.send_message(
            chat_id,
            txt,
            parse_mode="HTML",
            reply_markup=analytics_keyboard("sales", period, is_boss=False),
        )
    except Exception as e:
        await bot.send_message(chat_id, user_safe_error(e, f"manager_analytics:{user_id}"))


async def show_manager_summary(bot: Bot, chat_id: int, user_id: int):
    """Краткая сводка менеджера за текущий месяц — из локальной БД."""
    try:
        # Local time, чтобы совпадало с DB now_str(). См. cb_analytics_period.
        now = local_now()
        since = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        stats = await asyncio.to_thread(_personal_stats_from_local, user_id, since, now)
        if stats["count"] == 0:
            return

        from utils.helpers import format_price
        from utils.formatters import DIV

        total_str = format_price(stats["total"])
        avg_str = format_price(stats["total"] / stats["count"]) if stats["count"] else "0"

        text = (
            f"{DIV}\n"
            f"📊 <b>Ваши показатели · этот месяц</b>\n"
            f"\n"
            f"💰 Выручка: <b>{total_str}</b>\n"
            f"🚚 Отгрузок: <b>{stats['count']}</b>\n"
            f"👥 Клиентов: <b>{stats['clients']}</b>\n"
            f"📋 Средний чек: <b>{avg_str}</b>"
        )
        await bot.send_message(chat_id, text, parse_mode="HTML")
    except Exception as e:
        logger.warning("Не удалось показать сводку менеджера: %s", e)
        await bot.send_message(chat_id, user_safe_error(e, "manager_summary"))
