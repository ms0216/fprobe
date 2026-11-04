"""la_deep_buffer を組む小さな部品のテストベンチ（どれも 1 つの entity を toplevel にする。どの部品かは環境変数 PART）。

- cdc_request: 要求ごとの値が、通し番号つきで、ビットが混ざらず届く。重なった要求は最新の 1 つだけ届く。
- hold_one: 1 件の保持。出すのと同じ周期に次を受け、空く前に来たものを lost に数える（enable の 0→1 で 0 に）。
- la_run_cdc: run の 0→1 ごとに番号と設定が写り、mem は 1 度だけ始め、run の 1→0 で 1 度だけ止める。近い 2 つの辺でも
  始めと止めを取り違えない（届く前に止められた流れは、始めて次の周期に止める）。
- la_write_track: 応答の来ていない書き（新しい側の wr_pending 個）を種類ごとに引く。流れの開始の前のロジアナの書きは引かない。
- mem_cmd_arb: 両方が求め続けると交互に通し、片方だけなら（1 周期の切り替えの後）毎周期そちらを通す。
"""

import os
import random

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, ReadOnly, RisingEdge, Timer

PART = os.environ.get("PART", "")
SAME_CLOCK = os.environ.get("CDC_SAME_CLOCK") == "1"


def only(part):
    return cocotb.test(skip=PART != part)


# --- cdc_request ---------------------------------------------------------------------------------------------------

@only("cdc_request")
async def requests_arrive_whole_and_the_last_one_always(dut):
    cocotb.start_soon(Clock(dut.src_clk, 16, unit="ns").start())
    cocotb.start_soon(Clock(dut.dst_clk, 10 if not SAME_CLOCK else 16, unit="ns").start())
    dut.req.value = 0
    dut.value.value = 0
    dut.src_rst.value = 1
    dut.dst_rst.value = 1
    await ClockCycles(dut.src_clk, 4)
    dut.src_rst.value = 0
    dut.dst_rst.value = 0
    got = []

    async def watch():
        while True:
            await RisingEdge(dut.dst_clk)
            await ReadOnly()
            if int(dut.dst_new.value):
                got.append((int(dut.dst_seq.value), int(dut.dst_value.value)))

    cocotb.start_soon(watch())
    await ClockCycles(dut.src_clk, 10)
    assert got == [], "リセットの後の番号 0 を要求とみなした"
    rng = random.Random(5)
    sent = []
    for n in range(300):
        await RisingEdge(dut.src_clk)
        if rng.random() < (0.5 if n < 150 else 0.05):      # 前半は受け渡しより速く、後半はゆっくり
            v = rng.getrandbits(32)
            sent.append(v)
            dut.req.value = 1
            dut.value.value = v
        else:
            dut.req.value = 0
            dut.value.value = rng.getrandbits(32)          # 要求でない周期の値は届かない
    await RisingEdge(dut.src_clk)
    dut.req.value = 0
    await ClockCycles(dut.src_clk, 30)
    assert int(dut.src_seq.value) == len(sent) % 256
    assert got[-1] == (len(sent) % 256, sent[-1])
    assert len(sent) < 256
    seqs = [s for s, _ in got]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
    for seq, v in got:
        assert sent[seq - 1] == v, "番号と値の組が揃わない"


# --- hold_one ------------------------------------------------------------------------------------------------------

@only("hold_one")
async def holds_one_and_counts_what_it_drops(dut):
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    for s in (dut.enable, dut.in_valid, dut.in_data, dut.out_ready):
        s.value = 0
    dut.rst.value = 1
    await ClockCycles(dut.clk, 3)
    dut.rst.value = 0
    dut.enable.value = 1
    rng = random.Random(9)
    sent, out, model_lost, full = [], [], 0, False
    for n in range(2000):
        await RisingEdge(dut.clk)
        in_v, ready = int(rng.random() < 0.6), int(rng.random() < 0.5)
        dut.in_valid.value, dut.in_data.value, dut.out_ready.value = in_v, n, ready
        await ReadOnly()
        valid = int(dut.out_valid.value)
        assert valid == int(full), n
        if valid and ready:
            out.append(int(dut.out_data.value))
        if not full or ready:
            full = bool(in_v)
            if in_v:
                sent.append(n)
        elif in_v:
            model_lost += 1
    await RisingEdge(dut.clk)
    dut.in_valid.value = 0
    await ReadOnly()
    assert int(dut.lost.value) == model_lost and model_lost > 0
    assert out == sent[:len(out)]
    await RisingEdge(dut.clk)
    dut.enable.value = 0
    await RisingEdge(dut.clk)
    dut.enable.value = 1
    await ClockCycles(dut.clk, 2)
    await ReadOnly()
    assert int(dut.lost.value) == 0, "enable の 0→1 で 0 に戻らない"


# --- la_run_cdc ----------------------------------------------------------------------------------------------------

@only("la_run_cdc")
async def each_run_starts_once_and_stops_once(dut):
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    cocotb.start_soon(Clock(dut.mem_clk, 10 if SAME_CLOCK else 7, unit="ns").start())
    for s in (dut.run, dut.stream, dut.ps_read, dut.gaps, dut.post_beats):
        s.value = 0
    dut.rst.value = 1
    dut.mem_rst.value = 1
    await ClockCycles(dut.clk, 4)
    dut.rst.value = 0
    dut.mem_rst.value = 0
    events = []

    async def watch():
        while True:
            await RisingEdge(dut.mem_clk)
            await ReadOnly()
            if int(dut.m_start.value):
                events.append(("start", int(dut.m_epoch.value), int(dut.m_stream.value), int(dut.m_ps_read.value),
                               int(dut.m_gaps.value), int(dut.m_post.value)))
            if int(dut.m_stop.value):
                events.append(("stop", int(dut.m_epoch.value)))

    cocotb.start_soon(watch())
    await ClockCycles(dut.clk, 20)
    assert events == [], "リセットの後に始めた"
    rng = random.Random(3)
    expect = []
    for n in range(1, 40):
        stream, ps, gaps, post = rng.getrandbits(1), rng.getrandbits(1), rng.getrandbits(1), rng.getrandbits(8)
        await RisingEdge(dut.clk)
        dut.run.value = 1
        dut.stream.value, dut.ps_read.value, dut.gaps.value, dut.post_beats.value = stream, ps, gaps, post
        await RisingEdge(dut.clk)
        await ReadOnly()
        assert int(dut.epoch.value) == n and int(dut.run_q.value) == 1
        assert int(dut.ps_last.value) == (ps & stream)
        await ClockCycles(dut.clk, rng.choice([1, 2, 3, 30]))   # 短い run: mem が見る前に止められる
        dut.run.value = 0
        dut.stream.value = dut.ps_read.value = dut.gaps.value = 0   # 設定は辺の時点の値
        expect.append((n, stream, ps & stream, gaps, post))
        await ClockCycles(dut.clk, rng.choice([1, 2, 40]))
    await ClockCycles(dut.clk, 60)
    starts = [e for e in events if e[0] == "start"]
    stops = [e[1] for e in events if e[0] == "stop"]
    # mem が見る前に次の流れが始まった流れは飛ばす（始めない）。始めた流れは 1 度だけで、設定はその流れのもの。
    # 止めは始めた流れの後に高々 1 度（次の流れの開始が前の流れを終える）。最後の流れは必ず止める
    assert starts[-1][1:] == expect[-1], "最後の流れの番号・設定が届かない"
    for s in starts:
        assert s[1:] == expect[s[1] - 1]
    started = [s[1] for s in starts]
    assert started == sorted(set(started))
    assert len(started) > 10 and len(started) < len(expect) + 1
    assert stops == sorted(set(stops)) and set(stops) <= set(started) and stops[-1] == started[-1]
    for i, e in enumerate(events):
        if e[0] == "stop":
            prior = [x for x in events[:i] if x[0] == "start"]
            assert prior and prior[-1][1] == e[1], "止めが今の流れのものでない"


# --- la_write_track ------------------------------------------------------------------------------------------------

@only("la_write_track")
async def pending_writes_are_taken_off_by_kind(dut):
    addr_bits = len(dut.la_ptr)
    la_size = 15 << (addr_bits - 4)                        # mem_ring_pkg: ロジアナのリングは 15/16
    ev_bits = len(dut.ev_pos)
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    for s in (dut.start, dut.wr_fire, dut.wr_event, dut.wr_pending, dut.la_total, dut.la_ptr, dut.ev_pos):
        s.value = 0
    dut.rst.value = 1
    await ClockCycles(dut.clk, 3)
    dut.rst.value = 0
    rng = random.Random(11)
    history = []                                           # (種類, この流れか) の新しい順
    started = False
    for n in range(3000):
        await RisingEdge(dut.clk)
        start = int(rng.random() < 0.01)
        fire = int(rng.random() < 0.6)
        ev = rng.getrandbits(1)
        dut.start.value, dut.wr_fire.value, dut.wr_event.value = start, fire, ev
        pending = rng.randrange(0, min(len(history), 15) + 1)
        total, ptr, epos = rng.getrandbits(32), rng.randrange(la_size), rng.getrandbits(ev_bits)
        dut.wr_pending.value, dut.la_total.value, dut.la_ptr.value, dut.ev_pos.value = pending, total, ptr, epos
        await ReadOnly()
        la_p = sum(1 for k, own in history[:pending] if not k and own)
        ev_p = sum(1 for k, _ in history[:pending] if k)
        assert int(dut.la_safe.value) == (total - la_p) % (1 << 32), n
        assert int(dut.la_ptr_safe.value) == (ptr - la_p) % la_size, n
        assert int(dut.ev_safe.value) == (epos - ev_p) % (1 << ev_bits), n
        if start:
            history = [(k, False) for k, _ in history]
        if fire:
            history.insert(0, (ev, not ev and not start))
        history = history[:15]


# --- mem_cmd_arb ---------------------------------------------------------------------------------------------------

@only("mem_cmd_arb")
async def both_requesting_alternate(dut):
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    for s in (dut.a_valid, dut.a_write, dut.a_addr, dut.a_data, dut.b_valid, dut.b_write, dut.b_addr, dut.b_data,
              dut.cmd_ready):
        s.value = 0
    dut.rst.value = 1
    await ClockCycles(dut.clk, 3)
    dut.rst.value = 0
    dut.a_addr.value, dut.a_data.value, dut.a_write.value = 1, 0xA, 0
    dut.b_addr.value, dut.b_data.value, dut.b_write.value = 2, 0xB, 1
    dut.cmd_ready.value = 1
    dut.a_valid.value = 1
    dut.b_valid.value = 1
    seq = []
    for _ in range(20):
        await RisingEdge(dut.clk)
        await ReadOnly()
        assert int(dut.a_ready.value) + int(dut.b_ready.value) == 1
        seq.append("a" if int(dut.a_ready.value) else "b")
        side = seq[-1]
        assert int(dut.cmd_valid.value) == 1
        assert int(dut.cmd_addr.value) == (1 if side == "a" else 2)
        assert int(dut.cmd_write.value) == (0 if side == "a" else 1)
        assert int(dut.cmd_data.value) == (0xA if side == "a" else 0xB)
    assert all(x != y for x, y in zip(seq, seq[1:])), seq
    await RisingEdge(dut.clk)
    dut.b_valid.value = 0
    seen = []
    for _ in range(10):
        await RisingEdge(dut.clk)
        await ReadOnly()
        seen.append(int(dut.cmd_valid.value) and int(dut.a_ready.value))
    assert seen[1:] == [1] * 9, seen                    # 切り替えの 1 周期の後は毎周期 a
    await RisingEdge(dut.clk)
    dut.cmd_ready.value = 0                              # 受けられない間は交代しない
    dut.b_valid.value = 1
    await ClockCycles(dut.clk, 3)
    await ReadOnly()
    sel = int(dut.a_ready.value), int(dut.b_ready.value)
    assert sel == (0, 0)
    assert int(dut.cmd_valid.value) == 1
