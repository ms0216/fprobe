"""2026-11-03 のレビュー（DAP の線の速さ）: SWD の TransferBlock を half_period 1（50 MHz）・ターゲットの遅れ + 取り込みの遅れ（0〜7）・
ターンアラウンド 1/2・idle_cycles・失敗（FAULT / WAIT の使い切り / パリティ）ありで回し、値・処理数・応答と、失敗の後の
転送が線に出ないことを見る。"""

import struct

import cocotb

from tests.models.swd_target import STKERRCLR
from tests.tb.dap_processor_tb import (AP_DRW_R, AP_DRW_W, AP_TAR_W, DP_ABORT_W, DP_IDCODE_R, FAULT, OK, WAIT,
                                       setup)
from tests.models.swd_target import DPIDR


async def configure(dap, dut, hz, sd, turnaround, idle, retry):
    dut.swd_sample_delay.value = sd
    assert await dap.command(bytes([0x11]) + struct.pack("<I", hz)) == bytes([0x11, 0])
    assert await dap.command(bytes([0x13, turnaround - 1])) == bytes([0x13, 0])
    assert await dap.command(bytes([0x04, idle]) + struct.pack("<HH", retry, 0)) == bytes([0x04, 0])


async def case(dut, *, hz, sd, delay, turnaround=1, idle=0):
    dap, model = await setup(dut, seed=21, output_delay_ns=delay, turnaround=turnaround)
    await dap.connect()
    await configure(dap, dut, hz, sd, turnaround, idle, 5)
    await dap.mem_setup()
    tag = (hz, sd, delay, turnaround, idle)
    values = [0x5A000000 + 0x1111 * i for i in range(16)]
    assert (await dap.transfer((AP_TAR_W, 0x20000000))) == (1, OK, []), tag
    assert (await dap.block(AP_DRW_W, 16, values))[:2] == (16, OK), tag
    assert [model.memory[0x20000000 + 4 * i] for i in range(16)] == values, tag
    assert (await dap.transfer((AP_TAR_W, 0x20000000))) == (1, OK, []), tag
    assert await dap.block(AP_DRW_R, 16) == (16, OK, values), tag
    assert await dap.block(DP_IDCODE_R, 3) == (3, OK, [DPIDR] * 3), tag
    # FAULT（3 語目の書きで STICKYERR、4 語目で FAULT）
    model.fault_addresses = {0x20000008}
    assert (await dap.transfer((AP_TAR_W, 0x20000000))) == (1, OK, []), tag
    n = len(model.transactions)
    done, resp, _ = await dap.block(AP_DRW_W, 8, [0xA0 + i for i in range(8)])
    assert (done, resp) == (3, FAULT), (tag, done, resp)
    assert len(model.transactions) == n + 4, (tag, model.transactions[n:])
    assert (await dap.transfer((DP_ABORT_W, STKERRCLR))) == (1, OK, []), tag
    model.fault_addresses = set()
    # パリティ異常（読みの 3 語目）
    assert (await dap.transfer((AP_TAR_W, 0x20000000))) == (1, OK, []), tag
    n = len(model.transactions)
    model.corrupt_parity_at = n + 3
    done, resp, data = await dap.block(AP_DRW_R, 8)
    for k, t in enumerate(model.transactions[n - 6:]):
        dut._log.info("TX %d %s", n - 6 + k, t)
    assert (done, resp, data) == (2, 8, [0xA0, 0xA1]), (tag, done, resp, data, n)
    assert len(model.transactions) == n + 4, (tag, model.transactions[n:])
    model.corrupt_parity_at = None
    # WAIT の使い切り（再試行 0）
    await configure(dap, dut, hz, sd, turnaround, idle, 0)
    assert (await dap.transfer((AP_TAR_W, 0x20000000))) == (1, OK, []), tag
    n = len(model.transactions)
    model.wait_at = n + 2
    done, resp, data = await dap.block(AP_DRW_R, 8)
    assert (done, resp, data) == (1, WAIT, [0xA0]), (tag, done, resp, data)
    assert len(model.transactions) == n + 3, (tag, model.transactions[n:])
    model.wait_at = None
    # WAIT を再試行で越える（再試行 3、毎回 2 回 WAIT）
    await configure(dap, dut, hz, sd, turnaround, idle, 3)
    assert (await dap.transfer((AP_TAR_W, 0x20000000))) == (1, OK, []), tag
    model.wait_next = 2
    assert (await dap.block(AP_DRW_R, 8))[:2] == (8, OK), tag
    model.wait_next = 0


@cocotb.test()
async def swd_hp1_d0(dut):
    await case(dut, hz=50_000_000, sd=0, delay=3)


@cocotb.test()
async def swd_hp1_d7_late(dut):
    # 往復 75 ns、hp 1: d·T ∈ (RT − 2T, RT) = (55, 75) → d = 6, 7
    await case(dut, hz=50_000_000, sd=7, delay=75)


@cocotb.test()
async def swd_hp1_d3_trn2_idle(dut):
    # 往復 25 ns: (5, 25) → d = 1, 2
    await case(dut, hz=50_000_000, sd=2, delay=25, turnaround=2, idle=3)


@cocotb.test()
async def swd_hp2_d7_late(dut):
    # 往復 75 ns、hp 2: (35, 75) → d = 4..7。idle 1 で落ちる（下の bug_idle1_* と同じ原因: パリティの見落とし）
    await case(dut, hz=25_000_000, sd=7, delay=75, idle=1)


async def parity_only(dut, *, hz, sd, delay, idle=0, turnaround=1):
    dap, model = await setup(dut, seed=21, output_delay_ns=delay, turnaround=turnaround)
    await dap.connect()
    await configure(dap, dut, hz, sd, turnaround, idle, 5)
    await dap.mem_setup()
    values = [0xA0 + i for i in range(8)]
    assert (await dap.transfer((AP_TAR_W, 0x20000000))) == (1, OK, [])
    assert (await dap.block(AP_DRW_W, 8, values))[:2] == (8, OK)
    assert (await dap.transfer((AP_TAR_W, 0x20000000))) == (1, OK, [])
    n = len(model.transactions)
    model.corrupt_parity_at = n + 3
    done, resp, data = await dap.block(AP_DRW_R, 8)
    log = model.transactions[n:]
    return (done, resp, data), log


async def _parity_check(dut, hz, sd, delay, idle):
    r, log = await parity_only(dut, hz=hz, sd=sd, delay=delay, idle=idle)
    dut._log.info("PARITY %s %s %s", (hz, sd, delay, idle), r[:2], [x[4] for x in log])
    assert r[:2] == (2, 8), ((hz, sd, delay, idle), r, log)


@cocotb.test()
async def parity_hz25_d7_delay75_idle0(dut):
    await _parity_check(dut, 25000000, 7, 75, 0)


@cocotb.test()
async def parity_hz25_d7_delay75_idle1(dut):
    await _parity_check(dut, 25000000, 7, 75, 1)


@cocotb.test()
async def parity_hz25_d5_delay55_idle1(dut):
    await _parity_check(dut, 25000000, 5, 55, 1)


@cocotb.test()
async def parity_hz25_d0_delay3_idle1(dut):
    await _parity_check(dut, 25000000, 0, 3, 1)


@cocotb.test()
async def parity_hz25_d0_delay3_idle0(dut):
    await _parity_check(dut, 25000000, 0, 3, 0)


@cocotb.test()
async def parity_hz10_d0_delay3_idle1(dut):
    await _parity_check(dut, 10000000, 0, 3, 1)


@cocotb.test()
async def parity_hz50_d0_delay3_idle1(dut):
    await _parity_check(dut, 50000000, 0, 3, 1)


@cocotb.test()
async def parity_hz50_d0_delay3_idle2(dut):
    await _parity_check(dut, 50000000, 0, 3, 2)


@cocotb.test()
async def swd_hp2_d0_idle1(dut):
    await case(dut, hz=25_000_000, sd=0, delay=3, idle=1)


@cocotb.test()
async def swd_hp2_d7_late_idle0(dut):
    await case(dut, hz=25_000_000, sd=7, delay=75, idle=0)


@cocotb.test()
async def swd_hp1_d0_idle1(dut):
    await case(dut, hz=50_000_000, sd=0, delay=3, idle=1)


@cocotb.test()
async def swd_hp5_d0_idle1(dut):
    await case(dut, hz=10_000_000, sd=0, delay=3, idle=1)


# --- 確かめた不具合の再現: idle_cycles > 0 だと、読みの最後の取り込み（遅れ d）が済む前に P_IDLE で SWDIO を駆動する ---
# hp 1・ターン 1・往復 45 ns（仕様の窓 d·T ∈ (25, 45) → d = 3, 4）、d = 4。idle 0 は終わりで取り込みを待つので正しい。
# idle 1 は P_IDLE（SWDIO = 0 を駆動）を取り込みより先に始め、パリティのビットを自分の駆動とぶつかった線から取る
# （模型では X。実機では自分の 0 か競合）。パリティ異常を見落とす。

@cocotb.test()
async def bug_idle0_parity_detected_hp1_d4_rt45(dut):
    r, log = await parity_only(dut, hz=50_000_000, sd=4, delay=45, idle=0)
    assert r[:2] == (2, 8), (r, log)


@cocotb.test()
async def bug_idle1_parity_detected_hp1_d4_rt45(dut):
    r, log = await parity_only(dut, hz=50_000_000, sd=4, delay=45, idle=1)
    assert r[:2] == (2, 8), (r, log)
