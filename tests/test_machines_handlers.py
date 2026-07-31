"""
T4.2 — бот-хендлеры техники (handlers/machines).

Проверяем то, что видит пользователь: карточка не показывает менеджеру
себестоимость, откат моточасов требует подтверждения босса, фотографии
принимаются пачкой и не дублируются, сделка оформляется одной строкой, а роли
разграничены (менеджер не меняет статус и не продаёт).

Хендлеры зовём напрямую с фейковыми объектами (pytest-asyncio в проекте нет),
БД настоящая (isolated_db).
"""

import asyncio

import services.roles as roles


class _FakeUser:
    def __init__(self, uid, full_name="User"):
        self.id = uid
        self.full_name = full_name


class _FakeChat:
    id = 55


class _FakePhotoSize:
    def __init__(self, file_id="AgACfile", file_unique_id="AQADuniq"):
        self.file_id = file_id
        self.file_unique_id = file_unique_id


class _FakeMessage:
    def __init__(self, text="", uid=1, photo=None, caption=None):
        self.text = text
        self.caption = caption
        self.photo = photo or []
        self.from_user = _FakeUser(uid)
        self.chat = _FakeChat()
        self.message_id = 9
        self.answers = []

    async def answer(self, text, **kwargs):
        self.answers.append((text, kwargs))
        return _FakeMessage(text=text, uid=self.from_user.id)

    async def edit_text(self, text, **kwargs):
        self.answers.append(("EDIT:" + text, kwargs))

    async def edit_reply_markup(self, reply_markup=None):
        self.answers.append(("MARKUP", reply_markup))


class _FakeCall:
    def __init__(self, data, uid):
        self.data = data
        self.from_user = _FakeUser(uid)
        self.message = _FakeMessage(uid=uid)
        self.alerts = []

    async def answer(self, text="", **kwargs):
        self.alerts.append((text, kwargs))


class _FakeState:
    def __init__(self):
        self._data = {}
        self._state = None

    async def clear(self):
        self._data = {}
        self._state = None

    async def set_state(self, st):
        self._state = st

    async def update_data(self, **kw):
        self._data.update(kw)

    async def get_data(self):
        return dict(self._data)


def _texts(obj):
    return " ".join(t for t, _ in obj.answers)


def _kb_callbacks(markup):
    if markup is None:
        return []
    return [b.callback_data for row in markup.inline_keyboard for b in row if b.callback_data]


def _setup(db):
    roles.invalidate_all_roles()
    db.set_role(1, "mgr", "Manager", "manager")
    db.set_role(2, "boss", "Boss", "boss")


def _machine(uid=1, vin="JCB-1", **over):
    from services import machines

    payload = {"vin": vin, "name": "JCB 3CX", "created_by": uid}
    payload.update(over)
    res = asyncio.run(machines.create_machine(**payload))
    assert res["ok"], res
    return res["machine_id"]


# ─── Список и карточка ────────────────────────────────────────────────────────


def test_list_shows_machines_with_buttons(isolated_db):
    db = isolated_db
    from handlers.machines import cmd_machines

    _setup(db)
    mid = _machine(vin="SANY-1")
    msg = _FakeMessage(text="/machines", uid=1)
    asyncio.run(cmd_machines(msg))
    _text, kwargs = msg.answers[-1]
    assert f"mach:{mid}" in _kb_callbacks(kwargs.get("reply_markup"))


def test_empty_list_points_to_the_webapp(isolated_db):
    """Заведение машины переехало — подсказка должна вести туда, где оно есть."""
    db = isolated_db
    from handlers.machines import cmd_machines

    _setup(db)
    msg = _FakeMessage(text="/machines", uid=1)
    asyncio.run(cmd_machines(msg))
    assert "Техника" in _texts(msg)
    assert "/newmachine" not in _texts(msg)


def test_card_hides_cost_from_manager(isolated_db):
    """Себестоимость — только руководству; срез делает сервис, карточка просто
    не находит поле."""
    db = isolated_db
    from handlers.machines import cb_machine_card

    _setup(db)
    mid = _machine(price_cents=30_000_00, cost_cents=21_000_00)

    mgr = _FakeCall(f"mach:{mid}", uid=1)
    asyncio.run(cb_machine_card(mgr))
    assert "Себестоимость" not in _texts(mgr.message)
    assert "Цена" in _texts(mgr.message)

    boss = _FakeCall(f"mach:{mid}", uid=2)
    asyncio.run(cb_machine_card(boss))
    assert "Себестоимость" in _texts(boss.message)


def test_card_no_longer_offers_actions_moved_to_webapp(isolated_db):
    """Кнопка, за которой в боте больше нет обработчика, обещает операцию,
    которой нет. Остаться должен только возврат к списку."""
    db = isolated_db
    from handlers.machines import cb_machine_card

    _setup(db)
    mid = _machine()
    boss = _FakeCall(f"mach:{mid}", uid=2)
    asyncio.run(cb_machine_card(boss))
    cbs = _kb_callbacks(boss.message.answers[-1][1].get("reply_markup"))
    assert cbs == ["mach_list"]


# ─── Моточасы ─────────────────────────────────────────────────────────────────


def test_hours_one_liner_records(isolated_db):
    """Показание снимают с площадки: два числа одной строкой, без диалога."""
    db = isolated_db
    from handlers.machines import cmd_hours
    from services import machines

    _setup(db)
    mid = _machine(hours=1000)
    msg = _FakeMessage(text=f"/hours {mid} 1250", uid=1)
    asyncio.run(cmd_hours(msg))
    assert asyncio.run(machines.get_machine(mid, role="boss"))["hours"] == 1250
    assert "Моточасы" in _texts(msg)


def test_hours_explains_the_format(isolated_db):
    db = isolated_db
    from handlers.machines import cmd_hours

    _setup(db)
    mid = _machine()
    for text in (f"/hours {mid}", "/hours много 100", f"/hours {mid} много"):
        msg = _FakeMessage(text=text, uid=1)
        asyncio.run(cmd_hours(msg))
        assert "Формат" in _texts(msg), text


def test_hours_denied_for_guest(isolated_db):
    db = isolated_db
    from handlers.machines import cmd_hours

    _setup(db)
    mid = _machine()
    msg = _FakeMessage(text=f"/hours {mid} 100", uid=99)  # роли нет
    asyncio.run(cmd_hours(msg))
    assert "доступа" in _texts(msg).lower()


def test_hours_rollback_offers_force_to_boss_only(isolated_db):
    """Откат счётчика — либо опечатка, либо замена счётчика. Менеджеру просто
    отказ, боссу — кнопка подтверждения (решение уходит в аудит)."""
    db = isolated_db
    from handlers.machines import cb_force_hours, cmd_hours
    from services import machines

    _setup(db)
    mid = _machine(hours=15000)

    mgr_msg = _FakeMessage(text=f"/hours {mid} 1500", uid=1)
    asyncio.run(cmd_hours(mgr_msg))
    assert "меньше предыдущего" in _texts(mgr_msg)
    assert mgr_msg.answers[-1][1].get("reply_markup") is None  # кнопки force нет

    boss_msg = _FakeMessage(text=f"/hours {mid} 1500", uid=2)
    asyncio.run(cmd_hours(boss_msg))
    cbs = _kb_callbacks(boss_msg.answers[-1][1].get("reply_markup"))
    assert f"mach_hours_f:{mid}:1500" in cbs

    call = _FakeCall(f"mach_hours_f:{mid}:1500", uid=2)
    asyncio.run(cb_force_hours(call))
    assert asyncio.run(machines.get_machine(mid, role="boss"))["hours"] == 1500


# ─── Рассрочки ────────────────────────────────────────────────────────────────


def test_open_credits_listed_without_close_buttons(isolated_db):
    """Закрытие рассрочки переехало в WebApp — кнопки без обработчика тут быть
    не должно, но список кому напоминать боссу по-прежнему нужен в чате."""
    db = isolated_db
    from handlers.machines import cmd_open_credits
    from services import machines

    _setup(db)
    mid = _machine()
    deal = asyncio.run(
        machines.create_deal(
            mid, kind="credit", price_cents=25_000_00, buyer_name="Иванов",
            due_date="2026-12-31", created_by=2,
        )
    )
    assert deal["ok"], deal

    listing = _FakeMessage(text="/machine_deals", uid=2)
    asyncio.run(cmd_open_credits(listing, bot=None))
    text, kwargs = listing.answers[-1]
    assert "Иванов" in text
    assert not any(c.startswith("mach_deal_close") for c in _kb_callbacks(kwargs.get("reply_markup")))


def test_open_credits_denied_for_manager(isolated_db):
    db = isolated_db
    from handlers.machines import cmd_open_credits

    _setup(db)
    msg = _FakeMessage(text="/machine_deals", uid=1)
    asyncio.run(cmd_open_credits(msg, bot=None))
    assert "доступа" in _texts(msg).lower()


def test_open_credits_hides_passport(isolated_db):
    """Паспорт покупателя в чат не печатаем — он и в сервисе режется по роли."""
    db = isolated_db
    from handlers.machines import cmd_open_credits
    from services import machines

    _setup(db)
    mid = _machine()
    asyncio.run(
        machines.create_deal(
            mid, kind="credit", price_cents=1000, buyer_name="Иванов",
            buyer_passport="AB1234567", due_date="2026-12-31", created_by=2,
        )
    )
    msg = _FakeMessage(text="/machine_deals", uid=2)
    asyncio.run(cmd_open_credits(msg, bot=None))
    assert "AB1234567" not in _texts(msg)


# ─── Снятые команды ───────────────────────────────────────────────────────────


def test_retired_machine_commands_point_to_the_webapp():
    """Набравший `/sell` по памяти должен понять, что бот не сломался."""
    from handlers.start import _RETIRED_COMMANDS

    for command in ("newmachine", "sell", "credit"):
        assert "Техника" in _RETIRED_COMMANDS[command], command
