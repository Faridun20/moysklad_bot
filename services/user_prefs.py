"""Личные настройки интерфейса (таблица `user_prefs`).

Первая и пока единственная — «Рабочие действия» руководителя (`work_actions`).
Решение владельца: у руководителя в меню только то, что нужно смотреть,
решать и контролировать; работа менеджера (накладные, отгрузка, касса, лиды,
канал, моточасы…) спрятана за выключателем в «Меню» — на случай, когда
менеджер заболел.

**Это предпочтение ВИДА, а не права.** Ни одна ручка его не читает: сервер
пускает руководителя в те же операции, что и раньше, выключатель лишь решает,
рисовать ли кнопки. Иначе «спрятать» превратилось бы в «запретить», и в
экстренный день руководитель не смог бы сделать работу, на которую имеет право.

Хранится на сервере, а не в localStorage: Telegram WebView хранилище теряет, и
выключатель, который сам собой гаснет, хуже отсутствующего.
"""

from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)

# key → (значение по умолчанию, роли, которым настройка имеет смысл).
# Неизвестный ключ записать нельзя: таблица не должна становиться свалкой.
PREFS: dict[str, tuple[bool, tuple[str, ...]]] = {
    "work_actions": (False, ("admin", "boss")),
}


def defaults() -> dict:
    return {k: v[0] for k, v in PREFS.items()}


def applies_to(key: str, role: str) -> bool:
    spec = PREFS.get(key)
    return bool(spec) and role in spec[1]


def get_prefs(user_id: int) -> dict:
    """Все настройки пользователя поверх значений по умолчанию. Не падает:
    сбой чтения — значения по умолчанию (экран без выключателя лучше ошибки)."""
    # Модуль БД — лениво: тесты перезагружают services.database под свежую базу.
    from services import database as db

    prefs = defaults()
    try:
        with db.get_conn() as conn:
            cur = db.get_cursor(conn)
            cur.execute(
                db.q("SELECT pref_key, value FROM user_prefs WHERE user_id = ?"), (int(user_id),)
            )
            rows = cur.fetchall()
        for row in rows:
            key, raw = row["pref_key"], row["value"]
            if key not in PREFS:
                continue
            try:
                prefs[key] = json.loads(raw)
            except (TypeError, ValueError):
                logger.warning("user_prefs: битое значение %s у %s", key, user_id)
    except Exception:
        logger.warning("user_prefs: не прочитаны для %s", user_id, exc_info=True)
    return prefs


def set_pref(user_id: int, key: str, value) -> dict:
    """Записать настройку. Возвращает все настройки после записи."""
    from services import database as db

    if key not in PREFS:
        raise ValueError(f"неизвестная настройка: {key}")
    default = PREFS[key][0]
    if isinstance(default, bool):
        value = bool(value)
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(
            db.q(
                "INSERT INTO user_prefs (user_id, pref_key, value, updated_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT (user_id, pref_key) DO UPDATE SET value = excluded.value, "
                "updated_at = excluded.updated_at"
            ),
            (int(user_id), key, json.dumps(value), db.now_str()),
        )
        conn.commit()
    return get_prefs(user_id)
