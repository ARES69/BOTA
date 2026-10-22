#!/usr/bin/env python3
"""Единый раннер тестов: прогоняет все test_*.py и печатает сводку.

    python run_tests.py                 # все тесты, краткая сводка
    python run_tests.py -v              # с полным выводом каждого теста
    python run_tests.py --live          # живой тест на бирже (LIVE_CHECK=1)
    python run_tests.py --only health   # только тесты, чей файл содержит 'health'
    python run_tests.py --skip live     # пропустить совпавшие

Каждый тест запускается ОТДЕЛЬНЫМ процессом (в проекте нет pytest — тесты
самодостаточные, с кодом возврата 0/1). Раннер перехватывает вывод, показывает
по каждому тесту статус (PASS/FAIL/SKIP) и время, а в конце — сводку.
Общий код возврата: 0 — всё прошло (SKIP не считается провалом), 1 — есть
провалы (или ничего не нашлось для запуска).

Файл намеренно назван run_tests.py (а не test_*.py), чтобы раннер не находил
сам себя и не зацикливался. Живой тест test_live_check.py по умолчанию
печатает SKIP и ордеров не ставит — поэтому безопасен в общем прогоне;
флаг --live выставляет для него LIVE_CHECK=1 (он ставит РЕАЛЬНЫЕ ордера на
testnet, поэтому запускать только осознанно).
"""
from __future__ import annotations

import argparse
import glob
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
LIVE_TEST = "test_live_check.py"   # единственный тест, которому нужен LIVE_CHECK=1


def discover() -> list[str]:
    """Все test_*.py в папке проекта, по алфавиту."""
    return sorted(glob.glob(os.path.join(HERE, "test_*.py")))


def _tail(text: str) -> str:
    for line in reversed((text or "").splitlines()):
        if line.strip():
            return line.strip()
    return ""


def run_one(path: str, live: bool,
            timeout: float) -> tuple[int, str, float]:
    """Запустить один тест. Возврат (код выхода, объединённый вывод, сек).
    Для живого теста выставляем LIVE_CHECK=1 (иначе — как обычно)."""
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    cmd = [sys.executable, path]
    if live and os.path.basename(path) == LIVE_TEST:
        env["LIVE_CHECK"] = "1"
    t0 = time.time()
    try:
        res = subprocess.run(cmd, cwd=HERE,
                             capture_output=True, text=True, encoding="utf-8",
                             errors="replace", env=env, timeout=timeout)
        rc = res.returncode
        out = (res.stdout or "") + (res.stderr or "")
    except subprocess.TimeoutExpired as e:
        rc = -1
        part = ""
        if e.stdout:
            part += e.stdout if isinstance(e.stdout, str) else e.stdout.decode(
                "utf-8", "replace")
        if e.stderr:
            part += e.stderr if isinstance(e.stderr, str) else e.stderr.decode(
                "utf-8", "replace")
        out = part + f"\n!! TIMEOUT после {timeout:g}s"
    return rc, out, time.time() - t0


def main() -> int:
    # вывод раннера — UTF-8: тесты содержат кириллицу/эмодзи, а на Windows
    # консоль по умолчанию cp1252 (иначе UnicodeEncodeError при печати)
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001 — старый Python/нестандартный stdout
        pass

    ap = argparse.ArgumentParser(
        description="Прогнать все test_*.py и напечатать сводку.")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="печатать полный вывод каждого теста")
    ap.add_argument("--live", action="store_true",
                    help="живой тест на бирже: выставить LIVE_CHECK=1 "
                         "(ставит РЕАЛЬНЫЕ ордера на testnet!)")
    ap.add_argument("--only", default=None,
                    help="запускать только тесты, чей файл содержит подстроку")
    ap.add_argument("--skip", default=None,
                    help="пропустить тесты, чей файл содержит подстроку")
    ap.add_argument("--timeout", type=float, default=300.0,
                    help="таймаут одного теста, сек (default 300)")
    args = ap.parse_args()

    files = discover()
    if args.only:
        files = [f for f in files if args.only in os.path.basename(f)]
    if args.skip:
        files = [f for f in files if args.skip not in os.path.basename(f)]
    if not files:
        print("нет тестов для запуска (проверьте --only/--skip)")
        return 1

    note = " (включая живой тест на бирже)" if args.live else ""
    print(f"Запуск {len(files)} тестов{note}\n" + "-" * 64)

    results: list[tuple[str, str, int, float, str, str]] = []
    t0 = time.time()
    for path in files:
        name = os.path.basename(path)
        rc, out, dt = run_one(path, args.live, args.timeout)
        status = "PASS" if rc == 0 else "FAIL"
        if rc == 0 and "SKIP" in out:
            status = "SKIP"      # живой тест без LIVE_CHECK и т.п.
        tail = _tail(out)
        results.append((name, status, rc, dt, out, tail))
        print(f"{status:<4} {name:<26} {dt:7.2f}s  {tail}")
        if args.verbose or status == "FAIL":
            for line in out.rstrip().splitlines():
                print("       | " + line)
            if status == "FAIL":
                print("       | (код возврата: {} )".format(rc))

    total = len(results)
    failed = [r for r in results if r[1] == "FAIL"]
    skipped = [r for r in results if r[1] == "SKIP"]
    passed = total - len(failed) - len(skipped)

    print("-" * 64)
    print(f"Сводка: всего {total}, прошло {passed}, пропущено {len(skipped)}, "
          f"провалено {len(failed)}  ({time.time() - t0:.1f}s)")
    for name, status, rc, _dt, _out, tail in results:
        if status != "PASS":
            print(f"  {status:<4} {name}  {tail}")
    if failed:
        print("\nЕСТЬ ПРОВАЛЫ")
        return 1
    print("\nВсё прошло")
    return 0


if __name__ == "__main__":
    sys.exit(main())
