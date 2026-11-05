"""試験・デモ用の波形の発生器の土台: Wave（時刻（秒）で積んだ変化から Transitions を作る）と、ビット列の小さな道具。

プロトコルごとの発生器は synth.py（UART・SPI・I2C・CAN・I2S・Manchester）と synth_buses.py（SWD・1-Wire・JTAG の線を動かす部品）。
"""

import math
import random
from array import array
from collections.abc import Iterable, Mapping

from ..la import Transitions

__all__ = ["Wave", "bits_lsb", "bits_msb", "parity_bit"]


class Wave:
    """チャンネルごとの変化（時刻・値）を積み、サンプルの格子に載せて Transitions にする。
    変化はその時刻以降の最初のサンプルに置く。同じサンプルに落ちる変化は時刻の遅いものが勝つ。"""

    def __init__(self, channels: int, sample_rate_hz: float, idle: Mapping[int, int] | None = None,
                 jitter: float = 0.0, seed: int = 0):
        self.channels = channels
        self.sample_rate_hz = sample_rate_hz
        self.idle = dict(idle or {})
        self.jitter = jitter                               # 変化の時刻に加える一様な揺れ（秒、±jitter）
        self._rng = random.Random(seed)
        self._changes: list[tuple[float, int, int, int]] = []     # (時刻, 積んだ順, ch, 値)

    def set(self, channel: int, level: int, t: float) -> None:
        if not 0 <= channel < self.channels:
            raise ValueError(f"チャンネル {channel} は無い")
        if self.jitter:
            t += self._rng.uniform(-self.jitter, self.jitter)
        self._changes.append((t, len(self._changes), channel, level & 1))

    def bits(self, channel: int, levels: Iterable[int], t: float, bit_s: float) -> float:
        """levels を 1 ビット bit_s 秒ずつ順に出す。終わりの時刻を返す。"""
        for level in levels:
            self.set(channel, level, t)
            t += bit_s
        return t

    def _sample(self, t: float) -> int:
        return math.ceil(t * self.sample_rate_hz - 1e-6)     # 浮動小数の誤差で 1 サンプルずれない

    def build(self, *, start_s: float = 0.0, end_s: float | None = None, trigger_s: float = 0.0) -> Transitions:
        """[start_s, end_s) を捕捉した Transitions。trigger_s は捕捉の先頭からの秒ではなく絶対の時刻。"""
        changes = sorted(self._changes)
        state = 0
        for ch, level in self.idle.items():
            state |= (level & 1) << ch
        first = self._sample(start_s)
        if end_s is None:
            end_s = (changes[-1][0] if changes else start_s) + 1 / self.sample_rate_hz
        length = self._sample(end_s) - first
        if length <= 0:
            raise ValueError("捕捉の区間が空")
        values, starts = array("H"), array("Q")
        rate, n = self.sample_rate_hz, len(changes)
        samples = [math.ceil(c[0] * rate - 1e-6) for c in changes]      # _sample と同じ（ループの外で 1 回）
        i = 0
        while i < n and samples[i] <= first:                           # 先頭までの変化は初期値に畳む
            state = _apply(state, changes[i])
            i += 1
        values.append(state)
        starts.append(0)
        while i < n:
            s = samples[i]
            if s - first >= length:
                break
            while i < n and samples[i] == s:
                _, _, ch, level = changes[i]
                state = state & ~(1 << ch) | level << ch
                i += 1
            if state != values[-1]:
                values.append(state)
                starts.append(s - first)
        trigger = min(max(self._sample(trigger_s) - first, 0), length - 1)
        return Transitions(values, starts, length, trigger, 0, 1 / self.sample_rate_hz, self.channels)


def _apply(state: int, change: tuple[float, int, int, int]) -> int:
    _, _, ch, level = change
    return state & ~(1 << ch) | level << ch


def bits_lsb(value: int, bits: int) -> list[int]:
    return [value >> i & 1 for i in range(bits)]


def bits_msb(value: int, bits: int) -> list[int]:
    return [value >> i & 1 for i in reversed(range(bits))]


def parity_bit(value: int, kind: str) -> int | None:
    """kind のパリティのビット（"none" なら None）。"""
    ones = bin(value).count("1") & 1
    return {"none": None, "even": ones, "odd": ones ^ 1, "mark": 1, "space": 0}[kind]
