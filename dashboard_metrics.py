"""Метрики торговли для панели — чистый расчёт по данным position.json.

Отделён от dashboard.py намеренно: формулы здесь — то, что реально решает,
«зарабатывает бот или теряет», а разметка не должна их ломать. Всё в stdlib,
все функции принимают простые списки словарей (записи history) и возвращают
числа — их можно тестировать без сети и без рендера.

Ключевая идея для дельта-нейтрального арбитража: PnL раскладывается на
    цена + funding − комиссии,
и «цена» должна быть около нуля (иначе позиция не нейтральна). Именно
разбивка показывает, что именно съедает результат — обычно не ставка, а
комиссии круга и проскальзывание.
"""
from __future__ import annotations

import statistics
from datetime import datetime, timezone

# Порог «шумной» сделки — совпадает с памятью бота (MIN_HOLD_H)
NOISE_H = 0.5


def _f(x, default=0.0) -> float:
    try:
        if x is None or x == "":
            return default
        return float(x)
    except (TypeError, ValueError):
        return default


# ------------------------------------------------------------------ equity
def equity_curve(trades: list[dict]) -> list[float]:
    """Кумулятивный PnL после каждой закрытой сделки (начиная с 0)."""
    out, s = [0.0], 0.0
    for t in trades:
        s += _f(t.get("total"))
        out.append(s)
    return out


def drawdown_series(equity: list[float]) -> list[float]:
    """Просадка от локального максимума в $ (0 или положительное число)."""
    peak, out = equity[0] if equity else 0.0, []
    for v in equity:
        peak = max(peak, v)
        out.append(max(0.0, peak - v))
    return out


def max_drawdown(equity: list[float]) -> tuple[float, int]:
    """(максимальная просадка в $, индекс её точки в кривой)."""
    if not equity:
        return 0.0, 0
    peak, worst, at = equity[0], 0.0, 0
    for i, v in enumerate(equity):
        peak = max(peak, v)
        dd = peak - v
        if dd > worst:
            worst, at = dd, i
    return worst, at


# ------------------------------------------------------------------ core
def core_metrics(trades: list[dict]) -> dict:
    """Основные показатели торговли.

    Не использует Sharpe: у арбитража с редкими крупными сделками
    годовой Sharpe крайне неустойчив и обманчив. Profit factor, expectancy,
    payoff и recovery — устойчивые и честные метрики."""
    n = len(trades)
    if not n:
        return {"n": 0}
    totals = [_f(t.get("total")) for t in trades]
    wins = [x for x in totals if x > 0]
    losses = [x for x in totals if x < 0]
    gross_win = sum(wins)
    gross_loss = -sum(losses)          # положительное число

    equity = equity_curve(trades)
    mdd, mdd_i = max_drawdown(equity)
    net = sum(totals)

    holds = [_f(t.get("held_hours")) for t in trades]
    hold_total = sum(holds)
    span_days = _span_days(trades)
    # Время между первой и последней закрытой сделкой, а не от первой
    # до последней + удержание: сумма холдов может превышать span (сделки
    # шли внахлёст по разным символам), и exposure вылезал за 100%.
    span = _span_hours(trades)

    return {
        "n": n,
        "net": net,
        "wins": len(wins),
        "losses": len(losses),
        "zero": n - len(wins) - len(losses),
        "win_rate": len(wins) / n,
        "gross_win": gross_win,
        "gross_loss": gross_loss,
        # профит-фактор: сколько принесли победы на единицу потерь
        "profit_factor": (gross_win / gross_loss) if gross_loss > 1e-9
                         else (float("inf") if gross_win > 0 else 0.0),
        # ожидание с с��лки: сколько в среднем приносит одна сделка, $
        "expectancy": net / n,
        # payoff: средняя победа / средний убыток (во сколько раз).
        # avg_loss хранится ПОЛОЖИТЕЛЬНЫМ (размер убытка), иначе payoff
        # вышел бы отрицательным, хотя обе величины по модулю положительны
        "payoff": (gross_win / len(wins)) / (gross_loss / len(losses))
                  if wins and losses and gross_loss > 0 else 0.0,
        "avg_win": gross_win / len(wins) if wins else 0.0,
        "avg_loss": gross_loss / len(losses) if losses else 0.0,
        "worst": min(totals),
        "best": max(totals),
        "max_dd": mdd,
        "max_dd_at": mdd_i,
        # recovery factor: сколько заработано на единицу просадки.
        # При net <= 0 показываем 0, а не отрицательное: «не восстановились»
        # и «заработали в минус на просадку» — разные вещи, а для решения
        # «менять ли стратегию» безразлично, что хуже
        "recovery": (net / mdd) if (mdd > 1e-9 and net > 0) else 0.0,
        "avg_hold_h": hold_total / n,
        "total_hold_h": hold_total,
        "span_days": span_days,
        # доля времени в рынке: сколько процентов периода прошло в позиции.
        # Считаем от span_hours (между первой и последней сделкой) и
        # ограничиваем единицей — иначе при сделках внахлёст получалось >100%
        "exposure": min(1.0, (hold_total / span)) if span > 0 else 0.0,
    }


def _span_hours(trades: list[dict]) -> float:
    """Часы между первой и последней закрытой сделкой."""
    ts = [_f(t.get("ts")) for t in trades if _f(t.get("ts")) > 0]
    if len(ts) < 2:
        # одна сделка — «период» равен её собственному удержанию
        return _f(trades[0].get("held_hours")) if trades else 0.0
    return max(0.0, (max(ts) - min(ts)) / 3_600_000)


def _span_days(trades: list[dict]) -> float:
    """Период в днях — от первой до последней закрытой сделки (мин. 1)."""
    ts = [_f(t.get("ts")) for t in trades if _f(t.get("ts")) > 0]
    if len(ts) < 2:
        return 0.0
    span = (max(ts) - min(ts)) / 86_400_000
    return max(span, 1.0)


# ------------------------------------------------------------------ economics
def economics(trades: list[dict]) -> dict:
    """Откуда пришли деньги: цена, funding, комиссии.

    Для арбитража это главная таблица. Ожидание:
      • price_pnl ≈ 0 (дельта-нейтральность; ненулевое — сигнал, что позиция
        была направленной или ноги разъезжались),
      • funding > 0 (наш доход),
      • fees_total > 0 (неизбежная плата за круг входа+выхода).
    У старых записей полей нет — берём пересчёт от типовой комиссии круга."""
    funding = price = fees = 0.0
    notional_sum = 0.0
    known = 0
    for t in trades:
        funding += _f(t.get("funding"))
        if t.get("price_pnl") is not None:
            price += _f(t.get("price_pnl"))
            fees += _f(t.get("fees_total"))
            known += 1
        else:
            # оценка для старых записей: круг taker ≈ 0.155% от нотиона
            notional_sum += _f(t.get("notional"), 600.0)
        notional_sum += 0 if t.get("price_pnl") is not None else 0
    if known < len(trades):
        fees += notional_sum * 0.0031   # две ноги × две стороны
    net = sum(_f(t.get("total")) for t in trades)
    return {
        "funding": funding,
        "price_pnl": price,
        "fees": fees,
        "net": net,
        # сколько сделок содержат честную разбивку
        "decomposed": known,
        "total": len(trades),
        # вклад каждой составляющей в итог, %
        "funding_share": (funding / net * 100) if abs(net) > 1e-9 else 0.0,
        "price_share": (price / net * 100) if abs(net) > 1e-9 else 0.0,
        # фандинг против комиссий: >1 — ставка отбивает круг
        "coverage": (funding / fees) if fees > 1e-9 else 0.0,
    }


def per_symbol(trades: list[dict]) -> list[dict]:
    """Разбивка по символам: сколько заработано и сколько раз брали."""
    agg: dict[str, dict] = {}
    for t in trades:
        sym = t.get("symbol") or "?"
        a = agg.setdefault(sym, {"symbol": sym, "n": 0, "net": 0.0,
                                 "funding": 0.0, "wins": 0,
                                 "hold_h": 0.0, "worst": 0.0, "best": 0.0})
        tot = _f(t.get("total"))
        a["n"] += 1
        a["net"] += tot
        a["funding"] += _f(t.get("funding"))
        a["hold_h"] += _f(t.get("held_hours"))
        a["wins"] += 1 if tot > 0 else 0
        a["worst"] = min(a["worst"], tot)
        a["best"] = max(a["best"], tot)
    rows = list(agg.values())
    for a in rows:
        a["win_rate"] = a["wins"] / a["n"] if a["n"] else 0.0
        a["avg_hold_h"] = a["hold_h"] / a["n"] if a["n"] else 0.0
        a["net"] = round(a["net"], 4)
        a["funding"] = round(a["funding"], 4)
    rows.sort(key=lambda r: -r["net"])
    return rows


def hold_buckets(trades: list[dict]) -> list[dict]:
    """PnL по времени удержания — видно, на чём зарабатываем: на длинных
    удержаниях (собралось много выплат) или на коротких."""
    buckets = [("< 6 ч", 0, 6), ("6–24 ч", 6, 24), ("1–3 дн", 24, 72),
               ("3–7 дн", 72, 168), ("> 7 дн", 168, 10 ** 9)]
    out = []
    for label, lo, hi in buckets:
        sel = [t for t in trades if lo <= _f(t.get("held_hours")) < hi]
        out.append({
            "label": label, "n": len(sel),
            "net": round(sum(_f(t.get("total")) for t in sel), 4),
            "funding": round(sum(_f(t.get("funding")) for t in sel), 4),
        })
    return out


# ------------------------------------------------------------------ position
def position_risk(pos: dict | None, perp_now: float | None) -> dict:
    """Риски открытой позиции: сколько до стопа, сколько маржи под риском,
    сколько осталось до ликвидации по буферу."""
    if not pos or perp_now is None or perp_now <= 0:
        return {}
    entry = _f(pos.get("perp_entry"))
    if entry <= 0:
        return {}
    qty = _f(pos.get("perp_qty"))
    margin = _f(pos.get("margin"))
    stop = _f(pos.get("stop_price"))
    out = {
        "symbol": pos.get("symbol"),
        "entry": entry,
        "now": perp_now,
        "move_pct": (perp_now / entry - 1) * 100,
        "unrealized": (entry - perp_now) * qty,      # шорт: падение = плюс
        "margin": margin,
        "margin_used_pct": ((perp_now - entry) * qty / margin * 100)
                           if margin > 0 else 0.0,
        "stop_armed": bool(pos.get("stop_armed")),
        "stop_price": stop or 0.0,
    }
    if stop > 0:
        out["to_stop_pct"] = (stop / perp_now - 1) * 100
        # сколько маржи останется, если стоп сработает
        out["loss_at_stop"] = max(0.0, (stop - entry) * qty)
    else:
        out["to_stop_pct"] = None
        out["loss_at_stop"] = None
    # буфер до типичной ликвидации при плече: ≈ 1/L (грубая оценка для
    # дельта-нейтрального шорта без учёта биржевых риск-тиров)
    lev = _f(pos.get("leverage"), 2.0)
    if lev > 0:
        out["liq_est_pct"] = (100.0 / lev - 100.0)
    return out


# ------------------------------------------------------------------ health
def bot_health(log_path: str, state: dict) -> dict:
    """Живой ли бот: когда последнее событие, есть ли тревожные строки.

    Различает «бот работает и ждёт окно» и «бот упал/завис» — без этого
    тишина панели читается двумя разными способами."""
    out = {"alive": None, "last_event_age_s": None, "warns": 0,
           "last_error": None, "last_hb": None}
    try:
        with open(log_path, encoding="utf-8", errors="replace") as f:
            lines = [ln.rstrip() for ln in f if ln.strip()]
    except OSError:
        return out
    if not lines:
        return out
    ts_now = datetime.now(timezone.utc).timestamp()
    for ln in reversed(lines[-300:]):
        ts_txt, _, msg = ln.partition("] ")
        ts_txt = ts_txt.lstrip("[")
        try:
            ts = datetime.strptime(ts_txt, "%Y-%m-%d %H:%M:%S").timestamp()
        except ValueError:
            continue
        if out["last_event_age_s"] is None:
            out["last_event_age_s"] = ts_now - ts
        if out["last_hb"] is None and ("HOLD" in msg or "окна нет" in msg
                                       or "fast:" in msg):
            out["last_hb"] = ts_now - ts
        low = msg.lower()
        if out["last_error"] is None and any(
                k in low for k in ("ошибка", "не удал", "внимание",
                                   "рассинхрон", "поврежд", "сверка")):
            out["last_error"] = msg[:160]
    for ln in lines[-200:]:
        low = ln.lower()
        if any(k in low for k in ("ошибка цикла", "расхождение",
                                  "поврежден", "часы сервера",
                                  "внимание", "не продан")):
            out["warns"] += 1
    out["alive"] = (out["last_event_age_s"] is not None
                    and out["last_event_age_s"] < 900)
    pos = state.get("position")
    out["has_position"] = bool(pos)
    return out