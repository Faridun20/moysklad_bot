"""
CLI: реконсиляция удалённых в МойСклад заказов покупателя — страховка от
пропущенных `customerorder.DELETE`-вебхуков. Railway Cron.

За прогон: берём заказы со ссылкой `ms_customerorder_id` (все активные статусы,
кроме cancelled/rejected) и спрашиваем МС БАТЧАМИ — один списочный запрос на
100 документов (`filter=id=<uuid>;id=<uuid>…`, повторное «=» по одному полю у
МойСклад означает ИЛИ). Пришедшие в `rows` живы; наши id, которых в ответе нет,
удалены. Раньше на каждую строку выборки шёл отдельный GET: 1500 запросов в час,
99,9 % из которых отвечали «документ на месте» (MS-4). Если удалён — применяем
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


# limit=100 — максимум страницы у МойСклад; одна запись «id=<36 симв.>;» ≈ 40
# байт, значит URL остаётся далеко в пределах 8 КБ, отведённых на заголовки
# запроса (иначе HTTP 414).
_BATCH_SIZE = 100

# Предохранитель: если на батч из стольких id пришёл ПУСТОЙ ответ — это почти
# наверняка сломанный фильтр (сменился синтаксис, отозваны права), а не
# одновременное удаление всей пачки. Считать такой ответ за «все удалены»
# нельзя: реконсиляция отменит живые заказы разом.
_EMPTY_ANSWER_ALARM = 5


async def _alive_ids(entity: str, ids: list[str]) -> set[str] | None:
    """id документов, которые ЖИВЫ в МС. `None` — спросить не удалось.

    None и пустое множество — принципиально разные исходы: первое значит «не
    знаем», второе «ничего не осталось». Перепутать их здесь — значит отменить
    живые заказы, поэтому любая неуверенность возвращает None, и прогон просто
    пропускает батч до следующего часа.
    """
    from services.moysklad import ms_get

    alive: set[str] = set()
    for start in range(0, len(ids), _BATCH_SIZE):
        chunk = ids[start : start + _BATCH_SIZE]
        try:
            data = await ms_get(
                f"entity/{entity}",
                params={"filter": ";".join(f"id={i}" for i in chunk), "limit": _BATCH_SIZE},
            )
        except Exception as e:  # noqa: BLE001 — сеть, права, лимиты
            logger.warning("ms_reconcile: батч %s (%d id) не прошёл: %s", entity, len(chunk), e)
            return None
        rows = (data or {}).get("rows")
        if rows is None:
            logger.warning("ms_reconcile: ответ %s без rows — пропускаю прогон", entity)
            return None
        size = ((data or {}).get("meta") or {}).get("size")
        if isinstance(size, int) and size > len(rows):
            # Ответ урезан пагинацией: часть живых документов не попала в rows,
            # и они выглядели бы удалёнными.
            logger.warning(
                "ms_reconcile: %s вернул %d из %d — не сужу об удалении",
                entity, len(rows), size,
            )
            return None
        if not rows and len(chunk) >= _EMPTY_ANSWER_ALARM:
            logger.error(
                "ms_reconcile: %s — на батч из %d id пришло 0 строк. Похоже на сломанный "
                "фильтр, а не на удаление пачки. Прогон пропущен.",
                entity, len(chunk),
            )
            return None
        alive.update(r.get("id") for r in rows if r.get("id"))
    return alive


async def main() -> int:
    from services.database import (
        get_orders_with_ms_customerorder,
        get_orders_with_ms_demand,
        get_payments_with_ms_paymentin,
        init_db,
        reset_payment_ms_sync,
    )
    from services.moysklad import close_session
    from services.ms_sync_handler import apply_ms_customerorder_delete, apply_ms_demand_delete

    init_db()

    stats = {"checked": 0, "flagged": 0, "errors": 0}

    async def _reconcile(entity: str, rows: list[dict], id_field: str, on_deleted) -> None:
        """Общий проход: спросить батчами, кого нет в ответе — отдать обработчику.

        MS-4: раньше на каждую строку шёл свой GET под семафором. Теперь
        запросов ceil(N/100), и конкурентность не нужна вовсе — этот путь
        больше не тратит лимит параллельности МойСклад (MS-2).
        """
        docs = {r[id_field]: r for r in rows if r.get(id_field)}
        if not docs:
            return
        alive = await _alive_ids(entity, list(docs))
        if alive is None:
            stats["errors"] += len(docs)
            return
        stats["checked"] += len(docs)
        for doc_id, row in docs.items():
            if doc_id in alive:
                continue
            await on_deleted(row, doc_id)
            stats["flagged"] += 1

    async def _co_deleted(order: dict, co_id: str) -> None:
        await apply_ms_customerorder_delete(order, co_id)
        logger.info(
            "ms_reconcile: заказ #%s — CO %s удалён в МС → отменён/помечен локально",
            order["id"], co_id,
        )

    async def _demand_deleted(order: dict, demand_id: str) -> None:
        await apply_ms_demand_delete(order, demand_id)
        logger.info(
            "ms_reconcile: заказ #%s — отгрузка %s удалена в МС → помечен ms_deleted_at",
            order["id"], demand_id,
        )

    async def _paymentin_deleted(payment: dict, pin_id: str) -> None:
        # paymentin.DELETE-вебхук потерян (200 отдан до фоновой обработки,
        # рестарт). Сбрасываем ссылку → retry-cron пересоздаст paymentin
        # (как делает webhook-хендлер). WP-17.
        await asyncio.to_thread(reset_payment_ms_sync, payment["id"])
        logger.warning(
            "ms_reconcile: платёж #%s — paymentin %s удалён в МС → сброшен для пересоздания",
            payment["id"], pin_id,
        )

    try:
        # 1) Заказы покупателя (customerorder). Пустой набор → 0 МС-вызовов.
        co_orders = await get_orders_with_ms_customerorder()
        if co_orders:
            await _reconcile("customerorder", co_orders, "ms_customerorder_id", _co_deleted)

        # 2) Отгрузки (demand) — ПОСЛЕ CO-прохода: помеченные на шаге 1
        #    ms_deleted_at уже исключены из выборки (фильтр в SQL), не дёргаем МС
        #    повторно. Ловит случай «удалили отгрузку, а заказ покупателя жив».
        demand_orders = await get_orders_with_ms_demand()
        if demand_orders:
            await _reconcile("demand", demand_orders, "ms_demand_id", _demand_deleted)

        # 3) Входящие платежи (paymentin) — страховка от потерянных
        #    paymentin.DELETE-вебхуков (WP-17).
        pin_payments = await get_payments_with_ms_paymentin()
        if pin_payments:
            await _reconcile("paymentin", pin_payments, "ms_paymentin_id", _paymentin_deleted)

        if not co_orders and not demand_orders and not pin_payments:
            logger.info("ms_reconcile: заказов/платежей со ссылками на МС нет — пропуск")
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
