"""sync_link のテストベンチ: 親として出す線を Python で読み、子として受ける線を Python で作る（どちらも同じ CRC の模型）。

線の形（親）: アイドル 0 → パルス（1 を BIT_TICKS）→ 時刻 T の 64 ビット（下位から、各 BIT_TICKS）→ CRC-8（0x07、初期値 0xFF、
送る順の 64 ビットに 1 ビットずつ）→ 0。T = 立ち上げを決めた周期の time_count、線はその 1 周期後に 1（TX_LATENCY_TICKS = 1）。
子: 入力が 1 になった周期から RX_LATENCY_TICKS（= 2）後の time_count を pair_local に。
"""

import cocotb
from cocotb.triggers import ClockCycles, FallingEdge, ReadOnly, RisingEdge

from tests.tb.common import start_clock_and_reset

BIT = 10
TX_LATENCY, RX_LATENCY = 1, 2


def crc8(bits: list[int]) -> int:
    """送る順のビットに 1 つずつかける CRC-8（多項式 x^8 + x^2 + x + 1 = 0x07、初期値 0xFF）。"""
    crc = 0xFF
    for b in bits:
        fb = ((crc >> 7) & 1) ^ b
        crc = (crc << 1) & 0xFF
        if fb:
            crc ^= 0x07
    return crc


def data_bits(t: int) -> list[int]:
    return [(t >> k) & 1 for k in range(64)]


def frame_bits(t: int) -> list[int]:
    crc = crc8(data_bits(t))
    return data_bits(t) + [(crc >> k) & 1 for k in range(8)]


async def drive_time(dut, offset: int, cycles: list):
    """time_count = 周期の数 + offset（毎周期）。cycles[0] に今の周期の数を置く。"""
    while True:
        await RisingEdge(dut.clk)
        cycles[0] += 1
        dut.time_count.value = cycles[0] + offset


async def setup(dut, *, enable=1, master=0, period=500, offset=0):
    dut.enable.value = enable
    dut.master.value = master
    dut.period.value = period
    dut.sync_in.value = 0
    dut.time_count.value = offset
    cycles = [0]
    await start_clock_and_reset(dut)
    cocotb.start_soon(drive_time(dut, offset, cycles))
    return cycles


@cocotb.test()
async def master_sends_its_time_and_crc(dut):
    await setup(dut, master=0, period=1000, offset=0x1234_5678_9ABC)          # 1 回の送り（730 刻み）より長い間隔
    await ClockCycles(dut.clk, 5)
    await FallingEdge(dut.clk)
    seen = [int(dut.time_count.value)]                 # 次の立ち上がりで DUT が見る time_count
    dut.master.value = 1                               # 最初の送りは master を 1 にした直後
    for _ in range(2):
        times = seen                                   # 各周期の time_count（ReadOnly で読んだ = 次の周期に DUT が見る値）
        while True:                                    # 立ち上がりを待つ
            await RisingEdge(dut.clk)
            await ReadOnly()
            times.append(int(dut.time_count.value))
            if int(dut.sync_out.value):
                break
        t_rise = times[-1 - TX_LATENCY]                # 立ち上げを決めた周期の time_count
        line = []
        for _ in range(BIT * 73):
            line.append(int(dut.sync_out.value))
            await RisingEdge(dut.clk)
            await ReadOnly()
        assert all(line[:BIT]), "pulse"
        bits = [line[BIT * (k + 1) + BIT // 2] for k in range(72)]
        value = sum(b << k for k, b in enumerate(bits[:64]))
        crc = sum(b << k for k, b in enumerate(bits[64:]))
        assert value == t_rise, (hex(value), hex(t_rise))
        assert crc == crc8(data_bits(value))


async def send(dut, t: int, flip: int | None = None) -> int:
    """親の線を作る。線が 1 になった周期の time_count（その周期の終わりの立ち上がりで FF が初めて 1 を取り込む）を返す。"""
    bits = frame_bits(t)
    if flip is not None:
        bits[flip] ^= 1
    await FallingEdge(dut.clk)
    t_set = int(dut.time_count.value)                  # 次の立ち上がりで DUT が見る time_count
    dut.sync_in.value = 1
    await ClockCycles(dut.clk, BIT)
    for b in bits:
        dut.sync_in.value = b
        await ClockCycles(dut.clk, BIT)
    dut.sync_in.value = 0
    await ClockCycles(dut.clk, BIT * 3)
    return t_set


@cocotb.test()
async def pair_matches_the_parent_time(dut):
    cycles = await setup(dut, offset=5000)
    seen = []

    async def watch():
        while True:
            await RisingEdge(dut.clk)
            await ReadOnly()
            if int(dut.pair_valid.value):
                seen.append((int(dut.pair_local.value), int(dut.pair_parent.value)))
    cocotb.start_soon(watch())
    for k, t in enumerate((0xDEAD_BEEF_0000_0001, 0x10, (1 << 63) | 0x55)):
        await RisingEdge(dut.clk)
        t_set = await send(dut, t)
        assert seen[k] == (t_set + RX_LATENCY, t), (seen[k], t_set, hex(t))
    assert int(dut.crc_errors.value) == 0


@cocotb.test()
async def crc_mismatch_drops_pair(dut):
    await setup(dut)
    await send(dut, 0x0123_4567_89AB_CDEF, flip=20)
    await ReadOnly()
    assert int(dut.crc_errors.value) == 1
    await RisingEdge(dut.clk)
    await send(dut, 0x42)                               # 次の正しい組は受ける
    await ReadOnly()
    assert int(dut.pair_parent.value) == 0x42 and int(dut.crc_errors.value) == 1


@cocotb.test()
async def idle_without_master_and_disabled(dut):
    await setup(dut, enable=0, master=0, period=50)
    ones = 0
    valid = 0
    for _ in range(300):
        await RisingEdge(dut.clk)
        await ReadOnly()
        ones += int(dut.sync_out.value)
        valid += int(dut.pair_valid.value)
    assert ones == 0
    await RisingEdge(dut.clk)
    await send(dut, 0x99)                               # enable = 0: 受けない
    await ReadOnly()
    assert valid == 0 and int(dut.pair_valid.value) == 0 and int(dut.crc_errors.value) == 0


@cocotb.test()
async def a_pulse_without_a_frame_is_not_a_pair(dut):
    """送りの途中で線が 0 に落ちた（親の master を 0 にした・抜けた）: 72 ビットとも 0 で読んでも組にしない。
    パルスの中央より短い雑音は受け始めない（数えない）。"""
    await setup(dut)
    valid = []

    async def watch():
        while True:
            await RisingEdge(dut.clk)
            await ReadOnly()
            if dut.pair_valid.value == 1:
                valid.append(int(dut.pair_parent.value))
    cocotb.start_soon(watch())
    await FallingEdge(dut.clk)
    dut.sync_in.value = 1                              # パルスだけ出して 0 に戻す
    await ClockCycles(dut.clk, BIT)
    dut.sync_in.value = 0
    await ClockCycles(dut.clk, BIT * 80)
    assert valid == [] and int(dut.crc_errors.value) == 1
    await FallingEdge(dut.clk)
    dut.sync_in.value = 1                              # パルスの半分より短い雑音
    await ClockCycles(dut.clk, BIT // 2 - 2)
    dut.sync_in.value = 0
    await ClockCycles(dut.clk, BIT * 80)
    assert valid == [] and int(dut.crc_errors.value) == 1
    await send(dut, 0x77)                              # その後の正しい組は受ける
    await ClockCycles(dut.clk, 2)
    assert valid == [0x77]


@cocotb.test()
async def frames_are_separated_by_two_bits_of_zero_even_with_a_short_period(dut):
    """period が 1 回の送り（730 刻み）より短くても、送りの間に 0 を 2 ビット分置く（子の時計が遅くても次のパルスの
    立ち上がりを見落とさない。最終レビュー M2）。"""
    await setup(dut, master=0, period=1)
    dut.master.value = 1                               # 最初の送りの始まりから記録する
    line = [0]                                         # master を 1 にする前の線（最初の立ち上がりも数える）
    for _ in range(BIT * 73 * 3 + 100):
        await RisingEdge(dut.clk)
        await ReadOnly()
        line.append(int(dut.sync_out.value))
    rises = [k for k in range(1, len(line)) if line[k] and not line[k - 1]]
    starts = [rises[0]]                                # 送りの始まり: 前の送り（73 記号）が終わった後の最初の立ち上がり
    for r in rises:
        if r >= starts[-1] + BIT * 73:
            starts.append(r)
    assert len(starts) >= 2
    for start, nxt in zip(starts, starts[1:]):
        assert nxt - (start + BIT * 73) >= 2 * BIT, (start, nxt)


@cocotb.test()
async def disabling_in_the_middle_of_a_frame_drops_it(dut):
    """受信の途中で enable を 0 にしたら、そのフレームは組にしない（最終レビュー M4）。"""
    await setup(dut)
    valid = []

    async def watch():
        while True:
            await RisingEdge(dut.clk)
            await ReadOnly()
            if dut.pair_valid.value == 1:
                valid.append(int(dut.pair_parent.value))

    async def disable_later():
        await ClockCycles(dut.clk, BIT * 30)
        dut.enable.value = 0
    cocotb.start_soon(watch())
    cocotb.start_soon(disable_later())
    await send(dut, 0x1234)
    await ClockCycles(dut.clk, BIT * 10)
    assert valid == []
    dut.enable.value = 1
    await send(dut, 0x99)
    await ClockCycles(dut.clk, 2)
    assert valid == [0x99]


@cocotb.test()
async def frames_start_every_period(dut):
    """送りの始まりから次の始まりまで period 刻み（period が 1 回の送りより長いとき）。"""
    await setup(dut, master=0, period=1000)
    dut.master.value = 1
    line = [0]                                         # master を 1 にする前の線（最初の立ち上がりも数える）
    for _ in range(3200):
        await RisingEdge(dut.clk)
        await ReadOnly()
        line.append(int(dut.sync_out.value))
    rises = [k for k in range(1, len(line)) if line[k] and not line[k - 1]]
    starts = [rises[0]]
    for r in rises:
        if r >= starts[-1] + BIT * 73:
            starts.append(r)
    assert [b - a for a, b in zip(starts, starts[1:])] == [1000, 1000, 1000]
