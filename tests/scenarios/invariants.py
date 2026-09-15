"""Общие инварианты: что обязано быть правдой после ЛЮБОГО сценария.

Проверки читают базу напрямую и считают деньги и остатки НЕЗАВИСИМО от
`services.debts` / `services.warehouse`: смысл в том, чтобы второй способ
посчитать совпал с первым, а не в том, чтобы вызвать тот же код дважды.

Работает на Postgres (сценарии) и на SQLite (браузерные сценарии E2E) —
через синхронный слой `db.get_conn` и `db.q`.
"""

from __future__ import annotations

import logging
from typing import Any

# Инфраструктурный шум, который не является ошибкой приложения.
_IGNORED_LOGGERS = ("asyncio",)


def _rows(db, sql: str, params=()) -> list[dict]:
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(db.q(sql), params)
        return [dict(r) for r in cur.fetchall()]


def stock_never_negative(db) -> None:
    bad = _rows(db, "SELECT product_id, warehouse_id, quantity FROM stock WHERE quantity < 0")
    assert not bad, f"отрицательный остаток: {bad}"


def stock_matches_invoices(db) -> None:
    """Остаток = приходы − расходы по ДЕЙСТВУЮЩИМ накладным, по каждому товару.

    Все движения склада идут накладными (CLAUDE.md: «Движение склада — только
    через services/warehouse.py»), поэтому это сходится всегда, если никто не
    двигал `stock` в обход.
    """
    moved = {
        (int(r["product_id"]), int(r["warehouse_id"])): float(r["q"])
        for r in _rows(
            db,
            "SELECT ii.product_id, i.warehouse_id, "
            "SUM(CASE WHEN i.type = 'incoming' THEN ii.quantity ELSE -ii.quantity END) AS q "
            "FROM invoice_items ii JOIN invoices i ON i.id = ii.invoice_id "
            "WHERE i.status = 'confirmed' GROUP BY ii.product_id, i.warehouse_id",
        )
    }
    stock = {
        (int(r["product_id"]), int(r["warehouse_id"])): float(r["quantity"])
        for r in _rows(db, "SELECT product_id, warehouse_id, quantity FROM stock")
    }
    for key in set(moved) | set(stock):
        assert abs(moved.get(key, 0.0) - stock.get(key, 0.0)) < 1e-6, (
            f"товар/склад {key}: по накладным {moved.get(key, 0.0)}, в остатке {stock.get(key, 0.0)}"
        )


def _order_money(db) -> dict[int, dict[str, Any]]:
    """Независимый расчёт денег по каждому заказу (копейки)."""
    orders = {
        int(r["id"]): {**r, "total": 0, "confirmed": 0, "pending": 0, "deposits": 0, "returns": 0}
        for r in _rows(db, "SELECT id, status, currency, payment_type, payment_confirmed, "
                           "paid_confirmed_at FROM orders")
    }
    for r in _rows(db, "SELECT order_id, quantity, price_cents FROM order_items"):
        if int(r["order_id"]) in orders:
            orders[int(r["order_id"])]["total"] += round(float(r["quantity"]) * int(r["price_cents"]))
    for r in _rows(db, "SELECT order_id, status, amount_cents FROM payments WHERE order_id IS NOT NULL"):
        o = orders.get(int(r["order_id"]))
        if o and r["status"] in ("confirmed", "pending"):
            o[r["status"]] += int(r["amount_cents"])
    for r in _rows(db, "SELECT cdo.order_id, cdo.amount_allocated_cents AS c FROM cash_deposit_orders cdo "
                       "JOIN cash_deposits d ON d.id = cdo.deposit_id WHERE d.status = 'confirmed'"):
        o = orders.get(int(r["order_id"]))
        if o:
            o["deposits"] += int(r["c"])
    for r in _rows(db, "SELECT order_id, total_amount_cents AS c, refund_method FROM returns "
                       "WHERE status = 'confirmed'"):
        o = orders.get(int(r["order_id"]))
        if o and r["refund_method"] != "cash":
            o["returns"] += int(r["c"])
    for o in orders.values():
        o["remaining"] = max(0, o["total"] - o["confirmed"] - o["deposits"] - o["returns"])
    return orders


def money_is_consistent(db) -> None:
    """Деньги по заказам не расходятся сами с собой.

    * заявлено по заказу (подтверждено + ждёт + сдачи) не больше суммы заказа
      за вычетом возвратов — одни деньги не заявляются дважды;
    * «оплачен» (`paid` / `payment_confirmed`) — только при нулевом остатке;
    * сдача не распределена больше, чем сдано; распределение не отрицательное.
    """
    for oid, o in _order_money(db).items():
        claimed = o["confirmed"] + o["pending"] + o["deposits"]
        owed = o["total"] - o["returns"]
        assert claimed <= max(owed, 0) + 1, (
            f"заказ #{oid}: заявлено {claimed} коп. при сумме {o['total']} и возвратах {o['returns']}"
        )
        if o["status"] == "paid" or int(o["payment_confirmed"] or 0):
            assert o["remaining"] == 0, f"заказ #{oid} закрыт как оплаченный, а остаток {o['remaining']} коп."
    # Распределено сдачей = прежнее распределение по долгам + наличные строки
    # разбивки (обе части — в валюте сдачи: по долгам распределяется только
    # сдача в базовой валюте на заказы в ней же).
    for d in _rows(
        db,
        "SELECT d.id, d.amount_cents, "
        "COALESCE((SELECT SUM(cdo.amount_allocated_cents) FROM cash_deposit_orders cdo "
        "          WHERE cdo.deposit_id = d.id), 0) "
        "+ COALESCE((SELECT SUM(cdp.amount_cents) FROM cash_deposit_parts cdp "
        "            WHERE cdp.deposit_id = d.id), 0) AS alloc "
        "FROM cash_deposits d",
    ):
        assert 0 <= int(d["alloc"]) <= max(int(d["amount_cents"]), 0), (
            f"сдача #{d['id']}: распределено {d['alloc']} из {d['amount_cents']} коп."
        )
    bad = _rows(db, "SELECT id, amount_cents FROM payments WHERE amount_cents <= 0")
    assert not bad, f"платежи с неположительной суммой: {bad}"


def payment_breakdown_is_consistent(db) -> None:
    """Разбивка «как получены деньги» (services.order_payments) сходится с деньгами.

    * строка ↔ платёж один к одному: тот же заказ, сумма в валюте заказа равна
      сумме платежа, валюта платежа — валюта заказа;
    * сумма строки в своей валюте положительна, способ — из известных;
    * сдача берёт наличную строку ЦЕЛИКОМ, в своей валюте, и одна строка не
      лежит в двух живых сдачах сразу;
    * у подтверждённой сдачи все её строки подтверждены, у отклонённой — нет
      (подтверждённые наличные без живой сдачи бывают только через неё);
    * наличный платёж подтверждён только сдачей.
    """
    from config import BASE_CURRENCY

    base = (BASE_CURRENCY or "USD").upper()
    parts = _rows(
        db,
        "SELECT pp.id, pp.payment_id, pp.order_id, pp.method, pp.currency, pp.amount_cents, "
        "pp.order_amount_cents, p.order_id AS pay_order, p.amount_cents AS pay_cents, p.status, "
        "UPPER(p.currency) AS pay_cur, UPPER(COALESCE(o.currency, '')) AS order_cur "
        "FROM payment_parts pp JOIN payments p ON p.id = pp.payment_id JOIN orders o ON o.id = pp.order_id",
    )
    by_id = {int(r["id"]): r for r in parts}
    for r in parts:
        assert int(r["pay_order"]) == int(r["order_id"]), f"строка разбивки #{r['id']}: платёж другого заказа"
        assert int(r["order_amount_cents"]) == int(r["pay_cents"]) > 0, (
            f"строка разбивки #{r['id']}: {r['order_amount_cents']} ≠ платёж {r['pay_cents']} коп."
        )
        assert r["pay_cur"] == (r["order_cur"] or base), f"строка разбивки #{r['id']}: платёж не в валюте заказа"
        assert int(r["amount_cents"]) > 0 and r["method"] in ("cash", "card", "bank"), r
    links = _rows(
        db,
        "SELECT cdp.deposit_id, cdp.part_id, cdp.order_id, cdp.amount_cents, d.status, "
        "COALESCE(UPPER(c.currency), ?) AS dep_cur FROM cash_deposit_parts cdp "
        "JOIN cash_deposits d ON d.id = cdp.deposit_id "
        "LEFT JOIN cash_deposit_currency c ON c.deposit_id = d.id",
        (base,),
    )
    live: dict[int, int] = {}
    for link in links:
        part = by_id.get(int(link["part_id"]))
        assert part is not None, f"сдача #{link['deposit_id']}: строка #{link['part_id']} не найдена"
        assert part["method"] == "cash", f"сдача #{link['deposit_id']}: не наличная строка #{part['id']}"
        assert int(link["order_id"]) == int(part["order_id"]), f"сдача #{link['deposit_id']}: заказ строки не тот"
        assert int(link["amount_cents"]) == int(part["amount_cents"]), (
            f"сдача #{link['deposit_id']}: строка #{part['id']} взята не целиком "
            f"({link['amount_cents']} из {part['amount_cents']})"
        )
        assert link["dep_cur"] == str(part["currency"]).upper(), (
            f"сдача #{link['deposit_id']} в {link['dep_cur']} закрывает наличные в {part['currency']}"
        )
        if link["status"] in ("pending", "confirmed"):
            live[int(part["id"])] = live.get(int(part["id"]), 0) + 1
        if link["status"] == "confirmed":
            assert part["status"] == "confirmed", (
                f"сдача #{link['deposit_id']} подтверждена, а платёж строки #{part['id']} — {part['status']}"
            )
    doubled = {pid: n for pid, n in live.items() if n > 1}
    assert not doubled, f"наличные строки в нескольких живых сдачах: {doubled}"
    confirmed_deposits = {
        int(link["part_id"]) for link in links if link["status"] == "confirmed"
    }
    orphan = [r["id"] for r in parts if r["method"] == "cash" and r["status"] == "confirmed"
              and int(r["id"]) not in confirmed_deposits]
    assert not orphan, f"наличные подтверждены без сдачи: строки {orphan}"


def orders_follow_their_status(db) -> None:
    """Статус заказа согласован с деньгами и складом.

    * по отменённому заказу нет живых денег (ожидающих или подтверждённых
      платежей) — иначе босс «подтверждает» оплату того, чего не продали;
    * отгруженный заказ с товаром из каталога списал склад накладной — иначе
      товар уехал, а остаток остался на полке в учёте.
    """
    live = _rows(db, "SELECT p.order_id, p.status, p.amount_cents FROM payments p "
                     "JOIN orders o ON o.id = p.order_id "
                     "WHERE o.status = 'cancelled' AND p.status IN ('pending', 'confirmed')")
    assert not live, f"живые платежи по отменённым заказам: {live}"
    unshipped = _rows(
        db,
        "SELECT o.id, o.status FROM orders o "
        "WHERE o.status IN ('shipped', 'paid', 'partially_returned', 'returned') "
        "AND o.ms_demand_id IS NULL "
        "AND EXISTS (SELECT 1 FROM order_item_products l WHERE l.order_id = o.id) "
        "AND NOT EXISTS (SELECT 1 FROM order_shipment s WHERE s.order_id = o.id "
        "                AND s.invoice_id IS NOT NULL)",
    )
    assert not unshipped, f"отгружены без списания со склада: {unshipped}"


def debts_api_matches_money(w) -> None:
    """Экран «Долги» босса показывает ровно тот остаток, что считается по строкам."""
    from tests.scenarios import flows

    money = _order_money(w.db)
    api = {int(d["id"]): d for d in flows.debts(w, flows.BOSS)["debts"]}
    for oid, d in api.items():
        assert round(float(d["remaining"]) * 100) == money[oid]["remaining"], (
            f"заказ #{oid}: «Долги» показывают {d['remaining']}, по строкам {money[oid]['remaining']} коп."
        )
    # И наоборот: отгруженный неоплаченный заказ с остатком обязан быть в «Долгах».
    for oid, o in money.items():
        if o["status"] in ("shipped", "partially_returned") and o["remaining"] > 0 and not int(
            o["payment_confirmed"] or 0
        ):
            assert oid in api, f"заказ #{oid} ({o['status']}) должен {o['remaining']} коп., но его нет в «Долгах»"


def audit_is_written(db) -> None:
    rows = _rows(db, "SELECT user_id, action FROM audit_log")
    assert rows, "аудит пуст: ни одно действие сценария не записано"
    assert all(r["action"] for r in rows)


def no_server_errors(w, error_records: list[logging.LogRecord]) -> None:
    fivexx = [c for c in w.calls if c[2] >= 500]
    assert not fivexx, f"ответы 5xx: {fivexx}"
    errors = [
        f"{r.name}: {r.getMessage()}" for r in error_records
        if not r.name.startswith(_IGNORED_LOGGERS)
    ]
    assert not errors, "ERROR в логах сервера:\n" + "\n".join(errors[:10])


def check_all(w, *, error_records: list[logging.LogRecord] | None = None) -> None:
    w.wait_background()
    stock_never_negative(w.db)
    stock_matches_invoices(w.db)
    money_is_consistent(w.db)
    payment_breakdown_is_consistent(w.db)
    orders_follow_their_status(w.db)
    debts_api_matches_money(w)
    if w.wrote_something():
        audit_is_written(w.db)
    no_server_errors(w, error_records or [])


def expect_audit(db, *actions: str) -> None:
    """Для сценария: эти действия записаны в аудит."""
    have = {r["action"] for r in _rows(db, "SELECT DISTINCT action FROM audit_log")}
    missing = [a for a in actions if a not in have]
    assert not missing, f"в аудите нет {missing}; есть: {sorted(have)}"
