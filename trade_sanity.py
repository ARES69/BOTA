"""Санитария сделок: пометка физически невозможных результатов.

Зачем это нужно
===============
Экономика дельта-нейтральной пары long spot + short perp складывается ровно
из трёх слагаемых:

    итог = funding + (базис_вход − базис_выход) − комиссии круга − проскальзывание

Каждое из них ограничено и известно заранее:
  • funding приходит РАЗ В ИНТЕРВАЛ ставки (8 ч у BTC/ETH) — первой выплаты
    раньше интервала не бывает вообще;
  • базис ходит на десятые доли процента (фильтр берёт кандидатов с |базис| < 0.5%);
  • комиссия круга (4 ордера: вход+выход по двум ногам) — РАСХОД 0.3–0.5% нотиона.

Поэтому закрытие, у которого ноль удержания (меньше интервала выплат), ноль
полученного funding и при этом заметный ПЛЮС, — не удачная сделка, а дефект
записи: цена ноги записана из чужого/устаревшего снимка, нога закрыта не нами,
сделка восстановлена из лога с ошибкой. Живой пример с VPS: 19 записей ETHUSDT
testnet с held=0.0 ч, funding=$0 и «прибылью» +1.15…+7.4% капитала (итого
+$101.86) при заявленном проскальзывании входа 190–409 б.п. Прибыль такой
записи в точности равна аномалии цены входа — это бухгалтерия битых цен, а не
экономика пары. Попади такое в PnL и в память — бот учился бы на числах,
которых не было, а панель показывала бы выдуманный заработок.

Правило (консервативное — ловим только очевидное)
=================================================
Запись помечается подозрительной, когда держатся ВСЕ условия:
  1) удержание < NO_PAYOUT_H (0.5 ч) — меньше любого интервала выплат
     (та же граница, что MIN_HOLD_H памяти: «funding физически не платился»);
  2) funding ≤ NO_PAYOUT_FUNDING_EPS — выплат не было вовсе
     (поле отсутствует → считаем «неизвестно» и НЕ помечаем);
  3) результат ≥ GAIN_LIMIT_PCT % базы (или ≤ −LOSS_LIMIT_PCT: зеркальный
     дефект с убытком тоже ломает память — он бы построил ложный стоп-лист).
База — нотион, иначе капитал, иначе BASE_FALLBACK ($600, как в памяти).
Порог по прибыли намеренно ниже цены круга в комиссиях: чтобы получить плюс
при нулевом удержании и без выплат, источник дохода должен взяться из ниоткуда.

Что делает пометка
==================
  • панель (dashboard.py) не считает такие записи в PnL, винрейте, profit
    factor, кривой капитала, разбивках по символам и времени удержания;
  • память (bybit_memory.py) не учитывает их в статистике символов,
    калибровке, автотюнинге, сравнении maker/taker и не строит по ним
    стоп-листы (ложный «убыток» не должен банить живой символ);
  • запись остаётся в position.json/memory.json с полем "suspicious": причина,
    потому что разбор аномалии важнее тихого удаления — по ней видно, что
    сломалось. Уберите поле, когда данные исправлены.

Ничего не удаляется и не «подчищается» молча: пометка — фильтр, а не удаление.
Разовые действия по уже накопленным записям: `funding_executor.py sanity`.

Только stdlib.
"""
from __future__ import annotations

import time

SUSPICIOUS = "suspicious"      # имя поля-пометки в записи сделки

# --- границы правила --------------------------------------------------------
NO_PAYOUT_H = 0.5              # ч: короче — funding выплатиться не успевал
NO_PAYOUT_FUNDING_EPS = 0.01   # $: «выплат не было» (шум округления центов)
GAIN_LIMIT_PCT = 0.5           # % базы: плюс при нулевом удержании без выплат
LOSS_LIMIT_PCT = 5.0           # % базы: зеркальный дефект (битые цены ног)
BASE_FALLBACK = 600.0          # $: база, когда ни нотион, ни капитал неизвестны
SLIP_BPS_LIMIT = 200.0         # б.п.: цена ноги ушла от рынка заметнее нормы
SLIP_H = 0.3                   # ч: закрытие почти мгновенное (было 0.5 в правиле
                               #   выше — здесь порог ниже, потому что само по себе
                               #   «слип > 200 б.п.» уже нечестная запись цены)


def _f(x, default: float = 0.0) -> float:
    try:
        if x is None or x == "":
            return default
        return float(x)
    except (TypeError, ValueError):
        return default


def held_hours(rec: dict) -> float:
    """Удержание в часах: position.json пишет held_hours, память — held_h."""
    h = rec.get("held_hours")
    if h is None:
        h = rec.get("held_h")
    return _f(h)


def funding_paid(rec: dict) -> float | None:
    """Полученный funding, $; None — поле отсутствует (неизвестно, не 0)."""
    if "funding" not in rec or rec.get("funding") is None:
        return None
    return _f(rec.get("funding"))


def base_notional(rec: dict) -> float:
    """База для процентов — размер позиции, к которому относятся проценты.

    Порядок источников:
      1) notional — размер риска (capital × плечо), к нему считаются комиссии;
      2) capital — когда нотиона в записи нет (старые версии исполнителя);
      3) обратный счёт из записанного процента: запись памяти хранит pnl_pct
         (от капитала), а position.json — return_pct. Без этого шага база
         памяти падала бы до BASE_FALLBACK ($600), и «нереальные» +$2.30
         (+1.15% от капитала $200) проходили бы под порогом;
      4) BASE_FALLBACK — когда нет ничего (как в notional_guess памяти)."""
    n = _f(rec.get("notional"))
    if n > 0:
        return n
    c = _f(rec.get("capital"))
    if c > 0:
        return c
    total = _f(rec.get("total"))
    for key in ("pnl_pct", "return_pct"):
        pct = _f(rec.get(key))
        if pct != 0 and total != 0:
            return abs(total * 100.0 / pct)
    return BASE_FALLBACK


def result_pct(rec: dict) -> float:
    """Результат сделки в % базы (нотиона/капитала)."""
    base = base_notional(rec)
    return _f(rec.get("total")) / base * 100.0 if base > 0 else 0.0


def slip_note(rec: dict) -> str:
    """Короткая приписка о записи цены ноги: битый вход виден сразу.

    phantom-сделки 09.10 шли со slip_bps 190–409 — прибыль такой записи
    в точности равна аномалии цены. Без этого числа в причине пришлось бы
    лезть в position.json, чтобы понять, чем именно запись отличается от
    честной сделки."""
    sb = rec.get("slip_bps")
    if sb is None or sb == "":
        return ""
    try:
        sb = float(sb)
    except (TypeError, ValueError):
        return ""
    return f", слippедж входа {sb:g} б.п."


def slip_reason(rec: dict) -> str | None:
    """Ingest-пометка: цена ноги записана со слипом крупнее SLIP_BPS_LIMIT.

    Read-time правило выше ловит следствие (доход без источника); эта функция
    ловит причину — записанную цену, ушедшую от рынка на сотни б.п. Фантомы
    09.10 шли со slip 190–409 б.п.: прибыль такой записи в точности равна
    этой аномалии. Вызывается в момент закрытия (funding_executor), чтобы
    пометка появилась сразу, а не при следующем чтении файла.

    Консервативно: крупный слippедж сам по себе не приговор (бывает редкий
    тонкий стакан), поэтому нужны ещё короткое удержание и отсутствие выплат.
    Помеченную запись не трогаем (причина фиксируется один раз)."""
    if rec.get(SUSPICIOUS) or rec.get("total") is None:
        return None
    sb = _f(rec.get("slip_bps"), -1.0)
    if sb < SLIP_BPS_LIMIT:
        return None
    held = held_hours(rec)
    if held >= SLIP_H:
        return None
    fund = funding_paid(rec)
    if fund is None or fund > NO_PAYOUT_FUNDING_EPS:
        return None                     # выплата была — цена могла быть честной
    return (f"слippедж входа {sb:g} б.п. при удержании {held:.2f} ч без выплат "
            f"funding: цена ноги записана не от рынка (доходности взяться "
            f"неоткуда)")


def reason(rec: dict) -> str | None:
    """Почему запись физически невозможна, или None (правдоподобная).

    Пустое удержание + отсутствие выплат + результат, которому неоткуда
    взяться, → дефект. Оба знака: плюс («доход без источника») и слишком
    крупный минус (битые цены ног → ложная серия убытков → стоп-лист)."""
    if rec.get("total") is None:
        return None
    held = held_hours(rec)
    if held >= NO_PAYOUT_H:
        return None
    fund = funding_paid(rec)
    if fund is None or fund > NO_PAYOUT_FUNDING_EPS:
        return None                     # выплата была — источник дохода есть
    pct = result_pct(rec)
    if pct >= GAIN_LIMIT_PCT:
        return (f"доход без источника: удержание {held:.2f} ч (выплат funding "
                f"не было), а результат {pct:+.2f}% нотиона"
                f"{slip_note(rec)}")
    if pct <= -LOSS_LIMIT_PCT:
        return (f"убыток без причины: удержание {held:.2f} ч без выплат "
                f"funding, а результат {pct:+.2f}% нотиона — похоже на битые "
                f"цены ног, а не на рынок{slip_note(rec)}")
    return None


def note(rec: dict) -> str | None:
    """Причина пометки: сохранённая в записи или пересчитанная правилом.
    Пересчёт важен для записей, которые уже лежат в файлах без пометки:
    панель и память обязаны исключать их сразу после обновления кода,
    не дожидаясь `sanity --fix`."""
    saved = rec.get(SUSPICIOUS)
    if isinstance(saved, str) and saved:
        return saved
    if saved:                            # пометка старого формата (True/1)
        return reason(rec) or "подозрительная сделка"
    return reason(rec)


def is_suspicious(rec: dict) -> bool:
    """Исключать ли запись из PnL, статистики и обучения."""
    return note(rec) is not None


def mark(rec: dict) -> str | None:
    """Пометить запись (in place), если правило сработало. Возвращает причину.
    Уже помеченную запись не трогаем: причина фиксируется один раз — по ней
    потом видно, каким был набор данных в момент разбора."""
    if rec.get(SUSPICIOUS):
        return note(rec)
    why = reason(rec)
    if why:
        rec[SUSPICIOUS] = why
    return why


def mark_all(records: list[dict]) -> int:
    """Пометить список записей; возвращает число НОВЫХ пометок."""
    n = 0
    for r in records:
        if isinstance(r, dict) and not r.get(SUSPICIOUS) and mark(r):
            n += 1
    return n


def partition(records: list[dict]) -> tuple[list[dict], list[dict]]:
    """(правдоподобные, подозрительные) — порядок записей сохраняется."""
    clean: list[dict] = []
    susp: list[dict] = []
    for r in records:
        if isinstance(r, dict) and is_suspicious(r):
            susp.append(r)
        else:
            clean.append(r)
    return clean, susp


def summarize(records: list[dict]) -> dict:
    """Сводка по подозрительным записям: сколько, на какую сумму, где.

    Нужна в отчётах (панель, `sanity`, `stats`) — «из PnL выкинуто N сделок»
    без цифр читается как баг расчёта, а не как защита от фантомов."""
    susp = [r for r in records if isinstance(r, dict) and is_suspicious(r)]
    by_symbol: dict[str, dict] = {}
    for r in susp:
        sym = r.get("symbol") or "?"
        a = by_symbol.setdefault(sym, {"symbol": sym, "n": 0, "total": 0.0,
                                       "pct": 0.0})
        a["n"] += 1
        a["total"] += _f(r.get("total"))
        a["pct"] += result_pct(r)
    rows = sorted(by_symbol.values(), key=lambda r: -abs(r["total"]))
    for a in rows:
        a["total"] = round(a["total"], 4)
        a["pct"] = round(a["pct"], 3)
    return {"n": len(susp),
            "total": round(sum(_f(r.get("total")) for r in susp), 4),
            "no_payout_h": NO_PAYOUT_H,
            "gain_limit_pct": GAIN_LIMIT_PCT,
            "loss_limit_pct": LOSS_LIMIT_PCT,
            "by_symbol": rows,
            "first_ts": min((_f(r.get("ts")) for r in susp), default=0.0),
            "last_ts": max((_f(r.get("ts")) for r in susp), default=0.0)}


def describe(rec: dict) -> str:
    """Одна строка отчёта по подозрительной записи."""
    ts = _f(rec.get("ts"))
    when = (time.strftime("%Y-%m-%d %H:%M", time.localtime(ts / 1000))
            if ts else "—")
    return (f"{when} {rec.get('symbol', '?'):>10} {rec.get('mode', '?'):>7} "
            f"удерж {held_hours(rec):5.2f}ч funding {_f(rec.get('funding')):+8.4f}$ "
            f"итого {_f(rec.get('total')):+9.4f}$ ({result_pct(rec):+.2f}% базы)")
