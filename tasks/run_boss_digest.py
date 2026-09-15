"""
CLI: вечерний дайджест решений боссу (заменяет вал точечных пушей по мелким
платежам/сдачам/возвратам — `services.notify_policy`/`services.boss_digest`).
Запускается из Railway Cron / хостового crontab.

Что делает:
  1. Проверяет, что уже пора (`boss_digest.is_due`): текущее время в
     Asia/Tashkent не раньше настройки `app_settings.boss_digest_time`
     (дефолт 19:00), и сегодня дайджест ещё не уходил
     (`app_settings.boss_digest_last_run_at`).
  2. Собирает то, что накопилось НИЖЕ `boss_instant_threshold_usd` с прошлого
     дайджеста (`boss_digest.gather`), и шлёт КАЖДОМУ admin/boss одним Rich
     Message (текстовый фолбэк — при отказе Rich).
  3. Отмечает `boss_digest_last_run_at` = сейчас — ПОСЛЕ отправки (или после
     решения «нечего слать»), так что до конца дня повторные тики (см. ниже)
     молча выходят.

**Расписание — каждые 15 минут**, а не раз в день в фиксированный час: время
дайджеста само по себе настройка (`boss_digest_time`), и раз-в-день cron не
подхватил бы её смену без правки хостового crontab. Дешёвый SELECT раз в
15 минут — не проблема, а `is_due` — единственная точка, которая решает,
слать ли что-то в ЭТОТ конкретный прогон.

Использование:
    python -m tasks.run_boss_digest

Расписание в хостовом crontab (пример, см. docker-compose.yml):
    */15 * * * *  /srv/docker/moysklad_bot/scripts/cron.sh cron-boss-digest
"""

import logging
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("boss_digest")

from services.database import get_all_users, init_db  # noqa: E402
from services.notifier import close_tg_session  # noqa: E402


async def main() -> int:
    init_db()

    from services import boss_digest
    from utils.helpers import local_now

    if not boss_digest.is_due():
        logger.info("Рано или уже отправлено сегодня — выходим.")
        return 0

    now_str = local_now().strftime("%Y-%m-%d %H:%M:%S")
    data = await boss_digest.gather()
    if boss_digest.is_empty(data):
        logger.info("Нечего показывать — всё либо решено, либо ушло немедленным пушем.")
        boss_digest.mark_run(now_str)
        return 0

    users = get_all_users()
    bosses = [
        u for u in users
        if u["role"] in ("admin", "boss") and not u.get("deactivated_at")
    ]
    if not bosses:
        logger.warning("Нет активных получателей (admin/boss) — дайджест некому слать.")
        boss_digest.mark_run(now_str)
        return 0

    sent = {"rich": 0, "text": 0}
    try:
        for boss in bosses:
            kind = await boss_digest.send_report(boss["user_id"], data)
            sent[kind] += 1
        logger.info(
            "boss_digest: отправлено %d (rich: %d, текстом: %d) — платежей %d, "
            "сдач %d, возвратов %d, получено мелких %d",
            sum(sent.values()), sent["rich"], sent["text"],
            data["payments"]["count"], data["deposits"]["count"],
            data["returns"]["count"], data["received"]["count"],
        )
        boss_digest.mark_run(now_str)
        return 0
    except Exception:
        logger.exception("boss_digest: ошибка")
        # Не отмечаем last_run_at — пропущенный прогон не должен «съесть»
        # дайджест дня, следующий 15-минутный тик попробует снова.
        return 1
    finally:
        await close_tg_session()


if __name__ == "__main__":
    from tasks._cron_runner import run_cron

    sys.exit(run_cron("boss_digest", main))
