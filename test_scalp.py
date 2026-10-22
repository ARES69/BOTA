"""Тесты скальп-режима (--strategy scalp): EMA, сигнал по кроссу, выход
TP/SL/время, экономика комиссий, PnL, state и роутинг команд.
Запуск: python test_scalp.py  (stdlib, без pytest)."""
from __future__ import annotations

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

SCALP_MEM = os.path.join(tempfile.gettempdir(), "test_scalp_state.json")
if os.path.exists(SCALP_MEM):
    os.remove(SCALP_MEM)
fe.SCALP_STATE_FILE = SCALP_MEM


def check(name, cond):
    print(("OK   " if cond else "FAIL ") + name)
    return cond


def approx(a, b, tol=1e-6):
    return abs(a - b) <= tol


def ns(**kw):
    return types.SimpleNamespace(**kw)


def main() -> int:
    ok = True
    now_ms = 1_700_000_000_000

    # 1. EMA: на константном ряду EMA == константе; длина верна
    e = fe.ema([100.0] * 30, 9)
    ok &= check("ema: длина ряда", len(e) == 30)
    ok &= check("ema: константа остаётся константой", approx(e[-1], 100.0))
    ok &= check("ema: пустой вход → []", fe.ema([], 9) == [])

    # 2. EMA реагирует на скачок быстрее при меньшем периоде
    rising = [100.0] * 20 + [130.0]
    ef = fe.ema(rising, 9)[-1]
    es = fe.ema(rising, 21)[-1]
    ok &= check("ema: fast реагирует сильнее slow при росте", ef > es > 100.0)

    # 3. сигнал: пересечение вверх → long, вниз → short, флэт → None
    up = [100.0] * 40 + [130.0]
    down = [100.0] * 40 + [70.0]
    flat = [100.0] * 45
    sig_up, _ = fe.scalp_signal(up, 9, 21)
    sig_down, _ = fe.scalp_signal(down, 9, 21)
    sig_flat, _ = fe.scalp_signal(flat, 9, 21)
    ok &= check("сигнал: cross вверх → long", sig_up == "long")
    ok &= check("сигнал: cross вниз → short", sig_down == "short")
    ok &= check("сигнал: флэт → нет сигнала", sig_flat is None)
    sig_short, info = fe.scalp_signal([100.0] * 5, 9, 21)
    ok &= check("сигнал: мало свечей → None + причина",
                sig_short is None and "reason" in info)

    # 4. экономика: gross/net в базисных пунктах
    ok &= check("gross: лонг +0.5% = 50 б.п.",
                approx(fe.scalp_gross_bps("long", 100.0, 100.5), 50.0))
    ok &= check("gross: шорт растёт при падении цены",
                approx(fe.scalp_gross_bps("short", 100.0, 99.5), 50.0))
    ok &= check("gross: шорт падает при росте цены",
                approx(fe.scalp_gross_bps("short", 100.0, 100.5), -50.0))
    ok &= check("net = gross − комиссии",
                approx(fe.scalp_net_bps("long", 100.0, 100.5, 11.0), 39.0))

    # 5. комиссии по умолчанию = 2 × taker (%)
    fee = fe.scalp_fee_bps(ns(perp_taker_fee=0.055, scalp_fee_bps=None))
    ok &= check("fee: 2×0.055% = 11 б.п.", approx(fee, 11.0))
    ok &= check("fee: явный override сильнее",
                approx(fe.scalp_fee_bps(ns(perp_taker_fee=0.055,
                                           scalp_fee_bps=7.0)), 7.0))

    # 6. выход: take-profit / stop-loss / время / держим
    pos = {"side": "long", "entry": 100.0, "qty": 1.0, "opened_ms": now_ms}
    ex_tp, why_tp = fe.decide_scalp_exit(pos, 100.5, now_ms, 20, 15, 5, 11.0)
    ok &= check("выход: TP при net ≥ цели", ex_tp and "take-profit" in why_tp)
    ex_sl, why_sl = fe.decide_scalp_exit(pos, 99.9, now_ms, 20, 15, 5, 11.0)
    ok &= check("выход: SL при net ≤ −стоп", ex_sl and "stop-loss" in why_sl)
    ex_no, why_no = fe.decide_scalp_exit(pos, 100.05, now_ms + 60_000, 20, 15, 5, 11.0)
    ok &= check("выход: держим внутри коридора", not ex_no)
    # ровно на границе TP (net == tp) — выходим (>=)
    ex_eq, _ = fe.decide_scalp_exit(pos, 100.31, now_ms, 20, 15, 5, 11.0)
    ok &= check("выход: net ровно на цели срабатывает", ex_eq)
    # время
    ex_t, why_t = fe.decide_scalp_exit(pos, 100.0, now_ms + 6 * 60_000, 20, 15, 5, 11.0)
    ok &= check("выход: истекло время удержания", ex_t and "время" in why_t)
    # шорт на падении — прибыль
    pos_s = {"side": "short", "entry": 100.0, "qty": 1.0, "opened_ms": now_ms}
    ex_sshort, _ = fe.decide_scalp_exit(pos_s, 99.5, now_ms, 20, 15, 5, 11.0)
    ok &= check("выход: шорт ловит падение (TP)", ex_sshort)

    # 7. PnL с комиссиями: gross/fees/net
    pnl_l, gross_l, fees_l = fe.scalp_pnl(pos, 100.5, 11.0)
    ok &= check("pnl: лонг gross = 0.5$", approx(gross_l, 0.5))
    ok &= check("pnl: fees ~ (100+100.5)·5.5bps",
                approx(fees_l, (100.0 + 100.5) * 5.5 / 1e4))
    ok &= check("pnl: net = gross − fees", approx(pnl_l, 0.5 - fees_l))
    pnl_s, gross_s, _ = fe.scalp_pnl(pos_s, 99.5, 11.0)
    ok &= check("pnl: шорт в плюсе при падении", gross_s > 0 and pnl_s > 0)
    pnl_loss, _, _ = fe.scalp_pnl(pos, 99.9, 11.0)
    ok &= check("pnl: убыток при неблагоприятном движении", pnl_loss < 0)

    # 8. state: round-trip save→load
    st = {"position": pos, "history": [{"action": "enter"}]}
    fe.save_scalp(st)
    loaded = fe.load_scalp()
    ok &= check("state: сохранение/чтение позиции",
                loaded["position"]["entry"] == 100.0
                and len(loaded["history"]) == 1)
    st2 = fe.load_scalp()
    st2["position"] = None
    fe.save_scalp(st2)
    ok &= check("state: пустая позиция после закрытия",
                fe.load_scalp()["position"] is None)

    # 9. симвoлы: разбор строки, пробелы/регистр/пустые
    ok &= check("symbols: нормализация строки",
                fe.scalp_symbols(ns(scalp_symbols=" btcusdt , ethusdt,,"))
                == ["BTCUSDT", "ETHUSDT"])

    # 10. роутинг: watch/status уходят в скальп-ветку при --strategy scalp
    sentinel_w = object()
    saved_w = fe.cmd_watch_scalp
    fe.cmd_watch_scalp = lambda cli: sentinel_w
    ok &= check("роутинг: watch --strategy scalp → скальп-цикл",
                fe.cmd_watch(ns(strategy="scalp")) is sentinel_w)
    fe.cmd_watch_scalp = saved_w

    sentinel_s = object()
    saved_s = fe.cmd_scalp_status
    fe.cmd_scalp_status = lambda cli: sentinel_s
    ok &= check("роутинг: status --strategy scalp → статус скальпера",
                fe.cmd_status(ns(strategy="scalp")) is sentinel_s)
    fe.cmd_scalp_status = saved_s

    print("\nИтог: " + ("все проверки пройдены" if ok else "ЕСТЬ ПРОВАЛЫ"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
