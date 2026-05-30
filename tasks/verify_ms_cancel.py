"""
Верификация MS-реверса отмены заказа на ЖИВОМ аккаунте МойСклад. Запускать в
своём окружении, где MS_TOKEN уже в env (Railway/локально). Токен в чат НЕ
попадает — печатаются только безопасные факты (reason редактируется
redact_ms_error в самом клиенте).

Два режима:

  # 1) READ-ONLY проверка связи и контекста (org/store) — ничего не меняет:
  python -m tasks.verify_ms_cancel

  # 2) Реально реверснуть customerorder для отменённого заказа #N
  #    (УДАЛЯЕТ документ в Корзину МойСклад — обратимо, но запускайте осознанно):
  python -m tasks.verify_ms_cancel --order-id 142 --reverse

Что прислать для проверки: вывод режима (1) целиком (без секретов) и, если
делали (2), строку результата ok/ms_id/reason.
"""

import argparse
import asyncio
import sys

from services import ms_cancel, ms_demand
from services.database import get_order, init_db
from services.moysklad import close_session


async def _run(order_id: int | None, do_reverse: bool) -> int:
    init_db()
    ctx = await ms_demand.init_demand_context()
    print("MS context:", {k: ctx[k] for k in ("ready", "org", "store")})
    if not ctx["ready"]:
        print("❌ org/store не разрешились — проверьте MS_TOKEN / MS_ORG_ID / MS_STORE_ID.")
        return 1
    print("✅ Связь и контекст ОК (org/store найдены).")

    if order_id is None:
        print("ℹ️  Режим read-only. Для боевого реверса: --order-id N --reverse")
        return 0

    order = await get_order(order_id)
    if not order:
        print(f"❌ Заказ #{order_id} не найден в БД.")
        return 1
    print(
        f"Заказ #{order_id}: status={order.get('status')} "
        f"ms_customerorder_id={order.get('ms_customerorder_id') or '—'} "
        f"ms_cancel_synced_at={order.get('ms_cancel_synced_at') or '—'}"
    )
    if not do_reverse:
        print("ℹ️  Без --reverse документ не трогается (dry preview).")
        return 0

    res = await ms_cancel.reverse_customerorder(order_id)
    print("Результат reverse_customerorder:", {k: res.get(k) for k in ("ok", "ms_id", "skipped", "reason")})
    return 0 if res.get("ok") else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--order-id", type=int, default=None)
    ap.add_argument("--reverse", action="store_true", help="реально удалить customerorder в МС")
    args = ap.parse_args()
    try:
        return asyncio.run(_run(args.order_id, args.reverse))
    finally:
        asyncio.run(close_session())


if __name__ == "__main__":
    sys.exit(main())
