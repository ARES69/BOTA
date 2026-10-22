#!/usr/bin/env python3
"""
Bybit Funding Monitor (v2 — с риск-фильтром)
============================================
Сканирует USDT-перпетуалы Bybit и оценивает дельта-нейтральную стратегию
"long spot + short perp" (сбор ставки финансирования).

Что считает по каждому символу:
  • ставки funding: текущая, средние за 7 и 30 дней (в годовых, APY)
  • базис перп/спот: входит ли позиция выгодно (нужна премия перпа ≥ 0)
  • полная стоимость входа: комиссии + спреды обеих ног + невыгодный базис
  • окупаемость входа в днях при средней ставке 30д
  • ВЕРДИКТ: прошёл бы символ фильтр бота (пороги настраиваются флагами)

Только ПУБЛИЧНЫЕ эндпоинты API v5 — ключи не нужны, ордеров не отправляет.

Примеры:
    python3 funding_monitor.py                          # базовый отчёт
    python3 funding_monitor.py --top 20 --capital 5000
    python3 funding_monitor.py --symbols BTCUSDT,ETHUSDT,SOLUSDT
    python3 funding_monitor.py --watch 60               # обновление раз в час
    python3 funding_monitor.py --json snapshot.json     # снимок данных
    python3 funding_monitor.py --preset spike           # режим охоты на аномалии
    python3 funding_monitor.py --min-apy30 15 --max-breakeven 10   # свои пороги
"""

import argparse
import json
import statistics
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

BASE_URL = "https://api.bybit.com"
DAYS_PER_YEAR = 365

# Комиссии Bybit для non-VIP (могут меняться — см. README):
DEFAULT_SPOT_FEE = 0.0010         # 0.10% спот
DEFAULT_PERP_TAKER_FEE = 0.00055  # 0.055% перпы taker
DEFAULT_PERP_MAKER_FEE = 0.0002   # 0.020% перпы maker

MAJORS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "BNBUSDT", "DOGEUSDT"]

# Пресеты риск-фильтра: два режима работы бота (см. вывод монитора)
# hold    — "спокойный рынок": вход по устойчивой средней ставке 30д, удержание неделями
# spike   — "аномалия": вход по ТЕКУЩЕЙ ставке, короткий холд, жёсткая окупаемость
# mainnet — реальные деньги: пороги с запасом на комиссию (круг taker ~0.31%
#           нотионла требует apy30 ≥ ~15% для осмысленного эджа на 8ч-интервалах;
#           ниже — сделка лишь корректно теряет на комиссиях)
PRESETS = {
    "hold":    dict(min_apy30=10.0, min_pos=80.0, min_basis=0.0, max_breakeven=14.0, pass_oi=20.0),
    "spike":   dict(min_apy30=30.0, min_pos=60.0, min_basis=0.0, max_breakeven=3.0,  pass_oi=50.0),
    "mainnet": dict(min_apy30=15.0, min_pos=85.0, min_basis=0.0, max_breakeven=10.0, pass_oi=30.0,
                    fast_apy=6.0),
}

WIDTH = 132


# ---------------------------------------------------------------- API helpers

def api_get(path: str, params: dict | None = None, timeout: int = 15):
    """GET к публичному API Bybit v5 (только stdlib, без зависимостей)."""
    url = BASE_URL + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "funding-monitor/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode())
    if data.get("retCode") != 0:
        raise RuntimeError(f"Bybit API error {data.get('retCode')}: {data.get('retMsg')}")
    return data["result"]


def fetch_linear_tickers() -> list[dict]:
    """Все USDT-перпетуалы со ставкой финансирования (цены + стакан top1)."""
    result = api_get("/v5/market/tickers", {"category": "linear"})
    rows = []
    for t in result.get("list", []):
        sym = t.get("symbol", "")
        if not sym.endswith("USDT") or not t.get("fundingRate"):
            continue
        try:
            rows.append({
                "symbol": sym,
                "price": float(t["lastPrice"]),
                "perp_bid": float(t.get("bid1Price") or 0),
                "perp_ask": float(t.get("ask1Price") or 0),
                "funding_rate": float(t["fundingRate"]),            # ставка за интервал
                "interval_h": float(t.get("fundingIntervalHours") or 8),
                "next_funding_ms": int(t.get("nextFundingTime") or 0),
                "turnover24h": float(t.get("turnover24h") or 0),    # USDT
                "oi_value": float(t.get("openInterestValue") or 0),  # USDT
            })
        except (ValueError, TypeError, KeyError):
            continue
    return rows


def fetch_spot_quotes() -> dict[str, tuple[float, float]]:
    """Стакан top1 всех спот-пар: symbol -> (bid, ask)."""
    result = api_get("/v5/market/tickers", {"category": "spot"})
    out = {}
    for t in result.get("list", []):
        try:
            out[t["symbol"]] = (float(t.get("bid1Price") or 0),
                                float(t.get("ask1Price") or 0))
        except (ValueError, TypeError, KeyError):
            continue
    return out


def fetch_funding_history(symbol: str, limit: int = 200) -> list[tuple[int, float]]:
    """История ставок: [(timestamp_ms, rate), ...] от старых к новым."""
    result = api_get("/v5/market/funding/history",
                     {"category": "linear", "symbol": symbol, "limit": limit})
    rows = []
    for h in result.get("list", []):
        try:
            rows.append((int(h["fundingRateTimestamp"]), float(h["fundingRate"])))
        except (ValueError, TypeError, KeyError):
            continue
    rows.sort(key=lambda x: x[0])
    return rows


# ---------------------------------------------------------------- analytics

def apy_from_rate(rate: float, interval_h: float) -> float:
    """Ставка за интервал -> годовых в %.
    интервалов в году = (24 / interval_h) * 365. Пример: 0.01% за 8ч -> ~10.95% годовых."""
    return rate * (24.0 / interval_h) * DAYS_PER_YEAR * 100.0


def infer_interval_hours(history: list[tuple[int, float]], fallback_h: float) -> float:
    if len(history) < 3:
        return fallback_h
    diffs = [(history[i + 1][0] - history[i][0]) / 3_600_000 for i in range(len(history) - 1)]
    diffs = [d for d in diffs if 0.2 < d < 48]
    if not diffs:
        return fallback_h
    return max(0.5, round(statistics.median(diffs)))


def analyze_symbol(row: dict, now_ms: int) -> dict:
    """Текущая ставка + статистика за 7 и 30 дней по истории funding."""
    out = dict(row)
    out["cur_apy"] = apy_from_rate(row["funding_rate"], row["interval_h"])

    try:
        history = fetch_funding_history(row["symbol"])
    except Exception as e:  # noqa: BLE001
        out["error"] = f"history failed: {e}"
        history = []

    if history:
        interval_h = infer_interval_hours(history, row["interval_h"])
        out["interval_h"] = interval_h
        out["cur_apy"] = apy_from_rate(row["funding_rate"], interval_h)

        def window_stats(days: float) -> dict | None:
            cutoff = now_ms - days * 86_400_000
            rates = [r for ts, r in history if ts >= cutoff]
            if not rates:
                return None
            apys = [apy_from_rate(r, interval_h) for r in rates]
            return {
                "avg_apy": statistics.mean(apys),
                "pos_share": 100.0 * sum(1 for r in rates if r > 0) / len(rates),
                "n": len(rates),
                "days_covered": days,
                # хвостовая статистика — для исследования "токсичных" интервалов
                "sum_pct": sum(rates) * 100.0,    # сколько % нотиона заработано за окно
                "worst_pct": min(rates) * 100.0,  # худший интервал
                "best_pct": max(rates) * 100.0,   # лучший интервал
            }

        out["w7"] = window_stats(7)
        w30 = window_stats(30)
        if w30 and w30["n"] < 0.8 * (30 * 24 / interval_h):
            span_h = (history[-1][0] - history[0][0]) / 3_600_000
            apys = [apy_from_rate(r, interval_h) for _, r in history]
            rates = [r for _, r in history]
            w30 = {
                "avg_apy": statistics.mean(apys),
                "pos_share": 100.0 * sum(1 for r in rates if r > 0) / len(rates),
                "n": len(rates),
                "days_covered": round(span_h / 24, 1),
                "sum_pct": sum(rates) * 100.0,
                "worst_pct": min(rates) * 100.0,
                "best_pct": max(rates) * 100.0,
            }
        out["w30"] = w30
    time.sleep(0.12)  # бережём rate limit
    return out


def spot_match(sym: str, spot: dict) -> tuple[str | None, float]:
    """Ищем спот-ногу для перп-символа (учитывая префиксы 1000/1000000)."""
    if sym in spot:
        return sym, 1.0
    for mult, prefix in ((1_000_000.0, "1000000"), (1000.0, "1000")):
        if sym.startswith(prefix) and sym[len(prefix):] in spot:
            return sym[len(prefix):], mult
    return None, 1.0


def enrich(r: dict, spot: dict, args) -> dict:
    """Блок 2: базис, стоимость входа, окупаемость, вердикт фильтра."""
    fees_rt = 2 * (args.spot_fee + args.perp_taker_fee) * 100  # комиссия taker туда-обратно, %

    # mid и спред перп-ноги
    if r.get("perp_bid") and r.get("perp_ask") and r["perp_ask"] > 0:
        pmid = (r["perp_bid"] + r["perp_ask"]) / 2
        spread_rt = (r["perp_ask"] - r["perp_bid"]) / pmid * 100
    else:
        pmid = r["price"]
        spread_rt = 0.0

    # базис к споту и спред спот-ноги
    r["basis"] = None
    skey, mult = spot_match(r["symbol"], spot)
    if skey:
        sb, sa = spot[skey]
        if sb > 0 and sa > 0:
            smid = (sb + sa) / 2
            r["basis"] = (pmid / (smid * mult) - 1) * 100
            r["spot_symbol"] = skey
            spread_rt += (sa - sb) / smid * 100

    # полная стоимость круга: комиссии + спреды + невыгодный базис
    unfavourable_basis = max(0.0, -(r["basis"] if r["basis"] is not None else 0.0))
    r["entry_cost"] = fees_rt + spread_rt + unfavourable_basis

    # окупаемость в днях: по консервативной ставке (средняя 30д) и по текущей
    w30 = r.get("w30")
    r["breakeven_d"] = None
    if w30 and w30["avg_apy"] > 0:
        r["breakeven_d"] = r["entry_cost"] / (w30["avg_apy"] / DAYS_PER_YEAR)
    r["breakeven_d_cur"] = None
    if r.get("cur_apy", 0) > 0:
        r["breakeven_d_cur"] = r["entry_cost"] / (r["cur_apy"] / DAYS_PER_YEAR)

    # вердикт: прошёл бы символ фильтр бота (пороги зависят от пресета)
    spike = getattr(args, "preset", "hold") == "spike"
    if spike:
        rate_apy = r.get("cur_apy")            # режим аномалии: смотрим на ТЕКУЩУЮ ставку
        w = r.get("w7") or w30                 # стабильность — по 7 дням
        be = r["breakeven_d_cur"]
    else:
        rate_apy = w30["avg_apy"] if w30 else None  # режим удержания: средняя 30д
        w = w30
        be = r["breakeven_d"]

    reasons = []
    if r["basis"] is None:
        reasons.append("нет спот-ноги")
    if rate_apy is None or rate_apy < args.min_apy30:
        reasons.append("низкая ставка")
    if w and w["pos_share"] < args.min_pos:
        reasons.append("нестабильна")
    if r["basis"] is not None and r["basis"] < args.min_basis:
        reasons.append(f"базис {r['basis']:+.3f}%")
    if be is None or be > args.max_breakeven:
        reasons.append("долгий окуп")
    if r.get("oi_value", 0) < args.pass_oi * 1e6:
        reasons.append("тонкая ликв")
    r["verdict_ok"] = not reasons
    r["verdict"] = "✓ ГОДЕН" if not reasons else "✗ " + "; ".join(reasons[:2])
    r["fail_reasons"] = reasons
    r["breakeven_shown"] = be
    return r


def conservative_apy(r: dict) -> float:
    w30 = r.get("w30")
    return w30["avg_apy"] if w30 else r.get("cur_apy", 0.0)


def liq_tier(r: dict) -> str:
    oi = r.get("oi_value", 0)
    if oi >= 100e6:
        return "высокая"
    if oi >= 20e6:
        return "средняя"
    return "НИЗКАЯ"


def fmt_money(x: float) -> str:
    if x >= 1e9:
        return f"${x / 1e9:.2f}B"
    if x >= 1e6:
        return f"${x / 1e6:.1f}M"
    if x >= 1e3:
        return f"${x / 1e3:.0f}K"
    return f"${x:.0f}"


# ---------------------------------------------------------------- вывод

def print_table(rows: list[dict]):
    hdr = (f"{'СИМВОЛ':<13} {'ИНТР':>4} {'СТАВКА':>9} {'APY 7д':>8} {'APY 30д':>9} "
           f"{'+30д':>5} {'БАЗИС':>8} {'ВХОД':>6} {'ОКУП':>6} {'OI':>7} {'ЛИКВ':>7}  ВЕРДИКТ")
    print(hdr)
    print("-" * WIDTH)
    for r in rows:
        w7 = r.get("w7") or {}
        w30 = r.get("w30") or {}
        cur = f"{r['funding_rate'] * 100:+.4f}%"
        a7 = f"{w7['avg_apy']:+.1f}%" if w7 else "н/д"
        a30v = f"{w30['avg_apy']:+.1f}%" if w30 else "н/д"
        note = "*" if w30 and w30.get("days_covered", 30) < 25 else ""
        pos = f"{w30['pos_share']:.0f}%" if w30 else "н/д"
        bas = f"{r['basis']:+.3f}%" if r.get("basis") is not None else "нет"
        ec = f"{r['entry_cost']:.2f}%" if r.get("entry_cost") is not None else "—"
        be_val = r.get("breakeven_shown", r.get("breakeven_d"))
        be = f"{be_val:.1f}д" if be_val is not None else "∞"
        print(f"{r['symbol']:<13} {r['interval_h']:>3.0f}ч {cur:>9} {a7:>8} {a30v + note:>9} "
              f"{pos:>5} {bas:>8} {ec:>6} {be:>6} {fmt_money(r['oi_value']):>7} "
              f"{liq_tier(r):>7}  {r.get('verdict', '')}")
    print("-" * WIDTH)


def portfolio_block(title: str, rows: list[dict], args) -> dict | None:
    usable = [r for r in rows if (r.get("w30") or {}).get("avg_apy") is not None]
    if not usable:
        print(f"\n  {title}: сейчас ни один символ не подходит.")
        return None
    capital = args.capital
    hold_days = args.hold_days
    rt_taker = 2 * (args.spot_fee + args.perp_taker_fee) * 100
    rt_maker = 2 * (args.spot_fee + args.perp_maker_fee) * 100

    avg_apy = statistics.mean(conservative_apy(r) for r in usable)
    gross_m = capital * avg_apy / 100 / 12
    net_m_taker = gross_m - capital * rt_taker / 100
    net_m_maker = gross_m - capital * rt_maker / 100
    # hold_days=0 (пользовательский флаг) делил на ноль — падал весь отчёт
    net_apy = (avg_apy - rt_taker * 365 / hold_days) if hold_days > 0 else 0.0

    print(f"\n  {title}: ${capital:,.0f} поровну в {len(usable)} символах")
    print(f"    Консервативная ставка (средняя 30д): {avg_apy:+.1f}% годовых")
    print(f"    Грязный доход за месяц:              ${gross_m:>+9.2f}")
    print(f"    Комиссии вход+выход (taker, {rt_taker:.3f}%): -${capital * rt_taker / 100:>7.2f}")
    print(f"    Чистыми за месяц (taker-вход):       ${net_m_taker:>+9.2f}")
    print(f"    Чистыми за месяц (maker-вход):       ${net_m_maker:>+9.2f}")
    print(f"    Чистая годовая при удержании {hold_days:>2} дн.:  {net_apy:+.1f}%")
    if avg_apy <= 0:
        print("    Внимание: средняя ставка отрицательная — позиция ПЛАТИТ, а не зарабатывает.")

    return {
        "n_symbols": len(usable),
        "symbols": [r["symbol"] for r in usable],
        "avg_30d_apy": avg_apy,
        "gross_month": gross_m,
        "net_month_taker": net_m_taker,
        "net_month_maker": net_m_maker,
        "net_apy_taker": net_apy,
    }


def print_report(top_rows: list[dict], major_rows: list[dict], args, now_ms: int) -> dict:
    ts = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    preset = getattr(args, "preset", "hold")
    preset_ru = ("HOLD — спокойный рынок (по средней ставке 30д, удержание неделями)"
                 if preset == "hold" else
                 "SPIKE — охота на аномалии (по ТЕКУЩЕЙ ставке, короткий холд)")
    print("=" * WIDTH)
    print(f"  BYBIT FUNDING MONITOR v2 — {ts}")
    print(f"  Стратегия: long spot + short perp (дельта-нейтрально), собираем funding")
    print(f"  Режим фильтра: {preset_ru}")
    print("=" * WIDTH)

    print("\n  ТОП ВОЗМОЖНОСТЕЙ (сортировка по средней ставке за 30 дней)")
    print_table(top_rows)

    if major_rows:
        print("\n  ОРИЕНТИР — МЕЙДЖОРЫ")
        print_table(major_rows)

    print("""
  КАК ЧИТАТЬ:
    СТАВКА  — фандинг ближайшей выплаты (за интервал). APY — то же в годовых.
    APY 30д — средняя годовая ставка за 30 дней; самая честная оценка.
    +30д    — доля плюсовых интервалов за 30 дней (стабильность выплат).
    БАЗИС   — перп дороже (+) или дешевле (−) спота. Для входа нужна премия ≥ 0:
              шортим дорогое, покупаем дешёвое. «нет» = спот-ноги на Bybit нет.
    ВХОД    — полная стоимость круга: комиссии taker + спреды ног + невыгодный базис.
    ОКУП    — за сколько дней ставка отобьёт ВХОД (в режиме spike — по текущей
              ставке, в режиме hold — по средней за 30д). ∞ = ставка не плюсовая.
    ВЕРДИКТ — прошёл бы символ фильтр бота. Пороги зависят от пресета --preset:
              hold:  APY30д ≥ 10%, плюсовых интервалов ≥ 80%, базис ≥ 0,
                     окуп ≤ 14д, OI ≥ $20M
              spike: текущий APY ≥ 30%, плюсовых 7д ≥ 60%, базис ≥ 0,
                     окуп ≤ 3д, OI ≥ $50M
              Любой порог переопределяется флагами --min-apy30/--min-pos/
              --min-basis/--max-breakeven/--pass-oi.
    *       — истории на полные 30 дней нет, среднее по доступному окну""")

    # --- портфели ---
    pool = {r["symbol"]: r for r in top_rows}
    for r in major_rows:
        pool.setdefault(r["symbol"], r)
    passed = [r for r in pool.values() if r.get("verdict_ok")]
    passed.sort(key=conservative_apy, reverse=True)

    portfolio = {"passed": None, "top": None, "majors": None}
    if passed:
        portfolio["passed"] = portfolio_block(
            "✅ ПРОШЛИ ФИЛЬТР (то, что бот держал бы сейчас)", passed, args)
    else:
        print("\n  ✅ ПРОШЛИ ФИЛЬТР: сейчас ни один символ не годится — бот бы ждал в стейблкоинах.")
        print("     Это нормальное состояние: фандинг-арбитраж — игра ожидания окон.")

    # near-miss лог: символы, срезавшиеся по ОДНОМУ критерию — за ними следить в первую очередь
    near = [r for r in pool.values()
            if not r.get("verdict_ok") and len(r.get("fail_reasons", [])) == 1]
    near.sort(key=lambda r: -r.get("oi_value", 0))
    if near:
        print("\n  👀 В ШАГЕ ОТ ВХОДА (не хватает одного критерия) — наблюдать в первую очередь:")
        for r in near[:5]:
            w30 = r.get("w30") or {}
            print(f"     {r['symbol']:<13} APY30д {w30.get('avg_apy', 0):+6.1f}%  "
                  f"ставка {r['funding_rate'] * 100:+.4f}%  "
                  f"OI {fmt_money(r.get('oi_value', 0)):>8}  — не хватает: {r['fail_reasons'][0]}")
    portfolio["near_misses"] = [
        {"symbol": r["symbol"], "reason": r["fail_reasons"][0],
         "apy30": (r.get("w30") or {}).get("avg_apy"), "cur_apy": r.get("cur_apy"),
         "basis": r.get("basis"), "oi": r.get("oi_value")}
        for r in near[:10]
    ]

    portfolio["top"] = portfolio_block(
        "ПОРТФЕЛЬ ПО ТОП-СПИСКУ (агрессивно, БЕЗ фильтра — для сравнения)", top_rows, args)
    if major_rows:
        portfolio["majors"] = portfolio_block(
            "ПОРТФЕЛЬ ПО МЕЙДЖОРАМ (консервативно)", major_rows, args)

    print()
    print("  ⚠ РИСКИ: ставка может уйти в минус; микрокапы — делистинг/тонкий спот;")
    print("    скачок цены может ликвидировать perp-ногу (плечо ≤ 2-3x, буфер, cross/UTA);")
    print("    базис при выходе может оказаться против вас; funding платят только позиции,")
    print("    открытой на момент timestamp выплаты.")
    print("  Это монитор возможностей, а не сигнал «входить прямо сейчас».")
    print("=" * WIDTH)
    return portfolio


# ---------------------------------------------------------------- main flow

def run_once(args) -> dict:
    now_ms = int(time.time() * 1000)
    tickers_all = fetch_linear_tickers()
    spot_quotes = fetch_spot_quotes()
    by_symbol = {t["symbol"]: t for t in tickers_all}

    if args.symbols:
        wanted = [s.strip().upper() for s in args.symbols.split(",")]
        candidates = [by_symbol[s] for s in wanted if s in by_symbol]
    else:
        candidates = [
            t for t in tickers_all
            if t["turnover24h"] >= args.min_volume * 1e6
            and t["oi_value"] >= args.min_oi * 1e6
        ]
        candidates.sort(key=lambda t: t["funding_rate"], reverse=True)
        candidates = candidates[: args.prefilter]

    if not candidates:
        print("Нет символов, удовлетворяющих фильтрам. Снизьте --min-volume / --min-oi.")
        return {}

    analyzed = [analyze_symbol(t, now_ms) for t in candidates]
    for r in analyzed:
        enrich(r, spot_quotes, args)
    analyzed.sort(key=conservative_apy, reverse=True)
    top_rows = analyzed if args.symbols else analyzed[: args.top]

    picked = {r["symbol"] for r in top_rows}
    majors: list[dict] = []
    if not args.symbols:
        wanted_majors = args.majors.split(",") if args.majors else MAJORS
        extra = [by_symbol[s] for s in wanted_majors if s in by_symbol and s not in picked]
        majors = [analyze_symbol(t, now_ms) for t in extra]
        for r in majors:
            enrich(r, spot_quotes, args)
        majors_in_top = [r for r in top_rows if r["symbol"] in set(wanted_majors)]
        majors = majors_in_top + majors

    portfolio = print_report(top_rows, majors, args, now_ms)

    if args.json:
        snapshot = {
            "timestamp_ms": now_ms,
            "iso": datetime.now(timezone.utc).isoformat(),
            "params": dict(vars(args)),
            "top_rows": top_rows,
            "major_rows": majors,
            "portfolio": portfolio,
        }
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(snapshot, f, ensure_ascii=False, indent=2)
        print(f"\n  Снимок сохранён: {args.json}")
    return {"top_rows": top_rows, "majors": majors, "portfolio": portfolio}


def main():
    p = argparse.ArgumentParser(
        description="Монитор ставок финансирования Bybit + риск-фильтр (дельта-нейтральная стратегия)")
    p.add_argument("--top", type=int, default=12,
                   help="сколько символов показывать в топе (default 12)")
    p.add_argument("--prefilter", type=int, default=36,
                   help="сколько кандидатов тянуть историю для (default 36)")
    p.add_argument("--min-volume", type=float, default=5.0,
                   help="мин. оборот 24ч, $M (default 5)")
    p.add_argument("--min-oi", type=float, default=5.0,
                   help="мин. открытый интерес, $M (default 5)")
    p.add_argument("--capital", type=float, default=1000.0,
                   help="депозит для оценки, $ (default 1000)")
    p.add_argument("--hold-days", type=int, default=30,
                   help="срок удержания для амортизации комиссий (default 30)")
    p.add_argument("--symbols", type=str, default=None,
                   help="конкретные символы через запятую: BTCUSDT,ETHUSDT")
    p.add_argument("--majors", type=str, default=None,
                   help="свой список мейджоров (default BTCUSDT,ETHUSDT,SOLUSDT,XRPUSDT,BNBUSDT,DOGEUSDT)")
    # комиссии
    p.add_argument("--spot-fee", type=float, default=DEFAULT_SPOT_FEE,
                   help="комиссия спот (default 0.001)")
    p.add_argument("--perp-taker-fee", type=float, default=DEFAULT_PERP_TAKER_FEE,
                   help="комиссия перп taker (default 0.00055)")
    p.add_argument("--perp-maker-fee", type=float, default=DEFAULT_PERP_MAKER_FEE,
                   help="комиссия перп maker (default 0.0002)")
    # пороги риск-фильтра (вердикт); None = взять из пресета
    p.add_argument("--preset", choices=["hold", "spike"], default="hold",
                   help="hold: устойчивая ставка 30д, окуп ≤14д, APY30д ≥10%%; "
                        "spike: аномалия по текущей ставке, APY ≥30%%, окуп ≤3д, OI ≥$50M (default hold)")
    p.add_argument("--min-apy30", type=float, default=None,
                   help="фильтр: мин. APY (30д для hold / текущий для spike), %% (hold:10 spike:30)")
    p.add_argument("--min-pos", type=float, default=None,
                   help="фильтр: мин. доля плюсовых интервалов, %% (hold:80 spike:60)")
    p.add_argument("--min-basis", type=float, default=None,
                   help="фильтр: мин. базис перп/спот, %% (default 0)")
    p.add_argument("--max-breakeven", type=float, default=None,
                   help="фильтр: макс. окупаемость входа, дней (hold:14 spike:3)")
    p.add_argument("--pass-oi", type=float, default=None,
                   help="фильтр: мин. OI для вердикта ГОДЕН, $M (hold:20 spike:50)")
    # режимы
    p.add_argument("--json", type=str, default=None, help="сохранить снимок в JSON")
    p.add_argument("--watch", type=int, default=None,
                   help="обновлять каждые N минут (Ctrl+C — выход)")
    args = p.parse_args()

    # пороги, не заданные явно, добираем из пресета
    for key, default in PRESETS[args.preset].items():
        if getattr(args, key) is None:
            setattr(args, key, default)

    if args.watch:
        try:
            while True:
                try:
                    run_once(args)
                except Exception as e:  # noqa: BLE001
                    print(f"[{datetime.now():%H:%M:%S}] Ошибка: {e}", file=sys.stderr)
                print(f"\n  Следующее обновление через {args.watch} мин (Ctrl+C — выход)\n")
                time.sleep(args.watch * 60)
        except KeyboardInterrupt:
            print("\nОстановлено.")
    else:
        try:
            run_once(args)
        except Exception as e:  # noqa: BLE001
            print(f"Ошибка: {e}", file=sys.stderr)
            sys.exit(1)


if __name__ == "__main__":
    main()
