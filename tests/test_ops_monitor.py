"""
Тесты операционного монитора (tasks/run_ops_monitor). Проверяем чистую логику
билдеров дайджестов и сборку — без сети и БД (это и есть зона риска: что и
кому уходит). Граница с Telegram мокается на уровне HTTP в e2e-тестах
notifier'а; здесь — формирование текста.
"""

from tasks.run_ops_monitor import (
    assemble_digest,
    build_expiring_batches_block,
    build_overdue_undeposited_block,
    build_pending_deposits_block,
    build_pending_returns_block,
    build_stale_orders_block,
)


def test_blocks_return_none_when_empty():
    assert build_stale_orders_block([], 48) is None
    assert build_pending_deposits_block([]) is None
    assert build_pending_returns_block([]) is None
    assert build_overdue_undeposited_block([], 2) is None
    assert build_expiring_batches_block([], 7) is None


def test_stale_block_counts_and_truncates():
    orders = [{"id": i, "agent_name": f"A{i}", "full_name": "Mgr"} for i in range(20)]
    block = build_stale_orders_block(orders, 48)
    assert "20" in block
    assert "и ещё 5" in block  # показываем 15, остальные свёрнуты


def test_deposits_block_sums_amount():
    deposits = [{"id": 1, "amount": 100.0}, {"id": 2, "amount": 250.0}]
    block = build_pending_deposits_block(deposits)
    assert "350" in block
    assert "#1" in block and "#2" in block


def test_returns_block_shows_order():
    returns = [{"id": 5, "order_id": 42, "total_amount": 200.0}]
    block = build_pending_returns_block(returns)
    assert "#5" in block and "#42" in block and "200" in block


def test_assemble_skips_empty_blocks():
    digest = assemble_digest("T", [None, "БЛОК-A", None, "БЛОК-B"])
    assert "БЛОК-A" in digest and "БЛОК-B" in digest
    assert assemble_digest("T", [None, None]) is None


def test_assemble_none_when_all_empty():
    assert assemble_digest("Заголовок", []) is None
