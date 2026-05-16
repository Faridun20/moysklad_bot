"""
Форматирование сообщений для Telegram
"""

from utils.helpers import (
    get_folder_name, stock_indicator,
    format_date, format_price, trend_arrow
)

PAGE_SIZE = 10


def format_stock_page(rows: list[dict], page: int, cat_name: str = "") -> str:
    total = len(rows)
    total_pages = (total + PAGE_SIZE - 1) // PAGE_SIZE
    start = page * PAGE_SIZE
    end = min(start + PAGE_SIZE, total)

    lines = []
    cat_str = f" · {cat_name}" if cat_name else ""
    lines.append("<code>━━━━━━━━━━━━━━━━━━━━</code>")
    lines.append(f"📦 <b>Склад{cat_str}</b>")
    lines.append(f"<code>стр {page + 1}/{total_pages} · {total} позиций</code>")
    lines.append("")

    for r in rows[start:end]:
        name = r.get("name", "—")
        stock = r.get("stock", 0)
        reserve = r.get("reserve", 0)
        unit = r.get("uom", {}).get("name", "шт")
        folder_name = get_folder_name(r.get("folder", {}))
        indicator = stock_indicator(stock)

        if folder_name and not cat_name:
            lines.append(f"{indicator} <b>{name}</b>  <i>{folder_name}</i>")
        else:
            lines.append(f"{indicator} <b>{name}</b>")

        reserve_str = f"  резерв: {reserve} {unit}" if reserve else ""
        lines.append(f"    <code>{stock} {unit}</code>{reserve_str}")
        lines.append("")

    lines.append("<code>🟢 ≥100  🟡 20–99  🔴 &lt;20</code>")
    return "\n".join(lines)


def format_shipment(s: dict, positions: list[dict] = None) -> str:
    name = s.get("name", "—")
    moment = format_date(s.get("moment", "—"))
    agent = s.get("agent", {}).get("name", "—")
    owner = s.get("owner", {}).get("name", "—")
    sum_str = format_price(s.get("sum", 0))

    lines = []
    lines.append("<code>━━━━━━━━━━━━━━━━━━━━</code>")
    lines.append(f"🚚 <b>{name}</b>")
    lines.append(f"<code>{moment}</code>")
    lines.append("")
    lines.append(f"👤 Клиент: {agent}")
    lines.append(f"👨‍💼 Ответственный: {owner}")
    lines.append(f"💰 <b>{sum_str} $</b>")

    if positions:
        lines.append("")
        lines.append("<b>Товары:</b>")
        for pos in positions[:15]:
            assortment = pos.get("assortment", {})
            pos_name = assortment.get("name", "—")
            qty = pos.get("quantity", 0)
            uom = (
                pos.get("uom", {}).get("name", "")
                or assortment.get("uom", {}).get("name", "шт")
            )
            price_raw = pos.get("price", 0)
            price_str = format_price(price_raw)
            total_pos = format_price(price_raw * qty)
            lines.append(
                f"  • <b>{pos_name}</b>\n"
                f"    <code>{qty} {uom}</code>  ·  {price_str} $/ед.  ·  итого: {total_pos} $"
            )
        if len(positions) > 15:
            lines.append(f"  <i>...и ещё {len(positions) - 15} поз.</i>")
    else:
        lines.append("\n<i>Нет данных о товарах</i>")

    return "\n".join(lines)


def format_sales_report(label: str, stats: dict, prev_stats: dict = None) -> str:
    total = stats["total"]
    count = stats["count"]
    clients = stats["clients"]
    top = stats["top_products"]

    total_str = format_price(total)
    avg_str = format_price(total / count) if count else "0"

    lines = []
    lines.append("<code>━━━━━━━━━━━━━━━━━━━━</code>")
    lines.append(f"📊 <b>Продажи · {label}</b>")
    lines.append("")

    trend = ""
    if prev_stats and prev_stats["total"] > 0:
        trend = "  " + trend_arrow(total, prev_stats["total"])

    lines.append(f"💰 Выручка: <b>{total_str} $</b>{trend}")
    lines.append(f"🚚 Отгрузок: <b>{count}</b>")
    lines.append(f"👥 Клиентов: <b>{clients}</b>")
    if count:
        lines.append(f"📋 Средний чек: <b>{avg_str} $</b>")

    if top:
        lines.append("")
        lines.append("<b>🏆 Топ-5 товаров:</b>")
        for i, (name, data) in enumerate(top, 1):
            t_sum = format_price(data["sum"])
            qty = data["qty"]
            lines.append(f"  {i}. <b>{name}</b>")
            lines.append(f"      <code>{qty} шт · {t_sum} $</code>")

    if count == 0:
        lines.append("\n<i>Нет данных за период</i>")

    return "\n".join(lines)
