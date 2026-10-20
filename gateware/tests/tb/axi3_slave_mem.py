"""cocotb の AXI3 のスレーブのメモリの模型（64 ビット、INCR のバースト）。READY・VALID をランダムに遅らせる。

- 書き: AW と W を別々に受け（W が先でもよい）、B を返す（AW の順）。書いた値は B を返した時点で見える
  （本物のメモリ・相互接続と同じく、B より前の読みは古い値を返しうる）。
- 読み: AR の順に R を返す（バーストの拍の間にも隙間を入れる）。
- error_addresses に入っている番地（バイト）への読み書きは SLVERR を返す（書きは書かない）。
各周期: 立ち上がりの直後に、その周期の受け渡し（valid と ready が両方 1）を見てから、次の周期の値を出す。
"""
import random
from collections import deque

import cocotb
from cocotb.triggers import RisingEdge


class Axi3SlaveMem:
    def __init__(self, dut, clk, prefix="m_axi", seed=1, stall=0.3):
        self.dut, self.clk, self.p = dut, clk, prefix
        self.rng, self.stall = random.Random(seed), stall
        self.mem: dict[int, int] = {}            # 8 バイトの語の番地 → 値
        self.error_addresses: set[int] = set()
        self.first_beat_error_addresses: set[int] = set()   # 読みのバーストの 1 語目だけ SLVERR
        self.aw, self.w, self.b, self.r = deque(), deque(), deque(), deque()
        self.requests = 0                          # 受けた AW + AR の数
        for name in ("awready", "wready", "bvalid", "arready", "rvalid", "rlast"):
            self._s(name).value = 0
        for name in ("bresp", "rresp", "rdata"):
            self._s(name).value = 0
        cocotb.start_soon(self._run())

    def _s(self, name):
        return getattr(self.dut, f"{self.p}_{name}")

    def _go(self):
        return self.rng.random() >= self.stall

    async def _run(self):
        s = self._s
        while True:
            await RisingEdge(self.clk)
            if self.dut.rst.value != 0:
                continue
            if s("awvalid").value == 1 and s("awready").value == 1:
                self.aw.append((int(s("awaddr").value), int(s("awlen").value) + 1))
                self.requests += 1
            if s("wvalid").value == 1 and s("wready").value == 1:
                self.w.append((int(s("wdata").value), int(s("wstrb").value), int(s("wlast").value)))
            if s("arvalid").value == 1 and s("arready").value == 1:
                addr, n = int(s("araddr").value), int(s("arlen").value) + 1
                self.requests += 1
                bad = addr in self.error_addresses
                first = addr in self.first_beat_error_addresses
                for k in range(n):
                    resp = 2 if bad or (first and k == 0) else 0
                    self.r.append((0 if bad else self.mem.get((addr >> 3) + k, 0), resp, int(k == n - 1)))
            if s("bvalid").value == 1 and s("bready").value == 1:
                _, writes = self.b.popleft()
                self.mem.update(writes)             # 書きは B を返した時点で見えるようになる（それまでの読みは古い値）
                s("bvalid").value = 0
            if s("rvalid").value == 1 and s("rready").value == 1:
                self.r.popleft()
                s("rvalid").value = 0
            while self.aw and len(self.w) >= self.aw[0][1]:
                addr, n = self.aw.popleft()
                beats = [self.w.popleft() for _ in range(n)]
                assert beats[-1][2] == 1 and all(b[2] == 0 for b in beats[:-1]), "WLAST の位置が違う"
                bad = addr in self.error_addresses
                assert all(strb == 0xFF for _, strb, _ in beats)
                writes = {} if bad else {(addr >> 3) + k: data for k, (data, _, _) in enumerate(beats)}
                self.b.append((2 if bad else 0, writes))
            # 次の周期の値（valid は受け渡しまで下ろさない）
            s("awready").value = int(self._go())
            s("wready").value = int(self._go())
            s("arready").value = int(self._go())
            if self.b and s("bvalid").value == 0 and self._go():
                s("bresp").value = self.b[0][0]
                s("bvalid").value = 1
            if self.r and s("rvalid").value == 0 and self._go():
                data, resp, last = self.r[0]
                s("rdata").value, s("rresp").value, s("rlast").value = data, resp, last
                s("rvalid").value = 1
