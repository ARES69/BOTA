"""Тесты mainnet-перехода и изоляции памяти по режимам.
Проверяем: клиент выбирает среду по режиму и ключи из своих env-переменных;
set-leverage шлёт оба плеча одним запросом; вход на mainnet без
--confirm-live запрещён; калибровка/автотюнинг/entry_mode считают сделки
ТОЛЬКО своей среды (testnet-данные не влияют на mainnet-пороги).
Запуск: python test_mainnet.py  (stdlib, без pytest)."""
from __future__ import annotations

import os
import sys
import time
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
import bybit_memory as bm  # noqa: E402
import funding_executor as fe  # noqa: E402

NOW = int(time.time() * 1000)


def check(name, cond):
    print(("OK   " if cond else "FAIL ") + name)
    return cond


def mk_mem(trades=None, calibration=None):
    mem = {"version": 1, "trades": {}, "stops": {}, "events": [], "lessons": [],
           "ingest_ts": 0.0, "updated_ms": 0, "autotune_ts": 0}
    if calibration:
        mem["calibration"] = calibration
    for sym, rows in (trades or {}).items():
        mem["trades"].setdefault(sym, []).extend(rows)
    return mem


def trade(mode, total, apy30=30.0, ts=NOW, funding=0.05, held_h=1.0, legs=None):
    t = {"ts": ts, "mode": mode, "held_h": held_h, "pnl": total * 0.5,
         "funding": funding, "total": total, "apy30": apy30,
         "basis_entry": -0.05}
    if legs:
        t["legs"] = legs
        t["slip_bps"] = 1.0
    return t


def main() -> int:
    ok = True

    # --- 1. клиент выбирает среду и свои ключи ------------------------------
    os.environ["BYBIT_API_KEY"] = "MAIN_KEY"
    os.environ["BYBIT_API_SECRET"] = "MAIN_SECRET"
    os.environ["BYBIT_TESTNET_API_KEY"] = "TEST_KEY"
    os.environ["BYBIT_TESTNET_API_SECRET"] = "TEST_SECRET"

    mn = fe.make_client("mainnet")
    tn = fe.make_client("testnet")
    ok &= check("mainnet → base api.bybit.com", mn.base == "https://api.bybit.com")
    ok &= check("mainnet → ключи BYBIT_API_KEY", mn.key == "MAIN_KEY")
    ok &= check("testnet → base api-testnet", tn.base == "https://api-testnet.bybit.com")
    ok &= check("testnet → ключи BYBIT_TESTNET_API_KEY", tn.key == "TEST_KEY")
    ok &= check("режим записан в клиенте", mn.mode == "mainnet" and tn.mode == "testnet")

    # --- 2. set-leverage: оба плеча одним запросом --------------------------
    sent = {}

    def fake_request(method, path, params=None, body=None):
        sent.update({"method": method, "path": path, "body": body})
        return {}

    mn.request = fake_request
    mn.set_leverage("ETHUSDT", 2.0)
    ok &= check("set-leverage: POST /v5/position/set-leverage",
                sent["method"] == "POST" and sent["path"] == "/v5/position/set-leverage")
    ok &= check("set-leverage: symbol и оба плеча строкой",
                sent["body"] == {"category": "linear", "symbol": "ETHUSDT",
                                 "buyLeverage": "2.0", "sellLeverage": "2.0"})

    # --- 3. mainnet-вход без --confirm-live запрещён ------------------------
    cli_live = types.SimpleNamespace(mode="mainnet", confirm_live=False,
                                     capital=100.0, leverage=2.0)
    try:
        fe.open_position(cli_live, {"symbol": "ETHUSDT"})
        ok &= check("mainnet без --confirm-live → отказ", False)
    except SystemExit as e:
        ok &= check("mainnet без --confirm-live → отказ",
                    "confirm-live" in str(e))

    # --- 4. изоляция калибровки по режиму -----------------------------------
    # 5 testnet-сделок (прогноз завышен) + 1 mainnet (прогноз точен):
    # mainnet-калибровка не должна набирать порог по чужим сделкам
    mem = mk_mem(trades={
        "ETHUSDT": [trade("testnet", -0.9, apy30=200.0, ts=NOW - 100 + i)
                    for i in range(5)] + [trade("mainnet", 0.0, apy30=10.0)],
    })
    out = bm.calibrate(mem, preset="mainnet", mode="mainnet")
    thr = bm.preset_of(mem, "mainnet").get("thresholds")
    ok &= check("mainnet-калибровка не видит testnet-сделку (n<порога → None)",
                out is None and thr is None)
    out_tn = bm.calibrate(mem, preset="hold", mode="testnet")
    ok &= check("testnet-калибровка работает по своим сделкам",
                out_tn is not None and bm.preset_of(mem, "hold")["thresholds"]["min_apy30"] > 10.0)

    # --- 5. изоляция autotune по режиму -------------------------------------
    rows_main = [trade("mainnet", -0.5, apy30=12.0, ts=NOW - 5000 + i)
                 for i in range(5)]
    rows_test = [trade("testnet", 1.0, apy30=12.0, ts=NOW - 1000 + i)
                 for i in range(5)]
    mem = mk_mem(trades={"ETHUSDT": rows_main + rows_test}, calibration={})
    bm.autotune(mem, force=True, preset="mainnet", mode="mainnet")
    shift = bm.preset_of(mem, "mainnet").get("profit_shift") or {}
    ok &= check("autotune mainnet видит только свои убытки (сдвиг вверх)",
                shift.get("min_apy30", 0) > 0)

    # --- 6. изоляция entry_mode по режиму -----------------------------------
    mem = mk_mem(trades={
        "ETHUSDT": [trade("testnet", 1.0, legs="maker:maker") for _ in range(3)]
                   + [trade("testnet", -1.0, legs="taker:taker") for _ in range(3)],
    })
    st_net = bm.entry_mode_stats(mem, mode="testnet")
    st_main = bm.entry_mode_stats(mem, mode="mainnet")
    ok &= check("entry_mode testnet видит свои сделки",
                st_net["maker"]["n"] == 3 and st_net["taker"]["n"] == 3)
    ok &= check("entry_mode mainnet НЕ видит testnet-сделок",
                st_main["maker"]["n"] == 0 and st_main["taker"]["n"] == 0)
    ok &= check("learn_entry_mode для mainnet не делает вывода",
                bm.learn_entry_mode(mem, mode="mainnet") is None)

    # --- 7. режимы не смешиваются: у mainnet нет своих порогов, пока нет сделок
    mem = mk_mem(trades={"ETHUSDT": [trade("testnet", -0.5) for _ in range(5)]})
    bm.calibrate(mem, preset="hold", mode="testnet")
    ok &= check("hold-пресет выучил пороги",
                bm.preset_of(mem, "hold").get("thresholds") is not None)
    ok &= check("mainnet-пресет пуст при одних testnet-сделках",
                bm.preset_of(mem, "mainnet").get("thresholds") is None)
    ok &= check("profit_shift изолирован по пресетам",
                bm.preset_of(mem, "mainnet").get("profit_shift") is None)

    print("\nИтог: " + ("все проверки пройдены" if ok else "ЕСТЬ ПРОВАЛЫ"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
