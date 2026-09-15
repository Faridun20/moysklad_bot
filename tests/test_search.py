"""
Тесты глобального поиска (PR A): search_orders / search_payments + /api/search.

Покрываем:
  * LIKE-матч по тексту, id-матч для числового query
  * user_id-скоуп (менеджер видит только свои)
  * экранирование LIKE-метасимволов (%, _)
  * soft-deleted заказы не попадают в выдачу
  * endpoint authz + менеджер не получает чужое
"""

import importlib

import pytest
from fastapi.testclient import TestClient


def _mk_order(db, user_id, agent_name="Иванов", full_name="Менеджер", comment=""):
    oid = db.create_order(user_id, full_name, comment)
    db.update_order_agent(oid, f"agent-{oid}", agent_name)
    return oid


# ─── Юнит: search_orders ─────────────────────────────────────────────────────


def test_search_orders_by_agent_name(isolated_db):
    db = isolated_db
    db.set_role(100, "m", "Менеджер", "manager")
    oid = _mk_order(db, 100, agent_name="Иванов И.И.")
    _mk_order(db, 100, agent_name="Петров П.П.")
    res = db.search_orders("иванов")
    ids = {o["id"] for o in res}
    assert oid in ids
    assert len(res) == 1


def test_search_orders_by_id_numeric_query(isolated_db):
    db = isolated_db
    db.set_role(100, "m", "M", "manager")
    oid = _mk_order(db, 100, agent_name="Сидоров")
    res = db.search_orders(str(oid))
    assert any(o["id"] == oid for o in res)


def test_search_orders_user_scope(isolated_db):
    """user_id задан → только заказы этого юзера."""
    db = isolated_db
    db.set_role(100, "m1", "M1", "manager")
    db.set_role(200, "m2", "M2", "manager")
    mine = _mk_order(db, 100, agent_name="ОбщийКлиент")
    other = _mk_order(db, 200, agent_name="ОбщийКлиент")
    # Скоуп менеджера 100 → только его
    res = db.search_orders("общийклиент", user_id=100)
    ids = {o["id"] for o in res}
    assert mine in ids
    assert other not in ids
    # Без скоупа (boss) → оба
    res_all = db.search_orders("общийклиент", user_id=None)
    assert {mine, other} <= {o["id"] for o in res_all}


def test_search_orders_escapes_like_metachars(isolated_db):
    """% и _ в query не должны работать как wildcard."""
    db = isolated_db
    db.set_role(100, "m", "M", "manager")
    _mk_order(db, 100, agent_name="Скидка 50%")
    _mk_order(db, 100, agent_name="Другой клиент")
    # Поиск "50%" должен найти буквальный "50%", а не "5" + всё
    res = db.search_orders("50%")
    assert len(res) == 1
    assert "50%" in res[0]["agent_name"]
    # Поиск "%" в одиночку — буквальный %, не "всё подряд"
    res2 = db.search_orders("%")
    assert all("%" in o["agent_name"] for o in res2)


def test_search_orders_empty_query_returns_empty(isolated_db):
    db = isolated_db
    assert db.search_orders("") == []
    assert db.search_orders("   ") == []


# ─── Юнит: search_payments ───────────────────────────────────────────────────


def test_search_payments_by_comment_and_scope(isolated_db):
    db = isolated_db
    db.set_role(100, "m1", "Першин", "manager")
    db.set_role(200, "m2", "Второв", "manager")
    p1 = db.add_payment(100, "@m1", "Першин", 100.0, "USD", "аренда офиса")
    p2 = db.add_payment(200, "@m2", "Второв", 200.0, "USD", "аренда склада")
    # По комменту "аренда" — оба без скоупа
    res = db.search_payments("аренда")
    assert {p1, p2} <= {p["id"] for p in res}
    # Скоуп менеджера 100 → только p1
    res_scoped = db.search_payments("аренда", user_id=100)
    ids = {p["id"] for p in res_scoped}
    assert p1 in ids and p2 not in ids


def test_search_payments_by_full_name(isolated_db):
    db = isolated_db
    db.set_role(100, "m", "Уникумов", "manager")
    pid = db.add_payment(100, "@m", "Уникумов", 50.0, "USD", "x")
    res = db.search_payments("уникумов")
    assert any(p["id"] == pid for p in res)


# ─── E2E endpoint ────────────────────────────────────────────────────────────


@pytest.fixture
def client_env(isolated_db, monkeypatch):
    import services.roles as roles
    import webapp.server as server

    importlib.reload(roles)
    db = isolated_db
    boss_id, mgr_id, other_mgr = 100, 200, 300
    db.set_role(boss_id, "boss", "Boss", "boss")
    db.set_role(mgr_id, "mgr", "Manager", "manager")
    db.set_role(other_mgr, "mgr2", "Manager2", "manager")

    monkeypatch.setattr(
        server,
        "verify_init_data",
        lambda init_data: {"id": int(init_data), "first_name": "U", "username": "u"},
    )
    return TestClient(server.app), db, {"boss": boss_id, "mgr": mgr_id, "other": other_mgr}


def test_search_endpoint_empty_query(client_env):
    client, _db, ids = client_env
    resp = client.post("/api/search", json={"initData": str(ids["boss"]), "query": ""})
    assert resp.status_code == 200
    data = resp.json()
    assert data["orders"] == [] and data["payments"] == [] and data["agents"] == []


def test_search_endpoint_manager_sees_only_own(client_env):
    client, db, ids = client_env
    mine = _mk_order(db, ids["mgr"], agent_name="ОбщийАгент")
    other = _mk_order(db, ids["other"], agent_name="ОбщийАгент")
    # Менеджер видит только свой
    resp = client.post("/api/search", json={"initData": str(ids["mgr"]), "query": "общийагент"})
    assert resp.status_code == 200
    found = {o["id"] for o in resp.json()["orders"]}
    assert mine in found and other not in found
    # Boss видит оба
    resp2 = client.post("/api/search", json={"initData": str(ids["boss"]), "query": "общийагент"})
    found2 = {o["id"] for o in resp2.json()["orders"]}
    assert {mine, other} <= found2


def test_search_endpoint_forbidden_for_guest(client_env):
    client, db, ids = client_env
    db.set_role(999, "g", "Guest", "guest")
    resp = client.post("/api/search", json={"initData": "999", "query": "что-то"})
    assert resp.status_code == 403


def test_search_endpoint_forbidden_for_bookkeeper(client_env):
    """Бухгалтер видит каталог/склад своими ручками, но не через /api/search —
    он и раньше не входил в allowed_roles (не новая дыра из-за A2)."""
    client, db, ids = client_env
    db.set_role(998, "acc", "Acc", "bookkeeper")
    resp = client.post("/api/search", json={"initData": "998", "query": "что-то"})
    assert resp.status_code == 403


# ─── A2: новые группы результатов ───────────────────────────────────────────


def _run(coro):
    import asyncio
    return asyncio.run(coro)


def test_search_endpoint_finds_product(client_env):
    from services import container_receipt

    client, _db, ids = client_env
    _run(container_receipt.create_product("Кабель ВВГ 3х2.5", unit="м"))
    resp = client.post("/api/search", json={"initData": str(ids["boss"]), "query": "кабель ввг"})
    assert resp.status_code == 200
    names = {p["name"] for p in resp.json()["products"]}
    assert "Кабель ВВГ 3х2.5" in names


def test_search_endpoint_finds_container_by_number(client_env):
    from services import containers

    client, _db, ids = client_env
    _run(containers.create_container(number="MSCU1234567", created_by=ids["boss"]))
    resp = client.post("/api/search", json={"initData": str(ids["boss"]), "query": "MSCU1234567"})
    assert resp.status_code == 200
    numbers = {c["number"] for c in resp.json()["containers"]}
    assert "MSCU1234567" in numbers


def test_search_endpoint_finds_machine_by_vin_and_name(client_env):
    from services import machines

    client, _db, ids = client_env
    res = _run(machines.create_machine(
        vin="JCB3CX0012345", name="Экскаватор-погрузчик JCB", created_by=ids["boss"],
    ))
    assert res["ok"]
    resp_vin = client.post("/api/search", json={"initData": str(ids["boss"]), "query": "JCB3CX0012345"})
    assert {m["vin"] for m in resp_vin.json()["machines"]} == {"JCB3CX0012345"}
    resp_name = client.post("/api/search", json={"initData": str(ids["boss"]), "query": "погрузчик"})
    assert res["machine_id"] in {m["id"] for m in resp_name.json()["machines"]}


def test_search_endpoint_finds_lead_by_name_and_scopes_by_manager(client_env):
    from services import leads

    client, db, ids = client_env
    _run(leads.record_message(
        tg_user_id=555, manager_id=ids["mgr"], inbound=True,
        username="aziz01", display_name="Азизбек",
    ))
    _run(leads.record_message(
        tg_user_id=556, manager_id=ids["other"], inbound=True,
        username="azizjon", display_name="Азизжон",
    ))
    # У обоих есть "Ази" в имени — общий кусок запроса.
    resp_mgr = client.post("/api/search", json={"initData": str(ids["mgr"]), "query": "ази"})
    names_mgr = {l["display_name"] for l in resp_mgr.json()["leads"]}
    assert names_mgr == {"Азизбек"}  # только свой лид
    resp_boss = client.post("/api/search", json={"initData": str(ids["boss"]), "query": "ази"})
    names_boss = {l["display_name"] for l in resp_boss.json()["leads"]}
    assert names_boss == {"Азизбек", "Азизжон"}  # начальству — оба


def test_search_endpoint_finds_lead_by_phone(client_env):
    """Телефон живёт в lead_calls, не в leads — поиск обязан найти лид через
    привязанный звонок."""
    from services import lead_calls, leads

    client, _db, ids = client_env
    res = _run(leads.record_message(
        tg_user_id=777, manager_id=ids["mgr"], inbound=True,
        username="bek", display_name="Бекзод",
    ))
    lead_id = res["lead_id"]
    _run(lead_calls.add_call(
        manager_id=ids["mgr"], phone="+998 90 123-45-67", lead_id=lead_id,
    ))
    resp = client.post("/api/search", json={"initData": str(ids["mgr"]), "query": "901234567"})
    assert lead_id in {l["id"] for l in resp.json()["leads"]}
    # Чужому менеджеру звонок не виден.
    resp_other = client.post("/api/search", json={"initData": str(ids["other"]), "query": "901234567"})
    assert lead_id not in {l["id"] for l in resp_other.json()["leads"]}


def test_search_endpoint_status_labels_come_from_server(client_env):
    """Подписи статусов контейнера/техники/лида — те же словари, что и у
    их собственных ручек (containers/machines/leads.STATUS_LABELS), не
    захардкожены во фронте."""
    from services import containers, leads, machines

    client, _db, ids = client_env
    resp = client.post("/api/search", json={"initData": str(ids["boss"]), "query": "x"})
    data = resp.json()
    assert data["container_status_labels"] == containers.STATUS_LABELS
    assert data["machine_status_labels"] == machines.STATUS_LABELS
    assert data["lead_status_labels"] == leads.STATUS_LABELS
