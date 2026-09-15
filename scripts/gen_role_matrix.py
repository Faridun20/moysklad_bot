"""
Генератор `UI_QA_ROLES.md` — чеклист ручного QA WebApp по ролям (UI-WP-33).

Почему генератор, а не текст руками: права живут в `allowed_roles=(...)` внутри
`_authorize(...)` по каждому эндпоинту, и переписанный вручную список устаревает
первым же PR'ом. План пересборки прямо требует сверять роли по коду.

Запуск:
    python scripts/gen_role_matrix.py
"""

from __future__ import annotations

import ast
import collections
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent
SERVER = ROOT / "webapp" / "server.py"
ROLES = ROOT / "services" / "roles.py"
# Роутеры, подключённые в server.py отдельными файлами (чтобы параллельные
# ветки не правили одну середину server.py). Роли у них в том же формате.
ROUTERS = [ROOT / "webapp" / "costing_api.py", ROOT / "webapp" / "routes_accounting.py"]
OUT = ROOT / "UI_QA_ROLES.md"

ROLE_TITLES = {
    "admin": "Админ",
    "boss": "Руководитель",
    "manager": "Менеджер",
    "warehouse_keeper": "Кладовщик (сейчас не назначается)",
    "bookkeeper": "Бухгалтер (сейчас не назначается)",
}

ANY_ROLE = "любая активная роль"
NO_AUTH = "без авторизации"

# Какой экран какой эндпоинт дёргает — единственная РУЧНАЯ часть файла: из кода
# сервера этого не вывести, связь живёт во фронте.
SCREEN_MAP = [
    ("Главная", "все активные", "/api/home"),
    ("Заказы (список)", "admin, boss, manager", "/api/orders"),
    ("Решения (заявки, оплаты, сдачи, возвраты)", "admin, boss", "/api/orders/requests"),
    ("Меню → «Рабочие действия» (вид, не права)", "admin, boss", "/api/prefs/set"),
    ("Редактор заказа", "admin, boss, manager", "/api/orders/create"),
    ("Каталог/Склад", "admin, boss, manager, warehouse_keeper", "/api/stock"),
    ("Деньги → Подтвердить", "admin, boss, bookkeeper, manager*", "/api/deposits/pending"),
    ("Финансы → Долги", "admin, boss, manager", "/api/debts"),
    # «Поставщикам» — зеркало «Долгов»: там должны нам, здесь должны мы. Сумма
    # прихода это закупочная цена, поэтому экран руководству, как себестоимость.
    ("Деньги → Поставщикам («мы должны»)", "admin, boss", "/api/suppliers/debts"),
    ("Финансы → Клиенты", "admin, boss", "/api/clients/overview"),
    ("Курсы валют", "admin, boss (правка)", "/api/currency/rates"),
    ("Аналитика", "admin, boss, manager", "/api/analytics"),
    ("Деньги (лента)", "admin, boss", "/api/money/summary"),
    ("Операционная сводка", "admin, boss", "/api/ops-summary"),
    ("Возвраты (приёмка)", "admin, boss, warehouse_keeper, manager*", "/api/returns/pending"),
    ("Заказы → «Отгрузить» (руководство — с «Рабочими действиями»)", "admin, boss, warehouse_keeper, manager*", "/api/orders/ship"),
    ("Заказы → Техника", "admin, boss, manager", "/api/machines/list"),
    ("Техника → карточка", "admin, boss, manager", "/api/machines/card"),
    ("Техника → сделки", "admin, boss", "/api/machines/deal"),
    ("Оплата → «Куда поступили»; Настройки → Карты и счета", "admin, boss, manager (правка и архив — руководство)",
     "/api/pay_accounts"),
]

# Роли часто перечислены константой, а не литералом на месте: один и тот же
# набор у десятка ручек, и разъехавшийся дубль был бы дырой в правах. Резолвим
# такие имена, иначе генератор запишет раздел в «любая активная роль» — то есть
# соврёт в чеклисте ровно там, где его читают.
_CONST_RE = re.compile(r"^(_[A-Z][A-Z_0-9]*)\s*=\s*\(([^)]*)\)", re.M)

HEADER = """# QA-чеклист WebApp по ролям (UI-WP-33)

**Сгенерирован из кода** — `python scripts/gen_role_matrix.py`. Источник:
`allowed_roles=(...)` в `_authorize(...)` по каждому эндпоинту
`webapp/server.py`. Переписанный руками список прав устаревает первым же PR'ом,
поэтому план пересборки требует сверять его по коду.

## Как проверять

Под каждой ролью пройти все её разделы (Сегодня → Продажи → Склад → Деньги →
Клиенты; у руководства ещё Решения и Настройки, а работа менеджера — за
выключателем «Рабочие действия» в «Меню», проверить в обоих положениях; набор
зависит от роли) и убедиться, что:

1. экран открывается и не показывает `errorBox` вместо данных;
2. чего роли не положено — не отрисовано (кнопки/секции нет, а не «нажимается
   и отвечает 403»);
3. пустые состояния объясняют, что делать, а не просто «нет данных»;
4. в плотных списках (Долги, Клиенты, Курсы) строка не мельче 44px;
5. в тёмной и светлой теме карточка не сливается с фоном страницы.

`guest` проверяется отдельно: он обязан видеть ТОЛЬКО экран «Доступ не выдан»
(`renderNoAccess`) — без нижней навигации и поиска.
"""


def parse_acts_as(src: str) -> dict[str, list[str]]:
    """`ROLE_ALSO_ACTS_AS` из services/roles.py — через AST, без импорта модуля:
    импорт тянет config и БД, а генератору нужен только литерал."""
    for node in ast.parse(src).body:
        target = getattr(node, "target", None) or (getattr(node, "targets", None) or [None])[0]
        if isinstance(target, ast.Name) and target.id == "ROLE_ALSO_ACTS_AS" and node.value:
            return {k: list(v) for k, v in ast.literal_eval(node.value).items()}
    return {}


def parse_routes(src: str) -> list[tuple[str, list[str]]]:
    """[(путь, роли)] в порядке объявления в server.py."""
    marks = [
        (m.start(), m.group(1))
        for m in re.finditer(r'@(?:app|router)\.(?:get|post)\("([^"]+)"', src)
    ]
    consts = {
        m.group(1): sorted(re.findall(r'"([a-z_]+)"', m.group(2)))
        for m in _CONST_RE.finditer(src)
    }
    rows: list[tuple[str, list[str]]] = []
    for i, (pos, path) in enumerate(marks):
        end = marks[i + 1][0] if i + 1 < len(marks) else len(src)
        body = src[pos:end]
        allowed = re.search(r"allowed_roles=(\([^)]*\)|_[A-Z][A-Z_0-9]*)", body)
        if allowed:
            token = allowed.group(1)
            roles = consts.get(token) or sorted(re.findall(r'"([a-z_]+)"', token))
        elif "_authorize(" in body:
            # Гейт есть, но роли не сужены — пускает любую активную (не guest).
            roles = [ANY_ROLE]
        else:
            roles = [NO_AUTH]
        rows.append((path, roles))
    return rows


def render(rows: list[tuple[str, list[str]]], acts_as: dict[str, list[str]] | None = None) -> str:
    acts_as = acts_as or {}
    by_role: dict[str, list[str]] = collections.defaultdict(list)
    for path, roles in rows:
        for role in roles:
            by_role[role].append(path)
        # Совмещение ролей: замещающий получает ручки замещаемых — ровно как
        # `role_allowed` в `_authorize`. Иначе чеклист соврал бы менеджеру.
        for role, extra in acts_as.items():
            if role not in roles and any(r in roles for r in extra):
                by_role[role].append(path)

    out = [HEADER, f"\n## Доступ по ролям\n\n_Всего эндпоинтов: {len(rows)}._\n"]
    if acts_as:
        pairs = "; ".join(
            f"`{role}` = + " + ", ".join(f"`{r}`" for r in extra) for role, extra in acts_as.items()
        )
        out.append(
            "\n> **Временное совмещение ролей** (`services/roles.py::ROLE_ALSO_ACTS_AS`): "
            f"{pairs}. Кладовщика и бухгалтера в штате пока нет, их работу делает "
            "менеджер; списки ниже уже учитывают это. Роли `warehouse_keeper`/"
            "`bookkeeper` через `/addrole` не назначаются. `*` в таблице экранов — "
            "доступ через совмещение.\n"
        )
    for role, title in ROLE_TITLES.items():
        paths = sorted(by_role.get(role, []))
        out.append(f"\n### {title} (`{role}`) — {len(paths)} эндпоинтов\n")
        out.append("<details><summary>Показать список</summary>\n")
        out.append("\n".join(f"- `{p}`" for p in paths) or "- —")
        out.append("\n</details>\n")

    for key, title in ((ANY_ROLE, "Доступно любой активной роли"), (NO_AUTH, "Без авторизации")):
        out.append(f"\n### {title}\n")
        out.append("\n".join(f"- `{p}`" for p in sorted(by_role.get(key, []))) or "- —")
        out.append("\n")

    out.append("\n## Экраны против прав\n")
    out.append("| Экран | Кто открывает | Ключевой эндпоинт |")
    out.append("|---|---|---|")
    for screen, who, endpoint in SCREEN_MAP:
        out.append(f"| {screen} | {who} | `{endpoint}` |")
    out.append(
        "\nТаблица экранов ручная (какой экран какой эндпоинт зовёт — это знание "
        "фронта), списки выше машинные. При расхождении верить спискам.\n"
    )
    return "\n".join(out)


def main() -> int:
    rows = parse_routes(SERVER.read_text(encoding="utf-8"))
    for extra in ROUTERS:
        if extra.exists():
            rows += parse_routes(extra.read_text(encoding="utf-8"))
    if not rows:
        print("не нашёл ни одного @app.get/post — формат server.py изменился?")
        return 1
    OUT.write_text(render(rows, parse_acts_as(ROLES.read_text(encoding="utf-8"))), encoding="utf-8")
    print(f"{OUT.name}: {len(rows)} эндпоинтов")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
