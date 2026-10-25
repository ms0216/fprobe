"""jtag_phy のテストベンチ。チェーンは STM32 と同じ: TDO 側に JTAG-DP（装置 0、IR 4）、TDI 側にバウンダリスキャン
TAP（装置 1、IR 5）。"""

import cocotb
from cocotb.triggers import RisingEdge

from tests.models.jtag_target import BSC_IDCODE, JTAG_DP_IDCODE, Bypass, JtagChain, JtagDp
from tests.models.swd_target import CDBGPWRUPACK, CDBGPWRUPREQ
from tests.tb.common import start_clock_and_reset

OP_SEQ_OUT, OP_SEQ_IN, OP_TRANSFER, OP_PINS = 0, 1, 2, 3
OK, WAIT = 0b001, 0b010
DP_R, DP_W, AP_R, AP_W = 0b0010, 0b0000, 0b0011, 0b0001
A_CTRL, A_SELECT, A_RDBUFF = 0x4, 0x8, 0xC
A_CSW, A_TAR, A_DRW = 0x0, 0x4, 0xC


def request(kind: int, addr: int) -> int:
    return kind | (addr >> 2) << 2


class Phy:
    def __init__(self, dut):
        self.dut = dut

    async def command(self, op, *, count=1, req=0, data=0):
        d = self.dut
        d.cmd_op.value, d.cmd_count.value, d.cmd_request.value, d.cmd_data.value = op, count, req, data
        d.cmd_valid.value = 1
        while True:
            await RisingEdge(d.clk)
            if d.cmd_ready.value == 1:
                break
        d.cmd_valid.value = 0
        while True:
            await RisingEdge(d.clk)
            if d.rsp_valid.value == 1:
                return int(d.rsp_ack.value), int(d.rsp_data.value)

    async def transfer(self, kind, addr, data=0):
        return await self.command(OP_TRANSFER, req=request(kind, addr), data=data)


async def setup(dut, idle_cycles=0, tdo_delay_ns=0, sample_delay=0):
    dp = JtagDp()
    chain = JtagChain(dut, [dp, Bypass()], tdo_delay_ns=tdo_delay_ns)
    dut.enable.value = 1
    dut.half_period.value = 2
    dut.sample_delay.value = sample_delay
    dut.idle_cycles.value = idle_cycles
    dut.dr_before.value, dut.dr_after.value = 0, 1
    dut.ir_before.value, dut.ir_after.value, dut.ir_len.value = 0, 5, 4
    dut.cmd_valid.value = 0
    await start_clock_and_reset(dut)
    cocotb.start_soon(chain.run())
    phy = Phy(dut)
    await phy.command(OP_SEQ_IN, count=6, req=1, data=0x3F)          # Test-Logic-Reset
    await phy.command(OP_SEQ_IN, count=1, req=0, data=1)             # Run-Test/Idle
    return phy, dp


@cocotb.test()
async def sequence_reads_both_idcodes(dut):
    phy, _ = await setup(dut)                                        # リセット後は両方 IDCODE が選ばれている
    await phy.command(OP_SEQ_IN, count=1, req=1)                     # Select-DR
    await phy.command(OP_SEQ_IN, count=2, req=0)                     # Capture-DR → Shift-DR
    _, tdo = await phy.command(OP_SEQ_IN, count=64, req=0, data=(1 << 64) - 1)
    assert tdo & 0xFFFFFFFF == JTAG_DP_IDCODE and tdo >> 32 == BSC_IDCODE


@cocotb.test()
async def late_tdo_within_a_period(dut):
    # TDO は立ち下がりから次の立ち下がりまで保たれるので、取り込みは次の立ち下がりの直前（din = その 1 周期前のピン）。
    # half_period = 2 の 1 ビットは 5 周期（50 ns）: 立ち下がりから 35 ns 遅れて変わる TDO も読める
    phy, dp = await setup(dut, tdo_delay_ns=35)
    await phy.command(OP_SEQ_IN, count=1, req=1)                     # Select-DR
    await phy.command(OP_SEQ_IN, count=2, req=0)                     # Capture-DR → Shift-DR
    _, tdo = await phy.command(OP_SEQ_IN, count=64, req=0, data=(1 << 64) - 1)
    assert tdo & 0xFFFFFFFF == JTAG_DP_IDCODE and tdo >> 32 == BSC_IDCODE
    await phy.command(OP_SEQ_IN, count=2, req=1)                     # Exit1 → Update
    await phy.command(OP_SEQ_IN, count=1, req=0)                     # Run-Test/Idle
    assert await phy.transfer(DP_R, 0x0) == (OK, JTAG_DP_IDCODE)
    assert (await phy.transfer(DP_W, A_CTRL, CDBGPWRUPREQ))[0] == OK
    ack, ctrl = await phy.transfer(DP_R, A_CTRL)
    assert ack == OK and ctrl & CDBGPWRUPACK


async def read_idcodes(phy):
    await phy.command(OP_SEQ_IN, count=1, req=1)                     # Select-DR
    await phy.command(OP_SEQ_IN, count=2, req=0)                     # Capture-DR → Shift-DR
    _, tdo = await phy.command(OP_SEQ_IN, count=64, req=0, data=(1 << 64) - 1)
    await phy.command(OP_SEQ_IN, count=2, req=1)                     # Exit1 → Update
    await phy.command(OP_SEQ_IN, count=1, req=0)                     # Run-Test/Idle
    return tdo & 0xFFFFFFFF, tdo >> 32


@cocotb.test()
async def tdo_later_than_a_period_needs_sample_delay(dut):
    # 立ち下がりから 55 ns（1 周期 50 ns より長い）遅れて変わる TDO（Pi 2B の 14.3 MHz 以上で見た形）: High 相の終わりでは
    # 前のビットを読む（1 ビットずれる）
    phy, _ = await setup(dut, tdo_delay_ns=55)
    dp_id, _ = await read_idcodes(phy)
    assert dp_id == (JTAG_DP_IDCODE << 1 | 1) & 0xFFFFFFFF


@cocotb.test()
async def sample_delay_reads_tdo_later_than_a_period(dut):
    # 取り込みを High 相の終わりから 3 周期（30 ns）遅らせると読める。遅れた取り込みは次のビットの出力と重なるが、
    # TDI（IR・DR の書き込み）は乱れない
    phy, dp = await setup(dut, tdo_delay_ns=55, sample_delay=3)
    assert await read_idcodes(phy) == (JTAG_DP_IDCODE, BSC_IDCODE)
    assert await phy.transfer(DP_R, 0x0) == (OK, JTAG_DP_IDCODE)
    assert (await phy.transfer(DP_W, A_CTRL, CDBGPWRUPREQ))[0] == OK
    ack, ctrl = await phy.transfer(DP_R, A_CTRL)
    assert ack == OK and ctrl & CDBGPWRUPACK
    await phy.transfer(DP_W, A_SELECT, 0)
    await phy.transfer(AP_W, A_CSW, 0x23000012)
    await phy.transfer(AP_W, A_TAR, 0x20000000)
    for v in (0x11111111, 0xA5A5A5A5, 0x0F0F0F0F):
        assert (await phy.transfer(AP_W, A_DRW, v))[0] == OK
    assert [dp.regs.memory[0x20000000 + 4 * i] for i in range(3)] == [0x11111111, 0xA5A5A5A5, 0x0F0F0F0F]
    await phy.transfer(AP_W, A_TAR, 0x20000000)
    await phy.transfer(AP_R, A_DRW)
    assert (await phy.transfer(AP_R, A_DRW))[1] == 0x11111111
    assert (await phy.transfer(DP_R, A_RDBUFF))[1] == 0xA5A5A5A5


@cocotb.test()
async def dp_and_memory_access_like_swd(dut):
    phy, dp = await setup(dut, idle_cycles=2)
    assert await phy.transfer(DP_R, 0x0) == (OK, JTAG_DP_IDCODE)     # IDCODE 命令
    assert (await phy.transfer(DP_W, A_CTRL, CDBGPWRUPREQ))[0] == OK
    ack, ctrl = await phy.transfer(DP_R, A_CTRL)                      # DP の読みは値がその場で返る
    assert ack == OK and ctrl & CDBGPWRUPACK
    await phy.transfer(DP_W, A_SELECT, 0)
    await phy.transfer(AP_W, A_CSW, 0x23000012)                       # 32 ビット、自動増加
    await phy.transfer(AP_W, A_TAR, 0x20000000)
    for v in (0x11111111, 0x22222222, 0x33333333):
        assert (await phy.transfer(AP_W, A_DRW, v))[0] == OK
    assert dp.regs.memory[0x20000004] == 0x22222222
    await phy.transfer(AP_W, A_TAR, 0x20000000)
    await phy.transfer(AP_R, A_DRW)                                   # AP の読みはポスト（SWD と同じ）
    assert (await phy.transfer(AP_R, A_DRW))[1] == 0x11111111
    assert (await phy.transfer(DP_R, A_RDBUFF))[1] == 0x22222222
    # IR は変わるときだけスキャンする
    irs = [s[0] for s in dp.scans]
    assert irs.count(JtagDp.IR_APACC) >= 7


@cocotb.test()
async def abort_and_pins(dut):
    phy, dp = await setup(dut)
    assert (await phy.transfer(DP_W, 0x0, 0x1E))[0] == OK             # DP の A = 0 への書き込み = ABORT
    assert dp.scans[-1][0] == JtagDp.IR_ABORT and dp.scans[-1][3] == 0x1E
    _, pins = await phy.command(OP_PINS, data=1 << 10 | 1 << 9 | 1 << 8)   # TCK = TMS = TDI = 0 にする
    assert pins & 0b101 == 0b101                                      # 実行前: TCK = TDI = 1
    _, pins = await phy.command(OP_PINS, data=0)
    assert pins & 0b111 == 0b000


async def setup_chain(dut, devices, **kw):
    """任意のチェーンで起動する（位置の設定は呼び出し側が dut に入れる）。"""
    chain = JtagChain(dut, devices)
    dut.enable.value = 1
    dut.half_period.value = 2
    dut.sample_delay.value = 0
    dut.idle_cycles.value = 0
    dut.dr_before.value, dut.dr_after.value = 0, 0
    dut.ir_before.value, dut.ir_after.value, dut.ir_len.value = 0, 0, 4
    dut.cmd_valid.value = 0
    await start_clock_and_reset(dut)
    cocotb.start_soon(chain.run())
    phy = Phy(dut)
    await phy.command(OP_SEQ_IN, count=6, req=1, data=0x3F)
    await phy.command(OP_SEQ_IN, count=1, req=0, data=1)
    return phy


@cocotb.test()
async def ir_is_rescanned_when_the_selected_device_changes(dut):
    # 同じ IR 値でも装置（チェーン上の位置）が変われば IR を入れ直す。さもないと 2 台目の IR は BYPASS のまま
    dps = [JtagDp(), JtagDp()]
    phy = await setup_chain(dut, dps)

    def select(index):
        dut.dr_before.value, dut.dr_after.value = index, 1 - index
        dut.ir_before.value, dut.ir_after.value = 4 * index, 4 * (1 - index)

    for index in (0, 1):
        select(index)
        assert (await phy.transfer(DP_W, A_SELECT, 0))[0] == OK
        assert (await phy.transfer(AP_W, A_CSW, 0x23000012))[0] == OK
        assert (await phy.transfer(AP_W, A_TAR, 0x20000000))[0] == OK
    for n, index in enumerate((0, 1, 0, 1, 1)):                      # 直前も APACC の書き込み（IR の値は同じ）
        select(index)
        assert (await phy.transfer(AP_W, A_DRW, 0xC0DE0000 + n))[0] == OK
        assert (await phy.transfer(AP_W, A_TAR, 0x20000000))[0] == OK     # 完了させるため同じ装置への転送を続ける
        assert dps[index].regs.memory[0x20000000] == 0xC0DE0000 + n, n


@cocotb.test()
async def random_transfers_with_random_settings_match_a_memory_model(dut):
    """半周期・アイドル（65 以上を含む）・TDO の取り込みの遅れをランダムに変え、DP / AP の書き・ポストの読み・RDBUFF・
    IDCODE・ABORT を混ぜた転送が、順に実行したメモリと一致する。"""
    import random
    rng = random.Random(8080)
    dp = JtagDp()
    phy = await setup_chain(dut, [dp, Bypass()])
    dut.dr_before.value, dut.dr_after.value = 0, 1
    dut.ir_before.value, dut.ir_after.value, dut.ir_len.value = 0, 5, 4
    assert (await phy.transfer(DP_W, A_CTRL, CDBGPWRUPREQ))[0] == OK
    assert (await phy.transfer(DP_W, A_SELECT, 0))[0] == OK
    assert (await phy.transfer(AP_W, A_CSW, 0x23000012))[0] == OK
    mem, tar = {}, 0x20000000
    assert (await phy.transfer(AP_W, A_TAR, tar))[0] == OK
    for it in range(120):
        dut.half_period.value = rng.choice([2, 2, 3, 5])
        dut.idle_cycles.value = rng.choice([0, 0, 1, 4, 64, 65, 130, 255])
        dut.sample_delay.value = 0
        kind = rng.choice(["tar", "w", "w", "r", "idcode", "abort", "ctrl", "rdbuff"])
        if kind == "tar":
            tar = 0x20000000 + 4 * rng.randrange(0, 32)
            assert (await phy.transfer(AP_W, A_TAR, tar))[0] == OK
        elif kind == "w":
            value = rng.getrandbits(32)
            assert (await phy.transfer(AP_W, A_DRW, value))[0] == OK
            mem[tar] = value
            tar += 4
        elif kind == "r":
            assert (await phy.transfer(AP_R, A_DRW))[0] == OK              # ポスト: 値は RDBUFF で受け取る
            assert (await phy.transfer(DP_R, A_RDBUFF))[0] == OK
            tar += 4
        elif kind == "idcode":
            assert await phy.transfer(DP_R, 0x0) == (OK, JTAG_DP_IDCODE)
        elif kind == "abort":
            assert (await phy.transfer(DP_W, 0x0, 0x1E))[0] == OK
        elif kind == "ctrl":
            ack, ctrl = await phy.transfer(DP_R, A_CTRL)
            assert ack == OK and ctrl & CDBGPWRUPACK, it
        else:
            assert (await phy.transfer(DP_R, A_RDBUFF))[0] == OK
    for address, value in mem.items():
        assert dp.regs.memory[address] == value, hex(address)


@cocotb.test()
async def long_chain_ir_scan_exceeds_127_bits(dut):
    # IR の合計が 127 を超えるチェーン（TDO 側 63 + 4 + TDI 側 63 = 130）。シフトの残りを数える範囲が足りないと壊れる
    devices = [Bypass(ir_len=32, idcode_ir=1), Bypass(ir_len=31, idcode_ir=1), JtagDp(),
               Bypass(ir_len=32, idcode_ir=1), Bypass(ir_len=31, idcode_ir=1)]
    phy = await setup_chain(dut, devices)
    dut.dr_before.value, dut.dr_after.value = 2, 2
    dut.ir_before.value, dut.ir_after.value, dut.ir_len.value = 63, 63, 4
    assert await phy.transfer(DP_R, 0x0) == (OK, JTAG_DP_IDCODE)
    assert (await phy.transfer(DP_W, A_SELECT, 0))[0] == OK
    assert (await phy.transfer(AP_W, A_CSW, 0x23000012))[0] == OK
    assert devices[2].ir == JtagDp.IR_APACC
