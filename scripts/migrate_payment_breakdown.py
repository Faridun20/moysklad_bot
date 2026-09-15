"""
РАЗОВЫЙ скрипт: данные до разбивки «как получены деньги».

НЕ часть кода бота: ничем не импортируется, со старта не вызывается. По
умолчанию — ТОЛЬКО ОТЧЁТ (dry-run); запись — только с `--apply` и только того,
что названо явно. Способ оплаты скрипт НЕ угадывает: как клиент заплатил, знает
менеджер, а не база.

Что было на проде (сентябрь 2026): заказ #27 «оплата сразу» 12 130 USD,
отгружен; платёж #24 pending на всю сумму «Оплата по заказу #27 (отгрузка
одобрена)» — автоплатёж одобрения без способа; сдачи #1 и #2 по 2 000 USD
подтверждены, но `cash_deposit_orders` пуст («Заказы: —»): автоплатёж держал
весь остаток заказа «заявленным», и сдаче было не на что лечь.

Использование:
    python -m scripts.migrate_payment_breakdown
        отчёт: платежи без способа, «оплата сразу», которые не отгрузить без
        разбивки, нераспределённые сдачи, сдачи без валюты.

    python -m scripts.migrate_payment_breakdown --payment 24 \\
        --parts cash:5000,card:7130 [--apply]
        разложить ожидающий платёж без способа на строки. Строка —
        `способ:сумма[:валюта[:курс]]`, способ cash|card|bank, валюта по
        умолчанию — валюта заказа. Для «оплаты сразу» сумма строк = остатку
        заказа; для «в долг» — не больше остатка. Строки пишутся от имени
        менеджера заказа (наличные — у него на руках). Платёж становится
        rejected (заменён), всё — одной транзакцией.

    python -m scripts.migrate_payment_breakdown --allocate-deposit 1 \\
        --allocate-deposit 2 [--apply]
        нераспределённую сдачу положить FIFO на наличные строки её менеджера
        (в её валюте). Подтверждённая сдача сразу подтверждает платежи этих
        строк и закрывает покрытые заказы. Порядок: сначала --payment (иначе
        сдаче не на что лечь), потом --allocate-deposit — скрипт так и делает.

Для #27 (если, скажем, всё было наличными):
    --payment 24 --parts cash:12130 --allocate-deposit 1 --allocate-deposit 2
→ 4 000 USD подтверждены сдачами, 8 130 USD наличными остаются «на руках» до
следующей сдачи. Если часть была картой — `--parts cash:5000,card:7130`: карту
подтверждает руководитель/бухгалтер обычной кнопкой.

Код возврата: 0 — прошло (или dry-run), 1 — ошибка в одном из шагов.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

os.environ.setdefault("PG_IDLE_IN_TX_TIMEOUT_MS", "0")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("migrate_payment_breakdown")


def parse_parts_arg(text: str) -> list[dict]:
    """«cash:5000,card:7130:UZS:12700» → строки формы разбивки."""
    out = []
    for chunk in (text or "").split(","):
        bits = [b.strip() for b in chunk.strip().split(":")]
        if len(bits) < 2 or not bits[0] or not bits[1]:
            raise ValueError(f"Строка «{chunk}»: нужен формат способ:сумма[:валюта[:курс]]")
        row = {"method": bits[0], "amount": bits[1]}
        if len(bits) > 2 and bits[2]:
            row["currency"] = bits[2].upper()
        if len(bits) > 3 and bits[3]:
            row["rate"] = bits[3]
        out.append(row)
    return out


async def report() -> dict:
    from services import adb_core, order_payments

    unexplained = await adb_core.fetch(
        "SELECT p.id, p.order_id, p.amount_cents, p.currency, p.comment, p.user_id, "
        "o.status AS order_status, o.payment_type FROM payments p JOIN orders o ON o.id = p.order_id "
        "WHERE p.status = 'pending' "
        "AND NOT EXISTS (SELECT 1 FROM payment_parts pp WHERE pp.payment_id = p.id) "
        "AND NOT EXISTS (SELECT 1 FROM acc_docs ad WHERE ad.payment_id = p.id AND ad.status = 'posted') "
        "ORDER BY p.id"
    )
    approved_paid = [
        int(r["id"]) for r in await adb_core.fetch(
            "SELECT id FROM orders WHERE status = 'approved' AND payment_type = 'paid' ORDER BY id"
        )
    ]
    gaps = await order_payments.payment_gap_cents(approved_paid) if approved_paid else {}
    unallocated = await adb_core.fetch(
        "SELECT d.id, d.manager_id, d.amount_cents, d.status, d.confirmed_by FROM cash_deposits d "
        "WHERE d.status IN ('pending', 'confirmed') AND d.amount_cents > 0 "
        "AND NOT EXISTS (SELECT 1 FROM cash_deposit_orders c WHERE c.deposit_id = d.id) "
        "AND NOT EXISTS (SELECT 1 FROM cash_deposit_parts c WHERE c.deposit_id = d.id) ORDER BY d.id"
    )
    no_currency = await adb_core.fetchval(
        "SELECT COUNT(*) FROM cash_deposits d WHERE NOT EXISTS "
        "(SELECT 1 FROM cash_deposit_currency c WHERE c.deposit_id = d.id)"
    )
    return {
        "unexplained_payments": [dict(r) for r in unexplained],
        "paid_orders_blocked": {oid: gap for oid, gap in gaps.items() if gap > 0},
        "unallocated_deposits": [dict(r) for r in unallocated],
        "deposits_without_currency": int(no_currency or 0),
    }


def _print_report(rep: dict) -> None:
    from services.order_payments import fmt_cents

    logger.info("══ Платежи без способа (ждут разбивки) — %d", len(rep["unexplained_payments"]))
    for p in rep["unexplained_payments"]:
        logger.info(
            "  платёж #%s · заказ #%s (%s, %s) · %s · «%s»",
            p["id"], p["order_id"], p["payment_type"], p["order_status"],
            fmt_cents(int(p["amount_cents"]), p["currency"] or ""), p["comment"] or "",
        )
    logger.info("══ «Оплата сразу» одобрены, без разбивки не отгрузить — %d", len(rep["paid_orders_blocked"]))
    for oid, gap in rep["paid_orders_blocked"].items():
        logger.info("  заказ #%s · не внесено %s", oid, fmt_cents(gap))
    logger.info("══ Сдачи без распределения («Заказы: —») — %d", len(rep["unallocated_deposits"]))
    for d in rep["unallocated_deposits"]:
        self_note = " · подтвердил сам сдающий" if d["confirmed_by"] and d["confirmed_by"] == d["manager_id"] else ""
        logger.info("  сдача #%s · менеджер %s · %s · %s%s", d["id"], d["manager_id"],
                    fmt_cents(int(d["amount_cents"])), d["status"], self_note)
    logger.info(
        "══ Сдачи без строки валюты: %d — это базовая валюта (так их и считают), переносить нечего",
        rep["deposits_without_currency"],
    )


async def convert_payment(payment_id: int, parts: list[dict], *, apply: bool) -> dict:
    from services import adb_core, order_payments

    pay = await adb_core.fetchrow("SELECT * FROM payments WHERE id = $1", int(payment_id))
    if pay is None or not pay["order_id"]:
        return {"ok": False, "error": f"Платёж #{payment_id} не найден или не привязан к заказу"}
    order = await adb_core.fetchrow("SELECT * FROM orders WHERE id = $1", int(pay["order_id"]))
    order_cur = (order["currency"] or order_payments._base()).upper()
    rows = [{**p, "currency": p.get("currency") or order_cur} for p in parts]
    actor = order_payments.Actor(
        user_id=int(order["user_id"]), name=order["full_name"] or str(order["user_id"]), role="manager",
    )
    if not apply:
        base = order_payments._base()
        # Разовый перенос старых отметок: чья была карта, уже не восстановить —
        # «куда поступили» у таких строк не требуется (require_account=False).
        inputs = order_payments.parse_parts(rows, require_account=False)
        cbu = await order_payments._cbu_for({i.currency for i in inputs} | {order_cur})
        calcs = order_payments.compute_parts(inputs, order_cur, base, cbu)
        total = sum(c.order_amount_cents for c in calcs)
        return {"ok": True, "dry_run": True, "payment_id": int(payment_id), "order_id": int(order["id"]),
                "payment_cents": int(pay["amount_cents"]), "parts_cents": total,
                "text": order_payments.recorded_text(int(order["id"]), order_cur, calcs, [int(payment_id)])}
    res = await order_payments.record_payment_parts(
        int(order["id"]), actor, rows, supersede_payment_ids=[int(payment_id)], require_account=False,
    )
    return {"ok": True, "dry_run": False, **res}


async def run(args) -> int:
    from services import order_payments

    rc = 0
    rep = await report()
    _print_report(rep)
    for pid in args.payment or []:
        if not args.parts:
            logger.error("--payment %s: укажите --parts", pid)
            return 1
        try:
            res = await convert_payment(pid, parse_parts_arg(args.parts), apply=args.apply)
        except (ValueError, order_payments.PaymentError) as e:
            logger.error("Платёж #%s: %s", pid, getattr(e, "message", str(e)))
            rc = 1
            continue
        logger.info("%s платёж #%s: %s", "[dry-run]" if not args.apply else "✔", pid, res)
    for did in args.allocate_deposit or []:
        res = await order_payments.attach_deposit_to_parts(did, dry_run=not args.apply)
        if not res.get("ok"):
            logger.error("Сдача #%s: %s", did, res.get("error"))
            rc = 1
            continue
        logger.info("%s сдача #%s: %s", "[dry-run]" if not args.apply else "✔", did, res)
    if not args.apply and (args.payment or args.allocate_deposit):
        logger.info("dry-run: база не менялась. Для записи — тот же вызов с --apply.")
    return rc


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description="Разбивка оплаты и распределение сдач для данных до выката")
    p.add_argument("--apply", action="store_true", help="записать (по умолчанию — только отчёт)")
    p.add_argument("--payment", type=int, action="append", help="id ожидающего платежа без способа")
    p.add_argument("--parts", help="строки разбивки: cash:5000,card:7130[:валюта[:курс]]")
    p.add_argument("--allocate-deposit", type=int, action="append", help="id нераспределённой сдачи")
    args = p.parse_args(argv)
    if args.payment and len(args.payment) > 1:
        p.error("--payment по одному: у каждого платежа своя разбивка")
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
