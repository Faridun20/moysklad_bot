"""
Контейнеры: что едет, что здесь и сошёлся ли состав.

Главное, что проверяем, — сверку. Она врёт молча: «не считали» легко спутать с
«приехало ноль», а позиция из чужого контейнера, попавшая в приёмку, испортит
итог, которому потом верят.

БД настоящая (isolated_db), корутины через asyncio.run — pytest-asyncio в
проекте нет.
"""

import asyncio
import importlib

from fastapi.testclient import TestClient

import services.roles as roles


def _run(coro):
    return asyncio.run(coro)


def _setup(db):
    roles.invalidate_all_roles()
    db.set_role(1, "mgr", "Manager", "manager")
    db.set_role(2, "boss", "Boss", "boss")
    db.set_role(3, "acc", "Bookkeeper", "bookkeeper")


def _container(number="MSKU-1234567", **over):
    from services import containers

    payload = {"number": number, "created_by": 2, "creator_name": "Boss"}
    payload.update(over)
    res = _run(containers.create_container(**payload))
    assert res["ok"], res
    return res["container_id"]


def _item(cid, name="Кабель PV 0.6", expected=500, **over):
    from services import containers

    res = _run(containers.add_item(cid, name=name, expected_qty=expected, **over))
    assert res["ok"], res
    return res["item_id"]


def _close_window(db, cid, days=3):
    """Отодвинуть дату прибытия в прошлое — окно правки закрывается само."""
    from datetime import timedelta

    from utils.helpers import local_now

    past = (local_now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(db.q("UPDATE containers SET arrived_at = ? WHERE id = ?"), (past, cid))
        conn.commit()


# ─── Заведение ────────────────────────────────────────────────────────────────


def test_number_is_normalized(isolated_db):
    from services import containers

    assert containers.normalize_number(" msku-123 4567 ") == "MSKU1234567"
    assert containers.normalize_number(None) == ""


def test_same_number_written_differently_is_rejected(isolated_db):
    """Номер приезжает то из накладной, то из мессенджера — без нормализации
    UNIQUE не спасает и на один контейнер появятся две карточки."""
    from services import containers

    db = isolated_db
    _setup(db)
    _container("MSKU-1234567")
    res = _run(containers.create_container(number="msku 1234567", created_by=2))
    assert res["ok"] is False
    assert "уже заведён" in res["error"]


def test_new_container_is_in_transit(isolated_db):
    from services import containers

    db = isolated_db
    _setup(db)
    cid = _container()
    assert _run(containers.get_container(cid))["status"] == "in_transit"
    assert _run(containers.count_by_status()) == {"in_transit": 1, "arrived": 0, "all": 1}


# ─── Сверка ───────────────────────────────────────────────────────────────────


def test_unchecked_is_not_a_discrepancy(isolated_db):
    """«Ещё не считали» и «приехало ноль» — разные вещи: в первом случае
    непроверенный контейнер выглядел бы как полностью недостающий."""
    from services import containers

    db = isolated_db
    _setup(db)
    cid = _container()
    _item(cid, expected=500)

    rows = containers.diff(_run(containers.list_items(cid)))
    assert rows[0]["state"] == "unchecked"
    assert rows[0]["delta"] is None
    summary = containers.diff_summary(rows)
    assert summary["mismatch"] == 0 and summary["unchecked"] == 1


def test_zero_arrived_is_a_shortage(isolated_db):
    """Ноль означает «искали и не нашли» — это недостача целиком."""
    from services import containers

    db = isolated_db
    _setup(db)
    cid = _container()
    item = _item(cid, expected=500)
    _run(containers.set_arrived_quantities(cid, {item: 0}, user_id=2))

    rows = containers.diff(_run(containers.list_items(cid)))
    assert rows[0]["state"] == "short"
    assert rows[0]["delta"] == -500


def test_diff_counts_shortage_and_surplus(isolated_db):
    from services import containers

    db = isolated_db
    _setup(db)
    cid = _container()
    same = _item(cid, "Кабель", 500)
    short = _item(cid, "ThinkPower 6kw", 20)
    extra = _item(cid, "Автомат C16", 0)
    _run(containers.set_arrived_quantities(
        cid, {same: 500, short: 18, extra: 5}, user_id=2
    ))

    rows = containers.diff(_run(containers.list_items(cid)))
    by_name = {r["name"]: r for r in rows}
    assert by_name["Кабель"]["state"] == "match"
    assert by_name["ThinkPower 6kw"]["delta"] == -2
    # Автомат не заявляли — расхождение бывает только с тем, что обещали.
    assert by_name["Автомат C16"]["state"] == "received"
    assert by_name["Автомат C16"]["declared"] is False

    summary = containers.diff_summary(rows)
    assert summary == {"total": 3, "unchecked": 0, "short": 1, "extra": 0,
                       "received": 1, "mismatch": 1}


def test_undeclared_items_are_not_a_mismatch(isolated_db):
    """Контейнер, состав которого не заводили заранее, — это опись прибывшего,
    а не сверка: красных строк в нём быть не должно."""
    from services import containers

    db = isolated_db
    _setup(db)
    cid = _container()
    a = _item(cid, "Кабель", 0)
    b = _item(cid, "Штекер", 0)
    _run(containers.set_arrived_quantities(cid, {a: 500, b: 1000}, user_id=2))

    summary = containers.diff_summary(containers.diff(_run(containers.list_items(cid))))
    assert summary["mismatch"] == 0
    assert summary["received"] == 2


def test_check_can_be_taken_back(isolated_db):
    """Приёмщик должен уметь отменить свою же опечатку, а не только записать
    ноль — иначе «не считали» вернуть нечем."""
    from services import containers

    db = isolated_db
    _setup(db)
    cid = _container()
    item = _item(cid, expected=10)
    _run(containers.set_arrived_quantities(cid, {item: 7}, user_id=2))
    _run(containers.set_arrived_quantities(cid, {item: ""}, user_id=2))

    assert containers.diff(_run(containers.list_items(cid)))[0]["state"] == "unchecked"


def test_item_from_another_container_is_rejected(isolated_db):
    """Подстановка чужого id испортила бы итог, которому потом верят."""
    from services import containers

    db = isolated_db
    _setup(db)
    mine = _container("A-1")
    theirs = _container("B-2")
    alien = _item(theirs)

    res = _run(containers.set_arrived_quantities(mine, {alien: 5}, user_id=2))
    assert res["ok"] is False
    assert "не из этого" in res["error"]


def test_negative_quantity_is_rejected(isolated_db):
    from services import containers

    db = isolated_db
    _setup(db)
    cid = _container()
    item = _item(cid)
    res = _run(containers.set_arrived_quantities(cid, {item: -1}, user_id=2))
    assert res["ok"] is False


# ─── Прибытие ─────────────────────────────────────────────────────────────────


def test_arrival_is_cas(isolated_db):
    """Двое отметили приёмку одновременно — ответы не должны разойтись."""
    from services import containers

    db = isolated_db
    _setup(db)
    cid = _container()

    assert _run(containers.mark_arrived(cid, user_id=2))["ok"] is True
    second = _run(containers.mark_arrived(cid, user_id=1))
    assert second["ok"] is False
    assert second["current"] == "arrived"


def test_arrived_container_leaves_the_in_transit_shelf(isolated_db):
    from services import containers

    db = isolated_db
    _setup(db)
    cid = _container()
    _container("B-2")
    _run(containers.mark_arrived(cid, user_id=2))

    in_transit = _run(containers.list_containers("in_transit"))
    assert [c["number"] for c in in_transit] == ["B2"]
    assert _run(containers.count_by_status())["arrived"] == 1


def test_arrived_container_is_deletable_while_the_window_is_open(isolated_db):
    """Приёмку могли завести не на тот контейнер — запрет означал бы вечную
    неверную строку в списке."""
    from services import containers

    db = isolated_db
    _setup(db)
    cid = _container()
    _run(containers.mark_arrived(cid, user_id=2))

    assert _run(containers.delete_container(cid, user_id=2))["ok"] is True


def test_closed_reception_is_not_deletable(isolated_db):
    """После закрытия окна это история приёмки, по которой уже принимали
    решения."""
    from services import containers

    db = isolated_db
    _setup(db)
    cid = _container()
    _run(containers.mark_arrived(cid, user_id=2))
    _close_window(db, cid)

    res = _run(containers.delete_container(cid, user_id=2))
    assert res["ok"] is False
    assert "закрыта" in res["error"]


def test_delete_removes_the_items_too(isolated_db):
    from services import containers

    db = isolated_db
    _setup(db)
    cid = _container()
    _item(cid)
    assert _run(containers.delete_container(cid, user_id=2))["ok"]
    assert _run(containers.list_items(cid)) == []


# ─── Ручки ────────────────────────────────────────────────────────────────────


def _client(monkeypatch):
    import webapp.server as server

    importlib.reload(roles)
    monkeypatch.setattr(server, "verify_init_data", lambda s: {"id": int(s), "first_name": "U"})
    return TestClient(server.app)


def _post(client, path, uid, **body):
    return client.post(path, json={"initData": str(uid), **body})


def test_list_carries_the_discrepancy_counter(isolated_db, monkeypatch):
    """Иначе «сверить» значит открыть каждый контейнер по очереди."""
    from services import containers

    db = isolated_db
    _setup(db)
    cid = _container()
    item = _item(cid, expected=20)
    _run(containers.set_arrived_quantities(cid, {item: 18}, user_id=2))

    body = _post(_client(monkeypatch), "/api/containers/list", 1).json()
    assert body["containers"][0]["diff"]["mismatch"] == 1


def test_card_shows_expected_against_arrived(isolated_db, monkeypatch):
    from services import containers

    db = isolated_db
    _setup(db)
    cid = _container()
    item = _item(cid, "ThinkPower 6kw", 20)
    _run(containers.set_arrived_quantities(cid, {item: 18}, user_id=2))

    body = _post(_client(monkeypatch), "/api/containers/card", 2, container_id=cid).json()
    row = body["items"][0]
    assert row["expected_qty"] == 20 and row["arrived_qty"] == 18
    assert row["delta"] == -2 and row["state"] == "short"
    assert body["diff"]["short"] == 1


def test_full_flow_through_the_api(isolated_db, monkeypatch):
    db = isolated_db
    _setup(db)
    client = _client(monkeypatch)

    created = _post(client, "/api/containers/create", 1,
                    number="msku-777 888", eta_date="2026-08-12", idempotency_key="k1")
    assert created.status_code == 200, created.text
    cid = created.json()["container_id"]
    assert created.json()["number"] == "MSKU777888"

    assert _post(client, "/api/containers/item_add", 1, container_id=cid,
                 name="Кабель PV 0.6", expected_qty=500).status_code == 200
    assert _post(client, "/api/containers/arrive", 1, container_id=cid).status_code == 200

    card = _post(client, "/api/containers/card", 1, container_id=cid).json()
    item_id = card["items"][0]["id"]
    saved = _post(client, "/api/containers/check", 1, container_id=cid,
                  quantities={str(item_id): 480})
    assert saved.status_code == 200, saved.text

    card = _post(client, "/api/containers/card", 1, container_id=cid).json()
    assert card["items"][0]["delta"] == -20
    assert card["diff"]["mismatch"] == 1


def test_repeated_arrive_is_409(isolated_db, monkeypatch):
    db = isolated_db
    _setup(db)
    cid = _container()
    client = _client(monkeypatch)

    assert _post(client, "/api/containers/arrive", 1, container_id=cid).status_code == 200
    r = _post(client, "/api/containers/arrive", 1, container_id=cid)
    assert r.status_code == 409
    assert r.json()["current"] == "arrived"


def test_duplicate_number_is_400(isolated_db, monkeypatch):
    db = isolated_db
    _setup(db)
    _container("MSKU-1")
    client = _client(monkeypatch)

    r = _post(client, "/api/containers/create", 1, number="msku 1", idempotency_key="k")
    assert r.status_code == 400
    assert "уже заведён" in r.json()["detail"]


def test_delete_is_boss_only(isolated_db, monkeypatch):
    db = isolated_db
    _setup(db)
    cid = _container()
    client = _client(monkeypatch)

    assert _post(client, "/api/containers/delete", 1, container_id=cid).status_code == 403
    assert _post(client, "/api/containers/delete", 2, container_id=cid).status_code == 200


def test_bookkeeper_has_no_access(isolated_db, monkeypatch):
    db = isolated_db
    _setup(db)
    cid = _container()
    client = _client(monkeypatch)

    assert _post(client, "/api/containers/list", 3).status_code == 403
    assert _post(client, "/api/containers/card", 3, container_id=cid).status_code == 403


def test_item_delete_is_scoped_to_its_container(isolated_db, monkeypatch):
    db = isolated_db
    _setup(db)
    mine = _container("A-1")
    theirs = _container("B-2")
    alien = _item(theirs)
    client = _client(monkeypatch)

    r = _post(client, "/api/containers/item_delete", 1, container_id=mine, item_id=alien)
    assert r.status_code == 404
    from services import containers

    assert len(_run(containers.list_items(theirs))) == 1


def test_unknown_status_filter_is_400(isolated_db, monkeypatch):
    db = isolated_db
    _setup(db)
    r = _post(_client(monkeypatch), "/api/containers/list", 1, status="edet")
    assert r.status_code == 400


# ─── Сирот после удаления быть не должно ──────────────────────────────────────


def _tables_referencing(db, parent: str) -> list[str]:
    """Все таблицы с внешним ключом на `parent` — по самой схеме, а не по списку
    в тесте: список пришлось бы дописывать руками, а именно этого и не делают."""
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        names = [r[0] for r in cur.fetchall()]
        out = []
        for name in names:
            cur.execute(f"PRAGMA foreign_key_list({name})")
            if any(row[2] == parent for row in cur.fetchall()):
                out.append(name)
    return out


def test_delete_leaves_no_orphans_anywhere(isolated_db):
    """Регресс: `container_supply` не чистился при удалении контейнера.

    На SQLite внешние ключи выключены, поэтому сирота не мешает и тест на
    «удалилось» проходил; на Postgres FK энфорсится, и удаление падало 500-й.
    Проверяем не конкретную таблицу, а ВСЕ дочерние по схеме — тогда следующая
    такая таблица поймается сама, без правки этого теста.
    """
    from services import containers, ms_supply

    db = isolated_db
    _setup(db)
    cid = _container()
    _item(cid, "Кабель", 500)
    _run(ms_supply.set_supplier(cid, ms_id="agent-1", name="ООО Поставщик"))

    children = _tables_referencing(db, "containers")
    assert "container_items" in children and "container_supply" in children, children
    # Список в сервисе обязан покрывать схему — иначе он и есть источник бага.
    assert set(children) <= set(containers.CHILD_TABLES), (
        f"в схеме есть дети контейнера, которых нет в CHILD_TABLES: "
        f"{sorted(set(children) - set(containers.CHILD_TABLES))}"
    )

    assert _run(containers.delete_container(cid, user_id=2))["ok"] is True

    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        for table in children:
            cur.execute(db.q(f"SELECT COUNT(*) FROM {table} WHERE container_id = ?"), (cid,))
            assert cur.fetchone()[0] == 0, f"осиротевшие строки в {table}"


def test_container_with_supplier_is_deletable(isolated_db):
    """Боевой сценарий из логов: у контейнера задан поставщик — удаление
    падало ForeignKeyViolationError."""
    from services import containers, ms_supply

    db = isolated_db
    _setup(db)
    cid = _container()
    _run(ms_supply.set_supplier(cid, ms_id="agent-1", name="ООО Поставщик"))

    assert _run(containers.delete_container(cid, user_id=2))["ok"] is True
    assert _run(ms_supply.get_link(cid)) == {"unmatched": []}


# ─── Привязка позиции к номенклатуре ──────────────────────────────────────────


def _stock(db, ms_id, name, unit="шт"):
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(
            db.q("INSERT INTO ms_stock (ms_id, name, folder_id, folder_name, unit, stock, "
                 "reserve) VALUES (?, ?, '', '', ?, 0, 0)"),
            (ms_id, name, unit),
        )
        conn.commit()


def test_item_can_be_added_with_a_catalog_product(isolated_db):
    from services import containers

    db = isolated_db
    _setup(db)
    cid = _container()
    _run(containers.add_item(cid, name="Кабель PV 0.6", expected_qty=500, ms_id="p-1"))

    item = _run(containers.list_items(cid))[0]
    assert item["ms_id"] == "p-1"
    assert item["ms_name"] == "Кабель PV 0.6"


def test_free_text_item_stays_legal(isolated_db):
    """В контейнере регулярно едет то, чего в номенклатуре ещё нет: требовать
    карточку до сохранения значит остановить приёмку на полпути."""
    from services import containers

    db = isolated_db
    _setup(db)
    cid = _container()
    _item(cid, "Штекер тип C", 10)

    assert _run(containers.list_items(cid))[0]["ms_id"] is None


def test_link_replaces_previous_choice(isolated_db):
    from services import containers

    db = isolated_db
    _setup(db)
    cid = _container()
    item = _item(cid, "Кабель", 10)
    assert _run(containers.link_item(cid, item, ms_id="p-1", ms_name="Кабель PV 0.6"))["ok"]
    assert _run(containers.link_item(cid, item, ms_id="p-2", ms_name="Кабель PV 1.0"))["ok"]

    rows = _run(containers.list_items(cid))
    assert len(rows) == 1
    assert rows[0]["ms_id"] == "p-2"


def test_link_refuses_item_from_another_container(isolated_db):
    """Позицию всегда ищем ВНУТРИ заявленного контейнера — иначе подстановка
    чужого id перевесит приход на другую карточку."""
    from services import containers

    db = isolated_db
    _setup(db)
    mine = _container("MSKU-1111111")
    other = _container("MSKU-2222222")
    stranger = _item(other, "Кабель", 10)

    res = _run(containers.link_item(mine, stranger, ms_id="p-1"))
    assert res["ok"] is False
    assert "не найдена" in res["error"]


def test_link_is_refused_after_the_window_closes(isolated_db):
    from services import containers

    db = isolated_db
    _setup(db)
    cid = _container()
    item = _item(cid, "Кабель", 10)
    _run(containers.mark_arrived(cid, user_id=2))
    _close_window(db, cid)

    res = _run(containers.link_item(cid, item, ms_id="p-1"))
    assert res["ok"] is False
    assert res.get("window_closed") is True


def test_deleting_item_takes_its_link_along(isolated_db):
    """На Postgres живая связь отвергает DELETE по составу; на SQLite молчит."""
    from services import containers

    db = isolated_db
    _setup(db)
    cid = _container()
    item = _item(cid, "Кабель", 10)
    _run(containers.link_item(cid, item, ms_id="p-1"))

    assert _run(containers.delete_item(cid, item))["ok"] is True
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(db.q("SELECT COUNT(*) FROM container_item_links WHERE item_id = ?"), (item,))
        assert cur.fetchone()[0] == 0


def test_child_tables_are_ordered_leaves_first(isolated_db):
    """Порядок в CHILD_TABLES — не косметика.

    `container_item_links` ссылается и на контейнер, и на позицию: удали
    позиции первыми — Postgres отвергнет DELETE, а SQLite с выключенными FK
    промолчит, и баг снова доедет до прода. Порядок выводим из схемы, чтобы
    следующая такая таблица поймалась сама.
    """
    from services import containers

    db = isolated_db
    order = {name: i for i, name in enumerate(containers.CHILD_TABLES)}
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        for child in containers.CHILD_TABLES:
            cur.execute(f"PRAGMA foreign_key_list({child})")
            for row in cur.fetchall():
                target = row[2]
                if target in order and target != child:
                    assert order[child] < order[target], (
                        f"{child} ссылается на {target}, значит должна удаляться раньше"
                    )


def test_orphan_guard_covers_the_link_table(isolated_db):
    """Сторож сирот обязан видеть новую дочернюю таблицу — она с FK на
    контейнер, а значит попадает в выборку по схеме."""
    from services import containers

    db = isolated_db
    _setup(db)
    cid = _container()
    item = _item(cid, "Кабель", 10)
    _run(containers.link_item(cid, item, ms_id="p-1"))

    assert "container_item_links" in _tables_referencing(db, "containers")
    assert _run(containers.delete_container(cid, user_id=2))["ok"] is True
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(db.q("SELECT COUNT(*) FROM container_item_links WHERE container_id = ?"), (cid,))
        assert cur.fetchone()[0] == 0


def test_product_search_reads_the_snapshot(isolated_db, monkeypatch):
    db = isolated_db
    _setup(db)
    _stock(db, "p-1", "Кабель PV 0.6")
    _stock(db, "p-2", "ThinkPower 6kw")

    body = _post(_client(monkeypatch), "/api/products/search", 1, query="кабель").json()
    assert [p["ms_id"] for p in body["products"]] == ["p-1"]


def test_product_search_ignores_a_single_letter(isolated_db, monkeypatch):
    """Одна буква — это ещё не запрос: отдавать по ней первые двадцать товаров
    каталога значит подсунуть случайный выбор под палец."""
    db = isolated_db
    _setup(db)
    _stock(db, "p-1", "Кабель PV 0.6")

    body = _post(_client(monkeypatch), "/api/products/search", 1, query="к").json()
    assert body["products"] == []


def test_item_link_endpoint_binds_the_position(isolated_db, monkeypatch):
    from services import containers

    db = isolated_db
    _setup(db)
    cid = _container()
    item = _item(cid, "Кабель", 10)

    res = _post(_client(monkeypatch), "/api/containers/item_link", 1,
                container_id=cid, item_id=item, ms_id="p-1", ms_name="Кабель PV 0.6")
    assert res.status_code == 200, res.text
    assert _run(containers.list_items(cid))[0]["ms_id"] == "p-1"
