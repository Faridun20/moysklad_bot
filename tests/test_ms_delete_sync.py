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


# ─── #35: cron-реконсиляция ──────────────────────────────────────────────────


def _mock_ms_get(monkeypatch, behaviour):
    import services.moysklad as ms

    calls = []

    async def _ms_get(path, params=None):
        calls.append(path)
        return await behaviour(path)

    monkeypatch.setattr(ms, "ms_get", _ms_get)
    return calls


def test_reconcile_cancels_deleted_co(isolated_db, monkeypatch):
    import aiohttp

    import tasks.run_ms_reconcile as rec

    db = isolated_db
    _mock_notify(monkeypatch)

    async def _b(path):
        raise aiohttp.ClientResponseError(None, (), status=404)

    _mock_ms_get(monkeypatch, _b)
    oid = _mk_order(db, "approved", "CO-9")

    rc = asyncio.run(rec.main())
    assert rc == 0
    assert asyncio.run(db.get_order(oid))["status"] == "cancelled"


def test_reconcile_marks_deleted_shipped(isolated_db, monkeypatch):
    """Этап 1: реконсайл проверяет НЕ только approved. Shipped-заказ, чей CO удалён
    в МС (пропущенный вебхук), помечается ms_deleted_at — уходит из аналитики, но
    статус/деньги не трогаем. После обработки выпадает из набора реконсиляции."""
    import aiohttp

    import tasks.run_ms_reconcile as rec

    db = isolated_db
    _mock_notify(monkeypatch)

    async def _b(path):
        raise aiohttp.ClientResponseError(None, (), status=404)

    _mock_ms_get(monkeypatch, _b)
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

    async def _b(path):
        return {"id": "CO-10"}  # существует

    _mock_ms_get(monkeypatch, _b)
    oid = _mk_order(db, "approved", "CO-10")

    rc = asyncio.run(rec.main())
    assert rc == 0
    assert asyncio.run(db.get_order(oid))["status"] == "approved"  # не тронут


def test_reconcile_empty_makes_no_ms_calls(isolated_db, monkeypatch):
    import tasks.run_ms_reconcile as rec

    async def _b(path):
        return {}

    calls = _mock_ms_get(monkeypatch, _b)
    # Нет approved-заказов с ms_customerorder_id.
    rc = asyncio.run(rec.main())
    assert rc == 0
    assert calls == []  # 0 МС-вызовов до проверки набора
