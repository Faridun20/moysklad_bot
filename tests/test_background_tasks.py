"""
PR B (Tier 2.2): фоновые задачи держат сильную ссылку (страховка от GC) и
логируют необработанное исключение через done-callback — раньше упавший
fire-and-forget create_task исчезал молча.
"""

import asyncio
import logging


def test_webapp_spawn_bg_logs_exception(caplog):
    import webapp.server as server

    async def _run():
        async def boom():
            raise ValueError("boom-webapp")

        task = server._spawn_bg(boom(), "boomer")
        await asyncio.sleep(0.05)  # дать задаче упасть + сработать callback'ам
        return task

    with caplog.at_level(logging.ERROR):
        task = asyncio.run(_run())

    assert task.done()
    assert task not in server._background_tasks  # discard-callback отработал
    assert any(
        r.levelno == logging.ERROR and "boomer" in r.getMessage() for r in caplog.records
    )


def test_webapp_spawn_bg_success_no_error_log(caplog):
    import webapp.server as server

    async def _run():
        async def ok():
            return 42

        task = server._spawn_bg(ok(), "good")
        await asyncio.sleep(0.05)
        return task

    with caplog.at_level(logging.ERROR):
        task = asyncio.run(_run())

    assert task.done()
    assert task.result() == 42
    assert not any("good" in r.getMessage() for r in caplog.records)  # успех не логируем


def test_bot_spawn_startup_holds_ref_and_logs(caplog):
    import bot

    async def _run():
        async def boom():
            raise RuntimeError("boom-startup")

        task = bot._spawn_startup(boom(), "init_demand")
        # Пока задача в полёте — сильная ссылка держится.
        assert task in bot._startup_tasks
        await asyncio.sleep(0.05)
        return task

    with caplog.at_level(logging.ERROR):
        task = asyncio.run(_run())

    assert task.done()
    assert task not in bot._startup_tasks  # discard после завершения
    assert any(
        r.levelno == logging.ERROR and "init_demand" in r.getMessage() for r in caplog.records
    )
