"""配線の自動判定: 拡張ヘッダのどのピンにターゲットの SWD がつながっているかを、GPIO のビットバングで探す。

CSR の gpio ブロック（8 本、プルアップ付き）を PC から直接動かすので遅い（1 回の操作が USB の往復）が、
ゲートウェアの SWD エンジンに依存しない。配線を変えたときの確認・初回の割り当て決定に使う。
"""

from dataclasses import dataclass
from itertools import permutations

from . import generated as g
from .device import Device

__all__ = ["SwdPins", "find_swd_pins", "read_dpidr"]

PINS = 8
READ_DPIDR = 0xA5                 # start=1, APnDP=0, RnW=1, A=00, parity=1, stop=0, park=1（下位から）
JTAG_TO_SWD = 0xE79E


@dataclass(frozen=True)
class SwdPins:
    swclk: int
    swdio: int
    dpidr: int


class _BitBang:
    """SWCLK / SWDIO だけを動かす SWD（ARM の参照実装と同じタイミング）。"""

    def __init__(self, dev: Device, swclk: int, swdio: int, hold: tuple[int, int] = (0, 0)):
        """hold: 他のピンの (出力値, 出力有効)。ビットバング中もそのまま保つ。"""
        self.dev, self.clk, self.dio = dev, 1 << swclk, 1 << swdio
        self.hold_drive, self.hold_enable = hold
        self.drive = self.hold_drive | self.clk | self.dio      # アイドル: SWCLK = 1, SWDIO = 1
        self.enable = self.hold_enable | self.clk | self.dio
        dev.write(g.GPIO_DRIVE, [self.drive, self.enable])

    def _set(self, *, clk: int, dio: int | None):
        drive = self.hold_drive | (self.clk if clk else 0) | (self.dio if dio else 0)
        enable = self.hold_enable | self.clk | (self.dio if dio is not None else 0)
        if (drive, enable) != (self.drive, self.enable):
            self.dev.write(g.GPIO_DRIVE, [drive, enable])
            self.drive, self.enable = drive, enable

    def write_bits(self, value: int, count: int):
        for i in range(count):
            bit = value >> i & 1
            self._set(clk=0, dio=bit)
            self._set(clk=1, dio=bit)

    def read_bits(self, count: int) -> int:
        value = 0
        for i in range(count):
            self._set(clk=0, dio=None)
            bit = 1 if self.dev.read32(g.GPIO_LEVEL) & self.dio else 0
            self._set(clk=1, dio=None)
            value |= bit << i
        return value

    def release(self):
        self.dev.write(g.GPIO_ENABLE, [self.hold_enable])


def read_dpidr(dev: Device, swclk: int, swdio: int) -> int | None:
    """ラインリセット → JTAG から SWD へ → DPIDR を読む。成功すれば値、失敗すれば None。"""
    bb = _BitBang(dev, swclk, swdio)
    try:
        bb.write_bits((1 << 51) - 1, 51)
        bb.write_bits(JTAG_TO_SWD, 16)
        bb.write_bits((1 << 51) - 1, 51)
        bb.write_bits(0, 2)
        bb.write_bits(READ_DPIDR, 8)
        bb.read_bits(1)                                   # ターンアラウンド
        ack = bb.read_bits(3)
        if ack != 0b001:
            return None
        data = bb.read_bits(32)
        parity = bb.read_bits(1)
        bb.read_bits(1)                                   # ターンアラウンド
        bb.write_bits(0, 2)
        if bin(data).count("1") & 1 != parity:
            return None
        return data
    finally:
        bb.release()


def find_swd_pins(dev: Device, pins: int = PINS) -> list[SwdPins]:
    """全ての (SWCLK, SWDIO) の組を試し、DPIDR が読めた組を全部返す（最初の 1 つで決めない: 複数のターゲットが
    つながっていることもある。どれを使うかは使う側が決める）。"""
    found = []
    for swclk, swdio in permutations(range(pins), 2):
        dpidr = read_dpidr(dev, swclk, swdio)
        if dpidr is not None:
            found.append(SwdPins(swclk, swdio, dpidr))
    return found


class BitBangSwd:
    """GPIO のビットバングによる SWD（遅いが、ゲートウェアの SWD エンジン無しで DP / MEM-AP を使える）。"""

    def __init__(self, dev: Device, pins: SwdPins, hold: tuple[int, int] = (0, 0)):
        self.bb = _BitBang(dev, pins.swclk, pins.swdio, hold)
        self.bb.write_bits((1 << 51) - 1, 51)
        self.bb.write_bits(JTAG_TO_SWD, 16)
        self.bb.write_bits((1 << 51) - 1, 51)
        self.bb.write_bits(0, 2)
        if self.transfer(0b0010) is None:                                  # DPIDR
            raise RuntimeError("SWD で DPIDR が読めない")

    def transfer(self, request: int, data: int = 0) -> int | None:
        """CMSIS-DAP 形式の要求（bit0 APnDP, bit1 RnW, bit2-3 A[3:2]）。OK でなければ None。"""
        bb = self.bb
        bits = [request >> i & 1 for i in range(4)]
        header = 1 | bits[0] << 1 | bits[1] << 2 | bits[2] << 3 | bits[3] << 4 | (sum(bits) & 1) << 5 | 1 << 7
        bb.write_bits(header, 8)
        bb.read_bits(1)
        if bb.read_bits(3) != 0b001:
            bb.read_bits(1)
            bb.write_bits(0, 2)
            return None
        if request & 0b10:
            value = bb.read_bits(32)
            parity = bb.read_bits(1)
            bb.read_bits(1)
            bb.write_bits(0, 2)
            return value if bin(value).count("1") & 1 == parity else None
        bb.read_bits(1)
        bb.write_bits(data | (bin(data).count("1") & 1) << 32, 33)
        bb.write_bits(0, 2)
        return 0

    def power_up(self):
        self.transfer(0b0000, 0x1E)                                        # ABORT: エラーを消す
        self.transfer(0b0100, 0x50000000)                                  # CTRL/STAT: 電源要求
        self.transfer(0b1000, 0)                                           # SELECT: AP0 バンク 0
        self.transfer(0b0001, 0x23000002)                                  # CSW: 32 ビット

    def write32(self, address: int, value: int):
        self.transfer(0b0101, address)
        self.transfer(0b1101, value)

    def read32(self, address: int) -> int:
        self.transfer(0b0101, address)
        self.transfer(0b1111)                                              # DRW（ポスティング）
        return self.transfer(0b1110)                                       # RDBUFF

    def release(self):
        self.bb.release()
