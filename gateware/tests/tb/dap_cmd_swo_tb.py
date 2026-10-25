"""dap_cmd_swo のテストベンチ（CLK_HZ = 100 MHz）。基準モデルは CMSIS-DAP の SWO コマンドの仕様:

* SWO_Baudrate: divisor = clamp(round(CLK_HZ / baud), 4, 65535)、応答は実際のボーレート CLK_HZ // divisor（baud = 0 や
  受信中は 0）。
* SWO_Mode: 0・1・（Manchester があれば）2 だけ受けて受信を止める。それ以外は DAP_ERROR で状態を変えない。
* SWO_Transport: 受信中でなければ 0・2 だけ OK。
* リセットで受信は止まり、分周比は既定（CLK_HZ // 115200）に戻る（宣言の初期値は合成で効かないので RTL がリセットで与える）。
"""

import random

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, ReadOnly, RisingEdge

from tests.tb.common import CLK_PERIOD_NS

CLK_HZ = 100_000_000
DEFAULT_DIV = CLK_HZ // 115_200
DAP_OK, DAP_ERROR = 0x00, 0xFF
ID_TRANSPORT, ID_MODE, ID_BAUD, ID_CONTROL, ID_STATUS, ID_DATA = 0x17, 0x18, 0x19, 0x1A, 0x1B, 0x1C


def model_divisor(baud: int) -> int:
    return min(max((CLK_HZ + baud // 2) // baud, 4), 65535)


async def divider_model(dut):
    """割り算器（udiv_shared）の代わり: div_start の周期の被除数・除数から商を 34 周期後に done で返す。"""
    while True:
        await RisingEdge(dut.clk)
        await ReadOnly()
        if int(dut.div_start.value) == 1:
            num, den = int(dut.div_num.value), int(dut.div_den.value)
            await ClockCycles(dut.clk, 34)
            dut.div_quot.value = (num // den) & 0xFFFFFFFF if den else 0xFFFFFFFF
            dut.div_done.value = 1
            await RisingEdge(dut.clk)
            dut.div_done.value = 0


async def setup(dut):
    dut.command.value = 0
    dut.start.value = 0
    dut.get_ack.value = 0
    dut.arg.value = 0
    dut.put_ack.value = 0
    dut.swo_error.value = 0
    dut.swo_overrun.value = 0
    dut.div_done.value = 0
    dut.div_quot.value = 0
    cocotb.start_soon(Clock(dut.clk, CLK_PERIOD_NS, unit="ns").start())
    cocotb.start_soon(divider_model(dut))
    await reset(dut)


async def reset(dut):
    dut.rst.value = 1
    await ClockCycles(dut.clk, 3)
    dut.rst.value = 0
    await RisingEdge(dut.clk)


async def command(dut, cmd: int, arg: int = 0):
    """1 コマンド実行して (put_n, put_val) を返す。arg は get_ack の周期から次の get_req まで有効。"""
    dut.command.value = cmd
    dut.start.value = 1
    await RisingEdge(dut.clk)
    dut.start.value = 0
    reply = None
    for _ in range(400):
        await ReadOnly()
        if int(dut.get_req.value) == 1:
            await RisingEdge(dut.clk)
            dut.arg.value = arg
            dut.get_ack.value = 1
            await RisingEdge(dut.clk)
            dut.get_ack.value = 0
            continue
        if int(dut.put_req.value) == 1:
            reply = (int(dut.put_n.value), int(dut.put_val.value))
            await RisingEdge(dut.clk)
            dut.put_ack.value = 1
            await RisingEdge(dut.clk)
            dut.put_ack.value = 0
            continue
        if int(dut.done.value) == 1:
            await RisingEdge(dut.clk)
            return reply
        await RisingEdge(dut.clk)
    raise AssertionError(f"command 0x{cmd:02x} が終わらない")


@cocotb.test()
async def baudrate_matches_the_model(dut):
    await setup(dut)
    rng = random.Random(19)
    bauds = [1, 2, 3, 100, 115_200, 1_000_000, CLK_HZ // 4, CLK_HZ // 4 + 1, CLK_HZ // 4 - 1, CLK_HZ // 2, CLK_HZ,
             CLK_HZ * 2, 0xFFFF_FFFF, 0x8000_0000, CLK_HZ // 65535, CLK_HZ // 65535 + 1, 1526, 1525]
    bauds += [rng.randrange(1, 30_000_000) for _ in range(20)]
    bauds += [int(10 ** rng.uniform(0, 9.6)) for _ in range(20)]
    for baud in bauds:
        n, val = await command(dut, ID_BAUD, baud)
        div = model_divisor(baud)
        assert n == 4
        assert val & 0xFFFFFFFF == CLK_HZ // div, f"baud={baud}: 応答 {val} != {CLK_HZ // div}"
        assert int(dut.swo_divisor.value) == div, f"baud={baud}: divisor {int(dut.swo_divisor.value)} != {div}"


@cocotb.test()
async def zero_baud_and_running_baud_are_refused(dut):
    await setup(dut)
    n, val = await command(dut, ID_BAUD, 1_000_000)
    assert val == CLK_HZ // 100
    n, val = await command(dut, ID_BAUD, 0)
    assert val == 0 and int(dut.swo_divisor.value) == 100
    # 上位 32 ビットが 0 でなくても、下位 32 ビットが 0 なら 0 の扱い（引数は 32 ビット）
    n, val = await command(dut, ID_BAUD, 0x1_0000_0000)
    assert val == 0 and int(dut.swo_divisor.value) == 100
    await command(dut, ID_MODE, 1)
    n, val = await command(dut, ID_CONTROL, 1)
    assert int(dut.swo_run.value) == 1
    n, val = await command(dut, ID_BAUD, 115_200)
    assert val == 0 and int(dut.swo_divisor.value) == 100
    n, val = await command(dut, ID_TRANSPORT, 0)
    assert val & 0xFF == DAP_ERROR


@cocotb.test()
async def mode_and_transport(dut):
    await setup(dut)
    for mode, ok in ((0, True), (1, True), (2, dut_has_manchester(dut)), (3, False), (0x81, False)):
        before = int(dut.swo_manchester.value)
        n, val = await command(dut, ID_MODE, mode)
        assert val & 0xFF == (DAP_OK if ok else DAP_ERROR), f"mode={mode:#x}"
        if ok:
            assert int(dut.swo_manchester.value) == (1 if mode == 2 else 0)
        else:
            assert int(dut.swo_manchester.value) == before
    for transport, ok in ((0, True), (1, False), (2, True), (3, False)):
        n, val = await command(dut, ID_TRANSPORT, transport)
        assert val & 0xFF == (DAP_OK if ok else DAP_ERROR), f"transport={transport}"
    # 受信中でも Mode は受けて止める
    await command(dut, ID_MODE, 1)
    await command(dut, ID_CONTROL, 1)
    assert int(dut.swo_run.value) == 1
    n, val = await command(dut, ID_MODE, 0)
    assert val & 0xFF == DAP_OK and int(dut.swo_run.value) == 0


def dut_has_manchester(dut) -> bool:
    return True      # tests/test_swo.py の既定は HAS_MANCHESTER = true


@cocotb.test()
async def reset_restores_defaults(dut):
    await setup(dut)
    await command(dut, ID_BAUD, 2_000_000)
    await command(dut, ID_MODE, 2)
    await command(dut, ID_CONTROL, 1)
    assert int(dut.swo_divisor.value) == 50 and int(dut.swo_run.value) == 1
    await reset(dut)
    assert int(dut.swo_run.value) == 0
    assert int(dut.swo_manchester.value) == 0
    assert int(dut.swo_divisor.value) == DEFAULT_DIV, f"リセットで既定の分周比 {DEFAULT_DIV} に戻らない: {int(dut.swo_divisor.value)}"
