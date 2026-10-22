"""Тесты метрик панели: equity/drawdown, profit factor, разбивка PnL
(цена/funding/комиссии), риски позиции, здоровье бота.
Запуск: python test_dashboard_metrics.py  (stdlib, без pytest)."""
from __future__ import annotations

import os
import sys
import tempfile
import time
from datetime import datetime, timezone

# Консоль Windows по умолчанию cp1252 — печать кириллицы в check() роняла
# тест с UnicodeEncodeError при прямом запуске (через run_tests.py работало,
# потому что раннер выставляет PYTHONIOENCODING=utf-8).
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dashboard_metrics as m  # noqa: E402


def check(name, cond):
    print(("OK   " if cond else "FAIL ") + name)
    return bool(cond)


def trade(total=0.0, funding=0.0, held=24.0, sym="X", ts=None, **kw):
    t = {"action": "close", "symbol": sym, "mode": "paper",
         "held_hours": held, "pnl_excl_funding": total - funding,
         "funding": funding, "total": total, "return_pct": total / 1000 * 100}
    if ts is not None:
        t["ts"] = ts
    t.update(kw)
    return t


ok = True
NOW = int(time.time() * 1000)
D = 86_400_000

# --- equity / drawdown --------------------------------------------------
eq = m.equity_curve([trade(total=10), trade(total=-4), trade(total=6)])
ok &= check("equity: [0, 10, 6, 12]", eq == [0.0, 10.0, 6.0, 12.0])
dd = m.drawdown_series(eq)
ok &= check("drawdown: пик 10, падение до 6 = 4", dd == [0.0, 0.0, 4.0, 0.0])
mdd, at = m.max_drawdown(eq)
ok &= check("max_dd = 4 в точке 2", mdd == 4.0 and at == 2)
ok &= check("пустая кривая → 0 просадка", m.max_drawdown([0.0]) == (0.0, 0))

# --- core metrics -------------------------------------------------------
# 2 победы (+10, +6) и 2 убытка (-4, -2): net=10
tr = [trade(total=10, funding=12, held=24, ts=NOW - 4 * D),
      trade(total=-4, funding=-1, held=12, ts=NOW - 3 * D),
      trade(total=6, funding=8, held=48, ts=NOW - 2 * D),
      trade(total=-2, funding=0, held=6, ts=NOW - 1 * D)]
c = m.core_metrics(tr)
ok &= check("n=4, net=10", c["n"] == 4 and abs(c["net"] - 10.0) < 1e-9)
ok &= check("win_rate=50%", abs(c["win_rate"] - 0.5) < 1e-9)
ok &= check("gross_win=16, gross_loss=6",
            c["gross_win"] == 16.0 and c["gross_loss"] == 6.0)
ok &= check("profit_factor = 16/6 = 2.667",
            abs(c["profit_factor"] - 16 / 6) < 1e-9)
ok &= check("expectancy = 10/4 = 2.5", abs(c["expectancy"] - 2.5) < 1e-9)
ok &= check("avg_win=8, avg_loss=3",
            c["avg_win"] == 8.0 and c["avg_loss"] == 3.0)
ok &= check("payoff = 8/3", abs(c["payoff"] - 8 / 3) < 1e-9)
ok &= check("best=10, worst=-4", c["best"] == 10.0 and c["worst"] == -4.0)
ok &= check("max_dd=4 (10 → 6)", c["max_dd"] == 4.0)
ok &= check("recovery = net/max_dd = 2.5", abs(c["recovery"] - 2.5) < 1e-9)
ok &= check("пустая история не ломает", m.core_metrics([])["n"] == 0)

# только убытки → profit_factor = 0, recovery 0
# без побед → profit_factor = 0 (не «до бесконечности»), recovery 0
c2 = m.core_metrics([trade(total=-5), trade(total=-3)])
ok &= check("без побед → profit_factor=0 и recovery=0",
            c2["profit_factor"] == 0.0 and c2["recovery"] == 0.0)
# без убытков → profit_factor бесконечный
c3 = m.core_metrics([trade(total=5)])
ok &= check("без убытков → profit_factor=inf", c3["profit_factor"] == float("inf"))

# --- economics: главная таблица арбитража ------------------------------
tr = [trade(total=3.0, funding=5.0, price_pnl=0.2, fees_total=2.2,
            notional=600, capital=1000),
      trade(total=-1.0, funding=1.0, price_pnl=-0.3, fees_total=2.3,
            notional=600, capital=1000)]
e = m.economics(tr)
ok &= check("funding суммируется", abs(e["funding"] - 6.0) < 1e-9)
ok &= check("комиссии суммируются", abs(e["fees"] - 4.5) < 1e-9)
ok &= check("цена около нуля (нейтральность)", abs(e["price_pnl"] + 0.1) < 1e-6)
ok &= check("coverage = funding/fees = 6/4.5", abs(e["coverage"] - 6 / 4.5) < 1e-9)
ok &= check("funding > fees → coverage > 1 (ставка отбивает круг)",
            e["coverage"] > 1.0)
ok &= check("разбивка помечена как достоверная", e["decomposed"] == 2)

# старая запись без разбивки → комиссии оцениваются от нотиона
e2 = m.economics([trade(total=1.0, funding=2.0, notional=1000)])
ok &= check("старые записи: комиссия ≈ 0.31% нотиона",
            abs(e2["fees"] - 1000 * 0.0031) < 1e-6)
ok &= check("старые записи помечены как неразложенные", e2["decomposed"] == 0)

# фандинг НЕ отбивает комиссии — главный сигнал тревоги
e3 = m.economics([trade(total=-2.0, funding=1.0, price_pnl=0.0,
                        fees_total=3.0, notional=600)])
ok &= check("coverage < 1 → ставка не отбивает круг", e3["coverage"] < 1.0)

# --- per_symbol ---------------------------------------------------------
ps = m.per_symbol([trade(total=5, sym="BTCUSDT", held=10),
                   trade(total=-2, sym="BTCUSDT", held=20),
                   trade(total=3, sym="ETHUSDT", held=30)])
ok &= check("символов 2", len(ps) == 2)
btc = next(r for r in ps if r["symbol"] == "BTCUSDT")
eth = next(r for r in ps if r["symbol"] == "ETHUSDT")
ok &= check("BTC: 2 сделки, net=3, win 50%",
            btc["n"] == 2 and btc["net"] == 3.0 and btc["win_rate"] == 0.5)
ok &= check("сортировка по net (порядок неважен, сумма 6)",
            abs(sum(r["net"] for r in ps) - 6.0) < 1e-9
            and all(ps[i]["net"] >= ps[i + 1]["net"] for i in range(len(ps) - 1)))
ok &= check("avg_hold считается", btc["avg_hold_h"] == 15.0)

# --- hold buckets -------------------------------------------------------
hb = m.hold_buckets([trade(total=1, held=2), trade(total=2, held=12),
                     trade(total=3, held=48), trade(total=4, held=100),
                     trade(total=5, held=300)])
ok &= check("5 корзин", len(hb) == 5)
ok &= check("по одной сделке в каждой", all(b["n"] == 1 for b in hb))
ok &= check("<6ч и 6-24ч разделены", hb[0]["n"] == 1 and hb[1]["n"] == 1)

# --- position risk ------------------------------------------------------
pos = {"symbol": "BTCUSDT", "perp_entry": 100.0, "perp_qty": 1.0,
       "margin": 50.0, "leverage": 2.0, "stop_price": 110.0,
       "stop_armed": True}
r = m.position_risk(pos, 105.0)
ok &= check("движение +5%", abs(r["move_pct"] - 5.0) < 1e-9)
ok &= check("шорт в убытке на -5$", abs(r["unrealized"] - (-5.0)) < 1e-9)
ok &= check("маржа: 5/50 = 10%", abs(r["margin_used_pct"] - 10.0) < 1e-9)
ok &= check("до стопа +4.76%", abs(r["to_stop_pct"] - (110 / 105 - 1) * 100) < 1e-6)
ok &= check("убыток при стопе = 10$", abs(r["loss_at_stop"] - 10.0) < 1e-9)
ok &= check("оценка ликвидации ≈ 100%/L", r["liq_est_pct"] == -50.0)
ok &= check("без стопа → to_stop_pct=None",
            m.position_risk({**pos, "stop_price": None, "stop_armed": False},
                            105.0)["to_stop_pct"] is None)
ok &= check("без цены → пустой результат", m.position_risk(pos, None) == {})
ok &= check("без позиции → пустой результат", m.position_risk(None, 105.0) == {})

# --- bot health ---------------------------------------------------------
d = tempfile.mkdtemp()
lp = os.path.join(d, "bot.log")
now_txt = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")
open(lp, "w", encoding="utf-8").write(
    f"[{now_txt}] HOLD BTCUSDT pnl=$+0.50 funding=$+0.12\n"
    f"[{now_txt}] ошибка цикла: тест\n")
h = m.bot_health(lp, {"position": None})
ok &= check("бот жив (свежее событие)", h["alive"] is True)
ok &= check("найдено тревожных строк", h["warns"] >= 1)
ok &= check("последняя ошибка найдена", h["last_error"] is not None)

# старый лог → бот не жив
open(lp, "w", encoding="utf-8").write("[2020-01-01 00:00:00] HOLD X\n")
ok &= check("старый лог → alive=False", m.bot_health(lp, {})["alive"] is False)
ok &= check("нет файла → alive=None", m.bot_health(os.path.join(d, "no.log"), {})["alive"] is None)
ok &= check("битые строки не ломают", m.bot_health(lp, {})["last_event_age_s"] is not None)

print()
print("Итог:", "все проверки пройдены" if ok else "ЕСТЬ ПРОВАЛЫ")
sys.exit(0 if ok else 1)