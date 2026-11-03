"""event_byte_pack: 1 つの源（SWO・UART）のバイトを、1 件に最大 4 バイトと各バイトの時刻に詰める（無損失）。
別に書いた基準モデル（設計の切る条件そのもの）と毎周期突き合わせ、詰めた件をホストの解読（fprobe.events）で
バイトごとに戻して、受け付けたバイトと時刻が 1 つも変わらないこと（+ 数えた欠け）を確かめる。
バイトの時刻（in_time）は源が付けたもの（UART・SWO の受信は開始ビットの辺。受けた周期より前で、遅れは一定でない）。
周期の番号 c と別に与え、受けた周期より最大 LAG 周期前の、受けた順に増える値にする。
設計: docs/superpowers/specs/2026-11-01-bandwidth-observation-design.md 3.5、docs/superpowers/specs/2026-11-01-time-accuracy-design.md（R3）。"""
import random

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import FallingEdge, ReadOnly, RisingEdge

from fprobe.events import KIND_SWO, KIND_SWO_PACKED, PACK_DELTA_BITS, decode_events

GAP_LIMIT = 1 << PACK_DELTA_BITS      # この周期数以上空いたら、後のバイトは同じ件に入らない
MASK48 = (1 << 48) - 1


def word_of(packet):
    """詰めた件 [(時刻, バイト), ...] の 120 ビット（種類の 8 ビットの下）。"""
    t0 = packet[0][0]
    v = t0 | (len(packet) - 1) << 116
    for i, (_, b) in enumerate(packet):
        v |= b << (48 + 8 * i)
    for i in range(1, len(packet)):
        d = (packet[i][0] - packet[i - 1][0]) & MASK48
        assert d < GAP_LIMIT
        v |= d << (80 + 12 * (i - 1))
    return v


class Model:
    """周期ごとの入力（rst, enable, fire, data, 周期の番号 c, バイトの時刻 t, taken）から、次の周期の出力（out_valid, out_word, lost）。"""

    def __init__(self):
        self.acc = []                 # 詰めている途中の件 [(時刻, バイト)]
        self.last = None              # 最後にバイトを受けた周期
        self.ready = None             # 出すのを待つ件
        self.lost = 0

    def step(self, rst, enable, fire, data, c, t, taken):
        lost = 0
        free = self.ready is None or taken
        if taken:
            assert self.ready is not None
            self.ready = None
        # 切る条件（事実）: 4 バイト溜まった / 来たバイトの時刻と前のバイトの時刻の差が 12 ビットに収まらない /
        # 最後のバイトを受けてから GAP_LIMIT 周期以上たった（溜める時間の上限）
        fits = bool(self.acc) and 0 <= (t - self.acc[-1][0]) & MASK48 < GAP_LIMIT
        closing = (len(self.acc) == 4 or (bool(self.acc) and c - self.last >= GAP_LIMIT)
                   or (bool(self.acc) and fire and enable and not fits))
        if closing and free:
            self.ready, self.acc = self.acc, []
        if fire and enable:
            if not self.acc:
                self.acc, self.last = [(t, data)], c
            elif not closing:
                self.acc.append((t, data))
                self.last = c
            else:
                lost += 1                                   # 切るべき件がまだ出られない（出口が詰まっている）
        if not enable and not rst:                          # 記録を止めた: 溜めていたバイトは記録に残らない（数える）
            lost += len(self.acc) + (len(self.ready) if self.ready else 0)
            self.acc, self.ready = [], None
        if rst:
            self.acc, self.ready, lost = [], None, 0
        self.lost = lost
        return self.ready


async def cycle(dut, model, rst, enable, fire, data, t, taken_prob, rng, log, byte_time=None):
    """1 周期。t = 周期の番号、byte_time = そのバイトの時刻（None なら t = 受けた周期の時刻）。"""
    await FallingEdge(dut.clk)
    taken = int(not rst and dut.out_valid.value == 1 and rng.random() < taken_prob)
    bt = t if byte_time is None else byte_time
    dut.rst.value = rst
    dut.enable.value = enable
    dut.in_fire.value = fire
    dut.in_data.value = data
    dut.in_time.value = bt
    dut.out_taken.value = taken
    if taken:
        log["out"].append(int(dut.out_word.value))
    if fire and enable and not rst:
        log["in"].append((bt, data))
    exp = model.step(rst, enable, fire, data, t, bt, taken)
    await RisingEdge(dut.clk)
    await ReadOnly()
    if exp is None:
        assert dut.out_valid.value == 0, f"t={t}: unexpected packet"
    else:
        assert dut.out_valid.value == 1, f"t={t}: missing packet {exp}"
        got = int(dut.out_word.value)
        assert got == word_of(exp), f"t={t}: {got:030x} != {word_of(exp):030x}"
    assert int(dut.lost.value) == model.lost, f"t={t}: lost {int(dut.lost.value)} != {model.lost}"
    log["lost"] += model.lost


def gap(rng):
    r = rng.random()
    if r < 0.6:
        return rng.randint(1, 60)                           # 速い SWO（25 Mbit/s で 32〜40 周期）
    if r < 0.85:
        return rng.randint(GAP_LIMIT - 3, GAP_LIMIT + 3)    # 差の範囲の境
    return rng.randint(GAP_LIMIT + 4, 3 * GAP_LIMIT)


def unpack(words):
    data = b"".join(((KIND_SWO_PACKED << 120) | w).to_bytes(16, "little") for w in words)
    events = decode_events(data)
    assert all(e.kind == KIND_SWO for e in events)
    return [(e.time, e.data) for e in events]


async def start(dut):
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    for name in ("enable", "in_fire", "in_data", "out_taken"):
        getattr(dut, name).value = 0
    dut.rst.value = 1
    dut.in_time.value = 0


async def run(dut, seed, cycles, taken_prob, toggle, lag=0):
    """lag > 0: バイトの時刻を、受けた周期より 0〜lag 周期前の、受けた順に増える値にする（源が付けた時刻）。"""
    rng = random.Random(seed)
    model = Model()
    log = {"in": [], "out": [], "lost": 0}
    t = rng.getrandbits(47)
    for _ in range(3):
        t += 1
        await cycle(dut, model, 1, 0, 0, 0, t, taken_prob, rng, log)
    enable = 1
    next_fire = t + gap(rng)
    prev_bt = t - lag - 1
    for _ in range(cycles):
        t += 1
        if toggle and rng.random() < 0.0005:
            enable ^= 1
        fire = int(t >= next_fire)
        bt = None
        if fire:
            next_fire = t + gap(rng)
            if lag:
                bt = max(prev_bt + 1, t - rng.randint(0, lag))
                prev_bt = bt
                bt &= MASK48
        await cycle(dut, model, 0, enable, fire, rng.getrandbits(8), t & MASK48, taken_prob, rng, log, bt)
    for _ in range(3 * GAP_LIMIT):                         # 入力を止めて出し切る（最後の件は差の範囲を過ぎて出る）
        t += 1
        await cycle(dut, model, 0, enable, 0, 0, t & MASK48, 1.0, rng, log)
    return log


@cocotb.test()
async def matches_the_model_and_keeps_every_byte_and_time(dut):
    await start(dut)
    for seed, taken_prob, toggle, lag in ((1, 1.0, False, 0), (2, 0.3, False, 0), (3, 0.02, False, 0), (4, 0.5, True, 0),
                                          (5, 1.0, False, 3000), (6, 0.3, False, 200), (7, 0.5, True, 6000)):
        log = await run(dut, seed, 40000, taken_prob, toggle, lag)
        got = unpack(log["out"])
        assert len(got) + log["lost"] == len(log["in"]), (seed, len(got), log["lost"], len(log["in"]))
        if log["lost"] == 0:
            assert got == log["in"], seed                    # 無損失: バイトも時刻も元のまま
        else:                                               # 欠けた分を除いて、順も時刻も元のまま
            it = iter(log["in"])
            assert all(any(x == y for y in it) for x in got), seed


@cocotb.test()
async def a_gap_of_4095_joins_and_4096_starts_a_new_record(dut):
    await start(dut)
    rng = random.Random(0)
    model = Model()
    log = {"in": [], "out": [], "lost": 0}
    t = 100
    for _ in range(3):
        t += 1
        await cycle(dut, model, 1, 0, 0, 0, t, 1.0, rng, log)
    fires = {t + 10: 0x11, t + 10 + 4095: 0x22, t + 10 + 4095 + 4096: 0x33}
    for _ in range(4095 + 4096 + 10 + 2 * GAP_LIMIT):
        t += 1
        await cycle(dut, model, 0, 1, int(t in fires), fires.get(t, 0), t, 1.0, rng, log)
    assert [len(unpack([w])) for w in log["out"]] == [2, 1]
    assert unpack(log["out"]) == log["in"]


@cocotb.test()
async def bytes_close_in_arrival_but_far_apart_in_time_go_to_separate_records(dut):
    """時刻は源が付けたもの: 受けた周期が近くても、時刻の差が 12 ビットに収まらなければ件を分ける（丸めない）。"""
    await start(dut)
    rng = random.Random(0)
    model = Model()
    log = {"in": [], "out": [], "lost": 0}
    t = 20000
    for _ in range(3):
        t += 1
        await cycle(dut, model, 1, 0, 0, 0, t, 1.0, rng, log)
    fires = {t + 10: (0x11, t + 10 - 5000), t + 30: (0x22, t + 30), t + 50: (0x33, t + 50)}   # 1 番目は 5000 周期前の時刻
    for _ in range(80 + 2 * GAP_LIMIT):
        t += 1
        data, bt = fires.get(t, (0, None))
        await cycle(dut, model, 0, 1, int(t in fires), data, t, 1.0, rng, log, bt)
    assert [len(unpack([w])) for w in log["out"]] == [1, 2]
    assert unpack(log["out"]) == log["in"]


@cocotb.test()
async def a_lone_byte_leaves_as_soon_as_no_later_byte_can_join(dut):
    """1 バイトだけなら、差の範囲を過ぎた周期（4096 周期後）に件を切って出す。それより前には出さない。"""
    await start(dut)
    rng = random.Random(0)
    model = Model()
    log = {"in": [], "out": [], "lost": 0}
    t = 5000
    for _ in range(3):
        t += 1
        await cycle(dut, model, 1, 0, 0, 0, t, 1.0, rng, log)
    t += 1
    t_byte = t
    await cycle(dut, model, 0, 1, 1, 0x5A, t, 1.0, rng, log)
    seen = None
    for _ in range(GAP_LIMIT + 4):
        t += 1
        await cycle(dut, model, 0, 1, 0, 0, t, 0.0, rng, log)
        if seen is None and dut.out_valid.value == 1:
            seen = t
    assert seen == t_byte + GAP_LIMIT


@cocotb.test()
async def four_bytes_leave_without_waiting(dut):
    """4 バイト溜まったら、差の範囲を待たずに次の周期に件を切る。"""
    await start(dut)
    rng = random.Random(0)
    model = Model()
    log = {"in": [], "out": [], "lost": 0}
    t = 0
    for _ in range(3):
        t += 1
        await cycle(dut, model, 1, 0, 0, 0, t, 1.0, rng, log)
    times = [t + 5, t + 45, t + 85, t + 125]
    seen = None
    for _ in range(140):
        t += 1
        await cycle(dut, model, 0, 1, int(t in times), t & 0xFF, t, 0.0, rng, log)
        if seen is None and dut.out_valid.value == 1:
            seen = t
    assert seen == times[-1] + 1
