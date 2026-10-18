"""udiv_shared（2 つの使い手で共有する割り算器）のテストベンチ。"""

import random

import cocotb
from cocotb.triggers import ReadOnly, RisingEdge

from tests.tb.common import start_clock_and_reset


class Client:
    """使い手の約束: req を 1 にして num / den を保ち、done の周期の quotient を受け、次の周期に req を下ろす。"""

    def __init__(self, dut, prefix):
        self.dut = dut
        self.req = getattr(dut, f"{prefix}_req")
        self.num = getattr(dut, f"{prefix}_num")
        self.den = getattr(dut, f"{prefix}_den")
        self.done = getattr(dut, f"{prefix}_done")
        self.req.value = 0
        self.num.value = 0
        self.den.value = 1

    async def divide(self, a, b, timeout=200):
        self.num.value = a
        self.den.value = b
        self.req.value = 1
        for cycles in range(timeout):
            await RisingEdge(self.dut.clk)
            await ReadOnly()
            if int(self.done.value):
                quotient = int(self.dut.quotient.value)
                await RisingEdge(self.dut.clk)
                self.req.value = 0
                return quotient, cycles
        raise AssertionError("done が来ない")


@cocotb.test()
async def each_client_alone(dut):
    p0, p1 = Client(dut, "p0"), Client(dut, "p1")
    await start_clock_and_reset(dut)
    rng = random.Random(2)
    for _ in range(10):
        a, b = rng.getrandbits(32), rng.getrandbits(rng.randrange(1, 33)) or 1
        assert (await p0.divide(a, b))[0] == a // b
        a, b = rng.getrandbits(32), rng.getrandbits(rng.randrange(1, 33)) or 1
        assert (await p1.divide(a, b))[0] == a // b


@cocotb.test()
async def simultaneous_requests_are_served_in_turn_without_loss(dut):
    p0, p1 = Client(dut, "p0"), Client(dut, "p1")
    await start_clock_and_reset(dut)
    t0 = cocotb.start_soon(p0.divide(100_285_714, 115_200))
    t1 = cocotb.start_soon(p1.divide(1_000_000, 3))
    (q0, c0), (q1, c1) = await t0, await t1
    assert (q0, q1) == (100_285_714 // 115_200, 1_000_000 // 3)
    assert c0 < c1                                            # p0 が先（優先）、p1 は待たされた
    for _ in range(80):                                       # 下ろした後に余計な計算・done が無い
        await RisingEdge(dut.clk)
        await ReadOnly()
        assert int(dut.p0_done.value) == 0 and int(dut.p1_done.value) == 0


@cocotb.test()
async def a_request_while_busy_waits(dut):
    p0, p1 = Client(dut, "p0"), Client(dut, "p1")
    await start_clock_and_reset(dut)
    t1 = cocotb.start_soon(p1.divide(0xFFFFFFFF, 7))
    for _ in range(5):
        await RisingEdge(dut.clk)
    q0, _ = await p0.divide(12345678, 1000)                  # p1 の計算中に来た p0 は、その後で
    assert q0 == 12345
    assert (await t1)[0] == 0xFFFFFFFF // 7
