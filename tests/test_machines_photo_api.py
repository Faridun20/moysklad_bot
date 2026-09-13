"""
Фотографии техники: прокси отдачи и загрузка из WebApp.

Риск-профиль у этих двух ручек свой, поэтому и тесты отдельные:

* прямая ссылка Telegram содержит токен бота — наружу не должен уйти ни он сам,
  ни `tg_file_id`, ни текст исключения aiogram (токен входит в URL файлового
  API, поэтому попадает в repr ошибки);
* `photo_id` приходит от клиента — снимок обязан принадлежать заявленной машине;
* через поле «фотография» без проверки байтов пройдёт что угодно.

Telegram мокаем на границе (aiogram.Bot), БД настоящая.
"""

import asyncio
import base64
import importlib
import logging

from fastapi.testclient import TestClient

import services.roles as roles

JPEG = b"\xff\xd8\xff" + b"\x00" * 64
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
TOKEN = "123456:AAH-secret-bot-token"


def _run(coro):
    return asyncio.run(coro)


def _setup(db):
    roles.invalidate_all_roles()
    db.set_role(1, "mgr", "Manager", "manager")
    db.set_role(2, "boss", "Boss", "boss")


def _machine(vin="A-1"):
    from services import machines

    res = _run(machines.create_machine(vin=vin, name="JCB", created_by=2))
    assert res["ok"], res
    return res["machine_id"]


def _photo(machine_id, unique="uniq-1", file_id="AgAC-file-id"):
    from services import machines

    res = _run(
        machines.add_photo(
            machine_id, tg_file_id=file_id, file_unique_id=unique, uploaded_by=2
        )
    )
    assert res["ok"], res
    rows = _run(machines.list_photos(machine_id))
    return int(rows[-1]["id"])


class _FakeFile:
    def __init__(self, size=1000, path="photos/file_1.jpg"):
        self.file_size = size
        self.file_path = path


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
    """Границa с Telegram. Считает вызовы — по ним видно, работает ли кэш."""

    def __init__(self, *, blob=JPEG, get_file_error=None, send_error=None, sizes=None):
        self.blob = blob
        self.get_file_error = get_file_error
        self.send_error = send_error
        self.sizes = sizes
        self.get_file_calls = 0
        self.sent = []

    async def get_file(self, file_id):
        self.get_file_calls += 1
        if self.get_file_error:
            raise self.get_file_error
        return _FakeFile()

    async def download_file(self, path):
        import io

        return io.BytesIO(self.blob)

    async def send_photo(self, chat_id, photo, caption=None):
        if self.send_error:
            raise self.send_error
        self.sent.append((chat_id, photo, caption))
        sizes = self.sizes or [
            _FakePhotoSize("small-id", "small-uniq", 90, 60),
            _FakePhotoSize("big-id", "big-uniq", 1600, 1200),
        ]
        return _FakeMessage(sizes)


def _client(monkeypatch, bot=None, chat_id="-1001234567890"):
    import webapp.server as server

    importlib.reload(roles)
    monkeypatch.setattr(server, "verify_init_data", lambda s: {"id": int(s), "first_name": "U"})
    server._PHOTO_CACHE.clear()
    fake = bot or _FakeBot()

    async def _bot():
        return fake

    monkeypatch.setattr(server, "get_notify_bot", _bot)
    if chat_id is None:
        monkeypatch.delenv("MACHINE_PHOTOS_TG_CHAT_ID", raising=False)
    else:
        monkeypatch.setenv("MACHINE_PHOTOS_TG_CHAT_ID", chat_id)
    return TestClient(server.app), fake


def _post(client, path, uid, **body):
    return client.post(path, json={"initData": str(uid), **body})


# ─── Отдача ───────────────────────────────────────────────────────────────────


def test_photo_is_served_as_image(isolated_db, monkeypatch):
    db = isolated_db
    _setup(db)
    mid = _machine()
    pid = _photo(mid)
    client, _ = _client(monkeypatch)

    r = _post(client, "/api/machines/photo", 1, machine_id=mid, photo_id=pid)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("image/")
    assert r.content == JPEG
    # Приватный кэш: чужой прокси не должен раздавать снимок другим людям.
    assert "private" in r.headers["cache-control"]
    assert r.headers["x-content-type-options"] == "nosniff"


def test_second_request_is_served_from_cache(isolated_db, monkeypatch):
    """Лента карточки — десяток запросов подряд; без кэша каждый открытый
    экран стоил бы столько же обращений к Bot API."""
    db = isolated_db
    _setup(db)
    mid = _machine()
    pid = _photo(mid)
    client, bot = _client(monkeypatch)

    assert _post(client, "/api/machines/photo", 2, machine_id=mid, photo_id=pid).status_code == 200
    assert _post(client, "/api/machines/photo", 2, machine_id=mid, photo_id=pid).status_code == 200
    assert bot.get_file_calls == 1


def test_photo_of_another_machine_is_not_served(isolated_db, monkeypatch):
    """`photo_id` приходит от клиента: снимок обязан принадлежать той машине,
    доступ к которой заявлен."""
    db = isolated_db
    _setup(db)
    mine = _machine("A-1")
    theirs = _machine("B-2")
    alien = _photo(theirs, unique="uniq-2")
    client, bot = _client(monkeypatch)

    r = _post(client, "/api/machines/photo", 1, machine_id=mine, photo_id=alien)
    assert r.status_code == 404
    assert bot.get_file_calls == 0  # до Telegram даже не дошли


def test_stale_file_id_is_404_not_500(isolated_db, monkeypatch, caplog):
    """Протухший file_id — «фото недоступно», а не поломка сервера: 500 поднял
    бы тревогу на ровном месте."""
    db = isolated_db
    _setup(db)
    mid = _machine()
    pid = _photo(mid)
    client, _ = _client(
        monkeypatch, bot=_FakeBot(get_file_error=RuntimeError("file not found"))
    )

    with caplog.at_level(logging.WARNING):
        r = _post(client, "/api/machines/photo", 2, machine_id=mid, photo_id=pid)
    assert r.status_code == 404
    assert "недоступно" in r.json()["detail"]


def test_token_never_reaches_logs(isolated_db, monkeypatch, caplog):
    """Токен входит в URL файлового API, поэтому попадает в текст исключения
    aiogram — в лог он обязан идти только через redact_token."""
    import config

    db = isolated_db
    _setup(db)
    mid = _machine()
    pid = _photo(mid)
    monkeypatch.setattr(config, "TELEGRAM_TOKEN", TOKEN)
    err = RuntimeError(f"GET https://api.telegram.org/file/bot{TOKEN}/photos/x.jpg failed")
    client, _ = _client(monkeypatch, bot=_FakeBot(get_file_error=err))

    with caplog.at_level(logging.WARNING):
        assert _post(client, "/api/machines/photo", 2, machine_id=mid, photo_id=pid).status_code == 404
    assert TOKEN not in caplog.text
    assert "***" in caplog.text


def test_response_never_carries_file_ids(isolated_db, monkeypatch):
    db = isolated_db
    _setup(db)
    mid = _machine()
    pid = _photo(mid, file_id="AgAC-secret-file-id")
    client, _ = _client(monkeypatch)

    r = _post(client, "/api/machines/photo", 2, machine_id=mid, photo_id=pid)
    assert b"AgAC-secret-file-id" not in r.content


# ─── Загрузка ─────────────────────────────────────────────────────────────────


def _data_url(blob, mime="image/jpeg"):
    return f"data:{mime};base64," + base64.b64encode(blob).decode()


def test_upload_stores_the_largest_size(isolated_db, monkeypatch):
    """Telegram отдаёт лесенку превью; первый элемент — миниатюра ~90px, из
    которой карточку не рассмотреть."""
    from services import machines

    db = isolated_db
    _setup(db)
    mid = _machine()
    client, bot = _client(monkeypatch)

    r = _post(client, "/api/machines/photo_upload", 1, machine_id=mid,
              data_url=_data_url(JPEG), caption="перед")
    assert r.status_code == 200, r.text
    photos = _run(machines.list_photos(mid))
    assert len(photos) == 1
    assert photos[0]["tg_file_id"] == "big-id"
    assert photos[0]["file_unique_id"] == "big-uniq"
    assert bot.sent and bot.sent[0][0] == -1001234567890


def test_upload_accepts_png(isolated_db, monkeypatch):
    db = isolated_db
    _setup(db)
    mid = _machine()
    client, _ = _client(monkeypatch)

    r = _post(client, "/api/machines/photo_upload", 2, machine_id=mid,
              data_url=_data_url(PNG, "image/png"))
    assert r.status_code == 200, r.text


def test_upload_rejects_non_image_by_magic_bytes(isolated_db, monkeypatch):
    """Заявленный тип пишет клиент — верить можно только байтам."""
    db = isolated_db
    _setup(db)
    mid = _machine()
    client, bot = _client(monkeypatch)

    r = _post(client, "/api/machines/photo_upload", 2, machine_id=mid,
              data_url=_data_url(b"#!/bin/sh\nrm -rf /", "image/jpeg"))
    assert r.status_code == 400
    assert "JPEG" in r.json()["detail"]
    assert bot.sent == []


def test_upload_rejects_oversize(isolated_db, monkeypatch):
    db = isolated_db
    _setup(db)
    mid = _machine()
    client, _ = _client(monkeypatch)

    huge = _data_url(b"\xff\xd8\xff" + b"\x00" * (6 * 1024 * 1024))
    r = _post(client, "/api/machines/photo_upload", 2, machine_id=mid, data_url=huge)
    assert r.status_code == 413


def test_upload_rejects_garbage_payload(isolated_db, monkeypatch):
    db = isolated_db
    _setup(db)
    mid = _machine()
    client, _ = _client(monkeypatch)

    for bad in ("", "not-a-data-url", "data:image/jpeg;base64", "data:image/jpeg;base64,%%%%"):
        r = _post(client, "/api/machines/photo_upload", 2, machine_id=mid, data_url=bad)
        assert r.status_code == 400, bad


def test_upload_without_storage_channel_explains_itself(isolated_db, monkeypatch):
    """Без канала-хранилища загрузка выключена, но фото по-прежнему можно
    прислать боту — это деградация функции, а не поломка раздела."""
    db = isolated_db
    _setup(db)
    mid = _machine()
    client, _ = _client(monkeypatch, chat_id=None)

    r = _post(client, "/api/machines/photo_upload", 2, machine_id=mid, data_url=_data_url(JPEG))
    assert r.status_code == 503
    assert "боту" in r.json()["detail"]


def test_card_tells_the_front_whether_upload_is_possible(isolated_db, monkeypatch):
    db = isolated_db
    _setup(db)
    mid = _machine()

    client, _ = _client(monkeypatch)
    assert _post(client, "/api/machines/card", 2, machine_id=mid).json()["can_upload_photo"] is True

    client, _ = _client(monkeypatch, chat_id=None)
    assert _post(client, "/api/machines/card", 2, machine_id=mid).json()["can_upload_photo"] is False


def test_upload_failure_is_502_without_token(isolated_db, monkeypatch, caplog):
    import config

    db = isolated_db
    _setup(db)
    mid = _machine()
    monkeypatch.setattr(config, "TELEGRAM_TOKEN", TOKEN)
    client, _ = _client(monkeypatch, bot=_FakeBot(send_error=RuntimeError(f"bot{TOKEN} rejected")))

    with caplog.at_level(logging.WARNING):
        r = _post(client, "/api/machines/photo_upload", 2, machine_id=mid, data_url=_data_url(JPEG))
    assert r.status_code == 502
    assert TOKEN not in caplog.text


def test_upload_is_idempotent_for_the_same_file(isolated_db, monkeypatch):
    """Тот же снимок второй раз — не ошибка и не дубль в карточке."""
    from services import machines

    db = isolated_db
    _setup(db)
    mid = _machine()
    client, _ = _client(monkeypatch)

    first = _post(client, "/api/machines/photo_upload", 2, machine_id=mid, data_url=_data_url(JPEG))
    second = _post(client, "/api/machines/photo_upload", 2, machine_id=mid, data_url=_data_url(JPEG))
    assert first.status_code == 200 and second.status_code == 200
    assert second.json()["duplicate"] is True
    assert len(_run(machines.list_photos(mid))) == 1


# ─── Открепление ──────────────────────────────────────────────────────────────


def test_photo_delete_is_boss_only_and_scoped(isolated_db, monkeypatch):
    from services import machines

    db = isolated_db
    _setup(db)
    mine = _machine("A-1")
    theirs = _machine("B-2")
    pid = _photo(mine)
    alien = _photo(theirs, unique="uniq-2")
    client, _ = _client(monkeypatch)

    assert _post(client, "/api/machines/photo_delete", 1,
                 machine_id=mine, photo_id=pid).status_code == 403
    assert _post(client, "/api/machines/photo_delete", 2,
                 machine_id=mine, photo_id=alien).status_code == 404
    assert _post(client, "/api/machines/photo_delete", 2,
                 machine_id=mine, photo_id=pid).status_code == 200
    assert _run(machines.list_photos(mine)) == []
