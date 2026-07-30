"""
Аудит-фиксы (вторая волна): мульти-валютный долг/лимит (#4), валюта cash-возврата
(#7), MS-sync нелегальные переходы + TOCTOU (#6/#9, cas_order_status), идемпотентный
алерт customerorder.DELETE (LOW). Реальная БД (isolated_db); МС и Telegram — мок.
"""

import asyncio

import services.ms_sync_handler as h
import services.moysklad as ms
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


def _mock_ms_get(monkeypatch, behaviour):
    async def _ms_get(path, params=None):
        return await behaviour(path)

    monkeypatch.setattr(ms, "ms_get", _ms_get)


def _credit_order(db, total_per_unit, qty, currency="USD", status="shipped", mgr=2):
    db.set_role(1, "b", "Boss", "boss")
    db.set_role(mgr, "m", "Mgr", "manager")
    oid = db.create_order(mgr, "Mgr", "")
    db.update_order_agent(oid, "A", "Client")
    iid = db.add_order_item(oid, "Товар", "", qty, "шт", total_per_unit)
    db.update_order_status(oid, status)
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(
            db.q("UPDATE orders SET payment_type='credit', currency=? WHERE id=?"),
            (currency, oid),
        )
        conn.commit()
    return oid, iid


# ─── #4: мульти-валютный долг и лимит ─────────────────────────────────────────


def test_agent_debt_converts_to_base(isolated_db):
    db = isolated_db
    db.set_currency_rate("UZS", 0.00008, 1)  # 1 UZS = 0.00008 USD
    db._invalidate_currency_rates_cache()
    _credit_order(db, 100.0, 1, "USD")              # 100 USD
    _credit_order(db, 5_000_000.0, 1, "UZS")        # 5M UZS → 400 USD

    debt = asyncio.run(db.get_agent_current_debt("A"))
    assert debt == 500.0  # 100 + 400, а не «5 000 100» от смешения валют


def test_check_credit_limit_converts_order_total(isolated_db):
    db = isolated_db
    db.set_currency_rate("UZS", 0.00008, 1)
    db._invalidate_currency_rates_cache()
    # Новый заказ 5M UZS = 400 USD; дефолтный лимит 2000 → НЕ превышение.
    chk = asyncio.run(db.check_credit_limit("NEW-AGENT", 5_000_000.0, "UZS"))
    assert chk["order_total_base"] == 400.0
    assert chk["over_limit"] is False


# ─── #9: compare-and-set статуса ──────────────────────────────────────────────


def test_cas_order_status(isolated_db):
    db = isolated_db
    db.set_role(1, "m", "M", "manager")
    oid = db.create_order(1, "M", "")
    db.update_order_status(oid, "pending")
    assert asyncio.run(db.cas_order_status(oid, "approved", "pending")) is True
    assert asyncio.run(db.get_order(oid))["status"] == "approved"
    # expected не совпал (уже approved) → не применяем
    assert asyncio.run(db.cas_order_status(oid, "shipped", "pending")) is False
    assert asyncio.run(db.get_order(oid))["status"] == "approved"


# ─── #6: гейт нелегальных переходов из вебхука customerorder.UPDATE ────────────


def test_co_update_illegal_transition_skipped(isolated_db, monkeypatch):
    """approved + Unsuccessful (→rejected) — нелегальный переход: статус НЕ трогаем,
    алертим boss. Раньше заказ молча становился rejected без отката отгрузки."""
    db = isolated_db
    sent = _mock_notify(monkeypatch)
    oid, _ = _credit_order(db, 100.0, 1, status="approved")
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(db.q("UPDATE orders SET ms_customerorder_id='CO-1' WHERE id=?"), (oid,))
        conn.commit()

    async def _b(path):
        return {"sum": 10000, "state": {"stateType": "Unsuccessful", "name": "Отменён"}}

    _mock_ms_get(monkeypatch, _b)
    asyncio.run(h._handle_customerorder_updated("CO-1"))

    assert asyncio.run(db.get_order(oid))["status"] == "approved"  # НЕ rejected
    assert sent and any("вручную" in t for _, t in sent)


def test_illegal_transition_alert_not_suppressed_by_drift(isolated_db, monkeypatch):
    """Уже выставленный ms_drift_at (правка суммы в МС) НЕ должен глушить алерт о
    нелегальном переходе — это отдельный флаг ms_transition_blocked_at. Раньше оба
    делили ms_drift_at → drift молча съедал более опасный «застрявший статус»."""
    db = isolated_db
    sent = _mock_notify(monkeypatch)
    oid, _ = _credit_order(db, 100.0, 1, status="approved")
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(
            db.q(
                "UPDATE orders SET ms_customerorder_id='CO-D1', "
                "ms_drift_at='2026-06-04 00:00:00' WHERE id=?"
            ),
            (oid,),
        )
        conn.commit()

    async def _b(path):
        return {"sum": 10000, "state": {"stateType": "Unsuccessful", "name": "Отменён"}}

    _mock_ms_get(monkeypatch, _b)
    asyncio.run(h._handle_customerorder_updated("CO-D1"))

    o = asyncio.run(db.get_order(oid))
    assert o["status"] == "approved"  # статус не тронут
    assert sent and any("вручную" in t for _, t in sent)  # алерт всё равно ушёл
    assert o["ms_transition_blocked_at"] is not None  # отдельный флаг выставлен


def test_transition_blocked_dedup_and_surfaced_as_anomaly(isolated_db, monkeypatch):
    """Повторный customerorder.UPDATE по застрявшему заказу не шлёт второй алерт
    (дедуп по ms_transition_blocked_at), и заказ виден в аномалиях МС-синка."""
    db = isolated_db
    sent = _mock_notify(monkeypatch)
    oid, _ = _credit_order(db, 100.0, 1, status="approved")
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(db.q("UPDATE orders SET ms_customerorder_id='CO-D2' WHERE id=?"), (oid,))
        conn.commit()

    async def _b(path):
        return {"sum": 10000, "state": {"stateType": "Unsuccessful", "name": "Отменён"}}

    _mock_ms_get(monkeypatch, _b)
    asyncio.run(h._handle_customerorder_updated("CO-D2"))
    asyncio.run(h._handle_customerorder_updated("CO-D2"))  # повтор

    assert len([t for _, t in sent if "вручную" in t]) == 1  # ровно один алерт

    anomalies = asyncio.run(db.get_ms_sync_anomalies("2000-01-01 00:00:00"))
    assert any(o["id"] == oid for o in anomalies["transition_blocked"])


def test_co_update_legal_transition_applied(isolated_db, monkeypatch):
    """approved + Successful (→shipped) — легальный переход, применяется через CAS."""
    db = isolated_db
    _mock_notify(monkeypatch)
    oid, _ = _credit_order(db, 100.0, 1, status="approved")
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(db.q("UPDATE orders SET ms_customerorder_id='CO-2' WHERE id=?"), (oid,))
        conn.commit()

    async def _b(path):
        return {"sum": 10000, "state": {"stateType": "Successful", "name": "Отгружен"}}

    _mock_ms_get(monkeypatch, _b)
    asyncio.run(h._handle_customerorder_updated("CO-2"))

    shipped = asyncio.run(db.get_order(oid))
    assert shipped["status"] == "shipped"
    # WP-13: webhook-отгрузка проставляет shipped_at/shipped_by (инварианты
    # дедлайна возвратов и stale-детекта). 0 = маркер «МойСклад».
    assert shipped["shipped_at"] is not None
    assert shipped["shipped_by"] == 0


# ─── #7: валюта cash-возврата ─────────────────────────────────────────────────


def test_cash_refund_converted_to_base(isolated_db):
    """Cash-возврат по UZS-заказу пишется в кассу в БАЗОВОЙ валюте (через курс),
    а не как «5 000 000 USD»."""
    db = isolated_db
    db.set_currency_rate("UZS", 0.00008, 1)
    db._invalidate_currency_rates_cache()
    oid, iid = _credit_order(db, 500_000.0, 10, "UZS")  # 5M UZS

    r = asyncio.run(db.create_return(oid, "full", "брак", [(iid, 10, 5_000_000.0)], "cash", 1))
    assert r["ok"], r
    asyncio.run(db.mark_return_goods_received(r["return_id"], 1))
    asyncio.run(db.confirm_return(r["return_id"], 1, "Boss"))

    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(
            db.q("SELECT amount_cents FROM cash_deposits WHERE notes = ?"),
            (f"refund возврат #{r['return_id']}",),
        )
        row = cur.fetchone()
    assert row[0] == -40000  # -400 USD, а не -5 000 000


# ─── LOW: идемпотентный алерт customerorder.DELETE ────────────────────────────


def test_co_delete_alert_idempotent(isolated_db, monkeypatch):
    db = isolated_db
    sent = _mock_notify(monkeypatch)
    oid, _ = _credit_order(db, 100.0, 1, status="approved")

    asyncio.run(h.apply_ms_customerorder_delete(asyncio.run(db.get_order(oid)), "CO-9"))
    assert asyncio.run(db.get_order(oid))["status"] == "cancelled"
    assert len(sent) == 1

    # Повторный вызов с уже-помеченным заказом → без второго алерта.
    sent.clear()
    asyncio.run(h.apply_ms_customerorder_delete(asyncio.run(db.get_order(oid)), "CO-9"))
    assert sent == []


def test_co_delete_shipped_alert_dedup_on_race(isolated_db, monkeypatch):
    """WP-12: два конкурентных вызова с ОДНИМ стале order-dict (ms_deleted_at=None
    в памяти) для shipped-заказа шлют ровно одно уведомление — проигравший гонку
    set_order_ms_deleted выходит тихо."""
    db = isolated_db
    sent = _mock_notify(monkeypatch)
    oid, _ = _credit_order(db, 100.0, 1, status="shipped")
    stale = asyncio.run(db.get_order(oid))  # ms_deleted_at=None в памяти
    asyncio.run(h.apply_ms_customerorder_delete(stale, "CO-5"))
    asyncio.run(h.apply_ms_customerorder_delete(stale, "CO-5"))  # тот же стале dict
    assert len(sent) == 1
    assert asyncio.run(db.get_order(oid))["status"] == "shipped"  # статус не тронут


def test_demand_delete_alert_dedup_on_race(isolated_db, monkeypatch):
    """WP-12: то же для demand.DELETE."""
    db = isolated_db
    sent = _mock_notify(monkeypatch)
    oid, _ = _credit_order(db, 100.0, 1, status="shipped")
    stale = asyncio.run(db.get_order(oid))
    asyncio.run(h.apply_ms_demand_delete(stale, "DM-1"))
    asyncio.run(h.apply_ms_demand_delete(stale, "DM-1"))
    assert len(sent) == 1


def test_co_delete_cancels_approved_even_if_already_ms_deleted(isolated_db, monkeypatch):
    """demand.DELETE мог выставить ms_deleted_at, оставив заказ approved (он не
    отменяет). Последующий customerorder.DELETE всё равно ДОЛЖЕН отменить approved —
    ранний выход по ms_deleted_at это пропускал (регресс)."""
    db = isolated_db
    _mock_notify(monkeypatch)
    oid, _ = _credit_order(db, 100.0, 1, status="approved")
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(
            db.q("UPDATE orders SET ms_deleted_at='2026-06-04 00:00:00' WHERE id=?"), (oid,)
        )
        conn.commit()

    asyncio.run(h.apply_ms_customerorder_delete(asyncio.run(db.get_order(oid)), "CO-7"))
    assert asyncio.run(db.get_order(oid))["status"] == "cancelled"
