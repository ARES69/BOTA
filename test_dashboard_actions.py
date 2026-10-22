"""Тесты панели dashboard: кнопки управления, токен, анти-CSRF, рендер,
HTTP-контракт на живом сервере.

Панель читает position.json/memory.json/bot.log из ПАПКИ ПАНЕЛИ, поэтому тест
подменяет пути на временный каталог: проверки не зависят от реального
состояния бота и не ходят в API за живым PnL (LIVE=False).

Запуск: python test_dashboard_actions.py  (stdlib, без pytest).
"""
from __future__ import annotations

import http.server
import json
import os
import sys
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

# Консоль Windows по умолчанию cp1252 — печать кириллицы в check() роняла
# тест с UnicodeEncodeError при прямом запуске (через run_tests.py работало,
# потому что раннер выставляет PYTHONIOENCODING=utf-8).
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dashboard as d  # noqa: E402


def check(name, cond):
    print(("OK   " if cond else "FAIL ") + name)
    return cond


def main() -> int:
    ok = True

    # изоляция: свои данные и никаких запросов к бирже
    tmp = tempfile.mkdtemp()
    d.POSITION_FILE = os.path.join(tmp, "position.json")
    d.SNAPSHOT_FILE = os.path.join(tmp, "snapshot.json")
    d.MEMORY_FILE = os.path.join(tmp, "memory.json")
    d.EVENTS_FILE = os.path.join(tmp, "bot.log")
    d.CONFIG_FILE = os.path.join(tmp, "config.json")   # изоляция от config.json проекта
    json.dump({"mode": "testnet", "max-positions": 1},
              open(d.CONFIG_FILE, "w", encoding="utf-8"))
    d.LIVE = False
    json.dump({"position": None, "history": []},
              open(d.POSITION_FILE, "w", encoding="utf-8"))
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    open(d.EVENTS_FILE, "w", encoding="utf-8").write(f"[{ts}] HOLD BTCUSDT\n")

    # 1. action_argv: close берёт режим из позиции
    st_ts = {"position": {"symbol": "ETHUSDT", "mode": "testnet"}}
    argv = d.action_argv("close", st_ts)
    ok &= check("close: команда и --mode из позиции",
                argv[1].endswith("funding_executor.py")
                and argv[2] == "close" and argv[-2] == "--mode"
                and argv[-1] == "testnet")
    st_main = {"position": {"symbol": "BTCUSDT", "mode": "mainnet"}}
    ok &= check("close: mainnet-режим прокидывается",
                d.action_argv("close", st_main)[-1] == "mainnet")
    ok &= check("close: без позиции режим по умолчанию testnet",
                d.action_argv("close", {"position": None})[-1] == "testnet")

    # 2. action_argv: void --force (режим void берёт из state сам)
    av = d.action_argv("void", {"position": None})
    ok &= check("void: команда + --force",
                av[2] == "void" and av[-1] == "--force" and "--mode" not in av)

    # 3. action_argv: healthcheck — отдельный скрипт
    ah = d.action_argv("healthcheck", {"position": None})
    ok &= check("healthcheck: запускает healthcheck.py",
                ah[-1].endswith("healthcheck.py") and ah[-1] != ah[0])

    # 4. неизвестное действие → ValueError (вход с панели невозможен)
    try:
        d.action_argv("enter", {"position": None})
        bad = False
    except ValueError:
        bad = True
    ok &= check("неизвестное действие → ValueError (вход запрещён)", bad)

    # 4б. ручной вход: argv собирается только с валидным символом
    aen = d.action_argv("enter", {"position": None},
                        {"symbol": "ethusdt", "confirm": "ethusdt"})
    ok &= check("enter: символ приводится к BTCUSDT-виду и идёт в CLI",
                aen[2] == "enter" and aen[3] == "--symbol"
                and aen[4] == "ETHUSDT" and "--mode" in aen
                and "--force" not in aen)
    aenf = d.action_argv("enter", {"position": None},
                         {"symbol": "SOLUSDT", "force": "1"})
    ok &= check("enter: --force только по галочке",
                aenf[-1] == "--force" and aenf.count("--force") == 1)
    for bad in (None, "", "BT C", "<script>", "A" * 30):
        try:
            d.action_argv("enter", {"position": None}, {"symbol": bad})
            bad_ok = False
        except ValueError:
            bad_ok = True
        ok &= check(f"enter: мусорный символ ({bad!r}) → ValueError", bad_ok)

    # 4в. сервис бота: systemctl start/stop/restart нужного сервиса
    for act, verb in (("bot-start", "start"), ("bot-stop", "stop"),
                      ("bot-restart", "restart")):
        av2 = d.action_argv(act, {"position": None})
        ok &= check(f"{act}: {verb} сервиса {d.BOT_SERVICE}",
                    av2[0] == d.SYSTEMCTL and av2[1] == verb
                    and av2[2] == d.BOT_SERVICE)

    # 4г. validate_entry: три независимых подтверждения
    st_no = {"position": None}
    ok &= check("validate_entry: символ + повтор совпали → None",
                d.validate_entry(st_no, {"symbol": "ethusdt",
                                         "confirm": "ETHUSDT"}) is None)
    ok &= check("validate_entry: нет символа → ошибка",
                "символ" in (d.validate_entry(st_no, {}) or ""))
    ok &= check("validate_entry: подтверждение не совпало → ошибка",
                "подтвержд" in (d.validate_entry(
                    st_no, {"symbol": "BTCUSDT", "confirm": "ETHUSDT"}) or ""))
    ok &= check("validate_entry: позиция уже открыта → отказ",
                "уже открыта" in (d.validate_entry(
                    {"position": {"symbol": "ETHUSDT"}},
                    {"symbol": "BTCUSDT", "confirm": "BTCUSDT"}) or ""))
    ok &= check("validate_entry: mainnet без отметки → отказ",
                "mainnet" in (d.validate_entry(
                    st_no, {"symbol": "BTCUSDT", "confirm": "BTCUSDT"},
                    "mainnet") or ""))
    ok &= check("validate_entry: mainnet с отметкой → None",
                d.validate_entry(st_no, {"symbol": "BTCUSDT",
                                         "confirm": "BTCUSDT", "ack": "1"},
                                 "mainnet") is None)
    ok &= check("normalize_symbol/_truthy: регистр и галочки",
                d.normalize_symbol(" btcusdt ") == "BTCUSDT"
                and d.normalize_symbol("BTC-USDT") == ""
                and d._truthy("on") and d._truthy("1")
                and not d._truthy("0") and not d._truthy(""))

    # 5. _origin_ok: локальные проходят, внешние и мусор — нет
    ok &= check("origin: пусто → ok", d._origin_ok("") is True)
    ok &= check("origin: 127.0.0.1 → ok",
                d._origin_ok("http://127.0.0.1:8050") is True)
    ok &= check("origin: localhost → ok",
                d._origin_ok("http://localhost:8050") is True)
    ok &= check("origin: внешний → отказ",
                d._origin_ok("https://evil.example") is False)
    ok &= check("origin: кривой → отказ", d._origin_ok("http://[") is False)

    # 6. token_ok: верный / неверный / пустой ожидаемый
    d.TOKEN = "s3cret-token"
    ok &= check("токен: верный → ok", d.token_ok("s3cret-token") is True)
    ok &= check("токен: неверный → отказ", d.token_ok("nope") is False)
    ok &= check("токен: пустой предъявленный → отказ", d.token_ok("") is False)
    d.TOKEN = ""
    ok &= check("токен: не задан → действия запрещены",
                d.token_ok("s3cret-token") is False)
    # регрессия (поймано живьём на VPS): hmac.compare_digest со строками падает
    # на не-ASCII, обработчик умирал без ответа вместо 403
    d.TOKEN = "s3cret-token"
    ok &= check("токен: не-ASCII предъявленный → отказ без исключения",
                d.token_ok("неверный") is False)
    ok &= check("токен: не-ASCII эталон не роняет сверку",
                d.token_ok("s3cret-token", "т0кен") is False)
    d.TOKEN = ""

    # 7. run_action: успех и ошибка (подменяем subprocess.run)
    d.TOKEN = "s3cret-token"
    captured: dict = {}
    real_run = d.subprocess.run

    def fake_ok(argv, **kw):
        captured["argv"] = argv
        return type("R", (), {"returncode": 0, "stdout": "готово\n",
                              "stderr": ""})()

    def fake_err(argv, **kw):
        return type("R", (), {"returncode": 2, "stdout": "",
                              "stderr": "boom\n"})()

    try:
        d.subprocess.run = fake_ok
        res = d.run_action("healthcheck")
        ok &= check("run_action: успех → ok, rc 0, вывод",
                    res["ok"] and res["rc"] == 0
                    and res["output"] == "готово")
        ok &= check("run_action: пробрасывает argv команды",
                    captured.get("argv", [None])[-1].endswith("healthcheck.py"))
        d.subprocess.run = fake_err
        res = d.run_action("healthcheck")
        ok &= check("run_action: код≠0 → ok False, вывод с ошибкой",
                    res["ok"] is False and res["rc"] == 2
                    and "boom" in res["output"])
    finally:
        d.subprocess.run = real_run

    # 8. actions_block: без токена кнопок нет, с токеном — есть
    ok &= check("кнопки: без токена блока нет",
                d.actions_block({"symbol": "ETHUSDT"}, "") == "")
    block_pos = d.actions_block({"symbol": "ETHUSDT"}, "T0K")
    ok &= check("кнопки: три действия и скрытый токен",
                all(f"value='{n}'" in block_pos for n in ("close", "void",
                                                          "healthcheck"))
                and "value='T0K'" in block_pos)
    ok &= check("кнопки: с позицией close/void активны",
                "disabled" not in block_pos)
    block_no = d.actions_block(None, "T0K")
    ok &= check("кнопки: без позиции close/void выключены",
                block_no.count("disabled") == 2)

    # 8б. bot_block/entry_block: статус сервиса и защита входа
    ok &= check("bot_block: без токена — пусто", d.bot_block("active", "") == "")
    ok &= check("bot_block: bot=None (статичная панель) — пусто",
                d.bot_block(None, "T0K") == "")
    bb = d.bot_block("active", "T0K")
    ok &= check("bot_block: работает → stop активна, start выключена",
                bb.count("disabled") == 1 and "bot-stop" in bb
                and "🟢 работает" in bb)
    bb2 = d.bot_block("inactive", "T0K")
    ok &= check("bot_block: остановлен → start активна, stop выключена",
                bb2.count("disabled") == 1 and "⚪ остановлен" in bb2)
    bb3 = d.bot_block("н/д", "T0K")
    ok &= check("bot_block: нет systemd → «н/д», сервисные кнопки выключены",
                "❔" in bb3 and bb3.count("disabled") == 3
                # стоп-всё — аварийная: включена всегда, даже без systemd
                and "value='stop-all'" in bb3 and bb3.count("disabled") == 3)
    ok &= check("bot_block: упал (failed) → stop выключена, start активна",
                d.bot_block("failed", "T0K").count("disabled") == 1)
    # стоп-всё: кнопка с предупреждением и числом закрываемых пар
    bb_sa = d.bot_block("active", "T0K", n_open=2)
    ok &= check("стоп-всё: кнопка в блоке сервиса с предупреждением",
                "value='stop-all'" in bb_sa and "СТОП-ВСЁ" in bb_sa
                and "2 пар(ы) будут закрыты" in bb_sa)
    bb_sa0 = d.bot_block("active", "T0K", n_open=0)
    ok &= check("стоп-всё: без пар — сказано, что остановит только бота",
                "открытых пар нет" in bb_sa0)
    ok &= check("стоп-всё: подтверждение называет последствия",
                "ВСЕ открытые пары" in (d.action_confirm("stop-all") or ""))
    ok &= check("стоп-всё: действие опасное → требует пароля панели",
                "stop-all" in d.DANGEROUS and "stop-all" in d.ACTIONS
                and d.ACTION_LABEL["stop-all"].startswith("СТОП-ВСЁ"))
    ok &= check("стоп-всё: argv закрытия — --all без символа",
                "--all" in d.action_argv("close", st_ts, {"all": "1"})
                and "--symbol" not in d.action_argv("close", st_ts,
                                                    {"all": "1"}))

    # пуск-всё: кнопка, подтверждение, опасность
    bb_st = d.bot_block("inactive", "T0K", "testnet", 0)
    ok &= check("пуск-всё: кнопка в блоке сервиса рядом со стоп-всё",
                "value='start-all'" in bb_st and "ПУСК-ВСЁ" in bb_st
                and "value='stop-all'" in bb_st)
    ok &= check("пуск-всё: подтверждение называет последствия",
                "открывать позиции" in (d.action_confirm("start-all") or ""))
    ok &= check("пуск-всё: действие опасное → требует пароля панели",
                "start-all" in d.DANGEROUS and "start-all" in d.ACTIONS
                and d.ACTION_LABEL["start-all"].startswith("ПУСК-ВСЁ"))

    # Telegram в тестах не должен никуда слать: ключи убираем из окружения,
    # а .env не читаем (TG_ENV_LOADED) — иначе прогон на машине с боевыми
    # ключами заспамил бы канал.
    for _k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "TELEGRAM_API_BASE"):
        os.environ.pop(_k, None)
    d.TG_ENV_LOADED["done"] = True

    # --- run_stop_all: составное действие с отчётом по шагам --------------
    sent_msgs: list[str] = []

    def fake_notify(text: str) -> bool:
        sent_msgs.append(text)
        return True

    def stop_all(**kw):
        """run_stop_all с перехватом канала: тесты не шлют в Telegram."""
        kw.setdefault("notify_step", fake_notify)
        return d.run_stop_all(**kw)

    def mk_stop_env(stop_rc=0, stop_state="inactive", close_rc=0,
                    left_after=0, states=None, open_n=2, opens_on_stop=0):
        """Подмены для run_stop_all: команды, состояние, is-active.

        open_n — сколько пар «открыто» в начале; left_after — сколько
        осталось после УСПЕШНОГО close (сверка шага 3 читает файл повторно);
        opens_on_stop — сколько пар «открыл бот» в момент остановки сервиса
        (гонка: пара могла появиться между чтением состояния и stop).
        """
        calls: list[list] = []
        st = {"n": open_n}

        def fake_cmd(argv, timeout):
            calls.append(list(argv))
            if argv[:2] == [d.SYSTEMCTL, "stop"]:
                if opens_on_stop:
                    st["n"] += opens_on_stop
                return stop_rc, "остановлено"
            if argv[2:3] == ["close"]:
                if close_rc == 0:      # закрытие реально убирает пары
                    st["n"] = left_after
                return close_rc, "закрыто"
            return 0, ""

        def fake_state():
            return {"position": None,
                    "positions": [{"symbol": "ETHUSDT"}] * st["n"],
                    "history": []}

        seq = iter(states or [])

        def fake_active():
            return next(seq, stop_state)

        return calls, fake_cmd, fake_state, fake_active

    calls, fcmd, fstate, fac = mk_stop_env()
    res = stop_all(run_cmd=fcmd, state_reader=fstate, active_probe=fac)
    ok &= check("стоп-всё: успех → ok и три шага в отчёте",
                res["ok"] and "Шаг 1/3" in res["output"]
                and "Шаг 2/3" in res["output"]
                and "Шаг 3/3" in res["output"]
                and "ИТОГ: бот остановлен, все пары закрыты" in res["output"])
    ok &= check("стоп-всё: порядок — сначала stop сервиса, потом close --all",
                calls[0][:2] == [d.SYSTEMCTL, "stop"]
                and calls[1][2] == "close" and "--all" in calls[1])
    ok &= check("стоп-всё: отчёт называет шаги и коды",
                "systemctl stop" in res["output"]
                and "close --all: код 0" in res["output"])

    # сервис не остановился — закрываем всё равно, но предупреждаем
    calls2, fcmd2, fstate2, fac2 = mk_stop_env(stop_rc=1, stop_state="active")
    res2 = stop_all(run_cmd=fcmd2, state_reader=fstate2,
                          active_probe=fac2)
    ok &= check("стоп-всё: сервис не остался → не ok, но close всё равно шёл",
                not res2["ok"] and any(a[2] == "close" for a in calls2)
                and "БОТ НЕ ОСТАНОВЛЕН" in res2["output"])
    ok &= check("стоп-всё: подсказка как вернуть бота",
                "systemctl start" in res2["output"])

    # закрытие упало → не ok, код 1
    calls3, fcmd3, fstate3, fac3 = mk_stop_env(close_rc=2)
    res3 = stop_all(run_cmd=fcmd3, state_reader=fstate3,
                          active_probe=fac3)
    ok &= check("стоп-всё: close упал → не ok и код 1",
                not res3["ok"] and res3["rc"] == 1
                and "close --all: код 2" in res3["output"])

    # пары остались в position.json → сверка поймала
    calls4, fcmd4, fstate4, fac4 = mk_stop_env(left_after=1)
    res4 = stop_all(run_cmd=fcmd4, state_reader=fstate4,
                          active_probe=fac4)
    ok &= check("стоп-всё: сверка видит оставшиеся пары → не ok",
                not res4["ok"] and "ОСТАЛОСЬ пар" in res4["output"])

    # без открытых пар — только остановка, без close
    calls5, fcmd5, fstate5, fac5 = mk_stop_env(open_n=0)
    res5 = stop_all(run_cmd=fcmd5,
                          state_reader=lambda: {"position": None,
                                                "positions": [],
                                                "history": []},
                          active_probe=fac5)
    ok &= check("стоп-всё: пар нет → close не вызывался, шаг пропущен",
                all(a[2] != "close" for a in calls5)
                and "открытых пар нет" in res5["output"] and res5["ok"])

    # сервис перезапустился после закрытия → не ok
    calls6, fcmd6, fstate6, fac6 = mk_stop_env(
        states=["inactive", "active"])
    res6 = stop_all(run_cmd=fcmd6, state_reader=fstate6,
                          active_probe=fac6)
    ok &= check("стоп-всё: сервис снова active → не ok с пояснением",
                not res6["ok"] and "снова active" in res6["output"])

    # BOT_SERVICE не задан → шаг 1 пропущен без падения
    saved_svc = d.BOT_SERVICE
    d.BOT_SERVICE = ""
    try:
        calls7, fcmd7, _fs, fac7 = mk_stop_env(open_n=0)
        res7 = stop_all(run_cmd=fcmd7,
                              state_reader=lambda: {"position": None,
                                                    "positions": [],
                                                    "history": []},
                              active_probe=fac7)
    finally:
        d.BOT_SERVICE = saved_svc
    ok &= check("стоп-всё: сервис не задан → шаг пропущен, без исключения",
                "DASHBOARD_BOT_SERVICE" in res7["output"]
                and all(a[:2] != [saved_svc, "stop"] for a in calls7))

    # гонка: бот успел открыть пару В МОМЕНТ остановки — её тоже надо закрыть
    calls8, fcmd8, fstate8, fac8 = mk_stop_env(open_n=0, opens_on_stop=1)
    res8 = stop_all(run_cmd=fcmd8, state_reader=fstate8,
                           active_probe=fac8)
    ok &= check("стоп-всё: пара, открытая в момент остановки, тоже закрыта",
                any(a[2] == "close" for a in calls8)
                and "открытых пар не осталось" in res8["output"]
                and res8["ok"])
    ok &= check("стоп-всё: отчёт показывает изменившийся портфель",
                "портфель изменился" in res8["output"])

    # --- Telegram: отчёт по шагам, а не только браузер --------------------
    tail3 = sent_msgs[-3:]
    ok &= check("telegram: три сообщения — шаг 1, шаг 2 и итог",
                len(tail3) == 3
                and "Шаг 1/3" in tail3[0] and "Шаг 2/3" in tail3[1]
                and "Шаг 3/3" in tail3[2] and "ИТОГ:" in tail3[2])
    ok &= check("telegram: каждое сообщение помечено «СТОП-ВСЁ»",
                all(m.startswith("🚨 СТОП-ВСЁ") for m in tail3))
    ok &= check("telegram: причина неотправки видна в отчёте",
                "Telegram: не настроен" in res8["output"])

    # с настроенным каналом отчёт подтверждает доставку
    os.environ["TELEGRAM_BOT_TOKEN"] = "T"
    os.environ["TELEGRAM_CHAT_ID"] = "C"
    try:
        calls9, fcmd9, fstate9, fac9 = mk_stop_env()
        res9 = stop_all(run_cmd=fcmd9, state_reader=fstate9,
                        active_probe=fac9)
    finally:
        os.environ.pop("TELEGRAM_BOT_TOKEN", None)
        os.environ.pop("TELEGRAM_CHAT_ID", None)
    ok &= check("telegram: с настроенным каналом — «отправлено 3 из 3»",
                "отправлено 3 из 3 шагов" in res9["output"])

    # --- run_start_all: запуск бота + сверка автовхода ---------------------
    started_msgs: list[str] = []

    def start_all(**kw):
        kw.setdefault("notify_step",
                      lambda t: started_msgs.append(t) or True)
        kw.setdefault("sleep_fn", lambda s: None)   # тест не спит по 1.5 с
        return d.run_start_all(**kw)

    EXEC_WS = ("ExecStart={ path=/usr/bin/python3 ; "
               "argv[]=/usr/bin/python3 /home/u/bybit-funding/"
               "funding_executor.py watch --mode testnet --ws ; ...)")

    def mk_start_env(open_n=0, seq=None, execstart=EXEC_WS, start_rc=0):
        """Подмены для run_start_all: команды, состояние, is-active, юнит.
        seq — очередь ответов is-active (по умолчанию inactive → active)."""
        calls: list[list] = []
        states = list(seq if seq is not None else ["inactive", "active"])

        def fake_cmd(argv, timeout):
            calls.append(list(argv))
            return (start_rc, "запущено") if argv[1] == "start" else (0, "")

        def fake_active():
            return states.pop(0) if states else "inactive"

        def fake_state():
            return {"position": None,
                    "positions": [{"symbol": "ETHUSDT"}] * open_n,
                    "history": []}

        return calls, fake_cmd, fake_state, fake_active, (lambda: execstart)

    cs, fcmd_s, fstate_s, fac_s, funit = mk_start_env()
    res_s = start_all(run_cmd=fcmd_s, state_reader=fstate_s,
                      active_probe=fac_s, unit_probe=funit)
    ok &= check("пуск-всё: успех → ok, три шага и автовход включён",
                res_s["ok"] and "Шаг 1/3" in res_s["output"]
                and "Шаг 2/3" in res_s["output"]
                and "Шаг 3/3" in res_s["output"]
                and "автовход включён" in res_s["output"]
                and "ИТОГ: бот запущен, автовход включён" in res_s["output"])
    ok &= check("пуск-всё: start вызван и сервис подтверждён active",
                any(c[1] == "start" for c in cs)
                and "сервис active" in res_s["output"])

    # портфель не пуст → отказ, start НЕ вызывается
    n_before = len(started_msgs)
    c2, f2, st2, a2, u2 = mk_start_env(open_n=2)
    r2 = start_all(run_cmd=f2, state_reader=st2, active_probe=a2, unit_probe=u2)
    refusal = started_msgs[n_before:]
    ok &= check("пуск-всё: портфель не пуст → не запускаем",
                not r2["ok"] and r2["rc"] == 1
                and all(c[1] != "start" for c in c2)
                and "НЕ запущен" in r2["output"]
                and "СТОП-ВСЁ" in r2["output"])
    ok &= check("пуск-всё: отказ тоже уходит в канал (шаг + итог)",
                len(refusal) == 2 and "НЕ запущен" in refusal[-1])

    # сервис не поднялся (start вернул 0, но процесс упал)
    c3, f3, st3, a3, u3 = mk_start_env(seq=["inactive", "failed", "failed"])
    r3 = start_all(run_cmd=f3, state_reader=st3, active_probe=a3, unit_probe=u3)
    ok &= check("пуск-всё: сервис не поднялся → не ok и подсказка",
                not r3["ok"] and "НЕ ПОДНЯЛСЯ" in r3["output"]
                and "journalctl" in r3["output"])

    # --no-auto в юните
    c4, f4, st4, a4, u4 = mk_start_env(
        execstart="ExecStart=... funding_executor.py watch --no-auto")
    r4 = start_all(run_cmd=f4, state_reader=st4, active_probe=a4, unit_probe=u4)
    ok &= check("пуск-всё: --no-auto в юните → автовход выключен, не ok",
                not r4["ok"] and "автовход НЕ включён" in r4["output"]
                and "--no-auto" in r4["output"])

    # mainnet без --confirm-live → watch только наблюдает
    c5, f5, st5, a5, u5 = mk_start_env(
        execstart="ExecStart=... watch --mode mainnet --ws")
    r5 = start_all(run_cmd=f5, state_reader=st5, active_probe=a5, unit_probe=u5)
    ok &= check("пуск-всё: mainnet без --confirm-live → не ok с пояснением",
                not r5["ok"] and "--confirm-live" in r5["output"])

    # ExecStart не прочитан → не подтверждаем
    c6, f6, st6, a6, u6 = mk_start_env(execstart=None)
    r6 = start_all(run_cmd=f6, state_reader=st6, active_probe=a6, unit_probe=u6)
    ok &= check("пуск-всё: ExecStart не прочитан → «панель не знает»",
                not r6["ok"] and "не знает" in r6["output"])

    # сервис уже работал → start не нужен, но сверка идёт
    c7, f7, st7, a7, u7 = mk_start_env(seq=["active", "active"])
    r7 = start_all(run_cmd=f7, state_reader=st7, active_probe=a7, unit_probe=u7)
    ok &= check("пуск-всё: сервис уже active → start не вызывался",
                r7["ok"] and all(c[1] != "start" for c in c7)
                and "start не требуется" in r7["output"])

    # no-auto в config.json
    saved_cfg = d.CONFIG_FILE
    cfg_path = os.path.join(tmp, "cfg_noauto.json")
    json.dump({"mode": "testnet", "no-auto": True},
              open(cfg_path, "w", encoding="utf-8"))
    d.CONFIG_FILE = cfg_path
    try:
        c8, f8, st8, a8, u8 = mk_start_env()
        r8 = start_all(run_cmd=f8, state_reader=st8, active_probe=a8,
                       unit_probe=u8)
    finally:
        d.CONFIG_FILE = saved_cfg
    ok &= check("пуск-всё: no-auto в config.json → автовход выключен",
                not r8["ok"] and "no-auto" in r8["output"])

    last3 = started_msgs[-3:]
    ok &= check("пуск-всё: отчёт уходит в канал тремя шагами",
                len(last3) == 3 and "шаг 1/3" in last3[0]
                and "шаг 2/3" in last3[1] and "ИТОГ" in last3[2])
    ok &= check("пуск-всё: сообщения помечены «ПУСК-ВСЁ»",
                all(m.startswith("▶️ ПУСК-ВСЁ") for m in last3))

    # tg_send: настоящий HTTP-путь через фейковый Telegram на localhost
    got: list = []

    class _FakeTG(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            form = dict(urllib.parse.parse_qsl(self.rfile.read(n).decode()))
            got.append((self.path, form))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(
                {"ok": form.get("chat_id") != "fail"}).encode())

        def log_message(self, *a):
            pass

    tgs = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _FakeTG)
    threading.Thread(target=tgs.serve_forever, daemon=True).start()
    tg_port = tgs.server_address[1]
    saved_env = {k: os.environ.get(k) for k in
                 ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "TELEGRAM_API_BASE")}
    os.environ.update(TELEGRAM_BOT_TOKEN="123:ABC", TELEGRAM_CHAT_ID="777",
                      TELEGRAM_API_BASE="http://127.0.0.1:%d" % tg_port)
    try:
        ok &= check("telegram: tg_send кладёт текст в sendMessage",
                    d.tg_send("тест") and got[-1][0] == "/bot123:ABC/sendMessage"
                    and got[-1][1].get("chat_id") == "777"
                    and got[-1][1].get("text") == "тест")
        ok &= check("telegram: длинное сообщение обрезается под лимит",
                    d.tg_send("я" * 5000)
                    and len(got[-1][1].get("text", "")) <= d.TG_MAX_LEN)
        os.environ["TELEGRAM_CHAT_ID"] = "fail"
        ok &= check("telegram: ответ ok:false → False (панель не падает)",
                    d.tg_send("x") is False)
        os.environ["TELEGRAM_CHAT_ID"] = "777"
        os.environ["TELEGRAM_API_BASE"] = "http://127.0.0.1:1"
        ok &= check("telegram: сеть недоступна → False без исключения",
                    d.tg_send("x") is False)
        os.environ.pop("TELEGRAM_BOT_TOKEN", None)
        ok &= check("telegram: нет токена → False", d.tg_send("x") is False)
    finally:
        for _k, _v in saved_env.items():
            if _v is None:
                os.environ.pop(_k, None)
            else:
                os.environ[_k] = _v
        tgs.shutdown()
        tgs.server_close()

    ok &= check("entry_block: без токена — пусто", d.entry_block(None, "") == "")
    eb_nopw = d.entry_block(None, "T0K", has_password=False)
    ok &= check("entry_block: без пароля — подсказка и никакой формы",
                "DASHBOARD_PASSWORD" in eb_nopw
                and "action='/action'" not in eb_nopw)
    eb = d.entry_block(None, "T0K")
    ok &= check("entry_block: двойное подтверждение символа и фильтр",
                "name='symbol'" in eb and "name='confirm'" in eb
                and "name='force'" in eb and "name='ack'" not in eb)
    ok &= check("entry_block: открытая позиция → ввод и кнопка выключены",
                d.entry_block({"symbol": "ETHUSDT"}, "T0K").count("disabled") >= 4)
    # портфель (max-positions > 1): свободный слот — ввод доступен, а ввод по
    # уже открытой паре отклоняется сервером в validate_entry
    json.dump({"mode": "testnet", "max-positions": 3},
              open(d.CONFIG_FILE, "w", encoding="utf-8"))
    ok &= check("entry_block: портфель из одного занята — ввод доступен",
                "disabled" not in d.entry_block({"symbol": "ETHUSDT"}, "T0K"))
    ok &= check("entry_block: заполненный портфель → ввод выключен",
                d.entry_block([{"symbol": "ETHUSDT"}, {"symbol": "BTCUSDT"},
                               {"symbol": "SOLUSDT"}], "T0K")
                .count("disabled") >= 4)
    ok &= check("validate_entry: портфель из 3 — вход в четвёртую пару с окном",
                d.validate_entry({"positions": [{"symbol": "ETHUSDT"},
                                                {"symbol": "BTCUSDT"}]},
                                 {"symbol": "SOLUSDT", "confirm": "SOLUSDT"})
                is None)
    ok &= check("validate_entry: та же пара второй раз → отказ",
                "уже открыт" in (d.validate_entry(
                    {"positions": [{"symbol": "ETHUSDT"}]},
                    {"symbol": "ETHUSDT", "confirm": "ETHUSDT"}) or ""))
    json.dump({"mode": "testnet", "max-positions": 1},
              open(d.CONFIG_FILE, "w", encoding="utf-8"))
    eb_main = d.entry_block(None, "T0K", "mainnet")
    ok &= check("entry_block: mainnet → галочка о реальных деньгах",
                "name='ack'" in eb_main and "РЕАЛЬНЫЕ деньги" in eb_main)

    # форма входа рисуется только при заданном пароле — включаем его на время
    orig_pw = d.PASSWORD
    d.PASSWORD = "pw-for-render"
    try:
        page_bot = d.render(60, "T0K", bot="active")
    finally:
        d.PASSWORD = orig_pw
    ok &= check("render: серверная панель с паролем — сервис и форма входа",
                "bot-start" in page_bot and "name='symbol'" in page_bot)
    ok &= check("render: без пароля формы входа нет (только подсказка)",
                "name='symbol'" not in d.render(60, "T0K", bot="active"))
    ok &= check("render: статичная панель (bot=None) без сервисных кнопок",
                "bot-start" not in d.render(60, "T0K"))

    # 9. render: токен влияет на наличие кнопок
    page_tok = d.render(60, "T0K")
    page_non = d.render(60, "")
    ok &= check("render: с токеном есть формы действий",
                "action='/action'" in page_tok and "value='close'" in page_tok)
    ok &= check("render: без токена форм действий нет",
                "action='/action'" not in page_non)
    ok &= check("render: страница собирается без мусора",
                "</html>" in page_non and "None" not in page_non)

    # 10. result_page: статус и вывод экранируются
    rp = d.result_page("close", {"ok": True, "rc": 0,
                                 "output": "<b>закрыто</b> & всё"})
    ok &= check("result_page: успех и экранирование вывода",
                "✅" in rp and "&lt;b&gt;" in rp and "&amp;" in rp
                and "код возврата 0" in rp)
    rp2 = d.result_page("void", {"ok": False, "rc": -1, "output": "таймаут"})
    ok &= check("result_page: ошибка помечена", "⚠️" in rp2 and "таймаут" in rp2)

    # 11. html_escape экранирует кавычки (токен в атрибуте value='...')
    ok &= check("html_escape: кавычки",
                d.html_escape("a\"b'c<d&e")
                == "a&quot;b&#39;c&lt;d&amp;e")

    # 12. HTTP-контракт: живой сервер на свободном порту
    def start(token):
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0),
                                              d.make_handler(0, token))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        return srv, srv.server_address[1]

    def hget(port, path="/"):
        try:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}{path}", timeout=10) as r:
                return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")

    def hpost(port, data, origin=None, path="/action"):
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}{path}",
            data=urllib.parse.urlencode(data).encode(), method="POST")
        if origin:
            req.add_header("Origin", origin)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")

    srv, port = start("HTTPTOK")
    try:
        code, body = hget(port)
        ok &= check("http: GET / → 200 с формами действий",
                    code == 200 and "action='/action'" in body
                    and "value='HTTPTOK'" in body)
        ok &= check("http: GET /index.html → 200", hget(port, "/index.html")[0]
                    == 200)
        ok &= check("http: GET /nope → 404", hget(port, "/nope")[0] == 404)
        ok &= check("http: POST без токена → 403",
                    hpost(port, {"name": "healthcheck"})[0] == 403)
        ok &= check("http: POST неверный токен → 403",
                    hpost(port, {"token": "bad", "name": "healthcheck"})[0] == 403)
        # регрессия с живого VPS: не-ASCII токен обрывал соединение без ответа
        ok &= check("http: POST не-ASCII токен → 403, а не обрыв",
                    hpost(port, {"token": "неверный", "name": "healthcheck"})[0]
                    == 403)
        ok &= check("http: POST чужой Origin → 403",
                    hpost(port, {"token": "HTTPTOK", "name": "healthcheck"},
                          origin="https://evil.example")[0] == 403)
        # опасные действия (вход/сервис) без пароля панели — запрещены
        ok &= check("http: POST enter без DASHBOARD_PASSWORD → 403",
                    hpost(port, {"token": "HTTPTOK", "name": "enter",
                                 "symbol": "BTCUSDT",
                                 "confirm": "BTCUSDT"})[0] == 403)
        ok &= check("http: POST bot-start без DASHBOARD_PASSWORD → 403",
                    hpost(port, {"token": "HTTPTOK", "name": "bot-start"})[0]
                    == 403)
        ok &= check("http: POST неизвестное действие → 400",
                    hpost(port, {"token": "HTTPTOK", "name": "bogus"})[0] == 400)
        ok &= check("http: POST чужой путь → 404",
                    hpost(port, {"token": "HTTPTOK", "name": "close"},
                          path="/other")[0] == 404)
        ok &= check("http: POST с локальным Origin проходит проверку Origin",
                    hpost(port, {"token": "HTTPTOK", "name": "bogus"},
                          origin=f"http://127.0.0.1:{port}")[0] == 400)
        # отказы приходят как код, а не как обрыв: тело запроса съедается
        # ДО ответа (регрессия: 403 без чтения тела давал WinError 10053/RST)
        ok &= check("http: POST без тела и токена → 403 (ответ доехал)",
                    hpost(port, {})[0] == 403)
        ok &= check("http: POST с токеном без действия → 400",
                    hpost(port, {"token": "HTTPTOK"})[0] == 400)
        big = {"token": "HTTPTOK", "name": "close", "pad": "x" * 5000}
        ok &= check("http: POST с телом >4096 байт → 413",
                    hpost(port, big)[0] == 413)
        ok &= check("http: POST на чужой путь с телом → 404",
                    hpost(port, {"token": "HTTPTOK", "name": "close"},
                          path="/other2")[0] == 404)

        # лок занят — второе действие не стартует и ничего не выполняет
        d._ACTION_LOCK.acquire()
        try:
            ok &= check("http: параллельное действие → 409",
                        hpost(port, {"token": "HTTPTOK", "name": "close"})[0]
                        == 409)
        finally:
            d._ACTION_LOCK.release()

        # успешный путь: подменяем run_action, чтобы НЕ трогать биржу
        seen: dict = {}
        real_action = d.run_action

        def fake_run(name, params=None, timeout=None):
            seen["name"] = name
            return {"ok": True, "rc": 0, "output": "закрыто"}

        d.run_action = fake_run
        try:
            code, body = hpost(port, {"token": "HTTPTOK", "name": "close"},
                               origin=f"http://127.0.0.1:{port}")
            ok &= check("http: верный токен + close → 200 с результатом",
                        code == 200 and seen.get("name") == "close"
                        and "закрыто" in body and "код возврата 0" in body)
        finally:
            d.run_action = real_action
    finally:
        srv.shutdown()
        srv.server_close()

    # 13. сервер без токена: панель рисуется, но действий нет
    srv2, port2 = start("")
    try:
        code, body = hget(port2)
        ok &= check("http: сервер без токена → нет форм действий",
                    code == 200 and "action='/action'" not in body)
        ok &= check("http: сервер без токена → POST запрещён",
                    hpost(port2, {"name": "close"})[0] == 403)
    finally:
        srv2.shutdown()
        srv2.server_close()

    # 14. вход по паролю (панель по IP:порту)
    ok &= check("пароль: верный → ok", d.password_ok("pw", "pw") is True)
    ok &= check("пароль: пустой эталон → запрет",
                d.password_ok("", "") is False)
    ok &= check("пароль: не-ASCII не роняет сверку",
                d.password_ok("неверный", "pw") is False)

    tok = d.new_session()
    ok &= check("сессия: новая → действительна",
                d.session_ok(f"{d.SESSION_COOKIE}={tok}") is True)
    ok &= check("сессия: чужая cookie → отказ",
                d.session_ok(f"{d.SESSION_COOKIE}=nope") is False)
    d.drop_session(f"{d.SESSION_COOKIE}={tok}")
    ok &= check("сессия: после выхода → отказ",
                d.session_ok(f"{d.SESSION_COOKIE}={tok}") is False)

    tok_exp = d.new_session(now=1000.0)
    ok &= check("сессия: до истечения → действительна",
                d.session_ok(f"{d.SESSION_COOKIE}={tok_exp}",
                             now=1000.0 + d.SESSION_TTL - 1) is True)
    ok &= check("сессия: истекла → отказ",
                d.session_ok(f"{d.SESSION_COOKIE}={tok_exp}",
                             now=1000.0 + d.SESSION_TTL) is False)

    d.LOGIN_FAILS.clear()
    for _ in range(d.LOGIN_MAX_FAILS):
        d.note_login_failure("10.0.0.9")
    ok &= check("анти-брутфорс: 5 ошибок → блокировка",
                d.login_blocked("10.0.0.9") > 0)
    d.note_login_success("10.0.0.9")
    ok &= check("анти-брутфорс: успешный вход снимает блокировку",
                d.login_blocked("10.0.0.9") == 0)

    ok &= check("origin: тот же хост по IP → ok",
                d._origin_ok("http://45.9.18.57:8050",
                             "45.9.18.57:8050") is True)
    ok &= check("origin: другой IP → отказ",
                d._origin_ok("http://evil.example", "45.9.18.57:8050")
                is False)

    # публичный bind без пароля не должен даже стартовать
    import subprocess
    env = {k: v for k, v in os.environ.items()
           if k != "DASHBOARD_PASSWORD"}
    res = subprocess.run(
        [sys.executable, os.path.join(os.path.dirname(d.__file__),
                                      "dashboard.py"),
         "--serve", "8051", "--host", "0.0.0.0",
         "--out", os.path.join(tmp, "pub.html")],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=env, timeout=30)
    ok &= check("public bind без пароля → отказ на старте",
                res.returncode != 0 and "DASHBOARD_PASSWORD" in
                (res.stdout + res.stderr))

    # 15. HTTP-вход: сессия, cookie, кнопки, выход
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None

    opener = urllib.request.build_opener(NoRedirect)

    def call(port, path, data=None, cookie=None, origin=None):
        url = f"http://127.0.0.1:{port}{path}"
        body = urllib.parse.urlencode(data).encode() if data is not None else None
        req = urllib.request.Request(url, data=body,
                                     method="POST" if body is not None else "GET")
        if cookie:
            req.add_header("Cookie", cookie)
        if origin:
            req.add_header("Origin", origin)
        try:
            with opener.open(req, timeout=10) as r:
                return (r.status, r.read().decode("utf-8", "replace"),
                        r.headers)
        except urllib.error.HTTPError as e:
            return (e.code, e.read().decode("utf-8", "replace"), e.headers)

    srv3 = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0), d.make_handler(0, "HTTPTOK", "pw"))
    threading.Thread(target=srv3.serve_forever, daemon=True).start()
    port3 = srv3.server_address[1]
    try:
        code, _, hdrs = call(port3, "/")
        ok &= check("http+пароль: GET / без сессии → редирект на /login",
                    code == 303 and hdrs.get("Location") == "/login")
        code, body, _ = call(port3, "/login")
        ok &= check("http+пароль: форма входа отдаётся",
                    code == 200 and "name=\"password\"" in body)
        code, _, _ = call(port3, "/login", {"password": "bad"})
        ok &= check("http+пароль: неверный пароль → 401", code == 401)

        code, _, hdrs = call(port3, "/login", {"password": "pw"})
        set_cookie = hdrs.get("Set-Cookie") or ""
        cookie = set_cookie.split(";", 1)[0]
        ok &= check("http+пароль: верный пароль → 303 + HttpOnly cookie",
                    code == 303 and cookie.startswith(d.SESSION_COOKIE + "=")
                    and "HttpOnly" in set_cookie)

        code, body, _ = call(port3, "/", cookie=cookie)
        ok &= check("http+пароль: с сессией — панель и кнопки",
                    code == 200 and "action='/action'" in body
                    and "value='HTTPTOK'" in body)

        ok &= check("http+пароль: POST без сессии → 403",
                    call(port3, "/action",
                         {"token": "HTTPTOK", "name": "healthcheck"})[0]
                    == 403)
        ok &= check("http+пароль: POST с сессией и своим Origin → 200",
                    call(port3, "/action",
                         {"token": "HTTPTOK", "name": "bogus"},
                         cookie=cookie,
                         origin=f"http://127.0.0.1:{port3}")[0] == 400)

        # ручной вход и сервис: валидация и запуск на живой панели с паролем
        seen2: dict = {}
        real_run2 = d.run_action

        def fake_run2(name, params=None, timeout=None):
            seen2["name"], seen2["params"] = name, dict(params or {})
            return {"ok": True, "rc": 0, "output": "ок"}

        d.run_action = fake_run2
        try:
            code, body, _ = call(port3, "/action",
                                 {"token": "HTTPTOK", "name": "enter"},
                                 cookie=cookie,
                                 origin=f"http://127.0.0.1:{port3}")
            ok &= check("http+пароль: enter без символа → 400",
                        code == 400 and "символ" in body
                        and "name" not in seen2)
            code, body, _ = call(port3, "/action",
                                 {"token": "HTTPTOK", "name": "enter",
                                  "symbol": "BTCUSDT", "confirm": "ETHUSDT"},
                                 cookie=cookie,
                                 origin=f"http://127.0.0.1:{port3}")
            ok &= check("http+пароль: enter с несовпавшим подтверждением → 400",
                        code == 400 and "подтвержд" in body)
            code, body, _ = call(port3, "/action",
                                 {"token": "HTTPTOK", "name": "enter",
                                  "symbol": "btcusdt", "confirm": "BTCUSDT"},
                                 cookie=cookie,
                                 origin=f"http://127.0.0.1:{port3}")
            ok &= check("http+пароль: enter с подтверждением → 200 и символ",
                        code == 200 and seen2.get("name") == "enter"
                        and seen2.get("params", {}).get("symbol") == "btcusdt")
            code, _, _ = call(port3, "/action",
                              {"token": "HTTPTOK", "name": "bot-start"},
                              cookie=cookie, origin=f"http://127.0.0.1:{port3}")
            ok &= check("http+пароль: bot-start → 200",
                        code == 200 and seen2.get("name") == "bot-start")
            code, _, _ = call(port3, "/action",
                              {"token": "HTTPTOK", "name": "bot-stop"},
                              cookie=cookie, origin=f"http://127.0.0.1:{port3}")
            ok &= check("http+пароль: bot-stop → 200",
                        code == 200 and seen2.get("name") == "bot-stop")

            # стоп-всё — составное действие: панель зовёт ИМЕННО run_stop_all
            # (свой отчёт по шагам), а не run_action с одной командой
            seen3: dict = {}
            real_stop_all = d.run_stop_all

            def fake_stop_all(params=None):
                seen3["params"] = params
                return {"ok": True, "rc": 0,
                        "output": "=== СТОП-ВСЁ ===\nШаг 1/3: стоп\n"
                                  "Шаг 2/3: закрытие\nШаг 3/3: сверка\n"}

            d.run_stop_all = fake_stop_all
            try:
                code, body, _ = call(port3, "/action",
                                     {"token": "HTTPTOK", "name": "stop-all"},
                                     cookie=cookie,
                                     origin=f"http://127.0.0.1:{port3}")
                ok &= check("http+пароль: stop-all → 200 и вызов run_stop_all",
                            code == 200 and "params" in seen3
                            and "Шаг 1/3" in body)
                code, _, _ = call(port3, "/action",
                                  {"token": "HTTPTOK", "name": "stop-all"},
                                  origin=f"http://127.0.0.1:{port3}")
                ok &= check("http+пароль: stop-all без сессии → 403",
                            code == 403)
            finally:
                d.run_stop_all = real_stop_all

            # пуск-всё — тоже составное: панель зовёт run_start_all
            seen4: dict = {}
            real_start_all = d.run_start_all

            def fake_start_all(params=None):
                seen4["params"] = params
                return {"ok": True, "rc": 0,
                        "output": "Шаг 1/3: проверка\nИТОГ: бот запущен\n"}

            d.run_start_all = fake_start_all
            try:
                code, body, _ = call(port3, "/action",
                                     {"token": "HTTPTOK", "name": "start-all"},
                                     cookie=cookie,
                                     origin=f"http://127.0.0.1:{port3}")
                ok &= check("http+пароль: start-all → 200 и вызов run_start_all",
                            code == 200 and "params" in seen4
                            and "Шаг 1/3" in body)
                code, _, _ = call(port3, "/action",
                                  {"token": "HTTPTOK", "name": "start-all"},
                                  origin=f"http://127.0.0.1:{port3}")
                ok &= check("http+пароль: start-all без сессии → 403",
                            code == 403)
            finally:
                d.run_start_all = real_start_all
        finally:
            d.run_action = real_run2

        code, _, hdrs = call(port3, "/logout", cookie=cookie)
        ok &= check("http+пароль: /logout → 303 и сброс cookie",
                    code == 303 and "Max-Age=0" in (hdrs.get("Set-Cookie")
                                                    or ""))
        ok &= check("http+пароль: после выхода / снова редирект",
                    call(port3, "/", cookie=cookie)[0] == 303)
    finally:
        srv3.shutdown()
        srv3.server_close()

    print()
    print("Итог:", "все проверки пройдены" if ok else "ЕСТЬ ПРОВАЛЫ")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
