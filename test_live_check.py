"""ИНТЕГРАЦИОННЫЙ тест на ЖИВОЙ testnet-бирже: открыть реальную позицию,
прогнать health-check (сверка position.json с биржей) и закрыть её — одной
командой.

    LIVE_CHECK=1 python test_live_check.py
    LIVE_CHECK=1 python test_live_check.py --symbol ETHUSDT --capital 200

Сценарий (всё по-настоящему, через те же CLI-команды, что и у пользователя):
  1. убедиться, что позиции нет (тест НИКОГДА не трогает уже открытую);
  2. `funding_executor.py enter --mode testnet` — открыть пару;
  3. `healthcheck.py` — сверка должна сказать «совпадает» (проблем=0);
  4. намеренно испортить position.json → healthcheck должен поймать
     расхождение и подсказать `close`;
  5. `funding_executor.py close --mode testnet` — закрыть позицию;
  6. финальный healthcheck — проблем=0, биржа флэт.

Безопасность и изоляция (почему это НЕ ломает боевой бот):
  • тест запускается ТОЛЬКО при LIVE_CHECK=1 (или --live) — обычный прогон
    пачки test_*.py его пропускает: он ставит ЖИВЫЕ ордера на testnet;
  • нет testnet-ключей (или биржа недоступна) → SKIP, а не падение;
  • уже открытая позиция в position.json → отказ (тест не перехватывает
    чужую/боевую позицию);
  • funding-watch на время теста best-effort останавливается (иначе бот
    борется за position.json) и возвращается в конце — как и был;
  • гарантированная уборка в finally: испорченный json чинится, позиция
    закрывается, временный файл состояния health-check удаляется;
  • health-check зовётся с `--dry-run` и отдельным `--state` — Telegram
    НЕ спамится, боевой `health_state.json` не затирается.

Возврат: 0 — все шаги прошли; 1 — провал; 0 со словом SKIP — не запускался.
Запуск: python test_live_check.py  (stdlib, без pytest)."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
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
POS = os.path.join(HERE, "position.json")
ENV = os.path.join(HERE, ".env")
HC_STATE = os.path.join(HERE, "livecheck_health_state.json")

SERVICE = "funding-watch"


def check(name, cond) -> bool:
    print(("OK   " if cond else "FAIL ") + name)
    return bool(cond)


# ------------------------------------------------------------------ helpers
def run(args, timeout: int = 240) -> tuple[int, str]:
    """Запустить CLI-команду в папке теста, вернуть (rc, вывод).
    Вывод питон-скриптов в UTF-8 (эмодзи в логах) — читаем UTF-8."""
    res = subprocess.run(args, cwd=HERE, capture_output=True, text=True,
                         encoding="utf-8", errors="replace", timeout=timeout)
    return res.returncode, (res.stdout or "") + (res.stderr or "")


def read_pos() -> dict | None:
    try:
        with open(POS, encoding="utf-8") as f:
            return (json.load(f) or {}).get("position")
    except (OSError, ValueError):
        return None


def write_pos_raw(data: bytes) -> None:
    with open(POS, "wb") as f:
        f.write(data)


def has_testnet_keys() -> bool:
    """Ключи testnet из окружения или .env (значения не печатаем)."""
    if (os.environ.get("BYBIT_TESTNET_API_KEY", "").strip()
            and os.environ.get("BYBIT_TESTNET_API_SECRET", "").strip()):
        return True
    if os.path.exists(ENV):
        with open(ENV, encoding="utf-8") as f:
            keys = {ln.split("=", 1)[0].strip()
                    for ln in f if "=" in ln and not ln.strip().startswith("#")}
        return ("BYBIT_TESTNET_API_KEY" in keys
                and "BYBIT_TESTNET_API_SECRET" in keys)
    return False


def service_active(name: str) -> bool:
    try:
        r = subprocess.run(["systemctl", "is-active", name],
                           capture_output=True, text=True, timeout=10)
        return r.stdout.strip() == "active"
    except Exception:  # noqa: BLE001 — нет systemd: считаем, что сервиса нет
        return False


def service_verb(verb: str, name: str) -> None:
    try:
        subprocess.run(["systemctl", verb, name], capture_output=True,
                       text=True, timeout=30)
    except Exception:  # noqa: BLE001 — best-effort, не роняем тест
        pass


def hc(*extra: str) -> tuple[int, str]:
    """health-check без сервисных проверок и без Telegram: только позиция.
    --log на заведомо существующий файл + --max-log-age 0 отключают
    проверку свежести лога, чтобы итог зависел ТОЛЬКО от сверки позиции."""
    return run([sys.executable, os.path.join(HERE, "healthcheck.py"),
                "--services", "", "--no-autoheal", "--max-log-age", "0",
                "--no-bind-check",
                "--log", os.path.abspath(__file__),
                "--state", HC_STATE, "--dry-run", "--force", *extra])


# ------------------------------------------------------------------ main
def main() -> int:
    ap = argparse.ArgumentParser(description="Живой интеграционный тест "
                                             "сверки позиции (testnet).")
    ap.add_argument("--live", action="store_true",
                    help="то же, что окружение LIVE_CHECK=1")
    ap.add_argument("--symbol", default=os.environ.get("LIVE_SYMBOL", "ETHUSDT"))
    ap.add_argument("--capital", type=float, default=200.0)
    ap.add_argument("--leverage", type=float, default=2.0)
    args = ap.parse_args()
    # health-check здесь проверяет ТОЛЬКО сверку позиции: без --no-bind-check
    # он полез бы ещё и в bind панели, а публичный dashboard на машине ломал
    # бы шаг «проблем=0» и мог перезапустить чужой сервис.

    # --- предохранители: без явного согласия тест не ставит живые ордера ---
    if os.environ.get("LIVE_CHECK") != "1" and not args.live:
        print("SKIP: живой тест на бирже — запустите LIVE_CHECK=1 "
              "python test_live_check.py (или --live).")
        return 0
    if not has_testnet_keys():
        print("SKIP: нет testnet-ключей (BYBIT_TESTNET_API_KEY/SECRET) "
              "в окружении или .env — сверку на бирже не проверить.")
        return 0
    if read_pos() is not None:
        print("STOP: в position.json уже открыта позиция — тест её НЕ трогает. "
              "Закройте вручную (close) и повторите.")
        return 1

    ok = True
    good: bytes | None = None        # корректный position.json сразу после входа
    stopped = False                  # останавливали ли мы funding-watch
    print(f"=== живой тест сверки: {args.symbol} testnet, "
          f"капитал ${args.capital:g}, плечо {args.leverage:g}x ===")

    # бот не должен бороться за position.json, пока тест рулит позицией
    if service_active(SERVICE):
        service_verb("stop", SERVICE)
        stopped = True
        print(f"  (на время теста остановлен {SERVICE})")

    try:
        # --- 1. открыть живую позицию --------------------------------------
        rc, out = run([sys.executable, os.path.join(HERE, "funding_executor.py"),
                       "enter", "--mode", "testnet", "--symbol", args.symbol,
                       "--force", "--capital", str(args.capital),
                       "--leverage", str(args.leverage)])
        print(out.rstrip())
        ok &= check("enter: команда успешна", rc == 0 and "ВХОД TESTNET" in out)
        with open(POS, "rb") as f:
            good = f.read()
        pos = read_pos() or {}
        legs_ok = (pos.get("mode") == "testnet"
                   and pos.get("symbol") == args.symbol
                   and float(pos.get("perp_qty") or 0) > 0
                   and float(pos.get("spot_qty") or 0) > 0)
        ok &= check("enter: position.json содержит обе живые ноги", legs_ok)
        if not legs_ok:
            print("!! позиция не открылась корректно — дальше не иду")
            return 1

        # --- 2. health-check: сверка должна совпасть -----------------------
        rc2, out2 = hc()
        print(out2.rstrip())
        ok &= check("health-check: сверка совпадает (проблем=0)",
                    "совпадает" in out2 and "проблем=0" in out2)

        # --- 3. намеренно испортить json → должно поймать расхождение ------
        broken = json.loads(good)
        wrong = round(float(broken["position"]["perp_qty"]) * 3, 8)
        broken["position"]["perp_qty"] = wrong
        with open(POS, "w", encoding="utf-8") as f:
            json.dump(broken, f, ensure_ascii=False, indent=2)
        rc3, out3 = hc()
        print(out3.rstrip())
        ok &= check("health-check: ловит расхождение перп-ноги (≠)", "≠" in out3)
        ok &= check("health-check: exit 1 при расхождении", rc3 == 1)
        ok &= check("health-check: подсказка close в алерте",
                    "💡" in out3 and "close" in out3)

        # --- 4. вернуть корректный json и закрыть позицию ------------------
        write_pos_raw(good)
        rc4, out4 = run([sys.executable, os.path.join(HERE, "funding_executor.py"),
                         "close", "--mode", "testnet"])
        print(out4.rstrip())
        ok &= check("close: команда успешна", rc4 == 0 and "Закрыто" in out4)
        ok &= check("close: position.json пуст (флэт)", read_pos() is None)

        # --- 5. финальный health-check: чисто ------------------------------
        rc5, out5 = hc()
        print(out5.rstrip())
        ok &= check("финальный health-check: проблем=0", "проблем=0" in out5)

    finally:
        # уборка: что-либо из этого могло не доехать при сбое на любом шаге
        if read_pos() is not None:
            print("!! позиция осталась открытой — закрываю в finally")
            if good is not None:
                write_pos_raw(good)
            rc, out = run([sys.executable,
                           os.path.join(HERE, "funding_executor.py"),
                           "close", "--mode", "testnet"])
            print(out.rstrip())
            if read_pos() is not None:
                print("!! НЕ УДАЛОСЬ закрыть позицию — разберите вручную "
                      "(position.json / биржа)")
                ok = False
        if stopped:
            service_verb("start", SERVICE)
            print(f"  ({SERVICE} возвращён в работу)")
        if os.path.exists(HC_STATE):
            os.remove(HC_STATE)

    print("\nИтог: " + ("живой тест пройден" if ok else "ЕСТЬ ПРОВАЛЫ"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
