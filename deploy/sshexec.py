"""SSH-раннер для деплоя (нужен только paramiko, в сам бот не входит).

Использование:
    VPS_PASS=... python deploy/sshexec.py run  "команда"        # выполнить на VPS
    VPS_PASS=... python deploy/sshexec.py scp  "локальный" "удалённый"
    VPS_PASS=... python deploy/sshexec.py putd "src_dir" "dst_dir"  # рекопия папки

Вход: по ключу (по умолчанию) или по паролю. Ключ — ~/.ssh/id_ed25519_bybit
(путь меняется env VPS_KEY). Если ключа нет или он не принят сервером,
вход идёт по паролю из env VPS_PASS. Пароль в код и аргументы не пишется.

Хост/пользователь: --host 45.9.18.57 --user root (или env VPS_HOST/VPS_USER).
"""
import argparse
import os
import posixpath
import sys
from pathlib import Path

# окно/консоль Windows по умолчанию cp1252 — не должен падать на кириллице
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

import paramiko

HOST = os.environ.get("VPS_HOST", "45.9.18.57")
USER = os.environ.get("VPS_USER", "root")
KEY = os.environ.get(
    "VPS_KEY", str(Path.home() / ".ssh" / "id_ed25519_bybit"))


def client():
    """Подключение: сначала ключ, при неудаче — пароль из VPS_PASS."""
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    if os.path.isfile(KEY):
        try:
            c.connect(HOST, 22, USER, key_filename=KEY,
                      timeout=20, banner_timeout=20, auth_timeout=20,
                      look_for_keys=False, allow_agent=False)
            return c
        except Exception as e:  # noqa: BLE001 — падаем на пароль
            print(f"Ключ {KEY} не принят ({type(e).__name__}), "
                  f"пробую пароль", file=sys.stderr)
            c.close()
            c = paramiko.SSHClient()
            c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    pw = os.environ.get("VPS_PASS")
    if not pw:
        sys.exit("Нет VPS_PASS в окружении и ключ не подошёл "
                 f"({KEY})")
    try:
        c.connect(HOST, 22, USER, pw, timeout=20, banner_timeout=20)
    except Exception as e:  # noqa: BLE001 — своё сообщение вместо трассировки
        c.close()
        sys.exit(f"Не удалось подключиться по паролю: "
                 f"{type(e).__name__}: {e}")
    return c


def run(c, cmd: str) -> int:
    _, out, err = c.exec_command(cmd, timeout=300)
    o, e = out.read().decode(errors="replace"), err.read().decode(errors="replace")
    if o:
        print(o, end="")
    if e:
        print(e, end="", file=sys.stderr)
    return out.channel.recv_exit_status()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=["run", "scp", "putd"])
    ap.add_argument("args", nargs="+")
    a = ap.parse_args()
    c = client()
    try:
        if a.action == "run":
            sys.exit(run(c, a.args[0]))
        s = c.open_sftp()
        if a.action == "scp":
            s.put(a.args[0], a.args[1])
            print(f"copied: {a.args[0]} -> {a.args[1]}")
        elif a.action == "putd":
            src, dst = a.args[0], a.args[1]
            try:
                s.stat(dst)
            except IOError:
                s.mkdir(dst)
            for name in sorted(os.listdir(src)):
                sp = os.path.join(src, name)
                if not os.path.isfile(sp):
                    continue
                s.put(sp, posixpath.join(dst, name))
                print(f"copied: {sp} -> {posixpath.join(dst, name)}")
    finally:
        c.close()


if __name__ == "__main__":
    main()
