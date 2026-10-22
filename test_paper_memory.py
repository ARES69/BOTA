"""Тесты paper-обучения и изоляции по режимам (mode_match / learning_modes).

Смысл фичи: paper — симуляция на РЕАЛЬНЫХ ставках и комиссиях mainnet, поэтому
её сделки должны учить mainnet-пороги (calibrate/autotune/entry_mode), а
синтетический testnet — НЕ должен попадать в mainnet-обучение.
Запуск: python test_paper_memory.py  (stdlib, без pytest)."""
from __future__ import annotations

import copy
import os
import sys
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
import funding_executor as fe  # noqa: E402


def check(name, cond):
    print(("OK   " if cond else "FAIL ") + name)
    return cond


def trade(mode, ts, legs=None):
    """Годная к обучению сделка: длинная, с прогнозом APY и положительным funding."""
    t = {"ts": ts, "mode": mode, "apy30": 10.0, "held_h": 5.0,
         "funding": 1.0, "total": 1.0, "pnl_pct": 0.5}
    if legs is not None:
        t["legs"] = legs
    return t


def build_mem():
    now = int(time.time() * 1000) - 1_000
    return {"trades": {
        "AAAUSDT": [trade("paper", now - 3), trade("paper", now - 2),
                    trade("paper", now - 1)],
        "BBBUSDT": [trade("testnet", now - 2), trade("testnet", now - 1)],
    }}


def main() -> int:
    ok = True
    now = int(time.time() * 1000) - 1_000

    # --- 1. mode_match: None = все, строка = равенство, кортеж/список/сет = членство
    ok &= check("mode_match: None → все среды",
                bm.mode_match("testnet", None) and bm.mode_match("paper", None))
    ok &= check("mode_match: строка — только своя среда",
                bm.mode_match("paper", "paper") and
                not bm.mode_match("testnet", "paper"))
    ok &= check("mode_match: кортеж — paper в mainnet-обучении",
                bm.mode_match("paper", ("mainnet", "paper")) and
                bm.mode_match("mainnet", ("mainnet", "paper")) and
                not bm.mode_match("testnet", ("mainnet", "paper")))
    ok &= check("mode_match: список и set",
                bm.mode_match("paper", ["paper"]) and
                bm.mode_match("paper", {"paper", "mainnet"}))

    # --- 2. learning_modes: какие среды релевантны режиму бота
    ok &= check("learning_modes: mainnet ← (mainnet, paper)",
                set(fe.learning_modes("mainnet")) == {"mainnet", "paper"})
    ok &= check("learning_modes: paper ← (paper, mainnet)",
                set(fe.learning_modes("paper")) == {"paper", "mainnet"})
    ok &= check("learning_modes: testnet — только свои ставки",
                fe.learning_modes("testnet") == ("testnet",))

    # --- 3. calibrate: считает сделки ТОЛЬКО выбранных сред
    ok &= check("предусловие: 3 < CAL_MIN_TRADES (ранний выход с n)",
                3 < bm.CAL_MIN_TRADES)

    def cal_n(mode):
        mem = copy.deepcopy(build_mem())
        bm.calibrate(mem, preset="hold", mode=mode)
        return bm.preset_of(mem, "hold").get("n")

    ok &= check("calibrate: mode=paper → 3 (свои сделки)",
                cal_n(("paper",)) == 3)
    ok &= check("calibrate: mode=(mainnet,paper) → 3 (paper учит mainnet)",
                cal_n(("mainnet", "paper")) == 3)
    ok &= check("calibrate: mode=testnet → 2 (только свои)",
                cal_n(("testnet",)) == 2)
    ok &= check("calibrate: mode=mainnet → 0 (testnet НЕ просачивается)",
                cal_n("mainnet") == 0)
    ok &= check("calibrate: mode=None → 5 (все среды)",
                cal_n(None) == 5)

    # --- 4. autotune: вклад периода тоже изолирован по режиму
    ok &= check("предусловие: 3 < AUTO_MIN_TRADES", 3 < bm.AUTO_MIN_TRADES)

    def auto_count(mode):
        mem = copy.deepcopy(build_mem())
        msg = bm.autotune(mem, force=True, preset="hold", mode=mode) or ""
        return msg

    ok &= check("autotune: paper учитывает 3 сделки",
                "сделок 3" in auto_count(("paper",)))
    ok &= check("autotune: mainnet учится на paper (3), не на testnet",
                "сделок 3" in auto_count(("mainnet", "paper")))
    ok &= check("autotune: testnet видит только свои 2",
                "сделок 2" in auto_count(("testnet",)))

    # --- 5. entry_mode_stats: maker/taker тоже фильтруются по среде
    mem = copy.deepcopy(build_mem())
    mem["trades"]["AAAUSDT"] = [trade("paper", now - 3, "taker:maker"),
                                trade("paper", now - 2, "taker:maker"),
                                trade("paper", now - 1, "taker:maker")]
    mem["trades"]["BBBUSDT"] = [trade("testnet", now - 2, "maker:taker"),
                                trade("testnet", now - 1, "maker:taker")]
    st_paper = bm.entry_mode_stats(mem, mode=("paper",))
    st_test = bm.entry_mode_stats(mem, mode=("testnet",))
    ok &= check("entry_mode: paper → taker 3, maker 0",
                st_paper["taker"]["n"] == 3 and st_paper["maker"]["n"] == 0)
    ok &= check("entry_mode: testnet → maker 2, taker 0",
                st_test["maker"]["n"] == 2 and st_test["taker"]["n"] == 0)

    # --- 6. сквозная связка: режим бота mainnet учится на paper-сделках
    joined = bm.entry_mode_stats(mem, mode=fe.learning_modes("mainnet"))
    ok &= check("сквозная: обучение mainnet видит paper (taker 3)",
                joined["taker"]["n"] == 3 and joined["maker"]["n"] == 0)

    print("\nИтог: " + ("все проверки пройдены" if ok else "ЕСТЬ ПРОВАЛЫ"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
