"""
Какая версия кода сейчас работает — один ответ на оба процесса.

Задача бытовая, но до сих пор нерешённая: выкатили правку — и проверить, что
прод её взял, было нечем. WebApp считал SHA у себя в `webapp/server.py` ради
cache-busting статики, бот не знал о версии вообще, а два сервиса Railway
(`moysklad_bot` и `Webapp`) деплоятся ОТДЕЛЬНО и разъезжаются штатно: один
пересобрался, второй остался на прежнем коммите, и поведение «половина
починилась» выглядит как новый баг.

Поэтому:

* **Источник — переменные Railway, а не `.git`.** В контейнере репозитория нет:
  Railpack кладёт исходники без истории. `RAILWAY_GIT_COMMIT_SHA` ставит сама
  платформа на каждый деплой. `git rev-parse` оставлен вторым шагом для
  локального запуска, таймстамп старта — третьим: он не отвечает «какой
  коммит», но отвечает «когда перезапустили», а это тоже вопрос.
* **Заголовок коммита важнее SHA.** Восемь шестнадцатеричных знаков человек не
  сверит с GitHub по памяти; «Пикеры вместо нативных меню…» сверяется с
  первого взгляда. Railway отдаёт его в `RAILWAY_GIT_COMMIT_MESSAGE`.
* **Время старта считаем ЗДЕСЬ, на импорте модуля.** Оно отвечает на «сервис
  вообще перезапускался?» — деплой без рестарта это деплой, которого не было.
"""

from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent

# Момент импорта = момент старта процесса: модуль тянется из bot.py и
# webapp/server.py на их загрузке.
STARTED_AT = time.time()


def _env(*names: str) -> str:
    for name in names:
        value = (os.environ.get(name) or "").strip()
        if value:
            return value
    return ""


def _git_sha() -> str:
    """Короткий SHA из локального `.git`. На проде его нет — вернём пустое."""
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--short=8", "HEAD"],
            cwd=_REPO_ROOT,
            stderr=subprocess.DEVNULL,
            timeout=2,
        )
    except Exception:
        return ""
    return out.decode().strip()


def _compute_sha() -> str:
    sha = _env("RAILWAY_GIT_COMMIT_SHA", "GIT_COMMIT_SHA", "SOURCE_COMMIT")
    if sha:
        return sha[:8]
    return _git_sha() or str(int(STARTED_AT))


#: Восемь знаков SHA — этим же значением бьётся кэш статики WebApp.
APP_VERSION = _compute_sha()


def commit_subject() -> str:
    """Заголовок коммита (первая строка). Пусто, если платформа не сказала."""
    raw = _env("RAILWAY_GIT_COMMIT_MESSAGE", "GIT_COMMIT_MESSAGE")
    if not raw:
        try:
            out = subprocess.check_output(
                ["git", "log", "-1", "--pretty=%s"],
                cwd=_REPO_ROOT,
                stderr=subprocess.DEVNULL,
                timeout=2,
            )
            raw = out.decode()
        except Exception:
            return ""
    return raw.strip().splitlines()[0].strip() if raw.strip() else ""


def branch() -> str:
    return _env("RAILWAY_GIT_BRANCH", "GIT_BRANCH")


def uptime_seconds() -> int:
    return max(0, int(time.time() - STARTED_AT))


def human_uptime(seconds: int | None = None) -> str:
    """«3 ч 12 мин» — сколько процесс живёт. Секунды нужны только в первую
    минуту после рестарта, зато именно тогда они и нужны."""
    total = uptime_seconds() if seconds is None else max(0, int(seconds))
    days, rest = divmod(total, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    if days:
        return f"{days} д {hours} ч"
    if hours:
        return f"{hours} ч {minutes} мин"
    if minutes:
        return f"{minutes} мин"
    return f"{secs} с"


@dataclass(frozen=True)
class VersionInfo:
    version: str
    subject: str
    branch: str
    started_at: float
    uptime: int
    mode: str

    def as_dict(self) -> dict:
        return {
            "version": self.version,
            "subject": self.subject,
            "branch": self.branch,
            "started_at": int(self.started_at),
            "uptime": self.uptime,
            "mode": self.mode,
        }


def info() -> VersionInfo:
    """Снимок версии текущего процесса."""
    from config import BOT_MODE

    return VersionInfo(
        version=APP_VERSION,
        subject=commit_subject(),
        branch=branch(),
        started_at=STARTED_AT,
        uptime=uptime_seconds(),
        mode=BOT_MODE,
    )


def startup_line() -> str:
    """Строка для лога на старте — её ищут в логах Railway первой."""
    parts = [f"version={APP_VERSION}"]
    if branch():
        parts.append(f"branch={branch()}")
    if commit_subject():
        parts.append(f"commit={commit_subject()[:70]!r}")
    return " ".join(parts)
