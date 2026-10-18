"""CONTROL チャンネル（CSR の READ / WRITE）の符号化と送受信。

仕様: docs/superpowers/specs/2026-10-15-m3-control-plane-design.md §3
"""

import struct
from collections.abc import Sequence
from typing import Protocol

from . import generated as g

__all__ = ["ControlChannel", "ControlError", "MAX_READ_WORDS", "ProtocolError", "STATUS_NAMES",
           "Transport", "decode_response", "encode_exchange", "encode_read", "encode_write"]

MAX_READ_WORDS = 1024        # 1 要求で読む上限（応答の大きさを抑える）

STATUS_NAMES = {g.STATUS_OK: "OK", g.STATUS_BAD_OP: "BAD_OP", g.STATUS_BAD_LENGTH: "BAD_LENGTH",
                g.STATUS_BUS_ERROR: "BUS_ERROR"}


class Transport(Protocol):
    """要求 1 つを 1 パケットで送り、応答 1 つ（短パケットまで）を受け取る。"""
    max_packet: int

    def send(self, message: bytes) -> None: ...

    def receive(self, max_length: int) -> bytes: ...

    def flush(self) -> None:
        """届いている応答の残りを読み捨てる（失敗した要求の後始末）。"""


class ProtocolError(RuntimeError):
    """応答の形が仕様と違う（tag 不一致・長さ不正など）。"""


class ControlError(RuntimeError):
    """デバイスがエラー状態を返した。address は失敗したワードのアドレス。"""

    def __init__(self, status: int, address: int, done: int):
        self.status, self.address, self.done = status, address, done
        super().__init__(f"{STATUS_NAMES.get(status, status)} at 0x{address:08X}（{done} ワード処理済み）")


def encode_read(tag: int, address: int, count: int) -> bytes:
    return struct.pack("<BBHI", g.OP_READ, tag, count, address)


def encode_write(tag: int, address: int, words: Sequence[int]) -> bytes:
    return struct.pack(f"<BBHI{len(words)}I", g.OP_WRITE, tag, len(words), address, *words)


def encode_exchange(tag: int, address: int, words: Sequence[int]) -> bytes:
    """EXCHANGE（プロトコル 2）: 同じ番地に語を順に書き、書くごとにその番地を読む。応答は読んだ語の列。"""
    return struct.pack(f"<BBHI{len(words)}I", g.OP_EXCHANGE, tag, len(words), address, *words)


def decode_response(data: bytes) -> tuple[int, int, int, list[int]]:
    """(tag, status, done, words)。応答は READ のデータの後に末尾 4 バイトの状態。"""
    size = len(data) - g.RESPONSE_TRAILER_BYTES
    if size < 0 or size % 4:
        raise ProtocolError(f"応答の長さが不正: {len(data)} バイト")
    words = list(struct.unpack_from(f"<{size // 4}I", data))
    tag, status, done = struct.unpack_from("<BBH", data, size)
    return tag, status, done, words


class ControlChannel:
    def __init__(self, transport: Transport):
        self._transport = transport
        self._tag = 0

    def _next_tag(self) -> int:
        self._tag = (self._tag + 1) & 0xFF
        return self._tag

    def _transact(self, request: bytes, count: int, address: int) -> list[int]:
        """要求を送り、応答を検査して READ のデータを返す（WRITE は空）。

        応答の形が崩れていたら（または送受信が失敗したら）残りを読み捨て、次の要求が古い応答を
        読まないようにしてから例外を伝える。"""
        try:
            return self._exchange(request, count, address)
        except ControlError:
            raise
        except Exception:
            self._transport.flush()
            raise

    def _exchange(self, request: bytes, count: int, address: int) -> list[int]:
        reading = request[0] in (g.OP_READ, g.OP_EXCHANGE)
        self._transport.send(request)
        tag, status, done, words = decode_response(
            self._transport.receive((4 * count if reading else 0) + g.RESPONSE_TRAILER_BYTES))
        if tag != request[1]:
            raise ProtocolError(f"tag 不一致: 送信 {request[1]}, 応答 {tag}")
        if len(words) != (done if reading else 0):
            raise ProtocolError(f"データ {len(words)} ワードが done={done} と合わない")
        if status != g.STATUS_OK:
            fixed = request[0] == g.OP_EXCHANGE                 # EXCHANGE は番地を進めない
            raise ControlError(status, address if fixed else address + 4 * done, done)
        if done != count:
            raise ProtocolError(f"OK なのに {done}/{count} ワード")
        return words

    def read(self, address: int, count: int) -> list[int]:
        words: list[int] = []
        while len(words) < count:
            n = min(count - len(words), MAX_READ_WORDS)
            at = address + 4 * len(words)
            words += self._transact(encode_read(self._next_tag(), at, n), n, at)
        return words

    def _words_per_request(self) -> int:
        return (self._transport.max_packet - g.REQUEST_HEADER_BYTES) // 4

    def write(self, address: int, words: Sequence[int]) -> None:
        per_request = self._words_per_request()
        for i in range(0, len(words), per_request):
            chunk = list(words[i:i + per_request])
            at = address + 4 * i
            self._transact(encode_write(self._next_tag(), at, chunk), len(chunk), at)

    def exchange(self, address: int, words: Sequence[int]) -> list[int]:
        """同じ番地に語を順に書き、書くごとにその番地を読んだ値の列を返す（プロトコル 2。要求の大きさで分ける）。
        読みは書きの直後（同じ要求の中で途切れない。要求の境目では USB の往復の分だけ間が空く）。"""
        per_request = self._words_per_request()
        out: list[int] = []
        for i in range(0, len(words), per_request):
            chunk = list(words[i:i + per_request])
            out += self._transact(encode_exchange(self._next_tag(), address, chunk), len(chunk), address)
        return out
