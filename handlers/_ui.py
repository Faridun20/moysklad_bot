"""
Общие UI-приёмы для inline-кнопок бота (T3.2).

Проблема, которую решает модуль: после успешного действия клавиатура
оставалась в сообщении, и её можно было нажать повторно. Сервер в большинстве
случаев идемпотентен (CAS-переходы, idem_claim), поэтому второй эффект чаще
всего не наступал — но пользователь этого не знал: кнопка выглядела рабочей,
ответа не было или приходила ошибка «уже обработано». Отдельная беда —
списки-пикеры (лимиты, курсы, цены): они переживали вход в FSM, и второй тап
переключал контекст ввода на другого агента посередине диалога.

Правило: после успеха — снять клавиатуру и пометить в самом сообщении, ЧТО
и КОГДА произошло, чтобы в истории чата было видно решение.

`note` во всех функциях уходит с `parse_mode="HTML"` — пользовательский ввод
в нём оборачивай в `utils.helpers.esc()`.
"""

import logging

from aiogram.types import CallbackQuery

from utils.helpers import local_now

logger = logging.getLogger(__name__)


def _stamp() -> str:
    return local_now().strftime("%H:%M")


async def finish_card(call: CallbackQuery, note: str, *, keep_text: bool = True) -> None:
    """Пометить сообщение результатом и снять клавиатуру.

    `note` — короткая пометка («✅ Одобрено», «↩️ На доработку»); время
    подставляется само. `keep_text=False` — заменить текст целиком (для
    коротких подтверждений, где исходный текст уже не нужен).

    Ошибки Telegram глушим осознанно: сообщение могло быть удалено, слишком
    старое для правки или уже отредактировано параллельным обработчиком.
    Действие в БД к этому моменту УЖЕ выполнено — падать из-за косметики
    нельзя, но и молчать не будем (лог на debug: это ожидаемая ситуация).
    """
    msg = getattr(call, "message", None)
    if msg is None:
        return
    line = f"{note} · {_stamp()}"
    try:
        if keep_text and getattr(msg, "html_text", None):
            await msg.edit_text(f"{msg.html_text}\n\n{line}", parse_mode="HTML")
        elif keep_text and getattr(msg, "text", None):
            await msg.edit_text(f"{msg.text}\n\n{line}")
        else:
            await msg.edit_text(line)
    except Exception as e:
        # Не смогли отредактировать — хотя бы снимем клавиатуру.
        logger.debug("finish_card: edit_text не удался (%s), убираем клавиатуру", e)
        try:
            await msg.edit_reply_markup(reply_markup=None)
        except Exception:
            logger.debug("finish_card: клавиатуру снять тоже не удалось", exc_info=True)


async def replace_keyboard(call: CallbackQuery, note: str, markup) -> None:
    """Пометить результат и ЗАМЕНИТЬ клавиатуру (а не снять целиком).

    Нужно там, где карточка ведёт многошаговый процесс: «Товар получен»
    отработал, но «Подтвердить возврат» на той же карточке должен остаться —
    иначе после T2.8 (подтверждение требует приёмки) босс окажется без кнопки
    и пойдёт искать возврат заново.
    """
    msg = getattr(call, "message", None)
    if msg is None:
        return
    line = f"{note} · {_stamp()}"
    try:
        base = getattr(msg, "html_text", None) or getattr(msg, "text", "") or ""
        await msg.edit_text(f"{base}\n\n{line}", parse_mode="HTML", reply_markup=markup)
    except Exception as e:
        logger.debug("replace_keyboard: edit_text не удался (%s)", e)
        try:
            await msg.edit_reply_markup(reply_markup=markup)
        except Exception:
            logger.debug("replace_keyboard: замена клавиатуры не удалась", exc_info=True)


async def finish_message(bot, chat_id, message_id, note: str) -> bool:
    """То же, что finish_card, но по chat_id/message_id.

    Для FSM-сценариев: кнопку нажали в одном сообщении, а результат стал
    известен после ввода текста — исходную карточку надо погасить и пометить по
    сохранённым в state идентификаторам. Пометка уходит ответом на карточку,
    чтобы решение читалось рядом с заявкой, а не отдельной строкой в чате.

    Текст карточки не трогаем: по id его не прочитать, а держать копию в state
    ради косметики не стоит.

    Возвращает True, если пометка доставлена. False — карточки уже нет (или id
    не сохранились): вызывающий обязан отчитаться пользователю сам, иначе
    операция пройдёт молча.
    """
    if not chat_id or not message_id:
        return False
    try:
        await bot.edit_message_reply_markup(
            chat_id=chat_id, message_id=message_id, reply_markup=None
        )
    except Exception:
        # Не смертельно: клавиатуру могли снять раньше (вход в FSM) или
        # сообщение уже удалено — пометку всё равно пробуем доставить.
        logger.debug("finish_message: клавиатуру снять не удалось", exc_info=True)
    try:
        await bot.send_message(
            chat_id,
            f"{note} · {_stamp()}",
            parse_mode="HTML",
            reply_to_message_id=message_id,
        )
        return True
    except Exception:
        logger.debug("finish_message: пометку отправить не удалось", exc_info=True)
        return False


async def drop_keyboard(call: CallbackQuery) -> None:
    """Просто снять клавиатуру, не трогая текст.

    Для списков-пикеров при входе в FSM: сам список остаётся полезным
    контекстом («что я выбирал»), а вот кнопки должны перестать работать —
    иначе второй тап переключит выбранного агента посреди ввода суммы.
    """
    msg = getattr(call, "message", None)
    if msg is None:
        return
    try:
        await msg.edit_reply_markup(reply_markup=None)
    except Exception:
        logger.debug("drop_keyboard: клавиатуру снять не удалось", exc_info=True)
