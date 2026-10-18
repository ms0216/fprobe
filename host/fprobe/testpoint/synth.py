"""試験・デモ用: テストポイントの記録から、ターゲットが出すのと同じ波形（GPIO）・ITM のバイト列・RTT のデータを作る。

fprobe_tp.h の出し方をそのまま真似る（仕様の 3 節）。GPIO は `fprobe.decode.synth.Wave` に積む。
"""

import math
import struct
from collections.abc import Iterable, Sequence

from ..decode.synth import Wave

__all__ = ["Record", "gpio_parallel", "gpio_serial", "itm_bytes", "rtt_bytes"]

RTT_MARK = 0xA500                                  # RTT の記録の flags の上位（同期の取り直しの印）
Record = tuple[float | int, int, int | None]      # (時刻, ID, 値 or None)。GPIO は秒、ITM・RTT はクロック数


def _bits(probe_id: int, value: int | None) -> tuple[int, int]:
    if value is None:
        return probe_id & 0xFFFF, 16
    return (probe_id & 0xFFFF) << 32 | (value & 0xFFFFFFFF), 48


def gpio_parallel(wave: Wave, records: Iterable[Record], data_channels: Sequence[int], strobe_channel: int,
                  store_s: float = 50e-9) -> float:
    """並列: 記号ごとに (データ・ストローブ 0) → store_s 後に (ストローブ 1)。data_channels は D0..D(W-1)
    （最後が先頭の印）。記録の時刻は最初の書き込みの時刻。最後の書き込みの時刻を返す。"""
    width = len(data_channels)
    payload = width - 1
    end = 0.0
    for t, probe_id, value in records:
        bits, nbits = _bits(probe_id, value)
        symbols = math.ceil(nbits / payload)
        bits <<= symbols * payload - nbits
        for s in range(symbols - 1, -1, -1):
            sym = (bits >> (s * payload)) & ((1 << payload) - 1)
            if s == symbols - 1:
                sym |= 1 << payload
            for k, ch in enumerate(data_channels):
                wave.set(ch, sym >> k & 1, t)
            wave.set(strobe_channel, 0, t)
            wave.set(strobe_channel, 1, t + store_s)
            t += 2 * store_s
        end = t
    return end


def gpio_serial(wave: Wave, records: Iterable[Record], clock_channel: int, data_channel: int,
                store_s: float = 50e-9, gap_s: float = 100e-9) -> float:
    """2 線: 開始（CLK = 1 で DAT を 0）→ 間 → ビットごとに (CLK 0・DAT) → (CLK 1) → 終わりの 0 → 間 → 終了。
    記録の時刻は開始の時刻。アイドル（両方 1）は Wave の idle で与える。"""
    end = 0.0
    for t, probe_id, value in records:
        bits, nbits = _bits(probe_id, value)
        wave.set(data_channel, 0, t)
        t += gap_s
        for i in range(nbits, -1, -1):
            bit = 0 if i == 0 else bits >> (i - 1) & 1
            wave.set(clock_channel, 0, t)
            wave.set(data_channel, bit, t)
            wave.set(clock_channel, 1, t + store_s)
            t += 2 * store_s
        t += gap_s
        wave.set(data_channel, 1, t)
        t += store_s
        end = t
    return end


def _local_timestamp(delta: int) -> bytes:
    """ローカルのタイムスタンプ（形式 1、TC = 0）。"""
    out = [0xC0]
    while True:
        b = delta & 0x7F
        delta >>= 7
        out.append(b | (0x80 if delta else 0))
        if not delta:
            return bytes(out)


def itm_bytes(records: Iterable[Record], port: int = 1, *, timestamps: bool = True, sync: bool = True) -> bytes:
    """ITM: 値付きなら値（4 バイト）→ ID（2 バイト）。timestamps なら ID の後に前の記録からの差分のローカルの
    タイムスタンプ（最初の記録は時刻そのもの）。"""
    out = bytearray(b"\x00" * 5 + b"\x80" if sync else b"")
    previous = 0
    for t, probe_id, value in records:
        if value is not None:
            out += bytes([port << 3 | 3]) + (value & 0xFFFFFFFF).to_bytes(4, "little")
        out += bytes([port << 3 | 2]) + (probe_id & 0xFFFF).to_bytes(2, "little")
        if timestamps:
            if not 0 < t - previous < 1 << 28:
                raise ValueError("ローカルのタイムスタンプの差分は 1〜2^28-1")
            out += _local_timestamp(int(t - previous))
            previous = int(t)
    return bytes(out)


def rtt_bytes(records: Iterable[Record]) -> bytes:
    """RTT: u32 CYCCNT（下位 32 ビット）, u16 ID, u16 flags（0xA500 | 値付き）[, u32 値]。"""
    out = bytearray()
    for t, probe_id, value in records:
        out += struct.pack("<IHH", int(t) & 0xFFFFFFFF, probe_id & 0xFFFF, RTT_MARK | (0 if value is None else 1))
        if value is not None:
            out += struct.pack("<I", value & 0xFFFFFFFF)
    return bytes(out)
