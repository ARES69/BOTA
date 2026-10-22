"""Тесты health-check: разбор systemctl/.env, детекция сбоя/рестарта,
свежесть лога, анти-спам, формат сообщений.
Запуск: python test_healthcheck.py  (stdlib, без pytest)."""
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

    # 1. .env
    env = hc.parse_env("# коммент\nTELEGRAM_BOT_TOKEN=abc\n\nBAD LINE\n"
                       "TELEGRAM_CHAT_ID=123\n")
    ok &= check("parse_env: значения и игнор строк",
                env == {"TELEGRAM_BOT_TOKEN": "abc", "TELEGRAM_CHAT_ID": "123"})

    # 2. systemctl show
    show = hc.parse_show("ActiveState=active\nSubState=running\nNRestarts=0\n")
    ok &= check("parse_show: ключ=значение",
                show["ActiveState"] == "active" and show["SubState"] == "running"
                and show["NRestarts"] == "0")

    # 3. сервис активен, рестартов нет → проблем нет
    probs, nr = hc.service_problems("funding-watch", show, prev_restarts=0)
    ok &= check("активный сервис без проблем", probs == [] and nr == 0)

    # 4. сервис упал
    failed = hc.parse_show("ActiveState=failed\nSubState=dead\nNRestarts=3\n")
    probs, _ = hc.service_problems("funding-watch", failed, prev_restarts=3)
    ok &= check("упавший сервис → проблема",
                len(probs) == 1 and "не активен" in probs[0])

    # 5. сервис в авто-перезапуске
    restarting = hc.parse_show("ActiveState=activating\n"
                               "SubState=auto-restart\nNRestarts=5\n")
    probs, _ = hc.service_problems("dashboard", restarting, prev_restarts=5)
    ok &= check("авто-перезапуск → проблема",
                any("авто-перезапуск" in p or "не активен" in p for p in probs))

    # 6. выросло число перезапусков с прошлого прогона
    grew = hc.parse_show("ActiveState=active\nSubState=running\nNRestarts=7\n")
    probs, nr = hc.service_problems("funding-watch", grew, prev_restarts=5)
    ok &= check("рост NRestarts → проблема",
                any("перезапуск" in p for p in probs) and nr == 7)

    # 7. первый прогон (prev=None) — дельту не считаем
    probs, _ = hc.service_problems("funding-watch", grew, prev_restarts=None)
    ok &= check("первый прогон: рестарты не алертим", probs == [])

    # 8. свежесть лога
    ok &= check("свежий лог — ок", hc.log_age_problem(120.0, 900.0) == [])
    ok &= check("старый лог → проблема",
                bool(hc.log_age_problem(1200.0, 900.0)))
    ok &= check("нет файла лога → проблема",
                any("не найден" in p for p in hc.log_age_problem(None, 900.0)))

    # 9. агрегация
    shows = {
        "funding-watch": hc.parse_show("ActiveState=failed\nSubState=dead\nNRestarts=2\n"),
        "dashboard": hc.parse_show("ActiveState=active\nSubState=running\nNRestarts=0\n"),
    }
    problems, new_restarts = hc.evaluate(shows, {"funding-watch": 1,
                                                 "dashboard": 0}, 30.0, 900.0)
    ok &= check("evaluate: собрал проблемы и перезапуски",
                len(problems) == 2 and new_restarts["funding-watch"] == 2)
    ok &= check("evaluate: всё ок → пусто",
                hc.evaluate({"a": show}, {"a": 0}, 10.0, 900.0)[0] == [])

    # 10. отпечаток проблем не зависит от порядка
    s1 = hc.problem_signature(["b", "a"])
    s2 = hc.problem_signature(["a", "b"])
    ok &= check("signature: порядок не важен", s1 == s2)
    ok &= check("signature: разные наборы → разный хеш",
                s1 != hc.problem_signature(["a", "c"]))

    # 11. анти-спам
    now = 1_700_000_000_000
    recent = {"sig": "deadbeef", "ts_ms": now - 3_600_000}   # 1 ч назад
    old = {"sig": "deadbeef", "ts_ms": now - 30 * 3_600_000}  # 30 ч назад
    ok &= check("анти-спам: новая проблема → слать",
                hc.should_send("new", recent, now, 20, False))
    ok &= check("анти-спам: та же проблема недавно → молчать",
                not hc.should_send("deadbeef", recent, now, 20, False))
    ok &= check("анти-спам: та же проблема давно → слать снова",
                hc.should_send("deadbeef", old, now, 20, False))
    ok &= check("анти-спам: --force игнорирует подавление",
                hc.should_send("deadbeef", recent, now, 20, True))
    ok &= check("анти-спам: пустой отпечаток → не слать",
                not hc.should_send("", recent, now, 20, False))

    # 12. формат сообщений
    msg = hc.format_message(["funding-watch: сервис не активен (failed/dead)"],
                            ["funding-watch", "dashboard"], 30.0)
    ok &= check("format_message: содержит проблему и сервисы",
                "не активен" in msg and "systemctl status funding-watch" in msg)
    ok &= check("format_ok: подтверждение",
                "всё в порядке" in hc.format_ok(["dashboard"], 60.0))

    # 12b. позиция: сверка position.json с биржей (тихая потеря ноги)
    pos = {"symbol": "ETHUSDT", "spot_symbol": "ETHUSDT",
           "perp_qty": 0.5, "spot_qty": 0.5, "mode": "testnet"}
    ok &= check("позиция: сходится → нет проблем",
                hc.position_problems(pos, 0.5, 0.5) == [])
    ok &= check("позиция: перп-нога исчезла → проблема",
                any("исчезла" in p for p in hc.position_problems(pos, 0.0, 0.5)))
    ok &= check("позиция: перп вне допуска → проблема",
                any("≠" in p for p in hc.position_problems(pos, 0.4, 0.5)))
    ok &= check("позиция: перп в допуске 2% → ок",
                hc.position_problems(pos, 0.505, 0.5) == [])
    ok &= check("позиция: спот неполон → проблема",
                any("неполна" in p for p in hc.position_problems(pos, 0.5, 0.4)))
    ok &= check("позиция: спот больше записи → проблема",
                any("больше" in p for p in hc.position_problems(pos, 0.5, 0.7)))
    ok &= check("позиция: неизвестно (None) → нет алерта",
                hc.position_problems(pos, None, None) == [])
    ok &= check("позиция: запись без ног → не сравниваем",
                hc.position_problems({"symbol": "X", "perp_qty": 0,
                                      "spot_qty": 0}, 0.0, 0.0) == [])
    ok &= check("позиция: шорт больше записи → проблема",
                any("≠" in p for p in hc.position_problems(pos, 0.9, 0.5)))

    # base coin спот-ноги
    ok &= check("_base_coin: ETHUSDT → ETH", hc._base_coin("ETHUSDT") == "ETH")
    ok &= check("_base_coin: BTCUSDC → BTC", hc._base_coin("BTCUSDC") == "BTC")
    ok &= check("_base_coin: 1000PEPEUSDT → 1000PEPE",
                hc._base_coin("1000PEPEUSDT") == "1000PEPE")

    # read_position
    pj = os.path.join(tempfile.gettempdir(), "test_pos.json")
    with open(pj, "w", encoding="utf-8") as f:
        f.write('{"position": {"symbol": "ETHUSDT", "perp_qty": 1}}')
    ok &= check("read_position: читает позицию",
                (hc.read_position(pj) or {}).get("symbol") == "ETHUSDT")
    with open(pj, "w", encoding="utf-8") as f:
        f.write('{"position": null}')
    ok &= check("read_position: null → None", hc.read_position(pj) is None)
    ok &= check("read_position: нет файла → None",
                hc.read_position(pj + ".nope") is None)

    # probe_position без сети: paper — не проверяем; без ключей — примечание
    perp, spot, note = hc.probe_position({"mode": "paper"})
    ok &= check("probe_position: paper → биржа не проверяется",
                perp is None and spot is None and note is not None)
    saved = {k: os.environ.pop(k, None)
             for k in ("BYBIT_TESTNET_API_KEY", "BYBIT_TESTNET_API_SECRET")}
    perp, spot, note = hc.probe_position({"mode": "testnet",
                                          "symbol": "BTCUSDT",
                                          "spot_symbol": "BTCUSDT"})
    ok &= check("probe_position: нет ключей → примечание, без алерта",
                perp is None and spot is None and note is not None
                and "ключ" in note)
    for k, v in saved.items():
        if v is not None:
            os.environ[k] = v

    # evaluate: extra-проблемы входят в итог
    p2, _ = hc.evaluate({"a": show}, {"a": 0}, 10.0, 900.0,
                        ["позиция X: расхождение"])
    ok &= check("evaluate: extra-проблемы попадают в итог",
                p2 == ["позиция X: расхождение"])

    # 12c. подсказка по расхождению позиции
    h_perp_gone = hc.recovery_hint(pos, 0.0, 0.5)
    ok &= check("подсказка: перп исчез → void --force",
                h_perp_gone is not None and "void --force" in h_perp_gone)
    h_mismatch = hc.recovery_hint(pos, 0.4, 0.5)
    ok &= check("подсказка: расхождение → close",
                h_mismatch is not None and "close" in h_mismatch)
    ok &= check("подсказка: только спот неполон → close",
                "close" in (hc.recovery_hint(pos, 0.5, 0.1) or ""))
    ok &= check("подсказка: неизвестно → None",
                hc.recovery_hint(pos, None, None) is None)

    # формат алерта вставляет подсказку
    msg_hint = hc.format_message(["p"], ["s"], 10.0, "делай так")
    ok &= check("format_message: подсказка в тексте",
                "💡 делай так" in msg_hint)
    ok &= check("format_message: без подсказки — без блока",
                "💡" not in hc.format_message(["p"], ["s"], 10.0))

    # 12d. авто-восстановление
    ok &= check("_fresh: свежий / старый / нет файла",
                hc._fresh(5.0, 900.0) and not hc._fresh(1200.0, 900.0)
                and not hc._fresh(None, 900.0))

    calls = []

    def fake_run(name):
        calls.append(name)

    active = {"ActiveState": "active"}
    nope = lambda _s: None                      # без реального сна
    healed, m1 = hc.restart_service(
        "funding-watch", "watch.log", 900.0, run_cmd=fake_run,
        probe=lambda n: active, log_age_fn=lambda p: 3.0,
        sleep_fn=nope, tries=2, wait=0)
    ok &= check("autoheal: перезапуск + поднялся → успех",
                healed and "перезапущен" in m1 and calls == ["funding-watch"])

    healed2, m2 = hc.restart_service(
        "funding-watch", "watch.log", 900.0, run_cmd=fake_run,
        probe=lambda n: active, log_age_fn=lambda p: 9999.0,
        sleep_fn=nope, tries=2, wait=0)
    ok &= check("autoheal: лог не ожил → провал с пометкой",
                not healed2 and "НЕ ПОМОГ" in m2)

    def boom(_name):
        raise RuntimeError("no permission")

    healed3, m3 = hc.restart_service(
        "funding-watch", "watch.log", 900.0, run_cmd=boom,
        probe=lambda n: active, log_age_fn=lambda p: 3.0,
        sleep_fn=nope, tries=1, wait=0)
    ok &= check("autoheal: команда не удалась → без краха",
                not healed3 and "не удалась" in m3)

    healed4, m4 = hc.restart_service(
        "dashboard", "watch.log", 900.0, run_cmd=fake_run,
        probe=lambda n: {"ActiveState": "failed"},
        log_age_fn=lambda p: 3.0, sleep_fn=nope, tries=2, wait=0)
    ok &= check("autoheal: сервис не поднялся → провал",
                not healed4 and "НЕ ПОМОГ" in m4)

    # 13. send_telegram без ключей — False, без сети
    ok &= check("send_telegram: без токена → False",
                hc.send_telegram("", "", "x") is False)

    # 14. state: дефолт и round-trip
    tmp = os.path.join(tempfile.gettempdir(), "test_health_state.json")
    if os.path.exists(tmp):
        os.remove(tmp)
    st = hc.load_state(tmp)
    ok &= check("state: дефолт при отсутствии файла",
                st == {"services": {}, "last_alert": {}})
    st["services"]["funding-watch"] = {"nrestarts": 4}
    hc.save_state(tmp, st)
    ok &= check("state: сохранение/чтение",
                hc.load_state(tmp)["services"]["funding-watch"]["nrestarts"] == 4)

    print("\nИтог: " + ("все проверки пройдены" if ok else "ЕСТЬ ПРОВАЛЫ"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
