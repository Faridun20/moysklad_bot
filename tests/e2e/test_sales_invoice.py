"""
«Счёт» клиенту ДО отгрузки — путь целиком, в настоящем браузере.

Жалоба владельца, ради которой всё и делалось: «может, он просто хочет
накладную на отгрузку создать, чтобы показать клиенту — вот, вот такая. И
такой возможности не существует. Сначала отгрузить, потом появляется
накладная». Здесь проверяется обратный, правильный порядок: собрал заказ →
показал счёт → и только потом заявка и отгрузка.

Счёт при этом НИЧЕГО не двигает: между «собрал» и «отправил заявку» остаток
склада обязан остаться прежним, а накладных не появиться. Юнит-сторож того же
инварианта — `tests/test_sales_invoice.py`.
"""

from __future__ import annotations

from tests.e2e.conftest import go, settled, tab

import pytest


def _stock(e2e) -> float:
    rows = e2e.rows(
        "SELECT quantity FROM stock WHERE product_id = ?", (e2e.ids["product"],)
    )
    return float(rows[0]["quantity"]) if rows else 0.0


def test_manager_shows_the_invoice_before_the_request_and_then_ships(open_app, e2e):
    pytest.importorskip("weasyprint", reason="нет weasyprint/системных pango")
    ids = e2e.ids
    stock_before = _stock(e2e)
    # Счёт на оплату без банковских реквизитов не выписывается.
    for key, value in {"company_tin": "301234567", "company_address": "Ташкент",
                       "company_bank_account": "20208000900123456001",
                       "company_bank_name": "Капиталбанк", "company_bank_mfo": "01088"}.items():
        e2e.db.set_setting(key, value, ids["boss"])

    mgr = open_app(ids["mgr"])
    go(mgr, "sales")
    mgr.click("#btn-new-order")
    mgr.wait_for_selector("#choose-agent")
    mgr.click("#choose-agent")
    mgr.wait_for_selector(".agent-row")
    mgr.click('.agent-row:has-text("Ромашка")')
    mgr.wait_for_selector("#change-agent")
    mgr.click("#btn-add-product")
    mgr.wait_for_selector(".prod-row")
    mgr.click(f'.prod-row[data-product="{ids["product"]}"]')
    mgr.wait_for_selector("#qty-input")
    mgr.fill("#qty-input", "2")
    mgr.fill("#price-input", "100")
    mgr.evaluate("window.__tgMainClick()")
    mgr.wait_for_selector("#btn-submit:not([disabled])")

    # Заявку ещё НЕ отправляли — возвращаемся в список и печатаем счёт.
    go(mgr, "sales")
    tab(mgr, "orders")
    # «Счёт на оплату» — в подробной сводке карточки.
    mgr.click("#content [data-details-toggle]")
    mgr.wait_for_selector(".btn-sales-invoice")
    order_id = e2e.rows("SELECT id FROM orders ORDER BY id DESC LIMIT 1")[0]["id"]
    assert e2e.rows("SELECT status FROM orders WHERE id = ?", (order_id,))[0]["status"] == "draft"

    mgr.click(".btn-sales-invoice")
    mgr.wait_for_selector(".c-overlay .c-sheet-title")
    sheet = mgr.locator(".c-overlay").last
    assert f"Счёт на оплату № {order_id}" in sheet.inner_text()
    # Язык бумаги — сегментом; выбираем «Рус», сервер его запомнит.
    sheet.locator('.doc-lang [data-lang="ru"]').click()
    assert "Кабель" in sheet.inner_text()
    assert "200,00 USD" in sheet.inner_text()

    mgr.click(".c-overlay #ms-submit")
    mgr.wait_for_selector(".toast:has-text('Счёт на оплату отправлен')")
    assert e2e.rows("SELECT value FROM user_prefs WHERE user_id = ? AND pref_key = 'doc_lang'",
                    (ids["mgr"],))[0]["value"] == '"ru"'
    settled(mgr)

    # Файл ушёл тому, кто нажал: счёт обсуждают, пересылает его человек сам.
    docs = [d for d in e2e.bot.documents if "Счёт" in (d.get("caption") or "")]
    assert [d["chat_id"] for d in docs] == [ids["mgr"]]

    # И ничего не сдвинулось: ни остатка, ни накладной, ни статуса заказа.
    assert _stock(e2e) == stock_before
    assert e2e.rows("SELECT COUNT(*) AS n FROM order_shipment")[0]["n"] == 0
    assert e2e.rows("SELECT status FROM orders WHERE id = ?", (order_id,))[0]["status"] == "draft"
    printed = e2e.rows("SELECT action FROM audit_log WHERE action = 'sales_invoice_sent'")
    assert len(printed) == 1, "у руководителя есть история: счёт клиенту показывали"

    # А теперь — как раньше: заявка, одобрение, списание остатка.
    go(mgr, "sales")
    mgr.locator(".btn-edit-order").first.click()
    mgr.wait_for_selector("#btn-submit:not([disabled])")
    mgr.click("#btn-submit")
    mgr.wait_for_function("() => window.__tgAlerts.some(a => a.includes('отправлена'))")

    boss = open_app(ids["boss"])
    go(boss, "sales")
    boss.click("#show-requests")
    boss.wait_for_selector(".btn-approve")
    boss.click(".btn-approve")
    boss.wait_for_function("() => window.__tgAlerts.some(a => a.includes('одобрена'))")

    assert e2e.rows("SELECT status FROM orders WHERE id = ?", (order_id,))[0]["status"] == "approved"
    assert e2e.rows("SELECT invoice_id FROM order_shipment WHERE order_id = ?", (order_id,))[0]["invoice_id"]
    assert _stock(e2e) == stock_before - 2, "остаток списала отгрузка, а не счёт"

    # После отгрузки счёт остаётся — это просто копия того же документа.
    go(mgr, "sales")
    tab(mgr, "orders")
    # «Счёт на оплату» — в подробной сводке карточки.
    mgr.click("#content [data-details-toggle]")
    mgr.wait_for_selector(".btn-sales-invoice")


def test_client_requisites_and_waybill_language_reach_the_paper(open_app, e2e, monkeypatch):
    """ИНН/ПИНФЛ и адрес клиента вписывают в его карточке; «Отгрузки» дают
    выбрать язык товарной накладной, и печать уходит на этом языке с
    «Основание: Счёт на оплату № …» — отгрузка шла по заказу."""
    from services import invoice_pdf, printing
    from services.printing import PrintResult
    from tests.e2e.conftest import seed_order

    seeded = seed_order(e2e, qty=1, price=40.0)
    cp = seeded["counterparty_id"]
    rendered: list[dict] = []
    monkeypatch.setattr(invoice_pdf, "render_invoice_pdf", lambda inv: rendered.append(inv) or b"%PDF-1.4")
    monkeypatch.setattr(printing, "is_available", lambda: True)

    async def fake_print(pdf_bytes, *, filename="", printer_name="", label=""):
        return PrintResult(True, job="Canon-2")

    monkeypatch.setattr(printing, "print_pdf_bytes", fake_print)

    mgr = open_app(e2e.ids["mgr"])
    settled(mgr)
    mgr.evaluate("(id) => renderAgentDetail(String(id))", cp)
    mgr.wait_for_selector("#cl-req-edit")
    mgr.click("#cl-req-edit")
    mgr.fill(".c-overlay #ms-f-tin", "30123456789012")
    mgr.fill(".c-overlay #ms-f-address", "Самарканд, ул. Регистан, 1")
    mgr.click(".c-overlay #ms-submit")
    mgr.wait_for_selector(".toast:has-text('Реквизиты клиента сохранены')")
    mgr.wait_for_selector("#cl-requisites:has-text('30123456789012')")
    assert e2e.rows("SELECT tin, address FROM counterparty_requisites WHERE counterparty_id = ?", (cp,)) == [
        {"tin": "30123456789012", "address": "Самарканд, ул. Регистан, 1"}
    ]

    go(mgr, "stock")
    tab(mgr, "invoices")
    mgr.click('[data-whsub="outgoing"]')
    mgr.wait_for_selector(".doc-lang [data-lang='uz']")
    mgr.click(".doc-lang [data-lang='uz']")
    mgr.locator("[data-wh-print]").first.click()
    mgr.wait_for_selector(".toast:has-text('задание Canon-2')")
    # Фоновая печатная форма одобрения тоже рендерится этой функцией — берём
    # именно печать из «Отгрузок».
    printed = [inv for inv in rendered if inv["doc_lang"] == "uz"]
    assert len(printed) == 1
    assert printed[0]["basis"]["order_id"] == seeded["order_id"]
    assert printed[0]["buyer"]["address"] == "Самарканд, ул. Регистан, 1"
