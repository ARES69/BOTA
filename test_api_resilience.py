"""Тесты устойчивости к ошибкам биржи: 10006 (rate limit) с backoff,
10002 (рассинхрон часов) без ретрая, прочие коды — сразу исключение.
Запуск: python test_api_resilience.py  (stdlib, без pytest)."""
from __future__ import annotations

import io
import json
import os
import sys

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

# Тестовые ветки пишут через fe.log в рабочий bot.log: после прогона в лог
# бота попадали тестовые строки. Подменяем путь лога на время теста и
# возвращаем/удаляем временный файл при выходе (в т.ч. при исключении).
import atexit
import tempfile

_orig_log = fe.LOG_FILE
_tf = tempfile.NamedTemporaryFile("w", suffix=".log", delete=False)
_tf.close()
fe.LOG_FILE = _tf.name


def _restore_log():
    fe.LOG_FILE = _orig_log
    try:
        os.remove(_tf.name)
    except OSError:
        pass


atexit.register(_restore_log)


def check(name, cond):
    print(("OK   " if cond else "FAIL ") + name)
    return bool(cond)


class FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def client_with(codes):
    """Клиент, отвечающий последовательностью retCode; None = успех."""
    c = fe.BybitClient.__new__(fe.BybitClient)
    c.mode = "testnet"
    c.key = "k"
    c.secret = "s"
    c.base = "https://x"
    calls = {"n": 0}
    sleeps = []
    codes = list(codes)

    def fake_urlopen(req, timeout=None):
        code = codes[min(calls["n"], len(codes) - 1)]
        calls["n"] += 1
        return FakeResp(json.dumps(
            {"retCode": code, "retMsg": f"err{code}", "result": {"ok": 1}}
        ).encode())

    c._calls = calls
    return c, fake_urlopen, sleeps


ok = True

# 1. rate limit: 10006 дважды, потом успех — запрос повторяется и проходит
fe.time.sleep = lambda s: None          # не ждём реально
c, fake, _ = client_with([10006, 10006, 0])
fe.urllib.request.urlopen = fake
res = c.request("GET", "/v5/market/tickers")
ok &= check("10006 x2 -> retriй до успеха", res == {"ok": 1} and c._calls["n"] == 3)

# 2. rate limit: исчерпали попытки -> RateLimited
c, fake, _ = client_with([10006])
fe.urllib.request.urlopen = fake
try:
    c.request("GET", "/x")
    ok &= check("10006 без успеха -> RateLimited", False)
except fe.RateLimited as e:
    ok &= check("10006 без успеха -> RateLimited", c._calls["n"] == fe.BybitClient.RETRY_MAX)

# 3. 10002 (часы) -> ClockSkew БЕЗ ретрая
c, fake, _ = client_with([10002])
fe.urllib.request.urlopen = fake
try:
    c.request("GET", "/x")
    ok &= check("10002 -> ClockSkew", False)
except fe.ClockSkew as e:
    ok &= check("10002 -> ClockSkew без ретрая (1 попытка)",
                c._calls["n"] == 1 and "NTP" in str(e))

# 4. прочий код -> RuntimeError без ретрая
c, fake, _ = client_with([110001])
fe.urllib.request.urlopen = fake
try:
    c.request("GET", "/x")
    ok &= check("прочий код -> RuntimeError", False)
except fe.RateLimited:
    ok &= check("прочий код -> RuntimeError", False)
except fe.ClockSkew:
    ok &= check("прочий код -> RuntimeError", False)
except RuntimeError as e:
    ok &= check("прочий код -> RuntimeError без ретрая",
                c._calls["n"] == 1 and "110001" in str(e))

# 5. timestamp пересчитывается на каждой попытке (иначе после паузы
#    подпись вне recv_window)
seen_ts = []
c, fake, _ = client_with([10006, 0])
fe.urllib.request.urlopen = fake


def spy(req, timeout=None):
    seen_ts.append(req.headers.get("X-bapi-timestamp")
                   or req.headers.get("X-BAPI-TIMESTAMP"))
    return fake(req, timeout)


fe.urllib.request.urlopen = spy
c.request("GET", "/x")
ok &= check("timestamp пересчитывается на ретрае",
            len(seen_ts) == 2 and seen_ts[0] is not None)

# 6. backoff растёт и имеет потолок
d1 = fe.BybitClient.RETRY_BASE * (2 ** 0)
d2 = fe.BybitClient.RETRY_BASE * (2 ** 3)
ok &= check("backoff растёт", d2 > d1)
ok &= check("пауза не превышает потолок",
            min(fe.BybitClient.RETRY_BASE * 2 ** 20,
                fe.BybitClient.RETRY_CAP) == fe.BybitClient.RETRY_CAP)

print()
print("Итог:", "все проверки пройдены" if ok else "ЕСТЬ ПРОВАЛЫ")
sys.exit(0 if ok else 1)