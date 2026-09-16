"""E2E: скидка к прайсу — видимость в «Решениях» и явное одобрение (C2/C5).

Сценарий: у товара есть прайс, заказ ушёл со скидкой 30% (порог 15%) →
менеджер видит на своей карточке, чем занята заявка → руководитель видит в
«Решениях» строку со скидкой и пометку порога → одобряет, подтвердив скидку
отдельным вопросом → заказ одобрен, решение в журнале.

Заказ заводится СЕРВИСОМ (`seed_order`), а не через редактор: ручка
`/api/orders/add_item` держит прайс жёстким минимумом и позицию дешевле него
просто не примет — на живой базе такая скидка приходит из другого места
(прайс подняли после заказа, позиция приехала миграцией). Проверяем здесь
именно показ и решение, а не ввод цены.
"""

from __future__ import annotations

from tests.e2e.conftest import go, seed_order, settled


def _set_price(e2e, sale: float) -> None:
    from services.database import set_product_price

    ok, err = set_product_price(
        str(e2e.ids["product"]), "Кабель ВВГ 3x2.5", sale, None, "USD",
        updated_by=e2e.ids["boss"],
    )
    assert ok, err


def test_boss_sees_flagged_discount_in_decisions_and_approves(open_app, e2e):
    _set_price(e2e, 100.0)
    seeded = seed_order(e2e, payment_type="paid", qty=3, price=70.0, approve=False, pay=None)

    # Менеджеру видно, почему заявка стоит, и чем строка разошлась с прайсом.
    mgr = open_app(e2e.ids["mgr"])
    go(mgr, "sales")
    mgr.wait_for_selector(f'.order-card[data-id="{seeded["order_id"]}"]')
    settled(mgr)
    # Строки товаров со скидкой — в подробной сводке карточки.
    mgr.click(f'.order-card[data-id="{seeded["order_id"]}"] [data-details-toggle]')
    mgr_text = mgr.inner_text("#content")
    assert "Ждёт одобрения из-за скидки 30%" in mgr_text
    assert "прайс 100 USD · скидка 30%" in mgr_text, mgr_text

    # Руководитель: скидка видна строкой и помечена порогом.
    boss = open_app(e2e.ids["boss"])
    go(boss, "decisions")
    boss.wait_for_selector(".btn-approve")
    settled(boss)
    card = boss.locator(f'.order-card[data-request="{seeded["req_id"]}"]')
    text = card.inner_text()
    assert "прайс 100 USD · скидка 30%" in text, text
    assert "Скидка по заказу: скидка 30%" in text, text
    assert "нужно явное решение" in text, text
    assert "credit-ctx--bad" in card.inner_html()

    # Одобрение спрашивает про скидку отдельно (showConfirm в заглушке
    # отвечает «да» и складывает текст в window.__tgAlerts).
    card.locator(".btn-approve").click()
    boss.wait_for_function(
        "() => window.__tgAlerts.some(a => a.startsWith('confirm:') && a.includes('скидк'))"
    )
    boss.wait_for_function("() => window.__tgAlerts.some(a => a.includes('Заявка одобрена'))")

    assert e2e.rows(
        "SELECT status FROM orders WHERE id = ?", (seeded["order_id"],)
    )[0]["status"] == "approved"
    assert e2e.rows(
        "SELECT COUNT(*) AS n FROM audit_log WHERE action = 'discount_approved'"
    )[0]["n"] == 1


def test_discount_below_threshold_needs_no_second_tap(open_app, e2e):
    """Скидка 4% при пороге 15%: видна, но вопроса про скидку нет."""
    _set_price(e2e, 100.0)
    seeded = seed_order(e2e, payment_type="paid", qty=1, price=96.0, approve=False, pay=None)

    boss = open_app(e2e.ids["boss"])
    go(boss, "decisions")
    boss.wait_for_selector(".btn-approve")
    settled(boss)
    card = boss.locator(f'.order-card[data-request="{seeded["req_id"]}"]')
    assert "скидка 4%" in card.inner_text()
    assert "нужно явное решение" not in card.inner_text()

    card.locator(".btn-approve").click()
    boss.wait_for_function("() => window.__tgAlerts.some(a => a.includes('Заявка одобрена'))")
    assert not boss.evaluate(
        "() => window.__tgAlerts.some(a => a.startsWith('confirm:') && a.includes('скидк'))"
    )
    assert e2e.rows(
        "SELECT status FROM orders WHERE id = ?", (seeded["order_id"],)
    )[0]["status"] == "approved"
