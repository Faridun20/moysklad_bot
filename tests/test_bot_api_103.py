"""
Bot API 10.3 (aiogram 3.31) в боте: неактивные кнопки, force_reply у
inline-клавиатуры, кнопки и сворачиваемые цитаты в Rich Message.

Что держит файл:
1. Сборщики клавиатур (`utils.keyboards`) дают ровно тот JSON, который ждёт
   Bot API: `"disabled": {}` без колбэка, `"force_reply": true` рядом с
   `inline_keyboard`. Проверяется сериализацией настоящего метода aiogram, а не
   сравнением с нашим же словарём.
2. Карточки решений (заявка, платёж, сдача, возврат, разморозка): после
   действия кнопки решения превращаются в неактивный исход, соседние строки
   (пачка платежей, список /frozen) остаются живыми.
3. Устаревшая карточка (решили в WebApp / другой руководитель): нажатие
   отвечает алертом И гасит кнопки исходом, а не оставляет их живыми.
4. Ввод причины: вопрос с force_reply и кнопкой отмены; отмена возвращает
   карточке кнопки; кнопка со старого вопроса отвечает «неактуально».
5. Уведомление менеджеру об одобрении: у «оплаты сразу» отгрузка — неактивная
   кнопка с причиной.
6. «Где деньги»: компактная таблица, сворачиваемый список просроченных,
   кнопка в WebApp — и то же в текстовом фолбэке.

Хендлеры зовём напрямую через asyncio.run (pytest-asyncio в проекте нет), БД —
настоящая SQLite (isolated_db), FSM — настоящий FSMContext на MemoryStorage.
Мок стоит только на границе с Telegram (фейковые Bot/Message/CallbackQuery).
"""

from __future__ import annotations

import asyncio

import pytest

import services.roles as roles

MGR, BOSS = 1, 2
HTTPS = "https://webapp.example"


def _run(coro):
    return asyncio.run(coro)


# ─── Граница с Telegram ──────────────────────────────────────────────────────


class _User:
    def __init__(self, uid, full_name="Фаридун Масуджанов"):
        self.id = uid
        self.full_name = full_name
        self.first_name = full_name.split()[0]
        self.username = None


class _Chat:
    id = 55


class _Bot:
    def __init__(self):
        self.sent = []
        self.markup_edits = []

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text, kwargs))
        return _Message(text, bot=self)

    async def edit_message_reply_markup(self, chat_id, message_id, reply_markup=None):
        self.markup_edits.append((chat_id, message_id, reply_markup))


class _Message:
    def __init__(self, text="Карточка", *, uid=BOSS, markup=None, bot=None, message_id=9):
        self.text = text
        self.html_text = text
        self.from_user = _User(uid)
        self.chat = _Chat()
        self.message_id = message_id
        self.bot = bot or _Bot()
        self.reply_markup = markup
        self.answers = []
        self.edited = None

    async def answer(self, text, **kwargs):
        self.answers.append((text, kwargs))
        return _Message(text, uid=self.from_user.id, bot=self.bot, message_id=77)

    async def edit_text(self, text, **kwargs):
        self.edited = text
        self.reply_markup = kwargs.get("reply_markup")

    async def edit_reply_markup(self, reply_markup=None):
        self.reply_markup = reply_markup


class _Call:
    def __init__(self, data, *, uid=BOSS, message=None, bot=None):
        self.data = data
        self.from_user = _User(uid)
        self.message = message or _Message(uid=uid, bot=bot)
        self.alerts = []

    async def answer(self, text="", **kwargs):
        self.alerts.append((text, kwargs))


def _state(uid=BOSS):
    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.storage.base import StorageKey
    from aiogram.fsm.storage.memory import MemoryStorage

    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=55, user_id=uid))


def _buttons(markup):
    return [b for row in (markup.inline_keyboard if markup else []) for b in row]


def _callbacks(markup):
    return [b.callback_data for b in _buttons(markup) if b.callback_data]


def _disabled_texts(markup):
    return [b.text for b in _buttons(markup) if b.disabled is not None]


@pytest.fixture
def db(isolated_db, monkeypatch):
    import config
    import handlers._ui as ui

    roles.invalidate_all_roles()
    isolated_db.set_role(MGR, "mgr", "Manager", "manager")
    isolated_db.set_role(BOSS, "boss", "Boss", "boss")
    monkeypatch.setattr(config, "WEBAPP_URL", HTTPS)
    monkeypatch.setattr(ui, "WEBAPP_URL", HTTPS)
    return isolated_db


# ─── 1. Сборщики клавиатур и JSON Bot API ─────────────────────────────────────


def test_disabled_button_serializes_as_bot_api_10_3():
    from aiogram.methods import SendMessage

    from utils.keyboards import disabled_button, prompt_keyboard, status_keyboard

    markup = status_keyboard("✅ Одобрено · Фаридун 14:05")
    payload = SendMessage(chat_id=1, text="x", reply_markup=markup).model_dump(exclude_none=True)
    assert payload["reply_markup"] == {
        "inline_keyboard": [[{"text": "✅ Одобрено · Фаридун 14:05", "disabled": {}}]]
    }

    from aiogram.types import InlineKeyboardButton

    prompt = prompt_keyboard(InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_abort"))
    payload = SendMessage(chat_id=1, text="x", reply_markup=prompt).model_dump(exclude_none=True)
    assert payload["reply_markup"] == {
        "inline_keyboard": [[{"text": "❌ Отмена", "callback_data": "cancel_abort"}]],
        "force_reply": True,
    }
    # Подпись режется сама — исход длиннее ширины телефона не читается.
    assert len(disabled_button("x" * 200).text) == 48


def test_settle_markup_replaces_only_the_decided_row_and_adds_tail_when_done():
    from aiogram.types import InlineKeyboardButton as B
    from aiogram.types import InlineKeyboardMarkup

    from utils.keyboards import settle_markup

    tail = InlineKeyboardMarkup(inline_keyboard=[[B(text="🌐 WebApp", url="https://x")]])
    batch = InlineKeyboardMarkup(inline_keyboard=[
        [B(text="✅ 10 USD", callback_data="pay_ok:1"), B(text="❌", callback_data="pay_no:1")],
        [B(text="✅ 20 USD", callback_data="pay_ok:2"), B(text="❌", callback_data="pay_no:2")],
    ])
    first = settle_markup(batch, {"pay_ok:1", "pay_no:1"}, "✅ Принято", tail=tail)
    assert _disabled_texts(first) == ["✅ Принято"]
    assert _callbacks(first) == ["pay_ok:2", "pay_no:2"]  # сосед жив
    assert "🌐 WebApp" not in [b.text for b in _buttons(first)]  # решения ещё есть

    second = settle_markup(first, {"pay_ok:2", "pay_no:2"}, "❌ Отклонено", tail=tail)
    assert _disabled_texts(second) == ["✅ Принято", "❌ Отклонено"]
    assert _callbacks(second) == []
    assert [b.text for b in _buttons(second)][-1] == "🌐 WebApp"

    # Нет исходной клавиатуры — исход + хвост с нуля.
    fresh = settle_markup(None, {"x"}, "ℹ️ Уже решено", tail=tail)
    assert [b.text for b in _buttons(fresh)] == ["ℹ️ Уже решено", "🌐 WebApp"]


def test_outcome_label_uses_first_name_and_time():
    from handlers._ui import outcome_label

    label = outcome_label("✅ Одобрено", _User(BOSS, "Фаридун Масуджанов"))
    assert label.startswith("✅ Одобрено · Фаридун ")
    assert "Масуджанов" not in label
    assert label[-5:-2].endswith(":")  # «14:05»


def test_text_prompts_force_reply():
    from handlers.order_cancel import _abort_keyboard
    from handlers.payments import _pay_cancel_keyboard

    for markup, cb in ((_abort_keyboard(), "cancel_abort"), (_pay_cancel_keyboard(), "pay_cancel")):
        assert markup.force_reply is True
        assert _callbacks(markup) == [cb]


# ─── 2–4. Заявка на отгрузку ─────────────────────────────────────────────────


def _pending_request(db, payment_type="credit"):
    oid = db.create_order(MGR, "Manager", "")
    db.update_order_agent(oid, "A-1", "Клиент")
    db.add_order_item(oid, "Товар", "", 1, "шт", 100.0)
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(db.q("UPDATE orders SET payment_type=?, due_date=? WHERE id=?"),
                    (payment_type, "2030-01-01" if payment_type == "credit" else None, oid))
        conn.commit()
    db.update_order_status(oid, "pending")
    return oid, db.create_shipment_request(oid, MGR, "Manager")


def test_approve_turns_card_buttons_into_disabled_outcome(db):
    from handlers.orders import cb_approve_request, request_approve_keyboard

    _, req_id = _pending_request(db)
    bot = _Bot()
    call = _Call(f"req_ok:{req_id}", message=_Message(markup=request_approve_keyboard(req_id), bot=bot))
    _run(cb_approve_request(call, bot))

    markup = call.message.reply_markup
    assert db.get_shipment_request(req_id)["status"] == "approved"
    assert _disabled_texts(markup)[0].startswith("✅ Одобрено · Фаридун ")
    assert not any(cb.startswith("req_") for cb in _callbacks(markup))
    assert any(b.web_app for b in _buttons(markup))  # «что дальше» — WebApp
    assert "Одобрено" in call.message.edited  # пометка текстом осталась


def test_manager_is_told_shipping_waits_for_payment_on_paid_order(db):
    from handlers.orders import cb_approve_request, request_approve_keyboard

    _, req_id = _pending_request(db, payment_type="paid")
    bot = _Bot()
    call = _Call(f"req_ok:{req_id}", message=_Message(markup=request_approve_keyboard(req_id), bot=bot))
    _run(cb_approve_request(call, bot))

    to_manager = [(t, kw) for chat, t, kw in bot.sent if chat == MGR]
    assert to_manager, bot.sent
    text, kwargs = to_manager[0]
    assert "Сначала внесите оплату" in text
    markup = kwargs["reply_markup"]
    assert _disabled_texts(markup) == ["🚚 Отгрузка — после ввода оплаты"]
    assert [b.text for b in _buttons(markup) if b.web_app] == ["💳 Внести оплату — в WebApp"]


def test_approved_order_keyboard_variants(monkeypatch):
    import config
    from services.notify import approved_order_keyboard

    monkeypatch.setattr(config, "WEBAPP_URL", HTTPS)
    credit = approved_order_keyboard("credit")
    assert [b.text for b in _buttons(credit)] == ["🚚 Отгрузить — в WebApp"]
    assert _disabled_texts(credit) == []

    # Без https web_app-кнопку Telegram отвергает: у «оплаты сразу» остаётся
    # подсказка, у «в долг» — ничего.
    monkeypatch.setattr(config, "WEBAPP_URL", "")
    assert _disabled_texts(approved_order_keyboard("paid")) == ["🚚 Отгрузка — после ввода оплаты"]
    assert approved_order_keyboard("credit") is None


def test_stale_request_card_is_settled_not_left_alive(db):
    """Заявку одобрили в WebApp, а на карточке в чате кнопки живые. Нажатие —
    алерт и неактивный исход вместо кнопок."""
    from handlers.orders import cb_reject_request, request_approve_keyboard
    from services.order_workflow import approve_shipment_request

    _, req_id = _pending_request(db)
    assert _run(approve_shipment_request(req_id, BOSS, "Boss", None))["ok"]

    bot = _Bot()
    call = _Call(f"req_no:{req_id}", message=_Message(markup=request_approve_keyboard(req_id), bot=bot))
    _run(cb_reject_request(call, bot))

    assert call.alerts and call.alerts[0][1].get("show_alert")
    assert _disabled_texts(call.message.reply_markup) == ["✅ Заявка уже одобрена"]
    assert not any(cb.startswith("req_") for cb in _callbacks(call.message.reply_markup))
    assert db.get_shipment_request(req_id)["status"] == "approved"


def test_return_to_draft_prompt_force_reply_and_abort_restores_card(db):
    from handlers.orders import (
        ReturnToDraft,
        cb_return_to_draft,
        cb_return_to_draft_abort,
        request_approve_keyboard,
    )

    _, req_id = _pending_request(db)
    bot = _Bot()
    state = _state()
    card = _Message(markup=request_approve_keyboard(req_id), bot=bot)
    _run(cb_return_to_draft(_Call(f"req_draft:{req_id}", message=card, bot=bot), state))

    assert _run(state.get_state()) == ReturnToDraft.waiting_for_reason.state
    assert _disabled_texts(card.reply_markup) == ["✍️ Ждём причину доработки…"]
    _, prompt_kw = card.answers[-1]
    assert prompt_kw["reply_markup"].force_reply is True
    assert _callbacks(prompt_kw["reply_markup"]) == ["req_draft_abort"]

    prompt = _Message("вопрос", bot=bot, message_id=77)
    _run(cb_return_to_draft_abort(_Call("req_draft_abort", message=prompt, bot=bot), state, bot))
    assert _run(state.get_state()) is None
    chat, msg_id, restored = bot.markup_edits[-1]
    assert (chat, msg_id) == (55, 9)
    assert _callbacks(restored) == _callbacks(request_approve_keyboard(req_id))
    assert db.get_shipment_request(req_id)["status"] == "pending"

    # Та же кнопка ещё раз (вопрос устарел) — «неактуально», карточку не трогаем.
    edits = len(bot.markup_edits)
    again = _Call("req_draft_abort", message=prompt, bot=bot)
    _run(cb_return_to_draft_abort(again, state, bot))
    assert again.alerts[0][0] == "Уже неактуально"
    assert bot.markup_edits[edits:] == [(55, 77, None)]


def test_return_to_draft_reason_stamps_card_with_disabled_outcome(db):
    from handlers.orders import cb_return_to_draft, process_return_to_draft_reason, request_approve_keyboard

    _, req_id = _pending_request(db)
    bot = _Bot()
    state = _state()
    card = _Message(markup=request_approve_keyboard(req_id), bot=bot)
    _run(cb_return_to_draft(_Call(f"req_draft:{req_id}", message=card, bot=bot), state))
    _run(process_return_to_draft_reason(_Message("нет цены на позицию", bot=bot), state, bot))

    assert db.get_shipment_request(req_id)["status"] == "returned"
    card_edits = [m for chat, mid, m in bot.markup_edits if mid == 9]
    assert _disabled_texts(card_edits[-1])[0].startswith("↩️ На доработку · Фаридун ")
    assert (55, 77, None) in bot.markup_edits  # «Не возвращать» под вопросом погашена


def test_return_to_draft_on_stale_card_does_not_start_input(db):
    from handlers.orders import cb_return_to_draft, request_approve_keyboard
    from services.order_workflow import reject_shipment_request

    _, req_id = _pending_request(db)
    assert _run(reject_shipment_request(req_id, BOSS, "Boss", None))["ok"]
    state = _state()
    card = _Message(markup=request_approve_keyboard(req_id))
    call = _Call(f"req_draft:{req_id}", message=card)
    _run(cb_return_to_draft(call, state))
    assert _run(state.get_state()) is None
    assert card.answers == []  # причину не спрашиваем
    assert _disabled_texts(card.reply_markup) == ["❌ Заявка уже отклонена"]


# ─── Платежи ──────────────────────────────────────────────────────────────────


def _batch_markup(pids):
    from aiogram.types import InlineKeyboardButton as B
    from aiogram.types import InlineKeyboardMarkup

    return InlineKeyboardMarkup(inline_keyboard=[
        [B(text=f"✅ #{p}", callback_data=f"pay_ok:{p}"), B(text="❌", callback_data=f"pay_no:{p}")]
        for p in pids
    ])


def test_payment_batch_card_settles_only_decided_payment(db):
    from handlers.payments import confirm_pay

    p1 = db.add_payment(MGR, "@mgr", "Manager", 100.0, "USD", "аренда")
    p2 = db.add_payment(MGR, "@mgr", "Manager", 250.0, "USD", "аренда")
    bot = _Bot()
    call = _Call(f"pay_ok:{p1}", message=_Message(markup=_batch_markup([p1, p2]), bot=bot))
    _run(confirm_pay(call, bot))

    markup = call.message.reply_markup
    assert _run(db.get_payment(p1))["status"] == "confirmed"
    assert _callbacks(markup) == [f"pay_ok:{p2}", f"pay_no:{p2}"]
    [label] = _disabled_texts(markup)
    assert label.startswith("✅ Принято 100 USD · Фаридун ")  # в пачке — с суммой


def test_stale_payment_card_is_settled(db):
    from handlers.payments import confirm_keyboard, reject_pay

    pid = db.add_payment(MGR, "@mgr", "Manager", 100.0, "USD", "")
    assert _run(db.confirm_payment(pid, BOSS, "Boss"))
    bot = _Bot()
    call = _Call(f"pay_no:{pid}", message=_Message(markup=confirm_keyboard(pid), bot=bot))
    _run(reject_pay(call, bot))
    assert _run(db.get_payment(pid))["status"] == "confirmed"
    assert _disabled_texts(call.message.reply_markup) == ["✅ Платёж уже принят"]
    assert _callbacks(call.message.reply_markup) == ["menu"]  # решений не осталось


def test_cash_payment_accept_button_becomes_disabled_with_reason(db):
    from handlers.payments import confirm_keyboard, confirm_pay
    from services import order_payments

    oid = db.create_order(MGR, "Manager", "")
    db.update_order_agent(oid, "A-1", "Клиент")
    db.add_order_item(oid, "Товар", "", 1, "шт", 100.0)
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(db.q("UPDATE orders SET payment_type='credit', due_date='2030-01-01' WHERE id=?"), (oid,))
        conn.commit()
    db.update_order_status(oid, "shipped")
    actor = order_payments.Actor(user_id=MGR, name="Manager", role="manager")
    rec = _run(order_payments.record_payment_parts(
        oid, actor, [{"method": "cash", "amount": "100", "currency": "USD"}]
    ))
    pid = rec["parts"][0]["payment_id"]

    bot = _Bot()
    call = _Call(f"pay_ok:{pid}", message=_Message(markup=confirm_keyboard(pid), bot=bot))
    _run(confirm_pay(call, bot))
    assert _run(db.get_payment(pid))["status"] == "pending"
    assert _disabled_texts(call.message.reply_markup) == ["💵 Принять — через сдачу в кассу"]
    assert _callbacks(call.message.reply_markup) == [f"pay_no:{pid}"]  # отклонить можно


def test_pay_prompt_cancel_button_settles_after_payment_and_stale_cancel_is_honest(db):
    from handlers.payments import cmd_pay, pay_cancel, process_input

    bot = _Bot()
    state = _state(MGR)
    cmd = _Message("/pay", uid=MGR, bot=bot)
    _run(cmd_pay(cmd, state))
    assert cmd.answers[-1][1]["reply_markup"].force_reply is True

    _run(process_input(_Message("1500 USD за аренду", uid=MGR, bot=bot), state, bot))
    chat, msg_id, markup = bot.markup_edits[-1]
    assert msg_id == 77
    assert _disabled_texts(markup)[0].startswith("✅ Платёж #")

    stale = _Call("pay_cancel", uid=MGR, message=_Message("вопрос", uid=MGR, bot=bot))
    _run(pay_cancel(stale, state))
    assert stale.alerts[0][0] == "Уже неактуально"
    assert stale.message.edited is None  # «отправка отменена» не пишем


# ─── Сдачи, возвраты, разморозка, отмена ─────────────────────────────────────


def _deposit(db):
    oid = db.create_order(MGR, "Manager", "")
    db.update_order_agent(oid, "A-1", "Клиент")
    db.add_order_item(oid, "Товар", "", 1, "шт", 250.0)
    db.update_order_status(oid, "shipped")
    return _run(db.create_cash_deposit(MGR, 250.0))["deposit_id"]


def test_deposit_confirm_outcome_and_stale_card(db):
    from handlers.deposits import _confirm_keyboard, cb_deposit_confirm

    dep_id = _deposit(db)
    bot = _Bot()
    call = _Call(f"dep_ok:{dep_id}", message=_Message(markup=_confirm_keyboard(dep_id), bot=bot))
    _run(cb_deposit_confirm(call, bot))
    assert _disabled_texts(call.message.reply_markup)[0].startswith("✅ Подтверждено · Фаридун ")
    assert _callbacks(call.message.reply_markup) == ["menu"]

    # Вторая копия карточки (у другого подтверждающего) — уже решено.
    other = _Call(f"dep_ok:{dep_id}", message=_Message(markup=_confirm_keyboard(dep_id), bot=bot))
    _run(cb_deposit_confirm(other, bot))
    assert _disabled_texts(other.message.reply_markup) == ["✅ Сдача уже подтверждена"]


def test_deposit_reject_abort_restores_card(db):
    from handlers.deposits import (
        _confirm_keyboard,
        cb_deposit_reject,
        cb_deposit_reject_abort,
    )

    dep_id = _deposit(db)
    bot = _Bot()
    state = _state()
    card = _Message(markup=_confirm_keyboard(dep_id), bot=bot)
    _run(cb_deposit_reject(_Call(f"dep_no:{dep_id}", message=card, bot=bot), state))
    assert _disabled_texts(card.reply_markup) == ["✍️ Ждём причину отклонения…"]
    assert card.answers[-1][1]["reply_markup"].force_reply is True

    prompt = _Message("вопрос", bot=bot, message_id=77)
    _run(cb_deposit_reject_abort(_Call("dep_no_abort", message=prompt, bot=bot), state, bot))
    assert _run(state.get_state()) is None
    assert _callbacks(bot.markup_edits[-1][2]) == [f"dep_ok:{dep_id}", f"dep_no:{dep_id}"]
    assert _run(db.get_cash_deposit(dep_id))["status"] == "pending"


def _return(db):
    oid = db.create_order(MGR, "Manager", "")
    db.update_order_agent(oid, "A-1", "Клиент")
    db.add_order_item(oid, "Товар", "", 2, "шт", 50.0)
    db.update_order_status(oid, "shipped")
    items = _run(db.get_order_items(oid))
    res = _run(db.create_return(
        oid, "partial", "брак", [(items[0]["id"], 1, 50.0)], refund_method="no_refund", created_by=BOSS
    ))
    return res["return_id"]


def test_return_goods_received_shows_who_and_keeps_confirm(db):
    from handlers.returns import _confirm_keyboard, cb_return_confirm, cb_return_goods_received

    ret_id = _return(db)
    bot = _Bot()
    card = _Message(markup=_confirm_keyboard(ret_id), bot=bot)
    _run(cb_return_goods_received(_Call(f"ret_got:{ret_id}", message=card, bot=bot)))
    assert _callbacks(card.reply_markup) == [f"ret_ok:{ret_id}"]
    assert _disabled_texts(card.reply_markup)[0].startswith("📦 Товар получен · Фаридун ")

    _run(cb_return_confirm(_Call(f"ret_ok:{ret_id}", message=card, bot=bot), bot))
    texts = _disabled_texts(card.reply_markup)
    assert texts[0].startswith("✅ Возврат подтверждён · Фаридун ")
    assert texts[1].startswith("📦 Товар получен")
    assert not any(cb.startswith("ret_") for cb in _callbacks(card.reply_markup))

    stale = _Message(markup=_confirm_keyboard(ret_id), bot=bot)
    _run(cb_return_confirm(_Call(f"ret_ok:{ret_id}", message=stale, bot=bot), bot))
    assert _disabled_texts(stale.reply_markup) == ["✅ Возврат уже подтверждён"]


def test_unfreeze_disables_only_that_order_button(db):
    from aiogram.types import InlineKeyboardButton as B
    from aiogram.types import InlineKeyboardMarkup

    from handlers.orders import cb_unfreeze_order

    db.set_role(BOSS, "boss", "Boss", "admin")
    roles.invalidate_all_roles()
    oid1 = db.create_order(MGR, "Manager", "")
    oid2 = db.create_order(MGR, "Manager", "")
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(db.q("UPDATE orders SET frozen=1, rejection_count=3 WHERE id IN (?, ?)"), (oid1, oid2))
        conn.commit()
    listing = InlineKeyboardMarkup(inline_keyboard=[
        [B(text=f"🔓 Разморозить #{o}", callback_data=f"unfreeze:{o}")] for o in (oid1, oid2)
    ])
    bot = _Bot()
    call = _Call(f"unfreeze:{oid1}", message=_Message(markup=listing, bot=bot))
    _run(cb_unfreeze_order(call, bot))
    assert _callbacks(call.message.reply_markup) == [f"unfreeze:{oid2}"]
    assert _disabled_texts(call.message.reply_markup)[0].startswith(f"🔓 #{oid1} разморожен · ")


def test_stale_cancel_abort_does_not_claim_abort(db):
    from handlers.order_cancel import cb_cancel_abort

    call = _Call("cancel_abort", message=_Message("вопрос"))
    _run(cb_cancel_abort(call, _state()))
    assert call.alerts[0][0] == "Уже неактуально"
    assert call.message.edited is None


# ─── 6. «Где деньги» ─────────────────────────────────────────────────────────


def _overdue_order(db, name="ООО <Строй>", amount=700.0):
    oid = db.create_order(MGR, "Manager", "")
    db.update_order_agent(oid, f"A-{name}", name)
    db.add_order_item(oid, "Товар", "", 1, "шт", amount)
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(db.q("UPDATE orders SET payment_type='credit', due_date='2020-01-01' WHERE id=?"), (oid,))
        conn.commit()
    db.update_order_status(oid, "shipped")
    return oid


def test_money_report_rich_blocks_use_10_3_features(db):
    from aiogram.enums import InputRichBlockType
    from aiogram.methods import SendRichMessage
    from aiogram.types import InputRichMessage

    from services import money_report

    _overdue_order(db)
    data = _run(money_report.gather())
    assert data["overdue"]["total"] == 1 and data["overdue"]["rows"][0]["days"] > 365

    blocks = money_report.build_blocks(data)
    by_type = {b.type: b for b in blocks}
    assert by_type[InputRichBlockType.TABLE].is_compact is True
    quote = by_type[InputRichBlockType.EXPANDABLE_BLOCKQUOTE]
    assert "ООО <Строй> — 700 USD" in quote.text  # Rich — не HTML, экранировать нечего
    [button] = by_type[InputRichBlockType.BUTTONS].buttons
    assert button.web_app.url == HTTPS

    # Сериализуется настоящим методом aiogram — Bot API примет форму блоков.
    method = SendRichMessage(chat_id=1, rich_message=InputRichMessage(blocks=blocks))
    payload = method.rich_message.model_dump(exclude_none=True, mode="json")
    types = [b["type"] for b in payload["blocks"]]
    assert "expandable_blockquote" in types and types[-1] == "buttons"


def test_money_report_without_https_webapp_has_no_button(db, monkeypatch):
    import config
    from aiogram.enums import InputRichBlockType

    from services import money_report

    monkeypatch.setattr(config, "WEBAPP_URL", "http://localhost:8080")
    _overdue_order(db)
    data = _run(money_report.gather())
    assert InputRichBlockType.BUTTONS not in [b.type for b in money_report.build_blocks(data)]
    assert money_report.webapp_reply_markup() is None


def test_money_report_text_fallback_carries_list_and_button(db, monkeypatch):
    """Фолбэк несёт те же разделы: сворачиваемый список (HTML
    `<blockquote expandable>`, имена экранированы) и кнопку в WebApp."""
    from services import money_report

    _overdue_order(db)

    class _FailingBot:
        async def send_rich_message(self, **kw):
            raise RuntimeError("Bad Request: rich message is not supported")

    async def _get():
        return _FailingBot()

    import services.notifier as notifier
    import webapp.server as server

    monkeypatch.setattr(server, "get_notify_bot", _get)
    sent = []

    async def _send(chat_id, text, **kw):
        sent.append((chat_id, text, kw))
        return True

    monkeypatch.setattr(notifier, "tg_send_message", _send)

    data = _run(money_report.gather())
    assert _run(money_report.send_report(BOSS, data)) == "text"
    [(chat, text, kw)] = sent
    assert "<blockquote expandable>ООО &lt;Строй&gt; — 700 USD" in text
    assert kw["reply_markup"] == {
        "inline_keyboard": [[{"text": money_report.WEBAPP_BUTTON_TEXT, "web_app": {"url": HTTPS}}]]
    }


def test_overdue_list_is_capped(db, monkeypatch):
    from services import money_report

    monkeypatch.setattr(money_report, "OVERDUE_LIST_MAX", 2)
    for i in range(4):
        _overdue_order(db, name=f"Клиент {i}", amount=100.0 + i)
    data = _run(money_report.gather())
    lines = money_report._overdue_lines(data)
    assert len(lines) == 3 and lines[-1] == "…и ещё 2"


def test_ship_command_refusal_shows_blocked_step_disabled(db):
    """/ship по «оплате сразу» без разбивки: отказ, живая кнопка оплаты и
    неактивная отгрузка с причиной."""
    from handlers.order_ship import cmd_ship

    oid, _ = _pending_request(db, payment_type="paid")
    db.update_order_status(oid, "approved")
    msg = _Message(f"/ship {oid}")
    _run(cmd_ship(msg, _Bot()))
    text, kwargs = msg.answers[-1]
    assert text.startswith("⚠️")
    markup = kwargs["reply_markup"]
    assert _disabled_texts(markup) == ["🚚 Отгрузка — после ввода оплаты"]
    assert [b.text for b in _buttons(markup) if b.web_app] == ["💳 Внести оплату в WebApp"]
    assert _run(db.get_order(oid))["status"] == "approved"
