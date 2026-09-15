"""Сценарии «день из жизни склада» на НАСТОЯЩЕМ Postgres через HTTP-API WebApp.

Чем это отличается от остальных тестов:

* юнит- и API-тесты (`tests/test_*.py`) проверяют ОДИН шаг и чаще на SQLite;
  E2E (`tests/e2e`) — фронт, и продажу заводят сервисами в обход ручек;
* здесь — ЦЕПОЧКИ шагов разных ролей через те же ручки, что дёргает WebApp,
  на Postgres 16 (как на проде): товар → приход → заказ → одобрение →
  отгрузка → оплата → сдача → подтверждение, контейнер от «в пути» до
  остатка, техника от «в пути» до закрытой рассрочки. После каждого сценария
  проверяются общие инварианты (`invariants.check_all`): остаток не ушёл в
  минус и сходится с накладными, деньги по заказам сходятся с долгами, аудит
  пишется, 500-х и ERROR в логах сервера нет.

Сервер — живой uvicorn в потоке (`tests/liveserver.py`, как у E2E и perf),
клиент — httpx. Мокается только граница: подпись initData (initData = user_id)
и исходящие в Telegram. Роли — настоящие, из `user_roles`.

Состояние БД сценарий читает СИНХРОННЫМ слоем (`db.get_conn`), а не
`run_async`: asyncpg-пул привязан к event loop'у, и вызов из другого loop'а
рвёт пул сервера посреди его фоновых задач (печатная форма после одобрения).

Запуск: `TEST_PG_URL=postgresql://user:pass@host:5432/postgres pytest tests/scenarios`.
Без переменной пакет пропускается. Каждый тест получает свою базу
(CREATE DATABASE … / DROP … WITH (FORCE)). В локальной CI
(`scripts/local_ci.sh`) и в GitHub CI Postgres поднимается сам.

Шаги процессов — в `flows.py` маленькими функциями: когда поток меняется
(способ оплаты при отгрузке, выбор товара при приёмке контейнера), правится
одна функция, а не каждый сценарий.
"""

from __future__ import annotations

import logging
import os
import uuid
from urllib.parse import urlparse

import pytest

PG_URL = os.environ.get("TEST_PG_URL", "")


def pytest_collection_modifyitems(items):
    for item in items:
        if "tests/scenarios" in str(item.fspath).replace("\\", "/") and not PG_URL:
            item.add_marker(pytest.mark.skip(reason="TEST_PG_URL не задан — сценарии идут на живом Postgres"))


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    rep = outcome.get_result()
    if rep.when == "call":
        item.scenario_call_passed = rep.passed and not hasattr(rep, "wasxfail")


class _ErrorLogCollector(logging.Handler):
    """ERROR и выше из любых потоков (сервер крутится в своём)."""

    def __init__(self) -> None:
        super().__init__(level=logging.ERROR)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@pytest.fixture
def scenario_db(monkeypatch):
    """Своя база на Postgres со схемой `init_db()` и складом по умолчанию."""
    import psycopg2

    from tests.test_money_postgres import _drop_async_pool, _reload_modules

    name = f"t_scen_{uuid.uuid4().hex[:12]}"
    admin = psycopg2.connect(PG_URL)
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute(f'CREATE DATABASE "{name}"')
    url = urlparse(PG_URL)._replace(path=f"/{name}").geturl()

    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("TELEGRAM_TOKEN", "0:fake-token-for-tests")
    _drop_async_pool()
    db = _reload_modules()
    db.init_db()
    db.seed_warehouses()
    import services.roles as roles

    roles.invalidate_all_roles()
    try:
        yield db
    finally:
        _drop_async_pool()
        pool = getattr(db, "_pg_connection_pool", None)
        if pool is not None:
            pool.closeall()
        monkeypatch.undo()
        _reload_modules()
        with admin.cursor() as cur:
            cur.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        admin.close()


@pytest.fixture
def world(scenario_db, monkeypatch, request):
    """Живой сервер + роли + HTTP-клиент. После теста — общие инварианты."""
    from tests.liveserver import live_server
    from tests.scenarios import flows, invariants

    db = scenario_db
    for uid, (username, role) in flows.USERS.items():
        db.set_role(uid, username, username.title(), role)
    import services.roles as roles

    roles.invalidate_all_roles()

    collector = _ErrorLogCollector()
    root = logging.getLogger()
    root.addHandler(collector)
    try:
        with live_server(db, monkeypatch, seed=False) as srv:
            w = flows.World(srv)
            try:
                yield w
                # Инварианты — только если сам сценарий дошёл до конца: иначе
                # вторая ошибка заслонит первую (и у xfail-сценария с известным
                # багом состояние заведомо не то).
                if getattr(request.node, "scenario_call_passed", False):
                    invariants.check_all(w, error_records=collector.records)
            finally:
                w.close()
    finally:
        root.removeHandler(collector)
