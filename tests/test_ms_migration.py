"""Одноразовая миграция МойСклад → локальные таблицы: запись и сверка.

Главное, что здесь стережётся, — сверка. По плану переключения расхождение
обязано БЛОКИРОВАТЬ переход: после отключения МойСклад обратной
синхронизации нет, и разъехавшийся остаток уже нечем починить.

Выгрузку из МС (границу с внешним миром) не дёргаем — apply/verify работают
над готовыми данными, их и проверяем на настоящей БД.
"""

import asyncio
from decimal import Decimal

import pytest


@pytest.fixture
def mig(isolated_db):
    """Схема + склад по умолчанию. Возвращает модуль миграции."""
    import importlib

    import scripts.migrate_from_moysklad as m

    importlib.reload(m)

    db = isolated_db
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(db.q("INSERT INTO warehouses (name) VALUES (?)"), ("Основной склад",))
        conn.commit()
    return m


def _run(coro):
    return asyncio.run(coro)


PRODUCTS = [
    {"ms_id": "uuid-p1", "name": "Болт М8", "category": "Крепёж", "sku": "B8", "unit": "шт"},
    {"ms_id": "uuid-p2", "name": "Гайка М8", "category": "Крепёж", "sku": "G8", "unit": "шт"},
]
COUNTERPARTIES = [
    {"ms_id": "uuid-c1", "name": "ООО Ромашка", "phone": "+998901112233", "type": "customer"},
]
STOCK = {"uuid-p1": Decimal("10.5"), "uuid-p2": Decimal("3")}


def test_apply_writes_products_counterparties_and_stock(mig):
    async def go():
        from services import adb_core

        stats = await mig.apply_migration(PRODUCTS, COUNTERPARTIES, STOCK)
        assert stats["products"] == 2
        assert stats["counterparties"] == 1
        assert stats["stock_rows"] == 2

        rows = await adb_core.fetch("SELECT name, category, sku, unit, legacy_ms_id FROM products ORDER BY id")
        assert [r["name"] for r in rows] == ["Болт М8", "Гайка М8"]
        assert rows[0]["legacy_ms_id"] == "uuid-p1"
        assert rows[0]["category"] == "Крепёж"

        cp = await adb_core.fetchrow("SELECT * FROM counterparties")
        assert cp["name"] == "ООО Ромашка"
        assert cp["type"] == "customer"

        total = await adb_core.fetchval("SELECT SUM(quantity) FROM stock")
        assert Decimal(str(total)) == Decimal("13.5")

    _run(go())


def test_apply_fills_id_map(mig):
    """ms_id_map — то, по чему на шаге 4 заказы переедут на числовые ключи."""

    async def go():
        from services import adb_core

        await mig.apply_migration(PRODUCTS, COUNTERPARTIES, STOCK)
        rows = await adb_core.fetch("SELECT entity_type, ms_id, local_id FROM ms_id_map ORDER BY ms_id")
        got = {(r["entity_type"], r["ms_id"]) for r in rows}
        assert got == {
            ("product", "uuid-p1"),
            ("product", "uuid-p2"),
            ("counterparty", "uuid-c1"),
        }
        # local_id реально указывает на существующие строки.
        for r in rows:
            table = "products" if r["entity_type"] == "product" else "counterparties"
            assert await adb_core.fetchval(
                f"SELECT COUNT(*) FROM {table} WHERE id = $1", r["local_id"]
            ) == 1

    _run(go())


def test_apply_is_idempotent(mig):
    """Повторный прогон обновляет, а не плодит дубли."""

    async def go():
        from services import adb_core

        await mig.apply_migration(PRODUCTS, COUNTERPARTIES, STOCK)
        renamed = [dict(PRODUCTS[0], name="Болт М8 оцинкованный"), PRODUCTS[1]]
        await mig.apply_migration(renamed, COUNTERPARTIES, STOCK)

        assert await adb_core.fetchval("SELECT COUNT(*) FROM products") == 2
        assert await adb_core.fetchval("SELECT COUNT(*) FROM counterparties") == 1
        assert await adb_core.fetchval("SELECT COUNT(*) FROM stock") == 2
        assert await adb_core.fetchval("SELECT COUNT(*) FROM ms_id_map") == 3
        name = await adb_core.fetchval("SELECT name FROM products WHERE legacy_ms_id = 'uuid-p1'")
        assert name == "Болт М8 оцинкованный"

    _run(go())


def test_verify_passes_when_consistent(mig):
    async def go():
        await mig.apply_migration(PRODUCTS, COUNTERPARTIES, STOCK)
        assert await mig.verify(PRODUCTS, COUNTERPARTIES, STOCK) == []

    _run(go())


def test_verify_catches_missing_product(mig):
    """В МС товаров больше, чем доехало локально — переключение блокируется."""

    async def go():
        await mig.apply_migration(PRODUCTS[:1], COUNTERPARTIES, STOCK)
        problems = await mig.verify(PRODUCTS, COUNTERPARTIES, STOCK)
        assert problems
        assert any("товары" in p for p in problems)

    _run(go())


def test_verify_catches_missing_counterparty(mig):
    async def go():
        await mig.apply_migration(PRODUCTS, [], STOCK)
        problems = await mig.verify(PRODUCTS, COUNTERPARTIES, STOCK)
        assert any("контрагенты" in p for p in problems)

    _run(go())


def test_verify_catches_stock_drift(mig):
    """Остаток разошёлся на 0.5 — расхождение ловится, а не списывается на шум."""

    async def go():
        from services import adb_core

        await mig.apply_migration(PRODUCTS, COUNTERPARTIES, STOCK)
        pid = await adb_core.fetchval("SELECT id FROM products WHERE legacy_ms_id = 'uuid-p1'")
        await adb_core.execute("UPDATE stock SET quantity = 10.0 WHERE product_id = $1", pid)

        problems = await mig.verify(PRODUCTS, COUNTERPARTIES, STOCK)
        assert problems
        assert any("сумма остатков" in p for p in problems)
        assert any("uuid-p1" in p for p in problems)

    _run(go())


def test_verify_tolerance_is_exactly_zero(mig):
    """Допуск — ноль. Даже копеечное расхождение блокирует переход."""
    assert mig.TOLERANCE == 0  # Decimal("0"); сравнение с int, чтобы не ловить SIM300

    async def go():
        from services import adb_core

        await mig.apply_migration(PRODUCTS, COUNTERPARTIES, STOCK)
        pid = await adb_core.fetchval("SELECT id FROM products WHERE legacy_ms_id = 'uuid-p2'")
        await adb_core.execute("UPDATE stock SET quantity = 3.001 WHERE product_id = $1", pid)
        assert await mig.verify(PRODUCTS, COUNTERPARTIES, STOCK) != []

    _run(go())


def test_orphan_stock_is_skipped_and_does_not_break_verification(mig):
    """Остаток по товару без карточки (архив в МС) не ломает сверку.

    Включать его в ожидаемую сумму нельзя — переносить такой остаток некуда,
    и сверка не сходилась бы никогда, сколько ни перезапускай.
    """

    async def go():
        stock = dict(STOCK, **{"uuid-gone": Decimal("99")})
        stats = await mig.apply_migration(PRODUCTS, COUNTERPARTIES, stock)
        assert stats["stock_skipped"] == 1
        assert stats["stock_rows"] == 2
        assert await mig.verify(PRODUCTS, COUNTERPARTIES, stock) == []

    _run(go())


def test_fractional_quantities_compare_exactly(mig):
    """Дробные остатки сверяются через Decimal, а не float.

    0.1 + 0.2 в float даёт 0.30000000000000004, и наивное сравнение
    объявило бы расхождение на ровном месте.
    """

    async def go():
        stock = {"uuid-p1": Decimal("0.1"), "uuid-p2": Decimal("0.2")}
        await mig.apply_migration(PRODUCTS, COUNTERPARTIES, stock)
        assert await mig.verify(PRODUCTS, COUNTERPARTIES, stock) == []

    _run(go())


def test_apply_without_warehouse_fails_loudly(isolated_db):
    """Без засеянного склада миграция падает с внятным текстом, а не пишет мимо."""
    import importlib

    import scripts.migrate_from_moysklad as m

    importlib.reload(m)
    # Фикстура сеет склад по умолчанию (без него не проходит ни одна накладная);
    # здесь проверяется ровно противоположное состояние — убираем его.
    with isolated_db.get_conn() as conn:
        cur = isolated_db.get_cursor(conn)
        cur.execute("DELETE FROM warehouses")
        conn.commit()

    async def go():
        with pytest.raises(RuntimeError, match="warehouses"):
            await m.apply_migration(PRODUCTS, COUNTERPARTIES, STOCK)

    _run(go())


# ─── Повторный --apply после переключения (P0-4) ─────────────────────────────


def _live_outgoing_invoice(product_ms_id="uuid-p1", qty=4.0):
    """Живая расходная накладная через warehouse — как после переключения."""
    from services import adb_core, warehouse

    async def go():
        pid = await adb_core.fetchval(
            "SELECT id FROM products WHERE legacy_ms_id = $1", product_ms_id
        )
        wid = await adb_core.fetchval("SELECT id FROM warehouses ORDER BY id LIMIT 1")
        async with adb_core.transaction() as txn:
            return await warehouse.create_invoice_in(
                txn, invoice_type="outgoing", warehouse_id=int(wid),
                items=[{"product_id": pid, "quantity": qty, "price_cents": 100}],
                created_by=777,
            )

    return _run(go())


def _age_migration(isolated_db):
    """Сдвинуть момент первого переноса в прошлое: в тесте всё происходит в
    одну секунду, а граница «до/после» сравнивается строго."""
    with isolated_db.get_conn() as conn:
        cur = isolated_db.get_cursor(conn)
        cur.execute("UPDATE ms_id_map SET migrated_at = '2026-01-01 00:00:00'")
        conn.commit()


def test_no_live_data_right_after_first_migration(mig):
    async def go():
        await mig.apply_migration(PRODUCTS, COUNTERPARTIES, STOCK)
        live = await mig.live_activity()
        assert (live["invoices"], live["orders"]) == (0, 0)
        assert mig.live_data_refusal(live) is None

    _run(go())


def test_rerun_apply_is_refused_when_live_invoices_exist(mig, isolated_db, monkeypatch):
    """Живая накладная после переноса → повторный --apply отказывается и
    остаток не трогает. Снимок МС (10.5) стёр бы списание 4 шт молча."""
    from services import adb_core

    _run(mig.apply_migration(PRODUCTS, COUNTERPARTIES, STOCK))
    _age_migration(isolated_db)
    _live_outgoing_invoice(qty=4.0)

    async def fake_pull_products():
        return PRODUCTS

    async def fake_pull_counterparties():
        return COUNTERPARTIES

    async def fake_pull_stock():
        return STOCK

    monkeypatch.setattr(mig, "pull_products", fake_pull_products)
    monkeypatch.setattr(mig, "pull_counterparties", fake_pull_counterparties)
    monkeypatch.setattr(mig, "pull_stock", fake_pull_stock)

    assert _run(mig.main("apply")) == 1

    qty = _run(adb_core.fetchval(
        "SELECT s.quantity FROM stock s JOIN products p ON p.id = s.product_id "
        "WHERE p.legacy_ms_id = 'uuid-p1'"
    ))
    assert Decimal(str(qty)) == Decimal("6.5"), "живое списание 4 шт сохранено"


def test_refusal_text_explains_and_names_the_override(mig, isolated_db):
    _run(mig.apply_migration(PRODUCTS, COUNTERPARTIES, STOCK))
    _age_migration(isolated_db)
    _live_outgoing_invoice()

    live = _run(mig.live_activity())
    assert live["invoices"] == 1
    assert live["product_ms_ids"] == {"uuid-p1"}
    text = mig.live_data_refusal(live)
    assert "накладных 1" in text and "--i-know-live-data" in text


def test_override_updates_catalog_but_keeps_live_stock(mig, isolated_db, monkeypatch):
    """--i-know-live-data: справочник догоняется, остаток товаров с живыми
    движениями не перезаписывается, у остальных — снимок МС."""
    from services import adb_core

    _run(mig.apply_migration(PRODUCTS, COUNTERPARTIES, STOCK))
    _age_migration(isolated_db)
    _live_outgoing_invoice(product_ms_id="uuid-p1", qty=4.0)

    new_stock = {"uuid-p1": Decimal("99"), "uuid-p2": Decimal("7")}
    renamed = [dict(PRODUCTS[0], name="Болт М8 оцинк."), PRODUCTS[1]]
    monkeypatch.setattr(mig, "pull_products", lambda: _async(renamed))
    monkeypatch.setattr(mig, "pull_counterparties", lambda: _async(COUNTERPARTIES))
    monkeypatch.setattr(mig, "pull_stock", lambda: _async(new_stock))

    assert _run(mig.main("apply", allow_live=True)) == 0, "сверка без защищённых товаров сходится"

    rows = {
        r["legacy_ms_id"]: (r["name"], Decimal(str(r["quantity"])))
        for r in _run(adb_core.fetch(
            "SELECT p.legacy_ms_id, p.name, s.quantity FROM products p "
            "JOIN stock s ON s.product_id = p.id"
        ))
    }
    assert rows["uuid-p1"] == ("Болт М8 оцинк.", Decimal("6.5")), "живой остаток не стёрт"
    assert rows["uuid-p2"] == ("Гайка М8", Decimal("7")), "остальные — снимок МС"


def test_migrated_at_is_not_moved_by_rerun(mig, isolated_db):
    """Граница «до/после переноса» не сдвигается повтором — иначе второй
    повтор уже не увидел бы живую работу, случившуюся до первого."""
    from services import adb_core

    _run(mig.apply_migration(PRODUCTS, COUNTERPARTIES, STOCK))
    _age_migration(isolated_db)
    _run(mig.apply_migration(PRODUCTS, COUNTERPARTIES, STOCK))
    assert _run(adb_core.fetchval("SELECT MAX(migrated_at) FROM ms_id_map")) \
        == "2026-01-01 00:00:00"


def test_rerun_does_not_reset_counterparty_type(mig):
    """Тип контрагента уточняет перенос истории (supplier) или человек;
    повтор справочника не возвращает всех в customer."""
    from services import adb_core

    _run(mig.apply_migration(PRODUCTS, COUNTERPARTIES, STOCK))
    _run(adb_core.execute("UPDATE counterparties SET type = 'supplier'"))
    _run(mig.apply_migration(PRODUCTS, COUNTERPARTIES, STOCK))
    assert _run(adb_core.fetchval("SELECT type FROM counterparties")) == "supplier"


def test_override_flag_requires_apply(mig):
    with pytest.raises(SystemExit):
        mig._parse_args(["--dry-run", "--i-know-live-data"])
    assert mig._parse_args(["--apply", "--i-know-live-data"]).i_know_live_data is True


async def _async(value):
    return value
