"""la_deep_buffer（ロジアナの深いバッファ）のテストベンチ。ADDR_BITS = 10（容量 1024 拍）、sys 100 MHz・mem 99.56 MHz。
入力 probes には毎周期 1 ずつ増えるカウンタを入れ、読み出したバイト列をホストの復号（fprobe.la）で確かめる。"""

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, FallingEdge, ReadOnly, RisingEdge

from fprobe.la import decode_beats, trigger_sample
from tests.models.mem_port import MemPortModel

CAP = 1024 - 64                         # ロジアナのリング（容量 1024 拍の 15/16）
EV_CAP = 64


async def setup(dut, mem_beats=CAP):
    """mem_beats: メモリのモデルの大きさ（既定はロジアナのリングだけ。イベントのリングも使うなら 1024）。"""
    for name in ("rst", "mem_rst", "probes", "ext_trigger", "time", "rate", "trig_mask", "trig_value", "trig_rise",
                 "trig_fall", "run", "rle", "stream", "post_beats", "pretrigger", "read", "read_first", "read_count", "ev_enable", "ev_valid",
                 "ev_data", "read_region"):
        getattr(dut, name).value = 0
    dut.out_ready.value = 1
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    cocotb.start_soon(Clock(dut.mem_clk, 10040, unit="ps").start())
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


async def wait_for(dut, name, timeout=200_000):
    for _ in range(timeout):
        await RisingEdge(dut.clk)
        await ReadOnly()
        if getattr(dut, name).value == 1:
            await RisingEdge(dut.clk)
            return
    raise TimeoutError(name)


async def read_bytes(dut, first, count):
    dut.read_first.value, dut.read_count.value = first, count
    dut.read.value = 1
    await RisingEdge(dut.clk)
    dut.read.value = 0
    out = bytearray()
    while len(out) < 16 * count:
        await RisingEdge(dut.clk)
        await ReadOnly()
        if dut.out_valid.value == 1:
            out.append(int(dut.out_data.value))
    return bytes(out)


async def capture(dut, *, rle, rate, post, value, mask=0xFFFF):
    dut.rle.value, dut.rate.value, dut.post_beats.value = int(rle), rate, post
    dut.trig_mask.value, dut.trig_value.value = mask, value                # カウンタが value になったら
    dut.run.value = 1
    await RisingEdge(dut.clk)
    await ReadOnly()
    assert dut.capturing.value == 1 and dut.done.value == 0
    await wait_for(dut, "done")
    assert dut.overrun.value == 0
    beat, slot, written = int(dut.trigger_beat.value), int(dut.trigger_slot.value), int(dut.written.value)
    dut.run.value = 0
    await ClockCycles(dut.clk, 10)
    assert dut.done.value == 1                                              # 止めても結果は残る
    return beat, slot, written


@cocotb.test()
async def raw_capture_wraps_and_reads_back_in_time_order(dut):
    await setup(dut)
    beat, slot, written = await capture(dut, rle=False, rate=0, post=100, value=0x3000)   # 約 12000 サンプル後
    assert written == CAP                                                   # リングを回った
    pre = CAP - 101
    first = (beat - pre) % CAP
    data = await read_bytes(dut, first, CAP)
    samples = decode_beats(data, rle=False)
    t = trigger_sample(data, False, pre, slot)
    assert samples[t] == 0x3000
    assert all((b - a) & 0xFFFF == 1 for a, b in zip(samples, samples[1:]))   # 抜けも重複もない


@cocotb.test()
async def rle_capture_of_a_slow_counter(dut):
    await setup(dut)
    # 4 周期ごとのサンプル（rate = 3）のカウンタ: 変化点の圧縮は効かないが値は正しく戻る
    beat, slot, written = await capture(dut, rle=True, rate=3, post=10, value=0x2000, mask=0xFF00)
    first = (beat - min(written - 11, 50)) % CAP
    count = (beat - first) % CAP + 11
    data = await read_bytes(dut, first, count)
    samples = decode_beats(data, rle=True)
    t = trigger_sample(data, True, (beat - first) % CAP, slot)
    assert samples[t] & 0xFF00 == 0x2000 and samples[t - 1] & 0xFF00 == 0x1F00          # 初めて上位が 0x20
    assert all((b - a) & 0xFFFF == 4 for a, b in zip(samples, samples[1:]))


@cocotb.test()
async def events_are_recorded_beside_the_analyzer(dut):
    await setup(dut)
    dut.ev_enable.value = 1
    await ClockCycles(dut.clk, 5)
    sent = []

    async def events():
        i = 0
        while True:
            await ClockCycles(dut.clk, 37)
            dut.ev_valid.value, dut.ev_data.value = 1, 0xE0000 + i
            sent.append(0xE0000 + i)
            await RisingEdge(dut.clk)
            dut.ev_valid.value = 0
            i += 1

    task = cocotb.start_soon(events())
    await capture(dut, rle=False, rate=0, post=50, value=0x1800)               # ロジアナも同時に（100 MS/s の RAW）
    await ClockCycles(dut.clk, 20)
    await ReadOnly()
    assert dut.mem_active.value == 1                 # ロジアナが止まってもイベントの記録中は mem_port を使う
    await RisingEdge(dut.clk)
    task.cancel()
    await ClockCycles(dut.clk, 100)
    written = int(dut.ev_written.value)
    assert written == len(sent) and int(dut.ev_lost.value) == 0
    dut.read_region.value = 1
    data = await read_bytes(dut, (written - EV_CAP) % EV_CAP, EV_CAP)
    got = [int.from_bytes(data[i:i + 16], "little") for i in range(0, len(data), 16)]
    assert got == sent[-EV_CAP:]


@cocotb.test()
async def back_to_back_events_are_not_lost(dut):
    """イベントは連続する周期に来ることがある（DAP の転送の直後に SWO のバイト等）。保持レジスタは出すのと同じ周期に
    次を受ける（2026-10-18 に実機で、1 周期おきにしか受けず SWO のバイトが時々 1 つ欠けた）。"""
    await setup(dut)
    dut.ev_enable.value = 1
    await ClockCycles(dut.clk, 5)
    sent = []
    for burst in range(4):
        for i in range(3):
            await RisingEdge(dut.clk)
            dut.ev_valid.value, dut.ev_data.value = 1, 0xB0000 + burst * 16 + i
            sent.append(0xB0000 + burst * 16 + i)
        await RisingEdge(dut.clk)
        dut.ev_valid.value = 0
        await ClockCycles(dut.clk, 20)
    await ClockCycles(dut.clk, 100)
    assert int(dut.ev_lost.value) == 0 and int(dut.ev_written.value) == len(sent)
    dut.read_region.value = 1
    data = await read_bytes(dut, 0, len(sent))
    got = [int.from_bytes(data[i:i + 16], "little") for i in range(0, len(data), 16)]
    assert got == sent


async def stream_until(dut, *, beats, stall=None):
    """ストリームのバイト列を beats 拍分受け取ってから止め、出し切るまで受け取る。stall(n) が真の周期は受け取らない。
    受け渡しは立ち上がりの前（ReadOnly）の valid と ready で数える（read_range と同じ）。"""
    out = bytearray()
    cycle = 0
    stopped = False
    while True:
        dut.out_ready.value = 0 if stall and stall(cycle) else 1
        await ReadOnly()
        if dut.out_valid.value == 1 and dut.out_ready.value == 1:
            out.append(int(dut.out_data.value))
        done = stopped and dut.reading.value == 0
        await RisingEdge(dut.clk)
        cycle += 1
        if not stopped and len(out) >= 16 * beats:
            dut.run.value = 0                                     # 書き込みを止め、未読を出し切る
            stopped = True
        elif done:
            break                                                 # reading が 0 = 数えた拍を全部出し終えた
        elif not stopped and dut.overrun.value == 1 and dut.reading.value == 0:
            raise AssertionError(f"ストリームが溢れて終わった（{len(out)} バイト、{cycle} 周期）")
        if cycle > 2_000_000:
            raise TimeoutError("stream")
    dut.out_ready.value = 1
    for _ in range(200):
        await ReadOnly()
        assert dut.out_valid.value == 0, "reading が 0 になった後に拍が出た"
        await RisingEdge(dut.clk)
    return bytes(out)


@cocotb.test()
async def stream_survives_host_stalls_without_gaps(dut):
    # 8 周期ごとのサンプル（1 拍 = 64 周期）を、リング（960 拍）を 2 周以上流す。受け手は時々止まり、
    # 途中で 20000 周期（未読が約 300 拍）止まる。欠けも重複も無く、溢れない
    await setup(dut)
    dut.rle.value, dut.rate.value, dut.stream.value = 0, 7, 1
    dut.trig_mask.value, dut.trig_value.value = 0, 0                # トリガは常に成立するが、ストリームでは止まらない
    dut.run.value = 1
    data = await stream_until(dut, beats=2200,
                              stall=lambda n: (n // 37) % 5 == 0 or 60_000 <= n < 80_000)
    samples = decode_beats(data, False)
    assert len(samples) >= 2200 * 8
    # 先頭の 1 サンプルは開始直後の同期化段の初期値なので見ない
    gaps = [(i, a, b) for i, (a, b) in enumerate(zip(samples[1:], samples[2:])) if (b - a) & 0xFFFF != 8]
    assert not gaps, (len(samples), gaps[:5])
    assert dut.overrun.value == 0
    assert int(dut.stream_beats.value) * 8 == len(samples) or int(dut.stream_beats.value) * 8 >= len(samples) - 64


@cocotb.test()
async def stream_stops_and_reports_overrun_when_the_host_falls_behind(dut):
    # 受け手が止まったまま（100 MS/s の RAW = 8 周期に 1 拍）: 未読がリング - 256 拍を超えると書き込みを止めて
    # overrun。それまでの分は欠けなく受け取れる
    await setup(dut)
    dut.rle.value, dut.rate.value, dut.stream.value = 0, 0, 1
    dut.run.value = 1
    dut.out_ready.value = 0
    await wait_for(dut, "overrun", timeout=100_000)
    data = await stream_until(dut, beats=1)
    samples = decode_beats(data, False)
    gaps = [(i, a, b) for i, (a, b) in enumerate(zip(samples[1:], samples[2:])) if (b - a) & 0xFFFF != 1]
    assert CAP - 256 <= len(samples) // 8 <= CAP and not gaps, (len(samples) // 8, gaps[:5])



@cocotb.test()
async def trigger_is_ignored_until_the_pretrigger_samples_are_recorded(dut):
    # 条件はすぐ成立する（マスク 0）が、pretrigger = 1000 サンプルまでは受け付けない
    await setup(dut)
    dut.pretrigger.value = 1000
    beat, slot, written = await capture(dut, rle=False, rate=0, post=10, value=0, mask=0)
    assert beat * 8 + slot >= 1000                                      # トリガは 1000 サンプル目以降


@cocotb.test()
async def post_trigger_of_zero_and_one_beat_stop_at_the_right_beat(dut):
    """post_beats = 0 はトリガの拍で止まり、1 はその次の拍で止まる（written = トリガの拍の番号 + 1 + post）。"""
    await setup(dut)
    for post in (0, 1, 2):
        dut.pretrigger.value = 24                                           # 3 拍目でトリガ（条件は常に成立）
        beat, slot, written = await capture(dut, rle=False, rate=0, post=post, value=0, mask=0)
        assert (beat, slot) == (3, 0) and written == beat + 1 + post, (post, beat, slot, written)
        data = await read_bytes(dut, 0, written)
        samples = decode_beats(data, rle=False)
        assert trigger_sample(data, False, beat, slot) == 24 and len(samples) == 8 * written
        await ClockCycles(dut.clk, 50)


@cocotb.test()
async def pretrigger_samples_are_recorded_exactly(dut):
    """pretrigger = P のとき、条件が常に成立していても最初の P サンプルは受け付けず、P 番目（0 起点）のサンプルがトリガになる
    （トリガ前に P サンプル残る）。P が 8 の倍数の前後（拍の境目）でも同じ。"""
    await setup(dut)
    for pre in (0, 1, 7, 8, 9, 63, 64):
        dut.pretrigger.value = pre
        beat, slot, written = await capture(dut, rle=False, rate=0, post=4, value=0, mask=0)
        data = await read_bytes(dut, 0, written)
        t = trigger_sample(data, False, beat, slot)
        assert t == pre, (pre, t)
        await ClockCycles(dut.clk, 50)
