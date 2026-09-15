"""
Схема ежедневной сверки кассы: сколько наличных пересчитали руками.

Leaf-модуль, как `accounting_schema` и `order_payments_schema`: сервис
`services.cash_reconciliation` импортирует `database`, и объявление внутри
`database._create_tables` дало бы цикл. Таблица НОВАЯ (`CREATE TABLE IF NOT
EXISTS`) — доезжает обычным стартом, без ALTER.

**Почему не `acc_day_closes`.** У бухгалтерии уже есть пересчёт кассы, и по
смыслу это то же действие. Но её строка стоит на двух чужих ключах:
`account_id` → `acc_accounts` (счёт кассы, который заводят в бухгалтерии) и
`doc_id` → `acc_docs` (документ сверки, которым проводится расхождение), а
«сколько должно быть» там считается как остаток по журналу
(`accounting.account_balance` по `acc_entries`). При выключенном
`accounting_enabled` журнал пуст, счетов нет, и «ожидаемое» вышло бы нулём для
любой кассы. Включить «только эту таблицу» значит завести счета и писать
документы — то есть включить весь модуль, который владелец сознательно держит
выключенным до переноса истории. Поэтому здесь своя таблица, ни на что не
ссылающаяся, а «сколько должно быть» берётся из ЖИВОГО источника —
`order_payments.cash_on_hand` (наличные строки разбивки, которые менеджер ещё
не сдал). Когда учёт включат, две сверки не подерутся: у них разные источники
ожидаемого и разные таблицы, а эта останется журналом физических пересчётов.

Модель:
  daily_cash_counts — ОДНА строка на валюту в одном пересчёте: наличные держат
                      и в долларах, и в сумах, а складывать их в одну сумму
                      нечем (курс на момент пересчёта — это уже переоценка, а
                      не сверка). Строки одного пересчёта склеивает
                      `request_key` (он же ключ идемпатентности: UNIQUE по
                      паре с валютой — двойной тап не пишет второй пересчёт).

Пересчёт пишется ВСЕГДА, в том числе когда всё сошлось («записан пересчёт,
расхождений нет» — требование владельца): факт ежедневной сверки и есть то,
ради чего всё затевалось. Денег таблица не двигает: ни платежа, ни сдачи, ни
долга по ней не создаётся — это наблюдение, а не операция.
"""

from __future__ import annotations


def tables(id_type: str) -> list[str]:
    """DDL таблиц. `id_type` — SERIAL PRIMARY KEY / INTEGER PRIMARY KEY AUTOINCREMENT."""
    return [
        # `system_cents` — снимок расчётного остатка НА МОМЕНТ пересчёта, а не
        # ссылка на выборку: строки разбивки потом сдают в кассу, и через день
        # тот же запрос вернул бы другое число — расхождение «исправилось» бы
        # само. `diff_cents` хранится рядом, хотя выводится: по нему идёт
        # выборка «расхождения за период» для руководителя, а CHECK
        # (`scripts/apply_constraints`) держит его равным разнице — соврать
        # им нельзя.
        f"""CREATE TABLE IF NOT EXISTS daily_cash_counts (
            id              {id_type},
            count_date      TEXT NOT NULL,
            counted_by      BIGINT NOT NULL,
            counted_by_name TEXT,
            currency        TEXT NOT NULL,
            counted_cents   BIGINT NOT NULL,
            system_cents    BIGINT NOT NULL,
            diff_cents      BIGINT NOT NULL,
            note            TEXT,
            request_key     TEXT,
            created_at      TEXT NOT NULL
        )""",
    ]


INDEXES: list[str] = [
    # Идемпотентность и группировка: один пересчёт — одна строка на валюту.
    # Партиальный (ключа может не быть у записи из скрипта/теста), по ПАРЕ с
    # валютой — строки одного пересчёта делят ключ намеренно.
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_daily_cash_counts_key "
    "ON daily_cash_counts(request_key, currency) WHERE request_key IS NOT NULL",
    # «Сверял ли этот человек сегодня» — напоминание в очереди дел и своя история.
    "CREATE INDEX IF NOT EXISTS idx_daily_cash_counts_user "
    "ON daily_cash_counts(counted_by, count_date)",
    # Общая история и расхождения за период (руководителю) — уже в нужном порядке.
    "CREATE INDEX IF NOT EXISTS idx_daily_cash_counts_date "
    "ON daily_cash_counts(count_date, id)",
]
