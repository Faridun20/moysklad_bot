"""
Приёмка в МойСклад по прибывшему контейнеру.

Главный риск здесь — молча пропущенная позиция: остаток, которого нет на
складе, но который считают существующим. Поэтому проверяем не «документ
создался», а что именно в него попало и что вернулось несопоставленным.

Мок стоит на границе с МС (HTTP через aioresponses), сборка URL и payload
исполняется по-настоящему — конвенция тестов проекта.
"""

import asyncio

from aioresponses import aioresponses

import services.roles as roles
from services.moysklad import MS_BASE

SUPPLY_URL = f"{MS_BASE}/entity/supply"


def _run(coro):
    return asyncio.run(coro)


def _setup(db):
    roles.invalidate_all_roles()
    db.set_role(2, "boss", "Boss", "boss")


def _container(db, number="MSKU-1", notes=None):
    from services import containers

    res = _run(containers.create_container(
        number=number, created_by=2, creator_name="Boss", notes=notes
    ))
    assert res["ok"], res
    return res["container_id"]


def _item(cid, name, expected=0, arrived=None):
    from services import containers

    res = _run(containers.add_item(cid, name=name, expected_qty=expected, arrived_qty=arrived))
    assert res["ok"], res
    return res["item_id"]


def _stock(db, ms_id, name, unit="шт"):
    """Товар в снапшоте номенклатуры — по нему и идёт сопоставление."""
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(
            db.q("INSERT INTO ms_stock (ms_id, name, folder_id, folder_name, unit, stock, reserve) "
                 "VALUES (?, ?, '', '', ?, 0, 0)"),
            (ms_id, name, unit),
        )
        conn.commit()


def _ready_ms(monkeypatch):
    """Контекст МС (организация/склад) — иначе приёмку создавать не из чего."""
    from services import ms_demand

    monkeypatch.setitem(ms_demand._CTX, "ready", True)
    monkeypatch.setitem(ms_demand._CTX, "org_meta", {"href": f"{MS_BASE}/entity/organization/org-1"})
    monkeypatch.setitem(ms_demand._CTX, "store_meta", {"href": f"{MS_BASE}/entity/store/store-1"})


def _with_supplier(cid, ms_id="agent-1", name="ООО Поставщик"):
    from services import ms_supply

    assert _run(ms_supply.set_supplier(cid, ms_id=ms_id, name=name))["ok"]


# ─── Сопоставление по названию ────────────────────────────────────────────────


def test_exact_name_matches(isolated_db):
    from services import containers, ms_supply

    db = isolated_db
    _setup(db)
    _stock(db, "p-1", "Кабель PV 0.6")
    cid = _container(db)
    _item(cid, "  кабель   pv 0.6 ", arrived=500)

    matched, unmatched = ms_supply.match_items(_run(containers.list_items(cid)))
    assert unmatched == []
    assert matched[0]["ms_id"] == "p-1"
    assert matched[0]["quantity"] == 500


def test_unknown_product_is_reported_not_skipped(isolated_db):
    """Молча пропущенная позиция — это остаток, которого нет на складе, но
    который считают существующим."""
    from services import containers, ms_supply

    db = isolated_db
    _setup(db)
    cid = _container(db)
    _item(cid, "Штекер тип C", arrived=1000)

    matched, unmatched = ms_supply.match_items(_run(containers.list_items(cid)))
    assert matched == []
    assert unmatched[0]["name"] == "Штекер тип C"
    assert unmatched[0]["quantity"] == 1000


def test_similar_names_are_not_guessed(isolated_db):
    """«Кабель PV 0.6» и «Кабель PV 0.6 чёрный» — разные товары; угадывание
    означает приход не на ту карточку."""
    from services import containers, ms_supply

    db = isolated_db
    _setup(db)
    _stock(db, "p-1", "Кабель PV 0.6 чёрный")
    cid = _container(db)
    _item(cid, "Кабель PV 0.6", arrived=10)

    matched, unmatched = ms_supply.match_items(_run(containers.list_items(cid)))
    assert matched == []
    assert unmatched[0]["reason"] == "не найден в номенклатуре"


def test_nothing_arrived_is_not_a_position(isolated_db):
    from services import containers, ms_supply

    db = isolated_db
    _setup(db)
    _stock(db, "p-1", "Кабель")
    cid = _container(db)
    _item(cid, "Кабель", expected=100, arrived=0)

    matched, unmatched = ms_supply.match_items(_run(containers.list_items(cid)))
    assert matched == [] and unmatched == []


# ─── Создание документа ───────────────────────────────────────────────────────


def test_supply_is_created_with_zero_prices(isolated_db, monkeypatch):
    """Человек в этот момент считает коробки, а не деньги: пустая цена лучше
    выдуманной."""
    from services import containers, ms_supply

    db = isolated_db
    _setup(db)
    _ready_ms(monkeypatch)
    _stock(db, "p-1", "Кабель")
    cid = _container(db, notes="Запчасти для JCB")
    _item(cid, "Кабель", expected=500, arrived=498)
    _run(containers.mark_arrived(cid, user_id=2))
    _with_supplier(cid)

    with aioresponses() as m:
        m.post(SUPPLY_URL, payload={"id": "supply-1"})
        res = _run(ms_supply.sync_supply(cid))
        sent = next(iter(m.requests.values()))[0].kwargs["json"]

    assert res["ok"] and res["ms_id"] == "supply-1"
    assert sent["positions"][0]["quantity"] == 498
    assert sent["positions"][0]["price"] == 0
    assert sent["agent"]["meta"]["href"].endswith("/counterparty/agent-1")
    assert sent["description"] == "Запчасти для JCB"


def test_repeat_updates_instead_of_duplicating(isolated_db, monkeypatch):
    """Количества правятся сутки, и документ обязан ехать за ними — иначе
    исправленная недостача останется неверной там, где по ней торгуют."""
    from services import containers, ms_supply

    db = isolated_db
    _setup(db)
    _ready_ms(monkeypatch)
    _stock(db, "p-1", "Кабель")
    cid = _container(db)
    item = _item(cid, "Кабель", expected=500, arrived=498)
    _run(containers.mark_arrived(cid, user_id=2))
    _with_supplier(cid)

    with aioresponses() as m:
        m.post(SUPPLY_URL, payload={"id": "supply-1"})
        _run(ms_supply.sync_supply(cid))

    _run(containers.set_arrived_quantities(cid, {item: 500}, user_id=2))
    with aioresponses() as m:
        m.put(f"{SUPPLY_URL}/supply-1", payload={"id": "supply-1"})
        res = _run(ms_supply.sync_supply(cid))
        sent = next(iter(m.requests.values()))[0].kwargs["json"]

    assert res["updated"] is True
    assert sent["positions"][0]["quantity"] == 500
    # При обновлении шапку не пересылаем — меняются только строки.
    assert "syncId" not in sent


def test_supplier_is_required(isolated_db, monkeypatch):
    from services import containers, ms_supply

    db = isolated_db
    _setup(db)
    _ready_ms(monkeypatch)
    _stock(db, "p-1", "Кабель")
    cid = _container(db)
    _item(cid, "Кабель", arrived=10)
    _run(containers.mark_arrived(cid, user_id=2))

    res = _run(ms_supply.sync_supply(cid))
    assert res["ok"] is False
    assert res["needs_supplier"] is True


def test_container_in_transit_is_not_posted(isolated_db, monkeypatch):
    from services import ms_supply

    db = isolated_db
    _setup(db)
    _ready_ms(monkeypatch)
    cid = _container(db)
    _with_supplier(cid)

    res = _run(ms_supply.sync_supply(cid))
    assert res["ok"] is False
    assert "прибывший" in res["error"]


def test_ms_error_does_not_crash(isolated_db, monkeypatch):
    from services import containers, ms_supply

    db = isolated_db
    _setup(db)
    _ready_ms(monkeypatch)
    _stock(db, "p-1", "Кабель")
    cid = _container(db)
    _item(cid, "Кабель", arrived=10)
    _run(containers.mark_arrived(cid, user_id=2))
    _with_supplier(cid)

    with aioresponses() as m:
        m.post(SUPPLY_URL, status=412, body='{"errors":[{"error":"нет прав"}]}')
        res = _run(ms_supply.sync_supply(cid))
    assert res["ok"] is False
    assert "МойСклад" in res["error"]
    # Документ не записан — повтор должен создавать заново, а не «обновлять».
    assert _run(ms_supply.get_link(cid)).get("ms_supply_id") in (None, "")


def test_unmatched_survives_for_the_screen(isolated_db, monkeypatch):
    from services import containers, ms_supply

    db = isolated_db
    _setup(db)
    _ready_ms(monkeypatch)
    _stock(db, "p-1", "Кабель")
    cid = _container(db)
    _item(cid, "Кабель", arrived=10)
    _item(cid, "Штекер тип C", arrived=1000)
    _run(containers.mark_arrived(cid, user_id=2))
    _with_supplier(cid)

    with aioresponses() as m:
        m.post(SUPPLY_URL, payload={"id": "supply-1"})
        res = _run(ms_supply.sync_supply(cid))

    assert res["matched"] == 1
    assert [u["name"] for u in res["unmatched"]] == ["Штекер тип C"]
    assert [u["name"] for u in _run(ms_supply.get_link(cid))["unmatched"]] == ["Штекер тип C"]


def test_ms_not_configured_is_a_clear_refusal(isolated_db, monkeypatch):
    from services import containers, ms_demand, ms_supply

    db = isolated_db
    _setup(db)
    monkeypatch.setitem(ms_demand._CTX, "ready", False)
    cid = _container(db)
    _run(containers.mark_arrived(cid, user_id=2))

    res = _run(ms_supply.sync_supply(cid))
    assert res["ok"] is False
    assert "не настроен" in res["error"]


# ─── Выбор товара человеком ───────────────────────────────────────────────────


PRODUCT_URL = f"{MS_BASE}/entity/product"


def test_chosen_product_wins_over_the_name(isolated_db):
    """Человек выбрал карточку — переспрашивать у поиска, что он имел в виду,
    незачем. Тем более что по имени нашёлся бы ДРУГОЙ товар."""
    from services import containers, ms_supply

    db = isolated_db
    _setup(db)
    _stock(db, "p-name", "Кабель")
    cid = _container(db)
    _run(containers.add_item(cid, name="Кабель", arrived_qty=10, ms_id="p-chosen"))

    matched, unmatched = ms_supply.match_items(_run(containers.list_items(cid)))
    assert unmatched == []
    assert matched[0]["ms_id"] == "p-chosen"


def test_name_matching_still_covers_old_positions(isolated_db):
    """Позиции, заведённые до появления выбора, привязки не имеют — и должны
    по-прежнему сопоставляться по имени."""
    from services import containers, ms_supply

    db = isolated_db
    _setup(db)
    _stock(db, "p-1", "Кабель PV 0.6")
    cid = _container(db)
    _item(cid, "Кабель PV 0.6", arrived=10)

    matched, _ = ms_supply.match_items(_run(containers.list_items(cid)))
    assert matched[0]["ms_id"] == "p-1"


def test_unmatched_carries_the_item_id(isolated_db):
    """Без id несопоставленное остаётся списком «сходите заведите» — починить
    его прямо из карточки нечем."""
    from services import containers, ms_supply

    db = isolated_db
    _setup(db)
    cid = _container(db)
    item = _item(cid, "Штекер тип C", arrived=5)

    _, unmatched = ms_supply.match_items(_run(containers.list_items(cid)))
    assert unmatched[0]["item_id"] == item


def test_create_product_reuses_an_existing_card(isolated_db):
    """Вторая строка контейнера с тем же названием не должна заводить второй
    такой же товар — остаток разъехался бы по двум карточкам."""
    from services import ms_supply

    db = isolated_db
    _setup(db)
    _stock(db, "p-1", "Кабель PV 0.6")

    with aioresponses() as m:
        res = _run(ms_supply.create_product("  кабель   pv 0.6 "))
        assert m.requests == {}, "существующий товар не должен стоить запроса в МС"

    assert res["ok"] and res["ms_id"] == "p-1" and res["existed"] is True


def test_created_product_lands_in_the_snapshot(isolated_db):
    """Иначе до ночного полного среза его не найдёт ни поиск, ни сопоставление,
    и человек заведёт карточку повторно."""
    from services import ms_supply, snapshot

    db = isolated_db
    _setup(db)

    with aioresponses() as m:
        m.post(PRODUCT_URL, payload={"id": "p-new", "name": "Штекер тип C"})
        res = _run(ms_supply.create_product("Штекер тип C", unit="уп"))
        sent = next(iter(m.requests.values()))[0].kwargs["json"]

    assert res["ok"] and res["ms_id"] == "p-new" and res["existed"] is False
    assert sent == {"name": "Штекер тип C"}
    assert snapshot.get_product("p-new") == {"ms_id": "p-new", "name": "Штекер тип C", "unit": "уп"}
    # И сразу сопоставляется по имени, без второго захода в МойСклад.
    assert _run(ms_supply.create_product("штекер   тип c"))["existed"] is True


def test_created_product_never_overwrites_a_real_stock_row(isolated_db):
    """Снапшот — зеркало: перезапись строки обнулила бы настоящий остаток."""
    from services import snapshot

    db = isolated_db
    _setup(db)
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(
            db.q("INSERT INTO ms_stock (ms_id, name, folder_id, folder_name, unit, stock, "
                 "reserve) VALUES (?, ?, '', '', 'шт', 42, 0)"),
            ("p-1", "Кабель"),
        )
        conn.commit()

    _run(snapshot.remember_product("p-1", name="Кабель", unit="шт"))

    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(db.q("SELECT stock FROM ms_stock WHERE ms_id = ?"), ("p-1",))
        assert cur.fetchone()[0] == 42


def test_linked_position_is_supplied_even_without_a_snapshot_row(isolated_db, monkeypatch):
    """Товар завели минуту назад: в снапшоте его может не быть, а приход по нему
    обязан пройти — на то и привязка."""
    from services import containers, ms_supply

    db = isolated_db
    _setup(db)
    _ready_ms(monkeypatch)
    cid = _container(db)
    _run(containers.add_item(cid, name="Штекер тип C", expected_qty=5, arrived_qty=5,
                             ms_id="p-fresh"))
    _run(containers.mark_arrived(cid, user_id=2))
    _with_supplier(cid)

    with aioresponses() as m:
        m.post(SUPPLY_URL, payload={"id": "supply-1"})
        res = _run(ms_supply.sync_supply(cid))
        sent = next(iter(m.requests.values()))[0].kwargs["json"]

    assert res["ok"] and res["unmatched"] == []
    assert sent["positions"][0]["assortment"]["meta"]["href"].endswith("/product/p-fresh")
