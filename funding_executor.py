#!/usr/bin/env python3
"""
Funding Executor — исполнительный модуль дельта-нейтральной стратегии
=====================================================================
Открывает и закрывает пару «long spot + short perp» по правилам риск-фильтра
из funding_monitor.py. Два режима:

  paper   (по умолчанию) — симуляция на ЖИВЫХ ценах Bybit, ключи не нужны,
                           сделки фиксируются в position.json
  testnet — реальные ордера на testnet.bybit.com (игрушечные деньги),
            нужны ключи в переменных окружения BYBIT_TESTNET_API_KEY /
            BYBIT_TESTNET_API_SECRET

Правила входа (такие же, как вердикт монитора):
  • символ прошёл фильтр (✓ ГОДЕН по пресету)
  • базис ≥ 0 (перп дороже спота — шортим дорогое)
  • OI ≥ порога, окупаемость ≤ порога

Правила выхода (kill-switch):
  • funding ушёл в минус и продержался ≥ --exit-neg интервалов подряд
  • убыток позиции превысил --max-loss % от капитала
  • расхождение дельты (спот vs перп) > 1% — перебалансировка
  • hold-to-N: собрано ≥ --hold-payouts успешных выплат — фиксируем прибыль

Команды:
  python funding_executor.py scan                 # кто проходит фильтр сейчас
  python funding_executor.py enter --symbol X     # открыть пару
  python funding_executor.py status               # состояние позиции
  python funding_executor.py check                # решение по выходу
  python funding_executor.py close                # закрыть позицию
  python funding_executor.py watch --interval 60  # цикл: скан→вход→контроль

Скальпер (отдельный режим, --strategy scalp):
  python funding_executor.py watch  --strategy scalp --mode paper
  python funding_executor.py status --strategy scalp
  # перп мейджоров, свечи 1–5 мин, вход по EMA-кроссу, выход TP/SL/время.

Только stdlib, Python 3.10+.
"""

import argparse
import hashlib
import hmac
import json
import os
import random
import shutil
from decimal import ROUND_DOWN, ROUND_UP, Decimal
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

try:
    from funding_monitor import (
        DEFAULT_PERP_MAKER_FEE,
        DEFAULT_PERP_TAKER_FEE,
        DEFAULT_SPOT_FEE,
        PRESETS,
        analyze_symbol,
        api_get,
        apy_from_rate,
        conservative_apy,
        enrich,
        fetch_linear_tickers,
        fetch_spot_quotes,
        fmt_money,
        spot_match,
    )
except ImportError:
    sys.exit("Запускайте из папки рядом с funding_monitor.py")

try:
    from bybit_ws import TickerFeed, num
except ImportError:
    TickerFeed = None   # WS-лента опциональна: без файла работаем по REST

try:
    import bybit_memory as memory
except ImportError:
    memory = None   # память опциональна: бот без неё работает как раньше

    def num(v) -> float:
        try:
            if v is None or v == "":
                return 0.0
            return float(v)
        except (TypeError, ValueError):
            return 0.0

try:
    import trade_sanity as sanity
except ImportError:
    # Санитария сделок — не опция: без неё закрытие с битыми ценами ног
    # уходит в PnL и в обучение памяти. Лучше явно остановиться, чем тихо
    # работать без фильтра физически невозможных результатов.
    sys.exit("Нет trade_sanity.py рядом с funding_executor.py (санитария "
             "сделок) — обновите папку бота целиком")

PAPER_BASE = "https://api.bybit.com"
TESTNET_BASE = "https://api-testnet.bybit.com"
MAINNET_BASE = PAPER_BASE   # тот же публичный api.bybit.com, но с подписью реальных ключей
STATE_FILE = "position.json"
LOG_FILE = "bot.log"


LOG_MAX_BYTES = 8 * 1024 * 1024     # 8 МБ — ротация
LOG_KEEP = 5                        # сколько архивов хранить (.1 … .5)


def _rotate_log_if_needed():
    """Ротация bot.log по размеру.

    Зачем: бот пишет в файл НАПРЯМУЮ, а journald-ротация systemd на такие
    файлы не действует. Heartbeat раз в минуту = ~500КБ в год на символ;
    без ротации диск VPS переполняется за годы, а переполненный диск
    останавливает торговлю.

    Хвост не теряется: перед переименованием оставляем последние
    LOG_MAX_BYTES // 2 байт в свежем файле — иначе блок «События бота» на
    панели и health-check читали бы пустой лог сразу после ротации."""
    try:
        if not os.path.exists(LOG_FILE) or os.path.getsize(LOG_FILE) < LOG_MAX_BYTES:
            return
        # сдвигаем архивы: .4 удаляется, .3→.4, … , bot.log→.1
        for i in range(LOG_KEEP - 1, 0, -1):
            src = f"{LOG_FILE}.{i}"
            dst = f"{LOG_FILE}.{i + 1}"
            if os.path.exists(src):
                os.replace(src, dst)
        os.replace(LOG_FILE, f"{LOG_FILE}.1")
        # переносим хвост в новый файл, чтобы события не пропали
        with open(LOG_FILE, "w", encoding="utf-8") as f:
            with open(f"{LOG_FILE}.1", encoding="utf-8", errors="replace") as old:
                old.seek(0, os.SEEK_END)
                size = old.tell()
                old.seek(max(0, size - LOG_MAX_BYTES // 2))
                f.write(old.read())
        log(f"лог ротирован (>{LOG_MAX_BYTES / (1024 * 1024):.1f} МБ), "
            f"архивов: {LOG_KEEP}")
    except OSError:
        pass   # ротация не должна ломать торговлю


def log(msg: str):
    """Печать + дозапись в bot.log — вся история событий бота."""
    ts = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass
    _rotate_log_if_needed()


# Telegram: токен/чат из .env (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID),
# инициализируется в main(). Уведомления — только ключевые события, не heartbeat.
_TG = {"token": "", "chat": "", "enabled": True}
_TG_LAST: dict[str, float] = {}   # антиспам: ключ → время последней отправки


def notify(msg: str, key: str | None = None, min_gap: float = 0.0):
    """log() + сообщение в Telegram. Молчит, если не настроено или сбой сети —
    уведомление никогда не должно ломать торговый цикл.

    min_gap > 0 — антиспам: в лог пишем ВСЕГДА, в Telegram не чаще раза в
    min_gap секунд на ключ key. Без этого сетевой сбой в watch-цикле слал
    ~4 сообщения в минуту и забивал канал."""
    log(msg)
    if not _TG["enabled"] or not _TG["token"] or not _TG["chat"]:
        return
    if min_gap > 0:
        now = time.time()
        if now - _TG_LAST.get(key or msg, 0.0) < min_gap:
            return
        _TG_LAST[key or msg] = now
    try:
        data = urllib.parse.urlencode(
            {"chat_id": _TG["chat"], "text": msg}).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{_TG['token']}/sendMessage",
            data=data)
        urllib.request.urlopen(req, timeout=10).read()
    except Exception:  # noqa: BLE001 — сбой Telegram не должен убить цикл
        pass


def qty_step(symbol: str, category: str) -> Decimal:
    """Шаг лота инструмента.
    linear: lotSizeFilter.qtyStep; spot: basePrecision — это уже ШАГ (0.000001, 1),
    а не количество знаков (проверено по данным биржи)."""
    info = api_get("/v5/market/instruments-info",
                   {"category": category, "symbol": symbol})
    item = info["list"][0]
    lot = item.get("lotSizeFilter", {})
    if category == "linear":
        return Decimal(lot["qtyStep"])
    if lot.get("basePrecision") is not None:
        return Decimal(str(lot["basePrecision"]))
    if lot.get("qtyStep"):
        return Decimal(lot["qtyStep"])
    return Decimal("0.00000001")


def round_qty(qty: float, step: Decimal) -> Decimal:
    """Округление ВНИЗ до шага биржи (всегда в сторону уменьшения — консервативно)."""
    q = Decimal(str(qty))
    if step <= 0:
        return q
    return (q / step).to_integral_value(rounding=ROUND_DOWN) * step


def price_tick(symbol: str, category: str) -> Decimal:
    """Шаг цены инструмента (priceFilter.tickSize). Нужен для IOC-границы:
    цена не по тику отвергается биржей (10001 price invalid)."""
    info = api_get("/v5/market/instruments-info",
                   {"category": category, "symbol": symbol})
    item = info["list"][0]
    tick = (item.get("priceFilter") or {}).get("tickSize")
    return Decimal(str(tick)) if tick else Decimal("0")


def round_price(price: float, tick: Decimal, mode: str = "nearest") -> Decimal:
    """Округлить цену к тику. mode="down"/"up" — в сторону ЗАЩИТЫ/запаса:
    граница Sell округляется вниз, Buy — вверх: лимитка остаётся чуть
    мягче расчётной и IOC чаще исполняется, а перезащиты (отказа) не будет."""
    p = Decimal(str(price))
    if tick <= 0:
        return p
    q = p / tick
    if mode == "down":
        q = q.to_integral_value(rounding=ROUND_DOWN)
    elif mode == "up":
        q = q.to_integral_value(rounding=ROUND_UP)
    return q * tick


def bounded_limit_price(side: str, best_bid: float, best_ask: float,
                        max_slip_bps: float) -> float | None:
    """Худшая допустимая цена для IOC-ограничителя (защита от проскальзывания).
    Sell — не ниже best_bid × (1 − slip); Buy — не выше best_ask × (1 + slip).
    max_slip_bps ≤ 0 или нет стакана → None (защита выключена: вызывающий
    работает маркетом, как раньше)."""
    if max_slip_bps <= 0 or best_bid <= 0 or best_ask <= 0:
        return None
    slip = max_slip_bps / 10_000.0
    return best_bid * (1 - slip) if side == "Sell" else best_ask * (1 + slip)


def fmt_qty(qty: Decimal) -> str:
    """Строка количества без экспоненты и хвостовых нулей."""
    s = format(qty, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s or "0"


# ------------------------------------------------------------------ state
# Непустой = load_state() нашёл битый position.json и сбросил его. Пока
# флаг горит, бот НЕ входит в новые позиции: возможно, на бирже висит
# позиция, потерянная вместе с файлом (иначе получится двойная).
STATE_LOST: list = []


def base_coin(symbol: str) -> str:
    """BTCUSDT → BTC. Раньше стоял жёсткий срез [-4:]: для не-USDT пары
    (BTCUSDC, BTCUSDE) давал «BTCU» → coin_available возвращал 0 → ветка
    «спота нет» пропускала продажу, а сделка всё равно записывалась как
    закрытая (фантом). Тот же разбор, что в healthcheck._base_coin."""
    for quote in ("USDT", "USDC", "USDE", "USD"):
        if symbol.endswith(quote) and len(symbol) > len(quote):
            return symbol[:-len(quote)]
    return symbol


def load_state(path: str | None = None) -> dict:
    """Читает position.json. Битый/обрезанный файл — НЕ исключение:
    бот не должен зациклиться на ошибке чтения, потеряв управление
    открытой позицией. Отдаём пустое состояние и поднимаем файл в .bak,
    чтобы его можно было изучить вручную.
    path — явный файл состояния (команды, которые правят данные, работают
    с файлом рядом со скриптом, а не с тем, что случился в текущей папке)."""
    path = path or STATE_FILE
    if not os.path.exists(path):
        return {"position": None, "positions": [], "history": []}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError(f"ожидался объект, а не {type(data).__name__}")
        data.setdefault("position", None)
        data.setdefault("history", [])
        # миграция одиночной позиции в портфель: старый файл (поле position)
        # продолжает работать, новый пишется как positions[] + зеркало
        plist = data.get("positions")
        if not isinstance(plist, list):
            data["positions"] = [data["position"]] \
                if isinstance(data["position"], dict) else []
        data["positions"] = [p for p in data["positions"]
                             if isinstance(p, dict)]
        data["position"] = data["positions"][0] if data["positions"] else None
        return data
    except (OSError, ValueError) as e:
        try:
            os.replace(path, path + ".bak")
            log(f"СТАТЕ ПОВРЕЖДЕНО ({e}) — файл сохранён как {path}.bak, "
                f"состояние сброшено в пустое. Проверьте позицию на бирже!")
            STATE_LOST.append(e)
        except OSError:
            pass
        return {"position": None, "positions": [], "history": []}


# ------------------------------------------------------------------ позиции
# Состояние знает НЕСКОЛЬКО позиций (портфель из --max-positions пар).
# Источник правды — список state["positions"]; поле state["position"]
# остаётся зеркалом первой позиции: старые файлы и внешние инструменты
# (health-check, панель) читают именно его.
MIN_POSITION_CAPITAL = 50.0    # меньше этого пара не открывается: комиссии съедят доход


def positions_of(state: dict) -> list[dict]:
    """Открытые позиции: список positions[] или legacy-поле position."""
    plist = state.get("positions")
    if isinstance(plist, list):
        return [p for p in plist if isinstance(p, dict)]
    pos = state.get("position")
    return [pos] if isinstance(pos, dict) else []


def set_positions(state: dict, positions: list[dict]):
    """Записать портфель: список + зеркало первой позиции в legacy-поле."""
    state["positions"] = list(positions)
    state["position"] = positions[0] if positions else None


def find_position(state: dict, symbol: str | None = None,
                  pos: dict | None = None) -> dict | None:
    """Позиция по объекту/символу, или единственная открытая.

    Без symbol и без pos возвращает позицию только если она ОДНА: при
    портфеле «закрой позицию» обязано быть однозначным (иначе — ошибка,
    а не закрытие случайной пары).
    """
    plist = positions_of(state)
    if pos is not None:
        return pos
    if symbol:
        for p in plist:
            if p.get("symbol") == symbol:
                return p
        return None
    return plist[0] if len(plist) == 1 else None


def drop_position(state: dict, pos: dict) -> bool:
    """Убрать закрытую позицию из портфеля по объекту. False — такой уже нет."""
    rest, removed = [], False
    for p in positions_of(state):
        if not removed and p is pos:
            removed = True
            continue
        rest.append(p)
    set_positions(state, rest)
    return removed


def free_capital(cli, state: dict,
                 balance: float | None = None) -> float:
    """Свободный капитал под НОВУЮ пару: эквити − (нотион + маржа) открытых.

    paper — виртуальный счёт = capital × max-positions (иначе портфель в
    симуляции был бы невозможен). testnet/mainnet — реальное эквити UNIFIED.
    """
    used = sum(float(p.get("notional") or 0) + float(p.get("margin") or 0)
               for p in positions_of(state))
    if balance is None:
        equity = float(cli.capital) * max(1, int(getattr(cli, "max_positions", 1) or 1))
    else:
        equity = float(balance)
    return equity - used


def entry_capital(cli, state: dict,
                  balance: float | None = None) -> tuple[float, str | None]:
    """Сколько капитала дать НОВОЙ позиции. Возврат (капитал, ошибка|None).

    Один слот (--max-positions 1) — прежнее поведение: ровно cli.capital,
    достаточность средств проверяется там же, где и раньше.
    Портфель: капитал = min(capital, свободный остаток / 1.1) — тот же 10%
    буфер, что и в проверке баланса; меньше MIN_POSITION_CAPITAL — отказ.
    """
    max_pos = max(1, int(getattr(cli, "max_positions", 1) or 1))
    if max_pos <= 1:
        return float(cli.capital), None
    free = free_capital(cli, state, balance)
    want = float(cli.capital)
    allowed = min(want, max(0.0, free) / 1.1)
    if allowed < MIN_POSITION_CAPITAL:
        return 0.0, (f"нет свободной маржи под новую пару: свободно ${free:.2f}, "
                     f"нужно ≥ ${MIN_POSITION_CAPITAL:.0f} (капитал на пару "
                     f"${want:.2f}). Портфель: "
                     f"{len(positions_of(state))} из {max_pos} пар")
    return allowed, None


def save_state(state: dict, path: str | None = None):
    """Атомарная запись state (tmp + os.replace) — по образцу save_memory.
    Прямая запись обрезала файл при падении в середине json.dump, а
    position.json — единственный источник правды о позиции: его потеря
    оставляла бота живым, но слепым (он переставал управлять деньгами).
    path — явный файл состояния (см. load_state)."""
    path = path or STATE_FILE
    # зеркало держим в согласии ЗДЕСЬ, а не в каждом вызывающем: position.json
    # читают healthcheck/панель/старые версии — рассохшееся зеркало означало бы,
    # что они видят не ту позицию (или не видят вовсе)
    _plist = positions_of(state)
    state["positions"] = _plist
    state["position"] = _plist[0] if _plist else None
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())   # данные на диске ДО переименования
        os.replace(tmp, path)
    except OSError as e:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        raise RuntimeError(f"не удалось сохранить {path}: {e}")


class PartialFillError(RuntimeError):
    """Ордер исполнен частично, а сделка отменена (вход вне границы).
    Несёт ИСПОЛНЕННОЕ количество: вызывающий обязан откатить именно столько —
    иначе на бирже останется голая нога (пойманный баг с BTCUSDT)."""

    def __init__(self, msg: str, executed: float = 0.0):
        super().__init__(msg)
        self.executed = float(executed)


class OrderUnconfirmed(RuntimeError):
    """Сеть отвалилась при постановке ордера: неизвестно, ушёл ли он.
    Вызывающий обязан ОСТАНОВИТЬ вход и проверить позицию на бирже —
    продолжать нельзя: неизвестный ордер может исполниться позже."""


# ------------------------------------------------------------------ testnet client
def _load_env_file():
    """Читает .env рядом со скриптом, не затирая уже заданные переменные."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


# ------------------------------------------------------------------ config
CONFIG_FILE = "config.json"


def apply_config(parser: argparse.ArgumentParser, argv: list[str] | None = None,
                 path: str | None = None):
    """Загружает config.json и подставляет значения как «явно заданные».

    Зачем: 45 флагов в ExecStart systemd — это хрупко (опечатка = сервис не
    стартует) и нечитаемо в unit-файле. Конфиг держит торговые параметры
    в одном месте, а флаги остаются для разовых перекрытий.

    Правило приоритета: значение из конфига применяется, только если тот же
    параметр НЕ указан в командной строке — иначе нельзя было бы быстро
    переопределить одно число, не правя файл.
    """
    cfg_path = path or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), CONFIG_FILE)
    try:
        with open(cfg_path, encoding="utf-8") as f:
            cfg = json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        sys.exit(f"Не удалось прочитать {cfg_path}: {e}\n"
                 f"Исправьте JSON или удалите файл.")
    if not isinstance(cfg, dict):
        sys.exit(f"{cfg_path}: ожидался объект JSON, а не "
                 f"{type(cfg).__name__}")

    given = set()
    for tok in (argv if argv is not None else sys.argv[1:]):
        if tok.startswith("--"):
            given.add(tok[2:].split("=")[0].replace("-", "_"))

    known = {a.dest for a in parser._actions if a.dest != "help"}
    # bool-флаги в конфиге задаются как true/false
    for key, val in cfg.items():
        # ключи, начинающиеся с "_" — комментарии (в примере они объясняют
        # группы параметров); без их пропуска конфиг из примера не грузился
        if str(key).startswith("_"):
            continue
        dest = str(key).replace("-", "_")
        if dest not in known:
            sys.exit(f"{cfg_path}: неизвестный параметр '{key}'. "
                     f"Проверьте имя (см. --help).")
        if dest in given:
            continue                      # флаг в командной строке сильнее
        action = next((a for a in parser._actions
                       if a.dest == dest), None)
        if isinstance(action, argparse._StoreTrueAction):
            # булев флаг: строка "true"/"1"/"" трактуем как True,
            # "false"/"0"/"no" — как False. Иначе "mainnet" превратился бы
            # в True (любая непустая строка), и режим ездил бы в bool.
            parser.set_defaults(**{dest: str(val).strip().lower()
                                   not in ("0", "false", "no", "off", "")})
        elif isinstance(val, str):
            # store_true с пустой строкой ("no-notify": "") = False
            parser.set_defaults(**{dest: val})
        else:
            parser.set_defaults(**{dest: val})
    if argv is None:
        log(f"конфиг загружен: {cfg_path} ({len(cfg)} параметров, "
            f"флаги перекрывают)")
    return cfg


class RateLimited(RuntimeError):
    """Биржа упёрлась в лимит запросов (10006). Retry уже исчерпан —
    вызывающий решает, ждать или отступить."""


class ClockSkew(RuntimeError):
    """Часы сервера отстают больше recv_window (10002) — подпись не пройдёт."""


class BybitClient:
    """Подпись Bybit v5: HMAC-SHA256(timestamp + api_key + recv_window + payload).
    Один класс на две среды: testnet (игрушечные деньги) и mainnet
    (реальный счёт) — различаются только base-URL и имена переменных с ключами.

    request() сам повторяет запрос при 10006 (rate limit) с экспоненциальной
    паузой и jitter: без этого лимит запросов выглядел как случайный сбой,
    и бот просто терял итерацию цикла. Код 10002 (часы) — не ретраится,
    а поднимает ClockSkew с понятным текстом: подпись не пройдёт, пока
    на VPS не настроен NTP, и «терпеливый» ретрай только хуже сделает."""

    RECV_WINDOW = "5000"
    RETRY_MAX = 5          # попыток при 10006
    RETRY_BASE = 0.6       # сек базовой паузы (растёт ×2)
    RETRY_CAP = 8.0        # потолок паузы

    def __init__(self, mode: str = "testnet"):
        _load_env_file()
        self.mode = mode
        if mode == "mainnet":
            self.key = os.environ.get("BYBIT_API_KEY", "")
            self.secret = os.environ.get("BYBIT_API_SECRET", "")
            if not self.key or not self.secret:
                sys.exit(
                    "Нет ключей mainnet. Экспортируйте:\n"
                    "  export BYBIT_API_KEY=...\n"
                    "  export BYBIT_API_SECRET=...\n"
                    "Ключи: bybit.com → API Management. ВАЖНО: без права вывода "
                    "и с IP-whitelist (только этот сервер)."
                )
            self.base = MAINNET_BASE
        else:
            self.key = os.environ.get("BYBIT_TESTNET_API_KEY", "")
            self.secret = os.environ.get("BYBIT_TESTNET_API_SECRET", "")
            if not self.key or not self.secret:
                sys.exit(
                    "Нет ключей testnet. Экспортируйте:\n"
                    "  export BYBIT_TESTNET_API_KEY=...\n"
                    "  export BYBIT_TESTNET_API_SECRET=...\n"
                    "Ключи: testnet.bybit.com → API Management (без права вывода!)."
                )
            self.base = TESTNET_BASE

    def set_leverage(self, symbol: str, leverage: float,
                     category: str = "linear") -> dict:
        """Явно выставить плечо символа.
        needed на mainnet: новый символ возьмёт дефолт биржи (обычно 10x),
        и расчёт маржи/риска разойдётся с --leverage."""
        body = {"category": category, "symbol": symbol,
                "buyLeverage": str(leverage), "sellLeverage": str(leverage)}
        return self.request("POST", "/v5/position/set-leverage", body=body)

    def set_trading_stop(self, symbol: str, stop_loss: float = 0,
                     take_profit: float = 0, trigger_by: str = "MarkPrice") -> dict:
        """Биржевой TP/SL на перп-ногу (tpslMode=Full, one-way: positionIdx=0).

    Зачем при дельта-нейтрале: все выходы бота ЛОКАЛЬНЫЕ. Если VPS лежит,
        сеть отвалилась или процесс в паузе — единственная защита перп-ноги
        от ликвидации это биржа. Стробы на бирже срабатывают без нас.
        0 = снять соответствующую ногу (документация биржи)."""
        body = {"category": "linear", "symbol": symbol,
                "tpslMode": "Full", "positionIdx": 0,
                "takeProfit": str(take_profit or 0),
                "stopLoss": str(stop_loss or 0),
                "tpTriggerBy": trigger_by, "slTriggerBy": trigger_by}
        return self.request("POST", "/v5/position/trading-stop", body=body)

    def position_info(self, symbol: str) -> dict:
        """Фактическая позиция (size, liqPrice, markPrice) — источник истины
        о риске ликвидации."""
        r = self.request("GET", "/v5/position/list",
                         params={"category": "linear", "symbol": symbol})
        for p in r.get("list", []):
            if float(p.get("size") or 0) > 0:
                return p
        return {}

    def _sign(self, ts: int, recv_window: str, payload: str) -> str:
        msg = f"{ts}{self.key}{recv_window}{payload}".encode()
        return hmac.new(self.secret.encode(), msg, hashlib.sha256).hexdigest()

    def request(self, method: str, path: str, params: dict | None = None,
                body: dict | None = None) -> dict:
        url = self.base + path
        payload = ""
        if body is not None:
            payload = json.dumps(body, separators=(",", ":"))
        elif params:
            payload = urllib.parse.urlencode(params)
        if params and method == "GET":
            url += "?" + payload
        data = payload.encode() if body is not None else None
        attempt = 0
        while True:
            # подпись и timestamp пересчитываются на КАЖДУЮ попытку:
            # после паузы старый timestamp уже не в recv_window
            recv_window = self.RECV_WINDOW
            ts = int(time.time() * 1000)
            headers = {
                "X-BAPI-API-KEY": self.key,
                "X-BAPI-TIMESTAMP": str(ts),
                "X-BAPI-RECV-WINDOW": recv_window,
                "X-BAPI-SIGN": self._sign(ts, recv_window, payload),
                "Content-Type": "application/json",
            }
            req = urllib.request.Request(url, data=data, headers=headers,
                                         method=method)
            with urllib.request.urlopen(req, timeout=15) as resp:
                out = json.loads(resp.read().decode())
            code = out.get("retCode")
            if code == 0:
                return out["result"]
            if code == 10002:      # timestamp recv_window — рассинхрон часов
                raise ClockSkew(
                    f"Bybit: 10002 — часы сервера отстают больше "
                    f"{self.RECV_WINDOW} мс (retMsg: {out.get('retMsg')}). "
                    f"Настройте NTP: timedatectl set-ntp true")
            if code == 10006:      # rate limit — ждём и повторяем
                attempt += 1
                if attempt >= self.RETRY_MAX:
                    raise RateLimited(
                        f"Bybit: 10006 (лимит запросов) после "
                        f"{attempt} попыток: {out.get('retMsg')}")
                # +jitter, чтобы ретраи нескольких символов не били
                # по бирже синхронно и не усиливали лимит
                delay = min(self.RETRY_BASE * (2 ** (attempt - 1)),
                            self.RETRY_CAP) * (0.5 + random.random())
                log(f"RATE LIMIT (10006) {path}: пауза {delay:.1f}с, "
                    f"попытка {attempt}/{self.RETRY_MAX}")
                time.sleep(delay)
                continue
            label = "mainnet" if self.mode == "mainnet" else "testnet"
            raise RuntimeError(f"Bybit {label} API {code}: {out.get('retMsg')}")

    # --- торговые операции ---
    def flows_since(self, symbol: str, since_ms: int,
                    currency: str = "USDT") -> dict | None:
        """ФАКТИЧЕСКИЕ начисления по символу за окно: {funding, fee, n} $.
        Источник — /v5/account/transaction-log (type=SETTLEMENT/TRADE),
        а не расчёт «ставка × нотион»: реальные выплаты зависят от позиции
        на момент снимка ставки, от частичных закрытий и комиссий.
        funding > 0 = бот получил (шорт), fee > 0 = расход.
        None = не удалось прочитать (сеть/ключи) — молчаливую неизвестность
        в сверку не подмешиваем."""
        try:
            r = self.request("GET", "/v5/account/transaction-log",
                             params={"accountType": "UNIFIED",
                                     "category": "linear",
                                     "currency": currency,
                                     "symbol": symbol,
                                     "startTime": str(int(since_ms)),
                                     "limit": "50"})
        except Exception:  # noqa: BLE001
            return None
        funding = fee = 0.0
        n = 0
        for t in r.get("list") or []:
            try:
                f = float(t.get("funding") or 0)
                fe_ = float(t.get("fee") or 0)
            except (TypeError, ValueError):
                continue
            if f:
                funding += f
                n += 1
            fee += fe_
        return {"funding": funding, "fee": fee, "n": n}

    def position_qty(self, symbol: str) -> float | None:
        """Текущий размер net-позиции linear; None = не удалось узнать.
        Нужен перед reduce-only: нулевая позиция даёт 110017 (позиция могла
        исчезнуть без нас — неттинг чужой ликвидацией на тестовом аккаунте)."""
        try:
            r = self.request("GET", "/v5/position/list",
                             params={"category": "linear", "symbol": symbol})
        except Exception:  # noqa: BLE001
            return None
        for p in r.get("list", []):
            sz = float(p.get("size") or 0)
            if sz > 0:
                return sz
        return 0.0

    def coin_available(self, coin: str) -> float | None:
        """Баланс монеты (UNIFIED); None = не удалось узнать.
        Берём transferBalance — баланс, который реально можно продать/снять
        (walletBalance включает суммы в резерве висящих ордеров; продавать
        сверх него всё равно нельзя)."""
        try:
            r = self.request("GET", "/v5/account/wallet-balance",
                             params={"accountType": "UNIFIED"})
        except Exception:  # noqa: BLE001
            return None
        for acct in r.get("list", []):
            for c in acct.get("coin", []):
                if c.get("coin") == coin:
                    try:
                        v = c.get("transferBalance")
                        if v is None:   # биржа не дала поле — откат на walletBalance
                            v = c.get("walletBalance")
                        return float(v or 0)
                    except (TypeError, ValueError):
                        return None
        return 0.0

    def market_order(self, symbol: str, side: str, qty: float, category: str = "linear",
                     reduce_only: bool = False):
        # количество всегда кратно шагу лота — иначе биржа вернёт 10001 Qty invalid
        step = qty_step(symbol, category)
        q = round_qty(qty, step)
        if q <= 0:
            raise RuntimeError(f"Количество {qty} меньше шага лота {step} для {symbol}")
        body = {
            "category": category,
            "symbol": symbol,
            "side": side,            # Buy / Sell
            "orderType": "Market",
            "qty": fmt_qty(q),
        }
        if reduce_only and category == "linear":
            # закрытие позиции не требует новой маржи (иначе 110007 ab not enough,
            # когда кошелёк пуст, а позиция держится на equity)
            body["reduceOnly"] = True
        if category == "spot":
            # без явного marketUnit testnet отклоняет маркет спота (170140)
            body["marketUnit"] = "baseCoin"
        try:
            res = self.request("POST", "/v5/order/create", body=body)
        except OSError as e:
            # сеть отвалилась на постановке маркета: ордер мог уйти и
            # исполниться, а ответ потерян. Молчаливый возврат «ничего не
            # исполнилось» приводил бы к двойной покупке на следующей ноге.
            raise OrderUnconfirmed(
                f"{symbol} {category}: сеть отвалилась при постановке "
                f"маркета {side} ({e}) — исполнение неизвестно, проверьте "
                f"позицию на бирже")
        self.wait_fill(symbol, res.get("orderId", ""), category)
        return res

    def ioc_order(self, symbol: str, side: str, qty: float, price: float,
                  category: str = "linear", reduce_only: bool = False,
                  wait: float = 6.0) -> float:
        """IOC-лимитка с границей цены: исполняется СРАЗУ по доступным ценам
        стакана, но не хуже указанной; остаток биржа отменяет.
        Возвращает ИСПОЛНЕННОЕ количество (0 = цена ушла за границу).
        Это защита от проскальзывания: маркет в дырявом/пустом стакане может
        взять любую цену, IOC просто не исполняется и мы решаем, что делать."""
        step = qty_step(symbol, category)
        q = round_qty(qty, step)
        if q <= 0 or price <= 0:
            return 0.0
        body = {
            "category": category,
            "symbol": symbol,
            "side": side,
            "orderType": "Limit",
            "qty": fmt_qty(q),
            "price": fmt_qty(Decimal(str(price))),
            "timeInForce": "IOC",
        }
        if reduce_only and category == "linear":
            body["reduceOnly"] = True
        if category == "spot":
            body["marketUnit"] = "baseCoin"
        try:
            res = self.request("POST", "/v5/order/create", body=body)
        except OSError as e:
            # сеть отвалилась на постановке — состояние на бирже неизвестно
            raise OrderUnconfirmed(
                f"{symbol} {category}: сеть отвалилась при постановке IOC "
                f"({e}) — исполнение неизвестно")
        order_id = res.get("orderId", "")

        def status() -> tuple[float, str]:
            """(исполнено, статус) по orderId; (-1, "") — ордера ещё нет."""
            hist = self.request("GET", "/v5/order/history",
                                {"category": category, "symbol": symbol,
                                 "orderId": order_id})
            lst = hist.get("list") or []
            if not lst:
                return -1.0, ""
            o = lst[0]
            return float(o.get("cumExecQty") or 0), o.get("orderStatus") or ""

        deadline = time.time() + wait
        while time.time() < deadline:
            try:
                done, st = status()
            except RuntimeError:
                time.sleep(0.3)
                continue
            if st == "Filled":
                return float(q)
            # PartiallyFilled/New — НЕ финал: IOC может ещё добиваться,
            # а недоучёт исполненного заставит добить маркетом и дать
            # перевес ноги (баг BTCUSDT) — ждём финального статуса
            if st in ("Cancelled", "Rejected", "PartiallyFilledCanceled"):
                return done
            time.sleep(0.3)
        # ТАЙМАУТ: ордер мог остаться живым или частично исполниться.
        # Раньше здесь возвращался 0 — вызывающий считал ногу пустой и
        # добирал маркетом ВЕСЬ объём, получая перевес. Теперь снимаем
        # ордер и читаем финальный статус — возвращаем ровно то, что
        # биржа посчитала исполненным.
        try:
            self.request("POST", "/v5/order/cancel",
                         body={"category": category, "symbol": symbol,
                               "orderId": order_id})
        except RuntimeError:
            pass  # мог успеть исполниться/отмениться — читаем статус ниже
        deadline2 = time.time() + 3.0
        while time.time() < deadline2:
            try:
                done, st = status()
            except RuntimeError:
                time.sleep(0.3)
                continue
            if st and st != "New" and st != "PartiallyFilled":
                return float(q) if st == "Filled" else done
            time.sleep(0.3)
        # статус так и не стал финальным: консервативно считаем исполненным
        # всё, что биржа уже показала (done >= 0), но не больше q
        try:
            done, _ = status()
            if done >= 0:
                return min(done, float(q))
        except RuntimeError:
            pass
        return 0.0

    def limit_order(self, symbol: str, side: str, qty: float, price: float,
                    category: str = "linear", wait: float = 6.0) -> float:
        """PostOnly-лимитка (maker): цена из стакана — ордер ляжет в книгу.
        Возвращает ИСПОЛНЕННОЕ количество (0 = не исполнилась/отклонена):
        вызывающий обязан добрать маркетом только остаток qty − result,
        иначе частичное исполнение + полный маркет = перевес ноги.
        Спотовый ордер без marketUnit testnet не принимает (проверено ранее)."""
        step = qty_step(symbol, category)
        q = round_qty(qty, step)
        if q <= 0 or price <= 0:
            return 0.0
        body = {
            "category": category,
            "symbol": symbol,
            "side": side,
            "orderType": "Limit",
            "qty": fmt_qty(q),
            "price": fmt_qty(Decimal(str(price))),
            "timeInForce": "PostOnly",
        }
        if category == "spot":
            body["marketUnit"] = "baseCoin"
        try:
            res = self.request("POST", "/v5/order/create", body=body)
            order_id = res.get("orderId", "")
        except RuntimeError:
            return 0.0  # биржа отклонила — вызывающий пойдёт в маркет
        except OSError as e:
            # СЕТЬ ОТВАЛИЛАСЬ ПОСЛЕ отправки: ордер мог уйти на биржу, а
            # ответ потерялся. Раньше здесь исключение уходило наверх, бот
            # считал вход неудачным и через 10 мин открывал новую пару —
            # а висящая лимитка тем временем исполнялась (голый шорт).
            # Нам неизвестно, исполнено ли что-то: говорим явно, чтобы
            # вызывающий НЕ продолжил вход, и гасим ордер по orderId.
            raise OrderUnconfirmed(
                f"{symbol} {category}: сеть отвалилась при постановке ордера "
                f"({e}) — состояние на бирже неизвестно, вход остановлен")

        def executed() -> float:
            hist = self.request("GET", "/v5/order/history",
                                {"category": category, "symbol": symbol,
                                 "orderId": order_id})
            lst = hist.get("list") or []
            if not lst:
                return -1.0  # ордера ещё нет в истории — ждём
            o = lst[0]
            done = float(o.get("cumExecQty") or 0)
            if o.get("orderStatus") == "Filled":
                return float(o["qty"])
            if o.get("orderStatus") in ("Cancelled", "Rejected",
                                         "PartiallyFilledCanceled"):
                return done if done > 0 else 0.0
            return -1.0  # New / PartiallyFilled — ждём дальше

        deadline = time.time() + wait
        while time.time() < deadline:
            try:
                done = executed()
            except RuntimeError:
                time.sleep(0.4)
                continue
            if done >= 0:
                return done
            time.sleep(0.4)
        # таймаут: отменяем, чтобы висячий ордер не исполнился после маркета
        try:
            self.request("POST", "/v5/order/cancel",
                         body={"category": category, "symbol": symbol,
                               "orderId": order_id})
        except RuntimeError:
            pass  # мог успеть исполниться — ждём финальный статус ниже
        # ждём финального статуса: сразу после cancel история может ещё показывать
        # New/PartiallyFilled — один опрос дал бы 0 и маркет купил бы ВСЁ
        # количество повторно (пойманный баг: перевес 0.003502 BTC)
        deadline2 = time.time() + 3.0
        while time.time() < deadline2:
            try:
                done = executed()
            except RuntimeError:
                time.sleep(0.3)
                continue
            if done >= 0:
                return done
            time.sleep(0.3)
        return 0.0

    def wait_fill(self, symbol: str, order_id: str, category: str,
                  timeout: float = 10.0):
        """Ждёт ИСПОЛНЕНИЯ ордера. На testnet маркет-ордер может мгновенно
        отмениться (EC_NoImmediateQtyToFill — пустой стакан), и без этой
        проверки бот записал бы несуществующую позицию."""
        if not order_id:
            raise RuntimeError(f"Биржа не вернула orderId для {symbol}")
        deadline = time.time() + timeout
        while time.time() < deadline:
            hist = self.request("GET", "/v5/order/history",
                                {"category": category, "symbol": symbol,
                                 "orderId": order_id})
            lst = hist.get("list") or []
            if lst:
                o = lst[0]
                status = o.get("orderStatus")
                if status == "Filled":
                    return o
                if status in ("Cancelled", "Rejected", "PartiallyFilledCanceled"):
                    raise RuntimeError(
                        f"ордер {category} {symbol} не исполнён: {status} "
                        f"({o.get('rejectReason') or 'нет ликвидности в стакане'})")
                # New / PartiallyFilled — ждём дальше
            time.sleep(0.4)
        raise RuntimeError(f"таймаут исполнения {category} {symbol} ({timeout}c)")

    def wallet_balance(self) -> float:
        res = self.request("GET", "/v5/account/wallet-balance",
                           params={"accountType": "UNIFIED"})
        for acc in res.get("list", []):
            for coin in acc.get("coin", []):
                if coin.get("coin") == "USDT":
                    return float(coin.get("equity") or 0)
        return 0.0


class TestnetClient(BybitClient):
    """Совместимость: старые вызовы без аргумента — testnet."""

    def __init__(self):
        super().__init__("testnet")


def make_client(mode: str) -> BybitClient:
    """Подписанный клиент среды позиции/режима: mainnet или testnet."""
    return BybitClient("mainnet") if mode == "mainnet" else TestnetClient()


# ------------------------------------------------------------------ scan
def build_filter_args(cli) -> argparse.Namespace:
    """Собрать namespace с порогами для enrich() из пресета/флагов CLI.
    Если память откалибровала пороги (прогноз APY систематически расходился
    с фактом) — применяются откалиброванные значения вместо дефолта."""
    ns = argparse.Namespace(
        spot_fee=cli.spot_fee,
        perp_taker_fee=cli.perp_taker_fee,
        perp_maker_fee=cli.perp_maker_fee,
        preset=cli.preset,
    )
    for key, default in PRESETS[cli.preset].items():
        setattr(ns, key, default)
    # явные значения CLI (в т.ч. --pass-oi) сильнее пресета: после fill-цикла
    # в main() у cli уже стоят значения пресета, поэтому сравниваем с пресетом
    for key in PRESETS[cli.preset]:
        val = getattr(cli, key, None)
        if val is not None:
            setattr(ns, key, val)
    if memory is not None and not cli.no_learn:
        thr = memory.thresholds_for(cli.preset)
        if thr:
            # min_basis тоже применяется: _combine его считает, autotune
            # двигает (AUTO_BASIS_STEP), панель печатает — но раньше он
            # ни разу не доходил до фильтра, и вся ветка обучения по базису
            # была мёртвым кодом
            if "min_apy30" in thr:
                ns.min_apy30 = thr["min_apy30"]
            if "fast_apy" in thr:
                ns.fast_apy = thr["fast_apy"]
                cli.fast_apy = thr["fast_apy"]
            if "min_basis" in thr:
                ns.min_basis = thr["min_basis"]
                cli.min_basis = thr["min_basis"]
    return ns


def scan(cli) -> list[dict]:
    """Символы, проходящие фильтр (verdict_ok), отсортированные по APY 30д."""
    fargs = build_filter_args(cli)
    now_ms = int(time.time() * 1000)
    tickers = fetch_linear_tickers()
    spot = fetch_spot_quotes()
    cands = [
        t for t in tickers
        if t["turnover24h"] >= cli.min_volume * 1e6 and t["oi_value"] >= cli.min_oi * 1e6
    ]
    cands.sort(key=lambda t: t["funding_rate"], reverse=True)
    cands = cands[: cli.prefilter]
    rows = []
    for t in cands:
        r = analyze_symbol(t, now_ms)
        enrich(r, spot, fargs)
        if r.get("verdict_ok") and not spot_book_alive(cli, r.get("spot_symbol")):
            # как в deep_check: фильтр прошёл, но вход некуда исполнять
            continue
        if r.get("verdict_ok") and not_executable(cli, r):
            continue
        if r.get("verdict_ok"):
            rows.append(r)
        time.sleep(0.05)
    rows.sort(key=conservative_apy, reverse=True)
    return rows


def print_scan(rows: list[dict]):
    if not rows:
        print("Сейчас ни один символ не проходит фильтр — бот ждёт в стейблкоинах.")
        return
    print(f"{'СИМВОЛ':<14} {'APY 30д':>9} {'СТАВКА':>9} {'БАЗИС':>8} {'ОКУП':>6} {'OI':>8}")
    print("-" * 60)
    for r in rows:
        w30 = r.get("w30") or {}
        be = r.get("breakeven_shown")
        bas = f"{r['basis']:+.3f}%" if r.get("basis") is not None else "нет"
        print(f"{r['symbol']:<14} {w30.get('avg_apy', 0):>+8.1f}% "
              f"{r['funding_rate'] * 100:>+8.4f}% {bas:>8} "
              f"{f'{be:.1f}д' if be else '∞':>6} {fmt_money(r['oi_value']):>8}")


# ------------------------------------------------------------------ sizing / legs
def size_legs(capital: float, leverage: float) -> tuple[float, float]:
    """
    Капитал C делится: нотион спота N + маржа перпа N/L = C.
    Возвращает (нотион, маржа перпа). Плечо ≤ 3 по правилам проекта.
    """
    if leverage < 1.5 or leverage > 3:
        sys.exit("Плечо перп-ноги должно быть от 1.5 до 3 (правило проекта: ≤ 3).")
    notional = capital / (1 + 1 / leverage)
    margin = notional / leverage
    return notional, margin


def resolve_legs(symbol: str) -> tuple[str, float, float]:
    """(спот-символ, множитель, ...) — через spot_match монитора."""
    spot = fetch_spot_quotes()
    skey, mult = spot_match(symbol, spot)
    if not skey:
        sys.exit(f"У {symbol} нет спот-ноги на Bybit — классический хедж невозможен.")
    return skey, mult, spot[skey]


# ------------------------------------------------------------------ commands
def cmd_scan(cli):
    print_scan(scan(cli))


def orderbook(symbol: str, category: str, cli, mode: str | None = None) -> dict:
    """Стакан той площадки, куда реально идут ордера: paper — основная
    биржа, testnet — testnet (именно его стакан решает исход входа).
    mode задаётся ЯВНО на выходе: close может зваться с --mode paper, и
    стакан mainnet дал бы неверную IOC-границу для ордеров testnet."""
    m = mode or getattr(cli, "mode", "paper")
    base = TESTNET_BASE if m == "testnet" else PAPER_BASE
    url = f"{base}/v5/market/orderbook?" + urllib.parse.urlencode(
        {"category": category, "symbol": symbol, "limit": 50})
    req = urllib.request.Request(url, headers={"User-Agent": "funding-monitor/1.0"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode())
    if data.get("retCode") != 0:
        raise RuntimeError(f"Bybit API error {data.get('retCode')}: {data.get('retMsg')}")
    return data["result"]


def avg_fill_since(client, category: str, symbol: str, t0_ms: int,
                   limit: int = 50) -> float | None:
    """Средняя цена исполнения ордеров символа, созданных после t0_ms.
    Окно задаётся параметром startTime биржи: полагаться на «последние N
    ордеров» нельзя — при повторном входе в тот же символ старые сделки
    попали бы в среднюю и портили цену входа/выхода."""
    try:
        hist = client.request("GET", "/v5/order/history",
                              {"category": category, "symbol": symbol,
                               "startTime": str(t0_ms), "limit": str(limit)})["list"]
    except RuntimeError:
        return None
    val = qty = 0.0
    for o in hist:
        if int(o.get("createdTime") or 0) < t0_ms:
            continue
        done = float(o.get("cumExecQty") or 0)
        if done <= 0:
            continue
        value = float(o.get("cumExecValue") or 0)
        if value > 0:
            val += value
            qty += done
    return val / qty if qty > 0 else None


# Отметка «стакан проверяли» — символ → (мс проверки, жив ли стакан).
# На testnet спот-стакан есть только у мажоров (ETH/BTC/SOL/ADA): мелкие
# пары отвечают пусто, и вход туда физически неисполним. Кэш нужен, чтобы
# fast-цикл каждые 15 с не дёргал REST по одному и тому же символу.
_SPOT_BOOK_CACHE: dict[str, tuple[float, bool]] = {}
SPOT_BOOK_TTL = 600.0    # сек: как кулдаун failed-символа (10 мин)


def spot_book_alive(cli, skey: str | None) -> bool:
    """Жив ли спот-стакан символа (есть bids И asks). None/пустота — False.

    Сетевая ошибка/таймаут — True: неизвестность не должна блокировать
    вход (check_liquidity перед сделкой всё равно проверит стакан свежо).
    Отказ БИРЖИ (retCode≠0, напр. 10001 «нет такого символа на testnet»)
    — напротив, стакана нет: кэшируем False, чтобы кандидат не проходил
    фильтр каждые 15 с.
    Кэш на SPOT_BOOK_TTL сек — стакан не появляется/исчезает каждые секунды,
    а попытка входа с пустым стаканом и так откатится с алертом."""
    if not skey:
        return False
    now = time.time() * 1000
    hit = _SPOT_BOOK_CACHE.get(skey)
    if hit and now - hit[0] < SPOT_BOOK_TTL * 1000:
        return hit[1]
    try:
        ob = orderbook(skey, "spot", cli)
    except RuntimeError as e:
        # биржа ответила отказом (10001: спот-пара на testnet не числится) —
        # это не сбой сети, а отсутствие стакана
        if "Bybit API error" in str(e):
            _SPOT_BOOK_CACHE[skey] = (now, False)
            return False
        return True
    except (Exception, SystemExit):  # noqa: BLE001 — сбой сети ≠ пустой стакан
        return True
    alive = bool((ob.get("b") or []) and (ob.get("a") or []))
    _SPOT_BOOK_CACHE[skey] = (now, alive)
    return alive


# Кэш «исполним ли вход сейчас» — символ → (мс проверки, причина отказа).
_EXEC_CACHE: dict[str, tuple[float, str | None]] = {}
EXEC_TTL = 60.0    # сек: стакан за минуту не переворачивается катастрофически


def not_executable(cli, r: dict) -> str | None:
    """Причина, по которой вход в кандидата сейчас неисполним (спред/глубина
    обеих ног), или None. Те же проверки, что open_position ПЕРЕД сделкой,
    но заранее: попытка входа стоит пары минут (ордер, откат, кулдаун 10 мин),
    а проверка в фильтре — пары REST. Кэш EXEC_TTL сек, иначе fast-цикл
    каждые 15 с дёргал бы 2×N стаканов."""
    sym, skey = r.get("symbol"), r.get("spot_symbol")
    if not sym or not skey:
        return "нет спот-пары"
    now = time.time() * 1000
    hit = _EXEC_CACHE.get(sym)
    if hit and now - hit[0] < EXEC_TTL * 1000:
        return hit[1]
    notional, _margin = size_legs(float(getattr(cli, "capital", 1000.0)),
                                  float(getattr(cli, "leverage", 2.0)))
    reason = (check_liquidity(cli, sym, "linear", notional, side="bid")
              or check_liquidity(cli, skey, "spot", notional, side="ask"))
    if reason is None:
        # фактический базис ног ПО СТАКАНАМ: котировки ленты бывают гнилыми
        # (живой случай XRP testnet: фильтр видел базис +0.03%, стакан
        # давал +3.3% — вход исполнялся, дельта-контроль тут же закрывал
        # позицию, сжигая комиссию круга на каждой попытке)
        max_basis = float(getattr(cli, "max_entry_basis_pct", 0.5) or 0)
        if max_basis > 0:
            try:
                ob_p = orderbook(sym, "linear", cli)
                ob_s = orderbook(skey, "spot", cli)
            except (Exception, SystemExit):  # noqa: BLE001 — сбой сети не блокирует
                ob_p = ob_s = {}
            bp, ap = ob_p.get("b") or [], ob_p.get("a") or []
            bs, asx = ob_s.get("b") or [], ob_s.get("a") or []
            if bp and ap and bs and asx:
                pmid = (float(bp[0][0]) + float(ap[0][0])) / 2
                smid = (float(bs[0][0]) + float(asx[0][0])) / 2
                if pmid > 0 and smid > 0:
                    basis = (pmid / smid - 1) * 100
                    if abs(basis) > max_basis:
                        reason = (f"базис стакана {basis:+.2f}% "
                                  f"(порог ±{max_basis:g}%)")
    _EXEC_CACHE[sym] = (now, reason)
    return reason


def check_liquidity(cli, symbol: str, category: str, notional: float,
                    side: str) -> str | None:
    """Проверка стакана ПЕРЕД входом: возвращает причину отказа или None.
    Сторона входа: "ask" — мы покупаем (спот), "bid" — мы продаём (перп-шорт).
    Если спред широкий или глубины top-20 не хватает на наш нотион — taker-вход
    съест проскальзыванием окупаемость (или ордер отменится, как VVVUSDT)."""
    if cli.max_spread <= 0 and cli.min_depth <= 0:
        return None
    try:
        ob = orderbook(symbol, category, cli)
    except (KeyError, IndexError, TypeError):
        return f"стакан {symbol} недоступен"
    except RuntimeError as e:
        # биржа ответила отказом (10001 «нет символа» и т.п.) — не падаем
        # в «ошибку цикла», а честно отказываем от входа
        return f"стакан {symbol} недоступен ({str(e)[:60]})"
    # Bybit отдаёт ключи "b"/"a" (не bids/asks!)
    bids, asks = ob.get("b") or [], ob.get("a") or []
    if not bids or not asks:
        return f"стакан {symbol} пуст"
    best_bid, best_ask = float(bids[0][0]), float(asks[0][0])
    mid = (best_bid + best_ask) / 2
    if mid <= 0:
        return f"стакан {symbol} пуст"
    spread_pct = (best_ask - best_bid) / mid * 100
    if cli.max_spread > 0 and spread_pct > cli.max_spread:
        return (f"спред {symbol} {spread_pct:.3f}% больше порога "
                f"{cli.max_spread}%")
    # глубина на СТОРОНЕ нашего входа: top-20 в долларах
    book = asks if side == "ask" else bids
    depth = sum(float(p) * float(q) for p, q in book[:20])
    need = notional * cli.min_depth
    if cli.min_depth > 0 and depth < need:
        return (f"глубина {side}-top20 {symbol} ${depth:.0f} меньше "
                f"{need:.0f} (нотион ${notional:.0f})")
    return None


def entry_taker_only(cli) -> tuple[bool, str]:
    """Итоговый режим входа: приоритет флаг CLI --taker-only, дальше память.
    Память (learn_entry_mode) сравнивает фактическую прибыль maker/taker
    входов; prefer=taker → пропускаем maker-попытки. Возвращает
    (taker_only, причина_для_лога)."""
    if cli.taker_only:
        return True, ""
    if memory is None or cli.no_learn:
        return False, ""
    prefer = (memory.load_memory().get("entry_mode") or {}).get("prefer")
    if prefer == "taker":
        return True, ("MEMORY: вход сразу маркетом — память: taker-входы "
                      "приносили лучше maker-лимиток")
    return False, ""


def open_position(cli, r) -> dict:
    """Открыть пару по уже проанализированной строке r. Возвращает pos."""
    if cli.mode == "mainnet" and not getattr(cli, "confirm_live", False):
        sys.exit("Режим mainnet — РЕАЛЬНЫЕ деньги. Подтвердите флагом "
                 "--confirm-live (осознанный запуск на реальном счёте).")
    state = load_state()
    if STATE_LOST:
        sys.exit("position.json был повреждён и сброшен в пустое состояние "
                 f"({STATE_LOST[-1]}). Новый вход заблокирован: на бирже может "
                 f"висеть потерянная позиция — проверьте вручную. Если её нет, "
                 f"удалите {STATE_FILE}.bak и запустите снова.")
    symbol = r["symbol"]
    max_pos = max(1, int(getattr(cli, "max_positions", 1) or 1))
    plist = positions_of(state)
    if len(plist) >= max_pos:
        sys.exit(f"Портфель заполнен: {len(plist)}/{max_pos} пар "
                 f"({', '.join(str(p.get('symbol')) for p in plist)}) — "
                 f"новую пару открою после close (или увеличьте "
                 f"--max-positions).")
    if any(p.get("symbol") == symbol for p in plist):
        sys.exit(f"{symbol} уже в портфеле — второй раз не вхожу "
                 f"(переоткрыть: close --symbol {symbol}, затем enter).")

    # Баланс нужен ДО расчёта размера: в портфеле капитал пары урезается
    # по свободному остатку счёта, а не берётся равным --capital.
    client = None
    bal = None
    if cli.mode in ("testnet", "mainnet"):
        client = make_client(cli.mode)
        bal = client.wallet_balance()
        print(f"Баланс {cli.mode} UNIFIED: ${bal:.2f}")
    capital, why_not = entry_capital(cli, state, bal)
    if why_not:
        sys.exit(why_not)

    skey, mult, (spot_bid, spot_ask) = resolve_legs(symbol)
    notional, margin = size_legs(capital, cli.leverage)

    perp_price = r.get("perp_ask") or r["price"]   # шортим по худшей стороне
    spot_price = spot_ask                          # покупаем по ask

    # Шаг лота: qty кратно шагу биржи, иначе 10001 Qty invalid.
    # Порядок: сначала округляем перп-ногу вниз, потом подгоняем спот под
    # ФАКТИЧЕСКИЙ нотион перпа — ноги сходятся по значению по построению.
    perp_step = qty_step(symbol, "linear")
    perp_qty = round_qty(notional / perp_price, perp_step)
    if perp_qty <= 0:
        sys.exit(f"Нотион ${notional:.2f} меньше шага лота {perp_step} {symbol} — "
                 f"увеличьте --capital.")
    actual_notional = float(perp_qty) * perp_price
    spot_qty = round_qty(actual_notional / spot_price, qty_step(skey, "spot"))
    if spot_qty <= 0:
        sys.exit(f"Спот-количество меньше шага лота {skey} — увеличьте --capital.")
    notional = actual_notional
    margin = notional / cli.leverage

    # --- проверка стакана обеих ног до отправки ордеров (пункт плана 1) ---
    bad = check_liquidity(cli, symbol, "linear", notional, side="bid")
    if bad:
        sys.exit(f"вход отменён: {bad}")
    bad = check_liquidity(cli, skey, "spot", notional, side="ask")
    if bad:
        sys.exit(f"вход отменён: {bad}")

    entry_cost = r.get("entry_cost") or 0.0
    fees = notional * entry_cost / 100 * 0.5       # entry_cost — туда-обратно, вход = половина
    # Комиссия ВЫХОДА по той же схеме, что и вход (taker обеих ног):
    # без неё PnL сделки завышался вдвое по издержкам, а на этом числе
    # дальше учатся autotune/calibrate — бот получал ложный «эдж».
    # Ставки ног разные, держим их по отдельности.
    exit_spot_fee_pct = cli.spot_fee * 100          # % от спот-нотиона
    exit_perp_fee_pct = cli.perp_taker_fee * 100   # % от перп-нотиона

    if cli.mode == "paper":
        # симуляция: ордера НЕ отправляются, но ставки/цены/комиссии — реальные
        # (публичные данные mainnet). Сделка попадёт в память с mode=paper и
        # будет учить mainnet-пороги без риска капитала (см. learning_modes)
        log(f"PAPER вход {symbol}: ордера не отправляются "
            f"(симуляция на реальных данных и комиссиях mainnet)")

    if cli.mode in ("testnet", "mainnet"):
        if cli.mode == "mainnet":
            # плечо выставить ЯВНО: иначе новый символ возьмёт дефолт биржи
            # (обычно 10x), и расчёт маржи/риска разойдётся с --leverage
            try:
                client.set_leverage(symbol, cli.leverage)
                log(f"LEVERAGE {symbol} → {cli.leverage:g}x")
            except Exception as e:  # noqa: BLE001 — 110043 «not modified» и т.п.
                log(f"LEVERAGE {symbol}: {e} — продолжаю с текущим")
        taker_only_eff, mode_why = entry_taker_only(cli)
        if mode_why:
            log(mode_why)
        t_enter = int(time.time() * 1000)   # граница «ордеров ЭТОГО входа»
        # Нужны ОБЕ суммы: спот-нотион (USDT) + маржа перпа. Проверять только
        # маржу нельзя — при $1000 и 2x бот проходил проверку с $367 на
        # счёте, падал уже на спот-ноге и откатывал открытый шорт.
        # В портфеле счёт уже занят другими парами: считаем СВОБОДНЫЙ остаток.
        need = notional + margin
        avail = free_capital(cli, state, bal)
        if avail < need * 1.1:
            sys.exit(f"Недостаточно средств в {cli.mode}: нужно ≥ "
                     f"${need * 1.1:.2f} (спот ${notional:.2f} + маржа "
                     f"${margin:.2f}), свободно ${avail:.2f} из ${bal:.2f}.")

        # сперва пытаемся войти maker-лимитками (дешевле: perp 0.02% против
        # 0.055% taker), 6 сек на ногу; не исполнилась — фолбэк на маркет
        try:
            ob_p = orderbook(symbol, "linear", cli)
            ob_s = orderbook(skey, "spot", cli)
            # sell ложим по лучшему ask, buy — по лучшему bid (PostOnly не пересечёт стакан)
            # ключи стакана Bybit: "a" (asks) / "b" (bids)
            p_price = float(ob_p["a"][0][0]) if ob_p.get("a") else perp_price
            s_price = float(ob_s["b"][0][0]) if ob_s.get("b") else spot_price
        except Exception:  # noqa: BLE001 — стакан недоступен → сразу маркет
            p_price = s_price = 0.0

        # перп-нога: сначала maker-попытка, остаток — IOC по границе
        # (--max-slip-bps); вход НЕ доводится маркетом за границей: не вышло —
        # отменяем вход, а не покупаем по любой цене
        perp_done = 0.0
        if p_price > 0 and not taker_only_eff:
            try:
                perp_done = client.limit_order(symbol, "Sell", float(perp_qty),
                                               p_price, "linear",
                                               wait=cli.maker_wait)
            except OrderUnconfirmed as e:
                # Состояние перп-ноги на бирже НЕИЗВЕСТНО. Откатывать
                # нельзя (можно закрыть то, чего нет, и оставить открытым
                # то, что есть) — останавливаемся и зовём человека.
                notify(f"🚨 {e}. Вход остановлен. Проверьте позицию на бирже "
                       f"и решите: close или void --force. Автовход "
                       f"заблокирован на 30 мин.", key="order-unconfirmed",
                       min_gap=300)
                cli._entry_uncertain = True
                sys.exit(f"{e}")
        if perp_done < float(perp_qty):
            # добираем только ОСТАТОК (частичное исполнение лимитки + полный
            # маркет = перевес ноги — этот баг пойман на BTCUSDT testnet)
            rest = round_qty(float(perp_qty) - perp_done,
                             qty_step(symbol, "linear"))
            if rest > 0:
                try:
                    perp_done += fill_with_slippage(
                        client, cli, symbol, "Sell", float(rest), "linear",
                        ob=ob_p, allow_market=False)
                except RuntimeError as e:
                    # PartialFillError несёт исполненное частью IOC — без
                    # этого откат считал бы ногу пустой и оставил бы её
                    # открытой на бирже (голый шорт).
                    perp_done += getattr(e, "executed", 0.0)
                    if perp_done > 0:   # часть шорта уже стоит — снимаем его
                        back = round_qty(float(perp_done),
                                         qty_step(symbol, "linear"))
                        try:
                            client.market_order(symbol, "Buy", float(back),
                                                "linear", reduce_only=True)
                            rollback = f"частичный шорт {back:g} снят обратно"
                        except Exception as e2:  # noqa: BLE001
                            rollback = (f"ВНИМАНИЕ: шорт {back:g} остался "
                                        f"открытым! ({e2}) — закройте вручную")
                    else:
                        rollback = "ордера не отправлялись"
                    sys.exit(f"Вход отменён: {e}; откат: {rollback}")
        spot_done = 0.0
        if s_price > 0 and not taker_only_eff:
            spot_done = client.limit_order(skey, "Buy", float(spot_qty),
                                           s_price, "spot", wait=cli.maker_wait)
        if spot_done < float(spot_qty):
            rest = round_qty(float(spot_qty) - spot_done, qty_step(skey, "spot"))
            if rest > 0:
                try:
                    spot_done += fill_with_slippage(
                        client, cli, skey, "Buy", float(rest), "spot",
                        ob=ob_s, allow_market=False)
                except Exception as e:  # noqa: BLE001 — откатываем перп, чтобы не остался голый шорт
                    # спот мог исполниться частично — продаём и его, иначе
                    # на бирже остался бы голый лонг спота
                    spot_done += getattr(e, "executed", 0.0)
                    spot_rb = "спот не исполнен"
                    if spot_done > 0:
                        sback = round_qty(float(spot_done),
                                          qty_step(skey, "spot"))
                        try:
                            client.market_order(skey, "Sell", float(sback),
                                                "spot")
                            spot_rb = f"частичный спот {sback:g} продан"
                        except Exception as e3:  # noqa: BLE001
                            spot_rb = (f"ВНИМАНИЕ: спот {sback:g} остался! "
                                       f"({e3}) — закройте вручную")
                    try:
                        back = round_qty(float(perp_done), qty_step(symbol, "linear"))
                        client.market_order(symbol, "Buy", float(back), "linear", reduce_only=True)
                        rollback = "перп-шорт закрыт обратно"
                    except Exception as e2:  # noqa: BLE001
                        rollback = f"ВНИМАНИЕ: перп-шорт остался открытым! ({e2}) — закройте вручную"
                    sys.exit(f"Спот-нога не исполнена: {e}; откат: {rollback}; {spot_rb}")
        legs = (f"{'maker' if perp_done > 0 else 'taker'}:"
                f"{'maker' if spot_done > 0 else 'taker'}")
        print(f"Ордера на {cli.mode} исполнены (обе ноги, {legs}).")

        # фактические цены исполнения (взвешенно по всем ордерам этой ноги):
        # тикер в момент скана может отличаться на десятки пунктов от того,
        # по чему реально сделали — PnL обязан считаться от РЕАЛЬНЫХ цен.
        # Окно отсекается по startTime, а не «последними N ордерами».
        p_avg = avg_fill_since(client, "linear", symbol, t_enter)
        s_avg = avg_fill_since(client, "spot", skey, t_enter)
        if p_avg:
            perp_price = p_avg
            # нотион/маржа — от РЕАЛЬНОЙ цены шорта (funding начисляется от notional)
            notional = float(perp_qty) * p_avg
            margin = notional / cli.leverage
        if s_avg:
            spot_price = s_avg
        print(f"   Фактические цены: спот {spot_price:.6g}, перп {perp_price:.6g}")

        # биржевой стоп на перп-ногу: страховка на случай, когда бот не
        # работает (VPS упал, сеть, пауза) — локальные kill-switchы там бессильны
        arm_exchange_stop(client, {"symbol": symbol, "perp_entry": perp_price,
                                   "perp_qty": float(perp_qty),
                                   "margin": margin}, cli)

    pos = {
        "mode": cli.mode,
        "symbol": symbol,
        "spot_symbol": skey,
        "mult": mult,
        "notional": notional,
        "margin": margin,
        "leverage": cli.leverage,
        "spot_qty": float(spot_qty),
        "perp_qty": float(perp_qty),
        "spot_entry": spot_price,
        "perp_entry": perp_price,
        "fees_usd": fees,
        "entry_cost_pct": entry_cost,
        # ставки выхода для оценки PnL: комиссии taker по каждой ноге, % нотиона
        "exit_spot_fee_pct": exit_spot_fee_pct,
        "exit_perp_fee_pct": exit_perp_fee_pct,
        "apy30": (r.get("w30") or {}).get("avg_apy"),
        "basis_entry": r.get("basis"),
        "opened_ms": int(time.time() * 1000),
        "funding_accrued": 0.0,
        # счётчик успешных (положительных) выплат для hold-to-N авто-выхода
        "payouts": 0,
        # первая выплата придёт ровно на next_funding_ms; "−1" чтобы она
        # засчиталась один раз (строгое > в update_funding)
        "last_funding_ms": (r.get("next_funding_ms") or int(time.time() * 1000)) - 1,
        "neg_streak": 0,
        "capital": capital,
        # для памяти: как входили (maker/taker по ногам) и с каким проскальзыванием
        "legs": (f"{'maker' if perp_done > 0 else 'taker'}:"
                 f"{'maker' if spot_done > 0 else 'taker'}")
                 if cli.mode in ("testnet", "mainnet") else None,
        "slip_bps": round((perp_price / (r.get("perp_ask") or perp_price) - 1) * 1e4, 1)
                    if cli.mode in ("testnet", "mainnet")
                    and (r.get("perp_ask") or 0) > 0 else None,
        "fast_apy": r.get("cur_apy"),
        "interval_h": r.get("interval_h"),   # для памяти: платилась ли сделка
    }
    # в портфель: список + зеркало первой позиции в legacy-поле position
    set_positions(state, positions_of(state) + [pos])
    state["history"].append({"action": "enter", "ts": pos["opened_ms"],
                             "symbol": symbol, "mode": cli.mode})
    save_state(state)

    # Проверка прогноза памяти: обещала ли память этому символу доходность?
    if memory is not None:
        st = memory.symbol_stats(memory.load_memory(), symbol)
        if st["n"]:
            log(f"MEMORY: {symbol} — {st['n']} прошлых сделок, средний "
                f"{st['avg_pct']:+.2f}%, win {st['win_share']:.0%}")

    be = r.get("breakeven_shown")
    print(f"\n✅ ВХОД {cli.mode.upper()}: {symbol}")
    print(f"   Спот:  покупка {spot_qty:.6f} {skey} по {spot_price:.6g}")
    print(f"   Перп:  шорт {perp_qty:.6f} по {perp_price:.6g} (плечо {cli.leverage}x)")
    print(f"   Нотион ${notional:.2f} | маржа перпа ${margin:.2f} | "
          f"вход ~{entry_cost:.2f}% (${fees:.2f})")
    print(f"   APY 30д {pos['apy30']:+.1f}%" if pos["apy30"] else "   APY 30д н/д")
    print(f"   Окупаемость: {be:.1f} дн." if be else "   Окупаемость: ∞")
    print(f"   Состояние: {STATE_FILE}")
    log(f"ENTER {cli.mode} {symbol} notional=${notional:.2f} "
        f"leverage={cli.leverage}x apy30={pos['apy30']}")
    notify(f"🟢 ВХОД {cli.mode.upper()} {symbol}: нотион ${notional:.2f}, "
           f"APY30 {pos['apy30']:+.1f}%" if pos["apy30"] else
           f"🟢 ВХОД {cli.mode.upper()} {symbol}: нотион ${notional:.2f}")
    return pos


def cmd_enter(cli):
    state = load_state()
    if STATE_LOST:
        sys.exit("position.json был повреждён и сброшен — вход заблокирован. "
                 "Проверьте позицию на бирже; если её нет, удалите "
                 f"{STATE_FILE}.bak.")
    max_pos = max(1, int(getattr(cli, "max_positions", 1) or 1))
    plist = positions_of(state)
    if len(plist) >= max_pos:
        sys.exit(f"Портфель заполнен: {len(plist)}/{max_pos} пар "
                 f"({', '.join(str(p.get('symbol')) for p in plist)}) — "
                 f"сначала close (или увеличьте --max-positions).")
    open_syms = {p.get("symbol") for p in plist}

    if cli.symbol:
        if cli.symbol in open_syms:
            sys.exit(f"{cli.symbol} уже в портфеле.")
        now_ms = int(time.time() * 1000)
        fargs = build_filter_args(cli)
        tickers = {t["symbol"]: t for t in fetch_linear_tickers()}
        if cli.symbol not in tickers:
            sys.exit(f"{cli.symbol} не найден среди перпов Bybit.")
        r = analyze_symbol(tickers[cli.symbol], now_ms)
        enrich(r, fetch_spot_quotes(), fargs)
        if not r.get("verdict_ok") and not cli.force:
            sys.exit(f"{cli.symbol} не проходит фильтр: {r.get('verdict')}. "
                     f"Принудительно: --force")
    else:
        rows = [row for row in scan(cli) if row["symbol"] not in open_syms]
        if not rows:
            sys.exit("Нет символов, проходящих фильтр. Скажите --symbol или ждите.")
        r = rows[0]

    return open_position(cli, r)


def current_pnl(pos: dict, feed=None) -> tuple[float, float, float]:
    """(цена спота, цена перпа, pnl$) по живым тикерам.
    feed — WS-лента: перп берём из пуша (markPrice → lastPrice), спот bid —
    из спот-ленты; чего-то нет в ленте — добираем REST-запросом ТОЙ ЖЕ
    среды, где живёт позиция (testnet-ставки/цены отличаются от mainnet)."""
    perp_now = spot_now = 0.0
    mode = pos.get("mode") or "paper"
    mult = float(pos.get("mult") or 1.0)
    # живую ленту берём ТОЛЬКО пока соединение alive: после обрыва feed.linear()
    # отдаёт последний закэшированный пуш, и все kill-switchы (max-loss,
    # маржа, дельта) молча считались бы по замороженной цене.
    if feed is not None and feed.alive():
        t = feed.linear(pos["symbol"])
        if t:
            perp_now = num(t.get("markPrice")) or num(t.get("lastPrice"))
        s = feed.spot(pos["spot_symbol"])
        if s:
            spot_now = num(s.get("bid1Price"))
    if perp_now <= 0:   # лента отвалилась/символ не в подписке — старый REST-путь
        tickers = {t["symbol"]: t for t in market_get_tickers(mode)}
        price = tickers.get(pos["symbol"], {}).get("price") or 0.0
        if price <= 0:
            # символ делистнут/нет в тикерах: лучше честный ноль, чем падать
            # KeyError из decide_exit в «ошибку цикла» раз в секунду
            return 0.0, 0.0, -abs(pos.get("fees_usd") or 0.0)
        perp_now = price
        spot_now = 0.0
    if spot_now <= 0:
        s_bid, s_ask = market_spot_quotes(mode).get(pos["spot_symbol"], (0, 0))
        # ФОЛБЭК С МНОЖИТЕЛЕМ: для 1000PEPEUSDT цена перпа в 1000 раз выше
        # спотовой, а spot_qty посчитан в базовых монетах. Подстановка цены
        # перпа как «цены спота» давала фантомный PnL в тысячи раз больше
        # реального и срывала все kill-switchы.
        spot_now = s_bid or (perp_now / mult if mult else 0.0)

    spot_pnl = (spot_now - pos["spot_entry"]) * pos["spot_qty"]
    # notional = qty × price (проверено по OIvalue/OI у биржи) — множитель mult не нужен
    perp_pnl = (pos["perp_entry"] - perp_now) * pos["perp_qty"]
    return spot_now, perp_now, exit_adjusted_pnl(pos, spot_now, perp_now,
                                                spot_pnl + perp_pnl)


def market_get_tickers(mode: str) -> list[dict]:
    """Линейные тикеры СРЕДЫ позиции (testnet ≠ mainnet по ценам)."""
    if mode == "testnet":
        res = market_api_get("/v5/market/tickers", {"category": "linear"},
                             mode=mode)
        out = []
        for t in res.get("list", []):
            try:
                out.append({"symbol": t["symbol"],
                            "price": float(t.get("lastPrice") or 0)})
            except (ValueError, TypeError, KeyError):
                continue
        return out
    return fetch_linear_tickers()


def market_spot_quotes(mode: str) -> dict[str, tuple[float, float]]:
    """Спот-стакан СРЕДЫ позиции."""
    if mode != "testnet":
        return fetch_spot_quotes()
    res = market_api_get("/v5/market/tickers", {"category": "spot"}, mode=mode)
    out = {}
    for t in res.get("list", []):
        try:
            out[t["symbol"]] = (float(t.get("bid1Price") or 0),
                                float(t.get("ask1Price") or 0))
        except (ValueError, TypeError, KeyError):
            continue
    return out


def exit_cost_usd(pos: dict, spot_now: float, perp_now: float) -> float:
    """Комиссия ВЫХОДА в $ по фактическим нотионам ног.
    Комиссии ног РАЗНЫЕ (спот 0.10%, перп 0.055%), поэтому считаются от
    своих нотионов — сумма нотионов вдвое больше одного и дала бы двойной
    вычет. Старые позиции без exit_*_fee_pct — дефолты non-VIP."""
    spot_rate = pos.get("exit_spot_fee_pct")
    perp_rate = pos.get("exit_perp_fee_pct")
    if spot_rate is None or perp_rate is None:
        spot_rate = DEFAULT_SPOT_FEE * 100
        perp_rate = DEFAULT_PERP_TAKER_FEE * 100
    return (spot_now * pos["spot_qty"] * float(spot_rate)
            + perp_now * pos["perp_qty"] * float(perp_rate)) / 100.0


def exit_adjusted_pnl(pos: dict, spot_now: float, perp_now: float,
                      gross_after_entry: float) -> float:
    """PnL минус комиссия выхода (не только входа).
    fees_usd покрывает лишь половину круга — entry_cost в enrich() считается
    «туда-обратно», то есть комиссии входа. Без вычитания выхода PnL каждой
    сделки был завышен примерно вдвое по издержкам, и на этих числах училась
    память бота (autotune/calibrate)."""
    return (gross_after_entry - pos["fees_usd"]
            - exit_cost_usd(pos, spot_now, perp_now))


def cmd_status(cli):
    if getattr(cli, "strategy", "funding") == "scalp":
        return cmd_scalp_status(cli)
    state = load_state()
    plist = positions_of(state)
    if not plist:
        print("Позиции нет. Команда: scan / enter.")
        return
    total_pnl = 0.0
    for i, pos in enumerate(plist, 1):
        spot_now, perp_now, pnl = current_pnl(pos)
        total_pnl += pnl
        held_h = (time.time() * 1000 - pos["opened_ms"]) / 3_600_000
        if len(plist) > 1:
            print(f"--- [{i}/{len(plist)}] {pos['symbol']} ---")
        print(f"Позиция {pos['symbol']} ({pos['mode']}), открыта {held_h:.1f} ч назад")
        print(f"  Спот:  {pos['spot_entry']:.6g} → {spot_now:.6g}")
        print(f"  Перп:  {pos['perp_entry']:.6g} → {perp_now:.6g} (шорт)")
        print(f"  Funding накоплено: ${pos['funding_accrued']:+.4f}")
        print(f"  Выплат собрано: {pos.get('payouts', 0)}")
        print(f"  PnL (после комиссий): ${pnl:+.4f} "
              f"({pnl / pos['capital'] * 100:+.2f}% капитала)")
        print(f"  Отрицательных ставок подряд: {pos['neg_streak']}")
        if pos.get("stop_armed") and pos.get("stop_price"):
            sp = pos["stop_price"]
            print(f"  Стоп биржи (шорт): {sp:.6g} "
                  f"(+{(sp / pos['perp_entry'] - 1) * 100:.2f}% к входу) — "
                  f"защита, если бот не работает")
        elif pos.get("stop_armed") is False:
            print("  Стоп биржи: НЕ выставлен — работают только локальные "
                  "kill-switchы (VPS должен быть стабилен)")
    if len(plist) > 1:
        notional = sum(float(p.get("notional") or 0) for p in plist)
        max_pos = max(1, int(getattr(cli, "max_positions", 1) or 1))
        free = free_capital(cli, state)
        note = (f" (капитал на пару ${float(cli.capital):.2f})"
                if max_pos > 1 else " — портфель выключен (--max-positions 1)")
        print(f"---\nПортфель: {len(plist)}/{max_pos} пар, нотион "
              f"${notional:.2f}, PnL ${total_pnl:+.4f}")
        print(f"  Свободно под новую пару: ${free:.2f}{note}")


def market_api_get(path: str, params: dict | None = None, timeout: int = 15,
                   mode: str = "paper"):
    """Публичный GET к рынку той среды, где живёт позиция: mainnet для paper,
    testnet для testnet. Ставки funding у сред расходятся (у testnet свои),
    поэтому история выплат позиции обязана читаться из её среды."""
    base = TESTNET_BASE if mode == "testnet" else PAPER_BASE
    url = base + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "funding-monitor/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode())
    if data.get("retCode") != 0:
        raise RuntimeError(f"Bybit API error {data.get('retCode')}: {data.get('retMsg')}")
    return data["result"]


# Отметка «историю funding уже смотрели» — вне position.json: символ → мс.
_FUNDING_CHECKED: dict[str, int] = {}


def update_funding(pos: dict):
    """Начислить funding за прошедшие интервалы и обновить neg_streak.
    История — из среды позиции (testnet-ставки отличаются от mainnet:
    иначе выплатам/выходам считались бы по чужому рынку — поймано живьём
    на ETHUSDT testnet: neg_streak=2 по mainnet при стабильно +0.0025% на testnet)."""
    hist = market_api_get("/v5/market/funding/history",
                          {"category": "linear", "symbol": pos["symbol"],
                           "limit": 200},
                          mode=pos.get("mode") or "paper")["list"]
    events = sorted(
        ((int(h["fundingRateTimestamp"]), float(h["fundingRate"])) for h in hist),
        key=lambda x: x[0],
    )
    # funding получает шорт-нога при положительной ставке.
    # Идемпотентно: строгое " > " и ts <= now — иначе одна и та же выплата
    # считалась бы заново на каждом вызове (старый баг с >=).
    now_ms = int(time.time() * 1000)
    last = pos.get("last_funding_ms") or 0
    for ts, rate in events:
        if last < ts <= now_ms:
            pos["funding_accrued"] += rate * pos["notional"]
            # положительная ставка = шорт-нога ПОЛУЧИЛА выплату — считаем
            # её для hold-to-N (отрицательные ловит neg_streak, нулевые не выплаты)
            if rate > 0:
                pos["payouts"] = pos.get("payouts", 0) + 1
            last = ts
    pos["last_funding_ms"] = last
    if events:
        # streak считаем по последним закрытым интервалам
        recent = [r for ts, r in events if ts <= now_ms]
        streak = 0
        for rate in reversed(recent):
            if rate < 0:
                streak += 1
            else:
                break
        pos["neg_streak"] = streak


def decide_exit(pos: dict, cli, feed=None, fresh: bool = False) -> tuple[bool, str]:
    """Правила выхода: возвращает (надо_ли_выходить, причина).
    История funding опрашивается не чаще раза в 30 с (свежая всегда при
    fresh=True — командные вызовы); ценовые проверки при feed идут по
    пушам — на каждом тике цикла."""
    now_ms = int(time.time() * 1000)
    # отметка времени держится в памяти процесса, а НЕ в позиции: поле
    # _funding_checked_ms попадало в position.json и health-check/dashboard
    # видели служебный мусор в состоянии сделки
    if fresh or now_ms - _FUNDING_CHECKED.get(pos.get("symbol", "?"), 0) >= 30_000:
        update_funding(pos)
        _FUNDING_CHECKED[pos.get("symbol", "?")] = now_ms
    if pos["neg_streak"] >= cli.exit_neg:
        return True, (f"funding отрицательный {pos['neg_streak']} интервалов "
                      f"подряд (порог {cli.exit_neg})")
    # hold-to-N: фиксируем прибыль после N успешных выплат — цель удержания
    # достигнута, дальше лишь растёт риск разворота ставки (0 — выключено;
    # у позиций, открытых до этой фичи, счётчик добирается по факту выплат)
    hp = int(getattr(cli, "hold_payouts", 0) or 0)
    if hp > 0 and pos.get("payouts", 0) >= hp:
        return True, (f"собрано {pos['payouts']} успешных выплат "
                      f"(hold-to-N = {hp}) — фиксируем прибыль")
    spot_now, perp_now, pnl = current_pnl(pos, feed=feed)
    if pnl < 0 and abs(pnl) > pos["capital"] * cli.max_loss / 100:
        return True, (f"убыток ${pnl:.2f} превысил {cli.max_loss}% "
                      f"капитала")
    # дельта-контроль: расхождение нотионов ног (хедж слабеет: ногу могли
    # снять неттингом, либо базис ушёл далеко). Порог настраивается:
    # на testnet котировки прыгают и жёсткий1% закрывал только что
    # открытую позицию через секунды (живой случай XRPUSDT 18:24)
    delta_pct = float(getattr(cli, "max_delta_pct", 1.0) or 0)
    v_spot = spot_now * pos["spot_qty"]
    v_perp = perp_now * pos["perp_qty"]
    if delta_pct > 0 and v_spot > 0 \
            and abs(v_spot - v_perp) / v_spot > delta_pct / 100:
        return True, (f"дельта разошлась: спот ${v_spot:.2f} vs перп ${v_perp:.2f} "
                      f"(>{delta_pct:g}%)")
    # контроль маржи перп-ноги: сколько % маржи съел нереализованный убыток
    # шорта (цена выросла). 100% = ликвидация; выходим заранее (пункт 6 плана).
    if cli.max_margin_risk > 0 and pos.get("margin"):
        perp_loss = (perp_now - pos["perp_entry"]) * pos["perp_qty"]  # >0 = убыток
        risk_pct = perp_loss / pos["margin"] * 100
        if risk_pct >= cli.max_margin_risk:
            return True, (f"маржа перп-ноги под риском: убыток ${perp_loss:.2f} = "
                          f"{risk_pct:.0f}% маржи ${pos['margin']:.2f} "
                          f"(порог {cli.max_margin_risk}%)")
    return False, ""


# --- контроль целостности позиции: биржа vs position.json -------------------
# Урок (живьём, ETHUSDT): посторонний ордер/ликвидация неттингом снимают
# нашу ногу, а position.json продолжает считать позицию открытой. Раз в
# --integrity-every секунд сверяем фактические ноги с записанными.
INTEGRITY_SPOT_TOL = 0.02   # комиссия входа берётся в базовой монете (~0.1%)


def position_mismatch(pos: dict, perp_qty: float | None,
                      spot_avail: float | None) -> str | None:
    """Расхождение биржи и position.json, или None если всё сходится.
    Значение None у источника = «не удалось узнать», такой источник в
    сравнении не участвует (неизвестность — не повод для алерта)."""
    problems = []
    if perp_qty is not None:
        want = float(pos.get("perp_qty") or 0)
        if want > 0 and perp_qty <= 0:
            problems.append(f"перп-нога исчезла (в json {want:g}, на бирже 0)")
        elif want > 0 and abs(perp_qty - want) > max(want * 0.02, 1e-9):
            problems.append(f"перп-нога {perp_qty:g} ≠ {want:g} из json")
    if spot_avail is not None:
        want = float(pos.get("spot_qty") or 0)
        if want > 0:
            low, high = want * (1 - INTEGRITY_SPOT_TOL), want * (1 + INTEGRITY_SPOT_TOL)
            if spot_avail < low:
                problems.append(f"спот-нога неполна ({spot_avail:g} из {want:g})")
            elif spot_avail > high:
                problems.append(f"спот-нога больше записанного "
                                f"({spot_avail:g} vs {want:g})")
    return "; ".join(problems) if problems else None


def integrity_transition(prev_bad: bool, prob: str | None,
                         symbol: str) -> tuple[bool, str | None]:
    """Алерт только на ПЕРЕХОДЕ состояния (анти-спам): расхождение — при
    первом обнаружении, подтверждение — при восстановлении; пока всё так же
    плохо/хорошо — молчим. Возвращает (новый_флаг, сообщение или None)."""
    if prob and not prev_bad:
        return True, (f"⚠ РАСХОЖДЕНИЕ ПОЗИЦИИ {symbol}: {prob} — "
                      f"position.json не совпадает с биржей")
    if not prob and prev_bad:
        return False, (f"✅ {symbol}: позиция снова совпадает с position.json")
    return prev_bad, None


def check_position_integrity(pos: dict, cli) -> str | None:
    """Сверить фактические ноги на бирже с position.json (только real-среда).
    Возвращает текст расхождения или None. Сетевой сбой/неизвестность — None:
    контроль никогда не должен ломать торговый цикл."""
    if pos.get("mode") not in ("testnet", "mainnet"):
        return None
    try:
        client = make_client(pos["mode"])
        perp_qty = client.position_qty(pos["symbol"])
        base = base_coin(pos["spot_symbol"])
        spot_avail = client.coin_available(base)
    except (Exception, SystemExit):  # noqa: BLE001
        # SystemExit отдельно: TestnetClient при отсутствии ключей делает
        # sys.exit(), а это BaseException — обычный except его не ловит,
        # и контроль целостности убил бы весь watch-цикл.
        return None
    return position_mismatch(pos, perp_qty, spot_avail)


def stop_loss_price(pos: dict, risk_pct: float) -> float:
    """Цена биржевого стопа для ШОРТ: цена входа × (1 + риск).

    Мы в шорте: убыток растёт при росте цены, поэтому стоп выше входа.
    risk_pct — сколько % маржи готовы отдать до стопа (по умолчанию
    --max-margin-risk, 70%). Держим запас до ликвидации: биржа ликвидирует
    при 100%, а наш локальный max_margin_risk выходил заранее — стоп
    должен срабатывать РАНЬШЕ, пока маржа ещё есть.

    Возвращает 0, если стоп неприменим (плохой вход/риск) — тогда
    вызывающий решает, ставить ли стоп вообще."""
    entry = float(pos.get("perp_entry") or 0)
    qty = float(pos.get("perp_qty") or 0)
    margin = float(pos.get("margin") or 0)
    if entry <= 0 or qty <= 0 or margin <= 0 or risk_pct <= 0:
        return 0.0
    loss_usd = margin * risk_pct / 100.0     # сколько $ теряем до стопа
    move_pct = loss_usd / (entry * qty)      # насколько цена должна уйти
    if move_pct <= 0:
        return 0.0
    # стоп не ближе, чем 0.5% от входа: иначе шум рынка выбивает ногу
    return entry * (1 + max(move_pct, 0.005))


def arm_exchange_stop(client, pos: dict, cli) -> float:
    """Выставить биржевой стоп на перп-ногу. Возвращает цену стопа (0 = нет).
    Отказ не должен ломать вход: логируем и едем на локальные kill-switchы,
    но помечаем позицию, что биржевой защиты нет (видно в status).
    --no-exchange-stop — не ставить (для отладки/тестов на бирже)."""
    if getattr(cli, "no_exchange_stop", False):
        pos["stop_armed"] = False
        return 0.0
    price = stop_loss_price(pos, float(getattr(cli, "max_margin_risk", 0) or 0))
    if price <= 0:
        return 0.0
    tick = price_tick(pos["symbol"], "linear")
    price = float(round_price(price, tick, "up"))
    try:
        client.set_trading_stop(pos["symbol"], stop_loss=price)
        log(f"СТОП БИРЖИ {pos['symbol']} @ {price:g} "
            f"(+{(price / pos['perp_entry'] - 1) * 100:.2f}% к входу)")
        pos["stop_price"] = price
        pos["stop_armed"] = True
        return price
    except Exception as e:  # noqa: BLE001 — нет ключей/стоп недоступен
        log(f"СТОП БИРЖИ не выставлен ({e}) — полагаемся на локальные "
            f"kill-switchы, VPS должен быть стабилен")
        pos["stop_armed"] = False
        return 0.0


def suggest_price_for(cli, symbol: str, category: str, side: str,
                      ob: dict | None) -> float:
    """Граница цены для IOC по текущему стакану (0 = защита выключена/нет данных)."""
    slip = float(getattr(cli, "max_slip_bps", 0) or 0)
    if slip <= 0 or not ob:
        return 0.0
    bb = float(ob["b"][0][0]) if ob.get("b") else 0.0
    ba = float(ob["a"][0][0]) if ob.get("a") else 0.0
    limit = bounded_limit_price(side, bb, ba, slip)
    if limit is None:
        return 0.0
    # Sell — граница округляется вниз, Buy — вверх: IOC остаётся исполнимым,
    # а не отклоняется из-за одного тика
    tick = price_tick(symbol, category)
    return float(round_price(limit, tick, "down" if side == "Sell" else "up"))


def fill_with_slippage(client, cli, symbol: str, side: str, qty: float,
                       category: str, ob: dict | None = None,
                       reduce_only: bool = False,
                       allow_market: bool = True) -> float:
    """Исполнить количество с защитой от проскальзывания.

    С --max-slip-bps > 0 сначала идёт IOC-лимитка по границе bid/ask:
    она либо исполнится по лучшим ценам стакана, либо не исполнится вовсе —
    но НЕ возьмёт цену за границей (это и есть защита).
    allow_market=True (ВЫХОД) — незакрытый остаток добирается маркетом с
    предупреждением: застрять в позиции опаснее, чем заплатить слиппедж.
    allow_market=False (ВХОД) — ничего не исполнилось → RuntimeError,
    вызывающий откатывает уже открытую ногу.
    max_slip_bps ≤ 0 или нет стакана → обычный маркет (поведение как раньше)."""
    q = round_qty(qty, qty_step(symbol, category))
    if q <= 0:
        return 0.0
    limit = suggest_price_for(cli, symbol, category, side, ob)
    if limit > 0:
        unit = "bps"
        try:
            done = client.ioc_order(symbol, side, float(q), limit, category,
                                    reduce_only=reduce_only)
        except Exception as e:  # noqa: BLE001 — биржа отклонила IOC — падаем в маркет/ошибку
            log(f"IOC {symbol} {side}: {e}")
            done = 0.0
        if done > 0:
            log(f"IOC {symbol} {side} {done:g}/{float(q):g} по границе {limit:g} "
                f"(лимит {getattr(cli, 'max_slip_bps', 0):g}{unit})")
            rest = float(q) - done
            if rest <= 0:
                return done
            if not allow_market:
                # ВХОД отменяем, но IOC успел исполнить часть — сообщаем
                # вызывающему объём, иначе откат посчитает ногу пустой и
                # на бирже останется голый шорт/лонг (пойманный баг).
                raise PartialFillError(
                    f"IOC исполнил {done:g} из {float(q):g} {symbol} — цена ушла "
                    f"за границу {getattr(cli, 'max_slip_bps', 0):g} bps", done)
            log(f"⚠ остаток {rest:g} {symbol}: IOC не набрал — добираю маркетом")
            notify(f"⚠ {symbol}: остаток {rest:g} добираю маркетом (IOC не хватило)")
            client.market_order(symbol, side, rest, category, reduce_only=reduce_only)
            return float(q)
        # IOC пусто: цена за границей
        if not allow_market:
            raise RuntimeError(
                f"{symbol} {side}: стакан ушёл за границу "
                f"{getattr(cli, 'max_slip_bps', 0):g} bps — вход отменён")
        log(f"⚠ {symbol} {side}: IOC не исполнился в границе — беру маркетом")
        notify(f"⚠ {symbol}: IOC не исполнился, беру маркетом (слиппедж не ограничен)")
    client.market_order(symbol, side, float(q), category, reduce_only=reduce_only)
    return float(q)


def learning_modes(mode: str) -> tuple[str, ...]:
    """Какие среды считать релевантными для обучения в этом режиме.

    paper — симуляция на РЕАЛЬНЫХ данных и комиссиях mainnet (симулируется
    только заполнение по границам bid/ask, ордера не отправляются), поэтому
    для mainnet он релевантен и даёт статистику без риска капитала: calibrate
    набирает сделки за дни вместо месяцев. testnet — синтетические ставки,
    в mainnet-обучение не попадает."""
    if mode == "mainnet":
        return ("mainnet", "paper")
    if mode == "paper":
        return ("paper", "mainnet")
    return ("testnet",)


def cmd_check(cli):
    state = load_state()
    plist = positions_of(state)
    if not plist:
        print("Позиции нет.")
        return
    for pos in plist:
        update_funding(pos)
        save_state(state)
        exit_now, reason = decide_exit(pos, cli, fresh=True)
        if exit_now:
            print(f"🔴 ВЫХОД {pos['symbol']}: {reason}")
        else:
            print(f"🟢 {pos['symbol']} держим (neg_streak={pos['neg_streak']}, "
                  f"payouts={pos.get('payouts', 0)}/{cli.hold_payouts}, "
                  f"funding ${pos['funding_accrued']:+.4f})")


def close_position(cli, state: dict, pos: dict) -> dict:
    """Закрыть ОДНУ позицию портфеля. Возвращает запись сделки (history).

    Вынесено из cmd_close: в портфеле закрывается конкретная пара (--symbol)
    или все по очереди (--all), и каждая пишется в историю/память отдельно.
    state разделяется с вызывающим: позиция убирается из портфеля здесь же.
    """
    # funding ДО расчёта итога: последний интервал мог выплатиться уже после
    # последней проверки — без этого close записывал бы в сделку заниженную
    # сумму, и память обучалась бы на неверных выплатах.
    update_funding(pos)
    spot_now, perp_now, pnl = current_pnl(pos)
    # базис на выходе — в лог: рядом с basis_entry видно, сколько съело окно
    if spot_now and perp_now:
        bex = (perp_now / (spot_now * (pos.get("mult") or 1.0)) - 1) * 100
        be_in = (f"{pos['basis_entry']:+.3f}%"
                 if pos.get("basis_entry") is not None else "н/д")
        log(f"BASIS {pos['symbol']} {pos['mode']} вход={be_in} выход={bex:+.3f}%")

    if pos["mode"] in ("testnet", "mainnet"):
        client = make_client(pos["mode"])
        # стакан берём у СРЕДЫ ПОЗИЦИИ: close может зваться с --mode paper
        # (CLI по умолчанию), и стакан mainnet дал бы неверную IOC-границу
        # для ордеров, уходящих на testnet.
        try:
            ob_p = orderbook(pos["symbol"], "linear", cli, mode=pos["mode"])
        except Exception:  # noqa: BLE001 — нет стакана → маркетом
            ob_p = None
        try:
            ob_s = orderbook(pos["spot_symbol"], "spot", cli, mode=pos["mode"])
        except Exception:  # noqa: BLE001
            ob_s = None
        t_exit = int(time.time() * 1000)   # граница «ордеров ЭТОГО выхода»
        # Снимаем биржевой стоп ДО закрытия ноги: висящий TP/SL-ордер,
        # привязанный к позиции, при закрытии сбрасывается биржей сам —
        # но если снять не успеем/сбой, он может выстрелить уже по СЛЕДУЮЩЕЙ
        # позиции (тот же символ) и закрыть её без нашего ведома.
        if pos.get("stop_armed"):
            try:
                client.set_trading_stop(pos["symbol"], stop_loss=0)
                log(f"стоп биржи снят {pos['symbol']}")
            except Exception as e:  # noqa: BLE001
                log(f"не удалось снять стоп биржи ({e}) — проверьте вручную")
                notify(f"⚠ Не удалось снять биржевой стоп {pos['symbol']} "
                       f"({e}) — он может сработать на новой позиции",
                       key="stop-clean-fail", min_gap=1800)
        # выход в обратном порядке: сначала перп (снять шорт), потом спот
        pq = client.position_qty(pos["symbol"])
        perp_closed = 0.0
        if pq is None or pq > 0:
            q_close = min(float(pos["perp_qty"]), pq) if pq else float(pos["perp_qty"])
            perp_closed = fill_with_slippage(client, cli, pos["symbol"], "Buy",
                                            q_close, "linear", ob=ob_p,
                                            reduce_only=True, allow_market=True)
        else:
            log(f"CLOSE {pos['symbol']}: перп-позиция уже 0 (внешнее "
                f"вмешательство/неттинг?) — закрываю только спот")
        # фактическая цена выхода читается ДО вызова второй ноги: если перп
        # не закрылся, sell спота сделает позицию направленной — об этом
        # должен быть явный алерт, а не тихая запись в сделку.
        p_exit_avg = avg_fill_since(client, "linear", pos["symbol"], t_exit)
        if pq and perp_closed < float(pos["perp_qty"]) - float(pq) * 0.01:
            notify(f"🚨 CLOSE {pos['symbol']}: шорт закрыт не полностью "
                   f"({perp_closed:g} из {float(pos['perp_qty']):g}) — "
                   f"проверьте позицию на бирже")
        # спот: комиссия на входе берётся в базовой монете — на счету её
        # меньше, чем spot_qty; продаём доступное, округлённое вниз до шага
        sq = float(pos["spot_qty"])
        base = base_coin(pos["spot_symbol"])
        avail = client.coin_available(base)
        if avail is not None and avail <= 0:
            # спота на бирже нет: продавать нечего, а биржа вернёт ошибку
            # по quantity. Молчаливый Sell здесь оставлял бы сделку в
            # position.json с закрытым перпом — фантом.
            log(f"CLOSE {pos['symbol']}: на бирже нет {base} (avail={avail:g}) — "
                f"спот-ногу не продаю")
            notify(f"⚠ CLOSE {pos['symbol']}: {base} на бирже нет "
                   f"(avail={avail:g}) — спот-нога осталась, разберитесь вручную")
            sq = 0.0
        elif avail is not None and avail > 0:
            sq = min(sq, avail)
        if sq > 0:
            try:
                fill_with_slippage(client, cli, pos["spot_symbol"], "Sell", sq,
                                   "spot", ob=ob_s, allow_market=True)
            except Exception as e:  # noqa: BLE001 — шорт уже снят, спот докладываем
                log(f"CLOSE-ошибка: спот-нога не исполнена ({e}) — повторяю")
                try:
                    client.market_order(pos["spot_symbol"], "Sell", sq, "spot")
                except Exception as e2:  # noqa: BLE001 — спот остался на бирже
                    notify(f"🚨 CLOSE {pos['symbol']}: спот {sq:g} НЕ продан "
                           f"({e2}) — закройте вручную")
        print("Ордера на закрытие исполнены.")

        # PnL пересчитываем по ФАКТИЧЕСКИМ ценам выхода: тикер до отправки
        # ордеров на десятки пунктов расходится с реальным исполнением, и
        # именно эти цены уходят в память/статистику.
        p_avg = p_exit_avg or avg_fill_since(client, "linear", pos["symbol"],
                                             t_exit)
        s_avg = avg_fill_since(client, "spot", pos["spot_symbol"], t_exit)
        perp_now = p_avg or perp_now
        spot_now = s_avg or spot_now
        spot_pnl = (spot_now - pos["spot_entry"]) * pos["spot_qty"]
        perp_pnl = (pos["perp_entry"] - perp_now) * pos["perp_qty"]
        pnl = exit_adjusted_pnl(pos, spot_now, perp_now, spot_pnl + perp_pnl)
        # цены выхода — для точной разбивки PnL на цену/комиссии в записи
        pos["_spot_now"], pos["_perp_now"] = spot_now, perp_now
    else:
        log(f"PAPER выход {pos['symbol']}: ордера не отправляются "
            f"(симуляция, сделка идёт в память с mode=paper)")
        # цены из current_pnl выше — они и есть «выход» для симуляции
        pos["_spot_now"], pos["_perp_now"] = spot_now, perp_now

    total = pnl + pos["funding_accrued"]
    held_h = (time.time() * 1000 - pos["opened_ms"]) / 3_600_000
    # Разбивка PnL на составляющие — для панели и для понимания, ОТКУДА
    # пришли деньги: цена (должна быть ~0 у дельта-нейтрала), funding
    # (наш доход) и комиссии (издержки). Считаем из уже известных величин:
    # pnl = цена − комиссия_входа − комиссия_выхода.
    fees_in = float(pos.get("fees_usd") or 0.0)
    fees_out = (exit_cost_usd(pos, pos["_spot_now"], pos["_perp_now"])
                if pos.get("_spot_now") is not None else 0.0)
    price_pnl = pnl + fees_in + fees_out      # PnL по цене БЕЗ комиссий
    rec = {
        "action": "close", "ts": int(time.time() * 1000),
        "symbol": pos["symbol"], "mode": pos["mode"],
        "held_hours": round(held_h, 1),
        "pnl_excl_funding": round(pnl, 4),
        "funding": round(pos["funding_accrued"], 4),
        "total": round(total, 4),
        "return_pct": round(total / pos["capital"] * 100, 3),
        "notional": round(float(pos.get("notional") or 0.0), 2),
        "capital": round(float(pos.get("capital") or 0.0), 2),
        "fees_in": round(fees_in, 4),
        "fees_out": round(fees_out, 4),
        "fees_total": round(fees_in + fees_out, 4),
        "price_pnl": round(price_pnl, 4),
        "apy30": pos.get("apy30"),
        "basis_entry": pos.get("basis_entry"),
        "legs": pos.get("legs"),
    }
    # --- санитария записи ---------------------------------------------------
    # Дельта-нейтральная пара не имеет права заработать заметный плюс за
    # минуты без единой выплаты funding: источнику дохода неоткуда взяться.
    # Такой результат означает битые цены ног (записана цена чужого снимка)
    # и раньше уходил в PnL панели и в обучение памяти. Помечаем — панель и
    # память такие записи не считают (trade_sanity).
    susp = sanity.mark(rec)
    # Ingest-пометка по причине аномалии: цена ноги записана со слипом
    # крупнее SLIP_BPS_LIMIT при мгновенном закрытии без выплат funding
    # (правило выше ловит следствие, это — причину). Подробности в trade_sanity.
    if not susp:
        slip_why = sanity.slip_reason(rec)
        if slip_why:
            rec[sanity.SUSPICIOUS] = slip_why
            susp = slip_why
    # --- сверка с фактом по бирже (только реальные среды) ------------------
    # Расчёт «ставка × нотион» — модель. Бот на ней учится (calibrate/
    # autotune), поэтому модель обязана сверяться с деньгами: выплаты
    # зависят от позиции на момент снимка ставки, частичных закрытий и
    # комиссий. Расхождение — сигнал, что формулы разъехались с биржой.
    if pos["mode"] in ("testnet", "mainnet"):
        try:
            flows = make_client(pos["mode"]).flows_since(
                pos["symbol"], int(pos["opened_ms"]) - 60_000)
        except Exception:  # noqa: BLE001 — сбой сверки не должен ломать выход
            flows = None
        if flows and flows["n"]:
            calc = pos["funding_accrued"]
            fact = flows["funding"]
            diff = fact - calc
            # допуск: 5 центов или 10% от расчёта, что больше
            tol = max(0.05, abs(calc) * 0.10)
            rec["funding_fact"] = round(fact, 4)
            rec["funding_diff"] = round(diff, 4)
            if abs(diff) > tol:
                log(f"СВЕРКА {pos['symbol']} {pos['mode']}: расчётный funding "
                    f"${calc:+.4f} vs фактический ${fact:+.4f} "
                    f"(расхождение ${diff:+.4f}, выплат {flows['n']})")
                notify(f"⚠ Сверка funding {pos['symbol']}: расчёт ${calc:+.2f} "
                       f"vs факт ${fact:+.2f} (${diff:+.2f}). Если расхождение "
                       f"системное — формулы устарели.",
                       key=f"funding-diff-{pos['symbol']}", min_gap=900)
            else:
                log(f"СВЕРКА {pos['symbol']} {pos['mode']}: сходится "
                    f"(расчёт ${calc:+.4f} / факт ${fact:+.4f}, "
                    f"выплат {flows['n']})")
    state["history"].append(rec)
    drop_position(state, pos)
    save_state(state)

    log(f"CLOSE {pos['symbol']} {pos['mode']} held={held_h:.1f}h "
        f"pnl=${pnl:+.4f} funding=${pos['funding_accrued']:+.4f} "
        f"total=${total:+.4f} ({total / pos['capital'] * 100:+.2f}%)")
    notify(f"🔴 ЗАКРЫТО {pos['symbol']} ({pos['mode']}, {held_h:.1f} ч): "
           f"итого {total:+.2f} $ ({total / pos['capital'] * 100:+.2f}%)")
    if susp:
        # Громко и в лог, и в Telegram: это не «удачная сделка», это дефект
        # учёта (или чьё-то вмешательство на бирже) — разбирать надо сразу.
        notify(f"🚫 {pos['symbol']}: закрытие помечено подозрительным — {susp}. "
               f"Из PnL/статистики/памяти исключено. Разбор: "
               f"sanity", key=f"suspicious-{pos['symbol']}", min_gap=60)

    # сделка → в память самообучения (сделки/серии убытков/уроки)
    if memory is not None:
        mem = memory.load_memory()
        extra = {k: pos[k] for k in ("legs", "slip_bps", "fast_apy",
                                     "apy30", "entry_cost_pct", "basis_entry",
                                     "interval_h", "notional")
                 if pos.get(k) is not None}
        if susp:
            extra["suspicious"] = susp     # пометка видна и в памяти
        _ok, lesson = memory.record_trade(
            mem, pos["symbol"], pos["mode"], held_h, pnl,
            pos["funding_accrued"], total, capital=pos["capital"], extra=extra)
        lm = learning_modes(pos["mode"])
        calib_lesson = memory.calibrate(mem, preset=cli.preset, mode=lm)
        # недельный автотюнинг: раз в 7 дней пороги пересматриваются по
        # фактической прибыли сделок; предложения — в лог и Telegram
        tune_lesson = memory.autotune(mem, preset=cli.preset, mode=lm)
        mode_lesson = memory.learn_entry_mode(mem, mode=lm)
        memory.save_memory(mem)
        for l in (lesson, calib_lesson):
            if l:
                log(f"MEMORY урок: {l}")
                notify(f"🧠 {l}")
        if tune_lesson:
            log(f"MEMORY автотюнинг: {tune_lesson}")
            notify(f"🧠 {tune_lesson}")
        if mode_lesson:
            log(f"MEMORY {mode_lesson}")
            notify(f"🧠 {mode_lesson}")
    print(f"\nЗакрыто {pos['symbol']}: удержали {held_h:.1f} ч")
    print(f"  PnL по цене (после комиссий): ${pnl:+.4f}")
    print(f"  Funding собран:               ${pos['funding_accrued']:+.4f}")
    print(f"  ИТОГО:                        ${total:+.4f} "
          f"({total / pos['capital'] * 100:+.2f}% капитала)")
    return rec


def close_targets(state: dict, cli) -> list[dict]:
    """Какие позиции закрывать по флагам --symbol / --all.

    Без флагов — только если позиция ОДНА (прежнее поведение). В портфеле
    «закрой позицию» обязано быть однозначным: случайная пара вместо
    выбранной — это не «маленькая неточность», а реальные ордера.
    """
    plist = positions_of(state)
    if not plist:
        sys.exit("Позиции нет.")
    symbol = getattr(cli, "symbol", None)
    if getattr(cli, "close_all", False):
        return list(plist)
    if symbol:
        pos = find_position(state, symbol=symbol)
        if pos is None:
            sys.exit(f"В портфеле нет {symbol} (открыты: "
                     f"{', '.join(str(p.get('symbol')) for p in plist)}).")
        return [pos]
    if len(plist) > 1:
        sys.exit("Открыто несколько позиций ("
                 f"{', '.join(str(p.get('symbol')) for p in plist)}): "
                 "укажите --symbol SYMBOL или --all.")
    return plist


def cmd_close(cli):
    """Закрыть позицию(и): одну по --symbol, все — по --all."""
    state = load_state()
    targets = close_targets(state, cli)
    recs = []
    for pos in targets:
        recs.append(close_position(cli, state, pos))
    if len(recs) > 1:
        total = sum(float(r.get("total") or 0) for r in recs)
        fund = sum(float(r.get("funding") or 0) for r in recs)
        print(f"\n===== ПОРТФЕЛЬ ЗАКРЫТ: {len(recs)} пар =====")
        for r in recs:
            print(f"  {r['symbol']:<12} итого {float(r.get('total') or 0):+8.2f} $"
                  f" (цена {float(r.get('pnl_excl_funding') or 0):+7.2f} + "
                  f"funding {float(r.get('funding') or 0):+7.3f})")
        print(f"  ИТОГО: {total:+.2f} $ (funding {fund:+.3f} $)")
        left = positions_of(state)
        if left:
            print(f"  Осталось в портфеле: "
                  f"{', '.join(str(p.get('symbol')) for p in left)}")
    return recs


def fast_candidates(cli, feed=None) -> list[dict]:
    """Быстрый отсев по текущим тикерам: 2 запроса, БЕЗ истории ставок.
    Определяет кандидатов по текущему APY и базису за ~1 секунду.
    feed — WS-лента (по tickers.* пуши ~100 мс): тогда REST в цикле нет,
    все данные из памяти; лента отвалилась — фолбэк на REST."""
    if feed is not None and feed.alive():
        return fast_candidates_ws(cli, feed)
    tickers = fetch_linear_tickers()
    spot = fetch_spot_quotes()
    rows = []
    for t in tickers:
        if t["turnover24h"] < cli.min_volume * 1e6 or t["oi_value"] < cli.min_oi * 1e6:
            continue
        cur_apy = apy_from_rate(t["funding_rate"], t["interval_h"])
        if cur_apy <= 0 or cur_apy < cli.fast_apy:
            continue
        skey, mult = spot_match(t["symbol"], spot)
        if not skey:
            continue
        sb, sa = spot[skey]
        if sb <= 0 or sa <= 0:
            continue
        pmid = ((t["perp_bid"] + t["perp_ask"]) / 2
                if t["perp_bid"] and t["perp_ask"] else t["price"])
        basis = (pmid / ((sb + sa) / 2 * mult) - 1) * 100
        if basis < cli.min_basis:
            continue
        t["cur_apy"] = cur_apy
        t["basis_fast"] = basis
        rows.append(t)
    rows.sort(key=lambda x: x["cur_apy"], reverse=True)
    return rows[: cli.fast_top]


def fast_candidates_ws(cli, feed) -> list[dict]:
    """fast_candidates из WS-ленты: тот же фильтр, но 0 запросов REST.
    fundingIntervalHours в пушах нет — интервал берём из REST-базы
    (feed.intervals), без неё считаем 8ч — консервативнее, занизить APY
    для 1h-монет лучше, чем завысить."""
    rows = []
    spot = feed.spot_quotes()
    for sym, t in feed.linear_items().items():
        t["symbol"] = sym
        if num(t.get("turnover24h")) < cli.min_volume * 1e6 \
                or num(t.get("openInterestValue")) < cli.min_oi * 1e6:
            continue
        rate = num(t.get("fundingRate"))
        interval = feed.intervals.get(sym) or 8.0
        cur_apy = apy_from_rate(rate, interval)
        if cur_apy <= 0 or cur_apy < cli.fast_apy:
            continue
        bid, ask = num(t.get("bid1Price")), num(t.get("ask1Price"))
        skey, mult = spot_match(sym, spot)
        if not skey:
            continue
        sb, sa = spot[skey]
        if sb <= 0 or sa <= 0:
            continue
        pmid = (bid + ask) / 2 if bid > 0 and ask > 0 else num(t.get("lastPrice"))
        if pmid <= 0:
            continue
        basis = (pmid / ((sb + sa) / 2 * mult) - 1) * 100
        if basis < cli.min_basis:
            continue
        rows.append({
            "symbol": sym,
            "price": num(t.get("lastPrice")) or pmid,
            "perp_bid": bid, "perp_ask": ask,
            "funding_rate": rate, "interval_h": interval,
            "next_funding_ms": int(num(t.get("nextFundingTime"))),
            "turnover24h": num(t.get("turnover24h")),
            "oi_value": num(t.get("openInterestValue")),
            "cur_apy": cur_apy, "basis_fast": basis,
        })
    rows.sort(key=lambda x: x["cur_apy"], reverse=True)
    return rows[: cli.fast_top]


def start_feed(cli):
    """Поднять WS-ленту: linear по префильтр-топу, spot по их спот-парам.
    База (интервалы выплат + матчинг спот-пар) — 2 REST-запроса один раз.
    Возвращает TickerFeed или None (WS недоступен — режим REST)."""
    if TickerFeed is None:
        log("WS недоступен (bybit_ws.py не найден) — работаю по REST")
        return None
    try:
        rows = fetch_linear_tickers()   # разовая база: символы, интервалы, ликвидность
        spot = fetch_spot_quotes()      # разовая база: матчинг спот-пар
        cands = [t for t in rows
                 if t["turnover24h"] >= cli.min_volume * 1e6
                 and t["oi_value"] >= cli.min_oi * 1e6]
        cands.sort(key=lambda t: t["funding_rate"], reverse=True)
        cands = cands[: max(cli.prefilter, cli.fast_top)]
        lin_syms, spot_syms = [], []
        for t in cands:
            lin_syms.append(t["symbol"])
            skey, _mult = spot_match(t["symbol"], spot)
            if skey and skey not in spot_syms:
                spot_syms.append(skey)
        state = load_state()
        pos = state.get("position")
        if pos:
            if pos["symbol"] not in lin_syms:
                lin_syms.append(pos["symbol"])
            if pos.get("spot_symbol") and pos["spot_symbol"] not in spot_syms:
                spot_syms.append(pos["spot_symbol"])
        feed = TickerFeed(cli.mode, on_status=lambda m: log(m))
        feed.intervals.update({t["symbol"]: t["interval_h"] for t in rows})
        feed.start(lin_syms, spot_syms)
        log(f"WS лента запущена: {len(lin_syms)} linear + {len(spot_syms)} spot тем — "
            f"REST в цикле не нужен, реакции по пушам")
        return feed
    except Exception as e:  # noqa: BLE001 — без ленты бот не должен не запускаться
        log(f"WS не поднялся ({e}) — работаю по REST")
        return None


def deep_check(cli, cands: list[dict]) -> list[dict]:
    """Полная проверка кандидатов: история ставок + вердикт фильтра."""
    now_ms = int(time.time() * 1000)
    fargs = build_filter_args(cli)
    spot = fetch_spot_quotes()
    out = []
    for t in cands:
        r = analyze_symbol(t, now_ms)
        enrich(r, spot, fargs)
        if r.get("verdict_ok") and not spot_book_alive(cli, r.get("spot_symbol")):
            # фильтр прошёл, но исполнить вход некуда (на testnet у большинства
            # мелких пар стакан пуст): без этой проверки очередь кандидатов
            # (сортировка по APY) съедалась на отказах «стакан пуст», а до
            # жидкого символа бот не доходил часами
            r["verdict_ok"] = False
            r["verdict"] = "✗ спот-стакан пуст (исполнить вход некуда)"
        if r.get("verdict_ok"):
            why = not_executable(cli, r)
            if why:
                # то же на уровне спред/глубина: SUIUSDT со спредом 58%
                # формально имеет живой стакан, но вход туда отклоняется
                # в open_position — лучше отсечь в фильтре
                r["verdict_ok"] = False
                r["verdict"] = f"✗ неисполнимо: {why}"
        if r.get("verdict_ok"):
            out.append(r)
        time.sleep(0.05)
    out.sort(key=conservative_apy, reverse=True)
    return out


def cmd_watch(cli):
    """Автономный цикл: быстрый скан → авто-вход → контроль → авто-выход.
    С --ws данные тикеров приходят пушами (~100 мс) и живут в памяти:
    контроль позиции и риск-выходы проверяются каждую секунду без REST,
    поиск кандидатов — раз в interval с, сделки — по-прежнему REST."""
    if getattr(cli, "strategy", "funding") == "scalp":
        return cmd_watch_scalp(cli)
    auto = not cli.no_auto
    if cli.mode == "mainnet" and not getattr(cli, "confirm_live", False):
        # реальные деньги без осознанного подтверждения не открываем:
        # watch остаётся наблюдением (сигналы в лог/Telegram)
        auto = False
    feed = start_feed(cli) if cli.ws else None
    step = 1.0 if feed else max(1.0, float(cli.interval))   # такт цикла
    # --- самообучение: дочитываем bot.log и применяем память ---
    mem = None
    if memory is not None and not cli.no_learn:
        mem = memory.load_memory()
        cnt = memory.ingest_log(mem)
        # калибровка: прогноз APY vs факт по всем сделкам с прогнозом —
        # сдвигает пороги fast_apy/min_apy30, если прогноз систематически врёт
        lm = learning_modes(cli.mode)
        calib = memory.calibrate(mem, preset=cli.preset, mode=lm)
        # недельный автотюнинг: раз в 7 дней — по фактической прибыли сделок
        tune = memory.autotune(mem, preset=cli.preset, mode=lm)
        # сравнение maker/taker входов: prefer → open_position пропустит
        # maker-попытки, если память за taker
        mode_lesson = memory.learn_entry_mode(mem, mode=lm)
        memory.save_memory(mem)
        for line in memory.lessons(mem):
            log(f"MEMORY: {line}")
        if any(cnt.values()):
            log(f"MEMORY: память обновлена из лога: {cnt}")
        if calib:
            log(f"MEMORY калибровка: {calib}")
            notify(f"🧠 {calib}")
        if tune:
            log(f"MEMORY автотюнинг: {tune}")
            notify(f"🧠 {tune}")
        if mode_lesson:
            log(f"MEMORY {mode_lesson}")
            notify(f"🧠 {mode_lesson}")
    notify(f"🤖 WATCH start: mode={cli.mode} interval={cli.interval}s "
           f"auto={'on' if auto else 'off'} ws={'on' if feed else 'off'} "
           f"learn={'on' if mem else 'off'} preset={cli.preset} "
           f"capital=${cli.capital:.0f} leverage={cli.leverage}x "
           f"exit_neg={cli.exit_neg} max_loss={cli.max_loss}% "
           f"integrity={'off' if not getattr(cli, 'integrity_every', 0) else str(int(cli.integrity_every)) + 's'} "
           f"max_slip={getattr(cli, 'max_slip_bps', 0):g}bps")
    last_full = 0.0          # время последнего полного скана (36 символов)
    last_hb = 0.0            # heartbeat в лог раз в 60 сек
    fail_until = 0.0         # кулдаун после неудачного входа
    uncertain_until = 0.0   # блокировка входа: от биржи потерян ответ
    failed: dict[str, float] = {}  # символ → время, до которого его не пробуем
    last_search = 0.0        # с лентой поиск не чаще interval — цикл на пушиx
    last_save = 0.0          # и state пишем не чаще interval (там не меняется ничего ценового)
    last_integrity = 0.0     # сверка position.json с биржей (--integrity-every)
    last_report = 0.0        # таблица PnL каждые --report-every мин; 0 = сразу
    integrity_bad: dict[str, bool] = {}  # символ → было расхождение (алерт на переходе)
    if STATE_LOST:
        # файл был битым — вход заблокирован, но наблюдение продолжаем,
        # чтобы дать алерт в Telegram, а не молча стоять
        notify(f"🚨 position.json был повреждён и сброшен в пустое "
               f"({STATE_LOST[-1]}). Автовход ЗАБЛОКИРОН — возможно, на бирже "
               f"висит потерянная позиция. Проверьте и удалите {STATE_FILE}.bak.",
               key="state-lost", min_gap=3600)
    try:
        while True:
            t0 = time.time()
            # последняя ошибка итерации. Отдельная переменная, а не имя из
            # `except ... as e`: Python УДАЛЯЕТ это имя при выходе из блока
            # except, и `isinstance(e, ClockSkew)` ниже падало с
            # UnboundLocalError — то есть бот умирал на ПЕРВОЙ же итерации
            # (живой инцидент: systemd рестартовал его каждые ~25 с).
            loop_err: BaseException | None = None
            try:
                state = load_state()
                plist = positions_of(state)
                max_pos = max(1, int(getattr(cli, "max_positions", 1) or 1))
                # сверка с биржей — один раз за такт на ВСЕ пары, а не по таймеру
                # каждой (иначе каждая позиция сдвигала бы общий таймер)
                ievery = float(getattr(cli, "integrity_every", 0) or 0)
                check_integrity = ievery > 0 \
                    and time.time() - last_integrity >= ievery
                if check_integrity:
                    last_integrity = time.time()
                # таблица дохода/PnL каждые --report-every мин (лог + Telegram)
                last_report = maybe_pnl_report(state, cli, feed, last_report)
                # ежедневная сводка в --daily-report-at (баланс, день, неделя)
                maybe_daily_report(state, cli, feed)
                for pos in list(plist):
                    # --- позиция есть: контролируем ВЫХОД каждой пары ---
                    if feed is not None:
                        feed.ensure_spot([pos.get("spot_symbol")])
                    exit_now, reason = decide_exit(pos, cli, feed=feed)
                    if feed is None or exit_now \
                            or time.time() - last_save >= cli.interval:
                        save_state(state)
                        last_save = time.time()
                    # --- целостность: биржа vs position.json (по каждой паре) ---
                    sym = pos.get("symbol")
                    if check_integrity:
                        prob = check_position_integrity(pos, cli)
                        bad, imsg = integrity_transition(
                            integrity_bad.get(sym, False), prob, sym)
                        integrity_bad[sym] = bad
                        if imsg:
                            log(imsg)
                            notify(imsg)
                    if exit_now:
                        notify(f"⚠ ВЫХОД {sym}: {reason}")
                        # закрываем ИМЕННО эту пару: cmd_close без --symbol
                        # в портфеле отказался бы (и правильно)
                        close_position(cli, state, pos)
                        integrity_bad.pop(sym, None)
                        last_hb = 0.0
                    elif time.time() - last_hb >= 60:
                        last_hb = time.time()
                        _, _, pnl = current_pnl(pos, feed=feed)
                        log(f"HOLD {pos['symbol']} pnl=${pnl:+.2f} "
                            f"funding=${pos['funding_accrued']:+.4f} "
                            f"payouts={pos.get('payouts', 0)}/{cli.hold_payouts} "
                            f"neg={pos['neg_streak']}")
                if len(positions_of(state)) < max_pos:
                    # --- есть свободные слоты: ищем окно для новой пары ---
                    cand = None
                    cands: list = []
                    if feed is None or time.time() - last_search >= cli.interval:
                        last_search = time.time()
                        cands = fast_candidates(cli, feed=feed)
                        if cands:
                            if time.time() - last_hb >= 60:
                                last_hb = time.time()
                                log(f"fast: {len(cands)} кандидатов, "
                                    f"лидер {cands[0]['symbol']} "
                                    f"APY={cands[0]['cur_apy']:+.1f}%")
                        # память: отводим сожжённых и поднимаем проверенных
                        if mem is not None:
                            cands = [c for c in cands if not memory.avoid_symbol(mem, c["symbol"])]
                            cands = memory.prefer_order(mem, cands)
                        passed = deep_check(cli, cands)
                        # пары, которые уже в портфеле, второй раз не берём
                        held = {p.get("symbol") for p in positions_of(state)}
                        passed = [r for r in passed if r["symbol"] not in held]
                        # символы с недавней неудачей пропускаем — пробуем следующего
                        passed = [r for r in passed
                                  if failed.get(r["symbol"], 0) < time.time()]
                        if mem is not None:
                            passed = [r for r in passed
                                      if not memory.avoid_symbol(mem, r["symbol"])]
                        if passed:
                            cand = passed[0]
                    # периодический полный скан — ловим удержательные окна
                    # (средняя 30д высокая, но текущая ставка пока не вспыхнула)
                    if cand is None and time.time() - last_full >= cli.full_every:
                        last_full = time.time()
                        rows = scan(cli)
                        if rows:
                            held = {p.get("symbol") for p in positions_of(state)}
                            rows = [r for r in rows
                                    if r["symbol"] not in held
                                    and failed.get(r["symbol"], 0) < time.time()]
                        if mem is not None:
                            rows = [r for r in rows
                                    if not memory.avoid_symbol(mem, r["symbol"])]
                            rows = memory.prefer_order(mem, rows)
                        if rows:
                            cand = rows[0]
                    if cand:
                        if not auto:
                            if time.time() - last_hb >= 60:
                                last_hb = time.time()
                                log(f"окно: {cand['symbol']} "
                                    f"APY30д {conservative_apy(cand):+.1f}% "
                                    f"(авто-вход выключен)")
                        elif time.time() < fail_until:
                            pass  # кулдаун после ошибки входа
                        elif time.time() < uncertain_until:
                            # после потери ответа от биржи автовход заблокирован:
                            # на бирже может висеть неизвестный ордер, новый
                            # вход его добьёт и оставит голую ногу
                            if time.time() - last_hb >= 60:
                                last_hb = time.time()
                                log("автовход заблокирован: прошлый вход "
                                    "закончился потерей ответа от биржи — "
                                    "ждите проверки позиции")
                        else:
                            try:
                                open_position(cli, cand)
                            except (SystemExit, Exception) as e:  # noqa: BLE001 — RuntimeError от биржи тоже
                                if getattr(cli, "_entry_uncertain", False):
                                    # состояние на бирже неизвестно — длинный
                                    # пауз и никаких откатов «на автомате»
                                    uncertain_until = time.time() + 1800
                                    cli._entry_uncertain = False
                                    log(f"АВТОВХОД ЗАБЛОКИРОН на 30 мин: {e}")
                                    continue
                                fail_until = time.time() + 120
                                failed[cand["symbol"]] = time.time() + 600
                                if mem is not None:
                                    lesson = memory.record_fail(mem, cand["symbol"], str(e)[:100])
                                    memory.save_memory(mem)
                                    if lesson:
                                        log(f"MEMORY урок: {lesson}")
                                        notify(f"🧠 {lesson}")
                                notify(f"❌ вход не удался ({cand['symbol']}): {e} — "
                                       f"повтор через 2 мин, символ пропустим 10 мин",
                                       key=f"enter-fail-{cand['symbol']}",
                                       min_gap=900)
                    elif time.time() - last_hb >= 60:
                        last_hb = time.time()
                        if positions_of(state):
                            log(f"окна нет — держим {len(positions_of(state))}"
                                f"/{max_pos} пар, ждём")
                        else:
                            log("окна нет — ждём")
            except KeyboardInterrupt:
                raise
            except (Exception, SystemExit) as e:  # noqa: BLE001 — цикл не должен падать
                loop_err = e
                # антиспам: ошибка цикла повторяется каждые interval секунд,
                # без троттлинга Telegram забивался (~4 сообщения/мин)
                notify(f"❌ ошибка цикла: {e}", key="watch-error", min_gap=600)
            if isinstance(loop_err, ClockSkew):
                # часы = ни одна подписанная операция невозможна; это не
                # «сбой», который пройдёт сам — нужен явный вызов
                notify("🚨 ЧАСЫ СЕРВЕРА РАСХОДЯТСЯ С БИРЖЕЙ (10002). Все "
                       "подписанные запросы будут отклоняться, торговля "
                       "остановлена. Исправьте: timedatectl set-ntp true",
                       key="clock-skew", min_gap=3600)
            # сон с учётом времени цикла: реакция ≈ step сек
            time.sleep(max(0.5, step - (time.time() - t0)))
    except KeyboardInterrupt:
        log("WATCH stop (Ctrl+C)")
    finally:
        if feed is not None:
            feed.stop()
            log("WS лента остановлена")


def cmd_void(cli):
    """Снять позицию из state БЕЗ ордеров — если биржа и бот разошлись.
    Случай пойман вживую: шорт был закрыт руками на бирже, position.json
    продолжал считать позицию открытой. Обычный close при этом купил бы
    перп-ногу заново и оставил бы голый лонг — нужен именно void."""
    if not cli.force:
        sys.exit("void меняет состояние без ордеров — подтвердите флагом --force.")
    state = load_state()
    targets = close_targets(state, cli)   # --symbol / --all, как у close
    for pos in targets:
        void_position(cli, state, pos)


def void_position(cli, state: dict, pos: dict):
    """Снять ОДНУ позицию из state без ордеров (см. cmd_void)."""
    if pos["mode"] in ("testnet", "mainnet"):
        # страховка: на бирже ещё есть перп такой же стороны/объёма — нужен close
        try:
            client = make_client(pos["mode"])
            r = client.request("GET", "/v5/position/list",
                               {"category": "linear", "symbol": pos["symbol"]})
            for p in (r.get("list") or []):
                if float(p.get("size") or 0) > 0:
                    sys.exit(f"На бирже открыта перп-позиция {pos['symbol']} "
                             f"size={p['size']} — используйте close, а не void.")
        except SystemExit:
            raise
        except Exception:  # noqa: BLE001 — биржа недоступна: снимаем локально, ордеров void не делает
            print("Проверка биржи недоступна — снимаю только локальное состояние.")
    state["history"].append({
        "action": "void", "ts": int(time.time() * 1000),
        "symbol": pos["symbol"], "mode": pos["mode"],
        "note": "позиция на бирже отсутствует, снята из state без ордеров",
    })
    drop_position(state, pos)
    save_state(state)
    log(f"VOID {pos['symbol']} {pos['mode']}: фантом снят из state без ордеров")
    notify(f"⚠ Позиция {pos['symbol']} снята из state без ордеров (void)")
    if memory is not None:
        mem = memory.load_memory()
        lesson = memory.record_void(mem, pos["symbol"],
                                    "void: позиция отсутствовала на бирже")
        memory.save_memory(mem)
        if lesson:
            log(f"MEMORY урок: {lesson}")
            notify(f"🧠 {lesson}")
    print(f"Позиция {pos['symbol']} снята из state (ордеров не отправлялось).")


def cmd_memory(cli):
    """Что бот выучил из своих сделок: статистика по символам, стоп-лист, уроки."""
    if memory is None:
        sys.exit("Модуль памяти bybit_memory.py не найден.")
    mem = memory.load_memory()
    if cli.relearn:
        cnt = memory.relearn(mem)
        memory.save_memory(mem)
        print(f"Память перестроена с нуля из bot.log: {cnt}")
    else:
        cnt = memory.ingest_log(mem)
        if any(cnt.values()):
            memory.save_memory(mem)
            print(f"Дочитан bot.log: {cnt}")
    # --autotune: внеплановый пересмотр порогов по факту (после ingest/relearn,
    # чтобы учитывались свежедочитанные сделки)
    if cli.autotune:
        tune = memory.autotune(mem, force=True, preset=cli.preset,
                               mode=learning_modes(cli.mode))
        memory.save_memory(mem)
        print(f"\nАВТОТЮНИНГ (внепланово): {tune}")
    # --apply-tune: принудительно применить сохранённый автотюнинг к фильтру
    # (после ручной правки memory.json или --relearn)
    if cli.apply_tune:
        print(f"\nПРИМЕНЕНИЕ АВТОТЮНИНГА: {memory.apply_tune(mem, preset=cli.preset)}")
        memory.save_memory(mem)
    syms = memory.all_symbols(mem)
    if not syms:
        print("Память пуста — сделок ещё не было.")
        return
    print(f"{'СИМВОЛ':<14} {'ОЦЕН/ВСЕГО':>11} {'WIN':>5} {'СР.PnL%':>8} {'ИТОГО,$':>9} "
          f"{'FUNDING,$':>10} {'СР.ЧАС':>7}  СТАТУС")
    print("-" * 84)
    for sym in syms:
        st = memory.symbol_stats(mem, sym)
        n_all = len(mem.get("trades", {}).get(sym, []))
        stop = memory.avoid_symbol(mem, sym)
        if st["n"] == 0:
            # сделки есть, но короче MIN_HOLD_H — тестовый шум, в скоринг не идут
            status = (f"шум ({n_all} сдел. короче {memory.MIN_HOLD_H}ч)"
                      if n_all else "только неудачные входы/void")
            if stop:
                status += " + СТОП-ЛИСТ: " + stop.split(": ", 1)[1]
            print(f"{sym:<14} {0:>5}/{n_all:<5} {'—':>5} {'—':>8} {'—':>9} "
                  f"{'—':>10} {'—':>7}  {status}")
            continue
        if stop:
            status = "СТОП-ЛИСТ: " + stop.split(": ", 1)[1]
        else:
            status = ("приоритет" if st["avg_pct"] > 0 and st["win_share"] >= 0.6
                      else ("избегать" if st["avg_pct"] < 0 else "наблюдение"))
        print(f"{sym:<14} {st['n']:>5}/{n_all:<5} {st['win_share']:>4.0%} "
              f"{st['avg_pct']:>+8.2f} {st['total']:>+9.2f} "
              f"{st['funding']:>+10.4f} {st['avg_hold_h']:>7.1f}  {status}")
    lesson_list = memory.lessons(mem)
    if lesson_list:
        print("\nВЫВОДЫ БОТА:")
        for t in lesson_list:
            print(f"  • {t}")
    cal = mem.get("calibration") or {}
    if cal:
        print(f"\nКАЧЕСТВО ПРОГНОЗА APY (прогноз при входе vs факт по funding):")
        act = {"up": "пороги ПОВЫШЕНЫ (прогноз завышен)",
               "down": "пороги ПОНИЖЕНЫ (прогноз занижен)",
               "hold": "пороги точны, сдвиг в пределах шума"}.get(cal.get("action"), "—")
        thr = cal.get("thresholds") or {}
        print(f"  сделок с прогнозом: {cal.get('n', 0)}, средняя ошибка: "
              f"{cal.get('bias', 0):+.1f} п.п. → {act}")
        if thr:
            print(f"  пороги сейчас: min_apy30={thr.get('min_apy30')}%, "
                  f"fast_apy={thr.get('fast_apy')}% (исходные: "
                  f"min_apy30=10%, fast_apy=3%)")
    at = mem.get("autotune") or {}
    if at:
        sh = at.get("shift") or {}
        ts = at.get("ts")
        when = datetime.fromtimestamp(ts / 1000).strftime("%Y-%m-%d %H:%M") \
            if ts else "—"
        print(f"\nАВТОТЮНИНГ (раз в {memory.AUTO_EVERY_DAYS} дней, по фактической "
              f"прибыли сделок):")
        print(f"  последний: {when}, сделок учтено: {at.get('scored', 0)}/"
              f"{at.get('trades', 0)}")
        print(f"  profit-сдвиг: min_apy30 {sh.get('min_apy30', 0):+.1f} п.п., "
              f"min_basis {sh.get('min_basis', 0):+.2f}, "
              f"fast_apy {sh.get('fast_apy', 0):+.1f} — применён к формулe порогов")
    else:
        print(f"\nАВТОТЮНИНГ: ещё не проводился (первый — через "
              f"{memory.AUTO_EVERY_DAYS} дней, или `memory --autotune` сейчас)")
    tl = mem.get("threshold_log") or []
    if tl:
        print(f"\nЖУРНАЛ ПОРОГОВ (последние {min(len(tl), 8)} из {len(tl)}):")
        for e in tl[-8:]:
            b, a = e.get("before") or {}, e.get("after") or {}
            chg = ", ".join(f"{k} {b.get(k)}→{a.get(k)}" for k in b
                            if a.get(k) is not None and b.get(k) != a.get(k))
            when = datetime.fromtimestamp(e["ts"] / 1000).strftime("%m-%d %H:%M")
            print(f"  [{when}] {e.get('kind')}: {e.get('trigger', '')[:70]}")
            if chg:
                print(f"      {chg}")
    else:
        print("\nЖУРНАЛ ПОРОГОВ: пуст — пороги ещё не менялись")
    em = mem.get("entry_mode") or {}
    mk, tk = em.get("maker") or {}, em.get("taker") or {}
    if mk.get("n") or tk.get("n"):
        print(f"\nРЕЖИМ ВХОДА (maker vs taker по факту сделок):")
        for style, st in (("maker", mk), ("taker", tk)):
            slip = (f", slip {st['avg_slip_bps']:+.1f} bps"
                    if st.get("avg_slip_bps") is not None else "")
            print(f"  {style:>5}: {st.get('n', 0)} сдел., ср.PnL "
                  f"{st.get('avg_pct', 0):+.2f}%, win {st.get('win_share', 0):.0%}{slip}")
        pref = em.get("prefer")
        pref_s = {"maker": "вхожу maker-лимитками",
                  "taker": "вхожу сразу маркетом (maker-попытки пропущены)"}
        print(f"  вывод: {pref_s.get(pref, 'разницы нет — maker-попытка + фолбэк')} "
              f"(сильнее только явный флаг --taker-only)")
    else:
        print("\nРЕЖИМ ВХОДА: данных о maker/taker входах пока нет")
    print(f"\nПамять: {memory.MEM_FILE}")


def cmd_stats(cli):
    """Сводка статистики: история сделок (position.json) + события (bot.log).
    Подозрительные (физически невозможные) закрытия считаются отдельно и в
    итоги не входят — иначе панель/сводка показывала бы несуществующий PnL."""
    state = load_state()
    hist = state.get("history", [])
    closes_all = [h for h in hist if h.get("action") == "close"]
    closes, susp = sanity.partition(closes_all)
    enters = [h for h in hist if h.get("action") == "enter"]
    voids = [h for h in hist if h.get("action") == "void"]

    print("СТАТИСТИКА СДЕЛОК")
    print(f"  Входов записано: {len(enters)} | закрыто: {len(closes)} | "
          f"снято фантомных: {len(voids)}")
    if susp:
        sm = sanity.summarize(closes_all)
        print(f"  Подозрительных (исключены из итогов): {sm['n']} шт. на "
              f"{sm['total']:+.2f} $ — нулевое удержание без выплат funding; "
              f"разбор: sanity")
    if closes:
        totals = [c.get("total") or 0 for c in closes]
        pnls = [c.get("pnl_excl_funding") or 0 for c in closes]
        funds = [c.get("funding") or 0 for c in closes]
        rets = [c.get("return_pct") or 0 for c in closes]
        wins = [t for t in totals if t > 0]
        holds = [c.get("held_hours") or 0 for c in closes]
        print(f"  Прибыльных: {len(wins)} | убыточных: {len(totals) - len(wins)} "
              f"| винрейт {100 * len(wins) / len(totals):.0f}%")
        print(f"  Итого PnL: {sum(totals):+.2f} $ "
              f"(по цене {sum(pnls):+.2f} + funding {sum(funds):+.2f})")
        print(f"  Средняя сделка: {sum(rets) / len(rets):+.3f}% капитала | "
              f"среднее удержание {sum(holds) / len(holds):.1f} ч")
        print(f"  Лучшая: {max(totals):+.2f} $ | худшая: {min(totals):+.2f} $")
        print("  --- последние сделки ---")
        for c in closes_all[-10:]:
            when = datetime.fromtimestamp(c["ts"] / 1000).strftime("%m-%d %H:%M")
            mark = " ⚠ подозрительная" if sanity.is_suspicious(c) else ""
            print(f"    {when} {c['symbol']:>12} {c.get('mode', '?'):>7} "
                  f"удерж {c.get('held_hours', 0):5.1f}ч "
                  f"итого {c.get('total', 0):+.2f} $ ({c.get('return_pct', 0):+.2f}%)"
                  f"{mark}")

    # события бота из bot.log — сколько окон/ошибок было за сутки
    try:
        with open(LOG_FILE, encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        lines = []
    if lines:
        def n(prefix: str) -> int:
            return sum(1 for ln in lines if prefix in ln)
        print("  --- события бота (bot.log) ---")
        print(f"    ENTER {n('ENTER')} | EXIT {n('EXIT')} | CLOSE {n('CLOSE')} | "
              f"неудачных входов {n('вход не удался')} | ошибок цикла {n('ошибка цикла')}")
        print(f"    fast-сканов {n('fast:')} | окно не найдено {n('окна нет')} | "
              f"heartbeat HOLD {n('HOLD ')}")

    plist = positions_of(state)
    if plist:
        total_pnl = 0.0
        for pos in plist:
            _, _, pnl = current_pnl(pos)
            total_pnl += pnl
            held_h = (time.time() * 1000 - pos["opened_ms"]) / 3_600_000
            print(f"  ОТКРЫТА: {pos['symbol']} ({pos['mode']}) {held_h:.1f} ч, "
                  f"PnL {pnl:+.2f} $, funding {pos['funding_accrued']:+.4f} $")
        if len(plist) > 1:
            print(f"  Портфель: {len(plist)} пар, суммарный PnL "
                  f"{total_pnl:+.2f} $")
    else:
        print("  Открытой позиции нет (бот ждёт окно).")


def parse_hhmm(text: str | None) -> tuple[int, int] | None:
    """'ЧЧ:ММ' → (ч, мин); 'off'/пусто/мусор → None (рассылка выключена)."""
    t = (text or "").strip()
    if not t or t.lower() in ("off", "none", "no", "0"):
        return None
    hh, _, mm = t.partition(":")     # "7" и "7:30" работают одинаково
    try:
        h, m = int(hh), int(mm or 0)
    except ValueError:
        return None
    if not (0 <= h <= 23 and 0 <= m <= 59):
        return None
    return h, m


def parse_tz(name: str | None):
    """Часовой пояс отчётов.

    ''/local → локальное время сервера; '+03:00'/'−5' → фиксированный сдвиг
    (не требует базы tz: работает и на Windows); иначе — имя IANA через
    zoneinfo (на Windows нужен пакет tzdata). Неизвестное имя не роняет
    бота: пишем в лог и остаёмся на локальном времени.
    """
    text = (name or "").strip()
    if not text or text.lower() in ("local", "system"):
        return None
    if text[0] in "+-":
        sign = -1 if text[0] == "-" else 1
        hh, _, mm = text[1:].partition(":")
        try:
            h, m = abs(int(hh)), int(mm or 0)
        except ValueError:
            h, m = -1, -1
        if 0 <= h <= 14 and 0 <= m <= 59:
            return timezone(sign * timedelta(hours=h, minutes=m))
        log(f"часовой пояс '{text}' не распознан — беру локальное время")
        return None
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(text)
    except Exception as e:  # noqa: BLE001 — нет tzdata/неизвестное имя
        log(f"часовой пояс '{text}' не распознан ({e}) — беру локальное время")
        return None


def period_start_ms(kind: str, now: float, tz=None) -> float:
    """Начало периода в мс для 'day' (полночь) или 'week' (понедельник 00:00).
    Считается в часовом поясе отчёта, а не в поясе сервера: на VPS это UTC,
    и «за день» без этого сдвигалось бы на 3 часа от реальных суток."""
    dt = datetime.fromtimestamp(now, tz) if tz else datetime.fromtimestamp(now)
    start = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    if kind == "week":
        start -= timedelta(days=start.weekday())
    return start.timestamp() * 1000


def aggregate_closes(closes: list[dict], since_ms: float | None = None,
                     until_ms: float | None = None) -> dict:
    """Итоги по закрытым сделкам за период (чистая функция — легко тестировать)."""
    def _num(c: dict, key: str) -> float:
        v = c.get(key)
        return float(v) if isinstance(v, (int, float)) else 0.0

    sel: list[dict] = []
    for c in closes:
        ts = c.get("ts")
        if not isinstance(ts, (int, float)):
            continue
        if since_ms is not None and ts < since_ms:
            continue
        if until_ms is not None and ts >= until_ms:
            continue
        sel.append(c)
    price = sum(_num(c, "pnl_excl_funding") for c in sel)
    fund = sum(_num(c, "funding") for c in sel)
    # Итог берём из записанного "total" (как в pnl_report и на панели);
    # только если поля нет — собираем из цены и funding.
    totals = [_num(c, "total") if isinstance(c.get("total"), (int, float))
              else _num(c, "pnl_excl_funding") + _num(c, "funding")
              for c in sel]
    wins = sum(1 for t in totals if t > 0)
    return {"n": len(sel), "wins": wins, "price": price, "fund": fund,
            "total": sum(totals), "trades": sel,
            "winrate": (100.0 * wins / len(sel)) if sel else 0.0}


def account_balance(cli) -> float | None:
    """USDT-эквити UNIFIED-счёта среды запуска (None — paper/сеть/ключи).

    Нужен для отчёта «сколько всего на счёте». Вызывается раз в период
    (--report-every), поэтому отдельный клиент на запрос не проблема; сбой
    сети не должен ломать отчёт — возвращаем None и пишем в лог.
    """
    mode = str(getattr(cli, "mode", "") or "")
    if mode not in ("testnet", "mainnet"):
        return None
    try:
        return float(make_client(mode).wallet_balance())
    except Exception as e:  # noqa: BLE001 — баланс не важнее отчёта
        log(f"баланс счёта недоступен: {e}")
        return None


def pnl_report(state: dict, feed=None, now_ms: int | None = None,
               balance: float | None = None, tz=None) -> str:
    """Таблица дохода/PnL для периодической рассылки (--report-every).

    Одно сообщение: баланс счёта и результат за день, открытая позиция с
    живым PnL + таблица закрытых сделок с итогом. Подозрительные записи
    (trade_sanity) в таблицу и в итоги не идут — как в stats и на панели,
    иначе отчёт показывал бы несуществующий доход. Текст без разметки
    (Telegram), колонки выровнены пробелами.

    balance — USDT-эквити счёта (None: paper/сеть недоступна). «За день» =
    реализованный PnL сегодняшних закрытий + живой PnL открытой позиции.
    """
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    when = datetime.fromtimestamp(now_ms / 1000).strftime("%d.%m %H:%M")
    lines = [f"📊 ДОХОД/PnL · {when}"]

    # портфель: каждая открытая пара — отдельной строкой (PnL дня — сумма)
    plist = positions_of(state)
    open_pnl: float | None = None
    pos_lines: list[str] = []
    for pos in plist:
        try:
            _, _, p = current_pnl(pos, feed=feed)
            open_pnl = (open_pnl or 0.0) + p
            pnl_s = f"{p:+.2f} $"
        except Exception:  # noqa: BLE001 — сеть не должна ломать отчёт
            pnl_s = "н/д (биржа недоступна)"
        held_h = (now_ms - float(pos.get("opened_ms") or now_ms)) / 3_600_000
        pos_lines.append(
            f"Открытая: {pos.get('symbol')} ({pos.get('mode')}), удержано "
            f"{held_h:.1f} ч, PnL {pnl_s}, funding "
            f"{float(pos.get('funding_accrued') or 0):+.4f} $, выплат "
            f"{pos.get('payouts', 0)}")
    if not pos_lines:
        pos_lines = ["Открытой позиции нет (бот ждёт окно)."]

    closes_all = [h for h in (state.get("history") or [])
                  if h.get("action") == "close"]
    closes, susp = sanity.partition(closes_all)

    def num(c: dict, key: str) -> float:
        v = c.get(key)
        return float(v) if isinstance(v, (int, float)) else 0.0

    # Граница дня — в часовом поясе отчёта (см. period_start_ms): на VPS
    # локальное время обычно UTC, и «сегодня» без этого было бы сдвинуто.
    today = aggregate_closes(
        closes, since_ms=period_start_ms("day", now_ms / 1000, tz))["total"]
    day_total = today + (open_pnl or 0.0)

    # Баланс + результат за день: главное, что нужно видеть каждый час.
    if balance is None:
        bal_line = "💼 Счёт: н/д (биржа недоступна или режим paper)"
    else:
        bal_line = f"💼 Счёт: ${balance:,.2f} USDT · за день {day_total:+.2f} $"
        base = balance - day_total      # оценка баланса на начало дня
        if base > 0:
            bal_line += f" ({100 * day_total / base:+.2f}%)"
        if plist and open_pnl is not None:
            # портфель показываем суммой и числом пар; одна пара — как раньше
            bal_line += (f" · открытых {len(plist)} ({open_pnl:+.2f} $)"
                         if len(plist) > 1 else f" · открытая {open_pnl:+.2f} $")
    lines.append(bal_line)
    lines.extend(pos_lines)

    if not closes:
        lines.append("Закрытых сделок ещё нет.")
        return "\n".join(lines)

    price = sum(num(c, "pnl_excl_funding") for c in closes)
    fund = sum(num(c, "funding") for c in closes)
    total = sum(num(c, "total") for c in closes)
    wins = sum(1 for c in closes if num(c, "total") > 0)
    note = (f" · ⚠ {len(susp)} подозрительных в итоги не входит"
            if susp else "")
    lines.append(
        f"Закрыто сделок: {len(closes)}, винрейт {100 * wins / len(closes):.0f}%"
        f", итого {total:+.2f} $ (по цене {price:+.2f} + funding {fund:+.2f})"
        f", сегодня {today:+.2f} ${note}")
    lines.append(f"{'дата':<11} {'символ':<11} {'режим':>7} {'ч':>5} "
                 f"{'цена$':>8} {'фонд$':>9} {'итого$':>8} {'%':>7}")
    for c in closes[-10:]:
        ts = datetime.fromtimestamp(c["ts"] / 1000).strftime("%m-%d %H:%M")
        lines.append(
            f"{ts:<11} {c.get('symbol', '?'):<11} {c.get('mode', '?'):>7} "
            f"{num(c, 'held_hours'):>5.1f} "
            f"{num(c, 'pnl_excl_funding'):>+8.2f} "
            f"{num(c, 'funding'):>+9.3f} "
            f"{num(c, 'total'):>+8.2f} {num(c, 'return_pct'):>+7.2f}")
    # итоговая строка — по ВСЕМ сделкам, даже если показаны последние 10
    lines.append(f"{'ИТОГО':<11} {len(closes):<11} {'':>7} {'':>5} "
                 f"{price:>+8.2f} {fund:>+9.3f} {total:>+8.2f}")
    return "\n".join(lines)


def maybe_pnl_report(state: dict, cli, feed, last_report: float,
                     now: float | None = None) -> float:
    """--report-every: таблица дохода/PnL в лог и Telegram каждые N минут.

    Возвращает новое «время последнего отчёта» для вызывающего цикла.
    0 (или отрицательный) --report-every — рассылка выключена. Сбой
    формата/сети не должен ронять торговый цикл — ошибка только в лог.
    """
    every = float(getattr(cli, "report_every", 0) or 0) * 60.0
    now = time.time() if now is None else now
    if every <= 0 or now - last_report < every:
        return last_report
    try:
        notify(pnl_report(state, feed=feed, balance=account_balance(cli),
                          tz=getattr(cli, "report_tz_obj", None)))
    except Exception as e:  # noqa: BLE001 — отчёт не важнее торгового цикла
        log(f"PnL-отчёт не отправлен: {e}")
    return now


def daily_report(state: dict, feed=None, now_ms: int | None = None,
                 balance: float | None = None, tz=None,
                 tz_name: str = "") -> str:
    """Сводка на границе суток: баланс счёта, итог дня и итог недели.

    «День» — сутки в часовом поясе отчёта, включая живой PnL открытой
    позиции (как в часовом отчёте); «неделя» — с понедельника той же
    недели, только реализованные закрытия. Подозрительные записи
    (trade_sanity) не входят — иначе сводка показывала бы несуществующий доход.
    """
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    now = now_ms / 1000
    stamp = datetime.fromtimestamp(now, tz) if tz else datetime.fromtimestamp(now)
    head = f"🗓 ИТОГ ДНЯ · {stamp.strftime('%d.%m %H:%M')}"
    if tz_name:
        head += f" ({tz_name})"
    lines = [head]

    plist = positions_of(state)
    open_pnl: float | None = None
    for pos in plist:
        try:
            _, _, p = current_pnl(pos, feed=feed)
            open_pnl = (open_pnl or 0.0) + p
        except Exception:  # noqa: BLE001 — сеть не должна ломать сводку
            open_pnl = None

    closes_all = [h for h in (state.get("history") or [])
                  if h.get("action") == "close"]
    closes, susp = sanity.partition(closes_all)
    day_start = period_start_ms("day", now, tz)
    week_start = period_start_ms("week", now, tz)
    day = aggregate_closes(closes, since_ms=day_start)
    week = aggregate_closes(closes, since_ms=week_start)
    day_total = day["total"] + (open_pnl or 0.0)

    if balance is None:
        lines.append("💼 Счёт: н/д (биржа недоступна или режим paper)")
    else:
        line = f"💼 Счёт: ${balance:,.2f} USDT · за день {day_total:+.2f} $"
        base = balance - day_total
        if base > 0:
            line += f" ({100 * day_total / base:+.2f}%)"
        line += f" · за неделю {week['total']:+.2f} $"
        lines.append(line)

    def row(title: str, when: str, agg: dict) -> str:
        if not agg["n"]:
            return f"{title} ({when}): сделок нет"
        return (f"{title} ({when}): сделок {agg['n']}, винрейт "
                f"{agg['winrate']:.0f}%, итого {agg['total']:+.2f} $"
                f" (по цене {agg['price']:+.2f} + funding {agg['fund']:+.2f})")

    week_stamp = (datetime.fromtimestamp(week_start / 1000, tz) if tz else
                  datetime.fromtimestamp(week_start / 1000))
    lines.append(row("День", stamp.strftime("%d.%m"), day))
    lines.append(row("Неделя", f"с {week_stamp.strftime('%d.%m')}", week))

    for pos in plist:
        held_h = (now_ms - float(pos.get("opened_ms") or now_ms)) / 3_600_000
        try:
            _, _, p = current_pnl(pos, feed=feed)
            pnl_s = f"{p:+.2f} $"
        except Exception:  # noqa: BLE001 — сеть не должна ломать сводку
            pnl_s = "н/д (биржа недоступна)"
        lines.append(f"Открытая: {pos.get('symbol')} ({pos.get('mode')}), "
                     f"удержано {held_h:.1f} ч, PnL {pnl_s}, funding "
                     f"{float(pos.get('funding_accrued') or 0):+.4f} $")
    if not plist:
        lines.append("Открытой позиции нет (бот ждёт окно).")

    # Лучшая и худшая сделка дня — быстрый ответ «за счёт чего день».
    if day["trades"]:
        best = max(day["trades"], key=lambda c: float(c.get("total") or 0))
        worst = min(day["trades"], key=lambda c: float(c.get("total") or 0))
        lines.append(f"Лучшая: {best.get('symbol')} "
                     f"{float(best.get('total') or 0):+.2f} $ · "
                     f"Худшая: {worst.get('symbol')} "
                     f"{float(worst.get('total') or 0):+.2f} $")
    if susp:
        lines.append(f"⚠ {len(susp)} подозрительных в итоги не входит")
    return "\n".join(lines)


def maybe_daily_report(state: dict, cli, feed, now: float | None = None) -> bool:
    """--daily-report-at: сводка раз в сутки в заданное время (пояс --report-tz).

    День отправки запоминается в state (position.json): рестарт бота не
    дублирует сводку, а пропущенная в 00:00 (сервер лежал) уйдёт при
    подъёме — день ещё не отмечен. Пустое/off значение выключает рассылку.
    Сбой не роняет торговый цикл. Возвращает True, если отчёт отправлен.
    """
    at = getattr(cli, "daily_at", None)
    if not at:
        return False
    now = time.time() if now is None else now
    tz = getattr(cli, "report_tz_obj", None)
    stamp = datetime.fromtimestamp(now, tz) if tz else datetime.fromtimestamp(now)
    today = stamp.strftime("%Y-%m-%d")
    if state.get("last_daily_report") == today or (stamp.hour, stamp.minute) < at:
        return False
    # Отмечаем день ДО отправки: если формат/сеть падают, вторая попытка
    # не будет крутиться каждую секунду в цикле watch.
    state["last_daily_report"] = today
    try:
        notify(daily_report(state, feed=feed, now_ms=int(now * 1000),
                            balance=account_balance(cli), tz=tz,
                            tz_name=str(getattr(cli, "report_tz", "") or "")))
    except Exception as e:  # noqa: BLE001 — отчёт не важнее торгового цикла
        log(f"ежедневная сводка не отправлена: {e}")
    try:
        save_state(state)
    except Exception as e:  # noqa: BLE001 — состояние переживёт следующий сейв
        log(f"состояние сводки не сохранено: {e}")
    return True


def _backup_file(path: str) -> str | None:
    """Копия файла рядом (<имя>.pre-sanity.bak) перед правкой данных."""
    try:
        shutil.copy2(path, path + ".pre-sanity.bak")
        return path + ".pre-sanity.bak"
    except OSError:
        return None


def cmd_sanity(cli):
    """Разбор накопленных записей: физически невозможные сделки.

    Что считается невозможным — trade_sanity: нулевое удержание (funding за
    это время выплатиться не успевал), ноль полученного funding и при этом
    заметный результат. Такой результат — не сделка, а бухгалтерия битых цен
    ног (записан чужой/устаревший снимок цены входа): прибыль ровно равна
    аномалии цены, а не экономике пары.

    Панель, `stats` и память исключают такие записи НА ЧТЕНИИ, поэтому
    защита работает сразу после обновления кода. Команда нужна для другого:
      • показать, что именно отброшено и на какую сумму (молчаливое исключение
        из PnL выглядит как баг расчёта);
      • --fix: зафиксировать причину в самих файлах (position.json/memory.json),
        чтобы её видел человек, открывший данные, и чтобы пометку не затирало
        следующее обучение. Перед записью рядом остаётся копия
        <файл>.pre-sanity.bak.
    """
    # Пути — от ПАПКИ СКРИПТА, а не от текущей папки: команда правит данные
    # бота, и запуск из другого каталога создал бы там пустые position.json и
    # memory.json, а реальные данные остались бы нетронутыми.
    base_dir = os.path.dirname(os.path.abspath(__file__))
    state_path = os.path.join(base_dir, STATE_FILE)
    mem_path = (os.path.join(base_dir, memory.MEM_FILE)
                if memory is not None else None)
    state = load_state(state_path)
    hist = state.get("history", [])
    closes = [h for h in hist if h.get("action") == "close"]
    mem = memory.load_memory(mem_path) if memory is not None else {}
    mtrades = memory.all_trades(mem) if memory is not None else []

    print("САНИТАРИЯ СДЕЛОК: разбор физически невозможных результатов")
    print(f"  файлы: {state_path}" + (f", {mem_path}" if mem_path else ""))
    print(f"  правило: удержание < {sanity.NO_PAYOUT_H:g} ч (выплат funding "
          f"быть не могло), funding ≤ {sanity.NO_PAYOUT_FUNDING_EPS:g}$, "
          f"а результат ≥ {sanity.GAIN_LIMIT_PCT:g}% или "
          f"≤ {-sanity.LOSS_LIMIT_PCT:g}% базы")

    found = 0
    for title, rows in (("position.json (закрытия)", closes),
                        ("memory.json (сделки обучения)", mtrades)):
        sm = sanity.summarize(rows)
        found += sm["n"]
        print(f"\n  --- {title}: записей {len(rows)}, подозрительных "
              f"{sm['n']} на {sm['total']:+.2f} $ ---")
        for s in sm["by_symbol"]:
            print(f"    {s['symbol']:>12}: {s['n']} шт., {s['total']:+.2f} $"
                  f" (сумма {s['pct']:+.2f}% базы)")
        shown = [r for r in rows if sanity.is_suspicious(r)]
        for r in shown[:10]:
            print(f"      {sanity.describe(r)}")
        if len(shown) > 10:
            print(f"      … ещё {len(shown) - 10} записей")

    if not found:
        print("\n  Подозрительных сделок нет — все записи правдоподобны.")
        return

    print("\n  Прибыль такой записи равна аномалии цены ноги (обычно потерянный "
          "или чужой снимок цены входа), а не экономике пары. В PnL панели, "
          "в статистике и в обучении памяти она не идёт — фильтр срабатывает "
          "на чтении, пометка для этого не нужна.")
    if not cli.fix:
        print("  Зафиксировать причину в файлах: "
              "python funding_executor.py sanity --fix")
        return

    marked = 0
    if closes:
        n = sanity.mark_all(closes)
        if n:
            _backup_file(state_path)
            save_state(state, state_path)
            marked += n
            print(f"  position.json: помечено {n} записей")
    if mtrades and mem_path:
        n = sanity.mark_all(mtrades)
        if n:
            _backup_file(mem_path)
            memory.save_memory(mem, mem_path)
            marked += n
            print(f"  memory.json: помечено {n} записей")
    print(f"  Итого новых пометок: {marked}. Фильтр работал и до них — пометка "
          f"лишь фиксирует причину в данных для разбора.")


# ============================================================ SCALP (скальпер)
# Отдельный режим направленной торговли перпом мейджоров на минутных свечах:
# ОДНА нога (не дельта-нейтральная пара), удержание — минуты. Состояние живёт
# в собственном файле scalp_state.json и НЕ смешивается с funding-позицией,
# памятью самообучения и dashboard funding-арбитража (полная изоляция: рабочий
# арбитраж не затрагивается).
#
# Вход: пересечение EMA(fast) и EMA(slow) на последней ЗАКРЫТОЙ свече.
# Выход: take-profit / stop-loss по NET-базисным пунктам (после комиссий
# тейкера обеих сторон) или по времени (max hold). Экономика перпа:
# taker 0.055% туда-обратно = 11 б.п. — цель обязана их перекрывать, иначе
# скальпинг структурно убыточен.

SCALP_STATE_FILE = "scalp_state.json"
SCALP_TP_BPS = 20.0
SCALP_SL_BPS = 15.0
SCALP_MAX_HOLD_MIN = 5.0


def scalp_fee_bps(cli) -> float:
    """Комиссии тейкера туда-обратно в б.п.: 2 × --perp-taker-fee (в %)."""
    v = getattr(cli, "scalp_fee_bps", None)
    if v is not None:
        return float(v)
    return 2.0 * float(getattr(cli, "perp_taker_fee", 0.055)) * 100.0


def ema(values: list[float], period: int) -> list[float]:
    """EMA по потоку значений: k = 2/(period+1), старт — первое значение."""
    if not values or period <= 0:
        return []
    k = 2.0 / (period + 1)
    out = []
    e = float(values[0])
    for v in values:
        e = float(v) * k + e * (1 - k)
        out.append(e)
    return out


def scalp_signal(closes: list[float], fast: int = 9,
                 slow: int = 21) -> tuple[str | None, dict]:
    """Сигнал по последней ЗАКРЫТОЙ свече: EMA(fast) пересекла EMA(slow)
    вверх → long, вниз → short, иначе None. closes — только закрытые свечи
    (без формирующейся). Возвращает (сторона, инфо)."""
    if len(closes) < slow + 2:
        return None, {"reason": "мало свечей"}
    ef = ema(closes, fast)
    es = ema(closes, slow)
    f_now, s_now = ef[-1], es[-1]
    f_prev, s_prev = ef[-2], es[-2]
    info = {"fast": f_now, "slow": s_now, "f_prev": f_prev, "s_prev": s_prev}
    if f_prev <= s_prev and f_now > s_now:
        return "long", info
    if f_prev >= s_prev and f_now < s_now:
        return "short", info
    return None, info


def scalp_gross_bps(side: str, entry: float, price: float) -> float:
    """Валовая доходность сделки в базисных пунктах (без комиссий)."""
    if entry <= 0 or price <= 0:
        return 0.0
    d = 1.0 if side == "long" else -1.0
    return d * (price / entry - 1.0) * 1e4


def scalp_net_bps(side: str, entry: float, price: float, fee_bps: float) -> float:
    """Доходность после комиссий тейкера обеих сторон, в б.п."""
    return scalp_gross_bps(side, entry, price) - float(fee_bps)


def decide_scalp_exit(pos: dict, price: float, now_ms: int, tp_bps: float,
                      sl_bps: float, max_hold_min: float,
                      fee_bps: float) -> tuple[bool, str]:
    """(выходить, причина) для скальп-позиции: TP/SL по NET или время."""
    net = scalp_net_bps(pos["side"], pos["entry"], price, fee_bps)
    if tp_bps > 0 and net >= tp_bps:
        return True, f"take-profit {net:+.1f} б.п. ≥ {tp_bps:g}"
    if sl_bps > 0 and net <= -sl_bps:
        return True, f"stop-loss {net:+.1f} б.п. ≤ −{sl_bps:g}"
    held_min = (now_ms - int(pos["opened_ms"])) / 60000.0
    if max_hold_min > 0 and held_min >= max_hold_min:
        return True, f"время вышло {held_min:.1f} мин ≥ {max_hold_min:g}"
    return False, f"net {net:+.1f} б.п., удержание {held_min:.1f} мин"


def scalp_pnl(pos: dict, exit_price: float,
              fee_bps: float) -> tuple[float, float, float]:
    """(net$, gross$, fees$) по цене выхода; комиссии — доля номинала."""
    d = 1.0 if pos["side"] == "long" else -1.0
    qty = float(pos["qty"])
    gross = d * (exit_price - pos["entry"]) * qty
    fees = (pos["entry"] + exit_price) * qty * (float(fee_bps) / 2.0) / 1e4
    return gross - fees, gross, fees


def load_scalp() -> dict:
    if os.path.exists(SCALP_STATE_FILE):
        with open(SCALP_STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {"position": None, "history": []}


def save_scalp(state: dict):
    with open(SCALP_STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def scalp_symbols(cli) -> list[str]:
    raw = getattr(cli, "scalp_symbols", "") or "BTCUSDT,ETHUSDT,SOLUSDT"
    return [s.strip().upper() for s in raw.split(",") if s.strip()]


def fetch_klines(symbol: str, interval: str = "1", limit: int = 200,
                 mode: str = "paper") -> list[dict]:
    """Свечи перпа от старых к новым: [{t,o,h,l,c,v}]. API отдаёт обратный
    порядок — сортируем по времени (индикаторам нужна хронология)."""
    res = market_api_get("/v5/market/kline",
                         {"category": "linear", "symbol": symbol,
                          "interval": str(interval), "limit": str(limit)},
                         mode=mode)
    rows = []
    for r in res.get("list") or []:
        rows.append({"t": int(r[0]), "o": float(r[1]), "h": float(r[2]),
                     "l": float(r[3]), "c": float(r[4]), "v": float(r[5])})
    rows.sort(key=lambda x: x["t"])
    return rows


def _avg_fill_since(client, category: str, symbol: str,
                    t0_ms: int) -> float | None:
    """Средняя цена исполнения ордеров символа после t0 (иначе None)."""
    return avg_fill_since(client, category, symbol, t0_ms)


def open_scalp(cli, symbol: str, side: str, price: float) -> dict:
    """Открыть скальп-позицию (одна перп-нога). paper — симуляция по цене
    последней свечи, ордера не отправляются; testnet/mainnet — рыночный/IOC
    ордер с защитой --max-slip-bps."""
    if cli.mode == "mainnet" and not getattr(cli, "confirm_live", False):
        sys.exit("Режим mainnet — РЕАЛЬНЫЕ деньги. Подтвердите флагом --confirm-live.")
    st = load_scalp()
    if st.get("position"):
        sys.exit(f"Скальп-позиция уже открыта: {st['position']['symbol']}")
    if price <= 0:
        sys.exit(f"{symbol}: нет цены для входа")

    step = qty_step(symbol, "linear")
    qty = round_qty(float(cli.capital) / price, step)
    if qty <= 0:
        sys.exit(f"Нотион ${cli.capital:.2f} меньше шага лота {step} {symbol}")

    order_side = "Buy" if side == "long" else "Sell"
    fill = price
    if cli.mode in ("testnet", "mainnet"):
        client = make_client(cli.mode)
        if cli.mode == "mainnet":
            try:
                client.set_leverage(symbol, cli.leverage)
                log(f"LEVERAGE {symbol} → {cli.leverage:g}x")
            except Exception as e:  # noqa: BLE001 — 110043 «not modified» и т.п.
                log(f"LEVERAGE {symbol}: {e} — продолжаю с текущим")
        try:
            ob = orderbook(symbol, "linear", cli, mode=cli.mode)
        except Exception:  # noqa: BLE001 — нет стакана → IOC/маркет по защите
            ob = None
        t0 = int(time.time() * 1000)
        fill_with_slippage(client, cli, symbol, order_side, float(qty),
                           "linear", ob=ob, allow_market=True)
        avg = _avg_fill_since(client, "linear", symbol, t0)
        if avg:
            fill = avg
    else:
        log(f"PAPER СКАЛЬП вход {symbol} {side}: ордера не отправляются "
            f"(симуляция по цене {price:g})")

    notional = float(qty) * fill
    fee_bps = scalp_fee_bps(cli)
    pos = {
        "mode": cli.mode, "symbol": symbol, "side": side,
        "qty": float(qty), "entry": fill, "notional": notional,
        "margin": notional / cli.leverage if cli.leverage > 0 else notional,
        "leverage": cli.leverage, "capital": cli.capital,
        "fee_bps": fee_bps, "opened_ms": int(time.time() * 1000),
    }
    # Биржевой стоп по --scalp-sl-bps: у скальпа минутный холд, бот может
    # не успеть закрыться (краш/сеть) — на бирже стоп сработает сам.
    if cli.mode in ("testnet", "mainnet") and not getattr(cli, "no_exchange_stop", False):
        _sl_pct = float(getattr(cli, "scalp_sl_bps", 0) or 0) / 1e4
        if _sl_pct > 0:
            _sl = fill * ((1 + _sl_pct) if side == "long" else (1 - _sl_pct))
            _sl = float(round_price(_sl, price_tick(symbol, "linear"), "down"))
            try:
                make_client(cli.mode).set_trading_stop(symbol, stop_loss=_sl)
                pos["stop_price"] = _sl
                pos["stop_armed"] = True
                log(f"СТОП БИРЖИ (скальп) {symbol} @ {_sl:g}")
            except Exception as e:  # noqa: BLE001
                pos["stop_armed"] = False
                log(f"стоп биржи (скальп) не выставлен: {e}")
    st["position"] = pos
    st["history"].append({"action": "enter", "ts": pos["opened_ms"],
                          "symbol": symbol, "side": side, "mode": cli.mode})
    save_scalp(st)
    notify(f"🟢 СКАЛЬП ВХОД {symbol} {side.upper()} {float(qty):g} @ {fill:g}")
    return pos


def close_scalp(cli, pos: dict, price: float, reason: str) -> dict:
    """Закрыть скальп-позицию обратным ордером (reduce_only) и записать PnL."""
    exit_price = price
    if pos["mode"] in ("testnet", "mainnet"):
        client = make_client(pos["mode"])
        # стоп снимаем ДО закрытия: висящий TP/SL может выстрелить в новую
        # позицию по тому же символу
        if pos.get("stop_armed"):
            try:
                client.set_trading_stop(pos["symbol"], stop_loss=0)
            except Exception as e:  # noqa: BLE001
                log(f"не удалось снять стоп биржи (скальп): {e}")
        order_side = "Sell" if pos["side"] == "long" else "Buy"
        try:
            # стакан — у СРЕДЫ ПОЗИЦИИ: close_scalp может зваться с
            # --mode paper, и стакан mainnet дал бы неверную IOC-границу
            ob = orderbook(pos["symbol"], "linear", cli, mode=pos["mode"])
        except Exception:  # noqa: BLE001
            ob = None
        t0 = int(time.time() * 1000)
        fill_with_slippage(client, cli, pos["symbol"], order_side,
                           float(pos["qty"]), "linear", ob=ob,
                           reduce_only=True, allow_market=True)
        avg = _avg_fill_since(client, "linear", pos["symbol"], t0)
        if avg:
            exit_price = avg
    else:
        log(f"PAPER СКАЛЬП выход {pos['symbol']}: ордера не отправляются")

    pnl, gross, fees = scalp_pnl(pos, exit_price, pos["fee_bps"])
    held_min = (time.time() * 1000 - pos["opened_ms"]) / 60000.0
    st = load_scalp()
    st["history"].append({
        "action": "close", "ts": int(time.time() * 1000),
        "symbol": pos["symbol"], "side": pos["side"], "mode": pos["mode"],
        "held_min": round(held_min, 1), "entry": pos["entry"],
        "exit": exit_price, "gross": round(gross, 4), "fees": round(fees, 4),
        "total": round(pnl, 4), "reason": reason,
        "return_pct": round(pnl / pos["capital"] * 100, 3),
    })
    st["position"] = None
    save_scalp(st)
    notify(f"🔴 СКАЛЬП ЗАКРЫТО {pos['symbol']} {pos['side'].upper()} "
           f"({held_min:.1f} мин, {reason}): {pnl:+.2f} $"
           f" ({pnl / pos['capital'] * 100:+.2f}%)")
    return {"pnl": pnl, "gross": gross, "fees": fees, "exit": exit_price}


def cmd_scalp_status(cli):
    """Состояние скальпера: текущая позиция и сигналы по символам."""
    st = load_scalp()
    pos = st.get("position")
    if pos:
        sym = pos["symbol"]
        try:
            k = fetch_klines(sym, cli.scalp_interval, 3, mode=pos["mode"])
            price = k[-1]["c"] if k else pos["entry"]
        except Exception:  # noqa: BLE001 — нет сети → показываем по входу
            price = pos["entry"]
        net = scalp_net_bps(pos["side"], pos["entry"], price, pos["fee_bps"])
        held_min = (time.time() * 1000 - pos["opened_ms"]) / 60000.0
        print(f"Скальп-позиция {sym} {pos['side'].upper()} ({pos['mode']})")
        print(f"  вход {pos['entry']:g} → {price:g}  net {net:+.1f} б.п.")
        print(f"  удержание {held_min:.1f} мин, qty {pos['qty']:g}, "
              f"нотион ${pos['notional']:.2f}")
    else:
        print("Скальп-позиции нет.")
    print(f"\nСигналы ({cli.scalp_interval}m, EMA "
          f"{cli.scalp_fast}/{cli.scalp_slow}):")
    for sym in scalp_symbols(cli):
        try:
            k = fetch_klines(sym, cli.scalp_interval, cli.scalp_slow + 5,
                             mode=cli.mode)
            closes = [c["c"] for c in k[:-1]]
            sig, _ = scalp_signal(closes, cli.scalp_fast, cli.scalp_slow)
            last = k[-1]["c"] if k else 0.0
            mark = {"long": "▲ long", "short": "▼ short", None: "— нет"}.get(sig)
            print(f"  {sym:<10} {mark:<9} цена {last:g}")
        except Exception as e:  # noqa: BLE001 — сеть/символ
            print(f"  {sym:<10} ошибка: {e}")


def cmd_watch_scalp(cli):
    """Цикл скальпера: сигнал по EMA на закрытых свечах → вход, затем
    TP/SL/время → выход. Одна позиция за раз. paper — симуляция без ордеров."""
    auto = not cli.no_auto
    if cli.mode == "mainnet" and not getattr(cli, "confirm_live", False):
        auto = False
    syms = scalp_symbols(cli)
    fee_bps = scalp_fee_bps(cli)
    notify(f"🤖 SCALP start: mode={cli.mode} symbols={','.join(syms)} "
           f"tf={cli.scalp_interval}m ema={cli.scalp_fast}/{cli.scalp_slow} "
           f"tp={cli.scalp_tp_bps:g}bps sl={cli.scalp_sl_bps:g}bps "
           f"max_hold={cli.scalp_max_hold:g}min fee={fee_bps:g}bps "
           f"auto={'on' if auto else 'off'} capital=${cli.capital:.0f} "
           f"leverage={cli.leverage}x")
    last_hb = time.time()
    try:
        while True:
            t0 = time.time()
            try:
                st = load_scalp()
                pos = st.get("position")
                if pos:
                    k = fetch_klines(pos["symbol"], cli.scalp_interval, 3,
                                     mode=pos["mode"])
                    price = k[-1]["c"] if k else pos["entry"]
                    exit_now, reason = decide_scalp_exit(
                        pos, price, int(time.time() * 1000), cli.scalp_tp_bps,
                        cli.scalp_sl_bps, cli.scalp_max_hold, fee_bps)
                    if exit_now:
                        close_scalp(cli, pos, price, reason)
                        last_hb = time.time()
                    elif time.time() - last_hb >= 60:
                        last_hb = time.time()
                        net = scalp_net_bps(pos["side"], pos["entry"],
                                            price, fee_bps)
                        log(f"HOLD SCALP {pos['symbol']} {pos['side']} "
                            f"net={net:+.1f}bps")
                else:
                    sig_found = False
                    for sym in syms:
                        k = fetch_klines(sym, cli.scalp_interval,
                                         max(cli.scalp_slow + 5, 60),
                                         mode=cli.mode)
                        closes = [c["c"] for c in k[:-1]]
                        sig, _ = scalp_signal(closes, cli.scalp_fast,
                                              cli.scalp_slow)
                        if not sig:
                            continue
                        price = k[-1]["c"] if k else 0.0
                        sig_found = True
                        if not auto:
                            log(f"сигнал {sym} {sig} @ {price:g} "
                                f"(авто-вход выключен)")
                            break
                        open_scalp(cli, sym, sig, price)
                        break
                    if not sig_found and time.time() - last_hb >= 60:
                        last_hb = time.time()
                        log("сигналов нет — ждём")
            except (Exception, SystemExit) as e:  # noqa: BLE001 — цикл не падает
                notify(f"❌ ошибка скальп-цикла: {e}", key="scalp-error", min_gap=600)
            time.sleep(max(1.0, float(cli.interval) - (time.time() - t0)))
    except KeyboardInterrupt:
        log("SCALP stop (Ctrl+C)")


# ------------------------------------------------------------------ cli
def main():
    p = argparse.ArgumentParser(description="Исполнитель дельта-нейтральной пары "
                                            "long spot + short perp (Bybit)")
    p.add_argument("command", choices=["scan", "enter", "status", "check",
                                       "close", "watch", "stats", "void",
                                       "memory", "sanity"])
    p.add_argument("--mode", choices=["paper", "testnet", "mainnet"],
                   default="paper",
                   help="paper: симуляция (default); testnet: ордера на testnet; "
                        "mainnet: РЕАЛЬНЫЕ деньги (нужен --confirm-live)")
    p.add_argument("--confirm-live", action="store_true",
                   help="подтверждение реальных денег: без него mainnet-вход запрещён")
    p.add_argument("--symbol", default=None,
                   help="символ для enter (без него — авто) или для "
                        "close/void — какую пару портфеля закрывать")
    p.add_argument("--all", dest="close_all", action="store_true",
                   help="для close/void: закрыть/снять ВСЕ пары портфеля")
    p.add_argument("--force", action="store_true",
                   help="войти даже если символ не проходит фильтр")
    p.add_argument("--capital", type=float, default=1000.0,
                   help="капитал на ОДНУ пару: спот-нотион + маржа перпа "
                        "(в портфеле урезается по свободной марже)")
    p.add_argument("--max-positions", dest="max_positions", type=int,
                   default=1, metavar="N",
                   help="сколько пар держать одновременно (портфель). Вход "
                        "делит свободную маржу между парами: новая пара "
                        "получает min(--capital, свободно/1.1). 1 — прежнее "
                        "поведение «одна позиция» (default 1)")
    p.add_argument("--leverage", type=float, default=2.0,
                   help="плечо перп-ноги, ≤3 (default 2)")
    p.add_argument("--interval", type=int, default=15,
                   help="секунды между итерациями watch (default 15)")
    p.add_argument("--no-auto", action="store_true",
                   help="watch без авто-входа: только сигналы в лог")
    p.add_argument("--fast-top", type=int, default=8,
                   help="сколько кандидатов быстрого скана проверять глубоко (default 8)")
    p.add_argument("--fast-apy", type=float, default=3.0,
                   help="мин. ТЕКУЩИЙ APY%% для быстрого отсева (default 3)")
    p.add_argument("--full-every", type=int, default=300,
                   help="полный скан (prefilter символов) каждые N секунд "
                        "(default 300)")
    p.add_argument("--exit-neg", type=int, default=3,
                   help="выход после N отрицательных ставок подряд (default 3)")
    p.add_argument("--hold-payouts", type=int, default=2,
                   help="авто-выход после N успешных выплат (hold-to-N; "
                        "0 — выключить; default 2)")
    p.add_argument("--max-loss", type=float, default=5.0,
                   help="выход при убытке больше N%% капитала (default 5)")
    p.add_argument("--max-spread", type=float, default=0.5,
                   help="отменять вход, если спред стакана больше N%% "
                        "(0 — выключить; default 0.5)")
    p.add_argument("--max-slip-bps", type=float, default=15.0,
                   help="защита от проскальзывания: IOC-лимитка по границе "
                        "bid/ask ±N базисных пунктов (0 — выключить, "
                        "только маркет; default 15 = 0.15%%)")
    p.add_argument("--min-depth", type=float, default=1.0,
                   help="мин. глубина стороны входа top-20 в нотионах "
                        "(0 — выключить; default 1.0)")
    p.add_argument("--max-margin-risk", type=float, default=70.0,
                   help="выход, если убыток шорта съел больше N%% маржи "
                        "(0 — выключить; default 70)")
    p.add_argument("--max-delta-pct", type=float, default=1.0,
                   help="выход, если нотионы ног разошлись больше N%% "
                        "(потеря хеджа; 0 — выключить; default 1. На testnet "
                        "котировки прыгают — можно 3)")
    p.add_argument("--max-entry-basis-pct", type=float, default=0.5,
                   help="фильтр кандидатов: макс. |базис| спот/перп ПО СТАКАНАМ "
                        "на момент проверки, %% (гнилая котировка ленты ≠ "
                        "стакан; default 0.5; 0 — выключить)")
    p.add_argument("--integrity-every", type=float, default=60.0,
                   help="сверка position.json с биржей раз в N сек: "
                        "алерт при расхождении ног (0 — выключить; default 60)")
    p.add_argument("--report-every", type=float, default=60.0,
                   help="в watch: отчёт в лог и Telegram каждые N минут — "
                        "баланс счёта, PnL за день, открытая позиция и "
                        "закрытые сделки (0 — выключить; default 60 = раз в час)")
    p.add_argument("--daily-report-at", default="00:00", metavar="ЧЧ:ММ",
                   help="в watch: ежедневная сводка (баланс счёта, итог дня "
                        "и недели) раз в сутки в это время по поясу "
                        "--report-tz; off — выключить (default 00:00)")
    p.add_argument("--report-tz", default="", metavar="TZ",
                   help="часовой пояс отчётов: имя IANA (Europe/Moscow) или "
                        "сдвиг (+03:00); пусто — время сервера (обычно UTC)")
    p.add_argument("--no-notify", action="store_true",
                   help="не слать уведомления в Telegram")
    p.add_argument("--no-exchange-stop", action="store_true",
                   help="не выставлять биржевой стоп-лосс на перп-ногу "
                        "(по умолчанию выставляется: страховка, если бот/VPS "
                        "не работают). Только для отладки")
    p.add_argument("--maker-wait", type=float, default=6.0,
                   help="сколько секунд ждать исполнения maker-лимитки перед "
                        "фолбэком на маркет (default 6)")
    p.add_argument("--taker-only", action="store_true",
                   help="входить только маркет-ордерами (без maker-лимиток)")
    p.add_argument("--ws", action="store_true",
                   help="лента WebSocket (tickers.* пуши ~100 мс): контроль "
                        "позиции каждую секунду без опроса REST, поиск "
                        "кандидатов из памяти; сделки по-прежнему REST")
    p.add_argument("--no-learn", action="store_true",
                   help="не использовать память самообучения (не читать "
                        "memory.json, не учиться на своих сделках)")
    p.add_argument("--relearn", action="store_true",
                   help="для команды memory: перестроить память с нуля "
                        "из всего bot.log")
    p.add_argument("--autotune", action="store_true",
                   help="для команды memory: внеплановый автотюнинг порогов "
                        "по фактической прибыли сделок (не дожидаясь недели)")
    p.add_argument("--fix", action="store_true",
                   help="для команды sanity: записать пометку подозрительных "
                        "сделок в position.json/memory.json (с резервной "
                        "копией рядом)")
    p.add_argument("--apply-tune", action="store_true",
                   help="для команды memory: принудительно пересобрать и "
                        "применить пороги (base + bias + profit-сдвиг) "
                        "к фильтру прямо сейчас")
    # пороги фильтра — те же, что у монитора
    p.add_argument("--min-volume", type=float, default=3.0,
                   help="мин. оборот 24ч для предотбора, $M (default 3)")
    p.add_argument("--min-oi", type=float, default=3.0,
                   help="мин. открытый интерес для предотбора, $M (default 3)")
    p.add_argument("--prefilter", type=int, default=60,
                   help="топ-N по ставке: полный скан + база подписок WS "
                        "(default 60)")
    p.add_argument("--pass-oi", type=float, default=10.0,
                   help="мин. OI для вердикта «годен», $M (default 10; "
                        "в пресете hold 20 — вернуть: --pass-oi 20)")
    # пороги вердикта, раньше бывшие только у монитора: без этих флагов
    # их нельзя было задать ни CLI, ни config.json (apply_config падал
    # на неизвестном ключе), и watch всегда вердиковал по дефолту пресета
    p.add_argument("--min-apy30", type=float, default=None,
                   help="порог вердикта: мин. APY%% за 30 дней (None = "
                        "значение пресета; hold: 10)")
    p.add_argument("--min-pos", type=float, default=None,
                   help="порог вердикта: мин. доля плюсовых интервалов "
                        "30д, %% (None = значение пресета; hold: 80)")
    p.add_argument("--min-basis", type=float, default=None,
                   help="порог вердикта: мин. базис перп/спот, %% (None = "
                        "значение пресета; hold: 0)")
    p.add_argument("--max-breakeven", type=float, default=None,
                   help="порог вердикта: макс. окупаемость входа, дней "
                        "(None = значение пресета; hold: 14)")
    p.add_argument("--preset", choices=["hold", "spike", "mainnet"], default="hold")
    p.add_argument("--spot-fee", type=float, default=DEFAULT_SPOT_FEE)
    p.add_argument("--perp-taker-fee", type=float, default=DEFAULT_PERP_TAKER_FEE)
    p.add_argument("--perp-maker-fee", type=float, default=DEFAULT_PERP_MAKER_FEE)
    # --- скальпер (--strategy scalp): отдельный режим, отдельный state ---
    p.add_argument("--strategy", choices=["funding", "scalp"], default="funding",
                   help="funding: дельта-нейтральный арбитраж (default); "
                        "scalp: направленный скальпинг перпа мейджоров на минутах")
    p.add_argument("--scalp-symbols", default="BTCUSDT,ETHUSDT,SOLUSDT",
                   help="символы скальпера через запятую (default BTC/ETH/SOL)")
    p.add_argument("--scalp-interval", default="1",
                   help="таймфрейм свечей скальпера: 1/3/5 мин (default 1)")
    p.add_argument("--scalp-fast", type=int, default=9,
                   help="период EMA fast (default 9)")
    p.add_argument("--scalp-slow", type=int, default=21,
                   help="период EMA slow (default 21)")
    p.add_argument("--scalp-tp-bps", type=float, default=SCALP_TP_BPS,
                   help="take-profit в NET б.п. после комиссий (default 20)")
    p.add_argument("--scalp-sl-bps", type=float, default=SCALP_SL_BPS,
                   help="stop-loss в NET б.п. (default 15)")
    p.add_argument("--scalp-max-hold", type=float, default=SCALP_MAX_HOLD_MIN,
                   help="макс. удержание в минутах — выход по времени (default 5)")
    p.add_argument("--scalp-fee-bps", type=float, default=None,
                   help="комиссии туда-обратно в б.п. "
                        "(default: 2× --perp-taker-fee)")
    p.add_argument("--config", default=None, metavar="FILE",
                   help="файл с параметрами (по умолчанию config.json рядом "
                        "со скриптом, если есть). Значения из командной строки "
                        "перекрывают конфиг")

    # конфиг подставляется ДО parse_args, но только для параметров, которых
    # нет в argv: так `watch --capital 500` перекрывает конфиг, а не наоборот
    apply_config(p)

    cli = p.parse_args()

    # Telegram: ключи TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID из .env или окружения
    _load_env_file()
    _TG["token"] = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    _TG["chat"] = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    _TG["enabled"] = not cli.no_notify

    # Отчёты: часовой пояс и время ежедневной сводки. Мусор в конфиге не
    # должен ронять бота — пишем в лог и работаем как с выключенным.
    cli.report_tz_obj = parse_tz(getattr(cli, "report_tz", ""))
    cli.daily_at = parse_hhmm(getattr(cli, "daily_report_at", ""))
    if str(getattr(cli, "daily_report_at", "") or "").strip() \
            and cli.daily_at is None:
        log(f"daily-report-at='{cli.daily_report_at}' не распознан (нужно "
            f"ЧЧ:ММ или off) — ежедневная сводка выключена")

    # пороги пресета как у монитора (enrich их ожидает).
    # fast_apy особый: дефолт argparse (3.0) стоит всегда — заменяем его
    # значением пресета, если пользователь не указал флаг явно (иначе
    # mainnet-пресет с fast_apy=6 не применился бы к быстрому скану)
    cli_fast_apy_default = 3.0
    # Дефолты argparse, которые ПРЕСЕТ должен перекрывать: пользователь их
    # не задавал, значит это не его выбор. Раньше проверялся только fast_apy —
    # pass_oi со своим дефолтом 10.0 всегда «побеждал» пресет, и для
    # --preset mainnet фильтровал по $10M вместо $30M (втрое тоньше книги,
    # чем задумано пресетом). Тот же класс: min-volume/min-oi.
    cli_preset_overridable = {
        "fast_apy": cli_fast_apy_default,
        "pass_oi": 10.0,
        "min_volume": 3.0,
        "min_oi": 3.0,
    }
    for key, default in PRESETS[cli.preset].items():
        cur = getattr(cli, key, None)
        if cur is None or cur == cli_preset_overridable.get(key):
            setattr(cli, key, default)

    handlers = {
        "scan": cmd_scan, "enter": cmd_enter, "status": cmd_status,
        "check": cmd_check, "close": cmd_close, "watch": cmd_watch,
        "stats": cmd_stats, "void": cmd_void, "memory": cmd_memory,
        "sanity": cmd_sanity,
    }
    try:
        handlers[cli.command](cli)
    except KeyboardInterrupt:
        print("\nПрервано.")
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
        # сетевой сбой — понятное сообщение вместо traceback (бот должен переживать обрывы)
        print(f"\nСетевая ошибка: {e}\nПроверьте интернет/VPN и повторите команду.",
              file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
