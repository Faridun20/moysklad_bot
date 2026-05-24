"""
Тесты currency rates: get/set, кэш с TTL, convert_to_base, payment_summary
с *_base полями.

Без сети, БД настоящая (isolated_db).
"""

import pytest


def test_base_currency_seeded_on_init_db(isolated_db):
    """init_db должен засидить USD=1.0, иначе convert_to_base(x, 'USD') не работает."""
    db = isolated_db
    rates = db.get_all_currency_rates()
    by = {r["currency_code"]: r for r in rates}
    assert "USD" in by
    assert by["USD"]["rate_to_base"] == 1.0


def test_get_currency_rate_returns_seeded_usd(isolated_db):
    db = isolated_db
    assert db.get_currency_rate("USD") == 1.0
    assert db.get_currency_rate("usd") == 1.0  # case-insensitive


def test_get_currency_rate_returns_none_for_unknown(isolated_db):
    db = isolated_db
    assert db.get_currency_rate("UZS") is None
    assert db.get_currency_rate("") is None


def test_set_currency_rate_persists_and_invalidates_cache(isolated_db):
    db = isolated_db
    # Дёргаем get чтобы заполнить кэш (None для UZS)
    assert db.get_currency_rate("UZS") is None
    # Ставим — должно перечитать БД, не вернуть закэшированное None
    ok, err = db.set_currency_rate("UZS", 0.00008, updated_by=1)
    assert ok is True and err is None
    assert db.get_currency_rate("UZS") == 0.00008


def test_set_currency_rate_validates_amount(isolated_db):
    db = isolated_db
    for bad in (-1.0, 0.0, float("nan"), float("inf"), 10_000_001.0):
        ok, err = db.set_currency_rate("EUR", bad, updated_by=1)
        assert ok is False, f"должен отвергать {bad!r}"
        assert err is not None


def test_set_currency_rate_validates_currency_in_allowlist(isolated_db):
    db = isolated_db
    ok, err = db.set_currency_rate("BTC", 50000.0, updated_by=1)
    assert ok is False
    assert "ALLOWED" in err or "должен быть из" in err


def test_set_currency_rate_normalizes_code_uppercase(isolated_db):
    db = isolated_db
    db.set_currency_rate("uzs", 0.00008, updated_by=1)
    rates = {r["currency_code"]: r for r in db.get_all_currency_rates()}
    assert "UZS" in rates
    assert "uzs" not in rates


def test_convert_to_base_identity_for_base_currency(isolated_db):
    """USD → USD без обращения к БД (base check срабатывает первым)."""
    db = isolated_db
    assert db.convert_to_base(100.0, "USD") == 100.0
    # None currency → assume base
    assert db.convert_to_base(50.0, None) == 50.0


def test_convert_to_base_multiplies_by_rate(isolated_db):
    """UZS 1_000_000 при курсе 0.00008 → 80 USD."""
    db = isolated_db
    db.set_currency_rate("UZS", 0.00008, updated_by=1)
    assert db.convert_to_base(1_000_000.0, "UZS") == pytest.approx(80.0, rel=1e-6)


def test_convert_to_base_returns_none_for_missing_rate(isolated_db):
    """Если админ не задал курс — None (caller решает: показать «—» или skip)."""
    db = isolated_db
    assert db.convert_to_base(100.0, "EUR") is None


def test_convert_to_base_returns_none_for_invalid_amount(isolated_db):
    db = isolated_db
    assert db.convert_to_base("not-a-number", "USD") is None
    assert db.convert_to_base(None, "USD") is None


def test_get_order_payment_summary_includes_base_fields(isolated_db):
    """payment_summary должен возвращать *_base поля. USD-заказ — total_base=total."""
    db = isolated_db
    mgr = 100
    db.set_role(mgr, "m", "M", "manager")
    oid = db.create_order(mgr, "M", "")
    db.add_order_item(oid, "Товар", "", 2, "шт", 50.0)  # total=100
    db.update_order_status(oid, "approved")

    summary = db.get_order_payment_summary(oid)
    assert summary["total"] == 100.0
    assert summary["currency"] == "USD"
    assert summary["total_base"] == 100.0  # USD = base
    assert summary["base_currency"] == "USD"
    assert summary["confirmed_base"] == 0.0
    assert summary["remaining_base"] == 100.0


def test_get_order_payment_summary_base_none_for_unrated_currency(isolated_db):
    """Заказ в EUR без заданного курса → *_base = None."""
    db = isolated_db
    mgr = 100
    db.set_role(mgr, "m", "M", "manager")
    oid = db.create_order(mgr, "M", "")
    db.add_order_item(oid, "Товар", "", 1, "шт", 100.0)
    db.update_order_status(oid, "approved")
    # Меняем валюту заказа на EUR (не задан курс)
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(db.q("UPDATE orders SET currency = ? WHERE id = ?"), ("EUR", oid))
        conn.commit()

    summary = db.get_order_payment_summary(oid)
    assert summary["currency"] == "EUR"
    assert summary["total_base"] is None  # курс не задан → None, не 0


def test_get_order_payment_summary_converts_rated_currency(isolated_db):
    """Заказ в UZS с заданным курсом → total_base в USD."""
    db = isolated_db
    db.set_currency_rate("UZS", 0.00008, updated_by=1)
    mgr = 100
    db.set_role(mgr, "m", "M", "manager")
    oid = db.create_order(mgr, "M", "")
    db.add_order_item(oid, "Товар", "", 1, "шт", 1_000_000.0)
    db.update_order_status(oid, "approved")
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(db.q("UPDATE orders SET currency = ? WHERE id = ?"), ("UZS", oid))
        conn.commit()

    summary = db.get_order_payment_summary(oid)
    assert summary["currency"] == "UZS"
    assert summary["total"] == 1_000_000.0
    assert summary["total_base"] == pytest.approx(80.0)


def test_cache_invalidated_only_for_changed_currency(isolated_db):
    """set('UZS') не должен сбросить кэш USD."""
    db = isolated_db
    # Заполняем кэш USD (seeded = 1.0)
    assert db.get_currency_rate("USD") == 1.0
    # Меняем UZS
    db.set_currency_rate("UZS", 0.0001, updated_by=1)
    # USD остался кэшированным как 1.0 — это OK (rate не менялся)
    assert db.get_currency_rate("USD") == 1.0
    # UZS взялся из БД
    assert db.get_currency_rate("UZS") == 0.0001
