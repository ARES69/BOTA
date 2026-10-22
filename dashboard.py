#!/usr/bin/env python3
"""
Funding Terminal — панель торговли и метрик
===========================================
Читает position.json (история сделок + открытая позиция), memory.json
(память бота) и bot.log (события), рисует самодостаточный dashboard.html:
только HTML+CSS+SVG, никаких зависимостей и внешних CDN.

Считает через dashboard_metrics.py — отдельный модуль с чистыми формулами
(equity, просадка, profit factor, разбивка PnL), чтобы их можно было тестировать
независимо от вёрстки.

Команды:
    python dashboard.py                      # собрать dashboard.html
    python dashboard.py --no-live            # без запросов к API (оффлайн)
    python dashboard.py --refresh 60         # автообновление раз в 60 сек
    python dashboard.py --serve 8050         # сервер: http://127.0.0.1:8050
    python dashboard.py --out page.html      # свой файл вывода

В режиме --serve на панели есть кнопки управления: close / void /
health-check, запуск-остановка-перезапуск сервиса бота (systemctl) и
РУЧНОЙ ВХОД в позицию (funding_executor.py enter). Кнопки защищены токеном
(env DASHBOARD_TOKEN или --token; без токена — генерируется случайный).
Опасные действия (сервис и вход) работают только при заданном
DASHBOARD_PASSWORD: вход в панель + токен + подтверждение в форме
(символ вводится дважды, для mainnet — отдельная отметка о реальных деньгах).

Кнопка «СТОП-ВСЁ» останавливает сервис бота и закрывает ВСЕ пары, отчитываясь
по каждому шагу — и в браузере, и в Telegram (TELEGRAM_BOT_TOKEN /
TELEGRAM_CHAT_ID): закрытие портфеля может идти минутами, а «бот остановлен»
нужно знать сразу.

Доступ по IP:порту: `--host 0.0.0.0` открывает панель в сети, но ТОЛЬКО
вместе с паролем (env DASHBOARD_PASSWORD или --password) — иначе сервер
не стартует. Вход: /login, сессия в HttpOnly-cookie, выход: /logout.

Сервер пересобирает панель на каждый запрос — смотри всегда свежие данные.
"""

import argparse
import hmac
import http.server
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlencode, urlparse

# Консоль/лог на Windows по умолчанию cp1252 — печать кириллицы роняла сервер
# на старте (UnicodeEncodeError в print и процесс умирал). Для веб-панели это
# особенно опасно: она должна пережить любой вывод, поэтому чиним поток.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001 — старый Python/перенаправленный поток
        pass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dashboard_metrics as M  # noqa: E402
import trade_sanity as S  # noqa: E402  санитария: отсев невозможных сделок

BASE = os.path.dirname(os.path.abspath(__file__))
POSITION_FILE = os.path.join(BASE, "position.json")
SNAPSHOT_FILE = os.path.join(BASE, "snapshot.json")
MEMORY_FILE = os.path.join(BASE, "memory.json")
EVENTS_FILE = os.path.join(BASE, "bot.log")
LIVE = True          # запрашивать ли живой PnL открытой позиции (--no-live)


# ------------------------------------------------------------------ data
def load_json(path: str, default, expect: type | None = None):
    """expect — тип, который ожидается на месте (dict/list/NoneType).
    default=None означает «может быть dict или ничего», поэтому тип берётся
    из expect: иначе type(None)=NoneType отбрасывал бы валидный snapshot."""
    if not os.path.exists(path):
        return default
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        want = expect or type(default)
        # валидный JSON, но не тот тип (список вместо dict и т.п.) — дальше
        # .get() ронял рендер с AttributeError; отдаём дефолт
        return data if isinstance(data, want) else default
    except (json.JSONDecodeError, OSError):
        return default


def closed_trades(state: dict) -> list[dict]:
    return [h for h in state.get("history", [])
            if isinstance(h, dict) and h.get("action") == "close"
            and h.get("total") is not None]


def live_prices(pos: dict | None) -> tuple[float | None, float | None]:
    """(perp_price, pnl) открытой позиции по живым тикерам.
    None, если позиции нет или сети недоступно."""
    if not pos or not LIVE:
        return None, None
    try:
        from funding_executor import current_pnl
        spot_now, perp_now, pnl = current_pnl(pos)
        return perp_now, pnl
    except Exception:  # noqa: BLE001 — панель не должна падать без сети
        return None, None


# ------------------------------------------------------------------ format
def money(x: float) -> str:
    x = float(x or 0)
    return f"{'+' if x >= 0 else '-'}${abs(x):,.2f}"


def pct(x: float, digits: int = 2) -> str:
    x = float(x or 0)
    return f"{'+' if x >= 0 else ''}{x:.{digits}f}%"


def ratio(x: float) -> str:
    x = float(x or 0)
    if x == float("inf"):
        return "∞"
    return f"{x:.2f}"


def hold_str(h: float | None) -> str:
    if h is None:
        return "—"
    h = float(h)
    if h < 1:
        return f"{h * 60:.0f} мин"
    if h < 48:
        return f"{h:.1f} ч"
    return f"{h / 24:.1f} дн"


def fmt_ts(ms) -> str:
    if not ms:
        return "—"
    try:
        return datetime.fromtimestamp(float(ms) / 1000,
                                      timezone.utc).astimezone().strftime(
            "%d.%m %H:%M")
    except (ValueError, OSError, OverflowError):
        return "—"


def age_str(sec: float | None) -> str:
    if sec is None:
        return "—"
    sec = float(sec)
    if sec < 60:
        return f"{sec:.0f} сек назад"
    if sec < 3600:
        return f"{sec / 60:.0f} мин назад"
    if sec < 86400:
        return f"{sec / 3600:.1f} ч назад"
    return f"{sec / 86400:.1f} дн назад"


def html_escape(s: str) -> str:
    """Экранирование для атрибутов и текста (кавычки тоже — иначе токен
    в value='...' можно закрыть значением из данных)."""
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;").replace("'", "&#39;"))


def cls(v: float) -> str:
    return "up" if float(v or 0) >= 0 else "down"


# ------------------------------------------------------------------ charts
def _scales(lo: float, hi: float, pad_frac: float = 0.15):
    if abs(hi - lo) < 1e-9:
        hi, lo = lo + 1, lo - 1
    pad = (hi - lo) * pad_frac
    return lo - pad, hi + pad


def svg_equity(equity: list[float], dd: list[float]) -> str:
    """Кривая капитала с заливкой просадки снизу — видно, где был заработок,
    а где просадка съедала его (обычно это и есть реальный риск)."""
    if len(equity) < 2:
        return ('<div class="empty">Кривая появится после первой закрытой '
                'сделки — график строится по факту, а не по прогнозу.</div>')

    w, h = 900, 300
    pl, pr, pt, pb = 58, 58, 18, 32
    lo, hi = _scales(min(equity), max(equity))
    lo = min(lo, 0.0)
    dmax = max(dd) if dd else 0.0
    d_hi = dmax * 1.2 if dmax > 1e-9 else 1.0

    iw = w - pl - pr
    ih = h - pt - pb

    def X(i):
        return pl + iw * i / (len(equity) - 1)

    def Y(v):
        return pt + ih * (hi - v) / (hi - lo)

    # ---- просадка отдельной полосой снизу (своя шкала, всегда 0 вверху)
    dh = 58
    dy_top = h - pb + 6

    def DY(v):
        return dy_top + dh * (v / d_hi)

    color = "#26a69a" if equity[-1] >= 0 else "#ef5350"
    pts = " ".join(f"{X(i):.1f},{Y(v):.1f}" for i, v in enumerate(equity))
    area = (f"M{X(0):.1f},{Y(equity[0]):.1f} "
            + " ".join(f"L{X(i):.1f},{Y(v):.1f}" for i, v in enumerate(equity))
            + f" L{X(len(equity) - 1):.1f},{Y(lo):.1f} L{X(0):.1f},{Y(lo):.1f} Z")

    dd_path = " ".join(
        f"{'M' if i == 0 else 'L'}{X(i):.1f},{DY(dd[i]):.1f}"
        for i in range(len(dd)))

    grid, labels = [], []
    for k in range(5):
        v = lo + (hi - lo) * k / 4
        y = Y(v)
        grid.append(f'<line x1="{pl}" y1="{y:.1f}" x2="{w - pr}" y2="{y:.1f}" '
                    f'class="grid"/>')
        labels.append(f'<text x="{pl - 8}" y="{y + 4:.1f}" class="axis" '
                      f'text-anchor="end">${v:,.0f}</text>')

    if lo <= 0 <= hi:
        zy = Y(0)
        grid.append(f'<line x1="{pl}" y1="{zy:.1f}" x2="{w - pr}" y2="{zy:.1f}" '
                    f'class="zero"/>')

    # маркер максимальной просадки
    worst_i = max(range(len(dd)), key=lambda i: dd[i]) if dd else 0
    marker = ""
    if dd and dd[worst_i] > 1e-9:
        marker = (f'<circle cx="{X(worst_i):.1f}" cy="{DY(dd[worst_i]):.1f}" '
                  f'r="4" class="dd-dot"><title>макс. просадка '
                  f'{money(dd[worst_i])} (сделка #{worst_i})</title></circle>')

    xl = []
    for i in range(len(equity)):
        if i == 0 or i == len(equity) - 1 or i % max(1, len(equity) // 8) == 0:
            xl.append(f'<text x="{X(i):.1f}" y="{h - 8}" class="axis" '
                      f'text-anchor="middle">{"старт" if i == 0 else "#" + str(i)}'
                      f'</text>')

    return f'''<svg viewBox="0 0 {w} {h}" class="chart" role="img"
     aria-label="Кривая капитала и просадка">
  <defs>
    <linearGradient id="eqfill" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stop-color="{color}" stop-opacity="0.30"/>
      <stop offset="100%" stop-color="{color}" stop-opacity="0.02"/>
    </linearGradient>
    <linearGradient id="ddfill" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stop-color="#ef5350" stop-opacity="0.55"/>
      <stop offset="100%" stop-color="#ef5350" stop-opacity="0.06"/>
    </linearGradient>
  </defs>
  {''.join(grid)}
  <path d="{area}" fill="url(#eqfill)"/>
  <polyline points="{pts}" fill="none" stroke="{color}" stroke-width="2.4"/>
  <path d="{dd_path}" fill="none" stroke="#ef5350" stroke-width="1.3"
        stroke-dasharray="4 3" opacity="0.85"/>
  <line x1="{pl}" y1="{dy_top + dh:.1f}" x2="{w - pr}" y2="{dy_top + dh:.1f}"
        stroke="#2a3245"/>
  <text x="{pl}" y="{dy_top - 3}" class="axis">просадка ↓ max {money(dmax)}</text>
  {marker}
  {''.join(labels)}{''.join(xl)}
</svg>'''


def svg_trade_bars(totals: list[float]) -> str:
    """PnL по каждой сделке: видно серии убытков (стоп-лист памяти) и
    разброс — одна огромная сделка портит среднее, это сразу заметно."""
    if not totals:
        return '<div class="empty">Нет закрытых сделок.</div>'
    w, h = 900, 160
    pl, pr, pt, pb = 58, 14, 14, 26
    lo, hi = _scales(min(min(totals), 0.0), max(max(totals), 0.0), 0.1)
    iw, ih = w - pl - pr, h - pt - pb
    n = len(totals)
    bw = max(3.0, min(34.0, iw / max(1, n) * 0.7))
    step = iw / max(1, n)

    def Y(v):
        return pt + ih * (hi - v) / (hi - lo)

    bars, zero_y = [], Y(min(0.0, hi))
    for i, v in enumerate(totals):
        x = pl + step * i + (step - bw) / 2
        y = Y(v)
        top, bot = min(y, zero_y), max(y, zero_y)
        c = "#26a69a" if v >= 0 else "#ef5350"
        bars.append(f'<rect x="{x:.1f}" y="{top:.1f}" width="{bw:.1f}" '
                    f'height="{max(1.5, bot - top):.1f}" fill="{c}" rx="1.5">'
                    f'<title>сделка #{i + 1}: {money(v)}</title></rect>')
    grid = [f'<line x1="{pl}" y1="{zero_y:.1f}" x2="{w - pr}" y2="{zero_y:.1f}" '
            f'class="zero"/>']
    return f'''<svg viewBox="0 0 {w} {h}" class="chart" role="img"
     aria-label="PnL по сделкам">
  {''.join(grid)}{''.join(bars)}
  <text x="{pl - 8}" y="{pt + 10:.1f}" class="axis" text-anchor="end">
    {money(hi)}</text>
  <text x="{pl - 8}" y="{zero_y + 4:.1f}" class="axis" text-anchor="end">$0</text>
  <text x="{pl - 8}" y="{pt + ih:.1f}" class="axis" text-anchor="end">
    {money(lo)}</text>
</svg>'''


def attribution_bar(econ: dict) -> str:
    """Откуда пришли деньги: цена / funding / комиссии. Главная диагностика
    арбитража — если funding не отбивает комиссии, стратегия не работает,
    как бы ни выглядели остальные цифры."""
    if econ.get("total", 0) == 0:
        return '<div class="empty">Нет данных о сделках.</div>'
    price = econ["price_pnl"]
    funding = econ["funding"]
    fees = econ["fees"]          # положительное = издержка, рисуем вниз
    net = econ["net"]
    total = max(abs(price), abs(funding), abs(fees), abs(net), 1e-9)

    w, barh = 900, 46
    segs = [
        ("Цена (нейтрально ≈0)", price, "#4aa3ff"),
        ("Funding (доход)", funding, "#26a69a"),
        ("Комиссии (издержки)", -fees, "#ef5350"),
    ]
    x = 0.0
    parts = []
    for label, v, color in segs:
        width = abs(v) / total * (w - 180)
        y = 0 if v >= 0 else barh / 2
        h = barh / 2 - 2 if v >= 0 else barh / 2 - 2
        parts.append(
            f'<rect x="{180 + x:.1f}" y="{y}" width="{max(0.5, width):.1f}" '
            f'height="{h}" fill="{color}" opacity="0.85" rx="2">'
            f'<title>{label}: {money(v)}</title></rect>')
        x += width

    cov = econ["coverage"]
    cov_txt = (f"{ratio(cov)}×" if cov and cov != float("inf") else "—")
    verdict = ""
    if econ["decomposed"]:
        if cov >= 1.5:
            verdict = "<span class='up'>ставка с запасом отбивает круг</span>"
        elif cov >= 1.0:
            verdict = "<span class='up'>ставка еле отбивает круг</span>"
        else:
            verdict = ("<span class='down'>ставка НЕ отбивает комиссии круга "
                       "— это и есть причина убытков</span>")
    return f'''<div class="attr">
  <div class="attr-row">
    <span class="attr-lab">Цена</span><span class="attr-val">{money(price)}</span>
  </div>
  <div class="attr-row">
    <span class="attr-lab">Funding</span>
    <span class="attr-val up">{money(funding)}</span></div>
  <div class="attr-row">
    <span class="attr-lab">Комиссии</span>
    <span class="attr-val down">{money(-fees)}</span></div>
  <div class="attr-row">
    <span class="attr-lab">Итого</span>
    <span class="attr-val {'up' if net >= 0 else 'down'}"><b>{money(net)}</b></span>
  </div>
</div>
<svg viewBox="0 0 {w} {barh}" class="chart attr-svg" role="img"
     aria-label="Вклад цены, фандинга и комиссий">
  <line x1="180" y1="{barh / 2}" x2="{w}" y2="{barh / 2}" class="zero"/>
  {''.join(parts)}
</svg>
<div class="sub">Покрытие фандингом: <b>{cov_txt}</b> · {verdict}</div>'''


# ------------------------------------------------------------------ blocks
def kpi_row(c: dict, econ: dict, pos: dict | None, live: float | None) -> str:
    """Ключевые числа. Пустое состояние не должно показывать нули как
    «результат» — тогда это вводит в заблуждение."""
    if c.get("n", 0) == 0 and not pos:
        return ('<div class="empty">Нет закрытых сделок и открытой позиции. '
                'Панель заполнится автоматически: <code>watch</code> ведёт '
                'торговлю сам, сделки появляются здесь после выходов.</div>')

    open_pnl = live if live is not None else 0.0
    cards = []

    def card(label, value, sub, klass=""):
        cards.append(f'<div class="card"><div class="label">{label}</div>'
                     f'<div class="value {klass}">{value}</div>'
                     f'<div class="sub">{sub}</div></div>')

    card("Итоговый PnL", money(c.get("net", 0)),
         f"по {c.get('n', 0)} сделкам · открыто {money(open_pnl)}",
         cls(c.get("net", 0)))
    card("Винрейт", f"{c.get('win_rate', 0) * 100:.0f}%",
         f"{c.get('wins', 0)} плюс / {c.get('losses', 0)} минус")
    card("Profit factor", ratio(c.get("profit_factor", 0)),
         "прибыль на единицу убытка · >1 = работает",
         "up" if c.get("profit_factor", 0) >= 1 else "down")
    card("Макс. просадка", money(c.get("max_dd", 0)),
         f"recovery {ratio(c.get('recovery', 0))}× · худшая {money(c.get('worst', 0))}",
         "down" if c.get("max_dd", 0) > 0 else "")
    card("Ожидание/сделка", money(c.get("expectancy", 0)),
         f"средняя {hold_str(c.get('avg_hold_h'))} · "
         f"payoff {ratio(c.get('payoff', 0))}×",
         cls(c.get("expectancy", 0)))
    card("Funding собран", money(econ.get("funding", 0)),
         f"комиссии {money(econ.get('fees', 0))} · "
         f"покрытие {ratio(econ.get('coverage', 0))}×",
         "up" if econ.get("coverage", 0) >= 1 else "down")
    card("В рынке", f"{c.get('exposure', 0) * 100:.0f}%",
         f"{hold_str(c.get('total_hold_h'))} суммарно из "
         f"{c.get('span_days', 0) * 24:.0f} ч")
    card("Позиция", money(open_pnl) if pos else "—",
         f"{pos['symbol']} · {hold_str((datetime.now(timezone.utc).timestamp() * 1000 - pos['opened_ms']) / 3_600_000)}"
         if pos else "бот ждёт окно",
         cls(open_pnl) if pos else "")
    return f'<div class="cards">{"".join(cards)}</div>'


def health_bar(h: dict) -> str:
    """Жив ли бот. Различает «работает и ждёт» и «завис/упал» — иначе
    тишина панели читается двумя разными способами."""
    if h.get("alive") is None:
        return ('<div class="warn-line neutral">бот ещё не запущен — '
                '<code>watch</code> пишет события в bot.log</div>')
    if h["alive"]:
        tone, icon = "ok", "🟢"
        txt = f"бот работает · последнее событие {age_str(h.get('last_event_age_s'))}"
        if h.get("has_position"):
            txt += " · позиция открыта"
        else:
            txt += " · ждёт окно"
        if h.get("warns"):
            txt += f" · тревожных строк в логе: {h['warns']}"
        return f'<div class="warn-line ok">{icon} {txt}</div>'
    return (f'<div class="warn-line bad">🔴 бот молчит {age_str(h.get("last_event_age_s"))} '
            f'— вероятно, упал или завис. Проверьте: '
            f'<code>systemctl status funding-watch</code></div>')


def sanity_bar(susp: list[dict]) -> str:
    """Плашка «столько-то сделок исключено из PnL». Без неё исключённые записи
    выглядят как расхождение: в position.json итог один, на панели другой."""
    if not susp:
        return ""
    total = sum(float(t.get("total") or 0) for t in susp)
    syms = sorted({str(t.get("symbol") or "?") for t in susp})
    shown = ", ".join(syms[:4]) + ("…" if len(syms) > 4 else "")
    return (f'<div class="warn-line bad">🚫 исключено из PnL и статистики: '
            f'{len(susp)} подозрительных сделок ({money(total)}) — '
            f'{html_escape(shown)}. Нулевое удержание без выплат funding: '
            f'прибыли взяться неоткуда (битые цены ног). Разбор: '
            f'<code>python funding_executor.py sanity</code></div>')


def positions_of(state: dict) -> list[dict]:
    """Открытые позиции: портфель positions[] или legacy-поле position.

    Тот же разбор, что в funding_executor.positions_of: старый файл
    (одна пара в position) читается как портфель из одной.
    """
    plist = state.get("positions")
    if isinstance(plist, list):
        return [p for p in plist if isinstance(p, dict)]
    pos = state.get("position")
    return [pos] if isinstance(pos, dict) else []


def position_block(pos: dict | None, live: float | None,
                   perp_now: float | None) -> str:
    if not pos:
        return ('<div class="empty">Позиции нет — бот ищет окно. '
                'Когда APY, базис и ликвидность совпадут, пара откроется '
                'автоматически.</div>')
    held_h = (datetime.now(timezone.utc).timestamp() * 1000
              - pos["opened_ms"]) / 3_600_000
    risk = M.position_risk(pos, perp_now)
    # цены может не быть (--no-live или нет сети) — тогда показываем вход,
    # а не падаем: панель обязана рисоваться всегда
    perp_txt = f"{perp_now:.6g}" if perp_now else "—"
    move_txt = (f" ({pct(risk.get('move_pct', 0))})" if risk else "")

    pnl_html = (f'<div class="value {cls(live)}">{money(live)}</div>'
                if live is not None else '<div class="value muted">н/д</div>')

    # риск-строка: сколько до стопа и сколько маржи уже съедено
    risk_html = ""
    if risk:
        used = risk["margin_used_pct"]
        bar_w = min(100.0, max(0.0, used))
        tone = "down" if used > 60 else ("warn" if used > 30 else "ok")
        stop_txt = (f"до стопа {pct(risk['to_stop_pct'], 2)}"
                    if risk.get("to_stop_pct") is not None else "стоп не выставлен")
        stop_badge = ("<span class='badge ok'>биржевой стоп</span>"
                      if risk.get("stop_armed")
                      else "<span class='badge no'>стоп не выставлен</span>")
        risk_html = f'''
<div class="risk">
  <div class="risk-head"><span>Маржа под риском (шорт)</span>
    <b class="{tone}">{used:+.1f}%</b></div>
  <div class="meter"><div class="meter-fill {tone}" style="width:{bar_w:.1f}%"></div></div>
  <div class="sub">{stop_txt} · убыток при стопе {money(risk['loss_at_stop'])}
    · ликвидация ≈ {pct(risk.get('liq_est_pct', 0), 0)} {stop_badge}</div>
</div>'''

    neg = pos.get("neg_streak", 0)
    neg_cls = "down" if neg > 0 else ""
    return f'''
<div class="pos-grid">
  <div><span class="label">Символ</span><b>{html_escape(pos['symbol'])}</b></div>
  <div><span class="label">Режим</span>
    <b><span class="badge {pos['mode']}">{pos['mode']}</span></b></div>
  <div><span class="label">Нотион</span><b>${float(pos['notional']):,.2f}</b></div>
  <div><span class="label">Маржа</span><b>${float(pos['margin']):,.2f}</b></div>
  <div><span class="label">Плечо</span><b>{pos['leverage']}×</b></div>
  <div><span class="label">Спот вход</span><b>{float(pos['spot_entry']):.6g}</b></div>
  <div><span class="label">Перп вход</span><b>{float(pos['perp_entry']):.6g}</b></div>
  <div><span class="label">Перп сейчас</span>
    <b>{perp_txt}</b> <span class="sub">{move_txt}</span></div>
  <div><span class="label">Funding</span><b class="up">{money(pos['funding_accrued'])}</b></div>
  <div><span class="label">Выплат</span><b>{pos.get('payouts', 0)}</b></div>
  <div><span class="label">Отриц. подряд</span><b class="{neg_cls}">{neg}/3</b></div>
  <div><span class="label">Базис вход</span>
    <b>{pct(pos['basis_entry'], 3) if pos.get('basis_entry') is not None else '—'}</b></div>
</div>
<div class="pos-live">{pnl_html}
  <div class="sub">живой PnL · удержано {hold_str(held_h)} ·
    вход ~{float(pos.get('entry_cost_pct', 0)):.2f}% нотиона</div></div>
{risk_html}'''


def trades_table(trades: list[dict]) -> str:
    if not trades:
        return '<div class="empty">История пуста.</div>'
    rows = []
    for i, t in enumerate(trades, 1):
        c = cls(t.get("total", 0))
        pnl_ex = t.get("pnl_excl_funding")
        pnl_ex = pnl_ex if pnl_ex is not None else (t.get("total") or 0.0)
        # разбивка по цене: у дельта-нейтрала ≈0, ненулевое = позиция
        # была направленной — это стоит видеть построчно
        price = t.get("price_pnl")
        price_cell = (f'<td class="{cls(price)}">{money(price)}</td>'
                      if price is not None else "<td class='muted'>—</td>")
        apy = t.get("apy30")
        rows.append(
            f"<tr><td class='muted'>#{i}</td><td><b>{html_escape(t.get('symbol', '—'))}</b></td>"
            f"<td><span class='badge {t.get('mode', '')}'>{t.get('mode', '—')}</span></td>"
            f"<td>{fmt_ts(t.get('ts'))}</td>"
            f"<td>{hold_str(t.get('held_hours'))}</td>"
            f"<td class='muted'>{pct(apy, 1) if apy is not None else '—'}</td>"
            f"{price_cell}"
            f"<td class='up'>{money(t.get('funding') or 0)}</td>"
            f"<td class='{c}'>{money(pnl_ex)}</td>"
            f"<td class='{c}'><b>{money(t.get('total') or 0)}</b></td>"
            f"<td class='{c}'>{pct(t.get('return_pct') or 0)}</td></tr>")
    return f'''<table class="trades">
<thead><tr><th>#</th><th>Символ</th><th>Режим</th><th>Закрыта</th><th>Холд</th>
<th>APY прогноз</th><th>PnL цена</th><th>Funding</th><th>PnL</th><th>Итого</th>
<th>%</th></tr></thead><tbody>{"".join(rows)}</tbody></table>'''


def symbol_table(trades: list[dict]) -> str:
    rows_src = M.per_symbol(trades)
    if not rows_src:
        return '<div class="empty">Нет символов для разбивки.</div>'
    rows = []
    for r in rows_src:
        rows.append(
            f"<tr><td><b>{html_escape(r['symbol'])}</b></td>"
            f"<td>{r['n']}</td><td>{r['win_rate'] * 100:.0f}%</td>"
            f"<td class='{cls(r['net'])}'>{money(r['net'])}</td>"
            f"<td class='up'>{money(r['funding'])}</td>"
            f"<td>{hold_str(r['avg_hold_h'])}</td>"
            f"<td class='{cls(r['best'])}'>{money(r['best'])}</td>"
            f"<td class='{cls(r['worst'])}'>{money(r['worst'])}</td></tr>")
    return f'''<table class="trades">
<thead><tr><th>Символ</th><th>Сделок</th><th>Win</th><th>Итого</th>
<th>Funding</th><th>Ср. холд</th><th>Лучшая</th><th>Худшая</th></tr></thead>
<tbody>{"".join(rows)}</tbody></table>'''


def hold_table(trades: list[dict]) -> str:
    """PnL по времени удержания: на чём стратегия реально зарабатывает."""
    buckets = M.hold_buckets(trades)
    if not any(b["n"] for b in buckets):
        return '<div class="empty">Нет данных.</div>'
    mx = max((abs(b["net"]) for b in buckets), default=1.0) or 1.0
    rows = []
    for b in buckets:
        w = abs(b["net"]) / mx * 50
        bar = ("<div class='hbar'>"
               f"<div class='hbar-fill {cls(b['net'])}' style='width:{w:.1f}%'>"
               f"</div></div>")
        rows.append(f"<tr><td><b>{b['label']}</b></td><td>{b['n']}</td>"
                    f"<td class='up'>{money(b['funding'])}</td>"
                    f"<td class='{cls(b['net'])}'>{money(b['net'])}</td>"
                    f"<td>{bar}</td></tr>")
    return f'''<table class="trades">
<thead><tr><th>Удержание</th><th>Сделок</th><th>Funding</th><th>Итого</th>
<th>Вклад</th></tr></thead><tbody>{"".join(rows)}</tbody></table>'''


def market_block() -> str:
    snap = load_json(SNAPSHOT_FILE, None, expect=dict)
    if not snap:
        return ('<div class="empty">Нет snapshot.json — собери: '
                '<code>python funding_monitor.py --json snapshot.json</code>'
                '</div>')
    rows = (snap.get("top_rows") or [])[:10]
    port = snap.get("portfolio") or {}
    passed = (port.get("passed") or {}).get("symbols") or []
    trs = []
    for r in rows:
        w30 = r.get("w30") or {}
        basis = r.get("basis")
        basis_s = f"{basis:+.3f}%" if basis is not None else "нет"
        ok = r.get("verdict_ok")
        apy = w30.get("avg_apy")
        # горизонтальная шкала APY — сравнение ставок между собой «на глаз»
        apy_s = pct(apy, 1) if apy is not None else "—"
        bar = ""
        if apy is not None and apy > 0:
            w = min(100.0, apy / 40 * 100)
            bar = (f"<div class='hbar'><div class='hbar-fill up' "
                   f"style='width:{w:.1f}%'></div></div>")
        trs.append(
            f"<tr><td><b>{html_escape(r['symbol'])}</b></td>"
            f"<td>{r['funding_rate'] * 100:+.4f}%</td>"
            f"<td class='{cls(apy or 0)}'>{apy_s}</td><td>{bar}</td>"
            f"<td>{basis_s}</td>"
            f"<td>{(r.get('w30') or {}).get('pos_share', 0):.0f}%</td>"
            f"<td>{M.fmt_money(r['oi_value']) if hasattr(M, 'fmt_money') else (r.get('oi_value', 0) / 1e6):.0f}M</td>"
            f"<td><span class='badge {'ok' if ok else 'no'}'>"
            f"{'ГОДЕН' if ok else '✗'}</span></td></tr>")
    head = (f"<div class='sub'>Снимок {str(snap.get('iso', ''))[:16]} · "
            f"прошли фильтр: {html_escape(', '.join(passed)) if passed else 'никто (окно не пришло)'}"
            "</div>")
    return head + f'''<table class="trades">
<thead><tr><th>Символ</th><th>Ставка</th><th>APY 30д</th><th></th><th>Базис</th>
<th>+30д</th><th>OI</th><th>Вердикт</th></tr></thead>
<tbody>{"".join(trs)}</tbody></table>'''


def memory_block() -> str:
    """Память самообучения: скоринг символов, калибровка порогов, уроки."""
    try:
        import bybit_memory as memory
    except ImportError:
        return ('<div class="empty">bybit_memory.py не найден — память '
                'самообучения недоступна.</div>')
    mem = load_json(MEMORY_FILE, None, expect=dict) or memory.load_memory()
    syms = memory.all_symbols(mem)
    rows = []
    for sym in syms:
        st = memory.symbol_stats(mem, sym)
        sym_rows = (mem.get("trades") or {}).get(sym, [])
        n_all = len(sym_rows)
        # подозрительные (фантомные) записи в скоринг не идут — показываем их
        # отдельно, чтобы «0 оценено из 20» не читалось как поломка памяти
        n_susp = sum(1 for t in sym_rows if S.is_suspicious(t))
        stop = memory.avoid_symbol(mem, sym)
        # «1/20» без пояснения читается как поломка памяти: говорим прямо,
        # сколько записей исключено как подозрительные
        susp_txt = f" · ⚠ {n_susp} подозрит." if n_susp else ""
        if st["n"] == 0:
            status = (f"шум ({n_all - n_susp} короче {memory.MIN_HOLD_H}ч)"
                      if n_all - n_susp else "только неудачные входы")
            if stop:
                status += " + СТОП"
            rows.append(f"<tr><td><b>{html_escape(sym)}</b></td><td>0/{n_all}</td>"
                        f"{''.join('<td>—</td>' for _ in range(5))}"
                        f"<td><span class='badge no'>"
                        f"{html_escape(status + susp_txt)}</span></td></tr>")
            continue
        if stop:
            status, badge = "СТОП-ЛИСТ", "no"
        elif st["avg_pct"] > 0 and st["win_share"] >= 0.6:
            status, badge = "приоритет", "ok"
        elif st["avg_pct"] < 0 and st["n"] >= memory.MIN_TRADES:
            status, badge = "избегать", "no"
        else:
            status, badge = "наблюдение", ""
        rows.append(
            f"<tr><td><b>{html_escape(sym)}</b></td><td>{st['n']}/{n_all}</td>"
            f"<td>{st['win_share'] * 100:.0f}%</td>"
            f"<td class='{cls(st['avg_pct'])}'>{pct(st['avg_pct'])}</td>"
            f"<td class='{cls(st['total'])}'>{money(st['total'])}</td>"
            f"<td>{hold_str(st['avg_hold_h'])}</td>"
            f"<td><span class='badge {badge}'>"
            f"{html_escape(status + susp_txt)}</span></td></tr>")
    table = ""
    if rows:
        table = ('<table class="trades"><thead><tr><th>Символ</th>'
                 '<th>Оцен/всего</th><th>Win</th><th>Ср.PnL%</th><th>Итого</th>'
                 '<th>Ср. холд</th><th>Статус</th></tr></thead><tbody>'
                 + "".join(rows) + "</tbody></table>")

    cal = mem.get("calibration") or {}
    thr = cal.get("thresholds") or {}
    preset = "hold"
    base = {"min_apy30": 10.0, "fast_apy": 3.0, "min_basis": 0.0}
    try:
        import bybit_memory as _bm
        base = _bm.default_thresholds(preset)
    except Exception:  # noqa: BLE001
        pass
    cal_html = ('<div class="sub">Пороги фильтра: '
                f'min_apy30=<b>{thr.get("min_apy30", "—")}</b>% · '
                f'fast_apy=<b>{thr.get("fast_apy", "—")}</b>% · '
                f'min_basis=<b>{thr.get("min_basis", "—")}</b>% '
                f'(база пресета {preset}: {base["min_apy30"]:g}% / '
                f'{base["fast_apy"]:g}%) · качество прогноза: сделок '
                f'{cal.get("n", 0)}, ошибка {cal.get("bias", 0):+.1f} п.п.</div>'
                ) if cal else ('<div class="sub">Калибровка ещё не набрала '
                               'данных (нужно ≥5 сделок с прогнозом).</div>')

    tl = mem.get("threshold_log") or []
    tl_html = ""
    if tl:
        def _chg(e):
            b, a = e.get("before") or {}, e.get("after") or {}
            return ", ".join(f"{k} {b[k]:g}→{a[k]:g}" for k in a
                             if k in b and b[k] is not None and b[k] != a[k])
        items = "".join(
            f"<div class='ev'><span class='ev-ts'>⚖</span>"
            + html_escape(fmt_ts(e.get("ts")) + " " + (e.get("kind") or ""))
            + ": " + html_escape((e.get("trigger") or "")[:90])
            + (f" — {html_escape(_chg(e))}" if _chg(e) else "") + "</div>"
            for e in tl[-5:])
        tl_html = ('<div class="sub" style="margin-top:10px">Журнал порогов '
                   f'(последние {min(len(tl), 5)} из {len(tl)}):</div>'
                   f'<div class="events" style="max-height:140px">{items}</div>')

    em = mem.get("entry_mode") or {}
    em_html = ""
    if (em.get("maker") or {}).get("n") or (em.get("taker") or {}).get("n"):
        mk, tk = em.get("maker") or {}, em.get("taker") or {}
        pref = {"maker": "maker-лимитки",
                "taker": "сразу маркет"}.get(em.get("prefer"),
                                            "maker-попытка + фолбэк")
        em_html = (f'<div class="sub">Режим входа: <b>{pref}</b> · '
                   f"maker {mk.get('n', 0)} сдел. {mk.get('avg_pct', 0):+.2f}% · "
                   f"taker {tk.get('n', 0)} сдел. {tk.get('avg_pct', 0):+.2f}%</div>")

    lessons = memory.lessons(mem)
    les_html = ""
    if lessons:
        items = "".join(f"<div class='ev'><span class='ev-ts'>🧠</span>"
                        f"{html_escape(t)}</div>" for t in lessons)
        les_html = ('<div class="sub" style="margin-top:10px">Выводы бота:</div>'
                    f'<div class="events" style="max-height:180px">{items}</div>')

    if not table and not lessons:
        return ('<div class="empty">Память пуста — сделок ещё не было. '
                'Бот начнёт учиться после первых закрытых позиций.</div>')
    return (table or '<div class="empty">Нет символов с оценкой.</div>') \
        + cal_html + em_html + tl_html + les_html


def events_block(limit: int = 20) -> str:
    """Последние события бота из bot.log."""
    if not os.path.exists(EVENTS_FILE):
        return ('<div class="empty">Лог пуст — запусти бота: '
                '<code>python funding_executor.py watch</code>.</div>')
    try:
        with open(EVENTS_FILE, encoding="utf-8", errors="replace") as f:
            lines = [ln.rstrip() for ln in f if ln.strip()][-limit:]
    except OSError:
        return '<div class="empty">Лог не читается.</div>'
    if not lines:
        return '<div class="empty">Лог пуст.</div>'
    rows = []
    for ln in reversed(lines):
        ts, _, msg = ln.partition("] ")
        ts = ts.lstrip("[")
        k = ""
        low = msg.lower()
        if any(x in low for x in ("ошибка", "не удал", "внимание", "рассинхрон",
                                  "поврежд", "не продан", "расхождение")):
            k = "ev-warn"
        elif "ENTER" in msg:
            k = "ev-in"
        elif "CLOSE" in msg and "total=$+" in msg:
            k = "ev-up"
        elif "CLOSE" in msg:
            k = "ev-warn"
        rows.append(f"<div class='ev {k}'><span class='ev-ts'>{ts}</span>"
                    f"{html_escape(msg)}</div>")
    return '<div class="events">' + "".join(rows) + "</div>"


# ------------------------------------------------------------------ page
# ------------------------------------------------------------------ actions
# Кнопки управления на панели (только в режиме --serve). Действия не
# выполняются на месте — панель запускает ТЕ ЖЕ CLI-команды бота, что и
# пользователь, отдельным процессом.
#
# Опасные действия (запуск/остановка сервиса и РУЧНОЙ ВХОД) проходят три
# проверки: вход в панель (DASHBOARD_PASSWORD), токен кнопок и явное
# подтверждение в форме. Без заданного пароля они выключены целиком —
# панель без пароля предполагает только localhost.
ACTIONS = {"close", "void", "healthcheck",
           "bot-start", "bot-stop", "bot-restart", "enter",
           "stop-all", "start-all"}
ACTION_LABEL = {"close": "Закрыть позицию",
                "void": "Снять фантом (void)",
                "healthcheck": "Прогнать health-check",
                "bot-start": "Запустить бота",
                "bot-stop": "Остановить бота",
                "bot-restart": "Перезапустить бота",
                "stop-all": "СТОП-ВСЁ: остановить бота и закрыть всё",
                "start-all": "ПУСК-ВСЁ: запустить бота и включить автовход",
                "enter": "Войти в позицию"}
# Действия, которые двигают деньги или останавливают торговлю: только
# с паролем панели (fail closed, если пароль не задан).
DANGEROUS = {"bot-start", "bot-stop", "bot-restart", "enter",
             "stop-all", "start-all"}
# Сервис бота и бинарь systemctl (переопределяются env — нужно для тестов и
# для нестандартных установок).
BOT_SERVICE = os.environ.get("DASHBOARD_BOT_SERVICE", "funding-watch").strip()
SYSTEMCTL = os.environ.get("DASHBOARD_SYSTEMCTL", "systemctl").strip()
# Пауза на команду: вход делает maker-попытки по обеим ногам, ему нужно больше
ACTION_TIMEOUT = {"enter": 300.0}
SYMBOL_RE = re.compile(r"^[A-Z0-9]{4,20}$")
CONFIG_FILE = os.path.join(BASE, "config.json")


def cfg_mode() -> str:
    """Режим торговли из config.json (paper/testnet/mainnet) — для предупреждений.
    Пусто, если файла нет или параметр не задан."""
    data = load_json(CONFIG_FILE, {})
    mode = str((data or {}).get("mode") or "").strip().lower()
    return mode if mode in ("paper", "testnet", "mainnet") else ""


def action_argv(name: str, state: dict, params: dict | None = None) -> list[str]:
    """Аргументы команды для действия (чистая функция — удобно тестировать).
    params — поля формы (для enter: symbol/force)."""
    params = params or {}
    py = sys.executable
    executor = os.path.join(BASE, "funding_executor.py")
    # в портфеле пары могут быть в разных средах — режим берём у выбранной
    sym = normalize_symbol(params.get("symbol")) if params.get("symbol") else ""
    chosen = None
    if sym:
        chosen = next((p for p in positions_of(state)
                       if p.get("symbol") == sym), None)
    pos_mode = ((chosen or state.get("position") or {}).get("mode")
                or cfg_mode() or "testnet")
    if name == "close":
        argv = [py, executor, "close", "--mode", pos_mode]
        if _truthy(params.get("all")):
            # стоп-всё: закрыть весь портфель одной командой
            argv.append("--all")
        elif sym:
            argv += ["--symbol", sym]
        return argv
    if name == "void":
        argv = [py, executor, "void", "--force"]
        if sym:
            argv += ["--symbol", sym]
        return argv
    if name == "healthcheck":
        return [py, os.path.join(BASE, "healthcheck.py")]
    if name in ("bot-start", "bot-stop", "bot-restart"):
        verb = {"bot-start": "start", "bot-stop": "stop",
                "bot-restart": "restart"}[name]
        if not BOT_SERVICE:
            raise ValueError("не задан сервис бота (DASHBOARD_BOT_SERVICE)")
        return [SYSTEMCTL, verb, BOT_SERVICE]
    if name == "enter":
        sym = normalize_symbol(params.get("symbol"))
        if not sym:
            raise ValueError("не указан символ входа (например BTCUSDT)")
        # Тот же режим, что у бота: иначе вход уйдёт в другую среду
        argv = [py, executor, "enter", "--symbol", sym, "--mode", pos_mode]
        if _truthy(params.get("force")):
            argv.append("--force")
        if str(pos_mode).lower() == "mainnet":
            # вход по кнопке — осознанное действие, но политику биржи/бота
            # не обходим: для mainnet нужен тот же флаг подтверждения
            argv.append("--confirm-live")
        return argv
    raise ValueError(f"неизвестное действие: {name}")


def _truthy(value) -> bool:
    """Значение формы/JSON → bool ("1", "on", "true", "yes")."""
    return str(value or "").strip().lower() in ("1", "on", "true", "yes", "да")


def normalize_symbol(raw) -> str:
    """Символ входа: верхний регистр, только [A-Z0-9], иначе пусто."""
    sym = str(raw or "").strip().upper()
    return sym if SYMBOL_RE.match(sym) else ""


def validate_entry(state: dict, params: dict, mode: str = "") -> str | None:
    """Проверка ручного входа. Возврат — текст ошибки или None (можно входить).

    Три независимых подтверждения: позиция не открыта, символ введён дважды
    (поле подтверждения должно совпасть), для mainnet — отметка о реальных
    деньгах. Чистая функция: тестируется без HTTP и без биржи.
    """
    plist = positions_of(state)
    max_pos = max(1, int(load_json(CONFIG_FILE, {}).get("max-positions", 1) or 1))
    if len(plist) >= max_pos:
        held = ", ".join(str(p.get("symbol")) for p in plist)
        if max_pos <= 1:
            return f"позиция уже открыта: {held} — сначала закройте её"
        return (f"портфель заполнен: {len(plist)}/{max_pos} пар ({held}) — "
                f"увеличьте max-positions в config.json или закройте пару")
    sym = normalize_symbol(params.get("symbol"))
    if not sym:
        return "символ должен быть вида BTCUSDT (латиница и цифры)"
    if any(p.get("symbol") == sym for p in plist):
        return f"{sym} уже открыт в портфеле"
    if str(params.get("confirm") or "").strip().upper() != sym:
        return "подтверждение не совпало: повторите символ в поле «повторите символ»"
    if str(mode or "").lower() == "mainnet" and not _truthy(params.get("ack")):
        return ("режим mainnet: отметьте «понимаю, что это реальные деньги»")
    return None


def run_action(name: str, params: dict | None = None,
               timeout: float | None = None) -> dict:
    """Выполнить действие отдельным процессом. Возврат {ok, rc, output}."""
    try:
        argv = action_argv(name, load_json(POSITION_FILE,
                                           {"position": None, "history": []}),
                           params)
    except ValueError as e:
        return {"ok": False, "rc": -1, "output": str(e)}
    timeout = timeout or ACTION_TIMEOUT.get(name, 180.0)
    try:
        res = subprocess.run(argv, cwd=BASE, capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=timeout)
        return {"ok": res.returncode == 0, "rc": res.returncode,
                "output": ((res.stdout or "") + (res.stderr or "")).strip()}
    except subprocess.TimeoutExpired:
        return {"ok": False, "rc": -1, "output": f"таймаут {timeout:g}s"}
    except Exception as e:  # noqa: BLE001 — панель не должна падать
        return {"ok": False, "rc": -1, "output": f"ошибка запуска: {e}"}


# --- Telegram: отчёт «стоп-всё» уходит в канал ---------------------------
# Кнопка аварийная: её жмут, когда панель может быть недоступна (VPN, сеть,
# телефон), поэтому результат КАЖДОГО шага должен уйти туда, где его увидят
# сразу. Ключи — те же, что у бота и health-check: TELEGRAM_BOT_TOKEN /
# TELEGRAM_CHAT_ID (на VPS приходят из .env через EnvironmentFile юнита;
# при ручном запуске --serve читаем .env рядом с панелью — как healthcheck).
TG_API_DEFAULT = "https://api.telegram.org"
TG_MAX_LEN = 4000            # лимит Telegram 4096 — оставляем запас
TG_ENV_LOADED = {"done": False}


def _load_env_file(path: str) -> None:
    """KEY=VALUE построчно из .env, не затирая уже заданные переменные.

    Тот же разбор, что в healthcheck.py: панель обязана вести себя одинаково
    и под systemd (EnvironmentFile), и при запуске руками."""
    if not os.path.exists(path):
        return
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip())


def tg_config() -> tuple[str, str]:
    """(токен, чат) для уведомлений. Пусто — Telegram не настроен."""
    if not TG_ENV_LOADED["done"]:
        _load_env_file(os.path.join(BASE, ".env"))
        TG_ENV_LOADED["done"] = True
    return (os.environ.get("TELEGRAM_BOT_TOKEN", "").strip(),
            os.environ.get("TELEGRAM_CHAT_ID", "").strip())


def tg_send(text: str, timeout: float = 10.0) -> bool:
    """Отправить текст в Telegram. False — не настроено или сбой сети:
    уведомление не должно ломать само действие панели.

    TELEGRAM_API_BASE — точка подмены для тестов (как DASHBOARD_SYSTEMCTL),
    по умолчанию настоящий API."""
    token, chat = tg_config()
    if not token or not chat or not (text or "").strip():
        return False
    body = text if len(text) <= TG_MAX_LEN else (
        text[:TG_MAX_LEN - 20].rstrip() + "\n… (обрезано)")
    api = os.environ.get("TELEGRAM_API_BASE", TG_API_DEFAULT).strip()
    api = (api or TG_API_DEFAULT).rstrip("/")
    try:
        data = urlencode({"chat_id": chat, "text": body,
                          "disable_web_page_preview": "true"}).encode()
        req = urllib.request.Request(
            f"{api}/bot{token}/sendMessage", data=data)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return bool(json.loads(resp.read().decode()).get("ok"))
    except Exception:  # noqa: BLE001 — сеть/токен: панель не падает
        return False


def _step_sender(notify_step, title: str) -> tuple:
    """Отправка шагов составного действия в канал.

    Возврат (send, sent): send(label, block) шлёт блок отчёта, sent — счётчики
    доставки (n — сколько пробовали, ok — сколько дошло). Ошибка канала не
    пробрасывается: уведомление не важнее самого действия.
    """
    sent = {"n": 0, "ok": 0}

    def send(label: str, block: str) -> None:
        block = (block or "").strip()
        sent["n"] += 1
        try:
            if block and notify_step(f"{title} · {label}\n{block}"):
                sent["ok"] += 1
        except Exception:  # noqa: BLE001 — сеть/токен/чужой notify_step
            pass

    return send, sent


def _tg_delivery_line(sent: dict) -> str:
    """Строка отчёта о доставке: в браузере видно, дошло ли до канала."""
    token, chat = tg_config()
    if not (token and chat):
        return ("📨 Telegram: не настроен (TELEGRAM_BOT_TOKEN / "
                "TELEGRAM_CHAT_ID) — отчёт только в браузере")
    if sent["ok"] == sent["n"]:
        return f"📨 Telegram: отправлено {sent['ok']} из {sent['n']} шагов"
    return (f"📨 Telegram: ушло {sent['ok']} из {sent['n']} шагов — "
            f"часть сообщений не доставлена (сеть/токен)")


def _unit_execstart(service: str, timeout: float = 5.0) -> str | None:
    """Строка ExecStart юнита (None — нет systemd/сервиса/прав).

    Нужна, чтобы проверить НЕ только «сервис active», но и с какими флагами
    он запущен: --no-auto / --confirm-live решают, торгует бот или только
    смотрит."""
    if not service:
        return None
    try:
        res = subprocess.run([SYSTEMCTL, "show", "-p", "ExecStart", service],
                             capture_output=True, text=True, encoding="utf-8",
                             errors="replace", timeout=timeout)
        out = ((res.stdout or "") + (res.stderr or "")).strip()
        return out or None
    except Exception:  # noqa: BLE001 — нет systemd: это не ошибка панели
        return None


def _auto_entry_state(execstart: str | None,
                      cfg: dict | None = None) -> tuple[bool, str]:
    """(автовход включён, пояснение) по ExecStart юнита и config.json.

    «Сервис active» ≠ «бот торгует»: auto выключается флагом --no-auto,
    no-auto в config.json, а на mainnet ещё и отсутствием --confirm-live
    (тогда watch только наблюдает). Нечитаемый ExecStart — НЕ подтверждение:
    панель честно говорит, что не знает.
    """
    cfg = cfg if cfg is not None else (load_json(CONFIG_FILE, {}) or {})
    cmd = execstart or ""
    if "--no-auto" in cmd:
        return False, "в ExecStart юнита стоит --no-auto — бот только наблюдает"
    if (cfg or {}).get("no-auto"):
        return False, "в config.json \"no-auto\": true — бот только наблюдает"
    m = re.search(r"--mode[= ]+(\w+)", cmd)
    mode = (m.group(1) if m else str((cfg or {}).get("mode") or "")).lower()
    if mode == "mainnet" and "--confirm-live" not in cmd:
        return False, ("режим mainnet, а в ExecStart нет --confirm-live — "
                       "watch работает только как наблюдатель")
    if not cmd:
        return False, ("ExecStart не прочитан (нет systemd/show) — включён ли "
                       "автовход, панель не знает")
    return True, (f"ExecStart без --no-auto, config no-auto=false "
                  f"(mode {mode or 'н/д'})")


# --- стоп-всё: одно подтверждённое действие из двух шагов ------------------
STOP_ALL_TIMEOUT = {"stop": 60.0, "close": 900.0}
START_ALL_TIMEOUT = {"start": 60.0, "show": 5.0}
# сколько ждём подъёма сервиса: start возвращает 0 сразу, а юнит с
# Restart=always может тут же уйти в failed — «код 0» не значит «работает»
START_ALL_POLL = {"tries": 6, "wait": 1.5}


def _run_cmd_capture(argv: list, timeout: float) -> tuple[int, str]:
    """Запустить команду и вернуть (код возврата, вывод). Исключение — код -1."""
    try:
        res = subprocess.run(argv, cwd=BASE, capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=timeout)
        return res.returncode, ((res.stdout or "") + (res.stderr or "")).strip()
    except subprocess.TimeoutExpired:
        return -1, f"таймаут {timeout:g}s"
    except Exception as e:  # noqa: BLE001
        return -1, f"ошибка запуска: {e}"


def _service_active_uncached(timeout: float = 5.0) -> str:
    """Фактический is-active сервиса БЕЗ кэша: для отчёта «стоп-всё» нужно
    состояние сейчас, а не значение, закэшированное 5 сек назад."""
    if not BOT_SERVICE:
        return "н/д"
    try:
        res = subprocess.run([SYSTEMCTL, "is-active", BOT_SERVICE],
                             capture_output=True, text=True, encoding="utf-8",
                             errors="replace", timeout=timeout)
        line = ((res.stdout or "") + (res.stderr or "")).strip().splitlines()
        return (line[0].strip() if line else "н/д")[:24] or "н/д"
    except Exception:  # noqa: BLE001
        return "н/д"


def run_stop_all(params: dict | None = None, run_cmd=None,
                 state_reader=None, active_probe=None,
                 notify_step=None) -> dict:
    """«Стоп-всё»: остановить сервис бота и закрыть ВСЕ пары портфеля.

    Одно подтверждение → последовательные шаги с отчётом по каждому:
      1) остановка сервиса (иначе авто-вход может открыть пару взамен
         закрытой — гасим бота ПЕРЕД закрытием);
      2) close --all — закрытие всех открытых пар;
      3) сверка факта: is-active сервиса и сколько пар осталось в
         position.json (не верим кодам возврата на слово).

    Если сервис остановить не удалось — закрываем всё равно (снятие
    экспозиции важнее), но отчёт явно предупреждает, что бот ещё жив и может
    войти заново. run_cmd/state_reader/active_probe/notify_step — точки
    внедрения для тестов (по умолчанию боевые: команды, файл, systemctl и
    Telegram). Отчёт по шагам уходит в канал (notify_step → True = доставлено),
    а не только в браузер: закрытие портфеля может идти минутами.
    Возврат {ok, rc, output}: ok — все шаги прошли и сверка подтвердила.
    """
    run_cmd = run_cmd or _run_cmd_capture
    state_reader = state_reader or (
        lambda: load_json(POSITION_FILE, {"position": None, "history": []}))
    active_probe = active_probe or _service_active_uncached

    state = state_reader()
    n_open = len(positions_of(state))
    report = [f"=== СТОП-ВСЁ: остановить бота и закрыть {n_open} пар(ы) ===", ""]
    ok = True
    stopped = False

    # Отчёт уходит по шагам НЕ в конце: закрытие пар может идти минутами, а
    # «бот остановлен» нужно знать сразу — это главная новость аварийного
    # действия. В браузере при этом виден весь отчёт целиком.
    notify_step = notify_step if notify_step is not None else tg_send
    send, sent = _step_sender(notify_step, "🚨 СТОП-ВСЁ")

    def step_done(label: str, start: int) -> None:
        """Отправить результат шага в канал (start — начало блока в report)."""
        send(label, "\n".join(report[start:]))

    # --- шаг 1: остановка сервиса -----------------------------------------
    m1 = len(report)
    report.append(f"Шаг 1/3: остановка сервиса {BOT_SERVICE or '—'}")
    if not BOT_SERVICE:
        report.append("  ⚠ сервис не задан (DASHBOARD_BOT_SERVICE) — пропускаю")
    else:
        rc, out = run_cmd([SYSTEMCTL, "stop", BOT_SERVICE],
                          STOP_ALL_TIMEOUT["stop"])
        state_now = active_probe()
        stopped = rc == 0 and state_now not in ("active", "activating",
                                                "reloading")
        mark = "✅" if stopped else "⚠"
        report.append(f"  {mark} systemctl stop {BOT_SERVICE}: код {rc}, "
                      f"is-active → {state_now}")
        if out:
            report.append("  " + out.replace("\n", "\n  "))
        if not stopped:
            ok = False
            report.append("  ⚠ БОТ НЕ ОСТАНОВЛЕН — авто-вход может открыть "
                          "пару взамен закрытой. Продолжаю закрывать и так.")
    report.append("")
    step_done("шаг 1/3 — остановка сервиса", m1)

    # --- шаг 2: закрытие всех пар ------------------------------------------
    # Список пар перечитываем ЗДЕСЬ, а не берём из первого чтения: между
    # чтением в начале и остановкой сервиса бот мог успеть открыть пару —
    # закрывать надо то, что открыто СЕЙЧАС, иначе кнопка «стоп-всё»
    # оставит именно ту пару, ради которой её и нажимали.
    fresh = state_reader()
    fresh_open = len(positions_of(fresh))
    m2 = len(report)
    report.append(f"Шаг 2/3: закрытие открытых пар ({fresh_open})")
    if fresh_open != n_open:
        report.append(f"  ℹ портфель изменился, пока бот останавливался: "
                      f"открытых пар {fresh_open} (в начале было {n_open})")
    if fresh_open == 0:
        report.append("  ✅ открытых пар нет — закрывать нечего")
    else:
        argv = action_argv("close", fresh, {"all": "1"})
        rc2, out2 = run_cmd(argv, STOP_ALL_TIMEOUT["close"])
        report.append(f"  {'✅' if rc2 == 0 else '⚠'} close --all: код {rc2}")
        if out2:
            report.append("  " + out2.replace("\n", "\n  "))
        if rc2 != 0:
            ok = False
            report.append("  ⚠ закрытие завершилось с ошибкой — разберитесь "
                          "по выводу выше (см. также: health-check)")
    report.append("")
    step_done("шаг 2/3 — закрытие пар", m2)

    # --- шаг 3: сверка факта ----------------------------------------------
    m3 = len(report)
    report.append("Шаг 3/3: сверка результата")
    left = len(positions_of(state_reader()))
    if left:
        ok = False
        report.append(f"  ⚠ в position.json ОСТАЛОСЬ пар: {left} — закрытие "
                      f"прошло не до конца")
    else:
        report.append("  ✅ открытых пар не осталось")
    if BOT_SERVICE:
        final = active_probe()
        if final in ("active", "activating"):
            ok = False
            report.append(f"  ⚠ сервис снова {final} — бот перезапустился или "
                          f"остановился не полностью")
        else:
            report.append(f"  ✅ сервис не активен ({final})")
    report.append("")
    report.append("ИТОГ: " + ("бот остановлен, все пары закрыты"
                              if ok else "ЕСТЬ ПРОБЛЕМЫ — см. шаги выше"))
    if not stopped and BOT_SERVICE:
        report.append("Чтобы вернуть бота: systemctl start " + BOT_SERVICE)
    # последнее сообщение — сверка ВМЕСТЕ с итогом: отдельно «шаг 3» без
    # вердикта читался бы как незаконченный отчёт
    step_done("итог — сверка результата", m3)

    # честно сказано, дошёл ли отчёт до канала (а не только до браузера)
    report.append(_tg_delivery_line(sent))
    return {"ok": ok, "rc": 0 if ok else 1, "output": "\n".join(report)}


def run_start_all(params: dict | None = None, run_cmd=None,
                  state_reader=None, active_probe=None, notify_step=None,
                  sleep_fn=None, unit_probe=None) -> dict:
    """«ПУСК-ВСЁ»: запустить сервис бота И включить автовход (зеркало стоп-всё).

    Одно подтверждение → три шага с отчётом по каждому:
      1) предполётная проверка: портфель ОБЯЗАН быть пуст. Кнопка включает
         автовход — с уже открытыми парами бот сначала разбирал бы их по
         своим правилам, а это не то, чего ждут от «пуска». Не пуст →
         не запускаем и говорим, что делать (рядовой запуск — кнопка
         «Запустить бота»);
      2) systemctl start + ОПРОС is-active: код 0 у start не значит, что
         процесс жив (юнит с Restart=always успевает моргнуть failed);
      3) сверка автовхода: ExecStart без --no-auto, no-auto=false в
         config.json, для mainnet — с --confirm-live (иначе watch только
         наблюдает). «Сервис active» без этой сверки — не гарантия, что
         бот торгует.

    run_cmd/state_reader/active_probe/notify_step/sleep_fn/unit_probe — точки
    внедрения для тестов (по умолчанию боевые).
    Возврат {ok, rc, output}: ok — портфель пуст, сервис поднялся и автовход
    проверен включённым.
    """
    run_cmd = run_cmd or _run_cmd_capture
    state_reader = state_reader or (
        lambda: load_json(POSITION_FILE, {"position": None, "history": []}))
    active_probe = active_probe or _service_active_uncached
    sleep_fn = sleep_fn or time.sleep
    unit_probe = unit_probe or (lambda: _unit_execstart(BOT_SERVICE))
    notify_step = notify_step if notify_step is not None else tg_send
    send, sent = _step_sender(notify_step, "▶️ ПУСК-ВСЁ")

    state = state_reader()
    n_open = len(positions_of(state))
    report = ["=== ПУСК-ВСЁ: запустить бота и включить автовход ===", ""]
    ok = True
    started = False

    # --- шаг 1: предполётная проверка -------------------------------------
    m1 = len(report)
    report.append("Шаг 1/3: предполётная проверка (портфель пуст)")
    if n_open:
        report.append(f"  ⚠ в портфеле {n_open} пар(ы) — НЕ запускаю: "
                      f"«пуск-всё» включает автовход для пустого портфеля")
        report.append("  Сначала закройте пары (СТОП-ВСЁ) — или запустите сервис "
                      "как есть кнопкой «Запустить бота».")
        report.append("")
        send("шаг 1/3 — проверка портфеля", "\n".join(report[m1:]))
        report.append("ИТОГ: бот НЕ запущен — портфель не пуст")
        # вердикт отдельным сообщением: в канале он не должен теряться
        # среди подробностей проверки — как и в успешном прогоне
        send("итог — отказ", "ИТОГ: бот НЕ запущен — портфель не пуст")
        report.append(_tg_delivery_line(sent))
        return {"ok": False, "rc": 1, "output": "\n".join(report)}
    report.append("  ✅ открытых пар нет — можно включать автовход")
    report.append("")
    send("шаг 1/3 — проверка портфеля", "\n".join(report[m1:]))

    # --- шаг 2: запуск сервиса и ожидание подъёма -------------------------
    m2 = len(report)
    report.append(f"Шаг 2/3: запуск сервиса {BOT_SERVICE or '—'}")
    if not BOT_SERVICE:
        ok = False
        report.append("  ⚠ сервис не задан (DASHBOARD_BOT_SERVICE) — "
                      "запускать нечего")
    else:
        before = active_probe()
        if before in ("active", "activating"):
            # start на активном сервисе вернёт 0, но «поднялся» — неправда:
            # он уже работал. Проверяем дальше самим опросом.
            report.append(f"  ℹ сервис уже {before} — start не требуется")
        else:
            rc, out = run_cmd([SYSTEMCTL, "start", BOT_SERVICE],
                              START_ALL_TIMEOUT["start"])
            report.append(f"  systemctl start {BOT_SERVICE}: код {rc}")
            if out:
                report.append("  " + out.replace("\n", "\n  "))
        # опрос: «код 0» — не доказательство, что процесс жив
        seen = before
        t0 = time.time()
        for _ in range(max(1, START_ALL_POLL["tries"])):
            seen = active_probe()
            if seen == "active":
                break
            sleep_fn(START_ALL_POLL["wait"])
        started = seen == "active"
        waited = time.time() - t0
        if started:
            report.append(f"  ✅ сервис active" +
                          (f" (поднялся за {waited:.1f}с)" if waited >= 0.05
                           else ""))
        else:
            ok = False
            report.append(f"  ⚠ СЕРВИС НЕ ПОДНЯЛСЯ — is-active → {seen}. "
                          f"Бот не работает; смотрите: journalctl -u "
                          f"{BOT_SERVICE} -n 30 --no-pager")
    report.append("")
    send("шаг 2/3 — запуск сервиса", "\n".join(report[m2:]))

    # --- шаг 3: сверка автовхода ------------------------------------------
    m3 = len(report)
    report.append("Шаг 3/3: сверка автовхода")
    auto_on, why = _auto_entry_state(unit_probe())
    if auto_on:
        report.append(f"  ✅ автовход включён: {why}")
    else:
        ok = False
        report.append(f"  ⚠ автовход НЕ включён: {why}")
        report.append("  Сервис может работать, но пары не открываются. "
                      "Поправьте юнит/config.json и перезапустите сервис.")
    # портфель после старта: пара могла открыться сразу — это норма, а не
    # сбой; сообщаем факт, чтобы картина сходилась с панелью
    after = len(positions_of(state_reader()))
    if after:
        report.append(f"  ℹ после запуска в портфеле уже {after} пар(ы) — "
                      f"автовход работает")
    else:
        report.append("  ✅ портфель по-прежнему пуст")
    report.append("")
    report.append("ИТОГ: " + ("бот запущен, автовход включён"
                              if ok else "ЕСТЬ ПРОБЛЕМЫ — см. шаги выше"))
    if BOT_SERVICE and not started:
        report.append("Сервис не поднялся — панель не врёт: бот не торгует.")
    # последнее сообщение — сверка вместе с итогом
    send("итог — сверка автовхода", "\n".join(report[m3:]))
    report.append(_tg_delivery_line(sent))
    return {"ok": ok, "rc": 0 if ok else 1, "output": "\n".join(report)}


_BOT_STATUS = {"at": 0.0, "value": "н/д"}
BOT_STATUS_TTL = 5.0         # панель пересобирается на каждый запрос — кэшируем


def bot_status(timeout: float = 2.0) -> str:
    """Состояние сервиса бота ('active'/'inactive'/'failed'/'н/д').

    Без systemd (Windows, контейнер) команда падает — панель просто
    показывает «н/д», а не рисует ложное «остановлен». Кэш 5 с: страница
    пересобирается на каждый запрос, а systemctl — это fork+exec.
    """
    if not BOT_SERVICE:
        return "н/д"
    now = time.time()
    if now - _BOT_STATUS["at"] < BOT_STATUS_TTL:
        return _BOT_STATUS["value"]
    value = "н/д"
    try:
        res = subprocess.run([SYSTEMCTL, "is-active", BOT_SERVICE],
                             capture_output=True, text=True, encoding="utf-8",
                             errors="replace", timeout=timeout)
        line = ((res.stdout or "") + (res.stderr or "")).strip().splitlines()
        value = (line[0].strip() if line else "н/д")[:24] or "н/д"
    except Exception:  # noqa: BLE001 — нет systemctl: это не ошибка панели
        value = "н/д"
    _BOT_STATUS.update(at=now, value=value)
    return value


def _origin_ok(origin: str, host_header: str = "") -> bool:
    """Анти-CSRF: локальные Origin, отсутствие Origin или тот же хост.

    host_header — заголовок Host запроса. Он нужен панели, открытой по
    IP:порту: браузер пришлёт Origin http://<IP>:<порт>, и это НЕ чужой
    Origin. Сравниваем хост, а не строку целиком (порт может отличаться,
    если панель стоит за прокси).
    """
    if not origin:
        return True
    try:
        host = (urlparse(origin).hostname or "").lower()
    except ValueError:
        return False
    if not host:
        return False
    if host in ("127.0.0.1", "localhost", "::1"):
        return True
    if host_header:
        try:
            req_host = (urlparse("//" + host_header).hostname or "").lower()
        except ValueError:
            req_host = ""
        if req_host and host == req_host:
            return True
    return False


# Токен действий. Читается из окружения (DASHBOARD_TOKEN); если сервер
# запущен без него — генерируется случайный и печатается один раз в консоль.
# Пустой токен = действия запрещены (fail closed). Значение никогда не логируем.
TOKEN = os.environ.get("DASHBOARD_TOKEN", "").strip()
_ACTION_LOCK = threading.Lock()


# ------------------------------------------------------------------ login
# Панель показывает позицию, PnL и умеет закрывать позицию, поэтому
# публичный bind (--host 0.0.0.0) разрешён только с паролем. Пароль читается
# из окружения (DASHBOARD_PASSWORD) или --password; в HTML/логи не попадает.
PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "").strip()
SESSION_COOKIE = "fb_dash_session"
SESSION_TTL = 7 * 24 * 3600          # неделя; сессии живут в памяти процесса
SESSIONS: dict[str, float] = {}      # токен → время истечения (time.time())
_SESSION_LOCK = threading.Lock()
# Анти-брутфорс: ip → список неудачных попыток (время). 5 за 5 минут — пауза.
LOGIN_FAILS: dict[str, list[float]] = {}
LOGIN_MAX_FAILS = 5
LOGIN_WINDOW = 300.0


def password_ok(given: str, expected: str | None = None) -> bool:
    """Сверка пароля без утечки по времени. Пустой ожидаемый — запрет."""
    want = PASSWORD if expected is None else expected
    if not want:
        return False
    try:
        return hmac.compare_digest(want.encode("utf-8"),
                                   (given or "").encode("utf-8"))
    except (AttributeError, UnicodeError):
        return False


def parse_cookies(header: str) -> dict[str, str]:
    """Заголовок Cookie → {имя: значение} (битые куски пропускаются)."""
    out: dict[str, str] = {}
    for part in (header or "").split(";"):
        k, sep, v = part.strip().partition("=")
        if sep and k:
            out[k] = v
    return out


def new_session(now: float | None = None) -> str:
    tok = secrets.token_urlsafe(24)
    now = time.time() if now is None else now
    with _SESSION_LOCK:
        SESSIONS[tok] = now + SESSION_TTL
    return tok


def session_ok(cookie_header: str, now: float | None = None) -> bool:
    """Действующая сессия: токен есть в памяти и не истёк."""
    tok = parse_cookies(cookie_header).get(SESSION_COOKIE, "")
    if not tok:
        return False
    now = time.time() if now is None else now
    with _SESSION_LOCK:
        exp = SESSIONS.get(tok)
        if exp is None:
            return False
        if exp <= now:
            SESSIONS.pop(tok, None)
            return False
        return True


def drop_session(cookie_header: str) -> None:
    tok = parse_cookies(cookie_header).get(SESSION_COOKIE, "")
    with _SESSION_LOCK:
        SESSIONS.pop(tok, None)


def login_blocked(ip: str, now: float | None = None) -> float:
    """Сколько секунд ещё нельзя пробовать пароль (0 — можно)."""
    now = time.time() if now is None else now
    recent = [t for t in LOGIN_FAILS.get(ip, []) if now - t < LOGIN_WINDOW]
    if recent:
        LOGIN_FAILS[ip] = recent
    else:
        LOGIN_FAILS.pop(ip, None)
    if len(recent) >= LOGIN_MAX_FAILS:
        return max(0.0, LOGIN_WINDOW - (now - recent[-1]))
    return 0.0


def note_login_failure(ip: str, now: float | None = None) -> None:
    now = time.time() if now is None else now
    recent = [t for t in LOGIN_FAILS.get(ip, []) if now - t < LOGIN_WINDOW]
    recent.append(now)
    LOGIN_FAILS[ip] = recent


def note_login_success(ip: str) -> None:
    LOGIN_FAILS.pop(ip, None)


def login_page(error: str = "") -> str:
    """Страница входа — тот же тёмный стиль, что и панель, без внешних CDN."""
    err = f"<div class='err'>{html_escape(error)}</div>" if error else ""
    return f"""<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Вход — Funding Terminal</title>
<style>
  :root {{ color-scheme: dark }}
  * {{ box-sizing: border-box }}
  body {{ margin: 0; min-height: 100vh; display: grid; place-items: center;
         background: #0a0d13; color: #d7dee8;
         font: 15px/1.45 system-ui, -apple-system, Segoe UI, sans-serif }}
  .card {{ width: min(380px, calc(100vw - 32px)); padding: 26px 24px;
           background: #11161f; border: 1px solid #1e2733;
           border-radius: 14px; box-shadow: 0 18px 50px #0008 }}
  h1 {{ margin: 0 0 4px; font-size: 19px; letter-spacing: .2px }}
  .sub {{ margin: 0 0 18px; color: #8b97a8; font-size: 13px }}
  label {{ display: block; margin: 0 0 6px; color: #8b97a8; font-size: 12px;
           text-transform: uppercase; letter-spacing: .08em }}
  input {{ width: 100%; padding: 11px 12px; border-radius: 9px;
           border: 1px solid #2a3544; background: #0a0d13; color: #d7dee8;
           font: inherit; outline: none }}
  input:focus {{ border-color: #3d7bfd }}
  button {{ width: 100%; margin-top: 14px; padding: 11px 12px; border: 0;
            border-radius: 9px; background: #2f6fed; color: #fff;
            font: 600 15px system-ui, sans-serif; cursor: pointer }}
  button:hover {{ background: #3d7bfd }}
  .err {{ margin: 0 0 12px; padding: 9px 11px; border-radius: 8px;
          background: #2a1418; border: 1px solid #5a2430;
          color: #ff8a9a; font-size: 13px }}
</style></head><body>
<form class="card" method="post" action="/login">
  <h1>Funding Terminal</h1>
  <p class="sub">Панель бота — нужен пароль доступа</p>
  {err}
  <label for="pw">Пароль</label>
  <input id="pw" name="password" type="password" autocomplete="current-password"
         autofocus required>
  <button type="submit">Войти</button>
</form>
</body></html>"""


def token_ok(given: str, expected: str | None = None) -> bool:
    """Сверка токена без утечки по времени. Пустой ожидаемый токен — запрет.
    expected=None → берётся модульный TOKEN (удобно тестировать обработчик).

    Сравниваются байты, а не str: hmac.compare_digest со строками падает с
    TypeError на любом не-ASCII символе («сравнение строк с не-ASCII не
    поддерживается»), а токен приходит из тела запроса, то есть от кого угодно.
    Без этого запрос с кириллицей в поле токена не получал ответа вообще.
    """
    want = TOKEN if expected is None else expected
    if not want:
        return False
    try:
        return hmac.compare_digest(want.encode("utf-8"),
                                   (given or "").encode("utf-8"))
    except (AttributeError, UnicodeError):
        return False


# Тексты подтверждений — отдельной функцией: их обязано быть видно в тестах
# (кнопка, двигающая деньги, требует осознанного нажатия, а не догадки).
CONFIRM_TEXT = {
    "close": "Закрыть позицию по рынку (reduce-only перп + продажа спота)?",
    "void": "Снять фантом без сверки с биржей (void --force)?",
    "stop-all": "СТОП-ВСЁ: остановить бота и закрыть ВСЕ открытые пары "
                "по рынку? Автовход выключится, позиции закроются с "
                "рыночными ордерами (комиссии + проскальзывание). "
                "Продолжить?",
    "start-all": "ПУСК-ВСЁ: запустить бота и включить автовход? Бот начнёт "
                 "искать пары и открывать позиции по рынку — это комиссии и "
                 "реальные риски (на mainnet — реальные деньги). "
                 "Продолжить?",
    "bot-stop": "Остановить бота? Открытая позиция останется без "
                "контроля — kill-switchы работать не будут.",
    "bot-restart": "Перезапустить бота?",
}


def action_confirm(name: str, params: dict | None = None) -> str | None:
    """Текст confirm() для действия, или None — подтверждение не нужно."""
    confirm = CONFIRM_TEXT.get(name)
    if confirm and params and params.get("symbol"):
        confirm += f" ({params['symbol']})"
    return confirm


def action_form(name: str, token: str, disabled: bool = False,
                params: dict | None = None) -> str:
    """HTML-форма одной кнопки действия (POST /action) со скрытым токеном.
    params — дополнительные скрытые поля (для close/void: symbol — какую
    пару портфеля закрывать)."""
    label = ACTION_LABEL[name]
    confirm = action_confirm(name, params)
    on = f' onsubmit="return confirm(\'{confirm}\')"' if confirm else ""
    dis = " disabled" if disabled else ""
    extra = "".join(f"<input type='hidden' name='{html_escape(str(k))}' "
                     f"value='{html_escape(str(v))}'>"
                     for k, v in (params or {}).items())
    return (f"<form class='act' method='post' action='/action'{on}>"
            f"<input type='hidden' name='token' value='{html_escape(token)}'>"
            f"<input type='hidden' name='name' value='{name}'>{extra}"
            f"<button class='btn {name}'{dis}>{label}</button></form>")


def actions_block(pos: dict | None, token: str, symbol: str | None = None,
                  with_health: bool = True) -> str:
    """Кнопки управления. Без токена (статичный --out) не показываются.

    symbol — закрывать/снимать именно эту пару портфеля (в портфеле без
    явного символа команда close отказывается работать — и правильно).
    """
    if not token:
        return ""
    no_pos = not pos
    params = {"symbol": symbol} if symbol else None
    forms = [action_form("close", token, disabled=no_pos, params=params),
             action_form("void", token, disabled=no_pos, params=params)]
    if with_health:
        forms.append(action_form("healthcheck", token))
    return ("<div class='acts'>" + "".join(forms) + "</div>"
            "<div class='sub'>Кнопки запускают те же CLI-команды бота — "
            "отдельным процессом на сервере. Кнопки close/void активны, "
            "когда есть открытая позиция; health-check доступен всегда.</div>")


def portfolio_block(positions: list[dict], lives: list, perps: list,
                    token: str) -> str:
    """Секция «Текущая позиция» для портфеля: пары — по одной карточке.

    С одним слотом рисуется как раньше (один блок + кнопки), с несколькими —
    карточка на пару со своими кнопками закрытия: закрывать в портфеле нужно
    ИМЕННО выбранную пару.
    """
    if not positions:
        return position_block(None, None, None) + actions_block(None, token)
    if len(positions) == 1:
        return (position_block(positions[0], lives[0], perps[0])
                + actions_block(positions[0], token))
    total = sum(l for l in lives if l is not None) if any(
        l is not None for l in lives) else None
    out = [f'<div class="sub">Портфель: открыто {len(positions)} пар · '
           f'суммарный живой PnL {money(total) if total is not None else "н/д"}'
           f'</div>']
    for i, p in enumerate(positions):
        sym = str(p.get("symbol") or "?")
        out.append(f'<div class="pos-card">'
                   f'<div class="pos-head"><b>{html_escape(sym)}</b>'
                   f'<span class="badge {p.get("mode", "")}">'
                   f'{html_escape(str(p.get("mode", "")))}</span></div>'
                   + position_block(p, lives[i], perps[i])
                   + actions_block(p, token, symbol=sym, with_health=False)
                   + '</div>')
    # health-check — один на весь портфель (без кнопок close/void: они уже
    # есть у каждой карточки, а дублировать их в конце непонятно к чему)
    out.append("<div class='acts'>"
               + action_form("healthcheck", token) + "</div>")
    return "".join(out)


def bot_block(bot: str | None, token: str, mode: str = "",
              n_open: int = 0) -> str:
    """Состояние сервиса бота + кнопки запуска/остановки/перезапуска.

    bot=None — блок не рисуется (статичный --out и тесты без systemd).
    """
    if not token or bot is None:
        return ""
    active = bot in ("active", "activating", "reloading")
    # Статус неизвестен (нет systemd/сервиса) — start/stop выключены: панель не
    # должна делать вид, что знает состояние, и перезапускать наугад.
    known = bot in ("active", "inactive", "activating", "reloading",
                    "deactivating", "failed")
    badge = {"active": "🟢 работает", "inactive": "⚪ остановлен",
             "failed": "🔴 упал", "activating": "🟡 запускается",
             "deactivating": "🟡 останавливается"}.get(bot, f"❔ {bot}")
    btns = "".join([action_form("bot-start", token,
                                 disabled=active or not known),
                    action_form("bot-stop", token,
                                disabled=(not active) or not known),
                    action_form("bot-restart", token, disabled=not known)])
    # стоп-всё / пуск-всё — отдельной строкой: другая кнопка (остановить
    # бота) позицию НЕ закрывает, их легко перепутать. Включены всегда, кроме
    # статичной панели: при «н/д» пользователь вправе захотеть аварийный
    # выход, а «пуск-всё» ещё и проверяет, что портфель пуст.
    stop_all = action_form("stop-all", token)
    start_all = action_form("start-all", token)
    warn = (f" ({n_open} пар(ы) будут закрыты по рынку)" if n_open else
            " (открытых пар нет — остановит только бота)")
    return (f"<div class='acts'>{btns}</div>"
            f"<div class='acts stopall'>{start_all}{stop_all}</div>"
            f"<div class='sub'>Сервис <code>{html_escape(BOT_SERVICE)}</code> — "
            f"сейчас {badge}. Кнопки выполняют systemctl на сервере; запуск "
            f"отдельным процессом не нужен — бот живёт как сервис."
            f"{mainnet_note(mode)}</div>"
            f"<div class='sub'>🚨 <b>СТОП-ВСЁ</b>{warn}: сначала останавливает "
            f"сервис бота (иначе авто-вход вошёл бы взамен закрытой пары), "
            f"затем закрывает все пары и сверяет результат — в отчёте виден "
            f"итог каждого шага.</div>"
            f"<div class='sub'>▶️ <b>ПУСК-ВСЁ</b> — обратное действие: "
            f"запускает сервис и включает автовход, но только если портфель "
            f"ПУСТ; потом проверяет, что сервис действительно active и что "
            f"автовход не выключен (--no-auto, no-auto в config.json, "
            f"mainnet без --confirm-live).</div>")


def mainnet_note(mode: str) -> str:
    """Явное предупреждение в UI, когда вход пойдёт по реальным деньгам."""
    if str(mode or "").lower() != "mainnet":
        return ""
    return (" <b class='warn'>Режим mainnet: вход/закрытие — реальные "
            "деньги.</b>")


def entry_block(positions: list[dict] | None, token: str, mode: str = "",
                has_password: bool = True) -> str:
    """Форма ручного входа в позицию (POST /action, name=enter).

    Требует: пароль панели задан, в портфеле есть свободный слот, символ
    введён дважды, для mainnet — отметка о реальных деньгах.
    """
    if not token:
        return ""
    # вызывающие передают список позиций портфеля; одиночная позиция и None
    # (старая сигнатура) тоже принимаются — панель обязана рисоваться всегда
    if isinstance(positions, dict):
        positions = [positions]
    positions = list(positions or [])
    if not has_password:
        return ("<div class='sub'>Ручной вход и управление сервисом "
                "доступны только при заданном DASHBOARD_PASSWORD — без "
                "пароля эти действия запрещены.</div>")
    max_pos = max(1, int(load_json(CONFIG_FILE, {}).get("max-positions", 1) or 1))
    no_pos = len(positions) < max_pos
    mainnet = str(mode or "").lower() == "mainnet"
    ack = ("<label class='chk'><input type='checkbox' name='ack' value='1' "
           "required>понимаю, что это РЕАЛЬНЫЕ деньги (mainnet)</label>"
           if mainnet else "")
    dis = "" if no_pos else " disabled"
    hint = ("Бот ищет окно сам — ручной вход нужен, когда хочешь открыть "
            "конкретный символ сейчас. Открывайте при остановленном боте, "
            "чтобы он не вошёл параллельно." if no_pos else
            f"Свободных слотов нет: занято {len(positions)} из {max_pos} "
            f"(max-positions в config.json) — сначала закройте пару")
    return (
        "<form class='act entry' method='post' action='/action'"
        f" onsubmit=\"return confirm('Открыть позицию по рынку? Действие ' +"
        f" 'потратит деньги со счёта.')\">"
        f"<input type='hidden' name='token' value='{html_escape(token)}'>"
        "<input type='hidden' name='name' value='enter'>"
        f"<input name='symbol' placeholder='символ, напр. BTCUSDT'"
        f" autocapitalize='characters'{dis}>"
        f"<input name='confirm' placeholder='повторите символ'{dis}>"
        "<label class='chk'><input type='checkbox' name='force' value='1'"
        f"{dis}>войти даже если символ не проходит фильтр"
        "</label>"
        f"{ack}"
        f"<button class='btn enter'{dis}>Войти в позицию по рынку</button>"
        f"</form>"
        f"<div class='sub'>{hint} Инструмент ставит reduce-only перп + "
        f"покупку спота на капитал пары (capital из config.json, в портфеле "
        f"урезается по свободной марже)."
        f"{mainnet_note(mode)}</div>")


def result_page(name: str, res: dict) -> str:
    """Страница результата действия: статус + полный вывод команды."""
    label = ACTION_LABEL.get(name, name)
    ok = bool(res.get("ok"))
    cls_ = "up" if ok else "down"
    status = "✅ выполнено" if ok else "⚠️ ошибка"
    out = html_escape(res.get("output") or "(пустой вывод)")
    return f'''<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Действие: {html_escape(label)}</title>
<style>
body{{margin:0;background:#0b0e14;color:#d6dae4;
font:15px/1.5 "Segoe UI",system-ui,sans-serif;padding:26px}}
h1{{font-size:20px;margin:0 0 6px}}
.up{{color:#26a69a}} .down{{color:#ef5350}}
pre{{background:#121722;border:1px solid #232a3b;border-radius:10px;
padding:16px;overflow-x:auto;font:13px/1.6 Consolas,monospace;
white-space:pre-wrap;word-break:break-word}}
a{{color:#4aa3ff;text-decoration:none}} a:hover{{text-decoration:underline}}
</style></head><body>
<h1>{html_escape(label)}</h1>
<div class="{cls_}">{status} · код возврата {res.get('rc')}</div>
<pre>{out}</pre>
<p><a href="/">← назад на панель</a></p>
</body></html>'''


# ------------------------------------------------------------------ render
def render(refresh: int, token: str = "", bot: str | None = None) -> str:
    state = load_json(POSITION_FILE, {"position": None, "history": []})
    positions = positions_of(state)
    pos = positions[0] if positions else None
    # Физически невозможные закрытия (нулевое удержание, ноль выплат funding,
    # а результат есть) в метрики НЕ идут: на VPS такие 19 записей ETHUSDT
    # давали +$101.86 «заработка», которого не было. Исключение показывается
    # явной плашкой — молча выкидывать цифры из PnL нельзя.
    trades, suspicious = S.partition(closed_trades(state))

    # живой PnL — по КАЖДОЙ паре портфеля (в KPI идёт сумма)
    perps: list = []
    lives: list = []
    for p in positions:
        pn, lv = live_prices(p)
        perps.append(pn)
        lives.append(lv)
    perp_now = perps[0] if perps else None
    live = (sum(l for l in lives if l is not None)
            if any(l is not None for l in lives) else None)
    core = M.core_metrics(trades)
    econ = M.economics(trades)
    equity = M.equity_curve(trades)
    dd = M.drawdown_series(equity)
    health = M.bot_health(EVENTS_FILE, state)
    bot_mode = cfg_mode()

    now = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")
    meta = (f'<meta http-equiv="refresh" content="{refresh}">' if refresh > 0 else "")

    bars = svg_trade_bars([t["total"] for t in trades])
    return f'''<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">{meta}
<title>Funding Terminal — метрики торговли</title>
<style>
:root{{--bg:#0a0d13;--panel:#121722;--panel2:#171d2b;--line:#232a3b;
--tx:#d6dae4;--mut:#7a8194;--up:#26a69a;--down:#ef5350;--acc:#4aa3ff;
--warn:#e0a33e}}
*{{box-sizing:border-box}}
body{{margin:0;padding:24px;
background:radial-gradient(1200px 600px at 20% -10%,#131b2c 0%,#0a0d13 60%);
color:var(--tx);font:15px/1.5 "Segoe UI",system-ui,-apple-system,sans-serif}}
h1{{font-size:21px;margin:0 0 3px;letter-spacing:.3px}}
h1 span{{color:var(--acc)}}
.head{{display:flex;justify-content:space-between;align-items:flex-end;
flex-wrap:wrap;gap:12px;margin-bottom:16px}}
.sub{{color:var(--mut);font-size:12.5px;line-height:1.6}}
.muted{{color:var(--mut)}}
.up{{color:var(--up)}} .down{{color:var(--down)}}
.warn-line{{padding:9px 14px;border-radius:9px;font-size:13.5px;
margin-bottom:16px;border:1px solid}}
.warn-line.ok{{background:#26a69a12;border-color:#26a69a40;color:#7fd8cd}}
.warn-line.bad{{background:#ef535012;border-color:#ef535055;color:#ff8a85}}
.warn-line.neutral{{background:#ffffff06;border-color:var(--line);color:var(--mut)}}
.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(178px,1fr));
gap:12px;margin-bottom:18px}}
.card{{background:linear-gradient(180deg,var(--panel) 0%,#0f1420 100%);
border:1px solid var(--line);border-radius:11px;padding:13px 15px;
box-shadow:0 4px 14px rgba(0,0,0,.3)}}
.label{{color:var(--mut);font-size:10.5px;text-transform:uppercase;
letter-spacing:.7px;font-weight:600}}
.value{{font-size:23px;font-weight:650;margin-top:5px;
font-variant-numeric:tabular-nums;letter-spacing:-.3px}}
section{{background:var(--panel);border:1px solid var(--line);border-radius:11px;
padding:16px 18px;margin-bottom:16px;box-shadow:0 4px 14px rgba(0,0,0,.3)}}
section h2{{font-size:13px;margin:0 0 12px;color:#e8ecf4;
text-transform:uppercase;letter-spacing:.9px;font-weight:650}}
section h2 .hint{{color:var(--mut);font-weight:400;text-transform:none;
letter-spacing:0;font-size:12px;margin-left:8px}}
.chart{{width:100%;height:auto;display:block}}
.grid{{stroke:#1a2030;stroke-width:1}}
.zero{{stroke:#39415a;stroke-width:1;stroke-dasharray:4 4}}
.axis{{fill:var(--mut);font-size:11px;font-family:Consolas,monospace}}
.dd-dot{{fill:#ef5350}}
table.trades{{width:100%;border-collapse:collapse;font-size:13.5px}}
table.trades th{{color:var(--mut);font-weight:600;font-size:11px;text-align:left;
padding:7px 9px;border-bottom:1px solid var(--line);
text-transform:uppercase;letter-spacing:.5px;white-space:nowrap}}
td{{padding:7px 9px;border-bottom:1px solid #171d2a;
font-variant-numeric:tabular-nums;white-space:nowrap}}
tbody tr:hover{{background:#171d2c}}
.badge{{font-size:10.5px;padding:2px 7px;border-radius:20px;
border:1px solid var(--line);color:var(--mut);white-space:nowrap}}
.badge.paper{{border-color:#4aa3ff55;color:#4aa3ff}}
.badge.testnet{{border-color:#c9a22755;color:#e0c04a}}
.badge.mainnet{{border-color:#ef535055;color:#ef5350}}
.badge.ok{{border-color:#26a69a66;color:var(--up)}}
.badge.no{{border-color:#ef535055;color:var(--down)}}
.two{{display:grid;grid-template-columns:1.35fr 1fr;gap:16px}}
.three{{display:grid;grid-template-columns:repeat(3,1fr);gap:16px}}
@media(max-width:1000px){{.two,.three{{grid-template-columns:1fr}}}}
.empty{{color:var(--mut);padding:12px 2px;font-size:13.5px}}
.events{{font:12.5px/1.65 Consolas,monospace;max-height:320px;overflow-y:auto}}
.ev{{padding:2px 7px;border-left:3px solid #2a3245;white-space:nowrap;
overflow:hidden;text-overflow:ellipsis}}
.ev-ts{{color:var(--mut);margin-right:9px}}
.ev-in{{border-left-color:var(--acc);background:#4aa3ff0d}}
.ev-warn{{border-left-color:var(--down);background:#ef53500d}}
.ev-up{{border-left-color:var(--up);background:#26a69a0d}}
.pos-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(112px,1fr));
gap:9px;margin-bottom:12px}}
.pos-grid .label{{display:block;font-size:10px;margin-bottom:2px}}
.pos-grid b{{font-size:14px;font-variant-numeric:tabular-nums}}
.pos-live{{text-align:center;padding:10px 0;border-top:1px solid var(--line)}}
.pos-live .value{{font-size:30px}}
.pos-card{{border:1px solid var(--line);border-radius:12px;padding:14px;
background:var(--panel2);margin-bottom:14px}}
.pos-head{{display:flex;align-items:center;gap:9px;margin-bottom:10px;
font-size:15px}}
.pos-card .pos-live .value{{font-size:24px}}
.risk{{margin-top:12px;padding-top:12px;border-top:1px solid var(--line)}}
.risk-head{{display:flex;justify-content:space-between;
font-size:12.5px;color:var(--mut)}}
.risk-head b{{font-size:14px;font-variant-numeric:tabular-nums}}
.risk-head b.warn{{color:var(--warn)}}
.meter{{height:7px;background:#1c2231;border-radius:4px;overflow:hidden;
margin:7px 0 6px}}
.meter-fill{{height:100%;border-radius:4px;transition:width .3s}}
.meter-fill.ok{{background:var(--up)}}
.meter-fill.warn{{background:var(--warn)}}
.meter-fill.down{{background:var(--down)}}
.attr{{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));
gap:10px;margin-bottom:10px}}
.attr-row{{display:flex;justify-content:space-between;align-items:baseline;
gap:8px;padding:7px 11px;background:var(--panel2);border-radius:8px;
border:1px solid var(--line)}}
.attr-lab{{color:var(--mut);font-size:11.5px}}
.attr-val{{font-size:15px;font-weight:600;font-variant-numeric:tabular-nums}}
.attr-svg{{height:46px}}
.hbar{{background:#1a2030;border-radius:3px;height:9px;min-width:60px;
overflow:hidden}}
.hbar-fill{{height:100%;border-radius:3px}}
.hbar-fill.up{{background:var(--up)}}
.hbar-fill.down{{background:var(--down)}}
code{{background:#1a2030;padding:2px 6px;border-radius:5px;color:#9ecbff;
font-size:12.5px}}
.acts{{display:flex;gap:10px;flex-wrap:wrap;margin:14px 0 8px}}
.act{{margin:0}}
.btn{{font:inherit;font-size:13px;padding:8px 14px;border-radius:8px;
cursor:pointer;border:1px solid var(--line);background:#1a2030;color:var(--tx)}}
.btn:hover:not(:disabled){{background:#222a3d;border-color:#3a4152}}
.btn:disabled{{opacity:.45;cursor:not-allowed}}
.btn.close{{border-color:#ef535055;color:#ef8f8c}}
.btn.void{{border-color:#c9a22755;color:#c9a227}}
.btn.healthcheck{{border-color:#4aa3ff55;color:#7fc0ff}}
.btn.stop-all{{border-color:#ef5350;color:#ff8f8c;background:#ef535015;
font-weight:600}}
.btn.start-all{{border-color:#26a69a;color:#7fd7cc;background:#26a69a15;
font-weight:600}}
.acts.stopall{{margin-top:8px;padding-top:8px;border-top:1px dashed #ef535055;
display:flex;gap:6px;flex-wrap:wrap}}
footer{{color:var(--mut);font-size:12px;text-align:center;margin-top:8px;
padding-top:12px;border-top:1px solid var(--line)}}
</style></head><body>

<div class="head">
  <div>
    <h1>📊 Funding <span>Terminal</span></h1>
    <div class="sub">дельта-нейтральная пара long spot + short perp · Bybit ·
      метрики по фактическим закрытиям, не по прогнозу</div>
  </div>
  <div class="sub">обновлено {now} ·
    {'авто-обновление каждые ' + str(refresh) + ' с' if refresh > 0 else 'статичная версия'}
  </div>
</div>

{health_bar(health)}

{sanity_bar(suspicious)}

{kpi_row(core, econ, pos, live)}

<section><h2>Кривая капитала и просадка
  <span class="hint">просадка от локального максимума — реальный риск, а не итог</span>
</h2>{svg_equity(equity, dd)}</section>

<section><h2>PnL по сделкам
  <span class="hint">видны серии убытков и разброс результатов</span>
</h2>{bars}</section>

<div class="two">
<section><h2>Откуда пришли деньги
  <span class="hint">цена + funding − комиссии</span></h2>
{attribution_bar(econ)}</section>

<section><h2>Здоровье торговли</h2>
<div class="attr">
  <div class="attr-row"><span class="attr-lab">Profit factor</span>
    <span class="attr-val">{ratio(core.get('profit_factor', 0))}×</span></div>
  <div class="attr-row"><span class="attr-lab">Ожидание/сделка</span>
    <span class="attr-val {cls(core.get('expectancy', 0))}">
      {money(core.get('expectancy', 0))}</span></div>
  <div class="attr-row"><span class="attr-lab">Средняя победа</span>
    <span class="attr-val up">{money(core.get('avg_win', 0))}</span></div>
  <div class="attr-row"><span class="attr-lab">Средний убыток</span>
    <span class="attr-val down">{money(-core.get('avg_loss', 0))}</span></div>
  <div class="attr-row"><span class="attr-lab">Худшая / лучшая</span>
    <span class="attr-val">{money(core.get('worst', 0))} / {money(core.get('best', 0))}</span></div>
  <div class="attr-row"><span class="attr-lab">Recovery factor</span>
    <span class="attr-val">{ratio(core.get('recovery', 0))}×</span></div>
</div>
<div class="sub">Profit factor &gt; 1 — стратегия зарабатывает больше, чем
теряет. Ожидание — сколько в среднем приносит одна сделка (с комиссиями).
Recovery factor — заработок на единицу максимальной просадки.</div>
</section>
</div>

<section><h2>Текущая позиция</h2>{portfolio_block(positions, lives, perps, token)}
</section>

<section><h2>Управление ботом
  <span class="hint">сервис и ручной вход — только при заданном пароле</span>
</h2>
{bot_block(bot, token, bot_mode, len(positions))}
{entry_block(positions, token, bot_mode, bool(PASSWORD))}</section>

<div class="two">
<section><h2>Сделки по символам</h2>{symbol_table(trades)}</section>
<section><h2>По времени удержания</h2>{hold_table(trades)}</section>
</div>

<section><h2>История сделок
  <span class="hint">«PnL цена» ≈ 0 подтверждает нейтральность позиции</span>
</h2>{trades_table(trades)}</section>

<div class="two">
<section><h2>Рынок (снимок монитора)</h2>{market_block()}</section>
<section><h2>События бота</h2>{events_block()}</section>
</div>

<section><h2>Память бота (самообучение)</h2>{memory_block()}</section>

<footer>funding_executor + funding_monitor + dashboard_metrics ·
не является инвестиционной рекомендацией</footer>
</body></html>'''


# ------------------------------------------------------------------ server
def make_handler(refresh: int, token: str, password: str = ""):
    """Фабрика HTTP-обработчика панели (вынесена из main ради тестов).
    refresh — период автообновления в HTML, token — токен кнопок
    (пустой = кнопок нет и действия запрещены), password — пароль входа
    (пустой = вход не требуется, режим localhost)."""

    def authed(headers) -> bool:
        """Пустой пароль — вход не нужен (loopback); иначе нужна сессия."""
        return not password or session_ok(headers.get("Cookie", ""))

    class Handler(http.server.BaseHTTPRequestHandler):
        def _send(self, code: int, body: bytes,
                  ctype: str = "text/html; charset=utf-8"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store, max-age=0")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _err(self, code: int, text: str):
            self._send(code, f"<h1>{code}</h1><p>{text}</p>".encode())

        def _redirect(self, location: str, cookie: str | None = None):
            """303 — после POST; cookie задаётся только при успешном входе."""
            self.send_response(303)
            self.send_header("Location", location)
            self.send_header("Cache-Control", "no-store")
            if cookie:
                self.send_header("Set-Cookie", cookie)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _drain(self, length: int, cap: int = 65536):
            """Прочитать и выбросить тело запроса (до предела).

            BaseHTTPRequestHandler, отвечая, не читает тело и закрывает
            соединение, а клиент в этот момент ещё пишет в сокет — он
            получает RST вместо нашего кода ошибки (на Windows это
            ConnectionAbortedError). Поэтому тело съедаем ДО отказов.
            """
            left = min(max(length, 0), cap)
            while left > 0:
                chunk = self.rfile.read(min(left, 8192))
                if not chunk:
                    break
                left -= len(chunk)

        def do_GET(self):  # noqa: N802
            path = urlparse(self.path).path
            if path == "/login":
                if authed(self.headers):
                    self._redirect("/")
                else:
                    self._send(200, login_page().encode())
                return
            if path == "/logout":
                drop_session(self.headers.get("Cookie", ""))
                self._redirect("/login", cookie=f"{SESSION_COOKIE}=; Path=/; "
                                                "HttpOnly; SameSite=Lax; "
                                                "Max-Age=0")
                return
            if path not in ("/", "/index.html"):
                self._err(404, "не найдено")
                return
            if not authed(self.headers):
                self._redirect("/login")
                return
            try:
                doc = render(refresh, token, bot=bot_status()).encode()
            except Exception as e:  # noqa: BLE001
                # лучше отдать страницу с ошибкой, чем оборванное соединение
                self._send(500, (f"<!doctype html><meta charset=utf-8>"
                                 f"<body style='background:#0a0d13;"
                                 f"color:#ef5350;font:15px system-ui;"
                                 f"padding:30px'>"
                                 f"<h2>Панель не собралась</h2>"
                                 f"<pre>{html_escape(e)}</pre>").encode())
                return
            self._send(200, doc)

        def do_POST(self):  # noqa: N802
            # тело читаем (или выбрасываем) ПЕРВЫМ делом: любой отказ ниже
            # должен доехать до клиента как код, а не как обрыв соединения
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = -1
            body = b""
            if 0 <= length <= 4096:
                body = self.rfile.read(length)
            elif length > 4096:
                self._drain(length)

            path = urlparse(self.path).path
            if length < 0:
                self._err(400, "некорректный Content-Length")
                return
            if length > 4096:
                self._err(413, "слишком большое тело запроса")
                return
            form = parse_qs(body.decode("utf-8", "replace"))

            if path == "/login":
                self._login(form)
                return
            if path != "/action":
                self._err(404, "не найдено")
                return
            if not _origin_ok(self.headers.get("Origin", ""),
                              self.headers.get("Host", "")):
                self._err(403, "чужой Origin")
                return
            if not authed(self.headers):
                self._err(403, "нужен вход: /login")
                return
            if not token_ok((form.get("token") or [""])[0], token):
                self._err(403, "нет доступа: неверный токен")
                return
            name = (form.get("name") or [""])[0]
            if name not in ACTIONS:
                self._err(400, "неизвестное действие")
                return
            params = {k: (v or [""])[0] for k, v in form.items()}
            # Опасные действия — только на панели с паролем (fail closed):
            # вход в панель + токен кнопок + подтверждение в форме
            if name in DANGEROUS and not password:
                self._err(403, "опасное действие запрещено: задайте "
                               "DASHBOARD_PASSWORD и войдите в панель")
                return
            if name == "enter":
                state = load_json(POSITION_FILE, {"position": None,
                                                 "history": []})
                mode = cfg_mode() or str(
                    ((state.get("position") or {}).get("mode") or ""))
                err = validate_entry(state, params, mode)
                if err:
                    self._err(400, err)
                    return
            if not _ACTION_LOCK.acquire(blocking=False):
                self._err(409, "другое действие уже выполняется")
                return
            try:
                # стоп-всё / пуск-всё — составные действия (сервис + портфель):
                # своё исполнение с отчётом по шагам, а не одна команда
                composite = {"stop-all": run_stop_all,
                             "start-all": run_start_all}
                fn = composite.get(name)
                res = fn(params) if fn else run_action(name, params)
            finally:
                _ACTION_LOCK.release()
            self._send(200, result_page(name, res).encode())

        def _login(self, form: dict):
            """POST /login: проверка пароля, защита от перебора, сессия."""
            ip = (self.client_address or ("",))[0]
            wait = login_blocked(ip)
            if wait > 0:
                self._send(429, login_page(
                    f"слишком много попыток, подождите {int(wait) + 1} с").encode())
                return
            if not password:
                self._err(403, "вход не настроен: задайте DASHBOARD_PASSWORD")
                return
            if not password_ok((form.get("password") or [""])[0], password):
                note_login_failure(ip)
                self._send(401, login_page("неверный пароль").encode())
                return
            note_login_success(ip)
            tok = new_session()
            self._redirect("/", cookie=f"{SESSION_COOKIE}={tok}; Path=/; "
                                       f"HttpOnly; SameSite=Lax; "
                                       f"Max-Age={SESSION_TTL}")

        def log_message(self, *a):  # тихий режим: токен/пароль в лог не попадают
            pass

    return Handler


# ------------------------------------------------------------------ main
def main():
    p = argparse.ArgumentParser(description="HTML-панель торговли и метрик")
    p.add_argument("--out", default=os.path.join(BASE, "dashboard.html"))
    p.add_argument("--refresh", type=int, default=60,
                   help="период автообновления, сек (0 = выкл)")
    p.add_argument("--serve", type=int, default=None, metavar="PORT",
                   help="запустить HTTP-сервер на порту")
    p.add_argument("--host", default="127.0.0.1",
                   help="адрес прослушивания сервера (default 127.0.0.1 — "
                        "только локально; 0.0.0.0 — доступ по IP, требуется "
                        "пароль)")
    p.add_argument("--password", default=None,
                   help="пароль входа на панель (default: env "
                        "DASHBOARD_PASSWORD). Обязателен для --host вне "
                        "localhost")
    p.add_argument("--token", default=None,
                   help="токен для управляющих кнопок (default: env "
                        "DASHBOARD_TOKEN, иначе сгенерируется случайный)")
    p.add_argument("--no-live", action="store_true",
                   help="не ходить в API за живым PnL")
    p.add_argument("--data-dir", default=None,
                   help="откуда читать position.json/memory.json/bot.log "
                        "(по умолчанию — папка панели). Для тестов и отладки")
    args = p.parse_args()

    if args.data_dir:
        # пути вычисляются на импорте, поэтому переопределяем явно
        global POSITION_FILE, SNAPSHOT_FILE, MEMORY_FILE, EVENTS_FILE
        dd = args.data_dir
        POSITION_FILE = os.path.join(dd, "position.json")
        SNAPSHOT_FILE = os.path.join(dd, "snapshot.json")
        MEMORY_FILE = os.path.join(dd, "memory.json")
        EVENTS_FILE = os.path.join(dd, "bot.log")

    if args.no_live:
        global LIVE
        LIVE = False

    global TOKEN, PASSWORD
    if args.token:
        TOKEN = args.token
    if args.password:
        PASSWORD = args.password.strip()

    # Публичный bind без пароля — отказ на старте. Панель показывает
    # позицию/PnL и умеет закрывать позицию: «просто открыть порт» нельзя.
    public_bind = args.host not in ("127.0.0.1", "localhost", "::1")
    if args.serve and public_bind and not PASSWORD:
        sys.exit(f"Панель на {args.host}:{args.serve} доступна из сети, поэтому "
                 "нужен пароль: задайте DASHBOARD_PASSWORD в .env (или "
                 "--password). Без пароля используйте SSH-туннель.")

    # Кнопки требуют токен. Если его нет — генерируем и показываем один раз
    # в консоли. Секрет живёт только в памяти/ответе сервера: в файл
    # dashboard.html (и, значит, на диск) токен не пишется.
    generated = False
    if args.serve and not TOKEN:
        TOKEN = secrets.token_urlsafe(18)
        generated = True

    html_doc = render(args.refresh, "")
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(html_doc)
    print(f"Панель собрана: {args.out} ({len(html_doc)} байт)")

    if args.serve:
        Handler = make_handler(args.refresh, TOKEN, PASSWORD)

        if public_bind:
            print(f"Сервер слушает {args.host}:{args.serve} — доступ по "
                  f"http://<IP сервера>:{args.serve}")
            print("Вход по паролю: DASHBOARD_PASSWORD. Выход: /logout")
        srv = http.server.ThreadingHTTPServer((args.host, args.serve), Handler)
        print(f"Сервер: http://{args.host}:{args.serve}  (Ctrl+C — стоп)")
        if generated:
            print(f"Токен действий (сгенерирован): {TOKEN}")
            print("Передай его в DASHBOARD_TOKEN или --token, чтобы он не "
                  "менялся при перезапуске.")
        elif not TOKEN:
            print("Токен не задан — управляющие кнопки выключены.")
        if not public_bind:
            # пользователь — admin: root-логин по SSH на сервере выключен
            print(f"С VPS/удалённо: ssh -L {args.serve}:127.0.0.1:{args.serve} "
                  "admin@host  →  http://127.0.0.1:%d" % args.serve)
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            print("\nОстановлено.")


if __name__ == "__main__":
    main()