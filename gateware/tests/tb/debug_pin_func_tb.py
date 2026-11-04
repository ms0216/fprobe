"""debug_pin_func（端子の機能の出力）のテストベンチ。debug_pin_func_harness: Primer の割り当て（SWCLK 0、TDI 1、nRESET 2、
SWDIO 3、UART TX 7、RTS/CTS の候補 (4, 1)）。組み合わせ回路。"""

import cocotb
from cocotb.triggers import Timer

SWCLK, TDI, NRESET, SWDIO, UART_TX = 0, 1, 2, 3, 7
FLOW_CHOICES = (4, 1)


async def settle():
    await Timer(1, unit="ns")


def apply(dut, **values):
    defaults = dict(swclk_o=0, swclk_oe=0, swdio_o=0, swdio_oe=0, nreset_low=0, tdi_o=0, tdi_oe=0, uart_tx=0,
                    uart_tx_oe=0, rts_claim=0, rts_pin=0, uart_rts_n=1, cts_claim=0, cts_pin=0, pins_in=0xFF)
    defaults.update(values)
    for name, value in defaults.items():
        getattr(dut, name).value = value


def bit(signal, i):
    return int(signal.value) >> i & 1


@cocotb.test()
async def each_function_drives_its_pin(dut):
    for name, pin in (("swclk", SWCLK), ("swdio", SWDIO), ("tdi", TDI), ("uart_tx", UART_TX)):
        for value in (0, 1):
            oe = "uart_tx_oe" if name == "uart_tx" else f"{name}_oe"
            o = "uart_tx" if name == "uart_tx" else f"{name}_o"
            apply(dut, **{o: value, oe: 1})
            await settle()
            assert int(dut.func_oe.value) == 1 << pin, name
            assert bit(dut.func_o, pin) == value, name


@cocotb.test()
async def nreset_is_open_drain(dut):
    apply(dut, nreset_low=1)
    await settle()
    assert int(dut.func_oe.value) == 1 << NRESET and bit(dut.func_o, NRESET) == 0
    apply(dut, nreset_low=0)
    await settle()
    assert int(dut.func_oe.value) == 0


@cocotb.test()
async def nothing_is_driven_and_cts_reads_asserted_when_idle(dut):
    apply(dut)
    await settle()
    assert int(dut.func_oe.value) == 0
    assert int(dut.uart_cts_n.value) == 0                          # 取っていない CTS は出ているとみなす


@cocotb.test()
async def rts_drives_the_chosen_candidate(dut):
    for j, pin in enumerate(FLOW_CHOICES):
        for level in (0, 1):
            apply(dut, rts_claim=1, rts_pin=j, uart_rts_n=level, tdi_o=1, tdi_oe=1)
            await settle()
            assert bit(dut.func_oe, pin) == 1 and bit(dut.func_o, pin) == level
    apply(dut, rts_claim=1, rts_pin=2, uart_rts_n=0)               # 候補に無い番号は何もしない
    await settle()
    assert int(dut.func_oe.value) == 0


@cocotb.test()
async def cts_releases_the_pin_and_reads_it(dut):
    for j, pin in enumerate(FLOW_CHOICES):
        for level in (0, 1):
            pins = 0xFF & ~(1 << pin) | (level << pin)
            apply(dut, cts_claim=1, cts_pin=j, pins_in=pins, tdi_o=1, tdi_oe=1)
            await settle()
            assert bit(dut.func_oe, pin) == 0                       # 機能の出力を止める（TDI を取っても）
            assert int(dut.uart_cts_n.value) == level
    apply(dut, cts_claim=1, cts_pin=3, pins_in=0x00)               # 候補に無い番号: 出ているとみなす
    await settle()
    assert int(dut.uart_cts_n.value) == 0
