"""
Схема бухгалтерии (этап 1): счета, журнал денежных операций, закрытия дня.

Отдельный leaf-модуль, а не строки внутри `database._create_tables`: сервис
`services.accounting` импортирует `database`, и обратный импорт на уровне
модуля дал бы цикл. `database` зовёт отсюда две функции — таблицы и индексы.

Всё — НОВЫЕ таблицы (`CREATE TABLE IF NOT EXISTS`): колонку в существующую
таблицу на проде без ALTER не добавить, а новая таблица доезжает обычным
стартом. Поэтому связь с платежом/поступлением по рассрочке живёт в строке
документа журнала (`acc_docs.payment_id`/`machine_receipt_id`), а `payments`
и `machine_payment_receipts` не трогаются вовсе.

Модель:
  acc_accounts — справочник «куда пришли / откуда ушли деньги»: касса,
                 банковский счёт, карта. Валюта у счёта одна: наличные доллары
                 и наличные сумы — два счёта, иначе остаток кассы пришлось бы
                 складывать из разных валют.
  acc_docs     — документ журнала: одно действие человека (получил деньги,
                 расход, перевод, обмен, сверка, начальный остаток). Статус,
                 отмена с причиной и ключ идемпотентности — здесь, а не в
                 строках: отменяется действие целиком.
  acc_entries  — движения по счетам внутри документа (приход/расход, сумма в
                 валюте счёта, курс к базовой с источником, сумма в базовой).
  acc_day_closes — «Закрыть день»: пересчёт кассы. Пишется ВСЕГДА, даже когда
                 сошлось, — «день закрыт, всё сошлось» тоже факт; расхождение
                 дополнительно проводится документом сверки.

Физического удаления нет: ошибочная запись отменяется (status='void') с
причиной, и остатки считаются только по проведённым документам.
"""

from __future__ import annotations


def tables(id_type: str) -> list[str]:
    """DDL таблиц. `id_type` — SERIAL PRIMARY KEY / INTEGER PRIMARY KEY AUTOINCREMENT."""
    return [
        f"""CREATE TABLE IF NOT EXISTS acc_accounts (
            id             {id_type},
            name           TEXT NOT NULL,
            kind           TEXT NOT NULL,
            currency       TEXT NOT NULL,
            bank           TEXT,
            card_last4     TEXT,
            holder         TEXT,
            note           TEXT,
            archived_at    TEXT,
            created_by     BIGINT,
            created_at     TEXT NOT NULL,
            updated_at     TEXT
        )""",
        # Курс хранится ТЕКСТОМ-десятичной строкой («12700.5»): человек вводит
        # его точно, а REAL на Postgres (float4) потерял бы знаки. Семантика —
        # «сколько единиц валюты строки за 1 единицу базовой» (сум за доллар):
        # так курс называют вслух и так его пишет ЦБ.
        f"""CREATE TABLE IF NOT EXISTS acc_docs (
            id               {id_type},
            kind             TEXT NOT NULL,
            doc_date         TEXT NOT NULL,
            request_key      TEXT,
            order_id         BIGINT,
            deal_id          BIGINT,
            payment_id       BIGINT,
            machine_receipt_id BIGINT,
            counterparty     TEXT,
            target_currency  TEXT,
            target_cents     BIGINT,
            category         TEXT,
            note             TEXT,
            status           TEXT NOT NULL DEFAULT 'posted',
            void_reason      TEXT,
            voided_by        BIGINT,
            voided_by_name   TEXT,
            voided_at        TEXT,
            created_by       BIGINT NOT NULL,
            created_by_name  TEXT,
            created_at       TEXT NOT NULL
        )""",
        f"""CREATE TABLE IF NOT EXISTS acc_entries (
            id                {id_type},
            doc_id            BIGINT NOT NULL,
            account_id        BIGINT NOT NULL,
            direction         TEXT NOT NULL,
            amount_cents      BIGINT NOT NULL,
            currency          TEXT NOT NULL,
            rate              TEXT NOT NULL,
            rate_source       TEXT NOT NULL,
            cbu_rate          TEXT,
            amount_base_cents BIGINT NOT NULL,
            target_cents      BIGINT
        )""",
        f"""CREATE TABLE IF NOT EXISTS acc_day_closes (
            id             {id_type},
            account_id     BIGINT NOT NULL,
            close_date     TEXT NOT NULL,
            expected_cents BIGINT NOT NULL,
            counted_cents  BIGINT NOT NULL,
            diff_cents     BIGINT NOT NULL,
            doc_id         BIGINT,
            note           TEXT,
            created_by     BIGINT NOT NULL,
            created_by_name TEXT,
            created_at     TEXT NOT NULL
        )""",
    ]


INDEXES: list[str] = [
    # Идемпотентность записи: один ключ — один документ. Партиальный — записи
    # без ключа (начальный остаток из справочника) не конфликтуют.
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_acc_docs_request_key "
    "ON acc_docs(request_key) WHERE request_key IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS idx_acc_docs_date ON acc_docs(doc_date)",
    "CREATE INDEX IF NOT EXISTS idx_acc_docs_order ON acc_docs(order_id)",
    "CREATE INDEX IF NOT EXISTS idx_acc_entries_doc ON acc_entries(doc_id)",
    "CREATE INDEX IF NOT EXISTS idx_acc_entries_account ON acc_entries(account_id)",
    "CREATE INDEX IF NOT EXISTS idx_acc_day_closes_account ON acc_day_closes(account_id, close_date)",
]
