"""Тесты двусторонней сверки копий (compare_copies.py).

Проверяем не печать, а выводы, на которых строится решение: что считается
версией, кто новее и какой код возврата. Копии создаются во временном
каталоге — реальные папки проекта тест не трогает.

Запуск: python test_compare_copies.py  (stdlib, без pytest).
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile

# Консоль Windows по умолчанию cp1252 — печать кириллицы в check() роняла
# тест с UnicodeEncodeError при прямом запуске (через run_tests.py работало,
# потому что раннер выставляет PYTHONIOENCODING=utf-8).
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import compare_copies as C  # noqa: E402


def check(name, cond):
    print(("OK   " if cond else "FAIL ") + name)
    return cond


def write(root: str, rel: str, text: str, mtime: float | None = None):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    if mtime is not None:
        os.utime(path, (mtime, mtime))


def main() -> int:
    ok = True
    tmp = tempfile.mkdtemp()
    A = os.path.join(tmp, "A")
    B = os.path.join(tmp, "B")
    os.makedirs(A)
    os.makedirs(B)

    T1 = 1_700_000_000.0
    T2 = T1 + 3600.0          # на час новее
    T3 = T1 + 7200.0

    # 1. одинаковые копии → расхождений нет
    for root in (A, B):
        write(root, "bot.py", "print(1)\n", T1)
        write(root, "deploy/svc.service", "[Service]\n", T1)
    dA, dS = C.scan(A)
    dB, _ = C.scan(B)
    diff = C.compare(dA, dB)
    ok &= check("одинаковые копии → нет расхождений",
                not diff["only_a"] and not diff["only_b"]
                and not diff["differ"] and diff["same"] == 2
                and C.verdict(diff).startswith("копии совпадают"))

    # 2. содержимое разное, B новее → «B новее, A устарела»
    write(B, "bot.py", "print(2)\n", T2)
    dB, _ = C.scan(B)
    diff = C.compare(dA, dB)
    ok &= check("изменённый файл: различие найдено и B признан новее",
                [d["file"] for d in diff["differ"]] == ["bot.py"]
                and diff["differ"][0]["newer"] == "B"
                and C.verdict(diff) == "B новее — A устарела")

    # 3. содержимое разное при РАВНОМ mtime → mtime не врёт, но и не помогает
    write(A, "bot.py", "print(3)\n", T2)
    dA, _ = C.scan(A)
    diff = C.compare(dA, dB)
    ok &= check("равный mtime при разном содержимом → отдельный вердикт",
                diff["differ"][0]["newer"] == "="
                and "mtime не поможет" in C.verdict(diff))

    # 4. файл только в A → B отстаёт
    write(A, "new_feature.py", "x = 1\n", T3)
    dA, _ = C.scan(A)
    diff = C.compare(dA, dB)
    ok &= check("файл только в A → «B отстаёт»",
                diff["only_a"] == ["new_feature.py"]
                and C.verdict(diff).startswith("B отстаёт"))

    # 5. лишние файлы с обеих сторон → копии разошлись
    write(B, "other.py", "y = 2\n", T3)
    dB, _ = C.scan(B)
    diff = C.compare(dA, dB)
    ok &= check("свои файлы у каждой копии → «копии разошлись»",
                diff["only_a"] == ["new_feature.py"]
                and diff["only_b"] == ["other.py"]
                and "разошлись" in C.verdict(diff))

    # 6. данные прогона не считаются версией, но видны отдельно
    for root in (A, B):
        write(root, "position.json", "{}", T1)
        write(root, "bot.log", "лог\n", T1)
        write(root, ".env", "KEY=1\n", T1)
    write(B, "position.json", "{\"position\": null}", T2)
    write(B, ".env", "KEY=22\n", T2)
    pa, sa = C.scan(A)
    pb, sb = C.scan(B)
    diff = C.compare(pa, pb)
    state_lines = C.state_diff(sa, sb, detailed=False)
    state_names = {"position.json", "bot.log", ".env"}
    touched = (set(diff["only_a"]) | set(diff["only_b"])
               | {d["file"] for d in diff["differ"]})
    ok &= check("position.json/bot.log/.env не попадают в сверку версии",
                not (state_names & touched)
                and "bot.py" in {d["file"] for d in diff["differ"]})
    ok &= check("данные прогона показаны отдельно",
                any("position.json" in ln for ln in state_lines)
                and any(".env" in ln for ln in state_lines))
    ok &= check(".env: содержимое не хешируется (сравнение по размеру)",
                sa[".env"]["hash"] == "" and sb[".env"]["hash"] == ""
                and any(".env: отличается" in ln for ln in state_lines))

    # 6b. бэкапы и логи — данные прогона, а не код (на VPS это
    # config.json.bak.20261022_070218, memory.json.pre-sanity.bak,
    # stats.log; без правила они давали ложный «дрейф» копий)
    for root in (A, B):
        write(root, "config.json.bak.20261022_070218", "{}\n", T1)
        write(root, "memory.json.pre-sanity.bak", "{}\n", T1)
        write(root, "stats.log", "строки\n", T1)
    write(B, "stats.log", "другие строки\n", T2)
    write(A, "_bak/20261022_065640/bot.py", "print(9)\n", T1)
    write(B, "_bak/20261022_065640/bot.py", "print(9)\n", T1)
    pa, sa = C.scan(A)
    pb, sb = C.scan(B)
    diff = C.compare(pa, pb)
    touched = (set(diff["only_a"]) | set(diff["only_b"])
               | {d["file"] for d in diff["differ"]})
    ok &= check("бэкапы (*.bak) и логи (*.log) не попадают в сверку версии",
                not ({".bak", "stats.log"} & {t.rsplit("/", 1)[-1]
                                              for t in touched})
                and not any(t.endswith(".bak") for t in touched)
                and not any(t.endswith("stats.log") for t in touched))
    ok &= check("каталог _bak не сверяется вообще",
                not any(t.startswith("_bak/") for t in touched))
    ok &= check("бэкапы и логи видны как данные прогона",
                "config.json.bak.20261022_070218" in sa
                and "stats.log" in sa
                and any("stats.log" in ln
                        for ln in C.state_diff(sa, sb, detailed=False)))

    # 7. main(): коды возврата и --json
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc_diff = C.main([A, B, "--json"])
    payload = json.loads(buf.getvalue())
    ok &= check("main: расхождения → код 1 и JSON с вердиктом",
                rc_diff == 1 and payload["only_a"] == ["new_feature.py"]
                and payload["only_b"] == ["other.py"]
                and "разошлись" in payload["verdict"]
                and not any(p.endswith(".bak") for p in payload["only_a"])
                and not any(p.startswith("_bak/") for p in payload["only_a"]))

    C2 = os.path.join(tmp, "C")
    shutil.copytree(A, C2)
    with contextlib.redirect_stdout(io.StringIO()):
        rc_same = C.main([A, C2])
    ok &= check("main: идентичные копии → код 0", rc_same == 0)

    with contextlib.redirect_stderr(io.StringIO()):
        rc_missing = C.main([A, os.path.join(tmp, "нет-такой")])
    ok &= check("main: нет каталога → код 2", rc_missing == 2)

    # 8. размеры в отчёте читаются по-человечески (не «0.0 КБ»)
    ok &= check("fmt_size: байты, килобайты, мегабайты",
                C.fmt_size(286) == "286 Б"
                and C.fmt_size(5678) == "5.5 КБ"
                and C.fmt_size(3 * 1024 * 1024) == "3.0 МБ")

    # 9. известные копии заданы двумя путями
    ok &= check("известные копии заданы двумя путями",
                len(C.KNOWN_COPIES) == 2
                and all(p for p in C.KNOWN_COPIES))

    shutil.rmtree(tmp, ignore_errors=True)
    print()
    print("Итог:", "все проверки пройдены" if ok else "ЕСТЬ ПРОВАЛЫ")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
