"""
Наблюдатель переписок менеджеров (Telegram Business).

Бот подключён к личному аккаунту менеджера и ВИДИТ его переписку с клиентами,
но не участвует в ней: ничего не отвечает, не подсказывает и не отправляет от
его имени. Единственная задача — записать, что обращение было, и кто на него
отреагировал, чтобы воронка в WebApp показывала, сколько разговоров доходит до
сделки.

Три правила, которые здесь важнее удобства:

* **Ни одного исходящего сообщения.** В модуле нет ни `answer`, ни `send_*` —
  и это проверяется тестом. Бот, который однажды напишет клиенту от имени
  менеджера, разрушит доверие к самой затее.
* **Текст сообщения не покидает хендлер.** В сервис уходят только id, имя и
  направление; `message.text` не читается вовсе.
* **Неизвестное подключение игнорируем молча.** Апдейт от аккаунта, которого
  нет в нашей таблице, — не ошибка: менеджер мог подключить бота раньше, чем
  админ завёл его в системе.
"""

import logging

from aiogram import Router
from aiogram.types import BusinessConnection, Message

from services import leads

logger = logging.getLogger(__name__)
router = Router()


@router.business_connection()
async def on_business_connection(event: BusinessConnection) -> None:
    """Менеджер подключил (или отключил) бота в настройках Telegram Business."""
    rights = getattr(event, "rights", None)
    can_read = bool(getattr(rights, "can_read_messages", False)) if rights else False
    await leads.save_connection(
        event.id,
        manager_id=event.user.id,
        user_chat_id=getattr(event, "user_chat_id", None),
        is_enabled=bool(event.is_enabled),
        can_read=can_read,
    )
    logger.info(
        "business_connection %s: менеджер %s, включено=%s, чтение=%s",
        event.id, event.user.id, event.is_enabled, can_read,
    )
    if event.is_enabled and not can_read:
        # Без права читать сообщения воронка не наполнится, а выглядеть будет
        # как «клиенты не пишут». Пишем предупреждение, чтобы это было видно.
        logger.warning(
            "business_connection %s: нет права на чтение сообщений — воронка "
            "останется пустой. Менеджеру нужно разрешить его в настройках.",
            event.id,
        )


def _display_name(user) -> str:
    parts = [getattr(user, "first_name", "") or "", getattr(user, "last_name", "") or ""]
    return " ".join(p for p in parts if p).strip()


@router.business_message()
async def on_business_message(message: Message) -> None:
    """Сообщение в переписке менеджера. Записываем факт, не содержание."""
    connection = await leads.get_connection(message.business_connection_id or "")
    if not connection or not connection.get("is_enabled"):
        return

    manager_id = int(connection["manager_id"])
    sender = message.from_user
    if sender is None:
        return

    inbound = sender.id != manager_id
    # Собеседник — это чат: в личной переписке он же и клиент. Для исходящего
    # сообщения отправитель — менеджер, поэтому клиента берём из чата.
    client_id = message.chat.id if not inbound else sender.id
    if client_id == manager_id:
        # Заметки менеджера самому себе в воронку не идут.
        return

    client = sender if inbound else None
    res = await leads.record_message(
        tg_user_id=int(client_id),
        manager_id=manager_id,
        inbound=inbound,
        username=getattr(client, "username", None),
        display_name=_display_name(client) if client else None,
    )
    if res.get("reengaged"):
        logger.info("lead %s: клиент вернулся после паузы", res["lead_id"])
