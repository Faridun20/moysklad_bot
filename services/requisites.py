"""Реквизиты для печатных документов: наша компания и покупатель.

**Компания** (`app_settings`, ключи `company_*` и `invoice_valid_days`) —
одна форма «Настройки → Реквизиты компании», сгруппированная так, как её
читают документы: Компания / Банк / Подписи / Счёт на оплату / Расписка.
Жалоба владельца: реквизиты было не найти (строка жила только у руководства,
а он работает менеджером), и форма спрашивала «должность подписанта», которой
нет ни в одном документе физлица. Каждое поле здесь — с человеческой подписью
и примером заполнения; порядок полей — порядок формы.

**Кто правит**: руководство (admin/boss) — всегда; менеджер — пока активного
руководителя в системе нет (правило `no_boss`, `machine_deal_requests.decision_rights`).
Проверяет ручка, а не форма.

**Чего не хватает — говорим поимённо.** Документ, которому нужен реквизит,
не падает общим «ошибка», а отвечает `RequisitesMissing` с текстом «Заполните
ИНН в Настройки → Реквизиты компании»: менеджер сам знает, куда идти.
Обязательны только те поля, без которых документ теряет смысл
(`REQUIRED_BY_DOC`): счёт на оплату без банковских реквизитов оплатить нельзя,
расписка без ИНН и адреса кредитора не опознаёт его. Товарная накладная не
блокируется ничем: она сопровождает уже проведённую отгрузку, пустой
реквизит печатается чертой для записи от руки.

**Покупатель** — sidecar `counterparty_requisites` (ИНН/ПИНФЛ, адрес): у
`counterparties` таких колонок нет, а таблица уже на проде (менять
её определение в рабочем коде запрещено). Телефон — прежний `counterparties.phone`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

WHERE = "Настройки → Реквизиты компании"

# Срок действия счёта на оплату по умолчанию (решение владельца) и границы.
INVOICE_VALID_DAYS_DEFAULT = 3
INVOICE_VALID_DAYS_MAX = 90


@dataclass(frozen=True)
class Field:
    key: str
    group: str
    label: str
    placeholder: str
    hint: str = ""
    # Как поле называется в тексте «Заполните …» (в винительном падеже).
    short: str = ""


GROUPS: tuple[tuple[str, str], ...] = (
    ("company", "Компания"),
    ("bank", "Банк"),
    ("signatures", "Подписи"),
    ("invoice", "Счёт на оплату"),
    ("receipt", "Расписка"),
)

COMPANY_FIELDS: tuple[Field, ...] = (
    Field("company_name", "company", "Полное наименование", "ООО «FARID IMPEKS»",
          "Шапка счёта, накладной и расписки", "полное наименование"),
    Field("company_tin", "company", "ИНН", "301234567", "9 цифр", "ИНН"),
    Field("company_oked", "company", "ОКЭД", "46690", "Код вида деятельности, 5 цифр", "ОКЭД"),
    Field("company_address", "company", "Юридический адрес",
          "г. Ташкент, Юнусабадский р-н, ул. Амира Темура, 107Б", "", "юридический адрес"),
    Field("company_phone", "company", "Телефон", "+998 71 200-00-00", "", "телефон"),
    Field("company_bank_account", "bank", "Расчётный счёт", "20208000900123456001",
          "20 цифр", "расчётный счёт"),
    Field("company_bank_name", "bank", "Банк", "АКБ «Капиталбанк», г. Ташкент", "", "банк"),
    Field("company_bank_mfo", "bank", "МФО банка", "01088", "5 цифр", "МФО"),
    Field("company_director", "signatures", "Руководитель — Ф.И.О.", "Иванов Иван Иванович",
          "Под подписью в счёте. Пусто — черта, впишут от руки", "Ф.И.О. руководителя"),
    Field("company_chief_accountant", "signatures", "Главный бухгалтер — Ф.И.О.",
          "Петрова Анна Сергеевна", "Нет бухгалтера — оставьте пустым", "Ф.И.О. главного бухгалтера"),
    Field("company_release_by", "signatures", "Отпуск разрешил — должность, Ф.И.О.",
          "Директор Иванов И. И.", "В товарной накладной. Пусто — подставится руководитель",
          "кто разрешает отпуск"),
    Field("invoice_valid_days", "invoice", "Срок оплаты счёта, банковских дней", "3",
          "«Счёт действителен для оплаты в течение N банковских дней»", "срок оплаты счёта"),
    Field("company_city", "receipt", "Город", "Ташкент", "Где составлена расписка", "город"),
    Field("company_city_uz", "receipt", "Город по-узбекски", "Тошкент", "Для тилхата", "город по-узбекски"),
)

FIELDS_BY_KEY = {f.key: f for f in COMPANY_FIELDS}

# Документ → поля, без которых он не собирается, и как он называется в отказе.
REQUIRED_BY_DOC: dict[str, tuple[str, ...]] = {
    "sales_invoice": (
        "company_name", "company_tin", "company_address",
        "company_bank_account", "company_bank_name", "company_bank_mfo",
    ),
    "raspiska": ("company_tin", "company_address"),
}
DOC_NAMES = {"sales_invoice": "счёт на оплату", "raspiska": "расписку"}


class RequisitesMissing(Exception):
    """Не заполнены реквизиты, без которых документ не собрать. Текст — человеку."""

    def __init__(self, fields: list[Field], doc: str):
        self.fields = fields
        self.doc = doc
        self.keys = [f.key for f in fields]
        self.message = missing_message(fields, doc)
        super().__init__(self.message)


def _join_ru(items: list[str]) -> str:
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " и " + items[-1]


def missing_message(fields: list[Field], doc: str) -> str:
    """«Заполните ИНН и МФО в Настройки → Реквизиты компании — без этого не выписать счёт на оплату»."""
    names = _join_ru([f.short or f.label for f in fields])
    what = DOC_NAMES.get(doc, "документ")
    return f"Заполните {names} в {WHERE} — без этого не выписать {what}"


# ─── Компания ────────────────────────────────────────────────────────────────


def company_requisites() -> dict[str, str]:
    """Все реквизиты строками (пусто — не заполнено). Название — с запасным
    значением проекта (`invoice_pdf.COMPANY_NAME`): одно на проект, и требовать
    вписать его ради каждого документа незачем."""
    from services.database import get_setting

    out: dict[str, str] = {}
    for f in COMPANY_FIELDS:
        val = get_setting(f.key, "")
        out[f.key] = "" if val is None else str(val).strip()
    if not out["company_name"]:
        from services.invoice_pdf import COMPANY_NAME

        out["company_name"] = COMPANY_NAME
    if not out["invoice_valid_days"]:
        out["invoice_valid_days"] = str(INVOICE_VALID_DAYS_DEFAULT)
    return out


def invoice_valid_days(company: dict[str, Any]) -> int:
    """Срок действия счёта из реквизитов; мусор — значение по умолчанию."""
    try:
        days = int(str(company.get("invoice_valid_days") or "").strip())
    except ValueError:
        return INVOICE_VALID_DAYS_DEFAULT
    return days if 1 <= days <= INVOICE_VALID_DAYS_MAX else INVOICE_VALID_DAYS_DEFAULT


def missing_for(company: dict[str, Any], doc: str) -> list[Field]:
    return [FIELDS_BY_KEY[k] for k in REQUIRED_BY_DOC[doc] if not str(company.get(k) or "").strip()]


def require(company: dict[str, Any], doc: str) -> None:
    """Бросить `RequisitesMissing`, если документу чего-то не хватает."""
    missing = missing_for(company, doc)
    if missing:
        raise RequisitesMissing(missing, doc)


class RequisitesInvalid(ValueError):
    """Значение поля не подходит (например, срок счёта — не число)."""


def save_company_requisites(values: dict[str, Any], by: int) -> dict[str, Any]:
    """Записать присланные поля (неизвестные ключи — молча мимо). Возвращает
    записанное. Срок счёта проверяется ДО записи: половина сохранённой формы
    хуже отказа целиком."""
    from services.database import set_setting

    clean: dict[str, Any] = {}
    for f in COMPANY_FIELDS:
        if f.key not in values:
            continue
        raw = str(values.get(f.key) or "").strip()
        if f.key == "invoice_valid_days":
            if not raw:
                clean[f.key] = INVOICE_VALID_DAYS_DEFAULT
                continue
            try:
                days = int(raw)
            except ValueError:
                raise RequisitesInvalid("Срок оплаты счёта — целое число дней, например 3") from None
            if not 1 <= days <= INVOICE_VALID_DAYS_MAX:
                raise RequisitesInvalid(
                    f"Срок оплаты счёта — от 1 до {INVOICE_VALID_DAYS_MAX} банковских дней"
                )
            clean[f.key] = days
        else:
            clean[f.key] = raw[:300]
    for key, value in clean.items():
        set_setting(key, value, by)
    return clean


# ─── Покупатель ──────────────────────────────────────────────────────────────


def _digits(raw: Any) -> str:
    return "".join(ch for ch in str(raw or "") if ch.isdigit())


def clean_tin(raw: Any) -> str:
    """ИНН (9 цифр) или ПИНФЛ (14 цифр); пусто — законно. Иначе `RequisitesInvalid`."""
    text = str(raw or "").strip()
    if not text:
        return ""
    digits = _digits(text)
    if len(digits) not in (9, 14) or len(digits) != len(text.replace(" ", "")):
        raise RequisitesInvalid("ИНН — 9 цифр, ПИНФЛ — 14 цифр")
    return digits


async def counterparty_requisites(counterparty_id: Any) -> dict[str, str]:
    """ИНН/ПИНФЛ и адрес покупателя; нет строки — пустые."""
    from services import adb_core

    try:
        cid = int(counterparty_id)
    except (TypeError, ValueError):
        return {"tin": "", "address": ""}
    row = await adb_core.fetchrow(
        "SELECT tin, address FROM counterparty_requisites WHERE counterparty_id = $1", cid
    )
    return {"tin": str((row or {}).get("tin") or ""), "address": str((row or {}).get("address") or "")}


async def set_counterparty_requisites(
    counterparty_id: int, *, tin: Any, address: Any, phone: Any = None, by: int | None = None,
) -> dict[str, str]:
    """Записать ИНН/ПИНФЛ и адрес (sidecar) и, если прислан, телефон. Одной
    транзакцией: половина карточки хуже старой карточки."""
    from services import adb_core
    from services.database import now_str

    cid = int(counterparty_id)
    tin_clean = clean_tin(tin)
    address_clean = str(address or "").strip()[:300]
    stamp = now_str()
    async with adb_core.transaction() as txn:
        exists = await txn.fetchval("SELECT id FROM counterparties WHERE id = $1", cid)
        if not exists:
            raise RequisitesInvalid("Клиент не найден — обновите экран")
        have = await txn.fetchval(
            "SELECT counterparty_id FROM counterparty_requisites WHERE counterparty_id = $1", cid
        )
        if have:
            await txn.execute(
                "UPDATE counterparty_requisites SET tin = $1, address = $2, updated_by = $3, "
                "updated_at = $4 WHERE counterparty_id = $5",
                tin_clean, address_clean, by, stamp, cid,
            )
        else:
            await txn.execute(
                "INSERT INTO counterparty_requisites (counterparty_id, tin, address, updated_by, updated_at) "
                "VALUES ($1, $2, $3, $4, $5)",
                cid, tin_clean, address_clean, by, stamp,
            )
        if phone is not None:
            await txn.execute(
                "UPDATE counterparties SET phone = $1 WHERE id = $2",
                str(phone).strip()[:64] or None, cid,
            )
    return {"tin": tin_clean, "address": address_clean}
