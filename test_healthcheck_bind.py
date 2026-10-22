"""Тесты health-check: публичный bind панели и авто-фикс расхождения позиции.

Отдельно от test_healthcheck.py — это «закалка» панели и позиции: панель
обязана слушать только localhost (иначе позиция/PnL/кнопки видны из
интернета, а ufw на VPS может быть выключен), а расхождение position.json с
биржей по умолчанию только показывается, с --fix-position — правится по
безопасному правилу void/close.

Запуск: python test_healthcheck_bind.py  (stdlib, без pytest).
"""
from __future__ import annotations

import os
import sys
import tempfile

# Консоль Windows по умолчанию cp1252 — печать кириллицы в check() роняла
# тест с UnicodeEncodeError при прямом запуске (через run_tests.py работало,
# потому что раннер выставляет PYTHONIOENCODING=utf-8).
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import healthcheck as hc  # noqa: E402


def check(name, cond):
    print(("OK   " if cond else "FAIL ") + name)
    return cond


def main() -> int:
    ok = True

    # --- авто-фикс позиции: какое действие безопасно -----------------------
    fp = {"symbol": "ETHUSDT", "spot_symbol": "ETHUSDT", "mode": "testnet",
          "perp_qty": 0.5, "spot_qty": 0.5}
    ok &= check("fix_action: биржа пуста (0/0) → void",
                hc.fix_action(fp, 0.0, 0.0) == "void")
    ok &= check("fix_action: спот-пыль в допуске → void",
                hc.fix_action(fp, 0.0, 0.001) == "void")
    ok &= check("fix_action: перп на месте → close",
                hc.fix_action(fp, 0.5, 0.0) == "close")
    ok &= check("fix_action: спот на месте → close",
                hc.fix_action(fp, 0.0, 0.5) == "close")
    ok &= check("fix_action: обе ноги неизвестны → None",
                hc.fix_action(fp, None, None) is None)

    # --- авто-фикс позиции: исполнение и проверка итога --------------------
    def run_ok(_cmd):
        return 0, "Ордера на закрытие исполнены."

    def run_err(_cmd):
        return 1, "boom"

    def run_raise(_cmd):
        raise RuntimeError("нет прав")

    def probe_flat(_pos):
        return 0.0, 0.0, None

    okf, mf = hc.apply_position_fix(fp, "close", executor_path="fe.py",
                                    run_cmd=run_ok, probe=probe_flat,
                                    read_pos=lambda: None)
    ok &= check("apply_position_fix: close ок, биржа флэт → успех",
                okf and "флэт" in mf)

    okf2, mf2 = hc.apply_position_fix(fp, "void", executor_path="fe.py",
                                      run_cmd=run_err, probe=probe_flat,
                                      read_pos=lambda: None)
    ok &= check("apply_position_fix: ненулевой код → провал",
                not okf2 and "код 1" in mf2)

    okf3, mf3 = hc.apply_position_fix(fp, "close", executor_path="fe.py",
                                      run_cmd=run_raise, probe=probe_flat,
                                      read_pos=lambda: None)
    ok &= check("apply_position_fix: исключение → провал без краха",
                not okf3 and "не запустился" in mf3)

    okf4, mf4 = hc.apply_position_fix(
        fp, "close", executor_path="fe.py", run_cmd=run_ok,
        probe=lambda _p: (0.5, 0.5, None), read_pos=lambda: None)
    ok &= check("apply_position_fix: на бирже остались ноги → провал",
                not okf4 and "остались" in mf4)

    okf5, mf5 = hc.apply_position_fix(
        fp, "close", executor_path="fe.py", run_cmd=run_ok,
        probe=lambda _p: (0.5, 0.5, None), read_pos=lambda: fp)
    ok &= check("apply_position_fix: ноги снова сошлись → успех",
                okf5 and "сошлись" in mf5)

    okf6, mf6 = hc.apply_position_fix(
        fp, "void", executor_path="fe.py", run_cmd=run_ok,
        probe=lambda _p: (None, None, "нет сети"), read_pos=lambda: None)
    ok &= check("apply_position_fix: биржа недоступна после фикса → успех",
                okf6 and "недоступна" in mf6)

    # дефолтный read_pos читает pos_path (регрессия: раньше падало TypeError)
    tmp_pos = os.path.join(tempfile.gettempdir(), "test_fixpos.json")
    with open(tmp_pos, "w", encoding="utf-8") as f:
        f.write('{"position": null}')
    okf7, mf7 = hc.apply_position_fix(
        fp, "close", executor_path="fe.py", pos_path=tmp_pos,
        run_cmd=run_ok, probe=probe_flat)
    ok &= check("apply_position_fix: дефолтный read_pos читает pos_path",
                okf7 and "флэт" in mf7)

    # --- bind панели: localhost — ок, 0.0.0.0/::/внешний IP — проблема ----
    ok &= check("loopback: 127.0.0.1/127.0.0.5/::1/localhost → True",
                hc._is_loopback("127.0.0.1") and hc._is_loopback("127.0.0.5")
                and hc._is_loopback("::1") and hc._is_loopback("localhost"))
    ok &= check("loopback: 0.0.0.0/::/внешний IP → False",
                not hc._is_loopback("0.0.0.0") and not hc._is_loopback("::")
                and not hc._is_loopback("45.9.18.57"))

    ss_out = ("State  Recv-Q Send-Q Local Address:Port Peer Address:Port\n"
              "LISTEN 0      5          127.0.0.1:8050      0.0.0.0:*\n"
              "LISTEN 0      5          [::1]:8050          [::]:*\n"
              "LISTEN 0      128        0.0.0.0:22          0.0.0.0:*\n")
    lst = hc.parse_listeners(ss_out)
    ok &= check("parse_listeners: адреса/порты, заголовок отброшен",
                len(lst) == 3 and lst[0] == {"addr": "127.0.0.1", "port": "8050"}
                and lst[1]["addr"] == "::1" and lst[2]["port"] == "22")
    ok &= check("public_bind_problems: только localhost → чисто",
                hc.public_bind_problems(lst, 8050) == [])

    wild = hc.parse_listeners(
        "State Recv-Q Send-Q Local Address:Port Peer Address:Port\n"
        "LISTEN 0      5          0.0.0.0:8050      0.0.0.0:*\n")
    probs = hc.public_bind_problems(wild, 8050)
    ok &= check("public_bind_problems: 0.0.0.0 → проблема с подсказкой",
                len(probs) == 1 and "0.0.0.0:8050" in probs[0]
                and "127.0.0.1" in probs[0])
    ok &= check("public_bind_problems: чужой порт не трогаем",
                hc.public_bind_problems(lst, 9999) == [])

    ipv6 = hc.parse_listeners(
        "State Recv-Q Send-Q Local Address:Port Peer Address:Port\n"
        "LISTEN 0      5          [::]:8050           [::]:*\n")
    ok &= check("public_bind_problems: [::] (все интерфейсы) → проблема",
                len(hc.public_bind_problems(ipv6, 8050)) == 1)
    ext = hc.parse_listeners(
        "State Recv-Q Send-Q Local Address:Port Peer Address:Port\n"
        "LISTEN 0      5          45.9.18.57:8050     0.0.0.0:*\n")
    ok &= check("public_bind_problems: внешний IP → проблема",
                len(hc.public_bind_problems(ext, 8050)) == 1)
    both = lst + wild
    ok &= check("public_bind_problems: 127.0.0.1 + 0.0.0.0 → одна проблема",
                len(hc.public_bind_problems(both, 8050)) == 1)

    # политика bind: public с паролем — ок, public без пароля — алерт
    ok &= check("bind policy: localhost → чисто",
                hc.dashboard_bind_problems(lst, 8050) == [])
    ok &= check("bind policy: 0.0.0.0 без DASHBOARD_PUBLIC → проблема",
                len(hc.dashboard_bind_problems(wild, 8050)) == 1)
    ok &= check("bind policy: public + пароль → чисто",
                hc.dashboard_bind_problems(wild, 8050, True, "pw") == [])
    no_pw = hc.dashboard_bind_problems(wild, 8050, True, "")
    ok &= check("bind policy: public без пароля → проблема",
                len(no_pw) == 1 and "DASHBOARD_PASSWORD" in no_pw[0])

    # нет `ss` — проверка не должна ронять health-check
    ok &= check("probe_listeners: нет команды → [] без исключения",
                hc.probe_listeners(("нет-такой-команды-12345",)) == []
                or hc.probe_listeners(("нет-такой-команды-12345",)) == [])

    # --- bind-проблема доходит до общего списка проблем --------------------
    show = {"ActiveState": "active", "SubState": "running", "NRestarts": 0}
    ev, _ = hc.evaluate({"dashboard": show}, {"dashboard": 0}, 10.0, 900.0,
                        hc.public_bind_problems(wild, 8050))
    ok &= check("evaluate: bind-проблема попадает в общий список",
                any("панель слушает" in p for p in ev))

    # --- авто-фикс публичного bind: рестарт → bind вернулся на localhost ---
    calls: list[str] = []

    def fake_restart(name):
        calls.append(name)

    pub = [{"addr": "0.0.0.0", "port": "8050"}]
    loc = [{"addr": "127.0.0.1", "port": "8050"}]
    noop = lambda _s: None  # noqa: E731

    okh, msg = hc.heal_public_bind(8050, "dashboard", restart=fake_restart,
                                   probe=lambda: loc, sleep_fn=noop)
    ok &= check("heal bind: рестарт → панель снова на 127.0.0.1",
                okh and calls == ["dashboard"] and "127.0.0.1" in msg)

    # публичный bind прописан в юните → рестарт не помогает, без зацикливания
    tries: list[int] = []

    def counting(_s):
        tries.append(1)

    okh2, msg2 = hc.heal_public_bind(8050, "dashboard", restart=fake_restart,
                                     probe=lambda: pub, sleep_fn=counting,
                                     tries=2, wait=0)
    ok &= check("heal bind: юнит виноват → неудача с подсказкой",
                (not okh2) and "юнит" in msg2 and len(tries) == 2)

    # bind пропал на произвольном ретрае (панель поднималась не сразу)
    seq = {"n": 0}

    def slow_probe():
        seq["n"] += 1
        return loc if seq["n"] >= 2 else pub

    okh3, msg3 = hc.heal_public_bind(8050, "dashboard", restart=fake_restart,
                                     probe=slow_probe, sleep_fn=noop,
                                     tries=5, wait=0)
    ok &= check("heal bind: bind ожил на втором зонде → успех",
                okh3 and seq["n"] == 2)

    # рестарт упал (нет прав/сервиса) — не роняем health-check
    def boom(_name):
        raise RuntimeError("no systemd here")

    okh4, msg4 = hc.heal_public_bind(8050, restart=boom, probe=lambda: pub,
                                     sleep_fn=noop)
    ok &= check("heal bind: рестарт упал → понятное сообщение, без исключения",
                (not okh4) and "не удался" in msg4)

    print()
    print("Итог:", "все проверки пройдены" if ok else "ЕСТЬ ПРОВАЛЫ")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
