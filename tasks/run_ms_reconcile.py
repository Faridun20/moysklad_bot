"""
CLI: реконсиляция удалённых в МойСклад заказов покупателя — страховка от
пропущенных `customerorder.DELETE`-вебхуков. Railway Cron.

За прогон: берём заказы со ссылкой `ms_customerorder_id` (все активные статусы,
кроме cancelled/rejected), для каждого проверяем существование документа в МС
(GET entity/customerorder/{id}). Если 404 — документ удалён вручную → применяем
ту же логику, что в вебхук-хендлере (`apply_ms_customerorder_delete`): approved
отменяем локально, остальные помечаем ms_deleted_at + снимаем ссылку (уходят из
выручки аналитики). Обработанный заказ выпадает из набора (ms_deleted_at/ссылка),
повторной обработки нет.

Использование:
    python -m tasks.run_ms_reconcile

Расписание Railway Cron (пример): 0 * * * *  (ежечасно).
"""

import asyncio
import logging
import sys

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("ms_reconcile")


async def _doc_status(entity: str, doc_id: str) -> str:
    """Статус документа в МС: 'exists' | 'deleted' (404) | 'error'.

    entity — 'customerorder' или 'demand'."""
    import aiohttp

    from services.moysklad import ms_get

    try:
        await ms_get(f"entity/{entity}/{doc_id}")
        return "exists"
    except aiohttp.ClientResponseError as e:
        return "deleted" if e.status == 404 else "error"
    except Exception:
        logger.warning("ms_reconcile: проверка %s %s не удалась", entity, doc_id)
        return "error"


async def main() -> int:
    from services.database import (
        get_orders_with_ms_customerorder,
        get_orders_with_ms_demand,
        init_db,
        run_migrations,
    )
    from services.moysklad import close_session
    from services.ms_sync_handler import apply_ms_customerorder_delete, apply_ms_demand_delete

    init_db()
    run_migrations()

    # Cap concurrency к МС. Семафор создаётся внутри main → привязан к текущему
    # loop'у (cron делает один asyncio.run), cross-loop проблемы нет.
    sem = asyncio.Semaphore(8)
    stats = {"checked": 0, "flagged": 0, "errors": 0}

    async def _check_co(order: dict) -> None:
        co_id = order.get("ms_customerorder_id")
        if not co_id:
            return
        async with sem:
            st = await _doc_status("customerorder", co_id)
        stats["checked"] += 1
        if st == "deleted":
            await apply_ms_customerorder_delete(order, co_id)
            stats["flagged"] += 1
            logger.info(
                "ms_reconcile: заказ #%s — CO %s удалён в МС → отменён/помечен локально",
                order["id"], co_id,
            )
        elif st == "error":
            stats["errors"] += 1

    async def _check_demand(order: dict) -> None:
        demand_id = order.get("ms_demand_id")
        if not demand_id:
            return
        async with sem:
            st = await _doc_status("demand", demand_id)
        stats["checked"] += 1
        if st == "deleted":
            await apply_ms_demand_delete(order, demand_id)
            stats["flagged"] += 1
            logger.info(
                "ms_reconcile: заказ #%s — отгрузка %s удалена в МС → помечен ms_deleted_at",
                order["id"], demand_id,
            )
        elif st == "error":
            stats["errors"] += 1

    try:
        # 1) Заказы покупателя (customerorder). Пустой набор → 0 МС-вызовов.
        co_orders = await get_orders_with_ms_customerorder()
        if co_orders:
            await asyncio.gather(*(_check_co(o) for o in co_orders))

        # 2) Отгрузки (demand) — ПОСЛЕ CO-прохода: помеченные на шаге 1
        #    ms_deleted_at уже исключены из выборки (фильтр в SQL), не дёргаем МС
        #    повторно. Ловит случай «удалили отгрузку, а заказ покупателя жив».
        demand_orders = await get_orders_with_ms_demand()
        if demand_orders:
            await asyncio.gather(*(_check_demand(o) for o in demand_orders))

        if not co_orders and not demand_orders:
            logger.info("ms_reconcile: заказов со ссылками на МС нет — пропуск")
            return 0

        logger.info(
            "ms_reconcile: проверено=%d помечено=%d ошибок=%d",
            stats["checked"], stats["flagged"], stats["errors"],
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
