"""Тесты фильтра «исполним ли вход»: spot_book_alive (кэш, пустой стакан,
ошибка сети) и отсев неисполнимых кандидатов в deep_check.

Зачем: на testnet спот-стакан есть только у мажоров; без проверки очередь
кандидатов (сортировка по APY) съедалась на отказах «стакан пуст», и бот
часами не доходил до жидкого символа.
Запуск: python test_spot_book.py  (stdlib, без pytest).
"""
from __future__ import annotations

import os
import sys
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

ok = True


def check(name, cond):
    global ok
    print(("OK   " if cond else "FAIL ") + name)
    ok = ok and bool(cond)


CLI = types.SimpleNamespace(mode="testnet", preset="hold", no_learn=True,
                            spot_fee=fe.DEFAULT_SPOT_FEE,
                            perp_taker_fee=fe.DEFAULT_PERP_TAKER_FEE,
                            perp_maker_fee=fe.DEFAULT_PERP_MAKER_FEE,
                            min_apy30=3.5, min_pos=70.0, min_basis=-0.05,
                            max_breakeven=40.0, pass_oi=3.0, fast_apy=2.0,
                            capital=1000.0, leverage=2.0,
                            max_spread=0.0, min_depth=0.0,
                            max_entry_basis_pct=0.5)  # проверки стакана в тесте выключены: deep_check покрыт подменой

calls = {"n": 0}
BOOKS = {
    "LIQ": {"b": [["1.0", "5"]], "a": [["1.1", "5"]]},
    "EMPTY": {"b": [], "a": []},
}
# стаканы с разным базисом спот/перп (живой случай XRP testnet:
# котировка ленты +0.03%, а стаканы дают +3% — вход невыполним)
BOOKS_BY_CAT = {
    ("BASISUSDT", "linear"): {"b": [["1.336", "5"]], "a": [["1.337", "5"]]},
    ("BASISUSDT", "spot"): {"b": [["1.295", "5"]], "a": [["1.299", "5"]]},
    ("FLATUSDT", "linear"): {"b": [["1.336", "5"]], "a": [["1.337", "5"]]},
    ("FLATUSDT", "spot"): {"b": [["1.3359", "5"]], "a": [["1.3361", "5"]]},
}


def fake_orderbook(symbol, category, cli, mode=None):
    calls["n"] += 1
    if symbol == "BOOM":
        raise RuntimeError("сеть легла")
    if symbol == "NOSYM":
        # так testnet отвечает за спот-пару, которой там нет (INJUSDT живой
        # пример: retCode=10001 — это отказ биржи, а не сетевой сбой)
        raise RuntimeError("Bybit API error 10001: params error: symbol invalid")
    if (symbol, category) in BOOKS_BY_CAT:
        return BOOKS_BY_CAT[(symbol, category)]
    return BOOKS[symbol]


fe.orderbook = fake_orderbook
fe._SPOT_BOOK_CACHE.clear()

# 1. живой стакан → True; повторный вызов берёт кэш (без нового запроса)
check("живой стакан → True", fe.spot_book_alive(CLI, "LIQ") is True)
n0 = calls["n"]
check("повторный вызов из кэша (REST не дёрнулся)",
      fe.spot_book_alive(CLI, "LIQ") is True and calls["n"] == n0)

# 2. пустой стакан → False (и тоже кэшируется)
check("пустой стакан → False", fe.spot_book_alive(CLI, "EMPTY") is False)
check("пустой тоже в кэше", calls["n"] == n0 + 1)

# 3. ошибка сети → True (неизвестность не блокирует вход)
check("ошибка сети → True (не блокируем)", fe.spot_book_alive(CLI, "BOOM") is True)
check("ошибка сети НЕ кэшируется (проверим позже свежо)",
      calls["n"] == n0 + 2)
fe.spot_book_alive(CLI, "BOOM")
check("повторная проверка после сбоя снова идёт в REST", calls["n"] == n0 + 3)

# 3b. отказ биржи retCode≠0 (10001 «нет символа на testnet») → False + кэш
check("отказ биржи 10001 → False (стакана нет)",
      fe.spot_book_alive(CLI, "NOSYM") is False)
check("отказ биржи кэшируется как пустой стакан", calls["n"] == n0 + 4)

# 4. None/пустая строка → False без запросов
n0 = calls["n"]
check("спот-пара неизвестна → False", fe.spot_book_alive(CLI, None) is False
      and fe.spot_book_alive(CLI, "") is False)
check("без спот-пары REST не дёргается", calls["n"] == n0)

# 5. TTL: устаревший кэш перепроверяется
fe._SPOT_BOOK_CACHE["LIQ"] = (0.0, False)   # метка «давно»
fe.spot_book_alive(CLI, "LIQ")
check("истёкший TTL → перепроверка", calls["n"] == n0 + 1)

# 6. deep_check исключает кандидата с пустым стаканом
def fake_analyze(t, now_ms):
    return dict(t)


def fake_enrich(r, spot, fargs):
    r["verdict_ok"] = True
    r["spot_symbol"] = r.get("spot_symbol")
    return r


real_analyze, real_enrich, real_quotes = (fe.analyze_symbol, fe.enrich,
                                          fe.fetch_spot_quotes)
fe.analyze_symbol, fe.enrich, fe.fetch_spot_quotes = fake_analyze, fake_enrich, (lambda: {})
fe._SPOT_BOOK_CACHE.clear()
try:
    rows = fe.deep_check(CLI, [
        {"symbol": "EMPTYUSDT", "spot_symbol": "EMPTY"},
        {"symbol": "LIQUSDT", "spot_symbol": "LIQ"},
    ])
finally:
    fe.analyze_symbol, fe.enrich, fe.fetch_spot_quotes = (real_analyze,
                                                          real_enrich,
                                                          real_quotes)
check("deep_check оставил только исполнимого кандидата",
      [r["symbol"] for r in rows] == ["LIQUSDT"])

# 7. not_executable: спред/глубина — та же проверка, что в open_position,
#    но заранее в фильтре (+ кэш EXEC_TTL)
fe._EXEC_CACHE.clear()
cl_calls = {"n": 0}
real_cl = fe.check_liquidity


def fake_cl(cli, sym, category, notional, side):
    cl_calls["n"] += 1
    if sym == "WIDEUSDT":
        return "спред 58.9% больше порога 0.5%"
    return None


fe.check_liquidity = fake_cl
try:
    check("неисполнимая нога → причина из check_liquidity",
          fe.not_executable(CLI, {"symbol": "WIDEUSDT",
                                  "spot_symbol": "WIDEUSDT"})
          == "спред 58.9% больше порога 0.5%")
    n_cl = cl_calls["n"]
    fe.not_executable(CLI, {"symbol": "WIDEUSDT", "spot_symbol": "WIDEUSDT"})
    check("причина кэшируется (EXEC_TTL)", cl_calls["n"] == n_cl)
    check("исполнимый кандидат → None",
          fe.not_executable(CLI, {"symbol": "LIQUSDT",
                                  "spot_symbol": "LIQUSDT"}) is None)
    check("нет спот-пары → причина без запросов",
          fe.not_executable(CLI, {"symbol": "X"}) == "нет спот-пары")
    # нотион проверки — как в open_position (size_legs от капитала)
    seen = []

    def spy_cl(cli, sym, category, notional, side):
        seen.append((category, notional, side))
        return None

    fe.check_liquidity = spy_cl
    fe._EXEC_CACHE.clear()
    fe.not_executable(CLI, {"symbol": "AAAUSDT", "spot_symbol": "AAAUSDT"})
    want_notional = 1000.0 / (1 + 1 / 2.0)   # size_legs(1000, 2)
    check("проверяются обе ноги нотионом size_legs",
          seen == [("linear", want_notional, "bid"),
                   ("spot", want_notional, "ask")])

    # 8. фактический базис по стаканам (гнилая котировка ленты ≠ стакан)
    fe.check_liquidity = lambda *a, **k: None   # стаканы по спреду в норме
    fe._EXEC_CACHE.clear()
    why_basis = fe.not_executable(CLI, {"symbol": "BASISUSDT",
                                        "spot_symbol": "BASISUSDT"})
    check("базис стакана +3% > порога ±0.5% → причина",
          bool(why_basis) and "базис" in why_basis)
    fe._EXEC_CACHE.clear()
    check("базис стакана в норме → None",
          fe.not_executable(CLI, {"symbol": "FLATUSDT",
                                  "spot_symbol": "FLATUSDT"}) is None)
finally:
    fe.check_liquidity = real_cl
    fe._EXEC_CACHE.clear()

print()
print("Итог:", "все проверки пройдены" if ok else "ЕСТЬ ПРОВАЛЫ")
sys.exit(0 if ok else 1)
