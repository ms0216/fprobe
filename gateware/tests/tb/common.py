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


async def start_clock_and_reset(dut, *, reset_cycles: int = 3, period_ns: int = CLK_PERIOD_NS, cap_ratio: int = 1):
    """`clk` を起動し `rst` を数クロック与えて解除する。cap_ratio >= 2 なら、`cap_clk` も同じ時刻の立ち上がりから、
    clk の 1 / cap_ratio の周期で起動する（同じ MMCM の整数比のクロック。period_ns は cap_ratio で割り切れること）。"""
    cocotb.start_soon(Clock(dut.clk, period_ns, unit="ns").start())
    if cap_ratio > 1:
        assert period_ns % cap_ratio == 0
        cocotb.start_soon(Clock(dut.cap_clk, period_ns // cap_ratio, unit="ns").start())
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


class TimebaseClock:
    """タイムベース（timebase.vhd と同じ: clk の立ち上がりの直後に 1 増える）を DUT の入力 `time`（名前は signal で選ぶ）に置き、
    シミュレーションの時刻（ps）で辺と時刻の関係を記録する（時刻の正しさ S1b: docs/superpowers/specs/2026-11-01-time-accuracy-design.md
    §10.4 の 2。時刻 T の瞬間 = 値が T になる立ち上がり）。

    tick_at(t_ps) = シミュレーションの時刻 t_ps の、タイムベースの刻みでの位置（T + 周期の中の割合。T 以上 T + 1 未満）。
    ホストの式が返す範囲 [lo, hi)（刻み）がこの値を含むかで、辺と時刻の約束を確かめる。"""

    def __init__(self, dut, *, signal: str = "time", start: int = 1000, width: int = 64, clock=None):
        self.dut, self.signal, self.width = dut, signal, width
        self._clock = clock if clock is not None else dut.clk
        self.value = start
        self.edges: list[tuple[int, int]] = []          # (立ち上がりの ps, その辺で置いた値)
        getattr(dut, signal).value = start
        cocotb.start_soon(self._run())

    async def _run(self):
        from cocotb.utils import get_sim_time
        while True:
            await RisingEdge(self._clock)
            self.value = (self.value + 1) % (1 << self.width)
            getattr(self.dut, self.signal).value = self.value
            self.edges.append((get_sim_time("ps"), self.value))

    def tick_at(self, t_ps: int) -> float:
        """t_ps を含む周期 [辺 k, 辺 k + 1) の値 T と、周期の中の割合。"""
        for (e0, v), (e1, _) in zip(self.edges, self.edges[1:]):
            if e0 <= t_ps < e1:
                return v + (t_ps - e0) / (e1 - e0)
        raise AssertionError(f"{t_ps} ps は記録した辺の間に無い")
