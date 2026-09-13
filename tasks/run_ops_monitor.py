"""
CLI: операционный монитор (IMPLEMENTATION.md Фаза 6). Запускается из Railway Cron.

Один прогон собирает «висящие» сущности и шлёт КАЖДОМУ получателю ОДНУ сводку
(дайджест), а не по сообщению на каждую запись — чтобы не спамить чат:

  • boss/admin   — всё: зависшие заявки, неподтверждённые сдачи/возвраты,
                   просроченные несданные наличные, низкий остаток;
  • bookkeeper   — неподтверждённые сдачи;
  • warehouse    — неподтверждённые возвраты + низкий остаток.

Пороговые значения берём из app_settings (stale_pending_hours,
cash_deposit_escalation_days, и т.д.) с дефолтами.

Использование:
    python -m tasks.run_ops_monitor

Расписание Railway Cron (пример): 0 7 * * *  (7:00 UTC = 12:00 Ташкент).
"""

import logging
import sys

from datetime import datetime

from config import WEBAPP_URL
from services.database import (
    claim_ops_monitor_run,
    get_all_users,
    init_db,
)
from services.notifier import close_tg_session, tg_send_message
from services.ops_summary import gather_ops_summary
from utils.helpers import esc as _esc

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("ops_monitor")

DIV = "─" * 16


def _fmt_amount(n: float) -> str:
    return f"{int(round(n)):,}".replace(",", " ")


def _base_cur() -> str:
    """Валюта кассы — из BASE_CURRENCY, а не литерал «USD» (T2.13, §3.7)."""
    from config import BASE_CURRENCY

    return (BASE_CURRENCY or "USD").upper()


def _fmt_cents(cents: int) -> str:
    """Копейки → строка (деньги хранятся только в копейках, T1.3)."""
    from services import money

    return money.format_cents(int(cents or 0), decimals=0, sep=" ")


# ─── Чистые билдеры блоков (тестируются без сети/БД) ──────────────────────────


def build_stale_orders_block(orders: list[dict], hours: int) -> str | None:
    if not orders:
        return None
    lines = [f"⏳ <b>Зависшие заявки (>{hours}ч): {len(orders)}</b>"]
    for o in orders[:15]:
        agent = _esc(o.get("agent_name") or "—")
        owner = _esc(o.get("full_name") or "—")
        lines.append(f"  • #{o['id']} · {agent} · {owner}")
    if len(orders) > 15:
        lines.append(f"  …и ещё {len(orders) - 15}")
    return "\n".join(lines)


def build_pending_deposits_block(deposits: list[dict]) -> str | None:
    if not deposits:
        return None
    total_cents = sum(int(d.get("amount_cents") or 0) for d in deposits)
    lines = [f"💵 <b>Сдачи на подтверждении: {len(deposits)}</b> (на {_fmt_cents(total_cents)} {_base_cur()})"]
    for d in deposits[:15]:
        lines.append(f"  • сдача #{d['id']} — {_fmt_cents(d.get('amount_cents'))} {_base_cur()}")
    if len(deposits) > 15:
        lines.append(f"  …и ещё {len(deposits) - 15}")
    return "\n".join(lines)


def build_pending_returns_block(returns: list[dict]) -> str | None:
    if not returns:
        return None
    lines = [f"↩️ <b>Возвраты на подтверждении: {len(returns)}</b>"]
    for r in returns[:15]:
        amt = _fmt_cents(r.get("total_amount_cents"))
        lines.append(f"  • возврат #{r['id']} · заказ #{r.get('order_id', '?')} — {amt} {_base_cur()}")
    if len(returns) > 15:
        lines.append(f"  …и ещё {len(returns) - 15}")
    return "\n".join(lines)


def build_overdue_undeposited_block(orders: list[dict], days: int) -> str | None:
    if not orders:
        return None
    lines = [f"🚨 <b>Отгружено, деньги не сданы (>{days}д): {len(orders)}</b>"]
    for o in orders[:15]:
        agent = _esc(o.get("agent_name") or "—")
        owner = _esc(o.get("full_name") or "—")
        lines.append(f"  • #{o['id']} · {agent} · {owner}")
    if len(orders) > 15:
        lines.append(f"  …и ещё {len(orders) - 15}")
    return "\n".join(lines)


def build_low_stock_block(rows: list[dict], threshold: float) -> str | None:
    """Алерт о низком доступном остатке. rows из warehouse.get_low_stock."""
    if not rows:
        return None
    lines = [f"📉 <b>Низкий остаток (≤{_fmt_amount(threshold)}): {len(rows)}</b>"]
    for r in rows[:15]:
        name = _esc(r.get("name") or "—")
        avail = float(r.get("available", 0) or 0)
        unit = _esc(r.get("unit") or "шт")
        lines.append(f"  • {name} · {_fmt_amount(avail)} {unit}")
    if len(rows) > 15:
        lines.append(f"  …и ещё {len(rows) - 15}")
    return "\n".join(lines)


def build_dead_stock_block(rows: list[dict], days: int) -> str | None:
    """Алерт о «мёртвом» складе: в наличии, но не продавалось N дней.
    rows — list[dict] с name/stock/unit (см. collect_dead_stock)."""
    if not rows:
        return None
    lines = [f"🧊 <b>Не продаётся &gt;{days}д: {len(rows)}</b>"]
    for r in rows[:15]:
        name = _esc(r.get("name") or "—")
        qty = _fmt_amount(float(r.get("stock", 0) or 0))
        unit = _esc(r.get("unit") or "шт")
        lines.append(f"  • {name} · остаток {qty} {unit}")
    if len(rows) > 15:
        lines.append(f"  …и ещё {len(rows) - 15}")
    return "\n".join(lines)


def diff_dead_stock(in_stock: list[dict], sold_names: set[str]) -> list[dict]:
    """Чистая функция (тестируемая): из остатков убрать то, что продавалось.

    Матч по нормализованному имени (lower/strip). Осталось от эпохи, когда
    проданное приезжало позициями отгрузок МойСклад, где был только
    `assortment.name`; сейчас имена приходят из наших же накладных и совпадают
    буквально, но сравнение по нормализованному имени безобиднее строгого.
    Возвращает позиции в наличии (stock>0), которых нет в sold_names.
    """
    sold_norm = {(s or "").strip().lower() for s in sold_names}
    dead = []
    for r in in_stock:
        if float(r.get("stock", 0) or 0) <= 0:
            continue
        name_norm = (r.get("name") or "").strip().lower()
        if name_norm and name_norm not in sold_norm:
            dead.append(r)
    return dead


async def collect_dead_stock(days: int) -> list[dict]:
    """Собрать «мёртвый» склад: остатки минус то, что продавалось за `days`.

    Раньше это была самая дорогая операция дня: список отгрузок МойСклад плюс
    отдельный запрос позиций на каждую. Теперь всё считается по нашим же
    накладным, но функция остаётся cron-only — ей всё равно незачем бежать по
    запросу из webapp.
    """
    from datetime import timedelta

    from services.warehouse import get_catalog, sales_stats

    since = datetime.now() - timedelta(days=days)
    stats = await sales_stats(since)
    sold_names = {name for name, _d in stats.get("top_products") or []}
    rows = await get_catalog(only_positive=True)
    # `diff_dead_stock` читает поле `stock` — имя осталось с прежнего формата.
    in_stock = [{**r, "stock": r["quantity"]} for r in rows]
    return diff_dead_stock(in_stock, sold_names)


def build_cron_health_block(stale_crons: list[dict]) -> str | None:
    """Алерт о cron'ах, которые не отчитались success'ом дольше порога.

    Источник — services.database.get_stale_crons (порядок по task_name).
    Включаем в дайджест только если есть что-то — boss'у в текущий
    digest идут реальные проблемы (как зависшие заявки), не «всё ОК».
    """
    if not stale_crons:
        return None
    lines = [f"🛑 <b>Cron: не отчитались ({len(stale_crons)})</b>"]
    for c in stale_crons:
        task = _esc(c["task_name"])
        thr = c.get("threshold_hours", 0)
        if c.get("last_success_at") is None:
            lines.append(f"  • <code>{task}</code> · ни разу не запускался (порог {thr}ч)")
            continue
        ago = c.get("hours_ago") or 0
        status = c.get("last_status") or "?"
        err = _esc(str(c.get("last_error") or "")[:120])
        suffix = f" · err: {err}" if err else ""
        lines.append(
            f"  • <code>{task}</code> · {ago}ч назад · status={status} (порог {thr}ч){suffix}"
        )
    return "\n".join(lines)


def build_shipment_failed_block(rows: list[dict]) -> str | None:
    """Заказы, которые одобрили, а со склада не списали.

    Раньше этот блок назывался «рассинхрон с МойСклад» и собирал документы,
    которых МС не принял или которые в нём удалили. Расхождение осталось тем
    же по смыслу: заказ выглядит отгруженным, а остаток с ним не сошёлся.
    """
    if not rows:
        return None
    lines = [f"📦 <b>Остаток не списан (нужна доделка): {len(rows)}</b>"]
    for r in rows[:10]:
        agent = _esc(r.get("agent_name") or "—")
        err = _esc(str(r.get("error") or "")[:120])
        suffix = f" · {err}" if err else ""
        lines.append(f"  • #{r['order_id']} · {agent}{suffix}")
    if len(rows) > 10:
        lines.append(f"  …и ещё {len(rows) - 10}")
    return "\n".join(lines)


# ─── Дневной пинг (короткое уведомление → смотри в WebApp) ────────────────────
#
# Раньше ops_monitor рассылал большие дайджесты в Telegram. Теперь сводку
# смотрят в WebApp (`/api/ops-summary` + блок «Требует внимания» на главной),
# а бот шлёт лишь ОДИН короткий пинг в день: «есть N событий — откройте WebApp».
# Событийные уведомления с кнопками (заявки/платежи/сдачи/возвраты) остаются
# в боте без изменений — это срочные действия, а не сводка.

# Какие секции gather_ops_summary показываем в пинге каждой роли.
#
# `stale_crons` — только админу. Руководителю «cron не отчитались» ничего не
# говорит и ничего от него не требует: это здоровье инфраструктуры, а не
# состояние дел. Строка в сводке, по которой нельзя принять решение, приучает
# не читать сводку целиком.
_PING_ROLE_SECTIONS: dict[str, list[str]] = {
    "admin": [
        "stale_orders", "overdue_undeposited", "deposits", "returns",
        "low_stock", "stale_crons", "shipment_failed",
    ],
    "boss": [
        "stale_orders", "overdue_undeposited", "deposits", "returns",
        "low_stock", "shipment_failed",
    ],
    "bookkeeper": ["deposits"],
    "warehouse_keeper": ["returns", "low_stock"],
}

_PING_SECTION_LABELS: dict[str, str] = {
    "stale_orders": "⏳ Зависшие заявки",
    "overdue_undeposited": "🚨 Деньги не сданы",
    "deposits": "💵 Сдачи на подтверждении",
    "returns": "↩️ Возвраты на подтверждении",
    "low_stock": "📉 Низкий остаток",
    "stale_crons": "🛑 Cron не отчитались",
    "shipment_failed": "📦 Остаток не списан",
}

_PING_HEADERS: dict[str, tuple[str, str]] = {
    "admin": ("📲 <b>Операционная сводка</b>", "Детали, отчёты и аналитика — в WebApp."),
    "boss": ("📲 <b>Операционная сводка</b>", "Детали, отчёты и аналитика — в WebApp."),
    "bookkeeper": ("📲 <b>Сводка: финансы</b>", "Подтвердите в WebApp."),
    "warehouse_keeper": ("📲 <b>Сводка: склад</b>", "Детали в WebApp."),
}


def _section_count(summary: dict, key: str) -> int:
    sec = summary.get(key) or {}
    return int(sec.get("count") or 0)


def build_daily_ping(role: str, summary: dict) -> str | None:
    """Короткий текст пинга для роли. None — если для роли нечего показать
    (нет релевантных секций ИЛИ всё по нулям → не спамим)."""
    keys = _PING_ROLE_SECTIONS.get(role)
    if not keys:
        return None
    lines: list[str] = []
    total = 0
    for k in keys:
        n = _section_count(summary, k)
        if n:
            total += n
            lines.append(f"  {_PING_SECTION_LABELS[k]}: {n}")
    if total == 0:
        return None
    header, footer = _PING_HEADERS[role]
    return f"{header}\n\nТребует внимания: <b>{total}</b>\n" + "\n".join(lines) + f"\n\n{footer}"


def build_ping_keyboard(webapp_url: str | None) -> dict | None:
    """Inline-кнопка, открывающая WebApp (`web_app` работает в приватных чатах).
    Только HTTPS: Telegram отклоняет sendMessage с web_app-кнопкой на не-HTTPS URL
    (тогда упал бы ВЕСЬ пинг). Без валидного URL — без кнопки (текст пинга всё
    равно зовёт открыть WebApp)."""
    if not webapp_url or not webapp_url.startswith("https://"):
        return None
    return {"inline_keyboard": [[{"text": "📲 Открыть WebApp", "web_app": {"url": webapp_url}}]]}


# ─── Оркестрация ──────────────────────────────────────────────────────────────


async def main() -> int:
    init_db()

    # Round 6 RACE-4: idempotency-guard. Railway Cron при сетевом hiccup'е
    # может ретраить запуск, или ручной запуск пересечётся с плановым —
    # без этого пинг разойдётся всем 2 раза в день.
    today = datetime.now().strftime("%Y-%m-%d")
    if not claim_ops_monitor_run(today):
        logger.info("ops_monitor: уже запускался сегодня (%s) — пропускаю", today)
        return 0

    summary = await gather_ops_summary()
    kb = build_ping_keyboard(WEBAPP_URL)

    users = get_all_users()
    sent = 0
    try:
        for u in users:
            text = build_daily_ping(u["role"], summary)
            if text:
                await tg_send_message(u["user_id"], text, reply_markup=kb)
                sent += 1
        logger.info("ops_monitor ping: total=%s → %d сообщений", summary.get("total"), sent)
        return 0
    except Exception:
        logger.exception("ops_monitor: ошибка")
        return 1
        await close_tg_session()


if __name__ == "__main__":
    from tasks._cron_runner import run_cron

    sys.exit(run_cron("ops_monitor", main))
