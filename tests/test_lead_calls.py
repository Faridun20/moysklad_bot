"""
Журнал звонков и причины отказа.

Главное, что проверяем: звонок — самостоятельный факт, а не половина лида, и он
НЕ подмешивается в общий счётчик обращений. Подмешать значит сломать сравнимость
с прошлыми месяцами ровно в день выката: «конверсия упала» окажется неправдой —
упадёт определение знаменателя, а не бизнес.

БД настоящая (isolated_db), корутины через asyncio.run.
"""

import asyncio
from datetime import timedelta

from fastapi.testclient import TestClient

import services.roles as roles
from utils.helpers import local_now


def _run(coro):
    return asyncio.run(coro)


def _setup(db):
    roles.invalidate_all_roles()
    db.set_role(1, "mgr", "Manager", "manager")
    db.set_role(2, "boss", "Boss", "boss")
    db.set_role(3, "acc", "Bookkeeper", "bookkeeper")


def _today():
    return local_now().strftime("%Y-%m-%d")


def _ago(**kw):
    return (local_now().replace(tzinfo=None) - timedelta(**kw)).strftime("%Y-%m-%d %H:%M:%S")


def _lead(uid=100, manager_id=1):
    from services import leads

    res = _run(leads.record_message(tg_user_id=uid, manager_id=manager_id, inbound=True))
    return res["lead_id"]


def _client(monkeypatch):
    import importlib

    import webapp.server as server

    importlib.reload(roles)
    monkeypatch.setattr(server, "verify_init_data", lambda s: {"id": int(s), "first_name": "U"})
    return TestClient(server.app)


# ─── Телефон ──────────────────────────────────────────────────────────────────


def test_phone_key_survives_every_way_people_write_a_number():
    """Один номер приходит в трёх формах; без общего ключа поиск не найдёт
    ничего, а связать звонок с контрагентом позже станет нечем."""
    from services.lead_calls import phone_key

    same = ["+998 90 123-45-67", "998901234567", "901234567", "8 90 123 45 67"]
    assert len({phone_key(v) for v in same}) == 1
    assert phone_key("+998 90 123-45-67") == "901234567"


def test_phone_key_is_empty_when_there_is_no_number():
    from services.lead_calls import phone_key

    assert phone_key(None) == ""
    assert phone_key("не помню") == ""


# ─── Звонок как самостоятельный факт ──────────────────────────────────────────


def test_call_without_telegram_is_recorded(isolated_db):
    """У позвонившего нет tg_user_id, а он в leads NOT NULL UNIQUE — до этой
    таблицы такой клиент в системе не существовал вовсе."""
    from services import lead_calls

    db = isolated_db
    _setup(db)
    res = _run(lead_calls.add_call(
        manager_id=1, phone="+998901234567", display_name="Азиз", interest="кабель"
    ))
    assert res["ok"]
    calls = _run(lead_calls.list_calls(unlinked=True))
    assert len(calls) == 1
    assert calls[0]["lead_id"] is None
    assert calls[0]["phone_key"] == "901234567"


def test_call_needs_nothing_but_the_manager(isolated_db):
    """Половину звонков заносят постфактум, когда номера уже нет под рукой.
    Обязательное поле означало бы, что звонки перестанут записывать."""
    from services import lead_calls

    db = isolated_db
    _setup(db)
    assert _run(lead_calls.add_call(manager_id=1))["ok"] is True


def test_call_rejects_unknown_direction_and_source(isolated_db):
    from services import lead_calls

    db = isolated_db
    _setup(db)
    assert _run(lead_calls.add_call(manager_id=1, direction="вбок"))["ok"] is False
    assert _run(lead_calls.add_call(manager_id=1, source="сарафан"))["ok"] is False


def test_call_attached_to_a_lead_writes_an_event(isolated_db):
    from services import lead_calls, leads

    db = isolated_db
    _setup(db)
    lead_id = _lead()
    assert _run(lead_calls.add_call(manager_id=1, lead_id=lead_id, phone="901234567"))["ok"]

    card = _run(leads.get_lead(lead_id))
    assert [c["phone"] for c in card["calls"]] == ["901234567"]
    assert "call" in {e["kind"] for e in card["events"]}


def test_call_to_a_missing_lead_is_refused(isolated_db):
    from services import lead_calls

    db = isolated_db
    _setup(db)
    assert _run(lead_calls.add_call(manager_id=1, lead_id=999))["ok"] is False


# ─── Связка с перепиской ──────────────────────────────────────────────────────


def test_linking_moves_the_call_out_of_the_callback_list(isolated_db):
    """Звонок связали с перепиской — из списка «кому перезвонить» он уходит."""
    from services import lead_calls, leads

    db = isolated_db
    _setup(db)
    call_id = _run(lead_calls.add_call(manager_id=1, phone="901234567"))["call_id"]
    lead_id = _lead()

    assert _run(lead_calls.link_call(call_id, lead_id, user_id=1))["ok"]
    assert _run(lead_calls.list_calls(unlinked=True)) == []
    assert len(_run(lead_calls.list_calls(lead_id=lead_id))) == 1
    assert "call_linked" in {e["kind"] for e in _run(leads.get_lead(lead_id))["events"]}


def test_linking_refuses_unknown_ids(isolated_db):
    from services import lead_calls

    db = isolated_db
    _setup(db)
    call_id = _run(lead_calls.add_call(manager_id=1))["call_id"]
    assert _run(lead_calls.link_call(call_id, 999, user_id=1))["ok"] is False
    assert _run(lead_calls.link_call(999, _lead(), user_id=1))["ok"] is False


def test_wrong_record_can_be_deleted(isolated_db):
    """Звонок — заметка менеджера, а не денежный факт: запрещать правку значит
    копить мусор в списке «перезвонить»."""
    from services import lead_calls

    db = isolated_db
    _setup(db)
    call_id = _run(lead_calls.add_call(manager_id=1))["call_id"]
    assert _run(lead_calls.delete_call(call_id))["ok"] is True
    assert _run(lead_calls.delete_call(call_id))["ok"] is False


# ─── Воронка не должна поехать ────────────────────────────────────────────────


def test_calls_never_leak_into_contacted(isolated_db):
    """Подмешать звонки в общий счётчик значит сломать сравнимость с прошлыми
    месяцами в день выката."""
    from services import lead_calls, leads

    db = isolated_db
    _setup(db)
    _lead(uid=100)
    _run(lead_calls.add_call(manager_id=1, phone="901234567"))
    _run(lead_calls.add_call(manager_id=1, phone="907654321"))

    f = _run(leads.funnel(_today(), _today()))
    assert f["contacted"] == 1, "звонки в «обратились» попадать не должны"
    assert f["calls_unlinked"] == 2, "но и молча пропасть тоже не должны"


def test_linked_call_leaves_the_unlinked_counter(isolated_db):
    from services import lead_calls, leads

    db = isolated_db
    _setup(db)
    lead_id = _lead()
    call_id = _run(lead_calls.add_call(manager_id=1))["call_id"]
    assert _run(leads.funnel(_today(), _today()))["calls_unlinked"] == 1
    _run(lead_calls.link_call(call_id, lead_id, user_id=1))
    assert _run(leads.funnel(_today(), _today()))["calls_unlinked"] == 0


def test_sources_breakdown_counts_only_what_was_filled(isolated_db):
    from services import lead_calls, leads

    db = isolated_db
    _setup(db)
    _run(lead_calls.add_call(manager_id=1, source="channel"))
    _run(lead_calls.add_call(manager_id=1, source="channel"))
    _run(lead_calls.add_call(manager_id=1, source="referral"))
    _run(lead_calls.add_call(manager_id=1))  # источник не указали — не считаем

    sources = _run(leads.funnel(_today(), _today()))["sources"]
    assert [(s["source"], s["count"]) for s in sources] == [("channel", 2), ("referral", 1)]
    assert sources[0]["label"] == "Наш канал"


# ─── Причина отказа ───────────────────────────────────────────────────────────


def test_lost_reason_is_optional(isolated_db):
    """Кнопку «Не купил» и так нажимают редко; обязательное поле привело бы к
    тому, что её перестанут нажимать вовсе."""
    from services import leads

    db = isolated_db
    _setup(db)
    lead_id = _lead()
    assert _run(leads.set_status(lead_id, "lost", user_id=2))["ok"] is True
    assert _run(leads.get_lead(lead_id))["lost"] is None


def test_lost_reason_is_stored_and_labelled(isolated_db):
    from services import leads

    db = isolated_db
    _setup(db)
    lead_id = _lead()
    _run(leads.set_status(lead_id, "lost", user_id=2, reason="no_stock", note="ждал неделю"))

    card = _run(leads.get_lead(lead_id))
    assert card["lost"]["reason"] == "no_stock"
    assert card["lost"]["label"] == "Нет в наличии"
    assert card["lost"]["note"] == "ждал неделю"


def test_reason_can_be_added_after_the_status(isolated_db):
    """Отметили «не купил», через час вспомнили почему — это исправление, а не
    второй отказ."""
    from services import leads

    db = isolated_db
    _setup(db)
    lead_id = _lead()
    _run(leads.set_status(lead_id, "lost", user_id=2))
    res = _run(leads.set_status(lead_id, "lost", user_id=2, reason="price"))
    assert res["ok"] and res["changed"] is False
    assert _run(leads.get_lead(lead_id))["lost"]["reason"] == "price"


def test_reason_is_replaced_not_appended(isolated_db):
    from services import leads

    db = isolated_db
    _setup(db)
    lead_id = _lead()
    _run(leads.set_status(lead_id, "lost", user_id=2, reason="price"))
    _run(leads.set_status(lead_id, "lost", user_id=2, reason="competitor"))
    assert _run(leads.get_lead(lead_id))["lost"]["reason"] == "competitor"


def test_reason_disappears_when_the_lead_comes_back(isolated_db):
    """Вернули в работу или всё-таки купил — причина отказа больше не факт."""
    from services import leads

    db = isolated_db
    _setup(db)
    lead_id = _lead()
    _run(leads.set_status(lead_id, "lost", user_id=2, reason="price"))
    _run(leads.set_status(lead_id, "won", user_id=2))
    assert _run(leads.get_lead(lead_id))["lost"] is None


def test_unknown_reason_is_rejected(isolated_db):
    from services import leads

    db = isolated_db
    _setup(db)
    lead_id = _lead()
    res = _run(leads.set_status(lead_id, "lost", user_id=2, reason="не понравился"))
    assert res["ok"] is False


def test_lost_reasons_report_is_sorted_by_weight(isolated_db):
    from services import leads

    db = isolated_db
    _setup(db)
    for uid, reason in ((101, "no_stock"), (102, "no_stock"), (103, "price")):
        _run(leads.set_status(_lead(uid=uid), "lost", user_id=2, reason=reason))

    report = _run(leads.funnel(_today(), _today()))["lost_reasons"]
    assert [(r["reason"], r["count"]) for r in report] == [("no_stock", 2), ("price", 1)]
    assert report[0]["label"] == "Нет в наличии"


# ─── Ручки ────────────────────────────────────────────────────────────────────


def test_endpoints_are_closed_to_roles_without_leads(isolated_db, monkeypatch):
    db = isolated_db
    _setup(db)
    client = _client(monkeypatch)
    for path in ("/api/leads/calls", "/api/leads/call_add", "/api/leads/call_delete"):
        res = client.post(path, json={"initData": "3", "call_id": 1})
        assert res.status_code == 403, (path, res.text)


def test_call_add_and_list_round_trip(isolated_db, monkeypatch):
    db = isolated_db
    _setup(db)
    client = _client(monkeypatch)
    res = client.post("/api/leads/call_add", json={
        "initData": "1", "phone": "+998 90 123-45-67", "display_name": "Азиз",
        "direction": "in", "source": "channel", "interest": "кабель",
    })
    assert res.status_code == 200, res.text

    body = client.post("/api/leads/calls", json={"initData": "1"}).json()
    assert len(body["calls"]) == 1
    assert body["source_labels"]["channel"] == "Наш канал"

    listed = client.post("/api/leads/list", json={"initData": "2"}).json()
    assert len(listed["unlinked_calls"]) == 1


def test_status_endpoint_passes_the_reason_through(isolated_db, monkeypatch):
    from services import leads

    db = isolated_db
    _setup(db)
    lead_id = _lead()
    client = _client(monkeypatch)
    res = client.post("/api/leads/status", json={
        "initData": "2", "lead_id": lead_id, "status": "lost",
        "reason": "competitor", "note": "взял у соседей",
    })
    assert res.status_code == 200, res.text
    assert _run(leads.get_lead(lead_id))["lost"]["reason"] == "competitor"


def test_status_endpoint_rejects_garbage_reason(isolated_db, monkeypatch):
    db = isolated_db
    _setup(db)
    lead_id = _lead()
    res = _client(monkeypatch).post("/api/leads/status", json={
        "initData": "2", "lead_id": lead_id, "status": "lost", "reason": "хм",
    })
    assert res.status_code == 400
