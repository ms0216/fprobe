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
    for _ in range(timeout):
        await RisingEdge(dut.clk)
        await ReadOnly()
        if int(dut.smp_rounds.value) >= n:
            await RisingEdge(dut.clk)
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
async def idle_rounds_do_not_restore_and_take_three_transfers(dut):
    """ホストが線を使っていない間は戻さない（restores = 0、held）。1 か所 1 語の定常は DRW・RDBUFF・CTRL/STAT の 3 転送
    （自動増加なしの CSW で TAR を書き直さない。従来は戻しを含めて 9）。"""
    dap, model = await connected(dut, seed=7)
    model.memory[0x20000000] = 42
    await dap.transfer((AP_TAR_W, 0x20000100))                          # 控え: CSW（増加）・TAR が分かる
    got = []
    task = collect(dut, got)
    await configure(dut, [(0x20000000, 1)], period=1)                    # できるだけ速く
    dut.smp_run.value = 1
    mark, n, cycles = await steady_rounds(dut, model, 20)
    assert [count(model, mark, k) for k in (SELECT_W, CSW_W, TAR_W)] == [0, 0, 0]
    assert all(abs(count(model, mark, k) - n) <= 1 for k in (DRW_R, RDBUFF_R, CTRL_R))
    assert len(model.transactions) - mark <= 3 * n + 3
    assert cycles / n < 3 * TRANSFER_CYCLES + 60, cycles / n              # 従来は 9 転送
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
        await ClockCycles(dut.clk, 3000)
        task.cancel()
        assert [(a, d) for a, d, _ in got[:3 * len(expected)]] == expected * 3, (trial, table)
        assert all(k == OK for _, _, k in got) and int(dut.smp_errors.value) == 0
