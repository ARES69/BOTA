"""Тесты защиты от проскальзывания: граница bid/ask, тик-округление,
IOC-исполнение и семантика «вход отменяется / выход добирает маркетом».
Запуск: python test_slippage.py  (stdlib, без pytest)."""
from __future__ import annotations

import os
import sys
import types
from decimal import Decimal

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


def check(name, cond):
    print(("OK   " if cond else "FAIL ") + name)
    return cond


class FakeClient:
    """Клиент-заглушка: считает вызовы IOC и маркета, возвращает заданный fill."""

    def __init__(self, ioc_fill=0.0):
        self.ioc_fill = ioc_fill
        self.ioc_calls = []
        self.market_calls = []

    def ioc_order(self, symbol, side, qty, price, category="linear",
                  reduce_only=False, wait=6.0):
        self.ioc_calls.append((symbol, side, qty, price, category, reduce_only))
        return self.ioc_fill

    def market_order(self, symbol, side, qty, category="linear",
                     reduce_only=False):
        self.market_calls.append((symbol, side, qty, category, reduce_only))


def cli_of(max_slip_bps=15.0):
    return types.SimpleNamespace(max_slip_bps=max_slip_bps)


def patch_steps(step="0.001", tick="0.05"):
    """Подменяем сетевые запросы шага лота и тика цены."""
    oq, op = fe.qty_step, fe.price_tick
    fe.qty_step = lambda symbol, category: Decimal(step)
    fe.price_tick = lambda symbol, category: Decimal(tick)
    return oq, op


OB = {"b": [["2400.0", "5"]], "a": [["2401.0", "5"]]}


def main() -> int:
    ok = True

    # 1. граница цены: Sell — не ниже bid−slip, Buy — не выше ask+slip
    ok &= check("Sell: граница = bid×(1−slip)",
                abs(fe.bounded_limit_price("Sell", 2400.0, 2401.0, 15.0)
                    - 2400.0 * (1 - 0.0015)) < 1e-9)
    ok &= check("Buy: граница = ask×(1+slip)",
                abs(fe.bounded_limit_price("Buy", 2400.0, 2401.0, 15.0)
                    - 2401.0 * (1 + 0.0015)) < 1e-9)
    ok &= check("slip=0 → защита выключена (None)",
                fe.bounded_limit_price("Sell", 2400.0, 2401.0, 0) is None)
    ok &= check("нет стакана → None",
                fe.bounded_limit_price("Sell", 0.0, 0.0, 15.0) is None)

    # 2. округление к тику: Sell вниз, Buy вверх (IOC остаётся исполнимым)
    tick = Decimal("0.05")
    ok &= check("round_price вниз", fe.round_price(2400.0731, tick, "down")
                == Decimal("2400.05"))
    ok &= check("round_price вверх", fe.round_price(2400.0701, tick, "up")
                == Decimal("2400.10"))
    ok &= check("round_price без тика — как есть",
                fe.round_price(2400.07, Decimal("0"), "up") == Decimal("2400.07"))

    # 3. suggest_price_for: граница округлена в защитную сторону
    oq, op = patch_steps(tick="0.05")
    p_sell = fe.suggest_price_for(cli_of(15.0), "ETHUSDT", "linear", "Sell", OB)
    p_buy = fe.suggest_price_for(cli_of(15.0), "ETHUSDT", "linear", "Buy", OB)
    ok &= check("Sell-граница округлена вниз к тику",
                Decimal(str(p_sell)) % Decimal("0.05") == 0 and p_sell < 2400.0)
    ok &= check("Buy-граница округлена вверх к тику",
                Decimal(str(p_buy)) % Decimal("0.05") == 0 and p_buy > 2401.0)
    ok &= check("--max-slip-bps 0 → границы нет (маркет как раньше)",
                fe.suggest_price_for(cli_of(0), "ETHUSDT", "linear", "Sell", OB) == 0.0)
    fe.qty_step, fe.price_tick = oq, op

    # 4. IOC исполнил всё → маркета нет
    oq, op = patch_steps(step="0.001", tick="0.05")
    c = FakeClient(ioc_fill=0.25)
    done = fe.fill_with_slippage(c, cli_of(), "ETHUSDT", "Sell", 0.25, "linear",
                                 ob=OB, allow_market=False)
    ok &= check("IOC полностью исполнил → filled=qty", done == 0.25)
    ok &= check("IOC полностью исполнил → маркет не вызывался", not c.market_calls)
    ok &= check("IOC-ордер ушёл с границей и категорией",
                c.ioc_calls and c.ioc_calls[0][4] == "linear"
                and c.ioc_calls[0][3] < 2400.0)

    # 5. IOC не исполнил и allow_market=False (вход) → RuntimeError, без маркета
    c = FakeClient(ioc_fill=0.0)
    try:
        fe.fill_with_slippage(c, cli_of(), "ETHUSDT", "Sell", 0.25, "linear",
                              ob=OB, allow_market=False)
        ok &= check("вход: IOC пусто → RuntimeError", False)
    except RuntimeError as e:
        ok &= check("вход: IOC пусто → RuntimeError",
                    "границ" in str(e) and not c.market_calls)

    # 6. IOC не исполнил, allow_market=True (выход) → добираем маркетом
    c = FakeClient(ioc_fill=0.0)
    done = fe.fill_with_slippage(c, cli_of(), "ETHUSDT", "Buy", 0.25, "linear",
                                 ob=OB, reduce_only=True, allow_market=True)
    ok &= check("выход: IOC пусто → маркет с reduceOnly",
                done == 0.25 and bool(c.market_calls)
                and c.market_calls[0][4] is True)

    # 7. частичное IOC на выходе: остаток добирается маркетом
    c = FakeClient(ioc_fill=0.10)
    done = fe.fill_with_slippage(c, cli_of(), "ETHUSDT", "Buy", 0.25, "linear",
                                 ob=OB, allow_market=True)
    ok &= check("выход: частичный IOC → остаток маркетом",
                done == 0.25 and bool(c.market_calls)
                and abs(c.market_calls[0][2] - 0.15) < 1e-9)

    # 8. частичный IOC на входе → RuntimeError (ногу откатит вызывающий)
    c = FakeClient(ioc_fill=0.10)
    try:
        fe.fill_with_slippage(c, cli_of(), "ETHUSDT", "Sell", 0.25, "linear",
                              ob=OB, allow_market=False)
        ok &= check("вход: частичный IOC → RuntimeError", False)
    except RuntimeError:
        ok &= check("вход: частичный IOC → RuntimeError", not c.market_calls)

    # 9. выключенная защита (0 bps) → сразу маркет, как раньше
    c = FakeClient(ioc_fill=0.25)
    done = fe.fill_with_slippage(c, cli_of(0), "ETHUSDT", "Sell", 0.25, "linear",
                                 ob=OB, allow_market=False)
    ok &= check("--max-slip-bps 0 → маркет без IOC",
                done == 0.25 and not c.ioc_calls and bool(c.market_calls))

    # 10. IOC упал с исключением: выход продолжается маркетом, вход отменяется
    class Boom(FakeClient):
        def ioc_order(self, *a, **k):
            raise RuntimeError("10001 price invalid")

    c = Boom()
    done = fe.fill_with_slippage(c, cli_of(), "ETHUSDT", "Buy", 0.25, "linear",
                                 ob=OB, allow_market=True)
    ok &= check("выход: IOC-ошибка → маркет",
                done == 0.25 and bool(c.market_calls))
    c = Boom()
    try:
        fe.fill_with_slippage(c, cli_of(), "ETHUSDT", "Sell", 0.25, "linear",
                              ob=OB, allow_market=False)
        ok &= check("вход: IOC-ошибка → RuntimeError", False)
    except RuntimeError:
        ok &= check("вход: IOC-ошибка → RuntimeError", not c.market_calls)

    fe.qty_step, fe.price_tick = oq, op
    print("\nИтог: " + ("все проверки пройдены" if ok else "ЕСТЬ ПРОВАЛЫ"))
    return 0 if ok else 1


if __name__ == "__main__":
    # Тестовые ветки IOC пишут через fe.log в рабочий bot.log: после
    # прогона в лог бота попадали строки вроде «IOC ETHUSDT Sell: 10001
    # price invalid». На здоровье бота не влияло, но лог засорялся —
    # подменяем путь лога на время теста и возвращаем, что было.
    import tempfile

    _orig_log = fe.LOG_FILE
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".log", delete=False) as _tf:
            _tmp_log = _tf.name
        fe.LOG_FILE = _tmp_log
        sys.exit(main())
    finally:
        fe.LOG_FILE = _orig_log
        try:
            os.remove(_tmp_log)
        except (OSError, NameError):
            pass
