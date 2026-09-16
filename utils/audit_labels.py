"""
Единая точка перевода кодов `audit_log.action` на русский.

Раньше словарь (`ACTION_EMOJI`) жил только в `utils.formatters` и покрывал
6 кодов (`user_added`/`role_changed`/`payment_sent`/... — большинство
реальных `add_audit_log(...)` вызовов в проекте писали код, для которого
формула "▪️" (заглушка). Экран аудита в WebApp для босса (C1) должен
показывать русский ярлык действия, а не сырой код, — этот словарь общий
для бота (`utils.formatters.format_audit_entry`) и WebApp
(`webapp/server.py::api_audit_log`), чтобы не разъезжались.

Код, которого здесь нет (например, будущая фича забыла его завести), —
НЕ прячем: `translate_action` возвращает исходную строку кодом как есть,
так у неизвестного действия в списке всё равно есть текст.
"""

from __future__ import annotations

ACTION_EMOJI: dict[str, str] = {
    "user_added": "🟢",
    "user_removed": "🔴",
    "user_deactivated": "🔴",
    "user_reactivated": "🟢",
    "role_changed": "🔄",
    "payment_sent": "💵",
    "payment_confirmed": "✅",
    "payment_rejected": "❌",
    "login": "👤",
    "order_shipped": "🚚",
    "shipment_auto_approved": "🚚",
    "order_cancelled": "🚫",
    "order_fully_paid": "✅",
    "credit_override": "⚠️",
    "cash_deposit_confirmed": "✅",
    "cash_deposit_rejected": "❌",
    "return_confirmed": "↩️",
}

# Ярлык — короткая русская фраза для «типа действия» в ленте аудита.
# Список собран по всем реальным вызовам `add_audit_log`/`_audit(...)` в
# проекте (handlers/*, services/*, webapp/*) на момент C1/C3 (см. отчёт
# агента) — новый код без записи здесь не «теряется», а показывается как
# есть (см. `translate_action`).
ACTION_LABELS: dict[str, str] = {
    # ─ пользователи/роли ─
    "login": "Вход",
    "user_added": "Сотрудник добавлен",
    "user_removed": "Сотрудник удалён",
    "role_changed": "Роль изменена",
    "user_deactivated": "Сотруднику закрыт доступ",
    "user_reactivated": "Сотруднику возвращён доступ",
    # ─ настройки ─
    "pref_set": "Личная настройка изменена",
    "setting_changed": "Настройка изменена",
    "company_requisites": "Реквизиты компании изменены",
    "counterparty_requisites": "Реквизиты клиента изменены",
    # ─ заказы/отгрузка ─
    "credit_override": "Заказ одобрен сверх лимита долга",
    "order_shipped": "Заказ отгружен",
    "order_shipment_failed": "Отгрузка: товар не списан со склада",
    "shipment_positions_skipped": "Отгрузка: позиции без карточки товара пропущены",
    "order_rejected": "Заказ возвращён на доработку",
    "order_unfrozen": "Заказ разморожен",
    "order_cancelled": "Заказ отменён",
    "order_deleted": "Черновик заказа удалён",
    "order_fully_paid": "Заказ полностью оплачен",
    "shipment_returned": "Заявка возвращена на доработку",
    "shipment_request_sent": "Заявка на отгрузку подана",
    "shipment_auto_approved": "Отгрузка оформлена без одобрения руководителя",
    # ─ платежи/долги/сдачи ─
    "payment_sent": "Платёж внесён",
    "payment_confirmed": "Платёж подтверждён",
    "payment_rejected": "Платёж отклонён",
    "payment_linked_to_order": "Платёж привязан к заказу",
    "order_payment_recorded": "Оплата внесена: указано, как получены деньги",
    "debt_payment_claimed": "Оплата долга отмечена менеджером",
    "credit_limit_changed": "Лимит долга клиента изменён",
    "cash_deposit_confirmed": "Сдача в кассу подтверждена",
    "cash_deposit_rejected": "Сдача в кассу отклонена",
    # ─ возвраты ─
    "return_confirmed": "Возврат подтверждён",
    # ─ бухгалтерия ─
    "accounting_switch": "Бухгалтерский учёт включён или выключен",
    "accounting_toggled": "Бухгалтерский учёт включён или выключен",
    "accounting_account_saved": "Счёт бухгалтерии сохранён",
    "accounting_account_archived": "Счёт бухгалтерии отправлен в архив",
    "accounting_receipt": "Деньги поступили на счёт бухгалтерии",
    "accounting_expense": "Деньги списаны со счёта бухгалтерии",
    "accounting_transfer": "Перевод между счетами",
    "accounting_exchange": "Обмен валюты",
    "accounting_close_day": "День по счёту закрыт",
    "accounting_void": "Документ бухгалтерии отменён",
    # ─ товары/склад ─
    "product_price_set": "Цена товара установлена",
    "wh_invoice_create": "Движение по складу оформлено",
    "wh_invoice_cancel": "Движение по складу отменено",
    "container_created": "Контейнер заведён",
    "container_updated": "Контейнер изменён",
    "container_deleted": "Контейнер удалён",
    "container_checked": "Контейнер сверен",
    "container_arrived": "Контейнер прибыл",
    "container_costing": "Себестоимость контейнера рассчитана",
    # ─ техника ─
    "machine_created": "Техника заведена",
    "machine_updated": "Карточка техники изменена",
    "machine_vin_changed": "VIN изменён",
    "machine_status_changed": "Статус техники изменён",
    "machine_arrived": "Техника прибыла",
    "machine_deleted": "Техника удалена",
    "machine_hours_added": "Показание моточасов внесено",
    "machine_deal_created": "Сделка по технике оформлена",
    "machine_deal_closed": "Сделка по технике закрыта",
    "machine_deal_reopened": "Сделка по технике переоткрыта",
    "machine_deal_requested": "Заявка на сделку подана",
    "machine_deal_approved": "Заявка на сделку одобрена",
    "machine_deal_resubmitted": "Заявка на сделку отправлена повторно",
    "machine_unreserved": "Бронь техники снята",
    "machine_receipt_added": "Поступление по рассрочке добавлено",
    "machine_receipt_deleted": "Поступление по рассрочке удалено",
    "machine_receipt_overpaid": "Переплата по рассрочке подтверждена",
    # ─ клиенты и обращения ─
    "lead_status": "Статус обращения изменён",
    "lead_linked": "Обращение привязано к клиенту",
    "counterparty_create": "Клиент или поставщик заведён",
    "pay_account_created": "Наша карта или счёт заведены",
    "pay_account_updated": "Наша карта или счёт изменены",
    "pay_account_archived": "Наша карта или счёт отправлены в архив",
    # ─ документы ─
    "document_created": "Документ составлен",
    "sales_invoice_printed": "Счёт на оплату распечатан",
    "sales_invoice_sent": "Счёт на оплату отправлен в Telegram",
    # ─ синтетические коды истории заказа (C3, services.order_timeline —
    # не пишутся в audit_log, только для ярлыка события на ленте) ─
    "order_created": "Заказ создан",
    "order_submitted": "Заказ отправлен на одобрение",
    "shipment_requested": "Заявка на отгрузку подана",
    "shipment_approved": "Заявка на отгрузку одобрена",
    "shipment_rejected": "Заявка на отгрузку отклонена",
    "cash_deposit_created": "Сдача в кассу оформлена",
    "return_created": "Возврат оформлен",
    "return_rejected": "Возврат отклонён",
}


def translate_action(action: str | None) -> str:
    """Код действия → русский ярлык. Неизвестный код — как есть (см. докстринг
    модуля): молчаливая потеря информации хуже, чем показать сырой код."""
    if not action:
        return "—"
    return ACTION_LABELS.get(action, action)
