"""usb_core_cdc のテストベンチ。usb 60 MHz 相当・sys 100 MHz 相当。

約束（rtl/usb/usb_core_cdc.vhd）: 各チャンネルのバイト（と last）が順に欠けなく反対のドメインへ届く。ボーレートは
usb の line_changed で sys へ届き、CTS は usb の serial_dsr へ、high_speed はパケットサイズ（512 / 64）になる。"""

import random

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, ReadOnly, RisingEdge

# (送り手の接頭辞, 受け手の接頭辞, 送り手の時計, last を持つか)
CHANNELS = [
    ("u_ctrl_out", "s_ctrl_in", "usb_clk", True),
    ("s_ctrl_out", "u_ctrl_in", "sys_clk", True),
    ("u_dap_out", "s_dap_in", "usb_clk", True),
    ("s_dap_out", "u_dap_in", "sys_clk", True),
    ("u_uart_out", "s_uart_in", "usb_clk", False),
    ("s_uart_out", "u_uart_in", "sys_clk", False),
    ("s_swo_out", "u_swo_in", "sys_clk", False),
    ("s_stream_out", "u_stream_in", "sys_clk", False),
]
RX_CLOCK = {"u": "usb_clk", "s": "sys_clk"}


def sig(dut, prefix, name):
    return getattr(dut, f"{prefix}_{name}")


async def send(dut, prefix, clk, words, has_last):
    clock = getattr(dut, clk)
    for data, last in words:
        sig(dut, prefix, "valid").value = 1
        sig(dut, prefix, "data").value = data
        if has_last:
            sig(dut, prefix, "last").value = last
        while True:
            await RisingEdge(clock)
            if sig(dut, prefix, "ready").value == 1:
                break
        sig(dut, prefix, "valid").value = 0
        if random.random() < 0.3:
            await ClockCycles(clock, random.randint(1, 3))


async def receive(dut, prefix, n, has_last):
    clock = getattr(dut, RX_CLOCK[prefix[0]])
    got = []
    while len(got) < n:
        await RisingEdge(clock)                    # この辺で前の周期の valid・ready が見える
        if sig(dut, prefix, "valid").value == 1 and sig(dut, prefix, "ready").value == 1:
            last = int(sig(dut, prefix, "last").value) if has_last else 0
            got.append((int(sig(dut, prefix, "data").value), last))
        sig(dut, prefix, "ready").value = int(len(got) < n and random.random() < 0.7)
    return got


async def start(dut):
    cocotb.start_soon(Clock(dut.usb_clk, 16, unit="ns").start())
    cocotb.start_soon(Clock(dut.sys_clk, 10, unit="ns").start())
    for tx, rx, _, has_last in CHANNELS:
        sig(dut, tx, "valid").value = 0
        sig(dut, tx, "data").value = 0
        if has_last:
            sig(dut, tx, "last").value = 0
        sig(dut, rx, "ready").value = 0
    dut.u_high_speed.value = 0
    dut.u_line_rate.value = 0
    dut.u_line_changed.value = 0
    dut.s_swo_flush.value = 0
    dut.s_uart_cts.value = 1
    dut.usb_rst.value = 1
    dut.sys_rst.value = 1
    await ClockCycles(dut.usb_clk, 5)
    dut.usb_rst.value = 0
    dut.sys_rst.value = 0
    await ClockCycles(dut.usb_clk, 10)


@cocotb.test()
async def every_channel_delivers_bytes_in_order(dut):
    await start(dut)
    random.seed(1)
    tasks = []
    for tx, rx, clk, has_last in CHANNELS:
        words = [(random.randrange(256), int(has_last and (i % 7 == 6))) for i in range(40)]
        cocotb.start_soon(send(dut, tx, clk, words, has_last))
        tasks.append((tx, words, cocotb.start_soon(receive(dut, rx, len(words), has_last))))
    for tx, words, task in tasks:
        got = await task
        assert got == words, f"{tx}: {got} != {words}"


@cocotb.test()
async def side_signals_cross(dut):
    await start(dut)
    assert int(dut.s_packet_size.value) == 64
    dut.u_high_speed.value = 1
    await ClockCycles(dut.sys_clk, 5)
    assert int(dut.s_packet_size.value) == 512

    dut.u_line_rate.value = 115200
    dut.u_line_changed.value = 1
    await RisingEdge(dut.usb_clk)
    dut.u_line_changed.value = 0
    loads = 0
    for _ in range(40):
        await RisingEdge(dut.sys_clk)
        await ReadOnly()
        if dut.s_uart_baud_load.value == 1:
            loads += 1
            assert int(dut.s_uart_baud.value) == 115200
    assert loads == 1

    await RisingEdge(dut.usb_clk)
    assert dut.u_serial_dsr.value == 1
    dut.s_uart_cts.value = 0
    await ClockCycles(dut.usb_clk, 4)
    assert dut.u_serial_dsr.value == 0
