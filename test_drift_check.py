"""Тесты чекера расхождения кода (drift_check.py).

Проверяем то, на чём держится решение таймера: когда расхождения есть,
когда их нет, что попадает в отчёт и с каким кодом выходит процесс.
Git-режим проверяется на временном репозитории, сверка — на временных
каталогах, реальные папки проекта тест не трогает.

Запуск: python test_drift_check.py  (stdlib, без pytest).
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import drift_check as D  # noqa: E402


def check(name, cond):
    print(("OK   " if cond else "FAIL ") + name)
    return cond


def write(root: str, rel: str, text: str):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def run(argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = D.main(argv)
    return rc, out.getvalue(), err.getvalue()


def git(repo: str, *args: str) -> None:
    subprocess.run(["git", "-C", repo, "-c", "user.email=t@example.com",
                    "-c", "user.name=test", *args],
                   check=True, capture_output=True, text=True)


def main() -> int:
    ok = True
    tmp = tempfile.mkdtemp()

    # --- эталон (как будто «локальная копия», она же репозиторий) и сервер ---
    ref = os.path.join(tmp, "ref")
    live = os.path.join(tmp, "live")
    for root in (ref, live):
        write(root, "bot.py", "print(1)\n")
        write(root, "deploy/svc.service", "[Service]\n")

    report = os.path.join(tmp, "drift_report.txt")

    # 1. совпадает → код 0, отчёта нет
    rc, out, _ = run(["--local", ref, "--dir", live, "--report", report])
    ok &= check("одинаковый код → код 0 и сообщение «совпадает»",
                rc == 0 and "совпадает с эталоном" in out
                and not os.path.exists(report))

    # 2. разошлось содержимое → код 1 и отчёт с вердиктом
    write(live, "bot.py", "print(2)\n")
    rc, out, _ = run(["--local", ref, "--dir", live, "--report", report])
    text = open(report, encoding="utf-8").read() if os.path.exists(report) else ""
    ok &= check("изменённый файл → код 1, отчёт записан",
                rc == 1 and "ВЕРДИКТ" in text and "bot.py" in text)
    ok &= check("в отчёте видно, кто новее",
                "bot.py" in text and "новее" in text)

    # 3. файл только на сервере (правка мимо git)
    write(live, "hotfix.py", "x = 1\n")
    rc, _, _ = run(["--local", ref, "--dir", live, "--report", report])
    text = open(report, encoding="utf-8").read()
    ok &= check("файл только на сервере → расхождение и пометка «мимо git»",
                rc == 1 and "hotfix.py" in text and "мимо git" in text)

    # 4. файл только в репозитории (сервер не развёрнут)
    write(ref, "new_feature.py", "y = 2\n")
    rc, _, _ = run(["--local", ref, "--dir", live, "--report", report])
    text = open(report, encoding="utf-8").read()
    ok &= check("файл только в репозитории → «разверните»",
                rc == 1 and "new_feature.py" in text and "разверните" in text)

    # 5. --json: машинный вывод с разбивкой по сторонам
    rc, out, _ = run(["--local", ref, "--dir", live, "--report", report, "--json"])
    payload = json.loads(out[out.index("{"):])
    ok &= check("--json отдаёт списки и коммит",
                rc == 1 and payload["ok"] is False
                and "new_feature.py" in payload["only_repo"]
                and "hotfix.py" in payload["only_server"]
                and [d["file"] for d in payload["differ"]] == ["bot.py"])

    # 6. данные прогона и бэкапы расхождением не считаются
    clean_ref = os.path.join(tmp, "clean_ref")
    clean_live = os.path.join(tmp, "clean_live")
    for root in (clean_ref, clean_live):
        write(root, "bot.py", "print(1)\n")
        write(root, ".env", "KEY=1\n")
        write(root, "position.json", "{}\n")
    write(clean_live, "position.json", '{"position": null}\n')
    write(clean_live, ".env", "KEY=22\n")
    write(clean_live, "memory.json.pre-sanity.bak", "{}\n")
    write(clean_live, "stats.log", "строки\n")
    write(clean_live, "_bak/20261022/bot.py", "print(9)\n")
    write(clean_live, "drift_report.txt", "прошлый отчёт\n")
    rc, out, _ = run(["--local", clean_ref, "--dir", clean_live,
                      "--report", os.path.join(tmp, "r2.txt")])
    ok &= check("данные прогона, бэкапы, _bak и старый отчёт — не расхождение",
                rc == 0)

    # 7. успешная проверка убирает старый отчёт (не пугает устаревшим текстом)
    stale = os.path.join(tmp, "r2.txt")
    write(tmp, "r2.txt", "старый отчёт\n")
    run(["--local", clean_ref, "--dir", clean_live, "--report", stale])
    ok &= check("после успешной сверки старый отчёт удалён",
                not os.path.exists(stale))

    # 8. нет каталога-эталона → код 3 (ошибка путей, а не «дрейф»)
    rc, _, _ = run(["--local", os.path.join(tmp, "нет-такого"), "--dir", live,
                    "--report", report])
    ok &= check("нет каталога-эталона → код 3", rc == 3)

    # 9. эталон недоступен (git) → код 2, а не «расхождение»
    rc, _, err = run(["--repo", os.path.join(tmp, "нет-такого-репо"),
                      "--ref", "main", "--dir", live, "--report", report,
                      "--cache", os.path.join(tmp, "cache-err")])
    ok &= check("git-эталон недоступен → код 2", rc == 2 and "эталон" in err)

    # 10. git-режим: сверка с настоящей веткой репозитория
    if shutil.which("git"):
        grepo = os.path.join(tmp, "gitref")
        os.makedirs(grepo)
        subprocess.run(["git", "init", "-q", grepo], check=True,
                       capture_output=True)
        subprocess.run(["git", "-C", grepo, "symbolic-ref", "HEAD",
                        "refs/heads/main"], check=True, capture_output=True)
        write(grepo, "bot.py", "print(1)\n")
        write(grepo, "deploy/svc.service", "[Service]\n")
        git(grepo, "add", "-A")
        git(grepo, "commit", "-q", "-m", "init")

        gcache = os.path.join(tmp, "cache-git")
        greport = os.path.join(tmp, "git_report.txt")
        # копия содержимого репозитория: с ней git-эталон должен совпасть
        gmirror = os.path.join(tmp, "gmirror")
        write(gmirror, "bot.py", "print(1)\n")
        write(gmirror, "deploy/svc.service", "[Service]\n")
        rc, out, _ = run(["--repo", grepo, "--ref", "main", "--dir", gmirror,
                          "--cache", gcache, "--report", greport])
        ok &= check("git-эталон совпадает с каталогом → код 0",
                    rc == 0 and os.path.isdir(gcache))

        rc, out, _ = run(["--repo", grepo, "--ref", "main", "--dir", live,
                          "--cache", gcache, "--report", greport])
        text = open(greport, encoding="utf-8").read() if os.path.exists(greport) else ""
        ok &= check("git-эталон разошёлся → код 1 и коммит в отчёте",
                    rc == 1 and "коммит" in text and "hotfix.py" in text)
    else:
        print("SKIP git-режим: git не найден в PATH")

    # 11. один и тот же набор не должен спамить алертом (проверяем отпечаток)
    diff_a = {"only_a": ["x.py"], "only_b": [], "differ": [], "same": 0}
    diff_b = {"only_a": ["x.py"], "only_b": [], "differ": [], "same": 7}
    diff_c = {"only_a": ["x.py", "y.py"], "only_b": [], "differ": [], "same": 0}
    ok &= check("отпечаток одинаков для одного набора и различается для другого",
                D.fingerprint(diff_a) == D.fingerprint(diff_b)
                and D.fingerprint(diff_a) != D.fingerprint(diff_c))

    shutil.rmtree(tmp, ignore_errors=True)
    print()
    print("Итог:", "все проверки пройдены" if ok else "ЕСТЬ ПРОВАЛЫ")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
