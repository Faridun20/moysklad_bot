"""
CLI: еженедельный отчёт «Где деньги» руководству. Запускается из Railway Cron.

Что делает:
  1. Собирает дебиторку (заказы в кредит + рассрочки по технике), разбивку по
     срокам, прогноз поступлений и платёжную дисциплину за неделю.
  2. Шлёт каждому admin/boss одним Rich Message — с заголовками и таблицей.
     Не прошло (старый клиент, изменение API) — тем же отчётом простым текстом.

Пустой отчёт не отправляется: еженедельное «долгов нет» превращает сводку в
шум, который перестают читать, — а вместе с ней перестают читать и ту неделю,
когда цифры важны.

Использование:
    python -m tasks.run_money_report

Расписание в Railway Cron: 0 6 * * 1  (понедельник, 6:00 UTC = 11:00 Ташкент)
"""

import logging
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("money_report")

from services.database import get_all_users, init_db  # noqa: E402
from services.moysklad import close_session  # noqa: E402
from services.notifier import close_tg_session  # noqa: E402


async def main() -> int:
    init_db()

    from services import money_report

    data = await money_report.gather(period_days=7)
    if money_report.is_empty(data):
        logger.info("Нечего показывать: ни долгов, ни ожидавшихся платежей — не шлём.")
        return 0

    users = get_all_users()
    bosses = [
        u for u in users
        if u["role"] in ("admin", "boss") and not u.get("deactivated_at")
    ]
    if not bosses:
        logger.warning("Нет активных получателей (admin/boss) — отчёт некому слать.")
        return 0

    sent = {"rich": 0, "text": 0}
    try:
        for boss in bosses:
            kind = await money_report.send_report(boss["user_id"], data)
            sent[kind] += 1
        logger.info(
            "money_report: отправлено %d (rich: %d, текстом: %d), должников %d",
            sum(sent.values()), sent["rich"], sent["text"],
            data["totals"]["all"]["count"],
        )
        return 0
    except Exception:
        logger.exception("money_report: ошибка")
        return 1
    finally:
        await close_session()
        await close_tg_session()


if __name__ == "__main__":
    from tasks._cron_runner import run_cron

    sys.exit(run_cron("money_report", main))
