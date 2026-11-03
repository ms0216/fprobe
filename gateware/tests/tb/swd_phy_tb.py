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
        dut.pad_stages.value = 0
        dut.cmd_valid.value = 0
        dut.cmd_cond.value = 0
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
    for half in (1, 2, 5):                 # 半周期 1 = SWCLK sys / 2（下限）
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


async def _session(host, delay, label=None):
    await host.connect()
    assert await host.transfer(DP_READ_IDCODE) == (ACK_OK, DPIDR, 0), label or delay
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
        idle, half = rng.choice([0, 0, 1, 3, 8, 255]), rng.choice([1, 1, 2, 2, 3, 5])
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


# --- 線上の点の時刻（docs/superpowers/specs/2026-11-01-time-accuracy-design.md R5）---------------------------------------

async def record_rises(dut, rises: list):
    """SWCLK の立ち上がりごとに、その周期の now（ハーネスの周期のカウンタ）を rises に足す。"""
    prev = 1
    while True:
        await RisingEdge(dut.clk)
        await ReadOnly()
        level = int(dut.swclk.value)
        if level and not prev:
            rises.append(int(dut.now.value))
        prev = level


@cocotb.test()
async def rsp_time_is_the_rise_after_the_third_ack_bit(dut):
    """R5（SWD）: 転送の rsp_time = ACK の 3 ビット目の Low 相が終わって SWCLK が 1 になった最初の周期（転送の最初の立ち上がり
    から数えて 11 + turnaround 番目）。ヘッダの最初のビットの Low 相の始め（その立ち上がりの half_period 前）からの差は、
    ホストの fprobe.timing.swd_header_to_point_ticks と一致する。hp・turnaround・sample_delay・data_phase・idle・ACK（OK・WAIT）・
    RnW の組で（ACK・ターンの後の相の長さ・延びに依らないこと）。"""
    from fprobe.timing import swd_header_to_point_ticks
    host, model = await setup(dut)
    await host.connect()
    assert (await host.transfer(DP_WRITE_CTRL, CDBGPWRUPREQ))[0] == ACK_OK
    rises: list = []
    cocotb.start_soon(record_rises(dut, rises))
    acks = set()
    for hp in (1, 2, 3, 5):
        for trn in (1, 2, 4):
            for sd, dphase, idle in ((0, 0, 0), (3, 1, 2), (1, 0, 5), (7, 0, 0)):
                dut.half_period.value, dut.turnaround.value, model.turnaround = hp, trn, trn
                dut.sample_delay.value, dut.data_phase.value, dut.idle_cycles.value = sd, dphase, idle
                await RisingEdge(dut.clk)
                for request, wait in ((DP_READ_IDCODE, 0), (DP_WRITE_SELECT, 0), (AP_READ_DRW, 1), (AP_WRITE_TAR, 1),
                                      (AP_WRITE_TAR, 0), (AP_READ_DRW, 0)):
                    model.wait_next = wait
                    n = len(rises)
                    ack, _, _ = await host.transfer(request, 0)
                    first = rises[n]                                  # ヘッダの start ビットの立ち上がり
                    point = rises[n + 10 + trn]
                    got = int(dut.rsp_time.value)
                    assert got == point, (hp, trn, sd, dphase, idle, request, ack, got, rises[n:n + 14])
                    assert got - (first - hp) == swd_header_to_point_ticks(hp, trn), (hp, trn, request)
                    acks.add(ack)
    assert acks >= {ACK_OK, ACK_WAIT}, acks                  # ターンを変えた直後の応答の乱れ（FAULT・異常）でも点は同じ


@cocotb.test()
async def rsp_time_of_other_commands_is_when_they_were_accepted(dut):
    """転送でない命令（シーケンス）の rsp_time は、命令を受理した周期（線上の点を持たない）。"""
    host, _ = await setup(dut)
    d = dut
    d.cmd_op.value, d.cmd_count.value, d.cmd_data.value, d.cmd_valid.value = OP_SEQ_OUT, 8, 0, 1
    accepted = None
    for _ in range(100):
        await ReadOnly()
        if int(d.cmd_ready.value):
            accepted = int(d.now.value)
            await RisingEdge(d.clk)
            break
        await RisingEdge(d.clk)
    d.cmd_valid.value = 0
    for _ in range(1000):
        await ReadOnly()
        if int(d.rsp_valid.value):
            assert int(d.rsp_time.value) == accepted
            return
        await RisingEdge(d.clk)
    raise AssertionError("応答しない")


# --- sys / 2（half_period = 1）・取り込みの遅れ・続けて受ける（docs/superpowers/specs/2026-11-03-dap-line-rate-design.md）-----

def window(hp: int, pad: int, delay_ns: float, period_ns: float = 10.0) -> set[int]:
    """取り込みの遅れ d が正しく読む範囲（導出）。サンプル点 = 立ち上げる辺 + d 周期（din はその辺の直前のピン）。端子の
    立ち上がりは辺の pad 周期後、ターゲットはその delay_ns 後にビットを変える。ビット n は [前の端子の立ち上がり + delay、
    端子の立ち上がり + delay] で有効（辺に重ならない delay を使う）: d·T ∈ (pad·T + delay − 2·hp·T, pad·T + delay)。"""
    lo, hi = pad * period_ns + delay_ns - 2 * hp * period_ns, pad * period_ns + delay_ns
    return {d for d in range(8) if lo < d * period_ns < hi}


@cocotb.test()
async def sample_delay_window_matches_the_round_trip(dut):
    """往復の遅れ（ボードの出力の段 pad_stages + ターゲットの出力の遅れ）に対し、正しく読める取り込みの遅れの範囲が導出
    （window）と一致する。half_period 1（sys / 2 = 50 MHz）と 2。範囲の外の遅れは読み違える（取り込みの点が動く）。"""
    host, model = await setup(dut, output_delay_ns=3)
    checked = 0
    for hp in (1, 2):
        for pad in (0, 1):
            for delay_ns in (3, 13, 23, 33):
                dut.half_period.value, dut.pad_stages.value = hp, pad
                model.output_delay_ns = delay_ns
                good = window(hp, pad, delay_ns)
                assert good, (hp, pad, delay_ns)
                for d in range(8):
                    dut.sample_delay.value = d
                    await RisingEdge(dut.clk)
                    if d in good:
                        await _session(host, d, (hp, pad, delay_ns, d))
                    else:
                        await host.connect()
                        ack, value, _ = await host.transfer(DP_READ_IDCODE)
                        assert (ack, value) != (ACK_OK, DPIDR), (hp, pad, delay_ns, d)
                    checked += 1
    assert checked == 2 * 2 * 4 * 8


async def stream(dut, commands, *, cond=True, timeout=20000):
    """命令の列を空きなく出す（受理された周期の辺で次の命令に替える。2 つ目からは cmd_cond = cond）。受理された命令の数と
    応答の列 [(ack, data, parity)] を返す。受理されないまま応答がすべて揃って 50 周期たったら、出している命令を取り下げる。"""
    d = dut
    rsps: list = []

    async def collect():
        while True:
            await RisingEdge(d.clk)
            await ReadOnly()
            if int(d.rsp_valid.value):
                rsps.append((int(d.rsp_ack.value), int(d.rsp_data.value) & 0xFFFFFFFF, int(d.rsp_parity_error.value)))

    watcher = cocotb.start_soon(collect())
    accepted, idle = 0, 0
    for i, (op, request, data) in enumerate(commands):
        d.cmd_op.value, d.cmd_count.value, d.cmd_request.value, d.cmd_data.value = op, 1, request, data
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


async def record_levels(dut, levels: list):
    while True:
        await RisingEdge(dut.clk)
        await ReadOnly()
        levels.append(int(dut.swclk.value))


def stall_cycles(hp: int, d: int, trn: int, reading: bool) -> int:
    """1 転送で High 相が延びる周期数（導出。性能の約束）: ACK の 3 ビット目の取り込み（予約の辺 + d + 1）が High 相の終わり
    （予約の辺 + hp）に間に合わない分と、読みの最後（パリティ）の取り込みがターンの終わりに間に合わない分。"""
    ack = max(0, d + 1 - hp)
    end = max(0, d + 1 - hp - 2 * hp * trn) if reading else 0
    return ack + end


@cocotb.test()
async def back_to_back_transfers_have_no_gap(dut):
    """続けて出した転送の間に SWCLK の空きが無い: 転送の列の SWCLK の周期の合計 = Σ（ビット数 × 2·hp + 延び）。延びは
    取り込みの遅れが High 相に間に合わない分だけ（stall_cycles）で、転送の境目には 1 周期も入らない。half_period 1・2、
    取り込みの遅れ 0〜3、ターン 1・2、読み・書きを混ぜて、値も正しい。"""
    host, model = await setup(dut)
    await host.connect()
    assert (await host.transfer(DP_WRITE_CTRL, CDBGPWRUPREQ))[0] == ACK_OK
    await host.transfer(DP_WRITE_SELECT, 0)
    await host.transfer(AP_WRITE_CSW, 0x23000012)
    checked = 0
    for hp in (1, 2):
        for trn in (1, 2):
            for sd in (0, 1, 2, 3):
                delay = 10 * sd + 3                                     # 取り込みの点に合うターゲットの遅れ
                assert sd in window(hp, 0, delay), (hp, sd)
                dut.half_period.value, dut.turnaround.value, model.turnaround = hp, trn, trn
                dut.sample_delay.value = sd
                model.output_delay_ns = delay
                await host.transfer(AP_WRITE_TAR, 0x20000000)
                values = [(hp * 0x1000 + trn * 0x100 + sd * 0x10 + i) for i in range(6)]
                cmds = [(OP_TRANSFER, AP_WRITE_DRW, v) for v in values]
                cmds += [(OP_TRANSFER, AP_WRITE_TAR, 0x20000000)]
                cmds += [(OP_TRANSFER, AP_READ_DRW, 0) for _ in values] + [(OP_TRANSFER, DP_READ_RDBUFF, 0)]
                levels: list = []
                rec = cocotb.start_soon(record_levels(dut, levels))
                accepted, rsps = await stream(dut, cmds)
                rec.cancel()
                assert accepted == len(cmds) and len(rsps) == len(cmds), (hp, trn, sd, accepted, len(rsps))
                assert all(r[0] == ACK_OK and r[2] == 0 for r in rsps), (hp, trn, sd, rsps)
                assert [r[1] for r in rsps[len(values) + 2:]] == values, (hp, trn, sd)
                # 最初の立ち下がりから、最後の立ち上がりの後の High 相の終わりまで
                first = levels.index(0)
                last = len(levels) - 1 - levels[::-1].index(0)
                span = last - first + 1 + hp
                bits = 8 + trn + 3 + trn + 33                       # 書き・読みとも
                expect = sum(bits * 2 * hp + stall_cycles(hp, sd, trn, c[1] & 0b10 != 0) for c in cmds)
                expect -= stall_cycles(hp, sd, trn, True) - stall_cycles(hp, sd, trn, False)   # 最後の延びは High のまま見えない
                assert span == expect, (hp, trn, sd, span, expect)
                checked += 1
    assert checked == 16, checked


@cocotb.test()
async def a_conditional_command_is_not_accepted_after_a_failed_transfer(dut):
    """cmd_cond = 1 の命令は、直前の転送が OK でなければ（WAIT・FAULT・読みのパリティ異常）受けない（線に出ない）。OK なら
    続けて受ける。条件なし（cmd_cond = 0）の命令は受ける。"""
    host, model = await setup(dut)
    dut.half_period.value = 1
    await host.connect()
    assert (await host.transfer(DP_WRITE_CTRL, CDBGPWRUPREQ))[0] == ACK_OK
    await host.transfer(DP_WRITE_SELECT, 0)
    n = len(model.transactions)
    model.wait_next = 1
    accepted, rsps = await stream(dut, [(OP_TRANSFER, AP_WRITE_TAR, 0x1234), (OP_TRANSFER, AP_WRITE_TAR, 0x5678)])
    assert accepted == 1 and [r[0] for r in rsps] == [ACK_WAIT], (accepted, rsps)
    assert len(model.transactions) == n + 1                       # 2 つ目は線に出ていない
    model.corrupt_next_parity = True
    accepted, rsps = await stream(dut, [(OP_TRANSFER, DP_READ_IDCODE, 0), (OP_TRANSFER, DP_WRITE_SELECT, 0xF0)])
    assert accepted == 1 and rsps == [(ACK_OK, DPIDR, 1)] and model.select == 0, (accepted, rsps)
    model.ctrl_stat |= STICKYERR
    accepted, rsps = await stream(dut, [(OP_TRANSFER, AP_WRITE_TAR, 1), (OP_TRANSFER, DP_WRITE_SELECT, 0xF0)])
    assert accepted == 1 and [r[0] for r in rsps] == [ACK_FAULT] and model.select == 0
    assert await host.transfer(DP_WRITE_ABORT, STKERRCLR) == (ACK_OK, 0, 0)      # 条件なしは受ける
    accepted, rsps = await stream(dut, [(OP_TRANSFER, AP_WRITE_TAR, 0x9ABC), (OP_TRANSFER, DP_WRITE_SELECT, 0xF0)])
    assert accepted == 2 and [r[0] for r in rsps] == [ACK_OK, ACK_OK] and model.tar == 0x9ABC and model.select == 0xF0
