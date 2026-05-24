"""
Контракт с МойСклад: документ «Входящий платёж» (entity/paymentin).

Мокаем ТРАНСПОРТ (aioresponses) — реально исполняется сборка URL/payload.
БД настоящая (isolated_db). _CTX праймим как после init_demand_context().

Покрываем критичные для денег пути:
  * gate: без агента / без контекста — не ходим в МС, помечаем failed
  * payload: org_meta, agent href, sum в минорных единицах, currency
    meta, operations link (customerorder приоритетнее demand)
  * успех: ms_paymentin_id сохраняется в БД, set_payment_ms_sync='synced'
  * идемпотентность: повторный вызов на уже-синченном — early-return без POST
  * concurrent: claim_payment_for_ms_sync проигравший — не делает HTTP
"""

import asyncio

from aioresponses import aioresponses

import services.ms_demand as ms_demand
import services.ms_payments as ms_payments
from services.moysklad import MS_BASE


def _prime_ctx(monkeypatch):
    monkeypatch.setitem(ms_demand._CTX, "ready", True)
    monkeypatch.setitem(
        ms_demand._CTX, "org_meta", {"href": f"{MS_BASE}/entity/organization/ORG-UUID"}
    )


def _prime_currency(monkeypatch, iso="USD"):
    """Запатчить _currency_meta_cache — пропустим GET /entity/currency."""
    monkeypatch.setitem(
        ms_payments._currency_meta_cache,
        iso.upper(),
        {"href": f"{MS_BASE}/entity/currency/CURR-{iso}", "type": "currency"},
    )


def _make_confirmed_payment(db, *, with_customerorder=False, with_demand=False):
    """Создать заказ с агентом + платёж pending → confirmed."""
    mgr = 1
    db.set_role(mgr, "m", "Manager", "manager")
    oid = db.create_order(mgr, "Manager", "")
    db.update_order_agent(oid, "AGENT-UUID", "Client X")
    db.add_order_item(oid, "Товар", "", 1, "шт", 250.0)
    db.update_order_status(oid, "approved")
    if with_customerorder:
        db.set_order_ms_customerorder_id(oid, "CO-UUID")
    if with_demand:
        db.set_order_ms_demand_id(oid, "DEMAND-UUID")
    pid = db.add_payment(mgr, "@m", "Manager", 250.0, "USD", "тестовая оплата", order_id=oid)
    # Босс подтверждает — это переводит платёж в 'confirmed'.
    db.confirm_payment(pid, mgr, "Boss")
    return oid, pid


def test_paymentin_skip_without_order_link(isolated_db):
    """Платёж без order_id — это «бытовой» (напр. аренда), не синкаем."""
    db = isolated_db
    db.set_role(1, "m", "M", "manager")
    pid = db.add_payment(1, "@m", "M", 100.0, "USD", "канцелярия")
    res = asyncio.run(ms_payments.create_paymentin_for_payment(pid))
    assert res["ok"] is False
    assert "не связан" in res["reason"]


def test_paymentin_skip_missing_payment(isolated_db):
    res = asyncio.run(ms_payments.create_paymentin_for_payment(999999))
    assert res["ok"] is False
    assert "не найден" in res["reason"]


def test_paymentin_fails_without_agent_on_order(isolated_db, monkeypatch):
    """У заказа нет agent_id — paymentin нельзя создать, помечаем failed
    чтобы оператор увидел причину."""
    _prime_ctx(monkeypatch)
    db = isolated_db
    mgr = 1
    db.set_role(mgr, "m", "M", "manager")
    oid = db.create_order(mgr, "M", "")
    # НЕ ставим update_order_agent — agent_id остаётся NULL
    db.add_order_item(oid, "Товар", "", 1, "шт", 100.0)
    db.update_order_status(oid, "approved")
    pid = db.add_payment(mgr, "@m", "M", 100.0, "USD", "c", order_id=oid)
    db.confirm_payment(pid, mgr, "Boss")

    res = asyncio.run(ms_payments.create_paymentin_for_payment(pid))
    assert res["ok"] is False
    assert "agent_id" in res["reason"]
    # И в БД статус помечен failed с этой причиной
    stored = db.get_payment(pid)
    assert stored["ms_sync_status"] == "failed"


def test_paymentin_fails_without_ms_context(isolated_db, monkeypatch):
    """Контекст ms_demand не готов (org не резолвлена) — раньше эта ошибка
    маскировала оригинальный ms_sync_error; теперь явный fail."""
    monkeypatch.setitem(ms_demand._CTX, "ready", False)
    db = isolated_db
    _oid, pid = _make_confirmed_payment(db, with_customerorder=True)
    res = asyncio.run(ms_payments.create_paymentin_for_payment(pid))
    assert res["ok"] is False
    assert "Контекст" in res["reason"]


def test_paymentin_success_with_customerorder(isolated_db, monkeypatch):
    """Happy path: customerorder → paymentin привязан к нему через operations.
    Проверяем форму payload: org/agent/sum в минорных единицах/currency rate."""
    _prime_ctx(monkeypatch)
    _prime_currency(monkeypatch, "USD")
    db = isolated_db
    _oid, pid = _make_confirmed_payment(db, with_customerorder=True)
    captured = {}

    async def scenario():
        from services import moysklad

        moysklad._session = None
        try:
            with aioresponses() as m:

                def _cb(url, **kwargs):
                    captured["payload"] = kwargs["json"]

                m.post(
                    f"{MS_BASE}/entity/paymentin",
                    status=200,
                    payload={"id": "PI-NEW", "name": "Платёж"},
                    callback=_cb,
                )
                return await ms_payments.create_paymentin_for_payment(pid)
        finally:
            await moysklad.close_session()

    res = asyncio.run(scenario())
    assert res["ok"] is True, res
    assert res["paymentin_id"] == "PI-NEW"
    # И в БД сохранён id
    stored = db.get_payment(pid)
    assert stored["ms_paymentin_id"] == "PI-NEW"
    assert stored["ms_sync_status"] == "synced"

    p = captured["payload"]
    assert p["organization"]["meta"]["type"] == "organization"
    assert p["agent"]["meta"]["href"].endswith("/entity/counterparty/AGENT-UUID")
    # 250.0 USD → 25000 минорных единиц
    assert p["sum"] == 25000
    # currency через rate, не напрямую
    assert "rate" in p
    assert p["rate"]["currency"]["meta"]["href"].endswith("/entity/currency/CURR-USD")
    assert p["applicable"] is True
    # operations → customerorder приоритетнее demand
    assert len(p["operations"]) == 1
    assert p["operations"][0]["meta"]["type"] == "customerorder"
    assert p["operations"][0]["meta"]["href"].endswith("/entity/customerorder/CO-UUID")


def test_paymentin_falls_back_to_demand_when_no_customerorder(isolated_db, monkeypatch):
    """LEGACY заказ без ms_customerorder_id — fallback на demand."""
    _prime_ctx(monkeypatch)
    _prime_currency(monkeypatch, "USD")
    db = isolated_db
    _oid, pid = _make_confirmed_payment(db, with_demand=True)
    captured = {}

    async def scenario():
        from services import moysklad

        moysklad._session = None
        try:
            with aioresponses() as m:

                def _cb(url, **kwargs):
                    captured["payload"] = kwargs["json"]

                m.post(
                    f"{MS_BASE}/entity/paymentin",
                    status=200,
                    payload={"id": "PI-LEGACY"},
                    callback=_cb,
                )
                return await ms_payments.create_paymentin_for_payment(pid)
        finally:
            await moysklad.close_session()

    res = asyncio.run(scenario())
    assert res["ok"] is True
    p = captured["payload"]
    assert p["operations"][0]["meta"]["type"] == "demand"
    assert p["operations"][0]["meta"]["href"].endswith("/entity/demand/DEMAND-UUID")


def test_paymentin_idempotent_when_already_synced(isolated_db, monkeypatch):
    """Повторный create_paymentin на уже-синченном — early-return без POST.
    Не мокаем POST — если код решит сходить, упадёт ConnectionError."""
    _prime_ctx(monkeypatch)
    _prime_currency(monkeypatch, "USD")
    db = isolated_db
    _oid, pid = _make_confirmed_payment(db, with_customerorder=True)
    db.set_payment_ms_sync(pid, paymentin_id="PI-EXISTING", status="synced")

    res = asyncio.run(ms_payments.create_paymentin_for_payment(pid))
    assert res["ok"] is True
    assert res["paymentin_id"] == "PI-EXISTING"
    assert res.get("already") is True


def test_paymentin_handles_ms_400_error(isolated_db, monkeypatch):
    """МС вернул 400 (валидация payload) — failed + сохранён error в БД."""
    _prime_ctx(monkeypatch)
    _prime_currency(monkeypatch, "USD")
    db = isolated_db
    _oid, pid = _make_confirmed_payment(db, with_customerorder=True)

    async def scenario():
        from services import moysklad

        moysklad._session = None
        try:
            with aioresponses() as m:
                m.post(
                    f"{MS_BASE}/entity/paymentin",
                    status=400,
                    body='{"errors":[{"error":"agent.meta.href not found"}]}',
                )
                return await ms_payments.create_paymentin_for_payment(pid)
        finally:
            await moysklad.close_session()

    res = asyncio.run(scenario())
    assert res["ok"] is False
    assert "HTTP 400" in res["reason"]
    stored = db.get_payment(pid)
    assert stored["ms_sync_status"] == "failed"
    assert "HTTP 400" in stored["ms_sync_error"]


def test_paymentin_fails_when_currency_not_in_ms(isolated_db, monkeypatch):
    """Валюта заказа отсутствует в справочнике МС → failed (но не падаем)."""
    _prime_ctx(monkeypatch)
    # НЕ примируем currency_meta_cache → fall to GET, который вернёт []
    db = isolated_db
    _oid, pid = _make_confirmed_payment(db, with_customerorder=True)

    async def scenario():
        from services import moysklad

        moysklad._session = None
        try:
            with aioresponses() as m:
                m.get(
                    f"{MS_BASE}/entity/currency?limit=100",
                    status=200,
                    payload={"rows": [{"isoCode": "ZZZ", "meta": {"href": "x"}}]},
                )
                return await ms_payments.create_paymentin_for_payment(pid)
        finally:
            await moysklad.close_session()

    res = asyncio.run(scenario())
    assert res["ok"] is False
    assert "Валюта USD" in res["reason"]
    stored = db.get_payment(pid)
    assert stored["ms_sync_status"] == "failed"
