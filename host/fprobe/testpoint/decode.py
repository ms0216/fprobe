"""テストポイントの解読: GPIO（並列・2 線、ロジアナの Transitions）・ITM（パケット）・RTT（バイト列）→ Hit / Loss。

どの解読器も逐次（feed を何度呼んでもよく、かたまりの境目で記録が切れてよい）で、最後に flush する。
仕様: docs/superpowers/specs/2026-10-17-f7-testpoint-design.md の 3・4 節。
"""

import math
import struct
from collections.abc import Iterable, Sequence

from ..itm import DecodeError, Instrumentation, Overflow, Packet, Sync, TimedPacket
from ..la import Transitions
from .trace import Decoded, Hit, Item, Loss

__all__ = ["ItmDecoder", "ParallelDecoder", "RttDecoder", "SerialDecoder", "decode_gpio_parallel",
           "decode_gpio_serial", "decode_itm", "decode_rtt"]

ID_BITS, VALUE_BITS = 16, 32
RTT_MARK = 0xA500


def _record(bits: int, nbits: int) -> tuple[int, int | None] | None:
    """左詰めの nbits ビット → (ID, 値)。長さが ID だけ・値付きのどちらでもなければ None。"""
    if nbits == ID_BITS:
        return bits, None
    if nbits == ID_BITS + VALUE_BITS:
        return bits >> VALUE_BITS, bits & 0xFFFFFFFF
    return None


class ParallelDecoder:
    """並列: ストローブの立ち上がりのサンプルでデータを取る。data_channels は D0..D(W-1) のロジアナの ch
    （最後が先頭の印）。記録の時刻は最初の記号の立ち上がりのサンプル（base_sample からの通し番号）。"""

    def __init__(self, data_channels: Sequence[int], strobe_channel: int):
        if len(data_channels) < 2:
            raise ValueError("データは 2 本以上（先頭の印 + 中身 1 ビット以上）")
        self.data_channels = list(data_channels)
        self.strobe = strobe_channel
        self.payload = len(data_channels) - 1
        self._id_symbols = math.ceil(ID_BITS / self.payload)
        self._value_symbols = math.ceil((ID_BITS + VALUE_BITS) / self.payload)
        self._strobe_level: int | None = None
        self._symbols: list[int] | None = None       # 組み立て中の記録（None = 先頭の印を待つ）
        self._t0 = 0
        self.unsynced = 0                            # 先頭の印より前に来た記号（捕捉が記録の途中から始まった）

    def feed(self, tr: Transitions, base_sample: int = 0) -> list[Item]:
        out: list[Item] = []
        strobe_bit = 1 << self.strobe
        for v, s in zip(tr.values, tr.starts):
            level = 1 if v & strobe_bit else 0
            if self._strobe_level == 0 and level == 1:
                self._symbol(v, base_sample + s, out)
            self._strobe_level = level
        return out

    def _symbol(self, v: int, t: int, out: list[Item]) -> None:
        data = 0
        for k, ch in enumerate(self.data_channels):
            data |= (v >> ch & 1) << k
        payload = data & ((1 << self.payload) - 1)
        if data >> self.payload & 1:
            self._finish(out)
            self._symbols, self._t0 = [payload], t
            return
        if self._symbols is None:
            self.unsynced += 1
            return
        self._symbols.append(payload)
        if len(self._symbols) == self._value_symbols:
            self._finish(out)

    def _finish(self, out: list[Item]) -> None:
        symbols, self._symbols = self._symbols, None
        if not symbols:
            return
        n = len(symbols)
        bits = 0
        for sym in symbols:
            bits = bits << self.payload | sym
        nbits = ID_BITS if n == self._id_symbols else ID_BITS + VALUE_BITS if n == self._value_symbols else 0
        rec = _record(bits >> (n * self.payload - nbits), nbits) if nbits else None
        if rec is None:
            out.append(Loss(self._t0, f"並列の記録の長さが合わない（{n} 記号）"))
        else:
            out.append(Hit(self._t0, *rec))

    def flush(self) -> list[Item]:
        out: list[Item] = []
        self._finish(out)
        return out


class SerialDecoder:
    """2 線: CLK = 1 の間の DAT の立ち下がり = 開始、立ち上がり = 終了、CLK の立ち上がりで DAT を取る。
    開始・終了は、その後 guard サンプル CLK が 1 のままのときだけ認める（CLK の立ち下がりと同じ書き込みの DAT の
    変化が、ピンのずれで先に見えても誤らない）。記録の時刻は開始のサンプル。"""

    def __init__(self, clock_channel: int, data_channel: int, guard: int):          # guard は使う側が決める（既定なし）
        self.clock, self.data, self.guard = clock_channel, data_channel, guard
        self._clk: int | None = None
        self._dat: int | None = None
        self._pending: tuple[str, int] | None = None     # 確かめ中の開始・終了 (種類, サンプル)
        self._bits: list[int] | None = None              # 組み立て中（None = 開始を待つ）
        self._t0 = 0

    def feed(self, tr: Transitions, base_sample: int = 0) -> list[Item]:
        out: list[Item] = []
        for v, s in zip(tr.values, tr.starts):
            t = base_sample + s
            clk, dat = v >> self.clock & 1, v >> self.data & 1
            if self._pending is not None:
                if t > self._pending[1] + self.guard:
                    self._condition(out)
                elif clk == 0:
                    self._pending = None                 # CLK がすぐ落ちた: データの変化（ずれ）
            if self._clk is not None:
                if self._clk == 0 and clk == 1:
                    if self._bits is not None:
                        self._bits.append(dat)
                elif self._clk == 1 and clk == 1 and dat != self._dat:
                    self._pending = ("start" if dat == 0 else "stop", t)
            self._clk, self._dat = clk, dat
        if self._pending is not None and base_sample + tr.length - 1 >= self._pending[1] + self.guard:
            self._condition(out)                         # かたまりの終わりまで CLK は 1 のまま
        return out

    def _condition(self, out: list[Item]) -> None:
        kind, t = self._pending
        self._pending = None
        if kind == "start":
            if self._bits is not None:
                out.append(Loss(self._t0, "2 線の記録に終了が無い"))
            self._bits, self._t0 = [], t
            return
        bits, self._bits = self._bits, None
        if bits is None:
            return                                       # 開始を見ていない（捕捉が途中から）
        rec = None
        if bits and bits[-1] == 0:
            value = 0
            for b in bits[:-1]:
                value = value << 1 | b
            rec = _record(value, len(bits) - 1)
        if rec is None:
            out.append(Loss(self._t0, f"2 線の記録の長さ・終わりのビットが合わない（{len(bits)} ビット）"))
        else:
            out.append(Hit(self._t0, *rec))

    def flush(self) -> list[Item]:
        out: list[Item] = []
        if self._pending is not None:
            self._condition(out)
        if self._bits is not None:
            out.append(Loss(self._t0, "2 線の記録に終了が無い"))
            self._bits = None
        return out


class ItmDecoder:
    """ITM: スティミュラス・ポート port の 4 バイトの書き込みを値として預かり、次の 2 バイトの書き込み（ID）に付ける。

    入力は `itm.Packet`（時刻なし: 直前の時刻を使う）、`itm.TimedPacket`（ローカルのタイムスタンプの累積）、
    `(時刻, Packet)`（FPGA が SWO のバイトに付けた時刻など）。解読の誤り（DecodeError）と、バイトを捨てた同期の
    取り直し（Sync.skipped）は Loss（誤りから同期までで 1 回）。"""

    def __init__(self, port: int = 1):
        self.port = port
        self._value: int | None = None
        self._t = 0
        self._lost = False                               # 解読の誤りの後、同期の取り直しを待っている（Loss は 1 回）

    def feed(self, items: Iterable[Packet | TimedPacket | tuple[int, Packet]]) -> list[Item]:
        out: list[Item] = []
        for item in items:
            if isinstance(item, TimedPacket):
                packet = item.packet
                if item.local_time is not None:
                    self._t = item.local_time
            elif isinstance(item, tuple):
                self._t, packet = item
            else:
                packet = item
            if isinstance(packet, Overflow):
                out.append(Loss(self._t, "ITM のオーバーフロー"))
                self._value = None
            elif isinstance(packet, DecodeError) or (isinstance(packet, Sync) and packet.skipped):
                if not isinstance(packet, Sync) or not self._lost:
                    out.append(Loss(self._t, "SWO のバイトを解読できない（同期を取り直す）"))
                self._lost = isinstance(packet, DecodeError)
                self._value = None
            elif isinstance(packet, Instrumentation) and packet.port == self.port:
                if packet.size == 4:
                    if self._value is not None:
                        out.append(Loss(self._t, "ITM の値の後に ID が無い"))
                    self._value = packet.value
                elif packet.size == 2:
                    out.append(Hit(self._t, packet.value, self._value))
                    self._value = None
                else:
                    out.append(Loss(self._t, "ITM のテストポイントのポートに 1 バイトの書き込み"))
        return out

    def flush(self) -> list[Item]:
        out: list[Item] = []
        if self._value is not None:
            out.append(Loss(self._t, "ITM の値の後に ID が無い"))
            self._value = None
        return out


class RttDecoder:
    """RTT: u32 CYCCNT, u16 ID, u16 flags（上位 8 ビット = 0xA5 の印、bit0 = 値付き）[, u32 値]。CYCCNT の回り込みを
    戻す（記録の間が 2^32 クロックより短い前提）。前提で戻した回数は wraps（前提が外れると、その回数のどこかで時刻が
    2^32 の倍数ずれる）。印が合わなければ 1 バイトずつずらして同期を取り直す（Loss は 1 回）。"""

    def __init__(self):
        self._buf = bytearray()
        self._high = 0
        self._last: int | None = None
        self._lost = False
        self.wraps = 0                               # CYCCNT が減ったのを 1 周の回り込みとみなした回数

    def feed(self, data: bytes) -> list[Item]:
        out: list[Item] = []
        self._buf += data
        while len(self._buf) >= 8:
            cyc, probe_id, flags = struct.unpack_from("<IHH", self._buf)
            if flags & 0xFFFE != RTT_MARK or probe_id == 0:
                if not self._lost:
                    out.append(Loss(self._time(cyc, advance=False), "RTT の記録が壊れている（同期を取り直す）"))
                    self._lost = True
                del self._buf[0]
                continue
            has_value = flags & 1
            need = 12 if has_value else 8
            if len(self._buf) < need:
                break
            value = struct.unpack_from("<I", self._buf, 8)[0] if has_value else None
            del self._buf[:need]
            self._lost = False
            out.append(Hit(self._time(cyc), probe_id, value))
        return out

    def _time(self, cyc: int, advance: bool = True) -> int:
        high = self._high
        if self._last is not None and cyc < self._last:
            high += 1 << 32
        if advance:
            self.wraps += high != self._high
            self._high, self._last = high, cyc
        return high + cyc

    def flush(self) -> list[Item]:
        out: list[Item] = []
        if self._buf:
            last = 0 if self._last is None else self._high + self._last        # 最後に読めた記録の時刻
            out.append(Loss(last, f"RTT の記録の途中で終わった（{len(self._buf)} バイト）"))
            self._buf.clear()
        return out


# --- まとめて解く ----------------------------------------------------------------------------

def decode_gpio_parallel(tr: Transitions, data_channels: Sequence[int], strobe_channel: int) -> Decoded:
    dec = ParallelDecoder(data_channels, strobe_channel)
    return Decoded(dec.feed(tr) + dec.flush(), 1 / tr.sample_period_s)


def decode_gpio_serial(tr: Transitions, clock_channel: int, data_channel: int, guard: int) -> Decoded:
    dec = SerialDecoder(clock_channel, data_channel, guard)
    return Decoded(dec.feed(tr) + dec.flush(), 1 / tr.sample_period_s)


def decode_itm(items: Iterable[Packet | TimedPacket | tuple[int, Packet]], hz: float, port: int = 1) -> Decoded:
    """hz: 時刻の単位（ローカルのタイムスタンプならコアのクロック / TPIU の前置分周）。"""
    dec = ItmDecoder(port)
    return Decoded(dec.feed(items) + dec.flush(), hz)


def decode_rtt(data: bytes, hz: float) -> Decoded:
    """hz: コアのクロック（CYCCNT の速さ）。wraps = CYCCNT の回り込みを仮定した回数（RttDecoder.wraps）。"""
    dec = RttDecoder()
    items = dec.feed(data) + dec.flush()
    return Decoded(items, hz, wraps=dec.wraps)
