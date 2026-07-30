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


# ─── Заведение и список ───────────────────────────────────────────────────────


def test_new_machine_one_liner_creates_and_shows_card(isolated_db):
    db = isolated_db
    from handlers.machines import cmd_new_machine

    _setup(db)
    msg = _FakeMessage(text="/newmachine JCB-3CX-9911 JCB 3CX 2019", uid=1)
    asyncio.run(cmd_new_machine(msg))
    assert "заведена" in _texts(msg)
    assert "JCB3CX9911" in _texts(msg)  # VIN нормализован


def test_new_machine_requires_name(isolated_db):
    db = isolated_db
    from handlers.machines import cmd_new_machine

    _setup(db)
    msg = _FakeMessage(text="/newmachine JCB-1", uid=1)
    asyncio.run(cmd_new_machine(msg))
    assert "Формат" in _texts(msg)


def test_new_machine_denied_for_guest(isolated_db):
    db = isolated_db
    from handlers.machines import cmd_new_machine

    _setup(db)
    msg = _FakeMessage(text="/newmachine X Y", uid=99)  # роли нет
    asyncio.run(cmd_new_machine(msg))
    assert "доступа" in _texts(msg).lower()


def test_list_shows_machines_with_buttons(isolated_db):
    db = isolated_db
    from handlers.machines import cmd_machines

    _setup(db)
    mid = _machine(vin="SANY-1")
    msg = _FakeMessage(text="/machines", uid=1)
    asyncio.run(cmd_machines(msg))
    _text, kwargs = msg.answers[-1]
    assert f"mach:{mid}" in _kb_callbacks(kwargs.get("reply_markup"))


def test_empty_list_explains_how_to_add(isolated_db):
    db = isolated_db
    from handlers.machines import cmd_machines

    _setup(db)
    msg = _FakeMessage(text="/machines", uid=1)
    asyncio.run(cmd_machines(msg))
    assert "/newmachine" in _texts(msg)


# ─── Карточка и роли ──────────────────────────────────────────────────────────


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


def test_card_offers_status_buttons_only_to_boss(isolated_db):
    db = isolated_db
    from handlers.machines import cb_machine_card

    _setup(db)
    mid = _machine()

    mgr = _FakeCall(f"mach:{mid}", uid=1)
    asyncio.run(cb_machine_card(mgr))
    mgr_cbs = _kb_callbacks(mgr.message.answers[-1][1].get("reply_markup"))
    assert not any(c.startswith("mach_st:") for c in mgr_cbs)
    assert f"mach_hours:{mid}" in mgr_cbs  # моточасы менеджеру доступны

    boss = _FakeCall(f"mach:{mid}", uid=2)
    asyncio.run(cb_machine_card(boss))
    boss_cbs = _kb_callbacks(boss.message.answers[-1][1].get("reply_markup"))
    assert f"mach_st:{mid}:in_stock" in boss_cbs


def test_status_change_denied_for_manager(isolated_db):
    db = isolated_db
    from handlers.machines import cb_set_status
    from services import machines

    _setup(db)
    mid = _machine()
    call = _FakeCall(f"mach_st:{mid}:in_stock", uid=1)
    asyncio.run(cb_set_status(call))
    assert any("руководител" in (a[0] or "").lower() for a in call.alerts)
    assert asyncio.run(machines.get_machine(mid, role="boss"))["status"] == "in_transit"


def test_boss_status_change_applies(isolated_db):
    db = isolated_db
    from handlers.machines import cb_set_status
    from services import machines

    _setup(db)
    mid = _machine()
    call = _FakeCall(f"mach_st:{mid}:in_stock", uid=2)
    asyncio.run(cb_set_status(call))
    assert asyncio.run(machines.get_machine(mid, role="boss"))["status"] == "in_stock"


# ─── Моточасы ─────────────────────────────────────────────────────────────────


def test_hours_flow_drops_keyboard_and_records(isolated_db):
    """T3.2: пока вводится число, кнопки карточки не должны работать — второй
    тап по другой машине переписал бы machine_id в state."""
    db = isolated_db
    from handlers.machines import cb_add_hours, process_hours
    from services import machines

    _setup(db)
    mid = _machine(hours=1000)
    state = _FakeState()
    call = _FakeCall(f"mach_hours:{mid}", uid=1)
    asyncio.run(cb_add_hours(call, state))
    assert any(a[0] == "MARKUP" and a[1] is None for a in call.message.answers)

    asyncio.run(process_hours(_FakeMessage(text="1250", uid=1), state))
    assert asyncio.run(machines.get_machine(mid, role="boss"))["hours"] == 1250


def test_hours_rejects_non_number(isolated_db):
    db = isolated_db
    from handlers.machines import process_hours

    _setup(db)
    mid = _machine()
    state = _FakeState()
    asyncio.run(state.update_data(machine_id=mid))
    msg = _FakeMessage(text="много", uid=1)
    asyncio.run(process_hours(msg, state))
    assert "целое число" in _texts(msg)


def test_hours_rollback_offers_force_to_boss_only(isolated_db):
    """Откат счётчика — либо опечатка, либо замена счётчика. Менеджеру просто
    отказ, боссу — кнопка подтверждения (решение уходит в аудит)."""
    db = isolated_db
    from handlers.machines import cb_force_hours, process_hours
    from services import machines

    _setup(db)
    mid = _machine(hours=15000)

    mgr_state = _FakeState()
    asyncio.run(mgr_state.update_data(machine_id=mid))
    mgr_msg = _FakeMessage(text="1500", uid=1)
    asyncio.run(process_hours(mgr_msg, mgr_state))
    assert "меньше предыдущего" in _texts(mgr_msg)
    assert mgr_msg.answers[-1][1].get("reply_markup") is None  # кнопки force нет

    boss_state = _FakeState()
    asyncio.run(boss_state.update_data(machine_id=mid))
    boss_msg = _FakeMessage(text="1500", uid=2)
    asyncio.run(process_hours(boss_msg, boss_state))
    cbs = _kb_callbacks(boss_msg.answers[-1][1].get("reply_markup"))
    assert f"mach_hours_f:{mid}:1500" in cbs

    call = _FakeCall(f"mach_hours_f:{mid}:1500", uid=2)
    asyncio.run(cb_force_hours(call))
    assert asyncio.run(machines.get_machine(mid, role="boss"))["hours"] == 1500


# ─── Фото ─────────────────────────────────────────────────────────────────────


def test_photos_accepted_in_batch_and_deduped(isolated_db):
    """Альбом приходит отдельными сообщениями, поэтому состояние держим до
    /done; повторная отправка того же снимка не создаёт дубль."""
    db = isolated_db
    from handlers.machines import cb_add_photo, cmd_photo_done, process_photo
    from services import machines

    _setup(db)
    mid = _machine()
    state = _FakeState()
    asyncio.run(cb_add_photo(_FakeCall(f"mach_photo:{mid}", uid=1), state))

    first = _FakeMessage(uid=1, photo=[_FakePhotoSize("f1", "u1")])
    asyncio.run(process_photo(first, state))
    second = _FakeMessage(uid=1, photo=[_FakePhotoSize("f2", "u2")])
    asyncio.run(process_photo(second, state))
    dup = _FakeMessage(uid=1, photo=[_FakePhotoSize("f3", "u1")])
    asyncio.run(process_photo(dup, state))
    assert "уже в карточке" in _texts(dup)
    assert len(asyncio.run(machines.list_photos(mid))) == 2

    done = _FakeMessage(text="/done", uid=1)
    asyncio.run(cmd_photo_done(done, state))
    assert "Фото: 2" in _texts(done)


def test_photo_takes_largest_size(isolated_db):
    """Telegram отдаёт лестницу размеров — сохранять надо самый крупный."""
    db = isolated_db
    from handlers.machines import process_photo
    from services import machines

    _setup(db)
    mid = _machine()
    state = _FakeState()
    asyncio.run(state.update_data(machine_id=mid))
    sizes = [_FakePhotoSize("small", "u-small"), _FakePhotoSize("large", "u-large")]
    asyncio.run(process_photo(_FakeMessage(uid=1, photo=sizes), state))
    photos = asyncio.run(machines.list_photos(mid))
    assert photos[0]["tg_file_id"] == "large"


def test_non_photo_in_photo_flow_is_explained(isolated_db):
    db = isolated_db
    from handlers.machines import process_photo_wrong_type

    _setup(db)
    msg = _FakeMessage(text="вот файл", uid=1)
    asyncio.run(process_photo_wrong_type(msg))
    assert "фотографию" in _texts(msg)


# ─── Сделки ───────────────────────────────────────────────────────────────────


def test_sell_one_liner_creates_deal(isolated_db):
    db = isolated_db
    from handlers.machines import cmd_sell
    from services import machines

    _setup(db)
    mid = _machine()
    msg = _FakeMessage(text=f"/sell {mid} 25000 Иванов Пётр", uid=2)
    asyncio.run(cmd_sell(msg))
    assert "оформлена" in _texts(msg)
    deals = asyncio.run(machines.list_deals(mid))
    assert len(deals) == 1
    assert deals[0]["price_cents"] == 25_000_00
    assert deals[0]["buyer_name"] == "Иванов Пётр"


def test_sell_denied_for_manager(isolated_db):
    db = isolated_db
    from handlers.machines import cmd_sell
    from services import machines

    _setup(db)
    mid = _machine()
    msg = _FakeMessage(text=f"/sell {mid} 25000 Иванов", uid=1)
    asyncio.run(cmd_sell(msg))
    assert "руководител" in _texts(msg).lower()
    assert asyncio.run(machines.list_deals(mid)) == []


def test_sell_rejects_bad_price(isolated_db):
    db = isolated_db
    from handlers.machines import cmd_sell

    _setup(db)
    mid = _machine()
    msg = _FakeMessage(text=f"/sell {mid} бесплатно Иванов", uid=2)
    asyncio.run(cmd_sell(msg))
    assert "Цена" in _texts(msg)


def test_credit_requires_due_date_format(isolated_db):
    db = isolated_db
    from handlers.machines import cmd_credit

    _setup(db)
    mid = _machine()
    msg = _FakeMessage(text=f"/credit {mid} 25000 31.12.2026 Иванов", uid=2)
    asyncio.run(cmd_credit(msg))
    assert "ГГГГ-ММ-ДД" in _texts(msg)


def test_credit_creates_deal_and_closes_from_list(isolated_db):
    db = isolated_db
    from handlers.machines import cb_close_deal, cmd_credit, cmd_open_credits
    from services import machines

    _setup(db)
    mid = _machine()
    asyncio.run(cmd_credit(_FakeMessage(text=f"/credit {mid} 25000 2026-12-31 Иванов", uid=2)))
    assert asyncio.run(machines.get_machine(mid, role="boss"))["status"] == "on_credit"

    listing = _FakeMessage(text="/machine_deals", uid=2)
    asyncio.run(cmd_open_credits(listing, bot=None))
    deal_id = asyncio.run(machines.list_deals(mid))[0]["id"]
    assert f"mach_deal_close:{deal_id}" in _kb_callbacks(listing.answers[-1][1].get("reply_markup"))

    asyncio.run(cb_close_deal(_FakeCall(f"mach_deal_close:{deal_id}", uid=2)))
    assert asyncio.run(machines.get_machine(mid, role="boss"))["status"] == "sold"
    assert asyncio.run(machines.get_open_credit_deals()) == []
