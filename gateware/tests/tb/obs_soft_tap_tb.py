"""obs_soft_tap（ふつうの端子で受ける JTAG の TAP）+ obs_core。IR 4 ビット: USER 0x2、IDCODE 0x1、BYPASS 0xF。
TCK 10 MHz。TDO は TCK の立ち下がりで変わるので、立ち上がりの直前に読む。"""

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, Timer

IR_IDCODE, IR_USER, IR_BYPASS = 0x1, 0x2, 0xF
IDCODE = 0x0F0B5001


class Tap:
    def __init__(self, dut, half_ns=50):
        self.dut, self.half = dut, half_ns
        dut.tck.value = 0
        dut.tms.value = 1
        dut.tdi.value = 0

    async def clock(self, tms: int, tdi: int = 0) -> int:
        """TMS・TDI を置いて 1 回立ち上げる。立ち上がりの直前の TDO を返す。"""
        self.dut.tms.value, self.dut.tdi.value = tms, tdi
        await Timer(self.half, "ns")
        tdo = int(self.dut.tdo.value)
        self.dut.tck.value = 1
        await Timer(self.half, "ns")
        self.dut.tck.value = 0
        return tdo

    async def reset(self):
        """Test-Logic-Reset → Run-Test/Idle。"""
        for _ in range(5):
            await self.clock(1)
        await self.clock(0)

    async def _shift(self, value: int, n: int) -> int:
        """Shift-xR にいる前提で n ビット送り、Exit1 → Update → Run-Test/Idle に戻る。"""
        out = 0
        for i in range(n):
            out |= await self.clock(1 if i == n - 1 else 0, (value >> i) & 1) << i
        await self.clock(1)                                    # Exit1 → Update
        await self.clock(0)                                    # Update → Run-Test/Idle
        return out

    async def shift_ir(self, value: int, n: int = 4) -> int:
        for tms in (1, 1, 0, 0):                               # Select-DR → Select-IR → Capture-IR → Shift-IR
            await self.clock(tms)
        return await self._shift(value, n)

    async def shift_dr(self, value: int, n: int) -> int:
        for tms in (1, 0, 0):                                  # Select-DR → Capture-DR → Shift-DR
            await self.clock(tms)
        return await self._shift(value, n)


async def setup(dut):
    cocotb.start_soon(Clock(dut.clk, 20, unit="ns").start())
    dut.rst.value = 1
    dut.probes.value = 0
    tap = Tap(dut)
    await ClockCycles(dut.clk, 5)
    dut.rst.value = 0
    await tap.reset()
    return tap


@cocotb.test()
async def idcode_after_reset(dut):
    tap = await setup(dut)
    assert await tap.shift_dr(0, 32) == IDCODE
    assert await tap.shift_ir(IR_BYPASS) & 0b11 == 0b01                  # Capture-IR は下位 01


@cocotb.test()
async def bypass_is_one_bit(dut):
    tap = await setup(dut)
    await tap.shift_ir(IR_BYPASS)
    pattern = 0b1011_0010_1101
    assert await tap.shift_dr(pattern, 13) == (pattern << 1) & 0x1FFF    # 1 ビット遅れて出る（最初は 0）
    await tap.shift_ir(0x7)                                               # 知らない命令も BYPASS
    assert await tap.shift_dr(0b1, 2) == 0b10


@cocotb.test()
async def user_dr_reaches_the_core(dut):
    tap = await setup(dut)
    await tap.shift_ir(IR_USER)
    await tap.shift_dr((2 | 0 << 4) << 48, 64)                            # 最後の 16 ビットが命令。op 2 = ID の下位を読む
    await ClockCycles(dut.clk, 10)
    words = await tap.shift_dr(0, 64)                                    # 見出し + 記録 1 語
    assert words & 0xFFFF == 1 and words >> 24 & 0xFF == 0xA6            # 見出し: 記録 1・v2 の印
    assert words >> 32 == 0b110001 << 26 | 0 << 16 | 0x4253              # 応答の記録: 番地 0 = "BS"
    assert dut.tdo_oe.value == 0                                          # Shift の外では手放す
