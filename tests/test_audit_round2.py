"""Регресс-тесты на фиксы второго аудита (12 пунктов).

Каждый тест воспроизводит найденный дефект и падал бы на коде до фикса.
Мокается граница с внешним миром (Telegram, subprocess), БД — настоящая.
"""

import asyncio
import io
import xml.etree.ElementTree as ET
import zipfile
from datetime import date

import pytest


def _run(coro):
    return asyncio.run(coro)


# ─── #2. Имя клиента/менеджера в HTML-пуше боссу экранируется ─────────────────


def test_payment_pending_push_escapes_user_names(isolated_db, monkeypatch):
    """Клиент «ООО <Строй>» без esc() рушил разметку — Telegram отвечал
    can't parse entities, и босс НЕ получал пуш о платеже на подтверждение."""
    import services.notifier as notifier
    import webapp.server as server

    db = isolated_db
    db.set_role(1, "b", "Boss", "boss")
    db.set_role(200, "m", "Mgr", "manager")
    oid = db.create_order(200, "Менеджер <script>", "")
    db.update_order_agent(oid, "1", "ООО <Строй> & Ко")
    db.add_order_item(oid, "Труба", "", 1, "шт", 100.0)
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(
            db.q("UPDATE orders SET payment_type = 'credit', status = 'shipped' WHERE id = ?"),
            (oid,),
        )
        cur.execute(
            db.q(
                "INSERT INTO payments (user_id, full_name, amount_cents, currency, status, "
                "order_id, created_at) VALUES (?, ?, ?, ?, 'pending', ?, ?)"
            ),
            (200, "Mgr", 5000, "USD", oid, db.now_str()),
        )
        pid = cur.lastrowid
        conn.commit()

    sent: list[str] = []

    async def fake_send(uid, text, **kw):
        sent.append(text)

    async def fake_recipients():
        return [1]

    monkeypatch.setattr(notifier, "tg_send_message", fake_send)
    monkeypatch.setattr(notifier, "aget_notify_recipients", fake_recipients)

    _run(server._notify_bosses_payment_pending(oid, "Менеджер <script>", pid))

    assert sent, "пуш должен уйти"
    text = sent[0]
    assert "&lt;Строй&gt; &amp; Ко" in text
    assert "&lt;script&gt;" in text
    assert "<Строй>" not in text and "<script>" not in text


# ─── #3. Excel: имя, начинающееся с «=», не становится формулой ───────────────


def test_excel_text_cells_cannot_become_formulas():
    from openpyxl import load_workbook

    from services.excel_export import _text, build_analytics_xlsx

    assert _text('=HYPERLINK("http://evil","x")').startswith("'=")
    assert _text("+1+1").startswith("'+")
    assert _text("@SUM(A1)").startswith("'@")
    assert _text("ООО Ромашка") == "ООО Ромашка"
    assert _text(None) == "—"

    payload = {
        "label": "=1+1",
        "top_clients": [{"name": '=HYPERLINK("http://evil","Клиент")', "revenue": 1, "count": 1}],
        "top_managers": [{"name": "-Иванов", "revenue": 1, "count": 1}],
        "top_products": [{"name": "@Труба", "qty": 1, "sum": 1, "profit": None}],
    }
    wb = load_workbook(io.BytesIO(build_analytics_xlsx(payload)))
    for sheet, cell in (("Клиенты", "A2"), ("Менеджеры", "A2"), ("Товары", "A2"), ("Сводка", "B2")):
        c = wb[sheet][cell]
        assert c.data_type == "s", f"{sheet}!{cell} стала формулой: {c.value!r}"
        assert str(c.value).startswith("'")


# ─── #9. Rate limit: у каждого скоупа своё окно ────────────────────────────────


def test_gc_sweep_respects_each_buckets_own_window(monkeypatch):
    import services.rate_limit as rl

    rl.reset()
    fake_now = [1000.0]
    monkeypatch.setattr(rl.time, "monotonic", lambda: fake_now[0])

    # Короткое окно (1 с) и длинное (100 с) у разных скоупов одного юзера.
    assert rl.acquire("short", 7, max_calls=5, window_sec=1.0)
    for _ in range(3):
        assert rl.acquire("long", 7, max_calls=3, window_sec=100.0)
    assert rl.acquire("long", 7, max_calls=3, window_sec=100.0) is False, "лимит длинного исчерпан"

    fake_now[0] += 5.0  # короткое окно протухло, длинное — нет
    rl._gc_sweep(fake_now[0])

    # Раньше sweep чистил ВСЕ корзины окном вызвавшего скоупа: длинная
    # теряла записи после 1 с и лимит становился мягче объявленного.
    assert rl.acquire("long", 7, max_calls=3, window_sec=100.0) is False
    assert rl.acquire("short", 7, max_calls=5, window_sec=1.0) is True
    rl.reset()


# ─── #10. Пароль БД — в PGPASSWORD, а не в argv pg_dump ────────────────────────


def test_split_password_moves_secret_out_of_url():
    from tasks.run_backup import _split_password

    url, pw = _split_password("postgresql://bot:p%2Fss%40w@postgres:5432/db")
    assert pw == "p/ss@w", "пароль раскодирован из URL"
    assert url == "postgresql://bot@postgres:5432/db"
    assert "p%2F" not in url

    url, pw = _split_password("postgresql://bot@postgres:5432/db")
    assert pw is None and url == "postgresql://bot@postgres:5432/db"

    url, pw = _split_password("postgresql://u:s@[::1]:5432/db")
    assert url == "postgresql://u@[::1]:5432/db" and pw == "s"


def test_pg_dump_native_passes_password_via_env_not_argv(tmp_path, monkeypatch):
    import subprocess

    from tasks import run_backup

    captured = {}

    class _Proc:
        def __init__(self, cmd, stdout=None, env=None):
            captured["cmd"] = cmd
            captured["env"] = env
            self.stdout = io.BytesIO(b"-- dump\n")
            self.returncode = 0

        def wait(self):
            return 0

    monkeypatch.setattr(subprocess, "Popen", _Proc)
    out = tmp_path / "d.gz"
    run_backup._pg_dump_native("postgresql://bot:s3cr%2Ft@h:5432/db", out)

    assert not any("s3cr" in part for part in captured["cmd"]), captured["cmd"]
    assert captured["env"]["PGPASSWORD"] == "s3cr/t"
    assert out.stat().st_size > 0


# ─── #12. Роль и деактивация — один запрос, один TTL ───────────────────────────


def test_role_and_deactivation_share_one_cached_read(isolated_db, monkeypatch):
    import services.roles as roles

    db = isolated_db
    db.set_role(300, "u", "U", "manager")
    roles.invalidate_all_roles()

    calls = {"n": 0}
    real = roles._db_role_and_deactivation

    def counting(uid):
        calls["n"] += 1
        return real(uid)

    monkeypatch.setattr(roles, "_db_role_and_deactivation", counting)

    assert roles.cached_role(300) == "manager"
    assert roles.cached_is_deactivated(300) is False
    assert roles.cached_role(300) == "manager"
    assert calls["n"] == 1, "роль и флаг — из одной записи кэша, один SELECT"

    # Понижение роли в этом процессе применяется сразу.
    db.set_role(300, "u", "U", "guest")
    assert roles.cached_role(300) == "guest"

    # TTL общий и равен 30 с — понижение доезжает до другого процесса не
    # позже блокировки.
    assert roles._AUTH_TTL == 30.0
    assert roles._ROLE_TTL == roles._AUTH_TTL


def test_deactivated_user_reads_as_guest_from_cache(isolated_db):
    import services.roles as roles

    db = isolated_db
    db.set_role(301, "u", "U", "boss")
    assert roles.cached_role(301) == "boss"
    assert _run(db.deactivate_user(301, by=1)) is True
    assert roles.cached_is_deactivated(301) is True
    assert roles.cached_role(301) == "guest"


# ─── #7. ship_order: перепроверка под замком, guard invoice_id IS NULL ─────────


def _stocked_order(db, qty=5):
    from services import container_receipt, warehouse

    pid = _run(container_receipt.create_product("Кабель"))["product_id"]
    wid = _run(warehouse.default_warehouse_id())
    _run(warehouse.create_invoice(
        invoice_type="incoming", warehouse_id=wid,
        items=[{"product_id": pid, "quantity": 20, "price_cents": None}],
    ))
    db.set_role(200, "m", "Mgr", "manager")
    oid = db.create_order(200, "Mgr", "")
    db.add_order_item(oid, "Кабель", "", qty, "шт", 10.0, product_id=pid)
    db.update_order_status(oid, "approved")
    return oid


def _invoices(db):
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(db.q("SELECT id FROM invoices WHERE type = 'outgoing'"))
        return [r[0] if not isinstance(r, dict) else r["id"] for r in cur.fetchall()]


def test_ship_order_recheck_under_lock_blocks_double_shipment(isolated_db, monkeypatch):
    """Ранняя проверка вне транзакции — только быстрый выход. Решает
    перепроверка ПОД замком: даже если её обойти, второй накладной не будет."""
    from services import order_shipment

    db = isolated_db
    oid = _stocked_order(db)
    order = _run(db.get_order(oid))
    items = _run(db.get_order_items(oid))

    assert _run(order_shipment.ship_order(order, items, user_id=200))["ok"]
    assert len(_invoices(db)) == 1

    # Обходим раннюю проверку — как будто два вызова прошли её одновременно.
    async def _none(_):
        return None

    monkeypatch.setattr(order_shipment, "get_shipment", _none)
    res = _run(order_shipment.ship_order(order, items, user_id=200))
    assert res.get("already_shipped") is True
    assert len(_invoices(db)) == 1, "вторая накладная не должна появиться"


def test_ship_order_retries_after_failure_row(isolated_db):
    """Строка с прежней неудачей (failed_at) обновляется, а не блокирует."""
    from services import order_shipment

    db = isolated_db
    oid = _stocked_order(db)
    _run(order_shipment._remember_failure(oid, "не хватило"))
    order = _run(db.get_order(oid))
    items = _run(db.get_order_items(oid))

    res = _run(order_shipment.ship_order(order, items, user_id=200))
    assert res["ok"] and not res.get("already_shipped")
    row = _run(order_shipment.get_shipment(oid))
    assert row["invoice_id"] and row["failed_at"] is None and row["error"] is None


# ─── #8. confirm_cash_deposit — одна транзакция ────────────────────────────────


def test_confirm_cash_deposit_rolls_back_deposit_if_closing_orders_fails(isolated_db, monkeypatch):
    """Раньше статус сдачи коммитился ДО закрытия заказов: падение между ними
    оставляло сдачу confirmed с незакрытыми заказами и без пути повтора."""
    import services.debts as debts

    db = isolated_db
    db.set_role(5001, "m", "Mgr", "manager")
    oid = db.create_order(5001, "Mgr", "")
    db.add_order_item(oid, "Товар", "", 1, "шт", 100.0)
    db.update_order_status(oid, "shipped")
    res = _run(db.create_cash_deposit(5001, 100.0, allocations=[(oid, 100.0)]))
    assert res["ok"], res

    real_calc = debts.calc_order_balances

    async def boom(*a, **kw):
        raise RuntimeError("БД отвалилась на середине")

    monkeypatch.setattr(debts, "calc_order_balances", boom)
    with pytest.raises(RuntimeError):
        _run(db.confirm_cash_deposit(res["deposit_id"], 1, "Boss"))

    dep = _run(db.get_cash_deposit(res["deposit_id"]))
    assert dep["status"] == "pending", "сдача обязана откатиться вместе с заказами"
    assert _run(db.get_order(oid))["payment_confirmed"] == 0

    # А повтор после починки — проходит и закрывает заказ. (Не undo(): он снёс
    # бы и DB_PATH фикстуры, и adb_core открыл бы пустую базу.)
    monkeypatch.setattr(debts, "calc_order_balances", real_calc)
    cres = _run(db.confirm_cash_deposit(res["deposit_id"], 1, "Boss"))
    assert cres["ok"] and oid in cres["closed_orders"]


def test_confirm_cash_deposit_second_call_is_rejected(isolated_db):
    db = isolated_db
    db.set_role(5002, "m", "Mgr", "manager")
    oid = db.create_order(5002, "Mgr", "")
    db.add_order_item(oid, "Товар", "", 1, "шт", 50.0)
    db.update_order_status(oid, "shipped")
    res = _run(db.create_cash_deposit(5002, 50.0, allocations=[(oid, 50.0)]))
    assert _run(db.confirm_cash_deposit(res["deposit_id"], 1, "Boss"))["ok"]
    again = _run(db.confirm_cash_deposit(res["deposit_id"], 1, "Boss"))
    assert again["ok"] is False and "уже обработана" in again["error"]


# ─── #6. docxtpl: спецсимволы в ФИО не ломают XML документа ────────────────────


def test_legal_template_escapes_special_characters(tmp_path):
    from services import legal_docs as ld

    ctx = ld.build_context(
        doc_type="raspiska_ru", city="Ташкент",
        debtor={"full_name": "Иванов & Ко <x>", "passport": "AA<1>"},
        creditor={"name": "ООО \"Рога\" & <Копыта>"},
        product_name="<b>Труба</b>", total_cents=100_000, currency="USD",
        start_date=date(2026, 1, 1), term_months=2, payment_type="single",
        installments_count=None, penalty_rate="0.1%", grace_days=3,
    )
    dst = tmp_path / "out.docx"
    ld.fill_template(ld.template_path("raspiska_ru"), ctx, dst)

    with zipfile.ZipFile(dst) as z:
        xml = z.read("word/document.xml").decode("utf-8")
    ET.fromstring(xml)  # валидный XML — LibreOffice его откроет
    assert "Иванов &amp; Ко &lt;x&gt;" in xml
    assert "<x>" not in xml and "<b>Труба</b>" not in xml


# ─── #4. Тёзки: под Postgres берётся advisory-lock по имени ─────────────────────


def test_namesake_lock_is_taken_on_postgres(isolated_db, monkeypatch):
    """На SQLite advisory-lock'а нет (и гонки тоже — писатель один). Проверяем,
    что под Postgres замок запрашивается ДО проверки тёзки, по нормализованному
    имени: иначе две транзакции обе не видят дубля и обе вставляют."""
    import services.counterparties as cp
    import services.container_receipt as cr
    from services import adb_core

    statements: list[tuple[str, tuple]] = []
    real_transaction = adb_core.transaction

    class _Spy:
        def __init__(self, inner):
            self._i = inner

        async def execute(self, q, *a):
            statements.append((q, a))
            if "pg_advisory_xact_lock" in q:
                return 0  # SQLite такой функции не знает — глотаем
            return await self._i.execute(q, *a)

        def __getattr__(self, name):
            return getattr(self._i, name)

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def spy_transaction():
        async with real_transaction() as txn:
            yield _Spy(txn)

    monkeypatch.setattr(adb_core, "transaction", spy_transaction)
    monkeypatch.setattr(cp, "USE_POSTGRES", True)
    monkeypatch.setattr(cr, "USE_POSTGRES", True)

    assert _run(cp.create("  ООО  Ромашка "))["ok"]
    assert _run(cr.create_product("Труба  ПВХ"))["ok"]

    locks = [a for q, a in statements if "pg_advisory_xact_lock" in q]
    assert ("counterparty:name:ооо ромашка",) in locks
    assert ("product:name:труба пвх",) in locks
    # Замок — первым, до SELECT'а тёзки.
    first = statements[0][0]
    assert "pg_advisory_xact_lock" in first
