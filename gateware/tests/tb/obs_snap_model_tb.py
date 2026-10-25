"""obs_snap_core を基準モデルと乱数で突き合わせる（標本の値・Capture-DR までの変化の回数・255 の飽和）。"""
import random
import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, RisingEdge, FallingEdge
from tests.tb.obs_soft_core_tb import Jtag, bits_of, word


@cocotb.test()
async def snap_model(dut):
    cocotb.start_soon(Clock(dut.clk, 20, unit="ns").start())
    dut.rst.value = 1
    dut.probes.value = 0
    j = Jtag(dut)
    await ClockCycles(dut.clk, 5)
    dut.rst.value = 0
    rng = random.Random(7)
    state = {"prev": 0, "n": 0, "caps": []}

    async def monitor():
        prev = None
        while True:
            await RisingEdge(dut.clk)
            try:
                p = int(dut.probes.value)
            except ValueError:
                continue
            if prev is not None and p != prev and state["n"] < 255:
                state["n"] += 1
            prev = p
            if dut.rise.value == 1 and dut.u_sel.value == 1 and dut.u_capture.value == 1:
                state["caps"].append((p, state["n"]))
                state["n"] = 0

    async def wiggle():
        while True:
            await FallingEdge(dut.clk)
            if rng.random() < 0.05:
                dut.probes.value = rng.randrange(65536)
            if rng.random() < 0.002:
                for _ in range(rng.randrange(1, 400)):
                    await FallingEdge(dut.clk)
                    dut.probes.value = rng.randrange(65536)

    await j.reset()
    await j.shift(bits_of(0x2, 4), ir=True)
    cocotb.start_soon(monitor())
    cocotb.start_soon(wiggle())
    errs = 0
    for it in range(150):
        out = await j.shift([0] * 32, ir=False)
        w = word(out, 0)
        if it > 0:
            exp = state["caps"][-1]
            got = (w & 0xFFFF, w >> 16 & 0xFF)
            if w >> 24 != 0xA7 or got != exp:
                errs += 1
                dut._log.error(f"it={it} got={w:08x} exp={exp}")
        if rng.random() < 0.3:
            await ClockCycles(dut.clk, rng.randrange(1, 300))
        if rng.random() < 0.1:
            # DR を IDCODE/BYPASS に切り替えてまた USER
            await j.shift(bits_of(0xF, 4), ir=True)
            await j.shift([0] * 5, ir=False)
            await j.shift(bits_of(0x2, 4), ir=True)
    assert errs == 0
