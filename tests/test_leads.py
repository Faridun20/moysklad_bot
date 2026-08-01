"""
Воронка клиентов: наблюдатель переписок и метрики.

Два главных инварианта проверяются здесь, потому что нарушить их легко, а
заметить — нет:

* бот НИЧЕГО не отправляет (иначе он однажды напишет клиенту от имени
  менеджера);
* тексты сообщений нигде не сохраняются.

БД настоящая (isolated_db), корутины через asyncio.run.
"""

import asyncio
import importlib
import pathlib
import re
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


def _ago(**kw):
    return (local_now().replace(tzinfo=None) - timedelta(**kw)).strftime("%Y-%m-%d %H:%M:%S")


def _connect(manager_id=1, conn="conn-1", can_read=True, enabled=True):
    from services import leads

    _run(leads.save_connection(
        conn, manager_id=manager_id, user_chat_id=manager_id,
        is_enabled=enabled, can_read=can_read,
    ))
    return conn


# ─── Инварианты наблюдателя ───────────────────────────────────────────────────


def test_observer_never_sends_anything():
    """Бот, который однажды напишет клиенту от имени менеджера, разрушит саму
    затею. Проверяем исходник: ни одного вызова отправки."""
    src = pathlib.Path("handlers/business.py").read_text(encoding="utf-8")
    forbidden = re.findall(
        r"\b(?:message|bot|event)\.(answer\w*|reply\w*|send_\w+|edit_\w+)\s*\(", src
    )
    assert forbidden == [], f"наблюдатель пытается писать: {forbidden}"


def test_message_text_is_never_read():
    """Текст не должен покидать хендлер — в сервис уходят только id и
    направление.

    Разбираем КОД, а не файл целиком: в комментариях эти слова встречаются
    законно, и поиск по строке ловил бы объяснение вместо нарушения.
    """
    import ast

    tree = ast.parse(pathlib.Path("handlers/business.py").read_text(encoding="utf-8"))
    touched = {
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr in ("text", "caption", "html_text")
    }
    assert touched == set(), f"наблюдатель читает содержимое сообщения: {touched}"


def test_lead_table_has_no_place_for_message_text():
    """Хранить переписку клиентов — ответственность без выгоды: если колонки
    нет, туда ничего и не запишут."""
    schema = pathlib.Path("services/database.py").read_text(encoding="utf-8")
    start = schema.index("CREATE TABLE IF NOT EXISTS leads")
    columns = schema[start: schema.index(')"""', start)].lower()
    for banned in (" text_", "message_text", "body", "content"):
        assert banned not in columns, banned


# ─── Запись активности ────────────────────────────────────────────────────────


def test_first_inbound_creates_a_lead(isolated_db):
    from services import leads

    db = isolated_db
    _setup(db)
    res = _run(leads.record_message(
        tg_user_id=555, manager_id=1, inbound=True, username="akmal", display_name="Акмал"
    ))
    assert res["created"] is True

    rows = _run(leads.list_leads())
    assert len(rows) == 1
    assert rows[0]["display_name"] == "Акмал"
    assert rows[0]["state"]["never_answered"] is True
    assert rows[0]["status"] == "new"


def test_one_client_is_one_lead_even_across_managers(isolated_db):
    """«Сколько клиентов обратилось» не должно удваиваться оттого, что человек
    написал двум менеджерам."""
    from services import leads

    db = isolated_db
    _setup(db)
    _run(leads.record_message(tg_user_id=555, manager_id=1, inbound=True))
    _run(leads.record_message(tg_user_id=555, manager_id=2, inbound=True))

    rows = _run(leads.list_leads())
    assert len(rows) == 1
    assert rows[0]["manager_id"] == 2   # менеджер последнего контакта


def test_reply_is_recorded_and_state_changes(isolated_db):
    from services import leads

    db = isolated_db
    _setup(db)
    _run(leads.record_message(tg_user_id=555, manager_id=1, inbound=True))
    _run(leads.record_message(tg_user_id=555, manager_id=1, inbound=False))

    lead = _run(leads.list_leads())[0]
    assert lead["state"]["replied"] is True
    assert lead["state"]["never_answered"] is False
    assert lead["first_reply_at"]


def test_outreach_is_not_counted_as_a_reply(isolated_db):
    """Холодное сообщение менеджера не должно выглядеть как мгновенная реакция
    на обращение."""
    from services import leads

    db = isolated_db
    _setup(db)
    _run(leads.record_message(tg_user_id=555, manager_id=1, inbound=False))

    lead = _run(leads.list_leads())[0]
    assert lead["first_reply_at"] is None


def test_awaiting_reply_needs_the_threshold(isolated_db):
    """Сообщение минуту назад — не «висит без ответа»."""
    from services import leads

    db = isolated_db
    _setup(db)
    _run(leads.record_message(tg_user_id=555, manager_id=1, inbound=True))
    assert _run(leads.list_leads())[0]["state"]["awaiting_reply"] is False

    _run(leads.record_message(tg_user_id=556, manager_id=1, inbound=True, at=_ago(hours=20)))
    late = [x for x in _run(leads.list_leads()) if x["tg_user_id"] == 556][0]
    assert late["state"]["awaiting_reply"] is True
    assert [x["tg_user_id"] for x in _run(leads.awaiting_reply())] == [556]


def test_reengagement_is_recorded_on_the_gap(isolated_db):
    """Возврат определяется паузой в момент сообщения — задним числом его из
    агрегатов уже не достать."""
    from services import leads

    db = isolated_db
    _setup(db)
    _run(leads.record_message(tg_user_id=555, manager_id=1, inbound=True, at=_ago(days=40)))
    _run(leads.record_message(tg_user_id=555, manager_id=1, inbound=False, at=_ago(days=40)))

    res = _run(leads.record_message(tg_user_id=555, manager_id=1, inbound=True))
    assert res["reengaged"] is True
    kinds = [e["kind"] for e in _run(leads.get_lead(res["lead_id"]))["events"]]
    assert "reengaged" in kinds


def test_continuing_conversation_is_not_a_return(isolated_db):
    """Ответ клиента через день — продолжение разговора, а не новое обращение."""
    from services import leads

    db = isolated_db
    _setup(db)
    _run(leads.record_message(tg_user_id=555, manager_id=1, inbound=True, at=_ago(days=40)))
    _run(leads.record_message(tg_user_id=555, manager_id=1, inbound=False, at=_ago(days=1)))

    res = _run(leads.record_message(tg_user_id=555, manager_id=1, inbound=True))
    assert res["reengaged"] is False


# ─── Исход и привязка ─────────────────────────────────────────────────────────


def test_status_is_set_by_hand_and_audited(isolated_db):
    from services import leads

    db = isolated_db
    _setup(db)
    lead_id = _run(leads.record_message(tg_user_id=555, manager_id=1, inbound=True))["lead_id"]

    assert _run(leads.set_status(lead_id, "won", user_id=2))["changed"] is True
    assert _run(leads.get_lead(lead_id))["status"] == "won"
    # Повтор — не ошибка и не второе событие.
    assert _run(leads.set_status(lead_id, "won", user_id=2))["changed"] is False

    log = _run(db.get_audit_log(limit=10))
    assert any(e["action"] == "lead_status" for e in log)


def test_unknown_status_is_rejected(isolated_db):
    from services import leads

    db = isolated_db
    _setup(db)
    lead_id = _run(leads.record_message(tg_user_id=555, manager_id=1, inbound=True))["lead_id"]
    assert _run(leads.set_status(lead_id, "maybe", user_id=2))["ok"] is False


def test_link_to_counterparty(isolated_db):
    """В переписке клиент — Telegram-аккаунт, в заказах — контрагент; других
    общих полей у них нет."""
    from services import leads

    db = isolated_db
    _setup(db)
    lead_id = _run(leads.record_message(tg_user_id=555, manager_id=1, inbound=True))["lead_id"]

    assert _run(leads.link_agent(lead_id, "AG-1", user_id=2))["ok"] is True
    assert _run(leads.get_lead(lead_id))["agent_ms_id"] == "AG-1"
    # Отвязать тоже можно — привязали не того.
    _run(leads.link_agent(lead_id, None, user_id=2))
    assert _run(leads.get_lead(lead_id))["agent_ms_id"] is None


# ─── Воронка ──────────────────────────────────────────────────────────────────


def _today():
    return local_now().replace(tzinfo=None).strftime("%Y-%m-%d")


def test_funnel_counts_clients_not_messages(isolated_db):
    """Клиент, который пишет каждый день, — одно обращение, а не десять: иначе
    конверсия падала бы от активности, а не от результата."""
    from services import leads

    db = isolated_db
    _setup(db)
    for _ in range(5):
        _run(leads.record_message(tg_user_id=555, manager_id=1, inbound=True))
    _run(leads.record_message(tg_user_id=556, manager_id=1, inbound=True))

    res = _run(leads.funnel(_today(), _today()))
    assert res["contacted"] == 2


def test_funnel_rates(isolated_db):
    from services import leads

    db = isolated_db
    _setup(db)
    a = _run(leads.record_message(tg_user_id=555, manager_id=1, inbound=True))["lead_id"]
    _run(leads.record_message(tg_user_id=555, manager_id=1, inbound=False))
    _run(leads.set_status(a, "won", user_id=2))
    _run(leads.record_message(tg_user_id=556, manager_id=1, inbound=True))

    res = _run(leads.funnel(_today(), _today()))
    assert res["contacted"] == 2
    assert res["replied"] == 1
    assert res["won"] == 1
    assert res["in_progress"] == 1
    assert res["reply_rate"] == 0.5
    assert res["win_rate"] == 0.5


def test_funnel_is_empty_without_leads(isolated_db):
    from services import leads

    db = isolated_db
    _setup(db)
    res = _run(leads.funnel(_today(), _today()))
    assert res["contacted"] == 0
    assert res["reply_rate"] is None


def test_returned_and_bought_is_visible(isolated_db):
    """«Вернулся через время и купил» — метрика, ради которой всё затевалось."""
    from services import leads

    db = isolated_db
    _setup(db)
    lead_id = _run(leads.record_message(
        tg_user_id=555, manager_id=1, inbound=True, at=_ago(days=40)
    ))["lead_id"]
    _run(leads.record_message(tg_user_id=555, manager_id=1, inbound=True))
    _run(leads.set_status(lead_id, "won", user_id=2))

    # Период считаем по первому обращению — оно было 40 дней назад.
    since = (local_now().replace(tzinfo=None) - timedelta(days=60)).strftime("%Y-%m-%d")
    res = _run(leads.funnel(since, _today()))
    assert res["reengaged"] == 1
    assert res["reengaged_won"] == 1


def test_by_manager_splits_the_funnel(isolated_db):
    from services import leads

    db = isolated_db
    _setup(db)
    a = _run(leads.record_message(tg_user_id=555, manager_id=1, inbound=True))["lead_id"]
    _run(leads.set_status(a, "won", user_id=2))
    _run(leads.record_message(tg_user_id=556, manager_id=2, inbound=True))

    rows = _run(leads.by_manager(_today(), _today()))
    by_id = {r["manager_id"]: r for r in rows}
    assert by_id[1]["won"] == 1 and by_id[1]["win_rate"] == 1.0
    assert by_id[2]["won"] == 0


# ─── Ручки ────────────────────────────────────────────────────────────────────


def _client(monkeypatch):
    import webapp.server as server

    importlib.reload(roles)
    monkeypatch.setattr(server, "verify_init_data", lambda s: {"id": int(s), "first_name": "U"})
    return TestClient(server.app)


def _post(client, path, uid, **body):
    return client.post(path, json={"initData": str(uid), **body})


def test_manager_sees_only_own_leads(isolated_db, monkeypatch):
    """Чужие переписки — не его дело."""
    from services import leads

    db = isolated_db
    _setup(db)
    _run(leads.record_message(tg_user_id=555, manager_id=1, inbound=True))
    _run(leads.record_message(tg_user_id=556, manager_id=2, inbound=True))

    mine = _post(_client(monkeypatch), "/api/leads/list", 1).json()
    assert [x["tg_user_id"] for x in mine["leads"]] == [555]
    assert mine["scope"] == "personal"

    boss = _post(_client(monkeypatch), "/api/leads/list", 2).json()
    assert len(boss["leads"]) == 2


def test_manager_cannot_open_someone_elses_lead(isolated_db, monkeypatch):
    from services import leads

    db = isolated_db
    _setup(db)
    other = _run(leads.record_message(tg_user_id=556, manager_id=2, inbound=True))["lead_id"]

    assert _post(_client(monkeypatch), "/api/leads/card", 1, lead_id=other).status_code == 403
    assert _post(_client(monkeypatch), "/api/leads/status", 1,
                 lead_id=other, status="won").status_code == 403


def test_funnel_is_boss_only(isolated_db, monkeypatch):
    db = isolated_db
    _setup(db)
    client = _client(monkeypatch)
    assert _post(client, "/api/leads/funnel", 1).status_code == 403
    assert _post(client, "/api/leads/funnel", 2).status_code == 200


def test_leads_denied_to_bookkeeper(isolated_db, monkeypatch):
    db = isolated_db
    _setup(db)
    assert _post(_client(monkeypatch), "/api/leads/list", 3).status_code == 403


def test_funnel_endpoint_names_managers(isolated_db, monkeypatch):
    from services import leads

    db = isolated_db
    _setup(db)
    _run(leads.record_message(tg_user_id=555, manager_id=1, inbound=True))

    body = _post(_client(monkeypatch), "/api/leads/funnel", 2, period="month").json()
    assert body["by_manager"][0]["name"] == "Manager"
    assert body["funnel"]["contacted"] == 1


def test_connection_state_is_visible_to_boss(isolated_db, monkeypatch):
    """Подключение без права читать сообщения выглядит как «клиенты не пишут» —
    руководитель должен видеть, что дело в правах."""
    db = isolated_db
    _setup(db)
    _connect(manager_id=1, can_read=False)

    body = _post(_client(monkeypatch), "/api/leads/list", 2).json()
    assert body["connections"][0]["can_read"] == 0
