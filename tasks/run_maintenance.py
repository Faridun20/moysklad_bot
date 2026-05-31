"""
CLI: периодическая чистка БД (IMPLEMENTATION.md §13, janitor). Railway Cron.

Что делает за один прогон (всё идемпотентно, по ретеншенам из app_settings):
  • prune_notified_shipments  — дедуп-записи отгрузок старше 30 дней;
  • prune_audit_log           — аудит старше audit_log_retention_months;
  • purge_soft_deleted        — физ-удаление soft-deleted старше
                                soft_delete_retention_days (orders/cash_deposits/returns).

Использование:
    python -m tasks.run_maintenance

Расписание Railway Cron (пример): 0 3 * * *  (3:00 UTC, ночью).
"""

import logging
import sys

from services.database import (
    get_setting,
    init_db,
    prune_audit_log,
    prune_notified_shipments,
    purge_soft_deleted,
    run_migrations,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("maintenance")


def main() -> int:
    init_db()
    run_migrations()  # M1: на свежей БД догнать колонки (идемпотентно)
    try:
        audit_months = int(get_setting("audit_log_retention_months", 6))
        soft_days = int(get_setting("soft_delete_retention_days", 365))

        shipments = prune_notified_shipments(older_than_days=30)

        # Аудит старше ретеншена удаляем напрямую (внешний архив убран).
        # Ретеншен — audit_log_retention_months (по умолчанию 6 мес).
        audit = prune_audit_log(retention_months=audit_months)
        purged = purge_soft_deleted(retention_days=soft_days)

        logger.info(
            "maintenance: notified_shipments=-%d audit_log=-%d soft_deleted=%s",
            shipments,
            audit,
            purged,
        )
        return 0
    except Exception:
        logger.exception("maintenance: ошибка")
        return 1


if __name__ == "__main__":
    from tasks._cron_runner import run_cron

    sys.exit(run_cron("maintenance", main))
