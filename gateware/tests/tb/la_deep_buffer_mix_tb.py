"""la_deep_buffer のストリームにイベントも流す構成（STREAM_EVENTS = true、R1）のテストベンチ。
ADDR_BITS = 10（ロジアナのリング 960 拍、イベントのリング 64 件）。
仕様: docs/superpowers/specs/2026-10-19-r1-unified-stream-design.md

流れはホストの解き方（fprobe.la.split_segments）で分け、ロジアナの拍はカウンタの連続、イベントは送った順と比べる。"""

import cocotb
from cocotb.triggers import ClockCycles, FallingEdge, RisingEdge

from fprobe import generated as g
from fprobe.la import decode_beats, split_segments
from tests.tb.la_deep_buffer_tb import EV_CAP, setup, stream_until, wait_for


class EventSource:
    """period 周期ごとに 1 件のイベント（値 = base + 通し番号）を送る。"""

    def __init__(self, dut, period, base=0xE0000):
        self.dut, self.period, self.base = dut, period, base
        self.sent = []
        self.task = cocotb.start_soon(self._run())

    async def _run(self):
        dut = self.dut
        while True:
            await ClockCycles(dut.clk, self.period)
            value = self.base + len(self.sent)
            dut.ev_valid.value, dut.ev_data.value = 1, value
            self.sent.append(value)
            await RisingEdge(dut.clk)
            dut.ev_valid.value = 0

    def stop(self):
        self.task.cancel()
        self.dut.ev_valid.value = 0


def demux(data):
    """流れ → (ロジアナの拍のバイト列, イベントの値の列, かたまりの列)。通し番号の連続も確かめる。"""
    segments = split_segments(data)
    la, events, la_beats, ev_pos = bytearray(), [], 0, None
    for seg in segments:
        n = len(seg.data) // 16
        assert 1 <= n <= g.STREAM_SEG_MAX_BEATS
        if seg.kind == g.STREAM_SEG_LA:
            assert seg.seq == la_beats, (seg.seq, la_beats)
            la += seg.data
            la_beats += n
        else:
            assert seg.kind == g.STREAM_SEG_EVENTS
            assert ev_pos is None or seg.seq == ev_pos, (seg.seq, ev_pos)
            events += [int.from_bytes(seg.data[i:i + 16], "little") for i in range(0, len(seg.data), 16)]
            ev_pos = (seg.seq + n) % EV_CAP
    return bytes(la), events, segments


def counter_gaps(samples, step):
    # 先頭の 1 サンプルは開始直後の同期化段の初期値なので見ない
    return [(i, a, b) for i, (a, b) in enumerate(zip(samples[1:], samples[2:])) if (b - a) & 0xFFFF != step]


async def start_stream(dut, *, rate, events_from):
    dut.rle.value, dut.rate.value, dut.stream.value = 0, rate, 1
    dut.trig_mask.value, dut.trig_value.value = 0, 0
    dut.post_beats.value = events_from                      # このストリームでは最初のイベントの位置
    dut.run.value = 1


@cocotb.test()
async def analyzer_and_events_share_one_stream_without_gaps(dut):
    # ロジアナ（8 周期ごと）とイベント（約 100 周期ごと）を同時に流し、受け手が時々・長く止まっても、どちらも欠け・
    # 重複・入れ替わりが無い。止めた時点までに書いたイベントは全部届く
    await setup(dut, mem_beats=1024)
    dut.ev_enable.value = 1
    await ClockCycles(dut.clk, 5)
    source = EventSource(dut, 97)
    await start_stream(dut, rate=7, events_from=0)
    # 届く拍は見出し・イベントを含む（イベントの方が多い）。ロジアナだけでリング（960 拍）を超える量を流す。
    # 長い停止はイベントのリング（64 件）の半分に届かない長さ（1000 周期 = 約 10 件。出口は 1 バイト/周期なので、追いつくのに時間がかかる）
    at_stop = []

    async def watch_stop():
        await FallingEdge(dut.run)
        at_stop.append(len(source.sent))                                   # 止めた時点までに送った数
    cocotb.start_soon(watch_stop())
    data = await stream_until(dut, beats=2500, stall=lambda n: (n // 37) % 5 == 0 or 30_000 <= n < 31_000)
    source.stop()
    la, events, segments = demux(data)
    samples = decode_beats(la, False)
    assert len(samples) >= 1000 * 8
    assert not counter_gaps(samples, 8)
    assert dut.overrun.value == 0
    assert len(events) > 300 and events == source.sent[:len(events)]       # 先頭から順に、欠けなく
    assert at_stop[0] - 2 <= len(events) <= at_stop[0] + 2                 # 止めた時点までに書いた分（その後は流さない）
    kinds = [s.kind for s in segments]
    assert kinds.count(g.STREAM_SEG_EVENTS) > 5 and kinds.count(g.STREAM_SEG_LA) > 5   # 交互に載る
    assert int(dut.stream_beats.value) * 16 == len(data)                    # 出すと決めた拍 = 届いた拍
    assert len(segments) * 10 < len(data) // 16                            # 見出しは 1 割未満（溜めてから出す）


@cocotb.test()
async def events_start_from_the_given_number(dut):
    # 流す前に書かれた 10 件のうち、4 件目から流す（ホストの取り込みが読んだところの続き）
    await setup(dut, mem_beats=1024)
    dut.ev_enable.value = 1
    await ClockCycles(dut.clk, 5)
    source = EventSource(dut, 20)
    while len(source.sent) < 10:
        await RisingEdge(dut.clk)
    source.stop()
    await ClockCycles(dut.clk, 50)
    await start_stream(dut, rate=7, events_from=4)
    data = await stream_until(dut, beats=100)
    _, events, segments = demux(data)
    assert events == source.sent[4:]
    assert next(s.seq for s in segments if s.kind == g.STREAM_SEG_EVENTS) == 4


@cocotb.test()
async def event_backlog_of_half_the_ring_stops_the_stream_with_overrun(dut):
    # 受け手が止まったまま、イベントだけが速く来る（ロジアナは遅い）: イベントの未読がリングの半分（32 件）に
    # 達したら書き込みを止めて overrun。それまでのイベントは欠けなく届き、その後のイベントは流さない
    await setup(dut, mem_beats=1024)
    dut.ev_enable.value = 1
    await ClockCycles(dut.clk, 5)
    dut.out_ready.value = 0
    source = EventSource(dut, 9)
    await start_stream(dut, rate=999, events_from=0)
    await wait_for(dut, "overrun", timeout=100_000)
    data = await stream_until(dut, beats=1)
    source.stop()
    la, events, _ = demux(data)
    assert not counter_gaps(decode_beats(la, False), 1000)
    # 届くのは、止まる前に途中の FIFO（mem → sys の 64 拍等）へ出た分と、リングの半分の未読
    assert len(events) >= EV_CAP // 2 and dut.overrun.value == 1, len(events)
    assert events == source.sent[:len(events)]
    assert len(source.sent) > len(events) + 20                              # その後も来ていた


@cocotb.test()
async def plain_capture_and_event_reads_still_work_after_a_stream(dut):
    # ストリームの後も、普通の捕捉と、イベントのリングの読み出し（read_region = 1）は今のまま
    await setup(dut, mem_beats=1024)
    dut.ev_enable.value = 1
    await ClockCycles(dut.clk, 5)
    source = EventSource(dut, 25)
    await start_stream(dut, rate=7, events_from=0)
    await stream_until(dut, beats=50)
    dut.stream.value = 0
    source.stop()
    await ClockCycles(dut.clk, 100)
    written = int(dut.ev_written.value)
    assert written == len(source.sent)
    from tests.tb.la_deep_buffer_tb import read_bytes
    dut.read_region.value = 1
    n = min(written, EV_CAP)
    data = await read_bytes(dut, (written - n) % EV_CAP, n)
    got = [int.from_bytes(data[i:i + 16], "little") for i in range(0, len(data), 16)]
    assert got == source.sent[-n:]
