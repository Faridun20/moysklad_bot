"""E2E, вторая волна: склад.

Накладная руками, каталог и цена, контейнер от заведения до оприходования,
техника от карточки до рассрочки, «Залежалось». Везде проверяем не экран, а
последствия: остаток, накладную, график платежей.
"""

from __future__ import annotations

from tests.e2e.conftest import go, settled, sheet_fill, tab


def _stock(e2e) -> float:
    return e2e.rows("SELECT quantity FROM stock WHERE product_id = ?", (e2e.ids["product"],))[0]["quantity"]


# ─── Накладная руками: приход и отмена ───────────────────────────────────────


def test_boss_posts_incoming_invoice_and_cancels_it(open_app, e2e):
    boss = open_app(e2e.ids["boss"])
    go(boss, "stock")
    boss.click('[data-wh-go="whinvoices"]')
    boss.click("#wh-new")
    boss.wait_for_selector('[data-whtype="incoming"]')
    boss.click('[data-whtype="incoming"]')
    boss.wait_for_selector('[data-whtype="incoming"].active')
    boss.click("#wh-add")
    boss.wait_for_selector('.wh-pos [data-f="quantity"]')
    boss.fill('.wh-pos [data-f="quantity"]', "5")
    boss.fill("#wh-comment", "Довоз со склада поставщика")
    boss.click("#wh-save")
    boss.wait_for_selector(".toast:has-text('проведена')")
    boss.wait_for_selector("#wh-new")  # вернулись в список

    assert _stock(e2e) == 25
    inv = e2e.rows(
        "SELECT id, type, comment FROM invoices ORDER BY id DESC LIMIT 1"
    )[0]
    assert inv["type"] == "incoming" and inv["comment"] == "Довоз со склада поставщика"
    assert boss.locator(f'[data-wh-cancel="{inv["id"]}"]').count() == 1

    boss.click(f'[data-wh-cancel="{inv["id"]}"]')  # showConfirm → «да»
    boss.wait_for_function(
        "(id) => !document.querySelector(`[data-wh-cancel=\"${id}\"]`)", arg=inv["id"]
    )
    assert _stock(e2e) == 20, "отмена вернула остаток"
    assert e2e.rows("SELECT cancelled_at FROM invoices WHERE id = ?", (inv["id"],))[0]["cancelled_at"]


def test_incoming_invoice_form_rejects_zero_quantity(open_app, e2e):
    boss = open_app(e2e.ids["boss"])
    go(boss, "stock")
    boss.click('[data-wh-go="whinvoices"]')
    boss.click("#wh-new")
    boss.wait_for_selector('[data-whtype="incoming"]')
    boss.click('[data-whtype="incoming"]')
    boss.wait_for_selector('[data-whtype="incoming"].active')
    boss.click("#wh-add")
    boss.fill('.wh-pos [data-f="quantity"]', "0")
    boss.click("#wh-save")
    boss.wait_for_selector(".toast:has-text('больше нуля')")
    assert _stock(e2e) == 20
    assert boss.locator("#wh-save").count() == 1, "форма осталась, черновик не потерян"


# ─── Каталог: поиск и цена ───────────────────────────────────────────────────


def test_catalog_search_and_price_editor(open_app, e2e):
    boss = open_app(e2e.ids["boss"])
    go(boss, "stock")
    boss.wait_for_selector("#stock-search")
    boss.fill("#stock-search", "кабель")
    boss.wait_for_selector(".stock-row:has-text('Кабель')")
    assert boss.locator(".stock-row").count() == 1

    boss.fill("#stock-search", "нет такого товара")
    boss.wait_for_function("() => !document.querySelector('.stock-row')")
    boss.fill("#stock-search", "")
    boss.wait_for_selector(".stock-row")

    boss.click("[data-price-idx]")
    boss.wait_for_selector("#pe-sale")
    boss.fill("#pe-sale", "12.5")
    boss.fill("#pe-cost", "9")
    boss.click("#pe-save")
    boss.wait_for_selector(".toast:has-text('Цена сохранена')")

    row = e2e.rows("SELECT sale_price_cents, cost_price_cents FROM product_prices")
    assert row == [{"sale_price_cents": 1250, "cost_price_cents": 900}]
    assert "12,5" in boss.locator(".stock-row").first.inner_text().replace(".", ",")


def test_manager_catalog_has_no_price_editor(open_app, e2e):
    mgr = open_app(e2e.ids["mgr"])
    go(mgr, "stock")
    mgr.wait_for_selector(".stock-row")
    assert mgr.locator("[data-price-idx]").count() == 0


# ─── Контейнер: заведение → состав → прибытие → сверка → оприходование ────────


def test_container_lifecycle_moves_stock_once(open_app, e2e):
    boss = open_app(e2e.ids["boss"])
    go(boss, "stock")
    tab(boss, "containers")
    boss.click("#container-new")
    sheet_fill(boss, {"number": "msku 123-456"})
    boss.click("#ms-submit")
    boss.wait_for_selector("#cont-item-add")
    cont = e2e.rows("SELECT id, number, status FROM containers")[0]
    assert cont["status"] == "in_transit"
    assert cont["number"] == "MSKU123456", "номер нормализован"

    # Позиция: товар выбирается из каталога, а не угадывается по имени.
    boss.click("#cont-item-add")
    boss.wait_for_selector("#ms-f-name")
    # Что фронт реально отправил: спор «потерял выбор фронт или сервер»
    # решается по телу запроса, а не по догадкам.
    sent: list[dict] = []
    boss.on("request", lambda r: sent.append(r.post_data_json)
            if r.url.endswith("/api/containers/item_add") else None)
    boss.fill("#ms-f-name", "Кабель")  # одно событие input → один запрос подсказки
    boss.click(f'.c-overlay [data-product="{e2e.ids["product"]}"]')
    boss.wait_for_selector(f'.c-overlay [data-product="{e2e.ids["product"]}"].picked')
    boss.wait_for_function("() => document.querySelector('#ms-f-name').value === 'Кабель ВВГ 3x2.5'")
    boss.fill("#ms-f-expected_qty", "10")
    boss.click("#ms-submit")
    boss.wait_for_selector("#cont-arrive")
    assert sent and sent[-1].get("product_id") == e2e.ids["product"], sent
    link = e2e.rows("SELECT product_id FROM container_item_products")
    assert link == [{"product_id": e2e.ids["product"]}]

    boss.click("#cont-arrive")  # confirmDialog → «да»
    boss.wait_for_selector(".qty-input[data-item]")
    assert e2e.rows("SELECT status FROM containers")[0]["status"] == "arrived"
    boss.fill(".qty-input[data-item]", "8")
    boss.click("#cont-save")
    boss.wait_for_selector(".toast:has-text('Сверка сохранена')")
    item = e2e.rows("SELECT expected_qty, arrived_qty FROM container_items")[0]
    assert item == {"expected_qty": 10, "arrived_qty": 8}
    # Недостача видна на карточке.
    boss.wait_for_selector("#cont-supply")
    assert "2" in boss.locator("#content").inner_text()

    boss.click("#cont-supply")
    boss.wait_for_selector(".toast:has-text('Оприходовано')")
    assert _stock(e2e) == 28, "20 + 8 фактически прибывших"
    receipt = e2e.rows("SELECT invoice_id FROM container_receipt")[0]
    assert receipt["invoice_id"]
    inv = e2e.rows("SELECT type FROM invoices WHERE id = ?", (receipt["invoice_id"],))[0]
    assert inv["type"] == "incoming"

    # Повторное оприходование не удваивает остаток: прежняя накладная
    # отменяется, новая проводится.
    boss.wait_for_function("() => !document.querySelector('.toast')")
    boss.click("#cont-supply")
    boss.wait_for_selector(".toast:has-text('Оприходовано')")
    assert _stock(e2e) == 28
    assert e2e.rows("SELECT COUNT(*) AS n FROM invoices WHERE cancelled_at IS NULL")[0]["n"] == 2  # сид + одна живая


def test_container_list_shows_mismatch_summary(open_app, e2e):
    from services import containers

    ids = e2e.ids
    c = e2e.run(containers.create_container(number="ABCD1", created_by=ids["boss"]))
    assert c.get("ok"), c
    cid = c["container_id"]
    e2e.run(containers.add_item(cid, name="Кабель ВВГ 3x2.5", expected_qty=10, unit="м",
                                product_id=ids["product"]))
    e2e.run(containers.mark_arrived(cid, user_id=ids["boss"]))
    item = e2e.rows("SELECT id FROM container_items")[0]["id"]
    e2e.run(containers.set_arrived_quantities(cid, {item: "7"}, user_id=ids["boss"]))

    boss = open_app(ids["boss"])
    go(boss, "stock")
    tab(boss, "containers")
    boss.wait_for_selector(f'[data-container="{cid}"]')
    row = boss.locator(f'[data-container="{cid}"]')
    assert row.get_attribute("data-status") == "rejected", "расхождение подсвечено"
    assert "3" in row.inner_text()


# ─── Техника: карточка → моточасы → рассрочка → платёж ───────────────────────


def test_machine_from_card_to_installment(open_app, e2e):
    boss = open_app(e2e.ids["boss"])
    go(boss, "stock")
    tab(boss, "machines")
    boss.click("#machine-new")
    sheet_fill(boss, {"vin": "jcb-3cx 7788", "name": "JCB 3CX 2019", "year": "2019",
                      "hours": "1500", "price": "25000", "cost": "20000"})
    boss.click("#ms-submit")
    boss.wait_for_selector("[data-machine]")
    m = e2e.rows("SELECT id, vin, status, hours, cost_cents FROM machines")[0]
    assert m["vin"] == "JCB3CX7788" and m["status"] == "in_transit" and m["hours"] == 1500

    boss.click(f'[data-machine="{m["id"]}"]')
    boss.wait_for_selector('[data-mact="hours"]')
    # Себестоимость видна руководству.
    assert "20" in boss.locator("#content").inner_text()

    # Моточасы меньше предыдущих — опечатка, форма спрашивает; босс подтверждает.
    boss.click('[data-mact="hours"]')
    sheet_fill(boss, {"hours": "1400"})
    boss.click("#ms-submit")
    boss.wait_for_function("() => window.__tgAlerts.some(a => /счётчик/i.test(a))")
    boss.wait_for_selector(".toast:has-text('Моточасы записаны')")
    assert e2e.rows("SELECT hours FROM machines")[0]["hours"] == 1400
    assert e2e.rows("SELECT COUNT(*) AS n FROM machine_hours")[0]["n"] == 2

    # На склад → бронь → снять бронь: граф переходов один, подписи зависят от пары.
    boss.wait_for_function("() => !document.querySelector('.toast')")
    boss.click('[data-mstatus-to="in_stock"]')
    boss.wait_for_selector(".toast:has-text('Статус изменён')")
    boss.wait_for_selector('[data-mstatus-to="reserved"]')
    assert e2e.rows("SELECT status FROM machines")[0]["status"] == "in_stock"

    boss.wait_for_function("() => !document.querySelector('.toast')")
    boss.click('[data-mact="credit"]')
    sheet_fill(boss, {"price": "24000", "down_payment": "4000", "months": "4",
                      "buyer_name": "Азиз Рахимов", "buyer_phone": "+998901112233",
                      "buyer_passport": "AA1234567"})
    boss.click("#ms-submit")
    boss.wait_for_selector(".toast:has-text('Рассрочка оформлена')")
    deal = e2e.rows("SELECT id, kind, price_cents, buyer_name FROM machine_deals")[0]
    assert deal["kind"] == "credit" and deal["price_cents"] == 2_400_000
    sched = e2e.rows(
        "SELECT seq, amount_cents, paid_at FROM machine_deal_payments ORDER BY seq"
    )
    assert [s["seq"] for s in sched] == [0, 1, 2, 3, 4]
    assert sched[0]["amount_cents"] == 400_000 and sched[0]["paid_at"], "взнос оплачен сразу"
    assert sum(s["amount_cents"] for s in sched) == 2_400_000, "график сходится с ценой"
    assert e2e.rows("SELECT status FROM machines")[0]["status"] == "on_credit"

    # Первый плановый платёж «оплачен» — это поступление на плановую сумму.
    boss.wait_for_function("() => !document.querySelector('.toast')")
    boss.wait_for_selector('[data-payment][data-paid="0"]')
    boss.click('[data-payment][data-paid="0"]')
    boss.wait_for_selector(".toast:has-text('Отмечено')")
    rec = e2e.rows("SELECT amount_cents FROM machine_payment_receipts")
    assert rec == [{"amount_cents": 500_000}]
    paid = e2e.rows("SELECT COUNT(*) AS n FROM machine_deal_payments WHERE paid_at IS NOT NULL")[0]["n"]
    assert paid == 2, "взнос + первый плановый платёж"
    # У взноса переключателя нет (получен в момент сделки) — на экране отмечен
    # один плановый платёж.
    boss.wait_for_function(
        "() => document.querySelectorAll('[data-payment][data-paid=\"1\"]').length === 1"
    )

    # Долг по технике виден в «Долгах» по имени покупателя.
    go(boss, "money")
    tab(boss, "debts")
    boss.wait_for_selector('[data-buyer]')
    assert "Азиз" in boss.locator("[data-buyer]").first.inner_text()


def test_manager_sees_machine_without_cost_and_passport(open_app, e2e):
    from services import machines

    ids = e2e.ids
    r = e2e.run(machines.create_machine(vin="ZX200-1", name="Hitachi ZX200", price_cents=3_000_000,
                                        cost_cents=2_200_000, created_by=ids["boss"], status="in_stock"))
    mid = r["machine_id"]
    e2e.run(machines.create_deal(mid, kind="sale", price_cents=3_000_000, buyer_name="Клиент",
                                 buyer_passport="AB9999999", created_by=ids["boss"]))

    mgr = open_app(ids["mgr"])
    go(mgr, "stock")
    tab(mgr, "machines")
    mgr.wait_for_selector(f'[data-machine="{mid}"]')
    mgr.click(f'[data-machine="{mid}"]')
    mgr.wait_for_selector('[data-mact="hours"]')
    text = mgr.locator("#content").inner_text()
    assert "22 000" not in text and "22000" not in text, "себестоимость режется в сервисе"
    assert "AB9999999" not in text, "паспорт покупателя — только руководству"
    assert mgr.locator('[data-mact="edit"]').count() == 0


# ─── «Залежалось» — только руководству, с остатком на экране ─────────────────


def test_stale_tab_is_internal_and_shows_stock(open_app, e2e):
    boss = open_app(e2e.ids["boss"])
    go(boss, "stock")
    assert boss.locator('.seg-item[data-sect="stale"]').count() == 1
    tab(boss, "stale")
    settled(boss)
    text = boss.locator("#content").inner_text()
    assert "Ошибка" not in text and "Нет доступа" not in text

    mgr = open_app(e2e.ids["mgr"])
    go(mgr, "stock")
    assert mgr.locator('.seg-item[data-sect="stale"]').count() == 0
