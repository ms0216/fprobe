"""最小の RSP クライアント（GDB の代わり）。GDB サーバの試験と実機試験で使う。

    with RspClient("127.0.0.1", 3333) as gdb:
        gdb.request("qSupported:swbreak+;hwbreak+;error-message+")
        regs = gdb.request("g")
        gdb.monitor("rtos.tasks")             # → (コンソールの出力, 応答)
"""

import socket
import time

from . import rsp

__all__ = ["RspClient"]


class RspClient:
    def __init__(self, host: str, port: int, timeout_s: float = 10.0):
        self.sock = socket.create_connection((host, port), timeout=timeout_s)
        self.reader = rsp.PacketReader()
        self.pending: list = []
        self.ack = True
        self.console: list[bytes] = []                  # 受け取った O パケットの中身

    def close(self) -> None:
        self.sock.close()

    def __enter__(self) -> "RspClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def send(self, payload: str | bytes) -> None:
        self.sock.sendall(rsp.frame(payload.encode("latin-1") if isinstance(payload, str) else payload))

    def interrupt(self) -> None:
        self.sock.sendall(b"\x03")

    def _event(self, timeout_s: float | None = None):
        deadline = None if timeout_s is None else time.monotonic() + timeout_s
        while not self.pending:
            if deadline is not None:
                self.sock.settimeout(max(0.001, deadline - time.monotonic()))
            data = self.sock.recv(65536)
            if not data:
                raise ConnectionError("サーバが切断した")
            self.pending += self.reader.feed(data)
        return self.pending.pop(0)

    def receive(self, timeout_s: float | None = None) -> bytes:
        """次の応答のパケット（O パケットは console に貯めて飛ばす）。"""
        while True:
            event = self._event(timeout_s)
            if isinstance(event, rsp.Packet):
                if self.ack:
                    self.sock.sendall(b"+")
                if event.payload[:1] == b"O" and event.payload != b"OK":
                    self.console.append(bytes.fromhex(event.payload[1:].decode()))
                    continue
                return event.payload
            if event is rsp.CORRUPT:
                raise ValueError("検査和の合わないパケット")

    def request(self, payload: str | bytes, timeout_s: float | None = None) -> str:
        self.send(payload)
        return self.receive(timeout_s).decode("latin-1")

    def no_ack(self) -> None:
        assert self.request("QStartNoAckMode") == "OK"
        self.ack = False

    def monitor(self, command: str) -> tuple[str, str]:
        """(コンソールの出力, 応答)。"""
        self.console.clear()
        reply = self.request("qRcmd," + command.encode("utf-8").hex())
        return b"".join(self.console).decode("utf-8"), reply

    def read_memory(self, address: int, length: int) -> bytes:
        return bytes.fromhex(self.request(f"m{address:x},{length:x}"))

    def xfer(self, obj: str, annex: str = "") -> str:
        """qXfer の読みを最後まで。"""
        out, offset = b"", 0
        while True:
            reply = self.request(f"qXfer:{obj}:read:{annex}:{offset:x},400").encode("latin-1")
            out += reply[1:]
            offset += len(reply) - 1
            if reply[:1] == b"l":
                return out.decode("utf-8")
            if reply[:1] != b"m":
                raise ValueError(f"qXfer:{obj}: {reply!r}")
