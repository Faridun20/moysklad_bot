"""E2E, вторая волна: продажи и деньги.

Всё, что после «заявка одобрена»: превышение лимита, отказ и доработка,
отметка оплаты и её подтверждение, сдача наличных, возврат, экран долгов,
очередь «Сегодня», отчёты. Продажа как таковая уже покрыта первой волной —
здесь она заводится через сервисы (`seed_order`), чтобы каждый сценарий
проверял СВОЙ шаг, а не платил полминуты браузера за чужой.
"""

from __future__ import annotations

import asyncio
import time

from tests.e2e.conftest import go, pay_form, seed_order, settled, tab, open_confirmations

import pytest

# Руководитель здесь делает работу менеджера — с «Рабочими действиями»
# (conftest.boss_work_actions). Вид по умолчанию — test_boss_ui.py.
pytestmark = pytest.mark.usefixtures("boss_work_actions")


# ─── Кредитный лимит: одобрение с превышением ────────────────────────────────


def test_over_limit_request_is_approved_only_with_override(open_app, e2e):
    """Заказ в долг больше лимита: босс видит цифры и подтверждает превышение.

    Сервер на такую заявку отвечает не ошибкой, а `needs_override` — это
    просьба подтвердить. Фронт обязан ПОКАЗАТЬ превышение и повторить запрос
    с override=true, а не сообщить «одобрена», оставив заявку висеть.
    """
    from services.database import set_credit_limit

    ids = e2e.ids
    cp = e2e.rows("SELECT id FROM counterparties")[0]["id"]
    e2e.run(set_credit_limit(str(cp), "ООО Ромашка", 100.0, set_by=ids["boss"]))

    mgr = open_app(ids["mgr"])
    go(mgr, "sales")
    mgr.click("#btn-new-order")
    mgr.click("#choose-agent")
    mgr.click('.agent-row:has-text("Ромашка")')
    mgr.wait_for_selector("#change-agent")
    mgr.click("#btn-add-product")
    mgr.click(f'.prod-row[data-product="{ids["product"]}"]')
    mgr.fill("#qty-input", "3")
    mgr.fill("#price-input", "100")
    mgr.evaluate("window.__tgMainClick()")
    mgr.wait_for_selector("#btn-submit:not([disabled])")
    mgr.click('[data-pay="credit"]')
    mgr.wait_for_selector("#due-date-wrap:not(.hidden)")
    mgr.fill("#due-date-input", "2030-01-15")
    mgr.click("#btn-submit")
    mgr.wait_for_function("() => window.__tgAlerts.some(a => a.includes('отправлена'))")
    order_id = e2e.rows("SELECT order_id FROM shipment_requests")[0]["order_id"]

    boss = open_app(ids["boss"])
    go(boss, "sales")
    boss.click("#show-requests")
    boss.wait_for_selector(".btn-approve")
    boss.click(".btn-approve")
    # Босс обязан увидеть, что лимит превышен, и подтвердить.
    boss.wait_for_function("() => window.__tgAlerts.some(a => /лимит/i.test(a))")
    boss.wait_for_function("() => window.__tgAlerts.some(a => a.includes('одобрена'))")

    order = e2e.rows(
        "SELECT status, credit_limit_override FROM orders WHERE id = ?", (order_id,)
    )[0]
    assert order["status"] == "approved"
    assert order["credit_limit_override"], "превышение зафиксировано на заказе"
    assert e2e.rows("SELECT status FROM shipment_requests")[0]["status"] == "approved"
    # Заявка ушла из списка ожидающих.
    assert boss.locator(".btn-approve").count() == 0


# ─── Отказ и доработка ───────────────────────────────────────────────────────


def test_boss_rejects_request(open_app, e2e):
    seeded = seed_order(e2e, payment_type="paid", due_date=None, approve=False)
    boss = open_app(e2e.ids["boss"])
    go(boss, "sales")
    boss.click("#show-requests")
    boss.wait_for_selector(".btn-reject")
    boss.click(".btn-reject")  # showConfirm в заглушке отвечает «да»
    boss.wait_for_function("() => window.__tgAlerts.some(a => a.includes('отклонена'))")

    assert e2e.rows("SELECT status FROM shipment_requests")[0]["status"] == "rejected"
    assert e2e.rows("SELECT status FROM orders WHERE id = ?", (seeded["order_id"],))[0]["status"] == "rejected"
    # Склад не тронут.
    assert e2e.rows("SELECT quantity FROM stock WHERE product_id = ?", (e2e.ids["product"],))[0]["quantity"] == 20
    assert e2e.rows("SELECT COUNT(*) AS n FROM order_shipment")[0]["n"] == 0


def test_boss_returns_request_to_manager_with_comment(open_app, e2e):
    """«На доработку»: заявка возвращается менеджеру в черновик с комментарием."""
    seeded = seed_order(e2e, payment_type="paid", due_date=None, approve=False)
    boss = open_app(e2e.ids["boss"])
    go(boss, "sales")
    boss.click("#show-requests")
    boss.wait_for_selector(".btn-draft")
    boss.click(".btn-draft")
    boss.fill(".draft-box .draft-comment", "Проверьте цену")
    boss.click(".draft-send")
    boss.wait_for_function("() => window.__tgAlerts.some(a => a.includes('доработку'))")

    order = e2e.rows("SELECT status FROM orders WHERE id = ?", (seeded["order_id"],))[0]
    assert order["status"] == "draft"
    # Менеджеру ушло уведомление с комментарием босса (любой из двух каналов).
    sent = [p["text"] for p in e2e.pushes] + [m["text"] for m in e2e.bot.messages]
    assert any("Проверьте цену" in t for t in sent), sent

    # У менеджера заказ снова редактируемый.
    mgr = open_app(e2e.ids["mgr"])
    go(mgr, "sales")
    mgr.wait_for_selector(f'.order-card[data-id="{seeded["order_id"]}"]')
    card = mgr.locator(f'.order-card[data-id="{seeded["order_id"]}"]')
    assert card.locator(".btn-edit-order").count() == 1


# ─── Оплата: менеджер отмечает, босс подтверждает ────────────────────────────


def test_manager_marks_paid_and_boss_confirms(open_app, e2e):
    seeded = seed_order(e2e)  # кредит на 200 USD, отгружен
    oid = seeded["order_id"]

    mgr = open_app(e2e.ids["mgr"])
    go(mgr, "money")
    tab(mgr, "debts")
    mgr.wait_for_selector(f'.btn-pay-debt[data-id="{oid}"]')
    assert "Ромашка" in mgr.locator("#content").inner_text()
    pay_form(mgr, f'.btn-pay-debt[data-id="{oid}"]', [("card", "150")])
    mgr.wait_for_selector(".toast:has-text('записана')")

    pays = e2e.rows("SELECT amount_cents, status FROM payments WHERE order_id = ?", (oid,))
    assert pays == [{"amount_cents": 15000, "status": "pending"}]
    assert e2e.wait_for_push(lambda p: "оплат" in p["text"].lower()), "боссу ушёл пуш об оплате"

    # Кредитный долг босс подтверждает там же, где он виден, — в «Долгах».
    boss = open_app(e2e.ids["boss"])
    go(boss, "money")
    tab(boss, "debts")
    sel = f'.debt-awaiting .btn-confirm-pay[data-id="{oid}"]'
    boss.wait_for_selector(sel)
    boss.click(sel)  # showConfirm → «да»
    boss.wait_for_function("(s) => !document.querySelector(s)", arg=sel)

    pays = e2e.rows("SELECT amount_cents, status FROM payments WHERE order_id = ?", (oid,))
    assert pays == [{"amount_cents": 15000, "status": "confirmed"}]
    # Долг уменьшился, но не закрыт: остаток 50 остаётся в «Долгах».
    boss.wait_for_selector(".debt-card")
    text = boss.locator(".debt-card").first.inner_text()
    assert "Ромашка" in text and "50" in text


def test_paid_order_payment_is_confirmed_by_boss(open_app, e2e):
    """«Оплачено сразу»: менеджер внёс оплату картой (seed_order), босс подтверждает её в «Подтвердить»."""
    seeded = seed_order(e2e, payment_type="paid", due_date=None)
    oid = seeded["order_id"]
    pays = e2e.rows("SELECT amount_cents, status FROM payments WHERE order_id = ?", (oid,))
    assert pays == [{"amount_cents": 20000, "status": "pending"}]

    boss = open_app(e2e.ids["boss"])
    open_confirmations(boss)
    boss.wait_for_selector(f'.pay-confirm[data-id="{oid}"]')
    assert "Ромашка" in boss.locator(f'.debt-card[data-pay="{oid}"]').inner_text()
    boss.click(f'.pay-confirm[data-id="{oid}"]')  # showConfirm → «да»
    boss.wait_for_function(
        "(id) => !document.querySelector('.pay-confirm[data-id=\"' + id + '\"]')", arg=str(oid)
    )
    pays = e2e.rows("SELECT status FROM payments WHERE order_id = ?", (oid,))
    assert pays == [{"status": "confirmed"}]
    order = e2e.rows("SELECT paid_confirmed_at, status FROM orders WHERE id = ?", (oid,))[0]
    assert order["paid_confirmed_at"], "факт оплаты зафиксирован на заказе"


# ─── Сдача наличных: менеджер сдаёт, босс подтверждает, FIFO гасит заказ ─────


def test_keeper_marks_approved_order_shipped(open_app, e2e):
    """Одобрение списало товар, но «отгружен» ставит кладовщик, когда товар уехал."""
    seeded = seed_order(e2e, qty=1, price=80.0)
    oid = seeded["order_id"]
    keeper = open_app(e2e.ids["keeper"])
    go(keeper, "sales")
    keeper.wait_for_selector(f'.btn-ship-order[data-id="{oid}"]')
    keeper.click(f'.btn-ship-order[data-id="{oid}"]')  # showConfirm → «да»
    keeper.wait_for_function("() => window.__tgAlerts.some(a => a.startsWith('🚚'))")
    assert e2e.rows("SELECT status FROM orders WHERE id = ?", (oid,))[0]["status"] == "shipped"


def test_cash_deposit_is_confirmed_and_closes_debt_fifo(open_app, e2e):
    seeded = seed_order(e2e, qty=1, price=80.0)  # долг 80 USD
    oid = seeded["order_id"]
    # Сдача распределяется по ОТГРУЖЕННЫМ заказам менеджера.
    from services.database import mark_order_shipped

    assert e2e.run(mark_order_shipped(oid, e2e.ids["keeper"], "Keeper")).get("ok")

    mgr = open_app(e2e.ids["mgr"])
    go(mgr, "money")
    tab(mgr, "ops")
    mgr.wait_for_selector("#dep-amount")
    mgr.fill("#dep-amount", "80")
    mgr.click("#dep-create")
    mgr.wait_for_selector(".toast:has-text('Сдача #')")
    dep = e2e.rows("SELECT id, status, amount_cents FROM cash_deposits")[0]
    assert dep["status"] == "pending" and dep["amount_cents"] == 8000
    # Своя сдача видна менеджеру в «Мои сдачи».
    mgr.wait_for_selector(f".stock-row:has-text('#{dep['id']}')")

    boss = open_app(e2e.ids["boss"])
    open_confirmations(boss)
    boss.wait_for_selector(f'.debt-card[data-dep="{dep["id"]}"] .dep-confirm')
    boss.click(f'.debt-card[data-dep="{dep["id"]}"] .dep-confirm')
    boss.wait_for_selector(".toast:has-text('Сдача подтверждена')")

    assert e2e.rows("SELECT status FROM cash_deposits")[0]["status"] == "confirmed"
    alloc = e2e.rows("SELECT order_id, amount_allocated_cents FROM cash_deposit_orders")
    assert alloc == [{"order_id": oid, "amount_allocated_cents": 8000}]
    # Сдача закрывает заказ напрямую (payment_confirmed), без строки в payments.
    order = e2e.rows("SELECT payment_confirmed, status FROM orders WHERE id = ?", (oid,))[0]
    assert order["payment_confirmed"] and order["status"] == "paid", "заказ закрыт полностью"
    # И из «Долгов» босса он ушёл.
    go(boss, "money")
    tab(boss, "debts")
    settled(boss)
    assert boss.locator(f".debt-card:has-text('#{oid}')").count() == 0


def test_manual_payment_currency_is_picked_by_a_segment(open_app, e2e):
    """Валюта строки платежа — сегмент, а не нативный `<select>`.

    Системный список Telegram-WebView открывается во весь экран даже ради двух
    пунктов. Проверяем не разметку, а результат: выбранная валюта обязана
    доехать до записи в БД — выбор живёт в data-атрибуте, и без обработчика
    клика строка молча ушла бы с USD по умолчанию.
    """
    mgr = open_app(e2e.ids["mgr"])
    go(mgr, "money")
    tab(mgr, "ops")
    mgr.wait_for_selector(".pay-row-cur [data-cur-opt='UZS']")
    assert mgr.locator("#pay-rows select").count() == 0, "нативный select остался"

    mgr.fill(".pay-row-amount", "250000")
    mgr.click(".pay-row-cur [data-cur-opt='UZS']")
    mgr.wait_for_selector(".pay-row-cur [data-cur-opt='UZS'].active")
    mgr.fill("#pay-comment", "Аренда за май")
    mgr.click("#pay-submit")

    for _ in range(50):
        rows = e2e.rows("SELECT amount_cents, currency, comment FROM payments")
        if rows:
            break
        time.sleep(0.2)
    status = mgr.locator("#pay-status")
    assert status.count() == 0 or "❌" not in status.inner_text(), status.inner_text()
    assert rows == [{"amount_cents": 25000000, "currency": "UZS",
                     "comment": "Аренда за май"}]


# ─── Возврат: менеджер оформляет, босс принимает товар и подтверждает ────────


def test_return_flow_needs_goods_received_before_confirm(open_app, e2e):
    seeded = seed_order(e2e, payment_type="paid", due_date=None)
    oid = seeded["order_id"]
    # Возврат допускается по оплаченному заказу: платёж подтверждён боссом.
    from services.database import confirm_all_pending_payments_for_order

    e2e.run(confirm_all_pending_payments_for_order(oid, e2e.ids["boss"], "Boss"))

    mgr = open_app(e2e.ids["mgr"])
    go(mgr, "money")
    tab(mgr, "ops")
    mgr.wait_for_selector("#ret-order")
    mgr.fill("#ret-order", str(oid))
    mgr.fill("#ret-reason", "Брак партии")
    # Заказ оплачен — «в счёт долга» вычитать не из чего, сервер откажет.
    mgr.click('[data-refund="no_refund"]')
    mgr.click("#ret-create")  # позиции не подгружали → полный возврат
    mgr.wait_for_selector(".toast:has-text('Возврат #')")
    ret = e2e.rows("SELECT id, status, goods_received, return_type FROM returns")[0]
    assert ret["status"] == "pending" and not ret["goods_received"] and ret["return_type"] == "full"

    boss = open_app(e2e.ids["boss"])
    open_confirmations(boss)
    card = f'.debt-card[data-ret="{ret["id"]}"]'
    boss.wait_for_selector(card)
    # Пока товар не принят, «Подтвердить» выключена — подтвердить возврат
    # за товар, которого физически нет, нельзя.
    assert boss.locator(f"{card} .ret-confirm").is_disabled()
    boss.click(f"{card} .ret-goods")
    boss.wait_for_selector(".toast:has-text('принят')")
    boss.wait_for_selector(f"{card} .ret-confirm:not([disabled])")
    boss.click(f"{card} .ret-confirm")
    boss.wait_for_selector(".toast:has-text('Возврат подтверждён')")

    ret = e2e.rows("SELECT status, goods_received FROM returns")[0]
    assert ret["status"] == "confirmed" and ret["goods_received"]
    order = e2e.rows("SELECT status FROM orders WHERE id = ?", (oid,))[0]
    assert order["status"] == "returned"


# ─── Долги и «Сегодня» ───────────────────────────────────────────────────────


def test_debts_screen_shows_overdue_credit_order(open_app, e2e):
    seeded = seed_order(e2e)
    # Заявку с прошедшим сроком submit не принимает (и правильно); просрочку
    # делаем постфактум — так она и возникает в жизни.
    with e2e.db.get_conn() as conn:
        cur = e2e.db.get_cursor(conn)
        cur.execute(e2e.db.q("UPDATE orders SET due_date = ? WHERE id = ?"), ("2020-01-01", seeded["order_id"]))
        conn.commit()
    boss = open_app(e2e.ids["boss"])
    # «Сегодня» — очередь дел: просроченный долг стоит первым и ведёт в «Долги».
    go(boss, "today")
    boss.wait_for_selector('[data-queue="money:debts"]')
    first = boss.locator("[data-queue]").first
    assert first.get_attribute("data-queue") == "money:debts"
    assert "просрочен" in first.inner_text().lower()
    first.click()
    boss.wait_for_function(
        "() => document.querySelector('#bottom-nav .nav-item.active')?.dataset.screen === 'money'"
    )
    boss.wait_for_selector(".debt-card")
    card = boss.locator(".debt-card").first
    assert card.get_attribute("data-status") == "overdue"
    assert "Ромашка" in card.inner_text()
    assert f"#{seeded['order_id']}" in card.inner_text()


def test_today_queue_leads_boss_to_pending_requests(open_app, e2e):
    seed_order(e2e, payment_type="paid", due_date=None, approve=False)
    boss = open_app(e2e.ids["boss"])
    go(boss, "today")
    # Заявки руководителя — часть «Решений».
    boss.wait_for_selector('[data-queue="decisions"]')
    row = boss.locator('[data-queue="decisions"]')
    assert "1" in row.inner_text()
    row.click()
    boss.wait_for_selector(".btn-approve")


def test_manager_today_queue_has_no_boss_items(open_app, e2e):
    """Заявки ждут решения босса — менеджеру этот пункт не показывают."""
    seed_order(e2e, payment_type="paid", due_date=None, approve=False)
    mgr = open_app(e2e.ids["mgr"])
    go(mgr, "today")
    settled(mgr)
    assert mgr.locator('[data-queue="requests"]').count() == 0


# ─── Отчёты рисуются с живого сервера ────────────────────────────────────────


def test_sales_and_money_reports_render_for_boss(open_app, e2e):
    seed_order(e2e)
    boss = open_app(e2e.ids["boss"])
    go(boss, "sales")
    boss.wait_for_selector(".order-card")
    tab(boss, "report")
    settled(boss)
    boss.wait_for_selector("[data-period]")
    text = boss.locator("#content").inner_text()
    assert "Ошибка" not in text and "Нет доступа" not in text
    assert "Ромашка" in text or "Кабель" in text, "отчёт видит отгрузку"

    go(boss, "money")
    tab(boss, "report")
    settled(boss)
    text = boss.locator("#content").inner_text()
    assert "Ошибка" not in text and "Нет доступа" not in text


def test_report_cards_do_not_overlap(open_app, e2e):
    """Жалоба с площадки: карточки отчёта «налезают друг на друга».

    Проверяем не CSS, а наблюдаемое: реальные прямоугольники плиток в браузере
    не пересекаются и стоят сеткой 2×2 (две в ряд), с зазором между рядами.
    Скруглённые карточки вплотную дают «выемки» на стыке — это и видно глазом.
    """
    seed_order(e2e)
    boss = open_app(e2e.ids["boss"])
    go(boss, "sales")
    boss.wait_for_selector(".order-card")
    tab(boss, "report")
    settled(boss)
    boss.wait_for_selector(".stat-grid .stat")

    boxes = boss.eval_on_selector_all(
        ".stat-grid .stat",
        "els => els.map(e => { const r = e.getBoundingClientRect();"
        " return {x: r.x, y: r.y, w: r.width, h: r.height}; })",
    )
    assert len(boxes) >= 4, f"у руководства четыре показателя, получено {len(boxes)}"

    for i, a in enumerate(boxes):
        for b in boxes[i + 1:]:
            overlap_x = min(a["x"] + a["w"], b["x"] + b["w"]) - max(a["x"], b["x"])
            overlap_y = min(a["y"] + a["h"], b["y"] + b["h"]) - max(a["y"], b["y"])
            assert overlap_x <= 0 or overlap_y <= 0, f"плитки пересекаются: {a} и {b}"

    # Сетка именно 2×2: первые две плитки в одном ряду, третья — ниже.
    assert abs(boxes[0]["y"] - boxes[1]["y"]) < 1, "первые две плитки должны стоять в ряд"
    assert boxes[2]["y"] > boxes[0]["y"] + boxes[0]["h"] - 1, "третья плитка — новый ряд"
    assert boxes[2]["y"] - (boxes[0]["y"] + boxes[0]["h"]) >= 4, "между рядами нужен зазор"


def test_manager_sales_report_renders_own_scope(open_app, e2e):
    seed_order(e2e)
    mgr = open_app(e2e.ids["mgr"])
    go(mgr, "sales")
    mgr.wait_for_selector(".order-card")
    tab(mgr, "report")
    settled(mgr)
    mgr.wait_for_selector("[data-period]")
    text = mgr.locator("#content").inner_text()
    assert "Ошибка" not in text and "Нет доступа" not in text


# ─── Черновик: удаление ──────────────────────────────────────────────────────


def test_manager_deletes_own_draft(open_app, e2e):
    db, ids = e2e.db, e2e.ids
    oid = db.create_order(ids["mgr"], "Manager", "")
    mgr = open_app(ids["mgr"])
    go(mgr, "sales")
    mgr.wait_for_selector(f'.btn-delete-draft[data-id="{oid}"]')
    mgr.click(f'.btn-delete-draft[data-id="{oid}"]')
    mgr.wait_for_function(
        "(id) => !document.querySelector(`.order-card[data-id=\"${id}\"]`)", arg=oid
    )
    assert e2e.rows("SELECT COUNT(*) AS n FROM orders WHERE id = ?", (oid,))[0]["n"] == 0


# ─── Документы: расписка из формы → PDF в Telegram → печать ──────────────────


# Реквизиты подписанта печатаются в каждом виде расписки и обязательны.
_SIGNATORY = {
    "company_name": "ООО Ромашка", "company_tin": "123456789", "company_address": "Ташкент",
    "company_representative": "Петров Пётр", "company_position": "Директор",
    "company_position_uz": "Директор", "company_representative_gen": "директора Петрова Петра",
    "company_city": "Ташкент", "company_city_uz": "Тошкент",
}

# Поля прежней формы, которых бланк юриста не печатает.
_GONE_FIELDS = ("debtor_passport", "debtor_pinfl", "debtor_birth_date", "debtor_address", "debtor_phone",
                "currency", "penalty_rate", "grace_days", "witness_name", "company_representative",
                "payment_type")


def test_manager_creates_raspiska_and_prints_it(open_app, e2e, monkeypatch, tmp_path):
    """Расписку раньше было негде составить: движок был, входа не было.

    Границы подменены: LibreOffice пишет файл-заглушку, «принтер» отвечает
    «принято». Всё остальное настоящее: форма, ручки, запись, доставка в
    Telegram с кнопкой печати, кнопка печати в WebApp.
    """
    from pathlib import Path

    from services import documents, printing
    from services.printing import PrintResult

    monkeypatch.setenv("DOCUMENTS_DIR", str(tmp_path / "docs"))

    def _write(doc_type, context, out_dir):
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        p = out / f"{doc_type}.pdf"
        p.write_bytes(b"%PDF-1.4\n" + context["debtor_full_name"].encode())
        return p

    async def fake_render(doc_type, context, out_dir, template_override=None):
        return await asyncio.to_thread(_write, doc_type, context, out_dir)

    printed: list[str] = []

    async def fake_print(pdf_bytes, *, filename="", printer_name="", label=""):
        printed.append(label)
        return PrintResult(True, job="Canon-1")

    monkeypatch.setattr(documents, "render_pdf", fake_render)
    monkeypatch.setattr(printing, "is_available", lambda: True)
    monkeypatch.setattr(printing, "print_pdf_bytes", fake_print)
    for key, value in _SIGNATORY.items():
        e2e.db.set_setting(key, value, e2e.ids["boss"])

    mgr = open_app(e2e.ids["mgr"])
    go(mgr, "sales")
    tab(mgr, "docs")
    mgr.wait_for_selector("#doc-new")
    assert mgr.locator("#doc-company").count() == 0, "реквизиты правит только руководство"
    mgr.click("#doc-new")
    # Тип документа — сегмент, а не нативный `<select>`: значение держит
    # скрытое поле, которое читает общий сбор формы.
    mgr.click('[data-opt="raspiska_ru"]')
    mgr.wait_for_function(
        "() => document.querySelector('#ms-f-doc_type').value === 'raspiska_ru'"
    )
    mgr.fill("#ms-f-debtor_full_name", "Иванов Иван Иванович")
    mgr.fill("#ms-f-product_name", "Экскаватор JCB 3CX")
    mgr.fill("#ms-f-total_amount", "25000000")
    mgr.fill("#ms-f-term_months", "6")
    # Переключателя «Порядок оплаты» больше нет: рассрочку задаёт само число
    # платежей. Два поля противоречили друг другу — менеджер вписывал шесть
    # платежей, забывал переключить «Разовый», и расписка выходила с одной
    # строкой графика и остатком 0.
    assert mgr.locator("#ms-f-payment_type").count() == 0
    mgr.fill("#ms-f-installments_count", "6")
    mgr.click("#ms-submit")
    mgr.wait_for_selector(".toast:has-text('сформирован')")
    mgr.wait_for_selector("[data-doc-print]")

    docs = e2e.rows(
        "SELECT client_name, total_amount_cents, payment_type, installments_count "
        "FROM generated_documents"
    )
    assert docs == [{"client_name": "Иванов Иван Иванович", "total_amount_cents": 2_500_000_000,
                     "payment_type": "installment", "installments_count": 6}]
    # PDF ушёл составителю с кнопкой «Распечатать» (prn:doc:<id>).
    assert [d["chat_id"] for d in e2e.bot.documents] == [e2e.ids["mgr"]]
    markup = e2e.bot.documents[0].get("reply_markup")
    assert markup and "prn:doc:" in str(markup.inline_keyboard[0][0].callback_data)

    mgr.click("[data-doc-print]")
    mgr.wait_for_selector(".toast:has-text('Отправлено на печать')")
    assert printed and "Иванов" in printed[0]

    # Ошибка формы остаётся В форме, а не закрывает её.
    mgr.click("#doc-new")
    mgr.click('[data-opt="raspiska_ru"]')
    mgr.fill("#ms-f-debtor_full_name", "Петров")
    mgr.fill("#ms-f-product_name", "Ковш")
    mgr.fill("#ms-f-total_amount", "100")
    mgr.fill("#ms-f-term_months", "2")
    mgr.fill("#ms-f-installments_count", "5")
    mgr.click("#ms-submit")
    mgr.wait_for_selector("#ms-error:not([hidden])")
    assert "позже срока" in mgr.locator("#ms-error").inner_text()
    assert mgr.locator("#ms-f-debtor_full_name").input_value() == "Петров"


def test_boss_prints_invoice_from_list(open_app, e2e, monkeypatch):
    from services import printing
    from services.printing import PrintResult

    printed: list[str] = []

    async def fake_print(pdf_bytes, *, filename="", printer_name="", label=""):
        printed.append(label)
        return PrintResult(True, job="12")

    monkeypatch.setattr(printing, "is_available", lambda: True)
    monkeypatch.setattr(printing, "print_pdf_bytes", fake_print)

    boss = open_app(e2e.ids["boss"])
    go(boss, "stock")
    tab(boss, "invoices")
    boss.wait_for_selector("[data-wh-print]")  # сид-приход на 20 шт.
    boss.click("[data-wh-print]")
    boss.wait_for_selector(".toast:has-text('задание 12')")
    assert printed and "Накладная" in printed[0]


def test_invoice_list_has_no_print_button_without_cups(open_app, e2e, monkeypatch):
    from services import printing

    monkeypatch.setattr(printing, "is_available", lambda: False)
    boss = open_app(e2e.ids["boss"])
    go(boss, "stock")
    tab(boss, "invoices")
    boss.wait_for_selector("[data-wh-cancel]")
    assert boss.locator("[data-wh-print]").count() == 0


def test_receipt_form_offers_three_kinds_of_one_form(open_app, e2e, monkeypatch, tmp_path):
    """Три вида одного бланка юриста — RU+UZ (по умолчанию), рус., ўзб. Поля
    у них одинаковые и только те, что попадают в документ: паспорт, адрес и
    телефон Должник пишет от руки, пеня — в тексте бланка, валюта — сумы."""
    # PDF собирает LibreOffice, которого в CI нет: тест про форму, а не про
    # вёрстку (её проверяет tests/test_legal_docs.py там, где soffice есть).
    from pathlib import Path

    from services import documents

    def _write(doc_type, context, out_dir):
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        path = out / f"{doc_type}.pdf"
        path.write_bytes(b"%PDF-1.4\n")
        return path

    async def fake_render(doc_type, context, out_dir, template_override=None):
        return await asyncio.to_thread(_write, doc_type, context, out_dir)

    monkeypatch.setattr(documents, "render_pdf", fake_render)
    monkeypatch.setenv("DOCUMENTS_DIR", str(tmp_path / "docs"))
    for key, value in _SIGNATORY.items():
        e2e.db.set_setting(key, value, e2e.ids["boss"])

    mgr = open_app(e2e.ids["mgr"])
    go(mgr, "sales")
    tab(mgr, "docs")
    mgr.click("#doc-new")
    mgr.wait_for_selector("#ms-f-doc_type", state="attached")  # скрытое поле сегмента
    assert mgr.input_value("#ms-f-doc_type") == "raspiska_ru_uz", "RU+UZ — по умолчанию"
    assert mgr.locator("[data-opt]").evaluate_all("els => els.map(e => [e.dataset.opt, e.textContent])") == [
        ["raspiska_ru_uz", "Расписка RU+UZ"], ["raspiska_ru", "Расписка (рус.)"], ["tilxat_uz", "Тилхат (ўзб.)"],
    ]
    fields = mgr.locator(".c-overlay [id^='ms-f-']").evaluate_all("els => els.map(e => e.id.slice(5))")
    assert fields == ["doc_type", "debtor_full_name", "product_name", "total_amount", "start_date",
                      "term_months", "installments_count", "city"]
    assert not [k for k in _GONE_FIELDS if k in fields]
    assert mgr.locator("text=В документ не печатается").is_visible()
    assert mgr.locator("text=Сумма, сум").is_visible()

    # Переключение вида ничего в форме не прячет и не показывает.
    mgr.click('[data-opt="tilxat_uz"]')
    mgr.wait_for_function("() => document.querySelector('#ms-f-doc_type').value === 'tilxat_uz'")
    assert mgr.locator(".c-overlay [id^='ms-f-']").evaluate_all("els => els.map(e => e.id.slice(5))") == fields
    assert all(mgr.locator(f"#ms-f-{k}").is_visible() for k in fields if k != "doc_type")

    mgr.fill("#ms-f-debtor_full_name", "Иванов Иван Иванович")
    mgr.fill("#ms-f-product_name", "Экскаватор JCB 3CX")
    mgr.fill("#ms-f-total_amount", "300000000")
    mgr.fill("#ms-f-term_months", "12")
    mgr.fill("#ms-f-installments_count", "12")
    mgr.click("#ms-submit")
    mgr.wait_for_selector(".toast:has-text('сформирован')")

    docs = e2e.rows(
        "SELECT g.client_name, g.currency, g.total_amount_cents, g.installments_count, t.type "
        "FROM generated_documents g JOIN document_templates t ON t.id = g.template_id"
    )
    assert docs == [{"client_name": "Иванов Иван Иванович", "currency": "UZS",
                     "total_amount_cents": 30_000_000_000, "installments_count": 12, "type": "tilxat_uz"}]
