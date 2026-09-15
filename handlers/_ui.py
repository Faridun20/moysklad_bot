"""
Общие UI-приёмы для inline-кнопок бота (T3.2).

Проблема, которую решает модуль: после успешного действия клавиатура
оставалась в сообщении, и её можно было нажать повторно. Сервер в большинстве
случаев идемпотентен (CAS-переходы, idem_claim), поэтому второй эффект чаще
всего не наступал — но пользователь этого не знал: кнопка выглядела рабочей,
ответа не было или приходила ошибка «уже обработано». Отдельная беда —
списки-пикеры (лимиты, курсы, цены): они переживали вход в FSM, и второй тап
переключал контекст ввода на другого агента посередине диалога.

Правило: после успеха — пометить в самом сообщении, ЧТО и КОГДА произошло,
чтобы в истории чата было видно решение, а кнопки решения заменить.

Bot API 10.3 (aiogram 3.31) дал неактивные кнопки (`InlineKeyboardButton.
disabled`). Кнопки решения теперь не исчезают молча, а превращаются в
неактивную строку с исходом («✅ Одобрено · Фаридун 14:05») — там же, куда
человек смотрит, чтобы нажать. Нажать её нельзя: callback Telegram не шлёт
вовсе. Пометка ТЕКСТОМ в сообщении остаётся — это история, и клиент без
поддержки 10.3 увидит решение хотя бы так. Старые карточки с живыми кнопками
(отправленные до выката, или решённые в WebApp — id их сообщений мы не храним)
хендлеры по-прежнему обязаны переживать: на «уже обработано» они гасят
карточку тем же исходом (`settle_markup`).

`note` во всех функциях уходит с `parse_mode="HTML"` — пользовательский ввод
в нём оборачивай в `utils.helpers.esc()`.
"""

import logging

from aiogram.types import CallbackQuery, InlineKeyboardMarkup, WebAppInfo
from aiogram.utils.keyboard import InlineKeyboardBuilder

from config import WEBAPP_URL
from utils.helpers import local_now

# Сборщики клавиатур без Telegram-вызовов живут в utils.keyboards — их зовут и
# сервисы (уведомление об одобрении), которым handlers импортировать нельзя.
from utils.keyboards import (  # noqa: F401 — реэкспорт для хендлеров
    disabled_button,
    prompt_keyboard,
    settle_markup,
    status_keyboard,
    webapp_screen_url,
)

logger = logging.getLogger(__name__)


def webapp_keyboard(
    text: str = "🌐 Открыть WebApp", *, menu: bool = True, screen: str | None = None
) -> InlineKeyboardMarkup | None:
    """Кнопка входа в WebApp (+ «🏠 Меню»).

    T3.3: «что дальше» после решения ведёт в WebApp — списки заявок/сдач/
    возвратов из бота вырезаны, и старые callback'и (`ord_requests`,
    `dep_pending`, `ret_pending`, `debts_my`) больше никем не обрабатываются:
    кнопка на них висела бы без ответа.

    web_app-кнопку Telegram принимает только с https-URL, поэтому при пустом
    или локальном WEBAPP_URL остаётся одно «Меню» (а если и его не просят —
    None: пустой markup Bot API отвергает).

    `screen` — открыть сразу нужный экран (`"decisions"` — «Решения»
    руководителя), см. `utils.keyboards.webapp_screen_url`.
    """
    kb = InlineKeyboardBuilder()
    if WEBAPP_URL and WEBAPP_URL.startswith("https://"):
        url = webapp_screen_url(screen, base=WEBAPP_URL) if screen else WEBAPP_URL
        kb.button(text=text, web_app=WebAppInfo(url=url))
    if menu:
        kb.button(text="🏠 Меню", callback_data="menu")
    markup = kb.as_markup()
    return markup if markup.inline_keyboard else None


def _stamp() -> str:
    return local_now().strftime("%H:%M")


_NAME_MAX = 16


def actor_name(user) -> str:
    """Короткое имя того, кто нажал: в кнопку влезает имя, а не ФИО."""
    first = (getattr(user, "first_name", None) or "").strip()
    if not first:
        full = (getattr(user, "full_name", None) or "").strip()
        first = full.split()[0] if full else str(getattr(user, "id", "") or "")
    if len(first) > _NAME_MAX:
        first = first[: _NAME_MAX - 1] + "…"
    return first


def outcome_label(verb: str, user=None) -> str:
    """«✅ Одобрено · Фаридун 14:05» — исход для неактивной кнопки."""
    who = f"{actor_name(user)} " if user is not None else ""
    return f"{verb} · {who}{_stamp()}"


async def set_message_markup(bot, chat_id, message_id, markup) -> bool:
    """Поменять клавиатуру сообщения по id. Не бросает: косметика."""
    if not chat_id or not message_id:
        return False
    try:
        await bot.edit_message_reply_markup(
            chat_id=chat_id, message_id=message_id, reply_markup=markup
        )
        return True
    except Exception:
        logger.debug("set_message_markup: не удалось", exc_info=True)
        return False


async def settle_card(
    call: CallbackQuery,
    callbacks: set[str] | frozenset[str],
    label: str,
    *,
    tail: InlineKeyboardMarkup | None = None,
) -> None:
    """Погасить кнопки карточки исходом, не трогая текст.

    Для «уже обработано»: карточку решили в WebApp или другой руководитель
    на своей копии, а эта осталась с живыми кнопками. Раньше человек получал
    алерт и ту же живую клавиатуру — и жал снова.
    """
    msg = getattr(call, "message", None)
    if msg is None:
        return
    markup = settle_markup(getattr(msg, "reply_markup", None), callbacks, label, tail=tail)
    try:
        await msg.edit_reply_markup(reply_markup=markup)
    except Exception:
        logger.debug("settle_card: клавиатуру заменить не удалось", exc_info=True)


async def finish_card(
    call: CallbackQuery,
    note: str,
    *,
    keep_text: bool = True,
    outcome: str | None = None,
) -> None:
    """Пометить сообщение результатом и снять клавиатуру.

    `outcome` — вместо кнопок оставить неактивную строку с исходом.

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
    markup = status_keyboard(outcome) if outcome else None
    try:
        if keep_text and getattr(msg, "html_text", None):
            await msg.edit_text(
                f"{msg.html_text}\n\n{line}", parse_mode="HTML", reply_markup=markup
            )
        elif keep_text and getattr(msg, "text", None):
            await msg.edit_text(f"{msg.text}\n\n{line}", reply_markup=markup)
        else:
            await msg.edit_text(line, reply_markup=markup)
    except Exception as e:
        # Не смогли отредактировать — хотя бы снимем клавиатуру.
        logger.debug("finish_card: edit_text не удался (%s), убираем клавиатуру", e)
        try:
            await msg.edit_reply_markup(reply_markup=markup)
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


async def finish_message(
    bot, chat_id, message_id, note: str, *, outcome: str | None = None
) -> bool:
    """То же, что finish_card, но по chat_id/message_id.

    Для FSM-сценариев: кнопку нажали в одном сообщении, а результат стал
    известен после ввода текста — исходную карточку надо погасить и пометить по
    сохранённым в state идентификаторам. Пометка уходит ответом на карточку,
    чтобы решение читалось рядом с заявкой, а не отдельной строкой в чате.

    Текст карточки не трогаем: по id его не прочитать, а держать копию в state
    ради косметики не стоит. `outcome` — исход неактивной кнопкой на карточке
    (вместо пустой клавиатуры).

    Возвращает True, если пометка доставлена. False — карточки уже нет (или id
    не сохранились): вызывающий обязан отчитаться пользователю сам, иначе
    операция пройдёт молча.
    """
    if not chat_id or not message_id:
        return False
    try:
        await bot.edit_message_reply_markup(
            chat_id=chat_id,
            message_id=message_id,
            reply_markup=status_keyboard(outcome) if outcome else None,
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


async def drop_keyboard(call: CallbackQuery, *, status: str | None = None) -> None:
    """Снять клавиатуру, не трогая текст.

    Для карточек при входе в FSM: сама карточка остаётся полезным контекстом,
    а вот кнопки должны перестать работать — иначе ту же заявку можно одобрить,
    пока вводится причина возврата. `status` — вместо пустоты оставить
    неактивную строку («✍️ Ждём причину…»): видно, почему кнопок нет.
    """
    msg = getattr(call, "message", None)
    if msg is None:
        return
    try:
        await msg.edit_reply_markup(reply_markup=status_keyboard(status) if status else None)
    except Exception:
        logger.debug("drop_keyboard: клавиатуру снять не удалось", exc_info=True)
