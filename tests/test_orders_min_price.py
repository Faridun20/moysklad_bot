"""
Бот: минимальная цена продажи прямо в потоке создания заказа.

Раньше менеджер не видел минимальную цену при вводе и шёл сверять её в
каталог. Теперь cb_prod_pick показывает минимум, а process_qty_price
повторяет правила WebApp: пустая цена → префилл минимумом; ниже минимума →
отказ. Эти тесты проверяют префилл и запрет.
"""

import asyncio


class _FakeUser:
    def __init__(self, uid, full_name="Manager"):
        self.id = uid
        self.full_name = full_name


class _FakeMessage:
    def __init__(self, text, uid=1):
        self.text = text
        self.from_user = _FakeUser(uid)
        self.answers = []

    async def answer(self, text, **kwargs):
        self.answers.append((text, kwargs))


class _FakeState:
    def __init__(self, data):
        self._data = dict(data)

    async def get_data(self):
        return self._data

    async def update_data(self, **kw):
        self._data.update(kw)

    async def clear(self):
        self._data = {}

    async def set_state(self, _s):
        pass


def _state_for(oid, sale_min):
    return _FakeState(
        {
            "order_id": oid,
            "selected_product": {
                "name": "Товар",
                "href": "https://x/entity/product/abc",
                "ms_id": "abc",
                "unit": "шт",
                "stock": 10,
                "sale_min": sale_min,
            },
        }
    )


def test_only_qty_prefills_min_price(isolated_db):
    db = isolated_db
    from handlers.orders import process_qty_price

    db.set_role(1, "mgr", "Manager", "manager")
    oid = db.create_order(1, "Manager", "")

    msg = _FakeMessage("5", uid=1)  # только количество
    asyncio.run(process_qty_price(msg, _state_for(oid, sale_min=150.0)))

    items = asyncio.run(db.get_order_items(oid))
    assert len(items) == 1
    assert items[0]["price"] == 150.0  # подставлен минимум
    assert items[0]["price_cents"] == 15000


def test_price_below_min_rejected(isolated_db):
    db = isolated_db
    from handlers.orders import process_qty_price

    db.set_role(1, "mgr", "Manager", "manager")
    oid = db.create_order(1, "Manager", "")

    msg = _FakeMessage("5 100", uid=1)  # ниже минимума 150
    asyncio.run(process_qty_price(msg, _state_for(oid, sale_min=150.0)))

    assert asyncio.run(db.get_order_items(oid)) == []  # позиция не добавлена
    assert any("минимальной" in t for t, _ in msg.answers)


def test_price_above_min_accepted(isolated_db):
    db = isolated_db
    from handlers.orders import process_qty_price

    db.set_role(1, "mgr", "Manager", "manager")
    oid = db.create_order(1, "Manager", "")

    msg = _FakeMessage("5 200", uid=1)  # выше минимума — ок
    asyncio.run(process_qty_price(msg, _state_for(oid, sale_min=150.0)))

    items = asyncio.run(db.get_order_items(oid))
    assert len(items) == 1
    assert items[0]["price"] == 200.0


def test_no_min_keeps_legacy_behavior(isolated_db):
    db = isolated_db
    from handlers.orders import process_qty_price

    db.set_role(1, "mgr", "Manager", "manager")
    oid = db.create_order(1, "Manager", "")

    msg = _FakeMessage("5", uid=1)  # без минимума только qty → цена 0
    asyncio.run(process_qty_price(msg, _state_for(oid, sale_min=None)))

    items = asyncio.run(db.get_order_items(oid))
    assert len(items) == 1
    assert items[0]["price"] == 0
