"""Память и самообучение funding-бота: статистика сделок → скоринг символов.

Зачем: фильтр входа смотрит только на текущую ставку/базис и не учитывает
собственный опыт бота. Этот модуль копит результаты сделок (из bot.log и
прямых вызовов при close/void), считает по каждому символу фактическую
эффективность и возвращает поведение:

  • avoid_symbol()  — от кого воздержаться (серии убытков, повторные void'ы);
  • prefer_order()  — кого показывать первым (у кого получалось зарабатывать);
  • lessons()       — сформулированные выводы («X стабильно прибыльный»,
                      «Y платит меньше комиссий, чем приносит»).

  • calibrate()      — сравнивает прогноз APY при входе с фактической годовой
                       доходностью сделки и корректирует пороги фильтра:
                       прогноз систематически завышен → пороги вверх
                       (бот берёт только явно хорошие окна), занижен → вниз
                       (бот не пропускает годные окна).

  • autotune()       — раз в неделю (AUTO_EVERY_DAYS) пересматривает пороги
                       по фактической прибыли сделок: окна, где ставка
                       платила хорошо, а сделка всё равно убыточна, поднимают
                       min_apy30/fast_apy/min_basis. Предложения пишутся в
                       лог и Telegram и хранятся в mem["autotune"] —
                       применяются человеком вручную (флаги CLI).

  • learn_entry_mode() — сравнивает фактическую прибыль maker- и taker-входов
                       (данные legs/slip_bps в сделках) и пишет предпочтение
                       в mem["entry_mode"]["prefer"]: его читает open_position
                       и пропускает maker-попытки, если память сказала taker.

  • санитария (trade_sanity) — запись с нулевым удержанием, без выплат funding
                       и при этом с заметным результатом физически невозможна:
                       это битые цены ног, а не сделка. Такие записи помечаются
                       ("suspicious") и не учат ничего: ни скоринг, ни
                       калибровку, ни стоп-листы (иначе фантомный «убыток»
                       банил бы живой символ, а фантомный «плюс» уводил бы
                       пороги вверх). В истории они остаются для разбора.

Только stdlib (+ trade_sanity рядом). Файлы: memory.json (состояние памяти),
bot.log (источник событий ENTER/CLOSE/VOID). position.json не читается —
close сам зовёт record_trade с полными данными.
"""
from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime

try:
    import trade_sanity as sanity
except ImportError:      # модуль санитарии не приехал при обновлении папки:
    sanity = None        # фильтр выключается, но память продолжает работать

MEM_FILE = "memory.json"
LOG_FILE = "bot.log"

MIN_TRADES = 2          # меньше сделок по символу — вердикта нет, только наблюдение
MIN_HOLD_H = 0.5        # сделки короче — шум: в скоринг не идут (но записываются)
LOSS_STREAK = 2         # убыточных сделок подряд → стоп-лист
STOP_DAYS_1 = 7         # первый стоп-лист, дней
STOP_DAYS_2 = 21        # повторный (серия после снятия), дней
AVG_PNL_PCT = -0.05     # средний PnL% хуже этого → «плохой» символ
GOOD_PNL_PCT = 0.10     # средний PnL% лучше этого и win_share ≥ 0.6 → «хороший»
FAILS_TO_AVOID = 3      # неудачных входов (ордер не исполнился) → пауза 24 ч
VOIDS_TO_AVOID = 2      # void'ов (позиция испарилась) → пауза 24 ч
FAIL_COOLDOWN_H = 24
EVENTS_KEEP = 300       # сколько последних событий (fail/void) хранить

# --- калибровка порогов по качеству прогноза -------------------------------
CAL_MIN_TRADES = 5      # меньше сделок с прогнозом → коррекция не двигается
CAL_STEP_PCT = 2.0      # максимальный сдвиг порога за одну коррекцию, п.п.
CAL_MAX_SHIFT = 10.0    # предельный сдвиг от исходного порога, п.п. (санитарный буфер)

# --- недельный автотюнинг порогов по фактической прибыли -------------------
AUTO_EVERY_DAYS = 7     # периодичность автотюнинга, дней
AUTO_MIN_TRADES = 5     # меньше закрытых сделок за период → пороги не трогаем
AUTO_MIN_GOOD = 10.0    # окно считается окупившимся при прибыли ≥ этого, % нотион
AUTO_APY_STEP = 1.0     # шаг min_apy30 за каждое убыточное окно, п.п.
AUTO_BASIS_STEP = 0.05  # шаг min_basis за каждое убыточное окно, п.п.
AUTO_MAX_30 = 25.0      # потолок min_apy30 (выше почти нет окон — стоп)
AUTO_MAX_BASIS = 0.5    # потолок min_basis (вход стоил ~0.18%, выше не сойти)

# --- сравнение maker- и taker-входов ----------------------------------------
MODE_MIN_TRADES = 3     # сделок в КАЖДОЙ группе (maker/taker) — иначе вывода нет
MODE_PNL_MARGIN = 0.10  # мин. разница средних PnL%, п.п., чтобы предпочесть режим

# --- единый механизм порогов: журнал решений -------------------------------
THR_LOG_KEEP = 50       # сколько последних решений порогов хранить

# Пресет по умолчанию для вызовов без явного preset (обратная совместимость:
# старые команды и тесты считают от него). Калибровка/autotune в боте всегда
# передают пресет явно — см. cmd_close/cmd_watch.
LEGACY_PRESET = "hold"

# Базовые пороги фильтра по ПРЕСЕТАМ — те же значения, что в PRESETS
# funding_monitor.py. Раньше база была одна (hold) на всех пресеты, и
# калибровка mainnet (min_apy30 15%, fast_apy 6%) считалась от 10%/3%:
# сдвиг вниз уводил реальные деньги под порог «с запасом на комиссию круга».
# Дублируем таблицу, чтобы память не зависела от импорта монитора;
# test_preset_thresholds сверяет её с PRESETS.
PRESET_BASE = {
    "hold":    {"min_apy30": 10.0, "min_basis": 0.0, "fast_apy": 3.0},
    "spike":   {"min_apy30": 30.0, "min_basis": 0.0, "fast_apy": 3.0},
    "mainnet": {"min_apy30": 15.0, "min_basis": 0.0, "fast_apy": 6.0},
}


def _suspicious(trade: dict) -> bool:
    """Помечена ли сделка как физически невозможная (см. trade_sanity).
    Такие записи не участвуют ни в скоринге, ни в калибровке, ни в стоп-листах:
    у них «прибыль» и «убыток» — бухгалтерия битых цен ног, а не результат."""
    if sanity is not None:
        return sanity.is_suspicious(trade)
    return bool(trade.get("suspicious"))


def all_trades(mem: dict) -> list[dict]:
    """Все записанные сделки одним списком (по всем символам).
    Символ дозаполняется в самой записи: старые записи держат его только
    в ключе словаря trades, а отчётам (sanity, lessons, панель) нужен он
    в записи — иначе фантомы выводятся как «?». Дозаполнение безобидно
    и сохраняется вместе со следующим save_memory."""
    out: list[dict] = []
    for sym, rows in (mem.get("trades") or {}).items():
        for t in rows:
            if isinstance(t, dict):
                t.setdefault("symbol", sym)
                out.append(t)
    return out


def default_thresholds(preset: str = LEGACY_PRESET) -> dict:
    """Исходные (дефолтные) пороги фильтра ДЛЯ ПРЕСЕТА — база калибровки.
    Каждый пресет двигается от своих значений: сдвиг mainnet считается от
    15%/6%, а не от hold-овых 10%/3%."""
    return dict(PRESET_BASE.get(preset) or PRESET_BASE[LEGACY_PRESET])


def base_of(cal: dict, preset: str) -> dict:
    """База порогов пресета с миграцией старых записей.
    До правки база была одна на все пресеты и равнялась hold-ской (10/0/3);
    такая сохранённая база для spike/mainnet устарела — пересобираем её из
    текущего PRESET_BASE, иначе калибровка реальных денег продолжила бы
    двигаться от 10% вместо 15%."""
    want = default_thresholds(preset)
    base = cal.get("base")
    if not isinstance(base, dict):
        return want
    if want != default_thresholds(LEGACY_PRESET) \
            and base == default_thresholds(LEGACY_PRESET):
        return want                      # устаревшая hold-база
    return {k: base.get(k, want[k]) for k in want}


# --- пресет-изоляция порогов -----------------------------------------------
# Калибровка/автотюнинг строятся по сделкам с одним профилем (комиссии,
# ставки, проскальзывание). Данные testnet и mainnet различаются (ставки
# testnet — синтетика), поэтому выученные на одном пороги нельзя молча
# применять к другому: секция mem["presets"][<пресет>] хранит пороги,
# которые этот пресет выучил сам, а чтение чужой калибровки отсекается.


def preset_of(mem: dict, preset: str) -> dict:
    """Секция calibration пресета (создаёт при первом обращении).
    thresholds в ней появляется только после калибровки/автотюнинга
    ЭТОГО пресета — их отсутствие = «фильтр берёт дефолты пресета»."""
    return (mem.setdefault("presets", {})
               .setdefault(preset, {})
               .setdefault("calibration", {}))


def thresholds_for(preset: str) -> dict | None:
    """Пороги, выученные ЭТИМ пресетом, или None — тогда фильтр берёт
    дефолт пресета. Чужая калибровка (другой пресет) не применяется:
    autotune/calibrate других режимов обучались на другой экономике."""
    mem = load_memory()
    cal = mem.get("presets", {}).get(preset, {}).get("calibration", {})
    return cal.get("thresholds") or None


# ---------------------------------------------------------------- persistence

def load_memory(path: str = MEM_FILE) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            mem = json.load(f)
        if isinstance(mem, dict) and "trades" in mem:
            mem.setdefault("stops", {})
            mem.setdefault("events", [])
            mem.setdefault("lessons", [])
            return mem
    except (OSError, ValueError):
        pass
    return {"version": 1, "trades": {}, "stops": {}, "events": [], "lessons": [],
            "ingest_ts": 0.0, "updated_ms": 0}


def save_memory(mem: dict, path: str = MEM_FILE) -> None:
    mem["updated_ms"] = int(time.time() * 1000)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(mem, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)   # атомарно: сервис не прочитает недописанный файл


# ---------------------------------------------------------------- записи

def record_trade(mem: dict, symbol: str, mode: str, held_h: float, pnl: float,
                 funding: float, total: float, capital: float = 0.0,
                 extra: dict | None = None) -> tuple[bool, str | None]:
    """Закрытая сделка → в память. Обновляет стоп-лист при серии убытков.
    Возвращает (записана_ли, урок_или_None): дубликат по ts не пишется."""
    trade = {"ts": int(time.time() * 1000), "symbol": symbol, "mode": mode,
             "held_h": round(held_h, 2), "pnl": round(pnl, 4),
             "funding": round(funding, 4), "total": round(total, 4)}
    if capital > 0:
        trade["pnl_pct"] = round(total / capital * 100, 3)
    if extra:
        trade.update(extra)
        # нотион нужен калибровке (realized_apy_pct): без него годовая
        # доходность считалась от жёстких $600 и уезжала на десятки п.п.
        nt = trade.get("notional")
        if nt is not None and nt > 0:
            trade["notional"] = float(nt)
    trades = mem.setdefault("trades", {}).setdefault(symbol, [])
    # дедуп по содержимому: close записал сделку напрямую, а ingest потом
    # перечитает ту же CLOSE-строку из bot.log — ts разные, цифры почти те же
    # (held_h в логе округлён до 0.1ч, поэтому сравнение с допуском; точное
    # сравнение пропускало дубль и создавало ложные серии убытков → стоп-лист)
    def _dup(t) -> bool:
        # held_h в логе напечатан с одним знаком (%.1f), а здесь хранится с
        # двумя: расхождение до 0.05 — норма округления. Допуск 0.05 был
        # ровно на границе, и ~3% значений (8.25→"8.2", 1.05→"1.0" …) давали
        # abs(diff) >= 0.05: дубль НЕ отсекался, сделка писалась дважды, а
        # два убытка подряд давали ложный стоп-лист живому символу.
        # 0.051 запас на погрешность float.
        return (t.get("mode") == mode
                and abs((t.get("held_h") or 0) - trade["held_h"]) < 0.051
                and abs((t.get("pnl") or 0) - trade["pnl"]) < 1e-6
                and abs((t.get("total") or 0) - trade["total"]) < 1e-6)
    if any(_dup(t) for t in trades[-5:]):
        return False, None
    # Санитария: нулевое удержание + ноль выплат + результат, которому неоткуда
    # взяться, — дефект записи. Помечаем и выходим: такая сделка не строит
    # стоп-листы и не учит пороги. В истории остаётся с причиной (trade_sanity)
    # — аномалию надо разбирать, а не прятать.
    susp = sanity.mark(trade) if sanity is not None else None
    trades.append(trade)
    if susp:
        lesson = (f"{symbol}: подозрительная сделка исключена из статистики и "
                  f"обучения — {susp}")
        mem.setdefault("lessons", []).append({"ts": trade["ts"], "text": lesson})
        return True, lesson

    # окно серии: последние LOSS_STREAK сделок, ГОДНЫХ для вердикта. Раньше
    # брались последние LOSS_STREAK записей подряд, поэтому пачка фантомов
    # (held_h ≈ 0) вытесняла реальные убытки из окна и серия не замечалась.
    seq = [t for t in trades if t["held_h"] >= MIN_HOLD_H
           and not _suspicious(t)][-LOSS_STREAK:]
    if len(seq) >= LOSS_STREAK and all(t["total"] < 0 for t in seq):
        repeat = mem["stops"].get(symbol, 0) > time.time()   # уже был стоп → жёстче
        days = STOP_DAYS_2 if repeat else STOP_DAYS_1
        mem["stops"][symbol] = time.time() + days * 86400
        lesson = (f"{symbol}: {LOSS_STREAK} убытка подряд → пауза {days} дней "
                  f"(средний {symbol_stats(mem, symbol)['avg_pct']:+.2f}%)")
        mem.setdefault("lessons", []).append({"ts": trade["ts"], "text": lesson})
        return True, lesson
    return True, None


def _events(mem: dict) -> list:
    """Список событий с защитой от битого формата (не список → сброс).
    Также отбрасывает элементы без symbol/kind: в memory.json может
    попасть мусор (ручная правка, старый дамп), а KeyError на e['symbol']
    ронял cmd_memory, dashboard.memory_block и весь watch-цикл."""
    if not isinstance(mem.get("events"), list):
        mem["events"] = []
    clean = [e for e in mem["events"]
             if isinstance(e, dict) and e.get("symbol") and e.get("kind")]
    if len(clean) != len(mem["events"]):
        mem["events"] = clean
    return clean


def record_void(mem: dict, symbol: str, note: str = "") -> str | None:
    """Позиция испарилась (расхождение с биржей) → событие; повтор — пауза."""
    evs = _events(mem)
    seen = [e for e in evs
            if e["kind"] == "void" and e["symbol"] == symbol]
    evs.append({"kind": "void", "symbol": symbol,
                "ts": int(time.time() * 1000), "note": note[:120]})
    _trim_events(mem)
    if len(seen) + 1 >= VOIDS_TO_AVOID:
        mem["stops"][symbol] = max(mem["stops"].get(symbol, 0),
                                   time.time() + FAIL_COOLDOWN_H * 3600)
        lesson = (f"{symbol}: позиция дважды снята как фантом → пауза "
                  f"{FAIL_COOLDOWN_H} ч — разберитесь, кто закрывает ноги на бирже")
        mem.setdefault("lessons", []).append({"ts": int(time.time() * 1000),
                                              "text": lesson})
        return lesson
    return None


def record_fail(mem: dict, symbol: str, reason: str = "") -> str | None:
    """Неудачный вход (ордер не исполнился/отклонён) → событие; часто — пауза."""
    now = time.time()
    evs = _events(mem)
    seen = [e for e in evs
            if e["kind"] == "fail" and e["symbol"] == symbol
            and now - (e.get("ts") or 0) / 1000 < 86400]
    evs.append({"kind": "fail", "symbol": symbol,
                "ts": int(now * 1000), "note": reason[:120]})
    _trim_events(mem)
    if len(seen) + 1 >= FAILS_TO_AVOID:
        mem["stops"][symbol] = max(mem["stops"].get(symbol, 0),
                                   now + FAIL_COOLDOWN_H * 3600)
        lesson = (f"{symbol}: {FAILS_TO_AVOID} неудачных входов за сутки → пауза "
                  f"{FAIL_COOLDOWN_H} ч (тонкий стакан — входы туда стоят кулдауна)")
        mem.setdefault("lessons", []).append({"ts": int(now * 1000), "text": lesson})
        return lesson
    return None


def _trim_events(mem: dict) -> None:
    evs = _events(mem)
    if len(evs) > EVENTS_KEEP:
        mem["events"] = evs[-EVENTS_KEEP:]


# ---------------------------------------------------------------- ingest bot.log

_TS_RE = re.compile(r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]")
# mode ВСЕ три среды: без mainnet лог реальных сделок не разбирался ВООБЩЕ
# (ingest/relearn молча теряли всю историю бота на живых деньгах).
_MODES = "paper|testnet|mainnet"
_CLOSE_RE = re.compile(
    r"CLOSE (\S+) (" + _MODES + r") held=([\d.]+)h pnl=\$([+\-\d.]+) "
    r"funding=\$([+\-\d.]+) total=\$([+\-\d.]+)")
_VOID_RE = re.compile(r"VOID (\S+) (" + _MODES + r"): фантом")
_FAIL_RE = re.compile(r"вход не удался \((\S+)\)")


def _line_ts(line: str) -> float | None:
    m = _TS_RE.match(line)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").timestamp()
    except ValueError:
        return None


def ingest_log(mem: dict, path: str = LOG_FILE,
               since_ts: float | None = None) -> dict:
    """Дочитать bot.log: CLOSED-сделки, void'ы, неудачные входы.
    since_ts None → взять mem['ingest_ts'] (инкрементально), 0 → весь лог.
    Возвращает {trades, fails, voids}."""
    if since_ts is None:
        since_ts = mem.get("ingest_ts") or 0.0
    counts = {"trades": 0, "fails": 0, "voids": 0}
    last_ts = since_ts
    try:
        f = open(path, encoding="utf-8", errors="replace")
    except OSError:
        return counts
    with f:
        for line in f:
            ts = _line_ts(line)
            if ts is None or ts <= since_ts:
                continue
            last_ts = max(last_ts, ts)
            if (mc := _CLOSE_RE.search(line)):
                sym, mode = mc.group(1), mc.group(2)
                recorded, _lesson = record_trade(
                    mem, sym, mode, float(mc.group(3)), float(mc.group(4)),
                    float(mc.group(5)), float(mc.group(6)))
                if recorded:
                    counts["trades"] += 1
            elif (mv := _VOID_RE.search(line)):
                record_void(mem, mv.group(1), "ingest из bot.log")
                counts["voids"] += 1
            elif (mf := _FAIL_RE.search(line)):
                record_fail(mem, mf.group(1), "ingest из bot.log")
                counts["fails"] += 1
    mem["ingest_ts"] = last_ts
    return counts


# ---------------------------------------------------------------- аналитика

def symbol_stats(mem: dict, symbol: str) -> dict:
    """Агрегат по сделкам, годным для скоринга: held_h ≥ MIN_HOLD_H и не
    подозрительные (фантомы не искажают средний PnL и винрейт символа)."""
    rows = [t for t in mem.get("trades", {}).get(symbol, [])
            if t.get("held_h", 0) >= MIN_HOLD_H and not _suspicious(t)]
    if not rows:
        return {"n": 0, "wins": 0, "win_share": 0.0, "avg_pct": 0.0,
                "total": 0.0, "funding": 0.0, "avg_hold_h": 0.0, "best": None,
                "worst": None}
    def pct(t):
        if t.get("pnl_pct") is not None:
            return t["pnl_pct"]
        # fallback: PnL к нотиону $600 (типичный размер тестов)
        return t["total"] / 600.0 * 100
    pcts = [pct(t) for t in rows]
    totals = [t["total"] for t in rows]
    return {
        "n": len(rows),
        "wins": sum(1 for x in totals if x > 0),
        "win_share": sum(1 for x in totals if x > 0) / len(rows),
        "avg_pct": sum(pcts) / len(pcts),
        "total": sum(totals),
        "funding": sum(t.get("funding", 0) for t in rows),
        "avg_hold_h": sum(t.get("held_h", 0) for t in rows) / len(rows),
        "best": max(totals),
        "worst": min(totals),
    }


def all_symbols(mem: dict) -> list[str]:
    syms = set(mem.get("trades", {}) or {}) | {e["symbol"] for e in _events(mem)}
    return sorted(syms)


def avoid_symbol(mem: dict, symbol: str) -> str | None:
    """Причина пропустить символ в поиске входа или None."""
    until = mem.get("stops", {}).get(symbol, 0)
    if until > time.time():
        h = (until - time.time()) / 3600
        return f"стоп-лист памяти: ещё {h:.0f} ч ({symbol} приносил убытки)"
    return None


def prefer_order(mem: dict, rows: list[dict]) -> list[dict]:
    """Стабильно прибыльные символы — в начало списка кандидатов."""
    def key(r):
        st = symbol_stats(mem, r["symbol"])
        if st["n"] >= MIN_TRADES and st["avg_pct"] > 0 and st["win_share"] >= 0.6:
            return 0, -st["avg_pct"]          # проверенные добытчики — вперёд
        return 1, 0                           # остальные — в исходном порядке
    return sorted(rows, key=key)


def lessons(mem: dict) -> list[str]:
    """Сформулированные выводы по памяти — для команды memory и лога."""
    out: list[str] = []
    for sym in all_symbols(mem):
        st = symbol_stats(mem, sym)
        if st["n"] < MIN_TRADES:
            continue
        if st["avg_pct"] <= AVG_PNL_PCT and st["win_share"] < 0.5:
            out.append(f"{sym}: средний {st['avg_pct']:+.2f}% на {st['n']} сделках "
                       f"(win {st['win_share']:.0%}) — в основном приносит убыток, "
                       f"избегаю")
        elif st["avg_pct"] >= GOOD_PNL_PCT and st["win_share"] >= 0.6:
            out.append(f"{sym}: средний {st['avg_pct']:+.2f}% на {st['n']} сделках "
                       f"(win {st['win_share']:.0%}) — стабильно прибыльный, "
                       f"приоритет в поиске")
    now = time.time()
    for sym, until in sorted(mem.get("stops", {}).items()):
        if until > now:
            out.append(f"{sym}: в стоп-листе ещё {(until - now) / 3600:.0f} ч")
    susp = [t for sym in all_symbols(mem) for t in mem.get("trades", {}).get(sym, [])
            if _suspicious(t)]
    if susp:
        lost = sum(t.get("total") or 0 for t in susp)
        worst = sorted({t.get("symbol") or "?" for t in susp})[:3]
        out.append(f"подозрительные сделки: {len(susp)} шт. ({lost:+.2f} $) "
                   f"исключены из статистики — нулевое удержание без выплат "
                   f"funding ({', '.join(worst)}): разберите `sanity`")
    fails = [e for e in _events(mem) if e["kind"] == "fail"]
    if fails:
        top = {}
        for e in fails:
            top[e["symbol"]] = top.get(e["symbol"], 0) + 1
        worst = sorted(top.items(), key=lambda kv: -kv[1])[:3]
        txt = ", ".join(f"{s}×{c}" for s, c in worst)
        out.append(f"неудачные входы (ордер не исполнился): {txt} — стакан тонкий, "
                   f"входы туда стоят кулдауна")
    return out


# ---------------------------------------------------------------- калибровка порогов

def notional_guess(trade: dict) -> float:
    """Нотион, от которого считать % прибыли сделки.
    Приоритет — записанные проценты (pnl_pct/return_pct к total): так
    честно для позиций любого размера; fallback — 600 ($1000 × 2/(2+1) —
    типичный нотион тестов).

    ptn_pct НЕ ноль проверяется явно: сделка с total=0.0049 при капитале
    $1000 округляет pnl_pct до 0.0, и total/pct давал ZeroDivisionError —
    autotune() зовётся из cmd_close и из стартового блока cmd_watch
    (вне try), то есть ронял весь сервис."""
    total = trade.get("total") or 0.0
    for k in ("pnl_pct", "return_pct"):
        pct = trade.get(k)
        if pct is not None and total != 0 and pct != 0:
            notional = total / pct * 100.0
            # восстановленный нотион должен быть осмысленным: проценты в
            # записях из bot.log берутся от разных баз ($600, капитал),
            # мусорный знаменатель лучше отбросить, чем записать в статистику
            if notional > 0:
                return notional
    return 600.0


def autotune(mem: dict, force: bool = False, preset: str = LEGACY_PRESET,
             mode=None) -> str | None:
    """Раз в AUTO_EVERY_DAYS дней пересобрать пороги фильтра по факту.

    По каждой закрытой сделке за период считается фактическая прибыль в %
    нотион (total к notional_guess). Окно с хорошей ставкой (apy30 ≥ текущего
    min_apy30), в котором сделка убыточна, — значит финансирование не покрыло
    оплату входа (комиссии/базис/проскальзывание) — поднимает пороги:
      • min_apy30  (+AUTO_APY_STEP)  — заходить только в окна, что окупаются;
      • fast_apy   (+½ шага)         — вытеснить «почти годные» из предотбора;
      • min_basis  (+AUTO_BASIS_STEP)— не входить, когда перп уже дороже спота.
    Окупившиеся окна (прибыль ≥ AUTO_MIN_GOOD) опускают вклад обратно —
    но не ниже нуля (открывать шире дефолта без причин незачем).
    Результат — mem["profit_shift"]: вклад периода в ЕДИНУЮ формулу порогов
    _combine (base + bias + profit-сдвиг); применяет его та же пересборка
    _rebuild, что и calibrate — механизмы не конфликтуют, каждое изменение
    видно в threshold_log. Возвращает урок (для log/notify) или None.
    force=True — пересмотреть немедленно, не дожидаясь периода."""
    now_ms = int(time.time() * 1000)
    last = mem.get("autotune_ts") or 0      # хранится в мс, как updated_ms
    if not force and now_ms < last + AUTO_EVERY_DAYS * 86_400_000:
        return None
    # окно: сделки с прошлого тюнинга; force — стандартная неделя назад
    since = last if not force else now_ms - AUTO_EVERY_DAYS * 86_400_000
    # изоляция по режиму: экономика testnet и mainnet разная (комиссии,
    # ставки, проскальзывание) — вклад периода считается только по своим
    trades = [t for ts in mem.get("trades", {}).values() for t in ts
              if (t.get("ts") or 0) > since
              and not _suspicious(t)
              and mode_match(t.get("mode"), mode)]
    if len(trades) < AUTO_MIN_TRADES:
        mem["autotune_ts"] = now_ms
        return (f"за период сделок {len(trades)} (<{AUTO_MIN_TRADES}) — "
                f"пороги не трогаю, загляну через {AUTO_EVERY_DAYS} дней")
    # скоринг периода: дельта вклада. Убыточное окно поднимает свою часть
    # (financing не покрыл вход), окупившееся (≥AUTO_MIN_GOOD) опускает —
    # это была годная зона. Пол по вкладу — ноль: шире дефолта не открываем.
    # Льгота: сделка, закрытая ДО первой выплаты (funding=0, held < интервала
    # funding), убыточна только из-за комиссий — её окно в ставке не виновато,
    # пороги не двигает.
    cal = preset_of(mem, preset)
    base = base_of(cal, preset)
    d30 = df = db = 0.0
    scored = 0          # нешумовые сделки
    no_payout_losses = 0  # из них убыточных без единой выплаты (льгота)
    for t in trades:
        if (t.get("held_h") or 0) < MIN_HOLD_H:
            continue
        scored += 1
        pnl_pct = (t.get("total") or 0.0) / notional_guess(t) * 100.0
        paid_out = ((t.get("funding") or 0) > 0
                    or (t.get("interval_h") or 0) <= 0
                    or (t.get("held_h") or 0) >= t["interval_h"])
        if pnl_pct < 0 and paid_out:
            if t.get("apy30") is not None:
                d30 += AUTO_APY_STEP
                df += AUTO_APY_STEP * 0.5
            if t.get("basis_entry") is not None:
                db += AUTO_BASIS_STEP
        elif pnl_pct < 0:
            no_payout_losses += 1
        elif pnl_pct >= AUTO_MIN_GOOD and t.get("apy30") is not None:
            d30 -= AUTO_APY_STEP
            df -= AUTO_APY_STEP * 0.5
            db -= AUTO_BASIS_STEP
    if not scored:   # за период были только шумовые сделки (<0.5 ч) — вердикта нет
        mem["autotune_ts"] = now_ms
        return (f"{len(trades)} сделок, все короче "
                f"{MIN_HOLD_H}ч (шум) — пороги не трогаю, загляну через "
                f"{AUTO_EVERY_DAYS} дней")
    # вклад периода = profit-сдвиг единой формулы порогов: прошлая величина
    # плюс дельта периода, пол ноль, потолок — санитарный (сесть может только
    # через дельту вверх; вниз ниже нуля не нужно)
    # сдвиг своего пресета (изоляция): у mainnet и hold свои вклады; зеркалим
    # в общие ключи — их читают dashboard и печать apply_tune
    prev = cal.get("profit_shift") or {}
    shift = {"min_apy30": round(min(max(prev.get("min_apy30", 0.0) + d30, 0.0),
                                    AUTO_MAX_30 - base["min_apy30"]), 1),
             "fast_apy": round(max(prev.get("fast_apy", 0.0) + df, 0.0), 1),
             "min_basis": round(min(max(prev.get("min_basis", 0.0) + db, 0.0),
                                    AUTO_MAX_BASIS - base["min_basis"]), 2)}
    cal["profit_shift"] = shift
    mem["profit_shift"] = shift
    cal["autotune"] = {"ts": now_ms, "trades": len(trades), "scored": scored,
                       "shift": dict(shift)}
    mem["autotune"] = dict(cal["autotune"])
    mem["autotune_ts"] = now_ms
    cal["base"] = base
    lesson = _rebuild(mem, cal.get("bias"), "autotune",
                      f"автотюнинг по факту {scored} сделок", preset)
    head = f"автотюнинг по факту {scored} сделок за период: "
    if not lesson and no_payout_losses:
        lesson = (f"{no_payout_losses} убытков без единой выплаты funding — "
                  f"окно в ставке не виновато, пороги не трогаю")
    return head + (lesson if lesson else
                   "вошедшие окна окупаются — пороги без изменений")


# ---------------------------------------------------------------- maker vs taker

def _legs_style(legs) -> str | None:
    """Стиль перп-ноги входа из записи legs 'perp:spot' ( maker/taker или None)."""
    if not isinstance(legs, str) or not legs:
        return None
    first = legs.split(":")[0]
    return first if first in ("maker", "taker") else None


def mode_match(t_mode, mode) -> bool:
    """Подходит ли сделка среды t_mode фильтру mode.
    mode: None (все), строка или кортеж строк — какие среды учитывать.
    Нужно, чтобы paper-сделки (реальные ставки/комиссии mainnet, но
    симулированное заполнение) учили mainnet-пороги вместе с ним."""
    if mode is None:
        return True
    if isinstance(mode, (tuple, list, set, frozenset)):
        return t_mode in mode
    return t_mode == mode


def entry_mode_stats(mem: dict, mode=None) -> dict:
    """Фактическая эффективность maker- и taker-входов.
    Группы — по перп-ноге (первая компонента legs): именно она даёт главную
    разницу (комиссия maker/taker и проскальзывание, slip_bps пишется по ней).
    Нешумовые сделки (held_h ≥ MIN_HOLD_H) с известным legs.
    mode — считать только сделки этой среды (проскальзывание testnet
    синтетическое и не переносится на mainnet)."""
    out = {}
    for style in ("maker", "taker"):
        rows = [t for ts in mem.get("trades", {}).values() for t in ts
                if mode_match(t.get("mode"), mode)
                and (t.get("held_h") or 0) >= MIN_HOLD_H
                and not _suspicious(t)
                and _legs_style(t.get("legs")) == style]
        pcts = []
        for t in rows:
            if t.get("pnl_pct") is not None:
                pcts.append(t["pnl_pct"])
            else:
                pcts.append((t.get("total") or 0.0) / notional_guess(t) * 100.0)
        slips = [t["slip_bps"] for t in rows if t.get("slip_bps") is not None]
        out[style] = {
            "n": len(rows),
            "win_share": (sum(1 for x in pcts if x > 0) / len(pcts)) if pcts else 0.0,
            "avg_pct": (sum(pcts) / len(pcts)) if pcts else 0.0,
            "avg_slip_bps": (sum(slips) / len(slips)) if slips else None,
        }
    return out


def learn_entry_mode(mem: dict, mode=None) -> str | None:
    """Сравнить maker- и taker-входы по факту и записать предпочтение.

    Почему сравнение по PnL честное: maker-заполнения — классический
    adverse selection (лимитка исполняется, когда цена идёт СКВОЗЬ неё,
    т.е. против шорта), и это может съесть экономию на комиссии. Если факт
    говорит, что taker-входы в среднем прибыльнее — входим сразу маркетом.

    Условия вывода: в КАЖДОЙ группе ≥ MODE_MIN_TRADES нешумовых сделок и
    разница средних PnL% ≥ MODE_PNL_MARGIN. Итог — mem["entry_mode"] =
    {ts, maker, taker, prefer}; его читает open_position (флаг --taker-only
    остаётся сильнее памяти). Урок возвращается только при смене
    предпочтения (или первом выводе) — на каждом close лог не спамим."""
    stats = entry_mode_stats(mem, mode=mode)
    mk, tk = stats["maker"], stats["taker"]
    prev = (mem.get("entry_mode") or {}).get("prefer")
    prefer = None
    if mk["n"] >= MODE_MIN_TRADES and tk["n"] >= MODE_MIN_TRADES:
        diff = mk["avg_pct"] - tk["avg_pct"]
        if diff >= MODE_PNL_MARGIN:
            prefer = "maker"
        elif diff <= -MODE_PNL_MARGIN:
            prefer = "taker"
    mem["entry_mode"] = {"ts": int(time.time() * 1000),
                         "maker": mk, "taker": tk, "prefer": prefer}
    if prefer == prev:
        return None
    slip_s = ""
    if mk["avg_slip_bps"] is not None and tk["avg_slip_bps"] is not None:
        slip_s = (f", slip {mk['avg_slip_bps']:+.1f} vs "
                  f"{tk['avg_slip_bps']:+.1f} bps")
    if prefer == "maker":
        return (f"режим входа: maker лучше taker на "
                f"{mk['avg_pct'] - tk['avg_pct']:+.2f} п.п. "
                f"({mk['n']} maker vs {tk['n']} taker{slip_s}) — "
                f"вхожу maker-лимитками")
    if prefer == "taker":
        return (f"режим входа: taker лучше maker на "
                f"{tk['avg_pct'] - mk['avg_pct']:+.2f} п.п. "
                f"({tk['n']} taker vs {mk['n']} maker{slip_s}) — "
                f"вхожу сразу маркетом (maker-лимитки жгут время и "
                f"заполняются в худшей стороне)")
    if prev is not None:
        return (f"режим входа: разница maker/taker в пределах шума "
                f"(<{MODE_PNL_MARGIN:g} п.п.) — снова maker-попытка + фолбэк")
    return None


def realized_apy_pct(trade: dict) -> float | None:
    """Фактическая годовая доходность сделки в % (funding-часть) или None.

    Знаменатель — НОТИОН позиции, записанный при входе. Раньше стояли
    жёсткие $600: при --capital 5000 нотион втрое больше, realized завышался
    в разы, bias уезжал на десятки п.п. и уводил min_apy30 в потолок.
    pnl_pct тут не годится — это % от КАПИТАЛА (capital × L/(L+1) ≠ notional).
    Старые записи без notional — грубый fallback от $600."""
    hold_h = trade.get("held_h") or 0.0
    funding = trade.get("funding") or 0.0
    if hold_h <= 0 or funding <= 0:
        return None
    notional = trade.get("notional") or 0.0
    if notional <= 0:
        notional = 600.0
    return funding / notional * 100 * (24 * 365 / hold_h)


# --- единая пересборка порогов: bias прогноза + profit-сдвиг факта ----------

def _combine(mem: dict, bias: float | None,
             preset: str = LEGACY_PRESET) -> dict:
    """Итоговые применяемые пороги = base + bias-сдвиг + profit-сдвиг.
    bias — средняя ошибка прогноза APY (п.п.): завышен (<0) → пороги вверх,
    занижен (>0) → вниз, сдвиг мягкий (четверть смещения, ≤CAL_STEP_PCT,
    не дальше CAL_MAX_SHIFT от базы). profit-сдвиг (mem["profit_shift"])
    пишет autotune по фактической прибыли окон. Потолки санитарные."""
    cal = preset_of(mem, preset)
    base = base_of(cal, preset)
    shift = cal.get("profit_shift") or {}
    thr = {}
    for k in ("min_apy30", "fast_apy"):
        # bias < 0 (прогноз завышен) → пороги ВВЕРХ; generators занижены → вниз
        d = 0.0 if not bias else (1.0 if bias < 0 else -1.0) \
            * min(abs(bias) * 0.25, CAL_STEP_PCT)
        v = base[k] + d + shift.get(k, 0.0)
        v = min(max(v, base[k] - CAL_MAX_SHIFT), base[k] + CAL_MAX_SHIFT)
        thr[k] = round(v, 2)
    thr["min_apy30"] = min(thr["min_apy30"], AUTO_MAX_30)
    thr["fast_apy"] = min(thr["fast_apy"], thr["min_apy30"])
    thr["min_basis"] = round(min(max(base["min_basis"]
                                     + shift.get("min_basis", 0.0), 0.0),
                                 AUTO_MAX_BASIS), 2)
    return thr


def _log_thr(mem: dict, kind: str, trigger: str, before: dict, after: dict):
    """Запись решения о порогах в журнал (только при фактическом изменении)."""
    keys = ("min_apy30", "min_basis", "fast_apy")
    if before and all(abs(after[k] - before.get(k, after[k])) < 1e-9 for k in keys):
        return
    lg = mem.setdefault("threshold_log", [])
    lg.append({"ts": int(time.time() * 1000), "kind": kind, "trigger": trigger[:200],
               "before": {k: before.get(k) for k in keys}, "after": {k: after[k] for k in keys}})
    if len(lg) > THR_LOG_KEEP:
        mem["threshold_log"] = lg[-THR_LOG_KEEP:]


def _rebuild(mem: dict, bias: float | None, kind: str, trigger: str,
             preset: str = LEGACY_PRESET) -> str | None:
    """Пересобрать применяемые thresholds и записать решение в журнал.
    Пороги пишутся в секцию пресета mem["presets"][preset]["calibration"]
    (изоляция: каждый пресет учится на своих сделках) и зеркалятся в общий
    mem["calibration"] — туда смотрят dashboard и старый читатель.
    «До» при первом решении — от базы (thresholds ещё нет).
    Журнал и урок — только при фактическом изменении порогов."""
    cal = preset_of(mem, preset)
    base = base_of(cal, preset)
    keys = ("min_apy30", "fast_apy") if kind == "calibrate" \
        else ("min_apy30", "min_basis", "fast_apy")
    # «до»: пороги пресета; если у пресета их ещё не было — то, что было
    # применено ранее (зеркало в общем calibration), чтобы журнал видел
    # фактическую смену (напр. 12→10 при первом пересчёте)
    before = dict(cal.get("thresholds")
                  or mem.get("calibration", {}).get("thresholds") or {})
    thr = _combine(mem, bias, preset)
    cal["thresholds"] = thr
    # зеркало в общий calibration (dashboard, apply-tune-печатальщик):
    # не только thresholds, но и качество прогноза — иначе панель и
    # команда memory вечно показывали «данных мало», одновременно печатая
    # применённые пороги (n/bias/action жили только в секции пресета)
    mirror = mem.setdefault("calibration", {})
    mirror["thresholds"] = dict(thr)
    for k in ("n", "bias", "action", "last_update"):
        if k in cal:
            mirror[k] = cal[k]
    cmp_before = {k: (before[k] if before.get(k) is not None else base[k])
                  for k in keys}
    changed = [f"{k} {cmp_before[k]:g}→{thr[k]:g}%" for k in keys
               if abs(thr[k] - cmp_before[k]) > 1e-9]
    if not changed:
        return None
    _log_thr(mem, kind, trigger, cmp_before, thr)
    return f"пороги: {', '.join(changed)} ({trigger})"


def calibrate(mem: dict, preset: str = LEGACY_PRESET,
              mode=None) -> dict | None:
    """Сравнить прогноз APY (записан в сделке при входе) с фактом.

    Факт — реализованный funding в годовых, к PnL-части сознательно не
    прибавляем: PnL это шум цены, а оценка качества ПРОГНОЗА ставки должна
    опираться на ставку.
    Прогноз систематически завышен (факт < прогноз) → пороги вверх;
    занижен (факт > прогноз) → вниз. Пороги пересобираются единой функцией
    _combine (base + bias-сдвиг + profit-сдвиг от autotune), каждое
    фактическое изменение — запись в threshold_log. Возвращает урок или None."""
    cal = preset_of(mem, preset)
    # изоляция по режиму: прогноз/ставки/комиссии testnet и mainnet различаются,
    # поэтому калибровка считается только по сделкам ТОЙ среды, где работаем
    rows = [t for t in (t for ts in mem.get("trades", {}).values() for t in ts)
            if mode_match(t.get("mode"), mode)
            and t.get("apy30") is not None
            and (t.get("held_h") or 0) >= MIN_HOLD_H
            and not _suspicious(t)
            and (t.get("funding") or 0) > 0]
    if len(rows) < CAL_MIN_TRADES:
        cal["n"] = len(rows)
        return None
    errs = []
    for t in rows:
        actual = realized_apy_pct(t)
        if actual is None:
            continue
        errs.append(actual - t["apy30"])
    if len(errs) < CAL_MIN_TRADES:
        cal["n"] = len(rows)
        return None
    bias = sum(errs) / len(errs)               # >0 — прогноз занижен, <0 — завышен
    spread = (max(errs) - min(errs)) / 2 if errs else 0.0
    if abs(bias) <= spread / max(len(errs), 1) ** 0.5:
        cal.update({"n": len(errs), "bias": round(bias, 2), "action": "hold"})
        # пороги всё равно пересобираются: profit-сдвиг мог измениться;
        # «hold» — только оценка прогноза, урок про пороги не нужен
        _rebuild(mem, bias, "calibrate", f"прогноз в норме (bias {bias:+.1f} п.п.)",
                 preset)
        return None
    cal.update({"n": len(errs), "bias": round(bias, 2),
                "action": "up" if bias < 0 else "down",
                "last_update": int(time.time() * 1000)})
    tag = (f"прогноз APY {'завышен' if bias < 0 else 'занижен'} на "
           f"{abs(bias):.1f} п.п. ({len(errs)} сделок)")
    return _rebuild(mem, bias, "calibrate", tag, preset)


def apply_tune(mem: dict, preset: str = LEGACY_PRESET) -> str:
    """Принудительно применить сохранённый автотюнинг к фильтру: пересобрать
    thresholds = base + bias + profit-сдвиг прямо сейчас (memory --apply-tune).
    Полезно после ручной правки memory.json, после --relearn или чтобы увидеть,
    что именно применит фильтр. Журнал — только при фактическом изменении.
    Всегда возвращает итоговые пороги текстом."""
    cal = preset_of(mem, preset)
    cal["base"] = base_of(cal, preset)
    bias = cal.get("bias")
    lesson = _rebuild(mem, bias, "apply-tune",
                      "ручное применение автотюнинга (memory --apply-tune)",
                      preset)
    thr = cal["thresholds"]
    head = (f"пороги применены: min_apy30={thr['min_apy30']:g}%, "
            f"min_basis={thr['min_basis']:g}%, fast_apy={thr['fast_apy']:g}% "
            f"(base + bias {bias or 0:+.1f} + profit-сдвиг ")
    ps = mem.get("profit_shift") or {}
    head += (f"min_apy30 {ps.get('min_apy30', 0):+.1f}, "
             f"min_basis {ps.get('min_basis', 0):+.2f}, "
             f"fast_apy {ps.get('fast_apy', 0):+.1f})")
    return head + (f"; {lesson}" if lesson else " — без изменений")


def relearn(mem: dict, path: str = LOG_FILE) -> dict:
    """Полное переосмысление: ingest лога с нуля + свежие уроки.
    Калибровка тоже сбрасывается — она была построена на стёртых сделках
    (пороги пересчитаются, когда снова наберётся ≥5 сделок с прогнозом)."""
    mem["trades"] = {}
    mem["stops"] = {}
    mem["events"] = []
    mem["calibration"] = {}
    mem["autotune"] = {}
    mem["autotune_ts"] = 0.0
    mem["profit_shift"] = {}
    mem["entry_mode"] = {}
    mem["presets"] = {}      # выученные пороги по пресетам стираются вместе с сделками
    # threshold_log сохраняем: журнал решений — история, а не состояние
    mem["ingest_ts"] = 0.0
    counts = ingest_log(mem, path, 0.0)
    mem["lessons"] = [{"ts": int(time.time() * 1000), "text": t} for t in lessons(mem)]
    return counts
