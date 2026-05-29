"""
Регресс на CRITICAL кросс-loop баг paymentin-синка.

confirm_payment вызывается через services.async_db → asyncio.to_thread, т.е.
в воркер-треде БЕЗ running loop. MS-сессия привязана к ГЛАВНОМУ loop'у. Раньше
_trigger_ms_paymentin_sync делал asyncio.run() → новый loop дёргал сессию
чужого loop'а → RuntimeError, синк молча падал на каждом подтверждении.

Теперь корутина планируется на loop сессии через run_coroutine_threadsafe.

Важно: _trigger вызываем из СВЕЖЕГО воркер-треда (как реальный asyncio.to_thread),
а не из главного pytest-треда — иначе оставшееся от соседних тестов состояние
running-loop могло бы увести get_running_loop() по ложному пути (флакки).
"""

import asyncio
import threading
import time


def test_paymentin_sync_runs_on_session_loop_from_worker(monkeypatch):
    import services.moysklad as ms
    from services import database as db

    ran: dict = {}

    async def fake_create(pid):
        ran["loop"] = asyncio.get_running_loop()
        ran["pid"] = pid

    monkeypatch.setattr("services.ms_payments.create_paymentin_for_payment", fake_create)

    # «Главный» loop бота — крутится в отдельном потоке (как aiogram/uvicorn).
    main_loop = asyncio.new_event_loop()
    lt = threading.Thread(target=main_loop.run_forever, daemon=True)
    lt.start()
    try:
        monkeypatch.setattr(ms, "_session_loop", main_loop, raising=False)
        # Воркер-тред без running loop (как asyncio.to_thread в проде).
        worker = threading.Thread(target=db._trigger_ms_paymentin_sync, args=(777,))
        worker.start()
        worker.join(timeout=2)
        for _ in range(100):  # ждём до ~2с пока корутина отработает на main_loop
            if "pid" in ran:
                break
            time.sleep(0.02)
    finally:
        main_loop.call_soon_threadsafe(main_loop.stop)
        lt.join(timeout=2)

    assert ran.get("pid") == 777
    assert ran.get("loop") is main_loop  # исполнилось на loop'е сессии, не на новом


def test_paymentin_sync_cron_path_runs_without_main_loop(monkeypatch):
    import services.moysklad as ms
    from services import database as db

    ran: dict = {}

    async def fake_create(pid):
        ran["pid"] = pid

    monkeypatch.setattr("services.ms_payments.create_paymentin_for_payment", fake_create)
    monkeypatch.setattr(ms, "_session_loop", None, raising=False)

    # Свежий воркер-тред без главного loop'а → ветка asyncio.run (cron).
    worker = threading.Thread(target=db._trigger_ms_paymentin_sync, args=(888,))
    worker.start()
    worker.join(timeout=2)

    assert ran.get("pid") == 888
