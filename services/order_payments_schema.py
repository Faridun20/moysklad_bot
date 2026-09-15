"""
Схема «как получены деньги»: разбивка оплаты по способам и валютам, валюта
сдачи наличных и привязка сдачи к наличным по заказам.

Leaf-модуль, как `accounting_schema`: сервис `services.order_payments`
импортирует `database`, и объявление внутри `database._create_tables` дало бы
цикл. Всё — НОВЫЕ таблицы (`CREATE TABLE IF NOT EXISTS`): колонку в
существующие `payments`/`cash_deposits` на проде без ALTER не добавить.

Модель:
  payment_parts        — строка разбивки: способ (наличные/карта/перечисление),
                         валюта и сумма, как их отдал клиент, курс и сумма в
                         валюте заказа. Каждой строке соответствует РОВНО ОДИН
                         платёж `payments` (в валюте заказа, сумма =
                         `order_amount_cents`): долг, закрытие заказа и сводки
                         по-прежнему считаются по `payments` одной формулой
                         (`services.debts`), разбивка только объясняет, ЧТО это
                         за деньги. Статус строки не хранится — он выводится из
                         статуса платежа и привязки к сдаче.
  cash_deposit_currency — валюта сдачи наличных. У `cash_deposits` колонки
                         валюты нет: до этой таблицы касса знала только
                         базовую валюту, и наличные сумы сдать было нельзя.
                         Нет строки — сдача в базовой валюте (все старые).
  cash_deposit_parts   — какие наличные строки разбивки закрывает сдача.
                         Сумма — в валюте сдачи (= валюте строки). Сдача
                         берёт строку ЦЕЛИКОМ (часть — через разделение строки,
                         `order_payments.split_part_locked`), поэтому
                         подтверждение сдачи просто подтверждает платежи этих
                         строк. Строки отклонённой сдачи снова «на руках» —
                         привязка действует, пока сдача pending/confirmed.
  payment_part_accounts — КУДА поступили деньги строки «карта»/«на счёт»:
                         ссылка на запись справочника `acc_accounts` (тот же
                         справочник, что у бухгалтерии: включат учёт — поступления
                         уже указывают на свои счета). Отдельная таблица, а не
                         колонка: `payment_parts` уже на проде. Нет строки —
                         наличные или запись до справочника (законно).
  acc_account_details  — реквизиты счёта, которых нет в `acc_accounts`: номер
                         расчётного счёта (20 цифр, хранится целиком — это не
                         номер карты), ИНН и МФО. Номер карты НЕ хранится нигде:
                         только последние 4 цифры в `acc_accounts.card_last4`.
"""

from __future__ import annotations


def tables(id_type: str) -> list[str]:
    return [
        # Курс — ТЕКСТ-десятичная строка «единиц валюты за 1 базовую» (как
        # acc_entries.rate): REAL на Postgres — float4 и теряет знаки. `rate` —
        # курс валюты строки, `order_rate` — валюты заказа; NULL у базовой.
        f"""CREATE TABLE IF NOT EXISTS payment_parts (
            id                 {id_type},
            payment_id         BIGINT NOT NULL,
            order_id           BIGINT NOT NULL,
            method             TEXT NOT NULL,
            currency           TEXT NOT NULL,
            amount_cents       BIGINT NOT NULL,
            rate               TEXT,
            order_rate         TEXT,
            rate_source        TEXT NOT NULL,
            cbu_rate           TEXT,
            order_amount_cents BIGINT NOT NULL,
            split_from         BIGINT,
            created_by         BIGINT NOT NULL,
            created_by_name    TEXT,
            created_at         TEXT NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS cash_deposit_currency (
            deposit_id BIGINT PRIMARY KEY,
            currency   TEXT NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS cash_deposit_parts (
            deposit_id   BIGINT NOT NULL,
            part_id      BIGINT NOT NULL,
            order_id     BIGINT NOT NULL,
            amount_cents BIGINT NOT NULL,
            PRIMARY KEY (deposit_id, part_id)
        )""",
        """CREATE TABLE IF NOT EXISTS payment_part_accounts (
            part_id    BIGINT PRIMARY KEY,
            account_id BIGINT NOT NULL,
            created_at TEXT NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS acc_account_details (
            account_id     BIGINT PRIMARY KEY,
            account_number TEXT,
            company_tin    TEXT,
            mfo            TEXT,
            created_at     TEXT NOT NULL,
            updated_at     TEXT
        )""",
    ]


INDEXES: list[str] = [
    # Один платёж — одна строка разбивки: закрытие заказа и подтверждение
    # платежа опираются на то, что способ у платежа ровно один.
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_payment_parts_payment ON payment_parts(payment_id)",
    "CREATE INDEX IF NOT EXISTS idx_payment_parts_order ON payment_parts(order_id)",
    # «Наличные на руках у менеджера» — фильтр сдачи по автору строки.
    "CREATE INDEX IF NOT EXISTS idx_payment_parts_creator ON payment_parts(created_by, method)",
    "CREATE INDEX IF NOT EXISTS idx_cash_deposit_parts_part ON cash_deposit_parts(part_id)",
    "CREATE INDEX IF NOT EXISTS idx_cash_deposit_parts_order ON cash_deposit_parts(order_id)",
    # «Куда поступили»: платежи по счёту (правка номера у счёта с платежами —
    # отказ) и последний выбранный счёт менеджера.
    "CREATE INDEX IF NOT EXISTS idx_payment_part_accounts_account ON payment_part_accounts(account_id)",
    # Один расчётный счёт — одна запись справочника: второй «такой же» счёт
    # развёл бы поступления одного счёта по двум строкам сверки.
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_acc_account_details_number "
    "ON acc_account_details(account_number) WHERE account_number IS NOT NULL",
]
