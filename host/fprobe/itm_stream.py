"""解読した ITM のパケットの使い道（ITM の解読は itm_decode）: 時刻の付与（Timestamper）とポートごとの行（PortText）。

    timed = Timestamper().feed(packets) # ローカル・グローバルのタイムスタンプを付ける
    lines = PortText().feed(packets)    # ポートごとの printf の行

入出力を持たない。
"""

from collections.abc import Iterable
from dataclasses import dataclass

from .itm_decode import (GlobalTimestamp1, GlobalTimestamp2, Instrumentation, LocalTimestamp, Overflow, Packet,
                         TimestampRelation)

__all__ = ["PortText", "TextLine", "TimedPacket", "Timestamper"]


# --- 時刻の付与 -------------------------------------------------------------------------

@dataclass(frozen=True)
class TimedPacket:
    packet: Packet
    local_time: int | None           # ローカルのタイムスタンプの累積（最初のタイムスタンプからのクロック数）
    global_time: int | None          # グローバルのタイムスタンプ（上位が分からなければ None）
    after_overflow: bool = False     # 同じ区切りにオーバーフローがあった（時刻の基準が怪しい）
    time_relation: "TimestampRelation | None" = None   # ローカルの時刻の TC（SYNC 以外は時刻が遅れている）。時刻が未定なら None


class Timestamper:
    """ローカルのタイムスタンプは、それが計る出来事のパケットの後に来る。区切りまでのパケットをためて、
    タイムスタンプが来たらその累積時刻を付けて吐き出す。タイムスタンプのパケット自身は吐き出さない。"""

    def __init__(self):
        self._pending: list[Packet] = []
        self._local = 0
        self._overflow = False
        self._relation: TimestampRelation | None = None
        self._low = 0                    # グローバルの下位 26 ビット
        self._high: int | None = None    # グローバルの上位（ビット 26 から上）

    @property
    def global_time(self) -> int | None:
        return None if self._high is None else self._high << 26 | self._low

    def feed(self, packets: Iterable[Packet]) -> list[TimedPacket]:
        out: list[TimedPacket] = []
        for p in packets:
            if isinstance(p, LocalTimestamp):
                self._local += p.delta
                self._relation = p.relation
                out += self._release(self._local)
            elif isinstance(p, GlobalTimestamp1):
                mask = (1 << p.bits) - 1
                self._low = (self._low & ~mask) | p.value
                if p.wrap or p.clock_change:
                    self._high = None        # 次の GlobalTimestamp2 まで上位は分からない
            elif isinstance(p, GlobalTimestamp2):
                self._high = p.value
            else:
                self._overflow |= isinstance(p, Overflow)
                self._pending.append(p)
        return out

    def flush(self) -> list[TimedPacket]:
        """最後のタイムスタンプより後のパケット（ローカルの時刻は未定）。"""
        return self._release(None)

    def _release(self, local: int | None) -> list[TimedPacket]:
        relation = None if local is None else self._relation
        out = [TimedPacket(p, local, self.global_time, self._overflow, relation) for p in self._pending]
        self._pending, self._overflow = [], False
        return out


# --- ポートごとの文字列 ---------------------------------------------------------------------

@dataclass(frozen=True)
class TextLine:
    port: int
    text: str


class PortText:
    """スティミュラス・ポートに書かれたバイトを、ポートごとに改行で区切った行にする（printf 用）。"""

    def __init__(self, ports: Iterable[int] | None = None):
        self._ports = None if ports is None else frozenset(ports)
        self._buffers: dict[int, bytearray] = {}

    def feed(self, packets: Iterable[Packet]) -> list[TextLine]:
        out: list[TextLine] = []
        for p in packets:
            if not isinstance(p, Instrumentation) or (self._ports is not None and p.port not in self._ports):
                continue
            buf = self._buffers.setdefault(p.port, bytearray())
            buf += p.data
            while (end := buf.find(b"\n")) >= 0:
                out.append(TextLine(p.port, _text(buf[:end])))
                del buf[:end + 1]
        return out

    def pending(self, port: int) -> str:
        """まだ改行の来ていない途中の文字列。"""
        return _text(self._buffers.get(port, b""))


def _text(data: bytes | bytearray) -> str:
    return bytes(data).rstrip(b"\r").decode("utf-8", errors="replace")
