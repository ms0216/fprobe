"""書いたイベントの数が 2**32 を超えて数えられること（64 ビット。以前は 32 ビットで飽和した）。
COUNT_START_BELOW_4G: 記録の開始ごとに 2**32 - その数 から数える（試験用の generic。実際の回路は 0 から）。
リングの書き位置は 0 から数えるので、読みの番地（総数 % 件数）と合わせるため、手前の数はリングの件数の倍数にする
（実機では 2**32 が件数の倍数なので、いつでも合っている）。"""
import cocotb
from cocotb.triggers import ReadOnly, RisingEdge

from tests.tb import event_ring_tb as ring
from tests.tb import mem_ring_tb as mem

RING_START = (1 << 32) - 16                  # event_ring（16 件）
MEM_START = (1 << 32) - 64                   # la_mem_writer のイベントのリング（64 拍）


@cocotb.test()
async def event_ring_counts_past_2_pow_32(dut):
    await ring.setup(dut)
    dut.ev_enable.value = 1
    await RisingEdge(dut.clk)
    values = list(range(200, 200 + ring.DEPTH + 5))              # リング 1 周 + 5 件。数は 2**32 の境をまたぐ
    await ring.push(dut, values)
    await ring.ClockCycles(dut.clk, 2)
    written = int(dut.ev_written.value)
    assert written == RING_START + len(values) and written > 1 << 32
    # 読みの番地は件数（2 のべき）の剰余なので、64 ビットの総数でも最新の DEPTH 件が読める
    assert await ring.read_events(dut, (written - ring.DEPTH) % ring.DEPTH, ring.DEPTH) == values[-ring.DEPTH:]
    # 開始し直せば、また開始値から
    dut.ev_enable.value = 0
    await ring.ClockCycles(dut.clk, 2)
    dut.ev_enable.value = 1
    await RisingEdge(dut.clk)
    await ring.push(dut, [7])
    await ring.ClockCycles(dut.clk, 2)
    assert int(dut.ev_written.value) == RING_START + 1


@cocotb.test()
async def mem_writer_counts_past_2_pow_32_instead_of_saturating(dut):
    await mem.setup(dut)
    dut.ev_enable.value = 1
    await RisingEdge(dut.clk)
    n = 70                                                       # リング（64 拍）を一周する
    await mem.write_beats(dut, [(0xE000 + i, 1, False) for i in range(n)])
    await ReadOnly()
    written = int(dut.ev_written.value)
    assert written == MEM_START + n and written > 1 << 32
    await RisingEdge(dut.clk)
    # 最新の 64 件（6..69）は (総数 % 64) 番目から
    events = await mem.read_range(dut, mem.LA_BEATS, mem.EV_BEATS, written % mem.EV_BEATS, mem.EV_BEATS)
    assert events == [0xE000 + i for i in range(n - mem.EV_BEATS, n)]
