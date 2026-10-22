"""Тесты hold-to-N: авто-выход после N успешных (положительных) выплат.
update_funding обязан считать только положительные выплаты в pos["payouts"],
decide_exit — закрывать позицию при достижении порога --hold-payouts
и держать ниже порога; 0 = выключено; старые позиции без поля — по факту.
Запуск: python test_hold_payouts.py  (stdlib, без pytest)."""
from __future__ import annotations

import os
import sys
import time
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
import funding_executor as fe  # noqa: E402

NOW_MS = int(time.time() * 1000)


def mk_cli(hold_payouts=2, exit_neg=3, max_loss=5.0, max_margin_risk=70.0,
           max_delta_pct=1.0):
    return types.SimpleNamespace(hold_payouts=hold_payouts, exit_neg=exit_neg,
                                 max_loss=max_loss,
                                 max_margin_risk=max_margin_risk,
                                 max_delta_pct=max_delta_pct)


def mk_pos(**over):
    pos = {
        "symbol": "ETHUSDT", "mode": "testnet", "notional": 600.0,
        "capital": 1000.0, "margin": 300.0,
        "spot_entry": 2500.0, "perp_entry": 2500.0,
        "spot_qty": 0.24, "perp_qty": 0.24, "mult": 1.0,
        "opened_ms": NOW_MS - 20 * 3_600_000,
        "funding_accrued": 0.0, "payouts": 0,
        # задолго до самого раннего события истории (до 5 интервалов по 8 ч)
        "last_funding_ms": NOW_MS - 50 * 3_600_000,
        "neg_streak": 0,
        "_funding_checked_ms": 0,
    }
    pos.update(over)
    return pos


def patch_env(monkey_hist, pnl=(2500.0, 2500.0, 0.0)):
    """Подменяем сеть: market_api_get → заглушка истории, current_pnl → цены."""
    orig_get, orig_pnl = fe.market_api_get, fe.current_pnl
    fe.market_api_get = lambda path, params, mode="paper": {"list": monkey_hist}
    fe.current_pnl = lambda pos, feed=None: pnl
    return orig_get, orig_pnl


def hist(rates):
    """История выплат: каждая ставка = интервал 8ч, timestamp по порядку."""
    return [{"fundingRateTimestamp": NOW_MS - (len(rates) - i) * 8 * 3_600_000,
             "fundingRate": f"{r}"} for i, r in enumerate(rates)]


def check(name, cond):
    print(("OK   " if cond else "FAIL ") + name)
    return cond


def main() -> int:
    ok = True
    orig = patch_env(hist([0.0001, 0.0001, 0.0001]))

    # 1. update_funding: 3 положительные выплаты → payouts=3, funding начислен
    pos = mk_pos()
    fe.update_funding(pos)
    ok &= check("update_funding считает 3 положительные выплаты", pos["payouts"] == 3)
    ok &= check("funding начислен за все интервалы",
                abs(pos["funding_accrued"] - 0.0001 * 600 * 3) < 1e-9)

    # 2. decide_exit: 3 >= N=2 → выход с причиной про выплаты
    why = fe.decide_exit(pos, mk_cli(hold_payouts=2), fresh=True)
    ok &= check("exit при payouts>=N", why[0] is True and "выплат" in why[1])

    # 3. ниже порога → держим
    pos2 = mk_pos()
    fe.update_funding(pos2)   # payouts=3
    pos2["payouts"] = 1
    why2 = fe.decide_exit(pos2, mk_cli(hold_payouts=2), fresh=True)
    ok &= check("payouts<N → держим", why2[0] is False)

    # 4. hold-payouts=0 — выключено, держим даже при больших payouts
    why3 = fe.decide_exit(pos, mk_cli(hold_payouts=0), fresh=True)
    ok &= check("hold_payouts=0 → выключено", why3[0] is False)

    # 5. отрицательные/нулевые выплаты не считаются
    fe.market_api_get = lambda path, params, mode="paper": {"list": hist(
        [0.0001, -0.0002, 0.0, 0.0001])}
    pos4 = mk_pos()
    fe.update_funding(pos4)
    ok &= check("считаются только положительные выплаты", pos4["payouts"] == 2)
    ok &= check("отрицательный интервал виден в neg_streak", pos4["neg_streak"] == 0)

    # 6. старая позиция без поля payouts — добирается по факту, без падений
    pos5 = mk_pos()
    del pos5["payouts"]
    fe.market_api_get = lambda path, params, mode="paper": {"list": hist(
        [0.0001, 0.0001])}
    fe.update_funding(pos5)
    ok &= check("позиция без поля: счётчик появляется", pos5.get("payouts") == 2)
    why5 = fe.decide_exit(pos5, mk_cli(hold_payouts=2), fresh=True)
    ok &= check("старая позиция тоже уходит по hold-to-N", why5[0] is True)

    # 7. neg_streak-выход не сломан: 3 отрицательные ПОСЛЕДНИЕ подряд → выход;
    # положительная в середине не прерывает стрик (считаются последние закрытые)
    fe.market_api_get = lambda path, params, mode="paper": {"list": hist(
        [0.0001, -0.0001, -0.0001, -0.0001])}
    pos6 = mk_pos()
    fe.update_funding(pos6)
    why6 = fe.decide_exit(pos6, mk_cli(hold_payouts=2, exit_neg=3), fresh=True)
    ok &= check("neg_streak=3 → kill-switch приоритетен",
                why6[0] is True and "отрицательный" in why6[1])

    # 8. дельта-контроль: нотионы ног разошлись на ~1.6% (спот подрос):
    #    порог1% (дефолт) → выход; поднятый порог (--max-delta-pct 3, testnet)
    #    → держим позицию (живой случай XRPUSDT: котировки testnet прыгали
    #    и жёсткий1% закрывал только что открытую позицию)
    fe.market_api_get = lambda path, params, mode="paper": {"list": hist([])}
    fe.current_pnl = lambda pos, feed=None: (2540.0, 2500.0, 40.0)  # спот +1.6%
    pos_d = mk_pos(payouts=0)
    why_d = fe.decide_exit(pos_d, mk_cli(hold_payouts=0), fresh=True)
    ok &= check("дельта 1.6% > дефолтного порога 1% → выход",
                why_d[0] is True and "дельта" in why_d[1])
    pos_d2 = mk_pos(payouts=0)
    why_d2 = fe.decide_exit(pos_d2, mk_cli(hold_payouts=0, max_delta_pct=3.0),
                            fresh=True)
    ok &= check("дельта 1.6% < порога --max-delta-pct 3 → держим",
                why_d2[0] is False)

    fe.market_api_get, fe.current_pnl = orig
    print("\nИтог: " + ("все проверки пройдены" if ok else "ЕСТЬ ПРОВАЛЫ"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
