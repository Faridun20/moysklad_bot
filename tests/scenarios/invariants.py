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
    for d in _rows(db, "SELECT d.id, d.amount_cents, COALESCE(SUM(cdo.amount_allocated_cents), 0) AS alloc "
                       "FROM cash_deposits d LEFT JOIN cash_deposit_orders cdo ON cdo.deposit_id = d.id "
                       "GROUP BY d.id, d.amount_cents"):
        assert 0 <= int(d["alloc"]) <= int(d["amount_cents"]), (
            f"сдача #{d['id']}: распределено {d['alloc']} из {d['amount_cents']} коп."
        )
    bad = _rows(db, "SELECT id, amount_cents FROM payments WHERE amount_cents <= 0")
    assert not bad, f"платежи с неположительной суммой: {bad}"


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
