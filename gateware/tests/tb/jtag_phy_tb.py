"""jtag_phy のテストベンチ。チェーンは STM32 と同じ: TDO 側に JTAG-DP（装置 0、IR 4）、TDI 側にバウンダリスキャン
TAP（装置 1、IR 5）。"""

import cocotb
from cocotb.triggers import ReadOnly, RisingEdge

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
    phy.chain = chain
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


async def record_tck(dut, levels: list):
    """clk ごとの TCK のピンの値を levels に足す。"""
    while True:
        await RisingEdge(dut.clk)
        await ReadOnly()
        levels.append(int(dut.tck.value))


def runs(levels: list) -> list[tuple[int, int]]:
    """(値, 続いた周期数) の並び。最初と最後の連なりは途中から・途中までなので除く。"""
    out: list[tuple[int, int]] = []
    for v in levels:
        if out and out[-1][0] == v:
            out[-1] = (v, out[-1][1] + 1)
        else:
            out.append((v, 1))
    return out[1:-1]


@cocotb.test()
async def a_bit_is_two_half_periods(dut):
    """1 ビット = 2 × half_period 周期（Low 相 half_period・High 相 half_period）。TCK = sys / (2 × half_period)。
    区間の中のビット（シーケンスの 64 ビット）で測る（区間の境目は次の区間を決める周期の分 High 相が延びる）。"""
    phy, _ = await setup(dut)
    for half_period in (2, 3, 4, 7):
        dut.half_period.value = half_period
        levels: list = []
        watcher = cocotb.start_soon(record_tck(dut, levels))
        await phy.command(OP_SEQ_IN, count=64, req=0, data=0)        # Run-Test/Idle に留まる
        watcher.cancel()
        inner = runs(levels)[1:-1]                                   # 命令の始めと終わりの相を除く
        assert len(inner) >= 100, (half_period, len(inner))
        assert set(inner) == {(0, half_period), (1, half_period)}, (half_period, sorted(set(inner)))


@cocotb.test()
async def tdo_is_sampled_one_cycle_before_the_next_fall(dut):
    """取り込みは次の立ち下がりの直前（din = その 1 周期前のピン）: 立ち下がりから (2 × half_period − 1) 周期より前に変わった
    TDO はそのビット、それより後に変わった TDO は前のビットとして読む（1 ビットずれる）。"""
    phy, _ = await setup(dut)
    for half_period in (2, 3, 4):
        dut.half_period.value = half_period
        edge_ns = (2 * half_period - 1) * 10
        phy.chain.tdo_delay_ns = edge_ns - 5
        assert (await read_idcodes(phy))[0] == JTAG_DP_IDCODE, half_period
        phy.chain.tdo_delay_ns = edge_ns + 5
        assert (await read_idcodes(phy))[0] >> 1 == JTAG_DP_IDCODE & 0x7FFFFFFF, half_period   # 最下位は前のビット


@cocotb.test()
async def late_tdo_within_a_period(dut):
    # TDO は立ち下がりから次の立ち下がりまで保たれるので、取り込みは次の立ち下がりの直前（din = その 1 周期前のピン）。
    # half_period = 2 の 1 ビットは 4 周期（40 ns）: 立ち下がりから 25 ns 遅れて変わる TDO も読める
    phy, dp = await setup(dut, tdo_delay_ns=25)
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
    # 立ち下がりから 45 ns（1 ビット 40 ns より長い）遅れて変わる TDO（Pi 2B の 14.3 MHz 以上で見た形）: High 相の終わりでは
    # 前のビットを読む（1 ビットずれる）
    phy, _ = await setup(dut, tdo_delay_ns=45)
    dp_id, _ = await read_idcodes(phy)
    assert dp_id == (JTAG_DP_IDCODE << 1 | 1) & 0xFFFFFFFF


@cocotb.test()
async def sample_delay_reads_tdo_later_than_a_period(dut):
    # 取り込みを High 相の終わりから 3 周期（30 ns）遅らせると読める（立ち下がりから 60 ns、次のビットの TDO は 95 ns）。
    # 遅れた取り込みは次のビットの出力と重なるが、TDI（IR・DR の書き込み）は乱れない
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
async def a_posted_dp_read_returns_the_previous_result_in_one_scan(dut):
    """cmd_count = 2 の DP の読み（dap_sampler の先読み）: RDBUFF を続けずに 1 スキャンで、取り込み値（前の AP の読みの値）を
    返す。読んだ DP のレジスタの値は次のスキャンの取り込みで返る（次が AP の書きでも）。"""
    phy, dp = await setup(dut)
    await phy.transfer(DP_W, A_CTRL, CDBGPWRUPREQ)
    await phy.transfer(DP_W, A_SELECT, 0)
    await phy.transfer(AP_W, A_CSW, 0x23000002)
    dp.regs.memory[0x20000010] = 0xABCD1234
    await phy.transfer(AP_W, A_TAR, 0x20000010)
    await phy.transfer(AP_R, A_DRW)
    before = len(dp.scans)
    assert await phy.command(OP_TRANSFER, count=2, req=request(DP_R, A_CTRL)) == (OK, 0xABCD1234)
    assert dp.scans[before:] == [(JtagDp.IR_DPACC, 1, A_CTRL, 0)]              # 1 スキャンだけ
    ack, ctrl = await phy.transfer(AP_W, A_TAR, 0x20000000)                    # 次のスキャンが CTRL/STAT を返す
    assert ack == OK and ctrl & CDBGPWRUPACK


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
        dut.half_period.value = rng.choice([2, 2, 3, 4, 5])
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


@cocotb.test()
async def an_all_ones_capture_is_a_protocol_error_and_is_not_scanned_again(dut):
    """ACK 0b111（仕様にない値。TDO が浮いている等）は "111" として返し、同じ要求をやり直さない（やり直すと本当の誤りを隠す）。"""
    phy, dp = await setup(dut, idle_cycles=4)
    assert (await phy.transfer(DP_W, A_CTRL, CDBGPWRUPREQ))[0] == OK
    assert (await phy.transfer(DP_W, A_SELECT, 0))[0] == OK
    assert (await phy.transfer(AP_W, A_TAR, 0x20000000))[0] == OK
    scans = len(dp.scans)
    dp.all_ones = 1
    assert (await phy.transfer(AP_W, A_DRW, 0x12345678))[0] == 0b111
    assert len(dp.scans) == scans and 0x20000000 not in dp.regs.memory

async def watch_pins(dut, bad: list):
    """clk ごとにピンを見て、TMS が TCK の立ち下がりか TDI の変化と同じ周期に変わったら bad に記録する。"""
    prev = None
    while True:
        await RisingEdge(dut.clk)
        await ReadOnly()
        now = tuple(int(getattr(dut, n).value) for n in ("tck", "tms", "tdi"))
        if prev is not None and now[1] != prev[1] and ((prev[0], now[0]) == (1, 0) or now[2] != prev[2]):
            bad.append((prev, now))
        prev = now


@cocotb.test()
async def tms_never_switches_with_the_tck_fall_or_tdi(dut):
    """実機（Primer 20K → Raspberry Pi 2B、2026-10-29）: TCK の立ち下がりで TMS と TDI が同時に 0 → 1 になると、そのビットが
    化ける（周波数に依らず毎回。片方だけの変化では化けない）。TMS は Low 相の途中で変える。"""
    for half_period in (2, 3, 6):
        phy, dp = await setup(dut, idle_cycles=4)
        dut.half_period.value = half_period
        bad: list = []
        watcher = cocotb.start_soon(watch_pins(dut, bad))
        assert (await phy.transfer(DP_W, A_CTRL, CDBGPWRUPREQ))[0] == OK  # IR の最後のビット: TDI と TMS が 0 → 1
        assert (await phy.transfer(DP_W, A_SELECT, 0))[0] == OK
        assert (await phy.transfer(AP_W, A_TAR, 0x80000000))[0] == OK     # DR の最後のビット: TDI と TMS が 0 → 1
        watcher.cancel()
        assert not bad, (half_period, bad[:3])
        assert dp.regs.tar == 0x80000000


# --- 線上の点の時刻（docs/superpowers/specs/2026-11-01-time-accuracy-design.md R5）---------------------------------------

class TimedChain(JtagChain):
    """TCK の立ち上がりのうち Update-DR を出るもの（Update-DR → Run-Test/Idle か Select-DR）の周期を exits に控える。"""

    def __init__(self, dut, devices, counter, **kw):
        super().__init__(dut, devices, **kw)
        self.counter = counter
        self.exits: list[int] = []

    def _edge(self, tms, tdi):
        from tests.models.jtag_target import UPD_DR
        before = self.state
        super()._edge(tms, tdi)
        if before == UPD_DR:
            self.exits.append(self.counter[0])


async def count_cycles(dut, counter):
    """now（周期の番号）を駆動する: clk の立ち上がり e_k の後の周期の now = k（PHY は次の立ち上がりで取り込む）。"""
    while True:
        await RisingEdge(dut.clk)
        counter[0] += 1
        dut.now.value = counter[0]


@cocotb.test()
async def rsp_time_is_the_rise_leaving_update_dr(dut):
    """R5（JTAG）: rsp_time = その転送の DR スキャンの Update-DR を出る TCK の立ち上がり（tck が 1 になった最初の周期）。
    IR を省いた転送・省かなかった転送・続けて RDBUFF をスキャンする DP の読み（最初のスキャンの点）・IDCODE・ABORT、
    half_period・装置の位置の組で、点がモデルの TAP で見た立ち上がりと一致する。"""
    counter = [0]
    dut.now.value = 0
    dp = JtagDp()
    chain = TimedChain(dut, [dp, Bypass()], counter)
    dut.enable.value = 1
    dut.half_period.value = 2
    dut.sample_delay.value = 0
    dut.idle_cycles.value = 0
    dut.dr_before.value, dut.dr_after.value = 0, 1
    dut.ir_before.value, dut.ir_after.value, dut.ir_len.value = 0, 5, 4
    dut.cmd_valid.value = 0
    await start_clock_and_reset(dut)
    cocotb.start_soon(count_cycles(dut, counter))
    cocotb.start_soon(chain.run())
    phy = Phy(dut)
    await phy.command(OP_SEQ_IN, count=6, req=1, data=0x3F)
    await phy.command(OP_SEQ_IN, count=1, req=0, data=1)
    assert (await phy.transfer(DP_W, A_CTRL, CDBGPWRUPREQ))[0] == OK
    cases = 0
    for hp, idle in ((2, 0), (3, 2), (5, 1)):
        dut.half_period.value, dut.idle_cycles.value = hp, idle
        await RisingEdge(dut.clk)
        for kind, addr, data in ((DP_W, A_SELECT, 0), (AP_W, A_TAR, 0x20000000), (AP_W, A_DRW, 7),   # 2 つ目から IR を省く
                                 (AP_R, A_DRW, 0), (DP_R, A_CTRL, 0),                                 # DP の読み: RDBUFF が続く
                                 (DP_R, A_RDBUFF, 0), (DP_R, 0x0, 0), (DP_W, 0x0, 0)):               # IDCODE・ABORT
            n = len(chain.exits)
            await phy.transfer(kind, addr, data)
            first = chain.exits[n]                                     # この転送の最初の DR スキャンの Update-DR を出る点
            assert int(dut.rsp_time.value) == first, (hp, kind, addr, int(dut.rsp_time.value), chain.exits[n:])
            cases += 1
    assert cases == 24


# --- sys / 2（half_period = 1）・空きなし・取り込みの遅れ（docs/superpowers/specs/2026-11-03-dap-line-rate-design.md）---------

async def stream(dut, commands, *, cond=True, timeout=20000):
    """命令 [(op, count, request, data)] を空きなく出す（受理された周期の辺で次に替える。2 つ目からは cmd_cond = cond）。
    受理された数と応答 [(ack, data)] を返す。受理されないまま応答が揃って 50 周期たったら取り下げる。"""
    d = dut
    rsps: list = []

    async def collect():
        while True:
            await RisingEdge(d.clk)
            await ReadOnly()
            if int(d.rsp_valid.value):
                rsps.append((int(d.rsp_ack.value), int(d.rsp_data.value)))

    watcher = cocotb.start_soon(collect())
    accepted, idle = 0, 0
    for i, (op, count, req, data) in enumerate(commands):
        d.cmd_op.value, d.cmd_count.value, d.cmd_request.value, d.cmd_data.value = op, count, req, data
        d.cmd_cond.value = int(cond and i > 0)
        d.cmd_valid.value = 1
        for _ in range(timeout):
            await ReadOnly()
            ok = int(d.cmd_ready.value)
            await RisingEdge(d.clk)
            if ok:
                accepted += 1
                break
            idle = idle + 1 if len(rsps) == accepted else 0
            if idle > 50:
                break
        else:
            raise AssertionError("受理されない")
        if idle > 50:
            break
    d.cmd_valid.value = 0
    d.cmd_cond.value = 0
    for _ in range(timeout):
        if len(rsps) == accepted:
            break
        await RisingEdge(d.clk)
    await RisingEdge(d.clk)
    watcher.cancel()
    return accepted, rsps


def xfer(kind, addr, data=0, count=1):
    return (OP_TRANSFER, count, request(kind, addr), data)


def scan_bits(ir_scan: bool, dr_len: int, ir_total: int, dr_extra: int) -> int:
    """1 転送の TCK の数（導出）: IR スキャン 4 + IR の合計 + 2、DR スキャン 3 + 装置 + 本体 + 2。"""
    return (4 + ir_total + 2 if ir_scan else 0) + 3 + dr_extra + dr_len + 2


@cocotb.test()
async def back_to_back_transfers_have_no_gap_at_any_half_period(dut):
    """続けて出した転送（IR の入れ替え・DP の読みの RDBUFF の続き・ポストの読みを含む）の TCK は、区間の境目・転送の境目を
    含めてすべて Low 相 half_period・High 相 half_period（空きなし）。TCK の数の合計は導出と一致し、値も正しい。
    half_period 1（sys / 2）・2・3、取り込みの遅れ 0・3（遅れても線は止まらない）。"""
    phy, dp = await setup(dut)
    dut.cmd_cond.value = 0
    assert (await phy.transfer(DP_W, A_CTRL, CDBGPWRUPREQ))[0] == OK
    checked = 0
    for hp in (1, 2, 3):
        for sd in (0, 3):
            dut.half_period.value, dut.sample_delay.value = hp, sd
            phy.chain.tdo_delay_ns = sd * 10 - 5 if sd else 0         # 取り込みの点に合う遅れ
            base = 0x20000000 + 0x100 * checked
            values = [base + i for i in range(4)]
            cmds = [xfer(DP_W, A_SELECT, 0), xfer(AP_W, A_CSW, 0x23000012), xfer(AP_W, A_TAR, base)]
            cmds += [xfer(AP_W, A_DRW, v) for v in values]
            cmds += [xfer(AP_W, A_TAR, base)] + [xfer(AP_R, A_DRW) for _ in values] + [xfer(DP_R, A_RDBUFF)]
            cmds += [xfer(DP_R, A_CTRL)]                                # DP の読み: RDBUFF が続く
            levels: list = []
            rec = cocotb.start_soon(record_tck(dut, levels))
            await RisingEdge(dut.clk)
            await RisingEdge(dut.clk)
            accepted, rsps = await stream(dut, cmds)
            rec.cancel()
            assert accepted == len(cmds) and len(rsps) == len(cmds), (hp, sd, accepted, len(rsps))
            assert all(a == OK for a, _ in rsps), (hp, sd, rsps)
            assert [v & 0xFFFFFFFF for _, v in rsps[len(values) + 5:len(values) * 2 + 5]] == values, (hp, sd)
            assert rsps[-1][1] & CDBGPWRUPACK
            inner = runs(levels)
            assert set(inner) == {(0, hp), (1, hp)}, (hp, sd, sorted(set(inner)))
            # TCK の数: IR は SELECT（DPACC）・CSW（APACC）・RDBUFF（DPACC）・CTRL（同じ DPACC で省く）で入れ替わる
            # （直前は DPACC: SELECT は省き、CSW で APACC、RDBUFF で DPACC、CTRL は省く）
            irs = [False, True, False] + [False] * 4 + [False] + [False] * 4 + [True, False]
            dr_extra = 1                                                 # BYPASS が 1 台
            expect = sum(scan_bits(i, 35, 4 + 5, dr_extra) for i in irs) + scan_bits(False, 35, 9, dr_extra)  # CTRL の後の RDBUFF
            lows = sum(1 for v, _ in inner if v == 0)
            assert lows == expect, (hp, sd, lows, expect)
            checked += 1
    assert checked == 6


@cocotb.test()
async def tdo_sample_delay_window_at_sys_half(dut):
    """half_period 1（TCK = sys / 2）で、TDO の遅れ（端子の出力の段 1 周期 = 10 ns を足した形も）に対し正しく読める取り込みの
    遅れの範囲が導出と一致する: サンプル = 次の立ち下がりの辺 − 1 + d 周期のピン。ビットの TDO は [立ち下がり + 遅れ、
    次の立ち下がり + 遅れ] で有効 → d·T ∈ (遅れ − 2·hp·T + T、遅れ + T)。"""
    phy, dp = await setup(dut)
    checked = 0
    for hp in (1, 2):
        dut.half_period.value = hp
        for delay in (3, 13, 23, 33, 43):
            good = {d for d in range(8) if delay - 2 * hp * 10 + 10 < d * 10 < delay + 10}
            assert good
            phy.chain.tdo_delay_ns = delay
            for d in range(8):
                dut.sample_delay.value = d
                await RisingEdge(dut.clk)
                await phy.command(OP_SEQ_IN, count=6, req=1, data=0x3F)    # Test-Logic-Reset（IR = IDCODE）
                await phy.command(OP_SEQ_IN, count=1, req=0, data=1)       # Run-Test/Idle
                got = await read_idcodes(phy)
                assert (got == (JTAG_DP_IDCODE, BSC_IDCODE)) == (d in good), (hp, delay, d, [hex(v) for v in got])
                if d in good:
                    assert await phy.transfer(DP_R, 0x0) == (OK, JTAG_DP_IDCODE)
                    ack, ctrl = await phy.transfer(DP_R, A_CTRL)
                    assert ack == OK, (hp, delay, d)
                checked += 1
    assert checked == 2 * 5 * 8


async def watch_half_cycles(dut, bad: list, count: list):
    """sys の 1 周期を前半（tms_lead）と後半（tms）に分け（ODDR のボードの端子）、TMS が TCK の立ち下がりか TDI の変化と同じ
    半周期に変わったら bad に記録する。"""
    prev = None
    while True:
        await RisingEdge(dut.clk)
        await ReadOnly()
        tck, tdi = int(dut.tck.value), int(dut.tdi.value)
        for tms in (int(dut.tms_lead.value), int(dut.tms.value)):
            now = (tck, tms, tdi)
            if prev is not None and now[1] != prev[1]:
                count[0] += 1
                if (prev[0], now[0]) == (1, 0) or now[2] != prev[2]:
                    bad.append((prev, now))
            prev = now


@cocotb.test()
async def tms_lead_keeps_tms_off_the_fall_at_sys_half(dut):
    """half_period 1: TMS は立ち下がりの辺で変わる（tms）が、その周期の前半は前の値（tms_lead）。ODDR（前半 tms_lead・後半
    tms）で出すと TMS は TCK の立ち下がり・TDI の変化と同じ半周期に変わらない（実機で化けた形を避ける）。half_period ≥ 2 では
    tms_lead = tms。"""
    phy, dp = await setup(dut, idle_cycles=4)
    dut.half_period.value = 1
    bad: list = []
    changes = [0]
    watcher = cocotb.start_soon(watch_half_cycles(dut, bad, changes))
    assert (await phy.transfer(DP_W, A_CTRL, CDBGPWRUPREQ))[0] == OK  # IR の最後のビット: TDI と TMS が 0 → 1
    assert (await phy.transfer(DP_W, A_SELECT, 0))[0] == OK
    assert (await phy.transfer(AP_W, A_TAR, 0x80000000))[0] == OK     # DR の最後のビット: TDI と TMS が 0 → 1
    watcher.cancel()
    assert not bad and changes[0] > 10, (bad[:3], changes)
    assert dp.regs.tar == 0x80000000
    dut.half_period.value = 2
    for _ in range(40):
        await RisingEdge(dut.clk)
        await ReadOnly()
        assert int(dut.tms_lead.value) == int(dut.tms.value)


@cocotb.test()
async def a_conditional_transfer_is_not_scanned_after_a_wait(dut):
    """cmd_cond = 1 の転送は、直前の転送が WAIT（JTAG-DP が前の AP の転送を終えていない）なら受けない（スキャンしない）。
    OK なら続けて受ける。"""
    dp = JtagDp(wait_after_ap=1)
    phy = await setup_chain(dut, [dp, Bypass()])
    dut.cmd_cond.value = 0
    dut.half_period.value = 1
    dut.dr_before.value, dut.dr_after.value = 0, 1
    dut.ir_before.value, dut.ir_after.value, dut.ir_len.value = 0, 5, 4
    assert (await phy.transfer(DP_W, A_CTRL, CDBGPWRUPREQ))[0] == OK
    assert (await phy.transfer(DP_W, A_SELECT, 0))[0] == OK
    assert (await phy.transfer(AP_W, A_TAR, 0x20000000))[0] == OK     # この後の 1 スキャンは WAIT
    n, waits = len(dp.scans), dp.waits
    accepted, rsps = await stream(dut, [xfer(AP_W, A_DRW, 5), xfer(AP_W, A_DRW, 6)])
    assert accepted == 1 and [a for a, _ in rsps] == [WAIT], (accepted, rsps)
    assert (len(dp.scans), dp.waits) == (n, waits + 1)                # 2 つ目はスキャンしていない
    accepted, rsps = await stream(dut, [xfer(AP_W, A_DRW, 7), xfer(AP_W, A_DRW, 8)])
    assert accepted == 2 and [a for a, _ in rsps] == [OK, WAIT], (accepted, rsps)   # 7 の後の 1 スキャンは WAIT
    assert dp.regs.memory[0x20000000] == 7
