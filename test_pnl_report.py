"""Тесты отчётов о доходе: часового (--report-every) и ежедневной сводки.

pnl_report(state)  — текстовая таблица: баланс счёта, результат за день,
открытая позиция с живым PnL + таблица закрытых сделок с итогом;
подозрительные записи (trade_sanity) не попадают ни в таблицу, ни в итоги.
maybe_pnl_report() — таймер рассылки: период в минутах, 0 — выключено,
сбой формата/сети не роняет вызывающий цикл.
daily_report / maybe_daily_report — сводка раз в сутки в --daily-report-at
в поясе --report-tz: баланс, итог дня (с открытой) и недели, лучшая/худшая
сделка дня; день отправки пишется в state, повтор не отправляется.

Запуск: python test_pnl_report.py  (stdlib, без pytest).
"""
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
import funding_executor as fe  # noqa: E402

# лог отчёта пишем во временную папку — тест не мусорит в боевой bot.log
fe.LOG_FILE = os.path.join(tempfile.mkdtemp(), "bot.log")

ok = True


def check(name, cond):
    global ok
    print(("OK   " if cond else "FAIL ") + name)
    ok = ok and bool(cond)


NOW = 1_760_000_000_000          # фиксированное «сейчас», мс
DAY = 86_400_000
HOUR = 3_600_000


def trade(ts, symbol="BTCUSDT", mode="testnet", held=8.5, price=0.51,
          funding=0.42, total=0.93, ret=0.19, **kw):
    r = {"action": "close", "ts": ts, "symbol": symbol, "mode": mode,
         "held_hours": held, "pnl_excl_funding": price, "funding": funding,
         "total": total, "return_pct": ret}
    r.update(kw)
    return r


# --- таблица: закрытые сделки и итоги --------------------------------------
hist = [
    trade(NOW - DAY - HOUR, "BTCUSDT", held=8.5, price=0.51, funding=0.42,
          total=0.93, ret=0.19),
    trade(NOW - 2 * HOUR, "ETHUSDT", held=2.0, price=-0.30, funding=0.10,
          total=-0.20, ret=-0.04),
    # подозрительная (нулевое удержание без выплат + заметный плюс) —
    # в итоги и в таблицу не должна попасть
    trade(NOW - 60_000, "XRPUSDT", held=0.0, price=5.0, funding=0.0,
          total=5.0, ret=5.0),
]
state = {"position": None, "history": hist}
rep = fe.pnl_report(state, now_ms=NOW)

check("заголовок с датой отчёта", rep.startswith("📊 ДОХОД/PnL ·"))
check("нет позиции — сказано явно", "Открытой позиции нет" in rep)
check("сделки в таблице (BTC и ETH)",
      "BTCUSDT" in rep and "ETHUSDT" in rep)
check("подозрительная запись в таблицу не попадает", "XRPUSDT" not in rep)
check("итог считается по не-подозрительным (цена +0.21 + funding +0.52)",
      "+0.21" in rep and "+0.52" in rep)
check("итоговая сумма +0.73 (не +5.73 с фантомом)",
      "+0.73" in rep and "5.73" not in rep)
check("предупреждение об исключённой записи", "подозрительных" in rep)
check("винрейт по 2 нормальным сделкам = 50%", "винрейт 50%" in rep)
check("колонки таблицы на месте",
      all(h in rep for h in ("дата", "символ", "цена$", "фонд$", "итого$")))

# сделок нет — отчёт не падает
rep0 = fe.pnl_report({"position": None, "history": []}, now_ms=NOW)
check("без сделок — понятный текст, без таблицы",
      "Закрытых сделок ещё нет" in rep0 and "итого" not in rep0)

# --- открытая позиция с живым PnL -----------------------------------------
pos = {"symbol": "BTCUSDT", "mode": "testnet", "opened_ms": NOW - 3 * HOUR,
       "funding_accrued": 0.0042, "payouts": 1, "capital": 500.0}
orig_pnl = fe.current_pnl
fe.current_pnl = lambda p, feed=None: (2540.0, 2500.0, -1.10)
try:
    rep2 = fe.pnl_report({"position": pos, "history": hist}, now_ms=NOW)
finally:
    fe.current_pnl = orig_pnl
check("открытая позиция в отчёте", "Открытая: BTCUSDT (testnet)" in rep2)
check("живой PnL в отчёте", "-1.10" in rep2)
check("funding и выплаты в отчёте",
      "+0.0042" in rep2 and "выплат 1" in rep2)
check("удержание в часах", "удержано 3.0 ч" in rep2)

# биржа недоступна — отчёт всё равно строится
def _boom(pos, feed=None):
    raise TimeoutError("сеть")


fe.current_pnl = _boom
try:
    rep3 = fe.pnl_report({"position": pos, "history": []}, now_ms=NOW)
finally:
    fe.current_pnl = orig_pnl
check("сбой котировки не ломает отчёт (PnL н/д)", "н/д" in rep3)

# --- счёт и результат за день ---------------------------------------------
# без позиции: за день = реализованный PnL только сегодняшних закрытий
rep_bal = fe.pnl_report({"position": None, "history": hist}, now_ms=NOW,
                        balance=612.34)
check("баланс счёта в отчёте", "Счёт: $612.34 USDT" in rep_bal)
check("результат за день (только сегодняшняя сделка -0.20)",
      "за день -0.20 $" in rep_bal)
check("процент за день от баланса на начало дня",
      "(-0.03%)" in rep_bal)
check("без позиции нет строки «открытая»", "· открытая" not in rep_bal)

rep_nobal = fe.pnl_report({"position": None, "history": hist}, now_ms=NOW)
check("баланс недоступен — явное «н/д»", "Счёт: н/д" in rep_nobal)
check("без баланса процента нет", "%)" not in rep_nobal.split("\n")[1])

fe.current_pnl = lambda p, feed=None: (2540.0, 2500.0, -1.10)
try:
    rep_day = fe.pnl_report({"position": pos, "history": hist}, now_ms=NOW,
                            balance=612.34)
finally:
    fe.current_pnl = orig_pnl
check("за день = реализовано -0.20 + живой PnL -1.10 = -1.30",
      "за день -1.30 $" in rep_day)
check("живой PnL открытой в строке счёта", "открытая -1.10 $" in rep_day)

# account_balance: paper → None; testnet → значение из клиента; сбой → None
check("account_balance: paper → None",
      fe.account_balance(fe.argparse.Namespace(mode="paper")) is None)


class _FakeClient:
    def wallet_balance(self):
        return 777.5


orig_make = fe.make_client
try:
    fe.make_client = lambda mode: _FakeClient()
    check("account_balance: testnet → эквити счёта",
          fe.account_balance(fe.argparse.Namespace(mode="testnet")) == 777.5)

    class _BoomClient:
        def wallet_balance(self):
            raise TimeoutError("сеть")

    fe.make_client = lambda mode: _BoomClient()
    check("account_balance: сбой сети → None (отчёт живёт)",
          fe.account_balance(fe.argparse.Namespace(mode="mainnet")) is None)
finally:
    fe.make_client = orig_make

# --- таймер maybe_pnl_report ------------------------------------------------
sent = []
orig_notify, orig_report = fe.notify, fe.pnl_report
fe.notify = lambda msg, key=None, min_gap=0.0: sent.append(msg)
cli = fe.argparse.Namespace(report_every=30)
try:
    t = fe.maybe_pnl_report({"position": None, "history": hist}, cli, None,
                            last_report=0.0, now=5000.0)
    check("первый отчёт уходит сразу (last_report=0)", len(sent) == 1)
    check("время последнего отчёта обновилось", t == 5000.0)

    t2 = fe.maybe_pnl_report({"position": None, "history": hist}, cli, None,
                              last_report=t, now=5000.0 + 60)
    check("раньше срока — не шлём (прошла 1 минута)", len(sent) == 1)
    check("время не сдвинулось без отправки", t2 == t)

    t3 = fe.maybe_pnl_report({"position": None, "history": hist}, cli, None,
                              last_report=t, now=5000.0 + 30 * 60)
    check("через 30 минут — отчёт уходит",
          len(sent) == 2 and t3 == 5000.0 + 1800)
    check("в отчёте таблица дохода", "ДОХОД/PnL" in sent[-1])

    # часовой период + баланс, который таймер берёт сам
    cli60 = fe.argparse.Namespace(report_every=60)
    orig_bal = fe.account_balance
    fe.account_balance = lambda _cli: 612.34
    try:
        t6 = fe.maybe_pnl_report({"position": None, "history": hist}, cli60,
                                  None, last_report=t3, now=t3 + 3000)
        check("report-every=60: через 50 минут не шлём",
              len(sent) == 2 and t6 == t3)
        t7 = fe.maybe_pnl_report({"position": None, "history": hist}, cli60,
                                  None, last_report=t3, now=t3 + 3600)
        check("report-every=60: через час — отчёт уходит",
              len(sent) == 3 and t7 == t3 + 3600)
        check("в часовом отчёте баланс счёта", "Счёт: $612.34" in sent[-1])
    finally:
        fe.account_balance = orig_bal

    cli0 = fe.argparse.Namespace(report_every=0)
    t4 = fe.maybe_pnl_report({"position": None, "history": hist}, cli0, None,
                              last_report=0.0, now=10_000.0)
    check("report-every = 0 — рассылка выключена", len(sent) == 3 and t4 == 0.0)

    # сбой формата/сети не должен ронять вызывающий цикл
    fe.pnl_report = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    t5 = fe.maybe_pnl_report({"position": None, "history": []}, cli, None,
                              last_report=0.0, now=20_000.0)
    check("ошибка отчёта не поднимает исключение (цикл жив)",
          t5 == 20_000.0 and len(sent) == 3)
finally:
    fe.notify, fe.pnl_report = orig_notify, orig_report

# --- ежедневная сводка: время, пояс, день/неделя ---------------------------
# Свой «сейчас»: суббота 17.10.2026 00:30 MSK (+03:00) — тогда границы дня и
# недели проверяются точно, без зависимости от пояса машины.
from datetime import datetime, timedelta, timezone  # noqa: E402

TZ3 = timezone(timedelta(hours=3))
NOW3 = int(datetime(2026, 10, 17, 0, 30, tzinfo=TZ3).timestamp() * 1000)
DAY3 = NOW3 - int(20 * 60 * 1000)              # 17.10 00:10 MSK
PREV3 = int(datetime(2026, 10, 16, 23, 50, tzinfo=TZ3).timestamp() * 1000)
MON3 = int(datetime(2026, 10, 12, 10, 0, tzinfo=TZ3).timestamp() * 1000)
OLD3 = int(datetime(2026, 10, 11, 12, 0, tzinfo=TZ3).timestamp() * 1000)

check("parse_hhmm: '00:00'/'7'/'23:59' → время",
      fe.parse_hhmm("00:00") == (0, 0) and fe.parse_hhmm("7") == (7, 0)
      and fe.parse_hhmm("23:59") == (23, 59))
check("parse_hhmm: off/пусто/мусор → None (выкл.)",
      fe.parse_hhmm("off") is None and fe.parse_hhmm("") is None
      and fe.parse_hhmm("abc") is None and fe.parse_hhmm("25:00") is None
      and fe.parse_hhmm("7:60") is None)

check("parse_tz: пусто/local → время сервера",
      fe.parse_tz("") is None and fe.parse_tz("local") is None)
check("parse_tz: '+03:00' и '-5' → фиксированный сдвиг",
      fe.parse_tz("+03:00").utcoffset(None) == timedelta(hours=3)
      and fe.parse_tz("-5").utcoffset(None) == -timedelta(hours=5))
moscow = fe.parse_tz("Europe/Moscow")
check("parse_tz: имя IANA разобрано (или мягкий откат без исключения)",
      moscow is None or getattr(moscow, "key", "") == "Europe/Moscow")
check("parse_tz: мусор → None без исключения",
      fe.parse_tz("Not/AZone") is None and fe.parse_tz("+99:99") is None)

# границы: 17.10 00:30 MSK → день начался в 16.10 21:00 UTC, неделя в понедельник
DAY_MS = fe.period_start_ms("day", NOW3 / 1000, TZ3)
WEEK_MS = fe.period_start_ms("week", NOW3 / 1000, TZ3)
check("period_start_ms: начало дня — полночь в поясе отчёта",
      DAY_MS == datetime(2026, 10, 17, 0, 0, tzinfo=TZ3).timestamp() * 1000)
check("period_start_ms: неделя — с понедельника той же недели",
      WEEK_MS == datetime(2026, 10, 12, 0, 0, tzinfo=TZ3).timestamp() * 1000)
check("period_start_ms: без пояса — начало локального дня (не в будущем)",
      fe.period_start_ms("day", NOW3 / 1000) <= NOW3)

agg = fe.aggregate_closes(
    [{"ts": DAY3, "total": 1.0, "pnl_excl_funding": 0.6, "funding": 0.4},
     {"ts": PREV3, "total": 0.5, "pnl_excl_funding": 0.5, "funding": 0.0},
     {"ts": MON3, "total": -0.3, "pnl_excl_funding": -0.2, "funding": -0.1},
     {"ts": OLD3, "total": 5.0, "pnl_excl_funding": 5.0, "funding": 0.0},
     {"total": 9.0}],                       # без ts — не входит в периоды
    since_ms=WEEK_MS)
check("aggregate_closes: неделя = 1.0 + 0.5 - 0.3 (без прошлой недели и без ts)",
      agg["n"] == 3 and abs(agg["total"] - 1.2) < 1e-9)
check("aggregate_closes: винрейт 2/3 = 67%", abs(agg["winrate"] - 66.67) < 0.1)
check("aggregate_closes: день только сегодня",
      fe.aggregate_closes(agg["trades"], since_ms=DAY_MS)["n"] == 1)
check("aggregate_closes: нет поля total — берётся цена + funding",
      abs(fe.aggregate_closes([{"ts": DAY3, "pnl_excl_funding": 0.7,
                                "funding": 0.3}])["total"] - 1.0) < 1e-9)

# --- сам текст сводки ------------------------------------------------------
dhist = [
    trade(DAY3, "BTCUSDT", price=0.60, funding=0.40, total=1.00, ret=0.1),
    trade(PREV3, "ETHUSDT", price=0.50, funding=0.0, total=0.50, ret=0.05),
    trade(MON3, "SOLUSDT", price=-0.20, funding=-0.10, total=-0.30, ret=-0.03),
    trade(OLD3, "XRPUSDT", price=5.0, funding=0.0, total=5.00, ret=0.5),
]
dstate = {"position": None, "history": dhist}
rep_day = fe.daily_report(dstate, now_ms=NOW3, balance=612.34,
                          tz=TZ3, tz_name="+03:00")
check("сводка: заголовок с датой и поясом",
      rep_day.startswith("🗓 ИТОГ ДНЯ · 17.10") and "(+03:00)" in rep_day)
check("сводка: баланс счёта и итоги дня/недели",
      "Счёт: $612.34 USDT" in rep_day and "за день +1.00 $" in rep_day
      and "за неделю +1.20 $" in rep_day)
check("сводка: строка дня — сделки и винрейт за сегодня",
      "День (17.10): сделок 1, винрейт 100%" in rep_day)
check("сводка: строка недели — с понедельника",
      "Неделя (с 12.10): сделок 3" in rep_day
      and "итого +1.20 $" in rep_day)
check("сводка: лучшая/худшая сделка дня",
      "Лучшая: BTCUSDT +1.00 $ · Худшая: BTCUSDT +1.00 $" in rep_day)
check("сводка: без позиции — сказано явно",
      "Открытой позиции нет" in rep_day)

fe.current_pnl = lambda p, feed=None: (2540.0, 2500.0, 0.25)
try:
    rep_day2 = fe.daily_report({"position": pos, "history": dhist},
                               now_ms=NOW3, balance=612.34, tz=TZ3)
finally:
    fe.current_pnl = orig_pnl
check("сводка: живой PnL открытой входит в итог дня",
      "за день +1.25 $" in rep_day2 and "Открытая: BTCUSDT" in rep_day2)
check("сводка: процент за день от баланса на начало дня",
      "(+0.20%)" in rep_day2)

rep_nobal_d = fe.daily_report(dstate, now_ms=NOW3, tz=TZ3)
check("сводка: без баланса — явное «н/д», неделя всё равно считается",
      "Счёт: н/д" in rep_nobal_d and "за неделю" not in rep_nobal_d)

# --- таймер ежедневной сводки ---------------------------------------------
dsent: list[str] = []
orig_notify_d, orig_save = fe.notify, fe.save_state
fe.notify = lambda msg, key=None, min_gap=0.0: dsent.append(msg)
fe.save_state = lambda *a, **k: None      # НЕ трогаем боевой position.json
try:
    dcli = fe.argparse.Namespace(daily_at=(0, 0), report_tz_obj=TZ3,
                                 report_tz="+03:00", mode="paper")
    st_d: dict = {"position": None, "history": dhist}
    check("daily: до указанного времени — молчим и день не отмечаем",
          fe.maybe_daily_report(dict(st_d), fe.argparse.Namespace(
              daily_at=(23, 59), report_tz_obj=TZ3, mode="paper"), None,
              now=NOW3 / 1000) is False)
    check("daily: в 00:30 сводка уходит",
          fe.maybe_daily_report(st_d, dcli, None, now=NOW3 / 1000) is True
          and len(dsent) == 1 and "ИТОГ ДНЯ" in dsent[-1])
    check("daily: день отмечен в состоянии",
          st_d.get("last_daily_report") == "2026-10-17")
    check("daily: второй раз в тот же день — молчим",
          fe.maybe_daily_report(st_d, dcli, None,
                                now=NOW3 / 1000 + 3600) is False
          and len(dsent) == 1)
    check("daily: на следующий день — снова уходит",
          fe.maybe_daily_report(st_d, dcli, None,
                                now=NOW3 / 1000 + 86400) is True
          and len(dsent) == 2)
    check("daily: off — рассылка выключена",
          fe.maybe_daily_report({"position": None, "history": []},
                                fe.argparse.Namespace(daily_at=None), None,
                                now=NOW3 / 1000) is False)

    # переживает рестарт: пометка дня ложится в файл состояния и читается
    # обратно через load_state (иначе после перезапуска сводка шла бы снова)
    d_save = os.path.join(tempfile.mkdtemp(), "position.json")
    orig_state_file = fe.STATE_FILE
    fe.STATE_FILE = d_save
    fe.save_state = orig_save          # НАСТОЯЩАЯ запись, но в временный файл
    try:
        st_persist: dict = {"position": None, "history": []}
        fe.maybe_daily_report(st_persist, dcli, None, now=NOW3 / 1000)
        reloaded = fe.load_state(d_save)
        check("daily: пометка дня сохраняется в position.json",
              reloaded.get("last_daily_report") == "2026-10-17")
        check("daily: после рестарта (load_state) повтор не уходит",
              fe.maybe_daily_report(reloaded, dcli, None,
                                    now=NOW3 / 1000 + 600) is False
              and len(dsent) == 3)
    finally:
        fe.STATE_FILE = orig_state_file
        fe.save_state = lambda *a, **k: None   # дальше снова без записи на диск

    # формат падает — цикл не должен упасть, а день всё равно отмечается
    orig_dr = fe.daily_report
    fe.daily_report = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    try:
        st_err: dict = {"position": None, "history": []}
        sent_ok = fe.maybe_daily_report(st_err, dcli, None, now=NOW3 / 1000)
    finally:
        fe.daily_report = orig_dr
    check("daily: сбой сводки не роняет цикл (день отмечен)",
          sent_ok is True and st_err.get("last_daily_report") == "2026-10-17"
          and len(dsent) == 3)
finally:
    fe.notify, fe.save_state = orig_notify_d, orig_save

print()
print("Итог:", "все проверки пройдены" if ok else "ЕСТЬ ПРОВАЛЫ")
sys.exit(0 if ok else 1)
