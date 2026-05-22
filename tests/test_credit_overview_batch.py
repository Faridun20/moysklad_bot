"""
get_credit_overview переведён на батч-выборку (без N+1). Главная страховка:
посчитанный долг ДОЛЖЕН совпадать с get_agent_current_debt (неизменённый
одиночный путь) для каждого агента — два разных кода считают одно число.
Плюс точные значения на известном сценарии (клампинг total−confirmed−returns)
и поведение лимитов/сортировки. БД настоящая (isolated_db).
"""

import pytest


def _order(db, agent, total, qty=1, status="shipped", mgr=900):
    db.set_role(mgr, "mgr", "Manager", "manager")
    oid = db.create_order(mgr, "Manager", "")
    db.update_order_agent(oid, agent, f"Client {agent}")
    db.add_order_item(oid, "Товар", "", qty, "шт", total / qty)
    db.update_order_status(oid, status)
    return oid


def _confirm_payment(db, oid, amount, mgr=900):
    pid = db.add_payment(mgr, "mgr", "Manager", amount, "USD", "", order_id=oid)
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(db.q("UPDATE payments SET status = 'confirmed' WHERE id = ?"), (pid,))
        conn.commit()


def _confirmed_return(db, oid, amount, mgr=900):
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(
            db.q(
                "INSERT INTO returns (order_id, return_type, reason, total_amount, "
                "created_by, status, created_at) VALUES (?, ?, ?, ?, ?, 'confirmed', ?)"
            ),
            (oid, "partial", "брак", amount, mgr, db.now_str()),
        )
        conn.commit()


def test_overview_debt_matches_single_agent_path(isolated_db):
    """Долг из get_credit_overview == get_agent_current_debt для каждого агента
    (батч-путь тождественен одиночному)."""
    db = isolated_db

    # A-1: два открытых заказа, частичная оплата одного.
    _order(db, "A-1", 300.0, qty=3)
    o2 = _order(db, "A-1", 200.0)
    _confirm_payment(db, o2, 50.0)  # remaining 150

    # A-2: возврат снижает долг; есть paid/draft заказы (не считаются).
    o3 = _order(db, "A-2", 100.0)
    _confirmed_return(db, o3, 40.0)  # debt 60
    _order(db, "A-2", 999.0, status="paid")
    _order(db, "A-2", 999.0, status="draft")

    # A-4: переплата (debt 0) + pending-заказ (считается).
    o6 = _order(db, "A-4", 100.0)
    _confirm_payment(db, o6, 150.0)  # remaining 0
    _order(db, "A-4", 80.0, status="pending")

    # A-3: только строка лимита, заказов нет → долг 0, но в сводке присутствует.
    db.set_credit_limit("A-3", "VIP", 5000.0, set_by=1)

    overview = db.get_credit_overview()
    by_agent = {a["agent_id"]: a for a in overview}

    # Кросс-проверка обоих путей.
    for a in overview:
        assert a["debt"] == pytest.approx(
            db.get_agent_current_debt(a["agent_id"])
        ), f"расхождение долга у {a['agent_id']}"

    # Точные числа.
    assert by_agent["A-1"]["debt"] == pytest.approx(450.0)  # 300 + 150
    assert by_agent["A-2"]["debt"] == pytest.approx(60.0)  # 100 − 40
    assert by_agent["A-4"]["debt"] == pytest.approx(80.0)  # 0 + 80
    assert "A-3" in by_agent and by_agent["A-3"]["debt"] == pytest.approx(0.0)


def test_overview_limits_free_and_sort(isolated_db):
    """limit берётся из credit_limits (иначе дефолт), free = limit − debt,
    over_limit и сортировка по возрастанию free."""
    db = isolated_db
    o = _order(db, "A-OVER", 100.0)
    _ = o
    db.set_credit_limit("A-OVER", "Client", 50.0, set_by=1)  # лимит < долга
    _order(db, "A-UNDER", 100.0)  # дефолтный лимит 2000

    overview = db.get_credit_overview()
    by_agent = {a["agent_id"]: a for a in overview}

    over = by_agent["A-OVER"]
    assert over["limit"] == pytest.approx(50.0)
    assert over["debt"] == pytest.approx(100.0)
    assert over["free"] == pytest.approx(-50.0)
    assert over["over_limit"] is True

    under = by_agent["A-UNDER"]
    assert under["limit"] == pytest.approx(2000.0)  # credit_limit_default
    assert under["over_limit"] is False

    # Сортировка: меньший free (A-OVER, -50) идёт раньше.
    assert overview[0]["agent_id"] == "A-OVER"
