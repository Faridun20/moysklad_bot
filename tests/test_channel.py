"""
Посты в канал.

Главный инвариант: наружу не уходит ни одна цифра количества. Остатки и объёмы
поставок — внутренняя информация; клиенту она ничего не даёт, а конкуренту
рассказывает, сколько вы способны отгрузить. Всё остальное здесь — про то,
чтобы пост не соврал: не обещать недоехавшее и не публиковать одно дважды.
"""

import asyncio

import services.roles as roles


def _run(coro):
    return asyncio.run(coro)


def _setup(db):
    roles.invalidate_all_roles()
    db.set_role(2, "boss", "Boss", "boss")


def _container(db, number="MSKU-1"):
    from services import containers

    res = _run(containers.create_container(number=number, created_by=2, creator_name="Boss"))
    assert res["ok"], res
    return res["container_id"]


# ─── Сторож на количества ─────────────────────────────────────────────────────


def test_quantity_detector_finds_amounts():
    from services import channel

    for text in ("осталось 12 шт", "500 м кабеля", "по 3 штуки", "2 компл.",
                 "1 000 шт", "20 кг", "5 пар"):
        assert channel.contains_quantities(text) is True, text


def test_quantity_detector_ignores_names_and_prices():
    """«PV 0.6» и «2019» — не количества, иначе сторож заблокирует нормальный
    пост."""
    from services import channel

    for text in ("Кабель PV 0.6", "JCB 3CX 2019", "Цена: 1 200 USD",
                 "ThinkPower 6kw", "Есть в наличии"):
        assert channel.contains_quantities(text) is False, text


# ─── Витрина ──────────────────────────────────────────────────────────────────


def test_showcase_shows_availability_without_numbers(isolated_db):
    """«В наличии» — факт; «осталось 12» — сведения о ваших объёмах."""
    from services import channel

    post = channel.build_showcase(
        {"name": "Кабель PV 0.6"}, price="80 USD", manager_username="manager"
    )
    assert "Кабель PV 0.6" in post
    assert "Есть в наличии" in post
    assert channel.contains_quantities(post) is False
    assert "t.me/manager" in post


def test_showcase_escapes_the_name(isolated_db):
    """Название приходит из МойСклад и идёт в сообщение с parse_mode=HTML."""
    from services import channel

    post = channel.build_showcase({"name": 'Кабель <"Строй">'})
    assert "&lt;" in post and "<\"Строй\">" not in post


def test_contact_line_without_username(isolated_db):
    from services import channel

    post = channel.build_showcase({"name": "Кабель"})
    assert "Напишите менеджеру" in post
    assert "t.me/" not in post


# ─── Поступления ──────────────────────────────────────────────────────────────


def test_arrival_lists_only_names(isolated_db):
    from services import channel

    post = channel.build_arrival(["Кабель PV 0.6", "ThinkPower 6kw", "Штекер тип C"])
    assert "Кабель PV 0.6" in post
    assert channel.contains_quantities(post) is False
    # Ни номера контейнера, ни поставщика в сборщик даже не передаётся.
    assert "MSKU" not in post


def test_arrival_takes_only_what_actually_came(isolated_db):
    """Обещать то, что не доехало, нельзя."""
    from services import channel, containers

    db = isolated_db
    _setup(db)
    cid = _container(db)
    a = _run(containers.add_item(cid, name="Кабель", expected_qty=500))["item_id"]
    b = _run(containers.add_item(cid, name="Штекер", expected_qty=100))["item_id"]
    _run(containers.add_item(cid, name="Автомат", expected_qty=10))
    _run(containers.set_arrived_quantities(cid, {a: 500, b: 0}, user_id=2))

    names = _run(channel.arrival_names(cid))
    assert names == ["Кабель"]   # Штекер не приехал, Автомат не считали


def test_empty_arrival_makes_no_post(isolated_db):
    """Пост «поступило» без позиций — это шум, за которым перестают следить."""
    from services import channel

    assert channel.build_arrival([]) == ""
    assert channel.build_stale([]) == ""


# ─── Залежавшееся ─────────────────────────────────────────────────────────────


def test_stale_candidates_keep_numbers_for_the_internal_screen(isolated_db):
    """Внутри остаток нужен — по нему решают, что выносить в канал."""
    from services import channel

    rows = channel.stale_candidates([
        {"name": "Кабель", "stock": 500, "unit": "м"},
        {"name": "Штекер", "stock": 1000, "unit": "шт"},
        {"name": "Пустой", "stock": 0, "unit": "шт"},
    ])
    assert [r["name"] for r in rows] == ["Штекер", "Кабель"]   # больше сверху
    assert rows[0]["stock"] == 1000


def test_stale_post_drops_the_numbers(isolated_db):
    from services import channel

    rows = channel.stale_candidates([{"name": "Кабель", "stock": 500, "unit": "м"}])
    post = channel.build_stale([r["name"] for r in rows])
    assert "Кабель" in post
    assert channel.contains_quantities(post) is False


def test_stale_threshold_is_a_setting(isolated_db):
    from services import channel

    db = isolated_db
    _setup(db)
    assert channel.stale_days() == 60
    db.set_setting("stale_stock_days", 30, 2)
    assert channel.stale_days() == 30


# ─── История публикаций ───────────────────────────────────────────────────────


def test_repeat_publication_is_visible(isolated_db):
    """Один и тот же контейнер уходит в канал дважды обычно потому, что первый
    раз забыли."""
    from services import channel

    db = isolated_db
    _setup(db)
    assert _run(channel.already_posted("arrival", "7")) is None

    _run(channel.save_post(kind="arrival", ref="7", message_id=100, posted_by=2))
    posted = _run(channel.already_posted("arrival", "7"))
    assert posted["message_id"] == 100
    assert _run(channel.already_posted("arrival", "8")) is None


def test_history_is_newest_first(isolated_db):
    from services import channel

    db = isolated_db
    _setup(db)
    _run(channel.save_post(kind="arrival", ref="1", message_id=1, posted_by=2))
    _run(channel.save_post(kind="showcase", ref="p-1", message_id=2, posted_by=2))

    rows = _run(channel.history())
    assert [r["message_id"] for r in rows] == [2, 1]


# ─── Ни один сборщик не пропускает цифры ──────────────────────────────────────


def test_no_builder_ever_emits_quantities(isolated_db):
    """Общая проверка на все шаблоны разом: если завтра в шаблон добавят
    остаток, упадёт здесь."""
    from services import channel

    posts = [
        channel.build_showcase({"name": "Кабель PV 0.6"}, price="80 USD",
                               note="Осталось немного", manager_username="m"),
        channel.build_arrival(["Кабель PV 0.6", "ThinkPower 6kw"], note="Забирайте",
                              manager_username="m"),
        channel.build_stale(["Штекер тип C"], manager_username="m"),
    ]
    for post in posts:
        assert channel.contains_quantities(post) is False, post


# ─── Ручки ────────────────────────────────────────────────────────────────────


def _client(monkeypatch, channel_id="-1001111111111"):
    import importlib

    import webapp.server as server

    importlib.reload(roles)
    monkeypatch.setattr(server, "verify_init_data", lambda s: {"id": int(s), "first_name": "U"})
    if channel_id is None:
        monkeypatch.delenv("CHANNEL_ID", raising=False)
    else:
        monkeypatch.setenv("CHANNEL_ID", channel_id)
    from fastapi.testclient import TestClient

    return TestClient(server.app)


def _post(client, path, uid, **body):
    return client.post(path, json={"initData": str(uid), **body})


class _FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append(("text", chat_id, text))
        return type("M", (), {"message_id": 42})()

    async def send_photo(self, chat_id, photo, caption=None, **kw):
        self.sent.append(("photo", chat_id, caption))
        return type("M", (), {"message_id": 43})()


def _patch_bot(monkeypatch, bot):
    import webapp.server as server

    async def _get():
        return bot

    monkeypatch.setattr(server, "get_notify_bot", _get)


def test_draft_is_built_by_the_server(isolated_db, monkeypatch):
    """Текст собирает сервер: правило «без количеств» держится в одном месте."""
    from services import containers

    db = isolated_db
    _setup(db)
    db.set_role(1, "mgr", "Manager", "manager")
    cid = _container(db)
    item = _run(containers.add_item(cid, name="Кабель PV 0.6", expected_qty=500))["item_id"]
    _run(containers.set_arrived_quantities(cid, {item: 500}, user_id=2))

    body = _post(_client(monkeypatch), "/api/channel/draft", 2,
                 kind="arrival", container_id=cid, manager_username="manager").json()
    assert "Кабель PV 0.6" in body["text"]
    assert "MSKU" not in body["text"]
    assert "500" not in body["text"]
    assert body["ref"] == str(cid)


def test_draft_refuses_empty_arrival(isolated_db, monkeypatch):
    db = isolated_db
    _setup(db)
    cid = _container(db)

    r = _post(_client(monkeypatch), "/api/channel/draft", 2, kind="arrival", container_id=cid)
    assert r.status_code == 409


def test_draft_warns_about_repeat(isolated_db, monkeypatch):
    from services import channel, containers

    db = isolated_db
    _setup(db)
    cid = _container(db)
    item = _run(containers.add_item(cid, name="Кабель", expected_qty=1))["item_id"]
    _run(containers.set_arrived_quantities(cid, {item: 1}, user_id=2))
    _run(channel.save_post(kind="arrival", ref=str(cid), message_id=7, posted_by=2))

    body = _post(_client(monkeypatch), "/api/channel/draft", 2,
                 kind="arrival", container_id=cid).json()
    assert body["already_posted"]["message_id"] == 7


def test_publish_sends_and_records(isolated_db, monkeypatch):
    from services import channel

    db = isolated_db
    _setup(db)
    bot = _FakeBot()
    client = _client(monkeypatch)
    _patch_bot(monkeypatch, bot)

    r = _post(client, "/api/channel/publish", 2,
              kind="arrival", ref="7", text="📦 Новое поступление")
    assert r.status_code == 200, r.text
    assert bot.sent[0][1] == -1001111111111
    assert _run(channel.already_posted("arrival", "7"))["message_id"] == 42


def test_publish_without_channel_is_503(isolated_db, monkeypatch):
    db = isolated_db
    _setup(db)
    client = _client(monkeypatch, channel_id=None)

    r = _post(client, "/api/channel/publish", 2, kind="arrival", text="что-то")
    assert r.status_code == 503
    assert "CHANNEL_ID" in r.json()["detail"]


def test_publish_rejects_empty_text(isolated_db, monkeypatch):
    db = isolated_db
    _setup(db)
    _patch_bot(monkeypatch, _FakeBot())
    r = _post(_client(monkeypatch), "/api/channel/publish", 2, kind="arrival", text="   ")
    assert r.status_code == 400


def test_channel_is_boss_only(isolated_db, monkeypatch):
    db = isolated_db
    _setup(db)
    db.set_role(1, "mgr", "Manager", "manager")
    client = _client(monkeypatch)

    assert _post(client, "/api/channel/draft", 1, kind="stale", names=["A"]).status_code == 403
    assert _post(client, "/api/channel/publish", 1, kind="stale", text="x").status_code == 403
    assert _post(client, "/api/channel/history", 1).status_code == 403


def test_stale_draft_takes_names_only(isolated_db, monkeypatch):
    db = isolated_db
    _setup(db)

    body = _post(_client(monkeypatch), "/api/channel/draft", 2,
                 kind="stale", names=["Кабель PV 0.6", "Штекер"]).json()
    assert "Кабель PV 0.6" in body["text"]

    from services import channel

    assert channel.contains_quantities(body["text"]) is False


# ─── Фото товаров ─────────────────────────────────────────────────────────────


def test_product_photo_is_idempotent(isolated_db):
    """Тот же снимок второй раз — не ошибка и не дубль в карточке."""
    from services import product_photos

    db = isolated_db
    _setup(db)
    first = _run(product_photos.add_photo(
        "p-1", tg_file_id="A", file_unique_id="U1", uploaded_by=2))
    second = _run(product_photos.add_photo(
        "p-1", tg_file_id="B", file_unique_id="U1", uploaded_by=2))
    assert first["duplicate"] is False and second["duplicate"] is True
    assert len(_run(product_photos.list_photos("p-1"))) == 1


def test_product_photo_delete_is_scoped(isolated_db):
    """Иначе photo_id из формы стирает снимок чужого товара."""
    from services import product_photos

    db = isolated_db
    _setup(db)
    _run(product_photos.add_photo("p-1", tg_file_id="A", file_unique_id="U1", uploaded_by=2))
    photo_id = _run(product_photos.list_photos("p-1"))[0]["id"]

    assert _run(product_photos.delete_photo("p-2", photo_id))["ok"] is False
    assert _run(product_photos.delete_photo("p-1", photo_id))["ok"] is True


def test_photo_counts_are_batched(isolated_db):
    """Экран выбора товара иначе даёт N+1 на первом же открытии."""
    from services import product_photos

    db = isolated_db
    _setup(db)
    _run(product_photos.add_photo("p-1", tg_file_id="A", file_unique_id="U1", uploaded_by=2))
    _run(product_photos.add_photo("p-1", tg_file_id="B", file_unique_id="U2", uploaded_by=2))
    _run(product_photos.add_photo("p-2", tg_file_id="C", file_unique_id="U3", uploaded_by=2))

    assert _run(product_photos.photos_by_products(["p-1", "p-2", "p-3"])) == {"p-1": 2, "p-2": 1}
    assert _run(product_photos.photos_by_products([])) == {}


def test_photo_list_never_leaks_file_ids(isolated_db, monkeypatch):
    """Файловый URL Telegram содержит токен бота — наружу идут только id."""
    from services import product_photos

    db = isolated_db
    _setup(db)
    _run(product_photos.add_photo(
        "p-1", tg_file_id="AgAC-secret", file_unique_id="U1", uploaded_by=2))

    r = _post(_client(monkeypatch), "/api/products/photos", 2, ms_id="p-1")
    assert r.status_code == 200, r.text
    assert "AgAC-secret" not in r.text
    assert set(r.json()["photos"][0]) == {"id", "caption", "uploaded_at"}


# ─── Отклик на пост ───────────────────────────────────────────────────────────
#
# Это КОРРЕЛЯЦИЯ, а не атрибуция: ссылка под постом ведёт прямо в личку
# менеджера и метки не несёт. Поэтому проверяем ровно то, что обещаем, —
# «сколько обращений пришло ПОСЛЕ поста», а не «сколько пришло ИЗ поста».

from datetime import timedelta

from utils.helpers import local_now


def _stamp(**kw):
    return (local_now().replace(tzinfo=None) - timedelta(**kw)).strftime("%Y-%m-%d %H:%M:%S")


def _lead(uid, at):
    from services import leads

    _run(leads.record_message(tg_user_id=uid, manager_id=1, inbound=True, at=at))


def test_effect_counts_only_the_window_after_the_post(isolated_db):
    from services import channel

    db = isolated_db
    _setup(db)
    posted = _stamp(days=3)
    _lead(101, _stamp(days=3, hours=-2))   # через 2 часа после поста
    _lead(102, _stamp(days=3, hours=-20))  # через 20 часов — ещё в окне
    _lead(103, _stamp(days=1))             # спустя двое суток — уже вне окна
    _lead(104, _stamp(days=4))             # за сутки ДО поста

    effect = _run(channel.post_effect(posted))
    assert effect["after"] == 2
    assert effect["window_hours"] == channel.EFFECT_WINDOW_HOURS


def test_effect_reports_the_ordinary_day_for_comparison(isolated_db):
    """Без фона число «7 обращений» ничего не значит: семь при обычных двух и
    семь при обычных восьми — разные новости."""
    from services import channel

    db = isolated_db
    _setup(db)
    posted = _stamp(days=1)
    # Ровно 15 обращений за 30 дней до поста → обычный день = 0,5.
    for i in range(15):
        _lead(1000 + i, _stamp(days=2 + i))

    effect = _run(channel.post_effect(posted))
    assert effect["baseline"] == round(15 / channel.EFFECT_BASELINE_DAYS, 1) == 0.5
    assert effect["after"] == 0, "до поста никого не считаем в окно после него"


def test_effect_does_not_count_the_baseline_twice(isolated_db):
    """Обращения из окна после поста не должны попадать ещё и в фон — иначе
    удачный пост сам себе поднимает планку и выглядит обычным."""
    from services import channel

    db = isolated_db
    _setup(db)
    posted = _stamp(days=5)
    _lead(201, _stamp(days=5, hours=-1))   # в окне после поста
    _lead(202, _stamp(days=6))             # в фоне до поста

    effect = _run(channel.post_effect(posted))
    assert effect["after"] == 1
    assert effect["baseline"] == round(1 / channel.EFFECT_BASELINE_DAYS, 1)


def test_effect_on_empty_history_is_zero_not_none(isolated_db):
    """Пустой фон — законный ответ («канал новый»), а не отсутствие данных."""
    from services import channel

    db = isolated_db
    _setup(db)
    effect = _run(channel.post_effect(_stamp(days=1)))
    assert effect == {"after": 0, "baseline": 0.0, "window_hours": channel.EFFECT_WINDOW_HOURS}


def test_effect_is_none_for_a_post_without_a_timestamp(isolated_db):
    """Считать не от чего — честнее не показывать строку вовсе."""
    from services import channel

    db = isolated_db
    _setup(db)
    assert _run(channel.post_effect(None)) is None
    assert _run(channel.post_effect("не дата")) is None
