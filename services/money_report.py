"""
Отчёт «Где деньги» в чат — Rich Message с таблицами.

Почему это вообще стало возможно. T3.3 увёл всю глубину в WebApp, потому что
длинный отчёт в чате был нечитаемой простынёй: у бота были только текст и
моноширинный блок, а колонки в нём разъезжаются на любом клиенте с другой
шириной экрана. Bot API 10.1 добавил Rich Messages — заголовки, списки,
разделители и НАСТОЯЩИЕ таблицы. Ограничение, из-за которого решение
принималось, снято, поэтому сводка «где деньги» уезжает в чат целиком.

Два правила, которые здесь важнее красоты:

* **Фолбэк обязателен.** Если `sendRichMessage` не прошёл (старый клиент,
  изменение API, отключённая фича), шлём тот же отчёт простым текстом через
  `tg_send_message`. Отчёт, который не дошёл, хуже некрасивого.
* **Кнопка и сворачиваемый список (Bot API 10.3).** Внутри статьи —
  кнопка в WebApp (`InputRichBlockButtons`) и полный список просроченных
  должников в сворачиваемой цитате: в чате он занимает одну строку, пока его
  не развернули. Текстовый фолбэк несёт то же самое своими средствами:
  `<blockquote expandable>` и web_app-кнопку в reply_markup.
* **Пустой отчёт не отправляется.** Еженедельное «долгов нет» превращает
  сводку в шум, который перестают читать — а вместе с ней перестают читать и
  ту неделю, когда цифры важны.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

from services import money, receivables
from utils.helpers import esc, local_now, redact_token

logger = logging.getLogger(__name__)


def _fmt(block: dict) -> str:
    """Сумма блока для отчёта: одна валюта — как есть, несколько — к базовой."""
    if not block or not block.get("count"):
        return "—"
    rows = block.get("by_currency") or []
    if len(rows) == 1:
        return f"{money.format_cents(money.to_cents(rows[0]['total']), decimals=0, sep=' ')} {rows[0]['currency']}"
    if block.get("base_total") is None:
        return " · ".join(
            f"{money.format_cents(money.to_cents(r['total']), decimals=0, sep=' ')} {r['currency']}"
            for r in rows
        )
    tail = " (часть без курса)" if block.get("partial") else ""
    base = money.format_cents(money.to_cents(block["base_total"]), decimals=0, sep=" ")
    return f"≈ {base} {block.get('base_currency', 'USD')}{tail}"


# Сколько просроченных должников показывать в сворачиваемом списке. Больше —
# «…и ещё N»: развёрнутая цитата на сотню строк снова становится простынёй.
OVERDUE_LIST_MAX = 30

WEBAPP_BUTTON_TEXT = "💰 Долги — в WebApp"


def _webapp_url() -> str | None:
    """https-адрес WebApp или None: web_app-кнопку с другим URL Telegram
    отвергает вместе со всем сообщением."""
    import config

    url = config.WEBAPP_URL or ""
    return url if url.startswith("https://") else None


def overdue_debtors(items: list, today: date) -> dict:
    """Просроченные должники: имя, сумма, сколько дней самый старый срок.

    Порядок — по сумме (как «кто должен больше всех»), дни рядом: долг на
    5 дней и на 5 месяцев выглядят в списке одинаково, если их не подписать.
    """
    late = [r for r in items if receivables.bucket_of(r.due_date, today) != "not_due"]
    rows = receivables.by_counterparty(late, limit=len(late) or 1) if late else []
    oldest: dict[str, int] = {}
    for r in late:
        days = (today - date.fromisoformat(str(r.due_date)[:10])).days
        oldest[r.counterparty] = max(oldest.get(r.counterparty, 0), days)
    for row in rows:
        row["days"] = oldest.get(row["name"], 0)
    return {"rows": rows[:OVERDUE_LIST_MAX], "total": len(rows)}


async def gather(period_days: int = 7) -> dict:
    """Данные отчёта. Отдельно от рендера — так их можно проверить тестом, не
    собирая Telegram-разметку."""
    today = local_now().date()
    since = today - timedelta(days=period_days)
    items = await receivables.collect()
    stats = await receivables.collection_stats(since.isoformat(), today.isoformat())
    return {
        "since": since.isoformat(),
        "until": today.isoformat(),
        "totals": receivables.totals_by_source(items),
        "aging": receivables.aging(items, today),
        "forecast": receivables.forecast(items, months=3, today=today),
        "discipline": stats,
        "top": receivables.by_counterparty(items, limit=5),
        "overdue": overdue_debtors(items, today),
    }


def is_empty(data: dict) -> bool:
    """Нечего показывать — нет ни долгов, ни ожидавшихся за период платежей."""
    return not data["totals"]["all"]["count"] and not data["discipline"]["expected_count"]


def _period_label(data: dict) -> str:
    def ru(iso: str) -> str:
        y, m, d = iso.split("-")
        return f"{d}.{m}"

    return f"{ru(data['since'])}—{ru(data['until'])}"


def build_blocks(data: dict) -> list:
    """Rich-блоки отчёта: заголовок, таблица сроков, просроченные, дисциплина,
    должники, кнопка в WebApp."""
    from aiogram.types import (
        InputRichBlockButtons,
        InputRichBlockDivider,
        InputRichBlockExpandableBlockQuotation,
        InputRichBlockList,
        InputRichBlockListItem,
        InputRichBlockParagraph,
        InputRichBlockSectionHeading,
        InputRichBlockTable,
        RichBlockTableCell,
        RichMessageButton,
        WebAppInfo,
    )

    def cell(text: str, *, header: bool = False, right: bool = False) -> RichBlockTableCell:
        return RichBlockTableCell(
            align="right" if right else "left",
            valign="middle",
            text=str(text),
            is_header=header or None,
        )

    blocks: list = [
        InputRichBlockSectionHeading(text=f"Где деньги · {_period_label(data)}", size=1),
        InputRichBlockParagraph(text=f"Нам должны {_fmt(data['totals']['all'])}"),
    ]
    by_source = []
    if data["totals"]["orders"]["count"]:
        by_source.append(f"по заказам {_fmt(data['totals']['orders'])}")
    if data["totals"]["machines"]["count"]:
        by_source.append(f"по технике {_fmt(data['totals']['machines'])}")
    if by_source:
        blocks.append(InputRichBlockParagraph(text=" · ".join(by_source)))

    rows = [[cell("Срок", header=True), cell("Сумма", header=True, right=True),
             cell("Долгов", header=True, right=True)]]
    for b in data["aging"]["buckets"]:
        # Пустую корзину в таблицу не кладём: в чате её строка занимает место
        # ровно столько же, сколько содержательная, а смысла не несёт.
        if not b["count"]:
            continue
        rows.append([cell(b["label"]), cell(_fmt(b), right=True), cell(str(b["count"]), right=True)])
    if len(rows) > 1:
        # is_compact (10.3): три узкие колонки — без него таблица на телефоне
        # занимала пол-экрана полями ячеек.
        blocks.append(InputRichBlockTable(
            cells=rows, is_bordered=True, is_striped=True, is_compact=True,
        ))

    overdue_lines = _overdue_lines(data)
    if overdue_lines:
        # Полный список — в СВОРАЧИВАЕМОЙ цитате (10.3): свёрнутый он не
        # отодвигает остальной отчёт, а за именами не надо идти в WebApp.
        blocks.append(InputRichBlockSectionHeading(
            text=f"Просрочено — кто ({data['overdue']['total']})", size=3,
        ))
        blocks.append(InputRichBlockExpandableBlockQuotation(text="\n".join(overdue_lines)))

    disc = data["discipline"]
    if disc["expected_count"]:
        blocks.append(InputRichBlockDivider())
        blocks.append(InputRichBlockSectionHeading(text="Платят ли вовремя", size=2))
        share = "—" if disc["on_time_share"] is None else f"{round(disc['on_time_share'] * 100)}%"
        blocks.append(InputRichBlockParagraph(
            text=f"Собрано {_fmt(disc['collected'])} из {_fmt(disc['expected'])}"
        ))
        blocks.append(InputRichBlockParagraph(
            text=f"В срок {disc['on_time_count']} из {disc['paid_count']} платежей · {share}"
        ))
        if disc["laggards"]:
            blocks.append(InputRichBlockSectionHeading(text="Систематически задерживают", size=3))
            blocks.append(InputRichBlockList(items=[
                InputRichBlockListItem(blocks=[InputRichBlockParagraph(
                    text=f"{lag['name']} — {lag['late']} из {lag['total']} платежей"
                )])
                for lag in disc["laggards"][:5]
            ]))

    forecast_rows = [m for m in data["forecast"] if m["count"]]
    if forecast_rows:
        blocks.append(InputRichBlockDivider())
        blocks.append(InputRichBlockSectionHeading(text="Ждём поступлений", size=2))
        blocks.append(InputRichBlockList(items=[
            InputRichBlockListItem(blocks=[InputRichBlockParagraph(
                text=f"{m['month']} — {_fmt(m)}"
            )])
            for m in forecast_rows
        ]))

    if data["top"]:
        blocks.append(InputRichBlockDivider())
        blocks.append(InputRichBlockSectionHeading(text="Кто должен больше всех", size=2))
        blocks.append(InputRichBlockList(items=[
            InputRichBlockListItem(blocks=[InputRichBlockParagraph(
                text=f"{t['name']} — {_fmt(t)}"
            )])
            for t in data["top"]
        ]))

    url = _webapp_url()
    if url:
        blocks.append(InputRichBlockButtons(buttons=[
            RichMessageButton(text=WEBAPP_BUTTON_TEXT, web_app=WebAppInfo(url=url)),
        ]))
    return blocks


def _overdue_lines(data: dict) -> list[str]:
    """Строки списка просроченных — общие для Rich и текста (без HTML)."""
    block = data.get("overdue") or {}
    lines = [
        f"{row['name']} — {_fmt(row)} · {row['days']} дн."
        for row in block.get("rows") or []
    ]
    rest = int(block.get("total") or 0) - len(lines)
    if lines and rest > 0:
        lines.append(f"…и ещё {rest}")
    return lines


def webapp_reply_markup() -> dict | None:
    """Кнопка в WebApp для текстового фолбэка (Bot API JSON: его шлёт
    `tg_send_message`, не aiogram)."""
    url = _webapp_url()
    if not url:
        return None
    return {"inline_keyboard": [[{"text": WEBAPP_BUTTON_TEXT, "web_app": {"url": url}}]]}


def build_text(data: dict) -> str:
    """Тот же отчёт простым текстом — фолбэк, если Rich Message не прошёл.

    Пользовательские имена экранируются: сообщение уходит с parse_mode=HTML, и
    контрагент «ООО <Строй>» иначе рушит разметку целиком.
    """
    lines = [f"📊 <b>Где деньги · {_period_label(data)}</b>", ""]
    lines.append(f"Нам должны: <b>{esc(_fmt(data['totals']['all']))}</b>")
    if data["totals"]["orders"]["count"]:
        lines.append(f"  по заказам: {esc(_fmt(data['totals']['orders']))}")
    if data["totals"]["machines"]["count"]:
        lines.append(f"  по технике: {esc(_fmt(data['totals']['machines']))}")

    buckets = [b for b in data["aging"]["buckets"] if b["count"]]
    if buckets:
        lines.append("")
        for b in buckets:
            lines.append(f"  {esc(b['label'])}: <b>{esc(_fmt(b))}</b> ({b['count']})")

    overdue_lines = _overdue_lines(data)
    if overdue_lines:
        lines.append("")
        lines.append(f"<b>Просрочено — кто ({data['overdue']['total']})</b>")
        body = "\n".join(esc(line) for line in overdue_lines)
        lines.append(f"<blockquote expandable>{body}</blockquote>")

    disc = data["discipline"]
    if disc["expected_count"]:
        share = "—" if disc["on_time_share"] is None else f"{round(disc['on_time_share'] * 100)}%"
        lines.append("")
        lines.append(f"Собрано {esc(_fmt(disc['collected']))} из {esc(_fmt(disc['expected']))}")
        lines.append(f"В срок {disc['on_time_count']} из {disc['paid_count']} · {share}")
        for lag in disc["laggards"][:5]:
            lines.append(f"  ⚠️ {esc(lag['name'])} — {lag['late']} из {lag['total']}")

    # Фолбэк несёт те же разделы, что и Rich-версия: он должен заменять отчёт,
    # а не быть его огрызком.
    forecast_rows = [m for m in data["forecast"] if m["count"]]
    if forecast_rows:
        lines.append("")
        lines.append("<b>Ждём поступлений</b>")
        for m in forecast_rows:
            lines.append(f"  {esc(m['month'])} — {esc(_fmt(m))}")

    if data["top"]:
        lines.append("")
        lines.append("<b>Кто должен больше всех</b>")
        for t in data["top"]:
            lines.append(f"  {esc(t['name'])} — {esc(_fmt(t))}")

    lines.append("")
    lines.append("<i>Подробнее — WebApp → «Деньги» → «Долги».</i>")
    return "\n".join(lines)


async def send_report(chat_id: int, data: dict) -> str:
    """Отправить отчёт. Возвращает «rich» или «text» — что реально ушло.

    Rich Message — надстройка над доставкой, а не сама доставка: при любой
    ошибке уходим на текст, а не оставляем руководство без сводки.
    """
    from webapp.server import get_notify_bot

    try:
        from aiogram.types import InputRichMessage

        bot = await get_notify_bot()
        await bot.send_rich_message(
            chat_id=chat_id,
            rich_message=InputRichMessage(blocks=build_blocks(data)),
        )
        return "rich"
    except Exception as e:
        logger.warning(
            "Rich-отчёт не ушёл, отправляю текстом: %s", redact_token(repr(e))
        )

    from services.notifier import tg_send_message

    await tg_send_message(chat_id, build_text(data), reply_markup=webapp_reply_markup())
    return "text"
