"""E2E: менеджер временно делает работу кладовщика и бухгалтера.

Решение владельца: отдельных сотрудников пока нет, менеджер подтверждает сдачи,
принимает возвраты и отгружает, руководитель проверяет
(`services.roles.ROLE_ALSO_ACTS_AS`). Проверяем весь путь «сервер пускает И
фронт рисует кнопку»: одно без другого — либо 403 на нажатии, либо работа, до
которой нечем дотянуться. Отдельным файлом: при откате совмещения он удаляется
целиком, а сценарии кладовщика/бухгалтера в соседних файлах остаются.
"""

from __future__ import annotations

import pytest

from tests.e2e.conftest import go, seed_order, tab

pytestmark = pytest.mark.e2e


def test_manager_confirms_deposit_and_sees_it_in_today(open_app, e2e):
    """Руководителя и бухгалтера в системе нет (как на проде сейчас) — менеджер
    подтверждает сдачу сам."""
    import services.roles as roles

    seed_order(e2e, qty=1, price=30.0)
    from services.database import create_cash_deposit

    assert e2e.run(create_cash_deposit(e2e.ids["mgr"], 30.0)).get("ok")
    ids = e2e.ids
    e2e.exec("UPDATE user_roles SET role = 'guest' WHERE user_id IN (?, ?, ?)", (ids["boss"], ids["admin"], ids["book"]))
    roles.invalidate_all_roles()

    mgr = open_app(e2e.ids["mgr"])
    # «Сегодня»: пункт бухгалтера появился и у менеджера.
    row = mgr.locator('[data-queue="money:confirm"]:has-text("Сдачи наличных")')
    row.wait_for()
    row.click()
    mgr.wait_for_selector(".dep-confirm")
    # Руководские оплаты по заказам ему не показываем.
    assert mgr.locator(".pay-confirm").count() == 0
    mgr.click(".dep-confirm")
    # Итог действия — тостом (аудит фронта заменил блокирующие алерты).
    mgr.wait_for_selector(".toast:has-text('Сдача в кассу подтверждена')")
    assert e2e.rows("SELECT status, confirmed_by FROM cash_deposits")[0] == {
        "status": "confirmed", "confirmed_by": e2e.ids["mgr"],
    }


def test_manager_has_no_confirm_button_while_boss_is_active(open_app, e2e):
    """Руководитель активен — кнопки «Подтвердить» у менеджера нет, подсказка
    говорит, кто подтвердит (сервер всё равно ответил бы 403)."""
    seed_order(e2e, qty=1, price=30.0)
    from services.database import create_cash_deposit

    assert e2e.run(create_cash_deposit(e2e.ids["mgr"], 30.0)).get("ok")
    mgr = open_app(e2e.ids["mgr"])
    go(mgr, "money")
    tab(mgr, "confirm")
    mgr.wait_for_selector(".debt-card[data-dep]")
    assert mgr.locator(".dep-confirm").count() == 0
    assert "подтверждает руководитель или бухгалтер" in mgr.locator(".debt-card[data-dep]").inner_text()


def test_manager_marks_return_goods_received_but_boss_confirms(open_app, e2e):
    seeded = seed_order(e2e, payment_type="paid", due_date=None)
    from services.database import confirm_all_pending_payments_for_order, create_return

    e2e.run(confirm_all_pending_payments_for_order(seeded["order_id"], e2e.ids["boss"], "Boss"))
    item = e2e.rows("SELECT id FROM order_items WHERE order_id = ?", (seeded["order_id"],))[0]["id"]
    r = e2e.run(create_return(seeded["order_id"], "full", "Брак", [(item, 2, 200.0)],
                              "no_refund", e2e.ids["mgr"]))  # оплаченный заказ: «в счёт долга» вычитать не из чего (_debt_reduction_refusal)
    assert r.get("ok"), r

    mgr = open_app(e2e.ids["mgr"])
    go(mgr, "money")
    tab(mgr, "confirm")
    mgr.wait_for_selector(".ret-goods")
    mgr.click(".ret-goods")
    mgr.wait_for_selector(".toast:has-text('принят')")
    assert e2e.rows("SELECT goods_received, status FROM returns")[0] == {
        "goods_received": 1, "status": "pending",
    }


def test_manager_ships_approved_order(open_app, e2e):
    seeded = seed_order(e2e, qty=1, price=80.0)
    oid = seeded["order_id"]
    mgr = open_app(e2e.ids["mgr"])
    go(mgr, "sales")
    mgr.wait_for_selector(f'.btn-ship-order[data-id="{oid}"]')
    mgr.click(f'.btn-ship-order[data-id="{oid}"]')  # showConfirm → «да»
    mgr.wait_for_function("() => window.__tgAlerts.some(a => /Заказ #\\d+ отгружен/.test(a))")
    assert e2e.rows("SELECT status, shipped_by FROM orders WHERE id = ?", (oid,))[0] == {
        "status": "shipped", "shipped_by": e2e.ids["mgr"],
    }
