"""Тесты биржевого стопа на перп-ногу: расчёт цены для шорта, запас до
ликвидации, отказ не ломает вход, снятие стопа при выходе.
Запуск: python test_exchange_stop.py  (stdlib, без pytest)."""
from __future__ import annotations

import os
import sys
import types
from decimal import Decimal

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

# Тестовые ветки пишут через fe.log в рабочий bot.log: после прогона в лог
# бота попадали тестовые строки. Подменяем путь лога на время теста и
# возвращаем/удаляем временный файл при выходе (в т.ч. при исключении).
import atexit
import tempfile

_orig_log = fe.LOG_FILE
_tf = tempfile.NamedTemporaryFile("w", suffix=".log", delete=False)
_tf.close()
fe.LOG_FILE = _tf.name


def _restore_log():
    fe.LOG_FILE = _orig_log
    try:
        os.remove(_tf.name)
    except OSError:
        pass


atexit.register(_restore_log)


def check(name, cond):
    print(("OK   " if cond else "FAIL ") + name)
    return bool(cond)


ok = True


def pos_of(entry=75000.0, qty=0.008, margin=300.0, **over):
    p = {"symbol": "BTCUSDT", "perp_entry": entry, "perp_qty": qty,
         "margin": margin}
    p.update(over)
    return p


# --- расчёт цены стопа -------------------------------------------------
# 70% маржи = 210$ убытка; на 600$ нотиона это +35% цены
p = pos_of()
sl = fe.stop_loss_price(p, 70.0)
loss_at_sl = (sl - 75000.0) * 0.008
ok &= check("стоп выше входа (мы в шорте)", sl > 75000.0)
ok &= check("убыток до стопа ≈ 70% маржи",
            abs(loss_at_sl - 300.0 * 0.70) < 0.01)
ok &= check("стоп до ликвидации (убыток < 100% маржи)",
            loss_at_sl < 300.0)

# риск 90% — всё ещё до ликвидации
sl90 = fe.stop_loss_price(pos_of(), 90.0)
ok &= check("риск 90%: убыток ≈ 90% маржи, не ликвидация",
            abs((sl90 - 75000.0) * 0.008 - 270.0) < 0.01)

# нулевой риск / мусорные входные → 0 (стоп не ставим)
ok &= check("риск=0 → стоп не выставляется", fe.stop_loss_price(pos_of(), 0) == 0.0)
ok &= check("вход=0 → стоп не выставляется",
            fe.stop_loss_price(pos_of(entry=0), 70.0) == 0.0)
ok &= check("маржа=0 → стоп не выставляется",
            fe.stop_loss_price(pos_of(margin=0), 70.0) == 0.0)

# шум рынка: стоп не ближе 0.5% (иначе выбивает мгновенно)
tiny = pos_of(entry=1.0, qty=100000.0, margin=100.0)
sl_tiny = fe.stop_loss_price(tiny, 0.1)
ok &= check("минимальный отступ 0.5% от входа",
            sl_tiny >= 1.0 * 1.005 - 1e-12)

# --- arm_exchange_stop: отказ не ломает вход ---------------------------
class FailClient:
    def set_trading_stop(self, *a, **k):
        raise RuntimeError("110007 ab not enough")

p_fail = pos_of()
fe.price_tick = lambda s, c: Decimal("0.1")
got = fe.arm_exchange_stop(FailClient(), p_fail, types.SimpleNamespace(max_margin_risk=70.0))
ok &= check("отказ биржи не бросает исключение", got == 0.0)
ok &= check("помечено stop_armed=False", p_fail.get("stop_armed") is False)

# успешная выставка → цена в позиции, округлена к тику
class OkClient:
    def __init__(self):
        self.calls = []
    def set_trading_stop(self, symbol, stop_loss=0, take_profit=0,
                         trigger_by="MarkPrice"):
        self.calls.append((symbol, stop_loss, take_profit))
        return {}

c = OkClient()
p_ok = pos_of()
cli = types.SimpleNamespace(max_margin_risk=70.0)
got2 = fe.arm_exchange_stop(c, p_ok, cli)
ok &= check("стоп выставлен и записан", got2 > 0 and p_ok.get("stop_price") == got2
            and p_ok.get("stop_armed") is True)
ok &= check("на биржу ушла цена, TP=0", len(c.calls) == 1
            and c.calls[0][1] == got2 and c.calls[0][2] == 0)
ok &= check("цена кратна тику 0.1",
            abs(got2 / 0.1 - round(got2 / 0.1)) < 1e-9)

# --- клиент: тело запроса trading-stop корректно -----------------------
class Capture(fe.BybitClient):
    def __init__(self):
        self.body = None
        self.method = None
        self.path = None

    def request(self, method, path, params=None, body=None):
        self.method, self.path, self.body = method, path, body
        return {}


cc = Capture()
cc.set_trading_stop("ETHUSDT", stop_loss=2500.0)
b = cc.body
ok &= check("POST /v5/position/trading-stop",
            cc.method == "POST" and cc.path == "/v5/position/trading-stop")
ok &= check("tpslMode=Full, positionIdx=0 (one-way)",
            b["tpslMode"] == "Full" and b["positionIdx"] == 0)
ok &= check("symbol/category верны",
            b["symbol"] == "ETHUSDT" and b["category"] == "linear")
ok &= check("stopLoss задан, takeProfit=0",
            float(b["stopLoss"]) == 2500.0 and float(b["takeProfit"]) == 0.0)
ok &= check("строки, а не числа (биржа ждёт string)",
            isinstance(b["stopLoss"], str) and isinstance(b["takeProfit"], str))

# снятие: stop_loss=0
cc.set_trading_stop("ETHUSDT", stop_loss=0)
ok &= check("снятие стопа = stopLoss 0", float(cc.body["stopLoss"]) == 0.0)

print()
print("Итог:", "все проверки пройдены" if ok else "ЕСТЬ ПРОВАЛЫ")
sys.exit(0 if ok else 1)