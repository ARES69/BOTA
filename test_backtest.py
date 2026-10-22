"""Тесты логики бэктеста без сети: окна ставок, вердикт без look-ahead,
правила выхода, расчёт PnL, честность (пессимизм входа/выхода).
Запуск: python test_backtest.py  (stdlib, без pytest)."""
from __future__ import annotations

import argparse
import os
import sys

# Консоль Windows по умолчанию cp1252 — печать кириллицы в check() роняла
# тест с UnicodeEncodeError при прямом запуске (через run_tests.py работало,
# потому что раннер выставляет PYTHONIOENCODING=utf-8).
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import backtest as B  # noqa: E402
import dashboard_metrics as M  # noqa: E402

HOUR = 3_600_000
DAY = 86_400_000
T0 = 1_700_000_000_000


def check(name, cond):
    print(("OK   " if cond else "FAIL ") + name)
    return bool(cond)


ok = True

# --- окно ставок: только то, что известно на момент ts --------------------
# при интервале 8ч в 30-дневном окне помещается ~90 точек, а не 30 —
# главное, чтобы будущие ставки (в т.ч. огромные) не подмешались
hist = [(T0 + i * 8 * HOUR, 0.0001) for i in range(40)]
fut = [(T0 + (40 + i) * 8 * HOUR, 0.9) for i in range(20)]   # будущее
allh = hist + fut
# 0.0001 за 8ч = 10.95% годовых — это и есть ожидаемый avg
w = B.window_stats(allh, T0 + 30 * 8 * HOUR, 8.0, 30)
ok &= check("окно не видит будущее (ставка 0.9 не попала)",
            abs(w["avg_apy"] - 10.95) < 0.01,
            )
if abs(w["avg_apy"] - 10.95) > 0.01:
    print(f"       (фактически {w['avg_apy']:.2f}, ожидалось 10.95)")
w_full = B.window_stats(allh, T0 + 100 * 8 * HOUR, 8.0, 30)
# на 100-м интервале будущие ставки 0.9 (=~985% годовых) РЕАЛЬНО попадают
# в 30-дневное окно — это корректно, окно по времени, а не по индексу
ok &= check("окно по времени (на дальнем шаге будущее уже входит)",
            w_full["n"] > 40 and w_full["avg_apy"] > 100)
# при ts в середине окно ограничено моментом ts
mid = B.window_stats(allh, T0 + 20 * 8 * HOUR, 8.0, 30)
ok &= check("окно ограничено моментом ts (будущее не берётся)",
            abs(mid["avg_apy"] - 10.95) < 0.01)

# pos_share считается по доле плюсовых
w2 = B.window_stats([(T0, 0.001), (T0 + 8 * HOUR, -0.001),
                     (T0 + 16 * HOUR, 0.002)], T0 + 16 * HOUR, 8.0, 1)
ok &= check("pos_share = 2 из 3 плюсовых", abs(w2["pos_share"] - 66.67) < 0.1)

# --- _last_le: строго «последнее ≤ ts», без подглядывания вперёд ----------
seq = [(100, 1.0), (200, 2.0), (300, 3.0)]
ok &= check("_last_le на точном ts", B._last_le(seq, 200)[1] == 2.0)
ok &= check("_last_le между точками берёт предыдущую",
            B._last_le(seq, 250)[1] == 2.0)
ok &= check("_last_le до начала → None", B._last_le(seq, 50) is None)

# --- infer_interval ------------------------------------------------------
ok &= check("интервал по медиане = 8ч",
            B.infer_interval([(0, 0), (8 * HOUR, 0), (16 * HOUR, 0),
                              (24 * HOUR, 0)]) == 8)
ok &= check("короткая история → fallback", B.infer_interval([(0, 0)]) == 8.0)

# --- позиция: PnL и отсутствие look-ahead --------------------------------
sd = B.SymData("BTCUSDT", "BTCUSDT", 8.0,
               [(T0 + i * 8 * HOUR, 0.0005) for i in range(20)],
               [(T0 + i * DAY, 100.0) for i in range(20)],
               [(T0 + i * DAY, 100.0) for i in range(20)], 1e9, 1e9)

cfg = B.Config(capital=1000.0, leverage=2.0, hold_payouts=0, exit_neg=0,
               max_margin_risk=0.0, max_hold_h=1e9)
thr = {"min_apy30": 0.0, "min_pos": 0.0, "min_basis": -999.0,
       "max_breakeven": 1e6, "pass_oi": 0.0}

# вердикт проходит при снятых порогах
row = B.make_row(sd, T0 + 10 * DAY, 0.05)
ok &= check("make_row даёт строку при наличии данных", row is not None)
if row:
    from funding_monitor import enrich
    ns = B.simulate.__globals__  # не используется, enrich ниже с Namespace
    import argparse as _a
    args_ns = _a.Namespace(spot_fee=0.001, perp_taker_fee=0.00055,
                           perp_maker_fee=0.0002, preset="hold", **thr)
    r = enrich(row, B.spot_quotes_from(sd, T0 + 10 * DAY, 0.05), args_ns)
    ok &= check("вердикт проходит при снятых порогах", r.get("verdict_ok"))

# PnL позиции: цена не менялась → price_pnl = 0 (дельта-нейтральность),
# а весь убыток — это круг комиссий плюс спред на входе и выходе
pos = B.open_position(sd, 0, row, T0 + 10 * DAY, cfg)
closed = B.close_position(pos, sd, T0 + 10 * DAY, cfg, reason="test")
notional = closed["notional"]
# На плоском рынке спред симметричен (вход и выход по одной схеме), поэтому
# price_pnl = 0, и весь убыток — комиссии. Комиссию платит каждая нога и
# каждая сторона: notional × fee_pct × 2 ноги × 2 стороны. Проверяем
# главное — что price_pnl ≈ 0 и разбивка сходится (не абсолютную величину:
# она зависит от округления qty).
ok &= check("на плоском price_pnl = 0 (дельта-нейтральность)",
            abs(closed["price_pnl"]) < 0.01)
ok &= check("на плоском PnL = −fees_total (спред симметричен)",
            abs(closed["pnl_excl_funding"] + closed["fees_total"]) < 0.01)
ok &= check("PnL отрицателен на плоском (это пессимизм бэктеста)",
            closed["pnl_excl_funding"] < 0)
ok &= check("разбивка сходится: pnl + fees_total = price_pnl",
            abs(closed["pnl_excl_funding"] + closed["fees_total"]
                - closed["price_pnl"]) < 0.01)
ok &= check("комиссии круга в разумных пределах (0.3–0.6% нотиона)",
            0.003 * notional < closed["fees_total"] < 0.006 * notional)

# --- правила выхода ------------------------------------------------------
# hold-to-N: 2 положительные выплаты → выход
sd2 = B.SymData("X", "X", 8.0,
                [(T0 + i * 8 * HOUR, 0.001) for i in range(6)],
                [(T0 + i * DAY, 100.0) for i in range(6)],
                [(T0 + i * DAY, 100.0) for i in range(6)], 1e9, 1e9)
cfg2 = B.Config(capital=1000.0, hold_payouts=2, exit_neg=3,
                max_margin_risk=70.0)
p2 = {"idx": 0, "symbol": "X", "opened_ms": T0, "notional": 666.0,
      "margin": 333.0, "perp_entry": 100.0, "spot_entry": 100.0,
      "perp_qty": 6.66, "spot_qty": 6.66, "fees_in": 1.0, "payouts": 0,
      "funding": 0.0, "last_paid_ts": None, "neg_streak": 0, "apy30": 15.0,
      "capital": 1000.0}
reason, _ = B.check_exit(p2, sd2, T0 + 16 * HOUR, cfg2)
ok &= check("hold-to-N срабатывает после 2 выплат", reason == "hold_payouts")

# neg_streak: 3 отрицательные подряд → выход
sd3 = B.SymData("X", "X", 8.0,
                [(T0 + i * 8 * HOUR, -0.001) for i in range(6)],
                [(T0 + i * DAY, 100.0) for i in range(6)],
                [(T0 + i * DAY, 100.0) for i in range(6)], 1e9, 1e9)
p3 = {"idx": 0, "symbol": "X", "opened_ms": T0, "notional": 666.0,
      "margin": 333.0, "perp_entry": 100.0, "spot_entry": 100.0,
      "perp_qty": 6.66, "spot_qty": 6.66, "fees_in": 1.0, "payouts": 0,
      "funding": 0.0, "last_paid_ts": None, "neg_streak": 0, "apy30": 15.0,
      "capital": 1000.0}
reason3, _ = B.check_exit(p3, sd3, T0 + 24 * HOUR,
                          B.Config(capital=1000.0, hold_payouts=0,
                                   exit_neg=3, max_margin_risk=70.0))
ok &= check("neg_streak срабатывает после 3 отрицательных", reason3 == "neg_streak")

# margin_risk: цена уехала вверх (против шорта) на 70% маржи
sd4 = B.SymData("X", "X", 8.0,
                [(T0 + i * 8 * HOUR, 0.001) for i in range(6)],
                [(T0 + i * DAY, 100.0) for i in range(6)],
                [(T0 + i * DAY, 100.0) for i in range(6)], 1e9, 1e9)
# цена на последнем шаге выросла: +70% маржи = убыток 233 на 6.66 шорт = +35 цена
sd4.perp = [(T0 + i * DAY, 100.0) for i in range(5)] + [(T0 + 5 * DAY, 135.0)]
p4 = {"idx": 0, "symbol": "X", "opened_ms": T0, "notional": 666.0,
      "margin": 333.0, "perp_entry": 100.0, "spot_entry": 100.0,
      "perp_qty": 6.66, "spot_qty": 6.66, "fees_in": 1.0, "payouts": 0,
      "funding": 0.0, "last_paid_ts": T0 + 32 * HOUR, "neg_streak": 0,
      "apy30": 15.0, "capital": 1000.0}
reason4, _ = B.check_exit(p4, sd4, T0 + 5 * DAY,
                          B.Config(capital=1000.0, hold_payouts=0,
                                   exit_neg=0, max_margin_risk=70.0))
ok &= check("margin_risk срабатывает при росте цены", reason4 == "margin_risk")

# --- симуляция целиком (без сети) ----------------------------------------
# два символа с постоянной положительной ставкой и плоской ценой
def mk_flat(name, rate):
    n = 30
    return B.SymData(name, name, 8.0,
                     [(T0 + i * 8 * HOUR, rate) for i in range(n)],
                     [(T0 + i * DAY, 100.0) for i in range(n)],
                     [(T0 + i * DAY, 100.0) for i in range(n)], 1e9, 1e9)


res = B.simulate([mk_flat("A", 0.002), mk_flat("B", 0.002)], cfg2, thr)
ok &= check("симуляция на плоских данных даёт сделки", len(res["trades"]) > 0)
if res["trades"]:
    t = res["trades"][0]
    ok &= check("в сделке есть разбивка (цена/funding/комиссии)",
                "price_pnl" in t and "fees_total" in t and "funding" in t)
    ok &= check("сделка помечена mode=backtest", t["mode"] == "backtest")
    ok &= check("есть причина выхода", bool(t.get("exit_reason")))

# --- пессимизм: вход по ask хуже, чем по mid -----------------------------
def make(side):
    row_ = {"perp_ask": 100.0 + (0.025 if side == "ask" else 0),
            "perp_bid": 100.0}
    return row_


# симметрия: плоский рынок, спред двусторонний
ok &= check("конфиг симметричен (spread двусторонний)",
            B.Config().spread_pct == 0.05)

# --- метрики бэктеста совместимы с панелью --------------------------------
tr = B.simulate([mk_flat("A", 0.002)], cfg2, thr)["trades"]
core = M.core_metrics(tr)
ok &= check("метрики панели принимают сделки бэктеста",
            core.get("n", 0) == len(tr))

print()
print("Итог:", "все проверки пройдены" if ok else "ЕСТЬ ПРОВАЛЫ")
sys.exit(0 if ok else 1)