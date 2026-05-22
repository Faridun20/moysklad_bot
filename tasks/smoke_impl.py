"""
Ручной smoke-прогон бэкенда IMPLEMENTATION.md (Фазы 3–5) на ТЕКУЩЕЙ БД.

Зачем: новые возможности (лимиты, cash deposits, возвраты, reject/freeze)
пока без UI — этот скрипт даёт «пощупать» логику end-to-end и распечатать
результат. Не трогает Telegram и МойСклад (только функции БД).

Использование (на ТЕСТОВОЙ среде / тестовой БД!):
    python -m tasks.smoke_impl

По умолчанию убирает за собой все созданные строки (--keep чтобы оставить).
Идентификаторы намеренно «смоук»-овые (manager 999000777, agent SMOKE-*),
чтобы не пересекаться с реальными данными.
"""

import os
import sys

# Windows-консоль может быть в cp1251 — принудительно UTF-8, чтобы не падать
# на кириллице/символах в выводе.
try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
except Exception:
    pass

# Заглушки токенов — нужны только для импорта config (БД берётся из
# DATABASE_URL / DB_PATH окружения, скрипт их НЕ переопределяет).
os.environ.setdefault("TELEGRAM_TOKEN", "0:smoke")
os.environ.setdefault("MS_TOKEN", "smoke")

import services.database as db  # noqa: E402

MGR = 999000777
BOSS = 999000001
AGENT = "SMOKE-AGENT"


def _h(title: str) -> None:
    print(f"\n{'=' * 60}\n>> {title}\n{'=' * 60}")


def _mk_order(total: float, qty: int, status: str) -> int:
    oid = db.create_order(MGR, "SMOKE Manager", "")
    db.update_order_agent(oid, AGENT, "SMOKE Клиент")
    db.add_order_item(oid, "SMOKE Товар", "", qty, "шт", total / qty)
    db.update_order_status(oid, status)
    return oid


def run() -> None:
    created_orders: list[int] = []

    _h("Подготовка схемы")
    db._create_tables()
    db.run_migrations()
    db._create_indexes()
    db.run_backfills()
    db.set_role(MGR, "smoke_mgr", "SMOKE Manager", "manager")
    db.ensure_credit_limit(AGENT, "SMOKE Клиент")
    print(f"роль менеджера: {db.get_role(MGR)}; лимит {AGENT}: {db.get_credit_limit(AGENT)}")

    _h("1. Кредитный лимит на отгруженном заказе (долг 250)")
    o1 = _mk_order(250.0, 1, "shipped")
    created_orders.append(o1)
    chk = db.check_credit_limit(AGENT, 1900.0)
    print(f"order #{o1} shipped, total 250")
    print(
        f"долг={chk['current_debt']}, лимит={chk['limit']}, "
        f"проекция(+1900)={chk['projected']}, превышение={chk['over_limit']}"
    )

    _h("2. Сдача наличных закрывает заказ (cash deposit → paid)")
    dep = db.create_cash_deposit(MGR, 250.0)
    print(f"создана сдача #{dep['deposit_id']}, распределение={dep['allocations']}")
    conf = db.confirm_cash_deposit(dep["deposit_id"], BOSS, "SMOKE Boss")
    print(
        f"подтверждена; закрыты заказы={conf['closed_orders']}; "
        f"статус #{o1} = {db.get_order(o1)['status']}"
    )

    _h("3. Возврат уменьшает долг (partial return, debt_reduction)")
    o2 = _mk_order(200.0, 2, "shipped")
    created_orders.append(o2)
    items = db.get_order_items(o2)
    print(
        f"order #{o2} shipped, total 200; долг агента до возврата="
        f"{db.get_agent_current_debt(AGENT)}"
    )
    ret = db.create_return(
        o2,
        "partial",
        "брак",
        [(items[0]["id"], 1, 100.0)],
        refund_method="debt_reduction",
        created_by=MGR,
    )
    db.confirm_return(ret["return_id"], BOSS, "SMOKE Boss")
    print(
        f"возврат #{ret['return_id']} подтверждён; статус #{o2} = "
        f"{db.get_order(o2)['status']}; долг агента после = "
        f"{db.get_agent_current_debt(AGENT)}"
    )

    _h("4. Reject → draft + заморозка после 3 циклов")
    o3 = _mk_order(100.0, 1, "pending")
    created_orders.append(o3)
    for i in range(1, 4):
        r = db.reject_order_to_draft(o3, BOSS, "SMOKE Boss", f"причина {i}")
        print(f"reject {i}: count={r['rejection_count']}, frozen={r['frozen']}")
        if i < 3:
            db.update_order_status(o3, "pending")
    print(f"итог: frozen={db.get_order(o3)['frozen']}")

    _h("5. Зависшие pending")
    o4 = _mk_order(100.0, 1, "pending")
    created_orders.append(o4)
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(
            db.q("UPDATE orders SET submitted_at = ? WHERE id = ?"), ("2000-01-01 00:00:00", o4)
        )
        conn.commit()
    stale = db.get_stale_pending_orders(hours=48)
    stale_ids = {o["id"] for o in stale}
    print(f"зависших pending (>48ч): {len(stale)}; #{o4} в списке: {o4 in stale_ids}")

    return created_orders


def cleanup(order_ids: list[int]) -> None:
    _h("Очистка smoke-данных")
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        if order_ids:
            ph = ",".join(["?"] * len(order_ids))
            for tbl, col in [
                ("return_items", None),  # через returns ниже
                ("order_items", "order_id"),
                ("cash_deposit_orders", "order_id"),
                ("payments", "order_id"),
                ("order_change_log", "order_id"),
                ("returns", "order_id"),
                ("orders", "id"),
            ]:
                if col is None:
                    continue
                cur.execute(db.q(f"DELETE FROM {tbl} WHERE {col} IN ({ph})"), order_ids)
        cur.execute(db.q("DELETE FROM cash_deposits WHERE manager_id = ?"), (MGR,))
        cur.execute(db.q("DELETE FROM credit_limits WHERE agent_id = ?"), (AGENT,))
        cur.execute(db.q("DELETE FROM audit_log WHERE user_id IN (?, ?)"), (MGR, BOSS))
        cur.execute(db.q("DELETE FROM user_roles WHERE user_id = ?"), (MGR,))
        conn.commit()
    print("smoke-данные удалены")


def main() -> int:
    keep = "--keep" in sys.argv
    try:
        order_ids = run()
    except Exception:
        import traceback

        traceback.print_exc()
        return 1
    if keep:
        print("\n--keep: данные оставлены в БД")
    else:
        cleanup(order_ids)
    print("\n✅ smoke завершён")
    return 0


if __name__ == "__main__":
    sys.exit(main())
