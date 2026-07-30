"""
ДИАГНОСТИКА (read-only): почему заказ всё ещё считается в долгах после
reconcile. Ничего не меняет — только печатает отчёт.

Для каждого открытого долга (get_open_debts) показывает: статус, тип оплаты,
суммы, ссылки на МойСклад (customerorder/demand) и ЖИВОЙ статус этих документов
в МС (exists / deleted / error). По этому видно причину, почему reconcile его
не убрал:

  • НЕТ ссылок на МС            → заказ не синхронизирован с МС (reconcile его
                                  не видит — удалять в МС нечего). Решение:
                                  отменить/закрыть в боте вручную, либо это
                                  локальный заказ, который и должен считаться.
  • ссылка есть, документ EXISTS → заказ реально открыт в МС (не удалён). Долг
                                  настоящий — либо удалите в МС, либо это не
                                  фантом.
  • ссылка есть, документ DELETED→ удалён в МС, но флаг не проставлен: запустите
                                  `python -m tasks.run_ms_reconcile` (должен
                                  пометить). Если не помечает — это баг.
  • ERROR                       → МС вернул не-404 (нет доступа/иной код/сетевая
                                  ошибка) — смотрите лог.

Использование:
    python -m tasks.diagnose_ms_debts
"""

import asyncio
import logging
import sys

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("diagnose_ms_debts")


async def main() -> int:
    from services.database import get_open_debts, get_order_payment_summary, init_db
    from services.moysklad import close_session
    from tasks.run_ms_reconcile import _doc_status

    init_db()

    debts = await get_open_debts()
    if not debts:
        logger.info("Открытых долгов нет — считать нечего.")
        return 0

    logger.info("Открытых долгов: %d. Проверяю ссылки на МойСклад…\n", len(debts))

    buckets = {"no_link": 0, "exists": 0, "deleted": 0, "error": 0}
    try:
        for o in debts:
            oid = o["id"]
            co_id = o.get("ms_customerorder_id")
            demand_id = o.get("ms_demand_id")
            summary = await get_order_payment_summary(oid)

            # Живой статус документов в МС.
            co_st = await _doc_status("customerorder", co_id) if co_id else "—"
            dm_st = await _doc_status("demand", demand_id) if demand_id else "—"

            # Классификация причины.
            if not co_id and not demand_id:
                reason = "НЕТ ссылок на МС → reconcile не видит (не синхронизирован)"
                buckets["no_link"] += 1
            elif "deleted" in (co_st, dm_st):
                reason = "DELETED в МС, но НЕ помечен → запустите run_ms_reconcile"
                buckets["deleted"] += 1
            elif "error" in (co_st, dm_st):
                reason = "МС вернул не-404 (доступ/иной код) → см. лог"
                buckets["error"] += 1
            else:
                reason = "EXISTS в МС → заказ реально открыт (не удалён)"
                buckets["exists"] += 1

            logger.info(
                "#%s | %s | %s | %s | долг=%.2f (всего=%.2f, подтв.=%.2f) | "
                "CO=%s[%s] demand=%s[%s] | %s",
                oid,
                (o.get("created_at") or "")[:10],
                o.get("status"),
                o.get("agent_name") or "—",
                summary["remaining"],
                summary["total"],
                summary["confirmed"],
                co_id or "—",
                co_st,
                demand_id or "—",
                dm_st,
                reason,
            )

        logger.info(
            "\nИтог: всего=%d | нет_ссылок=%d | существуют_в_МС=%d | "
            "удалены_но_не_помечены=%d | ошибки=%d",
            len(debts),
            buckets["no_link"],
            buckets["exists"],
            buckets["deleted"],
            buckets["error"],
        )
        return 0
    except Exception:
        logger.exception("diagnose_ms_debts: ошибка")
        return 1
    finally:
        await close_session()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
