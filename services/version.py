"""
Какая версия кода сейчас работает — один ответ на оба процесса.

Задача бытовая, но до сих пор нерешённая: выкатили правку — и проверить, что
сервер её взял, было нечем. WebApp считал SHA у себя в `webapp/server.py` ради
cache-busting статики, бот не знал о версии вообще, и «вроде не починилось»
ничем не отличалось от «образ не пересобрали».

Развёртывание — СВОЁ, `docker-compose.yml` на своём сервере: `bot` и `webapp`
поднимаются из одного образа, но это разные контейнеры, и пересоздать можно
один из двух (`docker compose up -d webapp` после сборки). Тогда половина
правок работает, а половина нет — и выглядит это как новый баг, а не как
недокаченный деплой.

Поэтому:

* **Источник — `GIT_COMMIT_SHA`, проставленный НА СБОРКЕ**, а не `.git`:
  `.dockerignore` истории в образ не кладёт, и `git rev-parse` внутри
  контейнера всегда пуст. Переменную заводит `Dockerfile` (`ARG` → `ENV`), а
  подставляет `scripts/deploy.sh` — руками её забывают, и версия молча
  скатывается к таймстампу. `RAILWAY_*` остались вторым шагом: на Railway
  проект жил раньше, и ломать тот путь ради переименования незачем.
  `git rev-parse` — третий шаг, для запуска из исходников (`python bot.py`);
  таймстамп старта — последний: он не отвечает «какой коммит», но отвечает
  «когда перезапустили», а это тоже вопрос.
* **Заголовок коммита важнее SHA.** Восемь шестнадцатеричных знаков человек не
  сверит с GitHub по памяти; «Пикеры вместо нативных меню…» сверяется с
  первого взгляда. Едет тем же путём — `GIT_COMMIT_MESSAGE`.
* **Время старта считаем ЗДЕСЬ, на импорте модуля.** Оно отвечает на «контейнер
  вообще перезапускался?» — сборка без пересоздания это сборка, которой не было.
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
#
# `importlib.reload` исполняет модуль заново в ТОМ ЖЕ словаре — прежний момент
# старта берём оттуда. Иначе без SHA (сборка без GIT_COMMIT_SHA, исходники без
# .git) перезагрузка давала новую «версию»-таймстамп, и она расходилась с той,
# что `webapp/server.py` взял на своём импорте: процесс тот же, а версии две.
STARTED_AT: float = globals().get("STARTED_AT") or time.time()


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
    # Порядок = порядок надёжности на НАШЕМ развёртывании: свой build-arg
    # первым, платформенные — следом.
    sha = _env("GIT_COMMIT_SHA", "RAILWAY_GIT_COMMIT_SHA", "SOURCE_COMMIT")
    if sha:
        return sha[:8]
    return _git_sha() or str(int(STARTED_AT))


#: Восемь знаков SHA — этим же значением бьётся кэш статики WebApp.
APP_VERSION = _compute_sha()


def commit_subject() -> str:
    """Заголовок коммита (первая строка). Пусто, если платформа не сказала."""
    raw = _env("GIT_COMMIT_MESSAGE", "RAILWAY_GIT_COMMIT_MESSAGE")
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
    return _env("GIT_BRANCH", "RAILWAY_GIT_BRANCH")


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
    """Строка для лога на старте — её ищут в `docker compose logs` первой."""
    parts = [f"version={APP_VERSION}"]
    if branch():
        parts.append(f"branch={branch()}")
    if commit_subject():
        parts.append(f"commit={commit_subject()[:70]!r}")
    return " ".join(parts)
