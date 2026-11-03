"""la_deep_buffer の欠けの区間（STREAM_GAPS = true、control.gaps。段 B）のテストベンチ。ADDR_BITS = 12（ロジアナのリング 3840 拍、
イベントのリング 256 件）、PS が DDR のリングを直接読む流れ。LANES は lane_data の幅から（1 = probes、8 = lane_data）。
仕様: docs/superpowers/specs/2026-11-01-la-deep-fast-design.md §12

入力はサンプルの位置の関数（RAW: 位置そのもの、RLE: 位置 // 長さ）。PS の役が読んだロジアナの拍と欠けの記録（イベント）を、
ホストと同じ fprobe.la.GapPlacer で置き、値のある全サンプルが「欠けを数えた位置」の関数の値と一致することを確かめる
（欠けの長さが 1 サンプルでも違えば、後ろの全サンプルがずれて合わない）。"""

import random

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, ReadOnly, RisingEdge

from fprobe.events import KIND_LA_GAP, decode_events
from fprobe.la import GapPlacer, _expand_words, decode_beats
from tests.models.mem_port import MemPortModel
from tests.tb.ps_window_model import PsWindow

TOTAL = 4096
EV_CAP = TOTAL // 16
LA_RING = TOTAL - EV_CAP


async def setup(dut, *, mem_stall=0.2):
    for name in ("rst", "mem_rst", "probes", "lane_data", "step", "first_ch", "ext_trigger", "time", "rate", "trig_mask",
                 "trig_value", "trig_rise", "trig_fall", "run", "rle", "pack", "stream", "gaps", "ps_read", "ps_out_beats",
                 "ps_la_consumed", "ps_ev_consumed", "ps_ret_epoch", "post_beats", "pretrigger", "read", "read_ps", "ps_rd_ack",
                 "read_first", "read_count", "ev_enable", "ev_valid", "ev_data", "read_region"):
        getattr(dut, name).value = 0
    dut.out_ready.value = 1
    lanes = len(dut.lane_data) // 16
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    cocotb.start_soon(Clock(dut.mem_clk, 10040, unit="ps").start())
    mem = MemPortModel(dut, clock=dut.mem_clk, size_beats=TOTAL, stall=mem_stall)
    dut.rst.value = dut.mem_rst.value = 1
    await ClockCycles(dut.clk, 5)
    dut.rst.value = dut.mem_rst.value = 0
    cocotb.start_soon(mem.run())
    PsWindow(dut)                                        # 位置は流れの番号つきで返す（la_ps_port の模型）
    return mem, lanes


class Input:
    """サンプルの値の関数 f（細かいサンプルの番号 → 値）を毎周期入れる（LANES = 1 は probes、> 1 は lane_data のレーン）。"""

    def __init__(self, dut, lanes, f):
        self.dut, self.lanes, self.f = dut, lanes, f
        self.cycle = 0
        cocotb.start_soon(self._run())

    async def _run(self):
        dut = self.dut
        while True:
            await RisingEdge(dut.clk)
            self.cycle += 1
            dut.time.value = self.cycle
            base = self.cycle * self.lanes
            if self.lanes == 1:
                dut.probes.value = self.f(base) & 0xFFFF
            else:
                v = 0
                for k in range(self.lanes):
                    v |= (self.f(base + k) & 0xFFFF) << (16 * k)
                dut.lane_data.value = v


class Ps:
    """PS の役: every 周期ごとに公開された書き位置までロジアナの拍を読み（stall(cycle) の間は読まない）、ev_every 回に 1 回
    イベントも読む。読んだ拍を GapPlacer に入れ、その後に gap_last_beat → gap_count を読んで置く（ホストの LaStream と同じ順）。"""

    def __init__(self, dut, mem, *, rle, pack, every=64, ev_every=3, stall=lambda cycle: False):
        self.dut, self.mem, self.every, self.ev_every, self.stall = dut, mem, every, ev_every, stall
        self.placer = GapPlacer(bool(rle), pack, hold_bytes=64 << 20)   # 試験の流れ全体を持てる上限（host の GapPlacer は上限が要る）
        self.pieces = []
        self.la_beats = 0
        self.ev_count = 0
        self.dap_events = []
        self.task = cocotb.start_soon(self._run())

    def _read_events(self, ev_start, ev_wpos):
        while (ev_start + self.ev_count) % EV_CAP != ev_wpos:
            v = self.mem.memory.get(LA_RING + (ev_start + self.ev_count) % EV_CAP, 0)
            for e in decode_events(v.to_bytes(16, "little")):
                if e.kind == KIND_LA_GAP:
                    self.placer.add_record(e.address, e.data, e.time)
                else:
                    self.dap_events.append(e)
            self.ev_count += 1
            self.dut.ps_ev_consumed.value = self.ev_count % EV_CAP

    async def _run(self):
        dut = self.dut
        cycle = rounds = 0
        while True:
            await ClockCycles(dut.clk, self.every)
            cycle += self.every
            if self.stall(cycle):
                continue
            rounds += 1
            await ReadOnly()
            written = int(dut.ps_la_written.value)
            ev_wpos, ev_start = int(dut.ps_ev_wpos.value), int(dut.ps_ev_start.value)
            await RisingEdge(dut.clk)
            if rounds % self.ev_every == 0:
                self._read_events(ev_start, ev_wpos)
            if written > self.la_beats:
                self.placer.add_beats(b"".join(self.mem.memory.get((self.la_beats + i) % LA_RING, 0).to_bytes(16, "little")
                                               for i in range(written - self.la_beats)))
                self.la_beats = written
                dut.ps_la_consumed.value = written
            await ReadOnly()
            last, reported = int(dut.gap_last_beat.value), int(dut.gap_count.value)
            await RisingEdge(dut.clk)
            self.pieces += self.placer.release(self.placer.received_beats, reported, last)

    async def finish(self):
        """止めた後: 書いた分とイベントを全部読み、最後の読みで置く。"""
        dut = self.dut
        self.task.cancel()
        for _ in range(4):
            await ClockCycles(dut.clk, 40)
            await ReadOnly()
            written = int(dut.ps_la_written.value)
            ev_wpos, ev_start = int(dut.ps_ev_wpos.value), int(dut.ps_ev_start.value)
            await RisingEdge(dut.clk)
            self._read_events(ev_start, ev_wpos)
            if written > self.la_beats:
                self.placer.add_beats(b"".join(self.mem.memory.get((self.la_beats + i) % LA_RING, 0).to_bytes(16, "little")
                                               for i in range(written - self.la_beats)))
                self.la_beats = written
                dut.ps_la_consumed.value = written
        await ReadOnly()
        last, reported = int(dut.gap_last_beat.value), int(dut.gap_count.value)
        await RisingEdge(dut.clk)
        self.pieces += self.placer.finish(reported, last)
        return reported


async def run_stream(dut, mem, lanes, *, cycles, f, rle=0, pack=0, step=0, stall=lambda c: False, gaps=1, ev_enable=1,
                     ev_every=3):
    dut.ev_enable.value = ev_enable
    await ClockCycles(dut.clk, 5)
    Input(dut, lanes, f)
    ps = Ps(dut, mem, rle=rle, pack=pack, stall=stall, ev_every=ev_every)
    dut.rle.value, dut.pack.value, dut.rate.value, dut.stream.value, dut.ps_read.value = rle, pack, 0, 1, 1
    dut.step.value = step
    dut.gaps.value = gaps
    dut.post_beats.value = 0
    dut.run.value = 1
    await ClockCycles(dut.clk, cycles)
    dut.run.value = 0
    await ClockCycles(dut.clk, 300)
    reported = await ps.finish()
    return ps, reported


def placed_samples(pieces, rle, pack):
    """GapPlacer の並び → [(位置, 値)]（値のあるサンプル）と欠けの列。"""
    out, gaps = [], []
    for piece in pieces:
        if piece[0] == "gap":
            gaps.append(piece[1])
            continue
        _, data, first, *_ = piece                                     # ("data", 拍, 最初の位置, 直前の値, …)
        values = _expand_words(decode_beats(data, bool(rle)), pack) if data else []
        out.extend(zip(range(first, first + len(values)), values))
    return out, gaps


def check_function(samples, f):
    """位置 s のサンプルの値が f(s + offset) & 0xFFFF（offset は流れの最初のサンプルの番号。1 つに決まること）。
    先頭の 64 サンプル（開始の直後の入力を置く前の値・同期化の段の値のことがある）は見ない。"""
    body = samples[64:]
    s0, v0 = body[0]
    offsets = [o for o in range(-64, 1 << 16) if f(s0 + o) & 0xFFFF == v0][:64]
    good = [o for o in offsets if all(f(s + o) & 0xFFFF == v for s, v in body[:: max(1, len(body) // 4000)])]
    assert good, f"どの起点でも合わない: 最初 {body[:4]}"
    o = good[0]
    bad = [(s, v, f(s + o) & 0xFFFF) for s, v in body if f(s + o) & 0xFFFF != v]
    assert not bad, f"位置と値が合わない（起点 {o}）: {bad[:5]} / {len(bad)}"


@cocotb.test()
async def raw_stream_keeps_running_over_a_full_ring_and_records_the_gap(dut):
    # PS が読まない間にリングの未読が迫ると、書き込みを止めずに拍を捨て、PS が読んで空いたら欠けの記録を出して続ける
    mem, lanes = await setup(dut)
    if lanes == 1:
        cycles, step = 70_000, 0                          # 16 ch・毎周期 1 サンプル: 1 拍 = 8 周期
        f = g = lambda i: i
        stall = lambda c: 2_000 <= c < 40_000 or 50_000 <= c < 62_000
    else:
        cycles, step = 60_000, 3                          # 語のレーン 0 だけ（100 MS/s）・16 ch: 1 拍 = 8 周期
        f = lambda i: i // 8                              # 細かいサンプルの番号 → 値（流れのサンプル s は語 s のレーン 0）
        g = lambda s: s
        stall = lambda c: 2_000 <= c < 40_000 or 50_000 <= c < 56_000
    ps, reported = await run_stream(dut, mem, lanes, cycles=cycles, f=f, step=step, stall=stall)
    assert int(dut.overrun.value) == 0
    samples, gaps = placed_samples(ps.pieces, 0, 0)
    assert gaps, "欠けが起きなかった（試験の前提）"
    assert reported == ps.placer._records and ps.placer.unplaced_beats == 0
    assert samples[-1][0] > gaps[-1].start, "欠けの後に値のあるサンプルが続かない"
    assert all(gap.samples > 0 and gap.samples % 8 == 0 for gap in gaps)        # 拍まるごと
    check_function(samples, g)


@cocotb.test()
async def rle_gaps_carry_the_lengths_of_the_dropped_records(dut):
    mem, lanes = await setup(dut)
    if lanes == 1:
        length, cycles = 2, 60_000                        # 2 周期ごとの変化: 1 拍 = 4 レコード = 8 周期
        stall = lambda c: 2_000 <= c < 40_000
    else:
        length, cycles = 13, 40_000                       # 13 細かいサンプルごとの変化: 1 拍 ≈ 6.5 周期
        stall = lambda c: 2_000 <= c < 32_000
    f = lambda i: i // length
    ps, reported = await run_stream(dut, mem, lanes, cycles=cycles, f=f, rle=1, stall=stall)
    assert int(dut.overrun.value) == 0
    samples, gaps = placed_samples(ps.pieces, 1, 0)
    assert gaps and samples[-1][0] > gaps[-1].start
    assert reported == ps.placer._records and ps.placer.unplaced_beats == 0
    check_function(samples, f)


@cocotb.test()
async def a_writer_slower_than_the_encoder_makes_gaps_without_stopping(dut):
    # メモリの書き込みが符号器の拍に追いつかない（LANES = 8 の 16 ch・800 MS/s = 毎周期 1 拍、LANES = 1 の RLE の毎サンプルの
    # 変化 = 4 周期に 1 拍にメモリの待ち 85 %）: 非同期 FIFO が受けない間の拍を捨て、FIFO が半分空いたら欠けを閉じる。PS は読み続ける
    if len(dut.lane_data) // 16 == 1:
        mem, lanes = await setup(dut, mem_stall=0.9)
        rle = 1
    else:
        mem, lanes = await setup(dut)
        rle = 0
    f = lambda i: i
    ps, reported = await run_stream(dut, mem, lanes, cycles=8_000 if lanes > 1 else 30_000, f=f, rle=rle, ev_every=1)
    assert int(dut.overrun.value) == 0
    samples, gaps = placed_samples(ps.pieces, rle, 0)
    assert len(gaps) >= 2
    assert len(gaps) < ps.la_beats / 50, (len(gaps), ps.la_beats)     # 欠けを閉じるのは FIFO が半分（256 拍）空いてから
    assert reported == ps.placer._records and ps.placer.unplaced_beats == 0
    check_function(samples, f)


@cocotb.test()
async def without_gaps_the_stream_stops_with_overrun_as_before(dut):
    mem, lanes = await setup(dut)
    f = lambda i: i
    ps, reported = await run_stream(dut, mem, lanes, cycles=40_000 if lanes == 1 else 3_000, f=f, gaps=0,
                                    stall=(lambda c: c >= 2_000) if lanes == 1 else (lambda c: False))
    assert int(dut.overrun.value) == 1
    samples, gaps = placed_samples(ps.pieces, 0, 0)
    assert not gaps and reported == 0
    check_function(samples, f)                             # 届いた分は欠けの無い先頭の部分


@cocotb.test()
async def a_gap_that_cannot_be_recorded_stops_with_overrun(dut):
    # イベントの記録が切れている（ev_enable = 0）と欠けの記録が書かれない: 欠けを黙って作らず、溢れとして止まる
    mem, lanes = await setup(dut)
    f = lambda i: i
    ps, reported = await run_stream(dut, mem, lanes, cycles=40_000 if lanes == 1 else 3_000, f=f, ev_enable=0,
                                    stall=(lambda c: c >= 2_000) if lanes == 1 else (lambda c: False))
    assert int(dut.overrun.value) == 1
    samples, gaps = placed_samples(ps.pieces, 0, 0)
    assert not gaps and reported == 0
    check_function(samples, f)
