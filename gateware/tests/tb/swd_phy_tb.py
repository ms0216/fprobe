"""swd_phy（ハーネス経由）のテストベンチ。相手は SWD ターゲットモデル。"""

import cocotb
from cocotb.triggers import ReadOnly, RisingEdge

from tests.models.swd_target import (ACK_FAULT, ACK_OK, ACK_WAIT, AP_IDR, CDBGPWRUPACK, CDBGPWRUPREQ, DPIDR,
                                     STICKYERR, STKERRCLR, SwdTarget)
from tests.tb.common import start_clock_and_reset

OP_SEQ_OUT, OP_SEQ_IN, OP_TRANSFER, OP_PINS = 0, 1, 2, 3
# CMSIS-DAP の要求: bit0 APnDP, bit1 RnW, bit2-3 A[3:2]
DP_READ_IDCODE, DP_WRITE_ABORT = 0b0010, 0b0000
DP_READ_CTRL, DP_WRITE_CTRL = 0b0110, 0b0100
DP_WRITE_SELECT, DP_READ_RDBUFF = 0b1000, 0b1110
AP_WRITE_CSW, AP_WRITE_TAR, AP_WRITE_DRW, AP_READ_DRW = 0b0001, 0b0101, 0b1101, 0b1111
AP_READ_IDR = 0b1111                       # SELECT の APBANKSEL = 0xF のとき 0xFC


class SwdHost:
    def __init__(self, dut):
        self.dut = dut
        dut.enable.value = 1
        dut.half_period.value = 2
        dut.turnaround.value = 1
        dut.data_phase.value = 0
        dut.idle_cycles.value = 0
        dut.sample_delay.value = 0
        dut.cmd_valid.value = 0
        dut.cmd_op.value = 0
        dut.cmd_count.value = 1
        dut.cmd_request.value = 0
        dut.cmd_data.value = 0

    async def command(self, op, *, count=1, request=0, data=0, timeout=20000):
        d = self.dut
        d.cmd_op.value = op
        d.cmd_count.value = count
        d.cmd_request.value = request
        d.cmd_data.value = data
        d.cmd_valid.value = 1
        for _ in range(timeout):
            await ReadOnly()
            accepted = int(d.cmd_ready.value)
            await RisingEdge(d.clk)
            if accepted:
                break
        d.cmd_valid.value = 0
        for _ in range(timeout):
            await ReadOnly()
            if int(d.rsp_valid.value):
                result = (int(d.rsp_ack.value), int(d.rsp_data.value), int(d.rsp_parity_error.value))
                await RisingEdge(d.clk)
                return result
            await RisingEdge(d.clk)
        raise AssertionError("swd_phy が応答しない")

    async def connect(self):
        await self.command(OP_SEQ_OUT, count=51, data=(1 << 51) - 1)        # ラインリセット
        await self.command(OP_SEQ_OUT, count=16, data=0xE79E)              # JTAG から SWD へ
        await self.command(OP_SEQ_OUT, count=51, data=(1 << 51) - 1)
        await self.command(OP_SEQ_OUT, count=8, data=0)                    # アイドル

    async def transfer(self, request, data=0):
        ack, value, perr = await self.command(OP_TRANSFER, request=request, data=data)
        return ack, value & 0xFFFFFFFF, perr


async def setup(dut, **target):
    host = SwdHost(dut)
    model = SwdTarget(dut, **target)
    await start_clock_and_reset(dut)
    cocotb.start_soon(model.run())
    return host, model


@cocotb.test()
async def reads_dpidr_after_connect(dut):
    host, model = await setup(dut)
    assert await host.transfer(DP_READ_IDCODE) == (0b111, 0, 0) or not model.line_reset   # 接続前は無応答
    await host.connect()
    assert await host.transfer(DP_READ_IDCODE) == (ACK_OK, DPIDR, 0)


@cocotb.test()
async def memory_access_through_mem_ap_with_posted_reads(dut):
    host, model = await setup(dut)
    await host.connect()
    assert (await host.transfer(DP_WRITE_CTRL, CDBGPWRUPREQ))[0] == ACK_OK
    assert (await host.transfer(DP_READ_CTRL))[1] & CDBGPWRUPACK
    await host.transfer(DP_WRITE_SELECT, 0)
    await host.transfer(AP_WRITE_CSW, 0x23000012)                          # 32 ビット、自動インクリメント
    await host.transfer(AP_WRITE_TAR, 0x20000000)
    for value in (0x11111111, 0xDEADBEEF, 0x80000001):
        assert (await host.transfer(AP_WRITE_DRW, value))[0] == ACK_OK
    assert model.memory == {0x20000000: 0x11111111, 0x20000004: 0xDEADBEEF, 0x20000008: 0x80000001}
    await host.transfer(AP_WRITE_TAR, 0x20000000)
    await host.transfer(AP_READ_DRW)                                       # ポスティング: 値は次で返る
    assert (await host.transfer(AP_READ_DRW))[1] == 0x11111111
    assert (await host.transfer(DP_READ_RDBUFF))[1] == 0xDEADBEEF
    await host.transfer(DP_WRITE_SELECT, 0xF0)
    await host.transfer(AP_READ_IDR)
    assert (await host.transfer(DP_READ_RDBUFF))[1] == AP_IDR


@cocotb.test()
async def wait_fault_and_parity_errors_are_reported(dut):
    host, model = await setup(dut)
    await host.connect()
    model.wait_next = 1
    assert (await host.transfer(AP_WRITE_TAR, 0x20000000))[0] == ACK_WAIT
    assert (await host.transfer(AP_WRITE_TAR, 0x20000000))[0] == ACK_OK     # 再試行で通る
    model.fault_addresses.add(0x20000000)
    assert (await host.transfer(AP_WRITE_DRW, 1))[0] == ACK_OK             # バスエラーは STICKYERR へ
    assert (await host.transfer(AP_WRITE_DRW, 2))[0] == ACK_FAULT
    assert (await host.transfer(DP_WRITE_ABORT, STKERRCLR))[0] == ACK_OK
    model.corrupt_next_parity = True
    assert await host.transfer(DP_READ_IDCODE) == (ACK_OK, DPIDR, 1)
    assert await host.transfer(DP_READ_IDCODE) == (ACK_OK, DPIDR, 0)


@cocotb.test()
async def configuration_turnaround_data_phase_idle_and_clock(dut):
    host, model = await setup(dut, turnaround=2)
    dut.turnaround.value = 2
    dut.data_phase.value = 1
    dut.idle_cycles.value = 8
    for half in (2, 5):                    # 半周期 1 は入力の 2 段同期化で取れない（仕様上の下限は 2）
        dut.half_period.value = half
        await host.connect()
        assert await host.transfer(DP_READ_IDCODE) == (ACK_OK, DPIDR, 0), half
        model.wait_next = 1
        await host.transfer(DP_WRITE_SELECT, 0)
        assert (await host.transfer(AP_WRITE_TAR, 0x1234))[0] == ACK_WAIT  # データ相を出しても次が正しく動く
        assert (await host.transfer(AP_WRITE_TAR, 0x1234))[0] == ACK_OK
        assert model.tar == 0x1234


@cocotb.test()
async def sequences_and_pins(dut):
    host, model = await setup(dut)
    assert (await host.command(OP_SEQ_IN, count=13))[1] == (1 << 13) - 1    # 誰も駆動しなければプルアップ
    _, pins, _ = await host.command(OP_PINS, data=(1 << 8) | 0)            # SWCLK = 0
    assert pins & 1 == 1                                                   # 実行前の SWCLK
    await ReadOnly()
    assert str(dut.swclk.value) == "0"
    await RisingEdge(dut.clk)
    _, pins, _ = await host.command(OP_PINS, data=(1 << 9) | (1 << 8) | 0b01)   # SWCLK = 1, SWDIO = 0
    await ReadOnly()
    assert str(dut.swclk.value) == "1" and str(dut.swdio.value) == "0"
    await RisingEdge(dut.clk)
    dut.enable.value = 0                                                   # 解放
    await RisingEdge(dut.clk)
    await ReadOnly()
    assert str(dut.swdio.value) == "1" and str(dut.swclk.value) == "1"     # プルアップ


async def _session(host, delay):
    await host.connect()
    assert await host.transfer(DP_READ_IDCODE) == (ACK_OK, DPIDR, 0), delay
    assert (await host.transfer(DP_WRITE_CTRL, CDBGPWRUPREQ))[0] == ACK_OK
    await host.transfer(DP_WRITE_SELECT, 0)
    await host.transfer(AP_WRITE_CSW, 0x23000012)
    await host.transfer(AP_WRITE_TAR, 0x20000000)
    assert (await host.transfer(AP_WRITE_DRW, 0xA5C3_0F96 + delay))[0] == ACK_OK
    await host.transfer(AP_WRITE_TAR, 0x20000000)
    await host.transfer(AP_READ_DRW)
    assert await host.transfer(DP_READ_RDBUFF) == (ACK_OK, 0xA5C3_0F96 + delay, 0), delay
    _, bits, _ = await host.command(OP_SEQ_IN, count=8)                    # SWDIO は解放（プルアップ）→ 全 1
    assert bits & 0xFF == 0xFF


@cocotb.test()
async def sample_delay_moves_the_sample_point(dut):
    # 半周期 2（SWCLK = clk / 4、周期 40 ns）で、ターゲットが立ち上がりの 15 ns 後に SWDIO を変える。
    # ビット n は [前の立ち上がり + 15, 立ち上がり + 15] で有効。サンプル点は立ち上がり + 10 × sample_delay ns なので、
    # 0・1 は正しく、2・3 は次のビットを取る（取り込み点が動くこと = 往復遅延の補償ができることを確かめる）
    host, model = await setup(dut, output_delay_ns=15)
    for delay in (0, 1):
        dut.sample_delay.value = delay
        await _session(host, delay)
    for delay in (2, 3):
        dut.sample_delay.value = delay
        await host.connect()
        ack, value, _ = await host.transfer(DP_READ_IDCODE)
        assert (ack, value) != (ACK_OK, DPIDR), delay


@cocotb.test()
async def random_configurations_and_outcomes_have_exact_clock_counts(dut):
    """半周期・ターンアラウンド・データ相・アイドル・取り込みの遅れをランダムに変え、OK / WAIT / FAULT / パリティ異常 /
    無応答（プロトコル異常）の転送とシーケンスを混ぜて、結果と SWCLK の立ち上がりの数（CMSIS-DAP 参照実装の
    ビット数）が合う。"""
    import random
    rng = random.Random(97)
    host, model = await setup(dut, output_delay_ns=15)
    edges = [0]

    async def count_edges():
        while True:
            await RisingEdge(dut.swclk)
            edges[0] += 1

    cocotb.start_soon(count_edges())
    count_ref = host.command

    async def counted(op, **kw):
        edges[0] = 0
        result = await count_ref(op, **kw)
        return result, edges[0]

    # 接続前: 無応答（ACK = 7 = プロトコル異常）。ビット数はターンアラウンド + 33 のバックオフを含む
    for it in range(150):
        turnaround, data_phase = rng.randint(1, 4), rng.getrandbits(1)
        idle, half = rng.choice([0, 0, 1, 3, 8, 255]), rng.choice([2, 2, 3, 5])
        dut.turnaround.value, dut.data_phase.value, dut.idle_cycles.value = turnaround, data_phase, idle
        dut.half_period.value, dut.sample_delay.value = half, rng.choice([0, 1])
        model.turnaround = turnaround
        t = turnaround
        if not model.line_reset:                                     # 最初の 1 回だけ接続前
            (ack, _, _), n = await counted(OP_TRANSFER, request=DP_READ_IDCODE)
            assert ack == 0b111 and n == 8 + t + 3 + t + 33, (it, ack, n)
            await host.connect()
        kind = rng.choice(["dp_read", "dp_write", "ap_ok", "wait", "fault", "parity", "seq_out", "seq_in", "pins"])
        if kind == "dp_read":
            (ack, data, perr), n = await counted(OP_TRANSFER, request=DP_READ_IDCODE)
            assert (ack, data & 0xFFFFFFFF, perr) == (ACK_OK, DPIDR, 0), (it, ack, hex(data), perr)
            assert n == 8 + t + 3 + 33 + t + idle, (it, n, t, idle)
        elif kind == "parity":
            model.corrupt_next_parity = True
            (ack, data, perr), n = await counted(OP_TRANSFER, request=DP_READ_IDCODE)
            assert (ack, data & 0xFFFFFFFF, perr) == (ACK_OK, DPIDR, 1), (it, ack, hex(data), perr)
            assert n == 8 + t + 3 + 33 + t + idle, (it, n)
        elif kind == "dp_write":
            value = rng.getrandbits(32) & ~(1 << 29) & ~(1 << 31)
            (ack, _, _), n = await counted(OP_TRANSFER, request=DP_WRITE_SELECT, data=value & ~0x1F0)
            assert ack == ACK_OK and n == 8 + t + 3 + t + 33 + idle, (it, ack, n)
            assert model.select == value & ~0x1F0
        elif kind == "ap_ok":
            tar = rng.getrandbits(32)
            (ack, _, _), n = await counted(OP_TRANSFER, request=AP_WRITE_TAR, data=tar)
            await host.transfer(DP_WRITE_SELECT, 0)
            assert ack in (ACK_OK, ACK_FAULT) and n == (8 + t + 3 + t + 33 + idle if ack == ACK_OK
                                                       else 8 + t + 3 + t + 33 * data_phase), (it, ack, n)
        elif kind in ("wait", "fault"):
            if kind == "wait":
                model.wait_next = 1
            else:
                model.ctrl_stat |= STICKYERR
            rw = rng.choice([AP_WRITE_TAR, AP_READ_DRW])
            (ack, _, _), n = await counted(OP_TRANSFER, request=rw, data=5)
            assert ack == (ACK_WAIT if kind == "wait" else ACK_FAULT), (it, kind, ack)
            assert n == 8 + t + 3 + t + 33 * data_phase, (it, kind, rw, n, t, data_phase)
            if rw == AP_READ_DRW and data_phase:                     # 読みのダミーデータ相はプルアップの 1 が続く
                await host.command(OP_SEQ_OUT, count=8, data=0)           # （モデルは 0 の後の 1 を開始ビットと見る）
            if kind == "fault":
                assert (await host.transfer(DP_WRITE_ABORT, STKERRCLR))[0] == ACK_OK
        elif kind == "seq_out":
            c = rng.choice([1, 2, 31, 32, 33, 63, 64, rng.randint(1, 64)])
            value = rng.getrandbits(c)
            (ack, _, _), n = await counted(OP_SEQ_OUT, count=c, data=value)
            assert n == c, (it, c, n)
            await host.connect()                                     # ランダムなビットがモデルを迷わせたので繋ぎ直す
        elif kind == "seq_in":
            c = rng.choice([1, 2, 31, 32, 33, 63, 64, rng.randint(1, 64)])
            (_, data, _), n = await counted(OP_SEQ_IN, count=c)
            assert n == c and data == (1 << c) - 1, (it, c, n, hex(data))
            await host.command(OP_SEQ_OUT, count=8, data=0)          # モデルは 0 の後の 1 を開始ビットと見る
        else:
            _, pins, _ = await host.command(OP_PINS, data=(1 << 9) | 0b10)
            await host.command(OP_PINS, data=(1 << 9) | (1 << 8) | 0b11)


@cocotb.test()
async def slow_response_with_the_largest_delay(dut):
    # 応答が 35 ns 遅いターゲットでも、どの sample_delay でも読める（基準点が立ち上がりの直後なので）
    host, model = await setup(dut, output_delay_ns=35)
    for delay in (0, 1, 2, 3):
        dut.sample_delay.value = delay
        await _session(host, delay)
