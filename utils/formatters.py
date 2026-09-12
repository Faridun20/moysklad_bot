"""
Форматирование сообщений для Telegram — улучшенный визуал
"""

from services.money import format_cents, mul_qty
from utils.helpers import (
    esc,
    format_date,
    local_now,
)

# ─── Общие элементы ───────────────────────────────────────────────────────────

DIV = "<code>━━━━━━━━━━━━━━━━━━━━</code>"
DIV2 = "<code>────────────────────</code>"


# ─── Отгрузка ─────────────────────────────────────────────────────────────────


def format_shipment(invoice: dict) -> str:
    """Расходная накладная одним сообщением: шапка + позиции.

    Раньше на вход шли два документа МойСклад (demand + отдельно выкачанные
    позиции). Теперь это одна наша накладная, и позиции лежат в ней же —
    `warehouse.get_invoice` отдаёт их вместе с шапкой.
    """
    number = esc(str(invoice.get("invoice_number") or "—"))
    moment = esc(format_date(str(invoice.get("invoice_date") or "—")))
    agent = esc(str(invoice.get("counterparty_name") or "—"))
    currency = esc(str(invoice.get("currency") or ""))
    sum_str = format_cents(int(invoice.get("total_amount_cents") or 0))
    positions = invoice.get("items") or []

    lines = [
        DIV,
        f"🚚 <b>{number}</b>   <code>{moment}</code>",
        "",
        f"<b>👤 Клиент:</b> {agent}",
        f"<b>💰 Итого: {sum_str} {currency}</b>",
    ]
    if invoice.get("status") == "cancelled":
        lines.append("<b>🚫 Накладная отменена</b>")

    if positions:
        lines.append("")
        lines.append(f"<b>📋 Товары ({len(positions)}):</b>")
        lines.append(DIV2)
        for pos in positions[:15]:
            pos_name = esc(str(pos.get("product_name") or "—"))
            qty = pos.get("quantity", 0)
            uom = esc(str(pos.get("unit") or "шт"))
            price_cents = int(pos.get("price_cents") or 0)
            price_str = format_cents(price_cents)
            total_pos = format_cents(mul_qty(price_cents, float(qty or 0)))
            lines.append(
                f"▸ <b>{pos_name}</b>\n"
                f"  <code>{qty:g} {uom}</code>  ·  {price_str}  →  <b>{total_pos}</b>"
            )
        if len(positions) > 15:
            lines.append(f"\n<i>…ещё {len(positions) - 15} позиций</i>")
    else:
        lines.append(f"\n{DIV2}\n<i>Позиций нет</i>")

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
    now = local_now().strftime("%d.%m.%Y %H:%M")
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
    return f"{emoji} <code>{dt}</code>  <b>{esc(r['full_name'])}</b>{role_str}{detail_str}"
