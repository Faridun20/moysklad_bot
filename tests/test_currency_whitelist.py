"""Белый список валют (`config.ALLOWED_CURRENCIES`) на технике и накладных.

Без него в карточку машины, заявку на сделку и накладную уезжала любая строка
(в том числе разметка), курс для неё не находился, а бот и WebApp печатали её
как есть. Проверяем сервис и ручки; вывод — `handlers.machines._money`
экранирует валюту (фронт — `formatMoney`, vitest).
"""

from __future__ import annotations

from tests.test_machine_deal_requests import (  # noqa: F401
    BOSS, MGR, _client, _deal, _decide, _machine, _post, _rows, _run, _setup, sent,
)
from tests.test_warehouse_api import _incoming, api  # noqa: F401

BAD = "<b>X</b>"


def test_machine_create_and_update_refuse_unknown_currency(isolated_db, monkeypatch, sent):  # noqa: F811
    from services import machines

    db = isolated_db
    _setup(db)
    res = _run(machines.create_machine(vin="BAD-CUR", name="JCB", created_by=BOSS, currency="EUR"))
    assert not res["ok"] and "Валюта не поддерживается" in res["error"]

    client = _client(monkeypatch)
    r = _post(client, "/api/machines/create", MGR, vin="BAD-CUR2", name="JCB", currency=BAD)
    assert r.status_code == 400, r.text
    assert "Валюта не поддерживается" in r.json()["detail"]
    assert _rows(db, "SELECT COUNT(*) AS n FROM machines")[0]["n"] == 0

    ok = _post(client, "/api/machines/create", MGR, vin="GOOD-CUR", name="JCB", currency="uzs")
    assert ok.status_code == 200, ok.text
    mid = ok.json()["machine_id"]
    assert _rows(db, "SELECT currency FROM machines WHERE id = ?", (mid,))[0]["currency"] == "UZS"

    # Отказ по валюте не оставляет карточку изменённой наполовину (VIN цел).
    up = _post(client, "/api/machines/update", BOSS, machine_id=mid,
               fields={"vin": "NEW-VIN", "currency": BAD})
    assert up.status_code == 400, up.text
    assert _rows(db, "SELECT vin, currency FROM machines WHERE id = ?", (mid,))[0] == {
        "vin": "GOODCUR", "currency": "UZS"}
    res = _run(machines.update_machine_fields(mid, user_id=BOSS, currency="RUB"))
    assert not res["ok"] and "Валюта" in res["error"]
    fine = _post(client, "/api/machines/update", BOSS, machine_id=mid, fields={"currency": "usd"})
    assert fine.status_code == 200, fine.text
    assert _rows(db, "SELECT currency FROM machines WHERE id = ?", (mid,))[0]["currency"] == "USD"


def test_deal_requests_and_direct_deal_refuse_unknown_currency(isolated_db, monkeypatch, sent):  # noqa: F811
    from services import machines

    db = isolated_db
    _setup(db)
    mid = _machine()
    client = _client(monkeypatch)
    for kind in ("reserve", "sale", "credit"):
        r = _deal(client, MGR, mid, kind=kind, currency=BAD)
        assert r.status_code == 400, (kind, r.text)
        assert "Валюта не поддерживается" in r.json()["detail"]
    assert _rows(db, "SELECT COUNT(*) AS n FROM machine_deal_requests")[0]["n"] == 0

    err = _run(machines.prepare_deal(kind="sale", price_cents=100_000, buyer_name="Азиз",
                                     currency="EUR"))
    assert "Валюта не поддерживается" in err

    rid = _deal(client, MGR, mid, kind="sale", price="20 000").json()["request_id"]
    assert _decide(client, BOSS, "rework", rid, reason="цена").status_code == 200
    again = _decide(client, MGR, "resubmit", rid, price="21 000", currency=BAD)
    assert again.status_code == 400, again.text
    assert _rows(db, "SELECT status, currency FROM machine_deal_requests")[0] == {
        "status": "rework", "currency": "USD"}


def test_invoice_create_refuses_unknown_currency(api):  # noqa: F811
    client, db, ids = api
    from services import warehouse

    r = _incoming(client, ids["boss"], currency=BAD)
    assert r.status_code == 400, r.text
    assert "Валюта не поддерживается" in r.json()["detail"]
    res = _run(warehouse.create_invoice(
        invoice_type="incoming", warehouse_id=1, currency="EUR",
        items=[{"product_id": 1, "quantity": 1, "price_cents": None}],
    ))
    assert res == {"ok": False, "code": "bad_currency", "reason": res["reason"], "details": {}}
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute("SELECT COUNT(*) FROM invoices")
        assert cur.fetchone()[0] == 0
    ok = _incoming(client, ids["boss"], currency="uzs")
    assert ok.status_code == 200, ok.text


def test_bot_money_escapes_currency():
    from handlers.machines import _money

    assert _money(100_000, BAD) == "1 000 &lt;b&gt;X&lt;/b&gt;"
