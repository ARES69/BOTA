#!/usr/bin/env python3
"""
Бэктест дельта-нейтральной стратегии по историческим данным Bybit
================================================================
Проверяет пороги ДО того, как они коснутся денег: «если бы бот входил по
текущим правилам последние N месяцев — какой был бы PnL и какая просадка».

Ключевое требование — ЧЕСТНОСТЬ. Поэтому:

  • вердикт пересчитывается на каждом шаге только по данным, доступным
    В ТОТ момент (никакого look-ahead: будущие ставки не подглядываются);
  • вход моделируется по худшей цене (ask на входе, bid на выходе) плюс
    спред из настроек — то есть результат пессимистичный, а не розовый;
  • выплаты funding берутся из фактической истории биржи, а не из прогноза APY;
  • соблюдаются те же правила выхода, что у живого бота (hold-to-N,
    отрицательные интервалы подряд, стоп по риску маржи).

Что бэктест НЕ моделирует (и это честно оговаривается в отчёте):
  • глубину стакана и проскальзывание — вместо них фиксированный спред;
  • реальные исполнения: maker-вход в live часто проходит лучше, чем ask
    в бэктесте, поэтому backtest систематически пессимистичен;
  • комиссии по фактической VIP-ставке аккаунта — берутся дефолтные non-VIP;
  • выход по ликвидации (слишком крупное плечо) — плечо ограничено 3.

Команды:
    python backtest.py                          # топ-20 символов, 60 суток
    python backtest.py --symbols BTCUSDT,ETHUSDT
    python backtest.py --top 30 --capital 1000 --preset mainnet
    python backtest.py --sweep                 # перебор порогов min_apy30
    python backtest.py --sweep --quick         # перебор по готовым данным
    python backtest.py --json report.json      # сохранить отчёт

Данные — только публичные эндпоинты Bybit, ключи не нужны.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from funding_monitor import (  # noqa: E402
    DAYS_PER_YEAR,
    PRESETS,
    apy_from_rate,
    enrich,
    fetch_linear_tickers,
    fetch_spot_quotes,
    fmt_money,
    spot_match,
)

DAYS_MS = 86_400_000
HOUR_MS = 3_600_000


# ------------------------------------------------------------------ сбор данных
def api(path: str, params: dict, timeout: int = 20):
    """Публичный GET Bybit (own-реализация: нужны оба класса рынка)."""
    import urllib.parse
    import urllib.request
    url = "https://api.bybit.com" + path + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "funding-backtest/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode())
    if data.get("retCode") != 0:
        raise RuntimeError(f"Bybit {data.get('retCode')}: {data.get('retMsg')}")
    return data["result"]


def fetch_funding(symbol: str, limit: int = 200) -> list[tuple[int, float]]:
    """История ставок [(ts_ms, rate)] от старых к новым."""
    res = api("/v5/market/funding/history",
              {"category": "linear", "symbol": symbol, "limit": limit})
    rows = []
    for h in res.get("list", []):
        try:
            rows.append((int(h["fundingRateTimestamp"]),
                         float(h["fundingRate"])))
        except (ValueError, TypeError, KeyError):
            continue
    rows.sort()
    return rows


def fetch_klines(symbol: str, category: str, interval: str = "60",
                 limit: int = 200) -> list[tuple[int, float]]:
    """Свечи [(ts_ms, close)] по возрастанию — нужны для цены на шаге."""
    res = api("/v5/market/kline",
              {"category": category, "symbol": symbol,
               "interval": interval, "limit": limit})
    rows = []
    for r in res.get("list") or []:
        try:
            rows.append((int(r[0]), float(r[4])))
        except (ValueError, TypeError, IndexError):
            continue
    rows.sort()
    return rows


def fetch_price_history(symbol: str, category: str,
                        days: int = 75) -> list[tuple[int, float]]:
    """История цен с РАЗРЕШЕНИЕМ ПОД ОКНО ФАНДИНГА.

    Bybit отдаёт максимум 200 свечей за запрос. Часовые — ≈8 суток, 4-часовые
    ≈33 суток, а история ставок (200 точек × 8ч) покрывает ≈66 суток. Чтобы
    симуляция шла по всей доступной истории ставок, а не по её трети, для
    длинных окон берём дневные свечи (200 × 24ч ≈ 2 года) — шаг симуляции всё
    равно равен интервалу выплат (8ч или 1ч), точность цены на вердикт и PnL
    практически не влияет, зато покрытие полное."""
    for interval, limit in (("D", 200), ("240", 200), ("60", 200)):
        res = api("/v5/market/kline",
                  {"category": category, "symbol": symbol,
                   "interval": interval, "limit": limit})
        rows = []
        for r in res.get("list") or []:
            try:
                rows.append((int(r[0]), float(r[4])))
            except (ValueError, TypeError, IndexError):
                continue
        if not rows:
            continue
        rows.sort()
        span_days = (rows[-1][0] - rows[0][0]) / 86_400_000
        if span_days >= days:
            return rows
        # окно не покрыто — пробуем более детальный таймфрейм
    return rows if rows else []


def pick_symbols(top: int, min_oi: float) -> list[dict]:
    """Жидкие USDT-перпы со спот-парой — из них бот реально берёт ноги."""
    tickers = fetch_linear_tickers()
    spot = fetch_spot_quotes()
    out = []
    for t in tickers:
        sym = t["symbol"]
        if not sym.endswith("USDT") or t["oi_value"] < min_oi * 1e6:
            continue
        if t["turnover24h"] < 3e6:
            continue
        skey, mult = spot_match(sym, spot)
        if not skey or mult != 1.0:
            # мультипликаторы (1000XXX) в бэктесте не моделируем: цены
            # разных масштабов слишком легко спутать, а доля таких символов
            # в выборе мала. Для честности они просто исключены.
            continue
        out.append(t)
    out.sort(key=lambda t: t["oi_value"], reverse=True)
    return out[:top]


class SymData:
    """Всё нужное по одному символу, приведённое к общему таймлайну."""

    __slots__ = ("symbol", "spot_symbol", "interval_h", "funding", "perp",
                 "spot", "oi", "turnover")

    def __init__(self, symbol, spot_symbol, interval_h, funding, perp, spot,
                 oi, turnover):
        self.symbol = symbol
        self.spot_symbol = spot_symbol
        self.interval_h = interval_h
        self.funding = funding          # [(ts, rate)]
        self.perp = perp                # [(ts, close)]
        self.spot = spot                # [(ts, close)]
        self.oi = oi
        self.turnover = turnover

    def perp_at(self, ts: int) -> float | None:
        """Цена перпа на шаге ts (последняя свеча ≤ ts — без look-ahead)."""
        v = _last_le(self.perp, ts)
        return v[1] if v else None

    def spot_at(self, ts: int) -> float | None:
        v = _last_le(self.spot, ts)
        return v[1] if v else None

    def funding_upto(self, ts: int) -> list[tuple[int, float]]:
        """Ставки, известные на момент ts (строго ≤ — без утечки будущего)."""
        return [(t, r) for t, r in self.funding if t <= ts]


def _last_le(seq: list[tuple[int, float]], ts: int):
    """Последний элемент с ключом ≤ ts (бинарный поиск)."""
    lo, hi, best = 0, len(seq) - 1, None
    while lo <= hi:
        mid = (lo + hi) // 2
        if seq[mid][0] <= ts:
            best = seq[mid]
            lo = mid + 1
        else:
            hi = mid - 1
    return best


def infer_interval(history: list[tuple[int, float]], fallback: float = 8.0) -> float:
    """Периодичность выплат по медиане разностей (как у монитора)."""
    if len(history) < 3:
        return fallback
    d = sorted((history[i + 1][0] - history[i][0]) / HOUR_MS
               for i in range(len(history) - 1))
    d = [x for x in d if 0.2 < x < 48]
    if not d:
        return fallback
    return max(0.5, round(statistics.median(d)))


def load_dataset(top: int, min_oi: float, verbose: bool = True) -> list[SymData]:
    """Собирает данные по отобранным символам. Кэш в backtest_data.json."""
    cache_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "backtest_data.json")
    if os.path.exists(cache_path):
        try:
            with open(cache_path, encoding="utf-8") as f:
                raw = json.load(f)
            out = [SymData(**d) for d in raw]
            if verbose:
                print(f"данные из кэша: {len(out)} символов")
            return out
        except (OSError, ValueError, TypeError):
            pass

    tickers = pick_symbols(top, min_oi)
    out = []
    for i, t in enumerate(tickers, 1):
        sym = t["symbol"]
        try:
            fh = fetch_funding(sym, 200)
            if len(fh) < 10:
                continue
            skey, _ = spot_match(sym, fetch_spot_quotes())
            if not skey:
                continue
            # 4-часовые свечи: 200 точек ≈ 33 суток, сопоставимо с
            # окном истории ставок (часовые не покрывают и десятой части)
            perp = fetch_price_history(sym, "linear")
            spot = fetch_price_history(skey, "spot")
            if len(perp) < 10:
                continue
            out.append(SymData(sym, skey, infer_interval(fh, t["interval_h"]),
                               fh, perp, spot, t["oi_value"],
                               t["turnover24h"]))
            if verbose:
                print(f"  [{i}/{len(tickers)}] {sym}: {len(fh)} ставок, "
                      f"{len(perp)} свечей, интервал {out[-1].interval_h}ч")
            time.sleep(0.12)
        except Exception as e:  # noqa: BLE001 — один символ не должен ронять всё
            if verbose:
                print(f"  [{i}/{len(tickers)}] {sym}: пропущен ({e})")
    try:
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump([d.__dict__ if hasattr(d, "__dict__") else
                       {s: getattr(d, s) for s in SymData.__slots__}
                       for d in out], f)
    except OSError:
        pass
    return out


# ------------------------------------------------------------------ вердикт
def window_stats(funding: list[tuple[int, float]], ts: int,
                 interval_h: float, days: float) -> dict | None:
    """Статистика ставок за окно, известная на момент ts."""
    cutoff = ts - days * DAYS_MS
    rates = [r for t, r in funding if cutoff <= t <= ts]
    if not rates:
        return None
    apys = [apy_from_rate(r, interval_h) for r in rates]
    return {
        "avg_apy": statistics.mean(apys),
        "pos_share": 100.0 * sum(1 for r in rates if r > 0) / len(rates),
        "n": len(rates),
        "sum_pct": sum(rates) * 100.0,
    }


def make_row(sd: SymData, ts: int, spread_pct: float) -> dict | None:
    """Собирает строку r для enrich() ровно из данных на момент ts."""
    perp = sd.perp_at(ts)
    spot = sd.spot_at(ts)
    if not perp or not spot or perp <= 0 or spot <= 0:
        return None
    hist = sd.funding_upto(ts)
    if not hist:
        return None
    rate = hist[-1][1]
    row = {
        "symbol": sd.symbol,
        "price": perp,
        "perp_bid": perp * (1 - spread_pct / 200),
        "perp_ask": perp * (1 + spread_pct / 200),
        "funding_rate": rate,
        "interval_h": sd.interval_h,
        "next_funding_ms": hist[-1][0] + int(sd.interval_h * HOUR_MS),
        "turnover24h": sd.turnover,
        "oi_value": sd.oi,
        "cur_apy": apy_from_rate(rate, sd.interval_h),
    }
    w30 = window_stats(hist, ts, sd.interval_h, 30)
    w7 = window_stats(hist, ts, sd.interval_h, 7)
    if w30:
        row["w30"] = w30
    if w7:
        row["w7"] = w7
    return row


def spot_quotes_from(sd: SymData, ts: int, spread_pct: float) -> dict:
    """Снимок спот-стакана на шаге (для enrich: базис и спред спот-ноги)."""
    spot = sd.spot_at(ts)
    if not spot:
        return {}
    half = spread_pct / 200
    return {sd.spot_symbol: (spot * (1 - half), spot * (1 + half))}


# ------------------------------------------------------------------ симуляция
class Config:
    """Параметры симуляции (зеркалят настройки живого бота)."""

    def __init__(self, **kw):
        self.capital = 1000.0
        self.leverage = 2.0
        self.hold_payouts = 2
        self.exit_neg = 3
        self.max_margin_risk = 70.0
        self.max_hold_h = 24 * 21      # страховка от «вечной» позиции
        self.spread_pct = 0.05          # спред+проскальзывание, % (пессимизм)
        self.fee_in_pct = 0.155         # спот+перп taker за сторону, %
        for k, v in kw.items():
            setattr(self, k, v)


def simulate(datasets: list[SymData], cfg: Config, thresholds,
             verbose: bool = False) -> dict:
    """Прогоняет стратегию по общему таймлайну.

    Одновременная позиция — одна, как у живого бота (state хранит одну).
    Каждый шаг = один интервал выплат самого часто встречающегося периода.
    thresholds — dict порогов (как в PRESETS); Namespace для enrich()
    собирается здесь, поэтому sweep может свободно подменять min_apy30."""
    import argparse as _ap
    args_ns = _ap.Namespace(
        spot_fee=0.0010, perp_taker_fee=0.00055, perp_maker_fee=0.00020,
        preset=thresholds.get("preset", "hold"), **{
            k: v for k, v in thresholds.items() if k != "preset"})
    thresholds = args_ns
    if not datasets:
        return {"trades": [], "skipped": 0}

    # общий таймлайн: моменты выплат по всем символам
    marks: set[int] = set()
    for sd in datasets:
        marks.update(t for t, _ in sd.funding)
    # Стартуем там, где у КАЖДОГО символа хватает истории для окна w30
    # (30 интервалов) И есть цена. Индекс len-30 давал бы последние 30
    # ставок — после старта их оставалось ровно 30 шагов, то есть
    # симуляция шла 9 суток вместо всей доступной истории. Для полного
    # окна нужно оставить ЗАПАС: старт = len - (30 + запас шагов), где
    # запас ≈ 30% от len.
    start = 0
    for sd in datasets:
        n = len(sd.funding)
        if n < 20:
            continue
        need = int(n * 0.70)          # оставляем ~70% ставок на симуляцию
        need = max(need, n - 90)       # но не более 90 шагов на прогон
        if need < n:
            start = max(start, sd.funding[need][0])
        if sd.perp:
            start = max(start, sd.perp[0][0] + 24 * HOUR_MS)
    timeline = sorted(t for t in marks if t >= start)
    if not timeline:
        return {"trades": [], "skipped": 0}

    trades: list[dict] = []
    pos = None          # открытая позиция
    skipped = 0         # шагов, где данных не хватило
    peak = 0.0

    for ts in timeline:
        # ---- управление открытой позицией -------------------------------
        if pos is not None:
            sd = datasets[pos["idx"]]
            exit_reason, _exits = check_exit(pos, sd, ts, cfg)
            if exit_reason:
                # причину обязательно передаём: иначе сделка записывается
                # как «manual» и в отчёте не видно, почему вышли
                trades.append(close_position(pos, sd, ts, cfg,
                                            reason=exit_reason))
                pos = None
            elif ts - pos["opened_ms"] >= cfg.max_hold_h * HOUR_MS:
                trades.append(close_position(pos, sd, ts, cfg,
                                            reason="max_hold"))
                pos = None

        # ---- поиск входа -------------------------------------------------
        if pos is None:
            best = None
            for i, sd in enumerate(datasets):
                row = make_row(sd, ts, cfg.spread_pct)
                if row is None:
                    skipped += 1
                    continue
                row = enrich(row, spot_quotes_from(sd, ts, cfg.spread_pct),
                             thresholds)
                if not row.get("verdict_ok"):
                    continue
                # приоритет — консервативной доходности (как бот: сортирует
                # по conservative_apy)
                key = (row.get("w30") or {}).get("avg_apy", 0.0)
                if best is None or key > best[0]:
                    best = (key, i, sd, row)
            if best is not None:
                _, i, sd, row = best
                pos = open_position(sd, i, row, ts, cfg)
                if verbose:
                    print(f"  ВХОД {sd.symbol} @ {ts} "
                          f"APY30 {(row.get('w30') or {}).get('avg_apy', 0):.1f}%")

    # закрыть остаток по последней доступной цене — иначе PnL «теряется»
    if pos is not None:
        sd = datasets[pos["idx"]]
        last_ts = max(t for t, _ in sd.funding if t <= timeline[-1])
        trades.append(close_position(pos, sd, last_ts, cfg, reason="end"))

    return {"trades": trades, "skipped": skipped,
            "timeline": len(timeline)}


def open_position(sd: SymData, idx: int, row: dict, ts: int,
                  cfg: Config) -> dict:
    """Вход по ХУДШЕЙ цене: покупаем спот по ask, шортим перп по ask."""
    notional = cfg.capital / (1 + 1 / cfg.leverage)
    margin = notional / cfg.leverage
    perp_px = row["perp_ask"]
    spot_px = (row.get("perp_ask") or 0)   # заполнится из spot_quotes
    sp = spot_quotes_from(sd, ts, cfg.spread_pct).get(sd.spot_symbol)
    if sp:
        spot_px = sp[1]                     # ask спота — покупаем дороже
    perp_qty = notional / perp_px
    spot_qty = notional / spot_px
    fees = notional * cfg.fee_in_pct / 100
    # last_paid_ts = момент ВХОДА: иначе первый же check_exit обработал бы
    # всю историю ставок с самого начала (30 выплат разом) и hold-to-N
    # срабатывал бы мгновенно, начислив funding за всё прошлое
    return {
        "idx": idx, "symbol": sd.symbol,
        "opened_ms": ts, "notional": notional, "margin": margin,
        "perp_entry": perp_px, "spot_entry": spot_px,
        "perp_qty": perp_qty, "spot_qty": spot_qty,
        "fees_in": fees, "payouts": 0, "funding": 0.0,
        "last_paid_ts": ts, "neg_streak": 0,
        "apy30": (row.get("w30") or {}).get("avg_apy"),
        "capital": cfg.capital,
    }


def check_exit(pos: dict, sd: SymData, ts: int,
               cfg: Config) -> tuple[str, dict]:
    """Правила выхода — те же, что у бота (funding-ориентированные)."""
    # выплаты, произошедшие с прошлого шага
    for t, rate in sd.funding:
        if pos["last_paid_ts"] is not None and t <= pos["last_paid_ts"]:
            continue
        if t > ts:
            break
        pos["funding"] += rate * pos["notional"]
        if rate > 0:
            pos["payouts"] += 1
        # neg_streak — по последним закрытым интервалам
        pos["neg_streak"] = (pos["neg_streak"] + 1 if rate < 0 else 0)
        pos["last_paid_ts"] = t

    if cfg.hold_payouts > 0 and pos["payouts"] >= cfg.hold_payouts:
        return "hold_payouts", pos
    if cfg.exit_neg > 0 and pos["neg_streak"] >= cfg.exit_neg:
        return "neg_streak", pos
    # риск маржи: цена перпа уехала против шорта
    perp_now = sd.perp_at(ts)
    if perp_now and pos["margin"] > 0:
        loss = (perp_now - pos["perp_entry"]) * pos["perp_qty"]
        if loss / pos["margin"] * 100 >= cfg.max_margin_risk:
            return "margin_risk", pos
    return "", pos


def close_position(pos: dict, sd: SymData, ts: int, cfg: Config,
                   reason: str = "manual") -> dict:
    """Выход по худшей цене.

    Симметрия входа и выхода обязательна: вход — перп по ask и спот по ask,
    выход — обратно по bid. Если выход брать по «свече» (close), спред на
    перп-ноге терялся, и PnL на плоском рынке выходил не −(круг комиссий +
    спред), а меньше — то есть бэктест показывал прибыль там, где её нет."""
    mid_perp = sd.perp_at(ts) or pos["perp_entry"]
    sp = spot_quotes_from(sd, ts, cfg.spread_pct).get(sd.spot_symbol)
    if sp:
        spot_now = sp[0]                      # bid спота — продаём дешевле
        perp_now = mid_perp * (1 - cfg.spread_pct / 200)   # закрываем шорт по bid
    else:
        spot_now = pos["spot_entry"]
        perp_now = mid_perp * (1 - cfg.spread_pct / 200)

    # спот-нога: цена шла в паре с перпом, дельта ≈ 0; берём разницу
    price_pnl = (spot_now - pos["spot_entry"]) * pos["spot_qty"] \
        + (pos["perp_entry"] - perp_now) * pos["perp_qty"]
    exit_notional = spot_now * pos["spot_qty"] + perp_now * pos["perp_qty"]
    fees_out = exit_notional * cfg.fee_in_pct / 100
    pnl = price_pnl - pos["fees_in"] - fees_out
    total = pnl + pos["funding"]
    held_h = (ts - pos["opened_ms"]) / HOUR_MS
    return {
        "action": "close", "ts": ts, "symbol": pos["symbol"],
        "mode": "backtest", "held_hours": round(held_h, 1),
        "pnl_excl_funding": round(pnl, 4),
        "funding": round(pos["funding"], 4),
        "total": round(total, 4),
        "return_pct": round(total / pos["capital"] * 100, 3),
        "notional": round(pos["notional"], 2),
        "capital": round(pos["capital"], 2),
        "fees_in": round(pos["fees_in"], 4),
        "fees_out": round(fees_out, 4),
        "fees_total": round(pos["fees_in"] + fees_out, 4),
        "price_pnl": round(price_pnl, 4),
        "apy30": pos["apy30"],
        "exit_reason": reason,
        "payouts": pos["payouts"],
    }


# ------------------------------------------------------------------ метрики
def report(trades: list[dict], cfg: Config, thresholds: dict,
           n_syms: int, timeline_len: int, skipped: int) -> dict:
    import dashboard_metrics as M
    core = M.core_metrics(trades)
    econ = M.economics(trades)
    equity = M.equity_curve(trades)
    dd = M.drawdown_series(equity)
    mdd, _ = M.max_drawdown(equity)
    if core.get("n"):
        per_year = core["net"] / max(core["span_days"] / 365.0, 1e-9)
    else:
        per_year = 0.0
    return {
        "thresholds": thresholds,
        "config": {k: getattr(cfg, k) for k in
                   ("capital", "leverage", "hold_payouts", "exit_neg",
                    "max_margin_risk", "spread_pct", "fee_in_pct")},
        "universe": n_syms,
        "steps": timeline_len,
        "skipped_steps": skipped,
        "core": core,
        "economics": econ,
        "max_dd": mdd,
        "per_year_pct_of_capital": round(per_year / cfg.capital * 100, 2),
        "trades": trades,
    }


def print_report(rep: dict) -> None:
    c, e = rep["core"], rep["economics"]
    t = rep["thresholds"]
    print()
    print("=" * 74)
    print("  БЭКТЕСТ ДЕЛЬТА-НЕЙТРАЛЬНОЙ СТРАТЕГИИ")
    print("=" * 74)
    print(f"  Пороги: min_apy30={t['min_apy30']}%  min_pos={t.get('min_pos', '—')}%  "
          f"min_basis={t.get('min_basis', 0)}%  max_breakeven={t.get('max_breakeven', '—')}д")
    cf = rep["config"]
    print(f"  Капитал ${cf['capital']:.0f} · плечо {cf['leverage']}x · "
          f"hold-to-N={cf['hold_payouts']} выплат · спред {cf['spread_pct']}%")
    print(f"  Всего символов: {rep['universe']} · шагов: {rep['steps']} "
          f"(пропущено {rep['skipped_steps']})")
    print("-" * 74)
    if c.get("n", 0) == 0:
        print("  СДЕЛОК НЕТ — пороги не проходились ни разу за окно.")
        print("  Возможные причины: min_apy30 слишком высок, pass_oi/market")
        print("  фильтры отсекают всё, или в окне не было ставок.")
        print("=" * 74)
        return
    print(f"  Сделок: {c['n']}   Итог: {'+' if c['net'] >= 0 else ''}"
          f"{c['net']:.2f}$   Винрейт: {c['win_rate'] * 100:.0f}%")
    print(f"  Profit factor: {c['profit_factor']:.2f}   "
          f"Ожидание/сделка: {c['expectancy']:+.3f}$   "
          f"Payoff: {c['payoff']:.2f}x")
    print(f"  Макс. просадка: {rep['max_dd']:.2f}$   "
          f"Recovery: {c['recovery']:.2f}x   "
          f"В год (на капитал): {rep['per_year_pct_of_capital']:+.1f}%")
    print(f"  Funding: {e['funding']:+.2f}$   Комиссии: {e['fees']:.2f}$   "
          f"Покрытие фандингом: {e['coverage']:.2f}x")
    print(f"  Средняя сделка держалась {c['avg_hold_h']:.0f}ч, "
          f"в рынке {c['exposure'] * 100:.0f}% времени")
    print("-" * 74)
    print("  РАСПРЕДЕЛЕНИЕ ПО ПРИЧИНАМ ВЫХОДА:")
    reasons = {}
    for tr in rep["trades"]:
        reasons[tr.get("exit_reason", "?")] = reasons.get(
            tr.get("exit_reason", "?"), 0) + 1
    for k, v in sorted(reasons.items(), key=lambda kv: -kv[1]):
        print(f"    {k:<16} {v} сделок")
    if len(rep["trades"]) <= 20:
        print("-" * 74)
        print("  СДЕЛКИ:")
        for tr in rep["trades"]:
            print(f"    {tr['symbol']:<12} {tr['held_hours']:>6.1f}ч  "
                  f"фандинг {tr['funding']:+.2f}$  итого {tr['total']:+.2f}$  "
                  f"({tr['exit_reason']})")
    print("=" * 74)
    if c["net"] < 0:
        print("  ВЕРДИКТ: стратегия в этом окне УБЫТОЧНА.")
        if e["coverage"] < 1:
            print("  Причина видна прямо: фандинг НЕ отбивает комиссии круга.")
            print("  Лечится либо повышением ставки (min_apy30 вниз НЕ поможет —")
            print("  нужно ждать окна с apy30 > breakeven), либо снижением издержек.")
    elif c["profit_factor"] < 1.3:
        print("  ВЕРДИКТ: прибыль есть, но запас тонкий — на реальных")
        print("  проскальзываниях и комиссиях может уйти в ноль.")
    else:
        print("  ВЕРДИКТ: окно рабочее. Помните: бэктест пессимистичен")
        print("  (спред + вход по худшей цене), реальный результат может быть")
        print("  лучше — но не настолько, чтобы это были «бесплатные деньги».")


def sweep2(datasets: list[SymData], cfg: Config, base, verbose: bool = True):
    """Связка min_apy30 × max_breakeven.

    Эти два порога не независимы: окупаемость = entry_cost / (APY/365), то
    есть min_apy30=3% физически несовместим с max_breakeven=14 дней
    (нужно ≥ 40). Перебор по одному порогу давал нули и вводил в
    заблуждение — показываем сетку, где видно рабочую область."""
    grid = []
    for apy in (5.0, 8.0, 10.0, 12.0, 15.0, 20.0):
        for be in (14.0, 30.0, 60.0, 120.0):
            thr = dict(base)
            thr["min_apy30"] = apy
            thr["max_breakeven"] = be
            res = simulate(datasets, cfg, thr)
            import dashboard_metrics as M
            core = M.core_metrics(res["trades"])
            e = M.economics(res["trades"])
            grid.append({"min_apy30": apy, "max_breakeven": be,
                         "n": core.get("n", 0),
                         "net": round(core.get("net", 0.0), 2),
                         "mdd": round(M.max_drawdown(
                             M.equity_curve(res["trades"]))[0], 2),
                         "pf": round(core.get("profit_factor", 0), 2),
                         "cov": round(e["coverage"], 2)})
    if verbose:
        print(f"\n  {'APY':>6} {'ОКУП':>6} {'Сделок':>7} {'Итог$':>9} "
              f"{'Просадка$':>10} {'PF':>6} {'Покрыт':>7}")
        print("  " + "-" * 56)
        for r in grid:
            print(f"  {r['min_apy30']:>6.0f} {r['max_breakeven']:>6.0f} "
                  f"{r['n']:>7} {r['net']:>+9.2f} {r['mdd']:>10.2f} "
                  f"{r['pf']:>6.2f} {r['cov']:>7.2f}")
    return grid


def sweep(datasets: list[SymData], cfg: Config, base,
          values: list[float], verbose: bool = True) -> list[dict]:
    """Перебор min_apy30: какой порог даёт лучший результат."""
    rows = []
    for v in values:
        thr = dict(base)
        thr["min_apy30"] = v
        res = simulate(datasets, cfg, thr)
        c = res["trades"]
        import dashboard_metrics as M
        core = M.core_metrics(c)
        e = M.economics(c)
        rows.append({"min_apy30": v, "n": core.get("n", 0),
                     "net": round(core.get("net", 0.0), 2),
                     "mdd": round(M.max_drawdown(M.equity_curve(c))[0], 2),
                     "wr": round(core.get("win_rate", 0) * 100, 0),
                     "pf": round(core.get("profit_factor", 0), 2),
                     "cov": round(e["coverage"], 2)})
        if verbose:
            print(f"  min_apy30={v:>5.1f}%  сделок={rows[-1]['n']:>3}  "
                  f"итог={rows[-1]['net']:>+8.2f}$  просадка={rows[-1]['mdd']:>6.2f}$  "
                  f"win={rows[-1]['wr']:.0f}%  PF={rows[-1]['pf']:.2f}")
    return rows


# ------------------------------------------------------------------ main
def main() -> int:
    p = argparse.ArgumentParser(description="Бэктест дельта-нейтральной стратегии")
    p.add_argument("--symbols", default=None,
                   help="конкретные символы через запятую (иначе — топ по OI)")
    p.add_argument("--top", type=int, default=20,
                   help="сколько символов взять (default 20)")
    p.add_argument("--min-oi", type=float, default=5.0,
                   help="мин. OI для отбора символов, $M (default 5)")
    p.add_argument("--preset", choices=list(PRESETS), default="hold")
    p.add_argument("--capital", type=float, default=1000.0)
    p.add_argument("--leverage", type=float, default=2.0)
    p.add_argument("--hold-payouts", type=int, default=2,
                   help="авто-выход после N выплат (0 — не выходить)")
    p.add_argument("--exit-neg", type=int, default=3,
                   help="выход после N отрицательных ставок подряд")
    p.add_argument("--max-margin-risk", type=float, default=70.0,
                   help="выход при убытке, съевшем N%% маржи")
    p.add_argument("--spread-pct", type=float, default=0.05,
                   help="спред+проскальзывание, %% (пессимизм; default 0.05)")
    p.add_argument("--min-apy30", type=float, default=None,
                   help="переопределить порог APY пресета")
    p.add_argument("--min-pos", type=float, default=None)
    p.add_argument("--min-basis", type=float, default=None)
    p.add_argument("--allow-negative-basis", action="store_true",
                   help="разрешить вход при отрицательном базисе (перп дешевле "
                        "спота). По умолчанию бот требует базис ≥ 0 — на "
                        "мейджорах Bybit это часто около −0.05%%, то есть вход "
                        "закрыт. Флаг нужен, чтобы измерить, стоит ли "
                        "ослабить правило")
    p.add_argument("--max-breakeven", type=float, default=None)
    p.add_argument("--pass-oi", type=float, default=None)
    p.add_argument("--sweep", action="store_true",
                   help="перебор min_apy30: какой порог лучший")
    p.add_argument("--json", default=None, help="сохранить отчёт в файл")
    args = p.parse_args()

    thr = dict(PRESETS[args.preset])
    for k in ("min_apy30", "min_pos", "min_basis", "max_breakeven", "pass_oi"):
        v = getattr(args, k)
        if v is not None:
            thr[k] = v
    if args.allow_negative_basis:
        thr["min_basis"] = -999.0
    # Namespace с комиссиями и названием пресета для enrich() собирается
    # внутри simulate(): здесь thr остаётся dict, чтобы его можно было
    # отдавать в отчёт и свободно подменять в sweep.

    cfg = Config(capital=args.capital, leverage=args.leverage,
                 hold_payouts=args.hold_payouts, exit_neg=args.exit_neg,
                 max_margin_risk=args.max_margin_risk, spread_pct=args.spread_pct)

    print("Собираю историю (публичные API Bybit, ключи не нужны)...")
    if args.symbols:
        datasets = []
        want = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
        spot = fetch_spot_quotes()
        # OI/оборот нужны вердикту (pass_oi, min_volume). Брать 0 нельзя:
        # тогда pass_oi отсекает всё и бэктест молчит впустую.
        try:
            live_t = {t["symbol"]: t for t in fetch_linear_tickers()}
        except Exception as e:  # noqa: BLE001
            print(f"  тикеры не получились ({e}) — вердикт по OI будет строгим")
            live_t = {}
        for sym in want:
            try:
                fh = fetch_funding(sym, 200)
                skey, _ = spot_match(sym, spot)
                if not skey or len(fh) < 10:
                    continue
                t = live_t.get(sym, {})
                datasets.append(SymData(sym, skey, infer_interval(fh),
                                        fh,
                                        fetch_price_history(sym, "linear"),
                                        fetch_price_history(skey, "spot"),
                                        float(t.get("oi_value") or 0),
                                        float(t.get("turnover24h") or 0)))
                print(f"  {sym}: {len(fh)} ставок, OI "
                      f"{float(t.get('oi_value') or 0) / 1e6:.0f}M")
            except Exception as e:  # noqa: BLE001
                print(f"  {sym}: пропущен ({e})")
    else:
        datasets = load_dataset(args.top, args.min_oi)
    if not datasets:
        print("Нет данных для бэктеста (нет истории funding или сети).")
        return 1

    if args.sweep:
        base = dict(thr)
        # По умолчанию снимаем min_pos в sweep, если он не задан явно:
        # пресет hold требует 80% плюсовых интервалов — это почти не
        # проходится, и весь sweep давал нули. Пользователь может задать
        # --min-pos, чтобы проверить и строгий вариант.
        if args.min_pos is None:
            base["min_pos"] = 50.0
        print(f"\nПеребор порогов (min_pos={base['min_pos']}%, "
              f"pass_oi={base.get('pass_oi')}M, "
              f"min_basis={'любой' if base['min_basis'] < 0 else base['min_basis']}%):")
        grid = sweep2(datasets, cfg, base)
        live = [r for r in grid if r["n"] > 0]
        best = max(live, key=lambda r: r["net"], default=None)
        if best:
            print(f"\nЛучшая связка по ИТОГУ: apy≥{best['min_apy30']:.0f}% + "
                  f"окуп≤{best['max_breakeven']:.0f}д → "
                  f"{best['net']:+.2f}$ на {best['n']} сделках, "
                  f"просадка {best['mdd']:.2f}$")
            if best["cov"] < 1:
                print(f"  ⚠ покрытие фандингом {best['cov']:.2f}× < 1: ставка НЕ")
                print("    отбивает комиссии круга — прибыль может быть случайной.")
            if best["n"] < 10:
                print(f"  ⚠ всего {best['n']} сделок — вывод статистически слабый.")
        else:
            print("\nНи одна связка не дала сделок. Проверьте --min-pos,")
            print("--allow-negative-basis и окно данных (сейчас ставки низкие).")
        print("\nВнимание: лучший порог по ИТОГУ не значит лучший по риску —")
        print("смотрите на просадку, число сделок и покрытие фандиндом.")
        return 0

    print(f"\nСимулирую по {len(datasets)} символам...")
    res = simulate(datasets, cfg, thr, verbose=True)
    rep = report(res["trades"], cfg, thr, len(datasets),
                 res.get("timeline", 0), res.get("skipped", 0))
    print_report(rep)

    if args.json:
        try:
            with open(args.json, "w", encoding="utf-8") as f:
                json.dump(rep, f, ensure_ascii=False, indent=2)
            print(f"\nОтчёт сохранён: {args.json}")
        except OSError as e:
            print(f"Не удалось сохранить: {e}")

    print("\nОГРАНИЧЕНИЯ БЭКТЕСТА (почему результат пессимистичен):")
    print("  · вход по ask, выход по bid + спред — реальный maker проходит лучше")
    print("  · глубина стакана и частичные исполнения не моделируются")
    print("  · комиссии non-VIP; с VIP-ставкой результат был бы выше")
    print("  · мультипликаторы (1000XXX) исключены из выборки")
    print("  · окно ограничено 200 точками истории funding (≈66 суток на 8ч)")
    return 0


if __name__ == "__main__":
    sys.exit(main())