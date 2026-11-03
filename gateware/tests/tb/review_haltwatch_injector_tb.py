"""2026-11-03 のレビュー（停止の見張り）: 始動の列が S_WAIT_BOUNDARY で未接続になったとき、written_words が前の要求のまま残るか。"""

import cocotb
from cocotb.triggers import FallingEdge, ReadOnly, RisingEdge

from tests.tb.swd_injector_tb import DRCR, RESTART, Done, connected, inject, set_list
from tests.tb.dap_processor_tb import AP_TAR_W


@cocotb.test()
async def start_list_dropped_while_waiting_reports_zero_words(dut):
    dap, model = await connected(dut, seed=21)
    await dap.transfer((AP_TAR_W, 0x20000000))
    await set_list(dut, DRCR + RESTART)
    dut.inj_extra.value = 3
    assert await inject(dut) == 0                      # クロストリガの要求: 4 語（words_r = 4 が残る）
    d = Done(dut)
    task = cocotb.start_soon(dap.command(bytes([0x03])))   # DAP_Disconnect
    for _ in range(100_000):
        await RisingEdge(dut.clk)
        await ReadOnly()
        if dut.busy.value == 1:
            break
    else:
        raise TimeoutError("dap never busy")
    await FallingEdge(dut.clk)
    dut.inj_start_first.value, dut.inj_start_last.value, dut.inj_start_request.value = 4, 7, 1
    await FallingEdge(dut.clk)
    dut.inj_start_request.value = 0
    await task
    await d.wait(1)
    d.task.cancel()
    kind, result, words = d.done[0]
    assert d.writes == [], d.writes
    assert (kind, result) == (1, 2), d.done
    # 何も書いていないのに、受理された語の数が前の要求（4）のまま
    assert words == 0, "start list not connected but written_words=%d (stale from the previous request)" % words


@cocotb.test()
async def pending_is_still_set_in_the_done_cycle(dut):
    """fprobe_core は start_status.result・written_words・count を done の周期の終わりにレジスタに置く（その次の周期から見える）。
    pending が done の周期に既に 0 なら、その 1 周期に CSR を読むと「pending = 0 なのに前の始動の列の結果」が読める。"""
    dap, model = await connected(dut, seed=22)
    await dap.transfer((AP_TAR_W, 0x20000000))
    await set_list(dut, DRCR + RESTART)
    dut.inj_extra.value = 3
    await FallingEdge(dut.clk)
    dut.inj_start_first.value, dut.inj_start_last.value, dut.inj_start_request.value = 4, 7, 1
    await FallingEdge(dut.clk)
    dut.inj_start_request.value = 0
    for _ in range(200_000):
        await RisingEdge(dut.clk)
        await ReadOnly()
        if dut.inj_done.value == 1:
            break
    else:
        raise TimeoutError
    assert dut.inj_start_pending.value == 1, "pending already 0 in the done cycle (fprobe_core's start_status still old)"
