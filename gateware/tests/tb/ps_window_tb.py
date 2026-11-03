"""窓（zynq_frame_link）+ la_ps_port: PS が DDR を直接読む流れの「位置の約束」（tests/rtl/ps_window_harness.vhd）。
仕様: docs/superpowers/specs/2026-11-03-ps-stream-position-design.md。番地とビットは fprobe_gw/ps_window.py が正。

PS（中継）の側は AXI4-Lite で読み書きする。la_deep_buffer の側（流れの番号・公開された組・読み出しの要求）はこの試験が入れる。"""

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, RisingEdge

from fprobe_gw import ps_window
from tests.tb.axil_master import AxiLiteMaster

R = {r.name: r.addr for r in ps_window.REGS}
B = ps_window.STATE_BITS
EPOCH_MASK = (1 << ps_window.EPOCH_BITS) - 1


async def setup(dut):
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    dut.rst.value = 1
    for name in ("epoch", "pub_epoch", "pub_active", "pub_writing", "pub_overrun", "pub_la_written", "pub_la_wptr",
                 "pub_ev_wpos", "pub_ev_start", "rd_epoch", "rd_region", "rd_first", "rd_count"):
        getattr(dut, name).value = 0
    axi = AxiLiteMaster(dut, dut.clk)
    await ClockCycles(dut.clk, 3)
    dut.rst.value = 0
    await ClockCycles(dut.clk, 2)
    return axi


async def new_stream(dut, epoch):
    dut.epoch.value = epoch
    await ClockCycles(dut.clk, 3)


def publish(dut, *, epoch, active=1, writing=1, overrun=0, written=0, wptr=0, ev_wpos=0, ev_start=0):
    dut.pub_epoch.value, dut.pub_active.value, dut.pub_writing.value = epoch, active, writing
    dut.pub_overrun.value, dut.pub_la_written.value, dut.pub_la_wptr.value = overrun, written, wptr
    dut.pub_ev_wpos.value, dut.pub_ev_start.value = ev_wpos, ev_start


@cocotb.test()
async def positions_belong_to_the_stream_number_and_go_back_to_zero_when_it_changes(dut):
    axi = await setup(dut)
    await new_stream(dut, 1)
    await axi.write(R["POS_TAG"], 1)
    await axi.write(R["LA_CONSUMED"], 500)
    await axi.write(R["EV_CONSUMED"], 40)
    await axi.write(R["STREAM_BEATS"], 600)
    assert [await axi.read(R[n]) for n in ("LA_CONSUMED", "EV_CONSUMED", "STREAM_BEATS")] == [500, 40, 600]
    assert (int(dut.ret_epoch.value), int(dut.la_consumed.value), int(dut.ev_consumed.value),
            int(dut.out_beats.value)) == (1, 500, 40, 600)
    await new_stream(dut, 2)                                                 # 次の流れ: 位置は 0、番号は 2
    assert (int(dut.ret_epoch.value), int(dut.la_consumed.value), int(dut.ev_consumed.value),
            int(dut.out_beats.value)) == (2, 0, 0, 0)
    assert [await axi.read(R[n]) for n in ("LA_CONSUMED", "EV_CONSUMED", "STREAM_BEATS")] == [0, 0, 0]


@cocotb.test()
async def a_position_written_with_the_previous_stream_number_is_dropped_and_counted(dut):
    # 中継が新しい流れに気づく前に書いた古い位置は、新しい流れの位置にしない（C1）。前の窓は書きを全部受け、la_deep_buffer が
    # sys 側で新しい世代を付けたので、開始の後に届いた古い位置が新しい流れの位置として使われた
    axi = await setup(dut)
    await new_stream(dut, 7)
    await axi.write(R["POS_TAG"], 7)
    await axi.write(R["LA_CONSUMED"], 900)
    await new_stream(dut, 8)                                                 # ホストが次の流れを始めた
    await axi.write(R["LA_CONSUMED"], 950)                                   # 中継はまだ 7 のつもりで書く
    await axi.write(R["EV_CONSUMED"], 3)
    await axi.write(R["STREAM_BEATS"], 960)
    assert [await axi.read(R[n]) for n in ("LA_CONSUMED", "EV_CONSUMED", "STREAM_BEATS")] == [0, 0, 0]
    assert int(dut.la_consumed.value) == 0 and int(dut.ret_epoch.value) == 8
    assert await axi.read(R["STALE_WRITES"]) == 3
    await axi.write(R["POS_TAG"], 8)                                         # 付き直した
    await axi.write(R["LA_CONSUMED"], 12)
    assert await axi.read(R["LA_CONSUMED"]) == 12 and await axi.read(R["STALE_WRITES"]) == 3
    assert await axi.read(R["POS_TAG"]) == 8


@cocotb.test()
async def a_write_in_the_cycle_the_stream_number_changes_is_dropped(dut):
    # 番号が変わった周期の位置の書きは、前の番号のもの（0 戻しが勝つ）
    axi = await setup(dut)
    await new_stream(dut, 3)
    await axi.write(R["POS_TAG"], 3)
    for delay in range(6):                                                   # 書きの受け付けと番号の変化の周期をずらす
        task = cocotb.start_soon(axi.write(R["LA_CONSUMED"], 100 + delay))
        await ClockCycles(dut.clk, delay)
        await RisingEdge(dut.clk)
        dut.epoch.value = 4 + delay
        await task
        await ClockCycles(dut.clk, 3)
        value = await axi.read(R["LA_CONSUMED"])
        assert value == 0, (delay, value)                                     # 書きが先なら 0 戻しで消え、後なら捨てられる
        assert int(dut.ret_epoch.value) == 4 + delay
        await axi.write(R["POS_TAG"], 4 + delay)


@cocotb.test()
async def reading_the_stream_epoch_takes_one_point_in_time_of_the_published_group(dut):
    # STREAM_EPOCH の読みは公開された組を写す。以後の LA_WRITTEN・LA_WPTR・EV_WPOS・EV_START は、その後に PL の値が変わっても
    # 写しの値（別々の時点の値が混ざらない。C10）。値の CURRENT は、公開された番号が位置のレジスタの番号と同じとき
    axi = await setup(dut)
    await new_stream(dut, 5)
    publish(dut, epoch=4, active=1, writing=0, overrun=1, written=10, wptr=10, ev_wpos=3, ev_start=1)
    state = await axi.read(R["STREAM_EPOCH"])
    assert state & EPOCH_MASK == 4
    assert (state >> B["ACTIVE"]) & 1 and not (state >> B["WRITING"]) & 1 and (state >> B["OVERRUN"]) & 1
    assert not (state >> B["CURRENT"]) & 1                                   # 位置のレジスタは流れ 5 のもの
    publish(dut, epoch=5, active=1, writing=1, written=1234, wptr=274, ev_wpos=9, ev_start=6)
    assert await axi.read(R["LA_WRITTEN"]) == 10                              # まだ前の写し
    state = await axi.read(R["STREAM_EPOCH"])
    assert state & EPOCH_MASK == 5 and (state >> B["CURRENT"]) & 1 and (state >> B["WRITING"]) & 1
    publish(dut, epoch=5, written=2000, wptr=80, ev_wpos=11, ev_start=6)    # 写した後に PL が進む
    assert [await axi.read(R[n]) for n in ("LA_WRITTEN", "LA_WPTR", "EV_WPOS", "EV_START")] == [1234, 274, 9, 6]
    state = await axi.read(R["STREAM_EPOCH"])
    assert [await axi.read(R[n]) for n in ("LA_WRITTEN", "LA_WPTR", "EV_WPOS", "EV_START")] == [2000, 80, 11, 6]


@cocotb.test()
async def reading_rd_epoch_takes_the_whole_readout_request(dut):
    axi = await setup(dut)
    dut.rd_epoch.value, dut.rd_region.value, dut.rd_first.value, dut.rd_count.value = 5, 1, 1234, 777
    assert await axi.read(R["RD_EPOCH"]) == 5
    dut.rd_epoch.value, dut.rd_region.value, dut.rd_first.value, dut.rd_count.value = 6, 0, 1, 2
    assert [await axi.read(R[n]) for n in ("RD_REGION", "RD_FIRST", "RD_COUNT")] == [1, 1234, 777]
    assert await axi.read(R["RD_EPOCH"]) == 6
    assert [await axi.read(R[n]) for n in ("RD_REGION", "RD_FIRST", "RD_COUNT")] == [0, 1, 2]
    await axi.write(R["RD_FIRST"], 99)                                        # 読み出し専用への書きは無視
    assert await axi.read(R["RD_FIRST"]) == 1
    await axi.write(R["RD_ACK"], 6)
    assert await axi.read(R["RD_ACK"]) == 6 and int(dut.rd_ack.value) == 6
    await new_stream(dut, 9)                                                 # 流れの開始では、返事は消えない
    assert await axi.read(R["RD_ACK"]) == 6
