"""
ОДНОРАЗОВЫЙ скрипт: типы количеств, внешние ключи, CHECK и лишние индексы
на СУЩЕСТВУЮЩЕЙ базе Postgres.

НЕ часть кода бота: ничем не импортируется, со старта не вызывается (сторож —
`tests/test_db_constraints.py`). Живёт в `scripts/` по той же причине, что
`apply_legacy_columns`: `init_db()` делает только CREATE … IF NOT EXISTS, и
изменение типа, ограничение или удалённый индекс до уже развёрнутой базы сами
не доедут.

Использование:
    python -m scripts.apply_constraints              # = --dry-run: только отчёт
    python -m scripts.apply_constraints --apply      # применить

Что делает, по порядку (каждый шаг — своя транзакция, отказ шага не роняет
остальные):
    1. ТИПЫ. Количества в REAL (float4: 2.3 хранится как 2.2999999523) →
       NUMERIC; деньги `*_cents` не в BIGINT → BIGINT. Список не ручной — те же
       расхождения видит сверка старта (`startup_checks.check_column_types`).
       REAL переводится через текст (`col::text::numeric`): float4 печатается
       кратчайшей записью, которая даёт то же float4, — «2.3», а не
       «2.29999995231628» (путь через float8) и не «1234.57» (прямой
       `::numeric` режет до 6 значащих цифр).
    2. CHECK на статусы, направления и суммы; 3. FOREIGN KEY между денежными и
       складскими таблицами. Оба добавляются `NOT VALID` (новые строки
       проверяются сразу, существующие не читаются под блокировкой) и затем
       `VALIDATE`. Если в данных есть нарушители — отчёт печатает их, а
       ограничение остаётся NOT VALID: ронять прогон из-за исторической строки
       незачем, а новые записи оно уже стережёт. ИСКЛЮЧЕНИЕ — ограничения с
       `skip_if_violated` (остаток ≥ 0): NOT VALID проверяет и UPDATE старых
       строк, и приход +5 на остаток −10 (станет −5) упал бы. Такое ограничение
       не ставится вовсе, пока данные не исправлены; повторный --apply поставит.
    4. Лишние индексы (`database.DROPPED_INDEXES`) — DROP INDEX IF EXISTS:
       убрать строку из `_index_ddls` мало, на базе индекс остаётся.
    5. Индексы из `_index_ddls` — тот же `_create_indexes`, что на старте; до
       него — поиск дублей под новые UNIQUE, чтобы отказ был понятен заранее.
    Итог — сверка схемы старта (`startup_checks.check_schema`).

Идемпотентно: существующее ограничение пропускается (NOT VALID без
нарушителей повторный прогон довалидирует), тип уже верный — шага нет.

После --apply ПЕРЕЗАПУСТИТЕ bot и webapp: asyncpg кэширует подготовленные
запросы, и после смены типа колонки запрос из кэша внутри транзакции падает
«cached plan must not change result type» до переподключения.

Код возврата: 0 — прогон прошёл (NOT VALID и пропуски — не ошибка, они в
отчёте); 1 — какой-то шаг упал или база недоступна; 2 — не Postgres.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from dataclasses import dataclass, field

# Скрипт может думать между командами дольше обычного (печать отчёта по
# крупной таблице) — таймаут простоя в транзакции синхронного пула ему не нужен.
# До импорта services.database: пул читает значение при создании.
os.environ.setdefault("PG_IDLE_IN_TX_TIMEOUT_MS", "0")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("apply_constraints")

# Сколько нарушителей печатать по одному ограничению. Остальные — числом.
SHOW_ROWS = 50


# ─── Описание ограничений ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class Check:
    name: str
    table: str
    expr: str
    # Колонки, которые печатаются у нарушителя (первые — ключ строки).
    show: tuple[str, ...]
    why: str
    skip_if_violated: bool = False
    # Дополнительные выражения SELECT для отчёта (имя товара и т.п.): человеку
    # чинить данные по product_id=17 неудобно.
    extra: str = ""


@dataclass(frozen=True)
class ForeignKey:
    name: str
    table: str
    column: str
    ref_table: str
    ref_column: str = "id"


def _in(values: tuple[str, ...] | list[str]) -> str:
    return "(" + ", ".join(f"'{v}'" for v in values) + ")"


def _checks() -> list[Check]:
    """Допустимые значения — из кода, а не из головы: граф статусов заказа
    (`order_workflow.TRANSITIONS`), виды документов бухгалтерии
    (`accounting.DOC_KINDS`, `ACCOUNT_KINDS`). Правка там без правки здесь
    ловится тестом (`test_check_values_follow_the_code`)."""
    from services.accounting import ACCOUNT_KINDS, DOC_KINDS
    from services.order_workflow import TRANSITIONS

    order_statuses = sorted(TRANSITIONS)
    return [
        # ── Заказы и деньги ──
        Check("orders_status_chk", "orders", f"status IN {_in(order_statuses)}",
              ("id", "status"), "статус вне графа переходов"),
        Check("orders_payment_type_chk", "orders", "payment_type IN ('paid', 'credit')",
              ("id", "payment_type"), "тип оплаты"),
        Check("orders_return_status_chk", "orders", "return_status IN ('partial', 'full')",
              ("id", "return_status"), "статус возврата заказа"),
        Check("order_items_quantity_chk", "order_items", "quantity > 0",
              ("id", "order_id", "quantity"), "количество позиции > 0"),
        Check("order_items_price_chk", "order_items", "price_cents >= 0",
              ("id", "order_id", "price_cents"), "цена позиции ≥ 0"),
        Check("order_items_returned_chk", "order_items",
              "returned_qty >= 0 AND returned_qty <= quantity",
              ("id", "order_id", "quantity", "returned_qty"),
              "возвращено не больше, чем продано"),
        Check("payments_status_chk", "payments", "status IN ('pending', 'confirmed', 'rejected')",
              ("id", "status"), "статус платежа"),
        Check("payments_amount_chk", "payments", "amount_cents > 0",
              ("id", "order_id", "amount_cents"), "сумма платежа > 0 (сторно — статусом)"),
        Check("returns_status_chk", "returns", "status IN ('pending', 'confirmed', 'rejected')",
              ("id", "status"), "статус возврата"),
        Check("returns_type_chk", "returns", "return_type IN ('partial', 'full')",
              ("id", "return_type"), "тип возврата"),
        Check("returns_refund_chk", "returns",
              "refund_method IN ('cash', 'debt_reduction', 'no_refund')",
              ("id", "refund_method"), "способ возврата денег"),
        Check("returns_amount_chk", "returns", "total_amount_cents >= 0",
              ("id", "order_id", "total_amount_cents"), "сумма возврата ≥ 0"),
        Check("return_items_qty_chk", "return_items", "qty > 0",
              ("id", "return_id", "qty"), "количество в возврате > 0"),
        Check("return_items_amount_chk", "return_items", "amount_cents >= 0",
              ("id", "return_id", "amount_cents"), "сумма строки возврата ≥ 0"),
        # Сдача: сумма НЕ проверяется — выдача денег по возврату пишется
        # отрицательной подтверждённой сдачей (confirm_return), это законно.
        Check("cash_deposits_status_chk", "cash_deposits",
              "status IN ('pending', 'confirmed', 'rejected')", ("id", "status"), "статус сдачи"),
        Check("shipment_requests_status_chk", "shipment_requests",
              "status IN ('pending', 'approved', 'rejected', 'returned')",
              ("id", "order_id", "status"), "статус заявки на отгрузку"),
        # ── Склад ──
        Check("invoice_items_quantity_chk", "invoice_items", "quantity > 0",
              ("id", "invoice_id", "quantity"), "количество в накладной > 0"),
        Check("invoice_items_price_chk", "invoice_items", "price_cents >= 0",
              ("id", "invoice_id", "price_cents"), "цена в накладной ≥ 0"),
        Check("invoices_total_chk", "invoices", "total_amount_cents >= 0",
              ("id", "invoice_number", "total_amount_cents"), "сумма накладной ≥ 0"),
        Check("stock_quantity_chk", "stock", "quantity >= 0",
              ("product_id", "warehouse_id", "quantity"),
              "остаток не отрицательный", skip_if_violated=True,
              extra="(SELECT p.name FROM products p WHERE p.id = stock.product_id) AS product"),
        Check("container_items_qty_chk", "container_items",
              "expected_qty >= 0 AND (arrived_qty IS NULL OR arrived_qty >= 0)",
              ("id", "container_id", "expected_qty", "arrived_qty"), "количества контейнера ≥ 0"),
        Check("cost_batches_quantity_chk", "cost_batches", "quantity > 0",
              ("id", "invoice_id", "quantity"), "количество партии > 0"),
        Check("sale_costs_quantity_chk", "sale_costs", "quantity > 0",
              ("id", "invoice_id", "quantity"), "количество в себестоимости > 0"),
        Check("sale_costs_source_chk", "sale_costs",
              "cost_source IN ('batch', 'manual', 'unknown')",
              ("id", "cost_source"), "источник себестоимости"),
        # ── Бухгалтерия ──
        Check("acc_accounts_kind_chk", "acc_accounts", f"kind IN {_in(sorted(ACCOUNT_KINDS))}",
              ("id", "kind"), "вид счёта"),
        Check("acc_docs_status_chk", "acc_docs", "status IN ('posted', 'void')",
              ("id", "status"), "статус документа"),
        Check("acc_docs_kind_chk", "acc_docs", f"kind IN {_in(list(DOC_KINDS))}",
              ("id", "kind"), "вид документа"),
        Check("acc_entries_direction_chk", "acc_entries", "direction IN ('in', 'out')",
              ("id", "doc_id", "direction"), "направление движения"),
        Check("acc_entries_amount_chk", "acc_entries", "amount_cents > 0",
              ("id", "doc_id", "amount_cents"), "сумма движения > 0 (знак — в direction)"),
        Check("acc_entries_rate_source_chk", "acc_entries",
              "rate_source IN ('base', 'cbu', 'manual')",
              ("id", "doc_id", "rate_source"), "источник курса"),
        Check("acc_day_closes_counted_chk", "acc_day_closes", "counted_cents >= 0",
              ("id", "account_id", "counted_cents"), "пересчитанная сумма ≥ 0"),
    ]


# Связи, у которых удаление родителя в коде идёт ПОСЛЕ детей (или родитель не
# удаляется вовсе). Сознательно НЕ здесь:
#   * shipment_requests / order_change_log → orders — удаление черновика
#     (`delete_draft_order`) их не чистит, FK сломал бы удаление;
#   * acc_docs.machine_receipt_id — удалённое поступление по рассрочке законно
#     оставляет документ без основания (он выпадает из остатков сам);
#   * cost_batches.container_id — денормализованная подсказка, удаление
#     контейнера (`containers.CHILD_TABLES`) о партиях не знает;
#   * payments.user_id и прочие telegram-id — перенос истории пишет user_id = 0.
FOREIGN_KEYS: list[ForeignKey] = [
    # Заказы и деньги
    ForeignKey("order_items_order_fk", "order_items", "order_id", "orders"),
    ForeignKey("order_item_products_order_fk", "order_item_products", "order_id", "orders"),
    ForeignKey("order_item_products_product_fk", "order_item_products", "product_id", "products"),
    ForeignKey("payments_order_fk", "payments", "order_id", "orders"),
    ForeignKey("returns_order_fk", "returns", "order_id", "orders"),
    ForeignKey("return_items_return_fk", "return_items", "return_id", "returns"),
    ForeignKey("return_items_order_item_fk", "return_items", "order_item_id", "order_items"),
    ForeignKey("cash_deposit_orders_deposit_fk", "cash_deposit_orders", "deposit_id", "cash_deposits"),
    ForeignKey("cash_deposit_orders_order_fk", "cash_deposit_orders", "order_id", "orders"),
    ForeignKey("order_shipment_order_fk", "order_shipment", "order_id", "orders"),
    ForeignKey("order_shipment_invoice_fk", "order_shipment", "invoice_id", "invoices"),
    ForeignKey("return_receipt_return_fk", "return_receipt", "return_id", "returns"),
    ForeignKey("return_receipt_order_fk", "return_receipt", "order_id", "orders"),
    ForeignKey("return_receipt_invoice_fk", "return_receipt", "invoice_id", "invoices"),
    # Склад
    ForeignKey("invoices_warehouse_fk", "invoices", "warehouse_id", "warehouses"),
    ForeignKey("invoices_counterparty_fk", "invoices", "counterparty_id", "counterparties"),
    ForeignKey("invoice_items_invoice_fk", "invoice_items", "invoice_id", "invoices"),
    ForeignKey("invoice_items_product_fk", "invoice_items", "product_id", "products"),
    ForeignKey("stock_product_fk", "stock", "product_id", "products"),
    ForeignKey("stock_warehouse_fk", "stock", "warehouse_id", "warehouses"),
    ForeignKey("supplier_payments_counterparty_fk", "supplier_payments", "counterparty_id",
               "counterparties"),
    ForeignKey("supplier_payments_invoice_fk", "supplier_payments", "invoice_id", "invoices"),
    # Себестоимость
    ForeignKey("cost_batches_invoice_fk", "cost_batches", "invoice_id", "invoices"),
    ForeignKey("cost_batches_product_fk", "cost_batches", "product_id", "products"),
    ForeignKey("sale_costs_invoice_fk", "sale_costs", "invoice_id", "invoices"),
    ForeignKey("sale_costs_product_fk", "sale_costs", "product_id", "products"),
    ForeignKey("sale_costs_batch_fk", "sale_costs", "batch_id", "cost_batches"),
    # Контейнеры
    ForeignKey("container_receipt_invoice_fk", "container_receipt", "invoice_id", "invoices"),
    ForeignKey("container_receipt_supplier_fk", "container_receipt", "supplier_id", "counterparties"),
    ForeignKey("container_item_products_product_fk", "container_item_products", "product_id",
               "products"),
    # Бухгалтерия
    ForeignKey("acc_entries_doc_fk", "acc_entries", "doc_id", "acc_docs"),
    ForeignKey("acc_entries_account_fk", "acc_entries", "account_id", "acc_accounts"),
    ForeignKey("acc_day_closes_account_fk", "acc_day_closes", "account_id", "acc_accounts"),
    ForeignKey("acc_day_closes_doc_fk", "acc_day_closes", "doc_id", "acc_docs"),
    ForeignKey("acc_docs_order_fk", "acc_docs", "order_id", "orders"),
    ForeignKey("acc_docs_payment_fk", "acc_docs", "payment_id", "payments"),
    ForeignKey("acc_docs_deal_fk", "acc_docs", "deal_id", "machine_deals"),
]

# UNIQUE-индексы из `_index_ddls`, под которые заранее ищутся дубли:
# {имя индекса: (таблица, колонки, условие)}.
UNIQUE_KEYS: dict[str, tuple[str, tuple[str, ...], str]] = {
    "idx_order_shipment_invoice": ("order_shipment", ("invoice_id",), "invoice_id IS NOT NULL"),
    "idx_return_receipt_invoice": ("return_receipt", ("invoice_id",), "invoice_id IS NOT NULL"),
    "idx_container_receipt_invoice": (
        "container_receipt", ("invoice_id",), "invoice_id IS NOT NULL"
    ),
    "idx_return_items_return_item": ("return_items", ("return_id", "order_item_id"), "TRUE"),
    "idx_acc_day_closes_doc": ("acc_day_closes", ("doc_id",), "doc_id IS NOT NULL"),
}


# ─── Отчёт ────────────────────────────────────────────────────────────────────


@dataclass
class Report:
    dry_run: bool
    applied: list[str] = field(default_factory=list)
    planned: list[str] = field(default_factory=list)
    not_valid: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    violations: dict[str, int] = field(default_factory=dict)
    remaining: list[str] = field(default_factory=list)

    def done(self, text: str) -> None:
        (self.planned if self.dry_run else self.applied).append(text)
        logger.info("  %s %s", "[dry-run]" if self.dry_run else "+", text)


# ─── Чтение состояния ─────────────────────────────────────────────────────────


def _exists_table(cur, table: str) -> bool:
    cur.execute("SELECT to_regclass(%s) IS NOT NULL AS ok", (table,))
    return bool(cur.fetchone()["ok"])


def _columns(cur, table: str) -> set[str]:
    cur.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND table_name = %s",
        (table,),
    )
    return {r["column_name"] for r in cur.fetchall()}


def _constraint(cur, name: str) -> dict | None:
    """{'validated': bool} существующего ограничения или None."""
    cur.execute(
        "SELECT convalidated FROM pg_constraint c JOIN pg_namespace n ON n.oid = c.connamespace "
        "WHERE c.conname = %s AND n.nspname = current_schema()",
        (name,),
    )
    row = cur.fetchone()
    return {"validated": bool(row["convalidated"])} if row else None


def _print_rows(title: str, rows: list[dict], total: int) -> None:
    logger.warning("  ! %s: %d", title, total)
    for r in rows:
        logger.warning("      %s", ", ".join(f"{k}={v}" for k, v in r.items()))
    if total > len(rows):
        logger.warning("      … и ещё %d", total - len(rows))


def _run(conn, report: Report, what: str, statements: list[str]) -> bool:
    """Выполнить шаг одной транзакцией (или только показать в dry-run)."""
    if report.dry_run:
        report.done(what)
        for sql in statements:
            logger.info("      %s", sql)
        return True
    cur = conn.cursor()
    try:
        for sql in statements:
            cur.execute(sql)
        conn.commit()
    except Exception as e:  # noqa: BLE001 — шаг не роняет прогон, попадает в отчёт
        conn.rollback()
        report.failed.append(f"{what}: {type(e).__name__}: {e}".strip())
        logger.error("  ✗ %s: %s", what, e)
        return False
    report.done(what)
    return True


# ─── Шаги ─────────────────────────────────────────────────────────────────────


def step_types(conn, cur, report: Report) -> None:
    from services import database as db
    from services import startup_checks

    logger.info("1. Типы количеств и денег")
    expected = startup_checks.expected_column_types(db._table_ddls())
    actual = startup_checks.actual_column_types()
    todo = sorted(
        (t, c, fam) for (t, c), fam in expected.items()
        if (t, c) in actual and actual[(t, c)] != fam
    )
    if not todo:
        logger.info("  типы уже совпадают с определениями")
        return
    for table, column, family in todo:
        cur.execute(f"SELECT COUNT(*) AS n FROM {table}")
        rows = int(cur.fetchone()["n"])
        if family == "numeric":
            cur.execute(
                f"SELECT COUNT(*) AS n FROM {table} "
                f"WHERE {column} IS NOT NULL AND {column} <> trunc({column})"
            )
            frac = int(cur.fetchone()["n"])
            detail = f"строк {rows}, дробных {frac}"
            # extra_float_digits = 1 (дефолт с PG12) — кратчайшая точная запись
            # float4 в тексте; ставим явно, чтобы не зависеть от настроек роли.
            stmts = [
                "SET LOCAL extra_float_digits = 1",
                f"ALTER TABLE {table} ALTER COLUMN {column} TYPE NUMERIC "
                f"USING {column}::text::numeric",
            ]
        else:
            detail = f"строк {rows}"
            stmts = [
                f"ALTER TABLE {table} ALTER COLUMN {column} TYPE BIGINT USING {column}::bigint"
            ]
        _run(conn, report, f"{table}.{column}: {actual[(table, column)]} → {family} ({detail})",
             stmts)


def step_checks(conn, cur, report: Report) -> None:
    logger.info("2. CHECK-ограничения")
    for chk in _checks():
        if not _exists_table(cur, chk.table):
            report.skipped.append(f"{chk.name}: нет таблицы {chk.table}")
            continue
        missing = {c for c in chk.show if c not in _columns(cur, chk.table)}
        if missing:
            report.skipped.append(f"{chk.name}: нет колонок {', '.join(sorted(missing))}")
            continue
        existing = _constraint(cur, chk.name)
        if existing and existing["validated"]:
            continue
        # CHECK пропускает NULL, поэтому нарушитель — строго `IS FALSE`.
        where = f"({chk.expr}) IS FALSE"
        cur.execute(f"SELECT COUNT(*) AS n FROM {chk.table} WHERE {where}")
        bad = int(cur.fetchone()["n"])
        if bad:
            report.violations[chk.name] = bad
            cols = ", ".join(chk.show) + (f", {chk.extra}" if chk.extra else "")
            cur.execute(
                f"SELECT {cols} FROM {chk.table} WHERE {where} ORDER BY {chk.show[0]} "
                f"LIMIT {SHOW_ROWS}"
            )
            _print_rows(f"{chk.name} ({chk.why}) — нарушителей", [dict(r) for r in cur.fetchall()],
                        bad)
        if bad and chk.skip_if_violated and not existing:
            report.skipped.append(
                f"{chk.name}: НЕ поставлен — {bad} нарушителей; NOT VALID проверял бы и "
                "UPDATE старых строк (приход на отрицательный остаток упал бы). "
                "Исправьте данные и повторите --apply"
            )
            continue
        stmts = [] if existing else [
            f"ALTER TABLE {chk.table} ADD CONSTRAINT {chk.name} CHECK ({chk.expr}) NOT VALID"
        ]
        if bad:
            if stmts and _run(conn, report, f"{chk.name} NOT VALID ({bad} нарушителей)", stmts):
                report.not_valid.append(f"{chk.name}: {bad} нарушителей")
            elif existing:
                report.not_valid.append(f"{chk.name}: {bad} нарушителей (уже стоял NOT VALID)")
            continue
        stmts.append(f"ALTER TABLE {chk.table} VALIDATE CONSTRAINT {chk.name}")
        _run(conn, report, f"{chk.name}: CHECK ({chk.expr})", stmts)


def step_foreign_keys(conn, cur, report: Report) -> None:
    logger.info("3. Внешние ключи")
    for fk in FOREIGN_KEYS:
        if not (_exists_table(cur, fk.table) and _exists_table(cur, fk.ref_table)):
            report.skipped.append(f"{fk.name}: нет таблицы {fk.table} или {fk.ref_table}")
            continue
        if fk.column not in _columns(cur, fk.table):
            report.skipped.append(f"{fk.name}: нет колонки {fk.table}.{fk.column}")
            continue
        existing = _constraint(cur, fk.name)
        if existing and existing["validated"]:
            continue
        orphan_where = (
            f"c.{fk.column} IS NOT NULL AND NOT EXISTS "
            f"(SELECT 1 FROM {fk.ref_table} p WHERE p.{fk.ref_column} = c.{fk.column})"
        )
        cur.execute(f"SELECT COUNT(*) AS n FROM {fk.table} c WHERE {orphan_where}")
        orphans = int(cur.fetchone()["n"])
        if orphans:
            report.violations[fk.name] = orphans
            cur.execute(
                f"SELECT c.{fk.column} AS {fk.column}, COUNT(*) AS rows FROM {fk.table} c "
                f"WHERE {orphan_where} GROUP BY c.{fk.column} ORDER BY c.{fk.column} "
                f"LIMIT {SHOW_ROWS}"
            )
            _print_rows(
                f"{fk.name}: сироты {fk.table}.{fk.column} без {fk.ref_table}.{fk.ref_column}",
                [dict(r) for r in cur.fetchall()], orphans,
            )
        stmts = [] if existing else [
            f"ALTER TABLE {fk.table} ADD CONSTRAINT {fk.name} FOREIGN KEY ({fk.column}) "
            f"REFERENCES {fk.ref_table}({fk.ref_column}) NOT VALID"
        ]
        what = f"{fk.name}: {fk.table}.{fk.column} → {fk.ref_table}.{fk.ref_column}"
        if orphans:
            if stmts and _run(conn, report, f"{what} NOT VALID ({orphans} сирот)", stmts):
                report.not_valid.append(f"{fk.name}: {orphans} сирот")
            elif existing:
                report.not_valid.append(f"{fk.name}: {orphans} сирот (уже стоял NOT VALID)")
            continue
        stmts.append(f"ALTER TABLE {fk.table} VALIDATE CONSTRAINT {fk.name}")
        _run(conn, report, what, stmts)


def step_drop_indexes(conn, cur, report: Report) -> None:
    from services.database import DROPPED_INDEXES

    logger.info("4. Лишние индексы")
    cur.execute("SELECT indexname FROM pg_indexes WHERE schemaname = current_schema()")
    present = {r["indexname"] for r in cur.fetchall()}
    for name, why in DROPPED_INDEXES.items():
        if name in present:
            _run(conn, report, f"DROP INDEX {name} — {why}", [f"DROP INDEX IF EXISTS {name}"])


def step_create_indexes(conn, cur, report: Report) -> None:
    from services import database as db
    from services import startup_checks

    logger.info("5. Индексы из определений")
    for index, (table, cols, cond) in UNIQUE_KEYS.items():
        if not _exists_table(cur, table):
            continue
        key = ", ".join(cols)
        cur.execute(
            f"SELECT {key}, COUNT(*) AS rows FROM {table} WHERE {cond} "
            f"GROUP BY {key} HAVING COUNT(*) > 1 ORDER BY {key} LIMIT {SHOW_ROWS}"
        )
        dups = [dict(r) for r in cur.fetchall()]
        if dups:
            report.violations[index] = len(dups)
            _print_rows(f"{index}: дубли ({table}: {key}) — индекс не создастся", dups, len(dups))
    missing = startup_checks.check_indexes()
    if not missing:
        logger.info("  все индексы на месте")
        return
    if report.dry_run:
        report.done(missing[0])
        return
    failed = db._create_indexes()
    for sql in failed:
        report.failed.append(f"индекс не создан: {sql}")
    if not failed:
        report.done(missing[0].replace("нет индексов", "созданы индексы"))


def run(*, dry_run: bool) -> Report:
    """Весь прогон. Для тестов — без argparse и sys.exit."""
    from services import database as db
    from services import startup_checks

    report = Report(dry_run=dry_run)
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        for step in (step_types, step_checks, step_foreign_keys, step_drop_indexes,
                     step_create_indexes):
            step(conn, cur, report)
            # Чтения шага открыли транзакцию — не держим её до следующего.
            conn.rollback()
    if not dry_run:
        report.remaining = startup_checks.check_schema()
    return report


def _print_summary(report: Report) -> None:
    logger.info("══ ИТОГ%s ══", " (dry-run, база не менялась)" if report.dry_run else "")
    if report.dry_run:
        logger.info("Будет применено: %d", len(report.planned))
    else:
        logger.info("Применено: %d", len(report.applied))
    for title, items in (
        ("Оставлено NOT VALID (новые строки уже проверяются, старые — нет)", report.not_valid),
        ("Пропущено", report.skipped),
        ("ОШИБКИ", report.failed),
        ("Сверка схемы после прогона", report.remaining),
    ):
        if items:
            logger.warning("%s: %d", title, len(items))
            for item in items:
                logger.warning("  • %s", item)
    if report.violations:
        logger.warning(
            "Нарушители в данных: %s",
            ", ".join(f"{k}={v}" for k, v in sorted(report.violations.items())),
        )


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(
        description="Типы, FK, CHECK и лишние индексы на существующей базе (одноразово)"
    )
    g = p.add_mutually_exclusive_group()
    g.add_argument("--dry-run", action="store_true", help="только отчёт (по умолчанию)")
    g.add_argument("--apply", action="store_true", help="применить")
    args = p.parse_args(argv)

    from services import database as db

    if not db.USE_POSTGRES:
        logger.error("Нужен Postgres (DATABASE_URL): на SQLite ограничения так не добавить")
        return 2
    try:
        report = run(dry_run=not args.apply)
    except Exception:
        logger.exception("Прогон не выполнился — база недоступна или схема неожиданная")
        return 1
    _print_summary(report)
    return 1 if report.failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
