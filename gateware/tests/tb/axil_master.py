"""cocotb の小さな AXI4-Lite のマスタ（1 度に 1 つの取引。cocotbext-axi は使わない: 依存を増やさない）。

delay() が 0 以上の整数を返すと、その周期数だけ valid / ready を遅らせる（試験でランダムな遅れを入れる）。
"""
from cocotb.triggers import ClockCycles, ReadOnly, RisingEdge


class AxiLiteMaster:
    def __init__(self, dut, clk, prefix="s_axi", delay=lambda: 0, timeout=200):
        self.dut, self.clk, self.p, self.delay, self.timeout = dut, clk, prefix, delay, timeout
        for name in ("awvalid", "wvalid", "bready", "arvalid", "rready"):
            self._sig(name).value = 0
        self._sig("awaddr").value = 0
        self._sig("wdata").value = 0
        self._sig("wstrb").value = 0
        self._sig("araddr").value = 0

    def _sig(self, name):
        return getattr(self.dut, f"{self.p}_{name}")

    async def _wait(self, n):
        if n:
            await ClockCycles(self.clk, n)

    async def _handshake(self, valid, ready):
        """valid を立て、ready と重なった周期の後で下ろす。"""
        valid.value = 1
        for _ in range(self.timeout):
            await RisingEdge(self.clk)
            if ready.value == 1:
                valid.value = 0
                return
        raise TimeoutError(f"{ready._name} が立たない")

    async def _respond(self, valid, ready):
        """応答の valid を待ち、ready を（遅らせて）立てて受け取る。"""
        await self._wait(self.delay())
        ready.value = 1
        for _ in range(self.timeout):
            await RisingEdge(self.clk)
            if valid.value == 1:
                ready.value = 0
                return
        raise TimeoutError(f"{valid._name} が返らない")

    async def write(self, addr, data, strb=0xF):
        await self._wait(self.delay())
        self._sig("awaddr").value = addr
        self._sig("wdata").value = data
        self._sig("wstrb").value = strb
        channels = [(self._sig("awvalid"), self._sig("awready")), (self._sig("wvalid"), self._sig("wready"))]
        if self.delay() % 2:                       # AW と W の順も変える
            channels.reverse()
        for valid, ready in channels:
            await self._handshake(valid, ready)
            await self._wait(self.delay())
        await self._respond(self._sig("bvalid"), self._sig("bready"))
        await ReadOnly()
        assert self._sig("bresp").value == 0
        await RisingEdge(self.clk)

    async def read(self, addr):
        await self._wait(self.delay())
        self._sig("araddr").value = addr
        await self._handshake(self._sig("arvalid"), self._sig("arready"))
        await self._wait(self.delay())
        self._sig("rready").value = 1
        for _ in range(self.timeout):
            await RisingEdge(self.clk)
            if self._sig("rvalid").value == 1:
                raw = self._sig("rdata").value
                assert raw.is_resolvable, f"rdata {raw} @ araddr {int(self._sig('araddr').value):#x}"
                data = int(raw)
                assert self._sig("rresp").value == 0
                self._sig("rready").value = 0
                return data
        raise TimeoutError("rvalid が返らない")
