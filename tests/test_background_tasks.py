"""
PR B (Tier 2.2): фоновые задачи держат сильную ссылку (страховка от GC) и
логируют необработанное исключение через done-callback — раньше упавший
fire-and-forget create_task исчезал молча.

Хелпер общий для бота и WebApp — `utils/background.py` (раньше жил в
`webapp/server.py`; переехал, когда печатная форма после одобрения ушла в
фоновую задачу и понадобился сервису).
"""

import asyncio
import logging


def test_spawn_logs_exception(caplog):
    from utils import background

    async def _run():
        async def boom():
            raise ValueError("boom-webapp")

        task = background.spawn(boom(), "boomer")
        await asyncio.sleep(0.05)  # дать задаче упасть + сработать callback'ам
        return task

    with caplog.at_level(logging.ERROR):
        task = asyncio.run(_run())

    assert task.done()
    assert task not in background._tasks  # discard-callback отработал
    assert any(
        r.levelno == logging.ERROR and "boomer" in r.getMessage() for r in caplog.records
    )


def test_spawn_success_no_error_log(caplog):
    from utils import background

    async def _run():
        async def ok():
            return 42

        task = background.spawn(ok(), "good")
        assert task in background.pending()  # сильная ссылка, пока не завершилась
        await asyncio.sleep(0.05)
        return task

    with caplog.at_level(logging.ERROR):
        task = asyncio.run(_run())

    assert task.done()
    assert task.result() == 42
    assert not any("good" in r.getMessage() for r in caplog.records)  # успех не логируем
    assert task not in background.pending()
