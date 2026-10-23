"""道 B のリング: エージェントが物理メモリに書く出来事の輪の形と、その読み（PC 側）。

頭（64 バイト、リトルエンディアン）: magic b"FPRB"・version u32・record_size u32・capacity u32・head u64（次に書く seq）・
clock u32（1 = CLOCK_MONOTONIC_RAW）・0 u32・first u64（リングの最初の記録の seq。エージェントの出来事の通しの番号で、
道 A と同じ番号）・crc u32（先の 40 バイトの CRC-32。書き手は head を変えるたびに書き直す）・残りは 0。
頭の CRC が合わなければ、頭を読んだ値を使わない（書き換えの途中・読みで壊れた）。seq の記録は slot (seq − first) % capacity にある。
記録（48 バイト）: seq u64・t_ns u64・tid u32・bp u16・kind u16（1 = bp）・regs u32 × 5（r0〜r3・lr）・crc u32
（先の 44 バイトの CRC-32。zlib と同じ）。
書き手の順: seq を全部 1 に → 中身 → crc → seq = n → head = n + 1（それぞれの間にバリア）。
読み手は、番号の合わない記録（書きかけ・前の周の残り）を torn、番号は合うが CRC が合わない記録（読みで壊れた等）を
corrupt、追い越されて読めなかった範囲を lost として、事実のまま返す（推測で埋めない）。"""
from __future__ import annotations

import struct
import zlib
from collections.abc import Callable

MAGIC = b"FPRB"
VERSION = 1
HEADER = 64
RECORD = 48
KIND_BP = 1
CLOCKS = {1: "CLOCK_MONOTONIC_RAW"}
WRITING = 0xFFFF_FFFF_FFFF_FFFF
_HEAD = struct.Struct("<4sIIIQIIQ")
_REC = struct.Struct("<QQIHH5I")


class RingError(ValueError):
    """リングの頭が違う（magic・版・記録の大きさ）"""


def encode_header(capacity: int, head: int, clock: int = 1, first: int = 0) -> bytes:
    fields = _HEAD.pack(MAGIC, VERSION, RECORD, capacity, head, clock, 0, first)
    return (fields + struct.pack("<I", zlib.crc32(fields))).ljust(HEADER, b"\0")


def parse_header(data: bytes) -> dict:
    magic, version, size, capacity, head, clock, _, first = _HEAD.unpack_from(data)
    if magic != MAGIC:
        raise RingError(f"リングの magic が違う（{magic!r}。番地が違うか、まだ開いていない）")
    if version != VERSION:                                   # 版が違えば頭の形も違いうる（CRC より先に見る）
        raise RingError(f"リングの版が違う（{version}。読めるのは {VERSION}）")
    if struct.unpack_from("<I", data, _HEAD.size)[0] != zlib.crc32(data[:_HEAD.size]):
        raise RingError("リングの頭の CRC が合わない（書き換えの途中か、読みで壊れた。この読みの頭は使わない）")
    if size != RECORD or capacity == 0:
        raise RingError(f"記録の大きさ {size}・数 {capacity} が違う")
    if head < first:
        raise RingError(f"head {head} が first {first} より前（頭が書きかけか壊れている）")
    return {"version": version, "record_size": size, "capacity": capacity, "head": head, "first": first,
            "clock": CLOCKS.get(clock, f"unknown({clock})")}


def encode_record(seq: int, t_ns: int, tid: int, bp: int, regs: list[int], kind: int = KIND_BP) -> bytes:
    body = _REC.pack(seq, t_ns, tid, bp, kind, *(list(regs) + [0] * 5)[:5])
    return body + struct.pack("<I", zlib.crc32(body))


class RingReader:
    """前回の続き（next_seq）から読む。capacity・first は頭の値。"""

    def __init__(self, capacity: int, first: int = 0):
        self.capacity, self.first = capacity, first
        self.next_seq = first

    def take(self, head: int, slots: Callable[[int, int], bytes], limit: int | None = None) -> dict:
        """head（頭から読んだ値）までの新しい記録を、古い方から limit 個まで読む（残りの数は left）。
        slots(最初の slot, 数) は記録の生のバイト。"""
        out = {"records": [], "lost": [], "torn": [], "corrupt": [], "head": head, "left": 0}
        first = max(self.next_seq, head - self.capacity)
        if first > self.next_seq:
            out["lost"].append([self.next_seq, first])
        count = head - first
        if limit is not None and count > limit:
            out["left"], count = count - limit, limit
        end = first + max(count, 0)
        if count > 0:
            raw = b""
            start = (first - self.first) % self.capacity
            while len(raw) < count * RECORD:                         # 輪の端で 2 回に分けて読む
                n = min(count - len(raw) // RECORD, self.capacity - start)
                raw += slots(start, n)
                start = 0
            for i in range(count):
                self._check(first + i, raw[i * RECORD:(i + 1) * RECORD], out)
        self.next_seq = max(self.next_seq, end)
        return out

    @staticmethod
    def _check(seq: int, raw: bytes, out: dict) -> None:
        got, t_ns, tid, bp, kind, *regs = _REC.unpack_from(raw)
        if got != seq:
            out["torn"].append(seq)                                  # 書きかけ・前の周の残り
        elif struct.unpack_from("<I", raw, 44)[0] != zlib.crc32(raw[:44]):
            out["corrupt"].append(seq)                               # 番号は合うが中身が合わない
        else:
            out["records"].append({"seq": seq, "t_ns": t_ns, "tid": tid, "bp": bp, "kind": kind, "regs": regs})
