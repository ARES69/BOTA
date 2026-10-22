"""Тест-линтер: обращение к переменной `except ... as X` ПОСЛЕ блока except.

Python удаляет имя X при выходе из блока except, поэтому код вида

    try:
        ...
    except Exception as e:
        notify(f"ошибка: {e}")
    if isinstance(e, ClockSkew):        # ← UnboundLocalError
        ...

падает с UnboundLocalError ровно тогда, когда вылезла настоящая ошибка
(а если ошибок не было — на первой же итерации цикла).

Живой инцидент: так умирал `watch` — service `funding-watch` рестартовал
каждые ~25 секунд, потому что `isinstance(e, ClockSkew)` стоял после блока
except. Имя, которому присваивают значение в другом месте функции
(например, `loop_err = None` перед try), безопасно — тест такие места
не трогает.

Запуск: python test_except_scope.py [файл.py ...]
Без аргументов проверяет все *.py рядом с самим тестом. Stdlib, без pytest.
"""
from __future__ import annotations

import ast
import glob
import os
import sys

# Консоль Windows по умолчанию cp1252 — печать кириллицы в check() роняла
# тест с UnicodeEncodeError при прямом запуске (через run_tests.py работало,
# потому что раннер выставляет PYTHONIOENCODING=utf-8).
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

HERE = os.path.dirname(os.path.abspath(__file__))


def _parents(tree: ast.AST) -> dict:
    """Карта «узел → родитель» (нужна, чтобы понять область видимости)."""
    par: dict = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            par[child] = node
    return par


def _enclosing_func(node: ast.AST, par: dict):
    """Ближайшая enclosing-функция (или None для модуля)."""
    cur = par.get(node)
    while cur is not None:
        if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            return cur
        cur = par.get(cur)
    return None


def violations(path: str) -> list[tuple[int, str]]:
    """[(строка, имя)] — обращения к имени из except после его блока."""
    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename=path)
    par = _parents(tree)
    found: list[tuple[int, str]] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        scope = _enclosing_func(node, par)
        # имена, которым в этой же функции что-то присваивают: имя управляется
        # явно (например, loop_err = None), удаление из except не опасно
        assigned = {n.id for n in ast.walk(scope or tree)
                    if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}
        # все блоки except этой функции: какие имена в них действительно живы
        handlers = [(h.name, h.lineno, h.end_lineno or h.lineno)
                    for try_node in ast.walk(scope or tree)
                    if isinstance(try_node, ast.Try)
                    for h in try_node.handlers if h.name]
        for handler in node.handlers:
            name = handler.name
            if not name or name in assigned:
                continue             # `except ...:` без as — переменной нет
            for sub in ast.walk(scope or tree):
                if not isinstance(sub, ast.Name) or sub.id != name:
                    continue
                if not isinstance(sub.ctx, ast.Load):
                    continue
                if _enclosing_func(sub, par) is not scope:
                    continue                     # другая функция — другое имя
                # внутри блока except, который связывает ЭТО имя, оно живо
                # (в том числе во вложенных блоках того же handler)
                if any(hn == name and h0 <= sub.lineno <= h1
                       for hn, h0, h1 in handlers):
                    continue
                found.append((sub.lineno, name))
    return sorted(set(found))


def main() -> int:
    files = sys.argv[1:] or sorted(
        p for p in glob.glob(os.path.join(HERE, "*.py"))
        if os.path.basename(p) != os.path.basename(__file__))
    ok = True
    checked = bad_files = 0
    for path in files:
        name = os.path.basename(path)
        try:
            bad = violations(path)
        except SyntaxError as e:
            print(f"FAIL {name}: не разобрался как Python ({e})")
            ok = False
            continue
        checked += 1
        if bad:
            bad_files += 1
            ok = False
            for line, var in bad:
                print(f"FAIL {name}:{line} — '{var}' из except используется "
                      f"после своего блока (UnboundLocalError при ошибке)")
        else:
            print(f"OK   {name}")
    print()
    print(f"проверено файлов: {checked}, с обращениями к except-переменной: "
          f"{bad_files}")
    print("Итог:", "все проверки пройдены" if ok else "ЕСТЬ ПРОВАЛЫ")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
