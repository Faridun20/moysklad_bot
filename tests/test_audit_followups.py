"""
Аудит-фиксы (вторая волна): мульти-валютный долг/лимит (#4), валюта cash-возврата
(#7), TOCTOU при смене статуса заказа (#6/#9, cas_order_status).
Реальная БД (isolated_db).
"""

import asyncio


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








