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

    async def go():
        with pytest.raises(RuntimeError, match="warehouses"):
            await m.apply_migration(PRODUCTS, COUNTERPARTIES, STOCK)

    _run(go())
