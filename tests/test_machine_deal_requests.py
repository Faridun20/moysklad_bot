"""
Сделки по технике через одобрение руководителя (`services.machine_deal_requests`).

Решение владельца: бронь, продажу и рассрочку оформляет менеджер, руководитель
одобряет. Проверяем весь путь через ручки WebApp (SQLite, isolated_db) и
карточку бота:

* менеджер → заявка `pending`, машина не двигается, сделки и графика нет;
  руководитель одобряет → статус, сделка, график; поступление по графику;
* на доработку → правка → повторная отправка; отклонение не трогает машину;
* вторая заявка на ту же машину и ручной переход из-под заявки — 409;
* права: гость/кладовщик 403, менеджер не одобряет при живом руководителе, но
  одобряет сам, когда руководителя в системе нет (с пометкой в аудите);
* идемпотентность одобрения; уведомления (карточка руководителю, итог менеджеру);
* бот: исход решения — неактивной кнопкой, устаревшая карточка гасится.

Гонки на настоящем Postgres — `tests/test_machine_deal_requests_postgres.py`.
"""

from __future__ import annotations

import asyncio
import importlib
import sqlite3

import pytest
from fastapi.testclient import TestClient

import services.roles as roles

MGR, BOSS, BOOK, KEEPER, GUEST, MGR2 = 1, 2, 3, 4, 5, 6


def _run(coro):
    return asyncio.run(coro)


def _setup(db, *, boss: bool = True):
    roles.invalidate_all_roles()
    db.set_role(MGR, "mgr", "Manager", "manager")
    db.set_role(MGR2, "mgr2", "Manager Two", "manager")
    if boss:
        db.set_role(BOSS, "boss", "Boss", "boss")
    db.set_role(BOOK, "acc", "Bookkeeper", "bookkeeper")
    db.set_role(KEEPER, "wh", "Keeper", "warehouse_keeper")
    db.set_role(GUEST, "guest", "Guest", "guest")


@pytest.fixture
def sent(monkeypatch):
    """Исходящие в Telegram — на границе (`tg_send_message`), текст и кнопки."""
    import services.notifier as notifier

    box: list[tuple[int, str, dict | None]] = []

    async def fake(chat_id, text, *, parse_mode="HTML", reply_markup=None):
        box.append((int(chat_id), text, reply_markup))
        return True

    monkeypatch.setattr(notifier, "tg_send_message", fake)
    return box


def _machine(vin="JCB-001", status="in_stock", price_cents=2_500_000, **over):
    from services import machines

    res = _run(machines.create_machine(vin=vin, name="JCB 3CX", created_by=BOSS,
                                       price_cents=price_cents, status=status, **over))
    assert res["ok"], res
    return res["machine_id"]


def _client(monkeypatch):
    import webapp.server as server

    importlib.reload(roles)
    monkeypatch.setattr(server, "verify_init_data", lambda s: {"id": int(s), "first_name": "U"})
    return TestClient(server.app)


def _post(client, path, uid, **body):
    return client.post(path, json={"initData": str(uid), **body})


_key = iter(range(10_000))


def _deal(client, uid, mid, kind="credit", **over):
    body = {"machine_id": mid, "kind": kind, "price": "24 000", "buyer_name": "Азиз <b>Рахимов</b>",
            "buyer_phone": "+998901112233", "buyer_passport": "AA1234567",
            "idempotency_key": f"deal-{next(_key)}"}
    if kind == "credit":
        body.update(down_payment="4 000", months=4)
    body.update(over)
    return _post(client, "/api/machines/deal", uid, **body)


def _decide(client, uid, action, request_id, **body):
    return _post(client, f"/api/machines/deals/{action}", uid, request_id=request_id,
                 idempotency_key=f"{action}-{next(_key)}", **body)


def _rows(db, sql, params=()):
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(db.q(sql), params)
        return [dict(r) for r in cur.fetchall()]


# ─── Полный путь ──────────────────────────────────────────────────────────────


def test_manager_installment_waits_for_boss_then_schedule_lives(isolated_db, monkeypatch, sent):
    from services import machines

    db = isolated_db
    _setup(db)
    mid = _machine()
    client = _client(monkeypatch)

    r = _deal(client, MGR, mid)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["pending"] is True and body["deal_id"] is None
    rid = body["request_id"]

    # До решения: машина на складе, сделки и графика нет, «ждёт одобрения».
    assert _rows(db, "SELECT status FROM machines WHERE id = ?", (mid,)) == [{"status": "in_stock"}]
    assert _rows(db, "SELECT COUNT(*) AS n FROM machine_deals")[0]["n"] == 0
    assert _rows(db, "SELECT COUNT(*) AS n FROM machine_deal_payments")[0]["n"] == 0
    listing = _post(client, "/api/machines/list", MGR).json()
    row = next(m for m in listing["machines"] if m["id"] == mid)
    assert row["pending_request"]["status"] == "pending" and listing["pending_requests"] == 1
    card = _post(client, "/api/machines/card", MGR, machine_id=mid).json()
    assert card["request"]["id"] == rid and "buyer_passport" not in card["request"]
    assert card["request"]["has_passport"] is True
    assert card["can_request"] == [] and card["next_statuses"] == []
    assert card["can_decide"] is False and "Boss" in card["decide_hint"]

    # Карточка руководителю: цена против прайса, скидка, условия, кнопки.
    [(chat, text, markup)] = sent
    assert chat == BOSS
    assert "24 000 USD" in text and "прайс 25 000 USD" in text and "скидка 4%" in text
    assert "&lt;b&gt;Рахимов&lt;/b&gt;" in text and "<b>Рахимов</b>" not in text
    assert "AA1234567" in text and "4 мес. по 5 000 USD" in text
    callbacks = [b["callback_data"] for row in markup["inline_keyboard"] for b in row]
    assert callbacks == [f"mdr_ok:{rid}", f"mdr_no:{rid}", f"mdr_rw:{rid}"]

    boss_view = _post(client, "/api/machines/deals/pending", BOSS).json()
    assert boss_view["can_decide"] is True and boss_view["decide_hint"] is None
    assert boss_view["requests"][0]["buyer_passport"] == "AA1234567"

    approved = _decide(client, BOSS, "approve", rid)
    assert approved.status_code == 200, approved.text
    res = approved.json()
    assert res["status"] == "on_credit" and res["payments"] == 4 and res["approval_mode"] == "boss"

    deal = _rows(db, "SELECT id, created_by, price_cents FROM machine_deals")[0]
    assert deal["created_by"] == MGR and deal["price_cents"] == 2_400_000
    sched = _rows(db, "SELECT seq, amount_cents, paid_at FROM machine_deal_payments ORDER BY seq")
    assert [s["seq"] for s in sched] == [0, 1, 2, 3, 4] and sched[0]["paid_at"]
    req = _rows(db, "SELECT status, decided_by, approval_mode, deal_id FROM machine_deal_requests")[0]
    assert req == {"status": "approved", "decided_by": BOSS, "approval_mode": "boss", "deal_id": deal["id"]}
    # Итог менеджеру.
    assert sent[-1][0] == MGR and "одобрена" in sent[-1][1]

    # График живой: поступление гасит первый платёж.
    pay = _post(client, "/api/machines/receipt", BOSS, deal_id=deal["id"], amount="5000",
                method="cash", idempotency_key="r1")
    assert pay.status_code == 200, pay.text
    progress = _run(machines.deal_progress(int(deal["id"])))
    assert progress["left_cents"] == 1_500_000

    # Повторное одобрение — 409, а не вторая сделка.
    assert _decide(client, BOSS, "approve", rid).status_code == 409
    assert _rows(db, "SELECT COUNT(*) AS n FROM machine_deals")[0]["n"] == 1
    actions = {a["action"] for a in _run(db.get_audit_log(limit=50))}
    assert {"machine_deal_requested", "machine_deal_approved", "machine_deal_created",
            "machine_status_changed"} <= actions


def test_boss_deal_is_approved_at_once_and_recorded(isolated_db, monkeypatch, sent):
    db = isolated_db
    _setup(db)
    mid = _machine()
    client = _client(monkeypatch)

    r = _deal(client, BOSS, mid, kind="sale", price="25 000")
    assert r.status_code == 200, r.text
    assert r.json()["pending"] is False and r.json()["status"] == "sold" and r.json()["deal_id"]
    req = _rows(db, "SELECT status, approval_mode, decided_by, created_by FROM machine_deal_requests")
    assert req == [{"status": "approved", "approval_mode": "auto", "decided_by": BOSS, "created_by": BOSS}]
    assert sent == [], "руководитель не шлёт карточку сам себе"


def test_booking_rejected_leaves_machine_and_frees_it(isolated_db, monkeypatch, sent):
    db = isolated_db
    _setup(db)
    mid = _machine()
    client = _client(monkeypatch)

    r = _deal(client, MGR, mid, kind="reserve", price="", buyer_passport="")
    assert r.status_code == 200, r.text
    rid = r.json()["request_id"]
    rej = _decide(client, BOSS, "reject", rid, reason="клиент не внёс задаток")
    assert rej.status_code == 200, rej.text
    assert _rows(db, "SELECT status FROM machines WHERE id = ?", (mid,)) == [{"status": "in_stock"}]
    assert "отклонена" in sent[-1][1] and "прежнем статусе" in sent[-1][1]
    card = _post(client, "/api/machines/card", MGR, machine_id=mid).json()
    assert card["request"] is None and "reserve" in card["can_request"]

    again = _deal(client, MGR, mid, kind="reserve", price="", buyer_passport="")
    ok = _decide(client, BOSS, "approve", again.json()["request_id"])
    assert ok.status_code == 200 and ok.json()["status"] == "reserved"
    assert _rows(db, "SELECT COUNT(*) AS n FROM machine_deals")[0]["n"] == 0
    # Бронь — только со склада: на забронированную вторую бронь не оформить.
    assert _deal(client, MGR, mid, kind="reserve", price="", buyer_passport="").status_code == 409


def test_rework_then_resubmit_by_author_only(isolated_db, monkeypatch, sent):
    db = isolated_db
    _setup(db)
    mid = _machine()
    client = _client(monkeypatch)
    rid = _deal(client, MGR, mid, kind="sale", price="20 000").json()["request_id"]

    assert _decide(client, BOSS, "rework", rid, reason="").status_code == 400
    back = _decide(client, BOSS, "rework", rid, reason="скидка 20% — много")
    assert back.status_code == 200, back.text
    assert "доработке" in sent[-1][1] and "скидка 20%" in sent[-1][1]
    mine = _post(client, "/api/machines/deals/pending", MGR).json()
    assert mine["requests"] == [] and [r["id"] for r in mine["my_rework"]] == [rid]
    # Пока заявка на доработке, машина за ней: вторая заявка — 409.
    assert _deal(client, MGR2, mid, kind="sale").status_code == 409
    # Одобрить заявку на доработке нельзя; доработать — только автор.
    assert _decide(client, BOSS, "approve", rid).status_code == 409
    assert _decide(client, MGR2, "resubmit", rid, price="23 000").status_code == 403

    again = _decide(client, MGR, "resubmit", rid, price="23 000")
    assert again.status_code == 200, again.text
    row = _rows(db, "SELECT status, price_cents, attempts, buyer_passport FROM machine_deal_requests")[0]
    assert row == {"status": "pending", "price_cents": 2_300_000, "attempts": 2,
                   "buyer_passport": "AA1234567"}
    assert "попытка 2" in sent[-1][1] and sent[-1][0] == BOSS
    assert _decide(client, BOSS, "approve", rid).json()["status"] == "sold"


def test_manager_can_withdraw_own_request(isolated_db, monkeypatch, sent):
    db = isolated_db
    _setup(db)
    mid = _machine()
    client = _client(monkeypatch)
    rid = _deal(client, MGR, mid, kind="sale").json()["request_id"]
    assert _decide(client, MGR2, "cancel", rid).status_code == 403
    assert _decide(client, MGR, "cancel", rid).status_code == 200
    assert _deal(client, MGR2, mid, kind="sale").status_code == 200


# ─── Защита машины и права ────────────────────────────────────────────────────


def test_one_live_request_per_machine_and_no_manual_moves(isolated_db, monkeypatch, sent):
    from services import machines

    db = isolated_db
    _setup(db)
    mid = _machine()
    client = _client(monkeypatch)
    rid = _deal(client, MGR, mid, kind="sale").json()["request_id"]

    second = _deal(client, MGR2, mid, kind="credit")
    assert second.status_code == 409 and second.json()["request_id"] == rid
    assert _deal(client, BOSS, mid, kind="sale").status_code == 409
    status = _post(client, "/api/machines/status", BOSS, machine_id=mid, status="reserved",
                   expected="in_stock")
    assert status.status_code == 409
    direct = _run(machines.create_deal(mid, kind="sale", price_cents=100, buyer_name="X",
                                       created_by=BOSS))
    assert direct["ok"] is False and direct["request_id"] == rid
    deleted = _post(client, "/api/machines/delete", BOSS, machine_id=mid)
    assert deleted.status_code == 409
    assert _rows(db, "SELECT COUNT(*) AS n FROM machine_deal_requests")[0]["n"] == 1


def test_unique_index_is_the_last_line(isolated_db):
    """Если проверка под блокировкой однажды разойдётся, вторую живую заявку на
    машину не пустит сама база."""
    db = isolated_db
    _setup(db)
    mid = _machine()
    insert = ("INSERT INTO machine_deal_requests (machine_id, kind, status, buyer_name, machine_status, "
              "created_by, created_at, submitted_at, updated_at) VALUES (?, 'sale', ?, 'A', 'in_stock', 1, "
              "'t', 't', 't')")
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(insert, (mid, "pending"))
        cur.execute(insert, (mid, "rejected"))
        conn.commit()
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
            cur.execute(insert, (mid, "rework"))


def test_roles_create_and_decide(isolated_db, monkeypatch, sent):
    db = isolated_db
    _setup(db)
    mid = _machine()
    client = _client(monkeypatch)

    for uid in (GUEST, KEEPER, BOOK):
        assert _deal(client, uid, mid, kind="sale").status_code == 403, uid
        assert _post(client, "/api/machines/deals/pending", uid).status_code == 403, uid
    rid = _deal(client, MGR, mid, kind="sale").json()["request_id"]
    for uid in (GUEST, KEEPER, BOOK):
        assert _decide(client, uid, "approve", rid).status_code == 403, uid
    # Менеджер (он же бухгалтер и кладовщик по совмещению) при живом
    # руководителе не одобряет ни свою, ни чужую заявку.
    for uid in (MGR, MGR2):
        assert _decide(client, uid, "approve", rid).status_code == 403
        assert _decide(client, uid, "reject", rid).status_code == 403
        assert _decide(client, uid, "rework", rid, reason="так").status_code == 403
    assert _rows(db, "SELECT status FROM machine_deal_requests")[0]["status"] == "pending"


def test_without_boss_manager_decides_and_it_is_marked(isolated_db, monkeypatch, sent):
    """Руководителя в системе нет (сейчас так и есть): карточка приходит
    менеджеру, решает он сам — с пометкой на экране, в ответе и в аудите."""
    db = isolated_db
    _setup(db, boss=False)
    mid = _machine()
    client = _client(monkeypatch)

    rid = _deal(client, MGR, mid).json()["request_id"]
    recipients = {chat for chat, _t, _m in sent}
    assert recipients == {MGR, MGR2}
    assert "Руководителя в системе нет" in sent[0][1]
    view = _post(client, "/api/machines/deals/pending", MGR).json()
    assert view["can_decide"] is True and "руководителя в системе нет" in view["decide_hint"]

    res = _decide(client, MGR, "approve", rid)
    assert res.status_code == 200, res.text
    assert res.json()["self_approved"] is True and res.json()["approval_mode"] == "no_boss"
    audit = [a for a in _run(db.get_audit_log(limit=50)) if a["action"] == "machine_deal_approved"]
    assert "руководителя в системе нет" in audit[0]["details"]
    assert all(chat != MGR or "одобрена" not in text for chat, text, _m in sent), "сам себе итог не шлём"


def test_approve_is_idempotent_by_key(isolated_db, monkeypatch, sent):
    db = isolated_db
    _setup(db)
    mid = _machine()
    client = _client(monkeypatch)
    rid = _deal(client, MGR, mid).json()["request_id"]

    body = {"request_id": rid, "idempotency_key": "same"}
    first = _post(client, "/api/machines/deals/approve", BOSS, **body)
    second = _post(client, "/api/machines/deals/approve", BOSS, **body)
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    assert _rows(db, "SELECT COUNT(*) AS n FROM machine_deals")[0]["n"] == 1
    assert len([1 for chat, text, _m in sent if chat == MGR and "одобрена" in text]) == 1


def test_credit_requires_passport_and_valid_terms(isolated_db, monkeypatch, sent):
    db = isolated_db
    _setup(db)
    mid = _machine()
    client = _client(monkeypatch)
    no_passport = _deal(client, MGR, mid, buyer_passport="")
    assert no_passport.status_code == 400 and "паспорт" in no_passport.json()["detail"]
    full_down = _deal(client, MGR, mid, down_payment="24 000")
    assert full_down.status_code == 400
    zero_down = _deal(client, MGR, mid, down_payment="0")
    assert zero_down.status_code == 200, zero_down.text
    assert _rows(db, "SELECT down_payment_cents, months FROM machine_deal_requests") == [
        {"down_payment_cents": 0, "months": 4}]


def test_sold_machine_refuses_request(isolated_db, monkeypatch, sent):
    db = isolated_db
    _setup(db)
    mid = _machine(status="sold")
    client = _client(monkeypatch)
    r = _deal(client, MGR, mid, kind="sale")
    assert r.status_code == 409 and "сделка невозможна" in r.json()["detail"]
    assert _post(client, "/api/machines/card", MGR, machine_id=mid).json()["can_request"] == []


def test_deleting_machine_takes_finished_requests_along(isolated_db, monkeypatch, sent):
    from services import machines

    db = isolated_db
    _setup(db)
    mid = _machine()
    client = _client(monkeypatch)
    rid = _deal(client, MGR, mid, kind="sale").json()["request_id"]
    assert _decide(client, BOSS, "reject", rid).status_code == 200
    assert _run(machines.delete_machine(mid, user_id=BOSS))["ok"]
    assert _rows(db, "SELECT COUNT(*) AS n FROM machine_deal_requests")[0]["n"] == 0


# ─── Деньги по рассрочке и снятие брони — работа менеджера ───────────────────


def _approved_credit(client, db):
    mid = _machine()
    rid = _deal(client, MGR, mid).json()["request_id"]
    assert _decide(client, BOSS, "approve", rid).status_code == 200
    deal_id = _rows(db, "SELECT id FROM machine_deals")[0]["id"]
    return mid, int(deal_id)


def test_manager_records_installment_money_with_method(isolated_db, monkeypatch, sent):
    db = isolated_db
    _setup(db)
    client = _client(monkeypatch)
    mid, deal_id = _approved_credit(client, db)

    no_method = _post(client, "/api/machines/receipt", MGR, deal_id=deal_id, amount="5000",
                      idempotency_key="r0")
    assert no_method.status_code == 400 and "способ" in no_method.json()["detail"].lower()
    assert _post(client, "/api/machines/receipt", MGR, deal_id=deal_id, amount="5000",
                 method="barter", idempotency_key="r00").status_code == 400
    assert _post(client, "/api/machines/receipt", KEEPER, deal_id=deal_id, amount="5000",
                 method="cash", idempotency_key="rk").status_code == 403

    # «На карту» — только с картой справочника: чья карта, руководитель сверяет банк.
    no_account = _post(client, "/api/machines/receipt", MGR, deal_id=deal_id, amount="5000",
                       method="card", idempotency_key="r01")
    assert no_account.status_code == 400 and "на какую карту" in no_account.json()["detail"]
    from tests.conftest import pay_account_id

    bank_id = pay_account_id("bank")
    wrong = _post(client, "/api/machines/receipt", MGR, deal_id=deal_id, amount="5000",
                  method="card", account_id=bank_id, idempotency_key="r02")
    assert wrong.status_code == 400 and "выберите карту" in wrong.json()["detail"]
    ok = _post(client, "/api/machines/receipt", MGR, deal_id=deal_id, amount="5000",
               method="card", account_id=pay_account_id("card"), idempotency_key="r1")
    assert ok.status_code == 200, ok.text
    card = _post(client, "/api/machines/card", MGR, machine_id=mid).json()
    deal = card["deals"][0]
    assert deal["can_record"] is True and deal["can_undo"] is False
    assert [r["method"] for r in deal["receipts"]] == ["card"]
    assert deal["receipts"][0]["account_label"] == "на карту •••• 1234 (Фаридун М.)"
    assert not any(k.startswith("acc_") for k in deal["receipts"][0])
    assert deal["progress"]["left_cents"] == 1_500_000

    # Плановый платёж «оплачен» кнопкой — тоже менеджер; способ необязателен.
    second = [p for p in deal["payments"] if p["seq"] == 2][0]
    no_bank = _post(client, "/api/machines/payment", MGR, payment_id=second["id"], paid=True,
                    method="bank", idempotency_key="p0")
    assert no_bank.status_code == 400 and "на какой счёт" in no_bank.json()["detail"]
    paid = _post(client, "/api/machines/payment", MGR, payment_id=second["id"], paid=True,
                 method="cash", idempotency_key="p1")
    assert paid.status_code == 200, paid.text
    audit = [a["details"] for a in _run(db.get_audit_log(limit=50)) if a["action"] == "machine_receipt_added"]
    assert any("на карту •••• 1234 (Фаридун М.)" in d for d in audit) and any("наличные" in d for d in audit)

    # Стереть деньги при живом руководителе менеджер не может — ни удалением, ни снятием отметки.
    receipt_id = deal["receipts"][0]["id"]
    assert _post(client, "/api/machines/receipt_delete", MGR, receipt_id=receipt_id).status_code == 403
    assert _post(client, "/api/machines/payment", MGR, payment_id=second["id"], paid=False,
                 idempotency_key="p2").status_code == 403
    assert _post(client, "/api/machines/receipt_delete", BOSS, receipt_id=receipt_id).status_code == 200
    assert _rows(db, "SELECT COUNT(*) AS n FROM machine_receipt_methods WHERE receipt_id = ?",
                 (receipt_id,))[0]["n"] == 0
    assert _rows(db, "SELECT COUNT(*) AS n FROM machine_receipt_accounts WHERE receipt_id = ?",
                 (receipt_id,))[0]["n"] == 0


def test_without_boss_manager_deletes_receipt_with_note(isolated_db, monkeypatch, sent):
    from services import machines

    db = isolated_db
    _setup(db, boss=False)
    client = _client(monkeypatch)
    mid = _machine()
    rid = _deal(client, MGR, mid).json()["request_id"]
    assert _decide(client, MGR, "approve", rid).status_code == 200
    deal_id = int(_rows(db, "SELECT id FROM machine_deals")[0]["id"])
    assert _run(machines.add_receipt(deal_id, 100_000, user_id=MGR, method="bank"))["ok"]
    receipt_id = _rows(db, "SELECT id FROM machine_payment_receipts")[0]["id"]
    card = _post(client, "/api/machines/card", MGR, machine_id=mid).json()
    assert card["deals"][0]["can_undo"] is True
    assert _post(client, "/api/machines/receipt_delete", MGR, receipt_id=receipt_id).status_code == 200
    notes = [a["details"] for a in _run(db.get_audit_log(limit=50)) if a["action"] == "machine_receipt_deleted"]
    assert any("руководителя в системе нет" in n for n in notes)


def test_manager_unbooks_own_booking_only(isolated_db, monkeypatch, sent):
    db = isolated_db
    _setup(db)
    client = _client(monkeypatch)
    mine, other = _machine("B-1"), _machine("B-2")
    rid = _deal(client, MGR, mine, kind="reserve", price="", buyer_passport="").json()["request_id"]
    assert _decide(client, BOSS, "approve", rid).status_code == 200
    assert _post(client, "/api/machines/status", BOSS, machine_id=other, status="reserved",
                 expected="in_stock").status_code == 200

    assert _post(client, "/api/machines/card", MGR, machine_id=mine).json()["can_unreserve"] is True
    assert _post(client, "/api/machines/card", MGR, machine_id=other).json()["can_unreserve"] is False
    assert _post(client, "/api/machines/card", MGR2, machine_id=mine).json()["can_unreserve"] is False
    assert _post(client, "/api/machines/unreserve", MGR, machine_id=other).status_code == 403
    assert _post(client, "/api/machines/unreserve", MGR2, machine_id=mine).status_code == 403
    assert _post(client, "/api/machines/unreserve", KEEPER, machine_id=mine).status_code == 403
    # Прочие ручные переходы — по-прежнему руководству.
    assert _post(client, "/api/machines/status", MGR, machine_id=mine, status="in_stock",
                 expected="reserved").status_code == 403

    r = _post(client, "/api/machines/unreserve", MGR, machine_id=mine, idempotency_key="u1")
    assert r.status_code == 200 and r.json()["mode"] == "own", r.text
    assert _rows(db, "SELECT status FROM machines WHERE id = ?", (mine,)) == [{"status": "in_stock"}]
    assert _rows(db, "SELECT status FROM machine_deal_requests WHERE id = ?", (rid,)) == [{"status": "released"}]
    assert _post(client, "/api/machines/unreserve", MGR, machine_id=mine).status_code == 409

    # Бронь снята, руководитель забронировал снова сам — старая бронь менеджера
    # права не даёт.
    assert _post(client, "/api/machines/status", BOSS, machine_id=mine, status="reserved",
                 expected="in_stock").status_code == 200
    assert _post(client, "/api/machines/unreserve", MGR, machine_id=mine).status_code == 403
    assert _post(client, "/api/machines/unreserve", BOSS, machine_id=mine).status_code == 200


def test_boss_unbooking_releases_manager_booking(isolated_db, monkeypatch, sent):
    db = isolated_db
    _setup(db)
    client = _client(monkeypatch)
    mid = _machine()
    rid = _deal(client, MGR, mid, kind="reserve", price="", buyer_passport="").json()["request_id"]
    assert _decide(client, BOSS, "approve", rid).status_code == 200
    assert _post(client, "/api/machines/status", BOSS, machine_id=mid, status="in_stock",
                 expected="reserved").status_code == 200
    assert _rows(db, "SELECT status FROM machine_deal_requests") == [{"status": "released"}]


def test_without_boss_manager_unbooks_any_booking(isolated_db, monkeypatch, sent):
    from services import machines

    db = isolated_db
    _setup(db, boss=False)
    client = _client(monkeypatch)
    mid = _machine(status="reserved")
    assert _post(client, "/api/machines/card", MGR2, machine_id=mid).json()["can_unreserve"] is True
    r = _post(client, "/api/machines/unreserve", MGR2, machine_id=mid)
    assert r.status_code == 200 and r.json()["mode"] == "no_boss"
    assert _run(machines.get_machine(mid))["status"] == "in_stock"
    notes = [a["details"] for a in _run(db.get_audit_log(limit=50)) if a["action"] == "machine_unreserved"]
    assert notes and "руководителя в системе нет" in notes[0]


# ─── Бот: карточка решения ────────────────────────────────────────────────────


class _User:
    def __init__(self, uid, first_name="Фаридун"):
        self.id = uid
        self.first_name = first_name
        self.full_name = first_name


class _Chat:
    id = 77


class _Msg:
    def __init__(self, text, markup):
        self.text = text
        self.html_text = text
        self.reply_markup = markup
        self.chat = _Chat()
        self.message_id = 11
        self.edited = None
        self.answers = []

    async def edit_text(self, text, **kw):
        self.edited = text
        self.reply_markup = kw.get("reply_markup")

    async def edit_reply_markup(self, reply_markup=None):
        self.reply_markup = reply_markup

    async def answer(self, text, **kw):
        self.answers.append((text, kw))
        return _Msg(text, kw.get("reply_markup"))


class _Call:
    def __init__(self, data, uid, message):
        self.data = data
        self.from_user = _User(uid)
        self.message = message
        self.alerts = []

    async def answer(self, text="", **kw):
        self.alerts.append((text, kw))


class _State:
    def __init__(self):
        self.data, self.state = {}, None

    async def set_state(self, st):
        self.state = st

    async def update_data(self, **kw):
        self.data.update(kw)

    async def get_data(self):
        return dict(self.data)

    async def get_state(self):
        return getattr(self.state, "state", self.state)

    async def clear(self):
        self.data, self.state = {}, None


class _Bot:
    def __init__(self):
        self.markups, self.sent = [], []

    async def edit_message_reply_markup(self, chat_id, message_id, reply_markup=None):
        self.markups.append((chat_id, message_id, reply_markup))

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text, kw))


def _buttons(markup):
    return [b for row in (markup.inline_keyboard if markup else []) for b in row]


def _submit_as_manager(mid):
    from services import machine_deal_requests as mdr

    res = _run(mdr.submit(mid, kind="sale", actor_id=MGR, actor_name="Manager", actor_role="manager",
                          price_cents=2_000_000, buyer_name="ООО Карьер", notify=False))
    assert res["ok"], res
    return res["request_id"]


def test_bot_approve_turns_buttons_into_disabled_outcome(isolated_db, sent):
    from handlers import machines as h
    from utils.keyboards import machine_request_keyboard

    db = isolated_db
    _setup(db)
    roles.invalidate_all_roles()
    mid = _machine()
    rid = _submit_as_manager(mid)
    call = _Call(f"mdr_ok:{rid}", BOSS, _Msg("карточка", machine_request_keyboard(rid)))

    _run(h.cb_machine_request_approve(call))
    buttons = _buttons(call.message.reply_markup)
    assert not any(b.callback_data and b.callback_data.startswith("mdr_") for b in buttons)
    outcome = [b for b in buttons if b.disabled is not None]
    assert outcome and outcome[0].text.startswith("✅ Одобрена · Фаридун")
    assert "Заявка одобрена" in call.message.edited
    assert _rows(db, "SELECT status FROM machines WHERE id = ?", (mid,)) == [{"status": "sold"}]


def test_bot_stale_card_is_settled_not_decided_twice(isolated_db, sent):
    from handlers import machines as h
    from services import machine_deal_requests as mdr
    from utils.keyboards import machine_request_keyboard

    db = isolated_db
    _setup(db)
    roles.invalidate_all_roles()
    mid = _machine()
    rid = _submit_as_manager(mid)
    # Решили в WebApp; копия карточки в чате осталась с живыми кнопками.
    assert _run(mdr.reject(rid, actor_id=BOSS, actor_name="Boss", actor_role="boss"))["ok"]

    for data, handler in ((f"mdr_ok:{rid}", h.cb_machine_request_approve),
                          (f"mdr_no:{rid}", h.cb_machine_request_reject)):
        call = _Call(data, BOSS, _Msg("карточка", machine_request_keyboard(rid)))
        _run(handler(call))
        assert call.alerts and call.alerts[0][1].get("show_alert")
        buttons = _buttons(call.message.reply_markup)
        assert [b.text for b in buttons if b.disabled is not None] == ["❌ Заявка уже отклонена"]
        assert not any(b.callback_data and b.callback_data.startswith("mdr_") for b in buttons)

    state = _State()
    call = _Call(f"mdr_rw:{rid}", BOSS, _Msg("карточка", machine_request_keyboard(rid)))
    _run(h.cb_machine_request_rework(call, state))
    assert state.state is None, "по устаревшей карточке ввод причины не начинается"
    assert _rows(db, "SELECT COUNT(*) AS n FROM machine_deals")[0]["n"] == 0


def test_bot_rework_asks_reason_with_force_reply_and_settles(isolated_db, sent):
    from handlers import machines as h
    from utils.keyboards import machine_request_keyboard

    db = isolated_db
    _setup(db)
    roles.invalidate_all_roles()
    mid = _machine()
    rid = _submit_as_manager(mid)
    state, bot = _State(), _Bot()
    card = _Msg("карточка", machine_request_keyboard(rid))
    call = _Call(f"mdr_rw:{rid}", BOSS, card)

    _run(h.cb_machine_request_rework(call, state))
    assert state.state == h.MachineRework.waiting_for_reason
    [(question, kw)] = card.answers
    assert kw["reply_markup"].force_reply is True
    assert [b.text for b in _buttons(card.reply_markup) if b.disabled is not None] == [
        "✍️ Ждём причину доработки…"]

    class _Reply:
        from_user = _User(BOSS)
        text = "скидка слишком большая"

        async def answer(self, *a, **kw):
            pass

    _run(h.process_machine_request_rework(_Reply(), state, bot))
    assert _rows(db, "SELECT status, decision_note FROM machine_deal_requests") == [
        {"status": "rework", "decision_note": "скидка слишком большая"}]
    settled = [m for _c, mid_, m in bot.markups if mid_ == card.message_id and m is not None]
    assert settled and settled[-1].inline_keyboard[0][0].text.startswith("↩️ На доработку")
    assert any(chat == MGR and "доработке" in text for chat, text, _m in sent)


def test_bot_manager_cannot_decide_when_boss_exists(isolated_db, sent):
    from handlers import machines as h
    from utils.keyboards import machine_request_keyboard

    db = isolated_db
    _setup(db)
    roles.invalidate_all_roles()
    mid = _machine()
    rid = _submit_as_manager(mid)
    call = _Call(f"mdr_ok:{rid}", MGR, _Msg("карточка", machine_request_keyboard(rid)))
    _run(h.cb_machine_request_approve(call))
    assert "руководитель" in call.alerts[0][0]
    # Заявка ещё ждёт — кнопки на карточке не трогаем.
    assert [b.callback_data for b in _buttons(call.message.reply_markup)] == [
        f"mdr_ok:{rid}", f"mdr_no:{rid}", f"mdr_rw:{rid}"]


# ─── Связки: «Решения», очередь «Сегодня», политика уведомлений ─────────────


def test_pending_deal_is_in_boss_today_queue_pointing_to_decisions(isolated_db, monkeypatch, sent):
    """Заявка на сделку по технике — пункт очереди «Сегодня» руководителя с
    адресом `decisions`: по пунктам с этим адресом фронт считает общий бейдж
    «Решений». Доработка ждёт менеджера — из очереди руководителя уходит."""
    from services import work_queue

    db = isolated_db
    _setup(db)
    client = _client(monkeypatch)
    rid = _deal(client, MGR, _machine()).json()["request_id"]
    _deal(client, MGR, _machine(vin="JCB-002"), kind="sale")

    boss = {i["key"]: i for i in _run(work_queue.gather(BOSS, "boss"))}
    assert boss["machine_deals"]["count"] == 2
    assert boss["machine_deals"]["screen"] == work_queue.DECISIONS_SCREEN == "decisions"
    mgr = {i["key"] for i in _run(work_queue.gather(MGR, "manager"))}
    assert "machine_deals" not in mgr

    assert _decide(client, BOSS, "rework", rid, reason="паспорт").status_code == 200
    boss = {i["key"]: i for i in _run(work_queue.gather(BOSS, "boss"))}
    assert boss["machine_deals"]["count"] == 1


def test_decision_card_goes_through_notify_policy(isolated_db, monkeypatch, sent):
    """Карточка решения спрашивает `notify_policy.should_notify_now(
    MACHINE_DEAL_APPROVAL)` — единая точка правила «сразу или в дайджест»."""
    from services import notify_policy

    db = isolated_db
    _setup(db)
    client = _client(monkeypatch)
    asked: list[str] = []
    real = notify_policy.should_notify_now

    def spy(kind, *a, **kw):
        asked.append(kind)
        return real(kind, *a, **kw)

    monkeypatch.setattr(notify_policy, "should_notify_now", spy)
    _deal(client, MGR, _machine())
    assert asked == [notify_policy.MACHINE_DEAL_APPROVAL]
    assert [chat for chat, _t, _m in sent] == [BOSS], "одобрение — всегда сразу"

    monkeypatch.setattr(notify_policy, "should_notify_now", lambda kind, *a, **kw: False)
    sent.clear()
    _deal(client, MGR, _machine(vin="JCB-002"), kind="sale")
    assert sent == []


def test_manager_does_not_see_colleague_buyer_contacts(isolated_db, monkeypatch, sent):
    """Телефон и комментарий о покупателе в чужой заявке — не менеджеру, который
    её не решает. Свою заявку автор видит целиком, руководство — всё."""
    db = isolated_db
    _setup(db)
    mid = _machine()
    client = _client(monkeypatch)
    rid = _deal(client, MGR, mid, buyer_note="звонить после 18").json()["request_id"]

    other = _post(client, "/api/machines/deals/pending", MGR2).json()
    req = [r for r in other["requests"] if r["id"] == rid][0]
    assert "buyer_phone" not in req and "buyer_note" not in req and "buyer_passport" not in req
    mine = _post(client, "/api/machines/deals/pending", MGR).json()
    req = [r for r in mine["requests"] if r["id"] == rid][0]
    assert req["buyer_phone"] == "+998901112233" and req["buyer_note"] == "звонить после 18"
    boss = _post(client, "/api/machines/deals/pending", BOSS).json()
    req = [r for r in boss["requests"] if r["id"] == rid][0]
    assert req["buyer_phone"] and req["buyer_passport"]
    # Карточка машины — та же резка.
    card = _post(client, "/api/machines/card", MGR2, machine_id=mid).json()
    assert card["request"]["id"] == rid
    assert "buyer_phone" not in (card.get("request") or {})


def test_manager_deciding_without_boss_sees_contacts(isolated_db, monkeypatch, sent):
    db = isolated_db
    _setup(db, boss=False)
    mid = _machine()
    client = _client(monkeypatch)
    rid = _deal(client, MGR, mid).json()["request_id"]
    view = _post(client, "/api/machines/deals/pending", MGR2).json()
    req = [r for r in view["requests"] if r["id"] == rid][0]
    assert view["can_decide"] is True and req["buyer_phone"] == "+998901112233"
