"""Локальный сид примерных данных для ВИЗУАЛЬНОЙ отладки WebApp.

Запуск:  python -m tasks.seed_dev

Наполняет ЛОКАЛЬНУЮ SQLite (orders / order_items / payments / cash_deposits /
returns / credit_limits / user_roles) реалистичным набором, чтобы экраны
Главная / Заказы / Финансы (Долги, Платежи) / Аналитика рендерились с данными
в обычном браузере (вместе с DEV_AUTH_BYPASS=1, см. README → «Локальная
разработка»).

ВАЖНО:
  * Предохранитель: при заданном DATABASE_URL (прод/Postgres) скрипт отказывается
    работать — сид только для локальной SQLite.
  * Идемпотентно: свои строки помечаем маркером '[seed]' и удаляем их в начале,
    поэтому повторный запуск не плодит дубли.
  * Пишем напрямую в таблицы через services.database.get_conn() (а не через
    многошаговый бот-flow approve→ship→pay, который требует контекста МойСклад) —
    это самодостаточно и предсказуемо. Деньги: и amount, и amount_cents
    (money.to_cents); время: now_str(); due_date — 'YYYY-MM-DD'.

Без реального MS_TOKEN экраны Каталог/сток и MS-балансы во вкладке «Клиенты»
останутся пустыми — это ожидаемо (данные тянутся из МойСклад вживую)."""

import logging
import os
from datetime import datetime, timedelta

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("seed_dev")

SEED_MARK = "[seed]"


def _abort_if_prod() -> None:
    if os.environ.get("DATABASE_URL"):
        raise SystemExit(
            "DATABASE_URL задан — сид предназначен только для локальной SQLite, "
            "в прод/Postgres не пишем. Уберите DATABASE_URL и повторите."
        )


def _date(offset_days: int) -> str:
    """ISO-дата (YYYY-MM-DD) со сдвигом в днях от сегодня (для due_date)."""
    return (datetime.now() + timedelta(days=offset_days)).strftime("%Y-%m-%d")


def _ts(offset_days: int = 0) -> str:
    """Таймстамп now_str-формата со сдвигом в днях (для created_at/shipped_at)."""
    return (datetime.now() + timedelta(days=offset_days)).strftime("%Y-%m-%d %H:%M:%S")


def main() -> int:
    _abort_if_prod()

    from services import database as db
    from services import money

    dev_uid = int(os.environ.get("DEV_USER_ID") or "999000001")
    mgr1, mgr2 = 999000002, 999000003

    db.init_db()
    db.run_migrations()

    # ─── Роли (через хелпер — единая точка записи user_roles) ──────────────
    db.set_role(dev_uid, "dev", "Dev Админ", "admin")
    db.set_role(mgr1, "seed_mgr1", "Алиса Менеджер", "manager")
    db.set_role(mgr2, "seed_mgr2", "Борис Менеджер", "manager")

    with db.get_conn() as conn:
        cur = db.get_cursor(conn)

        # ─── Идемпотентность: снести прошлый сид по маркеру ────────────────
        cur.execute(
            "DELETE FROM return_items WHERE return_id IN "
            "(SELECT id FROM returns WHERE reason LIKE ?)",
            (SEED_MARK + "%",),
        )
        cur.execute("DELETE FROM returns WHERE reason LIKE ?", (SEED_MARK + "%",))
        cur.execute("DELETE FROM cash_deposits WHERE notes LIKE ?", (SEED_MARK + "%",))
        cur.execute("DELETE FROM payments WHERE comment LIKE ?", (SEED_MARK + "%",))
        cur.execute(
            "DELETE FROM order_items WHERE order_id IN "
            "(SELECT id FROM orders WHERE comment LIKE ?)",
            (SEED_MARK + "%",),
        )
        cur.execute("DELETE FROM orders WHERE comment LIKE ?", (SEED_MARK + "%",))
        cur.execute("DELETE FROM credit_limits WHERE notes LIKE ?", (SEED_MARK + "%",))

        def add_order(
            *, user_id, full_name, status, agent_name, currency, payment_type,
            due_date=None, created_offset=0, shipped=False, paid=False,
        ) -> int:
            shipped_at = _ts(created_offset) if shipped else None
            paid_at = _ts(created_offset) if paid else None
            cur.execute(
                "INSERT INTO orders (user_id, full_name, status, comment, agent_id, "
                "agent_name, currency, payment_type, due_date, payment_confirmed, "
                "payment_confirmed_at, paid_confirmed_at, shipped_at, shipped_by, "
                "approved_at, approved_by, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    user_id, full_name, status, f"{SEED_MARK} demo",
                    f"agent-{agent_name}", agent_name, currency, payment_type, due_date,
                    1 if paid else 0,
                    paid_at, paid_at, shipped_at, (dev_uid if shipped else None),
                    (_ts(created_offset) if status != "draft" else None),
                    (dev_uid if status != "draft" else None),
                    _ts(created_offset), _ts(created_offset),
                ),
            )
            oid = cur.lastrowid
            return oid

        def add_item(order_id, name, qty, price, unit="шт"):
            cur.execute(
                "INSERT INTO order_items (order_id, product_name, product_href, "
                "quantity, unit, price, price_cents, note) VALUES (?,?,?,?,?,?,?,?)",
                (order_id, name, "", qty, unit, price, money.to_cents(price), ""),
            )

        def add_payment(*, user_id, full_name, amount, currency, order_id, status):
            confirmed_at = _ts() if status == "confirmed" else None
            cur.execute(
                "INSERT INTO payments (user_id, username, full_name, amount, "
                "amount_cents, currency, comment, status, order_id, created_at, "
                "confirmed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    user_id, "dev", full_name, amount, money.to_cents(amount),
                    currency, f"{SEED_MARK} оплата", status, order_id, _ts(),
                    confirmed_at,
                ),
            )

        def add_deposit(*, manager_id, amount, status):
            confirmed_at = _ts() if status == "confirmed" else None
            cur.execute(
                "INSERT INTO cash_deposits (manager_id, amount, amount_cents, "
                "deposited_at, confirmed_by, confirmed_at, status, notes, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    manager_id, amount, money.to_cents(amount), _ts(),
                    (dev_uid if status == "confirmed" else None), confirmed_at,
                    status, f"{SEED_MARK} сдача", _ts(),
                ),
            )

        def add_return(*, order_id, amount, refund_method, status):
            confirmed_at = _ts() if status == "confirmed" else None
            cur.execute(
                "INSERT INTO returns (order_id, return_type, reason, total_amount, "
                "total_amount_cents, refund_method, created_by, confirmed_by, status, "
                "created_at, confirmed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    order_id, "partial", f"{SEED_MARK} брак части товара", amount,
                    money.to_cents(amount), refund_method, mgr1,
                    (dev_uid if status == "confirmed" else None), status, _ts(),
                    confirmed_at,
                ),
            )

        def add_credit_limit(agent_name, limit_amount):
            cur.execute(
                "INSERT INTO credit_limits (agent_id, agent_name, limit_amount, "
                "limit_amount_cents, set_by, notes, updated_at, created_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (
                    f"agent-{agent_name}", agent_name, limit_amount,
                    money.to_cents(limit_amount), dev_uid, f"{SEED_MARK} лимит",
                    _ts(), _ts(),
                ),
            )

        # ─── Заказы: набор статусов / валют / типов оплаты / сроков ────────
        # 1) Черновик
        o = add_order(user_id=mgr1, full_name="ООО Ромашка", status="draft",
                      agent_name="Ромашка", currency="USD", payment_type="paid")
        add_item(o, "Кабель ВВГ 3×2.5", 100, 1.20, "м")

        # 2) Одобрен, кредит, срок в будущем
        o = add_order(user_id=mgr1, full_name="ИП Сидоров", status="approved",
                      agent_name="Сидоров", currency="USD", payment_type="credit",
                      due_date=_date(10), created_offset=-1)
        add_item(o, "Автомат ABB 16A", 20, 4.50)
        add_item(o, "Розетка Legrand", 50, 2.10)

        # 3) Отгружен, кредит, ПРОСРОЧЕН → долг overdue
        o = add_order(user_id=mgr1, full_name="ООО Стройка", status="shipped",
                      agent_name="Стройка", currency="USD", payment_type="credit",
                      due_date=_date(-5), created_offset=-12, shipped=True)
        add_item(o, "Профиль ПН 50×40", 200, 1.80, "шт")
        add_item(o, "Саморез 3.5×25", 1000, 0.02)

        # 4) Отгружен, кредит, срок СЕГОДНЯ → долг today
        o = add_order(user_id=mgr2, full_name="ТД Электрик", status="shipped",
                      agent_name="Электрик", currency="UZS", payment_type="credit",
                      due_date=_date(0), created_offset=-8, shipped=True)
        add_item(o, "Лампа LED 12W", 300, 18000.0)

        # 5) Отгружен, кредит, срок в будущем → долг upcoming + частичная оплата
        o = add_order(user_id=mgr2, full_name="ООО Свет", status="shipped",
                      agent_name="Свет", currency="USD", payment_type="credit",
                      due_date=_date(7), created_offset=-3, shipped=True)
        add_item(o, "Светильник IP65", 40, 12.00)
        add_payment(user_id=mgr2, full_name="Борис Менеджер", amount=200.0,
                    currency="USD", order_id=o, status="confirmed")

        # 6) Оплачен (закрыт) → выручка
        o = add_order(user_id=mgr1, full_name="ИП Котов", status="paid",
                      agent_name="Котов", currency="USD", payment_type="paid",
                      created_offset=0, shipped=True, paid=True)
        add_item(o, "Щит ЩРН-24", 5, 35.00)
        add_payment(user_id=mgr1, full_name="Алиса Менеджер", amount=175.0,
                    currency="USD", order_id=o, status="confirmed")

        # 7) Частично возвращён → долг partial + подтверждённый возврат
        o = add_order(user_id=mgr1, full_name="ООО Монтаж", status="partially_returned",
                      agent_name="Монтаж", currency="USD", payment_type="credit",
                      due_date=_date(3), created_offset=-6, shipped=True)
        add_item(o, "Гофра d20", 500, 0.30, "м")
        add_return(order_id=o, amount=30.0, refund_method="debt_reduction",
                   status="confirmed")

        # 8) Отгружен + ОЖИДАЮЩИЙ платёж → долг awaiting_confirmation
        o = add_order(user_id=mgr2, full_name="ТД Энергия", status="shipped",
                      agent_name="Энергия", currency="EUR", payment_type="credit",
                      due_date=_date(2), created_offset=-2, shipped=True)
        add_item(o, "Трансформатор 100ВА", 3, 95.00)
        add_payment(user_id=mgr2, full_name="Борис Менеджер", amount=285.0,
                    currency="EUR", order_id=o, status="pending")

        # ─── Доп. данные для АНАЛИТИКИ: заказы по дням на обоих менеджеров ──
        # Личная аналитика менеджера (webapp _personal_analytics) считается из
        # ЛОКАЛЬНЫХ approved/shipped заказов за период → даёт график по дням,
        # топ-товары, выручку. Company-аналитика (boss/admin) тянется из МойСклад
        # и локально пуста — поэтому Аналитику смотрят под ролью manager
        # (DEV_USER_ID=999000002, см. README).
        demo_products = [
            ("Кабель ВВГ 3×2.5", 1.20, "м"),
            ("Автомат ABB 16A", 4.50, "шт"),
            ("Розетка Legrand", 2.10, "шт"),
            ("Светильник IP65", 12.00, "шт"),
            ("Щит ЩРН-24", 35.00, "шт"),
            ("Гофра d20", 0.30, "м"),
            ("Лампа LED 12W", 9.50, "шт"),
        ]
        clients = ["Ромашка", "Сидоров", "Стройка", "Свет", "Котов", "Монтаж", "Энергия"]
        for i in range(14):
            mgr = mgr1 if i % 2 == 0 else mgr2
            client = clients[i % len(clients)]
            day = -((i % 7) + 1)  # распределяем по последним 7 дням
            status = "approved" if i % 3 == 0 else "shipped"
            o = add_order(
                user_id=mgr, full_name=f"ООО {client}", status=status,
                agent_name=client, currency="USD",
                payment_type=("credit" if i % 2 else "paid"),
                due_date=(_date(7) if i % 2 else None),
                created_offset=day, shipped=(status == "shipped"),
            )
            p1 = demo_products[i % len(demo_products)]
            add_item(o, p1[0], 10 + i * 3, p1[1], p1[2])
            p2 = demo_products[(i + 3) % len(demo_products)]
            add_item(o, p2[0], 5 + i, p2[1], p2[2])
            if i % 4 == 0:  # часть с подтверждённой оплатой → money/received
                add_payment(
                    user_id=mgr, full_name="Менеджер", amount=50.0 + i * 10,
                    currency="USD", order_id=o, status="confirmed",
                )

        # ─── Сдачи наличными (касса) ───────────────────────────────────────
        add_deposit(manager_id=mgr1, amount=500.0, status="confirmed")
        add_deposit(manager_id=mgr2, amount=320.0, status="pending")  # ждёт босса

        # ─── Кредит-лимиты ─────────────────────────────────────────────────
        add_credit_limit("Стройка", 2000.0)
        add_credit_limit("Свет", 1500.0)

        conn.commit()

        cur.execute("SELECT COUNT(*) AS c FROM orders WHERE comment LIKE ?", (SEED_MARK + "%",))
        n_orders = cur.fetchone()["c"]
        cur.execute("SELECT COUNT(*) AS c FROM payments WHERE comment LIKE ?", (SEED_MARK + "%",))
        n_pay = cur.fetchone()["c"]

    logger.info("Сид готов: %s заказов, %s платежей, 2 сдачи, 1 возврат.", n_orders, n_pay)
    logger.info(
        "DEV_USER_ID=%s (роль admin). Запускайте webapp с теми же env: "
        "DEV_AUTH_BYPASS=1, DEV_USER_ID=%s — затем откройте http://localhost:8080",
        dev_uid, dev_uid,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
