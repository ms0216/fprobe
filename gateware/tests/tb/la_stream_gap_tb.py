"""la_stream_gap（ロジアナの垂れ流しの欠けの区間）のテストベンチ。SPB_LOG = 3、Q_BITS = 10（2^9 = 512 サンプルで件を分ける）。
仕様: docs/superpowers/specs/2026-11-01-la-deep-fast-design.md §12（段 B）

入力の拍 i の中身に通し番号 i を入れ（RLE はレコードの長さも乱数で）、出た拍と欠けの記録から、捨てた拍が連続した 1 区間で、
記録の位置 b（その前に出した拍の数）と長さ q（捨てた拍のサンプルの数の和）が入力と一致することを確かめる。"""

import random

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, ReadOnly, RisingEdge

from fprobe import generated as g

SPB_LOG = 3
Q_SPLIT = 1 << 9


def rle_beat(index, lengths):
    """通し番号 index（値の欄）と 4 つの長さのレコードの拍。"""
    v = 0
    for r, n in enumerate(lengths):
        v |= ((n - 1) << 16 | (index + r) & 0xFFFF) << (32 * r)
    return v


def decode_record(v):
    return {"time": v & ((1 << 48) - 1), "beat": v >> 48 & 0xFFFFFFFF, "samples": v >> 80 & ((1 << 40) - 1),
            "kind": v >> 120 & 0xFF}


class Bench:
    def __init__(self, dut):
        self.dut = dut
        self.out = []                                         # ("beat", data) / ("gap", 記録)
        self.ready = lambda cycle: True
        self.cycle = 0

    async def run_clock(self):
        while True:
            await RisingEdge(self.dut.clk)
            self.cycle += 1
            self.dut.time.value = self.cycle

    async def sink(self):
        dut = self.dut
        while True:
            dut.out_ready.value = int(self.ready(self.cycle))
            await ReadOnly()
            if int(dut.out_valid.value) and int(dut.out_ready.value):
                data = int(dut.out_data.value)
                if int(dut.out_kind.value):
                    self.out.append(("gap", decode_record(data)))
                else:
                    self.out.append(("beat", data))
            await RisingEdge(dut.clk)


async def setup(dut):
    for name in ("run", "enable", "rle", "pack", "time", "hold", "in_valid", "in_data", "in_tag"):
        getattr(dut, name).value = 0
    dut.room.value = 1
    dut.ev_enable.value = 1
    dut.out_ready.value = 1
    dut.rst.value = 1
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    b = Bench(dut)
    cocotb.start_soon(b.run_clock())
    await ClockCycles(dut.clk, 3)
    dut.rst.value = 0
    await ClockCycles(dut.clk, 2)
    cocotb.start_soon(b.sink())
    return b


async def start(dut, *, enable=1, rle=0, pack=0):
    dut.enable.value, dut.rle.value, dut.pack.value = enable, rle, pack
    dut.run.value = 1
    await RisingEdge(dut.clk)


async def send(dut, beats, *, every=1, control=None):
    """beats（値の列）を every 周期ごとに 1 拍入れる。in_ready が 0 なら待つ（enable = 0 の形）。control(k) は k 拍目の前に呼ぶ。"""
    for k, v in enumerate(beats):
        if control is not None:
            control(k)
        dut.in_valid.value, dut.in_data.value, dut.in_tag.value = 1, v, k & 0xF
        while True:
            await ReadOnly()
            ok = int(dut.in_ready.value)
            await RisingEdge(dut.clk)
            if ok:
                break
        dut.in_valid.value = 0
        if every > 1:
            await ClockCycles(dut.clk, every - 1)


def check(out, lengths, sent):
    """出た拍と記録が、入力（sent 拍、拍 i のサンプルの数 lengths[i]）と一致するか。返す: 記録の列（同じ位置の続きはまとめる）。"""
    passed, gaps, pending = [], [], None
    for kind, item in out:
        if kind == "beat":
            if pending is not None:
                gaps.append(pending)
                pending = None
            passed.append(item & 0xFFFF if lengths.rle else item)
        else:
            assert item["kind"] == g.EVENT_KIND_LA_GAP
            assert item["beat"] == len(passed), (item, len(passed))
            assert 0 < item["samples"] < 1 << 40
            if pending is not None and pending["beat"] == item["beat"]:
                assert pending["samples"] >= Q_SPLIT          # 分けたのは欄の半分に達したとき
                pending = {**pending, "samples": pending["samples"] + item["samples"]}
            else:
                if pending is not None:
                    gaps.append(pending)
                pending = item
    if pending is not None:
        gaps.append(pending)
    # 出た拍の通し番号の列: 欠けの位置で、捨てた拍の分だけ飛ぶ。飛んだ拍のサンプルの数の和 = 記録の q
    index = lengths.index_of
    expected = 0
    gi = 0
    for pos, value in enumerate(passed):
        i = index(value)
        if gi < len(gaps) and gaps[gi]["beat"] == pos:
            dropped = range(expected, i)
            assert len(dropped) > 0
            assert gaps[gi]["samples"] == sum(lengths[j] for j in dropped), (gaps[gi], list(dropped))
            gi += 1
        else:
            assert i == expected, (pos, i, expected)
        expected = i + 1
    assert expected <= sent
    return gaps, passed


class Raw:
    rle = False

    def __init__(self, pack):
        self.n = 1 << (SPB_LOG + pack)

    def __getitem__(self, i):
        return self.n

    @staticmethod
    def index_of(value):
        return value


class Rle:
    rle = True

    def __init__(self, seed, count):
        rng = random.Random(seed)
        self.records = [[rng.choice((1, 2, 7, 300, 65536)) for _ in range(4)] for _ in range(count)]

    def __getitem__(self, i):
        return sum(self.records[i])

    @staticmethod
    def index_of(value):
        return value // 4

    def beats(self):
        return [rle_beat(4 * i, r) for i, r in enumerate(self.records)]


@cocotb.test()
async def without_enable_the_beats_pass_through_with_back_pressure(dut):
    b = await setup(dut)
    b.ready = lambda c: c % 3 != 0
    await start(dut, enable=0)
    await send(dut, list(range(200)))
    await ClockCycles(dut.clk, 5)
    assert [x for _, x in b.out] == list(range(200)) and all(k == "beat" for k, _ in b.out)
    assert int(dut.gaps.value) == 0 and int(dut.lost.value) == 0


@cocotb.test()
async def raw_beats_that_cannot_go_out_become_one_recorded_gap(dut):
    b = await setup(dut)
    await start(dut, pack=1)                                  # 1 拍 = 16 サンプル

    def control(k):
        dut.hold.value = int(100 <= k < 160 or 400 <= k < 405)
    await send(dut, list(range(600)), control=control)
    dut.hold.value = 0
    await ClockCycles(dut.clk, 5)
    gaps, passed = check(b.out, Raw(1), 600)
    assert [(x["beat"], x["samples"] // 16) for x in gaps] == [(99, 61), (338, 6)], gaps   # hold は段 A の後の拍（1 つ前の拍）から効く
    records = [x for k, x in b.out if k == "gap"]                # 976 サンプルの欠けは 512 で 2 件に分かれる
    assert int(dut.gaps.value) == len(records) == 3 and int(dut.last_beat.value) == 338
    assert int(dut.gap_open.value) == 0 and int(dut.lost.value) == 0
    assert dut.in_ready.value == 1


@cocotb.test()
async def a_downstream_that_does_not_take_makes_gaps_that_close_only_with_room(dut):
    b = await setup(dut)
    b.ready = lambda c: (c // 50) % 3 != 2                   # 受け口が時々 50 周期受けない
    room_off = {"until": 0}

    def control(k):
        dut.room.value = int(k >= room_off["until"])
        if k == 300:
            room_off["until"] = 340                         # 空きが無い間は、受け口が空いても閉じない
    await start(dut, pack=0)
    await send(dut, list(range(1000)), control=control)
    dut.room.value = 1
    await ClockCycles(dut.clk, 60)
    gaps, passed = check(b.out, Raw(0), 1000)
    assert len(gaps) >= 3 and passed
    assert int(dut.gaps.value) >= len(gaps)


@cocotb.test()
async def a_long_gap_is_split_into_records_at_the_same_position(dut):
    b = await setup(dut)
    await start(dut, pack=0)                                  # 1 拍 = 8 サンプル: 2^9 で分ける = 64 拍ごと

    def control(k):
        dut.hold.value = int(50 <= k < 350)
    await send(dut, list(range(400)), control=control)
    dut.hold.value = 0
    await ClockCycles(dut.clk, 5)
    records = [x for k, x in b.out if k == "gap"]
    assert len(records) >= 4 and all(x["beat"] == 49 for x in records)
    gaps, _ = check(b.out, Raw(0), 400)
    assert [(x["beat"], x["samples"]) for x in gaps] == [(49, 301 * 8)]


@cocotb.test()
async def rle_gaps_carry_the_sum_of_the_dropped_record_lengths(dut):
    b = await setup(dut)
    rle = Rle(7, 500)
    b.ready = lambda c: (c // 37) % 4 != 3
    await start(dut, rle=1)

    def control(k):
        dut.hold.value = int(200 <= k < 260)
    await send(dut, rle.beats(), every=2, control=control)
    dut.hold.value = 0
    await ClockCycles(dut.clk, 60)
    gaps, _ = check(b.out, rle, 500)
    assert any(x["beat"] <= 200 and x["samples"] >= 60 * 4 for x in gaps), gaps


@cocotb.test()
async def the_record_time_is_when_the_first_beat_was_dropped(dut):
    b = await setup(dut)
    await start(dut, pack=0)
    when = {}

    def control(k):
        dut.hold.value = int(20 <= k < 30)
        if k == 20:
            when["cycle"] = b.cycle
    await send(dut, list(range(60)), control=control)
    dut.hold.value = 0
    await ClockCycles(dut.clk, 5)
    (record,) = [x for k, x in b.out if k == "gap"]
    # 拍 20 を入れた周期の後、段 A を経て段 B が捨てると決めた周期の time（入れた周期の 1〜2 周期後）
    assert when["cycle"] <= record["time"] <= when["cycle"] + 2, (record, when)


@cocotb.test()
async def a_gap_that_cannot_be_recorded_stops_with_lost(dut):
    b = await setup(dut)
    dut.ev_enable.value = 0
    await start(dut, pack=0)

    def control(k):
        dut.hold.value = int(30 <= k < 40)
    await send(dut, list(range(100)), control=control)
    await ClockCycles(dut.clk, 5)
    assert int(dut.lost.value) == 1
    assert [x for k, x in b.out] == list(range(29)) and all(k == "beat" for k, _ in b.out)
    assert int(dut.gaps.value) == 0
    # 次の開始で消える
    dut.run.value = 0
    await RisingEdge(dut.clk)
    dut.ev_enable.value = 1
    await start(dut, pack=0)
    await ClockCycles(dut.clk, 2)
    assert int(dut.lost.value) == 0


@cocotb.test()
async def a_gap_open_when_the_run_stops_is_not_recorded(dut):
    b = await setup(dut)
    await start(dut, pack=0)

    def control(k):
        dut.hold.value = int(k >= 40)
    await send(dut, list(range(60)), control=control)
    dut.run.value = 0
    dut.hold.value = 0
    await ClockCycles(dut.clk, 10)
    assert [x for k, x in b.out] == list(range(39))
    assert int(dut.gaps.value) == 0 and int(dut.gap_open.value) == 1
