"""メモリ I/F（mem_port: 1 コマンド = 1 拍、読み出しは発行順に返る）の振る舞いモデル（cocotb, テスト専用）。

DDR3 コントローラの代わり: cmd_ready をときどき下げ、読み出しは latency 周期後に返す。
size_beats を超える番地は折り返す（容量より先の番地線が無いのと同じ）。stuck_bits は読み値で常に 1 になるビット。
"""

import random
from collections import deque

from cocotb.triggers import ReadOnly, RisingEdge


class MemPortModel:
    def __init__(self, dut, *, prefix="", clock=None, size_beats=1 << 20, latency=(8, 20), stall=0.2,
                 stuck_bits=0, rng=None):
        self.dut = dut
        self.clock = clock if clock is not None else dut.clk
        self.sig = lambda name: getattr(dut, prefix + name)
        self.size, self.latency, self.stall, self.stuck = size_beats, latency, stall, stuck_bits
        self.rng = rng or random.Random(1)
        self.memory: dict[int, int] = {}
        self.pending: deque[tuple[int, int]] = deque()       # (返す周期, 値)
        self.writes = self.reads = 0
        self.sig("cmd_ready").value = 0
        self.sig("rd_valid").value = 0
        self.sig("rd_data").value = 0

    async def run(self):
        cycle = 0
        while True:
            ready = int(self.rng.random() >= self.stall)
            self.sig("cmd_ready").value = ready
            rd = self.pending[0][1] if self.pending and self.pending[0][0] <= cycle else None
            self.sig("rd_valid").value = int(rd is not None)
            if rd is not None:
                self.sig("rd_data").value = rd
                self.pending.popleft()
            await ReadOnly()
            if ready and int(self.sig("cmd_valid").value):
                addr = int(self.sig("cmd_addr").value) % self.size
                if int(self.sig("cmd_write").value):
                    self.memory[addr] = int(self.sig("cmd_data").value)
                    self.writes += 1
                else:
                    self.pending.append((cycle + self.rng.randint(*self.latency),
                                         self.memory.get(addr, 0) | self.stuck))
                    self.reads += 1
            await RisingEdge(self.clock)
            cycle += 1
