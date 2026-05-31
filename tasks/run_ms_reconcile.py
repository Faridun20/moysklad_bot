"""
CLI: реконсиляция удалённых в МойСклад заказов покупателя — страховка от
пропущенных `customerorder.DELETE`-вебхуков. Railway Cron.

За прогон: берём approved-заказы со ссылкой `ms_customerorder_id`, для каждого
проверяем существование документа в МС (GET entity/customerorder/{id}). Если
404 — документ удалён вручную → отменяем заказ локально той же логикой, что в
вебхук-хендлере (`services.ms_sync_handler.apply_ms_customerorder_delete`).

Только approved: их безопасно авто-отменять, после отмены уходят из набора
(нет повторной обработки). shipped/paid не трогаем.

Использование:
    python -m tasks.run_ms_reconcile

Расписание Railway Cron (пример): 0 * * * *  (ежечасно).
"""

import asyncio
import logging
import sys

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("ms_reconcile")


async def _co_status(co_id: str) -> str:
    """Статус документа в МС: 'exists' | 'deleted' (404) | 'error'."""
    import aiohttp

    from services.moysklad import ms_get

    try:
        await ms_get(f"entity/customerorder/{co_id}")
        return "exists"
    except aiohttp.ClientResponseError as e:
        return "deleted" if e.status == 404 else "error"
    except Exception:
        logger.warning("ms_reconcile: проверка CO %s не удалась", co_id)
        return "error"


async def main() -> int:
    from services.database import get_orders_with_ms_customerorder, init_db, run_migrations
    from services.moysklad import close_session
    from services.ms_sync_handler import apply_ms_customerorder_delete

    init_db()
    run_migrations()

    # Пустой набор → 0 МС-вызовов (как run_ms_sync_retry).
    orders = await get_orders_with_ms_customerorder()
    if not orders:
        logger.info("ms_reconcile: approved-заказов с ms_customerorder_id нет — пропуск")
        return 0

    # Cap concurrency к МС. Семафор создаётся внутри main → привязан к текущему
    # loop'у (cron делает один asyncio.run), cross-loop проблемы нет.
    sem = asyncio.Semaphore(8)
    stats = {"checked": 0, "cancelled": 0, "errors": 0}

    async def _one(order: dict) -> None:
        co_id = order.get("ms_customerorder_id")
        if not co_id:
            return
        async with sem:
            st = await _co_status(co_id)
        stats["checked"] += 1
        if st == "deleted":
            await apply_ms_customerorder_delete(order, co_id)
            stats["cancelled"] += 1
            logger.info(
                "ms_reconcile: заказ #%s — CO %s удалён в МС → отменён локально",
                order["id"],
                co_id,
            )
        elif st == "error":
            stats["errors"] += 1

    try:
        await asyncio.gather(*(_one(o) for o in orders))
        logger.info(
            "ms_reconcile: проверено=%d отменено=%d ошибок=%d (из %d заказов)",
            stats["checked"],
            stats["cancelled"],
            stats["errors"],
            len(orders),
        )
        return 0
    except Exception:
        logger.exception("ms_reconcile: ошибка")
        return 1
    finally:
        await close_session()


if __name__ == "__main__":
    from tasks._cron_runner import run_cron

    sys.exit(run_cron("ms_reconcile", main))
