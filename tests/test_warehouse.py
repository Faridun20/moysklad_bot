"""Локальный складской учёт: движение остатков, нумерация, отмена.

Мокать здесь нечего — БД настоящая (isolated_db, SQLite в tmp_path),
внешних границ у модуля нет. Проверяем ровно те инварианты, ради которых
складской учёт и переезжает из МойСклад: остаток не уходит в минус,
накладная проводится целиком или никак, отмена возвращает ровно столько,
сколько списала.
"""

import asyncio

import pytest


@pytest.fixture
def wh(isolated_db):
    """Схема + склад + пара товаров + контрагент. Возвращает services.warehouse."""
    import importlib

    import services.warehouse as warehouse

    importlib.reload(warehouse)

    db = isolated_db
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(db.q("INSERT INTO warehouses (name) VALUES (?)"), ("Основной склад",))
        for name, sku in (("Болт М8", "B8"), ("Гайка М8", "G8"), ("Шайба", "SH")):
            cur.execute(
                db.q(
                    "INSERT INTO products (name, sku, unit, created_at) VALUES (?, ?, ?, ?)"
                ),
                (name, sku, "шт", db.now_str()),
            )
        cur.execute(
            db.q(
                "INSERT INTO counterparties (name, type, created_at) VALUES (?, ?, ?)"
            ),
            ("ООО Ромашка", "customer", db.now_str()),
        )
        conn.commit()
    return warehouse


def _run(coro):
    return asyncio.run(coro)


async def _qty(wh, product_id, warehouse_id=1):
    from services import adb_core

    v = await adb_core.fetchval(
        "SELECT quantity FROM stock WHERE product_id = $1 AND warehouse_id = $2",
        product_id,
        warehouse_id,
    )
    return float(v) if v is not None else 0.0


# ─── Приход ───────────────────────────────────────────────────────────────────


def test_incoming_creates_stock_row_for_new_product(wh):
    """UPSERT, а не UPDATE: у товара ещё нет строки в stock.

    Обычный UPDATE затронул бы 0 строк и приход потерялся бы молча —
    ровно тот случай, который отдельно оговорён в ТЗ.
    """

    async def go():
        res = await wh.create_invoice(
            invoice_type="incoming", warehouse_id=1,
            items=[{"product_id": 1, "quantity": 10, "price_cents": 5000}],
        )
        assert res["ok"], res
        assert res["invoice_number"].startswith("IN-")
        assert await _qty(wh, 1) == 10.0

    _run(go())


def test_incoming_accumulates(wh):
    async def go():
        for _ in range(3):
            r = await wh.create_invoice(
                invoice_type="incoming", warehouse_id=1,
                items=[{"product_id": 1, "quantity": 4, "price_cents": 100}],
            )
            assert r["ok"], r
        assert await _qty(wh, 1) == 12.0

    _run(go())


# ─── Расход ───────────────────────────────────────────────────────────────────


def test_outgoing_decrements(wh):
    async def go():
        await wh.create_invoice(
            invoice_type="incoming", warehouse_id=1,
            items=[{"product_id": 1, "quantity": 10, "price_cents": 100}],
        )
        res = await wh.create_invoice(
            invoice_type="outgoing", warehouse_id=1, counterparty_id=1,
            items=[{"product_id": 1, "quantity": 3, "price_cents": 25000}],
        )
        assert res["ok"], res
        assert res["invoice_number"].startswith("OUT-")
        # 3 × 250.00 = 750.00
        assert res["total_amount_cents"] == 75000
        assert await _qty(wh, 1) == 7.0

    _run(go())


def test_outgoing_without_price_rejected(wh):
    """Цена обязательна для расхода — из неё считается сумма в PDF клиенту."""

    async def go():
        await wh.create_invoice(
            invoice_type="incoming", warehouse_id=1,
            items=[{"product_id": 1, "quantity": 10, "price_cents": 100}],
        )
        res = await wh.create_invoice(
            invoice_type="outgoing", warehouse_id=1, counterparty_id=1,
            items=[{"product_id": 1, "quantity": 1}],
        )
        assert not res["ok"]
        assert res["code"] == "price_required"
        assert await _qty(wh, 1) == 10.0

    _run(go())


def test_outgoing_insufficient_rolls_back_whole_invoice(wh):
    """Нехватка по ОДНОЙ позиции откатывает всю накладную.

    Товар 1 в достатке, товара 2 не хватает — частичного списания
    быть не должно ни по одной позиции.
    """

    async def go():
        from services import adb_core

        await wh.create_invoice(
            invoice_type="incoming", warehouse_id=1,
            items=[
                {"product_id": 1, "quantity": 10, "price_cents": 100},
                {"product_id": 2, "quantity": 1, "price_cents": 100},
            ],
        )
        res = await wh.create_invoice(
            invoice_type="outgoing", warehouse_id=1, counterparty_id=1,
            items=[
                {"product_id": 1, "quantity": 5, "price_cents": 100},
                {"product_id": 2, "quantity": 5, "price_cents": 100},
            ],
        )
        assert not res["ok"]
        assert res["code"] == "insufficient_stock"
        # Остатки не тронуты ни по одной позиции.
        assert await _qty(wh, 1) == 10.0
        assert await _qty(wh, 2) == 1.0
        # Накладная не создана.
        out = await adb_core.fetchval(
            "SELECT COUNT(*) FROM invoices WHERE type = 'outgoing'"
        )
        assert out == 0

    _run(go())


def test_failed_invoice_does_not_burn_invoice_number(wh):
    """Откат забирает с собой и инкремент счётчика — дырок в нумерации нет.

    Номер выдаётся внутри той же транзакции, поэтому неудачная накладная
    не должна «съедать» номер: следующая успешная получает 0001.
    """

    async def go():
        bad = await wh.create_invoice(
            invoice_type="outgoing", warehouse_id=1, counterparty_id=1,
            items=[{"product_id": 1, "quantity": 1, "price_cents": 100}],
        )
        assert not bad["ok"] and bad["code"] == "insufficient_stock"

        await wh.create_invoice(
            invoice_type="incoming", warehouse_id=1,
            items=[{"product_id": 1, "quantity": 5, "price_cents": 100}],
        )
        good = await wh.create_invoice(
            invoice_type="outgoing", warehouse_id=1, counterparty_id=1,
            items=[{"product_id": 1, "quantity": 1, "price_cents": 100}],
        )
        assert good["ok"], good
        assert good["invoice_number"].endswith("-0001"), good["invoice_number"]

    _run(go())


def test_duplicate_product_lines_are_summed_before_check(wh):
    """Две строки одного товара складываются ДО проверки остатка.

    Без схлопывания накладная [A×6, A×6] при остатке 10 прошла бы обе
    построчные проверки (каждая видит 10 ≥ 6) и увела бы остаток в −2.
    """

    async def go():
        await wh.create_invoice(
            invoice_type="incoming", warehouse_id=1,
            items=[{"product_id": 1, "quantity": 10, "price_cents": 100}],
        )
        res = await wh.create_invoice(
            invoice_type="outgoing", warehouse_id=1, counterparty_id=1,
            items=[
                {"product_id": 1, "quantity": 6, "price_cents": 100},
                {"product_id": 1, "quantity": 6, "price_cents": 100},
            ],
        )
        assert not res["ok"]
        assert res["code"] == "insufficient_stock"
        assert await _qty(wh, 1) == 10.0

    _run(go())


def test_duplicate_product_with_conflicting_price_rejected(wh):
    async def go():
        await wh.create_invoice(
            invoice_type="incoming", warehouse_id=1,
            items=[{"product_id": 1, "quantity": 10, "price_cents": 100}],
        )
        res = await wh.create_invoice(
            invoice_type="outgoing", warehouse_id=1, counterparty_id=1,
            items=[
                {"product_id": 1, "quantity": 1, "price_cents": 100},
                {"product_id": 1, "quantity": 1, "price_cents": 200},
            ],
        )
        assert not res["ok"]
        assert res["code"] == "duplicate_price_conflict"

    _run(go())


# ─── Валидация ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "items,code",
    [
        ([], "empty_invoice"),
        ([{"product_id": 1, "quantity": 0, "price_cents": 1}], "bad_quantity"),
        ([{"product_id": 1, "quantity": -5, "price_cents": 1}], "bad_quantity"),
        ([{"product_id": 0, "quantity": 1, "price_cents": 1}], "bad_product_id"),
        ([{"product_id": 999, "quantity": 1, "price_cents": 1}], "unknown_product"),
        ([{"product_id": 1, "quantity": 1, "price_cents": -1}], "bad_price"),
    ],
)
def test_validation_rejects(wh, items, code):
    async def go():
        res = await wh.create_invoice(
            invoice_type="incoming", warehouse_id=1, items=items
        )
        assert not res["ok"]
        assert res["code"] == code, res

    _run(go())


def test_unknown_warehouse_and_counterparty_rejected(wh):
    async def go():
        r1 = await wh.create_invoice(
            invoice_type="incoming", warehouse_id=42,
            items=[{"product_id": 1, "quantity": 1, "price_cents": 1}],
        )
        assert not r1["ok"] and r1["code"] == "unknown_warehouse"

        r2 = await wh.create_invoice(
            invoice_type="incoming", warehouse_id=1, counterparty_id=99,
            items=[{"product_id": 1, "quantity": 1, "price_cents": 1}],
        )
        assert not r2["ok"] and r2["code"] == "unknown_counterparty"

    _run(go())


# ─── Нумерация ────────────────────────────────────────────────────────────────


def test_numbering_is_sequential_and_per_type(wh):
    """IN и OUT нумеруются независимо, счётчик — свой на (тип, год)."""

    async def go():
        nums_in, nums_out = [], []
        for _ in range(3):
            r = await wh.create_invoice(
                invoice_type="incoming", warehouse_id=1,
                items=[{"product_id": 1, "quantity": 5, "price_cents": 100}],
            )
            nums_in.append(r["invoice_number"])
            r = await wh.create_invoice(
                invoice_type="outgoing", warehouse_id=1, counterparty_id=1,
                items=[{"product_id": 1, "quantity": 1, "price_cents": 100}],
            )
            nums_out.append(r["invoice_number"])

        year = nums_in[0].split("-")[1]
        assert nums_in == [f"IN-{year}-000{i}" for i in (1, 2, 3)]
        assert nums_out == [f"OUT-{year}-000{i}" for i in (1, 2, 3)]

    _run(go())


def test_numbering_year_comes_from_invoice_date(wh):
    """Счётчик ведётся по году НАКЛАДНОЙ, а не по текущему.

    Накладную задним числом проводят в начале года регулярно; если брать
    текущий год, декабрьский документ получит номер из следующей серии.
    """

    async def go():
        r = await wh.create_invoice(
            invoice_type="incoming", warehouse_id=1, invoice_date="2019-12-31",
            items=[{"product_id": 1, "quantity": 1, "price_cents": 100}],
        )
        assert r["invoice_number"] == "IN-2019-0001", r

    _run(go())


# ─── Отмена ───────────────────────────────────────────────────────────────────


def test_cancel_outgoing_returns_stock(wh):
    async def go():
        await wh.create_invoice(
            invoice_type="incoming", warehouse_id=1,
            items=[{"product_id": 1, "quantity": 10, "price_cents": 100}],
        )
        out = await wh.create_invoice(
            invoice_type="outgoing", warehouse_id=1, counterparty_id=1,
            items=[{"product_id": 1, "quantity": 4, "price_cents": 100}],
        )
        assert await _qty(wh, 1) == 6.0

        res = await wh.cancel_invoice(out["invoice_id"], cancelled_by=7)
        assert res["ok"], res
        assert await _qty(wh, 1) == 10.0

        inv = await wh.get_invoice(out["invoice_id"])
        assert inv["status"] == "cancelled"
        assert inv["cancelled_by"] == 7

    _run(go())


def test_cancel_incoming_removes_stock(wh):
    async def go():
        inc = await wh.create_invoice(
            invoice_type="incoming", warehouse_id=1,
            items=[{"product_id": 1, "quantity": 10, "price_cents": 100}],
        )
        res = await wh.cancel_invoice(inc["invoice_id"])
        assert res["ok"], res
        assert await _qty(wh, 1) == 0.0

    _run(go())


def test_cancel_incoming_blocked_when_goods_already_shipped(wh):
    """Откат прихода увёл бы остаток в минус — отмена блокируется целиком."""

    async def go():
        inc = await wh.create_invoice(
            invoice_type="incoming", warehouse_id=1,
            items=[{"product_id": 1, "quantity": 10, "price_cents": 100}],
        )
        await wh.create_invoice(
            invoice_type="outgoing", warehouse_id=1, counterparty_id=1,
            items=[{"product_id": 1, "quantity": 8, "price_cents": 100}],
        )
        res = await wh.cancel_invoice(inc["invoice_id"])
        assert not res["ok"]
        assert res["code"] == "insufficient_stock"
        # Ни остаток, ни статус не тронуты.
        assert await _qty(wh, 1) == 2.0
        inv = await wh.get_invoice(inc["invoice_id"])
        assert inv["status"] == "confirmed"

    _run(go())


def test_double_cancel_is_idempotent(wh):
    """Вторая отмена не двигает остаток ещё раз."""

    async def go():
        inc = await wh.create_invoice(
            invoice_type="incoming", warehouse_id=1,
            items=[{"product_id": 1, "quantity": 5, "price_cents": 100}],
        )
        assert (await wh.cancel_invoice(inc["invoice_id"]))["ok"]
        second = await wh.cancel_invoice(inc["invoice_id"])
        assert not second["ok"]
        assert second["code"] == "already_cancelled"
        assert await _qty(wh, 1) == 0.0

    _run(go())


def test_cancel_unknown_invoice(wh):
    async def go():
        res = await wh.cancel_invoice(4242)
        assert not res["ok"] and res["code"] == "not_found"

    _run(go())


# ─── Деньги ───────────────────────────────────────────────────────────────────


def test_total_uses_decimal_rounding_not_float(wh):
    """Сумма считается через money.mul_qty (Decimal), а не float-умножением.

    0.1 + 0.2 в float даёт 0.30000000000000004; цена 33.33 × 3 шт через
    float*100 уехала бы на копейку. Проверяем точное значение.
    """

    async def go():
        await wh.create_invoice(
            invoice_type="incoming", warehouse_id=1,
            items=[{"product_id": 1, "quantity": 10, "price_cents": 100}],
        )
        res = await wh.create_invoice(
            invoice_type="outgoing", warehouse_id=1, counterparty_id=1,
            items=[{"product_id": 1, "quantity": 3, "price_cents": 3333}],
        )
        assert res["total_amount_cents"] == 9999

    _run(go())


def test_fractional_quantity_rounds_half_up(wh):
    async def go():
        await wh.create_invoice(
            invoice_type="incoming", warehouse_id=1,
            items=[{"product_id": 1, "quantity": 10, "price_cents": 100}],
        )
        # 2.5 × 1.01 = 2.525 → 253 копейки (ROUND_HALF_UP), не 252.
        res = await wh.create_invoice(
            invoice_type="outgoing", warehouse_id=1, counterparty_id=1,
            items=[{"product_id": 1, "quantity": 2.5, "price_cents": 101}],
        )
        assert res["total_amount_cents"] == 253
        assert await _qty(wh, 1) == 7.5

    _run(go())


# ─── Чтения ───────────────────────────────────────────────────────────────────


def test_get_invoice_returns_items_with_names(wh):
    async def go():
        inc = await wh.create_invoice(
            invoice_type="incoming", warehouse_id=1, counterparty_id=1,
            items=[
                {"product_id": 1, "quantity": 2, "price_cents": 100},
                {"product_id": 2, "quantity": 3, "price_cents": 200},
            ],
        )
        inv = await wh.get_invoice(inc["invoice_id"])
        assert inv["counterparty_name"] == "ООО Ромашка"
        assert inv["warehouse_name"] == "Основной склад"
        assert [i["product_name"] for i in inv["items"]] == ["Болт М8", "Гайка М8"]

    _run(go())


def test_list_invoices_filters_by_type_newest_first(wh):
    async def go():
        await wh.create_invoice(
            invoice_type="incoming", warehouse_id=1,
            items=[{"product_id": 1, "quantity": 10, "price_cents": 100}],
        )
        await wh.create_invoice(
            invoice_type="outgoing", warehouse_id=1, counterparty_id=1,
            items=[{"product_id": 1, "quantity": 1, "price_cents": 100}],
        )
        rows = await wh.list_invoices(invoice_type="outgoing")
        assert len(rows) == 1
        assert rows[0]["type"] == "outgoing"

        allrows = await wh.list_invoices()
        assert [r["type"] for r in allrows] == ["outgoing", "incoming"]

    _run(go())


def test_get_stock_reports_zero_for_untouched_product(wh):
    """Товар без движений виден с нулём, а не пропадает из списка."""

    async def go():
        await wh.create_invoice(
            invoice_type="incoming", warehouse_id=1,
            items=[{"product_id": 1, "quantity": 10, "price_cents": 100}],
        )
        rows = await wh.get_stock()
        by_name = {r["name"]: float(r["quantity"]) for r in rows}
        assert by_name["Болт М8"] == 10.0
        assert by_name["Шайба"] == 0.0

        positive = await wh.get_stock(only_positive=True)
        assert {r["name"] for r in positive} == {"Болт М8"}

    _run(go())


def test_mark_telegram_sent(wh):
    async def go():
        inc = await wh.create_invoice(
            invoice_type="outgoing", warehouse_id=1, counterparty_id=1,
            items=[{"product_id": 1, "quantity": 1, "price_cents": 100}],
        )
        # Остатка нет — накладная не прошла, отмечать нечего.
        assert not inc["ok"]

        await wh.create_invoice(
            invoice_type="incoming", warehouse_id=1,
            items=[{"product_id": 1, "quantity": 5, "price_cents": 100}],
        )
        out = await wh.create_invoice(
            invoice_type="outgoing", warehouse_id=1, counterparty_id=1,
            items=[{"product_id": 1, "quantity": 1, "price_cents": 100}],
        )
        assert await wh.mark_telegram_sent(out["invoice_id"])
        inv = await wh.get_invoice(out["invoice_id"])
        assert inv["telegram_sent"] == 1

    _run(go())
