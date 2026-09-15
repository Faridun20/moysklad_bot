"""Ручная правка курса: границы и защита от ночной синхронизации.

* Поле формы — «сум за 1 USD», в базу уходит обратное число. Лишний ноль или
  перевёрнутый курс (12 600 вместо 1/12 600) проходил проверку «> 0» и
  пересчитывал все сводки в долларах в тысячи раз.
* Ручная правка держалась до ближайшего прогона `run_fx_sync` и молча
  исчезала. Теперь она пишется в дневной архив с source='manual', и синк того
  же дня её не трогает.

CBU мокается на транспорте (aioresponses), БД настоящая.
"""

from __future__ import annotations

import asyncio
import importlib
import re
from datetime import date

import pytest
from aioresponses import aioresponses
from fastapi.testclient import TestClient

CBU_USD = re.compile(r"^https://cbu\.uz/ru/arkhiv-kursov-valyut/json/USD/.*$")


# ─── Границы ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "rate",
    [
        12600.0,        # перевёрнут: «1 UZS = 12 600 USD»
        1 / 1260,       # пропущен ноль: 1 260 сум за доллар
        1 / 126000,     # лишний ноль
    ],
)
def test_implausible_uzs_rate_is_rejected_with_clear_message(isolated_db, rate):
    db = isolated_db
    ok, err = db.set_currency_rate("UZS", rate, updated_by=1)
    assert ok is False
    assert "сум за доллар" in err and "границ" in err
    assert db.get_currency_rate("UZS") is None


@pytest.mark.parametrize("uzs_per_usd", [5_000, 8_000, 12_600, 50_000])
def test_plausible_uzs_rate_is_accepted(isolated_db, uzs_per_usd):
    db = isolated_db
    ok, err = db.set_currency_rate("UZS", 1 / uzs_per_usd, updated_by=1)
    assert ok, err


def test_base_currency_rate_cannot_be_changed(isolated_db):
    db = isolated_db
    ok, err = db.set_currency_rate("USD", 0.5, updated_by=1)
    assert ok is False and "всегда 1" in err
    assert db.get_currency_rate("USD") == 1.0


# ─── Ручка ───────────────────────────────────────────────────────────────────


@pytest.fixture
def client_env(isolated_db, monkeypatch):
    import services.roles as roles
    import webapp.server as server

    importlib.reload(roles)
    isolated_db.set_role(100, "boss", "Boss", "boss")
    monkeypatch.setattr(
        server, "verify_init_data",
        lambda init_data: {"id": int(init_data), "first_name": "U", "username": "u"},
    )
    return TestClient(server.app), isolated_db


def _set(client, rate, code="UZS"):
    return client.post(
        "/api/currency/rates/set",
        json={"initData": "100", "currency_code": code, "rate_to_base": rate},
    )


@pytest.mark.parametrize("rate", [12600, 0, -0.0001, "abc", None, True])
def test_endpoint_rejects_bad_rate_with_400_text(client_env, rate):
    client, db = client_env
    resp = _set(client, rate)
    assert resp.status_code == 400, resp.text
    assert resp.json()["detail"]
    assert db.get_currency_rate("UZS") is None


def test_endpoint_writes_manual_rate_to_daily_archive(client_env):
    client, db = client_env
    resp = _set(client, 1 / 12700)
    assert resp.status_code == 200, resp.text
    today = db.now_str()[:10]
    assert db.get_currency_rate_daily_source("UZS", today) == "manual"
    assert db.get_currency_rate_asof("UZS", today) == pytest.approx(1 / 12700)


# ─── Ночная синхронизация не затирает ручную правку дня ──────────────────────


def _run_sync(cbu_rate: str) -> int:
    from services import fx_rates
    from tasks import run_fx_sync

    async def go():
        try:
            with aioresponses() as m:
                m.get(CBU_USD, payload=[{"Ccy": "USD", "Rate": cbu_rate}], repeat=True)
                return await run_fx_sync.main()
        finally:
            await fx_rates.close_session()

    return asyncio.run(go())


def test_sync_same_day_keeps_manual_rate(isolated_db):
    db = isolated_db
    ok, err = db.set_currency_rate_manual("UZS", 1 / 13000, updated_by=100)
    assert ok, err

    assert _run_sync("12052.05") == 0

    today = date.today().strftime("%Y-%m-%d")
    db._invalidate_currency_rates_cache()
    assert db.get_currency_rate("UZS") == pytest.approx(1 / 13000), "ручной курс перезаписан"
    assert db.get_currency_rate_daily_source("UZS", today) == "manual"
    assert db.get_currency_rate_asof("UZS", today) == pytest.approx(1 / 13000)


def test_sync_without_manual_rate_updates_as_before(isolated_db):
    db = isolated_db
    assert _run_sync("12052.05") == 0
    today = date.today().strftime("%Y-%m-%d")
    db._invalidate_currency_rates_cache()
    assert db.get_currency_rate("UZS") == pytest.approx(1 / 12052.05)
    assert db.get_currency_rate_daily_source("UZS", today) == "cbu"


def test_manual_rate_of_previous_day_is_overwritten_next_day(isolated_db):
    """Защита — только на ТОТ ЖЕ день: вчерашняя правка не держит курс вечно."""
    db = isolated_db
    ok, _ = db.set_currency_rate("UZS", 1 / 13000, updated_by=100)
    assert ok
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(
            db.q(
                "INSERT INTO currency_rate_daily (currency_code, rate_date, rate_to_base, "
                "source, created_at) VALUES (?, ?, ?, 'manual', ?)"
            ),
            ("UZS", "2000-01-01", 1 / 13000, db.now_str()),
        )
        conn.commit()

    assert _run_sync("12052.05") == 0
    db._invalidate_currency_rates_cache()
    assert db.get_currency_rate("UZS") == pytest.approx(1 / 12052.05)


def test_auto_archive_write_never_replaces_manual_day(isolated_db):
    db = isolated_db
    assert db.set_currency_rate_manual("UZS", 1 / 13000, updated_by=100)[0]
    today = db.now_str()[:10]
    assert db.set_currency_rate_daily("UZS", today, 1 / 12000, source="cbu")[0]
    assert db.get_currency_rate_asof("UZS", today) == pytest.approx(1 / 13000)
    # А вторая ручная правка того же дня — законно заменяет первую.
    assert db.set_currency_rate_manual("UZS", 1 / 12800, updated_by=100)[0]
    assert db.get_currency_rate_asof("UZS", today) == pytest.approx(1 / 12800)


def test_sync_rejects_anomalous_cbu_rate(isolated_db):
    """Аномальный ответ источника — громкий отказ (rc=1), а не запись."""
    db = isolated_db
    assert _run_sync("120.5") == 1
    assert db.get_currency_rate("UZS") is None
