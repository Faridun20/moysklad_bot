"""
Мост «написал → купил»: привязка клиента к контрагенту справочника.

Ради этого моста всё и нужно: в переписке клиент — Telegram-аккаунт, в заказах —
контрагент, и общих полей у них нет. Телефон при этом берётся ТОЛЬКО с карточки
контрагента: Telegram не отдаёт боту номер собеседника ни в каком поле, и
«считать» его из переписки невозможно.

Справочник локальный (`services.counterparties`), БД настоящая — мокать нечего.
"""

import asyncio

from fastapi.testclient import TestClient

import services.roles as roles


def _run(coro):
    return asyncio.run(coro)


def _setup(db):
    roles.invalidate_all_roles()
    db.set_role(1, "mgr", "Manager", "manager")
    db.set_role(2, "boss", "Boss", "boss")
    db.set_role(5, "mgr2", "Other", "manager")


def _lead(uid=100, manager_id=1):
    from services import leads

    return _run(leads.record_message(tg_user_id=uid, manager_id=manager_id, inbound=True))["lead_id"]


def _agent(name, phone=""):
    """Завести контрагента и вернуть его id строкой — в таком виде он и живёт
    в `leads.agent_ms_id` / `orders.agent_id` (колонки TEXT)."""
    from services import counterparties as cp

    res = _run(cp.create(name, phone=phone or None))
    assert res["ok"], res
    return str(res["counterparty_id"])


def _client(monkeypatch):
    import importlib

    import webapp.server as server

    importlib.reload(roles)
    monkeypatch.setattr(server, "verify_init_data", lambda s: {"id": int(s), "first_name": "U"})
    return TestClient(server.app)


# ─── Поиск ────────────────────────────────────────────────────────────────────


def test_agent_is_found_by_phone_not_only_by_name(isolated_db):
    """Клиента помнят по номеру, а записан он как «ООО Бахор Савдо»."""
    from services import counterparties as cp

    _setup(isolated_db)
    aid = _agent("ООО Бахор Савдо", "+998 90 123-45-67")

    assert [str(a["id"]) for a in _run(cp.search("901234567"))] == [aid]
    assert [str(a["id"]) for a in _run(cp.search("Бахор"))] == [aid]


def test_phone_search_ignores_separators(isolated_db):
    """Номер лежит свободным текстом со скобками и дефисами; поиск по сырой
    строке не находил бы ничего."""
    from services import counterparties as cp

    _setup(isolated_db)
    aid = _agent("ИП Каримов", "(90) 765-43-21")

    assert [str(a["id"]) for a in _run(cp.search("907654321"))] == [aid]


def test_short_digits_do_not_turn_into_a_phone_search(isolated_db):
    """«ООО 21 век» — это название, а не номер: три цифры не повод искать по
    телефону и вываливать половину справочника."""
    from services import counterparties as cp

    _setup(isolated_db)
    aid = _agent("ООО 21 век", "901111111")
    _agent("Другой", "902222222")

    assert [str(a["id"]) for a in _run(cp.search("21 век"))] == [aid]


# ─── Заведение ────────────────────────────────────────────────────────────────


def test_existing_counterparty_is_reused(isolated_db):
    """Второй одноимённый контрагент развёл бы заказы одного клиента по двум
    карточкам, а склеить их потом нечем."""
    from services import counterparties as cp

    _setup(isolated_db)
    aid = _agent("ООО Бахор Савдо")

    res = _run(cp.create("  ооо   бахор  савдо "))
    assert res["ok"] and str(res["counterparty_id"]) == aid and res["existed"] is True


def test_created_counterparty_is_searchable_at_once(isolated_db):
    """Иначе того же клиента заведут второй раз — поиск его не покажет."""
    from services import counterparties as cp

    _setup(isolated_db)
    res = _run(cp.create("Азиз", phone="+998901234567"))
    assert res["ok"] and res["existed"] is False

    found = _run(cp.search("901234567"))
    assert [str(a["id"]) for a in found] == [str(res["counterparty_id"])]
    assert _run(cp.get(res["counterparty_id"]))["name"] == "Азиз"


def test_phone_is_optional(isolated_db):
    """Telegram номер собеседника не отдаёт: требовать телефон значит не дать
    завести контрагента вовсе."""
    from services import counterparties as cp

    _setup(isolated_db)
    res = _run(cp.create("Азиз"))
    assert res["ok"]
    assert _run(cp.get(res["counterparty_id"]))["phone"] is None


def test_empty_name_is_refused(isolated_db):
    from services import counterparties as cp

    _setup(isolated_db)
    assert _run(cp.create("   "))["ok"] is False


def test_unknown_id_is_not_found_not_an_error(isolated_db):
    """`orders.agent_id` — TEXT, и у старых строк там мог остаться uuid, который
    backfill не сматчил. Это «не найден», а не падение карточки."""
    from services import counterparties as cp

    _setup(isolated_db)
    assert _run(cp.get("6f1a-not-a-number")) is None
    assert _run(cp.get(None)) is None


# ─── Ручки ────────────────────────────────────────────────────────────────────


def test_card_shows_the_counterparty_phone(isolated_db, monkeypatch):
    """Телефон приезжает с карточки контрагента — из переписки его взять негде."""
    from services import leads

    _setup(isolated_db)
    lead_id = _lead()
    aid = _agent("ООО Бахор Савдо", "+998 90 123-45-67")
    _run(leads.link_agent(lead_id, aid, user_id=2))

    body = _client(monkeypatch).post(
        "/api/leads/card", json={"initData": "2", "lead_id": lead_id}
    ).json()
    assert body["lead"]["agent"]["name"] == "ООО Бахор Савдо"
    assert body["lead"]["agent"]["phone"] == "+998 90 123-45-67"


def test_create_agent_links_it_to_the_lead(isolated_db, monkeypatch):
    from services import leads

    _setup(isolated_db)
    lead_id = _lead()
    res = _client(monkeypatch).post("/api/leads/create_agent", json={
        "initData": "2", "lead_id": lead_id, "name": "Азиз",
    })
    assert res.status_code == 200, res.text
    linked = _run(leads.get_lead(lead_id))["agent_ms_id"]
    assert linked and _run(leads.get_lead(lead_id))["agent"]["name"] == "Азиз"


def test_manager_cannot_touch_someone_elses_lead(isolated_db, monkeypatch):
    """Дыра, которой не было видно: ручку привязки фронт до сих пор не звал, и
    проверки владения в ней не было вовсе."""
    _setup(isolated_db)
    lead_id = _lead(manager_id=1)
    aid = _agent("Чужой")
    client = _client(monkeypatch)

    res = client.post("/api/leads/link", json={
        "initData": "5", "lead_id": lead_id, "counterparty_id": aid,
    })
    assert res.status_code == 403, res.text

    res = client.post("/api/leads/create_agent", json={
        "initData": "5", "lead_id": lead_id, "name": "Чужой",
    })
    assert res.status_code == 403, res.text


def test_agents_search_endpoint_answers_managers(isolated_db, monkeypatch):
    _setup(isolated_db)
    aid = _agent("ООО Бахор Савдо", "901234567")
    body = _client(monkeypatch).post(
        "/api/leads/agents", json={"initData": "1", "search": "901234567"}
    ).json()
    assert [str(a["id"]) for a in body["agents"]] == [aid]
