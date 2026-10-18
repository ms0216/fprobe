"""pin_select（端子ごとの駆動の選択）のテストベンチ。PINS = 8、ACTIONS = 2、PIN_CHOICES = (4, 5, 6, 7)。組み合わせ回路。"""

import cocotb
from cocotb.triggers import Timer

PIN_CHOICES = (4, 5, 6, 7)


async def settle():
    await Timer(1, unit="ns")


def apply(dut, *, gpio_drive=0, gpio_enable=0, func_o=0, func_oe=0, xout_o=0, xout_oe=0, xout_claim=0,
          pins=(0, 0)):
    dut.gpio_drive.value = gpio_drive
    dut.gpio_enable.value = gpio_enable
    dut.func_o.value = func_o
    dut.func_oe.value = func_oe
    dut.xout_o.value = xout_o
    dut.xout_oe.value = xout_oe
    dut.xout_claim.value = xout_claim
    dut.xout_pin.value = pins[0] | pins[1] << 2


@cocotb.test()
async def functions_drive_when_nothing_is_claimed(dut):
    apply(dut, func_o=0b1000_0001, func_oe=0b1000_0001, xout_o=0b11, xout_oe=0b11, pins=(3, 0))  # claim なし
    await settle()
    assert int(dut.pin_o.value) & 0x81 == 0x81
    assert int(dut.pin_oe.value) == 0b1000_0001
    assert int(dut.claimed.value) == 0


@cocotb.test()
async def claimed_pin_drops_the_function_and_follows_the_action(dut):
    # 動作 1 が候補 3 = 端子 7（UART TX）を claim: 機能（UART TX の休止 High）は駆動しない
    apply(dut, func_o=0b1000_0000, func_oe=0b1000_0000, xout_claim=0b10, pins=(0, 3))
    await settle()
    assert int(dut.pin_oe.value) >> 7 & 1 == 0                      # 動いていない: 高インピーダンス
    assert int(dut.claimed.value) == 0b1000_0000
    apply(dut, func_o=0b1000_0000, func_oe=0b1000_0000, xout_claim=0b10, xout_oe=0b10, xout_o=0, pins=(0, 3))
    await settle()
    assert int(dut.pin_oe.value) >> 7 & 1 == 1 and int(dut.pin_o.value) >> 7 & 1 == 0


@cocotb.test()
async def each_choice_maps_to_its_pin_for_each_action(dut):
    for k in range(2):
        for j, pin in enumerate(PIN_CHOICES):
            pins = [0, 0]
            pins[k] = j
            apply(dut, xout_claim=1 << k, xout_oe=1 << k, xout_o=1 << k, pins=tuple(pins))
            await settle()
            assert int(dut.claimed.value) == 1 << pin, (k, j)
            assert int(dut.pin_oe.value) == 1 << pin and int(dut.pin_o.value) == 1 << pin, (k, j)


@cocotb.test()
async def two_actions_drive_two_pins(dut):
    apply(dut, xout_claim=0b11, xout_oe=0b11, xout_o=0b01, pins=(0, 3))
    await settle()
    assert int(dut.claimed.value) == 0b1001_0000
    assert int(dut.pin_oe.value) == 0b1001_0000 and int(dut.pin_o.value) & 0b1001_0000 == 0b0001_0000


@cocotb.test()
async def gpio_overrides_everything(dut):
    apply(dut, gpio_enable=0b0001_0001, gpio_drive=0b0000_0001, func_o=0b1, func_oe=0b1,
          xout_claim=0b01, xout_oe=0b01, xout_o=0b01, pins=(0, 0))
    await settle()
    assert int(dut.pin_oe.value) == 0b0001_0001
    assert int(dut.pin_o.value) & 0b0001_0001 == 0b0000_0001          # 端子 4 は gpio の 0
    assert int(dut.claimed.value) == 0b0001_0000
