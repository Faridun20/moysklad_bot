"""
Шаг 4 оптимизаций:
  • get_setting — TTL-кэш + инвалидация в set_setting (горячий путь);
  • get_cash_deposit_orders_batch — батч вместо N+1 в /api/deposits/pending.
БД настоящая (isolated_db); кэш настроек обнуляется reload'ом модуля per-test.
"""

import asyncio


def test_settings_cache_and_invalidation(isolated_db):
    db = isolated_db
    db.set_setting("reject_max_cycles", 5, updated_by=1)
    assert db.get_setting("reject_max_cycles") == 5  # заполняет кэш

    # Меняем напрямую в БД (минуя set_setting) → кэш ещё держит старое значение.
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(
            db.q("UPDATE app_settings SET value = ? WHERE key = ?"), ("99", "reject_max_cycles")
        )
        conn.commit()
    assert db.get_setting("reject_max_cycles") == 5  # из кэша, не из БД

    # set_setting инвалидирует ключ → следующее чтение берёт свежее из БД.
    db.set_setting("reject_max_cycles", 7, updated_by=1)
    assert db.get_setting("reject_max_cycles") == 7


def test_settings_caller_default_not_cached(isolated_db):
    db = isolated_db
    # Неизвестный ключ: возвращается переданный default и НЕ кэшируется,
    # иначе следующий вызов с другим default отдал бы старый.
    assert db.get_setting("totally_unknown_key", default=42) == 42
    assert db.get_setting("totally_unknown_key", default=99) == 99


def test_cash_deposit_orders_batch_matches_per_id(isolated_db):
    db = isolated_db
    rows = [(1, 10, 100.0), (1, 11, 50.0), (2, 12, 200.0)]
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        for dep_id, oid, amt in rows:
            cur.execute(
                db.q(
                    "INSERT INTO cash_deposit_orders (deposit_id, order_id, amount_allocated) "
                    "VALUES (?, ?, ?)"
                ),
                (dep_id, oid, amt),
            )
        conn.commit()

    batch = asyncio.run(db.get_cash_deposit_orders_batch([1, 2, 3]))
    # Поэлементно совпадает с per-id версией (та же форма {order_id, amount_allocated}).
    assert batch[1] == asyncio.run(db.get_cash_deposit_orders(1))
    assert batch[2] == asyncio.run(db.get_cash_deposit_orders(2))
    assert len(batch[1]) == 2
    assert 3 not in batch  # сдача без привязанных заказов отсутствует
    assert asyncio.run(db.get_cash_deposit_orders_batch([])) == {}
