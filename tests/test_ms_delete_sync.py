"""
МС-delete синхронизация (#34/#35): вебхук customerorder.DELETE отменяет
approved-заказ локально; cron-реконсиляция ловит пропущенные вебхуки.

Реальная БД (isolated_db); МС (ms_get) и Telegram (notifier) — мок.
"""

import asyncio

import services.ms_sync_handler as h
import services.notifier as notifier


def _mock_notify(monkeypatch):
    sent = []

    async def _recips():
        return [99]

    async def _send(uid, text, **k):
        sent.append((uid, text))

    monkeypatch.setattr(notifier, "aget_notify_recipients", _recips)
    monkeypatch.setattr(notifier, "tg_send_message", _send)
    return sent


def _mk_order(db, status, co_id, agent="Client"):
    db.set_role(1, "m", "M", "manager")
    oid = db.create_order(1, "M", "")
    db.update_order_agent(oid, "A-1", agent)
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(
            db.q("UPDATE orders SET status=?, ms_customerorder_id=? WHERE id=?"),
            (status, co_id, oid),
        )
        conn.commit()
    return oid


# ─── #34: вебхук-хендлер ─────────────────────────────────────────────────────


def test_co_delete_cancels_approved_order(isolated_db, monkeypatch):
    db = isolated_db
    sent = _mock_notify(monkeypatch)
    oid = _mk_order(db, "approved", "CO-1", agent="Client <b>")

    asyncio.run(h._handle_customerorder_deleted("CO-1"))

    o = asyncio.run(db.get_order(oid))
    assert o["status"] == "cancelled"
    assert o["ms_cancel_synced_at"] is not None  # reverse в МС не нужен
    assert o["ms_customerorder_id"] is None
    # Уведомление ушло, имя клиента экранировано.
    assert sent and sent[0][0] == 99
    assert "Client &lt;b&gt;" in sent[0][1]


def test_co_delete_keeps_shipped_order(isolated_db, monkeypatch):
    db = isolated_db
    _mock_notify(monkeypatch)
    oid = _mk_order(db, "shipped", "CO-2")

    asyncio.run(h._handle_customerorder_deleted("CO-2"))

    o = asyncio.run(db.get_order(oid))
    assert o["status"] == "shipped"  # деньги/остатки двигались — статус не трогаем
    assert o["ms_customerorder_id"] is None  # ссылка снята
    assert o["ms_deleted_at"] is not None  # но помечен удалённым в МС → вне аналитики


def test_co_delete_unknown_order_noop(isolated_db, monkeypatch):
    _mock_notify(monkeypatch)
    # Нет заказа с таким CO — хендлер просто выходит.
    asyncio.run(h._handle_customerorder_deleted("CO-NONE"))


# ─── Этап 2b: demand.DELETE ──────────────────────────────────────────────────


def test_demand_delete_marks_order_phantom(isolated_db, monkeypatch):
    """Бот-созданная отгрузка удалена в МС → заказ помечается ms_deleted_at,
    статус НЕ трогаем (деньги/остатки двигались). Идемпотентно."""
    db = isolated_db
    sent = _mock_notify(monkeypatch)
    db.set_role(1, "m", "M", "manager")
    oid = db.create_order(1, "M", "")
    db.update_order_agent(oid, "A-1", "Client")
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(
            db.q("UPDATE orders SET status='shipped', ms_demand_id=? WHERE id=?"),
            ("DM-1", oid),
        )
        conn.commit()

    asyncio.run(h._handle_demand_deleted("DM-1"))

    o = asyncio.run(db.get_order(oid))
    assert o["status"] == "shipped"          # статус не тронут
    assert o["ms_deleted_at"] is not None    # помечен фантомом
    assert sent and sent[0][0] == 99         # boss уведомлён

    # Идемпотентность: повторный вызов не шлёт второе уведомление.
    sent.clear()
    asyncio.run(h._handle_demand_deleted("DM-1"))
    assert sent == []


def test_demand_delete_unknown_order_noop(isolated_db, monkeypatch):
    _mock_notify(monkeypatch)
    asyncio.run(h._handle_demand_deleted("DM-NONE"))


# ─── Этап 2a: дрифт суммы customerorder.UPDATE ───────────────────────────────


def _mk_order_with_items(db, qty, price, status="approved", co_id="CO-D"):
    db.set_role(1, "m", "M", "manager")
    oid = db.create_order(1, "M", "")
    db.update_order_agent(oid, "A-1", "Client")
    db.add_order_item(oid, "P", "", qty, "шт", price)
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(
            db.q("UPDATE orders SET status=?, ms_customerorder_id=? WHERE id=?"),
            (status, co_id, oid),
        )
        conn.commit()
    return oid


def test_co_update_flags_sum_drift(isolated_db, monkeypatch):
    """Сумма в МС материально отличается от локальной → ms_drift_at + уведомление,
    статус/деньги не трогаем."""
    db = isolated_db
    sent = _mock_notify(monkeypatch)
    oid = _mk_order_with_items(db, qty=2, price=100.0)  # локально 200.00 = 20000 коп.

    async def _b(path):
        return {"sum": 30000, "state": {}}  # 300.00 в МС — дрифт

    _mock_ms_get(monkeypatch, _b)
    asyncio.run(h._handle_customerorder_updated("CO-D"))

    o = asyncio.run(db.get_order(oid))
    assert o["ms_drift_at"] is not None
    assert o["status"] == "approved"  # статус не тронут
    assert sent and "изменён в МойСклад" in sent[0][1]

    # Идемпотентно: повторный UPDATE не шлёт второе уведомление.
    sent.clear()
    asyncio.run(h._handle_customerorder_updated("CO-D"))
    assert sent == []


def test_co_update_no_drift_when_sum_matches(isolated_db, monkeypatch):
    """Сумма совпадает (в пределах допуска) → дрифт не флагуем."""
    db = isolated_db
    _mock_notify(monkeypatch)
    oid = _mk_order_with_items(db, qty=2, price=100.0)  # 20000 коп.

    async def _b(path):
        return {"sum": 20000, "state": {}}

    _mock_ms_get(monkeypatch, _b)
    asyncio.run(h._handle_customerorder_updated("CO-D"))

    assert asyncio.run(db.get_order(oid))["ms_drift_at"] is None


# ─── #35: cron-реконсиляция ──────────────────────────────────────────────────


def _mock_ms_get(monkeypatch, behaviour):
    """Точечный мок `ms_get` — для вебхук-хендлеров, которые читают ОДИН
    документ (`entity/customerorder/{id}`). Реконсиляция с MS-4 работает
    иначе, для неё есть `_mock_ms_list`."""
    import services.moysklad as ms

    calls = []

    async def _ms_get(path, params=None):
        calls.append(path)
        return await behaviour(path)

    monkeypatch.setattr(ms, "ms_get", _ms_get)
    return calls


def _mock_ms_list(monkeypatch, alive: set[str]):
    """Мок списочного запроса реконсиляции (MS-4).

    Реконсиляция больше не спрашивает документы по одному: она шлёт
    `entity/<type>?filter=id=…;id=…` и считает удалённым всё, чего нет в
    ответе. Мок повторяет этот протокол — отдаёт только те id, что переданы
    в `alive`. Возвращает список запрошенных путей, чтобы тесты могли
    проверить, что лишних вызовов нет.
    """
    import services.moysklad as ms

    calls = []

    async def _ms_get(path, params=None):
        calls.append(path)
        asked = [
            part.split("=", 1)[1]
            for part in ((params or {}).get("filter") or "").split(";")
            if part.startswith("id=")
        ]
        return {"rows": [{"id": i} for i in asked if i in alive]}

    monkeypatch.setattr(ms, "ms_get", _ms_get)
    return calls


def test_reconcile_cancels_deleted_co(isolated_db, monkeypatch):

    import tasks.run_ms_reconcile as rec

    db = isolated_db
    _mock_notify(monkeypatch)

    _mock_ms_list(monkeypatch, alive=set())
    oid = _mk_order(db, "approved", "CO-9")

    rc = asyncio.run(rec.main())
    assert rc == 0
    assert asyncio.run(db.get_order(oid))["status"] == "cancelled"


def test_reconcile_marks_deleted_shipped(isolated_db, monkeypatch):
    """Этап 1: реконсайл проверяет НЕ только approved. Shipped-заказ, чей CO удалён
    в МС (пропущенный вебхук), помечается ms_deleted_at — уходит из аналитики, но
    статус/деньги не трогаем. После обработки выпадает из набора реконсиляции."""

    import tasks.run_ms_reconcile as rec

    db = isolated_db
    _mock_notify(monkeypatch)

    _mock_ms_list(monkeypatch, alive=set())
    oid = _mk_order(db, "shipped", "CO-SHIP")

    rc = asyncio.run(rec.main())
    assert rc == 0
    o = asyncio.run(db.get_order(oid))
    assert o["status"] == "shipped"          # статус не тронут
    assert o["ms_deleted_at"] is not None    # помечен фантомом
    assert o["ms_customerorder_id"] is None  # ссылка снята
    # Выпал из набора реконсиляции (идемпотентно, повторно не дёргаем МС).
    assert asyncio.run(db.get_orders_with_ms_customerorder()) == []


def test_reconcile_keeps_existing_co(isolated_db, monkeypatch):
    import tasks.run_ms_reconcile as rec

    db = isolated_db
    _mock_notify(monkeypatch)

    _mock_ms_list(monkeypatch, alive={"CO-10"})
    oid = _mk_order(db, "approved", "CO-10")

    rc = asyncio.run(rec.main())
    assert rc == 0
    assert asyncio.run(db.get_order(oid))["status"] == "approved"  # не тронут


def test_reconcile_empty_makes_no_ms_calls(isolated_db, monkeypatch):
    import tasks.run_ms_reconcile as rec

    calls = _mock_ms_list(monkeypatch, alive=set())
    # Нет заказов со ссылками на МС (ни CO, ни demand).
    rc = asyncio.run(rec.main())
    assert rc == 0
    assert calls == []  # 0 МС-вызовов до проверки набора


def _mk_demand_order(db, status, demand_id, agent="Client"):
    db.set_role(1, "m", "M", "manager")
    oid = db.create_order(1, "M", "")
    db.update_order_agent(oid, "A-1", agent)
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(
            db.q("UPDATE orders SET status=?, ms_demand_id=? WHERE id=?"),
            (status, demand_id, oid),
        )
        conn.commit()
    return oid


def test_reconcile_marks_deleted_demand(isolated_db, monkeypatch):
    """Удалили в МС именно отгрузку (а заказ покупателя отсутствует/не связан):
    demand-документ 404 → заказ помечается ms_deleted_at и выпадает из набора."""

    import tasks.run_ms_reconcile as rec

    db = isolated_db
    _mock_notify(monkeypatch)

    _mock_ms_list(monkeypatch, alive=set())
    oid = _mk_demand_order(db, "shipped", "DM-9")

    rc = asyncio.run(rec.main())
    assert rc == 0
    o = asyncio.run(db.get_order(oid))
    assert o["status"] == "shipped"          # статус/деньги не трогаем
    assert o["ms_deleted_at"] is not None    # помечен фантомом → вне долгов/аналитики
    assert asyncio.run(db.get_orders_with_ms_demand()) == []  # идемпотентно


def test_reconcile_resets_deleted_paymentin(isolated_db, monkeypatch):
    """WP-17: paymentin удалён в МС (пропущенный paymentin.DELETE-вебхук) →
    reset_payment_ms_sync; ссылка снята, статус 'deleted_in_ms', retry пересоздаст."""

    import tasks.run_ms_reconcile as rec

    db = isolated_db
    _mock_notify(monkeypatch)
    db.set_role(1, "m", "M", "manager")
    pid = db.add_payment(1, "@m", "M", 100.0, "USD", "x", order_id=5)
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(
            db.q("UPDATE payments SET status='confirmed', ms_paymentin_id='PI-9' WHERE id=?"),
            (pid,),
        )
        conn.commit()

    _mock_ms_list(monkeypatch, alive=set())
    rc = asyncio.run(rec.main())
    assert rc == 0
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(
            db.q("SELECT ms_paymentin_id, ms_sync_status FROM payments WHERE id=?"), (pid,)
        )
        row = dict(cur.fetchone())
    assert row["ms_paymentin_id"] is None
    assert row["ms_sync_status"] == "deleted_in_ms"


def test_reconcile_demand_deleted_while_co_alive(isolated_db, monkeypatch):
    """Заказ покупателя в МС жив, но отгрузка удалена → заказ всё равно помечается
    ms_deleted_at (раньше reconcile проверял только CO и пропускал такой случай)."""

    import tasks.run_ms_reconcile as rec

    db = isolated_db
    _mock_notify(monkeypatch)

    _mock_ms_list(monkeypatch, alive={"CO-X"})  # CO жив, DM-X — нет
    db.set_role(1, "m", "M", "manager")
    oid = db.create_order(1, "M", "")
    db.update_order_agent(oid, "A-1", "Client")
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(
            db.q(
                "UPDATE orders SET status='shipped', ms_customerorder_id=?, "
                "ms_demand_id=? WHERE id=?"
            ),
            ("CO-X", "DM-X", oid),
        )
        conn.commit()

    rc = asyncio.run(rec.main())
    assert rc == 0
    assert asyncio.run(db.get_order(oid))["ms_deleted_at"] is not None
