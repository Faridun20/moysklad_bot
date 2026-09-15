"""
ОДНОРАЗОВЫЙ скрипт: догнать СУЩЕСТВУЮЩУЮ базу до текущей схемы.

НЕ часть кода бота: ничем не импортируется, из `tasks/migrate.py` не
вызывается, на старте сервисов не бежит. Единственное место в проекте, где
живёт `ALTER TABLE` — и живёт здесь именно потому, что инкрементальных
миграций в проекте нет.

Зачем он нужен, если миграций нет:
    Свежая база получает полную схему за один проход (`_create_tables`), и
    новая колонка добавляется в определение таблицы. Но на проде база уже
    развёрнута — `CREATE TABLE IF NOT EXISTS` её не тронет, и колонка,
    дописанная в определение, до прода не доедет. Один прогон этого скрипта
    закрывает разрыв; дальше он снова не нужен.

Использование:
    python -m scripts.apply_legacy_columns --dry-run          # чего не хватает
    python -m scripts.apply_legacy_columns --apply            # добавить
    python -m scripts.apply_legacy_columns --apply --legacy   # + хвост до T1.1

ДВА СПИСКА, и это не украшение:
    NEW_COLUMNS    — колонки, которых требует ТЕКУЩИЙ код. Их и применяют.
    LEGACY_COLUMNS — исторический хвост прежнего `run_migrations()` для баз
                     старше T1.1. Среди него есть колонки-призраки
                     (`user_roles.active`, `orders.approved_by`,
                     `order_items.price_at_submit`…), вычищенные в T1.2: на
                     базе, созданной текущим кодом, они лишние, и
                     `test_schema_single_pass.py::test_ghost_columns_removed`
                     такую схему завалит. Поэтому хвост под отдельным флагом.

Идемпотентно: ADD COLUMN по существующей колонке — ошибка SQL, мы её ловим и
идём дальше. Повторный прогон безопасен.

Код возврата: 0 — применено или нечего применять; 1 — база недоступна.
"""

from __future__ import annotations

import argparse
import logging
import sys

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("apply_columns")

# Колонки, которых требует текущий код. Их же несёт определение таблицы в
# `_create_tables` — здесь они для базы, развёрнутой ДО того, как они там
# появились.
NEW_COLUMNS: list[tuple[str, str, str]] = [
    # Снимок курса валюты к BASE_CURRENCY на момент завершения операции
    # (заказ → отгрузка, платёж → подтверждение). Заморозка точки-во-
    # времени: общий итог в USD по прошлым сделкам не «плывёт» при
    # последующем движении курса. NULL = снимок не снят (старые строки
    # / валюта была неизвестна) → пересчёт fallback'ит на текущий курс.
    ("orders", "fx_rate_to_base", "REAL"),
    ("payments", "fx_rate_to_base", "REAL"),
    # B7: «цена для постоянных клиентов» — вторая ПОДСКАЗКА в форме позиции
    # заказа (не второй тариф). Свежая база берёт колонку из `_create_tables`,
    # существующую догоняет этот скрипт (ручной, со старта не вызывается).
    ("product_prices", "wholesale_price_cents", "BIGINT"),
]

# Хвост прежнего run_migrations(): базы старше T1.1 (свёртка схемы в один
# проход). Применяется только с --legacy — см. про призраков в шапке.
LEGACY_COLUMNS: list[tuple[str, str, str]] = [
    ("user_roles", "moysklad_employee_id", "TEXT"),
    ("user_roles", "ms_sync_status", "TEXT DEFAULT 'pending'"),
    ("user_roles", "created_at", "TEXT"),
    # Цена за единицу для позиции заказа (в основной валюте,
    # т.е. как пользователь ввёл — например 150.50 USD).
    # При создании demand в МойСклад умножаем на 100 (минорные единицы).
    ("order_items", "price", "REAL DEFAULT 0"),
    # Валюта заказа (USD/UZS/RUB/EUR). По умолчанию BASE_CURRENCY.
    # Хранится на уровне ордера, чтобы все позиции одного заказа
    # были в одной валюте.
    ("orders", "currency", "TEXT"),
    # Тип оплаты: 'paid' (оплачено сразу) или 'credit' (в долг).
    # Default 'paid' — все старые заказы считаем как оплаченные,
    # чтобы миграция была безопасной (не объявить вдруг весь
    # архив должниками).
    ("orders", "payment_type", "TEXT NOT NULL DEFAULT 'paid'"),
    # Дата к которой клиент обязался погасить долг (ISO YYYY-MM-DD).
    # Заполняется только когда payment_type='credit', NULL иначе.
    ("orders", "due_date", "TEXT"),
    # Когда долг был погашен (ISO YYYY-MM-DD HH:MM:SS). NULL пока
    # не погашен. Для 'paid' заказов также NULL — там оплата
    # сразу, отдельный timestamp не нужен (есть created_at).
    ("orders", "paid_at", "TEXT"),
    # Двухступенчатое подтверждение оплаты:
    #  - paid_at:           менеджер отметил «деньги получил»
    #  - paid_confirmed_*:  босс/админ подтвердил «да, в кассе»
    # Заказ считается реально оплаченным ТОЛЬКО когда оба поля
    # заполнены. Если босс отклонил — paid_at обнуляется (см.
    # reject_payment_received), цикл начинается заново.
    ("orders", "paid_confirmed_at", "TEXT"),
    ("orders", "paid_confirmed_by", "BIGINT"),
    ("orders", "paid_confirmed_by_name", "TEXT"),
    # Связь платежа с заказом. Если payment.order_id IS NOT NULL —
    # это «частичная оплата по заказу N», а не самостоятельный платёж
    # в кассу. У одного заказа может быть несколько payments
    # (клиент платит частями). Когда суммa confirmed payments >=
    # order.total, заказ автоматически считается закрытым.
    ("payments", "order_id", "BIGINT"),
    # ID документа Demand в МойСклад, созданного при approve
    # отгрузки. Нужен чтобы paymentin привязывался к конкретной
    # отгрузке (operations field в API МойСклад). NULL если
    # отгрузка ещё не отправлена или create_demand упал.
    # LEGACY: новые заказы используют ms_customerorder_id ниже.
    ("orders", "ms_demand_id", "TEXT"),
    # ID «Заказа покупателя» (customerorder) в МойСклад.
    # Новый workflow — бот создаёт именно customerorder, а не
    # demand. paymentin привязывается сюда через operations
    # вместо ms_demand_id для новых заказов.
    ("orders", "ms_customerorder_id", "TEXT"),
    # ID входящего платежа (paymentin) в МойСклад. Заполняется
    # после успешного create_paymentin. Защищает от дубликатов:
    # повторный confirm не плодит новые paymentin'ы в МойСклад.
    ("payments", "ms_paymentin_id", "TEXT"),
    # Статус синхронизации с МойСклад: NULL (ещё не пробовали),
    # 'synced', 'failed' (с описанием в ms_sync_error).
    ("payments", "ms_sync_status", "TEXT"),
    ("payments", "ms_sync_error", "TEXT"),
    # ─── IMPLEMENTATION.md Фаза 2 (адаптировано: BOOLEAN→INTEGER 0/1,
    #     JSONB→TEXT, NUMERIC→REAL, без FK). Все колонки аддитивны. ──────
    # users → у нас user_roles (telegram-id как PK).
    ("user_roles", "active", "INTEGER NOT NULL DEFAULT 1"),
    ("user_roles", "email", "TEXT"),
    ("user_roles", "phone", "TEXT"),
    ("user_roles", "deactivated_at", "TEXT"),
    ("user_roles", "deactivated_by", "BIGINT"),
    # orders
    ("orders", "deleted_at", "TEXT"),
    ("orders", "rejection_comment", "TEXT"),
    ("orders", "rejection_count", "INTEGER NOT NULL DEFAULT 0"),
    ("orders", "frozen", "INTEGER NOT NULL DEFAULT 0"),
    ("orders", "cancelled_at", "TEXT"),
    ("orders", "cancelled_by", "BIGINT"),
    ("orders", "cancellation_reason", "TEXT"),
    # Когда отмена была отражена в МойСклад (реверс customerorder).
    # NULL = ещё не синхронизировано; идемпотентность ms_cancel.
    ("orders", "ms_cancel_synced_at", "TEXT"),
    # Когда документ заказа был обнаружен УДАЛЁННЫМ в МойСклад (вебхук
    # customerorder.DELETE / cron-реконсиляция). Помечает «фантомные»
    # заказы (особенно shipped/paid, чей статус мы не трогаем) — они
    # исключаются из аналитики менеджеров, но остаются в учёте долгов
    # для ручной разборки. NULL = в МС ещё существует.
    ("orders", "ms_deleted_at", "TEXT"),
    # Когда обнаружено расхождение суммы заказа с документом в МойСклад
    # (кто-то отредактировал позиции/цены в МС). Это СИГНАЛ для ручной
    # проверки (флаг + уведомление), деньги/статус НЕ меняем молча.
    # NULL = расхождений не зафиксировано.
    ("orders", "ms_drift_at", "TEXT"),
    # Когда МойСклад сообщил статус, нелегальный для локальной машины
    # состояний (напр. approved→rejected): отгрузка/остаток в МС двинулись,
    # локально применить нельзя без отката. Отдельный флаг (НЕ ms_drift_at),
    # чтобы дедуп этого алерта не глушился правкой суммы и наоборот.
    # NULL = заблокированных переходов нет.
    ("orders", "ms_transition_blocked_at", "TEXT"),
    # R4: customerorder создан в МС, но demand (отгрузка) упал — заказ
    # approved с CO, но без списания остатков. Флаг для ночного дайджеста
    # «нужна доделка demand вручную». Снимается при успешном set_order_ms_demand_id.
    # NULL = проблемы нет.
    ("orders", "ms_demand_failed_at", "TEXT"),
    ("orders", "credit_limit_override", "INTEGER NOT NULL DEFAULT 0"),
    ("orders", "credit_limit_override_by", "BIGINT"),
    ("orders", "price_check_warnings", "TEXT"),
    ("orders", "payment_confirmed", "INTEGER NOT NULL DEFAULT 0"),
    ("orders", "payment_confirmed_at", "TEXT"),
    ("orders", "client_notification_sent", "INTEGER NOT NULL DEFAULT 0"),
    ("orders", "return_status", "TEXT"),
    ("orders", "submitted_at", "TEXT"),
    ("orders", "approved_by", "BIGINT"),
    ("orders", "approved_at", "TEXT"),
    ("orders", "shipped_at", "TEXT"),
    ("orders", "shipped_by", "BIGINT"),
    # Баланс контрагента (взаиморасчёты) из МойСклад report/counterparty,
    # в копейках, как отдаёт МС. balance<0 — клиент должен нам; >0 —
    # аванс/переплата (интерпретация — на фронте «Клиенты»).
    # Синкается ночным refresh_counterparties. NULL = ещё не синкнут.
    ("ms_counterparties", "balance_cents", "BIGINT"),
    # order_items
    ("order_items", "stock_snap", "REAL"),
    ("order_items", "price_at_submit", "REAL"),
    ("order_items", "batch_id", "TEXT"),
    ("order_items", "returned_qty", "REAL NOT NULL DEFAULT 0"),
    # ─── Деньги в копейках (минорные единицы) — канон вместо float.
    #     Аддитивные BIGINT-колонки рядом со старыми REAL; backfill
    #     в run_backfills (x_cents = round(x*100)). Старые REAL пока
    #     остаются для безопасного rolling-деплоя. См. services/money.py.
    ("payments", "amount_cents", "BIGINT"),
    # Время claim'а платежа для MS-синка (WP-10). Reaper orphan'ов судит
    # устаревание по нему, а не по confirmed_at: иначе любой платёж,
    # подтверждённый >30 мин назад, мог быть сброшен reaper'ом ПРЯМО во
    # время in-flight POST → второй paymentin в МС (дубль).
    ("payments", "ms_sync_claimed_at", "TEXT"),
    ("order_items", "price_cents", "BIGINT"),
    ("order_items", "price_at_submit_cents", "BIGINT"),
    ("credit_limits", "limit_amount_cents", "BIGINT"),
    ("cash_deposits", "amount_cents", "BIGINT"),
    ("cash_deposit_orders", "amount_allocated_cents", "BIGINT"),
    ("returns", "total_amount_cents", "BIGINT"),
    ("return_items", "amount_cents", "BIGINT"),
    ("product_prices", "sale_price_cents", "BIGINT"),
    ("product_prices", "cost_price_cents", "BIGINT"),
]


def _existing_columns(cur, table: str) -> set[str]:
    """Колонки таблицы. Пустое множество и для отсутствующей таблицы —
    ALTER по ней ниже честно отвалится и попадёт в «пропущено»."""
    from services.database import USE_POSTGRES

    try:
        if USE_POSTGRES:
            cur.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_name = %s",
                (table,),
            )
            return {r[0] for r in cur.fetchall()}
        cur.execute(f"PRAGMA table_info({table})")
        return {r[1] for r in cur.fetchall()}
    except Exception:
        return set()


def missing_columns(cur, columns: list[tuple[str, str, str]]) -> list[tuple[str, str, str]]:
    """Отсеять те, что уже есть. Отдельным проходом — чтобы --dry-run
    показывал ровно то, что сделает --apply, а не «попробую все 64»."""
    seen: dict[str, set[str]] = {}
    todo = []
    for table, column, col_type in columns:
        if table not in seen:
            seen[table] = _existing_columns(cur, table)
        if column not in seen[table]:
            todo.append((table, column, col_type))
    return todo


def apply_columns(columns: list[tuple[str, str, str]], *, dry_run: bool) -> int:
    """Добавить недостающие колонки. Возвращает число применённых."""
    from services.database import get_conn, get_cursor

    applied = 0
    with get_conn() as conn:
        cur = get_cursor(conn)
        todo = missing_columns(cur, columns)
        if not todo:
            logger.info("Нечего добавлять — схема уже актуальна")
            return 0
        logger.info("Не хватает колонок: %d", len(todo))
        for table, column, col_type in todo:
            if dry_run:
                logger.info("  [dry-run] %s.%s %s", table, column, col_type)
                continue
            try:
                cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
                conn.commit()
                applied += 1
                logger.info("  + %s.%s %s", table, column, col_type)
            except Exception as e:
                conn.rollback()
                logger.warning("  ! %s.%s пропущено: %s", table, column, e)
    return applied


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(
        description="Догнать существующую базу до текущей схемы (одноразово)"
    )
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true", help="только показать, чего не хватает")
    g.add_argument("--apply", action="store_true", help="добавить недостающие колонки")
    p.add_argument(
        "--legacy",
        action="store_true",
        help="плюс хвост для баз старше T1.1 (внимание: колонки-призраки)",
    )
    args = p.parse_args(argv)

    columns = list(NEW_COLUMNS)
    if args.legacy:
        columns += LEGACY_COLUMNS
        logger.warning(
            "--legacy: применяется хвост до T1.1, включая колонки-призраки. "
            "Базе, созданной текущим кодом, он не нужен."
        )

    try:
        applied = apply_columns(columns, dry_run=args.dry_run)
    except Exception:
        logger.exception("База недоступна — ничего не применено")
        return 1
    if not args.dry_run:
        logger.info("Готово: применено %d из %d", applied, len(columns))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
