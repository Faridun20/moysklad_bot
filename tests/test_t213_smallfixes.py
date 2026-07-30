"""T2.13 — семь мелких фиксов.

Каждый пункт — отдельный класс проблем, объединяет их только размер правки.
"""

import asyncio
import inspect
from datetime import datetime, timedelta

import services.database as db_module


# ─── 1. /addrole больше не затирает имя (§2.8) ──────────────────────────────


def test_addrole_keeps_existing_name(isolated_db):
    """set_role с пустыми username/full_name не должен стирать реальное имя.

    /addrole зовёт set_role(target, "", "", role) — у админа нет ни имени, ни
    username цели. Безусловный EXCLUDED затирал их, и /users показывал голый
    ID до следующего /start пользователя.
    """
    db = isolated_db
    db.set_role(555, "vasya", "Василий Петров", "manager")

    db.set_role(555, "", "", "boss")  # ← как из /addrole

    user = asyncio.run(db.get_user(555))
    assert user["full_name"] == "Василий Петров"
    assert user["username"] == "vasya"
    assert user["role"] == "boss"  # роль при этом меняется


def test_addrole_still_updates_name_when_given(isolated_db):
    """Непустое имя по-прежнему перезаписывает старое (/start)."""
    db = isolated_db
    db.set_role(556, "old", "Старое Имя", "manager")
    db.set_role(556, "new", "Новое Имя", "manager")

    user = asyncio.run(db.get_user(556))
    assert user["full_name"] == "Новое Имя"
    assert user["username"] == "new"


def test_addrole_warns_about_deactivated_user():
    """Админ должен видеть, что назначенная роль не действует."""
    src = inspect.getsource(__import__("handlers.users", fromlist=["cmd_addrole"]).cmd_addrole)
    assert "is_user_deactivated" in src
    assert "/reactivate" in src


# ─── 2. Чистка idempotency_keys (§3.5) ──────────────────────────────────────


def _put_key(db, key, expires_at):
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(
            db.q(
                "INSERT INTO idempotency_keys (key, operation, user_id, created_at, expires_at) "
                "VALUES (?, 'op', 1, ?, ?)"
            ),
            (key, db.now_str(), expires_at),
        )
        conn.commit()


def _keys(db):
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute("SELECT key FROM idempotency_keys ORDER BY key")
        return [r[0] for r in cur.fetchall()]


def test_expired_idempotency_keys_are_pruned(isolated_db):
    """expires_at писался, но никогда не читался — таблица росла вечно."""
    db = isolated_db
    past = (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S")
    future = (datetime.now() + timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S")
    _put_key(db, "stale", past)
    _put_key(db, "fresh", future)

    removed = db.prune_idempotency_keys()

    assert removed == 1
    assert _keys(db) == ["fresh"]


def test_prune_keys_is_idempotent(isolated_db):
    db = isolated_db
    _put_key(db, "stale", (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S"))
    assert db.prune_idempotency_keys() == 1
    assert db.prune_idempotency_keys() == 0


def test_maintenance_cron_prunes_keys():
    src = inspect.getsource(__import__("tasks.run_maintenance", fromlist=["main"]).main)
    assert "prune_idempotency_keys" in src


# ─── 3. Окно и LIMIT в реконсиляции МС (§3.9) ───────────────────────────────


def test_reconcile_window_defaults():
    cutoff, limit = db_module._reconcile_window()
    assert limit == 500
    # Порог — примерно 30 дней назад, посчитан в Python (не SQL NOW()).
    delta = datetime.now() - datetime.strptime(cutoff, "%Y-%m-%d %H:%M:%S")
    assert 29 <= delta.days <= 30


def test_reconcile_window_from_env(monkeypatch):
    monkeypatch.setenv("MS_RECONCILE_WINDOW_DAYS", "7")
    monkeypatch.setenv("MS_RECONCILE_LIMIT", "50")
    cutoff, limit = db_module._reconcile_window()
    assert limit == 50
    assert (datetime.now() - datetime.strptime(cutoff, "%Y-%m-%d %H:%M:%S")).days in (6, 7)


def test_reconcile_window_survives_garbage_env(monkeypatch):
    monkeypatch.setenv("MS_RECONCILE_WINDOW_DAYS", "не число")
    monkeypatch.setenv("MS_RECONCILE_LIMIT", "")
    cutoff, limit = db_module._reconcile_window()
    assert limit == 500 and cutoff


def test_old_orders_fall_out_of_reconcile_set(isolated_db):
    """Заказ вне окна не попадает в набор — иначе cron ходит в МС за архивом."""
    db = isolated_db
    db.set_role(1, "m", "M", "manager")
    old = db.create_order(1, "M", "")
    fresh = db.create_order(1, "M", "")
    db.set_order_ms_customerorder_id(old, "CO-OLD")
    db.set_order_ms_customerorder_id(fresh, "CO-NEW")
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(
            db.q("UPDATE orders SET updated_at = ?, created_at = ? WHERE id = ?"),
            ("2020-01-01 00:00:00", "2020-01-01 00:00:00", old),
        )
        conn.commit()

    ids = {o["id"] for o in asyncio.run(db.get_orders_with_ms_customerorder())}
    assert fresh in ids
    assert old not in ids, "заказ вне окна не должен попадать в реконсиляцию"


# ─── 4. /api/home: COUNT(*) вместо полных выборок (§3.8) ────────────────────


def test_count_boss_attention_matches_full_selects(isolated_db):
    """Счётчики должны совпадать с тем, что давали полные SELECT'ы."""
    db = isolated_db
    db.set_role(1, "m", "M", "manager")
    oid = db.create_order(1, "M", "")
    db.add_order_item(oid, "T", "href", 1, "шт", 100.0)
    db.update_order_agent(oid, "A-1", "Клиент")
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(db.q("UPDATE orders SET payment_type='credit' WHERE id=?"), (oid,))
        conn.commit()
    db.update_order_status(oid, "pending")
    db.update_order_status(oid, "approved")
    db.update_order_status(oid, "shipped")
    asyncio.run(db.mark_order_paid(oid, 1, "M", amount=50.0))
    asyncio.run(db.create_cash_deposit(1, 10.0))

    counts = asyncio.run(db.count_boss_attention())

    assert counts["deposits"] == len(asyncio.run(db.get_pending_cash_deposits()))
    assert counts["returns"] == len(asyncio.run(db.get_pending_returns()))
    assert counts["debts"] == len(asyncio.run(db.get_open_debts()))
    assert counts["payments"] == len(
        asyncio.run(db.get_paid_orders_awaiting_confirmation())
    )


def test_get_user_orders_has_limit(isolated_db):
    db = isolated_db
    db.set_role(1, "m", "M", "manager")
    for _ in range(7):
        db.create_order(1, "M", "")

    assert len(asyncio.run(db.get_user_orders(1, limit=3))) == 3
    assert len(asyncio.run(db.get_user_orders(1))) == 7  # дефолт 200 — все


# ─── 5. pending убран из _UPDATABLE_STATUSES (§2.17) ────────────────────────


def test_pending_no_longer_updatable_from_ms():
    """pending→shipped нелегален в TRANSITIONS, поэтому событие всегда уходило
    в ветку «нелегальный переход» и сыпало алертом боссу."""
    from services.ms_sync_handler import _UPDATABLE_STATUSES
    from services.order_workflow import TRANSITIONS

    assert "pending" not in _UPDATABLE_STATUSES
    # Оставшиеся статусы действительно умеют переходить в shipped.
    for st in _UPDATABLE_STATUSES:
        assert "shipped" in TRANSITIONS.get(st, []), f"{st}→shipped нелегален"


# ─── 6. Поллер берёт now ДО запроса (§2.15) ────────────────────────────────


def test_poller_timestamps_window_before_request():
    from services import notifier

    src = inspect.getsource(notifier.shipment_notifier)
    before = src.index("window_start = datetime.now()")
    request = src.index("await get_shipments(last_check)")
    assert before < request, "отметку времени снова берут после round-trip'а"


# ─── 7. BASE_CURRENCY вместо литерала USD (§3.7) ───────────────────────────


def test_deposit_screens_use_base_currency(monkeypatch):
    import handlers.deposits as dep
    import tasks.run_ops_monitor as ops

    monkeypatch.setenv("BASE_CURRENCY", "UZS")
    import config
    import importlib

    importlib.reload(config)

    assert dep._base_cur() == "UZS"
    assert ops._base_cur() == "UZS"


def test_no_hardcoded_usd_left_in_money_screens():
    import handlers.deposits as dep
    import tasks.run_ops_monitor as ops

    for mod in (dep, ops):
        src = inspect.getsource(mod)
        # Литерал допустим только внутри фолбэка _base_cur().
        for line in src.splitlines():
            if "USD" in line and "_base_cur" not in line and "BASE_CURRENCY" not in line:
                assert line.strip().startswith("#") or '"""' in line or "«USD»" in line, (
                    f"остался захардкоженный USD: {line.strip()}"
                )
