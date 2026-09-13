"""
Тесты services.order_workflow.reject_shipment_request — end-to-end на
isolated_db, с mocked bot (для notify_order_rejected).

Покрываем три ветки:
  * заявка не найдена → {ok: False, error: "не найдена"}
  * заявка уже обработана (не pending) → ok=False с понятным сообщением
  * нормальный reject → DB-статус 'rejected', notify_order_rejected вызван

approve_shipment_request не покрываем тут — там ~250 строк с aiogram bot
+ ms_demand + create_customerorder PDF chain; отдельный contract-test
требует значительной mock-инфраструктуры.
"""

import asyncio

from services import order_workflow


class _FakeBot:
    """Минимальный bot: только send_message (его дёргает notify_order_rejected)."""

    def __init__(self):
        self.messages = []

    async def send_message(self, chat_id, text, **kwargs):
        self.messages.append((chat_id, text, kwargs))


def _make_pending_request(db, manager_id: int = 700) -> tuple[int, int]:
    """Создать draft-заказ + shipment_request в pending. Возвращает (req_id, order_id)."""
    db.set_role(manager_id, "mgr_u", "Manager Name", "manager")
    oid = db.create_order(manager_id, "Manager Name", "")
    db.add_order_item(oid, "Товар", "", 1, "шт", 100.0)
    db.update_order_status(oid, "pending")
    rid = db.create_shipment_request(oid, manager_id, "Manager Name", "комм")
    return rid, oid


def test_reject_request_not_found(isolated_db):
    """req_id не существует → ok=False с понятной ошибкой."""
    bot = _FakeBot()
    res = asyncio.run(
        order_workflow.reject_shipment_request(999999, 1, "Boss", bot)
    )
    assert res["ok"] is False
    assert "не найдена" in res["error"]
    assert bot.messages == []


def test_reject_request_already_processed(isolated_db):
    """Заявка не в pending (уже approved/rejected) → ok=False."""
    db = isolated_db
    rid, _oid = _make_pending_request(db)
    # Сначала approve'аем чтобы статус стал не-pending.
    assert db.approve_shipment_request(rid, 1, "Boss").applied is True
    bot = _FakeBot()
    res = asyncio.run(order_workflow.reject_shipment_request(rid, 2, "Boss2", bot))
    assert res["ok"] is False
    assert "обработана" in res["error"]
    assert bot.messages == []


def test_reject_request_happy_path_notifies_manager(isolated_db, monkeypatch):
    """Reject pending заявки: DB status='rejected', notify_order_rejected дёргается с
    user_id владельца заказа."""
    db = isolated_db
    mgr = 700
    rid, _oid = _make_pending_request(db, manager_id=mgr)

    # Мокаем notify_order_rejected, чтобы не зависеть от tg_send_message.
    captured = {}

    async def fake_notify(bot_, user_id, req_id, boss_name, when):
        captured["user_id"] = user_id
        captured["req_id"] = req_id
        captured["boss_name"] = boss_name

    import services.notify as notify

    monkeypatch.setattr(notify, "notify_order_rejected", fake_notify)

    bot = _FakeBot()
    res = asyncio.run(order_workflow.reject_shipment_request(rid, 1, "Boss", bot))
    assert res["ok"] is True
    assert res["req_id"] == rid
    assert res["order_id"] == _oid
    assert "now" in res

    # DB: заявка в rejected
    req = db.get_shipment_request(rid)
    assert req["status"] == "rejected"
    # Менеджер уведомлён
    assert captured["user_id"] == mgr
    assert captured["req_id"] == rid
    assert captured["boss_name"] == "Boss"


def test_reject_request_swallows_notify_exception(isolated_db, monkeypatch):
    """notify_order_rejected упал — reject всё равно успешен (best-effort)."""
    db = isolated_db
    rid, _oid = _make_pending_request(db)

    async def explosive_notify(*a, **kw):
        raise RuntimeError("network down")

    import services.notify as notify

    monkeypatch.setattr(notify, "notify_order_rejected", explosive_notify)
    bot = _FakeBot()
    res = asyncio.run(order_workflow.reject_shipment_request(rid, 1, "Boss", bot))
    # Reject прошёл, exception от notify не утопил.
    assert res["ok"] is True
    req = db.get_shipment_request(rid)
    assert req["status"] == "rejected"


def test_reject_request_no_bot_skips_notification(isolated_db):
    """bot=None — пропускаем notify, всё остальное проходит."""
    db = isolated_db
    rid, _oid = _make_pending_request(db)
    res = asyncio.run(order_workflow.reject_shipment_request(rid, 1, "Boss", None))
    assert res["ok"] is True
    req = db.get_shipment_request(rid)
    assert req["status"] == "rejected"
