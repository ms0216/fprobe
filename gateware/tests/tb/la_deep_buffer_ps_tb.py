"""la_deep_buffer の「PS が DDR を直接読む」モード（ps_read）のテストベンチ。ADDR_BITS = 10（ロジアナのリング 960 拍、イベントの
リング 64 件）。仕様: docs/superpowers/specs/2026-11-03-ps-stream-position-design.md（位置の約束）、2026-10-30-la-stream-ps-direct-design.md

PS の役は、このテストが演じる（host/pynq/fprobe-relay の relay.c と同じ約束）: 公開された組（ps_pub_epoch の流れの書き位置）まで
メモリのモデルのリングから読み、読み終えた位置を、その流れの番号を添えて窓の模型（tests/tb/ps_window_model.py = la_ps_port）に書く。
リングの位置は総数からでなく、付き始めに公開された書き位置（ps_la_wptr）から数える（総数は 2^32 で折り返す）。

環境変数 LDB_SAME_CLOCK = 1 なら mem_clk = clk（PYNQ と同じ。generic SAME_CLOCK = true と組む）。"""

import os

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, ReadOnly, RisingEdge

from fprobe.la import decode_beats
from tests.models.mem_port import MemPortModel
from tests.tb.la_deep_buffer_mix_tb import EventSource, counter_gaps
from tests.tb.la_deep_buffer_tb import EV_CAP
from tests.tb.ps_window_model import PsWindow

LA_RING = 1024 - EV_CAP                                  # 960 拍
EV_BASE = LA_RING                                       # イベントのリングはロジアナのリングの後ろ
M32 = 1 << 32
SAME_CLOCK = os.environ.get("LDB_SAME_CLOCK") == "1"
TOTAL_START = (-int(os.environ.get("LDB_TOTAL_BELOW_4G", "0"))) % M32   # 総数の流れの開始の値（generic TOTAL_START_BELOW_4G）


async def setup(dut, mem_beats=1024):
    for name in ("rst", "mem_rst", "probes", "ext_trigger", "time", "rate", "trig_mask", "trig_value", "trig_rise",
                 "trig_fall", "run", "rle", "stream", "post_beats", "pretrigger", "read", "read_first", "read_count",
                 "ev_enable", "ev_valid", "ev_data", "read_region", "ps_read", "ps_ret_epoch", "ps_la_consumed",
                 "ps_ev_consumed", "ps_out_beats", "read_ps", "ps_rd_ack", "wr_pending"):
        getattr(dut, name).value = 0
    dut.out_ready.value = 1
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    cocotb.start_soon(Clock(dut.mem_clk, 10000 if SAME_CLOCK else 10040, unit="ps").start())
    mem = MemPortModel(dut, clock=dut.mem_clk, size_beats=mem_beats, stall=0.2)
    dut.rst.value = dut.mem_rst.value = 1
    await ClockCycles(dut.clk, 5)
    dut.rst.value = dut.mem_rst.value = 0
    cocotb.start_soon(mem.run())

    async def counter():
        value = 0
        while True:
            await RisingEdge(dut.clk)
            value = (value + 1) & 0xFFFF
            dut.probes.value = value
            dut.time.value = int(dut.time.value) + 1
    cocotb.start_soon(counter())
    return mem, PsWindow(dut, ev_mask=EV_CAP - 1, la_start=TOTAL_START)


async def start(dut, *, rate, events_from=0, ps_read=1):
    dut.rle.value, dut.rate.value, dut.stream.value = 0, rate, 1
    dut.trig_mask.value, dut.trig_value.value = 0, 0
    dut.post_beats.value = events_from
    dut.ps_read.value = ps_read
    dut.run.value = 1


def ring_beats(mem, pos, count):
    """メモリのモデルのロジアナのリングの位置 pos から count 拍（16 バイトずつ）。"""
    return b"".join(mem.memory.get((pos + i) % LA_RING, 0).to_bytes(16, "little") for i in range(count))


class Pub:
    """公開された組（同じ周期に読む）。"""

    def __init__(self, dut):
        self.epoch = int(dut.ps_pub_epoch.value)
        self.active, self.writing = int(dut.ps_active.value), int(dut.ps_writing.value)
        self.overrun = int(dut.ps_overrun.value)
        self.written, self.wptr = int(dut.ps_la_written.value), int(dut.ps_la_wptr.value)
        self.ev_wpos, self.ev_start = int(dut.ps_ev_wpos.value), int(dut.ps_ev_start.value)


class Ps:
    """PS の役（relay.c と同じ約束）: every 周期ごとに公開された組を読み、その流れに付いて（付き始めは窓の読み終えた位置と
    公開された書き位置からリングの読み位置を決める）、書き位置まで読み、位置をその流れの番号を添えて窓に書く。
    stall(cycle) が真の間は読まない。"""

    def __init__(self, dut, mem, window, *, every=40, stall=lambda cycle: False):
        self.dut, self.mem, self.window, self.every, self.stall = dut, mem, window, every, stall
        self.epoch = None
        self.la = bytearray()
        self.events = []
        self.la_read = self.ev_read = self.rpos = 0
        self.attaches = 0
        self.task = cocotb.start_soon(self._run())

    def step(self, pub):
        if not pub.active or pub.epoch != self.window.epoch:   # 動いていない・前の流れ（窓の位置は新しい流れのもの）
            return
        if pub.epoch != self.epoch:                         # 付き始め: 窓の読み終えた位置から
            self.epoch = pub.epoch
            self.attaches += 1
            self.la_read = int(self.dut.ps_la_consumed.value)
            self.ev_read = int(self.dut.ps_ev_consumed.value)
            self.rpos = (pub.wptr - (pub.written - self.la_read) % M32) % LA_RING
        n = (pub.written - self.la_read) % M32
        assert n <= LA_RING, ("未読がリングより多い", n)
        if n:
            self.la += ring_beats(self.mem, self.rpos, n)
            self.rpos = (self.rpos + n) % LA_RING
            self.la_read = (self.la_read + n) % M32
        while (pub.ev_start + self.ev_read) % EV_CAP != pub.ev_wpos:
            self.events.append(self.mem.memory.get(EV_BASE + (pub.ev_start + self.ev_read) % EV_CAP, 0))
            self.ev_read += 1
        assert self.window.write(self.epoch, la=self.la_read, ev=self.ev_read)

    async def _run(self):
        cycle = 0
        while True:
            await ClockCycles(self.dut.clk, self.every)
            cycle += self.every
            if self.stall(cycle):
                continue
            await ReadOnly()
            pub = Pub(self.dut)
            await RisingEdge(self.dut.clk)
            self.step(pub)

    def stop(self):
        self.task.cancel()


async def wait_until(dut, cond, limit, what):
    """cond が真になるまで待ち、その次の辺の後（書ける相）で返す。"""
    for _ in range(limit):
        await RisingEdge(dut.clk)
        await ReadOnly()
        if cond():
            await RisingEdge(dut.clk)
            return
    raise AssertionError(what)


@cocotb.test()
async def ps_mode_publishes_the_write_position_and_streams_nothing(dut):
    # 読まない PS: 書き位置が進み、データ（ロジアナの拍）がリングに並ぶ。メモリの読み出しも、ストリームのバイト列も無い
    mem, _ = await setup(dut)
    await start(dut, rate=0)
    streamed = 0
    for _ in range(1500):
        await ReadOnly()
        streamed += int(dut.out_valid.value)
        await RisingEdge(dut.clk)
    assert streamed == 0 and mem.reads == 0
    written = int(dut.ps_la_written.value)
    assert 100 < written < LA_RING, written
    assert int(dut.ps_la_wptr.value) == written                                 # 1 周目は書き位置 = 総数
    assert dut.overrun.value == 0
    samples = decode_beats(ring_beats(mem, 0, written - 2), False)
    assert not counter_gaps(samples, 1)


@cocotb.test()
async def unread_beats_near_the_ring_stop_the_writer_with_overrun(dut):
    # PS が読み終えた位置を返さないと、未読がリングに迫った時点（リング − 256 拍）で書き込みが止まり、overrun が立つ
    mem, _ = await setup(dut)
    await start(dut, rate=0)
    await wait_until(dut, lambda: dut.overrun.value == 1, 20_000, "overrun が立たない")
    await ClockCycles(dut.clk, 200)
    await ReadOnly()
    written = int(dut.ps_la_written.value)
    assert LA_RING - 256 <= written <= LA_RING, written
    assert int(dut.ps_overrun.value) == 1 and int(dut.ps_writing.value) == 0  # 公開された組でも
    assert mem.reads == 0


async def stream_many_turns(dut, turns):
    mem, window = await setup(dut)
    ps = Ps(dut, mem, window, every=100, stall=lambda n: (n // 700) % 4 == 3)         # 時々、長く読まない
    await start(dut, rate=0)
    while len(ps.la) < turns * LA_RING * 16:
        await RisingEdge(dut.clk)
        assert dut.overrun.value == 0, "読んでいるのに溢れた"
    dut.run.value = 0
    await wait_until(dut, lambda: dut.reading.value == 0, 20_000, "reading が下がらない")
    ps.stop()
    assert dut.overrun.value == 0
    assert ps.la_read == int(dut.ps_la_written.value)                           # 書いた分を全部読んだ
    assert not counter_gaps(decode_beats(bytes(ps.la), False), 1)
    assert mem.reads == 0 and ps.attaches == 1 and window.stale_writes == 0
    return ps


@cocotb.test()
async def a_reading_ps_lets_the_writer_run_many_ring_turns_without_gaps(dut):
    await stream_many_turns(dut, 3)


@cocotb.test()
async def totals_wrap_at_2_32_without_a_gap(dut):
    # 総数（書いた・読んだ）は 32 ビットで折り返す（2^32 拍 = 68.7 GB）。試験は TOTAL_START_BELOW_4G で 2^32 の手前から数え、
    # 折り返しをまたいで何周も読む。リングの位置は総数 mod リングと一致しない（リングは 2 の冪でない）ので、PS は公開された
    # 書き位置から数える。前は la_safe（w_total >= 応答待ち）と ps_consumed <= w_total を折り返しなしで比べていた（C2）
    ps = await stream_many_turns(dut, 4)
    assert (ps.la_read - TOTAL_START) % M32 == len(ps.la) // 16


@cocotb.test()
async def reading_stays_high_until_the_ps_has_read_everything(dut):
    # 止めても、PS が読み終えるまでは「読み出し中」（ホストの _discard_stream と同じ意味）
    _, window = await setup(dut)
    await start(dut, rate=0)
    await ClockCycles(dut.clk, 800)
    dut.run.value = 0
    await ClockCycles(dut.clk, 3000)
    await ReadOnly()
    assert dut.reading.value == 1
    pub = Pub(dut)
    await RisingEdge(dut.clk)
    assert pub.written > 50
    window.write(pub.epoch, la=pub.written)
    await wait_until(dut, lambda: dut.reading.value == 0, 2000, "reading が下がらない")


@cocotb.test()
async def events_are_published_by_position_and_read_by_the_ps(dut):
    mem, window = await setup(dut)
    dut.ev_enable.value = 1
    await ClockCycles(dut.clk, 5)
    ps = Ps(dut, mem, window, every=60)
    source = EventSource(dut, 97)
    await start(dut, rate=7, events_from=0)
    while len(source.sent) < 200:
        await RisingEdge(dut.clk)
        assert dut.overrun.value == 0
    source.stop()
    dut.run.value = 0
    await ClockCycles(dut.clk, 3000)
    ps.stop()
    assert ps.events == source.sent[:len(ps.events)] and len(ps.events) >= len(source.sent) - 2, (
        len(ps.events), len(source.sent))
    assert dut.overrun.value == 0 and mem.reads == 0


@cocotb.test()
async def restarting_event_recording_mid_stream_keeps_the_event_positions(dut):
    # イベントを読む流れの途中で events.enable を始め直しても、イベントのリングを先頭に戻さない（書き位置が続く）。前は書き込み器が
    # 書き位置を 0 に戻し、PS の読み位置（最初の位置 + 読んだ数）との差がでたらめになって、溢れの判定・PS の読みが壊れた（C3）
    mem, window = await setup(dut)
    dut.ev_enable.value = 1
    await ClockCycles(dut.clk, 5)
    ps = Ps(dut, mem, window, every=60)
    source = EventSource(dut, 53)
    await start(dut, rate=7, events_from=0)
    while len(source.sent) < 40:
        await RisingEdge(dut.clk)
    source.stop()
    await ClockCycles(dut.clk, 300)
    wpos = int(dut.ps_ev_wpos.value)
    assert wpos > 30
    dut.ev_enable.value = 0                                                      # 記録を止めて、始め直す
    await ClockCycles(dut.clk, 20)
    dut.ev_enable.value = 1
    await ClockCycles(dut.clk, 20)
    more = EventSource(dut, 53, base=0xF0000)
    while len(more.sent) < 60:
        await RisingEdge(dut.clk)
        assert dut.overrun.value == 0, "始め直しで溢れた"
    more.stop()
    dut.run.value = 0
    await ClockCycles(dut.clk, 3000)
    ps.stop()
    assert dut.overrun.value == 0
    sent = source.sent + more.sent
    assert ps.events == sent[:len(ps.events)] and len(ps.events) >= len(sent) - 2, (len(ps.events), len(sent))


@cocotb.test()
async def an_event_backlog_of_half_the_ring_stops_the_writer_with_overrun(dut):
    await setup(dut)
    dut.ev_enable.value = 1
    await ClockCycles(dut.clk, 5)
    source = EventSource(dut, 9)
    await start(dut, rate=999, events_from=0)                                  # ロジアナは遅い。イベントだけが溜まる
    for _ in range(50_000):
        await RisingEdge(dut.clk)
        if dut.overrun.value == 1:
            break
    source.stop()
    assert dut.overrun.value == 1
    sent_at_stop = len(source.sent)
    await ClockCycles(dut.clk, 300)
    assert EV_CAP // 2 <= sent_at_stop <= EV_CAP, sent_at_stop


@cocotb.test()
async def each_start_publishes_a_new_epoch_together_with_its_positions_and_state(dut):
    # 流れの番号（ps_epoch）は run の 0→1 ごとに sys で 1 進む。公開された組（ps_pub_epoch・書き位置・状態）は 1 つの時点のもの:
    # 新しい番号が見えたとき、書き位置・状態は新しい流れのもの（C4: 前は状態が別の受け渡しで、番号と揃っていなかった）
    _, window = await setup(dut)
    await ClockCycles(dut.clk, 40)
    epoch0 = int(dut.ps_epoch.value)
    for turn in (1, 2, 3):
        ps_read = int(turn != 3)                                                 # 3 回目は PL が読み戻す流れ
        await start(dut, rate=0, ps_read=ps_read)
        await RisingEdge(dut.clk)
        await ReadOnly()
        assert int(dut.ps_epoch.value) == epoch0 + turn
        for _ in range(600):
            await RisingEdge(dut.clk)
            await ReadOnly()
            pub = Pub(dut)
            if pub.epoch == epoch0 + turn:
                assert pub.writing == 1 and pub.overrun == 0 and pub.active == ps_read
                assert pub.written < 40, pub.written                             # 新しい流れの総数（前の流れの総数ではない）
                break
            assert pub.epoch == epoch0 + turn - 1
        else:
            raise AssertionError("新しい番号が公開されない")
        await ClockCycles(dut.clk, 200)
        dut.run.value = 0
        await ClockCycles(dut.clk, 300)
        await ReadOnly()
        pub = Pub(dut)
        await RisingEdge(dut.clk)
        if ps_read:
            window.write(pub.epoch, la=pub.written)
        await wait_until(dut, lambda: dut.reading.value == 0, 5000, "reading が下がらない")


@cocotb.test()
async def stale_positions_of_an_older_stream_are_never_used(dut):
    # 前の流れの番号を添えた位置（中継が新しい流れに気づく前に書いた古い位置）は、新しい流れでは使わない（C1）。前は書いた側でなく
    # sys 側で世代を付けたので、開始の後に届いた古い位置が新しい世代として使われ、後で書き位置が追い越した時に読み位置が飛んだ
    mem, window = await setup(dut)
    await start(dut, rate=0)
    await ClockCycles(dut.clk, 3000)
    dut.run.value = 0
    await ClockCycles(dut.clk, 400)
    old = int(dut.ps_pub_epoch.value)
    written = int(dut.ps_la_written.value)
    assert written > 300
    window.write(old, la=written)                                                # 前の流れを全部読んだ
    await wait_until(dut, lambda: dut.reading.value == 0, 5000, "reading が下がらない")
    await start(dut, rate=0)
    await ClockCycles(dut.clk, 5)
    # 窓は新しい番号で 0 に戻した。古い番号の書きは捨てる。窓を通さずに古い番号の値を直接入れても、mem は使わない
    assert not window.write(old, la=written)
    window.task.cancel()
    dut.ps_ret_epoch.value = old
    dut.ps_la_consumed.value = written
    await wait_until(dut, lambda: int(dut.ps_pub_epoch.value) == old + 1, 200, "mem が新しい流れを始めない")
    await ClockCycles(dut.clk, 20)                                               # 数えの受け渡しも新しい流れに
    for _ in range(12_000):                                                      # 8 周期に 1 拍: 溢れるまで約 6000 周期
        await RisingEdge(dut.clk)
        await ReadOnly()
        assert int(dut.stat_unread.value) == int(dut.stat_written.value), "古い位置を使った"
        if dut.overrun.value == 1:
            break
    assert dut.overrun.value == 1                                                # 何も読まれていないので溢れる
    assert int(dut.ps_la_written.value) > written                               # 古い位置を追い越しても使わない


@cocotb.test()
async def a_start_and_a_stop_closer_than_the_handover_are_both_carried_out(dut):
    # 開始の設定は状態（番号・run）として渡すので、run の辺が受け渡しより近くても、mem は最後の状態に着く（C5: 前は run の辺ごとの
    # 1 周期の読み込みで、近い 2 つの辺の片方が消えうるうえ、受け側が取り込む間に値が変わりえた）
    mem, window = await setup(dut)
    for pattern in ([1, 0, 1], [1, 0], [1, 1, 0, 0, 1, 0, 1]):
        e0 = int(dut.ps_epoch.value)
        dut.rle.value, dut.rate.value, dut.stream.value, dut.ps_read.value = 0, 0, 1, 1
        for level in pattern:
            dut.run.value = level
            await RisingEdge(dut.clk)
        rises = sum(1 for a, b in zip([0] + pattern, pattern) if b and not a)
        await ReadOnly()
        assert int(dut.ps_epoch.value) == e0 + rises
        await RisingEdge(dut.clk)
        await ClockCycles(dut.clk, 60)
        await ReadOnly()
        pub = Pub(dut)
        assert pub.epoch == e0 + rises                                          # mem は最後の流れに着いた
        assert pub.writing == pattern[-1], (pattern, pub.writing)
        await RisingEdge(dut.clk)
        dut.run.value = 0
        await ClockCycles(dut.clk, 100)
        await ReadOnly()
        pub = Pub(dut)
        await RisingEdge(dut.clk)
        window.write(pub.epoch, la=pub.written)
        await wait_until(dut, lambda: dut.reading.value == 0 and dut.capturing.value == 0, 5000, "止まらない")


@cocotb.test()
async def positions_exclude_writes_that_have_not_been_acknowledged_yet_by_their_kind(dut):
    # AXI は書きと読みの順を保証しない（axi_mem_port）ので、応答の来ていない書き（wr_pending 個）の拍は、PS に「読める」と
    # 教えない。応答は書いた順に返るので、応答の来ていないのは最後に出した wr_pending 個の書き。その中のイベントの数だけイベントの
    # 位置から、ロジアナの拍の数だけロジアナの位置（総数と書き位置）から引く
    mem, window = await setup(dut)
    dut.ev_enable.value = 1
    await ClockCycles(dut.clk, 5)
    await start(dut, rate=499, events_from=0)                                   # ロジアナは 4000 周期に 1 拍
    while int(dut.ps_la_written.value) < 3:                                     # 3 拍書いた直後から（次の拍まで約 4000 周期）
        await RisingEdge(dut.clk)
    source = EventSource(dut, 40)
    while len(source.sent) < 10:
        await RisingEdge(dut.clk)
    source.stop()
    await ClockCycles(dut.clk, 200)                                             # 最後の書きはイベント（ロジアナはこの間に書かない）
    la_total, ev_pos, wptr = int(dut.ps_la_written.value), int(dut.ps_ev_wpos.value), int(dut.ps_la_wptr.value)
    assert ev_pos == 10 and la_total == 3 and wptr == 3
    dut.wr_pending.value = 3
    await ClockCycles(dut.clk, 200)
    assert int(dut.ps_ev_wpos.value) == ev_pos - 3 and int(dut.ps_la_written.value) == la_total
    dut.wr_pending.value = 0
    await ClockCycles(dut.clk, 200)
    dut.run.value = 0
    await ClockCycles(dut.clk, 400)
    epoch = int(dut.ps_pub_epoch.value)
    window.write(epoch, la=int(dut.ps_la_written.value), ev=10)
    await wait_until(dut, lambda: dut.reading.value == 0, 5000, "reading が下がらない")
    await start(dut, rate=0, events_from=10)                                    # ロジアナは 8 周期に 1 拍、イベントは無い
    await ClockCycles(dut.clk, 300)
    dut.run.value = 0
    await ClockCycles(dut.clk, 400)                                             # 最後の書きはロジアナの拍
    la_total, ev_pos, wptr = int(dut.ps_la_written.value), int(dut.ps_ev_wpos.value), int(dut.ps_la_wptr.value)
    assert la_total > 3 and wptr == la_total
    dut.wr_pending.value = 3
    await ClockCycles(dut.clk, 200)
    assert int(dut.ps_la_written.value) == la_total - 3 and int(dut.ps_ev_wpos.value) == ev_pos
    assert int(dut.ps_la_wptr.value) == wptr - 3
    dut.wr_pending.value = 0
    await ClockCycles(dut.clk, 200)
    assert int(dut.ps_la_written.value) == la_total and int(dut.ps_ev_wpos.value) == ev_pos


@cocotb.test()
async def ps_active_stays_high_until_the_ps_has_read_everything_even_after_the_host_writes_zero(dut):
    # ホストは止めるとき、制御レジスタに 0 を書く（run も ps_read も 0 になる）。PS が読み終えていない拍があるうちは、
    # 流れは生きている（ps_active = 1）。読み終えて初めて 0 になる。中継はこの印を見て読み続ける
    _, window = await setup(dut)
    await ClockCycles(dut.clk, 40)
    assert int(dut.ps_active.value) == 0
    await start(dut, rate=0)
    await ClockCycles(dut.clk, 800)
    assert int(dut.ps_active.value) == 1
    dut.run.value = 0
    dut.ps_read.value = 0                                                         # 止めるとき、ホストは全部 0 を書く
    dut.stream.value = 0
    await ClockCycles(dut.clk, 3000)
    assert int(dut.ps_active.value) == 1 and dut.reading.value == 1               # 未読が残っている間は生きている
    await ReadOnly()
    pub = Pub(dut)
    await RisingEdge(dut.clk)
    assert pub.written > 50
    window.write(pub.epoch, la=pub.written)
    await wait_until(dut, lambda: dut.reading.value == 0 and int(dut.ps_active.value) == 0, 2000, "生きたまま")


@cocotb.test()
async def stream_beats_shows_what_the_ps_decided_to_send_and_keeps_showing_it_after_the_stream(dut):
    # PS が読む流れでは、stream_beats（ホストが「必ず届く量」を知る CSR）は、PS が出すと決めた拍の総数（ps_out_beats。見出しを含む）。
    # 流れが終わって ps_active が落ちても、その値のまま（受け取り済みとの差を壊さない）。PS が読まない流れは、今まで通り
    _, window = await setup(dut)
    await start(dut, rate=0)
    await ClockCycles(dut.clk, 300)
    epoch = int(dut.ps_pub_epoch.value)
    window.write(epoch, beats=123)
    await ClockCycles(dut.clk, 20)
    assert int(dut.stream_beats.value) == 123
    dut.run.value = 0
    dut.ps_read.value = 0
    dut.stream.value = 0
    await ClockCycles(dut.clk, 400)                                              # 止めた後に書かれる残りが落ち着く
    window.write(epoch, la=int(dut.ps_la_written.value))
    await ClockCycles(dut.clk, 3000)
    assert int(dut.ps_active.value) == 0
    assert int(dut.stream_beats.value) == 123                                    # 終わった後も同じ
    dut.stream.value, dut.ps_read.value, dut.run.value = 1, 0, 1                 # PS が読まない流れ: PL が出した拍の総数
    await ClockCycles(dut.clk, 200)
    assert int(dut.stream_beats.value) != 123


async def pulse(dut, name):
    getattr(dut, name).value = 1
    await RisingEdge(dut.clk)
    getattr(dut, name).value = 0


@cocotb.test()
async def a_ps_read_request_is_published_and_reading_stays_high_until_the_ps_acknowledges(dut):
    # トリガ取り込みの読み出しを PS が DDR から直接行う（read_ps）。PL は mem_reader を動かさず、「どの領域のどこから何拍」を
    # 通し番号つきで公開する。PS が読み終えて番号を返す（ps_rd_ack）まで、reading は 1
    mem, _ = await setup(dut)
    await ClockCycles(dut.clk, 40)
    epoch0 = int(dut.ps_rd_epoch.value)
    assert dut.reading.value == 0
    dut.read_region.value, dut.read_first.value, dut.read_count.value = 0, 10, 20
    await pulse(dut, "read_ps")
    dut.read_first.value, dut.read_count.value = 99, 99                       # 要求の後に CSR が変わっても、公開は要求の時点の値
    await ClockCycles(dut.clk, 5)
    assert int(dut.ps_rd_epoch.value) == (epoch0 + 1) & 0xFF
    assert (int(dut.ps_rd_region.value), int(dut.ps_rd_first.value), int(dut.ps_rd_count.value)) == (0, 10, 20)
    assert dut.reading.value == 1
    await ClockCycles(dut.clk, 300)
    assert dut.reading.value == 1 and mem.reads == 0                            # PS が返すまで 1。PL はメモリを読まない
    dut.ps_rd_ack.value = (epoch0 + 1) & 0xFF
    await wait_until(dut, lambda: dut.reading.value == 0, 100, "reading が下がらない")
    await RisingEdge(dut.clk)
    dut.read_region.value, dut.read_first.value, dut.read_count.value = 1, 3, 5   # 次の要求（イベントのリング）
    await pulse(dut, "read_ps")
    await ClockCycles(dut.clk, 5)
    assert int(dut.ps_rd_epoch.value) == (epoch0 + 2) & 0xFF
    assert (int(dut.ps_rd_region.value), int(dut.ps_rd_first.value), int(dut.ps_rd_count.value)) == (1, 3, 5)
    assert dut.reading.value == 1                                              # 古い番号の返事では下がらない


@cocotb.test()
async def a_normal_read_is_unchanged_and_reading_covers_every_byte_until_it_has_left(dut):
    # PL が読み戻す読み出し: reading は、最後のバイトが出るまで 1、出た後に下がる（前は「mem が busy でなくなってから 31 周期」の
    # 待ち時間で覆っていた。今は mem が入れた拍の数と sys が取り出した数を比べる。D8）
    from tests.tb.la_deep_buffer_tb import capture
    mem, _ = await setup(dut)
    await ClockCycles(dut.clk, 40)
    epoch0 = int(dut.ps_rd_epoch.value)
    beat, _, _ = await capture(dut, rle=False, rate=0, post=20, value=0x0800)
    dut.out_ready.value = 0
    dut.read_first.value, dut.read_count.value = (beat - 10) % LA_RING, 30
    await pulse(dut, "read")
    await ReadOnly()
    assert dut.reading.value == 1                                               # 要求の直後から
    out = bytearray()
    cycle = 0
    while len(out) < 30 * 16:
        await RisingEdge(dut.clk)
        dut.out_ready.value = int(cycle % 3 == 0)                               # 受け手はゆっくり
        cycle += 1
        await ReadOnly()
        if dut.out_valid.value == 1 and dut.out_ready.value == 1:
            out.append(int(dut.out_data.value))
        if len(out) < 30 * 16:
            assert dut.reading.value == 1, f"まだ {30 * 16 - len(out)} バイト残っているのに reading が下がった"
    await RisingEdge(dut.clk)
    dut.out_ready.value = 1
    await wait_until(dut, lambda: dut.reading.value == 0, 10, "出し終えても reading が下がらない")
    assert mem.reads > 0 and int(dut.ps_rd_epoch.value) == epoch0
