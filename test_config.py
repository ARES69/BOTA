"""Тесты конфиг-файла и ротации bot.log.
Запуск: python test_config.py  (stdlib, без pytest)."""
from __future__ import annotations

import argparse
import json
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
import funding_executor as fe  # noqa: E402

ok = True


def check(name, cond):
    global ok
    print(("OK   " if cond else "FAIL ") + name)
    ok = ok and bool(cond)


def mk_parser():
    # Перечень флагов повторяет боевой (урезанный набор давал ложную ошибку
    # «неизвестный параметр» на совершенно валидном config.example.json).
    p = argparse.ArgumentParser()
    p.add_argument("--capital", type=float, default=1000.0)
    p.add_argument("--max-positions", dest="max_positions", type=int, default=1)
    p.add_argument("--leverage", type=float, default=2.0)
    p.add_argument("--mode", default="paper",
                   choices=["paper", "testnet", "mainnet"])
    p.add_argument("--preset", default="hold",
                   choices=["hold", "spike", "mainnet"])
    p.add_argument("--ws", action="store_true")
    p.add_argument("--taker-only", action="store_true")
    p.add_argument("--no-notify", action="store_true")
    p.add_argument("--hold-payouts", type=int, default=2)
    p.add_argument("--confirm-live", action="store_true")
    p.add_argument("--no-auto", action="store_true")
    p.add_argument("--no-learn", action="store_true")
    p.add_argument("--no-exchange-stop", action="store_true")
    p.add_argument("--min-volume", type=float, default=3.0)
    p.add_argument("--min-oi", type=float, default=3.0)
    p.add_argument("--pass-oi", type=float, default=10.0)
    p.add_argument("--min-apy30", type=float, default=None)
    p.add_argument("--min-pos", type=float, default=None)
    p.add_argument("--min-basis", type=float, default=None)
    p.add_argument("--max-breakeven", type=float, default=None)
    p.add_argument("--prefilter", type=int, default=60)
    p.add_argument("--fast-apy", type=float, default=3.0)
    p.add_argument("--fast-top", type=int, default=8)
    p.add_argument("--exit-neg", type=int, default=3)
    p.add_argument("--max-loss", type=float, default=5.0)
    p.add_argument("--max-margin-risk", type=float, default=70.0)
    p.add_argument("--max-delta-pct", type=float, default=1.0)
    p.add_argument("--max-entry-basis-pct", type=float, default=0.5)
    p.add_argument("--integrity-every", type=float, default=60.0)
    p.add_argument("--report-every", type=float, default=60.0)
    p.add_argument("--daily-report-at", default="00:00")
    p.add_argument("--report-tz", default="")
    p.add_argument("--maker-wait", type=float, default=6.0)
    p.add_argument("--max-slip-bps", type=float, default=15.0)
    p.add_argument("--max-spread", type=float, default=0.5)
    p.add_argument("--min-depth", type=float, default=1.0)
    p.add_argument("--interval", type=int, default=15)
    p.add_argument("--full-every", type=int, default=300)
    return p


def write_cfg(data):
    d = tempfile.mkdtemp()
    path = os.path.join(d, "config.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)
    return path


# --- конфиг применяется ----------------------------------------------------
cfg = write_cfg({"capital": 3000, "leverage": 3.0, "mode": "mainnet",
                 "preset": "mainnet", "ws": True, "hold-payouts": 5,
                 "no-notify": False})
p = mk_parser()
fe.apply_config(p, argv=[], path=cfg)
cli = p.parse_args([])
check("числовые параметры из конфига", cli.capital == 3000
      and cli.leverage == 3.0 and cli.hold_payouts == 5)
check("строковые параметры остаются строками (mode/preset)",
      cli.mode == "mainnet" and cli.preset == "mainnet")
check("bool-параметры включаются", cli.ws is True)
check("bool-параметры с false остаются False", cli.no_notify is False)

# --- флаг сильнее конфига -------------------------------------------------
p = mk_parser()
fe.apply_config(p, argv=["--capital", "500", "--mode", "testnet"], path=cfg)
cli = p.parse_args(["--capital", "500", "--mode", "testnet"])
check("флаг в командной строке перекрывает конфиг",
      cli.capital == 500.0 and cli.mode == "testnet")
check("остальное осталось из конфига", cli.leverage == 3.0
      and cli.hold_payouts == 5)

# --- форма записи флага с "=" --------------------------------------------
p = mk_parser()
fe.apply_config(p, argv=["--capital=999"], path=cfg)
cli = p.parse_args(["--capital=999"])
check("форма --flag=value распознаётся как перекрытие", cli.capital == 999.0)

# --- ключи-комментарии (с "_") пропускаются -------------------------------
cfg_c = write_cfg({"_комментарий": "это пояснение", "_группа": "деньги",
                   "capital": 2500, "leverage": 1.5})
p = mk_parser()
fe.apply_config(p, argv=[], path=cfg_c)
cli = p.parse_args([])
check("комментарии с '_' пропускаются",
      cli.capital == 2500 and cli.leverage == 1.5)

# --- конфиг из примера в репозитории валиден ------------------------------
here = os.path.dirname(os.path.abspath(__file__))
example = os.path.join(here, "config.example.json")
if os.path.exists(example):
    p = mk_parser()
    try:
        fe.apply_config(p, argv=[], path=example)
        p.parse_args([])
        check("config.example.json загружается без ошибок", True)
    except SystemExit as e:
        check("config.example.json загружается без ошибок", False)
        print("   ", e)

# --- пороги вердикта из конфига доходят до фильтра -------------------------
# (apply_config знает новые флаги — иначе «неизвестный параметр» на старте;
#  значения не должны перекрываться дефолтом пресета hold)
cfg_thr = write_cfg({"min-apy30": 4.0, "min-pos": 70,
                     "max-breakeven": 30, "pass-oi": 3.0,
                     "fast-apy": 2.0, "report-every": 15})
p = mk_parser()
fe.apply_config(p, argv=[], path=cfg_thr)
cli = p.parse_args([])
check("пороги вердикта читаются из конфига",
      cli.min_apy30 == 4.0 and cli.min_pos == 70
      and cli.max_breakeven == 30 and cli.pass_oi == 3.0
      and cli.fast_apy == 2.0)
check("report-every (период таблицы PnL) читается из конфига",
      cli.report_every == 15)
check("без конфига отчёт о доходе раз в час (default 60)",
      mk_parser().parse_args([]).report_every == 60.0)

# --- отчёты: время сводки и часовой пояс -----------------------------------
cfg_rep = write_cfg({"daily-report-at": "03:15", "report-tz": "+05:00"})
p = mk_parser()
fe.apply_config(p, argv=[], path=cfg_rep)
cli_rep = p.parse_args([])
check("daily-report-at (время сводки) читается из конфига",
      cli_rep.daily_report_at == "03:15")
check("report-tz (часовой пояс отчётов) читается из конфига",
      cli_rep.report_tz == "+05:00")
cli_def_rep = mk_parser().parse_args([])
check("дефолты отчётов: сводка в 00:00, пояс — время сервера",
      cli_def_rep.daily_report_at == "00:00" and cli_def_rep.report_tz == "")
check("off выключает сводку (parse_hhmm → None)",
      fe.parse_hhmm("off") is None)

fa = fe.build_filter_args(argparse.Namespace(
    spot_fee=fe.DEFAULT_SPOT_FEE, perp_taker_fee=fe.DEFAULT_PERP_TAKER_FEE,
    perp_maker_fee=fe.DEFAULT_PERP_MAKER_FEE, preset="hold", no_learn=True,
    min_apy30=4.0, min_pos=70.0, min_basis=0.0, max_breakeven=30.0,
    fast_apy=2.0, pass_oi=3.0))
check("порог из конфига перекрывает дефолт пресета в build_filter_args",
      fa.min_apy30 == 4.0 and fa.min_pos == 70.0
      and fa.max_breakeven == 30.0 and fa.pass_oi == 3.0)
# fast_apy в namespace фильтра не попадает — быстрый скан читает его
# напрямую из cli (проверено выше: cli.fast_apy == 2.0 из конфига)
fa_def = fe.build_filter_args(argparse.Namespace(
    spot_fee=fe.DEFAULT_SPOT_FEE, perp_taker_fee=fe.DEFAULT_PERP_TAKER_FEE,
    perp_maker_fee=fe.DEFAULT_PERP_MAKER_FEE, preset="hold", no_learn=True))
check("без заданных порогов действует пресет hold (10/80/14)",
      fa_def.min_apy30 == 10.0 and fa_def.min_pos == 80.0
      and fa_def.max_breakeven == 14.0)

# --- ошибки конфига --------------------------------------------------------
bad = write_cfg({"несуществующий_параметр": 1})
try:
    fe.apply_config(mk_parser(), argv=[], path=bad)
    check("неизвестный параметр → понятная ошибка", False)
except SystemExit as e:
    check("неизвестный параметр → понятная ошибка",
          "неизвестный параметр" in str(e).lower()
          or "неизвестный" in str(e).lower())

broken = os.path.join(tempfile.mkdtemp(), "config.json")
with open(broken, "w", encoding="utf-8") as f:
    f.write("{ не json")
try:
    fe.apply_config(mk_parser(), argv=[], path=broken)
    check("битый JSON → понятная ошибка", False)
except SystemExit as e:
    check("битый JSON → понятная ошибка", "json" in str(e).lower())

lst = write_cfg([1, 2, 3])
try:
    fe.apply_config(mk_parser(), argv=[], path=lst)
    check("массив вместо объекта → ошибка", False)
except SystemExit as e:
    check("массив вместо объекта → ошибка", "объект" in str(e))

# отсутствующий файл — не ошибка (опциональный конфиг)
p = mk_parser()
fe.apply_config(p, argv=[], path=os.path.join(tempfile.mkdtemp(), "нет.json"))
check("отсутствие config.json не ломает запуск", p.parse_args([]).capital == 1000.0)

# --- ротация bot.log -------------------------------------------------------
d = tempfile.mkdtemp()
fe.LOG_FILE = os.path.join(d, "bot.log")
old_max, old_keep = fe.LOG_MAX_BYTES, fe.LOG_KEEP
fe.LOG_MAX_BYTES = 3000
fe.LOG_KEEP = 3
try:
    for i in range(80):
        fe.log(f"событие {i} " + "x" * 60)
    files = sorted(x for x in os.listdir(d) if x.startswith("bot.log"))
    check("ротация создаёт архивы", any(".1" in f for f in files))
    check("лог не превышает лимит", os.path.getsize(fe.LOG_FILE) < 4000)
    body = open(fe.LOG_FILE, encoding="utf-8").read()
    check("свежие события не потеряны (хвост перенесён)",
          "событие 79" in body)
    check("архивов не больше лимита",
          len([f for f in files if ".1" in f or ".2" in f or ".3" in f]) <= 3)
finally:
    fe.LOG_MAX_BYTES, fe.LOG_KEEP = old_max, old_keep

# ротация не падает на пустом/отсутствующем логе
d2 = tempfile.mkdtemp()
fe.LOG_FILE = os.path.join(d2, "нет-лога.log")
try:
    fe._rotate_log_if_needed()
    check("ротация без файла не падает", True)
except Exception as e:  # noqa: BLE001
    check("ротация без файла не падает", False)
    print("   ", e)

print()
print("Итог:", "все проверки пройдены" if ok else "ЕСТЬ ПРОВАЛЫ")
sys.exit(0 if ok else 1)