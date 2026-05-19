"""
Тесты жизненного цикла заказа: от черновика до полной оплаты.

Покрывают самый «дорогой» путь — там, где деньги. Любой коммит,
который трогает mark_order_paid / confirm_payment / mark_order_paid
race-fix / backfill — должен прогонять эти тесты до push.
"""


def test_order_creation_and_status_flow(isolated_db):
    db = isolated_db
    db.set_role(1, "mgr", "Manager", "manager")
    oid = db.create_order(1, "Manager", "test")
    o = db.get_order(oid)
    assert o["status"] == "draft"
    db.update_order_status(oid, "approved")
    assert db.get_order(oid)["status"] == "approved"


def test_default_role_for_new_user_is_guest(isolated_db, monkeypatch):
    """C1 fix: при пустом ALLOWED_USERS и LEGACY_OPEN_BOT default = guest."""
    monkeypatch.setenv("ALLOWED_USERS", "")
    monkeypatch.delenv("LEGACY_OPEN_BOT", raising=False)
    import importlib
    import config
    importlib.reload(config)
    db = isolated_db
    db.ensure_user(99, "newbie", "New Bie", admin_ids=[])
    assert db.get_role(99) == "guest"


def test_legacy_open_bot_falls_back_to_manager(isolated_db, monkeypatch):
    """C1 fix: LEGACY_OPEN_BOT=1 явно включает старое поведение."""
    monkeypatch.setenv("ALLOWED_USERS", "")
    monkeypatch.setenv("LEGACY_OPEN_BOT", "1")
    import importlib
    import config
    importlib.reload(config)
    db = isolated_db
    db.ensure_user(88, "legacy", "Legacy", admin_ids=[])
    assert db.get_role(88) == "manager"


def test_set_role_validates_whitelist(isolated_db):
    """C2 fix: 'employee' больше не принимается (нет в whitelist)."""
    db = isolated_db
    assert db.set_role(1, "x", "X", "manager") is True
    assert db.set_role(1, "x", "X", "boss") is True
    assert db.set_role(1, "x", "X", "admin") is True
    assert db.set_role(1, "x", "X", "guest") is True
    # Невалидные роли
    assert db.set_role(1, "x", "X", "employee") is False
    assert db.set_role(1, "x", "X", "nonsense") is False


def _setup_credit_order(db, total_per_unit=100, qty=4):
    """Helper: создать credit-заказ на total = qty * total_per_unit USD."""
    db.set_role(1, "mgr", "Manager", "manager")
    db.set_role(99, "boss", "Boss", "boss")
    oid = db.create_order(1, "Manager", "")
    db.update_order_agent(oid, "agent-uuid", "Client X")
    db.add_order_item(oid, "Product", "", qty, "шт", float(total_per_unit))
    db.set_order_payment(oid, "credit", "2026-12-31")
    db.update_order_status(oid, "approved")
    return oid


def test_full_payment_closes_order(isolated_db):
    """Менеджер отметил → босс подтвердил → заказ закрыт."""
    db = isolated_db
    oid = _setup_credit_order(db, 100, 4)  # total 400
    ok, pid = db.mark_order_paid(oid, 1, "Manager", amount=400)
    assert ok and pid
    # paid_at стоит, paid_confirmed_at — нет
    o = db.get_order(oid)
    assert o["paid_at"] is not None
    assert o["paid_confirmed_at"] is None
    # Босс подтверждает
    db.confirm_payment(pid, 99, "Boss")
    o = db.get_order(oid)
    assert o["paid_confirmed_at"] is not None


def test_partial_payments_close_when_sum_reaches_total(isolated_db):
    """Два частичных платежа 100 + 300 = 400 закрывают заказ."""
    db = isolated_db
    oid = _setup_credit_order(db, 100, 4)
    _, p1 = db.mark_order_paid(oid, 1, "Manager", amount=100)
    db.confirm_payment(p1, 99, "Boss")
    # После первого confirm заказ ещё открыт
    assert db.get_order(oid)["paid_confirmed_at"] is None
    _, p2 = db.mark_order_paid(oid, 1, "Manager", amount=300)
    db.confirm_payment(p2, 99, "Boss")
    # После второго закрыт
    assert db.get_order(oid)["paid_confirmed_at"] is not None


def test_overpay_is_capped_at_remaining(isolated_db):
    """H1 fix: попытка отметить больше остатка → сумма обрезается."""
    db = isolated_db
    oid = _setup_credit_order(db, 100, 4)  # 400 total
    _, p1 = db.mark_order_paid(oid, 1, "Manager", amount=300)
    # Пытаемся отметить ещё 200 — должно обрезаться до 100 (остаток)
    ok, p2 = db.mark_order_paid(oid, 1, "Manager", amount=200)
    assert ok
    payment2 = db.get_payment(p2)
    assert abs(float(payment2["amount"]) - 100) < 0.01


def test_marking_after_full_remaining_is_zero_returns_false(isolated_db):
    """Третий mark когда уже всё застолбили — отлетает."""
    db = isolated_db
    oid = _setup_credit_order(db, 100, 4)
    db.mark_order_paid(oid, 1, "Manager", amount=400)
    # Остаток 0
    ok, pid = db.mark_order_paid(oid, 1, "Manager", amount=50)
    assert ok is False
    assert pid is None


def test_confirm_idempotent_on_already_confirmed(isolated_db):
    """Повторный confirm того же payment_id ничего не меняет."""
    db = isolated_db
    oid = _setup_credit_order(db, 100, 1)
    _, pid = db.mark_order_paid(oid, 1, "Manager", amount=100)
    first = db.confirm_payment(pid, 99, "Boss")
    second = db.confirm_payment(pid, 99, "Boss")
    assert first is True
    assert second is False  # уже confirmed


def test_claim_payment_for_ms_sync_atomic(isolated_db):
    """H3 fix: claim возвращает True только тому, кто выиграл гонку."""
    db = isolated_db
    oid = _setup_credit_order(db, 100, 1)
    _, pid = db.mark_order_paid(oid, 1, "Manager", amount=100)
    db.confirm_payment(pid, 99, "Boss")
    # Платёж confirmed, ms_paymentin_id IS NULL → claim возможен
    assert db.claim_payment_for_ms_sync(pid) is True
    # Второй raise sync — уже in_progress
    assert db.claim_payment_for_ms_sync(pid) is False
    # После set_payment_ms_sync synced — тем более нет
    db.set_payment_ms_sync(pid, paymentin_id="ms-fake", status="synced")
    assert db.claim_payment_for_ms_sync(pid) is False


def test_backfill_does_not_close_partial_credit_debts(isolated_db, monkeypatch):
    """Регрессия по старому backfill-багу: run_backfills() не должен
    закрывать частично-оплаченные кредитные долги.

    После H4-фикса backfill вынесен из init_db в run_backfills() —
    отдельно вызываемая функция, идемпотентная по дизайну.
    """
    db = isolated_db
    oid = _setup_credit_order(db, 100, 4)  # 400 total
    # Частичная оплата 100 / 400
    _, pid = db.mark_order_paid(oid, 1, "Manager", amount=100)
    db.confirm_payment(pid, 99, "Boss")
    # Долг ещё открыт
    assert db.get_order(oid)["paid_confirmed_at"] is None
    # Прогон backfill+recovery — статус должен сохраниться
    db.run_backfills()
    assert db.get_order(oid)["paid_confirmed_at"] is None
