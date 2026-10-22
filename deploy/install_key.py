"""Ставит локальный SSH-ключ в authorized_keys на VPS (по root-паролю).

Пароль НИКОГДА не печатается и не пишется в историю команд:

    python deploy/install_key.py --ask      # спросит пароль в терминале
    VPS_PASS='ваш-root-пароль' python deploy/install_key.py   # если так удобнее

Ключ берётся из ~/.ssh/id_ed25519_bybit (тот, что сгенерирован для 45.9.18.57).
После успеха ключевой вход работает, и пароль больше не нужен:
    ssh root@45.9.18.57
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# --ask: спросить пароль в терминале. Без флага и без VPS_PASS скрипт
# ничего не ждёт (иначе он зависнет на getpass в неинтерактивном запуске,
# например в CI или при перенаправлении stdin).

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

import paramiko  # noqa: E402

HOST = os.environ.get("VPS_HOST", "45.9.18.57")
USER = os.environ.get("VPS_USER", "root")
KEY = Path(os.environ.get(
    "VPS_KEY",
    str(Path.home() / ".ssh" / "id_ed25519_bybit"),
))


def has_console() -> bool:
    """Есть ли за скриптом настоящая консоль (а не пайп/файл).

    На Windows getpass читает прямо из консоли через msvcrt и игнорирует
    перенаправление stdin — поэтому при перенаправлении он НАВСЕГДА
    зависает, а sys.stdin.isatty() этого не показывает (в Git Bash он
    возвращает True даже для < /dev/null). Надёжный признак — реальный
    дескриптор консоли Windows.
    """
    if os.name == "nt":
        try:
            import ctypes
            k = ctypes.windll.kernel32
            h = k.GetStdHandle(-10)          # STD_INPUT_HANDLE
            mode = ctypes.c_uint32()
            return bool(k.GetConsoleMode(h, ctypes.byref(mode)))
        except Exception:  # noqa: BLE001
            return False
    try:
        return os.isatty(0)
    except Exception:  # noqa: BLE001
        return False


def ask_password() -> str:
    """Пароль с терминала (не печатается и не попадает в историю команд)."""
    import getpass
    return getpass.getpass(f"root-пароль для {USER}@{HOST}: ")


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    pw = os.environ.get("VPS_PASS")
    if not pw:
        if "--ask" not in argv:
            print("Нет VPS_PASS в окружении.")
            print("Запуск:  python deploy/install_key.py --ask")
            print("      или  VPS_PASS='пароль' python deploy/install_key.py")
            return 2
        if not has_console():
            print("За скриптом нет интерактивной консоли — спросить пароль "
                  "нечем (и он бы завис).")
            print("Запуск:  VPS_PASS='пароль' python deploy/install_key.py")
            return 2
        pw = ask_password()
        if not pw:
            print("Пароль не введён.")
            return 2
    if not KEY.is_file():
        print(f"Нет файла ключа: {KEY}")
        return 2
    pub = KEY.with_suffix(".pub").read_text(encoding="utf-8").strip()
    if not pub:
        print("Публичный ключ пустой")
        return 2

    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        c.connect(HOST, 22, USER, pw, timeout=25, banner_timeout=25,
                  auth_timeout=25, look_for_keys=False,
                  allow_agent=False)
    except Exception as e:  # noqa: BLE001
        print(f"Не удалось подключиться по паролю: {type(e).__name__}: {e}")
        return 1

    cmd = (
        "umask 077; mkdir -p ~/.ssh; "
        "touch ~/.ssh/authorized_keys; chmod 700 ~/.ssh; "
        "chmod 600 ~/.ssh/authorized_keys; "
        f"grep -qxF {pub!r} ~/.ssh/authorized_keys || echo {pub!r} "
        ">> ~/.ssh/authorized_keys; "
        "wc -l < ~/.ssh/authorized_keys"
    )
    _, out, err = c.exec_command(cmd, timeout=60)
    o = out.read().decode(errors="replace").strip()
    e = err.read().decode(errors="replace").strip()
    rc = out.channel.recv_exit_status()
    c.close()
    if rc != 0:
        print(f"Ошибка на сервере: {e}")
        return 1
    print(f"OK: публичный ключ добавлен в ~/.ssh/authorized_keys "
          f"({o} ключ(ей) всего).")
    print("Проверка входа по ключу:")
    print("    ssh -o BatchMode=yes root@%s 'echo ДОСТУП_ЕСТЬ'" % HOST)
    return 0


if __name__ == "__main__":
    sys.exit(main())
