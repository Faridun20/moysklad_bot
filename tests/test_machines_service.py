"""
T4.2 — сервис учёта экскаваторов (services/machines).

Проверяем правила, ради которых слой вообще существует: себестоимость режется
здесь (а не на фронте), статус меняется только через CAS, моточасы пишутся
историей и не едут назад, фото идемпотентны, сделка и статус машины меняются
одной транзакцией.

БД настоящая (isolated_db), корутины гоняем через asyncio.run — pytest-asyncio
в проекте нет.
"""

import asyncio

import services.roles as roles


def _run(coro):
    return asyncio.run(coro)


def _machine(db, vin="JCB-001", **over):
    from services import machines

    payload = {"vin": vin, "name": "JCB 3CX", "created_by": 1, "creator_name": "Manager"}
    payload.update(over)
    res = _run(machines.create_machine(**payload))
    assert res["ok"], res
    return res["machine_id"]


def _setup_roles(db):
    roles.invalidate_all_roles()
    db.set_role(1, "mgr", "Manager", "manager")
    db.set_role(2, "boss", "Boss", "boss")


# ─── VIN ──────────────────────────────────────────────────────────────────────


def test_vin_normalization_collapses_separators():
    from services.machines import normalize_vin

    assert normalize_vin("  jcb-123 456_78 ") == "JCB12345678"
    assert normalize_vin("SANY—SY215") == "SANYSY215"
    assert normalize_vin(None) == ""


def test_same_vin_written_differently_is_rejected(isolated_db):
    """Один и тот же серийник из накладной и из мессенджера не должен дать
    две карточки на один экскаватор."""
    from services import machines

    db = isolated_db
    _setup_roles(db)
    _machine(db, vin="JCB-123 456")
    res = _run(machines.create_machine(vin="jcb123456", name="Дубль", created_by=1))
    assert res["ok"] is False
    assert "уже заведена" in res["error"]


def test_long_serial_is_accepted(isolated_db):
    """Корейские и китайские серийники короче/длиннее автомобильного VIN —
    жёсткая длина отсекла бы половину парка."""
    from services import machines

    db = isolated_db
    _setup_roles(db)
    assert _machine(db, vin="SY215C-9")
    res = _run(machines.create_machine(vin="X" * 40, name="Длинный", created_by=1))
    assert res["ok"], res


# ─── Роли и себестоимость ─────────────────────────────────────────────────────


def test_manager_never_sees_cost(isolated_db):
    from services import machines

    db = isolated_db
    _setup_roles(db)
    mid = _machine(db, price_cents=25_000_00, cost_cents=18_000_00)

    boss_view = _run(machines.get_machine(mid, role="boss"))
    mgr_view = _run(machines.get_machine(mid, role="manager"))

    assert boss_view["cost_cents"] == 18_000_00
    assert "cost_cents" not in mgr_view
    assert mgr_view["price_cents"] == 25_000_00  # цену продажи менеджер видит


def test_cost_is_stripped_in_lists_too(isolated_db):
    """Срез в слое сервиса, а не в одном экране: список — тот же путь наружу."""
    from services import machines

    db = isolated_db
    _setup_roles(db)
    _machine(db, cost_cents=1_000_00)
    rows = _run(machines.list_machines(role="manager"))
    assert rows and all("cost_cents" not in r for r in rows)


# ─── Статусы ──────────────────────────────────────────────────────────────────


def test_status_change_is_audited_and_applied(isolated_db):
    from services import machines

    db = isolated_db
    _setup_roles(db)
    mid = _machine(db)
    res = _run(machines.set_status(mid, "in_stock", user_id=2, full_name="Boss"))
    assert res["ok"] and res["to"] == "in_stock"
    assert _run(machines.get_machine(mid, role="boss"))["status"] == "in_stock"

    log = _run(db.get_audit_log(limit=10))
    assert any(e["action"] == "machine_status_changed" for e in log)


def test_status_cas_rejects_stale_expectation(isolated_db):
    """Пока один пользователь смотрел карточку, другой уже перевёл машину —
    безусловный UPDATE затёр бы чужое решение."""
    from services import machines

    db = isolated_db
    _setup_roles(db)
    mid = _machine(db)
    _run(machines.set_status(mid, "sold", user_id=2, expected="in_transit"))

    res = _run(machines.set_status(mid, "in_stock", user_id=2, expected="in_transit"))
    assert res["ok"] is False
    assert _run(machines.get_machine(mid, role="boss"))["status"] == "sold"


def test_unknown_status_rejected(isolated_db):
    from services import machines

    db = isolated_db
    _setup_roles(db)
    mid = _machine(db)
    res = _run(machines.set_status(mid, "prodano", user_id=2))
    assert res["ok"] is False


# ─── Моточасы ─────────────────────────────────────────────────────────────────


def test_hours_write_history_and_current(isolated_db):
    from services import machines

    db = isolated_db
    _setup_roles(db)
    mid = _machine(db, hours=1200)
    assert _run(machines.add_hours(mid, 1350, user_id=1, full_name="Manager"))["ok"]

    card = _run(machines.get_machine(mid, role="boss"))
    assert card["hours"] == 1350  # в карточке — последнее
    history = _run(machines.get_hours_history(mid))
    assert [h["hours"] for h in history] == [1350, 1200]  # в истории — оба


def test_hours_going_backwards_is_rejected_as_typo(isolated_db):
    """1500 вместо 15000 — самая частая опечатка; счётчик назад не идёт."""
    from services import machines

    db = isolated_db
    _setup_roles(db)
    mid = _machine(db, hours=15000)
    res = _run(machines.add_hours(mid, 1500, user_id=1))
    assert res["ok"] is False
    assert res["needs_force"] is True
    assert _run(machines.get_machine(mid, role="boss"))["hours"] == 15000


def test_hours_backwards_allowed_with_force(isolated_db):
    """Замена счётчика — законный случай, но требует явного подтверждения."""
    from services import machines

    db = isolated_db
    _setup_roles(db)
    mid = _machine(db, hours=15000)
    assert _run(machines.add_hours(mid, 10, user_id=2, force=True))["ok"]
    assert _run(machines.get_machine(mid, role="boss"))["hours"] == 10


def test_negative_hours_rejected(isolated_db):
    from services import machines

    db = isolated_db
    _setup_roles(db)
    mid = _machine(db)
    assert _run(machines.add_hours(mid, -1, user_id=1))["ok"] is False


# ─── Фото ─────────────────────────────────────────────────────────────────────


def test_photo_is_idempotent_by_unique_id(isolated_db):
    """Переслал ту же фотографию второй раз — не ошибка и не дубль в карточке."""
    from services import machines

    db = isolated_db
    _setup_roles(db)
    mid = _machine(db)
    first = _run(machines.add_photo(mid, tg_file_id="AgAC1", file_unique_id="U1", uploaded_by=1))
    second = _run(machines.add_photo(mid, tg_file_id="AgAC2", file_unique_id="U1", uploaded_by=1))
    assert first["ok"] and second["ok"]
    assert second["duplicate"] is True
    assert len(_run(machines.list_photos(mid))) == 1


def test_photo_requires_unique_id(isolated_db):
    from services import machines

    db = isolated_db
    _setup_roles(db)
    mid = _machine(db)
    res = _run(machines.add_photo(mid, tg_file_id="AgAC1", file_unique_id="", uploaded_by=1))
    assert res["ok"] is False


def test_photo_for_missing_machine_rejected(isolated_db):
    from services import machines

    db = isolated_db
    _setup_roles(db)
    res = _run(machines.add_photo(999, tg_file_id="A", file_unique_id="U", uploaded_by=1))
    assert res["ok"] is False


# ─── Сделки ───────────────────────────────────────────────────────────────────


def test_sale_moves_machine_to_sold(isolated_db):
    from services import machines

    db = isolated_db
    _setup_roles(db)
    mid = _machine(db)
    res = _run(
        machines.create_deal(
            mid, kind="sale", price_cents=30_000_00, buyer_name="Иванов", created_by=2
        )
    )
    assert res["ok"], res
    assert _run(machines.get_machine(mid, role="boss"))["status"] == "sold"


def test_second_deal_on_sold_machine_is_rejected(isolated_db):
    """Два «продал» подряд не должны создать две сделки на одну машину."""
    from services import machines

    db = isolated_db
    _setup_roles(db)
    mid = _machine(db)
    _run(machines.create_deal(mid, kind="sale", price_cents=1000, buyer_name="A", created_by=2))
    second = _run(
        machines.create_deal(mid, kind="sale", price_cents=1000, buyer_name="B", created_by=2)
    )
    assert second["ok"] is False
    assert len(_run(machines.list_deals(mid))) == 1


def test_credit_deal_requires_due_date(isolated_db):
    from services import machines

    db = isolated_db
    _setup_roles(db)
    mid = _machine(db)
    res = _run(
        machines.create_deal(mid, kind="credit", price_cents=1000, buyer_name="A", created_by=2)
    )
    assert res["ok"] is False
    # Машина осталась непроданной — сделка не прошла целиком.
    assert _run(machines.get_machine(mid, role="boss"))["status"] == "in_transit"


def test_credit_deal_sets_on_credit_and_closes_to_sold(isolated_db):
    from services import machines

    db = isolated_db
    _setup_roles(db)
    mid = _machine(db)
    deal = _run(
        machines.create_deal(
            mid, kind="credit", price_cents=50_000_00, buyer_name="Петров",
            created_by=2, due_date="2026-12-31",
        )
    )
    assert deal["ok"] and deal["status"] == "on_credit"
    assert len(_run(machines.get_open_credit_deals())) == 1

    assert _run(machines.close_deal(deal["deal_id"], user_id=2))["ok"]
    assert _run(machines.get_machine(mid, role="boss"))["status"] == "sold"
    assert _run(machines.get_open_credit_deals()) == []


def test_deal_price_must_be_positive_cents(isolated_db):
    from services import machines

    db = isolated_db
    _setup_roles(db)
    mid = _machine(db)
    for bad in (0, -100):
        res = _run(
            machines.create_deal(mid, kind="sale", price_cents=bad, buyer_name="A", created_by=2)
        )
        assert res["ok"] is False, bad


def test_buyer_passport_is_boss_only(isolated_db):
    """Паспорт — персональные данные. Режется там же, где себестоимость: в
    слое чтения, иначе первый же новый вызов вернёт его наружу."""
    from services import machines

    db = isolated_db
    _setup_roles(db)
    mid = _machine(db)
    _run(
        machines.create_deal(
            mid, kind="sale", price_cents=1000, buyer_name="Иванов",
            buyer_passport="AB1234567", created_by=2,
        )
    )
    assert _run(machines.list_deals(mid, role="boss"))[0]["buyer_passport"] == "AB1234567"
    assert "buyer_passport" not in _run(machines.list_deals(mid, role="manager"))[0]
    # Роль не передали — считаем, что показывать нельзя.
    assert "buyer_passport" not in _run(machines.list_deals(mid))[0]


# ─── Счётчики и граф переходов ────────────────────────────────────────────────


def test_count_by_status_matches_the_unfiltered_list(isolated_db):
    """`all` обязан совпадать с длиной списка без фильтра — иначе счётчик в
    интерфейсе врёт ровно на архив."""
    from services import machines

    db = isolated_db
    _setup_roles(db)
    _machine(db, vin="A-1")
    _machine(db, vin="A-2", status="in_stock")
    _machine(db, vin="A-3", status="archived")

    counts = _run(machines.count_by_status())
    assert counts["in_transit"] == 1
    assert counts["in_stock"] == 1
    assert counts["archived"] == 1
    assert counts["reserved"] == 0  # статус без машин присутствует нулём
    assert counts["all"] == len(_run(machines.list_machines(role="boss"))) == 2


def test_transition_graph_covers_every_status(isolated_db):
    """Статус без записи в графе — это экран без кнопок и без объяснения."""
    from services import machines

    assert set(machines.NEXT_STATUSES) == set(machines.STATUSES)
    for status, targets in machines.NEXT_STATUSES.items():
        assert set(targets) <= set(machines.STATUSES), status


def test_transition_options_carry_labels():
    """Подпись зависит от пары статусов: «на склад» и «снять бронь» ведут в
    один in_stock, но говорят о разном."""
    from services import machines

    assert machines.next_statuses("in_transit") == ("in_stock",)
    assert machines.next_status_options("in_transit")[0]["label"] == "🏗 На склад"
    assert machines.next_status_options("reserved")[0]["label"] == "🏗 Снять бронь"
    assert machines.next_status_options("archived") == []
    assert machines.next_status_options(None) == []


def test_sale_and_credit_are_not_in_the_manual_graph():
    """Продажа требует цены и покупателя, поэтому идёт через create_deal —
    кнопки «просто сменить статус на sold» быть не должно."""
    from services import machines

    reachable = {t for targets in machines.NEXT_STATUSES.values() for t in targets}
    assert "sold" not in reachable
    assert "on_credit" not in reachable


# ─── Удаление и архив ─────────────────────────────────────────────────────────


def test_delete_removes_children_explicitly(isolated_db):
    """FK с каскадом на SQLite не работают (pragma выключена), поэтому чистка
    детей — обязанность сервиса, иначе тесты и прод разъедутся."""
    from services import machines

    db = isolated_db
    _setup_roles(db)
    mid = _machine(db, hours=100)
    _run(machines.add_photo(mid, tg_file_id="A", file_unique_id="U", uploaded_by=1))

    assert _run(machines.delete_machine(mid, user_id=2))["ok"]
    assert _run(machines.get_machine(mid, role="boss")) is None
    assert _run(machines.list_photos(mid)) == []
    assert _run(machines.get_hours_history(mid)) == []


def test_machine_with_deal_is_not_deleted(isolated_db):
    """Сделка — денежный факт; удаление стёрло бы историю продажи."""
    from services import machines

    db = isolated_db
    _setup_roles(db)
    mid = _machine(db)
    _run(machines.create_deal(mid, kind="sale", price_cents=1000, buyer_name="A", created_by=2))
    res = _run(machines.delete_machine(mid, user_id=2))
    assert res["ok"] is False
    assert _run(machines.get_machine(mid, role="boss")) is not None


def test_archive_moves_only_old_sales(isolated_db):
    """T4.3: проданное больше 90 дней назад уходит в архив, свежее — остаётся."""
    from services import machines

    db = isolated_db
    _setup_roles(db)
    old = _machine(db, vin="OLD-1")
    fresh = _machine(db, vin="NEW-1")
    for mid in (old, fresh):
        _run(machines.create_deal(mid, kind="sale", price_cents=1000, buyer_name="A", created_by=2))
    # Старую сделку «состариваем» напрямую — время пишется локальным кадром.
    from datetime import timedelta

    from utils.helpers import local_now

    long_ago = (local_now() - timedelta(days=200)).strftime("%Y-%m-%d %H:%M:%S")
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(db.q("UPDATE machine_deals SET sold_at = ? WHERE machine_id = ?"), (long_ago, old))
        conn.commit()

    assert _run(machines.archive_sold_machines(days=90)) == 1
    assert _run(machines.get_machine(old, role="boss"))["status"] == "archived"
    assert _run(machines.get_machine(fresh, role="boss"))["status"] == "sold"


def test_archived_machines_hidden_from_default_list(isolated_db):
    from services import machines

    db = isolated_db
    _setup_roles(db)
    mid = _machine(db, vin="ARCH-1")
    _run(machines.set_status(mid, "archived", user_id=2))
    assert _run(machines.list_machines(role="boss")) == []
    assert len(_run(machines.list_machines(role="boss", status="archived"))) == 1


# ─── Крон архивации (T4.3) ────────────────────────────────────────────────────


def test_archive_cli_reads_setting_and_reports(isolated_db, monkeypatch):
    """CLI берёт порог из app_settings и не падает на кривом значении —
    настройку правит человек, а молча не архивировать хуже, чем взять дефолт."""
    from tasks import run_machines_archive

    db = isolated_db
    _setup_roles(db)
    monkeypatch.setattr(run_machines_archive, "init_db", lambda: None)

    captured = {}

    async def _fake_archive(days=90):
        captured["days"] = days
        return 3

    monkeypatch.setattr(run_machines_archive, "archive_sold_machines", _fake_archive)
    monkeypatch.setattr(run_machines_archive, "get_setting", lambda *_a: 30)
    assert run_machines_archive.main() == 0
    assert captured["days"] == 30

    monkeypatch.setattr(run_machines_archive, "get_setting", lambda *_a: "каждый вторник")
    assert run_machines_archive.main() == 0
    assert captured["days"] == 90  # дефолт вместо мусора


def test_archive_cli_returns_nonzero_on_failure(isolated_db, monkeypatch):
    """Cron-раннер должен увидеть провал: молчаливый успех спрячет поломку."""
    from tasks import run_machines_archive

    monkeypatch.setattr(run_machines_archive, "init_db", lambda: None)
    monkeypatch.setattr(run_machines_archive, "get_setting", lambda *_a: 90)

    async def _boom(days=90):
        raise RuntimeError("БД недоступна")

    monkeypatch.setattr(run_machines_archive, "archive_sold_machines", _boom)
    assert run_machines_archive.main() == 1
