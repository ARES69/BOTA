#!/usr/bin/env python3
"""
Ежедневный health-check funding-бота (автономный, только stdlib).

Проверяет и шлёт алерт в Telegram, если:
  • сервис не active (упал / failed / activating);
  • сервис в авто-перезапуске (SubState=auto-restart);
  • число перезапусков сервиса выросло с прошлого прогона;
  • лог бота не обновлялся дольше порога (сервис «жив», но завис);
  • позиция из position.json расходится с биржей — тихая потеря ноги
    (шорт снят ликвидацией/руками, спот продан) — независимо от того,
    сломался ли сам бот;
  • панель (dashboard) слушает НЕ на localhost — публичный bind (0.0.0.0,
    ::, внешний IP) открывает позицию/PnL и кнопки управления всему
    интернету; при этом ufw на VPS может быть выключен.

Авто-восстановление bind: в юните dashboard.py запускается без --host (т.е.
дефолт 127.0.0.1), поэтому при публичном bind сервис перезапускается и адрес
проверяется заново. Если панель после рестарта всё ещё публична — значит
публичный адрес прописан в юните (ExecStart с --host): health-check об этом
сообщит, но приведёт панель в порядок только само изменение юнита —
правки systemd-файлов он не делает (и не зацикливает рестарты).

Авто-восстановление: если лог завис, а сервис при этом active (systemd
считает его живым и сам его не перезапустит) — сервис перезапускается, а в
Telegram уходит сообщение с результатом. Расхождение позиции по умолчанию
само НЕ правится (это торговое решение) — в алерт добавляется готовая команда
void/close. С `--fix-position` health-check приводит ноги в порядок сам по
безопасному правилу (void, если на бирже пусто; иначе close), защита от
ложного срабатывания — повторная независимая сверка после паузы.

Запускается как oneshot из systemd-таймера (deploy/funding-health.timer).
Намеренно НЕ импортирует код бота: health-check обязан работать, даже если
funding_executor.py сломан. Telegram-ключи берутся из .env рядом со скриптом.

Коды выхода: 0 — всё в порядке; 1 — найдены проблемы; 2 — нет ключей Telegram.
Только stdlib, Python 3.10+.
"""

import argparse
import hashlib
import hmac
import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))


# ------------------------------------------------------------------ env
def parse_env(text: str) -> dict:
    """KEY=VALUE построчно, # — комментарий, пробелы обрезаются."""
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def load_env_file(path: str) -> dict:
    """Читает .env, не затирая уже заданные переменные окружения."""
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        vals = parse_env(f.read())
    for k, v in vals.items():
        os.environ.setdefault(k, v)
    return vals


# ------------------------------------------------------------------ state
def load_state(path: str) -> dict:
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            pass
    return {"services": {}, "last_alert": {}}


def save_state(path: str, state: dict):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


# ------------------------------------------------------------------ checks
def parse_show(text: str) -> dict:
    """Разбор вывода `systemctl show -p ...` («Key=Value» построчно)."""
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def service_problems(name: str, show: dict,
                     prev_restarts: int | None) -> tuple[list[str], int]:
    """(проблемы сервиса, текущее число перезапусков).
    prev_restarts=None — первый прогон: дельту рестартов не проверяем
    (иначе первый же запуск заалертил бы на «скачок» из нуля)."""
    probs: list[str] = []
    active = show.get("ActiveState") or "unknown"
    sub = show.get("SubState") or ""
    try:
        restarts = int(show.get("NRestarts") or 0)
    except (TypeError, ValueError):
        restarts = 0
    if active != "active":
        state_desc = active + (f"/{sub}" if sub else "")
        probs.append(f"{name}: сервис не активен ({state_desc})")
    elif sub == "auto-restart":
        probs.append(f"{name}: сервис в авто-перезапуске (auto-restart)")
    if prev_restarts is not None and restarts > prev_restarts:
        probs.append(f"{name}: {restarts - prev_restarts} новых перезапусков "
                     f"с прошлой проверки (всего {restarts})")
    return probs, restarts


POSITION_PERP_TOL = 0.02   # допустимое расхождение перп-ноги
POSITION_SPOT_TOL = 0.02   # спот-нога: комиссия входа берётся в базовой монете


def read_positions(path: str) -> list[dict]:
    """ВСЕ открытые позиции из position.json (портфель).

    Список positions[] — источник правды; поле position (одна позиция)
    читается тем же разбором, что и в funding_executor/dashboard: старый
    файл обязан работать. Файла нет / битый json / позиций нет → [].
    """
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    if not isinstance(data, dict):
        return []
    plist = data.get("positions")
    if isinstance(plist, list):
        out = [p for p in plist if isinstance(p, dict)]
        if out:
            return out
    pos = data.get("position")
    return [pos] if isinstance(pos, dict) else []


def read_position(path: str) -> dict | None:
    """Первая позиция из position.json (None — файла нет / позиции нет /
    битый json). Портфель целиком — read_positions."""
    plist = read_positions(path)
    return plist[0] if plist else None


def _base_coin(symbol: str) -> str:
    """ETHUSDT → ETH (базовая монета спот-ноги для проверки баланса)."""
    for quote in ("USDT", "USDC", "USDE", "USD"):
        if symbol.endswith(quote) and len(symbol) > len(quote):
            return symbol[: -len(quote)]
    return symbol


def position_problems(pos: dict, perp_qty: float | None,
                      spot_avail: float | None,
                      perp_tol: float = POSITION_PERP_TOL,
                      spot_tol: float = POSITION_SPOT_TOL) -> list[str]:
    """Расхождение position.json с биржей. None у источника = «не удалось узнать»
    (сеть/ключи) — такой источник в сравнении не участвует и НЕ даёт алерта:
    неизвестность не повод для тревоги."""
    sym = pos.get("symbol") or "?"
    probs: list[str] = []
    if perp_qty is not None and float(pos.get("perp_qty") or 0) > 0:
        want = float(pos["perp_qty"])
        if perp_qty <= 0:
            probs.append(f"позиция {sym}: перп-нога исчезла на бирже "
                         f"(в position.json {want:g})")
        elif abs(perp_qty - want) > max(want * perp_tol, 1e-9):
            probs.append(f"позиция {sym}: перп-нога {perp_qty:g} ≠ {want:g} "
                         f"из position.json")
    if spot_avail is not None and float(pos.get("spot_qty") or 0) > 0:
        want = float(pos["spot_qty"])
        if spot_avail < want * (1 - spot_tol):
            probs.append(f"позиция {sym}: спот-нога неполна "
                         f"({spot_avail:g} из {want:g})")
        elif spot_avail > want * (1 + spot_tol):
            probs.append(f"позиция {sym}: спот-ноги больше записи "
                         f"({spot_avail:g} vs {want:g})")
    return probs


def recovery_hint(pos: dict, perp_qty: float | None,
                  spot_avail: float | None) -> str | None:
    """Готовая команда для разбора расхождения (подсказка в алерте).
    Сам health-check позицию НЕ трогает: закрытие/снятие — торговое решение.
    None — если ничего достоверного о позиции не знаем."""
    sym = pos.get("symbol") or "?"
    want_perp = float(pos.get("perp_qty") or 0)
    if perp_qty is None and spot_avail is None:
        return None
    if perp_qty is not None and want_perp > 0 and perp_qty <= 0:
        return (f"перп-нога {sym} на бирже отсутствует → снять запись:"
                f"  python funding_executor.py void --force --symbol {sym}")
    return (f"ноги {sym} разошлись → закрыть: python funding_executor.py "
            f"close --symbol {sym} (или void --force --symbol {sym}, "
            f"если на бирже уже пусто)")


class _Signer:
    """Минимальный подписанный GET к Bybit v5 (только stdlib).
    health-check намеренно НЕ импортирует код бота — сверка позиции
    обязана работать, даже если funding_executor.py сломан."""

    def __init__(self, base: str, key: str, secret: str, timeout: int = 15):
        self.base = base
        self.key = key
        self.secret = secret
        self.timeout = timeout

    def get(self, path: str, params: dict | None = None) -> dict:
        recv = "5000"
        ts = int(time.time() * 1000)
        payload = urllib.parse.urlencode(params or {})
        msg = f"{ts}{self.key}{recv}{payload}".encode()
        sign = hmac.new(self.secret.encode(), msg, hashlib.sha256).hexdigest()
        url = self.base + path + (("?" + payload) if payload else "")
        req = urllib.request.Request(url, headers={
            "X-BAPI-API-KEY": self.key,
            "X-BAPI-TIMESTAMP": str(ts),
            "X-BAPI-RECV-WINDOW": recv,
            "X-BAPI-SIGN": sign,
        })
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            data = json.loads(resp.read().decode())
        if data.get("retCode") != 0:
            raise RuntimeError(f"Bybit {data.get('retCode')}: {data.get('retMsg')}")
        return data["result"]

    def perp_size(self, symbol: str) -> float:
        r = self.get("/v5/position/list",
                     {"category": "linear", "symbol": symbol})
        return sum(float(p.get("size") or 0) for p in (r.get("list") or []))

    def coin_available(self, coin: str) -> float | None:
        """Доступный к продаже баланс монеты (UNIFIED).
        transferBalance, а не walletBalance — как в funding_executor: в
        walletBalance входят суммы в резерве висящих ордеров, и сверка с
        position.json давала бы ложные расхождения (а после правки
        executor-а — ещё и систематически более мягкий порог, чем у бота)."""
        r = self.get("/v5/account/wallet-balance", {"accountType": "UNIFIED"})
        for acct in (r.get("list") or []):
            for c in (acct.get("coin") or []):
                if c.get("coin") == coin:
                    try:
                        v = c.get("transferBalance")
                        if v is None:
                            v = c.get("walletBalance")
                        return float(v or 0)
                    except (TypeError, ValueError):
                        return None
        return 0.0


def probe_position(pos: dict) -> tuple[float | None, float | None, str | None]:
    """(перп-размер, доступный спот, примечание) из среды позиции.
    paper — биржа не проверяется; нет ключей / сбой сети → (None, None, причина)."""
    mode = pos.get("mode")
    if mode == "paper":
        return None, None, "paper: симуляция — биржа не проверяется"
    if mode == "mainnet":
        key = os.environ.get("BYBIT_API_KEY", "").strip()
        secret = os.environ.get("BYBIT_API_SECRET", "").strip()
        base = "https://api.bybit.com"
    else:
        key = os.environ.get("BYBIT_TESTNET_API_KEY", "").strip()
        secret = os.environ.get("BYBIT_TESTNET_API_SECRET", "").strip()
        base = "https://api-testnet.bybit.com"
    if not key or not secret:
        return None, None, f"{mode}: нет API-ключей — позицию не проверить"
    try:
        signer = _Signer(base, key, secret)
        perp = signer.perp_size(pos["symbol"])
        coin = _base_coin(pos.get("spot_symbol") or "")
        spot = signer.coin_available(coin) if coin else None
        return perp, spot, None
    except Exception as e:  # noqa: BLE001 — недоступная биржа не должна ронять check
        return None, None, f"{mode}: биржа недоступна ({e})"


# ------------------------------------------------------------------ autofix
def fix_action(pos: dict, perp_qty: float | None,
               spot_avail: float | None) -> str | None:
    """Какое действие безопасно при расхождении позиции:
    «void» — на бирже пусто (нет ни перп-, ни спот-ноги), ордера не нужны;
    «close» — есть реальные ноги (флэт: снять перп reduce-only + продать спот);
    None — о позиции ничего достоверного не знаем, не трогаем.

    Правило намеренно асимметрично: пустая биржа → void (безопасно, без
    ордеров), любой реальный остаток → close (флэт-действие)."""
    if perp_qty is None and spot_avail is None:
        return None
    want_spot = float(pos.get("spot_qty") or 0)
    perp_empty = perp_qty is not None and perp_qty <= 0
    spot_empty = (spot_avail is not None
                  and (want_spot <= 0
                       or spot_avail <= want_spot * POSITION_SPOT_TOL))
    if perp_empty and spot_empty:
        return "void"
    return "close"


def _run_capture(cmd: list) -> tuple[int, str]:
    """Запустить команду-фикс (funding_executor void/close) и собрать вывод."""
    res = subprocess.run(cmd, capture_output=True, text=True,
                         encoding="utf-8", errors="replace", timeout=120)
    return res.returncode, (res.stdout or "") + (res.stderr or "")


def apply_position_fix(pos: dict, action: str,
                       executor_path: str | None = None,
                       pos_path: str | None = None,
                       run_cmd=None, probe=None,
                       read_pos=None) -> tuple[bool, str]:
    """Выполнить безопасный авто-фикс позиции и проверить итог.
    run_cmd/probe/read_pos — точки внедрения для тестов (по умолчанию боевые).
    Возврат (удалось, сообщение)."""
    executor_path = executor_path or os.path.join(HERE, "funding_executor.py")
    pos_path = pos_path or os.path.join(HERE, "position.json")
    run_cmd = run_cmd or _run_capture
    probe = probe or probe_position
    read_pos = read_pos or (lambda: read_position(pos_path))
    sym = pos.get("symbol") or "?"
    mode = pos.get("mode") or "testnet"
    if action == "void":
        cmd = [sys.executable, executor_path, "void", "--force", "--mode", mode]
    else:
        cmd = [sys.executable, executor_path, "close", "--mode", mode]
    if sym and sym != "?":
        # в портфеле закрывать надо ИМЕННО эту пару (та же гарантия, что у
        # команды close без --symbol: без символа она отказывается работать)
        cmd += ["--symbol", sym]
    try:
        rc, out = run_cmd(cmd)
    except Exception as e:  # noqa: BLE001 — не роняем health-check
        return False, f"позиция {sym}: авто-фикс ({action}) не запустился ({e})"
    if rc != 0:
        tail = (out or "").strip().replace(chr(10), " ")[-200:]
        return False, (f"позиция {sym}: авто-фикс ({action}) вернул "
                       f"код {rc} ({tail})")

    # итог: состояние очищено и на бирже пусто (или ноги снова сошлись)
    newpos = read_pos()
    if newpos is not None:
        perp, spot, _ = probe(newpos)
        left = position_problems(newpos, perp, spot)
        if left:
            return False, (f"позиция {sym}: авто-фикс ({action}) не помог — "
                           f"{'; '.join(left)}")
        return True, f"позиция {sym}: авто-фикс ({action}) — ноги сошлись"
    perp, spot, _ = probe(pos)
    if perp is None and spot is None:
        return True, (f"позиция {sym}: авто-фикс ({action}) выполнен; "
                      f"биржа недоступна для подтверждения")
    want_spot = float(pos.get("spot_qty") or 0)
    spot_ok = (spot is None or want_spot <= 0
               or spot <= want_spot * POSITION_SPOT_TOL)
    if (perp is None or perp <= 0) and spot_ok:
        return True, f"позиция {sym}: авто-фикс ({action}) — биржа флэт"
    return False, (f"позиция {sym}: авто-фикс ({action}) — на бирже "
                   f"остались ноги (перп {perp}, спот {spot})")


def log_age_problem(log_age_s: float | None, max_age_s: float,
                    name: str = "watch.log") -> list[str]:
    """Свежесть лога: None — файла нет; больше порога — сервис завис."""
    if log_age_s is None:
        return [f"{name}: файл лога не найден"]
    if max_age_s > 0 and log_age_s > max_age_s:
        return [f"{name}: лог не обновлялся {log_age_s / 60:.1f} мин "
                f"(порог {max_age_s / 60:.0f} мин) — сервис мог зависнуть"]
    return []


def evaluate(service_shows: dict, prev_restarts: dict,
             log_age_s: float | None,
             max_age_s: float,
             extra: list[str] | None = None) -> tuple[list[str], dict]:
    """Собрать все проблемы и новое число перезапусков по сервисам.
    extra — дополнительные проблемы (напр., расхождение позиции)."""
    problems: list[str] = []
    new_restarts: dict = {}
    for name, show in service_shows.items():
        prev = prev_restarts.get(name)
        probs, nr = service_problems(name, show, prev)
        problems += probs
        new_restarts[name] = nr
    problems += log_age_problem(log_age_s, max_age_s)
    problems += list(extra or [])
    return problems, new_restarts


# ------------------------------------------------------------------ bind
# По умолчанию панель слушает только localhost. Проверяем именно адрес bind,
# а не файрвол: ufw на VPS может быть выключен, и тогда единственная защита —
# сам bind. Публичный адрес = позиция/PnL/кнопки доступны из интернета.
# Сознательный публичный bind (DASHBOARD_PUBLIC=1) разрешён только вместе
# с DASHBOARD_PASSWORD — см. dashboard_bind_problems.
def _is_loopback(addr: str) -> bool:
    """127.0.0.0/8, ::1 или имя localhost."""
    a = (addr or "").strip().strip("[]")
    return a == "localhost" or a == "::1" or a.startswith("127.")


def parse_listeners(text: str) -> list[dict]:
    """Разобрать вывод `ss -ltn`: [{addr, port}] слушающих сокетов.
    Строка: State Recv-Q Send-Q <Local Address:Port> <Peer> [Process]."""
    out: list[dict] = []
    for line in (text or "").splitlines():
        parts = line.split()
        if len(parts) < 4 or parts[0] == "State":
            continue
        addr, sep, port = parts[3].rpartition(":")
        if not sep or not port.isdigit():
            continue
        out.append({"addr": addr.strip().strip("[]"), "port": port})
    return out


def public_bind_problems(listeners: list[dict], port: int,
                         name: str = "dashboard") -> list[str]:
    """Проблемы, если панель на порту слушает не только localhost.
    Пустой список — либо порт нигде не слушается, либо только loopback
    (отдельно ловит service_problems: лежащий сервис здесь не дублируем)."""
    seen: set[str] = set()
    probs: list[str] = []
    for lst in listeners:
        if lst.get("port") != str(port):
            continue
        addr = lst.get("addr") or ""
        if _is_loopback(addr) or addr in seen:
            continue
        seen.add(addr)
        shown = addr if addr not in ("", "*") else "0.0.0.0"
        probs.append(f"{name}: панель слушает {shown}:{port} — доступна "
                     f"из интернета; нужен bind на 127.0.0.1")
    return probs


def dashboard_bind_problems(listeners: list[dict], port: int,
                            allow_public: bool = False, password: str = "",
                            name: str = "dashboard") -> list[str]:
    """Политика bind панели: localhost — норма; публичный — только с паролем.

    allow_public (DASHBOARD_PUBLIC=1) — пользователь сознательно открыл панель
    по IP:порту. В этом случае bind не трогаем, но пустой пароль становится
    проблемой (иначе панель с кнопкой «закрыть позицию» открыта всем).
    """
    if allow_public:
        if not (password or "").strip():
            return [f"{name}: панель открыта по сети, но DASHBOARD_PASSWORD "
                    f"не задан — вход на панель без пароля"]
        return []
    return public_bind_problems(listeners, port, name)


def probe_listeners(cmd: tuple = ("ss", "-ltn")) -> list[dict]:
    """Слушающие сокеты системы. Нет `ss`/ошибка → [] (не роняем проверку)."""
    try:
        res = subprocess.run(list(cmd), capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=10)
    except (OSError, subprocess.SubprocessError):
        return []
    if res.returncode != 0:
        return []
    return parse_listeners(res.stdout or "")


def problem_signature(problems: list[str]) -> str:
    """Стабильный отпечаток набора проблем — для анти-спама."""
    return hashlib.sha1("\n".join(sorted(problems)).encode("utf-8")).hexdigest()[:16]


def should_send(sig: str, last_alert: dict, now_ms: int,
                realert_hours: float, force: bool) -> bool:
    """Слать ли алерт: новую проблему — да; ту же самую — не чаще, чем
    раз в realert_hours (таймер может сработать дважды)."""
    if force:
        return True
    if not sig:
        return False
    if last_alert.get("sig") == sig:
        prev = int(last_alert.get("ts_ms") or 0)
        if prev and (now_ms - prev) < realert_hours * 3_600_000:
            return False
    return True


# ------------------------------------------------------------------ telegram
def format_message(problems: list[str], services: list[str],
                   log_age_s: float | None,
                   hint: str | None = None) -> str:
    """Текст алерта: список проблем + подсказка, где смотреть / что делать."""
    head = (f"⚠️ Health-check бота: {len(problems)} " +
            ("проблема" if len(problems) == 1 else "проблемы"))
    body = "\n".join(f"• {p}" for p in problems)
    hint_s = f"\n\n💡 {hint}" if hint else ""
    tail = ("\nsystemctl status " + " ".join(services) +
            "\njournalctl -u funding-watch -n 30 --no-pager")
    when = datetime.now().strftime("%Y-%m-%d %H:%M")
    return f"{head}\n{body}{hint_s}\n{tail}\n({when})"


def format_ok(services: list[str], log_age_s: float | None) -> str:
    age = "нет данных" if log_age_s is None else f"{log_age_s / 60:.1f} мин"
    return (f"✅ Health-check бота: всё в порядке. "
            f"{', '.join(services)} — active, лог обновлён {age} назад "
            f"({datetime.now().strftime('%Y-%m-%d %H:%M')})")


def send_telegram(token: str, chat: str, text: str, timeout: int = 10) -> bool:
    """Отправка сообщения. False при любой ошибке — check не должен падать."""
    if not token or not chat:
        return False
    try:
        data = urllib.parse.urlencode(
            {"chat_id": chat, "text": text, "disable_web_page_preview": "true"}
        ).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage", data=data)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode())
        return bool(body.get("ok"))
    except Exception:  # noqa: BLE001 — сеть/токен: не роняем проверку
        return False


# ------------------------------------------------------------------ probes
def probe_service(name: str) -> dict:
    """Состояние сервиса через systemctl (пусто — сервиса нет)."""
    try:
        res = subprocess.run(
            ["systemctl", "show",
             "-p", "ActiveState,SubState,NRestarts", name],
            capture_output=True, text=True, timeout=15)
        return parse_show(res.stdout or "")
    except Exception:  # noqa: BLE001 — нет systemd/прав
        return {}


def log_age(path: str) -> float | None:
    """Сколько секунд назад лог обновлялся (None — файла нет)."""
    try:
        return max(0.0, time.time() - os.path.getmtime(path))
    except OSError:
        return None


# ------------------------------------------------------------------ autoheal
def _fresh(age: float | None, max_age_s: float) -> bool:
    """Лог считается живым: файл есть и обновлялся не позже порога."""
    return age is not None and (max_age_s <= 0 or age <= max_age_s)


def _systemctl_restart(name: str):
    """`systemctl restart <name>`; ненулевой rc — исключение."""
    res = subprocess.run(["systemctl", "restart", name],
                         capture_output=True, text=True, timeout=30)
    if res.returncode != 0:
        raise RuntimeError((res.stderr or res.stdout or "").strip()
                           or f"rc={res.returncode}")


def restart_service(name: str, log_path: str, max_age_s: float,
                    run_cmd=None, probe=None, log_age_fn=None,
                    sleep_fn=None, tries: int = 6,
                    wait: float = 2.0) -> tuple[bool, str]:
    """Перезапустить зависший сервис и убедиться, что он поднялся и лог ожил.
    Возвращает (восстановлен, сообщение). Параметры run_cmd/probe/... —
    точки внедрения для тестов (по умолчанию боевые)."""
    run_cmd = run_cmd or _systemctl_restart
    probe = probe or probe_service
    log_age_fn = log_age_fn or log_age
    sleep_fn = sleep_fn or time.sleep
    try:
        run_cmd(name)
    except Exception as e:  # noqa: BLE001 — нет прав/сервиса: не роняем check
        return False, f"{name}: команда перезапуска не удалась ({e})"
    last = "нет данных"
    for _ in range(max(1, tries)):
        sleep_fn(wait)
        show = probe(name)
        age = log_age_fn(log_path)
        active = show.get("ActiveState") == "active"
        if active and _fresh(age, max_age_s):
            return True, (f"{name}: лог завис — сервис перезапущен, "
                          f"снова active, лог свежий")
        last = (f"active={active}, возраст лога="
                f"{'нет' if age is None else f'{age:.0f} c'}")
    return False, (f"{name}: лог завис — ПЕРЕЗАПУСК НЕ ПОМОГ ({last}), "
                   f"нужен ручной разбор")


def heal_public_bind(port: int, service: str = "dashboard",
                     restart=None, probe=None, sleep_fn=None,
                     tries: int = 6, wait: float = 1.5) -> tuple[bool, str]:
    """Панель слушает публично — перезапустить сервис и убедиться, что bind
    вернулся на loopback.

    Рестарт лечит случай, когда публичный адрес был у ЗАПУЩЕННОГО процесса
    (кто-то стартовал dashboard.py --host 0.0.0.0 руками): дефолт без
    --host — 127.0.0.1. Публичный bind может быть и в юните — но только
    осознанно, через drop-in deploy/dashboard-public.conf: тогда после
    рестарта порт остаётся публичным, и мы НЕ зацикливаемся — сообщаем,
    что публичный адрес прописан в ExecStart и лечится либо DASHBOARD_PUBLIC=1
    в .env (осознанный выбор), либо удалением drop-in.
    restart/probe/sleep_fn — точки внедрения для тестов.
    Возврат (восстановлен, сообщение)."""
    restart = restart or _systemctl_restart
    probe = probe or probe_listeners
    sleep_fn = sleep_fn or time.sleep
    try:
        restart(service)
    except Exception as e:  # noqa: BLE001 — нет прав/сервиса: не роняем check
        return False, f"bind: перезапуск {service} не удался ({e})"
    for _ in range(max(1, tries)):
        sleep_fn(wait)
        if not public_bind_problems(probe(), port, service):
            return True, (f"bind: {service} слушал публично — сервис "
                          f"перезапущен, панель снова только на "
                          f"127.0.0.1:{port}")
    return False, (f"bind: {service} всё ещё слушает публично после "
                   f"перезапуска — публичный адрес прописан в юните. Если "
                   f"это осознанно (панель по IP за паролем) — добавь "
                   f"DASHBOARD_PUBLIC=1 в .env, иначе поправь ExecStart "
                   f"(--host 127.0.0.1)")


# ------------------------------------------------------------------ cli
def main() -> int:
    p = argparse.ArgumentParser(
        description="Ежедневный health-check funding-бота: сервисы, "
                    "перезапуски, свежесть лога → Telegram.")
    p.add_argument("--services", default="funding-watch,dashboard",
                   help="сервисы через запятую (default funding-watch,dashboard)")
    p.add_argument("--log", default=os.path.join(HERE, "watch.log"),
                   help="лог, следящий за живостью бота (default watch.log)")
    p.add_argument("--max-log-age", type=float, default=900.0,
                   help="порог устаревания лога в сек (default 900 = 15 мин)")
    p.add_argument("--realert-hours", type=float, default=20.0,
                   help="не повторять тот же алерт чаще N часов (default 20)")
    p.add_argument("--state", default=os.path.join(HERE, "health_state.json"),
                   help="файл состояния (перезапуски, последний алерт)")
    p.add_argument("--position", default=os.path.join(HERE, "position.json"),
                   help="файл позиции бота для сверки с биржей "
                        "(default position.json)")
    p.add_argument("--no-position", action="store_true",
                   help="не сверять позицию с биржей (только сервисы и лог)")
    p.add_argument("--dashboard-port", type=int, default=8050,
                   help="порт панели: проверяем, что слушает только "
                        "localhost, а не 0.0.0.0 (default 8050)")
    p.add_argument("--no-bind-check", action="store_true",
                   help="не проверять адрес bind панели")
    p.add_argument("--allow-public-bind", action="store_true",
                   default=False,
                   help="панель сознательно открыта по IP:порту и защищена "
                        "паролем входа (env DASHBOARD_PASSWORD). Иначе "
                        "публичный bind считается проблемой и лечится "
                        "рестартом. То же включает DASHBOARD_PUBLIC=1 в .env")
    p.add_argument("--dashboard-service", default="dashboard",
                   help="сервис панели для авто-фикса bind (default dashboard)")
    p.add_argument("--fix-position", dest="fix_position", action="store_true",
                   default=False,
                   help="при расхождении позиции самому выполнить void/close "
                        "(по умолчанию выключено — только подсказка)")
    p.add_argument("--no-fix-position", dest="fix_position",
                   action="store_false", default=False,
                   help="не исправлять позицию автоматически (default)")
    p.add_argument("--fix-confirm-delay", type=float, default=3.0,
                   help="пауза перед повторной сверкой для защиты от "
                        "ложного срабатывания, сек (default 3)")
    p.add_argument("--autoheal", dest="autoheal", action="store_true",
                   default=True,
                   help="лог завис, а сервис active → перезапустить сервис "
                        "(default: включено)")
    p.add_argument("--no-autoheal", dest="autoheal", action="store_false",
                   help="не перезапускать сервисы автоматически")
    p.add_argument("--autoheal-services", default="funding-watch",
                   help="какие сервисы разрешено перезапускать "
                        "(default funding-watch)")
    p.add_argument("--report-ok", action="store_true",
                   help="слать также сообщение об успехе (иначе молчим)")
    p.add_argument("--force", action="store_true",
                   help="игнорировать анти-спам: отправить алерт всегда")
    p.add_argument("--dry-run", action="store_true",
                   help="не отправлять в Telegram, только напечатать")
    args = p.parse_args()

    load_env_file(os.path.join(HERE, ".env"))
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    # Панель по IP:порту — сознательное решение (DASHBOARD_PUBLIC=1), но
    # только с паролем входа. Без пароля публичный bind остаётся проблемой.
    dash_pw = os.environ.get("DASHBOARD_PASSWORD", "").strip()
    allow_public = (args.allow_public_bind
                    or os.environ.get("DASHBOARD_PUBLIC", "").strip() == "1")

    services = [s.strip() for s in args.services.split(",") if s.strip()]
    state = load_state(args.state)
    prev = {n: (state.get("services", {}).get(n) or {}).get("nrestarts")
            for n in services}

    shows = {n: probe_service(n) for n in services}
    age = log_age(args.log)

    # позиция: биржа vs position.json — тихая потеря ноги (шорт снят ликвидацией,
    # спот продан). Проверяем независимо от процесса бота: если бот завис
    # или умер, его собственная сверка не работает, а наша — работает.
    extra: list[str] = []
    hint: str | None = None
    bind_extra: list[str] = []
    pos: dict | None = None
    # отчёты по КАЖДОЙ паре портфеля отдельно: одна испорченная нога в
    # одной паре не должна прятаться за здоровьем остальных
    reports: list[dict] = []
    if not args.no_position:
        for pos in read_positions(args.position):
            perp, spot, note = probe_position(pos)
            if note:
                print(f"позиция {pos.get('symbol')}: {note}")
            probs = position_problems(pos, perp, spot)
            if not probs:
                perp_s = "н/д" if perp is None else f"{perp:g}"
                spot_s = "н/д" if spot is None else f"{spot:g}"
                print(f"позиция {pos.get('symbol')}: сверено с биржей "
                      f"(перп {perp_s}, спот {spot_s}) — совпадает")
            reports.append({"pos": pos, "perp": perp, "spot": spot,
                            "problems": probs,
                            "hint": recovery_hint(pos, perp, spot)})
        extra = [p for r in reports for p in r["problems"]]
        hint = next((r["hint"] for r in reports if r["hint"]), None)
        if len(reports) > 1:
            print(f"портфель: {len(reports)} пар, расхождений "
                  f"{sum(1 for r in reports if r['problems'])}")

    # авто-фикс позиции (--fix-position): безопасное правило void/close.
    # Защита от ложного срабатывания — ПОВТОРНАЯ независимая сверка после
    # паузы: если второй зонд расхождения не видит, ничего не трогаем.
    # Само действие исполняет funding_executor.py (void/close) отдельным
    # процессом — health-check остаётся автономным и не импортирует бота.
    if any(r["problems"] for r in reports) and args.fix_position:
        for r in reports:
            if not r["problems"]:
                continue
            pos = r["pos"]
            if args.dry_run:
                print(f"[dry-run] авто-фикс позиции {pos.get('symbol')}: "
                      f"выполнил бы {fix_action(pos, r['perp'], r['spot'])}")
                continue
            if args.fix_confirm_delay > 0:
                time.sleep(args.fix_confirm_delay)
            p2, s2, _ = probe_position(pos)
            if not position_problems(pos, p2, s2):
                print(f"  авто-фикс {pos.get('symbol')}: повторная сверка "
                      f"совпала — вероятно, ложное срабатывание, "
                      f"позицию не трогаю")
                r["problems"] = []
                r["hint"] = None
                continue
            act = fix_action(pos, p2, s2)
            ok_fix, fmsg = apply_position_fix(pos, act,
                                              pos_path=args.position)
            print(f"  ⚙ {fmsg}")
            r["problems"] = [("✅ " if ok_fix else "‼ ") + fmsg]
            if ok_fix:
                r["hint"] = None   # ноги уже выровнены — ручная команда не нужна
        extra = [p for r in reports for p in r["problems"]]
        hint = next((r["hint"] for r in reports if r["problems"] and r["hint"]),
                    None)

    # bind панели: слушает ли dashboard публично (0.0.0.0/::/внешний IP).
    # Считаем ПОСЛЕ блока авто-фикса: тот может обнулить extra, а bind-проблемы
    # от этого зависеть не должны.
    if not args.no_bind_check:
        bind_extra = dashboard_bind_problems(
            probe_listeners(), args.dashboard_port, allow_public, dash_pw)
        if bind_extra:
            bind_hint = ("задайте DASHBOARD_PASSWORD в .env" if allow_public
                         else "панель доступна из интернета — запусти "
                              "dashboard.py с --host 127.0.0.1 и не открывай "
                              "порт в файрволе")
            hint = hint or bind_hint
            # Рестарт лечит только случайный публичный bind; при
            # DASHBOARD_PUBLIC=1 адрес прописан в юните сознательно.
            if args.autoheal and not args.dry_run and not allow_public:
                healed, bmsg = heal_public_bind(args.dashboard_port,
                                                args.dashboard_service)
                print(f"  ⚙ {bmsg}")
                bind_extra = [("✅ " if healed else "‼ ") + bmsg]
                if healed and hint == bind_hint:
                    hint = None      # bind уже приведён в порядок
            elif args.autoheal and args.dry_run and not allow_public:
                print(f"[dry-run] авто-фикс bind: перезапустил бы "
                      f"{args.dashboard_service} (bind вернётся на 127.0.0.1)")

    problems, new_restarts = evaluate(shows, prev, age, args.max_log_age,
                                      list(extra) + bind_extra)

    # авто-восстановление: сервис числится active, но лог не обновлялся —
    # это зависание: systemd его сам не перезапустит (Restart=always ловит
    # только смерть процесса). Позицию НЕ трогаем — только подсказка.
    autoheal_svcs = [s.strip() for s in args.autoheal_services.split(",")
                     if s.strip()]
    if args.autoheal and autoheal_svcs:
        stale_tags = log_age_problem(age, args.max_log_age)
        if stale_tags:
            for name in autoheal_svcs:
                show = shows.get(name) or probe_service(name)
                if show.get("ActiveState") != "active":
                    continue   # упавший сервис — не наш случай (его поднимает systemd)
                if args.dry_run:
                    print(f"[dry-run] авто-восстановление: перезапустил бы {name}")
                    continue
                healed, msg = restart_service(name, args.log, args.max_log_age)
                problems = [p for p in problems if p not in stale_tags]
                print(f"  ↻ {msg}")
                problems.append(("✅ " if healed else "‼ ") + msg)

    now_ms = int(time.time() * 1000)
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] health-check: "
          f"сервисы={services} log_age="
          f"{'нет' if age is None else f'{age:.0f}s'} проблем={len(problems)}")
    for pr in problems:
        print(f"  • {pr}")

    sent = False
    if problems:
        msg = format_message(problems, services, age, hint)
        sig = problem_signature(problems)
        last = state.get("last_alert") or {}
        if should_send(sig, last, now_ms, args.realert_hours, args.force):
            if args.dry_run:
                print("[dry-run] алерт:\n" + msg)
            elif not token or not chat:
                print("!! Telegram не настроен (.env: TELEGRAM_BOT_TOKEN/"
                      "TELEGRAM_CHAT_ID) — алерт не отправлен", file=sys.stderr)
            else:
                sent = send_telegram(token, chat, msg)
                print(f"Telegram: {'отправлен' if sent else 'ОШИБКА отправки'}")
            state["last_alert"] = {"sig": sig, "ts_ms": now_ms}
        else:
            print("алерт подавлен анти-спамом (та же проблема недавно)")
    elif args.report_ok:
        msg = format_ok(services, age)
        if args.dry_run:
            print("[dry-run] ok:\n" + msg)
        elif token and chat:
            print(f"Telegram: {'ok отправлен' if send_telegram(token, chat, msg) else 'ОШИБКА'}")
        if not token or not chat:
            print("!! Telegram не настроен — ok-сообщение не отправлено",
                  file=sys.stderr)

    state.setdefault("services", {})
    for name, nr in new_restarts.items():
        state["services"][name] = {"nrestarts": nr,
                                   "checked_ms": now_ms}
    save_state(args.state, state)

    if problems:
        return 1
    if not token or not chat:
        # проверки прошли, но канала уведомления нет — предупреждаем кодом 2
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
