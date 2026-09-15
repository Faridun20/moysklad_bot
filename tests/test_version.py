"""Версия работающего кода: один источник на оба процесса, `/healthz`, `/version`.

Задача бытовая, но до сих пор нерешённая: выкатили правку — проверить, что прод
её взял, было нечем. Главное, что здесь проверяется, — СВЕРКА двух сервисов:
`moysklad_bot` и `Webapp` на Railway деплоятся отдельно и разъезжаются штатно.
"""

from __future__ import annotations

import importlib

import pytest


@pytest.fixture
def version_mod(monkeypatch):
    """Свежий импорт модуля: SHA считается на импорте, из окружения."""

    def _load(**env):
        for key in ("RAILWAY_GIT_COMMIT_SHA", "GIT_COMMIT_SHA", "SOURCE_COMMIT",
                    "RAILWAY_GIT_COMMIT_MESSAGE", "GIT_COMMIT_MESSAGE",
                    "RAILWAY_GIT_BRANCH", "GIT_BRANCH"):
            monkeypatch.delenv(key, raising=False)
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        import services.version as mod

        return importlib.reload(mod)

    yield _load
    import services.version as mod

    importlib.reload(mod)


def test_build_arg_sha_is_the_source_of_truth(version_mod):
    """В образе `.git` нет (`.dockerignore`) — источник это аргумент сборки.

    Порядок важен: свой `GIT_COMMIT_SHA` бьёт платформенный `RAILWAY_*`.
    Развёртывание у нас своё (`docker-compose.yml`), и если где-то остался
    залипший `RAILWAY_GIT_COMMIT_SHA`, версия обязана показывать то, что
    реально собрано, а не то, что осталось от прежней площадки.
    """
    mod = version_mod(
        GIT_COMMIT_SHA="abcdef1234567890",
        RAILWAY_GIT_COMMIT_SHA="0000000000000000",
    )
    assert mod.APP_VERSION == "abcdef12"


def test_railway_vars_still_work_as_a_fallback(version_mod):
    """Проект жил на Railway — ломать тот путь ради переименования незачем."""
    mod = version_mod(RAILWAY_GIT_COMMIT_SHA="feedface12345678")
    assert mod.APP_VERSION == "feedface"


def test_version_falls_back_when_platform_is_silent(version_mod):
    """Без переменных версия всё равно есть: локально — git, иначе таймстамп.

    Пустая версия означала бы «не знаю», а это ровно тот ответ, ради избавления
    от которого модуль и написан.
    """
    mod = version_mod()
    assert mod.APP_VERSION and mod.APP_VERSION.strip()


def test_timestamp_version_survives_module_reload(version_mod, monkeypatch):
    """Без SHA и без .git версия — момент старта ПРОЦЕССА, а не импорта модуля.

    Так идут тесты в локальной CI (исходники без .git, образ без build-arg):
    перезагрузка `services.version` давала новый таймстамп, а
    `webapp/server.py` держал старый — `/api/me` и `/healthz` отвечали не той
    версией, что бот, хотя процесс один.
    """
    import subprocess

    import services.version as mod

    def no_git(*args, **kwargs):
        raise FileNotFoundError("git")

    # Патчим сам subprocess, а не `mod._git_sha`: reload заново определит функцию.
    monkeypatch.setattr(subprocess, "check_output", no_git)
    started = mod.STARTED_AT
    monkeypatch.setattr("time.time", lambda: started + 3600)
    first = version_mod()
    second = version_mod()
    assert first.STARTED_AT == second.STARTED_AT == started
    assert first.APP_VERSION == second.APP_VERSION == str(int(started))


def test_commit_subject_is_the_first_line_only(version_mod):
    """Заголовок коммита, а не всё тело: восемь знаков SHA человек с GitHub не
    сверит, а «Пикеры вместо нативных меню…» сверяется с первого взгляда."""
    mod = version_mod(
        GIT_COMMIT_SHA="f" * 40,
        GIT_COMMIT_MESSAGE="Заголовок правки\n\nДлинное тело\nи ещё строка",
    )
    assert mod.commit_subject() == "Заголовок правки"


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [(5, "5 с"), (90, "1 мин"), (3 * 3600 + 12 * 60, "3 ч 12 мин"), (2 * 86400 + 3600, "2 д 1 ч")],
)
def test_uptime_is_readable(version_mod, seconds, expected):
    mod = version_mod()
    assert mod.human_uptime(seconds) == expected


def test_webapp_and_bot_compute_the_same_version():
    """Сравнивать SHA двух сервисов можно, только если оба считают его одинаково.

    Пока WebApp считал версию у себя в `webapp/server.py`, а бот не считал
    вовсе, расхождение было невозможно ни увидеть, ни доказать.
    """
    from services import version as mod
    from webapp.server import APP_VERSION as webapp_version

    assert webapp_version == mod.APP_VERSION


@pytest.fixture
def api(isolated_db, monkeypatch):
    from fastapi.testclient import TestClient

    import webapp.server as server

    isolated_db.set_role(777001, "boss_user", "Boss", "boss")
    monkeypatch.setattr(
        server,
        "verify_init_data",
        lambda init_data: {"id": int(init_data), "first_name": "U", "username": "u"},
    )
    return TestClient(server.app)


def test_healthz_reports_version_and_uptime(api):
    """`/healthz` отвечает без авторизации: закрытый healthcheck не годится
    внешнему мониторингу, а SHA коммита сам по себе ничего не открывает."""
    from services import version as mod

    body = api.get("/healthz").json()
    assert body["ok"] is True
    assert body["version"] == mod.APP_VERSION
    assert body["uptime"] >= 0
    assert "mode" in body


def test_me_carries_version_for_the_screen(api):
    """Версия едет вместе с ролью: отдельный запрос ради восьми знаков — это
    запрос, который забудут сделать."""
    from services import version as mod

    body = api.post("/api/me", json={"initData": "777001"}).json()
    assert body["version"] == mod.APP_VERSION


# ─── /version в боте: сверка двух сервисов ───────────────────────────────────


class _Msg:
    """Минимальный Message: команда только отвечает текстом."""

    def __init__(self, user_id: int):
        self.from_user = type("U", (), {"id": user_id, "full_name": "U"})()
        self.answers: list[str] = []

    async def answer(self, text, **kwargs):
        self.answers.append(text)
        return None


@pytest.fixture
def cmd(isolated_db, monkeypatch):
    import importlib

    import services.roles as roles

    importlib.reload(roles)
    isolated_db.set_role(1, "admin_user", "Admin", "admin")
    isolated_db.set_role(200, "mgr_user", "Manager", "manager")

    import handlers.version as mod

    importlib.reload(mod)
    return mod


def _run(coro):
    import asyncio

    return asyncio.run(coro)


def test_version_is_closed_to_managers(cmd):
    msg = _Msg(200)
    _run(cmd.cmd_version(msg))
    assert "Нет доступа" in msg.answers[0]


def test_matching_versions_say_so_plainly(cmd, monkeypatch):
    from services import version as mod

    async def fake_health():
        return {"version": mod.APP_VERSION, "subject": "правка", "uptime": 120}, ""

    monkeypatch.setattr(cmd, "_webapp_health", fake_health)
    msg = _Msg(1)
    _run(cmd.cmd_version(msg))
    assert "🟢" in msg.answers[0]
    assert mod.APP_VERSION in msg.answers[0]


def test_diverged_services_are_called_out(cmd, monkeypatch):
    """Ради этого команда и написана.

    Два сервиса Railway деплоятся отдельно: один пересобрался, второй остался
    на прежнем коммите — и половина правок «не работает». Расхождение обязано
    быть выводом, а не парой SHA, которые надо сличать глазами.
    """

    async def fake_health():
        return {"version": "deadbeef", "subject": "прошлая правка", "uptime": 90000}, ""

    monkeypatch.setattr(cmd, "_webapp_health", fake_health)
    msg = _Msg(1)
    _run(cmd.cmd_version(msg))
    text = msg.answers[0]
    assert "🔴" in text and "РАЗНЫХ коммитах" in text
    assert "Redeploy" in text


def test_dead_webapp_is_reported_not_swallowed(cmd, monkeypatch):
    """«WebApp не ответил» — это тоже ответ, и своя версия всё равно нужна."""
    from services import version as mod

    async def fake_health():
        return None, "TimeoutError"

    monkeypatch.setattr(cmd, "_webapp_health", fake_health)
    msg = _Msg(1)
    _run(cmd.cmd_version(msg))
    text = msg.answers[0]
    assert "не ответил" in text and "TimeoutError" in text
    assert mod.APP_VERSION in text


def test_user_text_cannot_break_html(cmd, monkeypatch):
    """Заголовок коммита едет в parse_mode=HTML — экранируем, как всё остальное.

    Коммит «fix: <b> в карточке» иначе рушит разметку сообщения целиком.
    """

    async def fake_health():
        return {"version": "deadbeef", "subject": "fix: <b> в карточке"}, ""

    monkeypatch.setattr(cmd, "_webapp_health", fake_health)
    msg = _Msg(1)
    _run(cmd.cmd_version(msg))
    assert "&lt;b&gt;" in msg.answers[0]


def test_command_is_in_the_autocomplete(cmd):
    """Команда в /-автокомплите руководства — иначе её никто не найдёт.

    Что роутер подключён к диспетчеру, проверяет
    `test_bot_trimmed.py::test_surviving_commands_are_registered`: aiogram не
    даёт подключить один и тот же Router ко второму Dispatcher, поэтому
    `register_routers` за прогон зовётся ровно один раз — там, где кэш.
    """
    from handlers.start import _COMMANDS_BOSS, _COMMANDS_MANAGER, _COMMANDS_WAREHOUSE

    assert "version" in {c.command for c in _COMMANDS_BOSS}
    # Менеджеру и кладовщику номер сборки не нужен — как и доступ к команде.
    assert "version" not in {c.command for c in _COMMANDS_MANAGER}
    assert "version" not in {c.command for c in _COMMANDS_WAREHOUSE}


# ─── Сборка обязана донести версию до контейнера ─────────────────────────────


def test_build_passes_the_version_into_the_image():
    """`Dockerfile` и `docker-compose.yml` обязаны пробрасывать версию.

    В образе `.git` нет (`.dockerignore`), поэтому аргумент сборки — ЕДИНСТВЕННЫЙ
    источник. Пропадёт он из любого из двух файлов — версия молча скатится к
    таймстампу старта: `/version` перестанет отвечать «какой коммит», а кэш
    статики начнёт слетать у всех на каждом рестарте. Ни один тест приложения
    этого не заметит, потому что код при этом исправен.
    """
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    dockerfile = (root / "Dockerfile").read_text(encoding="utf-8")
    compose = (root / "docker-compose.yml").read_text(encoding="utf-8")

    for name in ("GIT_COMMIT_SHA", "GIT_COMMIT_MESSAGE"):
        assert f"ARG {name}" in dockerfile, f"Dockerfile не принимает {name}"
        assert f"ENV {name}=${name}" in dockerfile, f"Dockerfile не пробрасывает {name}"
        assert f"{name}: ${{{name}:-}}" in compose, f"compose не передаёт {name} в сборку"


def test_deploy_script_fills_the_version_in():
    """Подставляет их скрипт выката, а не человек: руками это забывают."""
    from pathlib import Path

    script = (Path(__file__).resolve().parent.parent / "scripts" / "deploy.sh").read_text(
        encoding="utf-8"
    )
    assert "git rev-parse HEAD" in script
    assert "git log -1 --pretty=%s" in script
    assert "export GIT_COMMIT_SHA" in script
    # Пересоздаём ОБА контейнера: поднять один из двух — это и есть то
    # расхождение, которое потом ловит /version.
    assert "docker compose up -d\n" in script
