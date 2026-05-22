"""
Верификация MS-интеграции возвратов на ЖИВОМ аккаунте МойСклад. Запускать в
своём окружении, где MS_TOKEN уже в env (Railway/локально). Токен в чат НЕ
попадает — скрипт печатает только безопасные факты (без секретов, через
redact в логах самого клиента).

Два режима:

  # 1) READ-ONLY проверка связи и контекста (org/store) — ничего не создаёт:
  python -m tasks.verify_ms_returns

  # 2) Реально создать «Возврат покупателя» для подтверждённого возврата #N
  #    (СОЗДАЁТ документ в МойСклад — запускайте на тестовом аккаунте или
  #    будьте готовы удалить документ вручную):
  python -m tasks.verify_ms_returns --return-id 12 --create

Что прислать мне для проверки: вывод режима (1) целиком (там нет секретов) и,
если делали (2), строку результата ok/ms_id/reason.
"""

import argparse
import asyncio
import sys

from services import ms_demand, ms_returns
from services.database import get_return, init_db
from services.moysklad import close_session


async def _run(return_id: int | None, do_create: bool) -> int:
    init_db()
    ctx = await ms_demand.init_demand_context()
    print("MS context:", {k: ctx[k] for k in ("ready", "org", "store")})
    if not ctx["ready"]:
        print("❌ org/store не разрешились — проверьте MS_TOKEN / MS_ORG_ID / MS_STORE_ID.")
        return 1
    print("✅ Связь и контекст ОК (org/store найдены).")

    if return_id is None:
        print("ℹ️  Режим read-only. Для боевого создания: --return-id N --create")
        return 0

    ret = get_return(return_id)
    if not ret:
        print(f"❌ Возврат #{return_id} не найден в БД.")
        return 1
    print(
        f"Возврат #{return_id}: status={ret.get('status')} "
        f"moysklad_return_id={ret.get('moysklad_return_id') or '—'}"
    )
    if not do_create:
        print("ℹ️  Без --create документ не создаётся (dry preview).")
        return 0

    res = await ms_returns.create_salesreturn(return_id)
    # res не содержит секретов; reason уже отредактирован redact_ms_error.
    print("Результат create_salesreturn:", {k: res.get(k) for k in ("ok", "ms_id", "reason")})
    return 0 if res.get("ok") else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--return-id", type=int, default=None)
    ap.add_argument("--create", action="store_true", help="реально создать документ в МС")
    args = ap.parse_args()
    try:
        return asyncio.run(_run(args.return_id, args.create))
    finally:
        asyncio.run(close_session())


if __name__ == "__main__":
    sys.exit(main())
