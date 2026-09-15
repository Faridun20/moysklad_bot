"""
Ручки бухгалтерии (`/api/acc/*`) — отдельным роутером, а не в `server.py`.

Правила те же, что у остальных `/api/*` (CLAUDE.md): каждая ручка проходит
`server._authorize` с явным `allowed_roles` и rate-limit, денежные записи
принимают `idempotency_key`. Логика — в `services/accounting.py`; здесь только
разбор запроса, актор и перевод `AccountingError` в HTTP-ответ с текстом,
который форма показывает внутри себя.

`server` импортируется НА ВЫЗОВЕ: `server.py` сам подключает этот роутер в
конце модуля, и импорт на уровне файла дал бы цикл.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from services import accounting as acc

router = APIRouter()

# Те же наборы, что в сервисе: права живут в одном месте.
_ROLES_RECORD = acc.ROLES_RECORD
_ROLES_MANAGE = acc.ROLES_MANAGE


def _server():
    from webapp import server

    return server


async def _auth(request: Request, roles: tuple[str, ...], scope: str, limit: int = 30):
    srv = _server()
    data = await request.json()
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="Неверный запрос")
    user = srv._authorize(data, allowed_roles=roles, rate_limit_scope=scope, rate_limit_max=limit)
    role = srv.get_role(user["id"])
    name = srv._actor_name(user) or user.get("username") or str(user["id"])
    actor = acc.Actor(
        user_id=int(user["id"]),
        name=name,
        role=role,
        username=f"@{user['username']}" if user.get("username") else "",
    )
    return data, actor


def _fail(e: acc.AccountingError) -> JSONResponse:
    return JSONResponse({"detail": e.message}, status_code=e.status)


@router.post("/api/acc/state")
async def api_acc_state(request: Request):
    """Включена ли бухгалтерия — отвечает всем рабочим ролям: по ответу фронт
    решает, показывать ли новые вкладки."""
    _, actor = await _auth(request, _ROLES_RECORD, "api_acc_state", 60)
    state = await acc.get_state()
    state["can_manage"] = actor.role in _ROLES_MANAGE
    return JSONResponse(state)


@router.post("/api/acc/settings")
async def api_acc_settings(request: Request):
    data, actor = await _auth(request, _ROLES_MANAGE, "api_acc_settings", 10)
    try:
        state = await acc.set_enabled(actor, bool(data.get("enabled")), data.get("start_date"))
    except acc.AccountingError as e:
        return _fail(e)
    state["can_manage"] = True
    return JSONResponse(state)


@router.post("/api/acc/accounts")
async def api_acc_accounts(request: Request):
    """Справочник счетов. Архив видит руководитель — менеджеру он не нужен:
    выбирать при записи из архивного счёта всё равно нельзя."""
    data, actor = await _auth(request, _ROLES_RECORD, "api_acc_accounts", 60)
    try:
        await acc.require_enabled()
    except acc.AccountingError as e:
        return _fail(e)
    include_archived = bool(data.get("include_archived")) and actor.role in _ROLES_MANAGE
    accounts = await acc.list_accounts(include_archived=include_archived)
    return JSONResponse({
        "accounts": accounts,
        "kinds": acc.ACCOUNT_KINDS,
        "currencies": await acc.known_currencies(),
        "base_currency": acc.base_currency(),
    })


@router.post("/api/acc/accounts/save")
async def api_acc_accounts_save(request: Request):
    data, actor = await _auth(request, _ROLES_MANAGE, "api_acc_accounts_save", 30)
    try:
        await acc.require_enabled()
        account = await acc.save_account(actor, data)
    except acc.AccountingError as e:
        return _fail(e)
    return JSONResponse({"ok": True, "account": account})


@router.post("/api/acc/accounts/archive")
async def api_acc_accounts_archive(request: Request):
    data, actor = await _auth(request, _ROLES_MANAGE, "api_acc_accounts_archive", 30)
    try:
        await acc.require_enabled()
        res = await acc.set_archived(actor, data.get("account_id"), bool(data.get("archived", True)))
    except acc.AccountingError as e:
        return _fail(e)
    return JSONResponse(res)


@router.post("/api/acc/balances")
async def api_acc_balances(request: Request):
    _, actor = await _auth(request, _ROLES_RECORD, "api_acc_balances", 60)
    try:
        await acc.require_enabled()
    except acc.AccountingError as e:
        return _fail(e)
    res = await acc.balances(actor)
    res["can_manage"] = actor.role in _ROLES_MANAGE
    return JSONResponse(res)


@router.post("/api/acc/rates")
async def api_acc_rates(request: Request):
    data, _ = await _auth(request, _ROLES_RECORD, "api_acc_rates", 60)
    try:
        await acc.require_enabled()
        day = acc._parse_date(data.get("date"), default=acc.today_str())
    except acc.AccountingError as e:
        return _fail(e)
    return JSONResponse(await acc.rates_view(day))


@router.post("/api/acc/receipt_targets")
async def api_acc_receipt_targets(request: Request):
    _, actor = await _auth(request, _ROLES_RECORD, "api_acc_receipt_targets", 60)
    try:
        await acc.require_enabled()
    except acc.AccountingError as e:
        return _fail(e)
    return JSONResponse(await acc.receipt_targets(actor))


@router.post("/api/acc/receipt")
async def api_acc_receipt(request: Request):
    """«Получил деньги». Менеджер записал — боссу уходит тот же пуш с кнопками
    «Принять/Отклонить», что и у старой «Отметить оплату»: подтверждение
    платежа не меняется, меняется только то, что деньги теперь лежат на
    конкретном счёте."""
    data, actor = await _auth(request, _ROLES_RECORD, "api_acc_receipt", 20)
    if not data.get("idempotency_key"):
        raise HTTPException(status_code=400, detail="idempotency_key обязателен")
    try:
        res = await acc.record_receipt(actor, data)
    except acc.AccountingError as e:
        return _fail(e)
    if not res.get("repeated") and res.get("payment_id") and res.get("payment_status") == "pending":
        await _server()._notify_bosses_payment_pending(
            int(data.get("order_id")), actor.name, int(res["payment_id"])
        )
    return JSONResponse(res)


@router.post("/api/acc/expense")
async def api_acc_expense(request: Request):
    data, actor = await _auth(request, _ROLES_RECORD, "api_acc_expense", 30)
    if not data.get("idempotency_key"):
        raise HTTPException(status_code=400, detail="idempotency_key обязателен")
    try:
        return JSONResponse(await acc.record_expense(actor, data))
    except acc.AccountingError as e:
        return _fail(e)


@router.post("/api/acc/transfer")
async def api_acc_transfer(request: Request):
    data, actor = await _auth(request, _ROLES_RECORD, "api_acc_transfer", 30)
    if not data.get("idempotency_key"):
        raise HTTPException(status_code=400, detail="idempotency_key обязателен")
    try:
        return JSONResponse(await acc.record_transfer(actor, data))
    except acc.AccountingError as e:
        return _fail(e)


@router.post("/api/acc/close_day")
async def api_acc_close_day(request: Request):
    data, actor = await _auth(request, _ROLES_RECORD, "api_acc_close_day", 30)
    if not data.get("idempotency_key"):
        raise HTTPException(status_code=400, detail="idempotency_key обязателен")
    try:
        return JSONResponse(await acc.close_day(actor, data))
    except acc.AccountingError as e:
        return _fail(e)


@router.post("/api/acc/journal")
async def api_acc_journal(request: Request):
    data, actor = await _auth(request, _ROLES_RECORD, "api_acc_journal", 60)
    try:
        return JSONResponse(await acc.journal(actor, data))
    except acc.AccountingError as e:
        return _fail(e)


@router.post("/api/acc/doc")
async def api_acc_doc(request: Request):
    data, actor = await _auth(request, _ROLES_RECORD, "api_acc_doc", 60)
    try:
        return JSONResponse(await acc.get_doc(actor, data.get("doc_id")))
    except acc.AccountingError as e:
        return _fail(e)


@router.post("/api/acc/void")
async def api_acc_void(request: Request):
    data, actor = await _auth(request, _ROLES_RECORD, "api_acc_void", 20)
    try:
        return JSONResponse(await acc.void_doc(actor, data))
    except acc.AccountingError as e:
        return _fail(e)
