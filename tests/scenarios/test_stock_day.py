"""Склад за день: товар → приход → остаток; контейнер от «в пути» до остатка."""

from __future__ import annotations

from tests.scenarios import flows as f
from tests.scenarios.invariants import expect_audit


def test_product_incoming_invoice_grows_stock(world):
    """Менеджер приходует товар — остаток растёт в БД и на экране «Склад».

    Расход напрямую менеджеру закрыт (отгрузка клиенту — только через заявку),
    руководству открыт; приход и расход сходятся в остатке.
    """
    w = world
    cable = f.create_product(w, "Кабель ВВГ 3x2.5", unit="м")
    lamp = f.create_product(w, "Лампа LED 12W")

    inv = f.incoming_invoice(w, f.MGR, [(cable, 120, 1.5), (lamp, 40, None)])
    assert inv["ok"] and inv["positions"] == 2
    assert f.stock(w, cable) == 120 and f.stock(w, lamp) == 40
    assert f.api_stock(w, f.MGR, cable) == 120

    # Второй приход по той же карточке складывается, а не перезаписывает.
    f.incoming_invoice(w, f.BOSS, [(cable, 30, 1.4)])
    assert f.stock(w, cable) == 150

    # Расход: менеджеру — 403, руководству — можно, но не больше остатка.
    cp = f.create_counterparty(w, f.BOSS, "ООО Свет")
    outgoing = {"type": "outgoing", "counterparty_id": cp,
                "items": [{"product_id": lamp, "quantity": 5, "price_cents": 300}]}
    assert w.status(f.MGR, "/api/wh/invoices/create", idempotency_key=f.key(), **outgoing) == 403
    w.call(f.BOSS, "/api/wh/invoices/create", idempotency_key=f.key(), **outgoing)
    assert f.stock(w, lamp) == 35
    too_much = {**outgoing, "items": [{"product_id": lamp, "quantity": 36, "price_cents": 300}]}
    w.call(f.BOSS, "/api/wh/invoices/create", expect=409, idempotency_key=f.key(), **too_much)
    assert f.stock(w, lamp) == 35, "отказ по остатку ничего не списал"

    expect_audit(w.db, "wh_invoice_create")


def test_container_from_transit_to_stock(world):
    """Контейнер: завели → состав (из каталога и новый товар) → прибыл →
    пересчитали с недостачей → остаток вырос на ФАКТ; правка факта в окне
    переоприходует, а не удваивает."""
    w = world
    cable = f.create_product(w, "Кабель ПВС 2x1.5", unit="м")
    f.incoming_invoice(w, f.MGR, [(cable, 10, None)])  # на складе уже что-то было

    cid = f.create_container(w, f.MGR, "MSKU 123 456 7")
    known = f.add_container_item(w, f.MGR, cid, "Кабель ПВС 2x1.5", 500, product_id=cable, unit="м")
    fresh = f.add_container_item(w, f.MGR, cid, "Розетка двойная белая", 200)
    # Новой позиции нет в каталоге — карточку заводит человек кнопкой.
    socket = f.link_new_product(w, f.MGR, cid, fresh)

    card = f.container_card(w, f.MGR, cid)
    assert card["container"]["status"] == "in_transit"
    assert f.stock(w, cable) == 10 and f.stock(w, socket) == 0, "в пути — остатка ещё нет"

    f.container_arrived(w, f.MGR, cid)
    res = f.receive_container(w, f.MGR, cid, {known: 480, fresh: 200})
    assert res["receipt"]["ok"], res["receipt"]
    assert f.stock(w, cable) == 490 and f.stock(w, socket) == 200

    card = f.container_card(w, f.MGR, cid)
    by_id = {int(i["id"]): i for i in card["items"]}
    assert by_id[known]["state"] == "short" and by_id[known]["delta"] == -20
    assert card["diff"]["short"] == 1
    assert card["receipt"] and card["receipt"].get("invoice_id")

    # Досчитали: коробка нашлась. Остаток едет за фактом, прежний приход отменён.
    res = f.receive_container(w, f.MGR, cid, {known: 500})
    assert res["receipt"]["ok"], res["receipt"]
    assert f.stock(w, cable) == 510 and f.stock(w, socket) == 200
    link = w.one("SELECT i.status FROM container_receipt r JOIN invoices i ON i.id = r.invoice_id "
                 "WHERE r.container_id = ?", (cid,))
    assert link["status"] == "confirmed"
    cancelled = w.rows("SELECT id FROM invoices WHERE type = 'incoming' AND status = 'cancelled'")
    assert len(cancelled) == 1, "прежний приход контейнера отменён, а не оставлен рядом"


def test_writeoff_and_inventory_count_day(world):
    """День пересчёта: часть товара разбилась, остальное пересчитали.

    Менеджер списывает бой с причиной, руководитель видит запись в журнале;
    затем менеджер пересчитывает склад, система сама считает дельты и проводит
    недостачу списанием, а излишек — приходом. Выручку это не трогает.
    """
    w = world
    cable = f.create_product(w, "Кабель КГ 3x4", unit="м")
    lamp = f.create_product(w, "Лампа LED 20W")
    f.incoming_invoice(w, f.MGR, [(cable, 100, 2.0), (lamp, 30, 1.0)])

    # 1. Разбилось — списываем с причиной. Больше остатка списать нельзя.
    broke = f.write_off(w, f.MGR, lamp, 3, "бой при разгрузке")
    assert broke["ok"] and f.stock(w, lamp) == 27
    f.write_off(w, f.MGR, lamp, 999, "недостача", expect=409)
    assert f.stock(w, lamp) == 27, "отказ ничего не списал"

    journal = f.writeoffs(w, f.BOSS)
    assert journal[0]["reason"] == "бой при разгрузке"
    assert journal[0]["items"][0]["quantity"] == 3

    # 2. Ошиблись — сторно возвращает товар, запись остаётся в журнале.
    wrong = f.write_off(w, f.MGR, cable, 5, "порча")
    assert f.stock(w, cable) == 95
    assert f.void_write_off(w, f.MGR, wrong["writeoff_id"])["ok"]
    assert f.stock(w, cable) == 100
    assert any(x["cancelled_at"] for x in f.writeoffs(w, f.BOSS))

    # 3. Пересчёт: по кабелю недостача, по лампам излишек.
    count = f.start_count(w, f.MGR, "ряд А")
    assert count["ok"]
    cid = count["count_id"]
    # Второй параллельный пересчёт по тому же складу не открывается.
    assert f.start_count(w, f.BOSS, "второй", expect=409)["code"] == "already_open"

    f.count_line(w, f.MGR, cid, cable, 97)
    f.count_line(w, f.MGR, cid, lamp, 29)
    card = f.count_card(w, f.MGR, cid)
    assert card["summary"]["short"] == 1 and card["summary"]["surplus"] == 1
    assert {ln["product_id"]: ln["delta"] for ln in card["lines"]} == {cable: -3.0, lamp: 2.0}

    applied = f.apply_count(w, f.MGR, cid)
    assert applied["ok"] and applied["writeoff"] and applied["surplus"]
    assert f.stock(w, cable) == 97 and f.stock(w, lamp) == 29
    # Повтор без ключа ловит CAS по статусу сессии.
    assert f.apply_count(w, f.MGR, cid, expect=409)["code"] == "count_closed"

    from_count = [x for x in f.writeoffs(w, f.BOSS) if x["count_id"] == cid]
    assert {x["kind"] for x in from_count} == {"writeoff", "surplus"}
    assert all(x["reason"] == "инвентаризация" for x in from_count)

    # 4. Списание — не продажа: в отчёте продаж его нет.
    report = w.call(f.BOSS, "/api/analytics", period="year")
    assert report["count"] == 0 and report["total"] == 0, "списания не попали в отгрузки"

    expect_audit(w.db, "stock_writeoff")
    expect_audit(w.db, "stock_count_apply")
