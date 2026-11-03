"""swd_injector（ホストの DAP コマンドの境界での割り込み書き込み）と dap_monitor（番地一致）のテストベンチ。
dap_harness（dap_processor + swd_injector + dap_monitor + swd_phy）と SWD ターゲットモデルで試す。"""

import struct

import cocotb
from cocotb.triggers import ClockCycles, FallingEdge, ReadOnly, RisingEdge

from tests.models.swd_target import AP_IDR
from tests.tb.common import PulseCounter
from tests.tb.dap_processor_tb import (AP_DRW_R, AP_DRW_W, AP_TAR_W, DP_CTRL_R, DP_SELECT_W, OK, Dap, setup)

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
    # ホストが CSW・TAR を書いた後は、控えを戻すだけで CSW・TAR を読まない（停止の書き込みまで 4 転送、全体 8 転送）。
    # まだ TAR を書いていなければ、従来どおり読んで保存する（全体 11 転送）。どちらも最後の書き込みの後に CTRL/STAT を
    # 1 回読む（書き込みの誤りはポストされるので、読まないと分からない。2026-10-29 の halt-watch-design §3.2）
    dap, model = await connected(dut, seed=3)              # CSW は書いた、TAR はまだ
    n = len(model.transactions)
    assert await inject(dut) == 0
    slow = model.transactions[n:]
    assert len(slow) == 11 and sum(1 for t in slow if t[1]) == 4    # CSW・TAR・RDBUFF・CTRL/STAT を読む
    assert int(dut.inj_transfers.value) == 7                       # R20: 停止の書き込みまでの転送数（読んで保存する経路）
    await dap.transfer((AP_TAR_W, 0x20000100))
    n = len(model.transactions)
    assert await inject(dut) == 0
    fast = model.transactions[n:]
    assert len(fast) == 8 and [t[:3] for t in fast if t[1]] == [(0, 1, 0x4)]   # 読みは CTRL/STAT だけ
    assert [t[0] for t in fast[:4]] == [0, 1, 1, 1]                 # SELECT, CSW, TAR, DRW（停止）
    assert int(dut.inj_transfers.value) == 4                       # R20: 控えを使う経路
    model.wait_next = 2                                            # 次の AP アクセス 2 回は WAIT: 繰り返しも数える
    assert await inject(dut) == 0
    assert int(dut.inj_transfers.value) == 6
    assert model.memory[DHCSR] == HALT
    assert (await dap.block(AP_DRW_W, 1, [0x55]))[:2] == (1, OK)  # TAR が戻っている
    assert model.memory[0x20000100] == 0x55


async def set_list(dut, words):
    """書く語の列: 語 0 は inj_addr / inj_value（今までの 1 語）、語 1〜 は表（inj_tbl_*）、inj_extra = 語の数 − 1。"""
    dut.inj_addr.value, dut.inj_value.value = words[0]
    for i, (a, v) in enumerate(words[1:], start=1):
        await FallingEdge(dut.clk)
        dut.inj_tbl_write.value, dut.inj_tbl_index.value, dut.inj_tbl_addr.value, dut.inj_tbl_value.value = 1, i, a, v
    await FallingEdge(dut.clk)
    dut.inj_tbl_write.value = 0
    dut.inj_extra.value = len(words) - 1


# Cortex-A7 × 4（BCM2836）の DBGDRCR に HRQ（停止要求）
DRCR = [(0x80010090 + 0x2000 * n, 1) for n in range(4)]


@cocotb.test()
async def a_list_of_words_is_written_in_order_and_the_host_state_is_restored(dut):
    dap, model = await connected(dut, seed=5)
    await dap.transfer((AP_TAR_W, 0x20000000))
    await dap.transfer((DP_SELECT_W, 0xF0))
    await set_list(dut, DRCR)
    n = len(model.transactions)
    pulses = []

    async def watch():
        while True:
            await RisingEdge(dut.clk)
            await ReadOnly()
            if dut.inj_written.value == 1:
                pulses.append((int(dut.inj_written_addr.value), int(dut.inj_written_value.value)))
    task = cocotb.start_soon(watch())
    assert await inject(dut) == 0
    task.cancel()
    assert pulses == DRCR                                    # 書き込みごとに、その語の番地と値（イベントに使う）
    drw = [(t[4]) for t in model.transactions[n:] if t[:3] == (1, 0, 0xC)]
    assert drw == [1, 1, 1, 1] and all(model.memory[a] == v for a, v in DRCR)
    tars = [t[4] for t in model.transactions[n:] if t[:3] == (1, 0, 0x4)]
    assert tars[:4] == [a for a, _ in DRCR]                  # 表の順に書く
    assert int(dut.inj_written_words.value) == 4 and int(dut.inj_count.value) == 1
    assert await dap.transfer((AP_IDR_R, None)) == (1, OK, [AP_IDR])            # SELECT が戻っている
    await dap.transfer((DP_SELECT_W, 0))
    assert (await dap.block(AP_DRW_W, 1, [7]))[:2] == (1, OK)                   # TAR（0x20000000）が戻っている
    assert model.memory[0x20000000] == 7


@cocotb.test()
async def a_fault_in_the_list_stops_it_and_says_how_many_words_were_accepted(dut):
    dap, model = await connected(dut, seed=6)
    await dap.transfer((AP_TAR_W, 0x20000000))
    model.fault_addresses.add(DRCR[1][0])                    # 語 1 の書き込みがバスの誤り（ポスト: 次の AP アクセスで FAULT）
    await set_list(dut, DRCR)
    assert await inject(dut) == 1
    assert int(dut.inj_written_words.value) == 2             # 受理された DRW は語 0・1（語 1 が誤り）
    assert DRCR[2][0] not in model.memory and DRCR[3][0] not in model.memory   # 誤りの後は書かない
    model.fault_addresses.clear()
    count, resp, (ctrl,) = await dap.transfer((DP_CTRL_R, None))
    assert (count, resp) == (1, OK) and not ctrl & 0x20       # 起こした STICKYERR は消してある


@cocotb.test()
async def a_write_error_on_the_last_word_is_found_by_ctrl_stat(dut):
    dap, model = await connected(dut, seed=7)
    await dap.transfer((AP_TAR_W, 0x20000000))
    model.fault_addresses.add(DRCR[3][0])                    # 最後の語: この後に AP アクセスは無い
    await set_list(dut, DRCR)
    assert await inject(dut) == 1                            # CTRL/STAT の STICKYERR で分かる
    assert int(dut.inj_written_words.value) == 4
    model.fault_addresses.clear()
    count, resp, (ctrl,) = await dap.transfer((DP_CTRL_R, None))
    assert (count, resp) == (1, OK) and not ctrl & 0x20


async def inject_and_cut(dut, dap, model, cut: bytes) -> int:
    """割り込み書き込みが転送の応答を待っている最中（AP の WAIT を繰り返している間）に、ホストの DAP コマンド cut
    （DAP_Disconnect・方式の切り替えの DAP_Connect。どちらも PHY を使わないので、割り込みが線を借りていても処理される）を送る。
    PHY は enable = '0'（または方式の切り替え）で転送を捨てて応答を出さないので、割り込みは線を放して終わること。"""
    await dap.command(bytes([0x04, 0]) + struct.pack("<HH", 0xFFFF, 3))   # WAIT をほぼ無限に繰り返す
    model.wait_next = 10 ** 9                                             # AP のアクセスはすべて WAIT
    await FallingEdge(dut.clk)
    dut.inj_request.value = 1
    await FallingEdge(dut.clk)
    dut.inj_request.value = 0
    await ClockCycles(dut.clk, 3000)                                       # SELECT の後、AP の WAIT を繰り返している
    assert dut.inj_busy.value == 1
    assert await dap.command(cut) == cut[:1] + bytes([cut[1] if cut[0] == 0x02 else 0])
    for _ in range(100_000):
        await RisingEdge(dut.clk)
        await ReadOnly()
        if dut.inj_busy.value == 0:
            break
    else:
        raise TimeoutError("swd_injector still holds the line after the cut")
    result = int(dut.inj_result.value)
    await FallingEdge(dut.clk)
    model.wait_next = 0
    return result


async def works_again(dap, model):
    """線が戻っている: 接続し直すと、ホストの DAP 命令が PHY に届く。"""
    await dap.connect()
    await dap.mem_setup()
    assert await dap.transfer((AP_TAR_W, 0x20000040), (AP_DRW_W, 0x1234)) == (2, OK, [])
    assert model.memory[0x20000040] == 0x1234


@cocotb.test()
async def a_disconnect_while_waiting_for_a_response_releases_the_line(dut):
    dap, model = await connected(dut, seed=8)
    assert await inject_and_cut(dut, dap, model, bytes([0x03])) == 2   # DAP_Disconnect: 後始末はできない（未接続）
    assert int(dut.inj_count.value) == 0
    await works_again(dap, model)
    assert await inject(dut) == 0                                       # 次の割り込みもできる


@cocotb.test()
async def a_switch_to_jtag_while_waiting_for_a_response_releases_the_line(dut):
    dap, model = await connected(dut, seed=9)
    assert await inject_and_cut(dut, dap, model, bytes([0x02, 2])) == 2  # DAP_Connect(JTAG): SWD の転送は捨てられる
    assert await dap.command(bytes([0x02, 1])) == bytes([0x02, 1])
    await works_again(dap, model)
    assert await inject(dut) == 0


# 始動の列（docs/superpowers/specs/2026-11-03-halt-watch-fpga-restart-design.md §3.2）: 表の語 first〜last を、ホストの要求で書く。
# 停止の見張りでは、語 0〜3 = 各コアの DBGDRCR に HRQ（クロストリガ）、語 4〜7 = 各コアの DBGDRCR に RRQ | CSE（始動の列 = 再開）
RESTART = [(0x80010090 + 0x2000 * n, 0b110) for n in range(4)]


class Done:
    """done の周期ごとに (kind_start, result, written_words) を記録し、DRW の書き込み（番地, 値）を順に記録する。"""

    def __init__(self, dut):
        self.dut, self.done, self.writes = dut, [], []
        self.task = cocotb.start_soon(self._run())

    async def _run(self):
        d = self.dut
        while True:
            await RisingEdge(d.clk)
            await ReadOnly()
            if d.inj_written.value == 1:
                self.writes.append((int(d.inj_written_addr.value), int(d.inj_written_value.value)))
            if d.inj_done.value == 1:
                self.done.append((int(d.inj_kind_start.value), int(d.inj_result.value), int(d.inj_written_words.value)))

    async def wait(self, n, timeout=200_000):
        for _ in range(timeout):
            if len(self.done) >= n:
                return
            await RisingEdge(self.dut.clk)
        raise TimeoutError(self.done)


async def request_start(dut, first, last):
    await FallingEdge(dut.clk)
    dut.inj_start_first.value, dut.inj_start_last.value, dut.inj_start_request.value = first, last, 1
    await FallingEdge(dut.clk)
    dut.inj_start_request.value = 0


@cocotb.test()
async def a_start_list_writes_its_words_and_says_it_is_done(dut):
    """始動の列は表の first〜last だけを順に書く（語 0〜3 は書かない）。要求してから終わるまで pending、終わった周期に done
    （kind_start = 1・結果・受理された語の数）。書き方（控え・戻し）はクロストリガの要求と同じ。"""
    dap, model = await connected(dut, seed=10)
    await dap.transfer((AP_TAR_W, 0x20000000))
    await set_list(dut, DRCR + RESTART)
    dut.inj_extra.value = 3                                   # クロストリガの要求は語 0〜3
    d = Done(dut)
    await request_start(dut, 4, 7)
    await ReadOnly()
    assert dut.inj_start_pending.value == 1
    await d.wait(1)
    await FallingEdge(dut.clk)
    assert d.done == [(1, 0, 4)] and d.writes == RESTART
    assert dut.inj_start_pending.value == 0
    assert all(model.memory[a] == v for a, v in RESTART)
    assert int(dut.inj_count.value) == 1
    assert (await dap.block(AP_DRW_W, 1, [9]))[:2] == (1, OK)  # ホストの TAR（0x20000000）が戻っている
    assert model.memory[0x20000000] == 9
    d.task.cancel()


@cocotb.test()
async def a_cross_trigger_request_in_the_same_cycle_goes_first(dut):
    """クロストリガの要求と始動の列が同じ周期に来たら、クロストリガ（停止）を先に書き、始動の列は待って後で書く。"""
    dap, model = await connected(dut, seed=11)
    await dap.transfer((AP_TAR_W, 0x20000000))
    await set_list(dut, DRCR + RESTART)
    dut.inj_extra.value = 3
    d = Done(dut)
    await FallingEdge(dut.clk)
    dut.inj_start_first.value, dut.inj_start_last.value = 4, 7
    dut.inj_request.value, dut.inj_start_request.value = 1, 1
    await FallingEdge(dut.clk)
    dut.inj_request.value, dut.inj_start_request.value = 0, 0
    await d.wait(2)
    assert d.done == [(0, 0, 4), (1, 0, 4)] and d.writes == DRCR + RESTART
    d.task.cancel()


@cocotb.test()
async def a_start_request_while_busy_waits_and_runs_after(dut):
    """書き込みの最中に来た始動の列の要求は捨てず、終わってから書く（クロストリガの要求は今どおり捨てる）。"""
    dap, model = await connected(dut, seed=12)
    await dap.transfer((AP_TAR_W, 0x20000000))
    await set_list(dut, DRCR + RESTART)
    dut.inj_extra.value = 3
    d = Done(dut)
    await FallingEdge(dut.clk)
    dut.inj_request.value = 1
    await FallingEdge(dut.clk)
    dut.inj_request.value = 0
    await ClockCycles(dut.clk, 50)
    assert dut.inj_busy.value == 1
    await request_start(dut, 5, 6)                           # 2 語だけ
    await d.wait(2)
    assert d.done == [(0, 0, 4), (1, 0, 2)] and d.writes == DRCR + RESTART[1:3]
    d.task.cancel()


@cocotb.test()
async def a_start_without_connection_reports_it(dut):
    """未接続: 始動の列は書かずに終わり（done・結果 2）、pending は消える。"""
    await setup(dut)
    d = Done(dut)
    await request_start(dut, 4, 7)
    await d.wait(1)
    await FallingEdge(dut.clk)
    assert d.done == [(1, 2, 0)] and d.writes == [] and dut.inj_start_pending.value == 0
    d.task.cancel()
