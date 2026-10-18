"""生のバイトの流れ（RTT のチャンネル・SWO・UART 等）を TCP で公開する口と、ファイルへの記録（U05）。

SEGGER の RTT Telnet サーバ（localhost:19021）・RTT Logger、OpenOCD の RTT の TCP ポート、orbuculum の多重化サーバに
相当する。機器にもデーモンにも依らない部品で、流れの出どころ・書き込み先は使う側（fprobe.probe_streams）が渡す。

- StreamServer: 1 つの流れを 1 つのポートで待ち受け、つながった全員に同じバイトを配る（それぞれ専用の送り待ちを
  持ち、読まない相手は上限を超えた分を捨てて数える。ほかの相手も流れの出どころも待たせない）。相手から来たバイトは
  on_input へ渡す（下りの書き込み）。protocol "telnet" は Telnet の取り決めの最小限（0xFF の二重化、来た交渉の
  命令を捨てる）を行う。待ち受けは既定で 127.0.0.1 だけ（認証の無い口を LAN に開かない）。
- StreamLogger: 流れをファイルに書く（そのまま、または行の頭にこの PC の時刻を付けて）。
"""

import collections
import socket
import threading
from datetime import datetime
from pathlib import Path
from typing import Callable

__all__ = ["IAC", "PROTOCOLS", "StreamLogger", "StreamServer", "TelnetFilter", "telnet_escape"]

PROTOCOLS = ("raw", "telnet")
QUEUE_LIMIT = 1 << 20                  # 相手ごとの送り待ちの上限（バイト）
IAC, SB, SE = 0xFF, 0xFA, 0xF0
_OPTION_COMMANDS = {0xFB, 0xFC, 0xFD, 0xFE}           # WILL / WONT / DO / DONT（後ろに 1 バイトの選択肢）


def telnet_escape(data: bytes) -> bytes:
    """送るデータの 0xFF（IAC）を二重にする。"""
    return data.replace(b"\xff", b"\xff\xff")


class TelnetFilter:
    """Telnet の相手から来たバイトから、命令（IAC …）を除いたデータを取り出す（呼び出しをまたいで続きを扱う）。
    交渉には応じない（WILL / DO 等は捨てる。相手は既定の動作のまま）。CR NUL は CR に戻す。"""

    def __init__(self):
        self._state = "data"

    def feed(self, data: bytes) -> bytes:
        out = bytearray()
        for b in data:
            state = self._state
            if state == "data":
                if b == IAC:
                    self._state = "iac"
                elif b == 0 and out[-1:] == b"\r":
                    pass
                else:
                    out.append(b)
            elif state == "iac":
                if b == IAC:
                    out.append(IAC)
                    self._state = "data"
                elif b in _OPTION_COMMANDS:
                    self._state = "option"
                elif b == SB:
                    self._state = "sub"
                else:
                    self._state = "data"                       # 2 バイトの命令（NOP・AYT 等）
            elif state == "option":
                self._state = "data"
            elif state == "sub":
                if b == IAC:
                    self._state = "sub_iac"
            elif state == "sub_iac":
                self._state = "data" if b == SE else "sub"
        return bytes(out)


class _Peer:
    """つながった相手 1 つ: 送り待ち（上限つき）と、送る・受けるスレッド。"""

    def __init__(self, server: "StreamServer", sock: socket.socket, address):
        self.server, self.sock = server, sock
        self.address = f"{address[0]}:{address[1]}"
        self.queue: collections.deque[bytes] = collections.deque()
        self.queued = 0
        self.sent = 0
        self.received = 0
        self.dropped = 0
        self.closed = False
        self.cond = threading.Condition()
        self.filter = TelnetFilter() if server.protocol == "telnet" else None
        self.threads = [threading.Thread(target=self._send_loop, name=f"fprobe-stream-{server.name}-send", daemon=True),
                        threading.Thread(target=self._recv_loop, name=f"fprobe-stream-{server.name}-recv", daemon=True)]

    def start(self) -> None:
        for t in self.threads:
            t.start()

    def put(self, data: bytes) -> None:
        with self.cond:
            if self.closed:
                return
            if self.queued + len(data) > self.server.queue_limit:
                self.dropped += len(data)                      # 読まない相手: 捨てて数える
                return
            self.queue.append(data)
            self.queued += len(data)
            self.cond.notify()

    def close(self) -> None:
        with self.cond:
            if self.closed:
                return
            self.closed = True
            self.cond.notify_all()
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.sock.close()
        self.server._forget(self)

    def _send_loop(self) -> None:
        while True:
            with self.cond:
                while not self.queue and not self.closed:
                    self.cond.wait()
                if self.closed:
                    return
                data = b"".join(self.queue)
                self.queue.clear()
                self.queued = 0
            try:
                self.sock.sendall(telnet_escape(data) if self.filter is not None else data)
            except OSError:
                self.close()
                return
            self.sent += len(data)

    def _recv_loop(self) -> None:
        while True:
            try:
                data = self.sock.recv(4096)
            except OSError:
                data = b""
            if not data:
                self.close()
                return
            if self.filter is not None:
                data = self.filter.feed(data)
            if data:
                self.received += len(data)
                self.server._input(data)

    def info(self) -> dict:
        return {"address": self.address, "sent": self.sent, "received": self.received, "dropped": self.dropped,
                "queued": self.queued}


class StreamServer:
    """流れ name を 127.0.0.1:port（0 なら空いているポート）で公開する。publish(data) で全員に配る。
    on_input が None なら読むだけの口（来たバイトは捨てて rejected に数える）。on_input の誤りは errors に残す。"""

    def __init__(self, name: str, port: int = 0, *, protocol: str = "raw",
                 on_input: Callable[[bytes], None] | None = None, host: str = "127.0.0.1",
                 queue_limit: int = QUEUE_LIMIT):
        if protocol not in PROTOCOLS:
            raise ValueError(f"protocol は {' / '.join(PROTOCOLS)}（{protocol!r}）")
        self.name, self.protocol, self.on_input, self.queue_limit = name, protocol, on_input, queue_limit
        self._lock = threading.Lock()
        self._peers: list[_Peer] = []
        self.published = 0
        self.rejected = 0
        self.connections = 0
        self.errors: collections.deque[str] = collections.deque(maxlen=10)
        self._input_lock = threading.Lock()                    # 相手が複数でも、書き込みは 1 つずつ（混ざらない）
        self._sock = socket.create_server((host, port))
        self.host, self.port = self._sock.getsockname()[:2]
        self._closed = False
        self._acceptor = threading.Thread(target=self._accept_loop, name=f"fprobe-stream-{name}", daemon=True)
        self._acceptor.start()

    def _accept_loop(self) -> None:
        while True:
            try:
                sock, address = self._sock.accept()
            except OSError:
                return                                         # 閉じた
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            peer = _Peer(self, sock, address)
            with self._lock:
                if self._closed:
                    sock.close()
                    return
                self._peers.append(peer)
                self.connections += 1
            peer.start()

    def _forget(self, peer: _Peer) -> None:
        with self._lock:
            if peer in self._peers:
                self._peers.remove(peer)

    def _input(self, data: bytes) -> None:
        if self.on_input is None:
            self.rejected += len(data)
            return
        try:
            with self._input_lock:
                self.on_input(data)
        except Exception as e:                                 # 書けない（RTT を始めていない等）: 口は保つ
            self.errors.append(f"{type(e).__name__}: {e}")

    def publish(self, data: bytes) -> None:
        """全員の送り待ちに入れる（待たない）。"""
        if not data:
            return
        self.published += len(data)
        with self._lock:
            peers = list(self._peers)
        for peer in peers:
            peer.put(data)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            peers = list(self._peers)
        self._sock.close()
        for peer in peers:
            peer.close()
        self._acceptor.join(timeout=2)

    def info(self) -> dict:
        with self._lock:
            peers = [p.info() for p in self._peers]
        return {"port": self.port, "address": self.host, "protocol": self.protocol,
                "writable": self.on_input is not None, "published": self.published, "connections": self.connections,
                "rejected": self.rejected, "errors": list(self.errors), "peers": peers}


class StreamLogger:
    """流れをファイルに書く。timestamps なら行の頭にこの PC の時刻（ISO 8601、ミリ秒）を付ける（文字の流れ用）。"""

    def __init__(self, path: str | Path, *, timestamps: bool = False, append: bool = True,
                 now: Callable[[], datetime] = datetime.now):
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.timestamps, self._now = timestamps, now
        self._file = open(self.path, "ab" if append else "wb")
        self._lock = threading.Lock()
        self._line_start = True
        self.written = 0

    def write(self, data: bytes) -> None:
        if self.timestamps:
            out = bytearray()
            stamp = f"[{self._now().isoformat(sep=' ', timespec='milliseconds')}] ".encode()
            for line in data.splitlines(keepends=True):
                if self._line_start:
                    out += stamp
                out += line
                self._line_start = line.endswith(b"\n")
            data = bytes(out)
        with self._lock:
            if self._file.closed:
                return
            self._file.write(data)
            self._file.flush()
            self.written += len(data)

    def close(self) -> None:
        with self._lock:
            self._file.close()

    def info(self) -> dict:
        return {"path": str(self.path), "timestamps": self.timestamps, "written": self.written}
