"""
Необработанные ошибки: человеку — короткое «что-то пошло не так», в лог —
трассу, админам — сообщение в Telegram.

Раньше упавший хендлер бота оставлял пользователя в тишине (у aiogram не было
`dp.errors`), а WebApp отдавал клиенту `str(e)` — внутренности запроса вплоть
до текста SQL. Об ошибке узнавали из жалобы «бот не отвечает».

Решения:

* **Одна функция на оба процесса** (`report_exception`): бот и WebApp — разные
  контейнеры, но формат алерта и дроссель у них общие.
* **Дроссель по «отпечатку» ошибки** (место + тип + последняя строка кода), не
  чаще раза в `ALERT_INTERVAL_SEC`: цикл падений не должен превратиться в
  сотню сообщений в минуту, из-за которых админ отключит уведомления. Сколько
  раз ошибку проглотили — пишем в следующем алерте.
* **Отправка best-effort**: `tg_send_message` не бросает, алерт никогда не
  роняет обработку ошибки (иначе ошибка в ошибке и снова тишина).
* Текст исключения — через `redact_token` и `esc`: в repr aiohttp-ошибок бывает
  URL с токеном бота, а сообщение уходит с parse_mode=HTML.
"""

from __future__ import annotations

import logging
import time
import traceback

from utils.helpers import esc, redact_token

logger = logging.getLogger(__name__)

USER_MESSAGE = "Что-то пошло не так. Попробуйте ещё раз — мы уже знаем об ошибке."

ALERT_INTERVAL_SEC = 10 * 60
_MAX_FINGERPRINTS = 500

# отпечаток → (момент последнего алерта, сколько раз проглочено после него)
_last_sent: dict[str, tuple[float, int]] = {}


def _fingerprint(exc: BaseException, where: str) -> str:
    """Одна и та же ошибка из одного места — один отпечаток, независимо от
    текста (в тексте бывают id и суммы, и дроссель бы не сработал)."""
    frames = traceback.extract_tb(exc.__traceback__)
    last = f"{frames[-1].filename}:{frames[-1].lineno}" if frames else ""
    return f"{where}|{type(exc).__name__}|{last}"


def _should_send(key: str, now: float) -> tuple[bool, int]:
    """(слать ли, сколько раз проглочено с прошлого алерта)."""
    prev = _last_sent.get(key)
    if prev is not None and now - prev[0] < ALERT_INTERVAL_SEC:
        _last_sent[key] = (prev[0], prev[1] + 1)
        return False, 0
    if len(_last_sent) >= _MAX_FINGERPRINTS:
        # Не даём словарю расти бесконечно на потоке разных ошибок.
        oldest = min(_last_sent, key=lambda k: _last_sent[k][0])
        _last_sent.pop(oldest, None)
    _last_sent[key] = (now, 0)
    return True, (prev[1] if prev else 0)


def reset() -> None:
    """Сбросить дроссель (тесты)."""
    _last_sent.clear()


def _admin_ids() -> list[int]:
    try:
        from config import ADMIN_IDS

        return list(ADMIN_IDS or [])
    except Exception:  # noqa: BLE001 — конфиг без ADMIN_IDS не повод падать
        return []


async def report_exception(
    exc: BaseException, *, where: str, user_id: int | None = None
) -> bool:
    """Записать трассу и (с дросселем) сообщить админам. True — алерт ушёл."""
    logger.error(
        "Необработанная ошибка в %s (user_id=%s)", where, user_id,
        exc_info=(type(exc), exc, exc.__traceback__),
    )
    key = _fingerprint(exc, where)
    send, swallowed = _should_send(key, time.monotonic())
    if not send:
        return False
    admins = _admin_ids()
    if not admins:
        return False

    detail = redact_token(f"{type(exc).__name__}: {exc}")[:500]
    lines = [
        f"⚠️ <b>Ошибка</b> в <code>{esc(where)}</code>",
        f"<code>{esc(detail)}</code>",
    ]
    if user_id is not None:
        lines.append(f"Пользователь: <code>{int(user_id)}</code>")
    if swallowed:
        lines.append(f"Повторялась ещё {swallowed} раз за {ALERT_INTERVAL_SEC // 60} мин")
    lines.append("Трасса — в логах сервиса.")
    text = "\n".join(lines)

    from services import notifier

    delivered = False
    for admin_id in admins:
        try:
            delivered = bool(await notifier.tg_send_message(admin_id, text)) or delivered
        except Exception:  # noqa: BLE001 — алерт не имеет права ронять обработку ошибки
            logger.warning("Не удалось отправить алерт об ошибке админу %s", admin_id)
    return delivered


async def report_problem(title: str, lines: list[str], *, key: str) -> bool:
    """Проблема БЕЗ исключения (расхождение схемы, не та часовая зона) —
    ERROR в лог и (с тем же дросселем по `key`) сообщение админам.

    Отдельно от `report_exception`: там отпечаток строится по трассе, а у
    проверки на старте трассы нет — есть только вывод. Не бросает никогда:
    проверка, уронившая старт из-за неотправленного алерта, хуже самой проблемы.
    """
    logger.error("%s: %s", title, "; ".join(lines))
    try:
        send, swallowed = _should_send(f"problem|{key}", time.monotonic())
        if not send:
            return False
        admins = _admin_ids()
        if not admins:
            return False
        body = [f"⚠️ <b>{esc(title)}</b>"]
        body += [f"• {esc(redact_token(line))[:400]}" for line in lines[:15]]
        if len(lines) > 15:
            body.append(f"… и ещё {len(lines) - 15}")
        if swallowed:
            body.append(f"Повторялось ещё {swallowed} раз за {ALERT_INTERVAL_SEC // 60} мин")
        text = "\n".join(body)

        from services import notifier

        delivered = False
        for admin_id in admins:
            try:
                delivered = bool(await notifier.tg_send_message(admin_id, text)) or delivered
            except Exception:  # noqa: BLE001 — алерт best-effort
                logger.warning("Не удалось отправить алерт «%s» админу %s", title, admin_id)
        return delivered
    except Exception:  # noqa: BLE001 — см. докстринг
        logger.exception("Алерт «%s» не отправлен", title)
        return False
