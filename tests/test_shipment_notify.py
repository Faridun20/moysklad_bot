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
        return True  # как реальная tg_send_message: True при успехе (R5)

    async def _no_positions(demand_id):
        return []

    monkeypatch.setattr(notifier, "tg_send_message", _fake_send)
    monkeypatch.setattr(notifier, "get_shipment_positions", _no_positions)
    return db, boss_id, sent


# ─── Дедуп на уровне БД ──────────────────────────────────────────────────────


def test_mark_shipment_notified_idempotent(isolated_db):
    db = isolated_db
    assert db.mark_shipment_notified("d-1") is True  # впервые → уведомлять
    assert db.mark_shipment_notified("d-1") is False  # повтор → не дублировать
    assert db.mark_shipment_notified("d-2") is True
    assert db.mark_shipment_notified("") is False  # пустой id игнорируем


def test_prune_notified_shipments_drops_only_old(isolated_db):
    db = isolated_db
    db.mark_shipment_notified("fresh")  # notified_at = сейчас
    # Состарим одну запись на 40 дней назад прямым UPDATE.
    from datetime import datetime, timedelta

    old_ts = (datetime.now() - timedelta(days=40)).strftime("%Y-%m-%d %H:%M:%S")
    db.mark_shipment_notified("old")
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(
            db.q("UPDATE notified_shipments SET notified_at = ? WHERE demand_id = ?"),
            (old_ts, "old"),
        )
        conn.commit()

    deleted = db.prune_notified_shipments(older_than_days=30)
    assert deleted == 1
    # Старая ушла (можно снова застолбить), свежая осталась (повтор → False).
    assert db.mark_shipment_notified("old") is True
    assert db.mark_shipment_notified("fresh") is False


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

    ok = asyncio.run(notifier.notify_new_shipment("d-5", shipment=_shipment(), recipients=[]))
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


def test_notify_all_sends_failed_frees_slot(notify_env, monkeypatch):
    """R5: если ВСЕ отправки упали (Telegram down) — дедуп-слот освобождается,
    чтобы резервный поллер повторил позже (иначе уведомление потеряно навсегда)."""
    db, boss_id, sent = notify_env

    async def _fail_send(chat_id, text, **kw):
        return False  # доставка не удалась

    monkeypatch.setattr(notifier, "tg_send_message", _fail_send)

    ok = asyncio.run(
        notifier.notify_new_shipment("d-fail", shipment=_shipment(), recipients=[boss_id])
    )
    assert ok is False
    # Слот свободен → поллер сможет повторить (mark вернёт True = впервые).
    assert db.mark_shipment_notified("d-fail") is True


def test_notify_partial_success_keeps_slot(notify_env, monkeypatch):
    """R5: если хотя бы один получатель получил уведомление — слот занят
    (не дублируем), несмотря на сбой по другим."""
    db, _boss, sent = notify_env

    async def _mixed_send(chat_id, text, **kw):
        return chat_id == 100  # 100 ок, остальные — нет

    monkeypatch.setattr(notifier, "tg_send_message", _mixed_send)

    ok = asyncio.run(
        notifier.notify_new_shipment("d-mix", shipment=_shipment(), recipients=[100, 200])
    )
    assert ok is True
    # Слот занят — повтор не нужен.
    assert db.mark_shipment_notified("d-mix") is False


# ─── Подавление уведомлений о бот-созданных отгрузках ────────────────────────


def test_bot_created_shipment_is_not_notified(notify_env):
    db, boss_id, sent = notify_env
    bot_demand = _shipment()
    bot_demand["attributes"] = [
        {"name": "telegram_user_id", "value": 12345},
        {"name": "telegram_full_name", "value": "Менеджер"},
    ]
    ok = asyncio.run(
        notifier.notify_new_shipment("d-bot", shipment=bot_demand, recipients=[boss_id])
    )
    assert ok is False
    assert sent == []
    # Слот застолблён — поллер тоже не пошлёт.
    assert db.mark_shipment_notified("d-bot") is False


def test_external_shipment_with_empty_attributes_is_notified(notify_env):
    db, boss_id, sent = notify_env
    ext = _shipment()
    ext["attributes"] = []  # внешний demand без бот-меток
    ok = asyncio.run(notifier.notify_new_shipment("d-ext", shipment=ext, recipients=[boss_id]))
    assert ok is True
    assert len(sent) == 1


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
