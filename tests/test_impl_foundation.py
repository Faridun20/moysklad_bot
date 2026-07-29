"""
IMPLEMENTATION.md Фаза 1–2: фундамент данных (адаптировано под dual-DB).

Проверяем, что на реальной SQLite-схеме (isolated_db) поднимаются новые
таблицы и колонки, сидинг app_settings работает, get/set_setting корректны,
единый whitelist ролей включает новые роли, и расширенная машина состояний
не сломала старые переходы и добавила новые.
"""

from services.order_workflow import can_transition, validate_transition


def _order(status: str) -> dict:
    return {"status": status}


# ─── Схема: новые таблицы и колонки ──────────────────────────────────────────

_NEW_TABLES = {
    "credit_limits",
    "cash_deposits",
    "cash_deposit_orders",
    "returns",
    "return_items",
    "product_batches",
    "order_change_log",
    "failed_notifications",
    "audit_archive_exports",
    "client_contacts",
    "idempotency_keys",
    "app_settings",
    "cron_runs",
}


def test_new_tables_created(isolated_db):
    db = isolated_db
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = {r[0] for r in cur.fetchall()}
    assert tables >= _NEW_TABLES, f"не созданы: {_NEW_TABLES - tables}"


def test_new_order_columns_present(isolated_db):
    db = isolated_db
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute("PRAGMA table_info(orders)")
        cols = {r[1] for r in cur.fetchall()}
    for c in (
        "frozen",
        "cancelled_at",
        "payment_confirmed",
        "rejection_count",
        "return_status",
    ):
        assert c in cols, f"нет колонки orders.{c}"


def test_new_user_roles_columns_present(isolated_db):
    db = isolated_db
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute("PRAGMA table_info(user_roles)")
        cols = {r[1] for r in cur.fetchall()}
    # active/email/phone удалены в T1.2 как призраки (ни read, ни write).
    # Деактивация живёт в deactivated_at/_by — их и проверяем.
    assert {"deactivated_at", "deactivated_by"} <= cols


# ─── app_settings ────────────────────────────────────────────────────────────


def test_settings_seeded_and_typed(isolated_db):
    db = isolated_db
    # Числа/строки/булевы десериализуются из JSON в нативные типы.
    assert db.get_setting("credit_limit_default") == 2000.0
    assert db.get_setting("cancellation_window_hours") == 4
    assert db.get_setting("cash_deposit_reminder_time") == "18:00"
    assert db.get_setting("client_notifications_enabled") is True


def test_set_setting_roundtrip_and_unknown_default(isolated_db):
    db = isolated_db
    db.set_setting("reject_max_cycles", 7, updated_by=1)
    assert db.get_setting("reject_max_cycles") == 7
    # Несуществующий ключ → переданный default.
    assert db.get_setting("no_such_key", default=42) == 42


def test_seed_is_idempotent(isolated_db):
    db = isolated_db
    db.set_setting("reject_max_cycles", 9)
    db.seed_app_settings()  # повторный сид не перетирает изменённое
    assert db.get_setting("reject_max_cycles") == 9


# ─── Роли ────────────────────────────────────────────────────────────────────


def test_valid_roles_includes_new_roles(isolated_db):
    db = isolated_db
    assert "bookkeeper" in db.VALID_ROLES
    assert "warehouse_keeper" in db.VALID_ROLES
    # set_role принимает новые роли (раньше молча отвергал).
    assert db.set_role(555, "u", "Bookkeeper", "bookkeeper") is True
    assert db.get_role(555) == "bookkeeper"


def test_role_predicates(isolated_db):
    db = isolated_db
    import importlib
    import services.roles as roles

    importlib.reload(roles)
    db.set_role(601, "b", "B", "bookkeeper")
    db.set_role(602, "w", "W", "warehouse_keeper")
    assert roles.is_bookkeeper(601) is True
    assert roles.can_confirm_deposit(601) is True
    assert roles.is_warehouse_keeper(602) is True
    assert roles.can_confirm_shipment(602) is True
    # Кладовщик не подтверждает деньги, бухгалтер не отгружает.
    assert roles.can_confirm_deposit(602) is False
    assert roles.can_confirm_shipment(601) is False


# ─── Машина состояний: старое не сломано, новое работает ─────────────────────


def test_legacy_transitions_still_valid():
    assert validate_transition(_order("draft"), "pending") is None
    assert validate_transition(_order("pending"), "approved") is None
    assert validate_transition(_order("approved"), "shipped") is None
    # Назад/в терминальные — по-прежнему нельзя.
    assert validate_transition(_order("shipped"), "draft") is not None
    assert validate_transition(_order("approved"), "pending") is not None
    assert validate_transition(_order("rejected"), "approved") is not None


def test_new_transitions_and_roles():
    # Отгрузка → оплата: бухгалтер и босс.
    assert can_transition(_order("shipped"), "paid", "bookkeeper") is True
    assert can_transition(_order("shipped"), "paid", "boss") is True
    # Кладовщик фиксирует отгрузку и возвраты, но не подтверждает оплату.
    assert can_transition(_order("approved"), "shipped", "warehouse_keeper") is True
    assert can_transition(_order("shipped"), "returned", "warehouse_keeper") is True
    assert can_transition(_order("shipped"), "paid", "warehouse_keeper") is False
    # Отмена одобренного — босс/админ.
    assert can_transition(_order("approved"), "cancelled", "boss") is True
    assert can_transition(_order("approved"), "cancelled", "manager") is False
    # Частичный → полный возврат.
    assert can_transition(_order("partially_returned"), "returned", "boss") is True
