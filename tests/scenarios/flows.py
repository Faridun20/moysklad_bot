"""Шаги бизнес-процессов для сценариев — ОДНА функция на шаг.

Правило: сценарий не собирает payload ручки сам, он зовёт шаг отсюда. Когда
поток меняется (отгрузка оплаченного заказа потребует разбивку по способам
оплаты; приёмка контейнера — выбор товара из каталога; у техники появится
кнопка «Прибыла»), правится функция здесь, а сценарии остаются как есть.

Каждый шаг идёт через HTTP-ручку WebApp под конкретным пользователем и
проверяет код ответа (по умолчанию 200). Ответ 5xx на любом вызове — сразу
падение: сценарий не имеет права «проскочить» сквозь серверную ошибку.
"""

from __future__ import annotations

import time
import uuid
from typing import Any

import httpx

# ─── Люди ────────────────────────────────────────────────────────────────────
# id совпадают с `tests/liveserver.ROLE_IDS` там, где он что-то знает о ролях
# (получатели уведомлений = босс 100).
ADMIN, BOSS, MGR, MGR2, KEEPER, BOOK, GUEST, FIRED = 1, 100, 200, 201, 400, 500, 900, 901
NOBODY = 999  # нет строки в user_roles вовсе

USERS: dict[int, tuple[str, str]] = {
    ADMIN: ("admin_user", "admin"),
    BOSS: ("boss_user", "boss"),
    MGR: ("mgr_user", "manager"),
    MGR2: ("mgr2_user", "manager"),
    KEEPER: ("keeper_user", "warehouse_keeper"),
    BOOK: ("book_user", "bookkeeper"),
    GUEST: ("guest_user", "guest"),
    FIRED: ("fired_user", "manager"),  # деактивируется в сценарии ролей
}


class ApiError(AssertionError):
    pass


class World:
    """Что видит сценарий: HTTP-клиент, БД (синхронный слой), журнал вызовов."""

    def __init__(self, srv) -> None:
        self.srv = srv
        self.db = srv.db
        self.http = httpx.Client(base_url=srv.base_url, timeout=60)
        self.calls: list[tuple[int, str, int]] = []

    def wrote_something(self) -> bool:
        """Был ли хоть один успешный вызов пишущей ручки (тогда обязан быть аудит)."""
        writes = ("create", "approve", "ship", "cancel", "confirm", "arrive", "check", "deal",
                  "receipt", "deactivate", "submit", "mark_paid")
        return any(code == 200 and path.rsplit("/", 1)[-1].startswith(writes)
                   for _, path, code in self.calls)

    def close(self) -> None:
        self.http.close()

    # ── HTTP ────────────────────────────────────────────────────────────────
    def call(self, uid: int, path: str, *, expect: int | tuple[int, ...] | None = 200, **payload) -> Any:
        body = {"initData": str(uid), **payload}
        r = self.http.post(path, json=body)
        self.calls.append((uid, path, r.status_code))
        if r.status_code >= 500:
            raise ApiError(f"{path} от {uid}: {r.status_code} {r.text[:500]}")
        if expect is not None:
            codes = (expect,) if isinstance(expect, int) else expect
            if r.status_code not in codes:
                raise ApiError(f"{path} от {uid}: ждали {codes}, пришло {r.status_code}: {r.text[:500]}")
        try:
            return r.json()
        except ValueError:
            return r.text

    def status(self, uid: int, path: str, **payload) -> int:
        """Только код ответа (для проверок прав)."""
        r = self.http.post(path, json={"initData": str(uid), **payload})
        self.calls.append((uid, path, r.status_code))
        if r.status_code >= 500:
            raise ApiError(f"{path} от {uid}: {r.status_code} {r.text[:500]}")
        return r.status_code

    # ── БД (синхронно — см. conftest) ───────────────────────────────────────
    def rows(self, sql: str, params=()) -> list[dict]:
        return self.srv.rows(sql, params)

    def one(self, sql: str, params=()) -> dict:
        rows = self.rows(sql, params)
        assert len(rows) == 1, f"ждали одну строку: {sql} {params} → {rows}"
        return rows[0]

    def exec(self, sql: str, params=()) -> None:
        self.srv.exec(sql, params)

    def wait_background(self, timeout: float = 15.0) -> None:
        """Дождаться фоновых задач сервера (печатная форма после одобрения).

        Задачи живут в `utils.background`; их loop — loop сервера, поэтому
        ждём снаружи опросом, а не `gather`.
        """
        from utils.background import pending

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not [t for t in pending() if not t.done()]:
                return
            time.sleep(0.05)


def key() -> str:
    return uuid.uuid4().hex


# ─── Каталог и склад ─────────────────────────────────────────────────────────


def create_product(w: World, name: str, unit: str = "шт") -> int:
    """Карточка товара.

    Ручки «завести товар» в WebApp пока нет: карточку заводят из позиции
    контейнера (`/api/containers/item_create_product`) или переносом из МС.
    Поэтому — прямая вставка тем же набором полей, что `container_receipt.
    create_product`. Появится ручка каталога — меняется только эта функция.
    """
    w.exec(
        "INSERT INTO products (name, unit, created_at) VALUES (?, ?, ?)",
        (name, unit, w.db.now_str()),
    )
    return int(w.rows("SELECT id FROM products WHERE name = ? ORDER BY id DESC", (name,))[0]["id"])


def create_counterparty(w: World, uid: int, name: str, phone: str = "+998900000000") -> int:
    body = w.call(uid, "/api/wh/counterparties/create", name=name, phone=phone, type="customer")
    cp = body.get("counterparty") or body
    return int(cp.get("id") or cp.get("counterparty_id"))


def incoming_invoice(w: World, uid: int, items: list[tuple[int, float, float | None]], *,
                     expect: int = 200) -> dict:
    """Приход: [(product_id, количество, цена за единицу | None)]."""
    return w.call(
        uid, "/api/wh/invoices/create", expect=expect, type="incoming", idempotency_key=key(),
        items=[
            {"product_id": pid, "quantity": qty,
             "price_cents": None if price is None else round(price * 100)}
            for pid, qty, price in items
        ],
    )


def stock(w: World, product_id: int) -> float:
    row = w.rows("SELECT COALESCE(SUM(quantity), 0) AS q FROM stock WHERE product_id = ?", (product_id,))
    return float(row[0]["q"])


def api_stock(w: World, uid: int, product_id: int) -> float:
    """Остаток глазами экрана «Склад» (`/api/wh/stock`)."""
    for p in w.call(uid, "/api/wh/stock")["products"]:
        if int(p["product_id"]) == product_id:
            return float(p["quantity"])
    return 0.0


# ─── Контейнер ───────────────────────────────────────────────────────────────


def create_container(w: World, uid: int, number: str, eta: str = "2030-01-10") -> int:
    body = w.call(uid, "/api/containers/create", number=number, eta_date=eta, idempotency_key=key())
    return int(body["container_id"])


def add_container_item(w: World, uid: int, container_id: int, name: str, expected_qty: float, *,
                       product_id: int | None = None, unit: str = "шт") -> int:
    body = w.call(uid, "/api/containers/item_add", container_id=container_id, name=name,
                  expected_qty=expected_qty, unit=unit, product_id=product_id)
    return int(body.get("item_id") or body.get("id"))


def link_new_product(w: World, uid: int, container_id: int, item_id: int) -> int:
    """Позиции нет в каталоге — «Завести товар» из карточки контейнера."""
    body = w.call(uid, "/api/containers/item_create_product", container_id=container_id, item_id=item_id)
    return int(body.get("product_id"))


def container_arrived(w: World, uid: int, container_id: int) -> dict:
    return w.call(uid, "/api/containers/arrive", container_id=container_id)


def receive_container(w: World, uid: int, container_id: int, quantities: dict[int, float]) -> dict:
    """Сверка фактических количеств и приход на склад.

    Сейчас `/api/containers/check` сохраняет факт и сам оприходует. Если поток
    приёмки разделится (выбор товара, отдельная кнопка «Оприходовать»), шаг
    дописывается здесь.
    """
    body = w.call(uid, "/api/containers/check", container_id=container_id,
                  quantities={str(k): v for k, v in quantities.items()})
    return body


def container_card(w: World, uid: int, container_id: int) -> dict:
    return w.call(uid, "/api/containers/card", container_id=container_id)


# ─── Продажа ─────────────────────────────────────────────────────────────────


def create_order(w: World, uid: int, counterparty_id: int, client_name: str,
                 items: list[tuple[int, str, float, float]], *, payment_type: str = "credit",
                 due_date: str | None = "2030-01-15", currency: str = "USD") -> dict:
    """Черновик → клиент → позиции [(product_id, название, кол-во, цена)] → заявка."""
    order_id = int(w.call(uid, "/api/orders/create")["order_id"])
    w.call(uid, "/api/orders/set_agent", order_id=order_id, agent_id=str(counterparty_id),
           agent_name=client_name)
    item_ids = []
    for pid, name, qty, price in items:
        body = w.call(uid, "/api/orders/add_item", order_id=order_id, product_id=pid,
                      product_name=name, quantity=qty, unit="шт", price=price, currency=currency)
        item_ids.append(int(body["item_id"]))
    sub = w.call(uid, "/api/orders/submit", order_id=order_id, payment_type=payment_type,
                 due_date=due_date if payment_type == "credit" else None, idempotency_key=key())
    return {"order_id": order_id, "req_id": int(sub["req_id"]), "item_ids": item_ids}


def approve(w: World, uid: int, req_id: int, *, override: bool = False, expect: int = 200) -> dict:
    body = w.call(uid, "/api/requests/approve", expect=expect, req_id=req_id, override=override,
                  idempotency_key=key())
    w.wait_background()
    return body


def receiving_account(w: World, uid: int, kind: str, **fields: Any) -> int:
    """Карта или счёт «куда поступили» (/api/pay_accounts/create) — заводит тот,
    кто вносит оплату, как в форме. Та же запись второй раз не заводится
    (сервер отдаёт `existed`), поэтому шаг можно звать перед каждой оплатой."""
    data = ({"holder": "Фаридун М.", "card_last4": "1234", "bank": "Kapitalbank"} if kind == "card"
            else {"holder": "ООО Farid Impeks", "account_number": "20208840900112236789", "bank": "Kapitalbank"})
    res = w.call(uid, "/api/pay_accounts/create", kind=kind, idempotency_key=key(), **{**data, **fields})
    return int(res["account"]["id"])


def record_payment(w: World, uid: int, order_id: int, payments: dict[str, float], *,
                   currency: str | None = None, accounts: dict[str, int] | None = None,
                   expect: int = 200) -> dict:
    """«Как получены деньги» (payments-flow): {"cash": …, "card": …, "bank": …}
    в валюте заказа → строки разбивки через /api/orders/payment. Карта и
    перечисление — с записью справочника «куда поступили» (`accounts` или
    тестовая карта/счёт, `receiving_account`)."""
    cur = currency or w.one("SELECT currency FROM orders WHERE id = ?", (order_id,))["currency"] or "USD"
    parts = []
    for m, a in payments.items():
        if not a:
            continue
        row: dict[str, Any] = {"method": m, "currency": cur, "amount": a}
        if m in ("card", "bank"):
            row["account_id"] = (accounts or {}).get(m) or receiving_account(w, uid, m)
        parts.append(row)
    return w.call(uid, "/api/orders/payment", expect=expect, order_id=order_id, parts=parts,
                  idempotency_key=key())


def ship_order(w: World, uid: int, order_id: int, *, payments: dict[str, float] | None = None,
               expect: int = 200) -> dict:
    """«Отгружен» (approved → shipped).

    `payments` — разбивка оплаты по способам {"cash": …, "card": …, "bank": …}.
    Её вносит автор заказа (он получил деньги) ДО отгрузки; «оплату сразу» без
    разбивки сервер не отгружает (409, code=payment_required).
    """
    if payments is not None:
        owner = w.one("SELECT user_id FROM orders WHERE id = ?", (order_id,))["user_id"]
        record_payment(w, owner, order_id, payments)
    return w.call(uid, "/api/orders/ship", expect=expect, order_id=order_id, idempotency_key=key())


def mark_paid(w: World, uid: int, order_id: int, amount: float | None = None, *, method: str = "card",
              expect: int = 200) -> dict:
    """Оплата долга — тоже разбивкой (сумма без способа сервером не принимается)."""
    if amount is None:
        amount = w.call(uid, "/api/orders/payment_context", order_id=order_id)["due_cents"] / 100
    return record_payment(w, uid, order_id, {method: amount}, expect=expect)


def confirm_payments(w: World, uid: int, order_id: int, *, expect: int = 200) -> dict:
    return w.call(uid, "/api/orders/confirm_payment", expect=expect, order_id=order_id,
                  idempotency_key=key())


def hand_over_cash(w: World, uid: int, amount: float, *, expect: int = 200) -> dict:
    """«Сдача» наличных менеджером (распределение по заказам — на сервере)."""
    return w.call(uid, "/api/deposits/create", expect=expect, amount=amount, idempotency_key=key())


def confirm_deposit(w: World, uid: int, deposit_id: int, *, expect: int = 200) -> dict:
    return w.call(uid, "/api/deposits/confirm", expect=expect, deposit_id=deposit_id,
                  idempotency_key=key())


def cancel_order(w: World, uid: int, order_id: int, reason: str = "Клиент передумал", *,
                 expect: int = 200) -> dict:
    return w.call(uid, "/api/orders/cancel", expect=expect, order_id=order_id, reason=reason)


def create_return(w: World, uid: int, order_id: int, items: list[tuple[int, float]] | None, *,
                  refund_method: str = "debt_reduction", reason: str = "Брак упаковки",
                  expect: int = 200) -> dict:
    payload: dict[str, Any] = {"order_id": order_id, "reason": reason,
                               "refund_method": refund_method, "idempotency_key": key()}
    if items is not None:
        payload["items"] = [{"item_id": iid, "quantity": qty} for iid, qty in items]
    return w.call(uid, "/api/returns/create", expect=expect, **payload)


def return_goods_received(w: World, uid: int, return_id: int, *, expect: int = 200) -> dict:
    return w.call(uid, "/api/returns/goods_received", expect=expect, return_id=return_id,
                  idempotency_key=key())


def confirm_return(w: World, uid: int, return_id: int, *, expect: int = 200) -> dict:
    return w.call(uid, "/api/returns/confirm", expect=expect, return_id=return_id, idempotency_key=key())


def debts(w: World, uid: int) -> dict:
    return w.call(uid, "/api/debts")


def debt_of(w: World, uid: int, order_id: int) -> dict | None:
    return next((d for d in debts(w, uid)["debts"] if int(d["id"]) == order_id), None)


def order_status(w: World, order_id: int) -> str:
    return w.one("SELECT status FROM orders WHERE id = ?", (order_id,))["status"]


# ─── Техника ─────────────────────────────────────────────────────────────────


def create_machine(w: World, uid: int, name: str, vin: str, *, price: float = 50000,
                   cost: float | None = None, status: str = "in_transit") -> int:
    payload: dict[str, Any] = {"name": name, "vin": vin, "price": price, "currency": "USD",
                               "status": status, "idempotency_key": key()}
    if cost is not None:
        payload["cost"] = cost
    return int(w.call(uid, "/api/machines/create", **payload)["machine_id"])


def machine_arrived(w: World, uid: int, machine_id: int) -> dict:
    """Машина прибыла: «в пути» → «на складе».

    Сейчас это смена статуса по графу (`/api/machines/status`); поток
    `container-receive` добавляет отдельную кнопку «Прибыла» — поменяется здесь.
    """
    return w.call(uid, "/api/machines/status", machine_id=machine_id, status="in_stock",
                  expected="in_transit")


def machine_card(w: World, uid: int, machine_id: int) -> dict:
    return w.call(uid, "/api/machines/card", machine_id=machine_id)


def machine_deal(w: World, uid: int, machine_id: int, *, kind: str, price: float | None, buyer: str,
                 down_payment: float = 0, months: int = 0, expect: int = 200,
                 approve_by: int | None = None) -> dict:
    """Бронь / продажа / рассрочка через `/api/machines/deal`.

    Менеджер получает заявку на одобрении (`pending`); `approve_by` — кто её
    сразу одобряет (шаг решения руководителя), ответ тогда — ответ одобрения
    (`deal_id`, `status`, `payments`). Руководство проводит сделку сразу.
    """
    payload: dict[str, Any] = {"machine_id": machine_id, "kind": kind, "price": price,
                               "buyer_name": buyer, "buyer_phone": "+998901112233",
                               "buyer_passport": "AA1234567", "currency": "USD",
                               "idempotency_key": key()}
    if kind == "credit":
        # Взнос «0» законен (рассрочка без первоначального взноса).
        payload.update(down_payment=down_payment, months=months)
    res = w.call(uid, "/api/machines/deal", expect=expect, **payload)
    if expect == 200 and approve_by is not None and res.get("pending"):
        return approve_machine_deal(w, approve_by, res["request_id"])
    return res


def machine_requests(w: World, uid: int) -> dict:
    return w.call(uid, "/api/machines/deals/pending")


def approve_machine_deal(w: World, uid: int, request_id: int, *, expect: int = 200) -> dict:
    return w.call(uid, "/api/machines/deals/approve", expect=expect, request_id=request_id,
                  idempotency_key=key())


def rework_machine_deal(w: World, uid: int, request_id: int, reason: str, *,
                        expect: int = 200) -> dict:
    return w.call(uid, "/api/machines/deals/rework", expect=expect, request_id=request_id,
                  reason=reason, idempotency_key=key())


def reject_machine_deal(w: World, uid: int, request_id: int, reason: str | None = None, *,
                        expect: int = 200) -> dict:
    return w.call(uid, "/api/machines/deals/reject", expect=expect, request_id=request_id,
                  reason=reason, idempotency_key=key())


def resubmit_machine_deal(w: World, uid: int, request_id: int, *, expect: int = 200,
                          **fields: Any) -> dict:
    return w.call(uid, "/api/machines/deals/resubmit", expect=expect, request_id=request_id,
                  idempotency_key=key(), **fields)


def machine_receipt(w: World, uid: int, deal_id: int, amount: float, *, expect: int = 200,
                    method: str = "cash") -> dict:
    """Поступление по рассрочке — со способом (и картой/счётом «куда»), как
    разбивка оплаты заказа."""
    extra = {"account_id": receiving_account(w, uid, method)} if method in ("card", "bank") else {}
    return w.call(uid, "/api/machines/receipt", expect=expect, deal_id=deal_id, amount=amount,
                  method=method, idempotency_key=key(), **extra)


def unreserve_machine(w: World, uid: int, machine_id: int, *, expect: int = 200) -> dict:
    return w.call(uid, "/api/machines/unreserve", expect=expect, machine_id=machine_id,
                  idempotency_key=key())


# ─── Расчёты с поставщиками ──────────────────────────────────────────────────


def container_supplier(w: World, uid: int, container_id: int, supplier_id: int,
                       name: str, *, expect: int = 200) -> dict:
    return w.call(uid, "/api/containers/supplier", expect=expect, container_id=container_id,
                  supplier_id=supplier_id, supplier_name=name)


def costing_enabled(w: World, uid: int, on: bool = True) -> dict:
    """Учёт себестоимости: без него закупочная цена в приход не уезжает, и долг
    перед поставщиком по контейнеру считать не из чего."""
    return w.call(uid, "/api/costing/settings/set", enabled=on)


def container_prices(w: World, uid: int, container_id: int, prices: dict[int, float], *,
                     currency: str = "USD", uzs_per_usd: str = "12700",
                     expect: int = 200) -> dict:
    """«Закупка и себестоимость» на карточке контейнера — цена за единицу."""
    return w.call(uid, "/api/costing/container/save", expect=expect, container_id=container_id,
                  currency=currency, uzs_per_usd=uzs_per_usd, rate_source="manual",
                  prices={str(k): str(v) for k, v in prices.items()})


def supplier_debts(w: World, uid: int, *, expect: int = 200) -> dict:
    return w.call(uid, "/api/suppliers/debts", expect=expect)


def supplier_terms(w: World, uid: int, invoice_id: int, payment_type: str = "credit",
                   due_date: str | None = None, *, expect: int = 200) -> dict:
    return w.call(uid, "/api/suppliers/terms", expect=expect, invoice_id=invoice_id,
                  payment_type=payment_type, due_date=due_date)


def supplier_payment(w: World, uid: int, supplier_id: int, amount: float, *,
                     invoice_id: int | None = None, method: str = "bank",
                     currency: str = "USD", rate: str | None = None,
                     account_id: int | None = None, expect: int = 200) -> dict:
    """Выплата поставщику той же формой, что и на экране: одна строка «как
    заплатили». Карта и перечисление указывают счёт, С КОТОРОГО ушли деньги."""
    row: dict[str, Any] = {"method": method, "currency": currency, "amount": amount}
    if method in ("card", "bank"):
        row["account_id"] = account_id or receiving_account(w, uid, method)
    if rate:
        row["rate"] = rate
    body: dict[str, Any] = {"supplier_id": supplier_id, "parts": [row], "idempotency_key": key()}
    if invoice_id:
        body["invoice_id"] = invoice_id
    else:
        body["currency"] = currency
    return w.call(uid, "/api/suppliers/payment", expect=expect, **body)
