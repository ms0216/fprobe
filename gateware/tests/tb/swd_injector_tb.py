"""swd_injector（ホストの DAP コマンドの境界での割り込み書き込み）と dap_monitor（番地一致）のテストベンチ。
dap_harness（dap_processor + swd_injector + dap_monitor + swd_phy）と SWD ターゲットモデルで試す。"""

import cocotb
from cocotb.triggers import ClockCycles, FallingEdge, ReadOnly, RisingEdge

from tests.models.swd_target import AP_IDR
from tests.tb.common import PulseCounter
from tests.tb.dap_processor_tb import (AP_DRW_R, AP_DRW_W, AP_TAR_W, DP_SELECT_W, OK, Dap, setup)

DHCSR = 0xE000EDF0
HALT = 0xA05F0003
AP_IDR_R = 0x0F                                   # bank 0xF の A = 0xC（読み）


async def inject(dut, *, timeout=100_000) -> int:
    await FallingEdge(dut.clk)
    dut.inj_request.value = 1
    await FallingEdge(dut.clk)
    dut.inj_request.value = 0
    for _ in range(timeout):
        await RisingEdge(dut.clk)
        await ReadOnly()
        if dut.inj_busy.value == 0:
            result = int(dut.inj_result.value)
            await FallingEdge(dut.clk)
            return result
    raise TimeoutError


async def connected(dut, seed=1) -> tuple[Dap, object]:
    dap, model = await setup(dut, seed=seed)
    await dap.connect()
    await dap.mem_setup()                         # CSW: 32 ビット・自動増加
    return dap, model


@cocotb.test()
async def injection_writes_and_restores_host_state(dut):
    dap, model = await connected(dut)
    await dap.transfer((AP_TAR_W, 0x20000000))
    assert (await dap.block(AP_DRW_W, 2, [1, 2]))[:2] == (2, OK)
    await dap.transfer((DP_SELECT_W, 0xF0))       # ホストは bank 0xF を選んだまま
    assert await inject(dut) == 0
    assert model.memory[DHCSR] == HALT and int(dut.inj_count.value) == 1
    # SELECT（bank 0xF）が戻っている
    assert await dap.transfer((AP_IDR_R, None)) == (1, OK, [AP_IDR])
    # CSW（自動増加）と TAR（0x20000008）が戻っている: 続きを書くと続きの番地に入る
    await dap.transfer((DP_SELECT_W, 0))
    assert (await dap.block(AP_DRW_W, 2, [3, 4]))[:2] == (2, OK)
    assert [model.memory[0x20000000 + 4 * i] for i in range(4)] == [1, 2, 3, 4]


@cocotb.test()
async def injection_waits_for_a_command_boundary(dut):
    dap, model = await connected(dut, seed=2)
    words = list(range(1, 301))
    await dap.transfer((AP_TAR_W, 0x20000000))
    task = cocotb.start_soon(dap.block(AP_DRW_W, len(words), words))
    await ClockCycles(dut.clk, 3000)              # ブロック転送の途中で要求
    assert await inject(dut, timeout=1_000_000) == 0
    assert (await task)[:2] == (300, OK)
    assert [model.memory[0x20000000 + 4 * i] for i in range(300)] == words
    assert model.memory[DHCSR] == HALT


@cocotb.test()
async def injection_without_connection_reports_it(dut):
    await setup(dut)
    assert await inject(dut) == 2
    assert int(dut.inj_count.value) == 0


@cocotb.test()
async def monitor_matches_address_direction_and_value(dut):
    dap, model = await connected(dut, seed=3)
    matches = PulseCounter(dut, dut.match)
    dut.match_addr.value = 0x20000010
    dut.addr_mask.value = 0xFFFFFFFC
    dut.on_write.value = 1
    await dap.transfer((AP_TAR_W, 0x20000000))
    await dap.block(AP_DRW_W, 8, list(range(8)))            # 0x20000010 には 4 を書く
    assert matches.count == 1
    dut.match_data.value = 5
    dut.data_mask.value = 0xFFFFFFFF                        # 値 5 のときだけ
    await dap.transfer((AP_TAR_W, 0x20000000))
    await dap.block(AP_DRW_W, 8, list(range(8)))
    assert matches.count == 1
    await dap.transfer((AP_TAR_W, 0x20000010), (AP_DRW_W, 5))
    assert matches.count == 2
    dut.on_write.value = 0
    dut.on_read.value = 1
    await dap.transfer((AP_TAR_W, 0x2000000C))
    await dap.block(AP_DRW_R, 2)                             # 0x2000000C, 0x20000010
    assert matches.count == 3
    # 割り込みの転送は数えない（ホストの転送だけを見る）
    dut.match_addr.value = DHCSR
    dut.on_write.value = 1
    dut.data_mask.value = 0
    assert await inject(dut) == 0
    assert matches.count == 3


@cocotb.test()
async def every_transfer_becomes_an_event(dut):
    dap, model = await connected(dut, seed=4)
    events = []

    async def collect():
        while True:
            await RisingEdge(dut.clk)
            await ReadOnly()
            if dut.ev_valid.value == 1:
                events.append((int(dut.ev_request.value), int(dut.ev_ack.value), int(dut.ev_addr.value),
                               int(dut.ev_data.value)))

    task = cocotb.start_soon(collect())
    await dap.transfer((AP_TAR_W, 0x20000100))
    await dap.block(AP_DRW_W, 3, [0xA, 0xB, 0xC])
    model.wait_next = 2                                       # 次の AP アクセス 2 回は WAIT
    await dap.transfer((AP_TAR_W, 0x20000104))
    await dap.block(AP_DRW_R, 2)
    await ClockCycles(dut.clk, 20)
    task.cancel()
    writes = [(a, d) for r, k, a, d in events if r == AP_DRW_W & 0xF and k == OK]
    assert writes == [(0x20000100, 0xA), (0x20000104, 0xB), (0x20000108, 0xC)]   # DRW の番地は TAR から
    assert sum(1 for r, k, a, d in events if k == 0b010) == 2                     # WAIT も 1 件ずつ
    # 線上の読み: AP の読みはポスト（値は 1 つ前の読みの結果）、最後は DP の RDBUFF で回収
    reads = [(r, a, d) for r, k, a, d in events if r & 0b0010 and k == OK]
    assert reads[-2:] == [(AP_DRW_R & 0xF, 0x20000108, 0xB), (0b1110, 0xC, 0xC)]


@cocotb.test()
async def injection_uses_the_host_state_shadow(dut):
    # ホストが CSW・TAR を書いた後は、控えを戻すだけで読まない（停止の書き込みまで 4 転送、全体 7 転送）。
    # まだ TAR を書いていなければ、従来どおり読んで保存する（全体 10 転送）
    dap, model = await connected(dut, seed=3)              # CSW は書いた、TAR はまだ
    n = len(model.transactions)
    assert await inject(dut) == 0
    slow = model.transactions[n:]
    assert len(slow) == 10 and sum(1 for t in slow if t[1]) == 3    # CSW・TAR・RDBUFF を読む
    assert int(dut.inj_transfers.value) == 7                       # R20: 停止の書き込みまでの転送数（読んで保存する経路）
    await dap.transfer((AP_TAR_W, 0x20000100))
    n = len(model.transactions)
    assert await inject(dut) == 0
    fast = model.transactions[n:]
    assert len(fast) == 7 and not any(t[1] for t in fast)          # 読みが無い
    assert [t[0] for t in fast[:4]] == [0, 1, 1, 1]                 # SELECT, CSW, TAR, DRW（停止）
    assert int(dut.inj_transfers.value) == 4                       # R20: 控えを使う経路
    model.wait_next = 2                                            # 次の AP アクセス 2 回は WAIT: 繰り返しも数える
    assert await inject(dut) == 0
    assert int(dut.inj_transfers.value) == 6
    assert model.memory[DHCSR] == HALT
    assert (await dap.block(AP_DRW_W, 1, [0x55]))[:2] == (1, OK)  # TAR が戻っている
    assert model.memory[0x20000100] == 0x55
