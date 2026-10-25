"""SEGGER RTT（Real-Time Transfer）の PC 側: 制御ブロックの探索、上り（ターゲット → PC）の読み出し、
下り（PC → ターゲット）の書き込み。

メモリ上の形式（公開の取り決め）だけに従い、SEGGER のソースは使わない。メモリの読み書きは抽象（MemoryAccess）で
受け取るので、pyOCD にも fprobe の Session にも依らない。読み書きの順序と回数は仕様書の 2.4 節の表のとおりで、
FPGA に移すときの参照になる。
仕様: docs/superpowers/specs/2026-10-17-f1-rtt-itm-semihosting-design.md の 2 節。

    rtt = Rtt(memory, rtt_address_from_elf("app.elf"))   # または find_control_block(memory, [(0x20000000, 0x18000)])
    rtt.open()
    rtt.poll()                    # {チャンネル: bytes}（データの来たチャンネルだけ）
    rtt.poll(wr_hint={0: 123})    # WrOff を別の手段（FPGA のサンプラ）で知っているとき: 記述子を読まない
    rtt.write(0, b"command\\n")    # 書けたバイト数
    rtt.stats()                   # 読み書きの回数・バイト数（速度の計測）

速さの工夫（U50）: 記述子の読みに推測読みを続ける（推測の長さは前回の量に合わせて伸び縮みする）、足りない区間は
チャンネルをまたいでまとめて読む（隙間が merge_gap 以下なら 1 回に）、WrOff が分かっていれば記述子を読まない。
"""

import struct
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path
from typing import Protocol

__all__ = ["ControlBlockNotFound", "CountingMemory", "MemoryAccess", "Rtt", "RttChannel", "RttControlBlock", "RttError",
           "RttMode", "coalesce", "find_control_block", "ring_free", "ring_spans", "ring_used", "rtt_address_from_elf"]

ID = b"SEGGER RTT\0"                  # 制御ブロックの先頭（16 バイトの欄の、NUL までの部分）
HEADER_SIZE = 24                      # ID（16）+ 上りの数（4）+ 下りの数（4）
DESCRIPTOR_SIZE = 24                  # 名前・リングの番地・大きさ・WrOff・RdOff・フラグ
WR_OFF = 12                           # 記述子の中の WrOff の位置
RD_OFF = 16                           # 記述子の中の RdOff の位置
NAME_LIMIT = 32                       # チャンネル名を読む長さの上限
COALESCE_LIMIT = 1 << 16              # まとめ読み 1 回の上限


class RttError(Exception):
    """制御ブロック・リングの値が壊れている、または使えないチャンネル。"""


class ControlBlockNotFound(RttError):
    pass


class MemoryAccess(Protocol):
    def read_memory(self, address: int, length: int) -> bytes: ...
    def write_memory(self, address: int, data: bytes) -> None: ...


class RttMode(IntEnum):
    """ターゲットが上りに書くとき、空きが足りない場合の振る舞い（フラグの下位 2 ビット）。"""
    NO_BLOCK_SKIP = 0          # その書き込みを丸ごと捨てる
    NO_BLOCK_TRIM = 1          # 入るだけ書いて残りを捨てる
    BLOCK_IF_FIFO_FULL = 2     # 空くまで待つ


# --- リング（FPGA の FSM と共有する定義） --------------------------------------------------

def ring_used(size: int, wr: int, rd: int) -> int:
    """読める（たまっている）バイト数。"""
    return (wr - rd) % size


def ring_free(size: int, wr: int, rd: int) -> int:
    """書けるバイト数（満杯と空を区別するため 1 バイトは空ける）。"""
    return size - 1 - ring_used(size, wr, rd)


def ring_spans(start: int, count: int, size: int) -> list[tuple[int, int]]:
    """リングの start から count バイトを、連続した (位置, 長さ) の区間（折り返せば 2 つ）に分ける。"""
    if count == 0:
        return []
    first = min(count, size - start)
    return [(start, first)] + ([(0, count - first)] if count > first else [])


def coalesce(spans: Iterable[tuple[int, int]], gap: int, limit: int = COALESCE_LIMIT) -> list[tuple[int, int]]:
    """読む区間 (番地, 長さ) を、隙間が gap 以下・全体が limit 以下ならつないだ、少ない回数の読みにする。"""
    out: list[tuple[int, int]] = []
    for address, length in sorted(s for s in spans if s[1] > 0):
        if out:
            first, n = out[-1]
            end = max(first + n, address + length)
            if address - (first + n) <= gap and end - first <= limit:
                out[-1] = (first, end - first)
                continue
        out.append((address, length))
    return out


class CountingMemory:
    """メモリの読み書きの回数とバイト数を数える（速度の計測。回数がデバッグポートの往復の数の目安）。"""

    def __init__(self, memory: MemoryAccess):
        self.memory = memory
        self.reads = self.writes = self.read_bytes = self.write_bytes = 0

    def read_memory(self, address: int, length: int) -> bytes:
        self.reads += 1
        self.read_bytes += length
        return self.memory.read_memory(address, length)

    def write_memory(self, address: int, data: bytes) -> None:
        self.writes += 1
        self.write_bytes += len(data)
        self.memory.write_memory(address, data)


# --- 制御ブロック ---------------------------------------------------------------------------

@dataclass
class RttChannel:
    """1 つのバッファ（チャンネル）。名前・リングは開くときに 1 度だけ読む。offset は PC が持つ位置
    （上りは RdOff、下りは WrOff。どちらも PC だけが書く欄）。"""
    index: int
    direction: str                  # "up"（ターゲット → PC）/ "down"（PC → ターゲット）
    name: str | None
    descriptor_address: int
    buffer_address: int
    size: int                       # 0 なら使われていない
    flags: int
    offset: int = 0
    resyncs: int = 0                # 別の書き手（他の道具・ターゲットの初期化し直し）に合わせて位置を取り直した回数
    expect: int = 0                 # 次の poll で来そうなバイト数（前回の量。推測読みの長さに使う）
    transferred: int = 0            # 読んだ（上り）・書いた（下り）バイト数の累計

    @property
    def mode(self) -> RttMode | int:
        mode = self.flags & 0x03
        return RttMode(mode) if mode in RttMode._value2member_map_ else mode

    @property
    def in_use(self) -> bool:
        return self.size > 0


@dataclass(frozen=True)
class RttControlBlock:
    address: int
    up: tuple[RttChannel, ...]
    down: tuple[RttChannel, ...]


def _check_descriptor(desc: bytes, where: str) -> tuple[int, int, int, int, int, int]:
    name, buf, size, wr, rd, flags = struct.unpack("<IIIIII", desc)
    if size == 0:
        return name, buf, size, wr, rd, flags
    if buf == 0:                                          # 初期化の途中（リングがまだ置かれていない）
        raise RttError(f"{where}: リングの番地が 0（大きさ {size}。ターゲットが RTT を初期化する前）")
    if buf + size > 1 << 32 or wr >= size or rd >= size:
        raise RttError(f"{where}: 壊れた記述子（番地 0x{buf:08X}、大きさ {size}、WrOff {wr}、RdOff {rd}）")
    return name, buf, size, wr, rd, flags


def _parse_header(header: bytes, address: int, end: int = 1 << 32) -> tuple[int, int]:
    """見出しの上り・下りの数。記述子の並びが end（制御ブロックが置かれうる範囲の終わり）に収まらなければ誤り
    （経験の上限は使わない。収まるかは事実）。"""
    if header[:len(ID)] != ID:
        raise RttError(f"0x{address:08X} に RTT の制御ブロックが無い（ID が違う）")
    num_up, num_down = struct.unpack_from("<II", header, 16)
    last = address + HEADER_SIZE + DESCRIPTOR_SIZE * (num_up + num_down)
    if last > end:
        raise RttError(f"0x{address:08X}: 上り {num_up}・下り {num_down} の記述子が範囲に収まらない"
                       f"（0x{last:X} まで要る。範囲の終わり 0x{end:X}）")
    return num_up, num_down


def _validate(memory: MemoryAccess, address: int, end: int) -> bool:
    """候補の番地の構造が制御ブロックとして成り立つか（見出しが範囲に収まり、記述子がどれも壊れていない）。"""
    try:
        num_up, num_down = _parse_header(memory.read_memory(address, HEADER_SIZE), address, end)
        descs = memory.read_memory(address + HEADER_SIZE, DESCRIPTOR_SIZE * (num_up + num_down))
        for i in range(0, len(descs), DESCRIPTOR_SIZE):
            _check_descriptor(descs[i:i + DESCRIPTOR_SIZE], "")
    except RttError:
        return False
    return True


def find_control_block(memory: MemoryAccess, ranges: Iterable[tuple[int, int]], chunk: int = 4096) -> int:
    """RAM の範囲（(番地, 長さ) の並び）を走査して制御ブロックの番地を返す。かたまりの境目をまたぐ ID も
    見つかるよう、読みを len(ID) - 1 バイト重ねる。候補は構造を検め（_validate）、成り立たないものは読み飛ばす。
    成り立つ候補が複数あれば選ばずに誤り（番地の一覧。address で指定する）。"""
    found_at: list[int] = []
    for start, length in ranges:
        end, pos, carry = start + length, start, b""
        while pos < end:
            n = min(chunk, end - pos)
            data = carry + memory.read_memory(pos, n)
            origin = pos - len(carry)
            found = data.find(ID)
            while found >= 0:
                if origin + found not in found_at and _validate(memory, origin + found, end):
                    found_at.append(origin + found)
                found = data.find(ID, found + 1)
            carry = data[-(len(ID) - 1):]
            pos += n
    if len(found_at) > 1:
        raise RttError("RTT の制御ブロックの候補が複数: " + "、".join(f"0x{a:08X}" for a in found_at)
                       + "（address で指定する）")
    if not found_at:
        raise ControlBlockNotFound("RTT の制御ブロックが見つからない（ターゲットが初期化する前か、範囲の外）")
    return found_at[0]


def rtt_address_from_elf(path: str | Path, symbol: str = "_SEGGER_RTT") -> int:
    """ELF のシンボルから制御ブロックの番地を引く（走査より速く確実）。無ければ KeyError。"""
    from .symbols import Symbols
    return Symbols(path).variable(symbol).address


# --- 読み書き -------------------------------------------------------------------------------

class Rtt:
    """制御ブロック 1 つ。open() の後、poll() / read() で上りを読み、write() で下りへ書く。

    speculate: poll() の最初の読み（記述子の配列）に続けて、各上りの読み出し位置から推測で読むバイト数の最小
    （前回の量が多ければそれまで伸ばす）。merge_gap: 読み足してよい隙間の上限（推測読み・まとめ読み）。
    max_read: 最初の読みの全体の上限。
    """

    def __init__(self, memory: MemoryAccess, address: int, *, speculate: int = 64, merge_gap: int = 256,
                 max_read: int = 4096, end: int = 1 << 32):
        self.memory = CountingMemory(memory)
        self.address = address
        self.end = end                                      # 制御ブロックが置かれうる範囲の終わり（記述子の数の検め）
        self.speculate, self.merge_gap, self.max_read = speculate, merge_gap, max_read
        self._cb: RttControlBlock | None = None
        self.polls = 0
        self.empty_polls = 0                                # 何も来なかった poll
        self.hinted_polls = 0                               # WrOff を教わって記述子を読まなかった poll
        self.reopens = 0                                    # 記述子のリングが変わっていたので開き直した回数

    def stats(self) -> dict:
        """読み書きの回数・バイト数と poll の数（累計）。"""
        m = self.memory
        cb = self._cb
        return {"polls": self.polls, "empty_polls": self.empty_polls, "hinted_polls": self.hinted_polls,
                "reads": m.reads, "writes": m.writes, "read_bytes": m.read_bytes, "write_bytes": m.write_bytes,
                "up_bytes": sum(c.transferred for c in cb.up) if cb else 0,
                "down_bytes": sum(c.transferred for c in cb.down) if cb else 0}

    def wroff_addresses(self) -> dict[int, int]:
        """使われている上りの WrOff の番地（チャンネル → 番地）。FPGA のサンプラに見張らせる。"""
        return {c.index: c.descriptor_address + WR_OFF for c in self.control_block.up if c.in_use}

    @property
    def control_block(self) -> RttControlBlock:
        if self._cb is None:
            raise RttError("open() の前")
        return self._cb

    def open(self) -> RttControlBlock:
        """制御ブロックを読む: 見出し（1 回）、記述子（1 回）、名前（名前のあるチャンネルごとに 1 回）。"""
        num_up, num_down = _parse_header(self.memory.read_memory(self.address, HEADER_SIZE), self.address, self.end)
        first = self.address + HEADER_SIZE
        descs = self.memory.read_memory(first, DESCRIPTOR_SIZE * (num_up + num_down))
        channels = []
        for i in range(num_up + num_down):
            up = i < num_up
            index = i if up else i - num_up
            where = f"{'上り' if up else '下り'} {index}"
            name_ptr, buf, size, wr, rd, flags = _check_descriptor(descs[DESCRIPTOR_SIZE * i:DESCRIPTOR_SIZE * (i + 1)],
                                                                   where)
            channels.append(RttChannel(index, "up" if up else "down", self._read_name(name_ptr),
                                       first + DESCRIPTOR_SIZE * i, buf, size, flags, offset=rd if up else wr))
        self._cb = RttControlBlock(self.address, tuple(channels[:num_up]), tuple(channels[num_up:]))
        return self._cb

    def _read_name(self, address: int) -> str | None:
        if address == 0:
            return None
        try:
            raw = bytes(self.memory.read_memory(address, NAME_LIMIT))
        except Exception:                                    # 領域の終わりの近く: 32 バイトは読めない。NUL まで 1 バイトずつ
            raw = b""
            while len(raw) < NAME_LIMIT and raw.find(0) < 0:
                try:
                    raw += bytes(self.memory.read_memory(address + len(raw), 1))
                except Exception:
                    break                                    # 読めた分までを名前とする（NUL が無くても open は続ける）
        return raw.split(b"\0", 1)[0].decode("utf-8", errors="replace")

    # --- 上り -----------------------------------------------------------------------------

    def poll(self, channels: Iterable[int] | None = None, wr_hint: dict[int, int] | None = None) -> dict[int, bytes]:
        """上りを読む（既定は全チャンネル）。データの来たチャンネルだけを返す。

        手順: R1 記述子の配列（+ 後ろに続く推測読み）を 1 回で読む → R2 足りないデータを、チャンネルをまたいで
        まとめて読む → W1 RdOff を書く（チャンネルごと。間の WrOff はターゲットの欄なので一緒には書けない）。
        wr_hint（チャンネル → WrOff）を渡すと R1 を省き、その位置まで読む（FPGA のサンプラが WrOff を読んでいるとき。
        WrOff はデータを書いた後に進むので、少し古い値でもそこまでのデータは書き終わっている）。このときは RdOff を
        読まないので、別の読み手に合わせた位置の取り直し（resyncs）は次の R1 のある poll まで遅れる。
        """
        cb = self.control_block
        wanted = [c for c in (cb.up if channels is None else (self._channel(cb.up, i) for i in channels)) if c.in_use]
        self.polls += 1
        if wr_hint is not None:
            self.hinted_polls += 1
            start, window = 0, b""
            positions = {c.index: (wr_hint[c.index], c.offset) for c in wanted if c.index in wr_hint}
        elif wanted:
            start = cb.up[0].descriptor_address
            window = self.memory.read_memory(start, self._window_length(start, len(cb.up), wanted))
            for c in wanted:                                 # ターゲットが RTT を初期化し直した: 古い番地を読まない
                buf, size = struct.unpack_from("<II", window, c.descriptor_address - start + 4)
                if (buf, size) != (c.buffer_address, c.size):
                    try:
                        self.open()
                        self.reopens += 1
                    except RttError:                         # まだ初期化の途中: 次の poll でもう一度
                        pass
                    return {}
            positions = {c.index: struct.unpack_from("<II", window, c.descriptor_address - start + WR_OFF)
                         for c in wanted}
        else:
            positions = {}
        plans: list[tuple[RttChannel, int, list[tuple[int, int]]]] = []
        for c in wanted:
            if c.index not in positions:
                continue
            wr, rd = positions[c.index]
            if wr >= c.size or rd >= c.size:
                raise RttError(f"上り {c.index}: 壊れた位置（WrOff {wr}、RdOff {rd}、大きさ {c.size}）")
            if rd != c.offset:
                c.offset, c.resyncs = rd, c.resyncs + 1
            count = ring_used(c.size, wr, c.offset)
            c.expect = count
            if count:
                plans.append((c, wr, [(c.buffer_address + pos, length)
                                      for pos, length in ring_spans(c.offset, count, c.size)]))
        if not plans:
            self.empty_polls += 1
            return {}
        fetched = self._fetch([span for _, _, spans in plans for span in spans], window, start)
        out = {}
        for c, wr, spans in plans:
            out[c.index] = b"".join(fetched[span] for span in spans)
            c.offset = wr
            c.transferred += len(out[c.index])
            self.memory.write_memory(c.descriptor_address + RD_OFF, struct.pack("<I", wr))
        return out

    def read(self, channel: int) -> bytes:
        """上りの 1 チャンネルを読む（来ていなければ空）。"""
        return self.poll([channel]).get(channel, b"")

    def _window_length(self, start: int, num_up: int, wanted: Sequence[RttChannel]) -> int:
        """R1 の長さ: 記述子の配列に、後ろに続く推測読みの区間を隙間・全体の上限の範囲で足す。
        記述子より前のリングは足さない（同じ 1 回の読みではデータを WrOff より先に読んでしまうため）。"""
        end = start + DESCRIPTOR_SIZE * num_up
        spans = sorted((c.buffer_address + c.offset, min(max(self.speculate, c.expect), c.size - c.offset))
                       for c in wanted)
        for addr, length in spans:
            if length <= 0 or addr < start + DESCRIPTOR_SIZE * num_up:
                continue
            if addr - end > self.merge_gap or max(end, addr + length) - start > self.max_read:
                break
            end = max(end, addr + length)
        return end - start

    def _fetch(self, spans: list[tuple[int, int]], window: bytes, start: int) -> dict[tuple[int, int], bytes]:
        """区間ごとの中身: window（start から）に含まれる先頭部分はそこから取り、残りはまとめて読む。"""
        def covered(address: int, length: int) -> int:
            return max(0, min(length, start + len(window) - address)) if window and address >= start else 0
        rest = [(a + covered(a, n), n - covered(a, n)) for a, n in spans]
        chunks = [(a, self.memory.read_memory(a, n)) for a, n in coalesce(rest, self.merge_gap)]
        out = {}
        for (address, length), (ra, rn) in zip(spans, rest):
            head = window[address - start:ra - start] if ra > address else b""
            tail = b""
            if rn:
                base, data = next((a, d) for a, d in chunks if a <= ra and ra + rn <= a + len(d))
                tail = data[ra - base:ra - base + rn]
            out[(address, length)] = head + tail
        return out

    # --- 下り -----------------------------------------------------------------------------

    def write(self, channel: int, data: bytes) -> int:
        """下りへ書く（待たない）。書けたバイト数を返す（空きが足りなければ一部だけ）。

        手順: D1 WrOff・RdOff を読む → D2 データを書く → D3 WrOff を書く（データの後）。
        """
        c = self._channel(self.control_block.down, channel)
        if not c.in_use:
            raise RttError(f"下り {channel} は使われていない")
        wr, rd = struct.unpack("<II", self.memory.read_memory(c.descriptor_address + WR_OFF, 8))
        if wr >= c.size or rd >= c.size:
            raise RttError(f"下り {channel}: 壊れた位置（WrOff {wr}、RdOff {rd}、大きさ {c.size}）")
        if wr != c.offset:
            c.offset, c.resyncs = wr, c.resyncs + 1
        count = min(len(data), ring_free(c.size, c.offset, rd))
        if count == 0:
            return 0
        done = 0
        for pos, length in ring_spans(c.offset, count, c.size):
            self.memory.write_memory(c.buffer_address + pos, bytes(data[done:done + length]))
            done += length
        c.offset = (c.offset + count) % c.size
        c.transferred += count
        self.memory.write_memory(c.descriptor_address + WR_OFF, struct.pack("<I", c.offset))
        return count

    def write_all(self, channel: int, data: bytes, timeout_s: float = 1.0, poll_s: float = 0.001,
                  wait: Callable[[float], None] = time.sleep) -> int:
        """すべて書けるまで（ターゲットが読んで空くまで）繰り返す。時間切れなら書けたバイト数を返す。"""
        deadline, done = time.monotonic() + timeout_s, 0
        while True:
            done += self.write(channel, data[done:])
            if done == len(data) or time.monotonic() >= deadline:
                return done
            wait(poll_s)

    @staticmethod
    def _channel(channels: tuple[RttChannel, ...], index: int) -> RttChannel:
        if not 0 <= index < len(channels):
            raise RttError(f"チャンネル {index} は無い（{len(channels)} 個）")
        return channels[index]
