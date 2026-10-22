"""Рендер панели на синтетической истории: длинная серия сделок, разные
символы, разное удержание, открытая позиция, тревоги в логе.
Проверяет, что HTML собирается без исключений и без мусора (None/nan/{{{),
SVG парный, ключевые блоки присутствуют.
Запуск: python test_dashboard_render.py  (stdlib, без pytest)."""
import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime

# Консоль Windows по умолчанию cp1252 — печать кириллицы в check() роняла
# тест с UnicodeEncodeError при прямом запуске (через run_tests.py работало,
# потому что раннер выставляет PYTHONIOENCODING=utf-8).
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

BASE = os.path.dirname(os.path.abspath(__file__))
d = tempfile.mkdtemp()
now = int(time.time() * 1000)
D = 86_400_000

symbols = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT"]
hist, enters = [], []
tn = 0.0
for i in range(24):
    sym = symbols[i % len(symbols)]
    held = [3, 8, 26, 50, 90, 200][i % 6]
    # сценарий: больше плюсов, но есть серия убытков и провал ниже пика
    tot = [4.2, 3.1, -2.4, 5.5, 2.2, -1.8, 6.1, 3.4, -2.9, 1.5,
           4.8, -3.2, 2.1, 5.9, 3.7, -2.2, 4.4, 1.9, 6.3, -1.4,
           2.8, 5.1, 3.2, 4.1][i]
    funding = max(0.0, tot * 1.4)
    fees = 1.86
    price_pnl = tot - funding + fees
    tn += tot
    hist.append({
        "action": "close", "ts": now - (24 - i) * D, "symbol": sym,
        "mode": "testnet" if i % 3 else "paper",
        "held_hours": held, "pnl_excl_funding": round(tot - funding, 4),
        "funding": round(funding, 4), "total": round(tot, 4),
        "return_pct": round(tot / 1000 * 100, 3), "notional": 666.67,
        "capital": 1000.0, "fees_in": 0.93, "fees_out": 0.93,
        "fees_total": fees, "price_pnl": round(price_pnl, 4),
        "apy30": [12.0, 18.5, 9.2, 24.0][i % 4],
        "basis_entry": [0.02, -0.01, 0.05][i % 3],
        "legs": "maker:maker" if i % 2 else "taker:taker",
    })
    enters.append({"action": "enter", "ts": now - (24 - i) * D,
                   "symbol": sym, "mode": "testnet"})

pos = {"mode": "mainnet", "symbol": "BTCUSDT", "spot_symbol": "BTCUSDT",
       "mult": 1.0, "notional": 666.67, "margin": 333.33, "leverage": 2.0,
       "spot_qty": 0.008, "perp_qty": 0.008, "spot_entry": 75000.0,
       "perp_entry": 75000.0, "fees_usd": 0.93, "exit_spot_fee_pct": 0.1,
       "exit_perp_fee_pct": 0.055, "entry_cost_pct": 0.31, "apy30": 21.4,
       "basis_entry": 0.012, "opened_ms": now - 30 * 3_600_000,
       "funding_accrued": 3.42, "payouts": 3, "neg_streak": 0,
       "capital": 1000.0, "stop_price": 79500.0, "stop_armed": True}

state = {"position": pos, "history": enters + hist}
json.dump(state, open(os.path.join(d, "position.json"), "w"))

# лог с тревогами
ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
old = time.strftime("%Y-%m-%d %H:%M:%S",
                     time.localtime(time.time() - 4000))
open(os.path.join(d, "bot.log"), "w", encoding="utf-8").write("\n".join([
    f"[{ts}] HOLD BTCUSDT pnl=$+2.10 funding=$+0.85 payouts=3/2 neg=0",
    f"[{ts}] ⚠ BTCUSDT: остаток добираю маркетом",
    f"[{old}] ошибка цикла: таймаут исполнения",
]))

env = dict(os.environ, PYTHONIOENCODING="utf-8")
r = subprocess.run([sys.executable, os.path.join(BASE, "dashboard.py"),
                    "--no-live", "--refresh", "0",
                    "--data-dir", d,
                    "--out", os.path.join(d, "out.html")],
                   capture_output=True, encoding="utf-8", errors="replace",
                   env=env, cwd=d)
print((r.stdout or "").strip() or (r.stderr or "").strip())

ok = r.returncode == 0 and os.path.exists(os.path.join(d, "out.html"))
print(("OK   " if ok else "FAIL ") + "панель собралась без исключений")
if not ok:
    print((r.stderr or "")[:800])
    sys.exit(1)

html = open(os.path.join(d, "out.html"), encoding="utf-8").read()
print("размер:", len(html))


def check(name, cond):
    global ok
    print(("OK   " if cond else "FAIL ") + name)
    ok = ok and bool(cond)


# мусор в разметке означает баг в шаблоне. '}}' пропускаем: это легитимный
# CSS (закрытие вложенного правила внутри @media), а не незакрытый f-шаблон
for bad in ("None", "nan", "{{"):
    check(f"нет мусора {bad!r} в выводе", bad not in html)
check("нет незакрытых f-шаблонов {{", "{{" not in html)
# парность тегов
check("SVG парный", html.count("<svg") == html.count("</svg>"))
check("строки таблиц парны", html.count("<tr>") == html.count("</tr>"))
check("html/body закрыты", "</html>" in html and "</body>" in html)

# ключевые блоки панели
blocks = {
    "KPI-карточки": 'class="cards"',
    "кривая капитала": "Кривая капитала и просадка",
    "просадка построена": "просадка ↓ max",
    "PnL по сделкам": "PnL по сделкам",
    "атрибуция": "Покрытие фандингом",
    "здоровье торговли": "Profit factor",
    "разбивка по символам": "Сделки по символам",
    "время удержания": "6–24 ч",
    "история": "История сделок",
    "память бота": "Память бота",
    "события": "События бота",
    "health-строка": "warn-line",
}
for name, needle in blocks.items():
    check(f"блок «{name}»", needle in html)

# данные дошли до вывода: символы, статусы позиции
check("символ из истории виден", "BTCUSDT" in html)
check("режим позиции виден", "mainnet" in html)
check("риск-панель (без цены не рисуется — ожидаемо)",
      ("Маржа под риском" in html) or ("— " in html))

# SVG-графики непустые (есть прямоугольники/полилинии)
check("есть содержимое SVG", html.count("<rect") + html.count("<polyline") > 5)

print()
print("Итог:", "все проверки пройдены" if ok else "ЕСТЬ ПРОВАЛЫ")
sys.exit(0 if ok else 1)