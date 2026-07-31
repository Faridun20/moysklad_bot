"""
Ручки техники в WebApp: чтение (`/api/machines/list`, `/api/machines/card`).

Проверяем то, ради чего слой ручек вообще нужен: роли, срез себестоимости и
паспорта покупателя, отсутствие `tg_file_id` в ответе и коды ошибок (404/400/
409 — по ним фронт решает, обновлять карточку или подсвечивать поле).

БД настоящая (isolated_db), корутины через asyncio.run — pytest-asyncio в
проекте нет.
"""

import asyncio
import importlib

from fastapi.testclient import TestClient

import services.roles as roles


def _run(coro):
    return asyncio.run(coro)


def _machine(vin="JCB-001", **over):
    from services import machines

    payload = {
        "vin": vin,
        "name": "JCB 3CX",
        "created_by": 2,
        "creator_name": "Boss",
        "price_cents": 2_500_000,
        "cost_cents": 2_000_000,
    }
    payload.update(over)
    res = _run(machines.create_machine(**payload))
    assert res["ok"], res
    return res["machine_id"]


def _setup(db):
    """Роли: 1 — менеджер, 2 — босс, 3 — бухгалтер, 4 — кладовщик."""
    roles.invalidate_all_roles()
    db.set_role(1, "mgr", "Manager", "manager")
    db.set_role(2, "boss", "Boss", "boss")
    db.set_role(3, "acc", "Bookkeeper", "bookkeeper")
    db.set_role(4, "wh", "Keeper", "warehouse_keeper")


def _client(monkeypatch):
    import webapp.server as server

    importlib.reload(roles)
    monkeypatch.setattr(server, "verify_init_data", lambda s: {"id": int(s), "first_name": "U"})
    return TestClient(server.app)


def _post(client, path, uid, **body):
    return client.post(path, json={"initData": str(uid), **body})


# ─── Список ───────────────────────────────────────────────────────────────────


def test_list_returns_machines_and_counts(isolated_db, monkeypatch):
    db = isolated_db
    _setup(db)
    _machine("A-1")
    _machine("A-2", status="in_stock")

    r = _post(_client(monkeypatch), "/api/machines/list", 2)
    assert r.status_code == 200, r.text
    body = r.json()
    assert {m["vin"] for m in body["machines"]} == {"A1", "A2"}
    assert body["counts"]["in_transit"] == 1
    assert body["counts"]["in_stock"] == 1
    assert body["counts"]["all"] == 2
    assert body["can_manage"] is True


def test_list_counts_all_excludes_archive(isolated_db, monkeypatch):
    """`all` — это размер списка без фильтра, а он архив не показывает.
    Считать это правило ещё раз на фронте значит завести второй источник."""
    db = isolated_db
    _setup(db)
    _machine("A-1")
    _machine("A-2", status="archived")

    body = _post(_client(monkeypatch), "/api/machines/list", 2).json()
    assert body["counts"]["archived"] == 1
    assert body["counts"]["all"] == 1
    assert len(body["machines"]) == 1


def test_list_filters_by_status(isolated_db, monkeypatch):
    db = isolated_db
    _setup(db)
    _machine("A-1")
    _machine("A-2", status="in_stock")

    body = _post(_client(monkeypatch), "/api/machines/list", 2, status="in_stock").json()
    assert [m["vin"] for m in body["machines"]] == ["A2"]


def test_unknown_status_filter_is_rejected(isolated_db, monkeypatch):
    """Пустой список на опечатку выглядит как «машины пропали»."""
    db = isolated_db
    _setup(db)
    _machine("A-1")

    r = _post(_client(monkeypatch), "/api/machines/list", 2, status="in_stok")
    assert r.status_code == 400
    assert "in_stok" in r.json()["detail"]


def test_manager_sees_list_without_cost(isolated_db, monkeypatch):
    db = isolated_db
    _setup(db)
    _machine("A-1")

    body = _post(_client(monkeypatch), "/api/machines/list", 1).json()
    assert body["machines"][0]["price_cents"] == 2_500_000
    assert "cost_cents" not in body["machines"][0]
    assert body["can_manage"] is False
    assert body["can_see_cost"] is False


def test_bookkeeper_and_keeper_have_no_access(isolated_db, monkeypatch):
    """Техника — не их участок; роли перечислены явно, а не «все, кроме гостя»."""
    db = isolated_db
    _setup(db)
    _machine("A-1")
    client = _client(monkeypatch)

    for uid in (3, 4):
        assert _post(client, "/api/machines/list", uid).status_code == 403
        assert _post(client, "/api/machines/card", uid, machine_id=1).status_code == 403


def test_deactivated_user_is_rejected(isolated_db, monkeypatch):
    db = isolated_db
    _setup(db)
    _machine("A-1")
    assert _run(db.deactivate_user(1, 2))
    roles.invalidate_all_roles()

    assert _post(_client(monkeypatch), "/api/machines/list", 1).status_code == 403


def test_guest_is_rejected(isolated_db, monkeypatch):
    db = isolated_db
    _setup(db)
    _machine("A-1")

    assert _post(_client(monkeypatch), "/api/machines/list", 777).status_code == 403


# ─── Карточка ─────────────────────────────────────────────────────────────────


def test_card_returns_everything_the_screen_needs(isolated_db, monkeypatch):
    from services import machines

    db = isolated_db
    _setup(db)
    mid = _machine("A-1", hours=1500)
    assert _run(machines.add_hours(mid, 1600, user_id=1))["ok"]

    body = _post(_client(monkeypatch), "/api/machines/card", 2, machine_id=mid).json()
    assert body["machine"]["vin"] == "A1"
    assert body["machine"]["cost_cents"] == 2_000_000
    # Показания: стартовое при заведении + новое.
    assert [h["hours"] for h in body["hours"]] == [1600, 1500]
    assert body["deals"] == []
    assert body["photos"] == []


def test_card_next_statuses_come_from_server(isolated_db, monkeypatch):
    """Граф переходов — один на бот и WebApp, поэтому едет с сервера."""
    db = isolated_db
    _setup(db)
    mid = _machine("A-1")  # in_transit

    body = _post(_client(monkeypatch), "/api/machines/card", 2, machine_id=mid).json()
    assert [o["status"] for o in body["next_statuses"]] == ["in_stock"]
    assert body["next_statuses"][0]["label"]


def test_card_hides_cost_and_passport_from_manager(isolated_db, monkeypatch):
    from services import machines

    db = isolated_db
    _setup(db)
    mid = _machine("A-1")
    deal = _run(
        machines.create_deal(
            mid,
            kind="sale",
            price_cents=2_500_000,
            buyer_name="Иванов Пётр",
            buyer_passport="AB1234567",
            created_by=2,
        )
    )
    assert deal["ok"], deal
    client = _client(monkeypatch)

    boss = _post(client, "/api/machines/card", 2, machine_id=mid).json()
    assert boss["machine"]["cost_cents"] == 2_000_000
    assert boss["deals"][0]["buyer_passport"] == "AB1234567"

    mgr = _post(client, "/api/machines/card", 1, machine_id=mid).json()
    assert "cost_cents" not in mgr["machine"]
    # Паспорт — персональные данные: менеджеру видно, кому продали, но не по
    # какому документу.
    assert "buyer_passport" not in mgr["deals"][0]
    assert mgr["deals"][0]["buyer_name"] == "Иванов Пётр"


def test_card_never_leaks_telegram_file_ids(isolated_db, monkeypatch):
    """`tg_file_id` открывает файл через Bot API — наружу он не выходит."""
    from services import machines

    db = isolated_db
    _setup(db)
    mid = _machine("A-1")
    assert _run(
        machines.add_photo(
            mid, tg_file_id="AgACtoken", file_unique_id="uniq-1", uploaded_by=1, caption="перед"
        )
    )["ok"]

    body = _post(_client(monkeypatch), "/api/machines/card", 2, machine_id=mid).json()
    assert len(body["photos"]) == 1
    assert set(body["photos"][0]) == {"id", "caption", "sort_order", "uploaded_at"}
    assert "AgACtoken" not in str(body)


def test_card_404_for_missing_machine(isolated_db, monkeypatch):
    db = isolated_db
    _setup(db)

    r = _post(_client(monkeypatch), "/api/machines/card", 2, machine_id=999)
    assert r.status_code == 404


def test_card_rejects_garbage_id(isolated_db, monkeypatch):
    db = isolated_db
    _setup(db)
    client = _client(monkeypatch)

    assert _post(client, "/api/machines/card", 2, machine_id="abc").status_code == 400
    assert _post(client, "/api/machines/card", 2).status_code == 400


def test_list_rate_limited(isolated_db, monkeypatch):
    """Ручка дешёвая, но лимит общий для всех /api/* — проверяем, что он есть."""
    db = isolated_db
    _setup(db)
    _machine("A-1")
    client = _client(monkeypatch)

    codes = {_post(client, "/api/machines/list", 2).status_code for _ in range(35)}
    assert 429 in codes


# ─── Маппинг ошибок сервиса на HTTP ───────────────────────────────────────────


def test_service_errors_map_to_http_codes():
    """409 против 400 — единственное, по чему фронт отличает «обнови карточку»
    от «исправь поле»."""
    from webapp.server import _machine_response

    assert _machine_response({"ok": True, "machine_id": 1}).status_code == 200
    assert _machine_response({"ok": False, "error": "Машина не найдена"}).status_code == 404
    assert (
        _machine_response({"ok": False, "error": "Статус уже «Продана»", "current": "sold"}).status_code
        == 409
    )
    assert (
        _machine_response(
            {"ok": False, "error": "Показание меньше предыдущего (1500). Опечатка?",
             "needs_force": True, "previous": 1500}
        ).status_code
        == 409
    )
    assert (
        _machine_response(
            {"ok": False, "error": "Машина в статусе «Продана» — сделка невозможна"}
        ).status_code
        == 409
    )
    assert _machine_response({"ok": False, "error": "VIN обязателен"}).status_code == 400


def test_error_body_keeps_fields_the_form_needs():
    """`detail` читает общий api(), остальное — форма подтверждения."""
    import json

    from webapp.server import _machine_response

    res = _machine_response(
        {"ok": False, "error": "Показание меньше предыдущего (1500). Опечатка?",
         "needs_force": True, "previous": 1500}
    )
    body = json.loads(res.body)
    assert body["detail"].startswith("Показание меньше")
    assert body["needs_force"] is True
    assert body["previous"] == 1500
