"""
«Счёт» — документ, который менеджер показывает клиенту ДО отгрузки.

Зачем он есть (жалоба владельца): печатная форма в проекте появлялась только
ПОСЛЕ отгрузки — расходной накладной из `services/order_shipment.py`. То есть
разговор с клиентом шёл в обратном порядке: сначала отгрузи товар, потом
покажи бумагу. Менеджеру нужно наоборот — составил заказ, распечатал счёт,
клиент согласился, и только тогда заявка на отгрузку.

**СЧЁТ НИЧЕГО НЕ ДВИГАЕТ.** Это бумага, а не документ учёта:

* не меняет остаток (ни строки в `stock`, ни накладной в `invoices`);
* не создаёт платежа и не влияет на долг (`services.debts`, дебиторка);
* не меняет статус заказа и не заменяет заявку на отгрузку;
* в выручку, отчёты о продажах и прибыли не попадает.

Поэтому модуль состоит из ЧТЕНИЯ: собрать заказ, контрагента и реквизиты
компании в один словарь для печатной формы. Единственная запись — строка
аудита о том, что счёт распечатали или отправили (её пишет ручка, не сборщик):
руководителю важно знать, что клиенту уже показывали бумагу. Сторож —
`tests/test_sales_invoice.py::test_building_and_printing_the_invoice_moves_nothing`.

**Номер счёта — это номер заказа.** Своей последовательности нет намеренно:

* `invoice_counters` выдаёт номера СКЛАДСКИХ накладных (`IN-`/`OUT-`), и
  подмешивать туда бумагу, по которой товар не двигался, значит делать дырки в
  складской нумерации из документов, которых на складе не было;
* счёт печатают дважды и трижды (клиент потерял, передумал, попросил ещё раз),
  и счётчик выдавал бы каждый раз новый номер — три разных счёта на один заказ,
  которые клиент не свяжет между собой;
* «Счёт № 31» и «Заказ #31» — одно и то же, и когда клиент звонит с номером
  счёта, менеджер сразу знает, какой заказ открыть.

По той же причине ДАТА счёта — дата заказа, а не дата печати: второй
распечатанный экземпляр обязан быть тем же документом, что первый.
"""

from __future__ import annotations

from services import money
from services.numerals import amount_in_words

# Статусы, в которых счёт не выписывается. Отменённый и отклонённый заказ —
# это не «предложение клиенту», а закрытая история; печатать по нему бумагу с
# ценами значит дать клиенту документ на то, чего не будет.
REFUSED_STATUSES = ("cancelled", "rejected")

REASON_TEXT = {
    "no_order": "Заказ не найден",
    "no_agent": "Сначала выберите клиента — без него счёт выписать некому",
    "no_items": "В заказе нет позиций — счёт выставлять не на что",
    "bad_status": "По отменённому заказу счёт не выписывают",
}


class SalesInvoiceError(Exception):
    """Счёт не собрать. `code` — ключ REASON_TEXT, `message` — текст человеку."""

    def __init__(self, code: str):
        self.code = code
        self.message = REASON_TEXT.get(code, "Счёт не удалось собрать")
        super().__init__(self.message)


def _date_ru(raw: str | None) -> str:
    """`2026-09-16 14:05:00` → `16.09.2026`. Непонятный формат — как есть."""
    text = str(raw or "").strip()[:10]
    parts = text.split("-")
    if len(parts) == 3 and all(parts) and len(parts[0]) == 4:
        return f"{parts[2]}.{parts[1]}.{parts[0]}"
    return text


def build_lines(items: list[dict]) -> list[dict]:
    """Строки счёта: наименование, кол-во, ед., цена и сумма в копейках.

    Считаем ровно так же, как расходная накладная (`money.mul_qty`): счёт и
    накладная по одному заказу обязаны сойтись до цента, иначе клиент получит
    два документа с разными итогами.
    """
    lines = []
    for it in items:
        price_cents = int(it.get("price_cents") or 0)
        qty = it.get("quantity") or 0
        lines.append(
            {
                "product_name": it.get("product_name") or "",
                "quantity": float(qty),
                "unit": it.get("unit") or "шт",
                "price_cents": price_cents,
                "amount_cents": money.mul_qty(price_cents, qty),
                "note": it.get("note") or "",
            }
        )
    return lines


def total_cents(lines: list[dict]) -> int:
    return money.add(*[int(ln["amount_cents"]) for ln in lines]) if lines else 0


async def build_sales_invoice(order_id: int) -> dict:
    """Собрать данные счёта по заказу. ТОЛЬКО чтение — ничего не меняет.

    Ни остаток, ни долг, ни статус заказа функция не трогает: счёт — бумага
    клиенту, а не документ учёта. Бросает `SalesInvoiceError`, если счёт
    выписывать не на что (нет клиента, нет позиций, заказ отменён).
    """
    import asyncio

    from services import async_db as adb
    from services import counterparties as cp_service
    from services.documents import company_requisites

    order = await adb.get_order(int(order_id))
    if not order:
        raise SalesInvoiceError("no_order")
    if (order.get("status") or "") in REFUSED_STATUSES:
        raise SalesInvoiceError("bad_status")
    if not str(order.get("agent_id") or "").strip():
        raise SalesInvoiceError("no_agent")

    items = await adb.get_order_items(int(order_id))
    lines = build_lines(items)
    if not lines:
        raise SalesInvoiceError("no_items")

    agent = await cp_service.get(order.get("agent_id"))
    # Реквизиты лежат в `app_settings` и читаются синхронно — в поток, как это
    # делает форма расписки (`webapp.server.api_docs_types`).
    company = await asyncio.to_thread(company_requisites)

    total = total_cents(lines)
    currency = order.get("currency") or "USD"
    return {
        "order_id": int(order_id),
        # Номер счёта = номер заказа (см. докстринг модуля).
        "number": str(order_id),
        "date": _date_ru(order.get("created_at")),
        "currency": currency,
        "company": company,
        "client_name": order.get("agent_name") or (agent or {}).get("name") or "—",
        "client_phone": (agent or {}).get("phone") or "",
        "manager_name": order.get("full_name") or "",
        "comment": order.get("comment") or "",
        "lines": lines,
        "total_cents": total,
        # Прописью — своя реализация (services/numerals.py): num2words сознательно
        # не используется, см. докстринг того модуля.
        "total_words": amount_in_words(money.from_cents(total), "ru"),
    }


def audit_details(doc: dict, action: str) -> str:
    """Строка для `audit_log`: по какому заказу и на какую сумму бумага."""
    total = money.format_cents(int(doc.get("total_cents") or 0), decimals=2)
    return (
        f"{action}: счёт № {doc.get('number')} по заказу #{doc.get('order_id')} · "
        f"{doc.get('client_name')} · {total} {doc.get('currency')}"
    )
