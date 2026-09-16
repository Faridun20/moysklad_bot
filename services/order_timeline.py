"""
История заказа (C3): вертикальная лента решений по заказу в одном месте —
кто одобрил, когда внесли и подтвердили оплату, кто отгрузил, сдал/подтвердил
наличные, оформил/подтвердил возврат, отменил.

Источник — НЕ `audit_log`. У `audit_log` нет колонки `order_id`: заказ
упоминается только внутри свободного текста `details` («Заказ #123», «Заявка
#45»), и надёжно вытащить его обратно можно только текстовым разбором с
дырами (заявки на отгрузку/кредитные превышения ссылаются только на id
заявки, не заказа). Вместо этого лента строится из тех же таблиц, из которых
`add_audit_log` и получал свои факты: `shipment_requests` (заявка/одобрение/
доработка), `payments` (внесение/подтверждение/отклонение), `cash_deposits`
через `cash_deposit_orders` (сдача наличных), `returns` (возврат) — плюс
денормализованные поля самой `orders` (создание, отправка на рассмотрение,
отгрузка, отмена, «полностью оплачен»). Это ПОЛНЕЕ и надёжнее, чем текстовый
разбор `audit_log`, и покрывает записи до и после появления таблицы аудита.

Известный пробел (см. финальный отчёт агента): точечные события, которые
живут ТОЛЬКО в `audit_log` и не имеют своей строки/колонки —
`credit_override` (заявка одобрена с превышением кредитного лимита),
`order_shipment_failed`/`shipment_positions_skipped` (проблемы списания
склада при отгрузке) — в ленту не попадают. Эти события по-прежнему видны
админу через `/audit` в боте.
"""

from __future__ import annotations

from services import async_db as adb


def _dt(value: str | None) -> str | None:
    """Первые 16 символов TEXT-таймстампа ("YYYY-MM-DD HH:MM") — как везде в
    /api/orders (см. webapp/server.py::api_orders)."""
    return value[:16] if value else None


async def _names_for(user_ids: set[int]) -> dict[int, str]:
    """user_id → full_name одним запросом на всю ленту (не на событие)."""
    ids = {int(u) for u in user_ids if u}
    if not ids:
        return {}
    users = await adb.get_all_users()
    return {
        int(u["user_id"]): (u.get("full_name") or u.get("username") or str(u["user_id"]))
        for u in users
        if int(u["user_id"]) in ids
    }


_SHIPMENT_STATUS_TEXT = {
    "approved": "Заявка на отгрузку одобрена",
    "rejected": "Заявка на отгрузку отклонена",
    "returned": "Заявка возвращена на доработку",
}


async def build_order_timeline(order_id: int) -> list[dict]:
    """Событий заказа — по возрастанию времени: `{ts, actor, action, text}`.

    `ts` — "YYYY-MM-DD HH:MM" (сортировка строкой работает — общий формат);
    `action` — код (для иконки/фильтра на фронте, переводится тем же словарём,
    что и C1, где код совпадает с `audit_log.action`, — см. `utils.audit_labels`);
    `actor` — имя человека, `text` — короткое описание на русском.
    """
    order = await adb.get_order(order_id)
    if not order:
        return []

    requests = await adb.get_shipment_requests_for_order(order_id)
    payments = await adb.get_payments_for_order(order_id)
    deposits = await adb.get_cash_deposits_for_order(order_id)
    returns = await adb.get_returns_for_order(order_id)

    # Имена по user_id одним батчем — у orders/cash_deposits/returns денег нет
    # своего денормализованного full_name (в отличие от shipment_requests и
    # payments, где он уже есть в строке).
    ids_needed = {
        order.get("shipped_by"),
        order.get("cancelled_by"),
        *(d.get("manager_id") for d in deposits),
        *(d.get("confirmed_by") for d in deposits),
        *(r.get("created_by") for r in returns),
        *(r.get("confirmed_by") for r in returns),
    }
    names = await _names_for(ids_needed)

    def name_of(uid) -> str:
        try:
            return names.get(int(uid), str(uid)) if uid else ""
        except (TypeError, ValueError):
            return ""

    events: list[dict] = []

    def add(ts, actor, action, text) -> None:
        if not ts:
            return
        events.append({"ts": _dt(ts), "actor": actor or "", "action": action, "text": text})

    add(order.get("created_at"), order.get("full_name"), "order_created", "Заказ создан")

    def without_approval(req: dict) -> bool:
        # Одобрение отгрузки не обязательно: «Отгрузить» проводит заявку тем же
        # переходом, и в `approved_by` стоит сам автор. Никто ничего не
        # рассматривал — «подана/отправлена/одобрена» в ленте были бы неправдой.
        return (
            req.get("status") == "approved"
            and bool(req.get("approved_by"))
            and req.get("approved_by") == req.get("user_id")
        )

    if not (requests and without_approval(requests[-1])):
        add(
            order.get("submitted_at"), order.get("full_name"), "order_submitted",
            "Отправлен на рассмотрение",
        )

    for req in requests:
        if without_approval(req):
            add(
                req.get("approved_at"), req.get("approved_by_name"), "shipment_auto_approved",
                "Отгрузка оформлена без одобрения руководителя",
            )
            continue
        add(
            req.get("created_at"), req.get("full_name"), "shipment_requested",
            f"Заявка на отгрузку №{req['id']} подана",
        )
        status = req.get("status")
        if status in _SHIPMENT_STATUS_TEXT and req.get("approved_at"):
            text = _SHIPMENT_STATUS_TEXT[status]
            action = {
                "approved": "shipment_approved",
                "rejected": "shipment_rejected",
                "returned": "shipment_returned",
            }[status]
            add(req["approved_at"], req.get("approved_by_name"), action, text)

    add(order.get("shipped_at"), name_of(order.get("shipped_by")), "order_shipped", "Заказ отгружен")

    for p in payments:
        method_bits = f" ({p['comment']})" if p.get("comment") else ""
        add(
            p.get("created_at"), p.get("full_name"), "payment_sent",
            f"Платёж №{p['id']}: {p.get('amount', 0):,.0f} {p.get('currency', '')}{method_bits}",
        )
        if p.get("status") == "confirmed":
            add(
                p.get("confirmed_at") or p.get("created_at"), "", "payment_confirmed",
                f"Платёж №{p['id']} подтверждён",
            )
        elif p.get("status") == "rejected":
            add(
                p.get("confirmed_at") or p.get("created_at"), "", "payment_rejected",
                f"Платёж №{p['id']} отклонён",
            )

    # «Полностью оплачен» — момент закрытия заказа (paid_confirmed_at), c
    # готовым именем (paid_confirmed_by_name уже денормализовано в orders) —
    # отдельно от подтверждения ОДНОГО платежа выше: это факт по ЗАКАЗУ.
    add(
        order.get("paid_confirmed_at"), order.get("paid_confirmed_by_name"), "order_fully_paid",
        "Заказ полностью оплачен",
    )

    for d in deposits:
        add(
            d.get("created_at") or d.get("deposited_at"), name_of(d.get("manager_id")),
            "cash_deposit_created",
            f"Сдача наличных №{d['id']}: {d.get('amount', 0):,.0f}",
        )
        if d.get("status") == "confirmed":
            add(
                d.get("confirmed_at"), name_of(d.get("confirmed_by")), "cash_deposit_confirmed",
                f"Сдача №{d['id']} подтверждена",
            )
        elif d.get("status") == "rejected":
            add(
                d.get("confirmed_at"), name_of(d.get("confirmed_by")), "cash_deposit_rejected",
                f"Сдача №{d['id']} отклонена" + (f": {d['reject_reason']}" if d.get("reject_reason") else ""),
            )

    for r in returns:
        add(
            r.get("created_at"), name_of(r.get("created_by")), "return_created",
            f"Оформлен возврат №{r['id']} ({r.get('return_type')})",
        )
        if r.get("status") == "confirmed":
            add(
                r.get("confirmed_at"), name_of(r.get("confirmed_by")), "return_confirmed",
                f"Возврат №{r['id']} подтверждён",
            )
        elif r.get("status") == "rejected":
            add(
                r.get("confirmed_at"), name_of(r.get("confirmed_by")), "return_rejected",
                f"Возврат №{r['id']} отклонён",
            )

    add(
        order.get("cancelled_at"), name_of(order.get("cancelled_by")), "order_cancelled",
        "Заказ отменён" + (f": {order['cancellation_reason']}" if order.get("cancellation_reason") else ""),
    )

    events.sort(key=lambda e: e["ts"] or "")
    return events
