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


# ─── Отрицательный остаток, дубли артикула, цены (перенос на чистую базу) ─────
#
# Прошлый боевой перенос привёз 32 отрицательных остатка (МС разрешает продавать
# в минус) — из-за них не ставится `stock_quantity_chk`. Артикул в МС не
# уникален, а у нас UNIQUE: один дубль ронял бы весь перенос. Цены владелец
# хочет перенести вместе со справочником.

USD_ID, UZS_ID, EUR_ID = "cur-usd", "cur-uzs", "cur-eur"
ISO = {USD_ID: "USD", UZS_ID: "UZS", EUR_ID: "EUR"}


def _money(value, cur_id, type_name=None):
    d = {"value": value,
         "currency": {"meta": {"href": f"https://api.moysklad.ru/api/remap/1.2/entity/currency/{cur_id}"}}}
    if type_name is not None:
        d["priceType"] = {"name": type_name}
    return d


def _raw_product(name="Болт", sale=None, buy=None):
    return {"id": "p", "name": name, "salePrices": sale or [], "buyPrice": buy}


def test_negative_ms_stock_is_written_as_zero_and_reported(mig):
    from services import adb_core

    stock = {"uuid-p1": Decimal("-5"), "uuid-p2": Decimal("3")}
    stats = _run(mig.apply_migration(PRODUCTS, COUNTERPARTIES, stock))
    assert stats["stock_negative_clamped"] == 1
    assert stats["stock_negative_total"] == Decimal("5")
    lines = next(v for k, v in stats["issues"].items() if "отрицательный" in k)
    assert lines == ["«Болт М8»: в МС -5 → записано 0"]
    qty = _run(adb_core.fetchval(
        "SELECT s.quantity FROM stock s JOIN products p ON p.id = s.product_id "
        "WHERE p.legacy_ms_id = 'uuid-p1'"))
    assert Decimal(str(qty)) == 0
    assert _run(adb_core.fetchval("SELECT COUNT(*) FROM stock WHERE quantity < 0")) == 0
    # Сверка сравнивает с тем, что обязан был записать перенос, — с нулём.
    assert _run(mig.verify(PRODUCTS, COUNTERPARTIES, stock)) == []
    assert mig.negative_stock(PRODUCTS, stock) == [("Болт М8", Decimal("-5"))]


def test_verify_still_catches_drift_on_clamped_row(mig):
    from services import adb_core

    stock = {"uuid-p1": Decimal("-5"), "uuid-p2": Decimal("3")}
    _run(mig.apply_migration(PRODUCTS, COUNTERPARTIES, stock))
    _run(adb_core.execute("UPDATE stock SET quantity = -5"))
    assert _run(mig.verify(PRODUCTS, COUNTERPARTIES, stock)) != []


def test_duplicate_sku_goes_to_first_product_only(mig):
    from services import adb_core

    products = [dict(PRODUCTS[0], sku="X1"), dict(PRODUCTS[1], sku="X1")]
    stats = _run(mig.apply_migration(products, COUNTERPARTIES, STOCK))
    assert stats["sku_duplicates"] == 1
    assert any("артикул X1" in line for lines in stats["issues"].values() for line in lines)
    rows = _run(adb_core.fetch("SELECT legacy_ms_id, sku FROM products ORDER BY id"))
    assert [(r["legacy_ms_id"], r["sku"]) for r in rows] == [("uuid-p1", "X1"), ("uuid-p2", None)]

    # Повтор: первый товар сохраняет артикул, дубль не «перехватывает» его.
    again = _run(mig.apply_migration(products, COUNTERPARTIES, STOCK))
    assert again["sku_duplicates"] == 1
    rows = _run(adb_core.fetch("SELECT legacy_ms_id, sku FROM products ORDER BY id"))
    assert [(r["legacy_ms_id"], r["sku"]) for r in rows] == [("uuid-p1", "X1"), ("uuid-p2", None)]


def test_sku_taken_by_live_card_is_a_duplicate(mig, isolated_db):
    from services import adb_core

    _run(adb_core.execute(
        "INSERT INTO products (name, unit, sku, created_at) VALUES ('Живой', 'шт', 'B8', '2026-01-01')"))
    stats = _run(mig.apply_migration(PRODUCTS, COUNTERPARTIES, STOCK))
    assert stats["sku_duplicates"] == 1
    assert _run(adb_core.fetchval("SELECT sku FROM products WHERE legacy_ms_id = 'uuid-p1'")) is None


def test_sku_swap_between_products_on_rerun(mig):
    """В МС артикулы двух товаров поменяли местами — UNIQUE не падает на обмене."""
    from services import adb_core

    _run(mig.apply_migration(PRODUCTS, COUNTERPARTIES, STOCK))
    swapped = [dict(PRODUCTS[0], sku="G8"), dict(PRODUCTS[1], sku="B8")]
    stats = _run(mig.apply_migration(swapped, COUNTERPARTIES, STOCK))
    assert stats["sku_duplicates"] == 0
    rows = _run(adb_core.fetch("SELECT legacy_ms_id, sku FROM products ORDER BY id"))
    assert [(r["legacy_ms_id"], r["sku"]) for r in rows] == [("uuid-p1", "G8"), ("uuid-p2", "B8")]


def test_extract_price_rules():
    sale = [_money(1250000, USD_ID, "Цена продажи"), _money(1100000, USD_ID, "Оптовая цена")]
    price, issues = mig_module().extract_price(
        _raw_product(sale=sale, buy=_money(900000, USD_ID)), ISO)
    assert issues == []
    assert price == {"sale_price_cents": 1250000, "wholesale_price_cents": 1100000,
                     "cost_price_cents": 900000, "currency": "USD"}

    # Закупочная и оптовая в другой валюте — не переносятся, с замечанием.
    sale2 = [_money(1250000, USD_ID), _money(9_000_000_00, UZS_ID, "опт")]
    price, issues = mig_module().extract_price(
        _raw_product(sale=sale2, buy=_money(100_000_000, UZS_ID)), ISO)
    assert price == {"sale_price_cents": 1250000, "wholesale_price_cents": None,
                     "cost_price_cents": None, "currency": "USD"}
    assert len(issues) == 2

    # Валюта не из разрешённых — цены нет вовсе.
    price, issues = mig_module().extract_price(_raw_product(sale=[_money(500, EUR_ID)]), ISO)
    assert price is None and "EUR" in issues[0]

    # Ничего нет — строки нет; только закупочная — строка в её валюте.
    assert mig_module().extract_price(_raw_product(sale=[_money(0, USD_ID)]), ISO) == (None, [])
    price, _ = mig_module().extract_price(_raw_product(buy=_money(700, UZS_ID)), ISO)
    assert price == {"sale_price_cents": None, "wholesale_price_cents": None,
                     "cost_price_cents": 700, "currency": "UZS"}

    # Валюта, которой нет в словаре, — не угадываем.
    price, issues = mig_module().extract_price(_raw_product(sale=[_money(5, "cur-x")]), ISO)
    assert price is None and "не найдена" in issues[0]


def mig_module():
    import scripts.migrate_from_moysklad as m

    return m


PRICE = {"sale_price_cents": 1250000, "wholesale_price_cents": None,
         "cost_price_cents": 900000, "currency": "USD"}


def _priced():
    return [dict(PRODUCTS[0], price=dict(PRICE), price_issues=[]),
            dict(PRODUCTS[1], price=None, price_issues=["«Гайка М8»: валюта EUR"])]


def test_prices_are_written_by_our_product_id_and_read_by_the_app(mig):
    from services import adb_core, database

    stats = _run(mig.apply_migration(_priced(), COUNTERPARTIES, STOCK))
    assert (stats["prices"], stats["prices_skipped"]) == (1, 1)
    pid = _run(adb_core.fetchval("SELECT id FROM products WHERE legacy_ms_id = 'uuid-p1'"))
    got = _run(database.get_product_prices_by_ids([str(pid)]))[str(pid)]
    assert got["sale_price_cents"] == 1250000 and got["currency"] == "USD"
    assert got["cost_price_cents"] == 900000
    assert _run(mig.verify(_priced(), COUNTERPARTIES, STOCK)) == []

    # Повтор обновляет цену, а не плодит строки.
    changed = _priced()
    changed[0]["price"]["sale_price_cents"] = 1300000
    _run(mig.apply_migration(changed, COUNTERPARTIES, STOCK))
    assert _run(adb_core.fetchval("SELECT COUNT(*) FROM product_prices")) == 1
    assert _run(adb_core.fetchval("SELECT sale_price_cents FROM product_prices")) == 1300000


def test_manual_price_edit_survives_rerun(mig):
    from services import adb_core

    _run(mig.apply_migration(_priced(), COUNTERPARTIES, STOCK))
    _run(adb_core.execute("UPDATE product_prices SET sale_price_cents = 1, updated_by = 42"))
    stats = _run(mig.apply_migration(_priced(), COUNTERPARTIES, STOCK))
    assert stats["prices_kept_manual"] == 1
    assert _run(adb_core.fetchval("SELECT sale_price_cents FROM product_prices")) == 1


def test_verify_catches_lost_prices(mig):
    from services import adb_core

    _run(mig.apply_migration(_priced(), COUNTERPARTIES, STOCK))
    _run(adb_core.execute("DELETE FROM product_prices"))
    problems = _run(mig.verify(_priced(), COUNTERPARTIES, STOCK))
    assert any("цены продажи" in p for p in problems)


def test_dry_run_reports_through_real_write_path_and_writes_nothing(mig):
    from services import adb_core

    products = [dict(p, sku="S") for p in _priced()]
    stock = {"uuid-p1": Decimal("-2"), "uuid-p2": Decimal("1")}
    stats = _run(mig.apply_migration(products, COUNTERPARTIES, stock, dry_run=True))
    assert stats["verify_problems"] == []
    assert (stats["stock_negative_clamped"], stats["sku_duplicates"], stats["prices"]) == (1, 1, 1)
    for table in ("products", "counterparties", "stock", "product_prices", "ms_id_map"):
        assert _run(adb_core.fetchval(f"SELECT COUNT(*) FROM {table}")) == 0, table


def test_pull_products_parses_ms_json_with_prices(mig, monkeypatch, caplog):
    base = "https://api.moysklad.ru/api/remap/1.2/entity"
    raw = [{
        "meta": {"href": f"{base}/product/uuid-p1"}, "id": "uuid-p1", "name": " Болт М8 ",
        "pathName": "Крепёж", "code": "B8", "article": "A-1", "uom": {"name": "шт"},
        "salePrices": [_money(1250000, USD_ID, "Цена продажи"),
                       _money(1000000, USD_ID, "Оптовая цена")],
        "buyPrice": _money(800000, USD_ID),
    }]
    currencies = [{"meta": {"href": f"{base}/currency/{USD_ID}"}, "id": USD_ID,
                   "name": "доллар", "isoCode": "USD"}]

    async def fake_fetch_all(path, params=None):
        return {"entity/product": raw, "entity/currency": currencies}[path]

    monkeypatch.setattr(mig, "_fetch_all", fake_fetch_all)
    caplog.set_level("INFO")
    products = _run(mig.pull_products())
    assert products[0]["name"] == "Болт М8" and products[0]["sku"] == "B8"
    assert products[0]["price"] == {"sale_price_cents": 1250000, "wholesale_price_cents": 1000000,
                                    "cost_price_cents": 800000, "currency": "USD"}
    assert "Оптовая цена" in caplog.text


def test_main_dry_run_prints_categories_and_writes_nothing(mig, monkeypatch, caplog):
    from services import adb_core

    products = [dict(p, sku="S") for p in _priced()]
    monkeypatch.setattr(mig, "pull_products", lambda: _async(products))
    monkeypatch.setattr(mig, "pull_counterparties", lambda: _async(COUNTERPARTIES))
    monkeypatch.setattr(mig, "pull_stock", lambda: _async({"uuid-p1": Decimal("-2")}))
    caplog.set_level("INFO")
    assert _run(mig.main("dry-run")) == 0
    text = caplog.text
    assert "ОТРИЦАТЕЛЬНЫЙ ОСТАТОК" in text and "дублей артикула: 1" in text
    assert "цены: не перенесено" in text
    assert _run(adb_core.fetchval("SELECT COUNT(*) FROM products")) == 0
