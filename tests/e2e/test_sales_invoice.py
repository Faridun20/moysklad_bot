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
    mgr.wait_for_selector(".btn-sales-invoice")
    order_id = e2e.rows("SELECT id FROM orders ORDER BY id DESC LIMIT 1")[0]["id"]
    assert e2e.rows("SELECT status FROM orders WHERE id = ?", (order_id,))[0]["status"] == "draft"

    mgr.click(".btn-sales-invoice")
    mgr.wait_for_selector(".c-overlay .c-sheet-title")
    sheet = mgr.locator(".c-overlay").last
    assert f"Счёт № {order_id}" in sheet.inner_text()
    assert "Кабель" in sheet.inner_text()
    assert "200,00 USD" in sheet.inner_text()

    mgr.click(".c-overlay #ms-submit")
    mgr.wait_for_selector(".toast:has-text('Счёт отправлен')")
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
    mgr.wait_for_selector(".btn-sales-invoice")
