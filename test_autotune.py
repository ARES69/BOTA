"""Тесты единого механизма порогов: calibrate (bias прогноза) + autotune
(profit-сдвиг по факту прибыли) → _combine → calibration.thresholds,
журнал решений threshold_log.
Запуск: python test_autotune.py  (stdlib, без pytest)."""
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
D = 86400 * 1000  # сутки в мс


def mk_mem(trades=None, autotune_ts=0.0, calibration=None):
    mem = {"version": 1, "trades": {}, "stops": {}, "events": [],
           "lessons": [], "ingest_ts": 0.0, "updated_ms": 0,
           "autotune_ts": autotune_ts}
    if calibration:
        mem["calibration"] = calibration
    for sym, rows in (trades or {}).items():
        mem["trades"].setdefault(sym, []).extend(rows)
    return mem


def trade(sym="BTCUSDT", held_h=2.0, total=-0.5, apy30=12.0,
          basis_entry=-0.05, ts_ms=None, pnl_pct=None, funding=0.3):
    t = {"ts": ts_ms if ts_ms is not None else NOW, "mode": "testnet",
         "held_h": held_h, "pnl": total * 0.5, "funding": funding,
         "total": total}
    if apy30 is not None:
        t["apy30"] = apy30
    if basis_entry is not None:
        t["basis_entry"] = basis_entry
    if pnl_pct is not None:
        t["pnl_pct"] = pnl_pct
    return t


def check(name, cond):
    print(("OK   " if cond else "FAIL ") + name)
    return bool(cond)


results = []

# 1) cadence: неделю не прошло → None (молча)
mem = mk_mem(autotune_ts=NOW - 1 * D)
results.append(check("1 cadence: неделю не прошло → None", bm.autotune(mem) is None))
results.append(check("1b ts не сдвинулся", mem["autotune_ts"] == NOW - 1 * D))

# 2) мало сделок → сообщение, ts сдвинулся, порогов нет
mem = mk_mem(autotune_ts=NOW - 8 * D,
             trades={"BTCUSDT": [trade(ts_ms=NOW - 1000),
                                 trade(ts_ms=NOW - 2000)]})
out = bm.autotune(mem)
results.append(check("2 мало сделок → сообщение", out is not None
                     and "пороги не трогаю" in out))
results.append(check("2b ts сдвинулся", abs(mem["autotune_ts"] - NOW) < 5000))
results.append(check("2c thresholds/profit_shift не создались",
                     "thresholds" not in mem.get("calibration", {})
                     and "profit_shift" not in mem))
results.append(check("2d журнал пуст", not mem.get("threshold_log")))

# 3) убыточные окна → profit-сдвиг ВВЕРХ и применён (единый слот)
mem = mk_mem(autotune_ts=NOW - 8 * D, trades={"BTCUSDT": [
    trade(total=-0.5, apy30=12.0, ts_ms=NOW - 5000),
    trade(total=-0.4, apy30=11.0, ts_ms=NOW - 4000),
    trade(total=-0.6, apy30=13.0, ts_ms=NOW - 3000),
    trade(total=0.3, apy30=12.0, ts_ms=NOW - 2000),
    trade(total=0.2, apy30=11.0, ts_ms=NOW - 1000),
]})
out = bm.autotune(mem)
shift = mem["profit_shift"]
thr = mem["calibration"]["thresholds"]
results.append(check("3 profit-сдвиг +3/+1.5/+0.15",
                     shift == {"min_apy30": 3.0, "fast_apy": 1.5,
                               "min_basis": 0.15}))
results.append(check("3b пороги применены: 13/0.15/4.5",
                     thr["min_apy30"] == 13.0 and thr["min_basis"] == 0.15
                     and thr["fast_apy"] == 4.5))
results.append(check("3c журнал: запись autotune",
                     len(mem.get("threshold_log", [])) == 1
                     and mem["threshold_log"][0]["kind"] == "autotune"))
results.append(check("3d журнал: before/after зафиксированы",
                     mem["threshold_log"][0]["before"]["min_apy30"] == 10.0
                     and mem["threshold_log"][0]["after"]["min_apy30"] == 13.0))
results.append(check("3e урок с изменением", out and "13" in out))

# 4) убыточные окна без apy30/basis → сдвиг 0 → пороги = база, без записи
mem = mk_mem(autotune_ts=NOW - 8 * D, trades={"BTCUSDT": [
    trade(total=-0.5, apy30=None, basis_entry=None, ts_ms=NOW - 5000),
    trade(total=-0.4, apy30=None, basis_entry=None, ts_ms=NOW - 4000),
    trade(total=-0.6, apy30=None, basis_entry=None, ts_ms=NOW - 3000),
    trade(total=0.3, apy30=12.0, ts_ms=NOW - 2000),
    trade(total=0.2, apy30=11.0, ts_ms=NOW - 1000),
]})
out = bm.autotune(mem)
results.append(check("4 без прогноза/базиса → сдвиг 0, без записи в журнал",
                     mem["profit_shift"] == {"min_apy30": 0.0, "fast_apy": 0.0,
                                             "min_basis": 0.0}
                     and mem["calibration"]["thresholds"] == bm.default_thresholds()
                     and not mem.get("threshold_log")))

# 5) окупившиеся окна опускают СУЩЕСТВУЮЩИЙ сдвиг к нулю, не ниже
mem = mk_mem(autotune_ts=NOW - 8 * D,
             calibration={"thresholds": {"min_apy30": 12.0, "min_basis": 0.10,
                                         "fast_apy": 4.0}},
             trades={"BTCUSDT": [
                 trade(total=1.2, pnl_pct=20.0, apy30=25.0, ts_ms=NOW - 5000),
                 trade(total=1.0, pnl_pct=17.0, apy30=22.0, ts_ms=NOW - 4000),
                 trade(total=1.1, pnl_pct=18.0, apy30=20.0, ts_ms=NOW - 3000),
                 trade(total=0.8, pnl_pct=13.0, apy30=18.0, ts_ms=NOW - 2000),
                 trade(total=0.9, pnl_pct=15.0, apy30=19.0, ts_ms=NOW - 1000),
             ]})
mem["profit_shift"] = {"min_apy30": 2.0, "fast_apy": 1.0, "min_basis": 0.10}
mem["calibration"]["base"] = dict(bm.default_thresholds())
out = bm.autotune(mem)
shift = mem["profit_shift"]
results.append(check("5 окупившиеся окна опустили сдвиг до 0",
                     shift == {"min_apy30": 0.0, "fast_apy": 0.0,
                               "min_basis": 0.0}))
results.append(check("5b пороги вернулись к базе", 
                     mem["calibration"]["thresholds"] == bm.default_thresholds()))

# 6) идемпотентность: повторный вызов → None, всё на месте
shift_before = dict(mem["profit_shift"])
thr_before = dict(mem["calibration"]["thresholds"])
log_before = len(mem.get("threshold_log", []))
results.append(check("6 повторный вызов → None", bm.autotune(mem) is None))
results.append(check("6b состояние не изменилось",
                     mem["profit_shift"] == shift_before
                     and mem["calibration"]["thresholds"] == thr_before
                     and len(mem["threshold_log"]) == log_before))

# 7) потолки: 20 убыточных окон → сдвиг и пороги ограничены
rows = [trade(total=-0.5, apy30=30.0, ts_ms=NOW - 50000 + i * 10)
        for i in range(20)]
mem = mk_mem(autotune_ts=NOW - 8 * D, trades={"BTCUSDT": rows})
out = bm.autotune(mem)
thr = mem["calibration"]["thresholds"]
results.append(check("7 потолки: min30≤25, basis≤0.5, fast≤min30",
                     thr["min_apy30"] <= bm.AUTO_MAX_30
                     and thr["min_basis"] <= bm.AUTO_MAX_BASIS
                     and thr["fast_apy"] <= thr["min_apy30"]))

# 8) шум: <5 сделок → «мало», 5+ шумовых → «шум», без порогов
mem = mk_mem(autotune_ts=NOW - 8 * D, trades={"BTCUSDT": [
    trade(held_h=0.1, total=-2.0, apy30=30.0, ts_ms=NOW - 5000),
    trade(held_h=0.2, total=-2.0, apy30=30.0, ts_ms=NOW - 4000),
]})
out = bm.autotune(mem)
results.append(check("8a 2 шумовые → «мало сделок»",
                     out is not None and "пороги не трогаю" in out
                     and "profit_shift" not in mem))
mem = mk_mem(autotune_ts=NOW - 8 * D, trades={"BTCUSDT": [
    trade(held_h=0.1, total=-2.0, apy30=30.0, ts_ms=NOW - 5000),
    trade(held_h=0.2, total=-2.0, apy30=30.0, ts_ms=NOW - 4000),
    trade(held_h=0.3, total=-2.0, apy30=30.0, ts_ms=NOW - 3000),
    trade(held_h=0.15, total=-2.0, apy30=30.0, ts_ms=NOW - 2000),
    trade(held_h=0.25, total=-2.0, apy30=30.0, ts_ms=NOW - 1000),
]})
out = bm.autotune(mem)
results.append(check("8b 5 шумовых → «шум»",
                     out is not None and "шум" in out and "profit_shift" not in mem))

# 9) force → немедленный пересчёт, окно — неделя назад
mem = mk_mem(autotune_ts=NOW, trades={"BTCUSDT": [
    trade(total=-0.5, apy30=12.0, ts_ms=NOW - 5000),
    trade(total=-0.4, apy30=11.0, ts_ms=NOW - 4000),
    trade(total=-0.6, apy30=13.0, ts_ms=NOW - 3000),
    trade(total=0.3, apy30=12.0, ts_ms=NOW - 2000),
    trade(total=0.2, apy30=11.0, ts_ms=NOW - 1000),
]})
out = bm.autotune(mem, force=True)
results.append(check("9 force → пересчёт и применение",
                     out is not None
                     and mem["calibration"]["thresholds"]["min_apy30"] == 13.0))

# 10) сделки старше autotune_ts исключены
mem = mk_mem(autotune_ts=NOW - 8 * D, trades={"BTCUSDT": [
    trade(total=-0.5, apy30=30.0, ts_ms=NOW - 9 * D),
    trade(total=-0.5, apy30=30.0, ts_ms=NOW - 9 * D + 1),
    trade(total=-0.5, apy30=30.0, ts_ms=NOW - 9 * D + 2),
    trade(total=-0.5, apy30=30.0, ts_ms=NOW - 9 * D + 3),
    trade(total=-0.5, apy30=30.0, ts_ms=NOW - 9 * D + 4),
    trade(total=0.3, apy30=12.0, ts_ms=NOW - 1000),
    trade(total=0.2, apy30=11.0, ts_ms=NOW - 2000),
]})
out = bm.autotune(mem)
results.append(check("10 старые сделки не учитываются",
                     out is not None and "пороги не трогаю" in out
                     and "profit_shift" not in mem))

# 11) relearn сбрасывает profit_shift, но сохраняет журнал
mem = mk_mem(autotune_ts=NOW)
mem["profit_shift"] = {"min_apy30": 1.0, "fast_apy": 0.5, "min_basis": 0.05}
mem["threshold_log"] = [{"ts": NOW, "kind": "calibrate", "trigger": "t",
                         "before": {}, "after": {}}]
bm.relearn(mem, os.path.join(tempfile.gettempdir(), "no_such_log.log"))
results.append(check("11 relearn: shift сброшен, журнал сохранён",
                     mem.get("profit_shift") == {}
                     and len(mem.get("threshold_log", [])) == 1))

# 12) save/load сохраняют profit_shift и threshold_log
mem = mk_mem(autotune_ts=NOW - 8 * D, trades={"BTCUSDT": [
    trade(total=-0.5, apy30=12.0, ts_ms=NOW - 5000),
    trade(total=-0.4, apy30=11.0, ts_ms=NOW - 4000),
    trade(total=-0.6, apy30=13.0, ts_ms=NOW - 3000),
    trade(total=0.3, apy30=12.0, ts_ms=NOW - 2000),
    trade(total=0.2, apy30=11.0, ts_ms=NOW - 1000),
]})
bm.autotune(mem)
tmp = os.path.join(tempfile.gettempdir(), "test_mem_thr.json")
bm.save_memory(mem, tmp)
mem2 = bm.load_memory(tmp)
results.append(check("12 save/load: shift, пороги, журнал на месте",
                     mem2.get("profit_shift") == mem["profit_shift"]
                     and mem2["calibration"]["thresholds"]
                     == mem["calibration"]["thresholds"]
                     and len(mem2.get("threshold_log", [])) == 1))
os.remove(tmp)

# 13) ГЛАВНОЕ: calibrate и autotune не затирают друг друга
rows = []
for i in range(6):
    rows.append({"ts": NOW - i * 1000, "mode": "testnet", "held_h": 1.0,
                 "pnl": -0.2, "funding": 0.05, "total": -0.2, "apy30": 200.0})
mem = mk_mem(trades={"BTCUSDT": rows})
cal_out = bm.calibrate(mem)                     # прогноз завышен → пороги вверх
thr_cal = dict(bm.preset_of(mem, "hold")["thresholds"])
results.append(check("13 calibrate: прогноз завышен → action=up (пороги вверх)",
                     cal_out is not None
                     and bm.preset_of(mem, "hold").get("action") == "up"
                     and thr_cal["min_apy30"] > 10.0))
# зеркало в общий calibration для dashboard/старых читателей
results.append(check("13-з зеркало: общий calibration.thresholds обновлён",
                     mem["calibration"]["thresholds"] == thr_cal))
at_out = bm.autotune(mem, force=True)           # убыточные окна → сдвиг вверх
thr_at = dict(mem["calibration"]["thresholds"])
results.append(check("13b autotune добавил profit-сдвиг поверх bias",
                     at_out is not None
                     and thr_at["min_apy30"] > thr_cal["min_apy30"]))
results.append(check("13c журнал: две записи (calibrate, autotune)",
                     [e["kind"] for e in mem["threshold_log"]]
                     == ["calibrate", "autotune"]))
# рестарт watch: calibrate снова — profit-сдвиг НЕ теряется
mem["calibration"]["n"] = 0                      # сбросить hold-порог нечем; гоняем по новой
cal2 = bm.calibrate(mem)
thr_after = dict(mem["calibration"]["thresholds"])
results.append(check("13d повторный calibrate сохранил profit-сдвиг",
                     thr_after == thr_at))
results.append(check("13e журнал не растёт без изменений",
                     len(mem["threshold_log"]) == 2))

# 14) журнал ограничен THR_LOG_KEEP
mem = {"trades": {}, "stops": {}, "events": [], "lessons": []}
for i in range(60):
    bm._log_thr(mem, "test", f"t{i}", {}, {"min_apy30": 10.0 + i * 0.1,
                                           "min_basis": 0.0, "fast_apy": 3.0})
results.append(check("14 журнал ограничен 50 записями",
                     len(mem["threshold_log"]) == bm.THR_LOG_KEEP
                     and mem["threshold_log"][0]["trigger"] == "t10"))

print()
fails = sum(1 for r in results if not r)
print(f"ИТОГО: {len(results) - fails}/{len(results)} OK" + (f", FAILS: {fails}" if fails else ""))
sys.exit(1 if fails else 0)
