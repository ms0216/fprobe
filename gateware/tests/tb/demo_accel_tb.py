"""demo_accel（Shrike-Lite のデモ: RP2040 が SPI で使う計算器。A × B（8 ビット）を B 回の足し算で求める。
足し算は STEP_CYCLES 周期に 1 回（試験は 1。実機は 256 で、B = 255 なら 25 MHz で約 2.6 ms）。

レジスタ（16 ビット）: 0x00 ID（0x5D01）、0x01 CTRL（書くと bit0 = 1 で開始）、0x02 A、0x03 B、0x04 RESULT、
0x06 STATUS（bit0 = 計算中、bit1 = 終わった）、0x07 残りの回数。読み出しは rd_addr の 1 周期後の rd_data。

題材の仕込み（MODE 0x08。リセット後 0 = 不具合なし。実行中に入れ外しする）:
  bit0 = S1: 計算中に開始すると、残りの回数だけ数え直して結果を 0 に戻さない（generic CLEAR_ON_RESTART = true なら無効）。
  bit1 = S2: 16 ビットの LFSR の下位 S2_MASK（0x09）ビットがすべて 0 の周期に来た A・B の書き込みを捨てる（S2_MASK = 0 は捨てない）。
  bit2 = S4: A = 0xFF のときに計算中の開始が来ると、計算中のまま進まなくなる（固まり）。MODE を書くと解ける。
EVENTS（0x0A）: bit3:0 = 捨てた回数、bit7:4 = 固まった回数（下位。書くと 0 に）。"""

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, RisingEdge

ID, CTRL, A, B, RESULT, _, STATUS, LEFT, MODE, S2_MASK, EVENTS = range(11)


async def setup(dut):
    cocotb.start_soon(Clock(dut.clk, 20, unit="ns").start())
    dut.rst.value = 1
    dut.wr_valid.value = 0
    dut.wr_addr.value = 0
    dut.wr_data.value = 0
    dut.rd_addr.value = 0
    await ClockCycles(dut.clk, 5)
    dut.rst.value = 0
    await ClockCycles(dut.clk, 2)


async def write(dut, addr, data):
    dut.wr_addr.value = addr
    dut.wr_data.value = data
    dut.wr_valid.value = 1
    await RisingEdge(dut.clk)
    dut.wr_valid.value = 0


async def read(dut, addr):
    dut.rd_addr.value = addr
    await ClockCycles(dut.clk, 2)
    return int(dut.rd_data.value)


async def result(dut):
    return await read(dut, RESULT)


@cocotb.test()
async def it_multiplies_by_repeated_addition(dut):
    await setup(dut)
    assert await read(dut, ID) == 0x5D01
    await write(dut, A, 250)
    await write(dut, B, 30)
    await write(dut, CTRL, 1)
    assert await read(dut, STATUS) == 0b01                         # 計算中
    await ClockCycles(dut.clk, 40)
    assert await read(dut, STATUS) == 0b10 and await result(dut) == 7500
    await write(dut, A, 0x1FF)                                     # 8 ビットを超える分は捨てる
    assert await read(dut, A) == 0xFF


@cocotb.test()
async def zero_times_is_done_at_once(dut):
    await setup(dut)
    await write(dut, A, 5)
    await write(dut, B, 0)
    await write(dut, CTRL, 1)
    await ClockCycles(dut.clk, 3)
    assert await read(dut, STATUS) == 0b10 and await result(dut) == 0


async def restart_while_busy(dut, mode=1):
    await setup(dut)
    await write(dut, MODE, mode)                                   # bit0 = S1（計算中の開始で和を 0 に戻さない）
    await write(dut, A, 10)
    await write(dut, B, 100)
    await write(dut, CTRL, 1)
    await ClockCycles(dut.clk, 30)
    assert await read(dut, STATUS) == 0b01
    await write(dut, CTRL, 1)                                      # 待ちきれずに頼み直す
    await ClockCycles(dut.clk, 150)
    assert await read(dut, STATUS) == 0b10
    return await result(dut)


@cocotb.test()
async def a_restart_while_busy_keeps_the_partial_sum(dut):
    # 仕込んだ不具合（CLEAR_ON_RESTART = false）: 途中の和（10 × 約 30 回）に 10 × 100 を足してしまう
    r = await restart_while_busy(dut)
    assert r > 1000 and r % 10 == 0


@cocotb.test()
async def a_restart_while_busy_starts_over_when_fixed(dut):
    assert await restart_while_busy(dut) == 1000


@cocotb.test()
async def mode_zero_restart_clears_the_sum(dut):
    assert await restart_while_busy(dut, mode=0) == 1000


@cocotb.test()
async def mode_s1_keeps_the_partial_sum(dut):
    r = await restart_while_busy(dut, mode=1)
    assert r > 1000 and r % 10 == 0


@cocotb.test()
async def mode_s2_drops_writes_when_the_lfsr_matches(dut):
    await setup(dut)
    await write(dut, MODE, 0b010)
    await write(dut, S2_MASK, 1)                                   # 下位 1 ビット: 約半分を捨てる
    lost = 0
    for k in range(1, 41):
        await write(dut, B, k)
        if await read(dut, B) != k:
            lost += 1
    events = await read(dut, EVENTS)
    assert lost > 0 and events & 0xF == lost % 16


@cocotb.test()
async def s2_mask_zero_never_drops(dut):
    await setup(dut)
    await write(dut, MODE, 0b010)
    await write(dut, S2_MASK, 0)
    for k in range(1000):
        await write(dut, B, k & 0xFF)
    assert await read(dut, B) == 999 & 0xFF and await read(dut, EVENTS) == 0


async def restart_with_a(dut, a):
    await write(dut, A, a)
    await write(dut, B, 100)
    await write(dut, CTRL, 1)
    await ClockCycles(dut.clk, 30)
    await write(dut, CTRL, 1)                                      # 計算中の開始
    await ClockCycles(dut.clk, 300)
    return await read(dut, STATUS)


@cocotb.test()
async def mode_s4_sticks_only_for_a_ff_with_a_restart(dut):
    await setup(dut)
    await write(dut, MODE, 0b100)
    assert await restart_with_a(dut, 0xFE) == 0b10                # 0xFE では固まらない
    assert await read(dut, EVENTS) >> 4 == 0
    assert await restart_with_a(dut, 0xFF) == 0b01                # 計算中のまま
    assert await read(dut, EVENTS) >> 4 == 1
    left = await read(dut, LEFT)
    await ClockCycles(dut.clk, 300)
    assert await read(dut, LEFT) == left                           # 進まない


@cocotb.test()
async def writing_mode_releases_a_stuck_engine(dut):
    await setup(dut)
    await write(dut, MODE, 0b100)
    assert await restart_with_a(dut, 0xFF) == 0b01
    await write(dut, MODE, 0)
    assert await read(dut, STATUS) == 0b00                        # 待ちに戻る
    await write(dut, EVENTS, 0)
    assert await read(dut, EVENTS) == 0
