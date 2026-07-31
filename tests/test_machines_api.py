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


# ─── Запись: заведение машины ─────────────────────────────────────────────────


def test_create_parses_human_money(isolated_db, monkeypatch):
    """Форма присылает «25 000», в БД должны лечь копейки."""
    from services import machines

    db = isolated_db
    _setup(db)

    r = _post(
        _client(monkeypatch), "/api/machines/create", 2,
        vin="jcb-77 88", name="JCB 3CX", price="25 000", cost="20000,50", year="2019",
    )
    assert r.status_code == 200, r.text
    m = _run(machines.get_machine(r.json()["machine_id"], role="boss"))
    assert m["vin"] == "JCB7788"  # нормализован
    assert m["price_cents"] == 2_500_000
    assert m["cost_cents"] == 2_000_050
    assert m["year"] == 2019


def test_manager_cannot_write_cost_it_cannot_see(isolated_db, monkeypatch):
    """Иначе роль режется только на чтении, а поле возвращается через форму."""
    from services import machines

    db = isolated_db
    _setup(db)

    r = _post(
        _client(monkeypatch), "/api/machines/create", 1,
        vin="A-1", name="Машина", price="1000", cost="900",
    )
    assert r.status_code == 200, r.text
    m = _run(machines.get_machine(r.json()["machine_id"], role="boss"))
    assert m["price_cents"] == 100_000
    assert m["cost_cents"] is None


def test_create_rejects_duplicate_vin(isolated_db, monkeypatch):
    db = isolated_db
    _setup(db)
    _machine("JCB-001")

    r = _post(_client(monkeypatch), "/api/machines/create", 2, vin="jcb 001", name="Дубль")
    assert r.status_code == 400
    assert "уже заведена" in r.json()["detail"]


def test_create_rejects_bad_money(isolated_db, monkeypatch):
    db = isolated_db
    _setup(db)

    r = _post(_client(monkeypatch), "/api/machines/create", 2, vin="A-1", name="X", price="дорого")
    assert r.status_code == 400
    assert "Цена" in r.json()["detail"]


def test_create_is_idempotent(isolated_db, monkeypatch):
    """Двойной тап по «Сохранить» не должен завести две карточки."""
    from services import machines

    db = isolated_db
    _setup(db)
    client = _client(monkeypatch)
    body = {"vin": "A-1", "name": "X", "idempotency_key": "same-key"}

    first = _post(client, "/api/machines/create", 2, **body)
    second = _post(client, "/api/machines/create", 2, **body)
    assert first.status_code == 200 and second.status_code == 200
    assert first.json()["machine_id"] == second.json()["machine_id"]
    assert len(_run(machines.list_machines(role="boss"))) == 1


# ─── Запись: правка, моточасы, статус ─────────────────────────────────────────


def test_update_is_boss_only(isolated_db, monkeypatch):
    db = isolated_db
    _setup(db)
    mid = _machine("A-1")

    r = _post(_client(monkeypatch), "/api/machines/update", 1, machine_id=mid,
              fields={"location": "Склад №2"})
    assert r.status_code == 403


def test_update_changes_fields(isolated_db, monkeypatch):
    from services import machines

    db = isolated_db
    _setup(db)
    mid = _machine("A-1")

    r = _post(_client(monkeypatch), "/api/machines/update", 2, machine_id=mid,
              fields={"location": "Склад №2", "price": "30 000"})
    assert r.status_code == 200, r.text
    m = _run(machines.get_machine(mid, role="boss"))
    assert m["location"] == "Склад №2"
    assert m["price_cents"] == 3_000_000


def test_update_rejects_vin(isolated_db, monkeypatch):
    """Смена серийника — не правка, а другая машина. Сервис его не пускает."""
    db = isolated_db
    _setup(db)
    mid = _machine("A-1")

    r = _post(_client(monkeypatch), "/api/machines/update", 2, machine_id=mid,
              fields={"vin": "B-2"})
    assert r.status_code == 400
    assert "vin" in r.json()["detail"]


def test_manager_can_record_hours(isolated_db, monkeypatch):
    """Показания снимают с площадки — это работа менеджера."""
    from services import machines

    db = isolated_db
    _setup(db)
    mid = _machine("A-1", hours=1500)

    r = _post(_client(monkeypatch), "/api/machines/hours", 1, machine_id=mid, hours=1600)
    assert r.status_code == 200, r.text
    assert _run(machines.get_machine(mid, role="boss"))["hours"] == 1600


def test_hours_rollback_asks_for_confirmation(isolated_db, monkeypatch):
    """409, а не 400: пользователю нечего исправлять в поле — ему надо
    подтвердить замену счётчика."""
    db = isolated_db
    _setup(db)
    mid = _machine("A-1", hours=15000)

    r = _post(_client(monkeypatch), "/api/machines/hours", 2, machine_id=mid, hours=1500)
    assert r.status_code == 409
    body = r.json()
    assert body["needs_force"] is True
    assert body["previous"] == 15000
    assert "Опечатка" in body["detail"]


def test_only_boss_may_force_hours_rollback(isolated_db, monkeypatch):
    """Иначе подтверждение «да, я уверен» обесценивает проверку от опечатки."""
    from services import machines

    db = isolated_db
    _setup(db)
    mid = _machine("A-1", hours=15000)
    client = _client(monkeypatch)

    assert _post(client, "/api/machines/hours", 1, machine_id=mid, hours=1500,
                 force=True).status_code == 403
    assert _post(client, "/api/machines/hours", 2, machine_id=mid, hours=1500,
                 force=True).status_code == 200
    assert _run(machines.get_machine(mid, role="boss"))["hours"] == 1500


def test_status_change_follows_the_graph(isolated_db, monkeypatch):
    from services import machines

    db = isolated_db
    _setup(db)
    mid = _machine("A-1")  # in_transit
    client = _client(monkeypatch)

    ok = _post(client, "/api/machines/status", 2, machine_id=mid,
               status="in_stock", expected="in_transit")
    assert ok.status_code == 200, ok.text
    assert _run(machines.get_machine(mid, role="boss"))["status"] == "in_stock"

    # Прыжок мимо графа: продажа требует цены и покупателя, поэтому идёт сделкой.
    bad = _post(client, "/api/machines/status", 2, machine_id=mid,
                status="sold", expected="in_stock")
    assert bad.status_code == 400
    assert "не предусмотрен" in bad.json()["detail"]


def test_status_change_detects_stale_card(isolated_db, monkeypatch):
    """Машину продали, пока карточка висела открытой: безусловный UPDATE затёр
    бы чужое решение, поэтому expected приходит с фронта."""
    from services import machines

    db = isolated_db
    _setup(db)
    mid = _machine("A-1")
    assert _run(machines.set_status(mid, "in_stock", user_id=2, expected="in_transit"))["ok"]

    r = _post(_client(monkeypatch), "/api/machines/status", 2, machine_id=mid,
              status="in_stock", expected="in_transit")
    assert r.status_code == 409
    assert r.json()["current"] == "in_stock"


def test_status_change_is_boss_only(isolated_db, monkeypatch):
    db = isolated_db
    _setup(db)
    mid = _machine("A-1")

    r = _post(_client(monkeypatch), "/api/machines/status", 1, machine_id=mid,
              status="in_stock", expected="in_transit")
    assert r.status_code == 403


# ─── Запись: сделки ───────────────────────────────────────────────────────────


def test_deal_requires_idempotency_key(isolated_db, monkeypatch):
    """Сделка — денежный факт, двойной тап по телефону обычное дело."""
    db = isolated_db
    _setup(db)
    mid = _machine("A-1")

    r = _post(_client(monkeypatch), "/api/machines/deal", 2, machine_id=mid,
              kind="sale", price="25 000", buyer_name="Иванов")
    assert r.status_code == 400
    assert "idempotency_key" in r.json()["detail"]


def test_repeated_deal_key_returns_the_same_deal(isolated_db, monkeypatch):
    from services import machines

    db = isolated_db
    _setup(db)
    mid = _machine("A-1")
    client = _client(monkeypatch)
    body = {"machine_id": mid, "kind": "sale", "price": "25 000",
            "buyer_name": "Иванов", "idempotency_key": "deal-1"}

    first = _post(client, "/api/machines/deal", 2, **body)
    second = _post(client, "/api/machines/deal", 2, **body)
    assert first.status_code == 200, first.text
    assert first.json()["deal_id"] == second.json()["deal_id"]
    assert len(_run(machines.list_deals(mid, role="boss"))) == 1


def test_deal_on_sold_machine_is_409(isolated_db, monkeypatch):
    db = isolated_db
    _setup(db)
    mid = _machine("A-1")
    client = _client(monkeypatch)
    base = {"machine_id": mid, "kind": "sale", "price": "1000", "buyer_name": "A"}

    assert _post(client, "/api/machines/deal", 2, idempotency_key="k1", **base).status_code == 200
    r = _post(client, "/api/machines/deal", 2, idempotency_key="k2", **base)
    assert r.status_code == 409
    assert "сделка невозможна" in r.json()["detail"]


def test_credit_deal_appears_in_open_list_and_closes(isolated_db, monkeypatch):
    from services import machines

    db = isolated_db
    _setup(db)
    mid = _machine("A-1")
    client = _client(monkeypatch)

    created = _post(client, "/api/machines/deal", 2, machine_id=mid, kind="credit",
                    price="50 000", buyer_name="Петров", due_date="2026-12-31",
                    buyer_passport="AB1", idempotency_key="c1")
    assert created.status_code == 200, created.text
    deal_id = created.json()["deal_id"]

    open_list = _post(client, "/api/machines/deals_open", 2).json()["deals"]
    assert [d["id"] for d in open_list] == [deal_id]
    assert open_list[0]["buyer_passport"] == "AB1"  # босс видит

    closed = _post(client, "/api/machines/deal_close", 2, deal_id=deal_id,
                   idempotency_key="cc1")
    assert closed.status_code == 200, closed.text
    assert _run(machines.get_machine(mid, role="boss"))["status"] == "sold"
    assert _post(client, "/api/machines/deals_open", 2).json()["deals"] == []


def test_closing_a_closed_deal_is_409(isolated_db, monkeypatch):
    db = isolated_db
    _setup(db)
    mid = _machine("A-1")
    client = _client(monkeypatch)
    created = _post(client, "/api/machines/deal", 2, machine_id=mid, kind="credit",
                    price="1000", buyer_name="A", due_date="2026-12-31",
                    idempotency_key="c1")
    deal_id = created.json()["deal_id"]
    assert _post(client, "/api/machines/deal_close", 2, deal_id=deal_id).status_code == 200

    r = _post(client, "/api/machines/deal_close", 2, deal_id=deal_id)
    assert r.status_code == 409


def test_deals_are_boss_only(isolated_db, monkeypatch):
    db = isolated_db
    _setup(db)
    mid = _machine("A-1")
    client = _client(monkeypatch)

    assert _post(client, "/api/machines/deal", 1, machine_id=mid, kind="sale",
                 price="1000", buyer_name="A", idempotency_key="k").status_code == 403
    assert _post(client, "/api/machines/deals_open", 1).status_code == 403
    assert _post(client, "/api/machines/deal_close", 1, deal_id=1).status_code == 403


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
