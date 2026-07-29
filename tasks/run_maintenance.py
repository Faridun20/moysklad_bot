"""
CLI: периодическая чистка БД (IMPLEMENTATION.md §13, janitor). Railway Cron.

Что делает за один прогон (всё идемпотентно, по ретеншенам из app_settings):
  • prune_notified_shipments  — дедуп-записи отгрузок старше 30 дней;
  • prune_audit_log           — аудит старше audit_log_retention_months.

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
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("maintenance")


def main() -> int:
    init_db()
    try:
        audit_months = int(get_setting("audit_log_retention_months", 6))

        shipments = prune_notified_shipments(older_than_days=30)

        # Аудит старше ретеншена удаляем напрямую (внешний архив убран).
        # Ретеншен — audit_log_retention_months (по умолчанию 6 мес).
        audit = prune_audit_log(retention_months=audit_months)

        logger.info(
            "maintenance: notified_shipments=-%d audit_log=-%d",
            shipments,
            audit,
        )
        return 0
    except Exception:
        logger.exception("maintenance: ошибка")
        return 1


if __name__ == "__main__":
    from tasks._cron_runner import run_cron

    sys.exit(run_cron("maintenance", main))
