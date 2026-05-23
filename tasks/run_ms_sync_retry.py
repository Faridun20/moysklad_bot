"""
CLI: ретрай неудавшихся (или вообще не запущенных) синхронизаций
платежей в МойСклад.

Запускается из Railway Cron каждые N минут — лечит транзиентные
сбои (сетевые таймауты, 5xx от МойСклад, временный 401 при ротации
токена) без человеческого вмешательства.

Использование:
    python -m tasks.run_ms_sync_retry [--limit N]

Расписание (рекомендуется):
    Cron Schedule: */15 * * * *   (раз в 15 минут)

Поведение:
  1. Берёт все confirmed-платежи с order_id IS NOT NULL и
     ms_paymentin_id IS NULL (то есть failed + never_tried).
  2. Для каждого пытается create_paymentin_for_payment(...).
  3. Логирует результат — сколько успешно, сколько упало.
  4. Завершается с кодом 0 если все попытки отработали без crash'а
     (даже если некоторые отдельные платежи всё ещё failed — это
     состояние, а не ошибка скрипта). Код 1 — только если main()
     поймал unexpected exception.
"""

import argparse
import asyncio
import logging
import sys

from services.database import init_db, get_payments_needing_ms_sync
from services.moysklad import close_session
from services.notifier import close_tg_session

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("ms_sync_retry")


async def main(limit: int) -> int:
    init_db()

    # Lazy-импорт чтобы не падать на старте если ms_payments тянет что-то
    from services.ms_payments import create_paymentin_for_payment
    from services.ms_demand import init_demand_context, is_ready

    ok_count = 0
    fail_count = 0
    pending: list = []
    try:
        # create_paymentin_for_payment упирается в ms_demand.is_ready() —
        # без org_meta МС-payload собрать не из чего, платёж копится в failed.
        # В bot-процессе контекст инициализирует bot.main(); cron-сервис
        # запускается отдельным контейнером, поэтому делаем это сами.
        if not is_ready():
            try:
                await init_demand_context()
            except Exception:
                logger.exception("init_demand_context упал — ретрай платежей будет провален")

        pending = get_payments_needing_ms_sync(limit=limit)
        if not pending:
            logger.info("Нет платежей требующих повторной синхронизации.")
            return 0

        logger.info("Найдено %d платежей для синхронизации", len(pending))

        for p in pending:
            pid = p["id"]
            result = await create_paymentin_for_payment(pid)
            if result.get("ok"):
                ok_count += 1
                if result.get("already"):
                    logger.info("payment #%d: уже был синхронизирован (skip)", pid)
                else:
                    logger.info(
                        "payment #%d: synced → paymentin %s",
                        pid,
                        result.get("paymentin_id"),
                    )
            else:
                fail_count += 1
                logger.warning(
                    "payment #%d: failed → %s",
                    pid,
                    result.get("reason"),
                )
    finally:
        # aiohttp-сессии (МС + Telegram-notify) держим закрытыми во ВСЕХ
        # ветках, иначе на noop-прогоне (нет платежей) словим
        # "Unclosed client session" — init_demand_context уже сходил в МС.
        await close_session()
        await close_tg_session()

    if pending:
        logger.info(
            "ms_sync_retry: успешно %d, провалено %d из %d",
            ok_count,
            fail_count,
            len(pending),
        )
    # Возвращаем 0 даже если есть failed — это «нормальное» состояние,
    # cron-job не должен помечать себя как сбоившийся; настоящий сбой
    # (например, no DATABASE_URL) поймает try/except в asyncio.run.
    return 0


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Retry failed МойСклад payment syncs")
    p.add_argument(
        "--limit",
        type=int,
        default=100,
        help="Максимум платежей за один запуск (default 100)",
    )
    return p.parse_args(argv)


if __name__ == "__main__":
    args = _parse_args(sys.argv[1:])
    sys.exit(asyncio.run(main(args.limit)))
