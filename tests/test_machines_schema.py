"""
T4.1 — схема учёта экскаваторов.

Схема из плана — под Postgres (SERIAL/TIMESTAMPTZ/now()); в проекте она обязана
подниматься и на SQLite (тесты, локальный запуск), поэтому типы адаптированы
под dual-DB. Тесты проверяют не «таблица есть», а то, ради чего в DDL стоят
ограничения: деньги в копейках, статусы из белого списка, VIN уникален, фото не
дублируются и всегда несут file_unique_id.
"""

import asyncio
import sqlite3

import pytest

# Нарушение ограничения приходит драйвером БД: sqlite3.IntegrityError в тестах,
# psycopg2.errors.* на проде. Ловим общий предок обоих драйверов по DB-API —
# blanket Exception запрещён ruff (B017) и прятал бы опечатку в самом тесте.
_CONSTRAINT_ERRORS = (sqlite3.IntegrityError, sqlite3.OperationalError)


def _insert_machine(db, vin="JCB1234567890", **over):
    fields = {
        "vin": vin,
        "name": "JCB 3CX",
        "status": "in_transit",
        "created_by": 1,
        "created_at": db.now_str(),
        "updated_at": db.now_str(),
    }
    fields.update(over)
    cols = ", ".join(fields)
    marks = ", ".join("?" for _ in fields)
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(db.q(f"INSERT INTO machines ({cols}) VALUES ({marks})"), tuple(fields.values()))
        conn.commit()
    return _last_machine_id(db, fields["vin"])


def _last_machine_id(db, vin):
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(db.q("SELECT id FROM machines WHERE vin = ?"), (vin,))
        row = cur.fetchone()
    return row["id"] if isinstance(row, dict) else row[0]


def _exec(db, sql, params=()):
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(db.q(sql), params)
        conn.commit()


# ─── таблицы поднимаются ──────────────────────────────────────────────────────


def test_machine_tables_exist(isolated_db):
    db = isolated_db
    for table in ("machines", "machine_hours", "machine_photos", "machine_deals"):
        with db.get_conn() as conn:
            cur = db.get_cursor(conn)
            cur.execute(db.q(f"SELECT COUNT(*) AS c FROM {table}"))
            assert cur.fetchone() is not None, table


def test_init_db_is_idempotent(isolated_db):
    """init_db зовётся при старте каждого процесса — второй прогон не должен
    ни падать, ни терять данные."""
    db = isolated_db
    mid = _insert_machine(db)
    db.init_db()
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(db.q("SELECT COUNT(*) AS c FROM machines WHERE id = ?"), (mid,))
        row = cur.fetchone()
    assert (row["c"] if isinstance(row, dict) else row[0]) == 1


# ─── ограничения ──────────────────────────────────────────────────────────────


def test_status_check_rejects_unknown_value(isolated_db):
    """Статус — белый список: опечатка не должна тихо создать новый статус,
    по которому потом не найдётся ни одна витрина."""
    db = isolated_db
    with pytest.raises(_CONSTRAINT_ERRORS):
        _insert_machine(db, vin="BADSTATUS1", status="prodano")


@pytest.mark.parametrize(
    "status", ["in_transit", "in_stock", "reserved", "sold", "on_credit", "archived"]
)
def test_all_planned_statuses_accepted(isolated_db, status):
    db = isolated_db
    assert _insert_machine(db, vin=f"VIN-{status}", status=status)


def test_vin_is_unique(isolated_db):
    """VIN — естественный ключ машины: две карточки на один экскаватор means
    двойной учёт моточасов и сделок."""
    db = isolated_db
    _insert_machine(db, vin="DUPVIN0001")
    with pytest.raises(_CONSTRAINT_ERRORS):
        _insert_machine(db, vin="DUPVIN0001", name="Вторая карточка")


def test_hours_cannot_be_negative(isolated_db):
    db = isolated_db
    mid = _insert_machine(db, vin="HOURS00001")
    with pytest.raises(_CONSTRAINT_ERRORS):
        _exec(
            db,
            "INSERT INTO machine_hours (machine_id, hours, recorded_by, recorded_at) "
            "VALUES (?, ?, ?, ?)",
            (mid, -5, 1, db.now_str()),
        )


def test_deal_kind_is_restricted(isolated_db):
    db = isolated_db
    mid = _insert_machine(db, vin="DEAL000001")
    with pytest.raises(_CONSTRAINT_ERRORS):
        _exec(
            db,
            "INSERT INTO machine_deals (machine_id, kind, price_cents, buyer_name, "
            "created_by, sold_at) VALUES (?, ?, ?, ?, ?, ?)",
            (mid, "barter", 100_000, "Клиент", 1, db.now_str()),
        )


# ─── фото ─────────────────────────────────────────────────────────────────────


def _insert_photo(db, machine_id, tg_file_id="AgAC-1", file_unique_id="uniq-1"):
    _exec(
        db,
        "INSERT INTO machine_photos (machine_id, tg_file_id, file_unique_id, "
        "uploaded_by, uploaded_at) VALUES (?, ?, ?, ?, ?)",
        (machine_id, tg_file_id, file_unique_id, 1, db.now_str()),
    )


def test_photo_requires_file_unique_id(isolated_db):
    """Волна 7: tg_file_id живёт в паре «бот + сервер Bot API» и обнулится при
    переезде на локальный Bot API server, а file_unique_id переживёт его и
    покажет осиротевшие записи. Поэтому NOT NULL, а не «по возможности»."""
    db = isolated_db
    mid = _insert_machine(db, vin="PHOTO00001")
    with pytest.raises(_CONSTRAINT_ERRORS):
        _exec(
            db,
            "INSERT INTO machine_photos (machine_id, tg_file_id, uploaded_by, uploaded_at) "
            "VALUES (?, ?, ?, ?)",
            (mid, "AgAC-no-unique", 1, db.now_str()),
        )


def test_same_photo_twice_is_rejected(isolated_db):
    """Переслал ту же фотографию второй раз — карточка не должна дублировать её."""
    db = isolated_db
    mid = _insert_machine(db, vin="PHOTO00002")
    _insert_photo(db, mid, file_unique_id="AQADabc")
    with pytest.raises(_CONSTRAINT_ERRORS):
        _insert_photo(db, mid, tg_file_id="другой-file-id", file_unique_id="AQADabc")


def test_same_photo_on_another_machine_is_allowed(isolated_db):
    """Уникальность — в пределах машины: одна и та же фотография может законно
    относиться к двум карточкам (общий вид площадки)."""
    db = isolated_db
    first = _insert_machine(db, vin="PHOTO00003")
    second = _insert_machine(db, vin="PHOTO00004")
    _insert_photo(db, first, file_unique_id="AQADshared")
    _insert_photo(db, second, file_unique_id="AQADshared")


# ─── деньги ───────────────────────────────────────────────────────────────────


def test_money_columns_are_integer_cents(isolated_db):
    """Глобальное правило 6: никаких REAL в деньгах. Проверяем, что копейки
    доезжают до БД целыми и не теряют точность на больших суммах."""
    db = isolated_db
    mid = _insert_machine(db, vin="MONEY00001", price_cents=9_999_999_99, cost_cents=8_500_000_00)
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(db.q("SELECT price_cents, cost_cents FROM machines WHERE id = ?"), (mid,))
        row = cur.fetchone()
    price = row["price_cents"] if isinstance(row, dict) else row[0]
    cost = row["cost_cents"] if isinstance(row, dict) else row[1]
    assert (price, cost) == (9_999_999_99, 8_500_000_00)
    assert isinstance(price, int) and isinstance(cost, int)


def test_schema_has_no_float_money_columns(isolated_db):
    """Регресс на правило 6: денежная колонка не должна появиться как REAL —
    ошибка проявилась бы копейками, разъезжающимися после округлений."""
    db = isolated_db
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute("SELECT sql FROM sqlite_master WHERE name LIKE 'machine%'")
        ddl = " ".join(r["sql"] if isinstance(r, dict) else r[0] for r in cur.fetchall() if r)
    for line in ddl.splitlines():
        low = line.lower()
        if "real" in low or "float" in low:
            assert "cents" not in low, line


def test_order_link_is_optional(isolated_db):
    """Продажа техники может идти мимо заказа МойСклад — order_id/agent_ms_id
    остаются пустыми, и это не ошибка."""
    db = isolated_db
    mid = _insert_machine(db, vin="DEALNOORD1")
    _exec(
        db,
        "INSERT INTO machine_deals (machine_id, kind, price_cents, buyer_name, "
        "created_by, sold_at) VALUES (?, ?, ?, ?, ?, ?)",
        (mid, "sale", 12_000_000, "Покупатель", 1, db.now_str()),
    )
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(db.q("SELECT order_id, agent_ms_id FROM machine_deals WHERE machine_id = ?"), (mid,))
        row = cur.fetchone()
    assert (row["order_id"] if isinstance(row, dict) else row[0]) is None
    assert (row["agent_ms_id"] if isinstance(row, dict) else row[1]) is None


def test_machines_table_survives_async_read(isolated_db):
    """adb_core-путь (asyncpg на проде / aiosqlite в тестах) читает ту же
    таблицу — денежное ядро и машины делят один пул."""
    db = isolated_db
    from services import adb_core

    mid = _insert_machine(db, vin="ASYNC00001", price_cents=555)
    row = asyncio.run(adb_core.fetchrow("SELECT vin, price_cents FROM machines WHERE id = $1", mid))
    assert row["vin"] == "ASYNC00001"
    assert row["price_cents"] == 555
