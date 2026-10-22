"""Тесты портфеля (несколько пар одновременно, --max-positions).

Что проверяем:
  • состояние: positions[] + legacy-зеркало position (старый файл читается);
  • размер: капитал пары урезается по свободной марже, < $50 — отказ;
  • вход: дубль символа и заполненный портфель блокируются ДО сети;
  • закрытие: --symbol / --all / отказ без флага при нескольких парах;
  • отчёты и healthcheck видят ВСЕ открытые пары.

Запуск: python test_portfolio.py  (stdlib, без pytest).
"""
from __future__ import annotations

import json
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
import healthcheck as hc       # noqa: E402
import dashboard as d          # noqa: E402

ok = True


def check(name, cond):
    global ok
    print(("OK   " if cond else "FAIL ") + name)
    ok = ok and bool(cond)


def exits(fn, *a, **kw) -> str:
    """Выполнить и вернуть текст SystemExit (пусто — не вышел)."""
    try:
        fn(*a, **kw)
    except SystemExit as e:
        return str(e)
    return ""


tmp = tempfile.mkdtemp()
fe.STATE_FILE = os.path.join(tmp, "position.json")
fe.LOG_FILE = os.path.join(tmp, "bot.log")
# изоляция: тест не должен трогать боевые memory.json и Telegram
fe.memory = None
fe._TG["enabled"] = False
POS_FILE = fe.STATE_FILE

E1 = {"symbol": "ETHUSDT", "spot_symbol": "ETHUSDT", "mode": "paper",
      "notional": 100.0, "margin": 50.0, "capital": 100.0,
      "perp_qty": 0.05, "spot_qty": 0.05, "perp_entry": 2000.0,
      "spot_entry": 2000.0, "opened_ms": 1, "funding_accrued": 0.0,
      "payouts": 0, "neg_streak": 0, "leverage": 2.0, "mult": 1.0}
B1 = {"symbol": "BTCUSDT", "spot_symbol": "BTCUSDT", "mode": "paper",
      "notional": 100.0, "margin": 50.0, "capital": 100.0,
      "perp_qty": 0.001, "spot_qty": 0.001, "perp_entry": 100000.0,
      "spot_entry": 100000.0, "opened_ms": 1, "funding_accrued": 0.0,
      "payouts": 0, "neg_streak": 0, "leverage": 2.0, "mult": 1.0}

# --- 1. состояние: новый формат, зеркало и чтение старого файла ------------
st = {"position": None, "positions": [E1, B1], "history": []}
fe.save_state(st)
raw = json.load(open(POS_FILE, encoding="utf-8"))
check("save_state: positions[] записан полностью", len(raw["positions"]) == 2)
check("save_state: position — зеркало ПЕРВОЙ пары",
      raw["position"]["symbol"] == "ETHUSDT")

with open(POS_FILE, "w", encoding="utf-8") as f:      # старый файл бота
    json.dump({"position": dict(E1), "history": []}, f)
legacy = fe.load_state()
check("load_state: старый файл (только position) → портфель из одной пары",
      [p["symbol"] for p in fe.positions_of(legacy)] == ["ETHUSDT"])
check("load_state: позиция не потерялась", legacy["position"]["symbol"] == "ETHUSDT")

fe.save_state({"position": None, "positions": [E1, B1], "history": []})
state = fe.load_state()
check("positions_of: две пары", len(fe.positions_of(state)) == 2)

# --- 2. выбор пары для закрытия -------------------------------------------
check("find_position: по символу", fe.find_position(state, symbol="BTCUSDT")
      is state["positions"][1])
check("find_position: без символа при портфеле → None (не угадываем)",
      fe.find_position(state) is None)
check("find_position: нет такого символа → None",
      fe.find_position(state, symbol="SOLUSDT") is None)

cli = types.SimpleNamespace(capital=100.0, mode="paper", max_positions=3,
                            symbol=None, close_all=False)
check("close_targets: без флагов при двух парах → отказ с подсказкой",
      "несколько позиций" in exits(fe.close_targets, state, cli)
      and "--all" in exits(fe.close_targets, state, cli))
cli.symbol = "BTCUSDT"
check("close_targets: --symbol → ровно эта пара",
      [p["symbol"] for p in fe.close_targets(state, cli)] == ["BTCUSDT"])
cli.symbol = "SOLUSDT"
check("close_targets: --symbol чужой пары → отказ",
      "нет SOLUSDT" in exits(fe.close_targets, state, cli))
cli.symbol = None
cli.close_all = True
check("close_targets: --all → все пары",
      [p["symbol"] for p in fe.close_targets(state, cli)] ==
      ["ETHUSDT", "BTCUSDT"])
cli.close_all = False
one = {"position": None, "positions": [E1], "history": []}
check("close_targets: одна пара без флагов → она же",
      [p["symbol"] for p in fe.close_targets(one, cli)] == ["ETHUSDT"])
check("close_targets: портфель пуст → отказ",
      "Позиции нет" in exits(fe.close_targets,
                             {"position": None, "positions": []}, cli))

# --- 3. размер позиции: свободная маржа и минимум -------------------------
c = types.SimpleNamespace(capital=100.0, max_positions=3)
free = fe.free_capital(c, {"position": None, "positions": [E1]})
check("free_capital: paper-счёт = capital × слоты минус занятое",
      abs(free - (300.0 - 150.0)) < 1e-9)
cap, why = fe.entry_capital(c, {"position": None, "positions": []})
check("entry_capital: свободно на всех → полный капитал пары",
      abs(cap - 100.0) < 1e-9 and why is None)
cap2, why2 = fe.entry_capital(c, {"position": None, "positions": [
    dict(E1, notional=80.0, margin=40.0), dict(B1, notional=80.0, margin=30.0)],
    "history": []})
# 3 слота × $100 = $300 эквити, занято $230 → свободно $70, делится с буфером
check("entry_capital: третья пара берёт урезанный остаток (не весь капитал)",
      why2 is None and 50.0 <= cap2 < 100.0)
# счёт занят почти полностью: новая пара не проходит минимум
c2 = types.SimpleNamespace(capital=100.0, max_positions=3)
full = {"position": None, "positions": [
    dict(E1, notional=90.0, margin=45.0), dict(B1, notional=90.0, margin=45.0)],
    "history": []}
cap3, why3 = fe.entry_capital(c2, full)
check("entry_capital: свободного меньше $50 → отказ с суммой",
      cap3 == 0.0 and why3 is not None and "свободной маржи" in why3)

# реальный счёт (balance задан): эквити − занятое
free_real = fe.free_capital(c2, {"position": None, "positions": [E1]},
                            balance=500.0)
check("free_capital: testnet считает по балансу биржи",
      abs(free_real - 350.0) < 1e-9)

# --- 4. вход блокируется до сети ------------------------------------------
fe.save_state(state)
cli_e = types.SimpleNamespace(mode="paper", max_positions=3,
                              confirm_live=False, capital=100.0)
check("open_position: дубль символа → отказ без ордеров",
      "--symbol" in exits(fe.open_position, cli_e, {"symbol": "ETHUSDT"}))
check("open_position: дубль определён ДО любых ключей/сети",
      "уже в портфеле" in exits(fe.open_position, cli_e, {"symbol": "ETHUSDT"}))

cli_full = types.SimpleNamespace(mode="paper", max_positions=2,
                                 confirm_live=False, capital=100.0)
check("open_position: портфель заполнен → отказ со списком пар",
      "Портфель заполнен" in exits(fe.open_position, cli_full,
                                   {"symbol": "SOLUSDT"}))
check("open_position: отказ называет занятые пары",
      "ETHUSDT" in exits(fe.open_position, cli_full, {"symbol": "SOLUSDT"}))

# --- 5. void: снятие конкретной пары --------------------------------------
vstate = {"position": None, "positions": [dict(E1), dict(B1)], "history": []}
cli_v = types.SimpleNamespace(symbol="ETHUSDT", close_all=False, force=True,
                              preset="hold", mode="paper", no_learn=True)
fe.void_position(cli_v, vstate, vstate["positions"][0])
check("void: снята ровно одна пара, вторая осталась",
      [p["symbol"] for p in fe.positions_of(vstate)] == ["BTCUSDT"])
check("void: зеркало position переехало на оставшуюся пару",
      vstate["position"]["symbol"] == "BTCUSDT")
check("void: в истории отмечен именно ETHUSDT",
      vstate["history"][-1]["action"] == "void"
      and vstate["history"][-1]["symbol"] == "ETHUSDT")

# --- 6. отчёты знают обо всех парах ---------------------------------------
fe.current_pnl = lambda pos, feed=None: (0.0, 0.0, 1.0)   # без сети
rep_state = {"position": None, "positions": [E1, B1], "history": []}
fe.save_state(rep_state)
text = fe.pnl_report({"position": None, "positions": [E1, B1], "history": []},
                     balance=1000.0)
check("pnl_report: строка на каждую пару",
      "ETHUSDT" in text and "BTCUSDT" in text)
check("pnl_report: в балансовой строке число открытых пар",
      "открытых 2" in text)
daily = fe.daily_report({"position": None, "positions": [E1, B1], "history": []})
check("daily_report: обе пары в сводке",
      "ETHUSDT" in daily and "BTCUSDT" in daily)

# --- 7. healthcheck сверяет каждую пару -----------------------------------
pj = os.path.join(tmp, "hc_position.json")
with open(pj, "w", encoding="utf-8") as f:
    json.dump({"position": dict(E1), "positions": [dict(E1), dict(B1)]}, f)
checks = hc.read_positions(pj)
check("healthcheck.read_positions: обе пары", len(checks) == 2)
check("healthcheck.read_position: первая пара (совместимость)",
      (hc.read_position(pj) or {}).get("symbol") == "ETHUSDT")
with open(pj, "w", encoding="utf-8") as f:
    json.dump({"position": dict(B1)}, f)      # старый формат
check("healthcheck.read_positions: старый файл → одна пара",
      [p["symbol"] for p in hc.read_positions(pj)] == ["BTCUSDT"])
with open(pj, "w", encoding="utf-8") as f:
    json.dump({"position": None, "positions": []}, f)
check("healthcheck.read_positions: пусто → []", hc.read_positions(pj) == [])

# --- 8. панель: все пары и закрытие выбранной -----------------------------
check("dashboard.positions_of: читает positions[]", len(d.positions_of(
    {"position": None, "positions": [E1, B1]})) == 2)
pstate = {"position": E1, "positions": [E1, B1]}
argv_close = d.action_argv("close", pstate, {"symbol": "btcusdt"})
check("панель: close получает --symbol пары и её режим",
      "--symbol" in argv_close and "BTCUSDT" in argv_close
      and argv_close[argv_close.index("--mode") + 1] == "paper")
argv_void = d.action_argv("void", pstate, {"symbol": "BTCUSDT"})
check("панель: void --force --symbol",
      argv_void[2] == "void" and "--force" in argv_void
      and "BTCUSDT" in argv_void)

d.CONFIG_FILE = os.path.join(tmp, "config.json")
with open(d.CONFIG_FILE, "w", encoding="utf-8") as f:
    json.dump({"max-positions": 3}, f)
check("панель: вход разрешён, когда есть свободный слот",
      d.validate_entry(pstate, {"symbol": "SOLUSDT",
                                "confirm": "SOLUSDT"}) is None)
check("панель: вход по уже открытой паре запрещён",
      "уже открыт" in (d.validate_entry(pstate, {"symbol": "ETHUSDT",
                                                 "confirm": "ETHUSDT"}) or ""))
with open(d.CONFIG_FILE, "w", encoding="utf-8") as f:
    json.dump({"max-positions": 2}, f)
check("панель: заполненный портфель → отказ со списком пар",
      "портфель заполнен" in (d.validate_entry(
          pstate, {"symbol": "SOLUSDT", "confirm": "SOLUSDT"}) or ""))

html = d.portfolio_block([E1, B1], [1.5, -2.0], [2000.0, 100000.0], "T0K")
check("панель: карточка на каждую пару",
      "ETHUSDT" in html and "BTCUSDT" in html)
check("панель: у каждой пары своя кнопка закрытия со символом",
      html.count("value='close'") == 2
      # close+void каждой карточки несёт символ пары
      and html.count("name='symbol'") == 4
      and html.count("value='ETHUSDT'") == 2
      and html.count("value='BTCUSDT'") == 2)
check("панель: health-check остаётся один на портфель",
      html.count("value='healthcheck'") == 1)
check("панель: статичная версия без токена — без кнопок",
      d.portfolio_block([E1], [1.0], [1.0], "") ==
      d.position_block(E1, 1.0, 1.0))

# --- 9. CLI: флаг и ключ конфига ------------------------------------------
import argparse  # noqa: E402
# реальный разбор флага (без чтения config.json) — через parse_args
p = argparse.ArgumentParser()
p.add_argument("--max-positions", dest="max_positions", type=int, default=1)
p.add_argument("--all", dest="close_all", action="store_true")
ns = p.parse_args(["--max-positions", "4", "--all"])
check("CLI: --max-positions и --all разбираются",
      ns.max_positions == 4 and ns.close_all is True)

# ключ конфига применяется к парсеру (как на VPS из config.json)
cfg_path = os.path.join(tmp, "cfg.json")
with open(cfg_path, "w", encoding="utf-8") as f:
    json.dump({"capital": 500, "max-positions": 3, "_заметка": "тест"}, f)
p2 = argparse.ArgumentParser()
p2.add_argument("--capital", type=float, default=1000.0)
p2.add_argument("--max-positions", dest="max_positions", type=int, default=1)
fe.apply_config(p2, argv=[], path=cfg_path)
ns2 = p2.parse_args([])
check("конфиг: max-positions подставляется из config.json",
      ns2.max_positions == 3)
check("конфиг: комментарии (_) не мешают",
      ns2.capital == 500)

# реальный config.json проекта принимается целиком (без «неизвестный параметр»)
real_cfg = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "config.json")
if os.path.exists(real_cfg):
    p3 = argparse.ArgumentParser()
    for flag, typ, dflt in (("--capital", float, 1000.0),
                            ("--leverage", float, 2.0),
                            ("--max-positions", int, 1),
                            ("--preset", str, "hold"),
                            ("--mode", str, "paper"),
                            ("--min-apy30", float, None),
                            ("--min-pos", float, None),
                            ("--min-basis", float, None),
                            ("--min-volume", float, 3.0),
                            ("--min-oi", float, 3.0),
                            ("--pass-oi", float, 10.0),
                            ("--fast-apy", float, 3.0),
                            ("--fast-top", int, 8),
                            ("--max-breakeven", float, None),
                            ("--interval", int, 15), ("--full-every", int, 300),
                            ("--hold-payouts", int, 2), ("--exit-neg", int, 3),
                            ("--max-loss", float, 5.0),
                            ("--max-margin-risk", float, 70.0),
                            ("--max-delta-pct", float, 1.0),
                            ("--integrity-every", float, 60.0),
                            ("--maker-wait", float, 6.0),
                            ("--max-slip-bps", float, 15.0),
                            ("--max-spread", float, 0.5),
                            ("--min-depth", float, 1.0),
                            ("--max-entry-basis-pct", float, 0.5),
                            ("--prefilter", int, 60),
                            ("--report-every", float, 60.0),
                            ("--daily-report-at", str, "00:00"),
                            ("--report-tz", str, ""),
                            ("--no-learn", None, False), ("--no-notify", None, False),
                            ("--no-auto", None, False), ("--ws", None, False),
                            ("--taker-only", None, False),
                            ("--no-exchange-stop", None, False),
                            ("--confirm-live", None, False)):
        if typ is None:
            p3.add_argument(flag, action="store_true")
        else:
            p3.add_argument(flag, type=typ, default=dflt)
    cfg_err = ""
    try:
        fe.apply_config(p3, argv=["--preset", "hold"], path=real_cfg)
        ns3 = p3.parse_args(["--preset", "hold"])
    except SystemExit as e:            # apply_config падает через sys.exit
        cfg_err, ns3 = str(e), types.SimpleNamespace(max_positions=None)
    # Проверяем НЕ конкретное число, а то, что config применяется как надо:
    # значение в файле доезжает до парсера. Само число — это данные
    # конкретной копии проекта (на VPS config.json старше и опции
    # max-positions там может не быть вовсе), а не инвариант кода.
    with open(real_cfg, encoding="utf-8") as f:
        real_data = json.load(f)
    want_mp = int(real_data.get("max-positions", 1))
    check("реальный config.json: max-positions принят без ошибок",
          not cfg_err and ns3.max_positions == want_mp)
print()
print("Итог:", "все проверки пройдены" if ok else "ЕСТЬ ПРОВАЛЫ")
sys.exit(0 if ok else 1)
