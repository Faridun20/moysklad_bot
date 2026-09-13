"""T2.2 — compare-and-set на переходах статуса заказа.

Критерий готовности из плана: заказ переведён в `rejected`, нажатие
«Одобрить» на старом сообщении не меняет статус и отвечает пояснением.

Сценарий из жизни (§2.2): МойСклад прислал `Unsuccessful` → ms_sync_handler
перевёл заказ pending→rejected. Заявка при этом осталась `pending` — её никто
не трогал. Босс открывает старое сообщение в чате и жмёт «Одобрить». Раньше
заявка становилась approved, а заказ ПРОДАВЛИВАЛСЯ в approved безусловным
UPDATE — отклонённый заказ воскресал и получал новые документы в МойСклад.
"""

import asyncio

import pytest

from services.database import legal_sources_for


def _pending_request(db, uid=1):
    """Заказ в pending + заявка на отгрузку в pending."""
    db.set_role(uid, "mgr", "Manager", "manager")
    oid = db.create_order(uid, "Manager", "")
    db.add_order_item(oid, "Товар", "href", 1, "шт", 100.0)
    db.update_order_agent(oid, "A-1", "Клиент")
    db.update_order_status(oid, "pending")
    rid = db.create_shipment_request(oid, uid, "Manager", "")
    return rid, oid


def _status(db, oid):
    return asyncio.run(db.get_order(oid))["status"]


# ─── legal_sources_for: берём из TRANSITIONS, второго списка нет ────────────


def test_legal_sources_derived_from_transitions():
    from services.order_workflow import TRANSITIONS

    assert legal_sources_for("approved") == ("pending",)
    assert set(legal_sources_for("rejected")) == {"draft", "pending"}
    # Ровно то, что записано в машине состояний, без параллельного списка.
    for target in ("approved", "rejected", "shipped", "cancelled"):
        expected = {src for src, dst in TRANSITIONS.items() if target in dst}
        assert set(legal_sources_for(target)) == expected


def test_unknown_target_yields_empty_and_blocks_update(isolated_db):
    """Опечатка в целевом статусе не должна превращаться в безусловный UPDATE."""
    db = isolated_db
    _rid, oid = _pending_request(db)
    assert legal_sources_for("bogus_status") == ()
    assert db.update_order_status(oid, "bogus_status", expected_status=()) is False
    assert _status(db, oid) == "pending"


# ─── update_order_status: CAS ───────────────────────────────────────────────


def test_cas_blocks_transition_from_wrong_status(isolated_db):
    db = isolated_db
    _rid, oid = _pending_request(db)
    db.update_order_status(oid, "rejected")

    moved = db.update_order_status(oid, "approved", expected_status=("pending",))
    assert moved is False
    assert _status(db, oid) == "rejected"


def test_cas_allows_transition_from_right_status(isolated_db):
    db = isolated_db
    _rid, oid = _pending_request(db)
    assert db.update_order_status(oid, "approved", expected_status=("pending",)) is True
    assert _status(db, oid) == "approved"


def test_cas_accepts_plain_string(isolated_db):
    db = isolated_db
    _rid, oid = _pending_request(db)
    assert db.update_order_status(oid, "approved", expected_status="pending") is True


def test_without_expected_status_update_is_unconditional(isolated_db):
    """Легаси-вызовы (submit, удаление черновика) пока без guard'а —
    закрываются в T2.3/T2.7. Фиксируем текущее поведение явно."""
    db = isolated_db
    _rid, oid = _pending_request(db)
    db.update_order_status(oid, "rejected")
    assert db.update_order_status(oid, "approved") is True
    assert _status(db, oid) == "approved"


# ─── Критерий готовности: воскрешения не происходит ────────────────────────


def test_approve_does_not_resurrect_rejected_order(isolated_db):
    db = isolated_db
    rid, oid = _pending_request(db)

    # МойСклад прислал Unsuccessful — заказ отклонён, заявка осталась pending.
    db.update_order_status(oid, "rejected")
    assert db.get_shipment_request(rid)["status"] == "pending"

    decision = db.approve_shipment_request(rid, 2, "Boss")

    assert decision.applied is False
    assert decision.reason == "order_moved"
    assert decision.order_status == "rejected"
    # Заказ не воскрес…
    assert _status(db, oid) == "rejected"
    # …и заявка НЕ осталась одобренной: откатились обе записи.
    assert db.get_shipment_request(rid)["status"] == "pending"


def test_reject_does_not_override_shipped_order(isolated_db):
    """Симметрично: reject не продавливает 'rejected' из shipped."""
    db = isolated_db
    rid, oid = _pending_request(db)
    db.update_order_status(oid, "approved")
    db.update_order_status(oid, "shipped")

    decision = db.reject_shipment_request(rid, 2, "Boss")

    assert decision.applied is False
    assert decision.reason == "order_moved"
    assert _status(db, oid) == "shipped"
    assert db.get_shipment_request(rid)["status"] == "pending"


def test_second_boss_gets_request_taken(isolated_db):
    """Два босса жмут «Одобрить»: второй получает другую причину."""
    db = isolated_db
    rid, _oid = _pending_request(db)

    first = db.approve_shipment_request(rid, 2, "Boss A")
    second = db.approve_shipment_request(rid, 3, "Boss B")

    assert first.applied is True
    assert second.applied is False
    assert second.reason == "request_taken"


def test_happy_path_still_works(isolated_db):
    db = isolated_db
    rid, oid = _pending_request(db)
    decision = db.approve_shipment_request(rid, 2, "Boss")
    assert decision.applied is True
    assert _status(db, oid) == "approved"
    assert db.get_shipment_request(rid)["status"] == "approved"


# ─── Сообщение боссу ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "reason,order_status,expect",
    [
        ("order_moved", "rejected", "отклонён"),
        ("order_moved", "shipped", "отгружен"),
        ("request_taken", None, "обработана"),
    ],
)
def test_decision_error_message_explains_cause(reason, order_status, expect):
    from services.database import ShipmentDecision
    from services.order_workflow import _decision_error

    msg = _decision_error(ShipmentDecision(False, reason, order_status), 42)
    assert expect in msg
    if reason == "order_moved":
        assert "#42" in msg


def test_workflow_reject_reports_order_moved(isolated_db):
    """End-to-end: через order_workflow босс получает пояснение, а не
    «заявка уже обработана»."""
    db = isolated_db
    from services import order_workflow

    rid, oid = _pending_request(db)
    db.update_order_status(oid, "shipped")

    res = asyncio.run(order_workflow.reject_shipment_request(rid, 2, "Boss", None))
    assert res["ok"] is False
    assert "отгружен" in res["error"]
    assert _status(db, oid) == "shipped"
