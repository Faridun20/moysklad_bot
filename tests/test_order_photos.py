"""
Фото к заказу (B9): подписанная расписка, накладная, акт передачи.

Сервисный слой (`services/order_photos.py`) — тот же приём, что у фото
техники/товаров, плюс своё окно удаления (автор — 24 ч, руководство —
всегда). Ручки WebApp — тот же риск-профиль, что у `/api/machines/photo*`
(tests/test_machines_photo_api.py): прямую ссылку Telegram не отдаём,
`photo_id`/`order_id` от клиента обязаны совпасть с видимым заказом.

БД настоящая (isolated_db), корутины через asyncio.run — pytest-asyncio в
проекте нет. Telegram мокаем на границе (aiogram.Bot).
"""

from __future__ import annotations

import asyncio
import base64
import importlib
from datetime import timedelta

from fastapi.testclient import TestClient

import services.roles as roles
from utils.helpers import local_now

JPEG = b"\xff\xd8\xff" + b"\x00" * 64
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def _run(coro):
    return asyncio.run(coro)


def _setup(db):
    roles.invalidate_all_roles()
    db.set_role(1, "mgr", "Manager", "manager")
    db.set_role(2, "boss", "Boss", "boss")
    db.set_role(3, "other_mgr", "Other Manager", "manager")


def _order(db, owner=1):
    return db.create_order(owner, "Manager", "")


def _push_uploaded_at_into_past(db, photo_id, hours):
    past = (local_now() - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(db.q("UPDATE order_photos SET uploaded_at = ? WHERE id = ?"), (past, photo_id))
        conn.commit()


# ─── Сервис: add_photo / list_photos / photos_by_orders ────────────────────


def test_add_photo_requires_ids(isolated_db):
    from services import order_photos

    db = isolated_db
    _setup(db)
    oid = _order(db)
    res = _run(order_photos.add_photo(oid, tg_file_id="", file_unique_id="", uploaded_by=1))
    assert not res["ok"]


def test_add_photo_rejects_missing_order(isolated_db):
    from services import order_photos

    res = _run(order_photos.add_photo(
        999999, tg_file_id="tg-1", file_unique_id="uniq-1", uploaded_by=1
    ))
    assert not res["ok"]
    assert "не найден" in res["error"]


def test_add_photo_is_idempotent_for_same_file(isolated_db):
    from services import order_photos

    db = isolated_db
    _setup(db)
    oid = _order(db)
    first = _run(order_photos.add_photo(
        oid, tg_file_id="tg-1", file_unique_id="uniq-1", uploaded_by=1
    ))
    second = _run(order_photos.add_photo(
        oid, tg_file_id="tg-2", file_unique_id="uniq-1", uploaded_by=1
    ))
    assert first["ok"] and not first["duplicate"]
    assert second["ok"] and second["duplicate"]
    assert len(_run(order_photos.list_photos(oid))) == 1


def test_photos_by_orders_batches_without_n_plus_one(isolated_db):
    from services import order_photos

    db = isolated_db
    _setup(db)
    o1, o2, o3 = _order(db), _order(db), _order(db)
    _run(order_photos.add_photo(o1, tg_file_id="a", file_unique_id="a", uploaded_by=1))
    _run(order_photos.add_photo(o1, tg_file_id="b", file_unique_id="b", uploaded_by=1))
    _run(order_photos.add_photo(o2, tg_file_id="c", file_unique_id="c", uploaded_by=1))

    by_order = _run(order_photos.photos_by_orders([o1, o2, o3]))
    assert len(by_order.get(o1, [])) == 2
    assert len(by_order.get(o2, [])) == 1
    assert o3 not in by_order
    assert _run(order_photos.photos_by_orders([])) == {}


# ─── Сервис: can_delete / окно удаления ─────────────────────────────────────


def test_can_delete_boss_always(isolated_db):
    from services import order_photos

    db = isolated_db
    _setup(db)
    oid = _order(db)
    _run(order_photos.add_photo(oid, tg_file_id="a", file_unique_id="a", uploaded_by=1))
    photo = _run(order_photos.list_photos(oid))[0]
    _push_uploaded_at_into_past(db, photo["id"], hours=48)
    photo = _run(order_photos.list_photos(oid))[0]
    assert order_photos.can_delete(photo, user_id=2, role="boss")
    assert order_photos.can_delete(photo, user_id=999, role="admin")


def test_can_delete_uploader_within_window(isolated_db):
    from services import order_photos

    db = isolated_db
    _setup(db)
    oid = _order(db)
    _run(order_photos.add_photo(oid, tg_file_id="a", file_unique_id="a", uploaded_by=1))
    photo = _run(order_photos.list_photos(oid))[0]
    assert order_photos.can_delete(photo, user_id=1, role="manager")


def test_can_delete_uploader_after_window_denied(isolated_db):
    from services import order_photos

    db = isolated_db
    _setup(db)
    oid = _order(db)
    _run(order_photos.add_photo(oid, tg_file_id="a", file_unique_id="a", uploaded_by=1))
    photo_id = _run(order_photos.list_photos(oid))[0]["id"]
    _push_uploaded_at_into_past(db, photo_id, hours=order_photos.DELETE_WINDOW_HOURS + 1)
    photo = _run(order_photos.list_photos(oid))[0]
    assert not order_photos.can_delete(photo, user_id=1, role="manager")


def test_can_delete_other_manager_denied(isolated_db):
    from services import order_photos

    db = isolated_db
    _setup(db)
    oid = _order(db)
    _run(order_photos.add_photo(oid, tg_file_id="a", file_unique_id="a", uploaded_by=1))
    photo = _run(order_photos.list_photos(oid))[0]
    assert not order_photos.can_delete(photo, user_id=3, role="manager")


def test_delete_photo_scoped_to_order_and_permission(isolated_db):
    from services import order_photos

    db = isolated_db
    _setup(db)
    oid = _order(db)
    other = _order(db)
    _run(order_photos.add_photo(oid, tg_file_id="a", file_unique_id="a", uploaded_by=1))
    photo_id = _run(order_photos.list_photos(oid))[0]["id"]

    # Чужой заказ — не находит фото вовсе.
    wrong_scope = _run(order_photos.delete_photo(other, photo_id, user_id=1, role="manager"))
    assert not wrong_scope["ok"]

    # Другой менеджер — отказ по праву.
    denied = _run(order_photos.delete_photo(oid, photo_id, user_id=3, role="manager"))
    assert not denied["ok"]
    assert _run(order_photos.list_photos(oid))  # осталось на месте

    ok = _run(order_photos.delete_photo(oid, photo_id, user_id=1, role="manager"))
    assert ok["ok"]
    assert _run(order_photos.list_photos(oid)) == []


# ─── API: прокси, загрузка, удаление ────────────────────────────────────────


class _FakePhotoSize:
    def __init__(self, file_id, unique, width, height):
        self.file_id = file_id
        self.file_unique_id = unique
        self.width = width
        self.height = height


class _FakeMessage:
    def __init__(self, sizes):
        self.photo = sizes


class _FakeBot:
    def __init__(self, *, blob=JPEG, sizes=None):
        self.blob = blob
        self.sizes = sizes
        self.get_file_calls = 0
        self.sent = []

    async def get_file(self, file_id):
        self.get_file_calls += 1

        class _F:
            file_size = 1000
            file_path = "photos/x.jpg"
        return _F()

    async def download_file(self, path):
        import io
        return io.BytesIO(self.blob)

    async def send_photo(self, chat_id, photo, caption=None):
        self.sent.append((chat_id, photo, caption))
        sizes = self.sizes or [
            _FakePhotoSize("small-id", "small-uniq", 90, 60),
            _FakePhotoSize("big-id", "big-uniq", 1600, 1200),
        ]
        return _FakeMessage(sizes)


def _client(monkeypatch, bot=None, chat_id="-1009876543210"):
    import webapp.server as server

    importlib.reload(roles)
    monkeypatch.setattr(server, "verify_init_data", lambda s: {"id": int(s), "first_name": "U"})
    server._PHOTO_CACHE.clear()
    fake = bot or _FakeBot()

    async def _bot():
        return fake

    monkeypatch.setattr(server, "get_notify_bot", _bot)
    if chat_id is None:
        monkeypatch.delenv("PHOTOS_TG_CHAT_ID", raising=False)
        monkeypatch.delenv("MACHINE_PHOTOS_TG_CHAT_ID", raising=False)
    else:
        monkeypatch.setenv("PHOTOS_TG_CHAT_ID", chat_id)
    return TestClient(server.app), fake


def _post(client, path, uid, **body):
    return client.post(path, json={"initData": str(uid), **body})


def _data_url(blob, mime="image/jpeg"):
    return f"data:{mime};base64," + base64.b64encode(blob).decode()


def test_manager_can_upload_and_view_own_order_photo(isolated_db, monkeypatch):
    db = isolated_db
    _setup(db)
    oid = _order(db, owner=1)
    client, bot = _client(monkeypatch)

    up = _post(client, "/api/orders/photo_upload", 1, order_id=oid, data_url=_data_url(JPEG))
    assert up.status_code == 200, up.text
    assert bot.sent and bot.sent[0][0] == -1009876543210

    photos = _post(client, "/api/orders/photos", 1, order_id=oid).json()["photos"]
    assert len(photos) == 1
    pid = photos[0]["id"]

    r = _post(client, "/api/orders/photo", 1, order_id=oid, photo_id=pid)
    assert r.status_code == 200
    assert r.content == JPEG


def test_manager_cannot_upload_to_someone_elses_order(isolated_db, monkeypatch):
    db = isolated_db
    _setup(db)
    oid = _order(db, owner=1)
    client, bot = _client(monkeypatch)

    r = _post(client, "/api/orders/photo_upload", 3, order_id=oid, data_url=_data_url(JPEG))
    assert r.status_code == 404
    assert bot.sent == []


def test_boss_sees_and_uploads_to_any_order(isolated_db, monkeypatch):
    db = isolated_db
    _setup(db)
    oid = _order(db, owner=1)
    client, _ = _client(monkeypatch)

    up = _post(client, "/api/orders/photo_upload", 2, order_id=oid, data_url=_data_url(JPEG))
    assert up.status_code == 200, up.text
    listed = _post(client, "/api/orders/photos", 2, order_id=oid).json()
    assert listed["photos"][0]["can_delete"] is True  # руководство — всегда


def test_photo_upload_without_storage_channel_hints_the_bot(isolated_db, monkeypatch):
    db = isolated_db
    _setup(db)
    oid = _order(db, owner=1)
    client, _ = _client(monkeypatch, chat_id=None)

    r = _post(client, "/api/orders/photo_upload", 1, order_id=oid, data_url=_data_url(JPEG))
    assert r.status_code == 503
    assert "боту" in r.json()["detail"]


def test_photo_upload_rejects_non_image(isolated_db, monkeypatch):
    db = isolated_db
    _setup(db)
    oid = _order(db, owner=1)
    client, bot = _client(monkeypatch)

    r = _post(client, "/api/orders/photo_upload", 1, order_id=oid,
              data_url=_data_url(b"not-an-image", "image/jpeg"))
    assert r.status_code == 400
    assert bot.sent == []


def test_delete_within_window_by_uploader_then_denied_after(isolated_db, monkeypatch):
    from services import order_photos

    db = isolated_db
    _setup(db)
    oid = _order(db, owner=1)
    client, _ = _client(monkeypatch)
    _post(client, "/api/orders/photo_upload", 1, order_id=oid, data_url=_data_url(JPEG))
    photo_id = _run(order_photos.list_photos(oid))[0]["id"]

    # Окно ещё открыто — автор удаляет сам.
    del_ok = _post(client, "/api/orders/photo_delete", 1, order_id=oid, photo_id=photo_id)
    assert del_ok.status_code == 200, del_ok.text

    # Второй снимок — состарим и проверим отказ автору, но не боссу.
    _post(client, "/api/orders/photo_upload", 1, order_id=oid, data_url=_data_url(PNG, "image/png"))
    photo_id2 = _run(order_photos.list_photos(oid))[0]["id"]
    _push_uploaded_at_into_past(db, photo_id2, hours=order_photos.DELETE_WINDOW_HOURS + 1)

    denied = _post(client, "/api/orders/photo_delete", 1, order_id=oid, photo_id=photo_id2)
    assert denied.status_code == 400
    boss_ok = _post(client, "/api/orders/photo_delete", 2, order_id=oid, photo_id=photo_id2)
    assert boss_ok.status_code == 200, boss_ok.text


def test_orders_list_embeds_photos_and_photos_enabled_flag(isolated_db, monkeypatch):
    db = isolated_db
    _setup(db)
    oid = _order(db, owner=1)
    client, _ = _client(monkeypatch)
    _post(client, "/api/orders/photo_upload", 1, order_id=oid, data_url=_data_url(JPEG))

    listing = _post(client, "/api/orders", 1).json()
    assert listing["photos_enabled"] is True
    entry = next(o for o in listing["orders"] if o["id"] == oid)
    assert len(entry["photos"]) == 1
    assert entry["photos"][0]["can_delete"] is True
