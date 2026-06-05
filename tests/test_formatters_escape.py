"""
Регресс: пользовательский ввод в HTML-сообщениях экранируется (CLAUDE.md —
никогда не интерполировать full_name/comment в parse_mode=HTML без esc).
"""

from utils.formatters import format_payments_report


def test_payments_report_escapes_user_input():
    summary = [{"full_name": "<b>Boss", "currency": "USD", "total": 100, "count": 1}]
    payments = [
        {
            "full_name": "A & B",
            "amount": 50,
            "currency": "USD",
            "comment": "<script>x</script>",
            "created_at": "2026-06-04 10:00",
        }
    ]
    joined = "\n".join(format_payments_report(summary, payments, "июнь"))
    # Пользовательский ввод заэкранирован…
    assert "&lt;b&gt;Boss" in joined
    assert "A &amp; B" in joined
    assert "&lt;script&gt;x" in joined
    # …и не попал сырым (что сломало бы HTML-парсинг Telegram).
    assert "<b>Boss" not in joined
    assert "<script>" not in joined
