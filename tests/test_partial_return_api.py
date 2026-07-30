"""T3.1 — WebApp получает то, чего у него не было.

Главное здесь — ЧАСТИЧНЫЙ возврат: он существовал только в боте (§5.2.6),
а `/api/returns/create` жёстко слал "full" и возвращал все позиции целиком.
Без этого урезать бота в T3.3 нельзя — операция стала бы недоступна.

Остальные три пункта (кнопки «на доработку», «товар получен», экран курсов)
— чисто фронтовые: эндпоинты уже были, тесты на них есть.
"""

import asyncio
import importlib

import pytest
from fastapi.testclient import TestClient

import webapp.server as server


@pytest.fixture
def env(isolated_db, monkeypatch):
    db = isolated_db
    boss, mgr, other = 800, 801, 802
    db.set_role(boss, "boss", "Boss", "boss")
    db.set_role(mgr, "mgr", "Mgr", "manager")
    db.set_role(other, "other", "Other", "manager")

    import services.rate_limit as rate_limit
    import services.roles as roles

    importlib.reload(roles)
    rate_limit.reset()  # returns/create — 10 запросов/мин, тесты делят счётчик
    monkeypatch.setattr(
        server,
        "verify_init_data",
        lambda init_data: {"id": int(init_data), "first_name": "U", "username": "u"},
    )
    return TestClient(server.app), db, {"boss": boss, "mgr": mgr, "other": other}


def _shipped(db, mgr, qty=5, price=100.0):
    oid = db.create_order(mgr, "Mgr", "")
    iid = db.add_order_item(oid, "Товар", "href", qty, "шт", price)
    db.update_order_agent(oid, "A-1", "Клиент")
    db.update_order_status(oid, "pending")
    db.update_order_status(oid, "approved")
    db.update_order_status(oid, "shipped")
    return oid, iid


def _return_row(db, rid):
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(db.q("SELECT return_type, total_amount_cents FROM returns WHERE id = ?"), (rid,))
        r = cur.fetchone()
        return {"return_type": r[0], "total_amount_cents": r[1]}


def _return_items(db, rid):
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(
            db.q("SELECT order_item_id, qty, amount_cents FROM return_items WHERE return_id = ?"),
            (rid,),
        )
        return [{"item_id": r[0], "qty": float(r[1]), "cents": r[2]} for r in cur.fetchall()]


# ─── /api/returns/positions ─────────────────────────────────────────────────


def test_positions_lists_returnable_items(env):
    client, db, ids = env
    oid, iid = _shipped(db, ids["mgr"])

    r = client.post(
        "/api/returns/positions", json={"initData": str(ids["mgr"]), "order_id": oid}
    )

    assert r.status_code == 200, r.text
    pos = r.json()["positions"]
    assert len(pos) == 1
    assert pos[0]["item_id"] == iid
    assert pos[0]["available"] == 5
    assert pos[0]["price"] == 100.0


def test_positions_subtracts_already_returned(env):
    """Позиция, частично возвращённая раньше, доступна только на остаток."""
    client, db, ids = env
    oid, iid = _shipped(db, ids["mgr"])
    res = asyncio.run(
        db.create_return(oid, "partial", "брак", [(iid, 2, 200.0)], refund_method="no_refund", created_by=ids["mgr"])
    )
    asyncio.run(db.mark_return_goods_received(res["return_id"], ids["boss"]))
    asyncio.run(db.confirm_return(res["return_id"], ids["boss"], "Boss"))

    r = client.post(
        "/api/returns/positions", json={"initData": str(ids["mgr"]), "order_id": oid}
    )
    assert r.json()["positions"][0]["available"] == 3


def test_positions_hides_fully_returned_items(env):
    """Полностью возвращённая позиция исчезает из списка, остальные остаются.

    Заказ при этом ещё живой (partially_returned) — если вернуть ВСЁ, он уйдёт
    в 'returned' и эндпоинт закономерно ответит 409 «возврат недоступен».
    """
    client, db, ids = env
    oid, iid = _shipped(db, ids["mgr"], qty=5)
    second = db.add_order_item(oid, "Второй товар", "href2", 3, "шт", 50.0)

    res = asyncio.run(
        db.create_return(
            oid, "partial", "брак", [(iid, 5, 500.0)],
            refund_method="no_refund", created_by=ids["mgr"],
        )
    )
    asyncio.run(db.mark_return_goods_received(res["return_id"], ids["boss"]))
    asyncio.run(db.confirm_return(res["return_id"], ids["boss"], "Boss"))

    r = client.post(
        "/api/returns/positions", json={"initData": str(ids["mgr"]), "order_id": oid}
    )
    assert r.status_code == 200, r.text
    pos = r.json()["positions"]
    assert [p["item_id"] for p in pos] == [second], "возвращённая позиция должна исчезнуть"
    assert pos[0]["available"] == 3


def test_positions_respects_ownership(env):
    """Менеджер не видит позиции чужого заказа — те же гейты, что в create."""
    client, db, ids = env
    oid, _iid = _shipped(db, ids["mgr"])

    assert client.post(
        "/api/returns/positions", json={"initData": str(ids["other"]), "order_id": oid}
    ).status_code == 403
    # Начальству — можно.
    assert client.post(
        "/api/returns/positions", json={"initData": str(ids["boss"]), "order_id": oid}
    ).status_code == 200


def test_positions_rejects_non_shipped_order(env):
    client, db, ids = env
    oid = db.create_order(ids["mgr"], "Mgr", "")
    db.add_order_item(oid, "T", "href", 1, "шт", 10.0)

    r = client.post(
        "/api/returns/positions", json={"initData": str(ids["mgr"]), "order_id": oid}
    )
    assert r.status_code == 409


# ─── Частичный возврат через /api/returns/create ───────────────────────────


def test_partial_return_creates_partial_type(env):
    """Вернули 2 из 5 → return_type='partial', сумма по выбранному количеству."""
    client, db, ids = env
    oid, iid = _shipped(db, ids["mgr"], qty=5, price=100.0)

    r = client.post(
        "/api/returns/create",
        json={
            "initData": str(ids["mgr"]),
            "order_id": oid,
            "reason": "две штуки бракованные",
            "refund_method": "no_refund",
            "items": [{"item_id": iid, "quantity": 2}],
        },
    )

    assert r.status_code == 200, r.text
    rid = r.json()["return_id"]
    assert _return_row(db, rid)["return_type"] == "partial"
    items = _return_items(db, rid)
    assert items == [{"item_id": iid, "qty": 2.0, "cents": 20000}]


def test_selecting_everything_is_treated_as_full(env):
    """Выбраны все позиции целиком — это фактически полный возврат.
    От типа зависит итоговый статус заказа, поэтому важно."""
    client, db, ids = env
    oid, iid = _shipped(db, ids["mgr"], qty=5)

    r = client.post(
        "/api/returns/create",
        json={
            "initData": str(ids["mgr"]),
            "order_id": oid,
            "reason": "весь заказ брак",
            "refund_method": "no_refund",
            "items": [{"item_id": iid, "quantity": 5}],
        },
    )
    assert r.status_code == 200, r.text
    assert _return_row(db, r.json()["return_id"])["return_type"] == "full"


def test_omitting_items_still_means_full_return(env):
    """Прежнее поведение сохранено: без items — полный возврат."""
    client, db, ids = env
    oid, iid = _shipped(db, ids["mgr"], qty=5)

    r = client.post(
        "/api/returns/create",
        json={
            "initData": str(ids["mgr"]),
            "order_id": oid,
            "reason": "полный возврат",
            "refund_method": "no_refund",
        },
    )
    assert r.status_code == 200, r.text
    rid = r.json()["return_id"]
    assert _return_row(db, rid)["return_type"] == "full"
    assert _return_items(db, rid)[0]["qty"] == 5.0


@pytest.mark.parametrize("qty", [0, -1, 6])
def test_bad_quantity_is_rejected(env, qty):
    """Больше доступного, ноль, отрицательное — 400, возврат не создан."""
    client, db, ids = env
    oid, iid = _shipped(db, ids["mgr"], qty=5)

    r = client.post(
        "/api/returns/create",
        json={
            "initData": str(ids["mgr"]),
            "order_id": oid,
            "reason": "проверка",
            "refund_method": "no_refund",
            "items": [{"item_id": iid, "quantity": qty}],
        },
    )
    assert r.status_code == 400, r.text
    assert "количество" in r.json()["detail"].lower()
    assert asyncio.run(db.get_pending_returns()) == []


def test_non_finite_quantity_is_rejected(env):
    """inf/nan строгий JSON не кодирует, но json.loads на сервере их ПРИНИМАЕТ —
    поэтому шлём сырым телом и проверяем, что isfinite отсекает."""
    client, db, ids = env
    oid, iid = _shipped(db, ids["mgr"], qty=5)

    body = (
        f'{{"initData": "{ids["mgr"]}", "order_id": {oid}, "reason": "inf-проба", '
        f'"refund_method": "no_refund", '
        f'"items": [{{"item_id": {iid}, "quantity": Infinity}}]}}'
    )
    r = client.post(
        "/api/returns/create",
        content=body.encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 400, r.text
    assert asyncio.run(db.get_pending_returns()) == []


def test_foreign_item_id_is_rejected(env):
    """Позиция из другого заказа не должна проходить."""
    client, db, ids = env
    oid, _iid = _shipped(db, ids["mgr"])
    _other_oid, other_iid = _shipped(db, ids["mgr"])

    r = client.post(
        "/api/returns/create",
        json={
            "initData": str(ids["mgr"]),
            "order_id": oid,
            "reason": "чужая позиция",
            "refund_method": "no_refund",
            "items": [{"item_id": other_iid, "quantity": 1}],
        },
    )
    assert r.status_code == 400
    assert "недоступна" in r.json()["detail"]


def test_empty_items_list_is_rejected(env):
    client, db, ids = env
    oid, _iid = _shipped(db, ids["mgr"])

    r = client.post(
        "/api/returns/create",
        json={
            "initData": str(ids["mgr"]),
            "order_id": oid,
            "reason": "ничего не выбрано",
            "refund_method": "no_refund",
            "items": [],
        },
    )
    assert r.status_code == 400
    assert "позици" in r.json()["detail"].lower()


def test_malformed_item_row_is_rejected(env):
    client, db, ids = env
    oid, _iid = _shipped(db, ids["mgr"])

    r = client.post(
        "/api/returns/create",
        json={
            "initData": str(ids["mgr"]),
            "order_id": oid,
            "reason": "кривая строка",
            "refund_method": "no_refund",
            "items": [{"quantity": 1}],  # нет item_id
        },
    )
    assert r.status_code == 400


def test_partial_then_rest_covers_whole_order(env):
    """Два частичных возврата подряд закрывают заказ целиком —
    остаток пересчитывается между ними."""
    client, db, ids = env
    oid, iid = _shipped(db, ids["mgr"], qty=5)

    first = client.post(
        "/api/returns/create",
        json={
            "initData": str(ids["mgr"]), "order_id": oid, "reason": "первая часть",
            "refund_method": "no_refund", "items": [{"item_id": iid, "quantity": 2}],
        },
    )
    assert first.status_code == 200, first.text
    rid1 = first.json()["return_id"]
    asyncio.run(db.mark_return_goods_received(rid1, ids["boss"]))
    asyncio.run(db.confirm_return(rid1, ids["boss"], "Boss"))

    # Осталось 3 — просим 4, должно отказать.
    too_many = client.post(
        "/api/returns/create",
        json={
            "initData": str(ids["mgr"]), "order_id": oid, "reason": "перебор",
            "refund_method": "no_refund", "items": [{"item_id": iid, "quantity": 4}],
        },
    )
    assert too_many.status_code == 400

    rest = client.post(
        "/api/returns/create",
        json={
            "initData": str(ids["mgr"]), "order_id": oid, "reason": "остаток",
            "refund_method": "no_refund", "items": [{"item_id": iid, "quantity": 3}],
        },
    )
    assert rest.status_code == 200, rest.text
    # Все доступные позиции выбраны целиком → трактуем как полный возврат.
    assert _return_row(db, rest.json()["return_id"])["return_type"] == "full"
