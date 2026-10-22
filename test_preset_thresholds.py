"""Тесты пресет-изоляции порогов thresholds_for().
Пороги, выученные пресетом hold (на deal-памяти testnet), НЕ должны
применяться к пресету mainnet — у того свои default'ы (с запасом на
комиссию). thresholds_for(mainnet) до собственной калибровки = None.
Запуск: python test_preset_thresholds.py  (stdlib, без pytest)."""
from __future__ import annotations

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
import bybit_memory as bm  # noqa: E402

MEM = os.path.join(tempfile.gettempdir(), "test_mem_presets.json")
if os.path.exists(MEM):
    os.remove(MEM)
# load/save_memory имеют дефолт path=MEM_FILE, захваченный при объявлении —
# подменяем ДЕФОЛТ в сигнатуре, чтобы все вызовы без аргумента ушли в тест-файл
bm.load_memory.__defaults__ = (MEM,)
bm.save_memory.__defaults__ = (MEM,)


def check(name, cond):
    print(("OK   " if cond else "FAIL ") + name)
    return cond


def main() -> int:
    ok = True
    bm.MEM_FILE = MEM

    # 1. новый mainnet-пресет без калибровки → None (дефолты пресета сильнее)
    ok &= check("mainnet без калибровки → None (дефолт пресета)",
                bm.thresholds_for("mainnet") is None)

    # 2. hold без калибровки — тоже None (дефолты пресета сильнее памяти)
    ok &= check("hold без калибровки → None",
                bm.thresholds_for("hold") is None)

    # 3. писать threshold в hold; mainnet НЕ должен его увидеть
    mem = bm.load_memory()
    bm.preset_of(mem, "hold")["thresholds"] = {
        "min_apy30": 13.5, "min_basis": 0.0, "fast_apy": 4.2}
    bm.save_memory(mem)
    ok &= check("hold отдаёт выученные пороги",
                bm.thresholds_for("hold")["min_apy30"] == 13.5)
    ok &= check("mainnet НЕ видит чужой калибровки",
                bm.thresholds_for("mainnet") is None)

    # 4. после собственной калибровки mainnet отдаёт свои пороги
    mem = bm.load_memory()
    bm.preset_of(mem, "mainnet")["thresholds"] = {
        "min_apy30": 18.0, "min_basis": 0.05, "fast_apy": 6.0}
    bm.save_memory(mem)
    thr_mn = bm.thresholds_for("mainnet")
    ok &= check("mainnet после калибровки — свои пороги",
                thr_mn["min_apy30"] == 18.0 and thr_mn["fast_apy"] == 6.0)
    ok &= check("изоляция работает в обе стороны: hold не тронут",
                bm.thresholds_for("hold")["min_apy30"] == 13.5)

    # 5. пресет mainnet существует в funding_monitor.PRESETS с запасом
    import funding_monitor as fm
    mn = fm.PRESETS["mainnet"]
    ok &= check("пресет mainnet в PRESETS", bool(mn))
    ok &= check("mainnet min_apy30 запас ≥ hold",
                mn["min_apy30"] > fm.PRESETS["hold"]["min_apy30"])
    ok &= check("mainnet max_breakeven жёстче hold (комиссии!)",
                mn["max_breakeven"] < fm.PRESETS["hold"]["max_breakeven"])

    # 6. БАЗА калибровки — от своего пресета (иначе сдвиг mainnet уводил
    #    реальные деньги ниже порога «с запасом на комиссию круга»)
    ok &= check("база по пресетам: mainnet 15/6, spike 30/3, hold 10/3",
                bm.default_thresholds("mainnet") == {"min_apy30": 15.0,
                                                     "min_basis": 0.0,
                                                     "fast_apy": 6.0}
                and bm.default_thresholds("spike")["min_apy30"] == 30.0
                and bm.default_thresholds("hold")["min_apy30"] == 10.0)
    ok &= check("PRESET_BASE совпадает с PRESETS монитора",
                all(bm.PRESET_BASE[p]["min_apy30"] == fm.PRESETS[p]["min_apy30"]
                    for p in ("hold", "spike", "mainnet")))
    ok &= check("mainnet не падает до hold-базы",
                bm.base_of({}, "mainnet")["min_apy30"] == 15.0)
    ok &= check("старая hold-база мигрирует под пресет",
                bm.base_of({"base": {"min_apy30": 10.0, "min_basis": 0.0,
                                     "fast_apy": 3.0}}, "mainnet")["min_apy30"] == 15.0)

    # 7. calibrate пресета mainnet не уводит min_apy30 ниже его базы
    import time as _t
    now = int(_t.time() * 1000)
    m2 = bm.load_memory()
    bm.relearn(m2, os.path.join(tempfile.gettempdir(), "no_such_log.log"))
    for i in range(6):
        m2["trades"].setdefault("BTCUSDT", []).append(
            {"ts": now - i * 1000, "mode": "mainnet", "held_h": 9.0,
             "pnl": 0.0, "funding": 0.05, "total": 0.05, "apy30": 15.0,
             "basis_entry": 0.1, "interval_h": 8, "pnl_pct": 0.5})
    bm.calibrate(m2, preset="mainnet", mode=("mainnet",))
    thr_mn2 = bm.preset_of(m2, "mainnet").get("thresholds") or {}
    ok &= check("calibrate mainnet: порог не ниже базы 15%",
                thr_mn2.get("min_apy30", 0) >= 15.0,
                )
    if thr_mn2.get("min_apy30", 0) < 15.0:
        print(f"       (фактически {thr_mn2['min_apy30']})")

    print("\nИтог: " + ("все проверки пройдены" if ok else "ЕСТЬ ПРОВАЛЫ"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
