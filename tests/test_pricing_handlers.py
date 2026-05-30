"""
Тесты бот-команд /rates и /prices (handlers/pricing) + snapshot.search_products.

Хендлеры aiogram зовём напрямую с фейковыми объектами через asyncio.run
(pytest-asyncio в проекте нет). DB — настоящая (isolated_db), мок только
объекты Telegram. #24
"""

import asyncio

import services.roles as roles
from services import snapshot


class _FakeUser:
    def __init__(self, uid, full_name="User"):
        self.id = uid
        self.full_name = full_name


class _FakeChat:
    id = 1


class _FakeMessage:
    def __init__(self, text="", uid=1):
        self.text = text
        self.from_user = _FakeUser(uid)
        self.chat = _FakeChat()
        self.message_id = 100
        self.answers = []

    async def answer(self, text, **kwargs):
        self.answers.append((text, kwargs))


class _FakeCall:
    def __init__(self, data, uid):
        self.data = data
        self.from_user = _FakeUser(uid)
        self.message = _FakeMessage(uid=uid)
        self.alerts = []

    async def answer(self, text="", **kwargs):
        self.alerts.append((text, kwargs))


class _FakeState:
    def __init__(self):
        self._data = {}
        self.state = None

    async def set_state(self, s):
        self.state = s

    async def update_data(self, **kw):
        self._data.update(kw)

    async def get_data(self):
        return dict(self._data)

    async def clear(self):
        self._data = {}
        self.state = None


def _roles(db):
    roles.invalidate_all_roles()
    # Курсы/цены кэшируются в module-level dict'ах — чистим, чтобы значения из
    # предыдущего теста (другая БД) не протекали сюда.
    db._invalidate_currency_rates_cache()
    db._invalidate_product_price_cache()
    db.set_role(1, "mgr", "Manager", "manager")
    db.set_role(2, "boss", "Boss", "boss")


# ─── /rates ──────────────────────────────────────────────────────────────────


def test_cmd_rates_lists_for_boss(isolated_db):
    from handlers.pricing import cmd_rates

    db = isolated_db
    _roles(db)
    db.set_currency_rate("UZS", 0.00008, 2)
    msg = _FakeMessage(text="/rates", uid=2)
    asyncio.run(cmd_rates(msg))
    text, kw = msg.answers[-1]
    assert "UZS" in text
    assert kw.get("reply_markup") is not None  # кнопки выбора валюты


def test_cmd_rates_denied_for_manager(isolated_db):
    from handlers.pricing import cmd_rates

    db = isolated_db
    _roles(db)
    msg = _FakeMessage(text="/rates", uid=1)
    asyncio.run(cmd_rates(msg))
    assert any("доступн" in t.lower() for t, _ in msg.answers)


def test_set_rate_flow_persists(isolated_db):
    from handlers.pricing import process_rate

    db = isolated_db
    _roles(db)
    state = _FakeState()
    asyncio.run(state.update_data(code="UZS"))
    msg = _FakeMessage(text="0.000079", uid=2)
    asyncio.run(process_rate(msg, state))
    assert db.get_currency_rate("UZS") == 0.000079


def test_set_rate_rejects_bad_input(isolated_db):
    from handlers.pricing import process_rate

    db = isolated_db
    _roles(db)
    state = _FakeState()
    asyncio.run(state.update_data(code="UZS"))
    msg = _FakeMessage(text="ноль", uid=2)
    asyncio.run(process_rate(msg, state))
    assert any("число" in t.lower() for t, _ in msg.answers)
    assert db.get_currency_rate("UZS") is None


# ─── snapshot search ─────────────────────────────────────────────────────────


def _seed_stock(db, ms_id, name):
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(
            db.q("INSERT INTO ms_stock (ms_id, name, unit, stock, reserve) VALUES (?, ?, ?, ?, ?)"),
            (ms_id, name, "шт", 10, 0),
        )
        conn.commit()


def test_search_products_and_get_product(isolated_db):
    db = isolated_db
    _seed_stock(db, "p-1", "Вода питьевая 0.5л")
    _seed_stock(db, "p-2", "Сок яблочный")
    found = snapshot.search_products("вода", 10)
    assert [p["ms_id"] for p in found] == ["p-1"]
    assert snapshot.get_product("p-2")["name"] == "Сок яблочный"
    assert snapshot.get_product("nope") is None


# ─── /prices ─────────────────────────────────────────────────────────────────


def test_cmd_prices_denied_for_manager(isolated_db):
    from handlers.pricing import cmd_prices

    db = isolated_db
    _roles(db)
    msg = _FakeMessage(text="/prices", uid=1)
    asyncio.run(cmd_prices(msg))
    assert any("доступн" in t.lower() for t, _ in msg.answers)


def test_cmd_prices_search_lists_catalog(isolated_db):
    from handlers.pricing import cmd_prices

    db = isolated_db
    _roles(db)
    _seed_stock(db, "p-9", "Молоко 1л")
    msg = _FakeMessage(text="/prices молоко", uid=2)
    asyncio.run(cmd_prices(msg))
    _text, kw = msg.answers[-1]
    assert kw.get("reply_markup") is not None  # кнопка выбора товара


def test_price_set_flow_preserves_existing_cost(isolated_db):
    """Если при правке указали только цену продажи — себестоимость не затирается
    в NULL (set_product_price UPSERT'ит переданные значения как есть)."""
    from handlers.pricing import process_price

    db = isolated_db
    _roles(db)
    db.set_product_price("p-5", "Хлеб", 150.0, 100.0, "USD", 2)

    state = _FakeState()
    # cb_price_set положил бы в state прежнюю себестоимость:
    asyncio.run(state.update_data(ms_id="p-5", name="Хлеб", cur_cost=100.0, cur_currency="USD"))
    msg = _FakeMessage(text="200", uid=2)  # только продажа
    asyncio.run(process_price(msg, state))

    row = db.get_product_price("p-5")
    assert row["sale_price"] == 200.0
    assert row["cost_price"] == 100.0  # сохранилась прежняя


def test_price_set_flow_updates_both(isolated_db):
    from handlers.pricing import process_price

    db = isolated_db
    _roles(db)
    state = _FakeState()
    asyncio.run(state.update_data(ms_id="p-6", name="Сахар", cur_cost=None, cur_currency="USD"))
    msg = _FakeMessage(text="80 55", uid=2)
    asyncio.run(process_price(msg, state))
    row = db.get_product_price("p-6")
    assert row["sale_price"] == 80.0 and row["cost_price"] == 55.0
