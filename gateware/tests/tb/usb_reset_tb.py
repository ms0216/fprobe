"""usb_reset のテストベンチ（時間定数は短縮）。

generics: T_RESET_SE0=10, T_CHIRP=50, T_LINE_STABLE=5, T_HOST_CHIRP_TIMEOUT=300,
          T_HS_IDLE=100, T_SUSPEND_SAMPLE=20, T_FS_IDLE=400
レジスタ書き込みはテストベンチが 4 フェーズで受けて記録する（ulpi_link の代わり）。
"""

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, ReadOnly, RisingEdge

SE0, J, K, SQUELCH = 0b00, 0b01, 0b10, 0b00
FC_DETACH, FC_FS, FC_CHIRP, FC_HS = 0x49, 0x45, 0x54, 0x40
REG_FC, REG_OTG = 0x04, 0x0A

T_DETACH, T_RESET_SE0, T_CHIRP, T_LINE_STABLE, T_HOST_CHIRP_TIMEOUT = 40, 10, 50, 5, 300
T_HS_IDLE, T_SUSPEND_SAMPLE, T_FS_IDLE = 100, 20, 400


class PhyRegisters:
    """reg_req/reg_done の相手をし、最新値と書き込み履歴を持つ。"""

    def __init__(self, dut):
        self.dut = dut
        self.values = {REG_FC: 0x41, REG_OTG: 0x06}
        self.history: list[tuple[int, int]] = []
        dut.reg_done.value = 0
        cocotb.start_soon(self._run())

    async def _run(self):
        while True:
            await RisingEdge(self.dut.clk)
            await ReadOnly()
            if int(self.dut.reg_req.value):
                assert int(self.dut.reg_we.value) == 1
                addr, value = int(self.dut.reg_addr.value), int(self.dut.reg_wdata.value)
                await ClockCycles(self.dut.clk, 3)          # ULPI の書き込みに数周期かかる
                self.values[addr] = value
                self.history.append((addr, value))
                self.dut.reg_done.value = 1
                await RisingEdge(self.dut.clk)
                self.dut.reg_done.value = 0
                while True:                                  # 要求が下がるのを待つ
                    await RisingEdge(self.dut.clk)
                    await ReadOnly()
                    if not int(self.dut.reg_req.value):
                        break


async def setup(dut) -> PhyRegisters:
    dut.line_state.value = SE0
    dut.vbus_valid.value = 1
    dut.link_ready.value = 1
    regs = PhyRegisters(dut)
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    dut.rst.value = 1
    await ClockCycles(dut.clk, 3)
    dut.rst.value = 0
    dut.line_state.value = J                     # 接続済み・アイドル（FS J）
    await ClockCycles(dut.clk, T_DETACH + 30)     # 起動時の切り離しを経て FS で接続
    return regs


async def count_pulses_during(dut, signal, cycles):
    count = 0
    for _ in range(cycles):
        await RisingEdge(dut.clk)
        await ReadOnly()
        count += int(signal.value)
    await RisingEdge(dut.clk)
    return count


async def bus_reset_and_chirp(dut, regs, *, host_pairs):
    """SE0 を与え、デバイスのチャープ後にホストの K-J を host_pairs 組返す。"""
    dut.line_state.value = SE0
    for _ in range(T_RESET_SE0 + T_CHIRP + 200):
        await RisingEdge(dut.clk)
        await ReadOnly()
        if int(dut.chirp_k.value):
            break
    else:
        raise AssertionError("device chirp did not start")
    assert regs.values[REG_FC] == FC_CHIRP
    while True:
        await RisingEdge(dut.clk)
        await ReadOnly()
        if not int(dut.chirp_k.value):
            break
    await RisingEdge(dut.clk)
    for _ in range(host_pairs):
        dut.line_state.value = K
        await ClockCycles(dut.clk, T_LINE_STABLE + 3)
        dut.line_state.value = J
        await ClockCycles(dut.clk, T_LINE_STABLE + 3)
    dut.line_state.value = SQUELCH if host_pairs >= 3 else SE0


@cocotb.test()
async def connects_as_full_speed_with_pulldowns_removed(dut):
    regs = await setup(dut)
    assert regs.values[REG_OTG] == 0x00
    # 起動時はまず切り離し（D+ プルアップ無し）を T_DETACH 保ち、ホストに切断を見せてから FS で接続する
    assert [v for a, v in regs.history if a == REG_FC][:2] == [FC_DETACH, FC_FS]
    await ReadOnly()
    assert int(dut.high_speed.value) == 0


@cocotb.test()
async def bus_reset_then_host_chirp_enters_high_speed(dut):
    regs = await setup(dut)
    # HS に入った後も squelch が T_HS_IDLE 続けば（SOF が無ければ）正しくリセット判定されるので、
    # 数えるのは HS に入るまでの期間に限る
    window = T_RESET_SE0 + T_CHIRP + 6 * (T_LINE_STABLE + 3) + 60
    resets = cocotb.start_soon(count_pulses_during(dut, dut.bus_reset, window))
    await bus_reset_and_chirp(dut, regs, host_pairs=3)
    await ClockCycles(dut.clk, 20)
    assert regs.values[REG_FC] == FC_HS
    await ReadOnly()
    assert int(dut.high_speed.value) == 1
    assert await resets == 1


@cocotb.test()
async def no_host_chirp_falls_back_to_full_speed(dut):
    regs = await setup(dut)
    await bus_reset_and_chirp(dut, regs, host_pairs=0)
    await ClockCycles(dut.clk, T_HOST_CHIRP_TIMEOUT + 20)
    dut.line_state.value = J
    await ClockCycles(dut.clk, 20)
    assert regs.values[REG_FC] == FC_FS
    await ReadOnly()
    assert int(dut.high_speed.value) == 0


@cocotb.test()
async def short_k_j_glitches_are_not_counted(dut):
    regs = await setup(dut)
    await bus_reset_and_chirp(dut, regs, host_pairs=0)
    for _ in range(6):                                   # 安定時間に満たない K/J
        dut.line_state.value = K
        await ClockCycles(dut.clk, T_LINE_STABLE - 2)
        dut.line_state.value = J
        await ClockCycles(dut.clk, T_LINE_STABLE - 2)
    dut.line_state.value = SE0
    await ClockCycles(dut.clk, T_HOST_CHIRP_TIMEOUT + 20)
    await ReadOnly()
    assert int(dut.high_speed.value) == 0


async def enter_hs(dut, regs):
    await bus_reset_and_chirp(dut, regs, host_pairs=3)
    await ClockCycles(dut.clk, 20)
    assert int(dut.high_speed.value) == 1


@cocotb.test()
async def hs_traffic_keeps_device_awake(dut):
    regs = await setup(dut)
    await enter_hs(dut, regs)
    for _ in range(10):                                  # SOF 相当の活動が続く
        dut.line_state.value = 0b01
        await ClockCycles(dut.clk, 2)
        dut.line_state.value = SQUELCH
        await ClockCycles(dut.clk, T_HS_IDLE // 2)
    await ReadOnly()
    assert int(dut.high_speed.value) == 1 and int(dut.suspended.value) == 0


@cocotb.test()
async def hs_idle_then_j_is_suspend(dut):
    regs = await setup(dut)
    await enter_hs(dut, regs)
    await ClockCycles(dut.clk, T_HS_IDLE + 5)            # squelch が続く → FS に戻して判定
    assert regs.values[REG_FC] == FC_FS
    dut.line_state.value = J
    await ClockCycles(dut.clk, T_SUSPEND_SAMPLE + 20)
    await ReadOnly()
    assert int(dut.suspended.value) == 1
    # レジューム: ホストの K（実際は 20 ms 以上）の間は FS のまま。K に続く SE0（LS の EOP）で HS に戻る
    resets = cocotb.start_soon(count_pulses_during(dut, dut.bus_reset, 225))
    await RisingEdge(dut.clk)
    dut.line_state.value = K
    await ClockCycles(dut.clk, 200)
    await ReadOnly()
    assert int(dut.suspended.value) == 0 and int(dut.high_speed.value) == 0
    assert regs.values[REG_FC] == FC_FS
    await RisingEdge(dut.clk)
    dut.line_state.value = SE0
    await ClockCycles(dut.clk, 20)
    await ReadOnly()
    assert int(dut.high_speed.value) == 1 and regs.values[REG_FC] == FC_HS
    assert await resets == 0                             # レジュームはリセットではない


@cocotb.test()
async def hs_idle_then_se0_is_reset(dut):
    regs = await setup(dut)
    await enter_hs(dut, regs)
    resets = cocotb.start_soon(count_pulses_during(dut, dut.bus_reset, T_HS_IDLE + 200))
    await ClockCycles(dut.clk, T_HS_IDLE + T_SUSPEND_SAMPLE + 20)   # SE0（squelch のまま）
    assert int(dut.chirp_k.value) == 1 or regs.values[REG_FC] == FC_CHIRP
    assert await resets == 1


@cocotb.test()
async def vbus_loss_resets_and_returns_to_full_speed(dut):
    regs = await setup(dut)
    await enter_hs(dut, regs)
    resets = cocotb.start_soon(count_pulses_during(dut, dut.bus_reset, 50))
    dut.vbus_valid.value = 0
    await ClockCycles(dut.clk, 30)
    assert regs.values[REG_FC] == FC_FS
    await ReadOnly()
    assert int(dut.high_speed.value) == 0
    assert await resets >= 1


@cocotb.test()
async def fs_idle_j_is_suspend(dut):
    await setup(dut)
    await ClockCycles(dut.clk, T_FS_IDLE + 20)
    await ReadOnly()
    assert int(dut.suspended.value) == 1
