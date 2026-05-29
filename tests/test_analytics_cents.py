"""
Stage 3: личная аналитика менеджера считается в копейках.

Регресс на флагнутый баг — раньше total был в копейках, а top_products[].sum
в мажорных единицах, и format_sales_report (÷100) рисовал топ-товары в 100×
меньше. Теперь и total, и sum в копейках.
"""

from datetime import datetime, timedelta


def test_personal_stats_total_and_top_in_cents(isolated_db):
    db = isolated_db
    from handlers.analytics import _personal_stats_from_local

    oid = db.create_order(1, "U")
    db.add_order_item(oid, "Товар-А", "hrefA", 2, "шт", 49.99)  # 99.98 → 9998 коп.
    db.add_order_item(oid, "Товар-Б", "hrefB", 1, "шт", 10.00)  # 10.00 → 1000 коп.
    db.update_order_status(oid, "approved")  # делаем заказ «relevant»

    since = datetime.now() - timedelta(days=1)
    until = datetime.now() + timedelta(days=1)
    stats = _personal_stats_from_local(1, since, until)

    assert stats["count"] == 1
    assert stats["total"] == 10998  # копейки (9998 + 1000)
    top = dict(stats["top_products"])
    # sum в копейках, а не 99.98 / 10.0 (это и был баг 100×).
    assert top["Товар-А"]["sum"] == 9998
    assert top["Товар-Б"]["sum"] == 1000


def test_personal_stats_hides_deleted_products(isolated_db):
    db = isolated_db
    from handlers.analytics import _personal_stats_from_local

    # «Товар-А» есть в снапшоте ms_products, «Товар-Удалён» — нет (удалён в МС).
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(db.q("INSERT INTO ms_products (ms_id, name) VALUES (?, ?)"), ("PROD-A", "Товар-А"))
        conn.commit()

    oid = db.create_order(1, "U")
    db.add_order_item(oid, "Товар-А", "https://x/entity/product/PROD-A", 1, "шт", 100.0)
    db.add_order_item(oid, "Товар-Удалён", "https://x/entity/product/PROD-DELETED", 1, "шт", 50.0)
    db.update_order_status(oid, "approved")

    since = datetime.now() - timedelta(days=1)
    until = datetime.now() + timedelta(days=1)
    stats = _personal_stats_from_local(1, since, until)

    names = [n for n, _ in stats["top_products"]]
    assert "Товар-А" in names
    assert "Товар-Удалён" not in names  # удалённый в МС скрыт из топа
    # При этом общая выручка учитывает обе позиции (продажа состоялась).
    assert stats["total"] == 15000


def test_personal_stats_empty(isolated_db):
    from handlers.analytics import _personal_stats_from_local

    since = datetime.now() - timedelta(days=1)
    until = datetime.now() + timedelta(days=1)
    stats = _personal_stats_from_local(999, since, until)
    assert stats == {"total": 0, "count": 0, "clients": 0, "top_products": []}
