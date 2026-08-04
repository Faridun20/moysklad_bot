"""
Мост «написал → купил»: привязка клиента к контрагенту МойСклад.

Ради этого моста всё и нужно: в переписке клиент — Telegram-аккаунт, в заказах —
контрагент, и общих полей у них нет. Телефон при этом берётся ТОЛЬКО с карточки
контрагента: Telegram не отдаёт боту номер собеседника ни в каком поле, и
«считать» его из переписки невозможно.

Мок на границе с МойСклад (aioresponses), БД настоящая.
"""

import asyncio

from aioresponses import aioresponses
from fastapi.testclient import TestClient

import services.roles as roles
from services.moysklad import MS_BASE

AGENT_URL = f"{MS_BASE}/entity/counterparty"


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


def _agent(db, ms_id, name, phone=""):
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(
            db.q("INSERT INTO ms_counterparties (ms_id, name, phone, href, balance_cents) "
                 "VALUES (?, ?, ?, '', 0)"),
            (ms_id, name, phone),
        )
        conn.commit()


def _client(monkeypatch):
    import importlib

    import webapp.server as server

    importlib.reload(roles)
    monkeypatch.setattr(server, "verify_init_data", lambda s: {"id": int(s), "first_name": "U"})
    return TestClient(server.app)


# ─── Поиск ────────────────────────────────────────────────────────────────────


def test_agent_is_found_by_phone_not_only_by_name(isolated_db):
    """Клиента помнят по номеру, а в МойСклад он записан как «ООО Бахор Савдо»."""
    from services import snapshot

    db = isolated_db
    _setup(db)
    _agent(db, "a-1", "ООО Бахор Савдо", "+998 90 123-45-67")

    assert [a["ms_id"] for a in snapshot.get_counterparties("901234567")] == ["a-1"]
    assert [a["ms_id"] for a in snapshot.get_counterparties("Бахор")] == ["a-1"]


def test_phone_search_ignores_separators(isolated_db):
    """В МойСклад номер лежит свободным текстом со скобками и дефисами; поиск по
    сырой строке не находил бы ничего."""
    from services import snapshot

    db = isolated_db
    _setup(db)
    _agent(db, "a-1", "ИП Каримов", "(90) 765-43-21")

    assert [a["ms_id"] for a in snapshot.get_counterparties("907654321")] == ["a-1"]


def test_short_digits_do_not_turn_into_a_phone_search(isolated_db):
    """«ООО 21 век» — это название, а не номер: три цифры не повод искать по
    телефону и вываливать половину справочника."""
    from services import snapshot

    db = isolated_db
    _setup(db)
    _agent(db, "a-1", "ООО 21 век", "901111111")
    _agent(db, "a-2", "Другой", "902222222")

    assert [a["ms_id"] for a in snapshot.get_counterparties("21 век")] == ["a-1"]


# ─── Заведение ────────────────────────────────────────────────────────────────


def test_existing_counterparty_is_reused_without_a_request(isolated_db):
    """Второй одноимённый контрагент развёл бы заказы одного клиента по двум
    карточкам, а склеить их потом нечем."""
    from services import ms_counterparty

    db = isolated_db
    _setup(db)
    _agent(db, "a-1", "ООО Бахор Савдо")

    with aioresponses() as m:
        res = _run(ms_counterparty.create_counterparty("  ооо   бахор  савдо "))
        assert m.requests == {}, "существующий контрагент не должен стоить запроса в МС"
    assert res["ok"] and res["ms_id"] == "a-1" and res["existed"] is True


def test_created_counterparty_lands_in_the_snapshot(isolated_db):
    """Иначе до ночной синхронизации его не найдёт поиск, и того же клиента
    заведут второй раз."""
    from services import ms_counterparty, snapshot

    db = isolated_db
    _setup(db)
    with aioresponses() as m:
        m.post(AGENT_URL, payload={"id": "a-new", "name": "Азиз"})
        res = _run(ms_counterparty.create_counterparty("Азиз", phone="+998901234567"))
        sent = next(iter(m.requests.values()))[0].kwargs["json"]

    assert res["ok"] and res["existed"] is False
    assert sent == {"name": "Азиз", "phone": "+998901234567"}
    assert snapshot.get_counterparty("a-new")["name"] == "Азиз"
    # И сразу находится поиском по номеру — второй раз его не заведут.
    assert [a["ms_id"] for a in snapshot.get_counterparties("901234567")] == ["a-new"]


def test_phone_is_optional(isolated_db):
    """Telegram номер собеседника не отдаёт: требовать телефон значит не дать
    завести контрагента вовсе."""
    from services import ms_counterparty

    db = isolated_db
    _setup(db)
    with aioresponses() as m:
        m.post(AGENT_URL, payload={"id": "a-new", "name": "Азиз"})
        res = _run(ms_counterparty.create_counterparty("Азиз"))
        sent = next(iter(m.requests.values()))[0].kwargs["json"]
    assert res["ok"]
    assert "phone" not in sent


def test_empty_name_is_refused(isolated_db):
    from services import ms_counterparty

    db = isolated_db
    _setup(db)
    assert _run(ms_counterparty.create_counterparty("   "))["ok"] is False


def test_existing_snapshot_row_is_never_overwritten(isolated_db):
    """Перезапись стёрла бы баланс взаиморасчётов."""
    from services import snapshot

    db = isolated_db
    _setup(db)
    _agent(db, "a-1", "ООО Бахор", "901111111")
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(db.q("UPDATE ms_counterparties SET balance_cents = -50000 WHERE ms_id = ?"),
                    ("a-1",))
        conn.commit()

    _run(snapshot.remember_counterparty("a-1", name="Другое имя", phone="909999999"))
    row = snapshot.get_counterparty("a-1")
    assert row["name"] == "ООО Бахор"
    assert row["phone"] == "901111111"


# ─── Ручки ────────────────────────────────────────────────────────────────────


def test_card_shows_the_counterparty_phone(isolated_db, monkeypatch):
    """Телефон приезжает с карточки контрагента — из переписки его взять негде."""
    from services import leads

    db = isolated_db
    _setup(db)
    lead_id = _lead()
    _agent(db, "a-1", "ООО Бахор Савдо", "+998 90 123-45-67")
    _run(leads.link_agent(lead_id, "a-1", user_id=2))

    body = _client(monkeypatch).post(
        "/api/leads/card", json={"initData": "2", "lead_id": lead_id}
    ).json()
    assert body["lead"]["agent"]["name"] == "ООО Бахор Савдо"
    assert body["lead"]["agent"]["phone"] == "+998 90 123-45-67"


def test_create_agent_links_it_to_the_lead(isolated_db, monkeypatch):
    from services import leads

    db = isolated_db
    _setup(db)
    lead_id = _lead()
    client = _client(monkeypatch)
    with aioresponses() as m:
        m.post(AGENT_URL, payload={"id": "a-new", "name": "Азиз"})
        res = client.post("/api/leads/create_agent", json={
            "initData": "2", "lead_id": lead_id, "name": "Азиз",
        })
    assert res.status_code == 200, res.text
    assert _run(leads.get_lead(lead_id))["agent_ms_id"] == "a-new"


def test_manager_cannot_touch_someone_elses_lead(isolated_db, monkeypatch):
    """Дыра, которой не было видно: ручку привязки фронт до сих пор не звал, и
    проверки владения в ней не было вовсе."""
    db = isolated_db
    _setup(db)
    lead_id = _lead(manager_id=1)
    _agent(db, "a-1", "Чужой")
    client = _client(monkeypatch)

    res = client.post("/api/leads/link", json={
        "initData": "5", "lead_id": lead_id, "agent_ms_id": "a-1",
    })
    assert res.status_code == 403, res.text

    res = client.post("/api/leads/create_agent", json={
        "initData": "5", "lead_id": lead_id, "name": "Чужой",
    })
    assert res.status_code == 403, res.text


def test_agents_search_endpoint_answers_managers(isolated_db, monkeypatch):
    db = isolated_db
    _setup(db)
    _agent(db, "a-1", "ООО Бахор Савдо", "901234567")
    body = _client(monkeypatch).post(
        "/api/leads/agents", json={"initData": "1", "search": "901234567"}
    ).json()
    assert [a["ms_id"] for a in body["agents"]] == ["a-1"]
