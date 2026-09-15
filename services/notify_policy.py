"""
Политика уведомлений РУКОВОДИТЕЛЮ (боссу): что шлём отдельной карточкой
СРАЗУ, а что копится в вечерний дайджест (`services.boss_digest`,
`tasks/run_boss_digest.py`).

Решение владельца (сентябрь 2026): раньше босс получал отдельное сообщение
на КАЖДУЮ заявку, платёж, сдачу наличных и возврат — вал уведомлений, за
которым стало легко пропустить действительно важное. Новое правило:

* **Сразу** — решения, которые блокируют работу менеджера (заявка на
  отгрузку ждёт одобрения — `ORDER_REQUEST`), одобрения сделок по технике
  (`MACHINE_DEAL_APPROVAL` — путь machine-approval, см. докстринг ниже) и
  ЛЮБОЕ денежное событие (платёж, сдача наличных, возврат) на сумму ≥
  `app_settings.boss_instant_threshold_usd` (курс — ТЕКУЩИЙ из
  `currency_rates`, а не замороженный на момент операции: порог должен
  реагировать на курс сегодня, а не курс, который уже неактуален).
* **Вечерний дайджест** — всё остальное: платежи/сдачи/возвраты меньше
  порога. Карточка-решение по ним боссу НЕ шлётся точечно; они остаются
  pending в своих таблицах (`payments`/`cash_deposits`/`returns`) и
  собираются `services.boss_digest` одним сообщением в day-end.

`should_notify_now` — ЕДИНАЯ точка этого решения: все места, которые раньше
слали боссу пуш напрямую (`services/notify.py`, `handlers/returns.py`,
`webapp/server.py`, `services/database.get_deposit_confirmers`), проверяют
её ПЕРЕД отправкой. Менеджерские уведомления (об одобрении/отклонении СВОЕЙ
заявки, о принятом/отклонённом СВОЕМ платеже) политика не трогает — это
не про боссовский вал, они как были персональными, так и остались.

**Пропавший курс — считаем «сразу».** `usd_equivalent` возвращает None,
если валюта платежа не USD и курса в `currency_rates` нет
(`services.database.convert_to_base`). Тогда `should_notify_now` отвечает
True: лучше лишний пуш, чем потерянное решение по крупной, возможно, сумме
— конвертировать её в USD мы всё равно не можем, а «списать в дайджест»
означает рискнуть эту сумму не заметить.

**API для параллельных агентов (machine-approval).** Одобрение сделки по
технике — как заявка на отгрузку: решение блокирует продажу, поэтому оно
ВСЕГДА немедленное и порогом не режется. Уведомление сделки по технике
отправляйте как раньше (`services.notifier.get_notify_recipients()` +
`bot.send_message`/`tg_send_message`) — точку входа менять не нужно.
`should_notify_now(MACHINE_DEAL_APPROVAL)` вызывать не обязательно (она
всегда вернёт True), но можно — на случай, если правило когда-нибудь
станет условным, чтобы не искать все вызовы по коду заново.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# ─── Виды событий ──────────────────────────────────────────────────────────
# Первые два блокируют работу — порог к ним не применяется.
ORDER_REQUEST = "order_request"
MACHINE_DEAL_APPROVAL = "machine_deal_approval"
# Денежные события — режутся порогом `boss_instant_threshold_usd`.
PAYMENT = "payment"
CASH_DEPOSIT = "cash_deposit"
RETURN = "return"

_ALWAYS_IMMEDIATE = frozenset({ORDER_REQUEST, MACHINE_DEAL_APPROVAL})
_MONEY_KINDS = frozenset({PAYMENT, CASH_DEPOSIT, RETURN})

DEFAULT_THRESHOLD_USD = 5000.0


def usd_equivalent(amount: float | None, currency: str | None) -> float | None:
    """Сумма в USD по ТЕКУЩЕМУ курсу `currency_rates`. None — валюта не USD и
    курса нет (админ его не завёл / БД недоступна): вызывающий обязан
    трактовать это явно, а не молча посчитать по курсу 1.0.

    BASE_CURRENCY в проекте — USD по умолчанию (`config.BASE_CURRENCY`), и
    именно от него зависит `database.convert_to_base`. Если однажды
    BASE_CURRENCY станет НЕ USD — прямого курса «валюта → USD» в схеме нет
    (только «валюта → BASE_CURRENCY»), и функция тоже отвечает None
    (тот же фолбэк «считаем сразу», см. докстринг модуля).
    """
    if amount is None:
        return None
    code = (currency or "USD").upper()
    if code == "USD":
        try:
            return float(amount)
        except (TypeError, ValueError):
            return None

    from config import BASE_CURRENCY

    if (BASE_CURRENCY or "USD").upper() != "USD":
        logger.warning(
            "usd_equivalent(%s, %s): BASE_CURRENCY=%s ≠ USD — прямого курса "
            "к USD нет, считаем «сразу» (None)",
            amount, currency, BASE_CURRENCY,
        )
        return None

    from services.database import convert_to_base

    return convert_to_base(amount, code)


def instant_threshold_usd() -> float:
    """Порог `boss_instant_threshold_usd` (USD) из app_settings, дефолт 5000."""
    from services.database import get_setting

    try:
        return float(get_setting("boss_instant_threshold_usd", DEFAULT_THRESHOLD_USD))
    except (TypeError, ValueError):
        return DEFAULT_THRESHOLD_USD


def should_notify_now(
    kind: str, amount: float | None = None, currency: str | None = None
) -> bool:
    """True — боссу шлём отдельную карточку СЕЙЧАС. False — событие остаётся
    pending в своей таблице и попадёт в вечерний дайджест.

    `kind` — один из `ORDER_REQUEST`/`MACHINE_DEAL_APPROVAL`/`PAYMENT`/
    `CASH_DEPOSIT`/`RETURN`. Для денежных `kind` `amount`/`currency`
    обязательны по смыслу (без суммы порог не с чем сравнивать — вернёт
    True, как и при отсутствующем курсе).
    """
    if kind in _ALWAYS_IMMEDIATE:
        return True
    if kind not in _MONEY_KINDS:
        # Неизвестный kind — лучше лишний пуш, чем тихо проглоченное решение.
        logger.warning("should_notify_now: неизвестный kind=%r — считаю «сразу»", kind)
        return True
    usd = usd_equivalent(amount, currency)
    if usd is None:
        return True
    return usd >= instant_threshold_usd()
