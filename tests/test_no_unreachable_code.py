"""Сторож: за `return`/`raise`/`continue`/`break` в блоке ничего не стоит.

Повод — живой баг. В трёх cron-CLI (`run_ops_monitor`, `run_debts_notify`,
`run_money_report`) закрытие HTTP-сессии Telegram было написано так:

    except Exception:
        logger.exception(...)
        return 1
        await close_tg_session()   # ← сюда не попасть никогда

Строка не исполнялась ни разу: на успехе `return 0` уходит из `try` мимо
`except`, на ошибке `return 1` срабатывает до неё. Сессия не закрывалась
вообще, и каждый прогон писал в лог два `[ERROR] Unclosed client session` —
при том что `rc=0`, и дежурный видел «задача отработала успешно».

Опечатка скопировалась в три файла и пережила полный гейт: ruff в наборе
`E9,F,B,ASYNC,UP,SIM` правила на недостижимый код не имеет, а mypy смотрит
только шесть модулей ядра. Проверка дешёвая — обход AST, — поэтому она здесь,
а не в надежде на глазок ревьюера.

Namespace-пакеты и `scripts/` не исключаем: правило общее.
"""

import ast
import pathlib

PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent

# Каталоги не нашего кода: чужие исходники под это правило не подписывались.
SKIP_TOP = {".git", ".tools", ".venv", "venv", "node_modules", "app"}

# Узлы, после которых управление дальше по блоку не идёт.
TERMINATORS = (ast.Return, ast.Raise, ast.Continue, ast.Break)

# Поля тела, которые бывают у узлов с блоками.
BODY_FIELDS = ("body", "orelse", "finalbody")


def _unreachable(tree: ast.AST) -> list[tuple[int, str]]:
    """Строки, до которых управление не дойдёт, и чем они отрезаны."""
    found = []
    for node in ast.walk(tree):
        for field in BODY_FIELDS:
            block = getattr(node, field, None)
            if not isinstance(block, list):
                continue
            for i, stmt in enumerate(block[:-1]):
                if isinstance(stmt, TERMINATORS):
                    found.append((block[i + 1].lineno, type(stmt).__name__.lower()))
                    break  # первого хватает: остальное в блоке тоже мертво
    return found


def test_no_statements_after_return_anywhere_in_project():
    offenders = []
    for path in PROJECT_ROOT.rglob("*.py"):
        rel = path.relative_to(PROJECT_ROOT)
        if rel.parts[0] in SKIP_TOP:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue  # не наш файл — пусть о нём говорит ruff
        for lineno, kind in _unreachable(tree):
            offenders.append(f"{rel}:{lineno} — код после {kind}")

    assert not offenders, "недостижимый код:\n  " + "\n  ".join(sorted(offenders))
