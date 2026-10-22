#!/usr/bin/env python3
"""Сверка кода на сервере с версией в репозитории; отчёт при расхождении.

Зачем: бот живёт на VPS, а правки делаются на рабочей машине и уезжают в
git. Между «запушил» и «развернул» есть окно, в котором сервер работает на
старом коде, и заметить это можно только сверкой. CI делает то же самое
снаружи (задача vps-drift), а этот чекер работает ИЗНУТРИ сервера, поэтому
не зависит от того, включён ли доступ к VPS из GitHub и лежит ли ноутбук.

Что сравнивается: файлы проекта (код, тесты, deploy/, документация) по
sha1 — через штатный compare_copies.py, чтобы правила совпадали с локальной
сверкой копий. Данные прогона и бэкапы (.env, position.json, memory.json,
bot.log, *.bak, *.log, drift_report.txt, каталог _bak) в версию не входят и
расхождением не считаются.

Эталон — зеркало git на самом сервере (`~/bybit-drift/mirror.git`), куда
локальная копия отправляет свой HEAD при каждом пуше (ci/publish_mirror.sh).
Поэтому проверка не зависит ни от GitHub, ни от того, включён ли доступ с
сервера наружу: SSH до сервера уже есть, а зеркало лежит локально.

Запуск (обычно из таймера deploy/drift-check.timer):

    python drift_check.py                     # эталон — локальное зеркало
    python drift_check.py --local /путь       # эталон — обычная папка
    python drift_check.py --repo URL --ref main   # эталон — любой git (напр. GitHub)
    python drift_check.py --no-alert          # без Telegram
    python drift_check.py --json              # машинный вывод

Коды возврата:
    0 — сервер совпадает с эталоном;
    1 — расхождения (отчёт записан);
    2 — не удалось получить эталон (сеть, git, права);
    3 — неверные пути.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time

# Консоль Windows по умолчанию cp1252 — отчёт и сообщения на русском.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import compare_copies as C  # noqa: E402

# Зеркало, куда локальная копия пушит свой HEAD (ci/publish_mirror.sh).
# Путь локальный — git fetch работает без сети и без ключей.
DEFAULT_REPO = os.path.join(os.path.expanduser("~"), "bybit-drift", "mirror.git")
DEFAULT_REF = "main"
DEFAULT_CACHE = os.path.join(os.path.expanduser("~"), ".cache", "bybit-drift", "repo.git")
DEFAULT_REPORT = os.path.join(HERE, "drift_report.txt")

# Эталон старше этого срока — предупреждаем: сверка сравнивает со старым
# срезом кода, и «всё совпадает» будет неправдой.
STALE_HOURS = 168

FETCH_TIMEOUT = 180          # секунд на git fetch: медленная сеть — не повод висеть


# --------------------------------------------------------------- эталон (git)
def _git_env(identity: str | None) -> dict:
    env = dict(os.environ)
    env.setdefault("GIT_TERMINAL_PROMPT", "0")   # не ждать ввода пароля в таймере
    if identity:
        env["GIT_SSH_COMMAND"] = (
            f"ssh -i {shlex.quote(identity)} -o IdentitiesOnly=yes "
            f"-o StrictHostKeyChecking=accept-new -o BatchMode=yes")
    return env


def fetch_reference(repo: str, ref: str, cache: str, dest: str,
                    identity: str | None = None) -> str:
    """Выкачать ветку ref из repo в dest. Возврат — sha коммита.

    Кэш — bare-репозиторий: повторный запуск забирает только новое
    (--depth 1), поэтому ежедневная проверка не тянет всю историю.
    """
    env = _git_env(identity)
    os.makedirs(cache, exist_ok=True)
    subprocess.run(["git", "init", "--bare", "--quiet", cache],
                   capture_output=True, text=True, check=True)

    res = subprocess.run(["git", "-C", cache, "fetch", "--quiet", "--depth", "1",
                          repo, ref],
                         capture_output=True, text=True, env=env,
                         timeout=FETCH_TIMEOUT)
    if res.returncode != 0:
        detail = (res.stderr or res.stdout or "").strip().splitlines()
        raise RuntimeError("git fetch не удался: "
                           + (detail[-1] if detail else f"код {res.returncode}"))

    sha = subprocess.run(["git", "-C", cache, "rev-parse", "FETCH_HEAD"],
                         capture_output=True, text=True, check=True).stdout.strip()

    # Распаковка потоком: архив в память не читаем (код бота — сотни КБ, но
    # не будем зависеть от размера).
    proc = subprocess.Popen(["git", "-C", cache, "archive", "--format=tar",
                             "FETCH_HEAD"], stdout=subprocess.PIPE)
    try:
        with tarfile.open(fileobj=proc.stdout, mode="r|") as tf:
            try:
                tf.extractall(dest, filter="data")    # Python 3.12+
            except TypeError:                          # 3.10/3.11
                tf.extractall(dest)
    finally:
        if proc.stdout:
            proc.stdout.close()
        if proc.wait(timeout=60) != 0:
            raise RuntimeError("git archive завершился с ошибкой")
    return sha


# ------------------------------------------------------------------- сравнение
def collect(ref_dir: str, live_dir: str) -> dict:
    """Сравнить эталон и живой каталог. Возврат — расхождения + вердикт."""
    ref_files, _ = C.scan(ref_dir)
    live_files, _ = C.scan(live_dir)
    diff = C.compare(ref_files, live_files)
    diff["verdict"] = C.verdict(diff)
    return diff


def has_drift(diff: dict) -> bool:
    return bool(diff["only_a"] or diff["only_b"] or diff["differ"])


def fingerprint(diff: dict) -> str:
    """Отпечаток набора расхождений: чтобы не слать один и тот же алерт."""
    items = (["+" + p for p in diff["only_a"]]
             + ["-" + p for p in diff["only_b"]]
             + ["~" + d["file"] for d in diff["differ"]])
    return hashlib.sha1("\n".join(sorted(items)).encode("utf-8")).hexdigest()[:16]


def build_report(diff: dict, *, ref_dir: str, live_dir: str, repo: str,
                 ref: str, sha: str) -> str:
    out = [
        "Сверка кода: сервер и репозиторий разошлись",
        f"Когда: {time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"Эталон: {repo} (ветка {ref}, коммит {sha[:12]})",
        f"Сервер: {live_dir}",
        "",
        "=== Файлы проекта (это и есть версия) ===",
        f"  совпадает: {diff['same']}",
        f"  только в репозитории: {len(diff['only_a'])}",
        f"  только на сервере: {len(diff['only_b'])}",
        f"  отличаются содержимым: {len(diff['differ'])}",
    ]
    if diff["only_a"]:
        out += [f"    + {p}   ← нет на сервере, разверните" for p in diff["only_a"]]
    if diff["only_b"]:
        out += [f"    - {p}   ← есть только на сервере (правка мимо git?)"
                for p in diff["only_b"]]
    if diff["differ"]:
        out.append("")
        for d in diff["differ"]:
            where = ("новее на сервере — правка мимо git"
                     if d["newer"] == "A" else
                     "новее в репозитории — сервер не развёрнут")
            out.append(f"    ~ {d['file']}: {where} "
                       f"(сервер {C.fmt_ts(d['b'])} | репозиторий {C.fmt_ts(d['a'])})")
    out += [
        "",
        f"ВЕРДИКТ: {diff['verdict']}",
        "",
        "Данные прогона и бэкапы не сравниваются: .env, position.json,",
        "memory.json, bot.log, *.bak, *.log, drift_report.txt, каталог _bak.",
        "Перенос кода на сервер: bash deploy/deploy_vps.sh (или git pull).",
        "",
    ]
    return "\n".join(out)


def payload(diff: dict, *, repo: str, ref: str, sha: str, live_dir: str) -> dict:
    return {
        "ok": not has_drift(diff),
        "verdict": diff["verdict"],
        "repo": repo,
        "ref": ref,
        "commit": sha,
        "dir": os.path.abspath(live_dir),
        "same": diff["same"],
        "only_repo": diff["only_a"],
        "only_server": diff["only_b"],
        "differ": [{k: d[k] for k in ("file", "newer")}
                   for d in diff["differ"]],
        "checked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


def _warn_if_stale(repo: str, ref: str, cache: str) -> None:
    """Предупредить, если эталон давно не обновлялся.

    Зеркало обновляет локальная копия при пуше. Если она месяц не пушила,
    сверка сравнивает сервер со старым срезом и может говорить «совпадает»
    про код, которого уже нет в работе — об этом надо сказать вслух.
    """
    try:
        raw = subprocess.run(["git", "-C", cache, "log", "-1", "--format=%ct",
                              "FETCH_HEAD"], capture_output=True, text=True)
        ts = int((raw.stdout or "0").strip() or 0)
    except (ValueError, OSError):
        return
    age_h = (time.time() - ts) / 3600.0 if ts else 0.0
    if age_h > STALE_HOURS:
        print(f"ВНИМАНИЕ: эталон обновлён {age_h / 24:.0f} дн. назад — "
              f"возможно, локальная копия давно не пушила (ci/publish_mirror.sh)",
              file=sys.stderr)


# ------------------------------------------------------------------ уведомление
def load_env(path: str) -> None:
    """Прочитать .env проекта (как healthcheck): секреты нужны для Telegram."""
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, val = line.split("=", 1)
                os.environ.setdefault(key.strip(), val.strip().strip("'\""))
    except OSError:
        pass


def notify(text: str) -> bool:
    """Telegram молча, если не настроено или сбой сети: чекер не должен падать."""
    try:
        import healthcheck as H
        return bool(H.send_telegram(
            os.environ.get("TELEGRAM_BOT_TOKEN", "").strip(),
            os.environ.get("TELEGRAM_CHAT_ID", "").strip(), text))
    except Exception:  # noqa: BLE001 — уведомление не критично
        return False


def alert_text(diff: dict, sha: str, report_path: str) -> str:
    bits = []
    if diff["only_a"]:
        bits.append(f"+{len(diff['only_a'])} в репозитории")
    if diff["only_b"]:
        bits.append(f"-{len(diff['only_b'])} только на сервере")
    if diff["differ"]:
        bits.append(f"~{len(diff['differ'])} отличаются")
    return ("⚠️ Код на сервере разошёлся с репозиторием "
            f"({sha[:7]})\n{diff['verdict']}\n" + ", ".join(bits)
            + f"\nОтчёт: {report_path}")


# ------------------------------------------------------------------------ main
def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Сверить код на сервере с репозиторием и записать отчёт "
                    "при расхождении.")
    p.add_argument("--dir", default=HERE,
                   help=f"каталог проекта на сервере (default {HERE})")
    p.add_argument("--repo", default=DEFAULT_REPO,
                   help=f"git-эталон: путь к зеркалу или URL (default {DEFAULT_REPO})")
    p.add_argument("--ref", default=DEFAULT_REF,
                   help=f"ветка эталона (default {DEFAULT_REF})")
    p.add_argument("--cache", default=DEFAULT_CACHE,
                   help="кэш git-репозитория (default ~/.cache/bybit-drift/repo.git)")
    p.add_argument("--identity", default=None,
                   help="SSH-ключ, если эталон — удалённый git по ssh")
    p.add_argument("--local", default=None,
                   help="эталон — обычная папка (без git); для локальной проверки")
    p.add_argument("--report", default=DEFAULT_REPORT,
                   help=f"файл отчёта (default {DEFAULT_REPORT})")
    p.add_argument("--json", action="store_true", help="машинный вывод")
    p.add_argument("--no-alert", action="store_true", help="не отправлять в Telegram")
    p.add_argument("--alert-every", type=float, default=24.0,
                   help="не повторять алерт про тот же набор расхождений "
                        "раньше, чем через N часов (default 24)")
    p.add_argument("--quiet", action="store_true",
                   help="печатать только при расхождении (для таймера)")
    args = p.parse_args(argv)

    live_dir = os.path.abspath(args.dir)
    if not os.path.isdir(live_dir):
        print(f"нет каталога: {live_dir}", file=sys.stderr)
        return 3

    tmp = None
    try:
        if args.local:
            ref_dir = os.path.abspath(args.local)
            if not os.path.isdir(ref_dir):
                print(f"нет каталога-эталона: {ref_dir}", file=sys.stderr)
                return 3
            sha, repo, ref = "local", ref_dir, "-"
        else:
            tmp = tempfile.mkdtemp(prefix="drift-ref-")
            identity = args.identity if (args.identity
                                         and os.path.exists(args.identity)) else None
            sha = fetch_reference(args.repo, args.ref, args.cache, tmp, identity)
            ref_dir, repo, ref = tmp, args.repo, args.ref
            _warn_if_stale(args.repo, args.ref, args.cache)

        diff = collect(ref_dir, live_dir)
        drift = has_drift(diff)

        if drift:
            text = build_report(diff, ref_dir=ref_dir, live_dir=live_dir,
                                repo=repo, ref=ref, sha=sha)
            with open(args.report, "w", encoding="utf-8") as f:
                f.write(text)
            if not args.quiet:
                print(text)
            else:
                print(f"РАСХОЖДЕНИЕ: {diff['verdict']} (отчёт: {args.report})")
        else:
            # старый отчёт не должен пережить успешную проверку
            try:
                os.remove(args.report)
            except FileNotFoundError:
                pass
            if not args.quiet:
                print(f"Код на сервере совпадает с эталоном "
                      f"({diff['same']} файлов проекта).")

        if args.json:
            print(json.dumps(payload(diff, repo=repo, ref=ref, sha=sha,
                                     live_dir=live_dir), ensure_ascii=False,
                             indent=2))

        if drift and not args.no_alert:
            _maybe_alert(diff, sha, args)
        return 1 if drift else 0

    except (RuntimeError, subprocess.SubprocessError, OSError) as e:
        print(f"не удалось получить эталон: {e}", file=sys.stderr)
        return 2
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)


def _maybe_alert(diff: dict, sha: str, args) -> None:
    """Алерт при новом наборе расхождений — или если о нём молчали сутки."""
    state_path = os.path.join(os.path.dirname(os.path.abspath(args.cache)),
                              "last_alert.json")
    fp = fingerprint(diff)
    last: dict = {}
    try:
        with open(state_path, encoding="utf-8") as f:
            last = json.load(f)
    except (OSError, ValueError):
        last = {}

    fresh = last.get("fingerprint") != fp
    stale = time.time() - float(last.get("at", 0)) > args.alert_every * 3600
    if fresh or stale:
        load_env(os.path.join(os.path.abspath(args.dir), ".env"))
        if notify(alert_text(diff, sha, args.report)):
            os.makedirs(os.path.dirname(state_path), exist_ok=True)
            with open(state_path, "w", encoding="utf-8") as f:
                json.dump({"fingerprint": fp, "at": time.time()}, f)


if __name__ == "__main__":
    sys.exit(main())
