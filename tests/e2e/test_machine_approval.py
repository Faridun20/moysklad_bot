"""E2E: сделки по технике через одобрение руководителя.

1. Менеджер оформляет рассрочку в WebApp → машина «ждёт одобрения», сделки и
   графика нет, руководителю ушла карточка → руководитель одобряет в «Складе →
   Техника» → машина «В рассрочку», график действует.
2. Продажа → «На доработку» с причиной → менеджер правит цену и отправляет
   снова → руководитель отклоняет → машина в прежнем статусе, заявки нет.
3. Руководителя в системе нет: менеджер видит кнопки решения с пометкой и
   одобряет бронь сам.
4. Выключатель «удаление — только руководитель».

Снимки экрана (390px) — в MACHINE_SHOTS_DIR, если он задан.
"""

from __future__ import annotations

import os

from tests.e2e.conftest import go, sheet_fill, tab


def _shot(page, name: str) -> None:
    folder = os.environ.get("MACHINE_SHOTS_DIR")
    if folder:
        page.wait_for_timeout(400)
        page.screenshot(path=os.path.join(folder, f"{name}.png"), full_page=True)


def _machine(e2e, vin: str, name: str, **kw) -> int:
    from services import machines

    res = e2e.run(machines.create_machine(vin=vin, name=name, created_by=e2e.ids["boss"], **kw))
    assert res.get("ok"), res
    return res["machine_id"]


def _open_machine(page, mid: int) -> None:
    go(page, "stock")
    tab(page, "machines")
    page.wait_for_selector(f'[data-machine="{mid}"]')
    page.click(f'[data-machine="{mid}"]')
    page.wait_for_selector('[data-mact="hours"]')


def _no_toast(page) -> None:
    page.wait_for_function("() => !document.querySelector('.toast')")


def test_manager_installment_is_approved_by_boss_in_webapp(open_app, e2e):
    mid = _machine(e2e, "APPR-1", "Hitachi ZX200", status="in_stock", price_cents=2_500_000)
    mgr = open_app(e2e.ids["mgr"])
    _open_machine(mgr, mid)
    for sel in ('[data-mact="reserve"]', '[data-mact="sale"]', '[data-mact="credit"]'):
        assert mgr.locator(sel).count() == 1, sel

    mgr.click('[data-mact="credit"]')
    mgr.wait_for_selector("#ms-f-months")
    assert "на одобрение" in mgr.inner_text("#ms-submit")
    sheet_fill(mgr, {"price": "24000", "down_payment": "0", "months": "6",
                     "buyer_name": "Азиз Рахимов", "buyer_phone": "+998901112233",
                     "buyer_passport": "AA1234567", "buyer_note": "Самовывоз"})
    _shot(mgr, "01-manager-installment-form")
    mgr.click("#ms-submit")
    mgr.wait_for_selector(".toast:has-text('руководителю')")
    mgr.wait_for_selector("[data-mreq]")
    assert "Ждёт одобрения" in mgr.inner_text("#content")
    assert mgr.locator('[data-mact="sale"]').count() == 0, "вторую заявку на машину не оформить"
    assert mgr.locator("[data-mreq-approve]").count() == 0, "при живом руководителе сам не одобряет"
    _no_toast(mgr)
    _shot(mgr, "02-manager-card-pending")

    req = e2e.rows("SELECT id, status, price_cents, down_payment_cents, months FROM machine_deal_requests")
    assert req == [{"id": req[0]["id"], "status": "pending", "price_cents": 2_400_000,
                    "down_payment_cents": 0, "months": 6}]
    assert e2e.rows("SELECT status FROM machines WHERE id = ?", (mid,))[0]["status"] == "in_stock"
    assert e2e.rows("SELECT COUNT(*) AS n FROM machine_deals")[0]["n"] == 0
    card = [p for p in e2e.pushes if p["uid"] == e2e.ids["boss"] and "на одобрение" in p["text"]]
    assert card and "скидка 4%" in card[0]["text"]
    assert f"mdr_ok:{req[0]['id']}" in str(card[0].get("reply_markup"))

    boss = open_app(e2e.ids["boss"])
    go(boss, "stock")
    tab(boss, "machines")
    boss.wait_for_selector("#machine-decisions [data-mreq-approve]")
    text = boss.inner_text("#machine-decisions")
    assert "AA1234567" in text and "скидка 4%" in text and "6 месяцев" in text
    _shot(boss, "03-boss-decisions-list")
    boss.click("#machine-decisions [data-mreq-approve]")
    boss.wait_for_selector(".toast:has-text('Рассрочка одобрена')")

    assert e2e.rows("SELECT status FROM machines WHERE id = ?", (mid,))[0]["status"] == "on_credit"
    sched = e2e.rows("SELECT seq, amount_cents FROM machine_deal_payments ORDER BY seq")
    assert [s["seq"] for s in sched] == [1, 2, 3, 4, 5, 6]
    assert sum(s["amount_cents"] for s in sched) == 2_400_000
    assert e2e.rows("SELECT status, approval_mode, decided_by FROM machine_deal_requests") == [
        {"status": "approved", "approval_mode": "boss", "decided_by": e2e.ids["boss"]}]
    assert any(p["uid"] == e2e.ids["mgr"] and "одобрена" in p["text"] for p in e2e.pushes)

    boss.click(f'[data-machine="{mid}"]')
    boss.wait_for_selector("#content:has-text('Платёж 6')")
    assert boss.locator("[data-receipt-add]").count() == 1, "график живой — оплату можно внести"
    _no_toast(boss)
    _shot(boss, "04-boss-card-schedule-active")

    # Деньги по рассрочке вносит менеджер — со способом, как оплату заказа.
    mgr.reload()
    mgr.wait_for_selector("#bottom-nav .nav-item", state="attached")
    _open_machine(mgr, mid)
    mgr.wait_for_selector("[data-receipt-add]")
    mgr.click("[data-receipt-add]")
    mgr.wait_for_selector("#ms-f-amount")
    mgr.fill("#ms-f-amount", "6000")
    mgr.click('.seg-item[data-opt="card"]')
    _shot(mgr, "11-manager-receipt-method")
    mgr.click("#ms-submit")
    mgr.wait_for_selector(".toast:has-text('Оплата записана')")
    mgr.wait_for_selector("#content:has-text('на карту')")
    assert e2e.rows("SELECT r.amount_cents, m.method, r.received_by FROM machine_payment_receipts r "
                    "JOIN machine_receipt_methods m ON m.receipt_id = r.id") == [
        {"amount_cents": 600_000, "method": "card", "received_by": e2e.ids["mgr"]}]
    assert mgr.locator("[data-receipt-del]").count() == 0, "стирает деньги руководитель"
    _no_toast(mgr)
    _shot(mgr, "12-manager-receipts")


def test_sale_rework_resubmit_then_reject_keeps_machine(open_app, e2e):
    mid = _machine(e2e, "APPR-2", "JCB 3CX", status="in_stock", price_cents=4_000_000)
    mgr = open_app(e2e.ids["mgr"])
    _open_machine(mgr, mid)
    mgr.click('[data-mact="sale"]')
    mgr.wait_for_selector("#ms-f-buyer_name")
    sheet_fill(mgr, {"price": "30000", "buyer_name": "ООО Карьер"})
    mgr.click("#ms-submit")
    mgr.wait_for_selector(".toast:has-text('руководителю')")

    boss = open_app(e2e.ids["boss"])
    _open_machine(boss, mid)
    boss.wait_for_selector("[data-mreq-rework]")
    _shot(boss, "05-boss-card-pending-sale")
    boss.click("[data-mreq-rework]")
    boss.wait_for_selector("#ms-f-reason")
    boss.fill("#ms-f-reason", "Скидка 25% — много, максимум 10%")
    _shot(boss, "06-boss-rework-reason")
    boss.click("#ms-submit")
    boss.wait_for_selector(".toast:has-text('на доработку')")
    assert e2e.rows("SELECT status FROM machine_deal_requests")[0]["status"] == "rework"

    mgr.reload()
    mgr.wait_for_selector("#bottom-nav .nav-item", state="attached")
    _open_machine(mgr, mid)
    mgr.wait_for_selector("[data-mreq-resubmit]")
    assert "максимум 10%" in mgr.inner_text("#content")
    _shot(mgr, "07-manager-rework")
    mgr.click("[data-mreq-resubmit]")
    mgr.wait_for_selector("#ms-f-price")
    assert mgr.input_value("#ms-f-price") == "30000"
    mgr.fill("#ms-f-price", "36000")
    mgr.click("#ms-submit")
    mgr.wait_for_selector(".toast:has-text('снова у руководителя')")
    row = e2e.rows("SELECT status, price_cents, attempts FROM machine_deal_requests")[0]
    assert row == {"status": "pending", "price_cents": 3_600_000, "attempts": 2}

    boss.reload()
    boss.wait_for_selector("#bottom-nav .nav-item", state="attached")
    _open_machine(boss, mid)
    boss.wait_for_selector("[data-mreq-reject]")
    boss.click("[data-mreq-reject]")
    boss.wait_for_selector("#ms-f-reason")
    boss.fill("#ms-f-reason", "Клиент ушёл к конкурентам")
    boss.click("#ms-submit")
    boss.wait_for_selector(".toast:has-text('отклонена')")
    boss.wait_for_selector('[data-mact="sale"]')
    assert boss.locator("[data-mreq]").count() == 0
    assert e2e.rows("SELECT status FROM machines WHERE id = ?", (mid,))[0]["status"] == "in_stock"
    assert e2e.rows("SELECT status FROM machine_deal_requests")[0]["status"] == "rejected"
    assert e2e.rows("SELECT COUNT(*) AS n FROM machine_deals")[0]["n"] == 0


def test_without_boss_manager_approves_booking_with_explicit_note(open_app, e2e):
    for who in ("boss", "admin"):
        e2e.run(e2e.db.deactivate_user(e2e.ids[who], e2e.ids["admin"]))
    import services.roles as roles

    roles.invalidate_all_roles()
    mid = _machine(e2e, "APPR-3", "Komatsu PC200", status="in_stock", price_cents=3_000_000)
    mgr = open_app(e2e.ids["mgr"])
    _open_machine(mgr, mid)
    mgr.click('[data-mact="reserve"]')
    mgr.wait_for_selector("#ms-f-buyer_name")
    assert mgr.locator("#ms-f-buyer_passport").count() == 0, "у брони паспорт не спрашиваем"
    sheet_fill(mgr, {"buyer_name": "ИП Каримов", "buyer_phone": "+998907778899"})
    _shot(mgr, "08-manager-booking-form")
    mgr.click("#ms-submit")
    mgr.wait_for_selector("[data-mreq-approve]")
    assert "руководителя в системе нет" in mgr.inner_text("#content")
    _no_toast(mgr)
    _shot(mgr, "09-manager-no-boss-decide")
    mgr.click("[data-mreq-approve]")
    mgr.wait_for_selector(".toast:has-text('руководителя нет')")
    assert e2e.rows("SELECT status FROM machines WHERE id = ?", (mid,))[0]["status"] == "reserved"
    assert e2e.rows("SELECT approval_mode FROM machine_deal_requests") == [{"approval_mode": "no_boss"}]

    # Клиент передумал — менеджер снимает свою бронь сам.
    mgr.wait_for_selector('[data-mact="unreserve"]')
    _no_toast(mgr)
    _shot(mgr, "13-manager-unreserve")
    mgr.click('[data-mact="unreserve"]')
    mgr.wait_for_selector(".toast:has-text('Бронь снята')")
    mgr.wait_for_selector('[data-mact="reserve"]')
    assert e2e.rows("SELECT status FROM machines WHERE id = ?", (mid,))[0]["status"] == "in_stock"
    assert e2e.rows("SELECT status FROM machine_deal_requests") == [{"status": "released"}]


def test_boss_toggles_delete_setting_and_manager_loses_delete(open_app, e2e):
    mid = _machine(e2e, "DEL-1", "Volvo EC210", status="in_stock")
    boss = open_app(e2e.ids["boss"])
    go(boss, "stock")
    tab(boss, "machines")
    boss.wait_for_selector("#delete-setting-toggle")
    boss.click("#delete-setting-toggle")
    boss.wait_for_selector(".toast:has-text('только руководитель')")
    boss.wait_for_selector("#content:has-text('Удаляет только руководитель')")
    _shot(boss, "10-boss-delete-setting")
    assert e2e.rows("SELECT value FROM app_settings WHERE key = 'delete_requires_boss'") == [{"value": "true"}]

    mgr = open_app(e2e.ids["mgr"])
    _open_machine(mgr, mid)
    assert mgr.locator('[data-mact="delete"]').count() == 0
    boss.click(f'[data-machine="{mid}"]')
    boss.wait_for_selector('[data-mact="delete"]')
