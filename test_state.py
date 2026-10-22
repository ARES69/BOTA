"""Тесты целостности position.json: атомарная запись, битый файл не роняет
бота, потеря состояния блокирует новый вход (иначе можно открыть пару поверх
потерянной позиции на бирже).
Запуск: python test_state.py  (stdlib, без pytest)."""
from __future__ import annotations

import json
import os
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
import funding_executor as fe  # noqa: E402

d = tempfile.mkdtemp()
fe.STATE_FILE = os.path.join(d, "position.json")
fe.LOG_FILE = os.path.join(d, "bot.log")

ok = True


def check(name, cond):
    global ok
    print(("OK   " if cond else "FAIL ") + name)
    ok = ok and bool(cond)


# 1. атомарность: tmp не остаётся, файл цел
st = {"position": {"symbol": "BTCUSDT"}, "history": []}
fe.save_state(st)
check("save_state отработал", json.load(open(fe.STATE_FILE))["position"]["symbol"] == "BTCUSDT")
check("tmp-файла не осталось", not os.path.exists(fe.STATE_FILE + ".tmp"))

# 2. битый state -> .bak + пустое состояние + флаг (без исключения)
fe.STATE_LOST.clear()
with open(fe.STATE_FILE, "w", encoding="utf-8") as f:
    f.write('{"position": {"symbol": "BTCUSDT"}, "hist')  # обрыв
st2 = fe.load_state()
# состояние теперь портфельное: пусто = position None + пустой positions[]
check("битый файл не роняет load_state",
      st2["position"] is None and fe.positions_of(st2) == [])
check("сохранён .bak", os.path.exists(fe.STATE_FILE + ".bak"))
check("поднят флаг STATE_LOST", len(fe.STATE_LOST) == 1)

# 3. валидный файл -> флаг НЕ поднимается
fe.STATE_LOST.clear()
fe.save_state({"position": None, "history": []})
fe.load_state()
check("на валидном файле флаг не поднимается", len(fe.STATE_LOST) == 0)

# 4. JSON вместо объекта (список) -> тоже защита
fe.STATE_LOST.clear()
with open(fe.STATE_FILE, "w", encoding="utf-8") as f:
    json.dump([1, 2, 3], f)
st3 = fe.load_state()
check("список вместо объекта не ломает", st3.get("position") is None and len(fe.STATE_LOST) == 1)

# 5. вход заблокирован при потерянном состоянии
with open(fe.STATE_FILE, "w", encoding="utf-8") as f:
    f.write('{"position": null, "hist')
fe.STATE_LOST.clear()
fe.load_state()          # файл снова битый -> флаг поднят
try:
    fe.cmd_enter(type("C", (), {"mode": "paper", "symbol": None, "force": True,
                                "capital": 1000, "leverage": 2, "preset": "hold",
                                "no_learn": True, "fast_apy": 3, "min_basis": 0,
                                "pass_oi": 20, "min_volume": 3, "min_oi": 3,
                                "spot_fee": 0.001, "perp_taker_fee": 0.00055,
                                "perp_maker_fee": 0.0002, "confirm_live": False,
                                "max_spread": 0.5, "min_depth": 1.0,
                                "no_notify": True, "taker_only": False})())
    check("вход заблокирован", False)
except SystemExit as e:
    check("вход заблокирован при битом state", "повреждён" in str(e))

print()
print("Итог:", "все проверки пройдены" if ok else "ЕСТЬ ПРОВАЛЫ")
sys.exit(0 if ok else 1)