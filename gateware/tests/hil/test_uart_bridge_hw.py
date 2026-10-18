"""Primer 20K + Nucleo F401RE で UART ブリッジ（M6）を確かめる。`pytest gateware --hil`。

Nucleo にはファームウェアを入れない: コアを SWD（fprobe の CMSIS-DAP）で止めたまま USART1（PA9 = TX、PA10 = RX、
J12 に配線済み）をレジスタで設定し、データレジスタを SWD で読み書きする。PC 側は fprobe の COM ポート。
"""

import time

import pytest

pyocd = pytest.importorskip("pyocd")
import serial  # noqa: E402
from pyocd.core.helpers import ConnectHelper  # noqa: E402
from serial.tools import list_ports  # noqa: E402

from tests.hil.program import program  # noqa: E402
from fprobe.pyocd_targets import register  # noqa: E402
from fprobe_gw.usb.fprobe_device import PID, STRINGS, STR_SERIAL, VID  # noqa: E402

pytestmark = pytest.mark.hil

RCC_AHB1ENR, RCC_APB2ENR = 0x40023830, 0x40023844
GPIOA_MODER, GPIOA_AFRH = 0x40020000, 0x40020024
USART1_SR, USART1_DR, USART1_BRR, USART1_CR1 = 0x40011000, 0x40011004, 0x40011008, 0x4001100C
HSI_HZ = 16_000_000                     # リセット直後の APB2 クロック


def fprobe_com_port() -> str:
    ports = [p.device for p in list_ports.comports() if p.vid == VID and p.pid == PID]
    assert len(ports) == 1, ports
    return ports[0]


@pytest.fixture(scope="module")
def target():
    program("fprobe")
    register()
    with ConnectHelper.session_with_chosen_probe(unique_id=STRINGS[STR_SERIAL - 1], target_override="stm32f401xe",
                                                 frequency=4_000_000, blocking=False,
                                                 options={"connect_mode": "attach", "reset_type": "hw"}) as s:
        t = s.target
        t.reset_and_halt()
        t.write32(RCC_AHB1ENR, t.read32(RCC_AHB1ENR) | 0x1)                 # GPIOA
        t.write32(RCC_APB2ENR, t.read32(RCC_APB2ENR) | (1 << 4))            # USART1
        moder = t.read32(GPIOA_MODER) & ~((3 << 18) | (3 << 20))
        t.write32(GPIOA_MODER, moder | (2 << 18) | (2 << 20))               # PA9 / PA10 を AF
        afrh = t.read32(GPIOA_AFRH) & ~0xFF0
        t.write32(GPIOA_AFRH, afrh | (7 << 4) | (7 << 8))                   # AF7 = USART1
        yield t
        t.reset()


def set_target_baud(t, baud):
    t.write32(USART1_CR1, 0)
    t.write32(USART1_BRR, round(HSI_HZ / baud))
    t.write32(USART1_CR1, (1 << 13) | (1 << 3) | (1 << 2))                  # UE, TE, RE
    t.read32(USART1_SR)
    t.read32(USART1_DR)                                                     # 受信の残りを捨てる


def target_send(t, data: bytes):
    for byte in data:
        while not t.read32(USART1_SR) & (1 << 7):                           # TXE
            pass
        t.write32(USART1_DR, byte)
    t.flush()                                                               # pyOCD は書き込みを溜めて送る


def target_receive(t, count: int, timeout_s=2.0) -> bytes:
    out = bytearray()
    deadline = time.monotonic() + timeout_s
    while len(out) < count and time.monotonic() < deadline:
        if t.read32(USART1_SR) & (1 << 5):                                  # RXNE
            out.append(t.read32(USART1_DR) & 0xFF)
    return bytes(out)


@pytest.mark.parametrize("baud", [115_200, 9_600])
def test_bridge_both_directions_follow_com_baud(target, baud):
    set_target_baud(target, baud)
    with serial.Serial(fprobe_com_port(), baud, timeout=2) as com:
        time.sleep(0.2)
        com.reset_input_buffer()
        target_send(target, b"from target\n")
        assert com.read(12) == b"from target\n"
        com.write(b"hi")                                                    # USART1 は 1 バイトしか溜めない
        assert target_receive(target, 1) == b"h"
        assert target_receive(target, 1) in (b"i", b"")                     # 2 バイト目は重なりうる
