"""Тесты контроля целостности позиции: position_mismatch (биржа vs json).
Урок живьём: посторонний ордер/ликвидация снимают нашу ногу, а state
продолжает считать позицию открытой. Проверяем: ловим исчезнувшую
перп-ногу, расхождение размера, неполный/лишний спот, допуск на комиссию
в базовой монете, и что «неизвестно» (None) не даёт ложного алерта.
Запуск: python test_integrity.py  (stdlib, без pytest)."""
from __future__ import annotations

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
import funding_executor as fe  # noqa: E402


def mk_pos(**over):
    pos = {"symbol": "ETHUSDT", "spot_symbol": "ETHUSDT", "mode": "testnet",
           "perp_qty": 0.25, "spot_qty": 0.25}
    pos.update(over)
    return pos


def check(name, cond):
    print(("OK   " if cond else "FAIL ") + name)
    return cond


def main() -> int:
    ok = True
    pos = mk_pos()

    # 1. всё сходится
    ok &= check("совпадает → None", fe.position_mismatch(pos, 0.25, 0.25) is None)
    # 1b. спот меньше на комиссию входа (~0.1%) — в допуске
    ok &= check("спот −0.1% (комиссия) → None",
                fe.position_mismatch(pos, 0.25, 0.24975) is None)

    # 2. перп-нога исчезла (наш живой случай: неттинг чужой ликвидацией)
    m = fe.position_mismatch(pos, 0.0, 0.25)
    ok &= check("перп 0 → ловим «исчезла»", m is not None and "исчезла" in m)

    # 3. перп-нога другого размера
    m = fe.position_mismatch(pos, 0.20, 0.25)
    ok &= check("перп 0.20 ≠ 0.25 → расхождение",
                m is not None and "перп-нога" in m and "0.2" in m)

    # 4. спот неполный (часть продана/выведена)
    m = fe.position_mismatch(pos, 0.25, 0.10)
    ok &= check("спот 0.10 из 0.25 → «неполна»", m is not None and "неполна" in m)

    # 5. спот больше записанного (чужие средства/лишний ордер)
    m = fe.position_mismatch(pos, 0.25, 0.30)
    ok &= check("спот 0.30 vs 0.25 → «больше»", m is not None and "больше" in m)

    # 6. допуск спота 2% (граница 0.245): 0.246 внутри, 0.244 — уже расхождение
    ok &= check("спот 0.246 (в допуске)", fe.position_mismatch(pos, 0.25, 0.246) is None)
    m = fe.position_mismatch(pos, 0.25, 0.24)
    ok &= check("спот 0.24 (вне допуска)", m is not None and "неполна" in m)

    # 7. неизвестность не алертит: оба источника None
    ok &= check("оба неизвестны → None", fe.position_mismatch(pos, None, None) is None)
    ok &= check("перп неизвестен, спот ок → None",
                fe.position_mismatch(pos, None, 0.25) is None)

    # 8. обе ноги разошлись одновременно — обе причины в одном сообщении
    m = fe.position_mismatch(pos, 0.0, 0.05)
    ok &= check("обе ноги: две причины", m is not None
                and "исчезла" in m and "неполна" in m)

    # 9. перп-нога больше 2%: 0.26 при 0.25 — вне допуска (2% = 0.005)
    m = fe.position_mismatch(pos, 0.26, 0.25)
    ok &= check("перп 0.26 vs 0.25 → расхождение", m is not None)

    # 10. paper-режим не ходит на биржу: check_position_integrity → None
    cli = types.SimpleNamespace(mode="paper")
    ok &= check("paper-режим → None без сети",
                fe.check_position_integrity(mk_pos(mode="paper"), cli) is None)

    # 11. переходы алертов (анти-спам): сообщение только при смене состояния
    bad, msg = fe.integrity_transition(False, "перп-нога исчезла", "ETHUSDT")
    ok &= check("ok→bad: алерт с символом и причиной",
                bad is True and msg is not None and "ETHUSDT" in msg
                and "исчезла" in msg)
    bad2, msg2 = fe.integrity_transition(True, "перп-нога исчезла", "ETHUSDT")
    ok &= check("bad→bad: молчим (без спама)",
                bad2 is True and msg2 is None)
    bad3, msg3 = fe.integrity_transition(True, None, "ETHUSDT")
    ok &= check("bad→ok: подтверждение восстановления",
                bad3 is False and msg3 is not None and "снова совпадает" in msg3)
    bad4, msg4 = fe.integrity_transition(False, None, "ETHUSDT")
    ok &= check("ok→ok: молчим", bad4 is False and msg4 is None)

    print("\nИтог: " + ("все проверки пройдены" if ok else "ЕСТЬ ПРОВАЛЫ"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
