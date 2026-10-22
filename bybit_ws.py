"""Минимальный клиент WebSocket (RFC 6455) для публичных лент Bybit v5.

Только stdlib: socket, ssl, threading, base64, hashlib, struct, json.
Зачем: публичные каналы tickers.* пушат ставку/цену каждые ~100 мс —
реакция бота на смену ставки, убыток и риск маржи становится мгновенной
вместо опроса REST каждые interval секунд. REST остаётся только на
сделках, истории ставок и предтрейд-проверках (глубина стакана).

Публичный API этого модуля:
  TickerFeed(mode, on_status)   — пуш-лента tickers.* (linear + spot)
    .start(linear_syms, spot_syms)
    .linear(sym) / .linear_items()      — состояние linear-тикеров
    .spot(sym) / .spot_quotes()         — спот bid/ask из ленты
    .ensure_spot(syms)                  — динамическая подписка спот-пар
    .intervals                          — symbol -> часов между выплатами (REST-база)
    .alive()                            — живо ли linear-соединение
    .stop()
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import ssl
import struct
import threading
import time

_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
_MAX_MSG = 1 << 20          # 1 MiB на сообщение — защита от мусора
_OP_CONT, _OP_TEXT, _OP_CLOSE, _OP_PING, _OP_PONG = 0, 1, 8, 9, 10


def ws_host(mode: str) -> str:
    return "stream-testnet.bybit.com" if mode == "testnet" else "stream.bybit.com"


def num(v) -> float:
    """Безопасный float из WS-строк ('', None, '83464.4')."""
    try:
        if v is None or v == "":
            return 0.0
        return float(v)
    except (TypeError, ValueError):
        return 0.0


class WsConn:
    """Одно соединение к публичному WS-эндпоинту Bybit v5.

    Поток-читатель: разбор кадров RFC 6455, автопинг {"op":"ping"} каждые
    20 с (требование Bybit, иначе сервер режет соединение), автопереподключение
    с удвоением паузы до 30 с и пересылкой подписок после реконнекта.
    """

    def __init__(self, name: str, host: str, path: str, on_json, on_status=None):
        self.name = name
        self.host = host
        self.path = path
        self.on_json = on_json              # callback(dict)
        self.on_status = on_status or (lambda msg: None)
        self.topics: list[str] = []         # восстановятся после реконнекта
        self._sock: socket.socket | None = None
        self._send_lock = threading.Lock()
        self._stop = False
        self._last_ping = 0.0
        self.alive = False

    # ------------------------------------------------------------------ lifecycle
    def start(self):
        self._stop = False
        threading.Thread(target=self._run, daemon=True, name=f"ws-{self.name}").start()

    def stop(self):
        self._stop = True
        try:
            self._close_sock()
        except OSError:
            pass

    # ------------------------------------------------------------------ API
    def subscribe(self, topics: list[str]):
        self.topics = list(dict.fromkeys(self.topics + list(topics)))
        self._send_json({"op": "subscribe", "args": list(topics)})

    def unsubscribe(self, topics: list[str]):
        self.topics = [t for t in self.topics if t not in set(topics)]
        self._send_json({"op": "unsubscribe", "args": list(topics)})

    # ------------------------------------------------------------------ connect loop
    def _run(self):
        delay = 1.0
        while not self._stop:
            try:
                sock = self._connect()
                self.alive = True
                self.on_status(f"WS {self.name}: лента подключена")
                delay = 1.0
                if self.topics:
                    self._send_json({"op": "subscribe", "args": list(self.topics)})
                self._read_loop(sock)
            except OSError as e:
                self.alive = False
                if not self._stop:
                    self.on_status(f"WS {self.name}: отвалился ({e}) — "
                                   f"переподключаюсь через {delay:.0f}с")
                    time.sleep(delay)
                    delay = min(delay * 2, 30.0)
            except Exception as e:  # noqa: BLE001 — поток не должен умирать молча
                self.alive = False
                if not self._stop:
                    self.on_status(f"WS {self.name}: сбой ({e}) — переподключаюсь")
                    time.sleep(delay)
                    delay = min(delay * 2, 30.0)
        self.alive = False

    def _connect(self) -> socket.socket:
        raw = socket.create_connection((self.host, 443), timeout=10)
        sock = ssl.create_default_context().wrap_socket(raw, server_hostname=self.host)
        key = base64.b64encode(os.urandom(16)).decode()
        req = (f"GET {self.path} HTTP/1.1\r\n"
               f"Host: {self.host}\r\n"
               "Upgrade: websocket\r\nConnection: Upgrade\r\n"
               f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n")
        sock.sendall(req.encode())
        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = sock.recv(4096)
            if not chunk:
                raise OSError("соединение закрыто при рукопожатии")
            resp += chunk
            if len(resp) > 65536:
                raise OSError("слишком длинный ответ на рукопожатие")
        head, _, rest = resp.partition(b"\r\n\r\n")
        if b" 101 " not in head.split(b"\r\n", 1)[0]:
            raise OSError("рукопожатие отклонено: "
                          + head.decode(errors="replace").splitlines()[0][:120])
        accept = base64.b64encode(hashlib.sha1((key + _WS_GUID).encode()).digest()).decode()
        if accept.lower() not in head.decode(errors="replace").lower():
            raise OSError("Sec-WebSocket-Accept не совпал")
        sock.settimeout(1.0)
        self._sock = sock
        self._rest = rest          # хвост после заголовков — уже данные кадра
        return sock

    def _close_sock(self):
        sock, self._sock = self._sock, None
        if sock:
            try:
                # CLOSE отправляем по ЛОКАЛЬНОМУ сокету: self._sock уже
                # None, поэтому _send_frame() без sock молча возвращался и
                # сервер не получал корректного закрытия (только TCP FIN)
                self._send_frame(_OP_CLOSE, b"", sock=sock)
            except OSError:
                pass
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass

    # ------------------------------------------------------------------ read loop
    def _read_loop(self, sock: socket.socket):
        buf = bytearray(getattr(self, "_rest", b"") or b"")
        self._rest = b""
        self._last_ping = time.time()
        frag_op, frag_buf = None, bytearray()
        while not self._stop:
            if time.time() - self._last_ping >= 20.0:   # ping уровня приложения
                self._send_json({"op": "ping"})
                self._last_ping = time.time()
            try:
                chunk = sock.recv(65536)
            except (socket.timeout, TimeoutError):
                continue
            if not chunk:
                raise OSError("сервер закрыл соединение")
            buf.extend(chunk)
            while True:
                parsed = self._parse(buf)
                if parsed is None:
                    break
                opcode, payload, fin = parsed
                if opcode == _OP_PING:
                    self._send_frame(_OP_PONG, payload)
                    continue
                if opcode == _OP_PONG:
                    continue
                if opcode == _OP_CLOSE:
                    raise OSError("close-кадр от сервера")
                if opcode not in (_OP_TEXT, _OP_CONT):
                    continue
                if fin and opcode == _OP_TEXT and not frag_buf:
                    self._dispatch(payload)
                    continue
                if opcode == _OP_TEXT:      # начало фрагментированного сообщения
                    frag_op, frag_buf = _OP_TEXT, bytearray(payload)
                else:
                    frag_buf.extend(payload)
                if fin:
                    if frag_op == _OP_TEXT:
                        self._dispatch(bytes(frag_buf))
                    frag_op, frag_buf = None, bytearray()

    @staticmethod
    def _parse(buf: bytearray):
        if len(buf) < 2:
            return None
        opcode = buf[0] & 0x0F
        fin = bool(buf[0] & 0x80)
        masked = bool(buf[1] & 0x80)
        n = buf[1] & 0x7F
        pos = 2
        if n == 126:
            if len(buf) < pos + 2:
                return None
            n = struct.unpack_from("!H", buf, pos)[0]
            pos += 2
        elif n == 127:
            if len(buf) < pos + 8:
                return None
            n = struct.unpack_from("!Q", buf, pos)[0]
            pos += 8
        if n > _MAX_MSG:
            raise OSError(f"кадр {n} байт — больше лимита {_MAX_MSG}")
        mask = None
        if masked:
            if len(buf) < pos + 4:
                return None
            mask = bytes(buf[pos:pos + 4])
            pos += 4
        if len(buf) < pos + n:
            return None
        payload = bytes(buf[pos:pos + n])
        if mask:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        del buf[:pos + n]
        return opcode, payload, fin

    def _dispatch(self, payload: bytes):
        try:
            msg = json.loads(payload.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return  # мусор в потоке — игнорируем
        if isinstance(msg, dict):
            self.on_json(msg)

    # ------------------------------------------------------------------ send
    def _send_json(self, obj):
        self._send_frame(_OP_TEXT,
                         json.dumps(obj, separators=(",", ":")).encode("utf-8"))

    def _send_frame(self, opcode: int, payload: bytes, sock=None):
        with self._send_lock:
            sock = sock or self._sock
            if not sock:
                return
            mask = os.urandom(4)
            masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            b0 = 0x80 | opcode
            if len(masked) < 126:
                hdr = struct.pack("!BB", b0, 0x80 | len(masked))
            elif len(masked) < 65536:
                hdr = struct.pack("!BBH", b0, 0x80 | 126, len(masked))
            else:
                hdr = struct.pack("!BBQ", b0, 0x80 | 127, len(masked))
            try:
                sock.sendall(hdr + mask + masked)
            except OSError:
                pass    # читающий поток увидит разрыв и переподключится


class TickerFeed:
    """Пуш-лента tickers.* для linear + spot с ведением состояния в памяти.

    Bybit шлёт snapshot, затем delta (только изменённые поля) — состояние
    символа это словарь, обновляемый по merge. Копии отдаются под замком.
    """

    def __init__(self, mode: str, on_status=None):
        self._on_status = on_status or (lambda msg: None)
        self.intervals: dict[str, float] = {}   # symbol -> часов между выплатами (REST-база)
        self._lin: dict[str, dict] = {}
        self._spot: dict[str, dict] = {}        # bid1Price/ask1Price из orderbook.1
        self._lock = threading.Lock()
        host = ws_host(mode)
        self.conn_lin = WsConn("linear", host, "/v5/public/linear",
                               self._on_ticker_msg, on_status=self._on_status)
        self.conn_spot = WsConn("spot", host, "/v5/public/spot",
                                self._on_spot_msg, on_status=self._on_status)
        self._spot_subscribed: set[str] = set()

    # ------------------------------------------------------------------ lifecycle
    def start(self, linear_syms: list[str], spot_syms: list[str]):
        if linear_syms:
            self.conn_lin.start()
            self.conn_lin.subscribe([f"tickers.{s}" for s in linear_syms])
        if spot_syms:
            self.conn_spot.start()
            self._spot_subscribed.update(spot_syms)
            # у tickers.* спота НЕТ bid/ask — спот-котировки берём из стакана глубины 1
            self.conn_spot.subscribe([f"orderbook.1.{s}" for s in spot_syms])

    def stop(self):
        self.conn_lin.stop()
        self.conn_spot.stop()

    def alive(self) -> bool:
        """Живы ли ОБЕ ленты. Спот-соединение может отвалиться отдельно от
        линейного — тогда цены спота заморожены, и PnL/дельта/max-loss
        молча считались бы по последнему пушу. Значит для обеих нужен alive."""
        if not self.conn_lin.alive:
            return False
        # спот подключается только если есть подписки — без них не считаем
        return self.conn_spot.alive or not self._spot_subscribed

    def spot_alive(self) -> bool:
        """Жива ли именно спот-лента (для точечной диагностики в лог)."""
        return self.conn_spot.alive or not self._spot_subscribed

    # ------------------------------------------------------------------ state
    def linear(self, sym: str) -> dict | None:
        with self._lock:
            d = self._lin.get(sym)
            return dict(d) if d else None

    def linear_items(self) -> dict[str, dict]:
        with self._lock:
            return {s: dict(d) for s, d in self._lin.items()}

    def spot(self, sym: str) -> dict | None:
        with self._lock:
            d = self._spot.get(sym)
            return dict(d) if d else None

    def spot_quotes(self) -> dict[str, tuple[float, float]]:
        """Формат fetch_spot_quotes: symbol -> (bid, ask) — взаимозаменяемо."""
        with self._lock:
            out = {}
            for s, d in self._spot.items():
                out[s] = (num(d.get("bid1Price")), num(d.get("ask1Price")))
            return out

    def ensure_spot(self, syms) -> list[str]:
        """Дописать спот-пары в подписку (позиция/новые кандидаты)."""
        new = [s for s in syms if s and s not in self._spot_subscribed]
        if new:
            self._spot_subscribed.update(new)
            self.conn_spot.subscribe([f"orderbook.1.{s}" for s in new])
        return new

    # ------------------------------------------------------------------ handlers
    def _on_ticker_msg(self, msg: dict):
        topic = msg.get("topic") or ""
        if not topic.startswith("tickers."):
            return
        data = msg.get("data")
        if not isinstance(data, dict):
            return
        sym = topic[len("tickers."):]
        with self._lock:
            if msg.get("type") == "snapshot":
                self._lin[sym] = dict(data)
            else:
                self._lin.setdefault(sym, {}).update(data)

    def _on_spot_msg(self, msg: dict):
        """orderbook.1.*: лучший bid/ask спота. По snapshot берём уровни
        напрямую; по delta — уровни с qty>0 это новый лучший уровень,
        qty=0 у текущего лучшего — уровень снят, ждём следующий пуш."""
        topic = msg.get("topic") or ""
        if not topic.startswith("orderbook.1."):
            if topic.startswith("tickers."):    # лента статов спота: только lastPrice
                data = msg.get("data")
                if isinstance(data, dict):
                    sym = topic[len("tickers."):]
                    with self._lock:
                        cur = self._spot.setdefault(sym, {})
                        if data.get("lastPrice"):
                            cur.setdefault("lastPrice", data["lastPrice"])
            return
        data = msg.get("data")
        if not isinstance(data, dict):
            return
        sym = topic[len("orderbook.1."):]
        with self._lock:
            cur = self._spot.setdefault(sym, {})
            for p, q in data.get("b") or []:
                if num(q) == 0 and p == cur.get("bid1Price"):
                    cur["bid1Price"] = ""     # уровень снят — bid до следующего пуша неизвестен
            for p, q in data.get("a") or []:
                if num(q) == 0 and p == cur.get("ask1Price"):
                    cur["ask1Price"] = ""
            bids = [p for p, q in (data.get("b") or []) if num(q) > 0]
            asks = [p for p, q in (data.get("a") or []) if num(q) > 0]
            if bids:
                cur["bid1Price"] = max(bids, key=num)
            if asks:
                cur["ask1Price"] = min(asks, key=num)
            cur["ts"] = int(msg.get("ts") or 0)
