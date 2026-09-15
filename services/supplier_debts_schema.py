"""
Схема «Долги поставщикам»: условия оплаты приходной накладной и детали
исходящего платежа.

Leaf-модуль, как `order_payments_schema`: сервис `services.supplier_debts`
импортирует `database`, и объявление внутри `database._create_tables` дало бы
цикл. Всё — НОВЫЕ таблицы (`CREATE TABLE IF NOT EXISTS`): колонку в уже
стоящую на проде `supplier_payments` без ALTER не добавить, а сама таблица
трогать историю переноса из МойСклад не должна.

Модель (зеркало дебиторки, `services/receivables.py`):

  supplier_invoice_terms  — условия оплаты ПРИХОДНОЙ накладной. Отдельной
                         сущности «долг поставщику» нет, ровно как у клиента:
                         долг клиента — это заказ `payment_type='credit'`, долг
                         поставщику — это приходная накладная с контрагентом.
                         СТРОКИ ПО УМОЛЧАНИЮ НЕТ, и её отсутствие значит «в
                         долг»: иначе четыре сотни исторических накладных из
                         МойСклад пришлось бы размечать задним числом, а долг
                         перед поставщиком по ним реален (его гасят
                         `supplier_payments`, перенесённые тем же прогоном).
                         Строка появляется, когда условия ЗАДАЛИ: «уже
                         оплачено» (в долги не попадает) или срок оплаты.

  supplier_payment_parts — «как и с чего заплатили» по одному исходящему
                         платежу. 1:1 с `supplier_payments` (PK — payment_id),
                         а не 1:N: у выплаты поставщику один способ; несколько
                         способов — это несколько выплат, и каждая видна
                         отдельной строкой в истории.
                         `debt_currency`/`debt_amount_cents` — сумма в валюте
                         ДОЛГА (аналог `payment_parts.order_amount_cents`):
                         приход в USD гасят сумами по курсу, и без пересчёта
                         остаток по накладной было бы не посчитать. У строк,
                         перенесённых из МойСклад, сайдкара нет — там валюта и
                         сумма платежа и есть валюта и сумма погашения.
                         `account_id` — карта/счёт, С КОТОРОГО ушли деньги
                         (тот же справочник `acc_accounts`, что у поступлений);
                         у наличных его нет.
"""

from __future__ import annotations


def tables(id_type: str) -> list[str]:  # noqa: ARG001 — новых SERIAL-таблиц здесь нет
    return [
        """CREATE TABLE IF NOT EXISTS supplier_invoice_terms (
            invoice_id      BIGINT PRIMARY KEY,
            payment_type    TEXT NOT NULL DEFAULT 'credit',
            due_date        TEXT,
            created_by      BIGINT,
            created_by_name TEXT,
            created_at      TEXT NOT NULL,
            updated_at      TEXT
        )""",
        # Курс — ТЕКСТ-десятичная строка «единиц валюты за 1 базовую», как у
        # `payment_parts.rate` и `acc_entries.rate`: REAL на Postgres это
        # float4, и знаки теряются.
        """CREATE TABLE IF NOT EXISTS supplier_payment_parts (
            payment_id        BIGINT PRIMARY KEY,
            method            TEXT NOT NULL,
            account_id        BIGINT,
            debt_currency     TEXT NOT NULL,
            debt_amount_cents BIGINT NOT NULL,
            rate              TEXT,
            rate_source       TEXT NOT NULL,
            cbu_rate          TEXT,
            created_by        BIGINT NOT NULL,
            created_by_name   TEXT,
            created_at        TEXT NOT NULL
        )""",
    ]


INDEXES: list[str] = [
    # «Сколько уже заплатили по этой накладной» — основной запрос экрана.
    # `supplier_payments.invoice_id` без индекса давал бы seq scan по всей
    # истории выплат на каждый пересчёт остатка.
    "CREATE INDEX IF NOT EXISTS idx_supplier_payments_invoice "
    "ON supplier_payments(invoice_id) WHERE invoice_id IS NOT NULL",
    # Правка карты/счёта с деньгами отказывает (`pay_accounts.usage_count`), и
    # «последний выбор» человека выводится из его же выплат.
    "CREATE INDEX IF NOT EXISTS idx_supplier_payment_parts_account "
    "ON supplier_payment_parts(account_id) WHERE account_id IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS idx_supplier_payment_parts_creator "
    "ON supplier_payment_parts(created_by, created_at)",
]
