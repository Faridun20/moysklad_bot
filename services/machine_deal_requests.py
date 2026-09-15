"""
Заявки на сделки по технике: бронь, продажа и рассрочка — через одобрение.

Решение владельца: «Продажи и рассрочки техники — этим занимается менеджер. И
лучше отправлять запрос на одобрение того, что сделал менеджер, руководителю».

Поток:
1. Менеджер оформляет бронь / продажу / рассрочку (`submit`) → заявка
   `pending`. Машина статус НЕ меняет: «ждёт одобрения» выводится из живой
   заявки, а вторая заявка на ту же машину получает отказ (проверка под
   блокировкой машины + частичный UNIQUE `idx_machine_deal_requests_active`).
2. Руководитель получает карточку (цена против прайса, скидка, покупатель,
   условия, график) и решает: `approve` / `return_for_rework` (причина) /
   `reject`. Решение пишется CAS'ом по статусу заявки под `FOR UPDATE`.
3. Только одобрение двигает машину (`reserved` / `sold` / `on_credit`) и
   создаёт `machine_deals` + график (`machines.insert_deal_locked`) — в той же
   транзакции, что и отметка решения. Поэтому график и поступления есть только
   у одобренных сделок, а дебиторка, напоминания и бухгалтерия, которые читают
   `machine_deals`, о заявках не знают вовсе.
4. Отклонение машину не трогает — она и не менялась, «возврат в прежний
   статус» получается по построению. На доработке менеджер правит условия и
   отправляет снова (`resubmit`), машина остаётся за его заявкой.
5. Заявка руководства (admin/boss) одобряется сразу той же транзакцией:
   `approval_mode = 'auto'`, `decided_by` — он сам.

Кто решает, когда руководителя нет (сейчас менеджер один): как с деньгами
(`server._money_confirmers`) — менеджер решает сам ТОЛЬКО пока в системе нет ни
одного активного admin/boss (`approval_mode = 'no_boss'`), экран и аудит
говорят об этом прямо. Совмещение ролей (`ROLE_ALSO_ACTS_AS`) права решать не
даёт: кладовщик и бухгалтер сделок не одобряют.

Аудит и уведомления — ПОСЛЕ коммита: их пишет синхронный слой, и на SQLite он
ждал бы нашу же пишущую транзакцию.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from services import adb_core, machines, money
from services.database import USE_POSTGRES, add_audit_log, get_all_users, get_role, now_str
from utils.helpers import esc, local_now

logger = logging.getLogger(__name__)

KINDS = ("reserve", "sale", "credit")
# released — одобренная бронь, которую потом сняли (машина снова на складе):
# без отметки старая бронь менеджера давала бы ему право снять чужую, более
# позднюю бронь той же машины.
STATUSES = ("pending", "rework", "approved", "rejected", "cancelled", "released")
ACTIVE_STATUSES = ("pending", "rework")
# boss — решил руководитель; auto — руководитель оформил сам, одобрено сразу;
# no_boss — руководителя в системе нет, решил менеджер.
APPROVAL_MODES = ("boss", "auto", "no_boss")

APPROVER_ROLES = ("admin", "boss")
CREATOR_ROLES = ("admin", "boss", "manager")

KIND_LABELS = {"reserve": "Бронь", "sale": "Продажа", "credit": "Рассрочка"}
STATUS_LABELS = {
    "pending": "⏳ Ждёт одобрения",
    "rework": "↩️ На доработке",
    "approved": "✅ Одобрена",
    "rejected": "❌ Отклонена",
    "cancelled": "✖️ Отозвана",
    "released": "🔓 Бронь снята",
}

# Из каких статусов машины какая заявка законна. Бронь — только со склада
# (граф `machines.NEXT_STATUSES`), продажа и рассрочка — как у прямой сделки.
_KIND_FROM: dict[str, tuple[str, ...]] = {
    "reserve": ("in_stock",),
    "sale": machines._SELLABLE,
    "credit": machines._SELLABLE,
}

NO_BOSS_NOTE = "одобрено самим менеджером — руководителя в системе нет"


def allowed_kinds(machine_status: str | None) -> list[str]:
    """Какие заявки можно оформить на машину в этом статусе."""
    return [k for k in KINDS if (machine_status or "") in _KIND_FROM[k]]


# ─── Кто решает ──────────────────────────────────────────────────────────────


def _active_users() -> list[dict]:
    return [u for u in get_all_users() if not u.get("deactivated_at")]


async def decision_rights(viewer_id: int, role: str | None = None) -> dict:
    """Может ли `viewer_id` решать заявки и что сказать ему на экране.

    Зеркало `server._money_confirmers`: руководитель решает всегда; менеджер —
    только когда активных admin/boss нет вовсе. Иначе он молча одобрял бы
    собственные скидки в обход живого руководителя.
    """
    users = await asyncio.to_thread(_active_users)
    holders = [u for u in users if u.get("role") in APPROVER_ROLES]
    if role is None:
        role = await asyncio.to_thread(get_role, viewer_id)
    is_holder = role in APPROVER_ROLES
    exist = bool(holders)
    can = is_holder or (role == "manager" and not exist)
    names = [u.get("full_name") or str(u["user_id"]) for u in holders][:3]
    hint = (
        None if is_holder
        else "одобряете вы — руководителя в системе нет" if can
        else "решит " + (", ".join(names) or "руководитель")
    )
    return {"exist": exist, "can_decide": can, "viewer_is_holder": is_holder,
            "names": names, "hint": hint}


def _recipients(users: list[dict]) -> list[int]:
    """Кому уходит карточка решения: активные admin/boss; если их нет —
    менеджеры (они и решают, см. `decision_rights`)."""
    holders = [int(u["user_id"]) for u in users if u.get("role") in APPROVER_ROLES]
    if holders:
        return holders
    return [int(u["user_id"]) for u in users if u.get("role") == "manager"]


# ─── Чтение ──────────────────────────────────────────────────────────────────


def visible_request(row: dict | None, role: str | None) -> dict | None:
    """Заявка без паспорта покупателя для всех, кроме руководства (как
    `machines.visible_deal`). Сам факт, что паспорт вписан, — виден."""
    if row is None:
        return None
    data = dict(row)
    data["has_passport"] = bool(data.get("buyer_passport"))
    if role not in APPROVER_ROLES:
        data.pop("buyer_passport", None)
    data["kind_label"] = KIND_LABELS.get(data.get("kind") or "", data.get("kind"))
    data["status_label"] = STATUS_LABELS.get(data.get("status") or "", data.get("status"))
    data.update(terms(data))
    return data


def terms(req: dict) -> dict:
    """Скидка к прайсу и сводка графика — одно место на карточку бота и WebApp."""
    out: dict[str, Any] = {"discount_pct": None, "schedule_preview": None}
    price = req.get("price_cents")
    listed = req.get("list_price_cents")
    if price and listed and int(listed) > 0:
        out["discount_pct"] = round((int(listed) - int(price)) * 100 / int(listed), 1)
    if req.get("kind") == "credit" and price and int(req.get("months") or 0) > 0:
        down = int(req.get("down_payment_cents") or 0)
        months = int(req["months"])
        if not machines.validate_installment(int(price), down, months):
            sched = machines.build_schedule(int(price), down, months, local_now().date())
            monthly = [r for r in sched if r["seq"] > 0]
            out["schedule_preview"] = {
                "down_payment_cents": down,
                "months": months,
                "monthly_cents": int(monthly[0]["amount_cents"]),
                "last_cents": int(monthly[-1]["amount_cents"]),
                "first_due": monthly[0]["due_date"],
                "last_due": monthly[-1]["due_date"],
            }
    return out


async def get_request(request_id: int) -> dict | None:
    row = await adb_core.fetchrow(
        "SELECT r.*, m.name AS machine_name, m.vin AS machine_vin, m.status AS machine_status_now "
        "FROM machine_deal_requests r JOIN machines m ON m.id = r.machine_id WHERE r.id = $1",
        request_id,
    )
    return dict(row) if row else None


async def list_requests(*, statuses: tuple[str, ...] = ("pending",),
                        created_by: int | None = None) -> list[dict]:
    placeholders = ", ".join(f"${i + 1}" for i in range(len(statuses)))
    sql = (
        "SELECT r.*, m.name AS machine_name, m.vin AS machine_vin, "
        "m.status AS machine_status_now FROM machine_deal_requests r "
        f"JOIN machines m ON m.id = r.machine_id WHERE r.status IN ({placeholders})"
    )
    params: list[Any] = list(statuses)
    if created_by is not None:
        params.append(created_by)
        sql += f" AND r.created_by = ${len(params)}"
    sql += " ORDER BY r.submitted_at, r.id"
    return [dict(r) for r in await adb_core.fetch(sql, *params)]


async def active_by_machine(machine_ids: list[int] | None = None) -> dict[int, dict]:
    """Живые заявки по машинам одним запросом — для списка и карточки."""
    rows = await adb_core.fetch(
        "SELECT id, machine_id, kind, status, created_by, creator_name, price_cents, currency "
        "FROM machine_deal_requests WHERE status IN ('pending', 'rework')"
    )
    wanted = set(machine_ids) if machine_ids is not None else None
    out: dict[int, dict] = {}
    for r in rows:
        mid = int(r["machine_id"])
        if wanted is None or mid in wanted:
            item = dict(r)
            item["kind_label"] = KIND_LABELS.get(item["kind"], item["kind"])
            item["status_label"] = STATUS_LABELS.get(item["status"], item["status"])
            out[mid] = item
    return out


# ─── Запись ──────────────────────────────────────────────────────────────────


async def _audit(user_id: int, full_name: str, action: str, details: str) -> None:
    role = await asyncio.to_thread(get_role, user_id)
    await asyncio.to_thread(add_audit_log, user_id, full_name, role, action, details)


class _Abort(Exception):
    """Отказ внутри транзакции: откатить уже вставленное и вернуть `result`."""

    def __init__(self, result: dict):
        super().__init__(result.get("error"))
        self.result = result


def _is_unique_violation(exc: BaseException) -> bool:
    name = type(exc).__name__
    if name in ("IntegrityError", "UniqueViolationError"):
        return True
    text = str(exc).lower()
    return "unique" in text and "constraint" in text


async def _validate(
    kind: str, *, price_cents: int | None, currency: str, buyer_name: str,
    buyer_passport: str | None, down_payment_cents: int, months: int,
) -> str:
    """Проверки полей заявки ДО транзакции (курс читает синхронный слой)."""
    if kind not in KINDS:
        return f"Тип заявки: {' / '.join(KINDS)}"
    if not (buyer_name or "").strip():
        return "Покупатель обязателен"
    if kind == "reserve":
        # Цена у брони необязательна: бронируют машину, а не условия.
        ok, err = await machines._validate_cents(price_cents, "Цена", currency)
        return "" if ok else err
    err = await machines.prepare_deal(
        kind=kind, price_cents=int(price_cents or 0), buyer_name=buyer_name, currency=currency,
        down_payment_cents=down_payment_cents, months=months,
    )
    if err:
        return err
    if kind == "credit" and not (buyer_passport or "").strip():
        # Договор рассрочки без паспорта не составить.
        return "Паспорт покупателя обязателен для рассрочки"
    return ""


def _list_price(machine: dict, currency: str) -> int | None:
    """Прайс машины для скидки — только в той же валюте: скидку между долларовым
    прайсом и ценой в сумах посчитать нельзя, а неверный процент хуже пустого."""
    if (machine.get("currency") or "USD").upper() != (currency or "USD").upper():
        return None
    return machine.get("price_cents")


def _status_refusal(kind: str, machine: dict) -> dict | None:
    if machine["status"] in _KIND_FROM[kind]:
        return None
    label = machines.STATUS_LABELS.get(machine["status"], machine["status"])
    # Текст «сделка невозможна» — тот же, что у прямой сделки (`insert_deal_locked`):
    # по нему `_machine_response` и фронт узнают «карточка устарела».
    what = "бронь" if kind == "reserve" else "сделка"
    return {"ok": False, "error": f"Машина в статусе «{label}» — {what} невозможна",
            "current": machine["status"]}


async def _apply_locked(txn: Any, req: dict, machine: dict) -> dict:
    """Провести одобренную заявку: статус машины, сделка, график."""
    if req["kind"] == "reserve":
        moved = await txn.execute(
            "UPDATE machines SET status = 'reserved', updated_at = $1 "
            "WHERE id = $2 AND status = 'in_stock'",
            now_str(), int(machine["id"]),
        )
        if not moved:
            return _status_refusal("reserve", machine) or {
                "ok": False, "error": "Бронь невозможна", "current": machine["status"]}
        return {"ok": True, "status": "reserved", "status_from": machine["status"], "deal_id": None}
    return await machines.insert_deal_locked(
        txn, machine, kind=req["kind"], price_cents=int(req["price_cents"]),
        buyer_name=req["buyer_name"], created_by=int(req["created_by"]),
        currency=req["currency"], buyer_phone=req.get("buyer_phone"),
        buyer_passport=req.get("buyer_passport"), buyer_note=req.get("buyer_note"),
        agent_ms_id=req.get("agent_ms_id"),
        down_payment_cents=int(req.get("down_payment_cents") or 0),
        months=int(req.get("months") or 0),
    )


async def _lock_request(txn: Any, request_id: int) -> dict | None:
    sql = "SELECT * FROM machine_deal_requests WHERE id = $1"
    if USE_POSTGRES:
        sql += " FOR UPDATE"
    row = await txn.fetchrow(sql, request_id)
    return dict(row) if row else None


def _details(req: dict, machine: dict | None = None) -> str:
    name = (machine or {}).get("name") or req.get("machine_name") or ""
    parts = [f"заявка #{req['id']}", f"машина #{req['machine_id']}" + (f" {name}" if name else ""),
             KIND_LABELS.get(req["kind"], req["kind"])]
    if req.get("price_cents"):
        parts.append(f"{money.format_cents(int(req['price_cents']))} {req.get('currency') or 'USD'}")
    parts.append(str(req.get("buyer_name") or ""))
    if req["kind"] == "credit":
        parts.append(f"взнос {money.format_cents(int(req.get('down_payment_cents') or 0))}, "
                     f"{int(req.get('months') or 0)} мес.")
    return " · ".join(p for p in parts if p)


async def submit(
    machine_id: int,
    *,
    kind: str,
    actor_id: int,
    actor_name: str = "",
    actor_role: str,
    price_cents: int | None = None,
    currency: str = "USD",
    buyer_name: str = "",
    buyer_phone: str | None = None,
    buyer_passport: str | None = None,
    buyer_note: str | None = None,
    agent_ms_id: str | None = None,
    down_payment_cents: int = 0,
    months: int = 0,
    notify: bool = True,
) -> dict:
    """Оформить бронь / продажу / рассрочку.

    Менеджер → заявка `pending` + карточка руководителю. Руководство → заявка
    сразу `approved` (`approval_mode='auto'`) и сделка той же транзакцией —
    ответ совместим с прежним `create_deal` (`deal_id`, `status`, `payments`).
    """
    if actor_role not in CREATOR_ROLES:
        return {"ok": False, "error": "Оформлять сделки может менеджер или руководитель",
                "forbidden": True}
    currency = (currency or "USD").upper()
    if kind != "credit":
        down_payment_cents, months = 0, 0
    err = await _validate(kind, price_cents=price_cents, currency=currency, buyer_name=buyer_name,
                          buyer_passport=buyer_passport, down_payment_cents=down_payment_cents,
                          months=months)
    if err:
        return {"ok": False, "error": err}

    boss = actor_role in APPROVER_ROLES
    stamp = now_str()
    buyer = buyer_name.strip()
    try:
        async with adb_core.transaction() as txn:
            machine = await machines.lock_machine(txn, machine_id)
            if not machine:
                return {"ok": False, "error": "Машина не найдена"}
            active = await machines.active_request_locked(txn, machine_id)
            if active:
                return machines.pending_refusal(active)
            refusal = _status_refusal(kind, machine)
            if refusal:
                return refusal
            values = (
                machine_id, kind, price_cents, _list_price(machine, currency), currency,
                down_payment_cents, months, buyer, buyer_phone, buyer_passport, buyer_note,
                agent_ms_id, machine["status"], actor_id, actor_name, stamp,
            )
            sql = (
                "INSERT INTO machine_deal_requests (machine_id, kind, status, price_cents, "
                "list_price_cents, currency, down_payment_cents, months, buyer_name, buyer_phone, "
                "buyer_passport, buyer_note, agent_ms_id, machine_status, attempts, created_by, "
                "creator_name, created_at, submitted_at, updated_at) VALUES "
                "($1, $2, 'pending', $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, 1, $14, $15, "
                "$16, $16, $16)"
            )
            if USE_POSTGRES:
                request_id = int(await txn.fetchval(sql + " RETURNING id", *values))
            else:
                await txn.execute(sql, *values)
                request_id = int(await txn.fetchval("SELECT last_insert_rowid()"))
            applied: dict | None = None
            if boss:
                req = await _lock_request(txn, request_id)
                applied = await _apply_locked(txn, req or {}, machine)
                if not applied["ok"]:
                    raise _Abort(applied)
                await txn.execute(
                    "UPDATE machine_deal_requests SET status = 'approved', decided_by = $1, "
                    "decider_name = $2, decided_at = $3, updated_at = $3, approval_mode = 'auto', "
                    "deal_id = $4 WHERE id = $5",
                    actor_id, actor_name, stamp, applied.get("deal_id"), request_id,
                )
    except _Abort as abort:
        return abort.result
    except Exception as exc:
        if not _is_unique_violation(exc):
            raise
        # Параллельная заявка успела первой: индекс одной живой заявки на машину.
        async with adb_core.transaction() as txn:
            active = await machines.active_request_locked(txn, machine_id)
        if active:
            return machines.pending_refusal(active)
        raise

    req_view = {"id": request_id, "machine_id": machine_id, "kind": kind, "price_cents": price_cents,
                "currency": currency, "buyer_name": buyer, "down_payment_cents": down_payment_cents,
                "months": months, "machine_name": machine.get("name")}
    if boss and applied is not None:
        await _audit_applied(req_view, applied, actor_id, actor_name, mode="auto")
        out = {"ok": True, "request_id": request_id, "request_status": "approved",
               "pending": False, "approval_mode": "auto",
               "status": applied.get("status"), "deal_id": applied.get("deal_id")}
        if kind != "reserve":
            out.update(due_date=applied.get("due_date"), payments=applied.get("payments"))
        return out

    await _audit(actor_id, actor_name, "machine_deal_requested", _details(req_view, machine))
    if notify:
        await notify_decision_card(request_id)
    users = await asyncio.to_thread(_active_users)
    return {"ok": True, "request_id": request_id, "request_status": "pending", "pending": True,
            "status": machine["status"], "deal_id": None,
            # Руководителя в системе нет — решать будет сам менеджер (экран
            # говорит это сразу, а не «отправлено руководителю»).
            "self_decide": not any(u.get("role") in APPROVER_ROLES for u in users)}


async def _audit_applied(req: dict, applied: dict, actor_id: int, actor_name: str, *,
                         mode: str) -> None:
    """Аудит проведённой заявки: решение + прежние события техники (по ним
    строится история машины и сторожа сценариев)."""
    note = {"auto": "оформил руководитель", "boss": "одобрил руководитель",
            "no_boss": NO_BOSS_NOTE}[mode]
    await _audit(actor_id, actor_name, "machine_deal_approved", f"{_details(req)} · {note}")
    if applied.get("status_from") and applied.get("status"):
        await _audit(actor_id, actor_name, "machine_status_changed",
                     f"#{req['machine_id']}: {applied['status_from']} → {applied['status']}")
    if req["kind"] != "reserve":
        await _audit(
            actor_id, actor_name, "machine_deal_created",
            machines.deal_audit_details(
                int(req["machine_id"]), req["kind"], int(req["price_cents"]), req["currency"],
                req["buyer_name"], int(req.get("down_payment_cents") or 0),
                int(req.get("months") or 0)),
        )


async def _forbidden_unless_decider(actor_id: int, actor_role: str) -> tuple[dict | None, str]:
    rights = await decision_rights(actor_id, actor_role)
    if not rights["can_decide"]:
        return ({"ok": False, "forbidden": True,
                 "error": "Решение по заявке принимает руководитель"}, "")
    return None, ("boss" if rights["viewer_is_holder"] else "no_boss")


def _stale(req: dict) -> dict:
    return {"ok": False, "error": f"Заявка уже {STATUS_LABELS.get(req['status'], req['status']).lower()}",
            "current": req["status"]}


async def approve(request_id: int, *, actor_id: int, actor_name: str = "",
                  actor_role: str) -> dict:
    """Одобрить заявку: статус машины, сделка и график — одной транзакцией."""
    refusal, mode = await _forbidden_unless_decider(actor_id, actor_role)
    if refusal:
        return refusal
    stamp = now_str()
    try:
        async with adb_core.transaction() as txn:
            head = await txn.fetchrow(
                "SELECT machine_id FROM machine_deal_requests WHERE id = $1", request_id)
            if not head:
                return {"ok": False, "error": "Заявка не найдена"}
            # Порядок замков — «машина → заявка», как у `submit`.
            machine = await machines.lock_machine(txn, int(head["machine_id"]))
            req = await _lock_request(txn, request_id)
            if not req or not machine:
                return {"ok": False, "error": "Заявка не найдена"}
            if req["status"] != "pending":
                return _stale(req)
            applied = await _apply_locked(txn, req, machine)
            if not applied["ok"]:
                raise _Abort(applied)
            await txn.execute(
                "UPDATE machine_deal_requests SET status = 'approved', decided_by = $1, "
                "decider_name = $2, decided_at = $3, updated_at = $3, approval_mode = $4, "
                "decision_note = NULL, deal_id = $5 WHERE id = $6 AND status = 'pending'",
                actor_id, actor_name, stamp, mode, applied.get("deal_id"), request_id,
            )
    except _Abort as abort:
        return abort.result
    req["machine_name"] = machine.get("name")
    await _audit_applied(req, applied, actor_id, actor_name, mode=mode)
    await notify_outcome(req, "approved", actor_id, actor_name, applied=applied)
    return {"ok": True, "request_id": request_id, "request_status": "approved",
            "approval_mode": mode, "self_approved": mode == "no_boss",
            "status": applied.get("status"), "deal_id": applied.get("deal_id"),
            "payments": applied.get("payments"), "machine_id": int(req["machine_id"])}


async def _decide_without_apply(
    request_id: int, *, target: str, from_statuses: tuple[str, ...], actor_id: int,
    actor_name: str, reason: str | None, audit_action: str,
) -> dict:
    stamp = now_str()
    async with adb_core.transaction() as txn:
        req = await _lock_request(txn, request_id)
        if not req:
            return {"ok": False, "error": "Заявка не найдена"}
        if req["status"] not in from_statuses:
            return _stale(req)
        placeholders = ", ".join(f"${i + 6}" for i in range(len(from_statuses)))
        await txn.execute(
            "UPDATE machine_deal_requests SET status = $1, decided_by = $2, decider_name = $3, "
            f"decided_at = $4, updated_at = $4, decision_note = $5 WHERE id = ${len(from_statuses) + 6} "
            f"AND status IN ({placeholders})",
            target, actor_id, actor_name, stamp, reason, *from_statuses, request_id,
        )
        machine = await txn.fetchrow("SELECT name FROM machines WHERE id = $1", req["machine_id"])
    req["machine_name"] = (machine or {}).get("name")
    await _audit(actor_id, actor_name, audit_action,
                 _details(req) + (f" · причина: {reason}" if reason else ""))
    return {"ok": True, "request_id": request_id, "request_status": target,
            "machine_id": int(req["machine_id"]), "req": req}


async def return_for_rework(request_id: int, *, actor_id: int, actor_name: str = "",
                            actor_role: str, reason: str) -> dict:
    """На доработку: заявка ждёт правки менеджера, машина остаётся за ней."""
    reason = (reason or "").strip()[:500]
    if len(reason) < 3:
        return {"ok": False, "error": "Напишите, что доработать"}
    refusal, _mode = await _forbidden_unless_decider(actor_id, actor_role)
    if refusal:
        return refusal
    res = await _decide_without_apply(
        request_id, target="rework", from_statuses=("pending",), actor_id=actor_id,
        actor_name=actor_name, reason=reason, audit_action="machine_deal_returned",
    )
    if res["ok"]:
        await notify_outcome(res.pop("req"), "rework", actor_id, actor_name, reason=reason)
    return res


async def reject(request_id: int, *, actor_id: int, actor_name: str = "",
                 actor_role: str, reason: str | None = None) -> dict:
    """Отклонить. Машину не трогаем — заявка её статус и не меняла."""
    refusal, _mode = await _forbidden_unless_decider(actor_id, actor_role)
    if refusal:
        return refusal
    reason = (reason or "").strip()[:500] or None
    res = await _decide_without_apply(
        request_id, target="rejected", from_statuses=ACTIVE_STATUSES, actor_id=actor_id,
        actor_name=actor_name, reason=reason, audit_action="machine_deal_rejected",
    )
    if res["ok"]:
        await notify_outcome(res.pop("req"), "rejected", actor_id, actor_name, reason=reason)
    return res


async def cancel(request_id: int, *, actor_id: int, actor_name: str = "",
                 actor_role: str) -> dict:
    """Отозвать свою заявку (клиент передумал). Руководство — любую живую."""
    req = await get_request(request_id)
    if not req:
        return {"ok": False, "error": "Заявка не найдена"}
    if int(req["created_by"]) != int(actor_id) and actor_role not in APPROVER_ROLES:
        return {"ok": False, "forbidden": True, "error": "Отозвать можно только свою заявку"}
    res = await _decide_without_apply(
        request_id, target="cancelled", from_statuses=ACTIVE_STATUSES, actor_id=actor_id,
        actor_name=actor_name, reason=None, audit_action="machine_deal_cancelled",
    )
    res.pop("req", None)
    return res


async def resubmit(
    request_id: int,
    *,
    actor_id: int,
    actor_name: str = "",
    actor_role: str,
    price_cents: int | None = None,
    currency: str | None = None,
    buyer_name: str | None = None,
    buyer_phone: str | None = None,
    buyer_passport: str | None = None,
    buyer_note: str | None = None,
    down_payment_cents: int | None = None,
    months: int | None = None,
    notify: bool = True,
) -> dict:
    """Отправить заявку с доработки снова (правка условий). Только автор.

    Пустой паспорт оставляет прежний: менеджер его не видит (режется на
    чтении), и заставлять вписывать заново ради правки цены незачем.
    """
    req = await get_request(request_id)
    if not req:
        return {"ok": False, "error": "Заявка не найдена"}
    if int(req["created_by"]) != int(actor_id):
        return {"ok": False, "forbidden": True, "error": "Доработать может только автор заявки"}
    if req["status"] != "rework":
        return _stale(req)
    kind = req["kind"]
    merged = {
        "price_cents": req["price_cents"] if price_cents is None else price_cents,
        "currency": (currency or req["currency"] or "USD").upper(),
        "buyer_name": (buyer_name if buyer_name is not None else req["buyer_name"]) or "",
        "buyer_phone": buyer_phone if buyer_phone is not None else req.get("buyer_phone"),
        "buyer_passport": (buyer_passport or "").strip() or req.get("buyer_passport"),
        "buyer_note": buyer_note if buyer_note is not None else req.get("buyer_note"),
        "down_payment_cents": int(req.get("down_payment_cents") or 0)
        if down_payment_cents is None else down_payment_cents,
        "months": int(req.get("months") or 0) if months is None else months,
    }
    if kind != "credit":
        merged["down_payment_cents"], merged["months"] = 0, 0
    err = await _validate(kind, price_cents=merged["price_cents"], currency=merged["currency"],
                          buyer_name=merged["buyer_name"], buyer_passport=merged["buyer_passport"],
                          down_payment_cents=merged["down_payment_cents"], months=merged["months"])
    if err:
        return {"ok": False, "error": err}
    stamp = now_str()
    async with adb_core.transaction() as txn:
        machine = await machines.lock_machine(txn, int(req["machine_id"]))
        locked = await _lock_request(txn, request_id)
        if not locked or not machine:
            return {"ok": False, "error": "Заявка не найдена"}
        if locked["status"] != "rework":
            return _stale(locked)
        refusal = _status_refusal(kind, machine)
        if refusal:
            return refusal
        await txn.execute(
            "UPDATE machine_deal_requests SET status = 'pending', price_cents = $1, currency = $2, "
            "buyer_name = $3, buyer_phone = $4, buyer_passport = $5, buyer_note = $6, "
            "down_payment_cents = $7, months = $8, list_price_cents = $9, machine_status = $10, "
            "attempts = attempts + 1, submitted_at = $11, updated_at = $11 "
            "WHERE id = $12 AND status = 'rework'",
            merged["price_cents"], merged["currency"], merged["buyer_name"].strip(),
            merged["buyer_phone"], merged["buyer_passport"], merged["buyer_note"],
            merged["down_payment_cents"], merged["months"], _list_price(machine, merged["currency"]),
            machine["status"], stamp, request_id,
        )
    view = {**req, **merged, "id": request_id}
    await _audit(actor_id, actor_name, "machine_deal_resubmitted", _details(view, machine))
    if notify:
        await notify_decision_card(request_id)
    return {"ok": True, "request_id": request_id, "request_status": "pending", "pending": True}


# ─── Снять бронь ─────────────────────────────────────────────────────────────


async def release_bookings_locked(txn: Any, machine_id: int) -> int:
    """Одобренные брони машины → `released`. Зовётся тем же, кто снимает бронь."""
    return await txn.execute(
        "UPDATE machine_deal_requests SET status = 'released', updated_at = $1 "
        "WHERE machine_id = $2 AND kind = 'reserve' AND status = 'approved'",
        now_str(), machine_id,
    )


async def unreserve(machine_id: int, *, actor_id: int, actor_name: str = "",
                    actor_role: str) -> dict:
    """Снять бронь: «Забронирована» → «На складе».

    Решение владельца: продажи и брони техники — работа менеджера. Поэтому
    менеджер снимает бронь, которую оформил сам (клиент передумал), а любую —
    только пока руководителя в системе нет (`no_boss`, пометка в аудите).
    Прочие ручные переходы статуса остаются руководству (`/api/machines/status`).
    """
    rights = await decision_rights(actor_id, actor_role)
    async with adb_core.transaction() as txn:
        machine = await machines.lock_machine(txn, machine_id)
        if not machine:
            return {"ok": False, "error": "Машина не найдена"}
        if machine["status"] != "reserved":
            label = machines.STATUS_LABELS.get(machine["status"], machine["status"])
            return {"ok": False, "error": f"Машина не в брони — сейчас «{label}»",
                    "current": machine["status"]}
        active = await machines.active_request_locked(txn, machine_id)
        if active:
            return machines.pending_refusal(active)
        booking = await txn.fetchrow(
            "SELECT id, created_by FROM machine_deal_requests WHERE machine_id = $1 "
            "AND kind = 'reserve' AND status = 'approved' ORDER BY id DESC LIMIT 1",
            machine_id,
        )
        own = bool(booking) and int(booking["created_by"]) == int(actor_id)
        if rights["viewer_is_holder"]:
            mode = "boss"
        elif actor_role == "manager" and own:
            mode = "own"
        elif actor_role == "manager" and not rights["exist"]:
            mode = "no_boss"
        else:
            return {"ok": False, "forbidden": True,
                    "error": "Снять чужую бронь может руководитель"}
        await txn.execute(
            "UPDATE machines SET status = 'in_stock', updated_at = $1 "
            "WHERE id = $2 AND status = 'reserved'",
            now_str(), machine_id,
        )
        await release_bookings_locked(txn, machine_id)
    note = {"boss": "", "own": " · свою бронь",
            "no_boss": " · снял менеджер — руководителя в системе нет"}[mode]
    await _audit(actor_id, actor_name, "machine_unreserved",
                 f"#{machine_id} {machine.get('name') or ''}{note}")
    await _audit(actor_id, actor_name, "machine_status_changed",
                 f"#{machine_id}: reserved → in_stock")
    return {"ok": True, "from": "reserved", "to": "in_stock", "mode": mode,
            "booking_request_id": int(booking["id"]) if booking else None}


async def can_unreserve(machine: dict, *, viewer_id: int, role: str, rights: dict) -> bool:
    """Рисовать ли кнопку «Снять бронь» (та же логика, что `unreserve`, без записи)."""
    if machine.get("status") != "reserved":
        return False
    if rights["viewer_is_holder"]:
        return True
    if role != "manager":
        return False
    if not rights["exist"]:
        return True
    booking = await adb_core.fetchrow(
        "SELECT created_by FROM machine_deal_requests WHERE machine_id = $1 "
        "AND kind = 'reserve' AND status = 'approved' ORDER BY id DESC LIMIT 1",
        int(machine["id"]),
    )
    return bool(booking) and int(booking["created_by"]) == int(viewer_id)


# ─── Уведомления ─────────────────────────────────────────────────────────────


def _fmt(cents: int | None, currency: str) -> str:
    if cents is None:
        return "—"
    return f"{money.format_cents(int(cents), decimals=0, sep=' ')} {esc(currency)}"


def format_card(req: dict, *, no_boss: bool = False) -> str:
    """Карточка решения руководителю. Ввод менеджера — через `esc`."""
    view = visible_request(req, "boss") or {}
    cur = view.get("currency") or "USD"
    kind = view.get("kind") or ""
    title = {"reserve": "🔒 Бронь", "sale": "✅ Продажа", "credit": "💳 Рассрочка"}.get(kind, kind)
    lines = [
        f"🚜 <b>{title} техники — на одобрение</b> · заявка #{view.get('id')}",
        "",
        f"Машина: <b>{esc(view.get('machine_name') or '—')}</b> · VIN <code>{esc(view.get('machine_vin') or '—')}</code>",
        f"Менеджер: {esc(view.get('creator_name') or str(view.get('created_by')))}",
    ]
    if int(view.get("attempts") or 1) > 1:
        lines.append(f"Повторно, попытка {int(view['attempts'])}")
    price = view.get("price_cents")
    listed = view.get("list_price_cents")
    if price:
        line = f"Цена: <b>{_fmt(price, cur)}</b>"
        if listed:
            line += f" · прайс {_fmt(listed, cur)}"
            pct = view.get("discount_pct")
            if pct is not None and pct > 0:
                line += f" · <b>скидка {pct:g}%</b>"
            elif pct is not None and pct < 0:
                line += f" · выше прайса на {abs(pct):g}%"
        lines.append(line)
    elif listed:
        lines.append(f"Прайс: {_fmt(listed, cur)}")
    buyer = esc(view.get("buyer_name") or "—")
    if view.get("buyer_phone"):
        buyer += f" · {esc(view['buyer_phone'])}"
    lines.append(f"Покупатель: <b>{buyer}</b>")
    if view.get("buyer_passport"):
        lines.append(f"Паспорт: {esc(view['buyer_passport'])}")
    sched = view.get("schedule_preview")
    if kind == "credit" and sched:
        lines.append(
            f"Условия: взнос {_fmt(sched['down_payment_cents'], cur)}, "
            f"{int(sched['months'])} мес. по {_fmt(sched['monthly_cents'], cur)}"
        )
        lines.append(f"График: с {esc(sched['first_due'])} по {esc(sched['last_due'])} "
                     "(считается от дня одобрения)")
    if view.get("buyer_note"):
        lines.append(f"📝 {esc(view['buyer_note'])}")
    if no_boss:
        lines += ["", "ℹ️ Руководителя в системе нет — решение принимаете вы, это попадёт в журнал."]
    return "\n".join(lines)


async def notify_decision_card(request_id: int) -> None:
    """Карточка решения — сразу (это решение, а не сводка). Best-effort.

    Решение «пушить сейчас или копить» — как у всех боссовских пушей — за
    `notify_policy.should_notify_now(MACHINE_DEAL_APPROVAL)`: сейчас оно всегда
    «сразу» (одобрение блокирует продажу), но правило живёт в одном месте.
    """
    from services import notify_policy
    from services.notifier import tg_send_message
    from utils.keyboards import machine_request_keyboard

    try:
        req = await get_request(request_id)
        if not req or req["status"] != "pending":
            return
        if not notify_policy.should_notify_now(notify_policy.MACHINE_DEAL_APPROVAL):
            return
        users = await asyncio.to_thread(_active_users)
        recipients = _recipients(users)
        no_boss = not any(u.get("role") in APPROVER_ROLES for u in users)
        text = format_card(req, no_boss=no_boss)
        markup = machine_request_keyboard(request_id).model_dump(exclude_none=True, mode="json")
        for uid in recipients:
            await tg_send_message(uid, text, reply_markup=markup)
    except Exception:
        logger.exception("Карточка заявки на сделку #%s не отправлена", request_id)


async def notify_outcome(req: dict, outcome: str, actor_id: int, actor_name: str, *,
                         reason: str | None = None, applied: dict | None = None) -> None:
    """Менеджеру — чем кончилась его заявка. Сам себе не пишем."""
    from services.notifier import tg_send_message

    if int(req.get("created_by") or 0) == int(actor_id):
        return
    kind = KIND_LABELS.get(req.get("kind") or "", req.get("kind") or "").lower()
    head = f"{kind} «{esc(req.get('machine_name') or '#' + str(req.get('machine_id')))}»"
    who = esc(actor_name or "руководитель")
    if outcome == "approved":
        tail = {"reserve": "Машина забронирована.", "sale": "Машина продана.",
                "credit": "Рассрочка оформлена, график действует."}.get(req.get("kind") or "", "")
        text = f"✅ <b>Заявка #{req['id']} одобрена</b>: {head}\nОдобрил: {who}\n{tail}"
    elif outcome == "rework":
        text = (f"↩️ <b>Заявка #{req['id']} на доработке</b>: {head}\nВернул: {who}\n"
                f"📝 {esc(reason or '')}\n\nИсправьте условия в WebApp и отправьте снова.")
    else:
        text = (f"❌ <b>Заявка #{req['id']} отклонена</b>: {head}\nОтклонил: {who}"
                + (f"\n📝 {esc(reason)}" if reason else "")
                + "\nМашина осталась в прежнем статусе.")
    try:
        await tg_send_message(int(req["created_by"]), text)
    except Exception:
        logger.exception("Итог заявки #%s менеджеру не отправлен", req.get("id"))
