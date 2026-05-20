"""
Событийные уведомления об отгрузках: дедуп + рассылка.

Покрывает переход с опроса раз в N секунд на мгновенное уведомление через
MS-вебхук. Ключевой инвариант — на каждую отгрузку уходит РОВНО одно
уведомление, кто бы ни сработал первым (вебхук в webapp-процессе или
поллер-резерв в bot-процессе). Дедуп — в БД (общий Postgres на проде).

Замокан tg_send_message (граница с Telegram) и MS-фетчи. БД настоящая.
"""

import asyncio

import pytest

import services.notifier as notifier


def _shipment(name="Отгрузка"):
    return {
        "name": name,
        "moment": "2026-05-21 10:00:00.000",
        "agent": {"name": "Клиент"},
        "owner": {"name": "Менеджер"},
        "sum": 10000,
    }


@pytest.fixture
def notify_env(isolated_db, monkeypatch):
    db = isolated_db
    boss_id = 100
    db.set_role(boss_id, "boss_user", "Boss", "boss")

    sent: list[tuple] = []

    async def _fake_send(chat_id, text, **kw):
        sent.append((chat_id, text))

    async def _no_positions(demand_id):
        return []

    monkeypatch.setattr(notifier, "tg_send_message", _fake_send)
    monkeypatch.setattr(notifier, "get_shipment_positions", _no_positions)
    return db, boss_id, sent


# ─── Дедуп на уровне БД ──────────────────────────────────────────────────────


def test_mark_shipment_notified_idempotent(isolated_db):
    db = isolated_db
    assert db.mark_shipment_notified("d-1") is True   # впервые → уведомлять
    assert db.mark_shipment_notified("d-1") is False  # повтор → не дублировать
    assert db.mark_shipment_notified("d-2") is True
    assert db.mark_shipment_notified("") is False     # пустой id игнорируем


# ─── notify_new_shipment ─────────────────────────────────────────────────────


def test_notify_sends_once_then_dedups(notify_env):
    db, boss_id, sent = notify_env

    ok = asyncio.run(
        notifier.notify_new_shipment("d-1", shipment=_shipment(), recipients=[boss_id])
    )
    assert ok is True
    assert len(sent) == 1
    assert sent[0][0] == boss_id
    assert "Новая отгрузка" in sent[0][1]

    # Тот же demand повторно (например, поллер после вебхука) → молчим.
    sent.clear()
    ok2 = asyncio.run(
        notifier.notify_new_shipment("d-1", shipment=_shipment(), recipients=[boss_id])
    )
    assert ok2 is False
    assert sent == []


def test_notify_fetches_shipment_when_not_given(notify_env, monkeypatch):
    db, boss_id, sent = notify_env

    async def _fake_get(demand_id):
        return _shipment(name=f"demand {demand_id}")

    monkeypatch.setattr(notifier, "get_shipment", _fake_get)

    ok = asyncio.run(notifier.notify_new_shipment("d-9"))  # без shipment-объекта
    assert ok is True
    assert len(sent) == 1
    assert sent[0][0] == boss_id  # получатель взят из БД (boss)


def test_notify_no_recipients_leaves_slot_free(notify_env):
    db, boss_id, sent = notify_env

    ok = asyncio.run(
        notifier.notify_new_shipment("d-5", shipment=_shipment(), recipients=[])
    )
    assert ok is False
    assert sent == []
    # Слот НЕ застолблён — поллер-резерв сможет повторить попытку позже.
    assert db.mark_shipment_notified("d-5") is True


def test_notify_failed_fetch_leaves_slot_free(notify_env, monkeypatch):
    db, boss_id, sent = notify_env

    async def _boom(demand_id):
        raise RuntimeError("MS down")

    monkeypatch.setattr(notifier, "get_shipment", _boom)

    ok = asyncio.run(notifier.notify_new_shipment("d-7"))
    assert ok is False
    assert sent == []
    assert db.mark_shipment_notified("d-7") is True  # не consumed


# ─── Разбор событий вебхука ──────────────────────────────────────────────────


def test_webhook_event_filter_picks_only_new_demands():
    from webapp.server import _new_demand_ids_from_events

    base = "https://api.moysklad.ru/api/remap/1.2/entity"
    events = [
        {"action": "CREATE", "meta": {"type": "demand", "href": f"{base}/demand/D1"}},
        {"action": "UPDATE", "meta": {"type": "demand", "href": f"{base}/demand/D2"}},
        {"action": "CREATE", "meta": {"type": "retaildemand", "href": f"{base}/retaildemand/D3"}},
        {"action": "CREATE", "meta": {"type": "customerorder", "href": f"{base}/customerorder/CO"}},
        {"action": "DELETE", "meta": {"type": "demand", "href": f"{base}/demand/D4"}},
    ]
    assert _new_demand_ids_from_events(events) == ["D1", "D3"]
