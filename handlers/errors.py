"""
Глобальный обработчик ошибок бота (`dp.errors`).

Без него упавший хендлер оставлял человека в тишине: кнопка «крутится» до
таймаута, на сообщение нет ответа, и выглядит это как «бот сломался». Теперь
пользователь получает короткое «что-то пошло не так», трасса уходит в лог, а
админам — алерт с дросселем (`services.error_alerts`).

Отвечаем ТОЛЬКО на личные сообщения и нажатия кнопок. Business-апдейты
(переписка менеджера с клиентом, `handlers/business.py`) бот лишь наблюдает:
ответ туда ушёл бы клиенту менеджера от его имени.
"""

import logging

from aiogram.types import ErrorEvent

from services import error_alerts

logger = logging.getLogger(__name__)


def _user_id(update) -> int | None:
    for attr in ("message", "callback_query"):
        obj = getattr(update, attr, None)
        user = getattr(obj, "from_user", None) if obj is not None else None
        if user is not None:
            return int(user.id)
    return None


async def on_error(event: ErrorEvent) -> bool:
    update = event.update
    kind = next(
        (k for k in ("message", "callback_query", "business_message", "edited_message")
         if getattr(update, k, None) is not None),
        "update",
    )
    await error_alerts.report_exception(
        event.exception, where=f"bot {kind}", user_id=_user_id(update)
    )
    try:
        if update.callback_query is not None:
            await update.callback_query.answer(error_alerts.USER_MESSAGE, show_alert=True)
        elif update.message is not None:
            await update.message.answer(error_alerts.USER_MESSAGE)
    except Exception:  # noqa: BLE001 — ответ best-effort, ошибка уже в логе и у админов
        logger.warning("Не удалось сообщить пользователю об ошибке", exc_info=True)
    return True  # обработано: aiogram не пишет второй трейс
