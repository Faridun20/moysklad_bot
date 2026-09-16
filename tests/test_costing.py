"""Себестоимость (services/costing.py): партии FIFO, фиксация при отгрузке,
курсовая разница, отчёт руководства и права.

БД настоящая (isolated_db), корутины через asyncio.run. Сеть ЦБ — граница,
мокается транспортом (aioresponses).
"""

from __future__ import annotations

import asyncio
import importlib
from datetime import date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

import services.roles as roles

MGR, BOSS = 1, 2


def _run(coro):
    return asyncio.run(coro)


def _setup(db, *, enabled=True, uzs_now=13000.0):
    roles.invalidate_all_roles()
    db.set_role(MGR, "mgr", "Manager", "manager")
    db.set_role(BOSS, "boss", "Boss", "boss")
    if enabled:
        db.set_setting("accounting_enabled", True)
    ok, err = db.set_currency_rate("UZS", 1 / uzs_now, 0)
    assert ok, err
    # Архив ЦБ на сегодня — подсказка курса в карточке контейнера не идёт в
    # сеть (граница мокается только в тестах подсказки).
    today = date.today().strftime("%Y-%m-%d")
    db.set_currency_rate_daily("UZS", today, 1 / uzs_now, "cbu")
    db.set_currency_rate_daily("CNY", today, 1800 / uzs_now, "cbu")


def isolated_db_exec(db, sql, params=()):
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(db.q(sql), params)
        conn.commit()


def _product(name="Фильтр гидравлический"):
    from services import container_receipt

    return _run(container_receipt.create_product(name))["product_id"]


def _container(pid, qty, *, number="MSKU1000001", price=None, uzs_per_usd="12500"):
    """Прибывший, посчитанный и оприходованный контейнер (+ цена, если задана)."""
    from services import container_receipt, containers, costing

    cid = _run(containers.create_container(number=number, created_by=BOSS))["container_id"]
    item = _run(containers.add_item(cid, name="Фильтр", expected_qty=qty, product_id=pid))["item_id"]
    assert _run(containers.mark_arrived(cid, user_id=BOSS))["ok"]
    assert _run(containers.set_arrived_quantities(cid, {item: qty}, user_id=BOSS))["ok"]
    rec = _run(container_receipt.receive(cid, user_id=BOSS))
    assert rec["ok"], rec
    if price is not None:
        res = _run(costing.save_container_costing(
            cid, currency="USD", uzs_per_usd=uzs_per_usd, prices={item: price}, user_id=BOSS,
        ))
        assert res["ok"], res
    return cid, item, rec["invoice_id"]


def _sell(pid, qty, *, price_cents, currency="USD"):
    from services import warehouse

    wid = _run(warehouse.default_warehouse_id())
    res = _run(warehouse.create_invoice(
        invoice_type="outgoing", warehouse_id=wid, currency=currency,
        items=[{"product_id": pid, "quantity": qty, "price_cents": price_cents}],
    ))
    assert res["ok"], res
    return res["invoice_id"]


def _incoming(pid, qty, price_cents=None):
    from services import warehouse

    wid = _run(warehouse.default_warehouse_id())
    res = _run(warehouse.create_invoice(
        invoice_type="incoming", warehouse_id=wid,
        items=[{"product_id": pid, "quantity": qty, "price_cents": price_cents}],
    ))
    assert res["ok"], res
    return res["invoice_id"]


def _sale_rows(invoice_id):
    from services import adb_core

    return _run(adb_core.fetch(
        "SELECT batch_id, quantity, cost_base_cents, cost_source, ref_rate_to_base "
        "FROM sale_costs WHERE invoice_id = $1 ORDER BY id", invoice_id,
    ))


def _cost(invoice_id):
    return sum(int(r["cost_base_cents"]) for r in _sale_rows(invoice_id))


def _today():
    return date.today().strftime("%Y-%m-%d")


def _period():
    # Моменты, как их отдаёт `_resolve_analytics_period`: верхняя — полночь «до».
    start = datetime.combine(date.today(), datetime.min.time())
    return start - timedelta(days=1), start + timedelta(days=1)


# ─── Выключатель ─────────────────────────────────────────────────────────────


def test_switch_off_keeps_old_behavior(isolated_db):
    """Выключен — ни партий, ни фиксаций, приход контейнера без цены, как раньше."""
    from services import adb_core, costing

    _setup(isolated_db, enabled=False)
    pid = _product()
    cid, item, inv = _container(pid, 10)
    assert _run(costing.save_container_costing(
        cid, currency="USD", uzs_per_usd="12500", prices={item: "10"}, user_id=BOSS,
    ))["ok"] is False
    _sell(pid, 3, price_cents=2000)

    assert _run(adb_core.fetchval("SELECT COUNT(*) FROM cost_batches")) == 0
    assert _run(adb_core.fetchval("SELECT COUNT(*) FROM sale_costs")) == 0
    assert _run(adb_core.fetchval(
        "SELECT price_cents FROM invoice_items WHERE invoice_id = $1", inv)) is None
    assert _run(costing.container_card(cid)) == {"ok": True, "enabled": False}


def test_switch_reads_the_shared_setting_key(isolated_db):
    from services import costing

    _setup(isolated_db, enabled=False)
    assert _run(costing.is_enabled()) is False
    _run(costing.set_enabled(True, BOSS))
    assert isolated_db.get_setting("accounting_enabled") is True
    assert _run(costing.is_enabled()) is True


# ─── FIFO и фиксация ─────────────────────────────────────────────────────────


def test_fifo_takes_old_batch_first_and_past_sale_does_not_float(isolated_db):
    _setup(isolated_db)
    pid = _product()
    _container(pid, 10, number="AAA1", price="10")
    _container(pid, 10, number="BBB2", price="14")

    first = _sell(pid, 12, price_cents=2500)
    assert _cost(first) == 10 * 1000 + 2 * 1400

    # Новая, дорогая закупка прибыль прошлой продажи не меняет.
    _container(pid, 10, number="CCC3", price="20")
    assert _cost(first) == 12800

    second = _sell(pid, 5, price_cents=2500)
    assert _cost(second) == 5 * 1400, "брали из второй партии, где осталось 8"


def test_stock_older_than_accounting_goes_first_at_manual_cost(isolated_db):
    """Остаток до включения учёта партии не имеет: уходит первым, по ручной цене."""
    from services import costing

    _setup(isolated_db, enabled=False)
    pid = _product()
    _incoming(pid, 5)
    ok, err = isolated_db.set_product_price(str(pid), "Фильтр", None, 7.0, "USD", BOSS)
    assert ok, err
    isolated_db.set_setting("accounting_enabled", True)
    _container(pid, 10, price="10")

    inv = _sell(pid, 7, price_cents=2000)
    rows = _sale_rows(inv)
    assert [r["cost_source"] for r in rows] == ["manual", "batch"]
    assert _cost(inv) == 5 * 700 + 2 * 1000

    avg = _run(costing.current_costs([pid]))[pid]
    assert avg["unit_cost_cents"] == 1000 and avg["qty"] == 8


def test_price_entered_after_sale_fills_the_frozen_cost(isolated_db):
    from services import costing

    _setup(isolated_db)
    pid = _product()
    cid, item, inv_in = _container(pid, 10)
    sale = _sell(pid, 3, price_cents=2000)
    assert [r["cost_base_cents"] for r in _sale_rows(sale)] == [None]

    assert _run(costing.save_container_costing(
        cid, currency="USD", uzs_per_usd="12500", prices={item: "9.50"}, user_id=BOSS,
    ))["ok"]
    assert _cost(sale) == 3 * 950
    from services import adb_core

    # Приходная накладная получила цену — документ показывает цену партии.
    assert _run(adb_core.fetchval(
        "SELECT price_cents FROM invoice_items WHERE invoice_id = $1", inv_in)) == 950
    assert _run(adb_core.fetchval(
        "SELECT total_amount_cents FROM invoices WHERE id = $1", inv_in)) == 9500


def test_cancelled_sale_returns_quantity_to_its_batch(isolated_db):
    from services import costing, warehouse

    _setup(isolated_db)
    pid = _product()
    _container(pid, 10, number="AAA1", price="10")
    _container(pid, 10, number="BBB2", price="20")
    sale = _sell(pid, 8, price_cents=3000)
    assert _run(warehouse.cancel_invoice(sale))["ok"]

    again = _sell(pid, 10, price_cents=3000)
    assert _cost(again) == 10 * 1000, "отменённая продажа не расходует партию"
    assert _run(costing.current_costs([pid]))[pid]["unit_cost_cents"] == 2000


def test_rereceive_moves_sold_quantity_to_the_new_batch(isolated_db):
    """Переоприходование: проданное из прежней партии переезжает на новую."""
    from services import adb_core, container_receipt, containers, costing

    _setup(isolated_db)
    pid = _product()
    cid, item, _inv = _container(pid, 10, price="10")
    _incoming(pid, 5)
    sale = _sell(pid, 2, price_cents=2000)
    old_batch = _sale_rows(sale)[0]["batch_id"]

    assert _run(containers.set_arrived_quantities(cid, {item: 9}, user_id=BOSS))["ok"]
    rec = _run(container_receipt.receive(cid, user_id=BOSS))
    assert rec["ok"] and rec["updated"], rec
    row = _sale_rows(sale)[0]
    assert row["batch_id"] != old_batch
    assert _run(adb_core.fetchval(
        "SELECT invoice_id FROM cost_batches WHERE id = $1", row["batch_id"])) == rec["invoice_id"]
    assert int(row["cost_base_cents"]) == 2000

    summary = _run(costing.container_summaries([cid]))[cid]
    assert summary["purchased_qty"] == 9 and summary["sold_qty"] == 2
    assert summary["remaining_qty"] == 7 and summary["remaining_cost_cents"] == 7000


# ─── Курсовая разница и отчёт ─────────────────────────────────────────────────


def test_fx_difference_for_sale_in_sums(isolated_db):
    """Товар пришёл при 12 500 сум за доллар, продан при 13 000.

    2 шт × 1 300 000 сум = 2 600 000 сум: по курсу продажи $200, по курсу
    прибытия было бы $208 → потеря на курсе $8.
    """
    from services import costing

    _setup(isolated_db, uzs_now=13000.0)
    pid = _product()
    cid, _item, _inv = _container(pid, 10, price="10", uzs_per_usd="12500")
    _sell(pid, 2, price_cents=130_000_000, currency="UZS")

    rep = _run(costing.period_report(*_period()))
    assert rep["totals"]["revenue_cents"] == 20000
    assert rep["totals"]["cogs_cents"] == 2000
    assert rep["totals"]["profit_cents"] == 18000
    assert rep["fx"]["at_arrival_cents"] == 20800
    assert rep["fx"]["at_sale_cents"] == 20000
    assert rep["fx"]["diff_cents"] == -800
    assert rep["fx"]["by_currency"] == [
        {"currency": "UZS", "amount_cents": 260_000_000, "diff_cents": -800}
    ]
    assert rep["by_month"][0]["fx_diff_cents"] == -800

    summary = rep["containers"][0]
    assert summary["container_id"] == cid
    assert summary["fx"]["diff_cents"] == -800
    assert summary["margin_cents"] == 18000


def test_sale_in_base_currency_has_no_fx_difference(isolated_db):
    from services import costing

    _setup(isolated_db)
    pid = _product()
    _container(pid, 10, price="10")
    _sell(pid, 2, price_cents=1500)
    rep = _run(costing.period_report(*_period()))
    assert rep["fx"]["diff_cents"] == 0 and rep["fx"]["by_currency"] == []


def test_report_lists_negative_deals_and_unknown_costs(isolated_db):
    from services import costing

    _setup(isolated_db)
    loss = _product("Уплотнитель")
    fine = _product("Палец ковша")
    unknown = _product("Втулка")
    _container(loss, 5, number="L1", price="30")
    _container(fine, 5, number="F1", price="10")
    _container(unknown, 5, number="U1")  # цену ещё не вписали

    bad = _sell(loss, 1, price_cents=2500)
    _sell(fine, 1, price_cents=2500)
    _sell(unknown, 2, price_cents=4000)

    rep = _run(costing.period_report(*_period()))
    assert [d["invoice_id"] for d in rep["negative_deals"]] == [bad]
    assert rep["negative_deals"][0]["profit_cents"] == -500
    assert rep["totals"]["unknown_cost_lines"] == 1
    assert rep["totals"]["unknown_cost_revenue_cents"] == 8000
    assert rep["totals"]["profit_cents"] == -500 + 1500
    names = [p["name"] for p in rep["top_products"]]
    assert names[0] == "Палец ковша" and "Уплотнитель" in [p["name"] for p in rep["worst_products"]]


def test_container_card_in_cny_converts_through_sums(isolated_db):
    """Контейнер в юанях: 1 CNY = 1 750 сум, 1 USD = 12 500 → 1 CNY = $0.14."""
    from services import costing

    _setup(isolated_db)
    pid = _product()
    cid, item, inv = _container(pid, 10)
    assert _run(costing.save_container_costing(
        cid, currency="CNY", uzs_per_usd="12500", uzs_per_unit="1750",
        prices={item: "100"}, user_id=BOSS,
    ))["ok"]
    card = _run(costing.container_card(cid))
    assert card["header"]["currency"] == "CNY"
    assert card["summary"]["purchased_cost_cents"] == 10 * 100 * 14  # $140
    assert card["items"][0]["unit_price_cents"] == 10000


def test_save_rejects_bad_input(isolated_db):
    from services import containers, costing

    _setup(isolated_db)
    pid = _product()
    cid, item, _inv = _container(pid, 3)
    other = _run(containers.create_container(number="OTHER1", created_by=BOSS))["container_id"]
    foreign = _run(containers.add_item(other, name="Чужое", expected_qty=1))["item_id"]

    def save(**kw):
        base = {"currency": "USD", "uzs_per_usd": "12500", "prices": {item: "1"}, "user_id": BOSS}
        return _run(costing.save_container_costing(cid, **{**base, **kw}))

    assert "валюту закупки" in save(currency="EUR")["error"]
    assert "курс" in save(uzs_per_usd="")["error"]
    assert "CNY" in save(currency="CNY")["error"]
    assert "не из этого" in save(prices={foreign: "1"})["error"]
    assert "числом" in save(prices={item: "-5"})["error"]


def test_order_profit_uses_frozen_cost_in_order_currency(isolated_db):
    from services import costing

    _setup(isolated_db, uzs_now=12500.0)
    pid = _product()
    _container(pid, 10, price="10")
    inv = _sell(pid, 2, price_cents=25_000_000, currency="UZS")  # 250 000 сум за шт
    from services import adb_core

    _run(adb_core.execute(
        "INSERT INTO order_shipment (order_id, invoice_id, shipped_at) VALUES (77, $1, 'x')", inv
    ))
    # Выручка 500 000 сум, себестоимость $20 = 250 000 сум → прибыль 250 000 сум.
    assert _run(costing.order_profits())[77] == {"profit": 250000.0, "partial": False}


def test_rates_from_uzs():
    from decimal import Decimal

    from services import costing

    rates = costing.rates_from_uzs("CNY", Decimal("12500"), Decimal("1750"))
    assert rates["USD"] == 1 and rates["CNY"] == Decimal("0.14")
    assert rates["UZS"] == Decimal(1) / Decimal(12500)


# ─── Подсказка курса ЦБ ──────────────────────────────────────────────────────


def test_suggestion_takes_cbu_archive_for_arrival_day(isolated_db):
    from aioresponses import aioresponses

    from services import costing

    _setup(isolated_db)
    ok, err = isolated_db.set_currency_rate_daily("UZS", _today(), 1 / 12650.0, "cbu")
    assert ok, err
    # Архив CNY на сегодня засеял _setup — убираем, чтобы пойти в ЦБ.
    isolated_db_exec(isolated_db, "DELETE FROM currency_rate_daily WHERE currency_code = 'CNY'")
    with aioresponses() as mocked:
        mocked.get(f"https://cbu.uz/ru/arkhiv-kursov-valyut/json/CNY/{_today()}/",
                   payload=[{"Ccy": "CNY", "Rate": "1760.55", "Nominal": "1"}])
        sug = _run(costing.suggest_rates(_today()))
    from services import fx_rates

    _run(fx_rates.close_session())
    assert sug["source"] == "cbu"
    assert sug["uzs_per"]["USD"] == "12650.00"
    assert sug["uzs_per"]["CNY"] == "1760.55"
    # Юань тоже запомнили: rate_to_base = 1760.55 / 12650.
    assert isolated_db.get_currency_rate_asof("CNY", _today()) == pytest.approx(1760.55 / 12650.0)


def test_suggestion_goes_to_cbu_when_archive_is_empty(isolated_db):
    from aioresponses import aioresponses

    from services import costing, fx_rates

    _setup(isolated_db)
    isolated_db_exec(isolated_db, "DELETE FROM currency_rate_daily")
    day = "2026-08-03"
    with aioresponses() as mocked:
        mocked.get(f"https://cbu.uz/ru/arkhiv-kursov-valyut/json/USD/{day}/",
                   payload=[{"Ccy": "USD", "Rate": "12480.10", "Nominal": "1"}])
        mocked.get(f"https://cbu.uz/ru/arkhiv-kursov-valyut/json/CNY/{day}/", status=404)
        sug = _run(costing.suggest_rates(day))
    _run(fx_rates.close_session())
    assert sug == {"date": day, "source": "cbu", "sources": {"USD": "cbu"},
                   "uzs_per": {"USD": "12480.10"}}
    # Запомнили в архив: следующая карточка в сеть не пойдёт.
    assert isolated_db.get_currency_rate_asof("UZS", day) == pytest.approx(1 / 12480.10)


# ─── Права: менеджер не видит себестоимость ───────────────────────────────────


def _client(monkeypatch):
    import webapp.server as server

    importlib.reload(roles)
    monkeypatch.setattr(server, "verify_init_data", lambda s: {"id": int(s), "first_name": "U"})
    return TestClient(server.app)


def _post(client, path, uid, **body):
    return client.post(path, json={"initData": str(uid), **body})


def test_role_constant_matches_service():
    from services import costing
    from webapp import costing_api

    assert costing_api._COST_ROLES == costing.COST_ROLES


def test_manager_sees_no_purchase_prices_anywhere(isolated_db, monkeypatch):
    _setup(isolated_db)
    pid = _product()
    cid, _item, inv_in = _container(pid, 10, price="10")
    _sell(pid, 2, price_cents=2500)
    ok, _ = isolated_db.set_product_price(str(pid), "Фильтр", 20.0, 8.0, "USD", BOSS)
    assert ok
    client = _client(monkeypatch)

    for path, body in (
        ("/api/costing/settings", {}),
        ("/api/costing/settings/set", {"enabled": False}),
        ("/api/costing/container", {"container_id": cid}),
        ("/api/costing/container/save", {"container_id": cid}),
        ("/api/costing/report", {"period": "month"}),
        ("/api/costing/product", {"product_id": pid}),
    ):
        assert _post(client, path, MGR, **body).status_code == 403, path

    # Накладные: сумма и цены прихода режутся, расход остаётся.
    mgr_list = _post(client, "/api/wh/invoices", MGR).json()["invoices"]
    incoming = next(r for r in mgr_list if r["type"] == "incoming")
    outgoing = next(r for r in mgr_list if r["type"] == "outgoing")
    assert incoming["total_amount_cents"] is None and incoming["prices_hidden"]
    assert outgoing["total_amount_cents"] == 5000
    boss_list = _post(client, "/api/wh/invoices", BOSS).json()["invoices"]
    assert next(r for r in boss_list if r["type"] == "incoming")["total_amount_cents"] == 10000

    inv = _post(client, "/api/wh/invoices/get", MGR, invoice_id=inv_in).json()["invoice"]
    assert inv["total_amount_cents"] is None
    assert all(it["price_cents"] is None for it in inv["items"])
    inv = _post(client, "/api/wh/invoices/get", BOSS, invoice_id=inv_in).json()["invoice"]
    assert inv["items"][0]["price_cents"] == 1000

    stock_mgr = _post(client, "/api/stock", MGR).json()
    row = next(p for p in stock_mgr["products"] if p["product_id"] == pid)
    assert "cost_price" not in row and "cost_batches" not in row
    stock_boss = _post(client, "/api/stock", BOSS).json()
    row = next(p for p in stock_boss["products"] if p["product_id"] == pid)
    assert row["cost_batches"] == 10.0 and row["cost_price"] == 8.0

    card = _post(client, "/api/containers/card", MGR, container_id=cid).json()
    assert "cost" not in str(card).lower() and "price" not in str(card).lower()


def test_print_callback_redacts_incoming_prices_for_manager(isolated_db, monkeypatch):
    from handlers import printing as printing_handler
    from services import invoice_pdf

    _setup(isolated_db)
    pid = _product()
    _cid, _item, inv_in = _container(pid, 4, price="10")
    seen = []
    monkeypatch.setattr(invoice_pdf, "render_invoice_pdf", lambda inv: seen.append(inv) or b"%PDF")
    _run(printing_handler._invoice_pdf(inv_in, MGR))
    _run(printing_handler._invoice_pdf(inv_in, BOSS))
    assert seen[0]["items"][0]["price_cents"] is None
    assert seen[1]["items"][0]["price_cents"] == 1000


def test_boss_api_flow_and_analytics_drop_manual_estimate(isolated_db, monkeypatch):
    _setup(isolated_db)
    pid = _product()
    cid, item, _inv = _container(pid, 10)
    client = _client(monkeypatch)

    card = _post(client, "/api/costing/container", BOSS, container_id=cid).json()
    assert card["enabled"] and card["items"][0]["unit_price_cents"] is None
    res = _post(client, "/api/costing/container/save", BOSS, container_id=cid, currency="USD",
                uzs_per_usd="12 500", prices={str(item): "11,25"})
    assert res.status_code == 200, res.text
    _sell(pid, 4, price_cents=2000)
    isolated_db.set_product_price(str(pid), "Фильтр", None, 1.0, "USD", BOSS)

    rep = _post(client, "/api/costing/report", BOSS, period="month").json()
    assert rep["totals"]["cogs_cents"] == 4 * 1125
    assert rep["totals"]["profit_cents"] == 4 * 2000 - 4 * 1125

    analytics = _post(client, "/api/analytics", BOSS, period="month").json()
    assert all(not p["margin_known"] for p in analytics["top_products"])

    product = _post(client, "/api/costing/product", BOSS, product_id=pid).json()
    assert product["avg_cost_cents"] == 1125 and product["batches"][0]["remaining"] == 6

    off = _post(client, "/api/costing/settings/set", BOSS, enabled=False).json()
    assert off == {"ok": True, "enabled": False}
    assert _post(client, "/api/costing/container/save", BOSS, container_id=cid, currency="USD",
                 uzs_per_usd="1", prices={}).status_code == 409
