"""Primer 20K + Nucleo F401RE を JTAG（CMSIS-DAP の JTAG コマンド）で pyOCD から使う（M5）。

配線は SWD と同じ J12: TCK = SWCLK（PA14）、TMS = SWDIO（PA13）、TDI（PA15）、TDO = SWO のピン（PB3）。
STM32F4 の JTAG チェーンは TDO 側から Cortex-M4 の JTAG-DP（IR 4）、バウンダリスキャン TAP（IR 5）。
最後に SWD に戻して（pyOCD の SWD 接続が JTAG→SWD の切り替えを送る）他のテストに影響を残さない。
"""

import random

import pytest

pytest.importorskip("pyocd")

from tests.hil.program import program  # noqa: E402
from fprobe.session import Session  # noqa: E402
from fprobe_gw.usb.fprobe_device import STRINGS, STR_SERIAL  # noqa: E402

pytestmark = pytest.mark.hil

SERIAL = STRINGS[STR_SERIAL - 1]
STM32_CHAIN = [4, 5]
JTAG_DP_IDCODE = 0x4BA00477
SRAM = 0x20004000


@pytest.fixture(scope="module")
def programmed():
    program("fprobe")


@pytest.mark.parametrize("frequency", [1_000_000, 8_000_000])
def test_debug_over_jtag(programmed, frequency):
    with Session.open(SERIAL) as s:
        with s.debug(target_override="stm32f401xe", frequency=frequency, jtag_chain=STM32_CHAIN) as debug:
            dp = debug.target.dp
            assert dp.dpidr.idr == JTAG_DP_IDCODE
            t = debug.target
            assert t.read32(0xE000ED00) == 0x410FC241                       # CPUID
            words = [random.Random(frequency).getrandbits(32) for _ in range(512)]
            t.write_memory_block32(SRAM, words)
            assert t.read_memory_block32(SRAM, len(words)) == words
            t.resume()
        with s.debug(target_override="stm32f401xe", connect_mode="attach") as debug:    # SWD に戻す
            assert debug.target.read32(0xE000ED00) == 0x410FC241
