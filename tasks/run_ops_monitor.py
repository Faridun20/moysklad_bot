"""
CLI: операционный монитор (IMPLEMENTATION.md Фаза 6). Запускается из Railway Cron.

Один прогон собирает «висящие» сущности и шлёт КАЖДОМУ получателю ОДНУ сводку
(дайджест), а не по сообщению на каждую запись — чтобы не спамить чат:

  • boss/admin   — всё: зависшие заявки, неподтверждённые сдачи/возвраты,
                   просроченные несданные наличные, истекающие партии;
  • bookkeeper   — неподтверждённые сдачи;
  • warehouse    — неподтверждённые возвраты + истекающие партии.

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
    run_migrations,
)
from services.moysklad import close_session
from services.notifier import close_tg_session, tg_send_message
from services.ops_summary import gather_ops_summary
from utils.helpers import esc as _esc

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("ops_monitor")

DIV = "─" * 16


def _fmt_amount(n: float) -> str:
    return f"{int(round(n)):,}".replace(",", " ")


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
    total = sum(float(d.get("amount", 0) or 0) for d in deposits)
    lines = [f"💵 <b>Сдачи на подтверждении: {len(deposits)}</b> (на {_fmt_amount(total)} USD)"]
    for d in deposits[:15]:
        lines.append(f"  • сдача #{d['id']} — {_fmt_amount(float(d.get('amount', 0) or 0))} USD")
    if len(deposits) > 15:
        lines.append(f"  …и ещё {len(deposits) - 15}")
    return "\n".join(lines)


def build_pending_returns_block(returns: list[dict]) -> str | None:
    if not returns:
        return None
    lines = [f"↩️ <b>Возвраты на подтверждении: {len(returns)}</b>"]
    for r in returns[:15]:
        amt = _fmt_amount(float(r.get("total_amount", 0) or 0))
        lines.append(f"  • возврат #{r['id']} · заказ #{r.get('order_id', '?')} — {amt} USD")
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


def build_expiring_batches_block(batches: list[dict], days: int) -> str | None:
    if not batches:
        return None
    lines = [f"⌛ <b>Истекают партии (≤{days}д): {len(batches)}</b>"]
    for b in batches[:15]:
        code = _esc(b.get("batch_code") or b.get("product_id") or "—")
        exp = _esc(b.get("expiry_date") or "—")
        qty = _fmt_amount(float(b.get("qty_remaining", 0) or 0))
        lines.append(f"  • {code} · до {exp} · остаток {qty}")
    if len(batches) > 15:
        lines.append(f"  …и ещё {len(batches) - 15}")
    return "\n".join(lines)


def build_low_stock_block(rows: list[dict], threshold: float) -> str | None:
    """Алерт о низком доступном остатке. rows из snapshot.get_low_stock."""
    if not rows:
        return None
    lines = [f"📉 <b>Низкий остаток (≤{_fmt_amount(threshold)}): {len(rows)}</b>"]
    for r in rows[:15]:
        name = _esc(r.get("name") or "—")
        avail = float(r.get("stock", 0) or 0) - float(r.get("reserve", 0) or 0)
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

    Матч по нормализованному имени (lower/strip) — у нас нет product_id
    в shipment positions, только assortment.name. Возвращает позиции
    в наличии (stock>0), которых нет в sold_names.
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

    Тяжёлая по МС-вызовам (shipments + positions) — вызывается только из
    cron (1×/день). Имена проданных собираем из позиций отгрузок за период.
    """
    from datetime import timedelta

    from services.moysklad import get_shipments, get_shipment_positions
    from services.snapshot import get_stock
    from utils.helpers import extract_id_from_href

    since = datetime.now() - timedelta(days=days)
    sold_names: set[str] = set()
    try:
        shipments = await get_shipments(since)
    except Exception:
        logger.exception("collect_dead_stock: get_shipments failed")
        return []
    for s in shipments:
        demand_id = extract_id_from_href(s.get("meta", {}).get("href", ""))
        if not demand_id:
            continue
        try:
            positions = await get_shipment_positions(demand_id)
        except Exception:
            continue
        for p in positions:
            name = (p.get("assortment") or {}).get("name") or p.get("name")
            if name:
                sold_names.add(name)
    in_stock = get_stock(only_positive=True)
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


def build_ms_sync_block(anomalies: dict[str, list[dict]]) -> str | None:
    """Этап 4: рассинхрон с МойСклад, требующий ручной разборки.
    anomalies из services.database.get_ms_sync_anomalies (drift + deleted)."""
    drift = anomalies.get("drift") or []
    deleted = anomalies.get("deleted") or []
    demand_failed = anomalies.get("demand_failed") or []
    if not drift and not deleted and not demand_failed:
        return None
    lines = ["🔄 <b>Рассинхрон с МойСклад</b>"]
    if demand_failed:
        lines.append(f"  📦 Отгрузка не создана (нужна доделка): {len(demand_failed)}")
        for o in demand_failed[:10]:
            agent = _esc(o.get("agent_name") or "—")
            lines.append(f"    • #{o['id']} · {agent}")
        if len(demand_failed) > 10:
            lines.append(f"    …и ещё {len(demand_failed) - 10}")
    if drift:
        lines.append(f"  ✏️ Изменены в МС (сумма ≠): {len(drift)}")
        for o in drift[:10]:
            agent = _esc(o.get("agent_name") or "—")
            lines.append(f"    • #{o['id']} · {agent}")
        if len(drift) > 10:
            lines.append(f"    …и ещё {len(drift) - 10}")
    if deleted:
        lines.append(f"  🗑 Удалены в МС (фантом {len(deleted)}):")
        for o in deleted[:10]:
            agent = _esc(o.get("agent_name") or "—")
            lines.append(f"    • #{o['id']} · {agent} · {_esc(o.get('status') or '')}")
        if len(deleted) > 10:
            lines.append(f"    …и ещё {len(deleted) - 10}")
    return "\n".join(lines)


def assemble_digest(title: str, blocks: list[str | None]) -> str | None:
    """Склеить непустые блоки в одну сводку. None — если всё пусто."""
    present = [b for b in blocks if b]
    if not present:
        return None
    return f"{DIV}\n{title}\n\n" + "\n\n".join(present)


def build_digest_keyboard(
    stale: list[dict] | None = None,
    deposits: list[dict] | None = None,
    returns: list[dict] | None = None,
    max_orders: int = 5,
) -> dict | None:
    """Инлайн-клавиатура к дайджесту: deep-link кнопки к УЖЕ существующим
    обработчикам бота — `ord_view:{id}` (просмотр заказа), `dep_pending`
    (список сдач), `ret_pending` (список возвратов). Возвращает dict в формате
    Telegram Bot API (reply_markup для tg_send_message) или None, если действий нет.

    Кнопки-заказы — по первым `max_orders` зависшим (по 3 в ряд); сдачи/возвраты —
    один вход в соответствующий список. Сами обработчики живут в bot-процессе
    (cron только отправляет сообщение, callback'и обрабатывает бот).
    """
    rows: list[list[dict]] = []
    order_btns = [
        {"text": f"⏳ #{o['id']}", "callback_data": f"ord_view:{o['id']}"}
        for o in (stale or [])[:max_orders]
    ]
    for i in range(0, len(order_btns), 3):
        rows.append(order_btns[i : i + 3])
    nav: list[dict] = []
    if deposits:
        nav.append({"text": f"💵 Сдачи ({len(deposits)})", "callback_data": "dep_pending"})
    if returns:
        nav.append({"text": f"↩️ Возвраты ({len(returns)})", "callback_data": "ret_pending"})
    if nav:
        rows.append(nav)
    return {"inline_keyboard": rows} if rows else None


# ─── Дневной пинг (короткое уведомление → смотри в WebApp) ────────────────────
#
# Раньше ops_monitor рассылал большие дайджесты в Telegram. Теперь сводку
# смотрят в WebApp (`/api/ops-summary` + блок «Требует внимания» на главной),
# а бот шлёт лишь ОДИН короткий пинг в день: «есть N событий — откройте WebApp».
# Событийные уведомления с кнопками (заявки/платежи/сдачи/возвраты) остаются
# в боте без изменений — это срочные действия, а не сводка.

# Какие секции gather_ops_summary показываем в пинге каждой роли.
_PING_ROLE_SECTIONS: dict[str, list[str]] = {
    "admin": [
        "stale_orders", "overdue_undeposited", "deposits", "returns",
        "expiring_batches", "low_stock", "stale_crons", "ms_anomalies",
    ],
    "boss": [
        "stale_orders", "overdue_undeposited", "deposits", "returns",
        "expiring_batches", "low_stock", "stale_crons", "ms_anomalies",
    ],
    "bookkeeper": ["deposits"],
    "warehouse_keeper": ["returns", "expiring_batches", "low_stock"],
}

_PING_SECTION_LABELS: dict[str, str] = {
    "stale_orders": "⏳ Зависшие заявки",
    "overdue_undeposited": "🚨 Деньги не сданы",
    "deposits": "💵 Сдачи на подтверждении",
    "returns": "↩️ Возвраты на подтверждении",
    "expiring_batches": "⌛ Истекают партии",
    "low_stock": "📉 Низкий остаток",
    "stale_crons": "🛑 Cron не отчитались",
    "ms_anomalies": "🔄 Рассинхрон с МС",
}

_PING_HEADERS: dict[str, tuple[str, str]] = {
    "admin": ("📲 <b>Операционная сводка</b>", "Детали, отчёты и аналитика — в WebApp."),
    "boss": ("📲 <b>Операционная сводка</b>", "Детали, отчёты и аналитика — в WebApp."),
    "bookkeeper": ("📲 <b>Сводка: финансы</b>", "Подтвердите в WebApp."),
    "warehouse_keeper": ("📲 <b>Сводка: склад</b>", "Детали в WebApp."),
}


def _section_count(summary: dict, key: str) -> int:
    sec = summary.get(key) or {}
    if key == "ms_anomalies":
        return (
            int(sec.get("drift") or 0)
            + int(sec.get("deleted") or 0)
            + int(sec.get("demand_failed") or 0)
        )
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
    Без WEBAPP_URL — без кнопки (текст пинга всё равно зовёт открыть WebApp)."""
    if not webapp_url:
        return None
    return {"inline_keyboard": [[{"text": "📲 Открыть WebApp", "web_app": {"url": webapp_url}}]]}


# ─── Оркестрация ──────────────────────────────────────────────────────────────


async def main() -> int:
    init_db()
    run_migrations()  # M1: на свежей БД догнать колонки (идемпотентно)

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
    finally:
        await close_session()
        await close_tg_session()


if __name__ == "__main__":
    from tasks._cron_runner import run_cron

    sys.exit(run_cron("ops_monitor", main))
