"""sampler_match のテストベンチ: サンプラの標本（番地・値・ACK・か所と語の番号）を入れ、一致に入った瞬間だけ fire が 1 周期
出ること、数・時刻（発火した標本と、同じ番号の直前の標本）を確かめる。
仕様 docs/superpowers/specs/2026-10-23-sampler-match-trigger-design.md、
番地のマスクと番号ごとの状態は docs/superpowers/specs/2026-10-29-halt-watch-design.md §3.1"""

import cocotb
from cocotb.triggers import FallingEdge, ReadOnly, RisingEdge

from tests.tb.common import start_clock_and_reset

DHCSR, S_HALT = 0xE000EDF0, 1 << 17
OK, WAIT = 0b001, 0b010


class Bench:
    def __init__(self, dut):
        self.dut = dut
        self.t = 0
        self.fires = []                                  # fire が出た周期（試験の時計 t）
        self.stamp = None                                # None: 標本の時刻 = 試験の時計。値: 次の標本の時刻（R6 の試験）

    async def clock(self):
        d = self.dut
        while True:
            await FallingEdge(d.clk)
            d.sample_time.value = self.t if self.stamp is None else self.stamp
            await RisingEdge(d.clk)
            await ReadOnly()
            if d.fire.value == 1:
                self.fires.append(self.t)
            self.t += 1

    async def sample(self, addr, data, ack=OK, gap=3, index=0):
        """標本を 1 つ入れ、回路がそれを受けた周期の time_count を返す。index = サンプラのか所 × 16 + 語。"""
        d = self.dut
        await RisingEdge(d.clk)
        d.sample_valid.value, d.sample_addr.value, d.sample_data.value, d.sample_ack.value = 1, addr, data, ack
        d.sample_index.value = index
        await RisingEdge(d.clk)
        taken = int(d.sample_time.value)                 # この立ち上がりで回路が取り込んだ標本の時刻
        d.sample_valid.value = 0
        for _ in range(gap):
            await RisingEdge(d.clk)
        return taken


async def setup(dut, mask=S_HALT, value=S_HALT, addr=DHCSR, addr_mask=0xFFFFFFFF):
    dut.enable.value = 0
    dut.sample_valid.value = 0
    dut.sample_addr.value, dut.sample_data.value, dut.sample_ack.value, dut.sample_index.value = 0, 0, 0, 0
    dut.match_addr.value, dut.match_mask.value, dut.match_value.value = addr, mask, value
    dut.match_addr_mask.value = addr_mask
    dut.once.value = 0
    dut.after_start.value, dut.first_sample.value, dut.start.value, dut.start_time.value = 0, 0, 0, 0
    dut.sample_time.value = 0
    await start_clock_and_reset(dut)
    b = Bench(dut)
    cocotb.start_soon(b.clock())
    await RisingEdge(dut.clk)
    dut.enable.value = 1
    await RisingEdge(dut.clk)
    return b


@cocotb.test()
async def fires_only_when_entering(dut):
    b = await setup(dut)
    await b.sample(DHCSR, 0x00030000 & ~S_HALT)          # 走っている
    await b.sample(DHCSR, 0x0)
    assert b.fires == [] and dut.count.value == 0
    t_prev = await b.sample(DHCSR, 0x0)
    t_fire = await b.sample(DHCSR, S_HALT | 1)            # 止まった → 入った瞬間
    await b.sample(DHCSR, S_HALT)                         # 止まったまま: 出さない
    assert len(b.fires) == 1 and dut.count.value == 1 and dut.matched.value == 1
    fire_t, prev_t = int(dut.fire_time.value), int(dut.prev_time.value)
    assert (fire_t, prev_t) == (t_fire, t_prev)           # 発火した標本と、その直前の（不一致の）標本の時刻
    assert dut.seen.value == 5
    await b.sample(DHCSR, 0)                              # 走り出した
    await b.sample(DHCSR, S_HALT)                         # もう一度止まった
    assert len(b.fires) == 2 and dut.count.value == 2


@cocotb.test()
async def first_sample_does_not_fire(dut):
    b = await setup(dut)
    await b.sample(DHCSR, S_HALT)                         # 有効にした最初の標本がすでに一致: 入った瞬間は見ていない
    await b.sample(DHCSR, S_HALT)
    assert b.fires == [] and dut.matched.value == 1 and dut.seen_any.value == 1 and dut.count.value == 0


@cocotb.test()
async def other_addresses_and_bad_acks_are_ignored(dut):
    b = await setup(dut)
    await b.sample(DHCSR, 0)
    await b.sample(DHCSR + 4, S_HALT)                     # 違う番地
    await b.sample(DHCSR, S_HALT, ack=WAIT)               # ACK が OK でない
    assert b.fires == [] and dut.seen.value == 1
    await b.sample(DHCSR, S_HALT)
    assert len(b.fires) == 1


@cocotb.test()
async def enable_rising_clears(dut):
    b = await setup(dut)
    await b.sample(DHCSR, 0)
    await b.sample(DHCSR, S_HALT)
    assert dut.count.value == 1
    dut.enable.value = 0
    await b.sample(DHCSR, 0)                              # 無効の間は数えない
    assert dut.seen.value == 2
    await RisingEdge(dut.clk)
    dut.enable.value = 1
    await RisingEdge(dut.clk)
    await RisingEdge(dut.clk)
    await FallingEdge(dut.clk)
    assert dut.count.value == 0 and dut.seen.value == 0 and dut.seen_any.value == 0
    await b.sample(DHCSR, S_HALT)                         # 消した後の最初の標本: 出さない
    assert len(b.fires) == 1


# Cortex-A7 × 4（BCM2836）: デバッグ部 0x80010000 + 0x2000 × n、DBGDSCR は +0x88、HALTED は bit0
DSCR = [0x80010088 + 0x2000 * n for n in range(4)]
CORE_BITS = 0x6000                                       # 4 つの番地で違うビット（13・14）
HALTED = 1


async def round_of(b, halted, gap=1):
    """4 コアの DSCR を 1 巡回ぶん（サンプラのか所 n = コア n、1 語）入れる。"""
    for n, a in enumerate(DSCR):
        await b.sample(a, 0x03000002 | (HALTED if n in halted else 0), gap=gap, index=16 * n)


@cocotb.test()
async def interleaved_cores_fire_once_when_one_of_them_enters(dut):
    b = await setup(dut, mask=HALTED, value=HALTED, addr=DSCR[0], addr_mask=0xFFFFFFFF & ~CORE_BITS)
    await round_of(b, set())
    await round_of(b, set())
    assert b.fires == []
    await round_of(b, {2})                                # コア 2 だけが止まった（他は走っている標本が交互に来る）
    assert len(b.fires) == 1 and int(dut.fire_index.value) == 32
    await round_of(b, {2})
    await round_of(b, {2})
    assert len(b.fires) == 1                              # 止まったまま: 他のコアの不一致の標本を挟んでも出さない
    await round_of(b, {0, 2})                             # コア 0 も止まった
    assert len(b.fires) == 2 and int(dut.fire_index.value) == 0 and dut.count.value == 2


@cocotb.test()
async def each_index_has_its_own_first_sample(dut):
    b = await setup(dut, mask=HALTED, value=HALTED, addr=DSCR[0], addr_mask=0xFFFFFFFF & ~CORE_BITS)
    await b.sample(DSCR[0], 0, index=0)                   # コア 0 は走っているのを見た
    await b.sample(DSCR[1], HALTED, index=16)             # コア 1 は最初の標本ですでに止まっている: 入った瞬間は見ていない
    await b.sample(DSCR[1], HALTED, index=16)
    assert b.fires == []
    await b.sample(DSCR[0], HALTED, index=0)              # コア 0 は走っている → 止まった
    assert len(b.fires) == 1 and int(dut.fire_index.value) == 0


@cocotb.test()
async def the_address_mask_ignores_only_its_bits(dut):
    b = await setup(dut, mask=HALTED, value=HALTED, addr=DSCR[0], addr_mask=0xFFFFFFFF & ~CORE_BITS)
    await b.sample(DSCR[0] + 4, 0, index=1)               # マスクの外のビットが違う番地: 比べない
    await b.sample(DSCR[0] + 4, HALTED, index=1)
    await b.sample(DSCR[0] + 0x8000, 0, index=2)          # bit15 はマスクされていない
    await b.sample(DSCR[0] + 0x8000, HALTED, index=2)
    assert b.fires == [] and dut.seen.value == 0


@cocotb.test()
async def prev_time_is_the_previous_sample_of_the_same_index(dut):
    b = await setup(dut, mask=HALTED, value=HALTED, addr=DSCR[0], addr_mask=0xFFFFFFFF & ~CORE_BITS)
    t1 = await b.sample(DSCR[1], 0, index=16)             # コア 1 の直前の（走っている）標本
    await b.sample(DSCR[0], 0, index=0)                   # 間に別のコアの標本
    t_fire = await b.sample(DSCR[1], HALTED, index=16)
    assert (int(dut.fire_time.value), int(dut.prev_time.value)) == (t_fire, t1)


@cocotb.test()
async def once_fires_only_for_the_first_entry_and_keeps_its_facts(dut):
    """once = 1（停止の見張り）: enable の後に最初に入った 1 回だけ発火し、その番号・時刻を残す。後から止まった他のコア
    （FPGA が止めた）や、同じコアの入り直しでは発火しない（数も時刻も上書きしない）。"""
    b = await setup(dut, mask=HALTED, value=HALTED, addr=DSCR[0], addr_mask=0xFFFFFFFF & ~CORE_BITS)
    dut.once.value = 1
    await round_of(b, set())
    t_prev = await b.sample(DSCR[1], 0, index=16)
    t_fire = await b.sample(DSCR[1], HALTED, index=16)                   # コア 1 が止まった
    await round_of(b, {0, 1, 2, 3})                                      # 残りも止まった（FPGA が止めた）
    await round_of(b, set())
    await round_of(b, {3})
    assert len(b.fires) == 1 and dut.count.value == 1 and int(dut.fire_index.value) == 16
    assert (int(dut.fire_time.value), int(dut.prev_time.value)) == (t_fire, t_prev)
    dut.once.value = 0


@cocotb.test()
async def times_are_the_samples_own_times(dut):
    """R6（docs/superpowers/specs/2026-11-01-time-accuracy-design.md）: fire_time・prev_time は標本の時刻（サンプラが読みに
    行った転送の線上の点）で、標本を受けた周期ではない。受けた周期と無関係な時刻を標本に付けて確かめる。"""
    b = await setup(dut)
    b.stamp = 0x123456789ABC
    await b.sample(DHCSR, 0)
    b.stamp = 0x0000000F0000
    await b.sample(DHCSR, 0)
    b.stamp = 0x0000000F1234
    await b.sample(DHCSR, S_HALT)
    assert len(b.fires) == 1
    assert (int(dut.fire_time.value), int(dut.prev_time.value)) == (0x0000000F1234, 0x0000000F0000)


def unmatched(dut) -> int:
    return int(dut.unmatched_seen.value)


async def settle(dut):
    """最後の標本が 2 段目を通るまで（1 周期）待ち、値を読める所へ。"""
    await RisingEdge(dut.clk)
    await FallingEdge(dut.clk)


@cocotb.test()
async def unmatched_seen_marks_each_index_seen_not_matching(dut):
    """停止の見張りの穴（2026-11-03、docs/superpowers/specs/2026-10-29-halt-watch-design.md §3.6）: 番号ごとに「有効にした後に
    不一致の標本を見た」を出す。全コアが止まっている間に有効にし（一致の標本だけ）、走っている標本を見た番号だけが 1 になる。
    ACK が OK でない標本・番地の違う標本では立たない。一致の標本に戻っても消えない（有効の 0→1 でだけ消える）。"""
    b = await setup(dut, mask=HALTED, value=HALTED, addr=DSCR[0], addr_mask=0xFFFFFFFF & ~CORE_BITS)
    await round_of(b, {0, 1, 2, 3})                                       # 止まったまま: どの番号も不一致を見ていない
    await settle(dut)
    assert unmatched(dut) == 0 and b.fires == []
    await b.sample(DSCR[1], 0, index=16)                                  # コア 1 が走っている
    await b.sample(DSCR[2], 0, ack=WAIT, index=32)                        # ACK が OK でない: 見ていない
    await b.sample(DSCR[0] + 4, 0, index=5)                               # 違う番地: 比べない
    await settle(dut)
    assert unmatched(dut) == 1 << 16
    await round_of(b, set())
    await settle(dut)
    assert unmatched(dut) == 1 << 0 | 1 << 16 | 1 << 32 | 1 << 48
    await round_of(b, {0, 1, 2, 3})                                       # 止まった（入った）: 立ったまま
    await settle(dut)
    assert unmatched(dut) == 1 << 0 | 1 << 16 | 1 << 32 | 1 << 48 and len(b.fires) == 4
    dut.enable.value = 0
    await RisingEdge(dut.clk)
    dut.enable.value = 1
    await settle(dut)
    await settle(dut)
    assert unmatched(dut) == 0                                            # 有効の 0→1 で消える


@cocotb.test()
async def unmatched_seen_keeps_recording_after_a_once_fire(dut):
    """once で発火を止めた後も、不一致の標本は記録し続ける（発火を見逃したかをホストが確かめるのに使う）。"""
    b = await setup(dut, mask=HALTED, value=HALTED, addr=DSCR[0], addr_mask=0xFFFFFFFF & ~CORE_BITS)
    dut.once.value = 1
    await round_of(b, {0, 1, 2, 3})
    await b.sample(DSCR[3], 0, index=48)
    await b.sample(DSCR[3], HALTED, index=48)                             # コア 3: 走った → 止まった（発火）
    await b.sample(DSCR[0], 0, index=0)                                   # 発火の後に走っているのを見たコア 0
    await settle(dut)
    assert len(b.fires) == 1 and int(dut.fire_index.value) == 48
    assert unmatched(dut) == 1 << 48 | 1 << 0
    dut.once.value = 0


# 門（after_start）と最初の標本（first_sample）: docs/superpowers/specs/2026-11-03-halt-watch-fpga-restart-design.md §3.1
RESTARTED = 1 << 1
H_R = HALTED | RESTARTED                                  # 再開の後に止まった（再開の要求の後に読んだときだけ、この意味）


async def pulse_start(dut, time):
    """start を 1 周期（start_time = time）。"""
    await RisingEdge(dut.clk)
    dut.start.value, dut.start_time.value = 1, time
    await RisingEdge(dut.clk)
    dut.start.value = 0


async def gated(dut, *, first_sample, once=1):
    b = await setup(dut, mask=H_R, value=H_R, addr=DSCR[0], addr_mask=0xFFFFFFFF & ~CORE_BITS)
    dut.enable.value = 0
    await RisingEdge(dut.clk)
    dut.after_start.value, dut.first_sample.value, dut.once.value = 1, first_sample, once
    dut.enable.value = 1                                  # 0→1: 門は閉じる
    await RisingEdge(dut.clk)
    await RisingEdge(dut.clk)
    return b


async def cores(b, values):
    """4 コアの DSCR を 1 巡回（values[n] = コア n の値）。"""
    for n, (a, v) in enumerate(zip(DSCR, values)):
        await b.sample(a, 0x03000000 | v, gap=1, index=16 * n)


@cocotb.test()
async def before_start_no_sample_is_compared(dut):
    """after_start = 1: start を受けるまで標本を比べない（数えず、番号ごとの状態・unmatched も変えない）。"""
    b = await gated(dut, first_sample=1)
    await cores(b, [H_R, H_R, H_R, H_R])                  # 再開の前: 前回の再開から止まったまま（1, 1）
    await cores(b, [0, 0, 0, 0])
    await settle(dut)
    assert b.fires == [] and dut.seen.value == 0 and dut.seen_any.value == 0 and unmatched(dut) == 0
    assert dut.started.value == 0
    await pulse_start(dut, 0x1234)
    await settle(dut)
    assert dut.started.value == 1
    await cores(b, [RESTARTED, RESTARTED, RESTARTED, RESTARTED])   # 走っている
    await settle(dut)
    assert dut.seen.value == 4 and b.fires == []


@cocotb.test()
async def a_core_halted_again_before_its_first_read_fires_on_that_read(dut):
    """再開の要求の後の最初の標本で、すでに（HALTED, RESTARTED）=（1, 1）のコア（再開してすぐ止まった）で発火する
    （first_sample = 1）。fire.first = 1、prev_time = start_time。要求を受けてまだデバッグ状態を出ていない（1, 0）・
    走っている（0, 1）では発火しない。once: 後から止まったコアでは発火しない。"""
    b = await gated(dut, first_sample=1)
    await cores(b, [H_R, H_R, H_R, H_R])                  # 門の前（無視される）
    await pulse_start(dut, 0xABCDE)
    t0 = await b.sample(DSCR[0], H_R, gap=1, index=0)     # コア 0: 再開してすぐ止まった
    await b.sample(DSCR[1], HALTED, gap=1, index=16)      # コア 1: まだデバッグ状態（RESTARTED = 0）
    await b.sample(DSCR[2], RESTARTED, gap=1, index=32)   # コア 2: 走っている
    await settle(dut)
    assert len(b.fires) == 1 and int(dut.fire_index.value) == 0 and dut.fire_first.value == 1
    assert (int(dut.fire_time.value), int(dut.prev_time.value)) == (t0, 0xABCDE)
    await cores(b, [H_R, H_R, H_R, H_R])                  # 残りも止まった（FPGA が止めた）: once
    await settle(dut)
    assert len(b.fires) == 1 and dut.count.value == 1 and int(dut.fire_index.value) == 0
    assert unmatched(dut) == 1 << 16 | 1 << 32            # 不一致を見た番号（コア 1・2）


@cocotb.test()
async def without_first_sample_a_matching_first_read_does_not_fire(dut):
    """first_sample = 0（今までの規則）: 門の後でも、その番号の最初の標本では発火しない。後の不一致 → 一致では発火し、
    fire.first = 0、prev_time = 直前の不一致の標本の時刻。"""
    b = await gated(dut, first_sample=0)
    await pulse_start(dut, 0x777)
    await b.sample(DSCR[0], H_R, gap=1, index=0)
    await settle(dut)
    assert b.fires == []
    t_prev = await b.sample(DSCR[1], RESTARTED, gap=1, index=16)
    t_fire = await b.sample(DSCR[1], H_R, gap=1, index=16)
    await settle(dut)
    assert len(b.fires) == 1 and int(dut.fire_index.value) == 16 and dut.fire_first.value == 0
    assert (int(dut.fire_time.value), int(dut.prev_time.value)) == (t_fire, t_prev)


@cocotb.test()
async def enable_rising_closes_the_gate(dut):
    """有効の 0→1 で started は消え、門は閉じる（次の start まで比べない）。"""
    b = await gated(dut, first_sample=1)
    await pulse_start(dut, 1)
    await settle(dut)
    assert dut.started.value == 1
    dut.enable.value = 0
    await RisingEdge(dut.clk)
    dut.enable.value = 1
    await settle(dut)
    await settle(dut)
    assert dut.started.value == 0
    await cores(b, [H_R, H_R, H_R, H_R])
    await settle(dut)
    assert b.fires == [] and dut.seen.value == 0


@cocotb.test()
async def without_after_start_the_start_pulse_changes_nothing_but_the_first_sample_rule(dut):
    """after_start = 0: start に関係なく比べる。first_sample = 1 なら最初の標本の一致で発火し、prev_time は start_time
    （start を受けていなければ 0）。"""
    b = await setup(dut, mask=H_R, value=H_R, addr=DSCR[0], addr_mask=0xFFFFFFFF & ~CORE_BITS)
    dut.enable.value = 0
    await RisingEdge(dut.clk)
    dut.first_sample.value = 1
    dut.enable.value = 1
    await RisingEdge(dut.clk)
    await RisingEdge(dut.clk)
    await b.sample(DSCR[3], H_R, gap=1, index=48)
    await settle(dut)
    assert len(b.fires) == 1 and dut.fire_first.value == 1 and int(dut.prev_time.value) == 0
    dut.first_sample.value = 0
