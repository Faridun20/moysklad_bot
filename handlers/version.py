"""
`/version` — какая версия кода сейчас работает у БОТА и у WebApp.

Зачем команда, а не «посмотреть в Railway»: выкатили правку и проверить, что
прод её взял, было нечем — в панель на телефоне с площадки не полезешь, а
поведение «вроде не починилось» ничем не отличается от «не задеплоилось».

Главное, ради чего команда существует, — **сверка двух сервисов**.
`moysklad_bot` и `Webapp` на Railway деплоятся ОТДЕЛЬНО: один пересобрался,
второй остался на прежнем коммите, и половина правок работает, а половина нет.
Самому боту своя версия известна и так, поэтому он спрашивает у WebApp
`/healthz` и сравнивает. Расхождение — это ответ, а не деталь, поэтому оно
выносится строкой с эмодзи, а не прячется между двумя SHA.

WebApp не ответил — это тоже результат («сервис лежит»), а не повод промолчать:
команда пишет причину и показывает хотя бы свою версию.
"""

import logging

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from config import WEBAPP_URL
from services import version as app_version
from services.roles import is_boss
from utils.helpers import esc

logger = logging.getLogger(__name__)
router = Router()

_HEALTH_TIMEOUT = 6


async def _webapp_health() -> tuple[dict | None, str]:
    """`/healthz` WebApp → (тело, причина отказа). Ровно одно из двух пусто."""
    if not WEBAPP_URL:
        return None, "WEBAPP_URL не задан"
    import aiohttp

    url = f"{WEBAPP_URL}/healthz"
    try:
        timeout = aiohttp.ClientTimeout(total=_HEALTH_TIMEOUT)
        async with (
            aiohttp.ClientSession(timeout=timeout) as session,
            session.get(url) as resp,
        ):
            if resp.status != 200:
                return None, f"HTTP {resp.status}"
            return await resp.json(content_type=None), ""
    except Exception as exc:  # сеть, таймаут, невалидный JSON
        # Тип исключения человеку ничего не говорит, а текст иногда говорит.
        return None, f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__


def _service_block(title: str, info: dict) -> list[str]:
    lines = [f"<b>{esc(title)}</b>", f"версия: <code>{esc(str(info.get('version', '?')))}</code>"]
    subject = str(info.get("subject") or "")
    if subject:
        lines.append(f"коммит: {esc(subject[:120])}")
    branch = str(info.get("branch") or "")
    if branch:
        lines.append(f"ветка: <code>{esc(branch)}</code>")
    uptime = info.get("uptime")
    if uptime is not None:
        lines.append(f"работает: {app_version.human_uptime(int(uptime))}")
    return lines


@router.message(Command("version"))
async def cmd_version(message: Message):
    """Версии обоих сервисов и вердикт: совпадают или разъехались."""
    # Руководству, а не только админу: «выкатилось ли моё исправление» —
    # вопрос того, кто его просил, и адресовать его больше некому.
    if not is_boss(message.from_user.id):
        return await message.answer("⛔ Версию программы смотрит руководство.")

    mine = app_version.info().as_dict()
    theirs, why = await _webapp_health()

    lines = ["📦 <b>Версия на проде</b>", ""]
    lines += _service_block(f"Бот (BOT_MODE={mine['mode']})", mine)

    if theirs is None:
        lines += ["", "<b>WebApp</b>", f"🔴 не ответил — {esc(why)}"]
    else:
        lines += [""] + _service_block("WebApp", theirs)
        lines.append("")
        if str(theirs.get("version")) == mine["version"]:
            lines.append("🟢 Оба сервиса на одном коммите.")
        else:
            # Ради этой строки команда и написана: один сервис передеплоился,
            # второй нет — и половина правок «не работает».
            lines.append(
                "🔴 <b>Сервисы на РАЗНЫХ коммитах.</b> Один не передеплоился: "
                "передеплойте отставший в Railway (Deployments → Redeploy)."
            )

    await message.answer("\n".join(lines), parse_mode="HTML")
