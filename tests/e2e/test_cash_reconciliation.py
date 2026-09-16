"""
E2E «Сверка кассы»: менеджер пересчитывает наличные, видит совпадение или
расхождение, записывает — руководитель находит запись в общей истории.

Сценарий владельца целиком: оплата наличными по заказу кладёт деньги менеджеру
на руки, вечером он их пересчитывает. Сошлось — «расхождений нет» и строка всё
равно пишется; не сошлось — точная сумма недостачи, примечание и запись,
которую видит руководитель. Ни платежей, ни сдач сверка при этом не создаёт.
"""

from __future__ import annotations

import pytest

from tests.e2e.conftest import go, pay_order, seed_order, tab, toast_text

pytestmark = pytest.mark.usefixtures("boss_work_actions")


def _open_reconcile(page):
    go(page, "money")
    tab(page, "reconcile")
    # Форма рисуется после двух запросов (контекст + история) — ждём её, а не
    # `settled`: спиннер сверки это `.spinner-wrap`, его тот не видит.
    page.wait_for_selector("#recon-submit")
    page.wait_for_selector("#recon-history")


def _fill(page, currency: str, value: str) -> None:
    page.fill(f'[data-recon-cur="{currency}"] .recon-amount', value)


def _diff_text(page, currency: str) -> str:
    return " ".join(page.text_content(f'[data-recon-diff="{currency}"]').split())


def test_manager_counts_cash_sees_match_and_records_it(open_app, e2e):
    """Совпало — «записан пересчёт, расхождений нет», и строка всё равно есть."""
    order = seed_order(e2e, payment_type="paid", qty=2, price=100.0, pay=None)
    pay_order(e2e, order["order_id"], [("cash", 200.0)])

    mgr = open_app(e2e.ids["mgr"])
    _open_reconcile(mgr)
    # «По системе» стоит рядом с полем ввода — сверяют два числа, а не память.
    assert "200" in " ".join(mgr.text_content('[data-recon-cur="USD"]').split())

    _fill(mgr, "USD", "200")
    assert _diff_text(mgr, "USD") == "сходится"
    mgr.click("#recon-submit")
    mgr.wait_for_function("() => document.querySelectorAll('.toast').length > 0")
    assert "расхождений нет" in toast_text(mgr)

    rows = e2e.rows("SELECT currency, counted_cents, system_cents, diff_cents, counted_by "
                    "FROM daily_cash_counts")
    assert rows == [{"currency": "USD", "counted_cents": 20000, "system_cents": 20000,
                     "diff_cents": 0, "counted_by": e2e.ids["mgr"]}]
    # Сверка — наблюдение: новых денег после неё не появилось.
    assert e2e.rows("SELECT COUNT(*) AS n FROM cash_deposits")[0]["n"] == 0
    assert e2e.rows("SELECT COUNT(*) AS n FROM payments")[0]["n"] == 1


def test_mismatch_is_flagged_with_note_and_visible_to_boss(open_app, e2e):
    """Недостача названа точной суммой, примечание — пояснение, а не исправление."""
    order = seed_order(e2e, payment_type="paid", qty=2, price=100.0, pay=None)
    pay_order(e2e, order["order_id"], [("cash", 200.0)])

    mgr = open_app(e2e.ids["mgr"])
    _open_reconcile(mgr)
    _fill(mgr, "USD", "150")
    assert _diff_text(mgr, "USD") == "не хватает 50 USD"
    assert "recon-warn" in mgr.get_attribute('[data-recon-diff="USD"]', "class")
    mgr.fill("#recon-note", "забыл занести оплату вчера")
    mgr.click("#recon-submit")
    mgr.wait_for_function("() => document.querySelectorAll('.toast').length > 0")
    assert "не хватает" in toast_text(mgr)

    row = e2e.rows("SELECT diff_cents, note FROM daily_cash_counts")[0]
    assert row["diff_cents"] == -5000
    assert row["note"] == "забыл занести оплату вчера"
    # Примечание НЕ создаёт платёж: объяснить недостачу текстом нельзя.
    assert e2e.rows("SELECT COUNT(*) AS n FROM payments")[0]["n"] == 1

    # Руководитель видит чужой пересчёт и умеет отфильтровать расхождения.
    boss = open_app(e2e.ids["boss"])
    _open_reconcile(boss)
    text = " ".join(boss.text_content("#recon-history").split())
    assert "не хватает 50 USD" in text
    assert "забыл занести оплату вчера" in text
    boss.click('[data-recon-filter="diff"]')
    boss.wait_for_selector('[data-recon-filter="diff"].active')
    boss.wait_for_selector("#recon-history")
    assert "не хватает 50 USD" in " ".join(boss.text_content("#recon-history").split())


def test_cash_absent_from_system_shows_up_as_surplus(open_app, e2e):
    """Оплату не занесли вовсе: по системе ноль, в кармане деньги — сверка это
    и ловит (сегодня такое расхождение не видно нигде)."""
    mgr = open_app(e2e.ids["mgr"])
    _open_reconcile(mgr)
    _fill(mgr, "USD", "300")
    assert _diff_text(mgr, "USD") == "лишние 300 USD"
    mgr.click("#recon-submit")
    mgr.wait_for_function("() => document.querySelectorAll('.toast').length > 0")
    assert e2e.rows("SELECT system_cents, diff_cents FROM daily_cash_counts") == [
        {"system_cents": 0, "diff_cents": 30000}
    ]
