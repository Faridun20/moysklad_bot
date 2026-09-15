"""E2E: себестоимость от выключателя до отчёта руководства.

Сценарий владельца целиком: учёт включают кнопкой → контейнер прибыл → босс
вписывает цены закупки и курс прибытия на карточке → часть товара продана в
сумах → в «Продажи → Отчёт» прибыль и курсовая разница сходятся с расчётом
руками, а менеджер ни на карточке, ни в накладных цен не видит.

Приход контейнера и продажа заводятся сервисами: их экраны покрыты своими
тестами (`test_stock.py`, `test_webapp_flows.py`), здесь проверяются только
экраны себестоимости.
"""

from __future__ import annotations

from datetime import date

from tests.e2e.conftest import go, settled, tab

import pytest

# Руководитель здесь делает работу менеджера — с «Рабочими действиями»
# (conftest.boss_work_actions). Вид по умолчанию — test_boss_ui.py.
pytestmark = pytest.mark.usefixtures("boss_work_actions")

UZS_ARRIVAL = 12500  # курс, который босс впишет на карточке
UZS_TODAY = 13000    # курс дня продажи


def _seed_rates(e2e):
    db = e2e.db
    today = date.today().strftime("%Y-%m-%d")
    assert db.set_currency_rate("UZS", 1 / UZS_TODAY, 0)[0]
    # Архив ЦБ на дату прибытия — подсказка курса без похода в сеть.
    db.set_currency_rate_daily("UZS", today, 1 / 12650.0, "cbu")
    db.set_currency_rate_daily("CNY", today, 1800 / 12650.0, "cbu")


def _arrived_container(e2e, qty=10):
    from services import container_receipt, containers

    boss = e2e.ids["boss"]
    pid = e2e.run(container_receipt.create_product("Гидромолот HB-20"))["product_id"]
    cid = e2e.run(containers.create_container(number="COST1234567", created_by=boss))["container_id"]
    item = e2e.run(containers.add_item(cid, name="Гидромолот HB-20", expected_qty=qty,
                                       product_id=pid))["item_id"]
    assert e2e.run(containers.mark_arrived(cid, user_id=boss))["ok"]
    assert e2e.run(containers.set_arrived_quantities(cid, {item: qty}, user_id=boss))["ok"]
    assert e2e.run(container_receipt.receive(cid, user_id=boss))["ok"]
    return pid, cid, item


def _sell_in_sums(e2e, pid, qty, price_sum):
    from services.order_workflow import approve_shipment_request, submit_order

    db, ids = e2e.db, e2e.ids
    cp = e2e.rows("SELECT id FROM counterparties ORDER BY id LIMIT 1")[0]["id"]
    oid = db.create_order(ids["mgr"], "Manager", "")
    db.update_order_agent(oid, str(cp), "ООО Ромашка")
    db.update_order_currency(oid, "UZS")
    db.add_order_item(oid, "Гидромолот HB-20", "", qty, "шт", price_sum, product_id=pid)
    res = e2e.run(submit_order(oid, ids["mgr"], "Manager", payment_type="paid"))
    assert res.get("ok"), res
    ap = e2e.run(approve_shipment_request(res["req_id"], ids["boss"], "Boss", e2e.bot))
    assert ap.get("ok"), ap
    return oid


def _text(page, selector):
    # toLocaleString('ru-RU') разделяет разряды неразрывным пробелом.
    return page.locator(selector).inner_text().replace("\xa0", " ").replace("\u202f", " ")


def _open_container(page, cid):
    go(page, "stock")
    tab(page, "containers")
    page.click(f'[data-container="{cid}"]')
    page.wait_for_selector("#cont-supply")


def test_boss_enables_costing_enters_prices_and_sees_profit_and_fx(open_app, e2e):
    _seed_rates(e2e)
    boss = open_app(e2e.ids["boss"])

    # 1. Учёт выключен — отчёт предлагает включить.
    go(boss, "sales")
    tab(boss, "report")
    boss.wait_for_selector("#costing-enable")
    boss.click("#costing-enable")  # showConfirm → «да»
    boss.wait_for_selector(".toast:has-text('Учёт себестоимости включён')")
    assert e2e.rows("SELECT value FROM app_settings WHERE key = 'accounting_enabled'")[0]["value"] == "true"

    # 2. Контейнер прибыл и оприходован — босс вписывает цену и курс.
    pid, cid, item = _arrived_container(e2e, qty=10)
    _open_container(boss, cid)
    boss.wait_for_selector("#costing-prices")
    assert "1 USD = 12 650 сум" in _text(boss, "#costing-host"), "подсказка — курс ЦБ"
    boss.click("#costing-prices")
    boss.wait_for_selector(f"#ms-f-p_{item}")
    assert boss.input_value("#ms-f-uzs_per_usd") == "12650.00"
    assert boss.locator("#ms-f-uzs_per_unit").is_hidden(), "курс прочей валюты для USD не нужен"
    boss.fill("#ms-f-uzs_per_usd", str(UZS_ARRIVAL))
    boss.fill(f"#ms-f-p_{item}", "100")
    boss.click("#ms-submit")
    boss.wait_for_selector(".toast:has-text('Цены закупки сохранены')")
    header = e2e.rows("SELECT currency, uzs_per_usd, rate_source FROM container_costing")[0]
    assert header == {"currency": "USD", "uzs_per_usd": "12500", "rate_source": "manual"}
    inv_price = e2e.rows(
        "SELECT ii.price_cents FROM invoice_items ii JOIN container_receipt r "
        "ON r.invoice_id = ii.invoice_id"
    )[0]["price_cents"]
    assert inv_price == 10000, "приходная накладная получила цену партии"

    # 3. Продали 4 шт по 1 625 000 сум ($125 по курсу 13 000).
    _sell_in_sums(e2e, pid, 4, 1_625_000)
    sale = e2e.rows("SELECT quantity, cost_base_cents, cost_source FROM sale_costs")
    assert sale == [{"quantity": 4, "cost_base_cents": 40000, "cost_source": "batch"}]

    # 4. Отчёт: выручка $500, себестоимость $400, прибыль +$100;
    #    по курсу прибытия выручка стоила бы $520 → курсовая разница −$20.
    go(boss, "sales")
    tab(boss, "report")
    boss.wait_for_selector("#costing-fx-text")
    settled(boss)
    block = _text(boss, "#costing-report")
    assert "+100 USD" in block, block
    assert "−20 USD" in block, block
    assert "потеряли на курсе 20 USD" in block
    assert "COST1234567" in block, "контейнер в отчёте"

    # 5. Карточка контейнера: закуплено → продано → осталось → маржа.
    _open_container(boss, cid)
    boss.wait_for_selector("#costing-host .stat-grid")
    card = _text(boss, "#costing-host")
    assert "10 шт" in card and "4 шт" in card and "6 шт" in card, card
    assert "+100 USD" in card and "−20 USD" in card, card


def test_manager_sees_no_costs_on_container_or_invoices(open_app, e2e):
    _seed_rates(e2e)
    e2e.db.set_setting("accounting_enabled", True)
    pid, cid, item = _arrived_container(e2e, qty=5)
    from services import costing

    assert e2e.run(costing.save_container_costing(
        cid, currency="USD", uzs_per_usd="12500", prices={item: "100"}, user_id=e2e.ids["boss"],
    ))["ok"]

    mgr = open_app(e2e.ids["mgr"])
    _open_container(mgr, cid)
    assert mgr.locator("#costing-host").inner_html().strip() == ""
    assert mgr.locator("#costing-prices").count() == 0

    go(mgr, "stock")
    tab(mgr, "invoices")
    mgr.wait_for_selector("#wh-new")
    settled(mgr)
    text = mgr.locator("#content").inner_text()
    assert "500,00" not in text and "500.00" not in text, "сумма прихода контейнера скрыта"

    statuses = mgr.evaluate(
        """async (cid) => {
        const out = {};
        for (const p of ['/api/costing/report', '/api/costing/container']) {
            const r = await fetch(p, {method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({initData: String(window.Telegram.WebApp.initData), container_id: cid})});
            out[p] = r.status;
        }
        return out;
    }""",
        cid,
    )
    assert statuses == {"/api/costing/report": 403, "/api/costing/container": 403}


def test_switch_off_report_and_container_card_stay_as_before(open_app, e2e):
    """Выключатель выключен: на карточке контейнера нет блока, приход без цен."""
    _seed_rates(e2e)
    _pid, cid, _item = _arrived_container(e2e, qty=3)
    boss = open_app(e2e.ids["boss"])
    _open_container(boss, cid)
    boss.wait_for_timeout(300)
    assert boss.locator("#costing-prices").count() == 0
    assert e2e.rows("SELECT COUNT(*) AS n FROM cost_batches")[0]["n"] == 0
