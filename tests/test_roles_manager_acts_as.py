"""
Временное совмещение ролей: менеджер = + кладовщик + бухгалтер.

Решение владельца: отдельных кладовщика и бухгалтера пока нет, всё делает
менеджер, а руководитель проверяет. Совмещение живёт одной таблицей
(`services.roles.ROLE_ALSO_ACTS_AS`), поэтому проверяем не отдельные кортежи, а
свойства:

* менеджер получает РОВНО ручки кладовщика и бухгалтера — ни одной
  admin/boss-only (себестоимость, одобрение, подтверждение возврата);
* фронт-зеркало не разъехалось с сервером;
* бот, очередь «Сегодня», граф переходов и уведомления видят то же совмещение;
* назначить кладовщика/бухгалтера нельзя, но уже назначенные работают.

При откате совмещения (пустой `ROLE_ALSO_ACTS_AS`) этот файл удаляется целиком.
"""

from __future__ import annotations

import asyncio
import pathlib
import re

import services.roles as roles

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _run(coro):
    return asyncio.run(coro)


# ─── Таблица и её зеркало ────────────────────────────────────────────────────


def test_js_mirror_matches_python_table():
    """Фронт рисует кнопки по своей копии: разъезд = кнопки, отвечающие 403."""
    src = (ROOT / "webapp" / "static" / "helpers.js").read_text(encoding="utf-8")
    m = re.search(r"const ROLE_ALSO_ACTS_AS = \{([^}]*)\};", src)
    assert m, "ROLE_ALSO_ACTS_AS не найден в helpers.js"
    js = {
        key: tuple(re.findall(r"'([a-z_]+)'", body))
        for key, body in re.findall(r"(\w+)\s*:\s*\[([^\]]*)\]", m.group(1))
    }
    assert js == roles.ROLE_ALSO_ACTS_AS


def test_role_allowed_adds_only_the_delegated_roles():
    assert roles.role_allowed("manager", ("warehouse_keeper",))
    assert roles.role_allowed("manager", ("bookkeeper",))
    assert not roles.role_allowed("manager", ("admin", "boss"))
    # Совмещение одностороннее: кладовщик менеджером не становится.
    assert not roles.role_allowed("warehouse_keeper", ("manager",))
    assert not roles.role_allowed("guest", ("admin", "boss", "manager"))


def test_manager_gains_exactly_keeper_and_bookkeeper_endpoints():
    """Сверка по ВСЕМ ручкам server.py: новое у менеджера — только то, что было
    у кладовщика/бухгалтера. Новая ручка с keeper/book в allowed_roles упадёт
    здесь и заставит решить, должен ли её получить менеджер."""
    from scripts.gen_role_matrix import parse_routes

    rows = parse_routes((ROOT / "webapp" / "server.py").read_text(encoding="utf-8"))
    gained = sorted(
        path for path, allowed in rows
        if "manager" not in allowed and roles.role_allowed("manager", allowed)
    )
    assert gained == [
        "/api/deposits/confirm",
        "/api/deposits/pending",
        "/api/deposits/reject",
        "/api/orders/ship",
        "/api/payments/unlinked",
        "/api/returns/goods_received",
        "/api/returns/pending",
    ]
    boss_only = [p for p, allowed in rows if allowed and set(allowed) <= {"admin", "boss"}]
    assert boss_only, "парсер не нашёл ни одной руководской ручки"
    for path in boss_only:
        allowed = dict(rows)[path]
        assert not roles.role_allowed("manager", allowed), path


def test_paused_roles_are_not_assignable_but_still_valid(isolated_db):
    db = isolated_db
    assert "warehouse_keeper" not in roles.ASSIGNABLE_ROLES
    assert "bookkeeper" not in roles.ASSIGNABLE_ROLES
    assert {"admin", "boss", "manager", "guest"} <= set(roles.ASSIGNABLE_ROLES)
    # Уже назначенные продолжают работать: сама роль в БД жива.
    assert db.set_role(700, "k", "Keeper", "warehouse_keeper") is True
    assert db.get_role(700) == "warehouse_keeper"


# ─── Предикаты и граф переходов ──────────────────────────────────────────────


def test_manager_predicates(isolated_db):
    db = isolated_db
    roles.invalidate_all_roles()
    db.set_role(10, "m", "Manager", "manager")
    assert roles.can_confirm_deposit(10)
    assert roles.can_confirm_shipment(10)
    assert roles.can_mark_return_goods_received(10)
    # Руководское — нет.
    assert not roles.can_confirm_return(10)
    assert not roles.is_boss(10)
    assert not roles.can_manage_payments(10)
    assert not roles.can_change_credit_limit(10)
    # is_* — это «роль ровно такая», совмещение их не трогает.
    assert not roles.is_warehouse_keeper(10) and not roles.is_bookkeeper(10)


def test_manager_transitions():
    from services.order_workflow import can_transition

    assert can_transition({"status": "approved"}, "shipped", "manager")
    assert can_transition({"status": "shipped"}, "paid", "manager")
    assert can_transition({"status": "shipped"}, "returned", "manager")
    assert can_transition({"status": "draft"}, "pending", "manager")
    # Решения руководства — нет.
    assert not can_transition({"status": "pending"}, "approved", "manager")
    assert not can_transition({"status": "approved"}, "cancelled", "manager")


# ─── Уведомления ─────────────────────────────────────────────────────────────


def _u(uid, role, deactivated=None):
    return {"user_id": uid, "role": role, "deactivated_at": deactivated}


def test_notify_recipients_falls_back_to_manager_only_without_holder():
    users = [_u(1, "boss"), _u(2, "manager"), _u(3, "manager", "2026-01-01")]
    # Кладовщика нет — карточку получает и активный менеджер.
    assert roles.notify_recipients(users, ("admin", "boss", "warehouse_keeper")) == [1, 2]
    # Живой кладовщик есть — менеджерам дублировать незачем.
    with_keeper = [*users, _u(4, "warehouse_keeper")]
    assert roles.notify_recipients(with_keeper, ("admin", "boss", "warehouse_keeper")) == [1, 4]
    # Деактивированный кладовщик — не носитель: снова менеджер.
    gone = [*users, _u(4, "warehouse_keeper", "2026-01-01")]
    assert roles.notify_recipients(gone, ("admin", "boss", "warehouse_keeper")) == [1, 2]


def test_deposit_confirmers_include_manager_when_no_bookkeeper(isolated_db):
    db = isolated_db
    db.set_role(1, "b", "Boss", "boss")
    db.set_role(2, "m", "Manager", "manager")
    assert sorted(db.get_deposit_confirmers()) == [1, 2]
    db.set_role(3, "acc", "Book", "bookkeeper")
    assert sorted(db.get_deposit_confirmers()) == [1, 3]


class _Bot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append(chat_id)


class _User:
    def __init__(self, uid):
        self.id = uid
        self.full_name = "U"


class _Msg:
    def __init__(self, text="", uid=1):
        self.text = text
        self.from_user = _User(uid)
        self.answers: list[str] = []
        self.html_text = text

    async def answer(self, text, **kwargs):
        self.answers.append(text)

    async def edit_text(self, text, **kwargs):
        self.answers.append(text)

    async def edit_reply_markup(self, **kwargs):
        return None


class _Call:
    def __init__(self, data, uid):
        self.data = data
        self.from_user = _User(uid)
        self.message = _Msg(uid=uid)
        self.alerts: list[str] = []

    async def answer(self, text="", **kwargs):
        self.alerts.append(text)


def _shipped_order_with_return(db, mgr=2):
    oid = db.create_order(mgr, "Manager", "")
    db.update_order_agent(oid, "A-1", "Клиент")
    db.add_order_item(oid, "Товар", "", 2, "шт", 100.0)
    db.update_order_status(oid, "shipped")
    items = _run(db.get_order_items(oid))
    r = _run(db.create_return(
        oid, "full", "брак", [(items[0]["id"], 2, 200.0)], refund_method="no_refund", created_by=mgr,
    ))
    return r["return_id"]


def test_bot_return_card_goes_to_manager_and_he_marks_goods(isolated_db):
    from handlers.returns import _notify_confirmers, cb_return_confirm, cb_return_goods_received

    db = isolated_db
    roles.invalidate_all_roles()
    db.set_role(1, "b", "Boss", "boss")
    db.set_role(2, "m", "Manager", "manager")
    ret_id = _shipped_order_with_return(db)

    bot = _Bot()
    _run(_notify_confirmers(bot, ret_id, 1, 200.0, "no_refund"))
    assert sorted(bot.sent) == [1, 2]

    got = _Call(f"ret_got:{ret_id}", uid=2)
    _run(cb_return_goods_received(got))
    assert _run(db.get_return(ret_id))["goods_received"]
    # Подтверждает возврат по-прежнему только руководство.
    conf = _Call(f"ret_ok:{ret_id}", uid=2)
    _run(cb_return_confirm(conf, _Bot()))
    assert any("доступа" in a.lower() for a in conf.alerts)


# ─── Бот: назначение ролей и команды ─────────────────────────────────────────


def test_addrole_refuses_paused_roles_but_assigns_manager(isolated_db):
    from handlers.users import cmd_addrole

    db = isolated_db
    roles.invalidate_all_roles()
    db.set_role(1, "adm", "Admin", "admin")

    for paused in ("warehouse_keeper", "bookkeeper"):
        msg = _Msg(f"/addrole 555 {paused}", uid=1)
        _run(cmd_addrole(msg))
        assert "не назначается" in msg.answers[-1], msg.answers
        assert db.get_role(555) == "guest"

    msg = _Msg("/addrole 555 manager", uid=1)
    _run(cmd_addrole(msg))
    assert "назначена роль" in msg.answers[-1], msg.answers
    assert db.get_role(555) == "manager"


def test_manager_commands_include_keeper_commands_once():
    from handlers.start import set_commands_for_user

    class _CmdBot:
        async def set_my_commands(self, commands, scope):
            self.commands = [c.command for c in commands]

    bot = _CmdBot()
    _run(set_commands_for_user(bot, 1, "manager"))
    assert "ship" in bot.commands and "pay" in bot.commands
    assert len(bot.commands) == len(set(bot.commands))

    _run(set_commands_for_user(bot, 1, "warehouse_keeper"))
    assert bot.commands == ["start", "ship", "shipments"]


# ─── «Сегодня» ───────────────────────────────────────────────────────────────


def test_manager_queue_has_deposits_and_returns(isolated_db):
    from services import work_queue

    db = isolated_db
    roles.invalidate_all_roles()
    db.set_role(2, "m", "Manager", "manager")
    _shipped_order_with_return(db)
    assert _run(db.create_cash_deposit(2, 50.0)).get("ok")

    items = {i["key"]: i for i in _run(work_queue.gather(2, "manager"))}
    assert items["deposits"]["screen"] == "money:confirm"
    assert items["returns"]["screen"] == "money:confirm"
    # Руководские пункты менеджеру не приходят.
    assert "requests" not in items and "payments" not in items
