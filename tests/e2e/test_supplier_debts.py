"""
E2E «Деньги → Поставщикам»: приход от поставщика → долг на экране → выплата
формой → остаток уменьшился → выплата видна в истории.

Настоящий Chromium против живого uvicorn. Проверяется ровно то, чего не видят
слои ниже: вкладка появляется у руководителя и её нет у менеджера, форма
собирает выплату из сегментов и листа выбора счёта, а число на экране меняется
после записи.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page

from tests.e2e.conftest import go, nav_screens, settled, tab, toast_text

pytestmark = pytest.mark.e2e


def _supplier(e2e, name: str = "Shandong Machinery") -> int:
    e2e.exec(
        "INSERT INTO counterparties (name, type, created_at) VALUES (?, ?, ?)",
        (name, "supplier", e2e.db.now_str()),
    )
    return int(e2e.rows("SELECT MAX(id) AS id FROM counterparties")[0]["id"])


def _incoming(e2e, supplier_id: int, *, price_cents: int = 500_000, qty: float = 2) -> int:
    from services import warehouse

    res = e2e.run(warehouse.create_invoice(
        invoice_type="incoming",
        warehouse_id=e2e.ids["warehouse"],
        counterparty_id=supplier_id,
        items=[{"product_id": e2e.ids["product"], "quantity": qty, "price_cents": price_cents}],
        created_by=e2e.ids["boss"],
    ))
    assert res["ok"], res
    return int(res["invoice_id"])


def _open_suppliers(page: Page) -> None:
    go(page, "money")
    tab(page, "suppliers")
    _loaded(page)


def _text(page: Page) -> str:
    """Текст экрана в сравнимом виде: `formatMoney` разделяет разряды неразрывным
    пробелом, а `.section-label` поднимает заголовки в капс средствами CSS."""
    return page.locator("#content").inner_text().replace("\u00a0", " ").lower()


def _loaded(page: Page) -> None:
    """Экран дорисован: спиннер `loading()` ушёл (как у соседних «Долгов»,
    он не `.loader` и не скелетон, поэтому общий `settled` его не ждёт)."""
    page.wait_for_selector("#content .spinner-wrap", state="detached")
    settled(page)


def test_supplier_tab_is_boss_only(open_app, e2e):
    """Сумма прихода — закупочная цена: у менеджера вкладки нет вовсе."""
    _incoming(e2e, _supplier(e2e))

    mgr = open_app(e2e.ids["mgr"])
    go(mgr, "money")
    assert mgr.locator('.seg-item[data-sect="suppliers"]').count() == 0

    boss = open_app(e2e.ids["boss"])
    assert "money" in nav_screens(boss)
    _open_suppliers(boss)
    assert boss.locator('.seg-item[data-sect="suppliers"]').count() == 1
    assert "shandong machinery" in _text(boss)


def test_receipt_becomes_debt_and_payment_reduces_it(open_app, e2e):
    """Приход → долг 10 000 USD → выплата 4 000 со счёта → осталось 6 000."""
    supplier_id = _supplier(e2e)
    _incoming(e2e, supplier_id, price_cents=500_000, qty=2)

    page = open_app(e2e.ids["boss"])
    _open_suppliers(page)
    body = _text(page)
    assert "10 000 usd" in body, body

    # Выплата по конкретному приходу: строка списка → лист действий → форма.
    page.click('#content .c-row[data-inv]')
    sheet = page.locator(".c-overlay").last
    sheet.wait_for()
    sheet.locator("button", has_text="Записать выплату").first.click()

    form = page.locator(".c-overlay").last
    form.wait_for()
    form.locator("#ms-f-amount").fill("4000")
    form.locator('.seg-item[data-opt="bank"]').click()
    from tests.e2e.conftest import pick_account

    pick_account(page, form.locator(".pay-part-account").first, "bank")
    form.locator("#ms-submit").click()
    page.wait_for_function("() => !document.querySelector('.c-overlay')")
    _loaded(page)

    assert "Выплата записана" in toast_text(page)
    after = _text(page)
    assert "6 000 usd" in after, after
    # Выплата попала в ленту с подписью «со счёта …», а не «на счёт».
    assert "со счёта" in after, after
    # И в базу — именно в supplier_payments, а не в payments клиентов.
    assert e2e.rows("SELECT id FROM payments") == []
    paid = e2e.rows("SELECT amount_cents, currency FROM supplier_payments")
    assert paid == [{"amount_cents": 400_000, "currency": "USD"}]


def test_marking_receipt_paid_removes_it_from_debts(open_app, e2e):
    """«Уже оплачено» убирает приход из долгов — без выдуманной выплаты."""
    _incoming(e2e, _supplier(e2e), price_cents=100_000, qty=1)

    page = open_app(e2e.ids["boss"])
    _open_suppliers(page)
    assert "1 000 usd" in _text(page)

    page.click('#content .c-row[data-inv]')
    sheet = page.locator(".c-overlay").last
    sheet.wait_for()
    sheet.locator("button", has_text="Отметить «уже оплачено»").click()
    # showConfirm подменён заглушкой: ответ «да» приходит сразу.
    page.wait_for_function("() => !document.querySelector('.c-overlay')")
    _loaded(page)

    assert "долгов перед поставщиками нет" in _text(page)
    assert e2e.rows("SELECT id FROM supplier_payments") == []


def test_unpriced_receipt_is_explained_not_hidden(open_app, e2e):
    """Контейнер посчитали, цену не вписали — экран говорит об этом прямо."""
    _incoming(e2e, _supplier(e2e), price_cents=None, qty=3)

    page = open_app(e2e.ids["boss"])
    _open_suppliers(page)
    body = _text(page)
    assert "приход без суммы" in body, body
    assert "нет цены" in body, body
