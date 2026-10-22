"""Тесты санитарии сделок: пометка физически невозможных результатов.

Что проверяем:
  1) детектор (trade_sanity) — на РЕАЛЬНЫХ данных с VPS: 19 фантомных записей
     ETHUSDT (held=0.0 ч, funding=$0, «прибыль» +1.15…+7.4% капитала) должны
     быть помечены, а настоящие сделки (ETHUSDT 20.5 ч с funding $4.40 и
     убыток DOTUSDT 1.1 ч) — нет;
  2) границы правила: порог, обе стороны (доход без источника / убыток без
     причины), отсутствие поля funding = «неизвестно» = не помечаем;
  3) память: фантом не строит стоп-лист, не участвует в symbol_stats,
     lessons, калибровке и сравнении maker/taker;
  4) панель: PnL/винрейт/profit factor считаются без фантомов, а плашка
     честно показывает, сколько денег исключено;
  5) команда `sanity` (и `sanity --fix`) на временных файлах состояния.

Запуск: python test_trade_sanity.py  (stdlib, без pytest).
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
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
import dashboard  # noqa: E402
import dashboard_metrics as M  # noqa: E402
import funding_executor as fe  # noqa: E402
import trade_sanity as S  # noqa: E402


def check(name, cond):
    print(("OK   " if cond else "FAIL ") + name)
    return bool(cond)


ok = True
CAP = 200.0          # капитал тестовых сделок на VPS (return_pct считался от него)


def close(sym, ts, held, funding, total, ret, mode="testnet", **kw):
    """Запись закрытия как в position.json (без notional — как на VPS).
    ret — тот самый return_pct из файла: по нему видно, что «живые» сделки
    шли с капиталом $1000, а фантомы — с $200 (одноразовый прогон
    с --capital 200), поэтому капитал считается обратно от total/ret."""
    r = {"action": "close", "ts": ts, "symbol": sym, "mode": mode,
         "held_hours": held, "funding": funding, "total": total,
         "pnl_excl_funding": total - funding, "return_pct": ret,
         "capital": round(abs(total / ret * 100), 2) if ret else 0.0}
    r.update(kw)
    return r


# ============================================================ 1) реальные данные
# 21 закрытие из position.json на VPS: один живой убыток DOTUSDT, одна живая
# сделка ETHUSDT (20.5 ч, funding $4.3978) и 19 фантомов ETHUSDT (held 0.0 ч,
# funding $0, «плюс» от +$2.30 до +$14.77). Сумма всех: +$101.86.
VPS_HISTORY = [
    close("DOTUSDT", 1791405877401, 1.1, 0.0, -1.2825, -0.128),
    close("ETHUSDT", 1791481044977, 20.5, 4.3978, 3.4542, 0.345),
    close("ETHUSDT", 1791568935150, 0.0, 0.0, 2.2972, 1.149),
    close("ETHUSDT", 1791569852593, 0.1, 0.0, 4.7392, 2.370),
    close("ETHUSDT", 1791570346157, 0.0, 0.0, 4.6241, 2.312),
    close("ETHUSDT", 1791570632258, 0.0, 0.0, 4.6401, 2.320),
    close("ETHUSDT", 1791571167091, 0.0, 0.0, 14.7682, 7.384),
    close("ETHUSDT", 1791571226937, 0.0, 0.0, 14.7426, 7.371),
    close("ETHUSDT", 1791571697683, 0.0, 0.0, 4.6381, 2.319),
    close("ETHUSDT", 1791571866144, 0.0, 0.0, 4.6039, 2.302),
    close("ETHUSDT", 1791572109760, 0.0, 0.0, 4.5650, 2.282),
    close("ETHUSDT", 1791572140967, 0.0, 0.0, 4.5543, 2.277),
    close("ETHUSDT", 1791572546494, 0.0, 0.0, 4.5436, 2.272),
    close("ETHUSDT", 1791572584250, 0.0, 0.0, 4.5456, 2.273),
    close("ETHUSDT", 1791572627629, 0.0, 0.0, 4.5460, 2.273),
    close("ETHUSDT", 1791572659879, 0.0, 0.0, 4.5378, 2.269),
    close("ETHUSDT", 1791572692509, 0.0, 0.0, 4.5492, 2.275),
    close("ETHUSDT", 1791573237706, 0.0, 0.0, 3.1923, 1.596),
    close("ETHUSDT", 1791573793559, 0.0, 0.0, 3.1813, 1.591),
    close("ETHUSDT", 1791573820169, 0.0, 0.0, 3.2163, 1.608),
    close("ETHUSDT", 1791573939233, 0.0, 0.0, 3.2010, 1.601),
]

clean, susp = S.partition(VPS_HISTORY)
ok &= check("VPS: помечено ровно 19 фантомов", len(susp) == 19)
ok &= check("VPS: чистых записей 2", len(clean) == 2)
ok &= check("VPS: живой убыток DOTUSDT не помечен",
            all(t["symbol"] != "DOTUSDT" for t in susp))
ok &= check("VPS: живая сделка ETHUSDT 20.5ч с funding не помечена",
            all(t["held_hours"] != 20.5 for t in susp))
sm = S.summarize(VPS_HISTORY)
ok &= check("VPS: сумма исключённого ≈ +$99.69",
            abs(sm["total"] - 99.6858) < 0.01)
ok &= check("VPS: живой убыток считался с капиталом $1000, фантомы — с $200",
            abs(clean[0]["capital"] - 1001.95) < 0.1
            and abs(susp[0]["capital"] - 199.93) < 0.1)
ok &= check("VPS: по символам только ETHUSDT",
            [r["symbol"] for r in sm["by_symbol"]] == ["ETHUSDT"])
ok &= check("VPS: причина помечена как «доход без источника»",
            "доход без источника" in (susp[0].get("suspicious") or S.note(susp[0])))
ok &= check("VPS: describe() не падает и даёт строку",
            isinstance(S.describe(susp[0]), str) and "ETHUSDT" in S.describe(susp[0]))

# Те же записи в формате памяти (held_h/pnl_pct, без капитала и нотиона):
# база восстанавливается обратным счётом из pnl_pct, поэтому порог тот же
mem_rows = []
for t in VPS_HISTORY:
    r = dict(t)
    r["held_h"] = r.pop("held_hours")
    r["pnl_pct"] = r["return_pct"]
    r.pop("capital")
    mem_rows.append(r)
mem_vps = {"trades": {"ETHUSDT": [r for r in mem_rows
                                  if r["symbol"] == "ETHUSDT"]}}
st = bm.symbol_stats(mem_vps, "ETHUSDT")
ok &= check("память: в скоринге только живая сделка (n=1, +0.35%)",
            st["n"] == 1 and abs(st["avg_pct"] - 0.345) < 1e-6)
ok &= check("запись памяти без капитала всё равно помечена (база из pnl_pct)",
            S.is_suspicious([r for r in mem_rows
                             if r["symbol"] == "ETHUSDT"][1]))
ok &= check("база восстанавливается из pnl_pct",
            abs(S.base_notional({"total": 2.2972, "pnl_pct": 1.149}) - 200.0) < 0.1)

# ============================================================ 2) границы правила
def pct_trade(held, total, funding=0.0, notional=None, capital=None, **kw):
    t = {"symbol": "XUSDT", "mode": "testnet", "held_hours": held,
         "funding": funding, "total": total, "ts": 1}
    if notional is not None:
        t["notional"] = notional
    if capital is not None:
        t["capital"] = capital
    t.update(kw)
    return t


ok &= check("граница: held ровно NO_PAYOUT_H не помечаем",
            not S.is_suspicious(pct_trade(S.NO_PAYOUT_H, 10.0, notional=100)))
ok &= check("граница: чуть короче + плюс выше порога → помечаем",
            S.is_suspicious(pct_trade(0.49, 0.6, notional=100)))
ok &= check("граница: плюс ровно на пороге (0.5% нотиона) → помечаем",
            S.is_suspicious(pct_trade(0.1, S.GAIN_LIMIT_PCT, notional=100)))
ok &= check("граница: плюс ниже порога (0.4%) → не помечаем",
            not S.is_suspicious(pct_trade(0.1, 0.4, notional=100)))
ok &= check("выплата была (funding $0.05) → дохода есть источник, не помечаем",
            not S.is_suspicious(pct_trade(0.1, 5.0, funding=0.05, notional=100)))
ok &= check("нет поля funding (неизвестно) → не помечаем",
            not S.is_suspicious({"symbol": "X", "held_hours": 0.0, "total": 9.0,
                                 "notional": 100}))
ok &= check("большой убыток при нулевом удержании → помечаем",
            S.is_suspicious(pct_trade(0.0, -6.0, notional=100)))
ok &= check("обычный убыток от комиссий (−0.4%) → не помечаем",
            not S.is_suspicious(pct_trade(0.0, -0.4, notional=100)))
ok &= check("убыток ровно 5% → помечаем (граница включительно)",
            S.is_suspicious(pct_trade(0.0, -5.0, notional=100)))
ok &= check("убыток 4.9% → не помечаем",
            not S.is_suspicious(pct_trade(0.0, -4.9, notional=100)))
ok &= check("база: нотион важнее капитала",
            abs(S.base_notional(pct_trade(0.0, 1.0, notional=800, capital=200)) - 800) < 1e-9)
ok &= check("база: без нотиона берётся капитал",
            abs(S.base_notional(pct_trade(0.0, 1.0, capital=250)) - 250) < 1e-9)
ok &= check("база: ничего нет → BASE_FALLBACK",
            abs(S.base_notional(pct_trade(0.0, 1.0)) - S.BASE_FALLBACK) < 1e-9)
ok &= check("удержание читается и как held_h (запись памяти)",
            S.is_suspicious({"symbol": "X", "held_h": 0.0, "funding": 0.0,
                             "total": 4.6, "capital": 200}))
ok &= check("правдоподобная сделка: причина = None", S.reason(pct_trade(2.0, 3.0)) is None)
ok &= check("запись без total не ломает детектор", S.note({"symbol": "X"}) is None)

# пометка: ставится один раз и сохраняется в записи
t_mark = pct_trade(0.0, 5.0, funding=0.0, notional=100)
ok &= check("mark() возвращает причину", bool(S.mark(t_mark)))
ok &= check("mark() записал поле suspicious", bool(t_mark.get(S.SUSPICIOUS)))
first = t_mark[S.SUSPICIOUS]
t_mark["total"] = 0.0          # данные «исправили», но пометка уже стоит
ok &= check("пометка не переписывается молча", S.mark(t_mark) == first)
ok &= check("mark_all считает только новые", S.mark_all([t_mark]) == 0)
ok &= check("partition сохраняет порядок",
            [t.get("total") for t in S.partition(
                [pct_trade(0.0, 0.1, notional=100), pct_trade(0.0, 9.0, notional=100)])[0]]
            == [pct_trade(0.0, 0.1, notional=100)["total"]])

# --- ingest-правило: пометка по причине (слippедж цены ноги) ---------------
ok &= check("ingest: слип 250 б.п. + мгновенное закрытие без funding → причина",
            bool(S.slip_reason(pct_trade(0.0, 0.0, funding=0.0, notional=100,
                                         slip_bps=250))))
_ok_slip = S.slip_reason(pct_trade(0.0, 0.0, funding=0.0, notional=100,
                                   slip_bps=409)) or ""
ok &= check("ingest: в причине видно число слипа (409 б.п.)", "409" in _ok_slip)
ok &= check("ingest: слип ниже лимита (190 < 200) → не помечаем ingest",
            S.slip_reason(pct_trade(0.0, 0.0, funding=0.0, notional=100,
                                    slip_bps=190)) is None)
ok &= check("ingest: граница лимита включительно (ровно 200) → помечаем",
            bool(S.slip_reason(pct_trade(0.0, 0.0, funding=0.0, notional=100,
                                         slip_bps=200))))
ok &= check("ingest: удержание дольше SLIP_H → не помечаем",
            S.slip_reason(pct_trade(0.4, 0.0, funding=0.0, notional=100,
                                    slip_bps=409)) is None)
ok &= check("ingest: funding был (выплата есть) → не помечаем",
            S.slip_reason(pct_trade(0.0, 0.0, funding=0.05, notional=100,
                                    slip_bps=409)) is None)
ok &= check("ingest: поля slip_bps нет → не помечаем",
            S.slip_reason(pct_trade(0.0, 0.0, funding=0.0, notional=100)) is None)
_slip_done = pct_trade(0.0, 0.0, funding=0.0, notional=100, slip_bps=409)
_slip_done[S.SUSPICIOUS] = "уже помечена"
ok &= check("ingest: помеченную запись не трогаем", S.slip_reason(_slip_done) is None)
_ok_reason = S.reason(pct_trade(0.1, 5.0, funding=0.0, notional=100,
                                slip_bps=350)) or ""
ok &= check("read-time: причина пометки содержит слippедж (350 б.п.)",
            "350" in _ok_reason)
ok &= check("read-time: без slip_bps причина без приписки",
            "слippедж" not in (S.reason(pct_trade(0.1, 5.0, funding=0.0,
                                                  notional=100)) or ""))

# ============================================================ 3) память
def mem_trade(sym, ts, held, funding, total, mode="testnet", **kw):
    t = {"ts": ts, "symbol": sym, "mode": mode, "held_h": held,
         "funding": funding, "total": total, "pnl": total - funding}
    if CAP:
        t["pnl_pct"] = total / CAP * 100
    t.update(kw)
    return t


# фантомы: два «убытка» подряд (зеркальный дефект, −6% капитала при нулевом
# удержании) не должны дать стоп-лист. held разный — иначе сработает дедуп.
mem2: dict = {"trades": {}, "stops": {}, "lessons": [], "events": []}
n1 = bm.record_trade(mem2, "ETHUSDT", "testnet", 0.0, -12.0, 0.0, -12.0, capital=CAP)
n2 = bm.record_trade(mem2, "ETHUSDT", "testnet", 0.2, -12.0, 0.0, -12.0, capital=CAP)
ok &= check("фантом записан в историю (для разбора)", len(mem2["trades"]["ETHUSDT"]) == 2)
ok &= check("фантом помечен в памяти",
            all(t.get("suspicious") for t in mem2["trades"]["ETHUSDT"]))
ok &= check("фантомный убыток не создал стоп-лист", "ETHUSDT" not in mem2["stops"])
ok &= check("урок про подозрительную сделку записан",
            n1[1] is not None and "подозрительная" in n1[1] and n2[1] is not None)

# а настоящие два убытка стоп-лист создают (контроль, что фильтр не всё глушит)
mem3: dict = {"trades": {}, "stops": {}, "lessons": [], "events": []}
bm.record_trade(mem3, "AAAUSDT", "testnet", 5.0, -6.0, 0.5, -5.5, capital=CAP)
bm.record_trade(mem3, "AAAUSDT", "testnet", 5.2, -6.1, 0.5, -5.6, capital=CAP)
ok &= check("серия реальных убытков стоп-лист создаёт", "AAAUSDT" in mem3["stops"])
ok &= check("record_trade пишет символ в саму запись",
            mem3["trades"]["AAAUSDT"][0].get("symbol") == "AAAUSDT")

# старые записи памяти держат символ только в ключе словаря trades — отчёты
# должны видеть его в самой записи (иначе разбор фантомов выводит «?»)
mem7: dict = {"trades": {"ETHUSDT": [{"ts": 1, "held_h": 0.0, "funding": 0.0,
                                        "total": 4.6, "pnl_pct": 2.3}]},
              "stops": {}, "lessons": [], "events": []}
rows7 = bm.all_trades(mem7)
ok &= check("all_trades дозаполняет символ в записи памяти",
            rows7[0]["symbol"] == "ETHUSDT")
ok &= check("sanity показывает символ, а не «?»", "ETHUSDT" in S.describe(rows7[0]))
ok &= check("summarize группирует по символу, а не по «?»",
            S.summarize(rows7)["by_symbol"][0]["symbol"] == "ETHUSDT")

# фантомы не попадают в статистику символа и в уроки
mem4: dict = {"trades": {"ETHUSDT": [
    mem_trade("ETHUSDT", 1, 0.0, 0.0, 4.60),
    mem_trade("ETHUSDT", 2, 0.0, 0.0, 3.20),
    mem_trade("ETHUSDT", 3, 20.0, 4.40, 3.45),
]}, "stops": {}, "lessons": [], "events": []}
st4 = bm.symbol_stats(mem4, "ETHUSDT")
ok &= check("symbol_stats: (n=1, total=$3.45) без фантомов",
            st4["n"] == 1 and abs(st4["total"] - 3.45) < 1e-9)
les = bm.lessons(mem4)
ok &= check("lessons: есть строка про исключённые фантомы",
            any("подозрительные сделки: 2 шт." in x for x in les))
ok &= check("lessons: фантомы не дают вердикт «стабильно прибыльный»",
            not any("стабильно прибыльный" in x for x in les))

# калибровка/автотюнинг/maker-taker видят только правдоподобные сделки
mem5: dict = {"trades": {"ETHUSDT": [
    mem_trade("ETHUSDT", i, 0.0, 0.0, 4.60, legs="maker:maker", apy30=10.0)
    for i in range(1, 7)
]}, "stops": {}, "lessons": [], "events": []}
em = bm.entry_mode_stats(mem5, mode="testnet")
ok &= check("entry_mode_stats: maker n=0 (фантомы не учат режим входа)",
            em["maker"]["n"] == 0 and em["taker"]["n"] == 0)
ok &= check("calibrate на фантомах не двигает пороги",
            bm.calibrate(mem5, preset="hold", mode="testnet") is None)
tune = bm.autotune(mem5, force=True)
ok &= check("autotune: фантомы не попадают в окно пересмотра порогов",
            "пороги не трогаю" in (tune or "")
            and "thresholds" not in (mem5.get("presets", {}).get("hold", {})
                                     .get("calibration", {})))

# ingest из bot.log: CLOSE-строка с нулевым удержанием и плюсом помечается
import tempfile  # noqa: E402
d_ing = tempfile.mkdtemp(prefix="sanity_ing_")
lp = os.path.join(d_ing, "bot.log")
with open(lp, "w", encoding="utf-8") as f:
    f.write("[2026-10-11 10:00:00] CLOSE ETHUSDT testnet held=0.0h pnl=$+4.6381 "
            "funding=$+0.0000 total=$+4.6381 (2.32%)\n")
mem6: dict = {"trades": {}, "stops": {}, "lessons": [], "events": []}
bm.ingest_log(mem6, path=lp, since_ts=0)
row = mem6["trades"]["ETHUSDT"][0]
ok &= check("ingest: фантом из bot.log помечен", bool(row.get("suspicious")))

# ============================================================ 4) панель
trades_all = dashboard.closed_trades({"history": VPS_HISTORY})
t_clean, t_susp = S.partition(trades_all)
core_clean = M.core_metrics(t_clean)
core_all = M.core_metrics(trades_all)
ok &= check("панель: итог без фантомов = +$2.1717 (живые сделки)",
            abs(core_clean["net"] - 2.1717) < 1e-3)
ok &= check("панель: чистый капитал не попал в исключения",
            len(t_susp) == 19)
ok &= check("панель: с фантомами было бы +$101.86 (регресс)",
            abs(core_all["net"] - 101.8575) < 1e-3)
ok &= check("панель: винрейт считается по 2 сделкам", core_clean["n"] == 2)
ok &= check("панель: разбивка по символам без фантомов",
            {r["symbol"]: r["n"] for r in M.per_symbol(t_clean)}
            == {"ETHUSDT": 1, "DOTUSDT": 1})
bar = dashboard.sanity_bar(t_susp)
ok &= check("панель: плашка сообщает, сколько исключено",
            "19 подозрительных" in bar and "$99.69" in bar)
ok &= check("панель: без фантомов плашки нет", dashboard.sanity_bar([]) == "")

# блок памяти на панели: «1/20» без пояснения читается как поломка памяти
d_pan = tempfile.mkdtemp(prefix="sanity_panel_")
old_memf = dashboard.MEMORY_FILE
try:
    pan_mem = {"version": 1, "trades": {"ETHUSDT": [
        mem_trade("ETHUSDT", 1, 0.0, 0.0, 4.60),
        mem_trade("ETHUSDT", 2, 0.0, 0.0, 3.20),
        mem_trade("ETHUSDT", 3, 20.0, 4.40, 3.45),
        mem_trade("ETHUSDT", 4, 24.0, 4.40, 3.45), ]}, "stops": {}, "lessons": [],
        "events": [], "calibration": {}}
    pan_path = os.path.join(d_pan, "memory.json")
    with open(pan_path, "w", encoding="utf-8") as f:
        json.dump(pan_mem, f)
    dashboard.MEMORY_FILE = pan_path
    html = dashboard.memory_block()
    ok &= check("панель: в памяти видно «2 подозрит.»", "⚠ 2 подозрит." in html)
    ok &= check("панель: скоринг памяти считает только правдоподобные (2/4)",
                "2/4" in html)
finally:
    dashboard.MEMORY_FILE = old_memf

# ============================================================ 5) команда sanity
d_san = tempfile.mkdtemp(prefix="sanity_cmd_")
old_state, old_mem = fe.STATE_FILE, bm.MEM_FILE
try:
    fe.STATE_FILE = os.path.join(d_san, "position.json")
    bm.MEM_FILE = os.path.join(d_san, "memory.json")
    with open(fe.STATE_FILE, "w", encoding="utf-8") as f:
        json.dump({"position": None, "history": VPS_HISTORY}, f)
    with open(bm.MEM_FILE, "w", encoding="utf-8") as f:
        json.dump({"version": 1, "trades": {"ETHUSDT": [
            mem_trade("ETHUSDT", 1, 0.0, 0.0, 4.60)]},
            "stops": {}, "lessons": [], "events": []}, f)

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        fe.cmd_sanity(types.SimpleNamespace(fix=False))
    out = buf.getvalue()
    ok &= check("sanity: находит 19 в position.json", "position.json (закрытия)" in out
                and "подозрительных 19" in out)
    ok &= check("sanity: печатает пути от папки скрипта", d_san in out)
    ok &= check("sanity: находит 1 в memory.json", "memory.json (сделки обучения)" in out)
    ok &= check("sanity: без --fix подсказывает флаг", "--fix" in out)
    ok &= check("sanity: без --fix файлы не меняются",
                "suspicious" not in open(fe.STATE_FILE, encoding="utf-8").read())

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        fe.cmd_sanity(types.SimpleNamespace(fix=True))
    out = buf.getvalue()
    ok &= check("sanity --fix: помечено 19 + 1", "Итого новых пометок: 20" in out)
    saved_state = json.load(open(fe.STATE_FILE, encoding="utf-8"))
    saved_mem = json.load(open(bm.MEM_FILE, encoding="utf-8"))
    ok &= check("sanity --fix: пометки в position.json",
                sum(1 for h in saved_state["history"] if h.get("suspicious")) == 19)
    ok &= check("sanity --fix: пометки в memory.json",
                bool(saved_mem["trades"]["ETHUSDT"][0].get("suspicious")))
    ok &= check("sanity --fix: сделана резервная копия position.json",
                os.path.exists(fe.STATE_FILE + ".pre-sanity.bak"))
    ok &= check("sanity --fix: сделана резервная копия memory.json",
                os.path.exists(bm.MEM_FILE + ".pre-sanity.bak"))

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        fe.cmd_sanity(types.SimpleNamespace(fix=True))
    ok &= check("sanity --fix идемпотентна (повторно 0 пометок)",
                "Итого новых пометок: 0" in buf.getvalue())
finally:
    fe.STATE_FILE, bm.MEM_FILE = old_state, old_mem

print()
print("Итог:", "все проверки пройдены" if ok else "ЕСТЬ ПРОВАЛЫ")
sys.exit(0 if ok else 1)
