"""
Форматирование сообщений для Telegram — улучшенный визуал
"""

from utils.helpers import (
    esc,
    get_folder_name,
    stock_indicator,
    format_date,
    format_price,
    trend_arrow,
)

from config import PAGE_SIZE  # единый источник в config

# ─── Общие элементы ───────────────────────────────────────────────────────────

DIV = "<code>━━━━━━━━━━━━━━━━━━━━</code>"
DIV2 = "<code>────────────────────</code>"


def section_header(emoji: str, title: str, subtitle: str = "") -> str:
    sub = f"\n<code>{subtitle}</code>" if subtitle else ""
    return f"{DIV}\n{emoji} <b>{title}</b>{sub}"


# ─── Остатки ──────────────────────────────────────────────────────────────────


def format_stock_page(rows: list[dict], page: int, cat_name: str = "") -> str:
    total = len(rows)
    total_pages = (total + PAGE_SIZE - 1) // PAGE_SIZE
    start = page * PAGE_SIZE
    end = min(start + PAGE_SIZE, total)

    cat_str = f" · {cat_name}" if cat_name else ""
    lines = [
        section_header(
            "📦", f"Склад{cat_str}", f"стр {page + 1}/{total_pages} · {total} позиций"
        ),
        "",
    ]

    for r in rows[start:end]:
        name = esc(r.get("name", "—"))
        stock = r.get("stock", 0)
        reserve = r.get("reserve", 0)
        unit = esc(r.get("uom", {}).get("name", "шт"))
        folder = esc(get_folder_name(r.get("folder", {})))
        ind = stock_indicator(stock)

        # Название + папка
        folder_str = f"  <i>{folder}</i>" if folder and not cat_name else ""
        lines.append(f"{ind} <b>{name}</b>{folder_str}")

        # Остаток + резерв в одну строку
        reserve_str = f"   <i>резерв: {reserve} {unit}</i>" if reserve else ""
        lines.append(f"    <code>{stock} {unit}</code>{reserve_str}")
        lines.append("")

    lines.append(DIV2)
    lines.append("<code>🟢 ≥100   🟡 20–99   🔴 &lt;20</code>")
    return "\n".join(lines)


# ─── Отгрузка ─────────────────────────────────────────────────────────────────


def format_shipment(s: dict, positions: list[dict] = None) -> str:
    name = esc(s.get("name", "—"))
    moment = esc(format_date(s.get("moment", "—")))
    agent = esc(s.get("agent", {}).get("name", "—"))
    owner = esc(s.get("owner", {}).get("name", "—"))
    sum_str = format_price(s.get("sum", 0))

    lines = [
        DIV,
        f"🚚 <b>{name}</b>   <code>{moment}</code>",
        "",
        f"<b>👤 Клиент:</b> {agent}",
        f"<b>👨‍💼 Менеджер:</b> {owner}",
        f"<b>💰 Итого: {sum_str} $</b>",
    ]

    if positions:
        lines.append("")
        lines.append(f"<b>📋 Товары ({len(positions)}):</b>")
        lines.append(DIV2)
        for pos in positions[:15]:
            assortment = pos.get("assortment", {})
            pos_name = esc(assortment.get("name", "—"))
            qty = pos.get("quantity", 0)
            uom = esc(pos.get("uom", {}).get("name", "")
                      or assortment.get("uom", {}).get("name", "шт"))
            price_raw = pos.get("price", 0)
            price_str = format_price(price_raw)
            total_pos = format_price(price_raw * qty)
            lines.append(
                f"▸ <b>{pos_name}</b>\n"
                f"  <code>{qty} {uom}</code>  ·  {price_str} $  →  <b>{total_pos} $</b>"
            )
        if len(positions) > 15:
            lines.append(f"\n<i>…ещё {len(positions) - 15} позиций</i>")
    else:
        lines.append(f"\n{DIV2}\n<i>Товары не загружены</i>")

    return "\n".join(lines)


# ─── Аналитика продаж ─────────────────────────────────────────────────────────


def format_sales_report(label: str, stats: dict, prev_stats: dict = None) -> str:
    total = stats["total"]
    count = stats["count"]
    clients = stats["clients"]
    top = stats["top_products"]

    total_str = format_price(total)
    avg_str = format_price(total / count) if count else "0"

    trend = ""
    if prev_stats and prev_stats["total"] > 0:
        trend = "  " + trend_arrow(total, prev_stats["total"])

    lines = [
        section_header("📊", f"Продажи · {label}"),
        "",
        f"💰 <b>Выручка:</b>  <b>{total_str} $</b>{trend}",
        f"🚚 <b>Отгрузок:</b>  {count}",
        f"👥 <b>Клиентов:</b>  {clients}",
    ]
    if count:
        lines.append(f"📋 <b>Средний чек:</b>  {avg_str} $")

    if top:
        lines.append("")
        lines.append(f"<b>🏆 Топ товаров:</b>")
        lines.append(DIV2)
        medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"]
        for i, (name, data) in enumerate(top):
            t_sum = format_price(data["sum"])
            qty = data["qty"]
            medal = medals[i] if i < len(medals) else f"{i+1}."
            lines.append(f"{medal} <b>{esc(name)}</b>")
            lines.append(f"    <code>{qty} шт  ·  {t_sum} $</code>")

    if count == 0:
        lines.append(f"\n{DIV2}\n<i>Нет данных за период</i>")

    return "\n".join(lines)


# ─── Платёж ───────────────────────────────────────────────────────────────────


def format_payment_notify(
    payment_id: int,
    full_name: str,
    username: str,
    amount: float,
    currency: str,
    comment: str,
) -> str:
    from datetime import datetime

    now = datetime.now().strftime("%d.%m.%Y %H:%M")
    return (
        f"{DIV}\n"
        f"💵 <b>Новый платёж #{payment_id}</b>\n"
        f"\n"
        f"<b>👤 Сотрудник:</b> {esc(full_name)} ({esc(username)})\n"
        f"<b>💰 Сумма:</b> {amount:,.0f} {esc(currency)}\n"
        f"<b>📝 Комментарий:</b> {esc(comment)}\n"
        f"<b>🕐 Время:</b> {now}"
    )


def format_payment_confirmed(amount: float, currency: str, comment: str) -> str:
    return (
        f"{DIV}\n"
        f"✅ <b>Платёж принят!</b>\n"
        f"\n"
        f"<b>💰 Сумма:</b> {amount:,.0f} {esc(currency)}\n"
        f"<b>📝 Комментарий:</b> {esc(comment)}"
    )


def format_payment_rejected(amount: float, currency: str, comment: str) -> str:
    return (
        f"{DIV}\n"
        f"❌ <b>Платёж отклонён</b>\n"
        f"\n"
        f"<b>💰 Сумма:</b> {amount:,.0f} {esc(currency)}\n"
        f"<b>📝 Комментарий:</b> {esc(comment)}\n"
        f"\n"
        f"<i>Свяжитесь с руководителем</i>"
    )


# ─── Отчёт по платежам ────────────────────────────────────────────────────────


def format_payments_report(
    summary: list[dict], payments: list[dict], label: str
) -> list[str]:
    """Возвращает список сообщений (Telegram ограничивает 4096 символов)."""
    lines = [
        section_header("📊", f"Платежи · {label}"),
        "",
        "<b>По сотрудникам:</b>",
        DIV2,
    ]

    total_by_currency: dict[str, float] = {}
    for s in summary:
        lines.append(
            f"👤 <b>{s['full_name']}</b>\n"
            f"    <code>{s['total']:,.0f} {s['currency']}  ·  {s['count']} платежей</code>"
        )
        total_by_currency[s["currency"]] = (
            total_by_currency.get(s["currency"], 0) + s["total"]
        )

    lines.append("")
    lines.append("<b>Итого:</b>")
    for cur, total in total_by_currency.items():
        lines.append(f"  💰 <b>{total:,.0f} {cur}</b>")

    # Разбиваем на сообщения
    messages = []
    current = "\n".join(lines)

    for p in payments:
        entry = (
            f"\n{DIV2}\n"
            f"👤 {p['full_name']}\n"
            f"💰 {p['amount']:,.0f} {p['currency']}\n"
            f"📝 {p['comment']}\n"
            f"🕐 {p['created_at'][:16]}"
        )
        if len(current) + len(entry) > 3800:
            messages.append(current)
            current = entry.lstrip("\n")
        else:
            current += entry

    if current:
        messages.append(current)

    return messages


# ─── Аудит лог ────────────────────────────────────────────────────────────────


def format_audit_entry(r: dict) -> str:
    ACTION_EMOJI = {
        "user_added": "🟢",
        "user_removed": "🔴",
        "role_changed": "🔄",
        "payment_sent": "💵",
        "payment_confirmed": "✅",
        "payment_rejected": "❌",
        "login": "👤",
    }
    emoji = ACTION_EMOJI.get(r["action"], "▪️")
    dt = r["created_at"][:16]
    # ВАЖНО: full_name/role/details пишутся юзерами и попадают в БД.
    # Без escape менеджер мог бы протащить HTML в audit, который потом
    # видит админ — потенциальная inject в Telegram-сообщение (например
    # кликабельный <a href="evil"> вид).
    role_str = f" [{esc(r['role'])}]" if r.get("role") else ""
    detail_str = f"\n    <i>{esc(r['details'])}</i>" if r.get("details") else ""
    return (
        f"{emoji} <code>{dt}</code>  <b>{esc(r['full_name'])}</b>"
        f"{role_str}{detail_str}"
    )

# ─── Отчёт по остаткам склада ─────────────────────────────────────────────────


def format_stock_report(data: dict) -> str:
    slow = data["slow"]
    fast = data["fast"]
    critical = data["critical"]

    lines = [DIV, "📦 <b>Отчёт по остаткам склада</b>", ""]

    if slow:
        lines.append("🐢 <b>Залежались (≥30 дней):</b>")
        for r, days in slow[:5]:
            name = r.get("name", "—")
            stock = r.get("stock", 0)
            unit = r.get("uom", {}).get("name", "шт")
            lines.append(f"  • <b>{name}</b>")
            lines.append(f"    <code>{stock} {unit} · {days} дней</code>")
        lines.append("")

    if fast:
        lines.append("🚀 <b>Быстро уходят (&lt;7 дней):</b>")
        for r, days in fast[:5]:
            name = r.get("name", "—")
            stock = r.get("stock", 0)
            unit = r.get("uom", {}).get("name", "шт")
            lines.append(f"  • <b>{name}</b>")
            lines.append(f"    <code>{stock} {unit} · {days} дней</code>")
        lines.append("")

    if critical:
        lines.append("🔴 <b>Критический остаток (&lt;20):</b>")
        for r in critical[:5]:
            name = r.get("name", "—")
            stock = r.get("stock", 0)
            unit = r.get("uom", {}).get("name", "шт")
            lines.append(f"  • <b>{name}</b>: <code>{stock} {unit}</code>")

    if not slow and not fast and not critical:
        lines.append("✅ <i>Всё в норме — проблем не обнаружено</i>")

    return "\n".join(lines)