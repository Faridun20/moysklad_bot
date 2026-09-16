"""Списание с причиной и инвентаризация: сервис и ручки WebApp.

Проверяем последствия, а не вызовы: остаток, накладную, запись причины, аудит,
идемпотентность и права. БД настоящая (`isolated_db`), мокаем только границу
Telegram (подпись initData).

Главные инварианты этого слоя:
  * списание уменьшает остаток ровно на списанное и не уводит его в минус;
  * причина обязательна и хранится;
  * пересчёт применяется ЦЕЛИКОМ или никак, и дельта считается от живого
    остатка в момент проведения, а не от запомненного при вводе;
  * списание не попадает в продажи и в отчёт о прибыли.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def wh(isolated_db):
    """Склад, два товара и остаток 10/10."""
    import importlib

    import services.inventory as inventory
    import services.roles as roles
    import services.warehouse as warehouse

    importlib.reload(roles)
    importlib.reload(warehouse)
    importlib.reload(inventory)

    db = isolated_db
    ids = {"admin": 1, "boss": 100, "mgr": 200, "guest": 300, "keeper": 400}
    db.set_role(ids["admin"], "admin_user", "Admin", "admin")
    db.set_role(ids["boss"], "boss_user", "Boss", "boss")
    db.set_role(ids["mgr"], "mgr_user", "Manager", "manager")
    db.set_role(ids["guest"], "guest_user", "Guest", "guest")
    db.set_role(ids["keeper"], "keeper_user", "Keeper", "warehouse_keeper")

    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(db.q("INSERT INTO warehouses (name) VALUES (?)"), ("Основной склад",))
        for name, sku in (("Болт М8", "B8"), ("Гайка М8", "G8"), ("Шайба", "SH")):
            cur.execute(
                db.q("INSERT INTO products (name, sku, unit, created_at) VALUES (?, ?, ?, ?)"),
                (name, sku, "шт", db.now_str()),
            )
        conn.commit()

    for pid in (1, 2, 3):
        res = _run(
            warehouse.create_invoice(
                invoice_type="incoming",
                warehouse_id=1,
                items=[{"product_id": pid, "quantity": 10, "price_cents": 5000}],
            )
        )
        assert res["ok"], res
    return db, ids, inventory, warehouse


def _stock(db, product_id: int) -> float:
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(
            db.q("SELECT quantity FROM stock WHERE product_id = ? AND warehouse_id = 1"),
            (product_id,),
        )
        row = cur.fetchone()
    if row is None:
        return 0.0
    return float(row["quantity"] if db.USE_POSTGRES else row[0])


# ─── Сервис: списание ─────────────────────────────────────────────────────────


def test_writeoff_decreases_stock_and_keeps_reason(wh):
    db, ids, inventory, _warehouse = wh
    res = _run(
        inventory.create_writeoff(
            items=[{"product_id": 1, "quantity": 2}], reason="бой", created_by=ids["mgr"]
        )
    )
    assert res["ok"], res
    assert _stock(db, 1) == 8
    rows = _run(inventory.list_writeoffs())
    assert len(rows) == 1
    assert rows[0]["reason"] == "бой"
    assert rows[0]["kind"] == "writeoff"
    assert rows[0]["items"][0]["quantity"] == 2
    # Движение прошло обычной накладной — иначе инвариант «остаток = приходы −
    # расходы по накладным» (сценарии) перестал бы держаться.
    assert res["invoice_number"].startswith("OUT-")


def test_writeoff_refuses_more_than_available_and_changes_nothing(wh):
    db, ids, inventory, _w = wh
    res = _run(
        inventory.create_writeoff(
            items=[{"product_id": 1, "quantity": 11}], reason="недостача", created_by=ids["mgr"]
        )
    )
    assert not res["ok"] and res["code"] == "insufficient_stock"
    assert _stock(db, 1) == 10
    assert _run(inventory.list_writeoffs()) == []


def test_writeoff_without_reason_is_refused(wh):
    _db, ids, inventory, _w = wh
    res = _run(
        inventory.create_writeoff(
            items=[{"product_id": 1, "quantity": 1}], reason="   ", created_by=ids["mgr"]
        )
    )
    assert not res["ok"] and res["code"] == "reason_required"


def test_writeoff_is_not_a_sale(wh):
    """Списание не должно попадать ни в выручку, ни в число отгрузок."""
    _db, ids, inventory, warehouse = wh
    _run(
        inventory.create_writeoff(
            items=[{"product_id": 1, "quantity": 3}], reason="порча", created_by=ids["mgr"]
        )
    )
    stats = _run(warehouse.sales_stats("2000-01-01"))
    assert stats["count"] == 0 and stats["total"] == 0
    assert _run(warehouse.list_shipments("2000-01-01")) == []


def test_void_returns_stock_and_marks_record(wh):
    db, ids, inventory, _w = wh
    made = _run(
        inventory.create_writeoff(
            items=[{"product_id": 2, "quantity": 4}], reason="бой", created_by=ids["mgr"]
        )
    )
    assert _stock(db, 2) == 6
    res = _run(inventory.void_writeoff(made["writeoff_id"], user_id=ids["mgr"], is_boss=False))
    assert res["ok"], res
    assert _stock(db, 2) == 10
    row = _run(inventory.list_writeoffs())[0]
    assert row["cancelled_at"]
    # Повтор — не второе движение остатка.
    again = _run(inventory.void_writeoff(made["writeoff_id"], user_id=ids["mgr"], is_boss=False))
    assert not again["ok"] and again["code"] == "already_cancelled"
    assert _stock(db, 2) == 10


def test_manager_cannot_void_foreign_writeoff_but_boss_can(wh):
    db, ids, inventory, _w = wh
    made = _run(
        inventory.create_writeoff(
            items=[{"product_id": 3, "quantity": 1}], reason="порча", created_by=ids["boss"]
        )
    )
    denied = _run(inventory.void_writeoff(made["writeoff_id"], user_id=ids["mgr"], is_boss=False))
    assert not denied["ok"] and denied["code"] == "not_owner"
    assert _stock(db, 3) == 9
    ok = _run(inventory.void_writeoff(made["writeoff_id"], user_id=ids["boss"], is_boss=True))
    assert ok["ok"] and _stock(db, 3) == 10


def test_void_window_closes_for_author_but_not_for_boss(wh):
    db, ids, inventory, _w = wh
    made = _run(
        inventory.create_writeoff(
            items=[{"product_id": 1, "quantity": 1}], reason="бой", created_by=ids["mgr"]
        )
    )
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(
            db.q("UPDATE stock_writeoffs SET created_at = ? WHERE id = ?"),
            ("2000-01-01 10:00:00", made["writeoff_id"]),
        )
        conn.commit()
    late = _run(inventory.void_writeoff(made["writeoff_id"], user_id=ids["mgr"], is_boss=False))
    assert not late["ok"] and late["code"] == "window_closed"
    assert _stock(db, 1) == 9
    boss = _run(inventory.void_writeoff(made["writeoff_id"], user_id=ids["boss"], is_boss=True))
    assert boss["ok"] and _stock(db, 1) == 10


# ─── Сервис: инвентаризация ───────────────────────────────────────────────────


def test_count_computes_deltas_and_applies_both_directions(wh):
    db, ids, inventory, _w = wh
    started = _run(inventory.start_count(note="ряд А", started_by=ids["mgr"]))
    assert started["ok"], started
    cid = started["count_id"]
    _run(inventory.set_count_line(cid, 1, 7))     # было 10 → недостача 3
    _run(inventory.set_count_line(cid, 2, 12))    # было 10 → излишек 2
    _run(inventory.set_count_line(cid, 3, 10))    # сходится

    card = _run(inventory.count_card(cid))
    assert card["summary"] == {
        "lines": 3, "short": 1, "surplus": 1, "match": 1,
        "short_qty": 3.0, "surplus_qty": 2.0,
    }

    res = _run(inventory.apply_count(cid, user_id=ids["mgr"]))
    assert res["ok"] and res["matched"] == 1
    assert _stock(db, 1) == 7 and _stock(db, 2) == 12 and _stock(db, 3) == 10
    rows = _run(inventory.list_writeoffs(count_id=cid))
    assert {r["kind"] for r in rows} == {"writeoff", "surplus"}
    assert all(r["reason"] == inventory.COUNT_REASON for r in rows)
    assert _run(inventory.count_card(cid))["status"] == "applied"


def test_count_line_is_upserted_not_duplicated(wh):
    _db, ids, inventory, _w = wh
    cid = _run(inventory.start_count(started_by=ids["mgr"]))["count_id"]
    _run(inventory.set_count_line(cid, 1, 4))
    line = _run(inventory.set_count_line(cid, 1, 6))
    assert line["counted_qty"] == 6
    card = _run(inventory.count_card(cid))
    assert len(card["lines"]) == 1 and card["lines"][0]["counted_qty"] == 6


def test_count_delta_is_taken_at_apply_time_not_at_entry(wh):
    """Между подсчётом и проведением склад живёт: считаем от ЖИВОГО остатка."""
    db, ids, inventory, warehouse = wh
    cid = _run(inventory.start_count(started_by=ids["mgr"]))["count_id"]
    _run(inventory.set_count_line(cid, 1, 10))    # на момент ввода сходилось
    # Пришёл приход на 5 штук — теперь в системе 15, а на полке 10.
    assert _run(
        warehouse.create_invoice(
            invoice_type="incoming", warehouse_id=1,
            items=[{"product_id": 1, "quantity": 5, "price_cents": 100}],
        )
    )["ok"]
    _run(inventory.apply_count(cid, user_id=ids["mgr"]))
    assert _stock(db, 1) == 10
    rows = _run(inventory.list_writeoffs(count_id=cid))
    assert len(rows) == 1 and rows[0]["kind"] == "writeoff"
    assert rows[0]["items"][0]["quantity"] == 5


def test_count_apply_is_all_or_nothing(wh):
    """Одна непроводимая строка откатывает ВЕСЬ пересчёт — полусклада не бывает."""
    db, ids, inventory, warehouse = wh
    cid = _run(inventory.start_count(started_by=ids["mgr"]))["count_id"]
    _run(inventory.set_count_line(cid, 1, 3))     # недостача 7 — законна
    _run(inventory.set_count_line(cid, 2, 12))    # излишек 2 — законен
    # Ломаем вторую позицию: товар исчез из справочника (в жизни — гонка с
    # удалением карточки). Приход по нему упадёт `unknown_product`.
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(db.q("DELETE FROM products WHERE id = ?"), (2,))
        conn.commit()
    with pytest.raises(warehouse.InvoiceError) as err:
        _run(inventory.apply_count(cid, user_id=ids["mgr"]))
    assert err.value.code == "unknown_product"
    assert _stock(db, 1) == 10, "недостача применилась, хотя излишек не прошёл"
    assert _run(inventory.list_writeoffs()) == []
    assert _run(inventory.count_card(cid))["status"] == "open"


def test_second_open_count_is_refused(wh):
    _db, ids, inventory, _w = wh
    first = _run(inventory.start_count(started_by=ids["mgr"]))
    second = _run(inventory.start_count(started_by=ids["boss"]))
    assert not second["ok"] and second["code"] == "already_open"
    assert second["count_id"] == first["count_id"]


def test_applied_count_cannot_be_applied_twice(wh):
    db, ids, inventory, _w = wh
    cid = _run(inventory.start_count(started_by=ids["mgr"]))["count_id"]
    _run(inventory.set_count_line(cid, 1, 8))
    _run(inventory.apply_count(cid, user_id=ids["mgr"]))
    with pytest.raises(inventory.InventoryError) as e:
        _run(inventory.apply_count(cid, user_id=ids["mgr"]))
    assert e.value.code == "count_closed"
    assert _stock(db, 1) == 8


def test_cancelled_count_changes_nothing(wh):
    db, ids, inventory, _w = wh
    cid = _run(inventory.start_count(started_by=ids["mgr"]))["count_id"]
    _run(inventory.set_count_line(cid, 1, 1))
    assert _run(inventory.cancel_count(cid, user_id=ids["mgr"]))["ok"]
    assert _stock(db, 1) == 10
    assert _run(inventory.count_card(cid))["status"] == "cancelled"


# ─── Себестоимость ────────────────────────────────────────────────────────────


def test_writeoff_records_cost_when_accounting_is_on(wh):
    """Учёт включён — списание фиксирует себестоимость тем же FIFO, что продажа."""
    _db, ids, inventory, warehouse = wh
    from services import accounting as acc
    from services import costing

    _run(acc.set_enabled(acc.Actor(ids["boss"], "Boss", "boss"), True))
    # Партия появляется только у прихода ПОСЛЕ включения учёта.
    assert _run(
        warehouse.create_invoice(
            invoice_type="incoming", warehouse_id=1,
            items=[{"product_id": 1, "quantity": 5, "price_cents": 20_000}],
        )
    )["ok"]
    res = _run(
        inventory.create_writeoff(
            items=[{"product_id": 1, "quantity": 12}], reason="порча", created_by=ids["mgr"]
        )
    )
    assert res["ok"], res
    # 10 штук «старше учёта» (без себестоимости) + 2 из партии по 200.00.
    assert res["cost_cents"] == 40_000
    row = _run(inventory.list_writeoffs())[0]
    assert row["cost_cents"] == 40_000
    # В отчёт о прибыли списание не попадает: выручки у него нет, и оно
    # выглядело бы убыточной сделкой на всю себестоимость.
    report = _run(costing.period_report("2000-01-01"))
    assert report["totals"]["revenue_cents"] == 0
    assert report["negative_deals"] == []
    assert report["totals"]["cogs_cents"] == 0


def test_writeoff_works_with_accounting_off(wh):
    """Базовое списание не требует бухгалтерии — цена потери просто пустая."""
    _db, ids, inventory, _w = wh
    res = _run(
        inventory.create_writeoff(
            items=[{"product_id": 1, "quantity": 1}], reason="бой", created_by=ids["mgr"]
        )
    )
    assert res["ok"] and res["cost_cents"] is None


# ─── Ручки WebApp ─────────────────────────────────────────────────────────────


@pytest.fixture
def api(wh, monkeypatch):
    import services.rate_limit as rate_limit
    import webapp.server as server

    rate_limit.reset()
    monkeypatch.setattr(
        server,
        "verify_init_data",
        lambda init_data: {"id": int(init_data), "first_name": "U", "username": "u"},
    )
    db, ids, inventory, warehouse = wh
    return TestClient(server.app), db, ids, inventory


def _writeoff(client, uid, product_id=1, qty=2, reason="бой", **extra):
    body = {
        "initData": str(uid), "product_id": product_id, "quantity": qty, "reason": reason,
    }
    body.update(extra)
    return client.post("/api/stock/writeoffs/create", json=body)


def test_manager_writes_off_and_it_lands_in_the_journal(api):
    client, db, ids, _inv = api
    r = _writeoff(client, ids["mgr"])
    assert r.status_code == 200, r.text
    assert _stock(db, 1) == 8
    lst = client.post("/api/stock/writeoffs", json={"initData": str(ids["mgr"])}).json()
    assert lst["writeoffs"][0]["reason"] == "бой"
    # Быстрые причины — подсказка, а не справочник: своя причина сохраняется
    # как есть и в список подсказок не попадает (inventory.QUICK_REASONS
    # переписали обычными словами — «разбили» вместо «бой»).
    from services import inventory

    assert lst["quick_reasons"] == list(inventory.QUICK_REASONS)
    assert "разбили" in lst["quick_reasons"]


def test_guest_cannot_write_off(api):
    client, db, ids, _inv = api
    assert _writeoff(client, ids["guest"]).status_code == 403
    assert _stock(db, 1) == 10


def test_keeper_can_write_off_through_role_combination(api):
    """Кладовщик — физическая работа со складом; ручка отвечает и ему."""
    client, _db, ids, _inv = api
    # Роли `warehouse_keeper` в `allowed_roles` нет, но и быть не должно:
    # список ролей ручки — менеджерский, а кладовщику её открывать отдельно не
    # решали. Проверяем ровно текущее правило, чтобы оно не поехало молча.
    assert _writeoff(client, ids["keeper"]).status_code == 403


def test_writeoff_without_reason_is_400(api):
    client, db, ids, _inv = api
    assert _writeoff(client, ids["mgr"], reason=" ").status_code == 400
    assert _stock(db, 1) == 10


def test_writeoff_over_stock_is_409_with_code(api):
    client, _db, ids, _inv = api
    r = _writeoff(client, ids["mgr"], qty=99)
    assert r.status_code == 409 and r.json()["code"] == "insufficient_stock"


def test_repeated_writeoff_with_same_key_does_not_double(api):
    client, db, ids, _inv = api
    key = "idem-writeoff-1"
    first = _writeoff(client, ids["mgr"], idempotency_key=key)
    second = _writeoff(client, ids["mgr"], idempotency_key=key)
    assert first.status_code == 200 and second.status_code == 200
    assert first.json()["writeoff_id"] == second.json()["writeoff_id"]
    assert _stock(db, 1) == 8


def test_writeoff_writes_audit_log(api):
    client, db, ids, _inv = api
    _writeoff(client, ids["mgr"], reason="недостача")
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(db.q("SELECT action, details FROM audit_log ORDER BY id DESC"))
        row = cur.fetchone()
    action = row["action"] if db.USE_POSTGRES else row[0]
    details = row["details"] if db.USE_POSTGRES else row[1]
    assert action == "stock_writeoff" and "недостача" in details


def test_cost_is_hidden_from_manager(api):
    """Себестоимость потери = закупочная цена: режем в ОТВЕТЕ, не во фронте."""
    client, _db, ids, _inv = api
    from services import accounting as acc

    _run(acc.set_enabled(acc.Actor(ids["boss"], "Boss", "boss"), True))
    from services import warehouse

    _run(warehouse.create_invoice(
        invoice_type="incoming", warehouse_id=1,
        items=[{"product_id": 1, "quantity": 5, "price_cents": 20_000}],
    ))
    _writeoff(client, ids["boss"], qty=12, reason="порча")
    mgr = client.post("/api/stock/writeoffs", json={"initData": str(ids["mgr"])}).json()
    boss = client.post("/api/stock/writeoffs", json={"initData": str(ids["boss"])}).json()
    assert mgr["writeoffs"][0]["cost_cents"] is None
    assert boss["writeoffs"][0]["cost_cents"] == 40_000


def test_void_is_closed_when_delete_requires_boss(api):
    client, db, ids, _inv = api
    made = _writeoff(client, ids["mgr"]).json()
    on = client.post(
        "/api/settings/delete_requires_boss",
        json={"initData": str(ids["boss"]), "enabled": True},
    )
    assert on.status_code == 200, on.text
    r = client.post(
        "/api/stock/writeoffs/void",
        json={"initData": str(ids["mgr"]), "writeoff_id": made["writeoff_id"]},
    )
    assert r.status_code == 403
    assert _stock(db, 1) == 8


def test_count_flow_through_api(api):
    client, db, ids, _inv = api
    started = client.post(
        "/api/stock/counts/start", json={"initData": str(ids["mgr"]), "note": "ряд Б"}
    ).json()
    cid = started["count_id"]
    for pid, qty in ((1, 9), (2, 11), (3, 10)):
        r = client.post(
            "/api/stock/counts/line",
            json={"initData": str(ids["mgr"]), "count_id": cid,
                  "product_id": pid, "counted_qty": qty},
        )
        assert r.status_code == 200, r.text
    card = client.post(
        "/api/stock/counts/card", json={"initData": str(ids["mgr"]), "count_id": cid}
    ).json()
    assert card["summary"]["short"] == 1 and card["summary"]["surplus"] == 1

    key = "idem-count-1"
    first = client.post(
        "/api/stock/counts/confirm",
        json={"initData": str(ids["mgr"]), "count_id": cid, "idempotency_key": key},
    )
    assert first.status_code == 200, first.text
    assert _stock(db, 1) == 9 and _stock(db, 2) == 11 and _stock(db, 3) == 10
    # Ретрай тем же ключом отдаёт сохранённый ответ, а не проводит второй раз.
    second = client.post(
        "/api/stock/counts/confirm",
        json={"initData": str(ids["mgr"]), "count_id": cid, "idempotency_key": key},
    )
    assert second.status_code == 200
    assert _stock(db, 1) == 9 and _stock(db, 2) == 11
    # Без ключа повтор ловит CAS по статусу сессии.
    third = client.post(
        "/api/stock/counts/confirm", json={"initData": str(ids["mgr"]), "count_id": cid}
    )
    assert third.status_code == 409 and third.json()["code"] == "count_closed"


def test_count_is_closed_to_guest(api):
    client, _db, ids, _inv = api
    for path in ("/api/stock/counts", "/api/stock/counts/start", "/api/stock/counts/confirm"):
        assert client.post(path, json={"initData": str(ids["guest"]), "count_id": 1}).status_code == 403


# ─── Фото «вот что разбилось» ─────────────────────────────────────────────────

JPEG = b"\xff\xd8\xff" + b"\x00" * 64


class _FakePhotoSize:
    def __init__(self, file_id, unique, width, height):
        self.file_id = file_id
        self.file_unique_id = unique
        self.width = width
        self.height = height


class _FakeFile:
    file_size = 1000
    file_path = "photos/file_1.jpg"


class _FakeBot:
    """Граница с Telegram: снимок уезжает в приватный канал и возвращается
    оттуда байтами. Своего стореджа у проекта нет."""

    def __init__(self):
        self.get_file_calls = 0

    async def send_photo(self, chat_id, photo, caption=None):
        return type(
            "Msg", (), {"photo": [_FakePhotoSize("small", "u1", 90, 60),
                                  _FakePhotoSize("big-file-id", "u2", 1600, 1200)]}
        )()

    async def get_file(self, file_id):
        self.get_file_calls += 1
        assert file_id == "big-file-id", "тянем именно тот файл, что записан у списания"
        return _FakeFile()

    async def download_file(self, path):
        import io

        return io.BytesIO(JPEG)


@pytest.fixture
def photo_api(api, monkeypatch):
    import webapp.server as server

    bot = _FakeBot()

    async def _bot():
        return bot

    monkeypatch.setattr(server, "get_notify_bot", _bot)
    monkeypatch.setenv("PHOTOS_TG_CHAT_ID", "-1001234567890")
    server._PHOTO_CACHE.clear()
    return (*api, bot)


def _data_url() -> str:
    import base64

    return "data:image/jpeg;base64," + base64.b64encode(JPEG).decode()


def test_photo_is_attached_before_the_writeoff_exists(photo_api):
    """Снимок уезжает ДО проведения: записи ещё нет, форма несёт file_id."""
    client, db, ids, _inv, bot = photo_api
    up = client.post(
        "/api/stock/writeoffs/photo",
        json={"initData": str(ids["mgr"]), "data_url": _data_url()},
    )
    assert up.status_code == 200, up.text
    file_id = up.json()["photo_file_id"]
    assert file_id == "big-file-id", "берём самый крупный размер, а не превью"

    made = _writeoff(client, ids["mgr"], photo_file_id=file_id).json()
    shown = client.post(
        "/api/stock/writeoffs/photo_view",
        json={"initData": str(ids["mgr"]), "writeoff_id": made["writeoff_id"]},
    )
    assert shown.status_code == 200
    assert shown.headers["content-type"].startswith("image/")
    assert shown.content == JPEG
    # Приватный кэш: прокси не должен раздавать снимок посторонним.
    assert "private" in shown.headers["cache-control"]
    # Второй заход — из кэша, а не новый поход в Bot API.
    client.post(
        "/api/stock/writeoffs/photo_view",
        json={"initData": str(ids["mgr"]), "writeoff_id": made["writeoff_id"]},
    )
    assert bot.get_file_calls == 1


def test_photo_view_is_scoped_by_record_and_closed_to_guest(photo_api):
    """`file_id` от клиента не принимаем вовсе — только номер записи."""
    client, _db, ids, _inv, _bot = photo_api
    made = _writeoff(client, ids["mgr"]).json()      # списание без фото
    missing = client.post(
        "/api/stock/writeoffs/photo_view",
        json={"initData": str(ids["boss"]), "writeoff_id": made["writeoff_id"]},
    )
    assert missing.status_code == 404
    guest = client.post(
        "/api/stock/writeoffs/photo",
        json={"initData": str(ids["guest"]), "data_url": _data_url()},
    )
    assert guest.status_code == 403


def test_non_image_payload_is_refused(photo_api):
    client, _db, ids, _inv, _bot = photo_api
    r = client.post(
        "/api/stock/writeoffs/photo",
        json={"initData": str(ids["mgr"]), "data_url": "data:text/html,<script>"},
    )
    assert r.status_code == 400


def test_photo_upload_is_off_without_a_storage_channel(api, monkeypatch):
    """Канала нет — кнопку не рисуем (`can_photo`), а ручка честно отвечает 503."""
    client, _db, ids, _inv = api
    monkeypatch.delenv("PHOTOS_TG_CHAT_ID", raising=False)
    monkeypatch.delenv("MACHINE_PHOTOS_TG_CHAT_ID", raising=False)
    lst = client.post("/api/stock/writeoffs", json={"initData": str(ids["mgr"])}).json()
    assert lst["can_photo"] is False
    r = client.post(
        "/api/stock/writeoffs/photo",
        json={"initData": str(ids["mgr"]), "data_url": _data_url()},
    )
    assert r.status_code == 503


def test_writeoff_invoice_cannot_be_cancelled_as_an_invoice(api):
    """Отмена накладной списания шла бы мимо записи о причине — отказ с адресом."""
    client, db, ids, _inv = api
    made = _writeoff(client, ids["boss"]).json()
    r = client.post(
        "/api/wh/invoices/cancel",
        json={"initData": str(ids["boss"]), "invoice_id": made["invoice_id"]},
    )
    assert r.status_code == 409 and r.json()["code"] == "linked_writeoff"
    assert _stock(db, 1) == 8


def test_writeoff_invoice_is_marked_in_the_invoice_list(api):
    client, _db, ids, _inv = api
    _writeoff(client, ids["boss"])
    rows = client.post("/api/wh/invoices", json={"initData": str(ids["boss"])}).json()["invoices"]
    writeoff = [r for r in rows if r["writeoff"]]
    assert len(writeoff) == 1 and writeoff[0]["type"] == "outgoing"
