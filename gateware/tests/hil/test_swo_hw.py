"""Primer 20K + Nucleo F401RE で、SWO（UART 方式）を pyOCD から受けられることを確かめる（M6 第 2 段）。

Nucleo にはファームウェアを入れない: リセット直後で止め（HCLK = HSI 16 MHz）、デバッガから TPIU・ITM を設定し、
ITM の stimulus ポートにデバッガから書いたバイトが SWO（PB3 → J12）→ fprobe → USB の SWO エンドポイントで
pyOCD に届くことを見る。最後にリセットして Nucleo の元のプログラムに戻す。
"""

import time

import pytest

pytest.importorskip("pyocd")

from tests.hil.program import program  # noqa: E402
from fprobe.session import Session  # noqa: E402
from fprobe_gw.usb.fprobe_device import STRINGS, STR_SERIAL  # noqa: E402

pytestmark = pytest.mark.hil

HCLK = 16_000_000                      # リセット直後（HSI）
BAUD = 2_000_000                       # HCLK / (ACPR + 1)
RCC_AHB1ENR, GPIOB_MODER, GPIOB_AFRL = 0x40023830, 0x40020400, 0x40020420
DBGMCU_CR, DEMCR = 0xE0042004, 0xE000EDFC
TPI_ACPR, TPI_SPPR, TPI_FFCR = 0xE0040010, 0xE00400F0, 0xE0040304
ITM_STIM0, ITM_TER, ITM_TCR, ITM_LAR = 0xE0000000, 0xE0000E00, 0xE0000E80, 0xE0000FB0


@pytest.fixture(scope="module")
def programmed():
    program("fprobe")


def setup_trace(t):
    t.write32(RCC_AHB1ENR, t.read32(RCC_AHB1ENR) | 1 << 1)                    # GPIOB
    t.write32(GPIOB_MODER, t.read32(GPIOB_MODER) & ~(3 << 6) | 2 << 6)        # PB3 = AF
    t.write32(GPIOB_AFRL, t.read32(GPIOB_AFRL) & ~(0xF << 12))                # AF0 = TRACESWO
    t.write32(DBGMCU_CR, t.read32(DBGMCU_CR) & ~(3 << 6) | 1 << 5)            # TRACE_IOEN、非同期
    t.write32(DEMCR, t.read32(DEMCR) | 1 << 24)                               # TRCENA
    t.write32(TPI_SPPR, 2)                                                    # NRZ（UART）
    t.write32(TPI_ACPR, HCLK // BAUD - 1)
    t.write32(TPI_FFCR, 0x100)                                                # フォーマッタなし
    t.write32(ITM_LAR, 0xC5ACCE55)
    t.write32(ITM_TCR, 1 << 16 | 1 << 3 | 1)                                  # ATB ID 1、ITMENA
    t.write32(ITM_TER, 1)                                                     # ポート 0


def test_itm_writes_arrive_over_swo(programmed):
    with Session.open(STRINGS[STR_SERIAL - 1]) as s, s.debug(target_override="stm32f401xe",
                                                             frequency=4_000_000) as debug:
        t, probe = debug.target, debug.probe
        try:
            t.reset_and_halt()
            setup_trace(t)
            t.flush()
            probe.swo_start(BAUD)
            message = b"fprobe SWO\n"
            for byte in message:
                t.write8(ITM_STIM0, byte)                                     # ITM: ヘッダ 0x01 + 1 バイト
            t.flush()
            received = bytearray()
            deadline = time.monotonic() + 2
            while len(received) < 2 * len(message) and time.monotonic() < deadline:
                received += probe.swo_read()
            probe.swo_stop()
            expected = b"".join(bytes([0x01, b]) for b in message)
            assert bytes(received) == expected, received.hex()
        finally:
            t.reset()                                                         # 元のプログラムに戻す
