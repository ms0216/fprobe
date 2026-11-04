"""la_stream_sched（ストリーミングの読み出しの段取り）のテストベンチ。ADDR_BITS = 10（ロジアナのリング 960 拍、イベントの
リング 64 拍）、STREAM_EVENTS = true。

書き込み器と読み出し器は模型（書き込み器: 総数・イベントの書き位置を進める。読み出し器: rd_start の次の周期から拍数ぶん busy）。
確かめること:
- 見出しとかたまりが 1 対 1 で、見出しの種類・拍数・通し番号がかたまりと一致し、ロジアナの拍・イベントがそれぞれ切れ目なく
  順に（リングの位置で）読まれ、止めた後に全部を出し切って active を下ろす。out_total = かたまりの拍と見出しの和。
- 読み出し器が空かないと、未読がリング − 256 を超えたところで書き込みを止めて ovf。
- gaps: 未読がリング − 240 を超えたら hold、480 拍減ったら下ろす。
- ps_read: 読み出し器に読ませず、この流れの番号の ret_la・ret_ev で未読が減り、PS が読み終えたら active を下ろす。
"""

import random

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, ReadOnly, RisingEdge

LA_SIZE = 960
EV_SIZE = 64
SEG_LA, SEG_EV = 1, 2
MAGIC = 0x0047455342525046


class Bench:
    def __init__(self, dut, rng, reader_speed=1.0):
        self.dut, self.rng = dut, rng
        self.reader_speed = reader_speed                  # 読み出し器が拍を出す割合（0 = 止まる）
        self.headers, self.chunks = [], []
        self.busy_left = 0
        self.full_stops = 0

    async def run(self):
        dut = self.dut
        while True:
            await RisingEdge(dut.clk)
            await ReadOnly()
            if int(dut.hdr_valid.value) and int(dut.hdr_ready.value):
                v = int(dut.hdr_data.value)
                assert v >> 64 == MAGIC
                self.headers.append((v & 0xFF, v >> 16 & 0xFFFF, v >> 32 & 0xFFFFFFFF))
            if int(dut.full_stop.value):
                self.full_stops += 1
            start = int(dut.rd_start.value)
            if start:
                assert self.busy_left == 0
                self.chunks.append((int(dut.rd_region.value), int(dut.rd_first.value), int(dut.rd_count.value)))
            busy = self.busy_left > 0
            if start:
                self.busy_left = self.chunks[-1][2] + 1
            elif busy and self.rng.random() < self.reader_speed:
                self.busy_left -= 1
            await cocotb.triggers.Timer(1, unit="ns")
            dut.rd_busy.value = int(self.busy_left > 0)


async def setup(dut):
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    for s in (dut.start, dut.epoch, dut.stream, dut.gaps, dut.ps_read, dut.ev_first, dut.w_total, dut.w_running,
              dut.ev_wpos, dut.ev_safe, dut.ret_epoch, dut.ret_la, dut.ret_ev, dut.rd_busy):
        s.value = 0
    dut.hdr_ready.value = 1
    dut.rst.value = 1
    await ClockCycles(dut.clk, 3)
    dut.rst.value = 0
    await RisingEdge(dut.clk)


async def start_stream(dut, epoch, *, ev_first=0, gaps=0, ps_read=0):
    dut.start.value = 1
    dut.epoch.value = epoch
    dut.stream.value = 1
    dut.gaps.value = gaps
    dut.ps_read.value = ps_read
    dut.ev_first.value = ev_first
    dut.w_running.value = 1
    dut.w_total.value = 0
    dut.ev_wpos.value = ev_first
    dut.ev_safe.value = ev_first
    await RisingEdge(dut.clk)
    dut.start.value = 0
    dut.stream.value = dut.gaps.value = dut.ps_read.value = 0     # 設定は start の周期の値


@cocotb.test()
async def chunks_cover_everything_in_order_and_the_stream_ends(dut):
    await setup(dut)
    rng = random.Random(1)
    bench = Bench(dut, rng)
    cocotb.start_soon(bench.run())
    ev_first = 50
    await start_stream(dut, 1, ev_first=ev_first)
    total, ev_pos = 0, ev_first
    for n in range(30000):
        if rng.random() < 0.3:
            total += 1
        if rng.random() < 0.01:
            ev_pos = (ev_pos + 1) % EV_SIZE
        dut.w_total.value = total
        dut.ev_wpos.value = ev_pos
        dut.ev_safe.value = ev_pos
        await RisingEdge(dut.clk)
    dut.w_running.value = 0
    for _ in range(20000):
        await RisingEdge(dut.clk)
        await ReadOnly()
        if not int(dut.active.value):
            break
    assert not int(dut.active.value), "止めた後に流れが終わらない"
    assert bench.full_stops == 0 and not int(dut.ovf.value)
    assert len(bench.headers) == len(bench.chunks) > 20
    la_next, ev_next, out = 0, ev_first, 0
    for (kind, count, seq), (region, first, ccount) in zip(bench.headers, bench.chunks):
        assert count == ccount and 0 < count <= 256
        out += count + 1
        if kind == SEG_LA:
            assert region == 0 and seq == la_next and first == la_next % LA_SIZE
            la_next += count
        else:
            assert kind == SEG_EV and region == 1 and seq == ev_next and first == ev_next
            ev_next = (ev_next + count) % EV_SIZE
    assert la_next == total, (la_next, total)
    assert ev_next == ev_pos
    assert int(dut.out_total.value) == out
    assert int(dut.peak.value) >= 1


@cocotb.test()
async def a_stuck_reader_stops_the_writer_with_overflow(dut):
    await setup(dut)
    bench = Bench(dut, random.Random(2), reader_speed=0.0)
    cocotb.start_soon(bench.run())
    await start_stream(dut, 2)
    for total in range(1, LA_SIZE):
        dut.w_total.value = total
        await RisingEdge(dut.clk)
        await ReadOnly()
        if bench.full_stops:
            break
        await cocotb.triggers.Timer(1, unit="ns")
    assert bench.full_stops == 1 and int(dut.ovf.value)
    read = sum(c for _, _, c in bench.chunks)                       # 止まる前に読ませた（読み出し器は出さないまま）
    assert len(bench.chunks) == 1
    assert LA_SIZE - 256 < total - read <= LA_SIZE - 256 + 4, (total, read)   # 判定は未読の数周期の遅れの分だけ後
    assert int(dut.stream_epoch.value) == 2


@cocotb.test()
async def gaps_raise_and_lower_hold(dut):
    await setup(dut)
    bench = Bench(dut, random.Random(3), reader_speed=0.0)
    cocotb.start_soon(bench.run())
    await start_stream(dut, 3, gaps=1, ps_read=1)                   # PS が読む流れで、読み終えた位置を手で動かす
    dut.ret_epoch.value = 3
    dut.w_total.value = 720
    await ClockCycles(dut.clk, 6)
    await ReadOnly()
    assert not int(dut.hold.value), "未読 720（リング − 240）で立てた"
    await RisingEdge(dut.clk)
    dut.w_total.value = 721
    await ClockCycles(dut.clk, 6)
    await ReadOnly()
    assert int(dut.hold.value)
    await RisingEdge(dut.clk)
    dut.ret_la.value = 240                                          # 未読 481
    await ClockCycles(dut.clk, 6)
    await ReadOnly()
    assert int(dut.unread.value) == 481
    assert int(dut.hold.value), "下ろす境（480）より上で下ろした"
    await RisingEdge(dut.clk)
    dut.ret_la.value = 241
    await ClockCycles(dut.clk, 6)
    await ReadOnly()
    assert int(dut.unread.value) == 480
    assert not int(dut.hold.value)
    assert bench.chunks == [], "PS が読む流れで読み出し器に読ませた"


@cocotb.test()
async def ps_read_follows_the_returned_positions_of_this_stream_only(dut):
    await setup(dut)
    bench = Bench(dut, random.Random(4))
    cocotb.start_soon(bench.run())
    await start_stream(dut, 7, ev_first=10, ps_read=1)
    dut.w_total.value = 100
    dut.ev_wpos.value = 30
    dut.ev_safe.value = 30
    dut.ret_epoch.value = 6                                         # 前の流れの値は使わない
    dut.ret_la.value = 100
    dut.ret_ev.value = 20
    await ClockCycles(dut.clk, 6)
    await ReadOnly()
    assert int(dut.unread.value) == 100
    await RisingEdge(dut.clk)
    dut.ret_epoch.value = 7
    dut.ret_ev.value = 0
    dut.ret_la.value = 150                                          # 書いた総数より先の値は使わない
    await ClockCycles(dut.clk, 6)
    await ReadOnly()
    assert int(dut.unread.value) == 100
    await RisingEdge(dut.clk)
    dut.ret_la.value = 60
    dut.w_running.value = 0
    await ClockCycles(dut.clk, 6)
    await ReadOnly()
    assert int(dut.unread.value) == 40 and int(dut.active.value)
    # 止めた後: 公開するイベントの書き位置は、止めた時点の書き位置（ev_lim = 30）で頭打ち
    await RisingEdge(dut.clk)
    dut.ev_wpos.value = 40
    dut.ev_safe.value = 40
    await ClockCycles(dut.clk, 3)
    await ReadOnly()
    assert int(dut.ev_pub.value) == 30
    await RisingEdge(dut.clk)
    dut.ret_la.value = 100
    await ClockCycles(dut.clk, 6)
    await ReadOnly()
    assert int(dut.active.value), "イベントを読み終える前に終わった"
    await RisingEdge(dut.clk)
    dut.ret_ev.value = 20                                           # ev_start 10 + 20 = 30 = ev_lim
    await ClockCycles(dut.clk, 6)
    await ReadOnly()
    assert not int(dut.active.value)
    assert bench.chunks == [] and bench.headers == []
