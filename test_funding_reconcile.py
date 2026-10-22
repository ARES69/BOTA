"""Тесты сверки расчётного funding с фактическим (transaction-log).
Запуск: python test_funding_reconcile.py  (stdlib, без pytest)."""
from __future__ import annotations

import os
import sys
import tempfile
import types

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

NOW = 1_800_000_000_000


def check(name, cond):
    print(("OK   " if cond else "FAIL ") + name)
    return bool(cond)


def mk_pos(**over):
    pos = {"mode": "testnet", "symbol": "BTCUSDT", "spot_symbol": "BTCUSDT",
           "mult": 1.0, "notional": 600.0, "margin": 300.0, "leverage": 2.0,
           "spot_qty": 0.008, "perp_qty": 0.008, "spot_entry": 75000.0,
           "perp_entry": 75000.0, "fees_usd": 0.93,
           "exit_spot_fee_pct": 0.1, "exit_perp_fee_pct": 0.055,
           "entry_cost_pct": 0.31, "apy30": 15.0, "basis_entry": 0.0,
           "opened_ms": NOW - 20 * 3_600_000, "funding_accrued": 1.20,
           "payouts": 3, "neg_streak": 0, "capital": 1000.0,
           "last_funding_ms": NOW - 50 * 3_600_000}
    pos.update(over)
    return pos


class FakeClient:
    """Клиент с заранее заданным ответом transaction-log."""

    def __init__(self, flows):
        self.flows = flows
        self.calls = 0

    def flows_since(self, symbol, since_ms, currency="USDT"):
        self.calls += 1
        return self.flows

    def position_qty(self, symbol):
        return 0.008

    def coin_available(self, coin):
        return 0.0079

    def request(self, m, p, params=None, body=None):
        return {"list": [{"createdTime": NOW, "cumExecQty": "0.008",
                          "cumExecValue": "600"}]}


def run_close(pos, flows):
    """Прогоняет cmd_close на моках, возвращает запись из history."""
    d = tempfile.mkdtemp()
    fe.STATE_FILE = os.path.join(d, "position.json")
    fe.LOG_FILE = os.path.join(d, "bot.log")
    fe.memory = None
    import json
    with open(fe.STATE_FILE, "w", encoding="utf-8") as f:
        json.dump({"position": pos, "history": []}, f)
    client = FakeClient(flows)
    fe.make_client = lambda m: client
    fe.fill_with_slippage = lambda *a, **k: 0.008
    fe.notify = lambda *a, **k: None
    fe.market_api_get = lambda p, q, mode="paper": {"list": []}
    cli = types.SimpleNamespace(mode="paper", preset="hold")
    fe.cmd_close(cli)
    with open(fe.STATE_FILE, encoding="utf-8") as f:
        return json.load(f)["history"][-1]


ok = True

# 1. факт совпадает с расчётом → расхождения нет
rec = run_close(mk_pos(), {"funding": 1.20, "fee": 0.0, "n": 3})
ok &= check("совпадение → funding_fact записан",
            rec.get("funding_fact") == 1.2 and rec.get("funding_diff") == 0.0)

# 2. фактическое расхождение → помечается
rec = run_close(mk_pos(), {"funding": 0.90, "fee": 0.0, "n": 3})
ok &= check("расхождение 0.3$ помечено как funding_diff",
            abs(rec.get("funding_diff", 0) - (-0.30)) < 1e-6)

# 3. биржа недоступна (None) → сверка молчит, сделка пишется
rec = run_close(mk_pos(), None)
ok &= check("нет данных о бирже → сверка пропущена, сделка записана",
            "funding_fact" not in rec and rec["action"] == "close")

# 4. ни одной выплаты (n=0) → сверка не выполняется (окно пустое не ошибка)
rec = run_close(mk_pos(), {"funding": 0.0, "fee": 0.0, "n": 0})
ok &= check("ноль выплат → без сверки", "funding_fact" not in rec)

# 5. допуск: маленькое расхождение не считается проблемой
rec = run_close(mk_pos(funding_accrued=1.20), {"funding": 1.26, "fee": 0.0, "n": 3})
ok &= check("разница 0.06 (5% от 1.20) — в допуске",
            abs(rec.get("funding_diff", 0) - 0.06) < 1e-6)

# 6. flows_since разбирает ответ биржи (SETTLEMENT/TRADE)
class C(fe.BybitClient):
    def __init__(self):
        pass
    def request(self, m, p, params=None, body=None):
        return {"list": [
            {"funding": "-0.003676", "fee": "0"},
            {"funding": "", "fee": "0.01908720"},
            {"funding": "0.10", "fee": ""},
            {"funding": None, "fee": "0.005"},
        ]}
r = C().flows_since("XRPUSDT", 0)
ok &= check("flows_since: funding суммируется, fee суммируется",
            abs(r["funding"] - (0.10 - 0.003676)) < 1e-9
            and abs(r["fee"] - (0.01908720 + 0.005)) < 1e-9
            and r["n"] == 2)

# 7. битый ответ биржи → None, а не исключение
class Broken(fe.BybitClient):
    def __init__(self):
        pass
    def request(self, *a, **k):
        raise RuntimeError("boom")
ok &= check("сбой запроса → None", Broken().flows_since("X", 0) is None)

# 8. не-числовые поля не роняют разбор
class Junk(fe.BybitClient):
    def __init__(self):
        pass
    def request(self, m, p, params=None, body=None):
        return {"list": [{"funding": "abc", "fee": None},
                         {"funding": "0.5", "fee": "0.1"}]}
r2 = Junk().flows_since("X", 0)
ok &= check("мусор в полях пропускается", abs(r2["funding"] - 0.5) < 1e-9)

print()
print("Итог:", "все проверки пройдены" if ok else "ЕСТЬ ПРОВАЛЫ")
sys.exit(0 if ok else 1)