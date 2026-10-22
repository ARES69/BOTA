"""Тесты сравнения maker/taker входов (bybit_memory.learn_entry_mode).
Запуск: python test_entry_mode.py  (stdlib, без pytest)."""
from __future__ import annotations

import os
import sys
import tempfile
import time

# Консоль Windows по умолчанию cp1252 — печать кириллицы в check() роняла
# тест с UnicodeEncodeError при прямом запуске (через run_tests.py работало,
# потому что раннер выставляет PYTHONIOENCODING=utf-8).
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bybit_memory as bm  # noqa: E402

NOW = int(time.time() * 1000)


def mk_mem(trades=None):
    mem = {"version": 1, "trades": {}, "stops": {}, "events": [],
           "lessons": [], "ingest_ts": 0.0, "updated_ms": 0}
    for sym, rows in (trades or {}).items():
        mem["trades"].setdefault(sym, []).extend(rows)
    return mem


def trade(legs="maker:maker", held_h=2.0, total=0.5, pnl_pct=None,
          slip_bps=1.0, sym="BTCUSDT"):
    t = {"ts": NOW, "mode": "testnet", "held_h": held_h, "pnl": total * 0.5,
         "funding": 0.2, "total": total, "legs": legs}
    if pnl_pct is not None:
        t["pnl_pct"] = pnl_pct
    if slip_bps is not None:
        t["slip_bps"] = slip_bps
    return t


def check(name, cond):
    print(("OK   " if cond else "FAIL ") + name)
    return bool(cond)


results = []

# 1) мало данных в одной группе → prefer=None, урок None
mem = mk_mem({"BTCUSDT": [trade(legs="maker:maker")] * 3
              + [trade(legs="taker:taker")]})
out = bm.learn_entry_mode(mem)
results.append(check("1 мало taker-сделок → prefer None", out is None
                     and mem["entry_mode"]["prefer"] is None))
results.append(check("1b группы посчитаны (3 maker / 1 taker)",
                     mem["entry_mode"]["maker"]["n"] == 3
                     and mem["entry_mode"]["taker"]["n"] == 1))

# 2) maker лучше на ≥0.10 п.п. → prefer=maker, урок при первом выводе
mem = mk_mem({"BTCUSDT": [trade(legs="maker:maker", total=0.9, pnl_pct=0.15)] * 3
              + [trade(legs="taker:taker", total=-0.2, pnl_pct=-0.03)] * 3})
out = bm.learn_entry_mode(mem)
results.append(check("2 maker лучше → prefer=maker",
                     out is not None and mem["entry_mode"]["prefer"] == "maker"))
results.append(check("2b в уроке числа", "maker лучше" in out and "maker" in out))

# 3) идемпотентность: тот же prefer → урок None (не спамим лог)
out2 = bm.learn_entry_mode(mem)
results.append(check("3 повторный вызов без смены → None", out2 is None))

# 4) taker лучше → prefer=taker, урок про маркет
mem = mk_mem({"BTCUSDT": [trade(legs="maker:maker", total=-0.4, pnl_pct=-0.07)] * 3
              + [trade(legs="taker:taker", total=0.6, pnl_pct=0.10)] * 3})
out = bm.learn_entry_mode(mem)
results.append(check("4 taker лучше → prefer=taker",
                     out is not None and mem["entry_mode"]["prefer"] == "taker"))
results.append(check("4b урок про маркет", "маркетом" in out))

# 5) смена предпочтения фиксируется уроком
mem["entry_mode"]["prefer"] = "maker"   # имитируем прошлое состояние
out = bm.learn_entry_mode(mem)
results.append(check("5 смена maker→taker даёт урок", out is not None
                     and mem["entry_mode"]["prefer"] == "taker"))

# 6) разница в пределах порога (0.05 п.п. < 0.10) → prefer=None
mem = mk_mem({"BTCUSDT": [trade(legs="maker:maker", total=0.1, pnl_pct=0.02)] * 3
              + [trade(legs="taker:taker", total=0.05, pnl_pct=0.01)] * 3})
out = bm.learn_entry_mode(mem)
results.append(check("6 разница < порога → prefer None", out is None
                     and mem["entry_mode"]["prefer"] is None
                     and mem["entry_mode"]["maker"]["n"] == 3))

# 7) шумовые сделки (held_h < 0.5) не считаются
mem = mk_mem({"BTCUSDT": [trade(legs="maker:maker", held_h=0.1)] * 5
              + [trade(legs="taker:taker")] * 5})
bm.learn_entry_mode(mem)
results.append(check("7 шум не идёт в группы",
                     mem["entry_mode"]["maker"]["n"] == 0
                     and mem["entry_mode"]["taker"]["n"] == 5))

# 8) slip_bps усредняется; без slip → None
mem = mk_mem({"BTCUSDT": [trade(legs="maker:maker", slip_bps=0.5)] * 3
              + [trade(legs="taker:taker", slip_bps=4.5)] * 3})
bm.learn_entry_mode(mem)
results.append(check("8 средний slip считается",
                     mem["entry_mode"]["maker"]["avg_slip_bps"] == 0.5
                     and mem["entry_mode"]["taker"]["avg_slip_bps"] == 4.5))
mem = mk_mem({"BTCUSDT": [trade(legs="maker:maker", slip_bps=None)] * 3
              + [trade(legs="taker:taker", slip_bps=None)] * 3})
bm.learn_entry_mode(mem)
results.append(check("8b без slip → None", mem["entry_mode"]["maker"]["avg_slip_bps"] is None))

# 9) legs нет/битый → сделка в никуда, не ломает
mem = mk_mem({"BTCUSDT": [trade(legs=None)] * 3 + [trade(legs="taker:taker")] * 3})
out = bm.learn_entry_mode(mem)
results.append(check("9 сделки без legs игнорируются",
                     mem["entry_mode"]["maker"]["n"] == 0
                     and mem["entry_mode"]["taker"]["n"] == 3))

# 10) mixed legs 'maker:taker' → группа по ПЕРВОЙ (перп) компоненте
mem = mk_mem({"BTCUSDT": [trade(legs="maker:taker", total=0.8, pnl_pct=0.13)] * 3
              + [trade(legs="taker:taker", total=-0.3, pnl_pct=-0.05)] * 3})
out = bm.learn_entry_mode(mem)
results.append(check("10 группа по перп-ноге: maker:taker → maker",
                     mem["entry_mode"]["prefer"] == "maker"))

# 11) relearn сбрасывает entry_mode
mem = mk_mem({"BTCUSDT": [trade()] * 3})
bm.learn_entry_mode(mem)
bm.relearn(mem, os.path.join(tempfile.gettempdir(), "no_such_log.log"))
results.append(check("11 relearn сбросил entry_mode", mem.get("entry_mode") == {}))

# 12) save/load сохраняют entry_mode
mem = mk_mem({"BTCUSDT": [trade(legs="maker:maker", total=0.9, pnl_pct=0.15)] * 3
              + [trade(legs="taker:taker", total=-0.2, pnl_pct=-0.03)] * 3})
bm.learn_entry_mode(mem)
tmp = os.path.join(tempfile.gettempdir(), "test_mem_entry_mode.json")
bm.save_memory(mem, tmp)
mem2 = bm.load_memory(tmp)
results.append(check("12 save/load: entry_mode на месте",
                     mem2.get("entry_mode", {}).get("prefer") == "maker"
                     and mem2["entry_mode"]["maker"]["n"] == 3))
os.remove(tmp)

# 13) нешумовая сделка с обоими стилями попадает только в свою группу
mem = mk_mem({"BTCUSDT": [trade(legs="maker:maker")] * 2
              + [trade(legs="taker:taker")] * 2})
stats = bm.entry_mode_stats(mem)
results.append(check("13 статистика групп: 2/2",
                     stats["maker"]["n"] == 2 and stats["taker"]["n"] == 2))

# 14) дедуп close+ingest: held в логе округлён до 0.1 ч — дубль с допуском
#     отсекается (жучок: точное сравнение пропускало дубль → ложная серия
#     убытков из 2 сделок → ложный стоп-лист на 7 дней; пойман живьём на DOT)
mem = mk_mem()
ok1, _ = bm.record_trade(mem, "DOTUSDT", "testnet", 1.128, -0.64, 0.0, -1.2825)
ok2, _ = bm.record_trade(mem, "DOTUSDT", "testnet", 1.1, -0.64, 0.0, -1.2825)
results.append(check("14a дубль close+ingest отсечён (held 1.128 vs 1.1)",
                     ok1 and not ok2
                     and len(mem["trades"]["DOTUSDT"]) == 1))
ok3, _ = bm.record_trade(mem, "DOTUSDT", "testnet", 1.3, -0.50, 0.02, -0.9)
results.append(check("14b другая сделка не спутана с дублем",
                     ok3 and len(mem["trades"]["DOTUSDT"]) == 2))
# настоящая серия из 2 убытков по-прежнему включает стоп-лист
mem = mk_mem()
bm.record_trade(mem, "XUSDT", "testnet", 2.0, -0.5, 0.1, -0.8)
bm.record_trade(mem, "XUSDT", "testnet", 3.0, -0.4, 0.05, -0.7)
results.append(check("14c настоящие 2 убытка подряд → стоп-лист работает",
                     mem["stops"].get("XUSDT", 0) > time.time()))

print()
fails = sum(1 for r in results if not r)
print(f"ИТОГО: {len(results) - fails}/{len(results)} OK" + (f", FAILS: {fails}" if fails else ""))
sys.exit(1 if fails else 0)
