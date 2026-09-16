"""Личные настройки интерфейса (таблица `user_prefs`).

Первая — «Рабочие действия» руководителя (`work_actions`).
Решение владельца: у руководителя в меню только то, что нужно смотреть,
решать и контролировать; работа менеджера (накладные, отгрузка, касса, лиды,
канал, моточасы…) спрятана за выключателем в «Меню» — на случай, когда
менеджер заболел.

**Это предпочтение ВИДА, а не права.** Ни одна ручка его не читает: сервер
пускает руководителя в те же операции, что и раньше, выключатель лишь решает,
рисовать ли кнопки. Иначе «спрятать» превратилось бы в «запретить», и в
экстренный день руководитель не смог бы сделать работу, на которую имеет право.

Вторая — `work_actions_hint_shown` (продуктовый аудит, D2): сколько раз
руководителю уже показали подсказку «Работаете один? Включите «Рабочие
действия»…» на «Сегодня». Счётчик, а не bool: подсказку можно пропустить не
заметив, поэтому показываем несколько раз подряд, но не бесконечно — фронт
(`workActionsHintVisible` в app.js) перестаёт её рисовать при достижении
предела показов.

Третья — `doc_lang`: язык печатных форм (счёт на оплату, товарная накладная)
— `ru_uz` / `ru` / `uz`. Выбирают его при печати или отправке, и следующая
печать предлагает тот же язык одним касанием. Пишут её сами ручки печати
(`remember_doc_lang`), а не выключатель в «Настройках»: это не решение, а
последний выбор. Нужна всем, кто печатает, — не только руководству.

Хранится на сервере, а не в localStorage: Telegram WebView хранилище теряет, и
выключатель (или счётчик показов), который сам собой гаснет, хуже
отсутствующего.
"""

from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)

# key → (значение по умолчанию, роли, которым настройка имеет смысл).
# Неизвестный ключ записать нельзя: таблица не должна становиться свалкой.
# Тип значения по умолчанию решает, как его валидирует `/api/prefs/set`
# (webapp/server.py): bool — переключатель, int — счётчик (0..HINT_MAX_SHOWS
# у `work_actions_hint_shown`, дальше фронт просто перестаёт слать инкременты).
PREFS: dict[str, tuple[bool | int | str, tuple[str, ...]]] = {
    "work_actions": (False, ("admin", "boss")),
    "work_actions_hint_shown": (0, ("admin", "boss")),
    "doc_lang": ("ru_uz", ("admin", "boss", "manager", "warehouse_keeper", "bookkeeper")),
}

# Строковые настройки — только из списка: таблица не свалка.
CHOICES: dict[str, tuple[str, ...]] = {
    "doc_lang": ("ru_uz", "ru", "uz"),
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
    elif isinstance(default, int):
        value = int(value)
    elif key in CHOICES:
        value = str(value)
        if value not in CHOICES[key]:
            raise ValueError(f"недопустимое значение {key}: {value!r}")
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


def doc_lang(user_id: int | None) -> str:
    """Последний выбранный язык печатных форм; нет — «рус + узб»."""
    if not user_id:
        return str(PREFS["doc_lang"][0])
    value = get_prefs(int(user_id)).get("doc_lang")
    return value if value in CHOICES["doc_lang"] else str(PREFS["doc_lang"][0])


def remember_doc_lang(user_id: int | None, lang: str | None) -> None:
    """Запомнить язык, если он отличается от сохранённого. Не бросает:
    сбой записи предпочтения не повод отказать в печати."""
    if not user_id or lang not in CHOICES["doc_lang"]:
        return
    try:
        if doc_lang(user_id) != lang:
            set_pref(int(user_id), "doc_lang", lang)
    except Exception:
        logger.warning("user_prefs: язык документов не запомнен для %s", user_id, exc_info=True)
