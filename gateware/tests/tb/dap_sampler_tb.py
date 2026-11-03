"""dap_sampler（F4: 変数のライブ観測）のテストベンチ。dap_harness（dap_processor + swd_injector + dap_sampler +
dap_monitor + swd_phy）と SWD ターゲットモデルで、周期的な読み・ホストの状態の復元・境界・誤り・取りこぼしを試す。"""

import random

import cocotb
from cocotb.triggers import ClockCycles, ReadOnly, RisingEdge

from tests.tb.dap_jtag_tb import setup as jtag_setup
from tests.tb.dap_processor_tb import AP_DRW_R, AP_DRW_W, AP_TAR_W, DP_SELECT_W, OK, setup
from tests.tb.swd_injector_tb import AP_IDR_R, connected

AP_IDR = 0x24770011


async def configure(dut, slots, period=20_000, ap=0):
    """slots: [(番地, 語数)]（最大 8）。か所の表に 1 つずつ書き、残りは無効にする。"""
    for k in range(8):
        a, n = slots[k] if k < len(slots) else (0, 1)
        dut.smp_index.value = k
        dut.smp_addr.value = a
        dut.smp_enable.value = int(k < len(slots))
        dut.smp_words.value = n - 1
        dut.smp_write.value = 1
        await RisingEdge(dut.clk)
    dut.smp_write.value = 0
    dut.smp_ap.value = ap
    dut.smp_period.value = period


def collect(dut, out: list):
    async def run():
        while True:
            await RisingEdge(dut.clk)
            await ReadOnly()
            if dut.smp_valid.value == 1:
                out.append((int(dut.smp_sample_addr.value), int(dut.smp_data.value), int(dut.smp_ack.value)))
    return cocotb.start_soon(run())


async def rounds(dut, n, timeout=2_000_000):
    """巡回が n 回になるまで待つ。確かめた値の件は確認の応答から最大 3 周期後に出る（buf に積んでから順に出す）ので、
    その分も待つ（巡回の数は応答の次の周期に増える）。"""
    for _ in range(timeout):
        await RisingEdge(dut.clk)
        await ReadOnly()
        if int(dut.smp_rounds.value) >= n:
            await ClockCycles(dut.clk, 4)
            return
    raise TimeoutError


@cocotb.test()
async def samples_slots_and_restores_host_state(dut):
    dap, model = await connected(dut)
    for i in range(20):
        model.memory[0x20000000 + 4 * i] = 0x1000 + i
    model.memory[0x20000400] = 0xCAFE
    await dap.transfer((AP_TAR_W, 0x20000100))
    assert (await dap.block(AP_DRW_W, 2, [1, 2]))[:2] == (2, OK)
    await dap.transfer((DP_SELECT_W, 0xF0))                     # ホストは bank 0xF を選んだまま
    got = []
    task = collect(dut, got)
    await configure(dut, [(0x20000000, 3), (0x20000400, 1)])
    dut.smp_run.value = 1
    await rounds(dut, 2)
    dut.smp_run.value = 0
    await ClockCycles(dut.clk, 2000)
    task.cancel()
    assert got[:4] == [(0x20000000, 0x1000, OK), (0x20000004, 0x1001, OK), (0x20000008, 0x1002, OK),
                       (0x20000400, 0xCAFE, OK)]
    assert len(got) >= 8 and int(dut.smp_errors.value) == 0
    # SELECT（bank 0xF）・CSW（自動増加）・TAR（0x20000108）が戻っている
    assert await dap.transfer((AP_IDR_R, None)) == (1, OK, [AP_IDR])
    await dap.transfer((DP_SELECT_W, 0))
    assert (await dap.block(AP_DRW_W, 2, [3, 4]))[:2] == (2, OK)
    assert [model.memory[0x20000100 + 4 * i] for i in range(4)] == [1, 2, 3, 4]


@cocotb.test()
async def sixteen_words_in_one_slot(dut):
    dap, model = await connected(dut, seed=2)
    for i in range(16):
        model.memory[0x20000800 + 4 * i] = i * 3
    got = []
    task = collect(dut, got)
    await configure(dut, [(0x20000800, 16)])
    dut.smp_run.value = 1
    await rounds(dut, 1)
    dut.smp_run.value = 0
    await ClockCycles(dut.clk, 2000)                                      # 最後の語は巡回の後に回収して出す（先読み）
    task.cancel()
    assert [(a, d) for a, d, _ in got[:16]] == [(0x20000800 + 4 * i, i * 3) for i in range(16)]


@cocotb.test()
async def sampling_only_borrows_at_host_command_boundaries(dut):
    dap, model = await connected(dut, seed=3)
    model.memory[0x20001000] = 77
    got = []
    task = collect(dut, got)
    await configure(dut, [(0x20001000, 1)], period=3000)               # ホストのブロック転送の間にも周期が来る
    dut.smp_run.value = 1
    words = list(range(1, 301))
    await dap.transfer((AP_TAR_W, 0x20000000))
    assert (await dap.block(AP_DRW_W, len(words), words))[:2] == (300, OK)
    assert [model.memory[0x20000000 + 4 * i] for i in range(300)] == words
    await dap.transfer((AP_TAR_W, 0x20000000))
    done, ack, values = await dap.block(AP_DRW_R, 300)
    bad = [(i, v, w) for i, (v, w) in enumerate(zip(values, words)) if v != w]
    assert (done, ack) == (300, OK) and not bad, (len(values), bad[:6])     # ホストの読みも壊れない
    dut.smp_run.value = 0
    await ClockCycles(dut.clk, 2000)
    task.cancel()
    assert got and all(a == 0x20001000 and d == 77 for a, d, _ in got)


@cocotb.test()
async def wait_is_retried_and_fault_aborts_the_slot(dut):
    dap, model = await connected(dut, seed=4)
    model.memory[0x20000000] = 5
    model.memory[0x20000010] = 6
    model.fault_addresses.add(0x20000010)
    got = []
    task = collect(dut, got)
    model.wait_next = 3
    await configure(dut, [(0x20000000, 1), (0x20000010, 1)])
    dut.smp_run.value = 1
    await rounds(dut, 1)
    dut.smp_run.value = 0
    await ClockCycles(dut.clk, 2000)
    task.cancel()
    assert (0x20000000, 5, OK) in got and all(a != 0x20000010 for a, _, _ in got)   # 誤りの語は出さない
    assert int(dut.smp_errors.value) >= 1
    # サンプラが起こした誤りは消してあり、ホストの次の転送は FAULT にならない。CSW（自動増加）も戻っている
    model.fault_addresses.clear()
    await dap.transfer((AP_TAR_W, 0x20000200))
    assert (await dap.block(AP_DRW_W, 2, [7, 8]))[:2] == (2, OK)
    assert [model.memory[0x20000200], model.memory[0x20000204]] == [7, 8]


@cocotb.test()
async def a_period_shorter_than_a_round_counts_overruns(dut):
    dap, model = await connected(dut, seed=5)
    await configure(dut, [(0x20000000 + 0x40 * k, 16) for k in range(8)], period=500)
    dut.smp_run.value = 1
    await rounds(dut, 2)
    start = int(dut.smp_rounds.value)
    gaps, last, cycle = [], None, 0
    while int(dut.smp_rounds.value) < start + 3:                          # 周期に追いつかない: 次の巡回は間を空けない
        await RisingEdge(dut.clk)
        await ReadOnly()
        cycle += 1
        if int(dut.smp_busy.value) == 0:
            gaps.append(cycle)
    await RisingEdge(dut.clk)
    dut.smp_run.value = 0
    assert int(dut.smp_overruns.value) > 0
    assert len(gaps) <= 3 * 3, gaps                                       # 巡回の間に待つ周期は（ほぼ）無い


@cocotb.test()
async def nothing_is_read_without_a_connection(dut):
    await setup(dut)
    got = []
    task = collect(dut, got)
    await configure(dut, [(0x20000000, 1)], period=200)
    dut.smp_run.value = 1
    await ClockCycles(dut.clk, 5000)
    task.cancel()
    assert got == [] and int(dut.smp_rounds.value) == 0


@cocotb.test()
async def jitter_varies_the_period_but_keeps_the_mean(dut):
    """jitter = 7: 周期に 0〜127 を足し 64 を引く（平均は period のまま）。PC サンプリングのエイリアシング対策。"""
    dap, model = await connected(dut, seed=6)
    model.memory[0x20000000] = 1
    times = []

    async def watch():
        cycle = 0
        while True:
            await RisingEdge(dut.clk)
            await ReadOnly()
            cycle += 1
            if dut.smp_valid.value == 1:
                times.append(cycle)
    task = cocotb.start_soon(watch())
    dut.smp_jitter.value = 7
    await configure(dut, [(0x20000000, 1)], period=8000)
    dut.smp_run.value = 1
    await rounds(dut, 41)
    dut.smp_run.value = 0
    task.cancel()
    # 最初の巡回は SELECT・CSW・TAR を書いてから読むので値が遅れる（B17: 以後は持ったまま）。2 巡回目からの間隔を見る
    gaps = [b - a for a, b in zip(times[1:], times[2:])]
    assert len(set(gaps)) > 5, gaps                                   # 揃っていない
    assert all(8000 - 64 - 40 <= g <= 8000 + 63 + 40 for g in gaps), gaps
    assert abs(sum(gaps) / len(gaps) - 8000) < 25, sum(gaps) / len(gaps)
    assert int(dut.smp_overruns.value) == 0
    dut.smp_jitter.value = 0


# --- B17: 遅延の戻し・状態の追跡・1 転送の検査（docs/superpowers/specs/2026-10-18-b17-sampler-lazy-restore-design.md）---

AP_CSW_R, AP_TAR_R = 0x03, 0x07
HOST_CSW = 0x23000012                                   # mem_setup: 32 ビット・自動増加
SELECT_W, CSW_W, TAR_W, DRW_R = (0, 0, 0x8), (1, 0, 0x0), (1, 0, 0x4), (1, 1, 0xC)
RDBUFF_R, CTRL_R = (0, 1, 0xC), (0, 1, 0x4)
BIT_CYCLES = 10                                         # SWD 10 MHz（clk 100 MHz、半周期 5）
TRANSFER_CYCLES = 46 * BIT_CYCLES                       # ヘッダ 8・ターン 1・ACK 3・データ 33・ターン 1（idle 0）


def count(model, since: int, kind: tuple) -> int:
    """モデルが受けた転送（since 以降）のうち (APnDP, RnW, A) が kind のもの（応答は問わない）。"""
    return sum(1 for t in model.transactions[since:] if t[:3] == kind)


async def steady_rounds(dut, model, n: int) -> tuple[int, int, int]:
    """巡回が n 回進む間を見る。(始めの転送の位置, 巡回の数, 周期数)。"""
    await rounds(dut, int(dut.smp_rounds.value) + 2)                      # 最初の書き込みの後の定常から
    mark, start, cycles = len(model.transactions), int(dut.smp_rounds.value), 0
    while int(dut.smp_rounds.value) < start + n:
        await RisingEdge(dut.clk)
        await ReadOnly()
        cycles += 1
    await RisingEdge(dut.clk)
    return mark, int(dut.smp_rounds.value) - start, cycles


@cocotb.test()
async def idle_rounds_do_not_restore_and_take_one_transfer(dut):
    """ホストが線を使っていない間は戻さない（restores = 0、held）。1 か所 1 語を間を空けずに読む定常は DRW の 1 転送
    （自動増加なしの CSW で TAR を書き直さない。前の巡回の値は次の DRW で返り、その ACK が誤りの確認。stop-precision
    §3.6。先読みの前は DRW・RDBUFF・CTRL/STAT の 3 転送、B17 の前は戻しを含めて 9）。"""
    dap, model = await connected(dut, seed=7)
    model.memory[0x20000000] = 42
    await dap.transfer((AP_TAR_W, 0x20000100))                          # 控え: CSW（増加）・TAR が分かる
    got = []
    task = collect(dut, got)
    await configure(dut, [(0x20000000, 1)], period=1)                    # できるだけ速く
    dut.smp_run.value = 1
    mark, n, cycles = await steady_rounds(dut, model, 20)
    assert [count(model, mark, k) for k in (SELECT_W, CSW_W, TAR_W)] == [0, 0, 0]
    assert abs(count(model, mark, DRW_R) - n) <= 1
    assert [count(model, mark, k) for k in (RDBUFF_R, CTRL_R)] == [0, 0]
    assert len(model.transactions) - mark <= n + 1
    assert cycles / n < TRANSFER_CYCLES + 30, cycles / n                  # 先読みの前は 3 転送
    assert int(dut.smp_restores.value) == 0 and int(dut.smp_held.value) == 1
    # ホストの次の命令の前に 1 度だけ戻す（CSW・TAR・SELECT）。巡回は続く
    mark = len(model.transactions)
    assert await dap.transfer((AP_CSW_R, None), (AP_TAR_R, None)) == (2, OK, [HOST_CSW, 0x20000100])
    assert int(dut.smp_restores.value) == 1
    assert all(1 <= count(model, mark, k) <= 2 for k in (SELECT_W, CSW_W, TAR_W))   # 戻し 1 組（+ 借り直し 1 組）
    # 止めたら戻して渡す（AP 等の設定は止めてから変える）
    await rounds(dut, int(dut.smp_rounds.value) + 2)
    dut.smp_run.value = 0
    await ClockCycles(dut.clk, 3000)
    task.cancel()
    assert got and all(a == 0x20000000 and d == 42 and k == OK for a, d, k in got)
    assert int(dut.smp_held.value) == 0 and int(dut.smp_restores.value) == 2
    assert await dap.transfer((AP_CSW_R, None), (AP_TAR_R, None)) == (2, OK, [HOST_CSW, 0x20000100])


@cocotb.test()
async def host_commands_between_rounds_see_their_own_state(dut):
    """巡回の合間にホストが SELECT（bank 0xF）・CSW・TAR を使う。ホストから見た状態と値、サンプラの値が正しい。"""
    dap, model = await connected(dut, seed=8)
    for i in range(3):
        model.memory[0x20000000 + 4 * i] = 0x100 + i
    model.memory[0x20000400] = 0xBEEF
    expect = {0x20000400: 0xBEEF, **{0x20000000 + 4 * i: 0x100 + i for i in range(3)}}
    got = []
    task = collect(dut, got)
    await configure(dut, [(0x20000400, 1), (0x20000000, 3)], period=700)
    dut.smp_run.value = 1
    rng = random.Random(3)
    base, written = 0x20000200, []
    await dap.transfer((AP_TAR_W, base))
    for k in range(12):
        await ClockCycles(dut.clk, rng.randrange(0, 4000))
        values = [rng.getrandbits(32) for _ in range(rng.randrange(1, 4))]
        assert (await dap.block(AP_DRW_W, len(values), values))[:2] == (len(values), OK)   # TAR は続きから
        written += values
        if k % 3 == 0:
            assert await dap.transfer((DP_SELECT_W, 0xF0), (AP_IDR_R, None), (DP_SELECT_W, 0)) == (3, OK, [AP_IDR])
        if k % 4 == 1:
            assert await dap.transfer((AP_CSW_R, None), (AP_TAR_R, None)) == (2, OK, [HOST_CSW, base + 4 * len(written)])
    assert [model.memory[base + 4 * i] for i in range(len(written))] == written
    await dap.transfer((AP_TAR_W, base))
    assert await dap.block(AP_DRW_R, len(written)) == (len(written), OK, written)
    dut.smp_run.value = 0
    await ClockCycles(dut.clk, 3000)
    task.cancel()
    assert len(got) > 40 and all(expect[a] == d and k == OK for a, d, k in got), got[:8]
    assert int(dut.smp_errors.value) == 0 and int(dut.smp_restores.value) >= 12


@cocotb.test()
async def mixed_slots_do_not_rewrite_select_or_csw(dut):
    """か所が複数なら 1 語のか所も自動増加 single のまま読み、定常で SELECT・CSW を書かない（TAR はか所ごとに 1 回）。"""
    dap, model = await connected(dut, seed=9)
    for i in range(18):
        model.memory[0x20000800 + 4 * i] = 0x5000 + i
    model.memory[0x20000100] = 0x77
    expect = {0x20000100: 0x77, **{0x20000800 + 4 * i: 0x5000 + i for i in range(18)}}
    got = []
    task = collect(dut, got)
    await configure(dut, [(0x20000800, 16), (0x20000840, 2), (0x20000100, 1)], period=1)
    dut.smp_run.value = 1
    mark, n, _ = await steady_rounds(dut, model, 4)
    steady = model.transactions[mark:]                                    # 止めたときの戻しは数えない
    dut.smp_run.value = 0
    await ClockCycles(dut.clk, 12000)                                     # 16 語のか所の途中なら読み終えてから戻す
    task.cancel()
    assert [sum(t[:3] == k for t in steady) for k in (SELECT_W, CSW_W)] == [0, 0]
    assert abs(sum(t[:3] == TAR_W for t in steady) - 3 * n) <= 3
    assert len(got) >= 19 * 4 and all(expect[a] == d for a, d, _ in got)
    assert int(dut.smp_errors.value) == 0


@cocotb.test()
async def a_disconnect_while_holding_the_line_drops_it(dut):
    """held のまま切断されたら戻さずに線を放す。つなぎ直した後は借りるときに全部書き直して正しく読む。"""
    dap, model = await connected(dut, seed=10)
    model.memory[0x20000000] = 9
    await dap.transfer((AP_TAR_W, 0x20000100))
    await configure(dut, [(0x20000000, 1)], period=2000)
    dut.smp_run.value = 1
    await rounds(dut, 2)
    assert int(dut.smp_held.value) == 1
    assert await dap.command(bytes([0x03])) == bytes([0x03, 0])            # Disconnect（PHY を使わない）
    await ClockCycles(dut.clk, 3000)
    assert int(dut.smp_held.value) == 0 and int(dut.smp_busy.value) == 0
    model.memory[0x20000000] = 10
    await dap.connect()
    await dap.mem_setup()
    mark = len(model.transactions)
    got = []
    task = collect(dut, got)
    await rounds(dut, int(dut.smp_rounds.value) + 2)
    task.cancel()
    assert count(model, mark, SELECT_W) >= 1 and count(model, mark, CSW_W) >= 1 and count(model, mark, TAR_W) >= 1
    assert got and all(a == 0x20000000 and d == 10 for a, d, _ in got)
    dut.smp_run.value = 0
    await ClockCycles(dut.clk, 2000)


@cocotb.test()
async def jtag_checks_the_last_word_with_ctrl_stat(dut):
    """JTAG-DP（ACK に FAULT は無い）でも、最後の語の誤りを RDBUFF + CTRL/STAT で見つけ、ホストの次の転送を壊さない。"""
    dap, dp = await jtag_setup(dut)
    await dap.mem_setup()
    for i in range(2):
        dp.regs.memory[0x20000000 + 4 * i] = 0x30 + i
    dp.regs.fault_addresses.add(0x20000010)
    got = []
    task = collect(dut, got)
    await configure(dut, [(0x20000000, 2), (0x20000010, 1)], period=5000)
    dut.smp_run.value = 1
    await rounds(dut, 3)
    dut.smp_run.value = 0
    await ClockCycles(dut.clk, 5000)
    task.cancel()
    assert {(a, d) for a, d, _ in got} == {(0x20000000, 0x30), (0x20000004, 0x31)}
    assert int(dut.smp_errors.value) >= 3
    dp.regs.fault_addresses.clear()
    await dap.transfer((AP_TAR_W, 0x20000200))
    assert (await dap.block(AP_DRW_W, 2, [7, 8]))[:2] == (2, OK)
    assert [dp.regs.memory[0x20000200], dp.regs.memory[0x20000204]] == [7, 8]


@cocotb.test()
async def jtag_checks_every_word_before_it_is_given(dut):
    """stop-precision §3.6: JTAG では語ごとに CTRL/STAT で確かめてから出す（最後の語だけでなく）。3 語のか所の真ん中の語が
    バスの誤りなら、前の語は出し、誤りの語とか所の残りは出さない。か所の値の並びは毎巡回同じ。"""
    dap, dp = await jtag_setup(dut)
    await dap.mem_setup()
    for i in range(3):
        dp.regs.memory[0x20000000 + 4 * i] = 0x40 + i
    dp.regs.memory[0x20000100] = 0x77
    dp.regs.fault_addresses.add(0x20000004)
    got = []
    task = collect(dut, got)
    await configure(dut, [(0x20000000, 3), (0x20000100, 1)], period=5000)
    dut.smp_run.value = 1
    await rounds(dut, 3)
    dut.smp_run.value = 0
    await ClockCycles(dut.clk, 5000)
    task.cancel()
    assert [(a, d) for a, d, _ in got] == [(0x20000000, 0x40), (0x20000100, 0x77)] * 3, got
    assert int(dut.smp_errors.value) == 3
    dp.regs.fault_addresses.clear()
    await dap.transfer((AP_TAR_W, 0x20000200))
    assert (await dap.block(AP_DRW_W, 2, [7, 8]))[:2] == (2, OK)
    assert [dp.regs.memory[0x20000200], dp.regs.memory[0x20000204]] == [7, 8]


@cocotb.test()
async def changing_the_ap_after_a_stop_selects_it(dut):
    """止めると戻して渡すので、AP（control.ap）は止めてから変える。次に借りたとき新しい AP を選ぶ。"""
    dap, model = await connected(dut, seed=11)
    model.memory[0x20000000] = 5
    await dap.transfer((AP_TAR_W, 0x20000100))
    got = []
    task = collect(dut, got)
    await configure(dut, [(0x20000000, 1)], period=2000)
    dut.smp_run.value = 1
    await rounds(dut, 2)
    mark = len(model.transactions)
    dut.smp_run.value = 0
    await ClockCycles(dut.clk, 5000)
    assert (int(dut.smp_held.value), int(dut.smp_restores.value)) == (0, 1), (int(dut.smp_held.value), int(dut.smp_restores.value))
    dut.smp_ap.value = 1                                                  # モデルは AP を区別しない（同じメモリ）
    dut.smp_run.value = 1
    await rounds(dut, int(dut.smp_rounds.value) + 2)
    dut.smp_run.value = 0
    await ClockCycles(dut.clk, 3000)
    task.cancel()
    selects = [t[4] for t in model.transactions[mark:] if t[:3] == SELECT_W]
    assert selects == [0, 0x01000000, 0], selects                         # 戻す・新しい AP・戻す
    assert got and all(a == 0x20000000 and d == 5 for a, d, _ in got)
    assert await dap.transfer((AP_TAR_R, None)) == (1, OK, [0x20000100])
    dut.smp_ap.value = 0


@cocotb.test()
async def host_commands_at_any_moment_see_their_own_state(dut):
    """ホストの命令が巡回のどの瞬間に来ても（か所の途中・戻しの直前・持ったまま休んでいる間）、ホストから見た
    SELECT・CSW・TAR はホストの値のまま。1 か所 1 語（自動増加なし・TAR を書かない形）と複数か所の両方で。"""
    dap, model = await connected(dut, seed=12)
    model.memory[0x20000000] = 0x11
    model.memory[0x20000004] = 0x22
    model.memory[0x20000300] = 0x33
    rng = random.Random(12)
    got = []
    task = collect(dut, got)
    for slots in ([(0x20000000, 1)], [(0x20000000, 2), (0x20000300, 1)]):
        dut.smp_run.value = 0
        await ClockCycles(dut.clk, 3000)
        await configure(dut, slots, period=1)                             # できるだけ速く（常に線を持つ）
        dut.smp_run.value = 1
        tar = 0x20000600
        await dap.transfer((AP_TAR_W, tar))
        for k in range(25):
            await ClockCycles(dut.clk, rng.randrange(0, 1400))           # 1 転送 ≈ 460 周期: どの位置にも当たる
            if k % 3 == 0:
                assert await dap.transfer((DP_SELECT_W, 0xF0), (AP_IDR_R, None), (DP_SELECT_W, 0)) == (3, OK, [AP_IDR])
            value = rng.getrandbits(32)
            assert (await dap.block(AP_DRW_W, 1, [value]))[:2] == (1, OK)
            assert model.memory[tar] == value
            tar += 4                                                       # ホストの CSW は自動増加
            assert await dap.transfer((AP_CSW_R, None), (AP_TAR_R, None)) == (2, OK, [HOST_CSW, tar])
    dut.smp_run.value = 0
    await ClockCycles(dut.clk, 3000)
    task.cancel()
    expect = {0x20000000: 0x11, 0x20000004: 0x22, 0x20000300: 0x33}
    assert len(got) > 50 and all(expect[a] == d and k == OK for a, d, k in got), got[:8]
    assert int(dut.smp_errors.value) == 0


@cocotb.test()
async def wait_retries_follow_the_host_transfer_configure(dut):
    # 総点検 G2: WAIT の再試行の回数は固定（255）でなく、ホストが DAP_TransferConfigure で設定した値（wait_retry）
    import struct
    dap, model = await connected(dut, seed=11)
    model.memory[0x20000000] = 5
    await dap.command(bytes([0x04, 0]) + struct.pack("<HH", 1, 3))             # WAIT は 1 回まで
    got = []
    task = collect(dut, got)
    await configure(dut, [(0x20000000, 1)])
    model.wait_next = 3                                                          # 次の 3 回の AP アクセスは WAIT
    dut.smp_run.value = 1
    await rounds(dut, 2)
    dut.smp_run.value = 0
    await ClockCycles(dut.clk, 2000)
    task.cancel()
    assert int(dut.smp_errors.value) >= 1                                        # 1 回で諦めて誤りに数える
    errors = int(dut.smp_errors.value)
    await dap.command(bytes([0x04, 0]) + struct.pack("<HH", 8, 3))             # 8 回まで待つ
    model.wait_next = 3
    dut.smp_run.value = 1
    await rounds(dut, 4)
    dut.smp_run.value = 0
    await ClockCycles(dut.clk, 2000)
    assert int(dut.smp_errors.value) == errors                                   # 3 回の WAIT は待って通る


async def configure_pattern(dut, table, period=20_000):
    """table: 8 要素の (番地, 語数) か None（無効）。"""
    for k, slot in enumerate(table):
        a, n = slot or (0, 1)
        dut.smp_index.value = k
        dut.smp_addr.value = a
        dut.smp_enable.value = int(slot is not None)
        dut.smp_words.value = n - 1
        dut.smp_write.value = 1
        await RisingEdge(dut.clk)
    dut.smp_write.value = 0
    dut.smp_ap.value = 0
    dut.smp_period.value = period


@cocotb.test()
async def random_tables_are_read_in_slot_order_with_every_word(dut):
    """ランダムな表（有効なか所の並び・語数 1〜16・番地）を、表を書き換えながら何度か巡回させ、出てきた（番地, 値）が
    「有効なか所を番号順に、各か所の語を番地順に」の期待に一致する（途中の巡回の分も欠けず、重複しない）。"""
    dap, model = await connected(dut, seed=21)
    rng = random.Random(5)
    for k in range(8 * 256):                                    # 各か所は別の 1 KB に置く
        model.memory[0x20000000 + 4 * k] = rng.getrandbits(32)
    for trial in range(6):
        table = [None] * 8
        for k in rng.sample(range(8), rng.choice([1, 1, 2, 5, 8])):
            words = rng.choice([1, 1, 2, 7, 16])
            table[k] = (0x20000000 + 0x400 * k + 4 * rng.randrange(0, 256 - words + 1), words)
        expected = [(a + 4 * i, model.memory[a + 4 * i])
                    for slot in table if slot for a, n in [slot] for i in range(n)]
        got = []
        task = collect(dut, got)
        await configure_pattern(dut, table, period=rng.choice([1, 300, 5000]))
        dut.smp_run.value = 1
        await rounds(dut, int(dut.smp_rounds.value) + 4)
        dut.smp_run.value = 0
        # 止めても、読んでいるか所（最大 16 語 ≈ 7500 周期）は読み終えてから休む: 休むまで待つ（決めた周期数では足りないことがある）
        for _ in range(40):
            await ClockCycles(dut.clk, 1000)
            if int(dut.smp_busy.value) == 0:
                break
        await ClockCycles(dut.clk, 100)
        task.cancel()
        assert [(a, d) for a, d, _ in got[:3 * len(expected)]] == expected * 3, (trial, table)
        assert all(k == OK for _, _, k in got) and int(dut.smp_errors.value) == 0


@cocotb.test()
async def each_sample_carries_its_slot_and_word_index(dut):
    dap, model = await connected(dut, seed=4)
    for i in range(3):
        model.memory[0x20000000 + 4 * i] = 0x10 + i
    model.memory[0x20000400] = 0x99
    got = []

    async def run():
        while True:
            await RisingEdge(dut.clk)
            await ReadOnly()
            if dut.smp_valid.value == 1:
                got.append((int(dut.smp_sample_addr.value), int(dut.smp_sample_index.value)))
    task = cocotb.start_soon(run())
    await configure(dut, [(0x20000000, 3), (0x20000400, 1)])
    dut.smp_run.value = 1
    await rounds(dut, 1)
    dut.smp_run.value = 0
    await ClockCycles(dut.clk, 2000)
    task.cancel()
    # か所 0 の語 0〜2 は 0〜2、か所 1 の語 0 は 16（か所 × 16 + 語）
    assert got[:4] == [(0x20000000, 0), (0x20000004, 1), (0x20000008, 2), (0x20000400, 16)]


def _tar_writes_before_and_after_halt(model, since):
    """since 以降の転送で、DHCSR への停止の書き込み（DRW ← HALT）の前と後の TAR の書き込みの値。"""
    from tests.tb.swd_injector_tb import HALT
    ts = model.transactions[since:]
    k = next(i for i, t in enumerate(ts) if t[:3] == (1, 0, 0xC) and t[4] == HALT)
    tars = [(i, t[4]) for i, t in enumerate(ts) if t[:3] == TAR_W]
    return [v for i, v in tars if i < k], [v for i, v in tars if i > k]


@cocotb.test()
async def an_injection_takes_the_line_straight_from_the_sampler(dut):
    """halt-watch-design §3.3: サンプラが線を持っている間に割り込み書き込みが来たら、ホストの状態に戻さずに渡す
    （戻しの CSW・TAR・SELECT の 3 転送を省く）。割り込み書き込みが最後にホストの控えに戻すので、ホストからは同じに見える。"""
    from tests.tb.swd_injector_tb import DHCSR, HALT, inject
    dap, model = await connected(dut, seed=8)
    model.memory[0x20000000] = 42
    await dap.transfer((AP_TAR_W, 0x20000100))                          # 控え: CSW（増加）・TAR が分かる（AP 0）
    await configure(dut, [(0x20000000, 1)], period=1)
    dut.smp_run.value = 1
    await steady_rounds(dut, model, 5)
    mark = len(model.transactions)
    assert await inject(dut) == 0
    assert model.memory[DHCSR] == HALT
    before, after = _tar_writes_before_and_after_halt(model, mark)
    assert 0x20000100 not in before                                     # サンプラは TAR を戻していない
    assert after[:1] == [0x20000100]                                    # 割り込み書き込みがホストの TAR に戻す
    assert int(dut.smp_restores.value) == 0 and int(dut.smp_handoffs.value) == 1
    # §3.7: 受け渡しの控えが正しいので、停止の書き込みまでに SELECT・CSW を書き直さない（TAR・DRW の 2 転送）
    ts = model.transactions[mark:]
    halt = next(i for i, t in enumerate(ts) if t[:3] == (1, 0, 0xC) and t[4] == HALT)
    assert ts[halt - 1][:3] == TAR_W and ts[halt - 1][4] == DHCSR
    assert not any(t[:3] in (SELECT_W, CSW_W) for t in ts[:halt]), ts[:halt]
    assert int(dut.inj_transfers.value) == 2
    await rounds(dut, int(dut.smp_rounds.value) + 2)                    # サンプラは借り直して読み続ける
    dut.smp_run.value = 0
    await ClockCycles(dut.clk, 3000)
    assert await dap.transfer((AP_CSW_R, None), (AP_TAR_R, None)) == (2, OK, [HOST_CSW, 0x20000100])


@cocotb.test()
async def an_injection_on_another_ap_waits_for_the_restore(dut):
    """AP が違えば、渡す前にサンプラの AP の CSW・TAR をホストの値に戻す（割り込み書き込みは自分の AP しか戻さない）。"""
    from tests.tb.swd_injector_tb import inject
    dap, model = await connected(dut, seed=9)
    model.memory[0x20000000] = 42
    await dap.transfer((AP_TAR_W, 0x20000100))
    await configure(dut, [(0x20000000, 1)], period=1)
    dut.smp_run.value = 1
    await steady_rounds(dut, model, 5)
    dut.inj_ap.value = 1
    mark = len(model.transactions)
    await inject(dut)
    ts = model.transactions[mark:]
    first_inj = next(i for i, t in enumerate(ts) if t[:3] == SELECT_W and t[4] == 0x01000000)   # 割り込み書き込みの SELECT ← AP 1
    assert any(t[:3] == TAR_W and t[4] == 0x20000100 for t in ts[:first_inj])   # その前にサンプラが TAR を戻した
    assert int(dut.smp_restores.value) >= 1 and int(dut.smp_handoffs.value) == 0
    dut.inj_ap.value = 0
    dut.smp_run.value = 0
    await ClockCycles(dut.clk, 3000)


@cocotb.test()
async def a_failed_first_injection_transfer_after_a_hand_off_still_restores_the_host_csw_and_tar(dut):
    """review finding 3: 直接の受け渡しの後、割り込み書き込みの最初の転送（§3.7 で SELECT・CSW を省くので TAR）が WAIT の
    使い切りで失敗しても、後始末は CSW・TAR をホストの控えに戻す（サンプラが戻していないので、戻さないとサンプラの値が残る）。"""
    from tests.tb.swd_injector_tb import inject
    dap, model = await connected(dut, seed=12)
    model.memory[0x20000000] = 42
    await dap.transfer((AP_TAR_W, 0x20000100))
    await configure(dut, [(0x20000000, 1), (0x20000010, 1)], period=1)   # 2 か所: サンプラの TAR は動く・CSW は自動増加
    dut.smp_run.value = 1
    await steady_rounds(dut, model, 3)
    mark = len(model.transactions)

    async def wait_after_hand_off():
        while int(dut.smp_handoffs.value) == 0:
            await RisingEdge(dut.clk)
        model.wait_next = 101                                            # wait_retry（既定 100）+ 1 回: 最初の TAR を使い切る
    cocotb.start_soon(wait_after_hand_off())
    assert await inject(dut) == 1
    model.wait_next = 0
    ts = model.transactions[mark:]
    first = next(i for i, t in enumerate(ts) if t[3] == 0b010)
    assert ts[first][:3] == TAR_W                                        # 渡された線で、最初の転送は TAR
    last_wait = max(i for i, t in enumerate(ts) if t[:4] == (*TAR_W, 0b010))
    after = [(t[:3], t[4]) for t in ts[last_wait + 1:last_wait + 4]]
    # 割り込み書き込みの後始末が、CSW・TAR をホストの控えに戻してから SELECT を戻す（サンプラは戻していない）
    assert after == [(CSW_W, HOST_CSW), (TAR_W, 0x20000100), (SELECT_W, 0)], after
    assert int(dut.smp_handoffs.value) == 1
    dut.smp_run.value = 0
    await ClockCycles(dut.clk, 3000)
    assert await dap.transfer((AP_CSW_R, None), (AP_TAR_R, None)) == (2, OK, [HOST_CSW, 0x20000100])


# --- 時刻と窓（time-accuracy R6）・JTAG の K 語の確認（bandwidth-observation 2.3、L1）----------------------------------
# 件の形は rtl/debug/dap_sampler.vhd の先頭: 7 = 語、17 = 値を返した転送の点、18 = 誤りの区間

MASK48 = (1 << 48) - 1


def watch_line(dut, records: list, transfers: list):
    """サンプラの件（(種類, 語)）と、サンプラが線を持つ間の PHY の応答（(点, 要求, ACK)）を順に集める。"""
    async def run():
        while True:
            await RisingEdge(dut.clk)
            await ReadOnly()
            if dut.smp_rec_valid.value == 1:
                records.append((int(dut.smp_rec_kind.value), int(dut.smp_rec_word.value)))
            if dut.rsp_valid_out.value == 1 and dut.smp_held.value == 1:
                transfers.append((int(dut.rsp_time.value) & MASK48, int(dut.smp_i.req_r.value),
                                  int(dut.smp_i.phy_rsp_ack.value)))
    return cocotb.start_soon(run())


def check_windows(records, transfers, returns_value):
    """R6 の並びの規則: 語（7）の時刻 = 読みに行った DRW の点、その後の最初のサンプラの件の時刻 = 値を返した転送の点
    （returns_value(要求) が真の、その後の最初の OK の転送）。[119:115] = その間の WAIT の回数（31 で止まる）。
    確かめた語の数を返す。"""
    points = [t for t, _, _ in transfers]
    checked = 0
    for k, (kind, w) in enumerate(records[:-1]):
        if kind != 7:
            continue
        t = w & MASK48
        assert t in points, f"語の時刻 {t} が線上の点でない"
        i = points.index(t)
        assert transfers[i][1] == 0b1111 and transfers[i][2] == OK, transfers[i]          # OK の DRW の読み
        j = next(j for j in range(i + 1, len(transfers)) if returns_value(transfers[j][1]) and transfers[j][2] == OK)
        waits = sum(1 for _, req, ack in transfers[i + 1:j] if ack == 0b010 and req == transfers[j][1])
        end = records[k + 1][1] & MASK48
        assert end == transfers[j][0], (k, records[k + 1], transfers[i:j + 1])
        assert w >> 115 & 0x1F == min(waits, 31), (k, w >> 115 & 0x1F, waits)
        if records[k + 1][0] == 17:
            assert records[k + 1][1] >> 112 & 0xF == transfers[j][1]                       # その転送の要求
            assert records[k + 1][1] >> 48 & 0xFFFFFFFF == w >> 48 & 0xFFFFFFFF           # その語の番地
        checked += 1
    return checked


@cocotb.test()
async def swd_sample_times_are_the_read_points_and_the_next_record_ends_the_window(dut):
    """R6（SWD）: 語の時刻は読みに行った DRW の転送の線上の点。窓の後端は次のサンプラの件の時刻で、それは値を返した転送
    （次の DRW の読みか回収の RDBUFF）の点。WAIT を繰り返した転送は WAIT の回数が [119:115] に入る。か所をまたいでも
    （TAR の書きは値を返さない）、巡回の終わりの回収（RDBUFF → 種類 17）でも。"""
    dap, model = await connected(dut, seed=31)
    for i in range(3):
        model.memory[0x20000000 + 4 * i] = 0x60 + i
    model.memory[0x20000400] = 0x99
    await dap.transfer((AP_TAR_W, 0x20000100))
    records, transfers = [], []
    task = watch_line(dut, records, transfers)
    await configure(dut, [(0x20000000, 3), (0x20000400, 1)], period=1500)
    dut.smp_run.value = 1
    for _ in range(6):
        await rounds(dut, int(dut.smp_rounds.value) + 1)
        model.wait_next = 2                                   # 次の AP の転送 2 回は WAIT
    dut.smp_run.value = 0
    await ClockCycles(dut.clk, 4000)
    task.cancel()
    assert 17 in [k for k, _ in records]                      # 回収の RDBUFF が返した値の窓の後端
    n = check_windows(records, transfers, lambda req: req in (0b1111, 0b1110))   # SWD: AP の読み・RDBUFF が値を返す
    assert n >= 16, n
    # 同じ番地を続けて読む（値は次の DRW で返る）間に WAIT: 値を返した DRW の WAIT の回数が語に入る
    records.clear()
    transfers.clear()
    task = watch_line(dut, records, transfers)
    await configure(dut, [(0x20000000, 1)], period=1)
    dut.smp_run.value = 1
    for _ in range(8):
        await rounds(dut, int(dut.smp_rounds.value) + 3)
        model.wait_next = 2
    dut.smp_run.value = 0
    await ClockCycles(dut.clk, 4000)
    task.cancel()
    assert check_windows(records, transfers, lambda req: req in (0b1111, 0b1110)) >= 16
    assert any(w >> 115 & 0x1F == 2 for k, w in records if k == 7)


@cocotb.test()
async def jtag_sample_times_and_windows(dut):
    """R6（JTAG）: 語の時刻は読みに行った DRW のスキャンの Update-DR の点。値を返すのは次のスキャン（CTRL/STAT・次の DRW・
    次のか所の TAR・回収）で、その点が窓の後端（DRW でなければ種類 17）。"""
    dap, dp = await jtag_setup(dut)
    await dap.mem_setup()
    for i in range(2):
        dp.regs.memory[0x20000000 + 4 * i] = 0x70 + i
    dp.regs.memory[0x20000100] = 0x77
    records, transfers = [], []
    task = watch_line(dut, records, transfers)
    await configure(dut, [(0x20000000, 2), (0x20000100, 1)], period=5000)
    dut.smp_run.value = 1
    await rounds(dut, 3)
    dut.smp_run.value = 0
    await ClockCycles(dut.clk, 5000)
    task.cancel()
    n = check_windows(records, transfers, lambda req: True)  # JTAG: どのスキャンも直前の結果を取り込む
    assert n >= 8, n
    assert [r for r in records if r[0] == 7 and r[1] >> 48 & 0xFFFFFFFF == 0x20000100]


async def jtag_stream(dut, check_words, rounds_n=64, fault=None, slots=((0x20000000, 1),)):
    """JTAG で同じ番地を続けて読む定常（period = 1、できるだけ速く）。(語ごとの TCK, 語ごとの DR スキャン, 件, dp)。"""
    dap, dp = await jtag_setup(dut)
    await dap.mem_setup()
    for a, n in slots:
        for i in range(n):
            dp.regs.memory[a + 4 * i] = 0x1000 + a + i
    await dap.transfer((AP_TAR_W, 0x20000100))
    if fault is not None:
        dp.regs.fault_addresses.add(fault)
    dut.smp_check_words.value = check_words
    records, transfers = [], []
    task = watch_line(dut, records, transfers)
    await configure(dut, list(slots), period=1)
    dut.smp_run.value = 1
    await rounds(dut, 4)
    tck = [0]

    async def count_tck():
        while True:
            await RisingEdge(dut.swclk)
            tck[0] += 1
    counter = cocotb.start_soon(count_tck())
    scans0, rounds0 = len(dp.scans), int(dut.smp_rounds.value)
    await rounds(dut, rounds0 + rounds_n)
    counter.cancel()
    scans, done = len(dp.scans) - scans0, int(dut.smp_rounds.value) - rounds0
    words = done * sum(n for _, n in slots)
    dut.smp_run.value = 0
    await ClockCycles(dut.clk, 20000)
    task.cancel()
    dut.smp_check_words.value = 1
    return tck[0] / words, scans / words, records, dp


@cocotb.test()
async def jtag_k_word_check_reads_one_scan_per_word(dut):
    """L1（bandwidth-observation 2.3）: JTAG で 1 か所 1 語を続けて読む定常の 1 語あたり。チェーンは JTAG-DP + BYPASS
    （DR 41・IR 15 TCK）。K = 1（語ごと。停止の見張りの形・直す前と同じ）は DRW + CTRL/STAT の 2 スキャン + IR 2 回 = 112 TCK
    （直す前の RTL で測った値と同じ。仕様書の L1 の実装の節）。K = 16 は DRW 16 回 + CTRL/STAT 1 回 + IR 2 回 = 16 語で
    17 × 41 + 2 × 15 = 727 TCK（1 語 45.4 TCK）。値は全部、番地の順に出る。"""
    dr, ir = 41, 15
    tck1, scans1, rec1, _ = await jtag_stream(dut, 1)
    dut._log.info("JTAG K=1: %.2f TCK/word, %.3f DR scans/word", tck1, scans1)
    assert abs(tck1 - (2 * dr + 2 * ir)) < 1.5 and abs(scans1 - 2) < 0.05, (tck1, scans1)
    tck16, scans16, rec16, _ = await jtag_stream(dut, 16, rounds_n=160)
    dut._log.info("JTAG K=16: %.2f TCK/word, %.3f DR scans/word", tck16, scans16)
    assert abs(tck16 - (17 * dr + 2 * ir) / 16) < 1.5 and abs(scans16 - 17 / 16) < 0.05, (tck16, scans16)
    for rec in (rec1, rec16):
        values = [w >> 80 & 0xFFFFFFFF for k, w in rec if k == 7]
        assert values and all(v == 0x1000 + 0x20000000 for v in values)
        assert not [k for k, _ in rec if k == 18]


@cocotb.test()
async def jtag_k_word_check_turns_an_error_into_an_interval_without_values(dut):
    """L1: K = 3 で 2 か所（1 か所はバスの誤りの番地）。確認で STICKYERR が立った区間の値は出さず、誤りの区間（種類 18:
    始め = 区間の最初の読みの点、語数、STICKYERR、見つけた転送の点）にする。出した語と区間の語の和は、受理された DRW の読みの
    数と等しい（黙って捨てない）。正しい値だけが出る。"""
    dap, dp = await jtag_setup(dut)
    await dap.mem_setup()
    dp.regs.memory[0x20000000] = 0x5A
    dp.regs.fault_addresses.add(0x20000400)
    await dap.transfer((AP_TAR_W, 0x20000100))
    dut.smp_check_words.value = 3
    records, transfers = [], []
    task = watch_line(dut, records, transfers)
    reads0 = len([s for s in dp.scans if s[:3] == (dp.IR_APACC, 1, 0xC)])
    await configure(dut, [(0x20000000, 1), (0x20000400, 1)], period=3000)
    dut.smp_run.value = 1
    await rounds(dut, 8)
    dp.regs.fault_addresses.clear()                          # 誤りが無くなれば両方の値が出る
    await rounds(dut, int(dut.smp_rounds.value) + 4)
    dut.smp_run.value = 0
    await ClockCycles(dut.clk, 10000)
    task.cancel()
    dut.smp_check_words.value = 1
    gaps = [w for k, w in records if k == 18]
    values = [(w >> 48 & 0xFFFFFFFF, w >> 80 & 0xFFFFFFFF) for k, w in records if k == 7]
    assert gaps and all(w >> 115 & 1 for w in gaps)            # CTRL/STAT の STICKYERR で見つけた
    assert all(v == (0x20000000, 0x5A) or v[0] == 0x20000400 for v in values)
    assert (0x20000400, dp.regs.memory.get(0x20000400, 0)) in values                       # 誤りが消えた後
    reads = len([s for s in dp.scans if s[:3] == (dp.IR_APACC, 1, 0xC)]) - reads0
    gap_words = sum(w >> 103 & 0x1FF for w in gaps)
    assert len(values) + gap_words == reads, (len(values), gap_words, reads)
    points = [t for t, _, _ in transfers]
    for w in gaps:                                             # 区間の始めと見つけた点は線上の点
        assert w & MASK48 in points and w >> 48 & MASK48 in points and (w & MASK48) <= (w >> 48 & MASK48)
    assert int(dut.smp_errors.value) == len(gaps)
