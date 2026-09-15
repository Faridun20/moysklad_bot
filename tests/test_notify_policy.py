"""
services.notify_policy — единая точка решения «боссу СРАЗУ или в вечерний
дайджест». Реальная БД (isolated_db) для app_settings/currency_rates.
"""

from services import notify_policy as policy


def test_order_request_is_always_immediate(isolated_db):
    assert policy.should_notify_now(policy.ORDER_REQUEST) is True
    # Сумма/валюта не при делах — заявка блокирует работу независимо от денег.
    assert policy.should_notify_now(policy.ORDER_REQUEST, 1.0, "USD") is True


def test_machine_deal_approval_is_always_immediate(isolated_db):
    assert policy.should_notify_now(policy.MACHINE_DEAL_APPROVAL) is True


def test_unknown_kind_defaults_to_immediate(isolated_db):
    """Лучше лишний пуш, чем тихо проглоченное решение по неизвестному виду."""
    assert policy.should_notify_now("something_new", 1.0, "USD") is True


def test_usd_payment_below_default_threshold_goes_to_digest(isolated_db):
    assert policy.should_notify_now(policy.PAYMENT, 4999.99, "USD") is False


def test_usd_payment_at_or_above_default_threshold_is_immediate(isolated_db):
    assert policy.should_notify_now(policy.PAYMENT, 5000.0, "USD") is True
    assert policy.should_notify_now(policy.PAYMENT, 10_000.0, "USD") is True


def test_threshold_is_configurable(isolated_db):
    isolated_db.set_setting("boss_instant_threshold_usd", 100.0)
    assert policy.should_notify_now(policy.CASH_DEPOSIT, 150.0, "USD") is True
    assert policy.should_notify_now(policy.CASH_DEPOSIT, 50.0, "USD") is False
    assert policy.instant_threshold_usd() == 100.0


def test_uzs_amount_converted_by_current_rate(isolated_db):
    """1 USD = 12 500 UZS → rate_to_base(UZS) = 1/12500. 60 000 000 UZS ≈
    4800 USD (ниже дефолтного порога 5000), 65 000 000 UZS ≈ 5200 (выше)."""
    ok, err = isolated_db.set_currency_rate("UZS", 1 / 12_500, updated_by=1)
    assert ok, err

    assert policy.usd_equivalent(60_000_000, "UZS") == 4800.0
    assert policy.should_notify_now(policy.RETURN, 60_000_000, "UZS") is False
    assert policy.should_notify_now(policy.RETURN, 65_000_000, "UZS") is True


def test_missing_rate_falls_back_to_immediate(isolated_db):
    """Валюта без курса в currency_rates — конвертировать не во что, и
    списать в дайджест значит рискнуть эту сумму не заметить."""
    import services.database as db

    db._invalidate_currency_rates_cache()
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(db.q("DELETE FROM currency_rates WHERE currency_code = 'UZS'"))
        conn.commit()
    db._invalidate_currency_rates_cache()

    assert policy.usd_equivalent(1.0, "UZS") is None
    assert policy.should_notify_now(policy.PAYMENT, 1.0, "UZS") is True


def test_missing_amount_falls_back_to_immediate(isolated_db):
    assert policy.should_notify_now(policy.PAYMENT, None, "USD") is True


def test_none_currency_defaults_to_usd(isolated_db):
    """Возвраты в проекте сейчас всегда в USD (handlers/returns.py) — currency=None
    должно вести себя как «USD», а не как «неизвестная валюта»."""
    assert policy.usd_equivalent(100.0, None) == 100.0
    assert policy.should_notify_now(policy.RETURN, 100.0, None) is False
    assert policy.should_notify_now(policy.RETURN, 6000.0, None) is True
