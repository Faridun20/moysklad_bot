"""
MS-5 (волна 7) — остатки обновляются дельтой, а не полным срезом.

Почему это P0. Полный `report/stock/all` стоит 5 единиц бюджета за запрос, и
дебаунс запускал его после каждой пачки stock-вебхуков — раз в две секунды в
активный день. Три страницы номенклатуры = 15 единиц каждые 2 с ≈ 7,5 в
секунду при бюджете ≈7,3. Один этот путь способен съесть весь лимит аккаунта.

Мокаем ТРАНСПОРТ (aioresponses): реально собирается URL с changedSince и
stockType и разбирается ответ.
"""

import asyncio
import re
from datetime import datetime, timedelta

from aioresponses import CallbackResult, aioresponses

import services.moysklad as ms
import services.snapshot as snap

_CURRENT = re.compile(r".*report/stock/all/current.*")
_FULL = re.compile(r".*report/stock/all(\?.*)?$")


def _run(factory):
    async def scenario():
        ms._session = None
        try:
            return await factory()
        finally:
            await ms.close_session()

    return asyncio.run(scenario())


def _seed_stock(db, ms_id="prod-1", stock=10, reserve=0):
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(
            db.q(
                "INSERT INTO ms_stock (ms_id, name, folder_id, folder_name, unit, "
                "stock, reserve, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
            ),
            (ms_id, "Товар", "f1", "Категория", "шт", stock, reserve, db.now_str()),
        )
        conn.commit()


def _row(db, ms_id):
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(db.q("SELECT stock, reserve FROM ms_stock WHERE ms_id = ?"), (ms_id,))
        r = cur.fetchone()
    return dict(r) if r else None


def _fresh_meta(db, minutes_ago=5):
    stamp = (datetime.now() - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%d %H:%M:%S")
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(
            db.q(
                "INSERT INTO ms_snapshot_meta (dataset, last_refresh, last_full_refresh, status) "
                "VALUES ('stock', ?, ?, 'ok')"
            ),
            (stamp, stamp),
        )
        conn.commit()


# ─── Формат запроса ───────────────────────────────────────────────────────────


def test_delta_asks_both_stock_types_in_msk(isolated_db):
    """Эндпоинт принимает один stockType за раз, а момент ждёт в часовом поясе
    аккаунта (МСК). Наивная подстановка локального времени тихо сдвинула бы
    окно и часть изменений не приехала бы вовсе."""
    db = isolated_db
    _seed_stock(db)
    _fresh_meta(db)
    seen: list[dict] = []

    def cb(url, **kwargs):
        seen.append(dict(kwargs.get("params") or {}))
        return CallbackResult(status=200, payload=[])

    with aioresponses() as m:
        m.get(_CURRENT, callback=cb, repeat=True)
        _run(snap.refresh_stock_delta)

    assert [p["stockType"] for p in seen] == ["stock", "reserve"]
    asked = datetime.strptime(seen[0]["changedSince"], "%Y-%m-%d %H:%M:%S")
    local_naive = datetime.now()
    # МСК = UTC+3; локальный кадр контейнера в тестах может быть любым —
    # проверяем, что момент СДВИНУТ на разницу поясов, а не подставлен как есть.
    shift_hours = round((local_naive - asked).total_seconds() / 3600)
    expected_shift = round(
        (local_naive.astimezone().utcoffset().total_seconds() - 3 * 3600) / 3600
    )
    assert shift_hours in (expected_shift, expected_shift + 1)  # +нахлёст 2 мин


def test_delta_window_has_overlap(isolated_db):
    """changedSince отсекает строго «позже»; без нахлёста изменение той же
    секунды потерялось бы до следующего полного среза."""
    db = isolated_db
    _seed_stock(db)
    _fresh_meta(db, minutes_ago=10)
    seen: list[dict] = []

    def cb(url, **kwargs):
        seen.append(dict(kwargs.get("params") or {}))
        return CallbackResult(status=200, payload=[])

    with aioresponses() as m:
        m.get(_CURRENT, callback=cb, repeat=True)
        _run(snap.refresh_stock_delta)

    # Момент уехал в МСК, поэтому сравнивать с локальным «наивно» нельзя —
    # на контейнере в UTC (CI) и на машине в UTC+5 разница разная. Возвращаем
    # его в абсолютное время и только потом сравниваем.
    asked = datetime.strptime(seen[0]["changedSince"], "%Y-%m-%d %H:%M:%S").replace(
        tzinfo=snap._MSK
    )
    marker = (datetime.now().astimezone()) - timedelta(minutes=10)
    overlap = (marker - asked).total_seconds()
    assert overlap >= snap._DELTA_OVERLAP_SEC - 5  # секунда на исполнение теста
    assert overlap < snap._DELTA_OVERLAP_SEC + 60  # нахлёст, а не «с начала времён»


# ─── Применение ───────────────────────────────────────────────────────────────


def test_delta_updates_only_changed_rows(isolated_db):
    db = isolated_db
    _seed_stock(db, "prod-1", stock=10, reserve=1)
    _seed_stock(db, "prod-2", stock=99, reserve=0)
    _fresh_meta(db)

    def cb(url, **kwargs):
        kind = (kwargs.get("params") or {}).get("stockType")
        payload = [{"assortmentId": "prod-1", "stock": 42}] if kind == "stock" else []
        return CallbackResult(status=200, payload=payload)

    with aioresponses() as m:
        m.get(_CURRENT, callback=cb, repeat=True)
        assert _run(snap.refresh_stock_delta) == 1

    assert _row(db, "prod-1") == {"stock": 42, "reserve": 1}  # резерв не затёрт
    assert _row(db, "prod-2") == {"stock": 99, "reserve": 0}  # чужая строка цела


def test_delta_updates_reserve_independently(isolated_db):
    db = isolated_db
    _seed_stock(db, "prod-1", stock=10, reserve=1)
    _fresh_meta(db)

    def cb(url, **kwargs):
        kind = (kwargs.get("params") or {}).get("stockType")
        payload = [{"assortmentId": "prod-1", "stock": 7}] if kind == "reserve" else []
        return CallbackResult(status=200, payload=payload)

    with aioresponses() as m:
        m.get(_CURRENT, callback=cb, repeat=True)
        _run(snap.refresh_stock_delta)

    assert _row(db, "prod-1") == {"stock": 10, "reserve": 7}


def test_empty_delta_is_success_not_fallback(isolated_db):
    """Ничего не изменилось — это нормальный ответ, а не повод тянуть полный
    срез за 15 единиц бюджета."""
    db = isolated_db
    _seed_stock(db)
    _fresh_meta(db)

    with aioresponses() as m:
        m.get(_CURRENT, payload=[], repeat=True)
        assert _run(snap.refresh_stock_delta) == 0


# ─── Когда дельта неприменима ─────────────────────────────────────────────────


def test_no_marker_means_full_refresh(isolated_db):
    """Первый запуск: сравнивать не с чем — фикстура даёт пустую БД без меты."""
    assert _run(snap.refresh_stock_delta) == -1


def test_stale_marker_means_full_refresh(isolated_db):
    """changedSince глубже 24 часов не работает — просить бессмысленно."""
    db = isolated_db
    _seed_stock(db)
    _fresh_meta(db, minutes_ago=60 * 30)  # 30 часов назад
    assert _run(snap.refresh_stock_delta) == -1


def test_unknown_product_means_full_refresh(isolated_db):
    """Новая позиция номенклатуры: в дельте есть количество, но нет имени,
    категории и единицы. Вставить строку с пустым названием — показать
    менеджеру безымянный товар в каталоге."""
    db = isolated_db
    _seed_stock(db, "prod-1")
    _fresh_meta(db)

    with aioresponses() as m:
        m.get(_CURRENT, payload=[{"assortmentId": "prod-NEW", "stock": 5}], repeat=True)
        assert _run(snap.refresh_stock_delta) == -1

    assert _row(db, "prod-1") == {"stock": 10, "reserve": 0}  # ничего не тронуто


def test_hot_path_prefers_delta_over_full_pull(isolated_db, monkeypatch):
    """Проверяем связку: дебаунс зовёт дельту и НЕ идёт за полным срезом,
    пока дельта справляется."""
    db = isolated_db
    _seed_stock(db)
    _fresh_meta(db)
    calls = {"delta": 0, "full": 0}

    async def _delta(*a, **kw):
        calls["delta"] += 1
        return 3

    async def _full(*a, **kw):
        calls["full"] += 1
        return 0

    monkeypatch.setattr(snap, "refresh_stock_delta", _delta)
    monkeypatch.setattr(snap, "refresh_stock", _full)

    async def one_tick():
        snap.mark_stock_dirty()
        task = asyncio.create_task(snap._stock_debounce_loop())
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    monkeypatch.setattr(snap, "_DEBOUNCE_SEC", 0)
    asyncio.run(one_tick())
    assert calls["delta"] == 1
    assert calls["full"] == 0
