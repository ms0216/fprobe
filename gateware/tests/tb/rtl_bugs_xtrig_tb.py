"""「条件で MCU を止める」経路（swd_injector・dap_monitor の控え・dap_sampler との競合）の不具合の再現試験。
台帳: docs/reports/bugs/rtl-xtrig.md。ここの試験は「あるべき動作」を書いてあり、現状の RTL では失敗する
（tests/test_rtl_bugs_xtrig.py が xfail(strict=True) で包む）。直したら xfail を外す。"""

import random
import struct

import cocotb
from cocotb.triggers import ClockCycles, ReadOnly, RisingEdge

from tests.models.swd_target import STKERRCLR
from tests.tb.dap_processor_tb import AP_CSW_W, AP_DRW_R, AP_DRW_W, AP_TAR_W, DP_ABORT_W, DP_SELECT_W, OK, WAIT
from tests.tb.dap_sampler_tb import collect, configure, rounds
from tests.tb.swd_injector_tb import AP_IDR_R, DHCSR, HALT, connected, inject

AP_IDR = 0x24770011


async def settle(dut, cycles=6000):
    await ClockCycles(dut.clk, cycles)


@cocotb.test()
async def bug_inj_1_fault_leaves_csw_and_tar_modified(dut):
    """BUG-RTL-INJ-1: 注入の DRW が FAULT だと、CSW（増加なし）・TAR（DHCSR）を戻さずに終わる。"""
    dap, model = await connected(dut, seed=21)
    await dap.transfer((AP_TAR_W, 0x20000100))
    host_csw = model.csw
    assert host_csw & 0x30 == 0x10
    model.fault_addresses.add(DHCSR)
    assert await inject(dut) == 1
    await settle(dut)
    assert model.csw == host_csw, hex(model.csw)             # ホストの CSW（自動増加）が残っている
    assert model.tar == 0x20000100, hex(model.tar)           # ホストの TAR が残っている


@cocotb.test()
async def bug_inj_2_select_restore_is_not_retried_after_a_failure(dut):
    """BUG-RTL-INJ-2: 失敗の後始末の SELECT の復帰が WAIT を返しても再試行せず、SELECT が ap のまま残る。"""
    dap, model = await connected(dut, seed=22)
    # DAP_TransferConfigure: アイドル 0、WAIT の再試行 2、一致の再試行 0
    assert await dap.command(bytes([0x04, 0]) + struct.pack("<HH", 2, 0)) == bytes([0x04, 0])
    await dap.transfer((DP_SELECT_W, 0xF0), (AP_CSW_W, 0x23000012), (AP_TAR_W, 0x20000100))   # CSW・TAR の控えを持つ
    state = {"cur": None, "left": 0, "armed": True}
    orig_tx, orig_ack = model._transaction, model._ack

    async def tx(ap, rnw, addr):
        state["cur"] = (ap, rnw, addr)
        await orig_tx(ap, rnw, addr)

    def ack(ap):
        if state["left"]:
            state["left"] -= 1
            return WAIT
        if state["armed"] and state["cur"] == (1, 0, 0xC):    # 注入の DRW の書き込み: 3 回（再試行 2 回の分）と、続く 1 回が WAIT
            state["armed"] = False
            state["left"] = 3
            return WAIT
        return orig_ack(ap)

    model._transaction, model._ack = tx, ack
    assert await inject(dut) == 1
    await settle(dut)
    assert model.select == 0xF0, hex(model.select)            # ホストの SELECT（bank 0xF）に戻っている


@cocotb.test()
async def bug_inj_3_injection_into_another_ap_writes_the_hosts_csw_tar(dut):
    """BUG-RTL-INJ-3: ホストが AP1 の CSW・TAR を書いた後、ap = 0 への注入が AP1 の控えを AP0 の CSW・TAR に書く
    （AP0 の CSW・TAR は読まずに上書き）。あるべき動作: 控えの AP が注入先と違えば、CSW・TAR を読んで保存する。"""
    dap, model = await connected(dut, seed=23)
    await dap.transfer((DP_SELECT_W, 0x01000000), (AP_CSW_W, 0x23000012), (AP_TAR_W, 0x20000100))
    n = len(model.transactions)
    dut.inj_ap.value = 0
    assert await inject(dut) == 0
    mine = model.transactions[n:]
    reads = [t for t in mine if t[0] == 1 and t[1] == 1]
    assert [t[2] for t in reads] == [0x0, 0x4], mine           # AP0 の CSW・TAR の読み（RDBUFF は DP の読み）


@cocotb.test()
async def bug_smp_1_sampler_on_another_ap_writes_the_hosts_csw_tar(dut):
    """BUG-RTL-SMP-1: BUG-RTL-INJ-3 のサンプラ版。ホストの控えは AP1 のものなのに、サンプラ（ap = 0）が AP0 の CSW・TAR を
    読まずに上書きし、戻しでも AP1 の控えの値を AP0 に書く。"""
    dap, model = await connected(dut, seed=24)
    model.memory[0x20000000] = 5
    await dap.transfer((DP_SELECT_W, 0x01000000), (AP_CSW_W, 0x23000012), (AP_TAR_W, 0x20000100))
    n = len(model.transactions)
    got = []
    task = collect(dut, got)
    await configure(dut, [(0x20000000, 1)], period=3000, ap=0)
    dut.smp_run.value = 1
    await rounds(dut, 1)
    dut.smp_run.value = 0
    await settle(dut)
    task.cancel()
    mine = model.transactions[n:]
    reads = [t for t in mine if t[0] == 1 and t[1] == 1 and t[2] in (0x0, 0x4)]   # AP の CSW・TAR の読み
    assert reads, mine


@cocotb.test()
async def injection_during_sampling_and_host_traffic_keeps_host_state(dut):
    """サンプラが常に線を持つ構成で、注入とホストの転送をランダムな時刻に重ねても、ホストから見た SELECT・CSW・TAR は
    ホストの値のまま、注入は毎回 DHCSR に停止を書く。"""
    dap, model = await connected(dut, seed=25)
    model.memory[0x20000000] = 0x11
    model.memory[0x20000004] = 0x22
    rng = random.Random(25)
    got = []
    task = collect(dut, got)
    await configure(dut, [(0x20000000, 2)], period=1)
    dut.smp_run.value = 1
    tar = 0x20000600
    await dap.transfer((AP_TAR_W, tar))
    for k in range(40):
        await ClockCycles(dut.clk, rng.randrange(0, 1500))
        model.memory.pop(DHCSR, None)
        # 注入の要求を出してから、ホストの転送もすぐ重ねる
        req = cocotb.start_soon(inject(dut, timeout=400_000))
        await ClockCycles(dut.clk, rng.randrange(0, 600))
        if k % 2 == 0:
            assert await dap.transfer((DP_SELECT_W, 0xF0), (AP_IDR_R, None), (DP_SELECT_W, 0)) == (3, OK, [AP_IDR])
        value = rng.getrandbits(32)
        assert (await dap.block(AP_DRW_W, 1, [value]))[:2] == (1, OK)
        assert model.memory[tar] == value
        tar += 4
        assert await req == 0
        assert model.memory[DHCSR] == HALT, k
        assert await dap.transfer((0x03, None), (0x07, None)) == (2, OK, [0x23000012, tar])
    dut.smp_run.value = 0
    await settle(dut)
    task.cancel()
    assert int(dut.smp_errors.value) == 0


@cocotb.test()
async def bug_mon_1_shadow_survives_a_disconnect_and_reconnect(dut):
    """BUG-RTL-MON-1: DAP_Disconnect → DAP_Connect の後も dap_monitor の控え（CSW・TAR）が残り、注入が控えを使う
    （CSW・TAR を読まない）。接続し直した後の AP の CSW・TAR はホストが書き直すまで不明。あるべき動作: 読んで保存する。"""
    dap, model = await connected(dut, seed=26)
    await dap.transfer((AP_TAR_W, 0x20000100))
    assert await dap.command(bytes([0x03])) == bytes([0x03, 0])          # DAP_Disconnect
    await dap.connect()
    n = len(model.transactions)
    assert await inject(dut) == 0
    reads = [t for t in model.transactions[n:] if t[0] == 1 and t[1] == 1]
    assert [t[2] for t in reads] == [0x0, 0x4], model.transactions[n:]   # CSW・TAR の読み


@cocotb.test()
async def injection_fuzz_with_waits_and_many_slots(dut):
    """注入・ホストの転送・サンプラ（複数か所・複数語・ゆらぎ）・WAIT をランダムに重ねても、ホストの状態は保たれる。"""
    dap, model = await connected(dut, seed=27)
    assert await dap.command(bytes([0x04, 0]) + struct.pack("<HH", 8, 0)) == bytes([0x04, 0])   # WAIT は 8 回まで再試行
    for i in range(9):
        model.memory[0x20000000 + 4 * i] = 0x100 + i
    rng = random.Random(27)
    got = []
    task = collect(dut, got)
    await configure(dut, [(0x20000000, 4), (0x20000010, 3), (0x20000020, 1)], period=1)
    dut.smp_jitter.value = 3
    dut.smp_run.value = 1
    tar = 0x20000600
    await dap.transfer((AP_TAR_W, tar))
    for k in range(40):
        await ClockCycles(dut.clk, rng.randrange(0, 1500))
        model.memory.pop(DHCSR, None)
        model.wait_next = rng.randrange(0, 4)
        req = cocotb.start_soon(inject(dut, timeout=600_000))
        await ClockCycles(dut.clk, rng.randrange(0, 800))
        if k % 3 == 0:
            assert await dap.transfer((DP_SELECT_W, 0xF0), (AP_IDR_R, None), (DP_SELECT_W, 0)) == (3, OK, [AP_IDR])
        value = rng.getrandbits(32)
        assert (await dap.block(AP_DRW_W, 1, [value]))[:2] == (1, OK)
        assert model.memory[tar] == value
        tar += 4
        assert await req == 0
        assert model.memory[DHCSR] == HALT, k
        assert await dap.transfer((0x03, None), (0x07, None)) == (2, OK, [0x23000012, tar])
    dut.smp_run.value = 0
    await settle(dut)
    task.cancel()
    assert int(dut.smp_errors.value) == 0
    assert all(d == 0x100 + (a - 0x20000000) // 4 for a, d, k in got if k == OK)
