"""
Тесты state machine заказа — services.order_workflow.

Чистая логика без БД: матрица допустимых переходов (TRANSITIONS) и прав
ролей (_ROLE_TRANSITIONS). Эти правила раньше были размазаны по
handlers/orders.py + webapp/server.py; тест фиксирует их в одном месте,
чтобы добавление нового статуса/правила не сломало старые молча.
"""

from services.order_workflow import can_transition, validate_transition


def _order(status: str) -> dict:
    return {"status": status}


# ─── validate_transition: только допустимость перехода (без ролей) ──────────


def test_validate_allows_known_transitions():
    assert validate_transition(_order("draft"), "pending") is None
    assert validate_transition(_order("pending"), "approved") is None
    assert validate_transition(_order("pending"), "rejected") is None
    assert validate_transition(_order("approved"), "shipped") is None


def test_validate_rejects_impossible_transitions():
    # Назад по цепочке нельзя
    assert validate_transition(_order("shipped"), "draft") is not None
    assert validate_transition(_order("approved"), "pending") is not None
    # Терминальные статусы — некуда идти
    assert validate_transition(_order("rejected"), "approved") is not None
    assert validate_transition(_order("shipped"), "approved") is not None


def test_validate_unknown_status_is_rejected():
    assert validate_transition(_order("nonsense"), "pending") is not None
    assert validate_transition(_order("draft"), "nonsense") is not None


# ─── can_transition: допустимость + права роли ──────────────────────────────


def test_manager_can_only_submit_draft():
    assert can_transition(_order("draft"), "pending", "manager") is True
    # Менеджер не апрувит и не отгружает
    assert can_transition(_order("pending"), "approved", "manager") is False
    assert can_transition(_order("approved"), "shipped", "manager") is False


def test_boss_can_approve_and_reject_pending():
    assert can_transition(_order("pending"), "approved", "boss") is True
    assert can_transition(_order("pending"), "rejected", "boss") is True
    # Но не инициирует отправку черновика
    assert can_transition(_order("draft"), "pending", "boss") is False


def test_admin_has_full_rights():
    assert can_transition(_order("draft"), "pending", "admin") is True
    assert can_transition(_order("pending"), "approved", "admin") is True
    assert can_transition(_order("pending"), "rejected", "admin") is True
    assert can_transition(_order("approved"), "shipped", "admin") is True
    assert can_transition(_order("draft"), "rejected", "admin") is True


def test_guest_can_do_nothing():
    assert can_transition(_order("draft"), "pending", "guest") is False
    assert can_transition(_order("pending"), "approved", "guest") is False


def test_unknown_role_can_do_nothing():
    assert can_transition(_order("pending"), "approved", "intern") is False


def test_can_transition_blocks_impossible_even_for_admin():
    # Даже у админа нет права на несуществующий переход — TRANSITIONS
    # проверяется раньше прав роли.
    assert can_transition(_order("shipped"), "draft", "admin") is False
    assert can_transition(_order("approved"), "pending", "admin") is False
