"""汎用の SPI マスタ: 拡張ヘッダの汎用入出力（fprobe.gpio）をビットバングで動かす（FPGA に回路を足さない）。

端子はロジアナの ch（0〜7）で指す（ロジアナで同じ時間軸に SPI の波形を記録できる）。mode 0〜3（CPOL・CPHA）、語長 1〜32
ビット、MSB / LSB 先頭、CS の極性を選べる。1 回の transfer は「CS を選ぶ → 語を送りながら受ける（全二重）→ 続けて
read_count 語を埋め（既定 全 1）を送りながら受ける → SCK を休止に戻して CS を離す」。keep_cs で CS を選んだまま続ける。

1 ビット = 2 状態（SCK の 2 つの辺。stretch で各状態を 1 + stretch 回書いて遅くする）。全状態を Gpio.sequence
（CONTROL の EXCHANGE）で流し、受ける側の辺の状態の読みから MISO を取る。MISO はその辺の前の辺（相手が出す辺）から
少なくとも 1 状態（1 語の処理 = 約 140 ns）後に読むので、相手の出力の遅れ（SPI Flash で 10 ns 以下）より十分長い。
長い配線で足りなければ stretch を上げる。
仕様: docs/superpowers/specs/2026-10-19-u42-spi-flash-design.md
"""

from collections.abc import Sequence
from dataclasses import dataclass

from .gpio import Gpio

__all__ = ["SpiConfig", "SpiMaster", "SpiPins"]

PINS = 8


@dataclass(frozen=True)
class SpiPins:
    sck: int
    mosi: int | None = None      # None: 送らない（読むだけ）
    miso: int | None = None      # None: 受けない（読みは全 0）
    cs: int | None = None        # None: CS を動かさない（相手の CS は別に決める）

    def __post_init__(self):
        used = [p for p in (self.sck, self.mosi, self.miso, self.cs) if p is not None]
        if any(not 0 <= p < PINS for p in used):
            raise ValueError("SPI の端子はロジアナの ch 0〜7")
        if len(set(used)) != len(used):
            raise ValueError("SPI の端子が重なる")

    @property
    def outputs(self) -> int:
        """プローブが駆動する端子のビット。"""
        return sum(1 << p for p in (self.sck, self.mosi, self.cs) if p is not None)


@dataclass(frozen=True)
class SpiConfig:
    mode: int = 0                # CPOL = mode の bit1、CPHA = bit0
    bits: int = 8                # 語長
    lsb_first: bool = False
    cs_active_high: bool = False
    stretch: int = 0             # 各状態を 1 + stretch 回書く（遅くする）

    def __post_init__(self):
        if self.mode not in (0, 1, 2, 3):
            raise ValueError("mode は 0〜3")
        if not 1 <= self.bits <= 32:
            raise ValueError("語長は 1〜32 ビット")
        if not 0 <= self.stretch <= 255:
            raise ValueError("stretch は 0〜255")

    @property
    def cpol(self) -> int:
        return self.mode >> 1 & 1

    @property
    def cpha(self) -> int:
        return self.mode & 1


class SpiMaster:
    def __init__(self, gpio: Gpio, pins: SpiPins, config: SpiConfig = SpiConfig()):
        self.gpio, self.pins, self.config = gpio, pins, config
        self.last_states = 0             # 最後の transfer で流した状態の数（速さの見積もり用）

    def _bit_order(self) -> range:
        bits = self.config.bits
        return range(bits) if self.config.lsb_first else range(bits - 1, -1, -1)

    def transfer(self, words: Sequence[int], read_count: int = 0, fill: int | None = None,
                 keep_cs: bool = False) -> list[int]:
        """words を送りながら受け、続けて read_count 語を fill（既定 全 1）を送りながら受ける。受けた語の列
        （len(words) + read_count 語）を返す。"""
        cfg, pins = self.config, self.pins
        mask = (1 << cfg.bits) - 1
        if words and pins.mosi is None:
            raise ValueError("MOSI の端子が無いので送れない（read_count で読むだけにする）")
        if read_count < 0:
            raise ValueError("read_count は 0 以上")
        fill = mask if fill is None else fill & mask
        out = [w & mask for w in words] + [fill] * read_count

        # 端子の初めの状態: SPI の端子以外は今のまま。SPI の出力の端子を休止の値で出力にする
        cur = self.gpio.state()
        cs_on = 0 if pins.cs is None else (1 if cfg.cs_active_high else 0) << pins.cs
        cs_off = 0 if pins.cs is None else (0 if cfg.cs_active_high else 1) << pins.cs
        cs_bit = 0 if pins.cs is None else 1 << pins.cs
        sck_bit = 1 << pins.sck
        mosi_bit = 0 if pins.mosi is None else 1 << pins.mosi
        idle = cfg.cpol * sck_bit
        selected_now = pins.cs is not None and cur.enable & cs_bit and (cur.drive & cs_bit) == cs_on
        base = cur.drive & ~(cs_bit | sck_bit | mosi_bit)
        start_cs = cs_on if selected_now else cs_off
        miso_in = 0 if pins.miso is None else 1 << pins.miso
        if (cur.drive & (cs_bit | sck_bit | mosi_bit)) != (start_cs | idle | mosi_bit) \
                or cur.enable & (pins.outputs | miso_in) != pins.outputs:
            self.gpio.set(drive=start_cs | idle | mosi_bit, enable=pins.outputs, mask=pins.outputs | miso_in)

        states: list[int] = []
        samples: list[int] = []
        repeat = 1 + cfg.stretch

        def put(value: int, sample: bool = False) -> None:
            states.extend([value] * repeat)
            if sample:
                samples.append(len(states) - 1)

        first = out[0] if out else fill
        first_bit = first >> next(iter(self._bit_order())) & 1 if out else 1
        put(base | cs_on | idle | (mosi_bit if first_bit else 0))       # 選ぶ（CPHA 0 は最初のビットも出す）
        active = (1 - cfg.cpol) * sck_bit
        for word in out:
            for i in self._bit_order():
                d = mosi_bit if word >> i & 1 else 0
                if cfg.cpha == 0:
                    put(base | cs_on | idle | d)                          # 出す辺（後ろの辺）
                    put(base | cs_on | active | d, sample=True)           # 受ける辺（前の辺）
                else:
                    put(base | cs_on | active | d)                        # 出す辺（前の辺）
                    put(base | cs_on | idle | d, sample=True)             # 受ける辺（後ろの辺）
        if cfg.cpha == 0 and out:
            put(base | cs_on | idle | mosi_bit)                           # SCK を休止に戻してから
        if not keep_cs:
            put(base | cs_off | idle | mosi_bit)                          # CS を離す

        levels = self.gpio.sequence(states)
        self.last_states = len(states)
        received: list[int] = []
        k = 0
        for _ in out:
            word = 0
            for i in self._bit_order():
                if pins.miso is not None and levels[samples[k]] >> pins.miso & 1:
                    word |= 1 << i
                k += 1
            received.append(word)
        return received
