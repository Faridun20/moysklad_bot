"""
Доставка PDF расходной накладной клиенту в Telegram.

Отдельный модуль, потому что это другая ответственность: `warehouse` знает
про остатки и не должен знать про Telegram, `invoice_pdf` рисует документ и
не должен знать, кому его слать.

Железное правило: доставка НИКОГДА не роняет накладную. Накладная уже
проведена и остатки списаны — ошибка отправки (клиент заблокировал бота,
Telegram лежит, weasyprint не собрался) обязана деградировать в
предупреждение менеджеру, а не в откат проведённого документа.
"""

from __future__ import annotations

import logging

from services import warehouse

logger = logging.getLogger(__name__)

# Причины, по которым PDF не ушёл. Фронт показывает их менеджеру текстом.
REASON_TEXT = {
    "not_outgoing": "Документ клиенту отправляют только по отгрузке",
    "no_counterparty": "В документе не указан клиент — некому отправить",
    "no_telegram_id": "У клиента не привязан Telegram — отправьте документ вручную",
    "already_sent": "Документ по этой отгрузке уже отправлен клиенту",
    "render_failed": "Не удалось собрать документ — попробуйте ещё раз",
    "send_failed": "Не удалось отправить документ в Telegram — попробуйте ещё раз",
}


async def deliver_invoice_pdf(invoice: dict, bot, *, force: bool = False, lang: str | None = None) -> dict:
    """Собрать PDF и отправить клиенту. Не бросает исключений никогда.

    Возвращает {"sent": bool, "reason": str|None}. reason — ключ из
    REASON_TEXT; вызывающий показывает его менеджеру как предупреждение.

    force=True — повторная отправка по кнопке «Отправить ещё раз»: снимает
    защиту от дубля (telegram_sent). lang — язык товарной накладной
    (`ru_uz`/`ru`/`uz`); не передан — рус + узб.
    """
    invoice_id = invoice.get("id")

    if invoice.get("type") != "outgoing":
        return {"sent": False, "reason": "not_outgoing"}
    if not invoice.get("counterparty_id"):
        return {"sent": False, "reason": "no_counterparty"}
    if invoice.get("telegram_sent") and not force:
        return {"sent": False, "reason": "already_sent"}

    chat_id = invoice.get("counterparty_telegram_id")
    if not chat_id:
        # Штатный случай по ТЗ: накладная сохранена, остатки списаны,
        # менеджер видит предупреждение и отправляет позже руками.
        return {"sent": False, "reason": "no_telegram_id"}

    if bot is None:
        return {"sent": False, "reason": "send_failed"}

    from services import waybill

    try:
        # WeasyPrint синхронный и тяжёлый (сотни миллисекунд CPU): `waybill.render`
        # рендерит в потоке, иначе на время сборки встаёт весь event loop.
        pdf_bytes, filename = await waybill.render(invoice, lang)
    except Exception:
        logger.exception("Не удалось собрать PDF накладной #%s", invoice_id)
        return {"sent": False, "reason": "render_failed"}

    try:
        from aiogram.types import BufferedInputFile

        document = BufferedInputFile(pdf_bytes, filename=filename)
        await bot.send_document(
            chat_id=int(chat_id),
            document=document,
            caption=f"Товарная накладная № {invoice.get('invoice_number')}",
        )
    except Exception:
        logger.exception(
            "Не удалось отправить PDF накладной #%s клиенту %s", invoice_id, chat_id
        )
        return {"sent": False, "reason": "send_failed"}

    try:
        await warehouse.mark_telegram_sent(int(invoice_id))
    except Exception:
        # PDF клиент уже получил. Не смогли записать отметку — это расхождение
        # в UI («не отправлено», хотя отправлено), но не повод объявлять
        # отправку неудачной: повтор прислал бы клиенту второй документ.
        logger.exception("PDF накладной #%s отправлен, но отметка не записана", invoice_id)

    logger.info("PDF накладной #%s отправлен клиенту %s", invoice_id, chat_id)
    return {"sent": True, "reason": None}
