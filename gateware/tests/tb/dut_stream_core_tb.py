"""DUT のコア（サンプル生成 + FIFO + 不具合の仕込み）。レジスタの語番地は dut_stream_core.vhd の表と同じ。"""
import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, FallingEdge, RisingEdge, Timer

CTRL, PERIOD, DATA, LEVEL, OVERFLOWS, GENERATED, FAULT, ID = range(8)


async def start(dut):
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    dut.rst.value = 1
    dut.reg_wr_en.value = 0
    dut.reg_rd_en.value = 0
    dut.reg_addr.value = 0
    dut.reg_wdata.value = 0
    await ClockCycles(dut.clk, 3)
    dut.rst.value = 0
    await ClockCycles(dut.clk, 2)


async def write(dut, addr, value):
    await FallingEdge(dut.clk)
    dut.reg_addr.value = addr
    dut.reg_wdata.value = value
    dut.reg_wr_en.value = 1
    await RisingEdge(dut.clk)
    await FallingEdge(dut.clk)
    dut.reg_wr_en.value = 0


async def read(dut, addr):
    """reg_rdata は reg_addr に対する組合せ。reg_rd_en が立つ周期の値が読んだ値で、DATA はその周期の終わりに取り出される。"""
    await FallingEdge(dut.clk)
    dut.reg_addr.value = addr
    dut.reg_rd_en.value = 1
    await Timer(1, unit="ns")
    value = int(dut.reg_rdata.value)
    await RisingEdge(dut.clk)
    await FallingEdge(dut.clk)
    dut.reg_rd_en.value = 0
    return value


@cocotb.test()
async def test_id_register(dut):
    await start(dut)
    assert await read(dut, ID) == 0x44555401


@cocotb.test()
async def test_generates_sequence(dut):
    await start(dut)
    await write(dut, PERIOD, 10)
    await write(dut, CTRL, 1)
    await ClockCycles(dut.clk, 100)
    await write(dut, CTRL, 0)
    level = await read(dut, LEVEL)
    assert 9 <= level <= 11, level
    assert await read(dut, GENERATED) == level
    assert [await read(dut, DATA) for _ in range(5)] == [0, 1, 2, 3, 4]


@cocotb.test()
async def test_empty_read_returns_all_ones(dut):
    await start(dut)
    assert await read(dut, DATA) == 0xFFFFFFFF


@cocotb.test()
async def test_overflow_counts_and_does_not_clear_on_read(dut):
    await start(dut)
    await write(dut, PERIOD, 2)
    await write(dut, CTRL, 1)
    await ClockCycles(dut.clk, 700)               # FIFO_DEPTH = 256 を読まずに放置（約 350 個生成）
    pulses = 0
    for _ in range(20):
        await RisingEdge(dut.clk)
        pulses += int(dut.dbg.value) >> 2 & 1
    first = await read(dut, OVERFLOWS)
    assert first > 0 and pulses > 0
    assert await read(dut, OVERFLOWS) >= first    # 読んでもクリアされない（増えることはある）
    await write(dut, CTRL, 0)
    stopped = await read(dut, OVERFLOWS)
    assert await read(dut, OVERFLOWS) == stopped == await read(dut, OVERFLOWS)


@cocotb.test()
async def test_fault_drops_every_nth(dut):
    await start(dut)
    await write(dut, FAULT, (5 << 8) | 1)         # 5 個に 1 個を捨てる
    await write(dut, PERIOD, 10)
    await write(dut, CTRL, 1)
    seen = []
    dropped_pulses = 0
    for _ in range(30):
        await ClockCycles(dut.clk, 10)
        dropped_pulses += int(dut.dbg.value) >> 4 & 1
        v = await read(dut, DATA)
        if v != 0xFFFFFFFF:
            seen.append(v)
    assert seen[:4] == [0, 1, 2, 3], seen          # 5 番目 (通し番号 4) が捨てられる
    assert seen[4] == 5, seen
    assert all(b - a in (1, 2) for a, b in zip(seen, seen[1:]))
    assert await read(dut, GENERATED) > len(seen)  # 生成した数は捨てた分を含む


@cocotb.test()
async def test_all_outputs_driven(dut):
    await start(dut)
    for _ in range(10):
        await RisingEdge(dut.clk)
        assert dut.dbg.value.is_resolvable and dut.reg_rdata.value.is_resolvable


@cocotb.test()
async def test_event_pulses_are_stretched_to_four_clocks(dut):
    # 1 クロック（10 ns）の脈はロジアナ（約 100.29 MS/s）の標本の間に落ちて約 1 割が見えなかった（実機 2026-10-25）。4 クロックに伸ばす
    await start(dut)
    await write(dut, PERIOD, 50)
    await write(dut, FAULT, (3 << 8) | 1)
    await write(dut, CTRL, 1)
    widths = {0: [], 4: []}                           # 生成の拍 / 不具合で捨てた拍
    run = {0: 0, 4: 0}
    for _ in range(400):
        await RisingEdge(dut.clk)
        value = int(dut.dbg.value)
        for bit in run:
            if value >> bit & 1:
                run[bit] += 1
            elif run[bit]:
                widths[bit].append(run[bit])
                run[bit] = 0
    assert len(widths[0]) >= 5 and len(widths[4]) >= 2
    assert all(w == 4 for w in widths[0] + widths[4]), widths


@cocotb.test()
async def test_fault_rewritten_while_ticking_takes_effect_within_n_ticks(dut):
    # レビューの指摘: FAULT の書き込みが生成の拍と同じクロックに重なるとカウンタのリセットが負けた。PERIOD = 1（毎クロック生成）で必ず重なる
    await start(dut)
    await write(dut, PERIOD, 1)
    await write(dut, FAULT, (1000 << 8) | 1)       # N = 1000 で走らせ、カウンタを進める
    await write(dut, CTRL, 1)
    await ClockCycles(dut.clk, 500)
    await write(dut, FAULT, (3 << 8) | 1)          # N = 3 に書き換え（生成中）
    high = 0
    for _ in range(40):
        await RisingEdge(dut.clk)
        high += int(dut.dbg.value) >> 4 & 1           # 毎クロック生成 + 3 個に 1 個 + 4 クロックの伸ばしで、ほぼ High のまま
    assert high >= 20, high                        # 旧実装（カウンタが新しい N に届かない）は 0
