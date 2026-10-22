#!/usr/bin/env python3
"""Двусторонняя сверка двух копий проекта: что разошлось и какая устарела.

Зачем: проект лежит в двух местах (Desktop/opencode и Documents/BOTA), git в
нём нет — понять, где версия свежее, можно только сравнением содержимого и
времени правки. Скрипт НИЧЕГО не меняет и не синхронизирует: только читает и
печатает отчёт. Решение о переносе — за человеком.

    python compare_copies.py                   # известные две папки
    python compare_copies.py A B               # свои пути
    python compare_copies.py --state           # подробно и про данные прогона
    python compare_copies.py --json            # машинный вывод

Что сравнивается:
  • файлы проекта — содержимое (sha1) + время правки: код, тесты, документация,
    deploy/. Именно они определяют «версию»;
  • данные прогона (position.json, bot.log, memory.json, dashboard.html,
    health_state.json, config.json, .env …) в версию не входят: они законно
    разные на разных машинах. Их отличие показывается отдельно, без паники.
    Содержимое .env не читается никогда — только наличие и размер.

Коды возврата: 0 — файлы проекта совпадают; 1 — есть расхождения;
2 — папка не найдена / неверные аргументы.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time

# Консоль Windows по умолчанию cp1252 — печать кириллицы (а отчёт полностью
# на русском) роняла скрипт с UnicodeEncodeError. Перенастраиваем поток,
# иначе сверка копий не работает ровно там, где она нужна.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

# Известные копии проекта: первая — «A», вторая — «B» в отчёте.
KNOWN_COPIES = (
    r"C:\Users\lookastarik\Desktop\opencode\bybit-funding",
    r"C:\Users\lookastarik\Documents\BOTA\bybit-funding",
)

IGNORE_DIRS = {".git", ".freebuff", "__pycache__", ".idea", ".vscode"}

# Данные прогона и локальные настройки: в версию проекта не входят.
STATE_EXACT = {
    ".env", "config.json", "position.json", "snapshot.json", "memory.json",
    "bot.log", "watch.out", "dashboard.html", "health_state.json",
    "backtest_data.json", "scalp_state.json",
}
STATE_PREFIX = ("bot.log.",)

MTIME_EPS = 1.0        # секунда: mtime в пределах секунды считаем равным


def is_state(rel: str) -> bool:
    """Файл — данные прогона (не версия проекта)?"""
    name = rel.rsplit("/", 1)[-1]
    return name in STATE_EXACT or name.startswith(STATE_PREFIX)


def file_hash(path: str) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def scan(root: str) -> tuple[dict, dict]:
    """(файлы проекта, данные прогона): путь → {size, mtime, hash}. Только чтение.

    .env не хешируется: сравниваются только наличие и размер, чтобы секреты
    не попадали ни в вывод, ни в хеш-сравнение.
    """
    project: dict[str, dict] = {}
    state: dict[str, dict] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in IGNORE_DIRS)
        for name in sorted(filenames):
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root).replace("\\", "/")
            try:
                st = os.stat(full)
                meta = {"size": st.st_size, "mtime": st.st_mtime,
                        "hash": ("" if name == ".env" else file_hash(full))}
            except OSError:
                continue
            (state if is_state(rel) else project)[rel] = meta
    return project, state


def compare(a_files: dict, b_files: dict) -> dict:
    """Сравнить две карты файлов. Возврат: only_a/only_b/differ/same."""
    only_a = sorted(set(a_files) - set(b_files))
    only_b = sorted(set(b_files) - set(a_files))
    differ: list[dict] = []
    same = 0
    for rel in sorted(set(a_files) & set(b_files)):
        fa, fb = a_files[rel], b_files[rel]
        if fa["hash"] and fb["hash"] and fa["hash"] == fb["hash"]:
            same += 1
            continue
        if not fa["hash"] and not fb["hash"]:
            # .env: содержимое не читаем — сравниваем размер
            if fa["size"] == fb["size"]:
                same += 1
                continue
        ma, mb = fa["mtime"], fb["mtime"]
        newer = "=" if abs(ma - mb) < MTIME_EPS else ("A" if ma > mb else "B")
        differ.append({"file": rel, "newer": newer, "a": ma, "b": mb,
                       "size_a": fa["size"], "size_b": fb["size"]})
    return {"only_a": only_a, "only_b": only_b, "differ": differ, "same": same}


def verdict(diff: dict) -> str:
    """Короткий вердикт: какая копия отстала (или что решать человеку).

    Приоритет у лишних файлов: набор файлов — самый сильный признак «где
    версия». Различающееся содержимое уточняет вывод (общие файлы могут быть
    новее на другой стороне — тогда копии разошлись, а не «отстала одна»).
    """
    extra_a, extra_b = bool(diff["only_a"]), bool(diff["only_b"])
    differ = diff["differ"]
    if not (extra_a or extra_b or differ):
        return "копии совпадают по файлам проекта"
    newer = {d["newer"] for d in differ}
    if extra_a and extra_b:
        return ("копии разошлись: у каждой есть файлы, которых нет в другой")
    if extra_a or extra_b:
        behind, ahead = ("B", "A") if extra_a else ("A", "B")
        if newer == {behind}:
            return (f"копии разошлись: файлы только у {ahead}, но общие "
                    f"файлы новее у {behind}")
        return f"{behind} отстаёт: есть файлы только у {ahead}"
    if not differ:
        return "копии разошлись"
    if newer == {"A"}:
        return "A новее — B устарела"
    if newer == {"B"}:
        return "B новее — A устарела"
    if newer == {"="}:
        return ("содержимое разное при равном времени правки — mtime не "
                "поможет, выбирайте по смыслу")
    return ("файлы новее в обеих копиях — копии разошлись, нужно решение по "
            "каждому файлу")


def fmt_ts(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


def fmt_size(n: float) -> str:
    """Размер по-человечески: 286 Б, 5.5 КБ, 3.0 МБ."""
    if n < 1024:
        return f"{n:.0f} Б"
    kb = n / 1024.0
    if kb < 1024:
        return f"{kb:.1f} КБ"
    return f"{kb / 1024.0:.1f} МБ"


def state_diff(a_state: dict, b_state: dict, detailed: bool) -> list[str]:
    """Расхождения по данным прогона: они нормальны, но о них надо знать."""
    lines: list[str] = []
    for rel in sorted(set(a_state) | set(b_state)):
        fa, fb = a_state.get(rel), b_state.get(rel)
        if fa and fb and fa["hash"] == fb["hash"] and fa["size"] == fb["size"]:
            continue
        if fa and fb:
            line = f"  {rel}: отличается"
            if detailed:
                line += (f" (A {fmt_size(fa['size'])}, {fmt_ts(fa['mtime'])} | "
                         f"B {fmt_size(fb['size'])}, {fmt_ts(fb['mtime'])})")
        elif fa:
            line = f"  {rel}: только в A"
            if detailed:
                line += f" ({fmt_size(fa['size'])}, {fmt_ts(fa['mtime'])})"
        else:
            line = f"  {rel}: только в B"
            if detailed:
                line += f" ({fmt_size(fb['size'])}, {fmt_ts(fb['mtime'])})"
        lines.append(line)
    return lines


def report(a_root: str, b_root: str, detailed: bool) -> tuple[dict, str]:
    """(результат сравнения, текст отчёта)."""
    a_project, a_state = scan(a_root)
    b_project, b_state = scan(b_root)
    diff = compare(a_project, b_project)
    diff["state"] = state_diff(a_state, b_state, detailed)
    diff["verdict"] = verdict(diff)

    out: list[str] = [
        f"Копия A: {a_root}",
        f"Копия B: {b_root}",
        "",
        "=== Файлы проекта (это и есть версия) ===",
        f"  совпадает: {diff['same']}",
        f"  только в A: {len(diff['only_a'])}",
        f"  только в B: {len(diff['only_b'])}",
        f"  отличаются содержимым: {len(diff['differ'])}",
    ]
    if diff["only_a"]:
        out += [f"    + {p}" for p in diff["only_a"]]
    if diff["only_b"]:
        out += [f"    - {p}" for p in diff["only_b"]]
    if diff["differ"]:
        out.append("")
        for d in diff["differ"]:
            who = {"A": "новее A", "B": "новее B", "=": "время равное"}[d["newer"]]
            out.append(f"    ~ {d['file']}: {who} "
                       f"(A {fmt_ts(d['a'])} | B {fmt_ts(d['b'])})")
    out += ["", f"ВЕРДИКТ: {diff['verdict']}"]
    if diff["state"]:
        out += ["",
                "=== Данные прогона (в версию не входят, отличие нормально) ==="]
        out += diff["state"]
    return diff, "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Двусторонняя сверка двух копий проекта: что разошлось и "
                    "какая копия устарела. Ничего не меняет.")
    p.add_argument("a", nargs="?", default=KNOWN_COPIES[0],
                   help=f"копия A (по умолчанию {KNOWN_COPIES[0]})")
    p.add_argument("b", nargs="?", default=KNOWN_COPIES[1],
                   help=f"копия B (по умолчанию {KNOWN_COPIES[1]})")
    p.add_argument("--state", action="store_true",
                   help="подробно про данные прогона (размеры и время)")
    p.add_argument("--json", action="store_true", help="машинный вывод")
    args = p.parse_args(argv)

    for path in (args.a, args.b):
        if not os.path.isdir(path):
            print(f"нет каталога: {path}", file=sys.stderr)
            return 2

    diff, text = report(args.a, args.b, args.state)
    if args.json:
        payload = {
            "a": os.path.abspath(args.a),
            "b": os.path.abspath(args.b),
            "same": diff["same"],
            "only_a": diff["only_a"],
            "only_b": diff["only_b"],
            "differ": [{k: d[k] for k in ("file", "newer")}
                       for d in diff["differ"]],
            "verdict": diff["verdict"],
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(text)

    identical = not (diff["only_a"] or diff["only_b"] or diff["differ"])
    return 0 if identical else 1


if __name__ == "__main__":
    sys.exit(main())
