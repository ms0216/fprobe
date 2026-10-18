"""cocotb テストベンチの共通部品。

ストリーム I/F の命名規約（RTL 側と共通）: `<prefix>_valid`, `<prefix>_ready`, `<prefix>_data`。

サンプリング規約: 監視は各クロック立ち上がりの直後（その時間ステップの ReadOnly 相）の値を読む。
DUT の入力がテストベンチによって立ち上がり直後に書き換えられる場合、この値は次の立ち上がりで
フリップフロップが取り込む値と同じなので、ハンドシェイクの成立を正しく数えられる。
**入力が立ち下がりで変わるモデル（ULPI PHY モデル等）と組み合わせる場合は使えない**
（tests/tb/ulpi_link_tb.py の settle() のように立ち下がり後に読むこと）。
"""

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, ReadOnly, RisingEdge

CLK_PERIOD_NS = 10


async def start_clock_and_reset(dut, *, reset_cycles: int = 3):
    """`clk` を起動し `rst` を数クロック与えて解除する。"""
    cocotb.start_soon(Clock(dut.clk, CLK_PERIOD_NS, unit="ns").start())
    dut.rst.value = 1
    await ClockCycles(dut.clk, reset_cycles)
    dut.rst.value = 0
    await RisingEdge(dut.clk)


def stream(dut, prefix: str):
    return (getattr(dut, f"{prefix}_valid"), getattr(dut, f"{prefix}_ready"),
            getattr(dut, f"{prefix}_data"))


async def stream_send(dut, prefix: str, values):
    """valid/ready ハンドシェイクで値を隙間なく順に送る。呼び出しはエッジ直後（書き込み可能な相）で行う。"""
    valid, ready, data = stream(dut, prefix)
    for value in values:
        valid.value = 1
        data.value = value
        while True:
            await ReadOnly()
            accepted = bool(int(ready.value))
            await RisingEdge(dut.clk)
            if accepted:
                break
    valid.value = 0


class _EdgeSampler:
    """各立ち上がりエッジ直前の値で `on_sample()` を呼ぶ常駐コルーチン（既定のクロックは dut.clk）。"""

    def __init__(self, dut, clock=None):
        self._clock = clock if clock is not None else dut.clk
        cocotb.start_soon(self._run())

    async def _run(self):
        while True:
            await ReadOnly()
            self.on_sample()
            await RisingEdge(self._clock)

    def on_sample(self):
        raise NotImplementedError


class StreamCollector(_EdgeSampler):
    """`valid & ready` が成立したエッジの `data` を記録する。"""

    def __init__(self, dut, prefix: str, *, always_ready: bool = True, clock=None):
        self.items: list[int] = []
        self._valid, self._ready, self._data = stream(dut, prefix)
        if always_ready:
            self._ready.value = 1
        super().__init__(dut, clock)

    def on_sample(self):
        if int(self._valid.value) and int(self._ready.value):
            self.items.append(int(self._data.value))


class LevelRecorder(_EdgeSampler):
    """1 クロック毎に信号レベルを記録する。"""

    def __init__(self, dut, signal, *, clock=None):
        self.levels: list[int] = []
        self._signal = signal
        super().__init__(dut, clock)

    def on_sample(self):
        self.levels.append(int(self._signal.value))


class PulseCounter(_EdgeSampler):
    """信号が 1 のクロック数を数える（1 クロック幅パルスなら回数）。"""

    def __init__(self, dut, signal, *, clock=None):
        self.count = 0
        self._signal = signal
        super().__init__(dut, clock)

    def on_sample(self):
        self.count += int(self._signal.value)
