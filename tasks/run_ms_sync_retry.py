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
import logging
import sys

from services.database import (
    init_db,
    get_payments_needing_ms_sync,
    reset_stale_in_progress_payments,
)
from services.moysklad import close_session
from services.notifier import close_tg_session

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("ms_sync_retry")


async def main(limit: int) -> int:
    init_db()

    # Reaper: сбросить orphan'ов из 'in_progress' (claim был, но процесс
    # убили до set_payment_ms_sync). Без этого orphan-строки навсегда
    # выпадают из retry — claim-UPDATE отвергает 'in_progress'.
    reset_stale_in_progress_payments(older_than_minutes=30)

    # ВАЖНО: pending fetch ДО init_demand_context. Если pending пусто (наша
    # обычная ветка — 96 noop-прогонов в день при `*/15 * * * *`), сразу
    # return 0 без обращения к МС API. До этого фикса каждый noop-прогон
    # делал 2-4 МС-вызова (org/store/2× attributes) впустую.
    pending = get_payments_needing_ms_sync(limit=limit)
    if not pending:
        logger.info("Нет платежей требующих повторной синхронизации.")
        return 0

    logger.info("Найдено %d платежей для синхронизации", len(pending))

    # Lazy-импорт ms_payments/ms_demand чтобы импорт не падал, если эти
    # модули тянут что-то неподъёмное на старте контейнера.
    from services.ms_payments import create_paymentin_for_payment
    from services.ms_demand import init_demand_context

    ok_count = 0
    fail_count = 0
    try:
        # init_demand_context() сам по себе НЕ raises (его helpers
        # _pick_first/_ensure_custom_attribute глотают exception и
        # возвращают None). Поэтому try/except здесь не нужен.
        # Инспектируем dict-возврат — он содержит ready/org/store/attr*
        # как bool. is_ready()-метод даёт только агрегат, а нам нужен
        # точный диагноз partial-init в логах.
        init_result = await init_demand_context()
        if not init_result.get("ready"):
            # МС недоступен/токен ротируется. ВАЖНО: не входить в цикл —
            # иначе каждый create_paymentin_for_payment сделает claim+set_failed
            # и перезапишет реальную ms_sync_error (типа "429" или
            # "currency not found") на бесполезное "Контекст ms_demand не готов".
            logger.error(
                "init_demand_context не дошёл: %s — пропускаю %d платежей до следующего тика",
                init_result,
                len(pending),
            )
            return 0

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
        # aiohttp-сессии (МС + Telegram-notify) — закрываем во всех ветках,
        # включая early-return при init failure (init уже мог открыть сессию).
        await close_session()
        await close_tg_session()

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
    from tasks._cron_runner import run_cron

    args = _parse_args(sys.argv[1:])
    sys.exit(run_cron("ms_sync_retry", main, args.limit))
