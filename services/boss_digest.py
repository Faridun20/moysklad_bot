"""
Вечерний дайджест боссу — ОДНО сообщение вместо вала точечных пушей.

Контекст (решение владельца, сентябрь 2026, см. `services.notify_policy`):
раньше каждый платёж/сдача/возврат ниже `boss_instant_threshold_usd` НЕ
пушился боссу отдельной карточкой — но так и оставался бы никем не замеченным
до тех пор, пока кто-нибудь не откроет WebApp сам. Этот модуль закрывает
дыру: раз в день (`tasks/run_boss_digest.py`, cron каждые 15 минут — время
дайджеста это настройка, а не фиксированный час, см. ниже) собирает то, что
накопилось, и шлёт одной сводкой.

**Без отдельной очереди.** Содержимое дайджеста НЕ хранится нигде отдельно —
оно каждый раз ВЫЧИСЛЯЕТСЯ из текущего состояния `payments`/`cash_deposits`/
`returns` (те же `get_pending_payments`/`get_pending_cash_deposits`/
`get_pending_returns`, что и `services.ops_summary`) плюс `should_notify_now`
из `services.notify_policy` — тот же порог, что резал пуш в момент события.
Это НАДЁЖНЕЕ отдельной таблицы-очереди по трём причинам:
  * то, что уже подтвердили/отклонили (в WebApp или другой карточкой) само
    выпадает из выборки — не нужно чистить очередь вдогонку;
  * ретрай cron'а (каждые 15 минут, см. ниже) не может «потерять» строку —
    нечего терять, это обычный SELECT;
  * если changed порог (`boss_instant_threshold_usd`) — исторические записи
    не рассинхронизируются со старым решением «immediate/digest», записанным
    в очередь заранее: пересчёт всегда свежий.

**Идемпотентность самого дайджеста** (одно сообщение в день, а не на каждый
15-минутный тик) — НЕ через содержимое, а через отдельную метку времени
`app_settings.boss_digest_last_run_at`: как только дайджест прогнан за
сегодня (Asia/Tashkent), повторные тики до полуночи молча выходят. Метка
пишется ПОСЛЕ отправки (тот же компромисс, что у `cash_deposit_reminder_time`
в `run_debts_notify`: пропущенный прогон не должен «съесть» дайджест дня, а
цена — возможный дубль при ретрае сразу после сбоя середины отправки).

**Пустой дайджест не отправляется** (то же правило, что у `money_report`) —
но метка `boss_digest_last_run_at` ВСЁ РАВНО обновляется, иначе крон продолжал
бы пересчитывать пустой дайджест каждые 15 минут до конца суток.
"""

from __future__ import annotations

import logging

from utils.keyboards import DECISIONS_SCREEN, webapp_screen_url

logger = logging.getLogger(__name__)

_ITEM_CAP = 8

WEBAPP_BUTTON_TEXT = "🗂 Открыть решения"

# Кнопка ведёт прямо в «Решения» руководителя — тем же каноническим deep link,
# что и остальные уведомления (`utils.keyboards.webapp_screen_url`:
# `?startapp=decisions`, фронт читает его в `launchScreen`). Свой сборщик адреса
# здесь разошёлся бы с фронтом при первой правке.


def _webapp_url() -> str | None:
    """https-адрес WebApp с deep link на «Решения», либо None (WEBAPP_URL не
    https — такую web_app-кнопку Bot API отвергает вместе с сообщением)."""
    return webapp_screen_url(DECISIONS_SCREEN)


def webapp_reply_markup() -> dict | None:
    url = _webapp_url()
    if not url:
        return None
    return {"inline_keyboard": [[{"text": WEBAPP_BUTTON_TEXT, "web_app": {"url": url}}]]}


# ─── Время дайджеста / идемпотентность ─────────────────────────────────────


_LAST_RUN_KEY = "boss_digest_last_run_at"


def _parse_hhmm(raw: str) -> tuple[int, int]:
    try:
        h, m = str(raw or "19:00").split(":")
        return int(h), int(m)
    except (TypeError, ValueError):
        return 19, 0


def digest_time_hhmm() -> tuple[int, int]:
    from services.database import get_setting

    return _parse_hhmm(get_setting("boss_digest_time", "19:00"))


def _last_run_at() -> str | None:
    from services.database import get_setting

    return get_setting(_LAST_RUN_KEY, None)


def mark_run(now_str_value: str) -> None:
    """Записать момент прогона — ПОСЛЕ отправки (см. докстринг модуля)."""
    from services.database import set_setting

    set_setting(_LAST_RUN_KEY, now_str_value)


def is_due(now=None) -> bool:
    """Пора ли слать дайджест: текущее время (Asia/Tashkent, см. `local_now`)
    не раньше `boss_digest_time`, и сегодня ещё не слали.

    Cron дёргает это КАЖДЫЕ 15 минут (docker-compose.yml, `cron-boss-digest`)
    — так изменение настройки `boss_digest_time` подхватывается без правки
    хостового crontab: расписание фиксированное (каждые 15 мин), а решение
    «пора ли» — динамическое."""
    from utils.helpers import local_now

    now = now or local_now()
    hh, mm = digest_time_hhmm()
    if (now.hour, now.minute) < (hh, mm):
        return False
    last = _last_run_at()
    if last and str(last)[:10] == now.strftime("%Y-%m-%d"):
        return False
    return True


# ─── Сбор содержимого ───────────────────────────────────────────────────────


def _sum_by_currency(rows: list[dict], amount_key: str = "amount", cur_key: str = "currency") -> list[dict]:
    totals: dict[str, float] = {}
    for r in rows:
        cur = (r.get(cur_key) or "USD").upper()
        totals[cur] = totals.get(cur, 0.0) + float(r.get(amount_key) or 0)
    return [{"currency": c, "total": t} for c, t in sorted(totals.items())]


async def gather() -> dict:
    """Данные дайджеста — отдельно от рендера, как у `money_report.gather`."""
    from services import database as db
    from services import notify_policy as policy
    from services import order_payments
    from utils.helpers import local_now

    since = _last_run_at() or local_now().strftime("%Y-%m-%d") + " 00:00:00"
    now = local_now()

    payments_all = await db.get_pending_payments()
    deposits_all = await db.get_pending_cash_deposits()
    returns_all = await db.get_pending_returns()
    confirmed_recent = await db.get_confirmed_payments_since(since)

    dep_currency = await order_payments.deposit_currency([int(d["id"]) for d in deposits_all])
    parts = await order_payments.parts_by_payment([int(p["id"]) for p in payments_all])

    def _payment_currency(row: dict) -> str:
        return (row.get("currency") or "USD").upper()

    def _deposit_currency(row: dict) -> str:
        return dep_currency.get(int(row["id"]), "USD")

    def _digest_only(rows: list[dict], kind: str, cur_of, amount_key: str = "amount") -> list[dict]:
        return [
            r for r in rows
            if not policy.should_notify_now(kind, r.get(amount_key), cur_of(r))
        ]

    payments_digest = _digest_only(payments_all, policy.PAYMENT, _payment_currency)
    deposits_digest = _digest_only(deposits_all, policy.CASH_DEPOSIT, _deposit_currency)
    returns_digest = _digest_only(
        returns_all, policy.RETURN, lambda r: "USD", amount_key="total_amount"
    )
    received_digest = _digest_only(confirmed_recent, policy.PAYMENT, _payment_currency)

    def _payment_line(p: dict) -> str:
        part = parts.get(int(p["id"]))
        method = order_payments.METHODS.get(part["method"], "—") if part else "без способа"
        order = f" · заказ #{p['order_id']}" if p.get("order_id") else ""
        return (
            f"{p.get('full_name') or p.get('user_id')} — "
            f"{p['amount']:,.0f} {_payment_currency(p)} ({method}){order}"
        ).replace(",", " ")

    def _deposit_line(d: dict) -> str:
        return f"#{d['id']} — {d['amount']:,.0f} {_deposit_currency(d)}".replace(",", " ")

    def _return_line(r: dict) -> str:
        return f"#{r['id']} · заказ #{r['order_id']} — {r['total_amount']:,.0f} USD".replace(",", " ")

    return {
        "since": since,
        "until": now.strftime("%Y-%m-%d %H:%M"),
        "payments": {
            "count": len(payments_digest),
            "waiting_total": len(payments_all),
            "lines": [_payment_line(p) for p in payments_digest[:_ITEM_CAP]],
            "rest": max(0, len(payments_digest) - _ITEM_CAP),
        },
        "deposits": {
            "count": len(deposits_digest),
            "waiting_total": len(deposits_all),
            "lines": [_deposit_line(d) for d in deposits_digest[:_ITEM_CAP]],
            "rest": max(0, len(deposits_digest) - _ITEM_CAP),
        },
        "returns": {
            "count": len(returns_digest),
            "waiting_total": len(returns_all),
            "lines": [_return_line(r) for r in returns_digest[:_ITEM_CAP]],
            "rest": max(0, len(returns_digest) - _ITEM_CAP),
        },
        "received": {
            "count": len(received_digest),
            "by_currency": _sum_by_currency(received_digest),
        },
    }


def is_empty(data: dict) -> bool:
    """Нечего показывать — ни одного ожидающего/полученного события ниже
    порога. «Что ещё ждёт крупного» в дайджест НЕ входит (оно уже ушло
    немедленным пушем) — пустой дайджест это буквально «ничего нового
    маленького с прошлого раза»."""
    return not (
        data["payments"]["count"]
        or data["deposits"]["count"]
        or data["returns"]["count"]
        or data["received"]["count"]
    )


# ─── Разметка ────────────────────────────────────────────────────────────────


def _period_label(data: dict) -> str:
    def ru(iso: str) -> str:
        s = str(iso)[:16]
        if len(s) < 16:
            return s
        d, t = s[:10], s[11:16]
        y, m, dd = d.split("-")
        return f"{dd}.{m} {t}"

    return f"{ru(data['since'])} — {ru(data['until'])}"


def _received_line(data: dict) -> str | None:
    block = data["received"]
    if not block["count"]:
        return None
    total = " · ".join(f"{t['total']:,.0f} {t['currency']}".replace(",", " ") for t in block["by_currency"])
    return f"Получено (мелкие): {block['count']} на {total}"


def build_blocks(data: dict) -> list:
    from aiogram.types import (
        InputRichBlockButtons,
        InputRichBlockDivider,
        InputRichBlockList,
        InputRichBlockListItem,
        InputRichBlockParagraph,
        InputRichBlockSectionHeading,
        RichMessageButton,
        WebAppInfo,
    )

    blocks: list = [
        InputRichBlockSectionHeading(text=f"Решения за день · {_period_label(data)}", size=1),
    ]

    def _section(title: str, block: dict, lines_key: str = "lines") -> None:
        if not block["count"]:
            return
        blocks.append(InputRichBlockDivider())
        blocks.append(InputRichBlockSectionHeading(
            text=f"{title} ({block['count']} из {block['waiting_total']} ждущих)", size=2,
        ))
        blocks.append(InputRichBlockList(items=[
            InputRichBlockListItem(blocks=[InputRichBlockParagraph(text=line)])
            for line in block[lines_key]
        ]))
        if block["rest"]:
            blocks.append(InputRichBlockParagraph(text=f"…и ещё {block['rest']}"))

    _section("💳 Платежи на подтверждение", data["payments"])
    _section("💵 Сдачи наличных", data["deposits"])
    _section("↩️ Возвраты", data["returns"])

    received = _received_line(data)
    if received:
        blocks.append(InputRichBlockDivider())
        blocks.append(InputRichBlockParagraph(text=received))

    url = _webapp_url()
    if url:
        blocks.append(InputRichBlockButtons(buttons=[
            RichMessageButton(text=WEBAPP_BUTTON_TEXT, web_app=WebAppInfo(url=url)),
        ]))
    return blocks


def build_text(data: dict) -> str:
    """Тот же дайджест текстом — фолбэк, если Rich Message не прошёл.
    Пользовательский ввод (имя плательщика) экранируем — сообщение идёт с
    parse_mode=HTML."""
    from utils.helpers import esc

    lines = [f"🗂 <b>Решения за день · {esc(_period_label(data))}</b>", ""]

    def _section(title: str, block: dict) -> None:
        if not block["count"]:
            return
        lines.append(f"<b>{esc(title)}</b> ({block['count']} из {block['waiting_total']} ждущих)")
        for line in block["lines"]:
            lines.append(f"  • {esc(line)}")
        if block["rest"]:
            lines.append(f"  …и ещё {block['rest']}")
        lines.append("")

    _section("💳 Платежи на подтверждение", data["payments"])
    _section("💵 Сдачи наличных", data["deposits"])
    _section("↩️ Возвраты", data["returns"])

    received = _received_line(data)
    if received:
        lines.append(esc(received))
        lines.append("")

    lines.append("<i>Подробнее и решения — в WebApp.</i>")
    return "\n".join(lines)


async def send_report(chat_id: int, data: dict) -> str:
    """Отправить дайджест. Возвращает «rich»/«text» — что реально ушло, или
    «failed» — ни один канал не доставил сообщение (Rich упал, а текстовый
    фолбэк `tg_send_message` вернул False — Telegram недоступен целиком).
    Тот же фолбэк-контракт, что у `money_report.send_report`: Rich Message —
    надстройка, при любой ошибке уходим на текст. Вызывающий (`tasks.
    run_boss_digest`) обязан отметить `mark_run` только при реальной
    доставке — «failed» здесь не должно приводить к «дайджест дня ушёл»."""
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
        from utils.helpers import redact_token

        logger.warning(
            "Rich-дайджест не ушёл, отправляю текстом: %s", redact_token(repr(e))
        )

    from services.notifier import tg_send_message

    ok = await tg_send_message(chat_id, build_text(data), reply_markup=webapp_reply_markup())
    if not ok:
        logger.error("boss_digest: текстовый фолбэк тоже не доставлен chat_id=%s", chat_id)
        return "failed"
    return "text"
