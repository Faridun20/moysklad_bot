"""
Печать документов на офисный принтер: кнопка под печатной формой и `/printer`.

Почему в боте, а не в WebApp (T3.3): печать — физическое действие у принтера,
и решение принимается там же, где пришёл документ. Аналога в WebApp нет и не
нужно — это ops-кнопка на push-карточке, ровно тот класс, который в боте
остаётся.

Два решения:

* **Печатаем ПО КНОПКЕ.** Половина печатных форм уходит на проверку перед
  отправкой клиенту; печатать их все — переводить бумагу. Кнопка же даёт и
  контроль: видно, что именно и когда отправили в очередь.
* **PDF пересобираем в момент нажатия, а не храним файл между показом и
  печатью.** Путь в callback_data не влезает в 64 байта и позволил бы
  напечатать любой файл контейнера, подставив чужую строку; файл на диске
  переживает не каждый рестарт. По `invoice_id` документ собирается заново и
  заведомо соответствует текущей накладной.
"""

import logging

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from services import printing
from services.roles import can_view_stock
from utils.helpers import esc

logger = logging.getLogger(__name__)
router = Router()


async def _invoice_pdf(invoice_id: int, user_id: int | None = None) -> tuple[bytes, str, str] | None:
    """Накладная → (pdf, имя файла, подпись для очереди). `None` — не нашли."""
    import asyncio

    from services import warehouse
    from services.costing import redact_invoice
    from services.invoice_pdf import invoice_filename, render_invoice_pdf
    from services.roles import cached_role

    invoice = await warehouse.get_invoice(invoice_id)
    if invoice is None:
        return None
    # callback_data подделывается клиентом: закупочные цены прихода печатаем
    # только руководству — как и в WebApp (services.costing.redact_invoice).
    invoice = redact_invoice(invoice, cached_role(user_id) if user_id is not None else None)
    # WeasyPrint синхронный и небыстрый — уводим с event loop, как это делает
    # order_workflow._build_invoice_pdf.
    pdf = await asyncio.to_thread(render_invoice_pdf, invoice)
    number = str(invoice.get("invoice_number") or invoice_id)
    return pdf, invoice_filename(invoice), f"Накладная {number}"


async def _document_pdf(doc_id: int, user_id: int) -> tuple[bytes, str, str] | None:
    """Юридический документ → (pdf, имя файла, подпись). Читается из файла:
    расписка подписана один раз, пересобирать её с новой датой нельзя.

    Чужой документ (не руководству) — как несуществующий: callback_data
    подделывается клиентом, а в расписке паспорт должника."""
    import asyncio

    from services import documents
    from services.roles import cached_role

    doc = await documents.get_document(doc_id)
    if doc is None or not documents.can_access(doc, user_id, cached_role(user_id)):
        return None
    found = await asyncio.to_thread(documents.read_pdf, doc)
    if found is None:
        return None
    pdf, filename = found
    return pdf, filename, documents.caption_for(doc)


@router.callback_query(F.data.startswith(printing.CALLBACK_PREFIX))
async def cb_print(call: CallbackQuery):
    """«🖨 Распечатать» под документом."""
    if not can_view_stock(call.from_user.id):
        return await call.answer("Нет доступа", show_alert=True)

    parsed = printing.parse_callback(call.data or "")
    if parsed is None:
        return await call.answer("Кнопка устарела", show_alert=True)
    kind, ref = parsed
    if kind not in ("inv", "doc"):
        logger.warning("Печать: неизвестный тип документа %r", kind)
        return await call.answer("Неизвестный тип документа", show_alert=True)

    # Отвечаем СРАЗУ: сборка PDF и разговор с CUPS занимают секунды, а Telegram
    # держит «часики» на кнопке лишь до первого answer — без него клиент
    # показывает таймаут, и человек жмёт второй раз.
    await call.answer("Отправляю на печать…")

    try:
        doc = await (_invoice_pdf(ref, call.from_user.id) if kind == "inv" else _document_pdf(ref, call.from_user.id))
    except Exception:
        logger.exception("Печать: не удалось собрать PDF (%s #%s)", kind, ref)
        return await _report(call, "❌ Не удалось собрать документ для печати")

    if doc is None:
        return await _report(
            call, "❌ Накладная не найдена" if kind == "inv" else "❌ Файл документа не найден — сформируйте заново"
        )

    pdf_bytes, filename, label = doc
    result = await printing.print_pdf_bytes(
        pdf_bytes,
        filename=filename,
        label=f"{label} · {call.from_user.full_name or call.from_user.id}",
    )
    logger.info(
        "Печать накладной #%s пользователем %s: %s%s",
        ref, call.from_user.id, "принята" if result.ok else "отказ",
        "" if result.ok else f" ({result.error})",
    )

    if result.ok:
        return await _report(call, f"🖨 {esc(result.message)}")
    # Причина — текстом: «Ошибка» отправляет менеджера искать админа, а
    # «принтер не принимает задания» он решит сам, подойдя к принтеру.
    return await _report(call, f"❌ Ошибка печати: {esc(result.error)}")


async def _report(call: CallbackQuery, text: str) -> None:
    """Ответ в тот же тред. Документ остаётся с кнопкой: печать можно повторить
    (замяло бумагу, принтер был занят) — снимать её после одной попытки значит
    заставить переоткрывать заявку."""
    try:
        await call.message.reply(text, parse_mode="HTML")
    except Exception:
        # Сообщение могли удалить — тогда хотя бы всплывашкой.
        try:
            await call.answer(text[:190], show_alert=True)
        except Exception:
            logger.warning("Печать: не удалось показать результат пользователю")


@router.message(Command("printer"))
async def cmd_printer(message: Message, bot: Bot):
    """Состояние очереди печати.

    Нужна ровно для случая «бот сказал „отправлено“, а бумага не вышла»:
    задание принято очередью, но принтер стоит — и увидеть это можно только
    здесь.
    """
    if not can_view_stock(message.from_user.id):
        return

    status = await printing.printer_status()
    lines = [f"🖨 <b>Принтер {esc(printing.DEFAULT_PRINTER)}</b>", ""]
    if status.ok:
        lines.append(status.label)
        if status.text:
            lines.append(f"<code>{esc(status.text)}</code>")
        if status.state == "error":
            lines.append("")
            lines.append("Очередь остановлена: проверьте бумагу, тонер и питание.")
    else:
        lines.append("🔴 Не удалось получить статус")
        lines.append(f"<code>{esc(status.text)}</code>")
    await message.answer("\n".join(lines), parse_mode="HTML")
